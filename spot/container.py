"""Composition root.

This module IS the wiring diagram. Every edge in the HLD is one assignment or
one constructor argument here — if an edge is missing from the diagram, it is
missing from this file too.
"""
from __future__ import annotations

import logging

from .bus import EventBus
from .core.admission_controller import AdmissionController
from .core.audit_log import PreemptionAuditLog
from .core.eligibility_guard import EligibilityGuard
from .core.grace_timer import GraceTimer
from .core.interruption_analytics import InterruptionAnalytics
from .core.lease_manager import SpotLeaseManager
from .core.metering import SpotMeteringRating
from .core.notice_delivery import NoticeDeliveryService
from .core.placement_adapter import PlacementAdapter
from .core.pool_view import SpotPoolView
from .core.demand_analytics import DemandTracker
from .core.saved_tasks import SavedTaskStore
from .core.pricing import Pricing
from .core.trend_service import TrendService
from .core.provisioning_adapter import ProvisioningAdapter
from .core.reclaim_handler import ReclaimOrderHandler
from .core.spot_market_api import SpotMarketAPI
from .core.telemetry import CapacitySampler, Metrics
from .core.teardown_confirmer import TeardownConfirmer
from .core.victim_selector import VictimSelector
from .config import CONFIG
from .domain.models import AVAILABILITY_ZONES
from .external.account_service import AccountService
from .external.billing import BillingSystem
from .external.capacity_ledger import CapacityLedger
from .external.forecast_headroom import ForecastHeadroom
from .external.hypervisor import Hypervisor
from .external.placement_scheduler import PlacementScheduler

log = logging.getLogger("spot.container")


class Container:
    def __init__(self):
        # ---------------- backend selection -------------------------------
        # sim: in-memory stubs. proxmox: real LXC containers + the node's
        # true capacity. Falls back to sim (and says so) if the API is down.
        self.backend = {"mode": "sim", "detail": "in-memory stubs"}
        hypervisor, host_groups = None, None
        if CONFIG.backend == "proxmox":
            try:
                from .core.capacity_helpers import proxmox_host_groups
                from .external.proxmox_hypervisor import (ProxmoxHypervisor,
                                                          fetch_node_inventory)
                inv = fetch_node_inventory()
                hypervisor = ProxmoxHypervisor()
                host_groups = proxmox_host_groups(inv)
                self.backend = {"mode": "proxmox", "node": inv["node"],
                                "cores": inv["cores"], "mem_gb": inv["mem_gb"],
                                "premium_cores": inv["premium_cores"],
                                "pve_version": inv["pve_version"]}
                log.info("backend: LIVE proxmox node %s — %d cores, %d GB, "
                         "%d cores already committed to premium guests",
                         inv["node"], inv["cores"], inv["mem_gb"],
                         inv["premium_cores"])
            except Exception as e:
                self.backend = {"mode": "sim",
                                "detail": f"proxmox unreachable, fell back to sim: {e}"}
                log.error("SPOT_BACKEND=proxmox but the API is unreachable "
                          "(%s) — falling back to sim", e)

        # ---------------- external, dashed boxes -------------------------
        self.bus = EventBus()
        self.accounts = AccountService()
        self.ledger = CapacityLedger(host_groups)
        self.forecast = ForecastHeadroom(self.ledger)
        self.scheduler = PlacementScheduler(self.ledger)
        self.hypervisor = hypervisor or Hypervisor()
        self.billing = BillingSystem()

        # ---------------- in scope ---------------------------------------
        self.audit = PreemptionAuditLog()
        # live mode: only zones with a real host group behind them exist
        zones = tuple(sorted({h.az for h in host_groups})) if host_groups \
            else AVAILABILITY_ZONES
        self.pool = SpotPoolView(self.forecast, self.bus, zones)                # edges 6,19,20,31
        self.pricing = Pricing(self.pool)
        self.trends = TrendService(self.ledger)
        self.demand = DemandTracker()
        self.saved = SavedTaskStore()
        self.guard = EligibilityGuard(self.accounts)                            # edge 5

        self.lease_manager = SpotLeaseManager(                                  # the hub
            bus=self.bus, audit=self.audit, pool=self.pool,
            ledger=self.ledger, pricing=self.pricing,
        )
        self.admission = AdmissionController(self.pool, self.lease_manager, self.pricing)  # 7,8,31

        self.placement_adapter = PlacementAdapter(self.scheduler)               # edges 9,10
        self.provisioning_adapter = ProvisioningAdapter(self.hypervisor)        # edges 11,12
        self.provisioning_adapter.saved_store = self.saved                      # stateful spot
        self.notice_delivery = NoticeDeliveryService(self.bus, self.provisioning_adapter)  # 16,17
        self.grace_timer = GraceTimer(self.audit)                               # edges 23,24,27
        self.teardown_confirmer = TeardownConfirmer(                            # edges 13,14,15
            ledger=self.ledger, audit=self.audit, bus=self.bus,
            provisioning_adapter=self.provisioning_adapter,
        )
        self.metering = SpotMeteringRating(                                     # edges 25,26
            billing=self.billing, audit=self.audit, bus=self.bus
        )
        self.analytics = InterruptionAnalytics(self.audit, self.lease_manager)  # edges 29,30
        self.victim_selector = VictimSelector(self.lease_manager)               # edges 21,22
        self.reclaim_handler = ReclaimOrderHandler(                             # edges 18,20,21
            pool=self.pool, victim_selector=self.victim_selector,
            lease_manager=self.lease_manager, bus=self.bus,
        )
        self.market = SpotMarketAPI(                                            # edges 3-7,30,32
            guard=self.guard, pool=self.pool, admission=self.admission,
            lease_manager=self.lease_manager, pricing=self.pricing,
            analytics=self.analytics,
        )

        # ---------------- telemetry (read-only; nothing depends on it) ----
        self.metrics = Metrics()
        self.sampler = CapacitySampler(
            ledger=self.ledger, pool=self.pool, lease_manager=self.lease_manager,
            interval=CONFIG.sample_interval_seconds,
        )

        # ---------------- late wiring (breaks import cycles) --------------
        self.guard.bind_lease_manager(self.lease_manager)
        self.lease_manager.placement_adapter = self.placement_adapter
        self.lease_manager.provisioning_adapter = self.provisioning_adapter
        self.lease_manager.notice_delivery = self.notice_delivery
        self.lease_manager.grace_timer = self.grace_timer
        self.lease_manager.teardown_confirmer = self.teardown_confirmer
        self.lease_manager.metering = self.metering
        self.grace_timer.lease_manager = self.lease_manager
        self.grace_timer.provisioning_adapter = self.provisioning_adapter
        self.teardown_confirmer.lease_manager = self.lease_manager

    async def start(self) -> None:
        await self.pool.start()
        await self.sampler.start()
        await self.teardown_confirmer.start()
        if hasattr(self.hypervisor, "startup_sweep"):
            # remove orphans; re-register hibernated tasks from VM metadata
            await self.hypervisor.startup_sweep(self.saved)
        log.info("spot subsystem started")

    async def stop(self) -> None:
        await self.teardown_confirmer.stop()
        await self.grace_timer.cancel_all()
        await self.sampler.stop()
        await self.pool.stop()
        if hasattr(self.hypervisor, "close"):
            await self.hypervisor.close()
        log.info("spot subsystem stopped")

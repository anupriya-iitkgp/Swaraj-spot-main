"""Teardown Confirmer (edges 13, 14, 15).

Confirms volumes detached, IPs and ports released, THEN reports capacity
genuinely returned to the ledger. Only after that is the lease closed.

If teardown stalls, the units stay in RECLAIMING forever rather than being
counted free — a stuck teardown must never look like available capacity.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..bus import EventBus, Topics
from ..config import CONFIG
from ..domain.models import Lease
from ..external.capacity_ledger import CapacityLedger
from .audit_log import PreemptionAuditLog

log = logging.getLogger("spot.teardown")


class TeardownConfirmer:
    def __init__(
        self, *, ledger: CapacityLedger, audit: PreemptionAuditLog, bus: EventBus,
        provisioning_adapter,
    ):
        self._ledger = ledger
        self._audit = audit
        self._bus = bus
        self._provisioning = provisioning_adapter
        self.lease_manager = None  # wired by the container
        self.stalled: list[str] = []
        self._stalled_leases: dict[str, Lease] = {}
        self._reaper_task = None

    async def start(self) -> None:
        """The reaper: a stalled teardown is retried until it succeeds —
        capacity may be held in RECLAIMING for a while, but never forever."""
        self._reaper_task = asyncio.create_task(self._reaper())

    async def stop(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            for lid, lease in list(self._stalled_leases.items()):
                log.warning("reaper: retrying stalled teardown of %s", lid)
                try:
                    await asyncio.wait_for(
                        self._provisioning.teardown_all(lease),
                        timeout=max(CONFIG.teardown_budget * 3, 30.0))
                    if self._provisioning.teardown_complete(lease):
                        del self._stalled_leases[lid]
                        if lid in self.stalled:
                            self.stalled.remove(lid)
                        self._audit.append("teardown_recovered", lease_id=lid)
                        await self._finish(lease)
                        log.info("reaper: teardown of %s recovered — capacity returned", lid)
                except Exception as e:
                    log.error("reaper: teardown of %s still failing: %s", lid, e)

    def _mark_stalled(self, lease: Lease, kind: str) -> None:
        if lease.lease_id not in self._stalled_leases:
            self.stalled.append(lease.lease_id)
            self._stalled_leases[lease.lease_id] = lease
            self._audit.append(kind, lease_id=lease.lease_id)

    async def confirm(self, lease: Lease) -> None:
        """Edge 13 -> 14 -> 15."""
        try:
            await asyncio.wait_for(
                self._provisioning.teardown_all(lease),
                timeout=max(CONFIG.teardown_budget, 0.1),
            )
        except asyncio.TimeoutError:
            # Capacity stays in RECLAIMING for now; the reaper keeps retrying.
            self._mark_stalled(lease, "teardown_stalled")
            log.error("teardown STALLED for %s — reaper will retry", lease.lease_id)
            return

        if not self._provisioning.teardown_complete(lease):
            self._mark_stalled(lease, "teardown_incomplete")
            return

        await self._finish(lease)

    async def _finish(self, lease: Lease) -> None:
        if lease.host_group:
            # edge 14 — only now is the capacity genuinely free
            await self._ledger.commit_capacity_returned(lease.host_group, lease.units)
            self._audit.capacity_returned(lease.lease_id, lease.host_group, lease.units)
            await self._bus.publish(
                Topics.CAPACITY_RETURNED,
                {"lease_id": lease.lease_id, "host_group": lease.host_group,
                 "units": lease.units, "ts": time.time()},
            )

        # edge 15 — CLOSED
        await self.lease_manager.close(lease)


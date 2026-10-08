"""Spot Market API — the only surface a spot customer touches.

Edges 3 (in), 4 (out), 5, 6, 7, 30, 32.

This is the service layer; `spot/api/app.py` is the thin HTTP shell over it.
Admission is synchronous and fails fast; fulfilment is kicked off in the
background so a slow hypervisor never holds the API call open.
"""
from __future__ import annotations

import asyncio
import logging

from ..domain.models import AVAILABILITY_ZONES, FLAVOURS, Lease
from .admission_controller import AdmissionController
from .eligibility_guard import EligibilityGuard
from .interruption_analytics import InterruptionAnalytics
from .pool_view import SpotPoolView
from .pricing import Pricing

log = logging.getLogger("spot.market")


class SpotMarketAPI:
    def __init__(
        self,
        *,
        guard: EligibilityGuard,
        pool: SpotPoolView,
        admission: AdmissionController,
        lease_manager,
        pricing: Pricing,
        analytics: InterruptionAnalytics,
    ):
        self._guard = guard
        self._pool = pool
        self._admission = admission
        self._leases = lease_manager
        self._pricing = pricing
        self._analytics = analytics
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------ inventory
    def inventory(self) -> dict:
        """GET /spot/inventory — sellable capacity, discount, interruption rate.

        Edge 6 for the capacity numbers, edge 30 for the interruption rate.
        """
        rates = {(r["flavour"], r["az"]): r["interruption_rate"]
                 for r in self._analytics.rate_by_flavour_az()}
        items = []
        for az in sorted(self._pool._pools):     # only zones that really exist
            pool = self._pool.pool(az)
            available = pool.available_units
            discount = self._pricing.discount_for(az)
            for flavour in FLAVOURS.values():
                if not flavour.spot_eligible:
                    continue
                items.append(
                    {
                        "az": az,
                        "flavour": flavour.name,
                        "vcpu": flavour.vcpu,
                        "ram_gb": flavour.ram_gb,
                        "max_instances": available // flavour.units,
                        "discount": discount,
                        "spot_rate_per_hour": round(
                            flavour.on_demand_rate_per_hour * (1 - discount), 4
                        ),
                        "on_demand_rate_per_hour": flavour.on_demand_rate_per_hour,
                        "interruption_rate_30d": rates.get((flavour.name, az), 0.0),
                    }
                )
        return {
            "pools": self._pool.snapshot(),
            "items": items,
            "note": "sellable capacity is a read model, stale by up to one control cycle",
        }

    # --------------------------------------------------------------- launch
    async def launch(
        self,
        *,
        tenant_id: str,
        flavour_name: str,
        count: int,
        az: str,
        idempotency_key: str | None,
        drain_seconds: float | None = 1.0,
        persist: bool = False,
        resume_vmids: list[int] | None = None,
    ) -> tuple[Lease, bool]:
        """POST /spot/leases. Returns (lease, replayed)."""
        # edge 5 — eligibility, quota, flavour
        flavour = await self._guard.validate(
            tenant_id=tenant_id, flavour_name=flavour_name, count=count, az=az
        )

        # edge 6 — the pool read is a HINT only; logged so the race is visible
        hint = self._pool.get_sellable(az)
        log.info("edge 6   pool hint: %d units available in %s", hint, az)

        # edges 7/31/8 — the atomic reserve is the decision
        lease, replayed = await self._admission.admit(
            tenant_id=tenant_id, flavour=flavour, count=count, az=az,
            idempotency_key=idempotency_key,
        )
        if not replayed:
            # stateful-spot flags ride on the lease into fulfilment
            lease.persist = persist
            lease.resume_vmids = list(resume_vmids or [])
            # fulfilment runs in the background (edges 9-12)
            task = asyncio.create_task(
                self._leases.fulfil(lease, drain_seconds=drain_seconds)
            )
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return lease, replayed

    # ------------------------------------------------------------- describe
    def describe(self, lease_id: str) -> dict:
        """GET /spot/leases/{id} — edge 32, lease state back to the API."""
        return self._leases.get(lease_id).to_dict()

    def list_leases(self, tenant_id: str | None = None) -> list[dict]:
        return [
            l.to_dict()
            for l in self._leases.all()
            if tenant_id is None or l.tenant_id == tenant_id
        ]

    async def release(self, lease_id: str) -> dict:
        """DELETE /spot/leases/{id}."""
        lease = await self._leases.release(lease_id)
        return lease.to_dict()

    async def drain_background(self) -> None:
        """Test helper: wait for in-flight fulfilment tasks."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

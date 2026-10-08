"""Spot Market API — edges 3, 4, 5, 6, 7 and 32.

HLD §6:

    Owns: The only surface a spot customer touches: inventory, launch, describe,
          terminate, interruption feed.
    Must not do: Compute capacity or make placement decisions.
    Key operations: GET /spot/inventory, POST /spot/leases,
                    GET|DELETE /spot/leases/{id}

This is the orchestration layer, not a decision layer. It runs the launch
sequence in the order HLD §7 draws it — validate, read, reserve — and every
actual decision belongs to the component it delegates to. The value it adds is
the *order*, and that order is a cost argument: the cheapest and most absolute
checks run first, so a request that was never going to succeed is rejected
before it touches the pool row that every other launch in the AZ is contending
on.

    edge 5   Eligibility & Quota Guard   entitlement, flavour, quota
    edge 6   Spot Pool View              a hint, explicitly stale
    edge 7   Admission Controller        the atomic reserve — the decision
    edge 8   Spot Lease Manager          the lease exists from here on

Fulfilment (edges 9-12) deliberately does not happen inside this call. HLD §5
marks those edges async, and HLD §11 gives admission a 200 ms p99 that a
placement round trip plus an instance boot cannot fit inside. The 201 says
"admitted", the lease reaches RUNNING shortly after, and the customer learns
about it from describe or from the event stream.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..domain.errors import LeaseNotFound, SpotError
from ..domain.models import Flavour, Lease, PoolSnapshot, PurchaseOption
from ..logging import edge, get_logger
from ..metrics import M
from .admission_controller import Admission
from .pricing import PricingEngine

log = get_logger(__name__)

__all__ = ["SpotMarketAPI", "LaunchResult", "InventoryEntry"]


@dataclass(frozen=True, slots=True)
class LaunchResult:
    lease: Lease
    admission: Admission
    #: True when this response replayed an earlier identical request.
    replayed: bool


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    az: str
    flavour: str
    vcpu: int
    memory_gb: int
    available_instances: int
    available_units: int
    discount: float
    price_per_hour: float
    list_price_per_hour: float
    interruption_rate: float | None
    interruption_sample_hours: float | None
    pool_staleness_seconds: float
    degraded: bool


class SpotMarketAPI:
    def __init__(
        self,
        *,
        settings: Settings,
        guard: Any,
        pool_view: Any,
        admission: Any,
        lease_manager: Any,
        reference_repo: Any,
        analytics: Any,
        pricing: PricingEngine,
    ) -> None:
        self._settings = settings
        self._guard = guard
        self._pool_view = pool_view
        self._admission = admission
        self._leases = lease_manager
        self._reference = reference_repo
        self._analytics = analytics
        self._pricing = pricing

    # ==================================================================
    # GET /spot/inventory
    # ==================================================================
    async def inventory(self, *, az: str | None = None) -> list[InventoryEntry]:
        """What is for sale, at what discount, at what interruption rate.

        Price and interruption rate are returned together on purpose. HLD §11
        calls the published rate the thing "customers cannot size spot workloads
        without"; showing an 80% discount without saying how often the instance
        will be taken away is the half of the trade-off that sells, not the half
        that informs.
        """
        flavours = await self._reference.list_flavours(spot_only=True)
        rates = {
            (r["flavour"], r["az"]): r
            for r in await self._analytics.published_rates()
        }
        entries: list[InventoryEntry] = []

        for snapshot in await self._pool_view.all():
            if az and snapshot.az != az:
                continue
            staleness = round(snapshot.staleness_seconds(), 2)
            for flavour in flavours:
                quote = self._pricing.indicative(snapshot, flavour)
                rate = rates.get((flavour.name, snapshot.az))
                entries.append(
                    InventoryEntry(
                        az=snapshot.az,
                        flavour=flavour.name,
                        vcpu=flavour.vcpu,
                        memory_gb=flavour.memory_gb,
                        available_instances=snapshot.capacity_for(flavour),
                        available_units=snapshot.available_units,
                        discount=quote.discount,
                        price_per_hour=round(quote.hourly, 6),
                        list_price_per_hour=round(quote.list_hourly, 6),
                        interruption_rate=(
                            round(float(rate["rate"]), 6) if rate else None
                        ),
                        interruption_sample_hours=(
                            round(float(rate["lease_hours"]), 1) if rate else None
                        ),
                        pool_staleness_seconds=staleness,
                        degraded=snapshot.degraded,
                    )
                )

        edge(log, 6, f"inventory: {len(entries)} entries", entries=len(entries), az=az)
        return entries

    # ==================================================================
    # POST /spot/leases
    # ==================================================================
    async def launch(
        self,
        *,
        tenant_id: str,
        flavour: str,
        count: int,
        az: str,
        idempotency_key: str,
        request_fingerprint: str,
        purchase_option: PurchaseOption | None,
    ) -> LaunchResult:
        """The launch sequence of HLD §7. Raises typed rejections."""
        started = time.perf_counter()
        try:
            # -- edge 5 ---------------------------------------------------
            validated = await self._guard.validate(
                tenant_id=tenant_id,
                flavour_name=flavour,
                count=count,
                az=az,
                requested_purchase_option=purchase_option,
            )

            # -- edge 7 (and 8 inside it) ---------------------------------
            admission = await self._admission.admit(
                validated=validated,
                az=az,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        except SpotError as exc:
            elapsed = time.perf_counter() - started
            M.admission_total.labels(outcome=str(exc.status)).inc()
            M.admission_latency.labels(outcome=str(exc.status)).observe(elapsed)
            edge(
                log,
                4,
                f"rejected {exc.status} {exc.code}: {exc.message}",
                tenant_id=tenant_id,
                status=exc.status,
                code=exc.code,
                latency_ms=round(elapsed * 1000, 2),
            )
            raise

        return LaunchResult(
            lease=admission.lease, admission=admission, replayed=admission.replayed
        )

    # ==================================================================
    # GET /spot/leases/{id} — edge 32
    # ==================================================================
    async def describe(self, lease_id: str, tenant_id: str) -> Lease:
        return await self._leases.describe(lease_id, tenant_id)

    async def list_leases(
        self, tenant_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[Lease]:
        return await self._leases.list_for_tenant(
            tenant_id, limit=limit, offset=offset
        )

    # ==================================================================
    # DELETE /spot/leases/{id}
    # ==================================================================
    async def terminate(self, lease_id: str, tenant_id: str) -> Lease:
        lease = await self._leases.describe(lease_id, tenant_id)
        return await self._leases.release(lease)

    # ==================================================================
    # GET /spot/interruptions
    # ==================================================================
    async def interruption_feed(
        self, *, flavour: str | None = None, az: str | None = None
    ) -> dict[str, Any]:
        """Edge 30 — the published interruption rate, with its own freshness.

        The staleness of the rate is published alongside it. A rate computed six
        hours ago is not the same product as one computed ten minutes ago, and
        HLD §11 requires at least hourly refresh — so the number that proves it
        is part of the response rather than something only operators can see.
        """
        rates = await self._analytics.published_rates(flavour=flavour, az=az)
        staleness = await self._analytics.staleness_seconds()
        return {
            "rates": [
                {
                    "flavour": r["flavour"],
                    "az": r["az"],
                    "interruption_rate_per_lease_hour": round(float(r["rate"]), 6),
                    "preemptions": r["preemptions"],
                    "lease_hours_observed": round(float(r["lease_hours"]), 1),
                    "window_start": r["window_start"].isoformat(),
                    "window_end": r["window_end"].isoformat(),
                }
                for r in rates
            ],
            "computed_seconds_ago": round(staleness, 1) if staleness is not None else None,
            "refresh_interval_seconds": self._settings.analytics_interval,
            "definition": (
                "preemptions per running lease-hour, over the stated window; "
                "a lease preempted by capacity reclaim counts once"
            ),
        }

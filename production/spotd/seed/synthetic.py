"""Synthetic dataset — a realistic mid-size region.

Everything is deterministic. There is no `random` seeded at import time and no
wall-clock dependence: the same command produces byte-identical rows on every
machine, which is what makes a failing test reproducible and a load-test number
comparable across runs.

The shape is chosen so that each part of the design has something real to act
on, rather than to be large for its own sake:

  * **3 AZs, 120 host groups of mixed size.** Mixed sizes matter for victim
    selection — the contiguity rule prefers fully draining a small host over
    partially draining a large one, and with uniform hosts that preference is
    unobservable.

  * **8 flavours, 2 of them not spot-eligible.** The licence-bound pair exercise
    the 400-not-409 branch of the Eligibility Guard. A flavour that will never
    be available as spot must not be answered with "try again later".

  * **40 tenants across four contract tiers, and three account classes.** Six
    non-SPOT tenants exist so the out-of-scope path (HLD §1) is exercised by
    ordinary traffic rather than only by a unit test, and one tenant is inactive
    so the fail-closed path has a subject.

  * **Varied webhook configuration.** Some tenants have an endpoint, some do
    not. Notice delivery must degrade to two channels for the latter, and the
    all-channels-failed credit path needs tenants for whom that can actually
    happen.

  * **A 14-day forecast trace** from `seed.trace`, materialised into
    `forecast_feed` — including the two deliberate incident windows, so the
    degradation logic has real history to be checked against.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterator

from ..domain.models import AccountClass, Flavour, HostGroup, Tenant
from ..logging import get_logger
from .trace import EPOCH, trace_point

log = get_logger(__name__)

__all__ = [
    "FLAVOURS",
    "AVAILABILITY_ZONES",
    "build_host_groups",
    "build_tenants",
    "forecast_rows",
    "dataset_summary",
]

AVAILABILITY_ZONES: tuple[str, ...] = ("az-1", "az-2", "az-3")

#: Two of these are deliberately not sellable as spot. HLD §6 gives the Guard
#: "flavour eligibility" and the licence terms on a Windows or Oracle image do
#: not survive an instance being taken away with two minutes' notice.
FLAVOURS: tuple[Flavour, ...] = (
    Flavour("s1.small", vcpu=2, memory_gb=4, spot_eligible=True, family="general"),
    Flavour("s1.medium", vcpu=4, memory_gb=8, spot_eligible=True, family="general"),
    Flavour("s1.large", vcpu=8, memory_gb=16, spot_eligible=True, family="general"),
    Flavour("s1.xlarge", vcpu=16, memory_gb=32, spot_eligible=True, family="general"),
    Flavour("c1.large", vcpu=8, memory_gb=8, spot_eligible=True, family="compute"),
    Flavour("m1.xlarge", vcpu=16, memory_gb=64, spot_eligible=True, family="memory"),
    Flavour(
        "w1.large",
        vcpu=8,
        memory_gb=16,
        spot_eligible=False,
        licence_bound=True,
        family="windows",
    ),
    Flavour(
        "o1.xlarge",
        vcpu=16,
        memory_gb=64,
        spot_eligible=False,
        licence_bound=True,
        family="oracle",
    ),
)

#: 40 host groups per AZ. The size pattern repeats every 8 so the mix is stable
#: and every AZ gets the same distribution of large and small hosts.
_HOST_SIZES: tuple[int, ...] = (32, 32, 48, 32, 64, 32, 48, 32)
_GROUPS_PER_AZ = 40

_CONTRACT_TIERS: tuple[tuple[str, int, int], ...] = (
    # (tier, spot quota in vCPU, concurrency cap)
    ("bronze", 32, 8),
    ("silver", 64, 20),
    ("gold", 128, 40),
    ("platinum", 256, 80),
)


def build_host_groups() -> list[HostGroup]:
    groups: list[HostGroup] = []
    for az_index, az in enumerate(AVAILABILITY_ZONES):
        for n in range(_GROUPS_PER_AZ):
            groups.append(
                HostGroup(
                    host_group=f"hg-{az}-{n:03d}",
                    az=az,
                    total_units=_HOST_SIZES[(n + az_index) % len(_HOST_SIZES)],
                )
            )
    return groups


def build_tenants() -> list[Tenant]:
    """40 tenants: 34 SPOT, 3 DYNAMIC, 3 STATIC, one of them inactive."""
    tenants: list[Tenant] = []

    for n in range(34):
        tier, quota, concurrency = _CONTRACT_TIERS[n % len(_CONTRACT_TIERS)]
        # Two thirds have a webhook. The rest must be served by the metadata and
        # event-stream channels alone.
        webhook = f"sim://tenant-{n:02d}.example.internal/spot-notices" if n % 3 else None
        tenants.append(
            Tenant(
                tenant_id=f"tenant-spot-{n:02d}",
                name=f"Spot Tenant {n:02d}",
                account_class=AccountClass.SPOT,
                spot_quota_units=quota,
                concurrency_cap=concurrency,
                webhook_url=webhook,
                contract_tier=tier,
                # One inactive tenant, so "entitled yesterday, not today" has a
                # subject and fail-closed is exercised by the dataset.
                active=(n != 33),
            )
        )

    for n in range(3):
        tenants.append(
            Tenant(
                tenant_id=f"tenant-dynamic-{n:02d}",
                name=f"Pay-per-use Tenant {n:02d}",
                account_class=AccountClass.DYNAMIC,
                spot_quota_units=0,
                concurrency_cap=0,
                contract_tier="standard",
            )
        )
    for n in range(3):
        tenants.append(
            Tenant(
                tenant_id=f"tenant-static-{n:02d}",
                name=f"Reserved Tenant {n:02d}",
                account_class=AccountClass.STATIC,
                spot_quota_units=0,
                concurrency_cap=0,
                contract_tier="reserved",
            )
        )
    return tenants


def forecast_rows(
    *, days: int = 14, step: timedelta = timedelta(hours=1)
) -> Iterator[dict[str, Any]]:
    """Materialise the trace into rows for `forecast_feed`.

    The same pure function the simulated feed calls at runtime, so the stored
    history and the live behaviour cannot diverge. Hourly granularity keeps the
    table small (about a thousand rows for a fortnight across three AZs) while
    still resolving the diurnal cycle and both incident windows.
    """
    capacity = az_capacity()
    end = EPOCH + timedelta(days=days)
    for az in AVAILABILITY_ZONES:
        at = EPOCH
        while at < end:
            point = trace_point(az, at)
            units = int(capacity[az] * point.fraction)
            yield {
                "az": az,
                "units": units,
                "confidence": round(point.confidence, 4),
                "published_at": point.published_at,
                "horizon_seconds": point.horizon_seconds,
                "accepted": point.incident is None,
                "applied_units": units if point.incident is None else int(units * 0.25),
                "reject_reason": point.incident,
            }
            at += step


def az_capacity() -> dict[str, int]:
    totals: dict[str, int] = {az: 0 for az in AVAILABILITY_ZONES}
    for group in build_host_groups():
        totals[group.az] += group.total_units
    return totals


def dataset_summary() -> dict[str, Any]:
    """What the seed actually contains. Printed by `spotd seed`."""
    groups = build_host_groups()
    tenants = build_tenants()
    capacity = az_capacity()
    spot_tenants = [t for t in tenants if t.account_class is AccountClass.SPOT]
    return {
        "availability_zones": list(AVAILABILITY_ZONES),
        "host_groups": len(groups),
        "total_vcpu": sum(capacity.values()),
        "vcpu_per_az": capacity,
        "host_group_sizes": sorted({g.total_units for g in groups}),
        "flavours": len(FLAVOURS),
        "spot_eligible_flavours": sum(1 for f in FLAVOURS if f.spot_eligible),
        "licence_bound_flavours": [f.name for f in FLAVOURS if f.licence_bound],
        "tenants": len(tenants),
        "spot_tenants": len(spot_tenants),
        "inactive_tenants": [t.tenant_id for t in tenants if not t.active],
        "non_spot_tenants": [
            t.tenant_id for t in tenants if t.account_class is not AccountClass.SPOT
        ],
        "tenants_with_webhook": sum(1 for t in spot_tenants if t.webhook_url),
        "total_spot_quota_units": sum(t.spot_quota_units for t in spot_tenants),
        "contract_tiers": [tier for tier, _, _ in _CONTRACT_TIERS],
    }


async def seed(db: Any, *, with_trace: bool = True, days: int = 14) -> dict[str, Any]:
    """Load the dataset. Idempotent — safe to re-run against a live database."""
    from ..db.repositories import ReferenceRepository  # local: avoids a cycle

    reference = ReferenceRepository(db)

    for flavour in FLAVOURS:
        await reference.upsert_flavour(flavour)
    for group in build_host_groups():
        await reference.upsert_host_group(group)
    for tenant in build_tenants():
        await reference.upsert_tenant(tenant)

    trace_written = 0
    if with_trace:
        rows = list(forecast_rows(days=days))
        await db.executemany(
            """
            INSERT INTO forecast_feed
                (az, units, confidence, published_at, horizon_seconds,
                 accepted, applied_units, reject_reason)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (az, published_at) DO NOTHING
            """,
            [
                (
                    r["az"],
                    r["units"],
                    r["confidence"],
                    r["published_at"],
                    r["horizon_seconds"],
                    r["accepted"],
                    r["applied_units"],
                    r["reject_reason"],
                )
                for r in rows
            ],
        )
        trace_written = len(rows)

    summary = dataset_summary()
    summary["forecast_trace_rows"] = trace_written
    summary["forecast_trace_days"] = days if with_trace else 0
    log.info("seed.complete", **{k: v for k, v in summary.items() if not isinstance(v, dict)})
    return summary

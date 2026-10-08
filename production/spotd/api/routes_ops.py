"""Operational surface: health, metrics, and the evidence endpoints.

LLD §14 defines the metrics and the log fields; this module exposes them plus
the queries an operator actually runs during an incident. Each endpoint here
answers a question the design says someone will need to ask:

    /ops/slo                is the reclaim SLO being met?          (HLD §11)
    /ops/pools              is the pool degraded, and how stale?   (HLD §12)
    /ops/notice-delivery    is the trust anchor holding?           (HLD §11, §12)
    /ops/fairness           are the same tenants absorbing it all? (HLD §12)
    /ops/reconciliation     has the pool counter drifted?          (HLD §11)
    /ops/audit/{lease_id}   what actually happened to this lease?  (HLD §11)
    /ops/invoice/{lease_id} why is this the bill?                  (HLD §10)
    /ops/audit/verify       has the audit log been tampered with?

`/ops/invoice` deserves a note. HLD §11 sets the exit criterion for the metering
phase as "invoices reproducible from the lease record alone". This endpoint is
that criterion, executable: it reconstructs the charge from stored rows only.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from ..domain.errors import LeaseNotFound
from ..domain.models import utcnow
from ..logging import get_logger
from ..metrics import CONTENT_TYPE_LATEST, render
from .deps import ContainerDep

log = get_logger(__name__)

router = APIRouter(tags=["ops"])


# ======================================================================
# health — split into liveness and readiness for Kubernetes
# ======================================================================
@router.get("/health/live", include_in_schema=False)
async def live() -> dict[str, str]:
    """Liveness: is the process running?

    Deliberately does not touch the database. A liveness probe that fails on a
    database blip restarts every replica at once, turning a recoverable
    dependency problem into an outage.
    """
    return {"status": "alive"}


@router.get("/health/ready", include_in_schema=False)
async def ready(container: ContainerDep, response: Response) -> dict[str, Any]:
    """Readiness: can this replica serve traffic?"""
    health = await container.health()
    if health["status"] != "ok":
        response.status_code = 503
    return health


@router.get("/health")
async def health(container: ContainerDep, response: Response) -> dict[str, Any]:
    return await ready(container, response)


@router.get("/metrics", include_in_schema=False)
async def metrics(container: ContainerDep) -> Response:
    # Refresh the gauges that are cheap to recompute, so a scrape is a
    # point-in-time truth rather than whatever the last write happened to leave.
    await container.lease_repo.counts_by_state()
    await container.pool_repo.publish_gauges()
    await container.outbox_repo.depth()
    return Response(content=render(), media_type=CONTENT_TYPE_LATEST)


# ======================================================================
# the evidence endpoints
# ======================================================================
@router.get("/ops/slo")
async def slo(
    container: ContainerDep,
    hours: Annotated[float, Query(gt=0, le=720)] = 24.0,
) -> dict[str, Any]:
    """Every non-functional target in HLD §11, measured."""
    window = timedelta(hours=hours)
    since = utcnow() - window

    reclaim = [r.as_dict() for r in await container.analytics.slo(window=window)]
    notice = await container.notice.delivery_rate(since)
    pools = await container.pool_repo.all()
    staleness = {p.az: round(p.staleness_seconds(), 1) for p in pools}
    rate_staleness = await container.analytics.staleness_seconds()
    reconciliation = await container.pool_repo.reconcile()

    return {
        "window_hours": hours,
        "targets": {
            "reclaim_within_grace": {
                "target": 0.999,
                "per_az": reclaim,
                "met": all(r["met"] for r in reclaim) if reclaim else None,
            },
            "notice_delivery": {
                "target": 0.9999,
                **notice,
                "met": (
                    notice["at_least_one_channel_rate"] >= 0.9999
                    if notice["at_least_one_channel_rate"] is not None
                    else None
                ),
            },
            "over_allocation": {
                "target": 0,
                "detected": sum(1 for r in reconciliation if r.over_allocated),
                "met": not any(r.over_allocated for r in reconciliation),
                "note": "HLD §11: any occurrence is a correctness bug, not a "
                "tuning issue",
            },
            "pool_staleness": {
                "target_seconds": container.settings.control_cycle,
                "per_az_seconds": staleness,
                "met": all(
                    v <= container.settings.control_cycle * 2 for v in staleness.values()
                ),
            },
            "interruption_rate_freshness": {
                "target_seconds": 3600,
                "actual_seconds": (
                    round(rate_staleness, 1) if rate_staleness is not None else None
                ),
                "met": rate_staleness is not None and rate_staleness <= 3600,
            },
        },
    }


@router.get("/ops/pools")
async def pools(container: ContainerDep) -> dict[str, Any]:
    snapshots = await container.pool_repo.all()
    return {
        "control_cycle_seconds": container.settings.control_cycle,
        "cooldown_seconds": container.settings.cooldown,
        "degraded_factor": container.settings.degraded_factor,
        "pools": [
            {
                "az": s.az,
                "sellable_units": s.sellable_units,
                "reserved_units": s.reserved_units,
                "cooldown_units": s.cooldown_units,
                "available_units": s.available_units,
                "utilisation": round(s.utilisation, 4),
                "confidence": s.confidence,
                "degraded": s.degraded,
                "cycle_seq": s.cycle_seq,
                "staleness_seconds": round(s.staleness_seconds(), 1),
            }
            for s in snapshots
        ],
    }


@router.get("/ops/reconciliation")
async def reconciliation(container: ContainerDep) -> dict[str, Any]:
    """Recompute reserved_units from the leases that should back it."""
    results = await container.pool_repo.reconcile()
    return {
        "over_allocation_detected": any(r.over_allocated for r in results),
        "per_az": [
            {
                "az": r.az,
                "pool_counter": r.counter,
                "sum_of_active_lease_units": r.actual,
                "drift": r.drift,
                "over_allocated": r.over_allocated,
            }
            for r in results
        ],
        "note": (
            "negative drift means leases hold more units than the pool admits — "
            "the atomic reserve was bypassed. Positive drift is conservative: it "
            "under-sells but never over-allocates."
        ),
    }


@router.get("/ops/notice-delivery")
async def notice_delivery(
    container: ContainerDep, hours: Annotated[float, Query(gt=0, le=720)] = 24.0
) -> dict[str, Any]:
    """Per-channel delivery, and the all-channel failure rate.

    HLD §12 warns that "three channels do not help if they share a failure
    mode". Per-channel rates are the evidence: three channels failing together,
    repeatedly, means they are not as independent as the design assumes.
    """
    return await container.notice.delivery_rate(utcnow() - timedelta(hours=hours))


@router.get("/ops/fairness")
async def fairness(
    container: ContainerDep, hours: Annotated[float, Query(gt=0, le=720)] = 24.0
) -> dict[str, Any]:
    """HLD §12's cross-tenant fairness question, answered with a number."""
    since = utcnow() - timedelta(hours=hours)
    spread = await container.analytics_repo.fairness_spread(since=since)
    per_tenant = await container.analytics_repo.per_tenant(since=since)
    return {
        "window_hours": hours,
        "spread": spread.as_dict(),
        "per_tenant": per_tenant[:50],
        "interpretation": (
            "gini > 0.4 over five or more tenants means a minority is absorbing "
            "most interruptions. HLD §12: if the spread widens, victim selection "
            "needs a fairness term."
        ),
    }


@router.get("/ops/audit/verify")
async def verify_audit(container: ContainerDep) -> dict[str, Any]:
    """Recompute the audit hash chain and report the first break."""
    result = await container.audit_repo.verify()
    return {
        "entries": result.entries,
        "valid": result.valid,
        "broken_at_id": result.broken_at,
        "detail": result.detail,
    }


@router.get("/ops/audit/{lease_id}")
async def lease_audit(lease_id: str, container: ContainerDep) -> dict[str, Any]:
    """The full evidence trail for one lease — HLD §11's dispute record."""
    entries = await container.audit_repo.for_lease(lease_id)
    if not entries:
        raise LeaseNotFound(f"no audit entries for {lease_id}")
    return {
        "lease_id": lease_id,
        "entries": [
            {
                "id": e["id"],
                "at": e["ts"].isoformat(),
                "event": e["event"],
                "order_id": e["order_id"],
                "actor": e["actor"],
                "detail": e["detail"],
                "hash": e["entry_hash"][:16],
            }
            for e in entries
        ],
    }


@router.get("/ops/invoice/{lease_id}")
async def invoice(lease_id: str, container: ContainerDep) -> dict[str, Any]:
    """Reconstruct the charge from stored rows only.

    This is HLD §11's phase-5 exit criterion made executable: "Invoices
    reproducible from the lease record alone."
    """
    lease = await container.lease_repo.get(lease_id)
    if lease is None:
        raise LeaseNotFound(f"no lease {lease_id}")

    detail = await container.billing_repo.invoice_for_lease(lease_id)
    return {
        **detail,
        "explanation": {
            "flavour": lease.flavour,
            "count": lease.count,
            "units": lease.units,
            "discount_snapshot": lease.discount_snapshot,
            "rate_per_sec": lease.rate_per_sec,
            "ran_from": lease.running_at.isoformat() if lease.running_at else None,
            "meter_stopped_at": (
                lease.notice_at.isoformat()
                if lease.notice_at
                else (lease.stopped_at.isoformat() if lease.stopped_at else None)
            ),
            "grace_seconds_excluded": lease.grace_seconds_excluded,
            "notice_channels_delivered": [
                c.value for c in lease.notice_channels_delivered
            ],
            "why_credited": (
                "no notice channel delivered — the customer lost the instance "
                "without warning (HLD §10)"
                if lease.notice_at and not lease.notice_channels_delivered
                else None
            ),
        },
    }


@router.get("/ops/outbox")
async def outbox(container: ContainerDep) -> dict[str, Any]:
    pending, dead = await container.outbox_repo.depth()
    return {
        "pending": pending,
        "dead": dead,
        "note": (
            "sustained pending growth means the relay is stuck and the ledger "
            "and event views are diverging (LLD §12.8); dead rows are parked, "
            "never dropped"
        ),
    }


@router.get("/ops/config")
async def config(container: ContainerDep) -> dict[str, Any]:
    """Effective configuration, secrets redacted."""
    settings = container.settings
    return {
        "settings": settings.describe(),
        "derived": {
            "teardown_deadline_seconds": settings.teardown_deadline,
            "grace_headroom_seconds": settings.grace_headroom,
            "invariant": "force_stop_at + teardown_budget < grace_seconds",
        },
    }


@router.get("/ops/workers")
async def workers(container: ContainerDep) -> dict[str, Any]:
    return {
        "worker_id": container.settings.worker_id,
        "workers": [
            {
                "name": w.name,
                "running": w.running,
                "leader": getattr(w, "is_leader", None),
            }
            for w in container.workers
        ],
    }

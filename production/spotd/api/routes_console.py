"""The console back-end-for-frontend.

Two jobs, and they are separate on purpose.

**Aggregation.** `GET /console/overview` answers, in one round trip, every
question the operator dashboard asks at its refresh interval. It could have been
eight endpoints and eight fetches, but a dashboard polling eight endpoints at
1 Hz distorts the very metrics it is displaying — the p99 admission latency in
LLD §14.1 stops being a measurement of customer traffic and starts being a
measurement of the dashboard. One call is one line in the access log.

**Signing.** `POST /console/actions/*` is the answer to LLD §12.1. The operator's
browser holds a session cookie; the *process* holds the HMAC key. An action route
authenticates the session, signs the equivalent internal call with
`sign_request`, and puts it through the same `SignatureVerifier` the capacity
side goes through. Nothing bypasses the signed path — the console is simply
another signed caller that happens to live inside the same process, so the nonce
is consumed, the skew is checked, and the audit trail records the action with
`actor = console`.

Everything under `/console` is cross-tenant, so everything under `/console`
requires an operator session. The tenant console is *not* served from here: it
talks to `/spot/*` with an `X-Tenant-Id` header, exactly as any other customer
client would. That is deliberate. A tenant-facing UI that reads privileged
aggregate endpoints proves nothing about the customer contract; one that can only
use the published API proves the contract is complete.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response

from ..db.repositories import fingerprint
from ..domain.errors import InvalidRequest, LeaseNotFound, Unauthenticated
from ..domain.models import ReclaimOrder, utcnow
from ..domain.state_machine import LeaseState
from ..logging import get_logger, order_context
from . import console_auth
from .auth import HEADER_CALLER, SignatureVerifier, sign_request
from .deps import ContainerDep
from .schemas import (
    HeadroomOverrideRequest,
    LeaseResponse,
    ReclaimRequest,
    ReclaimResponse,
    lease_to_response,
)

log = get_logger(__name__)

__all__ = ["router", "session_router"]

#: Session management is reachable without a session, for the obvious reason.
session_router = APIRouter(prefix="/console", tags=["console"])

#: Everything else is not.
router = APIRouter(
    prefix="/console",
    tags=["console"],
    dependencies=[Depends(console_auth.session_dependency)],
)


# ======================================================================
# session
# ======================================================================
@session_router.get("/session")
async def whoami(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        **console_auth.describe(settings, console_auth.read_session(settings, request)),
        "console_enabled": settings.console_enabled,
    }


@session_router.post("/session")
async def login(
    request: Request,
    response: Response,
    token: Annotated[str, Body(embed=True)] = "",
) -> dict[str, Any]:
    settings = request.app.state.settings
    if not settings.console_enabled:
        raise InvalidRequest("the operator console is disabled on this deployment")

    if not console_auth.verify_token(settings, token):
        # No detail about *why*. A login that distinguishes "wrong token" from
        # "no token configured" tells an attacker which one to work on.
        log.warning("console_auth.rejected", note="operator login refused")
        raise Unauthenticated("that operator token was not accepted")

    session = console_auth.issue_session(settings, response)
    log.info("console_auth.session_issued", role=session.role)
    return console_auth.describe(settings, session)


@session_router.delete("/session")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    settings = request.app.state.settings
    console_auth.clear_session(response, settings)
    return {"authenticated": False, "requires_token": console_auth.requires_token(settings)}


# ======================================================================
# GET /console/overview — the whole dashboard, one call
# ======================================================================
@router.get("/overview")
async def overview(
    container: ContainerDep,
    hours: Annotated[float, Query(gt=0, le=720)] = 24.0,
) -> dict[str, Any]:
    settings = container.settings
    since = utcnow() - timedelta(hours=hours)

    # Independent reads, so they go out together. Sequentially this is a dozen
    # round trips per poll; concurrently it is one.
    (
        pools,
        states,
        in_grace,
        reclaim_orders,
        audit,
        slo_reports,
        notice_rate,
        outbox_depth,
        reconciliation,
        fairness,
        event_counts,
        ledger_units,
    ) = await asyncio.gather(
        container.pool_repo.all(),
        container.lease_repo.counts_by_state(),
        container.lease_repo.outstanding_notices(),
        container.reclaim_repo.recent(limit=12),
        container.audit_repo.recent(limit=40),
        container.analytics.slo(window=timedelta(hours=hours)),
        container.notice.delivery_rate(since),
        container.outbox_repo.depth(),
        container.pool_repo.reconcile(),
        container.analytics_repo.fairness_spread(since=since),
        container.audit_repo.count_events(since),
        container.ledger_repo.units_by_state(),
    )

    pending, dead = outbox_depth
    now = utcnow()

    totals = {
        "sellable_units": sum(p.sellable_units for p in pools),
        "reserved_units": sum(p.reserved_units for p in pools),
        "cooldown_units": sum(p.cooldown_units for p in pools),
        "available_units": sum(p.available_units for p in pools),
    }
    totals["utilisation"] = (
        round(totals["reserved_units"] / totals["sellable_units"], 4)
        if totals["sellable_units"]
        else 0.0
    )

    return {
        "at": now.isoformat(),
        "window_hours": hours,
        "service": {
            "environment": settings.environment,
            "backend": settings.backend,
            "region": settings.region,
            "worker_id": settings.worker_id,
            "sim_enabled": settings.enable_sim_endpoints,
        },
        "policy": {
            "grace_seconds": settings.grace_seconds,
            "force_stop_at": settings.force_stop_at,
            "teardown_budget": settings.teardown_budget,
            "control_cycle": settings.control_cycle,
            "cooldown": settings.cooldown,
            "blast_radius": settings.blast_radius,
            "min_discount": settings.min_discount,
            "max_discount": settings.max_discount,
            "retry_after": settings.retry_after,
            "tenant_quota": settings.tenant_quota,
            "availability_zones": list(settings.availability_zones),
        },
        "totals": totals,
        "pools": [
            {
                "az": p.az,
                "sellable_units": p.sellable_units,
                "reserved_units": p.reserved_units,
                "cooldown_units": p.cooldown_units,
                "available_units": p.available_units,
                "utilisation": round(p.utilisation, 4),
                "confidence": p.confidence,
                "degraded": p.degraded,
                "cycle_seq": p.cycle_seq,
                "staleness_seconds": round(p.staleness_seconds(), 1),
            }
            for p in pools
        ],
        "lease_states": states,
        # The leases HLD §11's whole grace promise is currently riding on. The
        # dashboard counts down against `force_stop_deadline` rather than
        # against a client clock, so what it shows is the deadline the reaper
        # will actually act on.
        "in_grace": [
            {
                "lease_id": lease.lease_id,
                "tenant_id": lease.tenant_id,
                "az": lease.az,
                "host_group": lease.host_group,
                "units": lease.units,
                "state": lease.state.value,
                "notice_at": lease.notice_at.isoformat() if lease.notice_at else None,
                "force_stop_deadline": (
                    lease.force_stop_deadline.isoformat()
                    if lease.force_stop_deadline
                    else None
                ),
                "seconds_to_force_stop": (
                    round((lease.force_stop_deadline - now).total_seconds(), 1)
                    if lease.force_stop_deadline
                    else None
                ),
                "grace_seconds": lease.grace_seconds,
                "notice_channels_delivered": [
                    c.value for c in lease.notice_channels_delivered
                ],
                "reclaim_order_id": lease.reclaim_order_id,
            }
            for lease in in_grace
        ],
        "slo": {
            "reclaim": [r.as_dict() for r in slo_reports],
            "notice": notice_rate,
            "over_allocation": {
                "detected": sum(1 for r in reconciliation if r.over_allocated),
                "per_az": [
                    {
                        "az": r.az,
                        "counter": r.counter,
                        "actual": r.actual,
                        "drift": r.drift,
                        "over_allocated": r.over_allocated,
                    }
                    for r in reconciliation
                ],
            },
            "fairness": fairness.as_dict(),
        },
        "outbox": {"pending": pending, "dead": dead},
        "ledger": ledger_units,
        "workers": [
            {"name": w.name, "running": w.running, "leader": getattr(w, "is_leader", None)}
            for w in container.workers
        ],
        "reclaim_orders": [_order_summary(o) for o in reclaim_orders],
        "audit": [_audit_row(e) for e in audit],
        "audit_event_counts": event_counts,
    }


def _order_summary(order: Any) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "az": order.az,
        "host_group": order.host_group,
        "units": order.units,
        "units_selected": order.units_selected,
        "state": order.state.value,
        "reason": order.reason,
        "requested_by": order.requested_by,
        "received_at": order.received_at.isoformat(),
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
        "deadline": order.deadline.isoformat() if order.deadline else None,
        "detail": order.detail,
        "leases_selected": list(order.leases_selected),
        # HLD §12: a partially met order is deliberate and must stay visible —
        # the blast-radius cap can leave units unfound rather than take one
        # tenant's whole fleet.
        "partial": order.units_selected < order.units,
    }


def _audit_row(entry: Any, **known: Any) -> dict[str, Any]:
    """One audit row, in the console's shape.

    The three audit queries select different columns — `for_lease` omits
    `lease_id`, `for_order` omits `order_id` — because each already knows the
    value it filtered on. Rather than widen those queries, the caller supplies
    what it filtered on as `known`, so the response shape is the same whichever
    query produced it and the UI does not need three code paths.
    """
    return {
        "id": entry["id"],
        "at": entry["ts"].isoformat(),
        "event": entry["event"],
        "lease_id": known.get("lease_id", entry.get("lease_id")),
        "order_id": known.get("order_id", entry.get("order_id")),
        "tenant_id": known.get("tenant_id", entry.get("tenant_id")),
        "actor": entry.get("actor"),
        "detail": entry.get("detail"),
    }


# ======================================================================
# fleet, orders, evidence
# ======================================================================
@router.get("/leases", response_model=list[LeaseResponse])
async def leases(
    container: ContainerDep,
    state: Annotated[list[str] | None, Query()] = None,
    az: str | None = None,
    tenant_id: str | None = None,
    host_group: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[LeaseResponse]:
    """Every tenant's leases. The operator fleet view."""
    states: list[LeaseState] | None = None
    if state:
        try:
            states = [LeaseState(s) for s in state]
        except ValueError as exc:
            raise InvalidRequest(
                f"unknown lease state; valid states are "
                f"{', '.join(s.value for s in LeaseState)}"
            ) from exc

    rows = await container.lease_repo.list_all(
        states=states,
        az=az,
        tenant_id=tenant_id,
        host_group=host_group,
        limit=limit,
        offset=offset,
    )
    return [lease_to_response(lease) for lease in rows]


@router.get("/timeline")
async def timeline(
    container: ContainerDep,
    minutes: Annotated[float, Query(gt=0, le=10_080)] = 60.0,
    buckets: Annotated[int, Query(ge=4, le=240)] = 60,
) -> dict[str, Any]:
    """Held units, admissions, notices and forced stops over a window.

    Recomputed from `spot_lease` on every call rather than sampled into a
    history table. That costs a group-by per poll and buys a series that is
    identical from every replica and unbroken by a restart — a sampled series
    has a hole in it exactly where the incident was.
    """
    until = utcnow()
    since = until - timedelta(minutes=minutes)
    points = await container.lease_repo.timeline(
        since=since, until=until, buckets=buckets
    )
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "bucket_seconds": round(minutes * 60 / buckets, 3),
        "points": points,
    }


@router.get("/reclaim-orders")
async def reclaim_orders(
    container: ContainerDep,
    az: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    orders = await container.reclaim_repo.recent(limit=limit, az=az)
    return {"orders": [_order_summary(o) for o in orders]}


@router.get("/reclaim-orders/{order_id}")
async def reclaim_order_detail(order_id: str, container: ContainerDep) -> dict[str, Any]:
    order = await container.reclaim_repo.get(order_id)
    if order is None:
        raise LeaseNotFound(f"no reclaim order {order_id}")

    # Settle the order's state before reporting it, so a completed drain does
    # not sit displayed as in-flight until some other caller happens to look.
    await container.reclaim_handler.complete_if_drained(order_id)
    refreshed = await container.reclaim_repo.get(order_id)
    assert refreshed is not None

    victims = await asyncio.gather(
        *(container.lease_repo.get(lid) for lid in refreshed.leases_selected)
    )
    entries = await container.audit_repo.for_order(order_id)
    return {
        **_order_summary(refreshed),
        "victims": [lease_to_response(v).model_dump(mode="json") for v in victims if v],
        "audit": [_audit_row(e, order_id=order_id) for e in entries],
    }


@router.get("/audit")
async def audit(
    container: ContainerDep,
    event: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    entries = await container.audit_repo.recent(limit=limit, event=event)
    verification = await container.audit_repo.verify()
    return {
        "entries": [_audit_row(e) for e in entries],
        "chain": {
            "entries": verification.entries,
            "valid": verification.valid,
            "broken_at_id": verification.broken_at,
            "detail": verification.detail,
        },
    }


@router.get("/events")
async def events(
    container: ContainerDep,
    since_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """The bus, unfiltered — every topic in LLD §8, not one tenant's slice.

    In-process and bounded, so a fresh replica starts with an empty buffer. That
    is a property of the ring buffer standing in for Kafka (LLD §16), not of the
    console: the durable record of what happened is the audit log.
    """
    matched = container.bus.recent(since_seq=since_seq, limit=limit)
    return {
        "events": [e.as_dict() for e in matched],
        "latest_seq": container.bus._seq,  # noqa: SLF001 - read-only cursor
        "buffered": container.bus.depth,
    }


@router.get("/tenants")
async def tenants(container: ContainerDep) -> dict[str, Any]:
    """Tenants and their live usage — the quota picture behind a 429."""
    rows = await container.reference_repo.list_tenants()
    usage = await asyncio.gather(
        *(container.lease_repo.tenant_usage(t.tenant_id) for t in rows)
    )
    return {
        "tenants": [
            {
                "tenant_id": t.tenant_id,
                "name": t.name,
                "account_class": t.account_class.value,
                "spot_entitled": t.spot_entitled,
                "quota_units": t.spot_quota_units,
                "concurrency_cap": t.concurrency_cap,
                "units_in_use": used,
                "live_leases": count,
                "headroom_units": max(0, t.spot_quota_units - used),
            }
            for t, (used, count) in zip(rows, usage)
        ]
    }


@router.get("/host-groups")
async def host_groups(container: ContainerDep, az: str | None = None) -> dict[str, Any]:
    groups = await container.reference_repo.list_host_groups(az=az, include_quarantined=True)
    return {
        "host_groups": [
            {
                "host_group": g.host_group,
                "az": g.az,
                "total_units": g.total_units,
                "quarantined": g.quarantined,
            }
            for g in groups
        ]
    }


@router.get("/invoice/{lease_id}")
async def invoice(lease_id: str, container: ContainerDep) -> dict[str, Any]:
    from .routes_ops import invoice as ops_invoice

    return await ops_invoice(lease_id, container)


# ======================================================================
# actions — session in front, signature behind
# ======================================================================
async def _signed(request: Request, method: str, path: str, body: bytes) -> None:
    """Sign this call server-side and verify it like any other internal caller.

    The point is that there is exactly one authorisation path into the reclaim
    handler. If the console called `reclaim_handler.handle()` directly it would
    be a second path, and second paths are where the check that everyone
    remembers on the first one gets forgotten.
    """
    settings = request.app.state.settings
    verifier: SignatureVerifier = request.app.state.verifier

    headers: dict[str, str] = {HEADER_CALLER: "console"}
    if settings.internal_hmac_key:
        headers.update(
            sign_request(
                key=settings.internal_hmac_key, method=method, path=path, body=body
            )
        )
    await verifier.verify(method=method, path=path, body=body, headers=headers)


@router.post("/actions/reclaim", response_model=ReclaimResponse)
async def fire_reclaim(
    body: ReclaimRequest, container: ContainerDep, request: Request
) -> ReclaimResponse:
    """Issue a reclaim order on the operator's authority.

    Same handler, same idempotency, same audit trail as an order arriving from
    the capacity side (edge 18) — only the `requested_by` differs, so the log
    can tell an operator-initiated reclaim from a forecast-initiated one when
    someone asks afterwards why those instances died.
    """
    if body.az not in container.settings.availability_zones:
        raise InvalidRequest(
            f"unknown availability zone {body.az!r}",
            details={"known_zones": list(container.settings.availability_zones)},
        )

    payload = body.model_dump_json(exclude_none=False).encode()
    await _signed(request, "POST", "/internal/spot/reclaim", payload)

    order = ReclaimOrder(
        order_id=body.order_id,
        az=body.az,
        units=body.units,
        host_group=body.host_group,
        deadline=utcnow() + timedelta(seconds=body.deadline_seconds),
        reason=body.reason,
        requested_by="console-operator",
    )
    with order_context(order.order_id):
        outcome = await container.reclaim_handler.handle(order, flavour=body.flavour)
    return ReclaimResponse(**outcome.as_dict())


@router.post("/actions/headroom")
async def set_headroom(
    body: HeadroomOverrideRequest, container: ContainerDep, request: Request
) -> dict[str, Any]:
    """Move the forecast's sellable number — the proactive reclaim trigger.

    HLD §12's reclaim risk is capacity that must come back *before* a
    guaranteed-class customer is waiting on it, so this is the button that
    demonstrates the intended path: drop headroom, watch the pool shrink on the
    next control cycle, watch the shortfall become an order.
    """
    _require_sim(container)
    override = getattr(container.externals.forecast, "set_override", None)
    if override is None:
        raise InvalidRequest("the live forecast feed cannot be overridden")

    await _signed(request, "POST", "/sim/headroom", body.model_dump_json().encode())
    override(body.az, body.units)
    snapshot = await container.pool_view.refresh(body.az)

    # `sellable` below `reserved` is a legal, transient state: it means more is
    # held than may now be sold. It has to be legal, because edge 20 shrinks the
    # advertised pool before edge 21 selects victims, and a floor at `reserved`
    # would make that shrink a no-op exactly when the pool is fully sold — which
    # is when a reclaim order arrives. Nothing can be sold into the gap
    # (`available_units` floors at zero); the gap is simply the capacity a
    # reclaim order has to go and get.
    shortfall = (
        max(0, snapshot.reserved_units - body.units) if body.units is not None else 0
    )
    return {
        "az": body.az,
        "override_units": body.units,
        "sellable_units": snapshot.sellable_units,
        "reserved_units": snapshot.reserved_units,
        "available_units": snapshot.available_units,
        "degraded": snapshot.degraded,
        "shortfall_units": shortfall,
        "note": (
            f"the feed now says {body.units} vCPU is sellable in {body.az} but leases hold "
            f"{snapshot.reserved_units}; nothing further can be sold there, and "
            f"{shortfall} vCPU has to be reclaimed to make the forecast true"
            if shortfall
            else "the feed's number covers everything currently held; no reclaim is needed"
        ),
    }


@router.post("/actions/control-cycle")
async def control_cycle(container: ContainerDep, request: Request) -> dict[str, Any]:
    """Run one control cycle now instead of waiting out the interval."""
    _require_sim(container)
    await _signed(request, "POST", "/sim/control-cycle", b"")
    snapshots = await container.pool_view.refresh_all()
    released = await container.pool_view.expire_cooldowns()
    return {
        "pools": [
            {
                "az": s.az,
                "sellable_units": s.sellable_units,
                "reserved_units": s.reserved_units,
                "cooldown_units": s.cooldown_units,
                "available_units": s.available_units,
                "degraded": s.degraded,
                "cycle_seq": s.cycle_seq,
            }
            for s in snapshots
        ],
        "cooldown_released": released,
    }


@router.post("/actions/guest-behaviour/{lease_id}")
async def guest_behaviour(
    lease_id: str, container: ContainerDep, request: Request, behaviour: str
) -> dict[str, Any]:
    """Pin how a simulated guest answers its next notice.

    The three behaviours are the three rows of LLD §6.5: honour the notice,
    ignore it and be force-stopped at `force_stop_at`, or take the host with it
    and be escalated to a hypervisor destroy.
    """
    _require_sim(container)
    pin = getattr(container.externals.hypervisor, "pin_behaviour", None)
    if pin is None:
        raise InvalidRequest("guest behaviour can only be pinned in the simulator")
    await _signed(request, "POST", f"/sim/guest-behaviour/{lease_id}", b"")
    pin(lease_id, behaviour)
    return {"lease_id": lease_id, "behaviour": behaviour}


@router.post("/actions/clean-exit/{lease_id}")
async def clean_exit(
    lease_id: str, container: ContainerDep, request: Request
) -> dict[str, Any]:
    """Report a clean guest exit — edge 13, as the host agent would."""
    await _signed(request, "POST", f"/internal/spot/leases/{lease_id}/exited", b"")
    accepted = await container.lease_manager.report_clean_exit(lease_id)
    lease = await container.lease_repo.get(lease_id)
    if lease is None:
        raise LeaseNotFound(f"no lease {lease_id}")
    return {
        "lease_id": lease_id,
        "accepted": accepted,
        "state": lease.state.value,
        "detail": (
            "clean exit recorded; teardown and capacity return follow"
            if accepted
            else "lease had already left the grace window (force-stopped or closed)"
        ),
    }


@router.post("/actions/quarantine/{host_group}")
async def quarantine(
    host_group: str, container: ContainerDep, request: Request, reason: str = "manual"
) -> dict[str, Any]:
    await _signed(request, "POST", f"/internal/hosts/{host_group}/quarantine", b"")
    await container.reference_repo.quarantine_host_group(host_group, reason)
    _invalidate_forecast(container)
    return {"host_group": host_group, "quarantined": True, "reason": reason}


@router.delete("/actions/quarantine/{host_group}")
async def release_quarantine(
    host_group: str, container: ContainerDep, request: Request
) -> dict[str, Any]:
    await _signed(request, "DELETE", f"/internal/hosts/{host_group}/quarantine", b"")
    await container.reference_repo.release_quarantine(host_group)
    _invalidate_forecast(container)
    return {"host_group": host_group, "quarantined": False}


@router.post("/actions/burst")
async def burst(
    container: ContainerDep,
    request: Request,
    tenant_id: str,
    flavour: str = "s1.medium",
    az: str = "az-1",
    count: int = 1,
    launches: Annotated[int, Query(ge=2, le=32)] = 6,
) -> dict[str, Any]:
    """Race N launches at the pool at once — LLD §6.1's guarantee, on demand.

    "Two launches read the same stale available_units → atomic reserve; the
    loser gets 409, never a partial allocation" (LLD §10.4). The interesting
    output is the mix: some 201s, some 409s, and a reserved total that never
    exceeds what was sellable.
    """
    _require_sim(container)
    await _signed(request, "POST", "/sim/burst", b"")

    async def one(i: int) -> dict[str, Any]:
        try:
            result = await container.market.launch(
                tenant_id=tenant_id,
                flavour=flavour,
                count=count,
                az=az,
                idempotency_key=f"burst-{uuid.uuid4().hex}",
                request_fingerprint=fingerprint(
                    {"flavour": flavour, "count": count, "az": az}
                ),
                purchase_option=None,
            )
        except Exception as exc:  # noqa: BLE001 - a 409 is the expected outcome
            return {
                "index": i,
                "admitted": False,
                "code": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
                "retry_after": getattr(exc, "retry_after", None),
            }
        container.spawn_fulfilment(result.lease.lease_id)
        return {
            "index": i,
            "admitted": True,
            "lease_id": result.lease.lease_id,
            "units": result.lease.units,
        }

    results = await asyncio.gather(*(one(i) for i in range(launches)))
    admitted = [r for r in results if r["admitted"]]
    snapshot = await container.pool_repo.get(az)
    return {
        "requested": launches,
        "admitted": len(admitted),
        "rejected": launches - len(admitted),
        "units_admitted": sum(r.get("units", 0) for r in admitted),
        "results": results,
        "pool_after": {
            "sellable_units": snapshot.sellable_units,
            "reserved_units": snapshot.reserved_units,
            "available_units": snapshot.available_units,
        }
        if snapshot
        else None,
        "guarantee": "reserved_units + cooldown_units <= sellable_units held "
        "throughout; every rejection is a 409, never a partial allocation",
    }


def _require_sim(container: Any) -> None:
    if not container.settings.enable_sim_endpoints:
        raise InvalidRequest(
            "this action drives the simulator and is disabled on this deployment "
            "(SPOT_ENABLE_SIM=false)"
        )


def _invalidate_forecast(container: Any) -> None:
    invalidate = getattr(container.externals.forecast, "invalidate_capacity", None)
    if invalidate is not None:
        invalidate()

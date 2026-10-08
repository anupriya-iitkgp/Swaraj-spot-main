"""The control interface — edge 18, and the guest's clean-exit callback (edge 13).

Every route here is signed (LLD §12.1). `POST /internal/spot/reclaim` can
terminate every spot lease in an AZ; unauthenticated, it is the single most
dangerous endpoint in the system.

HLD §1 is the reason this is an inbound interface rather than a decision:

    "This subsystem does not decide how much capacity exists, nor when capacity
    must be taken back. Both arrive from outside as inputs."

The `/sim` routes are mounted only when `SPOT_ENABLE_SIM=true`, which config
validation forces to false in production — the feature flag half of the §12.1
fix. They are still signed, because a lab that anyone can drive is still a lab
anyone can break.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..domain.errors import InvalidRequest, LeaseNotFound
from ..domain.models import ReclaimOrder, utcnow
from ..logging import get_logger, order_context
from .auth import SignatureVerifier
from .deps import ContainerDep
from .schemas import (
    CleanExitRequest,
    HeadroomOverrideRequest,
    ReclaimRequest,
    ReclaimResponse,
)

log = get_logger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


async def require_signature(request: Request) -> Any:
    """Verify the HMAC signature over method, path, timestamp, nonce and body."""
    container = request.app.state.container
    verifier: SignatureVerifier = request.app.state.verifier
    body = await request.body()
    return await verifier.verify(
        method=request.method,
        path=request.url.path,
        body=body,
        headers=dict(request.headers),
    )


Signed = Depends(require_signature)


# ======================================================================
# POST /internal/spot/reclaim — edge 18
# ======================================================================
@router.post("/spot/reclaim", response_model=ReclaimResponse, dependencies=[Signed])
async def reclaim(body: ReclaimRequest, container: ContainerDep) -> ReclaimResponse:
    """Accept a reclaim order from the capacity side.

    The response is returned as soon as the notices are issued, not when the
    capacity is back: the capacity side needs to know its order was accepted and
    how many units were found, and the actual return takes up to the grace
    window. `GET /internal/spot/reclaim/{order_id}` reports completion.
    """
    if body.az not in container.settings.availability_zones:
        raise InvalidRequest(
            f"unknown availability zone {body.az!r}",
            details={"known_zones": list(container.settings.availability_zones)},
        )

    order = ReclaimOrder(
        order_id=body.order_id,
        az=body.az,
        units=body.units,
        host_group=body.host_group,
        deadline=utcnow() + timedelta(seconds=body.deadline_seconds),
        reason=body.reason,
        requested_by="capacity-side",
    )
    with order_context(order.order_id):
        outcome = await container.reclaim_handler.handle(order, flavour=body.flavour)
    return ReclaimResponse(**outcome.as_dict())


@router.get("/spot/reclaim/{order_id}", dependencies=[Signed])
async def reclaim_status(order_id: str, container: ContainerDep) -> dict[str, Any]:
    order = await container.reclaim_repo.get(order_id)
    if order is None:
        raise LeaseNotFound(f"no reclaim order {order_id}")
    await container.reclaim_handler.complete_if_drained(order_id)
    refreshed = await container.reclaim_repo.get(order_id)
    assert refreshed is not None

    leases = [
        await container.lease_repo.get(lease_id)
        for lease_id in refreshed.leases_selected
    ]
    return {
        "order_id": refreshed.order_id,
        "state": refreshed.state.value,
        "az": refreshed.az,
        "units_requested": refreshed.units,
        "units_selected": refreshed.units_selected,
        "received_at": refreshed.received_at.isoformat(),
        "completed_at": (
            refreshed.completed_at.isoformat() if refreshed.completed_at else None
        ),
        "detail": refreshed.detail,
        "leases": [
            {
                "lease_id": lease.lease_id,
                "state": lease.state.value,
                "units": lease.units,
                "host_group": lease.host_group,
                "notice_at": lease.notice_at.isoformat() if lease.notice_at else None,
                "closed_at": lease.closed_at.isoformat() if lease.closed_at else None,
                "forced_stop": lease.forced_stop,
                "reclaim_seconds": (
                    round(lease.reclaim_window_seconds, 2)
                    if lease.reclaim_window_seconds is not None
                    else None
                ),
            }
            for lease in leases
            if lease is not None
        ],
    }


# ======================================================================
# POST /internal/spot/leases/{id}/exited — edge 13
# ======================================================================
@router.post("/spot/leases/{lease_id}/exited", dependencies=[Signed])
async def clean_exit(
    lease_id: str, body: CleanExitRequest, container: ContainerDep
) -> dict[str, Any]:
    """The host agent reporting that the guest honoured the notice.

    This is what replaces `wait_for_clean_exit`. Inverting the wait is what
    removes the in-process grace timer entirely, and with it the restart
    stranding of LLD §12.3 — there is no coroutine holding a countdown, so there
    is nothing for a restart to lose.

    Returning `accepted: false` is not an error. The reaper may have force-stopped
    the lease microseconds earlier; both callers racing to end the same lease is
    the expected case, and the state machine settles it (LLD §10.4).
    """
    accepted = await container.lease_manager.report_clean_exit(lease_id)
    lease = await container.lease_repo.get(lease_id)
    if lease is None:
        raise LeaseNotFound(f"no lease {lease_id}")
    return {
        "lease_id": lease_id,
        "accepted": accepted,
        "state": lease.state.value,
        "reported_by": body.reported_by,
        "detail": (
            "clean exit recorded; teardown and capacity return follow"
            if accepted
            else "lease had already left the grace window (force-stopped or closed)"
        ),
    }


# ======================================================================
# host quarantine — the LLD §11 escalation, exposed for operators
# ======================================================================
@router.post("/hosts/{host_group}/quarantine", dependencies=[Signed])
async def quarantine(
    host_group: str, container: ContainerDep, reason: str = "manual"
) -> dict[str, Any]:
    await container.reference_repo.quarantine_host_group(host_group, reason)
    invalidate = getattr(container.externals.forecast, "invalidate_capacity", None)
    if invalidate is not None:
        invalidate()
    return {"host_group": host_group, "quarantined": True, "reason": reason}


@router.delete("/hosts/{host_group}/quarantine", dependencies=[Signed])
async def release_quarantine(
    host_group: str, container: ContainerDep
) -> dict[str, Any]:
    await container.reference_repo.release_quarantine(host_group)
    invalidate = getattr(container.externals.forecast, "invalidate_capacity", None)
    if invalidate is not None:
        invalidate()
    return {"host_group": host_group, "quarantined": False}


# ======================================================================
# /sim — lab only, gated by SPOT_ENABLE_SIM (LLD §12.1)
# ======================================================================
sim_router = APIRouter(prefix="/sim", tags=["simulation"])


@sim_router.post("/headroom", dependencies=[Signed])
async def set_headroom(
    body: HeadroomOverrideRequest, container: ContainerDep
) -> dict[str, Any]:
    """Pin the sellable number, to drive the proactive reclaim path.

    This models the trigger the design actually intends. HLD §12's reclaim risk
    is about capacity that must be taken back *before* a guaranteed-class
    customer is waiting on it — the grace window is spent ahead of the demand,
    not after it arrives. Lowering headroom here shrinks the pool on the next
    control cycle, exactly as a real forecast would.
    """
    override = getattr(container.externals.forecast, "set_override", None)
    if override is None:
        raise InvalidRequest("the live forecast feed cannot be overridden")
    override(body.az, body.units)
    snapshot = await container.pool_view.refresh(body.az)
    return {
        "az": body.az,
        "override_units": body.units,
        "sellable_units": snapshot.sellable_units,
        "reserved_units": snapshot.reserved_units,
        "available_units": snapshot.available_units,
        "degraded": snapshot.degraded,
    }


@sim_router.post("/guest-behaviour/{lease_id}", dependencies=[Signed])
async def pin_guest_behaviour(
    lease_id: str, behaviour: str, container: ContainerDep
) -> dict[str, Any]:
    """Force a simulated guest's response to a notice, for demos and tests."""
    pin = getattr(container.externals.hypervisor, "pin_behaviour", None)
    if pin is None:
        raise InvalidRequest("guest behaviour can only be pinned in the simulator")
    pin(lease_id, behaviour)
    return {"lease_id": lease_id, "behaviour": behaviour}


@sim_router.post("/control-cycle", dependencies=[Signed])
async def run_control_cycle(container: ContainerDep) -> dict[str, Any]:
    """Run one control cycle immediately instead of waiting for the timer."""
    snapshots = await container.pool_view.refresh_all()
    released = await container.pool_view.expire_cooldowns()
    reconciliation = await container.pool_repo.reconcile()
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
        "reconciliation": [
            {"az": r.az, "counter": r.counter, "actual": r.actual, "drift": r.drift}
            for r in reconciliation
        ],
    }

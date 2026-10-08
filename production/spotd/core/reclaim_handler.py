"""Reclaim Order Handler — edges 18, 20 and 21.

HLD §6:

    Owns: Accepting reclaim orders and shrinking advertised inventory immediately.
    Must not do: Decide how much to reclaim.
    Key operations: POST /internal/spot/reclaim

The "must not" restates HLD §1's framing of the whole subsystem: "this subsystem
does not decide how much capacity exists, nor when capacity must be taken back.
Both arrive from outside as inputs." The order says N units, a host group and a
deadline; everything here is faithful execution.

The one ordering constraint that matters is edge 20 before edge 21:

    shrink the advertised pool  ->  then select victims

If victims were chosen first, a launch arriving in between would be admitted
against capacity that is already being taken back — and would then either be
preempted seconds after being sold, or push the reclaim into needing a second
wave. Shrinking first closes that window. The two steps share a transaction so
the ordering cannot be broken by a crash either.

Order replay is a no-op by construction (`ReclaimRepository.claim`). This
matters more than ordinary idempotency: a duplicated launch wastes capacity,
while a duplicated reclaim kills twice as many customer instances as the
capacity side asked for, with no undo.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..db.repositories import AuditEvent, Topics
from ..domain.models import ReclaimOrder, ReclaimOrderState, utcnow
from ..logging import edge, get_logger, order_context
from ..metrics import M

log = get_logger(__name__)

__all__ = ["ReclaimOrderHandler", "ReclaimOutcome"]


@dataclass(frozen=True, slots=True)
class ReclaimOutcome:
    order: ReclaimOrder
    replayed: bool
    units_shrunk: int
    units_selected: int
    leases_noticed: list[str]
    partial: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order.order_id,
            "az": self.order.az,
            "host_group": self.order.host_group,
            "units_requested": self.order.units,
            "units_shrunk_from_pool": self.units_shrunk,
            "units_selected": self.units_selected,
            "leases_noticed": self.leases_noticed,
            "state": self.order.state.value,
            "partial": self.partial,
            "replayed": self.replayed,
            "detail": self.detail,
            "deadline": self.order.deadline.isoformat(),
        }


class ReclaimOrderHandler:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Any,
        reclaim_repo: Any,
        lease_repo: Any,
        audit_repo: Any,
        outbox_repo: Any,
        pool_view: Any,
        selector: Any,
        lease_manager: Any,
    ) -> None:
        self._settings = settings
        self._db = db
        self._orders = reclaim_repo
        self._leases = lease_repo
        self._audit = audit_repo
        self._outbox = outbox_repo
        self._pool = pool_view
        self._selector = selector
        self._leases_manager = lease_manager

    async def handle(
        self, order: ReclaimOrder, *, flavour: str | None = None
    ) -> ReclaimOutcome:
        """Execute one reclaim order."""
        with order_context(order.order_id):
            claimed, stored = await self._orders.claim(order)
            if not claimed:
                # Idempotent per order_id (LLD §16): report the original outcome
                # and preempt nothing further.
                return ReclaimOutcome(
                    order=stored,
                    replayed=True,
                    units_shrunk=0,
                    units_selected=stored.units_selected,
                    leases_noticed=list(stored.leases_selected),
                    partial=stored.state is ReclaimOrderState.PARTIAL,
                    detail="replayed: this order_id was already executed",
                )

            edge(
                log,
                18,
                f"reclaim order for {order.units}u in {order.az}"
                + (f" on {order.host_group}" if order.host_group else "")
                + f", deadline in {order.seconds_to_deadline:.0f}s",
                order_id=order.order_id,
                az=order.az,
                units=order.units,
                host_group=order.host_group,
                requested_by=order.requested_by,
            )
            await self._audit.append(
                AuditEvent.RECLAIM_RECEIVED,
                order_id=order.order_id,
                detail={
                    "az": order.az,
                    "units": order.units,
                    "host_group": order.host_group,
                    "deadline": order.deadline.isoformat(),
                    "reason": order.reason,
                    "requested_by": order.requested_by,
                },
            )

            # -- edge 20: shrink FIRST ---------------------------------
            async with self._db.transaction() as conn:
                await self._orders.set_state(
                    order.order_id, ReclaimOrderState.SHRINKING, conn=conn
                )
                units_shrunk = await self._pool.shrink(
                    order.az, order.units, conn=conn
                )
                await self._audit.append(
                    AuditEvent.RECLAIM_SHRUNK,
                    order_id=order.order_id,
                    detail={
                        "az": order.az,
                        "units_requested": order.units,
                        "units_shrunk": units_shrunk,
                        "ordering": "edge 20 strictly precedes edge 21 so nothing "
                        "new is sold into capacity already being taken back",
                    },
                    conn=conn,
                )

            # -- edge 21: only now, select victims ---------------------
            await self._orders.set_state(order.order_id, ReclaimOrderState.SELECTING)
            selection = await self._selector.select(order, flavour=flavour)

            await self._audit.append(
                AuditEvent.RECLAIM_VICTIMS_SELECTED,
                order_id=order.order_id,
                detail={
                    "victims": selection.lease_ids,
                    "units_selected": selection.units_selected,
                    "units_requested": order.units,
                    "host_groups": selection.host_groups,
                    "blast_radius_exceeded": selection.blast_radius_exceeded,
                    "precedence": "contiguity > flavour > fairness > blast_radius",
                },
            )

            # -- edge 22: hand the victim set to the lease manager -----
            noticed: list[str] = []
            for candidate in selection.victims:
                lease = await self._leases.get(candidate.lease_id)
                if lease is None:
                    continue
                if await self._leases_manager.preempt(lease, order):
                    noticed.append(lease.lease_id)

            partial = not selection.satisfied
            state = (
                ReclaimOrderState.PARTIAL if partial else ReclaimOrderState.NOTICED
            )
            detail = selection.shortfall_reason or (
                f"{len(noticed)} lease(s) noticed; capacity returns as each "
                f"teardown is confirmed"
            )
            final = await self._orders.set_state(
                order.order_id,
                state,
                units_selected=selection.units_selected,
                leases_selected=noticed,
                detail=detail,
                completed=partial,
                conn=None,
            )

            await self._outbox.enqueue(
                topic=Topics.RECLAIM,
                aggregate_type="reclaim_order",
                aggregate_id=order.order_id,
                payload={
                    "order_id": order.order_id,
                    "az": order.az,
                    "units_requested": order.units,
                    "units_selected": selection.units_selected,
                    "leases": noticed,
                    "partial": partial,
                    "at": utcnow().isoformat(),
                },
            )

            if partial:
                # Not an error here — this subsystem does not decide how much
                # capacity exists. The capacity side asked for more than any
                # running spot lease is holding, and needs to know that.
                log.error(
                    "reclaim.partial",
                    order_id=order.order_id,
                    az=order.az,
                    units_requested=order.units,
                    units_selected=selection.units_selected,
                    reason=selection.shortfall_reason,
                    note="the shortfall must be found outside the spot pool",
                )
                await self._audit.append(
                    AuditEvent.RECLAIM_PARTIAL,
                    order_id=order.order_id,
                    detail={
                        "units_requested": order.units,
                        "units_selected": selection.units_selected,
                        "reason": selection.shortfall_reason,
                    },
                )

            return ReclaimOutcome(
                order=final or stored,
                replayed=False,
                units_shrunk=units_shrunk,
                units_selected=selection.units_selected,
                leases_noticed=noticed,
                partial=partial,
                detail=detail,
            )

    async def in_flight_orders(self) -> list[ReclaimOrder]:
        """Orders that have issued notices but whose capacity has not all returned."""
        return await self._orders.in_flight()

    async def complete_if_drained(self, order_id: str) -> bool:
        """Mark an order COMPLETED once every selected lease has closed.

        The order is not finished when the notices go out — it is finished when
        the capacity is actually back, which is what the capacity side is
        waiting on.
        """
        order = await self._orders.get(order_id)
        if order is None or order.state not in (
            ReclaimOrderState.NOTICED,
            ReclaimOrderState.SELECTING,
        ):
            return False

        outstanding = await self._db.fetchval(
            """
            SELECT COUNT(*)::int FROM spot_lease
             WHERE reclaim_order_id = $1 AND state <> 'CLOSED'
            """,
            order_id,
        )
        if outstanding:
            return False

        await self._orders.set_state(
            order_id,
            ReclaimOrderState.COMPLETED,
            detail="all selected leases closed; capacity returned",
            completed=True,
        )
        await self._audit.append(
            AuditEvent.RECLAIM_COMPLETED,
            order_id=order_id,
            detail={"leases": list(order.leases_selected)},
        )
        log.info("reclaim.completed", order_id=order_id, leases=len(order.leases_selected))
        return True

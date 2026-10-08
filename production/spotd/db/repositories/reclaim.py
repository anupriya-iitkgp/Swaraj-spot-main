"""Reclaim order persistence — edge 18.

LLD §16 requires reclaim orders to be "idempotent per order_id; a replayed order
must not double-preempt". That is enforced here by `claim()`: the insert is
`ON CONFLICT DO NOTHING`, so a replay finds the existing row and the handler
returns the original outcome instead of selecting a second set of victims.

This matters more than the usual idempotency argument. A duplicated launch
wastes capacity; a duplicated reclaim kills twice as many customer instances as
the capacity side asked for, and there is no undo.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

import asyncpg

from ...domain.models import ReclaimOrder, ReclaimOrderState
from ...logging import get_logger

log = get_logger(__name__)

__all__ = ["ReclaimRepository"]

_COLUMNS = """
    order_id, az, units, host_group, deadline, reason, requested_by, state,
    received_at, units_selected, leases_selected, completed_at, detail
"""


def _order(row: asyncpg.Record) -> ReclaimOrder:
    return ReclaimOrder(
        order_id=row["order_id"],
        az=row["az"],
        units=row["units"],
        host_group=row["host_group"],
        deadline=row["deadline"],
        reason=row["reason"],
        requested_by=row["requested_by"],
        state=ReclaimOrderState(row["state"]),
        received_at=row["received_at"],
        units_selected=row["units_selected"],
        leases_selected=tuple(row["leases_selected"] or ()),
        completed_at=row["completed_at"],
        detail=row["detail"],
    )


class ReclaimRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def claim(
        self, order: ReclaimOrder, *, conn: asyncpg.Connection | None = None
    ) -> tuple[bool, ReclaimOrder]:
        """Record a new order, or return the existing one.

        `(True, order)` means this call owns the order and should execute it.
        `(False, existing)` means it is a replay — the caller reports the
        original outcome and preempts nothing.
        """
        executor = conn or self._db
        row = await executor.fetchrow(
            f"""
            INSERT INTO reclaim_order
                (order_id, az, units, host_group, deadline, reason, requested_by,
                 state, received_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (order_id) DO NOTHING
            RETURNING {_COLUMNS}
            """,
            order.order_id,
            order.az,
            order.units,
            order.host_group,
            order.deadline,
            order.reason,
            order.requested_by,
            ReclaimOrderState.RECEIVED.value,
            order.received_at,
        )
        if row is not None:
            return True, _order(row)

        existing = await self.get(order.order_id, conn=conn)
        assert existing is not None
        log.warning(
            "reclaim.replayed_order",
            order_id=order.order_id,
            original_state=existing.state.value,
            note="idempotent per order_id — no victims re-selected (LLD §16)",
        )
        return False, existing

    async def set_state(
        self,
        order_id: str,
        state: ReclaimOrderState,
        *,
        units_selected: int | None = None,
        leases_selected: Sequence[str] | None = None,
        detail: str | None = None,
        completed: bool = False,
        conn: asyncpg.Connection | None = None,
    ) -> ReclaimOrder | None:
        args: list[Any] = [order_id, state.value]
        assignments = ["state = $2"]
        if units_selected is not None:
            args.append(units_selected)
            assignments.append(f"units_selected = ${len(args)}")
        if leases_selected is not None:
            args.append(list(leases_selected))
            assignments.append(f"leases_selected = ${len(args)}")
        if detail is not None:
            args.append(detail)
            assignments.append(f"detail = ${len(args)}")
        if completed:
            assignments.append("completed_at = now()")

        row = await (conn or self._db).fetchrow(
            f"""
            UPDATE reclaim_order SET {', '.join(assignments)}
             WHERE order_id = $1
            RETURNING {_COLUMNS}
            """,
            *args,
        )
        return _order(row) if row else None

    async def get(
        self, order_id: str, *, conn: asyncpg.Connection | None = None
    ) -> ReclaimOrder | None:
        row = await (conn or self._db).fetchrow(
            f"SELECT {_COLUMNS} FROM reclaim_order WHERE order_id = $1", order_id
        )
        return _order(row) if row else None

    async def recent(
        self, *, limit: int = 50, az: str | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> list[ReclaimOrder]:
        if az:
            rows = await (conn or self._db).fetch(
                f"""
                SELECT {_COLUMNS} FROM reclaim_order WHERE az = $1
                 ORDER BY received_at DESC LIMIT $2
                """,
                az,
                limit,
            )
        else:
            rows = await (conn or self._db).fetch(
                f"SELECT {_COLUMNS} FROM reclaim_order ORDER BY received_at DESC LIMIT $1",
                limit,
            )
        return [_order(r) for r in rows]

    async def in_flight(
        self, *, conn: asyncpg.Connection | None = None
    ) -> list[ReclaimOrder]:
        rows = await (conn or self._db).fetch(
            f"""
            SELECT {_COLUMNS} FROM reclaim_order
             WHERE state IN ('RECEIVED','SHRINKING','SELECTING','NOTICED')
             ORDER BY received_at
            """
        )
        return [_order(r) for r in rows]

"""Preemption audit log — edges 27 and 28.

HLD §11 sets audit completeness at 100% of notices, expiries and credits, and
gives the reason in one sentence: "Preemption disputes are settled from this log
or not at all."

That sentence is why this table is append-only in the database (triggers, not
convention) and hash-chained. A log the application can rewrite settles nothing;
a log an operator can quietly delete a row from settles nothing either. Each
entry's hash covers the previous entry's hash, so any removal or edit breaks the
chain from that point forward and `verify()` reports exactly where.

The chaining and immutability live in the schema (migration 0001). This module
only appends and reads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

import asyncpg

from ...logging import get_logger

log = get_logger(__name__)

__all__ = ["AuditRepository", "AuditEvent", "ChainVerification"]


class AuditEvent:
    """Canonical event names. Fixed strings, because dashboards query them."""

    LEASE_CREATED = "lease.created"
    LEASE_TRANSITION = "lease.transition"
    LEASE_REJECTED = "lease.rejected"
    RESERVE_GRANTED = "admission.reserve_granted"
    RESERVE_REFUSED = "admission.reserve_refused"
    NOTICE_ISSUED = "preemption.notice_issued"
    NOTICE_DELIVERED = "preemption.notice_delivered"
    NOTICE_ALL_CHANNELS_FAILED = "preemption.notice_all_channels_failed"
    GRACE_EXPIRED = "preemption.grace_expired"
    FORCED_STOP = "preemption.forced_stop"
    CLEAN_EXIT = "preemption.clean_exit"
    CANCELLED_IN_FLIGHT = "preemption.cancelled_in_flight"
    TEARDOWN_CONFIRMED = "capacity.teardown_confirmed"
    TEARDOWN_STALLED = "capacity.teardown_stalled"
    CAPACITY_RETURNED = "capacity.returned"
    RECLAIM_RECEIVED = "reclaim.received"
    RECLAIM_SHRUNK = "reclaim.pool_shrunk"
    RECLAIM_VICTIMS_SELECTED = "reclaim.victims_selected"
    RECLAIM_COMPLETED = "reclaim.completed"
    RECLAIM_PARTIAL = "reclaim.partial"
    CREDIT_RAISED = "billing.credit_raised"
    HOST_QUARANTINED = "host.quarantined"
    POOL_DEGRADED = "pool.degraded"


class ChainVerification:
    __slots__ = ("entries", "valid", "broken_at", "detail")

    def __init__(
        self, entries: int, valid: bool, broken_at: int | None, detail: str
    ) -> None:
        self.entries = entries
        self.valid = valid
        self.broken_at = broken_at
        self.detail = detail

    def __bool__(self) -> bool:
        return self.valid

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ChainVerification {self.entries} entries valid={self.valid}>"


class AuditRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def append(
        self,
        event: str,
        *,
        lease_id: str | None = None,
        order_id: str | None = None,
        tenant_id: str | None = None,
        actor: str = "spotd",
        detail: dict[str, Any] | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> int:
        """Append one entry. The hash chain is computed by the insert trigger.

        Pass `conn` from the surrounding transaction so the evidence lands with
        the state change it describes — an audit entry for a transition that
        rolled back would be worse than no entry at all.
        """
        return await (conn or self._db).fetchval(
            """
            INSERT INTO preemption_audit
                (event, lease_id, order_id, tenant_id, actor, detail)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            event,
            lease_id,
            order_id,
            tenant_id,
            actor,
            detail or {},
        )

    async def for_lease(
        self, lease_id: str, *, conn: asyncpg.Connection | None = None
    ) -> list[asyncpg.Record]:
        """The evidence trail for one lease — what a dispute is answered with."""
        return await (conn or self._db).fetch(
            """
            SELECT id, ts, event, order_id, actor, detail, entry_hash
              FROM preemption_audit
             WHERE lease_id = $1
             ORDER BY id
            """,
            lease_id,
        )

    async def for_order(
        self, order_id: str, *, conn: asyncpg.Connection | None = None
    ) -> list[asyncpg.Record]:
        return await (conn or self._db).fetch(
            """
            SELECT id, ts, event, lease_id, actor, detail
              FROM preemption_audit
             WHERE order_id = $1
             ORDER BY id
            """,
            order_id,
        )

    async def recent(
        self,
        *,
        limit: int = 200,
        event: str | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> list[asyncpg.Record]:
        if event:
            return await (conn or self._db).fetch(
                """
                SELECT id, ts, event, lease_id, order_id, tenant_id, actor, detail
                  FROM preemption_audit WHERE event = $1
                 ORDER BY id DESC LIMIT $2
                """,
                event,
                limit,
            )
        return await (conn or self._db).fetch(
            """
            SELECT id, ts, event, lease_id, order_id, tenant_id, actor, detail
              FROM preemption_audit ORDER BY id DESC LIMIT $1
            """,
            limit,
        )

    async def count_events(
        self, since: datetime, *, conn: asyncpg.Connection | None = None
    ) -> dict[str, int]:
        rows = await (conn or self._db).fetch(
            """
            SELECT event, COUNT(*)::int AS n FROM preemption_audit
             WHERE ts >= $1 GROUP BY event ORDER BY n DESC
            """,
            since,
        )
        return {r["event"]: r["n"] for r in rows}

    # ------------------------------------------------------------------
    async def verify(
        self, *, limit: int | None = None, conn: asyncpg.Connection | None = None
    ) -> ChainVerification:
        """Recompute the chain in the database and report the first break.

        The recomputation uses the same expression as the insert trigger, so a
        mismatch means the stored row differs from what was originally hashed —
        a row was edited, or a predecessor was removed and the successors no
        longer chain.

        Run from `spotd audit-verify`, and worth running on a schedule: the
        value of tamper-evidence is only realised if someone looks.
        """
        rows = await (conn or self._db).fetch(
            """
            SELECT id, prev_hash, entry_hash,
                   encode(sha256(convert_to(
                       prev_hash || '|' ||
                       to_char(ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '|' ||
                       event || '|' ||
                       COALESCE(lease_id, '') || '|' ||
                       COALESCE(order_id, '') || '|' ||
                       COALESCE(tenant_id, '') || '|' ||
                       actor || '|' ||
                       COALESCE(detail::text, '{}'), 'UTF8')), 'hex') AS recomputed,
                   LAG(entry_hash) OVER (ORDER BY id) AS predecessor
              FROM preemption_audit
             ORDER BY id
            """
        )
        if not rows:
            return ChainVerification(0, True, None, "log is empty")

        for row in rows:
            if row["entry_hash"] != row["recomputed"]:
                return ChainVerification(
                    len(rows),
                    False,
                    row["id"],
                    f"entry {row['id']} does not match its own hash — the row was "
                    f"modified after insertion",
                )
            expected_prev = row["predecessor"] or "0" * 64
            if row["prev_hash"] != expected_prev:
                return ChainVerification(
                    len(rows),
                    False,
                    row["id"],
                    f"entry {row['id']} chains from {row['prev_hash'][:12]}… but its "
                    f"predecessor hashes to {expected_prev[:12]}… — an entry between "
                    f"them was removed",
                )
        return ChainVerification(
            len(rows), True, None, f"{len(rows)} entries chain cleanly"
        )

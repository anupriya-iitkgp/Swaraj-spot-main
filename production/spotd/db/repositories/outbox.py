"""Transactional outbox — the fix for LLD §12.8.

The gap, stated there:

    "Ledger and pool are updated in separate steps with no outbox. A crash
    between them leaves the two views disagreeing until reconciliation. Fix:
    Transactional outbox — write lease state + ledger intent in one
    transaction, publish from the outbox."

So the write path never calls the ledger or the bus directly. It writes the
lease row and an outbox row in the *same* transaction; either both land or
neither does. A separate relay then publishes, retrying until the downstream
accepts. That converts an atomicity problem (two systems, one crash) into a
delivery problem (one system, at-least-once), which every downstream here is
already required to tolerate: LLD §9 specifies the ledger as "idempotent per
(host_group, lease, operation)" and billing as "accepts duplicates safely".

The relay claims rows with `FOR UPDATE SKIP LOCKED`, so N relays share the
table without contending, and a row whose relay dies is simply re-claimed by
the next one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

import asyncpg

from ...logging import get_logger
from ...metrics import M

log = get_logger(__name__)

__all__ = ["OutboxRepository", "OutboxMessage", "Topics"]


class Topics:
    """Event topics. Partitioned by `lease_id` when this moves to Kafka (LLD §16)."""

    LEASE_STATE = "spot.lease.state"
    LEASE_NOTICE = "spot.lease.notice"
    LEDGER = "spot.capacity.ledger"
    BILLING_USAGE = "spot.billing.usage"
    BILLING_CREDIT = "spot.billing.credit"
    RECLAIM = "spot.reclaim.order"


class OutboxMessage:
    __slots__ = ("id", "topic", "aggregate_type", "aggregate_id", "payload", "attempts")

    def __init__(self, row: asyncpg.Record) -> None:
        self.id: int = row["id"]
        self.topic: str = row["topic"]
        self.aggregate_type: str = row["aggregate_type"]
        self.aggregate_id: str = row["aggregate_id"]
        self.payload: dict[str, Any] = row["payload"]
        self.attempts: int = row["attempts"]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Outbox {self.id} {self.topic} {self.aggregate_id}>"


class OutboxRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def enqueue(
        self,
        *,
        topic: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
        conn: asyncpg.Connection | None = None,
    ) -> int:
        """Record an intent to publish.

        Callers must pass `conn` from the transaction that also writes the state
        change. Enqueueing on a separate connection would reintroduce exactly
        the crash window this table exists to close, so the parameter is
        deliberately not optional in practice even though the signature allows
        it for tests.
        """
        return await (conn or self._db).fetchval(
            """
            INSERT INTO outbox (topic, aggregate_type, aggregate_id, payload)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            topic,
            aggregate_type,
            aggregate_id,
            payload,
        )

    async def claim(
        self, *, batch: int, conn: asyncpg.Connection | None = None
    ) -> list[OutboxMessage]:
        """Take a batch of unpublished rows for this relay.

        Ordered by id so per-aggregate ordering is preserved within a batch. The
        claim is the row lock itself, held for the duration of the caller's
        transaction — so this must be called inside one, and the caller must
        mark the rows before committing.
        """
        rows = await (conn or self._db).fetch(
            """
            SELECT id, topic, aggregate_type, aggregate_id, payload, attempts
              FROM outbox
             WHERE published_at IS NULL
               AND NOT dead
               AND available_at <= now()
             ORDER BY id
             FOR UPDATE SKIP LOCKED
             LIMIT $1
            """,
            batch,
        )
        return [OutboxMessage(r) for r in rows]

    async def mark_published(
        self, ids: Sequence[int], *, topic: str = "", conn: asyncpg.Connection | None = None
    ) -> None:
        if not ids:
            return
        await (conn or self._db).execute(
            """
            UPDATE outbox SET published_at = now(), attempts = attempts + 1
             WHERE id = ANY($1::bigint[])
            """,
            list(ids),
        )
        if topic:
            M.outbox_published_total.labels(topic=topic).inc(len(ids))

    async def mark_failed(
        self,
        message_id: int,
        *,
        error: str,
        max_attempts: int,
        backoff_seconds: float,
        conn: asyncpg.Connection | None = None,
    ) -> bool:
        """Reschedule with backoff, or park the row as dead.

        A dead row is never silently dropped: it stays in the table, it is
        counted by `spot_outbox_dead`, and the alert on that gauge is how an
        operator finds out that something downstream has been rejecting a
        message for long enough to matter. Returns True if the row went dead.
        """
        dead = await (conn or self._db).fetchval(
            """
            UPDATE outbox
               SET attempts     = attempts + 1,
                   last_error   = $2,
                   available_at = now() + make_interval(secs => $3::float8),
                   dead         = (attempts + 1) >= $4
             WHERE id = $1
            RETURNING dead
            """,
            message_id,
            error[:1000],
            float(backoff_seconds),
            max_attempts,
        )
        if dead:
            log.error(
                "outbox.dead_letter",
                outbox_id=message_id,
                error=error[:500],
                attempts=max_attempts,
                remediation="downstream has rejected this message repeatedly; "
                "inspect the row and requeue with `spotd outbox requeue`",
            )
        return bool(dead)

    async def requeue_dead(
        self, *, ids: Sequence[int] | None = None, conn: asyncpg.Connection | None = None
    ) -> int:
        """Operator action: put dead rows back in the queue after a fix."""
        if ids:
            status = await (conn or self._db).execute(
                """
                UPDATE outbox SET dead = false, attempts = 0, available_at = now()
                 WHERE dead AND id = ANY($1::bigint[])
                """,
                list(ids),
            )
        else:
            status = await (conn or self._db).execute(
                "UPDATE outbox SET dead = false, attempts = 0, available_at = now() "
                "WHERE dead"
            )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

    async def depth(self, *, conn: asyncpg.Connection | None = None) -> tuple[int, int]:
        """(pending, dead). Sustained pending growth means the relay is stuck."""
        row = await (conn or self._db).fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE published_at IS NULL AND NOT dead)::int
                       AS pending,
                   COUNT(*) FILTER (WHERE dead)::int AS dead
              FROM outbox
            """
        )
        pending, dead = (row["pending"], row["dead"]) if row else (0, 0)
        M.outbox_pending.set(pending)
        M.outbox_dead.set(dead)
        return pending, dead

    async def prune_published(
        self, *, older_than_seconds: float, limit: int = 20_000,
        conn: asyncpg.Connection | None = None,
    ) -> int:
        """Delete successfully published rows past their retention window.

        Published rows are kept for a while deliberately: when a downstream
        claims it never received something, the outbox is the evidence that it
        was sent and when.
        """
        status = await (conn or self._db).execute(
            """
            DELETE FROM outbox
             WHERE id IN (
                 SELECT id FROM outbox
                  WHERE published_at IS NOT NULL
                    AND published_at < now() - make_interval(secs => $1::float8)
                  LIMIT $2
             )
            """,
            float(older_than_seconds),
            limit,
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

"""Idempotency keys.

HLD §10 requires a repeat to return the original lease and the key to be
retained at least 24 h; HLD §11 restates the retention as a non-functional
target, because "a client retry after a network partition must not
double-allocate".

LLD §12.4 records why the reference implementation leaked: the in-memory map
"is only pruned when the same key is looked up again", so "keys that are never
retried are never evicted" — unbounded growth on the happy path. The fix it
prescribes is "the idempotency_key table with expires_at and a nightly delete",
which is what this is, with the sweep on a timer rather than nightly so memory
is bounded by the TTL rather than by the gap between sweeps.

The claim is `INSERT … ON CONFLICT DO NOTHING`, so the first caller wins on the
primary key and everyone else reads the winner's row. That is one round trip
and no lock, and it is correct across replicas because the uniqueness is the
database's, not a process's.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import asyncpg
import orjson

from ...domain.models import utcnow
from ...logging import get_logger
from ...metrics import M

log = get_logger(__name__)

__all__ = ["IdempotencyRepository", "IdempotencyRecord", "fingerprint"]


def fingerprint(payload: dict[str, Any]) -> str:
    """Stable hash of the semantically significant request fields.

    Only the fields that change what gets allocated are included, and they are
    sorted, so re-ordered JSON keys or an added client-side annotation do not
    make an honest retry look like a different request.
    """
    canonical = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(canonical).hexdigest()


class IdempotencyRecord:
    __slots__ = ("tenant_id", "key", "lease_id", "outcome", "response_status",
                 "response_body", "request_fingerprint", "created_at", "expires_at")

    def __init__(self, row: asyncpg.Record) -> None:
        self.tenant_id: str = row["tenant_id"]
        self.key: str = row["key"]
        self.lease_id: str | None = row["lease_id"]
        self.outcome: str = row["outcome"]
        self.response_status: int | None = row["response_status"]
        self.response_body: dict[str, Any] | None = row["response_body"]
        self.request_fingerprint: str = row["request_fingerprint"]
        self.created_at: datetime = row["created_at"]
        self.expires_at: datetime = row["expires_at"]

    @property
    def is_complete(self) -> bool:
        return self.outcome != "pending"


class IdempotencyRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def claim(
        self,
        tenant_id: str,
        key: str,
        request_fingerprint: str,
        ttl_seconds: float,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> tuple[bool, IdempotencyRecord]:
        """Reserve the key for this request.

        Returns `(True, record)` when this caller owns the key and should do the
        work, `(False, record)` when someone already claimed it and the caller
        should replay the stored outcome.
        """
        executor = conn or self._db
        expires_at = utcnow() + timedelta(seconds=ttl_seconds)
        row = await executor.fetchrow(
            """
            INSERT INTO idempotency_key
                (tenant_id, key, request_fingerprint, outcome, expires_at)
            VALUES ($1, $2, $3, 'pending', $4)
            ON CONFLICT (tenant_id, key) DO NOTHING
            RETURNING tenant_id, key, lease_id, outcome, response_status,
                      response_body, request_fingerprint, created_at, expires_at
            """,
            tenant_id,
            key,
            request_fingerprint,
            expires_at,
        )
        if row is not None:
            return True, IdempotencyRecord(row)

        existing = await self.get(tenant_id, key, conn=conn)
        if existing is None:
            # The row was claimed and then expired-and-swept between our INSERT
            # and our SELECT. Vanishingly rare, and retrying is correct.
            return await self.claim(
                tenant_id, key, request_fingerprint, ttl_seconds, conn=conn
            )
        M.idempotent_replay_total.inc()
        return False, existing

    async def get(
        self, tenant_id: str, key: str, *, conn: asyncpg.Connection | None = None
    ) -> IdempotencyRecord | None:
        row = await (conn or self._db).fetchrow(
            """
            SELECT tenant_id, key, lease_id, outcome, response_status,
                   response_body, request_fingerprint, created_at, expires_at
              FROM idempotency_key
             WHERE tenant_id = $1 AND key = $2 AND expires_at > now()
            """,
            tenant_id,
            key,
        )
        return IdempotencyRecord(row) if row else None

    async def complete(
        self,
        tenant_id: str,
        key: str,
        *,
        lease_id: str | None,
        status: int,
        body: dict[str, Any],
        outcome: str = "completed",
        conn: asyncpg.Connection | None = None,
    ) -> None:
        """Store the response so a retry returns exactly what the first call got.

        Storing the response body, not just the lease id, is what makes a replay
        byte-identical — including a rejection. A tenant retrying a 409 should
        get the same 409, not a fresh attempt that might succeed and leave them
        holding capacity they thought they had not been given.
        """
        await (conn or self._db).execute(
            """
            UPDATE idempotency_key
               SET lease_id = $3, outcome = $4, response_status = $5,
                   response_body = $6
             WHERE tenant_id = $1 AND key = $2
            """,
            tenant_id,
            key,
            lease_id,
            outcome,
            status,
            body,
        )

    async def release(
        self, tenant_id: str, key: str, *, conn: asyncpg.Connection | None = None
    ) -> None:
        """Drop a claim that never produced an outcome.

        Used when admission fails in a way the client should be able to retry
        immediately with the same key — a transient dependency failure, not a
        decision. Leaving a pending row would make the retry replay a result
        that was never computed.
        """
        await (conn or self._db).execute(
            """
            DELETE FROM idempotency_key
             WHERE tenant_id = $1 AND key = $2 AND outcome = 'pending'
            """,
            tenant_id,
            key,
        )

    async def sweep(
        self, *, limit: int = 10_000, conn: asyncpg.Connection | None = None
    ) -> int:
        """Delete expired keys — the eviction LLD §12.4 asks for.

        Bounded per call so a long-neglected table is cleared over several ticks
        instead of in one statement that holds locks for minutes.
        """
        status = await (conn or self._db).execute(
            """
            DELETE FROM idempotency_key
             WHERE (tenant_id, key) IN (
                 SELECT tenant_id, key FROM idempotency_key
                  WHERE expires_at <= now()
                  LIMIT $1
             )
            """,
            limit,
        )
        # asyncpg returns the command tag, e.g. "DELETE 137".
        deleted = int(status.rsplit(" ", 1)[-1]) if status else 0
        if deleted:
            log.info("idempotency.swept", deleted=deleted)
        return deleted

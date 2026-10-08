"""Cross-replica coordination: leader election, rate limiting, replay nonces.

Three small pieces of shared state that stop being correct the moment there is
more than one replica, which is precisely the situation LLD §16 describes as the
target deployment.

**Leader election** is an expiring lease with a monotonic fencing token. It is
used only for work that is wasteful or wrong when duplicated — the pool refresh
and the analytics rollup. It is deliberately *not* used for the grace reaper:
the reaper claims individual rows with `SKIP LOCKED`, so it keeps working during
an election, and a lease sitting past its force-stop deadline is the one thing
that must not wait for a leadership handover.

**Rate limiting** is a shared token bucket, because a per-replica limiter
divides the real limit by the replica count and drifts every time the deployment
scales. LLD §12.9 is the driver: without it, "a capacity shortage plus
aggressive client retries becomes a self-inflicted API flood".

**Nonces** stop a captured `/internal` request being replayed inside the
clock-skew window. A signature alone proves the sender knew the key; it does not
prove they are not repeating a reclaim order from five minutes ago.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import asyncpg

from ...logging import get_logger
from ...metrics import M

log = get_logger(__name__)

__all__ = ["LeaderRepository", "RateLimitRepository", "NonceRepository", "LeaderState"]


class LeaderState:
    __slots__ = ("name", "held", "holder", "fence", "expires_at")

    def __init__(
        self, name: str, held: bool, holder: str, fence: int, expires_at: datetime | None
    ) -> None:
        self.name = name
        self.held = held
        self.holder = holder
        self.fence = fence
        self.expires_at = expires_at

    def __bool__(self) -> bool:
        return self.held


class LeaderRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def acquire(
        self, name: str, holder: str, ttl_seconds: float,
        *, conn: asyncpg.Connection | None = None,
    ) -> LeaderState:
        """Take or renew the lock.

        One statement: insert if absent, take over if expired, renew if already
        held by this holder. The fence increments only on a genuine handover, so
        a holder that has been superseded can detect it — its fence is stale
        even though its clock says the lease should still be valid.
        """
        row = await (conn or self._db).fetchrow(
            """
            INSERT INTO leader_lock (name, holder, fence, acquired_at, expires_at)
            VALUES ($1, $2, 1, now(), now() + make_interval(secs => $3::float8))
            ON CONFLICT (name) DO UPDATE
               SET holder      = EXCLUDED.holder,
                   acquired_at = CASE WHEN leader_lock.holder = EXCLUDED.holder
                                      THEN leader_lock.acquired_at ELSE now() END,
                   fence       = leader_lock.fence
                                 + CASE WHEN leader_lock.holder = EXCLUDED.holder
                                        THEN 0 ELSE 1 END,
                   expires_at  = EXCLUDED.expires_at
             WHERE leader_lock.holder = EXCLUDED.holder
                OR leader_lock.expires_at < now()
            RETURNING holder, fence, expires_at
            """,
            name,
            holder,
            float(ttl_seconds),
        )
        if row is None:
            M.leader.labels(lock=name).set(0)
            return LeaderState(name, False, "", 0, None)

        M.leader.labels(lock=name).set(1)
        return LeaderState(name, True, row["holder"], row["fence"], row["expires_at"])

    async def release(
        self, name: str, holder: str, *, conn: asyncpg.Connection | None = None
    ) -> None:
        """Give up the lock at shutdown so the next replica takes over promptly."""
        await (conn or self._db).execute(
            "DELETE FROM leader_lock WHERE name = $1 AND holder = $2", name, holder
        )
        M.leader.labels(lock=name).set(0)

    async def current(
        self, name: str, *, conn: asyncpg.Connection | None = None
    ) -> LeaderState:
        row = await (conn or self._db).fetchrow(
            "SELECT holder, fence, expires_at FROM leader_lock WHERE name = $1", name
        )
        if row is None or row["expires_at"] < datetime.now(row["expires_at"].tzinfo):
            return LeaderState(name, False, "", 0, None)
        return LeaderState(name, True, row["holder"], row["fence"], row["expires_at"])


class RateLimitRepository:
    """Shared token bucket, refilled lazily on read.

    Lazy refill means no background job and no per-tenant timer: the bucket's
    level is a function of the last update and the elapsed time, computed in the
    same statement that spends a token.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    async def take(
        self,
        tenant_id: str,
        *,
        burst: int,
        refill_per_sec: float,
        cost: float = 1.0,
        conn: asyncpg.Connection | None = None,
    ) -> tuple[bool, float]:
        """Spend `cost` tokens. Returns (allowed, seconds until the next token)."""
        row = await (conn or self._db).fetchrow(
            """
            INSERT INTO rate_limit_bucket (tenant_id, tokens, updated_at)
            VALUES ($1, $2::float8 - $4::float8, now())
            ON CONFLICT (tenant_id) DO UPDATE
               SET tokens = LEAST(
                       $2::float8,
                       rate_limit_bucket.tokens
                       + EXTRACT(EPOCH FROM now() - rate_limit_bucket.updated_at)
                         * $3::float8
                   ) - $4::float8,
                   updated_at = now()
             WHERE LEAST(
                       $2::float8,
                       rate_limit_bucket.tokens
                       + EXTRACT(EPOCH FROM now() - rate_limit_bucket.updated_at)
                         * $3::float8
                   ) >= $4::float8
            RETURNING tokens
            """,
            tenant_id,
            float(burst),
            float(refill_per_sec),
            float(cost),
        )
        if row is not None:
            return True, 0.0

        M.rate_limited_total.labels(tenant=tenant_id).inc()
        deficit = await (conn or self._db).fetchval(
            """
            SELECT GREATEST(0, $2::float8 - LEAST(
                       $3::float8,
                       tokens + EXTRACT(EPOCH FROM now() - updated_at) * $4::float8))
              FROM rate_limit_bucket WHERE tenant_id = $1
            """,
            tenant_id,
            float(cost),
            float(burst),
            float(refill_per_sec),
        )
        wait = float(deficit or cost) / refill_per_sec if refill_per_sec > 0 else 60.0
        return False, wait

    async def penalise(
        self, tenant_id: str, tokens: float, *, conn: asyncpg.Connection | None = None
    ) -> None:
        """Charge extra tokens for an outcome the client should have backed off from.

        A 409 already carries Retry-After. A client that ignores it and retries
        immediately is the retry storm HLD §12 warns about, so the second
        rejection costs more than the first.
        """
        await (conn or self._db).execute(
            """
            UPDATE rate_limit_bucket
               SET tokens = GREATEST(-$2::float8 * 4, tokens - $2::float8)
             WHERE tenant_id = $1
            """,
            tenant_id,
            float(tokens),
        )

    async def sweep(
        self, *, idle_seconds: float = 3600.0, conn: asyncpg.Connection | None = None
    ) -> int:
        """Drop buckets that have been full and untouched — they carry no state."""
        status = await (conn or self._db).execute(
            """
            DELETE FROM rate_limit_bucket
             WHERE updated_at < now() - make_interval(secs => $1::float8)
            """,
            float(idle_seconds),
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0


class NonceRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def consume(
        self, nonce: str, ttl_seconds: float, *, key_id: str = "current",
        conn: asyncpg.Connection | None = None,
    ) -> bool:
        """Record a nonce. False means it has been seen — reject the request."""
        row = await (conn or self._db).fetchrow(
            """
            INSERT INTO hmac_nonce (nonce, key_id, expires_at)
            VALUES ($1, $2, now() + make_interval(secs => $3::float8))
            ON CONFLICT (nonce) DO NOTHING
            RETURNING nonce
            """,
            nonce,
            key_id,
            float(ttl_seconds),
        )
        return row is not None

    async def sweep(self, *, conn: asyncpg.Connection | None = None) -> int:
        status = await (conn or self._db).execute(
            "DELETE FROM hmac_nonce WHERE expires_at <= now()"
        )
        return int(status.rsplit(" ", 1)[-1]) if status else 0

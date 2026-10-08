"""asyncpg connection pool and transaction helpers.

Raw asyncpg rather than an ORM, for one reason: the central guarantee of this
design is a single conditional UPDATE (LLD §6.1), and the moment that statement
is generated rather than written it stops being reviewable. Everything on the
admission path is a hand-written statement you can read against the design
document.

Three things are configured here that a default pool would get wrong for a
control plane:

* `statement_timeout` — every query on the request path is a primary-key read
  or a single-row conditional UPDATE. One that runs for seconds is a bug, and
  a bug that holds a connection is how a capacity shortage becomes an outage.
* `idle_in_transaction_session_timeout` — a leaked transaction on `spot_pool`
  would block every admission in the AZ. Bounded, not trusted.
* jsonb codecs bound to orjson, so payload encoding is not a per-row surprise.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence

import asyncpg
import orjson

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

__all__ = ["Database", "connect", "RetryableDBError", "with_retry"]

#: Postgres SQLSTATEs that mean "try again", not "you are wrong".
#: 40001 serialization_failure, 40P01 deadlock_detected, 55P03 lock_not_available.
_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01", "55P03"})


class RetryableDBError(RuntimeError):
    """Raised after retries are exhausted on a transient database failure."""


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, asyncpg.PostgresError):
        return getattr(exc, "sqlstate", None) in _RETRYABLE_SQLSTATES
    return isinstance(
        exc, (asyncpg.ConnectionDoesNotExistError, asyncpg.TooManyConnectionsError, ConnectionError)
    )


async def with_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    attempts: int = 3,
    base_delay: float = 0.02,
    what: str = "query",
) -> Any:
    """Retry a serialization failure or deadlock with jittered backoff.

    Only wraps operations that are safe to repeat. Every write in this service
    is either idempotent by key or conditional on a version, so a retried write
    cannot apply twice — that property is what makes this helper safe to use
    broadly rather than case by case.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if not _is_retryable(exc) or attempt == attempts:
                raise
            last = exc
            delay = base_delay * (2 ** (attempt - 1))
            log.warning(
                "db.retry",
                what=what,
                attempt=attempt,
                sqlstate=getattr(exc, "sqlstate", None),
                delay=round(delay, 4),
            )
            await asyncio.sleep(delay)
    raise RetryableDBError(f"{what} failed after {attempts} attempts") from last


def _dsn_for_asyncpg(url: str) -> str:
    """Accept the SQLAlchemy-style URL Alembic uses and hand asyncpg a plain DSN."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: orjson.dumps(v).decode(),
        decoder=orjson.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=lambda v: orjson.dumps(v).decode(),
        decoder=orjson.loads,
        schema="pg_catalog",
    )


class Database:
    """Thin wrapper over an asyncpg pool.

    Deliberately not a repository: repositories take a `Database` (or a
    connection inside a transaction) so that a caller can compose several
    repository calls into one atomic unit. That is what makes the transactional
    outbox in LLD §12.8 expressible — lease state and the outbox row are written
    by two different repositories in one transaction.
    """

    def __init__(self, pool: asyncpg.Pool, settings: Settings) -> None:
        self._pool = pool
        self._settings = settings

    # -- lifecycle ---------------------------------------------------------
    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    async def close(self) -> None:
        await self._pool.close()

    async def healthy(self) -> bool:
        try:
            async with self.acquire() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except Exception as exc:  # noqa: BLE001 - health check must not raise
            log.warning("db.health_check_failed", error=str(exc))
            return False

    # -- access ------------------------------------------------------------
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with self._pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(
        self, *, isolation: str = "read_committed"
    ) -> AsyncIterator[asyncpg.Connection]:
        """One transaction, one connection.

        `read_committed` is the default and is sufficient everywhere here: the
        atomic reserve gets its guarantee from the conditional UPDATE's row lock,
        not from an isolation level, and raising the level would only trade a
        cheap rejection for an expensive serialization failure under exactly the
        contention the design expects.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction(isolation=isolation):
                yield conn

    # -- convenience passthroughs -----------------------------------------
    async def execute(self, query: str, *args: Any) -> str:
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with self.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def executemany(self, query: str, args: Sequence[Sequence[Any]]) -> None:
        async with self.acquire() as conn:
            await conn.executemany(query, args)


async def connect(settings: Settings) -> Database:
    """Create the pool. Raises if the database is unreachable — fail fast at boot."""
    server_settings = {
        "application_name": f"{settings.service_name}/{settings.worker_id}",
        "statement_timeout": str(settings.db_statement_timeout_ms),
        # A transaction left open on spot_pool blocks every admission in the AZ.
        "idle_in_transaction_session_timeout": str(
            max(settings.db_statement_timeout_ms * 3, 15_000)
        ),
    }
    pool = await asyncpg.create_pool(
        dsn=_dsn_for_asyncpg(settings.database_url),
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        timeout=settings.db_connect_timeout,
        command_timeout=settings.db_command_timeout,
        init=_init_connection,
        server_settings=server_settings,
    )
    assert pool is not None
    log.info(
        "db.connected",
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        statement_timeout_ms=settings.db_statement_timeout_ms,
    )
    return Database(pool, settings)

"""Test fixtures — real PostgreSQL, no mocks in the persistence layer.

The central guarantee of this service is a conditional UPDATE evaluated under
concurrency. A fake repository cannot test that: it would test the fake. So the
suite runs against a real database, created and migrated once per session, with
data truncated between tests.

Timings are compressed (a 3-second grace window rather than 120) but the
*relationships* between them are the production ones, and config validation
enforces the same invariant either way: force_stop_at + teardown_budget must
stay below grace_seconds.

Workers do not start automatically. Tests drive `run_once()` explicitly, so a
test that claims the reaper force-stopped a lease is a test where the reaper
demonstrably ran — rather than one that slept and hoped.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_DSN = "postgresql://spot@127.0.0.1:55432/spot"
ADMIN_DSN = os.environ.get("SPOT_TEST_ADMIN_DSN", DEFAULT_DSN)
TEST_DB = os.environ.get("SPOT_TEST_DB", "spot_test")


def _test_dsn() -> str:
    base = ADMIN_DSN.rsplit("/", 1)[0]
    return f"{base}/{TEST_DB}"


# Set before any spotd import so `get_settings()` sees the test configuration.
os.environ.update(
    SPOT_DATABASE_URL=_test_dsn(),
    SPOT_ENV="dev",
    SPOT_LOG_LEVEL="WARNING",
    SPOT_LOG_FORMAT="console",
    SPOT_BACKEND="sim",
    SPOT_ENABLE_SIM="true",
    SPOT_INTERNAL_HMAC_KEY="test-signing-key-at-least-32-characters-long",
    # Set explicitly so the console tests exercise the credential path rather
    # than the open development mode, which is a different code path.
    SPOT_CONSOLE_TOKEN="test-operator-token-0123456789",
    SPOT_CONSOLE_COOKIE_SECURE="false",
    # Compressed but proportional: 1.6 + 0.6 < 3.0, the same invariant the
    # production values satisfy at 95 + 18 < 120.
    SPOT_GRACE_SECONDS="3",
    SPOT_FORCE_STOP_AT="1.6",
    SPOT_TEARDOWN_BUDGET="0.6",
    SPOT_CONTROL_CYCLE="1",
    SPOT_COOLDOWN="1",
    SPOT_REAPER_INTERVAL="0.2",
    SPOT_REAPER_CLAIM_TTL="1",
    SPOT_OUTBOX_INTERVAL="0.2",
    SPOT_IDEMPOTENCY_TTL="60",
    SPOT_DB_POOL_MIN="1",
    SPOT_DB_POOL_MAX="24",
    SPOT_RATE_LIMIT_ENABLED="false",
    SPOT_TENANT_QUOTA="64",
)

#: Tables truncated between tests. Reference data is seeded once and left alone.
_VOLATILE = (
    "notice_delivery",
    "usage_record",
    "credit_record",
    "capacity_ledger_entry",
    "interruption_stat",
    "outbox",
    "idempotency_key",
    "hmac_nonce",
    "rate_limit_bucket",
    "leader_lock",
    "spot_pool_cooldown",
    "reclaim_order",
    "spot_lease",
)


async def _ensure_database() -> None:
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        exists = await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB
        )
        if not exists:
            await admin.execute(f'CREATE DATABASE "{TEST_DB}"')
    finally:
        await admin.close()


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Create, migrate and seed the test database once per session."""
    asyncio.run(_ensure_database())

    env = {**os.environ, "SPOT_DATABASE_URL": _test_dsn()}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover - surfaces setup failures
        raise RuntimeError(f"migrations failed:\n{result.stdout}\n{result.stderr}")

    async def load() -> None:
        from spotd.db import connect
        from spotd.config import get_settings
        from spotd.seed import seed

        db = await connect(get_settings(reload=True))
        try:
            # No forecast trace: tests drive the pool explicitly so their
            # arithmetic is exact rather than dependent on the time of day.
            await seed(db, with_trace=False)
        finally:
            await db.close()

    asyncio.run(load())


@pytest_asyncio.fixture
async def clean_db(migrated_database: None) -> AsyncIterator[None]:
    """Truncate volatile tables before each test.

    `preemption_audit` has a DELETE trigger — it is append-only in the database,
    which is the point (HLD §11). TRUNCATE bypasses row triggers, so the audit
    log can be cleared for tests without weakening the guarantee that protects
    it in production.
    """
    conn = await asyncpg.connect(_test_dsn())
    try:
        await conn.execute(
            f"TRUNCATE {', '.join(_VOLATILE)}, preemption_audit RESTART IDENTITY CASCADE"
        )
        await conn.execute("UPDATE spot_pool SET sellable_units = 0, reserved_units = 0, cooldown_units = 0")
        await conn.execute("UPDATE host_group SET quarantined = false")
    finally:
        await conn.close()
    yield


@pytest_asyncio.fixture
async def container(clean_db: None) -> AsyncIterator[Any]:
    """A fully wired container with the workers *not* running."""
    from spotd.config import get_settings
    from spotd.container import build

    c = await build(get_settings(reload=True))
    await c.pool_view.ensure()
    try:
        yield c
    finally:
        for task in list(c._fulfilment_tasks):  # noqa: SLF001
            task.cancel()
        for worker in c.workers:
            await worker.stop()
        await c.externals.aclose()
        await c.db.close()


@pytest_asyncio.fixture
async def pooled(container: Any) -> Any:
    """A container whose AZs have a known, fixed amount of capacity for sale.

    Every capacity assertion in the suite is against these numbers, so a test
    that says "exactly six launches win" means exactly six.
    """
    from datetime import timedelta

    from spotd.domain.models import SellableFeed, utcnow

    for az, units in (("az-1", 240), ("az-2", 120), ("az-3", 60)):
        await container.pool_repo.refresh(
            SellableFeed(
                az=az,
                units=units,
                confidence=0.95,
                published_at=utcnow(),
                horizon_seconds=900,
            ),
            accepted=True,
            applied_units=units,
            degraded=False,
        )
    return container


@pytest_asyncio.fixture
async def client(pooled: Any) -> AsyncIterator[Any]:
    """An HTTP client bound to the app, sharing the fixture's container."""
    import httpx

    from spotd.api.app import create_app
    from spotd.api.auth import SignatureVerifier
    from spotd.config import get_settings

    app = create_app(get_settings())
    # Reuse the fixture container instead of letting lifespan build a second
    # one — two containers would mean two connection pools and two views of the
    # same rows, and a test could pass against the wrong one.
    app.state.container = pooled
    app.state.verifier = SignatureVerifier(
        settings=pooled.settings, nonce_repo=pooled.nonce_repo
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://spot.test") as c:
        yield c


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
@pytest.fixture
def signed() -> Any:
    """Produce signed headers for an /internal call."""
    import orjson

    from spotd.api.auth import sign_request

    def _sign(method: str, path: str, payload: Any = None) -> tuple[bytes, dict[str, str]]:
        body = orjson.dumps(payload) if payload is not None else b""
        headers = sign_request(
            key=os.environ["SPOT_INTERNAL_HMAC_KEY"],
            method=method,
            path=path,
            body=body,
        )
        headers["content-type"] = "application/json"
        return body, headers

    return _sign


@pytest.fixture
def launch() -> Any:
    """Admit a lease directly through the core, bypassing HTTP."""
    import uuid

    from spotd.db.repositories import fingerprint

    async def _launch(
        container: Any,
        *,
        tenant: str = "tenant-spot-00",
        flavour: str = "s1.medium",
        count: int = 1,
        az: str = "az-1",
        key: str | None = None,
    ) -> Any:
        result = await container.market.launch(
            tenant_id=tenant,
            flavour=flavour,
            count=count,
            az=az,
            idempotency_key=key or f"k-{uuid.uuid4().hex}",
            request_fingerprint=fingerprint(
                {"flavour": flavour, "count": count, "az": az, "purchase_option": None}
            ),
            purchase_option=None,
        )
        return result.lease

    return _launch


@pytest.fixture
def run_to_running(launch: Any) -> Any:
    """Admit and fulfil a lease, returning it in RUNNING."""

    async def _run(container: Any, **kwargs: Any) -> Any:
        lease = await launch(container, **kwargs)
        running = await container.lease_manager.fulfil(lease.lease_id)
        assert running is not None, "fulfilment did not reach RUNNING"
        return running

    return _run


@pytest.fixture
def reclaim_order() -> Any:
    """Build a reclaim order for the handler."""
    from datetime import timedelta

    from spotd.domain.models import ReclaimOrder, utcnow

    def _order(
        *,
        order_id: str = "order-test",
        az: str = "az-1",
        units: int = 8,
        host_group: str | None = None,
        deadline_seconds: float = 5.0,
    ) -> ReclaimOrder:
        return ReclaimOrder(
            order_id=order_id,
            az=az,
            units=units,
            host_group=host_group,
            deadline=utcnow() + timedelta(seconds=deadline_seconds),
            reason="test",
            requested_by="pytest",
        )

    return _order

"""Test fixtures.

Environment is set BEFORE importing anything from `spot`, because Config reads
os.environ once at import time.
"""
from __future__ import annotations

import os

os.environ.setdefault("SPOT_GRACE_SECONDS", "1.2")
os.environ.setdefault("SPOT_FORCE_STOP_AT", "0.9")
os.environ.setdefault("SPOT_TEARDOWN_BUDGET", "1.5")
os.environ.setdefault("SPOT_CONTROL_CYCLE", "0.3")
os.environ.setdefault("SPOT_COOLDOWN", "0.2")
os.environ.setdefault("SIM_CREATE_SECONDS", "0.02")
os.environ.setdefault("SIM_STOP_SECONDS", "0.02")
os.environ.setdefault("SIM_TEARDOWN_SECONDS", "0.05")

import asyncio  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from spot.container import Container  # noqa: E402
from spot.domain.models import LeaseState  # noqa: E402


@pytest_asyncio.fixture
async def c():
    container = Container()
    await container.start()
    try:
        yield container
    finally:
        await container.stop()


async def wait_for(pred, timeout: float = 8.0, interval: float = 0.01) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


async def launch_running(c, tenant="tenant-spot-a", flavour="s1.small", count=1,
                         az="az-1", key=None, drain_seconds=0.05):
    lease, _ = await c.market.launch(
        tenant_id=tenant, flavour_name=flavour, count=count, az=az,
        idempotency_key=key, drain_seconds=drain_seconds,
    )
    assert await wait_for(lambda: lease.state is LeaseState.RUNNING), (
        f"lease stuck in {lease.state}"
    )
    return lease


pytest_plugins: list[str] = []

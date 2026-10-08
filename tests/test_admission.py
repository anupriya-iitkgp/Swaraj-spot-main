"""Admission path: entitlement, quota, idempotency, and the no-over-allocation
guarantee that the whole design rests on.
"""
from __future__ import annotations

import asyncio

import pytest

from spot.domain.errors import (
    FlavourNotEligible,
    NoCapacity,
    NotEntitled,
    QuotaExceeded,
)
from spot.domain.models import LeaseState

from .conftest import launch_running, wait_for

pytestmark = pytest.mark.asyncio


async def test_launch_reaches_running_and_places_on_a_host(c):
    lease = await launch_running(c, flavour="s1.medium", count=2)
    assert lease.state is LeaseState.RUNNING
    assert lease.host_group is not None
    assert len(lease.instance_ids) == 2
    assert lease.units == 8
    # discount is snapshotted at admission
    assert 0.0 < lease.discount_snapshot <= 1.0
    assert lease.rate_per_sec > 0


async def test_non_spot_account_is_rejected_403(c):
    with pytest.raises(NotEntitled):
        await c.market.launch(tenant_id="tenant-dynamic", flavour_name="s1.small",
                              count=1, az="az-1", idempotency_key=None)


async def test_unknown_tenant_is_rejected(c):
    with pytest.raises(NotEntitled):
        await c.market.launch(tenant_id="nobody", flavour_name="s1.small",
                              count=1, az="az-1", idempotency_key=None)


async def test_non_spot_eligible_flavour_is_rejected_400(c):
    with pytest.raises(FlavourNotEligible) as exc:
        await c.market.launch(tenant_id="tenant-spot-a", flavour_name="db1.xlarge",
                              count=1, az="az-1", idempotency_key=None)
    assert "s1.small" in exc.value.extra["spot_eligible_flavours"]


async def test_quota_exceeded_is_429(c):
    with pytest.raises(QuotaExceeded):
        await c.market.launch(tenant_id="tenant-spot-a", flavour_name="s1.large",
                              count=32, az="az-1", idempotency_key=None)


async def test_idempotent_retry_returns_the_original_lease(c):
    first, replayed_a = await c.market.launch(
        tenant_id="tenant-spot-a", flavour_name="s1.small", count=1, az="az-1",
        idempotency_key="k-1",
    )
    second, replayed_b = await c.market.launch(
        tenant_id="tenant-spot-a", flavour_name="s1.small", count=1, az="az-1",
        idempotency_key="k-1",
    )
    assert replayed_a is False and replayed_b is True
    assert first.lease_id == second.lease_id
    # and only ONE lease exists
    assert len([l for l in c.lease_manager.all()]) == 1


async def test_no_capacity_returns_retry_after_and_alternatives(c):
    # squeeze az-1 to nothing, leave az-2 with room
    c.forecast.set_headroom("az-1", 10_000)
    await c.pool.refresh()
    with pytest.raises(NoCapacity) as exc:
        await c.market.launch(tenant_id="tenant-spot-a", flavour_name="s1.small",
                              count=1, az="az-1", idempotency_key=None)
    assert exc.value.retry_after >= 1
    alts = exc.value.extra["alternatives"]
    assert any(a["az"] == "az-2" and a["available_units"] > 0 for a in alts)


async def test_concurrent_launches_never_over_allocate(c):
    """The core admission guarantee.

    Ten tenants race for a pool that can only satisfy some of them. The pool
    read is stale by design; the atomic reserve must make over-allocation
    impossible, and the losers must get a clean 409.
    """
    az = "az-1"
    # pin the pool to exactly 12 units => 6 x s1.small (2 units each)
    free = c.ledger.free_units(az)
    c.forecast.set_headroom(az, free - 12)
    await c.pool.refresh()
    assert c.pool.get_sellable(az) == 12

    async def attempt(i: int):
        try:
            lease, _ = await c.market.launch(
                tenant_id=f"tenant-spot-{'abc'[i % 3]}", flavour_name="s1.small",
                count=1, az=az, idempotency_key=f"race-{i}", drain_seconds=0.05,
            )
            return lease
        except NoCapacity:
            return None

    results = await asyncio.gather(*(attempt(i) for i in range(20)))
    admitted = [r for r in results if r is not None]
    rejected = [r for r in results if r is None]

    assert len(admitted) == 6, f"over/under-allocated: {len(admitted)} admitted"
    assert len(rejected) == 14
    assert sum(l.units for l in admitted) <= 12
    pool = c.pool.pool(az)
    assert pool.reserved_units <= pool.sellable_units


async def test_failed_provisioning_releases_the_reservation(c, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(c.scheduler, "place", boom)
    before = c.pool.pool("az-1").reserved_units

    lease, _ = await c.market.launch(tenant_id="tenant-spot-a", flavour_name="s1.small",
                                     count=1, az="az-1", idempotency_key=None)
    assert await wait_for(lambda: lease.state is LeaseState.REJECTED)
    assert c.pool.pool("az-1").reserved_units == before, "capacity leaked"
    assert lease.amount == 0.0

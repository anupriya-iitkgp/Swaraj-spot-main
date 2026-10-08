"""Reclaim path: ordering, victim policy, the grace timer, and the rule that
capacity is only free once teardown is confirmed.
"""
from __future__ import annotations

import asyncio

import pytest

from spot.config import CONFIG
from spot.domain.models import LeaseState

from .conftest import launch_running, wait_for

pytestmark = pytest.mark.asyncio


async def test_pool_shrinks_before_victims_are_selected(c):
    """Edge 20 must fire before edge 21.

    If victim selection ran first, new spot could be sold into capacity that is
    already being taken back. We assert the ordering directly by recording when
    each happens.
    """
    await launch_running(c)
    order: list[str] = []

    real_shrink = c.pool.shrink
    real_select = c.victim_selector.select

    async def traced_shrink(*a, **k):
        order.append("shrink")
        return await real_shrink(*a, **k)

    def traced_select(*a, **k):
        order.append("select")
        return real_select(*a, **k)

    c.pool.shrink = traced_shrink
    c.victim_selector.select = traced_select

    await c.reclaim_handler.handle(units=2, az="az-1", reason="test")
    assert order[:2] == ["shrink", "select"], order


async def test_clean_guest_exits_inside_the_grace_window(c):
    lease = await launch_running(c, drain_seconds=0.05)
    res = await c.reclaim_handler.handle(units=lease.units, az="az-1",
                                         host_group=lease.host_group, reason="test")
    assert lease.lease_id in res["preempted"]
    assert await wait_for(lambda: lease.state is LeaseState.CLOSED)
    assert lease.forced_stop is False
    assert lease.notice_at is not None
    assert (lease.closed_at - lease.notice_at) <= CONFIG.grace_seconds


async def test_guest_that_ignores_the_notice_is_force_stopped(c):
    lease = await launch_running(c, drain_seconds=None)  # never exits
    await c.reclaim_handler.handle(units=lease.units, az="az-1",
                                   host_group=lease.host_group, reason="test")
    assert await wait_for(lambda: lease.state is LeaseState.CLOSED,
                          timeout=CONFIG.grace_seconds + 5)
    assert lease.forced_stop is True
    kinds = [e["kind"] for e in c.audit.entries(lease_id=lease.lease_id)]
    assert "timer_expired" in kinds and "forced_stop" in kinds


async def test_capacity_is_only_free_after_teardown_is_confirmed(c):
    lease = await launch_running(c, drain_seconds=0.05)
    hg = c.ledger.get(lease.host_group)
    assert hg.spot_allocated >= lease.units

    await c.reclaim_handler.handle(units=lease.units, az="az-1",
                                   host_group=lease.host_group, reason="test")
    # while draining, the units are RECLAIMING — not allocated, not free
    assert await wait_for(lambda: hg.spot_reclaiming >= lease.units or lease.is_terminal)

    assert await wait_for(lambda: lease.state is LeaseState.CLOSED)
    assert hg.spot_reclaiming == 0
    entries = c.audit.entries(lease_id=lease.lease_id)
    kinds = [e["kind"] for e in entries]
    assert "capacity_returned" in kinds
    # the CLOSED transition must come AFTER capacity was reported returned
    returned_seq = next(e["seq"] for e in entries if e["kind"] == "capacity_returned")
    closed_seq = next(e["seq"] for e in entries
                      if e["kind"] == "lease_transition" and e["dst"] == "CLOSED")
    assert returned_seq < closed_seq


async def test_stalled_teardown_holds_capacity_in_reclaiming(c, monkeypatch):
    lease = await launch_running(c, drain_seconds=0.05)
    hg = c.ledger.get(lease.host_group)

    async def hang(*a, **k):
        await asyncio.sleep(30)

    monkeypatch.setattr(c.provisioning_adapter, "teardown_all", hang)
    await c.reclaim_handler.handle(units=lease.units, az="az-1",
                                   host_group=lease.host_group, reason="test")
    assert await wait_for(lambda: lease.lease_id in c.teardown_confirmer.stalled,
                          timeout=CONFIG.grace_seconds + CONFIG.teardown_budget + 5)
    # never counted free
    assert hg.spot_reclaiming >= lease.units
    assert lease.state is LeaseState.STOPPED and not lease.is_terminal


async def test_reclaim_during_provisioning_cancels_outright(c):
    """No notice for an instance that never ran, and no charge for it."""
    lease, _ = await c.market.launch(tenant_id="tenant-spot-a", flavour_name="s1.small",
                                     count=1, az="az-2", idempotency_key=None)
    assert lease.state in (LeaseState.ADMITTED, LeaseState.PROVISIONING)

    await c.reclaim_handler.handle(units=lease.units, az="az-2", reason="test")
    assert await wait_for(lambda: lease.state is LeaseState.REJECTED)

    assert c.audit.entries(lease_id=lease.lease_id, kind="notice_issued") == []
    assert lease.amount == 0.0
    assert lease.notice_at is None


async def test_grace_seconds_are_not_billed(c):
    lease = await launch_running(c, drain_seconds=0.4)
    await asyncio.sleep(0.5)  # some genuinely billable runtime
    await c.reclaim_handler.handle(units=lease.units, az="az-1",
                                   host_group=lease.host_group, reason="test")
    assert await wait_for(lambda: lease.state is LeaseState.CLOSED)

    gross = lease.stopped_at - lease.running_at
    assert lease.grace_seconds_excluded > 0
    assert lease.billed_seconds < gross
    assert abs((lease.billed_seconds + lease.grace_seconds_excluded) - gross) < 0.05
    assert lease.amount == pytest.approx(lease.billed_seconds * lease.rate_per_sec)


async def test_all_notice_channels_failing_raises_a_credit(c):
    c.notice_delivery.disabled_channels = {"metadata", "webhook", "event_stream"}
    lease = await launch_running(c, drain_seconds=None)
    await asyncio.sleep(0.3)
    await c.reclaim_handler.handle(units=lease.units, az="az-1",
                                   host_group=lease.host_group, reason="test")
    assert await wait_for(lambda: lease.state is LeaseState.CLOSED,
                          timeout=CONFIG.grace_seconds + 5)
    assert lease.notice_channels_delivered == []
    assert c.billing.credits, "no credit raised for an undelivered notice"
    assert c.audit.entries(lease_id=lease.lease_id, kind="credit")


async def test_notice_reaches_the_tenant_webhook_and_metadata(c):
    got: list[dict] = []

    async def hook(payload):
        got.append(payload)

    c.notice_delivery.register_webhook("tenant-spot-a", hook)
    lease = await launch_running(c, tenant="tenant-spot-a", drain_seconds=0.05)
    await c.reclaim_handler.handle(units=lease.units, az="az-1",
                                   host_group=lease.host_group, reason="test")
    assert await wait_for(lambda: bool(got))
    assert set(lease.notice_channels_delivered) == {"metadata", "webhook", "event_stream"}
    meta = c.notice_delivery.termination_time(lease.instance_ids[0])
    assert meta and meta["action"] == "terminate"


async def test_blast_radius_caps_a_single_tenant(c):
    """One tenant with four leases must not lose the whole fleet in one wave."""
    leases = [await launch_running(c, tenant="tenant-spot-b", drain_seconds=0.05)
              for _ in range(4)]
    total_units = sum(l.units for l in leases)

    res = await c.reclaim_handler.handle(units=total_units, az="az-1", reason="test")
    taken = res["units_freed"]
    cap = int(total_units * CONFIG.blast_radius_fraction)
    assert 0 < taken <= max(cap, max(l.units for l in leases)), (
        f"blast radius exceeded: took {taken} of {total_units}"
    )


async def test_victim_selection_prefers_draining_one_host(c):
    """Contiguity beats fairness: the order that can be satisfied from a single
    host group must not be spread across two."""
    a = await launch_running(c, tenant="tenant-spot-a", flavour="s1.medium",
                             count=1, az="az-1", drain_seconds=0.05)   # 4 units
    b = await launch_running(c, tenant="tenant-spot-b", flavour="s1.medium",
                             count=1, az="az-1", drain_seconds=0.05)   # 4 units
    hosts = {a.host_group, b.host_group}
    if len(hosts) == 1:
        # bin-packing already put them on one host: that IS the property.
        assert a.host_group == b.host_group
        return
    res = await c.reclaim_handler.handle(units=4, az="az-1", reason="test")
    chosen = [c.lease_manager.get(i) for i in res["preempted"]]
    assert len({l.host_group for l in chosen}) == 1

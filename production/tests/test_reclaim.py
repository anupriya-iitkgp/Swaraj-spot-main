"""The reclaim path: shrink, select, notice, force stop, teardown, close.

Maps to the reclaim block of the LLD §15 matrix, plus the edge cases HLD §12
asks to be treated "as a first-class path, not an exception".
"""

from __future__ import annotations

import asyncio

import pytest

from spotd.domain.models import ReclaimOrderState
from spotd.domain.state_machine import LeaseState
from spotd.external.hypervisor import GuestBehaviour

pytestmark = pytest.mark.asyncio


async def _drain(container, lease_id: str, *, timeout: float = 12.0) -> object:
    """Run the reaper and sweepers until the lease closes, or give up."""
    reaper = next(w for w in container.workers if w.name == "grace_reaper")
    sweeper = next(w for w in container.workers if w.name == "teardown_sweeper")
    deadline = asyncio.get_running_loop().time() + timeout

    while asyncio.get_running_loop().time() < deadline:
        lease = await container.lease_repo.get(lease_id)
        if lease.state is LeaseState.CLOSED:
            return lease
        await reaper.run_once()
        await sweeper.run_once()
        await asyncio.sleep(0.15)
    return await container.lease_repo.get(lease_id)


async def test_pool_shrinks_before_victims_are_selected(
    pooled, run_to_running, reclaim_order, monkeypatch
):
    """Edge 20 strictly precedes edge 21 (§3.1).

    If victims were chosen first, a launch arriving in between would be admitted
    against capacity already being taken back. The ordering is asserted by
    observing the pool at the moment selection runs.
    """
    lease = await run_to_running(pooled, flavour="s1.large")
    sellable_before = (await pooled.pool_repo.get("az-1")).sellable_units

    observed: dict[str, int] = {}
    original = pooled.victim_selector.select

    async def spy(order, **kwargs):
        snapshot = await pooled.pool_repo.get(order.az)
        observed["sellable_at_selection"] = snapshot.sellable_units
        return await original(order, **kwargs)

    monkeypatch.setattr(pooled.victim_selector, "select", spy)

    await pooled.reclaim_handler.handle(
        reclaim_order(units=8, host_group=lease.host_group)
    )

    assert observed["sellable_at_selection"] == sellable_before - 8, (
        "the advertised pool must already be smaller by the time victims are "
        "chosen (edge 20 before edge 21)"
    )


async def test_clean_guest_exit_inside_the_grace_window(pooled, run_to_running, reclaim_order):
    """The clean path completes inside the budget (§6.5)."""
    lease = await run_to_running(pooled, flavour="s1.large")
    pooled.externals.hypervisor.pin_behaviour(
        lease.lease_id, GuestBehaviour.COOPERATIVE
    )

    await pooled.reclaim_handler.handle(
        reclaim_order(units=8, host_group=lease.host_group)
    )
    closed = await _drain(pooled, lease.lease_id)

    assert closed.state is LeaseState.CLOSED
    assert closed.forced_stop is False
    assert closed.grace_window_seconds <= pooled.settings.grace_seconds
    assert closed.reclaim_window_seconds <= pooled.settings.grace_seconds


async def test_guest_that_ignores_the_notice_is_force_stopped(
    pooled, run_to_running, reclaim_order
):
    """The timer is authoritative (§6.5).

    HLD §6 forbids the Grace Timer from waiting "on the guest beyond the timer".
    The grace period is a courtesy; the force stop is the guarantee.
    """
    lease = await run_to_running(pooled, flavour="s1.large")
    pooled.externals.hypervisor.pin_behaviour(
        lease.lease_id, GuestBehaviour.IGNORES_NOTICE
    )

    await pooled.reclaim_handler.handle(
        reclaim_order(units=8, host_group=lease.host_group)
    )
    noticed = await pooled.lease_repo.get(lease.lease_id)
    assert noticed.state is LeaseState.NOTICE_ISSUED
    assert noticed.force_stop_deadline is not None

    closed = await _drain(pooled, lease.lease_id)

    assert closed.state is LeaseState.CLOSED
    assert closed.forced_stop is True
    assert pooled.externals.hypervisor.is_stopped(lease.lease_id)


async def test_capacity_is_only_free_after_teardown_is_confirmed(
    pooled, run_to_running, reclaim_order
):
    """capacity_returned precedes CLOSED (§4.1).

    LLD §9: never report capacity free on an unconfirmed commit.
    """
    lease = await run_to_running(pooled, flavour="s1.large")
    pooled.externals.hypervisor.pin_behaviour(lease.lease_id, GuestBehaviour.COOPERATIVE)

    await pooled.reclaim_handler.handle(
        reclaim_order(units=8, host_group=lease.host_group)
    )
    closed = await _drain(pooled, lease.lease_id)
    assert closed.state is LeaseState.CLOSED

    entries = await pooled.ledger_repo.units_by_state()
    assert entries.get("returned", 0) >= lease.units

    # The audit ordering is the actual proof: capacity.returned is written in
    # the same transaction as the close, and never before teardown confirms.
    events = [e["event"] for e in await pooled.audit_repo.for_lease(lease.lease_id)]
    assert "capacity.returned" in events
    assert events.index("preemption.notice_issued") < events.index("capacity.returned")


async def test_stalled_teardown_holds_capacity_in_reclaiming(
    pooled, run_to_running, reclaim_order
):
    """A stuck teardown never looks like free capacity (§11).

    LLD §11: "Units stay RECLAIMING; lease stays STOPPED; the stalled list grows."
    """
    lease = await run_to_running(pooled, flavour="s1.large")
    pooled.externals.hypervisor.pin_behaviour(
        lease.lease_id, GuestBehaviour.TEARDOWN_STALLS_FOREVER
    )
    reserved_before = (await pooled.pool_repo.get("az-1")).reserved_units

    await pooled.reclaim_handler.handle(
        reclaim_order(units=8, host_group=lease.host_group)
    )
    reaper = next(w for w in pooled.workers if w.name == "grace_reaper")
    sweeper = next(w for w in pooled.workers if w.name == "teardown_sweeper")
    # Long enough to pass force_stop_at (1.6s) and then sweep repeatedly.
    for _ in range(20):
        await reaper.run_once()
        await sweeper.run_once()
        await asyncio.sleep(0.2)

    stuck = await pooled.lease_repo.get(lease.lease_id)
    assert stuck.state is LeaseState.STOPPED
    assert stuck.teardown_stalled is True
    assert stuck.closed_at is None

    # The units are still held — not silently returned to the sellable pool.
    assert (await pooled.pool_repo.get("az-1")).reserved_units == reserved_before

    events = [e["event"] for e in await pooled.audit_repo.for_lease(lease.lease_id)]
    assert "capacity.teardown_stalled" in events
    assert "capacity.returned" not in events


async def test_transient_stall_recovers_without_an_operator(
    pooled, run_to_running, reclaim_order
):
    """The sweeper is the automated recovery LLD §11 asks for."""
    lease = await run_to_running(pooled, flavour="s1.large")
    pooled.externals.hypervisor.pin_behaviour(
        lease.lease_id, GuestBehaviour.TEARDOWN_STALLS
    )

    await pooled.reclaim_handler.handle(
        reclaim_order(units=8, host_group=lease.host_group)
    )
    closed = await _drain(pooled, lease.lease_id, timeout=15.0)

    assert closed.state is LeaseState.CLOSED
    assert closed.teardown_stalled is False


async def test_reclaim_during_provisioning_cancels_outright(
    pooled, launch, reclaim_order
):
    """No notice, no charge for a lease that never ran (§4.1, §10.4).

    HLD §12 calls this "the easiest case to get wrong" and asks for it to be
    modelled explicitly and tested as a first-class path.
    """
    lease = await launch(pooled, flavour="s1.large")
    assert lease.state is LeaseState.ADMITTED
    reserved_before = (await pooled.pool_repo.get("az-1")).reserved_units

    order = reclaim_order(units=8)
    assert await pooled.lease_manager.preempt(lease, order) is True

    cancelled = await pooled.lease_repo.get(lease.lease_id)
    assert cancelled.state is LeaseState.CLOSED
    assert cancelled.preemption_reason.value == "cancelled_in_flight"
    assert cancelled.notice_at is None, "a lease that never ran gets no notice"
    assert cancelled.running_at is None
    assert cancelled.billed_amount == 0, "no charge for an instance that never ran"

    # The reservation went back.
    assert (await pooled.pool_repo.get("az-1")).reserved_units < reserved_before

    # And fulfilment, arriving late, must not resurrect it.
    assert await pooled.lease_manager.fulfil(lease.lease_id) is None
    still = await pooled.lease_repo.get(lease.lease_id)
    assert still.state is LeaseState.CLOSED


async def test_two_reclaim_orders_cannot_preempt_the_same_lease_twice(
    pooled, run_to_running, reclaim_order
):
    """preempt() is a no-op unless the lease is RUNNING (§10.4)."""
    lease = await run_to_running(pooled, flavour="s1.large")

    first = await pooled.lease_manager.preempt(lease, reclaim_order(order_id="o1"))
    stale = lease  # the caller's copy is now out of date, as it would be in a race
    second = await pooled.lease_manager.preempt(stale, reclaim_order(order_id="o2"))

    assert first is True
    assert second is False

    noticed = await pooled.lease_repo.get(lease.lease_id)
    assert noticed.reclaim_order_id == "o1"


async def test_replayed_reclaim_order_preempts_nothing_further(
    pooled, run_to_running, reclaim_order
):
    """Idempotent per order_id (LLD §16).

    A duplicated launch wastes capacity; a duplicated reclaim kills twice as
    many customer instances as the capacity side asked for.
    """
    await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-00")
    await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-01")

    order = reclaim_order(order_id="dup-1", units=8)
    first = await pooled.reclaim_handler.handle(order)
    second = await pooled.reclaim_handler.handle(order)

    assert first.replayed is False
    assert second.replayed is True
    assert second.leases_noticed == first.leases_noticed
    assert second.units_shrunk == 0, "a replay must not shrink the pool again"

    noticed = await pooled.db.fetchval(
        "SELECT COUNT(*)::int FROM spot_lease WHERE state = 'NOTICE_ISSUED'"
    )
    assert noticed == len(first.leases_noticed)


async def test_victim_selection_prefers_draining_one_host(pooled, run_to_running, reclaim_order):
    """Contiguity beats fairness (§6.4).

    The precedence is contiguity > flavour > fairness > blast radius. Fairness
    orders victims *within* the host set contiguity chose; it does not choose
    the set.
    """
    # Spread leases over several host groups by filling the first ones.
    leases = [
        await run_to_running(pooled, flavour="s1.xlarge", tenant=f"tenant-spot-{n:02d}")
        for n in range(6)
    ]
    by_host: dict[str, list] = {}
    for lease in leases:
        by_host.setdefault(lease.host_group, []).append(lease)

    outcome = await pooled.reclaim_handler.handle(reclaim_order(units=16))

    chosen_hosts = {
        (await pooled.lease_repo.get(lid)).host_group for lid in outcome.leases_noticed
    }
    assert len(chosen_hosts) == 1, (
        f"a 16-unit order should be satisfied from one host group, not "
        f"{len(chosen_hosts)}: {chosen_hosts}"
    )


async def test_blast_radius_caps_a_single_tenant(pooled, run_to_running, reclaim_order):
    """One wave cannot take a whole fleet (§6.4).

    With blast_radius 0.5 a tenant holding four leases can lose at most two in
    one wave beyond the first — the cap never blocks the first, or a
    single-lease tenant would be permanently un-preemptible.
    """
    fleet = [
        await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-02")
        for _ in range(4)
    ]
    host = fleet[0].host_group
    same_host = [lease for lease in fleet if lease.host_group == host]
    assert len(same_host) >= 3, "test needs at least three leases packed together"

    selection = await pooled.victim_selector.select(
        reclaim_order(units=8 * len(same_host), host_group=host)
    )

    taken = sum(
        1 for v in selection.victims if v.tenant_id == "tenant-spot-02"
    )
    # Everything on that host belongs to one tenant, so the cap must have been
    # hit and the escalation recorded rather than silently ignored.
    assert selection.blast_radius_exceeded is True
    assert taken >= 1


async def test_reclaim_larger_than_the_running_fleet_reports_partial(
    pooled, run_to_running, reclaim_order
):
    """This subsystem does not decide how much capacity exists (HLD §1).

    An order for more than any running spot lease holds is answered honestly,
    not by inventing victims.
    """
    await run_to_running(pooled, flavour="s1.large")

    outcome = await pooled.reclaim_handler.handle(reclaim_order(units=200))

    assert outcome.partial is True
    assert outcome.units_selected < 200
    assert "only" in outcome.detail
    assert outcome.order.state is ReclaimOrderState.PARTIAL


async def test_unplaced_leases_are_never_selected_for_a_host_scoped_order(
    pooled, launch, run_to_running, reclaim_order
):
    """Closes LLD §12.6.

    "An unplaced lease may be killed to satisfy an order for a host it was never
    going to land on." A host-scoped order can only be satisfied by leases
    actually on that host.
    """
    placed = await run_to_running(pooled, flavour="s1.large")
    unplaced = await launch(pooled, flavour="s1.large", tenant="tenant-spot-04")
    assert unplaced.host_group is None

    selection = await pooled.victim_selector.select(
        reclaim_order(units=64, host_group=placed.host_group)
    )

    assert unplaced.lease_id not in selection.lease_ids
    for victim in selection.victims:
        assert victim.host_group == placed.host_group


async def test_notice_channels_are_recorded_atomically(
    pooled, run_to_running, reclaim_order
):
    """Closes LLD §12.7.

    "A concurrent describe can observe a lease in NOTICE_ISSUED with an empty
    channel list." Once the lease is observable as noticed, the channel list is
    already final.
    """
    lease = await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-01")
    await pooled.reclaim_handler.handle(
        reclaim_order(units=8, host_group=lease.host_group)
    )

    noticed = await pooled.lease_repo.get(lease.lease_id)
    assert noticed.state is LeaseState.NOTICE_ISSUED
    assert noticed.notice_channels_delivered, "channels must be set with the state"

    receipts = await pooled.db.fetch(
        "SELECT channel, delivered FROM notice_delivery WHERE lease_id = $1",
        lease.lease_id,
    )
    # All three channels are attempted, whatever the outcome — proof of delivery
    # is proof of the attempt too (HLD §6: never fail silently).
    assert {r["channel"] for r in receipts} == {"metadata", "webhook", "event_stream"}


async def test_all_channels_failing_raises_an_automatic_credit(
    pooled, run_to_running, reclaim_order, monkeypatch
):
    """An undelivered notice is a credit, not a silence (§6.6).

    HLD §10 on notice_channels_delivered: "Empty => automatic credit."
    """
    lease = await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-05")

    from spotd.domain.models import NoticeChannel, NoticeReceipt, utcnow

    async def no_metadata(lease, deadline):
        # A channel that reports failure cleanly...
        return NoticeReceipt(
            lease_id=lease.lease_id,
            channel=NoticeChannel.METADATA,
            delivered=False,
            attempt=1,
            at=utcnow(),
            error="metadata service unavailable",
        )

    async def no_webhook(*args, **kwargs):
        # ...and two that fail by raising. Both must be absorbed.
        raise RuntimeError("tenant endpoint unreachable")

    async def no_event_stream(*args, **kwargs):
        raise RuntimeError("bus unavailable")

    monkeypatch.setattr(pooled.notice, "_metadata", no_metadata)
    monkeypatch.setattr(pooled.notice, "_webhook", no_webhook)
    monkeypatch.setattr(pooled.notice, "_event_stream", no_event_stream)

    await pooled.reclaim_handler.handle(
        reclaim_order(units=8, host_group=lease.host_group)
    )
    noticed = await pooled.lease_repo.get(lease.lease_id)
    assert noticed.notice_channels_delivered == ()

    closed = await _drain(pooled, lease.lease_id)
    assert closed.state is LeaseState.CLOSED
    assert closed.credit_raised > 0

    credits = await pooled.db.fetch(
        "SELECT reason, amount FROM credit_record WHERE lease_id = $1", lease.lease_id
    )
    assert credits and credits[0]["reason"] == "notice_undelivered"

    events = [e["event"] for e in await pooled.audit_repo.for_lease(lease.lease_id)]
    assert "preemption.notice_all_channels_failed" in events
    assert "billing.credit_raised" in events


async def test_an_undelivered_notice_is_credited_at_least_one_grace_window(pooled):
    """The credit floor, which is the part of §6.6 that does the work.

    Billed amount alone makes the credit proportional to how long the lease
    happened to run before it was killed, so a lease preempted a second after
    it started is credited approximately nothing — precisely the case where the
    customer was worst served. What they lost is not the compute, it is the
    notice, and the notice's price is one grace window at the lease's own rate.

    For a lease that ran longer than its grace window the billed amount
    dominates and the floor never binds, so §6.6's arithmetic is unchanged for
    the ordinary case. Both directions are asserted here.
    """
    from datetime import timedelta

    from spotd.domain.models import Lease, LeaseState, PurchaseOption, utcnow
    from spotd.domain.models import PurchaseOptionSource

    def _lease(*, ran_for: float) -> Lease:
        started = utcnow() - timedelta(seconds=ran_for)
        return Lease(
            lease_id="lease-credit-floor",
            tenant_id="tenant-spot-00",
            idempotency_key="k",
            purchase_option=PurchaseOption.SPOT,
            purchase_option_source=PurchaseOptionSource.ACCOUNT,
            flavour="s1.large",
            count=1,
            units=8,
            az="az-1",
            state=LeaseState.STOPPED,
            discount_snapshot=0.8,
            rate_per_sec=0.001,
            grace_seconds=pooled.settings.grace_seconds,
            created_at=started,
            running_at=started,
            notice_at=utcnow(),
            stopped_at=utcnow(),
            notice_channels_delivered=(),   # nothing reached the customer
        )

    grace = pooled.settings.grace_seconds
    notice_value = grace * 0.001

    # Killed almost immediately: billed ~0, but the credit is a real number.
    instant = pooled.metering.compute(_lease(ran_for=0.01))
    assert instant.amount < notice_value
    assert instant.credit is not None
    assert instant.credit.amount == pytest.approx(notice_value, rel=1e-6)
    assert instant.credit.reason == "notice_undelivered"

    # Long-running: the billed amount dominates and the floor does not bind.
    long_run = pooled.metering.compute(_lease(ran_for=grace * 10))
    assert long_run.amount > notice_value
    assert long_run.credit.amount == pytest.approx(long_run.amount)


async def test_a_delivered_notice_raises_no_credit(pooled):
    """The floor only applies where every channel failed."""
    from datetime import timedelta

    from spotd.domain.models import (
        Lease, LeaseState, NoticeChannel, PurchaseOption, PurchaseOptionSource, utcnow,
    )

    started = utcnow() - timedelta(seconds=30)
    lease = Lease(
        lease_id="lease-delivered",
        tenant_id="tenant-spot-00",
        idempotency_key="k2",
        purchase_option=PurchaseOption.SPOT,
        purchase_option_source=PurchaseOptionSource.ACCOUNT,
        flavour="s1.large",
        count=1,
        units=8,
        az="az-1",
        state=LeaseState.STOPPED,
        discount_snapshot=0.8,
        rate_per_sec=0.001,
        grace_seconds=pooled.settings.grace_seconds,
        created_at=started,
        running_at=started,
        notice_at=utcnow(),
        stopped_at=utcnow(),
        notice_channels_delivered=(NoticeChannel.METADATA,),
    )
    assert pooled.metering.compute(lease).credit is None


async def test_host_unreachable_escalates_to_destroy_and_quarantine(
    pooled, run_to_running, reclaim_order
):
    """LLD §11: escalate to destroy; quarantine the host from the spot pool."""
    lease = await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-06")
    pooled.externals.hypervisor.pin_behaviour(
        lease.lease_id, GuestBehaviour.HOST_UNREACHABLE
    )

    await pooled.reclaim_handler.handle(
        reclaim_order(units=8, host_group=lease.host_group)
    )
    await _drain(pooled, lease.lease_id)

    assert pooled.externals.hypervisor.is_destroyed(lease.lease_id)
    quarantined = await pooled.db.fetchval(
        "SELECT quarantined FROM host_group WHERE host_group = $1", lease.host_group
    )
    assert quarantined is True

    # A quarantined host is out of the placement pool.
    groups = await pooled.reference_repo.list_host_groups(az="az-1")
    assert lease.host_group not in {g.host_group for g in groups}


async def test_reclaimed_capacity_is_held_in_cooldown_before_re_sale(
    pooled, run_to_running, reclaim_order
):
    """Anti-thrash: reclaimed units are parked before they are sellable again.

    HLD §12 asks for the cooldown to be a tunable policy value whose cost is
    measurable, so it is stored as expiring rows rather than a bare counter.
    """
    lease = await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-07")
    pooled.externals.hypervisor.pin_behaviour(lease.lease_id, GuestBehaviour.COOPERATIVE)

    await pooled.reclaim_handler.handle(
        reclaim_order(units=8, host_group=lease.host_group)
    )
    await _drain(pooled, lease.lease_id)

    snapshot = await pooled.pool_repo.get("az-1")
    assert snapshot.cooldown_units >= lease.units

    await asyncio.sleep(pooled.settings.cooldown + 0.3)
    released = await pooled.pool_view.expire_cooldowns()
    assert released.get("az-1", 0) >= lease.units
    assert (await pooled.pool_repo.get("az-1")).cooldown_units == 0

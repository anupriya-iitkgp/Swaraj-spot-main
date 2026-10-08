"""Admission: entitlement, quota, idempotency, and the no-over-allocation guarantee.

Maps to the first block of the LLD §15 test matrix. Every test here protects a
stated property of the design, and the docstring says which — a test that does
not protect a stated property is not worth keeping.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from spotd.db.repositories import fingerprint
from spotd.domain.errors import (
    FlavourNotEligible,
    IdempotencyConflict,
    NoCapacity,
    NotEntitled,
    OutOfScope,
    QuotaExceeded,
    UnknownTenant,
)
from spotd.domain.models import PurchaseOption
from spotd.domain.state_machine import LeaseState

pytestmark = pytest.mark.asyncio


async def test_launch_reaches_running_and_places_on_a_host(pooled, run_to_running):
    """Happy path reaches RUNNING with instances and a host group (§4.1)."""
    lease = await run_to_running(pooled, flavour="s1.large", count=2)

    assert lease.state is LeaseState.RUNNING
    assert lease.host_group is not None
    assert len(lease.instance_ids) == 2
    assert lease.units == 16
    assert lease.running_at is not None
    # The discount was frozen at admission, not recomputed at fulfilment.
    assert 0.40 <= lease.discount_snapshot <= 0.80
    assert lease.rate_per_sec > 0


async def test_non_spot_account_is_rejected_403(pooled, launch):
    """Entitlement is re-checked, never inferred (§3.1).

    The gateway already classified this tenant; the Guard asks again.
    """
    with pytest.raises(OutOfScope):
        await launch(pooled, tenant="tenant-static-00")


async def test_unknown_tenant_is_rejected(pooled, launch):
    """Unknown tenant fails closed (§9) — never a guess, never a default."""
    with pytest.raises(UnknownTenant):
        await launch(pooled, tenant="tenant-does-not-exist")


async def test_inactive_tenant_is_indistinguishable_from_unknown(pooled, launch):
    """A deactivated account must not be told apart from a non-existent one.

    Saying which would let an unauthenticated prober enumerate tenant ids.
    """
    with pytest.raises(UnknownTenant):
        await launch(pooled, tenant="tenant-spot-33")


async def test_licence_bound_flavour_is_rejected_400_not_409(pooled, launch):
    """Licence-bound flavours are never sold as spot (§4.2).

    400, not 409: no amount of retrying will make a Windows image sellable as an
    interruptible instance, so telling the client to retry would be a lie.
    """
    with pytest.raises(FlavourNotEligible) as exc:
        await launch(pooled, flavour="w1.large")
    assert exc.value.status == 400
    assert exc.value.details["licence_bound"] is True


async def test_quota_exceeded_is_429_before_any_capacity_work(pooled, launch):
    """Per-tenant ceiling enforced before capacity work is done (§3.1).

    tenant-spot-00 is bronze: 32 units. Four s1.large leases fill it exactly.
    """
    for _ in range(4):
        await launch(pooled, flavour="s1.large", tenant="tenant-spot-00")

    before = (await pooled.pool_repo.get("az-1")).reserved_units
    with pytest.raises(QuotaExceeded) as exc:
        await launch(pooled, flavour="s1.large", tenant="tenant-spot-00")

    assert exc.value.status == 429
    assert exc.value.retry_after is not None
    # The rejection must not have touched the pool.
    assert (await pooled.pool_repo.get("az-1")).reserved_units == before


async def test_concurrency_cap_is_enforced_independently_of_units(pooled, launch):
    """A tenant can hit the lease-count cap while still under their unit quota."""
    # tenant-spot-00: bronze, 32 units, concurrency cap 8. s1.small is 2 units,
    # so the cap bites at 8 leases (16 units), well under the quota.
    for _ in range(8):
        await launch(pooled, flavour="s1.small", tenant="tenant-spot-00")
    with pytest.raises(QuotaExceeded) as exc:
        await launch(pooled, flavour="s1.small", tenant="tenant-spot-00")
    assert "concurrency" in exc.value.message


async def test_idempotent_retry_returns_the_original_lease(pooled, launch):
    """Exactly one lease per idempotency key (§6.1).

    HLD §11: a client retry after a network partition must not double-allocate.
    """
    first = await launch(pooled, key="retry-me")
    before = (await pooled.pool_repo.get("az-1")).reserved_units

    second = await launch(pooled, key="retry-me")

    assert second.lease_id == first.lease_id
    # The replay reserved nothing.
    assert (await pooled.pool_repo.get("az-1")).reserved_units == before


async def test_same_key_different_request_is_a_conflict(pooled):
    """Replaying the original lease for a different body would hand back
    something the caller did not ask for, so it is a rejection."""

    async def call(count: int) -> None:
        await pooled.market.launch(
            tenant_id="tenant-spot-01",
            flavour="s1.medium",
            count=count,
            az="az-1",
            idempotency_key="ambiguous",
            request_fingerprint=fingerprint(
                {"flavour": "s1.medium", "count": count, "az": "az-1",
                 "purchase_option": None}
            ),
            purchase_option=None,
        )

    await call(1)
    with pytest.raises(IdempotencyConflict):
        await call(4)


async def test_no_capacity_returns_retry_after_and_alternatives(pooled, launch):
    """409 is cheap and actionable (§7.1).

    HLD §7: "A 409 is normal traffic on a busy pool, not an incident." It still
    has to tell the client something they can act on.
    """
    # az-3 holds 60 units. Three 16-unit leases take 48, leaving 12 — not enough
    # for a fourth. A different tenant per lease, so quota is not what stops us.
    for n in range(3):
        await launch(pooled, flavour="s1.xlarge", az="az-3", tenant=f"tenant-spot-{n:02d}")

    with pytest.raises(NoCapacity) as exc:
        await launch(pooled, flavour="s1.xlarge", az="az-3", tenant="tenant-spot-05")

    assert exc.value.status == 409
    assert exc.value.retry_after == pooled.settings.retry_after
    # az-1 has room, so the client is told where to go instead of retrying blind.
    assert any(a["az"] == "az-1" for a in exc.value.details["alternatives"])
    assert "pool_staleness_seconds" in exc.value.details


async def test_concurrent_launches_never_over_allocate(pooled):
    """THE core admission guarantee (§6.1, §10.4).

    HLD §11 sets over-allocation to zero: "Guaranteed by atomic reserve. Any
    occurrence is a correctness bug, not a tuning issue."

    az-2 holds 120 units. Twenty concurrent 16-unit launches, from twenty
    different tenants so quota never fires first. Exactly seven can fit
    (7 x 16 = 112; an eighth would need 128). Not "about seven" — exactly seven.
    """
    total_units = (await pooled.pool_repo.get("az-2")).available_units
    assert total_units == 120

    async def attempt(n: int) -> bool:
        try:
            await pooled.market.launch(
                tenant_id=f"tenant-spot-{n:02d}",
                flavour="s1.xlarge",  # 16 units
                count=1,
                az="az-2",
                idempotency_key=f"race-{n}",
                request_fingerprint=fingerprint(
                    {"flavour": "s1.xlarge", "count": 1, "az": "az-2",
                     "purchase_option": None}
                ),
                purchase_option=None,
            )
            return True
        except NoCapacity:
            return False

    results = await asyncio.gather(*(attempt(n) for n in range(20)))
    winners = sum(results)

    assert winners == 7, f"expected exactly 7 winners, got {winners}"

    snapshot = await pooled.pool_repo.get("az-2")
    assert snapshot.reserved_units == 112
    assert snapshot.reserved_units <= snapshot.sellable_units

    # And the pool counter agrees with the leases that back it.
    for result in await pooled.pool_repo.reconcile():
        assert not result.over_allocated, f"over-allocation in {result.az}"


async def test_failed_provisioning_releases_the_reservation(pooled, launch, monkeypatch):
    """No capacity leak on a failed launch (§11).

    LLD §11: "Placement or provisioning fails -> Lease REJECTED, reservation
    released, nothing billed." The release is the part that matters: without it
    the leak is invisible until reconciliation notices the counter drifting.
    """
    from spotd.external.base import ExternalError

    lease = await launch(pooled)
    reserved_after_admit = (await pooled.pool_repo.get("az-1")).reserved_units

    async def boom(**kwargs):
        raise ExternalError("hypervisor", "create", "disk array unavailable")

    monkeypatch.setattr(pooled.externals.hypervisor, "create", boom)
    await pooled.lease_manager.fulfil(lease.lease_id)

    rejected = await pooled.lease_repo.get(lease.lease_id)
    assert rejected.state is LeaseState.REJECTED
    assert rejected.rejection_code.value == "provisioning_failed"
    assert rejected.billed_amount == 0

    after = (await pooled.pool_repo.get("az-1")).reserved_units
    assert after == reserved_after_admit - lease.units


async def test_purchase_option_on_the_request_wins_over_account_class(pooled):
    """HLD §12 risk 1: the purchase option is carried on the request.

    Account class stays the entitlement; the request field selects the option.
    The source is reported so a tenant mid-migration can see which of their
    calls are still routed by account class.
    """
    from spotd.domain.models import PurchaseOptionSource

    result = await pooled.market.launch(
        tenant_id="tenant-spot-02",
        flavour="s1.small",
        count=1,
        az="az-1",
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint="fp",
        purchase_option=PurchaseOption.SPOT,
    )
    assert result.lease.purchase_option_source is PurchaseOptionSource.REQUEST

    # Omitted: falls back to the account class, so the finalised diagram's
    # account-only routing keeps working unchanged.
    result = await pooled.market.launch(
        tenant_id="tenant-spot-02",
        flavour="s1.small",
        count=1,
        az="az-1",
        idempotency_key=uuid.uuid4().hex,
        request_fingerprint="fp2",
        purchase_option=None,
    )
    assert result.lease.purchase_option_source is PurchaseOptionSource.ACCOUNT

    # An entitled tenant asking for on-demand is out of scope, not forbidden.
    with pytest.raises(OutOfScope):
        await pooled.market.launch(
            tenant_id="tenant-spot-02",
            flavour="s1.small",
            count=1,
            az="az-1",
            idempotency_key=uuid.uuid4().hex,
            request_fingerprint="fp3",
            purchase_option=PurchaseOption.ON_DEMAND,
        )


async def test_pricing_is_frozen_at_lease_start(pooled, launch):
    """HLD §10: a later change to the published discount must not re-rate a
    running lease."""
    from spotd.domain.models import SellableFeed, utcnow

    lease = await launch(pooled, flavour="s1.large")
    original = lease.discount_snapshot

    # Collapse the surplus, which moves the live discount hard.
    await pooled.pool_repo.refresh(
        SellableFeed("az-1", 40, 0.95, utcnow(), 900),
        accepted=True, applied_units=40, degraded=False,
    )
    live, _ = pooled.pricing.discount_for(await pooled.pool_repo.get("az-1"))
    assert live != original

    unchanged = await pooled.lease_repo.get(lease.lease_id)
    assert unchanged.discount_snapshot == original
    assert unchanged.rate_per_sec == lease.rate_per_sec

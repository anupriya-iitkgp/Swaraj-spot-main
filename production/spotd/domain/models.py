"""Domain types.

These are the shapes that cross component boundaries. They are plain frozen
dataclasses rather than ORM entities on purpose: the Spot Lease Manager is the
single writer of lease state (HLD §6), and an object that can lazily write
itself back to the database quietly breaks that rule. Everything here is inert
data; all persistence goes through `spotd.db.repositories`.

`Lease` mirrors the record in HLD §10 field for field, including the timestamps
that the design calls out as evidence: notice_at -> stopped_at proves the grace
window was honoured, stopped_at -> closed_at proves the teardown budget. Those
two spans are what a preemption dispute is settled from, so they are first-class
columns rather than something reconstructed from logs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from .state_machine import BILLABLE, LeaseState

__all__ = [
    "AccountClass",
    "PurchaseOption",
    "PurchaseOptionSource",
    "PreemptionReason",
    "NoticeChannel",
    "ReclaimOrderState",
    "RejectionCode",
    "Tenant",
    "Flavour",
    "HostGroup",
    "SellableFeed",
    "PoolSnapshot",
    "Lease",
    "ReclaimOrder",
    "NoticeReceipt",
    "UsageRecord",
    "CreditRecord",
    "InterruptionRate",
    "utcnow",
    "new_id",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


# --------------------------------------------------------------------------
# enumerations
# --------------------------------------------------------------------------

class AccountClass(StrEnum):
    """HLD §2. STATIC and DYNAMIC leave this design entirely."""

    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"
    SPOT = "SPOT"


class PurchaseOption(StrEnum):
    """Carried on the launch request (HLD §12, risk 1).

    The HLD flags that binding the class to the account stops a tenant running
    reserved, on-demand and spot side by side without three separate accounts,
    splitting their billing, IAM and networking. The accepted resolution is to
    keep account-level *entitlement* but route on a request field.
    """

    SPOT = "spot"
    ON_DEMAND = "on_demand"


class PurchaseOptionSource(StrEnum):
    """Where the effective purchase option came from — echoed in the response.

    A tenant migrating to per-request routing needs to see which of their calls
    are still being routed by account class, so this is reported, not inferred.
    """

    REQUEST = "request"
    ACCOUNT = "account"


class PreemptionReason(StrEnum):
    CAPACITY_RECLAIM = "capacity_reclaim"
    CANCELLED_IN_FLIGHT = "cancelled_in_flight"
    CUSTOMER_RELEASE = "customer_release"
    HOST_QUARANTINE = "host_quarantine"


class NoticeChannel(StrEnum):
    """HLD §6: fan-out over three independent channels, with proof of delivery.

    HLD §12 warns that "three channels do not help if they share a failure
    mode", so these must be genuinely independent infrastructure paths — the
    guest-local metadata service, an outbound webhook, and the tenant event
    stream.
    """

    METADATA = "metadata"
    WEBHOOK = "webhook"
    EVENT_STREAM = "event_stream"


class ReclaimOrderState(StrEnum):
    RECEIVED = "RECEIVED"
    SHRINKING = "SHRINKING"
    SELECTING = "SELECTING"
    NOTICED = "NOTICED"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class RejectionCode(StrEnum):
    NO_CAPACITY = "no_capacity"
    QUOTA_EXCEEDED = "quota_exceeded"
    NOT_ENTITLED = "not_entitled"
    FLAVOUR_NOT_ELIGIBLE = "flavour_not_eligible"
    PLACEMENT_FAILED = "placement_failed"
    PROVISIONING_FAILED = "provisioning_failed"


# --------------------------------------------------------------------------
# reference data
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Tenant:
    tenant_id: str
    name: str
    account_class: AccountClass
    #: Per-tenant vCPU ceiling for spot. Falls back to SPOT_TENANT_QUOTA.
    spot_quota_units: int
    #: Max simultaneous spot leases, independent of unit count.
    concurrency_cap: int
    webhook_url: str | None = None
    contract_tier: str = "standard"
    active: bool = True

    @property
    def spot_entitled(self) -> bool:
        return self.active and self.account_class is AccountClass.SPOT


@dataclass(frozen=True, slots=True)
class Flavour:
    name: str
    vcpu: int
    memory_gb: int
    #: False for licence-bound flavours (Windows, Oracle) whose licensing terms
    #: make an interruptible instance unsellable. The Guard rejects these with
    #: 400 rather than 409 — no amount of retrying will make them available.
    spot_eligible: bool
    licence_bound: bool = False
    family: str = "general"

    def units(self, count: int) -> int:
        """Capacity is accounted in vCPU units; a lease's footprint is count x vcpu."""
        return self.vcpu * count


@dataclass(frozen=True, slots=True)
class HostGroup:
    host_group: str
    az: str
    total_units: int
    #: A host whose agent proved unreachable is quarantined out of the spot pool
    #: rather than repeatedly selected and repeatedly failed (LLD §11).
    quarantined: bool = False


# --------------------------------------------------------------------------
# the forecast feed — an input, not a fact
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SellableFeed:
    """One publication from Forecast & Headroom (HLD edge 19).

    HLD §12 is blunt about this: "The sellable-spot number is an input, not a
    fact." If forecasting hands over an optimistic number this subsystem will
    faithfully sell capacity that does not exist. The mitigation the design
    requires is a *confidence signal alongside the number*, and degradation to a
    conservative floor when the feed is stale or confidence is low — never
    extrapolation of the last known value.
    """

    az: str
    units: int
    confidence: float
    published_at: datetime
    horizon_seconds: float

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.published_at).total_seconds()

    def is_stale(self, control_cycle: float, max_cycles: float, now: datetime | None = None) -> bool:
        return self.age_seconds(now) > control_cycle * max_cycles

    def is_trustworthy(
        self, control_cycle: float, max_cycles: float, min_confidence: float,
        now: datetime | None = None,
    ) -> bool:
        return (
            self.confidence >= min_confidence
            and not self.is_stale(control_cycle, max_cycles, now)
        )


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    """The Spot Pool View's read model for one AZ (HLD §6).

    "A cached projection of sellable spot per flavour and AZ, refreshed each
    control cycle. Must not be treated as authoritative — it is stale by design."

    Capacity is fungible vCPU within an AZ, so the pool is keyed by AZ and the
    per-flavour numbers customers see are a projection of `available_units`
    divided by the flavour's vCPU count. Keying the stored pool per flavour
    would double-count shared capacity: reserving for one flavour would have to
    decrement every other flavour's row.
    """

    az: str
    sellable_units: int
    reserved_units: int
    cooldown_units: int
    confidence: float
    published_at: datetime | None
    degraded: bool
    cycle_seq: int
    updated_at: datetime

    @property
    def available_units(self) -> int:
        return max(0, self.sellable_units - self.reserved_units - self.cooldown_units)

    @property
    def utilisation(self) -> float:
        return self.reserved_units / self.sellable_units if self.sellable_units else 0.0

    def staleness_seconds(self, now: datetime | None = None) -> float:
        if self.published_at is None:
            return float("inf")
        return ((now or utcnow()) - self.published_at).total_seconds()

    def capacity_for(self, flavour: Flavour) -> int:
        """How many whole instances of this flavour the pool could currently sell."""
        if not flavour.spot_eligible or flavour.vcpu <= 0:
            return 0
        return self.available_units // flavour.vcpu


# --------------------------------------------------------------------------
# the lease — HLD §10
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Lease:
    lease_id: str
    tenant_id: str
    idempotency_key: str
    purchase_option: PurchaseOption
    purchase_option_source: PurchaseOptionSource
    flavour: str
    count: int
    units: int
    az: str
    state: LeaseState
    #: Optimistic-concurrency token. Every write is conditional on it, which is
    #: what replaces the in-process per-lease lock once there is more than one
    #: replica (LLD §16).
    version: int = 0
    host_group: str | None = None
    instance_ids: tuple[str, ...] = ()

    # -- rating: frozen at lease start (HLD §10) ---------------------------
    #: "A later change to the published discount must not re-rate a running
    #: lease." The snapshot is what makes that enforceable rather than aspirational.
    discount_snapshot: float = 0.0
    rate_per_sec: float = 0.0
    grace_seconds: float = 120.0

    # -- evidence timestamps ------------------------------------------------
    created_at: datetime = field(default_factory=utcnow)
    admitted_at: datetime | None = None
    provisioning_at: datetime | None = None
    running_at: datetime | None = None
    notice_at: datetime | None = None
    #: Absolute deadline the reaper compares against. Persisted rather than
    #: derived so a restart can reconstruct it without the in-memory timer that
    #: LLD §12.3 identifies as the stranding bug.
    force_stop_deadline: datetime | None = None
    stopped_at: datetime | None = None
    closed_at: datetime | None = None

    # -- preemption traceability -------------------------------------------
    preemption_reason: PreemptionReason | None = None
    reclaim_order_id: str | None = None
    forced_stop: bool = False
    #: Which channels actually succeeded. Empty on a preempted lease means the
    #: customer was never warned — HLD §10 makes that an automatic credit.
    notice_channels_delivered: tuple[NoticeChannel, ...] = ()

    # -- billing ------------------------------------------------------------
    grace_seconds_excluded: float = 0.0
    credit_raised: float = 0.0
    billed_seconds: float = 0.0
    billed_amount: float = 0.0

    rejection_code: RejectionCode | None = None
    rejection_detail: str | None = None
    teardown_stalled: bool = False
    updated_at: datetime = field(default_factory=utcnow)

    # ----------------------------------------------------------------------
    @property
    def is_billable_now(self) -> bool:
        return self.state in BILLABLE

    @property
    def ran(self) -> bool:
        """False for a lease cancelled in flight — the "no charge" predicate."""
        return self.running_at is not None

    @property
    def grace_window_seconds(self) -> float | None:
        """notice_at -> stopped_at. The proof that the promised window was honoured."""
        if self.notice_at is None or self.stopped_at is None:
            return None
        return (self.stopped_at - self.notice_at).total_seconds()

    @property
    def teardown_window_seconds(self) -> float | None:
        """stopped_at -> closed_at. The proof that the teardown budget was met."""
        if self.stopped_at is None or self.closed_at is None:
            return None
        return (self.closed_at - self.stopped_at).total_seconds()

    @property
    def reclaim_window_seconds(self) -> float | None:
        """notice_at -> closed_at, the number the 120 s SLO is measured against."""
        if self.notice_at is None or self.closed_at is None:
            return None
        return (self.closed_at - self.notice_at).total_seconds()

    @property
    def notice_delivered(self) -> bool:
        return bool(self.notice_channels_delivered)

    def with_state(self, state: LeaseState, **changes: Any) -> "Lease":
        return replace(self, state=state, updated_at=utcnow(), **changes)

    def deadline_from(self, now: datetime | None = None) -> datetime:
        return (now or utcnow()) + timedelta(seconds=self.grace_seconds)


# --------------------------------------------------------------------------
# reclaim
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReclaimOrder:
    """HLD edge 18 — an order from the capacity side, not a decision made here.

    "This subsystem does not decide how much capacity exists, nor when capacity
    must be taken back." The order arrives with N, a host group and a deadline;
    all this subsystem owns is faithful execution.
    """

    order_id: str
    az: str
    units: int
    host_group: str | None
    deadline: datetime
    reason: str
    requested_by: str
    state: ReclaimOrderState = ReclaimOrderState.RECEIVED
    received_at: datetime = field(default_factory=utcnow)
    units_selected: int = 0
    leases_selected: tuple[str, ...] = ()
    completed_at: datetime | None = None
    detail: str | None = None

    @property
    def seconds_to_deadline(self) -> float:
        return (self.deadline - utcnow()).total_seconds()


@dataclass(frozen=True, slots=True)
class NoticeReceipt:
    """Proof of delivery for one channel — HLD §6 forbids failing silently."""

    lease_id: str
    channel: NoticeChannel
    delivered: bool
    attempt: int
    at: datetime
    error: str | None = None
    latency_ms: float | None = None


# --------------------------------------------------------------------------
# rating
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One rated window. Unique on (lease_id, window_start) so a replayed
    submission is absorbed by the billing system rather than double-charged."""

    lease_id: str
    tenant_id: str
    window_start: datetime
    window_end: datetime
    units: int
    billable_seconds: float
    rate_per_sec: float
    discount: float
    amount: float
    grace_seconds_excluded: float = 0.0

    @property
    def idempotency_key(self) -> str:
        return f"{self.lease_id}:{self.window_start.isoformat()}"


@dataclass(frozen=True, slots=True)
class CreditRecord:
    credit_id: str
    lease_id: str
    tenant_id: str
    reason: str
    amount: float
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class InterruptionRate:
    """Published per flavour and AZ (HLD §11: refreshed at least hourly).

    "Customers cannot size spot workloads without it." The per-tenant variant of
    the same measure is the fairness signal from HLD §12 — if the spread across
    tenants widens, victim selection needs a fairness term.
    """

    flavour: str
    az: str
    tenant_id: str | None
    window_start: datetime
    window_end: datetime
    preemptions: int
    lease_hours: float
    rate: float
    sample_size: int

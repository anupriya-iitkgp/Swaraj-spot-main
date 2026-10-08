"""Domain model: account classes, flavours, the spot lease and its state machine.

The lease — not the instance — is the unit of billing and of preemption.
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------
# Account class (supplied by the external Account Service, edge 1)
# --------------------------------------------------------------------------
class AccountClass(str, enum.Enum):
    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"
    SPOT = "SPOT"


class PurchaseOption(str, enum.Enum):
    """Recommended extension from the HLD risk table.

    Account class stays an *entitlement*; the request carries the purchase
    option. A SPOT-entitled tenant can then still ask for on-demand.
    """

    SPOT = "spot"
    ON_DEMAND = "on-demand"


# --------------------------------------------------------------------------
# Flavours
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Flavour:
    name: str
    vcpu: int
    ram_gb: int
    on_demand_rate_per_hour: float
    spot_eligible: bool = True
    disk_gb: int = 0

    @property
    def units(self) -> int:
        """Capacity is accounted in vCPU units throughout."""
        return self.vcpu


FLAVOURS: dict[str, Flavour] = {
    "s1.small": Flavour("s1.small", 2, 4, 1.20, disk_gb=50),
    "s1.medium": Flavour("s1.medium", 4, 8, 2.40, disk_gb=100),
    "s1.large": Flavour("s1.large", 8, 16, 4.80, disk_gb=200),
    # Licence-bound flavour: never sold as spot (Eligibility Guard, edge 5).
    "db1.xlarge": Flavour("db1.xlarge", 16, 64, 14.00, spot_eligible=False, disk_gb=500),
}

AVAILABILITY_ZONES = ("az-1", "az-2")


# --------------------------------------------------------------------------
# Lease state machine (diagram section 3)
# --------------------------------------------------------------------------
class LeaseState(str, enum.Enum):
    REQUESTED = "REQUESTED"
    ADMITTED = "ADMITTED"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    NOTICE_ISSUED = "NOTICE_ISSUED"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


TERMINAL_STATES = frozenset({LeaseState.CLOSED, LeaseState.REJECTED})

#: States in which a reclaim order cancels the lease outright — no termination
#: notice is issued and nothing is billed, because no instance ever ran.
CANCELLABLE_STATES = frozenset(
    {LeaseState.REQUESTED, LeaseState.ADMITTED, LeaseState.PROVISIONING}
)

#: States that hold real capacity and can therefore be preempted with notice.
PREEMPTIBLE_STATES = frozenset({LeaseState.RUNNING})

ALLOWED_TRANSITIONS: dict[LeaseState, frozenset[LeaseState]] = {
    LeaseState.REQUESTED: frozenset({LeaseState.ADMITTED, LeaseState.REJECTED}),
    LeaseState.ADMITTED: frozenset(
        {LeaseState.PROVISIONING, LeaseState.REJECTED, LeaseState.CLOSED}
    ),
    LeaseState.PROVISIONING: frozenset(
        {LeaseState.RUNNING, LeaseState.REJECTED, LeaseState.CLOSED}
    ),
    LeaseState.RUNNING: frozenset({LeaseState.NOTICE_ISSUED, LeaseState.STOPPED}),
    LeaseState.NOTICE_ISSUED: frozenset({LeaseState.DRAINING, LeaseState.STOPPED}),
    LeaseState.DRAINING: frozenset({LeaseState.STOPPED}),
    LeaseState.STOPPED: frozenset({LeaseState.CLOSED}),
    LeaseState.CLOSED: frozenset(),
    LeaseState.REJECTED: frozenset(),
}


class IllegalTransition(Exception):
    def __init__(self, lease_id: str, src: LeaseState, dst: LeaseState):
        super().__init__(f"lease {lease_id}: {src.value} -> {dst.value} is not allowed")
        self.src, self.dst = src, dst


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Lease:
    """The spot lease record (HLD §10)."""

    lease_id: str
    tenant_id: str
    flavour: str
    count: int
    az: str
    units: int
    discount_snapshot: float
    rate_per_sec: float
    idempotency_key: Optional[str] = None
    state: LeaseState = LeaseState.REQUESTED
    host_group: Optional[str] = None
    instance_ids: list[str] = field(default_factory=list)

    # timestamps — the evidence trail
    created_at: float = field(default_factory=time.time)
    admitted_at: Optional[float] = None
    running_at: Optional[float] = None
    notice_at: Optional[float] = None
    stopped_at: Optional[float] = None
    closed_at: Optional[float] = None

    # stateful spot: save the task on preemption instead of destroying it,
    # and resume from previously saved machine state
    persist: bool = False
    resume_vmids: list[int] = field(default_factory=list)

    # preemption
    preemption_reason: Optional[str] = None
    reclaim_order_id: Optional[str] = None
    notice_channels_delivered: list[str] = field(default_factory=list)
    forced_stop: bool = False

    # billing
    grace_seconds_excluded: float = 0.0
    billed_seconds: float = 0.0
    amount: float = 0.0
    credit_raised: float = 0.0

    rejection_reason: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict:
        return {
            "lease_id": self.lease_id,
            "tenant_id": self.tenant_id,
            "flavour": self.flavour,
            "count": self.count,
            "az": self.az,
            "units": self.units,
            "state": self.state.value,
            "host_group": self.host_group,
            "instance_ids": list(self.instance_ids),
            "persist": self.persist,
            "resumed": bool(self.resume_vmids),
            "discount_snapshot": round(self.discount_snapshot, 4),
            "rate_per_sec": round(self.rate_per_sec, 8),
            "timestamps": {
                "created_at": self.created_at,
                "admitted_at": self.admitted_at,
                "running_at": self.running_at,
                "notice_at": self.notice_at,
                "stopped_at": self.stopped_at,
                "closed_at": self.closed_at,
            },
            "preemption_reason": self.preemption_reason,
            "reclaim_order_id": self.reclaim_order_id,
            "notice_channels_delivered": list(self.notice_channels_delivered),
            "forced_stop": self.forced_stop,
            "billing": {
                "billed_seconds": round(self.billed_seconds, 3),
                "grace_seconds_excluded": round(self.grace_seconds_excluded, 3),
                "amount": round(self.amount, 6),
                "credit_raised": round(self.credit_raised, 6),
            },
            "rejection_reason": self.rejection_reason,
        }


@dataclass
class ReclaimOrder:
    """Inbound order from the capacity side (edge 18).

    Carries a *deadline*, not just a quantity — without it the grace timer has
    nothing to work against.
    """

    order_id: str
    units: int
    host_group: Optional[str]
    az: str
    deadline: float
    reason: str = "headroom-rise"
    created_at: float = field(default_factory=time.time)

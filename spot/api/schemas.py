from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class LaunchRequest(BaseModel):
    flavour: str = Field(examples=["s1.medium"])
    count: int = Field(default=1, ge=1, le=64)
    az: str = Field(default="az-1", examples=["az-1"])
    #: Recommended HLD extension: account class is an ENTITLEMENT, the request
    #: carries the purchase option. Kept optional so the account-only model in
    #: the finalised diagram still works unchanged.
    purchase_option: str = Field(default="spot")
    #: Simulation only: seconds the guest needs to drain. None = ignores the
    #: notice and must be force-stopped by the grace timer.
    drain_seconds: Optional[float] = 1.0
    #: Stateful spot: if preempted, hibernate the machine state to storage
    #: instead of destroying it — the task can be resumed later.
    persist: bool = False


class ReclaimRequest(BaseModel):
    units: int = Field(ge=1)
    az: str = "az-1"
    host_group: Optional[str] = None
    deadline: Optional[float] = None
    reason: str = "headroom-rise"


class HeadroomRequest(BaseModel):
    az: str = "az-1"
    units: int = Field(ge=0)


class PricingRequest(BaseModel):
    """Operator pricing control.

    ``manual`` pins the discount for a bounded window, after which the
    demand-driven curve resumes on its own; ``auto`` reverts immediately.
    """
    mode: str = Field(pattern="^(auto|manual)$", examples=["manual"])
    #: manual only: the pinned discount, e.g. 0.65 = 65% off on-demand.
    discount: Optional[float] = Field(default=None, ge=0.0, le=0.95)
    #: manual only: how long the pin lasts. 60 s to 24 h.
    duration_seconds: Optional[float] = Field(default=None, ge=60, le=86400)
    #: manual only: restrict to one AZ; None applies everywhere.
    az: Optional[str] = None


class RateRequest(BaseModel):
    """Operator rate card: set the on-demand price of one node type.

    Spot pricing derives from it (rate × (1 − discount)); running leases keep
    the rate snapshotted at admission and are never re-rated.
    """
    flavour: str = Field(examples=["s1.medium"])
    rate_per_hour: float = Field(gt=0, le=10000)


class FlavourRequest(BaseModel):
    """Operator rate card: publish a new node type with its specs and price."""
    name: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    vcpu: int = Field(ge=1, le=128)
    ram_gb: int = Field(ge=1, le=1024)
    disk_gb: int = Field(default=0, ge=0, le=10000)
    rate_per_hour: float = Field(gt=0, le=10000)
    spot_eligible: bool = True


class WebhookRequest(BaseModel):
    """Simulation only: stands in for the tenant's own HTTP endpoint.

    Notices pushed on channel 2 land in an in-process inbox you can read back,
    so the third notice channel is exercisable without a real listener.
    """
    tenant_id: str = Field(examples=["tenant-spot-a"])

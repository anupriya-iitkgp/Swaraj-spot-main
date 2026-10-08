"""Request and response models for the public and internal surfaces.

The response models are more verbose than the minimum, on purpose. Two of them
carry fields the design specifically asks to be *visible* rather than internal:

  * `LaunchResponse.purchase_option_source` — HLD §12's risk 1 is that binding
    the class to the account stops a tenant running reserved, on-demand and spot
    side by side. The accepted fix carries the option on the request; a tenant
    migrating to it needs to see which of their calls are still being routed by
    account class.

  * `LeaseResponse.grace_window_seconds` / `teardown_window_seconds` — HLD §10:
    "notice_at -> stopped_at proves the grace window; stopped_at -> closed_at
    proves the teardown budget." A customer disputing a preemption should be
    able to read the proof from the API rather than open a support ticket to
    have someone read it from a log.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..domain.models import Lease, PurchaseOption

__all__ = [
    "LaunchRequest",
    "LaunchResponse",
    "LeaseResponse",
    "InventoryResponse",
    "ReclaimRequest",
    "ReclaimResponse",
    "CleanExitRequest",
    "ErrorResponse",
    "HeadroomOverrideRequest",
    "lease_to_response",
]


class LaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flavour: str = Field(..., min_length=1, max_length=64)
    count: int = Field(1, ge=1, le=256)
    az: str = Field(..., min_length=1, max_length=32)
    #: HLD §12, risk 1. Optional: when absent, the account class decides, which
    #: keeps the finalised diagram's account-only routing working unchanged.
    purchase_option: PurchaseOption | None = None

    @field_validator("flavour", "az")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    def fingerprint_fields(self) -> dict[str, Any]:
        """The fields that make two requests the same request.

        Used for the idempotency fingerprint. Only what changes what gets
        allocated — a re-ordered JSON body or an added client annotation must
        not make an honest retry look like a different request.
        """
        return {
            "flavour": self.flavour,
            "count": self.count,
            "az": self.az,
            "purchase_option": self.purchase_option.value
            if self.purchase_option
            else None,
        }


class LeaseResponse(BaseModel):
    lease_id: str
    tenant_id: str
    state: str
    flavour: str
    count: int
    units: int
    az: str
    host_group: str | None
    instance_ids: list[str]

    purchase_option: str
    purchase_option_source: str

    discount: float
    rate_per_sec: float
    price_per_hour: float
    grace_seconds: float

    created_at: datetime
    admitted_at: datetime | None
    running_at: datetime | None
    notice_at: datetime | None
    force_stop_deadline: datetime | None
    stopped_at: datetime | None
    closed_at: datetime | None

    preemption_reason: str | None
    reclaim_order_id: str | None
    forced_stop: bool
    notice_channels_delivered: list[str]

    # -- HLD §10 evidence, exposed to the customer --------------------
    grace_window_seconds: float | None = Field(
        None, description="notice_at -> stopped_at: proof the grace window was honoured"
    )
    teardown_window_seconds: float | None = Field(
        None, description="stopped_at -> closed_at: proof of the teardown budget"
    )

    billed_seconds: float
    billed_amount: float
    grace_seconds_excluded: float
    credit_raised: float

    rejection_code: str | None
    rejection_detail: str | None
    teardown_stalled: bool


class LaunchResponse(BaseModel):
    lease: LeaseResponse
    #: True when this response replayed an earlier identical request.
    idempotent_replay: bool = False
    message: str


class InventoryEntryResponse(BaseModel):
    az: str
    flavour: str
    vcpu: int
    memory_gb: int
    available_instances: int
    available_units: int
    discount: float
    price_per_hour: float
    list_price_per_hour: float
    interruption_rate_per_lease_hour: float | None
    interruption_sample_hours: float | None
    pool_staleness_seconds: float
    #: True when the forecast feed is stale or low-confidence and the pool has
    #: been degraded to a conservative floor. Published rather than hidden: a
    #: customer sizing a burst deserves to know the number is defensive.
    degraded: bool


class InventoryResponse(BaseModel):
    pools: list[InventoryEntryResponse]
    control_cycle_seconds: float
    note: str = (
        "Availability is a projection refreshed each control cycle and is stale "
        "by design. A launch may still return 409; that is normal on a busy pool."
    )


class ReclaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Idempotency is per order_id (LLD §16). A replayed order must not
    #: double-preempt, so the caller supplies a stable id.
    order_id: str = Field(..., min_length=1, max_length=128)
    az: str = Field(..., min_length=1, max_length=32)
    units: int = Field(..., ge=1)
    host_group: str | None = None
    #: Optional shape hint — narrows victim selection within the host set.
    flavour: str | None = None
    deadline_seconds: float = Field(120.0, gt=0, le=3600)
    reason: str = "forecast_headroom"


class ReclaimResponse(BaseModel):
    order_id: str
    az: str
    host_group: str | None
    units_requested: int
    units_shrunk_from_pool: int
    units_selected: int
    leases_noticed: list[str]
    state: str
    partial: bool
    replayed: bool
    detail: str
    deadline: str


class CleanExitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Reported by the host agent when the guest honoured the notice.
    reported_by: str = "host-agent"


class HeadroomOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    az: str
    #: null clears the override and returns the AZ to the synthetic trace.
    units: int | None = Field(None, ge=0)


class ErrorResponse(BaseModel):
    error: dict[str, Any]


def lease_to_response(lease: Lease) -> LeaseResponse:
    return LeaseResponse(
        lease_id=lease.lease_id,
        tenant_id=lease.tenant_id,
        state=lease.state.value,
        flavour=lease.flavour,
        count=lease.count,
        units=lease.units,
        az=lease.az,
        host_group=lease.host_group,
        instance_ids=list(lease.instance_ids),
        purchase_option=lease.purchase_option.value,
        purchase_option_source=lease.purchase_option_source.value,
        discount=lease.discount_snapshot,
        rate_per_sec=lease.rate_per_sec,
        price_per_hour=round(lease.rate_per_sec * 3600, 6),
        grace_seconds=lease.grace_seconds,
        created_at=lease.created_at,
        admitted_at=lease.admitted_at,
        running_at=lease.running_at,
        notice_at=lease.notice_at,
        force_stop_deadline=lease.force_stop_deadline,
        stopped_at=lease.stopped_at,
        closed_at=lease.closed_at,
        preemption_reason=(
            lease.preemption_reason.value if lease.preemption_reason else None
        ),
        reclaim_order_id=lease.reclaim_order_id,
        forced_stop=lease.forced_stop,
        notice_channels_delivered=[c.value for c in lease.notice_channels_delivered],
        grace_window_seconds=(
            round(lease.grace_window_seconds, 3)
            if lease.grace_window_seconds is not None
            else None
        ),
        teardown_window_seconds=(
            round(lease.teardown_window_seconds, 3)
            if lease.teardown_window_seconds is not None
            else None
        ),
        billed_seconds=lease.billed_seconds,
        billed_amount=lease.billed_amount,
        grace_seconds_excluded=lease.grace_seconds_excluded,
        credit_raised=lease.credit_raised,
        rejection_code=lease.rejection_code.value if lease.rejection_code else None,
        rejection_detail=lease.rejection_detail,
        teardown_stalled=lease.teardown_stalled,
    )

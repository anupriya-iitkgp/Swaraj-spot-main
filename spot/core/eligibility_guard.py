"""Eligibility & Quota Guard (edge 5).

Re-verifies account class = SPOT (never trusts the caller), tenant spot quota,
per-tenant concurrency cap, and whether the flavour is spot-eligible at all.

Fails fast: every rejection here is cheap and happens before any capacity work.
"""
from __future__ import annotations

import logging

from ..config import CONFIG
from ..domain.errors import BadRequest, FlavourNotEligible, NotEntitled, QuotaExceeded
from ..domain.models import FLAVOURS, AccountClass, Flavour
from ..external.account_service import AccountService

log = logging.getLogger("spot.eligibility")


class EligibilityGuard:
    def __init__(self, accounts: AccountService, lease_manager=None):
        self._accounts = accounts
        self._lease_manager = lease_manager  # set late; avoids a circular import
        self.quotas: dict[str, int] = {}

    def bind_lease_manager(self, lease_manager) -> None:
        self._lease_manager = lease_manager

    def quota_for(self, tenant_id: str) -> int:
        return self.quotas.get(tenant_id, CONFIG.default_tenant_quota_units)

    async def validate(
        self, *, tenant_id: str, flavour_name: str, count: int, az: str
    ) -> Flavour:
        """Edge 5 — returns the Flavour or raises a typed rejection."""
        if count < 1:
            raise BadRequest("count must be >= 1")

        # 1. entitlement — re-read from the Account Service, never trust input
        account_class = await self._accounts.get_account_class(tenant_id)
        if account_class is None:
            raise NotEntitled(f"unknown tenant {tenant_id}")
        if account_class is not AccountClass.SPOT:
            raise NotEntitled(
                f"account class {account_class.value} is not entitled to spot; "
                "use the on-demand path",
                account_class=account_class.value,
                on_demand_path="/v1/instances",
            )

        # 2. flavour eligibility
        flavour = FLAVOURS.get(flavour_name)
        if flavour is None:
            raise BadRequest(
                f"unknown flavour {flavour_name}",
                spot_eligible_flavours=[f.name for f in FLAVOURS.values() if f.spot_eligible],
            )
        if not flavour.spot_eligible:
            raise FlavourNotEligible(
                f"{flavour_name} is not available as spot",
                spot_eligible_flavours=[f.name for f in FLAVOURS.values() if f.spot_eligible],
            )

        # 3. quota / concurrency cap
        requested_units = flavour.units * count
        in_use = self._lease_manager.tenant_units_in_use(tenant_id) if self._lease_manager else 0
        quota = self.quota_for(tenant_id)
        if in_use + requested_units > quota:
            raise QuotaExceeded(
                f"spot quota exceeded for {tenant_id}",
                quota_units=quota,
                units_in_use=in_use,
                units_requested=requested_units,
            )

        log.info("edge 5   eligibility ok: %s %s x%d in %s", tenant_id, flavour_name, count, az)
        return flavour

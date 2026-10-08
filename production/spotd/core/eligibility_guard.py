"""Eligibility & Quota Guard — edge 5.

HLD §6:

    Owns: Re-verifying spot entitlement, tenant quota, concurrency cap, flavour
          eligibility.
    Must not do: Trust a class supplied by the caller.
    Key operations: validate(tenant, flavour, count)

The "must not" is the whole point of the component. The gateway already looked
up the account class in order to route (edge 1), and it would be cheaper to pass
that finding along. The Guard re-derives it instead, from the Account Service,
because a class that arrived as a parameter is a class an attacker can supply.

Ordering here is deliberate and is a cost decision as much as a correctness one.
Entitlement, then flavour, then quota, then concurrency — cheapest and most
absolute first. HLD §11 wants a fast rejection ("a slow reject is worse than a
fast one"), and none of these checks should ever reach the capacity work if an
earlier one already settles it.

The purchase-option decision (HLD §12, risk 1) is resolved here too. Account
class remains the *entitlement*; the request field selects the purchase option.
A tenant entitled to spot can therefore run on-demand and spot side by side
without a second account, which is the problem the HLD flags with the
account-only design.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..domain.errors import (
    FlavourNotEligible,
    InvalidRequest,
    NotEntitled,
    OutOfScope,
    QuotaExceeded,
    UnknownTenant,
)
from ..domain.models import (
    AccountClass,
    Flavour,
    PurchaseOption,
    PurchaseOptionSource,
    Tenant,
)
from ..external.account_service import AccountService
from ..logging import edge, get_logger

log = get_logger(__name__)

__all__ = ["EligibilityGuard", "Validated"]


@dataclass(frozen=True, slots=True)
class Validated:
    """Everything downstream needs, proven rather than asserted."""

    tenant: Tenant
    account_class: AccountClass
    flavour: Flavour
    count: int
    units: int
    purchase_option: PurchaseOption
    purchase_option_source: PurchaseOptionSource
    quota_units: int
    units_in_use: int
    leases_in_use: int

    @property
    def quota_headroom(self) -> int:
        return max(0, self.quota_units - self.units_in_use)


class EligibilityGuard:
    def __init__(
        self,
        *,
        settings: Settings,
        accounts: AccountService,
        reference: object,
        leases: object,
    ) -> None:
        self._settings = settings
        self._accounts = accounts
        self._reference = reference
        self._leases = leases

    async def validate(
        self,
        *,
        tenant_id: str,
        flavour_name: str,
        count: int,
        az: str,
        requested_purchase_option: PurchaseOption | None,
    ) -> Validated:
        """Run every entitlement check. Raises the specific typed rejection."""
        settings = self._settings

        # -- shape -------------------------------------------------------
        if count < 1:
            raise InvalidRequest(f"count must be at least 1, got {count}")
        if az not in settings.availability_zones:
            raise InvalidRequest(
                f"unknown availability zone {az!r}",
                details={"known_zones": list(settings.availability_zones)},
            )

        # -- entitlement: re-derived, never taken from the caller ---------
        account_class = await self._accounts.get_account_class(tenant_id)
        if account_class is None:
            # LLD §9: unknown tenant returns None, never a guess. Fail closed.
            raise UnknownTenant(
                "the account service does not recognise this tenant, or could "
                "not be reached; spot access is refused rather than assumed"
            )

        tenant = await self._reference.get_tenant(tenant_id)  # type: ignore[attr-defined]
        if tenant is None or not tenant.active:
            raise UnknownTenant("tenant is not active")

        # -- purchase option: request field, entitlement from the account --
        option, source = self._resolve_purchase_option(
            account_class, requested_purchase_option
        )
        if option is PurchaseOption.ON_DEMAND:
            # HLD §1: STATIC and DYNAMIC requests leave this design entirely.
            raise OutOfScope(
                "this service implements the spot path only; on-demand and "
                "reserved requests are handled by the pay-per-use path",
                details={"purchase_option": option.value, "source": source.value},
            )
        if account_class is not AccountClass.SPOT:
            raise NotEntitled(
                f"account class {account_class.value} is not entitled to spot",
                details={"account_class": account_class.value},
            )

        # -- flavour ------------------------------------------------------
        flavour = await self._reference.get_flavour(flavour_name)  # type: ignore[attr-defined]
        if flavour is None:
            raise InvalidRequest(f"unknown flavour {flavour_name!r}")
        if not flavour.spot_eligible:
            # 400, not 409. A licence-bound flavour will never become available
            # as spot, so telling the client to retry would be a lie.
            raise FlavourNotEligible(
                f"{flavour.name} is not sold as spot"
                + (" because it is licence-bound" if flavour.licence_bound else ""),
                details={"flavour": flavour.name, "licence_bound": flavour.licence_bound},
            )

        units = flavour.units(count)
        if units > settings.max_units_per_request:
            raise InvalidRequest(
                f"request of {units} units exceeds the per-request maximum of "
                f"{settings.max_units_per_request}",
                details={"units": units, "max": settings.max_units_per_request},
            )

        # -- quota and concurrency, before any capacity work --------------
        quota = tenant.spot_quota_units or settings.tenant_quota
        units_in_use, leases_in_use = await self._leases.tenant_usage(tenant_id)  # type: ignore[attr-defined]

        if units_in_use + units > quota:
            raise QuotaExceeded(
                f"spot quota exceeded: {units_in_use} of {quota} units in use, "
                f"this request needs {units}",
                details={
                    "quota_units": quota,
                    "units_in_use": units_in_use,
                    "units_requested": units,
                },
                retry_after=settings.retry_after,
            )
        if leases_in_use + 1 > tenant.concurrency_cap:
            raise QuotaExceeded(
                f"concurrency cap reached: {leases_in_use} of "
                f"{tenant.concurrency_cap} leases in flight",
                details={
                    "concurrency_cap": tenant.concurrency_cap,
                    "leases_in_use": leases_in_use,
                },
                retry_after=settings.retry_after,
            )

        edge(
            log,
            5,
            f"validated {tenant_id}: {count}x{flavour.name} = {units}u in {az}",
            tenant_id=tenant_id,
            flavour=flavour.name,
            count=count,
            units=units,
            quota_units=quota,
            units_in_use=units_in_use,
            purchase_option=option.value,
            purchase_option_source=source.value,
        )
        return Validated(
            tenant=tenant,
            account_class=account_class,
            flavour=flavour,
            count=count,
            units=units,
            purchase_option=option,
            purchase_option_source=source,
            quota_units=quota,
            units_in_use=units_in_use,
            leases_in_use=leases_in_use,
        )

    def _resolve_purchase_option(
        self, account_class: AccountClass, requested: PurchaseOption | None
    ) -> tuple[PurchaseOption, PurchaseOptionSource]:
        """Resolve HLD §12's risk 1.

        An explicit `purchase_option` on the request wins. When it is absent the
        account class decides, which keeps the finalised diagram's account-only
        routing working unchanged for clients that have not adopted the field.
        The source is reported back to the caller so a tenant mid-migration can
        see which of their calls are still being routed the old way.
        """
        if requested is not None:
            return requested, PurchaseOptionSource.REQUEST
        if account_class is AccountClass.SPOT:
            return PurchaseOption.SPOT, PurchaseOptionSource.ACCOUNT
        return PurchaseOption.ON_DEMAND, PurchaseOptionSource.ACCOUNT

"""Provisioning Adapter — edges 11, 12 and 13.

HLD §6:

    Owns: Idempotent create / stop / destroy with compensating actions.
    Must not do: Leave a half-created instance without compensation.

"Never leave a half-created instance without compensation" is the requirement
that shapes this module. Every failure path here ends in either a successful
operation or a destroy, and the destroy is best-effort-but-loud: if it also
fails, the host is quarantined and the failure is audited rather than swallowed.
An instance that exists but that no lease accounts for is invisible capacity
loss — it consumes a host slot forever and no reconciliation will find it,
because reconciliation compares the ledger against *leases*.

The escalation ladder for a stop, from LLD §11:

    force stop  ->  (host agent unreachable)  ->  destroy  ->  quarantine host
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from ..config import Settings
from ..domain.errors import ProvisioningFailed
from ..domain.models import Lease
from ..external.base import ExternalError
from ..external.hypervisor import HostUnreachable, Hypervisor
from ..logging import edge, get_logger
from ..metrics import M

log = get_logger(__name__)

__all__ = ["ProvisioningAdapter", "StopOutcome"]


class StopOutcome:
    """What actually happened when instances were stopped."""

    __slots__ = ("stopped", "escalated_to_destroy", "host_quarantined", "detail")

    def __init__(
        self,
        stopped: bool,
        *,
        escalated_to_destroy: bool = False,
        host_quarantined: bool = False,
        detail: str = "",
    ) -> None:
        self.stopped = stopped
        self.escalated_to_destroy = escalated_to_destroy
        self.host_quarantined = host_quarantined
        self.detail = detail

    def __bool__(self) -> bool:
        return self.stopped


class ProvisioningAdapter:
    def __init__(
        self,
        *,
        settings: Settings,
        hypervisor: Hypervisor,
        reference_repo: Any,
        audit_repo: Any,
    ) -> None:
        self._settings = settings
        self._hypervisor = hypervisor
        self._reference = reference_repo
        self._audit = audit_repo

    # ------------------------------------------------------------------
    async def create(self, lease: Lease, host_group: str) -> list[str]:
        """Create the instances. Compensates by destroying on partial failure."""
        try:
            instance_ids = await self._hypervisor.create(
                lease_id=lease.lease_id,
                host_group=host_group,
                flavour=lease.flavour,
                count=lease.count,
            )
        except ExternalError as exc:
            # The call failed, but we cannot know whether the far side created
            # anything before failing. Compensate unconditionally: destroy is
            # idempotent and destroying nothing is free, whereas leaving an
            # orphan is permanent.
            await self._compensate(lease, reason=f"create failed: {exc}")
            raise ProvisioningFailed(
                f"could not provision {lease.count}x{lease.flavour}: {exc}",
                details={"host_group": host_group},
            ) from exc

        if len(instance_ids) != lease.count:
            await self._compensate(
                lease,
                reason=f"expected {lease.count} instances, got {len(instance_ids)}",
            )
            raise ProvisioningFailed(
                f"provisioning returned {len(instance_ids)} of {lease.count} "
                f"instances; the partial set has been destroyed",
                details={"host_group": host_group},
            )

        edge(
            log, 11, f"provisioned {len(instance_ids)} instance(s) on {host_group}",
            lease_id=lease.lease_id, host_group=host_group,
            instance_ids=list(instance_ids),
        )
        return list(instance_ids)

    async def deliver_notice(self, lease: Lease, deadline: datetime) -> bool:
        """Write the notice into the guest-local metadata service (one channel)."""
        try:
            return await self._hypervisor.deliver_notice(
                lease_id=lease.lease_id,
                instance_ids=list(lease.instance_ids),
                deadline=deadline,
            )
        except ExternalError as exc:
            log.warning(
                "provisioning.metadata_notice_failed",
                lease_id=lease.lease_id,
                error=str(exc),
            )
            return False

    async def force_stop(self, lease: Lease) -> StopOutcome:
        """Stop the instances, escalating exactly as LLD §11 prescribes."""
        host_group = lease.host_group or "unknown"
        try:
            await self._hypervisor.force_stop(
                lease_id=lease.lease_id,
                host_group=host_group,
                instance_ids=list(lease.instance_ids),
            )
        except HostUnreachable as exc:
            log.error(
                "provisioning.host_unreachable",
                lease_id=lease.lease_id,
                host_group=host_group,
                note="escalating to destroy and quarantining the host (LLD §11)",
            )
            destroyed = await self._destroy(lease)
            await self._reference.quarantine_host_group(
                host_group, f"agent unreachable during force stop of {lease.lease_id}"
            )
            await self._audit.append(
                "host.quarantined",
                lease_id=lease.lease_id,
                detail={"host_group": host_group, "trigger": "force_stop_unreachable"},
            )
            return StopOutcome(
                destroyed,
                escalated_to_destroy=True,
                host_quarantined=True,
                detail=str(exc),
            )
        except ExternalError as exc:
            destroyed = await self._destroy(lease)
            return StopOutcome(
                destroyed, escalated_to_destroy=True, detail=str(exc)
            )

        M.forced_stop_total.labels(flavour=lease.flavour).inc()
        return StopOutcome(True)

    async def teardown(self, lease: Lease) -> bool:
        """Detach volumes and release IPs within the budget.

        False is not an error — it is the signal that the units must stay in
        RECLAIMING. LLD §11: a stalled teardown must never look like free
        capacity.
        """
        try:
            return await self._hypervisor.teardown(
                lease_id=lease.lease_id,
                instance_ids=list(lease.instance_ids),
                budget_seconds=self._settings.teardown_budget,
            )
        except ExternalError as exc:
            log.warning(
                "provisioning.teardown_failed",
                lease_id=lease.lease_id,
                error=str(exc),
                note="units remain RECLAIMING; the sweeper will retry",
            )
            return False

    async def destroy(self, lease: Lease) -> bool:
        return await self._destroy(lease)

    # ------------------------------------------------------------------
    async def _destroy(self, lease: Lease) -> bool:
        try:
            await self._hypervisor.destroy(
                lease_id=lease.lease_id, instance_ids=list(lease.instance_ids)
            )
            return True
        except ExternalError as exc:
            # This is the one failure with no further recovery in software. Say
            # so at ERROR with the instance ids, because someone has to go and
            # look.
            log.error(
                "provisioning.destroy_failed",
                lease_id=lease.lease_id,
                instance_ids=list(lease.instance_ids),
                host_group=lease.host_group,
                error=str(exc),
                remediation="orphaned instances consume host capacity that no "
                "lease accounts for; reconcile manually against the hypervisor",
            )
            await self._audit.append(
                "provisioning.orphaned_instances",
                lease_id=lease.lease_id,
                detail={
                    "instance_ids": list(lease.instance_ids),
                    "host_group": lease.host_group,
                    "error": str(exc),
                },
            )
            return False

    async def _compensate(self, lease: Lease, *, reason: str) -> None:
        log.warning(
            "provisioning.compensating",
            lease_id=lease.lease_id,
            reason=reason,
            note="HLD §6: never leave a half-created instance without compensation",
        )
        await self._destroy(lease)

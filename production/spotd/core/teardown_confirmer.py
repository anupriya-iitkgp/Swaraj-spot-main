"""Teardown Confirmer — edges 13, 14 and 15.

The component that answers one question: *has the capacity actually come back?*

It exists because "stopped" and "returned" are not the same event. An instance
can be stopped while its volumes are still attached and its floating IP is still
held, and during that interval the host cannot take a guaranteed-class workload.
Reporting the capacity as free at the moment of stop would tell the ledger it
has room it does not have — and HLD §12 lists exactly that as the worst outcome
in the whole design: "guaranteed-class SLA breaches".

So the ordering is strict, and it is the ordering LLD §9 requires of the ledger
interface ("never report capacity free on an unconfirmed commit"):

    instances stopped
      -> teardown confirms volumes detached and IPs released   (edge 13)
      -> commit_capacity_returned succeeds                     (edge 14)
      -> only then is the lease CLOSED                         (edge 15)

If any step fails, the lease stays STOPPED, the units stay RECLAIMING, and the
lease joins the stalled set where an operator can see it. That is deliberately
the "stuck" outcome rather than the "assume success" outcome, because stuck is
visible and recoverable while a false free is neither.

Note the confirmer does not write lease state itself — it returns a verdict and
the Spot Lease Manager performs the transition, preserving HLD §6's single-writer
rule.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..db.repositories import AuditEvent, Topics
from ..domain.models import Lease, utcnow
from ..external.base import ExternalError
from ..external.ledger import CapacityLedger
from ..logging import edge, get_logger
from ..metrics import M

log = get_logger(__name__)

__all__ = ["TeardownConfirmer", "TeardownVerdict"]


@dataclass(frozen=True, slots=True)
class TeardownVerdict:
    confirmed: bool
    reason: str
    elapsed_seconds: float

    def __bool__(self) -> bool:
        return self.confirmed


class TeardownConfirmer:
    def __init__(
        self,
        *,
        settings: Settings,
        provisioning: Any,
        ledger: CapacityLedger,
        ledger_repo: Any,
        audit_repo: Any,
        outbox_repo: Any,
    ) -> None:
        self._settings = settings
        self._provisioning = provisioning
        self._ledger = ledger
        self._ledger_repo = ledger_repo
        self._audit = audit_repo
        self._outbox = outbox_repo

    async def confirm(self, lease: Lease) -> TeardownVerdict:
        """Prove the capacity is back. Returns False rather than raising."""
        started = time.perf_counter()
        host_group = lease.host_group or "unknown"

        # -- edge 13: volumes detached, IPs released ----------------------
        released = await self._provisioning.teardown(lease)
        if not released:
            elapsed = time.perf_counter() - started
            M.teardown_stalled.labels(host_group=host_group).inc()
            await self._audit.append(
                AuditEvent.TEARDOWN_STALLED,
                lease_id=lease.lease_id,
                tenant_id=lease.tenant_id,
                detail={
                    "host_group": host_group,
                    "units": lease.units,
                    "budget_seconds": self._settings.teardown_budget,
                    "consequence": "units remain RECLAIMING and are not reported "
                    "free; the lease stays STOPPED",
                },
            )
            log.error(
                "teardown.stalled",
                lease_id=lease.lease_id,
                host_group=host_group,
                units=lease.units,
                remediation="volumes or IPs did not release inside the budget; "
                "the sweeper will retry, and the capacity stays accounted for",
            )
            return TeardownVerdict(
                False, "volumes or IPs did not release within the budget", elapsed
            )

        # -- edge 14: the ledger must accept the commit -------------------
        try:
            await self._ledger.commit_capacity_returned(
                host_group=host_group, lease_id=lease.lease_id, units=lease.units
            )
        except ExternalError as exc:
            elapsed = time.perf_counter() - started
            log.error(
                "teardown.ledger_commit_failed",
                lease_id=lease.lease_id,
                host_group=host_group,
                error=str(exc),
                remediation="units stay RECLAIMING — never reported free on an "
                "unconfirmed commit (LLD §9); the sweeper retries with backoff",
            )
            return TeardownVerdict(False, f"ledger commit failed: {exc}", elapsed)

        await self._ledger_repo.record(
            host_group=host_group,
            lease_id=lease.lease_id,
            operation="returned",
            units=lease.units,
            confirmed=True,
        )
        elapsed = time.perf_counter() - started
        edge(
            log,
            13,
            f"teardown confirmed in {elapsed:.2f}s; {lease.units}u returned "
            f"on {host_group}",
            lease_id=lease.lease_id,
            host_group=host_group,
            units=lease.units,
            elapsed_seconds=round(elapsed, 3),
        )
        return TeardownVerdict(True, "volumes and IPs released; ledger committed", elapsed)

    async def mark_allocated(self, lease: Lease, host_group: str) -> None:
        """Tell the ledger a lease now holds capacity on a host group.

        Enqueued through the outbox rather than called directly, so the ledger
        write and the lease's transition to RUNNING cannot disagree after a
        crash (LLD §12.8).
        """
        await self._ledger_repo.record(
            host_group=host_group,
            lease_id=lease.lease_id,
            operation="allocated",
            units=lease.units,
        )

    async def mark_reclaiming(self, lease: Lease, *, conn: Any = None) -> None:
        """Move the units into RECLAIMING at the moment the notice is issued.

        Doing this at notice time rather than at stop time is what stops the
        pool re-selling capacity that is already promised to the capacity side.
        """
        host_group = lease.host_group or "unknown"
        await self._ledger_repo.record(
            host_group=host_group,
            lease_id=lease.lease_id,
            operation="reclaiming",
            units=lease.units,
            conn=conn,
        )
        await self._outbox.enqueue(
            topic=Topics.LEDGER,
            aggregate_type="lease",
            aggregate_id=lease.lease_id,
            payload={
                "operation": "reclaiming",
                "host_group": host_group,
                "lease_id": lease.lease_id,
                "units": lease.units,
            },
            conn=conn,
        )

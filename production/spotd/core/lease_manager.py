"""Spot Lease Manager — the hub. Edges 8, 9, 11, 15, 16, 22, 23, 25, 28 and 32.

HLD §6:

    Owns: The lease record, discount snapshot, TTL, state machine and audit trail.
          Single writer.
    Must not do: Let any other component write lease state.

Every state change in the system passes through this file. Placement, notice
delivery, teardown and metering all hand back *results*; this module decides
what those results mean for the lease and performs the transition. That is why,
for example, `TeardownConfirmer` returns a verdict rather than closing the lease
itself — edge 15 is drawn from the confirmer *to* the lease manager for exactly
this reason.

Two behaviours here are worth reading closely because they are where the design
is most easily got wrong.

**Fulfilment re-checks state after provisioning.** LLD §10.4 lists the race:
"Reclaim order lands while fulfil() is mid-flight" and its resolution: "fulfil()
re-checks state after provisioning; if it is no longer PROVISIONING it destroys
what it just created". Without that re-check a reclaim can be acknowledged, the
lease cancelled, and then a boot completes on top of it — leaving a running
instance on capacity that has already been promised to someone else.

**There is no in-process grace timer.** `preempt()` issues the notice, persists
an absolute `force_stop_deadline`, and returns. Nothing waits. A guest that
exits cleanly calls back through `report_clean_exit()`; a guest that does not is
force-stopped by the database-backed reaper. LLD §12.3 identifies the in-memory
timer as the mechanism that strands leases across a restart, so the fix is not
to persist the timer but to remove it.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from ..config import Settings
from ..db.repositories import AuditEvent, Topics
from ..domain.errors import InvalidLeaseState, LeaseNotFound, SpotError
from ..domain.models import (
    Lease,
    NoticeChannel,
    PreemptionReason,
    ReclaimOrder,
    RejectionCode,
    utcnow,
)
from ..domain.state_machine import CANCELLABLE, LeaseState
from ..logging import edge, get_logger, lease_context
from ..metrics import M

log = get_logger(__name__)

__all__ = ["SpotLeaseManager"]


class SpotLeaseManager:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Any,
        lease_repo: Any,
        reference_repo: Any,
        audit_repo: Any,
        outbox_repo: Any,
        admission: Any,
        placement: Any,
        provisioning: Any,
        notice: Any,
        teardown: Any,
        metering: Any,
    ) -> None:
        self._settings = settings
        self._db = db
        self._leases = lease_repo
        self._reference = reference_repo
        self._audit = audit_repo
        self._outbox = outbox_repo
        self._admission = admission
        self._placement = placement
        self._provisioning = provisioning
        self._notice = notice
        self._teardown = teardown
        self._metering = metering

    # ==================================================================
    # launch path — edges 9, 10, 11, 12
    # ==================================================================
    async def fulfil(self, lease_id: str) -> Lease | None:
        """Take an ADMITTED lease to RUNNING.

        Runs off the request path: HLD §5 marks edges 9 and 11 async, and the
        200 ms admission target in §11 cannot absorb a placement round trip plus
        an instance boot.

        The ADMITTED -> PROVISIONING transition *is* the claim. It is conditional
        on the current state, so exactly one caller wins — whether that is the
        in-process task the API spawned for latency, or the fulfilment sweeper
        picking up a lease stranded by a restart.
        """
        with lease_context(lease_id):
            claimed = await self._leases.try_transition(
                lease_id,
                to=LeaseState.PROVISIONING,
                require_state=[LeaseState.ADMITTED],
                provisioning_at=utcnow(),
            )
            if claimed is None:
                return None

            lease = claimed
            try:
                host_group = await self._placement.place(lease)
                instance_ids = await self._provisioning.create(lease, host_group)
            except SpotError as exc:
                await self._reject(lease, exc)
                return None

            # LLD §10.4: a reclaim may have landed while we were provisioning.
            current = await self._leases.get(lease_id)
            if current is None or current.state is not LeaseState.PROVISIONING:
                observed = current.state.value if current else "gone"
                log.warning(
                    "lease.cancelled_during_provisioning",
                    lease_id=lease_id,
                    observed_state=observed,
                    note="destroying the instances that were just created; the "
                    "lease was cancelled outright (HLD §12)",
                )
                await self._provisioning.destroy(
                    lease.with_state(lease.state, instance_ids=tuple(instance_ids))
                )
                return None

            now = utcnow()
            async with self._db.transaction() as conn:
                running = await self._leases.transition(
                    lease_id,
                    to=LeaseState.RUNNING,
                    require_state=[LeaseState.PROVISIONING],
                    conn=conn,
                    host_group=host_group,
                    instance_ids=tuple(instance_ids),
                    running_at=now,
                )
                await self._outbox.enqueue(
                    topic=Topics.LEDGER,
                    aggregate_type="lease",
                    aggregate_id=lease_id,
                    payload={
                        "operation": "allocated",
                        "host_group": host_group,
                        "lease_id": lease_id,
                        "units": lease.units,
                    },
                    conn=conn,
                )
                await self._audit.append(
                    AuditEvent.LEASE_TRANSITION,
                    lease_id=lease_id,
                    tenant_id=lease.tenant_id,
                    detail={
                        "to": "RUNNING",
                        "host_group": host_group,
                        "instance_ids": list(instance_ids),
                    },
                    conn=conn,
                )
                await self._outbox.enqueue(
                    topic=Topics.LEASE_STATE,
                    aggregate_type="lease",
                    aggregate_id=lease_id,
                    payload={
                        "lease_id": lease_id,
                        "tenant_id": lease.tenant_id,
                        "state": "RUNNING",
                        "host_group": host_group,
                        "instance_ids": list(instance_ids),
                        "at": now.isoformat(),
                    },
                    conn=conn,
                )

            await self._teardown.mark_allocated(running, host_group)
            edge(
                log,
                11,
                f"running on {host_group} with {len(instance_ids)} instance(s)",
                lease_id=lease_id,
                host_group=host_group,
            )
            return running

    async def _reject(self, lease: Lease, error: SpotError) -> None:
        """Fail a launch and give the capacity back.

        LLD §11: "Placement or provisioning fails -> Lease REJECTED, reservation
        released, nothing billed." Releasing the reservation is the part that
        matters: without it a failed launch leaks units from the pool
        permanently, and the leak is invisible until reconciliation notices the
        counter drifting.
        """
        code = (
            RejectionCode.PLACEMENT_FAILED
            if error.code == "placement_failed"
            else RejectionCode.PROVISIONING_FAILED
        )
        async with self._db.transaction() as conn:
            await self._leases.try_transition(
                lease.lease_id,
                to=LeaseState.REJECTED,
                require_state=[LeaseState.ADMITTED, LeaseState.PROVISIONING],
                conn=conn,
                rejection_code=code,
                rejection_detail=error.message[:500],
                closed_at=utcnow(),
            )
            await self._admission.release(
                lease, cooldown=False, reason="launch_failed", conn=conn
            )
            await self._audit.append(
                AuditEvent.LEASE_REJECTED,
                lease_id=lease.lease_id,
                tenant_id=lease.tenant_id,
                detail={"code": code.value, "detail": error.message[:500]},
                conn=conn,
            )
        M.admission_total.labels(outcome="503").inc()
        log.warning(
            "lease.rejected",
            lease_id=lease.lease_id,
            code=code.value,
            reason=error.message,
            note="reservation released; nothing billed",
        )

    # ==================================================================
    # preemption path — edges 16, 22, 23
    # ==================================================================
    async def preempt(self, lease: Lease, order: ReclaimOrder) -> bool:
        """Issue a preemption notice, or cancel a lease that never ran.

        Returns True if the lease is now on its way out. False means another
        writer got there first, which is the ordinary outcome when two reclaim
        orders select the same lease (LLD §10.4).
        """
        with lease_context(lease.lease_id, lease.tenant_id):
            if lease.state in CANCELLABLE:
                return await self.cancel_in_flight(lease, order)
            if lease.state is not LeaseState.RUNNING:
                return False

            now = utcnow()
            # The deadline is force_stop_at, not grace_seconds: config validation
            # guarantees force_stop_at + teardown_budget < grace_seconds, so
            # stopping here still leaves room to return the capacity inside the
            # window the customer was promised.
            deadline = now + timedelta(seconds=self._settings.force_stop_at)

            async with self._db.transaction() as conn:
                noticed = await self._leases.try_transition(
                    lease.lease_id,
                    to=LeaseState.NOTICE_ISSUED,
                    require_state=[LeaseState.RUNNING],
                    conn=conn,
                    notice_at=now,
                    # Absolute, persisted. This one column is what makes the
                    # preemption path survive a restart (LLD §12.3).
                    force_stop_deadline=deadline,
                    preemption_reason=PreemptionReason.CAPACITY_RECLAIM,
                    reclaim_order_id=order.order_id,
                )
                if noticed is None:
                    return False

                await self._teardown.mark_reclaiming(noticed, conn=conn)
                await self._audit.append(
                    AuditEvent.NOTICE_ISSUED,
                    lease_id=lease.lease_id,
                    order_id=order.order_id,
                    tenant_id=lease.tenant_id,
                    detail={
                        "grace_seconds": lease.grace_seconds,
                        "force_stop_deadline": deadline.isoformat(),
                        "host_group": lease.host_group,
                        "units": lease.units,
                    },
                    conn=conn,
                )

            edge(
                log,
                16,
                f"notice issued, force stop at {deadline.isoformat()} "
                f"({self._settings.force_stop_at:.0f}s)",
                lease_id=lease.lease_id,
                order_id=order.order_id,
                deadline=deadline.isoformat(),
            )

            # -- edge 17: fan out, then record the outcome atomically -----
            tenant = await self._reference.get_tenant(lease.tenant_id)
            result = await self._notice.publish_notice(
                noticed, tenant, deadline, order_id=order.order_id
            )
            # LLD §12.7: one atomic replace, so no reader can observe a
            # NOTICE_ISSUED lease with an empty channel list.
            await self._leases.set_fields(
                lease.lease_id,
                notice_channels_delivered=tuple(result.delivered),
            )
            return True

    async def cancel_in_flight(self, lease: Lease, order: ReclaimOrder | None) -> bool:
        """Cancel a lease that has not started running: no notice, no charge.

        HLD §12 calls this "the easiest case to get wrong — it can produce a
        notice for an instance that never booted, or a charge for one that never
        ran" and asks for it to be modelled explicitly and tested as a
        first-class path. It goes straight to CLOSED; `running_at` is NULL, so
        the rating arithmetic yields nothing and the database constraint
        `unrun_lease_is_not_billed` enforces it independently.
        """
        async with self._db.transaction() as conn:
            cancelled = await self._leases.try_transition(
                lease.lease_id,
                to=LeaseState.CLOSED,
                require_state=[LeaseState.ADMITTED, LeaseState.PROVISIONING],
                conn=conn,
                closed_at=utcnow(),
                preemption_reason=PreemptionReason.CANCELLED_IN_FLIGHT,
                reclaim_order_id=order.order_id if order else None,
            )
            if cancelled is None:
                return False
            await self._admission.release(
                lease,
                cooldown=order is not None,
                reason="cancelled_in_flight",
                conn=conn,
            )
            await self._audit.append(
                AuditEvent.CANCELLED_IN_FLIGHT,
                lease_id=lease.lease_id,
                order_id=order.order_id if order else None,
                tenant_id=lease.tenant_id,
                detail={
                    "state_when_cancelled": lease.state.value,
                    "notice_issued": False,
                    "billed": False,
                },
                conn=conn,
            )

        if lease.instance_ids:
            await self._provisioning.destroy(lease)
        log.info(
            "lease.cancelled_in_flight",
            lease_id=lease.lease_id,
            from_state=lease.state.value,
            note="no notice, no charge (HLD §12)",
        )
        return True

    # ==================================================================
    # the two ways a preemption ends
    # ==================================================================
    async def report_clean_exit(self, lease_id: str) -> bool:
        """Edge 13 — the guest exited on its own, inside the window.

        In production this is called from the authenticated
        `POST /internal/spot/leases/{id}/exited` endpoint by the host agent.
        """
        with lease_context(lease_id):
            stopped = await self._leases.try_transition(
                lease_id,
                to=LeaseState.STOPPED,
                require_state=[LeaseState.NOTICE_ISSUED, LeaseState.DRAINING],
                stopped_at=utcnow(),
            )
            if stopped is None:
                # Already stopped — the reaper won, or this is a duplicate
                # report. Both are fine and neither is an error (LLD §10.4).
                return False

            await self._audit.append(
                AuditEvent.CLEAN_EXIT,
                lease_id=lease_id,
                tenant_id=stopped.tenant_id,
                order_id=stopped.reclaim_order_id,
                detail={
                    "grace_used_seconds": round(stopped.grace_window_seconds or 0.0, 2),
                    "grace_allowed_seconds": stopped.grace_seconds,
                },
            )
            log.info(
                "lease.clean_exit",
                lease_id=lease_id,
                grace_used_seconds=round(stopped.grace_window_seconds or 0.0, 2),
            )
            await self.finish_teardown(stopped)
            return True

    async def force_stop(self, lease: Lease) -> bool:
        """The reaper's path — edge 24. The timer is authoritative.

        HLD §6 gives the Grace Timer "the authoritative 120 s clock, forced stop,
        and escalation when the host agent is unreachable" and forbids it from
        waiting "on the guest beyond the timer". The grace period is a courtesy;
        this is the guarantee.
        """
        with lease_context(lease.lease_id, lease.tenant_id):
            draining = await self._leases.try_transition(
                lease.lease_id,
                to=LeaseState.DRAINING,
                require_state=[LeaseState.NOTICE_ISSUED],
            )
            if draining is None:
                return False

            await self._audit.append(
                AuditEvent.GRACE_EXPIRED,
                lease_id=lease.lease_id,
                order_id=lease.reclaim_order_id,
                tenant_id=lease.tenant_id,
                detail={
                    "deadline": lease.force_stop_deadline.isoformat()
                    if lease.force_stop_deadline
                    else None,
                    "note": "guest did not exit within the grace window",
                },
            )

            outcome = await self._provisioning.force_stop(draining)
            stopped = await self._leases.try_transition(
                lease.lease_id,
                to=LeaseState.STOPPED,
                require_state=[LeaseState.DRAINING],
                stopped_at=utcnow(),
                forced_stop=True,
            )
            if stopped is None:
                return False

            await self._audit.append(
                AuditEvent.FORCED_STOP,
                lease_id=lease.lease_id,
                order_id=lease.reclaim_order_id,
                tenant_id=lease.tenant_id,
                detail={
                    "escalated_to_destroy": outcome.escalated_to_destroy,
                    "host_quarantined": outcome.host_quarantined,
                    "detail": outcome.detail,
                },
            )
            log.warning(
                "lease.force_stopped",
                lease_id=lease.lease_id,
                escalated=outcome.escalated_to_destroy,
                host_quarantined=outcome.host_quarantined,
            )
            await self.finish_teardown(stopped)
            return True

    # ==================================================================
    # customer-initiated release
    # ==================================================================
    async def release(self, lease: Lease) -> Lease:
        """DELETE /spot/leases/{id}. The tenant asked, so there is no notice."""
        with lease_context(lease.lease_id, lease.tenant_id):
            if lease.state in CANCELLABLE:
                await self.cancel_in_flight(lease, None)
                refreshed = await self._leases.get(lease.lease_id)
                assert refreshed is not None
                return refreshed

            if lease.state is not LeaseState.RUNNING:
                raise InvalidLeaseState(
                    f"lease is {lease.state.value} and cannot be released",
                    details={"state": lease.state.value},
                )

            draining = await self._leases.try_transition(
                lease.lease_id,
                to=LeaseState.DRAINING,
                require_state=[LeaseState.RUNNING],
                preemption_reason=PreemptionReason.CUSTOMER_RELEASE,
            )
            if draining is None:
                refreshed = await self._leases.get(lease.lease_id)
                assert refreshed is not None
                return refreshed

            await self._provisioning.force_stop(draining)
            stopped = await self._leases.try_transition(
                lease.lease_id,
                to=LeaseState.STOPPED,
                require_state=[LeaseState.DRAINING],
                stopped_at=utcnow(),
            )
            if stopped is not None:
                await self.finish_teardown(stopped)
            refreshed = await self._leases.get(lease.lease_id)
            assert refreshed is not None
            return refreshed

    # ==================================================================
    # edge 15 — CLOSED, but only once the capacity is provably back
    # ==================================================================
    async def finish_teardown(self, lease: Lease) -> Lease | None:
        """Confirm the capacity returned, rate the lease, and close it.

        A lease is CLOSED only after `TeardownConfirmer` proves both that the
        instances released their volumes and IPs and that the ledger accepted
        the commit. If either fails the lease stays STOPPED with
        `teardown_stalled = true` and its units stay accounted for — never
        reported free (LLD §9, §11).
        """
        verdict = await self._teardown.confirm(lease)
        if not verdict:
            await self._leases.set_fields(lease.lease_id, teardown_stalled=True)
            return None

        result = self._metering.compute(lease, closed_at=utcnow())
        closed_at = utcnow()
        preempted = lease.preemption_reason is PreemptionReason.CAPACITY_RECLAIM

        async with self._db.transaction() as conn:
            closed = await self._leases.try_transition(
                lease.lease_id,
                to=LeaseState.CLOSED,
                require_state=[LeaseState.STOPPED],
                conn=conn,
                closed_at=closed_at,
                billed_seconds=result.billable_seconds,
                billed_amount=result.amount,
                grace_seconds_excluded=result.grace_excluded,
                credit_raised=result.credit.amount if result.credit else 0.0,
                teardown_stalled=False,
            )
            if closed is None:
                return None

            await self._metering.close_lease(closed, conn=conn, closed_at=closed_at)
            # The capacity goes back with an anti-thrash hold only when it was
            # taken by a reclaim. A customer who released voluntarily created no
            # churn to damp.
            await self._admission.release(
                closed,
                cooldown=preempted,
                reason=lease.preemption_reason.value
                if lease.preemption_reason
                else "closed",
                conn=conn,
            )
            await self._audit.append(
                AuditEvent.CAPACITY_RETURNED,
                lease_id=lease.lease_id,
                order_id=lease.reclaim_order_id,
                tenant_id=lease.tenant_id,
                detail={
                    "units": lease.units,
                    "host_group": lease.host_group,
                    "teardown_seconds": round(verdict.elapsed_seconds, 3),
                    "billed_seconds": result.billable_seconds,
                    "billed_amount": result.amount,
                    "grace_seconds_excluded": result.grace_excluded,
                    "credit": result.credit.amount if result.credit else 0.0,
                },
                conn=conn,
            )
            await self._outbox.enqueue(
                topic=Topics.LEASE_STATE,
                aggregate_type="lease",
                aggregate_id=lease.lease_id,
                payload={
                    "lease_id": lease.lease_id,
                    "tenant_id": lease.tenant_id,
                    "state": "CLOSED",
                    "at": closed_at.isoformat(),
                    "billed_amount": result.amount,
                },
                conn=conn,
            )

        self._observe_reclaim(closed)
        edge(
            log,
            15,
            f"closed; {lease.units}u returned, billed {result.amount:.6f}",
            lease_id=lease.lease_id,
            billed_amount=result.amount,
            billed_seconds=result.billable_seconds,
        )
        return closed

    def _observe_reclaim(self, lease: Lease) -> None:
        """Record the number HLD §11's 99.9%-within-120s target is measured on."""
        window = lease.reclaim_window_seconds
        if window is None or lease.preemption_reason is not PreemptionReason.CAPACITY_RECLAIM:
            return
        M.reclaim_duration.labels(forced=str(lease.forced_stop).lower()).observe(window)
        if window > lease.grace_seconds:
            log.error(
                "reclaim.slo_breach",
                lease_id=lease.lease_id,
                took_seconds=round(window, 2),
                advertised_grace_seconds=lease.grace_seconds,
                forced=lease.forced_stop,
                note="notice to capacity-returned exceeded the advertised grace "
                "window; the guaranteed classes waited longer than promised",
            )

    # ==================================================================
    # reads — edge 32
    # ==================================================================
    async def describe(self, lease_id: str, tenant_id: str) -> Lease:
        lease = await self._leases.get_for_tenant(lease_id, tenant_id)
        if lease is None:
            raise LeaseNotFound(f"no lease {lease_id}")
        return lease

    async def list_for_tenant(self, tenant_id: str, **kwargs: Any) -> list[Lease]:
        return await self._leases.list_for_tenant(tenant_id, **kwargs)

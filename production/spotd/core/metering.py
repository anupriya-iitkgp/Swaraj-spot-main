"""Spot Metering & Rating — edges 25 and 26.

HLD §6:

    Owns: Per-second usage at the snapshotted discount, grace exclusion, credits.
    Must not do: Re-rate a running lease when the published discount changes.

The "must not" is structural rather than procedural: this module reads
`lease.rate_per_sec` and `lease.discount_snapshot`, which were frozen at
admission. It has no access to the pricing engine and no way to obtain a current
price, so re-rating is not something a future change could do by accident.

Three arithmetic rules, each from the design:

1.  **The grace window is not billed.** HLD §10 has `grace_seconds_excluded` as
    a first-class field, so the billable window ends at `notice_at`, not at
    `stopped_at`. A customer who is being interrupted does not pay for the two
    minutes they spend shutting down.

2.  **A lease that never ran is never charged.** HLD §12 requires a reclaim
    during ADMITTED or PROVISIONING to produce "no notice, no charge". With
    `running_at` NULL the billable window is empty, so this falls out of the
    arithmetic rather than needing a special case — and the database enforces it
    independently through the `unrun_lease_is_not_billed` constraint.

3.  **An undelivered notice is an automatic credit.** HLD §10, on
    `notice_channels_delivered`: "Empty ⇒ automatic credit." The credit is the
    full amount billed for the lease: the customer lost the work *and* had no
    chance to checkpoint it, so charging for the compute that was thrown away is
    not defensible.

    Rule 3 has a floor, and the floor is the part that matters. Billed amount
    alone makes the credit proportional to how long the lease happened to run
    before it was killed, so a lease preempted a second after it started is
    credited approximately nothing — which is precisely the case where the
    customer was most badly served. What they lost is not the compute, it is the
    notice: HLD §11 sets notice delivery at "≥ 99.99% on at least one channel"
    and calls it "the trust anchor of the whole product", and HLD §12 says an
    all-channel failure is "an SLO breach with automatic credit". The thing that
    failed has a price — one grace window at the lease's own rate — so that is
    the floor. For any lease that ran longer than its grace window the billed
    amount dominates and the floor never binds, leaving §6.6's arithmetic
    unchanged for the ordinary case.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import Settings
from ..db.repositories import AuditEvent, Topics
from ..domain.models import CreditRecord, Lease, UsageRecord, new_id, utcnow
from ..logging import edge, get_logger

log = get_logger(__name__)

__all__ = ["MeteringService", "RatingResult"]

#: Reason codes on credits. Stable strings — they appear on invoices.
CREDIT_NOTICE_UNDELIVERED = "notice_undelivered"
CREDIT_SLO_BREACH = "reclaim_slo_breach"


class RatingResult:
    __slots__ = ("usage", "credit", "billable_seconds", "grace_excluded", "amount")

    def __init__(
        self,
        usage: UsageRecord | None,
        credit: CreditRecord | None,
        billable_seconds: float,
        grace_excluded: float,
        amount: float,
    ) -> None:
        self.usage = usage
        self.credit = credit
        self.billable_seconds = billable_seconds
        self.grace_excluded = grace_excluded
        self.amount = amount


class MeteringService:
    def __init__(
        self,
        *,
        settings: Settings,
        billing_repo: Any,
        audit_repo: Any,
        outbox_repo: Any,
    ) -> None:
        self._settings = settings
        self._billing = billing_repo
        self._audit = audit_repo
        self._outbox = outbox_repo

    # ------------------------------------------------------------------
    def compute(self, lease: Lease, *, closed_at: datetime | None = None) -> RatingResult:
        """Rate a lease. Pure — no I/O, so it is trivially testable."""
        end = closed_at or lease.closed_at or lease.stopped_at or utcnow()

        if lease.running_at is None:
            # Rule 2: never ran, never charged.
            return RatingResult(None, None, 0.0, 0.0, 0.0)

        # Rule 1: the meter stops when the notice is issued.
        billable_end = lease.notice_at or lease.stopped_at or end
        billable_seconds = max(
            0.0, (billable_end - lease.running_at).total_seconds()
        )

        grace_excluded = 0.0
        if lease.notice_at is not None:
            grace_end = lease.stopped_at or end
            grace_excluded = max(0.0, (grace_end - lease.notice_at).total_seconds())

        amount = round(billable_seconds * lease.rate_per_sec, 6)

        usage = UsageRecord(
            lease_id=lease.lease_id,
            tenant_id=lease.tenant_id,
            window_start=lease.running_at,
            window_end=billable_end,
            units=lease.units,
            billable_seconds=round(billable_seconds, 3),
            rate_per_sec=lease.rate_per_sec,
            discount=lease.discount_snapshot,
            amount=amount,
            grace_seconds_excluded=round(grace_excluded, 3),
        )

        credit: CreditRecord | None = None
        if lease.notice_at is not None and not lease.notice_channels_delivered:
            # Rule 3, with the floor: at least one grace window at this lease's
            # own rate. A credit of zero is not a credit, and a lease killed
            # without warning one second into its life would earn exactly that
            # from the billed amount alone.
            notice_value = round(lease.grace_seconds * lease.rate_per_sec, 6)
            credit = CreditRecord(
                credit_id=new_id("credit"),
                lease_id=lease.lease_id,
                tenant_id=lease.tenant_id,
                reason=CREDIT_NOTICE_UNDELIVERED,
                amount=max(amount, notice_value),
            )

        return RatingResult(
            usage, credit, round(billable_seconds, 3), round(grace_excluded, 3), amount
        )

    # ------------------------------------------------------------------
    async def close_lease(
        self, lease: Lease, *, conn: Any, closed_at: datetime | None = None
    ) -> RatingResult:
        """Rate and record, inside the transaction that closes the lease.

        Writing the usage record in the same transaction is what makes HLD §11's
        "invoices reproducible from the lease record alone" hold across a crash:
        there is no window in which a lease is closed but its usage was never
        computed.
        """
        result = self.compute(lease, closed_at=closed_at)

        if result.usage is not None and result.usage.billable_seconds > 0:
            await self._billing.record_usage(result.usage, conn=conn)
            await self._outbox.enqueue(
                topic=Topics.BILLING_USAGE,
                aggregate_type="lease",
                aggregate_id=lease.lease_id,
                payload={
                    "lease_id": lease.lease_id,
                    "tenant_id": lease.tenant_id,
                    "window_start": result.usage.window_start.isoformat(),
                    "window_end": result.usage.window_end.isoformat(),
                    "billable_seconds": result.usage.billable_seconds,
                    "units": lease.units,
                    "discount": lease.discount_snapshot,
                    "amount": result.amount,
                },
                conn=conn,
            )
            edge(
                log,
                25,
                f"rated {result.billable_seconds:.1f}s at {lease.discount_snapshot:.0%} "
                f"off = {result.amount:.6f} "
                f"({result.grace_excluded:.1f}s grace excluded)",
                lease_id=lease.lease_id,
                billable_seconds=result.billable_seconds,
                grace_seconds_excluded=result.grace_excluded,
                amount=result.amount,
            )

        if result.credit is not None:
            await self._billing.record_credit(result.credit, conn=conn)
            await self._outbox.enqueue(
                topic=Topics.BILLING_CREDIT,
                aggregate_type="lease",
                aggregate_id=lease.lease_id,
                payload={
                    "credit_id": result.credit.credit_id,
                    "lease_id": lease.lease_id,
                    "tenant_id": lease.tenant_id,
                    "reason": result.credit.reason,
                    "amount": result.credit.amount,
                },
                conn=conn,
            )
            await self._audit.append(
                AuditEvent.CREDIT_RAISED,
                lease_id=lease.lease_id,
                tenant_id=lease.tenant_id,
                detail={
                    "reason": result.credit.reason,
                    "amount": result.credit.amount,
                    "explanation": "no notice channel delivered; the customer "
                    "lost the instance without warning (HLD §10)",
                },
                conn=conn,
            )

        return result

    async def raise_slo_credit(
        self, lease: Lease, *, overrun_seconds: float, conn: Any
    ) -> CreditRecord | None:
        """Credit a reclaim that overran the advertised grace window.

        HLD §11 promises 99.9% of reclaims complete within the grace period,
        teardown included. When one does not, the customer was told they had
        two minutes and did not get them, so it is credited on the same
        principle as an undelivered notice — proportionally, since they did get
        *some* warning.
        """
        if overrun_seconds <= 0 or lease.billed_amount <= 0:
            return None
        share = min(1.0, overrun_seconds / max(lease.grace_seconds, 1.0))
        credit = CreditRecord(
            credit_id=new_id("credit"),
            lease_id=lease.lease_id,
            tenant_id=lease.tenant_id,
            reason=CREDIT_SLO_BREACH,
            amount=round(lease.billed_amount * share, 6),
        )
        if await self._billing.record_credit(credit, conn=conn):
            await self._audit.append(
                AuditEvent.CREDIT_RAISED,
                lease_id=lease.lease_id,
                tenant_id=lease.tenant_id,
                detail={
                    "reason": credit.reason,
                    "amount": credit.amount,
                    "overrun_seconds": round(overrun_seconds, 2),
                    "advertised_grace_seconds": lease.grace_seconds,
                },
                conn=conn,
            )
            return credit
        return None

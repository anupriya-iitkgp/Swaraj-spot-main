"""Spot Metering & Rating (edges 25, 26).

Per-second usage at the discount SNAPSHOTTED AT LEASE START. A later change to
the published discount must not re-rate a running lease.

Grace-period seconds are excluded from the bill, and an automatic credit is
raised when notice delivery failed on every channel.
"""
from __future__ import annotations

import logging

from ..bus import EventBus, Topics
from ..domain.models import Lease
from ..external.billing import BillingSystem
from .audit_log import PreemptionAuditLog

log = logging.getLogger("spot.metering")


class SpotMeteringRating:
    def __init__(self, *, billing: BillingSystem, audit: PreemptionAuditLog, bus: EventBus):
        self._billing = billing
        self._audit = audit
        self._bus = bus

    async def close_lease(self, lease: Lease) -> dict:
        """Edge 25 -> 26."""
        start = lease.running_at
        end = lease.stopped_at or lease.closed_at
        if start is None or end is None:
            # Never ran: nothing to bill. This is the cancelled-during-
            # provisioning case and it must cost the customer zero.
            lease.billed_seconds = 0.0
            lease.amount = 0.0
            record = self._record(lease)
            await self._billing.submit_usage(record)
            return record

        gross = max(0.0, end - start)
        # grace seconds are not billed
        grace = 0.0
        if lease.notice_at is not None:
            grace = max(0.0, end - lease.notice_at)
        lease.grace_seconds_excluded = grace
        lease.billed_seconds = max(0.0, gross - grace)
        lease.amount = lease.billed_seconds * lease.rate_per_sec

        record = self._record(lease)
        await self._billing.submit_usage(record)
        await self._bus.publish(Topics.USAGE_RECORD, record)

        # notice failed on every channel -> automatic credit + SLO breach
        if lease.notice_at is not None and not lease.notice_channels_delivered:
            credit = {
                "lease_id": lease.lease_id,
                "tenant_id": lease.tenant_id,
                "amount": round(lease.amount, 6),
                "reason": "termination notice not delivered on any channel",
            }
            lease.credit_raised = credit["amount"]
            await self._billing.submit_credit(credit)
            self._audit.credit(lease.lease_id, credit["amount"], credit["reason"])
            await self._bus.publish(Topics.CREDIT_RAISED, credit)

        return record

    @staticmethod
    def _record(lease: Lease) -> dict:
        return {
            "lease_id": lease.lease_id,
            "tenant_id": lease.tenant_id,
            "flavour": lease.flavour,
            "count": lease.count,
            "az": lease.az,
            "discount_snapshot": lease.discount_snapshot,
            "rate_per_sec": lease.rate_per_sec,
            "billed_seconds": round(lease.billed_seconds, 3),
            "grace_seconds_excluded": round(lease.grace_seconds_excluded, 3),
            "amount": round(lease.amount, 6),
            "preempted": lease.preemption_reason is not None,
        }

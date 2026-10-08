"""Usage records, credits, and the local mirror of the capacity ledger.

HLD §11 sets the bar for this table: "Invoices reproducible from the lease
record alone." So every rated window is stored with the discount and rate that
were actually applied, not with a foreign key to a price list that may have
changed since. HLD §6 forbids re-rating a running lease when the published
discount changes; storing the applied numbers is what makes that auditable
rather than merely intended.

The ledger mirror exists because of one line in LLD §9: the Capacity Ledger must
"never report capacity free on an unconfirmed commit". A local record of what
was sent and whether it was confirmed is what lets the teardown confirmer hold
units in RECLAIMING rather than optimistically freeing them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

import asyncpg

from ...domain.models import CreditRecord, UsageRecord
from ...logging import get_logger
from ...metrics import M

log = get_logger(__name__)

__all__ = ["BillingRepository", "LedgerRepository"]


class BillingRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def record_usage(
        self, usage: UsageRecord, *, conn: asyncpg.Connection | None = None
    ) -> bool:
        """Store one rated window. Returns False if the window already existed.

        The unique constraint on (lease_id, window_start) makes a duplicate
        impossible to create, which is a stronger guarantee than asking the
        billing system to tolerate one.
        """
        row = await (conn or self._db).fetchrow(
            """
            INSERT INTO usage_record
                (lease_id, tenant_id, window_start, window_end, units,
                 billable_seconds, rate_per_sec, discount, amount,
                 grace_seconds_excluded)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (lease_id, window_start) DO NOTHING
            RETURNING id
            """,
            usage.lease_id,
            usage.tenant_id,
            usage.window_start,
            usage.window_end,
            usage.units,
            usage.billable_seconds,
            usage.rate_per_sec,
            usage.discount,
            usage.amount,
            usage.grace_seconds_excluded,
        )
        return row is not None

    async def record_credit(
        self, credit: CreditRecord, *, conn: asyncpg.Connection | None = None
    ) -> bool:
        """Raise a credit. Unique per (lease, reason) so it cannot be raised twice."""
        row = await (conn or self._db).fetchrow(
            """
            INSERT INTO credit_record
                (credit_id, lease_id, tenant_id, reason, amount, created_at)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (lease_id, reason) DO NOTHING
            RETURNING credit_id
            """,
            credit.credit_id,
            credit.lease_id,
            credit.tenant_id,
            credit.reason,
            credit.amount,
            credit.created_at,
        )
        if row is not None:
            M.credit_total.labels(reason=credit.reason).inc()
            log.info(
                "billing.credit_raised",
                lease_id=credit.lease_id,
                tenant_id=credit.tenant_id,
                reason=credit.reason,
                amount=round(credit.amount, 6),
            )
        return row is not None

    async def unsubmitted_usage(
        self, *, limit: int = 500, conn: asyncpg.Connection | None = None
    ) -> list[asyncpg.Record]:
        return await (conn or self._db).fetch(
            """
            SELECT id, lease_id, tenant_id, window_start, window_end, units,
                   billable_seconds, rate_per_sec, discount, amount
              FROM usage_record WHERE submitted_at IS NULL
             ORDER BY created_at LIMIT $1
            """,
            limit,
        )

    async def unsubmitted_credits(
        self, *, limit: int = 500, conn: asyncpg.Connection | None = None
    ) -> list[asyncpg.Record]:
        return await (conn or self._db).fetch(
            """
            SELECT credit_id, lease_id, tenant_id, reason, amount
              FROM credit_record WHERE submitted_at IS NULL
             ORDER BY created_at LIMIT $1
            """,
            limit,
        )

    async def mark_usage_submitted(
        self, ids: Sequence[int], ref: str, *, conn: asyncpg.Connection | None = None
    ) -> None:
        if not ids:
            return
        await (conn or self._db).execute(
            """
            UPDATE usage_record SET submitted_at = now(), billing_ref = $2
             WHERE id = ANY($1::bigint[])
            """,
            list(ids),
            ref,
        )

    async def mark_credits_submitted(
        self, ids: Sequence[str], ref: str, *, conn: asyncpg.Connection | None = None
    ) -> None:
        if not ids:
            return
        await (conn or self._db).execute(
            """
            UPDATE credit_record SET submitted_at = now(), billing_ref = $2
             WHERE credit_id = ANY($1::text[])
            """,
            list(ids),
            ref,
        )

    async def invoice_for_lease(
        self, lease_id: str, *, conn: asyncpg.Connection | None = None
    ) -> dict[str, Any]:
        """Everything needed to explain one line on an invoice.

        HLD §10 wants the invoice explainable "without reconstructing logs", so
        this returns the windows, the credits and the totals together.
        """
        usage = await (conn or self._db).fetch(
            """
            SELECT window_start, window_end, units, billable_seconds,
                   rate_per_sec, discount, amount, grace_seconds_excluded
              FROM usage_record WHERE lease_id = $1 ORDER BY window_start
            """,
            lease_id,
        )
        credits = await (conn or self._db).fetch(
            """
            SELECT credit_id, reason, amount, created_at
              FROM credit_record WHERE lease_id = $1 ORDER BY created_at
            """,
            lease_id,
        )
        gross = sum(float(r["amount"]) for r in usage)
        credited = sum(float(r["amount"]) for r in credits)
        return {
            "lease_id": lease_id,
            "windows": [dict(r) for r in usage],
            "credits": [dict(r) for r in credits],
            "billable_seconds": sum(float(r["billable_seconds"]) for r in usage),
            "grace_seconds_excluded": sum(
                float(r["grace_seconds_excluded"]) for r in usage
            ),
            "gross": round(gross, 6),
            "credited": round(credited, 6),
            "net": round(gross - credited, 6),
        }


class LedgerRepository:
    """Local mirror of what was told to the out-of-scope Capacity Ledger."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def record(
        self,
        *,
        host_group: str,
        lease_id: str,
        operation: str,
        units: int,
        confirmed: bool = False,
        conn: asyncpg.Connection | None = None,
    ) -> bool:
        """Note an intent, or a confirmation.

        Idempotent per (host_group, lease, operation), matching the contract LLD
        §9 requires of the real ledger, so a retried call after a network
        timeout cannot double-count units.
        """
        row = await (conn or self._db).fetchrow(
            """
            INSERT INTO capacity_ledger_entry
                (host_group, lease_id, operation, units, confirmed, confirmed_at)
            VALUES ($1,$2,$3,$4,$5, CASE WHEN $5 THEN now() END)
            ON CONFLICT (host_group, lease_id, operation) DO NOTHING
            RETURNING id
            """,
            host_group,
            lease_id,
            operation,
            units,
            confirmed,
        )
        return row is not None

    async def confirm(
        self,
        *,
        host_group: str,
        lease_id: str,
        operation: str,
        conn: asyncpg.Connection | None = None,
    ) -> bool:
        row = await (conn or self._db).fetchrow(
            """
            UPDATE capacity_ledger_entry
               SET confirmed = true, confirmed_at = now()
             WHERE host_group = $1 AND lease_id = $2 AND operation = $3
            RETURNING id
            """,
            host_group,
            lease_id,
            operation,
        )
        return row is not None

    async def unconfirmed(
        self, *, older_than_seconds: float = 0.0,
        conn: asyncpg.Connection | None = None,
    ) -> list[asyncpg.Record]:
        """Entries the ledger has not acknowledged.

        Their units are still RECLAIMING and must never be reported free — LLD
        §11: "Ledger commit fails -> units stay RECLAIMING — never reported
        free. Retry with backoff."
        """
        return await (conn or self._db).fetch(
            """
            SELECT id, host_group, lease_id, operation, units, created_at
              FROM capacity_ledger_entry
             WHERE NOT confirmed
               AND created_at <= now() - make_interval(secs => $1::float8)
             ORDER BY created_at
            """,
            float(older_than_seconds),
        )

    async def units_by_state(
        self, *, conn: asyncpg.Connection | None = None
    ) -> dict[str, int]:
        rows = await (conn or self._db).fetch(
            """
            SELECT operation, SUM(units)::int AS units
              FROM capacity_ledger_entry GROUP BY operation
            """
        )
        return {r["operation"]: r["units"] for r in rows}

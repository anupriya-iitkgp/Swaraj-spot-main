"""EXTERNAL — Billing System (dashed box, edge 26).

Consumes rated spot usage records and credit events. Nothing more.
"""
from __future__ import annotations

import logging

log = logging.getLogger("spot.ext.billing")


class BillingSystem:
    def __init__(self):
        self.usage_records: list[dict] = []
        self.credits: list[dict] = []

    async def submit_usage(self, record: dict) -> None:
        self.usage_records.append(record)
        log.info(
            "edge 26  billing: lease %s  %.2fs  %.4f",
            record["lease_id"], record["billed_seconds"], record["amount"],
        )

    async def submit_credit(self, credit: dict) -> None:
        self.credits.append(credit)
        log.info("edge 26  billing: CREDIT lease %s %.4f", credit["lease_id"], credit["amount"])

    def invoice_for(self, tenant_id: str) -> dict:
        charges = [r for r in self.usage_records if r["tenant_id"] == tenant_id]
        credits = [c for c in self.credits if c["tenant_id"] == tenant_id]
        return {
            "tenant_id": tenant_id,
            "charges": charges,
            "credits": credits,
            "total": round(
                sum(r["amount"] for r in charges) - sum(c["amount"] for c in credits), 6
            ),
        }

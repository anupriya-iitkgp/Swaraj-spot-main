"""Billing System — edge 26, out of scope (HLD §1).

LLD §9:

    submit_usage(record), submit_credit(credit)
    Accepts duplicates safely (unique on lease_id).
    Behaviour if it breaks: queue locally and retry; never drop a usage record.

"Never drop a usage record" is why nothing in this service calls billing
synchronously. Usage and credits are written to `usage_record` / `credit_record`
inside the transaction that closes the lease, and a worker submits them
afterwards. Billing being down then delays revenue recognition instead of losing
it, and the local tables remain the reproducible source HLD §11 asks for:
"Invoices reproducible from the lease record alone."
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence

import httpx

from ..config import Settings
from ..domain.models import CreditRecord, UsageRecord
from ..logging import edge, get_logger
from .base import ExternalCaller, ExternalError

log = get_logger(__name__)

__all__ = ["BillingSystem", "SimulatedBillingSystem", "HttpBillingSystem"]


class BillingSystem(Protocol):
    async def submit_usage(self, records: Sequence[dict[str, Any]]) -> str: ...
    async def submit_credit(self, credits: Sequence[dict[str, Any]]) -> str: ...


class SimulatedBillingSystem:
    """Accepts batches and dedupes them, as the real system is required to."""

    def __init__(self) -> None:
        self.usage: dict[str, dict[str, Any]] = {}
        self.credits: dict[str, dict[str, Any]] = {}
        self._batch = 0
        self._fail_next = 0

    def fail_next(self, times: int = 1) -> None:
        self._fail_next = times

    def _maybe_fail(self, operation: str) -> None:
        if self._fail_next > 0:
            self._fail_next -= 1
            raise ExternalError("billing", operation, "injected failure")

    async def submit_usage(self, records: Sequence[dict[str, Any]]) -> str:
        self._maybe_fail("submit_usage")
        self._batch += 1
        ref = f"batch-{self._batch:06d}"
        for record in records:
            key = f"{record['lease_id']}:{record['window_start']}"
            self.usage.setdefault(key, {**record, "billing_ref": ref})
        edge(log, 26, f"submitted {len(records)} usage record(s)", batch=ref)
        return ref

    async def submit_credit(self, credits: Sequence[dict[str, Any]]) -> str:
        self._maybe_fail("submit_credit")
        self._batch += 1
        ref = f"credit-{self._batch:06d}"
        for credit in credits:
            self.credits.setdefault(credit["credit_id"], {**credit, "billing_ref": ref})
        edge(log, 26, f"submitted {len(credits)} credit(s)", batch=ref)
        return ref

    def total_billed(self) -> float:
        return round(sum(float(r["amount"]) for r in self.usage.values()), 6)

    def total_credited(self) -> float:
        return round(sum(float(c["amount"]) for c in self.credits.values()), 6)


class HttpBillingSystem:
    """Live backend: the billing system's usage ingest."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._base = (settings.billing_url or "").rstrip("/")
        self._caller = ExternalCaller("billing", settings)

    async def submit_usage(self, records: Sequence[dict[str, Any]]) -> str:
        return await self._caller.call(
            "submit_usage", lambda: self._post("usage", {"records": list(records)}),
            timeout=30.0,
        )

    async def submit_credit(self, credits: Sequence[dict[str, Any]]) -> str:
        return await self._caller.call(
            "submit_credit", lambda: self._post("credits", {"credits": list(credits)}),
            timeout=30.0,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> str:
        response = await self._client.post(f"{self._base}/{path}", json=payload)
        if response.status_code >= 500:
            raise ExternalError("billing", path, f"HTTP {response.status_code}")
        if response.status_code >= 400:
            # Still retryable-by-worker: the records stay unsubmitted locally and
            # an operator sees them in the ops endpoint rather than losing them.
            raise ExternalError(
                "billing", path,
                f"HTTP {response.status_code}: {response.text[:200]}",
                retryable=False,
            )
        return str(response.json().get("batch_ref", "accepted"))

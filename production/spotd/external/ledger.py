"""Capacity Ledger — edge 14, out of scope (HLD §1).

LLD §9:

    record_spot_allocated, mark_reclaiming, commit_capacity_returned, release_spot
    Idempotent per (host_group, lease, operation).
    Behaviour if it breaks: retry; never report capacity free on an unconfirmed
    commit.

That last clause is the one this file exists to honour. `commit_capacity_returned`
is the moment the guaranteed classes are told they can have the capacity back.
If it is reported optimistically and the commit actually failed, the ledger
believes capacity exists that is still occupied by a spot instance — and the
next reserved-class launch fails, which is exactly the SLA breach HLD §12 warns
about. So a failed commit leaves the units in RECLAIMING and raises; the caller
holds the lease in STOPPED rather than closing it.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from ..config import Settings
from ..logging import edge, get_logger
from .base import ExternalCaller, ExternalError

log = get_logger(__name__)

__all__ = ["CapacityLedger", "SimulatedCapacityLedger", "HttpCapacityLedger"]


class CapacityLedger(Protocol):
    async def record_spot_allocated(
        self, *, host_group: str, lease_id: str, units: int
    ) -> None: ...

    async def mark_reclaiming(
        self, *, host_group: str, lease_id: str, units: int
    ) -> None: ...

    async def commit_capacity_returned(
        self, *, host_group: str, lease_id: str, units: int
    ) -> None: ...

    async def release_spot(
        self, *, host_group: str, lease_id: str, units: int
    ) -> None: ...


class SimulatedCapacityLedger:
    """In-memory ledger with the same idempotency guarantee as the real one.

    Keyed on (host_group, lease_id, operation) exactly as LLD §9 requires, so a
    retry after a timeout is absorbed rather than double-counted. A failure
    probability can be injected to exercise the "never report free on an
    unconfirmed commit" path in tests without waiting for a real outage.
    """

    def __init__(self) -> None:
        self._applied: set[tuple[str, str, str]] = set()
        self._units: dict[str, dict[str, int]] = {}
        self._fail_next: dict[str, int] = {}

    def fail_next(self, operation: str, times: int = 1) -> None:
        """Test/chaos hook: make the next N calls to `operation` fail."""
        self._fail_next[operation] = times

    def _maybe_fail(self, operation: str) -> None:
        remaining = self._fail_next.get(operation, 0)
        if remaining > 0:
            self._fail_next[operation] = remaining - 1
            raise ExternalError("capacity_ledger", operation, "injected failure")

    def _apply(self, host_group: str, lease_id: str, operation: str, units: int) -> bool:
        key = (host_group, lease_id, operation)
        if key in self._applied:
            return False
        self._applied.add(key)
        bucket = self._units.setdefault(host_group, {})
        bucket[operation] = bucket.get(operation, 0) + units
        return True

    async def record_spot_allocated(
        self, *, host_group: str, lease_id: str, units: int
    ) -> None:
        self._maybe_fail("record_spot_allocated")
        self._apply(host_group, lease_id, "allocated", units)

    async def mark_reclaiming(
        self, *, host_group: str, lease_id: str, units: int
    ) -> None:
        self._maybe_fail("mark_reclaiming")
        self._apply(host_group, lease_id, "reclaiming", units)

    async def commit_capacity_returned(
        self, *, host_group: str, lease_id: str, units: int
    ) -> None:
        self._maybe_fail("commit_capacity_returned")
        applied = self._apply(host_group, lease_id, "returned", units)
        if applied:
            edge(
                log,
                14,
                f"capacity returned: {units}u on {host_group}",
                lease_id=lease_id,
                host_group=host_group,
                units=units,
            )

    async def release_spot(self, *, host_group: str, lease_id: str, units: int) -> None:
        self._maybe_fail("release_spot")
        self._apply(host_group, lease_id, "released", units)

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {hg: dict(ops) for hg, ops in self._units.items()}


class HttpCapacityLedger:
    """Live backend: the real ledger service."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._base = (settings.ledger_url or "").rstrip("/")
        self._caller = ExternalCaller("capacity_ledger", settings)

    async def _post(
        self, operation: str, host_group: str, lease_id: str, units: int
    ) -> None:
        async def send() -> None:
            response = await self._client.post(
                f"{self._base}/capacity/{operation}",
                json={
                    "host_group": host_group,
                    "lease_id": lease_id,
                    "units": units,
                    # The ledger dedupes on this; see LLD §9.
                    "idempotency_key": f"{host_group}:{lease_id}:{operation}",
                },
            )
            if response.status_code >= 500:
                raise ExternalError(
                    "capacity_ledger", operation, f"HTTP {response.status_code}"
                )
            if response.status_code >= 400:
                raise ExternalError(
                    "capacity_ledger", operation,
                    f"HTTP {response.status_code}: {response.text[:200]}",
                    retryable=False,
                )

        await self._caller.call(operation, send)

    async def record_spot_allocated(
        self, *, host_group: str, lease_id: str, units: int
    ) -> None:
        await self._post("allocated", host_group, lease_id, units)

    async def mark_reclaiming(
        self, *, host_group: str, lease_id: str, units: int
    ) -> None:
        await self._post("reclaiming", host_group, lease_id, units)

    async def commit_capacity_returned(
        self, *, host_group: str, lease_id: str, units: int
    ) -> None:
        await self._post("returned", host_group, lease_id, units)
        edge(
            log, 14, f"capacity returned: {units}u on {host_group}",
            lease_id=lease_id, host_group=host_group, units=units,
        )

    async def release_spot(self, *, host_group: str, lease_id: str, units: int) -> None:
        await self._post("released", host_group, lease_id, units)

"""Account Service — edge 1, out of scope (HLD §1).

LLD §9 gives the contract exactly:

    get_account_class(tenant) -> AccountClass | None
    p99 < 20 ms; unknown tenant returns None, never a guess.
    Behaviour if it breaks: fail closed: 403.

All three clauses are load-bearing.

* `None` for unknown, never a guess — this is the only thing standing between a
  typo'd tenant id and a stranger's capacity. There is no default class
  anywhere in this file.
* p99 < 20 ms — it is on the request path, inside a 200 ms admission budget, so
  a short TTL cache sits in front of it. The cache is small and time-bounded
  rather than clever: entitlement changes must take effect quickly, so a stale
  entry is a real risk and the TTL is measured in seconds.
* Fail closed — when the service is unreachable, this returns None and the
  caller answers 403. The tempting alternative, "assume the last known class",
  means an outage in an unrelated service silently grants spot access.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx

from ..config import Settings
from ..domain.models import AccountClass
from ..logging import get_logger
from .base import ExternalCaller, ExternalError

log = get_logger(__name__)

__all__ = ["AccountService", "DatabaseAccountService", "HttpAccountService"]

#: Deliberately short. An entitlement revocation that takes a minute to apply is
#: a security problem; one that takes five seconds is a cache.
_CACHE_TTL = 5.0


class AccountService(Protocol):
    async def get_account_class(self, tenant_id: str) -> AccountClass | None:
        """Return the tenant's class, or None if unknown or unverifiable."""
        ...


class _TtlCache:
    __slots__ = ("_entries", "_ttl", "_max")

    def __init__(self, ttl: float, max_entries: int = 50_000) -> None:
        self._entries: dict[str, tuple[float, AccountClass | None]] = {}
        self._ttl = ttl
        self._max = max_entries

    def get(self, key: str) -> tuple[bool, AccountClass | None]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        expires_at, value = entry
        if expires_at < time.monotonic():
            self._entries.pop(key, None)
            return False, None
        return True, value

    def put(self, key: str, value: AccountClass | None) -> None:
        if len(self._entries) >= self._max:
            # Bounded, and cheap: drop the oldest tenth rather than tracking LRU
            # order for a cache whose entries live five seconds anyway.
            for stale in list(self._entries)[: self._max // 10]:
                self._entries.pop(stale, None)
        self._entries[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        self._entries.clear()


class DatabaseAccountService:
    """Simulator backend: entitlement from the seeded `tenant` table.

    This is the `SPOT_BACKEND=sim` implementation. It behaves like the real
    thing in the ways that matter — unknown tenants return None, an inactive
    tenant is not entitled — so every code path downstream is exercised the same
    way it would be against the live service.
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._cache = _TtlCache(_CACHE_TTL)

    async def get_account_class(self, tenant_id: str) -> AccountClass | None:
        hit, cached = self._cache.get(tenant_id)
        if hit:
            return cached

        row = await self._db.fetchrow(
            "SELECT account_class, active FROM tenant WHERE tenant_id = $1",
            tenant_id,
        )
        if row is None or not row["active"]:
            # An inactive account is indistinguishable from an unknown one at
            # this boundary. Both mean "not entitled", and saying which would
            # tell an unauthenticated prober that the tenant id exists.
            self._cache.put(tenant_id, None)
            return None

        account_class = AccountClass(row["account_class"])
        self._cache.put(tenant_id, account_class)
        return account_class


class HttpAccountService:
    """Live backend: the identity/IAM service over HTTP."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._base = (settings.account_service_url or "").rstrip("/")
        self._caller = ExternalCaller("account_service", settings)
        self._cache = _TtlCache(_CACHE_TTL)

    async def get_account_class(self, tenant_id: str) -> AccountClass | None:
        hit, cached = self._cache.get(tenant_id)
        if hit:
            return cached

        try:
            value = await self._caller.call(
                "get_account_class",
                lambda: self._fetch(tenant_id),
                # Tight, because this sits inside the 200 ms admission budget
                # and the contract promises a 20 ms p99.
                timeout=min(self._settings.external_timeout, 0.5),
            )
        except ExternalError as exc:
            # Fail closed. The caller turns None into 403.
            log.warning(
                "account_service.unavailable",
                tenant_id=tenant_id,
                error=str(exc),
                note="failing closed per LLD §9 — request will be rejected 403",
            )
            return None

        self._cache.put(tenant_id, value)
        return value

    async def _fetch(self, tenant_id: str) -> AccountClass | None:
        response = await self._client.get(f"{self._base}/accounts/{tenant_id}/class")
        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            raise ExternalError(
                "account_service", "get_account_class",
                f"HTTP {response.status_code}", retryable=True,
            )
        if response.status_code >= 400:
            raise ExternalError(
                "account_service", "get_account_class",
                f"HTTP {response.status_code}", retryable=False,
            )
        payload = response.json()
        raw = payload.get("account_class")
        if raw not in {c.value for c in AccountClass}:
            # An unrecognised class is not a guess we are allowed to make.
            log.error(
                "account_service.unknown_class",
                tenant_id=tenant_id,
                received=raw,
                note="treated as unknown; never inferred",
            )
            return None
        return AccountClass(raw)

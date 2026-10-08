"""Forecast & Headroom — edges 18 and 19, out of scope (HLD §1).

This is the interface HLD §12 is most worried about:

    "The sellable-spot number is an input, not a fact. If forecasting hands over
    an optimistic number, this subsystem faithfully sells capacity that does not
    exist and the failure surfaces as customer-visible 409s or, worse,
    guaranteed-class SLA breaches."

    "Require a confidence signal alongside the number, and degrade to a
    conservative floor when the feed is stale or the confidence is low. Never
    extrapolate the last known value."

Which is why `SellableFeed` carries `confidence` and `published_at` as required
fields rather than optional metadata: a feed that cannot say how sure it is
cannot be trusted with the size of the pool. The *decision* about whether to
trust a given publication lives in `core.pool_view`; this module's job is to
obtain the number honestly, including reporting a stale `published_at` when the
upstream really is stale.

The simulator drives its numbers from `seed.trace`, the same pure function the
seed command materialises, so the running service and the stored trace never
disagree.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from ..config import Settings
from ..domain.models import SellableFeed, utcnow
from ..logging import get_logger
from ..seed.trace import trace_point
from .base import ExternalCaller, ExternalError

log = get_logger(__name__)

__all__ = ["ForecastFeed", "SimulatedForecastFeed", "HttpForecastFeed"]


class ForecastFeed(Protocol):
    async def sellable(self, az: str) -> SellableFeed:
        """Latest publication for an AZ, with its confidence and publication time."""
        ...


class SimulatedForecastFeed:
    """Simulator backend, driven by the synthetic trace.

    Also supports a manual override, which is how the proactive reclaim path is
    demonstrated: raising headroom shrinks the sellable number, the pool notices
    on its next refresh, and the capacity side issues a reclaim order — with the
    grace window spent *before* a guaranteed-class customer is waiting on the
    capacity, which is the whole point of doing it on a forecast rather than on
    demand.
    """

    def __init__(self, db: Any, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._overrides: dict[str, int] = {}
        self._capacity: dict[str, int] = {}

    async def _az_capacity(self, az: str) -> int:
        """Physical capacity of the AZ, from the seeded host groups."""
        if az not in self._capacity:
            units = await self._db.fetchval(
                """
                SELECT COALESCE(SUM(total_units), 0)::int FROM host_group
                 WHERE az = $1 AND NOT quarantined
                """,
                az,
            )
            self._capacity[az] = int(units or 0)
        return self._capacity[az]

    def invalidate_capacity(self) -> None:
        """Drop the cached physical capacity after a quarantine or a re-seed."""
        self._capacity.clear()

    def set_override(self, az: str, units: int | None) -> None:
        """Pin the sellable number, or clear the pin with None."""
        if units is None:
            self._overrides.pop(az, None)
            log.info("forecast.override_cleared", az=az)
        else:
            self._overrides[az] = max(0, units)
            log.info("forecast.override_set", az=az, units=units)

    async def sellable(self, az: str) -> SellableFeed:
        now = utcnow()
        if az in self._overrides:
            return SellableFeed(
                az=az,
                units=self._overrides[az],
                confidence=0.99,
                published_at=now,
                horizon_seconds=900.0,
            )

        capacity = await self._az_capacity(az)
        point = trace_point(az, now)
        return SellableFeed(
            az=az,
            units=int(capacity * point.fraction),
            confidence=point.confidence,
            published_at=point.published_at,
            horizon_seconds=point.horizon_seconds,
        )


class HttpForecastFeed:
    """Live backend: the capacity side's sellable-spot feed."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._base = (settings.forecast_url or "").rstrip("/")
        self._caller = ExternalCaller("forecast", settings)

    async def sellable(self, az: str) -> SellableFeed:
        return await self._caller.call("sellable", lambda: self._fetch(az))

    async def _fetch(self, az: str) -> SellableFeed:
        response = await self._client.get(f"{self._base}/headroom/{az}/sellable")
        if response.status_code >= 500:
            raise ExternalError("forecast", "sellable", f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ExternalError(
                "forecast", "sellable", f"HTTP {response.status_code}", retryable=False
            )
        payload = response.json()

        missing = {"units", "confidence", "published_at"} - payload.keys()
        if missing:
            # HLD §12 requires the confidence signal. A feed that omits it is
            # rejected rather than defaulted: guessing a confidence would defeat
            # the entire purpose of asking for one.
            raise ExternalError(
                "forecast",
                "sellable",
                f"publication for {az} is missing required fields {sorted(missing)}; "
                f"HLD §12 requires a confidence signal alongside the number",
                retryable=False,
            )

        published_at = payload["published_at"]
        if isinstance(published_at, str):
            published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)

        return SellableFeed(
            az=az,
            units=max(0, int(payload["units"])),
            confidence=max(0.0, min(1.0, float(payload["confidence"]))),
            published_at=published_at,
            horizon_seconds=float(payload.get("horizon_seconds", 0.0)),
        )

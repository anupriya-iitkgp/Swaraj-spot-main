"""Spot Pool View — edges 6, 19, 20 and 31.

HLD §6:

    Owns: A cached projection of sellable spot per flavour and AZ, refreshed
          each control cycle.
    Must not do: Be treated as authoritative — it is stale by design.

The second line is the interesting one. This component is *allowed* to be wrong,
and the design absorbs that by making the read a hint and the reserve the
decision (HLD §7). Nothing here needs to be exact. What it must be is honest:
never optimistic, never extrapolated, and always able to say how stale it is.

That honesty is the whole content of `_decide()`. HLD §12 sets the rule:

    "Require a confidence signal alongside the number, and degrade to a
    conservative floor when the feed is stale or the confidence is low. Never
    extrapolate the last known value."

There are exactly three outcomes for a publication, and the difference between
the second and third is the part that is easy to get wrong:

  * **trusted** — fresh and confident: apply it as published.
  * **degraded** — stale or low-confidence: apply `degraded_factor` of *the
    number that was just published*, not of the last good one. Scaling the last
    known value would be extrapolation wearing a haircut.
  * **capped** — trusted, but larger than the last accepted number while the
    feed is recovering: growth is held back for a cycle. A feed that has just
    come back from a stale window is exactly when an optimistic number is most
    dangerous, and HLD §12 asks to "stop growing the pool" until it recovers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..config import Settings
from ..domain.models import Flavour, PoolSnapshot, SellableFeed, utcnow
from ..external.forecast import ForecastFeed
from ..logging import edge, get_logger
from ..metrics import M

log = get_logger(__name__)

__all__ = ["SpotPoolView", "FeedDecision", "FlavourAvailability"]


@dataclass(frozen=True, slots=True)
class FeedDecision:
    applied_units: int
    degraded: bool
    accepted: bool
    reason: str


@dataclass(frozen=True, slots=True)
class FlavourAvailability:
    """The per-flavour projection customers see on the inventory endpoint."""

    flavour: str
    vcpu: int
    available_instances: int
    az: str


class SpotPoolView:
    def __init__(
        self,
        *,
        settings: Settings,
        pool_repo: Any,
        forecast: ForecastFeed,
        audit: Any,
    ) -> None:
        self._settings = settings
        self._pool = pool_repo
        self._forecast = forecast
        self._audit = audit
        #: Last number actually applied per AZ, used only to cap growth during
        #: recovery — never to fill in a missing publication.
        self._last_accepted: dict[str, int] = {}
        self._consecutive_degraded: dict[str, int] = {}

    # ------------------------------------------------------------------
    # edge 19 — one control cycle
    # ------------------------------------------------------------------
    async def refresh(self, az: str) -> PoolSnapshot:
        """Pull one publication and apply it under the degradation policy."""
        feed = await self._forecast.sellable(az)
        decision = self._decide(feed)

        snapshot = await self._pool.refresh(
            feed,
            accepted=decision.accepted,
            applied_units=decision.applied_units,
            degraded=decision.degraded,
            reject_reason=None if decision.accepted else decision.reason,
        )

        if decision.degraded:
            streak = self._consecutive_degraded.get(az, 0) + 1
            self._consecutive_degraded[az] = streak
            # §14.1 alerts on spot_pool_degraded == 1 for more than 3 cycles.
            # Auditing the transition, not every cycle, keeps the log readable.
            if streak == 1:
                await self._audit.append(
                    "pool.degraded",
                    detail={
                        "az": az,
                        "reason": decision.reason,
                        "feed_units": feed.units,
                        "applied_units": decision.applied_units,
                        "confidence": feed.confidence,
                        "feed_age_seconds": round(feed.age_seconds(), 1),
                    },
                )
            elif streak > 3:
                log.error(
                    "pool.degraded_sustained",
                    az=az,
                    cycles=streak,
                    reason=decision.reason,
                    note="HLD §12: the sellable number is an input, not a fact — "
                    "the capacity side's feed needs attention",
                )
        else:
            if self._consecutive_degraded.pop(az, 0):
                log.info("pool.recovered", az=az, applied_units=decision.applied_units)
            self._last_accepted[az] = decision.applied_units

        return snapshot

    async def refresh_all(self) -> list[PoolSnapshot]:
        return [await self.refresh(az) for az in self._settings.availability_zones]

    def _decide(self, feed: SellableFeed) -> FeedDecision:
        s = self._settings
        now = utcnow()
        stale = feed.is_stale(s.control_cycle, s.feed_stale_cycles, now)
        low_confidence = feed.confidence < s.min_feed_confidence

        if stale or low_confidence:
            reason = (
                f"feed is {feed.age_seconds(now):.0f}s old "
                f"(> {s.control_cycle * s.feed_stale_cycles:.0f}s)"
                if stale
                else f"confidence {feed.confidence:.2f} < {s.min_feed_confidence:.2f}"
            )
            # Degrade the number that was just published. Applying the factor to
            # the last *good* number would be extrapolation, which HLD §12
            # forbids in as many words.
            return FeedDecision(
                applied_units=int(feed.units * s.degraded_factor),
                degraded=True,
                accepted=False,
                reason=reason,
            )

        previous = self._last_accepted.get(feed.az)
        if previous is not None and feed.units > previous and self._consecutive_degraded.get(feed.az):
            return FeedDecision(
                applied_units=previous,
                degraded=False,
                accepted=True,
                reason="growth held for one cycle while the feed recovers",
            )

        return FeedDecision(
            applied_units=feed.units, degraded=False, accepted=True, reason="trusted"
        )

    # ------------------------------------------------------------------
    # edge 6 — getSellable
    # ------------------------------------------------------------------
    async def get(self, az: str) -> PoolSnapshot | None:
        return await self._pool.get(az)

    async def all(self) -> list[PoolSnapshot]:
        return await self._pool.all()

    async def get_sellable(
        self, az: str, flavours: Sequence[Flavour]
    ) -> list[FlavourAvailability]:
        """The per-flavour projection.

        Capacity is fungible vCPU within an AZ, so this divides rather than
        reading a per-flavour row — see the note on `spot_pool` in migration
        0001 for why storing it per flavour would be wrong.
        """
        snapshot = await self._pool.get(az)
        if snapshot is None:
            return []
        return [
            FlavourAvailability(
                flavour=f.name,
                vcpu=f.vcpu,
                available_instances=snapshot.capacity_for(f),
                az=az,
            )
            for f in flavours
            if f.spot_eligible
        ]

    async def alternatives(
        self, az: str, flavour: Flavour, count: int
    ) -> list[dict[str, Any]]:
        """Where the request *could* be served right now.

        HLD §11 wants a 409 to be actionable, and §12 recommends a capacity-watch
        subscription so tenants "wait on an event instead of polling". Until they
        adopt that, telling them which AZ has room converts a retry loop into a
        single redirected request.
        """
        needed = flavour.units(count)
        out: list[dict[str, Any]] = []
        for snapshot in await self._pool.all():
            if snapshot.az == az or snapshot.available_units < needed:
                continue
            out.append(
                {
                    "az": snapshot.az,
                    "available_units": snapshot.available_units,
                    "available_instances": snapshot.capacity_for(flavour),
                }
            )
        return sorted(out, key=lambda a: -a["available_units"])[:3]

    # ------------------------------------------------------------------
    # edge 20 — shrink, before victims are selected
    # ------------------------------------------------------------------
    async def shrink(self, az: str, units: int, *, conn: Any = None) -> int:
        removed = await self._pool.shrink(az, units, conn=conn)
        snapshot = await self._pool.get(az, conn=conn)
        if snapshot is not None:
            M.pool_available_units.labels(az=az).set(snapshot.available_units)
        # Shrinking invalidates the recovery baseline: the pool is genuinely
        # smaller now, and the next publication should not be capped against a
        # number that included capacity we no longer have.
        self._last_accepted.pop(az, None)
        return removed

    async def ensure(self) -> None:
        await self._pool.ensure(self._settings.availability_zones)
        M.reset_pool_gauges(self._settings.availability_zones)

    async def expire_cooldowns(self) -> dict[str, int]:
        released = await self._pool.expire_cooldowns()
        if released:
            await self._pool.publish_gauges()
        return released

    async def staleness(self) -> dict[str, float]:
        """Per-AZ feed age. HLD §11: staleness must stay within one control cycle."""
        return {s.az: round(s.staleness_seconds(), 2) for s in await self._pool.all()}

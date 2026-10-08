"""Spot pricing — the discount curve.

HLD §10 fixes one rule about pricing and this module exists to make it
structurally true rather than merely observed:

    "discount_snapshot, rate_per_sec — Frozen at lease start. A later change to
    the published discount must not re-rate a running lease."

So `quote()` is a pure function of a pool snapshot, and its output is written
onto the lease at creation and never consulted again. Nothing in the rating path
can reach back to a live price.

The curve keys off *surplus depth* — how much of the sellable pool is currently
unsold. A pool that is nearly empty is worth close to list price; a pool that is
mostly idle is discounted hard to move it. That is the same signal the capacity
side is reacting to, so price and reclaim risk move together, which is what
makes the published interruption rate meaningful next to the price.

The curve is concave (square root) rather than linear. Linear interpolation
spends most of the discount band on the shallow-surplus region where capacity is
scarce and the discount is doing no work; a concave curve reaches a meaningful
discount as soon as there is real surplus, then flattens.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import Settings
from ..domain.models import Flavour, PoolSnapshot

__all__ = ["Quote", "PricingEngine"]


@dataclass(frozen=True, slots=True)
class Quote:
    """A price, and enough context to explain it on an invoice."""

    discount: float
    rate_per_sec: float
    list_rate_per_sec: float
    surplus_depth: float
    degraded: bool

    @property
    def hourly(self) -> float:
        return self.rate_per_sec * 3600.0

    @property
    def list_hourly(self) -> float:
        return self.list_rate_per_sec * 3600.0

    @property
    def saving_pct(self) -> float:
        return round(self.discount * 100.0, 1)


class PricingEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def discount_for(self, pool: PoolSnapshot) -> tuple[float, float]:
        """(discount, surplus_depth) for the pool as it stands right now."""
        s = self._settings
        if pool.sellable_units <= 0:
            # Nothing for sale. The minimum discount is still the honest answer:
            # it is what the next unit would cost, not a statement that any
            # exists.
            return s.min_discount, 0.0

        depth = max(0.0, min(1.0, pool.available_units / pool.sellable_units))
        curve = math.sqrt(depth)
        discount = s.min_discount + (s.max_discount - s.min_discount) * curve

        if pool.degraded:
            # The feed is stale or low-confidence, so the surplus number driving
            # this curve is not trustworthy. Quoting the deep discount it
            # implies would sell capacity cheaply on the strength of a number
            # the design explicitly says not to trust (HLD §12).
            discount = min(discount, s.min_discount + (s.max_discount - s.min_discount) * 0.25)

        return round(min(s.max_discount, max(s.min_discount, discount)), 4), round(depth, 4)

    def quote(self, pool: PoolSnapshot, flavour: Flavour, count: int) -> Quote:
        """Price one launch. The result is snapshotted onto the lease."""
        discount, depth = self.discount_for(pool)
        units = flavour.units(count)
        list_rate = self._settings.base_rate_per_unit_sec * units
        return Quote(
            discount=discount,
            rate_per_sec=round(list_rate * (1.0 - discount), 12),
            list_rate_per_sec=round(list_rate, 12),
            surplus_depth=depth,
            degraded=pool.degraded,
        )

    def indicative(self, pool: PoolSnapshot, flavour: Flavour) -> Quote:
        """Price shown on the inventory endpoint, for one instance."""
        return self.quote(pool, flavour, 1)

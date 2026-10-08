"""EXTERNAL — Forecast & Headroom, the capacity side (dashed box, edges 18/19).

Publishes the sellable-spot number every control cycle. This subsystem consumes
it and never recomputes it.

Per the HLD risk table the feed carries a *confidence* signal, and the consumer
degrades to a conservative floor when confidence is low or the feed is stale.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .capacity_ledger import CapacityLedger

log = logging.getLogger("spot.ext.forecast")


@dataclass(frozen=True)
class SellableSpot:
    az: str
    units: int
    confidence: float  # 0..1
    published_at: float
    horizon_seconds: float

    @property
    def age(self) -> float:
        return time.time() - self.published_at


class ForecastHeadroom:
    """Simulated capacity side.

    sellable = free capacity - forecast headroom for dynamic burst.
    `headroom_units` is the knob a test or the demo drives to trigger reclaim.
    """

    def __init__(self, ledger: CapacityLedger, horizon_seconds: float = 30.0):
        self._ledger = ledger
        self._horizon = horizon_seconds
        self.headroom_units: dict[str, int] = {"az-1": 8, "az-2": 4}
        self.confidence: float = 0.9

    def set_headroom(self, az: str, units: int) -> None:
        """Raising this is what makes capacity need to come back (edge 18)."""
        self.headroom_units[az] = units
        log.info("edge 19  forecast: headroom for %s -> %d units", az, units)

    def sellable(self, az: str) -> SellableSpot:
        free = self._ledger.free_units(az)
        units = max(0, free - self.headroom_units.get(az, 0))
        return SellableSpot(
            az=az,
            units=units,
            confidence=self.confidence,
            published_at=time.time(),
            horizon_seconds=self._horizon,
        )

    def protected_shortfall(self, az: str, currently_sold: int) -> int:
        """How many units of live spot must be reclaimed to restore headroom.

        Positive result = the capacity side will issue a reclaim order.
        """
        free = self._ledger.free_units(az)
        needed = self.headroom_units.get(az, 0)
        if free >= needed:
            return 0
        return min(currently_sold, needed - free)

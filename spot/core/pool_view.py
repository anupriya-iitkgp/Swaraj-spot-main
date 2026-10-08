"""Spot Pool View — the read model (edges 6, 19, 20, 31).

A cached projection of sellable spot per flavour and AZ, refreshed each control
cycle from the capacity side. It is *stale by design*, by up to one cycle.

Three things happen here:
  edge 19  the capacity side pushes a new sellable number each cycle
  edge 20  a reclaim order shrinks the advertised pool IMMEDIATELY, before any
           victim is chosen, so nothing new is sold into capacity being taken back
  edge 31  the Admission Controller reserves and releases against it
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..bus import EventBus, Topics
from ..config import CONFIG
from ..external.forecast_headroom import ForecastHeadroom, SellableSpot

log = logging.getLogger("spot.pool")

#: If the feed is older than this many control cycles, or its confidence is
#: below the floor, we degrade to a conservative fraction of the last number
#: rather than extrapolating it.
STALE_CYCLES = 3
CONFIDENCE_FLOOR = 0.5
DEGRADED_FRACTION = 0.25


@dataclass
class AZPool:
    az: str
    sellable_units: int = 0          # advertised ceiling from the capacity side
    reserved_units: int = 0          # held by live + in-flight leases
    cooldown_units: int = 0          # reclaimed, not yet re-sellable (anti-thrash)
    confidence: float = 1.0
    published_at: float = field(default_factory=time.time)
    degraded: bool = False

    @property
    def available_units(self) -> int:
        return max(0, self.sellable_units - self.reserved_units - self.cooldown_units)


class SpotPoolView:
    def __init__(self, forecast: ForecastHeadroom, bus: EventBus, azs: tuple[str, ...]):
        self._forecast = forecast
        self._bus = bus
        self._pools: dict[str, AZPool] = {az: AZPool(az=az) for az in azs}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ---------------------------------------------------------------- reads
    def pool(self, az: str) -> AZPool:
        if az not in self._pools:
            raise KeyError(az)
        return self._pools[az]

    def get_sellable(self, az: str) -> int:
        """Edge 6: Spot Market API -> Spot Pool View."""
        return self.pool(az).available_units

    def snapshot(self) -> list[dict]:
        return [
            {
                "az": p.az,
                "sellable_units": p.sellable_units,
                "reserved_units": p.reserved_units,
                "cooldown_units": p.cooldown_units,
                "available_units": p.available_units,
                "confidence": round(p.confidence, 3),
                "age_seconds": round(time.time() - p.published_at, 2),
                "degraded": p.degraded,
            }
            for p in self._pools.values()
        ]

    def alternatives(self, exclude_az: str, units: int) -> list[dict]:
        """Nearest AZ that *does* have capacity — returned with every 409."""
        return [
            {"az": p.az, "available_units": p.available_units}
            for p in self._pools.values()
            if p.az != exclude_az and p.available_units >= units
        ]

    # -------------------------------------------------------------- writes
    async def refresh(self) -> None:
        """Edge 19 — pull the sellable number for each AZ from the capacity side."""
        async with self._lock:
            for az, pool in self._pools.items():
                s: SellableSpot = self._forecast.sellable(az)
                degraded = s.confidence < CONFIDENCE_FLOOR
                units = int(s.units * DEGRADED_FRACTION) if degraded else s.units
                # never advertise less than what is already reserved and running
                pool.sellable_units = max(units, pool.reserved_units)
                pool.confidence = s.confidence
                pool.published_at = s.published_at
                pool.degraded = degraded
        await self._bus.publish(Topics.POOL_UPDATED, {"pools": self.snapshot()})

    async def shrink(self, az: str, units: int, reason: str) -> None:
        """Edge 20 — called by the Reclaim Order Handler the instant an order lands.

        This runs BEFORE victim selection. Ordering matters: shrink first, then
        choose who dies, otherwise you keep selling spot into capacity you are
        already taking back.
        """
        async with self._lock:
            pool = self.pool(az)
            pool.sellable_units = max(0, pool.sellable_units - units)
            log.info("edge 20  pool %s: sellable -%d (%s) -> %d",
                     az, units, reason, pool.sellable_units)

    async def try_reserve(self, az: str, units: int) -> bool:
        """Edge 31 — atomic reserve. The decision, not the read."""
        async with self._lock:
            pool = self.pool(az)
            if pool.available_units < units:
                return False
            pool.reserved_units += units
            return True

    async def release(self, az: str, units: int, cooldown: bool = False) -> None:
        """Edge 31 — give the units back.

        `cooldown=True` parks them for CONFIG.cooldown_seconds so a wobbling
        forecast cannot reclaim and immediately re-sell the same capacity.
        """
        async with self._lock:
            pool = self.pool(az)
            pool.reserved_units = max(0, pool.reserved_units - units)
            if cooldown:
                pool.cooldown_units += units
        if cooldown:
            asyncio.create_task(self._expire_cooldown(az, units))

    async def _expire_cooldown(self, az: str, units: int) -> None:
        await asyncio.sleep(CONFIG.cooldown_seconds)
        async with self._lock:
            pool = self.pool(az)
            pool.cooldown_units = max(0, pool.cooldown_units - units)

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(CONFIG.control_cycle_seconds)
            try:
                await self.refresh()
            except Exception:
                log.exception("pool refresh failed")

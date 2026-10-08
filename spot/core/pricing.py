"""Spot discount curve.

Discount is set from surplus depth: the deeper the idle pool, the cheaper spot
gets. It is snapshotted onto the lease at admission and never re-rated.

An operator can pin the discount manually for a bounded window; when the
window lapses the curve takes over again, so a forgotten override cannot
outlive the shift that set it.
"""
from __future__ import annotations

import time
from typing import Optional

from ..config import CONFIG
from .pool_view import SpotPoolView


class Pricing:
    def __init__(self, pool: SpotPoolView):
        self._pool = pool
        #: manual override: {discount, az (None = every AZ), set_at, expires_at,
        #: duration_seconds}. Expiry is checked lazily on read — no timer to leak.
        self._override: Optional[dict] = None

    # ---------------------------------------------------------------- auto
    def auto_discount_for(self, az: str) -> float:
        p = self._pool.pool(az)
        if p.sellable_units <= 0:
            return CONFIG.min_discount
        depth = p.available_units / max(1, p.sellable_units)  # 0..1
        span = CONFIG.max_discount - CONFIG.min_discount
        return round(CONFIG.min_discount + span * depth, 4)

    # -------------------------------------------------------------- manual
    def override(self) -> Optional[dict]:
        if self._override and self._override["expires_at"] <= time.time():
            self._override = None
        return self._override

    def set_manual(self, discount: float, duration_seconds: float,
                   az: Optional[str] = None) -> dict:
        now = time.time()
        self._override = {
            "discount": round(float(discount), 4),
            "az": az,
            "set_at": now,
            "expires_at": now + float(duration_seconds),
            "duration_seconds": float(duration_seconds),
        }
        return self._override

    def set_auto(self) -> None:
        self._override = None

    # ------------------------------------------------------------ effective
    def discount_for(self, az: str) -> float:
        ov = self.override()
        if ov and (ov["az"] is None or ov["az"] == az):
            return ov["discount"]
        return self.auto_discount_for(az)

    def status(self) -> dict:
        ov = self.override()
        return {
            "mode": "manual" if ov else "auto",
            "override": ov and {**ov, "remaining_seconds":
                                round(max(0.0, ov["expires_at"] - time.time()), 1)},
            "min_discount": CONFIG.min_discount,
            "max_discount": CONFIG.max_discount,
            "per_az": [
                {"az": p["az"],
                 "auto_discount": self.auto_discount_for(p["az"]),
                 "effective_discount": self.discount_for(p["az"]),
                 "overridden": bool(ov and (ov["az"] is None or ov["az"] == p["az"]))}
                for p in self._pool.snapshot()
            ],
        }

    @staticmethod
    def rate_per_sec(on_demand_rate_per_hour: float, count: int, discount: float) -> float:
        return on_demand_rate_per_hour * count * (1.0 - discount) / 3600.0

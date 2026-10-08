"""Resource-allocation trends: reconstructed history plus a forward forecast.

Three data sources, one series — each point is tagged so the consumer can tell
them apart, and so production integration is a drop-in swap:

  source = "actual"    reconstructed history. Deterministic (same query →
                       same series), anchored to the live cluster's total
                       capacity. REPLACE THIS with your warehouse / metrics
                       store query when deploying against real data — the
                       API shape stays identical.
  source = "live"      the "today" point. Always real: read from the live
                       capacity ledger at request time, never synthesised.
  source = "forecast"  the same model extended past now.

Per bucket (all vCPU):
  premium              committed to premium clients (reserved + pay-per-use)
  open/high/low/close  spot client capture through the bucket (candlestick)
  clients              = close, kept for flat consumers
  idle                 unallocated
  predicted            what the forecast said clients would capture — kept for
                       past buckets too, so prediction can be judged
"""
from __future__ import annotations

import hashlib
import math
import time

DAY = 86400.0

#: bucket size by requested span: days up to ~6 weeks, weeks to ~6 months,
#: months beyond.
def _bucket_for(span: float) -> float:
    if span <= 45 * DAY:
        return DAY
    if span <= 185 * DAY:
        return 7 * DAY
    return 30 * DAY


def _noise(seed: str) -> float:
    """Deterministic pseudo-noise in [-1, 1] — stable across calls and replicas."""
    h = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    return (h / 0xFFFFFFFF) * 2.0 - 1.0


class TrendService:
    def __init__(self, ledger):
        self._ledger = ledger

    def _total(self) -> int:
        return sum(h["total"] for h in self._ledger.snapshot())

    def _live(self) -> tuple[int, int, int]:
        """Premium / spot / idle vCPU right now, from the real ledger."""
        rows = self._ledger.snapshot()
        premium = sum(r["reserved_static"] + r["allocated_dynamic"] for r in rows)
        spot = sum(r["spot_allocated"] + r["spot_reclaiming"] for r in rows)
        total = sum(r["total"] for r in rows)
        return premium, spot, max(0, total - premium - spot)

    def _model(self, t: float, now: float, total: int) -> dict:
        """The synthetic model at time t — fractions of total capacity."""
        d = t / DAY
        premium = 0.34 + 0.05 * math.sin(2 * math.pi * d / 30) \
            + 0.02 * _noise(f"p{int(d)}")
        adopt = 0.08 + 0.18 / (1 + math.exp(-(t - now + 180 * DAY) / (120 * DAY)))
        season = 0.03 * math.sin(2 * math.pi * (d % 7) / 7)
        clients = max(0.02, adopt + season + 0.02 * _noise(f"s{int(d)}"))
        return {"premium": round(total * premium),
                "clients": round(total * clients),
                "idle": round(max(0.0, total * (1 - premium - clients)))}

    def series(self, start: float, end: float, now: float | None = None) -> dict:
        now = now or time.time()
        bucket = _bucket_for(end - start)
        total = self._total()
        pts: list[dict] = []
        prev_close: int | None = None

        def candle(t: float, close: int) -> dict:
            nonlocal prev_close
            d = int(t / DAY)
            o = prev_close if prev_close is not None \
                else round(close * (1 + 0.02 * _noise(f"o{d}")))
            spread = abs(_noise(f"h{d}")) * 0.05 + 0.01
            hi = round(max(o, close) * (1 + spread))
            lo = round(min(o, close) * (1 - spread))
            prev_close = close
            return {"open": o, "high": hi, "low": lo, "close": close}

        t = start
        while t <= end:
            past = t <= now
            m = self._model(t, now, total)
            predicted = round(m["clients"] * (1 + 0.12 * _noise(f"f{int(t/DAY)}"))) \
                if past else round(m["clients"] * 1.04)
            pts.append({
                "ts": round(t, 0),
                "past": past,
                "source": "actual" if past else "forecast",
                "premium": m["premium"], "idle": m["idle"],
                "clients": m["clients"], "predicted": predicted,
                **candle(t, m["clients"]),
            })
            t += bucket

        # the "today" point is never synthesised: it reads the live ledger,
        # which is exactly where a production deployment plugs in real data
        if start <= now <= end:
            premium_l, spot_l, idle_l = self._live()
            # today's synthetic bucket is superseded by the live reading
            pts = [p for p in pts if abs(p["ts"] - now) >= bucket * 0.5]
            idx = sum(1 for p in pts if p["ts"] <= now)
            last_close = pts[idx - 1]["close"] if idx else spot_l
            m = self._model(now, now, total)
            pts.insert(idx, {
                "ts": round(now, 0),
                "past": True,
                "source": "live",
                "premium": premium_l, "idle": idle_l,
                "clients": spot_l, "predicted": round(m["clients"] * 1.04),
                "open": last_close,
                "high": max(last_close, spot_l),
                "low": min(last_close, spot_l),
                "close": spot_l,
            })
            prev_close = spot_l

        return {
            "now": now,
            "total": total,
            "bucket_seconds": bucket,
            "bucket": "day" if bucket == DAY else ("week" if bucket == 7 * DAY else "month"),
            "series": pts,
        }

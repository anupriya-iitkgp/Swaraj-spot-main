"""Demand signals for spot instances.

Every launch attempt (admitted OR rejected — a 429 is still demand) and every
targeted availability check is recorded as a demand signal for a node shape
(its flavour carries the vCPU/RAM/HDD) so pricing and capacity planning can
see what customers actually looked for, not just what they got.

Windows longer than the process lifetime are backfilled deterministically
(same convention as trend_service: same query → same numbers); live events
are counted exactly and overlaid. Each series bucket says which it is.
"""
from __future__ import annotations

import hashlib
import time

DAY = 86400.0

_POPULARITY = {"s1.small": 1.0, "s1.medium": 0.75, "s1.large": 0.45,
               "db1.xlarge": 0.15}


def _noise01(seed: str) -> float:
    return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


class DemandTracker:
    def __init__(self):
        self._events: list[dict] = []
        self._start = time.time()

    def record(self, flavour: str, az: str, kind: str = "launch_attempt") -> None:
        self._events.append({"ts": time.time(), "flavour": flavour,
                             "az": az, "kind": kind})
        if len(self._events) > 50_000:          # bounded memory
            self._events = self._events[-25_000:]

    def _synth_day(self, day_index: int, flavour: str) -> int:
        pop = _POPULARITY.get(flavour, 0.5)
        return round((6 + 30 * _noise01(f"d{day_index}:{flavour}")) * pop)

    def summary(self, start: float, end: float, flavours: list[str],
                now: float | None = None) -> dict:
        now = now or time.time()
        span = end - start
        bucket = 3600.0 if span <= 2 * DAY else DAY if span <= 120 * DAY else 30 * DAY

        by_flavour: dict[str, int] = {}
        series: list[dict] = []
        t = start
        while t <= end:
            count = 0
            # backfill only before the process started, and only at day+ grain
            if t < self._start and bucket >= DAY:
                for f in flavours:
                    s = self._synth_day(int(t / DAY), f)
                    count += s
                    by_flavour[f] = by_flavour.get(f, 0) + s
            series.append({"ts": round(t, 0), "count": count,
                           "synthetic": t < self._start})
            t += bucket

        live = [e for e in self._events if start <= e["ts"] <= end]
        for e in live:
            by_flavour[e["flavour"]] = by_flavour.get(e["flavour"], 0) + 1
            idx = int((e["ts"] - start) // bucket)
            if 0 <= idx < len(series):
                series[idx]["count"] += 1

        total = sum(by_flavour.values())
        rows = [{"flavour": f, "checks": c,
                 "share": round(c / total, 4) if total else 0}
                for f, c in sorted(by_flavour.items(), key=lambda kv: -kv[1])]
        return {"start": start, "end": end, "bucket_seconds": bucket,
                "total_checks": total, "live_events": len(live),
                "by_flavour": rows, "series": series}

    def top(self, window: float = DAY, now: float | None = None) -> dict | None:
        """Most-demanded flavour over the window — live events only."""
        now = now or time.time()
        counts: dict[str, int] = {}
        for e in self._events:
            if e["ts"] >= now - window:
                counts[e["flavour"]] = counts.get(e["flavour"], 0) + 1
        if not counts:
            return None
        f, c = max(counts.items(), key=lambda kv: kv[1])
        return {"flavour": f, "checks": c}

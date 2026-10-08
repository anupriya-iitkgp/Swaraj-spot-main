"""Capacity sampler and request metrics — what the dashboard reads.

The sampler snapshots the ledger, the pool read model and the lease state
histogram on a fixed interval into a ring buffer. Nothing else in the subsystem
depends on it, so it can be dropped or replaced with a Prometheus exporter
without touching a single core component.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from typing import Optional

log = logging.getLogger("spot.telemetry")


class Metrics:
    """Counters and latency samples for the API surface."""

    def __init__(self, latency_window: int = 500):
        self.counters: Counter[str] = Counter()
        self.latency_ms: deque[float] = deque(maxlen=latency_window)
        self.admission_ms: deque[float] = deque(maxlen=latency_window)
        self.recent_calls: deque[dict] = deque(maxlen=60)

    def inc(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def observe_request(self, method: str, path: str, status: int, ms: float) -> None:
        self.latency_ms.append(ms)
        self.inc(f"http.{status}")
        self.inc("http.total")
        if path.endswith("/spot/leases") and method == "POST":
            self.admission_ms.append(ms)
            self.inc(f"admission.{status}")
        self.recent_calls.appendleft(
            {"ts": time.time(), "method": method, "path": path,
             "status": status, "ms": round(ms, 1)}
        )

    @staticmethod
    def _pct(values, q: float) -> Optional[float]:
        if not values:
            return None
        s = sorted(values)
        i = min(len(s) - 1, int(q * len(s)))
        return round(s[i], 1)

    def snapshot(self) -> dict:
        return {
            "counters": dict(self.counters),
            "http_p50_ms": self._pct(self.latency_ms, 0.50),
            "http_p99_ms": self._pct(self.latency_ms, 0.99),
            "admission_p50_ms": self._pct(self.admission_ms, 0.50),
            "admission_p99_ms": self._pct(self.admission_ms, 0.99),
            "recent_calls": list(self.recent_calls),
        }


class CapacitySampler:
    """Rolling history of capacity and lease state, for the dashboard charts."""

    def __init__(self, *, ledger, pool, lease_manager, interval: float = 2.0,
                 window: int = 450):
        self._ledger = ledger
        self._pool = pool
        self._leases = lease_manager
        self.interval = interval
        self.samples: deque[dict] = deque(maxlen=window)
        self._task: asyncio.Task | None = None

    # ---------------------------------------------------------------- sample
    def cluster(self) -> dict:
        hgs = self._ledger.host_groups()
        total = sum(h.total_units for h in hgs)
        agg = {
            "total": total,
            "reserved_static": sum(h.reserved_static for h in hgs),
            "allocated_dynamic": sum(h.allocated_dynamic for h in hgs),
            "spot_allocated": sum(h.spot_allocated for h in hgs),
            "spot_reclaiming": sum(h.spot_reclaiming for h in hgs),
            "ops_buffer": sum(h.ops_buffer for h in hgs),
        }
        agg["free"] = max(0, total - sum(v for k, v in agg.items() if k != "total"))
        used = total - agg["free"]
        agg["utilisation_pct"] = round(100 * used / total, 1) if total else 0.0
        agg["spot_share_pct"] = round(
            100 * (agg["spot_allocated"] + agg["spot_reclaiming"]) / total, 1
        ) if total else 0.0
        return agg

    def state_histogram(self) -> dict:
        c = Counter(l.state.value for l in self._leases.all())
        return dict(c)

    def take(self) -> dict:
        s = {
            "ts": time.time(),
            "cluster": self.cluster(),
            "pools": self._pool.snapshot(),
            "states": self.state_histogram(),
        }
        self.samples.append(s)
        return s

    def history(self, limit: int = 180) -> list[dict]:
        return list(self.samples)[-limit:]

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.take()
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
            await asyncio.sleep(self.interval)
            try:
                self.take()
            except Exception:
                log.exception("sampler failed")

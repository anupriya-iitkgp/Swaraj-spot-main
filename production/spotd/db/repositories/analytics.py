"""Interruption statistics — edges 29 and 30.

HLD §11 requires the published interruption rate to be "refreshed at least
hourly, per flavour and AZ", because "customers cannot size spot workloads
without it".

HLD §12 asks for a second cut of the same measure. Under sustained pressure, if
reclaim keeps hitting the same host groups, "the same tenants absorb every
interruption while others never do", so the recommendation is to "track
interruption rate per tenant, not just per flavour. If the spread widens, victim
selection needs a fairness term."

Both cuts live in one table, distinguished by `tenant_id IS NULL` for the
published number. `fairness_spread()` turns the per-tenant rows into the single
figure that answers the §12 question.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Sequence

import asyncpg

from ...domain.models import InterruptionRate
from ...logging import get_logger
from ...metrics import M

log = get_logger(__name__)

__all__ = ["AnalyticsRepository", "FairnessSpread"]


class FairnessSpread:
    """How unevenly interruptions are distributed across tenants."""

    __slots__ = ("tenants", "mean", "stdev", "p95", "minimum", "maximum", "gini")

    def __init__(self, rates: Sequence[float]) -> None:
        ordered = sorted(rates)
        self.tenants = len(ordered)
        self.mean = statistics.fmean(ordered) if ordered else 0.0
        self.stdev = statistics.pstdev(ordered) if len(ordered) > 1 else 0.0
        self.p95 = ordered[int(0.95 * (len(ordered) - 1))] if ordered else 0.0
        self.minimum = ordered[0] if ordered else 0.0
        self.maximum = ordered[-1] if ordered else 0.0
        self.gini = _gini(ordered)

    @property
    def widening_signal(self) -> bool:
        """True when the spread is wide enough to warrant a fairness term.

        A Gini above 0.4 over a meaningful sample means a minority of tenants is
        absorbing most of the interruptions. That is the condition HLD §12 says
        should change victim selection, so it is computed rather than eyeballed.
        """
        return self.tenants >= 5 and self.gini > 0.4

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenants": self.tenants,
            "mean": round(self.mean, 6),
            "stdev": round(self.stdev, 6),
            "p95": round(self.p95, 6),
            "min": round(self.minimum, 6),
            "max": round(self.maximum, 6),
            "gini": round(self.gini, 4),
            "widening": self.widening_signal,
        }


def _gini(sorted_values: Sequence[float]) -> float:
    n = len(sorted_values)
    total = sum(sorted_values)
    if n == 0 or total <= 0:
        return 0.0
    cumulative = sum((i + 1) * v for i, v in enumerate(sorted_values))
    return (2 * cumulative) / (n * total) - (n + 1) / n


class AnalyticsRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def upsert(
        self, rate: InterruptionRate, *, conn: asyncpg.Connection | None = None
    ) -> None:
        await (conn or self._db).execute(
            """
            INSERT INTO interruption_stat
                (flavour, az, tenant_id, window_start, window_end,
                 preemptions, lease_hours, rate, sample_size)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (flavour, az, tenant_id, window_start) DO UPDATE SET
                window_end  = EXCLUDED.window_end,
                preemptions = EXCLUDED.preemptions,
                lease_hours = EXCLUDED.lease_hours,
                rate        = EXCLUDED.rate,
                sample_size = EXCLUDED.sample_size,
                computed_at = now()
            """,
            rate.flavour,
            rate.az,
            rate.tenant_id,
            rate.window_start,
            rate.window_end,
            rate.preemptions,
            rate.lease_hours,
            rate.rate,
            rate.sample_size,
        )
        M.interruption_rate.labels(
            flavour=rate.flavour, az=rate.az, tenant=rate.tenant_id or "_all"
        ).set(rate.rate)

    async def published(
        self,
        *,
        flavour: str | None = None,
        az: str | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """The customer-facing number: latest window per (flavour, az).

        DISTINCT ON is the cheapest way to say "most recent row per group" in
        Postgres, and this is read on every inventory call.
        """
        clauses = ["tenant_id IS NULL"]
        args: list[Any] = []
        if flavour:
            args.append(flavour)
            clauses.append(f"flavour = ${len(args)}")
        if az:
            args.append(az)
            clauses.append(f"az = ${len(args)}")

        rows = await (conn or self._db).fetch(
            f"""
            SELECT DISTINCT ON (flavour, az)
                   flavour, az, window_start, window_end, preemptions,
                   lease_hours, rate, sample_size, computed_at
              FROM interruption_stat
             WHERE {' AND '.join(clauses)}
             ORDER BY flavour, az, window_start DESC
            """,
            *args,
        )
        return [dict(r) for r in rows]

    async def per_tenant(
        self, *, since: datetime, conn: asyncpg.Connection | None = None
    ) -> list[dict[str, Any]]:
        rows = await (conn or self._db).fetch(
            """
            SELECT tenant_id, flavour, az, rate, preemptions, lease_hours
              FROM interruption_stat
             WHERE tenant_id IS NOT NULL AND window_start >= $1
             ORDER BY rate DESC
            """,
            since,
        )
        return [dict(r) for r in rows]

    async def fairness_spread(
        self, *, since: datetime, conn: asyncpg.Connection | None = None
    ) -> FairnessSpread:
        """The HLD §12 fairness signal, aggregated across flavours per tenant."""
        rows = await (conn or self._db).fetch(
            """
            SELECT tenant_id,
                   CASE WHEN SUM(lease_hours) > 0
                        THEN SUM(preemptions) / SUM(lease_hours)
                        ELSE 0 END AS rate
              FROM interruption_stat
             WHERE tenant_id IS NOT NULL AND window_start >= $1
             GROUP BY tenant_id
            """,
            since,
        )
        spread = FairnessSpread([float(r["rate"]) for r in rows])
        if spread.widening_signal:
            log.warning(
                "analytics.fairness_spread_widening",
                gini=round(spread.gini, 4),
                tenants=spread.tenants,
                max_rate=round(spread.maximum, 6),
                mean_rate=round(spread.mean, 6),
                note="HLD §12: if the spread widens, victim selection needs a "
                "fairness term",
            )
        return spread

    async def staleness_seconds(
        self, *, conn: asyncpg.Connection | None = None
    ) -> float | None:
        """Age of the newest published row — the HLD §11 hourly-refresh check."""
        value = await (conn or self._db).fetchval(
            """
            SELECT EXTRACT(EPOCH FROM now() - MAX(computed_at))::float8
              FROM interruption_stat WHERE tenant_id IS NULL
            """
        )
        return float(value) if value is not None else None

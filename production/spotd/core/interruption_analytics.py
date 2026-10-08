"""Interruption Analytics — edges 29 and 30.

HLD §11 makes this a non-functional requirement rather than a nice-to-have:

    Interruption rate publication | Refreshed at least hourly, per flavour and AZ
    | Customers cannot size spot workloads without it.

A spot product without a published interruption rate asks customers to guess how
often they will be interrupted, and the rational response to that uncertainty is
not to adopt it. So the rate is computed from the audit-grade lease history —
not sampled, not estimated — and published on the inventory endpoint next to the
price, so the trade-off is visible in one place.

Two derived numbers come out of the same pass:

  * **Reclaim SLO attainment** — the fraction of reclaims completing inside the
    grace window, against HLD §11's 99.9% target. Measured notice-to-closed,
    "teardown included", exactly as the target is worded.
  * **Fairness spread** — the per-tenant cut HLD §12 asks for: "Track
    interruption rate per tenant, not just per flavour. If the spread widens,
    victim selection needs a fairness term."

The rate is preemptions per lease-hour. Per lease-hour rather than per lease
because a lease that ran for a week and one that ran for a minute are not
comparable denominators, and customers sizing a workload care about the hourly
hazard.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..config import Settings
from ..domain.models import InterruptionRate, utcnow
from ..logging import edge, get_logger
from ..metrics import M

log = get_logger(__name__)

__all__ = ["InterruptionAnalytics", "SLOReport"]

#: Below this many lease-hours a rate is noise. Publishing 100% because the one
#: lease in the sample happened to be preempted would be actively misleading.
_MIN_LEASE_HOURS = 1.0


class SLOReport:
    __slots__ = ("az", "total", "within_window", "attainment", "breaches", "target")

    def __init__(self, az: str, total: int, within: int, target: float) -> None:
        self.az = az
        self.total = total
        self.within_window = within
        self.breaches = total - within
        self.attainment = (within / total) if total else 1.0
        self.target = target

    @property
    def met(self) -> bool:
        return self.attainment >= self.target

    def as_dict(self) -> dict[str, Any]:
        return {
            "az": self.az,
            "reclaims": self.total,
            "within_grace_window": self.within_window,
            "breaches": self.breaches,
            "attainment": round(self.attainment, 6),
            "target": self.target,
            "met": self.met,
        }


class InterruptionAnalytics:
    def __init__(
        self,
        *,
        settings: Settings,
        lease_repo: Any,
        analytics_repo: Any,
        db: Any,
    ) -> None:
        self._settings = settings
        self._leases = lease_repo
        self._analytics = analytics_repo
        self._db = db

    # ------------------------------------------------------------------
    async def roll_up(self, *, window: timedelta = timedelta(hours=24)) -> int:
        """Recompute published and per-tenant rates. Returns rows written."""
        window_end = utcnow()
        window_start = window_end - window

        preemptions = await self._leases.preemptions_since(window_start)
        exposure = await self._leases.lease_seconds_since(window_start)

        # preemptions per (flavour, az) and per (flavour, az, tenant)
        published: dict[tuple[str, str], int] = {}
        per_tenant: dict[tuple[str, str, str], int] = {}
        for row in preemptions:
            published[(row["flavour"], row["az"])] = (
                published.get((row["flavour"], row["az"]), 0) + 1
            )
            key = (row["flavour"], row["az"], row["tenant_id"])
            per_tenant[key] = per_tenant.get(key, 0) + 1

        hours_published: dict[tuple[str, str], float] = {}
        hours_tenant: dict[tuple[str, str, str], float] = {}
        samples: dict[tuple[str, str], int] = {}
        for row in exposure:
            hours = float(row["lease_seconds"]) / 3600.0
            pk = (row["flavour"], row["az"])
            hours_published[pk] = hours_published.get(pk, 0.0) + hours
            samples[pk] = samples.get(pk, 0) + int(row["leases"])
            tk = (row["flavour"], row["az"], row["tenant_id"])
            hours_tenant[tk] = hours_tenant.get(tk, 0.0) + hours

        written = 0

        for (flavour, az), hours in hours_published.items():
            if hours < _MIN_LEASE_HOURS:
                continue
            count = published.get((flavour, az), 0)
            await self._analytics.upsert(
                InterruptionRate(
                    flavour=flavour,
                    az=az,
                    tenant_id=None,
                    window_start=window_start,
                    window_end=window_end,
                    preemptions=count,
                    lease_hours=round(hours, 4),
                    rate=round(count / hours, 6),
                    sample_size=samples.get((flavour, az), 0),
                )
            )
            written += 1

        for (flavour, az, tenant_id), hours in hours_tenant.items():
            if hours < _MIN_LEASE_HOURS:
                continue
            count = per_tenant.get((flavour, az, tenant_id), 0)
            await self._analytics.upsert(
                InterruptionRate(
                    flavour=flavour,
                    az=az,
                    tenant_id=tenant_id,
                    window_start=window_start,
                    window_end=window_end,
                    preemptions=count,
                    lease_hours=round(hours, 4),
                    rate=round(count / hours, 6),
                    sample_size=1,
                )
            )
            written += 1

        # HLD §12's fairness question, answered with a number.
        spread = await self._analytics.fairness_spread(since=window_start)

        edge(
            log,
            29,
            f"rolled up {written} interruption-rate row(s) over "
            f"{window.total_seconds() / 3600:.0f}h; tenant spread gini="
            f"{spread.gini:.3f}",
            rows=written,
            window_hours=window.total_seconds() / 3600,
            fairness=spread.as_dict(),
        )
        return written

    # ------------------------------------------------------------------
    async def published_rates(
        self, *, flavour: str | None = None, az: str | None = None
    ) -> list[dict[str, Any]]:
        """Edge 30 — what the Spot Market API shows customers."""
        return await self._analytics.published(flavour=flavour, az=az)

    async def slo(self, *, window: timedelta = timedelta(hours=24)) -> list[SLOReport]:
        """Reclaim SLO attainment per AZ, measured notice-to-closed."""
        since = utcnow() - window
        rows = await self._db.fetch(
            """
            SELECT az,
                   COUNT(*)::int AS total,
                   COUNT(*) FILTER (
                       WHERE EXTRACT(EPOCH FROM closed_at - notice_at) <= grace_seconds
                   )::int AS within
              FROM spot_lease
             WHERE preemption_reason = 'capacity_reclaim'
               AND notice_at >= $1
               AND closed_at IS NOT NULL
             GROUP BY az
            """,
            since,
        )
        reports = [
            SLOReport(r["az"], r["total"], r["within"], 0.999) for r in rows
        ]
        for report in reports:
            M.reclaim_slo_attainment.labels(az=report.az).set(report.attainment)
            if not report.met and report.total >= 10:
                log.error(
                    "slo.reclaim_attainment_below_target",
                    az=report.az,
                    attainment=round(report.attainment, 6),
                    target=report.target,
                    breaches=report.breaches,
                    note="HLD §11: 99.9% of reclaims within 120s, teardown included",
                )
        return reports

    async def staleness_seconds(self) -> float | None:
        """Age of the published rate. HLD §11 requires at least hourly refresh."""
        return await self._analytics.staleness_seconds()

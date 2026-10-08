"""Prometheus metrics — the twelve series named in LLD §14.1, with those labels.

The table in §14.1 also gives an alert condition for each one. Those conditions
are encoded in `deploy/prometheus/spot_rules.yml`; the metric names and labels
here are the contract between this file and that one, so they are written out
verbatim rather than generated.

Two of them deserve a note:

  spot_over_allocation_total   Alert is "> 0 ever". This counter should never
                               move. If it does, the atomic reserve has been
                               bypassed and the design's central guarantee is
                               broken — it is a correctness bug, not a tuning
                               issue. It is incremented only by an assertion
                               that recomputes the pool from the lease table.

  spot_lease_state             Alert is "NOTICE_ISSUED count not draining".
                               That is the observable symptom of the stranded
                               grace timers in LLD §12.3. The reaper fixes the
                               cause; this gauge proves the fix is working.
"""

from __future__ import annotations

from typing import Iterable

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST

__all__ = ["REGISTRY", "CONTENT_TYPE_LATEST", "render", "M"]

REGISTRY = CollectorRegistry(auto_describe=True)

#: Admission is on the request path and the target is a p99, so the buckets are
#: dense either side of the 200 ms objective in HLD §11 and sparse beyond it.
_ADMISSION_BUCKETS = (
    0.005, 0.010, 0.025, 0.050, 0.075, 0.100, 0.150, 0.200, 0.300, 0.500,
    1.000, 2.500, 5.000,
)
#: Reclaim is measured against the 120 s grace window, alerted at p999.
_RECLAIM_BUCKETS = (
    1, 5, 10, 20, 30, 45, 60, 75, 90, 95, 105, 120, 150, 180, 300,
)


class _Metrics:
    """Namespace object so callers write `M.admission_total.labels(...)`."""

    def __init__(self, registry: CollectorRegistry) -> None:
        r = registry

        # -- §14.1, row 1 --------------------------------------------------
        self.admission_latency = Histogram(
            "spot_admission_latency_seconds",
            "End-to-end latency of a spot launch admission decision.",
            ["outcome"],
            buckets=_ADMISSION_BUCKETS,
            registry=r,
        )
        # -- row 2 ---------------------------------------------------------
        self.admission_total = Counter(
            "spot_admission_total",
            "Admission decisions by outcome (admitted/403/409/429/400/503).",
            ["outcome"],
            registry=r,
        )
        # -- row 3 ---------------------------------------------------------
        self.pool_available_units = Gauge(
            "spot_pool_available_units",
            "Sellable minus reserved minus cooldown, per AZ, as last refreshed.",
            ["az"],
            registry=r,
        )
        # -- row 4 ---------------------------------------------------------
        self.pool_degraded = Gauge(
            "spot_pool_degraded",
            "1 when the forecast feed is stale or low-confidence and the pool "
            "has been degraded to the conservative floor.",
            ["az"],
            registry=r,
        )
        # -- row 5 — must never move --------------------------------------
        self.over_allocation_total = Counter(
            "spot_over_allocation_total",
            "Detected over-allocations. Any non-zero value is a correctness bug.",
            ["az"],
            registry=r,
        )
        # -- row 6 ---------------------------------------------------------
        self.reclaim_duration = Histogram(
            "spot_reclaim_duration_seconds",
            "Notice to capacity-returned, per lease. forced=true when the guest "
            "did not exit on its own and the timer stopped it.",
            ["forced"],
            buckets=_RECLAIM_BUCKETS,
            registry=r,
        )
        # -- row 7 ---------------------------------------------------------
        self.reclaim_slo_attainment = Gauge(
            "spot_reclaim_slo_attainment",
            "Fraction of reclaims completing within the grace window, per AZ. "
            "HLD §11 target: 0.999.",
            ["az"],
            registry=r,
        )
        # -- row 8 ---------------------------------------------------------
        self.forced_stop_total = Counter(
            "spot_forced_stop_total",
            "Guests force-stopped at the deadline. A sudden rise means customers "
            "are not honouring the notice.",
            ["flavour"],
            registry=r,
        )
        # -- row 9 ---------------------------------------------------------
        self.notice_delivery_total = Counter(
            "spot_notice_delivery_total",
            "Notice delivery attempts by channel and result.",
            ["channel", "delivered"],
            registry=r,
        )
        self.notice_all_channels_failed_total = Counter(
            "spot_notice_all_channels_failed_total",
            "Preemptions where no channel delivered. HLD §11 calls notice "
            "delivery the trust anchor of the product; each one is an SLO "
            "breach and an automatic credit.",
            ["az"],
            registry=r,
        )
        # -- row 10 --------------------------------------------------------
        self.teardown_stalled = Gauge(
            "spot_teardown_stalled",
            "Leases whose teardown exceeded the budget; their units are still "
            "RECLAIMING and must never be reported free.",
            ["host_group"],
            registry=r,
        )
        # -- row 11 --------------------------------------------------------
        self.interruption_rate = Gauge(
            "spot_interruption_rate",
            "Published interruption rate. The tenant label is the fairness "
            "signal from HLD §12: a widening spread means victim selection "
            "needs a fairness term.",
            ["flavour", "az", "tenant"],
            registry=r,
        )
        # -- row 12 --------------------------------------------------------
        self.lease_state = Gauge(
            "spot_lease_state",
            "Leases per state. NOTICE_ISSUED not draining indicates stranded "
            "grace timers (LLD §12.3).",
            ["state"],
            registry=r,
        )

        # -- supporting series, not in §14.1 but needed to operate ---------
        self.reserve_conflict_total = Counter(
            "spot_reserve_conflict_total",
            "Atomic reserves that lost the race. Normal traffic on a busy pool "
            "(HLD §7), not an incident.",
            ["az"],
            registry=r,
        )
        self.outbox_pending = Gauge(
            "spot_outbox_pending",
            "Unpublished outbox rows. Sustained growth means the relay is stuck "
            "and the ledger/event views are diverging (LLD §12.8).",
            registry=r,
        )
        self.outbox_dead = Gauge(
            "spot_outbox_dead",
            "Outbox rows past max attempts, parked for manual intervention.",
            registry=r,
        )
        self.outbox_published_total = Counter(
            "spot_outbox_published_total",
            "Events published from the outbox.",
            ["topic"],
            registry=r,
        )
        self.reaper_claimed_total = Counter(
            "spot_reaper_claimed_total",
            "Leases claimed by the grace reaper. Non-zero after a restart is "
            "the reaper doing its job (LLD §12.3).",
            ["reason"],
            registry=r,
        )
        self.external_call_duration = Histogram(
            "spot_external_call_duration_seconds",
            "Calls to out-of-scope services (the dashed boxes).",
            ["service", "operation", "outcome"],
            buckets=(0.005, 0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
            registry=r,
        )
        self.breaker_state = Gauge(
            "spot_circuit_breaker_state",
            "0=closed, 1=half-open, 2=open, per external service.",
            ["service"],
            registry=r,
        )
        self.rate_limited_total = Counter(
            "spot_rate_limited_total",
            "Requests rejected by the tenant token bucket, enforcing Retry-After "
            "at the edge (LLD §12.9).",
            ["tenant"],
            registry=r,
        )
        self.idempotent_replay_total = Counter(
            "spot_idempotent_replay_total",
            "Launches served from an existing idempotency key.",
            registry=r,
        )
        self.credit_total = Counter(
            "spot_credit_total",
            "Credits raised, by reason.",
            ["reason"],
            registry=r,
        )
        self.leader = Gauge(
            "spot_leader",
            "1 when this replica holds the named singleton lock.",
            ["lock"],
            registry=r,
        )
        self.worker_iterations_total = Counter(
            "spot_worker_iterations_total",
            "Background worker loop iterations, by worker and outcome.",
            ["worker", "outcome"],
            registry=r,
        )

    def reset_pool_gauges(self, azs: Iterable[str]) -> None:
        """Initialise per-AZ gauges so they exist before the first refresh.

        A gauge that only appears once it is non-zero cannot be alerted on with
        `== 0`, which is exactly the §14.1 condition for `spot_pool_available_units`.
        """
        for az in azs:
            self.pool_available_units.labels(az=az).set(0)
            self.pool_degraded.labels(az=az).set(0)
            self.reclaim_slo_attainment.labels(az=az).set(1.0)


M = _Metrics(REGISTRY)


def render() -> bytes:
    """Serialise the registry for the /metrics endpoint."""
    return generate_latest(REGISTRY)

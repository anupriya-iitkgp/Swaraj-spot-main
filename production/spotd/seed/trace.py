"""Synthetic forecast trace — a deterministic model of sellable spot over time.

One pure function, `sellable_fraction(az, at)`, is the whole model. Both the
simulator forecast feed and the seed command call it, so the 14-day trace you
can inspect in `forecast_feed` is exactly the series the running service sees —
there is no second, differently-behaved copy of the data.

Being a pure function of (az, timestamp) rather than a stored series means the
trace is reproducible, seekable, and unbounded: a soak test can run for a
simulated month without generating a month of rows first.

The shape is built from four components, chosen because each one exercises a
different branch of the code that consumes the feed:

  * **Diurnal cycle** — guaranteed-class demand peaks in office hours, so
    sellable spot surplus is deepest overnight. This is what makes the discount
    curve move, since pricing keys off surplus depth.
  * **Weekly cycle** — weekends have more surplus. Gives the interruption-rate
    rollup something with real structure to average over.
  * **Bounded noise** — small, seeded, so the pool moves every control cycle
    and the staleness logic is genuinely exercised.
  * **Two deliberate incident windows** — one low-confidence, one stale. These
    exist to drive the degradation path in HLD §12: "Require a confidence
    signal alongside the number, and degrade to a conservative floor when the
    feed is stale or the confidence is low. Never extrapolate the last known
    value." Without them that branch is only ever reached in unit tests.

Timestamps are interpreted in Asia/Kolkata, since the modelled region is
in-mum-1 and "office hours" is a local-time notion.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

__all__ = [
    "TracePoint",
    "sellable_fraction",
    "trace_point",
    "EPOCH",
    "INCIDENT_WINDOWS",
]

#: Anchor for the 14-day trace. Fixed so the same day of the trace always looks
#: the same, no matter when the service is started.
EPOCH = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)

_IST = timezone(timedelta(hours=5, minutes=30))


class IncidentWindow(NamedTuple):
    """A deliberate fault in the feed, expressed as hours from EPOCH."""

    start_hours: float
    end_hours: float
    kind: str  # "low_confidence" | "stale"
    detail: str


#: Two faults, placed on different days so a single test run can hit one and a
#: 14-day soak hits both.
INCIDENT_WINDOWS: tuple[IncidentWindow, ...] = (
    IncidentWindow(
        start_hours=76.0,
        end_hours=81.0,
        kind="low_confidence",
        detail="forecast model retrained on partial data; confidence collapses "
        "to 0.3 while the number itself stays plausible",
    ),
    IncidentWindow(
        start_hours=196.0,
        end_hours=202.5,
        kind="stale",
        detail="capacity-side publisher wedged; the last good number keeps "
        "being returned with an ageing published_at",
    ),
)


class TracePoint(NamedTuple):
    az: str
    at: datetime
    fraction: float
    confidence: float
    #: Timestamp the feed *claims*. Diverges from `at` during a stale window.
    published_at: datetime
    horizon_seconds: float
    incident: str | None


def _az_phase(az: str) -> float:
    """A stable per-AZ offset, so the three AZs do not move in lockstep.

    Real availability zones serve different customers and peak at different
    times; identical curves would make a fairness or contiguity bug invisible
    because every AZ would be equally attractive at every moment.
    """
    digest = hashlib.sha256(az.encode()).digest()
    return (digest[0] / 255.0) * 2.0 * math.pi


def _noise(az: str, hour_index: int) -> float:
    """Deterministic bounded noise in [-1, 1] for one AZ-hour."""
    digest = hashlib.sha256(f"{az}:{hour_index}".encode()).digest()
    return (int.from_bytes(digest[:4], "big") / 0xFFFFFFFF) * 2.0 - 1.0


def _incident_for(hours: float) -> IncidentWindow | None:
    for window in INCIDENT_WINDOWS:
        if window.start_hours <= hours < window.end_hours:
            return window
    return None


def sellable_fraction(az: str, at: datetime) -> float:
    """Fraction of an AZ's physical capacity that is sellable as spot.

    Bounded to [0.05, 0.55]. The floor is not zero on purpose: a pool that
    empties completely makes every launch a 409 and stops exercising the
    admission path at all. The ceiling reflects that most of a real cluster is
    committed to the guaranteed classes.
    """
    local = at.astimezone(_IST)
    hour = local.hour + local.minute / 60.0
    phase = _az_phase(az)

    # Deepest surplus around 03:00 local, shallowest around 15:00.
    diurnal = math.cos(((hour - 3.0) / 24.0) * 2.0 * math.pi + phase * 0.15)
    # Saturday=5, Sunday=6 carry more surplus.
    weekly = 1.0 if local.weekday() >= 5 else 0.0

    hours_since_epoch = (at - EPOCH).total_seconds() / 3600.0
    noise = _noise(az, int(hours_since_epoch))

    fraction = 0.30 + 0.14 * diurnal + 0.08 * weekly + 0.03 * noise
    return max(0.05, min(0.55, fraction))


def trace_point(az: str, at: datetime, *, horizon_seconds: float = 900.0) -> TracePoint:
    """The full feed publication for one AZ at one instant, faults included."""
    hours = (at - EPOCH).total_seconds() / 3600.0
    incident = _incident_for(hours)
    fraction = sellable_fraction(az, at)

    confidence = 0.92 + 0.06 * _noise(az, int(hours) + 7919)
    published_at = at
    incident_kind: str | None = None

    if incident is not None:
        incident_kind = incident.kind
        if incident.kind == "low_confidence":
            confidence = 0.30
        elif incident.kind == "stale":
            # The publisher is wedged: it keeps returning the value from the
            # moment it stopped, with a published_at that ages. The number looks
            # fine — only the timestamp gives it away, which is exactly why the
            # design requires staleness to be checked and not just confidence.
            frozen_at = EPOCH + timedelta(hours=incident.start_hours)
            fraction = sellable_fraction(az, frozen_at)
            published_at = frozen_at

    return TracePoint(
        az=az,
        at=at,
        fraction=fraction,
        confidence=max(0.0, min(1.0, confidence)),
        published_at=published_at,
        horizon_seconds=horizon_seconds,
        incident=incident_kind,
    )

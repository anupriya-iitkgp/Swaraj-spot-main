"""Spot Pool View persistence, and the atomic reserve.

This file contains the single most important statement in the service.

HLD §7 explains why it has to be a reserve rather than a read:

    "The Spot Pool View is refreshed once per control cycle, so at any moment it
    can claim capacity that another launch has already taken. Making the read
    stronger does not fix this — it only moves the contention. The design
    therefore treats the read as a hint and the reserve as the decision:
    tryReserve is atomic, idempotency-keyed, and cheap to fail."

LLD §12.2 records why the reference implementation could not ship: the reserve
was an in-process `asyncio.Lock`, so "two API replicas will both admit against
the same units — over-allocation". The fix it prescribes is the conditional
UPDATE below. Postgres takes a row lock on `spot_pool` for the duration of the
statement, so concurrent reserves across any number of replicas serialise on
that one row and the predicate is evaluated against committed state. A losing
reserve updates zero rows and returns immediately — it does not block, it does
not queue, and it costs one round trip.

HLD §11 makes over-allocation a zero-tolerance target: "Guaranteed by atomic
reserve. Any occurrence is a correctness bug, not a tuning issue." `reconcile()`
at the bottom of this file is the assertion that proves it in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

import asyncpg

from ...domain.models import PoolSnapshot, SellableFeed, utcnow
from ...domain.state_machine import HOLDS_RESERVATION
from ...logging import edge, get_logger
from ...metrics import M

log = get_logger(__name__)

__all__ = ["PoolRepository", "ReserveOutcome", "ReconcileResult"]

_POOL_COLUMNS = """
    az, sellable_units, reserved_units, cooldown_units, confidence,
    published_at, horizon_seconds, degraded, cycle_seq, updated_at
"""


class ReserveOutcome:
    """Result of a reserve attempt. Falsy when the reserve lost the race."""

    __slots__ = ("granted", "units", "az", "available_after", "sellable", "reserved")

    def __init__(
        self,
        granted: bool,
        *,
        az: str,
        units: int,
        available_after: int = 0,
        sellable: int = 0,
        reserved: int = 0,
    ) -> None:
        self.granted = granted
        self.az = az
        self.units = units
        self.available_after = available_after
        self.sellable = sellable
        self.reserved = reserved

    def __bool__(self) -> bool:
        return self.granted

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        verb = "granted" if self.granted else "refused"
        return f"<Reserve {verb} {self.units}u in {self.az}, {self.available_after} left>"


class ReconcileResult:
    """Comparison of the pool counter against the leases that should back it."""

    __slots__ = ("az", "counter", "actual", "drift")

    def __init__(self, az: str, counter: int, actual: int) -> None:
        self.az = az
        self.counter = counter
        self.actual = actual
        self.drift = counter - actual

    @property
    def over_allocated(self) -> bool:
        """True when leases hold more units than the pool counter admits to.

        This is the direction that matters. A counter *higher* than reality is
        conservative — it under-sells. A counter *lower* than reality means
        units were handed out that the pool never accounted for, which is the
        correctness bug HLD §11 says must never occur.
        """
        return self.drift < 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Reconcile {self.az} counter={self.counter} actual={self.actual}>"


def _snapshot(row: asyncpg.Record) -> PoolSnapshot:
    return PoolSnapshot(
        az=row["az"],
        sellable_units=row["sellable_units"],
        reserved_units=row["reserved_units"],
        cooldown_units=row["cooldown_units"],
        confidence=row["confidence"],
        published_at=row["published_at"],
        degraded=row["degraded"],
        cycle_seq=row["cycle_seq"],
        updated_at=row["updated_at"],
    )


class PoolRepository:
    """All reads and writes of `spot_pool`.

    Every method takes an optional connection so a caller can compose it into a
    larger transaction — the reserve and the lease insert are one unit of work,
    and so are the shrink and the reclaim-order row.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # THE RESERVE — LLD §6.1
    # ------------------------------------------------------------------
    RESERVE_SQL = """
        UPDATE spot_pool
           SET reserved_units = reserved_units + $2,
               updated_at     = now()
         WHERE az = $1
           AND sellable_units - reserved_units - cooldown_units >= $2
        RETURNING sellable_units, reserved_units, cooldown_units,
                  sellable_units - reserved_units - cooldown_units AS available
    """

    async def try_reserve(
        self, az: str, units: int, *, conn: asyncpg.Connection | None = None
    ) -> ReserveOutcome:
        """Atomically take `units` from the AZ pool, or refuse.

        Returns a falsy outcome when the predicate did not hold. The caller
        turns that into a 409 with Retry-After; HLD §7 is explicit that this is
        normal traffic on a busy pool and not an incident, so it is logged at
        INFO and counted, not warned about.
        """
        if units <= 0:
            raise ValueError(f"reserve units must be positive, got {units}")

        executor = conn or self._db
        row = await executor.fetchrow(self.RESERVE_SQL, az, units)

        if row is None:
            M.reserve_conflict_total.labels(az=az).inc()
            current = await self.get(az, conn=conn)
            available = current.available_units if current else 0
            edge(
                log,
                7,
                f"admission refused: {az} has {available}u, needed {units}u",
                az=az,
                requested=units,
                available=available,
            )
            return ReserveOutcome(False, az=az, units=units, available_after=available)

        edge(
            log,
            31,
            f"pool {az}: reserved +{units} ({row['available']} left)",
            az=az,
            units=units,
            available=row["available"],
        )
        return ReserveOutcome(
            True,
            az=az,
            units=units,
            available_after=row["available"],
            sellable=row["sellable_units"],
            reserved=row["reserved_units"],
        )

    async def release(
        self,
        az: str,
        units: int,
        *,
        cooldown: bool = False,
        lease_id: str | None = None,
        cooldown_seconds: float = 0.0,
        reason: str = "release",
        conn: asyncpg.Connection | None = None,
    ) -> None:
        """Give units back.

        `cooldown=True` parks them first. HLD §12 calls the anti-thrash cooldown
        a revenue cost — "reclaimed capacity held in cooldown before re-sale is
        idle capacity you are not billing for" — and asks for it to be a tunable
        policy value whose trade-off is measured. Parking is therefore recorded
        as rows with an expiry rather than folded into a counter, so the idle
        core-seconds it costs can actually be totalled.
        """
        if units <= 0:
            return
        executor = conn or self._db

        if cooldown and cooldown_seconds > 0:
            await executor.execute(
                """
                WITH moved AS (
                    UPDATE spot_pool
                       SET reserved_units = GREATEST(0, reserved_units - $2),
                           cooldown_units = cooldown_units + $2,
                           updated_at     = now()
                     WHERE az = $1
                    RETURNING az
                )
                INSERT INTO spot_pool_cooldown (az, units, lease_id, reason, releases_at)
                SELECT az, $2::int, $3::text, $4::text,
                       now() + make_interval(secs => $5::double precision)
                  FROM moved
                """,
                az,
                units,
                lease_id,
                reason,
                float(cooldown_seconds),
            )
            edge(
                log,
                31,
                f"pool {az}: released {units}u into cooldown for {cooldown_seconds:.0f}s",
                az=az,
                units=units,
                lease_id=lease_id,
            )
            return

        await executor.execute(
            """
            UPDATE spot_pool
               SET reserved_units = GREATEST(0, reserved_units - $2),
                   updated_at     = now()
             WHERE az = $1
            """,
            az,
            units,
        )
        edge(log, 31, f"pool {az}: released {units}u", az=az, units=units, lease_id=lease_id)

    # ------------------------------------------------------------------
    # edge 20 — shrink, which must strictly precede victim selection
    # ------------------------------------------------------------------
    async def shrink(
        self, az: str, units: int, *, conn: asyncpg.Connection | None = None
    ) -> int:
        """Reduce advertised inventory immediately on a reclaim order.

        HLD §6 gives the Reclaim Order Handler "accepting reclaim orders and
        shrinking advertised inventory immediately". Doing this *before*
        selecting victims (edge 20 strictly before edge 21) is what stops the
        pool selling new leases into capacity that is already being taken back.

        Returns how much was actually removed, which is less than requested when
        the pool was already smaller than the order. The caller needs the real
        number: the difference has to come from somewhere, and that somewhere is
        running leases.
        """
        executor = conn or self._db
        # RETURNING sees only post-update values, so the pre-update number is
        # captured in a CTE that also takes the row lock. Without FOR UPDATE two
        # concurrent orders could both read the same `old` and report having
        # removed more units than actually left the pool.
        removed = await executor.fetchval(
            """
            WITH before AS (
                SELECT az, sellable_units AS old
                  FROM spot_pool
                 WHERE az = $1
                 FOR UPDATE
            )
            UPDATE spot_pool p
               SET sellable_units = GREATEST(0, p.sellable_units - $2::int),
                   updated_at     = now()
              FROM before b
             WHERE p.az = b.az
            RETURNING b.old - p.sellable_units AS removed
            """,
            az,
            units,
        )
        actual = int(removed or 0)
        edge(
            log,
            20,
            f"pool {az}: sellable -{actual}",
            az=az,
            requested=units,
            removed=actual,
        )
        return actual

    # ------------------------------------------------------------------
    # edge 19 — the forecast feed
    # ------------------------------------------------------------------
    async def refresh(
        self,
        feed: SellableFeed,
        *,
        accepted: bool,
        applied_units: int,
        degraded: bool,
        reject_reason: str | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> PoolSnapshot:
        """Apply one control cycle's sellable number.

        The decision about whether to trust the feed is made in
        `core.pool_view`, not here — this method records what was decided and
        why. Every publication is kept in `forecast_feed`, accepted or not,
        because HLD §12 warns the number is an input rather than a fact: when a
        shortage is investigated, the first question is what the feed said and
        how confident it was.
        """
        executor = conn or self._db
        await executor.execute(
            """
            INSERT INTO forecast_feed
                (az, units, confidence, published_at, horizon_seconds,
                 accepted, applied_units, reject_reason)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (az, published_at) DO NOTHING
            """,
            feed.az,
            feed.units,
            feed.confidence,
            feed.published_at,
            feed.horizon_seconds,
            accepted,
            applied_units,
            reject_reason,
        )
        row = await executor.fetchrow(
            f"""
            INSERT INTO spot_pool
                (az, sellable_units, confidence, published_at, horizon_seconds,
                 degraded, cycle_seq, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, 1, now())
            ON CONFLICT (az) DO UPDATE
               SET sellable_units  = EXCLUDED.sellable_units,
                   confidence      = EXCLUDED.confidence,
                   published_at    = EXCLUDED.published_at,
                   horizon_seconds = EXCLUDED.horizon_seconds,
                   degraded        = EXCLUDED.degraded,
                   cycle_seq       = spot_pool.cycle_seq + 1,
                   updated_at      = now()
            RETURNING {_POOL_COLUMNS}
            """,
            feed.az,
            applied_units,
            feed.confidence,
            feed.published_at,
            feed.horizon_seconds,
            degraded,
        )
        assert row is not None
        snapshot = _snapshot(row)
        M.pool_available_units.labels(az=snapshot.az).set(snapshot.available_units)
        M.pool_degraded.labels(az=snapshot.az).set(1 if degraded else 0)
        edge(
            log,
            19,
            f"pool {feed.az}: sellable={applied_units} "
            f"(feed={feed.units} conf={feed.confidence:.2f}"
            f"{' DEGRADED' if degraded else ''})",
            az=feed.az,
            sellable=applied_units,
            feed_units=feed.units,
            confidence=feed.confidence,
            degraded=degraded,
            reject_reason=reject_reason,
        )
        return snapshot

    async def ensure(
        self, azs: Iterable[str], *, conn: asyncpg.Connection | None = None
    ) -> None:
        """Create pool rows for configured AZs, degraded and empty.

        LLD §11.1 requires `pool.refresh()` before `pool.start()` so inventory is
        never served from an empty pool. A row that does not exist yet is a
        harder failure than one that exists and honestly reports zero: the
        former makes `getSellable` raise, the latter makes it return "nothing
        for sale right now", which is a true statement.
        """
        executor = conn or self._db
        for az in azs:
            await executor.execute(
                """
                INSERT INTO spot_pool (az, sellable_units, degraded)
                VALUES ($1, 0, true)
                ON CONFLICT (az) DO NOTHING
                """,
                az,
            )

    # ------------------------------------------------------------------
    # cooldown expiry
    # ------------------------------------------------------------------
    async def expire_cooldowns(
        self, *, conn: asyncpg.Connection | None = None
    ) -> dict[str, int]:
        """Return units whose anti-thrash hold has elapsed. Safe to run on N replicas."""
        executor = conn or self._db
        rows = await executor.fetch(
            """
            WITH due AS (
                UPDATE spot_pool_cooldown
                   SET released_at = now()
                 WHERE released_at IS NULL
                   AND releases_at <= now()
                RETURNING az, units
            ), agg AS (
                SELECT az, SUM(units)::int AS units FROM due GROUP BY az
            )
            UPDATE spot_pool p
               SET cooldown_units = GREATEST(0, p.cooldown_units - agg.units),
                   updated_at     = now()
              FROM agg
             WHERE p.az = agg.az
            RETURNING p.az, agg.units
            """
        )
        released = {r["az"]: r["units"] for r in rows}
        if released:
            log.info("pool.cooldown_released", released=released)
        return released

    async def cooldown_idle_unit_seconds(
        self, since: datetime, *, conn: asyncpg.Connection | None = None
    ) -> float:
        """Idle core-seconds spent in cooldown — the cost side of HLD §12's trade-off."""
        executor = conn or self._db
        value = await executor.fetchval(
            """
            SELECT COALESCE(SUM(
                units * EXTRACT(EPOCH FROM
                    LEAST(COALESCE(released_at, now()), now()) - GREATEST(created_at, $1))
            ), 0)::float8
              FROM spot_pool_cooldown
             WHERE COALESCE(released_at, now()) >= $1
            """,
            since,
        )
        return float(value or 0.0)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    async def get(
        self, az: str, *, conn: asyncpg.Connection | None = None
    ) -> PoolSnapshot | None:
        executor = conn or self._db
        row = await executor.fetchrow(
            f"SELECT {_POOL_COLUMNS} FROM spot_pool WHERE az = $1", az
        )
        return _snapshot(row) if row else None

    async def all(self, *, conn: asyncpg.Connection | None = None) -> list[PoolSnapshot]:
        executor = conn or self._db
        rows = await executor.fetch(
            f"SELECT {_POOL_COLUMNS} FROM spot_pool ORDER BY az"
        )
        return [_snapshot(r) for r in rows]

    # ------------------------------------------------------------------
    # the over-allocation assertion — HLD §11
    # ------------------------------------------------------------------
    async def reconcile(
        self, *, conn: asyncpg.Connection | None = None
    ) -> list[ReconcileResult]:
        """Recompute `reserved_units` from the leases that should back it.

        HLD §11 sets over-allocation to zero and says any occurrence "is a
        correctness bug, not a tuning issue". A guarantee nobody checks is a
        hope, so this runs on a timer and increments
        `spot_over_allocation_total` — the one counter whose alert threshold is
        "greater than zero, ever".

        It does not repair the drift. Silently correcting a counter would erase
        the evidence of how it drifted, and the drift is the bug.
        """
        executor = conn or self._db
        states = tuple(s.value for s in HOLDS_RESERVATION)
        rows = await executor.fetch(
            """
            SELECT p.az,
                   p.reserved_units AS counter,
                   COALESCE(SUM(l.units) FILTER (WHERE l.state = ANY($1::text[])), 0)::int
                       AS actual
              FROM spot_pool p
              LEFT JOIN spot_lease l ON l.az = p.az
             GROUP BY p.az, p.reserved_units
             ORDER BY p.az
            """,
            list(states),
        )
        results = [ReconcileResult(r["az"], r["counter"], r["actual"]) for r in rows]
        for result in results:
            if result.over_allocated:
                M.over_allocation_total.labels(az=result.az).inc()
                log.error(
                    "pool.OVER_ALLOCATION",
                    az=result.az,
                    counter=result.counter,
                    actual_lease_units=result.actual,
                    drift=result.drift,
                    remediation=(
                        "the atomic reserve was bypassed; do not patch the "
                        "counter, find the write path that skipped try_reserve"
                    ),
                )
            elif result.drift > 0:
                log.warning(
                    "pool.reserved_units_drift",
                    az=result.az,
                    counter=result.counter,
                    actual_lease_units=result.actual,
                    drift=result.drift,
                    note="conservative direction — under-sells, does not over-allocate",
                )
        return results

    async def publish_gauges(self, *, conn: asyncpg.Connection | None = None) -> None:
        """Refresh the per-AZ gauges from committed state."""
        for snapshot in await self.all(conn=conn):
            M.pool_available_units.labels(az=snapshot.az).set(snapshot.available_units)
            M.pool_degraded.labels(az=snapshot.az).set(1 if snapshot.degraded else 0)

"""Lease persistence.

HLD §6 gives the Spot Lease Manager "the lease record, discount snapshot, TTL,
state machine and audit trail. Single writer." and forbids any other component
from writing lease state. This repository is how that rule is kept once the
lease lives in a database rather than a dict: it is the only module in the
service that issues UPDATE against `spot_lease`, and every transition it offers
is *conditional* — on the current state, and optionally on the version.

That conditionality is what replaces the per-lease lock. LLD §10.1 lists
`SpotLeaseManager._locks[lease_id]` as "held across I/O in two places", with
§10.3 noting that a slow ledger therefore serialises operations on that lease.
LLD §16 gives the target: `SELECT … FOR UPDATE` on the lease row, or optimistic
`version`. Optimistic is chosen here, because the pessimistic form reproduces
exactly the hold-across-I/O problem the LLD warns about — a `FOR UPDATE` held
while the hypervisor is called is the same lock with a different name.

With optimistic control the transition is a single atomic statement, nothing is
held while an external call is in flight, and a lost race is visible as "zero
rows updated" rather than as a timeout.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

import asyncpg

from ...domain.models import (
    Lease,
    NoticeChannel,
    PreemptionReason,
    PurchaseOption,
    PurchaseOptionSource,
    RejectionCode,
    utcnow,
)
from ...domain.state_machine import HOLDS_RESERVATION, LeaseState, assert_transition
from ...logging import get_logger
from ...metrics import M

log = get_logger(__name__)

__all__ = ["LeaseRepository", "TransitionRejected", "VictimCandidate"]

_COLUMNS = """
    lease_id, tenant_id, idempotency_key, purchase_option, purchase_option_source,
    flavour, count, units, az, host_group, state, version, instance_ids,
    discount_snapshot, rate_per_sec, grace_seconds,
    created_at, admitted_at, provisioning_at, running_at, notice_at,
    force_stop_deadline, stopped_at, closed_at,
    preemption_reason, reclaim_order_id, forced_stop, notice_channels_delivered,
    grace_seconds_excluded, credit_raised, billed_seconds, billed_amount,
    rejection_code, rejection_detail, teardown_stalled, updated_at
"""

#: Columns a transition may set. Anything outside this set is a programming
#: error, not a runtime condition — the SET clause is built from these names, so
#: the allow-list is also what makes that construction safe.
_SETTABLE: frozenset[str] = frozenset(
    {
        "host_group",
        "instance_ids",
        "discount_snapshot",
        "rate_per_sec",
        "grace_seconds",
        "admitted_at",
        "provisioning_at",
        "running_at",
        "notice_at",
        "force_stop_deadline",
        "stopped_at",
        "closed_at",
        "preemption_reason",
        "reclaim_order_id",
        "forced_stop",
        "notice_channels_delivered",
        "grace_seconds_excluded",
        "credit_raised",
        "billed_seconds",
        "billed_amount",
        "rejection_code",
        "rejection_detail",
        "teardown_stalled",
        "reaper_claimed_at",
        "reaper_claimed_by",
    }
)


class TransitionRejected(RuntimeError):
    """The conditional UPDATE matched no row.

    Either another writer moved the lease first, or the version is stale. Both
    are ordinary outcomes under concurrency; the caller decides whether to
    re-read and retry (a reaper losing a race) or to treat it as a no-op (two
    reclaim orders selecting the same lease — LLD §10.4).
    """

    def __init__(self, lease_id: str, target: LeaseState, observed: str | None) -> None:
        super().__init__(
            f"lease {lease_id}: transition to {target} did not apply "
            f"(current state {observed or 'unknown'})"
        )
        self.lease_id = lease_id
        self.target = target
        self.observed = observed


class VictimCandidate:
    """A RUNNING lease considered for preemption, with the fields §6.4 orders on."""

    __slots__ = ("lease_id", "tenant_id", "host_group", "flavour", "units", "created_at", "az")

    def __init__(self, row: asyncpg.Record) -> None:
        self.lease_id: str = row["lease_id"]
        self.tenant_id: str = row["tenant_id"]
        self.host_group: str | None = row["host_group"]
        self.flavour: str = row["flavour"]
        self.units: int = row["units"]
        self.created_at: datetime = row["created_at"]
        self.az: str = row["az"]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Victim {self.lease_id} {self.units}u on {self.host_group}>"


def _to_lease(row: asyncpg.Record) -> Lease:
    return Lease(
        lease_id=row["lease_id"],
        tenant_id=row["tenant_id"],
        idempotency_key=row["idempotency_key"],
        purchase_option=PurchaseOption(row["purchase_option"]),
        purchase_option_source=PurchaseOptionSource(row["purchase_option_source"]),
        flavour=row["flavour"],
        count=row["count"],
        units=row["units"],
        az=row["az"],
        host_group=row["host_group"],
        state=LeaseState(row["state"]),
        version=row["version"],
        instance_ids=tuple(row["instance_ids"] or ()),
        discount_snapshot=row["discount_snapshot"],
        rate_per_sec=row["rate_per_sec"],
        grace_seconds=row["grace_seconds"],
        created_at=row["created_at"],
        admitted_at=row["admitted_at"],
        provisioning_at=row["provisioning_at"],
        running_at=row["running_at"],
        notice_at=row["notice_at"],
        force_stop_deadline=row["force_stop_deadline"],
        stopped_at=row["stopped_at"],
        closed_at=row["closed_at"],
        preemption_reason=(
            PreemptionReason(row["preemption_reason"])
            if row["preemption_reason"]
            else None
        ),
        reclaim_order_id=row["reclaim_order_id"],
        forced_stop=row["forced_stop"],
        notice_channels_delivered=tuple(
            NoticeChannel(c) for c in (row["notice_channels_delivered"] or ())
        ),
        grace_seconds_excluded=row["grace_seconds_excluded"],
        credit_raised=row["credit_raised"],
        billed_seconds=row["billed_seconds"],
        billed_amount=row["billed_amount"],
        rejection_code=(
            RejectionCode(row["rejection_code"]) if row["rejection_code"] else None
        ),
        rejection_detail=row["rejection_detail"],
        teardown_stalled=row["teardown_stalled"],
        updated_at=row["updated_at"],
    )


class LeaseRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # create — edge 8
    # ------------------------------------------------------------------
    async def create(
        self, lease: Lease, *, conn: asyncpg.Connection | None = None
    ) -> Lease:
        """Insert a new lease.

        Normally called inside the same transaction as the reserve and the
        idempotency-key row, so that a crash cannot leave units reserved with no
        lease to account for them.
        """
        executor = conn or self._db
        row = await executor.fetchrow(
            f"""
            INSERT INTO spot_lease (
                lease_id, tenant_id, idempotency_key, purchase_option,
                purchase_option_source, flavour, count, units, az, state,
                discount_snapshot, rate_per_sec, grace_seconds, admitted_at,
                created_at, version
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,0)
            RETURNING {_COLUMNS}
            """,
            lease.lease_id,
            lease.tenant_id,
            lease.idempotency_key,
            lease.purchase_option.value,
            lease.purchase_option_source.value,
            lease.flavour,
            lease.count,
            lease.units,
            lease.az,
            lease.state.value,
            lease.discount_snapshot,
            lease.rate_per_sec,
            lease.grace_seconds,
            lease.admitted_at,
            lease.created_at,
        )
        assert row is not None
        return _to_lease(row)

    # ------------------------------------------------------------------
    # the conditional transition
    # ------------------------------------------------------------------
    async def transition(
        self,
        lease_id: str,
        *,
        to: LeaseState,
        require_state: Iterable[LeaseState] | None = None,
        expect_version: int | None = None,
        conn: asyncpg.Connection | None = None,
        **fields: Any,
    ) -> Lease:
        """Move a lease, conditionally, in one statement.

        `require_state` guards the transition against a concurrent writer, and
        is checked against the state machine first so an illegal target is
        caught as a programming error rather than as a silent no-op.

        Raises `TransitionRejected` when no row matched. Callers that expect to
        lose the race routinely — the reaper, a second reclaim order — should
        use `try_transition` instead.
        """
        if require_state:
            for state in require_state:
                assert_transition(state, to, lease_id)

        unknown = set(fields) - _SETTABLE
        if unknown:
            raise ValueError(f"cannot set {sorted(unknown)} on a lease transition")

        assignments = ["state = $2", "version = version + 1", "updated_at = now()"]
        args: list[Any] = [lease_id, to.value]

        for name, value in fields.items():
            args.append(_adapt(name, value))
            assignments.append(f"{name} = ${len(args)}")

        where = ["lease_id = $1"]
        if require_state is not None:
            args.append([s.value for s in require_state])
            where.append(f"state = ANY(${len(args)}::text[])")
        if expect_version is not None:
            args.append(expect_version)
            where.append(f"version = ${len(args)}")

        row = await (conn or self._db).fetchrow(
            f"""
            UPDATE spot_lease
               SET {', '.join(assignments)}
             WHERE {' AND '.join(where)}
            RETURNING {_COLUMNS}
            """,
            *args,
        )
        if row is None:
            observed = await (conn or self._db).fetchval(
                "SELECT state FROM spot_lease WHERE lease_id = $1", lease_id
            )
            raise TransitionRejected(lease_id, to, observed)
        return _to_lease(row)

    async def try_transition(
        self,
        lease_id: str,
        *,
        to: LeaseState,
        require_state: Iterable[LeaseState] | None = None,
        expect_version: int | None = None,
        conn: asyncpg.Connection | None = None,
        **fields: Any,
    ) -> Lease | None:
        """`transition`, returning None instead of raising when it does not apply.

        This is the shape LLD §10.4 relies on for "two reclaim orders select the
        same lease": preempt is a no-op unless the lease is RUNNING, and the
        loser simply gets None.
        """
        try:
            return await self.transition(
                lease_id,
                to=to,
                require_state=require_state,
                expect_version=expect_version,
                conn=conn,
                **fields,
            )
        except TransitionRejected:
            return None

    async def set_fields(
        self, lease_id: str, *, conn: asyncpg.Connection | None = None, **fields: Any
    ) -> Lease | None:
        """Update fields without changing state, still bumping the version.

        Used for the LLD §12.7 fix: `notice_channels_delivered` was written
        outside the per-lease lock, so a concurrent describe could observe a
        lease in NOTICE_ISSUED with an empty channel list. Here it is a single
        atomic replace on the row, so no reader can see the intermediate state.
        """
        unknown = set(fields) - _SETTABLE
        if unknown:
            raise ValueError(f"cannot set {sorted(unknown)} on a lease")
        if not fields:
            return await self.get(lease_id, conn=conn)

        args: list[Any] = [lease_id]
        assignments = ["version = version + 1", "updated_at = now()"]
        for name, value in fields.items():
            args.append(_adapt(name, value))
            assignments.append(f"{name} = ${len(args)}")

        row = await (conn or self._db).fetchrow(
            f"""
            UPDATE spot_lease SET {', '.join(assignments)}
             WHERE lease_id = $1
            RETURNING {_COLUMNS}
            """,
            *args,
        )
        return _to_lease(row) if row else None

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    async def get(
        self, lease_id: str, *, conn: asyncpg.Connection | None = None
    ) -> Lease | None:
        row = await (conn or self._db).fetchrow(
            f"SELECT {_COLUMNS} FROM spot_lease WHERE lease_id = $1", lease_id
        )
        return _to_lease(row) if row else None

    async def get_for_tenant(
        self, lease_id: str, tenant_id: str, *, conn: asyncpg.Connection | None = None
    ) -> Lease | None:
        """Tenant-scoped read. A lease id from another tenant must look absent,
        not forbidden — a 403 here would confirm the id exists."""
        row = await (conn or self._db).fetchrow(
            f"SELECT {_COLUMNS} FROM spot_lease WHERE lease_id = $1 AND tenant_id = $2",
            lease_id,
            tenant_id,
        )
        return _to_lease(row) if row else None

    async def get_by_idempotency_key(
        self, tenant_id: str, key: str, *, conn: asyncpg.Connection | None = None
    ) -> Lease | None:
        row = await (conn or self._db).fetchrow(
            f"""
            SELECT {_COLUMNS} FROM spot_lease
             WHERE tenant_id = $1 AND idempotency_key = $2
            """,
            tenant_id,
            key,
        )
        return _to_lease(row) if row else None

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        states: Sequence[LeaseState] | None = None,
        limit: int = 100,
        offset: int = 0,
        conn: asyncpg.Connection | None = None,
    ) -> list[Lease]:
        if states:
            rows = await (conn or self._db).fetch(
                f"""
                SELECT {_COLUMNS} FROM spot_lease
                 WHERE tenant_id = $1 AND state = ANY($2::text[])
                 ORDER BY created_at DESC LIMIT $3 OFFSET $4
                """,
                tenant_id,
                [s.value for s in states],
                limit,
                offset,
            )
        else:
            rows = await (conn or self._db).fetch(
                f"""
                SELECT {_COLUMNS} FROM spot_lease
                 WHERE tenant_id = $1
                 ORDER BY created_at DESC LIMIT $2 OFFSET $3
                """,
                tenant_id,
                limit,
                offset,
            )
        return [_to_lease(r) for r in rows]

    # ------------------------------------------------------------------
    # operator views — cross-tenant, never reachable from the customer surface
    # ------------------------------------------------------------------
    async def list_all(
        self,
        *,
        states: Sequence[LeaseState] | None = None,
        az: str | None = None,
        tenant_id: str | None = None,
        host_group: str | None = None,
        limit: int = 200,
        offset: int = 0,
        conn: asyncpg.Connection | None = None,
    ) -> list[Lease]:
        """Every tenant's leases, filtered. The operator fleet view.

        Deliberately separate from `list_for_tenant` rather than that method with
        an optional tenant. A cross-tenant read reached by leaving an argument
        out is one forgotten argument away from a customer seeing another
        customer's fleet; making it a different method means the console route
        has to ask for it by name.
        """
        rows = await (conn or self._db).fetch(
            f"""
            SELECT {_COLUMNS} FROM spot_lease
             WHERE ($1::text[] IS NULL OR state = ANY($1::text[]))
               AND ($2::text   IS NULL OR az = $2)
               AND ($3::text   IS NULL OR tenant_id = $3)
               AND ($4::text   IS NULL OR host_group = $4)
             ORDER BY created_at DESC
             LIMIT $5 OFFSET $6
            """,
            [s.value for s in states] if states else None,
            az,
            tenant_id,
            host_group,
            limit,
            offset,
        )
        return [_to_lease(r) for r in rows]

    async def timeline(
        self,
        *,
        since: datetime,
        until: datetime,
        buckets: int = 60,
        conn: asyncpg.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Units held and preemptions over time, in equal buckets.

        Derived entirely from `spot_lease` timestamps rather than from a sampled
        history table, which is what makes it survive a restart and agree across
        replicas: the same window queried from any process returns the same
        series, because the series is a function of committed rows and not of
        what some process happened to observe while it was up.

        A lease counts as *held* in a bucket when its life overlaps that bucket —
        `running_at` to whichever of `closed_at` / `stopped_at` / now ended it.
        That is the same interval the capacity ledger accounts for, so the chart
        and the ledger cannot tell different stories.
        """
        rows = await (conn or self._db).fetch(
            """
            WITH bounds AS (
                SELECT $1::timestamptz AS lo, $2::timestamptz AS hi, $3::int AS n
            ),
            grid AS (
                SELECT
                    lo + (hi - lo) * (i - 1) / n AS bucket_start,
                    lo + (hi - lo) * i / n       AS bucket_end
                  FROM bounds, generate_series(1, (SELECT n FROM bounds)) AS i
            )
            SELECT
                g.bucket_start,
                g.bucket_end,
                COALESCE(SUM(l.units) FILTER (
                    WHERE l.running_at IS NOT NULL
                      AND l.running_at < g.bucket_end
                      AND COALESCE(l.closed_at, l.stopped_at, g.bucket_end)
                          >= g.bucket_start
                ), 0)::int AS held_units,
                COUNT(*) FILTER (
                    WHERE l.notice_at >= g.bucket_start
                      AND l.notice_at <  g.bucket_end
                )::int AS notices,
                COUNT(*) FILTER (
                    WHERE l.forced_stop
                      AND l.stopped_at >= g.bucket_start
                      AND l.stopped_at <  g.bucket_end
                )::int AS forced_stops,
                COUNT(*) FILTER (
                    WHERE l.admitted_at >= g.bucket_start
                      AND l.admitted_at <  g.bucket_end
                )::int AS admissions
              FROM grid g
              LEFT JOIN spot_lease l ON true
             GROUP BY g.bucket_start, g.bucket_end
             ORDER BY g.bucket_start
            """,
            since,
            until,
            buckets,
        )
        return [
            {
                "at": r["bucket_end"].isoformat(),
                "held_units": r["held_units"],
                "notices": r["notices"],
                "forced_stops": r["forced_stops"],
                "admissions": r["admissions"],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # quota — enforced before any capacity work is done (HLD §6)
    # ------------------------------------------------------------------
    async def tenant_usage(
        self, tenant_id: str, *, conn: asyncpg.Connection | None = None
    ) -> tuple[int, int]:
        """(units held, lease count) across every state that still holds units.

        Includes STOPPED: a lease whose teardown has not been confirmed is still
        occupying capacity, so it must still count against the tenant's quota.
        Excluding it would let a tenant exceed their ceiling during any wave of
        preemptions, which is exactly when the ceiling matters most.
        """
        row = await (conn or self._db).fetchrow(
            """
            SELECT COALESCE(SUM(units), 0)::int AS units, COUNT(*)::int AS leases
              FROM spot_lease
             WHERE tenant_id = $1 AND state = ANY($2::text[])
            """,
            tenant_id,
            [s.value for s in HOLDS_RESERVATION],
        )
        return (row["units"], row["leases"]) if row else (0, 0)

    # ------------------------------------------------------------------
    # victim selection — edge 21/22 input
    # ------------------------------------------------------------------
    async def victim_candidates(
        self,
        az: str,
        *,
        host_group: str | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> list[VictimCandidate]:
        """RUNNING leases eligible for preemption.

        Only RUNNING: a lease that never ran is cancelled outright rather than
        preempted, and one already draining is on its way out.

        The `host_group IS NOT NULL` filter closes LLD §12.6, where the selector
        "can include leases with host_group = None when the order names a host
        group", so "an unplaced lease may be killed to satisfy an order for a
        host it was never going to land on". A host-scoped order can only be
        satisfied by leases actually on that host, so unplaced leases are
        excluded by the query rather than by a check the caller might forget.
        """
        if host_group is not None:
            rows = await (conn or self._db).fetch(
                """
                SELECT lease_id, tenant_id, host_group, flavour, units, created_at, az
                  FROM spot_lease
                 WHERE az = $1 AND state = 'RUNNING'
                   AND host_group = $2
                 ORDER BY created_at DESC
                """,
                az,
                host_group,
            )
        else:
            rows = await (conn or self._db).fetch(
                """
                SELECT lease_id, tenant_id, host_group, flavour, units, created_at, az
                  FROM spot_lease
                 WHERE az = $1 AND state = 'RUNNING'
                   AND host_group IS NOT NULL
                 ORDER BY created_at DESC
                """,
                az,
            )
        return [VictimCandidate(r) for r in rows]

    async def tenant_fleet_units(
        self, az: str, *, conn: asyncpg.Connection | None = None
    ) -> dict[str, int]:
        """Running units per tenant in an AZ — the denominator for blast radius."""
        rows = await (conn or self._db).fetch(
            """
            SELECT tenant_id, SUM(units)::int AS units
              FROM spot_lease
             WHERE az = $1 AND state = 'RUNNING'
             GROUP BY tenant_id
            """,
            az,
        )
        return {r["tenant_id"]: r["units"] for r in rows}

    # ------------------------------------------------------------------
    # THE REAPER — LLD §12.3
    # ------------------------------------------------------------------
    async def claim_expired_notices(
        self,
        *,
        worker: str,
        batch: int,
        claim_ttl: float,
        conn: asyncpg.Connection | None = None,
    ) -> list[Lease]:
        """Claim leases whose grace window has run out.

        This is the fix for the most dangerous gap in the design. LLD §12.3:

            "Grace timers are in-memory asyncio.Tasks with no persistence. A
            restart during a grace window strands the lease in NOTICE_ISSUED
            forever: never stopped, never billed, capacity never returned."

        and its prescribed fix: "Persist notice_at; on startup and every 5 s,
        reap leases where now > notice_at + force_stop_at and force-stop them."

        The deadline is persisted as an absolute timestamp, so recovery needs no
        knowledge of when the process started or what the config was when the
        notice was issued.

        `FOR UPDATE SKIP LOCKED` plus a claim stamp is what makes this safe on N
        replicas — LLD §16 names the reaper as "the one component that must not
        run N times concurrently on the same lease", and answers it with "a
        conditional UPDATE that claims the lease before force-stopping it".
        A claim older than `claim_ttl` is re-claimable, so a worker that dies
        mid-reap does not park the lease forever.
        """
        rows = await (conn or self._db).fetch(
            f"""
            WITH due AS (
                SELECT lease_id
                  FROM spot_lease
                 WHERE state = 'NOTICE_ISSUED'
                   AND force_stop_deadline <= now()
                   AND (reaper_claimed_at IS NULL
                        OR reaper_claimed_at < now() - make_interval(secs => $2::float8))
                 ORDER BY force_stop_deadline
                 FOR UPDATE SKIP LOCKED
                 LIMIT $3
            )
            UPDATE spot_lease l
               SET reaper_claimed_at = now(),
                   reaper_claimed_by = $1,
                   version = l.version + 1
              FROM due
             WHERE l.lease_id = due.lease_id
            RETURNING {', '.join('l.' + c.strip() for c in _COLUMNS.split(','))}
            """,
            worker,
            float(claim_ttl),
            batch,
        )
        if rows:
            M.reaper_claimed_total.labels(reason="grace_expired").inc(len(rows))
            log.warning(
                "reaper.claimed",
                count=len(rows),
                worker=worker,
                lease_ids=[r["lease_id"] for r in rows],
                note="grace window elapsed without a clean guest exit",
            )
        return [_to_lease(r) for r in rows]

    async def claim_stalled_teardowns(
        self,
        *,
        worker: str,
        budget_seconds: float,
        batch: int,
        conn: asyncpg.Connection | None = None,
    ) -> list[Lease]:
        """STOPPED leases whose teardown has outrun its budget.

        LLD §11: "Teardown stalls -> units stay RECLAIMING; lease stays STOPPED;
        the stalled list grows." Their capacity must never be reported free,
        which is why they are surfaced rather than closed out.
        """
        rows = await (conn or self._db).fetch(
            f"""
            WITH due AS (
                SELECT lease_id
                  FROM spot_lease
                 WHERE state = 'STOPPED'
                   AND stopped_at <= now() - make_interval(secs => $1::float8)
                 ORDER BY stopped_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT $2
            )
            UPDATE spot_lease l
               SET reaper_claimed_by = $3,
                   reaper_claimed_at = now(),
                   version = l.version + 1
              FROM due
             WHERE l.lease_id = due.lease_id
            RETURNING {', '.join('l.' + c.strip() for c in _COLUMNS.split(','))}
            """,
            float(budget_seconds),
            batch,
            worker,
        )
        return [_to_lease(r) for r in rows]

    async def outstanding_notices(
        self, *, conn: asyncpg.Connection | None = None
    ) -> list[Lease]:
        """Every lease currently inside its grace window.

        Called at startup (LLD §11.1: "rehydrate leases; re-arm timers"). With a
        persisted deadline there is nothing to re-arm — the reaper picks them up
        on its next tick — but the count is logged so a restart that inherits
        in-flight preemptions says so out loud.
        """
        rows = await (conn or self._db).fetch(
            f"""
            SELECT {_COLUMNS} FROM spot_lease
             WHERE state IN ('NOTICE_ISSUED', 'DRAINING')
             ORDER BY force_stop_deadline NULLS LAST
            """
        )
        return [_to_lease(r) for r in rows]

    # ------------------------------------------------------------------
    # metering and reporting
    # ------------------------------------------------------------------
    async def running_leases(
        self, *, limit: int = 5000, conn: asyncpg.Connection | None = None
    ) -> list[Lease]:
        rows = await (conn or self._db).fetch(
            f"SELECT {_COLUMNS} FROM spot_lease WHERE state = 'RUNNING' LIMIT $1",
            limit,
        )
        return [_to_lease(r) for r in rows]

    async def counts_by_state(
        self, *, conn: asyncpg.Connection | None = None
    ) -> dict[str, int]:
        rows = await (conn or self._db).fetch(
            "SELECT state, COUNT(*)::int AS n FROM spot_lease GROUP BY state"
        )
        counts = {r["state"]: r["n"] for r in rows}
        for state in LeaseState:
            M.lease_state.labels(state=state.value).set(counts.get(state.value, 0))
        return counts

    async def preemptions_since(
        self, since: datetime, *, conn: asyncpg.Connection | None = None
    ) -> list[asyncpg.Record]:
        """Closed preempted leases, for the interruption rate and the SLO."""
        return await (conn or self._db).fetch(
            """
            SELECT lease_id, tenant_id, flavour, az, units, forced_stop,
                   notice_at, stopped_at, closed_at, grace_seconds,
                   jsonb_array_length(notice_channels_delivered) AS channels
              FROM spot_lease
             WHERE preemption_reason = 'capacity_reclaim'
               AND notice_at >= $1
             ORDER BY notice_at
            """,
            since,
        )

    async def lease_seconds_since(
        self, since: datetime, *, conn: asyncpg.Connection | None = None
    ) -> list[asyncpg.Record]:
        """Running lease-seconds per (flavour, az, tenant) — the rate denominator."""
        return await (conn or self._db).fetch(
            """
            SELECT flavour, az, tenant_id,
                   COUNT(*)::int AS leases,
                   COALESCE(SUM(EXTRACT(EPOCH FROM
                       LEAST(COALESCE(closed_at, now()), now())
                       - GREATEST(running_at, $1)
                   )), 0)::float8 AS lease_seconds
              FROM spot_lease
             WHERE running_at IS NOT NULL
               AND COALESCE(closed_at, now()) >= $1
             GROUP BY flavour, az, tenant_id
            """,
            since,
        )


def _adapt(name: str, value: Any) -> Any:
    """Convert enums and tuples to what asyncpg expects for that column."""
    if value is None:
        return None
    if name in {"instance_ids", "notice_channels_delivered"}:
        return [str(v) for v in value]
    if isinstance(value, (PreemptionReason, RejectionCode, LeaseState, NoticeChannel)):
        return value.value
    return value

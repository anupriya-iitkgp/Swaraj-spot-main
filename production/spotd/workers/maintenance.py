"""The remaining background loops.

Five workers, each closing a specific gap or meeting a specific target:

* `PoolRefresher`      — edge 19, one control cycle. Leader-elected, because N
                         replicas all applying the same publication is wasted
                         work and makes the cycle sequence meaningless.
* `FulfilmentSweeper`  — picks up ADMITTED leases whose in-process fulfilment
                         task died with the process. Same class of gap as
                         LLD §12.3, on the launch path instead of the
                         preemption path.
* `TeardownSweeper`    — retries stalled teardowns so units in RECLAIMING
                         eventually come back, and completes reclaim orders once
                         every selected lease has closed.
* `RetentionSweeper`   — the TTL sweeps of LLD §12.4 and §12.5, plus outbox
                         pruning and nonce expiry.
* `AnalyticsWorker`    — the hourly interruption-rate refresh HLD §11 requires,
                         plus the reclaim SLO and the fairness spread.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from ..config import Settings
from ..domain.models import utcnow
from ..logging import get_logger, lease_context
from ..metrics import M
from .base import LeaderElectedWorker, PeriodicWorker

log = get_logger(__name__)

__all__ = [
    "PoolRefresher",
    "FulfilmentSweeper",
    "TeardownSweeper",
    "RetentionSweeper",
    "AnalyticsWorker",
]


class PoolRefresher(LeaderElectedWorker):
    """Edge 19 — one control cycle: refresh, expire cooldowns, reconcile."""

    name = "pool_refresher"
    lock_name = "pool_refresh"

    def __init__(
        self,
        *,
        settings: Settings,
        leader_repo: Any,
        pool_view: Any,
        pool_repo: Any,
    ) -> None:
        super().__init__(
            interval=settings.control_cycle, settings=settings, leader_repo=leader_repo
        )
        self._pool_view = pool_view
        self._pool = pool_repo

    async def lead(self) -> None:
        await self._pool_view.refresh_all()
        await self._pool_view.expire_cooldowns()
        # HLD §11 sets over-allocation to zero and calls any occurrence a
        # correctness bug. Checking it every cycle is what turns that from an
        # assertion in a document into a signal on a dashboard.
        await self._pool.reconcile()
        await self._pool.publish_gauges()


class FulfilmentSweeper(PeriodicWorker):
    """Fulfil ADMITTED leases that nobody is working on.

    The API spawns an in-process task to fulfil each admission, which keeps
    launch latency low. That task does not survive a restart, so a lease
    admitted a moment before a deploy would sit in ADMITTED forever — holding a
    reservation, billed for nothing, never provisioned.

    The transition ADMITTED -> PROVISIONING is itself the claim, so this sweeper
    and the in-process task cannot both fulfil the same lease; whichever gets
    there first wins and the other sees zero rows updated.
    """

    name = "fulfilment_sweeper"

    def __init__(
        self,
        *,
        settings: Settings,
        db: Any,
        lease_manager: Any,
        stale_after: float = 20.0,
    ) -> None:
        super().__init__(interval=10.0, settings=settings)
        self._db = db
        self._manager = lease_manager
        self._stale_after = stale_after

    async def tick(self) -> None:
        rows = await self._db.fetch(
            """
            SELECT lease_id FROM spot_lease
             WHERE state = 'ADMITTED'
               AND admitted_at <= now() - make_interval(secs => $1::float8)
             ORDER BY admitted_at
             LIMIT 100
            """,
            float(self._stale_after),
        )
        if not rows:
            return

        log.warning(
            "fulfilment.recovering_stranded_leases",
            count=len(rows),
            stale_after_seconds=self._stale_after,
            note="these were admitted but never provisioned — most likely a "
            "restart between admission and fulfilment",
        )
        for row in rows:
            try:
                await self._manager.fulfil(row["lease_id"])
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "fulfilment.recovery_failed",
                    lease_id=row["lease_id"],
                    error=str(exc),
                )


class TeardownSweeper(PeriodicWorker):
    """Retry stalled teardowns, and complete reclaim orders that have drained.

    LLD §11 on a stalled teardown: "Units stay RECLAIMING; lease stays STOPPED;
    the `stalled` list grows. Alert; manual or automated sweeper; SLO breach
    recorded." This is the automated sweeper.
    """

    name = "teardown_sweeper"

    def __init__(
        self,
        *,
        settings: Settings,
        db: Any,
        lease_repo: Any,
        lease_manager: Any,
        reclaim_handler: Any,
    ) -> None:
        super().__init__(interval=settings.teardown_sweep_interval, settings=settings)
        self._db = db
        self._leases = lease_repo
        self._manager = lease_manager
        self._reclaim = reclaim_handler

    async def tick(self) -> None:
        stalled = await self._leases.claim_stalled_teardowns(
            worker=self._settings.worker_id,
            budget_seconds=self._settings.teardown_budget,
            batch=50,
        )
        for lease in stalled:
            with lease_context(lease.lease_id, lease.tenant_id):
                try:
                    closed = await self._manager.finish_teardown(lease)
                    if closed is None:
                        log.warning(
                            "teardown.still_stalled",
                            lease_id=lease.lease_id,
                            host_group=lease.host_group,
                            units=lease.units,
                            stopped_at=lease.stopped_at.isoformat()
                            if lease.stopped_at
                            else None,
                            note="units remain accounted for and are not "
                            "reported free",
                        )
                except Exception as exc:  # noqa: BLE001
                    log.exception(
                        "teardown.retry_failed", lease_id=lease.lease_id, error=str(exc)
                    )

        await self._publish_stalled_gauge()
        await self._complete_drained_orders()

    async def _publish_stalled_gauge(self) -> None:
        rows = await self._db.fetch(
            """
            SELECT COALESCE(host_group, 'unplaced') AS host_group, COUNT(*)::int AS n
              FROM spot_lease
             WHERE state = 'STOPPED' AND teardown_stalled
             GROUP BY 1
            """
        )
        for row in rows:
            M.teardown_stalled.labels(host_group=row["host_group"]).set(row["n"])

    async def _complete_drained_orders(self) -> None:
        for order in await self._reclaim.in_flight_orders():
            await self._reclaim.complete_if_drained(order.order_id)


class RetentionSweeper(PeriodicWorker):
    """TTL sweeps — LLD §12.4 and §12.5, plus outbox and nonce hygiene."""

    name = "retention_sweeper"

    def __init__(
        self,
        *,
        settings: Settings,
        idempotency_repo: Any,
        nonce_repo: Any,
        rate_limit_repo: Any,
        outbox_repo: Any,
    ) -> None:
        super().__init__(interval=settings.sweeper_interval, settings=settings)
        self._idem = idempotency_repo
        self._nonces = nonce_repo
        self._rate = rate_limit_repo
        self._outbox = outbox_repo

    async def tick(self) -> None:
        swept = {
            # §12.4: keys that are never retried are now evicted on their TTL
            # rather than only when looked up again.
            "idempotency_keys": await self._idem.sweep(),
            "hmac_nonces": await self._nonces.sweep(),
            "rate_limit_buckets": await self._rate.sweep(),
            # Published outbox rows are kept for a week as delivery evidence,
            # then pruned. Retention is not zero: when a downstream claims it
            # never received something, this is the proof it was sent.
            "outbox_rows": await self._outbox.prune_published(
                older_than_seconds=7 * 24 * 3600
            ),
        }
        if any(swept.values()):
            log.info("retention.swept", **swept)


class AnalyticsWorker(LeaderElectedWorker):
    """Edges 29 and 30, plus the SLO. Leader-elected: the rollup is idempotent
    but recomputing it on every replica is pure waste."""

    name = "analytics"
    lock_name = "analytics_rollup"

    def __init__(
        self,
        *,
        settings: Settings,
        leader_repo: Any,
        analytics: Any,
        lease_repo: Any,
        billing_repo: Any,
        billing_system: Any,
    ) -> None:
        super().__init__(
            interval=settings.analytics_interval,
            settings=settings,
            leader_repo=leader_repo,
        )
        self._analytics = analytics
        self._leases = lease_repo
        self._billing_repo = billing_repo
        self._billing = billing_system

    async def lead(self) -> None:
        await self._analytics.roll_up(window=timedelta(hours=24))
        await self._analytics.slo(window=timedelta(hours=24))
        await self._leases.counts_by_state()
        await self._submit_billing()

    async def _submit_billing(self) -> None:
        """Edge 26 — hand rated records to billing, and never drop one.

        LLD §9: "queue locally and retry; never drop a usage record." The
        records live in `usage_record` until the billing system acknowledges
        them, so a billing outage delays revenue recognition rather than
        losing it.
        """
        usage = await self._billing_repo.unsubmitted_usage(limit=500)
        if usage:
            try:
                ref = await self._billing.submit_usage(
                    [
                        {
                            "lease_id": r["lease_id"],
                            "tenant_id": r["tenant_id"],
                            "window_start": r["window_start"].isoformat(),
                            "window_end": r["window_end"].isoformat(),
                            "units": r["units"],
                            "billable_seconds": r["billable_seconds"],
                            "rate_per_sec": r["rate_per_sec"],
                            "discount": r["discount"],
                            "amount": r["amount"],
                        }
                        for r in usage
                    ]
                )
                await self._billing_repo.mark_usage_submitted(
                    [r["id"] for r in usage], ref
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "billing.usage_submit_failed",
                    pending=len(usage),
                    error=str(exc),
                    note="records stay unsubmitted locally and are retried",
                )

        credits = await self._billing_repo.unsubmitted_credits(limit=500)
        if credits:
            try:
                ref = await self._billing.submit_credit(
                    [
                        {
                            "credit_id": c["credit_id"],
                            "lease_id": c["lease_id"],
                            "tenant_id": c["tenant_id"],
                            "reason": c["reason"],
                            "amount": c["amount"],
                        }
                        for c in credits
                    ]
                )
                await self._billing_repo.mark_credits_submitted(
                    [c["credit_id"] for c in credits], ref
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "billing.credit_submit_failed",
                    pending=len(credits),
                    error=str(exc),
                )

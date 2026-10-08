"""Composition root — this file *is* the wiring diagram.

Every arrow in HLD §5's connection table is one constructor argument or one
assignment below, and the edge numbers are in the comments so the file can be
read against the diagram. Nothing else in the service constructs its own
collaborators; if a dependency is not visible here, it does not exist.

Startup and shutdown follow LLD §11.1, with one difference that is worth being
explicit about:

    start():   pool.refresh()             # never serve inventory from an empty pool
               pool.start()               # control-cycle task
               [production] rehydrate leases; re-arm timers; sweep expired idem keys

    stop():    grace_timer.cancel_all()   # in-flight preemptions abandoned
               pool.stop()
               [production] drain in-flight fulfilment before exit

There are no timers to re-arm and none to cancel. Deadlines live in
`spot_lease.force_stop_deadline`, so a preemption in flight during a restart is
picked up by the reaper on its next tick — and shutdown cannot abandon one,
because there was never a timer object holding it. The idempotency sweep is a
worker rather than a startup step, for the same reason: a step that only runs at
boot is a step that stops running when boot stops happening.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from .bus import EventBus
from .config import Settings, get_settings
from .core import (
    AdmissionController,
    EligibilityGuard,
    InterruptionAnalytics,
    MeteringService,
    NoticeDeliveryService,
    PlacementAdapter,
    PricingEngine,
    ProvisioningAdapter,
    ReclaimOrderHandler,
    SpotLeaseManager,
    SpotMarketAPI,
    SpotPoolView,
    TeardownConfirmer,
    VictimSelector,
)
from .db import Database, connect
from .db.repositories import (
    AnalyticsRepository,
    AuditRepository,
    BillingRepository,
    IdempotencyRepository,
    LeaderRepository,
    LeaseRepository,
    LedgerRepository,
    NonceRepository,
    OutboxRepository,
    PoolRepository,
    RateLimitRepository,
    ReclaimRepository,
    ReferenceRepository,
)
from .external import Externals, build_externals
from .logging import configure_logging, get_logger
from .metrics import M
from .workers import (
    AnalyticsWorker,
    FulfilmentSweeper,
    GraceReaper,
    OutboxRelay,
    PoolRefresher,
    RetentionSweeper,
    TeardownSweeper,
)

log = get_logger(__name__)

__all__ = ["Container", "build"]


@dataclass(slots=True)
class Container:
    settings: Settings
    db: Database
    externals: Externals
    bus: EventBus

    # repositories
    pool_repo: PoolRepository
    lease_repo: LeaseRepository
    reference_repo: ReferenceRepository
    idempotency_repo: IdempotencyRepository
    outbox_repo: OutboxRepository
    audit_repo: AuditRepository
    reclaim_repo: ReclaimRepository
    billing_repo: BillingRepository
    ledger_repo: LedgerRepository
    analytics_repo: AnalyticsRepository
    leader_repo: LeaderRepository
    rate_limit_repo: RateLimitRepository
    nonce_repo: NonceRepository

    # core components — the solid boxes of HLD §4
    pricing: PricingEngine
    pool_view: SpotPoolView
    guard: EligibilityGuard
    admission: AdmissionController
    placement: PlacementAdapter
    provisioning: ProvisioningAdapter
    notice: NoticeDeliveryService
    teardown: TeardownConfirmer
    metering: MeteringService
    lease_manager: SpotLeaseManager
    victim_selector: VictimSelector
    reclaim_handler: ReclaimOrderHandler
    analytics: InterruptionAnalytics
    market: SpotMarketAPI

    # workers
    workers: list[Any] = field(default_factory=list)
    _http: httpx.AsyncClient | None = None
    _fulfilment_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    # ------------------------------------------------------------------
    async def start(self) -> None:
        """LLD §11.1 start(). Refresh before serving; never an empty pool."""
        await self.pool_view.ensure()
        try:
            await self.pool_view.refresh_all()
        except Exception as exc:  # noqa: BLE001
            # A pool row that exists and honestly says zero is better than a
            # failed boot: the reclaim path still works, and the refresher will
            # retry every control cycle.
            log.error(
                "startup.initial_refresh_failed",
                error=str(exc),
                note="serving zero inventory until the forecast feed recovers",
            )

        outstanding = await self.lease_repo.outstanding_notices()
        if outstanding:
            log.warning(
                "startup.preemptions_in_flight",
                count=len(outstanding),
                note="deadlines are persisted; the reaper resumes them with no "
                "re-arming (LLD §12.3)",
            )

        for worker in self.workers:
            worker.start()

        await self.lease_repo.counts_by_state()
        await self.pool_repo.publish_gauges()
        log.info(
            "startup.complete",
            environment=self.settings.environment,
            backend=self.settings.backend,
            worker_id=self.settings.worker_id,
            workers=[w.name for w in self.workers],
            azs=list(self.settings.availability_zones),
        )

    async def stop(self) -> None:
        """Drain in-flight fulfilment, then stop the workers."""
        if self._fulfilment_tasks:
            log.info("shutdown.draining_fulfilment", tasks=len(self._fulfilment_tasks))
            await asyncio.gather(*self._fulfilment_tasks, return_exceptions=True)

        for worker in reversed(self.workers):
            await worker.stop()

        await self.externals.aclose()
        if self._http is not None:
            await self._http.aclose()
        await self.db.close()
        log.info("shutdown.complete")

    # ------------------------------------------------------------------
    def spawn_fulfilment(self, lease_id: str) -> None:
        """Fulfil off the request path (edges 9-12 are async in HLD §5).

        The task is tracked so shutdown can drain it. If the process dies
        anyway, `FulfilmentSweeper` picks the lease up — the in-process task is
        a latency optimisation, never the correctness path.
        """
        task = asyncio.create_task(self.lease_manager.fulfil(lease_id))
        self._fulfilment_tasks.add(task)
        task.add_done_callback(self._fulfilment_tasks.discard)

    async def health(self) -> dict[str, Any]:
        db_ok = await self.db.healthy()
        pools = await self.pool_repo.all()
        pending, dead = await self.outbox_repo.depth()
        return {
            "status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "unreachable",
            "backend": self.settings.backend,
            "workers": {w.name: w.running for w in self.workers},
            "pools": {
                p.az: {
                    "available_units": p.available_units,
                    "degraded": p.degraded,
                    "staleness_seconds": round(p.staleness_seconds(), 1),
                }
                for p in pools
            },
            "outbox": {"pending": pending, "dead": dead},
        }


# ======================================================================
async def build(settings: Settings | None = None) -> Container:
    """Construct the whole graph. One pass, no lazy resolution, no globals."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format, settings.service_name)

    db = await connect(settings)
    externals = build_externals(settings, db)
    bus = EventBus()

    # -- repositories --------------------------------------------------
    pool_repo = PoolRepository(db)
    lease_repo = LeaseRepository(db)
    reference_repo = ReferenceRepository(db)
    idempotency_repo = IdempotencyRepository(db)
    outbox_repo = OutboxRepository(db)
    audit_repo = AuditRepository(db)
    reclaim_repo = ReclaimRepository(db)
    billing_repo = BillingRepository(db)
    ledger_repo = LedgerRepository(db)
    analytics_repo = AnalyticsRepository(db)
    leader_repo = LeaderRepository(db)
    rate_limit_repo = RateLimitRepository(db)
    nonce_repo = NonceRepository(db)

    http = httpx.AsyncClient(timeout=httpx.Timeout(5.0))

    # -- core ----------------------------------------------------------
    pricing = PricingEngine(settings)

    # edge 19: Forecast & Headroom -> Spot Pool View
    pool_view = SpotPoolView(
        settings=settings,
        pool_repo=pool_repo,
        forecast=externals.forecast,
        audit=audit_repo,
    )

    # edge 1/5: Account Service -> Eligibility & Quota Guard
    guard = EligibilityGuard(
        settings=settings,
        accounts=externals.accounts,
        reference=reference_repo,
        leases=lease_repo,
    )

    # edges 7, 8, 31: Admission Controller <-> Pool, -> Lease Manager
    admission = AdmissionController(
        settings=settings,
        db=db,
        pool_repo=pool_repo,
        lease_repo=lease_repo,
        idempotency_repo=idempotency_repo,
        audit_repo=audit_repo,
        outbox_repo=outbox_repo,
        pool_view=pool_view,
        pricing=pricing,
    )

    # edges 9, 10: Placement Adapter <-> Placement Scheduler
    placement = PlacementAdapter(settings=settings, scheduler=externals.placement)

    # edges 11, 12: Provisioning Adapter <-> Hypervisor
    provisioning = ProvisioningAdapter(
        settings=settings,
        hypervisor=externals.hypervisor,
        reference_repo=reference_repo,
        audit_repo=audit_repo,
    )

    # edges 16, 17: Notice Delivery -> instance + tenant
    notice = NoticeDeliveryService(
        settings=settings,
        provisioning=provisioning,
        outbox_repo=outbox_repo,
        audit_repo=audit_repo,
        db=db,
        http_client=http,
    )

    # edges 13, 14: Teardown Confirmer -> Capacity Ledger
    teardown = TeardownConfirmer(
        settings=settings,
        provisioning=provisioning,
        ledger=externals.ledger,
        ledger_repo=ledger_repo,
        audit_repo=audit_repo,
        outbox_repo=outbox_repo,
    )

    # edges 25, 26: Metering & Rating -> Billing
    metering = MeteringService(
        settings=settings,
        billing_repo=billing_repo,
        audit_repo=audit_repo,
        outbox_repo=outbox_repo,
    )

    # the hub — edges 8, 9, 11, 15, 16, 22, 23, 25, 28, 32
    lease_manager = SpotLeaseManager(
        settings=settings,
        db=db,
        lease_repo=lease_repo,
        reference_repo=reference_repo,
        audit_repo=audit_repo,
        outbox_repo=outbox_repo,
        admission=admission,
        placement=placement,
        provisioning=provisioning,
        notice=notice,
        teardown=teardown,
        metering=metering,
    )

    # edge 13: the guest reporting a clean exit. In production the host agent
    # calls POST /internal/spot/leases/{id}/exited; the simulator calls straight
    # into the same handler.
    register = getattr(externals.hypervisor, "set_clean_exit_callback", None)
    if register is not None:
        register(lease_manager.report_clean_exit)

    # edges 21, 22: Victim Selector -> Lease Manager
    victim_selector = VictimSelector(
        settings=settings, lease_repo=lease_repo, audit_repo=audit_repo
    )

    # edges 18, 20, 21: Reclaim Order Handler
    reclaim_handler = ReclaimOrderHandler(
        settings=settings,
        db=db,
        reclaim_repo=reclaim_repo,
        lease_repo=lease_repo,
        audit_repo=audit_repo,
        outbox_repo=outbox_repo,
        pool_view=pool_view,
        selector=victim_selector,
        lease_manager=lease_manager,
    )

    # edges 29, 30: Audit -> Interruption Analytics -> Spot Market API
    analytics = InterruptionAnalytics(
        settings=settings,
        lease_repo=lease_repo,
        analytics_repo=analytics_repo,
        db=db,
    )

    # edges 3, 4, 6, 7, 32: the customer contract
    market = SpotMarketAPI(
        settings=settings,
        guard=guard,
        pool_view=pool_view,
        admission=admission,
        lease_manager=lease_manager,
        reference_repo=reference_repo,
        analytics=analytics,
        pricing=pricing,
    )

    # -- workers -------------------------------------------------------
    workers: list[Any] = [
        OutboxRelay(
            settings=settings,
            db=db,
            outbox_repo=outbox_repo,
            ledger=externals.ledger,
            bus_sink=bus,
        ),
        GraceReaper(
            settings=settings, lease_repo=lease_repo, lease_manager=lease_manager
        ),
        PoolRefresher(
            settings=settings,
            leader_repo=leader_repo,
            pool_view=pool_view,
            pool_repo=pool_repo,
        ),
        FulfilmentSweeper(settings=settings, db=db, lease_manager=lease_manager),
        TeardownSweeper(
            settings=settings,
            db=db,
            lease_repo=lease_repo,
            lease_manager=lease_manager,
            reclaim_handler=reclaim_handler,
        ),
        RetentionSweeper(
            settings=settings,
            idempotency_repo=idempotency_repo,
            nonce_repo=nonce_repo,
            rate_limit_repo=rate_limit_repo,
            outbox_repo=outbox_repo,
        ),
        AnalyticsWorker(
            settings=settings,
            leader_repo=leader_repo,
            analytics=analytics,
            lease_repo=lease_repo,
            billing_repo=billing_repo,
            billing_system=externals.billing,
        ),
    ]

    M.reset_pool_gauges(settings.availability_zones)

    return Container(
        settings=settings,
        db=db,
        externals=externals,
        bus=bus,
        pool_repo=pool_repo,
        lease_repo=lease_repo,
        reference_repo=reference_repo,
        idempotency_repo=idempotency_repo,
        outbox_repo=outbox_repo,
        audit_repo=audit_repo,
        reclaim_repo=reclaim_repo,
        billing_repo=billing_repo,
        ledger_repo=ledger_repo,
        analytics_repo=analytics_repo,
        leader_repo=leader_repo,
        rate_limit_repo=rate_limit_repo,
        nonce_repo=nonce_repo,
        pricing=pricing,
        pool_view=pool_view,
        guard=guard,
        admission=admission,
        placement=placement,
        provisioning=provisioning,
        notice=notice,
        teardown=teardown,
        metering=metering,
        lease_manager=lease_manager,
        victim_selector=victim_selector,
        reclaim_handler=reclaim_handler,
        analytics=analytics,
        market=market,
        workers=workers,
        _http=http,
    )

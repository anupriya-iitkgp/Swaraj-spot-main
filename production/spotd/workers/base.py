"""Worker scaffolding.

Every background loop in this service has the same three obligations and they
are easy to get subtly wrong one at a time, so they are implemented once:

1.  **An exception must not kill the loop.** A worker that dies on a transient
    database error and never restarts is worse than one that never existed: the
    service looks healthy, and the failure only surfaces later as stranded
    leases or an unbounded outbox.

2.  **Cancellation must be honoured immediately.** `asyncio.CancelledError` is
    not an error to be logged and swallowed; on shutdown the loop must stop.

3.  **The loop must be observable.** Every iteration increments
    `spot_worker_iterations_total` with an outcome, so "the reaper stopped
    running" is a query rather than an inference from missing side effects.

`LeaderElectedWorker` adds an expiring database lease for the loops that must
not run on every replica. Note which workers do *not* use it: the grace reaper
and the outbox relay claim individual rows with `SKIP LOCKED` instead, so they
keep working during a leadership handover. A lease sitting past its force-stop
deadline must not wait for an election.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from ..config import Settings
from ..logging import get_logger
from ..metrics import M

log = get_logger(__name__)

__all__ = ["PeriodicWorker", "LeaderElectedWorker"]


class PeriodicWorker:
    """Runs `tick()` every `interval` seconds until stopped."""

    name: str = "worker"

    def __init__(self, *, interval: float, settings: Settings) -> None:
        self._interval = interval
        self._settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._consecutive_failures = 0

    async def tick(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def on_start(self) -> None:
        return None

    async def on_stop(self) -> None:
        return None

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name=f"spotd.{self.name}")

    async def stop(self, *, timeout: float = 10.0) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        finally:
            self._task = None
        await self.on_stop()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(self) -> None:
        """Run a single iteration. Used by tests and by one-shot CLI commands."""
        await self.tick()

    # ------------------------------------------------------------------
    async def _run(self) -> None:
        await self.on_start()
        # Stagger the first tick. N replicas starting together in a rolling
        # deploy would otherwise hit the same rows in the same millisecond
        # forever after.
        await self._sleep(random.uniform(0, min(self._interval, 2.0)))

        while not self._stopping.is_set():
            try:
                await self.tick()
                M.worker_iterations_total.labels(worker=self.name, outcome="ok").inc()
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                self._consecutive_failures += 1
                M.worker_iterations_total.labels(worker=self.name, outcome="error").inc()
                log.exception(
                    "worker.tick_failed",
                    worker=self.name,
                    consecutive_failures=self._consecutive_failures,
                    error=str(exc),
                )
                # Back off so a persistent failure does not become a hot loop
                # against whatever is already broken.
                await self._sleep(
                    min(30.0, self._interval * (2 ** min(self._consecutive_failures, 5)))
                )
                continue

            await self._sleep(self._interval)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return


class LeaderElectedWorker(PeriodicWorker):
    """A periodic worker that only ticks while holding a named lock."""

    lock_name: str = "worker"

    def __init__(self, *, interval: float, settings: Settings, leader_repo: Any) -> None:
        super().__init__(interval=interval, settings=settings)
        self._leader = leader_repo
        self._is_leader = False

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def tick(self) -> None:
        state = await self._leader.acquire(
            self.lock_name, self._settings.worker_id, self._settings.leader_lease_ttl
        )
        if not state:
            if self._is_leader:
                log.info("worker.leadership_lost", worker=self.name)
            self._is_leader = False
            M.worker_iterations_total.labels(worker=self.name, outcome="standby").inc()
            return

        if not self._is_leader:
            log.info(
                "worker.leadership_acquired",
                worker=self.name,
                holder=state.holder,
                fence=state.fence,
            )
        self._is_leader = True
        await self.lead()

    async def lead(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def on_stop(self) -> None:
        if self._is_leader:
            # Hand over promptly rather than making the next replica wait out
            # the full lease TTL.
            await self._leader.release(self.lock_name, self._settings.worker_id)
            self._is_leader = False

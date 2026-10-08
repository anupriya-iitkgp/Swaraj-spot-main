"""Grace reaper — the authoritative clock.

This worker is the fix for LLD §12.3, the gap the LLD itself calls "the most
dangerous":

    Gap: Grace timers are in-memory asyncio.Tasks with no persistence.
    Consequence: A restart during a grace window strands the lease in
                 NOTICE_ISSUED forever: never stopped, never billed, capacity
                 never returned.
    Fix: Persist notice_at; on startup and every 5 s, reap leases where
         now > notice_at + force_stop_at and force-stop them.

Implemented slightly differently from the prescription, in a way that removes
the failure mode rather than recovering from it. Instead of `notice_at` plus a
config value read at reap time, the *absolute* `force_stop_deadline` is written
when the notice is issued. That matters because the config can change between
the notice and the reap — a deployment that lowers `SPOT_FORCE_STOP_AT` would
otherwise retroactively shorten grace windows that customers were already
promised, and one that raises it would extend deadlines the capacity side is
waiting on.

There is no separate startup sweep either. A lease whose deadline has passed
looks identical whether it passed while the process was down or a moment ago,
so the ordinary tick handles both and there is no recovery path that only runs
once and could therefore be wrong.

Multi-replica safety, per LLD §16 — "the grace-timer reaper is the one component
that must not run N times concurrently on the same lease" — comes from
`FOR UPDATE SKIP LOCKED` plus a claim stamp, not from leader election. The
reaper is the last line of defence for a customer-facing promise; it must keep
working while an election is in flight.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..config import Settings
from ..logging import get_logger, lease_context
from ..metrics import M
from .base import PeriodicWorker

log = get_logger(__name__)

__all__ = ["GraceReaper"]


class GraceReaper(PeriodicWorker):
    name = "grace_reaper"

    def __init__(
        self, *, settings: Settings, lease_repo: Any, lease_manager: Any
    ) -> None:
        super().__init__(interval=settings.reaper_interval, settings=settings)
        self._leases = lease_repo
        self._manager = lease_manager

    async def on_start(self) -> None:
        outstanding = await self._leases.outstanding_notices()
        if outstanding:
            # A restart that inherits in-flight preemptions should say so.
            # Nothing needs re-arming — the deadlines are in the table — but an
            # operator reading the log after a deploy deserves to know.
            log.warning(
                "reaper.inherited_in_flight_preemptions",
                count=len(outstanding),
                lease_ids=[lease.lease_id for lease in outstanding[:20]],
                note="deadlines are persisted; these are picked up on the next "
                "tick with no re-arming (LLD §12.3)",
            )

    async def tick(self) -> None:
        claimed = await self._leases.claim_expired_notices(
            worker=self._settings.worker_id,
            batch=self._settings.reaper_batch,
            claim_ttl=self._settings.reaper_claim_ttl,
        )
        if not claimed:
            return

        # Force-stops are independent per lease and each involves a hypervisor
        # round trip, so a serial loop would make the batch as slow as its sum.
        # Bounded concurrency keeps the tail short without stampeding the
        # hypervisor with a hundred simultaneous stops.
        semaphore = asyncio.Semaphore(10)

        async def reap(lease: Any) -> None:
            async with semaphore:
                with lease_context(lease.lease_id, lease.tenant_id):
                    try:
                        await self._manager.force_stop(lease)
                    except Exception as exc:  # noqa: BLE001
                        # The claim stamp expires after reaper_claim_ttl, so a
                        # lease that fails here is retried on a later tick
                        # rather than abandoned.
                        log.exception(
                            "reaper.force_stop_failed",
                            lease_id=lease.lease_id,
                            error=str(exc),
                            note="claim expires and the lease is retried",
                        )

        await asyncio.gather(*(reap(lease) for lease in claimed))
        log.info("reaper.batch_complete", claimed=len(claimed))

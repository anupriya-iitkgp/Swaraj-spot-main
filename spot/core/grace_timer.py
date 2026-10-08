"""Grace Timer & Escalation (edges 23, 24, 27).

The authoritative 120 s clock, per lease. A clean guest acknowledgement ends it
early; expiry forces the stop. It NEVER waits on the guest.

The budget the timer enforces (production values):
    0-5 s    notice delivered
    5-95 s   guest drain window
    95-100 s force stop if no clean exit     <- force_stop_at
    100-118  teardown: volumes, IPs, ports   <- teardown_budget
    118-120  ledger commit, capacity FREE
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import CONFIG
from ..domain.models import Lease
from .audit_log import PreemptionAuditLog

log = logging.getLogger("spot.timer")


class GraceTimer:
    def __init__(self, audit: PreemptionAuditLog):
        self._audit = audit
        self._tasks: dict[str, asyncio.Task] = {}
        self.lease_manager = None       # wired by the container
        self.provisioning_adapter = None
        #: lease_id -> notice timestamp, for the SLO report
        self.started_at: dict[str, float] = {}

    async def start(self, lease: Lease) -> None:
        """Edge 23 — Spot Lease Manager starts the clock."""
        if lease.lease_id in self._tasks:
            return
        self.started_at[lease.lease_id] = time.time()
        self._tasks[lease.lease_id] = asyncio.create_task(self._run(lease))

    async def _run(self, lease: Lease) -> None:
        try:
            clean = await self.provisioning_adapter.wait_clean_exit(
                lease, timeout=CONFIG.force_stop_at
            )
            if clean:
                await self.lease_manager.mark_draining(lease)
                await self.lease_manager.mark_stopped(lease, forced=False)
                return

            # Timer expiry: the grace period is a courtesy, the timer is law.
            self._audit.timer_expired(lease.lease_id)          # edge 27
            log.warning("edge 24  grace expired for %s -> force stop", lease.lease_id)
            await self.provisioning_adapter.force_stop_all(lease)
            await self.lease_manager.mark_stopped(lease, forced=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("grace timer failed for %s", lease.lease_id)
        finally:
            self._tasks.pop(lease.lease_id, None)

    def elapsed(self, lease_id: str) -> float | None:
        t0 = self.started_at.get(lease_id)
        return None if t0 is None else time.time() - t0

    async def cancel_all(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()

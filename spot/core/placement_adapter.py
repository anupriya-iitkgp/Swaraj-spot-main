"""Placement Adapter (edges 9, 10).

Submits placement with class = SPOT and the bin-pack hint. It does not choose
hosts itself — that is the external scheduler's job.
"""
from __future__ import annotations

import logging

from ..domain.models import Lease
from ..external.placement_scheduler import PlacementResult, PlacementScheduler

log = logging.getLogger("spot.placement")


class PlacementAdapter:
    def __init__(self, scheduler: PlacementScheduler):
        self._scheduler = scheduler

    async def place(self, lease: Lease) -> PlacementResult:
        log.info("edge 9   place(class=SPOT, bin_pack=True) for %s", lease.lease_id)
        return await self._scheduler.place(
            az=lease.az, units=lease.units, purchase_class="SPOT", bin_pack=True
        )

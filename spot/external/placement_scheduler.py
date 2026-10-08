"""EXTERNAL — Placement Scheduler (dashed box, edge 10).

Honours the class and the bin-pack hint. Spot cannot pick hosts itself.

The bin-pack hint is a correctness requirement, not an optimisation: freeing 64
vCPUs scattered across 20 hosts satisfies no large flavour. Spot is therefore
concentrated onto the fewest host groups that can take it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..domain.errors import NoCapacity
from .capacity_ledger import CapacityLedger

log = logging.getLogger("spot.ext.scheduler")


@dataclass(frozen=True)
class PlacementResult:
    host_group: str
    az: str


class PlacementScheduler:
    def __init__(self, ledger: CapacityLedger):
        self._ledger = ledger

    async def place(
        self, *, az: str, units: int, purchase_class: str, bin_pack: bool = True
    ) -> PlacementResult:
        """Edge 10: Placement Adapter <-> Placement Scheduler."""
        candidates = [h for h in self._ledger.host_groups(az) if h.free_units >= units]
        if not candidates:
            raise NoCapacity(f"no host group in {az} can take {units} units")

        if purchase_class == "SPOT" and bin_pack:
            # Fewest hosts: pick the *tightest* fit that already carries spot,
            # so reclaim later drains whole hosts and frees contiguous capacity.
            candidates.sort(key=lambda h: (-h.spot_allocated, h.free_units))
        else:
            # Guaranteed classes spread for anti-affinity.
            candidates.sort(key=lambda h: -h.free_units)

        chosen = candidates[0]
        log.info(
            "edge 10  scheduler: %d units -> %s (bin_pack=%s)",
            units, chosen.host_group, bin_pack,
        )
        return PlacementResult(host_group=chosen.host_group, az=chosen.az)

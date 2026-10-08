"""EXTERNAL — Capacity Ledger (dashed box, edge 14).

The record of capacity truth. Spot *reports*; the ledger decides. Capacity is
only FREE once teardown is confirmed, never when the guest shuts down.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("spot.ext.ledger")


@dataclass
class HostGroupCapacity:
    host_group: str
    az: str
    total_units: int
    reserved_static: int = 0
    allocated_dynamic: int = 0
    spot_allocated: int = 0
    spot_reclaiming: int = 0
    ops_buffer: int = 0

    @property
    def free_units(self) -> int:
        return max(
            0,
            self.total_units
            - self.reserved_static
            - self.allocated_dynamic
            - self.spot_allocated
            - self.spot_reclaiming
            - self.ops_buffer,
        )


class CapacityLedger:
    def __init__(self, host_groups: list[HostGroupCapacity] | None = None):
        self._hg: dict[str, HostGroupCapacity] = {
            h.host_group: h
            for h in (
                host_groups
                or [
                    HostGroupCapacity("hg-1", "az-1", total_units=96, reserved_static=24,
                                      allocated_dynamic=16, ops_buffer=8),
                    HostGroupCapacity("hg-2", "az-1", total_units=96, reserved_static=16,
                                      allocated_dynamic=24, ops_buffer=8),
                    HostGroupCapacity("hg-3", "az-2", total_units=64, reserved_static=8,
                                      allocated_dynamic=8, ops_buffer=4),
                ]
            )
        }
        self._lock = asyncio.Lock()
        self.audit: list[dict] = []

    def host_groups(self, az: str | None = None) -> list[HostGroupCapacity]:
        return [h for h in self._hg.values() if az is None or h.az == az]

    def get(self, host_group: str) -> HostGroupCapacity:
        return self._hg[host_group]

    def free_units(self, az: str) -> int:
        return sum(h.free_units for h in self.host_groups(az))

    async def record_spot_allocated(self, host_group: str, units: int) -> None:
        async with self._lock:
            self._hg[host_group].spot_allocated += units
            self._entry("spot_allocated", host_group, units)

    async def mark_reclaiming(self, host_group: str, units: int) -> None:
        """Capacity leaves the free pool the moment reclaim starts, not at the end."""
        async with self._lock:
            hg = self._hg[host_group]
            moved = min(units, hg.spot_allocated)
            hg.spot_allocated -= moved
            hg.spot_reclaiming += moved
            self._entry("mark_reclaiming", host_group, moved)

    async def commit_capacity_returned(self, host_group: str, units: int) -> None:
        """Edge 14 — only now is the capacity genuinely free.

        Preempted units arrive via RECLAIMING; a customer release returns
        them straight from ALLOCATED. Drain both pots, in that order —
        otherwise every voluntary release leaks allocated units forever."""
        async with self._lock:
            hg = self._hg[host_group]
            from_reclaiming = min(units, hg.spot_reclaiming)
            hg.spot_reclaiming -= from_reclaiming
            from_allocated = min(units - from_reclaiming, hg.spot_allocated)
            hg.spot_allocated -= from_allocated
            released = from_reclaiming + from_allocated
            self._entry("capacity_returned", host_group, released)
            log.info("edge 14  ledger: %s +%d units FREE", host_group, released)

    async def release_spot(self, host_group: str, units: int) -> None:
        """Voluntary release / failed provisioning — straight back to free."""
        async with self._lock:
            hg = self._hg[host_group]
            released = min(units, hg.spot_allocated)
            hg.spot_allocated -= released
            self._entry("spot_released", host_group, released)

    def _entry(self, kind: str, host_group: str, units: int) -> None:
        self.audit.append(
            {"ts": time.time(), "kind": kind, "host_group": host_group, "units": units}
        )

    def snapshot(self) -> list[dict]:
        return [
            {
                "host_group": h.host_group,
                "az": h.az,
                "total": h.total_units,
                "reserved_static": h.reserved_static,
                "allocated_dynamic": h.allocated_dynamic,
                "spot_allocated": h.spot_allocated,
                "spot_reclaiming": h.spot_reclaiming,
                "ops_buffer": h.ops_buffer,
                "free": h.free_units,
            }
            for h in self._hg.values()
        ]

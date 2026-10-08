"""Saved tasks — the stateful-spot registry.

When a preempted lease carries ``persist``, its machines are hibernated to
storage instead of destroyed; each entry here is a resumable task. The
registry itself is rebuildable: in proxmox mode every hibernated machine
carries its metadata in the VM description, so a restart of this app loses
nothing — ``rebuild()`` re-reads it from the hypervisor.
"""
from __future__ import annotations

import time

from ..domain.models import new_id


class SavedTaskStore:
    def __init__(self):
        self._tasks: dict[str, dict] = {}

    def add(self, lease, vmids: list[int]) -> dict:
        t = {
            "saved_id": new_id("saved"),
            "tenant_id": lease.tenant_id,
            "flavour": lease.flavour,
            "count": len(vmids),
            "az": lease.az,
            "vmids": list(vmids),
            "from_lease": lease.lease_id,
            "saved_at": time.time(),
        }
        self._tasks[t["saved_id"]] = t
        return t

    def restore_entry(self, meta: dict, vmid: int) -> None:
        """Startup rebuild: one hibernated machine found on the hypervisor."""
        for t in self._tasks.values():        # merge machines of the same lease
            if t["from_lease"] == meta.get("from_lease"):
                if vmid not in t["vmids"]:
                    t["vmids"].append(vmid)
                    t["count"] = len(t["vmids"])
                return
        t = {
            "saved_id": new_id("saved"),
            "tenant_id": meta.get("tenant_id", "unknown"),
            "flavour": meta.get("flavour", "s1.small"),
            "count": 1,
            "az": meta.get("az", "az-1"),
            "vmids": [vmid],
            "from_lease": meta.get("from_lease", "unknown"),
            "saved_at": meta.get("saved_at", time.time()),
        }
        self._tasks[t["saved_id"]] = t

    def list(self, tenant_id: str | None = None) -> list[dict]:
        return [dict(t) for t in self._tasks.values()
                if tenant_id is None or t["tenant_id"] == tenant_id]

    def get(self, saved_id: str) -> dict | None:
        return self._tasks.get(saved_id)

    def pop(self, saved_id: str) -> dict | None:
        return self._tasks.pop(saved_id, None)

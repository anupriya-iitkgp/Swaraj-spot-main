"""EXTERNAL — Provisioning / Hypervisor (dashed box, edge 12).

create / graceful stop / force stop / destroy, plus cleanup confirmation.

The simulated guest models the two behaviours that matter: one that drains
cleanly inside the grace window, and one that ignores the notice entirely and
must be killed by the timer.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from ..config import CONFIG
from ..domain.models import new_id

log = logging.getLogger("spot.ext.hypervisor")


@dataclass
class Instance:
    instance_id: str
    lease_id: str
    host_group: str
    flavour: str
    state: str = "CREATING"  # CREATING | RUNNING | DRAINING | STOPPED | DESTROYED
    #: Simulated guest behaviour: seconds it needs to drain, or None = ignores
    #: the notice and will have to be force-stopped.
    drain_seconds: Optional[float] = 1.0
    notice_received_at: Optional[float] = None
    stopped_at: Optional[float] = None
    volumes_detached: bool = False
    ip_released: bool = False


class Hypervisor:
    """In-memory fake. Replace with the libvirt/Nova/K8s client."""

    def __init__(self):
        self.instances: dict[str, Instance] = {}
        self.unreachable_hosts: set[str] = set()

    async def create(
        self, *, lease_id: str, host_group: str, flavour: str, count: int,
        drain_seconds: Optional[float] = 1.0, persist: bool = False,
        meta: Optional[dict] = None,
    ) -> list[str]:
        await asyncio.sleep(CONFIG.hypervisor_create_seconds)
        ids = []
        for _ in range(count):
            inst = Instance(
                instance_id=new_id("i"),
                lease_id=lease_id,
                host_group=host_group,
                flavour=flavour,
                state="RUNNING",
                drain_seconds=drain_seconds,
            )
            self.instances[inst.instance_id] = inst
            ids.append(inst.instance_id)
        log.info("edge 12  hypervisor: created %s on %s", ids, host_group)
        return ids

    async def deliver_notice(self, instance_id: str) -> None:
        inst = self.instances[instance_id]
        inst.notice_received_at = time.time()
        if inst.drain_seconds is not None:
            inst.state = "DRAINING"

    async def wait_for_clean_exit(self, instance_id: str, timeout: float) -> bool:
        """Returns True if the guest exited cleanly inside the window."""
        inst = self.instances[instance_id]
        if inst.drain_seconds is None:
            await asyncio.sleep(timeout)
            return False
        try:
            await asyncio.wait_for(asyncio.sleep(inst.drain_seconds), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        inst.state = "STOPPED"
        inst.stopped_at = time.time()
        return True

    async def force_stop(self, instance_id: str) -> None:
        inst = self.instances[instance_id]
        if inst.host_group in self.unreachable_hosts:
            # Escalation path: host agent unreachable -> hypervisor-level destroy.
            log.warning("host %s unreachable, escalating to destroy", inst.host_group)
        await asyncio.sleep(CONFIG.hypervisor_stop_seconds)
        inst.state = "STOPPED"
        inst.stopped_at = time.time()

    async def teardown(self, instance_id: str) -> None:
        """Detach volumes, release floating IP and ports, scrub the host."""
        await asyncio.sleep(CONFIG.teardown_seconds)
        inst = self.instances[instance_id]
        inst.volumes_detached = True
        inst.ip_released = True
        inst.state = "DESTROYED"

    async def destroy(self, instance_id: str) -> None:
        inst = self.instances.get(instance_id)
        if inst:
            inst.state = "DESTROYED"

    def for_lease(self, lease_id: str) -> list[Instance]:
        return [i for i in self.instances.values() if i.lease_id == lease_id]

    # ---------------- stateful spot (sim parity with the live adapter) -----
    async def hibernate(self, instance_id: str, meta: dict) -> Optional[int]:
        """Save machine state instead of destroying; returns a sim vmid."""
        inst = self.instances[instance_id]
        inst.volumes_detached = True
        inst.ip_released = True
        inst.state = "DESTROYED"        # capacity-wise it is gone from the host
        return abs(hash(instance_id)) % 90000 + 10000

    async def resume_saved(self, *, lease_id: str, host_group: str, flavour: str,
                           vmids: list[int], drain_seconds=1.0) -> list[str]:
        await asyncio.sleep(CONFIG.hypervisor_create_seconds)
        ids = []
        for v in vmids:
            inst = Instance(instance_id=f"i-resumed-{v}", lease_id=lease_id,
                            host_group=host_group, flavour=flavour,
                            state="RUNNING", drain_seconds=drain_seconds)
            self.instances[inst.instance_id] = inst
            ids.append(inst.instance_id)
        log.info("edge 12  hypervisor: resumed %s on %s", ids, host_group)
        return ids

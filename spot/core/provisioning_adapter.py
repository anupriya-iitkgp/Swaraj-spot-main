"""Provisioning Adapter (edges 11, 12, 13, 24).

Idempotent create / stop / destroy through the hypervisor, with compensating
actions so a half-completed launch never leaves capacity double-booked.
"""
from __future__ import annotations

import asyncio
import logging

from ..domain.models import Lease
from ..external.hypervisor import Hypervisor

log = logging.getLogger("spot.provisioning")


class ProvisioningAdapter:
    def __init__(self, hypervisor: Hypervisor):
        self._hv = hypervisor
        #: wired by the container; receives (lease, vmids) when a preempted
        #: persist-lease is hibernated instead of destroyed
        self.saved_store = None

    async def create(self, lease: Lease, *, drain_seconds: float | None = 1.0) -> list[str]:
        """Edges 11/12 — create instances (or resume saved machine state)."""
        if lease.resume_vmids and hasattr(self._hv, "resume_saved"):
            log.info("edge 11  RESUME %d saved machine(s) for %s",
                     len(lease.resume_vmids), lease.lease_id)
            return await self._hv.resume_saved(
                lease_id=lease.lease_id, host_group=lease.host_group,
                flavour=lease.flavour, vmids=lease.resume_vmids,
                drain_seconds=drain_seconds,
            )
        log.info("edge 11  provision %s x%d for %s", lease.flavour, lease.count, lease.lease_id)
        return await self._hv.create(
            lease_id=lease.lease_id,
            host_group=lease.host_group,
            flavour=lease.flavour,
            count=lease.count,
            drain_seconds=drain_seconds,
            persist=lease.persist,
            meta={"tenant_id": lease.tenant_id, "flavour": lease.flavour,
                  "az": lease.az, "from_lease": lease.lease_id},
        )

    async def notify(self, lease: Lease) -> None:
        await asyncio.gather(*(self._hv.deliver_notice(i) for i in lease.instance_ids))

    async def wait_clean_exit(self, lease: Lease, timeout: float) -> bool:
        """True only if every instance exited cleanly inside the window."""
        results = await asyncio.gather(
            *(self._hv.wait_for_clean_exit(i, timeout) for i in lease.instance_ids)
        )
        return all(results)

    async def force_stop_all(self, lease: Lease) -> None:
        """Edge 24 — Grace Timer escalation. The timer is authoritative."""
        log.warning("edge 24  force stop %s", lease.lease_id)
        await asyncio.gather(*(self._hv.force_stop(i) for i in lease.instance_ids))

    async def stop_all(self, lease: Lease, *, graceful: bool = True) -> None:
        await asyncio.gather(*(self._hv.force_stop(i) for i in lease.instance_ids))

    async def teardown_all(self, lease: Lease) -> None:
        """Edge 13 — teardown; a preempted persist-lease is hibernated instead:
        machine state goes to storage and the task becomes resumable."""
        preempted = bool(lease.preemption_reason
                         and "customer release" not in lease.preemption_reason)
        if lease.persist and preempted and hasattr(self._hv, "hibernate"):
            # saved only when the PLATFORM took the capacity away; a customer
            # release means "I am done" — the machines are truly deleted
            vmids = await asyncio.gather(
                *(self._hv.hibernate(i, meta={
                    "tenant_id": lease.tenant_id, "flavour": lease.flavour,
                    "az": lease.az, "from_lease": lease.lease_id,
                }) for i in lease.instance_ids))
            vmids = [v for v in vmids if v is not None]
            if vmids and self.saved_store is not None:
                self.saved_store.add(lease, vmids)
            return
        await asyncio.gather(*(self._hv.teardown(i) for i in lease.instance_ids))

    async def destroy_all(self, instance_ids: list[str]) -> None:
        await asyncio.gather(*(self._hv.destroy(i) for i in instance_ids))

    def teardown_complete(self, lease: Lease) -> bool:
        instances = self._hv.for_lease(lease.lease_id)
        return bool(instances) and all(
            i.volumes_detached and i.ip_released and i.state == "DESTROYED" for i in instances
        )

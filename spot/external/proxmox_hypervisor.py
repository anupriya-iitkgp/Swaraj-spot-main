"""EXTERNAL — real hypervisor adapter: Proxmox VE QEMU (edge 12, live).

Same interface as the in-memory ``Hypervisor`` stub, but every instance is a
real virtual machine cloned from a template on the Proxmox node. Launch =
clone + boot; preemption notice = real guest shutdown after its drain time;
grace-timer escalation = real hard stop; teardown = real deletion.

Stateful spot: ``hibernate`` suspends the VM **to disk** — RAM, CPU state and
every running process are written to storage and the machine vanishes from
the host's compute load. ``resume_saved`` starts it again exactly where it
stopped. Each hibernated VM carries its lease metadata in the VM description,
so the saved-task registry can be rebuilt from the hypervisor after a restart
of this app: the storage is the source of truth, not our memory.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import httpx

from ..config import CONFIG
from ..domain.models import FLAVOURS
from .hypervisor import Instance

log = logging.getLogger("spot.ext.proxmox")

#: Guests this adapter creates carry the "spot" tag — set at birth, and the
#: ONLY thing sweep/inventory logic trusts. Names are for humans; a name
#: prefix must never be an ownership claim (a VM merely *called* spot-something
#: — like the app's own deployment VM — is not ours to touch).
SPOT_TAG = "spot"
NAME_PREFIX = "spot-"


def _is_spot_guest(vm: dict) -> bool:
    tags = str(vm.get("tags") or "").replace(",", ";").split(";")
    return SPOT_TAG in [t.strip() for t in tags]


def _base_url() -> str:
    return f"https://{CONFIG.proxmox_host}:8006/api2/json"


def _headers() -> dict:
    return {"Authorization":
            f"PVEAPIToken={CONFIG.proxmox_token_id}={CONFIG.proxmox_token_secret}"}


def fetch_node_inventory() -> dict:
    """Synchronous startup probe: the node's real capacity and current guests.

    Raises on any failure — the caller decides whether to fall back to sim.
    """
    with httpx.Client(base_url=_base_url(), headers=_headers(),
                      verify=False, timeout=10.0) as c:
        status = c.get(f"/nodes/{CONFIG.proxmox_node}/status")
        status.raise_for_status()
        st = status.json()["data"]
        qemu = c.get(f"/nodes/{CONFIG.proxmox_node}/qemu")
        qemu.raise_for_status()
        lxc = c.get(f"/nodes/{CONFIG.proxmox_node}/lxc")
        lxc.raise_for_status()
        # cores already committed to non-spot guests = the "premium clients"
        premium = 0
        for vm in qemu.json()["data"]:
            if vm.get("status") == "running" and not _is_spot_guest(vm):
                premium += int(vm.get("cpus") or 0)
        for ct in lxc.json()["data"]:
            if ct.get("status") == "running" and not _is_spot_guest(ct):
                premium += int(ct.get("cpus") or 0)
        return {
            "node": CONFIG.proxmox_node,
            "cores": int(st["cpuinfo"]["cpus"]),
            "mem_gb": round(st["memory"]["total"] / 2**30),
            "premium_cores": premium,
            "pve_version": st.get("pveversion", ""),
        }


class ProxmoxHypervisor:
    """Duck-typed drop-in for ``external.hypervisor.Hypervisor``."""

    def __init__(self):
        self.instances: dict[str, Instance] = {}
        self.unreachable_hosts: set[str] = set()
        self._vmids: dict[str, int] = {}          # instance_id -> vmid
        self._client: Optional[httpx.AsyncClient] = None
        self._vmid_lock = asyncio.Lock()
        self._create_sem = asyncio.Semaphore(3)   # be kind to the node

    # ------------------------------------------------------------- plumbing
    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=_base_url(), headers=_headers(),
                                             verify=False, timeout=30.0)
        return self._client

    async def _task_wait(self, upid: str, timeout: float = 120.0) -> None:
        node = CONFIG.proxmox_node
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = await self._c().get(f"/nodes/{node}/tasks/{upid}/status")
            r.raise_for_status()
            d = r.json()["data"]
            if d["status"] == "stopped":
                if d.get("exitstatus") != "OK":
                    raise RuntimeError(f"proxmox task failed: {d.get('exitstatus')}")
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"proxmox task {upid} did not finish in {timeout}s")

    async def _next_vmid(self) -> int:
        async with self._vmid_lock:
            r = await self._c().get("/cluster/nextid")
            r.raise_for_status()
            return int(r.json()["data"])

    async def _vm_status(self, vmid: int) -> str:
        r = await self._c().get(f"/nodes/{CONFIG.proxmox_node}/qemu/{vmid}/status/current")
        if r.status_code >= 500:
            return "gone"
        r.raise_for_status()
        return r.json()["data"]["status"]     # running | stopped | suspended

    def _register(self, iid: str, vmid: int, *, lease_id: str, host_group: str,
                  flavour: str, drain_seconds: Optional[float]) -> None:
        self._vmids[iid] = vmid
        self.instances[iid] = Instance(
            instance_id=iid, lease_id=lease_id, host_group=host_group,
            flavour=flavour, state="RUNNING", drain_seconds=drain_seconds,
        )

    # ------------------------------------------------------------ interface
    async def create(
        self, *, lease_id: str, host_group: str, flavour: str, count: int,
        drain_seconds: Optional[float] = 1.0, persist: bool = False,
        meta: Optional[dict] = None,
    ) -> list[str]:
        fl = FLAVOURS[flavour]
        node = CONFIG.proxmox_node
        tmpl = CONFIG.proxmox_vm_template
        ids: list[str] = []
        for _ in range(count):
            async with self._create_sem:
                vmid = await self._next_vmid()
                name = f"{NAME_PREFIX}{lease_id.split('-')[-1][:8]}-{vmid}"
                r = await self._c().post(
                    f"/nodes/{node}/qemu/{tmpl}/clone",
                    data={"newid": vmid, "name": name, "full": 0})  # tagged below
                if r.status_code >= 400:
                    raise RuntimeError(f"proxmox clone failed: {r.text[:200]}")
                await self._task_wait(r.json()["data"])
                # size the clone to its flavour (limits, not reservations)
                await self._c().put(f"/nodes/{node}/qemu/{vmid}/config",
                                    data={"cores": fl.vcpu,
                                          "memory": max(256, fl.ram_gb * 64),
                                          "tags": "spot",
                                          "description": json.dumps(
                                              {**(meta or {}),
                                               "saved_at": time.time()})})
                r = await self._c().post(f"/nodes/{node}/qemu/{vmid}/status/start")
                if r.status_code >= 400:
                    raise RuntimeError(f"proxmox start failed: {r.text[:200]}")
                await self._task_wait(r.json()["data"])
            iid = f"vm-{vmid}"
            self._register(iid, vmid, lease_id=lease_id, host_group=host_group,
                           flavour=flavour, drain_seconds=drain_seconds)
            self.instances[iid].persist = persist       # rides on the instance
            ids.append(iid)
        log.info("edge 12  proxmox: cloned+started VMs %s on %s", ids, node)
        return ids

    async def deliver_notice(self, instance_id: str) -> None:
        inst = self.instances[instance_id]
        inst.notice_received_at = time.time()
        if inst.drain_seconds is not None:
            inst.state = "DRAINING"
            asyncio.get_running_loop().create_task(
                self._guest_drain(instance_id, inst.drain_seconds))

    async def _guest_drain(self, instance_id: str, after: float) -> None:
        """The guest reacts to its notice. A persist guest hibernates — RAM
        and processes to disk; anything else does a real ACPI shutdown."""
        await asyncio.sleep(after)
        vmid = self._vmids.get(instance_id)
        inst = self.instances.get(instance_id)
        if vmid is None or inst is None:
            return
        try:
            if getattr(inst, "persist", False):
                r = await self._c().post(
                    f"/nodes/{CONFIG.proxmox_node}/qemu/{vmid}/status/suspend",
                    data={"todisk": 1})
                if r.status_code < 400:
                    await self._task_wait(r.json()["data"], timeout=120)
                    inst.hibernated = True
                    log.info("stateful spot: %s suspended to disk on notice",
                             instance_id)
                    return
            r = await self._c().post(
                f"/nodes/{CONFIG.proxmox_node}/qemu/{vmid}/status/shutdown",
                data={"timeout": 8, "forceStop": 1})
            if r.status_code < 400:
                await self._task_wait(r.json()["data"], timeout=30)
        except Exception as e:          # the grace timer remains authoritative
            log.warning("guest drain of %s failed (%s); timer will escalate",
                        instance_id, e)

    async def wait_for_clean_exit(self, instance_id: str, timeout: float) -> bool:
        vmid = self._vmids.get(instance_id)
        inst = self.instances[instance_id]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if await self._vm_status(vmid) in ("stopped", "gone"):
                    inst.state = "STOPPED"
                    inst.stopped_at = time.time()
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def force_stop(self, instance_id: str) -> None:
        vmid = self._vmids.get(instance_id)
        inst = self.instances[instance_id]
        if vmid is not None:
            try:
                if await self._vm_status(vmid) == "running":
                    r = await self._c().post(
                        f"/nodes/{CONFIG.proxmox_node}/qemu/{vmid}/status/stop")
                    r.raise_for_status()
                    await self._task_wait(r.json()["data"], timeout=30)
            except Exception as e:
                log.error("force stop of %s: %s", instance_id, e)
        inst.state = "STOPPED"
        inst.stopped_at = time.time()

    async def teardown(self, instance_id: str) -> None:
        """Really delete the VM (stop first if needed)."""
        vmid = self._vmids.get(instance_id)
        inst = self.instances[instance_id]
        if vmid is not None:
            try:
                if await self._vm_status(vmid) == "running":
                    r = await self._c().post(
                        f"/nodes/{CONFIG.proxmox_node}/qemu/{vmid}/status/stop")
                    if r.status_code < 400:
                        await self._task_wait(r.json()["data"], timeout=30)
                r = await self._c().delete(
                    f"/nodes/{CONFIG.proxmox_node}/qemu/{vmid}",
                    params={"purge": 1, "destroy-unreferenced-disks": 1})
                if r.status_code < 400:
                    await self._task_wait(r.json()["data"], timeout=60)
            except Exception as e:
                log.error("teardown of %s: %s", instance_id, e)
        inst.volumes_detached = True
        inst.ip_released = True
        inst.state = "DESTROYED"

    async def destroy(self, instance_id: str) -> None:
        if instance_id in self.instances:
            await self.teardown(instance_id)

    def for_lease(self, lease_id: str) -> list[Instance]:
        return [i for i in self.instances.values() if i.lease_id == lease_id]

    # ------------------------------------------------- stateful spot (live)
    async def hibernate(self, instance_id: str, meta: dict) -> Optional[int]:
        """Preserve the machine instead of destroying it. Three cases:
        already suspended on notice → just stamp metadata; still running
        (timer beat the guest) → suspend to disk now; force-stopped (guest
        ignored the notice) → RAM is gone, but the DISK is still saved and
        resume becomes a cold boot. Never deletes — worst case we keep a
        stopped VM rather than losing a customer's work."""
        vmid = self._vmids.get(instance_id)
        inst = self.instances[instance_id]
        if vmid is None:
            return None
        try:
            if not getattr(inst, "hibernated", False):
                if await self._vm_status(vmid) == "running":
                    r = await self._c().post(
                        f"/nodes/{CONFIG.proxmox_node}/qemu/{vmid}/status/suspend",
                        data={"todisk": 1})
                    r.raise_for_status()
                    await self._task_wait(r.json()["data"], timeout=120)
                else:
                    log.warning("%s was stopped before it could hibernate — "
                                "disk saved, RAM lost (resume = cold boot)",
                                instance_id)
            # metadata was stamped on the VM at birth — nothing to write here,
            # which matters: a suspended VM is locked against config changes
        except Exception as e:
            log.error("hibernate of %s hit an API error (%s) — keeping the VM "
                      "anyway; it will be re-registered by the next sweep", 
                      instance_id, e)
        inst.volumes_detached = True
        inst.ip_released = True
        inst.state = "DESTROYED"          # gone from the host's compute load
        log.info("stateful spot: %s preserved as vmid %d", instance_id, vmid)
        return vmid

    async def resume_saved(self, *, lease_id: str, host_group: str, flavour: str,
                           vmids: list[int], drain_seconds=1.0) -> list[str]:
        """Start hibernated VMs — they continue exactly where they stopped."""
        node = CONFIG.proxmox_node
        ids = []
        for vmid in vmids:
            r = await self._c().post(f"/nodes/{node}/qemu/{vmid}/status/start")
            if r.status_code >= 400:
                raise RuntimeError(f"resume of vmid {vmid} failed: {r.text[:200]}")
            await self._task_wait(r.json()["data"])
            try:      # VM is running & unlocked now — refresh the lease pointer
                cfg = await self._c().get(f"/nodes/{node}/qemu/{vmid}/config")
                m = json.loads(cfg.json()["data"].get("description") or "{}")
                m["from_lease"] = lease_id
                await self._c().put(f"/nodes/{node}/qemu/{vmid}/config",
                                    data={"description": json.dumps(m)})
            except Exception:
                pass  # cosmetic only; the registry entry is authoritative
            iid = f"vm-{vmid}"
            self._register(iid, vmid, lease_id=lease_id, host_group=host_group,
                           flavour=flavour, drain_seconds=drain_seconds)
            ids.append(iid)
        log.info("edge 12  proxmox: RESUMED saved VMs %s on %s", ids, node)
        return ids

    # ------------------------------------------------------------- hygiene
    async def startup_sweep(self, saved_store=None) -> int:
        """Previous-run spot VMs: hibernated ones are re-registered as saved
        tasks (their metadata lives on the VM); running orphans are removed.

        Ownership is decided by the "spot" TAG only — a guest without the tag
        is never touched, whatever it is called."""
        removed = 0
        try:
            r = await self._c().get(f"/nodes/{CONFIG.proxmox_node}/qemu")
            r.raise_for_status()
            for vm in r.json()["data"]:
                if not _is_spot_guest(vm):
                    continue
                if vm.get("template") or int(vm["vmid"]) == CONFIG.proxmox_vm_template:
                    continue
                vmid = int(vm["vmid"])
                if vm.get("status") != "running":
                    # a stopped spot VM is (or may be) a saved task — NEVER
                    # deleted. Re-register it from its birth metadata.
                    cfg = await self._c().get(
                        f"/nodes/{CONFIG.proxmox_node}/qemu/{vmid}/config")
                    desc = (cfg.json()["data"].get("description") or "") \
                        if cfg.status_code < 400 else ""
                    try:
                        if saved_store is not None:
                            saved_store.restore_entry(json.loads(desc), vmid)
                            log.info("startup: re-registered saved task vmid %d", vmid)
                    except Exception:
                        log.warning("startup: spot vm %d has no readable "
                                    "metadata — keeping it untouched", vmid)
                    continue
                try:      # a RUNNING spot VM with no lease is a true orphan
                    s = await self._c().post(
                        f"/nodes/{CONFIG.proxmox_node}/qemu/{vmid}/status/stop")
                    if s.status_code < 400:
                        await self._task_wait(s.json()["data"], timeout=30)
                    d = await self._c().delete(
                        f"/nodes/{CONFIG.proxmox_node}/qemu/{vmid}",
                        params={"purge": 1, "destroy-unreferenced-disks": 1})
                    if d.status_code < 400:
                        await self._task_wait(d.json()["data"], timeout=60)
                    removed += 1
                except Exception as e:
                    log.warning("orphan sweep: could not remove vm %s: %s", vmid, e)
        except Exception as e:
            log.warning("orphan sweep failed: %s", e)
        # legacy: spot-tagged LXC containers from the container-era adapter.
        # They are never saved tasks — stop and remove them whatever their state.
        try:
            r = await self._c().get(f"/nodes/{CONFIG.proxmox_node}/lxc")
            r.raise_for_status()
            for ct in r.json()["data"]:
                if not _is_spot_guest(ct):
                    continue
                vmid = int(ct["vmid"])
                try:
                    if ct.get("status") == "running":
                        st = await self._c().post(
                            f"/nodes/{CONFIG.proxmox_node}/lxc/{vmid}/status/stop")
                        if st.status_code < 400:
                            await self._task_wait(st.json()["data"], timeout=30)
                    d = await self._c().delete(
                        f"/nodes/{CONFIG.proxmox_node}/lxc/{vmid}", params={"purge": 1})
                    if d.status_code < 400:
                        await self._task_wait(d.json()["data"], timeout=60)
                    removed += 1
                    log.info("orphan sweep: removed legacy spot container %d", vmid)
                except Exception as e:
                    log.warning("orphan sweep: could not remove ct %s: %s", vmid, e)
        except Exception as e:
            log.warning("lxc orphan sweep failed: %s", e)
        if removed:
            log.info("orphan sweep removed %d stale spot guest(s)", removed)
        return removed

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

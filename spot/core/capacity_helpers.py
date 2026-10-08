"""Build ledger host groups from a real hypervisor's inventory.

In proxmox mode the "premium clients" are not invented: they are the cores
already committed to the node's running QEMU VMs and non-spot containers
(the VM this very app runs in counts as one). The ops buffer is the N+1
slice a real operator would hold back.
"""
from __future__ import annotations

from ..external.capacity_ledger import HostGroupCapacity


def proxmox_host_groups(inv: dict) -> list[HostGroupCapacity]:
    total = inv["cores"]
    premium = min(inv["premium_cores"], max(0, total - 2))
    buffer = max(2, total // 12)
    # split the committed cores the way the ledger models them: half static
    # reservations, half pay-per-use — the distinction is billing-side only
    reserved = premium // 2
    dynamic = premium - reserved
    return [HostGroupCapacity(inv["node"], "az-1", total_units=total,
                              reserved_static=reserved, allocated_dynamic=dynamic,
                              ops_buffer=buffer)]

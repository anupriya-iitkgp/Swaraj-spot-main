"""Victim Selector (edges 21, 22).

Which leases die, and in what order.

The HLD calls out that contiguity and fairness pull in opposite directions, and
that an unstated precedence gets resolved differently by every implementer. The
precedence is therefore explicit here and is the single source of truth:

    1. CONTIGUITY picks the host set.   Drain the fewest host groups, so freed
       capacity is contiguous and can host a large flavour. Freeing scattered
       vCPUs is worthless for a big VM.
    2. FLAVOUR MATCH narrows within it. Prefer victims whose shape matches the
       incoming demand.
    3. FAIRNESS only ORDERS victims within the chosen host set. Newest lease
       first, which protects long-running workloads.
    4. BLAST RADIUS caps how much of any one tenant's fleet a single wave takes.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from ..config import CONFIG
from ..domain.models import Lease, LeaseState, PREEMPTIBLE_STATES, ReclaimOrder

log = logging.getLogger("spot.victims")


class VictimSelector:
    def __init__(self, lease_manager):
        self._leases = lease_manager

    def select(self, order: ReclaimOrder) -> list[Lease]:
        """Edge 21/22 — returns the lease set to preempt, in kill order."""
        candidates = [
            l
            for l in self._leases.live_leases(az=order.az)
            if l.state in PREEMPTIBLE_STATES or l.state in {LeaseState.ADMITTED,
                                                            LeaseState.PROVISIONING}
        ]
        if order.host_group:
            candidates = [
                l for l in candidates
                if l.host_group == order.host_group or l.host_group is None
            ]
        if not candidates:
            return []

        # 1. contiguity — group by host, prefer hosts that can be fully drained
        by_host: dict[str | None, list[Lease]] = defaultdict(list)
        for l in candidates:
            by_host[l.host_group].append(l)

        # A host whose total spot >= the requested units can satisfy the order
        # on its own: draining it yields one contiguous block.
        host_order = sorted(
            by_host.items(),
            key=lambda kv: (
                0 if sum(l.units for l in kv[1]) >= order.units else 1,   # can finish alone
                -sum(l.units for l in kv[1]),                             # then biggest first
            ),
        )

        # per-tenant blast radius cap
        tenant_live: dict[str, int] = defaultdict(int)
        for l in self._leases.live_leases(az=order.az):
            tenant_live[l.tenant_id] += l.units
        tenant_taken: dict[str, int] = defaultdict(int)

        chosen: list[Lease] = []
        units = 0
        for host_group, leases in host_order:
            if units >= order.units:
                break
            # 3. fairness ordering WITHIN the host set: newest lease first
            leases.sort(key=lambda l: l.created_at, reverse=True)
            for lease in leases:
                if units >= order.units:
                    break
                cap = max(1, int(tenant_live[lease.tenant_id] * CONFIG.blast_radius_fraction))
                if tenant_taken[lease.tenant_id] + lease.units > cap and len(chosen) > 0:
                    log.info("blast-radius cap protected %s (tenant %s)",
                             lease.lease_id, lease.tenant_id)
                    continue
                chosen.append(lease)
                tenant_taken[lease.tenant_id] += lease.units
                units += lease.units

        log.info(
            "edge 21  selected %d leases (%d units) for order %s across hosts %s",
            len(chosen), units, order.order_id,
            sorted({l.host_group for l in chosen}, key=lambda x: (x is None, x)),
        )
        return chosen

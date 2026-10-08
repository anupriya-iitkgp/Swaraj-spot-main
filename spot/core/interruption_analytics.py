"""Interruption Analytics (edges 29, 30).

Rolling interruption rate per flavour and AZ, published back into the Spot
Market API inventory feed. Customers price their own risk from this number, so
it is published even when it is unflattering.

Also tracks the per-TENANT interruption rate: if reclaim keeps hitting the same
host groups, the same tenants absorb every interruption while others never do.
A widening spread means victim selection needs a fairness term.
"""
from __future__ import annotations

from collections import defaultdict

from ..domain.models import LeaseState
from .audit_log import PreemptionAuditLog


class InterruptionAnalytics:
    def __init__(self, audit: PreemptionAuditLog, lease_manager):
        self._audit = audit
        self._leases = lease_manager

    def rate_by_flavour_az(self) -> list[dict]:
        """Edge 30 — what the inventory feed publishes."""
        total: dict[tuple[str, str], int] = defaultdict(int)
        preempted: dict[tuple[str, str], int] = defaultdict(int)
        for lease in self._leases.all():
            if lease.state is LeaseState.REQUESTED:
                continue
            key = (lease.flavour, lease.az)
            total[key] += 1
            if lease.preemption_reason and "customer release" not in lease.preemption_reason:
                preempted[key] += 1
        return [
            {
                "flavour": f,
                "az": az,
                "leases": total[(f, az)],
                "interrupted": preempted[(f, az)],
                "interruption_rate": round(preempted[(f, az)] / total[(f, az)], 4),
            }
            for (f, az) in sorted(total)
        ]

    def rate_by_tenant(self) -> list[dict]:
        total: dict[str, int] = defaultdict(int)
        preempted: dict[str, int] = defaultdict(int)
        for lease in self._leases.all():
            total[lease.tenant_id] += 1
            if lease.preemption_reason and "customer release" not in lease.preemption_reason:
                preempted[lease.tenant_id] += 1
        rows = [
            {
                "tenant_id": t,
                "leases": total[t],
                "interrupted": preempted[t],
                "interruption_rate": round(preempted[t] / total[t], 4),
            }
            for t in sorted(total)
        ]
        rates = [r["interruption_rate"] for r in rows]
        spread = round(max(rates) - min(rates), 4) if rates else 0.0
        for r in rows:
            r["fleet_spread"] = spread
        return rows

    def preemption_history(self) -> list[dict]:
        return self._audit.preemption_history()

"""Victim Selector — edges 21 and 22.

HLD §6:

    Owns: Which leases die and in what order, under the contiguity and
          blast-radius policy.
    Must not do: Ignore the deadline in the order.

HLD §12 raises this as an unresolved risk and it is worth quoting in full,
because the whole file is an answer to it:

    "Contiguity and fairness pull in opposite directions. Host-drain-first frees
    usable capacity; newest-lease-first is what customers perceive as fair. On a
    given reclaim these two rules often select different victims, and an
    unstated precedence will be resolved differently by every implementer.

    Recommendation: Write the precedence into the policy explicitly: contiguity
    picks the host set, fairness only orders victims within it. Publish the rule
    so customers can reason about it."

So the precedence is stated once, here, and every other statement of it in the
codebase and the docs is derived from this one:

    1. CONTIGUITY   picks the host set — drain the fewest host groups, and
                    prefer fully draining one small host over partially
                    draining a large one.
    2. FLAVOUR      narrows within that set, when the order asks for a shape.
    3. FAIRNESS     orders victims inside the set: newest lease first.
    4. BLAST RADIUS caps how much of any one tenant's fleet a single wave takes.

Why contiguity outranks fairness: the capacity being reclaimed has to be
*usable* by the guaranteed classes, and a guaranteed workload needs whole hosts,
not a scattering of freed vCPUs across forty machines. Selecting the fairest
victims across the whole AZ would free the right number of units in a shape
nobody can use, and the reclaim would have to run again.

One escalation is possible and it is deliberate. If the blast-radius cap leaves
the order short, the remainder is taken anyway, with an explicit audit entry and
a metric. Fairness between spot tenants is a policy; returning capacity to the
guaranteed classes is an SLA. When they genuinely conflict, the SLA wins — but
it is recorded rather than quietly done, so the cap can be retuned.

LLD §12.6 is closed here too: a host-scoped order can only select leases that
are actually placed on that host. Unplaced leases are excluded by the repository
query, not by a check a caller might forget.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..config import Settings
from ..db.repositories import VictimCandidate
from ..domain.models import ReclaimOrder
from ..logging import edge, get_logger

log = get_logger(__name__)

__all__ = ["VictimSelector", "Selection"]


@dataclass(slots=True)
class Selection:
    victims: list[VictimCandidate] = field(default_factory=list)
    units_selected: int = 0
    units_requested: int = 0
    host_groups: list[str] = field(default_factory=list)
    blast_radius_exceeded: bool = False
    shortfall_reason: str | None = None

    @property
    def satisfied(self) -> bool:
        return self.units_selected >= self.units_requested

    @property
    def lease_ids(self) -> list[str]:
        return [v.lease_id for v in self.victims]


class VictimSelector:
    def __init__(self, *, settings: Settings, lease_repo: Any, audit_repo: Any) -> None:
        self._settings = settings
        self._leases = lease_repo
        self._audit = audit_repo

    async def select(self, order: ReclaimOrder, *, flavour: str | None = None) -> Selection:
        """Choose which leases die, in what order, for one reclaim order."""
        candidates = await self._leases.victim_candidates(
            order.az, host_group=order.host_group
        )
        selection = Selection(units_requested=order.units)

        if not candidates:
            selection.shortfall_reason = (
                f"no running spot leases in {order.az}"
                + (f" on {order.host_group}" if order.host_group else "")
            )
            return selection

        fleet = await self._leases.tenant_fleet_units(order.az)

        # -- 1. CONTIGUITY: pick the host set ----------------------------
        by_host: dict[str, list[VictimCandidate]] = defaultdict(list)
        for candidate in candidates:
            by_host[candidate.host_group or ""].append(candidate)

        host_order = self._order_hosts(by_host, order.units)
        selection.host_groups = list(host_order)

        # -- 2 & 3: flavour narrows, fairness orders ---------------------
        ordered: list[VictimCandidate] = []
        for host_group in host_order:
            group = by_host[host_group]
            if flavour:
                matching = [c for c in group if c.flavour == flavour]
                # Fall back to the whole group rather than under-delivering the
                # order: the flavour is a preference, the unit count is not.
                group = matching or group
            # Fairness: newest lease first. The candidates arrive newest-first
            # from the repository; sorting explicitly keeps the rule visible in
            # the code that claims to implement it.
            ordered.extend(sorted(group, key=lambda c: c.created_at, reverse=True))

        # -- 4. BLAST RADIUS ---------------------------------------------
        taken_per_tenant: dict[str, int] = defaultdict(int)
        deferred: list[VictimCandidate] = []

        for candidate in ordered:
            if selection.units_selected >= order.units:
                break
            cap = self._cap_for(fleet.get(candidate.tenant_id, candidate.units))
            already = taken_per_tenant[candidate.tenant_id]
            # The cap never blocks a tenant's *first* lease in a wave. A tenant
            # running a single 8-unit lease has a fleet of 8 and a cap of 4, so
            # a strict reading would make them permanently un-preemptible — the
            # blast radius would protect exactly the tenants it was not written
            # for, and every wave would escalate past it.
            if already > 0 and already + candidate.units > cap:
                deferred.append(candidate)
                continue
            selection.victims.append(candidate)
            selection.units_selected += candidate.units
            taken_per_tenant[candidate.tenant_id] += candidate.units

        # -- escalation, if the cap left the order short -----------------
        if selection.units_selected < order.units and deferred:
            selection.blast_radius_exceeded = True
            log.warning(
                "victim_selector.blast_radius_escalation",
                order_id=order.order_id,
                az=order.az,
                units_requested=order.units,
                units_within_cap=selection.units_selected,
                blast_radius=self._settings.blast_radius,
                note="the blast-radius cap cannot satisfy this order; taking the "
                "remainder because returning capacity to the guaranteed classes "
                "is an SLA and inter-tenant fairness is a policy",
            )
            await self._audit.append(
                "reclaim.blast_radius_exceeded",
                order_id=order.order_id,
                detail={
                    "az": order.az,
                    "units_requested": order.units,
                    "units_within_cap": selection.units_selected,
                    "blast_radius": self._settings.blast_radius,
                    "tenants_over_cap": sorted(
                        {c.tenant_id for c in deferred}
                    ),
                },
            )
            for candidate in deferred:
                if selection.units_selected >= order.units:
                    break
                selection.victims.append(candidate)
                selection.units_selected += candidate.units
                taken_per_tenant[candidate.tenant_id] += candidate.units

        if not selection.satisfied:
            selection.shortfall_reason = (
                f"only {selection.units_selected} of {order.units} units are "
                f"held by running spot leases"
                + (f" on {order.host_group}" if order.host_group else f" in {order.az}")
            )

        edge(
            log,
            21,
            f"selected {len(selection.victims)} victim(s) totalling "
            f"{selection.units_selected}u of {order.units}u requested across "
            f"{len({v.host_group for v in selection.victims})} host group(s)",
            order_id=order.order_id,
            az=order.az,
            units_requested=order.units,
            units_selected=selection.units_selected,
            victims=selection.lease_ids,
            host_groups=sorted({v.host_group or "" for v in selection.victims}),
            precedence="contiguity > flavour > fairness > blast_radius",
        )
        return selection

    # ------------------------------------------------------------------
    def _order_hosts(
        self, by_host: dict[str, list[VictimCandidate]], units_needed: int
    ) -> list[str]:
        """Contiguity: the host order that drains the fewest groups.

        A host group that can satisfy the whole order on its own goes first, and
        among those the *smallest* wins — that fully drains one modest host
        rather than leaving a large one half-empty, which is the shape the
        guaranteed classes can actually use. Otherwise, largest first, so the
        order is covered by as few groups as possible.
        """
        totals = {
            host: sum(c.units for c in candidates)
            for host, candidates in by_host.items()
        }
        sufficient = sorted(
            (h for h, u in totals.items() if u >= units_needed),
            key=lambda h: (totals[h], h),
        )
        insufficient = sorted(
            (h for h, u in totals.items() if u < units_needed),
            key=lambda h: (-totals[h], h),
        )
        return sufficient + insufficient

    def _cap_for(self, fleet_units: int) -> int:
        """Max units of one tenant's fleet a single wave may take beyond the first."""
        return max(1, int(fleet_units * self._settings.blast_radius))

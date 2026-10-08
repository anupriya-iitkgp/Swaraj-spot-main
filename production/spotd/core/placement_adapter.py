"""Placement Adapter — edges 9 and 10.

HLD §6:

    Owns: Submitting placement with class = SPOT and the bin-pack hint.
    Must not do: Choose hosts itself.
    Key operations: place(leaseId, hint)

The component is thin by design and the "must not" is why it exists at all. Host
selection belongs to the Placement Scheduler, which knows about NUMA topology,
anti-affinity, maintenance windows and everything else this subsystem has no
business modelling. What this subsystem *does* know, and must communicate, is
that a spot lease should be packed tightly rather than spread — because
contiguity is the first term in victim selection, and a scattered spot
population makes a host-scoped reclaim impossible to satisfy without killing far
more leases than the order asked for.

So the adapter's entire contribution is: always send `purchase_class=SPOT`,
always send `bin_pack=True`, never second-guess the answer.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..domain.errors import PlacementFailed
from ..domain.models import Lease
from ..external.base import ExternalError
from ..external.placement import PlacementScheduler, PlacementUnavailable
from ..logging import edge, get_logger

log = get_logger(__name__)

__all__ = ["PlacementAdapter"]


class PlacementAdapter:
    def __init__(self, *, settings: Settings, scheduler: PlacementScheduler) -> None:
        self._settings = settings
        self._scheduler = scheduler

    async def place(self, lease: Lease) -> str:
        """Ask the scheduler for a host group. Raises PlacementFailed (503).

        LLD §9's failure behaviour for this interface is "Lease -> REJECTED,
        reservation released", which the lease manager performs on this
        exception. Nothing is retried here beyond what `ExternalCaller` already
        does: a scheduler that cannot place is reporting a fact about capacity,
        and asking again immediately will not change it.
        """
        edge(
            log,
            9,
            f"requesting placement for {lease.units}u in {lease.az} (bin-pack)",
            lease_id=lease.lease_id,
            az=lease.az,
            units=lease.units,
        )
        try:
            host_group = await self._scheduler.place(
                az=lease.az,
                units=lease.units,
                purchase_class="SPOT",
                # Never conditional. See the module docstring.
                bin_pack=True,
                lease_id=lease.lease_id,
            )
        except PlacementUnavailable as exc:
            raise PlacementFailed(
                f"no host group in {lease.az} can accept {lease.units} units",
                details={"az": lease.az, "units": lease.units},
                retry_after=self._settings.retry_after,
            ) from exc
        except ExternalError as exc:
            raise PlacementFailed(
                f"placement scheduler unavailable: {exc}",
                details={"az": lease.az},
                retry_after=self._settings.retry_after,
            ) from exc

        if not host_group:
            raise PlacementFailed(
                "placement scheduler returned an empty host group",
                details={"az": lease.az},
            )
        return host_group

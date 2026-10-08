"""Placement Scheduler — edge 10, out of scope (HLD §1).

LLD §9:

    place(az, units, purchase_class, bin_pack) -> host_group
    Must honour bin_pack=True for SPOT.
    Behaviour if it breaks: Lease -> REJECTED, reservation released.

The bin-pack hint is not a performance preference, it is what makes the reclaim
path work at all. HLD §6 gives the Victim Selector its policy "under the
contiguity and blast-radius policy", and the README's precedence rule puts
contiguity first: drain the fewest host groups. Contiguity is only achievable if
spot instances were packed tightly in the first place. Spread spot across every
host in the AZ and a reclaim for one host group has to kill leases scattered
everywhere — or kill far more of them than the order asked for.

So this adapter's one job on the sim side is best-fit packing: choose the host
group with the *least* remaining free capacity that still fits the request.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from ..config import Settings
from ..logging import edge, get_logger
from .base import ExternalCaller, ExternalError

log = get_logger(__name__)

__all__ = ["PlacementScheduler", "SimulatedPlacementScheduler", "HttpPlacementScheduler",
           "PlacementUnavailable"]


class PlacementUnavailable(ExternalError):
    """No host group can take the request. Not retryable — capacity, not a fault."""

    def __init__(self, az: str, units: int) -> None:
        super().__init__(
            "placement",
            "place",
            f"no host group in {az} can accept {units} units",
            retryable=False,
        )


class PlacementScheduler(Protocol):
    async def place(
        self, *, az: str, units: int, purchase_class: str, bin_pack: bool,
        lease_id: str,
    ) -> str:
        """Return the host group the lease should land on."""
        ...


class SimulatedPlacementScheduler:
    """Best-fit bin packing over the seeded host groups.

    Free capacity is computed from live leases rather than tracked separately,
    so it cannot drift from the lease table — which is the same reason the pool
    reconciliation in `PoolRepository.reconcile` recomputes rather than trusts.
    """

    def __init__(self, db: Any, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    async def place(
        self, *, az: str, units: int, purchase_class: str, bin_pack: bool,
        lease_id: str,
    ) -> str:
        rows = await self._db.fetch(
            """
            SELECT h.host_group,
                   h.total_units,
                   h.total_units - COALESCE(l.used, 0) AS free
              FROM host_group h
              LEFT JOIN (
                    SELECT host_group, SUM(units)::int AS used
                      FROM spot_lease
                     WHERE state IN ('PROVISIONING','RUNNING','NOTICE_ISSUED','DRAINING')
                       AND host_group IS NOT NULL
                     GROUP BY host_group
              ) l ON l.host_group = h.host_group
             WHERE h.az = $1 AND NOT h.quarantined
               AND h.total_units - COALESCE(l.used, 0) >= $2
             ORDER BY (h.total_units - COALESCE(l.used, 0)) {order}, h.host_group
             LIMIT 1
            """.format(order="ASC" if bin_pack else "DESC"),
            az,
            units,
        )
        if not rows:
            raise PlacementUnavailable(az, units)

        host_group = rows[0]["host_group"]
        edge(
            log,
            10,
            f"placed {lease_id} on {host_group} ({rows[0]['free']}u free, "
            f"bin_pack={bin_pack})",
            lease_id=lease_id,
            host_group=host_group,
            az=az,
            units=units,
            purchase_class=purchase_class,
            bin_pack=bin_pack,
        )
        return host_group


class HttpPlacementScheduler:
    """Live backend: Nova / K8s scheduler client honouring the bin-pack hint."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._base = (settings.placement_url or "").rstrip("/")
        self._caller = ExternalCaller("placement", settings)

    async def place(
        self, *, az: str, units: int, purchase_class: str, bin_pack: bool,
        lease_id: str,
    ) -> str:
        return await self._caller.call(
            "place",
            lambda: self._request(
                az=az, units=units, purchase_class=purchase_class,
                bin_pack=bin_pack, lease_id=lease_id,
            ),
        )

    async def _request(
        self, *, az: str, units: int, purchase_class: str, bin_pack: bool,
        lease_id: str,
    ) -> str:
        response = await self._client.post(
            f"{self._base}/placements",
            json={
                "az": az,
                "units": units,
                "purchase_class": purchase_class,
                "bin_pack": bin_pack,
                # Idempotency key: a retried placement must not consume a second
                # host slot on the scheduler side.
                "request_id": lease_id,
            },
        )
        if response.status_code == 409:
            raise PlacementUnavailable(az, units)
        if response.status_code >= 500:
            raise ExternalError("placement", "place", f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ExternalError(
                "placement", "place", f"HTTP {response.status_code}", retryable=False
            )
        host_group = response.json().get("host_group")
        if not host_group:
            raise ExternalError(
                "placement", "place", "response carried no host_group", retryable=False
            )
        return str(host_group)

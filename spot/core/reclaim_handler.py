"""Reclaim Order Handler (edges 18, 20, 21).

The only inbound control interface. Receives reclaim(N units, host-group,
deadline) from the capacity side.

ORDERING IS THE WHOLE POINT OF THIS COMPONENT:
    edge 20 (shrink the advertised pool) fires BEFORE edge 21 (choose victims).
Miss that and you keep selling spot into capacity you are already taking back.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..bus import EventBus, Topics
from ..config import CONFIG
from ..domain.models import ReclaimOrder, new_id
from .pool_view import SpotPoolView
from .victim_selector import VictimSelector

log = logging.getLogger("spot.reclaim")


class ReclaimOrderHandler:
    def __init__(
        self, *, pool: SpotPoolView, victim_selector: VictimSelector,
        lease_manager, bus: EventBus,
    ):
        self._pool = pool
        self._selector = victim_selector
        self._leases = lease_manager
        self._bus = bus
        self.orders: list[ReclaimOrder] = []

    async def handle(
        self, *, units: int, az: str, host_group: str | None = None,
        deadline: float | None = None, reason: str = "headroom-rise",
    ) -> dict:
        """Edge 18 — capacity side -> Reclaim Order Handler."""
        order = ReclaimOrder(
            order_id=new_id("reclaim"),
            units=units,
            host_group=host_group,
            az=az,
            deadline=deadline if deadline is not None else time.time() + CONFIG.grace_seconds,
            reason=reason,
        )
        self.orders.append(order)
        log.info("edge 18  reclaim order %s: %d units in %s (%s)",
                 order.order_id, units, az, host_group or "any host group")
        await self._bus.publish(
            Topics.RECLAIM_ORDER,
            {"order_id": order.order_id, "units": units, "az": az,
             "host_group": host_group, "deadline": order.deadline},
        )

        # --- edge 20 FIRST: stop advertising the capacity, immediately -------
        await self._pool.shrink(az, units, reason=f"reclaim {order.order_id}")

        # --- edge 21/22 THEN: choose who dies --------------------------------
        victims = self._selector.select(order)
        if not victims:
            log.warning("reclaim %s: no eligible spot leases to preempt", order.order_id)
            return {"order_id": order.order_id, "preempted": [], "units_freed": 0}

        await asyncio.gather(
            *(self._leases.preempt(l, order.order_id, reason) for l in victims)
        )
        return {
            "order_id": order.order_id,
            "preempted": [l.lease_id for l in victims],
            "units_freed": sum(l.units for l in victims),
            "deadline": order.deadline,
        }

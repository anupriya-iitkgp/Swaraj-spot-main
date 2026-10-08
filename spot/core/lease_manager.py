"""Spot Lease Manager — the hub (edges 8, 9, 11, 15, 16, 23, 25, 28, 32).

Nine of the thirty-two edges touch this component, which is exactly why it must
be the SINGLE WRITER of lease state. Every transition is validated against the
state machine, appended to the audit log and published on the bus.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable, Optional

from ..bus import EventBus, Topics
from ..config import CONFIG
from ..domain.errors import LeaseNotFound
from ..domain.models import (
    ALLOWED_TRANSITIONS,
    CANCELLABLE_STATES,
    Flavour,
    IllegalTransition,
    Lease,
    LeaseState,
    new_id,
)
from ..external.capacity_ledger import CapacityLedger
from .audit_log import PreemptionAuditLog
from .pool_view import SpotPoolView
from .pricing import Pricing

log = logging.getLogger("spot.lease")


class SpotLeaseManager:
    def __init__(
        self,
        *,
        bus: EventBus,
        audit: PreemptionAuditLog,
        pool: SpotPoolView,
        ledger: CapacityLedger,
        pricing: Pricing,
    ):
        self._bus = bus
        self._audit = audit
        self._pool = pool
        self._ledger = ledger
        self._pricing = pricing

        self._leases: dict[str, Lease] = {}
        self._locks: dict[str, asyncio.Lock] = {}

        # wired by the container (avoids import cycles)
        self.placement_adapter = None
        self.provisioning_adapter = None
        self.notice_delivery = None
        self.grace_timer = None
        self.teardown_confirmer = None
        self.metering = None

    # ------------------------------------------------------------- accessors
    def get(self, lease_id: str) -> Lease:
        lease = self._leases.get(lease_id)
        if lease is None:
            raise LeaseNotFound(f"no lease {lease_id}")
        return lease

    def get_optional(self, lease_id: str) -> Optional[Lease]:
        return self._leases.get(lease_id)

    def all(self) -> list[Lease]:
        return list(self._leases.values())

    def live_leases(self, az: str | None = None, host_group: str | None = None) -> list[Lease]:
        return [
            l
            for l in self._leases.values()
            if not l.is_terminal
            and (az is None or l.az == az)
            and (host_group is None or l.host_group == host_group)
        ]

    def tenant_units_in_use(self, tenant_id: str) -> int:
        return sum(l.units for l in self._leases.values()
                   if l.tenant_id == tenant_id and not l.is_terminal)

    def _lock(self, lease_id: str) -> asyncio.Lock:
        return self._locks.setdefault(lease_id, asyncio.Lock())

    # ------------------------------------------------------- state machine
    async def transition(self, lease: Lease, dst: LeaseState, reason: str = "") -> None:
        src = lease.state
        if dst not in ALLOWED_TRANSITIONS[src]:
            raise IllegalTransition(lease.lease_id, src, dst)
        lease.state = dst
        now = time.time()
        if dst is LeaseState.ADMITTED:
            lease.admitted_at = now
        elif dst is LeaseState.RUNNING:
            lease.running_at = now
        elif dst is LeaseState.NOTICE_ISSUED:
            lease.notice_at = now
        elif dst is LeaseState.STOPPED:
            lease.stopped_at = now
        elif dst in (LeaseState.CLOSED, LeaseState.REJECTED):
            lease.closed_at = now

        # edge 28: every transition -> Preemption Audit Log
        self._audit.lease_transition(lease.lease_id, src.value, dst.value, reason)
        await self._bus.publish(
            Topics.LEASE_TRANSITION,
            {"lease_id": lease.lease_id, "from": src.value, "to": dst.value, "reason": reason},
        )
        log.info("lease %s  %s -> %s  %s", lease.lease_id, src.value, dst.value, reason)

    # ------------------------------------------------------------ creation
    async def create_lease(
        self, *, tenant_id: str, flavour: Flavour, count: int, az: str,
        units: int, discount: float, idempotency_key: str | None,
    ) -> Lease:
        """Edge 8 — Admission Controller -> Spot Lease Manager."""
        lease = Lease(
            lease_id=new_id("lease"),
            tenant_id=tenant_id,
            flavour=flavour.name,
            count=count,
            az=az,
            units=units,
            discount_snapshot=discount,
            rate_per_sec=Pricing.rate_per_sec(flavour.on_demand_rate_per_hour, count, discount),
            idempotency_key=idempotency_key,
        )
        self._leases[lease.lease_id] = lease
        await self.transition(lease, LeaseState.ADMITTED, "capacity reserved")
        await self._bus.publish(Topics.LEASE_CREATED, {"lease_id": lease.lease_id})
        return lease

    # --------------------------------------------------------- fulfilment
    async def fulfil(self, lease: Lease, *, drain_seconds: float | None = 1.0) -> None:
        """Edges 9/10 then 11/12. Asynchronous — the API call does not wait."""
        async with self._lock(lease.lease_id):
            if lease.state is not LeaseState.ADMITTED:
                return  # cancelled or already fulfilled
            await self.transition(lease, LeaseState.PROVISIONING, "placing")

        try:
            placement = await self.placement_adapter.place(lease)   # edges 9, 10
            lease.host_group = placement.host_group
            instance_ids = await self.provisioning_adapter.create(  # edges 11, 12
                lease, drain_seconds=drain_seconds
            )
        except Exception as exc:  # compensating action, or capacity leaks
            log.warning("fulfilment failed for %s: %s", lease.lease_id, exc)
            async with self._lock(lease.lease_id):
                if lease.state in CANCELLABLE_STATES:
                    lease.rejection_reason = str(exc)
                    await self.transition(lease, LeaseState.REJECTED, "provisioning failed")
                    await self._pool.release(lease.az, lease.units)
            return

        async with self._lock(lease.lease_id):
            if lease.state is not LeaseState.PROVISIONING:
                # A reclaim order landed mid-provisioning and already cancelled
                # us. Destroy what we just built; never bill it.
                log.info("lease %s cancelled during provisioning; destroying instances",
                         lease.lease_id)
                await self.provisioning_adapter.destroy_all(instance_ids)
                return
            lease.instance_ids = instance_ids
            await self.transition(lease, LeaseState.RUNNING, "instances up")
            await self._ledger.record_spot_allocated(lease.host_group, lease.units)

    # ----------------------------------------------------------- preemption
    async def preempt(self, lease: Lease, order_id: str, reason: str) -> None:
        """Edges 16 and 23 — issue the notice and start the authoritative clock."""
        async with self._lock(lease.lease_id):
            if lease.state in CANCELLABLE_STATES:
                # Never issue a termination notice for an instance that never
                # ran, and never bill it.
                await self._cancel_before_run(lease, order_id, reason)
                return
            if lease.state is not LeaseState.RUNNING:
                return
            lease.reclaim_order_id = order_id
            lease.preemption_reason = reason
            await self.transition(lease, LeaseState.NOTICE_ISSUED, reason)
            await self._ledger.mark_reclaiming(lease.host_group, lease.units)

        # edge 16 -> Notice Delivery Service -> edge 17 to the guest and tenant
        channels = await self.notice_delivery.publish_notice(lease, CONFIG.grace_seconds)
        lease.notice_channels_delivered = channels
        self._audit.notice_issued(lease.lease_id, order_id, CONFIG.grace_seconds)
        self._audit.notice_delivery(lease.lease_id, channels, bool(channels))

        if not channels:
            # All three channels failed. Still terminate on the timer — the
            # capacity is owed elsewhere — but credit it and flag the SLO breach.
            log.error("notice delivery FAILED on all channels for %s", lease.lease_id)

        # edge 23 — the timer, not the guest, is authoritative
        await self.grace_timer.start(lease)

    async def _cancel_before_run(self, lease: Lease, order_id: str, reason: str) -> None:
        lease.reclaim_order_id = order_id
        lease.preemption_reason = f"{reason} (cancelled before run)"
        await self.transition(lease, LeaseState.REJECTED, "reclaimed during provisioning")
        await self._pool.release(lease.az, lease.units)
        if lease.host_group:
            await self._ledger.release_spot(lease.host_group, lease.units)
        log.info("lease %s cancelled outright: no notice, no charge", lease.lease_id)

    async def mark_draining(self, lease: Lease) -> None:
        async with self._lock(lease.lease_id):
            if lease.state is LeaseState.NOTICE_ISSUED:
                await self.transition(lease, LeaseState.DRAINING, "guest acknowledged")

    async def mark_stopped(self, lease: Lease, *, forced: bool) -> None:
        async with self._lock(lease.lease_id):
            if lease.state not in (LeaseState.NOTICE_ISSUED, LeaseState.DRAINING,
                                   LeaseState.RUNNING):
                return
            lease.forced_stop = forced
            await self.transition(
                lease, LeaseState.STOPPED, "forced at timer expiry" if forced else "clean exit"
            )
        if forced:
            self._audit.forced_stop(lease.lease_id)
        # edges 13 -> 14 -> 15
        await self.teardown_confirmer.confirm(lease)

    async def close(self, lease: Lease, reason: str = "capacity returned") -> None:
        """Edge 15 — teardown confirmed; only now is the lease CLOSED."""
        async with self._lock(lease.lease_id):
            if lease.state is not LeaseState.STOPPED:
                return
            await self.transition(lease, LeaseState.CLOSED, reason)
        await self._pool.release(lease.az, lease.units, cooldown=True)
        await self._bus.publish(Topics.LEASE_CLOSED, {"lease_id": lease.lease_id})
        # edge 25 -> Spot Metering & Rating
        await self.metering.close_lease(lease)

    # ------------------------------------------------- customer-initiated
    async def release(self, lease_id: str) -> Lease:
        """DELETE /spot/leases/{id} — normal teardown, billing stops at release."""
        lease = self.get(lease_id)
        async with self._lock(lease.lease_id):
            if lease.is_terminal:
                return lease
            if lease.state in CANCELLABLE_STATES:
                await self.transition(lease, LeaseState.REJECTED, "released before run")
                await self._pool.release(lease.az, lease.units)
                return lease
            if lease.state is LeaseState.RUNNING:
                lease.preemption_reason = "customer release"
                await self.transition(lease, LeaseState.STOPPED, "customer release")
        if lease.state is LeaseState.STOPPED:
            await self.provisioning_adapter.stop_all(lease, graceful=True)
            await self.teardown_confirmer.confirm(lease)
        return lease

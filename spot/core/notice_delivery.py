"""Notice Delivery Service (edges 16, 17).

Fans the termination signal out over three INDEPENDENT channels and records
delivery. Non-delivery on all three is a billable credit event and an SLO
breach — never a silent failure.

  1. instance metadata endpoint  (guest polls it)
  2. tenant webhook              (push to the tenant's HTTP endpoint)
  3. tenant event stream         (bus topic the tenant subscribes to)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from ..bus import EventBus, Topics
from ..domain.models import Lease

log = logging.getLogger("spot.notice")

WebhookFn = Callable[[dict], Awaitable[None]]


class NoticeDeliveryService:
    def __init__(self, bus: EventBus, provisioning_adapter):
        self._bus = bus
        self._provisioning = provisioning_adapter
        #: instance_id -> termination metadata, read by GET /metadata/... (channel 1)
        self.metadata: dict[str, dict] = {}
        #: tenant_id -> webhook callable (channel 2)
        self.webhooks: dict[str, WebhookFn] = {}
        #: channels forced to fail, for testing the all-channels-down path
        self.disabled_channels: set[str] = set()

    def register_webhook(self, tenant_id: str, fn: WebhookFn) -> None:
        self.webhooks[tenant_id] = fn

    async def publish_notice(self, lease: Lease, grace_seconds: float) -> list[str]:
        """Edges 16/17 — returns the list of channels that succeeded."""
        deadline = time.time() + grace_seconds
        payload = {
            "lease_id": lease.lease_id,
            "tenant_id": lease.tenant_id,
            "instance_ids": list(lease.instance_ids),
            "action": "terminate",
            "reason": lease.preemption_reason,
            "grace_seconds": grace_seconds,
            "terminate_after": deadline,
            "issued_at": time.time(),
        }
        delivered: list[str] = []

        # channel 1 — instance metadata
        if "metadata" not in self.disabled_channels:
            for iid in lease.instance_ids:
                self.metadata[iid] = payload
            await self._provisioning.notify(lease)
            delivered.append("metadata")

        # channel 2 — tenant webhook
        if "webhook" not in self.disabled_channels:
            fn = self.webhooks.get(lease.tenant_id)
            if fn is not None:
                try:
                    await asyncio.wait_for(fn(payload), timeout=2.0)
                    delivered.append("webhook")
                except Exception as exc:
                    log.warning("webhook failed for %s: %s", lease.tenant_id, exc)

        # channel 3 — event stream
        if "event_stream" not in self.disabled_channels:
            await self._bus.publish(Topics.PREEMPT_NOTICE, payload)
            delivered.append("event_stream")

        log.info("edge 17  notice for %s delivered via %s", lease.lease_id, delivered or "NOTHING")
        return delivered

    def termination_time(self, instance_id: str) -> dict | None:
        """Channel 1 read path — what the guest polls."""
        return self.metadata.get(instance_id)

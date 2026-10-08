"""In-process event bus.

Stands in for Kafka/NATS. Replace `EventBus` with a real broker client and the
rest of the subsystem is unchanged — components only ever see publish/subscribe.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

log = logging.getLogger("spot.bus")

Handler = Callable[[str, dict], Awaitable[None]]


class EventBus:
    def __init__(self, history: int = 500):
        self._subs: dict[str, list[Handler]] = {}
        self._history: deque[dict] = deque(maxlen=history)
        self._seq = 0

    def subscribe(self, topic: str, handler: Handler) -> None:
        self._subs.setdefault(topic, []).append(handler)

    async def publish(self, topic: str, payload: dict) -> None:
        self._seq += 1
        record = {"seq": self._seq, "ts": time.time(), "topic": topic, "payload": payload}
        self._history.append(record)
        log.debug("event %s %s", topic, payload)
        handlers = list(self._subs.get(topic, [])) + list(self._subs.get("*", []))
        if handlers:
            await asyncio.gather(
                *(self._safe(h, topic, payload) for h in handlers), return_exceptions=True
            )

    async def _safe(self, handler: Handler, topic: str, payload: dict) -> None:
        try:
            await handler(topic, payload)
        except Exception:  # a bad subscriber must not break the publisher
            log.exception("subscriber failed for %s", topic)

    def recent(self, since: int = 0, topic_prefix: str = "") -> list[dict]:
        return [
            r
            for r in self._history
            if r["seq"] > since and r["topic"].startswith(topic_prefix)
        ]


# Topic names used across the subsystem
class Topics:
    LEASE_TRANSITION = "spot.lease.transition"
    LEASE_CREATED = "spot.lease.created"
    LEASE_CLOSED = "spot.lease.closed"
    PREEMPT_NOTICE = "spot.preempt.notice"
    PREEMPT_FORCED = "spot.preempt.forced"
    CAPACITY_RETURNED = "spot.capacity.returned"
    POOL_UPDATED = "spot.pool.updated"
    RECLAIM_ORDER = "spot.reclaim.order"
    USAGE_RECORD = "spot.usage.record"
    CREDIT_RAISED = "spot.credit.raised"

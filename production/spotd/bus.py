"""Event sink for the tenant-facing event stream.

Published *from the outbox*, never directly from the write path, so an event can
only exist for a state change that actually committed.

This build fans out in-process and keeps a bounded ring buffer that backs
`GET /spot/events`. LLD §16 replaces it with Kafka, "partition by lease_id for
per-lease ordering" — the surface here (`publish`, `subscribe`) is the same one
a Kafka producer presents, so the relay does not change.

The buffer is bounded. An events endpoint whose backing store grows with traffic
is a memory leak with a nice API in front of it.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Iterable

from .domain.models import utcnow
from .logging import get_logger

log = get_logger(__name__)

__all__ = ["EventBus", "Event"]


class Event:
    __slots__ = ("seq", "topic", "payload", "at")

    def __init__(self, seq: int, topic: str, payload: dict[str, Any]) -> None:
        self.seq = seq
        self.topic = topic
        self.payload = payload
        self.at = utcnow()

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "topic": self.topic,
            "at": self.at.isoformat(),
            "payload": self.payload,
        }


class EventBus:
    def __init__(self, *, capacity: int = 2000) -> None:
        self._events: Deque[Event] = deque(maxlen=capacity)
        self._subscribers: list[Callable[[Event], Awaitable[None]]] = []
        self._seq = 0
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, payload: dict[str, Any]) -> Event:
        async with self._lock:
            self._seq += 1
            event = Event(self._seq, topic, payload)
            self._events.append(event)
        for subscriber in list(self._subscribers):
            try:
                await subscriber(event)
            except Exception as exc:  # noqa: BLE001 - a bad subscriber is not
                # a reason to fail the publish; the outbox has already committed.
                log.warning("bus.subscriber_failed", topic=topic, error=str(exc))
        return event

    def subscribe(self, callback: Callable[[Event], Awaitable[None]]) -> None:
        self._subscribers.append(callback)

    def recent(
        self, *, topic: str | None = None, since_seq: int = 0, limit: int = 100
    ) -> list[Event]:
        matched = [
            e
            for e in self._events
            if e.seq > since_seq and (topic is None or e.topic.startswith(topic))
        ]
        return matched[-limit:]

    def for_tenant(self, tenant_id: str, *, limit: int = 100) -> list[Event]:
        matched = [
            e for e in self._events if e.payload.get("tenant_id") == tenant_id
        ]
        return matched[-limit:]

    @property
    def depth(self) -> int:
        return len(self._events)

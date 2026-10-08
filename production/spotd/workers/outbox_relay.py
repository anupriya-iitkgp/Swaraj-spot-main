"""Outbox relay — publishes what the write path recorded (LLD §12.8).

The write path never calls the ledger, the billing system or the event bus
directly. It writes an outbox row inside the same transaction as the state
change, and this relay delivers it afterwards. That turns "two systems, one
crash" into "one system, at-least-once delivery", which every downstream here is
contractually required to tolerate (LLD §9: the ledger is idempotent per
(host_group, lease, operation); billing "accepts duplicates safely").

Rows are claimed with `FOR UPDATE SKIP LOCKED` inside the publishing
transaction, so N relays share the table without contending and a relay that
dies mid-batch simply loses its lock — the rows become claimable again with
nothing marked published.

Delivery order is by primary key, which preserves per-aggregate ordering: a
lease's ADMITTED event cannot overtake its RUNNING event. When this moves to
Kafka (LLD §16) the same property comes from partitioning on `lease_id`.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..db.repositories import Topics
from ..logging import get_logger
from ..metrics import M
from .base import PeriodicWorker

log = get_logger(__name__)

__all__ = ["OutboxRelay"]


class OutboxRelay(PeriodicWorker):
    name = "outbox_relay"

    def __init__(
        self,
        *,
        settings: Settings,
        db: Any,
        outbox_repo: Any,
        ledger: Any,
        bus_sink: Any = None,
    ) -> None:
        super().__init__(interval=settings.outbox_interval, settings=settings)
        self._db = db
        self._outbox = outbox_repo
        self._ledger = ledger
        self._bus = bus_sink

    async def tick(self) -> None:
        published = 0
        async with self._db.transaction() as conn:
            messages = await self._outbox.claim(
                batch=self._settings.outbox_batch, conn=conn
            )
            if not messages:
                await self._outbox.depth(conn=conn)
                return

            delivered: list[int] = []
            for message in messages:
                try:
                    await self._dispatch(message)
                except Exception as exc:  # noqa: BLE001 - per-message isolation
                    # One poisoned message must not block the queue behind it.
                    await self._outbox.mark_failed(
                        message.id,
                        error=str(exc),
                        max_attempts=self._settings.outbox_max_attempts,
                        backoff_seconds=min(
                            300.0, 2.0 ** min(message.attempts, 8)
                        ),
                        conn=conn,
                    )
                    log.warning(
                        "outbox.publish_failed",
                        outbox_id=message.id,
                        topic=message.topic,
                        aggregate_id=message.aggregate_id,
                        attempts=message.attempts + 1,
                        error=str(exc),
                    )
                    continue
                delivered.append(message.id)
                M.outbox_published_total.labels(topic=message.topic).inc()

            await self._outbox.mark_published(delivered, conn=conn)
            published = len(delivered)
            await self._outbox.depth(conn=conn)

        if published:
            log.debug("outbox.published", count=published)

    async def _dispatch(self, message: Any) -> None:
        """Route one message to the system that owns it."""
        if message.topic == Topics.LEDGER:
            payload = message.payload
            operation = payload["operation"]
            if operation == "allocated":
                await self._ledger.record_spot_allocated(
                    host_group=payload["host_group"],
                    lease_id=payload["lease_id"],
                    units=payload["units"],
                )
            elif operation == "reclaiming":
                await self._ledger.mark_reclaiming(
                    host_group=payload["host_group"],
                    lease_id=payload["lease_id"],
                    units=payload["units"],
                )
            elif operation == "released":
                await self._ledger.release_spot(
                    host_group=payload["host_group"],
                    lease_id=payload["lease_id"],
                    units=payload["units"],
                )
            else:
                raise ValueError(f"unknown ledger operation {operation!r}")
            return

        # Everything else is an event for subscribers. In this build the bus
        # sink is an in-process fan-out that also backs `GET /spot/events`;
        # LLD §16 replaces it with Kafka partitioned by lease_id, and nothing
        # above this line changes.
        if self._bus is not None:
            await self._bus.publish(message.topic, message.payload)

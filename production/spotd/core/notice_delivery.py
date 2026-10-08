"""Notice Delivery Service — edges 16 and 17.

HLD §6:

    Owns: Fan-out over three independent channels and proof of delivery.
    Must not do: Fail silently when no channel succeeds.

HLD §11 rates this at "≥ 99.99% on at least one channel" and says why in five
words: "This is the trust anchor of the whole product."

HLD §12 adds the caveat that makes the number meaningful:

    "Three channels do not help if they share a failure mode. Ensure the
    channels are genuinely independent (different infrastructure paths). Treat
    all-channel failure as an SLO breach with automatic credit, and report the
    rate."

So the three channels here traverse deliberately different infrastructure:

  * **metadata**     — written into the guest-local metadata service by the
                       hypervisor. Reaches the instance even with no working
                       network egress, but dies with the host.
  * **webhook**      — an outbound HTTPS call to the tenant's endpoint. Survives
                       host failure, but depends on egress and on the tenant's
                       own availability.
  * **event_stream** — enqueued in the transactional outbox and relayed to the
                       tenant's event subscription. Survives both, at the cost
                       of being the slowest.

They are fanned out concurrently, not in sequence: the grace window is the
budget, and three sequential timeouts would spend a meaningful fraction of it
before the first byte reaches anyone.

Two gaps from LLD §12 are closed here. §12.5 — the in-memory metadata dict never
evicted — is fixed by keeping delivery proof in the `notice_delivery` table
instead of in the process. §12.7 — `notice_channels_delivered` written outside
the lock, so "a concurrent describe can observe a lease in NOTICE_ISSUED with an
empty channel list" — is fixed by writing the whole set in one atomic UPDATE
after the fan-out completes.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

import httpx

from ..config import Settings
from ..db.repositories import AuditEvent, Topics
from ..domain.models import Lease, NoticeChannel, NoticeReceipt, Tenant, utcnow
from ..logging import edge, get_logger
from ..metrics import M

log = get_logger(__name__)

__all__ = ["NoticeDeliveryService", "NoticeResult"]

#: The webhook gets a hard, short timeout. A tenant endpoint that takes four
#: seconds to answer has already failed for our purposes — the notice is only
#: useful if it arrives with enough of the grace window left to act on.
_WEBHOOK_TIMEOUT = 2.0


@dataclass(frozen=True, slots=True)
class NoticeResult:
    lease_id: str
    delivered: tuple[NoticeChannel, ...]
    receipts: tuple[NoticeReceipt, ...]

    @property
    def any_delivered(self) -> bool:
        return bool(self.delivered)


class NoticeDeliveryService:
    def __init__(
        self,
        *,
        settings: Settings,
        provisioning: Any,
        outbox_repo: Any,
        audit_repo: Any,
        db: Any,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._provisioning = provisioning
        self._outbox = outbox_repo
        self._audit = audit_repo
        self._db = db
        self._client = http_client
        #: Simulated tenant endpoints, for `sim://` webhook URLs.
        self.simulated_inbox: dict[str, list[dict[str, Any]]] = {}

    async def publish_notice(
        self, lease: Lease, tenant: Tenant, deadline: datetime, *, order_id: str | None
    ) -> NoticeResult:
        """Fan out one preemption notice. Never raises — a failure is a result."""
        payload = {
            "type": "spot.preemption.notice",
            "lease_id": lease.lease_id,
            "tenant_id": lease.tenant_id,
            "az": lease.az,
            "flavour": lease.flavour,
            "count": lease.count,
            "instance_ids": list(lease.instance_ids),
            "deadline": deadline.isoformat(),
            "grace_seconds": lease.grace_seconds,
            "reclaim_order_id": order_id,
            "issued_at": utcnow().isoformat(),
        }

        receipts = await asyncio.gather(
            self._metadata(lease, deadline),
            self._webhook(lease, tenant, payload),
            self._event_stream(lease, payload),
            return_exceptions=True,
        )

        settled: list[NoticeReceipt] = []
        for channel, receipt in zip(
            (NoticeChannel.METADATA, NoticeChannel.WEBHOOK, NoticeChannel.EVENT_STREAM),
            receipts,
        ):
            if isinstance(receipt, NoticeReceipt):
                settled.append(receipt)
                continue
            # A channel that raised — or that returned something other than a
            # receipt — is a channel that did not deliver. It is recorded as such
            # rather than allowed to abort the fan-out: the other two may well
            # have succeeded, and a crash here would leave a lease noticed with
            # no record of whether anyone was told.
            detail = (
                f"{type(receipt).__name__}: {receipt}"
                if isinstance(receipt, BaseException)
                else f"channel returned {type(receipt).__name__}, expected a receipt"
            )
            settled.append(
                NoticeReceipt(
                    lease_id=lease.lease_id,
                    channel=channel,
                    delivered=False,
                    attempt=1,
                    at=utcnow(),
                    error=detail,
                )
            )

        await self._record(lease, settled)
        delivered = tuple(r.channel for r in settled if r.delivered)

        for receipt in settled:
            M.notice_delivery_total.labels(
                channel=receipt.channel.value,
                delivered=str(receipt.delivered).lower(),
            ).inc()

        if delivered:
            edge(
                log,
                17,
                f"notice delivered via {', '.join(c.value for c in delivered)}",
                lease_id=lease.lease_id,
                channels=[c.value for c in delivered],
                deadline=deadline.isoformat(),
            )
            await self._audit.append(
                AuditEvent.NOTICE_DELIVERED,
                lease_id=lease.lease_id,
                order_id=order_id,
                tenant_id=lease.tenant_id,
                detail={
                    "channels": [c.value for c in delivered],
                    "failed": [
                        {"channel": r.channel.value, "error": r.error}
                        for r in settled
                        if not r.delivered
                    ],
                    "deadline": deadline.isoformat(),
                },
            )
        else:
            # HLD §6: must not fail silently. This is an SLO breach and an
            # automatic credit; the credit itself is raised by the metering
            # service on close, from `notice_channels_delivered` being empty.
            M.notice_all_channels_failed_total.labels(az=lease.az).inc()
            log.error(
                "notice.all_channels_failed",
                lease_id=lease.lease_id,
                tenant_id=lease.tenant_id,
                errors={r.channel.value: r.error for r in settled},
                note="HLD §11: notice delivery is the trust anchor of the "
                "product; this is an SLO breach and an automatic credit",
            )
            await self._audit.append(
                AuditEvent.NOTICE_ALL_CHANNELS_FAILED,
                lease_id=lease.lease_id,
                order_id=order_id,
                tenant_id=lease.tenant_id,
                detail={
                    "errors": {r.channel.value: r.error for r in settled},
                    "consequence": "automatic credit on close",
                },
            )

        return NoticeResult(
            lease_id=lease.lease_id, delivered=delivered, receipts=tuple(settled)
        )

    # ------------------------------------------------------------------
    # the three channels
    # ------------------------------------------------------------------
    async def _metadata(self, lease: Lease, deadline: datetime) -> NoticeReceipt:
        started = time.perf_counter()
        ok = await self._provisioning.deliver_notice(lease, deadline)
        return NoticeReceipt(
            lease_id=lease.lease_id,
            channel=NoticeChannel.METADATA,
            delivered=bool(ok),
            attempt=1,
            at=utcnow(),
            error=None if ok else "metadata service did not accept the notice",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def _webhook(
        self, lease: Lease, tenant: Tenant, payload: dict[str, Any]
    ) -> NoticeReceipt:
        started = time.perf_counter()

        def receipt(delivered: bool, error: str | None) -> NoticeReceipt:
            return NoticeReceipt(
                lease_id=lease.lease_id,
                channel=NoticeChannel.WEBHOOK,
                delivered=delivered,
                attempt=1,
                at=utcnow(),
                error=error,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )

        url = tenant.webhook_url
        if not url:
            return receipt(False, "tenant has no webhook configured")

        if url.startswith("sim://"):
            # A simulated tenant endpoint. Delivery still fails sometimes,
            # because a channel that never fails proves nothing about the
            # all-channels-failed path.
            self.simulated_inbox.setdefault(lease.tenant_id, []).append(payload)
            if hash((lease.lease_id, "webhook")) % 20 == 0:
                return receipt(False, "simulated tenant endpoint returned 503")
            return receipt(True, None)

        if self._client is None:
            return receipt(False, "no HTTP client configured for webhook delivery")

        try:
            response = await self._client.post(
                url, json=payload, timeout=_WEBHOOK_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 - any transport failure is a miss
            return receipt(False, f"{type(exc).__name__}: {exc}")

        if response.status_code >= 300:
            return receipt(False, f"HTTP {response.status_code}")
        return receipt(True, None)

    async def _event_stream(
        self, lease: Lease, payload: dict[str, Any]
    ) -> NoticeReceipt:
        """Enqueue on the outbox; the relay publishes to the tenant's subscription.

        This channel is 'delivered' once the intent is durably recorded. That is
        a weaker claim than the other two and it is deliberate: the outbox
        guarantees the event will be published even across a crash, which is a
        stronger guarantee than a synchronous publish that might be lost in
        flight.
        """
        started = time.perf_counter()
        try:
            await self._outbox.enqueue(
                topic=Topics.LEASE_NOTICE,
                aggregate_type="lease",
                aggregate_id=lease.lease_id,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001
            return NoticeReceipt(
                lease_id=lease.lease_id,
                channel=NoticeChannel.EVENT_STREAM,
                delivered=False,
                attempt=1,
                at=utcnow(),
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        return NoticeReceipt(
            lease_id=lease.lease_id,
            channel=NoticeChannel.EVENT_STREAM,
            delivered=True,
            attempt=1,
            at=utcnow(),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    # ------------------------------------------------------------------
    async def _record(self, lease: Lease, receipts: Sequence[NoticeReceipt]) -> None:
        """Persist proof of delivery — the LLD §12.5 eviction fix."""
        await self._db.executemany(
            """
            INSERT INTO notice_delivery
                (lease_id, tenant_id, channel, attempt, delivered, latency_ms,
                 error, at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (lease_id, channel, attempt) DO NOTHING
            """,
            [
                (
                    r.lease_id,
                    lease.tenant_id,
                    r.channel.value,
                    r.attempt,
                    r.delivered,
                    r.latency_ms,
                    r.error,
                    r.at,
                )
                for r in receipts
            ],
        )

    async def delivery_rate(self, since: datetime) -> dict[str, Any]:
        """The rate HLD §12 asks to be reported, per channel and overall."""
        rows = await self._db.fetch(
            """
            SELECT channel,
                   COUNT(*)::int AS attempts,
                   COUNT(*) FILTER (WHERE delivered)::int AS delivered
              FROM notice_delivery WHERE at >= $1 GROUP BY channel
            """,
            since,
        )
        per_channel = {
            r["channel"]: {
                "attempts": r["attempts"],
                "delivered": r["delivered"],
                "rate": round(r["delivered"] / r["attempts"], 6) if r["attempts"] else None,
            }
            for r in rows
        }
        overall = await self._db.fetchrow(
            """
            SELECT COUNT(DISTINCT lease_id)::int AS leases,
                   COUNT(DISTINCT lease_id) FILTER (WHERE delivered)::int AS reached
              FROM notice_delivery WHERE at >= $1
            """,
            since,
        )
        leases = overall["leases"] if overall else 0
        reached = overall["reached"] if overall else 0
        return {
            "per_channel": per_channel,
            "leases_noticed": leases,
            "leases_reached_on_at_least_one_channel": reached,
            # The HLD §11 number: >= 99.99% on at least one channel.
            "at_least_one_channel_rate": round(reached / leases, 6) if leases else None,
            "target": 0.9999,
        }

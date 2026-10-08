"""Admission Controller — edges 7, 8 and 31.

HLD §6:

    Owns: The atomic reserve that resolves the race between a stale read and
          concurrent launches.
    Must not do: Over-allocate under any circumstance. It rejects instead.
    Key operations: tryReserve(units, key), release(key)

"It rejects instead" is the design's answer to every ambiguous case, and this
module takes it literally. There is no path here that admits a lease without a
reserve having succeeded first, and no path that succeeds partially.

The unit of atomicity is one database transaction covering three writes:

    idempotency claim  ->  pool reserve  ->  lease insert

All three, or none. That grouping is what makes the failure modes uninteresting.
A crash after the reserve but before the lease insert would leave units reserved
with nothing accountable holding them — the counter would drift upward forever,
which `PoolRepository.reconcile` would report as drift and no operator could
explain. Inside one transaction that state cannot exist.

Idempotency is claimed *first*, before the reserve, so a retry storm of the same
key contends on a primary key rather than on the pool row. HLD §7 wants a
rejection to be cheap; losing on a unique index is the cheapest rejection
available.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..domain.errors import IdempotencyConflict, NoCapacity
from ..domain.models import (
    Lease,
    PurchaseOption,
    PurchaseOptionSource,
    RejectionCode,
    new_id,
    utcnow,
)
from ..domain.state_machine import LeaseState
from ..db.repositories import AuditEvent, Topics
from ..logging import edge, get_logger
from ..metrics import M
from .eligibility_guard import Validated
from .pricing import PricingEngine, Quote

log = get_logger(__name__)

__all__ = ["AdmissionController", "Admission"]


@dataclass(frozen=True, slots=True)
class Admission:
    lease: Lease
    quote: Quote
    replayed: bool
    #: Populated only on a replay, so the caller can return the original bytes.
    stored_response: dict[str, Any] | None = None
    stored_status: int | None = None


class AdmissionController:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Any,
        pool_repo: Any,
        lease_repo: Any,
        idempotency_repo: Any,
        audit_repo: Any,
        outbox_repo: Any,
        pool_view: Any,
        pricing: PricingEngine,
    ) -> None:
        self._settings = settings
        self._db = db
        self._pool = pool_repo
        self._leases = lease_repo
        self._idem = idempotency_repo
        self._audit = audit_repo
        self._outbox = outbox_repo
        self._pool_view = pool_view
        self._pricing = pricing

    async def admit(
        self,
        *,
        validated: Validated,
        az: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> Admission:
        """Reserve capacity and create the lease, or reject.

        Raises `NoCapacity` (409) when the reserve loses, and
        `IdempotencyConflict` (409) when the key was used for a different
        request.
        """
        started = time.perf_counter()
        tenant_id = validated.tenant.tenant_id

        # -- fast path: a completed key replays without touching the pool --
        existing = await self._idem.get(tenant_id, idempotency_key)
        if existing is not None and existing.is_complete:
            return await self._replay(existing, request_fingerprint, validated)

        async with self._db.transaction() as conn:
            owner, record = await self._idem.claim(
                tenant_id,
                idempotency_key,
                request_fingerprint,
                self._settings.idempotency_ttl,
                conn=conn,
            )

            if not owner:
                if record.request_fingerprint != request_fingerprint:
                    raise IdempotencyConflict(
                        "this idempotency key was used for a different request; "
                        "reusing it would return a lease you did not ask for",
                        details={"idempotency_key": idempotency_key},
                    )
                if record.is_complete:
                    return await self._replay(record, request_fingerprint, validated)
                # Claimed but not finished: an identical request is in flight on
                # another replica. Answering 409 with Retry-After is honest —
                # the client's original call is still being processed.
                raise NoCapacity(
                    "a request with this idempotency key is already in flight",
                    details={"idempotency_key": idempotency_key, "state": "in_progress"},
                    retry_after=1,
                )

            # -- price against the pool as it stands, before reserving ----
            snapshot = await self._pool.get(az, conn=conn)
            if snapshot is None:
                raise NoCapacity(
                    f"no spot pool is published for {az}",
                    details={"az": az},
                    retry_after=self._settings.retry_after,
                )
            quote = self._pricing.quote(snapshot, validated.flavour, validated.count)

            # -- THE RESERVE (HLD §7) -------------------------------------
            outcome = await self._pool.try_reserve(az, validated.units, conn=conn)
            if not outcome:
                # Release the claim so an honest retry with the same key is not
                # answered from a pending row that never produced a result.
                await self._idem.release(tenant_id, idempotency_key, conn=conn)
                await self._audit.append(
                    AuditEvent.RESERVE_REFUSED,
                    tenant_id=tenant_id,
                    detail={
                        "az": az,
                        "units_requested": validated.units,
                        "units_available": outcome.available_after,
                        "flavour": validated.flavour.name,
                    },
                    conn=conn,
                )
                alternatives = await self._pool_view.alternatives(
                    az, validated.flavour, validated.count
                )
                M.admission_total.labels(outcome="409").inc()
                M.admission_latency.labels(outcome="409").observe(
                    time.perf_counter() - started
                )
                raise NoCapacity(
                    f"{az} cannot serve {validated.units} units right now "
                    f"({outcome.available_after} available)",
                    details={
                        "az": az,
                        "units_requested": validated.units,
                        "units_available": outcome.available_after,
                        # HLD §11 wants a 409 to be actionable, not just correct.
                        "alternatives": alternatives,
                        "pool_staleness_seconds": round(
                            snapshot.staleness_seconds(), 1
                        ),
                    },
                    retry_after=self._settings.retry_after,
                )

            # -- edge 8: create the lease, ADMITTED -----------------------
            now = utcnow()
            lease = Lease(
                lease_id=new_id("lease"),
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                purchase_option=validated.purchase_option,
                purchase_option_source=validated.purchase_option_source,
                flavour=validated.flavour.name,
                count=validated.count,
                units=validated.units,
                az=az,
                state=LeaseState.ADMITTED,
                discount_snapshot=quote.discount,
                rate_per_sec=quote.rate_per_sec,
                grace_seconds=self._settings.grace_seconds,
                created_at=now,
                admitted_at=now,
            )
            lease = await self._leases.create(lease, conn=conn)

            await self._idem.complete(
                tenant_id,
                idempotency_key,
                lease_id=lease.lease_id,
                status=201,
                body={"lease_id": lease.lease_id},
                outcome="admitted",
                conn=conn,
            )
            await self._audit.append(
                AuditEvent.LEASE_CREATED,
                lease_id=lease.lease_id,
                tenant_id=tenant_id,
                detail={
                    "az": az,
                    "flavour": lease.flavour,
                    "count": lease.count,
                    "units": lease.units,
                    "discount": quote.discount,
                    "rate_per_sec": lease.rate_per_sec,
                    "surplus_depth": quote.surplus_depth,
                    "purchase_option": lease.purchase_option.value,
                    "purchase_option_source": lease.purchase_option_source.value,
                },
                conn=conn,
            )
            # Published from the outbox after commit, so a subscriber can never
            # see a lease event for a transaction that rolled back.
            await self._outbox.enqueue(
                topic=Topics.LEASE_STATE,
                aggregate_type="lease",
                aggregate_id=lease.lease_id,
                payload={
                    "lease_id": lease.lease_id,
                    "tenant_id": tenant_id,
                    "state": lease.state.value,
                    "az": az,
                    "units": lease.units,
                    "flavour": lease.flavour,
                    "at": now.isoformat(),
                },
                conn=conn,
            )

        elapsed = time.perf_counter() - started
        M.admission_total.labels(outcome="admitted").inc()
        M.admission_latency.labels(outcome="admitted").observe(elapsed)
        edge(
            log,
            8,
            f"admitted {lease.lease_id}: {lease.units}u in {az} "
            f"at {quote.saving_pct}% off ({elapsed * 1000:.0f}ms)",
            lease_id=lease.lease_id,
            tenant_id=tenant_id,
            units=lease.units,
            discount=quote.discount,
            latency_ms=round(elapsed * 1000, 2),
        )
        return Admission(lease=lease, quote=quote, replayed=False)

    async def _replay(
        self, record: Any, request_fingerprint: str, validated: Validated
    ) -> Admission:
        """Return the original outcome for a repeated idempotency key."""
        if record.request_fingerprint != request_fingerprint:
            raise IdempotencyConflict(
                "this idempotency key was used for a different request",
                details={"idempotency_key": record.key},
            )

        lease = (
            await self._leases.get(record.lease_id) if record.lease_id else None
        )
        if lease is None:
            # The key survived its lease (a very old key, or a purge). Treat it
            # as absent rather than replaying a reference to nothing.
            raise IdempotencyConflict(
                "the lease created for this idempotency key no longer exists; "
                "retry with a new key",
                details={"idempotency_key": record.key},
            )

        snapshot = await self._pool.get(lease.az)
        quote = (
            self._pricing.quote(snapshot, validated.flavour, validated.count)
            if snapshot
            else Quote(lease.discount_snapshot, lease.rate_per_sec, 0.0, 0.0, False)
        )
        log.info(
            "admission.idempotent_replay",
            lease_id=lease.lease_id,
            tenant_id=lease.tenant_id,
            idempotency_key=record.key,
            note="returning the original lease; no capacity was reserved",
        )
        M.admission_total.labels(outcome="replayed").inc()
        return Admission(
            lease=lease,
            quote=quote,
            replayed=True,
            stored_response=record.response_body,
            stored_status=record.response_status,
        )

    # ------------------------------------------------------------------
    async def release(
        self, lease: Lease, *, cooldown: bool, reason: str, conn: Any = None
    ) -> None:
        """Give a lease's units back to the pool — HLD §6's `release(key)`.

        `cooldown=True` for capacity taken back by a reclaim, so the anti-thrash
        hold applies. A customer-initiated release goes straight back into the
        pool: they chose to leave, so there is no churn to damp.
        """
        await self._pool.release(
            lease.az,
            lease.units,
            cooldown=cooldown,
            lease_id=lease.lease_id,
            cooldown_seconds=self._settings.cooldown if cooldown else 0.0,
            reason=reason,
            conn=conn,
        )
        edge(
            log,
            31,
            f"released {lease.units}u from {lease.az} ({reason})",
            lease_id=lease.lease_id,
            units=lease.units,
            cooldown=cooldown,
            reason=reason,
        )

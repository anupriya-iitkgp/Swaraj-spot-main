"""Admission Controller (edges 7, 8, 31).

The atomic reserve that resolves the race between a stale read model and
concurrent launches. It never over-allocates: it rejects instead.

Also owns idempotency — a client retry with the same key returns the ORIGINAL
lease. Without this a network timeout costs the customer double capacity.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from ..config import CONFIG
from ..domain.errors import NoCapacity
from ..domain.models import Flavour, Lease
from .pool_view import SpotPoolView

log = logging.getLogger("spot.admission")


@dataclass
class _IdemRecord:
    lease_id: str
    created_at: float


class AdmissionController:
    def __init__(self, pool: SpotPoolView, lease_manager, pricing):
        self._pool = pool
        self._leases = lease_manager
        self._pricing = pricing
        self._idem: dict[tuple[str, str], _IdemRecord] = {}
        self._idem_lock = asyncio.Lock()

    # ------------------------------------------------------------ idempotency
    async def _lookup_idempotent(self, tenant_id: str, key: str | None) -> Lease | None:
        if not key:
            return None
        async with self._idem_lock:
            rec = self._idem.get((tenant_id, key))
            if rec is None:
                return None
            if time.time() - rec.created_at > CONFIG.idempotency_ttl_seconds:
                self._idem.pop((tenant_id, key), None)
                return None
        return self._leases.get_optional(rec.lease_id)

    async def _remember(self, tenant_id: str, key: str | None, lease_id: str) -> None:
        if key:
            async with self._idem_lock:
                self._idem[(tenant_id, key)] = _IdemRecord(lease_id, time.time())

    # ------------------------------------------------------------- admission
    async def admit(
        self,
        *,
        tenant_id: str,
        flavour: Flavour,
        count: int,
        az: str,
        idempotency_key: str | None,
    ) -> tuple[Lease, bool]:
        """Edges 7 and 31, then edge 8.

        Returns (lease, replayed). `replayed=True` means an idempotent retry
        returned the original lease and nothing new was reserved.
        """
        existing = await self._lookup_idempotent(tenant_id, idempotency_key)
        if existing is not None:
            log.info("edge 7   idempotent replay -> %s", existing.lease_id)
            return existing, True

        units = flavour.units * count

        # Edge 31: the atomic reserve. The pool read was a hint; this is the
        # decision. A failure here is expected traffic, not an incident.
        if not await self._pool.try_reserve(az, units):
            available = self._pool.get_sellable(az)
            log.info("edge 7   409: %s wanted %d units in %s, %d available",
                     tenant_id, units, az, available)
            raise NoCapacity(
                f"only {available} spot units available in {az}",
                retry_after=CONFIG.retry_after_seconds,
                alternatives=self._pool.alternatives(az, units),
            )

        try:
            discount = self._pricing.discount_for(az)
            # Edge 8: createLease(ADMITTED) — discount is snapshotted here and
            # never re-rated for the life of the lease.
            lease = await self._leases.create_lease(
                tenant_id=tenant_id,
                flavour=flavour,
                count=count,
                az=az,
                units=units,
                discount=discount,
                idempotency_key=idempotency_key,
            )
        except Exception:
            await self._pool.release(az, units)
            raise

        await self._remember(tenant_id, idempotency_key, lease.lease_id)
        return lease, False

"""Builds the six external adapters from configuration.

`SPOT_BACKEND` selects the whole set together rather than per-service, on
purpose: a half-live configuration — a real hypervisor driven by a simulated
capacity ledger, say — would destroy real instances on the strength of numbers
nobody is accounting for. Config validation already refuses `backend=sim` in
production and requires every URL when `backend=live`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..logging import get_logger
from .account_service import AccountService, DatabaseAccountService, HttpAccountService
from .billing import BillingSystem, HttpBillingSystem, SimulatedBillingSystem
from .forecast import ForecastFeed, HttpForecastFeed, SimulatedForecastFeed
from .hypervisor import HttpHypervisor, Hypervisor, SimulatedHypervisor
from .ledger import CapacityLedger, HttpCapacityLedger, SimulatedCapacityLedger
from .placement import (
    HttpPlacementScheduler,
    PlacementScheduler,
    SimulatedPlacementScheduler,
)

log = get_logger(__name__)

__all__ = ["Externals", "build_externals"]


@dataclass(slots=True)
class Externals:
    """The six dashed boxes, resolved."""

    accounts: AccountService
    forecast: ForecastFeed
    ledger: CapacityLedger
    placement: PlacementScheduler
    hypervisor: Hypervisor
    billing: BillingSystem
    _client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        closer = getattr(self.hypervisor, "aclose", None)
        if closer is not None:
            await closer()
        if self._client is not None:
            await self._client.aclose()


def build_externals(settings: Settings, db: Any) -> Externals:
    if settings.backend == "sim":
        log.info(
            "externals.simulated",
            note="SPOT_BACKEND=sim — capacity, placement and instances are "
            "synthetic; config validation refuses this in production",
        )
        return Externals(
            accounts=DatabaseAccountService(db),
            forecast=SimulatedForecastFeed(db, settings),
            ledger=SimulatedCapacityLedger(),
            placement=SimulatedPlacementScheduler(db, settings),
            hypervisor=SimulatedHypervisor(settings),
            billing=SimulatedBillingSystem(),
        )

    # One connection pool across all six: they are separate services but they
    # share a limits budget, and an unbounded pool per adapter is how a slow
    # dependency exhausts file descriptors.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.external_timeout),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        headers={"user-agent": f"{settings.service_name}/{settings.worker_id}"},
    )
    log.info("externals.live", backend="live")
    return Externals(
        accounts=HttpAccountService(settings, client),
        forecast=HttpForecastFeed(settings, client),
        ledger=HttpCapacityLedger(settings, client),
        placement=HttpPlacementScheduler(settings, client),
        hypervisor=HttpHypervisor(settings, client),
        billing=HttpBillingSystem(settings, client),
        _client=client,
    )

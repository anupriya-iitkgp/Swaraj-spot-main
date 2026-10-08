"""Adapters for the six out-of-scope services (the dashed boxes in HLD §4).

Each module defines a `Protocol` — the contract from LLD §9 — plus two
implementations:

  * a **simulator** used when `SPOT_BACKEND=sim`, faithful enough that every
    branch of the consuming code is exercised, including the failure branches;
  * an **HTTP client** used when `SPOT_BACKEND=live`, wrapped in the timeout,
    retry, breaker and metrics stack from `base.py`.

Nothing above this package imports either implementation directly. Components
receive the Protocol, so swapping a simulator for the real service is a change
in `factory.py` and nowhere else — which is what "keep the method signatures,
change the body" means in practice.
"""

from .account_service import AccountService, DatabaseAccountService, HttpAccountService
from .base import (
    BreakerState,
    CircuitBreaker,
    CircuitOpen,
    ExternalCaller,
    ExternalError,
    ExternalTimeout,
)
from .billing import BillingSystem, HttpBillingSystem, SimulatedBillingSystem
from .factory import Externals, build_externals
from .forecast import ForecastFeed, HttpForecastFeed, SimulatedForecastFeed
from .hypervisor import (
    GuestBehaviour,
    HostUnreachable,
    HttpHypervisor,
    Hypervisor,
    SimulatedHypervisor,
)
from .ledger import CapacityLedger, HttpCapacityLedger, SimulatedCapacityLedger
from .placement import (
    HttpPlacementScheduler,
    PlacementScheduler,
    PlacementUnavailable,
    SimulatedPlacementScheduler,
)

__all__ = [
    "AccountService",
    "BillingSystem",
    "BreakerState",
    "CapacityLedger",
    "CircuitBreaker",
    "CircuitOpen",
    "DatabaseAccountService",
    "Externals",
    "ExternalCaller",
    "ExternalError",
    "ExternalTimeout",
    "ForecastFeed",
    "GuestBehaviour",
    "HostUnreachable",
    "HttpAccountService",
    "HttpBillingSystem",
    "HttpCapacityLedger",
    "HttpForecastFeed",
    "HttpHypervisor",
    "HttpPlacementScheduler",
    "Hypervisor",
    "PlacementScheduler",
    "PlacementUnavailable",
    "SimulatedBillingSystem",
    "SimulatedCapacityLedger",
    "SimulatedForecastFeed",
    "SimulatedHypervisor",
    "build_externals",
]

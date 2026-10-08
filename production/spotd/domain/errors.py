"""Typed rejections.

Every way a spot request can fail is one class here, and each carries the HTTP
status it maps to plus a stable machine-readable `code`. Two rules the design
leans on:

  * A rejection is *cheap and actionable* (HLD §7): `409 NoCapacity` is normal
    traffic on a busy pool, not an incident, so it carries `Retry-After` and a
    list of alternatives the customer can actually act on rather than a bare
    error string.

  * Unknown tenants and unverifiable entitlement *fail closed* (LLD §9). There
    is no "assume spot" path anywhere in this module.

The `code` values are part of the public API contract. Change a message freely;
changing a code breaks customer automation.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SpotError",
    "InvalidRequest",
    "Unauthenticated",
    "OutOfScope",
    "NotEntitled",
    "UnknownTenant",
    "FlavourNotEligible",
    "QuotaExceeded",
    "RateLimited",
    "NoCapacity",
    "LeaseNotFound",
    "IdempotencyConflict",
    "InvalidLeaseState",
    "PlacementFailed",
    "ProvisioningFailed",
    "DependencyUnavailable",
    "InternalAuthFailed",
]


class SpotError(Exception):
    """Base class. Subclasses set `status` and `code`."""

    status: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.retry_after = retry_after

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        if self.retry_after is not None:
            body["error"]["retry_after"] = self.retry_after
        return body

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.code}, {self.message!r})"


# -- 400 -------------------------------------------------------------------

class InvalidRequest(SpotError):
    status, code = 400, "invalid_request"


class FlavourNotEligible(SpotError):
    """Licence-bound or otherwise non-spot flavours are never sold as spot.

    HLD §6 gives the Eligibility Guard "flavour eligibility" and forbids it from
    trusting a class supplied by the caller.
    """

    status, code = 400, "flavour_not_spot_eligible"


# -- 401 / 403 -------------------------------------------------------------

class Unauthenticated(SpotError):
    status, code = 401, "unauthenticated"


class InternalAuthFailed(SpotError):
    """Signature, timestamp or nonce failed on an /internal call (LLD §12.1)."""

    status, code = 401, "internal_auth_failed"


class UnknownTenant(SpotError):
    """The Account Service returned no class. Fail closed — never guess."""

    status, code = 403, "unknown_tenant"


class NotEntitled(SpotError):
    """The account exists but is not entitled to spot."""

    status, code = 403, "not_entitled"


# -- 404 -------------------------------------------------------------------

class LeaseNotFound(SpotError):
    status, code = 404, "lease_not_found"


# -- 409 -------------------------------------------------------------------

class NoCapacity(SpotError):
    """The atomic reserve lost.

    HLD §7: "A 409 is normal traffic on a busy pool, not an incident." It is
    logged at INFO, counted, and answered with Retry-After plus alternatives.
    """

    status, code = 409, "no_capacity"


class IdempotencyConflict(SpotError):
    """Same idempotency key, materially different request body.

    Returning the original lease would silently give the caller something other
    than what they asked for, so this is a rejection rather than a replay.
    """

    status, code = 409, "idempotency_conflict"


class InvalidLeaseState(SpotError):
    """The operation is not legal from the lease's current state."""

    status, code = 409, "invalid_lease_state"


# -- 429 -------------------------------------------------------------------

class QuotaExceeded(SpotError):
    """Per-tenant ceiling, enforced before any capacity work is done."""

    status, code = 429, "quota_exceeded"


class RateLimited(SpotError):
    """Token bucket. Closes the retry-storm gap in LLD §12.9."""

    status, code = 429, "rate_limited"


# -- 501 -------------------------------------------------------------------

class OutOfScope(SpotError):
    """STATIC and DYNAMIC requests leave this design entirely (HLD §1).

    This service does not implement the reserved or pay-per-use paths, and
    pretending otherwise would be worse than saying so.
    """

    status, code = 501, "out_of_scope_for_spot_subsystem"


# -- 503 -------------------------------------------------------------------

class PlacementFailed(SpotError):
    status, code = 503, "placement_failed"


class ProvisioningFailed(SpotError):
    status, code = 503, "provisioning_failed"


class DependencyUnavailable(SpotError):
    """An out-of-scope service is down or its circuit breaker is open."""

    status, code = 503, "dependency_unavailable"

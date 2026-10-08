"""Typed rejections. Each maps to one row of the HLD edge-case table."""
from __future__ import annotations

from typing import Optional


class SpotError(Exception):
    status_code = 400
    code = "spot_error"

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


class NotEntitled(SpotError):
    """403 — account is not entitled to spot. Never fall back to a paid class."""

    status_code = 403
    code = "not_entitled"


class QuotaExceeded(SpotError):
    """429 — tenant spot quota or concurrency cap exceeded."""

    status_code = 429
    code = "quota_exceeded"


class FlavourNotEligible(SpotError):
    """400 — flavour is permanently excluded from spot."""

    status_code = 400
    code = "flavour_not_spot_eligible"


class BadRequest(SpotError):
    status_code = 400
    code = "bad_request"


class NoCapacity(SpotError):
    """409 — pool exhausted, or a stale read lost the race in tryReserve().

    This is expected traffic on a busy pool, not an incident. It must be cheap
    and it must carry Retry-After plus a hint at where capacity does exist.
    """

    status_code = 409
    code = "no_capacity"

    def __init__(
        self,
        message: str,
        retry_after: int = 5,
        alternatives: Optional[list[dict]] = None,
    ):
        super().__init__(message, retry_after=retry_after, alternatives=alternatives or [])
        self.retry_after = retry_after


class LeaseNotFound(SpotError):
    status_code = 404
    code = "lease_not_found"

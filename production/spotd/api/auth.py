"""Signed-request authentication for `/internal`.

LLD §12.1:

    Gap: /internal/spot/reclaim and /sim/* are unauthenticated.
    Consequence: Anyone who can reach the pod can terminate every spot lease in
                 an AZ.
    Fix: mTLS or a signed service token on /internal; remove /sim from
         production builds via a feature flag.

Both halves are implemented — the signed token here, the feature flag in
`config.enable_sim_endpoints` (which config validation forces to false in
production).

The scheme signs the *request*, not just the caller. A bearer token proves who
is calling; it does not prevent that call being replayed, or its body being
swapped in transit. For an endpoint whose effect is "terminate every spot lease
in an AZ" that distinction is the whole point.

    canonical = METHOD \\n PATH \\n TIMESTAMP \\n NONCE \\n SHA256(BODY)
    signature = HMAC-SHA256(key, canonical)

Four checks, all required:

  1. **Timestamp within skew** — bounds how long a captured request stays useful
     even if the nonce store is lost.
  2. **Nonce unused** — makes each signed request usable exactly once. Without
     it, a captured reclaim order could be replayed for the whole skew window,
     and a replayed reclaim kills a second set of customer instances.
  3. **Body hash covers the payload** — the unit count and host group are inside
     the signature, so a proxy cannot turn a 10-unit reclaim into a 1000-unit one.
  4. **Constant-time comparison** — signature verification that short-circuits
     leaks the correct prefix a byte at a time.

Two keys are accepted (`current` and `previous`) so a key can be rotated without
a synchronised restart of both sides.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..domain.errors import InternalAuthFailed
from ..logging import get_logger

log = get_logger(__name__)

__all__ = [
    "SignatureVerifier",
    "sign_request",
    "HEADER_TIMESTAMP",
    "HEADER_NONCE",
    "HEADER_SIGNATURE",
    "HEADER_KEY_ID",
    "HEADER_CALLER",
]

HEADER_TIMESTAMP = "x-spot-timestamp"
HEADER_NONCE = "x-spot-nonce"
HEADER_SIGNATURE = "x-spot-signature"
HEADER_KEY_ID = "x-spot-key-id"
HEADER_CALLER = "x-spot-caller"


def canonical_string(
    method: str, path: str, timestamp: str, nonce: str, body: bytes
) -> str:
    return "\n".join(
        [
            method.upper(),
            path,
            timestamp,
            nonce,
            hashlib.sha256(body).hexdigest(),
        ]
    )


def sign_request(
    *,
    key: str,
    method: str,
    path: str,
    body: bytes = b"",
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Produce the headers for a signed call.

    Used by the CLI, the tests, and by whatever on the capacity side issues
    reclaim orders. Exported so nobody has to reimplement the canonical string
    and get it subtly wrong.
    """
    ts = str(timestamp if timestamp is not None else int(time.time()))
    non = nonce or uuid.uuid4().hex
    signature = hmac.new(
        key.encode(),
        canonical_string(method, path, ts, non, body).encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        HEADER_TIMESTAMP: ts,
        HEADER_NONCE: non,
        HEADER_SIGNATURE: signature,
        HEADER_KEY_ID: "current",
    }


@dataclass(frozen=True, slots=True)
class VerifiedCaller:
    key_id: str
    nonce: str
    caller: str


class SignatureVerifier:
    def __init__(self, *, settings: Settings, nonce_repo: Any) -> None:
        self._settings = settings
        self._nonces = nonce_repo

    @property
    def enabled(self) -> bool:
        return bool(self._settings.internal_hmac_key)

    async def verify(
        self, *, method: str, path: str, body: bytes, headers: dict[str, str]
    ) -> VerifiedCaller:
        """Authenticate one internal call. Raises InternalAuthFailed."""
        settings = self._settings

        if not settings.internal_hmac_key:
            # Config validation makes this unreachable in production. Outside
            # production it is a loud, explicit development mode rather than a
            # silent bypass.
            if settings.is_production:  # pragma: no cover - defence in depth
                raise InternalAuthFailed("internal signing key is not configured")
            log.warning(
                "internal_auth.disabled",
                path=path,
                note="SPOT_INTERNAL_HMAC_KEY is unset; this is refused in "
                "production by config validation (LLD §12.1)",
            )
            return VerifiedCaller("unsigned", "", headers.get(HEADER_CALLER, "dev"))

        lowered = {k.lower(): v for k, v in headers.items()}
        timestamp = lowered.get(HEADER_TIMESTAMP)
        nonce = lowered.get(HEADER_NONCE)
        signature = lowered.get(HEADER_SIGNATURE)
        key_id = lowered.get(HEADER_KEY_ID, "current")

        if not (timestamp and nonce and signature):
            raise InternalAuthFailed(
                "signed request requires the "
                f"{HEADER_TIMESTAMP}, {HEADER_NONCE} and {HEADER_SIGNATURE} headers"
            )

        # -- 1. skew ---------------------------------------------------
        try:
            age = abs(time.time() - float(timestamp))
        except ValueError as exc:
            raise InternalAuthFailed("timestamp is not a unix time") from exc
        if age > settings.hmac_skew_seconds:
            raise InternalAuthFailed(
                f"timestamp is {age:.0f}s away from now, outside the "
                f"{settings.hmac_skew_seconds:.0f}s window"
            )

        # -- 3 & 4. signature over method, path, time, nonce and body ---
        canonical = canonical_string(method, path, timestamp, nonce, body)
        candidates = [("current", settings.internal_hmac_key)]
        if settings.internal_hmac_key_previous:
            candidates.append(("previous", settings.internal_hmac_key_previous))

        matched: str | None = None
        for name, key in candidates:
            expected = hmac.new(
                key.encode(), canonical.encode(), hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(expected, signature):
                matched = name
                break

        if matched is None:
            log.error(
                "internal_auth.signature_mismatch",
                path=path,
                key_id=key_id,
                caller=lowered.get(HEADER_CALLER),
                note="a bad signature on /internal is a security event, not a bug",
            )
            raise InternalAuthFailed("signature does not match")

        # -- 2. replay -------------------------------------------------
        if not await self._nonces.consume(
            nonce, settings.hmac_nonce_ttl, key_id=matched
        ):
            log.error(
                "internal_auth.replay_detected",
                path=path,
                nonce=nonce,
                caller=lowered.get(HEADER_CALLER),
                note="this exact signed request has already been executed",
            )
            raise InternalAuthFailed("nonce has already been used")

        if matched == "previous":
            log.info(
                "internal_auth.previous_key_used",
                path=path,
                note="key rotation in progress; retire the previous key once "
                "callers have migrated",
            )

        return VerifiedCaller(matched, nonce, lowered.get(HEADER_CALLER, "unknown"))

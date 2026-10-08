"""Operator sessions for the console.

The console exists because the alternative was worse. An operator dashboard that
can fire a reclaim order has to authorise that call somehow, and the two obvious
shortcuts both fail:

  * *Ship the HMAC key to the browser and sign in JavaScript.* The key that
    authorises `/internal/spot/reclaim` can terminate every spot lease in an AZ
    (LLD §12.1). Putting it in `localStorage` puts it one cross-site script,
    one shared laptop or one screen-share away from being someone else's.

  * *Leave `/console` unauthenticated because it is "just a dashboard".* It is
    not just a dashboard the moment it has a button that ends customer
    workloads — that is exactly the §12.1 gap with better typography.

So the browser gets a session and the server keeps the key. The session proves
*who is asking*; `routes_console` then signs the call itself and puts it through
the same `SignatureVerifier` every other internal caller uses, so the signed path
is genuinely exercised rather than bypassed for convenience.

The token itself is stateless — signed, not stored — for the same reason the
grace deadline is a column rather than a coroutine: a session table is another
thing to migrate, sweep and keep consistent across replicas, and nothing here
needs server-side revocation that rotating `SPOT_CONSOLE_TOKEN` does not already
give. The cost is honest and bounded: a session stays valid until it expires.

    cookie value = issued_at "." expires_at "." role "." HMAC-SHA256(secret, …)

The secret is derived from the console token, so rotating the token invalidates
every outstanding session at once — which is the revocation story.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response

from ..config import Settings
from ..domain.errors import Unauthenticated
from ..logging import get_logger

log = get_logger(__name__)

__all__ = [
    "COOKIE_NAME",
    "ConsoleSession",
    "issue_session",
    "read_session",
    "clear_session",
    "verify_token",
    "requires_token",
]

COOKIE_NAME = "spot_console"
ROLE_OPERATOR = "operator"


@dataclass(frozen=True, slots=True)
class ConsoleSession:
    role: str
    issued_at: float
    expires_at: float

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())


def requires_token(settings: Settings) -> bool:
    """Is a credential configured?

    When it is not, the console runs in an explicit development mode — the same
    shape as `SignatureVerifier` with no key, and refused in production by config
    validation rather than by a runtime check that could be reached with the flag
    set the wrong way.
    """
    return bool(settings.console_token)


def _secret(settings: Settings) -> bytes:
    """Derive the cookie-signing secret.

    Domain-separated from the internal HMAC key: signing two different kinds of
    thing with one key means a flaw in either one is a flaw in both. In dev with
    nothing configured the secret is per-process and random, so a session does
    not survive a restart — a lab restarting is not a security event, and a
    predictable fallback secret would be.
    """
    material = settings.console_token or settings.internal_hmac_key
    if not material:
        return _EPHEMERAL
    return hashlib.sha256(b"spotd/console/v1:" + material.encode()).digest()


#: Only ever used when neither a console token nor an internal key is set.
_EPHEMERAL = secrets.token_bytes(32)


def verify_token(settings: Settings, presented: str) -> bool:
    """Constant-time check of the operator token."""
    expected = settings.console_token
    if not expected:
        # Development mode: any non-empty token opens a session, and the fact
        # that it did is logged rather than assumed to be understood.
        log.warning(
            "console_auth.open",
            note="SPOT_CONSOLE_TOKEN is unset, so the operator console accepts "
            "any credential; config validation refuses this in production",
        )
        return bool(presented)
    return hmac.compare_digest(expected, presented)


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def issue_session(
    settings: Settings, response: Response, *, role: str = ROLE_OPERATOR
) -> ConsoleSession:
    now = time.time()
    session = ConsoleSession(
        role=role, issued_at=now, expires_at=now + settings.console_session_ttl
    )
    payload = f"{session.issued_at:.0f}.{session.expires_at:.0f}.{session.role}"
    value = f"{payload}.{_sign(_secret(settings), payload)}"

    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=int(settings.console_session_ttl),
        httponly=True,  # a session that JavaScript can read is a session XSS can steal
        secure=settings.console_cookie_secure,
        samesite="strict",  # no cross-site request can carry it into an action
        path="/",
    )
    return session


def clear_session(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.console_cookie_secure,
        samesite="strict",
    )


def read_session(settings: Settings, request: Request) -> ConsoleSession | None:
    """Return the caller's session, or None. Never raises on a malformed cookie."""
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 4:
        return None
    issued, expires, role, signature = parts
    payload = f"{issued}.{expires}.{role}"
    if not hmac.compare_digest(_sign(_secret(settings), payload), signature):
        log.warning(
            "console_auth.bad_signature",
            note="a forged or stale-secret console cookie was presented",
        )
        return None
    try:
        issued_at, expires_at = float(issued), float(expires)
    except ValueError:
        return None
    if expires_at <= time.time():
        return None
    return ConsoleSession(role=role, issued_at=issued_at, expires_at=expires_at)


def require_session(settings: Settings, request: Request) -> ConsoleSession:
    session = read_session(settings, request)
    if session is None:
        raise Unauthenticated(
            "the operator console requires a session; POST /console/session with "
            "the operator token first",
            details={"login": "POST /console/session", "cookie": COOKIE_NAME},
        )
    return session


def session_dependency(request: Request) -> ConsoleSession:
    """FastAPI dependency form — used with `dependencies=[...]` on the router."""
    settings: Settings = request.app.state.settings
    if not settings.console_enabled:
        raise Unauthenticated("the operator console is disabled on this deployment")
    return require_session(settings, request)


def describe(settings: Settings, session: ConsoleSession | None) -> dict[str, Any]:
    return {
        "authenticated": session is not None,
        "role": session.role if session else None,
        "expires_in_seconds": round(session.seconds_remaining) if session else None,
        "requires_token": requires_token(settings),
        "environment": settings.environment,
    }

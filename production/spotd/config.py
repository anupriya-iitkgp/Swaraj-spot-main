"""Configuration — every value is environment-driven and validated at startup.

LLD §13 lists the configuration reference and ends with a warning that the
relationship

    SPOT_FORCE_STOP_AT + SPOT_TEARDOWN_BUDGET < SPOT_GRACE_SECONDS

"must hold, or the advertised grace period is a fiction. This is not currently
validated at startup — worth adding as an assertion in config.py."

It is validated here, along with every other constraint that can be checked
before the process serves traffic. A misconfigured spot control plane does not
fail visibly — it quietly advertises a grace window it cannot honour — so the
only safe time to catch it is boot.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Literal

__all__ = ["Settings", "ConfigError", "load_settings", "get_settings"]

Environment = Literal["dev", "staging", "prod"]
Backend = Literal["sim", "live"]


class ConfigError(RuntimeError):
    """Raised at startup when configuration is internally inconsistent."""


# --------------------------------------------------------------------------
# primitive parsers — each reports the variable name so the failure is actionable
# --------------------------------------------------------------------------

def _env(name: str, default: str | None) -> str | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def _str(name: str, default: str) -> str:
    return _env(name, default) or default


def _opt_str(name: str) -> str | None:
    return _env(name, None)


def _int(name: str, default: int) -> int:
    raw = _env(name, None)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


def _float(name: str, default: float) -> float:
    raw = _env(name, None)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, None)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean")


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = _str(name, default)
    if value not in allowed:
        raise ConfigError(f"{name}={value!r} must be one of {', '.join(allowed)}")
    return value


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in _str(name, default).split(",") if p.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, fully validated runtime configuration."""

    # -- identity -----------------------------------------------------------
    environment: Environment = "dev"
    service_name: str = "spotd"
    worker_id: str = ""
    region: str = "in-mum-1"

    # -- database -----------------------------------------------------------
    database_url: str = "postgresql://spot@127.0.0.1:5432/spot"
    db_pool_min: int = 4
    db_pool_max: int = 32
    db_connect_timeout: float = 5.0
    db_command_timeout: float = 10.0
    # A statement that outruns this is a bug, not slowness: every query on the
    # request path is a single-row primary-key read or a conditional UPDATE.
    db_statement_timeout_ms: int = 5_000

    # -- grace and teardown timing (LLD §13) --------------------------------
    grace_seconds: float = 120.0
    force_stop_at: float = 95.0
    teardown_budget: float = 18.0

    # -- pool / feed --------------------------------------------------------
    control_cycle: float = 30.0
    cooldown: float = 180.0
    #: The forecast feed is an input, not a fact (HLD §12). When it is stale or
    #: low-confidence the pool degrades to this fraction rather than
    #: extrapolating the last known value.
    degraded_factor: float = 0.25
    #: Feed older than this many control cycles counts as stale.
    feed_stale_cycles: float = 2.0
    min_feed_confidence: float = 0.5

    # -- admission ----------------------------------------------------------
    idempotency_ttl: float = 86_400.0
    retry_after: int = 5
    tenant_quota: int = 64
    max_units_per_request: int = 256

    # -- victim selection ---------------------------------------------------
    blast_radius: float = 0.5

    # -- pricing ------------------------------------------------------------
    min_discount: float = 0.40
    max_discount: float = 0.80
    #: List price per vCPU-second, before the spot discount is applied.
    base_rate_per_unit_sec: float = 0.0000105

    # -- metering -----------------------------------------------------------
    metering_interval: float = 60.0

    # -- workers ------------------------------------------------------------
    reaper_interval: float = 5.0
    reaper_batch: int = 200
    reaper_claim_ttl: float = 30.0
    outbox_interval: float = 1.0
    outbox_batch: int = 200
    outbox_max_attempts: int = 12
    sweeper_interval: float = 300.0
    teardown_sweep_interval: float = 15.0
    analytics_interval: float = 900.0
    leader_lease_ttl: float = 45.0

    # -- rate limiting (LLD §12.9) ------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_burst: int = 40
    rate_limit_refill_per_sec: float = 8.0
    #: A tenant that ignores Retry-After is throttled harder than one that does.
    rate_limit_penalty_on_409: int = 4

    # -- internal auth (LLD §12.1) ------------------------------------------
    internal_hmac_key: str | None = None
    internal_hmac_key_previous: str | None = None
    hmac_skew_seconds: float = 300.0
    hmac_nonce_ttl: float = 900.0

    # -- external backends --------------------------------------------------
    backend: Backend = "sim"
    external_timeout: float = 3.0
    external_retries: int = 3
    external_backoff_base: float = 0.05
    external_backoff_max: float = 1.0
    breaker_failure_threshold: int = 5
    breaker_reset_timeout: float = 15.0
    account_service_url: str | None = None
    forecast_url: str | None = None
    ledger_url: str | None = None
    placement_url: str | None = None
    hypervisor_url: str | None = None
    billing_url: str | None = None

    # -- operator console ---------------------------------------------------
    #: The console is a first-party client of this service, served from the same
    #: origin. It never holds the signing key: privileged actions go through
    #: `/console/actions/*`, which authenticates the operator's session and then
    #: signs server-side (LLD §12.1 — the key that can terminate every lease in
    #: an AZ has no business in a browser).
    console_enabled: bool = True
    console_token: str | None = None
    console_session_ttl: float = 43_200.0
    #: Sent on the session cookie. Off outside production only because a lab is
    #: routinely driven over plain http on localhost.
    console_cookie_secure: bool = True

    # -- feature flags ------------------------------------------------------
    enable_sim_endpoints: bool = False
    enable_metrics: bool = True
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # -- inventory ----------------------------------------------------------
    availability_zones: tuple[str, ...] = ("az-1", "az-2", "az-3")

    # ----------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment == "prod"

    @property
    def teardown_deadline(self) -> float:
        """Seconds from notice to the point capacity must be back in the ledger."""
        return self.force_stop_at + self.teardown_budget

    @property
    def grace_headroom(self) -> float:
        """Slack between the advertised window and the worst-case internal path."""
        return self.grace_seconds - self.teardown_deadline

    def describe(self) -> dict[str, Any]:
        """Config as a dict, with secrets redacted. Safe to log or expose on /ops."""
        secret = {
            "internal_hmac_key",
            "internal_hmac_key_previous",
            "database_url",
            "console_token",
        }
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in secret and value:
                out[f.name] = _redact(str(value))
            else:
                out[f.name] = list(value) if isinstance(value, tuple) else value
        return out


def _redact(value: str) -> str:
    if "://" in value:  # a DSN — keep the shape, drop the credentials
        scheme, _, rest = value.partition("://")
        host = rest.split("@")[-1]
        return f"{scheme}://***@{host}"
    return f"***{value[-4:]}" if len(value) > 8 else "***"


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _validate(s: Settings) -> None:
    errors: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    # The assertion LLD §13 asks for, stated as the invariant it protects.
    check(
        s.force_stop_at + s.teardown_budget < s.grace_seconds,
        f"SPOT_FORCE_STOP_AT ({s.force_stop_at}) + SPOT_TEARDOWN_BUDGET "
        f"({s.teardown_budget}) must be < SPOT_GRACE_SECONDS ({s.grace_seconds}); "
        f"otherwise the advertised grace period is a fiction — capacity cannot be "
        f"back in the ledger by the deadline the customer was promised",
    )
    check(s.grace_seconds > 0, "SPOT_GRACE_SECONDS must be > 0")
    check(s.force_stop_at > 0, "SPOT_FORCE_STOP_AT must be > 0")
    check(s.teardown_budget > 0, "SPOT_TEARDOWN_BUDGET must be > 0")

    check(
        0.0 <= s.min_discount < s.max_discount <= 0.99,
        f"require 0 <= SPOT_MIN_DISCOUNT ({s.min_discount}) < SPOT_MAX_DISCOUNT "
        f"({s.max_discount}) <= 0.99",
    )
    check(
        0.0 < s.degraded_factor <= 1.0,
        f"SPOT_DEGRADED_FACTOR ({s.degraded_factor}) must be in (0, 1]",
    )
    check(
        0.0 < s.blast_radius <= 1.0,
        f"SPOT_BLAST_RADIUS ({s.blast_radius}) must be in (0, 1]",
    )
    check(
        0.0 <= s.min_feed_confidence <= 1.0,
        "SPOT_MIN_CONFIDENCE must be in [0, 1]",
    )
    check(s.control_cycle > 0, "SPOT_CONTROL_CYCLE must be > 0")
    check(s.cooldown >= 0, "SPOT_COOLDOWN must be >= 0")
    check(s.tenant_quota > 0, "SPOT_TENANT_QUOTA must be > 0")
    check(s.max_units_per_request > 0, "SPOT_MAX_UNITS_PER_REQUEST must be > 0")
    check(s.retry_after > 0, "SPOT_RETRY_AFTER must be > 0")
    check(s.db_pool_min >= 1, "SPOT_DB_POOL_MIN must be >= 1")
    check(
        s.db_pool_max >= s.db_pool_min,
        f"SPOT_DB_POOL_MAX ({s.db_pool_max}) must be >= SPOT_DB_POOL_MIN "
        f"({s.db_pool_min})",
    )
    check(bool(s.availability_zones), "SPOT_AVAILABILITY_ZONES must not be empty")

    # LLD §11: idempotency retention >= 24 h is a stated non-functional target,
    # not a tuning knob — a client retry after a network partition must not
    # double-allocate.
    check(
        s.idempotency_ttl >= 86_400 or not s.is_production,
        f"SPOT_IDEMPOTENCY_TTL ({s.idempotency_ttl}s) must be >= 86400 in "
        f"production (HLD §11: idempotency retention >= 24 h)",
    )

    # The reaper is the restart-safety net for in-grace leases (LLD §12.3). If
    # it runs less often than the grace window it cannot catch a stranded lease
    # before the customer notices.
    check(
        s.reaper_interval < s.grace_seconds,
        f"SPOT_REAPER_INTERVAL ({s.reaper_interval}s) must be < "
        f"SPOT_GRACE_SECONDS ({s.grace_seconds}s), or a restart can strand a "
        f"lease in NOTICE_ISSUED past its deadline",
    )
    check(
        s.reaper_claim_ttl > s.reaper_interval,
        f"SPOT_REAPER_CLAIM_TTL ({s.reaper_claim_ttl}s) must exceed "
        f"SPOT_REAPER_INTERVAL ({s.reaper_interval}s), or two workers will "
        f"reclaim the same lease concurrently",
    )

    # Production-only gates. Each of these is a documented gap that must be
    # closed before the service is exposed (LLD §12.1).
    if s.is_production:
        check(
            bool(s.internal_hmac_key),
            "SPOT_INTERNAL_HMAC_KEY is required in production: /internal/spot/"
            "reclaim can terminate every spot lease in an AZ (LLD §12.1)",
        )
        check(
            len(s.internal_hmac_key or "") >= 32,
            "SPOT_INTERNAL_HMAC_KEY must be at least 32 characters",
        )
        check(
            not s.enable_sim_endpoints,
            "SPOT_ENABLE_SIM must be false in production — /sim/* can fabricate "
            "forecast headroom and drive real reclaims (LLD §12.1)",
        )
        check(
            s.backend == "live",
            "SPOT_BACKEND must be 'live' in production; 'sim' serves synthetic "
            "capacity and would sell instances that do not exist",
        )
        check(
            s.log_format == "json",
            "SPOT_LOG_FORMAT must be 'json' in production for log ingestion",
        )
        # The console can fire reclaim orders. Unauthenticated, it is LLD §12.1
        # with a nicer interface — so in production it is either shut off or it
        # has a credential.
        check(
            not s.console_enabled or bool(s.console_token),
            "SPOT_CONSOLE_TOKEN is required in production when the operator "
            "console is enabled: /console/actions/* signs reclaim orders on the "
            "operator's behalf, and an unauthenticated console is the LLD §12.1 "
            "gap wearing a dashboard. Set the token or SPOT_CONSOLE_ENABLED=false",
        )
        check(
            len(s.console_token or "") >= 16 or not s.console_enabled,
            "SPOT_CONSOLE_TOKEN must be at least 16 characters",
        )
        check(
            s.console_cookie_secure or not s.console_enabled,
            "SPOT_CONSOLE_COOKIE_SECURE must stay true in production; the "
            "session cookie authorises reclaim orders",
        )

    if s.backend == "live":
        required = {
            "SPOT_ACCOUNT_SERVICE_URL": s.account_service_url,
            "SPOT_FORECAST_URL": s.forecast_url,
            "SPOT_LEDGER_URL": s.ledger_url,
            "SPOT_PLACEMENT_URL": s.placement_url,
            "SPOT_HYPERVISOR_URL": s.hypervisor_url,
            "SPOT_BILLING_URL": s.billing_url,
        }
        for name, value in required.items():
            check(bool(value), f"{name} is required when SPOT_BACKEND=live")

    if errors:
        joined = "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(f"invalid configuration ({len(errors)} problem(s)):\n{joined}")


def load_settings() -> Settings:
    """Read the environment, build Settings, and validate. Raises ConfigError."""
    env = _choice("SPOT_ENV", "dev", ("dev", "staging", "prod"))

    settings = Settings(
        environment=env,  # type: ignore[arg-type]
        service_name=_str("SPOT_SERVICE_NAME", "spotd"),
        worker_id=_str("SPOT_WORKER_ID", socket.gethostname() or "local"),
        region=_str("SPOT_REGION", "in-mum-1"),
        database_url=_str(
            "SPOT_DATABASE_URL", "postgresql://spot@127.0.0.1:5432/spot"
        ),
        db_pool_min=_int("SPOT_DB_POOL_MIN", 4),
        db_pool_max=_int("SPOT_DB_POOL_MAX", 32),
        db_connect_timeout=_float("SPOT_DB_CONNECT_TIMEOUT", 5.0),
        db_command_timeout=_float("SPOT_DB_COMMAND_TIMEOUT", 10.0),
        db_statement_timeout_ms=_int("SPOT_DB_STATEMENT_TIMEOUT_MS", 5_000),
        grace_seconds=_float("SPOT_GRACE_SECONDS", 120.0),
        force_stop_at=_float("SPOT_FORCE_STOP_AT", 95.0),
        teardown_budget=_float("SPOT_TEARDOWN_BUDGET", 18.0),
        control_cycle=_float("SPOT_CONTROL_CYCLE", 30.0),
        cooldown=_float("SPOT_COOLDOWN", 180.0),
        degraded_factor=_float("SPOT_DEGRADED_FACTOR", 0.25),
        feed_stale_cycles=_float("SPOT_FEED_STALE_CYCLES", 2.0),
        min_feed_confidence=_float("SPOT_MIN_CONFIDENCE", 0.5),
        idempotency_ttl=_float("SPOT_IDEMPOTENCY_TTL", 86_400.0),
        retry_after=_int("SPOT_RETRY_AFTER", 5),
        tenant_quota=_int("SPOT_TENANT_QUOTA", 64),
        max_units_per_request=_int("SPOT_MAX_UNITS_PER_REQUEST", 256),
        blast_radius=_float("SPOT_BLAST_RADIUS", 0.5),
        min_discount=_float("SPOT_MIN_DISCOUNT", 0.40),
        max_discount=_float("SPOT_MAX_DISCOUNT", 0.80),
        base_rate_per_unit_sec=_float("SPOT_BASE_RATE_PER_UNIT_SEC", 0.0000105),
        metering_interval=_float("SPOT_METERING_INTERVAL", 60.0),
        reaper_interval=_float("SPOT_REAPER_INTERVAL", 5.0),
        reaper_batch=_int("SPOT_REAPER_BATCH", 200),
        reaper_claim_ttl=_float("SPOT_REAPER_CLAIM_TTL", 30.0),
        outbox_interval=_float("SPOT_OUTBOX_INTERVAL", 1.0),
        outbox_batch=_int("SPOT_OUTBOX_BATCH", 200),
        outbox_max_attempts=_int("SPOT_OUTBOX_MAX_ATTEMPTS", 12),
        sweeper_interval=_float("SPOT_SWEEPER_INTERVAL", 300.0),
        teardown_sweep_interval=_float("SPOT_TEARDOWN_SWEEP_INTERVAL", 15.0),
        analytics_interval=_float("SPOT_ANALYTICS_INTERVAL", 900.0),
        leader_lease_ttl=_float("SPOT_LEADER_LEASE_TTL", 45.0),
        rate_limit_enabled=_bool("SPOT_RATE_LIMIT_ENABLED", True),
        rate_limit_burst=_int("SPOT_RATE_LIMIT_BURST", 40),
        rate_limit_refill_per_sec=_float("SPOT_RATE_LIMIT_REFILL", 8.0),
        rate_limit_penalty_on_409=_int("SPOT_RATE_LIMIT_PENALTY", 4),
        internal_hmac_key=_opt_str("SPOT_INTERNAL_HMAC_KEY"),
        internal_hmac_key_previous=_opt_str("SPOT_INTERNAL_HMAC_KEY_PREVIOUS"),
        hmac_skew_seconds=_float("SPOT_HMAC_SKEW_SECONDS", 300.0),
        hmac_nonce_ttl=_float("SPOT_HMAC_NONCE_TTL", 900.0),
        backend=_choice("SPOT_BACKEND", "sim", ("sim", "live")),  # type: ignore[arg-type]
        external_timeout=_float("SPOT_EXTERNAL_TIMEOUT", 3.0),
        external_retries=_int("SPOT_EXTERNAL_RETRIES", 3),
        external_backoff_base=_float("SPOT_EXTERNAL_BACKOFF_BASE", 0.05),
        external_backoff_max=_float("SPOT_EXTERNAL_BACKOFF_MAX", 1.0),
        breaker_failure_threshold=_int("SPOT_BREAKER_THRESHOLD", 5),
        breaker_reset_timeout=_float("SPOT_BREAKER_RESET", 15.0),
        account_service_url=_opt_str("SPOT_ACCOUNT_SERVICE_URL"),
        forecast_url=_opt_str("SPOT_FORECAST_URL"),
        ledger_url=_opt_str("SPOT_LEDGER_URL"),
        placement_url=_opt_str("SPOT_PLACEMENT_URL"),
        hypervisor_url=_opt_str("SPOT_HYPERVISOR_URL"),
        billing_url=_opt_str("SPOT_BILLING_URL"),
        console_enabled=_bool("SPOT_CONSOLE_ENABLED", True),
        console_token=_opt_str("SPOT_CONSOLE_TOKEN"),
        console_session_ttl=_float("SPOT_CONSOLE_SESSION_TTL", 43_200.0),
        console_cookie_secure=_bool("SPOT_CONSOLE_COOKIE_SECURE", env == "prod"),
        enable_sim_endpoints=_bool("SPOT_ENABLE_SIM", env != "prod"),
        enable_metrics=_bool("SPOT_ENABLE_METRICS", True),
        log_level=_str("SPOT_LOG_LEVEL", "INFO").upper(),
        log_format=_choice("SPOT_LOG_FORMAT", "json", ("json", "console")),  # type: ignore[arg-type]
        availability_zones=_csv("SPOT_AVAILABILITY_ZONES", "az-1,az-2,az-3"),
    )
    _validate(settings)
    return settings


_cached: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Process-wide settings singleton. `reload=True` re-reads the environment."""
    global _cached
    if _cached is None or reload:
        _cached = load_settings()
    return _cached

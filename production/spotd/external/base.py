"""Shared machinery for calling the out-of-scope services.

HLD §1 lists six things this subsystem consumes as interfaces rather than owns:
account classification, forecasting, the capacity ledger, the placement
scheduler, provisioning, and the core billing system. LLD §9 then gives each one
a contract *and* a "behaviour if it breaks" column, and those two columns are
what this module implements once so that six adapters do not implement them six
different ways.

Three properties, in order of how much they matter:

1.  **A timeout on every call.** The admission path has a 200 ms p99 target
    (HLD §11) and "a slow reject is worse than a fast one". A dependency with
    no timeout converts its own slowness into ours.

2.  **A circuit breaker.** Retrying into a service that is already failing turns
    a dependency outage into a self-inflicted load problem — the same dynamic
    HLD §12 describes for client retry storms, one layer down. Once the breaker
    is open, calls fail immediately and the caller applies its documented
    fallback rather than queueing behind a dead socket.

3.  **Retry only what is safe to retry.** Every operation named in LLD §9 is
    idempotent by contract, so retrying is safe — but only for errors that
    might succeed on a second attempt. A 4xx from placement means the request
    was wrong; repeating it just spends the deadline.
"""

from __future__ import annotations

import asyncio
import random
import time
from enum import Enum
from typing import Any, Awaitable, Callable, TypeVar

from ..config import Settings
from ..logging import get_logger
from ..metrics import M

log = get_logger(__name__)

T = TypeVar("T")

__all__ = [
    "ExternalError",
    "ExternalTimeout",
    "CircuitOpen",
    "CircuitBreaker",
    "BreakerState",
    "ExternalCaller",
]


class ExternalError(RuntimeError):
    """A call to an out-of-scope service failed."""

    def __init__(
        self, service: str, operation: str, message: str, *, retryable: bool = True
    ) -> None:
        super().__init__(f"{service}.{operation}: {message}")
        self.service = service
        self.operation = operation
        self.retryable = retryable


class ExternalTimeout(ExternalError):
    def __init__(self, service: str, operation: str, timeout: float) -> None:
        super().__init__(
            service, operation, f"timed out after {timeout:.2f}s", retryable=True
        )


class CircuitOpen(ExternalError):
    def __init__(self, service: str, operation: str, retry_in: float) -> None:
        super().__init__(
            service,
            operation,
            f"circuit is open, retry in {retry_in:.1f}s",
            retryable=False,
        )
        self.retry_in = retry_in


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-service breaker.

    Closed until `failure_threshold` consecutive failures, then open for
    `reset_timeout`, then half-open: exactly one probe is allowed through. A
    successful probe closes it; a failed probe re-opens it for another full
    interval. Letting a *single* probe through matters — releasing all queued
    callers at once is how a recovering service gets knocked over again.
    """

    __slots__ = ("_service", "_threshold", "_reset", "_failures", "_opened_at",
                 "_state", "_probe_in_flight", "_lock")

    def __init__(self, service: str, *, failure_threshold: int, reset_timeout: float) -> None:
        self._service = service
        self._threshold = failure_threshold
        self._reset = reset_timeout
        self._failures = 0
        self._opened_at = 0.0
        self._state = BreakerState.CLOSED
        self._probe_in_flight = False
        self._lock = asyncio.Lock()
        M.breaker_state.labels(service=service).set(0)

    @property
    def state(self) -> BreakerState:
        return self._state

    async def before(self, operation: str) -> None:
        """Raise CircuitOpen if this call should not be attempted."""
        async with self._lock:
            if self._state is BreakerState.OPEN:
                elapsed = time.monotonic() - self._opened_at
                if elapsed < self._reset:
                    raise CircuitOpen(self._service, operation, self._reset - elapsed)
                self._state = BreakerState.HALF_OPEN
                self._probe_in_flight = False
                M.breaker_state.labels(service=self._service).set(1)
                log.info("breaker.half_open", service=self._service)

            if self._state is BreakerState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpen(self._service, operation, self._reset)
                self._probe_in_flight = True

    async def on_success(self) -> None:
        async with self._lock:
            if self._state is not BreakerState.CLOSED:
                log.info("breaker.closed", service=self._service)
            self._failures = 0
            self._state = BreakerState.CLOSED
            self._probe_in_flight = False
            M.breaker_state.labels(service=self._service).set(0)

    async def on_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if self._state is BreakerState.HALF_OPEN or self._failures >= self._threshold:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
                M.breaker_state.labels(service=self._service).set(2)
                log.error(
                    "breaker.opened",
                    service=self._service,
                    consecutive_failures=self._failures,
                    reset_in=self._reset,
                    note="calls fail fast until the reset elapses; callers apply "
                    "their LLD §9 fallback",
                )


class ExternalCaller:
    """Wraps one external service with timeout, retry, breaker and metrics."""

    def __init__(self, service: str, settings: Settings) -> None:
        self.service = service
        self._settings = settings
        self._breaker = CircuitBreaker(
            service,
            failure_threshold=settings.breaker_failure_threshold,
            reset_timeout=settings.breaker_reset_timeout,
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    async def call(
        self,
        operation: str,
        fn: Callable[[], Awaitable[T]],
        *,
        timeout: float | None = None,
        retries: int | None = None,
        retry_on_timeout: bool = True,
    ) -> T:
        """Invoke `fn` under the full protection stack.

        `retries` counts *total* attempts, not extra ones, so `retries=1` means
        "try once". Backoff is exponential with full jitter — synchronised
        retries from many replicas are indistinguishable from a load spike to
        the service being retried.
        """
        attempts = retries if retries is not None else self._settings.external_retries
        budget = timeout if timeout is not None else self._settings.external_timeout
        last: BaseException | None = None

        for attempt in range(1, attempts + 1):
            await self._breaker.before(operation)
            started = time.perf_counter()
            try:
                async with asyncio.timeout(budget):
                    result = await fn()
            except asyncio.TimeoutError as exc:
                last = ExternalTimeout(self.service, operation, budget)
                await self._breaker.on_failure()
                self._observe(operation, "timeout", started)
                if not retry_on_timeout or attempt == attempts:
                    raise last from exc
            except ExternalError as exc:
                last = exc
                if exc.retryable:
                    await self._breaker.on_failure()
                else:
                    # A 4xx is the service telling us the request is wrong; that
                    # is not evidence the service is unhealthy, so it must not
                    # count towards opening the breaker.
                    await self._breaker.on_success()
                self._observe(operation, "error", started)
                if not exc.retryable or attempt == attempts:
                    raise
            except Exception as exc:  # noqa: BLE001 - normalised below
                last = ExternalError(self.service, operation, str(exc))
                await self._breaker.on_failure()
                self._observe(operation, "error", started)
                if attempt == attempts:
                    raise last from exc
            else:
                await self._breaker.on_success()
                self._observe(operation, "ok", started)
                return result

            delay = min(
                self._settings.external_backoff_max,
                self._settings.external_backoff_base * (2 ** (attempt - 1)),
            )
            jittered = random.uniform(0, delay)
            log.warning(
                "external.retry",
                service=self.service,
                operation=operation,
                attempt=attempt,
                of=attempts,
                delay=round(jittered, 4),
                error=str(last),
            )
            await asyncio.sleep(jittered)

        assert last is not None
        raise last

    def _observe(self, operation: str, outcome: str, started: float) -> None:
        M.external_call_duration.labels(
            service=self.service, operation=operation, outcome=outcome
        ).observe(time.perf_counter() - started)

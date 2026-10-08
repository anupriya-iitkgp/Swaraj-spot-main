"""Structured logging and request-scoped context.

LLD §14.2 sets two requirements that this module exists to make automatic:

  * "Every log line in the core carries lease_id, and reclaim-path lines also
    carry order_id."  — bound once via `lease_context()` / `order_context()`
    and carried through every downstream call by contextvars, so no component
    has to thread identifiers through its signatures to get them into the logs.

  * "Edge numbers are logged verbatim (edge 20  pool az-1: sellable -6) so a
    production log can be read against the wiring diagram."  — `edge()` emits
    exactly that, with the edge number as a structured field so you can also
    query it: `edge=20`.

The wiring diagram has 32 numbered edges (HLD §5). `EDGES` names them all, so a
log line can carry both the number and what the number means without the
operator needing the document open.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import uuid
from contextvars import ContextVar, Token
from typing import Any, Iterator

import structlog

__all__ = [
    "configure_logging",
    "get_logger",
    "edge",
    "lease_context",
    "order_context",
    "request_context",
    "current_trace_id",
    "EDGES",
]

# --------------------------------------------------------------------------
# HLD §5 connection table — every arrow on the wiring diagram.
# --------------------------------------------------------------------------
EDGES: dict[int, str] = {
    1: "Account Service -> API Gateway: account class",
    2: "Spot Customer -> API Gateway: launch request",
    3: "API Gateway -> Spot Market API: spot request",
    4: "Spot Market API -> Spot Customer: 201 lease / 409 + Retry-After",
    5: "Spot Market API <-> Eligibility & Quota Guard: validate",
    6: "Spot Market API <-> Spot Pool View: getSellable",
    7: "Spot Market API <-> Admission Controller: tryReserve",
    8: "Admission Controller -> Spot Lease Manager: createLease(ADMITTED)",
    9: "Spot Lease Manager -> Placement Adapter: place(SPOT, bin-pack)",
    10: "Placement Adapter <-> Placement Scheduler: host assignment",
    11: "Spot Lease Manager <-> Provisioning Adapter: provision",
    12: "Provisioning Adapter <-> Hypervisor: create/stop/destroy",
    13: "Provisioning Adapter -> Teardown Confirmer: stopped + cleanup done",
    14: "Teardown Confirmer -> Capacity Ledger: commitCapacityReturned",
    15: "Teardown Confirmer -> Spot Lease Manager: CLOSED",
    16: "Spot Lease Manager -> Notice Delivery: publishNotice(T-120s)",
    17: "Notice Delivery -> Spot Instance + tenant: metadata/webhook/event",
    18: "Forecast & Headroom -> Reclaim Order Handler: reclaim(N, group, deadline)",
    19: "Forecast & Headroom -> Spot Pool View: sellable spot per cycle",
    20: "Reclaim Order Handler -> Spot Pool View: shrink advertised pool",
    21: "Reclaim Order Handler -> Victim Selector: selectVictims",
    22: "Victim Selector -> Spot Lease Manager: victim lease set + deadline",
    23: "Spot Lease Manager <-> Grace Timer: start timer / expiry",
    24: "Grace Timer -> Provisioning Adapter: force stop",
    25: "Spot Lease Manager -> Spot Metering & Rating: usage, grace exclusion",
    26: "Spot Metering & Rating -> Billing System: rated records + credits",
    27: "Grace Timer -> Preemption Audit Log: notice/expiry/forced stop",
    28: "Spot Lease Manager -> Preemption Audit Log: lease transitions",
    29: "Preemption Audit Log -> Interruption Analytics: preemption history",
    30: "Interruption Analytics -> Spot Market API: published interruption rate",
    31: "Admission Controller <-> Spot Pool View: reserve / release",
    32: "Spot Lease Manager -> Spot Market API: lease state (describe)",
}

_trace_id: ContextVar[str | None] = ContextVar("spot_trace_id", default=None)
_lease_id: ContextVar[str | None] = ContextVar("spot_lease_id", default=None)
_order_id: ContextVar[str | None] = ContextVar("spot_order_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("spot_tenant_id", default=None)

_configured = False


def _context_processor(
    _logger: Any, _name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Inject the request-scoped identifiers into every event."""
    for key, var in (
        ("trace_id", _trace_id),
        ("lease_id", _lease_id),
        ("order_id", _order_id),
        ("tenant_id", _tenant_id),
    ):
        value = var.get()
        if value is not None and key not in event_dict:
            event_dict[key] = value
    return event_dict


def configure_logging(
    level: str = "INFO", fmt: str = "json", service: str = "spotd"
) -> None:
    """Idempotent logging setup. Safe to call from workers and from tests."""
    global _configured

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    # uvicorn installs its own handlers; route them through structlog's format
    # so a production log stream is homogeneous.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _context_processor,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if not _configured:  # tests and one-shot CLI commands
        configure_logging(fmt="console")
    return structlog.get_logger(name)


def edge(
    logger: structlog.stdlib.BoundLogger, number: int, message: str, **fields: Any
) -> None:
    """Log one traversal of a numbered edge from the HLD wiring diagram.

    Renders as `edge 20  pool az-1: sellable -6`, the format LLD §14.2
    specifies, while also emitting `edge=20` as a queryable field.
    """
    logger.info(
        f"edge {number}  {message}",
        edge=number,
        edge_name=EDGES.get(number, "unknown"),
        **fields,
    )


# --------------------------------------------------------------------------
# scoped binders
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _bind(var: ContextVar[str | None], value: str | None) -> Iterator[None]:
    token: Token[str | None] = var.set(value)
    try:
        yield
    finally:
        var.reset(token)


@contextlib.contextmanager
def lease_context(lease_id: str | None, tenant_id: str | None = None) -> Iterator[None]:
    """Bind lease_id (and optionally tenant_id) to every log line in the block."""
    with _bind(_lease_id, lease_id), _bind(_tenant_id, tenant_id or _tenant_id.get()):
        yield


@contextlib.contextmanager
def order_context(order_id: str | None) -> Iterator[None]:
    """Bind order_id — the reclaim path's trace root (LLD §14.2)."""
    with _bind(_order_id, order_id):
        yield


@contextlib.contextmanager
def request_context(
    trace_id: str | None = None, tenant_id: str | None = None
) -> Iterator[str]:
    """Open one API-call trace. Yields the trace id so it can be echoed back."""
    tid = trace_id or uuid.uuid4().hex
    with _bind(_trace_id, tid), _bind(_tenant_id, tenant_id):
        yield tid


def current_trace_id() -> str | None:
    return _trace_id.get()

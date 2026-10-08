"""The ASGI application.

Assembles the three routers, installs the exception handlers that turn typed
rejections into the status codes HLD §9 specifies, and owns the lifespan that
starts and stops the container.

One middleware behaviour is worth calling out: `Retry-After` is set from the
error object rather than left to each handler. HLD §12 makes enforcing it the
answer to retry storms, and a header that some paths remember to set is a header
clients cannot rely on.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import Settings, get_settings
from ..container import Container, build
from ..domain.errors import SpotError
from ..logging import configure_logging, get_logger, request_context
from .auth import SignatureVerifier
from . import routes_console, routes_internal, routes_ops, routes_public
from .static import mount_console

log = get_logger(__name__)

__all__ = ["create_app"]

_DESCRIPTION = """
Spot capacity for interruptible workloads.

**What you are buying.** Spare capacity at a discount, which can be taken back
when the guaranteed classes need it. You get a notice before that happens, and
the length of that notice is the product.

**Three things worth knowing before you build on this**

1. `GET /spot/inventory` is a projection refreshed each control cycle and is
   stale by design. A launch can still return `409` — that is normal on a busy
   pool, not an incident. Honour `Retry-After`.
2. Preemption always begins with a notice, delivered over three independent
   channels. If none of them reached you, the lease is credited automatically.
3. The interruption rate is published next to the price, per flavour and AZ.
   Size your workload against it.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    container = await build(settings)
    app.state.container = container
    app.state.verifier = SignatureVerifier(
        settings=settings, nonce_repo=container.nonce_repo
    )
    await container.start()
    try:
        yield
    finally:
        await container.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format, settings.service_name)

    app = FastAPI(
        title="ESDS Spot Market API",
        version=__version__,
        description=_DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "spot", "description": "The customer contract (HLD §6)."},
            {"name": "gateway", "description": "Classification and routing (edges 1-3)."},
            {
                "name": "internal",
                "description": "Signed control interface. Every route requires an "
                "HMAC signature over method, path, timestamp, nonce and body.",
            },
            {"name": "ops", "description": "Health, metrics and evidence."},
            {
                "name": "console",
                "description": "First-party UI back-end. Operator session in "
                "front, server-side request signing behind — the browser never "
                "holds the key that can terminate a lease (LLD §12.1).",
            },
        ],
    )
    app.state.settings = settings

    # ------------------------------------------------------------------
    # middleware
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def trace_and_time(request: Request, call_next: Any) -> Any:
        """One trace per API call, with lease/order ids bound by the handlers.

        LLD §14.2: "One trace span per API call, with child spans for validate,
        try_reserve, place, create." The trace id is echoed back so a customer
        reporting a problem can quote it.
        """
        incoming = request.headers.get("x-request-id") or request.headers.get(
            "traceparent"
        )
        tenant = request.headers.get("x-tenant-id")
        started = time.perf_counter()

        with request_context(incoming, tenant) as trace_id:
            response = await call_next(request)

        elapsed = (time.perf_counter() - started) * 1000
        response.headers["x-request-id"] = trace_id
        response.headers["x-response-time-ms"] = f"{elapsed:.2f}"
        if request.url.path not in {"/metrics", "/health/live", "/health/ready"}:
            log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round(elapsed, 2),
            )
        return response

    # ------------------------------------------------------------------
    # exception handlers — HLD §9's rejection table
    # ------------------------------------------------------------------
    @app.exception_handler(SpotError)
    async def spot_error_handler(request: Request, exc: SpotError) -> JSONResponse:
        headers: dict[str, str] = {}
        if exc.retry_after is not None:
            # Set centrally: HLD §12 makes Retry-After the mechanism that stops a
            # capacity shortage becoming an API flood, so it cannot be optional.
            headers["Retry-After"] = str(exc.retry_after)

        level = log.info if exc.status < 500 else log.error
        level(
            "http.rejected",
            path=request.url.path,
            status=exc.status,
            code=exc.code,
            message=exc.message,
        )
        return JSONResponse(
            status_code=exc.status, content=exc.to_payload(), headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "the request body or parameters are not valid",
                    "details": {"violations": exc.errors()},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # Never leak an internal message to a customer, but log it in full.
        log.exception(
            "http.unhandled_exception",
            path=request.url.path,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "an unexpected error occurred; the request id in "
                    "the x-request-id header identifies it in our logs",
                }
            },
        )

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    app.include_router(routes_public.router)
    app.include_router(routes_internal.router)
    app.include_router(routes_ops.router)

    if settings.console_enabled:
        app.include_router(routes_console.session_router)
        app.include_router(routes_console.router)
        if not settings.console_token:
            log.warning(
                "console.open",
                note="SPOT_CONSOLE_TOKEN is unset, so any credential opens an "
                "operator session; config validation refuses this in production",
            )

    if settings.enable_sim_endpoints:
        # LLD §12.1: "remove /sim from production builds via a feature flag".
        # Config validation refuses to let this be true in production, so the
        # flag cannot be flipped on by accident there.
        app.include_router(routes_internal.sim_router)
        log.warning(
            "sim_endpoints.enabled",
            note="/sim/* can fabricate forecast headroom and drive real "
            "reclaims; config validation refuses this in production (LLD §12.1)",
        )

    @app.get("/api", include_in_schema=False)
    async def api_root() -> dict[str, Any]:
        return {
            "service": settings.service_name,
            "version": __version__,
            "environment": settings.environment,
            "scope": "everything after a request is identified as spot: "
            "admission, lease, placement, preemption and rating (HLD §1)",
            "docs": "/docs",
            "customer_endpoints": [
                "GET /spot/inventory",
                "POST /spot/leases",
                "GET /spot/leases/{id}",
                "DELETE /spot/leases/{id}",
                "GET /spot/interruptions",
                "GET /spot/events",
            ],
            "console": "/" if settings.console_enabled else None,
        }

    # The UI is mounted last so that every API route above wins its path. A
    # single-page app with a catch-all is otherwise perfectly capable of
    # answering `/spot/leases` with an HTML document.
    mount_console(app, settings)

    if not settings.console_enabled:
        @app.get("/", include_in_schema=False)
        async def root() -> dict[str, Any]:
            return await api_root()

    return app


app = None  # populated by `uvicorn spotd.api.app:build_app` factory below


def build_app() -> FastAPI:
    """Factory for `uvicorn spotd.api.app:build_app --factory`."""
    return create_app()

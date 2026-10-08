"""Serving the single-page console from the service itself.

Same origin, no build step, no CDN. Each of those is a decision:

**Same origin.** The console authenticates with a `SameSite=strict` cookie, and
`strict` only works if the UI and the API are one origin. Splitting them would
mean either relaxing that to `lax`/`none` — reintroducing the cross-site request
the strict setting exists to prevent on an endpoint that fires reclaim orders —
or adding CORS with credentials, which is the same trade with more moving parts.

**No build step.** The console is native ES modules, served as written. There is
no bundler, no `node_modules`, and nothing generated, so the file on disk is the
file in the browser: what you review is what runs, and a stack trace points at a
real line. The cost is one HTTP request per module, which HTTP/2 and a warm
cache make uninteresting for an internal tool.

**No CDN.** A control plane that needs the public internet to render its own
incident dashboard has picked the wrong dependency for the wrong moment.

Caching is revalidate-always, not immutable. The tempting alternative — a long
`max-age` on content-addressed URLs — does not survive native ES modules: only
the entry point's URL can carry a build stamp, because the rest are reached
through `import` statements written in the source. Marking those immutable would
pin every module except the one that changed, and the failure mode is a browser
running half of yesterday's UI against today's API. `no-cache` still yields a 304
on the ETag, so the bytes only move when they actually differ.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import Settings
from ..logging import get_logger

log = get_logger(__name__)

__all__ = ["mount_console", "CONSOLE_DIR", "build_stamp"]

CONSOLE_DIR = Path(__file__).parent / "console"

#: Paths that belong to the API. A request under one of these must never fall
#: through to the SPA — answering a mistyped API call with 200 and an HTML
#: document turns a clear 404 into a client-side JSON parse error, three layers
#: from the mistake.
_API_PREFIXES = (
    "spot/",
    "v1/",
    "internal/",
    "sim/",
    "ops/",
    "console/",
    "health",
    "metrics",
    "docs",
    "redoc",
    "openapi.json",
    "api",
)


def build_stamp() -> str:
    """A short digest over the console sources.

    Changes when any file changes, so cache-busting follows the code rather
    than a version number someone has to remember to bump.
    """
    digest = hashlib.sha256()
    if CONSOLE_DIR.is_dir():
        for path in sorted(CONSOLE_DIR.rglob("*")):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


class _RevalidatingStatic(StaticFiles):
    """Static files the browser must revalidate, answered 304 when unchanged."""

    async def get_response(self, path: str, scope) -> Response:  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "no-cache"
        return response


def mount_console(app: FastAPI, settings: Settings) -> None:
    if not settings.console_enabled:
        return
    if not CONSOLE_DIR.is_dir():  # pragma: no cover - packaging accident
        log.error(
            "console.assets_missing",
            path=str(CONSOLE_DIR),
            note="the API is unaffected; only the UI is unavailable",
        )
        return

    stamp = build_stamp()
    app.state.console_stamp = stamp
    index = CONSOLE_DIR / "index.html"

    app.mount(
        "/console-assets",
        _RevalidatingStatic(directory=str(CONSOLE_DIR)),
        name="console-assets",
    )

    def _index() -> HTMLResponse:
        html = index.read_text(encoding="utf-8").replace("__BUILD__", stamp)
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store",
                # The console loads nothing it did not ship with, so the policy
                # can say exactly that. An XSS in a page that can fire reclaim
                # orders is worth spending a header on.
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; "
                    "form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
                ),
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
            },
        )

    @app.get("/", include_in_schema=False)
    async def console_index() -> HTMLResponse:
        return _index()

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(CONSOLE_DIR / "favicon.svg")

    @app.get("/{path:path}", include_in_schema=False)
    async def console_fallback(path: str, request: Request) -> Response:
        """Client-side routes resolve to the app; API paths keep their 404."""
        if path.startswith(_API_PREFIXES):
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"no route {request.method} /{path}",
                    }
                },
            )
        return _index()

    log.info("console.mounted", stamp=stamp, path=str(CONSOLE_DIR))

"""`spotd` — operator command line.

Deliberately small. Each command is something an operator or an on-call engineer
actually needs, and each one is safe to run against production except where it
says otherwise:

    spotd migrate            apply schema migrations (advisory-locked)
    spotd seed               load the synthetic dataset (dev and staging only)
    spotd serve              run the API and the workers
    spotd worker             run only the workers, no HTTP listener
    spotd audit-verify       recompute the audit hash chain
    spotd reconcile          recompute reserved_units from the lease table
    spotd sign               produce headers for a signed /internal call
    spotd reclaim            issue a signed reclaim order
    spotd config             print the effective configuration, secrets redacted
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from typing import Any

from .config import ConfigError, get_settings
from .logging import configure_logging, get_logger

log = get_logger(__name__)

__all__ = ["main"]


def _json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, default=str))


# ----------------------------------------------------------------------
async def _with_db(fn: Any) -> Any:
    from .db import connect

    settings = get_settings()
    db = await connect(settings)
    try:
        return await fn(db, settings)
    finally:
        await db.close()


def cmd_migrate(args: argparse.Namespace) -> int:
    """Run Alembic. Kept as a subprocess so the exact command is reproducible."""
    cmd = [sys.executable, "-m", "alembic", "upgrade", args.revision]
    if args.sql:
        cmd.append("--sql")
    return subprocess.call(cmd, cwd=os.path.dirname(os.path.dirname(__file__)) or ".")


def cmd_seed(args: argparse.Namespace) -> int:
    from .seed import seed

    settings = get_settings()
    if settings.is_production and not args.force:
        print(
            "refusing to seed synthetic tenants and host groups into production.\n"
            "This would create fake accounts with real quota. Pass --force only "
            "if you are certain SPOT_ENV is wrong.",
            file=sys.stderr,
        )
        return 2

    async def run(db: Any, _settings: Any) -> dict[str, Any]:
        return await seed(db, with_trace=not args.no_trace, days=args.days)

    _json(asyncio.run(_with_db(run)))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "spotd.api.app:build_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=1,  # the container owns background workers; see `spotd worker`
        log_config=None,
        access_log=False,
        reload=args.reload,
    )
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    """Run the background loops without an HTTP listener.

    Useful when workers are scaled independently of the API — the reaper and the
    outbox relay are throughput-bound, the API is request-bound, and coupling
    their replica counts wastes one or starves the other.
    """
    from .container import build

    async def run() -> None:
        container = await build()
        await container.start()
        log.info("worker.mode", workers=[w.name for w in container.workers])
        stop = asyncio.Event()

        loop = asyncio.get_running_loop()
        for signame in ("SIGINT", "SIGTERM"):
            try:
                import signal

                loop.add_signal_handler(getattr(signal, signame), stop.set)
            except (ImportError, NotImplementedError):  # pragma: no cover
                pass
        try:
            await stop.wait()
        finally:
            await container.stop()

    asyncio.run(run())
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    from .db.repositories import AuditRepository

    async def run(db: Any, _settings: Any) -> dict[str, Any]:
        result = await AuditRepository(db).verify()
        return {
            "entries": result.entries,
            "valid": result.valid,
            "broken_at_id": result.broken_at,
            "detail": result.detail,
        }

    outcome = asyncio.run(_with_db(run))
    _json(outcome)
    return 0 if outcome["valid"] else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    from .db.repositories import PoolRepository

    async def run(db: Any, _settings: Any) -> dict[str, Any]:
        results = await PoolRepository(db).reconcile()
        return {
            "over_allocation_detected": any(r.over_allocated for r in results),
            "per_az": [
                {
                    "az": r.az,
                    "pool_counter": r.counter,
                    "sum_of_active_lease_units": r.actual,
                    "drift": r.drift,
                    "over_allocated": r.over_allocated,
                }
                for r in results
            ],
        }

    outcome = asyncio.run(_with_db(run))
    _json(outcome)
    # Non-zero exit so this can be a cron check that pages.
    return 1 if outcome["over_allocation_detected"] else 0


def cmd_sign(args: argparse.Namespace) -> int:
    from .api.auth import sign_request

    settings = get_settings()
    if not settings.internal_hmac_key:
        print("SPOT_INTERNAL_HMAC_KEY is not set", file=sys.stderr)
        return 2
    body = (args.body or "").encode()
    headers = sign_request(
        key=settings.internal_hmac_key, method=args.method, path=args.path, body=body
    )
    if args.curl:
        header_args = " ".join(f"-H '{k}: {v}'" for k, v in headers.items())
        print(
            f"curl -X {args.method} {header_args} "
            f"-H 'content-type: application/json' "
            f"-d '{args.body or ''}' http://localhost:8000{args.path}"
        )
    else:
        _json(headers)
    return 0


def cmd_reclaim(args: argparse.Namespace) -> int:
    """Issue a signed reclaim order against a running service."""
    import httpx
    import orjson

    from .api.auth import sign_request

    settings = get_settings()
    if not settings.internal_hmac_key:
        print("SPOT_INTERNAL_HMAC_KEY is not set", file=sys.stderr)
        return 2

    path = "/internal/spot/reclaim"
    payload = {
        "order_id": args.order_id,
        "az": args.az,
        "units": args.units,
        "host_group": args.host_group,
        "deadline_seconds": args.deadline,
        "reason": args.reason,
    }
    body = orjson.dumps(payload)
    headers = sign_request(
        key=settings.internal_hmac_key, method="POST", path=path, body=body
    )
    headers["content-type"] = "application/json"

    response = httpx.post(
        f"{args.base_url.rstrip('/')}{path}", content=body, headers=headers, timeout=30
    )
    _json(response.json())
    return 0 if response.status_code < 400 else 1


def cmd_config(args: argparse.Namespace) -> int:
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _json(
        {
            "settings": settings.describe(),
            "derived": {
                "teardown_deadline_seconds": settings.teardown_deadline,
                "grace_headroom_seconds": settings.grace_headroom,
            },
        }
    )
    return 0


# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spotd", description="ESDS spot control plane"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="apply schema migrations")
    p.add_argument("--revision", default="head")
    p.add_argument("--sql", action="store_true", help="print SQL instead of applying")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("seed", help="load the synthetic dataset")
    p.add_argument("--days", type=int, default=14, help="days of forecast trace")
    p.add_argument("--no-trace", action="store_true")
    p.add_argument("--force", action="store_true", help="allow seeding in production")
    p.set_defaults(fn=cmd_seed)

    p = sub.add_parser("serve", help="run the API and the workers")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("worker", help="run only the background workers")
    p.set_defaults(fn=cmd_worker)

    p = sub.add_parser("audit-verify", help="recompute the audit hash chain")
    p.set_defaults(fn=cmd_audit_verify)

    p = sub.add_parser("reconcile", help="check for over-allocation")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("sign", help="produce signed headers for an /internal call")
    p.add_argument("--method", default="POST")
    p.add_argument("--path", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--curl", action="store_true", help="print a ready-to-run curl")
    p.set_defaults(fn=cmd_sign)

    p = sub.add_parser("reclaim", help="issue a signed reclaim order")
    p.add_argument("--order-id", required=True)
    p.add_argument("--az", required=True)
    p.add_argument("--units", type=int, required=True)
    p.add_argument("--host-group", default=None)
    p.add_argument("--deadline", type=float, default=120.0)
    p.add_argument("--reason", default="manual")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.set_defaults(fn=cmd_reclaim)

    p = sub.add_parser("config", help="print the effective configuration")
    p.set_defaults(fn=cmd_config)

    args = parser.parse_args(argv)
    configure_logging(
        os.environ.get("SPOT_LOG_LEVEL", "INFO"),
        os.environ.get("SPOT_LOG_FORMAT", "console"),
    )
    try:
        return int(args.fn(args) or 0)
    except ConfigError as exc:
        print(f"configuration error:\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

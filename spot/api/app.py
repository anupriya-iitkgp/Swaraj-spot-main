"""HTTP shell.

`/v1/instances` is the API Gateway (edges 1-3): it authenticates, looks up the
account class and routes. Only the SPOT branch continues into this subsystem;
STATIC and DYNAMIC return 501 because those services are out of scope.

`/spot/*` is the Spot Market API itself.
`/internal/spot/reclaim` is the only inbound control interface (edge 18).
`/sim/*` drives the external stubs so you can trigger a reclaim by hand.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import CONFIG
from ..container import Container
from ..domain.errors import NoCapacity, SpotError
from ..domain.models import FLAVOURS, AccountClass, Flavour
from .schemas import (FlavourRequest, HeadroomRequest, LaunchRequest,
                      PricingRequest, RateRequest, ReclaimRequest, WebhookRequest)

log = logging.getLogger("spot.api")

container = Container()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await container.start()
    yield
    await container.stop()


app = FastAPI(
    title="ESDS Spot Capacity Subsystem",
    version="0.1.0",
    description="Reference implementation of the spot customer request-handling HLD.",
    lifespan=lifespan,
)


@app.exception_handler(SpotError)
async def spot_error_handler(request: Request, exc: SpotError):
    headers = {}
    if isinstance(exc, NoCapacity):
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=headers)


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def record_metrics(request: Request, call_next):
    """Times every call and records it, so the dashboard can show the API
    traffic it is generating rather than just its results."""
    t0 = time.perf_counter()
    response = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    path = request.url.path
    # everything the dashboard polls is excluded, or its own traffic would
    # dominate the very log and latency histogram it displays
    if not path.startswith(("/static", "/ops/overview", "/ops/timeseries",
                            "/ops/trends", "/ops/audit", "/ops/demand",
                            "/ops/host", "/favicon")):
        container.metrics.observe_request(request.method, path, response.status_code, ms)
    return response


@app.get("/", include_in_schema=False)
async def dashboard():
    # always revalidate: a stale cached console must never outlive a deploy
    return FileResponse(STATIC_DIR / "dashboard.html",
                        headers={"Cache-Control": "no-cache"})


def _tenant(x_tenant_id: str | None) -> str:
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="X-Tenant-Id header required")
    return x_tenant_id


# ===========================================================================
# API Gateway — edges 1, 2, 3
# ===========================================================================
@app.post("/v1/instances", tags=["gateway"])
async def gateway_launch(
    body: LaunchRequest,
    response: Response,
    x_tenant_id: str = Header(default=None),
    idempotency_key: str = Header(default=None),
):
    """Edge 2 in, edge 1 for the class lookup, edge 3 to route SPOT onward."""
    tenant = _tenant(x_tenant_id)
    account_class = await container.accounts.get_account_class(tenant)  # edge 1
    if account_class is None:
        raise HTTPException(status_code=403, detail=f"unknown tenant {tenant}")

    if account_class is not AccountClass.SPOT:
        # Out of scope for this project — shown in the HLD as a greyed branch.
        raise HTTPException(
            status_code=501,
            detail={
                "message": f"{account_class.value} requests are handled by the "
                           f"{'reserved-capacity' if account_class is AccountClass.STATIC else 'pay-per-use'} service",
                "out_of_scope": True,
            },
        )

    return await _launch(tenant, body, idempotency_key, response)


# ===========================================================================
# Spot Market API — edges 4, 5, 6, 7, 30, 32
# ===========================================================================
@app.get("/spot/inventory", tags=["spot-market-api"])
async def inventory(flavour: str | None = None, az: str | None = None):
    """Published offer. A targeted query (?flavour=…) is a demand signal."""
    if flavour:
        container.demand.record(flavour, az or "any", "availability_check")
    return container.market.inventory()


@app.post("/spot/leases", status_code=201, tags=["spot-market-api"])
async def launch(
    body: LaunchRequest,
    response: Response,
    x_tenant_id: str = Header(default=None),
    idempotency_key: str = Header(default=None),
):
    return await _launch(_tenant(x_tenant_id), body, idempotency_key, response)


async def _launch(tenant: str, body: LaunchRequest, idem: str | None,
                  response: Response | None = None):
    # every attempt is demand — a rejection is still a customer who wanted one
    container.demand.record(body.flavour, body.az, "launch_attempt")
    lease, replayed = await container.market.launch(
        tenant_id=tenant,
        flavour_name=body.flavour,
        count=body.count,
        az=body.az,
        idempotency_key=idem,
        drain_seconds=body.drain_seconds,
        persist=body.persist,
    )
    payload = lease.to_dict()
    payload["idempotent_replay"] = replayed
    payload["notice_channels"] = {
        "metadata": f"/metadata/spot/termination-time/{{instance_id}}",
        "event_stream": "/spot/events?topic=spot.preempt.notice",
        "webhook": f"register via POST /sim/webhook, read back at /sim/webhook/{tenant}",
    }
    if response is not None:
        # A replay is not a creation, so it must not claim 201. Set both sides
        # explicitly: the gateway route has no status_code of its own.
        response.status_code = 200 if replayed else 201
    return payload


@app.get("/spot/leases", tags=["spot-market-api"])
async def list_leases(x_tenant_id: str = Header(default=None)):
    return container.market.list_leases(x_tenant_id)


@app.get("/spot/leases/{lease_id}", tags=["spot-market-api"])
async def describe(lease_id: str):
    return container.market.describe(lease_id)


@app.delete("/spot/leases/{lease_id}", tags=["spot-market-api"])
async def release(lease_id: str):
    return await container.market.release(lease_id)


@app.get("/metadata/spot/termination-time/{instance_id}", tags=["spot-market-api"])
async def termination_time(instance_id: str):
    """Notice channel 1 — what the guest polls (edge 17)."""
    notice = container.notice_delivery.termination_time(instance_id)
    if notice is None:
        return {"instance_id": instance_id, "terminate_after": None}
    return notice


@app.get("/spot/saved", tags=["spot-market-api"])
async def saved_tasks(x_tenant_id: str = Header(default=None)):
    """Stateful spot: hibernated tasks waiting to be resumed."""
    return container.saved.list(x_tenant_id)


@app.post("/spot/saved/{saved_id}/resume", status_code=201, tags=["spot-market-api"])
async def resume_saved(saved_id: str, x_tenant_id: str = Header(default=None)):
    """Resume a hibernated task: full admission at today's price, then the
    saved machines start exactly where they stopped."""
    tenant = _tenant(x_tenant_id)
    task = container.saved.get(saved_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"unknown saved task {saved_id}")
    if task["tenant_id"] != tenant:
        raise HTTPException(status_code=403, detail="not your saved task")
    lease, _ = await container.market.launch(
        tenant_id=tenant, flavour_name=task["flavour"], count=task["count"],
        az=task["az"], idempotency_key=None, drain_seconds=2.0,
        persist=True, resume_vmids=task["vmids"],
    )
    container.saved.pop(saved_id)
    container.audit.append("task_resumed", saved_id=saved_id,
                           lease_id=lease.lease_id, vmids=task["vmids"])
    out = lease.to_dict()
    out["resumed_from"] = saved_id
    return out


@app.get("/spot/events", tags=["spot-market-api"])
async def events(since: int = 0, topic: str = ""):
    """Notice channel 3 — the tenant event stream."""
    return container.bus.recent(since=since, topic_prefix=topic)


# ===========================================================================
# Internal control interface — edge 18
# ===========================================================================
@app.post("/internal/spot/reclaim", tags=["internal"])
async def reclaim(body: ReclaimRequest):
    return await container.reclaim_handler.handle(
        units=body.units, az=body.az, host_group=body.host_group,
        deadline=body.deadline, reason=body.reason,
    )


# ===========================================================================
# Observability
# ===========================================================================
_HOST_CACHE: dict = {"ts": 0.0, "data": None}


@app.get("/ops/host", tags=["ops"])
async def host_live():
    """The physical machine, live: real telemetry and every guest on it.

    Proxmox mode only; cached for 5 s so ten dashboards cost one API call."""
    if container.backend.get("mode") != "proxmox":
        return {"live": False, "detail": "sim backend — no physical host attached"}
    now = time.time()
    if now - _HOST_CACHE["ts"] < 5 and _HOST_CACHE["data"]:
        return _HOST_CACHE["data"]
    try:
        c = container.hypervisor._c()
        node = CONFIG.proxmox_node
        st = (await c.get(f"/nodes/{node}/status")).json()["data"]
        qemu = (await c.get(f"/nodes/{node}/qemu")).json()["data"]
        lxc = (await c.get(f"/nodes/{node}/lxc")).json()["data"]
        guests = []
        for g in sorted(qemu + lxc, key=lambda x: int(x["vmid"])):
            tags = str(g.get("tags") or "")
            guests.append({
                "vmid": int(g["vmid"]),
                "name": g.get("name", "?"),
                "type": "vm" if "cpus" in g and g in qemu else ("vm" if g in qemu else "ct"),
                "status": g.get("status"),
                "cores": int(g.get("cpus") or 0),
                "mem_mb": round((g.get("maxmem") or 0) / 2**20),
                "uptime": g.get("uptime") or 0,
                "spot": "spot" in tags,
                "template": bool(g.get("template")),
            })
        data = {
            "live": True,
            "node": node,
            "pve_version": st.get("pveversion", ""),
            "kernel": (st.get("current-kernel") or {}).get("release", ""),
            "uptime": st.get("uptime", 0),
            "loadavg": st.get("loadavg", []),
            "cpu_pct": round(100 * (st.get("cpu") or 0), 1),
            "cores": st["cpuinfo"]["cpus"],
            "cpu_model": st["cpuinfo"].get("model", ""),
            "mem_used_gb": round(st["memory"]["used"] / 2**30, 1),
            "mem_total_gb": round(st["memory"]["total"] / 2**30, 1),
            "rootfs_used_gb": round(st.get("rootfs", {}).get("used", 0) / 2**30, 1),
            "rootfs_total_gb": round(st.get("rootfs", {}).get("total", 0) / 2**30, 1),
            "guests": guests,
        }
        _HOST_CACHE.update(ts=now, data=data)
        return data
    except Exception as e:
        return {"live": False, "detail": f"host telemetry unavailable: {e}"}


@app.get("/ops/pools", tags=["ops"])
async def pools():
    return container.pool.snapshot()


@app.get("/ops/ledger", tags=["ops"])
async def ledger():
    return container.ledger.snapshot()


@app.get("/ops/trends", tags=["ops"])
async def trends(range: str = "month", start: str | None = None, end: str | None = None):
    """Allocation history + forecast: premium vs spot clients vs idle.

    range = week | month | year, or custom with start/end as YYYY-MM-DD
    (an end date in the future extends the forecast to it).
    """
    from datetime import datetime

    now = time.time()
    day = 86400.0
    if range == "week":
        s, e = now - 7 * day, now + 2 * day
    elif range == "year":
        s, e = now - 365 * day, now + 60 * day
    elif range == "custom":
        try:
            s = datetime.fromisoformat(start).timestamp()
            e = datetime.fromisoformat(end).timestamp() + day  # inclusive end date
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail="custom range needs start and end as YYYY-MM-DD")
        if e <= s:
            raise HTTPException(status_code=400, detail="end must be after start")
        if e - s > 3 * 365 * day:
            raise HTTPException(status_code=400, detail="range is capped at 3 years")
    else:  # month
        s, e = now - 30 * day, now + 7 * day
    return container.trends.series(s, e, now)


@app.get("/ops/demand", tags=["ops"])
async def demand(range: str = "today", start: str | None = None, end: str | None = None):
    """Demand for spot instances: availability checks + launch attempts
    per node type, over today | week | month | year | custom."""
    from datetime import datetime

    now = time.time()
    day = 86400.0
    spans = {"today": day, "week": 7*day, "month": 30*day, "year": 365*day}
    if range in spans:
        s, e = now - spans[range], now
    elif range == "custom":
        try:
            s = datetime.fromisoformat(start).timestamp()
            e = datetime.fromisoformat(end).timestamp() + day
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail="custom range needs start and end as YYYY-MM-DD")
        if e <= s:
            raise HTTPException(status_code=400, detail="end must be after start")
    else:
        raise HTTPException(status_code=400, detail=f"unknown range {range}")
    if start is None and end is None:              # hot path for many viewers
        return _ttl(("demand", range), 2.0,
                    lambda: container.demand.summary(s, min(e, now), list(FLAVOURS), now))
    return container.demand.summary(s, min(e, now), list(FLAVOURS), now)


@app.get("/ops/pricing", tags=["ops"])
async def pricing_status():
    return container.pricing.status()


def _flavour_dict(f: Flavour) -> dict:
    return {"name": f.name, "vcpu": f.vcpu, "ram_gb": f.ram_gb, "disk_gb": f.disk_gb,
            "rate_per_hour": f.on_demand_rate_per_hour, "spot_eligible": f.spot_eligible}


@app.get("/ops/rates", tags=["ops"])
async def rate_card():
    return [_flavour_dict(f) for f in FLAVOURS.values()]


@app.post("/ops/rates", tags=["ops"])
async def set_rate(body: RateRequest):
    """Re-price one node type. Running leases keep their admission snapshot."""
    f = FLAVOURS.get(body.flavour)
    if f is None:
        raise HTTPException(status_code=404, detail=f"unknown flavour {body.flavour}")
    old = f.on_demand_rate_per_hour
    FLAVOURS[body.flavour] = replace(f, on_demand_rate_per_hour=body.rate_per_hour)
    container.audit.append("rate_changed", flavour=body.flavour,
                           old_rate_per_hour=old, new_rate_per_hour=body.rate_per_hour)
    return _flavour_dict(FLAVOURS[body.flavour])


@app.post("/ops/flavours", status_code=201, tags=["ops"])
async def add_flavour(body: FlavourRequest):
    """Publish a new node type. It appears in inventory on the next refresh."""
    if body.name in FLAVOURS:
        raise HTTPException(status_code=409, detail=f"flavour {body.name} already exists")
    FLAVOURS[body.name] = Flavour(body.name, body.vcpu, body.ram_gb,
                                  body.rate_per_hour, spot_eligible=body.spot_eligible,
                                  disk_gb=body.disk_gb)
    container.audit.append("flavour_added", flavour=body.name, vcpu=body.vcpu,
                           ram_gb=body.ram_gb, disk_gb=body.disk_gb,
                           rate_per_hour=body.rate_per_hour,
                           spot_eligible=body.spot_eligible)
    return _flavour_dict(FLAVOURS[body.name])


@app.post("/ops/pricing", tags=["ops"])
async def pricing_set(body: PricingRequest):
    """Operator pricing control: pin the discount for a window, or revert.

    A manual pin always expires on its own, so pricing cannot be left stuck
    on a human decision; the audit log records who-asked-for-what either way.
    """
    if body.mode == "manual":
        if body.discount is None or body.duration_seconds is None:
            raise HTTPException(status_code=400,
                                detail="manual mode requires discount and duration_seconds")
        ov = container.pricing.set_manual(body.discount, body.duration_seconds, body.az)
        container.audit.append("pricing_override_set", discount=ov["discount"],
                               az=ov["az"] or "all", duration_seconds=ov["duration_seconds"])
    else:
        container.pricing.set_auto()
        container.audit.append("pricing_override_cleared")
    return container.pricing.status()


_TTL_CACHE: dict = {}


def _ttl(key, ttl: float, build):
    """100 dashboards polling at 1 Hz must cost ~1 build AND ~1 JSON
    serialisation per interval, not 100 of each: the cache stores the
    serialised bytes and every request after the first is a memcpy."""
    import json as _json
    now = time.monotonic()
    hit = _TTL_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return Response(content=hit[1], media_type="application/json")
    body = _json.dumps(build(), separators=(",", ":")).encode()
    _TTL_CACHE[key] = (now, body)
    return Response(content=body, media_type="application/json")


@app.get("/ops/audit", tags=["ops"])
async def audit(lease_id: str | None = None, kind: str | None = None):
    if lease_id is None and kind is None:          # the dashboard's hot path
        return _ttl("audit", 1.0,
                    lambda: container.audit.entries(lease_id=None, kind=None))
    return container.audit.entries(lease_id=lease_id, kind=kind)


@app.get("/ops/interruption-rates", tags=["ops"])
async def interruption_rates():
    return {
        "by_flavour_az": container.analytics.rate_by_flavour_az(),
        "by_tenant": container.analytics.rate_by_tenant(),
    }


@app.get("/ops/billing/{tenant_id}", tags=["ops"])
async def invoice(tenant_id: str):
    return container.billing.invoice_for(tenant_id)


@app.get("/ops/slo", tags=["ops"])
async def slo():
    """Reclaim SLO: notice -> capacity actually returned, per lease."""
    rows = []
    for lease in container.lease_manager.all():
        if lease.notice_at is None or lease.closed_at is None:
            continue
        rows.append(
            {
                "lease_id": lease.lease_id,
                "notice_to_closed_seconds": round(lease.closed_at - lease.notice_at, 3),
                "within_budget": (lease.closed_at - lease.notice_at) <= CONFIG.grace_seconds,
                "forced_stop": lease.forced_stop,
            }
        )
    met = [r for r in rows if r["within_budget"]]
    return {
        "reclaims_measured": len(rows),
        "within_budget": len(met),
        "attainment": round(len(met) / len(rows), 4) if rows else None,
        "stalled_teardowns": container.teardown_confirmer.stalled,
        "detail": rows,
    }


@app.get("/ops/metrics", tags=["ops"])
async def metrics():
    return container.metrics.snapshot()


@app.get("/ops/timeseries", tags=["ops"])
async def timeseries_cached(limit: int = 180):
    return _ttl(("ts", limit), 1.0, lambda: _timeseries(limit))


def _timeseries(limit: int = 180):
    return {"interval_seconds": container.sampler.interval,
            "samples": container.sampler.history(limit)}


@app.get("/ops/overview", tags=["ops"])
async def overview_cached():
    return _ttl("overview", 0.5, _build_overview)


def _billing_summary():
    b = container.billing
    tenants = sorted({r["tenant_id"] for r in b.usage_records}
                     | {c["tenant_id"] for c in b.credits})
    rows = []
    for t in tenants:
        ch = [r for r in b.usage_records if r["tenant_id"] == t]
        cr = [c for c in b.credits if c["tenant_id"] == t]
        rows.append({"tenant_id": t, "charges": len(ch),
                     "charged": round(sum(r["amount"] for r in ch), 6),
                     "credits": len(cr),
                     "credited": round(sum(c["amount"] for c in cr), 6),
                     "net": round(sum(r["amount"] for r in ch)
                                  - sum(c["amount"] for c in cr), 6)})
    return {"tenants": rows,
            "total_charged": round(sum(r["charged"] for r in rows), 6),
            "total_credited": round(sum(r["credited"] for r in rows), 6),
            "total_net": round(sum(r["net"] for r in rows), 6)}


def _build_overview():
    """Everything the dashboard needs, in one call.

    Deliberately one endpoint: a dashboard polling eight endpoints at 1 Hz
    generates eight times the noise in the metrics it is trying to display.
    """
    lm = container.lease_manager
    leases = sorted(lm.all(), key=lambda l: l.created_at, reverse=True)
    grace = CONFIG.grace_seconds

    slo_rows = []
    for l in lm.all():
        if l.notice_at is None or l.closed_at is None:
            continue
        secs = l.closed_at - l.notice_at
        slo_rows.append({"lease_id": l.lease_id, "seconds": round(secs, 3),
                         "within_budget": secs <= grace, "forced_stop": l.forced_stop})
    met = [r for r in slo_rows if r["within_budget"]]

    def countdown(l):
        if l.notice_at is None or l.state.value not in ("NOTICE_ISSUED", "DRAINING"):
            return None
        return round(max(0.0, l.notice_at + grace - time.time()), 1)

    return {
        "now": time.time(),
        "config": {
            "grace_seconds": grace,
            "force_stop_at": CONFIG.force_stop_at,
            "teardown_budget": CONFIG.teardown_budget,
            "control_cycle_seconds": CONFIG.control_cycle_seconds,
            "cooldown_seconds": CONFIG.cooldown_seconds,
            "blast_radius_fraction": CONFIG.blast_radius_fraction,
            "sample_interval_seconds": container.sampler.interval,
        },
        "cluster": container.sampler.cluster(),
        "host_groups": container.ledger.snapshot(),
        "pools": container.pool.snapshot(),
        "pricing": container.pricing.status(),
        "flavours": [_flavour_dict(f) for f in FLAVOURS.values()],
        "demand_top": container.demand.top(),
        "backend": container.backend,
        "headroom": container.forecast.headroom_units,
        "billing_summary": _billing_summary(),
        "saved_tasks": container.saved.list(),
        "states": container.sampler.state_histogram(),
        "inventory": container.market.inventory()["items"],
        "leases": [
            {**l.to_dict(),
             "age_seconds": round(time.time() - l.created_at, 1),
             "grace_remaining": countdown(l)}
            for l in leases[:60]
        ],
        "slo": {
            "measured": len(slo_rows),
            "within_budget": len(met),
            "attainment": round(len(met) / len(slo_rows), 4) if slo_rows else None,
            "stalled_teardowns": container.teardown_confirmer.stalled,
            "detail": slo_rows[-10:],
        },
        "interruption": {
            "by_flavour_az": container.analytics.rate_by_flavour_az(),
            "by_tenant": container.analytics.rate_by_tenant(),
        },
        "reclaim_orders": [
            {"order_id": o.order_id, "units": o.units, "az": o.az,
             "host_group": o.host_group, "reason": o.reason,
             "received_at": o.created_at, "deadline": o.deadline}
            for o in container.reclaim_handler.orders[-10:][::-1]
        ],
        "audit": container.audit.entries()[-40:][::-1],
        "events": container.bus.recent()[-40:][::-1],
        "metrics": container.metrics.snapshot(),
        "billing": {
            t: container.billing.invoice_for(t)["total"]
            for t in sorted({l.tenant_id for l in lm.all()})
        },
        "tenants": sorted(container.accounts._accounts.keys()),
    }


# ===========================================================================
# Simulation of the external capacity side — so you can drive a reclaim
# ===========================================================================
_HEADROOM_DEFAULTS = dict(container.forecast.headroom_units)
_HEADROOM_EPOCH: dict[str, int] = {}


async def _headroom_auto_revert(az: str, epoch: int):
    """A raised headroom expires on its own — a forgotten demo click can
    never leave the pool advertising zero forever."""
    await __import__("asyncio").sleep(CONFIG.headroom_ttl_seconds)
    if _HEADROOM_EPOCH.get(az) == epoch:          # nobody touched it since
        default = _HEADROOM_DEFAULTS.get(az, 8)
        if container.forecast.headroom_units.get(az) != default:
            container.forecast.set_headroom(az, default)
            await container.pool.refresh()
            container.audit.append("headroom_auto_reverted", az=az, units=default)


@app.post("/sim/headroom", tags=["sim"])
async def set_headroom(body: HeadroomRequest):
    """Raise forecast headroom, then ask the capacity side what it needs back.

    This is the PROACTIVE trigger from the HLD: reclaim starts because the
    forecast crossed a threshold, not because a customer request arrived.
    Reverts to the default automatically after SPOT_HEADROOM_TTL seconds.
    """
    container.forecast.set_headroom(body.az, body.units)
    _HEADROOM_EPOCH[body.az] = _HEADROOM_EPOCH.get(body.az, 0) + 1
    __import__("asyncio").create_task(
        _headroom_auto_revert(body.az, _HEADROOM_EPOCH[body.az]))
    await container.pool.refresh()
    sold = sum(l.units for l in container.lease_manager.live_leases(az=body.az))
    shortfall = container.forecast.protected_shortfall(body.az, sold)
    result = {"az": body.az, "headroom_units": body.units, "spot_units_live": sold,
              "shortfall_units": shortfall,
              "auto_reverts_in_seconds": CONFIG.headroom_ttl_seconds}
    if shortfall > 0:
        result["reclaim"] = await container.reclaim_handler.handle(
            units=shortfall, az=body.az, reason="forecast headroom rise"
        )
    return result


#: tenant_id -> notices pushed on channel 2. Simulation only: it stands in for
#: the tenant's own HTTP listener, so the webhook channel can be exercised
#: without one existing.
_WEBHOOK_INBOX: dict[str, list[dict]] = {}


@app.post("/sim/webhook", tags=["sim"])
async def register_webhook(body: WebhookRequest):
    """Register the tenant webhook — notice channel 2 (edge 16)."""
    tenant = body.tenant_id
    _WEBHOOK_INBOX.setdefault(tenant, [])

    async def deliver(payload: dict) -> None:
        _WEBHOOK_INBOX[tenant].append(payload)

    container.notice_delivery.register_webhook(tenant, deliver)
    return {"tenant_id": tenant, "registered": True,
            "inbox": f"/sim/webhook/{tenant}"}


@app.get("/sim/webhook/{tenant_id}", tags=["sim"])
async def webhook_inbox(tenant_id: str):
    """Read back what channel 2 actually pushed."""
    if tenant_id not in _WEBHOOK_INBOX:
        raise HTTPException(status_code=404, detail=f"no webhook registered for {tenant_id}")
    return {"tenant_id": tenant_id, "notices": _WEBHOOK_INBOX[tenant_id]}


@app.get("/healthz", tags=["ops"])
async def healthz():
    return {"status": "ok"}

"""HTTP surface: the gateway routes on account class, and the Spot Market API
returns the documented status codes and headers.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import spot.api.app as app_module
from spot.api.app import app
from spot.container import Container

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client(monkeypatch):
    """Fresh container per test — the app holds it as a module global, so the
    routes pick up the replacement by name at call time."""
    fresh = Container()
    monkeypatch.setattr(app_module, "container", fresh)
    await fresh.start()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        ac.container = fresh          # handy for tests that poke the stubs
        try:
            yield ac
        finally:
            await fresh.stop()


async def test_gateway_routes_spot_accounts_into_the_subsystem(client):
    r = await client.post(
        "/v1/instances",
        json={"flavour": "s1.small", "count": 1, "az": "az-1", "drain_seconds": 0.05},
        headers={"X-Tenant-Id": "tenant-spot-a", "Idempotency-Key": "http-1"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["state"] in ("ADMITTED", "PROVISIONING", "RUNNING")
    assert body["tenant_id"] == "tenant-spot-a"

    # Same idempotency key: a replay is not a creation, so 200 and no new lease.
    again = await client.post(
        "/v1/instances",
        json={"flavour": "s1.small", "count": 1, "az": "az-1", "drain_seconds": 0.05},
        headers={"X-Tenant-Id": "tenant-spot-a", "Idempotency-Key": "http-1"},
    )
    assert again.status_code == 200, again.text
    assert again.json()["lease_id"] == body["lease_id"]
    assert again.json()["idempotent_replay"] is True


async def test_gateway_refuses_non_spot_accounts_as_out_of_scope(client):
    for tenant in ("tenant-dynamic", "tenant-static"):
        r = await client.post(
            "/v1/instances",
            json={"flavour": "s1.small"},
            headers={"X-Tenant-Id": tenant},
        )
        assert r.status_code == 501
        assert r.json()["detail"]["out_of_scope"] is True


async def test_missing_tenant_header_is_401(client):
    r = await client.post("/v1/instances", json={"flavour": "s1.small"})
    assert r.status_code == 401


async def test_inventory_publishes_price_and_interruption_rate(client):
    r = await client.get("/spot/inventory")
    assert r.status_code == 200
    body = r.json()
    assert body["items"], "no inventory published"
    item = body["items"][0]
    for field in ("az", "flavour", "max_instances", "discount",
                  "spot_rate_per_hour", "interruption_rate_30d"):
        assert field in item


async def test_409_carries_retry_after_header(client):
    client.container.forecast.set_headroom("az-1", 10_000)
    client.container.forecast.set_headroom("az-2", 10_000)
    await client.container.pool.refresh()
    r = await client.post(
        "/spot/leases",
        json={"flavour": "s1.large", "count": 1, "az": "az-1"},
        headers={"X-Tenant-Id": "tenant-spot-a"},
    )
    assert r.status_code == 409
    assert "Retry-After" in r.headers
    assert r.json()["error"] == "no_capacity"


async def test_describe_and_release_round_trip(client):
    r = await client.post(
        "/spot/leases",
        json={"flavour": "s1.small", "count": 1, "az": "az-1", "drain_seconds": 0.05},
        headers={"X-Tenant-Id": "tenant-spot-a"},
    )
    assert r.status_code == 201, r.text
    lease_id = r.json()["lease_id"]

    d = await client.get(f"/spot/leases/{lease_id}")
    assert d.status_code == 200 and d.json()["lease_id"] == lease_id

    x = await client.delete(f"/spot/leases/{lease_id}")
    assert x.status_code == 200
    assert x.json()["state"] in ("STOPPED", "CLOSED", "REJECTED")

    missing = await client.get("/spot/leases/lease-does-not-exist")
    assert missing.status_code == 404


async def test_internal_reclaim_endpoint(client):
    r = await client.post(
        "/spot/leases",
        json={"flavour": "s1.small", "count": 1, "az": "az-1", "drain_seconds": 0.05},
        headers={"X-Tenant-Id": "tenant-spot-a"},
    )
    assert r.status_code == 201
    rec = await client.post("/internal/spot/reclaim",
                            json={"units": 2, "az": "az-1", "reason": "test"})
    assert rec.status_code == 200
    assert "order_id" in rec.json()


async def test_ops_endpoints_answer(client):
    for path in ("/healthz", "/ops/pools", "/ops/ledger", "/ops/audit",
                 "/ops/interruption-rates", "/ops/slo"):
        r = await client.get(path)
        assert r.status_code == 200, path

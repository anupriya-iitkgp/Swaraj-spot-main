"""The console surface: session, isolation, aggregation, and the signing proxy.

The tests that matter most here are not the ones checking that a dashboard
renders numbers. They are the two that check the console cannot be used to get
around something:

  * `test_actions_are_refused_without_a_session` — the console exists because
    LLD §12.1 says an unauthenticated path to `/internal/spot/reclaim` lets
    anyone who can reach the pod terminate every spot lease in an AZ. A console
    action route that skipped its own check would reintroduce exactly that, with
    a nicer interface.

  * `test_console_leases_are_cross_tenant_and_customer_leases_are_not` — the
    console reads every tenant's leases. If that query ever leaked into the
    customer surface, one tenant would see another's fleet.
"""

from __future__ import annotations

import os

import pytest

from spotd.domain.models import LeaseState

TOKEN = os.environ["SPOT_CONSOLE_TOKEN"]


async def _sign_in(client) -> None:
    response = await client.post("/console/session", json={"token": TOKEN})
    assert response.status_code == 200, response.text
    assert response.json()["authenticated"] is True


# ======================================================================
# session
# ======================================================================
async def test_console_reads_require_a_session(client):
    """Every cross-tenant read is behind the session, not just the actions."""
    for path in (
        "/console/overview",
        "/console/leases",
        "/console/timeline",
        "/console/audit",
        "/console/tenants",
        "/console/reclaim-orders",
    ):
        response = await client.get(path)
        assert response.status_code == 401, f"{path} answered {response.status_code}"
        assert response.json()["error"]["code"] == "unauthenticated"


async def test_actions_are_refused_without_a_session(client):
    """LLD §12.1, restated: no session, no reclaim.

    A console action is a signed internal call made on someone's behalf. If the
    "someone" is not established, the call must not happen — otherwise the
    console is a public endpoint that terminates customer instances.
    """
    response = await client.post(
        "/console/actions/reclaim",
        json={"order_id": "unauth-1", "az": "az-1", "units": 8},
    )
    assert response.status_code == 401

    # And nothing happened.
    assert (await client.get("/console/reclaim-orders")).status_code == 401


async def test_wrong_token_is_refused_without_saying_why(client):
    response = await client.post("/console/session", json={"token": "not-the-token"})
    assert response.status_code == 401
    body = response.json()["error"]
    # No hint about whether a token is configured, or how close this one was.
    assert "not accepted" in body["message"]
    assert "token" not in str(body.get("details", {})).lower() or not body.get("details")


async def test_session_cookie_is_http_only_and_same_site_strict(client):
    """A session that JavaScript can read is a session an XSS can steal."""
    response = await client.post("/console/session", json={"token": TOKEN})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "spot_console=" in cookie


async def test_a_tampered_cookie_does_not_authenticate(client):
    await _sign_in(client)
    assert (await client.get("/console/overview")).status_code == 200

    # Flip the role in the signed payload and keep the signature.
    original = client.cookies["spot_console"]
    issued, expires, _role, signature = original.split(".")
    client.cookies.set("spot_console", f"{issued}.{expires}.superuser.{signature}")

    response = await client.get("/console/overview")
    assert response.status_code == 401


async def test_sign_out_clears_the_session(client):
    await _sign_in(client)
    assert (await client.get("/console/overview")).status_code == 200
    await client.delete("/console/session")
    client.cookies.clear()
    assert (await client.get("/console/overview")).status_code == 401


# ======================================================================
# aggregation
# ======================================================================
async def test_overview_answers_the_dashboard_in_one_call(client, pooled, run_to_running):
    """One request, not eight.

    A dashboard polling eight endpoints at 1 Hz becomes the dominant client in
    the very latency histogram an operator is trying to read (LLD §14.1).
    """
    await _sign_in(client)
    lease = await run_to_running(pooled, tenant="tenant-spot-00")

    data = (await client.get("/console/overview")).json()

    assert {p["az"] for p in data["pools"]} == set(pooled.settings.availability_zones)
    assert data["lease_states"]["RUNNING"] >= 1
    assert data["totals"]["reserved_units"] >= lease.units
    assert data["totals"]["available_units"] >= 0
    # The invariant the whole admission design exists to hold.
    assert (
        data["totals"]["reserved_units"] + data["totals"]["cooldown_units"]
        <= data["totals"]["sellable_units"]
    )
    assert {w["name"] for w in data["workers"]}
    assert set(data["slo"]) == {"reclaim", "notice", "over_allocation", "fairness"}
    assert data["policy"]["grace_seconds"] == pooled.settings.grace_seconds
    assert data["slo"]["over_allocation"]["detected"] == 0


async def test_overview_reports_leases_inside_the_grace_window(
    client, pooled, run_to_running, reclaim_order
):
    """The countdown the dashboard runs on is the deadline the reaper acts on."""
    await _sign_in(client)
    lease = await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-02")
    await pooled.reclaim_handler.handle(reclaim_order(units=8, host_group=lease.host_group))

    data = (await client.get("/console/overview")).json()
    in_grace = {entry["lease_id"]: entry for entry in data["in_grace"]}
    assert lease.lease_id in in_grace

    entry = in_grace[lease.lease_id]
    assert entry["state"] == LeaseState.NOTICE_ISSUED.value
    assert entry["force_stop_deadline"] is not None
    # Positive and no larger than the window it was derived from.
    assert 0 < entry["seconds_to_force_stop"] <= pooled.settings.force_stop_at + 1


async def test_timeline_is_derived_from_lease_rows(client, pooled, run_to_running):
    """No sampling table, so no hole in the series where a restart was."""
    await _sign_in(client)
    lease = await run_to_running(pooled, tenant="tenant-spot-03")

    data = (await client.get("/console/timeline?minutes=30&buckets=12")).json()
    assert len(data["points"]) == 12
    assert data["bucket_seconds"] == pytest.approx(150.0)

    # The lease is running now, so it must be held in the final bucket.
    assert data["points"][-1]["held_units"] >= lease.units
    assert data["points"][-1]["admissions"] >= 1


async def test_console_leases_are_cross_tenant_and_customer_leases_are_not(
    client, pooled, run_to_running
):
    """The isolation property, from both sides."""
    await _sign_in(client)
    mine = await run_to_running(pooled, tenant="tenant-spot-00")
    theirs = await run_to_running(pooled, tenant="tenant-spot-01")

    console = {row["lease_id"] for row in (await client.get("/console/leases")).json()}
    assert {mine.lease_id, theirs.lease_id} <= console

    customer = await client.get(
        "/spot/leases", headers={"X-Tenant-Id": "tenant-spot-00"}
    )
    visible = {row["lease_id"] for row in customer.json()}
    assert mine.lease_id in visible
    assert theirs.lease_id not in visible


async def test_console_lease_filters_narrow_the_fleet(client, pooled, run_to_running):
    await _sign_in(client)
    await run_to_running(pooled, tenant="tenant-spot-00", az="az-1")
    await run_to_running(pooled, tenant="tenant-spot-01", az="az-2")

    by_tenant = (await client.get("/console/leases?tenant_id=tenant-spot-01")).json()
    assert by_tenant and all(r["tenant_id"] == "tenant-spot-01" for r in by_tenant)

    by_az = (await client.get("/console/leases?az=az-1")).json()
    assert by_az and all(r["az"] == "az-1" for r in by_az)

    by_state = (await client.get("/console/leases?state=RUNNING")).json()
    assert by_state and all(r["state"] == "RUNNING" for r in by_state)

    unknown = await client.get("/console/leases?state=NOT_A_STATE")
    assert unknown.status_code == 400


async def test_tenants_report_quota_headroom(client, pooled, run_to_running):
    """The number an operator wants when a tenant reports a 429."""
    await _sign_in(client)
    lease = await run_to_running(pooled, tenant="tenant-spot-00")

    rows = {t["tenant_id"]: t for t in (await client.get("/console/tenants")).json()["tenants"]}
    tenant = rows["tenant-spot-00"]
    assert tenant["units_in_use"] >= lease.units
    assert tenant["headroom_units"] == max(0, tenant["quota_units"] - tenant["units_in_use"])
    assert tenant["spot_entitled"] is True
    assert rows["tenant-static-00"]["spot_entitled"] is False


# ======================================================================
# the signing proxy
# ======================================================================
async def test_console_reclaim_is_attributed_to_the_operator(
    client, pooled, run_to_running
):
    """Same handler as edge 18, with the caller recorded.

    The console is not a shortcut past the reclaim path — it is another caller
    of it. What differs is `requested_by`, so afterwards the audit trail can
    answer why those instances died and on whose authority.
    """
    await _sign_in(client)
    lease = await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-04")

    response = await client.post(
        "/console/actions/reclaim",
        json={
            "order_id": "console-order-1",
            "az": lease.az,
            "units": lease.units,
            "host_group": lease.host_group,
            "deadline_seconds": 5.0,
            "reason": "operator_manual",
        },
    )
    assert response.status_code == 200, response.text
    outcome = response.json()
    assert lease.lease_id in outcome["leases_noticed"]

    order = await pooled.reclaim_repo.get("console-order-1")
    assert order.requested_by == "console-operator"

    noticed = await pooled.lease_repo.get(lease.lease_id)
    assert noticed.state is LeaseState.NOTICE_ISSUED

    # And the trail records it.
    events = [e["event"] for e in await pooled.audit_repo.for_order("console-order-1")]
    assert "reclaim.received" in events
    assert "preemption.notice_issued" in events


async def test_replaying_an_order_through_the_console_does_not_double_preempt(
    client, pooled, run_to_running
):
    """LLD §16: a replayed order must not double-preempt."""
    await _sign_in(client)
    await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-05")

    body = {"order_id": "console-replay", "az": "az-1", "units": 8, "deadline_seconds": 5.0}
    first = (await client.post("/console/actions/reclaim", json=body)).json()
    second = (await client.post("/console/actions/reclaim", json=body)).json()

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert second["leases_noticed"] == first["leases_noticed"]


async def test_reclaim_into_an_unknown_zone_is_refused(client):
    await _sign_in(client)
    response = await client.post(
        "/console/actions/reclaim",
        json={"order_id": "bad-az", "az": "az-nowhere", "units": 4},
    )
    assert response.status_code == 400
    assert "known_zones" in response.json()["error"]["details"]


async def test_reclaim_order_detail_carries_victims_and_evidence(
    client, pooled, run_to_running
):
    await _sign_in(client)
    lease = await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-06")
    await client.post(
        "/console/actions/reclaim",
        json={"order_id": "detail-1", "az": "az-1", "units": 8, "deadline_seconds": 5.0},
    )

    detail = (await client.get("/console/reclaim-orders/detail-1")).json()
    assert detail["order_id"] == "detail-1"
    assert [v["lease_id"] for v in detail["victims"]] == [lease.lease_id]
    # The audit rows come from a query that does not select order_id; the
    # response must carry it anyway.
    assert detail["audit"] and all(e["order_id"] == "detail-1" for e in detail["audit"])


async def test_headroom_drop_reports_the_shortfall_a_reclaim_has_to_cover(
    client, pooled, run_to_running
):
    """`sellable` below `reserved` is legal, and the gap is the reclaim's job.

    LLD §6.2 says the refresh should floor the pool at `reserved_units` —
    "never advertise less than what is already running" — and §5.2 asks for a
    `reserved + cooldown <= sellable` database constraint to enforce it. Both
    are wrong, and this service deliberately implements neither.

    The reason is §6.4's own ordering. Edge 20 shrinks the advertised pool
    *before* edge 21 selects victims, precisely so no launch is admitted against
    capacity that is already being taken back. A floor at `reserved` would make
    that shrink a no-op whenever the pool is fully sold — which is exactly when
    a reclaim order arrives. `sellable < reserved` is therefore a legal and
    transient state meaning "more is held than may now be sold", and it is
    resolved by leases ending, not by refusing to record it. Over-allocation is
    prevented by the reserve predicate, which never admits against units that
    are not there.

    What must hold instead is that the *sellable* figure never goes negative and
    availability floors at zero, so no launch can be admitted into the gap.
    """
    await _sign_in(client)
    lease = await run_to_running(pooled, flavour="s1.large", tenant="tenant-spot-07")

    response = await client.post(
        "/console/actions/headroom", json={"az": lease.az, "units": 0}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["shortfall_units"] == lease.units
    assert body["sellable_units"] == 0
    assert body["reserved_units"] == lease.units
    assert body["available_units"] == 0, "nothing may be sold into the shortfall"
    assert "has to be reclaimed" in body["note"]

    # And the reserve agrees: no launch can slip into that gap.
    assert not await pooled.pool_repo.try_reserve(lease.az, 1)


# ======================================================================
# serving the app
# ======================================================================
async def test_the_console_is_served_and_client_routes_fall_through_to_it(client):
    index = await client.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "no-store" in index.headers["cache-control"]
    # A page that can fire reclaim orders is worth a policy header.
    assert "default-src 'self'" in index.headers["content-security-policy"]

    # A client-side route the server has never heard of still gets the app.
    deep = await client.get("/operator/reclaim")
    assert deep.status_code == 200
    assert deep.headers["content-type"].startswith("text/html")


async def test_an_unknown_api_path_keeps_its_json_404(client):
    """Answering a mistyped API call with an HTML document turns a clear 404
    into a JSON parse error three layers from the mistake."""
    for path in ("/spot/nope", "/ops/nope", "/console/nope", "/internal/nope"):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"].startswith("application/json"), path
        assert response.json()["error"]["code"] == "not_found"


async def test_the_api_index_is_still_reachable_behind_the_console(client):
    response = await client.get("/api")
    assert response.status_code == 200
    assert response.json()["console"] == "/"

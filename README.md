# ESDS Spot Capacity Subsystem — reference implementation

Runnable Python implementation of the finalised HLD: everything that happens
**after** a request is identified as belonging to a spot account — admission,
lease, placement, preemption and rating.

It runs standalone. No database, no Kafka, no hypervisor: the dashed boxes in
the diagram are in-memory stubs with the same interfaces as the real services,
so you can swap them one at a time.

> ### Two builds live here
>
> | | `spot/` (this document) | [`production/`](production/README.md) |
> |---|---|---|
> | State | in-memory | PostgreSQL, migrated, multi-replica |
> | Purpose | read the design in one sitting | run it |
> | Auth | none | HMAC-signed `/internal`, session-gated console |
> | UI | single-file ops dashboard | tenant **and** operator consoles |
> | Tests | 28 | 53, against a real database |
>
> `production/` closes the nine gaps in LLD §12 — persistent leases and pool, a
> distributed reserve, a DB-backed grace reaper, signed internal calls, a
> transactional outbox, TTL sweeps, and the metrics. **Start there if you want
> to use it**; start here if you want to read it.

---

## Quick start

```bash
pip install -r requirements.txt

python run_demo.py            # full walkthrough, no server needed
pytest -q                     # 28 tests
uvicorn spot.api.app:app --reload    # HTTP API on :8000, docs at /docs
```

### The dashboard

`uvicorn spot.api.app:app` then open **http://localhost:8000/** — a live operations
dashboard, served by the app itself (no build step, no CDN, works offline):

- **Capacity**: cluster totals, utilisation, a stacked area chart of capacity over
  time, and per-host-group stacked bars — reserved / dynamic / spot / reclaiming /
  ops buffer / free, in vCPU units.
- **Spot pool read model**: sellable, reserved, cooldown and available per AZ, with
  the feed's confidence and staleness.
- **Leases**: every lease with its state, host group, discount, billed amount and —
  while it is being preempted — a **live grace countdown**.
- **Reclaim orders**, the **event stream**, the **append-only audit log**, and an
  **API call log** with p50/p99 latency.
- **Controls** to drive it: launch a lease, fire a reclaim order, raise forecast
  headroom (the proactive path), release a lease, or run a 6-way concurrent burst
  that races the admission controller so you can watch some launches take a 409.

Every control is a real HTTP call and appears in the API log a second later, so the
dashboard doubles as a live explanation of how the API fits together. Dashboard
polling itself is excluded from that log so it does not drown out real traffic.

Endpoints behind it: `GET /ops/overview` (one aggregate payload — a dashboard
polling eight endpoints at 1 Hz would pollute the metrics it is displaying) and
`GET /ops/timeseries`.

### Drive it over HTTP

```bash
# what's for sale
curl -s localhost:8000/spot/inventory | jq '.pools'

# launch (the gateway looks up the account class and routes SPOT onward)
curl -s -X POST localhost:8000/v1/instances \
  -H 'X-Tenant-Id: tenant-spot-a' -H 'Idempotency-Key: k1' \
  -H 'content-type: application/json' \
  -d '{"flavour":"s1.medium","count":2,"az":"az-1"}' | jq

# a non-spot account is out of scope for this project -> 501
curl -s -X POST localhost:8000/v1/instances \
  -H 'X-Tenant-Id: tenant-dynamic' -H 'content-type: application/json' \
  -d '{"flavour":"s1.small"}' | jq

# raise forecast headroom -> the capacity side issues a reclaim order
curl -s -X POST localhost:8000/sim/headroom \
  -H 'content-type: application/json' -d '{"az":"az-1","units":90}' | jq

# watch it happen
curl -s 'localhost:8000/spot/events?topic=spot' | jq '.[].topic'
curl -s localhost:8000/ops/slo | jq
curl -s localhost:8000/ops/ledger | jq
```

---

## Where each arrow of the diagram lives

| Edge | From → To | Code |
|---|---|---|
| 1 | API Gateway ⇄ Account Service | `api/app.py::gateway_launch`, `external/account_service.py` |
| 2, 3 | Customer → Gateway → Spot Market API | `api/app.py` |
| 4 | Spot Market API → Customer | `api/app.py::_launch`, `domain/errors.py` |
| 5 | Spot Market API ⇄ Eligibility & Quota Guard | `core/eligibility_guard.py` |
| 6 | Spot Market API ⇄ Spot Pool View | `core/pool_view.py::get_sellable` |
| 7, 31 | Spot Market API ⇄ Admission Controller ⇄ Pool | `core/admission_controller.py`, `pool_view.try_reserve` |
| 8 | Admission Controller → Spot Lease Manager | `core/lease_manager.py::create_lease` |
| 9, 10 | Lease Manager → Placement Adapter ⇄ Scheduler | `core/placement_adapter.py`, `external/placement_scheduler.py` |
| 11, 12 | Lease Manager ⇄ Provisioning Adapter ⇄ Hypervisor | `core/provisioning_adapter.py`, `external/hypervisor.py` |
| 13 | Provisioning Adapter → Teardown Confirmer | `core/teardown_confirmer.py` |
| 14 | Teardown Confirmer → Capacity Ledger | `external/capacity_ledger.py::commit_capacity_returned` |
| 15 | Teardown Confirmer → Lease Manager (CLOSED) | `lease_manager.close` |
| 16, 17 | Lease Manager → Notice Delivery → guest/tenant | `core/notice_delivery.py` |
| 18 | Capacity side → Reclaim Order Handler | `core/reclaim_handler.py::handle` |
| 19 | Forecast & Headroom → Spot Pool View | `external/forecast_headroom.py`, `pool_view.refresh` |
| 20 | Reclaim Order Handler → Pool (shrink) | `pool_view.shrink` |
| 21, 22 | Reclaim Handler → Victim Selector → Lease Manager | `core/victim_selector.py` |
| 23, 24 | Lease Manager ⇄ Grace Timer → force stop | `core/grace_timer.py` |
| 25, 26 | Lease Manager → Metering & Rating → Billing | `core/metering.py`, `external/billing.py` |
| 27, 28 | Timer / Lease Manager → Preemption Audit Log | `core/audit_log.py` |
| 29, 30 | Audit → Interruption Analytics → Spot Market API | `core/interruption_analytics.py` |
| 32 | Lease Manager → Spot Market API (describe) | `core/spot_market_api.py::describe` |

`spot/container.py` **is** the wiring diagram — every edge is one constructor
argument or one assignment in that file.

---

## The five invariants the code enforces

These are the parts of the design that are easy to get wrong, so each one has a
test that fails loudly if it regresses.

1. **No over-allocation, ever.** The pool read is a hint; `try_reserve` is the
   decision. `test_concurrent_launches_never_over_allocate` races 20 launches
   against a 12-unit pool and asserts exactly 6 win.
2. **Shrink before select.** A reclaim order shrinks the advertised pool
   (edge 20) *before* victims are chosen (edge 21), so nothing new is sold into
   capacity already being taken back — `test_pool_shrinks_before_victims_are_selected`.
3. **Capacity is free only after teardown.** Units sit in `RECLAIMING` until
   volumes are detached and IPs released; a stalled teardown holds them there
   rather than reporting them free —
   `test_capacity_is_only_free_after_teardown_is_confirmed`,
   `test_stalled_teardown_holds_capacity_in_reclaiming`.
4. **No notice, no charge for a lease that never ran.** A reclaim landing during
   `ADMITTED`/`PROVISIONING` cancels outright —
   `test_reclaim_during_provisioning_cancels_outright`.
5. **The timer is authoritative.** A guest that ignores the notice is force-stopped
   at `force_stop_at`; the grace period is a courtesy —
   `test_guest_that_ignores_the_notice_is_force_stopped`.

### Victim-selection precedence (written down on purpose)

The HLD flags that contiguity and fairness conflict. `core/victim_selector.py`
resolves it explicitly and this ordering is the single source of truth:

1. **Contiguity** picks the host set — drain the fewest host groups.
2. **Flavour match** narrows within it.
3. **Fairness** (newest lease first) only *orders* victims inside that set.
4. **Blast radius** caps how much of one tenant's fleet a single wave takes.

---

## Configuration

All via environment variables. Defaults are **demo-fast** so a full reclaim
completes in seconds; production values are in the right-hand column.

| Variable | Default | Production |
|---|---|---|
| `SPOT_GRACE_SECONDS` | 8 | 120 |
| `SPOT_FORCE_STOP_AT` | 6.3 | 95 |
| `SPOT_TEARDOWN_BUDGET` | 1.2 | 18 |
| `SPOT_CONTROL_CYCLE` | 2 | 30–60 |
| `SPOT_TENANT_QUOTA` | 64 units | per contract |
| `SPOT_BLAST_RADIUS` | 0.5 | tune from interruption spread |
| `SPOT_COOLDOWN` | 3 | 60–300 |
| `SPOT_MIN_DISCOUNT` / `SPOT_MAX_DISCOUNT` | 0.40 / 0.80 | commercial decision |
| `SPOT_SAMPLE_INTERVAL` | 2 | 10–30 |

For a readable dashboard demo, run with a longer grace window so you can watch the
countdown: `SPOT_GRACE_SECONDS=25 SPOT_FORCE_STOP_AT=20 uvicorn spot.api.app:app`.

---

## Going to production: what to replace

Every dashed box is one file under `spot/external/`. Keep the method
signatures, change the body.

| Stub | Replace with |
|---|---|
| `account_service.py` | your identity/IAM client |
| `capacity_ledger.py` | the real ledger service (Postgres + reconciliation) |
| `forecast_headroom.py` | the capacity side's sellable-spot feed |
| `placement_scheduler.py` | Nova / K8s scheduler client, honouring the bin-pack hint |
| `hypervisor.py` | libvirt / Nova / K8s API |
| `billing.py` | the billing system's usage ingest |
| `bus.py` | Kafka / NATS — same publish/subscribe surface |

In-process state that needs a real store before production:
`SpotLeaseManager._leases`, `AdmissionController._idem`,
`SpotPoolView._pools`, `PreemptionAuditLog._entries`. The reserve in
`pool_view.try_reserve` is an `asyncio.Lock` today; across multiple API
replicas it must become a database transaction or a Redis Lua script — the
atomicity is the guarantee, the mechanism is not.

---

## Two design notes worth reading before you build on this

**Account class as a router is a limitation, not a feature.** Binding the class
to the account means one tenant cannot run reserved, on-demand and spot side by
side without three separate accounts. The code therefore treats account class
as an *entitlement* and accepts an optional `purchase_option` on the launch
request (`api/schemas.py`), which is the cheap fix if you want it later. The
finalised diagram's account-only routing still works unchanged.

**Reclaim must be forecast-driven.** `/sim/headroom` models the proactive path:
the trigger is the forecast crossing a threshold, so the grace window is spent
*before* the dynamic customer calls the API. If reclaim only starts when demand
arrives, the customer waits the full grace period plus boot — the 2 minutes
protects capacity accounting, not latency.

---

## Layout

```
spot/
  config.py                 env-driven config
  container.py              composition root == the wiring diagram
  bus.py                    in-process event bus
  domain/
    models.py               account classes, flavours, Lease, state machine
    errors.py               typed rejections -> HTTP status codes
  core/                     ← the project scope (solid boxes)
    spot_market_api.py      customer contract
    eligibility_guard.py    entitlement, quota, flavour
    pool_view.py            read model + atomic reserve
    admission_controller.py idempotency + reserve-then-commit
    pricing.py              discount from surplus depth
    lease_manager.py        the hub: single writer of lease state
    placement_adapter.py    bin-pack hint
    provisioning_adapter.py idempotent instance ops
    notice_delivery.py      three independent channels
    grace_timer.py          the authoritative clock
    teardown_confirmer.py   proves capacity actually returned
    reclaim_handler.py      inbound order; shrink-then-select
    victim_selector.py      contiguity > flavour > fairness > blast radius
    metering.py             discount snapshot, grace exclusion, credits
    audit_log.py            immutable evidence
    interruption_analytics.py  published interruption rate
    telemetry.py            capacity sampler + API metrics (dashboard only)
  external/                 ← dashed boxes: stubs to replace
  api/
    app.py                  gateway + Spot Market API + ops endpoints
    schemas.py
    static/dashboard.html   the operations dashboard (single file, no build)
tests/                      28 tests
run_demo.py                 six-act end-to-end walkthrough
```

---

## Design documents

`docs/` contains the HLD and LLD this implementation was built from:

- `spot_hld_architecture.png`, `spot_hld_lifecycle.png`, `spot_hld_wiring.png` — the HLD diagrams (the wiring diagram is the one to read first)
- `lld_class_diagram.png` — class design with real signatures
- `lld_erd.png` — production persistence model
- `ESDS_Spot_Customer_HLD.docx`, `ESDS_Spot_Customer_LLD.docx` — full documents

Section 12 of the LLD lists nine known gaps between this code and production. Read it before deploying anything.

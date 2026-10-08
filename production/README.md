# spotd — Spot Customer Request Handling

The production service for the HLD and LLD in `../docs`: everything that happens
**after** a request is identified as belonging to a spot account — admission,
lease, placement, preemption and rating.

PostgreSQL-backed, multi-replica capable, with a first-party web console served
from the same process.

```
spotd/
  api/            HTTP: customer contract, gateway, signed /internal, ops, console BFF
  api/console/    the web console — native ES modules, no build step
  core/           the solid boxes of HLD §4, one module per component
  db/             engine + one repository per aggregate; every SQL statement lives here
  domain/         models, state machine, typed errors
  external/       the dashed boxes of HLD §4, behind interfaces (sim and live)
  workers/        the loops: outbox relay, grace reaper, pool refresher, sweepers
migrations/       alembic; one revision, advisory-locked
tests/            53 tests against a real PostgreSQL — no mocked persistence
```

---

## Running it

You need PostgreSQL. Nothing else — no Node, no bundler, no CDN.

```bash
export SPOT_DATABASE_URL='postgresql://spot@127.0.0.1:5432/spot'
export SPOT_ENV=dev
export SPOT_BACKEND=sim                  # synthetic capacity, no hypervisor needed
export SPOT_ENABLE_SIM=true              # /sim/* and the console's lab controls
export SPOT_INTERNAL_HMAC_KEY='a-key-of-at-least-32-characters-long'
export SPOT_CONSOLE_TOKEN='an-operator-token'
export SPOT_CONSOLE_COOKIE_SECURE=false  # only because localhost is plain http

python -m alembic upgrade head           # schema
python -m spotd.cli seed                 # synthetic tenants, flavours, host groups
python -m spotd.cli serve --port 8000    # API + console + workers
```

Then open **http://localhost:8000/**.

A compressed-timing lab is easier to watch than the production 120-second grace
window — the *relationships* between the values are what matter, and config
validation enforces the same invariant either way:

```bash
export SPOT_GRACE_SECONDS=45 SPOT_FORCE_STOP_AT=30 SPOT_TEARDOWN_BUDGET=10
export SPOT_CONTROL_CYCLE=10 SPOT_COOLDOWN=20
```

```bash
pytest -q                                # 53 tests; needs a reachable PostgreSQL
python -m spotd.cli config               # effective configuration, secrets redacted
python -m spotd.cli audit-verify         # recompute the audit hash chain
python -m spotd.cli reclaim --az az-1 --units 32   # a signed reclaim order
```

---

## The console

Two consoles, one origin, one process.

**Tenant** (`/`, `/leases`, `/activity`) is a client of the **published customer
API only** — `/spot/inventory`, `/v1/instances`, `/spot/leases`,
`/spot/interruptions`, `/spot/events`. It reads no privileged endpoint. That is
the point: a tenant UI that needs an internal endpoint is evidence the customer
contract is incomplete, so keeping it to the public API keeps the contract
honest.

- **Market** — availability, discount, price, published interruption rate and
  feed staleness side by side, because HLD §11 says a customer cannot size a
  spot workload without the interruption rate. Launching can go through the
  gateway (`POST /v1/instances`, exercising account classification) or straight
  to `POST /spot/leases`. Reusing an idempotency key demonstrates that the
  retry returns the original lease rather than allocating a second one.
- **Leases** — the fleet, with a live grace countdown on anything being
  reclaimed. The countdown is computed against `force_stop_deadline`, the
  column the reaper actually claims on, so a throttled or sleeping tab shows
  the true remaining time instead of a drifted one.
- **Lease detail** — the two questions a customer asks after a preemption,
  answered from stored evidence: *was I warned* (the timeline, and which of the
  three notice channels succeeded) and *why is this the bill* (rate snapshot,
  billable window, grace seconds excluded, credit).
- **Activity** — the tenant event stream, which is HLD §12's answer to retry
  storms: wait on `spot.preempt.notice` instead of polling.

**Operator** (`/operator/...`) is behind a session and is cross-tenant:
overview, pools, fleet, reclaim, SLO, audit, tenants, config.

### Why the operator console has a login

The console can fire a reclaim order, and a reclaim order ends customer
workloads. LLD §12.1 is explicit that an unauthenticated path to
`/internal/spot/reclaim` lets anyone who can reach the pod terminate every spot
lease in an AZ. Two shortcuts were available and both are worse:

| Shortcut | Why not |
|---|---|
| Ship the HMAC key to the browser and sign in JavaScript | That key terminates every lease in a zone. In `localStorage` it is one XSS, one shared laptop or one screen-share away from being someone else's. |
| Leave `/console` open because "it is only a dashboard" | It stops being only a dashboard the moment it has a button that ends customer workloads. |

So the browser gets a session cookie and the **process keeps the key**:

```
browser ──cookie──▶ POST /console/actions/reclaim
                      │  1. session verified   (who is asking)
                      │  2. request signed server-side with SPOT_INTERNAL_HMAC_KEY
                      │  3. put through the same SignatureVerifier as any
                      │     capacity-side caller — nonce consumed, skew checked
                      ▼
                    ReclaimOrderHandler   (requested_by = "console-operator")
```

Nothing bypasses the signed path; the console is simply another signed caller
that happens to live in the same process. The audit trail records the action
against the operator, so afterwards the log can say why those instances died
and on whose authority.

The session cookie is `HttpOnly`, `SameSite=strict`, signed rather than stored,
with the secret derived from `SPOT_CONSOLE_TOKEN` — so rotating that token
invalidates every outstanding session at once. That is the revocation story;
there is no session table to migrate, sweep and keep consistent.

Config validation refuses to start in production with the console enabled and
no token set, in the same way it refuses `SPOT_ENABLE_SIM=true`.

### Why there is no build step

The console is native ES modules, served as written by `api/static.py`. No
bundler, no `node_modules`, nothing generated: the file on disk is the file in
the browser, so what you review is what runs and a stack trace points at a real
line. A control plane that needs the public internet to render its own incident
dashboard has picked the wrong dependency for the wrong moment.

The cost is one request per module, which HTTP/2 and a warm cache make
uninteresting for an internal tool. Assets are served `no-cache` — revalidated,
answered `304` when unchanged — rather than `immutable`, because only the entry
point's URL can carry a build stamp; the rest are reached through `import`
statements in the source, and pinning those would leave a browser running half
of yesterday's UI against today's API.

`index.html` ships a `Content-Security-Policy` of `default-src 'self'` with no
`unsafe-inline`. A page that can fire reclaim orders is worth a header.

### UI routes vs API routes

The operator UI lives at `/operator/*`, not `/ops/*`, because `/ops/pools`,
`/ops/slo` and `/ops/config` are **live API endpoints**. Sharing the prefix
meant reloading the page at `/ops/pools` returned JSON instead of the app. The
SPA fallback also refuses to serve HTML for anything under a known API prefix —
answering a mistyped API call with `200` and an HTML document turns a clear
`404` into a JSON parse error three layers from the mistake.

---

## Console endpoints

Everything under `/console` requires an operator session.

| Endpoint | Purpose |
|---|---|
| `GET/POST/DELETE /console/session` | who am I · sign in · sign out |
| `GET /console/overview` | the whole dashboard in one call |
| `GET /console/timeline` | held units, admissions, notices, forced stops over a window |
| `GET /console/leases` | cross-tenant fleet, filtered |
| `GET /console/reclaim-orders[/{id}]` | orders, with victims and their evidence |
| `GET /console/audit` | audit trail + hash-chain verification |
| `GET /console/events` | the bus, every topic |
| `GET /console/tenants` · `/host-groups` · `/invoice/{lease}` | quota, placement, charge |
| `POST /console/actions/reclaim` | issue a reclaim order (signed server-side) |
| `POST /console/actions/headroom` · `/control-cycle` · `/guest-behaviour/{id}` · `/burst` | lab controls, gated on `SPOT_ENABLE_SIM` |
| `POST /console/actions/clean-exit/{id}` · `POST|DELETE /console/actions/quarantine/{hg}` | edge 13, and the §11 host escalation |

`GET /console/overview` is one endpoint rather than eight on purpose. A
dashboard polling eight endpoints at 1 Hz becomes the dominant client in the
very latency histogram an operator is reading, so the p99 in LLD §14.1 stops
being a measurement of customer traffic and starts being a measurement of the
dashboard.

`GET /console/timeline` recomputes its series from `spot_lease` on every call
rather than reading a sampled history table. That costs a group-by per poll and
buys a series that is identical from every replica and unbroken by a restart —
a sampled series has a hole in it exactly where the incident was.

---

## Two design decisions worth knowing about

### The undelivered-notice credit has a floor

LLD §6.6 credits the billed amount when no notice channel delivered. Billed
amount alone makes the credit proportional to how long the lease happened to
run before it was killed, so a lease preempted a second after it started is
credited approximately nothing — precisely the case where the customer was
worst served.

What they lost is not the compute, it is the notice. HLD §11 puts notice
delivery at "≥ 99.99% on at least one channel" and calls it the trust anchor of
the product, so the thing that failed has a price: one grace window at the
lease's own rate. The credit is `max(billed_amount, grace_seconds × rate)`. For
any lease that ran longer than its grace window the billed amount dominates and
the floor never binds, leaving §6.6's arithmetic unchanged for the ordinary
case. Covered by `test_an_undelivered_notice_is_credited_at_least_one_grace_window`.

### `sellable < reserved` is legal, and the LLD is wrong about it

LLD §6.2 says the pool refresh should floor `sellable_units` at
`reserved_units` — "never advertise less than what is already running" — and
§5.2 asks for a `reserved + cooldown <= sellable` database constraint to
enforce it.

This service implements neither, deliberately, because §6.4's own ordering
contradicts them. Edge 20 shrinks the advertised pool **before** edge 21 selects
victims, precisely so no launch is admitted against capacity already being taken
back. A floor at `reserved` would make that shrink a no-op whenever the pool is
fully sold — which is exactly when a reclaim order arrives.

So `sellable < reserved` is a legal, transient state meaning "more is held than
may now be sold", resolved by leases ending rather than by refusing to record
it. Over-allocation is prevented by the reserve predicate, which never admits
against units that are not there; `available_units` floors at zero so nothing
can be sold into the gap. The console's headroom control reports that gap as
`shortfall_units` — the capacity a reclaim order has to go and get. Covered by
`test_headroom_drop_reports_the_shortfall_a_reclaim_has_to_cover`.

---

## Configuration

`python -m spotd.cli config` prints the effective values with secrets redacted;
the console's Config page renders the same thing with the grace budget drawn to
scale. The variables the console adds:

| Variable | Default | Effect |
|---|---|---|
| `SPOT_CONSOLE_ENABLED` | `true` | Serve the console and its BFF at all. |
| `SPOT_CONSOLE_TOKEN` | — | The operator credential. **Required in production** when the console is enabled; unset means any credential opens a session, which config validation refuses outside dev. |
| `SPOT_CONSOLE_SESSION_TTL` | `43200` | Session lifetime in seconds. |
| `SPOT_CONSOLE_COOKIE_SECURE` | `true` in prod | Must stay true in production — the cookie authorises reclaim orders. |

Everything else is unchanged from LLD §13.

---

## Walking the whole system in about two minutes

1. **Market** — note the discount tracking surplus depth, and the interruption
   rate published next to the price rather than hidden.
2. Launch a couple of leases. Submit the second one twice with the same
   idempotency key and watch it come back as a replay, not a new lease.
3. **Operator → Tenants → Run burst** — eight launches raced at one pool. Some
   are admitted, the rest take a `409` with `Retry-After`, and the reserved
   total never exceeds what was sellable. That is the admission guarantee, and
   it is more convincing watched than asserted.
4. **Operator → Reclaim → Drop forecast headroom** below what is held. The
   response names the shortfall; the form pre-fills with it. This is the
   *proactive* path — the grace window is spent ahead of the demand, not after
   it arrives.
5. Issue the order. Victims appear in **Draining now** with a countdown against
   the deadline the reaper acts on. Report a clean exit for one and let the
   other run out — one closes cleanly, the other is force-stopped, and both
   return their capacity only after teardown is confirmed.
6. **Lease detail** for a victim — the timeline proves the grace window was
   honoured, the notice panel says which channels reached the customer, and the
   charge panel shows the grace seconds excluded from the bill.
7. **Operator → Audit** — the same story, hash-chained, plus a verification that
   recomputes the chain and reports the first break.

"""Initial schema for the spot control plane.

This is LLD §16.1 step 1 — "Persist leases, pool and audit to Postgres; keep the
same interfaces" — and it is what unblocks everything else. Four decisions in
here are load-bearing and are explained where they appear:

  * `spot_pool` is keyed by AZ, not by (AZ, flavour). Capacity is fungible vCPU
    within an AZ; a per-flavour row would have to be decremented on every other
    flavour's sale.
  * `spot_lease.version` exists so a transition can be conditional rather than
    locked, which is what lets N replicas write leases safely (LLD §16).
  * `preemption_audit` is append-only *in the database*, not by convention, and
    hash-chained so a deletion is detectable. HLD §11 requires 100% audit
    completeness because "preemption disputes are settled from this log or not
    at all" — a log the application could rewrite settles nothing.
  * The reaper's index is partial on `state = 'NOTICE_ISSUED'`. That set is
    small and short-lived; a full index on a hot table would be paid for on
    every lease write to serve a query that only matters for a few seconds per
    lease.

Statements are listed one per element because asyncpg uses the extended query
protocol, which refuses multiple commands in one statement. Keeping them
separate also means a failure names the exact object that failed.

Revision ID: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


UPGRADE: list[str] = [
    # ------------------------------------------------------------------
    # reference data
    # ------------------------------------------------------------------
    """
    CREATE TABLE tenant (
        tenant_id         text PRIMARY KEY,
        name              text NOT NULL,
        account_class     text NOT NULL
                          CHECK (account_class IN ('STATIC','DYNAMIC','SPOT')),
        spot_quota_units  integer NOT NULL DEFAULT 64 CHECK (spot_quota_units >= 0),
        concurrency_cap   integer NOT NULL DEFAULT 50 CHECK (concurrency_cap >= 0),
        webhook_url       text,
        contract_tier     text NOT NULL DEFAULT 'standard',
        active            boolean NOT NULL DEFAULT true,
        created_at        timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE flavour (
        name           text PRIMARY KEY,
        vcpu           integer NOT NULL CHECK (vcpu > 0),
        memory_gb      integer NOT NULL CHECK (memory_gb > 0),
        -- Licence-bound flavours are rejected with 400, not 409: no amount of
        -- retrying will ever make them sellable as spot.
        spot_eligible  boolean NOT NULL DEFAULT true,
        licence_bound  boolean NOT NULL DEFAULT false,
        family         text NOT NULL DEFAULT 'general',
        CHECK (NOT (licence_bound AND spot_eligible))
    )
    """,
    """
    CREATE TABLE host_group (
        host_group        text PRIMARY KEY,
        az                text NOT NULL,
        total_units       integer NOT NULL CHECK (total_units >= 0),
        -- A host whose agent proved unreachable during a forced stop is
        -- quarantined out of the spot pool rather than selected again and
        -- failed again (LLD §11).
        quarantined       boolean NOT NULL DEFAULT false,
        quarantined_at    timestamptz,
        quarantine_reason text
    )
    """,
    "CREATE INDEX host_group_az_idx ON host_group (az) WHERE NOT quarantined",

    # ------------------------------------------------------------------
    # the pool read model + the atomic reserve target
    # ------------------------------------------------------------------
    """
    CREATE TABLE spot_pool (
        az               text PRIMARY KEY,
        sellable_units   integer NOT NULL DEFAULT 0 CHECK (sellable_units >= 0),
        reserved_units   integer NOT NULL DEFAULT 0 CHECK (reserved_units >= 0),
        cooldown_units   integer NOT NULL DEFAULT 0 CHECK (cooldown_units >= 0),
        confidence       double precision NOT NULL DEFAULT 0
                         CHECK (confidence BETWEEN 0 AND 1),
        published_at     timestamptz,
        horizon_seconds  double precision NOT NULL DEFAULT 0,
        degraded         boolean NOT NULL DEFAULT true,
        cycle_seq        bigint NOT NULL DEFAULT 0,
        updated_at       timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    COMMENT ON TABLE spot_pool IS
        'Spot Pool View (HLD 6). Stale by design; authoritative only for the '
        'reserve, which is the conditional UPDATE in PoolRepository.try_reserve. '
        'Note there is deliberately NO check that reserved+cooldown <= sellable: '
        'a reclaim shrinks sellable while leases still hold reservations, so '
        'that state is legal and transient. Over-allocation is prevented by the '
        'reserve predicate, not by a constraint.'
    """,
    # Cooldown is tracked as expiring entries rather than a bare counter so the
    # sweeper can return exactly the units whose hold has elapsed. HLD §12 asks
    # for the cooldown to be a tunable policy value whose cost is measurable;
    # per-entry rows are what make "idle core-seconds held in cooldown"
    # measurable at all.
    """
    CREATE TABLE spot_pool_cooldown (
        id           bigserial PRIMARY KEY,
        az           text NOT NULL REFERENCES spot_pool(az) ON DELETE CASCADE,
        units        integer NOT NULL CHECK (units > 0),
        lease_id     text,
        reason       text NOT NULL DEFAULT 'reclaim',
        created_at   timestamptz NOT NULL DEFAULT now(),
        releases_at  timestamptz NOT NULL,
        released_at  timestamptz
    )
    """,
    """
    CREATE INDEX spot_pool_cooldown_due_idx
        ON spot_pool_cooldown (releases_at) WHERE released_at IS NULL
    """,
    # Every publication from the forecast feed is kept. HLD §12 warns the
    # sellable number is an input, not a fact; when a shortage is investigated
    # the first question is what the feed said and how confident it was.
    """
    CREATE TABLE forecast_feed (
        id               bigserial PRIMARY KEY,
        az               text NOT NULL,
        units            integer NOT NULL CHECK (units >= 0),
        confidence       double precision NOT NULL CHECK (confidence BETWEEN 0 AND 1),
        published_at     timestamptz NOT NULL,
        horizon_seconds  double precision NOT NULL DEFAULT 0,
        received_at      timestamptz NOT NULL DEFAULT now(),
        accepted         boolean NOT NULL,
        applied_units    integer NOT NULL,
        reject_reason    text,
        UNIQUE (az, published_at)
    )
    """,
    "CREATE INDEX forecast_feed_az_time_idx ON forecast_feed (az, published_at DESC)",

    # ------------------------------------------------------------------
    # the lease — HLD §10
    # ------------------------------------------------------------------
    """
    CREATE TABLE spot_lease (
        lease_id          text PRIMARY KEY,
        tenant_id         text NOT NULL REFERENCES tenant(tenant_id),
        idempotency_key   text NOT NULL,
        purchase_option   text NOT NULL DEFAULT 'spot'
                          CHECK (purchase_option IN ('spot','on_demand')),
        purchase_option_source text NOT NULL DEFAULT 'account'
                          CHECK (purchase_option_source IN ('request','account')),
        flavour           text NOT NULL REFERENCES flavour(name),
        count             integer NOT NULL CHECK (count > 0),
        units             integer NOT NULL CHECK (units > 0),
        az                text NOT NULL,
        host_group        text,
        state             text NOT NULL CHECK (state IN (
                              'REQUESTED','ADMITTED','PROVISIONING','RUNNING',
                              'NOTICE_ISSUED','DRAINING','STOPPED','CLOSED',
                              'REJECTED')),
        -- Optimistic concurrency. Every transition is conditional on this
        -- value, which replaces the in-process per-lease lock and lets any
        -- replica write any lease (LLD 16).
        version           integer NOT NULL DEFAULT 0,
        instance_ids      jsonb NOT NULL DEFAULT '[]'::jsonb,

        -- rating, frozen at lease start (HLD 10)
        discount_snapshot double precision NOT NULL DEFAULT 0
                          CHECK (discount_snapshot BETWEEN 0 AND 1),
        rate_per_sec      double precision NOT NULL DEFAULT 0 CHECK (rate_per_sec >= 0),
        grace_seconds     double precision NOT NULL DEFAULT 120,

        -- evidence timestamps
        created_at        timestamptz NOT NULL DEFAULT now(),
        admitted_at       timestamptz,
        provisioning_at   timestamptz,
        running_at        timestamptz,
        notice_at         timestamptz,
        -- Persisted, not derived from an in-memory timer. This single column is
        -- the fix for LLD 12.3: after a restart the reaper can reconstruct
        -- every outstanding deadline from the table.
        force_stop_deadline timestamptz,
        stopped_at        timestamptz,
        closed_at         timestamptz,

        -- preemption traceability
        preemption_reason text CHECK (preemption_reason IN (
                              'capacity_reclaim','cancelled_in_flight',
                              'customer_release','host_quarantine')),
        reclaim_order_id  text,
        forced_stop       boolean NOT NULL DEFAULT false,
        notice_channels_delivered jsonb NOT NULL DEFAULT '[]'::jsonb,

        -- billing
        grace_seconds_excluded double precision NOT NULL DEFAULT 0,
        credit_raised     double precision NOT NULL DEFAULT 0,
        billed_seconds    double precision NOT NULL DEFAULT 0,
        billed_amount     double precision NOT NULL DEFAULT 0,

        rejection_code    text,
        rejection_detail  text,
        teardown_stalled  boolean NOT NULL DEFAULT false,

        -- reaper claim (LLD 16: the reaper must not run N times on one lease)
        reaper_claimed_at timestamptz,
        reaper_claimed_by text,

        updated_at        timestamptz NOT NULL DEFAULT now(),

        -- A lease that reached RUNNING must have a host group; one that never
        -- ran must never be billed. Both are invariants the rating code would
        -- otherwise have to be trusted to respect.
        CONSTRAINT running_lease_is_placed
            CHECK (running_at IS NULL OR host_group IS NOT NULL),
        CONSTRAINT unrun_lease_is_not_billed
            CHECK (running_at IS NOT NULL OR billed_amount = 0)
    )
    """,
    """
    CREATE UNIQUE INDEX spot_lease_tenant_idem_idx
        ON spot_lease (tenant_id, idempotency_key)
    """,
    "CREATE INDEX spot_lease_tenant_state_idx ON spot_lease (tenant_id, state)",
    "CREATE INDEX spot_lease_az_state_idx ON spot_lease (az, state)",
    # Victim selection scans live leases in one host group. Partial, because
    # terminal leases vastly outnumber live ones on any real deployment.
    """
    CREATE INDEX spot_lease_victim_idx
        ON spot_lease (az, host_group, flavour, created_at DESC)
        WHERE state = 'RUNNING'
    """,
    # The reaper's index (LLD §12.3). Small, hot, and short-lived.
    """
    CREATE INDEX spot_lease_reaper_idx
        ON spot_lease (force_stop_deadline)
        WHERE state = 'NOTICE_ISSUED'
    """,
    # The teardown sweeper's index: STOPPED leases whose capacity has not yet
    # been confirmed back are the ones holding units in RECLAIMING.
    """
    CREATE INDEX spot_lease_teardown_idx
        ON spot_lease (stopped_at) WHERE state = 'STOPPED'
    """,
    """
    CREATE INDEX spot_lease_metering_idx
        ON spot_lease (state, running_at) WHERE state = 'RUNNING'
    """,

    # ------------------------------------------------------------------
    # idempotency — HLD §11 requires >= 24 h retention
    # ------------------------------------------------------------------
    """
    CREATE TABLE idempotency_key (
        tenant_id            text NOT NULL,
        key                  text NOT NULL,
        lease_id             text,
        -- A hash of the semantically significant request fields. A repeat with
        -- the same key but a different body is a client bug; replaying the
        -- original lease would hand back something they did not ask for.
        request_fingerprint  text NOT NULL,
        outcome              text NOT NULL DEFAULT 'pending',
        response_status      integer,
        response_body        jsonb,
        created_at           timestamptz NOT NULL DEFAULT now(),
        -- LLD 12.4: keys were previously evicted only when looked up again, so
        -- keys that are never retried were never evicted. expires_at plus the
        -- sweeper closes that leak.
        expires_at           timestamptz NOT NULL,
        PRIMARY KEY (tenant_id, key)
    )
    """,
    "CREATE INDEX idempotency_key_expiry_idx ON idempotency_key (expires_at)",

    # ------------------------------------------------------------------
    # transactional outbox — LLD §12.8
    # ------------------------------------------------------------------
    """
    CREATE TABLE outbox (
        id             bigserial PRIMARY KEY,
        topic          text NOT NULL,
        aggregate_type text NOT NULL,
        aggregate_id   text NOT NULL,
        payload        jsonb NOT NULL,
        created_at     timestamptz NOT NULL DEFAULT now(),
        available_at   timestamptz NOT NULL DEFAULT now(),
        attempts       integer NOT NULL DEFAULT 0,
        published_at   timestamptz,
        dead           boolean NOT NULL DEFAULT false,
        last_error     text
    )
    """,
    """
    COMMENT ON TABLE outbox IS
        'Lease state and the intent to tell the ledger/bus are written in one '
        'transaction; the relay publishes from here. Closes LLD 12.8, where a '
        'crash between the two writes left the views disagreeing until '
        'reconciliation.'
    """,
    # The relay claims work with FOR UPDATE SKIP LOCKED over this index, so N
    # relay replicas never contend on the same row.
    """
    CREATE INDEX outbox_unpublished_idx
        ON outbox (available_at, id) WHERE published_at IS NULL AND NOT dead
    """,
    "CREATE INDEX outbox_aggregate_idx ON outbox (aggregate_type, aggregate_id)",

    # ------------------------------------------------------------------
    # append-only, hash-chained audit — HLD §11 (100% completeness)
    # ------------------------------------------------------------------
    """
    CREATE TABLE preemption_audit (
        id          bigserial PRIMARY KEY,
        ts          timestamptz NOT NULL DEFAULT now(),
        event       text NOT NULL,
        lease_id    text,
        order_id    text,
        tenant_id   text,
        actor       text NOT NULL DEFAULT 'spotd',
        detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
        prev_hash   text NOT NULL DEFAULT '',
        entry_hash  text NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX preemption_audit_lease_idx ON preemption_audit (lease_id, id)",
    """
    CREATE INDEX preemption_audit_order_idx ON preemption_audit (order_id, id)
        WHERE order_id IS NOT NULL
    """,
    "CREATE INDEX preemption_audit_ts_idx ON preemption_audit (ts)",
    "CREATE INDEX preemption_audit_event_idx ON preemption_audit (event, ts)",
    # Each row's hash covers the previous row's hash, so removing or editing any
    # row breaks the chain from that point on and `spotd audit-verify` finds it.
    # sha256() is a Postgres built-in from 11 onward, so this needs no extension.
    """
    CREATE FUNCTION spot_audit_chain() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
    DECLARE
        last_hash text;
    BEGIN
        -- Serialises appenders so two concurrent inserts cannot both chain from
        -- the same predecessor. Transaction-scoped: released on commit.
        PERFORM pg_advisory_xact_lock(hashtext('preemption_audit'));
        SELECT entry_hash INTO last_hash
          FROM preemption_audit ORDER BY id DESC LIMIT 1;
        NEW.prev_hash := COALESCE(last_hash, repeat('0', 64));
        NEW.entry_hash := encode(sha256(convert_to(
            NEW.prev_hash || '|' ||
            to_char(NEW.ts AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') || '|' ||
            NEW.event || '|' ||
            COALESCE(NEW.lease_id, '') || '|' ||
            COALESCE(NEW.order_id, '') || '|' ||
            COALESCE(NEW.tenant_id, '') || '|' ||
            NEW.actor || '|' ||
            COALESCE(NEW.detail::text, '{}'), 'UTF8')), 'hex');
        RETURN NEW;
    END $fn$
    """,
    """
    CREATE TRIGGER preemption_audit_chain_trg
        BEFORE INSERT ON preemption_audit
        FOR EACH ROW EXECUTE FUNCTION spot_audit_chain()
    """,
    """
    CREATE FUNCTION spot_audit_immutable() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
    BEGIN
        RAISE EXCEPTION
            'preemption_audit is append-only: % is not permitted', TG_OP
            USING ERRCODE = 'restrict_violation';
    END $fn$
    """,
    """
    CREATE TRIGGER preemption_audit_no_update_trg
        BEFORE UPDATE OR DELETE ON preemption_audit
        FOR EACH ROW EXECUTE FUNCTION spot_audit_immutable()
    """,

    # ------------------------------------------------------------------
    # notice delivery proof — HLD §6 forbids failing silently
    # ------------------------------------------------------------------
    """
    CREATE TABLE notice_delivery (
        id          bigserial PRIMARY KEY,
        lease_id    text NOT NULL,
        tenant_id   text NOT NULL,
        channel     text NOT NULL
                    CHECK (channel IN ('metadata','webhook','event_stream')),
        attempt     integer NOT NULL DEFAULT 1,
        delivered   boolean NOT NULL,
        latency_ms  double precision,
        error       text,
        payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
        at          timestamptz NOT NULL DEFAULT now(),
        UNIQUE (lease_id, channel, attempt)
    )
    """,
    "CREATE INDEX notice_delivery_lease_idx ON notice_delivery (lease_id)",
    "CREATE INDEX notice_delivery_window_idx ON notice_delivery (at, delivered)",
    """
    COMMENT ON TABLE notice_delivery IS
        'Delivery proof lives here rather than in process memory, which is also '
        'the eviction fix for LLD 12.5.'
    """,

    # ------------------------------------------------------------------
    # reclaim orders — idempotent per order_id (LLD §16)
    # ------------------------------------------------------------------
    """
    CREATE TABLE reclaim_order (
        order_id        text PRIMARY KEY,
        az              text NOT NULL,
        units           integer NOT NULL CHECK (units > 0),
        host_group      text,
        deadline        timestamptz NOT NULL,
        reason          text NOT NULL DEFAULT 'forecast_headroom',
        requested_by    text NOT NULL,
        state           text NOT NULL DEFAULT 'RECEIVED' CHECK (state IN (
                            'RECEIVED','SHRINKING','SELECTING','NOTICED',
                            'COMPLETED','PARTIAL','FAILED')),
        received_at     timestamptz NOT NULL DEFAULT now(),
        units_selected  integer NOT NULL DEFAULT 0,
        leases_selected jsonb NOT NULL DEFAULT '[]'::jsonb,
        completed_at    timestamptz,
        detail          text
    )
    """,
    "CREATE INDEX reclaim_order_state_idx ON reclaim_order (state, received_at)",
    "CREATE INDEX reclaim_order_az_idx ON reclaim_order (az, received_at DESC)",

    # ------------------------------------------------------------------
    # capacity ledger mirror — idempotent per (host_group, lease, operation)
    # ------------------------------------------------------------------
    """
    CREATE TABLE capacity_ledger_entry (
        id           bigserial PRIMARY KEY,
        host_group   text NOT NULL,
        lease_id     text NOT NULL,
        operation    text NOT NULL CHECK (operation IN (
                         'allocated','reclaiming','returned','released')),
        units        integer NOT NULL,
        created_at   timestamptz NOT NULL DEFAULT now(),
        confirmed    boolean NOT NULL DEFAULT false,
        confirmed_at timestamptz,
        UNIQUE (host_group, lease_id, operation)
    )
    """,
    """
    CREATE INDEX capacity_ledger_unconfirmed_idx
        ON capacity_ledger_entry (created_at) WHERE NOT confirmed
    """,
    """
    COMMENT ON TABLE capacity_ledger_entry IS
        'Local mirror of what was told to the out-of-scope Capacity Ledger. '
        'Units are never reported free on an unconfirmed commit (LLD 9).'
    """,

    # ------------------------------------------------------------------
    # rating
    # ------------------------------------------------------------------
    """
    CREATE TABLE usage_record (
        id                bigserial PRIMARY KEY,
        lease_id          text NOT NULL,
        tenant_id         text NOT NULL,
        window_start      timestamptz NOT NULL,
        window_end        timestamptz NOT NULL,
        units             integer NOT NULL,
        billable_seconds  double precision NOT NULL CHECK (billable_seconds >= 0),
        rate_per_sec      double precision NOT NULL,
        discount          double precision NOT NULL,
        amount            double precision NOT NULL CHECK (amount >= 0),
        grace_seconds_excluded double precision NOT NULL DEFAULT 0,
        created_at        timestamptz NOT NULL DEFAULT now(),
        submitted_at      timestamptz,
        billing_ref       text,
        -- The billing system is told to accept duplicates safely (LLD 9); this
        -- makes a duplicate impossible to create in the first place.
        UNIQUE (lease_id, window_start)
    )
    """,
    """
    CREATE INDEX usage_record_unsubmitted_idx
        ON usage_record (created_at) WHERE submitted_at IS NULL
    """,
    "CREATE INDEX usage_record_tenant_idx ON usage_record (tenant_id, window_start)",
    """
    CREATE TABLE credit_record (
        credit_id     text PRIMARY KEY,
        lease_id      text NOT NULL,
        tenant_id     text NOT NULL,
        reason        text NOT NULL,
        amount        double precision NOT NULL CHECK (amount >= 0),
        created_at    timestamptz NOT NULL DEFAULT now(),
        submitted_at  timestamptz,
        billing_ref   text,
        UNIQUE (lease_id, reason)
    )
    """,
    """
    CREATE INDEX credit_record_unsubmitted_idx
        ON credit_record (created_at) WHERE submitted_at IS NULL
    """,

    # ------------------------------------------------------------------
    # published interruption rate — HLD §11: refreshed at least hourly
    # ------------------------------------------------------------------
    """
    CREATE TABLE interruption_stat (
        id            bigserial PRIMARY KEY,
        flavour       text NOT NULL,
        az            text NOT NULL,
        -- NULL means "all tenants": the published, customer-facing number. A
        -- non-NULL tenant row is the fairness signal from HLD 12.
        tenant_id     text,
        window_start  timestamptz NOT NULL,
        window_end    timestamptz NOT NULL,
        preemptions   integer NOT NULL DEFAULT 0,
        lease_hours   double precision NOT NULL DEFAULT 0,
        rate          double precision NOT NULL DEFAULT 0,
        sample_size   integer NOT NULL DEFAULT 0,
        computed_at   timestamptz NOT NULL DEFAULT now(),
        UNIQUE NULLS NOT DISTINCT (flavour, az, tenant_id, window_start)
    )
    """,
    """
    CREATE INDEX interruption_stat_published_idx
        ON interruption_stat (flavour, az, window_start DESC)
        WHERE tenant_id IS NULL
    """,

    # ------------------------------------------------------------------
    # internal auth replay protection — LLD §12.1
    # ------------------------------------------------------------------
    """
    CREATE TABLE hmac_nonce (
        nonce       text PRIMARY KEY,
        key_id      text NOT NULL DEFAULT 'current',
        seen_at     timestamptz NOT NULL DEFAULT now(),
        expires_at  timestamptz NOT NULL
    )
    """,
    "CREATE INDEX hmac_nonce_expiry_idx ON hmac_nonce (expires_at)",
    """
    COMMENT ON TABLE hmac_nonce IS
        'A valid signature replayed inside the clock-skew window would re-issue '
        'a reclaim. The nonce makes each signed request usable once.'
    """,

    # ------------------------------------------------------------------
    # singleton coordination — LLD §16
    # ------------------------------------------------------------------
    """
    CREATE TABLE leader_lock (
        name         text PRIMARY KEY,
        holder       text NOT NULL,
        fence        bigint NOT NULL DEFAULT 1,
        acquired_at  timestamptz NOT NULL DEFAULT now(),
        expires_at   timestamptz NOT NULL
    )
    """,
    """
    COMMENT ON TABLE leader_lock IS
        'Expiring lease with a monotonic fencing token. Used for work that must '
        'not run N times concurrently - the pool refresher and the analytics '
        'rollup. The grace reaper deliberately does NOT use this: it claims '
        'individual rows with SKIP LOCKED so it stays available while a leader '
        'election is in flight.'
    """,

    # ------------------------------------------------------------------
    # rate limiting — LLD §12.9
    # ------------------------------------------------------------------
    """
    CREATE TABLE rate_limit_bucket (
        tenant_id     text PRIMARY KEY,
        tokens        double precision NOT NULL,
        updated_at    timestamptz NOT NULL DEFAULT now(),
        blocked_until timestamptz
    )
    """,
    """
    COMMENT ON TABLE rate_limit_bucket IS
        'Shared token bucket so the limit is per tenant across all replicas, '
        'not per tenant per replica. HLD 12: automation that retries a 409 '
        'immediately turns a capacity shortage into a self-inflicted flood.'
    """,
]


_DROP_ORDER = (
    "rate_limit_bucket",
    "leader_lock",
    "hmac_nonce",
    "interruption_stat",
    "credit_record",
    "usage_record",
    "capacity_ledger_entry",
    "reclaim_order",
    "notice_delivery",
    "preemption_audit",
    "outbox",
    "idempotency_key",
    "spot_lease",
    "forecast_feed",
    "spot_pool_cooldown",
    "spot_pool",
    "host_group",
    "flavour",
    "tenant",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    # The audit table's DELETE trigger would block a DROP of its own rows, so
    # the trigger goes first. Dropping audit history is only ever correct in a
    # test database — in production this migration should never be reversed.
    op.execute("DROP TRIGGER IF EXISTS preemption_audit_no_update_trg ON preemption_audit")
    for table in _DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS spot_audit_chain() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS spot_audit_immutable() CASCADE")

/**
 * Effective configuration, and the invariants it has to satisfy.
 *
 * The grace budget panel is the reason this page is not just a settings dump.
 * force_stop_at + teardown_budget must stay below grace_seconds, or the
 * advertised grace period is a fiction — the customer is promised a window the
 * service cannot finish inside. The service refuses to start when that fails,
 * so what is shown here is always satisfied; showing it anyway makes the
 * relationship visible to whoever is about to change one of the three numbers.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import { statTile, banner, verdict, keyValues } from '../../lib/ui.js';
import { resource } from '../../lib/store.js';

const GROUPS = [
  ['Grace and teardown', [
    ['grace_seconds', 'Total window from notice to capacity returned. This is the number advertised to customers.'],
    ['force_stop_at', 'When the timer stops waiting for the guest. Must leave room for teardown.'],
    ['teardown_budget', 'Timeout on teardown before the lease is marked stalled.'],
  ]],
  ['Pool and feed', [
    ['control_cycle', 'Pool refresh interval, and the maximum staleness of published inventory.'],
    ['cooldown', 'Anti-thrash hold before reclaimed units are sellable again.'],
    ['degraded_factor', 'The fraction the pool falls back to when the feed is stale or low-confidence.'],
    ['min_feed_confidence', 'Below this, the feed counts as low-confidence.'],
    ['feed_stale_cycles', 'Feed older than this many cycles counts as stale.'],
  ]],
  ['Admission', [
    ['tenant_quota', 'Default per-tenant spot ceiling, in vCPU.'],
    ['idempotency_ttl', 'How long a retry still returns the original lease.'],
    ['retry_after', 'Value of the Retry-After header on a 409.'],
    ['max_units_per_request', 'Largest single launch accepted.'],
  ]],
  ['Preemption policy', [
    ['blast_radius', 'Most of one tenant\'s fleet a single reclaim wave may take.'],
  ]],
  ['Pricing', [
    ['min_discount', 'Discount floor — a shallow surplus.'],
    ['max_discount', 'Discount ceiling — a deep surplus.'],
    ['base_rate_per_unit_sec', 'List price per vCPU-second, before the spot discount.'],
  ]],
  ['Rate limiting', [
    ['rate_limit_enabled', 'Per-tenant token bucket, shared across replicas.'],
    ['rate_limit_burst', 'Bucket size.'],
    ['rate_limit_refill_per_sec', 'Refill rate.'],
    ['rate_limit_penalty_on_409', 'Extra cost charged to a client that ignored Retry-After.'],
  ]],
  ['Workers', [
    ['reaper_interval', 'How often stranded grace windows are swept.'],
    ['reaper_claim_ttl', 'How long a reaper claim on a lease is honoured.'],
    ['outbox_interval', 'Relay tick.'],
    ['outbox_max_attempts', 'Attempts before a row is parked rather than dropped.'],
    ['teardown_sweep_interval', 'How often stalled teardowns are retried.'],
    ['analytics_interval', 'How often the interruption rate is republished.'],
    ['leader_lease_ttl', 'Leader lease for the singleton loops.'],
  ]],
];

export default function createOpsConfig() {
  const config = resource(() => api.ops.config(), { interval: 30000 }).start();
  const health = resource(() => api.ops.health(), { interval: 5000 }).start();

  function budgetCard(settings, derived) {
    const total = settings.grace_seconds;
    const stopAt = settings.force_stop_at;
    const teardown = settings.teardown_budget;
    const headroom = derived.grace_headroom_seconds;

    const segments = [
      { label: 'Guest drains', width: stopAt / total, colour: 'var(--series-1)',
        note: `0–${stopAt}s · the guest decides, up to here` },
      { label: 'Teardown', width: teardown / total, colour: 'var(--series-2)',
        note: `${stopAt}–${stopAt + teardown}s · volumes, IPs and the ledger commit` },
      { label: 'Headroom', width: Math.max(0, headroom) / total, colour: 'var(--series-3)',
        note: `${fmt.num(headroom, 1)}s of slack against the advertised window` },
    ];

    return h('.card',
      h('.card-head',
        h('h2', 'The grace budget'),
        verdict(stopAt + teardown < total, { yes: 'invariant holds', no: 'advertised grace is a fiction' }),
      ),
      h('.card-note',
        `${stopAt}s + ${teardown}s must stay under ${total}s. If it does not, the service is `
        + 'promising a window it cannot finish inside — so the invariant is checked at startup '
        + 'and the process refuses to serve traffic when it fails.'),

      h('.stack-track', { style: { height: '28px' }, role: 'img',
        'aria-label': `Grace budget: guest ${stopAt}s, teardown ${teardown}s, headroom ${fmt.num(headroom, 1)}s` },
        segments.filter((s) => s.width > 0).map((segment) => h('.stack-seg', {
          key: segment.label,
          style: { width: `${segment.width * 100}%`, background: segment.colour },
        }))),

      h('.grid', { style: { gap: '6px', 'margin-top': '12px' } },
        segments.map((segment) => h('div', {
          key: segment.label,
          style: { display: 'flex', gap: '8px', 'align-items': 'baseline', 'font-size': '12px' },
        },
          h('span.legend-swatch', { style: { background: segment.colour } }),
          h('strong', segment.label),
          h('span', { style: { color: 'var(--text-muted)' } }, segment.note),
        ))),
    );
  }

  function settingsCards(settings) {
    return GROUPS.map(([title, keys]) => h('.card', { key: title },
      h('.card-head', h('h2', title)),
      keyValues(keys
        .filter(([key]) => settings[key] !== undefined)
        .map(([key, note]) => [
          h('span', { title: note },
            key.replace(/_/g, ' '),
            h('span', { style: { display: 'block', 'font-size': '11px', color: 'var(--text-muted)', 'max-width': '34ch' } }, note)),
          h('strong', formatValue(settings[key])),
        ])),
    ));
  }

  function formatValue(value) {
    if (typeof value === 'boolean') return value ? 'on' : 'off';
    if (typeof value === 'number') return Number.isInteger(value) ? fmt.num(value) : String(value);
    if (Array.isArray(value)) return value.join(', ');
    return String(value);
  }

  return {
    dispose: () => { config.stop(); health.stop(); },
    render: () => {
      const data = config.data;
      if (!data) return h('.page', h('.card', h('.empty', 'Loading…')));
      const settings = data.settings;
      const status = health.data;

      return h(`.page${config.stale ? '.stale' : ''}`,
        h('.page-head',
          h('div',
            h('h1', 'Configuration'),
            h('p.lede',
              'Every value is environment-driven and validated at startup, because a '
              + 'misconfigured spot control plane does not fail visibly — it quietly '
              + 'advertises a grace window it cannot honour.'),
          ),
        ),

        h('.grid.grid-4',
          statTile({ label: 'Environment', value: settings.environment,
            sub: `${settings.backend} backend · ${settings.region}` }),
          statTile({ label: 'Database', value: status?.database ?? '—',
            sub: status?.status === 'ok' ? 'ready to serve' : 'degraded',
            subKind: status?.status === 'ok' ? 'good' : 'bad' }),
          statTile({ label: 'Zones', value: fmt.num((settings.availability_zones || []).length),
            sub: (settings.availability_zones || []).join(', ') }),
          statTile({ label: 'Simulation endpoints', value: settings.enable_sim_endpoints ? 'enabled' : 'disabled',
            sub: settings.enable_sim_endpoints
              ? 'lab build — refused in production by config validation'
              : 'production build',
            subKind: settings.enable_sim_endpoints ? null : 'good' }),
        ),

        settings.enable_sim_endpoints && banner('warning',
          h('strong', 'Simulation endpoints are on. '),
          'They can fabricate forecast headroom and drive real reclaims. Config validation '
          + 'refuses to start with these enabled in production.'),

        !settings.internal_hmac_key && banner('critical',
          h('strong', 'No internal signing key is configured. '),
          'Signed-request verification is in development mode: /internal accepts unsigned '
          + 'calls. Production configuration refuses to start this way, because that endpoint '
          + 'can terminate every spot lease in a zone.'),

        budgetCard(settings, data.derived),
        h('.grid.grid-2', ...settingsCards(settings)),
      );
    },
  };
}

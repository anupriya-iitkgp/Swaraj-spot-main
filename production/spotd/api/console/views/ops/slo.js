/**
 * The non-functional targets, measured rather than asserted.
 *
 * Each tile here is one row of the design's target table with a number beside
 * it. Two are worth reading closely:
 *
 *   - **Over-allocation, target zero.** Any occurrence is a correctness bug,
 *     not a tuning issue, so it is rendered as pass/fail and never as a rate.
 *   - **Notice delivery, target 99.99% on at least one channel.** This is the
 *     trust anchor of the product. Per-channel rates sit underneath it because
 *     three channels do not help if they share a failure mode, and repeated
 *     simultaneous failure is the evidence that they do.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import { icons, statTile, banner, verdict } from '../../lib/ui.js';
import { bars, meter } from '../../lib/charts.js';
import { resource, notify } from '../../lib/store.js';

const CHANNEL_NAMES = {
  metadata: 'Instance metadata',
  webhook: 'Tenant webhook',
  event_stream: 'Event stream',
};

export default function createOpsSlo() {
  let hours = 24;

  const slo = resource(() => api.ops.slo(hours), { interval: 10000 }).start();
  const notice = resource(() => api.ops.noticeDelivery(hours), { interval: 10000 }).start();
  const fairness = resource(() => api.ops.fairness(hours), { interval: 15000 }).start();

  function setWindow(value) {
    hours = value;
    slo.refresh(); notice.refresh(); fairness.refresh();
    notify();
  }

  function reclaimCard(targets) {
    const perAz = targets.reclaim_within_grace.per_az;
    return h('.card',
      h('.card-head',
        h('h2', 'Reclaim completion'),
        verdict(targets.reclaim_within_grace.met),
      ),
      h('.card-note',
        '99.9% of reclaims complete within the advertised grace window, teardown included. '
        + 'The guaranteed classes are waiting on that capacity, so a reclaim that overruns is '
        + 'not only a customer promise broken but capacity that arrived late where it was needed.'),
      perAz.length === 0
        ? h('.empty', 'No reclaims closed inside this window.')
        : h('.table-wrap',
            h('table.data',
              h('thead', h('tr',
                h('th', 'Zone'), h('th.num', 'Reclaims'), h('th.num', 'Within grace'),
                h('th.num', 'Attainment'), h('th', ''),
              )),
              h('tbody', perAz.map((row) => h('tr', { key: row.az },
                h('td', row.az),
                h('td.num', fmt.num(row.total)),
                h('td.num', fmt.num(row.within)),
                h('td.num', fmt.pct(row.attainment, 3)),
                h('td', verdict(row.met)),
              ))),
            ),
          ),
    );
  }

  function noticeCard() {
    const data = notice.data;
    if (!data) return h('.card', h('.empty', 'Loading…'));

    const channels = Object.entries(data.per_channel || {}).map(([key, value]) => ({
      key,
      label: CHANNEL_NAMES[key] || key,
      value: value.rate ?? 0,
      attempts: value.attempts,
    }));
    const overall = data.at_least_one_channel_rate;

    return h('.card',
      h('.card-head',
        h('h2', 'Notice delivery'),
        verdict(overall == null ? null : overall >= 0.9999),
      ),
      h('.card-note',
        'At least one channel must reach the customer, 99.99% of the time. This is the trust '
        + 'anchor: a customer who loses instances without warning even occasionally stops '
        + 'adopting spot. All-channel failure is an SLO breach with an automatic credit.'),

      overall != null && h('div', { style: { 'margin-bottom': '16px' } },
        h('.stat',
          h('.stat-label', 'Reached on at least one channel'),
          h('.stat-value', fmt.pct(overall, 4)),
          meter(overall, { severity: overall >= 0.9999 ? null : 'critical' }),
        )),

      channels.length === 0
        ? h('.empty', 'No notices issued in this window.')
        : h('div',
            h('h3', { style: { 'margin-bottom': '8px', 'font-size': '13px', color: 'var(--text-secondary)' } },
              'Per channel'),
            bars(channels, { format: (v) => fmt.pct(v, 2) }),
            h('p', { style: { 'font-size': '12px', color: 'var(--text-muted)', 'margin-top': '10px' } },
              'Three channels do not help if they share a failure mode. Repeated simultaneous '
              + 'failure across all three is the signal that they are not as independent as the '
              + 'design assumes.'),
          ),
    );
  }

  function fairnessCard() {
    const data = fairness.data;
    if (!data) return h('.card', h('.empty', 'Loading…'));

    const spread = data.spread || {};
    // per_tenant is per (tenant, flavour, az); fairness is a per-tenant
    // question, so the rows are folded before they are ranked.
    const byTenant = new Map();
    for (const row of data.per_tenant || []) {
      byTenant.set(row.tenant_id, (byTenant.get(row.tenant_id) || 0) + (row.preemptions || 0));
    }
    const perTenant = [...byTenant.entries()]
      .map(([tenant, value]) => ({ key: tenant, label: tenant, value }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 12);

    const gini = spread.gini;
    const widening = spread.widening ?? spread.widening_signal;

    return h('.card',
      h('.card-head',
        h('h2', 'Cross-tenant fairness'),
        widening
          ? h('span.pill.warning', icons.warning({ size: 12 }), 'spread widening')
          : h('span.pill.good', icons.check({ size: 12 }), 'spread stable'),
      ),
      h('.card-note',
        'If reclaim consistently hits the same host groups, the same tenants absorb every '
        + 'interruption while others never do. Gini above 0.4 over five or more tenants means '
        + 'a minority is carrying it — and victim selection needs a fairness term.'),

      gini != null && h('div', { style: { 'margin-bottom': '16px' } },
        h('.stat',
          h('.stat-label', 'Gini coefficient of preemptions per tenant'),
          h('.stat-value', gini.toFixed(3)),
          meter(gini, { severity: gini > 0.4 ? 'warning' : null }),
          h('.stat-sub', `${spread.tenants ?? perTenant.length} tenant(s) in the window`),
        )),

      perTenant.length === 0
        ? h('.empty', 'No preemptions in this window.')
        : bars(perTenant, { format: (v) => fmt.num(v), slot: 2 }),
    );
  }

  return {
    dispose: () => { slo.stop(); notice.stop(); fairness.stop(); },
    render: () => {
      const data = slo.data;
      if (!data) return h('.page', h('.card', h('.empty', 'Loading…')));
      const targets = data.targets;

      const staleness = targets.pool_staleness;
      const overAllocation = targets.over_allocation;
      const freshness = targets.interruption_rate_freshness;

      return h(`.page${slo.stale ? '.stale' : ''}`,
        h('.page-head',
          h('div',
            h('h1', 'Service level'),
            h('p.lede', `Every non-functional target, measured over the last ${hours} hours.`),
          ),
          h('.page-head-actions',
            h('.mode-switch',
              [1, 6, 24, 168].map((value) => h('a', {
                key: value,
                href: '#',
                'aria-current': String(hours === value),
                onClick: (event) => { event.preventDefault(); setWindow(value); },
              }, value === 168 ? '7d' : `${value}h`)),
            ),
          ),
        ),

        overAllocation.detected > 0 && banner('critical',
          h('strong', `Over-allocation in ${overAllocation.detected} zone(s). `),
          overAllocation.note),

        h('.grid.grid-4',
          statTile({
            label: 'Over-allocation',
            value: overAllocation.detected === 0 ? 'zero' : fmt.num(overAllocation.detected),
            sub: overAllocation.detected === 0
              ? 'guaranteed by the atomic reserve'
              : 'a correctness bug, not a tuning issue',
            subKind: overAllocation.detected === 0 ? 'good' : 'bad',
          }),
          statTile({
            label: 'Pool staleness',
            value: fmt.duration(Math.max(...Object.values(staleness.per_az_seconds || { a: 0 }))),
            sub: `target ≤ one control cycle (${staleness.target_seconds}s)`,
            subKind: staleness.met ? 'good' : 'bad',
          }),
          statTile({
            label: 'Interruption rate freshness',
            value: freshness.actual_seconds == null ? 'never' : fmt.duration(freshness.actual_seconds),
            sub: 'target: republished at least hourly',
            subKind: freshness.met ? 'good' : 'bad',
          }),
          statTile({
            label: 'Notice delivery',
            value: targets.notice_delivery.at_least_one_channel_rate == null
              ? 'no data'
              : fmt.pct(targets.notice_delivery.at_least_one_channel_rate, 3),
            sub: 'target 99.99% on at least one channel',
            subKind: targets.notice_delivery.met == null ? null : targets.notice_delivery.met ? 'good' : 'bad',
          }),
        ),

        reclaimCard(targets),
        h('.grid.grid-2', noticeCard(), fairnessCard()),
      );
    },
  };
}

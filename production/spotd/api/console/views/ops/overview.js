/**
 * The operator overview.
 *
 * One `GET /console/overview` per refresh, not eight. A dashboard polling eight
 * endpoints at 1 Hz becomes the dominant client in its own latency histogram,
 * and the p99 an operator is reading stops being a measurement of customer
 * traffic — so the aggregation happens server-side and this page makes one call.
 *
 * The layout is ordered by what an operator checks first during an incident:
 * is anything over-allocated (a correctness bug, never a tuning issue), is
 * anything stuck mid-grace, is the pool degraded, and only then the history.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import { icons, statTile, banner, statePill, graceCountdown } from '../../lib/ui.js';
import { timeArea, stackedRows, bars, meter } from '../../lib/charts.js';
import { resource, notify, navigate, link, toast, toastError } from '../../lib/store.js';

const POOL_SERIES = [
  { key: 'reserved', name: 'Reserved by leases', slot: 1 },
  { key: 'cooldown', name: 'Cooling down', slot: 2 },
  { key: 'available', name: 'Sellable now', slot: 3 },
];

export default function createOpsOverview() {
  let tableView = false;

  const overview = resource(() => api.console_.overview(24), { interval: 2000 }).start();
  const timeline = resource(() => api.console_.timeline({ minutes: 60, buckets: 60 }), { interval: 15000 }).start();

  async function runCycle() {
    try {
      await api.console_.controlCycle();
      toast('ok', 'Control cycle run', 'The pool has been refreshed and expired cooldowns released.');
      overview.refresh();
    } catch (error) {
      toastError(error, 'Control cycle failed');
    }
  }

  function alerts(data) {
    const out = [];
    const overAllocated = data.slo.over_allocation.detected;
    if (overAllocated > 0) {
      out.push(banner('critical',
        h('strong', `Over-allocation detected in ${overAllocated} zone(s). `),
        'Leases hold more units than the pool admits, which means the atomic reserve was '
        + 'bypassed somewhere. This is a correctness bug, not a tuning issue.'));
    }
    const degraded = data.pools.filter((p) => p.degraded);
    if (degraded.length) {
      out.push(banner('warning',
        h('strong', `${degraded.length} zone(s) degraded. `),
        `The forecast feed is stale or low-confidence, so ${degraded.map((p) => p.az).join(', ')} `
        + 'has been cut to a conservative floor rather than extrapolated. The pool will not grow '
        + 'until the feed recovers.'));
    }
    if (data.outbox.dead > 0) {
      out.push(banner('warning',
        h('strong', `${data.outbox.dead} outbox row(s) parked. `),
        'These exhausted their retries. They are never dropped, but until they drain the ledger '
        + 'and the event view disagree.'));
    }
    const stalled = data.lease_states.STOPPED || 0;
    if (stalled > 3) {
      out.push(banner('warning',
        h('strong', `${stalled} leases sitting in STOPPED. `),
        'A lease stays STOPPED while its teardown is unconfirmed, and its units stay accounted '
        + 'for — never reported free.'));
    }
    return out;
  }

  function headline(data) {
    const totals = data.totals;
    const live = Object.entries(data.lease_states)
      .filter(([key]) => !['CLOSED', 'REJECTED'].includes(key))
      .reduce((sum, [, count]) => sum + count, 0);
    const reclaimReports = data.slo.reclaim;
    const attainment = reclaimReports.length
      ? reclaimReports.reduce((sum, r) => sum + r.attainment, 0) / reclaimReports.length
      : null;

    return h('.grid.grid-4',
      statTile({
        label: 'Sellable now',
        value: fmt.compact(totals.available_units),
        unit: 'vCPU',
        sub: `${fmt.pct(totals.utilisation, 0)} of the pool is reserved`,
        meter: meter(totals.utilisation),
      }),
      statTile({
        label: 'Live leases',
        value: fmt.num(live),
        sub: `${fmt.num(totals.reserved_units)} vCPU held across ${data.pools.length} zones`,
      }),
      statTile({
        label: 'In the grace window',
        value: fmt.num(data.in_grace.length),
        sub: data.in_grace.length
          ? 'the promise is running right now'
          : 'nothing being reclaimed',
        subKind: data.in_grace.length ? 'bad' : 'good',
      }),
      statTile({
        label: 'Reclaim SLO',
        value: attainment == null ? 'no data' : fmt.pct(attainment, 2),
        sub: attainment == null
          ? 'no reclaims closed in the window'
          : `target 99.9% within ${data.policy.grace_seconds}s, teardown included`,
        subKind: attainment == null ? null : attainment >= 0.999 ? 'good' : 'bad',
      }),
    );
  }

  function poolCard(data) {
    const rows = data.pools.map((pool) => ({
      label: pool.az,
      note: pool.degraded ? 'degraded' : `confidence ${pool.confidence.toFixed(2)}`,
      total: pool.sellable_units,
      segments: [
        { key: 'reserved', value: pool.reserved_units },
        { key: 'cooldown', value: pool.cooldown_units },
        { key: 'available', value: pool.available_units },
      ],
    }));

    return h('.card',
      h('.card-head',
        h('h2', 'Pool by zone'),
        h('.chart-toggle',
          h('button.btn.small.ghost', {
            onClick: () => { tableView = !tableView; notify(); },
          }, tableView ? 'Chart' : 'Table'),
        ),
      ),
      h('.card-note',
        'reserved + cooling down never exceeds sellable — that inequality is a database '
        + 'constraint, so an over-allocating code path fails its transaction instead of '
        + 'quietly overselling.'),
      stackedRows(rows, POOL_SERIES, { format: (v) => `${fmt.num(v)}`, tableView }),
    );
  }

  function historyCard() {
    const points = (timeline.data?.points ?? []).map((p) => ({ at: p.at, value: p.held_units }));
    const notices = (timeline.data?.points ?? [])
      .map((p, index) => ({ index, count: p.notices, forced: p.forced_stops }))
      .filter((p) => p.count > 0 || p.forced > 0)
      .map((p) => ({ index: p.index, kind: p.forced > 0 ? 'forced' : 'notice' }));

    return h('.card',
      h('.card-head', h('h2', 'Capacity held, last hour')),
      h('.card-note',
        'Recomputed from the lease table on every call rather than sampled, so the series is '
        + 'identical from any replica and has no hole where a restart was. Ticks on the '
        + 'baseline mark notices; red ticks mark forced stops.'),
      timeArea('capacity-history', points, {
        label: 'vCPU held',
        format: (v) => fmt.num(Math.round(v)),
        markers: notices,
      }),
    );
  }

  function gracePanel(data) {
    if (!data.in_grace.length) {
      return h('.card',
        h('.card-head', h('h2', 'Grace window')),
        h('.empty', 'Nothing is being reclaimed. When a reclaim order lands, every victim '
          + 'appears here with the deadline the reaper will act on.'));
    }
    return h('.card', { style: { 'border-color': 'var(--serious)' } },
      h('.card-head',
        h('h2', 'In the grace window'),
        h('span.pill.serious', `${data.in_grace.length} lease(s)`),
      ),
      h('.card-note',
        'Counted down against force_stop_deadline — the column the reaper claims on — not '
        + 'against a browser clock.'),
      h('.table-wrap',
        h('table.data',
          h('thead', h('tr',
            h('th', 'Lease'), h('th', 'Tenant'), h('th', 'State'),
            h('th.num', 'vCPU'), h('th', 'Host group'), h('th', 'Notice'),
            h('th', 'Force stop in'), h('th', 'Channels'),
          )),
          h('tbody', data.in_grace.map((lease) => h('tr', { key: lease.lease_id },
            h('td.mono', fmt.shortId(lease.lease_id, 12)),
            h('td', lease.tenant_id),
            h('td', statePill(lease.state)),
            h('td.num', lease.units),
            h('td', lease.host_group || '—'),
            h('td', fmt.time(lease.notice_at)),
            h('td', graceCountdown(lease.force_stop_deadline, lease.grace_seconds)),
            h('td', lease.notice_channels_delivered.length
              ? h('span.pill.good', icons.check({ size: 12 }), `${lease.notice_channels_delivered.length}/3`)
              : h('span.pill.critical', icons.warning({ size: 12 }), 'none')),
          ))),
        ),
      ),
    );
  }

  function statesCard(data) {
    const ORDER = ['REQUESTED', 'ADMITTED', 'PROVISIONING', 'RUNNING', 'NOTICE_ISSUED',
      'DRAINING', 'STOPPED', 'CLOSED', 'REJECTED'];
    const rows = ORDER
      .filter((stateName) => data.lease_states[stateName])
      .map((stateName) => ({
        key: stateName,
        label: stateName.replace(/_/g, ' ').toLowerCase(),
        value: data.lease_states[stateName],
      }));

    return h('.card',
      h('.card-head', h('h2', 'Leases by state')),
      h('.card-note',
        'A NOTICE_ISSUED count that is not draining means timers are stranded — the one '
        + 'failure a restart used to cause and the reaper now catches.'),
      rows.length ? bars(rows) : h('.empty', 'No leases yet.'),
    );
  }

  function ordersCard(data) {
    if (!data.reclaim_orders.length) {
      return h('.card',
        h('.card-head', h('h2', 'Recent reclaim orders')),
        h('.empty', 'No orders yet. Fire one from the Reclaim page.'));
    }
    return h('.card',
      h('.card-head',
        h('h2', 'Recent reclaim orders'),
        h('a.btn.small.ghost', link('/operator/reclaim'), 'Reclaim', icons.arrowRight({ size: 13 })),
      ),
      h('.table-wrap',
        h('table.data',
          h('thead', h('tr',
            h('th', 'Order'), h('th', 'Zone'), h('th.num', 'Asked'),
            h('th.num', 'Found'), h('th', 'State'), h('th', 'Received'),
          )),
          h('tbody', data.reclaim_orders.slice(0, 8).map((order) => h('tr.clickable', {
            key: order.order_id,
            onClick: () => navigate(`/operator/reclaim?order=${encodeURIComponent(order.order_id)}`),
          },
            h('td.mono', fmt.shortId(order.order_id, 14)),
            h('td', order.az),
            h('td.num', order.units),
            h('td.num', order.units_selected),
            h('td', order.partial
              ? h('span.pill.warning', icons.warning({ size: 12 }), 'partial')
              : h('span.pill', order.state.toLowerCase())),
            h('td', fmt.ago(order.received_at)),
          ))),
        ),
      ),
    );
  }

  function auditCard(data) {
    return h('.card',
      h('.card-head',
        h('h2', 'Audit trail'),
        h('a.btn.small.ghost', link('/operator/audit'), 'All', icons.arrowRight({ size: 13 })),
      ),
      h('.card-note', 'Append-only and hash-chained. Preemption disputes are settled from this or not at all.'),
      data.audit.length === 0
        ? h('.empty', 'No entries yet.')
        : h('.scroll-y', data.audit.slice(0, 25).map((entry) => h('.event-row', { key: entry.id },
            h('.event-time', fmt.time(entry.at)),
            h('div',
              h('.event-topic', fmt.eventLabel(entry.event)),
              h('.event-payload',
                entry.lease_id && h('span.tag', fmt.shortId(entry.lease_id, 12)),
                entry.tenant_id && h('span', { style: { 'margin-left': '6px' } }, entry.tenant_id),
              ),
            ),
          ))),
    );
  }

  function workersCard(data) {
    return h('.card',
      h('.card-head', h('h2', 'Workers and queues')),
      h('.card-note',
        'The reaper is the one loop that must not run twice on the same lease; it claims a '
        + 'lease with a conditional UPDATE before force-stopping it.'),
      h('.grid', { style: { gap: '6px' } },
        data.workers.map((worker) => h('div', {
          key: worker.name,
          style: { display: 'flex', 'align-items': 'center', gap: '8px', 'font-size': '13px' },
        },
          worker.running
            ? h('span', { style: { color: 'var(--good-text)' } }, icons.check({ size: 14 }))
            : h('span', { style: { color: 'var(--critical)' } }, icons.x({ size: 14 })),
          h('span', worker.name),
          worker.leader === true && h('span.pill', { title: 'This replica holds the leader lease for that loop.' }, 'leader'),
          worker.leader === false && h('span.pill.outline', 'follower'),
        )),
      ),
      h('div', { style: { 'margin-top': '14px', 'padding-top': '14px', 'border-top': '1px solid var(--grid)' } },
        h('div', { style: { display: 'flex', 'justify-content': 'space-between', 'font-size': '13px' } },
          h('span', 'Outbox pending'),
          h('strong', fmt.num(data.outbox.pending))),
        h('div', { style: { display: 'flex', 'justify-content': 'space-between', 'font-size': '13px', 'margin-top': '4px' } },
          h('span', 'Outbox parked'),
          h('strong', { style: { color: data.outbox.dead ? 'var(--critical)' : 'inherit' } },
            fmt.num(data.outbox.dead))),
      ),
    );
  }

  return {
    dispose: () => { overview.stop(); timeline.stop(); },
    render: () => {
      const data = overview.data;
      if (overview.error && !data) {
        return h('.page', h('.card',
          h('h1', 'Overview unavailable'),
          h('p.lede', overview.error.guidance || overview.error.message)));
      }
      if (!data) return h('.page', h('.card', h('.empty', 'Loading…')));

      return h(`.page${overview.stale ? '.stale' : ''}`,
        h('.page-head',
          h('div',
            h('h1', 'Operations'),
            h('p.lede',
              `${data.service.environment} · ${data.service.backend} backend · ${data.service.region}`
              + ` · refreshed ${fmt.time(data.at)}`),
          ),
          h('.page-head-actions',
            data.service.sim_enabled && h('button.btn.small', { onClick: runCycle },
              icons.refresh({ size: 14 }), 'Run control cycle'),
            h('a.btn.small.primary', link('/operator/reclaim'), icons.bolt({ size: 14 }), 'Reclaim'),
          ),
        ),

        ...alerts(data),
        headline(data),
        gracePanel(data),
        historyCard(),
        h('.grid.grid-2', poolCard(data), statesCard(data)),
        h('.grid.grid-2', ordersCard(data), workersCard(data)),
        auditCard(data),
      );
    },
  };
}

/**
 * Pools: the read model, its freshness, and whether the counter still agrees
 * with the leases that are supposed to back it.
 *
 * The reconciliation panel is the one that matters. The pool counter and the
 * sum of active lease units are two views of the same quantity, maintained by
 * different code paths, and the sign of the drift between them says which kind
 * of problem you have: positive drift under-sells (conservative, annoying);
 * negative drift means leases hold more than the pool admits, which means the
 * atomic reserve was bypassed and the guarantee is broken.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import { icons, statTile, verdict } from '../../lib/ui.js';
import { stackedRows, bars, meter, timeArea } from '../../lib/charts.js';
import { resource, notify, toast, toastError } from '../../lib/store.js';

const POOL_SERIES = [
  { key: 'reserved', name: 'Reserved by leases', slot: 1 },
  { key: 'cooldown', name: 'Cooling down', slot: 2 },
  { key: 'available', name: 'Sellable now', slot: 3 },
];

export default function createOpsPools() {
  let tableView = false;

  const overview = resource(() => api.console_.overview(24), { interval: 3000 }).start();
  const hostGroups = resource(() => api.console_.hostGroups(), { interval: 15000 }).start();
  const timeline = resource(() => api.console_.timeline({ minutes: 180, buckets: 60 }), { interval: 30000 }).start();

  async function runCycle() {
    try {
      const result = await api.console_.controlCycle();
      const released = Object.values(result.cooldown_released || {}).reduce((a, b) => a + b, 0);
      toast('ok', 'Control cycle run',
        released ? `${released} vCPU released from cooldown back into the pool.` : 'Pool refreshed.');
      overview.refresh();
    } catch (error) {
      toastError(error, 'Control cycle failed');
    }
  }

  async function toggleQuarantine(group) {
    const action = group.quarantined ? 'release' : 'quarantine';
    if (!confirm(
      group.quarantined
        ? `Return ${group.host_group} to the spot pool?`
        : `Quarantine ${group.host_group}?\n\nIt leaves the placement pool immediately. `
          + 'Leases already on it keep running.',
    )) return;
    try {
      if (group.quarantined) await api.console_.releaseQuarantine(group.host_group);
      else await api.console_.quarantine(group.host_group, 'operator');
      toast('ok', `Host ${action}d`, group.host_group);
      hostGroups.refresh();
      overview.refresh();
    } catch (error) {
      toastError(error, `Could not ${action} the host`);
    }
  }

  function freshnessCard(data) {
    const cycle = data.policy.control_cycle;
    const rows = data.pools.map((pool) => ({
      key: pool.az,
      label: pool.az,
      value: pool.staleness_seconds,
    }));
    const worst = Math.max(...rows.map((r) => r.value), 0);

    return h('.card',
      h('.card-head',
        h('h2', 'Feed freshness'),
        verdict(worst <= cycle * 2, { yes: 'within a cycle', no: 'stale' }),
      ),
      h('.card-note',
        `The pool is a projection refreshed every ${cycle}s and is stale by design. `
        + 'Anything older than about one cycle and the 409 rate stops being usable for '
        + 'customers, because the number they sized against was never true.'),
      bars(rows, { format: (v) => fmt.duration(v), max: Math.max(worst, cycle * 2) }),
    );
  }

  function reconciliationCard(data) {
    const rows = data.slo.over_allocation.per_az;
    const bad = rows.filter((r) => r.over_allocated);

    return h('.card', bad.length ? { style: { 'border-color': 'var(--critical)' } } : {},
      h('.card-head',
        h('h2', 'Reconciliation'),
        verdict(bad.length === 0, { yes: 'counters agree', no: 'over-allocated' }),
      ),
      h('.card-note',
        'reserved_units recomputed from the leases that should back it. Negative drift means '
        + 'leases hold more units than the pool admits — the atomic reserve was bypassed. '
        + 'Positive drift is conservative: it under-sells but never over-allocates.'),
      h('.table-wrap',
        h('table.data',
          h('thead', h('tr',
            h('th', 'Zone'), h('th.num', 'Pool counter'), h('th.num', 'Sum of lease units'),
            h('th.num', 'Drift'), h('th', ''),
          )),
          h('tbody', rows.map((row) => h('tr', { key: row.az },
            h('td', row.az),
            h('td.num', fmt.num(row.counter)),
            h('td.num', fmt.num(row.actual)),
            h('td.num', { style: { color: row.drift < 0 ? 'var(--critical)' : 'inherit' } },
              row.drift > 0 ? `+${row.drift}` : row.drift),
            h('td', row.over_allocated
              ? h('span.pill.critical', icons.warning({ size: 12 }), 'over-allocated')
              : h('span.pill.good', icons.check({ size: 12 }), 'consistent')),
          ))),
        ),
      ),
    );
  }

  function poolTable(data) {
    return h('.card',
      h('.card-head',
        h('h2', 'Pool detail'),
        h('button.btn.small.ghost', { onClick: () => { tableView = !tableView; notify(); } },
          tableView ? 'Chart' : 'Table'),
      ),
      h('.card-note',
        'sellable is what the forecast says can be sold; reserved is what leases hold; '
        + 'cooling down is reclaimed capacity parked before re-sale to damp thrash. '
        + 'available is what is left, and the only number a launch can succeed against.'),
      stackedRows(
        data.pools.map((pool) => ({
          label: pool.az,
          note: pool.degraded ? 'degraded — conservative floor' : `confidence ${pool.confidence.toFixed(2)}`,
          total: pool.sellable_units,
          segments: [
            { key: 'reserved', value: pool.reserved_units },
            { key: 'cooldown', value: pool.cooldown_units },
            { key: 'available', value: pool.available_units },
          ],
        })),
        POOL_SERIES,
        { tableView },
      ),
      h('.table-wrap', { style: { 'margin-top': '18px' } },
        h('table.data',
          h('thead', h('tr',
            h('th', 'Zone'), h('th.num', 'Sellable'), h('th.num', 'Reserved'),
            h('th.num', 'Cooldown'), h('th.num', 'Available'), h('th.num', 'Utilisation'),
            h('th.num', 'Confidence'), h('th.num', 'Cycle'), h('th', 'Age'),
          )),
          h('tbody', data.pools.map((pool) => h('tr', { key: pool.az },
            h('td', pool.az, pool.degraded && h('span.pill.warning', { style: { 'margin-left': '8px' } }, 'degraded')),
            h('td.num', fmt.num(pool.sellable_units)),
            h('td.num', fmt.num(pool.reserved_units)),
            h('td.num', fmt.num(pool.cooldown_units)),
            h('td.num', fmt.num(pool.available_units)),
            h('td.num', fmt.pct(pool.utilisation, 0)),
            h('td.num', pool.confidence.toFixed(2)),
            h('td.num', fmt.num(pool.cycle_seq)),
            h('td', fmt.duration(pool.staleness_seconds)),
          ))),
        ),
      ),
    );
  }

  function hostGroupCard() {
    const groups = hostGroups.data?.host_groups ?? [];
    if (!groups.length) return null;
    const quarantined = groups.filter((g) => g.quarantined);

    return h('.card',
      h('.card-head',
        h('h2', 'Host groups'),
        quarantined.length
          ? h('span.pill.warning', `${quarantined.length} quarantined`)
          : h('span.pill.good', 'all in the pool'),
      ),
      h('.card-note',
        'A host whose agent proved unreachable is quarantined out of the spot pool rather than '
        + 'repeatedly selected and repeatedly failed. Leases already on it keep running.'),
      h('.table-wrap',
        h('table.data',
          h('thead', h('tr',
            h('th', 'Host group'), h('th', 'Zone'), h('th.num', 'Capacity'), h('th', 'Status'), h('th', ''),
          )),
          h('tbody', groups.map((group) => h('tr', { key: group.host_group },
            h('td.mono', group.host_group),
            h('td', group.az),
            h('td.num', fmt.units(group.total_units)),
            h('td', group.quarantined
              ? h('span.pill.warning', icons.warning({ size: 12 }), 'quarantined')
              : h('span.pill.good', icons.check({ size: 12 }), 'in pool')),
            h('td', h('button.btn.small.ghost', { onClick: () => toggleQuarantine(group) },
              group.quarantined ? 'Return to pool' : 'Quarantine')),
          ))),
        ),
      ),
    );
  }

  return {
    dispose: () => { overview.stop(); hostGroups.stop(); timeline.stop(); },
    render: () => {
      const data = overview.data;
      if (!data) return h('.page', h('.card', h('.empty', 'Loading…')));
      const totals = data.totals;

      return h(`.page${overview.stale ? '.stale' : ''}`,
        h('.page-head',
          h('div',
            h('h1', 'Pools'),
            h('p.lede',
              'A cached projection of sellable spot per zone, refreshed each control cycle. '
              + 'It is never authoritative — the reserve is the decision, and this is the hint.'),
          ),
          h('.page-head-actions',
            data.service.sim_enabled && h('button.btn.small', { onClick: runCycle },
              icons.refresh({ size: 14 }), 'Run control cycle'),
          ),
        ),

        h('.grid.grid-4',
          statTile({ label: 'Sellable', value: fmt.compact(totals.sellable_units), unit: 'vCPU',
            sub: 'what the forecast says can be sold' }),
          statTile({ label: 'Reserved', value: fmt.compact(totals.reserved_units), unit: 'vCPU',
            sub: `${fmt.pct(totals.utilisation, 0)} utilisation`, meter: meter(totals.utilisation) }),
          statTile({ label: 'Cooling down', value: fmt.compact(totals.cooldown_units), unit: 'vCPU',
            sub: `held ${fmt.duration(data.policy.cooldown)} before re-sale` }),
          statTile({ label: 'Available', value: fmt.compact(totals.available_units), unit: 'vCPU',
            sub: 'the only number a launch can succeed against' }),
        ),

        poolTable(data),
        h('.grid.grid-2', reconciliationCard(data), freshnessCard(data)),

        h('.card',
          h('.card-head', h('h2', 'Capacity held, last three hours')),
          h('.card-note', 'Derived from lease timestamps, so it agrees with the ledger by construction.'),
          timeArea('pool-history',
            (timeline.data?.points ?? []).map((p) => ({ at: p.at, value: p.held_units })),
            { label: 'vCPU held', format: (v) => fmt.num(Math.round(v)) }),
        ),

        hostGroupCard(),
      );
    },
  };
}

/**
 * Every tenant's leases, filtered.
 *
 * The filter row sits above everything it scopes rather than inside each card,
 * so one change re-slices the whole page and there is never a card showing a
 * different slice from its neighbour.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import { icons, statePill, graceCountdown, statTile, field, select } from '../../lib/ui.js';
import { bars } from '../../lib/charts.js';
import { resource, notify, navigate } from '../../lib/store.js';

const STATES = ['REQUESTED', 'ADMITTED', 'PROVISIONING', 'RUNNING', 'NOTICE_ISSUED',
  'DRAINING', 'STOPPED', 'CLOSED', 'REJECTED'];

export default function createOpsFleet() {
  const filters = { state: '', az: '', tenant_id: '', host_group: '' };

  const overview = resource(() => api.console_.overview(24), { interval: 5000 }).start();
  const leases = resource(() => api.console_.leases({
    state: filters.state || undefined,
    az: filters.az || undefined,
    tenant_id: filters.tenant_id || undefined,
    host_group: filters.host_group || undefined,
    limit: 300,
  }), { interval: 3000 }).start();

  function applyFilters() {
    leases.refresh();
    notify();
  }

  function filterBar(data) {
    const zones = data ? ['', ...data.policy.availability_zones] : [''];
    return h('.filter-bar',
      field('State', select({
        value: filters.state,
        onChange: (event) => { filters.state = event.target.value; applyFilters(); },
      }, [{ value: '', label: 'any state' }, ...STATES.map((s) => ({ value: s, label: s.replace(/_/g, ' ').toLowerCase() }))])),

      field('Zone', select({
        value: filters.az,
        onChange: (event) => { filters.az = event.target.value; applyFilters(); },
      }, zones.map((z) => ({ value: z, label: z || 'any zone' })))),

      field('Tenant', h('input.input', {
        value: filters.tenant_id,
        placeholder: 'any tenant',
        onChange: (event) => { filters.tenant_id = event.target.value.trim(); applyFilters(); },
      })),

      field('Host group', h('input.input', {
        value: filters.host_group,
        placeholder: 'any host',
        onChange: (event) => { filters.host_group = event.target.value.trim(); applyFilters(); },
      })),

      h('button.btn.small', { onClick: () => leases.refresh() },
        icons.refresh({ size: 14 }), 'Refresh'),

      (filters.state || filters.az || filters.tenant_id || filters.host_group)
        && h('button.btn.small.ghost', {
          onClick: () => {
            Object.keys(filters).forEach((key) => { filters[key] = ''; });
            applyFilters();
          },
        }, 'Clear'),
    );
  }

  function summary() {
    const rows = leases.data ?? [];
    const live = rows.filter((l) => !['CLOSED', 'REJECTED'].includes(l.state));
    const grace = rows.filter((l) => ['NOTICE_ISSUED', 'DRAINING'].includes(l.state));
    const stalled = rows.filter((l) => l.teardown_stalled);
    const tenants = new Set(rows.map((l) => l.tenant_id));

    return h('.grid.grid-4',
      statTile({ label: 'Matching leases', value: fmt.num(rows.length),
        sub: `${fmt.num(live.length)} live · ${tenants.size} tenant(s)` }),
      statTile({ label: 'Units held', value: fmt.compact(live.reduce((sum, l) => sum + l.units, 0)), unit: 'vCPU',
        sub: 'across the current filter' }),
      statTile({ label: 'In grace', value: fmt.num(grace.length),
        sub: grace.length ? 'being reclaimed now' : 'none interrupted', subKind: grace.length ? 'bad' : 'good' }),
      statTile({ label: 'Teardown stalled', value: fmt.num(stalled.length),
        sub: stalled.length ? 'units held, never reported free' : 'all teardowns confirmed',
        subKind: stalled.length ? 'bad' : 'good' }),
    );
  }

  function byTenantCard() {
    const rows = leases.data ?? [];
    const live = rows.filter((l) => !['CLOSED', 'REJECTED'].includes(l.state));
    const grouped = new Map();
    for (const lease of live) {
      grouped.set(lease.tenant_id, (grouped.get(lease.tenant_id) || 0) + lease.units);
    }
    const series = [...grouped.entries()]
      .map(([tenant, value]) => ({ key: tenant, label: tenant, value }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 12);

    if (!series.length) return null;
    return h('.card',
      h('.card-head', h('h2', 'Units held by tenant')),
      h('.card-note', 'Live leases only. The denominator the blast-radius cap is computed against.'),
      bars(series, { format: (v) => `${fmt.num(v)} vCPU` }),
    );
  }

  function table() {
    const rows = leases.data ?? [];
    if (!rows.length) {
      return h('.card', h('.empty',
        leases.error
          ? `Could not load: ${leases.error.message}`
          : leases.data == null
            ? 'Loading the fleet…'
            : 'No leases match this filter.'));
    }
    return h('.card',
      h('.table-wrap',
        h('table.data',
          h('thead', h('tr',
            h('th', 'Lease'), h('th', 'Tenant'), h('th', 'State'), h('th', 'Flavour'),
            h('th.num', 'vCPU'), h('th', 'Zone'), h('th', 'Host group'),
            h('th.num', 'Discount'), h('th.num', 'Billed'), h('th', 'Grace'), h('th', 'Age'),
          )),
          h('tbody', rows.map((lease) => h('tr.clickable', {
            key: lease.lease_id,
            onClick: () => navigate(`/leases/${lease.lease_id}`),
            title: 'Opens the tenant-facing view of this lease',
          },
            h('td.mono', fmt.shortId(lease.lease_id, 12)),
            h('td', lease.tenant_id),
            h('td', statePill(lease.state)),
            h('td', lease.flavour, lease.count > 1 && h('span.tag', { style: { 'margin-left': '6px' } }, `×${lease.count}`)),
            h('td.num', lease.units),
            h('td', lease.az),
            h('td', lease.host_group || h('span', { style: { color: 'var(--text-muted)' } }, 'unplaced')),
            h('td.num', fmt.pct(lease.discount, 0)),
            h('td.num', fmt.money(lease.billed_amount)),
            h('td', lease.notice_at && !lease.stopped_at
              ? graceCountdown(lease.force_stop_deadline, lease.grace_seconds)
              : lease.forced_stop
                ? h('span.pill.warning', 'forced')
                : h('span', { style: { color: 'var(--text-muted)' } }, '—')),
            h('td', fmt.ago(lease.created_at)),
          ))),
        ),
      ),
    );
  }

  return {
    dispose: () => { overview.stop(); leases.stop(); },
    render: () => h(`.page${leases.stale ? '.stale' : ''}`,
      h('.page-head',
        h('div',
          h('h1', 'Fleet'),
          h('p.lede',
            'Every tenant\'s leases. A cross-tenant read, which is why it lives behind an '
            + 'operator session and not on the customer API.'),
        ),
      ),
      filterBar(overview.data),
      summary(),
      table(),
      byTenantCard(),
    ),
  };
}

/**
 * Tenants: entitlement, quota and how close each one is to its ceiling.
 *
 * The quota headroom column is what an operator wants when a tenant reports a
 * 429. The ceiling is enforced before any capacity work is done, so a tenant at
 * their limit is rejected without the pool ever being consulted — which is why
 * "no capacity" and "quota exceeded" are different rejections with different
 * fixes, and why the console shows them apart.
 *
 * The burst control is here because this is where tenants are. It races N
 * launches at the pool simultaneously: some are admitted, the rest take a 409,
 * and the reserved total never exceeds what was sellable. That is the one
 * guarantee the whole admission design exists to provide, and it is more
 * convincing watched than asserted.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import { icons, statTile, banner, field, select } from '../../lib/ui.js';
import { bars, meter } from '../../lib/charts.js';
import { resource, notify, toast, toastError, setTenant, navigate } from '../../lib/store.js';

export default function createOpsTenants() {
  const tenants = resource(() => api.console_.tenants(), { interval: 5000 }).start();
  const overview = resource(() => api.console_.overview(24), { interval: 10000 }).start();

  const burst = { tenant: '', flavour: 's1.medium', az: '', launches: 6, count: 1, busy: false, result: null };

  async function runBurst(event) {
    event.preventDefault();
    burst.busy = true;
    burst.result = null;
    notify();
    try {
      burst.result = await api.console_.burst({
        tenant_id: burst.tenant,
        flavour: burst.flavour,
        az: burst.az,
        count: Number(burst.count),
        launches: Number(burst.launches),
      });
      toast(
        'ok',
        'Burst complete',
        `${burst.result.admitted} admitted, ${burst.result.rejected} rejected. `
        + 'Every rejection is a 409 — never a partial allocation.',
      );
      tenants.refresh();
      overview.refresh();
    } catch (error) {
      toastError(error, 'Burst failed');
    } finally {
      burst.busy = false;
      notify();
    }
  }

  function quotaTable() {
    const rows = tenants.data?.tenants ?? [];
    if (!rows.length) return h('.card', h('.empty', 'No tenants configured.'));

    return h('.card',
      h('.card-head', h('h2', 'Tenants')),
      h('.card-note',
        'Entitlement is re-read from the account service on every launch and never inferred '
        + 'from the request — a caller-supplied class is not trusted. An unknown tenant fails '
        + 'closed rather than being guessed at.'),
      h('.table-wrap',
        h('table.data',
          h('thead', h('tr',
            h('th', 'Tenant'), h('th', 'Name'), h('th', 'Class'), h('th', 'Spot'),
            h('th.num', 'Quota'), h('th.num', 'In use'), h('th.num', 'Headroom'),
            h('th', 'Utilisation'), h('th.num', 'Live leases'), h('th', ''),
          )),
          h('tbody', rows.map((tenant) => {
            const used = tenant.quota_units ? tenant.units_in_use / tenant.quota_units : 0;
            return h('tr', { key: tenant.tenant_id },
              h('td.mono', tenant.tenant_id),
              h('td', tenant.name),
              h('td', h('span.pill', tenant.account_class)),
              h('td', tenant.spot_entitled
                ? h('span.pill.good', icons.check({ size: 12 }), 'entitled')
                : h('span.pill', icons.x({ size: 12 }), 'not entitled')),
              h('td.num', fmt.num(tenant.quota_units)),
              h('td.num', fmt.num(tenant.units_in_use)),
              h('td.num', { style: { color: tenant.headroom_units === 0 ? 'var(--critical)' : 'inherit' } },
                fmt.num(tenant.headroom_units)),
              h('td', { style: { width: '110px' } }, meter(used)),
              h('td.num', fmt.num(tenant.live_leases)),
              h('td', h('button.btn.small.ghost', {
                onClick: () => {
                  setTenant(tenant.tenant_id);
                  navigate('/leases');
                },
                title: 'Switch the tenant console to this tenant',
              }, 'View as tenant')),
            );
          })),
        ),
      ),
    );
  }

  function usageCard() {
    const rows = (tenants.data?.tenants ?? [])
      .filter((tenant) => tenant.units_in_use > 0)
      .map((tenant) => ({
        key: tenant.tenant_id,
        label: tenant.tenant_id,
        value: tenant.units_in_use,
      }))
      .sort((a, b) => b.value - a.value);

    if (!rows.length) return null;
    return h('.card',
      h('.card-head', h('h2', 'Units in use')),
      h('.card-note',
        'Includes leases in STOPPED: a lease whose teardown is unconfirmed still occupies '
        + 'capacity, so it still counts against the ceiling — which matters most during a '
        + 'wave of preemptions.'),
      bars(rows, { format: (v) => `${fmt.num(v)} vCPU` }),
    );
  }

  function burstCard() {
    const data = overview.data;
    if (!data?.service.sim_enabled) return null;
    const zones = data.policy.availability_zones;
    if (!burst.az) burst.az = zones[0] || '';
    if (!burst.tenant) burst.tenant = tenants.data?.tenants?.[0]?.tenant_id || '';

    return h('.card',
      h('.card-head', h('h2', 'Race the admission controller')),
      h('.card-note',
        'Fires N launches simultaneously against the same pool. The pool read they all start '
        + 'from is the same stale number, so the reserve is what settles it: the winners get '
        + 'their units, the losers get a 409 with Retry-After, and the reserved total never '
        + 'exceeds what was sellable. Zero over-allocation is the guarantee, not a target.'),

      h('form', { onSubmit: runBurst },
        h('.form-row',
          field('Tenant', h('input.input', {
            value: burst.tenant,
            onInput: (event) => { burst.tenant = event.target.value.trim(); },
          })),
          field('Flavour', h('input.input', {
            value: burst.flavour,
            onInput: (event) => { burst.flavour = event.target.value.trim(); },
          })),
          field('Zone', select({
            value: burst.az,
            onChange: (event) => { burst.az = event.target.value; notify(); },
          }, zones)),
          field('Concurrent launches', h('input.input', {
            type: 'number', min: '2', max: '32', value: burst.launches,
            onInput: (event) => { burst.launches = event.target.value; },
          })),
        ),
        h('button.btn.primary', { type: 'submit', disabled: burst.busy || !burst.tenant, style: { 'margin-top': '12px' } },
          icons.bolt({ size: 15 }), burst.busy ? 'Racing…' : 'Run burst'),
      ),

      burst.result && h('div', { style: { 'margin-top': '18px' } },
        h('.grid.grid-3',
          h('.stat', h('.stat-label', 'Admitted'), h('.stat-value', fmt.num(burst.result.admitted)),
            h('.stat-sub', `${fmt.num(burst.result.units_admitted)} vCPU reserved`)),
          h('.stat', h('.stat-label', 'Rejected'), h('.stat-value', fmt.num(burst.result.rejected)),
            h('.stat-sub', 'every one a 409, never a partial allocation')),
          h('.stat', h('.stat-label', 'Pool after'),
            h('.stat-value', fmt.num(burst.result.pool_after?.available_units ?? 0)),
            h('.stat-sub', `of ${fmt.num(burst.result.pool_after?.sellable_units ?? 0)} sellable`)),
        ),
        h('.table-wrap', { style: { 'margin-top': '14px' } },
          h('table.data',
            h('thead', h('tr', h('th.num', '#'), h('th', 'Outcome'), h('th', 'Detail'))),
            h('tbody', burst.result.results.map((row) => h('tr', { key: row.index },
              h('td.num', row.index + 1),
              h('td', row.admitted
                ? h('span.pill.good', icons.check({ size: 12 }), 'admitted')
                : h('span.pill.warning', row.code)),
              h('td', row.admitted
                ? h('span.mono', fmt.shortId(row.lease_id, 18))
                : h('span', { style: { color: 'var(--text-secondary)' } },
                    row.message, row.retry_after ? ` · retry after ${row.retry_after}s` : '')),
            ))),
          )),
      ),
    );
  }

  return {
    dispose: () => { tenants.stop(); overview.stop(); },
    render: () => {
      const rows = tenants.data?.tenants ?? [];
      const entitled = rows.filter((tenant) => tenant.spot_entitled);
      const atCeiling = rows.filter((tenant) => tenant.headroom_units === 0 && tenant.quota_units > 0);

      return h(`.page${tenants.stale ? '.stale' : ''}`,
        h('.page-head',
          h('div',
            h('h1', 'Tenants'),
            h('p.lede',
              'Who may buy spot, how much of it, and how close they are to the ceiling. '
              + 'The quota is enforced before any capacity work is done.'),
          ),
        ),

        h('.grid.grid-4',
          statTile({ label: 'Tenants', value: fmt.num(rows.length),
            sub: `${entitled.length} entitled to spot` }),
          statTile({ label: 'Units in use', value: fmt.compact(rows.reduce((sum, t) => sum + t.units_in_use, 0)),
            unit: 'vCPU', sub: 'across every tenant' }),
          statTile({ label: 'At their ceiling', value: fmt.num(atCeiling.length),
            sub: atCeiling.length ? 'further launches will be 429' : 'everyone has headroom',
            subKind: atCeiling.length ? 'bad' : 'good' }),
          statTile({ label: 'Live leases', value: fmt.num(rows.reduce((sum, t) => sum + t.live_leases, 0)),
            sub: 'held across all tenants' }),
        ),

        atCeiling.length > 0 && banner('info',
          h('strong', `${atCeiling.length} tenant(s) at their quota. `),
          'Their next launch is a 429 with the quota, usage and request size in the body — '
          + 'a different rejection from a 409, with a different fix.'),

        quotaTable(),
        h('.grid.grid-2', usageCard(), burstCard()),
      );
    },
  };
}

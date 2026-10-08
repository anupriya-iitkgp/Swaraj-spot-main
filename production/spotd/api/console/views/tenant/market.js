/**
 * The market: what is for sale, and the launch form.
 *
 * This page is the customer contract, so it only uses the customer API —
 * `/spot/inventory`, `/spot/interruptions`, `/v1/instances`. Nothing here reads
 * a privileged endpoint, which means the page is also a check on the contract:
 * if a tenant cannot size a workload from what is rendered here, the published
 * API is missing something.
 *
 * Three things are shown next to the price because the design says a customer
 * cannot buy spot honestly without them:
 *
 *   - the **interruption rate** (HLD §11: "Customers cannot size spot workloads
 *     without it"), published even when unflattering;
 *   - the **staleness** of the number, since inventory is a projection refreshed
 *     once per control cycle and is stale by design;
 *   - the **degraded** flag, when the forecast feed is low-confidence and the
 *     pool has been cut to a conservative floor — a customer sizing a burst
 *     deserves to know the number in front of them is defensive.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import { icons, statTile, banner, field, select } from '../../lib/ui.js';
import { meter } from '../../lib/charts.js';
import { state, resource, toast, toastError, navigate, notify } from '../../lib/store.js';

export default function createMarket() {
  const inventory = resource(() => api.spot.inventory(), { interval: 5000 }).start();

  const form = {
    flavour: '',
    az: '',
    count: 1,
    idempotencyKey: '',
    viaGateway: true,
    submitting: false,
    lastResult: null,
  };

  function entries() {
    return inventory.data?.pools ?? [];
  }

  async function submit(event) {
    event.preventDefault();
    if (!state.tenantId) {
      toast('error', 'No tenant selected', 'Set a tenant id in the header first — a spot launch is never anonymous.');
      return;
    }
    form.submitting = true;
    notify();

    const key = form.idempotencyKey.trim() || `console-${Date.now().toString(36)}`;
    const body = { flavour: form.flavour, az: form.az, count: Number(form.count) || 1 };
    const call = form.viaGateway ? api.spot.launchViaGateway : api.spot.launch;

    try {
      const result = await call(state.tenantId, body, key);
      form.lastResult = result;
      form.idempotencyKey = key;   // keep it, so the replay can be demonstrated
      toast(
        result.idempotent_replay ? 'info' : 'ok',
        result.idempotent_replay ? 'Idempotent replay' : 'Lease admitted',
        result.idempotent_replay
          ? 'Same key, same lease — the retry allocated nothing new.'
          : `${result.lease.lease_id} · ${result.lease.units} vCPU reserved. Provisioning runs off the request path.`,
      );
      inventory.refresh();
    } catch (error) {
      form.lastResult = null;
      toastError(error, error.code === 'no_capacity' ? 'No capacity (409)' : 'Launch refused');
    } finally {
      form.submitting = false;
      notify();
    }
  }

  function launchCard() {
    const rows = entries();
    const flavours = [...new Set(rows.map((r) => r.flavour))];
    const zones = [...new Set(rows.map((r) => r.az))];
    if (!form.flavour && flavours.length) form.flavour = flavours[0];
    if (!form.az && zones.length) form.az = zones[0];

    const chosen = rows.find((r) => r.flavour === form.flavour && r.az === form.az);
    const requested = (chosen?.vcpu ?? 0) * (Number(form.count) || 0);
    const fits = chosen ? requested <= chosen.available_units : true;

    return h('.card',
      h('.card-head', h('h2', 'Launch')),
      h('.card-note',
        'Admission is a reserve, not a read. The availability above can be a full '
        + 'control cycle out of date, so a launch may still come back 409 — that is '
        + 'normal traffic on a busy pool, not an incident.'),

      h('form', { onSubmit: submit },
        h('.form-row',
          field('Flavour', select({
            value: form.flavour,
            onChange: (e) => { form.flavour = e.target.value; notify(); },
          }, flavours.length ? flavours : ['—'])),

          field('Zone', select({
            value: form.az,
            onChange: (e) => { form.az = e.target.value; notify(); },
          }, zones.length ? zones : ['—'])),

          field('Instances', h('input.input', {
            type: 'number', min: '1', max: '64', value: form.count,
            onInput: (e) => { form.count = e.target.value; notify(); },
          }), chosen ? `${requested} vCPU requested` : null),
        ),

        h('.form-row', { style: { 'margin-top': '12px' } },
          field('Idempotency key',
            h('input.input', {
              value: form.idempotencyKey,
              placeholder: 'generated if left blank',
              onInput: (e) => { form.idempotencyKey = e.target.value; },
            }),
            'Submit twice with the same key: the second call returns the original lease, not a second one.'),
        ),

        h('label', {
          style: { display: 'flex', gap: '8px', 'align-items': 'flex-start', 'margin-top': '12px', 'font-size': '12px', color: 'var(--text-secondary)' },
        },
          h('input', {
            type: 'checkbox', checked: form.viaGateway,
            onChange: (e) => { form.viaGateway = e.target.checked; notify(); },
            style: { 'margin-top': '2px' },
          }),
          h('span', 'Route through the gateway (POST /v1/instances) so the account class is looked up '
            + 'and non-spot classes are refused as out of scope. Unchecked, the request goes straight '
            + 'to POST /spot/leases.'),
        ),

        !fits && chosen && h('div', { style: { 'margin-top': '12px' } },
          banner('warning',
            h('strong', 'That is more than the pool is advertising. '),
            `${requested} vCPU requested, ${chosen.available_units} available in ${form.az}. `
            + 'You can still submit — the reserve is the decision, not this number.')),

        h('div', { style: { 'margin-top': '14px', display: 'flex', gap: '10px', 'align-items': 'center' } },
          h('button.btn.primary', { type: 'submit', disabled: form.submitting || !state.tenantId },
            icons.bolt({ size: 15 }),
            form.submitting ? 'Reserving…' : 'Launch'),
          !state.tenantId && h('span', { style: { 'font-size': '12px', color: 'var(--text-muted)' } },
            'Set a tenant id in the header first.'),
        ),
      ),

      form.lastResult && h('div', { style: { 'margin-top': '16px' } },
        banner('info',
          h('strong', form.lastResult.idempotent_replay ? 'Replayed. ' : 'Admitted. '),
          form.lastResult.message, ' ',
          h('a', {
            href: `/leases/${form.lastResult.lease.lease_id}`,
            onClick: (e) => { e.preventDefault(); navigate(`/leases/${form.lastResult.lease.lease_id}`); },
          }, 'Follow the lease'),
        ),
      ),
    );
  }

  function inventoryTable() {
    const rows = entries();
    const staleness = rows.length ? Math.max(...rows.map((r) => r.pool_staleness_seconds)) : null;
    const cycle = inventory.data?.control_cycle_seconds;

    return h('.card',
      h('.card-head',
        h('h2', 'Availability and price'),
        staleness != null && h('span.pill', {
          title: `The pool is refreshed every ${cycle}s. Anything older than one cycle is stale by design.`,
        }, `refreshed ${fmt.duration(staleness)} ago`),
      ),
      h('.card-note',
        'Price is the discounted rate; the discount tracks how deep the surplus is, '
        + 'and is frozen onto a lease at admission — a later change never re-rates a running lease.'),

      rows.length === 0
        // "Nothing for sale" and "not answered yet" are different facts, and a
        // customer sizing a burst must not read the second as the first.
        ? h('.empty', inventory.error
            ? `Inventory unavailable: ${inventory.error.message}`
            : inventory.data == null
              ? 'Loading inventory…'
              : 'No sellable capacity is being advertised right now.')
        : h('.table-wrap',
            h('table.data',
              h('thead', h('tr',
                h('th', 'Flavour'),
                h('th', 'Zone'),
                h('th.num', 'vCPU'),
                h('th.num', 'Memory'),
                h('th.num', 'Available'),
                h('th.num', 'Discount'),
                h('th.num', 'Spot / hour'),
                h('th.num', 'Interruption rate'),
                h('th', ''),
              )),
              h('tbody', rows.map((row) => h('tr', { key: `${row.az}/${row.flavour}` },
                h('td', { style: { 'font-weight': '500' } }, row.flavour),
                h('td', row.az),
                h('td.num', row.vcpu),
                h('td.num', `${row.memory_gb} GB`),
                h('td.num',
                  h('div', `${fmt.num(row.available_instances)}×`),
                  h('div', { style: { 'font-size': '11px', color: 'var(--text-muted)' } },
                    `${fmt.num(row.available_units)} vCPU`),
                ),
                h('td.num', fmt.pct(row.discount, 0)),
                h('td.num',
                  h('div', { style: { 'font-weight': '500' } }, fmt.money(row.price_per_hour)),
                  h('div', { style: { 'font-size': '11px', color: 'var(--text-muted)', 'text-decoration': 'line-through' } },
                    fmt.money(row.list_price_per_hour)),
                ),
                h('td.num', row.interruption_rate_per_lease_hour == null
                  ? h('span', { style: { color: 'var(--text-muted)' }, title: 'Not enough history yet to publish a rate.' }, 'no data')
                  : h('div',
                      h('div', `${(row.interruption_rate_per_lease_hour * 100).toFixed(2)}%`),
                      h('div', { style: { 'font-size': '11px', color: 'var(--text-muted)' } },
                        `per lease-hour · ${fmt.duration((row.interruption_sample_hours || 0) * 3600)} sample`),
                    )),
                h('td', row.degraded && h('span.pill.warning', {
                  title: 'The forecast feed is stale or low-confidence, so the pool has been cut '
                       + 'to a conservative floor rather than extrapolated.',
                }, icons.warning({ size: 13 }), 'degraded')),
              ))),
            ),
          ),
    );
  }

  function summary() {
    const rows = entries();
    const loaded = inventory.data != null;
    const totalUnits = rows.reduce((sum, r) => sum + r.available_units, 0);
    const best = rows.reduce((top, r) => (!top || r.discount > top.discount ? r : top), null);
    const degraded = rows.filter((r) => r.degraded).length;

    return h('.grid.grid-4',
      statTile({
        label: 'Available now',
        // A zero before the first response is a lie about the pool, and this
        // tile is the first thing anyone reads.
        value: loaded ? fmt.compact(totalUnits) : '—',
        unit: loaded ? 'vCPU' : null,
        sub: loaded
          ? `${rows.length} flavour/zone combinations advertised`
          : 'asking the pool…',
      }),
      statTile({
        label: 'Best discount',
        value: best ? fmt.pct(best.discount, 0) : '—',
        sub: best ? `${best.flavour} in ${best.az}` : loaded ? 'nothing for sale' : ' ',
        meter: best ? meter(best.discount) : null,
      }),
      statTile({
        label: 'Grace on interruption',
        value: '120',
        unit: 'seconds',
        sub: 'notice first, on three independent channels',
      }),
      statTile({
        label: 'Degraded zones',
        value: loaded ? fmt.num(degraded) : '—',
        sub: !loaded ? ' ' : degraded ? 'pool cut to a conservative floor' : 'all feeds confident',
        subKind: !loaded ? null : degraded ? 'bad' : 'good',
      }),
    );
  }

  return {
    dispose: () => inventory.stop(),
    render: () => h(`.page${inventory.stale ? '.stale' : ''}`,
      h('.page-head',
        h('div',
          h('h1', 'Spot market'),
          h('p.lede',
            'Spare capacity at a discount, taken back when the guaranteed classes need it. '
            + 'You get a notice before that happens, and the length of that notice is the product.'),
        ),
        h('.page-head-actions',
          h('button.btn.small', { onClick: () => inventory.refresh() },
            icons.refresh({ size: 14 }), 'Refresh'),
        ),
      ),
      summary(),
      h('.grid.grid-2', inventoryTable(), launchCard()),
    ),
  };
}

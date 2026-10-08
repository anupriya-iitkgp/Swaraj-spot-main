/**
 * The reclaim console — the most dangerous page in the product.
 *
 * Firing an order here ends customer workloads. The controls reflect that: the
 * order id is explicit (a replayed order must not double-preempt, so idempotency
 * is per order id), the victim preview is shown before the button is armed, and
 * the confirmation states what will actually happen rather than asking "are you
 * sure?".
 *
 * Two paths are offered, because they are genuinely different:
 *
 *   - **Headroom drop** is the intended trigger. The forecast says less spot is
 *     sellable; the pool shrinks on the next cycle; the shortfall becomes an
 *     order. The grace window is spent *ahead* of the guaranteed-class demand.
 *   - **Direct order** is the reactive path, used when capacity is already
 *     needed. It is the same handler, so it is not a shortcut — only a
 *     different reason recorded against it.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import {
  icons, banner, statePill, graceCountdown, field, select, statTile, keyValues,
} from '../../lib/ui.js';
import { state, resource, notify, toast, toastError } from '../../lib/store.js';

const BEHAVIOURS = [
  { value: 'cooperative', label: 'Honours the notice (exits cleanly)' },
  { value: 'ignores_notice', label: 'Ignores the notice (force-stopped at the deadline)' },
  { value: 'host_unreachable', label: 'Host agent unreachable (escalates to destroy)' },
];

export default function createOpsReclaim() {
  const overview = resource(() => api.console_.overview(24), { interval: 2000 }).start();
  const orders = resource(() => api.console_.reclaimOrders({ limit: 30 }), { interval: 3000 }).start();

  let selectedOrder = state.route.params.order || null;
  let detail = null;
  let detailFor = null;

  const form = {
    az: '',
    units: 8,
    hostGroup: '',
    reason: 'forecast_headroom',
    deadline: 120,
    orderId: '',
    submitting: false,
  };

  const headroom = { az: '', units: '', submitting: false };

  async function loadDetail(orderId) {
    if (detailFor === orderId) return;
    detailFor = orderId;
    try {
      detail = await api.console_.reclaimOrder(orderId);
    } catch (error) {
      detail = null;
      toastError(error, 'Could not load the order');
    }
    notify();
  }

  async function fire(event) {
    event.preventDefault();
    const orderId = form.orderId.trim() || `op-${Date.now().toString(36)}`;
    const message =
      `Reclaim ${form.units} vCPU from ${form.az}${form.hostGroup ? ` on ${form.hostGroup}` : ''}.\n\n`
      + 'Every selected lease gets a termination notice immediately and is stopped within '
      + `${form.deadline}s. This is not reversible.\n\nOrder id: ${orderId}`;
    if (!confirm(message)) return;

    form.submitting = true;
    notify();
    try {
      const result = await api.console_.reclaim({
        order_id: orderId,
        az: form.az,
        units: Number(form.units),
        host_group: form.hostGroup || null,
        deadline_seconds: Number(form.deadline),
        reason: form.reason,
      });
      form.orderId = orderId;
      selectedOrder = result.order_id;
      detailFor = null;
      toast(
        result.partial ? 'info' : 'ok',
        result.replayed ? 'Order replayed' : result.partial ? 'Partially satisfied' : 'Reclaim accepted',
        result.replayed
          ? 'Same order id — no second set of instances was preempted.'
          : `${result.units_selected} of ${result.units_requested} vCPU selected across `
            + `${result.leases_noticed.length} lease(s). ${result.detail}`,
      );
      overview.refresh();
      orders.refresh();
    } catch (error) {
      toastError(error, 'Reclaim refused');
    } finally {
      form.submitting = false;
      notify();
    }
  }

  async function dropHeadroom(event) {
    event.preventDefault();
    headroom.submitting = true;
    notify();
    try {
      const result = await api.console_.headroom({
        az: headroom.az,
        units: headroom.units === '' ? null : Number(headroom.units),
      });
      toast(
        result.shortfall_units > 0 ? 'info' : 'ok',
        'Headroom set',
        result.shortfall_units > 0
          ? `${result.az} is now short ${result.shortfall_units} vCPU — more is held by leases `
            + 'than the forecast says is sellable. That shortfall is what a reclaim order is for.'
          : `${result.az} now advertises ${result.sellable_units} vCPU sellable.`,
      );
      if (result.shortfall_units > 0) {
        form.az = result.az;
        form.units = result.shortfall_units;
        form.reason = 'forecast_headroom';
      }
      overview.refresh();
    } catch (error) {
      toastError(error, 'Headroom override failed');
    } finally {
      headroom.submitting = false;
      notify();
    }
  }

  async function pinBehaviour(leaseId, behaviour) {
    try {
      await api.console_.guestBehaviour(leaseId, behaviour);
      toast('ok', 'Guest behaviour pinned', `${fmt.shortId(leaseId, 14)} will ${behaviour.replace(/_/g, ' ')}.`);
    } catch (error) {
      toastError(error, 'Could not pin the behaviour');
    }
  }

  async function reportCleanExit(leaseId) {
    try {
      const result = await api.console_.cleanExit(leaseId);
      toast(result.accepted ? 'ok' : 'info',
        result.accepted ? 'Clean exit recorded' : 'Too late',
        result.detail);
      overview.refresh();
    } catch (error) {
      toastError(error, 'Clean exit failed');
    }
  }

  function orderForm(data) {
    const zones = data.policy.availability_zones;
    if (!form.az) form.az = zones[0] || '';
    if (!headroom.az) headroom.az = zones[0] || '';

    const pool = data.pools.find((p) => p.az === form.az);
    const liveHere = data.in_grace.filter((l) => l.az === form.az);

    return h('.card',
      h('.card-head', h('h2', 'Issue a reclaim order')),
      h('.card-note',
        'The advertised pool is shrunk before any victim is selected, so no launch can be '
        + 'admitted against capacity that is already spoken for. Then victims are chosen: '
        + 'contiguity picks the host set, fairness only orders victims within it, and the '
        + `blast-radius cap stops one wave taking more than ${fmt.pct(data.policy.blast_radius, 0)} `
        + 'of any single tenant\'s fleet.'),

      h('form', { onSubmit: fire },
        h('.form-row',
          field('Zone', select({
            value: form.az,
            onChange: (event) => { form.az = event.target.value; notify(); },
          }, zones)),
          field('vCPU to reclaim', h('input.input', {
            type: 'number', min: '1', value: form.units,
            onInput: (event) => { form.units = event.target.value; notify(); },
          }), pool ? `${pool.reserved_units} vCPU currently held in ${form.az}` : null),
          field('Host group', h('input.input', {
            value: form.hostGroup,
            placeholder: 'any',
            onInput: (event) => { form.hostGroup = event.target.value; },
          }), 'Narrows selection to one host. An unplaced lease can never satisfy a host-scoped order.'),
        ),
        h('.form-row', { style: { 'margin-top': '12px' } },
          field('Deadline', h('input.input', {
            type: 'number', min: '1', value: form.deadline,
            onInput: (event) => { form.deadline = event.target.value; },
          }), 'seconds'),
          field('Reason', select({
            value: form.reason,
            onChange: (event) => { form.reason = event.target.value; },
          }, ['forecast_headroom', 'guaranteed_class_demand', 'host_maintenance', 'operator_manual'])),
          field('Order id', h('input.input', {
            value: form.orderId,
            placeholder: 'generated if blank',
            onInput: (event) => { form.orderId = event.target.value; },
          }), 'Reissue the same id to prove a replay does not double-preempt.'),
        ),

        liveHere.length > 0 && h('div', { style: { 'margin-top': '12px' } },
          banner('warning', `${liveHere.length} lease(s) in ${form.az} are already inside a grace `
            + 'window. Preempting a lease twice is a no-op — the state guard settles it — but the '
            + 'order will find fewer units than you expect.')),

        h('button.btn.danger', {
          type: 'submit',
          disabled: form.submitting || !form.az,
          style: { 'margin-top': '14px' },
        }, icons.bolt({ size: 15 }), form.submitting ? 'Issuing…' : 'Issue reclaim order'),
      ),
    );
  }

  function headroomCard(data) {
    if (!data.service.sim_enabled) {
      return h('.card',
        h('.card-head', h('h2', 'Forecast headroom')),
        h('.empty', 'Simulation endpoints are disabled on this deployment, so the forecast '
          + 'feed cannot be overridden here. In production the number arrives from the '
          + 'capacity side.'));
    }
    return h('.card',
      h('.card-head', h('h2', 'Drop forecast headroom')),
      h('.card-note',
        'The proactive path. Lower the sellable number and the pool shrinks on the next control '
        + 'cycle; whatever leases already hold beyond it is the shortfall an order has to cover. '
        + 'This is how a reclaim is supposed to start — before a guaranteed-class customer is '
        + 'waiting on the capacity.'),
      h('form', { onSubmit: dropHeadroom },
        h('.form-row',
          field('Zone', select({
            value: headroom.az,
            onChange: (event) => { headroom.az = event.target.value; notify(); },
          }, data.policy.availability_zones)),
          field('Sellable vCPU', h('input.input', {
            type: 'number', min: '0', value: headroom.units,
            placeholder: 'blank clears the override',
            onInput: (event) => { headroom.units = event.target.value; },
          })),
        ),
        h('button.btn', { type: 'submit', disabled: headroom.submitting, style: { 'margin-top': '12px' } },
          headroom.submitting ? 'Applying…' : 'Set headroom'),
      ),
    );
  }

  function inFlight(data) {
    if (!data.in_grace.length) return null;
    return h('.card', { style: { 'border-color': 'var(--serious)' } },
      h('.card-head',
        h('h2', 'Draining now'),
        h('span.pill.serious', `${data.in_grace.length}`),
      ),
      h('.card-note',
        'The guest decides how this goes, up to the deadline. Below, you can play the guest: '
        + 'report a clean exit, or pin how a simulated one will answer its next notice.'),
      h('.table-wrap',
        h('table.data',
          h('thead', h('tr',
            h('th', 'Lease'), h('th', 'Tenant'), h('th', 'State'), h('th.num', 'vCPU'),
            h('th', 'Force stop in'), h('th', 'Notice'), h('th', ''),
          )),
          h('tbody', data.in_grace.map((lease) => h('tr', { key: lease.lease_id },
            h('td.mono', fmt.shortId(lease.lease_id, 12)),
            h('td', lease.tenant_id),
            h('td', statePill(lease.state)),
            h('td.num', lease.units),
            h('td', graceCountdown(lease.force_stop_deadline, lease.grace_seconds)),
            h('td', lease.notice_channels_delivered.length
              ? h('span.pill.good', `${lease.notice_channels_delivered.length}/3`)
              : h('span.pill.critical', 'none')),
            h('td', h('button.btn.small.ghost', {
              onClick: () => reportCleanExit(lease.lease_id),
              title: 'Edge 13 — the host agent reporting that the guest honoured the notice.',
            }, 'Report clean exit')),
          ))),
        ),
      ),
    );
  }

  function behaviourCard(data) {
    if (!data.service.sim_enabled) return null;
    const running = (data.in_grace.length ? [] : null);
    return h('.card',
      h('.card-head', h('h2', 'Pin a guest\'s response')),
      h('.card-note',
        'Before issuing an order, decide how the guest will behave. The three options are the '
        + 'three rows of the grace budget: exit cleanly inside the window, ignore the notice and '
        + 'be force-stopped, or take the host agent down and be escalated to a hypervisor destroy '
        + 'with the host quarantined out of the pool.'),
      h('.form-row',
        field('Lease id', h('input.input', {
          id: 'behaviour-lease',
          placeholder: 'lease-…',
        })),
        field('Behaviour', select({ id: 'behaviour-kind' }, BEHAVIOURS)),
        h('button.btn', {
          onClick: () => {
            const leaseId = document.getElementById('behaviour-lease')?.value.trim();
            const behaviour = document.getElementById('behaviour-kind')?.value;
            if (!leaseId) { toast('error', 'No lease id', 'Paste a lease id first.'); return; }
            pinBehaviour(leaseId, behaviour);
          },
        }, 'Pin'),
      ),
    );
  }

  function orderList() {
    const list = orders.data?.orders ?? [];
    if (!list.length) {
      return h('.card',
        h('.card-head', h('h2', 'Orders')),
        h('.empty', 'No reclaim orders yet.'));
    }
    return h('.card',
      h('.card-head', h('h2', 'Orders')),
      h('.table-wrap',
        h('table.data',
          h('thead', h('tr',
            h('th', 'Order'), h('th', 'Zone'), h('th', 'Host group'),
            h('th.num', 'Asked'), h('th.num', 'Found'), h('th', 'State'),
            h('th', 'Reason'), h('th', 'By'), h('th', 'Received'),
          )),
          h('tbody', list.map((order) => h('tr.clickable', {
            key: order.order_id,
            onClick: () => { selectedOrder = order.order_id; loadDetail(order.order_id); },
          },
            h('td.mono', fmt.shortId(order.order_id, 16)),
            h('td', order.az),
            h('td', order.host_group || 'any'),
            h('td.num', order.units),
            h('td.num', order.units_selected),
            h('td', order.partial
              ? h('span.pill.warning', icons.warning({ size: 12 }), 'partial')
              : h('span.pill', order.state.toLowerCase())),
            h('td', order.reason),
            h('td', order.requested_by),
            h('td', fmt.ago(order.received_at)),
          ))),
        ),
      ),
    );
  }

  function orderDetail() {
    if (!selectedOrder) return null;
    if (!detail || detail.order_id !== selectedOrder) {
      loadDetail(selectedOrder);
      return h('.card', h('.empty', 'Loading order…'));
    }

    return h('.card',
      h('.card-head',
        h('h2', 'Order ', h('span.mono', detail.order_id)),
        h('button.btn.small.ghost', {
          onClick: () => { selectedOrder = null; detail = null; detailFor = null; notify(); },
        }, 'Close'),
      ),
      detail.partial && banner('warning',
        h('strong', 'Partially satisfied. '),
        'The blast-radius cap left units unfound rather than take one tenant\'s whole fleet. '
        + 'That is deliberate: a partial order is visible and alertable; silently destroying '
        + 'a fleet is not.'),

      h('div', { style: { 'margin-top': '14px' } },
        keyValues([
          ['Zone', detail.az],
          ['Host group', detail.host_group || 'any'],
          ['Requested', `${detail.units} vCPU`],
          ['Selected', `${detail.units_selected} vCPU across ${detail.victims.length} lease(s)`],
          ['Reason', detail.reason],
          ['Requested by', detail.requested_by],
          ['Received', fmt.dateTime(detail.received_at)],
          detail.completed_at && ['Completed', fmt.dateTime(detail.completed_at)],
          detail.detail && ['Detail', detail.detail],
        ])),

      detail.victims.length > 0 && h('div', { style: { 'margin-top': '18px' } },
        h('h3', { style: { 'margin-bottom': '8px' } }, 'Victims'),
        h('.table-wrap',
          h('table.data',
            h('thead', h('tr',
              h('th', 'Lease'), h('th', 'Tenant'), h('th', 'State'), h('th.num', 'vCPU'),
              h('th', 'Host group'), h('th', 'Forced'), h('th.num', 'Notice → closed'),
              h('th.num', 'Billed'), h('th.num', 'Credit'),
            )),
            h('tbody', detail.victims.map((victim) => h('tr', { key: victim.lease_id },
              h('td.mono', fmt.shortId(victim.lease_id, 12)),
              h('td', victim.tenant_id),
              h('td', statePill(victim.state)),
              h('td.num', victim.units),
              h('td', victim.host_group || '—'),
              h('td', victim.forced_stop
                ? h('span.pill.warning', 'forced')
                : h('span.pill.good', 'clean')),
              h('td.num', victim.grace_window_seconds != null
                ? `${fmt.num(victim.grace_window_seconds, 1)}s`
                : '—'),
              h('td.num', fmt.money(victim.billed_amount)),
              h('td.num', victim.credit_raised > 0
                ? h('span', { style: { color: 'var(--good-text)' } }, fmt.money(victim.credit_raised))
                : '—'),
            ))),
          ),
        )),

      detail.audit.length > 0 && h('div', { style: { 'margin-top': '18px' } },
        h('h3', { style: { 'margin-bottom': '8px' } }, 'What happened'),
        h('.scroll-y', detail.audit.map((entry) => h('.event-row', { key: entry.id },
          h('.event-time', fmt.time(entry.at)),
          h('div',
            h('.event-topic', fmt.eventLabel(entry.event)),
            h('.event-payload',
              entry.lease_id && h('span.tag', fmt.shortId(entry.lease_id, 12)),
              entry.detail && h('span', { style: { 'margin-left': '6px' } },
                Object.entries(entry.detail).slice(0, 4)
                  .map(([key, value]) => `${key}=${value}`).join(' · ')),
            ),
          ),
        )))),
    );
  }

  return {
    dispose: () => { overview.stop(); orders.stop(); },
    render: () => {
      const data = overview.data;
      if (!data) return h('.page', h('.card', h('.empty', 'Loading…')));

      return h('.page',
        h('.page-head',
          h('div',
            h('h1', 'Reclaim'),
            h('p.lede',
              'This subsystem does not decide how much capacity exists, nor when it must be '
              + 'taken back. Both arrive from outside as inputs — what it owns is faithful '
              + 'execution of the order.'),
          ),
        ),

        h('.grid.grid-3',
          statTile({
            label: 'Grace window',
            value: fmt.num(data.policy.grace_seconds),
            unit: 'seconds',
            sub: `force stop at ${data.policy.force_stop_at}s, teardown budget ${data.policy.teardown_budget}s`,
          }),
          statTile({
            label: 'Blast radius',
            value: fmt.pct(data.policy.blast_radius, 0),
            sub: 'the most of one tenant\'s fleet a single wave may take',
          }),
          statTile({
            label: 'Cooldown',
            value: fmt.num(data.policy.cooldown),
            unit: 'seconds',
            sub: 'reclaimed units are parked before they are sellable again',
          }),
        ),

        inFlight(data),
        h('.grid.grid-2', orderForm(data), headroomCard(data)),
        behaviourCard(data),
        orderDetail(),
        orderList(),
      );
    },
  };
}

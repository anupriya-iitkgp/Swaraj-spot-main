/**
 * The tenant's fleet.
 *
 * The column that justifies the page is the countdown. A lease in
 * NOTICE_ISSUED is inside the grace window, and the number shown is computed
 * against `force_stop_deadline` — the timestamp the reaper itself acts on —
 * rather than a locally decremented timer. A tab that was throttled or asleep
 * therefore shows the true remaining time, not a drifted one, which is the
 * whole point of a countdown that a customer might be making a decision on.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import { statePill, graceCountdown, statTile, banner } from '../../lib/ui.js';
import { state, resource, toast, toastError, navigate, notify } from '../../lib/store.js';

const LIVE = new Set(['REQUESTED', 'ADMITTED', 'PROVISIONING', 'RUNNING', 'NOTICE_ISSUED', 'DRAINING', 'STOPPED']);

export default function createTenantLeases() {
  let filter = 'live';

  const leases = resource(async (signal) => {
    if (!state.tenantId) return [];
    return api.spot.leases(state.tenantId, { limit: 200 }, { signal });
  }, { interval: 3000 }).start();

  let lastTenant = state.tenantId;

  async function release(lease) {
    if (!confirm(
      `Release ${lease.lease_id}?\n\n`
      + 'A voluntary release is not a preemption: no notice is issued, and the '
      + 'capacity goes back without the anti-thrash cooldown.',
    )) return;
    try {
      const result = await api.spot.release(state.tenantId, lease.lease_id);
      toast('ok', 'Released', `${lease.lease_id} is now ${result.state}.`);
      leases.refresh();
    } catch (error) {
      toastError(error, 'Release failed');
    }
  }

  function rows() {
    const all = leases.data ?? [];
    if (filter === 'live') return all.filter((l) => LIVE.has(l.state));
    if (filter === 'grace') return all.filter((l) => ['NOTICE_ISSUED', 'DRAINING'].includes(l.state));
    if (filter === 'closed') return all.filter((l) => ['CLOSED', 'REJECTED'].includes(l.state));
    return all;
  }

  function summary() {
    const all = leases.data ?? [];
    const live = all.filter((l) => LIVE.has(l.state));
    const inGrace = all.filter((l) => ['NOTICE_ISSUED', 'DRAINING'].includes(l.state));
    const spend = all.reduce((sum, l) => sum + (l.billed_amount || 0), 0);
    const credits = all.reduce((sum, l) => sum + (l.credit_raised || 0), 0);

    return h('.grid.grid-4',
      statTile({
        label: 'Live leases',
        value: fmt.num(live.length),
        sub: `${fmt.num(live.reduce((s, l) => s + l.units, 0))} vCPU held`,
      }),
      statTile({
        label: 'Being reclaimed',
        value: fmt.num(inGrace.length),
        sub: inGrace.length ? 'inside the grace window' : 'nothing interrupted',
        subKind: inGrace.length ? 'bad' : 'good',
      }),
      statTile({
        label: 'Billed',
        value: fmt.money(spend, { digits: 4 }),
        sub: 'grace seconds excluded',
      }),
      statTile({
        label: 'Credits raised',
        value: fmt.money(credits, { digits: 4 }),
        sub: credits > 0 ? 'notice was not delivered on any channel' : 'every notice reached a channel',
        subKind: credits > 0 ? 'bad' : 'good',
      }),
    );
  }

  function table() {
    const list = rows();
    if (!state.tenantId) {
      return h('.card', banner('info',
        h('strong', 'No tenant selected. '),
        'Set a tenant id in the header — leases are per tenant, and the API refuses anonymous calls.'));
    }
    if (!list.length) {
      return h('.card', h('.empty',
        leases.error
          ? `Could not load leases: ${leases.error.message}`
          : leases.data == null
            ? 'Loading leases…'
            : 'No leases in this view.'));
    }

    return h('.card',
      h('.table-wrap',
        h('table.data',
          h('thead', h('tr',
            h('th', 'Lease'),
            h('th', 'State'),
            h('th', 'Flavour'),
            h('th.num', 'vCPU'),
            h('th', 'Zone'),
            h('th', 'Host group'),
            h('th.num', 'Discount'),
            h('th.num', 'Billed'),
            h('th', 'Grace left'),
            h('th', ''),
          )),
          h('tbody', list.map((lease) => h('tr.clickable', {
            key: lease.lease_id,
            onClick: () => navigate(`/leases/${lease.lease_id}`),
          },
            h('td.mono', { title: lease.lease_id }, fmt.shortId(lease.lease_id, 14)),
            h('td', statePill(lease.state)),
            h('td', lease.flavour, lease.count > 1 && h('span.tag', { style: { 'margin-left': '6px' } }, `×${lease.count}`)),
            h('td.num', lease.units),
            h('td', lease.az),
            h('td', lease.host_group || h('span', { style: { color: 'var(--text-muted)' } }, 'unplaced')),
            h('td.num', fmt.pct(lease.discount, 0)),
            h('td.num',
              fmt.money(lease.billed_amount),
              lease.credit_raised > 0 && h('div', { style: { 'font-size': '11px', color: 'var(--good-text)' } },
                `−${fmt.money(lease.credit_raised)} credit`),
            ),
            h('td', lease.notice_at && !lease.stopped_at
              ? graceCountdown(lease.force_stop_deadline, lease.grace_seconds)
              : h('span', { style: { color: 'var(--text-muted)' } }, '—')),
            h('td', { onClick: (event) => event.stopPropagation() },
              ['CLOSED', 'REJECTED', 'STOPPED'].includes(lease.state)
                ? null
                : h('button.btn.small.ghost', { onClick: () => release(lease) }, 'Release')),
          ))),
        ),
      ),
    );
  }

  return {
    dispose: () => leases.stop(),
    render: () => {
      // The tenant is typed in the header, so the fleet has to follow it.
      if (state.tenantId !== lastTenant) {
        lastTenant = state.tenantId;
        leases.refresh();
      }

      return h(`.page${leases.stale ? '.stale' : ''}`,
        h('.page-head',
          h('div',
            h('h1', 'Leases'),
            h('p.lede',
              'The lease, not the instance, is the unit of billing and of preemption. '
              + 'Every timestamp on it is evidence: notice to stopped proves the grace '
              + 'window, stopped to closed proves the teardown budget.'),
          ),
          h('.page-head-actions',
            h('.mode-switch',
              ['live', 'grace', 'closed', 'all'].map((key) => h('a', {
                key,
                href: '#',
                'aria-current': String(filter === key),
                onClick: (event) => { event.preventDefault(); filter = key; notify(); },
              }, key === 'grace' ? 'in grace' : key)),
            ),
          ),
        ),
        summary(),
        table(),
      );
    },
  };
}

/**
 * One lease, in full.
 *
 * The page exists to answer two questions a customer actually asks after a
 * preemption — "was I warned?" and "why is this the bill?" — from stored
 * evidence rather than from a support ticket. The timeline is the first
 * answer, the charge breakdown is the second, and both are reconstructed from
 * fields the public API already returns.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import {
  icons, statePill, graceCountdown, leaseTimeline, banner, keyValues, statTile,
} from '../../lib/ui.js';
import { meter } from '../../lib/charts.js';
import { state, resource, toast, toastError, link } from '../../lib/store.js';

const CHANNELS = [
  ['metadata', 'Instance metadata', 'read from inside the guest at /metadata/spot/termination-time'],
  ['webhook', 'Tenant webhook', 'posted to the endpoint on the account'],
  ['event_stream', 'Event stream', 'published on spot.preempt.notice'],
];

export default function createLeaseDetail({ args }) {
  const leaseId = decodeURIComponent(args[0]);

  const lease = resource(
    () => api.spot.lease(state.tenantId, leaseId),
    { interval: 2000 },
  ).start();

  async function release() {
    try {
      await api.spot.release(state.tenantId, leaseId);
      toast('ok', 'Released', 'The lease is stopping; capacity returns once teardown is confirmed.');
      lease.refresh();
    } catch (error) {
      toastError(error, 'Release failed');
    }
  }

  function noticePanel(data) {
    if (!data.notice_at) {
      return h('.card',
        h('.card-head', h('h2', 'Termination notice')),
        h('.card-note',
          'No notice has been issued. If this lease is ever reclaimed, a notice goes out '
          + 'first on three independent channels, and the grace window starts from that moment.'),
      );
    }

    const delivered = new Set(data.notice_channels_delivered);
    const none = delivered.size === 0;

    return h('.card',
      h('.card-head',
        h('h2', 'Termination notice'),
        none
          ? h('span.pill.critical', icons.warning({ size: 13 }), 'no channel delivered')
          : h('span.pill.good', icons.check({ size: 13 }), `${delivered.size} of 3 delivered`),
      ),
      h('.card-note',
        none
          ? 'Every channel failed. The lease was terminated anyway — the timer is authoritative — '
            + 'and a credit was raised automatically, because losing an instance without warning is '
            + 'the one failure this product cannot ask a customer to absorb.'
          : 'The channels are attempted independently, so one failing cannot block the others. '
            + 'What succeeded is recorded as proof, and used when a charge is disputed.'),

      h('.grid', { style: { gap: '8px' } },
        CHANNELS.map(([key, name, description]) => h('div', {
          key,
          style: { display: 'flex', gap: '10px', 'align-items': 'flex-start' },
        },
          delivered.has(key)
            ? h('span', { style: { color: 'var(--good-text)' } }, icons.check({ size: 15 }))
            : h('span', { style: { color: 'var(--critical)' } }, icons.x({ size: 15 })),
          h('div',
            h('div', { style: { 'font-weight': '500' } }, name,
              h('span', { style: { color: 'var(--text-muted)', 'font-weight': '400', 'margin-left': '8px', 'font-size': '12px' } },
                delivered.has(key) ? 'delivered' : 'failed')),
            h('div', { style: { 'font-size': '12px', color: 'var(--text-muted)' } }, description),
          ),
        )),
      ),
    );
  }

  function gracePanel(data) {
    if (!data.notice_at || data.stopped_at) return null;
    const left = fmt.secondsUntil(data.force_stop_deadline);
    const fraction = data.grace_seconds ? Math.max(0, left) / data.grace_seconds : 0;

    return h('.card', { style: { 'border-color': 'var(--serious)' } },
      h('.card-head',
        h('h2', 'This lease is being reclaimed'),
        graceCountdown(data.force_stop_deadline, data.grace_seconds),
      ),
      h('.card-note',
        'The timer, not the guest, decides when the instance dies. Checkpoint now and exit '
        + 'cleanly; if nothing exits by the deadline the instance is force-stopped. '
        + 'Grace seconds are not billed.'),
      meter(1 - fraction, { severity: left <= 0 ? 'critical' : left < data.grace_seconds * 0.3 ? 'warning' : null }),
      h('div', { style: { display: 'flex', 'justify-content': 'space-between', 'font-size': '12px', color: 'var(--text-muted)', 'margin-top': '6px' } },
        h('span', `notice at ${fmt.time(data.notice_at)}`),
        h('span', `force stop at ${fmt.time(data.force_stop_deadline)}`),
      ),
    );
  }

  function chargePanel(data) {
    const gross = data.running_at
      ? ((data.stopped_at ? new Date(data.stopped_at) : new Date()) - new Date(data.running_at)) / 1000
      : 0;

    return h('.card',
      h('.card-head', h('h2', 'Charge')),
      h('.card-note',
        'The meter stops when the notice is issued, so the shutdown window is free. '
        + 'The rate was snapshotted at admission and is never recomputed — a later change '
        + 'to the published discount does not re-rate this lease.'),

      keyValues([
        ['Rate', `${fmt.money(data.rate_per_sec, { digits: 8 })} / second`],
        ['Discount snapshot', fmt.pct(data.discount, 1)],
        ['Price per hour', fmt.money(data.price_per_hour)],
        ['Ran for', data.running_at ? fmt.duration(gross) : 'never ran'],
        ['Billable seconds', fmt.num(data.billed_seconds, 1)],
        data.grace_seconds_excluded > 0 && ['Grace excluded', `${fmt.num(data.grace_seconds_excluded, 1)}s — not billed`],
        ['Amount', h('strong', fmt.money(data.billed_amount))],
        data.credit_raised > 0 && ['Credit', h('span', { style: { color: 'var(--good-text)' } },
          `−${fmt.money(data.credit_raised)}`)],
      ]),

      !data.running_at && data.closed_at && h('div', { style: { 'margin-top': '14px' } },
        banner('info', 'This lease never reached RUNNING, so it is billed zero. It still emits a '
          + 'usage record, which is what keeps the invoice complete and explainable.')),

      data.credit_raised > 0 && h('div', { style: { 'margin-top': '14px' } },
        banner('warning',
          h('strong', 'Automatically credited. '),
          'No notice channel delivered, so the customer lost the instance without warning.')),
    );
  }

  function facts(data) {
    return h('.grid.grid-4',
      statTile({ label: 'State', value: h('span', { style: { 'font-size': '20px' } }, statePill(data.state)),
        sub: data.forced_stop ? 'force-stopped at the deadline' : null, subKind: data.forced_stop ? 'bad' : null }),
      statTile({ label: 'Capacity', value: fmt.num(data.units), unit: 'vCPU',
        sub: `${data.flavour} × ${data.count} in ${data.az}` }),
      statTile({ label: 'Placement', value: data.host_group || 'unplaced',
        sub: data.instance_ids.length ? `${data.instance_ids.length} instance(s)` : 'no instances yet' }),
      statTile({ label: 'Purchase option', value: data.purchase_option,
        sub: `decided by ${data.purchase_option_source.replace(/_/g, ' ')}` }),
    );
  }

  return {
    dispose: () => lease.stop(),
    render: () => {
      const data = lease.data;

      if (lease.error) {
        return h('.page', h('.card',
          h('h1', 'Lease not available'),
          h('p.lede', lease.error.guidance || lease.error.message),
          h('p', { style: { 'margin-top': '12px' } },
            h('a', link('/leases'), 'Back to leases')),
        ));
      }
      if (!data) return h('.page', h('.card', h('.empty', 'Loading lease…')));

      return h(`.page${lease.stale ? '.stale' : ''}`,
        h('.page-head',
          h('div',
            h('h1', { class: 'mono' }, data.lease_id),
            h('p.lede', `${data.flavour} × ${data.count} · ${data.units} vCPU · ${data.az}`
              + (data.host_group ? ` · ${data.host_group}` : '')),
          ),
          h('.page-head-actions',
            h('a.btn.small', link('/leases'), 'All leases'),
            !['CLOSED', 'REJECTED', 'STOPPED'].includes(data.state)
              && h('button.btn.small.danger', { onClick: release }, 'Release'),
          ),
        ),

        data.rejection_code && banner('critical',
          h('strong', `Rejected — ${data.rejection_code.replace(/_/g, ' ')}. `),
          data.rejection_detail || 'The reservation was released and nothing was billed.'),

        data.teardown_stalled && banner('warning',
          h('strong', 'Teardown stalled. '),
          'The instances have stopped but the capacity has not been proven returned, so the '
          + 'units are still accounted for. They are never reported free on an unconfirmed commit.'),

        facts(data),
        gracePanel(data),

        h('.grid.grid-2',
          h('.card',
            h('.card-head', h('h2', 'Evidence')),
            h('.card-note',
              'The timestamps are what make a preemption dispute or a billing credit provable '
              + 'after the fact.'),
            leaseTimeline(data),
            (data.grace_window_seconds != null || data.teardown_window_seconds != null)
              && h('div', { style: { 'margin-top': '8px', 'padding-top': '14px', 'border-top': '1px solid var(--grid)' } },
                keyValues([
                  data.grace_window_seconds != null
                    && ['Grace window honoured', `${fmt.num(data.grace_window_seconds, 2)}s of ${fmt.num(data.grace_seconds)}s`],
                  data.teardown_window_seconds != null
                    && ['Teardown took', `${fmt.num(data.teardown_window_seconds, 2)}s`],
                  data.reclaim_order_id && ['Reclaim order', h('span.mono', data.reclaim_order_id)],
                  data.preemption_reason && ['Reason', data.preemption_reason.replace(/_/g, ' ')],
                ])),
          ),
          noticePanel(data),
        ),

        chargePanel(data),
      );
    },
  };
}

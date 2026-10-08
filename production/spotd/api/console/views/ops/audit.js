/**
 * The audit trail, and the proof it has not been edited.
 *
 * The chain verification at the top is the reason this page is not just a log
 * viewer. Preemption disputes are settled from this log or not at all, so
 * "the log says X" is only worth anything if the log can be shown not to have
 * been rewritten since. Each entry hashes its predecessor; recomputing the
 * chain finds the first row that no longer matches.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import { icons, verdict, field, select } from '../../lib/ui.js';
import { bars } from '../../lib/charts.js';
import { resource, notify, navigate } from '../../lib/store.js';

const INTERESTING = [
  '', 'preemption.notice_issued', 'preemption.notice_all_channels_failed',
  'preemption.forced_stop', 'preemption.timer_expired', 'capacity.returned',
  'capacity.reclaim_order', 'billing.credit_raised', 'teardown.stalled',
  'host.quarantined',
];

export default function createOpsAudit() {
  let event = '';
  let expanded = null;

  const audit = resource(() => api.console_.audit({ event: event || undefined, limit: 300 }),
    { interval: 4000 }).start();
  const overview = resource(() => api.console_.overview(24), { interval: 10000 }).start();
  const events = resource(() => api.console_.events({ limit: 60 }), { interval: 3000 }).start();

  function chainCard() {
    const chain = audit.data?.chain;
    if (!chain) return null;
    return h('.card', chain.valid ? {} : { style: { 'border-color': 'var(--critical)' } },
      h('.card-head',
        h('h2', 'Chain integrity'),
        verdict(chain.valid, { yes: 'chain intact', no: 'chain broken' }),
      ),
      h('.card-note',
        'Every entry hashes its predecessor, so an edited or removed row breaks every '
        + 'hash after it. Recomputed here from the stored rows using the same expression '
        + 'the insert trigger used.'),
      h('.grid.grid-3',
        h('.stat', h('.stat-label', 'Entries'), h('.stat-value', fmt.num(chain.entries))),
        h('.stat', h('.stat-label', 'First break'),
          h('.stat-value', chain.broken_at_id ?? 'none')),
        h('.stat', h('.stat-label', 'Verdict'),
          h('.stat-value', { style: { 'font-size': '16px' } }, chain.detail || 'consistent')),
      ),
    );
  }

  function eventMixCard() {
    const counts = overview.data?.audit_event_counts ?? {};
    const rows = Object.entries(counts)
      .map(([key, value]) => ({ key, label: fmt.eventLabel(key), value }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 12);
    if (!rows.length) return null;

    const noticeFailures = counts['preemption.notice_all_channels_failed'] || 0;
    const forced = counts['preemption.forced_stop'] || 0;

    return h('.card',
      h('.card-head', h('h2', 'What has been happening')),
      h('.card-note', 'Audit events in the last 24 hours, by kind.'),
      bars(rows, {
        emphasise: noticeFailures > 0
          ? 'preemption.notice_all_channels_failed'
          : forced > 0 ? 'preemption.forced_stop' : null,
      }),
    );
  }

  function busCard() {
    const list = (events.data?.events ?? []).slice().reverse();
    return h('.card',
      h('.card-head',
        h('h2', 'Event bus'),
        h('span.pill', `${events.data?.buffered ?? 0} buffered`),
      ),
      h('.card-note',
        'Published from the outbox, never from the write path, so an event can only exist '
        + 'for a state change that actually committed. In-process and bounded here; Kafka, '
        + 'partitioned by lease id, in the target deployment.'),
      list.length === 0
        ? h('.empty', 'The buffer is empty. It starts empty on a fresh replica — the durable '
            + 'record is the audit log beside it.')
        : h('.scroll-y', list.map((item) => h('.event-row', { key: item.seq },
            h('.event-time', fmt.time(item.at)),
            h('div',
              h('.event-topic', item.topic),
              h('.event-payload',
                Object.entries(item.payload)
                  .filter(([key]) => !['tenant_id'].includes(key))
                  .slice(0, 5)
                  .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : value}`)
                  .join(' · ')),
            ),
          ))),
    );
  }

  function trailCard() {
    const entries = audit.data?.entries ?? [];
    return h('.card',
      h('.card-head',
        h('h2', 'Audit trail'),
        h('.page-head-actions',
          field('', select({
            value: event,
            onChange: (e) => { event = e.target.value; audit.refresh(); notify(); },
          }, INTERESTING.map((value) => ({
            value,
            label: value ? fmt.eventLabel(value) : 'every event',
          })))),
        ),
      ),
      entries.length === 0
        ? h('.empty', 'No entries match.')
        : h('.table-wrap',
            h('table.data',
              h('thead', h('tr',
                h('th.num', '#'), h('th', 'At'), h('th', 'Event'),
                h('th', 'Lease'), h('th', 'Tenant'), h('th', 'Order'),
                h('th', 'Actor'), h('th', 'Detail'),
              )),
              h('tbody', entries.map((entry) => h('tr.clickable', {
                key: entry.id,
                onClick: () => { expanded = expanded === entry.id ? null : entry.id; notify(); },
              },
                h('td.num', entry.id),
                h('td', fmt.dateTime(entry.at)),
                h('td', fmt.eventLabel(entry.event)),
                h('td.mono', entry.lease_id
                  ? h('a', {
                      href: `/leases/${entry.lease_id}`,
                      onClick: (clickEvent) => {
                        clickEvent.preventDefault();
                        clickEvent.stopPropagation();
                        navigate(`/leases/${entry.lease_id}`);
                      },
                    }, fmt.shortId(entry.lease_id, 12))
                  : '—'),
                h('td', entry.tenant_id || '—'),
                h('td.mono', entry.order_id ? fmt.shortId(entry.order_id, 10) : '—'),
                h('td', entry.actor || '—'),
                h('td', { style: { 'max-width': '340px' } },
                  expanded === entry.id
                    ? h('pre', { style: { margin: 0, 'white-space': 'pre-wrap', 'font-size': '11px' } },
                        JSON.stringify(entry.detail, null, 2))
                    : h('span', { style: { color: 'var(--text-secondary)', 'font-size': '12px' } },
                        Object.entries(entry.detail || {}).slice(0, 3)
                          .map(([key, value]) => `${key}=${value}`).join(' · ') || '—')),
              ))),
            ),
          ),
    );
  }

  return {
    dispose: () => { audit.stop(); overview.stop(); events.stop(); },
    render: () => h(`.page${audit.stale ? '.stale' : ''}`,
      h('.page-head',
        h('div',
          h('h1', 'Audit'),
          h('p.lede',
            'Append-only evidence. Entries are never mutated or deleted, and 100% of notices, '
            + 'expiries and credits are recorded — preemption disputes are settled from this '
            + 'log or not at all.'),
        ),
        h('.page-head-actions',
          h('button.btn.small', { onClick: () => audit.refresh() },
            icons.refresh({ size: 14 }), 'Refresh'),
        ),
      ),
      chainCard(),
      h('.grid.grid-2', eventMixCard(), busCard()),
      trailCard(),
    ),
  };
}

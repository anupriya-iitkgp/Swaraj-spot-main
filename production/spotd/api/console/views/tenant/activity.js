/**
 * The tenant's event stream and the published interruption rate.
 *
 * HLD §12 offers "a capacity-watch subscription so tenants wait on an event
 * instead of polling" as the answer to retry storms. This page is the read side
 * of that subscription, and the reason it is worth showing a customer: a client
 * that reacts to `spot.preempt.notice` gets its 120 seconds; a client that
 * polls `GET /spot/leases` in a loop is the retry storm.
 *
 * The interruption rate below it is the other half of buying spot honestly.
 * HLD §6 forbids hiding it, and §11 requires it refreshed at least hourly, per
 * flavour and AZ — so it is published here even when it is unflattering.
 */

import { h } from '../../lib/dom.js';
import * as api from '../../lib/api.js';
import * as fmt from '../../lib/fmt.js';
import { icons, banner, statTile } from '../../lib/ui.js';
import { bars } from '../../lib/charts.js';
import { state, resource } from '../../lib/store.js';

const TOPIC_NOTES = {
  'spot.preempt.notice': 'A lease of yours is being reclaimed. The grace window starts now.',
  'spot.lease.created': 'A launch was admitted and capacity reserved.',
  'spot.lease.transition': 'The lease moved to a new state.',
  'spot.lease.closed': 'Teardown was confirmed and the capacity went back.',
  'spot.credit.raised': 'A credit was raised against this lease.',
  'spot.usage.record': 'A rated usage record was emitted to billing.',
};

export default function createTenantActivity() {
  let cursor = 0;
  let seen = [];

  const events = resource(async () => {
    if (!state.tenantId) return { events: [], latest_seq: 0 };
    const page = await api.spot.events(state.tenantId, { since_seq: cursor, limit: 100 });
    if (page.events.length) {
      seen = [...page.events.reverse(), ...seen].slice(0, 200);
      cursor = page.latest_seq;
    }
    return page;
  }, { interval: 2000 }).start();

  const rates = resource(() => api.spot.interruptions(), { interval: 30000 }).start();

  let lastTenant = state.tenantId;

  function eventFeed() {
    if (!state.tenantId) {
      return h('.card', banner('info',
        h('strong', 'No tenant selected. '),
        'Set a tenant id in the header to subscribe to its events.'));
    }
    return h('.card',
      h('.card-head',
        h('h2', 'Event stream'),
        h('span.pill', `${seen.length} buffered`),
      ),
      h('.card-note',
        'Delivery is at-least-once and ordering is only guaranteed per lease, so a consumer '
        + 'must be idempotent. The natural key is the lease id plus the transition.'),
      seen.length === 0
        ? h('.empty', 'Nothing yet. Launch a lease, or wait for one to be reclaimed.')
        : h('.scroll-y', seen.map((event) => h('.event-row', { key: event.seq },
            h('.event-time', fmt.time(event.at)),
            h('div',
              h('.event-topic', event.topic),
              h('.event-payload',
                TOPIC_NOTES[event.topic] || '',
                event.payload.lease_id && h('span.tag', { style: { 'margin-left': '6px' } },
                  fmt.shortId(event.payload.lease_id, 14)),
                event.payload.terminate_after && h('span.pill.serious', { style: { 'margin-left': '6px' } },
                  `terminates ${fmt.time(event.payload.terminate_after)}`),
              ),
            ),
          ))),
    );
  }

  function interruptionPanel() {
    const data = rates.data;
    const rows = (data?.rates ?? []).map((entry) => ({
      key: `${entry.az}/${entry.flavour}`,
      label: `${entry.flavour} · ${entry.az}`,
      value: entry.interruption_rate_per_lease_hour,
      note: `${entry.preemptions} preemption(s) over ${fmt.num(entry.lease_hours_observed, 1)} lease-hours`,
    }));

    return h('.card',
      h('.card-head',
        h('h2', 'Published interruption rate'),
        // The staleness of the rate is part of the product: a rate computed six
        // hours ago is not the same claim as one computed ten minutes ago.
        data?.computed_seconds_ago != null
          ? h('span.pill', `computed ${fmt.duration(data.computed_seconds_ago)} ago`)
          : h('span.pill', 'not yet computed'),
      ),
      h('.card-note',
        'Preemptions per lease-hour, per flavour and zone, measured from the audit log. '
        + 'This is the number to size a workload against — if it is high for a flavour you '
        + 'depend on, spread across zones or take a smaller discount elsewhere.'),
      rows.length === 0
        ? h('.empty', 'Not enough preemption history yet to publish a rate. '
            + 'The analytics worker republishes it on its own interval.')
        : h('div',
            bars(rows.sort((a, b) => b.value - a.value), {
              format: (v) => `${(v * 100).toFixed(2)}%`,
              slot: 2,
            }),
            data?.definition && h('p', {
              style: { 'font-size': '11px', color: 'var(--text-muted)', 'margin-top': '10px' },
            }, data.definition),
          ),
    );
  }

  function summary() {
    const notices = seen.filter((e) => e.topic === 'spot.preempt.notice');
    const credits = seen.filter((e) => e.topic === 'spot.credit.raised');
    return h('.grid.grid-3',
      statTile({
        label: 'Notices received',
        value: fmt.num(notices.length),
        sub: notices.length ? `last ${fmt.ago(notices[0].at)}` : 'none in this session',
      }),
      statTile({
        label: 'Credits raised',
        value: fmt.num(credits.length),
        sub: credits.length ? 'a notice failed to reach you' : 'every notice was delivered',
        subKind: credits.length ? 'bad' : 'good',
      }),
      statTile({
        label: 'Events buffered',
        value: fmt.num(seen.length),
        sub: 'the ring buffer standing in for Kafka',
      }),
    );
  }

  return {
    dispose: () => { events.stop(); rates.stop(); },
    render: () => {
      if (state.tenantId !== lastTenant) {
        lastTenant = state.tenantId;
        cursor = 0;
        seen = [];
        events.refresh();
      }

      return h('.page',
        h('.page-head',
          h('div',
            h('h1', 'Activity'),
            h('p.lede',
              'Wait on an event rather than polling for one. A capacity shortage plus '
              + 'aggressive retries is a self-inflicted API flood; this is the subscription '
              + 'that avoids it.'),
          ),
          h('.page-head-actions',
            h('button.btn.small', { onClick: () => { events.refresh(); rates.refresh(); } },
              icons.refresh({ size: 14 }), 'Refresh'),
          ),
        ),
        summary(),
        h('.grid.grid-2', eventFeed(), interruptionPanel()),
      );
    },
  };
}

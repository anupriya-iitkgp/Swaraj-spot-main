/**
 * Shared components.
 *
 * The lease-state pill is the one worth reading closely. Every state gets a
 * colour *and* its name, never colour alone — six of the nine states are
 * transient and an operator scanning a fleet table has to be able to tell
 * NOTICE_ISSUED from DRAINING at a glance, which two similar ambers cannot do
 * on their own.
 */

import { h } from './dom.js';
import * as fmt from './fmt.js';

/* --------------------------------------------------------------------- */
/* icons — 16px, currentColor, no external sprite                         */
/* --------------------------------------------------------------------- */
const svg = (...paths) => (props = {}) =>
  h('svg', {
    width: props.size || 16, height: props.size || 16,
    viewBox: '0 0 16 16', fill: 'none',
    stroke: 'currentColor', 'stroke-width': 1.5,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    'aria-hidden': 'true', ...props,
  }, ...paths);

export const icons = {
  gauge: svg(h('path', { d: 'M2 12a6 6 0 1 1 12 0' }), h('path', { d: 'M8 12 11 7' })),
  layers: svg(h('path', { d: 'M8 2 2 5.5 8 9l6-3.5L8 2Z' }), h('path', { d: 'M2 10.5 8 14l6-3.5' })),
  server: svg(h('rect', { x: 2, y: 2.5, width: 12, height: 4.5, rx: 1 }), h('rect', { x: 2, y: 9, width: 12, height: 4.5, rx: 1 })),
  bolt: svg(h('path', { d: 'M9 1.5 3.5 9H8l-1 5.5L12.5 7H8l1-5.5Z' })),
  shield: svg(h('path', { d: 'M8 1.5 13.5 4v4c0 3.2-2.3 5.6-5.5 6.5C4.8 13.6 2.5 11.2 2.5 8V4L8 1.5Z' })),
  clipboard: svg(h('rect', { x: 3.5, y: 3, width: 9, height: 11, rx: 1.5 }), h('path', { d: 'M6 3V2h4v1' }), h('path', { d: 'M6 7h4M6 10h3' })),
  users: svg(h('circle', { cx: 6, cy: 6, r: 2.5 }), h('path', { d: 'M2 13.5a4 4 0 0 1 8 0' }), h('path', { d: 'M11 4.2a2.5 2.5 0 0 1 0 4.6M12 13.5a4 4 0 0 0-1.2-2.9' })),
  sliders: svg(h('path', { d: 'M2 5h7M11.5 5H14M2 11h3M7.5 11H14' }), h('circle', { cx: 10, cy: 5, r: 1.6 }), h('circle', { cx: 6, cy: 11, r: 1.6 })),
  cart: svg(h('path', { d: 'M1.5 2h1.7l1.6 7.5h7l1.7-5.5H4.5' }), h('circle', { cx: 6, cy: 13, r: 1.2 }), h('circle', { cx: 11.5, cy: 13, r: 1.2 })),
  list: svg(h('path', { d: 'M5.5 4h8M5.5 8h8M5.5 12h8M2.5 4h.01M2.5 8h.01M2.5 12h.01' })),
  activity: svg(h('path', { d: 'M1.5 8h3l2-5 3 10 2-5h3' })),
  warning: svg(h('path', { d: 'M8 2.2 14.5 13.5h-13L8 2.2Z' }), h('path', { d: 'M8 6.5v3M8 11.6h.01' })),
  check: svg(h('path', { d: 'm3 8.5 3.2 3.2L13 4.8' })),
  x: svg(h('path', { d: 'm4 4 8 8M12 4l-8 8' })),
  sun: svg(h('circle', { cx: 8, cy: 8, r: 3 }), h('path', { d: 'M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3.1 3.1l1.1 1.1M11.8 11.8l1.1 1.1M12.9 3.1l-1.1 1.1M4.2 11.8l-1.1 1.1' })),
  moon: svg(h('path', { d: 'M13 9.5A5.6 5.6 0 0 1 6.5 3a5.75 5.75 0 1 0 6.5 6.5Z' })),
  logout: svg(h('path', { d: 'M6 14H3.5a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1H6' }), h('path', { d: 'M10.5 11 13.5 8l-3-3M13.5 8H6' })),
  refresh: svg(h('path', { d: 'M13.5 8a5.5 5.5 0 1 1-1.7-4' }), h('path', { d: 'M13.8 1.5V4.5h-3' })),
  arrowRight: svg(h('path', { d: 'M3 8h10M9 4l4 4-4 4' })),
};

/* --------------------------------------------------------------------- */
/* pieces                                                                 */
/* --------------------------------------------------------------------- */

export function statTile({ label, value, unit, sub, subKind, meter: meterNode, hero = false, trend }) {
  return h('.card',
    h('.stat',
      h('.stat-label', label),
      h(`.stat-value${hero ? '.hero' : ''}`, value, unit && h('span.unit', unit)),
      meterNode,
      trend,
      sub && h(`.stat-sub${subKind ? `.${subKind}` : ''}`, sub),
    ),
  );
}

export function statePill(state) {
  return h('span.pill', { 'data-state': state },
    h('span.dot'),
    state.replace(/_/g, ' ').toLowerCase(),
  );
}

/** A pass/fail marker that never relies on colour alone. */
export function verdict(met, { yes = 'met', no = 'breached', unknown = 'no data' } = {}) {
  if (met == null) return h('span.pill', unknown);
  return met
    ? h('span.pill.good', icons.check({ size: 13 }), yes)
    : h('span.pill.critical', icons.warning({ size: 13 }), no);
}

export function banner(kind, ...children) {
  const icon = kind === 'critical' || kind === 'warning' ? icons.warning : icons.activity;
  return h(`.banner.${kind}`, icon({ size: 15 }), h('div', ...children));
}

export function card(title, note, ...children) {
  return h('.card',
    title && h('.card-head', h('h2', title)),
    note && h('.card-note', note),
    ...children,
  );
}

export function emptyState(message) {
  return h('.empty', message);
}

export function field(label, control, hint) {
  return h('.field',
    h('label', label),
    control,
    hint && h('.hint', hint),
  );
}

export function select(props, options) {
  return h('select.input', props,
    options.map((option) =>
      h('option', {
        key: option.value ?? option,
        value: option.value ?? option,
        selected: String(props.value ?? '') === String(option.value ?? option),
      }, option.label ?? option)),
  );
}

/** A grace countdown, computed against the deadline the reaper acts on. */
export function graceCountdown(deadlineIso, graceSeconds) {
  const left = fmt.secondsUntil(deadlineIso);
  if (left == null) return h('span', '—');
  const severity = fmt.graceSeverity(left, graceSeconds);
  const cls = severity === 'normal' ? '' : `.${severity}`;
  return h(`span.countdown${cls}`,
    left <= 0 ? 'force-stopping' : fmt.clock(left),
  );
}

export function keyValues(pairs) {
  return h('dl.kv', pairs.filter(Boolean).map(([key, value]) =>
    [h('dt', { key: `k${key}` }, key), h('dd', { key: `v${key}` }, value)]).flat());
}

/**
 * A lease's evidence trail as a timeline. HLD §10: "notice_at → stopped_at
 * proves the grace window; stopped_at → closed_at proves the teardown budget."
 * This is that sentence, rendered.
 */
export function leaseTimeline(lease) {
  const steps = [
    ['Requested', lease.created_at, null],
    ['Admitted', lease.admitted_at, 'capacity reserved; discount snapshotted'],
    ['Running', lease.running_at, lease.host_group && `placed on ${lease.host_group}`],
    ['Notice issued', lease.notice_at,
      lease.notice_at
        ? (lease.notice_channels_delivered.length
            ? `delivered on ${lease.notice_channels_delivered.join(', ')}`
            : 'no channel delivered — automatic credit')
        : null],
    ['Stopped', lease.stopped_at, lease.forced_stop ? 'forced at the grace deadline' : 'clean exit'],
    ['Closed', lease.closed_at, 'teardown confirmed; capacity returned'],
  ];

  return h('.timeline', steps.map(([title, at, note], index) => {
    const done = Boolean(at);
    const warn = title === 'Notice issued' && at && !lease.notice_channels_delivered.length;
    return h(`.timeline-step${done ? '.done' : ''}${warn ? '.warn' : ''}`, { key: title },
      h('.timeline-rail',
        h('.timeline-dot'),
        index < steps.length - 1 && h('.timeline-line'),
      ),
      h('.timeline-body',
        h('.timeline-title', title),
        h('.timeline-meta',
          done ? fmt.dateTime(at) : 'not reached',
          note && done ? ` · ${note}` : '',
        ),
      ),
    );
  }));
}

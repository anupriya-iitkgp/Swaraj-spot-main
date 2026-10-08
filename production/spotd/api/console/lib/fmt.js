/**
 * Formatting.
 *
 * The units are the design's units, not generic ones. LLD §1: "Capacity is
 * accounted in integer vCPU units everywhere... No component uses instance
 * counts as a capacity measure." So a number rendered as capacity is rendered
 * as vCPU, and nothing here quietly converts one into the other.
 */

export const nf = new Intl.NumberFormat(undefined);

export function num(value, digits = 0) {
  if (value == null || Number.isNaN(value)) return '—';
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function units(value) {
  return value == null ? '—' : `${num(value)} vCPU`;
}

export function compact(value) {
  if (value == null) return '—';
  if (Math.abs(value) < 1000) return num(value);
  return value.toLocaleString(undefined, { notation: 'compact', maximumFractionDigits: 1 });
}

export function pct(value, digits = 1) {
  if (value == null || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(digits)}%`;
}

/**
 * Money. Rates here are per-vCPU-second and genuinely small, so a currency
 * formatter rounding to two decimals would print every spot charge as 0.00 —
 * which is how a billing bug hides in plain sight on a dashboard.
 */
export function money(value, { digits = 4 } = {}) {
  if (value == null || Number.isNaN(value)) return '—';
  if (value === 0) return '0';
  if (Math.abs(value) < 0.0001) return value.toExponential(2);
  return value.toFixed(digits).replace(/0+$/, '').replace(/\.$/, '');
}

export function duration(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return '—';
  const s = Math.abs(seconds);
  if (s < 1) return `${(s * 1000).toFixed(0)}ms`;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.round((s % 86400) / 3600)}h`;
}

/** mm:ss — for a countdown that has to be read at a glance. */
export function clock(seconds) {
  if (seconds == null) return '—';
  const s = Math.max(0, Math.round(seconds));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

export function time(iso, { seconds = true } = {}) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    ...(seconds ? { second: '2-digit' } : {}),
    hour12: false,
  });
}

export function dateTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString(undefined, {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  });
}

export function ago(iso) {
  if (!iso) return '—';
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 0) return `in ${duration(-delta)}`;
  if (delta < 5) return 'just now';
  return `${duration(delta)} ago`;
}

export function secondsSince(iso) {
  if (!iso) return null;
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

export function secondsUntil(iso) {
  if (!iso) return null;
  return (new Date(iso).getTime() - Date.now()) / 1000;
}

/** Shorten an id for a table cell without making it unrecognisable. */
export function shortId(id, keep = 10) {
  if (!id) return '—';
  return id.length <= keep + 3 ? id : `${id.slice(0, keep)}…`;
}

/** Human-readable audit event names, from LLD §8's topic vocabulary. */
const EVENT_LABELS = {
  'lease.created': 'Lease created',
  'lease.transition': 'State change',
  'lease.admitted': 'Admitted',
  'lease.running': 'Running',
  'lease.rejected': 'Rejected',
  'lease.closed': 'Closed',
  'lease.force_stopped': 'Force-stopped',
  'preemption.notice_issued': 'Notice issued',
  'preemption.notice_delivered': 'Notice delivered',
  'preemption.notice_all_channels_failed': 'All notice channels failed',
  'preemption.timer_expired': 'Grace timer expired',
  'preemption.forced_stop': 'Forced stop',
  'preemption.victim_selected': 'Victim selected',
  'capacity.returned': 'Capacity returned',
  'capacity.reclaim_order': 'Reclaim order',
  'capacity.pool_shrunk': 'Pool shrunk',
  'billing.credit_raised': 'Credit raised',
  'teardown.confirmed': 'Teardown confirmed',
  'teardown.stalled': 'Teardown stalled',
  'host.quarantined': 'Host quarantined',
};

export function eventLabel(event) {
  return EVENT_LABELS[event] || event.replace(/[._]/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

/** Severity band for a grace countdown. Drives colour AND the label beside it. */
export function graceSeverity(secondsLeft, graceSeconds) {
  if (secondsLeft == null) return 'normal';
  if (secondsLeft <= 0) return 'critical';
  const fraction = graceSeconds ? secondsLeft / graceSeconds : 1;
  if (fraction <= 0.2) return 'critical';
  if (fraction <= 0.5) return 'warning';
  return 'normal';
}

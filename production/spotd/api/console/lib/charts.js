/**
 * Charts — inline SVG, no library.
 *
 * The rules these follow, and why each one is here rather than a preference:
 *
 *  - **One y-axis, always.** Never two scales on one plot; the alignment
 *    between them is arbitrary and invents a correlation that is not in the
 *    data. Where two measures matter, they get two charts.
 *  - **Colour follows the entity.** `reserved` is slot 1 wherever it appears,
 *    in every chart, whether or not it is the biggest segment. A filter that
 *    repaints the survivors teaches the operator the wrong thing.
 *  - **2px surface gaps, never borders**, between touching segments.
 *  - **Thin marks, hairline solid grid** — never dashed, which reads as
 *    "threshold" when it is only a gridline.
 *  - **A legend for two or more series, one colour and no legend for one.**
 *  - **A table view for every chart.** Three of the light-mode hues sit under
 *    3:1 on the light surface, so colour is never the only way to read a value.
 */

import { h } from './dom.js';
import { notify } from './store.js';
import * as fmt from './fmt.js';

/* --------------------------------------------------------------------- */
/* hover state, shared so a re-render does not drop the tooltip           */
/* --------------------------------------------------------------------- */
const hovered = new Map();

export function setHover(chartId, value) {
  if (hovered.get(chartId) === value) return;
  if (value == null) hovered.delete(chartId);
  else hovered.set(chartId, value);
  notify();
}

const getHover = (chartId) => hovered.get(chartId);

/* --------------------------------------------------------------------- */
/* helpers                                                                */
/* --------------------------------------------------------------------- */

/** Round an axis maximum up to something a person would have chosen. */
export function niceMax(value) {
  if (!value || value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalised = value / magnitude;
  const step = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10;
  return step * magnitude;
}

function ticks(max, count = 4) {
  const out = [];
  for (let i = 0; i <= count; i++) out.push((max / count) * i);
  return out;
}

const SERIES = (n) => `var(--series-${n})`;

/* ===================================================================== */
/* area / line over time                                                 */
/* ===================================================================== */

/**
 * A single measure over time. One series, so no legend box — the card title
 * already names what is plotted.
 *
 * @param points  [{ at, value }]
 */
export function timeArea(id, points, {
  height = 180,
  label = 'value',
  format = fmt.num,
  slot = 1,
  markers = null,        // [{ at, kind }] — events drawn as ticks on the baseline
} = {}) {
  const padding = { top: 12, right: 12, bottom: 22, left: 44 };
  const width = 720;                       // viewBox units; the SVG scales to fit
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;

  if (!points || points.length < 2) {
    return h('.chart', h('.empty', 'Not enough history yet — the series fills in as leases run.'));
  }

  const max = niceMax(Math.max(...points.map((p) => p.value), 1));
  const x = (i) => padding.left + (plotW * i) / (points.length - 1);
  const y = (v) => padding.top + plotH - (plotH * v) / max;

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join(' ');
  const area = `${line} L${x(points.length - 1).toFixed(1)},${(padding.top + plotH).toFixed(1)} L${x(0).toFixed(1)},${(padding.top + plotH).toFixed(1)} Z`;

  const active = getHover(id);
  const hoverIndex = active == null ? null : Math.min(points.length - 1, Math.max(0, active));

  return h('.chart',
    h('svg', {
      viewBox: `0 0 ${width} ${height}`,
      preserveAspectRatio: 'none',
      role: 'img',
      'aria-label': `${label} over time`,
      style: { height: `${height}px` },
      onMouseLeave: () => setHover(id, null),
      onMouseMove: (event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        const ratio = (event.clientX - rect.left) / rect.width;
        setHover(id, Math.round(ratio * (points.length - 1)));
      },
    },
      // gridlines + y ticks
      ticks(max).map((value) => h('g', { key: `t${value}` },
        h('line', {
          class: 'grid-line',
          x1: padding.left, x2: width - padding.right,
          y1: y(value), y2: y(value),
        }),
        h('text', {
          class: 'tick-label',
          x: padding.left - 8, y: y(value) + 3,
          'text-anchor': 'end',
        }, format(value)),
      )),

      h('path', { d: area, fill: SERIES(slot), 'fill-opacity': 0.1 }),
      h('path', { class: 'series-line', d: line, stroke: SERIES(slot) }),

      // event markers on the baseline — identity is the label in the tooltip,
      // not the tick alone
      markers && markers.map((m, i) => h('line', {
        key: `m${i}`,
        x1: x(m.index), x2: x(m.index),
        y1: padding.top + plotH - 6, y2: padding.top + plotH,
        stroke: m.kind === 'forced' ? 'var(--critical)' : 'var(--serious)',
        'stroke-width': 2,
      })),

      h('line', {
        class: 'axis-line',
        x1: padding.left, x2: width - padding.right,
        y1: padding.top + plotH, y2: padding.top + plotH,
      }),

      // x labels: first, middle, last only — a label per bucket is unreadable
      [0, Math.floor((points.length - 1) / 2), points.length - 1].map((i) =>
        h('text', {
          key: `x${i}`,
          class: 'tick-label',
          x: x(i), y: height - 6,
          'text-anchor': i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle',
        }, fmt.time(points[i].at, { seconds: false }))),

      // crosshair
      hoverIndex != null && h('g',
        h('line', {
          class: 'axis-line',
          x1: x(hoverIndex), x2: x(hoverIndex),
          y1: padding.top, y2: padding.top + plotH,
        }),
        h('circle', {
          cx: x(hoverIndex), cy: y(points[hoverIndex].value), r: 4.5,
          fill: SERIES(slot), stroke: 'var(--surface-1)', 'stroke-width': 2,
        }),
      ),

      // the endpoint, direct-labelled — selective, not one per point
      h('circle', {
        cx: x(points.length - 1), cy: y(points[points.length - 1].value), r: 4,
        fill: SERIES(slot), stroke: 'var(--surface-1)', 'stroke-width': 2,
      }),
    ),

    hoverIndex != null && h('.tooltip', {
      style: {
        left: `${((padding.left + (plotW * hoverIndex) / (points.length - 1)) / width) * 100}%`,
        top: `${(y(points[hoverIndex].value) / height) * 100}%`,
      },
    },
      h('h4', fmt.time(points[hoverIndex].at)),
      h('.row',
        h('span.key',
          h('span.legend-swatch', { style: { background: SERIES(slot) } }),
          label),
        h('span.val', format(points[hoverIndex].value)),
      ),
    ),
  );
}

/* ===================================================================== */
/* stacked composition — the pool, per AZ                                */
/* ===================================================================== */

/**
 * Horizontal stacked bars in HTML rather than SVG: the segments carry a 2px
 * surface gap and the row needs to reflow at narrow widths, both of which
 * flexbox does better than a viewBox.
 *
 * @param rows    [{ label, segments: [{ key, value }], total }]
 * @param series  [{ key, name, slot }] — fixed order, fixed colour per key
 */
export function stackedRows(rows, series, { format = fmt.num, tableView = false } = {}) {
  if (tableView) return compositionTable(rows, series, format);

  const colourOf = (key) => SERIES(series.find((s) => s.key === key)?.slot ?? 1);

  return h('div',
    h('.grid', { style: { gap: '10px' } },
      rows.map((row) => {
        const total = row.total ?? row.segments.reduce((sum, s) => sum + s.value, 0);
        return h('.stack-row', { key: row.label },
          h('div',
            h('div', { style: { 'font-weight': '500' } }, row.label),
            row.note && h('div', { style: { 'font-size': '11px', color: 'var(--text-muted)' } }, row.note),
          ),
          h('.stack-track', { role: 'img', 'aria-label': `${row.label}: ${row.segments.map((s) => `${s.value} ${s.key}`).join(', ')}` },
            row.segments.filter((s) => s.value > 0).map((segment) =>
              h('.stack-seg', {
                key: segment.key,
                style: {
                  width: `${total ? (segment.value / total) * 100 : 0}%`,
                  background: colourOf(segment.key),
                },
                title: `${series.find((s) => s.key === segment.key)?.name}: ${format(segment.value)}`,
              })),
          ),
          h('.stack-total', format(total)),
        );
      }),
    ),
    legend(series),
  );
}

function compositionTable(rows, series, format) {
  return h('.table-wrap',
    h('table.data',
      h('thead', h('tr',
        h('th', 'Zone'),
        series.map((s) => h('th.num', { key: s.key }, s.name)),
        h('th.num', 'Total'),
      )),
      h('tbody', rows.map((row) => h('tr', { key: row.label },
        h('td', row.label),
        series.map((s) => h('td.num', { key: s.key },
          format(row.segments.find((seg) => seg.key === s.key)?.value ?? 0))),
        h('td.num', format(row.total ?? row.segments.reduce((sum, s) => sum + s.value, 0))),
      ))),
    ),
  );
}

/* ===================================================================== */
/* bars — one measure across nominal categories                          */
/* ===================================================================== */

/**
 * One colour for every bar. The categories here (lease states, tenants) have no
 * natural order, and shading them by value would burn the colour channel on
 * information the bar length already carries.
 */
export function bars(rows, {
  format = fmt.num,
  slot = 1,
  emphasise = null,      // a key to lift out of the set — one story, one accent
  max = null,
} = {}) {
  if (!rows.length) return h('.empty', 'Nothing to show.');
  const peak = max ?? Math.max(...rows.map((r) => r.value), 1);

  return h('.grid', { style: { gap: '7px' } },
    rows.map((row) => h('div', { key: row.key ?? row.label,
      style: { display: 'grid', 'grid-template-columns': '1fr auto', gap: '10px', 'align-items': 'center' } },
      h('div',
        h('div', { style: { display: 'flex', 'justify-content': 'space-between', gap: '10px', 'font-size': '12px' } },
          h('span', row.label),
          h('span', { style: { color: 'var(--text-muted)', 'font-variant-numeric': 'tabular-nums' } }, format(row.value)),
        ),
        h('div', { style: { height: '6px', background: 'var(--surface-sunken)', 'border-radius': '999px', 'margin-top': '3px' } },
          h('div', {
            style: {
              width: `${(row.value / peak) * 100}%`,
              height: '100%',
              'border-radius': '999px',
              background: emphasise && row.key !== emphasise ? 'var(--seq-250)' : SERIES(slot),
            },
          }),
        ),
      ),
    )),
  );
}

/* ===================================================================== */
/* sparkline — a stat tile's trend, never a chart on its own             */
/* ===================================================================== */
export function sparkline(values, { slot = 1, width = 120, height = 28 } = {}) {
  if (!values || values.length < 2) return null;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const points = values.map((v, i) => {
    const x = (width * i) / (values.length - 1);
    const y = height - ((v - min) / span) * (height - 4) - 2;
    return `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');

  return h('svg', {
    viewBox: `0 0 ${width} ${height}`,
    style: { width: `${width}px`, height: `${height}px` },
    'aria-hidden': 'true',
  },
    h('path', { d: points, fill: 'none', stroke: SERIES(slot), 'stroke-width': 2, 'stroke-linecap': 'round' }),
  );
}

/* ===================================================================== */
/* legend                                                                */
/* ===================================================================== */
export function legend(series) {
  if (series.length < 2) return null;   // one series needs no legend box
  return h('.legend', series.map((s) => h('.legend-item', { key: s.key },
    h('span.legend-swatch', { style: { background: SERIES(s.slot) } }),
    s.name,
  )));
}

/* ===================================================================== */
/* meter — a bounded fraction, with severity in the fill                 */
/* ===================================================================== */
export function meter(fraction, { severity = null } = {}) {
  const clamped = Math.max(0, Math.min(1, fraction || 0));
  const band = severity
    ?? (clamped >= 0.95 ? 'critical' : clamped >= 0.85 ? 'warning' : null);
  return h(`.meter${band ? `.is-${band}` : ''}`, {
    role: 'progressbar',
    'aria-valuenow': Math.round(clamped * 100),
    'aria-valuemin': '0',
    'aria-valuemax': '100',
  }, h('span', { style: { width: `${clamped * 100}%` } }));
}

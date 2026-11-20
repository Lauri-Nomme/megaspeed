/* Trend / time series (prd/50): one headline metric, one point per bucket.
 *
 * The server (`/api/trend`) evaluates the *same* metric functions the headline
 * uses on each week's (or sprint's) window, so this module only draws: a small
 * inline SVG (no canvas, no build step, testable as markup) plus the same
 * numbers as a table. The bucket containing the snapshot is marked
 * **in progress** — its cohort is still accumulating — never hidden.
 *
 * Nothing here fetches or touches the DOM: `main.js` owns the request and `node
 * --test` can assert the whole chart.
 */

import { esc, fmtHours } from './format.js';
import { color } from './theme.js';

const WIDTH = 640;
const HEIGHT = 168;
const PAD = { top: 18, right: 16, bottom: 30, left: 58 };

/** A metric value in its own unit (`ratio`, `hours`, `count`). */
export function trendValue(value, unit) {
  if (value == null || !isFinite(Number(value))) return '–';
  const number = Number(value);
  if (unit === 'ratio') return `${(Math.round(number * 1000) / 10).toFixed(1)}%`;
  if (unit === 'hours') return fmtHours(number);
  if (unit === 'count') return String(Math.round(number));
  return String(number);
}

/** The change between the first and latest measured bucket, in the unit. */
export function trendChange(summary, unit) {
  if (!summary || summary.change == null) return '';
  const value = Number(summary.change);
  if (!isFinite(value) || value === 0) return 'no change';
  const sign = value > 0 ? '+' : '−';
  if (unit === 'ratio') {
    return `${sign}${(Math.round(Math.abs(value) * 1000) / 10).toFixed(1)} pts`;
  }
  if (unit === 'hours') return `${sign}${fmtHours(Math.abs(value))}`;
  return `${sign}${Math.abs(value)}`;
}

/** The metric tabs, from the payload's own catalogue (no local copy). */
export function trendTabs(payload, selected = null) {
  const metrics = (payload && payload.metrics) || [];
  if (!metrics.length) return '';
  const active = selected || (payload && payload.metric);
  return metrics.map((metric) => `<button type="button" role="tab"
      data-trend-metric="${esc(metric.id)}"
      aria-selected="${metric.id === active}"
      class="${metric.id === active ? 'on' : ''}"
      title="${esc(`${metric.label} — ${metric.higher_is_better ? 'higher is better' : 'lower is better'}`)}">${esc(metric.label)}</button>`).join('');
}

/**
 * The chart geometry, pure: one `{x, y, point}` per bucket, plus the y range.
 *
 * Time-proportional x (a bucket is placed at its calendar start), so a missing
 * week is a visible gap; null values are gaps in the line, not zeroes (a
 * month before the team existed is not "flow efficiency 0").
 */
export function trendGeometry(payload) {
  const points = (payload && payload.points) || [];
  const values = points.map((point) => point.value).filter((value) => value != null);
  if (!points.length || !values.length) {
    return { points: [], bounds: null, min: null, max: null };
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || Math.abs(max) || 1;
  const low = min - span * 0.12;
  const high = max + span * 0.12;
  const stamps = points.map((point) => Date.parse(point.bucket_start || point.start));
  const t0 = Math.min(...stamps);
  const t1 = Math.max(...stamps) || t0 + 1;
  const plotW = WIDTH - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const scaleX = (ms) => PAD.left + ((ms - t0) / (t1 - t0 || 1)) * plotW;
  const scaleY = (value) => PAD.top + (1 - ((value - low) / (high - low || 1))) * plotH;
  const mapped = points.map((point, index) => ({
    point,
    index,
    x: Math.round(scaleX(stamps[index]) * 10) / 10,
    y: point.value == null ? null : Math.round(scaleY(point.value) * 10) / 10,
  }));
  return { points: mapped, bounds: { low, high }, min, max };
}

/** A polyline per run of consecutive measured buckets (gaps stay gaps). */
function trendPaths(geometry) {
  const runs = [];
  let run = [];
  for (const entry of geometry.points) {
    if (entry.y == null) {
      if (run.length) runs.push(run);
      run = [];
      continue;
    }
    run.push(entry);
  }
  if (run.length) runs.push(run);
  return runs.map((entries) => entries.map((entry) => `${entry.x},${entry.y}`).join(' '));
}

/** The inline SVG chart, with the active bucket banded and labelled. */
export function trendChart(payload) {
  const geometry = trendGeometry(payload);
  if (!geometry.points.length) {
    return `<p class="empty-row">No bucket in this window has a measured value for ${esc((payload && payload.label) || 'this metric')}.</p>`;
  }
  const unit = payload.unit;
  const ink = color('--text-secondary');
  const line = color('--series-default');
  const grid = color('--canvas-grid-major');
  const band = color('--phase-unobserved');
  const plotW = WIDTH - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const ticks = [geometry.bounds.high, (geometry.bounds.high + geometry.bounds.low) / 2, geometry.bounds.low];
  const gridlines = ticks.map((value) => {
    const y = PAD.top + (1 - ((value - geometry.bounds.low) / (geometry.bounds.high - geometry.bounds.low || 1))) * plotH;
    return `<line x1="${PAD.left}" y1="${y.toFixed(1)}" x2="${WIDTH - PAD.right}" y2="${y.toFixed(1)}"
      stroke="${grid}" stroke-width="1" stroke-dasharray="2 4"/>
      <text x="${PAD.left - 8}" y="${(y + 3.5).toFixed(1)}" text-anchor="end"
        fill="${ink}" font-size="10">${esc(trendValue(value, unit))}</text>`;
  }).join('');
  // The in-progress bucket is shaded and named: a provisional point must not
  // look like a settled one (prd/50).
  const active = geometry.points.filter((entry) => entry.point.in_progress);
  const bands = active.map((entry) => `<rect x="${(entry.x - 7).toFixed(1)}" y="${PAD.top}"
      width="14" height="${plotH}" fill="${band}" opacity="0.16"/>
    <text x="${entry.x.toFixed(1)}" y="${PAD.top - 6}" text-anchor="middle"
      fill="${ink}" font-size="9.5">in progress</text>`).join('');
  const paths = trendPaths(geometry).map((points) => `<polyline points="${points}"
    fill="none" stroke="${line}" stroke-width="2" stroke-linejoin="round"/>`).join('');
  const dots = geometry.points.filter((entry) => entry.y != null).map((entry) => `<circle
      cx="${entry.x}" cy="${entry.y}" r="${entry.point.in_progress ? 3 : 3.4}"
      data-trend-point="${esc(entry.point.label)}"
      data-in-progress="${entry.point.in_progress ? 'true' : 'false'}"
      ${entry.point.in_progress ? `fill="#fff" stroke="${line}" stroke-width="2"` : `fill="${line}"`}>
      <title>${esc(`${entry.point.label}: ${trendValue(entry.point.value, unit)}${entry.point.in_progress ? ' (in progress)' : ''}`)}</title>
    </circle>`).join('');
  const stamps = geometry.points.map((entry) => Date.parse(entry.point.start));
  const axis = [0, geometry.points.length - 1].map((index) => `<text
      x="${index === 0 ? PAD.left : WIDTH - PAD.right}" y="${HEIGHT - 8}"
      text-anchor="${index === 0 ? 'start' : 'end'}" fill="${ink}" font-size="10">${
      esc(geometry.points[index].point.label)}</text>`).join('');
  const label = (payload && payload.label) || 'metric';
  return `<svg class="trend-chart" viewBox="0 0 ${WIDTH} ${HEIGHT}" role="img"
      data-trend-metric="${esc(payload.metric)}"
      aria-label="${esc(`${label} per ${payload.bucket}: ${geometry.points.length} buckets, ${active.length ? `${active.length} in progress` : 'all complete'}`)}">
    <title>${esc(`${label} per ${payload.bucket}`)}</title>
    ${bands}${gridlines}${paths}${dots}${axis}
  </svg>`;
}

/** The same series as a table — readable without hovering, and copyable. */
export function trendTable(payload) {
  const points = (payload && payload.points) || [];
  if (!points.length) return '';
  const rows = points.map((point) => `<tr data-trend-row="${esc(point.label)}"
      class="${point.in_progress ? 'in-progress' : ''}">
      <td class="mono" data-label="bucket">${esc(point.label)}</td>
      <td class="c-dur num" data-label="${esc(payload.label)}">${esc(trendValue(point.value, payload.unit))}${
        point.in_progress ? ' <span class="muted">in progress</span>' : ''}</td>
      <td class="c-dur num" data-label="n">${esc(point.n == null ? '–' : point.n)}</td>
    </tr>`).join('');
  return `<table class="table compact trend-table"><thead><tr>
      <th>bucket</th><th class="c-dur num">${esc(payload.label || 'value')}</th>
      <th class="c-dur num" title="the cohort behind the point (PRs or sprints)">n</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

/** The stats strip: latest value, first → latest change, active bucket. */
export function trendStats(payload) {
  if (!payload) return '';
  const summary = payload.summary || {};
  const latest = (payload.points || []).filter((point) => point.available).slice(-1)[0];
  const active = (summary.in_progress || [])[0];
  return `<span class="stat-inline"><b>${latest ? esc(trendValue(latest.value, payload.unit)) : '–'}</b> latest</span>
    <span class="stat-inline"><b>${summary.measured_buckets || 0}</b>/${summary.buckets || 0} buckets measured</span>
    ${summary.change == null ? '' : `<span class="stat-inline">${esc(trendChange(summary, payload.unit))} since the first bucket</span>`}
    ${active ? `<span class="stat-inline"><b>${esc(active)}</b> in progress</span>` : ''}`;
}

/** Tabs + chart + table. `bucket` switches week/sprint. */
export function trendMarkup(payload, { bucket = 'week', enabled = true } = {}) {
  if (!payload) return '<p class="empty-row">Loading the trend…</p>';
  const tabs = `<div class="trend-toolbar">
      <div class="seg" id="trend-metrics" role="tablist" aria-label="metric">${trendTabs(payload)}</div>
      <div class="seg" id="trend-buckets" role="group" aria-label="bucket">
        <button type="button" data-trend-bucket="week" aria-pressed="${bucket === 'week'}"
          class="${bucket === 'week' ? 'on' : ''}">week</button>
        <button type="button" data-trend-bucket="sprint" aria-pressed="${bucket === 'sprint'}"
          class="${bucket === 'sprint' ? 'on' : ''}">sprint</button>
      </div>
    </div>`;
  if (!payload.available && !(payload.points || []).length) {
    return `${tabs}<p class="empty-row">${esc(payload.note || 'No trend for this metric in this store.')}</p>`;
  }
  return `${tabs}
    <div class="trend-body" ${enabled ? '' : 'data-benchmark-off="true"'}>
      ${trendChart(payload)}
      ${trendTable(payload)}
    </div>
    ${payload.note ? `<p class="panel-note">${esc(payload.note)}</p>` : ''}`;
}

/** Copy-as-Markdown: the same points, one line per bucket. */
export function trendMarkdown(payload) {
  if (!payload) return '';
  const lines = [
    `# ${payload.label || 'Trend'} per ${payload.bucket}`,
    '',
    `${payload.definition || ''}`,
    '',
    `| bucket | ${payload.label || 'value'} | n |`,
    '| --- | --- | --- |',
    ...(payload.points || []).map((point) =>
      `| ${point.label}${point.in_progress ? ' (in progress)' : ''} | ` +
      `${trendValue(point.value, payload.unit)} | ${point.n == null ? '–' : point.n} |`),
  ];
  return lines.join('\n');
}

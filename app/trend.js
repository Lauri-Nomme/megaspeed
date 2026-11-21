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
import {
  annotationEmptyMarkup, annotationList, annotationMarkdown, annotationRecordMarkup,
  annotationSummary, beforeAfterMarkdown, beforeAfterPanel, stabilityMarkdown,
  stabilityStrip,
} from './intervention.js';

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
  // prd/63 B8: no metric is negative, and a count axis must not invent a
  // −2 tick under an empty bucket. Clamp the floor at zero.
  const low = Math.max(0, min - span * 0.12);
  const high = max + span * 0.12;
  const stamps = points.map((point) => Date.parse(point.bucket_start || point.start));
  const t0 = Math.min(...stamps);
  // prd/61: the time domain reaches the last bucket's *end* (not only its
  // start), so an annotation dated inside the in-progress week lands on the
  // plot instead of being clipped past the right edge.
  const ends = points.map((point) => Date.parse(point.end || point.start)).filter((value) => isFinite(value));
  const t1 = Math.max(...stamps, ...ends) || t0 + 1;
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
  return {
    points: mapped,
    bounds: { low, high },
    min,
    max,
    t0,
    t1,
    // prd/61: the annotation layer needs the same time scale as the series (a
    // marker at 8 Oct must land between the buckets, not on the nearest one).
    xAt: (ms) => Math.round(scaleX(ms) * 10) / 10,
    bucketWidth: plotW / Math.max(1, points.length),
  };
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

/** The cumulative-flow bands, as polylines on the chart's own time scale. */
function cumulativeFlowPaths(geometry) {
  const first = geometry.points.find((entry) => entry.point.bands);
  if (!first) return '';
  const tokens = ['--series-default', '--info-accent', '--warning', '--healthy'];
  return (first.point.bands || []).map((band, index) => {
    const points = geometry.points
      .filter((entry) => Array.isArray(entry.point.bands) && entry.point.bands[index])
      .map((entry) => {
        const value = Number(entry.point.bands[index].value || 0);
        const y = PAD.top + (1 - ((value - geometry.bounds.low) / (geometry.bounds.high - geometry.bounds.low || 1))) * (HEIGHT - PAD.top - PAD.bottom);
        return `${entry.x},${Math.round(y * 10) / 10}`;
      });
    if (points.length < 2) return '';
    return `<polyline class="trend-band" data-band="${esc(band.id)}" points="${points.join(' ')}"
      fill="none" stroke="${color(tokens[index % tokens.length])}" stroke-width="1.5"
      stroke-linejoin="round"><title>${esc(band.label)}</title></polyline>`;
  }).join('');
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
  const bands = active.map((entry) => {
    // prd/63 B8: the "in progress" label sat at the right edge and clipped to
    // "in progre". Anchor it inside the plot when the bucket is the last one.
    const anchor = entry.x > WIDTH - PAD.right - 46 ? 'end' : 'middle';
    return `<rect x="${(entry.x - 7).toFixed(1)}" y="${PAD.top}"
      width="14" height="${plotH}" fill="${band}" opacity="0.16"/>
    <text x="${entry.x.toFixed(1)}" y="${PAD.top - 6}" text-anchor="${anchor}"
      fill="${ink}" font-size="9.5">in progress</text>`;
  }).join('');
  const paths = trendPaths(geometry).map((points) => `<polyline points="${points}"
    fill="none" stroke="${line}" stroke-width="2" stroke-linejoin="round"/>`).join('');
  // prd/61: the cumulative-flow view draws the four bands (created / started /
  // in review / merged) from the same buckets, instead of a single value line.
  const flowPaths = payload && payload.metric === 'cumulative_flow'
    ? cumulativeFlowPaths(geometry)
    : '';
  // prd/61: holiday/vacation spans behind the plot (`--warning` 8 %-alpha), and
  // a dated policy change as a 1 px `--info-accent` hairline with a dot on the
  // axis. An annotation outside the window is counted, never drawn.
  const warning = color('--warning');
  const accent = color('--info-accent');
  const spans = ((payload && payload.spans) || [])
    .filter((span) => span.in_window !== false)
    .map((span) => {
      const start = Date.parse(`${span.start}T00:00:00Z`);
      const end = Date.parse(`${span.end}T00:00:00Z`) + 86400000; // the end day is inclusive
      const x1 = Math.max(PAD.left, Math.min(WIDTH - PAD.right, geometry.xAt(start)));
      const x2 = Math.max(PAD.left, Math.min(WIDTH - PAD.right, geometry.xAt(end)));
      const width = Math.max(1, x2 - x1);
      const label = width >= geometry.bucketWidth * 2
        ? `<text x="${(x1 + width / 2).toFixed(1)}" y="${PAD.top + 9}" text-anchor="middle"
            fill="${color('--text-secondary')}" font-size="10">${esc(span.label)}</text>`
        : '';
      return `<g class="trend-span ${esc(span.kind)}" data-span-kind="${esc(span.kind)}"
          data-span-start="${esc(span.start)}" data-span-end="${esc(span.end)}">
        <rect x="${x1.toFixed(1)}" y="${PAD.top}" width="${width.toFixed(1)}"
          height="${plotH}" fill="${warning}" opacity="0.08"/>
        ${label}
      </g>`;
    }).join('');
  const markers = ((payload && payload.annotations) || [])
    .filter((entry) => entry.in_window !== false)
    .map((entry) => {
      const x = geometry.xAt(Date.parse(`${entry.date}T00:00:00Z`));
      if (!isFinite(x) || x < PAD.left - 1 || x > WIDTH - PAD.right + 1) return '';
      // 44 px invisible hit area: a phone marker is tappable, never hover-only.
      return `<g class="trend-annotation" data-annotation-id="${esc(entry.id)}"
          data-kind="${esc(entry.kind)}" data-date="${esc(entry.date)}"
          tabindex="0" role="button" aria-label="${esc(`${entry.label} · ${entry.date}${entry.note ? ` · ${entry.note}` : ''}`)}">
        <line x1="${x.toFixed(1)}" y1="${PAD.top}" x2="${x.toFixed(1)}" y2="${HEIGHT - PAD.bottom}"
          stroke="${accent}" stroke-width="1"/>
        <circle cx="${x.toFixed(1)}" cy="${HEIGHT - PAD.bottom}" r="2.5" fill="${accent}"/>
        <rect class="annotation-hit" x="${(x - 22).toFixed(1)}" y="${PAD.top}"
          width="44" height="${plotH + 14}" fill="transparent"/>
        <title>${esc(`${entry.label} · ${entry.date}${entry.note ? ` · “${entry.note}”` : ''}`)}</title>
      </g>`;
    }).join('');
  const dots = geometry.points.filter((entry) => entry.y != null).map((entry) => {
    // prd/63 F8/V-A5: a bucket whose cohort is partly still open is
    // right-censored — its median is over the survivors. The dot is hollow and
    // titled so, instead of looking as settled as the others.
    const censored = Boolean(entry.point.censored);
    return `<circle
      cx="${entry.x}" cy="${entry.y}" r="${entry.point.in_progress ? 3 : 3.4}"
      class="trend-dot${censored ? ' censored' : ''}"
      data-trend-point="${esc(entry.point.label)}"
      data-in-progress="${entry.point.in_progress ? 'true' : 'false'}"
      data-censored="${censored ? 'true' : 'false'}"
      ${entry.point.in_progress || censored
        ? `fill="none" stroke="${line}" stroke-width="${censored ? 1.2 : 2}"`
        : `fill="${line}"`}>
      <title>${esc(`${entry.point.label}: ${trendValue(entry.point.value, unit)}${entry.point.in_progress ? ' (in progress)' : ''}${censored
        ? ` (right-censored: ${entry.point.censored} PR${entry.point.censored === 1 ? '' : 's'} opened in the bucket still open)` : ''}`)}</title>
    </circle>`;
  }).join('');
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
    ${spans}${bands}${gridlines}${paths}${flowPaths}${dots}${markers}${axis}
  </svg>`;
}

/** The cumulative-flow legend (one line per band, from the payload's own ids). */
export function cumulativeFlowLegend(payload) {
  const point = ((payload && payload.points) || []).find((entry) => entry.bands);
  if (!point) return '';
  const tokens = ['--series-default', '--info-accent', '--warning', '--healthy'];
  return `<p class="cfd-legend">${(point.bands || []).map((band, index) =>
    `<span class="cfd-key" data-band="${esc(band.id)}"><i style="background:${color(tokens[index % tokens.length])}"></i>${esc(band.label)}</span>`).join('')}
    <span class="muted">cumulative PRs by stage, binned on each bucket's end</span></p>`;
}

/** Does a bucket's N clear the payload's minimum-cohort floor? */
function summaryMinNReached(point, payload) {
  const minN = Number((payload && payload.summary && payload.summary.min_n) ?? (payload && payload.min_n));
  if (!Number.isFinite(minN) || minN <= 0) return true;
  if (point && point.in_progress) return true; // labelled, never a headline
  return Number(point && point.n || 0) >= minN;
}

/** The same series as a table — readable without hovering, and copyable. */
export function trendTable(payload) {
  const points = (payload && payload.points) || [];
  if (!points.length) return '';
  const rows = points.map((point) => `<tr data-trend-row="${esc(point.label)}"
      class="${point.in_progress ? 'in-progress' : ''}${point.censored ? ' censored' : ''}">
      <td class="mono" data-label="bucket">${esc(point.label)}</td>
      <td class="c-dur num" data-label="${esc(payload.label)}">${esc(trendValue(point.value, payload.unit))}${
        point.in_progress ? ' <span class="muted">in progress</span>' : ''}${
        point.censored ? ` <span class="muted" title="right-censored: this bucket's cohort includes PRs that are still open">right-censored${
          point.censored ? ` (${esc(point.censored)} open)` : ''}</span>` : ''}</td>
      <td class="c-dur num" data-label="n">${esc(point.n == null ? '–' : point.n)}${
        summaryMinNReached(point, payload) ? '' : ' <span class="muted" title="below the minimum cohort — drawn, not headlined">low n</span>'}</td>
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
  // prd/63 B8: the headline is the latest *settled* bucket (the server already
  // chose it), names its label and its N, and never compares against an empty
  // in-progress bucket.
  // prd/63 F8/V-A5: the headline also respects the metric's minimum cohort
  // (n ≥ min_n). When no bucket clears it the strip says so instead of
  // quoting an n=1 / n=3 number as a trend; the latest low-N bucket is named
  // so the reader knows which one was skipped.
  const latest = summary.latest;
  const label = summary.latest_label;
  const active = (summary.in_progress || [])[0];
  const minN = summary.min_n;
  const below = Array.isArray(summary.below_min) ? summary.below_min : [];
  const lowAfter = Array.isArray(summary.low_n_after_headline)
    ? summary.low_n_after_headline : [];
  if (latest == null) {
    const newest = below.length ? below[below.length - 1] : null;
    return `<span class="stat-inline"><b>–</b> no bucket meets the n≥${esc(minN ?? 5)} floor${
      newest ? ` (the latest measured bucket, ${esc(newest.label)}, has n=${esc(newest.n)})` : ''}</span>
      <span class="stat-inline"><b>${summary.measured_buckets || 0}</b>/${summary.buckets || 0} buckets measured</span>
      ${active ? `<span class="stat-inline"><b>${esc(active)}</b> in progress</span>` : ''}`;
  }
  return `<span class="stat-inline"><b>${latest == null ? '–' : esc(trendValue(latest, payload.unit))}</b> latest${
    label ? ` (${esc(label)}${summary.latest_n != null ? ` · n=${esc(summary.latest_n)}` : ''})` : ''}${
    summary.latest_provisional ? ' <span class="muted">provisional</span>' : ''}</span>
    <span class="stat-inline"><b>${summary.measured_buckets || 0}</b>/${summary.buckets || 0} buckets measured</span>
    ${summary.change == null ? '' : `<span class="stat-inline">${esc(trendChange(summary, payload.unit))} since the first measured bucket</span>`}
    ${summary.latest_censored
      ? `<span class="stat-inline censored" title="the bucket is right-censored: PRs opened in it have not merged yet, so they cannot enter the median">right-censored${summary.latest_censored_n
        ? ` · ${esc(summary.latest_censored_n)} from that bucket still open` : ''}</span>`
      : ''}
    ${lowAfter.length
      ? `<span class="stat-inline muted" title="a bucket below the minimum cohort is drawn and named, but is not the headline">${esc(lowAfter.length)} newer bucket${lowAfter.length === 1 ? '' : 's'} below n≥${esc(minN ?? 5)}${lowAfter.length ? ` (${esc(lowAfter.join(', '))})` : ''}</span>`
      : ''}
    ${active ? `<span class="stat-inline"><b>${esc(active)}</b> in progress${summary.latest_provisional ? '' : ' (not in the headline)'}</span>` : ''}`;
}

/** The error state a failed `/api/trend` fetch renders — never a spinner. */
export function trendErrorMarkup(message) {
  const text = String(message || 'the request failed');
  return `<div class="trend-error" role="alert" data-trend-error="true">
    <p><b>The trend did not load.</b> ${esc(text)}</p>
    <p class="muted">This is the request's error, not an empty chart: the metric,
      bucket and scope are still in the URL, so a retry (or reload) asks the
      server the same question again.</p>
    <button type="button" class="chip" data-trend-retry>Retry the trend</button>
  </div>`;
}

/** Tabs + chart + annotations + before/after + stability + table. */
export function trendMarkup(payload, { bucket = 'week', enabled = true, error = null, compare = null } = {}) {
  if (error) return trendErrorMarkup(error);
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
  const censored = (payload.points || []).filter((point) => point.censored).length;
  const annotations = payload.annotations || [];
  const catalogue = payload.kinds || [];
  const record = catalogue.length ? annotationRecordMarkup(catalogue) : '';
  return `${tabs}
    <div class="trend-body" ${enabled ? '' : 'data-benchmark-off="true"'}>
      ${trendChart(payload)}
      ${payload.metric === 'cumulative_flow' ? cumulativeFlowLegend(payload) : ''}
      ${annotationSummary(payload)}
      ${annotations.length ? annotationList(payload) : (catalogue.length ? annotationEmptyMarkup(catalogue) : '')}
      ${annotations.length ? beforeAfterPanel(compare, {
        annotations,
        metric: compare ? compare.metric : null,
      }) : ''}
      ${stabilityStrip(payload.stability)}
      ${trendTable(payload)}
    </div>
    ${annotations.length ? '' : ''}
    ${record && annotations.length ? record : ''}
    ${censored ? `<p class="panel-note" data-trend-censored="${censored}">a hollow point is
      right-censored: the bucket's cohort still has open PRs, so only the merged ones
      can enter its value.</p>` : ''}
    ${payload.note ? `<p class="panel-note">${esc(payload.note)}</p>` : ''}`;
}

/** Copy-as-Markdown: the points, the annotations, the verdict and the stability. */
export function trendMarkdown(payload, { compare = null } = {}) {
  if (!payload) return '';
  const lines = [
    `# ${payload.label || 'Trend'} per ${payload.bucket}`,
    '',
    `${payload.definition || ''}`,
    '',
    `| bucket | ${payload.label || 'value'} | n |`,
    '| --- | --- | --- |',
    ...(payload.points || []).map((point) =>
      `| ${point.label}${point.in_progress ? ' (in progress)' : ''}${
        point.censored ? ` (right-censored${point.censored ? `: ${point.censored} still open` : ''})` : ''} | ` +
      `${trendValue(point.value, payload.unit)} | ${point.n == null ? '–' : point.n} |`),
  ];
  const extra = [
    annotationMarkdown(payload),
    compare ? beforeAfterMarkdown(compare) : '',
    stabilityMarkdown(payload.stability),
  ].filter(Boolean);
  return [lines.join('\n'), ...extra].join('\n\n');
}

/* prd/53 — can we trust the worklogs?
 *
 * The Rules card used to blame *late logging*. The measured defect is that
 * logged hours do not track the story's size at all: r(points, logged) ≈ −0.05
 * on the QA store, a 13-point story logs 2–3 h, and the total is a fraction of
 * the active PR time. Lateness stays on the card, as a secondary line.
 *
 * This module is the card's small chart: logged hours by point size (the
 * flatness), logged vs active (the effort gap), and the same points ↔ logged
 * correlation bucketed by week — "is logging getting more trustworthy?".
 *
 * Pure functions over the API's `metrics.worklog_cross_check` block, so the
 * numbers are the server's (one metric, one definition) and the whole panel is
 * unit-testable without a browser. `docs/concepts.md` §1: durations are
 * formatted by `fmtHours`, never an ad-hoc hour string.
 */

import { esc, fmtHours } from './format.js';

/** Pearson r as the card prints it: "−0.05"-style, two decimals. */
export function fmtR(value) {
  if (value == null || !Number.isFinite(Number(value))) return '–';
  const n = Number(value);
  // a typographic minus reads as a sign, not a hyphen in a sentence
  return `${n < 0 ? '−' : ''}${Math.abs(n).toFixed(2)}`;
}

/** Hours per point, a *rate*: "4 h/pt" (not a business-hour duration). */
export function fmtRate(hours) {
  if (hours == null || !Number.isFinite(Number(hours))) return '–';
  return `${Number(hours).toFixed(1)} h/pt`;
}

/** "logged hours do not track story points" vs a real relationship. */
export function flatness(metric) {
  const r = metric ? metric.points_logged_correlation : null;
  if (r == null) return 'unknown';
  if (Math.abs(r) < 0.3) return 'flat';
  if (r > 0) return 'tracks';
  return 'inverse';
}

/**
 * The card's headline sentence (concepts.md §6, prd/53 §1).
 *
 * The effort gap is the headline; the words state the correlation, the plain
 * hours-per-story median and the logged-vs-active ratio, so the reader does not
 * have to interpret `r`.
 */
export function worklogHeadline(metric) {
  if (!metric || !metric.available) return '';
  const kind = flatness(metric);
  const n = Number(metric.points_logged_n || 0);
  const median = metric.median_logged_hours;
  const perPoint = metric.median_hours_per_point;
  const ratio = metric.logged_to_active_ratio;
  const parts = [];
  if (kind === 'flat') {
    parts.push(
      `Logged hours do not track story points (r = ${fmtR(metric.points_logged_correlation)}` +
      `${n ? ` over ${n} stor${n === 1 ? 'y' : 'ies'}` : ''})`,
    );
    if (median != null && perPoint != null) {
      parts.push(
        `the median story logs ${fmtHours(median)} whatever its size (${fmtRate(perPoint)})`,
      );
    }
  } else if (kind === 'inverse') {
    parts.push(
      `Bigger stories log fewer hours (r = ${fmtR(metric.points_logged_correlation)}) — ` +
      'the worklog is not measuring effort',
    );
  } else if (kind === 'tracks') {
    parts.push(
      `Logged hours track story points (r = ${fmtR(metric.points_logged_correlation)}` +
      `${n ? ` over ${n} stor${n === 1 ? 'y' : 'ies'}` : ''})`,
    );
  }
  if (ratio != null) {
    parts.push(
      `the team logs ${fmtHours(metric.logged_hours)} against ` +
      `${fmtHours(metric.active_hours)} of active PR time (${Number(ratio).toFixed(2)}×)`,
    );
  }
  return parts.length ? `${parts.join('; ')}.` : '';
}

/** The lateness line, secondary by design (prd/53 §1). */
export function latenessLine(metric) {
  if (!metric || !metric.available) return '';
  const delay = metric.mean_delay_days;
  if (delay == null) return '';
  const late = Math.round((Number(metric.late_share) || 0) * 100);
  return `lateness is the minor problem: ${Number(delay).toFixed(1)} calendar days mean ` +
    `logging delay, ${late}% more than a week late (secondary)`;
}

/** One bar per point size: median logged hours, plus its story count. */
export function pointsBars(metric) {
  const byPoints = (metric && metric.logged_by_points) || {};
  const sizes = Object.keys(byPoints)
    .map(Number)
    .filter((value) => Number.isFinite(value))
    .sort((a, b) => a - b);
  const max = sizes.reduce((best, size) => Math.max(best, Number(byPoints[size]) || 0), 0);
  return sizes.map((size) => {
    const hours = Number(byPoints[String(size)]) || 0;
    return {
      points: size,
      hours,
      share: max > 0 ? hours / max : 0,
      label: `${size} pt`,
    };
  });
}

/** Logged vs active as two comparable bars (the effort gap). */
export function effortGap(metric) {
  if (!metric || !metric.available) return null;
  const logged = Number(metric.logged_hours) || 0;
  const active = Number(metric.active_hours) || 0;
  const max = Math.max(logged, active, 0);
  return {
    logged,
    active,
    ratio: metric.logged_to_active_ratio,
    loggedShare: max > 0 ? logged / max : 0,
    activeShare: max > 0 ? active / max : 0,
    gap: active - logged,
  };
}

/**
 * The per-week trend: r(points, logged) and median h/pt per bucket.
 *
 * A bucket with fewer than three stories has no correlation (a two-point r is
 * ±1 by construction, so the API says `null`); it stays on the axis so a gap is
 * visible instead of collapsed.
 */
export function trendSeries(metric) {
  const trend = (metric && Array.isArray(metric.trend)) ? metric.trend : [];
  return trend.map((row, index) => ({
    index,
    week: row.week_start,
    label: row.label,
    stories: Number(row.stories) || 0,
    correlation: row.correlation == null ? null : Number(row.correlation),
    hoursPerPoint: row.median_hours_per_point,
    loggedHours: Number(row.logged_hours) || 0,
    current: Boolean(row.current),
  }));
}

/** "improving", "worsening" or "flat" across the buckets that have an r. */
export function trendVerdict(points) {
  const known = (points || []).filter((point) => point.correlation != null);
  if (known.length < 2) return 'not enough weeks';
  const first = known[0].correlation;
  const last = known[known.length - 1].correlation;
  const delta = last - first;
  if (Math.abs(delta) < 0.1) return 'flat';
  return delta > 0 ? 'improving' : 'worsening';
}

const SPARK_W = 260;
const SPARK_H = 46;
const SPARK_PAD = 4;

/** A small SVG sparkline of r by week; the zero line is the "no signal" mark. */
export function trendSparkline(points, { size = 2.4 } = {}) {
  const rows = (points || []);
  if (!rows.length) return '';
  const span = Math.max(rows.length - 1, 1);
  const x = (index) => SPARK_PAD + (index / span) * (SPARK_W - 2 * SPARK_PAD);
  const y = (value) => {
    const clamped = Math.max(-1, Math.min(1, value));
    const mid = SPARK_H / 2;
    return mid - clamped * (mid - SPARK_PAD);
  };
  const mid = SPARK_H / 2;
  const segments = [];
  let current = [];
  for (const point of rows) {
    if (point.correlation == null) {
      if (current.length) segments.push(current);
      current = [];
      continue;
    }
    current.push(`${x(point.index).toFixed(1)},${y(point.correlation).toFixed(1)}`);
  }
  if (current.length) segments.push(current);
  const lines = segments.map((segment) => (segment.length === 1
    ? `<circle class="spark-dot" cx="${segment[0].split(',')[0]}" cy="${segment[0].split(',')[1]}" r="1.6"></circle>`
    : `<polyline class="spark-line" points="${segment.join(' ')}"></polyline>`)).join('');
  const dots = rows.map((point) => (point.correlation == null ? '' :
    `<circle class="spark-node${point.current ? ' current' : ''}"
       cx="${x(point.index).toFixed(1)}" cy="${y(point.correlation).toFixed(1)}" r="${size}"
       data-trend-week="${esc(point.week || '')}"
       data-trend-r="${point.correlation}"><title>${esc(point.label || '')} · ` +
      `r = ${fmtR(point.correlation)} · ${point.stories} stor${point.stories === 1 ? 'y' : 'ies'}</title></circle>`))
    .join('');
  return `<svg class="worklog-spark" viewBox="0 0 ${SPARK_W} ${SPARK_H}" width="${SPARK_W}" height="${SPARK_H}"
    role="img" aria-label="per-week correlation between logged hours and story points">
    <line class="spark-zero" x1="${SPARK_PAD}" x2="${SPARK_W - SPARK_PAD}" y1="${mid}" y2="${mid}"></line>
    ${lines}${dots}
  </svg>`;
}

/** The whole card body (chart + trend), or '' when the store has no worklogs. */
export function worklogPanel(metric) {
  if (!metric || !metric.available) return '';
  const bars = pointsBars(metric);
  const gap = effortGap(metric);
  const trend = trendSeries(metric);
  const headline = worklogHeadline(metric);
  const late = latenessLine(metric);
  const byPoints = bars.length ? `<div class="worklog-chart" aria-label="median logged hours by story points">
      ${bars.map((bar) => `<div class="worklog-bar" data-worklog-points="${bar.points}"
        title="${bar.points} pt stories log a median ${fmtHours(bar.hours)}">
        <i style="height:${Math.max(4, Math.round(bar.share * 100))}%"></i>
        <span class="worklog-bar-k mono">${esc(bar.label)}</span>
        <span class="worklog-bar-v mono">${esc(fmtHours(bar.hours))}</span>
      </div>`).join('')}
    </div>` : '';
  const effort = gap ? `<div class="worklog-gap" aria-label="logged hours vs active PR time">
      <div class="worklog-gap-row"><span class="k">logged</span>
        <span class="worklog-track"><i style="width:${Math.round(gap.loggedShare * 100)}%"></i></span>
        <span class="worklog-gap-v mono">${esc(fmtHours(gap.logged))}</span></div>
      <div class="worklog-gap-row"><span class="k">active (PR)</span>
        <span class="worklog-track"><i class="active" style="width:${Math.round(gap.activeShare * 100)}%"></i></span>
        <span class="worklog-gap-v mono">${esc(fmtHours(gap.active))}</span></div>
      <p class="worklog-gap-note">${gap.ratio == null ? 'no active PR time to compare'
        : `<b>${Number(gap.ratio).toFixed(2)}×</b> — the two clocks are ${esc(fmtHours(Math.abs(gap.gap)))} apart`}</p>
    </div>` : '';
  const trendBlock = trend.length ? `<div class="worklog-trend" data-worklog-trend>
      <span class="legend-label">per week</span>
      ${trendSparkline(trend)}
      <span class="worklog-trend-note">${esc(trendVerdict(trend))} · ${
        trend.filter((point) => point.correlation != null).length
      } of ${trend.length} weeks with an r${
        trend.some((point) => point.current) ? ' · the last bucket is the most recent week' : ''}</span>
    </div>` : '';
  return `<section class="worklog-panel" data-worklog-panel>
    <div class="worklog-top">
      <h3>Logged hours vs story points</h3>
      <span class="pill ${flatness(metric) === 'tracks' ? 'healthy' : 'warning'} mono"
        data-worklog-r="${metric.points_logged_correlation ?? ''}">r = ${fmtR(metric.points_logged_correlation)}</span>
    </div>
    ${headline ? `<p class="worklog-headline" data-worklog-headline>${esc(headline)}</p>` : ''}
    ${byPoints}
    ${effort}
    ${trendBlock}
    ${late ? `<p class="panel-note worklog-late" data-worklog-late>${esc(late)}.</p>` : ''}
  </section>`;
}

/** The Markdown export of the same block (Copy as Markdown on Rules). */
export function worklogMarkdown(metric) {
  if (!metric || !metric.available) return [];
  const lines = [
    '## Worklog honesty',
    '',
    `- ${worklogHeadline(metric)}`,
    `- lateness: ${latenessLine(metric) || '–'}`,
  ];
  const bars = pointsBars(metric);
  if (bars.length) {
    lines.push(`- median logged hours by points: ${
      bars.map((bar) => `${bar.points} pt ${fmtHours(bar.hours)}`).join(' · ')}`);
  }
  const trend = trendSeries(metric);
  if (trend.length) {
    lines.push(`- logged ↔ points r by week: ${
      trend.map((point) => `${point.label} ${point.correlation == null
        ? '–' : fmtR(point.correlation)}`).join(' · ')} (${trendVerdict(trend)})`);
  }
  return lines;
}

/* PR-size calibration (prd/39) — the size rule's own numbers, made visible.
 *
 * The "PR size" rule flags a PR as large when `commits > cap` OR
 * `changed_lines > line_cap`. The caps are a per-tenant setting (the UI stores
 * them with the tenant's other preferences, like the scope/timeframe); this
 * module owns the pure half: normalising the caps, counting the PRs over them,
 * turning the API's bucket edges into bars, and the panel markup.
 *
 * The panel draws the two distributions the API returns (commits and changed
 * lines) with the current caps as markers, and two sliders. Moving a slider
 * re-counts the *same* per-PR rows (`payload.rows`), so "N of M PRs flagged
 * large" updates live and exactly — never from summing two independent
 * histograms, which would count a PR that breaches both caps twice.
 *
 * Pure markup + pure arithmetic: no DOM, no fetch, so `node --test` covers the
 * re-bucketing without a browser. main.js owns the input events and persistence.
 */

import { esc } from './format.js';

export const DEFAULT_SIZE_CAPS = Object.freeze({ commits: 10, lines: 400 });
export const SIZE_CAP_MIN = 1;
export const SIZE_CAP_MAX = 10_000;

function capNumber(value, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.max(SIZE_CAP_MIN, Math.min(SIZE_CAP_MAX, Math.round(number)));
}

/** A valid `{commits, lines}` pair; anything missing or malformed falls back. */
export function normalizeSizeCaps(raw) {
  const source = raw && typeof raw === 'object' ? raw : {};
  return {
    commits: capNumber(source.commits, DEFAULT_SIZE_CAPS.commits),
    lines: capNumber(source.lines, DEFAULT_SIZE_CAPS.lines),
  };
}

/* The per-tenant preference map. The shape mirrors `state.scopes` (prd/12):
 * one entry per tenant, because two datasets can need different calibrations.
 */
export function sizeCapsForTenant(map, tenant) {
  const entry = map && tenant ? map[tenant] : null;
  return normalizeSizeCaps(entry);
}

export function rememberSizeCaps(map, tenant, caps) {
  const next = { ...(map && typeof map === 'object' ? map : {}) };
  if (tenant) next[tenant] = normalizeSizeCaps(caps);
  return next;
}

/** The query string that asks the API to evaluate at these caps. */
export function capsQuery(caps) {
  const chosen = normalizeSizeCaps(caps);
  return `size_commits=${chosen.commits}&size_lines=${chosen.lines}`;
}

/**
 * Count the PRs a cap pair flags large, from the API's per-PR rows.
 *
 * `large` counts a PR once even when it breaches both caps; `overCommits` /
 * `overLines` are the per-dimension counts (their sum may exceed `large`).
 */
export function flagCounts(rows, caps) {
  const chosen = normalizeSizeCaps(caps);
  let large = 0;
  let overCommits = 0;
  let overLines = 0;
  for (const row of rows || []) {
    const commits = Number(row.commits) || 0;
    const lines = row.diff_size == null ? null : Number(row.diff_size);
    const overCommit = commits > chosen.commits;
    const overLine = lines != null && lines > chosen.lines;
    if (overCommit) overCommits += 1;
    if (overLine) overLines += 1;
    if (overCommit || overLine) large += 1;
  }
  return {
    large,
    total: (rows || []).length,
    overCommits,
    overLines,
    share: rows && rows.length ? large / rows.length : 0,
  };
}

/** Where a cap sits on a bucket axis, as a 0–100 percentage. */
export function capPercent(edges, cap) {
  if (!Array.isArray(edges) || edges.length < 2) return 0;
  const first = Number(edges[0]) || 0;
  const last = Number(edges[edges.length - 1]);
  const span = last - first;
  if (!(span > 0)) return 0;
  const value = Math.max(first, Math.min(last, Number(cap) || 0));
  return ((value - first) / span) * 100;
}

/** Bucket edges + counts -> positioned bars (percentages, for HTML). */
export function histogramBars(series) {
  const edges = (series && series.edges) || [];
  const counts = (series && series.counts) || [];
  if (edges.length < 2) return [];
  const first = Number(edges[0]) || 0;
  const last = Number(edges[edges.length - 1]);
  const span = last - first || 1;
  const top = Math.max(1, ...counts.map((count) => Number(count) || 0));
  return counts.map((count, index) => {
    const from = Number(edges[index]) || 0;
    const to = Number(edges[index + 1]) || from;
    const value = Number(count) || 0;
    const lastBucket = index === counts.length - 1;
    return {
      left: ((from - first) / span) * 100,
      width: Math.max(0.35, ((to - from) / span) * 100),
      height: value ? Math.max(3, (value / top) * 100) : 0,
      count: value,
      from,
      to,
      last: lastBucket,
      title: `${from}–${to}${lastBucket ? '+' : ''}: ${value} PR${value === 1 ? '' : 's'}`,
    };
  });
}

function barsMarkup(series) {
  return histogramBars(series).map((bar) => `
    <span class="prsize-bar" style="left:${bar.left.toFixed(2)}%;width:${bar.width.toFixed(2)}%;height:${bar.height.toFixed(1)}%"
      title="${bar.title}"></span>`).join('');
}

function seriesMarkup(kind, label, series, caps, sliderMax, { available = true } = {}) {
  const cap = caps[kind];
  const max = Math.max(sliderMax, cap);
  // prd/41 P5: a step of 1 keeps the slider on the cap it shows. With a
  // min of 1 and a step of 10 the grid was 1, 11, … so the default 400 read
  // 401 and round caps (100/150/200) could not be set at all.
  const step = 1;
  const hint = kind === 'commits'
    ? 'a PR is large above this many commits'
    : 'a PR is large above this many changed lines';
  const chart = available
    ? `<div class="prsize-chart" role="img" aria-label="${label} distribution">
        ${barsMarkup(series)}
        <span class="prsize-marker" data-pr-size-marker="${kind}"
          style="left:${capPercent(series && series.edges, cap).toFixed(2)}%"
          title="cap: ${cap}"><b data-pr-size-cap="${kind}">${cap}</b></span>
      </div>`
    : `<p class="prsize-empty">changed lines are not captured in this store, so the
        size rule can only calibrate on commits. The line cap is kept for the
        store that captures diff size.</p>`;
  return `<div class="prsize-row" data-pr-size-series="${kind}">
    <div class="prsize-rowhead">
      <label for="pr-size-${kind}">${label}</label>
      <span class="prsize-cap-label">cap <b data-pr-size-cap-label="${kind}">${cap}</b></span>
    </div>
    ${chart}
    <input type="range" id="pr-size-${kind}" data-pr-size="${kind}"
      min="${SIZE_CAP_MIN}" max="${max}" step="${step}" value="${cap}"
      aria-label="${hint}" ${available ? '' : 'disabled'}>
  </div>`;
}

/**
 * prd/42 M8: size vs cycle next to the sliders.
 *
 * A firing size rule is not evidence that size is the lever. The correlation is
 * the evidence the sliders were missing: a weak r means "cap PRs" would be a
 * guess (the QA run measured r ≈ 0.20 for lines), while review rounds explain
 * far more of the cycle (r ≈ 0.60).
 */
export function correlationLine(payload) {
  const correlations = payload && payload.correlations;
  if (!correlations || !correlations.n) return '';
  const fmt = (value) => (value == null ? '—' : Number(value).toFixed(2));
  const weak = correlations.commits != null && Math.abs(correlations.commits) < 0.3
    && (correlations.lines == null || Math.abs(correlations.lines) < 0.3);
  return `<p class="prsize-corr" data-pr-size-corr="${fmt(correlations.commits)}">
    <b>size vs cycle</b>: r = ${esc(fmt(correlations.commits))} for commits${
      correlations.lines == null ? '' : ` · ${esc(fmt(correlations.lines))} for changed lines`}${
      correlations.rounds == null ? '' : ` · ${esc(fmt(correlations.rounds))} for review rounds`}
    over ${correlations.n} closed PR${correlations.n === 1 ? '' : 's'}${
      weak ? ' — weak, so capping size is not a proven lever by itself' : ''}.
    <span class="muted">${esc(correlations.basis || '')}</span></p>`;
}

/**
 * The "PR size" panel: two histograms + two sliders + the live count.
 *
 * `payload` is `/api/pr-size`; `caps` is the tenant's persisted choice (falls
 * back to the payload's caps, then the 10 / 400 defaults). The count is computed
 * from `payload.rows`, so it is exact and moves with the slider.
 */
export function prSizePanel(payload, { caps = null } = {}) {
  if (!payload || !payload.ok) return '';
  const chosen = normalizeSizeCaps(caps || payload.caps);
  const counts = flagCounts(payload.rows, chosen);
  const commits = payload.commits || {};
  const lines = payload.lines || {};
  const commitMax = Math.max(Number(commits.max) || 0, chosen.commits) + 2;
  const lineMax = Math.max(Number(lines.max) || 0, chosen.lines * 2, 100);
  const pending = !lines.available;
  return `<section class="prsize-panel" id="pr-size-panel" data-pr-size-panel
      aria-labelledby="pr-size-title">
    <div class="prsize-head">
      <h2 class="policy-h" id="pr-size-title">PR size</h2>
      <span class="muted">calibrate the size rule against this store</span>
      <span class="grow"></span>
      <button type="button" class="chip" data-pr-size-reset
        title="reset to the stated defaults (${DEFAULT_SIZE_CAPS.commits} commits / ${DEFAULT_SIZE_CAPS.lines} changed lines)">Reset to ${DEFAULT_SIZE_CAPS.commits} / ${DEFAULT_SIZE_CAPS.lines}</button>
    </div>
    <p class="prsize-count" id="pr-size-count" aria-live="polite">At these caps,
      <b data-pr-size-large>${counts.large}</b> of <b data-pr-size-total>${counts.total}</b>
      PRs are flagged large<span class="muted" data-pr-size-split> ·
      ${counts.overCommits} over commits · ${counts.overLines} over changed lines</span>.</p>
    ${correlationLine(payload)}
    ${seriesMarkup('commits', 'commits per PR', commits, chosen, commitMax)}
    ${seriesMarkup('lines', 'changed lines per PR', lines, chosen, lineMax, { available: !pending })}
    <p class="panel-note">A PR is large iff <code>commits &gt; cap</code> or
      <code>changed lines &gt; line cap</code>. The same caps drive the
      <span class="mono">pr_size</span> rule and its drill-down; the choice is
      saved per tenant.${pending
        ? ' Diff size is not captured yet, so the line cap is inactive in this store.'
        : ''}</p>
  </section>`;
}

/* Intervention-aware trend (prd/61): annotations, before/after, stability.
 *
 * Three pure markup builders for the Trend page, so `node --test` can assert
 * the whole answer without a DOM:
 *
 *   1. the annotation layer (`annotationSummary`, `annotationList`,
 *      `annotationRecordMarkup`) — dated policy changes and calendar spans;
 *   2. the "Did it work?" panel (`beforeAfterPanel`) — both `n`s, the CI and
 *      exactly one of four outcomes, never a naked p-value;
 *   3. the stability strip (`stabilityStrip`) — Theil–Sen WIP/throughput slopes,
 *      arrivals vs departures, and Little's Law only when it diverges.
 *
 * Honesty rules encoded here, not only in prose: `can't tell at this n` is a
 * neutral outline (never a failure colour), the p-value is a tooltip, and the
 * before/after copy always names the cohort basis.
 */

import { esc, fmtHours } from './format.js';

/** The four outcomes, exactly (concepts.md §6, prd/61). */
export const OUTCOMES = {
  better: { label: 'better', tone: 'positive' },
  worse: { label: 'worse', tone: 'critical' },
  no_clear_change: { label: 'no clear change', tone: 'neutral' },
  cant_tell: { label: "can't tell at this n", tone: 'neutral' },
};

export const OUTCOME_ORDER = ['better', 'worse', 'no_clear_change', 'cant_tell'];

export const INTERVAL_NOTE = 'A before/after is not an experiment: it compares two '
  + 'samples of items either side of the change, with everything else that also '
  + 'changed. The interval is the answer.';

/** A number in the comparison's own unit (`hours`, `ratio`, `count`). */
export function compareValue(value, unit = 'hours', { signed = false } = {}) {
  if (value == null || !isFinite(Number(value))) return '–';
  const number = Number(value);
  const sign = signed ? (number > 0 ? '+' : number < 0 ? '−' : '') : (number < 0 ? '−' : '');
  const abs = Math.abs(number);
  let body;
  if (unit === 'hours') body = fmtHours(abs);
  else if (unit === 'ratio') body = `${(Math.round(abs * 1000) / 10).toFixed(1)} %`;
  else if (unit === 'count') body = String(Math.round(abs));
  else body = String(abs);
  return `${sign}${body}`;
}

/** `better`/`worse` tint, `no clear change`/`can't tell` a neutral outline. */
export function outcomePill(outcome) {
  const spec = OUTCOMES[outcome] || { label: String(outcome || '–'), tone: 'neutral' };
  return `<span class="outcome-pill ${esc(spec.tone)}" data-outcome="${esc(outcome)}">${esc(spec.label)}</span>`;
}

/** The verdict sentence from the server, else a local fallback in the same voice. */
export function verdictSentence(compare) {
  if (!compare) return '';
  if (compare.note) return compare.note;
  const outcome = compare.outcome;
  if (outcome === 'cant_tell') {
    return `Can't tell at this n: ${compare.before?.n ?? 0} before, ${compare.after?.n ?? 0} after.`;
  }
  return OUTCOMES[outcome]?.label || '';
}

/** The p-value lives in a tooltip: never a headline, never a colour. */
function pTooltip(compare) {
  if (compare?.p_value == null) return '';
  const method = compare.test_method ? ` (${compare.test_method})` : '';
  return `title="Mann–Whitney U p=${compare.p_value}${method}; the interval, not the p, is the answer"`;
}

/**
 * The CI as a 12 px line with a 2 px range bar centred on zero.
 * Returns markup plus a text form; a missing interval states why.
 */
export function ciBar(compare) {
  const unit = compare?.unit || 'hours';
  const ci = compare?.ci;
  if (!Array.isArray(ci) || ci.length !== 2) {
    const why = compare?.reason === 'small_n'
      ? `no interval: fewer than ${compare?.min_items ?? 8} finished items on one side`
      : "no interval: this window cannot support one";
    return `<div class="ci-block"><span class="ci-none">${esc(why)}</span></div>`;
  }
  const [low, high] = ci.map(Number);
  const diff = Number(compare.diff || 0);
  const reach = Math.max(Math.abs(low), Math.abs(high), Math.abs(diff), 0.001);
  const span = reach * 1.1;
  const pos = (value) => `${((value + span) / (2 * span)) * 100}%`;
  const width = `${((high - low) / (2 * span)) * 100}%`;
  return `<div class="ci-block" data-ci-low="${low}" data-ci-high="${high}" data-ci-diff="${diff}">
    <div class="ci-bar" role="img" aria-label="${esc(`95 % interval ${compareValue(low, unit, { signed: true })} to ${compareValue(high, unit, { signed: true })}`)}">
      <span class="ci-zero" style="left:${pos(0)}"></span>
      <span class="ci-range" style="left:${pos(low)};width:${width}"></span>
      <span class="ci-diff" style="left:${pos(diff)}"></span>
    </div>
    <span class="ci-text">95 % CI <b>${esc(compareValue(low, unit, { signed: true }))} … ${esc(compareValue(high, unit, { signed: true }))}</b></span>
  </div>`;
}

function statCell(block, unit) {
  return `<td class="num" data-label="n">${esc(block?.n ?? '–')}</td>
    <td class="num" data-label="p50">${esc(compareValue(block?.p50, unit))}</td>
    <td class="num" data-label="p85">${esc(compareValue(block?.p85, unit))}</td>`;
}

/**
 * The "Did it work?" panel: verdict first, then the two-column table and the CI.
 *
 * `compare` is `/api/trend/compare`; `annotations` is the trend payload's list
 * (the select offers every dated change, not only the selected one).
 */
export function beforeAfterPanel(compare, { annotations = [], metric = null } = {}) {
  if (!compare) {
    return `<div class="before-after empty" data-before-after="empty">
      <p class="panel-note">Record a change to compare the items either side of it.</p>
    </div>`;
  }
  const unit = compare.unit || 'hours';
  const selected = compare.annotation?.id || '';
  const options = (annotations || [])
    .filter((entry) => entry.policy !== false)
    .map((entry) => `<option value="${esc(entry.date)}" data-annotation-id="${esc(entry.id)}"
      ${entry.id === selected || (!selected && entry.date === compare.split?.slice(0, 10)) ? 'selected' : ''}>${esc(entry.label)} · ${esc(entry.date)}</option>`)
    .join('');
  const metrics = (compare.metrics || [])
    .map((entry) => `<option value="${esc(entry.id)}" ${entry.id === (metric || compare.metric) ? 'selected' : ''}>${esc(entry.label)}</option>`)
    .join('');
  const censored = Number(compare.censored_before || 0) + Number(compare.censored_after || 0);
  return `<section class="before-after" data-before-after="${esc(compare.outcome)}" data-reason="${esc(compare.reason)}">
    <div class="section-head">
      <h3>Did it work?</h3>
      ${options ? `<label class="control">change <select data-compare-split>${options}</select></label>` : ''}
      ${metrics ? `<label class="control">metric <select data-compare-metric>${metrics}</select></label>` : ''}
    </div>
    <div class="verdict" ${pTooltip(compare)}>
      ${outcomePill(compare.outcome)}
      <p class="verdict-sentence">${esc(verdictSentence(compare))}</p>
    </div>
    <table class="table compact before-after-table">
      <thead><tr><th></th><th class="num">n</th><th class="num">p50</th><th class="num">p85</th></tr></thead>
      <tbody>
        <tr class="before"><th scope="row">before</th>${statCell(compare.before, unit)}</tr>
        <tr class="after"><th scope="row">after</th>${statCell(compare.after, unit)}</tr>
        <tr class="diff"><th scope="row">median change</th>
          <td class="num" colspan="3">${esc(compareValue(compare.diff, unit, { signed: true }))}</td></tr>
      </tbody>
    </table>
    ${ciBar(compare)}
    <p class="before-after-basis">${esc(compare.basis || '')} · ${esc(`${compare.buckets_before} buckets before, ${compare.buckets_after} after`)}.</p>
    ${censored ? `<p class="panel-note censored" data-censored="${censored}">${esc(compare.censored_before)} open item(s) before and ${esc(compare.censored_after)} after are excluded and counted: a running age is not a completed duration.</p>` : ''}
    <p class="panel-note interval-note">${esc(INTERVAL_NOTE)}</p>
    ${compare.annotated_note || compare.annotation?.note ? `<p class="annotation-note">“${esc(compare.annotation?.note || '')}”</p>` : ''}
  </section>`;
}

/** Copy-as-Markdown: verdict, both n's, the CI and the basis — no p-value headline. */
export function beforeAfterMarkdown(compare) {
  if (!compare) return '';
  const unit = compare.unit || 'hours';
  const lines = [
    `## Did it work? ${compare.label || compare.metric}${compare.annotation ? ` — ${compare.annotation.label}` : ''}`,
    '',
    `| | n | p50 | p85 |`,
    `| --- | --- | --- | --- |`,
    `| before | ${compare.before?.n ?? '–'} | ${compareValue(compare.before?.p50, unit)} | ${compareValue(compare.before?.p85, unit)} |`,
    `| after | ${compare.after?.n ?? '–'} | ${compareValue(compare.after?.p50, unit)} | ${compareValue(compare.after?.p85, unit)} |`,
    '',
    `**${OUTCOMES[compare.outcome]?.label || compare.outcome}** — ${verdictSentence(compare)}`,
    compare.ci
      ? `95 % CI on the median difference: ${compareValue(compare.ci[0], unit, { signed: true })} … ${compareValue(compare.ci[1], unit, { signed: true })} (n=${compare.trials || 0} resamples).`
      : 'No interval: the sample cannot support one.',
    `Basis: ${compare.basis || ''}.`,
    compare.p_value != null ? `Mann–Whitney U p=${compare.p_value}.` : '',
    `_${INTERVAL_NOTE}_`,
  ];
  return lines.filter((line) => line !== '').join('\n');
}

/* ------------------------------------------------------------------ stability */

const ARROW = { rising: '↗', falling: '↘', flat: '→' };

export function slopeChip(label, value, unitLabel = '/wk') {
  if (value == null || !isFinite(Number(value))) return '';
  const number = Number(value);
  const sign = number > 0 ? '+' : number < 0 ? '−' : '';
  const abs = Math.abs(number);
  const shown = abs >= 10 ? Math.round(abs) : (Math.round(abs * 10) / 10).toFixed(1);
  return `${label} ${sign}${shown}${unitLabel}`;
}

/**
 * The stability strip: WIP · throughput · arrivals/departures · Little's Law.
 * `overload` is the only flag that refuses a forecast (prd/60 reads it).
 */
export function stabilityStrip(stability) {
  if (!stability) return '';
  if (stability.available === false) {
    return `<div class="stability-strip insufficient" data-stability-flag="insufficient">
      <span class="chip stability">insufficient history</span>
      <span class="stability-sentence">${esc(stability.note || 'Not enough settled buckets for a stability reading.')}</span>
    </div>`;
  }
  const wip = stability.wip || {};
  const throughput = stability.throughput || {};
  const arrivals = stability.arrivals_departures || {};
  const little = stability.little || {};
  const littleShown = little.flag === 'dead_wip'
    || (little.ratio != null && (little.ratio >= 2 || little.ratio <= 0.5));
  return `<div class="stability-strip${stability.flag === 'overload' ? ' overload' : ''}"
      data-stability-flag="${esc(stability.flag)}" data-forecast-refused="${stability.refuses_forecast ? 'true' : 'false'}">
    <span class="chip stability wip ${esc(wip.trend || 'flat')}" ${wip.slope_per_week == null ? '' : `data-slope="${wip.slope_per_week}"`}>WIP ${ARROW[wip.trend] || '→'} ${esc(slopeChip('', wip.slope_per_week).trim())}<span class="muted"> ${esc(wip.from)} → ${esc(wip.to)}</span></span>
    <span class="chip stability throughput ${esc(throughput.trend || 'flat')}" ${throughput.slope_per_week == null ? '' : `data-slope="${throughput.slope_per_week}"`}>throughput ${ARROW[throughput.trend] || '→'} ${esc(throughput.mean == null ? '–' : Number(throughput.mean).toFixed(1))}/wk${throughput.slope_per_week == null ? '' : ` <span class="muted">(${esc(slopeChip('', throughput.slope_per_week).trim())})</span>`}</span>
    ${arrivals.gap_items != null ? `<span class="chip stability arrivals ${arrivals.gap_items > 0 ? 'behind' : 'ahead'}" data-gap="${arrivals.gap_items}">arrivals ${arrivals.gap_items > 0 ? '>' : '≤'} departures (${arrivals.gap_items > 0 ? '+' : ''}${esc(arrivals.gap_items)})</span>` : ''}
    ${littleShown ? `<span class="chip stability little ${esc(little.flag)}" data-little="${esc(little.flag)}">implied ${esc(little.implied_weeks)} wk vs measured ${esc(little.measured_weeks)} wk</span>` : ''}
    <p class="stability-sentence">${esc(stability.note || '')}</p>
    ${littleShown && little.note ? `<p class="stability-sentence little-note">${esc(little.note)}</p>` : ''}
  </div>`;
}

export function stabilityMarkdown(stability) {
  if (!stability || stability.available === false) return '';
  const wip = stability.wip || {};
  const throughput = stability.throughput || {};
  const arrivals = stability.arrivals_departures || {};
  const little = stability.little || {};
  const lines = [
    `Stability: **${stability.flag}** — ${wip.slope_per_week == null ? '–' : `${wip.slope_per_week >= 0 ? '+' : '−'}${Math.abs(wip.slope_per_week).toFixed(2)}`}/wk WIP (${wip.from} → ${wip.to}), throughput ${throughput.mean}/wk (${throughput.slope_per_week >= 0 ? '+' : '−'}${Math.abs(throughput.slope_per_week || 0).toFixed(2)}/wk).`,
  ];
  if (arrivals.sentence) lines.push(arrivals.sentence);
  if (little.implied_weeks != null && little.measured_weeks != null) {
    lines.push(`Little's Law: implied ${little.implied_weeks} wk vs measured ${little.measured_weeks} wk (${little.flag}).`);
  }
  if (stability.note) lines.push(`> ${stability.note}`);
  return lines.join('\n');
}

/* ---------------------------------------------------------------- annotations */

export function annotationCounts(payload) {
  const counts = payload?.annotation_counts || {};
  return {
    policy: Number(counts.policy || 0),
    holiday: Number(counts.holiday || 0),
    vacation: Number(counts.vacation || 0),
    outside: Number(counts.outside || 0) + Number(counts.spans_outside || 0),
  };
}

/** `2 policy changes · 1 holiday in this window`, plus what was counted out. */
export function annotationSummary(payload) {
  const { policy, holiday, vacation, outside } = annotationCounts(payload);
  const parts = [];
  if (policy) parts.push(`${policy} policy change${policy === 1 ? '' : 's'}`);
  if (holiday) parts.push(`${holiday} holiday${holiday === 1 ? '' : 's'}`);
  if (vacation) parts.push(`${vacation} vacation${vacation === 1 ? '' : 's'}`);
  const inside = parts.length
    ? `${parts.join(' · ')} in this window`
    : 'No policy changes or calendar spans in this window';
  const out = outside ? ` · ${outside} outside the window, counted not drawn` : '';
  return `<p class="annotation-summary" data-annotations="${policy}">
    <span>${esc(inside + out)}</span>
    ${payload?.annotations?.length ? '<span class="muted"> · a marker is a dated change, not proof it caused anything</span>' : ''}
  </p>`;
}

/** The markers as readable rows: label, date, and the team's own note. */
export function annotationList(payload) {
  const entries = (payload?.annotations || []).filter((entry) => entry.in_window !== false);
  if (!entries.length) return '';
  const rows = entries.map((entry) => `<li class="annotation-item" data-annotation-id="${esc(entry.id)}" data-date="${esc(entry.date)}">
      <span class="annotation-label">${esc(entry.label)}</span>
      <span class="annotation-date mono">${esc(entry.date)}</span>
      ${entry.note ? `<span class="annotation-note">“${esc(entry.note)}”</span>` : ''}
      <button type="button" class="chip ghost" data-annotation-delete="${esc(entry.id)}" aria-label="delete ${esc(entry.label)}">delete</button>
    </li>`).join('');
  return `<ul class="annotation-list">${rows}</ul>`;
}

/** The closed-vocabulary "Record a change" affordance (no modal). */
export function annotationRecordMarkup(catalogue, { date = '' } = {}) {
  const kinds = (catalogue || []).map((entry) => `<option value="${esc(entry.id)}" data-unit="${esc(entry.unit || '')}" data-numeric="${entry.numeric ? 'true' : 'false'}">${esc(entry.label)}</option>`).join('');
  return `<details class="annotation-record" data-annotation-record>
    <summary class="chip">Record a change</summary>
    <form class="annotation-form" data-annotation-form>
      <label class="control">change <select name="kind" required>${kinds}</select></label>
      <label class="control">value <input name="value" type="number" step="any" inputmode="decimal" placeholder="4"></label>
      <label class="control">date <input name="date" type="date" value="${esc(date)}" required></label>
      <label class="control grow">note (optional) <input name="note" maxlength="2000" placeholder="adopted after the retro"></label>
      <button type="submit" class="chip">Record the change</button>
      <p class="panel-note">The note is your team's own words, stored with the annotation — never scraped from Bitbucket or Jira.</p>
    </form>
  </details>`;
}

/** The empty state: a sentence, not a blank panel. */
export function annotationEmptyMarkup(catalogue, { date = '' } = {}) {
  return `<div class="annotation-empty" data-annotation-empty>
    <p>No policy changes recorded; a change you did not write down is indistinguishable from noise.</p>
    ${annotationRecordMarkup(catalogue, { date })}
  </div>`;
}

/** Every marker in Markdown (Copy as Markdown includes the changes). */
export function annotationMarkdown(payload) {
  const entries = payload?.annotations || [];
  if (!entries.length) return '';
  return ['', '## Annotations', '',
    ...entries.map((entry) => `- ${entry.date} — ${entry.label}${entry.in_window === false ? ' (outside the window)' : ''}${entry.note ? ` — “${entry.note}”` : ''}`),
  ].join('\n');
}

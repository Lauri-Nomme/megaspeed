/* Probabilistic throughput forecast (prd/60): how many, by when.
 *
 * One card, two entry points (the top of the sprint lens and prd/52's
 * "Will it land?" button). Everything is a pure markup builder so `node --test`
 * pins the whole answer without a browser:
 *
 *   - the **interval** is the headline (`22–30 of 30 · by 27 Dec`), never a
 *     single number;
 *   - the bar encodes P15/P50/P85 with a hairline at the target; it is neutral
 *     accent (an encoding), turning `--warning` only when `p85 < target`, and
 *     never red — a forecast is information, not an incident;
 *   - a refused forecast renders the exact sentence and **no numbers** (never a
 *     zero dressed up as an interval);
 *   - a target change re-renders from the payload's pre-computed `uncapped`
 *     quantiles and `p_reach_curve` — the client never interpolates and never
 *     re-simulates;
 *   - the phone layout is three lines + a `2 at risk →` disclosure and a 32 px
 *     bar with only the three numbers: no axis, no chart, no horizontal scroll.
 */

import { esc, fmtDay, fmtHours } from './format.js';

/** `22–30` (en dash) — the interval, never a point estimate. */
export function intervalLabel(view) {
  if (!view || view.p15 == null || view.p85 == null) return '';
  return `${view.p15}–${view.p85}`;
}

/**
 * The forecast re-read for one target, from the **payload's** pre-computed
 * quantities.
 *
 * Capping is monotone, so `Q_p(min(total, n)) = min(Q_p(total), n)` exactly:
 * the quantiles below are the simulation's own numbers, re-capped (never
 * interpolated). `p_reach` comes from the exact `p_reach_curve` entry for `n`.
 */
export function forecastTargetView(forecast, target) {
  if (!forecast) return null;
  const uncapped = forecast.uncapped || forecast.how_many || {};
  const n = Math.max(0, Math.round(Number(target)));
  const cap = (key) => {
    const value = Number(uncapped[key]);
    return Number.isFinite(value) ? Math.min(value, n) : null;
  };
  const curve = forecast.p_reach_curve || [];
  const entry = curve.find((row) => Number(row.n) === n);
  return {
    n,
    p15: cap('p15'),
    p50: cap('p50'),
    p85: cap('p85'),
    p_reach: entry ? Number(entry.p) : null,
  };
}

/** `27 Dec` from an ISO date, or '' (a missing date is stated, never fabricated). */
function dayLabel(iso) {
  if (!iso || String(iso).length < 10) return '';
  const parsed = Date.parse(`${String(iso).slice(0, 10)}T12:00:00Z`);
  return Number.isFinite(parsed) ? fmtDay(parsed) : '';
}

/** One prd/52 risk row: `#60 stuck on FrMo · 18 d` (name, holder, wait). */
export function forecastRiskLine(row) {
  if (!row) return '';
  const name = row.pr || row.key || '—';
  const holder = (row.holder || '').trim();
  let what = row.label || row.kind || 'at risk';
  if (row.kind === 'stuck-on') {
    what = holder
      ? `stuck on ${holder}`
      : 'stuck on a person';
  } else if (holder) {
    what = `${what} · ${holder}`;
  }
  const wait = row.hours != null ? ` · ${fmtHours(row.hours)}` : '';
  return `${name} ${what}${wait}`.trim();
}

function riskRows(rows, { limit = 3 } = {}) {
  return (rows || []).slice(0, limit).map((row) => `<li
      title="${esc(`${row.key || ''} · ${row.kind || ''}`)}">${esc(forecastRiskLine(row))}</li>`).join('');
}

/**
 * The interval bar: P15–P85 span, a 2 px P50 tick, a 1 px hairline at the
 * target. The scale includes the target so the hairline is always visible.
 */
export function forecastBar(view, target, { phone = false } = {}) {
  if (!view) return '';
  const low = Number(view.p15);
  const high = Math.max(Number(view.p85), Number(target));
  const scaleLow = Math.min(low, Number(target));
  const span = Math.max(1, high - scaleLow);
  const point = high === scaleLow;
  const pos = (value) => (point ? 100 : ((Number(value) - scaleLow) / span) * 100);
  const width = point ? 100 : Math.max(0, ((low - scaleLow) / span) * 100);
  const p50 = pos(view.p50);
  const targetPos = pos(target);
  const label = `interval ${view.p15} to ${view.p85}, P50 ${view.p50}, target ${target}`;
  return `<div class="forecast-bar${phone ? ' phone' : ''}" data-forecast-bar
      data-p50-pos="${p50.toFixed(2)}" data-target-pos="${targetPos.toFixed(2)}"
      role="img" aria-label="${esc(label)}">
    <span class="forecast-span" style="left:${point ? 0 : width}%;width:${point ? 100 : Math.max(0, ((high - low) / span) * 100)}%"></span>
    <span class="forecast-p50" style="left:${p50.toFixed(2)}%"></span>
    <span class="forecast-target-hair" style="left:${targetPos.toFixed(2)}%"></span>
  </div>
  <div class="forecast-ends${phone ? ' phone' : ''}">
    <span style="left:${pos(view.p15).toFixed(2)}%">${view.p15}</span>
    <span style="left:${p50.toFixed(2)}%">${view.p50}</span>
    <span style="left:${pos(view.p85).toFixed(2)}%">${view.p85}</span>
  </div>`;
}

function paramsRow(payload, target) {
  const request = payload.request || {};
  const curve = (payload.forecast && payload.forecast.p_reach_curve) || [];
  const max = curve.length ? Math.max(...curve.map((row) => Number(row.n))) : target;
  return `<div class="forecast-params">
    <label class="control">Target
      <input type="number" inputmode="numeric" min="0" max="${esc(max)}"
        value="${esc(target)}" data-forecast-target-input
        aria-label="forecast target"></label>
    <span class="forecast-dot">·</span>
    <label class="control">By
      <input type="date" value="${esc(String(request.by || '').slice(0, 10))}"
        data-forecast-by-input aria-label="forecast deadline"></label>
    <span class="forecast-dot">·</span>
    <span class="control">Confidence <b>P15–P85</b>
      <span class="muted" title="a date is a promise (P85); how many is read at P15 for the same reason">85 % edge</span></span>
  </div>`;
}

function refusalCard(payload) {
  const reasons = (payload.assess && payload.assess.reasons) || [];
  const reason = reasons[0] || {};
  const handoff = reason.code === 'overload'
    ? `<button type="button" class="chip" data-forecast-handoff="prd61">See the stability strip (prd/61)</button>`
    : '';
  return `<section class="panel wide forecast-card refused" id="forecast-card"
      data-forecast-state="refused" data-forecast-refusal="${esc(reason.code || '')}"
      data-forecast-numbers="none">
    <div class="forecast-overline">FORECAST</div>
    <div class="forecast-desktop">
      <p class="forecast-refusal">${esc(reason.sentence || payload.note || 'This window cannot support a forecast.')}</p>
      ${reasons.slice(1).map((row) => `<p class="forecast-refusal minor">${esc(row.sentence)}</p>`).join('')}
      ${payload.backtest ? `<p class="forecast-scope-note">backtest: the sample stops at the sprint's
        start, so the model cannot see the outcome it is explaining</p>` : ''}
      ${handoff ? `<div class="forecast-handoff">${handoff}</div>` : ''}
    </div>
    <div class="forecast-phone">
      <p class="forecast-refusal">${esc(reason.sentence || 'This window cannot support a forecast.')}</p>
      ${handoff}
    </div>
  </section>`;
}

function delegateCard(payload) {
  const delegate = payload.delegate || {};
  const rows = delegate.rows || [];
  return `<section class="panel wide forecast-card delegate" id="forecast-card"
      data-forecast-state="delegate" data-forecast-numbers="none">
    <div class="forecast-overline">FORECAST</div>
    <div class="forecast-desktop">
      <p class="forecast-refusal">Not forecastable: ${esc(delegate.reason || 'a small set is a risk question')}.</p>
      <p class="panel-note">A small set is a list of names, not a distribution — the prd/52
        at-risk rows say who owes each item and for how long.</p>
      ${rows.length ? `<ul class="forecast-risk-rows">${riskRows(rows)}</ul>` : ''}
      <div class="forecast-handoff">
        <button type="button" class="chip" data-forecast-handoff="prd52">See the at-risk list (prd/52/59)</button>
      </div>
    </div>
    <div class="forecast-phone">
      <p class="forecast-refusal">${esc(delegate.reason || 'a small set is a risk question')}.</p>
      <button type="button" class="forecast-phone-risk" data-forecast-handoff="prd52">prd/52 →</button>
    </div>
  </section>`;
}

/**
 * The forecast card. `target` defaults to the payload's requested `n`; a
 * changed target re-reads the pre-computed quantiles (see
 * :func:`forecastTargetView`) and never calls the model again.
 */
export function forecastCard(payload, { target = null, base = '' } = {}) {
  if (!payload) {
    return `<section class="panel wide forecast-card loading" id="forecast-card"
        data-forecast-state="loading">
      <div class="forecast-overline">FORECAST</div>
      <p class="panel-note">Reading the throughput history…</p>
    </section>`;
  }
  if (payload.available === false) return refusalCard(payload);
  if (payload.delegate) return delegateCard(payload);
  const forecast = payload.forecast;
  if (!forecast) return refusalCard(payload);
  const request = payload.request || {};
  const n = Number(request.n);
  const view = forecastTargetView(forecast, target == null ? n : target);
  const targetN = view.n;
  const warn = view.p85 != null && view.p85 < targetN;
  const day = dayLabel(request.by);
  const pct = view.p_reach == null ? null : Math.round(view.p_reach * 100);
  const adjust = payload.adjust || {};
  const risk = adjust.at_risk || [];
  const riskCount = adjust.at_risk_count || risk.length;
  const deliveredNow = Number(adjust.delivered_now || 0);
  const remaining = adjust.delivered_now == null
    ? (adjust.remaining == null ? null : Number(adjust.remaining))
    : Math.max(0, targetN - deliveredNow);
  const holdCeiling = Math.max(0, targetN - Number(adjust.doubtful || 0));
  const headline = `${intervalLabel(view)} of ${targetN}${day ? ` · by ${day}` : ''}`;
  const when = forecast.when || {};
  const p85Day = dayLabel(when.p85_date);
  const whenBit = p85Day && p85Day !== day
    ? ` · 85 % by ${p85Day}` : '';
  const unfinished = Number(when.unfinished_share || 0);
  const unfinishedBit = unfinished > 0
    ? ` · ${Math.round(unfinished * 100)} % of runs do not land inside ${when.horizon_weeks || 26} weeks`
    : '';
  const subline = `P50 ${view.p50}${pct == null ? '' : ` · ${pct} % chance all ${targetN}`}` +
    `${whenBit} · ${Number(forecast.trials || 10000).toLocaleString('en-US')} resamples` +
    ` · ${payload.throughput ? payload.throughput.n : 0} weeks${unfinishedBit}`;
  const scopeAdded = Number(payload.scope_added_after_start || 0);
  const scopeNote = scopeAdded
    ? `<p class="forecast-scope-note">this forecasts the scope as of today;
        ${scopeAdded} stor${scopeAdded === 1 ? 'y was' : 'ies were'} added after
        the sprint started (adopted scope); scope added later is not in the interval</p>`
    : '';
  const backtestNote = payload.backtest
    ? `<p class="forecast-scope-note">backtest: the sample stops at the sprint's
        start, so the model cannot see the outcome it is explaining</p>`
    : '';
  const warnClass = warn ? ' warn' : '';
  const riskLine = riskCount
    ? `<p class="forecast-blind-head">What the model can't see
        <span class="muted">${riskCount} at risk${remaining == null ? '' : ` · ${remaining} left`}</span></p>
       <ul class="forecast-risk-rows">${riskRows(risk)}</ul>
       <p class="forecast-blind-count">delivered if the blocked items hold:
         <b>${holdCeiling}</b></p>`
    : `<p class="forecast-blind-head">What the model can't see
        <span class="muted">nothing at risk</span></p>`;
  return `<section class="panel wide forecast-card${warnClass}" id="forecast-card"
      data-forecast-state="ok" data-forecast-target="${esc(targetN)}"
      data-forecast-p85="${esc(view.p85)}" data-forecast-warn="${warn ? 'true' : 'false'}"
      data-forecast-seed="${esc(forecast.seed)}" data-forecast-numbers="interval">
    <div class="forecast-overline">FORECAST</div>
    <div class="forecast-desktop">
      <h2 class="forecast-headline">${esc(headline)}</h2>
      <p class="forecast-subline">${esc(subline)}</p>
      ${scopeNote}
      ${backtestNote}
      ${forecastBar(view, targetN)}
      <div class="forecast-blind">${riskLine}</div>
      <p class="forecast-method">${esc(payload.method || '')}<span
        class="forecast-seed mono"> · seed ${esc(forecast.seed)}</span></p>
      <p class="forecast-caveat">${esc(payload.caveat || '')}</p>
      ${paramsRow(payload, targetN)}
    </div>
    <div class="forecast-phone">
      <h2 class="forecast-headline">${esc(`${intervalLabel(view)} of ${targetN}`)}</h2>
      <p class="forecast-subline">${esc(`${day ? `by ${day}` : 'no deadline'} · P50 ${view.p50}`)}</p>
      <button type="button" class="forecast-phone-risk" data-forecast-risk-toggle
        aria-expanded="false">${riskCount} at risk →</button>
      ${forecastBar(view, targetN, { phone: true })}
      <ul class="forecast-risk-rows" data-forecast-phone-rows hidden>${riskRows(risk)}</ul>
    </div>
  </section>`;
}

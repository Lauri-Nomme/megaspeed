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
 *   - coinciding quantiles print **once** (`30/30/30` is one `30` at the right
 *     edge, not three overlapping spans that render `3030`);
 *   - `Copy as Markdown` formats the payload the card rendered — no second
 *     fetch, no re-simulation — and keeps `?tenant=` in every link;
 *   - a target change re-renders from the payload's pre-computed `uncapped`
 *     quantiles and `p_reach_curve` — the client never interpolates and never
 *     re-simulates;
 *   - the phone layout is three lines + a `2 at risk →` disclosure and a 32 px
 *     bar with only the three numbers: no axis, no chart, no horizontal scroll.
 */

import { esc, fmtDay, fmtHours } from './format.js';
import { ticketHref } from './tickets.js';

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

/** The class, holder and wait of one prd/52 risk row — `stuck on FrMo · 18 d`. */
function riskBit(row) {
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
  return `${what}${wait}`.trim();
}

/** One prd/52 risk row: `#60 stuck on FrMo · 18 d` (name, holder, wait). */
export function forecastRiskLine(row) {
  if (!row) return '';
  const name = row.pr || row.key || '—';
  return `${name} ${riskBit(row)}`.trim();
}

function riskRows(rows, { limit = 3 } = {}) {
  return (rows || []).slice(0, limit).map((row) => `<li
      title="${esc(`${row.key || ''} · ${row.kind || ''}`)}">${esc(forecastRiskLine(row))}</li>`).join('');
}

/* Two end labels closer than this (in % of the bar's width) would overlap at
 * any realistic bar width, so they are the same label. A capped target puts
 * P15 = P50 = P85 = 30 at `left: 100 %`; three spans there render `3030`. */
const LABEL_GAP = 2;

/**
 * The labels under the bar, one span per distinct position. Quantiles that
 * coincide (the capped `30/30/30`, or any two within `LABEL_GAP`) collapse
 * into one label at the rightmost of their positions — the value is printed
 * once (`30`), never three times on top of itself.
 */
function endLabels(ends) {
  const groups = [];
  for (const end of ends) {
    const last = groups[groups.length - 1];
    if (last && Math.abs(end.pos - last.pos) <= LABEL_GAP) {
      if (!last.values.some((value) => String(value) === String(end.value))) {
        last.values.push(end.value);
      }
      last.pos = Math.max(last.pos, end.pos);
      continue;
    }
    groups.push({ pos: end.pos, values: [end.value] });
  }
  return groups.map((group) => `<span style="left:${group.pos.toFixed(2)}%">${
    esc(group.values.join('/'))}</span>`).join('\n    ');
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
  const ends = endLabels([
    { value: view.p15, pos: pos(view.p15) },
    { value: view.p50, pos: p50 },
    { value: view.p85, pos: pos(view.p85) },
  ]);
  return `<div class="forecast-bar${phone ? ' phone' : ''}" data-forecast-bar
      data-p50-pos="${p50.toFixed(2)}" data-target-pos="${targetPos.toFixed(2)}"
      role="img" aria-label="${esc(label)}">
    <span class="forecast-span" style="left:${point ? 0 : width}%;width:${point ? 100 : Math.max(0, ((high - low) / span) * 100)}%"></span>
    <span class="forecast-p50" style="left:${p50.toFixed(2)}%"></span>
    <span class="forecast-target-hair" style="left:${targetPos.toFixed(2)}%"></span>
  </div>
  <div class="forecast-ends${phone ? ' phone' : ''}">
    ${ends}
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

/* The export copies what the card rendered: the payload is registered here at
 * render time, so a click never fetches `/api/forecast` again and never runs a
 * client-side simulation. `main.js` owns the page's click router; keeping the
 * registry in this module means the chip works from the card alone. */
const FORECAST_COPY = new Map();
let forecastCopySeq = 0;

function registerForecastCopy(payload, { target = null, base = '' } = {}) {
  if (FORECAST_COPY.size >= 8) FORECAST_COPY.clear();
  const id = `fc${++forecastCopySeq}`;
  FORECAST_COPY.set(id, { payload, target, base });
  return id;
}

/** The card's head: the overline and the export chip (same chip as the other
 *  analyst surfaces — `class="chip"`, a title, no icon-only affordance). */
function forecastHead(copyId) {
  return `<div class="forecast-overline" style="display:flex;align-items:center;gap:8px">FORECAST
    <span class="grow"></span>
    <button type="button" class="chip" data-forecast-copy="${esc(copyId)}"
      style="text-transform:none;letter-spacing:0"
      title="copy the interval, the by-date, p(reach), the at-risk ceiling and the method as Markdown">Copy as Markdown</button>
  </div>`;
}

function refusalCard(payload, { base = '' } = {}) {
  const reasons = (payload.assess && payload.assess.reasons) || [];
  const reason = reasons[0] || {};
  const handoff = reason.code === 'overload'
    ? `<button type="button" class="chip" data-forecast-handoff="prd61">See the stability strip (prd/61)</button>`
    : '';
  const copyId = registerForecastCopy(payload, { base });
  return `<section class="panel wide forecast-card refused" id="forecast-card"
      data-forecast-state="refused" data-forecast-refusal="${esc(reason.code || '')}"
      data-forecast-numbers="none">
    ${forecastHead(copyId)}
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

function delegateCard(payload, { base = '' } = {}) {
  const delegate = payload.delegate || {};
  const rows = delegate.rows || [];
  const copyId = registerForecastCopy(payload, { base });
  return `<section class="panel wide forecast-card delegate" id="forecast-card"
      data-forecast-state="delegate" data-forecast-numbers="none">
    ${forecastHead(copyId)}
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
  if (payload.available === false) return refusalCard(payload, { base });
  if (payload.delegate) return delegateCard(payload, { base });
  const forecast = payload.forecast;
  if (!forecast) return refusalCard(payload, { base });
  const request = payload.request || {};
  const n = Number(request.n);
  const view = forecastTargetView(forecast, target == null ? n : target);
  const targetN = view.n;
  const copyId = registerForecastCopy(payload, { target: targetN, base });
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
    ${forecastHead(copyId)}
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

/* ------------------------------------------------------------------ export */

/** The tenant named in `base` (`http://host/?tenant=del-combined`). */
function tenantIn(base) {
  const match = /[?&]tenant=([^&#]+)/.exec(String(base || ''));
  if (!match) return '';
  try { return decodeURIComponent(match[1]); } catch { return match[1]; }
}

function decimal(value, digits) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : null;
}

/** `- [DEL-176](…#ticket/DEL-176) #60 stuck on FrMo · 18 d` — the tenant travels. */
function riskMarkdown(row, base) {
  if (!row) return '';
  const ticket = row.key
    ? (base ? `[${row.key}](${ticketHref(row.key, base)})` : row.key)
    : '';
  const head = [ticket, row.pr].filter(Boolean).join(' ');
  return `- ${head ? `${head} ` : ''}${riskBit(row)}`.trimEnd();
}

/**
 * The card as Markdown — the same numbers the card rendered, read from the
 * payload it was given. It never calls `/api/forecast` and never resamples:
 * the interval, the by-date, P50, p(reach), the sample's n/mean/sd/CV, the
 * at-risk ceiling and any warning or refusal are already in the payload.
 * `?tenant=` rides in every link so the export cannot open another dataset.
 */
export function forecastMarkdown(payload, { target = null, base = '' } = {}) {
  if (!payload) return '';
  const request = payload.request || {};
  const reasons = (payload.assess && payload.assess.reasons) || [];
  const tenant = tenantIn(base);
  const provenance = tenant ? `_forecast for [${tenant}](${base})_` : '';

  // A refusal states why and prints no forecast number (never a zero dressed
  // up as an interval). The sentence is the card's own, verbatim.
  if (payload.available === false || (!payload.forecast && !payload.delegate)) {
    const sentence = (reasons[0] && reasons[0].sentence) || payload.note
      || 'This window cannot support a forecast.';
    const lines = ['# Forecast — refused', '', sentence];
    for (const row of reasons.slice(1)) {
      if (row.sentence) lines.push('', row.sentence);
    }
    lines.push('', '_No interval: this window cannot support a forecast, so the reason is '
      + 'stated and no number is invented._');
    return lines.join('\n');
  }

  if (payload.delegate) {
    const delegate = payload.delegate;
    const rows = delegate.rows || [];
    const lines = [
      '# Forecast — not forecastable',
      '',
      `Not forecastable: ${delegate.reason || 'a small set is a risk question'}.`,
      '',
      'A small set is a list of names, not a distribution — the at-risk rows say '
        + 'who owes each item and for how long.',
    ];
    if (rows.length) {
      lines.push('');
      for (const row of rows) lines.push(riskMarkdown(row, base));
    }
    if (provenance) lines.push('', provenance);
    return lines.join('\n');
  }

  const forecast = payload.forecast;
  const n = Number(request.n);
  const view = forecastTargetView(forecast, target == null ? n : target);
  const targetN = view.n;
  const day = dayLabel(request.by);
  const isoBy = request.by ? String(request.by).slice(0, 10) : '';
  const pct = view.p_reach == null ? null : Math.round(view.p_reach * 100);
  const warn = view.p85 != null && view.p85 < targetN;
  const when = forecast.when || {};
  const adjust = payload.adjust || {};
  const risk = adjust.at_risk || [];
  const riskCount = adjust.at_risk_count || risk.length;
  const deliveredNow = Number(adjust.delivered_now || 0);
  const remaining = adjust.delivered_now == null
    ? (adjust.remaining == null ? null : Number(adjust.remaining))
    : Math.max(0, targetN - deliveredNow);
  const holdCeiling = Math.max(0, targetN - Number(adjust.doubtful || 0));

  const headline = `${intervalLabel(view)} of ${targetN}${day ? ` · by ${day}` : ''}`;
  const lines = [`# Forecast — ${headline}`, ''];
  lines.push(`- interval: **${intervalLabel(view)}** of ${targetN}`);
  lines.push(`- by: ${day ? `${day}${isoBy ? ` (${isoBy})` : ''}` : (isoBy || 'no deadline')}`);
  lines.push(`- P50: ${view.p50}`);
  lines.push(pct == null
    ? '- p(reach): — (the target is outside the sampled range)'
    : `- p(reach): ${pct} % chance all ${targetN}`);

  const throughput = payload.throughput || {};
  const stats = [`n = ${Number(throughput.n) || 0} weeks`];
  const mean = decimal(throughput.mean, 1);
  const sd = decimal(throughput.sd, 1);
  const cv = decimal(throughput.cv, 2);
  if (mean != null) stats.push(`mean ${mean}`);
  if (sd != null) stats.push(`sd ${sd}`);
  if (cv != null) stats.push(`CV ${cv}`);
  lines.push(`- throughput (${throughput.basis || 'stories delivered / week'}): ${stats.join(' · ')}`);

  const p85Day = dayLabel(when.p85_date);
  if (p85Day && p85Day !== day) {
    lines.push(`- P85 date: 85 % by ${p85Day}${
      when.p85_date ? ` (${String(when.p85_date).slice(0, 10)})` : ''}`);
  }
  const unfinished = Number(when.unfinished_share || 0);
  if (unfinished > 0) {
    lines.push(`- horizon: ${Math.round(unfinished * 100)} % of runs do not land inside ${
      when.horizon_weeks || 26} weeks`);
  }

  if (warn) {
    lines.push('', `**Warning:** even the optimistic edge (P85 = ${view.p85}) misses the `
      + `target of ${targetN} — a good run does not close it.`);
  }

  const scopeAdded = Number(payload.scope_added_after_start || 0);
  if (scopeAdded) {
    lines.push('', `_This forecasts the scope as of today; ${scopeAdded} stor${
      scopeAdded === 1 ? 'y was' : 'ies were'} added after the sprint started `
      + '(adopted scope) — scope added later is not in the interval._');
  }
  if (payload.backtest) {
    lines.push('', "_Backtest: the sample stops at the sprint's start, so the model cannot "
      + 'see the outcome it is explaining._');
  }

  lines.push('', "## What the model can't see", '');
  if (riskCount) {
    lines.push(`${riskCount} at risk${remaining == null ? '' : ` · ${remaining} left`}`, '');
    for (const row of risk.slice(0, 3)) lines.push(riskMarkdown(row, base));
    if (risk.length > 3) lines.push(`- and ${risk.length - 3} more at risk`);
    lines.push('', `delivered if the blocked items hold: **${holdCeiling}**`);
  } else {
    lines.push('nothing at risk');
  }

  if (payload.method) lines.push('', payload.method);
  if (payload.caveat) lines.push('', payload.caveat);
  lines.push('', `_A forecast, not a measurement — ${
    Number(forecast.trials || 10000).toLocaleString('en-US')} resamples, seed ${forecast.seed}._`);
  if (provenance) lines.push('', provenance);
  return lines.join('\n');
}

/* The chip's click handler. It lives here (not in `main.js`) so the card is
 * self-contained: the payload registry above is the rendered payload, and the
 * fallback textarea keeps copy working on a plain-HTTP origin where
 * `navigator.clipboard` is unavailable. */
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('click', (event) => {
    const chip = event.target && event.target.closest
      ? event.target.closest('[data-forecast-copy]') : null;
    if (!chip) return;
    const entry = FORECAST_COPY.get(chip.dataset.forecastCopy);
    if (!entry) return;
    event.preventDefault();
    const markdown = forecastMarkdown(entry.payload, entry);
    if (!markdown) return;
    const done = (ok) => {
      const label = chip.textContent;
      chip.textContent = ok ? 'copied' : 'copy failed';
      setTimeout(() => { chip.textContent = label; }, 1400);
    };
    const write = (typeof navigator !== 'undefined' && navigator.clipboard
      && navigator.clipboard.writeText)
      ? navigator.clipboard.writeText(markdown).then(() => true)
      : Promise.resolve(false);
    write.then((ok) => {
      if (ok) { done(true); return; }
      try {
        const area = document.createElement('textarea');
        area.value = markdown;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        const copied = document.execCommand('copy');
        area.remove();
        done(copied);
      } catch { done(false); }
    }).catch(() => done(false));
  });
}

/* First-pass yield and stability (prd/62) — the cost side of every lever.
 *
 * The Rules page recommends speed levers; these two panels show what the
 * process pays today. `/api/insights.metrics.yield` is per-step %C&A and VSM's
 * **rolled %C&A** — the product of the four steps, with the joint clean count
 * next to it because a story can loop at more than one step. `.stability` is
 * the same deliveries' loop-back rates: three direct rates plus a *labelled*
 * proxy for rework; the bug share is a dash with its reason when the store
 * carries no issue type.
 *
 * Pure markup, like the rest of the app: no fetch, no DOM. `main.js` owns the
 * request so every string here is testable with `node --test`.
 */

import { esc } from './format.js';
import { ticketHref } from './tickets.js';

/** The four steps, in the lifecycle order the waterfall renders. */
export const YIELD_STEPS = [
  { id: 'refine', label: 'Refined' },
  { id: 'develop', label: 'Developed' },
  { id: 'review', label: 'Reviewed' },
  { id: 'verify', label: 'Verified' },
];

/** The stability tiles, in the order prd/62 specifies. */
export const STABILITY_TILES = [
  { key: 'reopen', label: 'Reopened' },
  { key: 'failed_verification', label: 'Failed verification' },
  { key: 'review_bounce', label: 'Review bounced' },
  { key: 'bug_share', label: 'Bug share' },
];

/**
 * A share as a rate: "5.7 %", "4.6 %", "24 %", "10 %", "8.6 %".
 *
 * Two significant digits: a rate below 10 % keeps one decimal (5.7 %), above it
 * the whole percent is the honest precision (24.1 → 24 %). The API rounds each
 * rate to three decimals, so this is display only.
 */
export function fmtShare(rate) {
  if (rate == null || !Number.isFinite(Number(rate))) return '–';
  const pct = Number(rate) * 100;
  return `${Math.abs(pct) >= 10 ? Math.round(pct) : Number(pct.toFixed(1))} %`;
}

function stepRate(step) {
  if (!step || step.rate == null) return '–';
  return fmtShare(step.rate);
}

function drillLinks(step, base) {
  const items = (step && step.items) || [];
  if (!items.length) return '';
  return `<div class="yield-drill-list">${
    items.map((key) => `<a class="drill-link mono" href="${esc(ticketHref(key, base))}"
      data-yield-item="${esc(key)}">${esc(key)}</a>`).join('')
  }</div>`;
}

/** One waterfall column: label, rate, n / N, a 4 px bar and its drill-down. */
function stepColumn(step, n, implied, base) {
  const rate = step.rate == null ? null : Number(step.rate);
  const width = rate == null ? 0 : Math.max(0, Math.min(100, rate * 100));
  const threshold = implied && implied.steps ? implied.steps[step.id] : null;
  // a step below the lab-implied yield is *marked*, never recoloured
  const below = rate != null && threshold != null && rate < Number(threshold);
  const dot = below
    ? `<span class="yield-warning" data-yield-below="${esc(threshold)}"
         title="below the ${esc(fmtShare(threshold))} the policy lab's fitted rework implies"></span>
         <span class="sr-only">below the lab-implied yield of ${esc(fmtShare(threshold))}</span>`
    : '';
  const clean = step.clean_n == null ? '–' : step.clean_n;
  const cohort = step.n ?? n ?? 0;
  const tooltip = step.available
    ? `${step.label}: ${clean} of ${cohort} clean — signals: ${(step.signals || []).join(', ')}${
      below ? ` · below the lab-implied ${fmtShare(threshold)}` : ''}`
    : `${step.label}: unfalsifiable — ${step.note || 'the transitions are not in this store'}`;
  const note = step.note
    ? `<p class="yield-step-note">${esc(step.note)}</p>` : '';
  const items = step.available ? drillLinks(step, base) : '';
  return `<li class="yield-step${step.available ? '' : ' unavailable'}"
      data-yield-step="${esc(step.id)}" data-rate="${rate == null ? '' : rate}">
    <details class="yield-drill">
      <summary title="${esc(tooltip)}">
        <span class="yield-label">${esc(step.label)}</span>
        <b class="yield-rate">${stepRate(step)}</b>
        <span class="yield-n mono">${esc(clean)} / ${esc(cohort)}</span>
        <span class="yield-bar" aria-hidden="true"><i style="width:${width}%"></i></span>
        ${dot}
      </summary>
      ${note}${items}
    </details>
  </li>`;
}

/** Why the rolled product has no number because a step is unfalsifiable.
 *
 * prd/68 F-07: on a Jira-only store `rolled` is `null` even with 84 deliveries
 * because Reviewed has no merged PR to falsify it. That is not "too few for a
 * rate" and not "0 of 84" — the product is unknown, and the reason is a step
 * the store does not measure.
 */
export function rolledUnavailableReason(payload) {
  if (!payload || payload.rolled != null) return '';
  const steps = payload.steps || [];
  const missing = YIELD_STEPS
    .map(({ id }) => steps.find((step) => step.id === id))
    .filter((step) => step && step.available === false);
  if (!missing.length) return '';
  return `Rolled %C&A is unavailable because ${missing
    .map((step) => step.label).join(' and ')} ${missing.length === 1 ? 'is' : 'are'} ` +
    'not measured in this store';
}

/** The rolled sentence for an export: measured, unavailable, or too few. */
function rolledSentence(payload) {
  if (payload.rolled != null) {
    return [`Only ${payload.clean_joint} of ${payload.n} stories went through without a ` +
      `loop-back (joint clean ${payload.clean_joint}; the product is ${fmtShare(payload.rolled)}).`];
  }
  const reason = rolledUnavailableReason(payload);
  if (reason) return [`${reason}.`];
  return [`${payload.n} deliveries — too few for a rate (minimum ${payload.min_deliveries || 10}).`];
}

/**
 * The `First-pass yield` panel: `REFINED 100 % · … → ROLLED 8.6 %`.
 *
 * The rolled figure is neutral (a number, not a state); the steps carry the
 * category colour (pipeline stage). The joint clean count is printed next to
 * the product, with the one-sentence honesty line the API supplies.
 */
export function yieldPanel(metrics, { base = '' } = {}) {
  const payload = metrics && metrics.yield;
  if (!payload) return '';
  if (!payload.available) {
    return `<section class="panel wide yield-panel" id="first-pass-yield">
      <div class="panel-head"><h2>First-pass yield</h2>
        <span class="pill neutral">unavailable</span></div>
      <p class="empty-row">${esc(payload.note || 'no pipeline in this store to measure first-pass yield over')}</p>
    </section>`;
  }
  const steps = payload.steps || [];
  const byId = new Map(steps.map((step) => [step.id, step]));
  const ordered = YIELD_STEPS.map((step) => byId.get(step.id)).filter(Boolean);
  const rolled = payload.rolled == null ? '–' : fmtShare(payload.rolled);
  const n = payload.n;
  const minDeliveries = Number(payload.min_deliveries || 10);
  // prd/68 F-07: `rolled == null` has two very different causes. A genuinely
  // small cohort is "too few for a rate"; a step the store cannot falsify
  // (Reviewed on a Jira-only store) makes the product unavailable, not zero.
  const rolledReason = rolledUnavailableReason(payload);
  const unmeasured = rolledReason !== '';
  const tooFew = payload.rolled == null && !unmeasured && Number(n) < minDeliveries;
  // the joint clean count is a measurement of all four steps; when one is not
  // measured the count is not 0, it is unknown.
  const joint = payload.clean_joint == null || unmeasured
    ? '–'
    : payload.clean_joint;
  const hidden = tooFew
    ? `<p class="yield-step-note" data-yield-too-few>${esc(
      `${n} deliveries — too few for a rate (minimum ${minDeliveries})`,
    )}</p>`
    : '';
  const unavailableNote = rolledReason
    ? `<p class="yield-step-note" data-yield-unavailable>${esc(rolledReason)}.</p>`
    : '';
  const noPr = payload.no_pr
    ? `<p class="yield-step-note">${payload.no_pr} stor${payload.no_pr === 1 ? 'y has' : 'ies have'} no merged PR, so ${payload.no_pr === 1 ? 'it is' : 'they are'} excluded from the review denominator.</p>`
    : '';
  const sentence = rolled === '–'
    ? ''
    : `Only <b>${joint}</b> of ${n} stories went through without a loop-back.`;
  return `<section class="panel wide yield-panel" id="first-pass-yield"
      data-yield-n="${esc(n)}" data-rolled="${payload.rolled ?? ''}">
    <div class="panel-head">
      <h2>First-pass yield</h2>
      <span class="muted">the product of the four step yields · ${esc(payload.n)} completed stor${payload.n === 1 ? 'y' : 'ies'}</span>
    </div>
    <ol class="yield-waterfall">
      ${ordered.map((step) => stepColumn(step, n, payload.implied, base)).join('')}
      <li class="yield-rolled" data-yield-rolled-cell>
        <span class="yield-arrow" aria-hidden="true">→</span>
        <span class="yield-label">Rolled</span>
        <b class="yield-rate">${esc(rolled)}</b>
        <span class="yield-n mono">${esc(joint)} / ${esc(n)}</span>
      </li>
    </ol>
    ${sentence ? `<p class="yield-sentence">${sentence}</p>` : ''}
    ${hidden}${unavailableNote}${noPr}
    ${payload.implied && (payload.implied.steps && Object.keys(payload.implied.steps).length)
      ? `<p class="yield-step-note">${esc(payload.implied.basis || '')}</p>` : ''}
    <p class="panel-note">${esc(payload.basis || '')}</p>
  </section>`;
}

function stabilityValue(entry) {
  if (!entry || entry.rate == null) return '–';
  return fmtShare(entry.rate);
}

function stabilityDetail(entry, n, base) {
  if (!entry || entry.count == null) return '';
  const items = (entry.items || []).slice(0, 8);
  const more = (entry.items || []).length - items.length;
  const links = items.length
    ? ` ${items.map((key) => `<a class="drill-link mono" href="${esc(ticketHref(key, base))}">${esc(key)}</a>`).join(' ')}${more > 0 ? ` <span class="muted">+${more}</span>` : ''}`
    : '';
  return `${esc(entry.count)} of ${esc(n)}${links}`;
}

/**
 * The stability panel: four tiles of *deliveries*, each with its cohort and n.
 *
 * The bug share is a dash with the reason (never `summary_class` — that
 * classifies summary text, not the issue type); the rework proxy is printed
 * below and labelled a proxy, because this store has no deploy or incident
 * data and cannot claim DORA's rework rate.
 */
export function stabilityPanel(stability, { base = '' } = {}) {
  if (!stability) return '';
  if (!stability.available) {
    return `<section class="panel wide stability-panel" id="stability">
      <div class="panel-head"><h2>Stability</h2>
        <span class="pill neutral">unavailable</span></div>
      <p class="empty-row">${esc(stability.note || 'no status history in this store to derive loop-back rates from')}</p>
    </section>`;
  }
  const n = stability.n;
  const tiles = STABILITY_TILES.map(({ key, label }) => {
    const entry = stability[key] || {};
    if (key === 'bug_share' && entry.available === false) {
      return `<div class="stability-tile" data-stability="bug_share"
          title="bug share needs the issue type, which this store does not carry">
        <span class="overline">${esc(label)}</span>
        <b class="stability-rate">—</b>
        <span class="stability-detail">${esc(entry.reason || 'issue type is not in this store')}</span>
      </div>`;
    }
    const detail = stabilityDetail(entry, n, base);
    const tooltip = `${label}: ${entry.count ?? '–'} of ${n} deliveries${entry.signals ? ` — ${entry.signals.join(', ')}` : ''}`;
    return `<div class="stability-tile" data-stability="${esc(key)}" title="${esc(tooltip)}">
      <span class="overline">${esc(label)}</span>
      <b class="stability-rate">${esc(stabilityValue(entry))}</b>
      <span class="stability-detail">${detail}</span>
    </div>`;
  }).join('');
  const proxy = stability.rework_proxy || {};
  const proxyLine = proxy.rate != null
    ? `Rework proxy ${fmtShare(proxy.rate)} (${esc(proxy.basis || 'reopened or failed verification / deliveries')}) — not DORA's rework rate: this store has no deploy or incident data.`
    : 'Rework proxy unavailable — no status history to derive it from.';
  return `<section class="panel wide stability-panel" id="stability" data-stability-n="${esc(n)}">
    <div class="panel-head"><h2>Stability</h2>
      <span class="muted">loop-back rates over ${esc(n)} deliveries</span></div>
    <div class="stability-tiles">${tiles}</div>
    <p class="panel-note" data-rework-proxy>${proxyLine}</p>
  </section>`;
}

/** Both panels, in the order the Rules page reads: yield, then stability. */
export function firstPassPanels(metrics, { base = '' } = {}) {
  const yieldHtml = yieldPanel(metrics, { base });
  if (!yieldHtml) return '';
  return `${yieldHtml}${stabilityPanel(metrics && metrics.stability, { base })}`;
}

/** The "Copy as Markdown" half: the step table, the rates and the proxy. */
export function yieldMarkdown(metrics, base = '') {
  const payload = metrics && metrics.yield;
  if (!payload) return [];
  if (!payload.available) {
    return ['## First-pass yield', '', payload.note || 'unavailable in this store', ''];
  }
  const lines = [
    `## First-pass yield (rolled %C&A ${payload.rolled == null ? '–' : fmtShare(payload.rolled)})`,
    '',
    '| step | %C&A | clean / n | signals |',
    '| --- | --- | --- | --- |',
    ...(payload.steps || []).map((step) => `| ${step.label} | ${
      step.available === false
        ? 'not measured'
        : step.rate == null ? '–' : fmtShare(step.rate)} | ${
      step.clean_n ?? '–'} / ${step.n} | ${(step.signals || []).join(', ')} |`),
    '',
    ...rolledSentence(payload),
    '',
  ];
  const stability = metrics && metrics.stability;
  if (stability && stability.available) {
    const tile = (key, label) => {
      const entry = stability[key] || {};
      if (key === 'bug_share' && entry.available === false) {
        return `- ${label}: — (${entry.reason || 'issue type is not in this store'})`;
      }
      const items = (entry.items || []).length
        ? `: ${(entry.items || []).map((key_) => `[${key_}](${ticketHref(key_, base)})`).join(', ')}`
        : '';
      return `- ${label}: ${entry.rate == null ? '–' : fmtShare(entry.rate)} ` +
        `(${entry.count ?? '–'} of ${stability.n})${items}`;
    };
    lines.push('### Stability', '');
    lines.push(tile('reopen', 'Reopened'));
    lines.push(tile('failed_verification', 'Failed verification'));
    lines.push(tile('review_bounce', 'Review bounced'));
    lines.push(tile('bug_share', 'Bug share'));
    const proxy = stability.rework_proxy || {};
    lines.push('', `_Rework proxy ${proxy.rate == null ? '–' : fmtShare(proxy.rate)} ` +
      `(${proxy.basis || 'reopened or failed verification / deliveries'}) — not DORA's ` +
      'rework rate: this store has no deploy or incident data._', '');
  }
  return lines;
}

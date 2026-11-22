/* Aging WIP (prd/59): the forward-looking half of flow on the Delivery list.
 *
 * One question, the page's own ("What needs attention?"): how old is the work in
 * flight compared with how long this team's finished work takes? The answer is
 * a strip chart of in-flight age against the cohort's own p50/p85/p95 and a
 * ranked list — the chase data source prd/54's card and prd/58's exploit rung
 * read, instead of the fixed "stale = 5 business days" constant.
 *
 * Rules that keep the view honest and testable without a browser:
 *   · every row states its clock (`basis`) and its cohort's `n`;
 *   · a cohort below `min_n` renders "insufficient history" instead of a fake
 *     chart, a band or a multiple;
 *   · colour means state only: `aging` is a warning tint, `very old` a critical
 *     tint, `normal`/`watch` stay neutral (prd/10 §3.3);
 *   · a duration is business hours through the shared formatter, and the
 *     calendar-day span is spelled as such (concepts.md §1);
 *   · the list is window-scoped: a row is shown only when it moved in the
 *     chosen window, and the tab states how many open rows the window / live
 *     filter hid (prd/67 W-03);
 *   · on a phone the strip is a tick line (no chart), the rows stack to two
 *     lines, and nothing forces horizontal scroll (prd/10 §5.3, prd/28).
 */

import {
  esc, fmtHours, fmtCalendarDays, fmtDayYear, BUSINESS_HOURS_PER_DAY,
} from './format.js';
import { ticketHref, ticketLink, rowTicket } from './tickets.js';

export const BANDS = ['normal', 'watch', 'aging', 'very_old', 'insufficient'];

export const BAND_LABEL = {
  normal: 'normal',
  watch: 'watch',
  aging: 'aging',
  very_old: 'very old',
  insufficient: 'insufficient history',
};

/** The band's pill class: colour is state, and only aging/very-old are state. */
export function bandClass(band) {
  if (band === 'very_old') return 'blocked';
  if (band === 'aging') return 'waiting';
  return 'neutral';
}

/**
 * The active threshold block (the one the rows are read against).
 *
 * The API names it (`active_threshold`) so the chart, the bands and the feed
 * cannot disagree about which cohort is on screen.
 */
export function thresholdOf(payload) {
  if (!payload) return null;
  const blocks = Array.isArray(payload.thresholds) ? payload.thresholds : [];
  return blocks.find((block) => block.id === payload.active_threshold)
    || blocks[0] || null;
}

/** The view selector's options: `Story cycle | PR review | PR cycle | <stages>`. */
export function agingViewOptions(payload) {
  if (!payload) return [];
  // One control for four cohorts: the two story clocks, the two PR clocks and
  // every stage this store can answer. The payload carries the story stages
  // whatever cohort is on screen, so the selector never loses an option when
  // the kind changes.
  const options = [
    { value: 'story:cycle', label: 'Story cycle' },
    { value: 'pr:review', label: 'PR review' },
    { value: 'pr:cycle', label: 'PR cycle' },
  ];
  const stages = Array.isArray(payload.story_stages)
    ? payload.story_stages
    : (Array.isArray(payload.stages) ? payload.stages.filter((stage) => stage !== 'review') : []);
  for (const stage of stages) {
    if (stage === 'cycle') continue;
    if (options.some((option) => option.value === `story:${stage}`)) continue;
    options.push({
      value: `story:${stage}`,
      label: `Stage: ${String(stage).replace(/_/g, ' ')}`,
    });
  }
  return options;
}

/** `story:in_review` → `{kind: 'story', stage: 'in_review'}`. */
export function parseAgingView(value) {
  const text = String(value || 'story:cycle');
  const at = text.indexOf(':');
  const kind = at < 0 ? 'story' : text.slice(0, at);
  const stage = at < 0 ? text : text.slice(at + 1);
  return { kind: kind === 'pr' ? 'pr' : 'story', stage: stage || 'cycle' };
}

/** The three ticks (plus p75 under time in review), from the active block. */
export function agingTicks(payload) {
  const block = thresholdOf(payload);
  if (!block) return [];
  const ticks = [];
  if (block.p50 != null) ticks.push({ id: 'p50', label: 'p50', hours: block.p50 });
  if (block.p75 != null) ticks.push({ id: 'p75', label: 'p75', hours: block.p75 });
  if (block.p85 != null) ticks.push({ id: 'p85', label: 'p85', hours: block.p85 });
  if (block.p95 != null) ticks.push({ id: 'p95', label: 'p95', hours: block.p95 });
  return ticks;
}

/** The tick line a reader sees: `p50 13 h · p85 22 h 40 min · p95 31 h 11 min`. */
export function tickLine(payload) {
  const ticks = agingTicks(payload);
  if (!ticks.length) return 'no percentile — insufficient history';
  return ticks.map((tick) => `${tick.label} ${fmtHours(tick.hours)}`).join(' · ');
}

/**
 * prd/67 W-03: what the window and the only-live filter hid, as three numbers.
 *
 * The server scopes the rows to the window (a story must have moved in it) and
 * `only=live` hides the rest; `hidden` says how many of each, so the tab can
 * explain a short or empty list instead of looking broken.
 */
export function agingHidden(payload) {
  const hidden = (payload && payload.hidden) || {};
  const outsideWindow = Number(hidden.outside_window) || 0;
  const noLivePr = Number(hidden.no_live_pr) || 0;
  return {
    outside_window: outsideWindow,
    no_live_pr: noLivePr,
    total: outsideWindow + noLivePr,
  };
}

/** The window the payload was scoped to: `5 Jan 2026 → 1 Mar 2026`. */
export function agingWindowLabel(payload) {
  const window = (payload && payload.window) || {};
  const start = Date.parse(window.start);
  const end = Date.parse(window.end);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return '';
  return `${fmtDayYear(start)} → ${fmtDayYear(end)}`;
}

/**
 * The one sentence that explains a short aging list (prd/67 W-03).
 *
 * `9 open items are outside this window or have no live PR — see re-plan`. The
 * same words the API states in `hidden.note`, built here from the numbers the
 * same way, so the tab and an API reader cannot read two different counts.
 */
export function agingHiddenNote(payload) {
  const hidden = agingHidden(payload);
  if (!hidden.total) return '';
  if (hidden.total === 1) {
    return `1 open item ${hidden.outside_window
      ? 'is outside this window' : 'has no live PR'} — see re-plan`;
  }
  const reason = hidden.outside_window && hidden.no_live_pr
    ? 'are outside this window or have no live PR'
    : (hidden.outside_window ? 'are outside this window' : 'have no live PR');
  return `${hidden.total} open items ${reason} — see re-plan`;
}

/** The strip's x scale, shared by the dots, the ticks and the row bars. */
export function agingScale(payload) {
  const block = thresholdOf(payload);
  const rows = (payload && payload.rows) || [];
  const values = rows
    .map((row) => Number(row.age_hours) || 0)
    .concat(agingTicks(payload).map((tick) => Number(tick.hours) || 0))
    .filter((value) => value > 0);
  const max = Math.max(1, ...values) * 1.08;
  const pct = (value) => Math.max(0, Math.min(100, ((Number(value) || 0) / max) * 100));
  return {
    max,
    pct,
    ticks: agingTicks(payload).map((tick) => ({ ...tick, pct: pct(tick.hours) })),
    p85: block && block.p85 != null ? Number(block.p85) : null,
  };
}

/**
 * The 3-stop micro-scale a row carries: p50/p85/p95 tick positions with the
 * item's own position filled. The same encoding on every row, so the column
 * scans vertically (prd/59 §Layout).
 */
export function bandBar(row, payload) {
  if (!row || row.age_hours == null) return '<span class="band-bar blank"></span>';
  const block = thresholdOf(payload);
  const scale = agingScale(payload);
  const ticks = scale.ticks.map((tick) => `<i class="tick" style="left:${tick.pct.toFixed(2)}%" data-tick="${tick.id}"></i>`).join('');
  const band = row.band || 'insufficient';
  return `<span class="band-bar band-${esc(band)}" data-band="${esc(band)}"
    title="${esc(`${BAND_LABEL[band] || band} · ${fmtHours(row.age_hours)}`)}"
    >${ticks}<b class="mark" style="left:${scale.pct(row.age_hours).toFixed(2)}%"></b></span>`;
}

/** The tick row: `p50 11 d 2 h · p85 15 d 7 h · p95 21 d 4 h`, in flow.
 *
 * prd/65 N-06: the desktop strip used to place these labels with `left: %`
 * and `translateX(-50%)` on an axis scaled to the *oldest* item, so the
 * finished-work percentiles clustered in the left third and overlapped by
 * 7–45 px. One readable row in flow (wrapping, with gaps) cannot overlap on
 * either stage; the axis keeps its dashed rules so the position is still
 * visible without a label sitting on top of another.
 */
export function tickRow(payload) {
  const scale = agingScale(payload);
  return `<div class="aging-ticks" data-aging-ticks>${scale.ticks.map((tick) => `
      <span data-tick="${tick.id}" title="${esc(`${tick.label} = ${fmtHours(tick.hours)}`)}"
        >${tick.label} ${esc(fmtHours(tick.hours))}</span>`).join('')}</div>`;
}

/** One dot plot of in-flight age; desktop only (a phone gets the tick line). */
export function agingChart(payload, { phone = false } = {}) {
  const block = thresholdOf(payload);
  const rows = (payload && payload.rows) || [];
  if (!block || !block.available) {
    const n = block ? block.n : 0;
    const need = block ? block.min_n : 20;
    return `<div class="aging-insufficient" data-aging-insufficient>
      <p>Insufficient history: ${n} finished item${n === 1 ? '' : 's'} (needs ${need}) — no percentile shown.</p>
      <p class="muted">${esc((block && block.note) || 'The cohort is too small to describe this team’s work.')}</p>
    </div>`;
  }
  const scale = agingScale(payload);
  if (phone) {
    // prd/10 phone density: the three tick labels, no chart, no overflow.
    return `<div class="aging-ticks" data-aging-ticks>${scale.ticks.map((tick) => `
      <span data-tick="${tick.id}">${tick.label} ${esc(fmtHours(tick.hours))}</span>`).join('')}
      <span class="muted" data-aging-n>n = ${block.n}</span></div>`;
  }
  const dots = rows.map((row, index) => {
    const y = 16 + (index % 5) * 12;
    const age = Number(row.age_hours) || 0;
    return `<circle class="aging-dot band-${esc(row.band || 'insufficient')}"
      data-aging-dot="${esc(row.object_id)}" data-band="${esc(row.band || '')}"
      cx="${scale.pct(age).toFixed(2)}" cy="${y}" r="3.2"
      ><title>${esc(`${row.pr || row.ticket || row.object_id} · ${fmtHours(age)} · ${BAND_LABEL[row.band] || row.band}${row.live && row.live !== 'live' ? ` · ${row.live}` : ''}`)}</title></circle>`;
  }).join('');
  const ticks = scale.ticks.map((tick) => `
    <line class="aging-tick" data-chart-tick="${tick.id}"
      x1="${tick.pct.toFixed(2)}" x2="${tick.pct.toFixed(2)}" y1="8" y2="76"></line>`).join('');
  return `<div class="aging-strip" data-aging-chart>
    ${tickRow(payload)}
    <svg class="aging-plot" viewBox="0 0 100 88" preserveAspectRatio="none" role="img"
      aria-label="in-flight age against the team’s own p50, p85 and p95">
      <line class="aging-base" x1="0" x2="100" y1="76" y2="76"></line>
      ${ticks}${dots}
    </svg>
    <p class="aging-axis" data-aging-n>n = ${block.n} finished item${block.n === 1 ? '' : 's'} · ${
      esc(block.basis || '')}</p>
  </div>`;
}

/** The holder cell: `FrMo (sentinel)` — every holder shares the wait. */
export function holderLabel(row) {
  const holders = (row && row.holder) || [];
  if (!holders.length) return '—';
  return holders
    .map((holder) => `${holder.person}${holder.role ? ` (${holder.role})` : ''}`)
    .join(' + ');
}

/** `1.0×` inside the band, `8.5×` outside — the multiple of the p85. */
export function multipleLabel(row) {
  // A suppressed percentile has no multiple: an "×" over a cohort below the
  // minimum would be a comparison the data does not support (prd/59 §Honesty).
  if (!row || row.multiple == null || row.band === 'insufficient') return '—';
  return `${Number(row.multiple).toFixed(1)}×`;
}

/** The ranked rows: hours at risk descending, as the API's chase order. */
export function agingRows(payload, { onlyLive = false } = {}) {
  const rows = ((payload && payload.rows) || []).slice();
  const kept = onlyLive
    ? rows.filter((row) => row.live === 'live' || row.live === 'unmerged')
    : rows;
  return kept.sort((a, b) => (
    (Number(b.hours_at_risk) || 0) - (Number(a.hours_at_risk) || 0)
    || (Number(b.dependents) || 0) - (Number(a.dependents) || 0)
    || (Number(b.age_hours) || 0) - (Number(a.age_hours) || 0)
    || String(a.object_id).localeCompare(String(b.object_id))
  ));
}

/** The ranked list, dense single-line rows on every device (prd/10 §5.3). */
export function agingTable(payload, { base = '', onlyLive = false, rowFilter = null } = {}) {
  let rows = agingRows(payload, { onlyLive });
  if (rowFilter) rows = rows.filter(rowFilter);
  const block = thresholdOf(payload);
  if (!rows.length) {
    // prd/67 W-03: an empty list is honest only when it says what it hid — the
    // window it was scoped to and how many open items the window/only filter left
    // out (they are still in the store; the re-plan feed names the dead ones).
    const window = agingWindowLabel(payload);
    const hidden = agingHidden(payload);
    const note = agingHiddenNote(payload);
    return `<div class="empty-row" data-aging-empty>Nothing in flight matches this view${
      onlyLive ? ' with a live PR' : ''} — an empty list here is a real result.${
      window ? ` Window: ${esc(window)}.` : ''}${
      hidden.total ? ` ${esc(note)}.` : ''}</div>`;
  }
  const body = rows.map((row) => {
    const key = row.ticket || rowTicket(row);
    const live = row.live || 'live';
    return `<tr class="aging-row" tabindex="0" data-aging="${esc(row.object_id)}"
        data-band="${esc(row.band || '')}" data-live="${esc(live)}">
      <td class="c-band" data-label="">${bandBar(row, payload)}</td>
      <td class="c-kind" data-label="kind"><span class="mono">${esc(row.kind)}</span></td>
      <td class="c-id" data-label="">${key
        ? ticketLink(key, { base })
        : `<span class="keys">${esc(row.object_id)}</span>`}${
        row.pr ? ` <span class="mono id">${esc(row.pr)}</span>` : ''}${
        row.repo_short ? ` <span class="repo">${esc(row.repo_short)}</span>` : ''}</td>
      <td class="c-stage" data-label="stage"><span class="mono">${esc(row.stage || '')}</span></td>
      <td class="c-age num" data-label="age"><b>${esc(fmtHours(row.age_hours))}</b>
        <span class="calendar">${esc(fmtCalendarDays(row.calendar_days))}</span>
        <span class="working">${esc(workingDaysLabel(row.age_hours))}</span></td>
      <td class="c-mult num" data-label="×p85"
        title="${block && block.p85 != null ? esc(`p85 = ${fmtHours(block.p85)}` ) : 'no p85'}">${esc(multipleLabel(row))}</td>
      <td class="c-holder mono" data-label="holder">${esc(holderLabel(row))}</td>
      <td class="c-live" data-label="state"><span class="pill ${live === 'live' ? 'neutral' : (live === 'unmerged' ? 'waiting' : 'blocked')}"
        data-live-pill="${esc(live)}">${esc(live)}</span>
        <span class="pill ${bandClass(row.band)}" data-band-pill="${esc(row.band || '')}">${esc(BAND_LABEL[row.band] || row.band || '')}</span></td>
    </tr>`;
  }).join('');
  return `<table class="table aging" data-aging-table>${'<thead><tr>' +
    '<th class="c-band"></th><th class="c-kind">Kind</th><th class="c-id">Ticket / PR</th>' +
    '<th class="c-stage">Stage</th><th class="c-age num">Age</th>' +
    '<th class="c-mult num" title="age ÷ the cohort’s p85">×p85</th>' +
    '<th class="c-holder">Holder</th><th class="c-live">State</th></tr></thead>'}<tbody>${body}</tbody></table>`;
}

/** The abandoned tail: no live PR, so a nudge changes nothing. */
export function replanList(payload) {
  const items = (payload && payload.feed && payload.feed.replan) || [];
  if (!items.length) return '';
  const rows = items.map((item) => `<tr data-replan="${esc(item.object_id)}">
      <td class="c-id" data-label="ticket">${item.href
        ? `<a class="keys" href="${esc(item.href)}">${esc(item.ticket || item.pr || item.object_id)}</a>`
        : esc(item.ticket || item.object_id)}${
        item.pr ? ` <span class="mono id">${esc(item.pr)}</span>` : ''}</td>
      <td class="c-age num" data-label="age">${esc(fmtHours(item.age_hours))}</td>
      <td class="c-who" data-label="why">${esc(item.reason || 'no live PR')}</td>
    </tr>`).join('');
  return `<section class="panel wide aging-replan" id="aging-replan">
    <div class="panel-head"><h2>No live PR — close or re-plan</h2>
      <span class="muted">${items.length} item${items.length === 1 ? '' : 's'} not waiting on a nudge</span></div>
    <table class="table compact"><thead><tr><th class="c-id">ticket</th>
      <th class="c-age num">age</th><th class="c-who">why</th></tr></thead>
      <tbody>${rows}</tbody></table>
  </section>`;
}

/**
 * The counts of the rows *on screen*, not the team's (prd/65 N-05).
 *
 * The payload's `counts` are computed server-side over every row of the
 * cohort, which is right for `scope=all` and wrong the moment the page filters
 * the rows client-side (person/role/ticket scopes, the sprint lens). The chip,
 * the honesty line and the tab badge read this function instead, so all three
 * describe the same list as the table.
 */
export function agingScopedCounts(rows, payload) {
  const list = rows || [];
  const block = thresholdOf(payload);
  const p50 = block && block.p50 != null ? Number(block.p50) : null;
  const base = (payload && payload.counts) || {};
  return {
    ...base,
    open: list.length,
    live: list.filter((row) => row.live === 'live' || row.live === 'unmerged').length,
    dead: list.filter((row) => row.live === 'dead').length,
    over_p50: p50 == null
      ? 0
      : list.filter((row) => row.age_hours != null && Number(row.age_hours) >= p50).length,
    over_p85: list.filter((row) => row.band === 'aging' || row.band === 'very_old').length,
    over_p95: list.filter((row) => row.band === 'very_old').length,
    insufficient: list.filter((row) => row.band === 'insufficient').length,
    unmerged: list.filter((row) => row.live === 'unmerged').length,
    chase: list.filter((row) => (row.live === 'live' || row.live === 'unmerged')
      && (row.band === 'aging' || row.band === 'very_old')).length,
  };
}

/**
 * The payload every surface of the Aging tab renders (prd/65 N-05).
 *
 * One `rowFilter`, applied once, produces one row list; the chart, the count
 * chip, the table, the re-plan list and the honesty line all read *this*
 * payload, so a scope can never filter one of them and leave another showing
 * the team's numbers. The chase feed and the re-plan list are filtered by the
 * rows that survived (the server derives both from the same rows).
 */
export function scopedAgingPayload(payload, { onlyLive = false, rowFilter = null } = {}) {
  if (!payload) return payload;
  // No client-side scope (the common case): the server's rows and counts are
  // already the answer, and the chart keeps the payload's own order.
  if (!rowFilter) return payload;
  const kept = agingRows(payload, { onlyLive }).filter(rowFilter);
  const ids = new Set(kept.map((row) => row.object_id));
  const feed = payload.feed
    ? {
      ...payload.feed,
      items: (payload.feed.items || []).filter((item) => ids.has(item.object_id)),
      replan: (payload.feed.replan || []).filter((item) => ids.has(item.object_id)),
    }
    : payload.feed;
  return {
    ...payload,
    rows: kept,
    counts: agingScopedCounts(kept, payload),
    feed,
  };
}

/** The whole tab: controls, strip, ranked list and the honesty lines. */
export function agingMarkup(payload, {
  base = '', phone = false, onlyLive = false, view = 'story:cycle',
  source = 'self', rowFilter = null,
} = {}) {
  if (!payload) return '<div class="empty-row">Loading the aging view…</div>';
  // prd/65 N-05: one scoped payload for the whole tab — the chip, the dots, the
  // table and the re-plan list below all read these rows, so a scope cannot
  // leave one of them quoting the team's numbers.
  const scoped = scopedAgingPayload(payload, { onlyLive, rowFilter });
  const block = thresholdOf(scoped);
  const options = agingViewOptions(scoped);
  const counts = scoped.counts || {};
  const controls = `<div class="aging-controls" data-aging-controls>
    <label class="aging-control"><span>Stage</span>
      <select data-aging-view aria-label="aging cohort">${options.map((option) => `
        <option value="${esc(option.value)}"${option.value === view ? ' selected' : ''}>${esc(option.label)}</option>`).join('')}
      </select></label>
    <button type="button" class="chip" data-aging-only aria-pressed="${onlyLive ? 'true' : 'false'}"
      title="hide abandoned work: a row with no live PR is close-or-re-plan, not a nudge"
      >${onlyLive ? 'Only live ✓' : 'Only live'}</button>
    <label class="aging-control"><span>Threshold source</span>
      <select data-aging-source aria-label="threshold source">
        <option value="self"${source !== 'fixed' ? ' selected' : ''}>self-calibrated</option>
        <option value="fixed"${source === 'fixed' ? ' selected' : ''}>fixed (5 bd, 24 h)</option>
      </select></label>
    <span class="grow"></span>
    <span class="aging-counts" data-aging-counts>${counts.open || 0} open · ${counts.over_p85 || 0} past p85${
      counts.dead ? ` · ${counts.dead} no live PR` : ''}${counts.insufficient ? ` · ${counts.insufficient} insufficient` : ''}</span>
  </div>`;
  const sourceLine = block && block.source_label
    ? `<span class="aging-source" data-threshold-source>${esc(block.source)} · ${esc(block.source_label)}</span>`
    : '';
  // prd/67 W-03: the window scopes the list, so the tab says what it hid rather
  // than letting a short list read as "nothing is in flight".
  const hiddenNote = agingHiddenNote(scoped);
  return `<div class="aging-view" data-aging-view-root data-kind="${esc(scoped.kind || '')}">
    ${controls}
    ${hiddenNote
      ? `<p class="aging-hidden muted" data-aging-hidden>${esc(hiddenNote)}</p>`
      : ''}
    <div class="aging-head">
      <h3>${esc(scoped.kind === 'pr' ? 'PRs in flight' : 'Stories in flight')}
        <span class="muted">${esc(scoped.stage ? String(scoped.stage).replace(/_/g, ' ') : '')}</span></h3>
      ${sourceLine}
    </div>
    ${agingChart(scoped, { phone })}
    <p class="aging-meta">Ages are business hours on the holder’s calendar; a person on vacation accrues none.
      Calendar days in brackets.</p>
    <p class="aging-censor">An age is a censored duration — the item has not finished yet; compare it with
      finished work, never add it to a cycle-time median.</p>
    ${agingTable(scoped, { base, onlyLive })}
    ${replanList(scoped)}
  </div>`;
}

/** The ranked list as Markdown — the same order, band and multiple as the rows. */
export function agingMarkdown(payload, {
  base = '', onlyLive = false, rowFilter = null,
} = {}) {
  if (!payload) return '';
  let rows = agingRows(payload, { onlyLive });
  if (rowFilter) rows = rows.filter(rowFilter);
  const block = thresholdOf(payload);
  const title = `Aging — ${payload.kind === 'pr' ? 'PR' : 'story'} ${
    payload.stage === 'cycle' ? 'cycle' : String(payload.stage || '').replace(/_/g, ' ')}`;
  const head = [
    `# ${title}`,
    '',
    block && block.available
      ? `_${tickLine(payload)} · n = ${block.n} (${block.basis || ''})_`
      : `_insufficient history — no percentile shown_`,
    '',
    '| band | kind | ticket / PR | stage | age | ×p85 | holder | state |',
    '| --- | --- | --- | --- | --- | --- | --- | --- |',
  ];
  const body = rows.map((row) => {
    const key = row.ticket || rowTicket(row);
    const ticket = key ? `[${key}](${ticketHref(key, base)})` : row.object_id;
    return `| ${BAND_LABEL[row.band] || row.band || ''} | ${row.kind} | ${ticket}${
      row.pr ? ` ${row.pr}` : ''} | ${row.stage || ''} | ${fmtHours(row.age_hours)} (${fmtCalendarDays(row.calendar_days)}) | ${
      multipleLabel(row)} | ${holderLabel(row)} | ${row.live || 'live'} |`;
  });
  // prd/67 W-03: the export states the same hidden count as the tab.
  const hidden = agingHiddenNote(payload);
  const window = agingWindowLabel(payload);
  const footer = [
    ...(hidden ? ['', `_${hidden}${window ? ` (window ${window})` : ''}_`] : []),
  ];
  return [...head, ...body, ...footer].join('\n');
}

/** The tab's count, from the same rows the table renders. */
export function agingCount(payload) {
  return Number((payload && payload.counts && payload.counts.open) || 0);
}

/** The chase feed prd/54 / prd/58 read: the ranked items, not a second list. */
export function chaseItems(payload) {
  return ((payload && payload.feed && payload.feed.items) || []).slice();
}

/** `58 working days` — the second line a phone row shows. */
export function workingDaysLabel(hours) {
  const days = (Number(hours) || 0) / BUSINESS_HOURS_PER_DAY;
  const rounded = Math.round(days * 10) / 10;
  return `${rounded} working day${rounded === 1 ? '' : 's'}`;
}

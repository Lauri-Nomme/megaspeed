/* Sprints (prd/26 P6, prd/37): the sprint lens as bands, scope and a strip.
 *
 * The timeline canvas draws the **bands** (one per SPRINT object over its
 * measured window); this module turns the same `/api/sprints` payload into
 *
 *   - `sprintBands` — the bands the canvas needs, in milliseconds, including
 *     the stories that belong to each (the lens filter);
 *   - `sprintScopeLine` — the one-line disclosure: how many stories the window
 *     committed to a sprint and how many it did not;
 *   - `sprintStrip` — a dense strip under the timeline: one row per sprint with
 *     its own scope, its **done-by-end / done-late / never** breakdown, and its
 *     carry-over counts. A row selects that sprint (`data-sprint-start`/`-end`,
 *     handled by main.js): the window, the story filter, the band highlight and
 *     the URL are one action.
 *
 * Everything here is pure so it can be unit-tested without a browser.
 */

import { fmtDay, fmtHours, esc } from './format.js';
import { ticketLink } from './tickets.js';

/** The payload's bands as `{id, label, start, end, stories, ...}` in milliseconds. */
export function sprintBands(data) {
  if (!data || !data.available) return [];
  return (data.sprints || [])
    .map((sprint) => ({
      id: sprint.sprint_id,
      label: `S${sprint.sprint_id}`,
      state: sprint.state,
      open: Boolean(sprint.open),
      start: Date.parse(sprint.band_start),
      end: Date.parse(sprint.band_end),
      plannedEnd: sprint.planned_end
        ? Date.parse(`${sprint.planned_end}T23:59:59Z`) : null,
      stories: sprint.stories || [],
      storyCount: sprint.story_count || 0,
      points: sprint.points || 0,
      // prd/37: "done" is done by the sprint's close, never "done by now"
      done: sprint.done_by_end ?? sprint.done ?? 0,
      doneByEnd: sprint.done_by_end ?? sprint.done ?? 0,
      doneLate: sprint.done_late || 0,
      neverDone: sprint.never_done || 0,
      // prd/43 §5: Done in Jira, PR unmerged — not delivered work
      unmerged: sprint.unmerged || 0,
      doneShare: sprint.done_share ?? null,
      // prd/46 §4: the plan (at start) vs the scope adopted mid-sprint
      committed: sprint.committed ?? null,
      adopted: sprint.adopted ?? null,
      pointsShare: sprint.points_share ?? null,
      lateHours: sprint.late_hours || 0,
      // prd/55 §5: a long sprint's delivery share is not comparable
      days: sprint.days ?? null,
      plannedDays: sprint.planned_days ?? null,
      lengthOutlier: Boolean(sprint.length_outlier),
      lengthNote: sprint.length_note || null,
      carriedIn: sprint.carried_in || [],
      carriedOut: sprint.carried_out || [],
    }))
    .filter((band) => Number.isFinite(band.start) && Number.isFinite(band.end));
}

/** The sprint scope in words, or '' when the store has no sprint layer. */
export function sprintScopeLine(data) {
  if (!data || !data.available) return '';
  const scope = data.scope || {};
  const carry = data.carry_over || {};
  const stories = scope.stories || 0;
  const window = data.window || {};
  const days = Number(window.days);
  // prd/41 B8: the carry-over count is the window's cohort; the bands are the
  // store's sprints. Say which window the numbers are filtered to.
  // concepts.md §1: `d` is a business day, so a wall-clock window length must
  // say "calendar days" explicitly (this number is `(end - start) / 86400`).
  const windowNote = Number.isFinite(days) && days > 0
    ? ` · created in ${days} calendar days` : '';
  const bits = [
    `${data.count || 0} sprint${(data.count || 0) === 1 ? '' : 's'}`,
    `${scope.in_sprint || 0} of ${stories} stories in a sprint${windowNote}`,
    `${scope.out_of_sprint || 0} out of scope`,
    `${carry.count || 0} carried over`,
  ];
  if (carry.late) bits.push(`${carry.late} finished late`);
  if (data.active != null) bits.push(`active S${data.active}`);
  return bits.join(' · ');
}

function windowLabel(sprint) {
  const start = fmtDay(Date.parse(sprint.band_start));
  const end = fmtDay(Date.parse(sprint.band_end));
  return `${start} → ${end}`;
}

/** The done-by-end / late / never breakdown of one sprint, in words. */
export function sprintOutcomeLine(sprint) {
  const total = sprint.story_count || 0;
  const byEnd = sprint.done_by_end ?? sprint.done ?? 0;
  const late = sprint.done_late || 0;
  const never = sprint.never_done || 0;
  const unmerged = sprint.unmerged || 0;
  const parts = [`${byEnd} of ${total} done by end`];
  // prd/46 §4: "committed" is the scope at the start; the rest was adopted.
  if (sprint.committed != null) {
    parts.push(`${sprint.committed} committed at start`);
    if (sprint.adopted) parts.push(`${sprint.adopted} adopted mid-sprint`);
  }
  if (late) parts.push(`${late} late`);
  if (unmerged) parts.push(`${unmerged} Done in Jira · PR unmerged`);
  if (never) parts.push(`${never} still open`);
  // prd/55 §5: the length outlier is part of the verdict, not a footnote
  if (sprint.length_outlier && sprint.length_note) parts.push(sprint.length_note);
  return parts.join(' · ');
}

function carryCell(sprint) {
  const into = (sprint.carried_in || []).length;
  const out = (sprint.carried_out || []).length;
  if (!into && !out) return '';
  const parts = [];
  if (into) parts.push(`${into} in`);
  if (out) parts.push(`${out} out`);
  const keys = [...(sprint.carried_in || []), ...(sprint.carried_out || [])];
  return `<span class="sprint-carry" title="committed but not done by the close: ${esc(keys.join(', '))}">↻ ${esc(parts.join(' · '))}</span>`;
}

/* ------------------------------------------------------------------------- */
/* Sprint-at-risk (prd/52).                                                   */
/* ------------------------------------------------------------------------- */

const RISK_LABELS = {
  'done-but-unmerged': 'Done in Jira, PR unmerged',
  'stuck-on': 'stuck on a person',
  'approved-unmerged': 'approved, never merged',
  'no-live-pr': 'no live PR',
};

function riskWho(row) {
  return (row.who || []).filter(Boolean);
}

/** The risk reason in words; a single holder is named ("stuck on FrMo"). */
export function riskReasonText(row) {
  if (!row) return '';
  const who = riskWho(row);
  if (row.reason === 'stuck-on') {
    return who.length === 1
      ? `stuck on ${who[0]}`
      : `stuck on ${who.length} people`;
  }
  return RISK_LABELS[row.reason] || row.label || row.reason || 'at risk';
}

/** What the row's detail cell says, from the same PR facts the list uses. */
export function riskDetailText(row) {
  if (!row) return '';
  const detail = row.detail || {};
  if (row.reason === 'done-but-unmerged') {
    const prs = (detail.prs || []).map((number) => `#${number}`).join(', ');
    const after = detail.opened_after_close || [];
    const suffix = after.length ? ' (opened after the ticket closed)' : '';
    return prs ? `${prs} still open${suffix}` : `the PR is still open${suffix}`;
  }
  if (row.reason === 'stuck-on') {
    const prs = (detail.prs || []).map((number) => `#${number}`).join(', ');
    return prs ? `${prs} open` : 'a live PR is waiting';
  }
  if (row.reason === 'approved-unmerged') {
    const approved = (detail.approved_by || []).join(' + ') || 'nobody';
    if (detail.state === 'declined') {
      return `approved by ${approved} · ${detail.bot_closed
        ? `closed by the bot ${detail.closed_by || ''}`.trim()
        : `closed by ${detail.closed_by || 'someone'}`}`;
    }
    return `approved by ${approved} · not merged`;
  }
  if (row.reason === 'no-live-pr') {
    const prs = (detail.prs || []).map((number) => `#${number}`).join(', ');
    if (!prs) return 'no PR names this key';
    const merged = detail.merged_prs || [];
    if (merged.length && merged.length === (detail.prs || []).length) {
      return `${prs} merged · Jira still open`;
    }
    return `${prs} closed without merging`;
  }
  return '';
}

/**
 * The active sprint's forecast: each not-delivered story, why it is at risk and
 * who owes the next action. It reuses the sprint band's own counts (25 of 30
 * done by the snapshot) and the delivery list's holder facts, so the list can
 * never disagree with the strip above it.
 */
export function atRiskMarkup(data, { base = '' } = {}) {
  const risk = data && data.at_risk;
  if (!risk || !risk.available) return '';
  const rows = risk.rows || [];
  const count = risk.count ?? rows.length;
  const points = risk.points_at_risk ?? 0;
  const head = `<div class="panel-head"><h2>Sprint ${esc(risk.sprint_id)} at risk</h2>
      <span class="pill ${count ? 'warning' : 'healthy'}" data-at-risk-count="${count}">${count} stor${count === 1 ? 'y' : 'ies'} · ${points} pts</span>
      <span class="muted">${risk.done_by_end ?? 0} of ${risk.stories ?? 0} delivered by the snapshot${
        risk.planned_end ? ` · plan ends ${esc(risk.planned_end)}` : ''}</span></div>`;
  if (!rows.length) {
    return `<section class="panel wide at-risk-panel" id="sprint-at-risk">
      ${head}<p class="panel-note">Every story in the active sprint is done and its code merged.</p>
    </section>`;
  }
  const body = rows.map((row) => {
    const who = riskWho(row);
    return `<tr data-at-risk-key="${esc(row.key)}" data-risk="${esc(row.reason)}"
        data-risk-who="${esc(who.join(' + '))}">
      <td class="c-id" data-label="ticket">${ticketLink(row.key, { base })}
        <span class="muted">${row.points || 0} pts</span></td>
      <td class="c-state" data-label="reason"><span class="pill ${
        row.reason === 'done-but-unmerged' ? 'blocked' : 'warning'
      }" title="${esc(RISK_LABELS[row.reason] || '')}">${esc(riskReasonText(row))}</span></td>
      <td class="c-who" data-label="who owes">${esc(who.join(' + ') || '—')}</td>
      <td class="c-dur num" data-label="waiting">${esc(fmtHours(row.waiting_hours))}</td>
      <td class="c-note" data-label="note">${esc(riskDetailText(row))}</td>
    </tr>`;
  }).join('');
  return `<section class="panel wide at-risk-panel" id="sprint-at-risk">
    ${head}
    <div class="table-wrap"><table class="table compact at-risk-table"><thead><tr>
      <th class="c-id">ticket</th><th class="c-state">reason</th>
      <th class="c-who">who owes</th><th class="c-dur num">waiting</th>
      <th class="c-note">note</th>
    </tr></thead><tbody>${body}</tbody></table></div>
    <p class="panel-note">${esc(risk.basis || '')} · a story is at risk when the
      sprint's own done-by-snapshot count does not deliver it</p>
  </section>`;
}

function outOfScope(data, base) {
  const scope = data.scope || {};
  const keys = scope.unplanned || [];
  if (!keys.length) return '';
  const shown = keys.slice(0, 50);
  const more = keys.length > shown.length
    ? ` <span class="muted">+${keys.length - shown.length} more</span>` : '';
  const reasons = `${scope.never_in_sprint || 0} never planned · ` +
    `${scope.removed_to_backlog || 0} removed to the backlog`;
  return `<details class="sprint-out">
    <summary>${keys.length} stor${keys.length === 1 ? 'y' : 'ies'} out of sprint scope
      <span class="muted">(${reasons})</span></summary>
    <p class="sprint-out-keys">${shown.map((key) => ticketLink(key, { base })).join(' · ')}${more}</p>
  </details>`;
}

function carriedOver(data, base) {
  const carry = data.carry_over || {};
  const rows = carry.rows || [];
  if (!rows.length) return '';
  const shown = rows.slice(0, 50);
  const more = rows.length > shown.length
    ? ` <span class="muted">+${rows.length - shown.length} more</span>` : '';
  const items = shown.map((row) => {
    const path = (row.sprints || []).join(' → ');
    const status = row.status ? ` ${row.status}` : '';
    // prd/42 M5: say where it went next, or that it never reappeared.
    const destination = row.reappears === true
      ? ` → S${row.destination_sprint}`
      : row.reappears === false
        ? ' → never re-committed'
        : '';
    return `${ticketLink(row.key, { base })} <span class="muted mono">${esc(path + status + destination)}</span>`;
  });
  const destination = (carry.never_reappeared || 0)
    ? ` · <b>${carry.never_reappeared}</b> never reappear in the next sprint`
      + (carry.no_next_sprint ? ` (${carry.no_next_sprint} have no next sprint)` : '')
    : '';
  return `<p class="sprint-carry-list">committed but not done by the close: ${items.join(' · ')}${more}${destination}</p>`;
}

/** The sprint lens selection line: what is filtered and how to clear it. */
function focusLine(data, focus) {
  const sprint = (data.sprints || []).find((row) => row.sprint_id === focus);
  if (!sprint) return '';
  const late = sprint.done_late || 0;
  const never = sprint.never_done || 0;
  return `<span class="sprint-focus" data-sprint-focus="${esc(focus)}">
    <b>S${esc(focus)} lens</b> — ${esc(sprintOutcomeLine(sprint))}${
      late ? ` · ${fmtHours(sprint.late_hours || 0)} of slip` : ''} ·
    the timeline shows this sprint's stories
    <button type="button" class="chip" data-sprint-clear>clear</button></span>`;
}

/** The sprint strip: one row per sprint, plus the out-of-scope and carry lists. */
export function sprintStrip(data, { base = '', focus = null } = {}) {
  if (!data || !data.available) return '';
  const sprints = data.sprints || [];
  const rows = sprints.map((sprint) => {
    const start = Date.parse(sprint.band_start);
    const end = Date.parse(sprint.band_end);
    const plan = sprint.open && sprint.planned_end
      ? `<span class="sprint-plan" title="the plan is not the band: an active sprint's band ends at the snapshot">plan ${esc(sprint.planned_end)}</span>`
      : '';
    const state = sprint.state || (sprint.open ? 'active' : 'closed');
    const planned = sprint.planned_days ?? sprint.plannedDays;
    const outlier = Boolean(sprint.length_outlier ?? sprint.lengthOutlier);
    const days = outlier && planned != null
      ? `${planned} calendar days planned · outlier`
      : (sprint.days == null ? '' : `${sprint.days} calendar days`);
    const selected = focus != null && String(focus) === String(sprint.sprint_id);
    const late = sprint.done_late || 0;
    const never = sprint.never_done || 0;
    const unmerged = sprint.unmerged || 0;
    const added = sprint.added_after_start || 0;
    const adopted = sprint.adopted == null ? added : sprint.adopted;
    const committed = sprint.committed == null ? null : sprint.committed;
    return `<button type="button" class="sprint-row${sprint.open ? ' open' : ''}${selected ? ' selected' : ''}"
        data-sprint-id="${esc(sprint.sprint_id)}"
        data-sprint-start="${Number.isFinite(start) ? start : ''}"
        data-sprint-end="${Number.isFinite(end) ? end : ''}"
        aria-pressed="${selected}"
        title="select sprint ${esc(sprint.sprint_id)} (${esc(windowLabel(sprint))}): ${esc(sprintOutcomeLine(sprint))}">
      <span class="sprint-id mono">S${esc(sprint.sprint_id)}</span>
      <span class="sprint-state">${esc(state)}</span>
      <span class="sprint-window mono">${esc(windowLabel(sprint))}</span>
      <span class="sprint-days num muted">${esc(days)}</span>
      ${outlier ? `<span class="sprint-length-flag" title="${esc(sprint.length_note || 'a sprint whose planned length is an outlier')}">length outlier</span>` : ''}
      <span class="sprint-scope-count num">${sprint.story_count || 0} stor${(sprint.story_count || 0) === 1 ? 'y' : 'ies'} · ${sprint.points || 0} pts${
        committed != null ? ` · <span class="sprint-committed" title="stories in the sprint when it started — the commitment it was planned with">${committed} committed at start</span>` : ''}${
        adopted ? ` · <span class="sprint-added" title="stories added after the sprint's start — adopted scope, not the plan it was committed to">${adopted} adopted</span>` : ''}</span>
      <span class="sprint-done num" title="done by this sprint's close — not done by today">${
        sprint.done_by_end ?? sprint.done ?? 0} of ${sprint.story_count || 0} by end</span>
      ${late ? `<span class="sprint-late num" title="reached done after the sprint's close">${late} late</span>` : ''}
      ${unmerged ? `<span class="sprint-unmerged num" title="Done in Jira while a PR the story names never merged — the code is not delivered">${unmerged} unmerged</span>` : ''}
      ${never ? `<span class="sprint-never num muted" title="not done at the snapshot">${never} open</span>` : ''}
      ${carryCell(sprint)}
      ${plan}
    </button>`;
  }).join('');
  const delivery = data.delivery || {};
  const onTime = delivery.measured_sprints
    ? `<span class="sprint-ontime" title="done by end ÷ committed, per closed sprint; target ${Math.round((delivery.target_share || 0) * 100)} %">${
      delivery.on_time_sprints || 0}/${delivery.measured_sprints} sprints on time</span>`
    : '';
  return `<div class="sprint-strip" role="group" aria-label="sprints">
    <div class="sprint-strip-head">
      <span class="sprint-strip-title">sprints</span>
      <span class="sprint-scope-line">${esc(sprintScopeLine(data))}</span>
      ${onTime}
      ${(data.carry_over && data.carry_over.count)
        ? '<span class="sprint-legend"><span class="sprint-carry">↻</span> committed but not done by the close</span>'
        : ''}
      <span class="grow"></span>
      <span class="hint">click a sprint to filter the timeline to it</span>
    </div>
    ${focus != null ? focusLine(data, focus) : ''}
    <div class="sprint-rows">${rows}</div>
    ${atRiskMarkup(data, { base })}
    ${carriedOver(data, base)}
    ${outOfScope(data, base)}
  </div>`;
}

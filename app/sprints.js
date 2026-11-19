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
      doneShare: sprint.done_share ?? null,
      lateHours: sprint.late_hours || 0,
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
  const bits = [
    `${data.count || 0} sprint${(data.count || 0) === 1 ? '' : 's'}`,
    `${scope.in_sprint || 0} of ${stories} stories in a sprint`,
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
  const committed = sprint.story_count || 0;
  const byEnd = sprint.done_by_end ?? sprint.done ?? 0;
  const late = sprint.done_late || 0;
  const never = sprint.never_done || 0;
  const parts = [`${byEnd} of ${committed} done by end`];
  if (late) parts.push(`${late} late`);
  if (never) parts.push(`${never} still open`);
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
    return `${ticketLink(row.key, { base })} <span class="muted mono">${esc(path + status)}</span>`;
  });
  return `<p class="sprint-carry-list">committed but not done by the close: ${items.join(' · ')}${more}</p>`;
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
    const days = sprint.days == null ? '' : `${sprint.days} calendar days`;
    const selected = focus != null && String(focus) === String(sprint.sprint_id);
    const late = sprint.done_late || 0;
    const never = sprint.never_done || 0;
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
      <span class="sprint-scope-count num">${sprint.story_count || 0} stor${(sprint.story_count || 0) === 1 ? 'y' : 'ies'} · ${sprint.points || 0} pts</span>
      <span class="sprint-done num" title="done by this sprint's close — not done by today">${
        sprint.done_by_end ?? sprint.done ?? 0} of ${sprint.story_count || 0} by end</span>
      ${late ? `<span class="sprint-late num" title="reached done after the sprint's close">${late} late</span>` : ''}
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
    ${carriedOver(data, base)}
    ${outOfScope(data, base)}
  </div>`;
}

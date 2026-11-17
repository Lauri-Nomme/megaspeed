/* Sprints (prd/26 P6): the sprint lens as bands, scope and a dense strip.
 *
 * The timeline canvas draws the **bands** (one per SPRINT object over its
 * measured window); this module turns the same `/api/sprints` payload into
 *
 *   - `sprintBands` — the bands the canvas needs, in milliseconds;
 *   - `sprintScopeLine` — the one-line disclosure: how many stories the window
 *     committed to a sprint and how many it did not;
 *   - `sprintStrip` — a dense strip under the timeline: one row per sprint with
 *     its own scope (stories, points, done) and its carry-over counts. A row
 *     sets the timeframe to that sprint (`data-sprint-start`/`-end`, handled by
 *     main.js), which is the sprint view without a second page.
 *
 * Everything here is pure so it can be unit-tested without a browser.
 */

import { fmtDay, esc } from './format.js';
import { ticketLink } from './tickets.js';

/** The payload's bands as `{id, label, start, end, ...}` in milliseconds. */
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
      stories: sprint.story_count || 0,
      points: sprint.points || 0,
      done: sprint.done || 0,
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
  if (data.active != null) bits.push(`active S${data.active}`);
  return bits.join(' · ');
}

function windowLabel(sprint) {
  const start = fmtDay(Date.parse(sprint.band_start));
  const end = fmtDay(Date.parse(sprint.band_end));
  return `${start} → ${end}`;
}

function carryCell(sprint) {
  const into = (sprint.carried_in || []).length;
  const out = (sprint.carried_out || []).length;
  if (!into && !out) return '';
  const parts = [];
  if (into) parts.push(`${into} in`);
  if (out) parts.push(`${out} out`);
  const keys = [...(sprint.carried_in || []), ...(sprint.carried_out || [])];
  return `<span class="sprint-carry" title="carried over: ${esc(keys.join(', '))}">↻ ${esc(parts.join(' · '))}</span>`;
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
    return `${ticketLink(row.key, { base })} <span class="muted mono">${esc(path)}</span>`;
  });
  return `<p class="sprint-carry-list">carried over: ${items.join(' · ')}${more}</p>`;
}

/** The sprint strip: one row per sprint, plus the out-of-scope and carry lists. */
export function sprintStrip(data, { base = '' } = {}) {
  if (!data || !data.available) return '';
  const sprints = data.sprints || [];
  const rows = sprints.map((sprint) => {
    const start = Date.parse(sprint.band_start);
    const end = Date.parse(sprint.band_end);
    const plan = sprint.open && sprint.planned_end
      ? `<span class="sprint-plan" title="the plan is not the band: an active sprint's band ends at the snapshot">plan ${esc(sprint.planned_end)}</span>`
      : '';
    const state = sprint.state || (sprint.open ? 'active' : 'closed');
    const days = sprint.days == null ? '' : `${sprint.days} d`;
    return `<button type="button" class="sprint-row${sprint.open ? ' open' : ''}"
        data-sprint-id="${esc(sprint.sprint_id)}"
        data-sprint-start="${Number.isFinite(start) ? start : ''}"
        data-sprint-end="${Number.isFinite(end) ? end : ''}"
        title="set the timeframe to sprint ${esc(sprint.sprint_id)} (${esc(windowLabel(sprint))})">
      <span class="sprint-id mono">S${esc(sprint.sprint_id)}</span>
      <span class="sprint-state">${esc(state)}</span>
      <span class="sprint-window mono">${esc(windowLabel(sprint))}</span>
      <span class="sprint-days num muted">${esc(days)}</span>
      <span class="sprint-scope-count num">${sprint.story_count || 0} stor${(sprint.story_count || 0) === 1 ? 'y' : 'ies'} · ${sprint.points || 0} pts</span>
      <span class="sprint-done num">${sprint.done || 0} done</span>
      ${carryCell(sprint)}
      ${plan}
    </button>`;
  }).join('');
  return `<div class="sprint-strip" role="group" aria-label="sprints">
    <div class="sprint-strip-head">
      <span class="sprint-strip-title">sprints</span>
      <span class="sprint-scope-line">${esc(sprintScopeLine(data))}</span>
      ${(data.carry_over && data.carry_over.count)
        ? '<span class="sprint-legend"><span class="sprint-carry">↻</span> carried over — marked on the story lane</span>'
        : ''}
      <span class="grow"></span>
      <span class="hint">click a sprint to set the timeframe to it</span>
    </div>
    <div class="sprint-rows">${rows}</div>
    ${carriedOver(data, base)}
    ${outOfScope(data, base)}
  </div>`;
}

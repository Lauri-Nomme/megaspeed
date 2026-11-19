/* Ticket detail page (prd/25): the whole cycle of one ticket.
 *
 * The timeline answers "where does the time go" per PR; this page answers it
 * per *ticket*: the eight segments (backlog → refinement → sprint → work →
 * review → merge → done, plus the prd/29 verification split when the workflow
 * has a QA stage) as one phase-coloured bar, the PRs that carry the ticket
 * beneath it, the sprints it was committed to, and the worklog-vs-active
 * hours. The one-line prose answer at the top is the point of the page: it
 * either says where the time went or names the boundary the store is missing
 * (prd/10 §7 — never an estimate standing in for a measurement).
 *
 * Pure markup: no fetch, no DOM. `main.js` owns the request and the route, so
 * every string here is testable with `node --test`.
 */

import { fmtAbsolute, esc } from './format.js';
import { PHASE } from './timeline.js';
import { ticketHref } from './tickets.js';
import { color, onPalette } from './theme.js';

/* The PR-gantt vocabulary (PHASE) covers waiting / active / review / closed;
 * the five story-only segments extend it with their own hues, in lifecycle
 * order. Colour still means something: neutral = backlog/settled, cyan =
 * scheduled, amber = committed but not started, purple = work, blues = review,
 * green = approval/merge. */
export const SEGMENT_PHASE = {
  // concepts.md §5: the first segment is created → ready; refinement is the
  // work inside it, so the short phase label is "refinement", never "backlog".
  created_ready: { token: '--cat-7', color: '', phase: 'waiting', label: 'refinement' },
  ready_sprint: { token: '--cat-2', color: '', phase: 'waiting', label: 'unscheduled' },
  sprint_in_progress: { token: '--cat-5', color: '', phase: 'waiting', label: 'committed' },
  in_progress_pr_open: { token: '--phase-active', color: '', phase: 'active', label: 'in progress' },
  pr_open_first_review: { token: '--phase-review', color: '', phase: 'review', label: 'in review' },
  review_rounds: { token: '--series-default', color: '', phase: 'review', label: 'review rounds' },
  final_approval_merge: { token: '--cat-1', color: '', phase: 'review', label: 'merge' },
  merge_done: { token: '--phase-unobserved', color: '', phase: 'closed', label: 'done' },
  // prd/29: the verification split, with the two workflow-stage hues from the
  // shared phase vocabulary (a merged PR waiting for QA, and the QA time).
  merge_verification: { token: '--phase-in-review', color: '', phase: 'review', label: 'awaiting verification' },
  verification_done: { token: '--phase-in-verification', color: '', phase: 'review', label: 'in verification' },
};

/** Resolve the segment hues from the theme tokens (dark fallbacks first). */
export function applyPalette() {
  for (const phase of Object.values(SEGMENT_PHASE)) phase.color = color(phase.token);
}
onPalette(applyPalette);

export function segmentPhase(key) {
  return SEGMENT_PHASE[key] || SEGMENT_PHASE.created_ready;
}

/** "45 h", "0 h", "—" — hours are the analysis unit, never a fake duration. */
export function segmentHours(hours) {
  if (hours == null) return '—';
  const value = Number(hours);
  if (!isFinite(value)) return '—';
  return `${Math.round(value * 10) / 10} h`;
}

function pct(share) {
  return share == null ? '—' : `${Math.round(Number(share) * 100)}%`;
}

function stamp(iso) {
  return iso ? fmtAbsolute(Date.parse(iso)) : '—';
}

function shortStamp(iso) {
  if (!iso) return '—';
  const ms = Date.parse(iso);
  const date = new Date(ms);
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${date.getUTCDate()} ${months[date.getUTCMonth()]} ${String(date.getUTCHours()).padStart(2, '0')}:${String(date.getUTCMinutes()).padStart(2, '0')}`;
}

/** The segment bar: one block per segment, absent blocks are hatched. */
export function segmentBar(payload) {
  const segments = (payload && payload.segments) || {};
  const rows = segments.rows || [];
  if (!rows.length) {
    return '<p class="empty-row">The store has no segment decomposition for this ticket.</p>';
  }
  const blocks = rows.map((row) => {
    const phase = segmentPhase(row.key);
    if (!row.available) {
      const boundary = (row.missing || [])[0] || 'boundary';
      return {
        key: row.key,
        absent: true,
        color: phase.color,
        grow: 0,
        title: `${row.label}: not measurable — the ${boundary} boundary is missing (${row.reason || 'no event'})`,
        text: 'not measured',
      };
    }
    const title = `${row.label} — ${segmentHours(row.hours)}` +
      ` (${pct(row.share)} of the measured cycle, ${pct(row.efficiency)} active)` +
      (row.no_pr_open_hours > 0
        ? ` · ${segmentHours(row.no_pr_open_hours)} with no PR open` : '') +
      (row.inverted ? ' · boundaries out of order, so no wait' : '');
    return {
      key: row.key,
      absent: false,
      color: phase.color,
      grow: Math.max(Number(row.hours) || 0, 0.01),
      title,
      text: '',
    };
  });
  const count = rows.length === 8 ? 'eight' : String(rows.length);
  const bar = `<div class="ticket-bar" role="img" aria-label="${count}-segment ticket cycle">${
    blocks.map((block) => `<span class="ticket-seg${block.absent ? ' absent' : ''}"
      data-segment="${esc(block.key)}" data-available="${String(!block.absent)}"
      style="${block.absent ? `flex:0 0 74px;--seg:${block.color}` : `flex:${block.grow} 1 0;background:${block.color}`}"
      title="${esc(block.title)}">${block.text ? `<span>${esc(block.text)}</span>` : ''}</span>`).join('')}</div>`;
  const legend = `<div class="ticket-seg-legend">${rows.map((row) => {
    const phase = segmentPhase(row.key);
    const value = row.available
      ? `<b>${esc(segmentHours(row.hours))}</b> <span class="muted">${pct(row.share)}</span>`
      : `<span class="muted">not measured</span>`;
    const swatch = row.available
      ? `<i style="background:${phase.color}"></i>`
      : `<i class="hatch" style="--c:${phase.color}"></i>`;
    return `<span class="ticket-seg-key${row.available ? '' : ' zero'}"
      title="${esc(row.available
        ? `${row.label} · ${pct(row.efficiency)} active${row.no_pr_open_hours > 0 ? ` · ${segmentHours(row.no_pr_open_hours)} with no PR open` : ''}`
        : row.reason || '')}">
      ${swatch}<span>${esc(row.label)}</span> ${value}</span>`;
  }).join('')}</div>`;
  return `${bar}${legend}`;
}

/** The boundary timestamps, with the missing ones named. */
export function boundaryChips(payload) {
  const boundaries = (payload && payload.segments && payload.segments.boundaries) || {};
  const entries = Object.entries(boundaries);
  if (!entries.length) return '';
  return `<div class="ticket-boundaries">${entries.map(([name, entry]) => (
    entry.available
      ? `<span class="ticket-boundary mono" title="the ${esc(name)} boundary">
          <span class="k">${esc(name)}</span>${esc(shortStamp(entry.ts))}</span>`
      : `<span class="ticket-boundary missing mono" title="${esc(entry.reason || 'missing')}">
          <span class="k">${esc(name)}</span>missing</span>`
  )).join('')}</div>`;
}

/** A mini lifecycle bar for one PR, in the PR-gantt phase colours. */
export function prPhaseBar(pr) {
  const intervals = (pr && pr.intervals) || [];
  const measured = intervals.filter((interval) => interval.measured && interval.hours > 0);
  if (!measured.length) return '<span class="muted">no measured interval</span>';
  const blocks = measured.map((interval) => {
    const phase = PHASE[interval.phase] || PHASE.unobserved;
    return `<span class="ticket-seg" style="flex:${Math.max(interval.hours, 0.01)} 1 0;background:${phase.color}"
      title="${esc(`${PHASE[interval.phase] ? PHASE[interval.phase].label : interval.phase} — ${segmentHours(interval.hours)}${interval.waited_on ? ` · ${interval.waited_on}` : ''}`)}"></span>`;
  }).join('');
  return `<span class="ticket-mini-bar" role="img" aria-label="PR lifecycle">${blocks}</span>`;
}

/** The ticket's PRs: state, cycle, review rounds, push classes, censoring. */
export function prTable(payload) {
  const prs = (payload && payload.prs) || [];
  if (!prs.length) {
    return `<p class="empty-row">No PR names this ticket in the store${
      payload && payload.available ? ' — the story has no code linked to it.' : '.'}</p>`;
  }
  const head = `<thead><tr>
    <th class="c-id">PR</th>
    <th class="c-state">State</th>
    <th class="c-since">Opened</th>
    <th class="c-dur num">Cycle</th>
    <th class="c-rounds num">Rounds</th>
    <th class="c-push">Push classes</th>
    <th class="c-bar">Lifecycle</th>
  </tr></thead>`;
  const body = prs.map((pr) => {
    const state = pr.censored
      ? `<span class="pill waiting">${esc(pr.state)} · censored</span>`
      : `<span class="pill ${pr.state === 'merged' ? 'healthy' : 'neutral'}">${esc(pr.state)}</span>`;
    const classes = Object.entries(pr.push_classes || {})
      .map(([name, count]) => `${esc(name)} ×${count}`).join(' · ') || '—';
    return `<tr class="pr-row" data-pr="${esc(pr.object_id)}"
        data-pr-ticket="${esc((payload && payload.key) || '')}"
        tabindex="0" role="button"
        title="open PR #${esc(pr.number)} ${esc(String(pr.repo || '').split('/').pop())} in the drawer"
        aria-label="open PR #${esc(pr.number)} ${esc(String(pr.repo || '').split('/').pop())} in the drawer">
      <td class="c-id" data-label=""><span class="mono id">#${esc(pr.number)}</span>
        <span class="repo">${esc(String(pr.repo || '').split('/').pop())}</span></td>
      <td class="c-state" data-label="state">${state}</td>
      <td class="c-since mono" data-label="opened">${esc(stamp(pr.opened))}</td>
      <td class="c-dur num" data-label="cycle">${esc(segmentHours(pr.cycle_hours))}</td>
      <td class="c-rounds num" data-label="rounds">${esc(pr.review_rounds)}</td>
      <td class="c-push mono" data-label="push classes">${classes}</td>
      <td class="c-bar" data-label="lifecycle">${prPhaseBar(pr)}</td>
    </tr>`;
  }).join('');
  return `<table class="table ticket-prs">${head}<tbody>${body}</tbody></table>`;
}

/** Sprint membership: which sprint, when, and the done-by-end outcome (prd/37). */
export function sprintStrip(payload) {
  const sprints = (payload && payload.sprints) || {};
  const memberships = sprints.memberships || [];
  if (!memberships.length) {
    return '<p class="empty-row">No sprint membership event names this ticket, so the store cannot say which sprint committed it.</p>';
  }
  const carried = sprints.carried_over
    ? `<span class="pill warning" title="committed to a closed sprint but not done by its close — the plan slipped">carried over${
      sprints.late ? ` · ${sprints.late} late` : ''}${
      sprints.never ? ` · ${sprints.never} open` : ''}</span>`
    : '';
  const OUTCOME = {
    by_end: ['healthy', 'done by end'],
    late: ['warning', 'finished late'],
    never: ['waiting', 'still open'],
    unknown: ['neutral', 'no sprint close'],
  };
  const chips = memberships.map((sprint) => {
    const [kind, label] = OUTCOME[sprint.outcome] || OUTCOME.unknown;
    return `
    <span class="sprint-chip" title="sprint ${esc(sprint.sprint_id)}${sprint.object_id ? ` · ${esc(sprint.object_id)}` : ''}">
      <b class="mono">#${esc(sprint.sprint_id)}</b>
      <span class="mono muted">${esc(shortStamp(sprint.start))} → ${esc(shortStamp(sprint.end || sprint.planned_end))}</span>
      <span class="pill ${kind}">${esc(label)}</span>
      <span class="pill neutral">${esc(sprint.state || 'unknown')}</span>
      <span class="muted">added ${esc(shortStamp(sprint.added))}${sprint.removed ? ` · removed ${esc(shortStamp(sprint.removed))}` : ''}</span>
    </span>`;
  }).join('');
  return `<div class="sprint-strip">${carried}${chips}</div>`;
}

/** Worklog hours vs the PR-side active hours, and how late the logs were. */
export function worklogBlock(payload) {
  const worklog = (payload && payload.worklog) || {};
  const logged = Number(worklog.logged_hours) || 0;
  const active = Number(worklog.active_hours) || 0;
  const ratio = worklog.logged_to_active_ratio;
  if (!worklog.worklogs) {
    return `<p class="empty-row">${esc(worklog.note || 'No worklog names this ticket.')}</p>`;
  }
  const max = Math.max(logged, active, 0.01);
  const buckets = worklog.buckets || {};
  // prd/36 §5: "logged a week late" is the `late` bucket (> 7 days);
  // within_week (2–7 d) is shown in the bucket line, never counted as it.
  const late = buckets.late || 0;
  const bucketText = Object.entries(buckets)
    .map(([name, count]) => `${esc(name.replace(/_/g, ' '))} ${count}`).join(' · ');
  return `
    <div class="worklog-bars">
      <div class="worklog-row"><span class="k">logged</span>
        <span class="worklog-track"><i style="width:${(logged / max) * 100}%;background:${color('--cat-2')}"></i></span>
        <b class="num">${esc(segmentHours(logged))}</b></div>
      <div class="worklog-row"><span class="k">active (PR)</span>
        <span class="worklog-track"><i style="width:${(active / max) * 100}%;background:${color('--cat-3')}"></i></span>
        <b class="num">${esc(segmentHours(active))}</b></div>
    </div>
    <dl class="facts">
      <dt>worklogs</dt><dd class="mono">${esc(worklog.worklogs)}</dd>
      <dt>logged ÷ active</dt><dd class="mono">${ratio == null ? '–' : esc(ratio)}</dd>
      <dt>logged a week late</dt><dd class="mono">${pct(worklog.late_share)}${late ? ` (${late})` : ''}</dd>
      <dt>log delay</dt><dd class="mono">${bucketText}</dd>
    </dl>`;
}

/** The story's own events (status / sprint / points / worklog), newest last. */
export function eventsList(payload, { limit = 40 } = {}) {
  const events = (payload && payload.events) || [];
  if (!events.length) {
    return '<p class="empty-row">No event names this ticket: the store has no story history for it.</p>';
  }
  const shown = events.slice(-limit);
  const rows = shown.map((event) => {
    const attrs = Object.entries(event.attributes || {})
      .map(([key, value]) => `${key}=${value}`).join(' ');
    return `<tr>
      <td class="mono c-since" data-label="when">${esc(stamp(event.ts))}</td>
      <td class="mono" data-label="activity">${esc(event.activity)}</td>
      <td class="mono c-who" data-label="actor">${esc(event.actor)}</td>
      <td class="mono muted c-attrs" data-label="attributes">${esc(attrs)}</td>
    </tr>`;
  }).join('');
  const more = events.length > shown.length
    ? `<p class="panel-note">showing the newest ${shown.length} of ${events.length} events</p>` : '';
  return `<table class="table compact ticket-events"><thead><tr>
      <th class="c-since">when</th><th>activity</th><th class="c-who">actor</th><th>attributes</th>
    </tr></thead><tbody>${rows}</tbody></table>${more}`;
}

/** The stats strip: counts before content (prd/10 §11). */
export function ticketStats(payload) {
  if (!payload) return '';
  const segments = payload.segments || {};
  const story = payload.story || {};
  const cycle = segments.cycle_hours;
  const largest = segments.largest_label;
  const total = (segments.rows || []).length;
  const reopened = payload.reopened || segments.reopened;
  return `<span class="stat-inline"><b>${cycle == null ? '–' : esc(segmentHours(cycle))}</b> cycle</span>
    <span class="stat-inline"><b>${segments.measured ?? 0}</b>/${total} segments</span>
    <span class="stat-inline"><b>${payload.pr_count ?? 0}</b> PR${payload.pr_count === 1 ? '' : 's'}</span>
    ${story.points != null ? `<span class="stat-inline"><b>${esc(story.points)}</b> points</span>` : ''}
    ${reopened ? `<span class="stat-inline"><b>reopened</b> ${segments.reopen_count ? `×${esc(segments.reopen_count)}` : ''}</span>` : ''}
    ${largest ? `<span class="stat-inline">most in <b>${esc(largest)}</b> (${pct(segments.largest_share)})</span>` : ''}`;
}

/**
 * prd/35 §2: state the clock once, where the numbers are.
 *
 * Timestamps are UTC; every duration is business hours on the team's calendar
 * (Tallinn by default), so a 0 h span between two UTC stamps an hour apart is
 * an evening or a weekend, not a bug.
 */
export function clockNote(payload) {
  if (!payload) return '';
  if (payload.hours_mode === 'business') {
    return 'timestamps are UTC · durations are business hours on the team calendar' +
      (payload.assumption ? ` (${payload.assumption})` : '') +
      ' — a 0 h span can be an evening, a weekend or a holiday';
  }
  return 'timestamps are UTC · no working-hours calendar is configured, so durations ' +
    'are wall-clock hours';
}

/** The whole page body. `base` is the page URL used by copied Markdown links. */
export function ticketMarkup(payload, { base = '' } = {}) {
  if (!payload) return '<p class="empty-row">Loading the ticket…</p>';
  const story = payload.story;
  const segments = payload.segments || {};
  const key = payload.key || '';
  const context = story
    ? [
      story.epic_key ? `epic ${story.epic_key}` : '',
      `assignee ${story.assignee || '—'}`,
      `created ${stamp(story.created)}`,
      story.closed ? `closed ${stamp(story.closed)}` : 'not closed',
      story.summary_class ? story.summary_class : '',
    ].filter(Boolean).join(' · ')
    : 'the store holds no story object for this key; the PR rows are the only evidence';
  const openPrs = payload.open_prs || [];
  const reopened = payload.reopened || segments.reopened;
  // prd/36 §3/§4: the end state and any censored PR are stated in the head, so
  // the headline can never look like a clean close while a PR is still open.
  const endPills = [
    reopened ? `<span class="pill warning" title="the story moved out of done and back">reopened${
      payload.reopen_count ? ` ×${esc(payload.reopen_count)}` : ''}</span>` : '',
    ...openPrs.map((pr) => `<span class="pill waiting" title="still open at the snapshot — right-censored">PR #${esc(pr.number)} open · right-censored</span>`),
  ].filter(Boolean).join(' ');
  return `
    <div class="ticket-head">
      <span class="overline">TICKET</span>
      <h2 class="mono">${esc(key)}</h2>
      <p class="context">${esc(story
        ? `${story.state || 'unknown state'}${story.censored ? ' · still open' : ''}`
        : 'no STORY object')}</p>
      ${endPills ? `<p class="context">${endPills}</p>` : ''}
      <p class="context muted">${esc(context)}</p>
      <p class="clock-note" id="ticket-clock">${esc(clockNote(payload))}</p>
    </div>
    <p class="impact ticket-answer" id="ticket-answer" data-kind="${esc(payload.answer_kind || '')}">${esc(payload.answer || '')}</p>
    <div class="ticket-grid">
      <section class="ticket-panel wide">
        <h2 class="policy-h">cycle · ${(segments.rows || []).length} segments</h2>
        ${segmentBar(payload)}
        ${boundaryChips(payload)}
        <p class="panel-note">${esc(segments.heuristic || '')}${segments.hours_mode === 'business' ? '' : ' · wall-clock hours (no calendar configured)'}</p>
      </section>
      <section class="ticket-panel">
        <h2 class="policy-h">sprints</h2>
        ${sprintStrip(payload)}
        <h2 class="policy-h">worklog vs active</h2>
        ${worklogBlock(payload)}
      </section>
    </div>
    <section class="ticket-panel">
      <h2 class="policy-h">pull requests · ${payload.pr_count ?? 0}</h2>
      ${prTable(payload)}
    </section>
    <section class="ticket-panel">
      <h2 class="policy-h">ticket events · ${payload.event_count ?? 0}</h2>
      ${eventsList(payload)}
    </section>`;
}

/** Copy-as-Markdown: the same answer, with ticket keys as clickable links. */
export function ticketMarkdown(payload, base = '') {
  if (!payload) return '';
  const story = payload.story || {};
  const segments = payload.segments || {};
  const key = payload.key || '';
  const lines = [
    `# ${key} — why did it take this long?`,
    '',
    `> ${payload.answer || ''}`,
    '',
    `_${payload.hours_mode === 'business' ? 'business hours' : 'wall-clock hours'} · ` +
      `timestamps UTC · snapshot ${payload.snapshot || '—'} · [open in the app](${ticketHref(key, base)})_`,
    ...(payload.reopened
      ? ['', `**reopened** ×${payload.reopen_count || 1} — created→first done ` +
        `${segmentHours(segments.cycle_first_done_hours)}, created→final done ` +
        `${segmentHours(segments.cycle_hours)}`]
      : []),
    ...((payload.open_prs || []).length
      ? ['', `**still open:** ${(payload.open_prs || []).map((pr) => `#${pr.number} (right-censored)`).join(', ')}`]
      : []),
    '',
    '| segment | hours | share | active |',
    '| --- | --- | --- | --- |',
    ...(segments.rows || []).map((row) => `| ${row.label} | ${
      row.available ? segmentHours(row.hours) : 'not measured'} | ${
      row.available ? pct(row.share) : '—'} | ${
      row.available ? pct(row.efficiency) : '—'} |`),
    '',
    `story: ${story.points == null ? '—' : `${story.points} points`} · ` +
      `${story.epic_key ? `epic ${story.epic_key}` : 'no epic'} · ` +
      `${story.assignee || 'no assignee'} · created ${story.created || '—'}`,
    '',
    `## pull requests (${payload.pr_count || 0})`,
    '',
    ...(payload.prs || []).map((pr) => `- #${pr.number} ${pr.repo} · ${pr.state}` +
      `${pr.censored ? ' (right-censored)' : ''} · ${segmentHours(pr.cycle_hours)} · ` +
      `${pr.review_rounds} review round${pr.review_rounds === 1 ? '' : 's'} · ` +
      `pushes ${Object.entries(pr.push_classes || {}).map(([name, n]) => `${name}×${n}`).join(', ') || 'none'}`),
    '',
    `## sprints`,
    '',
    ...((payload.sprints && payload.sprints.memberships) || []).map((sprint) =>
      `- #${sprint.sprint_id} · ${sprint.state || 'unknown'} · added ${sprint.added || '—'}` +
      `${sprint.removed ? ` · removed ${sprint.removed}` : ''}`),
    '',
    `## worklog vs active`,
    '',
    `- logged ${segmentHours(payload.worklog && payload.worklog.logged_hours)} in ` +
      `${payload.worklog ? payload.worklog.worklogs : 0} worklog${payload.worklog && payload.worklog.worklogs === 1 ? '' : 's'}`,
    `- active PR time ${segmentHours(payload.worklog && payload.worklog.active_hours)}`,
    `- logged ÷ active ${payload.worklog && payload.worklog.logged_to_active_ratio != null
      ? payload.worklog.logged_to_active_ratio : '—'}`,
  ];
  return lines.join('\n');
}

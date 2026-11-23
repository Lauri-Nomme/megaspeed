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

import { fmtAbsolute, fmtHours, fmtCalendarDuration, esc } from './format.js';
import { PHASE } from './timeline.js';
import { ticketHref } from './tickets.js';
import { color, onPalette } from './theme.js';
import { benchmarkInline, benchmarkText } from './benchmark.js';

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
  // prd/63 V-C6: the wait before anyone was asked to review is its own
  // interval — review queuing, not review time.
  pr_open_reviewer_added: { token: '--cat-4', color: '', phase: 'review', label: 'awaiting a reviewer' },
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

/**
 * "3 d 0 h", "45 min", "—" — the one duration language (prd/63 V-B).
 *
 * The page used to print "73.3 h" beside "21 d 0 h" and "192 h" for the same
 * PR the header priced as "24 d 0 h". Every business duration goes through
 * `fmtHours`, so the ticket can never use a second unit.
 */
export function segmentHours(hours) {
  if (hours == null) return '—';
  const value = Number(hours);
  if (!isFinite(value)) return '—';
  return fmtHours(value);
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
const MATERIAL_GAP_HOURS = 8;

/**
 * prd/62: this story's four-step pass, in the ticket drawer.
 *
 * `%C&A` is a *measured* fact about the story (which transitions happened), so
 * the line says "clean" or "bounced" per step and never a rate — one story is
 * not a cohort. The rolled answer is a sentence.
 */
export function yieldPassLine(payload) {
  const pass = payload && payload.yield;
  if (!pass || !(pass.steps || []).length) return '';
  const steps = pass.steps.map((step) => `<span class="pass-step ${
    step.available === false ? 'unmeasured' : step.clean ? 'clean' : 'bounced'}"
    data-pass-step="${esc(step.id)}"
    title="${esc(`${step.label}: ${(step.signals || []).join(', ')}${step.note ? ` — ${step.note}` : ''}`)}">
    <span class="pass-dot" aria-hidden="true"></span>${esc(step.label)} ${
    step.available === false ? 'not measured' : step.clean ? 'clean' : 'bounced'}</span>`).join('');
  const rolled = pass.available === false
    ? '— not measurable for this story'
    : pass.rolled_clean
      ? '— clean at every step'
      : '— a loop-back somewhere';
  return `<p class="ticket-pass" id="ticket-pass" data-rolled-clean="${pass.rolled_clean ? 'true' : 'false'}">
    <span class="overline">first pass</span> ${steps}
    <span class="muted">${rolled}</span></p>`;
}

/** What a segment's hours were *not*: a no-PR gap or a reopen loop (V-A7). */
export function segmentCaveat(row) {
  if (!row) return '';
  const bits = [];
  if ((row.no_pr_open_hours || 0) >= MATERIAL_GAP_HOURS) {
    bits.push(`${segmentHours(row.no_pr_open_hours)} with no PR open`);
  }
  if ((row.rework_hours || 0) > 0) {
    bits.push(`${segmentHours(row.rework_hours)} is the reopen loop`);
  }
  return bits.join(' · ');
}

export function segmentBar(payload, { benchmark = true } = {}) {
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
    // prd/57: the segment's hours and its cohort median travel together — the
    // median uses the same business-hour renderer, so "20 h" and "median 10 h"
    // cannot be read on two different clocks.
    const contrast = benchmark ? benchmarkText(row.benchmark, { format: segmentHours }) : '';
    const caveat = segmentCaveat(row);
    const title = `${row.label} — ${segmentHours(row.hours)}` +
      (contrast ? ` (${contrast})` : '') +
      ` (${pct(row.share)} of the measured cycle, ${pct(row.efficiency)} active)` +
      (caveat ? ` · ${caveat}` : '') +
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
      ? `<b>${esc(segmentHours(row.hours))}</b> <span class="muted">${pct(row.share)}</span>${
        benchmarkInline(row.benchmark, { enabled: benchmark, format: segmentHours })}`
      : `<span class="muted">not measured</span>`;
    const swatch = row.available
      ? `<i style="background:${phase.color}"></i>`
      : `<i class="hatch" style="--c:${phase.color}"></i>`;
    const caveat = row.available ? segmentCaveat(row) : '';
    return `<span class="ticket-seg-key${row.available ? '' : ' zero'}"
      title="${esc(row.available
        ? `${row.label} · ${pct(row.efficiency)} active${caveat ? ` · ${caveat}` : ''}`
        : row.reason || '')}">
      ${swatch}<span>${esc(row.label)}</span> ${value}${
        caveat ? `<span class="seg-caveat">${esc(caveat)}</span>` : ''}</span>`;
  }).join('')}</div>`;
  return `${bar}${legend}`;
}

/**
 * The boundary timestamps, with the missing ones named.
 *
 * prd/63 F17: the chips are drawn in **clock order**, not lifecycle order, and
 * a boundary the store dated before its lifecycle predecessor carries a `!`
 * with the pair named. DEL-183 lists `READY 13:00` before `REFINED 13:20`;
 * the old lifecycle order showed the later stamp first and asserted a sequence
 * the events do not support.
 */
export function boundaryChips(payload) {
  const segments = (payload && payload.segments) || {};
  const boundaries = segments.boundaries || {};
  if (!Object.keys(boundaries).length) return '';
  const order = Array.isArray(segments.boundary_order) && segments.boundary_order.length
    ? segments.boundary_order
    : Object.keys(boundaries);
  return `<div class="ticket-boundaries">${order.map((name) => {
    const entry = boundaries[name] || {};
    // prd/64 F-21: the chip and its tooltip read like English ("PR OPEN",
    // "IN VERIFICATION"); the raw key stays in `data-boundary` for selectors,
    // and the uppercase styling remains CSS (text-transform), not the data.
    const label = name.replace(/_/g, ' ');
    if (!entry.available) {
      return `<span class="ticket-boundary missing mono" title="${esc(entry.reason || 'missing')}">
        <span class="k">${esc(label)}</span>missing</span>`;
    }
    const quirk = entry.out_of_order || entry.order_quirk;
    const quirkText = entry.out_of_order
      ? `data quirk: the raw ${label} boundary is ${entry.raw_ts ? shortStamp(entry.raw_ts) : 'out of order'} (${entry.out_of_order.reason}), clamped to the cycle`
      : (entry.order_quirk
        ? `data quirk: ${label} is dated after ${entry.order_quirk.after} on the clock but follows it in the lifecycle — shown in clock order, not reordered`
        : `the ${label} boundary`);
    return `<span class="ticket-boundary mono${quirk ? ' quirk' : ''}" data-boundary="${esc(name)}"
        data-order-quirk="${entry.order_quirk ? 'true' : 'false'}"
        title="${esc(quirkText)}">
      <span class="k">${esc(label)}</span>${esc(shortStamp(entry.ts))}${quirk ? ' <span class="quirk-mark">!</span>' : ''}</span>`;
  }).join('')}</div>`;
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
function afterCloseText(pr) {
  // prd/64 F-14: a PR opened after Jira closed the ticket cannot have been part
  // of the delivered story — the same finding as the done-open panel.
  return pr && pr.opened_after_close
    ? `opened ${segmentHours(pr.opened_after_close_hours)} after the ticket closed — the work cannot have been part of the delivered story`
    : '';
}

function approvedUnmergedText(pr) {
  // prd/64 F-15: every reviewer approved and the PR closed unmerged (DEL-161
  // #34) — the delivery the story lost, never hidden on the ticket page.
  const finding = pr && pr.approved_unmerged;
  if (!finding || !(finding.approved_by || []).length) return '';
  const who = finding.approved_by.join(' + ');
  const closer = finding.closed_by
    ? `closed unmerged by ${finding.bot_closed ? `the bot ${finding.closed_by}` : finding.closed_by}`
    : 'closed unmerged after approval';
  return `approved by ${who}, ${closer} — ${segmentHours(finding.hours)} lost`;
}

export function prFindingText(pr) {
  return [afterCloseText(pr), approvedUnmergedText(pr)].filter(Boolean);
}

/**
 * The findings as a full-width row under their PR, so the sentence is visible
 * (a 190 px identity cell cannot hold it) and only rows that have one pay for
 * the extra line. The compact pill names the state; the sentence states it.
 */
export function prFindingRow(pr, payload) {
  const after = afterCloseText(pr);
  const approved = approvedUnmergedText(pr);
  if (!after && !approved) return '';
  const badges = [
    after ? '<span class="pill waiting">opened after the ticket closed</span>' : '',
    approved ? '<span class="pill blocked">approved, never merged</span>' : '',
  ].filter(Boolean).join(' ');
  return `<tr class="pr-finding-row" data-pr="${esc(pr.object_id)}"
      data-pr-ticket="${esc((payload && payload.key) || '')}"
      ${after ? 'data-pr-after-close="true"' : ''}
      ${approved ? 'data-pr-approved-unmerged="true"' : ''}>
    <td colspan="7" class="c-attrs">${badges}
      <span class="pr-finding">${esc([after, approved].filter(Boolean).join(' · '))}</span></td>
  </tr>`;
}

export function prTable(payload, { benchmark = true } = {}) {
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
      <td class="c-dur num" data-label="cycle">${esc(segmentHours(pr.cycle_hours))}${
        benchmarkInline(pr.cycle_benchmark, { enabled: benchmark, format: segmentHours })}${
        pr.cycle_benchmark && pr.cycle_benchmark.declined
          ? ` <span class="muted" title="${esc(pr.cycle_benchmark.note || '')}">declined</span>` : ''}</td>
      <td class="c-rounds num" data-label="rounds">${esc(pr.review_rounds)}</td>
      <td class="c-push mono" data-label="push classes">${classes}</td>
      <td class="c-bar" data-label="lifecycle">${prPhaseBar(pr)}</td>
    </tr>${prFindingRow(pr, payload)}`;
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
    // prd/43 §5: Jira says done by the close, but a PR the story owns never
    // merged — the same fact the delivery list calls "Done in Jira, PR open".
    unmerged: ['warning', 'done in Jira · PR unmerged'],
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

const ATTR_LABELS = {
  actor_kind: 'by',
  from_status: 'from',
  to_status: 'to',
  points: 'estimate',
  sprint_id: 'sprint',
  time_spent_seconds: 'time spent',
  log_delay_seconds: 'logged late by',
  log_delay_bucket: 'log delay',
  push_class: 'push',
  at_start: 'at sprint start',
  sentinel: 'sentinel',
  cause: 'cause',
  summary_class: 'change kind',
};

/** prd/63 F17/V-B: attributes are labelled facts, never a raw `k=v` dump. */
export function attributeFacts(attributes) {
  return Object.entries(attributes || {}).map(([key, value]) => {
    const label = ATTR_LABELS[key] || key.replace(/_/g, ' ');
    let text = String(value);
    if (key === 'time_spent_seconds') {
      text = segmentHours(Number(value) / 3600);
    } else if (key === 'log_delay_seconds') {
      // prd/64 F-22: the bucket is calendar days (`within_week` = at most 7
      // calendar days), so the duration is a *calendar* duration. Formatting
      // the same 76 h on the 8-business-hour "d" clock printed "9 d 4 h"
      // beside "within week" — a contradiction the pair never had.
      text = fmtCalendarDuration(Number(value) * 1000);
    } else if (key === 'actor_kind') {
      text = value === 'bot' ? 'a bot' : 'a human';
    } else if (key === 'log_delay_bucket') {
      text = String(value).replace(/_/g, ' ');
    } else if (typeof value === 'boolean') {
      text = value ? 'yes' : 'no';
    }
    return { key, label, text };
  });
}

/** The story's own events (status / sprint / points / worklog), newest last. */
export function eventsList(payload, { limit = 40 } = {}) {
  const events = (payload && payload.events) || [];
  if (!events.length) {
    return '<p class="empty-row">No event names this ticket: the store has no story history for it.</p>';
  }
  const shown = events.slice(-limit);
  const rows = shown.map((event) => {
    const facts = attributeFacts(event.attributes)
      .map(({ key, label, text }) =>
        `<span class="event-attr" data-attr="${esc(key)}"><span class="k">${esc(label)}</span>
          <span class="v mono">${esc(text)}</span></span>`)
      .join(' ');
    return `<tr>
      <td class="mono c-since" data-label="when">${esc(stamp(event.ts))}</td>
      <td class="mono" data-label="activity">${esc(event.activity)}</td>
      <td class="mono c-who" data-label="actor">${esc(event.actor)}</td>
      <td class="c-attrs" data-label="attributes">${facts || '<span class="muted">—</span>'}</td>
    </tr>`;
  }).join('');
  const more = events.length > shown.length
    ? `<p class="panel-note">showing the newest ${shown.length} of ${events.length} events</p>` : '';
  return `<table class="table compact ticket-events"><thead><tr>
      <th class="c-since">when</th><th>activity</th><th class="c-who">actor</th><th>attributes</th>
    </tr></thead><tbody>${rows}</tbody></table>${more}`;
}

/** The stats strip: counts before content (prd/10 §11). */
export function ticketStats(payload, { benchmark = true } = {}) {
  if (!payload) return '';
  if (payload.kind === 'epic') {
    const epic = payload.epic || {};
    return `<span class="stat-inline"><b>epic</b> not a story</span>
      <span class="stat-inline"><b>${epic.story_count || 0}</b> stor${epic.story_count === 1 ? 'y' : 'ies'}</span>
      <span class="stat-inline"><b>${epic.points || 0}</b> points</span>
      <span class="stat-inline"><b>${epic.open_stories || 0}</b> still open</span>`;
  }
  const segments = payload.segments || {};
  const story = payload.story || {};
  const cycle = segments.cycle_hours;
  const largest = segments.largest_label;
  const total = (segments.rows || []).length;
  const measured = Number(segments.measured ?? 0);
  const reopened = payload.reopened || segments.reopened;
  const change = payload.change || {};
  const quirk = payload.quirk || {};
  // prd/63 F17: a tenant with no STORY object (del3) showed "0/8 segments"
  // beside a merged PR. Say what the store does have — the PR-side cycles —
  // instead of counting story segments that cannot exist.
  if (!payload.story) {
    const prs = payload.prs || [];
    return `<span class="stat-inline"><b>no STORY object</b> in this dataset</span>
      <span class="stat-inline"><b>${prs.length}</b> PR${prs.length === 1 ? '' : 's'} measured</span>
      ${prs.map((pr) => `<span class="stat-inline">#${esc(pr.number)} ${esc(pr.state)}${
        pr.cycle_hours == null ? '' : ` · ${esc(segmentHours(pr.cycle_hours))}`}${
        pr.censored ? ' <span class="muted">right-censored</span>' : ''}</span>`).join('')}`;
  }
  // prd/64 F-15: an open story's `cycle_hours` is its age to the snapshot, so
  // the strip calls it age and prints the measured sum separately. The largest
  // segment's share is a share of that measured sum — printing "(30%)" beside
  // "32 d 4 h" read as 30 % of the age. One denominator per number.
  const open = Boolean(story.censored
    || (segments.boundaries && segments.boundaries.done
      && !segments.boundaries.done.ts));
  const partial = measured < total;
  const measuredStat = (open || partial)
    ? `<span class="stat-inline"><b>${segments.total_hours == null
      ? '–' : esc(segmentHours(segments.total_hours))}</b> measured across <b>${measured}</b>/${total} segments</span>`
    : `<span class="stat-inline"><b>${measured}</b>/${total} segments</span>`;
  const shareLabel = open || partial
    ? `${pct(segments.largest_share)} of measured`
    : pct(segments.largest_share);
  return `<span class="stat-inline"><b>${cycle == null ? '–' : esc(segmentHours(cycle))}</b> ${open ? 'age (open)' : 'cycle'}</span>
    ${benchmarkLine(payload, { benchmark })}
    ${change.unmerged_share
      ? `<span class="stat-inline attention" data-unmerged-share="${esc(change.unmerged_share)}"
           title="${esc(`lines changed in open PRs ÷ lines changed in all of this story's PRs — ${change.lines_unmerged} of ${change.lines_total} changed lines`)}">
           <b>${Math.round(change.unmerged_share * 100)}%</b> of the change unmerged</span>`
      : ''}
    ${measuredStat}
    <span class="stat-inline"><b>${payload.pr_count ?? 0}</b> PR${payload.pr_count === 1 ? '' : 's'}</span>
    ${story.points != null ? `<span class="stat-inline"><b>${esc(story.points)}</b> points</span>` : ''}
    ${reopened ? `<span class="stat-inline"><b>reopened</b> ${segments.reopen_count ? `×${esc(segments.reopen_count)}` : ''}</span>` : ''}
    ${largest ? `<span class="stat-inline">most in <b>${esc(largest)}</b> (${shareLabel})${
      (segments.largest_no_pr_open_hours || 0) >= MATERIAL_GAP_HOURS
        ? ` — ${esc(segmentHours(segments.largest_no_pr_open_hours))} with no PR open` : ''}</span>` : ''}
    ${quirk.count
      ? `<span class="stat-inline quirk-stat" data-quirk="${quirk.count}"
           title="${esc(`out-of-order boundaries: ${(quirk.clamped || []).join(', ') || '—'}${quirk.order && quirk.order.length ? `; ${quirk.order.map((item) => `${item.boundary} after ${item.after}`).join('; ')}` : ''}`)}">
           <b>${quirk.count}</b> boundary quirk${quirk.count === 1 ? '' : 's'}</span>`
      : ''}`;
}

/**
 * prd/42 M2: the cohort benchmark next to the cycle headline.
 *
 * "73 h" is read against its cohort: "73rd of 87 fastest · median 89.7 h ·
 * 8-pt median 109 h". The same-points median is the comparison a lead makes
 * ("is this slow for an 8?"), and a censored story says so rather than
 * implying a finished cycle.
 */
export function benchmarkLine(payload, { benchmark = true } = {}) {
  const benchmarkData = payload && payload.benchmark;
  if (!benchmarkData || !benchmarkData.available) return '';
  const ordinal = (n) => {
    const mod = n % 100;
    if (mod >= 11 && mod <= 13) return `${n}th`;
    return `${n}${['th', 'st', 'nd', 'rd'][n % 10] || 'th'}`;
  };
  // prd/46 §6: rank n of n is the slowest story, not an "87th fastest" —
  // name the fact rather than a rank that reads as praise.
  // prd/63 V-A6: a Done-with-open-PR story's cycle is measured to Jira's done,
  // not to the merge. The rank is still a fact about that clock — say which.
  const unmerged = payload && payload.change && payload.change.unmerged_share;
  const position = benchmarkData.rank === benchmarkData.n
    ? `slowest of ${benchmarkData.n}`
    : `${ordinal(benchmarkData.rank)} fastest of ${benchmarkData.n}`;
  const positionText = unmerged
    ? `${position} to Jira done`
    : position;
  const contrast = benchmark
    ? benchmarkText(benchmarkData.contrast, { format: segmentHours })
    : '';
  const parts = [
    positionText,
    // prd/57: when the contrast is on it already carries "median …"; otherwise
    // the plain median still answers "how does this compare".
    contrast || (benchmarkData.median_hours == null
      ? ''
      : `median ${segmentHours(benchmarkData.median_hours)}`),
    benchmarkData.points_median_hours == null || benchmarkData.points == null
      ? ''
      : `${benchmarkData.points}-pt median ${segmentHours(benchmarkData.points_median_hours)}`,
  ].filter(Boolean);
  const title = `${benchmarkData.basis || ''}${benchmarkData.censored
    ? ' · this story is still open, so its cycle is right-censored at the snapshot'
    : ''}${unmerged
    ? ` · the cycle is measured to the ticket's done, not to its unmerged PR (${Math.round(unmerged * 100)} % of the change), so this is not a delivery rank`
    : ''}`;
  return `<span class="stat-inline benchmark" data-benchmark-rank="${benchmarkData.rank}"
      title="${esc(title)}">${esc(parts.join(' · '))}</span>`;
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

/**
 * prd/42 P9: an epic is a parent grouping, not a story.
 *
 * The old page rendered an epic as a story with "0/9 segments", which read as
 * a broken ticket. The epic page now says what it is, lists the child stories
 * with their own cycles, and links each one — a doorway, not a dead end.
 */
export function epicMarkup(payload, { base = '', benchmark = true } = {}) {
  const epic = payload.epic || {};
  const children = epic.stories || [];
  const rows = children.map((story) => `<tr>
      <td class="c-id" data-label="story">${ticketHref(story.key, base)
        ? `<a class="ticket-link mono" href="${esc(ticketHref(story.key, base))}" data-ticket="${esc(story.key)}">${esc(story.key)}</a>`
        : `<span class="mono">${esc(story.key)}</span>`}</td>
      <td class="c-state" data-label="state"><span class="pill ${story.censored ? 'waiting' : 'healthy'}">${esc(story.state || 'unknown')}${story.censored && story.state !== 'open' ? ' · open' : ''}</span></td>
      <td class="c-dur num" data-label="points">${story.points == null ? '—' : esc(story.points)}</td>
      <td class="c-dur num" data-label="cycle">${story.cycle_hours == null ? '—' : esc(segmentHours(story.cycle_hours))}${
        benchmarkInline(story.cycle_benchmark, { enabled: benchmark, format: segmentHours })}</td>
    </tr>`).join('');
  return `
    <div class="ticket-head">
      <span class="overline">EPIC</span>
      <h2 class="mono">${esc(payload.key || '')}</h2>
      <p class="context">${esc(epic.state || 'unknown state')} · a parent grouping of ${epic.story_count || 0} stor${epic.story_count === 1 ? 'y' : 'ies'}${epic.points ? ` · ${epic.points} points` : ''}</p>
      <p class="clock-note">an epic has no created → done cycle of its own; the cycle belongs to each child story</p>
    </div>
    <p class="impact ticket-answer" id="ticket-answer" data-kind="epic">${esc(payload.answer || '')}</p>
    <section class="ticket-panel">
      <h2 class="policy-h">child stories · ${children.length}</h2>
      ${rows ? `<table class="table compact ticket-epic">
        <thead><tr><th class="c-id">story</th><th class="c-state">state</th>
          <th class="c-dur num">points</th><th class="c-dur num">cycle</th></tr></thead>
        <tbody>${rows}</tbody></table>`
    : '<p class="empty-row">No story in this store names this epic, so there is nothing to drill into.</p>'}
    </section>`;
}

/** The open PR's current wait + approval position, for a chip or an export. */
export function openPrState(pr) {
  if (!pr) return '';
  const bits = [];
  if (pr.reviewers_total) {
    bits.push(`${pr.reviewers_approved || 0}/${pr.reviewers_total} approved`);
  }
  if (pr.waiting_hours != null) {
    const who = (pr.waiting_on || []).join(' + ') || 'nobody';
    bits.push(`waiting ${fmtHours(pr.waiting_hours)} on ${who}`);
  }
  return bits.length ? bits.join(' · ') : 'right-censored';
}

/** The whole page body. `base` is the page URL used by copied Markdown links. */
export function ticketMarkup(payload, { base = '', benchmark = true } = {}) {
  if (!payload) return '<p class="empty-row">Loading the ticket…</p>';
  if (payload.kind === 'epic') return epicMarkup(payload, { base, benchmark });
  const story = payload.story;
  const segments = payload.segments || {};
  const key = payload.key || '';
  const context = story
    ? [
      story.epic_key ? `epic ${story.epic_key}` : '',
      `assignee ${story.assignee || '—'}`,
      `created ${stamp(story.created)}`,
      story.closed ? `closed ${stamp(story.closed)}` : 'not closed',
      // prd/63 V-B: `· other` was an unlabelled closed-vocabulary value.
      story.summary_class ? `change kind ${story.summary_class}` : '',
    ].filter(Boolean).join(' · ')
    : 'the store holds no story object for this key; the PR rows are the only evidence';
  const openPrs = payload.open_prs || [];
  const reopened = payload.reopened || segments.reopened;
  const change = payload.change || {};
  // prd/36 §3/§4: the end state and any censored PR are stated in the head, so
  // the headline can never look like a clean close while a PR is still open.
  // prd/63 V-A6: the unmerged share of the *change* is stated too.
  const endPills = [
    reopened ? `<span class="pill warning" title="the story moved out of done and back">reopened${
      payload.reopen_count ? ` ×${esc(payload.reopen_count)}` : ''}</span>` : '',
    ...openPrs.map((pr) => `<span class="pill waiting" title="still open at the snapshot — right-censored">PR #${esc(pr.number)} open · ${esc(openPrState(pr))}</span>`),
    change.unmerged_share
      ? `<span class="pill blocked" data-change-unmerged="${esc(change.unmerged_share)}"
           title="${esc(`${change.lines_unmerged} of ${change.lines_total} changed lines are in an open PR — the ticket is Done, the change is not`)}">
           ${Math.round(change.unmerged_share * 100)} % of the change unmerged</span>`
      : '',
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
    ${yieldPassLine(payload)}
    <nav class="ticket-links" aria-label="this ticket elsewhere">
      <span class="overline">this ticket elsewhere</span>
      <button type="button" class="chip" data-ticket-open="timeline">Timeline</button>
      <button type="button" class="chip" data-ticket-open="delivery">Delivery rows</button>
      <button type="button" class="chip" data-ticket-open="blocking">Blocking map</button>
      ${(payload.prs || []).map((pr) => `<button type="button" class="chip ticket-pr-link"
        data-ticket-pr-open="${esc(pr.object_id)}"
        title="open PR #${esc(pr.number)} in the drawer">#${esc(pr.number)} ${esc(String(pr.repo || '').split('/').pop())}</button>`).join('')}
    </nav>
    <div class="ticket-grid">
      <section class="ticket-panel wide">
        <h2 class="policy-h">${payload.story
          ? `cycle · ${(segments.rows || []).length} segments`
          : 'lifecycle · no story history'}</h2>
        ${payload.story
          ? `${segmentBar(payload, { benchmark })}
             ${boundaryChips(payload)}
             <p class="panel-note">${esc(segments.heuristic || '')}${segments.hours_mode === 'business' ? '' : ' · wall-clock hours (no calendar configured)'}${
               payload.active ? ` Active work: ${esc(segmentHours(payload.active.segment_hours))} inside the segments (${esc(payload.active.note)}), ${esc(segmentHours(payload.active.pr_hours))} on the PR clock.` : ''}${
               (segments.rework_segments || []).length
                 ? ` The reopen loop is priced inside ${esc(segments.rework_segments.map((item) => item.label).join(', '))} (${esc(segmentHours(segments.rework_hours))}).`
                 : ''}</p>`
          : `<p class="empty-row">This dataset has no Jira story history for ${esc(key)}, so the eight story
               segments cannot exist — the PR rows below are the code's own measured lifecycle.
               That is why PR-side boundaries read “missing” here while a merged PR is listed.</p>`}
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
      ${prTable(payload, { benchmark })}
    </section>
    <section class="ticket-panel">
      <h2 class="policy-h">ticket events · ${payload.event_count ?? 0}</h2>
      ${eventsList(payload)}
    </section>`;
}

/**
 * Copy-as-Markdown for an epic (prd/68 F-12).
 *
 * The story template below describes a created → done cycle an epic does not
 * have, and its worklog/sprint sections were empty placeholders ("logged — in
 * undefined worklogs"). An epic exports its own header and the child table the
 * page shows, so the copy is the page.
 */
export function epicMarkdown(payload, base = '') {
  const epic = payload.epic || {};
  const key = payload.key || epic.key || '';
  const children = epic.stories || [];
  const count = epic.story_count ?? children.length;
  const state = epic.state || 'unknown state';
  const points = epic.points == null ? '—' : epic.points;
  const childRows = children.map((story) => `| ${
    ticketHref(story.key, base)
      ? `[${story.key}](${ticketHref(story.key, base)})`
      : story.key} | ${story.state || 'unknown'}${
    story.censored && story.state !== 'open' ? ' · open' : ''} | ${
    story.points == null ? '—' : story.points} | ${
    story.cycle_hours == null ? '—' : segmentHours(story.cycle_hours)} |`);
  return [
    `# ${key} — epic`,
    '',
    `> ${payload.answer || `${key} is an epic — a parent grouping of ${count} stories.`}`,
    '',
    `_${payload.hours_mode === 'business' ? 'business hours' : 'wall-clock hours'} · ` +
      `timestamps UTC · snapshot ${payload.snapshot || '—'} · [open in the app](${ticketHref(key, base)})_`,
    '',
    '| epic | state | stories | points |',
    '| --- | --- | --- | --- |',
    `| ${key} | ${state} | ${count} | ${points} |`,
    '',
    `## child stories (${count})`,
    '',
    ...(children.length
      ? [
        '| story | state | points | cycle |',
        '| --- | --- | --- | --- |',
        ...childRows,
      ]
      : ['No story in this store names this epic, so there is nothing to drill into.']),
    '',
  ].join('\n');
}

/** Copy-as-Markdown: the same answer, with ticket keys as clickable links. */
export function ticketMarkdown(payload, base = '') {
  if (!payload) return '';
  if (payload.kind === 'epic') return epicMarkdown(payload, base);
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
    ...(payload.yield && (payload.yield.steps || []).length
      ? ['', `**first pass:** ${payload.yield.steps.map((step) =>
        `${step.label.toLowerCase()} ${step.available === false
          ? 'not measured' : step.clean ? 'clean' : 'bounced'}`).join(' · ')}` +
        ` — ${(payload.yield.steps || []).some((step) => step.available === false)
          ? 'not measurable for this story'
          : payload.yield.rolled_clean ? 'clean at every step' : 'a loop-back somewhere'}`]
      : []),
    ...((payload.open_prs || []).length
      ? ['', `**still open:** ${(payload.open_prs || []).map((pr) =>
        `#${pr.number} (right-censored) — ${openPrState(pr)}`).join('; ')}`]
      : []),
    ...(payload.change && payload.change.unmerged_share
      ? [`**unmerged change:** ${Math.round(payload.change.unmerged_share * 100)} % ` +
        `(${payload.change.lines_unmerged} of ${payload.change.lines_total} changed lines) — ` +
        'the cycle is measured to the ticket\'s done, not to a shipped change']
      : []),
    '',
    '| segment | hours | share | active |',
    '| --- | --- | --- | --- |',
    ...(segments.rows || []).map((row) => `| ${row.label} | ${
      row.available ? segmentHours(row.hours) : 'not measured'}${
      row.available && benchmarkText(row.benchmark, { format: segmentHours })
        ? ` (${benchmarkText(row.benchmark, { format: segmentHours })})` : ''} | ${
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
      `${pr.censored ? ' (right-censored)' : ''} · ${segmentHours(pr.cycle_hours)}` +
      `${benchmarkText(pr.cycle_benchmark, { format: segmentHours })
        ? ` (${benchmarkText(pr.cycle_benchmark, { format: segmentHours })})` : ''}${
        pr.cycle_benchmark && pr.cycle_benchmark.declined ? ' (declined — no merged-PR benchmark)' : ''} · ` +
      `${pr.review_rounds} review round${pr.review_rounds === 1 ? '' : 's'} · ` +
      `pushes ${Object.entries(pr.push_classes || {}).map(([name, n]) => `${name}×${n}`).join(', ') || 'none'}` +
      `${prFindingText(pr).length ? ` — ${prFindingText(pr).join('; ')}` : ''}`),
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

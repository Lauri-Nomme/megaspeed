/* megaspeed timeline — custom Canvas 2D renderer (no chart library, no build).
 *
 * LTR time axis, swimlanes (actors / PRs / queues), activity markers, PR
 * lifecycle bars, semantic-ish LOD (aggregate when several events land in one
 * pixel column), zoom + pan, keyboard path.
 *
 * Everything it draws is a ticket key, a PR id, a commit hash, a pseudonym, a
 * timestamp or a closed label. There is no raw text in the data model.
 *
 * Phase honesty (docs/QA-UI-REPORT.md B3): a bar segment is a *measured* phase
 * only when the event log supports it. Author-ball intervals with commits are
 * "in progress"; without commits they are "waiting on author"; an interval that
 * ends at a rebase push is "rebasing"; CI only when a build event exists. The
 * tail after the last event of an open PR is "no events since" — hatched, never
 * painted as a measured phase.
 */

import {
  MINUTE, HOUR, DAY, fmtDay, fmtClock, fmtStamp, fmtCalendarDuration, fmtHours,
  fmtTick, fmtCompact, esc, rgba,
} from './format.js';
import { epicKeySet, epicTicket, ownTicket, ticketKey } from './tickets.js';
import { touchAxis } from './mobile.js';
import { color, onPalette } from './theme.js';

export const ACTIVITY_ORDER = [
  'pr_opened', 'pr_pushed', 'comment_added', 'pr_approved', 'pr_unapproved',
  'pr_needs_work', 'pr_merged', 'pr_declined', 'reviewer_changed', 'build_status',
];

/* Category hues only (prd/10 §3.3.5: teal = waits on and amber = waited on by
 * are a reserved pair, never reused for a category or a phase).
 *
 * The hue itself is a design token: `style.css` owns the value, so the light
 * theme recolours every marker by re-pointing `--cat-2` — the renderer never
 * knows which theme is on. */
export const ACTIVITY_TOKEN = {
  pr_opened: '--cat-2',
  pr_pushed: '--cat-3',
  comment_added: '--cat-7',
  pr_approved: '--cat-1',
  pr_unapproved: '--critical',
  pr_needs_work: '--warning',
  pr_merged: '--series-default',
  pr_declined: '--cat-6',
  reviewer_changed: '--cat-4',
  build_status: '--cat-2',
};

/** Resolved activity hues; `applyPalette()` fills them from the tokens. */
export const ACTIVITY_COLOR = {};

export const PHASE = {
  waiting: { token: '--phase-waiting', color: '', label: 'waiting on author', hint: 'ball with the author, no commits in the log' },
  active: { token: '--phase-active', color: '', label: 'in progress', hint: 'author held the ball and committed' },
  review: { token: '--phase-review', color: '', label: 'in review', hint: 'ball with reviewers' },
  ci: { token: '--phase-ci', color: '', label: 'CI', hint: 'ball with CI (needs a build event in the store)' },
  rebasing: { token: '--phase-rebasing', color: '', label: 'rebasing', hint: 'interval ended in a rebase push' },
  closed: { token: '--phase-closed', color: '', label: 'closed', hint: 'closed after the last event' },
  unobserved: { token: '--phase-unobserved', color: '', label: 'no events since', hint: 'not measured — no events in the store' },
  // prd/29: the two Jira workflow stages that are not PR phases. A ticket can
  // sit in review (no PR yet, or a PR the store cannot see) and in verification
  // (merged, waiting for QA) — different waits, so different hues. They are not
  // in PHASE_ORDER: that legend describes PR intervals, and these phases only
  // colour the ticket-detail segments (ticket.js reuses the hues).
  in_review: { token: '--phase-in-review', color: '', label: 'ticket in review', hint: 'Jira status → in review (workflow stage)' },
  in_verification: { token: '--phase-in-verification', color: '', label: 'in verification', hint: 'Jira status → in verification (QA)' },
};

export const PHASE_ORDER = ['waiting', 'active', 'review', 'ci', 'rebasing', 'closed', 'unobserved'];

/** Resolve every canvas hue from the theme tokens. Registered with theme.js,
 *  so it runs once for the dark fallbacks and again on each theme switch —
 *  which is what lets a toggle recolour the canvas without a data reload. */
export function applyPalette() {
  for (const [activity, token] of Object.entries(ACTIVITY_TOKEN)) {
    ACTIVITY_COLOR[activity] = color(token);
  }
  for (const phase of Object.values(PHASE)) phase.color = color(phase.token);
}
onPalette(applyPalette);

const METRICS = {
  S: { lane: 15, bar: 11, label: 10, marker: 2.2 },
  M: { lane: 22, bar: 15, label: 11, marker: 3 },
  L: { lane: 30, bar: 21, label: 12, marker: 3.8 },
};

/* prd/33 §3: the canvas is a *window* on the lane list, not the list itself.
 * `#timeline-scroll` scrolls a spacer of the full lane height and the canvas is
 * pinned at its top, so the bitmap is never taller than the viewport — which is
 * what keeps a 5,240-PR store (78,930 px of lanes) under Chrome's 65,535 px
 * canvas limit instead of silently painting nothing. */
const DEFAULT_VIEW_H = 600;
const MIN_VIEW_H = 240;
/** Extra rows drawn above/below the viewport so a lane never pops in mid-edge. */
const OVERSCAN = 1;

const GUTTER = 176;
// prd/28: on a 412px phone the 176px gutter would leave ~226px of plot; the
// labels shrink to the ticket key (and its PR number) so the time axis keeps
// most of the width. 112px is the narrowest gutter that still fits
// `DEL-105 #131` in the 11px mono label font.
const GUTTER_PHONE = 112;
const AXIS_H = 42;
const HIST_H = 36;
const HEADER_H = 22;
// prd/67 W-01: the one-line "N people … hidden" note gets its own row under the
// lane header, so it never runs into the section header or a sprint band label
// (on a 412px phone there is no free space on the header row itself).
const NOTE_H = 15;
const SECTION_GAP = 18;
const BOTTOM = 14;

const STEPS = [
  MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE,
  HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 28 * DAY, 91 * DAY,
];

const ACTIVITY_STATES = ['on', 'faint', 'hide'];
const QUEUE_ORDER = ['review', 'author', 'CI', 'calendar'];

/** The gutter label for one lane (pure, so the phone hierarchy is testable).
 *
 * On a phone a PR lane keeps both its ticket key and its PR number: repeating
 * only the story key made the PRs under one story indistinguishable. In the
 * story dimension the glyph carries the hierarchy (epic ◆ → story ▸ → PR).
 */
export function laneLabel(row, { narrow = false, dim = 'actor' } = {}) {
  if (!row) return '';
  if (narrow && dim === 'story') {
    if (row.kind === 'pr') return `${row.ticket ? `${row.ticket} ` : ''}#${row.number}`;
    const glyph = row.kind === 'epic' ? '◆ ' : row.kind === 'story' ? '▸ ' : '';
    return `${glyph}${row.label || row.key || ''}`;
  }
  if (narrow) {
    if (row.ticket) return `${row.ticket}${row.pr ? ` #${row.pr.number}` : ''}`;
    if (row.pr) return `#${row.pr.number}`;
    return row.label || row.key || '';
  }
  const indent = row.depth ? `${'  '.repeat(row.depth)}↳ ` : '';
  return `${indent}${row.label || row.key || ''}`;
}

/** W-01 (prd/67): the one line that says why the actor lanes are short.
 *
 * A window that hides every actor still says so — an empty list is an answer
 * ("nobody acted in this window"), not a missing one. `0` is silent, so the
 * note appears only when it is true.
 */
export function actorHiddenNote(hidden) {
  const n = Number(hidden) || 0;
  if (n <= 0) return '';
  return `${n} ${n === 1 ? 'person' : 'people'} with no activity in this window hidden`;
}

/** The section header over the gutter. A phone gets the short form, because
 *  `STORIES · 100 stories · 131 PRs` is wider than the canvas itself. */
export function laneHeader(dim, rows, { narrow = false } = {}) {
  const list = rows || [];
  const section = dim === 'actor' ? 'ACTORS'
    : dim === 'object' ? 'PULL REQUESTS'
      : dim === 'story' ? 'STORIES' : 'QUEUES';
  const storyCount = list.filter((row) => row.kind === 'story').length;
  const prCount = list.filter((row) => row.kind === 'pr').length;
  if (!narrow) {
    return dim === 'story'
      ? `${section} · ${storyCount} stories · ${prCount} PRs`
      : `${section} · ${list.length}`;
  }
  const short = dim === 'actor' ? 'ACTORS'
    : dim === 'object' ? 'PRs'
      : dim === 'story' ? 'STORIES' : 'QUEUES';
  return `${short} ${dim === 'story' ? storyCount : list.length}`;
}

/* prd/25: semantic zoom for the `stories` dimension. A wide window shows the
 * epic → story hierarchy (PRs would be unreadable), a medium window nests each
 * story's PRs beneath it, and the event markers on those PR bars are the third
 * level. The nesting threshold is in time, not pixels, so the hierarchy is
 * stable while panning. On a phone the epic header stays visible at every zoom:
 * with an 112px gutter a story is only legible as a child of its epic (DEL-1),
 * whether its PRs are collapsed or nested beneath it. The desktop keeps the
 * wide-window-only epic header it always had. */
const STORY_NEST_SPAN = 45 * DAY;
const STORY_EPIC_SPAN = 60 * DAY;

/** The sprint line of a story tooltip (prd/26 P6).
 *
 * The membership comes from ``/api/sprints``; a story with no membership is
 * *out of sprint scope*, said in words rather than left blank (prd/10 §7).
 */
function sprintRow(membership) {
  if (!membership) return '';
  const sprints = membership.sprints || [];
  if (!sprints.length) {
    return '<div class="row"><span class="k">sprint</span>' +
      '<span class="v">none — out of sprint scope</span></div>';
  }
  const mark = membership.carried_over ? ' · ↻ carried over' : '';
  return `<div class="row"><span class="k">sprint</span>` +
    `<span class="v">${esc(sprints.join(' → '))}${mark}</span></div>`;
}

function fit(ctx, text, maxWidth) {
  if (maxWidth <= 0) return '';
  if (ctx.measureText(text).width <= maxWidth) return text;
  let value = text;
  while (value.length > 1 && ctx.measureText(`${value}…`).width > maxWidth) value = value.slice(0, -1);
  return `${value}…`;
}

/**
 * The participant-status model of prd/40, mirrored on the client.
 *
 * This is a *port of the server's transitions*, not a second state machine: the
 * old copy here had no roster at all and flipped the ball on every approval, so
 * it already disagreed with `analysis/ballcourt.py`. When the payload carries
 * `pr.intervals` (the ticket view does), `lifecycle()` reads those instead of
 * deriving anything; this path exists only for the events-only timeline.
 *
 * State: `reviewers` (roster), `status` (Bitbucket participant status), `debt`
 * (whose objection still stands), `ci` (a flag, never a ball). `pending`,
 * `author_open`, `ready` and `ball` are derived.
 */
function deriveState(reviewers, status, debt, sentinels = new Set()) {
  let pending = [...reviewers].filter((person) => (status.get(person) || 'none') === 'none');
  // the sentinel is a precedence constraint: not charged while a peer is pending
  if (sentinels.size) {
    const peers = pending.filter((person) => !sentinels.has(person));
    if (peers.length) pending = peers;
  }
  const authorOpen = debt.size > 0;
  const ready = !authorOpen && pending.length === 0 && reviewers.size > 0;
  const ball = authorOpen ? 'author' : (pending.length || reviewers.size === 0 ? 'review' : 'ready');
  return { pending: new Set(pending), author_open: authorOpen, ready, ball };
}

function eventPeople(event) {
  return (event.objects || [])
    .filter((objectId) => String(objectId).startsWith('PERSON:'))
    .map((objectId) => String(objectId).slice('PERSON:'.length))
    .filter(Boolean);
}

function advanceState(pr, state, event, sentinels = new Set()) {
  const activity = event.activity;
  const attrs = event.attributes || {};
  const author = pr.actor;
  const reviewers = new Set(state?.reviewers || []);
  const status = new Map(state?.status || []);
  const debt = new Set(state?.debt || []);
  let ci = Boolean(state?.ci);
  const enlist = (person) => {
    if (!person || person === author) return;
    reviewers.add(person);
    if (!status.has(person)) status.set(person, 'none');
  };
  const people = eventPeople(event);
  if (activity === 'reviewer_changed') {
    const change = attrs.change;
    if (change === 'added') {
      people.forEach(enlist);
    } else if (change === 'removed') {
      people.forEach((person) => {
        reviewers.delete(person);
        status.delete(person);
        debt.delete(person);
      });
    } else if (change === 'reviewed' && event.actor !== author) {
      enlist(event.actor);
      status.set(event.actor, 'reviewed');
    }
  } else if (activity === 'pr_approved' && event.actor !== author) {
    enlist(event.actor);
    status.set(event.actor, 'approved');
    debt.delete(event.actor);
  } else if (activity === 'pr_needs_work' && event.actor !== author) {
    enlist(event.actor);
    status.set(event.actor, 'needs_work');
    debt.add(event.actor);
  } else if (activity === 'pr_unapproved' && event.actor !== author) {
    enlist(event.actor);
    if (attrs.cause === 'push') {
      status.set(event.actor, 'none');   // owes a new verdict, not author debt
    } else {
      status.set(event.actor, 'needs_work');
      debt.add(event.actor);
    }
  }
  if (activity === 'pr_merged' || activity === 'pr_declined') return null;
  let kind = activity;
  if (activity === 'pr_opened') {
    reviewers.forEach((person) => status.set(person, 'none'));
    debt.clear();
    ci = false;
  } else if (activity === 'pr_pushed' || activity === 'rebased') {
    const pushClass = attrs.push_class || (activity === 'rebased' ? 'pure_rebase' : 'other');
    const lost = Boolean(attrs.approvals_lost);
    if (!(pushClass === 'pure_rebase' && !lost)) {
      debt.clear();
      reviewers.forEach((person) => {
        if (lost || status.get(person) === 'needs_work' || status.get(person) === 'reviewed') {
          status.set(person, 'none');
        }
      });
    }
    kind = activity === 'pr_pushed' ? `push:${attrs.push_class || 'other'}` : 'rebase';
  } else if (activity === 'comment_added') {
    if (event.actor === author) {
      debt.clear();
      reviewers.forEach((person) => {
        if (status.get(person) === 'needs_work' || status.get(person) === 'reviewed') {
          status.set(person, 'none');
        }
      });
      kind = 'author_reply';
    } else if (attrs.severity === 'mandatory') {
      enlist(event.actor);
      status.set(event.actor, 'needs_work');
      debt.add(event.actor);
      kind = 'blocker_comment';
    } else {
      kind = 'comment';
    }
  } else if (activity === 'build_status') {
    const result = String(attrs.result || attrs.state || '').toLowerCase();
    ci = !['success', 'failed', 'passed', 'green', 'red'].includes(result);
    kind = 'ci';
  } else if (['pr_approved', 'pr_needs_work', 'pr_unapproved', 'reviewer_changed',
    'question_raised', 'question_answered'].includes(activity)) {
    kind = activity;
  } else {
    return state;
  }
  return {
    reviewers, status, debt, ci,
    ...deriveState(reviewers, status, debt, sentinels),
    by: event.actor, kind, since: event.t,
  };
}

function phaseFor(state, pr, next, commits) {
  if (state.ci && !state.author_open && state.pending.size === 0) return 'ci';
  const activity = next.activity;
  const attrs = next.attributes || {};
  if (activity === 'pr_pushed') {
    const pushClass = attrs.push_class || 'other';
    return pushClass === 'pure_rebase' || pushClass === 'rebase_conflict' ? 'rebasing' : 'active';
  }
  if (activity === 'rebased') return 'rebasing';
  if (activity === 'pr_merged' || activity === 'pr_declined') return 'review';
  if (state.author_open) {
    // evidence of author work: their own push/comment, or commits in the interval
    if (activity === 'pr_pushed' || next.actor === pr.actor) return 'active';
    return commits > 0 ? 'active' : 'waiting';
  }
  return 'review';
}

export class Timeline {
  constructor({ canvas, tip, scroller = null, spacer = null, onStats, onSelect, onTicket, onSelection, onSprint }) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.tip = tip;
    // prd/33 §3: the scroll container and the spacer that gives it the lane
    // list's full height. Both are optional so a headless test can drive the
    // layout with a stub; without them the canvas falls back to one viewport.
    this.scroller = scroller;
    this.spacer = spacer;
    this._spacerH = -1;
    this.onStats = onStats;
    this.onSelect = onSelect;
    this.onTicket = onTicket;
    this.onSelection = onSelection;
    this.onSprint = onSprint;
    this.selection = null;   // { start, end } picked by brushing the histogram
    this.brushing = null;
    this.density = 'M';
    this.dim = 'actor';
    this.activityState = new Map();
    // prd/40: the sentinel role, when the payload carries it, so the client's
    // port applies the same precedence gate as the server.
    this.sentinels = new Set();
    this.events = [];
    this.prs = [];
    this.commitsByPr = new Map();
    // prd/26 P6: the sprint bands and the per-story membership (carry-over).
    this.sprints = [];
    this.sprintStory = new Map();
    this.sprintScope = null;
    this.sprintCarry = null;
    // prd/37 §4: the selected sprint id — filters the story lanes and marks
    // the band; null means "every story".
    this.sprintFocus = null;
    // The epic/tracking keys across the whole store, set by the page (B2). When
    // null the census is taken over the PRs handed to setData().
    this.epicKeys = null;
    // prd/25: STORY objects carry their own `epic_key` attribute; the story lane
    // must use it even when no PR links the story to that epic.
    this.storyEpic = new Map();
    this.now = Date.now();
    this.domain = { start: 0, end: 1 };
    this.view = { start: 0, end: 1 };
    this.hits = [];
    this.hover = null;
    this.drag = null;
    // prd/28: touch state — one finger pans, two fingers pinch, a tap selects
    // and a hold shows the tooltip. `_touchAt` suppresses the synthetic mouse
    // events a browser fires after a touch so one tap never acts twice.
    this._touchAt = 0;
    this._pointers = new Map();
    this._pinch = null;
    this._holdTimer = 0;
    this.dirty = false;
    this.raf = 0;
    this._life = new Map();
    this._initialised = false;
    this._bind();
  }

  // ---------------------------------------------------------------- data
  /** The store-wide epic-key census, so a scope-filtered timeline still labels
   *  a PR with its own ticket rather than the shared epic key (B2). */
  setTicketIndex(index = {}) {
    const keys = index.epicKeys;
    this.epicKeys = keys instanceof Set ? keys : new Set(keys || []);
    this.schedule();
  }

  /** Sprint bands + the per-story membership (prd/26 P6).
   *
   * The payload is ``/api/sprints``: one band per SPRINT object over its
   * measured window, the cohort's scope, and the carry-over rows. Bands are
   * drawn behind the lanes; the story marks ride on the story lanes. A payload
   * with ``available: false`` clears them, so a PR-only store draws no sprint
   * furniture at all.
   */
  setSprints(payload = null) {
    const data = payload && payload.available ? payload : null;
    this.sprintScope = data ? data.scope || null : null;
    this.sprintCarry = data ? data.carry_over || null : null;
    this.sprints = ((data && data.sprints) || [])
      .map((sprint) => {
        const start = Date.parse(sprint.band_start);
        const end = Date.parse(sprint.band_end);
        return {
          id: sprint.sprint_id,
          state: sprint.state,
          open: Boolean(sprint.open),
          // an active sprint is one the store says is active (or an old row with
          // no state but no completion); a closed sprint without an end is still
          // closed, only its band has to stop at the snapshot
          active: sprint.state === 'active'
            || (Boolean(sprint.open) && !sprint.state),
          start,
          end,
          // prd/68 F-02: a dateless event-only sprint has real membership but no
          // band. Keep it in `this.sprints` so focusing it still filters the
          // story lanes; only `drawSprintBands` checks `hasBand`.
          hasBand: Number.isFinite(start) && Number.isFinite(end),
          plannedEnd: sprint.planned_end
            ? Date.parse(`${sprint.planned_end}T23:59:59Z`) : null,
          stories: sprint.story_count || 0,
          storyKeys: sprint.stories || [],
          points: sprint.points || 0,
          done: sprint.done_by_end ?? sprint.done ?? 0,
          doneByEnd: sprint.done_by_end ?? sprint.done ?? 0,
          doneLate: sprint.done_late || 0,
          neverDone: sprint.never_done || 0,
          // prd/43 §5: Jira-done but the story's PRs never merged
          unmerged: sprint.unmerged || 0,
          lateHours: sprint.late_hours || 0,
          carriedIn: sprint.carried_in || [],
          carriedOut: sprint.carried_out || [],
        };
      });
    this.sprintStory = new Map();
    for (const row of (data && data.rows) || []) {
      if (row && row.key) this.sprintStory.set(row.key, row);
    }
    if (this.sprintFocus != null
        && !this.sprints.some((sprint) => String(sprint.id) === String(this.sprintFocus))) {
      this.sprintFocus = null;
    }
    this._rowCache = null;
    this._objectRows = null;
    this.schedule();
  }

  /** Select (or clear) the sprint lens (prd/37 §4).
   *
   * The chosen sprint filters the story lanes to its committed stories, marks
   * its band on the canvas and dims the others. ``null`` restores every lane.
   */
  setSprintFocus(id) {
    const next = id == null ? null : id;
    if (this.sprintFocus === next) return;
    this.sprintFocus = next;
    this._rowCache = null;
    this._objectRows = null;
    this.schedule();
  }

  /** The story keys committed to the focused sprint, or null when none is. */
  sprintFocusKeys() {
    if (this.sprintFocus == null) return null;
    const sprint = this.sprints.find(
      (row) => String(row.id) === String(this.sprintFocus),
    );
    return sprint ? new Set(sprint.storyKeys || []) : null;
  }

  setData(events, objects, meta = {}) {
    const previous = this._initialised ? { ...this.view } : null;
    this.events = events
      .map((e) => ({ ...e, t: Date.parse(e.ts) }))
      .sort((a, b) => a.t - b.t);
    const prs = new Map();
    this.commitsByPr = new Map();
    // prd/25: a story object names its epic in `attributes.epic_key`. That is
    // the authoritative parent link for a story lane (a story may have no PR,
    // or its PRs may carry only the story key).
    this.storyEpic = new Map();
    for (const obj of objects) {
      if (obj.object_type !== 'STORY') continue;
      const key = ticketKey(obj.object_id) || (obj.ticket_keys || [])[0];
      const epic = (obj.attributes && obj.attributes.epic_key) || '';
      if (key) this.storyEpic.set(key, epic);
    }
    this._storyCache = null;
    this._rowCache = null;
    this._statsCache = null;
    this._epicPrs = null;
    for (const obj of objects) {
      if (obj.object_type === 'PR') {
        prs.set(obj.object_id, {
          id: obj.object_id,
          number: obj.pr_id,
          repo: (obj.repo || '').split('/').pop(),
          repoFull: obj.repo || '',
          keys: obj.ticket_keys || [],
          created: Date.parse(obj.created_at),
          closed: obj.closed_at ? Date.parse(obj.closed_at) : null,
          state: obj.state || 'open',
          actor: obj.actor,
          events: [],
        });
      } else if (obj.object_type === 'COMMIT' && obj.repo && obj.pr_id != null && obj.created_at) {
        const key = `${obj.repo}#${obj.pr_id}`;
        if (!this.commitsByPr.has(key)) this.commitsByPr.set(key, []);
        this.commitsByPr.get(key).push(Date.parse(obj.created_at));
      }
    }
    for (const times of this.commitsByPr.values()) times.sort((a, b) => a - b);
    for (const event of this.events) {
      const pr = prs.get(`PR:${event.repo}#${event.pr_id}`);
      if (pr) pr.events.push(event);
    }
    for (const pr of prs.values()) pr.commits = this.commitsByPr.get(`${pr.repoFull}#${pr.number}`) || [];
    this.prs = [...prs.values()].sort((a, b) => (a.repoFull || '').localeCompare(b.repoFull || '') || a.number - b.number);
    // One label key per PR: its own story ticket, not the shared epic link (B2).
    const epics = this.epicKeys || epicKeySet(this.prs.map((pr) => pr.keys));
    for (const pr of this.prs) {
      pr.ticket = ownTicket(pr.keys, epics);
      pr.epic = epicTicket(pr.keys, epics);
    }
    this.epics = epics;
    this.sentinels = new Set(meta.sentinels || []);
    this.now = meta.as_of ? Date.parse(meta.as_of) : (meta.last_ts ? Date.parse(meta.last_ts) : Date.now());

    const times = this.events.map((e) => e.t).concat(this.prs.map((p) => p.created));
    if (!times.length) {
      this.domain = { start: this.now - DAY, end: this.now };
      this.view = { ...this.domain };
      this._initialised = true;
      this.schedule();
      return;
    }
    const start = Math.min(...times);
    const end = Math.max(...times, ...this.prs.map((p) => p.closed || this.now));
    const padMs = Math.max((end - start) * 0.015, 10 * MINUTE);
    this.domain = { start: start - padMs, end: end + padMs };
    this._life = new Map();
    // B5: a live refresh updates the data under the current view; it does not
    // throw the investigator's window away.
    if (previous && previous.end > this.domain.start && previous.start < this.domain.end) {
      this.view = { start: previous.start, end: previous.end };
      this.clampView();
    } else {
      this.view = { ...this.domain };
    }
    this._initialised = true;
    this.clearHover();
    this.schedule();
  }

  setDensity(d) {
    this.density = d;
    this.clearHover();
    this.schedule();
  }

  /** Snapshot the investigation window across a data refresh (B5).

   * The API is paged, so ``setData`` may run several times per load (once per
   * page). The view is captured once, before the first page, and restored after
   * the last one, so progressive loading never moves the user's window.
   */
  captureView() {
    return this._initialised && this.view ? { ...this.view } : null;
  }

  restoreView(view) {
    if (!view || !this._initialised) return false;
    const start = Math.max(view.start, this.domain.start);
    const end = Math.min(view.end, this.domain.end);
    if (!(end > start)) return false;
    this.view = { start, end };
    this.schedule();
    return true;
  }

  setLanes(dim) {
    this.dim = dim;
    this.clearHover();
    this.schedule();
  }

  stateOf(activity) {
    return this.activityState.get(activity) || 'on';
  }

  cycleActivity(activity) {
    const current = this.stateOf(activity);
    const next = ACTIVITY_STATES[(ACTIVITY_STATES.indexOf(current) + 1) % ACTIVITY_STATES.length];
    if (next === 'on') this.activityState.delete(activity);
    else this.activityState.set(activity, next);
    this.clearHover();
    this.schedule();
    return next;
  }

  setActivityState(activity, state) {
    if (!state || state === 'on') this.activityState.delete(activity);
    else this.activityState.set(activity, state);
    this.schedule();
  }

  activityCounts() {
    const counts = new Map();
    for (const event of this.events) counts.set(event.activity, (counts.get(event.activity) || 0) + 1);
    return counts;
  }

  // ---------------------------------------------------------------- view
  fit() {
    this.view = { ...this.domain };
    this.clearHover();
    this.schedule();
  }

  span() {
    return Math.max(this.view.end - this.view.start, 60_000);
  }

  zoom(factor, anchorPx = null) {
    const g = this.geom;
    const span = this.span();
    const anchor = anchorPx == null ? (g ? g.gutter + g.plotW / 2 : this.canvas.clientWidth / 2) : anchorPx;
    const t = this.timeAt(anchor);
    let next = span * factor;
    // B10: zooming out stops at the data window (Fit), never at 2× the domain.
    next = Math.max(10 * MINUTE, Math.min(next, this.domain.end - this.domain.start));
    const ratio = (anchor - (g ? g.gutter : 0)) / Math.max(1, g ? g.plotW : this.canvas.clientWidth);
    this.view = { start: t - next * ratio, end: t + next * (1 - ratio) };
    this.clampView();
    this.clearHover();
    this.schedule();
  }

  panPixels(dx) {
    const g = this.geom;
    if (!g) return;
    const dt = (dx / g.plotW) * this.span();
    this.view.start -= dt;
    this.view.end -= dt;
    this.clampView();
    this.clearHover();
    this.schedule();
  }

  panFraction(fraction) {
    const dt = this.span() * fraction;
    this.view.start += dt;
    this.view.end += dt;
    this.clampView();
    this.clearHover();
    this.schedule();
  }

  clampView() {
    const span = Math.min(this.span(), this.domain.end - this.domain.start);
    const slack = Math.max(span * 0.2, HOUR);
    const lo = this.domain.start - slack;
    const hi = this.domain.end + slack - span;
    let start = Math.min(this.view.start, Math.max(lo, hi));
    start = Math.max(start, lo);
    this.view = { start, end: start + span };
  }

  /** Set the visible window from absolute timestamps (top-bar timeframe). */
  setWindow(start, end) {
    if (!(end > start)) return;
    const domainSpan = this.domain.end - this.domain.start;
    const span = Math.min(end - start, domainSpan);
    const latest = this.domain.end - span;
    const from = Math.max(this.domain.start, Math.min(start, latest));
    this.view = { start: from, end: from + span };
    this.clearHover();
    this.schedule();
  }

  timeAt(px) {
    const g = this.geom;
    if (!g) return this.view.start;
    const x = Math.min(Math.max(px, g.gutter), g.gutter + g.plotW);
    return this.view.start + ((x - g.gutter) / g.plotW) * this.span();
  }

  px(t) {
    const g = this.geom;
    return g.gutter + ((t - this.view.start) / this.span()) * g.plotW;
  }

  // ---------------------------------------------------------------- lifecycle
  lifecycle(pr) {
    const cached = this._life.get(pr.id);
    if (cached) return cached;
    // prd/40: when the payload carries the server's intervals, draw those.
    // The server is the one state machine; the local port below is only for
    // the events-only timeline.
    if (Array.isArray(pr.intervals) && pr.intervals.length) {
      const segments = pr.intervals.map((interval) => ({
        from: Date.parse(interval.from || interval.start),
        to: Date.parse(interval.to || interval.end),
        phase: interval.phase,
        commits: interval.commits || 0,
        ball: interval.ball,
        by: interval.by || null,
        kind: interval.kind,
        ready: Boolean(interval.ready),
        holders: interval.holders || [],
        endedBy: null,
      })).filter((seg) => Number.isFinite(seg.from) && Number.isFinite(seg.to));
      this._life.set(pr.id, segments);
      return segments;
    }
    const segments = [];
    let state = null;
    let since = pr.created;
    for (const event of pr.events) {
      if (state && event.t > since) {
        const commits = countBetween(pr.commits, since, event.t);
        const phase = phaseFor(state, pr, event, commits);
        segments.push({
          from: since, to: event.t, phase, commits,
          ball: state.ball, by: state.by, kind: state.kind,
          ready: Boolean(state.ready),
          holders: [...state.pending],
          endedBy: event.activity,
        });
      }
      const next = advanceState(pr, state, event, this.sentinels);
      if (!next) {
        state = null;
        segments.push({ from: event.t, to: event.t, phase: 'closed', commits: 0, ball: 'closed', by: event.actor, kind: event.activity, endedBy: event.activity });
        break;
      }
      state = next;
      since = event.t;
    }
    if (state) {
      const end = pr.closed || this.now;
      if (end > since) {
        segments.push({
          from: since, to: end, phase: pr.closed ? 'closed' : 'unobserved', commits: 0,
          ball: state.ball, by: state.by, kind: state.kind,
          ready: Boolean(state.ready),
          holders: [...state.pending],
          endedBy: null,
        });
      }
    }
    this._life.set(pr.id, segments);
    return segments;
  }

  phases(pr) {
    const totals = new Map();
    for (const seg of this.lifecycle(pr)) {
      const duration = seg.to - seg.from;
      if (duration <= 0) continue;
      totals.set(seg.phase, (totals.get(seg.phase) || 0) + duration);
    }
    return totals;
  }

  phaseStats() {
    // Cached per setData: a scroll frame must not re-walk every PR's life.
    if (this._statsCache) return this._statsCache;
    const stats = new Map();
    for (const pr of this.prs) {
      for (const seg of this.lifecycle(pr)) {
        const duration = seg.to - seg.from;
        if (duration <= 0) continue;
        const entry = stats.get(seg.phase) || { hours: 0, count: 0 };
        entry.hours += duration / HOUR;
        entry.count += 1;
        stats.set(seg.phase, entry);
      }
    }
    this._statsCache = stats;
    return stats;
  }

  // ---------------------------------------------------------------- lanes
  laneRows() {
    if (this.dim === 'story') return this.storyRows();
    if (this.dim === 'object') {
      // The row list is a function of the loaded data only, and `layout()` runs
      // on every scroll frame now — build it once per setData, not per frame.
      const focus = this.sprintFocusKeys();
      const cacheKey = this.sprintFocus;
      if (this._objectRows && this._objectRows.prs === this.prs
          && this._objectRows.focus === cacheKey) return this._objectRows.rows;
      const rows = this.prs
        .filter((pr) => !focus || focus.has(pr.ticket))
        .map((pr) => ({
        key: `${pr.repo}#${pr.number}`,
        pr,
        ticket: pr.ticket,
        count: pr.events.length,
        label: `${pr.ticket ? `${pr.ticket} ` : ''}#${pr.number} ${pr.repo}`,
      }));
      this._objectRows = { prs: this.prs, rows, focus: cacheKey };
      return rows;
    }
    if (this.dim === 'queue') {
      return QUEUE_ORDER.map((key) => {
        const hours = this.queueHours(key);
        return { key, hours, count: Math.round(hours), label: `${key} · ${fmtCompact(hours)} h` };
      }).filter((row) => row.hours > 0);
    }
    // W-01 (prd/67): an actor lane is someone who acted *inside the window*.
    // The list used to be every actor ever seen — a 252-person Jira store drew
    // hundreds of empty lanes, whatever the timeframe. The window is part of
    // the cache key, so a pan/zoom/timeframe change rebuilds the list, and
    // `hidden` reports how many people this window hides (the note is drawn
    // next to the lane header, never invented when it is 0).
    //
    // B4 is preserved: a row's `count` stays its all-time event total, so a
    // toggle of a legend key never re-sorts the lanes.
    const start = this.view.start;
    const end = this.view.end;
    const cache = this._actorRows;
    if (cache && cache.events === this.events
        && cache.start === start && cache.end === end) return cache.rows;
    const counts = this.actorCounts();
    const active = new Set();
    for (let i = this.firstEventIndex(start); i < this.events.length; i += 1) {
      const event = this.events[i];
      if (event.t > end) break;
      active.add(event.actor);
    }
    const rows = [...active]
      .sort((a, b) => ((counts.get(b) || 0) - (counts.get(a) || 0)) || a.localeCompare(b))
      .map((key) => ({ key, count: counts.get(key) || 0 }));
    this._actorRows = {
      events: this.events, start, end, rows,
      hidden: Math.max(0, counts.size - rows.length),
    };
    return rows;
  }

  /** Every actor's all-time event count (the lane order and row total).
   *
   * Cached per `setData`: a pan rebuilds the actor rows (the window changed),
   * and re-walking the log for the same totals on every frame would make a
   * windowed lane list cost more than the unwindowed one it replaced.
   */
  actorCounts() {
    if (this._actorCounts && this._actorCounts.events === this.events) {
      return this._actorCounts.counts;
    }
    const counts = new Map();
    for (const event of this.events) {
      counts.set(event.actor, (counts.get(event.actor) || 0) + 1);
    }
    this._actorCounts = { events: this.events, counts };
    return counts;
  }

  /** Index of the first event at or after `t` (`this.events` is time-sorted). */
  firstEventIndex(t) {
    let lo = 0;
    let hi = this.events.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.events[mid].t < t) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  /** W-01: how many actors the current window hides (0 outside the actor dim). */
  hiddenActors() {
    if (this.dim !== 'actor') return 0;
    this.laneRows();
    return (this._actorRows && this._actorRows.hidden) || 0;
  }

  /** Stories as lanes with their PRs nested beneath (prd/25, epic → story → PR).
   *
   * At a wide zoom the rows are the epic → story hierarchy; below
   * :data:`STORY_NEST_SPAN` each story's PRs become child rows. The event
   * markers on those PR bars are the third semantic level, drawn by the same
   * `paintLifecycle` the object lanes use.
   */
  isPhone() {
    const parent = this.canvas && this.canvas.parentElement;
    return ((parent && parent.clientWidth) || 1200) <= 640;
  }

  storyRows() {
    // The grouping is a function of the loaded data, not of the zoom, so it is
    // built once per setData (a pan/zoom only re-reads it).
    const storyEpic = this.storyEpic || new Map();
    let groups;
    if (this._storyCache && this._storyCache.prs === this.prs
        && this._storyCache.events === this.events) {
      groups = this._storyCache.groups;
    } else {
      groups = new Map();
      for (const pr of this.prs) {
        const story = pr.ticket || '(no ticket)';
        if (!groups.has(story)) {
          groups.set(story, {
            story,
            epic: pr.epic || storyEpic.get(story) || '',
            prs: [],
            events: [],
          });
        }
        const group = groups.get(story);
        if (!group.epic) group.epic = pr.epic || storyEpic.get(story) || '';
        group.prs.push(pr);
      }
      // A story with no PR is still a lane (prd/25: story → event): its own
      // created/status/sprint/pointed events are in the log and name the STORY.
      for (const event of this.events) {
        for (const objectId of event.objects || []) {
          if (!String(objectId).startsWith('STORY:')) continue;
          const story = String(objectId).slice('STORY:'.length);
          if (!groups.has(story)) {
            groups.set(story, {
              story,
              epic: storyEpic.get(story) || '',
              prs: [],
              events: [],
            });
          }
          const group = groups.get(story);
          if (!group.epic) group.epic = storyEpic.get(story) || '';
          group.events.push(event);
        }
      }
      this._storyCache = { prs: this.prs, events: this.events, groups };
    }
    const span = this.span();
    const nested = span <= STORY_NEST_SPAN;
    // On a phone the epic header is the top of the hierarchy at every zoom:
    // hiding it when PRs nest made a story row look parentless.
    const hasEpics = [...groups.values()].some((group) => group.epic);
    const showEpics = hasEpics && (this.isPhone() || (!nested && span > STORY_EPIC_SPAN));
    // The row list is a function of the data and of the two zoom thresholds, and
    // `layout()` runs on every scroll frame — so build it once per (data, zoom)
    // instead of re-allocating 7,920 rows per frame at 4,000 stories.
    const focus = this.sprintFocusKeys();
    const cache = this._rowCache;
    if (cache && cache.groups === groups && cache.nested === nested
        && cache.showEpics === showEpics && cache.prs === this.prs
        && cache.focus === this.sprintFocus) {
      this._storyNested = nested;
      this._storyEpics = showEpics;
      this._epicPrs = cache.epicPrs;
      return cache.rows;
    }
    const stories = [...groups.values()]
      // prd/37 §4: the sprint lens filters the story lanes, not just the window
      .filter((group) => !focus || focus.has(group.story))
      .sort((a, b) => {
      const start = (group) => Math.min(
        ...group.prs.map((pr) => pr.created),
        ...group.events.map((event) => event.t),
        Infinity,
      );
      return start(a) - start(b) || a.story.localeCompare(b.story);
    });
    // prd/33 §5.3: the epic header needs its members, and the epic bracket and
    // tooltip need its PRs. Both used to be a `stories.filter(...)` *inside*
    // the loop over stories — O(stories²) at 4,000 stories (88 ms). One pass
    // builds both indexes, so the rows stay linear.
    const epicMembers = new Map();
    for (const group of stories) {
      if (!group.epic) continue;
      const list = epicMembers.get(group.epic) || [];
      list.push(group);
      epicMembers.set(group.epic, list);
    }
    const epicPrs = new Map();
    for (const [key, list] of epicMembers) epicPrs.set(key, list.flatMap((group) => group.prs));
    this._epicPrs = epicPrs;

    const rows = [];
    let epic = null;
    for (const group of stories) {
      if (showEpics && group.epic && group.epic !== epic) {
        epic = group.epic;
        const members = epicMembers.get(epic) || [];
        rows.push({
          key: `epic:${epic}`,
          kind: 'epic',
          epic,
          ticket: epic,
          count: members.reduce((sum, entry) => sum + entry.prs.length, 0),
          label: epic,
        });
      }
      rows.push({
        key: `story:${group.story}`,
        kind: 'story',
        story: group.story,
        ticket: group.story,
        epic: group.epic,
        prs: group.prs,
        events: group.events,
        count: group.prs.reduce((sum, pr) => sum + pr.events.length, 0) + group.events.length,
        label: group.story,
        depth: showEpics && group.epic ? 1 : 0,
      });
      if (nested) {
        for (const pr of group.prs) {
          rows.push({
            key: `pr:${pr.id}`,
            kind: 'pr',
            pr,
            story: group.story,
            ticket: pr.ticket || group.story,
            number: pr.number,
            count: pr.events.length,
            // prd/25: the PR row carries its own ticket key and number (the
            // phone gutter prints `DEL-100 #1`, see laneLabel).
            label: `#${pr.number} ${pr.repo}`,
            depth: showEpics && group.epic ? 2 : 1,
          });
        }
      }
    }
    this._storyNested = nested;
    this._storyEpics = showEpics;
    this._rowCache = {
      groups, nested, showEpics, prs: this.prs, rows, epicPrs,
      focus: this.sprintFocus,
    };
    return rows;
  }

  queueHours(queue) {
    let hours = 0;
    for (const pr of this.prs) {
      for (const seg of this.lifecycle(pr)) {
        if (seg.to <= seg.from) continue;
        if (this.queueOf(seg) === queue) hours += (seg.to - seg.from) / HOUR;
      }
    }
    return hours;
  }

  queueOf(seg) {
    if (seg.ball === 'author') return 'author';
    if (seg.ball === 'CI') return 'CI';
    if (seg.ball === 'calendar') return 'calendar';
    return 'review';
  }

  // ---------------------------------------------------------------- layout
  /** The height of the lane viewport: the scroll container, never the content.
   *  A hidden page reports 0, so fall back to a sane viewport rather than a
   *  1px canvas that the first visible frame would have to fix up. */
  viewportHeight() {
    const box = this.scroller || this.canvas.parentElement;
    const h = box && box.clientHeight;
    if (h) return Math.max(MIN_VIEW_H, Math.round(h));
    return DEFAULT_VIEW_H;
  }

  /** How far the lane list is scrolled (0 when there is no container). */
  scrollPos() {
    const top = this.scroller && this.scroller.scrollTop;
    return Number.isFinite(top) && top > 0 ? top : 0;
  }

  /** Give the scroll container the lane list's height, so the scrollbar is
   *  proportional to the store instead of to the canvas. */
  setSpacer(height) {
    if (!this.spacer || !this.spacer.style) return;
    const h = Math.max(0, Math.round(height));
    if (h === this._spacerH) return;
    this._spacerH = h;
    this.spacer.style.height = `${h}px`;
  }

  /** Scroll the lane list by one screenful (PageUp / PageDown). */
  scrollLanes(direction) {
    const g = this.geom;
    if (!g || !this.scroller) return;
    const step = Math.max(g.laneH * 3, g.height - g.laneTop - g.laneH);
    this.scroller.scrollTop = Math.max(
      0,
      Math.min(g.maxScroll, this.scrollPos() + direction * step),
    );
    this.schedule();
  }

  /** The slice of rows a viewport can show, in content space.
   *
   * Rows are laid out at `top + i * rowH`; the content is translated up by
   * `scrollTop`, and the canvas is only `height` tall. One row of overscan on
   * each side keeps a partly visible lane drawn instead of popping in. */
  rowWindow(top, rowH, count, scrollTop, height, overscan = OVERSCAN) {
    if (!(rowH > 0) || !(count > 0)) return { first: 0, last: 0 };
    const first = Math.max(0, Math.floor((scrollTop - top) / rowH) - overscan);
    const last = Math.min(count, Math.ceil((scrollTop + height - top) / rowH) + overscan);
    return { first: Math.min(first, last), last };
  }

  layout() {
    const m = METRICS[this.density];
    const lanes = this.laneRows();
    const objectMode = this.dim === 'object' || this.dim === 'story';
    const laneH = objectMode ? Math.max(m.bar + 6, m.lane) : m.lane;
    const barRows = objectMode ? [] : this.prs;
    // prd/67 W-01: the actor dim owns a note row only when the window hides
    // someone; every other dim and a fully-visible actor list keeps the old
    // geometry exactly.
    const hiddenPeople = this.dim === 'actor' ? this.hiddenActors() : 0;
    const noteH = hiddenPeople > 0 ? NOTE_H : 0;
    const laneTop = AXIS_H + HIST_H + HEADER_H + noteH;
    const laneBottom = laneTop + lanes.length * laneH;
    const barTop = laneBottom + SECTION_GAP + HEADER_H;
    // The lane list in content pixels. This is what the scroll container
    // scrolls through; the canvas itself is only ever one viewport tall
    // (prd/33 §3: 5,240 PRs = 78,930 px of lanes, past Chrome's 65,535 px
    // canvas limit, where it silently paints nothing).
    const contentHeight = objectMode ? laneBottom + BOTTOM
      : barTop + barRows.length * m.bar + BOTTOM;
    const width = this.canvas.parentElement.clientWidth || 1200;
    const gutter = this.isPhone() ? GUTTER_PHONE : GUTTER;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const viewport = this.viewportHeight();
    // The canvas is the window on the list, so it can never outgrow the
    // window — that is what removes the 65,535 px cliff entirely.
    const height = Math.max(1, Math.min(contentHeight, viewport));
    const maxScroll = Math.max(0, contentHeight - height);
    const scrollTop = Math.min(this.scrollPos(), maxScroll);
    this.setSpacer(maxScroll);
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.geom = {
      width, height, contentHeight, scrollTop, maxScroll,
      gutter, plotW: Math.max(80, width - gutter - 10),
      axisH: AXIS_H, histH: HIST_H, headerH: HEADER_H, noteH, hiddenPeople, laneH, barH: m.bar,
      label: m.label, marker: m.marker, objectMode,
      laneTop, laneBottom, barTop, lanes, barRows,
      window: {
        lanes: this.rowWindow(laneTop, laneH, lanes.length, scrollTop, height),
        bars: this.rowWindow(barTop, m.bar, barRows.length, scrollTop, height),
      },
    };
    return this.geom;
  }

  /** Draw inside the lane viewport only, so a row scrolled under the pinned
   *  axis/histogram is clipped instead of overpainting them. */
  withLaneClip(ctx, g, draw) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, g.laneTop, g.width, Math.max(0, g.height - g.laneTop));
    ctx.clip();
    try { draw(); } finally { ctx.restore(); }
  }

  // ---------------------------------------------------------------- draw
  schedule() {
    this.dirty = true;
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = 0;
      if (this.dirty) this.render();
    });
  }

  clearHover() {
    this.hover = null;
    if (this.tip) this.tip.hidden = true;
  }

  setSelection(start, end) {
    if (!(end > start)) {
      this.clearSelection();
      return;
    }
    this.selection = { start, end };
    this.schedule();
    if (this.onSelection) this.onSelection(this.selection);
  }

  clearSelection() {
    this.selection = null;
    this.brushing = null;
    this.schedule();
    if (this.onSelection) this.onSelection(null);
  }

  /* prd/38 #20: "Timeline" from a PR drawer keeps the PR selected on the chart.
   * The ticket scope narrows the lanes; this marks the exact PR the user came
   * from, so the destination is not just a filtered list with no anchor. */
  focusPr(id) {
    const pr = this.prs.find((candidate) => candidate.id === id);
    this.focusPrId = pr ? pr.id : null;
    this.schedule();
    return Boolean(pr);
  }

  render() {
    this.dirty = false;
    const ctx = this.ctx;
    const g = this.layout();
    const m = METRICS[this.density];
    this.hits = [];

    ctx.clearRect(0, 0, g.width, g.height);
    ctx.fillStyle = color('--canvas-bg');
    ctx.fillRect(0, 0, g.width, g.height);

    this.drawTimeBands(ctx, g);
    this.drawSprintBands(ctx, g);
    this.drawAxis(ctx, g);
    this.drawHistogram(ctx, g);
    this.drawLanes(ctx, g, m);
    if (!g.objectMode) this.drawBars(ctx, g, m);

    ctx.strokeStyle = color('--canvas-hairline');
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(g.gutter + 0.5, 0);
    ctx.lineTo(g.gutter + 0.5, g.height);
    ctx.stroke();

    // A lane scrolled under the pinned axis/histogram must not answer hits
    // there: the strip stays brushable and the header stays click-through.
    this.clipRowHits(g);

    // B6: a tooltip may not survive the state that produced it.
    if (this.hover) {
      const ttl = this.hover.event ? this.hits.some((h) => h.kind === 'event' && h.event === this.hover.event)
        : this.hover.pr ? this.hits.some((h) => h.kind === 'bar' && h.pr === this.hover.pr)
          : this.hover.story ? this.hits.some((h) => h.kind === 'story' && h.story === this.hover.story)
            : this.hover.interval ? this.hits.some((h) => h.kind === 'interval' && h.interval === this.hover.interval)
              : this.hover.sprint ? this.hits.some((h) => h.kind === 'sprint' && h.sprint === this.hover.sprint)
                : false;
      if (!ttl) this.clearHover();
    }

    const stats = this.phaseStats();
    const counts = this.activityCounts();
    const hiddenAll = counts.size > 0 && [...counts.keys()].every((a) => this.stateOf(a) === 'hide');
    if (this.onStats) {
      this.onStats({
        start: this.view.start,
        end: this.view.end,
        visible: this.visibleCount(),
        total: this.events.length,
        span: this.span(),
        bucketMs: this.span() / g.plotW,
        aggregated: this.aggregated > 0,
        openPrs: this.prs.filter((p) => p.state === 'open').length,
        lanes: g.lanes.length,
        dim: this.dim,
        storyLanes: this.dim === 'story' ? g.lanes.filter((row) => row.kind === 'story').length : 0,
        prLanes: this.dim === 'story' ? g.lanes.filter((row) => row.kind === 'pr').length : 0,
        nested: this.dim === 'story' ? Boolean(this._storyNested) : false,
        allHidden: hiddenAll,
        phases: stats,
        // prd/26 P6: the sprint furniture currently drawn
        sprints: this.sprints.length,
        sprintScope: this.sprintScope,
        sprintCarry: this.sprintCarry,
      });
    }
    const hiddenPeople = this.dim === 'actor' ? this.hiddenActors() : 0;
    this.canvas.setAttribute(
      'aria-label',
      `Event timeline, ${fmtDay(this.view.start)} ${fmtClock(this.view.start)} to ` +
      `${fmtDay(this.view.end)} ${fmtClock(this.view.end)}; ${visibleWord(this)} events, ` +
      `${g.lanes.length} ${this.dim === 'actor' ? 'actor' : this.dim === 'object' ? 'PR' : this.dim === 'story' ? 'story' : 'queue'} lanes. ` +
      (hiddenPeople > 0 ? `${actorHiddenNote(hiddenPeople)}. ` : '') +
      'Arrow keys pan, plus and minus zoom, Home fits.',
    );
  }

  visibleCount() {
    let n = 0;
    for (const event of this.events) {
      if (this.stateOf(event.activity) === 'hide') continue;
      if (event.t < this.view.start || event.t > this.view.end) continue;
      n += 1;
    }
    return n;
  }

  /** Drop or clamp the row hits that fall outside the visible lane window. */
  clipRowHits(g) {
    for (const hit of this.hits) {
      // the strip and the sprint bands own the header rows on purpose
      if (hit.kind === 'histogram' || hit.kind === 'sprint') continue;
      if (hit.y0 === undefined || hit.y1 === undefined) {
        if (hit.kind === 'event' && hit.y < g.laneTop) hit.dead = true;
        continue;
      }
      hit.y0 = Math.max(hit.y0, g.laneTop);
      hit.y1 = Math.min(hit.y1, g.height);
      if (hit.y1 <= hit.y0) hit.dead = true;
    }
  }

  drawTimeBands(ctx, g) {
    const dayWidth = g.plotW / (this.span() / DAY);
    if (dayWidth < 26) return;
    const first = Math.floor(this.view.start / DAY) * DAY;
    for (let t = first; t < this.view.end; t += DAY) {
      const d = new Date(t);
      const weekend = d.getUTCDay() === 0 || d.getUTCDay() === 6;
      const x0 = Math.max(g.gutter, this.px(t));
      const x1 = Math.min(g.gutter + g.plotW, this.px(t + DAY));
      if (x1 <= x0) continue;
      if (weekend) {
        ctx.fillStyle = color('--canvas-weekend');
        ctx.fillRect(x0, AXIS_H, x1 - x0, g.height - AXIS_H);
      }
      if (dayWidth >= 90) {
        const night0 = this.px(t + 18 * HOUR);
        const night1 = this.px(t + DAY);
        if (night1 > night0) {
          ctx.fillStyle = color('--canvas-night');
          ctx.fillRect(night0, AXIS_H, night1 - night0, g.height - AXIS_H);
        }
        const night2 = this.px(t);
        const night3 = this.px(t + 8 * HOUR);
        if (night3 > night2) {
          ctx.fillStyle = color('--canvas-night');
          ctx.fillRect(night2, AXIS_H, night3 - night2, g.height - AXIS_H);
        }
      }
    }
  }

  /** Sprint bands (prd/26 P6): one tinted region per SPRINT object, behind the
   *  lanes, with the measured window as the edges. The active sprint's band ends
   *  at the snapshot; its *plan* is a dashed line, never the band itself. */
  drawSprintBands(ctx, g) {
    if (!this.sprints.length) return;
    const top = g.laneTop;
    const bottom = g.height;
    const labelY = AXIS_H + HIST_H + 11;
    // prd/68 F-15: sprint bands overlap by a few days, so label text used to be
    // painted on top of the previous one (`S15621`/`S16256`). Track the right
    // edge of the last label like `drawAxis` does and skip a colliding one.
    let lastLabelRight = -Infinity;
    this.sprints.forEach((sprint, index) => {
      if (!sprint.hasBand) return;
      const left = Math.max(g.gutter, this.px(sprint.start));
      const right = Math.min(g.gutter + g.plotW, this.px(sprint.end));
      if (right <= left) return;
      const focused = this.sprintFocus != null
        && String(sprint.id) === String(this.sprintFocus);
      const dimmed = this.sprintFocus != null && !focused;
      // prd/37 §4: the selected band is marked, the others recede
      ctx.fillStyle = focused
        ? color('--canvas-band-focus')
        : dimmed
          ? color('--canvas-band-dim')
          : (index % 2 === 0 ? color('--canvas-band') : color('--canvas-band-alt'));
      ctx.fillRect(left, top, right - left, bottom - top);
      ctx.fillStyle = color('--canvas-spine');
      ctx.fillRect(left, top, 1, bottom - top);
      if (right < g.gutter + g.plotW) ctx.fillRect(right - 1, top, 1, bottom - top);
      // the plan of an active sprint: a dashed boundary, not a measured edge
      if (sprint.active && Number.isFinite(sprint.plannedEnd)
          && sprint.plannedEnd > this.view.start && sprint.plannedEnd < this.view.end) {
        const planX = Math.round(this.px(sprint.plannedEnd)) + 0.5;
        ctx.save();
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = color('--canvas-plan-line');
        ctx.beginPath();
        ctx.moveTo(planX, top);
        ctx.lineTo(planX, bottom);
        ctx.stroke();
        ctx.restore();
      }
      if (right - left >= 30) {
        ctx.font = `600 ${Math.max(10, g.label - 1)}px "JetBrains Mono", ui-monospace, monospace`;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        const text = fit(
          ctx, `S${sprint.id}${sprint.active ? ' · active' : ''}${focused ? ' · lens' : ''}`,
          right - left - 12,
        );
        const labelLeft = left + 6;
        const labelWidth = ctx.measureText(text).width;
        // a label whose box would touch the last one is skipped, not overdrawn
        if (text && labelLeft >= lastLabelRight + 4) {
          ctx.fillStyle = color('--canvas-region-label');
          ctx.fillText(text, labelLeft, labelY);
          lastLabelRight = labelLeft + labelWidth;
        }
      }
      // pushed before the lanes, so a bar / event / story hit always wins
      this.hits.push({
        kind: 'sprint', x0: left, x1: right,
        y0: top - HEADER_H, y1: bottom, sprint,
      });
    });
  }

  /** B7: two legible label rows, measured and edge-clamped so nothing clips. */
  drawAxis(ctx, g) {
    const span = this.span();
    let step = STEPS[STEPS.length - 1];
    for (const candidate of STEPS) {
      if (span / candidate <= 9) { step = candidate; break; }
    }
    this.tickStep = step;

    const first = Math.ceil(this.view.start / step) * step;
    ctx.font = `500 ${Math.max(10, g.label - 1)}px "JetBrains Mono", ui-monospace, monospace`;
    ctx.textBaseline = 'middle';
    const right = g.gutter + g.plotW;
    const clockRow = AXIS_H - 13;
    const dayRow = 12;
    let lastRight = -Infinity;
    let lastDay = null;

    ctx.fillStyle = color('--canvas-muted');
    ctx.textAlign = 'right';
    ctx.fillText('UTC', g.gutter - 10, clockRow);

    for (let t = first; t <= this.view.end; t += step) {
      const x = this.px(t);
      if (x < g.gutter || x > right) continue;
      const major = step >= DAY || new Date(t).getUTCHours() === 0;
      ctx.strokeStyle = major ? color('--canvas-grid-major') : color('--canvas-grid-minor');
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, AXIS_H - 7);
      ctx.lineTo(Math.round(x) + 0.5, g.height - BOTTOM);
      ctx.stroke();

      const label = fmtTick(t, step);
      ctx.font = `500 ${Math.max(10, g.label - 1)}px "JetBrains Mono", ui-monospace, monospace`;
      const width = ctx.measureText(label).width;
      let left = x + 4;
      if (left + width > right - 2) left = x - 4 - width;
      if (left < g.gutter || left < lastRight + 6) continue;
      ctx.textAlign = 'left';
      ctx.fillStyle = major ? color('--canvas-label') : color('--canvas-muted');
      ctx.fillText(label, left, clockRow);
      lastRight = left + width;

      if (step < DAY && new Date(t).getUTCHours() === 0) {
        const day = fmtDay(t);
        if (day !== lastDay) {
          lastDay = day;
          const dayWidth = ctx.measureText(day).width;
          let dayLeft = x + 4;
          if (dayLeft + dayWidth > right - 2) dayLeft = x - 4 - dayWidth;
          if (dayLeft >= g.gutter && dayLeft + dayWidth < right) {
            ctx.fillStyle = color('--canvas-label');
            ctx.font = `600 ${Math.max(10, g.label - 1)}px "JetBrains Mono", ui-monospace, monospace`;
            ctx.fillText(day, dayLeft, dayRow);
            ctx.font = `500 ${Math.max(10, g.label - 1)}px "JetBrains Mono", ui-monospace, monospace`;
          }
        }
      }
    }
    ctx.strokeStyle = color('--canvas-axis-line');
    ctx.beginPath();
    ctx.moveTo(g.gutter, AXIS_H - 0.5);
    ctx.lineTo(g.width, AXIS_H - 0.5);
    ctx.stroke();
  }

  drawHistogram(ctx, g) {
    const buckets = new Map();
    let max = 0;
    let total = 0;
    for (const event of this.events) {
      if (event.t < this.view.start || event.t > this.view.end) continue;
      const state = this.stateOf(event.activity);
      if (state === 'hide') continue;
      total += 1;
      const x = Math.floor(this.px(event.t));
      const n = (buckets.get(x) || 0) + (state === 'faint' ? 0.35 : 1);
      buckets.set(x, n);
      if (n > max) max = n;
    }
    const top = AXIS_H + 5;
    const height = HIST_H - 13;
    ctx.font = `500 ${Math.max(9, g.label - 1)}px "JetBrains Mono", ui-monospace, monospace`;
    ctx.fillStyle = color('--canvas-muted');
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillText(`${total} event${total === 1 ? '' : 's'}`, g.gutter - 10, top + height / 2);

    for (const [x, n] of buckets) {
      const h = Math.max(2, Math.round((n / Math.max(1, max)) * height));
      ctx.fillStyle = n > 1 ? color('--canvas-hist') : color('--canvas-hist-weak');
      ctx.fillRect(x, top + height - h, 2, h);
    }
    // brushed range: visible in the strip and faintly across the lanes
    const range = this.brushing || this.selection;
    if (range && range.end > range.start) {
      const x0 = Math.max(g.gutter, this.px(range.start));
      const x1 = Math.min(g.gutter + g.plotW, this.px(range.end));
      if (x1 > x0) {
        ctx.fillStyle = color('--canvas-brush-veil');
        ctx.fillRect(x0, AXIS_H, x1 - x0, g.height - AXIS_H - BOTTOM);
        ctx.fillStyle = this.brushing ? color('--canvas-brush') : color('--canvas-brush-idle');
        ctx.fillRect(x0, top, x1 - x0, height);
        ctx.strokeStyle = color('--canvas-brush-edge');
        ctx.lineWidth = 1;
        for (const x of [x0, x1]) {
          ctx.beginPath();
          ctx.moveTo(Math.round(x) + 0.5, AXIS_H);
          ctx.lineTo(Math.round(x) + 0.5, g.height - BOTTOM);
          ctx.stroke();
        }
      }
    }
    ctx.strokeStyle = color('--canvas-hairline');
    ctx.beginPath();
    ctx.moveTo(g.gutter, AXIS_H + HIST_H - 0.5);
    ctx.lineTo(g.width, AXIS_H + HIST_H - 0.5);
    ctx.stroke();
    this.hits.push({
      kind: 'histogram', x0: g.gutter, x1: g.gutter + g.plotW,
      y0: AXIS_H, y1: AXIS_H + HIST_H,
    });
  }

  drawLanes(ctx, g, m) {
    const rows = g.lanes;
    ctx.font = `500 ${g.label}px "JetBrains Mono", ui-monospace, monospace`;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'right';
    ctx.fillStyle = color('--canvas-muted');
    const narrow = g.gutter < 140;
    // The section header is fitted to the gutter: `STORIES · 4,000 stories ·
    // 0 PRs` is wider than 176px, and a clipped "ES · 4000 stories" reads like
    // a different word.
    ctx.fillText(
      fit(ctx, laneHeader(this.dim, rows, { narrow }), g.gutter - 12),
      g.gutter - 10,
      AXIS_H + HIST_H + 11,
    );
    // W-01 (prd/67): the honest counterpart of a short lane list. Its own row
    // under the lane header keeps it clear of the section header and of any
    // sprint band label (a phone has no free space on the header row).
    if (g.hiddenPeople > 0) {
      ctx.font = `500 ${Math.max(9, g.label - 2)}px "JetBrains Mono", ui-monospace, monospace`;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = color('--canvas-muted');
      ctx.fillText(
        fit(ctx, actorHiddenNote(g.hiddenPeople), g.plotW - 20),
        g.gutter + 6,
        AXIS_H + HIST_H + HEADER_H + g.noteH / 2 + 1,
      );
      ctx.font = `500 ${g.label}px "JetBrains Mono", ui-monospace, monospace`;
    }

    // prd/33 §3: only the rows inside the viewport (plus overscan) are drawn,
    // so the cost of a frame is O(visible lanes), not O(store).
    const { first, last } = g.window.lanes;
    this.withLaneClip(ctx, g, () => {
      for (let i = first; i < last; i += 1) {
        const row = rows[i];
        const y = g.laneTop + i * g.laneH - g.scrollTop;
        if (i % 2 === 0) {
          ctx.fillStyle = color('--canvas-row');
          ctx.fillRect(g.gutter, y, g.plotW, g.laneH);
        }
        ctx.strokeStyle = color('--canvas-row-rule');
        ctx.beginPath();
        ctx.moveTo(g.gutter, Math.round(y + g.laneH) - 0.5);
        ctx.lineTo(g.width, Math.round(y + g.laneH) - 0.5);
        ctx.stroke();

        const hovered = this.hover && this.hover.laneKey === row.key;
        ctx.font = `500 ${g.label}px "JetBrains Mono", ui-monospace, monospace`;
        // A ticket key is a doorway (prd/25): the label is drawn in the link hue
        // and the gutter is a hit target that opens the ticket detail page.
        const linked = Boolean(row.ticket);
        ctx.fillStyle = hovered ? color('--canvas-strong') : linked ? color('--link') : color('--canvas-label');
        const text = laneLabel(row, { narrow, dim: this.dim });
        if (narrow && this.dim === 'story') {
          // The story hierarchy is drawn in the gutter: epic (◆) → story (▸) →
          // PR. Left alignment + indent is what makes "under its epic" visible;
          // right-aligned labels erased the nesting on a phone.
          const depth = row.depth || 0;
          const x = 5 + depth * 9;
          ctx.textAlign = 'left';
          ctx.fillText(fit(ctx, text, g.gutter - x - 5), x, y + g.laneH / 2);
        } else {
          ctx.textAlign = 'right';
          const label = fit(ctx, text, narrow ? g.gutter - 12 : g.gutter - 22);
          ctx.fillText(label, g.gutter - 10, y + g.laneH / 2);
        }
        if (linked) {
          this.hits.push({
            kind: 'label', x0: 0, x1: g.gutter, y0: y, y1: y + g.laneH,
            ticket: row.ticket, laneKey: row.key,
          });
        }
      }
    });

    if (this.dim === 'queue') {
      this.withLaneClip(ctx, g, () => this.drawQueueLanes(ctx, g, rows));
      return;
    }
    if (this.dim === 'story') {
      this.withLaneClip(ctx, g, () => this.drawStoryLanes(ctx, g, rows));
      return;
    }
    if (this.dim === 'object') {
      this.withLaneClip(ctx, g, () => this.drawObjectLanes(ctx, g, rows));
      return;
    }
    this.withLaneClip(ctx, g, () => this.drawActorLanes(ctx, g, rows));
  }

  drawActorLanes(ctx, g, rows) {
    const { first, last } = g.window.lanes;
    // Scrolled past the lanes into the PR-lifecycle section: nothing to bucket.
    if (first >= last) { this.aggregated = 0; return; }
    // Only the actors inside the viewport can be hit, so only they are indexed
    // and only their buckets are built — the event scan stays O(events) but the
    // per-pixel map is O(visible lanes).
    const index = new Map();
    for (let i = first; i < last; i += 1) index.set(rows[i].key, i);
    const perPixel = new Map();
    for (const event of this.events) {
      if (event.t < this.view.start || event.t > this.view.end) continue;
      const state = this.stateOf(event.activity);
      if (state === 'hide') continue;
      const lane = index.get(event.actor);
      if (lane == null) continue;
      const x = Math.round(this.px(event.t));
      const key = `${lane}:${x}`;
      const bucket = perPixel.get(key);
      if (bucket) bucket.items.push(event);
      else perPixel.set(key, { lane, x, items: [event], faint: state === 'faint' });
    }
    let aggregated = 0;
    for (const bucket of perPixel.values()) {
      const y = g.laneTop + bucket.lane * g.laneH - g.scrollTop + g.laneH / 2;
      if (bucket.items.length > 1) {
        aggregated += bucket.items.length;
        const fromX = this.px(Math.min(...bucket.items.map((e) => e.t)));
        const toX = Math.max(this.px(Math.max(...bucket.items.map((e) => e.t))), fromX + 2);
        ctx.fillStyle = bucket.faint ? color('--canvas-bucket') : color('--canvas-bucket-strong');
        ctx.fillRect(fromX, y - g.laneH / 2 + 3, Math.max(2, toX - fromX), g.laneH - 6);
        this.hits.push({
          kind: 'bucket', x0: fromX, x1: toX, y0: y - g.laneH / 2, y1: y + g.laneH / 2,
          items: bucket.items, step: this.span() / g.plotW,
          laneKey: rows[bucket.lane].key,
        });
        continue;
      }
      const event = bucket.items[0];
      const hue = ACTIVITY_COLOR[event.activity] || color('--cat-7');
      const r = g.marker;
      ctx.beginPath();
      ctx.arc(bucket.x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = bucket.faint ? rgba(hue, 0.2) : hue;
      ctx.fill();
      if (this.hover && this.hover.event && this.hover.event.key === event.key) {
        ctx.strokeStyle = color('--canvas-strong');
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      this.hits.push({ kind: 'event', x: bucket.x, y, r: r + 4, event, laneKey: rows[bucket.lane].key });
    }
    this.aggregated = aggregated;
  }

  drawObjectLanes(ctx, g, rows) {
    const { first, last } = g.window.lanes;
    for (let i = first; i < last; i += 1) {
      const row = rows[i];
      const pr = row.pr;
      if (!pr) continue;
      const y = g.laneTop + i * g.laneH - g.scrollTop;
      const barY = y + 3;
      const barH = g.laneH - 6;
      this.paintLifecycle(ctx, g, pr, barY, barH);
      if (barH >= 9) {
        for (const event of pr.events) {
          if (event.t < this.view.start || event.t > this.view.end) continue;
          const state = this.stateOf(event.activity);
          if (state === 'hide') continue;
          const x = this.px(event.t);
          ctx.fillStyle = rgba(ACTIVITY_COLOR[event.activity] || color('--cat-7'), state === 'faint' ? 0.2 : 0.85);
          ctx.fillRect(x, barY, 1.5, barH);
        }
      }
      const end = pr.closed || this.now;
      this.hits.push({
        kind: 'bar', x0: this.px(pr.created), x1: Math.max(this.px(end), this.px(pr.created) + 2),
        y0: y, y1: y + g.laneH, pr, laneKey: row.key,
      });
    }
  }

  drawStoryLanes(ctx, g, rows) {
    this.aggregated = 0;
    const { first, last } = g.window.lanes;
    for (let i = first; i < last; i += 1) {
      const row = rows[i];
      const y = g.laneTop + i * g.laneH - g.scrollTop;
      const barY = y + 3;
      const barH = g.laneH - 6;
      if (row.kind === 'epic') {
        // prd/33 §5.3: the members were a scan of every row per epic row; the
        // index is built once per row build (see storyRows).
        const prs = (this._epicPrs && this._epicPrs.get(row.epic)) || [];
        if (!prs.length) continue;
        const from = Math.min(...prs.map((pr) => pr.created));
        const to = Math.max(...prs.map((pr) => pr.closed || this.now));
        const x0 = Math.max(g.gutter, this.px(from));
        const x1 = Math.min(g.gutter + g.plotW, this.px(to));
        if (x1 <= x0) continue;
        // A bracket, not a lifecycle: the epic has no phases of its own.
        ctx.fillStyle = color('--canvas-epic');
        ctx.fillRect(x0, barY, x1 - x0, barH);
        ctx.fillStyle = color('--canvas-epic-edge');
        ctx.fillRect(x0, barY, 2, barH);
        ctx.fillRect(x1 - 2, barY, 2, barH);
        this.hits.push({
          kind: 'epic', x0, x1, y0: y, y1: y + g.laneH,
          epic: row.epic, ticket: row.ticket, laneKey: row.key,
        });
        continue;
      }
      if (row.kind === 'story') {
        const prs = row.prs || [];
        const storyEvents = row.events || [];
        if (prs.length) {
          // One thin lifecycle per PR, stacked inside the story lane: overlapping
          // PRs stay individually visible instead of overpainting each other.
          const shown = prs.slice(0, 6);
          const slice = Math.max(2, barH / shown.length);
          shown.forEach((pr, index) => {
            this.paintLifecycle(ctx, g, pr, barY + index * slice, Math.max(2, slice - 1));
          });
        } else if (storyEvents.length) {
          // A story with no code still has a lane: a neutral baseline for its
          // own life, so its events have something to sit on.
          const first = storyEvents[0].t;
          const last = storyEvents[storyEvents.length - 1].t;
          const x0 = Math.max(g.gutter, this.px(first));
          const x1 = Math.min(g.gutter + g.plotW, this.px(last));
          if (x1 > x0) {
            ctx.fillStyle = color('--canvas-baseline');
            ctx.fillRect(x0, barY + barH / 2 - 1, x1 - x0, 2);
          }
        }
        // story → event: the ticket's own markers on the story lane
        for (const event of storyEvents) {
          if (event.t < this.view.start || event.t > this.view.end) continue;
          const state = this.stateOf(event.activity);
          if (state === 'hide') continue;
          const x = this.px(event.t);
          ctx.fillStyle = rgba(
            ACTIVITY_COLOR[event.activity] || color('--cat-7'),
            state === 'faint' ? 0.25 : 0.9,
          );
          ctx.fillRect(x, barY, 1.5, barH);
        }
        const times = prs.length
          ? prs.flatMap((pr) => [pr.created, pr.closed || this.now])
          : storyEvents.map((event) => event.t);
        if (!times.length) continue;
        const membership = this.sprintStory.get(row.story);
        if (membership && membership.carried_over) {
          // prd/26 P6: a story re-added across sprints is marked at its end —
          // the sprint plan's cost, on the lane the investigator is reading.
          const endX = Math.min(g.gutter + g.plotW - 2, this.px(Math.max(...times)));
          ctx.fillStyle = color('--canvas-carry');
          ctx.fillRect(endX - 1, barY, 2, barH);
          ctx.font = `700 ${Math.max(9, g.label)}px "JetBrains Mono", ui-monospace, monospace`;
          ctx.textAlign = 'right';
          ctx.textBaseline = 'middle';
          ctx.fillStyle = color('--warning');
          ctx.fillText('↻', endX - 4, y + g.laneH / 2);
        }
        this.hits.push({
          kind: 'story', x0: this.px(Math.min(...times)),
          x1: Math.max(this.px(Math.max(...times)), this.px(Math.min(...times)) + 2),
          y0: y, y1: y + g.laneH, story: row.story, ticket: row.ticket,
          prs, events: storyEvents, membership, laneKey: row.key,
        });
        continue;
      }
      const pr = row.pr;
      if (!pr) continue;
      this.paintLifecycle(ctx, g, pr, barY, barH);
      if (barH >= 9) {
        for (const event of pr.events) {
          if (event.t < this.view.start || event.t > this.view.end) continue;
          const state = this.stateOf(event.activity);
          if (state === 'hide') continue;
          const x = this.px(event.t);
          ctx.fillStyle = rgba(ACTIVITY_COLOR[event.activity] || color('--cat-7'), state === 'faint' ? 0.2 : 0.85);
          ctx.fillRect(x, barY, 1.5, barH);
        }
      }
      const end = pr.closed || this.now;
      this.hits.push({
        kind: 'bar', x0: this.px(pr.created),
        x1: Math.max(this.px(end), this.px(pr.created) + 2),
        y0: y, y1: y + g.laneH, pr, laneKey: row.key,
      });
    }
  }

  drawQueueLanes(ctx, g, rows) {
    const index = new Map(rows.map((r, i) => [r.key, i]));
    for (const pr of this.prs) {
      for (const seg of this.lifecycle(pr)) {
        if (seg.to <= seg.from) continue;
        const queue = this.queueOf(seg);
        const lane = index.get(queue);
        if (lane == null) continue;
        if (seg.to < this.view.start || seg.from > this.view.end) continue;
        const y = g.laneTop + lane * g.laneH - g.scrollTop;
        const x0 = Math.max(g.gutter, this.px(seg.from));
        const x1 = Math.min(g.gutter + g.plotW, this.px(seg.to));
        if (x1 <= x0) continue;
        const hue = PHASE[seg.phase].color;
        ctx.fillStyle = rgba(hue, seg.phase === 'unobserved' ? 0.14 : 0.3);
        ctx.fillRect(x0, y + 4, Math.max(1, x1 - x0), g.laneH - 8);
        ctx.fillStyle = rgba(hue, seg.phase === 'unobserved' ? 0.25 : 0.75);
        ctx.fillRect(x0, y + 4, Math.min(2, x1 - x0), g.laneH - 8);
        this.hits.push({
          kind: 'interval', x0, x1, y0: y, y1: y + g.laneH, interval: seg, pr, queue,
          laneKey: queue,
        });
      }
    }
  }

  /** Bar segments + end caps, shared by the gantt section and object lanes. */
  paintLifecycle(ctx, g, pr, barY, barH) {
    const segments = this.lifecycle(pr);
    for (const seg of segments) {
      const width = seg.to - seg.from;
      if (width <= 0) continue;
      if (seg.to < this.view.start || seg.from > this.view.end) continue;
      const x0 = Math.max(g.gutter, this.px(seg.from));
      const x1 = Math.min(g.gutter + g.plotW, this.px(seg.to));
      if (x1 <= x0) continue;
      const hue = PHASE[seg.phase].color;
      if (seg.phase === 'unobserved') {
        // hatched, because nothing was measured here
        ctx.fillStyle = rgba(hue, 0.10);
        ctx.fillRect(x0, barY, Math.max(1, x1 - x0), barH);
        ctx.fillStyle = rgba(hue, 0.28);
        for (let x = x0 + 2; x < x1; x += 6) ctx.fillRect(x, barY, 1, barH);
        continue;
      }
      ctx.fillStyle = rgba(hue, seg.phase === 'closed' ? 0.22 : 0.34);
      ctx.fillRect(x0, barY, Math.max(1, x1 - x0), barH);
      // saturated cap keeps phase boundaries legible without a solid block
      ctx.fillStyle = rgba(hue, 0.9);
      ctx.fillRect(x0, barY, Math.min(2, x1 - x0), barH);
    }
    const end = pr.closed || this.now;
    if (pr.closed) {
      const x = Math.min(g.gutter + g.plotW, this.px(end));
      ctx.fillStyle = rgba(PHASE.closed.color, 0.9);
      ctx.fillRect(x - 2, barY, 2.5, barH);
    } else {
      const x = Math.min(g.gutter + g.plotW, this.px(end));
      ctx.fillStyle = rgba(color('--warning'), 0.75);
      ctx.beginPath();
      ctx.moveTo(x, barY);
      ctx.lineTo(x + 4, barY + barH / 2);
      ctx.lineTo(x, barY + barH);
      ctx.closePath();
      ctx.fill();
    }
    if (this.focusPrId === pr.id) {
      // prd/38 #20: the PR the drawer's Timeline chip came from stays marked.
      const start = Math.max(g.gutter, this.px(pr.created));
      const stop = Math.min(g.gutter + g.plotW, this.px(end));
      ctx.strokeStyle = rgba(color('--focus-ring'), 0.95);
      ctx.lineWidth = 1.5;
      ctx.strokeRect(start - 1, barY - 1.5, Math.max(2, stop - start) + 2, barH + 3);
    }
  }

  drawBars(ctx, g, m) {
    // The PR lifecycle section is a virtual list too: it sits below the lanes
    // in the same scroll, and at 5,240 PRs it is another 78k px of rows.
    const { first, last } = g.window.bars;
    this.withLaneClip(ctx, g, () => {
      ctx.font = `600 ${Math.max(10, g.label - 1)}px Inter, system-ui, sans-serif`;
      ctx.textAlign = 'right';
      ctx.fillStyle = color('--canvas-muted');
      ctx.fillText(
        g.gutter < 120 ? `PRs ${g.barRows.length}` : `PR LIFECYCLE · ${g.barRows.length}`,
        g.gutter - 10,
        g.barTop - g.scrollTop - 11,
      );

      for (let i = first; i < last; i += 1) {
        const pr = g.barRows[i];
        const y = g.barTop + i * g.barH - g.scrollTop;
        const isHover = this.hover && this.hover.pr === pr;
        if (i % 2 === 0 || isHover) {
          ctx.fillStyle = isHover ? color('--canvas-row-hover') : color('--canvas-row');
          ctx.fillRect(g.gutter, y, g.plotW, g.barH);
        }

        ctx.font = `500 ${g.label}px "JetBrains Mono", ui-monospace, monospace`;
        ctx.textAlign = 'right';
        ctx.fillStyle = isHover ? color('--canvas-strong') : color('--canvas-label');
        const key = pr.ticket ? `${pr.ticket} ` : '';
        const label = g.gutter < 140
          ? (pr.ticket ? `${pr.ticket} #${pr.number}` : `#${pr.number}`)
          : `${key}#${pr.number} ${pr.repo}`;
        ctx.fillText(fit(ctx, label, g.gutter < 140 ? g.gutter - 12 : g.gutter - 22), g.gutter - 10, y + g.barH / 2);

        const barY = y + 2;
        const barH = g.barH - 4;
        this.paintLifecycle(ctx, g, pr, barY, barH);

        if (barH >= 9) {
          for (const event of pr.events) {
            if (event.t < this.view.start || event.t > this.view.end) continue;
            const state = this.stateOf(event.activity);
            if (state === 'hide') continue;
            const x = this.px(event.t);
            ctx.fillStyle = rgba(ACTIVITY_COLOR[event.activity] || color('--cat-7'), state === 'faint' ? 0.2 : 0.85);
            ctx.fillRect(x, barY, 1.5, barH);
          }
        }
        const end = pr.closed || this.now;
        this.hits.push({
          kind: 'bar', x0: this.px(pr.created), x1: Math.max(this.px(end), this.px(pr.created) + 2),
          y0: y, y1: y + g.barH, pr,
        });
        if (pr.ticket) {
          this.hits.push({
            kind: 'label', x0: 0, x1: g.gutter, y0: y, y1: y + g.barH,
            ticket: pr.ticket, laneKey: `bar:${pr.id}`,
          });
        }
      }
    });
  }

  // ---------------------------------------------------------------- hover
  hitTest(cx, cy) {
    for (let i = this.hits.length - 1; i >= 0; i -= 1) {
      const hit = this.hits[i];
      if (hit.dead) continue;
      if (hit.kind === 'event') {
        if (Math.hypot(cx - hit.x, cy - hit.y) <= hit.r) return hit;
      } else if (cx >= hit.x0 - 1 && cx <= hit.x1 + 1 && cy >= hit.y0 && cy <= hit.y1) {
        return hit;
      }
    }
    return null;
  }

  showHover(hit, cx, cy) {
    if (!hit) {
      this.clearHover();
      return;
    }
    this.hover = hit.kind === 'bar' ? { pr: hit.pr }
      : hit.kind === 'event' ? { event: hit.event }
        : hit.kind === 'interval' ? { interval: hit.interval, pr: hit.pr }
          : hit.kind === 'sprint' ? { sprint: hit.sprint }
            : { laneKey: hit.laneKey };
    if (hit.laneKey) this.hover.laneKey = hit.laneKey;

    if (hit.kind === 'event') {
      const event = hit.event;
      const attrs = Object.entries(event.attributes || {});
      this.tip.innerHTML = `
        <div class="row"><span class="act" style="color:${ACTIVITY_COLOR[event.activity] || color('--cat-7')}">${esc(event.activity)}</span>
          <span class="muted">${esc(fmtStamp(event.t))}</span></div>
        <div class="row"><span class="k">actor</span><span class="v">${esc(event.actor)}</span></div>
        <div class="row"><span class="k">objects</span><span class="v">${event.objects.map(esc).join('<br>')}</span></div>
        ${attrs.length ? `<div class="row"><span class="k">attrs</span><span class="v">${attrs
          .map(([k, v]) => `${esc(k)}=${esc(v)}`).join('<br>')}</span></div>` : ''}`;
    } else if (hit.kind === 'bucket') {
      const first = hit.items[0];
      const last = hit.items[hit.items.length - 1];
      this.tip.innerHTML = `
        <div class="row"><span class="act">${hit.items.length} events</span>
          <span class="muted">≤ ${esc(fmtCalendarDuration(hit.step))} apart</span></div>
        <div class="row"><span class="k">lane</span><span class="v">${esc(hit.laneKey || '')}</span></div>
        <div class="row"><span class="k">window</span><span class="v">${esc(fmtStamp(first.t))}<br>${esc(fmtStamp(last.t))}</span></div>
        <div class="row"><span class="k">zoom</span><span class="v">zoom in for individual events</span></div>`;
    } else if (hit.kind === 'interval') {
      const seg = hit.interval;
      const pr = hit.pr;
      this.tip.innerHTML = `
        <div class="row"><span class="act" style="color:${PHASE[seg.phase].color}">${esc(PHASE[seg.phase].label)}</span>
          <span class="muted">${esc(fmtCalendarDuration(seg.to - seg.from))}</span></div>
        <div class="row"><span class="k">queue</span><span class="v">${esc(hit.queue)}</span></div>
        <div class="row"><span class="k">pr</span><span class="v">${pr.ticket ? `${esc(pr.ticket)} ` : ''}#${esc(pr.number)} ${esc(pr.repo)}</span></div>
        <div class="row"><span class="k">window</span><span class="v">${esc(fmtStamp(seg.from))}<br>${esc(fmtStamp(seg.to))}</span></div>
        <div class="row"><span class="k">basis</span><span class="v">${esc(PHASE[seg.phase].hint)} · calendar time (wall clock)</span></div>`;
    } else if (hit.kind === 'label') {
      this.tip.innerHTML = `
        <div class="row"><span class="act mono">${esc(hit.ticket)}</span>
          <span class="muted">ticket key</span></div>
        <div class="row"><span class="k">open</span><span class="v">click to open the ticket detail page</span></div>`;
    } else if (hit.kind === 'sprint') {
      const sprint = hit.sprint;
      this.tip.innerHTML = `
        <div class="row"><span class="act">Sprint ${esc(sprint.id)}</span>
          <span class="muted">${esc(sprint.state || (sprint.active ? 'active' : 'closed'))}</span></div>
        <div class="row"><span class="k">band</span><span class="v">${esc(fmtStamp(sprint.start))}<br>${esc(fmtStamp(sprint.end))}${sprint.open ? ' · at the snapshot' : ''}</span></div>
        ${sprint.active && Number.isFinite(sprint.plannedEnd)
          ? `<div class="row"><span class="k">plan</span><span class="v">ends ${esc(fmtStamp(sprint.plannedEnd))} — dashed line, not measured</span></div>` : ''}
        ${sprint.lengthOutlier && sprint.lengthNote
          ? `<div class="row"><span class="k">length</span><span class="v">${esc(sprint.lengthNote)}</span></div>` : ''}
        <div class="row"><span class="k">scope</span><span class="v">${sprint.stories} stor${sprint.stories === 1 ? 'y' : 'ies'} · ${sprint.points} points</span></div>
        <div class="row"><span class="k">by end</span><span class="v">${sprint.doneByEnd} done by end${sprint.doneLate ? ` · ${sprint.doneLate} late` : ''}${sprint.neverDone ? ` · ${sprint.neverDone} still open` : ''}</span></div>
        ${sprint.doneLate && sprint.lateHours ? `<div class="row"><span class="k">slip</span><span class="v">${esc(fmtHours(sprint.lateHours))} after the close</span></div>` : ''}
        ${sprint.carriedIn.length
          ? `<div class="row"><span class="k">carried in</span><span class="v">${esc(sprint.carriedIn.join(', '))}</span></div>` : ''}
        ${sprint.carriedOut.length
          ? `<div class="row"><span class="k">carried out</span><span class="v">${esc(sprint.carriedOut.join(', '))}</span></div>` : ''}
        <div class="row"><span class="k">open</span><span class="v">click to filter the timeline to this sprint</span></div>`;
    } else if (hit.kind === 'story' || hit.kind === 'epic') {
      const prs = hit.kind === 'story'
        ? (hit.prs || [])
        : ((this._epicPrs && this._epicPrs.get(hit.epic)) || []);
      const totals = new Map();
      for (const pr of prs) {
        for (const [phase, ms] of this.phases(pr)) totals.set(phase, (totals.get(phase) || 0) + ms);
      }
      const from = prs.length ? Math.min(...prs.map((pr) => pr.created)) : null;
      const to = prs.length ? Math.max(...prs.map((pr) => pr.closed || this.now)) : null;
      this.tip.innerHTML = `
        <div class="row"><span class="act mono">${esc(hit.ticket)}</span>
          <span class="muted">${hit.kind === 'epic' ? 'epic' : 'story'} · ${prs.length} PR${prs.length === 1 ? '' : 's'}</span></div>
        ${from != null ? `<div class="row"><span class="k">span</span><span class="v">${esc(fmtCalendarDuration(to - from))}</span></div>` : ''}
        ${hit.kind === 'story' ? sprintRow(hit.membership) : ''}
        ${[...totals.entries()].filter(([, ms]) => ms > 0).map(([phase, ms]) => `
          <div class="row"><span class="k">${esc(PHASE[phase].label)}</span>
            <span class="v" style="color:${PHASE[phase].color}">${esc(fmtCalendarDuration(ms))}</span></div>`).join('')}
        <div class="row"><span class="k">open</span><span class="v">click for the ticket detail page</span></div>`;
    } else {
      const pr = hit.pr;
      const totals = this.phases(pr);
      const segments = this.lifecycle(pr);
      const total = segments.length ? segments[segments.length - 1].to - segments[0].from : 0;
      const measured = [...totals.entries()].filter(([phase]) => phase !== 'unobserved');
      const unobserved = totals.get('unobserved') || 0;
      this.tip.innerHTML = `
        <div class="row"><span class="act">#${pr.number} ${esc(pr.repo)}</span>
          <span class="muted">${esc(pr.state)}${pr.closed ? '' : ' · right-censored'}</span></div>
        <div class="row"><span class="k">ticket</span><span class="v">${esc(pr.ticket) || '—'}</span></div>
        ${pr.epic ? `<div class="row"><span class="k">epic</span><span class="v">${esc(pr.epic)}</span></div>` : ''}
        <div class="row"><span class="k">author</span><span class="v">${esc(pr.actor)}</span></div>
        <div class="row"><span class="k">cycle</span><span class="v">${esc(fmtCalendarDuration(total))} <span class="muted">calendar time</span></span></div>
        ${measured.map(([phase, ms]) => `<div class="row"><span class="k">${esc(PHASE[phase].label)}</span>
          <span class="v" style="color:${PHASE[phase].color}">${esc(fmtCalendarDuration(ms))}</span></div>`).join('')}
        ${unobserved ? `<div class="row"><span class="k">no events</span><span class="v" style="color:${PHASE.unobserved.color}">${esc(fmtCalendarDuration(unobserved))} · not measured</span></div>` : ''}`;
    }
    this.tip.hidden = false;
    const stage = this.canvas.parentElement;
    const w = this.tip.offsetWidth || 320;
    const h = this.tip.offsetHeight || 120;
    const left = Math.min(Math.max(8, cx + 14), stage.clientWidth - w - 8);
    const top = Math.min(Math.max(8, cy + 14), stage.clientHeight - h - 8);
    this.tip.style.left = `${left}px`;
    this.tip.style.top = `${top}px`;
  }

  // ---------------------------------------------------------------- input
  /** A browser fires compatibility mouse events after a touch; ignore them. */
  _recentTouch() {
    return this._touchAt > 0 && Date.now() - this._touchAt < 800;
  }

  /** What a click/tap on a hit does (shared by mouse and touch). */
  _activate(hit) {
    if (!hit) return false;
    // prd/25: a ticket label (or a story/epic bar) is a link to the ticket
    // detail page; a PR bar still opens the PR drawer.
    if ((hit.kind === 'label' || hit.kind === 'story' || hit.kind === 'epic')
        && hit.ticket && this.onTicket) {
      this.onTicket(hit.ticket);
      return true;
    }
    // prd/26 P6: clicking a sprint band scopes the timeline to that sprint
    if (hit.kind === 'sprint' && this.onSprint) {
      this.onSprint(hit.sprint);
      return true;
    }
    if ((hit.kind === 'bar' || hit.kind === 'interval') && this.onSelect) {
      this.onSelect(hit.pr, { phase: hit.interval ? hit.interval.phase : null, kind: hit.kind });
      return true;
    }
    return false;
  }

  _pinchState() {
    const points = [...this._pointers.values()];
    if (points.length < 2) return null;
    const [a, b] = points;
    return {
      dist: Math.hypot(a.x - b.x, a.y - b.y),
      x: (a.x + b.x) / 2,
      y: (a.y + b.y) / 2,
    };
  }

  _bind() {
    const canvas = this.canvas;

    canvas.addEventListener('mousemove', (ev) => {
      if (this._recentTouch()) return;
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      if (this.brushing) {
        this.brushing = {
          start: Math.min(this.brushAnchor, this.timeAt(x)),
          end: Math.max(this.brushAnchor, this.timeAt(x)),
        };
        this.schedule();
        return;
      }
      if (this.drag) {
        const dx = x - this.drag.x;
        this.drag.x = x;
        this.panPixels(dx);
        this.tip.hidden = true;
        return;
      }
      const hit = this.hitTest(x, y);
      const linked = hit && (hit.kind === 'label' || hit.kind === 'story'
        || hit.kind === 'epic' || hit.kind === 'sprint');
      canvas.style.cursor = linked ? 'pointer' : '';
      this.showHover(hit, x, y);
      this.schedule();
    });

    canvas.addEventListener('mouseleave', () => {
      if (this._recentTouch()) return;
      this.clearHover();
      this.schedule();
    });

    canvas.addEventListener('mousedown', (ev) => {
      if (this._recentTouch()) return;
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      const onStrip = this.geom && y >= AXIS_H && y <= AXIS_H + HIST_H && x >= this.geom.gutter;
      if (onStrip && ev.button === 0) {
        const anchor = this.timeAt(x);
        this.brushing = { start: anchor, end: anchor };
        this.brushAnchor = anchor;
        this.clearHover();
        this.schedule();
        return;
      }
      this.drag = { x, moved: false };
      canvas.classList.add('dragging');
    });

    window.addEventListener('mouseup', (ev) => {
      if (this._recentTouch()) return;
      if (this.brushing) {
        const range = this.brushing;
        this.brushing = null;
        if (range.end - range.start > 2 * MINUTE) this.setSelection(range.start, range.end);
        else this.schedule();
        return;
      }
      if (!this.drag) return;
      const rect = canvas.getBoundingClientRect();
      const moved = Math.abs((ev.clientX - rect.left) - this.drag.x) > 3;
      this.drag = null;
      this.canvas.classList.remove('dragging');
      if (moved) return;
      const hit = this.hitTest(ev.clientX - rect.left, ev.clientY - rect.top);
      if (!hit) return;
      this._activate(hit);
    });

    canvas.addEventListener('dblclick', (ev) => {
      const rect = canvas.getBoundingClientRect();
      this.zoom(0.5, ev.clientX - rect.left);
    });

    canvas.addEventListener('wheel', (ev) => {
      if (ev.ctrlKey || ev.metaKey) return; // leave browser zoom alone
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      // prd/33 §3: the lane list is a virtual scroller now. Over the labels the
      // wheel keeps its native meaning (scroll the lanes) — otherwise a wheel
      // could never reach lane 200 of 5,240. Over the plot it still zooms.
      if (!ev.shiftKey && this.geom && x < this.geom.gutter) return;
      ev.preventDefault();
      if (ev.shiftKey) {
        this.panPixels(-ev.deltaY / 2);
        return;
      }
      const factor = Math.exp(Math.max(-0.6, Math.min(0.6, ev.deltaY * 0.0015)));
      this.zoom(factor, ev.clientX - rect.left);
    }, { passive: false });

    // B8: keyboard path — pan, zoom, fit, dismiss.
    canvas.addEventListener('keydown', (ev) => {
      const key = ev.key;
      if (key === 'ArrowLeft') { this.panFraction(-0.12); ev.preventDefault(); }
      else if (key === 'ArrowRight') { this.panFraction(0.12); ev.preventDefault(); }
      else if (key === 'PageDown') { this.scrollLanes(1); ev.preventDefault(); }
      else if (key === 'PageUp') { this.scrollLanes(-1); ev.preventDefault(); }
      else if (key === '+' || key === '=') { this.zoom(0.75); ev.preventDefault(); }
      else if (key === '-' || key === '_') { this.zoom(1.32); ev.preventDefault(); }
      else if (key === 'Home' || key === '0' || key === 'f' || key === 'F') { this.fit(); ev.preventDefault(); }
      else if (key === 'Escape') { this.clearHover(); this.clearSelection(); ev.preventDefault(); }
    });

    // prd/28 §2: touch. One finger pans horizontally, a vertical drag scrolls
    // the page (the canvas is `touch-action: pan-y`), two fingers pinch around
    // their midpoint, a short tap selects/drills, and a 450 ms hold shows the
    // tooltip the desktop gets from hover (never a hover-only affordance).
    const local = (ev) => {
      const rect = canvas.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    };
    const clearHold = () => {
      clearTimeout(this._holdTimer);
      this._holdTimer = 0;
    };
    const capture = (pointerId) => {
      try { canvas.setPointerCapture(pointerId); } catch { /* not fatal */ }
    };

    canvas.addEventListener('pointerdown', (ev) => {
      if (ev.pointerType === 'mouse') return;
      this._touchAt = Date.now();
      const point = local(ev);
      this._pointers.set(ev.pointerId, {
        ...point,
        startX: point.x, startY: point.y,
        lastX: point.x, lastY: point.y,
        moved: false, held: false, axis: null, t: Date.now(),
      });
      if (this._pointers.size > 1) {
        // Pinch owns the gesture: stop the browser scrolling under two fingers.
        clearHold();
        ev.preventDefault();
        for (const [id, pointer] of this._pointers) {
          pointer.moved = true;
          capture(id);
        }
        this._pinch = this._pinchState();
        this.tip.hidden = true;
        return;
      }
      clearHold();
      this._holdTimer = setTimeout(() => {
        const pointer = this._pointers.get(ev.pointerId);
        if (!pointer || pointer.moved || this._pointers.size > 1) return;
        pointer.held = true;
        const hit = this.hitTest(pointer.x, pointer.y);
        this.showHover(hit, pointer.x, pointer.y);
        this.schedule();
      }, 450);
    });

    canvas.addEventListener('pointermove', (ev) => {
      if (ev.pointerType === 'mouse') return;
      const pointer = this._pointers.get(ev.pointerId);
      if (!pointer) return;
      const point = local(ev);
      pointer.x = point.x;
      pointer.y = point.y;
      if (this._pointers.size > 1) {
        ev.preventDefault();
        const next = this._pinchState();
        if (this._pinch && next) {
          // spreading the fingers (dist grows) zooms in: factor < 1
          this.zoom(this._pinch.dist / Math.max(1, next.dist), next.x);
          this.panPixels(next.x - this._pinch.x);
          this.tip.hidden = true;
        }
        this._pinch = next;
        pointer.lastX = pointer.x;
        pointer.lastY = pointer.y;
        return;
      }
      if (!pointer.moved) {
        // Decide once: horizontal is a pan, vertical belongs to the page.
        const axis = touchAxis(point.x - pointer.startX, point.y - pointer.startY);
        if (!axis) {
          pointer.lastX = point.x;
          pointer.lastY = point.y;
          return;
        }
        pointer.moved = true;
        pointer.axis = axis;
        clearHold();
        if (axis === 'x') {
          capture(ev.pointerId);
          canvas.classList.add('dragging');
        }
      }
      if (pointer.axis === 'x') {
        ev.preventDefault();
        this.panPixels(point.x - pointer.lastX);
        this.tip.hidden = true;
      }
      pointer.lastX = point.x;
      pointer.lastY = point.y;
    });

    const endPointer = (ev) => {
      if (ev.pointerType === 'mouse') return;
      const pointer = this._pointers.get(ev.pointerId);
      if (!pointer) return;
      clearHold();
      this._pointers.delete(ev.pointerId);
      if (pointer.axis === 'x') canvas.classList.remove('dragging');
      if (this._pointers.size < 2) this._pinch = null;
      if (this._pointers.size === 1) {
        // the remaining finger carries on as a pan from where it is now
        const rest = [...this._pointers.values()][0];
        rest.startX = rest.x;
        rest.startY = rest.y;
        rest.lastX = rest.x;
        rest.lastY = rest.y;
        rest.moved = true;
        rest.axis = 'x';
        return;
      }
      if (this._pointers.size > 0) return;
      if (pointer.moved || pointer.held) return;
      if (Date.now() - pointer.t > 600) return;
      const hit = this.hitTest(pointer.x, pointer.y);
      if (hit) {
        this._activate(hit);
        return;
      }
      this.clearHover();
      this.schedule();
    };
    canvas.addEventListener('pointerup', endPointer);
    canvas.addEventListener('pointercancel', endPointer);

    new ResizeObserver(() => this.schedule()).observe(canvas.parentElement);

    // prd/33 §3: scrolling the lane list repaints the window (the canvas itself
    // is pinned and never moves).
    if (this.scroller && this.scroller.addEventListener) {
      this.scroller.addEventListener('scroll', () => {
        this.clearHover();
        this.schedule();
      }, { passive: true });
    }
  }
}

function countBetween(times, start, end) {
  if (!times || !times.length) return 0;
  let n = 0;
  for (const t of times) {
    if (t > start && t <= end) n += 1;
    else if (t > end) break;
  }
  return n;
}

function visibleWord(timeline) {
  const n = timeline.visibleCount();
  return `${n} of ${timeline.events.length}`;
}


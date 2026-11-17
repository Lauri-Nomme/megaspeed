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
  MINUTE, HOUR, DAY, fmtDay, fmtClock, fmtStamp, fmtDuration, fmtTick, fmtCompact, esc, rgba,
} from './format.js';
import { epicKeySet, epicTicket, ownTicket } from './tickets.js';

export const ACTIVITY_ORDER = [
  'pr_opened', 'pr_pushed', 'comment_added', 'pr_approved', 'pr_unapproved',
  'pr_needs_work', 'pr_merged', 'pr_declined', 'reviewer_changed', 'build_status',
];

// Category hues only (prd/10 §3.3.5: teal = waits on and amber = waited on by
// are a reserved pair, never reused for a category or a phase).
export const ACTIVITY_COLOR = {
  pr_opened: '#53C9DF',
  pr_pushed: '#B565E8',
  comment_added: '#8A8D93',
  pr_approved: '#73CF55',
  pr_unapproved: '#F05C70',
  pr_needs_work: '#FBCD58',
  pr_merged: '#238FF9',
  pr_declined: '#C04B56',
  reviewer_changed: '#E25AB5',
  build_status: '#53C9DF',
};

export const PHASE = {
  waiting: { color: '#E8B84A', label: 'waiting on author', hint: 'ball with the author, no commits in the log' },
  active: { color: '#B565E8', label: 'in progress', hint: 'author held the ball and committed' },
  review: { color: '#63B1FF', label: 'in review', hint: 'ball with reviewers' },
  ci: { color: '#53C9DF', label: 'CI', hint: 'ball with CI (needs a build event in the store)' },
  rebasing: { color: '#E25AB5', label: 'rebasing', hint: 'interval ended in a rebase push' },
  closed: { color: '#8A8D93', label: 'closed', hint: 'closed after the last event' },
  unobserved: { color: '#70747B', label: 'no events since', hint: 'not measured — no events in the store' },
};

export const PHASE_ORDER = ['waiting', 'active', 'review', 'ci', 'rebasing', 'closed', 'unobserved'];

const METRICS = {
  S: { lane: 15, bar: 11, label: 10, marker: 2.2 },
  M: { lane: 22, bar: 15, label: 11, marker: 3 },
  L: { lane: 30, bar: 21, label: 12, marker: 3.8 },
};

const GUTTER = 176;
// prd/28: on a 412px phone the 176px gutter would leave ~226px of plot; the
// labels shrink to the ticket key so the time axis keeps most of the width.
const GUTTER_PHONE = 88;
const AXIS_H = 42;
const HIST_H = 36;
const HEADER_H = 22;
const SECTION_GAP = 18;
const BOTTOM = 14;

const STEPS = [
  MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE,
  HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 28 * DAY, 91 * DAY,
];

const ACTIVITY_STATES = ['on', 'faint', 'hide'];
const QUEUE_ORDER = ['review', 'author', 'CI', 'calendar'];

/* prd/25: semantic zoom for the `stories` dimension. A wide window shows the
 * epic → story hierarchy (PRs would be unreadable), a medium window nests each
 * story's PRs beneath it, and the event markers on those PR bars are the third
 * level. The thresholds are in time, not pixels, so the hierarchy is stable
 * while panning. */
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

/** Ball-in-court state machine — mirrors analysis/ballcourt.py on the client. */
function advanceState(pr, state, event) {
  const activity = event.activity;
  const attrs = event.attributes || {};
  if (activity === 'pr_merged' || activity === 'pr_declined') return null;
  if (activity === 'pr_opened' || activity === 'pr_pushed' || activity === 'pr_approved' || activity === 'reviewer_changed') {
    const kind = activity === 'pr_pushed' ? `push:${attrs.push_class || 'other'}` : activity;
    return { ball: 'review', by: event.actor, kind, since: event.t };
  }
  if (activity === 'pr_unapproved' || activity === 'pr_needs_work') {
    return { ball: 'author', by: event.actor, kind: activity, since: event.t };
  }
  if (activity === 'comment_added') {
    if (event.actor === pr.author) return { ball: 'review', by: event.actor, kind: 'author_reply', since: event.t };
    if (attrs.severity === 'mandatory') return { ball: 'author', by: event.actor, kind: 'blocker_comment', since: event.t };
    return { ball: state?.ball || 'review', by: event.actor, kind: 'comment', since: event.t };
  }
  if (activity === 'rebased') return { ball: 'review', by: event.actor, kind: 'rebase', since: event.t };
  if (activity === 'build_status') return { ball: 'CI', by: event.actor, kind: 'ci', since: event.t };
  return state;
}

function phaseFor(state, pr, next, commits) {
  if (state.ball === 'CI') return 'ci';
  const activity = next.activity;
  const attrs = next.attributes || {};
  if (activity === 'pr_pushed') {
    const pushClass = attrs.push_class || 'other';
    return pushClass === 'pure_rebase' || pushClass === 'rebase_conflict' ? 'rebasing' : 'active';
  }
  if (activity === 'rebased') return 'rebasing';
  if (activity === 'pr_merged' || activity === 'pr_declined') return 'review';
  if (state.ball === 'author') {
    // evidence of author work: their own push/comment, or commits in the interval
    if (activity === 'pr_pushed' || next.actor === pr.actor) return 'active';
    return commits > 0 ? 'active' : 'waiting';
  }
  return 'review';
}

export class Timeline {
  constructor({ canvas, tip, onStats, onSelect, onTicket, onSelection, onSprint }) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.tip = tip;
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
    this.events = [];
    this.prs = [];
    this.commitsByPr = new Map();
    // prd/26 P6: the sprint bands and the per-story membership (carry-over).
    this.sprints = [];
    this.sprintStory = new Map();
    this.sprintScope = null;
    this.sprintCarry = null;
    // The epic/tracking keys across the whole store, set by the page (B2). When
    // null the census is taken over the PRs handed to setData().
    this.epicKeys = null;
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
      .map((sprint) => ({
        id: sprint.sprint_id,
        state: sprint.state,
        open: Boolean(sprint.open),
        // an active sprint is one the store says is active (or an old row with
        // no state but no completion); a closed sprint without an end is still
        // closed, only its band has to stop at the snapshot
        active: sprint.state === 'active'
          || (Boolean(sprint.open) && !sprint.state),
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
      .filter((sprint) => Number.isFinite(sprint.start) && Number.isFinite(sprint.end));
    this.sprintStory = new Map();
    for (const row of (data && data.rows) || []) {
      if (row && row.key) this.sprintStory.set(row.key, row);
    }
    this.schedule();
  }

  setData(events, objects, meta = {}) {
    const previous = this._initialised ? { ...this.view } : null;
    this.events = events
      .map((e) => ({ ...e, t: Date.parse(e.ts) }))
      .sort((a, b) => a.t - b.t);
    const prs = new Map();
    this.commitsByPr = new Map();
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
          endedBy: event.activity,
        });
      }
      const next = advanceState(pr, state, event);
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
          ball: state.ball, by: state.by, kind: state.kind, endedBy: null,
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
    return stats;
  }

  // ---------------------------------------------------------------- lanes
  laneRows() {
    if (this.dim === 'story') return this.storyRows();
    if (this.dim === 'object') {
      return this.prs.map((pr) => ({
        key: `${pr.repo}#${pr.number}`,
        pr,
        ticket: pr.ticket,
        count: pr.events.length,
        label: `${pr.ticket ? `${pr.ticket} ` : ''}#${pr.number} ${pr.repo}`,
      }));
    }
    if (this.dim === 'queue') {
      return QUEUE_ORDER.map((key) => {
        const hours = this.queueHours(key);
        return { key, hours, count: Math.round(hours), label: `${key} · ${fmtCompact(hours)} h` };
      }).filter((row) => row.hours > 0);
    }
    // B4: rows come from ALL events, so toggling a legend key never re-sorts
    // the lanes. Counts are totals, not "visible" counts.
    const counts = this.activityCounts();
    const keys = [...new Set(this.events.map((e) => e.actor))];
    keys.sort((a, b) => (counts.get(b) - counts.get(a)) || a.localeCompare(b));
    return keys.map((key) => ({ key, count: counts.get(key) }));
  }

  /** Stories as lanes with their PRs nested beneath (prd/25, epic → story → PR).
   *
   * At a wide zoom the rows are the epic → story hierarchy; below
   * :data:`STORY_NEST_SPAN` each story's PRs become child rows. The event
   * markers on those PR bars are the third semantic level, drawn by the same
   * `paintLifecycle` the object lanes use.
   */
  storyRows() {
    // The grouping is a function of the loaded data, not of the zoom, so it is
    // built once per setData (a pan/zoom only re-reads it).
    let groups;
    if (this._storyCache && this._storyCache.prs === this.prs
        && this._storyCache.events === this.events) {
      groups = this._storyCache.groups;
    } else {
      groups = new Map();
      for (const pr of this.prs) {
        const story = pr.ticket || '(no ticket)';
        if (!groups.has(story)) {
          groups.set(story, { story, epic: '', prs: [], events: [] });
        }
        const group = groups.get(story);
        if (!group.epic && pr.epic) group.epic = pr.epic;
        group.prs.push(pr);
      }
      // A story with no PR is still a lane (prd/25: story → event): its own
      // created/status/sprint/pointed events are in the log and name the STORY.
      for (const event of this.events) {
        for (const objectId of event.objects || []) {
          if (!String(objectId).startsWith('STORY:')) continue;
          const story = String(objectId).slice('STORY:'.length);
          if (!groups.has(story)) {
            groups.set(story, { story, epic: '', prs: [], events: [] });
          }
          groups.get(story).events.push(event);
        }
      }
      this._storyCache = { prs: this.prs, events: this.events, groups };
    }
    const span = this.span();
    const nested = span <= STORY_NEST_SPAN;
    const showEpics = !nested && span > STORY_EPIC_SPAN
      && [...groups.values()].some((group) => group.epic);
    const stories = [...groups.values()].sort((a, b) => {
      const start = (group) => Math.min(
        ...group.prs.map((pr) => pr.created),
        ...group.events.map((event) => event.t),
        Infinity,
      );
      return start(a) - start(b) || a.story.localeCompare(b.story);
    });
    const rows = [];
    let epic = null;
    for (const group of stories) {
      if (showEpics && group.epic && group.epic !== epic) {
        epic = group.epic;
        const members = stories.filter((entry) => entry.epic === epic);
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
            ticket: group.story,
            count: pr.events.length,
            label: `#${pr.number} ${pr.repo}`,
            depth: showEpics && group.epic ? 2 : 1,
          });
        }
      }
    }
    this._storyNested = nested;
    this._storyEpics = showEpics;
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
  layout() {
    const m = METRICS[this.density];
    const lanes = this.laneRows();
    const objectMode = this.dim === 'object' || this.dim === 'story';
    const laneH = objectMode ? Math.max(m.bar + 6, m.lane) : m.lane;
    const barRows = objectMode ? [] : this.prs;
    const height = AXIS_H + HIST_H + HEADER_H + lanes.length * laneH +
      (objectMode ? 0 : SECTION_GAP + HEADER_H + barRows.length * m.bar) + BOTTOM;
    const width = this.canvas.parentElement.clientWidth || 1200;
    const gutter = width <= 640 ? GUTTER_PHONE : GUTTER;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.geom = {
      width, height, gutter, plotW: Math.max(80, width - gutter - 10),
      axisH: AXIS_H, histH: HIST_H, headerH: HEADER_H, laneH, barH: m.bar,
      label: m.label, marker: m.marker, objectMode,
      laneTop: AXIS_H + HIST_H + HEADER_H,
      barTop: AXIS_H + HIST_H + HEADER_H + lanes.length * laneH + SECTION_GAP + HEADER_H,
      lanes, barRows,
    };
    return this.geom;
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

  render() {
    this.dirty = false;
    const ctx = this.ctx;
    const g = this.layout();
    const m = METRICS[this.density];
    this.hits = [];

    ctx.clearRect(0, 0, g.width, g.height);
    ctx.fillStyle = '#151924';
    ctx.fillRect(0, 0, g.width, g.height);

    this.drawTimeBands(ctx, g);
    this.drawSprintBands(ctx, g);
    this.drawAxis(ctx, g);
    this.drawHistogram(ctx, g);
    this.drawLanes(ctx, g, m);
    if (!g.objectMode) this.drawBars(ctx, g, m);

    ctx.strokeStyle = 'rgba(255,255,255,.06)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(g.gutter + 0.5, 0);
    ctx.lineTo(g.gutter + 0.5, g.height);
    ctx.stroke();

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
    this.canvas.setAttribute(
      'aria-label',
      `Event timeline, ${fmtDay(this.view.start)} ${fmtClock(this.view.start)} to ` +
      `${fmtDay(this.view.end)} ${fmtClock(this.view.end)}; ${visibleWord(this)} events, ` +
      `${g.lanes.length} ${this.dim === 'actor' ? 'actor' : this.dim === 'object' ? 'PR' : this.dim === 'story' ? 'story' : 'queue'} lanes. ` +
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
        ctx.fillStyle = 'rgba(255,255,255,.022)';
        ctx.fillRect(x0, AXIS_H, x1 - x0, g.height - AXIS_H);
      }
      if (dayWidth >= 90) {
        const night0 = this.px(t + 18 * HOUR);
        const night1 = this.px(t + DAY);
        if (night1 > night0) {
          ctx.fillStyle = 'rgba(0,0,0,.18)';
          ctx.fillRect(night0, AXIS_H, night1 - night0, g.height - AXIS_H);
        }
        const night2 = this.px(t);
        const night3 = this.px(t + 8 * HOUR);
        if (night3 > night2) {
          ctx.fillStyle = 'rgba(0,0,0,.18)';
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
    this.sprints.forEach((sprint, index) => {
      const left = Math.max(g.gutter, this.px(sprint.start));
      const right = Math.min(g.gutter + g.plotW, this.px(sprint.end));
      if (right <= left) return;
      ctx.fillStyle = index % 2 === 0
        ? 'rgba(140,152,212,.055)' : 'rgba(140,152,212,.022)';
      ctx.fillRect(left, top, right - left, bottom - top);
      ctx.fillStyle = 'rgba(140,152,212,.30)';
      ctx.fillRect(left, top, 1, bottom - top);
      if (right < g.gutter + g.plotW) ctx.fillRect(right - 1, top, 1, bottom - top);
      // the plan of an active sprint: a dashed boundary, not a measured edge
      if (sprint.active && Number.isFinite(sprint.plannedEnd)
          && sprint.plannedEnd > this.view.start && sprint.plannedEnd < this.view.end) {
        const planX = Math.round(this.px(sprint.plannedEnd)) + 0.5;
        ctx.save();
        ctx.setLineDash([3, 3]);
        ctx.strokeStyle = 'rgba(251,205,88,.45)';
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
        ctx.fillStyle = '#8C98D4';
        ctx.fillText(
          fit(ctx, `S${sprint.id}${sprint.active ? ' · active' : ''}`, right - left - 12),
          left + 6,
          labelY,
        );
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

    ctx.fillStyle = '#8A8D93';
    ctx.textAlign = 'right';
    ctx.fillText('UTC', g.gutter - 10, clockRow);

    for (let t = first; t <= this.view.end; t += step) {
      const x = this.px(t);
      if (x < g.gutter || x > right) continue;
      const major = step >= DAY || new Date(t).getUTCHours() === 0;
      ctx.strokeStyle = major ? 'rgba(255,255,255,.10)' : 'rgba(255,255,255,.045)';
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
      ctx.fillStyle = major ? '#B9BBC0' : '#8A8D93';
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
            ctx.fillStyle = '#B9BBC0';
            ctx.font = `600 ${Math.max(10, g.label - 1)}px "JetBrains Mono", ui-monospace, monospace`;
            ctx.fillText(day, dayLeft, dayRow);
            ctx.font = `500 ${Math.max(10, g.label - 1)}px "JetBrains Mono", ui-monospace, monospace`;
          }
        }
      }
    }
    ctx.strokeStyle = 'rgba(255,255,255,.08)';
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
    ctx.fillStyle = '#8A8D93';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillText(`${total} event${total === 1 ? '' : 's'}`, g.gutter - 10, top + height / 2);

    for (const [x, n] of buckets) {
      const h = Math.max(2, Math.round((n / Math.max(1, max)) * height));
      ctx.fillStyle = n > 1 ? 'rgba(99,177,255,.55)' : 'rgba(99,177,255,.30)';
      ctx.fillRect(x, top + height - h, 2, h);
    }
    // brushed range: visible in the strip and faintly across the lanes
    const range = this.brushing || this.selection;
    if (range && range.end > range.start) {
      const x0 = Math.max(g.gutter, this.px(range.start));
      const x1 = Math.min(g.gutter + g.plotW, this.px(range.end));
      if (x1 > x0) {
        ctx.fillStyle = 'rgba(104,192,252,.14)';
        ctx.fillRect(x0, AXIS_H, x1 - x0, g.height - AXIS_H - BOTTOM);
        ctx.fillStyle = this.brushing ? 'rgba(104,192,252,.35)' : 'rgba(104,192,252,.22)';
        ctx.fillRect(x0, top, x1 - x0, height);
        ctx.strokeStyle = 'rgba(104,192,252,.7)';
        ctx.lineWidth = 1;
        for (const x of [x0, x1]) {
          ctx.beginPath();
          ctx.moveTo(Math.round(x) + 0.5, AXIS_H);
          ctx.lineTo(Math.round(x) + 0.5, g.height - BOTTOM);
          ctx.stroke();
        }
      }
    }
    ctx.strokeStyle = 'rgba(255,255,255,.06)';
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
    ctx.fillStyle = '#8A8D93';
    const section = this.dim === 'actor' ? 'ACTORS'
      : this.dim === 'object' ? 'PULL REQUESTS'
        : this.dim === 'story' ? 'STORIES' : 'QUEUES';
    const sectionCount = this.dim === 'story'
      ? `${rows.filter((row) => row.kind === 'story').length} stories · ` +
        `${rows.filter((row) => row.kind === 'pr').length} PRs`
      : String(rows.length);
    const narrow = g.gutter < 120;
    ctx.fillText(
      narrow ? `${section} ${sectionCount}` : `${section} · ${sectionCount}`,
      g.gutter - 10,
      AXIS_H + HIST_H + 11,
    );

    rows.forEach((row, i) => {
      const y = g.laneTop + i * g.laneH;
      if (i % 2 === 0) {
        ctx.fillStyle = 'rgba(255,255,255,.012)';
        ctx.fillRect(g.gutter, y, g.plotW, g.laneH);
      }
      ctx.strokeStyle = 'rgba(255,255,255,.035)';
      ctx.beginPath();
      ctx.moveTo(g.gutter, Math.round(y + g.laneH) - 0.5);
      ctx.lineTo(g.width, Math.round(y + g.laneH) - 0.5);
      ctx.stroke();

      const hovered = this.hover && this.hover.laneKey === row.key;
      ctx.font = `500 ${g.label}px "JetBrains Mono", ui-monospace, monospace`;
      // A ticket key is a doorway (prd/25): the label is drawn in the link hue
      // and the gutter is a hit target that opens the ticket detail page.
      const linked = Boolean(row.ticket);
      ctx.fillStyle = hovered ? '#E8E9EB' : linked ? '#94B4D4' : '#B9BBC0';
      ctx.textAlign = 'right';
      const indent = row.depth ? `${'  '.repeat(row.depth)}↳ ` : '';
      const text = narrow
        ? (row.ticket || (row.pr ? `#${row.pr.number}` : (row.label || row.key)))
        : `${indent}${row.label || row.key}`;
      const label = fit(ctx, text, narrow ? g.gutter - 12 : g.gutter - 22);
      ctx.fillText(label, g.gutter - 10, y + g.laneH / 2);
      if (linked) {
        this.hits.push({
          kind: 'label', x0: 0, x1: g.gutter, y0: y, y1: y + g.laneH,
          ticket: row.ticket, laneKey: row.key,
        });
      }
    });

    if (this.dim === 'queue') {
      this.drawQueueLanes(ctx, g, rows);
      return;
    }
    if (this.dim === 'story') {
      this.drawStoryLanes(ctx, g, rows);
      return;
    }
    if (this.dim === 'object') {
      this.drawObjectLanes(ctx, g, rows);
      return;
    }
    this.drawActorLanes(ctx, g, rows);
  }

  drawActorLanes(ctx, g, rows) {
    const index = new Map(rows.map((r, i) => [r.key, i]));
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
      const y = g.laneTop + bucket.lane * g.laneH + g.laneH / 2;
      if (bucket.items.length > 1) {
        aggregated += bucket.items.length;
        const fromX = this.px(Math.min(...bucket.items.map((e) => e.t)));
        const toX = Math.max(this.px(Math.max(...bucket.items.map((e) => e.t))), fromX + 2);
        ctx.fillStyle = bucket.faint ? 'rgba(232,233,235,.16)' : 'rgba(232,233,235,.5)';
        ctx.fillRect(fromX, y - g.laneH / 2 + 3, Math.max(2, toX - fromX), g.laneH - 6);
        this.hits.push({
          kind: 'bucket', x0: fromX, x1: toX, y0: y - g.laneH / 2, y1: y + g.laneH / 2,
          items: bucket.items, step: this.span() / g.plotW,
          laneKey: rows[bucket.lane].key,
        });
        continue;
      }
      const event = bucket.items[0];
      const color = ACTIVITY_COLOR[event.activity] || '#8A8D93';
      const r = g.marker;
      ctx.beginPath();
      ctx.arc(bucket.x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = bucket.faint ? rgba(color, 0.2) : color;
      ctx.fill();
      if (this.hover && this.hover.event && this.hover.event.key === event.key) {
        ctx.strokeStyle = '#E8E9EB';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      this.hits.push({ kind: 'event', x: bucket.x, y, r: r + 4, event, laneKey: rows[bucket.lane].key });
    }
    this.aggregated = aggregated;
  }

  drawObjectLanes(ctx, g, rows) {
    rows.forEach((row, i) => {
      const pr = row.pr;
      if (!pr) return;
      const y = g.laneTop + i * g.laneH;
      const barY = y + 3;
      const barH = g.laneH - 6;
      this.paintLifecycle(ctx, g, pr, barY, barH);
      if (barH >= 9) {
        for (const event of pr.events) {
          if (event.t < this.view.start || event.t > this.view.end) continue;
          const state = this.stateOf(event.activity);
          if (state === 'hide') continue;
          const x = this.px(event.t);
          ctx.fillStyle = rgba(ACTIVITY_COLOR[event.activity] || '#8A8D93', state === 'faint' ? 0.2 : 0.85);
          ctx.fillRect(x, barY, 1.5, barH);
        }
      }
      const end = pr.closed || this.now;
      this.hits.push({
        kind: 'bar', x0: this.px(pr.created), x1: Math.max(this.px(end), this.px(pr.created) + 2),
        y0: y, y1: y + g.laneH, pr, laneKey: row.key,
      });
    });
  }

  drawStoryLanes(ctx, g, rows) {
    this.aggregated = 0;
    rows.forEach((row, i) => {
      const y = g.laneTop + i * g.laneH;
      const barY = y + 3;
      const barH = g.laneH - 6;
      if (row.kind === 'epic') {
        const prs = rows
          .filter((entry) => entry.kind === 'story' && entry.epic === row.epic)
          .flatMap((entry) => entry.prs || []);
        if (!prs.length) return;
        const from = Math.min(...prs.map((pr) => pr.created));
        const to = Math.max(...prs.map((pr) => pr.closed || this.now));
        const x0 = Math.max(g.gutter, this.px(from));
        const x1 = Math.min(g.gutter + g.plotW, this.px(to));
        if (x1 <= x0) return;
        // A bracket, not a lifecycle: the epic has no phases of its own.
        ctx.fillStyle = 'rgba(140,152,212,.16)';
        ctx.fillRect(x0, barY, x1 - x0, barH);
        ctx.fillStyle = 'rgba(140,152,212,.65)';
        ctx.fillRect(x0, barY, 2, barH);
        ctx.fillRect(x1 - 2, barY, 2, barH);
        this.hits.push({
          kind: 'epic', x0, x1, y0: y, y1: y + g.laneH,
          epic: row.epic, ticket: row.ticket, laneKey: row.key,
        });
        return;
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
            ctx.fillStyle = 'rgba(138,141,147,.28)';
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
            ACTIVITY_COLOR[event.activity] || '#8A8D93',
            state === 'faint' ? 0.25 : 0.9,
          );
          ctx.fillRect(x, barY, 1.5, barH);
        }
        const times = prs.length
          ? prs.flatMap((pr) => [pr.created, pr.closed || this.now])
          : storyEvents.map((event) => event.t);
        if (!times.length) return;
        const membership = this.sprintStory.get(row.story);
        if (membership && membership.carried_over) {
          // prd/26 P6: a story re-added across sprints is marked at its end —
          // the sprint plan's cost, on the lane the investigator is reading.
          const endX = Math.min(g.gutter + g.plotW - 2, this.px(Math.max(...times)));
          ctx.fillStyle = 'rgba(251,205,88,.9)';
          ctx.fillRect(endX - 1, barY, 2, barH);
          ctx.font = `700 ${Math.max(9, g.label)}px "JetBrains Mono", ui-monospace, monospace`;
          ctx.textAlign = 'right';
          ctx.textBaseline = 'middle';
          ctx.fillStyle = '#FBCD58';
          ctx.fillText('↻', endX - 4, y + g.laneH / 2);
        }
        this.hits.push({
          kind: 'story', x0: this.px(Math.min(...times)),
          x1: Math.max(this.px(Math.max(...times)), this.px(Math.min(...times)) + 2),
          y0: y, y1: y + g.laneH, story: row.story, ticket: row.ticket,
          prs, events: storyEvents, membership, laneKey: row.key,
        });
        return;
      }
      const pr = row.pr;
      if (!pr) return;
      this.paintLifecycle(ctx, g, pr, barY, barH);
      if (barH >= 9) {
        for (const event of pr.events) {
          if (event.t < this.view.start || event.t > this.view.end) continue;
          const state = this.stateOf(event.activity);
          if (state === 'hide') continue;
          const x = this.px(event.t);
          ctx.fillStyle = rgba(ACTIVITY_COLOR[event.activity] || '#8A8D93', state === 'faint' ? 0.2 : 0.85);
          ctx.fillRect(x, barY, 1.5, barH);
        }
      }
      const end = pr.closed || this.now;
      this.hits.push({
        kind: 'bar', x0: this.px(pr.created),
        x1: Math.max(this.px(end), this.px(pr.created) + 2),
        y0: y, y1: y + g.laneH, pr, laneKey: row.key,
      });
    });
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
        const y = g.laneTop + lane * g.laneH;
        const x0 = Math.max(g.gutter, this.px(seg.from));
        const x1 = Math.min(g.gutter + g.plotW, this.px(seg.to));
        if (x1 <= x0) continue;
        const color = PHASE[seg.phase].color;
        ctx.fillStyle = rgba(color, seg.phase === 'unobserved' ? 0.14 : 0.3);
        ctx.fillRect(x0, y + 4, Math.max(1, x1 - x0), g.laneH - 8);
        ctx.fillStyle = rgba(color, seg.phase === 'unobserved' ? 0.25 : 0.75);
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
      const color = PHASE[seg.phase].color;
      if (seg.phase === 'unobserved') {
        // hatched, because nothing was measured here
        ctx.fillStyle = rgba(color, 0.10);
        ctx.fillRect(x0, barY, Math.max(1, x1 - x0), barH);
        ctx.fillStyle = rgba(color, 0.28);
        for (let x = x0 + 2; x < x1; x += 6) ctx.fillRect(x, barY, 1, barH);
        continue;
      }
      ctx.fillStyle = rgba(color, seg.phase === 'closed' ? 0.22 : 0.34);
      ctx.fillRect(x0, barY, Math.max(1, x1 - x0), barH);
      // saturated cap keeps phase boundaries legible without a solid block
      ctx.fillStyle = rgba(color, 0.9);
      ctx.fillRect(x0, barY, Math.min(2, x1 - x0), barH);
    }
    const end = pr.closed || this.now;
    if (pr.closed) {
      const x = Math.min(g.gutter + g.plotW, this.px(end));
      ctx.fillStyle = rgba(PHASE.closed.color, 0.9);
      ctx.fillRect(x - 2, barY, 2.5, barH);
    } else {
      const x = Math.min(g.gutter + g.plotW, this.px(end));
      ctx.fillStyle = rgba('#FBCD58', 0.75);
      ctx.beginPath();
      ctx.moveTo(x, barY);
      ctx.lineTo(x + 4, barY + barH / 2);
      ctx.lineTo(x, barY + barH);
      ctx.closePath();
      ctx.fill();
    }
  }

  drawBars(ctx, g, m) {
    ctx.font = `600 ${Math.max(10, g.label - 1)}px Inter, system-ui, sans-serif`;
    ctx.textAlign = 'right';
    ctx.fillStyle = '#8A8D93';
    ctx.fillText(
      g.gutter < 120 ? `PRs ${g.barRows.length}` : `PR LIFECYCLE · ${g.barRows.length}`,
      g.gutter - 10,
      g.barTop - 11,
    );

    g.barRows.forEach((pr, i) => {
      const y = g.barTop + i * g.barH;
      const isHover = this.hover && this.hover.pr === pr;
      if (i % 2 === 0 || isHover) {
        ctx.fillStyle = isHover ? 'rgba(99,177,255,.07)' : 'rgba(255,255,255,.012)';
        ctx.fillRect(g.gutter, y, g.plotW, g.barH);
      }

      ctx.font = `500 ${g.label}px "JetBrains Mono", ui-monospace, monospace`;
      ctx.textAlign = 'right';
      ctx.fillStyle = isHover ? '#E8E9EB' : '#B9BBC0';
      const key = pr.ticket ? `${pr.ticket} ` : '';
      const label = g.gutter < 120
        ? (pr.ticket || `#${pr.number}`)
        : `${key}#${pr.number} ${pr.repo}`;
      ctx.fillText(fit(ctx, label, g.gutter < 120 ? g.gutter - 12 : g.gutter - 22), g.gutter - 10, y + g.barH / 2);

      const barY = y + 2;
      const barH = g.barH - 4;
      this.paintLifecycle(ctx, g, pr, barY, barH);

      if (barH >= 9) {
        for (const event of pr.events) {
          if (event.t < this.view.start || event.t > this.view.end) continue;
          const state = this.stateOf(event.activity);
          if (state === 'hide') continue;
          const x = this.px(event.t);
          ctx.fillStyle = rgba(ACTIVITY_COLOR[event.activity] || '#8A8D93', state === 'faint' ? 0.2 : 0.85);
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
    });
  }

  // ---------------------------------------------------------------- hover
  hitTest(cx, cy) {
    for (let i = this.hits.length - 1; i >= 0; i -= 1) {
      const hit = this.hits[i];
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
        <div class="row"><span class="act" style="color:${ACTIVITY_COLOR[event.activity] || '#8A8D93'}">${esc(event.activity)}</span>
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
          <span class="muted">≤ ${esc(fmtDuration(hit.step))} apart</span></div>
        <div class="row"><span class="k">lane</span><span class="v">${esc(hit.laneKey || '')}</span></div>
        <div class="row"><span class="k">window</span><span class="v">${esc(fmtStamp(first.t))}<br>${esc(fmtStamp(last.t))}</span></div>
        <div class="row"><span class="k">zoom</span><span class="v">zoom in for individual events</span></div>`;
    } else if (hit.kind === 'interval') {
      const seg = hit.interval;
      const pr = hit.pr;
      this.tip.innerHTML = `
        <div class="row"><span class="act" style="color:${PHASE[seg.phase].color}">${esc(PHASE[seg.phase].label)}</span>
          <span class="muted">${esc(fmtDuration(seg.to - seg.from))}</span></div>
        <div class="row"><span class="k">queue</span><span class="v">${esc(hit.queue)}</span></div>
        <div class="row"><span class="k">pr</span><span class="v">${pr.ticket ? `${esc(pr.ticket)} ` : ''}#${esc(pr.number)} ${esc(pr.repo)}</span></div>
        <div class="row"><span class="k">window</span><span class="v">${esc(fmtStamp(seg.from))}<br>${esc(fmtStamp(seg.to))}</span></div>
        <div class="row"><span class="k">basis</span><span class="v">${esc(PHASE[seg.phase].hint)}</span></div>`;
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
        <div class="row"><span class="k">scope</span><span class="v">${sprint.stories} stor${sprint.stories === 1 ? 'y' : 'ies'} · ${sprint.points} points · ${sprint.done} done</span></div>
        ${sprint.carriedIn.length
          ? `<div class="row"><span class="k">carried in</span><span class="v">${esc(sprint.carriedIn.join(', '))}</span></div>` : ''}
        ${sprint.carriedOut.length
          ? `<div class="row"><span class="k">carried out</span><span class="v">${esc(sprint.carriedOut.join(', '))}</span></div>` : ''}
        <div class="row"><span class="k">open</span><span class="v">click to set the timeframe to this sprint</span></div>`;
    } else if (hit.kind === 'story' || hit.kind === 'epic') {
      const prs = hit.kind === 'story'
        ? (hit.prs || [])
        : this.laneRows().filter((row) => row.kind === 'story' && row.epic === hit.epic)
          .flatMap((row) => row.prs || []);
      const totals = new Map();
      for (const pr of prs) {
        for (const [phase, ms] of this.phases(pr)) totals.set(phase, (totals.get(phase) || 0) + ms);
      }
      const from = prs.length ? Math.min(...prs.map((pr) => pr.created)) : null;
      const to = prs.length ? Math.max(...prs.map((pr) => pr.closed || this.now)) : null;
      this.tip.innerHTML = `
        <div class="row"><span class="act mono">${esc(hit.ticket)}</span>
          <span class="muted">${hit.kind === 'epic' ? 'epic' : 'story'} · ${prs.length} PR${prs.length === 1 ? '' : 's'}</span></div>
        ${from != null ? `<div class="row"><span class="k">span</span><span class="v">${esc(fmtDuration(to - from))}</span></div>` : ''}
        ${hit.kind === 'story' ? sprintRow(hit.membership) : ''}
        ${[...totals.entries()].filter(([, ms]) => ms > 0).map(([phase, ms]) => `
          <div class="row"><span class="k">${esc(PHASE[phase].label)}</span>
            <span class="v" style="color:${PHASE[phase].color}">${esc(fmtDuration(ms))}</span></div>`).join('')}
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
        <div class="row"><span class="k">cycle</span><span class="v">${esc(fmtDuration(total))}</span></div>
        ${measured.map(([phase, ms]) => `<div class="row"><span class="k">${esc(PHASE[phase].label)}</span>
          <span class="v" style="color:${PHASE[phase].color}">${esc(fmtDuration(ms))}</span></div>`).join('')}
        ${unobserved ? `<div class="row"><span class="k">no events</span><span class="v" style="color:${PHASE.unobserved.color}">${esc(fmtDuration(unobserved))} · not measured</span></div>` : ''}`;
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
      ev.preventDefault();
      const rect = canvas.getBoundingClientRect();
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
      else if (key === '+' || key === '=') { this.zoom(0.75); ev.preventDefault(); }
      else if (key === '-' || key === '_') { this.zoom(1.32); ev.preventDefault(); }
      else if (key === 'Home' || key === '0' || key === 'f' || key === 'F') { this.fit(); ev.preventDefault(); }
      else if (key === 'Escape') { this.clearHover(); this.clearSelection(); ev.preventDefault(); }
    });

    // prd/28 §2: touch. One finger pans, two fingers pinch around their
    // midpoint, a short tap selects/drills, and a 450 ms hold shows the
    // tooltip the desktop gets from hover (never a hover-only affordance).
    const local = (ev) => {
      const rect = canvas.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    };
    const clearHold = () => {
      clearTimeout(this._holdTimer);
      this._holdTimer = 0;
    };

    canvas.addEventListener('pointerdown', (ev) => {
      if (ev.pointerType === 'mouse') return;
      this._touchAt = Date.now();
      ev.preventDefault();
      try { canvas.setPointerCapture(ev.pointerId); } catch { /* not fatal */ }
      const point = local(ev);
      this._pointers.set(ev.pointerId, {
        ...point,
        startX: point.x, startY: point.y,
        lastX: point.x, lastY: point.y,
        moved: false, held: false, t: Date.now(),
      });
      if (this._pointers.size > 1) {
        clearHold();
        for (const pointer of this._pointers.values()) pointer.moved = true;
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
      ev.preventDefault();
      const point = local(ev);
      pointer.x = point.x;
      pointer.y = point.y;
      if (this._pointers.size > 1) {
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
      if (!pointer.moved && Math.hypot(point.x - pointer.startX, point.y - pointer.startY) > 8) {
        pointer.moved = true;
        clearHold();
      }
      if (pointer.moved) {
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
      if (this._pointers.size < 2) this._pinch = null;
      if (this._pointers.size === 1) {
        // the remaining finger carries on as a pan from where it is now
        const rest = [...this._pointers.values()][0];
        rest.startX = rest.x;
        rest.startY = rest.y;
        rest.lastX = rest.x;
        rest.lastY = rest.y;
        rest.moved = true;
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


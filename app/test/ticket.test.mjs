/* Ticket detail page + ticket-key links + the `stories` lane dimension
 * (prd/25). The page is pure markup, so the answer sentence, the segment bar,
 * the PR rows and the Markdown export are asserted without a browser; the lane
 * grouping is a pure method on the timeline class.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  SEGMENT_PHASE, boundaryChips, prTable, segmentBar, segmentHours,
  sprintStrip, ticketMarkdown, ticketMarkup, ticketStats, worklogBlock,
} from '../ticket.js';
import {
  isTicketKey, linkifyTickets, markdownLinkTickets, ticketHref, ticketKey, ticketLink,
} from '../tickets.js';
import { rulesMarkdown, rulesMarkup } from '../rules.js';
import { Timeline } from '../timeline.js';

const DAY = 86_400_000;
const T0 = Date.parse('2026-10-02T09:00:00Z');

function segment(key, label, bounds, hours, extra = {}) {
  return {
    key, label, bounds, hours, share: hours == null ? null : 0.2,
    active_hours: hours == null ? null : 1, efficiency: hours == null ? null : 0.2,
    available: hours != null, inverted: false, clamped: false,
    missing: hours == null ? [bounds[1]] : [], reason: hours == null ? 'no pointed event' : null,
    ...extra,
  };
}

function payload(overrides = {}) {
  const rows = [
    segment('ready_refined', 'ready → refined', ['created', 'refined'], 45),
    segment('refined_sprint', 'refined → sprint', ['refined', 'sprint'], null),
    segment('sprint_in_progress', 'sprint → in progress', ['sprint', 'started'], 8),
    segment('in_progress_pr_open', 'in progress → PR open', ['started', 'pr_open'], 2),
    segment('pr_open_first_review', 'PR open → first review', ['pr_open', 'first_review'], 4),
    segment('review_rounds', 'review rounds', ['first_review', 'approval'], 10),
    segment('final_approval_merge', 'final approval → merge', ['approval', 'merged'], 2),
    segment('merge_done', 'merge → done', ['merged', 'done'], 1),
  ];
  const base = {
    ok: true,
    key: 'DEL-167',
    available: true,
    snapshot: '2026-11-17T00:00:00Z',
    hours_mode: 'business',
    assumption: 'business hours',
    story: {
      key: 'DEL-167', object_id: 'STORY:DEL-167', points: 8, state: 'done',
      epic_key: 'DEL-1', summary_class: 'feature', assignee: 'BoFe',
      created: '2026-10-02T09:00:00Z', closed: '2026-10-16T14:38:42Z',
      cycle_hours: 85, censored: false,
    },
    segments: {
      order: rows.map((row) => row.key),
      rows,
      boundaries: Object.fromEntries([
        'created', 'refined', 'sprint', 'started', 'pr_open', 'first_review',
        'approval', 'merged', 'done',
      ].map((name, index) => [name, {
        ts: name === 'refined' ? null : new Date(T0 + index * 3_600_000).toISOString(),
        available: name !== 'refined',
        reason: name === 'refined' ? "no 'pointed' event" : null,
      }])),
      measured: 7,
      missing_segments: ['refined_sprint'],
      missing: ['refined'],
      total_hours: 72,
      active_hours: 9,
      efficiency: 0.125,
      cycle_hours: 85,
      explained_share: 0.85,
      largest: 'ready_refined',
      largest_label: 'ready → refined',
      largest_hours: 45,
      largest_share: 0.625,
      clamped: 0,
      hours_mode: 'business',
      heuristic: 'refined = the first pointed event',
    },
    prs: [{
      object_id: 'PR:DEL/delivery-api#53', number: 53, repo: 'DEL/delivery-api',
      state: 'open', author: 'AlNg', ticket_keys: ['DEL-1', 'DEL-167'],
      opened: '2026-10-14T16:10:36Z', closed: null, cycle_hours: 184,
      censored: true, censored_at: '2026-11-17T00:00:00Z', review_rounds: 2,
      first_review: '2026-10-19T09:48:20Z', merged: null, active_hours: 8,
      pushes: [{ ts: '2026-10-19T13:13:38Z', push_class: 'content' }],
      push_classes: { content: 2 },
      intervals: [
        { phase: 'review', from: '2026-10-14T16:10:36Z', to: '2026-10-19T09:48:20Z', hours: 10, ball: 'review', waited_on: 'CaOs', measured: true, kind: 'opened', evidence: null, commits: 0, open: true },
        { phase: 'active', from: '2026-10-19T09:48:20Z', to: '2026-10-19T13:13:38Z', hours: 3, ball: 'author', waited_on: 'AlNg', measured: true, kind: 'needs_work', evidence: 'commits', commits: 1, open: true },
      ],
    }],
    pr_count: 1,
    sprints: {
      memberships: [{
        sprint_id: 3, object_id: 'SPRINT:DEL#3', state: 'active',
        start: '2026-11-02T09:00:00Z', end: null, planned_end: '2026-11-27',
        added: '2026-10-13T12:22:00Z', removed: null, at_start: false,
      }],
      carried_over: false, sprints_involved: 1,
    },
    worklogs: [
      { ts: '2026-10-16T10:10:00Z', hours: 1, log_delay_hours: 303, bucket: 'late', actor: 'BoFe' },
    ],
    worklog: {
      worklogs: 1, logged_hours: 1, active_hours: 8, logged_to_active_ratio: 0.13,
      buckets: { same_day: 0, next_day: 0, within_week: 0, late: 1 },
      late_share: 1, note: null,
    },
    events: [
      { key: 'DEL-167:created', ts: '2026-10-02T09:00:00Z', activity: 'created', actor: 'LaAd', attributes: {} },
      { key: 'DEL-167:pointed', ts: '2026-10-09T15:00:00Z', activity: 'pointed', actor: 'LaAd', attributes: { points: 8 } },
    ],
    event_count: 2,
    answer: "the store cannot say how long the refined → sprint segment took because the refined boundary is missing (no 'pointed' event); the 7 of 8 segments it can measure cover 72 h, most of it in ready → refined (45 h, 62%).",
    answer_kind: 'partial',
    note: null,
  };
  return { ...base, ...overrides };
}

test('the segment bar draws all eight segments and names the absent one', () => {
  const html = segmentBar(payload());
  assert.equal((html.match(/data-segment=/g) || []).length, 8);
  assert.match(html, /data-segment="ready_refined" data-available="true"/);
  assert.match(html, /data-segment="refined_sprint" data-available="false"/);
  // an absent segment says why, and never paints hours it does not have
  assert.match(html, /not measurable — the sprint boundary is missing/);
  assert.match(html, /not measured/);
  assert.doesNotMatch(html, /data-segment="refined_sprint"[^>]*background:/);
});

test('the segment colours extend the PR gantt vocabulary in lifecycle order', () => {
  for (const key of Object.keys(SEGMENT_PHASE)) {
    assert.match(SEGMENT_PHASE[key].color, /^#[0-9A-F]{6}$/);
  }
  // the story-only phases are distinct from each other, and the PR phases are
  // reused by name (waiting / active / review / closed)
  const colors = Object.values(SEGMENT_PHASE).map((phase) => phase.color);
  assert.equal(new Set(colors).size, 8);
  assert.equal(SEGMENT_PHASE.in_progress_pr_open.phase, 'active');
  assert.equal(SEGMENT_PHASE.review_rounds.phase, 'review');
});

test('the prose answer is rendered first, and names the missing boundary', () => {
  const html = ticketMarkup(payload());
  assert.match(html, /id="ticket-answer" data-kind="partial"/);
  assert.match(html, /the store cannot say how long the refined → sprint segment took/);
  assert.match(html, /because the refined boundary is missing/);
  // the nine boundary chips name the absent one rather than printing a dash
  const chips = boundaryChips(payload());
  assert.equal((chips.match(/class="ticket-boundary/g) || []).length, 9);
  assert.match(chips, /ticket-boundary missing[^>]*title="no 'pointed' event"/);
  assert.match(chips, />refined<\/span>missing/);
});

test('PR rows carry censoring, review rounds and push classes', () => {
  const html = prTable(payload());
  assert.match(html, /#53/);
  assert.match(html, /open · censored/);
  assert.match(html, /data-pr="PR:DEL\/delivery-api#53"/);
  assert.match(html, />2<\/td>/); // review rounds
  assert.match(html, /content ×2/);
  assert.match(html, /ticket-mini-bar/);
});

test('the sprint strip and the worklog block state their own facts', () => {
  const sprints = sprintStrip(payload());
  assert.match(sprints, /#3/);
  assert.match(sprints, /active/);
  assert.match(sprints, /added 13 Oct 12:22/);
  const worklog = worklogBlock(payload());
  assert.match(worklog, /logged/);
  assert.match(worklog, /active \(PR\)/);
  assert.match(worklog, /0\.13/);
  assert.match(worklog, /late 1/);
  const stats = ticketStats(payload());
  assert.match(stats, /85 h/);
  assert.match(stats, /7<\/b>\/8 segments/);
});

test('the ticket Markdown export keeps the answer and links the key', () => {
  const markdown = ticketMarkdown(payload(), 'http://127.0.0.1:8766/');
  assert.match(markdown, /# DEL-167 — why did it take this long\?/);
  assert.match(markdown, /> the store cannot say how long the refined → sprint segment took/);
  assert.match(markdown, /\[open in the app\]\(http:\/\/127\.0\.0\.1:8766\/#ticket\/DEL-167\)/);
  assert.match(markdown, /\| refined → sprint \| not measured \|/);
  assert.match(markdown, /#53 DEL\/delivery-api · open \(right-censored\)/);
});

test('a missing story is a stated result, not an empty page', () => {
  const html = ticketMarkup(payload({
    available: false,
    story: null,
    segments: { ...payload().segments, measured: 0, rows: [], boundaries: {}, missing: [] },
    answer: 'the store has no STORY object for DEL-167: its 1 PR can be shown, but the ticket\'s own waiting cannot be decomposed.',
    answer_kind: 'story_missing',
    note: 'no STORY object carries this key',
  }));
  assert.match(html, /no STORY object/);
  assert.match(html, /has no STORY object for DEL-167/);
  assert.match(html, /data-kind="story_missing"/);
});

test('ticket keys are links: href, label, and whole-token linkify', () => {
  assert.equal(ticketKey('STORY:DEL-167'), 'DEL-167');
  assert.equal(ticketKey('TICKET:DEL-167'), 'DEL-167');
  assert.equal(ticketKey('DEL-167'), 'DEL-167');
  assert.equal(ticketHref('DEL-167'), '#ticket/DEL-167');
  assert.equal(ticketHref('DEL-167', 'http://x/'), 'http://x/#ticket/DEL-167');
  assert.equal(isTicketKey('DEL-167'), true);
  assert.equal(isTicketKey('no ticket here'), false);
  const link = ticketLink('DEL-167');
  assert.match(link, /href="#ticket\/DEL-167"/);
  assert.match(link, /data-ticket="DEL-167"/);
  // a sentence keeps its prose; only whole key tokens become anchors
  const sentence = linkifyTickets('DEL-167 and ENG-9 waited', 'http://x/');
  assert.match(sentence, /<a class="ticket-link" href="http:\/\/x\/#ticket\/DEL-167"/);
  assert.match(sentence, /and <a class="ticket-link" href="http:\/\/x\/#ticket\/ENG-9"/);
  assert.match(sentence, / waited$/);
  // the key is escaped, never interpolated raw
  assert.match(linkifyTickets('<b>DEL-167</b>'), /&lt;b&gt;<a/);
});

/* ---------------------------------------------------------------- stories */

function fakeTimeline(prs, span, events = []) {
  const timeline = Object.create(Timeline.prototype);
  timeline.prs = prs;
  timeline.events = events;
  timeline.now = T0 + 40 * DAY;
  timeline.view = { start: T0, end: T0 + span };
  timeline._life = new Map();
  return timeline;
}

function fakePr(number, ticket, epic, created, closed = null) {
  return {
    id: `PR:DEL/web#${number}`, number, repo: 'web', repoFull: 'DEL/web',
    ticket, epic, created, closed, state: closed ? 'merged' : 'open',
    actor: 'AlNg', events: [], commits: [],
  };
}

test('the stories lane groups PRs under their story and nests them when zoomed in', () => {
  const prs = [
    fakePr(52, 'DEL-167', 'DEL-1', T0),
    fakePr(53, 'DEL-167', 'DEL-1', T0 + 2 * DAY),
    fakePr(60, 'DEL-176', 'DEL-1', T0 + 5 * DAY),
  ];
  const wide = fakeTimeline(prs, 60 * DAY).storyRows();
  const stories = wide.filter((row) => row.kind === 'story');
  assert.deepEqual(stories.map((row) => row.story), ['DEL-167', 'DEL-176']);
  assert.equal(stories[0].prs.length, 2);
  // 60 d: PRs collapsed, the story is the lane
  assert.equal(wide.filter((row) => row.kind === 'pr').length, 0);

  const close = fakeTimeline(prs, 20 * DAY).storyRows();
  const nested = close.filter((row) => row.kind === 'pr');
  assert.equal(nested.length, 3);
  assert.deepEqual(nested.map((row) => row.pr.number), [52, 53, 60]);
  // PRs are children of their story, in order
  assert.deepEqual(nested.map((row) => row.story), ['DEL-167', 'DEL-167', 'DEL-176']);
  assert.equal(nested[0].depth, 1);
});

test('a very wide window shows the epic → story hierarchy, not PRs', () => {
  const prs = [
    fakePr(52, 'DEL-167', 'DEL-1', T0),
    fakePr(60, 'DEL-176', 'DEL-1', T0 + 5 * DAY),
  ];
  const rows = fakeTimeline(prs, 200 * DAY).storyRows();
  assert.equal(rows[0].kind, 'epic');
  assert.equal(rows[0].epic, 'DEL-1');
  assert.deepEqual(rows.filter((row) => row.kind === 'story').map((row) => row.story),
    ['DEL-167', 'DEL-176']);
  assert.equal(rows.filter((row) => row.kind === 'pr').length, 0);
  assert.equal(rows[1].depth, 1); // story indented under its epic
});

test('a story with no PR is still a lane, because its own events name it', () => {
  const events = [
    { key: 'e1', t: T0, activity: 'created', objects: ['STORY:DEL-176'] },
    { key: 'e2', t: T0 + DAY, activity: 'status_changed', objects: ['STORY:DEL-176'] },
    { key: 'e3', t: T0 + DAY, activity: 'pr_opened', objects: ['PR:DEL/web#1'] },
  ];
  const rows = fakeTimeline([], 20 * DAY, events).storyRows();
  const stories = rows.filter((row) => row.kind === 'story');
  assert.deepEqual(stories.map((row) => row.story), ['DEL-176']);
  assert.equal(stories[0].events.length, 2);
  assert.equal(stories[0].prs.length, 0);
  assert.equal(rows.filter((row) => row.kind === 'pr').length, 0);
});

test('rule evidence ticket keys are links, not plain text', () => {
  const data = {
    level: 'team',
    sample: { prs: 10, days: 30 },
    hours_mode: 'business',
    as_of: '2026-11-17T00:00:00Z',
    note: '',
    suggestions: [{
      id: 'jira-waiting-time', rank: 1, severity: 'warning',
      metric: 'waiting_time_decomposition', title: 'The team spends 59% in review',
      suggestion: 'Shrink review.', impact_hours: 10, confidence: 0.8,
      evidence: { segment: 'review rounds', examples: 'DEL-167, DEL-176' },
    }],
  };
  const html = rulesMarkup(data, { base: 'http://x/' });
  assert.match(html, /data-ticket="DEL-167"/);
  assert.match(html, /href="http:\/\/x\/#ticket\/DEL-167"/);
  assert.match(html, /data-ticket="DEL-176"/);
  // the Markdown export links every key token, not only a whole-value key
  const markdown = rulesMarkdown(data, 'http://x/');
  assert.match(markdown, /examples \[DEL-167\]\(http:\/\/x\/#ticket\/DEL-167\), \[DEL-176\]/);
});

test('markdownLinkTickets links whole key tokens and leaves prose alone', () => {
  assert.equal(
    markdownLinkTickets('DEL-167, DEL-176 and 3 stories', 'http://x/'),
    '[DEL-167](http://x/#ticket/DEL-167), [DEL-176](http://x/#ticket/DEL-176) and 3 stories',
  );
  assert.equal(markdownLinkTickets('no keys here', ''), 'no keys here');
});

test('object lane labels carry the ticket so the gutter links it', () => {
  const t = fakeTimeline([fakePr(52, 'DEL-167', 'DEL-1', T0)], 20 * DAY);
  t.dim = 'object';
  const rows = t.laneRows();
  assert.equal(rows[0].ticket, 'DEL-167');
  assert.match(rows[0].label, /DEL-167 #52/);
});

test('segmentHours never invents a duration for a missing segment', () => {
  assert.equal(segmentHours(45), '45 h');
  assert.equal(segmentHours(1.63), '1.6 h');
  assert.equal(segmentHours(null), '—');
  assert.equal(segmentHours(undefined), '—');
});

/* prd/63 — batch 2: the remaining misleading numbers and papercuts.
 *
 * One regression per item. The pure presentational fixes are tested on the
 * markup; wiring-only fixes (focus, the route, the CSS) have a source guard,
 * because a DOM-less runner cannot click them.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  chaseCard, deliveryTable, overviewMarkup,
} from '../tables.js';
import {
  attributeFacts, boundaryChips, eventsList, segmentBar, segmentCaveat,
  ticketStats,
} from '../ticket.js';
import { sprintStrip } from '../sprints.js';
import { responseTable } from '../queues.js';
import { parseRoute, serializeRoute } from '../route.js';
import { fmtAge } from '../format.js';
import { DAY, HOUR } from '../format.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const row = (overrides = {}) => ({
  object_id: 'PR:DEL/api#55', number: 55, repo: 'DEL/api', repo_short: 'api',
  tickets: ['DEL-168'], ticket: 'DEL-168', story_tickets: [],
  author: 'ErLi', state: 'open', ball: 'review', ball_label: 'review',
  phase: 'review', waiting_since: '2026-10-19T10:43:00Z',
  waiting_hours: 152, age_hours: 300, blocked: false, stale: true,
  stale_reasons: [], waiting_on: ['FrMo'], waiting_roles: { FrMo: 'sentinel' },
  reviewers_approved: 1, reviewers_total: 2, dependents: 0, impact_hours: 0,
  unobserved: true, ...overrides,
});

const del = (rows = [row()]) => ({
  rows,
  counts: { open: rows.length, blocked: 0, oldest_waiting: rows.length, all: rows.length, waiting_week_hours: 40 },
  done_open: { available: true, count: 0, rows: [] },
  snapshot: '2026-11-18T00:10:44Z',
});

/* ------------------------------------------------------------- V-A6 change */

test('V-A6: a Done-with-open-PR story names the unmerged share, and its rank is "to Jira done"', () => {
  const html = ticketStats({
    key: 'DEL-167',
    story: { points: 8 },
    segments: { cycle_hours: 73.31, measured: 9, rows: [], largest_label: 'created → ready', largest_share: 0.36 },
    benchmark: {
      available: true, rank: 13, n: 87, median_hours: 90.07,
      points: 8, points_median_hours: 109, censored: false, basis: 'rank',
    },
    change: { available: true, unmerged_share: 0.62, lines_unmerged: 160, lines_total: 258 },
    pr_count: 2,
  });
  assert.match(html, /13th fastest of 87 to Jira done/);
  assert.match(html, /62%<\/b> of the change unmerged/);
  assert.match(html, /data-unmerged-share="0.62"/);
  // the title says the cycle is not a delivery rank
  assert.match(html, /not a delivery rank/);
});

/* ------------------------------------------------------------- V-A7 labels */

test('V-A7: a segment states its no-PR share and a reopen loop by name', () => {
  assert.match(segmentCaveat({ no_pr_open_hours: 60, rework_hours: 0 }), /with no PR open/);
  assert.match(segmentCaveat({ no_pr_open_hours: 0, rework_hours: 16 }), /reopen loop/);
  const html = segmentBar({
    segments: {
      rows: [{
        key: 'verification_done', label: 'verification → done', available: true,
        hours: 25.75, share: 0.31, efficiency: 0.16, no_pr_open_hours: 0,
        rework_hours: 16, inverted: false, missing: [], reason: null,
      }],
    },
  });
  assert.match(html, /2 d 0 h is the reopen loop/);
});

/* -------------------------------------------------------------- V-A8 queue */

test('V-A8: the response table distinguishes "waiting only on the sentinel"', () => {
  const html = responseTable({
    response: {
      people: [{
        person: 'FrMo', role: 'sentinel', n: 107, median_hours: 6.25,
        mean_hours: 6.6, p90_hours: 12, never: 8,
        never_oldest_hours: 172, never_only_holder: 3, never_other_holder: 5,
      }],
      reviewer_median_hours: 3,
      sentinel_after_peers: true,
    },
  });
  assert.match(html, /8 <span class="muted"[^>]*>\(3 only\)/);
  assert.match(html, /\+5/);
  // prd/64 F-16: the oldest age is labelled as the oldest of the total 8, so it
  // cannot be read as the age of the "+5 other holder" subset.
  assert.match(html, /\(oldest of 8: 21 d 4 h\)/);
  assert.match(html, /data-response-role="sentinel"/);
  // a clickable row, as the hint promised
  assert.match(html, /class="response-row /);
  assert.match(html, /data-response-person="FrMo"/);
});

/* --------------------------------------------------------------- V-A9 sprint */

test('V-A9/V-C3: the sprint strip shows work finished in the window beside the membership verdict', () => {
  const html = sprintStrip({
    available: true,
    sprints: [{
      sprint_id: 2, state: 'closed', band_start: '2026-09-11T09:00:00Z',
      band_end: '2026-10-07T16:00:00Z', story_count: 30, points: 118,
      done_by_end: 15, committed: 5, adopted: 25,
      window_done: 23, window_points: 84, window_carry_in: 8, window_own: 15,
    }],
    scope: {}, carry_over: {}, delivery: {},
  });
  assert.match(html, /23 finished in the window/);
  assert.match(html, /84 pts/);
  assert.match(html, /8 carried in/);
});

/* ------------------------------------------------------------- V-A12 people */

test('V-A12: people are flagged only against the person baseline, not every row', () => {
  const html = overviewMarkup({
    delivery: del(),
    queues: {
      level: 'person',
      baseline_mean_wait_hours: 9,
      outlier_threshold_hours: 18,
      streams: [
        { id: 'FrMo', role: 'sentinel', summary: { diagnosis: 'healthy', mean_wait_hours: 11, mean_wip: 2 } },
        { id: 'CaOs', role: 'person', summary: { diagnosis: 'healthy', mean_wait_hours: 9, mean_wip: 0.6 } },
      ],
    },
    blocking: null,
  });
  assert.match(html, /a flag needs &gt; 2 d 2 h|a flag needs > 2 d 2 h/);
  assert.doesNotMatch(html, /2 overloaded people/);
});

/* -------------------------------------------------------------------- F10 */

test('F10: the chase card reconciles its count with the Rules sentinel-tail threshold', () => {
  const older = row({ object_id: 'PR:DEL/api#60', number: 60, waiting_hours: 131 });
  const html = chaseCard(del([row(), older]));
  assert.match(html, /orphaned approval/);
  assert.match(html, /Rules counts the 2 older than its 24 h sentinel-tail threshold/);
});

/* -------------------------------------------------------------------- F14 */

test('F14: every silent fallback has a one-line notice, and meta takes a tenant', () => {
  const main = read('main.js');
  assert.match(main, /function setNotice/);
  assert.match(main, /No dataset “\$\{requested\}”/);
  assert.match(main, /does not exist in dataset \$\{state\.tenant\} — showing everything/);
  assert.match(main, /This link names \$\{problems\.join\(' and '\)\}/);
  assert.match(main, /Dataset switch cleared the \$\{previousScope\.kind\} scope/);
  const index = read('index.html');
  assert.match(index, /id="route-notice"/);
  const server = read('../server/app.py');
  assert.match(server, /export function|def meta/);
  assert.match(server, /# prd\/63 F14: every other endpoint takes `\?tenant=`/);
  assert.match(server, /return \{?"ok": True, \*\*build\.VERSION_INFO, "tenant": name\}/);
});

/* -------------------------------------------------------------------- F15 */

test('F15: a role that filters no view is not offered, and an inert scope says so', () => {
  const main = read('main.js');
  assert.match(main, /filteredPages\(\{ kind: 'role', value: role \}\)\.size > 0/);
  assert.match(main, /control\.classList\.toggle\('scope-inert'/);
  assert.match(main, /this view is not filterable by scope/);
  const css = read('style.css');
  assert.match(css, /#scope\.scope-inert/);
});

/* -------------------------------------------------------------------- F16 */

test('F16: "unobserved" is shown only when informative, and the snapshot age is stated once', () => {
  const all = deliveryTable(del([row(), row({ object_id: 'PR:DEL/api#60', number: 60 })]), 'all');
  assert.doesNotMatch(all, />unobserved</);
  assert.match(all, /priced to the snapshot/);
  const some = deliveryTable(del([row(), row({ object_id: 'PR:DEL/api#60', number: 60, unobserved: false })]), 'all');
  assert.match(some, />unobserved</);
});

/* -------------------------------------------------------------------- F17 */

test('F17: boundaries are drawn in clock order and a quirk is flagged', () => {
  const html = boundaryChips({
    segments: {
      boundary_order: ['created', 'ready', 'refined', 'done'],
      boundaries: {
        created: { ts: '2026-10-19T09:40:00Z', available: true },
        ready: { ts: '2026-10-23T13:00:00Z', available: true },
        refined: {
          ts: '2026-10-23T13:20:00Z', available: true,
          order_quirk: { after: 'ready', after_ts: '2026-10-23T13:00:00Z', reason: 'x' },
        },
        done: { ts: '2026-11-04T15:46:00Z', available: true },
      },
    },
  });
  // ready (13:00) precedes refined (13:20) on screen, unlike lifecycle order
  assert.ok(html.indexOf('>ready<') < html.indexOf('>refined<'));
  assert.match(html, /data-order-quirk="true"/);
  assert.match(html, /data quirk: refined is dated after ready/);
});

test('F17: event attributes are labelled facts, never a raw k=v dump', () => {
  const facts = attributeFacts({
    actor_kind: 'human', log_delay_seconds: 72480, time_spent_seconds: 10800,
    from_status: 'in_review', to_status: 'done',
  });
  assert.deepEqual(facts.map((fact) => fact.label), [
    'by', 'logged late by', 'time spent', 'from', 'to',
  ]);
  assert.equal(facts[1].text, '2 d 4 h');
  const html = eventsList({
    events: [{
      ts: '2026-11-04T15:46:00Z', activity: 'worklogged', actor: 'DaKo',
      attributes: { time_spent_seconds: 10800, log_delay_seconds: 72480 },
    }],
  });
  assert.match(html, /<span class="k">time spent<\/span>/);
  assert.doesNotMatch(html, /time_spent_seconds=/);
});

test('F17: a no-STORY tenant shows the PR cycles, not "0/8 segments"', () => {
  const html = ticketStats({
    key: 'DEL-183', story: null, available: false, pr_count: 2,
    prs: [
      { number: 65, repo: 'DEL/api', state: 'merged', cycle_hours: 27.67 },
      { number: 66, repo: 'DEL/api', state: 'open', cycle_hours: 3.28, censored: true },
    ],
    segments: { measured: 0, rows: [{}, {}, {}, {}, {}, {}, {}, {}] },
  });
  assert.match(html, /no STORY object/);
  assert.match(html, /#65 merged/);
  assert.match(html, /right-censored/);
  assert.doesNotMatch(html, /0<\/b>\/8 segments/);
});

/* -------------------------------------------------------------------- F19 */

test('F19: a skip link, focus moves into the drawer, and a click behind closes it', () => {
  const html = read('index.html');
  assert.match(html, /class="skip-link" href="#main"/);
  assert.match(html, /id="drawer"[^>]*tabindex="-1"/);
  assert.match(html, /id="main" tabindex="-1"/);
  const main = read('main.js');
  assert.match(main, /state\.drawerOpener = active/);
  assert.match(main, /drawer\.focus\(\{ preventScroll: true \}\)/);
  assert.match(main, /opener\.focus\(\{ preventScroll: true \}\)/);
  assert.match(main, /!event\.target\.closest\('#drawer'\)/);
});

/* -------------------------------------------------------------------- F20 */

test('F20: the blocking basis is named and the Overview uses the role number', () => {
  const main = read('main.js');
  assert.match(main, /person level charges every concurrent holder/);
  assert.match(main, /the Overview headline uses role-level queue wait/);
  const tables = read('tables.js');
  assert.match(tables, /role-level handover graph, each handover once/);
});

/* --------------------------------------------------------------- F21 / F27 */

test('F21: presets compose, a run is visibly loading, and the scenario is in the URL', () => {
  const main = read('main.js');
  assert.match(main, /state\.policyScenario, \.\.\.chosen\.scenario/);
  assert.match(main, /data-policy-loading="true"/);
  assert.match(main, /policy: state\.page === 'policy' && !isBaselineScenario/);
  const policy = read('policy.js');
  assert.match(policy, /Presets <b>add to<\/b> the current/);
  assert.match(policy, /requests \/ replication/);
});

test('F27/V-B: Sources states the analysis window, the store span and each source run', () => {
  const main = read('main.js');
  assert.match(main, /\['analysis window'/);
  assert.match(main, /\['store span'/);
  assert.match(main, /runs\.map\(\(run\) =>/);
  assert.match(main, /data-source="\$\{esc\(run\.source\)\}"/);
});

/* -------------------------------------------------------------------- F22 */

test('F22: a 3 d 15 h old snapshot says 3 calendar days, not 4', () => {
  const now = 10 * DAY;
  assert.equal(fmtAge(now - (3 * DAY + 15 * HOUR), now), '3 calendar days 15 h ago');
  assert.equal(fmtAge(now - (5 * DAY + 2 * HOUR), now), '5 calendar days 2 h ago');
  assert.equal(fmtAge(now - 10 * DAY, now), '10 calendar days ago');
});

/* -------------------------------------------------------------------- F24 */

test('F24: the canvas precedes the prose, an open sprint names its plan end, and a row toggles', () => {
  const html = read('index.html');
  const stage = html.indexOf('class="stage timeline-stage"');
  const strip = html.indexOf('id="sprint-strip"');
  assert.ok(stage > 0 && strip > stage, 'the canvas is above the sprint strip');
  const stripHtml = sprintStrip({
    available: true,
    sprints: [{
      sprint_id: 3, state: 'active', open: true,
      band_start: '2026-10-08T09:00:00Z', band_end: '2026-11-18T00:10:00Z',
      planned_end: '2026-11-27', story_count: 30, done_by_end: 25,
    }],
    scope: {}, carry_over: {}, delivery: {},
  });
  assert.match(stripHtml, /plan 2026-11-27/);
  const main = read('main.js');
  assert.match(main, /String\(state\.sprint\) === String\(sprintRow\.dataset\.sprintId\)/);
  assert.match(main, /clearSprintFocus\(\);/);
});

/* -------------------------------------------------------------------- F26 */

test('F26: one WIP interval label, a scoped response table, one explanatory paragraph', () => {
  const queues = read('queues.js');
  assert.match(queues, /const basisMs = this\.data && this\.data\.bucket_hours/);
  // the response table takes the same scope as the chart
  assert.match(queues, /export function responseTable\(data, \{ benchmark = true, scope = null \}/);
  const main = read('main.js');
  assert.match(main, /responseTable\(data, \{[\s\S]*?scope: filteredPages/);
  // the duplicate "queue wait, not calendar utilisation" paragraph is gone
  assert.doesNotMatch(main, /Both labels are relative to the median wait/);
});

/* -------------------------------------------------------------------- F29 */

test('F29: separators collapse, the SLA paragraph has padding, theme links do not persist', () => {
  const html = read('index.html');
  assert.match(html, /id="dot-honesty"/);
  const main = read('main.js');
  assert.match(main, /if \(dot\) dot\.hidden = !value/);
  const css = read('style.css');
  assert.match(css, /\.chase-actions \{ padding: 0 14px/);
  assert.match(main, /save: false, repaint: false/);
});

/* -------------------------------------------------------------------- F30 */

test('F30: the trend metric/bucket round-trips, and a Delivery sprint link filters', () => {
  const url = serializeRoute({
    tenant: 'del', view: 'trend', trendMetric: 'merged', trendBucket: 'sprint',
  });
  assert.match(url, /metric=merged/);
  assert.match(url, /bucket=sprint/);
  const route = parseRoute(url, '');
  assert.equal(route.metric, 'merged');
  assert.equal(route.bucket, 'sprint');
  assert.equal(parseRoute('?metric=bogus', '').metric, null);
  const main = read('main.js');
  assert.match(main, /state\.page === 'timeline' \|\| state\.page === 'delivery'/);
  assert.match(main, /const sprintKeys = state\.page === 'delivery' \? sprintStoryKeys\(\)/);
  assert.match(main, /rowFilter: sprintKeys\.size \? \(row\) => rowInSprint/);
});

/* --------------------------------------------------------------------- V-B */

test('V-B: carry-over is a table, the live-state cards are marked, and the ticket links out', () => {
  const sprints = read('sprints.js');
  assert.match(sprints, /sprint-carry-table/);
  assert.match(sprints, /never re-committed/);
  const rules = read('rules.js');
  assert.match(rules, /data-live-state="true"/);
  const ticket = read('ticket.js');
  assert.match(ticket, /data-ticket-open="timeline"/);
  assert.match(ticket, /data-ticket-pr-open/);
  // no developer jargon in the rendered heuristic
  const tickets = read('../analysis/tickets.py');
  assert.doesNotMatch(tickets, /"the same boundaries and active-work accounting as the prd\/21/);
  assert.match(tickets, /Active "\s*\n\s*"time is the measured work inside each segment|time is the measured work inside each segment/);
});

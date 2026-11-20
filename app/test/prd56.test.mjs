/* prd/55 + prd/56 regressions: diagnosis consistency and UI papercuts.
 *
 * Each test names the QA finding it pins, so a re-broken view fails with the
 * sentence the exploratory run used.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  deliveryTable, matchesScope, overviewMarkup, scopeTargets,
} from '../tables.js';
import { blockingText, edgeSentence } from '../graph.js';
import { openPrState, ticketMarkdown } from '../ticket.js';
import { sprintStrip } from '../sprints.js';
import { rulesStats } from '../rules.js';
import { scopeBanner } from '../scope.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const row = (overrides = {}) => ({
  object_id: 'PR:a#53',
  number: 53,
  repo: 'DEL/delivery-api',
  repo_short: 'delivery-api',
  tickets: ['DEL-167'],
  story_tickets: ['DEL-167'],
  author: 'ErLi',
  state: 'open',
  ball: 'review',
  ball_label: 'review',
  waiting_for: 'review',
  waiting_on: ['CaOs', 'FrMo'],
  waiting_roles: { CaOs: 'review', FrMo: 'sentinel' },
  waiting_since: '2026-10-19T13:43:38Z',
  waiting_hours: 168.27,
  age_hours: 192,
  phase: 'review',
  phase_hours: { review: 19.81, active: 3.92, unobserved: 168.27 },
  stale: true,
  stale_reasons: ['ticket DEL-167 is Done in Jira but this PR is open'],
  unobserved: true,
  blocked: false,
  dependents: 0,
  impact_hours: 0,
  reviewers_total: 2,
  reviewers_approved: 0,
  reviewers_pending: 2,
  ...overrides,
});

/* ---- prd/56 §2: the pivot menu offers the holder, not only the author ---- */
test('the pivot menu offers the holder (FrMo) as well as the author (ErLi)', () => {
  const targets = scopeTargets({ row: row(), author: 'ErLi' });
  assert.deepEqual(targets.map((t) => t.person), ['CaOs', 'FrMo', 'ErLi']);
  assert.equal(targets[0].label, 'Scope to CaOs');
  assert.ok(targets.some((t) => t.holder && t.person === 'FrMo'));
  // the menu is positioned in CSS at the pointer (fixed) and the click carries
  // the chosen person, never a fallback to the author.
  const main = read('main.js');
  assert.match(main, /scopeTargets\(entity\)/);
  assert.match(main, /data-person=/);
  const css = read('style.css');
  assert.match(css, /\.menu\s*\{[^}]*position:\s*fixed/s);
});

/* ---- prd/56 §3: the blocking map is readable without hover ---- */
test('the blocking map has a text view with d h waits and named handovers', () => {
  const data = {
    nodes: [
      { id: 'sentinel', kind: 'role', hours: 1348, out_hours: 52, count: 122, wip_now: 2 },
      { id: 'FrMo', kind: 'person', role: 'sentinel', hours: 300, out_hours: 12, count: 4, wip_now: 1 },
    ],
    edges: [
      { from: 'sentinel', to: 'ready', hours: 375.8, count: 40, mean_hours: 9.4 },
    ],
  };
  const html = blockingText(data);
  // 1348 h is 168 d 4 h — the same rendering every other view uses
  assert.match(html, /168 d 4 h/);
  assert.doesNotMatch(html, /1348h/);
  assert.match(html, /text view/);
  assert.match(html, /waits on others/);
  assert.equal(edgeSentence({ from: 'sentinel', to: 'ready' }),
    'PRs waited for merge after approval');
  assert.match(html, /PRs waited for merge after approval/);
});

/* ---- prd/56 §4: the overview narrows under a scope and says so ---- */
test('a scoped overview filters its attention rows and tiles', () => {
  const delivery = {
    rows: [
      row(),
      row({ object_id: 'PR:a#60', number: 60, waiting_on: ['BoFe'], ball: 'author',
        author: 'ErLi', waiting_hours: 2, stale: false, unobserved: false }),
    ],
    counts: { open: 2, blocked: 0, oldest_waiting: 1, stale: 1, all: 2 },
    done_open: { available: true, count: 0, rows: [] },
  };
  const scope = { kind: 'person', value: 'FrMo', label: 'FrMo' };
  assert.equal(matchesScope(delivery.rows[0], scope), true);
  assert.equal(matchesScope(delivery.rows[1], scope), false);
  const html = overviewMarkup({ delivery, queues: null, blocking: null, scope });
  assert.match(html, /#53/);
  assert.doesNotMatch(html, /#60/);
  // the banner is derived from the same list pages the filters really touch
  assert.match(scopeBanner(scope, 'overview'), /this view \(overview\) is filtered to it/);
  assert.match(scopeBanner(scope, 'overview'), /overview/);
});

/* ---- prd/56 §5: copy-as-Markdown keeps a Done ticket's open-PR state ---- */
test('the ticket Markdown states the open PR wait and approvals', () => {
  const pr = {
    number: 53, repo: 'DEL/delivery-api', state: 'open', censored: true,
    cycle_hours: 192, waiting_hours: 168.27, waiting_on: ['CaOs', 'FrMo'],
    reviewers_approved: 0, reviewers_total: 2,
  };
  assert.equal(openPrState(pr), '0/2 approved · waiting 21 d 0 h on CaOs + FrMo');
  const text = ticketMarkdown({
    key: 'DEL-167', answer: '…', hours_mode: 'business', snapshot: '2026-11-18T00:10:44Z',
    story: {}, segments: { rows: [] }, prs: [], sprouts: {},
    open_prs: [pr],
    worklog: {},
  }, 'http://x/');
  assert.match(text, /still open/);
  assert.match(text, /0\/2 approved/);
  assert.match(text, /waiting 21 d 0 h on CaOs \+ FrMo/);
});

/* ---- prd/56 §1: the drawer states the wait and tiles the cycle ---- */
test('the PR drawer shows waiting since / waiting and a phase total', () => {
  const main = read('main.js');
  assert.match(main, /\['waiting since'/);
  assert.match(main, /\['waiting',/);
  assert.match(main, /unobserved \(no events since the snapshot\)/);
  assert.match(main, /\['phase total'/);
  assert.match(main, /cycle \(sum of phases\)/);
});

/* ---- prd/55 §5: an outlier sprint is flagged where it is read ---- */
test('a long sprint is flagged in the sprint strip', () => {
  const data = {
    available: true,
    sprints: [{
      sprint_id: 3, state: 'active', open: true,
      band_start: '2026-10-08T09:00:00Z', band_end: '2026-11-18T00:10:44Z',
      planned_end: '2026-11-27', days: 40.6, planned_days: 50.3,
      length_outlier: true,
      length_note: 'planned 50.3 calendar days vs 24.3–26.3 for the other sprints — its delivery share is not comparable',
      story_count: 30, stories: [], points: 125, done_by_end: 25,
      done_late: 0, never_done: 2, unmerged: 0, committed: 2, adopted: 28,
    }],
    scope: { stories: 100, in_sprint: 90, out_of_sprint: 10 },
    carry_over: { count: 0 },
  };
  const html = sprintStrip(data);
  assert.match(html, /length outlier/);
  assert.match(html, /50.3 calendar days planned/);
  assert.match(html, /not comparable/);
});

/* ---- prd/55 §6: the header states the window, not the dataset census ---- */
test('the rules header says "124 of 131 PRs" when the window drops seven', () => {
  const html = rulesStats({
    suggestions: [],
    sample: { prs: 124, stories: 85, days: 90 },
    dataset: { prs: 131, prs_in_window: 124, prs_outside_window: 7, days: 103.6 },
    at_stake: { hours: 0, pools: [] },
  });
  assert.match(html, /124<\/b> of 131 PRs \(7 opened before the window\)/);
  assert.match(html, /90<\/b> of 103.6 calendar days/);
});

/* ---- prd/56 §6: wording fixes ---- */
test('"sentinel waited on ready" reads as "PRs waited for merge after approval"', () => {
  const delivery = {
    rows: [], counts: { open: 0, blocked: 0, oldest_waiting: 0, stale: 0, all: 0 },
    done_open: { available: true, count: 0, rows: [] },
  };
  const html = overviewMarkup({
    delivery,
    queues: null,
    blocking: {
      edges: [{ from: 'sentinel', to: 'ready', hours: 375.8 }],
      totals: { hours: 375.8 },
    },
  });
  assert.match(html, /PRs waited for merge after approval/);
  assert.doesNotMatch(html, /sentinel<\/span> waited on/);
});

test('the delivery table defines "unobserved" and "d" inline', () => {
  const html = deliveryTable({
    rows: [row()],
    counts: { waiting_week_hours: 40 },
  }, 'all');
  assert.match(html, /unobserved = no events since the snapshot/);
  assert.match(html, /1 d = 8 h/);
});

test('the timeline says the canvas is calendar time, tables are business hours', () => {
  const main = read('main.js');
  assert.match(main, /canvas spans are calendar time \(wall clock\)/);
  assert.match(main, /business hours \(1 d = 8 h\)/);
  const timeline = read('timeline.js');
  assert.match(timeline, /calendar time \(wall clock\)/);
});

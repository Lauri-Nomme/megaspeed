/* prd/42 — second-order features and presentation.
 *
 * The markup helpers are pure, so the comparative answers ("who is the
 * constraint", "where does this story sit", "where did the carried story go")
 * can be asserted without a browser. The source guards pin the wiring that only
 * exists in main.js (the drawer's facts, the URL lens, the honesty dedupe).
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { legendMarkup, responseTable } from '../queues.js';
import { benchmarkLine, epicMarkup, ticketStats } from '../ticket.js';
import { correlationLine } from '../prsize.js';
import { sprintStrip } from '../sprints.js';
import { doneOpenMarkup, jargonTitle, overviewMarkup } from '../tables.js';
import { defaultScenario, isBaselineScenario, KNOBS, readScenario } from '../policy.js';
import { readFileSync } from 'node:fs';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

// --------------------------------------------------------------------- M1
test('prd/42 M1: the response table makes the sentinel gap one glance', () => {
  const html = responseTable({
    response: {
      reviewer_median_hours: 2.5,
      basis: 'added as reviewer → first response',
      sentinel_lag_basis: 'last peer approval → sentinel approval',
      people: [
        { person: 'FrMo', role: 'sentinel', n: 10, median_hours: 20.4,
          mean_hours: 21.0, p90_hours: 35.5, never: 4,
          sentinel_lag_median_hours: 6.6 },
        { person: 'AlNg', role: 'reviewer', n: 30, median_hours: 2.7,
          mean_hours: 2.8, p90_hours: 5.0, never: 0,
          sentinel_lag_median_hours: null },
      ],
    },
  });
  assert.match(html, /Response time/);
  assert.match(html, /data-response-person="FrMo"/);
  assert.match(html, /2 d 4 h/);          // 20.4 business hours
  assert.match(html, /×8\.2/);            // 20.4 / 2.5
  assert.match(html, /sentinel/);
  assert.match(html, /6 h 36 min/);       // the peer → sentinel lag
});

test('prd/42 M7: the legend names roles, never a real name', () => {
  const html = legendMarkup({
    legend: {
      note: 'roles, not names: the store holds pseudonyms only',
      people: [
        { person: 'FrMo', role: 'sentinel', prs_authored: 0, approvals: 107,
          stories_owned: 0 },
        { person: 'BiBi', role: 'bot', prs_authored: 0, approvals: 0,
          stories_owned: 0 },
        { person: 'LaAd', role: 'story owner', prs_authored: 0, approvals: 0,
          stories_owned: 30 },
      ],
    },
  });
  assert.match(html, /data-role="sentinel"/);
  assert.match(html, /data-role="bot"/);
  assert.match(html, /data-role="story owner"/);
  assert.match(html, /pseudonyms only/);
  assert.doesNotMatch(html, /frank/i);
});

// --------------------------------------------------------------------- M2
test('prd/42 M2: the ticket cycle is stated against its cohort', () => {
  const payload = {
    key: 'DEL-167',
    story: { points: 8 },
    segments: { cycle_hours: 73.31, measured: 9, rows: [], largest_label: 'created → ready', largest_share: 0.36 },
    benchmark: {
      available: true, rank: 73, n: 87, median_hours: 89.7,
      points: 8, points_median_hours: 109, points_n: 12,
      censored: false, basis: 'rank = 1 + shorter cycles',
    },
    pr_count: 2, reopened: false,
  };
  const stats = ticketStats(payload);
  assert.match(stats, /data-benchmark-rank="73"/);
  assert.match(stats, /73rd fastest of 87/);
  assert.match(stats, /median 89\.7 h/);
  assert.match(stats, /8-pt median 109 h/);
  // prd/46 §6: the last-ranked story is "slowest of N", not "87th fastest"
  const slowest = ticketStats({
    key: 'DEL-155', story: { points: 5 },
    segments: { cycle_hours: 200, measured: 8, rows: [], largest_label: 'review rounds', largest_share: 0.62 },
    benchmark: { available: true, rank: 87, n: 87, median_hours: 90.1, points: 5, points_median_hours: 80 },
    pr_count: 2,
  });
  assert.match(slowest, /slowest of 87/);
  assert.doesNotMatch(slowest, /87th/);
  assert.equal(benchmarkLine({ benchmark: { available: false } }), '');
});

// --------------------------------------------------------------------- M8
test('prd/42 M8: size vs cycle sits next to the size sliders', () => {
  const html = correlationLine({
    correlations: {
      n: 106, commits: 0.16, lines: 0.2, rounds: 0.6,
      basis: 'Pearson r over closed PRs',
    },
  });
  assert.match(html, /size vs cycle/);
  assert.match(html, /r = 0\.16 for commits/);
  assert.match(html, /0\.20 for changed lines/);
  assert.match(html, /0\.60 for review rounds/);
  assert.match(html, /weak/);
  assert.match(html, /not a proven lever/);
});

// --------------------------------------------------------------------- M5
test('prd/42 M5: a sprint names scope added after the start and the carry destination', () => {
  const html = sprintStrip({
    available: true,
    sprints: [{
      sprint_id: 2, state: 'closed', band_start: '2026-09-11T00:00:00Z',
      band_end: '2026-10-07T00:00:00Z', start: '2026-09-11T00:00:00Z',
      story_count: 30, points: 118, done_by_end: 15, done_late: 12,
      never_done: 3, added_after_start: 25, added_after_start_stories: ['DEL-153'],
      committed: 5, adopted: 25, points_share: 0.44,
      carried_in: [], carried_out: ['DEL-153'],
    }],
    carry_over: {
      count: 1, rows: [{
        key: 'DEL-153', sprints: [2], status: 'never',
        destination_sprint: 3, reappears: false,
      }],
      never_reappeared: 1,
    },
  }, { focus: 2 });
  assert.match(html, /25 adopted/);
  assert.match(html, /5 committed at start/);
  assert.doesNotMatch(html, /\+25 after start/);
  assert.match(html, /never re-committed/);
  assert.match(html, /never reappear in the next sprint/);
});

// --------------------------------------------------------------------- M4
test('prd/42 M4: the Done-in-Jira list links the ticket and names who owes', () => {
  const html = doneOpenMarkup({
    done_open: {
      available: true, count: 1, opened_after_close: 1,
      note: 'a ticket is Done when its final done boundary exists',
      rows: [{
        key: 'DEL-183', number: 66, repo_short: 'delivery-api',
        ticket_closed: '2026-11-04T12:00:00Z',
        opened_after_close: true, opened_after_close_hours: 8,
        waiting_on: ['BoFe'],
      }],
    },
  });
  assert.match(html, /#ticket\/DEL-183/);
  assert.match(html, /BoFe/);
  assert.match(html, /PR opened after the ticket closed/);
  assert.match(html, /1 d 0 h later/);
  // and the overview actually renders the section
  const overview = overviewMarkup({
    delivery: { counts: { open: 1, blocked: 1, oldest_waiting: 0 }, rows: [], done_open: {
      available: true, count: 1, opened_after_close: 0, rows: [{
        key: 'DEL-167', number: 53, repo_short: 'delivery-api',
        ticket_closed: '2026-10-19T12:00:00Z', waiting_on: ['CaOs'],
      }],
    } },
    queues: { streams: [] }, blocking: { nodes: [], edges: [] },
  });
  assert.match(overview, /Done in Jira, PR open/);
  assert.match(overview, /#ticket\/DEL-167/);
});

// --------------------------------------------------------------------- M3
test('prd/42 M3: refinement and round-cap knobs round-trip', () => {
  const keys = KNOBS.map((knob) => knob.key);
  assert.ok(keys.includes('refine_before_start'));
  assert.ok(keys.includes('review_round_cap'));
  const scenario = { ...defaultScenario(), refine_before_start: true, review_round_cap: 2 };
  assert.equal(isBaselineScenario(scenario), false);
  assert.equal(isBaselineScenario(defaultScenario()), true);
  const root = {
    querySelector: (selector) => {
      const key = /data-knob="(.+)"/.exec(selector)?.[1];
      const values = {
        wip_limit: '', sentinels: '1', review_sla_hours: '',
        size_cap_commits: '', stack_depth_cap: '', review_round_cap: '2',
      };
      if (key === 'refine_before_start') return { type: 'checkbox', checked: true };
      if (key === 'review_first') return { type: 'checkbox', checked: false };
      return key ? { type: 'select', value: values[key] ?? '' } : null;
    },
  };
  const readback = readScenario(root);
  assert.equal(readback.refine_before_start, true);
  assert.equal(readback.review_round_cap, 2);
});

// --------------------------------------------------------------------- P9
test('prd/42 P9: an epic is stated as a grouping, not a story', () => {
  const html = epicMarkup({
    key: 'DEL-1', kind: 'epic',
    answer: 'DEL-1 is an epic — a parent grouping of 1 story, not a story.',
    epic: {
      state: 'open', story_count: 1, points: 3, open_stories: 0,
      stories: [{ key: 'DEL-220', state: 'done', points: 3, cycle_hours: 20, censored: false }],
    },
  });
  assert.match(html, /EPIC/);
  assert.match(html, /not a story/);
  assert.match(html, /#ticket\/DEL-220/);
  assert.match(ticketStats({ kind: 'epic', epic: { story_count: 1, points: 3 } }), /not a story/);
});

// --------------------------------------------------------------------- P11
test('prd/42 P11: jargon has an explanation', () => {
  assert.match(jargonTitle('unobserved'), /no events since the snapshot/);
  assert.match(jargonTitle('sentinel waited on ready'), /ready to merge/);
});

// --------------------------------------------------- source-level wiring
test('prd/42 M6/P6: the PR drawer shows review facts and one cycle duration', () => {
  const main = read('main.js');
  assert.match(main, /\['reviewers', row\.reviewers_total/);
  assert.match(main, /\['open threads', String\(row\.open_threads/);
  assert.match(main, /\['last event'/);
  assert.match(main, /\['cycle \(business hours\)'/);
});

test('prd/42 B2: the drawer reserves space instead of covering the inspector', () => {
  const main = read('main.js');
  const css = read('style.css');
  assert.match(main, /classList\.add\('drawer-open'\)/);
  assert.match(main, /classList\.remove\('drawer-open'\)/);
  assert.match(css, /\.app\.drawer-open \.main \{ margin-right: 384px; \}/);
});

test('prd/42 P3/P7: the queue states its basis and the honesty line dedupes the clock', () => {
  const main = read('main.js');
  assert.match(main, /relative to the median wait in this window/);
  assert.match(main, /business\|wall-clock\) hours/);
});

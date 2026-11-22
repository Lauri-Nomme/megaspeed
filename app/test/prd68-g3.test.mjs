/* prd/68 G3 — no-PR honesty in the UI (F-04, F-07, F-10, F-12).
 *
 * The live `demo-tlla` corpus holds 2,320 stories and 0 PRs. These tests pin
 * the four client-side findings that came from treating the absent PR layer as
 * a measured zero:
 *
 * - F-04: a calibration with a measured refinement keeps `refine before start`
 *   enabled (only a null `refinement.hours` may disable it);
 * - F-07: "too few for a rate" is a small-cohort note, not the reason a
 *   Jira-only rolled %C&A is unavailable — that is `– / 84`, never `0 / 84`,
 *   and the ticket export says "reviewed not measured";
 * - F-10: with no PRs the Done-in-Jira panel says "unavailable", not "every
 *   ticket has its code merged";
 * - F-12: an epic exports its header and child table, never the story template
 *   with "logged — in undefined worklogs".
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  defaultScenario, policyConfigMarkup, refinementUnavailable,
  refinementUnavailableReason,
} from '../policy.js';
import { yieldMarkdown, yieldPanel } from '../yield.js';
import { doneOpenMarkup } from '../tables.js';
import { ticketMarkdown } from '../ticket.js';

/* ---- F-04: the refine knob on a Jira-only store ----------------------- */

function calibration(refinement) {
  return {
    ok: false,
    available: false,
    note: 'no reviewer response was observed in this window',
    prs: { n: 0 },
    ...(refinement === undefined ? {} : { refinement }),
  };
}

const measuredRefinement = {
  hours: 6.84, n: 133, basis: 'median created → ready segment of this cohort',
};
const emptyRefinement = {
  hours: null, n: 0, basis: 'no measured created → ready segment in this cohort',
};

test('F-04: a Jira-only store with a measured refinement keeps refine enabled', () => {
  const cal = calibration(measuredRefinement);
  assert.equal(refinementUnavailable(cal), false);
  assert.equal(refinementUnavailableReason(cal), null);
  const markup = policyConfigMarkup(defaultScenario(), cal);
  const refine = markup.match(/<input[^>]*data-knob="refine_before_start"[^>]*>/)[0];
  assert.doesNotMatch(refine, /disabled/);
  assert.doesNotMatch(refine, /aria-disabled/);
  // the false claim the QA run found must not appear anywhere on the page
  assert.doesNotMatch(markup, /no Jira layer/);
  assert.doesNotMatch(markup, /not measurable/);
  // the preset is enabled too, so the policy can actually be run
  const preset = markup.match(/<button[^>]*data-preset="refine"[^>]*>/)[0];
  assert.doesNotMatch(preset, /disabled/);
});

test('F-04: a truly empty refinement disables the knob with its real reason', () => {
  const cal = calibration(emptyRefinement);
  assert.equal(refinementUnavailable(cal), true);
  const reason = refinementUnavailableReason(cal);
  assert.match(reason, /no measured created → ready segment/);
  assert.doesNotMatch(reason, /no Jira layer/);
  const markup = policyConfigMarkup(defaultScenario(), cal);
  const refine = markup.match(/<input[^>]*data-knob="refine_before_start"[^>]*>/)[0];
  assert.match(refine, /disabled/);
  assert.match(markup, /id="policy-knob-refine_before_start-note"[^>]*>not measurable — no measured created → ready segment/);
});

test('F-04: only a missing refinement block may mean "no Jira layer"', () => {
  const cal = calibration(undefined);       // no `refinement` key at all
  assert.equal(refinementUnavailable(cal), true);
  assert.match(refinementUnavailableReason(cal), /no Jira layer/);
  // no calibration in hand: leave the control alone
  assert.equal(refinementUnavailable(undefined), false);
});

/* ---- F-07: rolled %C&A when Reviewed is unfalsifiable ----------------- */

function yieldPayload(overrides = {}) {
  return {
    available: true,
    cohort: 'completed stories',
    n: 84,
    min_deliveries: 10,
    steps: [
      { id: 'refine', label: 'Refined', clean_n: 77, n: 84, rate: 0.917,
        available: true, signals: ['ready → backlog'], items: [] },
      { id: 'develop', label: 'Developed', clean_n: 83, n: 84, rate: 0.988,
        available: true, signals: ['in_review → in_progress'], items: [] },
      // the Jira-only step: no merged PR, so the store cannot falsify it
      { id: 'review', label: 'Reviewed', clean_n: null, n: 0, rate: null,
        available: false, unfalsifiable: true, rate_hidden: false,
        signals: ['zero review rounds across every merged PR'],
        note: 'no review signal in this store', items: [] },
      { id: 'verify', label: 'Verified', clean_n: 82, n: 84, rate: 0.976,
        available: true, signals: ['in_verification → in_progress'], items: [] },
    ],
    rolled: null,
    clean_joint: 0,
    joint_rate: 0,
    no_pr: 84,
    basis: '%C&A = the share of items that left this step complete and accurate.',
    note: 'no STORY objects in this store: there is no pipeline to measure first-pass yield over',
    implied: { steps: {}, basis: 'the lab did not run for this request' },
    ...overrides,
  };
}

const metrics = (yieldOverrides = {}) => ({ yield: yieldPayload(yieldOverrides) });

test('F-07: an unfalsifiable step is "unavailable", not "too few for a rate"', () => {
  const html = yieldPanel(metrics());
  assert.match(html, /data-yield-unavailable/);
  assert.match(html, /Rolled %C&amp;A is unavailable because Reviewed is not measured in this store\./);
  assert.doesNotMatch(html, /too few for a rate/);
  // the joint count is unknown, not zero: `– / 84`, never `0 / 84`
  assert.match(html, /<span class="yield-n mono">– \/ 84<\/span>/);
  assert.doesNotMatch(html, /0 \/ 84/);
  // the step's own rate stays a dash with its note
  const review = html.slice(html.indexOf('data-yield-step="review"'),
    html.indexOf('data-yield-step="verify"'));
  assert.match(review, /class="yield-rate">–</);
  assert.match(review, /unfalsifiable/);
});

test('F-07: a genuinely small cohort still says "too few for a rate"', () => {
  const small = yieldPayload({
    n: 4,
    steps: yieldPayload().steps.map((step) => (step.id === 'review'
      ? { ...step, available: true, clean_n: 3, n: 4, rate: 0.75, note: '' }
      : { ...step, clean_n: 4, n: 4 })),
    clean_joint: 2,
  });
  const html = yieldPanel({ yield: small });
  assert.match(html, /data-yield-too-few/);
  assert.match(html, /4 deliveries — too few for a rate \(minimum 10\)/);
  assert.doesNotMatch(html, /data-yield-unavailable/);
  assert.match(html, /<span class="yield-n mono">2 \/ 4<\/span>/);
});

test('F-07: the Markdown export states the same unavailable reason', () => {
  const lines = yieldMarkdown(metrics()).join('\n');
  assert.match(lines, /Rolled %C&A is unavailable because Reviewed is not measured in this store\./);
  assert.doesNotMatch(lines, /too few for a rate/);
  assert.match(lines, /\| Reviewed \| not measured \| – \/ 0 \|/);
});

/* ---- F-07: the ticket "first pass" line ------------------------------ */

function ticketPayload() {
  return {
    key: 'DEMO-57062',
    answer: 'DEMO-57062 took 5 d 2 h from created to done',
    hours_mode: 'business',
    snapshot: '2026-11-22T00:14:11Z',
    story: { points: 3, epic_key: 'DEMO-12350', assignee: 'LaNo', created: '2026-08-01T09:00:00Z' },
    segments: { rows: [] },
    prs: [],
    pr_count: 0,
    sprints: { memberships: [] },
    worklog: {},
    yield: {
      steps: [
        { id: 'refine', label: 'Refined', clean: true, available: true, signals: [] },
        { id: 'develop', label: 'Developed', clean: true, available: true, signals: [] },
        { id: 'review', label: 'Reviewed', clean: false, available: false,
          note: 'no merged PR in this store', signals: [] },
        { id: 'verify', label: 'Verified', clean: true, available: true, signals: [] },
      ],
      rolled_clean: false,
      available: true,
    },
  };
}

test('F-07: the ticket export says "reviewed not measured", never "bounced"', () => {
  const text = ticketMarkdown(ticketPayload());
  assert.match(text, /reviewed not measured/);
  assert.doesNotMatch(text, /reviewed bounced/);
  assert.match(text, /not measurable for this story/);
  assert.doesNotMatch(text, /a loop-back somewhere/);
  // the measured steps keep their verdict
  assert.match(text, /refined clean · developed clean/);
});

/* ---- F-10: Done in Jira, PR open with 0 PRs -------------------------- */

test('F-10: a 0-PR store renders "unavailable", never "every ticket merged"', () => {
  const html = doneOpenMarkup({
    done_open: {
      available: false,
      count: 0,
      rows: [],
      unavailable_reason: 'unavailable: this dataset holds no pull requests, ' +
        "so whether a Done ticket's code was merged cannot be checked here",
    },
  });
  assert.match(html, /data-done-open-unavailable/);
  assert.match(html, /<span class="pill neutral">unavailable<\/span>/);
  assert.match(html, /this dataset holds no pull requests/);
  assert.doesNotMatch(html, /pill healthy/);
  assert.doesNotMatch(html, /Every ticket marked Done in Jira has its code merged/);
  assert.doesNotMatch(html, /<span class="pill blocked">/);
});

test('F-10: a store with PRs keeps the measured panel', () => {
  const html = doneOpenMarkup({
    done_open: {
      available: true,
      count: 1,
      opened_after_close: 0,
      rows: [{
        key: 'DEL-183', repo: 'ENG/delivery-api', repo_short: 'delivery-api',
        number: 66, waiting_on: ['BoFe'],
      }],
      note: 'a ticket is Done when its final done boundary exists',
    },
  });
  assert.match(html, /data-done-open="DEL-183"/);
  assert.match(html, /<span class="pill blocked">1<\/span>/);
  assert.doesNotMatch(html, /data-done-open-unavailable/);
});

/* ---- F-12: the epic Markdown export ---------------------------------- */

function epicPayload() {
  return {
    ok: true,
    key: 'DEMO-12350',
    kind: 'epic',
    snapshot: '2026-11-22T00:14:11Z',
    hours_mode: 'business',
    answer: 'DEMO-12350 is an epic — a parent grouping of 2 stories, not a story.',
    epic: {
      key: 'DEMO-12350',
      state: 'done',
      story_count: 2,
      points: 11,
      stories: [
        { key: 'DEMO-1', state: 'done', points: 8, cycle_hours: 20, censored: false },
        { key: 'DEMO-2', state: 'open', points: 3, cycle_hours: 12, censored: true },
      ],
    },
    story: null,
    segments: {},
    prs: [],
    pr_count: 0,
    sprints: { memberships: [] },
    worklog: {},
  };
}

test('F-12: the epic export carries the header and the child table', () => {
  const text = ticketMarkdown(epicPayload(), 'http://127.0.0.1:8766/');
  assert.match(text, /^# DEMO-12350 — epic/);
  // the header: state, story count and points
  assert.match(text, /\| DEMO-12350 \| done \| 2 \| 11 \|/);
  assert.match(text, /child stories \(2\)/);
  // the children, linked, with state / points / cycle
  assert.match(text, /\| \[DEMO-1\]\(http:\/\/127\.0\.0\.1:8766\/#ticket\/DEMO-1\) \| done \| 8 \| 2 d 4 h \|/);
  assert.match(text, /\| \[DEMO-2\]\(http:\/\/127\.0\.0\.1:8766\/#ticket\/DEMO-2\) \| open \| 3 \| 1 d 4 h \|/);
  assert.doesNotMatch(text, /open · open/);
  // no story template leaked into the epic export
  assert.doesNotMatch(text, /undefined/);
  assert.doesNotMatch(text, /worklogs?/);
  assert.doesNotMatch(text, /logged —/);
  assert.doesNotMatch(text, /## pull requests/);
  assert.doesNotMatch(text, /## sprints/);
});

test('F-12: an epic with no children says so instead of exporting an empty template', () => {
  const payload = epicPayload();
  payload.epic = { ...payload.epic, stories: [], story_count: 0, points: 0 };
  const text = ticketMarkdown(payload);
  assert.match(text, /child stories \(0\)/);
  assert.match(text, /No story in this store names this epic/);
  assert.doesNotMatch(text, /undefined/);
});

/* prd/67 W-04 — the verdict ranks what is owed now, not the historical tail.
 *
 * A Jira-only window has `live_hours = 0` at every station. The card must say
 * "no current queue" and show the largest historical tails as history — never
 * "The constraint is Refinement … 0 h currently owed and 2312 h of abnormal
 * tail". The normal (live > 0) state is unchanged.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  constraintSentence, historicalMarkup, historicalRows, nextConstraint,
  nextLine, owedLabel, poolRows, verdictBanner, verdictCard, verdictMarkdown,
} from '../verdict.js';

const NOTE = 'No current queue is visible in this window: 0 h owed now. '
  + 'The largest historical tails are Refinement (2312 h) and Starting work (1817 h); '
  + 'this is history, not a live constraint.';

function rung(step, state, overrides = {}) {
  return {
    step, label: step[0].toUpperCase() + step.slice(1), state, reason: null,
    rules: [], labs: [], note: '', claims: 'lab', ...overrides,
  };
}

/** The Jira-only shape: completed work, no live PR, big historical tails. */
export const NO_QUEUE = {
  ok: true,
  as_of: '2026-11-18T00:10:44Z',
  available: true,
  constraint: null,
  historical: [
    { station: 'refine', label: 'Refinement', tail_hours: 2312 },
    { station: 'start', label: 'Starting work', tail_hours: 1817 },
    { station: 'review', label: 'Approval gate', tail_hours: 411 },
  ],
  pools: [
    { station: 'refine', label: 'Refinement', removable_hours: 2312, removable_now_hours: 0, live_hours: 0, tail_hours: 2312, n_completed: 2312 },
    { station: 'start', label: 'Starting work', removable_hours: 1817, removable_now_hours: 0, live_hours: 0, tail_hours: 1817, n_completed: 2140 },
    { station: 'review', label: 'Approval gate', removable_hours: 411, removable_now_hours: 0, live_hours: 0, tail_hours: 411, n_completed: 2088 },
  ],
  holders: { clock: 'pr-holder', people: [], available: false },
  adjacent: [],
  exploit: { items: [], orphans: [], n: 0, hours: 0, note: '' },
  ladder: [
    rung('exploit', 'open', {
      rules: [{ id: 'jira-waiting-time', title: 'Waiting time is unmeasured', impact_hours: 100, lab: { note: 'no lab verdict' } }],
      note: 'the rules claim the ticket-level wait',
      claims: 'rules',
    }),
    rung('subordinate', 'done', {}),
    rung('elevate', 'done', {}),
  ],
  next: [],
  current_rung: 'exploit',
  liveness: { open: 0, live: 0, dead: 0, unobserved: 0, unmerged: 0, open_prs: 0 },
  note: NOTE,
  pool_note: 'Pool = business hours on the team calendar.',
  holder_note: 'Holder hours are person-hours with the ball in review.',
  basis: 'the verdict ranks the queue owed now',
};

/** The normal shape (live > 0), kept here to pin "the normal state is unchanged". */
export const LIVE = {
  ...NO_QUEUE,
  constraint: {
    station: 'review', label: 'Approval gate', wait_label: 'review',
    removable_hours: 619.1, live_hours: 237.54, tail_hours: 381.56,
    removable_now_hours: 237.54, history_hours: 3328.7, n_completed: 87,
    p85_hours: 70.78, open_n: 4, clock: 'story', hours_kind: 'story',
    tied_with: [],
    items: [{ key: 'DEL-176', hours: 144, pr: '#60', holders: ['FrMo'] }],
  },
  historical: [
    { station: 'review', label: 'Approval gate', tail_hours: 381.56 },
  ],
  pools: [
    { station: 'review', label: 'Approval gate', removable_hours: 619.1, removable_now_hours: 237.54, live_hours: 237.54, tail_hours: 381.56, n_completed: 87 },
    { station: 'verify', label: 'Verification', removable_hours: 80.85, removable_now_hours: 0, live_hours: 0, tail_hours: 80.85, n_completed: 87 },
  ],
  next: [{ station: 'verify', label: 'Verification', removable_hours: 80.85 }],
  note: 'Pool = business hours on the team calendar.',
};

test('the no-current-queue sentence is the note, never "The constraint is"', () => {
  assert.equal(constraintSentence(NO_QUEUE), NOTE);
  assert.doesNotMatch(constraintSentence(NO_QUEUE), /The constraint is/);
});

test('the card renders the no-queue state and keeps the exploit rules', () => {
  const html = verdictCard(NO_QUEUE);
  assert.match(html, /data-verdict-state="no-queue"/);
  assert.match(html, /data-available="true"/);
  assert.match(html, /no current queue/);
  assert.match(html, /data-verdict-sentence[^>]*>No current queue is visible/);
  // no constraint claim, no 0-owed sentence, no definition list of zeros
  assert.doesNotMatch(html, /The constraint is/);
  assert.doesNotMatch(html, /0 h of removable/);
  assert.doesNotMatch(html, /data-pool="removable"/);
  assert.doesNotMatch(html, /data-pool="owed"/);
  // the ladder's exploit rules still travel; nothing names a station as the
  // current constraint
  assert.match(html, /data-verdict-ladder/);
  assert.match(html, /data-rung="exploit"/);
  assert.match(html, /jira-waiting-time/);
  // history is shown as history, not as a "next constraint"
  assert.match(html, /data-verdict-historical/);
  assert.match(html, /History, not a queue: Refinement ≈2.3k h/);
  assert.match(html, /data-historical-station="start"/);
  assert.doesNotMatch(html, /data-next-constraint/);
  assert.doesNotMatch(html, /Next:/);
});

test('the historical list is the payload order, and only when there is no constraint', () => {
  assert.deepEqual(historicalRows(NO_QUEUE).map((row) => row.station),
    ['refine', 'start', 'review']);
  assert.equal(historicalMarkup(NO_QUEUE).includes('≈2.3k h'), true);
  // a live constraint never renders the history block
  assert.equal(historicalMarkup(LIVE), '');
  assert.equal(historicalMarkup({ ...NO_QUEUE, available: false }), '');
});

test('no next-constraint ranking when no station owes work now', () => {
  assert.deepEqual(nextConstraint(NO_QUEUE), []);
  assert.equal(nextLine(NO_QUEUE), 'no other station carries removable wait');
  assert.equal(verdictCard(NO_QUEUE).includes('data-next-constraint'), false);
});

test('the Rules banner shows the note, not a station called "the constraint"', () => {
  const html = verdictBanner(NO_QUEUE);
  assert.match(html, /data-verdict-banner/);
  assert.match(html, /data-verdict-state="no-queue"/);
  assert.match(html, /no current queue/);
  assert.match(html, /No current queue is visible in this window/);
  assert.match(html, /Refinement \(2312 h\)/);
  assert.doesNotMatch(html, /The constraint is/);
  assert.doesNotMatch(html, /the constraint<\/span>/);
  // a refusal stays bannerless, as before
  assert.equal(verdictBanner({ ...NO_QUEUE, available: false }), '');
});

test('the Markdown export names the history, never a constraint', () => {
  const markdown = verdictMarkdown(NO_QUEUE);
  assert.match(markdown, /^# The constraint/);
  assert.match(markdown, /No current queue is visible in this window/);
  assert.match(markdown, /_Historical tails \(not a current queue\): Refinement ≈2.3k h/);
  assert.doesNotMatch(markdown, /The constraint is/);
  assert.doesNotMatch(markdown, /_Next:/);
  assert.match(markdown, /## Exploit — open/);
});

test('owedLabel is null-safe and never invents removable hours', () => {
  assert.equal(owedLabel(null), '0 h on 0 stories');
  assert.equal(owedLabel({ live_hours: 237.54, open_n: 4 }), '238 h on 4 stories');
  assert.equal(owedLabel({ live_hours: 10, open_n: 1 }), '10 h on 1 story');
});

test('the normal live state is unchanged', () => {
  const sentence = constraintSentence(LIVE);
  assert.match(sentence, /^The constraint is the approval gate\./);
  assert.match(sentence, /619 h of removable review wait/);
  assert.match(sentence, /238 h currently owed and 382 h of abnormal tail/);
  const html = verdictCard(LIVE);
  assert.match(html, /data-pool="removable"/);
  assert.match(html, /data-pool="owed"/);
  assert.match(html, /data-verdict-ladder/);
  assert.match(html, /data-next-constraint/);
  assert.doesNotMatch(html, /data-verdict-historical/);
  assert.deepEqual(poolRows(LIVE).map((row) => row.id),
    ['removable', 'owed', 'tail', 'holders', 'oldest']);
  assert.match(verdictBanner(LIVE), /The constraint is the approval gate\./);
});

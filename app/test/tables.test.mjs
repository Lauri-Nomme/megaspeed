/* Markup-level tests for the ticket-labelled surfaces (QA round 2, B2/B19).
 *
 * `tables.js` returns strings, so the delivery table, detail panel and impact
 * sentence can be asserted without a browser. Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { annotateRow, epicKeySet } from '../tickets.js';
import { deliveryTable, detailMarkup, entityMarkdown, impactSentence } from '../tables.js';

function openRow(overrides = {}) {
  return annotateRow({
    object_id: 'PR:DEL/delivery-web#52',
    repo_short: 'delivery-web',
    number: 52,
    tickets: ['DEL-1', 'DEL-197'],
    author: 'AlNg',
    state: 'open',
    ball: 'review',
    ball_label: 'review',
    phase: 'review',
    kind: 'pr_approved',
    waiting_since: '2026-11-13T12:07:00Z',
    waiting_hours: 2.9,
    age_hours: 16.4,
    blocked: false,
    dependents: 1,
    impact_hours: 2.9,
    commits: 2,
    ...overrides,
  }, epicKeySet([['DEL-1', 'DEL-197'], ['DEL-1', 'DEL-167'], ['DEL-1', 'DEL-176'], ['DEL-1', 'DEL-198']]));
}

test('the impact sentence names the PR key and agrees in number', () => {
  const one = impactSentence(openRow({ dependents: 1 }));
  assert.match(one, /1 other open PR shares ticket DEL-197/);
  const two = impactSentence(openRow({ dependents: 2, impact_hours: 5.8 }));
  assert.match(two, /2 other open PRs share ticket DEL-197/);
  const none = impactSentence(openRow({ dependents: 0, impact_hours: 0 }));
  assert.match(none, /no other open PR shares ticket DEL-197/);
});

test('the delivery table shows the story key, never the epic key', () => {
  const html = deliveryTable({ rows: [openRow()] }, 'all');
  assert.match(html, />DEL-197</);
  assert.doesNotMatch(html, />DEL-1</);
  assert.match(html, /#52/);
});

test('the detail panel and markdown name the story key and keep the epic separate', () => {
  const row = openRow();
  const detail = detailMarkup({
    row,
    facts: [['ticket', 'DEL-197'], ['epic', 'DEL-1']],
    children: ['DEL-197', 'DEL-1'],
  });
  assert.match(detail, /DEL-197 #52 delivery-web<\/a>/);
  assert.match(detail, /data-ticket="DEL-197"/);
  assert.match(detail, /DEL-1/); // epic fact / related chip, never the name
  const markdown = entityMarkdown({ row });
  assert.match(markdown, /## \[DEL-197\]\(#ticket\/DEL-197\) #52 delivery-web/);
  assert.match(markdown, /- epic: DEL-1/);
});

/* prd/41 B3: one waiting-week threshold (40 business hours) everywhere. */
test('the oldest tab and the week label read the payload threshold', async () => {
  const { tabRows, weekLabel, reviewStateLine, waitingLabel } = await import('../tables.js');
  const data = {
    counts: { open: 5, oldest_waiting: 3, waiting_over_week: 3,
      waiting_week_hours: 40, waiting_week_days: 5 },
    rows: [
      openRow({ object_id: 'PR:a#1', number: 1, waiting_hours: 168 }),
      openRow({ object_id: 'PR:a#2', number: 2, waiting_hours: 152 }),
      openRow({ object_id: 'PR:a#3', number: 3, waiting_hours: 132 }),
      openRow({ object_id: 'PR:a#4', number: 4, waiting_hours: 20 }),
      openRow({ object_id: 'PR:a#5', number: 5, waiting_hours: 16 }),
    ],
  };
  assert.equal(weekLabel(data), '5 d = 40 h');
  assert.deepEqual(tabRows(data, 'oldest').map((row) => row.number), [1, 2, 3]);
  // a row just past the 40 h week is "waiting 1w+", never at 168 h
  const justOver = openRow({ waiting_hours: 41 });
  assert.equal(tabRows({ rows: [justOver], counts: data.counts }, 'oldest').length, 1);
});

/* prd/41 B6: #53 is 0/2 approved, and both reviewers owe a verdict. */
test('the state line says 0/2 approved and names every pending reviewer', async () => {
  const { reviewStateLine, waitingLabel } = await import('../tables.js');
  const row = openRow({
    ball: 'review', ball_label: 'review', waiting_on: ['CaOs', 'FrMo'],
    reviewers_total: 2, reviewers_pending: 2, reviewers_approved: 0,
  });
  assert.equal(reviewStateLine(row), '0/2 approved · 2/2 reviewers pending');
  assert.equal(waitingLabel(row), 'CaOs + FrMo');
  // a single named holder keeps its name
  assert.equal(waitingLabel(openRow({ ball_label: 'FrMo', waiting_on: ['FrMo'] })), 'FrMo');
  // prd/63 D: the author-debt row names the same holder set as "who owes" —
  // DEL-183 #66 reads "BoFe + FrMo" on both surfaces, never "author".
  assert.equal(waitingLabel(openRow({ ball: 'author', ball_label: 'author',
    waiting_on: ['BoFe', 'FrMo'] })), 'BoFe + FrMo');
});

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
  assert.match(detail, /DEL-197 #52 delivery-web/);
  assert.match(detail, /DEL-1/); // epic fact / related chip, never the name
  const markdown = entityMarkdown({ row });
  assert.match(markdown, /## DEL-197 #52 delivery-web/);
  assert.match(markdown, /- epic: DEL-1/);
});

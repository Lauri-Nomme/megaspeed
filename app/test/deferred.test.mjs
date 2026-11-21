/* Deferred QA-round-2 items (B6/B9–B21): the pure parts.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  deliveryMarkdown, deliveryTable, entityMarkdown, overviewMarkup, rowStateText,
} from '../tables.js';
import {
  evidenceValue, personSignalNote, rulesMarkdown, rulesStats,
} from '../rules.js';
import { fmtWipTick, wipTicks } from '../queues.js';

function row(overrides = {}) {
  return {
    object_id: 'PR:DEL/web#52',
    repo_short: 'web',
    number: 52,
    tickets: ['DEL-197'],
    ticket: 'DEL-197',
    author: 'AlNg',
    state: 'open',
    ball: 'review',
    ball_label: 'review',
    phase: 'review',
    waiting_since: '2026-11-13T12:07:00Z',
    waiting_hours: 200,
    age_hours: 400,
    blocked: false,
    dependents: 0,
    impact_hours: 0,
    commits: 2,
    ...overrides,
  };
}

function stream(id, diagnosis) {
  return {
    id,
    summary: {
      diagnosis,
      mean_wait_hours: 12,
    },
  };
}

test('B11: an empty Blocked tab says so instead of silently refusing the click', () => {
  const html = deliveryTable({ rows: [] }, 'blocked');
  assert.match(html, /Nothing blocked — good\./);
  const scoped = deliveryTable({ rows: [] }, 'blocked', { scope: { kind: 'person', value: 'BoFe', label: 'BoFe' } });
  assert.match(scoped, /Nothing blocked for scope BoFe — good\./);
});

test('B19: delivery Markdown exports the same state words as the table', () => {
  assert.equal(rowStateText(row({ waiting_hours: 200 })), 'waiting 1w+');
  assert.equal(rowStateText(row({ phase: 'active', waiting_hours: 2 })), 'in progress');
  assert.equal(rowStateText(row({ phase: 'review', waiting_hours: 2 })), 'in review');
  const markdown = deliveryMarkdown([row({ waiting_hours: 200 })], 'meta');
  // prd/25: the key is a link to the ticket detail page, not plain text
  assert.match(markdown, /\| waiting 1w\+ \| \[DEL-197\]\(#ticket\/DEL-197\) #52 web/);
  assert.doesNotMatch(markdown, /\| review \|/);
  const dependents = entityMarkdown({ row: row({ dependents: 1, impact_hours: 2 }) });
  assert.match(dependents, /over 1 dependent PR/);
});

test('B17: the overview labels people as people, not queues', () => {
  const delivery = { counts: { open: 3, blocked: 0, oldest_waiting: 1 }, rows: [] };
  const people = overviewMarkup({
    delivery,
    queues: { level: 'person', streams: [stream('AlNg', 'overloaded')] },
  });
  assert.match(people, /overloaded person/);
  assert.match(people, /People diagnosis/);
  const queues = overviewMarkup({
    delivery,
    queues: { level: 'queue', streams: [stream('review', 'overloaded'), stream('author', 'overloaded')] },
  });
  assert.match(queues, /overloaded queues/);
  assert.match(queues, /Queue diagnosis/);
});

test('B19: rule stats agree in number', () => {
  const one = rulesStats({ suggestions: [{ severity: 'warning', impact_hours: 1 }], sample: { prs: 1, days: 1 } });
  assert.match(one, /<b>1<\/b> rule</);
  assert.match(one, /<b>1<\/b> PR ·/);
  const two = rulesStats({ suggestions: [{ severity: 'warning', impact_hours: 1 }, { severity: 'info', impact_hours: 1 }], sample: { prs: 2, days: 2 } });
  assert.match(two, /<b>2<\/b> rules</);
});

test('B19: a fixed target takes no ≈, an estimate still does', () => {
  assert.equal(evidenceValue('target_hours', 72), '9 d 0 h');  // d = 8 business hours (prd/35)
  assert.doesNotMatch(evidenceValue('target_hours', 72), /≈/);
  assert.match(evidenceValue('hours_per_round', 59), /≈/);
});

test('flow efficiency and waiting shares are percentages, not fractions', () => {
  assert.equal(evidenceValue('aggregate_efficiency', 0.1879), '19%');
  assert.equal(evidenceValue('median_efficiency', 0.33), '33%');
  assert.equal(evidenceValue('waiting_share', 0.81), '81%');
  // a duration that happens to contain the word is still hours
  assert.match(evidenceValue('median_waiting_hours', 17.02), /≈/);
});

test('B16: the people level admits when it has no person signal', () => {
  const team = { level: 'person', suggestions: [{ scope: { kind: 'team' } }] };
  const note = personSignalNote(team);
  assert.match(note, /No person-level rule crosses its threshold/);
  assert.match(note, /the team tab names the team-level rules/);
  const empty = personSignalNote({ level: 'person', suggestions: [] }, { below: false });
  assert.equal(empty, 'No person-level rule crosses its threshold in this window.');
  assert.equal(personSignalNote({ level: 'team', suggestions: [] }), '');
  assert.equal(personSignalNote({ level: 'person', suggestions: [{ scope: { kind: 'person', person: 'AlNg' } }] }), '');
});

test('B16: the Markdown heading never says "(person)" over team rules', () => {
  const data = {
    level: 'person',
    sample: { prs: 1, days: 1 },
    hours_mode: 'wall-clock',
    as_of: '2026-11-15T00:00:00Z',
    suggestions: [{
      rank: 1,
      title: '1 PR merged with no approval',
      severity: 'critical',
      metric: 'conformance',
      confidence: 0.5,
      impact_hours: 10,
      suggestion: 'do the thing',
      evidence: { target_hours: 72 },
      scope: { kind: 'team' },
    }],
    note: '',
  };
  const markdown = rulesMarkdown(data);
  // prd/63 F28: the export follows the people toggle (no team rules under it)
  assert.match(markdown, /\(people — none\)/);
  assert.match(markdown, /1 team rule is on the team tab/);
  assert.doesNotMatch(markdown, /1 PR merged with no approval/);
  assert.match(markdown, /1 PR opened over 1 calendar day/);
  assert.doesNotMatch(markdown, /1 PRs/);
});

test('B15: the queue y scale is shared and labelled at 0, half and top', () => {
  assert.deepEqual(wipTicks(8), [0, 4, 8]);
  assert.deepEqual(wipTicks(1), [0, 0.5, 1]);
  assert.equal(fmtWipTick(4), '4');
  assert.equal(fmtWipTick(4.5), '4.5');
});

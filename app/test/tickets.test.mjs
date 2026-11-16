/* Unit tests for the ticket-key helpers (QA round 2, B2).
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  annotateRow, epicKeySet, epicTicket, keyListsOf, ownTicket, rowTicket, ticketFields,
} from '../tickets.js';

/** The `del` shape: every PR carries the shared DEL-1 epic plus its own key. */
function delLikePrs(count = 131) {
  return Array.from({ length: count }, (_, i) => [`DEL-1`, `DEL-${100 + i}`]);
}

test('a key shared by (nearly) every PR is an epic, a story key is not', () => {
  const epics = epicKeySet(delLikePrs(131));
  assert.deepEqual([...epics], ['DEL-1']);
});

test('ownTicket returns the PR key, not the shared epic key', () => {
  const epics = epicKeySet(delLikePrs(131));
  assert.equal(ownTicket(['DEL-1', 'DEL-167'], epics), 'DEL-167');
  assert.equal(epicTicket(['DEL-1', 'DEL-167'], epics), 'DEL-1');
});

test('a single-key PR keeps its key even when that key is an epic', () => {
  const epics = epicKeySet(delLikePrs(131));
  assert.equal(ownTicket(['DEL-1'], epics), 'DEL-1');
});

test('without a census the most specific key wins (never keys[0])', () => {
  assert.equal(ownTicket(['DEL-1', 'DEL-167'], null), 'DEL-167');
  assert.equal(ownTicket(['DEL-1', 'DEL-167'], new Set()), 'DEL-167');
  assert.equal(ownTicket([], null), '');
});

test('a small store is not classified as epic-heavy', () => {
  // Mirrors analysis/ballcourt.py: a corpus of three PRs cannot tell a shared
  // epic from a real shared ticket, so nothing is demoted.
  const tiny = epicKeySet([['DEL-1', 'DEL-2'], ['DEL-1', 'DEL-3'], ['DEL-1', 'DEL-4']]);
  assert.equal(tiny.size, 0);
  assert.equal(ownTicket(['DEL-1', 'DEL-2'], tiny), 'DEL-2');
  // One more PR is enough to classify the shared link.
  const small = epicKeySet([['DEL-1', 'DEL-2'], ['DEL-1', 'DEL-3'], ['DEL-1', 'DEL-4'], ['DEL-1', 'DEL-5']]);
  assert.deepEqual([...small], ['DEL-1']);
});

test('ticketFields / annotateRow label a delivery row', () => {
  const epics = epicKeySet(delLikePrs(7));
  const row = annotateRow({ tickets: ['DEL-1', 'DEL-167'] }, epics);
  assert.equal(row.ticket, 'DEL-167');
  assert.equal(row.epic, 'DEL-1');
  assert.deepEqual(ticketFields(['DEL-1', 'DEL-9'], epics), { ticket: 'DEL-9', epic: 'DEL-1' });
});

test('rowTicket tolerates a row that was never annotated', () => {
  assert.equal(rowTicket({ tickets: ['DEL-1', 'DEL-167'] }), 'DEL-167');
  assert.equal(rowTicket(null), '');
});

test('keyListsOf reads only PR objects', () => {
  const lists = keyListsOf([
    { object_type: 'PR', ticket_keys: ['DEL-1', 'DEL-7'] },
    { object_type: 'COMMIT', ticket_keys: ['DEL-1'] },
    { object_type: 'PR', ticket_keys: null },
  ]);
  assert.deepEqual(lists, [['DEL-1', 'DEL-7'], []]);
});

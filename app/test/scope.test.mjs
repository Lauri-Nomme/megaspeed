/* Unit tests for the scope honesty + queue-level helpers
 * (QA round 2, B3/B4/B5). Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  filteredPages, isRoleQueue, queueLevelForNode, scopeBanner, scopeForNode,
  scopeForStream, scopeMeaning, streamIsQueue, streamRoleLabel,
} from '../scope.js';

test('a stream named after a role is a queue, not a person named "author"', () => {
  assert.equal(isRoleQueue('author'), true);
  assert.equal(isRoleQueue('CI'), true);
  assert.equal(isRoleQueue('AlNg'), false);
  // The API reports `author` as role "person"; the id is what matters.
  assert.deepEqual(scopeForStream({ id: 'author', role: 'person' }), {
    kind: 'role', value: 'author', label: 'author',
  });
  assert.deepEqual(scopeForStream({ id: 'review', role: 'synthetic' }), {
    kind: 'role', value: 'review', label: 'review',
  });
  assert.deepEqual(scopeForStream({ id: 'AlNg', role: 'person' }), {
    kind: 'person', value: 'AlNg', label: 'AlNg',
  });
  assert.equal(scopeForStream(null), null);
});

test('streamIsQueue recognises synthetic and role streams', () => {
  assert.equal(streamIsQueue({ id: 'review', role: 'synthetic' }), true);
  assert.equal(streamIsQueue({ id: 'author', role: 'person' }), true);
  assert.equal(streamIsQueue({ id: 'BoFe', role: 'person' }), false);
});

test('a role queue is never labelled as a person', () => {
  assert.equal(streamRoleLabel({ id: 'author', role: 'person' }), 'role');
  assert.equal(streamRoleLabel({ id: 'CI', role: 'person' }), 'role');
  assert.equal(streamRoleLabel({ id: 'AlNg', role: 'person' }), 'person');
  assert.equal(streamRoleLabel({ id: 'review', role: 'synthetic' }), 'role');
});

test('a person node drills down to the person level, a role node to queues', () => {
  assert.equal(queueLevelForNode({ id: 'AlNg', kind: 'person' }), 'person');
  assert.equal(queueLevelForNode({ id: 'review', kind: 'synthetic' }), 'queue');
  assert.equal(queueLevelForNode({ id: 'author', kind: 'person' }), 'queue');
  assert.equal(queueLevelForNode(null), 'queue');
  assert.deepEqual(scopeForNode({ id: 'AlNg', kind: 'person' }), {
    kind: 'person', value: 'AlNg', label: 'AlNg',
  });
  assert.deepEqual(scopeForNode({ id: 'review', kind: 'synthetic' }), {
    kind: 'role', value: 'review', label: 'review',
  });
});

test('a person scope filters timeline, delivery, queue and overview — and the banner says so', () => {
  // prd/56 §4: the overview really does narrow under a person scope (its
  // attention list and diagnosis tile are filtered), so it is listed.
  assert.deepEqual([...filteredPages({ kind: 'person', value: 'AlNg' })].sort(),
    ['delivery', 'overview', 'queue', 'timeline']);
  const onQueue = scopeBanner({ kind: 'person', value: 'AlNg' }, 'queue');
  assert.match(onQueue, /this view \(queue view\) is filtered to it/);
  assert.match(onQueue, /filtered: timeline, delivery list, queue view and overview/);
  assert.match(onQueue, /not filtered: blocking map, rules and sources/);
  // the old lie: the banner claimed the queue series shows everything
  assert.doesNotMatch(onQueue, /not filtered: [^·]*queue/);
});

test('on a view that is not filtered the banner says that first', () => {
  const onBlocking = scopeBanner({ kind: 'person', value: 'AlNg' }, 'blocking');
  assert.match(onBlocking, /this view \(blocking map\) is not filtered/);
  assert.match(onBlocking, /filtered: timeline, delivery list, queue view and overview/);
  assert.match(onBlocking, /waits are measured over the whole log/);
});

test('a queue/role scope never claims the timeline is filtered', () => {
  const banner = scopeBanner({ kind: 'role', value: 'author' }, 'timeline');
  assert.match(banner, /this view \(timeline\) is not filtered/);
  assert.match(banner, /filtered: delivery list, queue view and overview/);
  // prd/34 §3: role:sentinel applies to the delivery list too, so a PR waiting
  // only on the sentinel shows up there as well as in the queue chart.
  assert.deepEqual([...filteredPages({ kind: 'role', value: 'sentinel' })].sort(),
    ['delivery', 'overview', 'queue']);
});

test('no scope means no banner', () => {
  assert.equal(scopeBanner({ kind: 'all', value: null }, 'queue'), '');
  assert.equal(scopeBanner(null, 'queue'), '');
});

test('prd/34 §3: a person scope has one stated meaning per view', () => {
  // Delivery/Queue mean "waiting on X"; the timeline means "events by X".
  assert.equal(scopeMeaning({ kind: 'person', value: 'FrMo' }, 'delivery'), 'waiting on FrMo');
  assert.equal(scopeMeaning({ kind: 'person', value: 'FrMo' }, 'queue'), 'waiting on FrMo');
  assert.equal(scopeMeaning({ kind: 'person', value: 'FrMo' }, 'timeline'), 'events by FrMo');
  const banner = scopeBanner({ kind: 'person', value: 'FrMo' }, 'delivery');
  assert.match(banner, /person FrMo \(waiting on FrMo\)/);
  const role = scopeBanner({ kind: 'role', value: 'sentinel' }, 'delivery');
  assert.match(role, /the sentinel queue/);
});

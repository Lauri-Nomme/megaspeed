/* Unit tests for the scope honesty + queue-level helpers
 * (QA round 2, B3/B4/B5). Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  filteredPages, isRoleQueue, queueLevelForNode, scopeBanner, scopeForNode,
  scopeForStream, streamIsQueue, streamRoleLabel,
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

test('a person scope filters timeline, delivery and queue — and the banner says so', () => {
  assert.deepEqual([...filteredPages({ kind: 'person', value: 'AlNg' })].sort(),
    ['delivery', 'queue', 'timeline']);
  const onQueue = scopeBanner({ kind: 'person', value: 'AlNg' }, 'queue');
  assert.match(onQueue, /this view \(queue view\) is filtered to it/);
  assert.match(onQueue, /filtered: timeline, delivery list and queue view/);
  assert.match(onQueue, /not filtered: overview, blocking map, rules and sources/);
  // the old lie: the banner claimed the queue series shows everything
  assert.doesNotMatch(onQueue, /not filtered: [^·]*queue/);
});

test('on a view that is not filtered the banner says that first', () => {
  const onBlocking = scopeBanner({ kind: 'person', value: 'AlNg' }, 'blocking');
  assert.match(onBlocking, /this view \(blocking map\) is not filtered/);
  assert.match(onBlocking, /filtered: timeline, delivery list and queue view/);
  assert.match(onBlocking, /waits are measured over the whole log/);
});

test('a queue/role scope never claims the timeline is filtered', () => {
  const banner = scopeBanner({ kind: 'role', value: 'author' }, 'timeline');
  assert.match(banner, /this view \(timeline\) is not filtered/);
  assert.match(banner, /filtered: delivery list and queue view/);
  assert.deepEqual([...filteredPages({ kind: 'role', value: 'sentinel' })], ['queue']);
});

test('no scope means no banner', () => {
  assert.equal(scopeBanner({ kind: 'all', value: null }, 'queue'), '');
  assert.equal(scopeBanner(null, 'queue'), '');
});

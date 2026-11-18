/* prd/40 — the client's port of the participant-status model, and the drawer
 * one-liner. The timeline used to duplicate the old single-ball machine (and
 * its copy had no roster at all), so it disagreed with the server. These tests
 * pin the transitions the rewrite changes: `ready`, debt that survives another
 * reviewer's approval, a push that keeps a standing approval, and a payload
 * that carries the server's own intervals.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { Timeline } from '../timeline.js';
import { matchesScope, reviewStateLine, rowStateText } from '../tables.js';

const HOUR = 3_600_000;
const T0 = Date.parse('2026-01-05T09:00:00Z');

function stubTimeline() {
  const ctx = {
    setTransform() {}, clearRect() {}, fillRect() {}, beginPath() {}, moveTo() {},
    lineTo() {}, stroke() {}, fill() {}, fillText() {}, save() {}, restore() {},
    setLineDash() {}, arc() {}, closePath() {}, rect() {}, clip() {},
    measureText: (t) => ({ width: String(t).length * 6.6 }),
  };
  const canvas = {
    width: 0, height: 0, style: {}, getContext: () => ctx,
    addEventListener() {}, setAttribute() {},
    classList: { add() {}, remove() {} },
    parentElement: { clientWidth: 1600, clientHeight: 900, addEventListener() {} },
  };
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  globalThis.requestAnimationFrame = () => 1;
  globalThis.window = { devicePixelRatio: 1, addEventListener() {}, innerWidth: 1600 };
  return new Timeline({ canvas, tip: { hidden: true } });
}

function pr(events) {
  return {
    id: 'PR:ENG/platform#1',
    number: 1,
    repo: 'platform',
    repoFull: 'ENG/platform',
    keys: ['ENG-1'],
    created: T0,
    closed: null,
    state: 'open',
    actor: 'AuAu',
    commits: [],
    events: events.map((event, index) => ({
      t: T0 + (event.at || 0) * HOUR,
      activity: event.activity,
      actor: event.actor,
      attributes: event.attributes || {},
      objects: (event.people || []).map((person) => `PERSON:${person}`),
      index,
    })),
  };
}

function segments(timeline, pull) {
  return timeline.lifecycle(pull).filter((seg) => seg.to > seg.from);
}

test('prd/40: every reviewer answered is a ready segment, not review', () => {
  const timeline = stubTimeline();
  const pull = pr([
    { activity: 'pr_opened', actor: 'AuAu', at: 0 },
    { activity: 'reviewer_changed', actor: 'AuAu', at: 0, people: ['RArA'], attributes: { change: 'added' } },
    { activity: 'pr_approved', actor: 'RArA', at: 4 },
  ]);
  const segs = segments(timeline, pull);
  assert.equal(segs.at(-1).ball, 'ready');
  assert.equal(segs.at(-1).ready, true);
  assert.deepEqual(segs.at(-1).holders, []);
});

test('prd/40: an approval does not cancel another reviewer\'s objection', () => {
  const timeline = stubTimeline();
  const pull = pr([
    { activity: 'pr_opened', actor: 'AuAu', at: 0 },
    { activity: 'reviewer_changed', actor: 'AuAu', at: 0, people: ['RArA', 'RBrB'], attributes: { change: 'added' } },
    { activity: 'pr_needs_work', actor: 'RBrB', at: 2 },
    { activity: 'pr_approved', actor: 'RArA', at: 4 },
    { activity: 'pr_pushed', actor: 'AuAu', at: 20, attributes: { push_class: 'content' } },
  ]);
  const segs = segments(timeline, pull);
  const afterApproval = segs.find((seg) => seg.from === T0 + 4 * HOUR);
  assert.equal(afterApproval.ball, 'author');       // the debt still stands
  assert.equal(afterApproval.phase, 'active');
  // RBrB answered with needs-work, so it is debt, not a pending reviewer
  assert.deepEqual(afterApproval.holders, []);
});

test('prd/40: a content push keeps a standing approval (no re-pend)', () => {
  const timeline = stubTimeline();
  const pull = pr([
    { activity: 'pr_opened', actor: 'AuAu', at: 0 },
    { activity: 'reviewer_changed', actor: 'AuAu', at: 0, people: ['RArA', 'RBrB'], attributes: { change: 'added' } },
    { activity: 'pr_approved', actor: 'RArA', at: 2 },
    { activity: 'pr_pushed', actor: 'AuAu', at: 10, attributes: { push_class: 'content' } },
  ]);
  const segs = segments(timeline, pull);
  const afterPush = segs.find((seg) => seg.from === T0 + 10 * HOUR);
  assert.equal(afterPush.ball, 'review');
  assert.deepEqual(afterPush.holders, ['RBrB']);    // RArA's approval stood
});

test('prd/40: a pure rebase that lost nothing changes nothing', () => {
  const timeline = stubTimeline();
  const pull = pr([
    { activity: 'pr_opened', actor: 'AuAu', at: 0 },
    { activity: 'reviewer_changed', actor: 'AuAu', at: 0, people: ['RArA'], attributes: { change: 'added' } },
    { activity: 'pr_approved', actor: 'RArA', at: 2 },
    { activity: 'pr_pushed', actor: 'AuAu', at: 10, attributes: { push_class: 'pure_rebase', approvals_lost: false } },
  ]);
  const segs = segments(timeline, pull);
  const afterPush = segs.find((seg) => seg.from === T0 + 10 * HOUR);
  assert.equal(afterPush.ball, 'ready');
  assert.equal(afterPush.ready, true);
});

test('prd/40: CI is a flag, so a pending reviewer keeps the review phase', () => {
  const timeline = stubTimeline();
  const pull = pr([
    { activity: 'pr_opened', actor: 'AuAu', at: 0 },
    { activity: 'reviewer_changed', actor: 'AuAu', at: 0, people: ['RArA'], attributes: { change: 'added' } },
    { activity: 'build_status', actor: 'CiCi', at: 1 },
    { activity: 'build_status', actor: 'CiCi', at: 2, attributes: { result: 'success' } },
    { activity: 'comment_added', actor: 'XxXx', at: 3, attributes: { severity: 'suggestion' } },
  ]);
  const segs = segments(timeline, pull);
  const running = segs.find((seg) => seg.from === T0 + 1 * HOUR);
  assert.equal(running.phase, 'review');
  assert.deepEqual(running.holders, ['RArA']);
  const cleared = segs.find((seg) => seg.from === T0 + 2 * HOUR);
  assert.equal(cleared.phase, 'review');
});

test('prd/40: the server\'s interval payload is drawn as-is', () => {
  const timeline = stubTimeline();
  const pull = pr([]);
  pull.intervals = [
    { from: '2026-01-05T09:00:00Z', to: '2026-01-05T11:00:00Z', phase: 'review', ball: 'review' },
    { from: '2026-01-05T11:00:00Z', to: '2026-01-05T12:00:00Z', phase: 'review', ball: 'ready', ready: true },
  ];
  const segs = timeline.lifecycle(pull);
  assert.equal(segs.length, 2);
  assert.equal(segs[1].ball, 'ready');
  assert.equal(segs[1].ready, true);
});

test('prd/40: the drawer line states rework, pending reviewers and ready', () => {
  assert.equal(
    reviewStateLine({
      author: 'AuAu', author_open: true, ball: 'author',
      reviewers_pending: 1, reviewers_total: 3, ready_since: null,
    }),
    'needs rework by AuAu · 1/3 reviewers pending',
  );
  const ready = reviewStateLine({
    author: 'AuAu', author_open: false, ball: 'ready',
    reviewers_pending: 0, reviewers_total: 2,
    ready_since: '2026-01-05T09:00:00Z',
  });
  assert.match(ready, /all reviewers answered/);
  assert.match(ready, /ready to merge since/);
  assert.equal(rowStateText({ ball: 'ready', blocked: false }), 'ready to merge');
});

test('prd/40: a ready row is the author\'s merge click, not a review wait', () => {
  const row = { ball: 'ready', author: 'AuAu', waiting_for: 'ready', phase: 'review' };
  assert.equal(matchesScope(row, { kind: 'role', value: 'author' }), true);
  assert.equal(matchesScope(row, { kind: 'role', value: 'ready' }), true);
  assert.equal(matchesScope(row, { kind: 'role', value: 'review' }), false);
  const review = { ball: 'review', author: 'AuAu', waiting_for: 'RBrB', phase: 'review' };
  assert.equal(matchesScope(review, { kind: 'role', value: 'review' }), true);
  assert.equal(matchesScope(review, { kind: 'role', value: 'author' }), false);
});

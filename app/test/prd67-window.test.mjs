/* prd/67 W-01 — the timeline's actor lanes are the people who acted in the
 * visible window, not everyone the store ever saw.
 *
 * The live symptom (a Jira-only corpus with 252 people) was hundreds of empty
 * lanes: `laneRows()` built rows from every loaded event while the window was
 * only the axis. Now the row list is windowed, the window is part of the cache
 * key (a pan/zoom/timeframe change rebuilds it), and the hidden count is
 * reported so the canvas can say "N people with no activity in this window
 * hidden" instead of pretending the roster is that small.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { Timeline, actorHiddenNote } from '../timeline.js';

const DAY = 86_400_000;
const T0 = Date.parse('2026-09-01T09:00:00Z');

/* ---- a stub canvas + scroll container (no browser needed) --------------- */

function stubContext() {
  const ctx = {
    calls: 0,
    setTransform() {}, clearRect() {}, fillRect() { ctx.calls += 1; },
    beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {},
    fillText() {}, save() {}, restore() {}, setLineDash() {}, arc() {},
    closePath() {}, rect() {}, clip() {},
    measureText: (t) => ({ width: String(t).length * 6.6 }),
  };
  return ctx;
}

function stubTimeline({ width = 1600, height = 900 } = {}) {
  const ctx = stubContext();
  const canvas = {
    width: 0,
    height: 0,
    style: {},
    getContext: () => ctx,
    addEventListener() {},
    setAttribute() {},
    classList: { add() {}, remove() {} },
    parentElement: { clientWidth: width, clientHeight: height, addEventListener() {} },
  };
  const scroller = { clientHeight: height, scrollTop: 0, addEventListener() {} };
  const spacer = { style: {} };
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  globalThis.requestAnimationFrame = () => 1;
  globalThis.window = { devicePixelRatio: 1, addEventListener() {}, innerWidth: width };
  const timeline = new Timeline({ canvas, tip: { hidden: true }, scroller, spacer });
  return { timeline, canvas, ctx };
}

/** Two actors, one on each side of the calendar: AaAa in September, BbBb in
 *  November. Nothing else — the row list is the whole subject here. */
function twoActors() {
  const events = [
    {
      key: 'e1', ts: new Date(T0).toISOString(), activity: 'pr_opened',
      actor: 'AaAa', repo: 'DEL/app', pr_id: 1, objects: [], attributes: {},
    },
    {
      key: 'e2', ts: new Date(T0 + 60 * DAY).toISOString(), activity: 'pr_opened',
      actor: 'BbBb', repo: 'DEL/app', pr_id: 2, objects: [], attributes: {},
    },
  ];
  return { events, objects: [] };
}

function loaded() {
  const stubs = stubTimeline();
  const { events, objects } = twoActors();
  stubs.timeline.setData(events, objects, { as_of: new Date(T0 + 61 * DAY).toISOString() });
  return stubs;
}

const at = (timeline, start, end) => {
  timeline.view = { start, end };
  return timeline.laneRows();
};

test('the actor lanes hold only the people who acted in the window', () => {
  const { timeline } = loaded();
  assert.equal(timeline.dim, 'actor');
  assert.equal(timeline.laneRows().length, 2);
  assert.equal(timeline.hiddenActors(), 0);

  const september = at(timeline, T0 - DAY, T0 + DAY);
  assert.deepEqual(september.map((row) => row.key), ['AaAa']);
  assert.equal(timeline.hiddenActors(), 1);
  // the row total is the actor's all-time event count, not the window's
  assert.equal(september[0].count, 1);

  // move the window: the lane set flips, it does not accumulate
  const november = at(timeline, T0 + 59 * DAY, T0 + 61 * DAY);
  assert.deepEqual(november.map((row) => row.key), ['BbBb']);
  assert.equal(timeline.hiddenActors(), 1);

  // both inside again: nothing is hidden and the note is silent
  const both = at(timeline, T0 - DAY, T0 + 61 * DAY);
  assert.deepEqual(both.map((row) => row.key).sort(), ['AaAa', 'BbBb']);
  assert.equal(timeline.hiddenActors(), 0);
});

test('a window with no activity hides everyone (and says so)', () => {
  const { timeline } = loaded();
  const empty = at(timeline, T0 + 20 * DAY, T0 + 22 * DAY);
  assert.deepEqual(empty, []);
  assert.equal(timeline.hiddenActors(), 2);
  assert.equal(actorHiddenNote(2), '2 people with no activity in this window hidden');
});

test('the note is silent at zero and honest at one', () => {
  assert.equal(actorHiddenNote(0), '');
  assert.equal(actorHiddenNote(undefined), '');
  assert.equal(actorHiddenNote(1), '1 person with no activity in this window hidden');
});

test('other dimensions are untouched by the actor window', () => {
  const { timeline } = loaded();
  timeline.dim = 'queue';
  assert.equal(timeline.hiddenActors(), 0);
  timeline.dim = 'object';
  // no PR objects in this fixture: the object dim is empty, and owns its list
  assert.deepEqual(timeline.laneRows(), []);
  assert.equal(timeline.hiddenActors(), 0);
});

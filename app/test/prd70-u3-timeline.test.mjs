/* prd/70 U3 — the timeline's clock and its labels (F-07, F-12).
 *
 * F-07: the queue lanes summed raw 24/7 wall-clock milliseconds and printed them
 * as `/h` (`review · 60.9k h`) under a `business hours · 1 d = 8 h` legend, so a
 * bare `h` meant two different things on one screen. The lanes now use the
 * team's business-hours clock — the same `calendar.business_hours(None, …)` the
 * analysis prices a team queue with — so one working day reads 8 h again.
 *
 * F-12: at the 5-year `All` zoom a sprint band too narrow for its own text drew a
 * clipped `S55…` and the next label (`S5800`) could start two pixels later: two
 * labels that read as one. Labels are never clipped now; they are shortened to
 * the longest form that fits, or skipped.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  Timeline, makeBusinessClock, parseWorkingWindow, placeSprintLabels,
} from '../timeline.js';

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
/* 2026-11-23 is a Monday; 2026-11-21/22 are the weekend before it. */
const MONDAY = Date.parse('2026-11-23T09:00:00Z');
const SATURDAY = Date.parse('2026-11-21T00:00:00Z');

/** The committed default calendar as `/api/health` briefs it. */
const BRIEF = {
  hours_mode: 'business',
  calendar_id: 'deadbeefcafe',
  timezone: 'UTC',
  working_hours: { default: '09:00-17:00' },
  weekdays: [1, 2, 3, 4, 5],
  holidays: 0,
};

/* ---- stubs (no browser) ------------------------------------------------- */

function recordingContext() {
  const texts = [];
  const ctx = {
    texts,
    font: '',
    fillStyle: '',
    textAlign: '',
    textBaseline: '',
    setTransform() {}, clearRect() {}, fillRect() {}, beginPath() {}, moveTo() {},
    lineTo() {}, stroke() {}, fill() {}, save() {}, restore() {}, setLineDash() {},
    arc() {}, closePath() {}, rect() {}, clip() {},
    measureText(text) { return { width: String(text).length * 6.6 }; },
    fillText(text, x, y) { texts.push({ text: String(text), x, y, width: String(text).length * 6.6 }); },
  };
  return ctx;
}

function stubTimeline() {
  const ctx = recordingContext();
  const canvas = {
    width: 0,
    height: 0,
    style: {},
    getContext: () => ctx,
    addEventListener() {},
    setAttribute() {},
    classList: { add() {}, remove() {} },
    parentElement: { clientWidth: 1600, clientHeight: 900, addEventListener() {} },
  };
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  globalThis.requestAnimationFrame = () => 1;
  globalThis.window = { devicePixelRatio: 1, addEventListener() {}, innerWidth: 1600 };
  return { timeline: new Timeline({ canvas, tip: { hidden: true } }), ctx };
}

/* ---- the working window ------------------------------------------------- */

test('prd/70 F-07: a calendar window parses in both printed forms, "off" is not 24/7', () => {
  assert.deepEqual(parseWorkingWindow('09:00–17:00'), [540, 1020]); // config en dash
  assert.deepEqual(parseWorkingWindow('09:00-17:00'), [540, 1020]); // /api/calendar
  assert.deepEqual(parseWorkingWindow(' 8:30 - 16:45 '), [510, 1005]);
  assert.equal(parseWorkingWindow('off'), null);
  assert.equal(parseWorkingWindow(''), null);
  assert.equal(parseWorkingWindow(null), null);
  assert.equal(parseWorkingWindow('17:00-09:00'), null); // reversed is a config error
});

test('prd/70 F-07: one working day of queue time is 8 h, a weekend is 0 h', () => {
  const clock = makeBusinessClock(BRIEF);
  assert.equal(clock.mode, 'business');
  // the acceptance: 24 h of review on a working day prices as one 8 h day
  assert.equal(clock.hours(MONDAY, MONDAY + DAY), 8);
  assert.equal(clock.hours(SATURDAY, SATURDAY + 2 * DAY), 0);
  // a full working week (Mon 00:00 → Mon 00:00) is five 8 h days, not 168 h
  assert.equal(clock.hours(Date.parse('2026-11-23T00:00:00Z'), Date.parse('2026-11-30T00:00:00Z')), 40);
  // a span inside the window is its own length
  assert.equal(clock.hours(MONDAY, MONDAY + 2 * HOUR), 2);
  // …and a span completely outside it is nothing
  assert.equal(clock.hours(MONDAY + 12 * HOUR, MONDAY + 15 * HOUR), 0);
});

test('prd/70 F-07: the window is local to the calendar timezone', () => {
  try {
    new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Tallinn' });
  } catch {
    return; // no timezone database in this runtime
  }
  const clock = makeBusinessClock({ ...BRIEF, timezone: 'Europe/Tallinn' });
  // 09:00 UTC is 11:00 in Tallinn (EET, winter), so the 09:00–17:00 window is
  // still owed its 8 h across any full local day — the UTC hours are not the
  // working hours.
  assert.equal(clock.hours(MONDAY, MONDAY + DAY), 8);
  // 05:00–07:00 UTC = 07:00–09:00 local: before the window, so zero
  assert.equal(clock.hours(MONDAY - 4 * HOUR, MONDAY - 2 * HOUR), 0);
  // 06:00–10:00 UTC = 08:00–12:00 local: three hours of it (09:00–12:00 local)
  assert.equal(clock.hours(MONDAY - 3 * HOUR, MONDAY + HOUR), 3);
});

test('prd/70 F-07: a calendar that never works prices zero, a wall-clock store prices elapsed', () => {
  const off = makeBusinessClock({ ...BRIEF, working_hours: { default: 'off' } });
  assert.equal(off.window, null);
  assert.equal(off.hours(MONDAY, MONDAY + DAY), 0);

  const wall = makeBusinessClock({ hours_mode: 'wall-clock', calendar_id: 'none' });
  assert.equal(wall.mode, 'wall-clock');
  assert.equal(wall.hours(MONDAY, MONDAY + DAY), 24);
  // no calendar at all is the same honest number, not a silent business guess
  assert.equal(makeBusinessClock(null).mode, 'wall-clock');
});

/* ---- the queue lanes on the canvas -------------------------------------- */

/** One PR opened on Monday 09:00 with a reviewer, answered exactly a day later. */
function oneDayReview(asOf = '2026-11-24T09:00:00Z') {
  const objects = [{
    object_id: 'PR:ENG/platform#1',
    object_type: 'PR',
    pr_id: 1,
    repo: 'ENG/platform',
    ticket_keys: ['ENG-1'],
    created_at: '2026-11-23T09:00:00Z',
    closed_at: null,
    state: 'open',
    actor: 'AuAu',
  }];
  const events = [
    {
      key: 'e1', ts: '2026-11-23T09:00:00Z', activity: 'pr_opened', actor: 'AuAu',
      repo: 'ENG/platform', pr_id: 1, objects: ['PR:ENG/platform#1'], attributes: {},
    },
    {
      key: 'e2', ts: '2026-11-23T09:00:00Z', activity: 'reviewer_changed', actor: 'AuAu',
      repo: 'ENG/platform', pr_id: 1, objects: ['PR:ENG/platform#1', 'PERSON:RArA'],
      attributes: { change: 'added' },
    },
    {
      key: 'e3', ts: '2026-11-24T09:00:00Z', activity: 'pr_approved', actor: 'RArA',
      repo: 'ENG/platform', pr_id: 1, objects: ['PR:ENG/platform#1'], attributes: {},
    },
  ];
  return { objects, events, meta: { as_of: asOf } };
}

test('prd/70 F-07: the review lane prices a day of review as 8 h, not 24 h', () => {
  const { timeline } = stubTimeline();
  const { objects, events, meta } = oneDayReview();
  timeline.setData(events, objects, { ...meta, calendar: BRIEF });
  timeline.setLanes('queue');
  const rows = timeline.laneRows();
  const review = rows.find((row) => row.key === 'review');
  assert.ok(review, 'the review queue has a lane');
  assert.equal(review.hours, 8, `review lane is ${review.hours} h`);
  assert.equal(review.label, 'review · 8 h');
  // the lane total and the legend now agree: 8 h is one business day
  assert.equal(review.count, 8);
});

test('prd/70 F-07: without a calendar the same lane honestly says 24 h', () => {
  const { timeline } = stubTimeline();
  const { objects, events, meta } = oneDayReview();
  timeline.setData(events, objects, { ...meta, calendar: { hours_mode: 'wall-clock' } });
  timeline.setLanes('queue');
  const review = timeline.laneRows().find((row) => row.key === 'review');
  assert.equal(review.hours, 24);
  assert.equal(review.label, 'review · 24 h');
  assert.equal(timeline.clock.mode, 'wall-clock');
});

test('prd/70 F-07: a two-week wait prices its ten working days, not 336 h', () => {
  const { timeline } = stubTimeline();
  const { objects, events } = oneDayReview('2026-12-07T09:00:00Z'); // two weeks on
  timeline.setData(events.slice(0, 2), objects, {
    as_of: '2026-12-07T09:00:00Z', calendar: BRIEF,
  });
  timeline.setLanes('queue');
  // Mon 23 Nov 09:00 → Mon 7 Dec 09:00 = ten working days = 80 business hours
  assert.equal(timeline.queueHours('review'), 80);
  assert.equal(timeline.queueHours('author'), 0);
});

test('prd/70 F-07: a queue whose time is all weekend keeps its lane at an honest 0 h', () => {
  const { timeline } = stubTimeline();
  const objects = [{
    object_id: 'PR:ENG/platform#2',
    object_type: 'PR',
    pr_id: 2,
    repo: 'ENG/platform',
    ticket_keys: ['ENG-2'],
    created_at: '2026-11-21T00:00:00Z', // Saturday
    closed_at: null,
    state: 'open',
    actor: 'AuAu',
  }];
  const event = (key, ts, activity, actor, attributes = {}, participants = []) => ({
    key, ts, activity, actor, repo: 'ENG/platform', pr_id: 2,
    objects: ['PR:ENG/platform#2', ...participants.map((p) => `PERSON:${p}`)], attributes,
  });
  const events = [
    event('f1', '2026-11-21T00:00:00Z', 'pr_opened', 'AuAu'),
    event('f2', '2026-11-21T00:00:00Z', 'reviewer_changed', 'AuAu', { change: 'added' }, ['RArA']),
    // answered before the window opens on Monday: the ball sat in the review
    // queue over the whole weekend and never during a working hour
    event('f3', '2026-11-23T00:00:00Z', 'pr_approved', 'RArA'),
  ];
  timeline.setData(events, objects, { as_of: '2026-11-23T00:00:00Z', calendar: BRIEF });
  timeline.setLanes('queue');
  const review = timeline.laneRows().find((row) => row.key === 'review');
  assert.ok(review, 'the lane still exists — the canvas draws its segments');
  assert.equal(review.hours, 0);
  assert.equal(review.label, 'review · 0 h');
});

/* ---- sprint band labels ------------------------------------------------- */

test('prd/70 F-12: a clipped label is never drawn, and labels never touch', () => {
  const measure = (text) => text.length * 6.6;
  const placed = placeSprintLabels([
    // the fixture of the finding: a band too narrow for its five-character id,
    // followed immediately by a label that fits
    { left: 1000, right: 1034, texts: ['S5512'] },
    { left: 1036, right: 1136, texts: ['S5800', 'S5800 · active'] },
  ], { measure });
  assert.equal(placed.length, 1, JSON.stringify(placed));
  assert.equal(placed[0].text, 'S5800');
  assert.equal(placed[0].x, 1042);
  // the drawn box is fully inside its own band
  assert.ok(placed[0].x >= 1036 && placed[0].right <= 1136);
});

test('prd/70 F-12: the longest form that fits is used, else the band is skipped', () => {
  const measure = (text) => text.length * 6.6;
  const wide = placeSprintLabels([
    { left: 200, right: 340, texts: ['S5800 · active · lens', 'S5800 · active', 'S5800'] },
  ], { measure });
  assert.equal(wide[0].text, 'S5800 · active');
  const narrow = placeSprintLabels([
    { left: 200, right: 260, texts: ['S5800 · active', 'S5800'] },
  ], { measure });
  assert.equal(narrow[0].text, 'S5800');
});

test('prd/70 F-12: a label that would come within the gap of the last one is skipped', () => {
  const measure = (text) => text.length * 6.6;
  const placed = placeSprintLabels([
    // two overlapping bands (the store's sprints overlap by a few days): the
    // first label is wide, so the second band's label would land on top of it
    { left: 0, right: 200, texts: ['S1111 · active'] },
    { left: 60, right: 300, texts: ['S2222'] },
    { left: 400, right: 520, texts: ['S3333'] },
  ], { measure });
  assert.deepEqual(placed.map((label) => label.text), ['S1111 · active', 'S3333']);
});

test('prd/70 F-12: placement is left-to-right whatever order the payload lists the bands in', () => {
  const measure = (text) => text.length * 6.6;
  const placed = placeSprintLabels([
    { left: 400, right: 520, texts: ['S3333'] },
    { left: 100, right: 220, texts: ['S1111'] },
    { left: 250, right: 370, texts: ['S2222'] },
  ], { measure });
  assert.deepEqual(placed.map((label) => label.text), ['S1111', 'S2222', 'S3333']);
  assert.deepEqual(placed.map((label) => label.x), [106, 256, 406]);
});

/* ---- the whole header row, drawn ---------------------------------------- */

/** The `All`-zoom shape of the finding: one early sprint in a narrow band, a
 *  handful of mid-store bands too narrow for any text, and the long recent one. */
function sprintPayload() {
  return {
    available: true,
    count: 4,
    scope: null,
    sprints: [
      { sprint_id: 5512, state: 'closed', band_start: '2021-09-28T00:00:00Z', band_end: '2021-11-10T00:00:00Z' },
      { sprint_id: 1900, state: 'closed', band_start: '2024-01-08T00:00:00Z', band_end: '2024-01-21T00:00:00Z' },
      { sprint_id: 5799, state: 'closed', band_start: '2026-08-17T00:00:00Z', band_end: '2026-08-30T00:00:00Z' },
      { sprint_id: 5800, state: 'active', band_start: '2026-09-01T00:00:00Z', band_end: '2026-11-21T00:00:00Z' },
    ],
    rows: [],
  };
}

test('prd/70 F-12: at `All` no sprint label is truncated and none overlaps another', () => {
  const { timeline, ctx } = stubTimeline();
  const objects = [];
  const events = [
    { key: 'a', ts: '2021-09-28T00:00:00Z', activity: 'pr_opened', actor: 'AaAa', repo: 'ENG/platform', pr_id: 1, objects: [], attributes: {} },
    { key: 'b', ts: '2026-11-21T00:00:00Z', activity: 'pr_merged', actor: 'AaAa', repo: 'ENG/platform', pr_id: 1, objects: [], attributes: {} },
  ];
  timeline.setData(events, objects, { as_of: '2026-11-21T00:00:00Z', calendar: BRIEF });
  timeline.setSprints(sprintPayload());
  timeline.setLanes('object');
  timeline.fit();
  ctx.texts.length = 0;
  timeline.render();

  const g = timeline.geom;
  const labels = ctx.texts.filter((entry) => /^S\d/.test(entry.text));
  assert.ok(labels.length >= 1, 'the wide active band keeps a label');
  for (const label of labels) {
    assert.ok(!label.text.includes('…'), `truncated sprint label ${label.text}`);
    assert.ok(label.x >= g.gutter, `${label.text} starts in the axis`);
    assert.ok(label.x + label.width <= g.gutter + g.plotW, `${label.text} runs off the plot`);
  }
  const boxes = [...labels].sort((a, b) => a.x - b.x);
  for (let i = 1; i < boxes.length; i += 1) {
    assert.ok(boxes[i].x >= boxes[i - 1].x + boxes[i - 1].width + 5,
      `${boxes[i - 1].text} touches ${boxes[i].text}`);
  }
  // no early `S55…` at this zoom: the band is narrower than the id
  assert.ok(!labels.some((label) => label.text.startsWith('S55')));
  assert.ok(labels.some((label) => label.text.startsWith('S5800')));
});

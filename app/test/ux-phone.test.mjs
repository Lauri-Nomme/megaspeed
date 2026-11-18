/* Phone UX regressions (prd/28): ticket-key labels, touch scroll vs pan, and
 * the no-overflow guards. Pure helpers plus a headless Timeline instance, so
 * the phone decisions are testable without a browser.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { touchAxis } from '../mobile.js';
import { Timeline, laneLabel, laneHeader } from '../timeline.js';
import { epicKeySet, keyListsOf } from '../tickets.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/* ---- touch: a vertical drag belongs to the page ------------------------ */

test('touchAxis: short moves are not a gesture, the dominant axis wins', () => {
  assert.equal(touchAxis(0, 0), null);
  assert.equal(touchAxis(3, 4), null); // 5px < 8px slop
  assert.equal(touchAxis(30, 5), 'x');
  assert.equal(touchAxis(-30, 5), 'x');
  assert.equal(touchAxis(5, 30), 'y');
  assert.equal(touchAxis(5, -30), 'y');
  // a perfect diagonal is a scroll, not a pan (the safe choice)
  assert.equal(touchAxis(20, 20), 'y');
  assert.equal(touchAxis(20, 20, 40), null);
});

/* ---- ticket-key labels: PR under story under epic ---------------------- */

function stubTimeline(width = 412) {
  const ctx = {
    setTransform() {}, clearRect() {}, fillRect() {}, beginPath() {}, moveTo() {},
    lineTo() {}, stroke() {}, fill() {}, fillText() {}, measureText: (t) => ({ width: String(t).length * 6.6 }),
    save() {}, restore() {}, setLineDash() {}, arc() {}, closePath() {}, quadraticCurveTo() {},
  };
  const canvas = {
    getContext: () => ctx,
    addEventListener() {},
    setAttribute() {},
    classList: { add() {}, remove() {} },
    style: {},
    parentElement: { clientWidth: width, addEventListener() {}, clientHeight: 600 },
  };
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  globalThis.requestAnimationFrame = () => 1;
  globalThis.window = { devicePixelRatio: 1, addEventListener() {}, innerWidth: 412 };
  return new Timeline({ canvas, tip: { hidden: true } });
}

const OBJECTS = [
  { object_type: 'PR', object_id: 'PR:DEL/delivery-api#1', pr_id: 1, repo: 'DEL/delivery-api', ticket_keys: ['DEL-1', 'DEL-100'], actor: 'AlNg', state: 'merged', created_at: '2026-08-10T09:00:00Z', closed_at: '2026-08-15T09:00:00Z' },
  { object_type: 'PR', object_id: 'PR:DEL/delivery-api#2', pr_id: 2, repo: 'DEL/delivery-api', ticket_keys: ['DEL-1', 'DEL-100'], actor: 'BiBi', state: 'merged', created_at: '2026-08-11T09:00:00Z', closed_at: '2026-08-16T09:00:00Z' },
  { object_type: 'PR', object_id: 'PR:DEL/delivery-web#3', pr_id: 3, repo: 'DEL/delivery-web', ticket_keys: ['DEL-1', 'DEL-101'], actor: 'CaOs', state: 'open', created_at: '2026-08-12T09:00:00Z' },
  { object_type: 'STORY', object_id: 'STORY:DEL-100', ticket_keys: ['DEL-100'], attributes: { epic_key: 'DEL-1' } },
  { object_type: 'STORY', object_id: 'STORY:DEL-101', ticket_keys: ['DEL-101'], attributes: { epic_key: 'DEL-1' } },
  // no PR names this story; its epic comes from the STORY object attribute
  { object_type: 'STORY', object_id: 'STORY:DEL-102', ticket_keys: ['DEL-102'], attributes: { epic_key: 'DEL-1' } },
];

const EVENTS = [
  { key: 'e1', ts: '2026-08-10T09:00:00Z', activity: 'pr_opened', actor: 'AlNg', repo: 'DEL/delivery-api', pr_id: 1, objects: ['PR:DEL/delivery-api#1'], attributes: {} },
  { key: 'e2', ts: '2026-08-12T09:00:00Z', activity: 'created', actor: 'DaKo', objects: ['STORY:DEL-102'], attributes: {} },
];

function loadedTimeline(width = 412) {
  const tl = stubTimeline(width);
  tl.setTicketIndex({ epicKeys: epicKeySet(keyListsOf(OBJECTS)) });
  tl.setData(EVENTS, OBJECTS, { as_of: '2026-11-17T00:00:00Z' });
  return tl;
}

test('the story dimension always shows the epic header, even when PRs nest', () => {
  const tl = loadedTimeline();
  const wide = { start: Date.parse('2026-08-19T00:00:00Z'), end: Date.parse('2026-11-17T00:00:00Z') };
  tl.view = { ...wide }; // 90 days: PRs collapsed
  let rows = tl.storyRows();
  assert.equal(rows[0].kind, 'epic');
  assert.equal(rows[0].ticket, 'DEL-1');
  assert.ok(rows.some((row) => row.kind === 'story' && row.story === 'DEL-102' && row.epic === 'DEL-1'));

  tl.view = { start: wide.end - 30 * 86400000, end: wide.end }; // 30 days: PRs nested
  rows = tl.storyRows();
  assert.equal(rows[0].kind, 'epic');
  const pr = rows.find((row) => row.kind === 'pr' && row.number === 1);
  assert.ok(pr, 'PR #1 is nested under its story');
  assert.equal(pr.depth, 2);
  assert.equal(laneLabel(pr, { narrow: true, dim: 'story' }), 'DEL-100 #1');
});

test('a desktop-width story lane is unchanged: epic header only on a wide window', () => {
  const tl = loadedTimeline(1200);
  const end = Date.parse('2026-11-17T00:00:00Z');
  tl.view = { start: end - 30 * 86400000, end }; // nested PRs
  let rows = tl.storyRows();
  assert.equal(rows.some((row) => row.kind === 'epic'), false);
  const pr = rows.find((row) => row.kind === 'pr' && row.number === 1);
  assert.equal(pr.depth, 1);
  assert.equal(pr.label, '#1 delivery-api');
  tl.view = { start: end - 90 * 86400000, end }; // wide
  rows = tl.storyRows();
  assert.equal(rows[0].kind, 'epic');
});

test('a phone lane label keeps both the ticket key and the PR number', () => {
  const tl = loadedTimeline();
  tl.view = { start: Date.parse('2026-10-18T00:00:00Z'), end: Date.parse('2026-11-17T00:00:00Z') };
  const rows = tl.storyRows();
  const story = rows.find((row) => row.kind === 'story' && row.story === 'DEL-100');
  const pr = rows.find((row) => row.kind === 'pr' && row.number === 2);
  assert.equal(laneLabel(story, { narrow: true, dim: 'story' }), '▸ DEL-100');
  assert.equal(laneLabel(pr, { narrow: true, dim: 'story' }), 'DEL-100 #2');
  // the object dimension keeps the key *and* the number too
  tl.setLanes('object');
  const objectRow = tl.laneRows().find((row) => row.pr && row.pr.number === 3);
  assert.equal(laneLabel(objectRow, { narrow: true, dim: 'object' }), 'DEL-101 #3');
});

test('the phone section header is short enough for the gutter', () => {
  const rows = [
    { kind: 'epic' }, { kind: 'story' }, { kind: 'story' }, { kind: 'pr' }, { kind: 'pr' },
  ];
  assert.equal(laneHeader('story', rows, { narrow: true }), 'STORIES 2');
  assert.equal(laneHeader('story', rows), 'STORIES · 2 stories · 2 PRs');
  assert.equal(laneHeader('object', rows, { narrow: true }), 'PRs 5');
  assert.equal(laneHeader('actor', rows, { narrow: true }), 'ACTORS 5');
});

/* ---- source guards ----------------------------------------------------- */

test('prd/28: vertical scroll is allowed on every canvas; pan is axis-locked', () => {
  const css = read('style.css');
  assert.match(css, /#timeline, #blocking, #queues \{ touch-action: pan-y; \}/);
  assert.doesNotMatch(css, /#timeline, #blocking, #queues \{ touch-action: none; \}/);
  for (const file of ['timeline.js', 'graph.js']) {
    const src = read(file);
    assert.match(src, /touchAxis\(/, `${file} does not disambiguate scroll from pan`);
    assert.match(src, /pointer\.axis === 'x'/, `${file} pans on a vertical drag`);
  }
  // the WIP chart has no pan; it must still let the page scroll
  assert.match(read('style.css'), /#timeline, #blocking, #queues \{ touch-action: pan-y; \}/);
});

test('prd/28: story labels come from the STORY epic_key and never clip the gutter', () => {
  const timeline = read('timeline.js');
  assert.match(timeline, /attributes\.epic_key/);
  assert.match(timeline, /this\.storyEpic/);
  // the phone header is the short form, fitted inside the gutter
  assert.match(timeline, /laneHeader\(this\.dim, rows, \{ narrow \}\)/);
  assert.match(timeline, /fit\(ctx, text, g\.gutter - x - 5\)/);
});

test('prd/28: no viewport-wide overflow is hidden to mask a clipping bug', () => {
  const css = read('style.css');
  assert.doesNotMatch(css, /html, body \{[^}]*overflow-x: hidden/);
  // intentional internal scroller only (the activity legend)
  assert.match(css, /\.legend \{[^}]*overflow-x: auto/s);
  // the sprint strip stacks its rows on a phone instead of hiding them right
  assert.match(css, /\.sprint-rows \{[^}]*flex-wrap: wrap/s);
  assert.match(css, /\.sprint-row \{[^}]*flex: 1 1 100%/s);
  // a phone tenant pill wraps its name instead of ellipsizing it
  assert.match(css, /\.topbar \.tenant-name \{[^}]*white-space: normal/s);
  // and the tables become cards rather than staying wider than the screen
  assert.match(css, /\.table, \.table tbody, \.table tr, \.table td/);
});

test('prd/28: a card value is never pinned to a desktop column width', () => {
  const css = read('style.css');
  const phone = css.slice(css.indexOf('@media (max-width: 640px)'));
  // `.panel .table .c-dur { width: 96px }` / `.table.compact .c-dur { width: 90px }`
  // outrank `.table .c-dur { width: auto }`; the phone block must beat them too,
  // or `9 h 11 min` wraps into three vertical fragments in the Overview cards.
  assert.match(phone, /\.panel \.table \.c-dur,[^{]*\.table\.compact \.c-dur \{\s*width: auto;/);
  assert.match(phone, /\.panel \.table td, \.panel \.table tr:first-child td \{ padding: 2px 0; \}/);
  assert.match(phone, /\.table\.compact td \{ height: auto; \}/);
});

test('prd/28: the `custom` timeframe chip takes no room in the top bar', () => {
  const css = read('style.css');
  const phone = css.slice(css.indexOf('@media (max-width: 640px)'));
  // in the flow it wrapped the context row and shoved the page down 50px
  // mid-pan; scroll anchoring then jumped the chart under the finger
  assert.match(phone, /\.ctx \.tf-custom \{[^}]*position: absolute/s);
  assert.match(phone, /\.ctx \{ min-width: 0; position: relative; \}/);
});

test('prd/28: ticket keys in cards are 44px targets', () => {
  const css = read('style.css');
  assert.match(css, /\.table td\.c-id a, \.drill td\.drill-item a \{[^}]*min-height: 44px/s);
});

test('prd/28: the WIP chart keeps its role text and fits the diagnosis pill on a phone', () => {
  const queues = read('queues.js');
  assert.match(queues, /pillW = phone \? Math\.max\(76,/);
  assert.match(queues, /const roleX = phone \?/);
  // the desktop layout is the one it always had
  assert.match(queues, /phone \? GUTTER - pillW - 8 : 194/);
});

test('a phone story lane is flagged by canvas width only', () => {
  assert.equal(loadedTimeline(412).isPhone(), true);
  assert.equal(loadedTimeline(640).isPhone(), true);
  assert.equal(loadedTimeline(641).isPhone(), false);
  assert.equal(loadedTimeline(1200).isPhone(), false);
});

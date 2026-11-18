/* prd/33 §3 — the timeline is virtualised, so a big store stays renderable.
 *
 * The canvas used to be as tall as the lane list: 5,240 PRs = 78,930 px and
 * 4,000 stories = 174,354 px, both past Chrome's 65,535 px canvas limit, where
 * the bitmap is not painted at all (alpha 0 at every pixel) and a single render
 * below the cliff already cost 138–460 ms. Now the canvas is one viewport tall
 * and only the rows intersecting it are drawn.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { Timeline } from '../timeline.js';
import { epicKeySet, keyListsOf } from '../tickets.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/** Chrome's per-side canvas limit: past it the bitmap silently paints nothing. */
const CHROME_CANVAS_MAX = 65_535;

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
  return { timeline, canvas, ctx, scroller, spacer };
}

/* ---- a synthetic store the size of the review's big40 ------------------- */

const DAY = 86_400_000;
const BASE = Date.parse('2026-08-01T00:00:00Z');

/** 5,240 PRs across 4,000 stories (1.3 PRs/story, like the real store), all of
 *  them under one tracking epic — the shape of `data/del-combined.duckdb`, where
 *  `DEL-1` is on every PR and is demoted out of the story label by the census. */
const EPIC = 'DEL-1';

function largeStore({ prs = 5_240, stories = 4_000 } = {}) {
  const objects = [];
  const events = [];
  for (let s = 0; s < stories; s += 1) {
    objects.push({
      object_type: 'STORY',
      object_id: `STORY:DEL-${s + 2}`,
      ticket_keys: [`DEL-${s + 2}`],
      attributes: { epic_key: EPIC },
    });
  }
  for (let i = 0; i < prs; i += 1) {
    const story = `DEL-${(i % stories) + 2}`;
    const epic = EPIC;
    const repo = `DEL/repo-${i % 7}`;
    const opened = BASE + Math.floor((i / prs) * 80) * DAY;
    const closed = opened + 2 * DAY;
    objects.push({
      object_type: 'PR',
      object_id: `PR:${repo}#${i + 1}`,
      pr_id: i + 1,
      repo,
      ticket_keys: [story, epic],
      actor: `U${i % 23}`,
      state: 'merged',
      created_at: new Date(opened).toISOString(),
      closed_at: new Date(closed).toISOString(),
    });
    events.push({
      key: `e${i}a`, ts: new Date(opened).toISOString(), activity: 'pr_opened',
      actor: `U${i % 23}`, repo, pr_id: i + 1,
      objects: [`PR:${repo}#${i + 1}`], attributes: {},
    });
    events.push({
      key: `e${i}b`, ts: new Date(closed).toISOString(), activity: 'pr_merged',
      actor: `U${(i + 5) % 23}`, repo, pr_id: i + 1,
      objects: [`PR:${repo}#${i + 1}`], attributes: {},
    });
  }
  return { objects, events };
}

function loaded(options = {}) {
  const stubs = stubTimeline(options);
  const { objects, events } = largeStore(options);
  stubs.timeline.setTicketIndex({ epicKeys: epicKeySet(keyListsOf(objects)) });
  stubs.timeline.setData(events, objects, { as_of: '2026-11-01T00:00:00Z' });
  stubs.timeline.view = { ...stubs.timeline.domain };
  return stubs;
}

/* ---- the canvas height is bounded, the lane list is not ----------------- */

test('prd/33 §3: 5,240 PRs no longer ask for a canvas past the 65,535 px limit', () => {
  const { timeline, canvas, spacer } = loaded({ width: 1600, height: 900 });
  timeline.setLanes('object'); // one lane per PR
  const g = timeline.layout();
  assert.equal(g.lanes.length, 5_240);
  // the list is still 78k px of lanes — that is what the *scroll* covers
  assert.ok(g.contentHeight > CHROME_CANVAS_MAX,
    `content height ${g.contentHeight} should exceed the canvas limit`);
  // …and the bitmap is one viewport, nowhere near the cliff
  assert.ok(g.height <= 900, `canvas is ${g.height}px tall, not the viewport`);
  assert.ok(canvas.height <= 900, `backing store ${canvas.height}px is not bounded`);
  assert.ok(canvas.height < CHROME_CANVAS_MAX);
  assert.equal(canvas.style.height, `${g.height}px`);
  // the spacer carries the scroll range instead
  assert.equal(Number.parseFloat(spacer.style.height), g.maxScroll);
  assert.ok(g.maxScroll > 70_000);
});

test('prd/33 §3: story mode (4,000 stories) is bounded too', () => {
  const { timeline, canvas } = loaded({ width: 1600, height: 900 });
  timeline.setLanes('story');
  const g = timeline.layout();
  assert.ok(g.lanes.length >= 4_000, `${g.lanes.length} story rows`);
  assert.ok(g.contentHeight > CHROME_CANVAS_MAX, `content ${g.contentHeight}`);
  assert.ok(canvas.height <= 900);
  assert.ok(g.height <= 900);
});

test('prd/33 §3: the rendered lane count is bounded by the viewport, not the store', () => {
  const { timeline } = loaded({ width: 1600, height: 900 });
  timeline.setDensity('M');
  timeline.setLanes('object');
  const g = timeline.layout();
  const drawn = g.window.lanes.last - g.window.lanes.first;
  const fits = Math.ceil((g.height - g.laneTop) / g.laneH) + 2 + 2; // + overscan
  assert.ok(drawn <= fits, `${drawn} rows drawn for a ${fits}-row viewport`);
  assert.ok(drawn < 60, `${drawn} rows drawn`);
  assert.equal(g.window.lanes.first, 0);
  assert.ok(g.window.lanes.last < g.lanes.length);
});

test('prd/33 §3: scrolling maps the window onto the lane list, top and bottom', () => {
  const { timeline, scroller } = loaded({ width: 1600, height: 900 });
  timeline.setDensity('M');
  timeline.setLanes('object');
  const first = timeline.layout();

  scroller.scrollTop = first.maxScroll;
  const last = timeline.layout();
  assert.equal(last.scrollTop, first.maxScroll);
  // the last row of the list is inside the window at the bottom of the scroll
  assert.ok(last.window.lanes.last >= last.lanes.length - 2,
    `bottom window ends at ${last.window.lanes.last} of ${last.lanes.length}`);
  assert.ok(last.window.lanes.first > 1_000, `top row ${last.window.lanes.first}`);
  const drawn = last.window.lanes.last - last.window.lanes.first;
  assert.ok(drawn < 60, `${drawn} rows drawn at the bottom`);

  // a scroll past the end is clamped, never negative and never beyond content
  scroller.scrollTop = 10_000_000;
  const clamped = timeline.layout();
  assert.equal(clamped.scrollTop, first.maxScroll);
});

test('prd/33 §3: a render at 5,240 PRs draws only the visible rows', () => {
  const { timeline, ctx } = loaded({ width: 1600, height: 900 });
  timeline.setDensity('M');
  timeline.setLanes('object');
  ctx.calls = 0;
  timeline.render();
  const g = timeline.geom;
  assert.ok(g.lanes.length === 5_240);
  // 5,240 lanes × (band + separator + label) used to be ~15,000 primitives
  assert.ok(ctx.calls < 500, `${ctx.calls} draw calls for one frame`);
  // every hit is inside the canvas and below the pinned axis/histogram
  for (const hit of timeline.hits) {
    if (hit.dead) continue;
    if (hit.kind === 'histogram' || hit.kind === 'sprint') continue;
    if (hit.y0 !== undefined) {
      assert.ok(hit.y0 >= g.laneTop - 0.001, `hit ${hit.kind} starts at ${hit.y0}`);
      assert.ok(hit.y1 <= g.height + 0.001, `hit ${hit.kind} ends at ${hit.y1}`);
    }
  }
  assert.ok(timeline.hits.length > 0, 'nothing was drawn');
});

test('prd/33 §3: a row scrolled under the header cannot answer a hit there', () => {
  const { timeline, scroller } = loaded({ width: 1600, height: 900 });
  timeline.setDensity('M');
  timeline.setLanes('object');
  scroller.scrollTop = 300; // rows slide under the pinned axis
  timeline.render();
  const g = timeline.geom;
  assert.ok(g.scrollTop > 0);
  // the gutter of the header strip belongs to no lane
  const header = timeline.hitTest(10, g.axisH + 4);
  assert.equal(header, null, `header hit was ${header && header.kind}`);
  // …while the same column just below the header is the first visible label
  const row = timeline.hitTest(10, g.laneTop + 4);
  assert.ok(row && row.kind === 'label', `row hit was ${row && row.kind}`);
});

test('prd/33 §3: a phone viewport (412px) is bounded and keeps the narrow gutter', () => {
  const { timeline, canvas } = loaded({ width: 412, height: 915 });
  timeline.setDensity('M');
  timeline.setLanes('object');
  const g = timeline.layout();
  assert.equal(timeline.isPhone(), true);
  assert.equal(g.gutter, 112);
  assert.ok(g.height <= 915);
  assert.ok(canvas.height <= 915);
  assert.ok(g.window.lanes.last - g.window.lanes.first < 80);
});

/* ---- the wiring, and the phone scroll contract -------------------------- */

test('prd/33 §3: the page has the scroll container and the spacer', () => {
  const html = read('index.html');
  assert.match(html, /id="timeline-scroll"/);
  assert.match(html, /id="lane-spacer"/);
  // the canvas and the spacer live inside the scroller, in that order
  assert.match(html, /id="timeline-scroll">\s*<canvas id="timeline"[\s\S]*?id="lane-spacer"/);
  const main = read('main.js');
  assert.match(main, /scroller: \$\('timeline-scroll'\)/);
  assert.match(main, /spacer: \$\('lane-spacer'\)/);
});

test('prd/33 §3: the stage is one viewport tall and the canvas is pinned', () => {
  const css = read('style.css');
  assert.match(css, /\.timeline-stage \{ height: clamp\(/);
  assert.match(css, /\.timeline-scroll \{[^}]*overflow-y: auto/s);
  assert.match(css, /#timeline \{ position: sticky; top: 0;/);
});

test('prd/28 is not regressed: a vertical drag still scrolls, it never pans', () => {
  const css = read('style.css');
  // the canvas stays a pan-y gesture surface (no touch-action: none)
  assert.match(css, /#timeline, #blocking, #queues \{ touch-action: pan-y; \}/);
  assert.doesNotMatch(css, /#timeline[^{}]*\{[^}]*touch-action: none/s);
  const src = read('timeline.js');
  assert.match(src, /touchAxis\(/);
  assert.match(src, /pointer\.axis === 'x'/);
  // and the lane scroller keeps the page's own chaining (no scroll trap)
  const scroller = css.slice(css.indexOf('.timeline-scroll {'), css.indexOf('.timeline-scroll {') + 400);
  assert.doesNotMatch(scroller, /overscroll-behavior: contain/);
});

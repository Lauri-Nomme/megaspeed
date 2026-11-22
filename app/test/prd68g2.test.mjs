/* prd/68 group G2 — sprints, timeline and trend UI regressions.
 *
 * F-02: a dateless event-only sprint says "dates unknown"/"unknown" and still
 *       filters the story lanes when focused; a future sprint is not a band and
 *       not an in-progress bucket.
 * F-11: an absent PR layer is unavailable, not "13/13 measured · 0 latest";
 *       cumulative flow names the story population it actually draws.
 * F-14: the aging stage/age/state columns fit 3-digit ages and long pills.
 * F-15: colliding canvas sprint labels are skipped, and the phone scope line
 *       can wrap inside the 412 px row.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { sprintStateLabel, sprintStrip } from '../sprints.js';
import { Timeline } from '../timeline.js';
import { cumulativeFlowLegend, trendChart, trendMarkup, trendStats } from '../trend.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const css = () => read('style.css');

const DAY = 86_400_000;
const BASE = Date.parse('2026-09-01T00:00:00Z');

function stubContext() {
  const ctx = {
    calls: 0,
    labels: [],
    setTransform() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
    stroke() {}, fill() {}, save() {}, restore() {}, setLineDash() {}, arc() {},
    closePath() {}, rect() {}, clip() {},
    fillRect() { ctx.calls += 1; },
    fillText(text, x, y) { ctx.calls += 1; ctx.labels.push({ text, x, y }); },
    measureText: (text) => ({ width: String(text).length * 6.6 }),
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
  return { timeline, ctx };
}

function sprintPayload(sprints) {
  return {
    available: true,
    count: sprints.length,
    sprints,
    scope: {},
    carry_over: { count: 0, rows: [] },
    rows: [],
  };
}

function sprintRow(overrides = {}) {
  return {
    sprint_id: 1,
    state: 'closed',
    open: false,
    closed: true,
    band_start: '2026-09-01T00:00:00Z',
    band_end: '2026-09-14T16:00:00Z',
    story_count: 10,
    points: 30,
    stories: ['DEMO-1'],
    ...overrides,
  };
}

/* ---------------------------------------------------------------- F-02 UI */

test('prd/68 F-02: a dateless sprint says "dates unknown", never NaN undefined', () => {
  const html = sprintStrip(sprintPayload([
    sprintRow({
      sprint_id: 6596, state: null, open: false, closed: false,
      band_start: null, band_end: null, story_count: 1, stories: ['DEMO-1'],
    }),
  ]));
  assert.match(html, /dates unknown/);
  assert.match(html, /<span class="sprint-state">unknown<\/span>/);
  assert.doesNotMatch(html, /NaN/);
  assert.doesNotMatch(html, /undefined →/);
  // the row still carries a real id and empty (not invented) band attributes
  assert.match(html, /data-sprint-id="6596"/);
  assert.match(html, /data-sprint-start=""/);
  assert.match(html, /data-sprint-end=""/);
});

test('prd/68 F-02: sprintStateLabel only says closed when the store says so', () => {
  assert.equal(sprintStateLabel({ state: null, open: false, closed: false }), 'unknown');
  assert.equal(sprintStateLabel({ state: null, open: false, closed: true }), 'closed');
  assert.equal(sprintStateLabel({ state: 'future', open: false, closed: false }), 'future');
  assert.equal(sprintStateLabel({ state: null, open: true, closed: false }), 'active');
  assert.equal(sprintStateLabel({ state: 'closed', open: false, closed: true }), 'closed');
});

test('prd/68 F-02: focusing a dateless sprint still filters the story lanes', () => {
  const { timeline } = stubTimeline();
  timeline.setData([
    { ts: '2026-09-01T10:00:00Z', activity: 'created', objects: ['STORY:DEMO-1'] },
    { ts: '2026-09-02T10:00:00Z', activity: 'created', objects: ['STORY:DEMO-2'] },
  ], []);
  timeline.setSprints(sprintPayload([
    sprintRow({
      sprint_id: 6596, state: null, open: false, closed: false,
      band_start: null, band_end: null, story_count: 1, stories: ['DEMO-1'],
    }),
  ]));
  assert.ok(timeline.sprints.some((sprint) => String(sprint.id) === '6596'));
  timeline.setSprintFocus(6596);
  assert.deepEqual([...timeline.sprintFocusKeys()], ['DEMO-1']);
  const stories = timeline.storyRows()
    .filter((row) => row.kind === 'story')
    .map((row) => row.story);
  assert.deepEqual(stories, ['DEMO-1']);
});

test('prd/68 F-02: a future sprint is not an active band', () => {
  const { timeline } = stubTimeline();
  timeline.setSprints(sprintPayload([
    sprintRow({
      sprint_id: 7564, state: 'future', open: false, closed: false,
      band_start: null, band_end: null, story_count: 0, stories: [],
    }),
  ]));
  const [future] = timeline.sprints;
  assert.equal(future.active, false);
  assert.equal(future.hasBand, false);
});

/* ---------------------------------------------------------------- F-15 */

test('prd/68 F-15: overlapping sprint labels are skipped, never overdrawn', () => {
  const { timeline, ctx } = stubTimeline();
  timeline.setSprints(sprintPayload([
    sprintRow({
      sprint_id: 15621, band_start: new Date(BASE + 2 * DAY).toISOString(),
      band_end: new Date(BASE + 20 * DAY).toISOString(),
    }),
    sprintRow({
      sprint_id: 16256, band_start: new Date(BASE + 4 * DAY).toISOString(),
      band_end: new Date(BASE + 22 * DAY).toISOString(),
    }),
    sprintRow({
      sprint_id: 16000, band_start: new Date(BASE + 40 * DAY).toISOString(),
      band_end: new Date(BASE + 50 * DAY).toISOString(),
    }),
  ]));
  timeline.view = { start: BASE, end: BASE + 60 * DAY };
  timeline.geom = {
    gutter: 100, plotW: 1000, width: 1100, label: 11,
    laneTop: 120, height: 600,
  };
  timeline.drawSprintBands(ctx, { ...timeline.geom, label: 11, plotW: 1000 });
  const labels = ctx.labels.filter((entry) => /^S\d/.test(entry.text));
  assert.ok(labels.length >= 2, 'the non-overlapping labels are still drawn');
  assert.ok(labels.length < 3, 'the colliding label is skipped');
  for (let i = 0; i < labels.length; i += 1) {
    for (let j = i + 1; j < labels.length; j += 1) {
      const a = labels[i];
      const b = labels[j];
      const aRight = a.x + ctx.measureText(a.text).width;
      const bRight = b.x + ctx.measureText(b.text).width;
      const overlap = a.x < bRight && b.x < aRight;
      assert.equal(overlap, false, `${a.text} must not overlap ${b.text}`);
    }
  }
});

test('prd/68 F-15: the phone scope count may wrap inside the sprint row', () => {
  const source = css();
  assert.match(
    source,
    /\.sprint-row \.sprint-scope-count\s*\{[^}]*white-space:\s*normal[^}]*overflow-wrap:\s*anywhere/s,
  );
  // 412 px viewport − 2×14 strip padding − 2×9 row padding − 2 px border
  const inner = 412 - 2 * 14 - 2 * 9 - 2;
  const longest = '70 stories · 107 pts · 31 committed at start · 39 adopted';
  assert.ok(longest.length * 6.36 <= inner + 4,
    'the longest scope line fits the row before wrapping');
});

/* ---------------------------------------------------------------- F-14 */

test('prd/68 F-14: aging stage, age and state columns fit their worst cells', () => {
  const source = css();
  const widthOf = (selector) => {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const rule = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`).exec(source);
    assert.ok(rule, `${selector} rule`);
    return Number(/width:\s*(\d+)px/.exec(rule[1])[1]);
  };
  const stage = widthOf('.aging .c-stage');
  const age = widthOf('.aging .c-age');
  const live = widthOf('.aging .c-live');
  const kind = widthOf('.aging .c-kind');
  const id = widthOf('.aging .c-id');
  const mult = widthOf('.aging .c-mult');
  const holder = widthOf('.aging .c-holder');
  const band = widthOf('.aging .c-band');
  // Chromium monospace at 12 px is ≈ 6.36–6.6 px per glyph; a fixed-layout
  // cell's width includes its 2×8 px padding.
  const content = (width) => width - 16;
  assert.ok(content(stage) >= 'in verification'.length * 6.6,
    `stage ${stage} fits "in verification"`);
  assert.ok(content(age) >= '420 d 6 h 610 calendar days'.length * 6.36,
    `age ${age} fits a 3-digit business-hours + calendar-days pair`);
  assert.ok(content(live) >= 'unobserved'.length * 6.6,
    `state ${live} fits "unobserved"`);
  assert.ok(content(live) >= 'very old'.length * 6.6,
    `state ${live} fits "very old"`);
  // the table still fits the 1600×1000 delivery panel (clientWidth 1048) with
  // no horizontal scrollbar
  const table = band + kind + id + stage + age + mult + holder + live;
  assert.ok(table <= 1048, `aging columns total ${table} px`);
});

/* ---------------------------------------------------------------- F-11 UI */

const unavailable = {
  metric: 'merged', label: 'Merged PRs', unit: 'count', bucket: 'week',
  available: false, unavailable_reason: 'no PR objects in this store',
  points: Array.from({ length: 13 }, (_, index) => ({
    label: `w${index}`, start: `2026-08-0${(index % 9) + 1}T00:00:00Z`,
    bucket_start: `2026-08-0${(index % 9) + 1}T00:00:00Z`,
    in_progress: index === 12, available: false, value: null, n: 0,
  })),
  summary: {
    latest: null, measured_buckets: 0, buckets: 13,
    unavailable_reason: 'no PR objects in this store', in_progress: [],
  },
};

test('prd/68 F-11: a PR-only tab says the layer is absent, not 0 latest', () => {
  const stats = trendStats(unavailable);
  assert.match(stats, /unavailable · no PR objects in this store/);
  assert.match(stats, /0<\/b>\/13 week buckets measured/);
  assert.doesNotMatch(stats, /0<\/b> latest/);
  assert.doesNotMatch(stats, /13\/13/);
  assert.match(trendChart(unavailable), /unavailable: no PR objects in this store/);
  assert.match(trendMarkup(unavailable), /data-trend-unavailable="true"/);
});

test('prd/68 F-11: cumulative flow on a Jira-only store names the story bands', () => {
  const bands = [
    { id: 'created', label: 'created', value: 2320 },
    { id: 'started', label: 'started', value: 1900 },
    { id: 'in_review', label: 'in review', value: 260 },
    { id: 'done', label: 'done', value: 1865 },
  ];
  const flow = {
    metric: 'cumulative_flow', label: 'Cumulative flow', unit: 'count',
    bucket: 'week', available: true,
    points: [
      {
        label: 'w1', start: '2026-08-03T00:00:00Z',
        bucket_start: '2026-08-03T00:00:00Z', end: '2026-08-10T00:00:00Z',
        in_progress: false, available: true, value: 2320, n: 2320,
        population: 'story', bands,
      },
      {
        label: 'w2', start: '2026-08-10T00:00:00Z',
        bucket_start: '2026-08-10T00:00:00Z', end: '2026-08-17T00:00:00Z',
        in_progress: false, available: true, value: 2320, n: 2320,
        population: 'story', bands,
      },
    ],
    summary: { latest: 2320, measured_buckets: 2, buckets: 2, in_progress: [] },
  };
  const legend = cumulativeFlowLegend(flow);
  assert.match(legend, /cumulative stories by stage/);
  assert.match(legend, /data-cfd-population="story"/);
  assert.match(legend, /data-band="done"/);
  assert.doesNotMatch(legend, /cumulative PRs by stage/);
  assert.match(trendChart(flow), /data-band="done"/);
});

test('prd/68 F-02: a sprint bucket with no in-progress sprint says so', () => {
  const payload = {
    metric: 'on_time', label: 'Sprint story delivery share', unit: 'ratio',
    bucket: 'sprint',
    points: [{ label: 'S1', start: '2026-01-05T00:00:00Z', value: 0.5, n: 1,
      in_progress: false, available: true }],
    summary: { latest: 0.5, latest_label: 'S1', latest_n: 1,
      measured_buckets: 1, buckets: 1, in_progress: [], min_n: 0,
      min_n_basis: 'n' },
  };
  const stats = trendStats(payload);
  assert.match(stats, /no sprint in progress/);
  assert.doesNotMatch(stats, /S7564/);
});

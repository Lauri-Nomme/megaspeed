/* prd/70 U7 — one sprint label on every surface (F-16).
 *
 * The store keeps no sprint **name** text (prd/02), only `sprint_id`
 * (`SPRINT:DEMO#4392`). The team's Jira names are `Sprint 352`-style, so the
 * reducer parses the number into `sprint_no`; every surface must print `S352`
 * when it has one and fall back to `S4392` when it does not — never both, never
 * a different label on the strip, the canvas, the risk panel or the lens line.
 *
 * main.js is the wired page, not an importable module: its label decisions are
 * read from its source (same pattern as the prd/68/70 guards). The pure
 * formatters are imported for real.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { sprintLabel } from '../format.js';
import { sprintBands, sprintScopeLine, sprintStrip } from '../sprints.js';
import { Timeline } from '../timeline.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const MAIN = read('main.js');
const SPRINTS = read('sprints.js');
const TIMELINE = read('timeline.js');

const DAY = 86_400_000;
const START = Date.parse('2026-09-01T00:00:00Z');

/* ------------------------------------------------------------ the formatter */

test('prd/70 F-16: sprintLabel prints the parsed number, else the id', () => {
  assert.equal(sprintLabel({ sprint_id: 4392, sprint_no: 352 }), 'S352');
  assert.equal(sprintLabel({ sprint_id: 4392, sprint_no: 0 }), 'S0');
  // a store scraped before the attribute existed still gets an honest label
  assert.equal(sprintLabel({ sprint_id: 4392 }), 'S4392');
  assert.equal(sprintLabel({ sprint_id: 4392, sprint_no: null }), 'S4392');
  // a bare id (a carry-over path, a route parameter) reads the same way
  assert.equal(sprintLabel(4392), 'S4392');
  assert.equal(sprintLabel(null), '');
  // an unparseable value is ignored, never printed
  assert.equal(sprintLabel({ sprint_id: 4392, sprint_no: 'Sprint 352' }), 'S4392');
});

/* ------------------------------------------------------------------ payload */

function sprintRow(overrides = {}) {
  return {
    sprint_id: 4392,
    sprint_no: 352,
    state: 'closed',
    open: false,
    closed: true,
    band_start: new Date(START).toISOString(),
    band_end: new Date(START + 14 * DAY).toISOString(),
    planned_end: '2026-09-15',
    story_count: 10,
    points: 30,
    done_by_end: 8,
    done_late: 1,
    never_done: 1,
    stories: ['DEMO-1'],
    carried_in: [],
    carried_out: [],
    ...overrides,
  };
}

function sprintPayload(rows, extra = {}) {
  return {
    available: true,
    count: rows.length,
    sprints: rows,
    active: null,
    scope: { stories: 10, in_sprint: 10, out_of_sprint: 0, unplanned: [] },
    carry_over: { count: 0, rows: [] },
    rows: [],
    ...extra,
  };
}

test('prd/70 F-16: the band, the strip and the scope line agree', () => {
  const data = sprintPayload([sprintRow()], { active: 4392 });
  assert.equal(sprintBands(data)[0].label, 'S352');
  const strip = sprintStrip(data);
  assert.match(strip, /<span class="sprint-id mono">S352<\/span>/);
  assert.doesNotMatch(strip, /S4392/);
  assert.match(sprintScopeLine(data), /active S352/);
  // the data attribute stays the numeric id — behavior, not a label
  assert.match(strip, /data-sprint-id="4392"/);
});

test('prd/70 F-16: a legacy sprint without a number falls back to its id', () => {
  const data = sprintPayload(
    [sprintRow({ sprint_id: 18568, sprint_no: null })],
    { active: 18568 },
  );
  assert.equal(sprintBands(data)[0].label, 'S18568');
  const strip = sprintStrip(data);
  assert.match(strip, /<span class="sprint-id mono">S18568<\/span>/);
  assert.doesNotMatch(strip, /S352/);
  assert.match(sprintScopeLine(data), /active S18568/);
});

test('prd/70 F-16: the risk panel and the lens line use the same label', () => {
  const data = sprintPayload([sprintRow({ open: true, closed: false, state: 'active' })], {
    active: 4392,
    at_risk: {
      available: true, sprint_id: 4392, sprint_no: 352,
      stories: 10, done_by_end: 8, points: 30, count: 0,
      points_at_risk: 0, rows: [],
    },
  });
  const focused = sprintStrip(data, { focus: 4392 });
  assert.match(focused, /<h2>S352 at risk<\/h2>/);
  assert.match(focused, /<b>S352 lens<\/b>/);
  assert.doesNotMatch(focused, /S4392/);
  // the button still opens the forecast by id; only the text is a label
  assert.match(focused, /data-forecast-open="4392"/);
});

test('prd/70 F-16: a carry-over path is labels, not raw ids', () => {
  const data = sprintPayload(
    [sprintRow(), sprintRow({ sprint_id: 4395, sprint_no: 353, band_start: new Date(START + 14 * DAY).toISOString(), band_end: new Date(START + 28 * DAY).toISOString() })],
    {
      carry_over: {
        count: 1,
        late: 1,
        rows: [{
          key: 'DEMO-1', sprints: [4392, 4395], missed_sprints: [4392],
          destination_sprint: 4395, reappears: true, status: 'late',
          points: 3, added: null, done_at: null, wait_hours: 4,
        }],
      },
    },
  );
  const strip = sprintStrip(data);
  assert.match(strip, /S352 → S353/);
  // the destination column says the same sprint the path ends on
  assert.match(strip, /<td data-label="next">S353<\/td>/);
});

/* ------------------------------------------------------------- the timeline */

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

const EVENTS = [
  { key: 'a', ts: new Date(START + DAY).toISOString(), activity: 'created', actor: 'AaAa', objects: ['STORY:DEMO-1'], attributes: {} },
  { key: 'b', ts: new Date(START + 10 * DAY).toISOString(), activity: 'status_changed', actor: 'AaAa', objects: ['STORY:DEMO-1'], attributes: { stage: 'in_review' } },
];

test('prd/70 F-16: the canvas band is labelled S352, not the id', () => {
  const { timeline, ctx } = stubTimeline();
  timeline.setData(EVENTS, [], { as_of: new Date(START + 20 * DAY).toISOString() });
  timeline.setSprints(sprintPayload([sprintRow()]));
  timeline.setLanes('object');
  timeline.fit();
  ctx.labels.length = 0;
  timeline.render();
  const labels = ctx.labels.filter((entry) => /^S\d/.test(entry.text));
  assert.ok(labels.length >= 1, 'the band keeps a label');
  assert.ok(labels.some((entry) => entry.text.startsWith('S352')), labels.map((l) => l.text).join(', '));
  assert.ok(!labels.some((entry) => entry.text.startsWith('S4392')));
});

test('prd/70 F-16: a legacy band keeps its id on the canvas', () => {
  const { timeline, ctx } = stubTimeline();
  timeline.setData(EVENTS, [], { as_of: new Date(START + 20 * DAY).toISOString() });
  timeline.setSprints(sprintPayload([sprintRow({ sprint_id: 18568, sprint_no: null })]));
  timeline.setLanes('object');
  timeline.fit();
  ctx.labels.length = 0;
  timeline.render();
  const labels = ctx.labels.filter((entry) => /^S\d/.test(entry.text));
  assert.ok(labels.some((entry) => entry.text.startsWith('S18568')));
  assert.ok(!labels.some((entry) => entry.text.startsWith('S352')));
});

/* -------------------------------------------------------------- source guard */

test('prd/70 F-16: no surface builds its own `S${...}` label', () => {
  // the shared formatter is imported, not re-implemented
  assert.match(SPRINTS, /import \{[^}]*sprintLabel[^}]*\} from '\.\/format\.js'/);
  assert.match(TIMELINE, /sprintLabel/);
  assert.match(MAIN, /sprintLabel/);
  // the old hand-rolled labels are gone
  assert.doesNotMatch(MAIN, /`S\$\{state\.sprint\}`/);
  assert.doesNotMatch(MAIN, /sprint S\$\{state\.sprint\}/);
  assert.doesNotMatch(SPRINTS, /label: `S\$\{sprint\.sprint_id\}`/);
  assert.doesNotMatch(TIMELINE, /const key = `S\$\{sprint\.id\}`/);
});

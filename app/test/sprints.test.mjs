/* The sprint lens in the UI (prd/26 P6): bands, scope, carry-over, strip.
 *
 * The canvas bands are drawn from `sprintBands`; the strip is pure markup. Both
 * are asserted without a browser, including the sprint-row window attributes
 * that make a row set the timeframe, and the out-of-scope / carry-over lists
 * that are ticket links.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { sprintBands, sprintScopeLine, sprintStrip } from '../sprints.js';

function payload(overrides = {}) {
  return {
    ok: true,
    available: true,
    count: 3,
    active: 3,
    sprints: [
      {
        sprint_id: 1, object_id: 'SPRINT:DEL#1', state: 'closed', open: false,
        start: '2026-08-17T09:00:00Z', end: '2026-09-10T16:00:00Z',
        band_start: '2026-08-17T09:00:00Z', band_end: '2026-09-10T16:00:00Z',
        planned_end: '2026-09-10', days: 24.3,
        story_count: 30, current_count: 30, points: 112, done: 26,
        open_stories: 4, carried_in: [], carried_out: ['DEL-101'],
      },
      {
        sprint_id: 2, object_id: 'SPRINT:DEL#2', state: 'closed', open: false,
        start: '2026-09-11T09:00:00Z', end: '2026-10-07T16:00:00Z',
        band_start: '2026-09-11T09:00:00Z', band_end: '2026-10-07T16:00:00Z',
        planned_end: '2026-10-07', days: 26.3,
        story_count: 30, current_count: 30, points: 118, done: 27,
        open_stories: 3, carried_in: ['DEL-101'], carried_out: [],
      },
      {
        sprint_id: 3, object_id: 'SPRINT:DEL#3', state: 'active', open: true,
        start: '2026-10-08T09:00:00Z', end: null,
        band_start: '2026-10-08T09:00:00Z', band_end: '2026-11-17T02:25:45Z',
        planned_end: '2026-11-27', days: 39.7,
        story_count: 30, current_count: 30, points: 125, done: 28,
        open_stories: 2, carried_in: [], carried_out: [],
      },
    ],
    scope: {
      stories: 100, in_sprint: 90, out_of_sprint: 10,
      in_share: 0.9, out_share: 0.1,
      never_in_sprint: 10, removed_to_backlog: 0,
      unplanned: ['DEL-190', 'DEL-191'],
    },
    carry_over: {
      count: 1, share: 0.01, stories: ['DEL-101'], sprints_involved: 2,
      rows: [{ key: 'DEL-101', sprints: [1, 2] }],
    },
    rows: [],
    note: null,
    ...overrides,
  };
}

test('a band spans its measured window in milliseconds', () => {
  const bands = sprintBands(payload());
  assert.equal(bands.length, 3);
  assert.equal(bands[0].label, 'S1');
  assert.equal(bands[0].start, Date.parse('2026-08-17T09:00:00Z'));
  assert.equal(bands[0].end, Date.parse('2026-09-10T16:00:00Z'));
  // the active sprint's band ends at the snapshot, not at its planned end
  assert.equal(bands[2].open, true);
  assert.equal(bands[2].end, Date.parse('2026-11-17T02:25:45Z'));
  assert.notEqual(bands[2].end, Date.parse('2026-11-27T23:59:59Z'));
  assert.equal(bands[2].plannedEnd, Date.parse('2026-11-27T23:59:59Z'));
});

test('a payload without sprint data yields no bands and no strip', () => {
  assert.deepEqual(sprintBands(null), []);
  assert.deepEqual(sprintBands({ available: false, sprints: [] }), []);
  assert.equal(sprintStrip(null), '');
  assert.equal(sprintStrip({ available: false }), '');
  assert.equal(sprintScopeLine({ available: false }), '');
});

test('the scope line counts in, out and carried over', () => {
  assert.equal(
    sprintScopeLine(payload()),
    '3 sprints · 90 of 100 stories in a sprint · 10 out of scope · 1 carried over · active S3',
  );
});

test('the strip is one dense row per sprint with its own scope', () => {
  const html = sprintStrip(payload());
  assert.match(html, /class="sprint-strip"/);
  assert.match(html, /3 sprints · 90 of 100 stories in a sprint/);
  // every sprint is a row, and each row carries the window it sets
  assert.equal((html.match(/data-sprint-id=/g) || []).length, 3);
  assert.match(html, /data-sprint-id="1"/);
  assert.match(html, /data-sprint-start="\d+"/);
  assert.match(html, /data-sprint-end="\d+"/);
  assert.match(html, /30 stor(?:y|ies) · 112 pts/);
  assert.match(html, /26 done/);
  // the active sprint is marked, and its plan is stated separately
  assert.match(html, /sprint-row open/);
  assert.match(html, /plan 2026-11-27/);
});

test('carry-over is marked on the sprint and listed with links', () => {
  const html = sprintStrip(payload(), { base: 'http://x/' });
  assert.match(html, /↻ 1 out/);            // sprint 1 carried DEL-101 out
  assert.match(html, /↻ 1 in/);             // sprint 2 carried it in
  // the mark on the story lane is explained, not left as a mystery glyph
  assert.match(html, /carried over — marked on the story lane/);
  assert.match(html, /class="sprint-carry-list"/);
  assert.match(html, /href="http:\/\/x\/#ticket\/DEL-101"/);
  assert.match(html, /1 → 2/);
});

test('no carry-over means no mark and no legend', () => {
  const data = payload({
    carry_over: { count: 0, share: 0, stories: [], sprints_involved: 0, rows: [] },
    sprints: payload().sprints.map((sprint) => ({
      ...sprint, carried_in: [], carried_out: [],
    })),
  });
  const html = sprintStrip(data);
  assert.doesNotMatch(html, /↻/);
  assert.doesNotMatch(html, /marked on the story lane/);
});

test('out-of-scope stories are listed with their two reasons', () => {
  const html = sprintStrip(payload(), { base: '' });
  assert.match(html, /2 stories out of sprint scope/);
  assert.match(html, /10 never planned · 0 removed to the backlog/);
  assert.match(html, /href="#ticket\/DEL-190"/);
  assert.match(html, /href="#ticket\/DEL-191"/);
});

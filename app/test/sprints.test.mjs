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

import { sprintBands, sprintScopeLine, sprintStrip, atRiskMarkup } from '../sprints.js';

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
        done_by_end: 26, done_late: 4, never_done: 0, done_share: 0.867,
        late_hours: 30, stories: ['DEL-100', 'DEL-101'],
        open_stories: 0, carried_in: [], carried_out: ['DEL-101'],
      },
      {
        sprint_id: 2, object_id: 'SPRINT:DEL#2', state: 'closed', open: false,
        start: '2026-09-11T09:00:00Z', end: '2026-10-07T16:00:00Z',
        band_start: '2026-09-11T09:00:00Z', band_end: '2026-10-07T16:00:00Z',
        planned_end: '2026-10-07', days: 26.3,
        story_count: 30, current_count: 30, points: 118, done: 27,
        done_by_end: 15, done_late: 12, never_done: 3, done_share: 0.5,
        late_hours: 448, stories: ['DEL-110', 'DEL-111'],
        open_stories: 3, carried_in: ['DEL-101'], carried_out: [],
      },
      {
        sprint_id: 3, object_id: 'SPRINT:DEL#3', state: 'active', open: true,
        start: '2026-10-08T09:00:00Z', end: null,
        band_start: '2026-10-08T09:00:00Z', band_end: '2026-11-17T02:25:45Z',
        planned_end: '2026-11-27', days: 39.7,
        story_count: 30, current_count: 30, points: 125, done: 28,
        done_by_end: 28, done_late: 0, never_done: 2, done_share: 0.933,
        late_hours: 0, stories: ['DEL-120', 'DEL-121'],
        open_stories: 2, carried_in: [], carried_out: [],
      },
    ],
    scope: {
      stories: 100, in_sprint: 90, out_of_sprint: 10,
      in_share: 0.9, out_share: 0.1,
      never_in_sprint: 10, removed_to_backlog: 0,
      unplanned: ['DEL-190', 'DEL-191'],
    },
    delivery: {
      target_share: 0.8, measured_sprints: 2, on_time_sprints: 0,
      done_by_end: 41, done_late: 16, never_done: 3, late_hours: 478,
    },
    carry_over: {
      count: 1, share: 0.01, stories: ['DEL-101'], sprints_involved: 2,
      late: 1, never: 0,
      rows: [{ key: 'DEL-101', sprints: [1, 2], status: 'late' }],
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
    '3 sprints · 90 of 100 stories in a sprint · 10 out of scope · 1 carried over · 1 finished late · active S3',
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
  assert.match(html, /26 of 30 by end/);
  assert.match(html, /4 late/);
  // the active sprint is marked, and its plan is stated separately
  assert.match(html, /sprint-row open/);
  assert.match(html, /plan 2026-11-27/);
  // prd/37: the strip states the on-time verdict of the closed sprints
  assert.match(html, /0\/2 sprints on time/);
});

test('the sprint lens marks the selected sprint and offers a clear', () => {
  const html = sprintStrip(payload(), { focus: 2 });
  // the selected row is a pressed toggle, not just a highlighted button
  assert.match(html, /class="sprint-row[^"]*selected"/);
  assert.match(html, /aria-pressed="true"/);
  assert.match(html, /data-sprint-focus="2"/);
  assert.match(html, /S2 lens/);
  assert.match(html, /data-sprint-clear/);
  // the other rows are not pressed
  assert.match(html, /data-sprint-id="1"[\s\S]*?aria-pressed="false"/);
});

test('a band carries the stories the lens filters the lanes to', () => {
  const bands = sprintBands(payload());
  assert.deepEqual(bands[0].stories, ['DEL-100', 'DEL-101']);
  assert.equal(bands[0].doneByEnd, 26);
  assert.equal(bands[0].doneLate, 4);
  assert.equal(bands[1].doneShare, 0.5);
});

test('carry-over is marked on the sprint and listed with links', () => {
  const html = sprintStrip(payload(), { base: 'http://x/' });
  assert.match(html, /↻ 1 out/);            // sprint 1 carried DEL-101 out
  assert.match(html, /↻ 1 in/);             // sprint 2 carried it in
  // the mark on the story lane is explained, not left as a mystery glyph
  assert.match(html, /committed but not done by the close/);
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

/* ------------------------------------------------------------------------- */
/* sprint-at-risk (prd/52)                                                    */
/* ------------------------------------------------------------------------- */
function atRisk(overrides = {}) {
  return {
    available: true,
    sprint_id: 3,
    planned_end: '2026-11-27',
    band_end: '2026-11-17T02:25:45Z',
    stories: 30,
    done_by_end: 25,
    points: 125,
    count: 3,
    points_at_risk: 15,
    basis: 'the active sprint\'s not-delivered stories, priced to the snapshot',
    rows: [
      {
        key: 'DEL-167', points: 8, reason: 'done-but-unmerged',
        who: ['CaOs', 'FrMo'], waiting_hours: 168,
        detail: { prs: [53], done_at: '2026-10-19T12:38:42Z' },
      },
      {
        key: 'DEL-176', points: 5, reason: 'stuck-on',
        who: ['FrMo'], waiting_hours: 131,
        detail: { prs: [60] },
      },
      {
        key: 'DEL-161', points: 2, reason: 'approved-unmerged',
        who: ['ErLi'], waiting_hours: 300,
        detail: {
          pr: 34, state: 'declined', approved_by: ['DaKo', 'FrMo'],
          closed_by: 'BiBi', bot_closed: true,
        },
      },
    ],
    ...overrides,
  };
}

test('at-risk names each open story, why, and who owes it', () => {
  const html = atRiskMarkup({ at_risk: atRisk() }, { base: '' });
  assert.match(html, /id="sprint-at-risk"/);
  assert.match(html, /Sprint 3 at risk/);
  assert.match(html, /3 stor(?:y|ies) · 15 pts/);
  assert.match(html, /25 of 30 delivered by the snapshot/);
  assert.match(html, /plan ends 2026-11-27/);
  // each row is a ticket link with its own reason and holder set
  assert.match(html, /data-at-risk-key="DEL-167" data-risk="done-but-unmerged"/);
  assert.match(html, /data-at-risk-key="DEL-176" data-risk="stuck-on"/);
  assert.match(html, /data-at-risk-key="DEL-161" data-risk="approved-unmerged"/);
  assert.match(html, /href="#ticket\/DEL-167"/);
  assert.match(html, /Done in Jira, PR unmerged/);
  // the stuck story names the person, not a generic queue
  assert.match(html, /stuck on FrMo/);
  assert.match(html, /data-risk-who="FrMo"/);
  // the orphan names both approvals and the bot that closed it
  assert.match(html, /approved by DaKo \+ FrMo · closed by the bot BiBi/);
  assert.match(html, /waiting|168 h/);
});

test('a delivered sprint has no at-risk section body', () => {
  assert.equal(atRiskMarkup({}), '');
  assert.equal(atRiskMarkup({ at_risk: { available: false, rows: [] } }), '');
  const html = atRiskMarkup({ at_risk: atRisk({ count: 0, points_at_risk: 0, rows: [] }) });
  assert.match(html, /Every story in the active sprint is done and its code merged/);
});

test('the at-risk table rides the sprint strip', () => {
  const html = sprintStrip(payload({ at_risk: atRisk() }), { base: 'http://x/' });
  assert.match(html, /id="sprint-at-risk"/);
  assert.match(html, /href="http:\/\/x\/#ticket\/DEL-176"/);
  assert.match(html, /stuck on FrMo/);
});

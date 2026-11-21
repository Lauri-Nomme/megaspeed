/* prd/64 F-17 — the sprint bucket's min-N contract is its committed stories.
 *
 * A `bucket=sprint` trend holds exactly one sprint per bucket (`n = 1`), so a
 * floor counting buckets can never be met: the strip was permanently
 * "– no bucket meets the n≥2 floor" while the chart drew a rising line. The
 * server now sends `min_n_basis: 'stories'` (floor `≥ 5` committed stories) for
 * `metric=on_time` + `bucket=sprint`, and the strip/table say so:
 *
 *   50.0% latest (S2 · 15/30 stories) · −10.0 pts since S1
 *
 * Week buckets keep the `n≥2` closed-sprint rule, and the tab names the plotted
 * quantity ("sprint story delivery share") so it is not read as the
 * "sprints on time (≥80 %)" count on Timeline/Rules.
 */

import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  trendMarkdown, trendStats, trendTable, trendTabs,
} from '../trend.js';

/** The sprint bucket for the QA repro: S1 18/30, S2 15/30, S3 active. */
const sprintPayload = () => ({
  metric: 'on_time',
  label: 'Sprint story delivery share',
  unit: 'ratio',
  bucket: 'sprint',
  definition: "stories done by the sprint's close ÷ the sprint's committed stories",
  points: [
    { label: 'S1', value: 0.6, n: 1, stories: 30, done_by_end: 18, in_progress: false },
    { label: 'S2', value: 0.5, n: 1, stories: 30, done_by_end: 15, in_progress: false },
    { label: 'S3', value: null, n: 0, in_progress: true },
  ],
  summary: {
    first: 0.6, latest: 0.5, change: -0.1, buckets: 3, measured_buckets: 2,
    latest_label: 'S2', latest_n: 1,
    latest_stories: 30, latest_done_by_end: 15,
    first_label: 'S1', first_n: 1, latest_provisional: false, settled_buckets: 2,
    min_n: 5, min_n_basis: 'stories',
    below_min: [], low_n_after_headline: [], in_progress: ['S3'],
  },
  metrics: [
    { id: 'on_time', label: 'Sprint story delivery share', unit: 'ratio', higher_is_better: true },
  ],
});

/** A week bucket of the same metric: the floor counts closed sprints (n≥2). */
const weekPayload = (summary) => ({
  metric: 'on_time',
  label: 'Sprint story delivery share',
  unit: 'ratio',
  bucket: 'week',
  points: [
    { label: '12 Jan', value: 0.6, n: 1, stories: 30, done_by_end: 18, in_progress: false },
    { label: '26 Jan', value: 0.5, n: 1, stories: 30, done_by_end: 15, in_progress: false },
  ],
  summary: {
    first: null, latest: null, change: null, buckets: 2, measured_buckets: 2,
    latest_label: null, latest_n: null, first_label: null, first_n: null,
    latest_provisional: false, settled_buckets: 2,
    min_n: 2, min_n_basis: 'n',
    below_min: [{ label: '12 Jan', n: 1 }, { label: '26 Jan', n: 1 }],
    low_n_after_headline: [], in_progress: [],
    ...summary,
  },
});

test('F17: a sprint bucket headlines its story share and the delta since S1', () => {
  const stats = trendStats(sprintPayload());
  // the exact QA sentence: 50.0% of S2's 30 committed stories, −10 pts vs S1
  assert.match(stats, /<b>50\.0%<\/b> latest \(S2 · 15\/30 stories\)/);
  assert.match(stats, /−10\.0 pts since S1/);
  // the bucket count is not the sample: no "n=1" headline, no n≥2 floor
  assert.doesNotMatch(stats, /n=1/);
  assert.doesNotMatch(stats, /no bucket meets the n≥2 floor/);
  assert.match(stats, /2<\/b>\/3 buckets measured/);
  assert.match(stats, /S3<\/b> in progress \(not in the headline\)/);
});

test('F17: the table shows the sprint cohort as "done/committed stories", not n=1', () => {
  const table = trendTable(sprintPayload());
  assert.match(table, /<th class="c-dur num" title="the sprint's stories delivered by its close ÷ committed">stories<\/th>/);
  assert.match(table, /data-label="stories">18\/30 stories/);
  assert.match(table, /data-label="stories">15\/30 stories/);
  // 30 committed stories clears the ≥5 floor: no "low n" marker
  assert.doesNotMatch(table, /low n/);
  // the value column keeps the share
  assert.match(table, /50\.0%/);
});

test('F17: a sprint under five committed stories is marked low n', () => {
  const payload = sprintPayload();
  payload.points[1] = {
    label: 'S2', value: 0.3333, n: 1, stories: 3, done_by_end: 1, in_progress: false,
  };
  payload.summary = {
    ...payload.summary,
    latest: 0.6, latest_label: 'S1', latest_n: 1,
    latest_stories: 30, latest_done_by_end: 18,
    change: null, below_min: [{ label: 'S2', n: 1, stories: 3 }],
    low_n_after_headline: ['S2'],
  };
  const table = trendTable(payload);
  assert.match(table, /data-label="stories">1\/3 stories <span class="muted" title="below the minimum cohort — 5 committed stories — drawn, not headlined">low n<\/span>/);
  // the drawn point is not the headline, and the strip names the skip
  const stats = trendStats(payload);
  assert.match(stats, /<b>60\.0%<\/b> latest \(S1 · 18\/30 stories\)/);
  assert.match(stats, /1 newer bucket below 5 committed stories \(S2\)/);
});

test('F17: no sprint clearing the story floor says which quantity is short', () => {
  const payload = sprintPayload();
  payload.summary = {
    ...payload.summary,
    first: null, latest: null, latest_label: null, latest_n: null,
    first_label: null, latest_stories: null, latest_done_by_end: null,
    change: null, below_min: [{ label: 'S2', n: 1, stories: 3 }],
    low_n_after_headline: [],
  };
  const stats = trendStats(payload);
  assert.match(stats, /no sprint meets the 5 committed-story floor/);
  assert.match(stats, /the latest measured sprint, S2, has 3 committed stories/);
  assert.doesNotMatch(stats, /n≥2/);
});

test('F17: week buckets keep the n≥2 closed-sprint floor', () => {
  const stats = trendStats(weekPayload());
  assert.match(stats, /no bucket meets the n≥2 floor/);
  assert.match(stats, /the latest measured bucket, 26 Jan, has n=1/);
  assert.doesNotMatch(stats, /committed-story floor/);
  // and the table keeps the n column for a week bucket
  const table = trendTable(weekPayload());
  assert.match(table, /<th class="c-dur num" title="the cohort behind the point \(PRs or sprints\)">n<\/th>/);
  assert.match(table, /data-label="n">1 <span class="muted" title="below the minimum cohort — n≥2 — drawn, not headlined">low n<\/span>/);
});

test('F17: the tab names the plotted quantity, not "sprints on time (≥80 %)"', () => {
  const payload = sprintPayload();
  assert.match(trendTabs(payload), /Sprint story delivery share/);
  assert.doesNotMatch(trendTabs(payload), /Sprint on-time delivery/);
  // Markdown carries the same cohort column and the same framing
  const markdown = trendMarkdown(payload);
  assert.match(markdown, /\| bucket \| Sprint story delivery share \| stories \|/);
  assert.match(markdown, /\| S2 \| 50\.0% \| 15\/30 stories \|/);
  assert.match(markdown, /sprint's close ÷ the sprint's committed stories/);
});

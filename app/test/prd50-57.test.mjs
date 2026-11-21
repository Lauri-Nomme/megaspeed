/* prd/50 trend / time-series + prd/57 benchmark contrast.
 *
 * Both features are pure markup: the trend is an inline SVG built from the
 * `/api/trend` payload, and the contrast is one helper rendering the server's
 * `{median, multiple, n}` block. These tests pin the chart (points, gaps, the
 * in-progress bucket, labels) and the contrast on every surface that shows a
 * per-item duration, including the toggle and the small-N suppression.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  BENCHMARK_MIN_N, benchmarkInline, benchmarkReady, benchmarkText,
  multipleLabel,
} from '../benchmark.js';
import { deliveryTable, detailMarkup } from '../tables.js';
import { responseTable } from '../queues.js';
import { ticketStats } from '../ticket.js';
import { segmentBar } from '../ticket.js';
import {
  trendChange, trendChart, trendGeometry, trendMarkdown, trendMarkup,
  trendStats, trendTable, trendTabs, trendValue,
} from '../trend.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/* ------------------------------------------------------------------ prd/57 */

const contrast = (overrides = {}) => ({
  available: true, median: 10, multiple: 2, n: 11, min_n: 10,
  basis: 'median of the same segment over completed stories',
  note: null,
  ...overrides,
});

test('the contrast helper renders the concept form and refuses a small cohort', () => {
  const html = benchmarkInline(contrast());
  assert.match(html, /\(median 1 d 2 h · 2\.0×\)/);
  assert.match(html, /class="benchmark-contrast"/);
  assert.match(html, /N=11/);
  assert.equal(benchmarkText(contrast()), 'median 1 d 2 h · 2.0×');
  assert.equal(multipleLabel(4.06), '4.1×');
  // N < 10 is suppressed even if a caller forgot to check `available`
  const small = contrast({ n: 9, available: false, multiple: null, note: 'cohort too small' });
  assert.equal(benchmarkReady(small), false);
  assert.equal(benchmarkInline(small), '');
  assert.equal(benchmarkInline(contrast(), { enabled: false }), '');
  assert.equal(BENCHMARK_MIN_N, 10);
});

test('a ticket segment shows its cohort median × and the toggle hides it', () => {
  const payload = {
    key: 'DEL-167',
    segments: {
      rows: [
        {
          key: 'review_rounds', label: 'review rounds', available: true,
          hours: 20, share: 0.25, efficiency: 0.5, no_pr_open_hours: 0,
          inverted: false, missing: [], reason: null,
          benchmark: contrast(),
        },
        {
          key: 'merge_done', label: 'merge → done', available: true,
          hours: 5, share: 0.1, efficiency: 0.2, no_pr_open_hours: 0,
          inverted: false, missing: [], reason: null,
          benchmark: contrast({ available: false, multiple: null, n: 3, note: 'cohort too small (N=3 < 10)' }),
        },
      ],
    },
  };
  const shown = segmentBar(payload);
  assert.match(shown, /review rounds<\/span> <b>20 h<\/b>/);
  assert.match(shown, /median 10 h · 2\.0×/);
  // the small-N segment keeps its hours but has no contrast
  assert.match(shown, /merge → done<\/span> <b>5 h<\/b>/);
  assert.equal((shown.match(/benchmark-contrast/g) || []).length, 1);
  const off = segmentBar(payload, { benchmark: false });
  assert.doesNotMatch(off, /median 10 h/);
  assert.match(off, /20 h/);
});

test('the ticket cycle stat carries the multiple of the completed cohort', () => {
  const html = ticketStats({
    key: 'DEL-167',
    story: { points: 8 },
    segments: { cycle_hours: 90, rows: [], measured: 8, reopen_count: 0 },
    pr_count: 1,
    benchmark: {
      available: true, rank: 4, n: 11, median_hours: 80,
      points: 8, points_median_hours: 70,
      contrast: contrast({ median: 80, multiple: 1.13, n: 11, basis: 'median created → done over completed stories' }),
    },
  });
  assert.match(html, /4th fastest of 11/);
  assert.match(html, /median 80 h · 1\.1×/);
});

test('a delivery row renders its wait next to the window cohort median', () => {
  const row = {
    object_id: 'PR:DEL/web#53', number: 53, repo: 'DEL/web', repo_short: 'web',
    tickets: ['DEL-167'], author: 'ErLi', state: 'open', ball: 'review',
    ball_label: 'review', phase: 'review',
    waiting_since: '2026-10-19T13:43:38Z', waiting_hours: 132,
    age_hours: 168, blocked: false, dependents: 0, impact_hours: 0,
    commits: 2,
    wait_benchmark: contrast({ median: 40, multiple: 3.3, n: 14, basis: 'median current wait over the window cohort (open PRs)' }),
  };
  const data = { rows: [row], counts: { waiting_week_hours: 40 } };
  const shown = deliveryTable(data, 'all');
  assert.match(shown, /16 d 4 h/);
  assert.match(shown, /\(median 5 d 0 h · 3\.3×\)/);
  assert.match(shown, /N=14/);
  assert.doesNotMatch(deliveryTable(data, 'all', { benchmark: false }), /median 5 d/);
});

test('the PR drawer phases and the response table show their cohorts', () => {
  const facts = detailMarkup({
    overline: 'PULL REQUEST',
    row: { number: 53, repo_short: 'web', author: 'ErLi', state: 'open' },
    facts: [
      ['review', '35 h', contrast({ median: 30, multiple: 1.17, basis: 'median hours in the same PR phase over merged PRs' })],
      ['waiting', '4 h', contrast({ available: false, multiple: null, n: 2, note: 'cohort too small' })],
    ],
  });
  assert.match(facts, /median 3 d 6 h · 1\.2×/);
  assert.equal((facts.match(/benchmark-contrast/g) || []).length, 1);
  assert.doesNotMatch(detailMarkup({
    overline: 'PULL REQUEST',
    row: { number: 53, repo_short: 'web', author: 'ErLi', state: 'open' },
    facts: [['review', '35 h', contrast({ median: 30, multiple: 1.17 })]],
  }, { benchmark: false }), /median 30/);

  const response = responseTable({
    response: {
      people: [{
        person: 'RrRr', role: 'reviewer', n: 12, median_hours: 18,
        mean_hours: 20, p90_hours: 30, never: 0,
        response_benchmark: contrast({ median: 9, multiple: 2, n: 120 }),
      }],
    },
  });
  assert.match(response, /median 1 d 1 h · 2\.0×/);
  assert.doesNotMatch(responseTable({
    response: {
      people: [{
        person: 'RrRr', role: 'reviewer', n: 12, median_hours: 18,
        response_benchmark: contrast({ median: 9, multiple: 2, n: 120 }),
      }],
    },
  }, { benchmark: false }), /median 1 d 1 h/);
});

test('every surface with a duration has the per-view toggle wired', () => {
  const html = read('index.html');
  // delivery, ticket and queue toolbars each expose the switch
  assert.ok((html.match(/data-benchmark-toggle/g) || []).length >= 3);
  const main = read('main.js');
  assert.match(main, /function setBenchmark\(on\)/);
  assert.match(main, /'\[data-benchmark-toggle\]'/);
  assert.match(main, /benchmark: state\.benchmark/);
  assert.match(main, /if \(typeof stored\.benchmark === 'boolean'\) state\.benchmark = stored\.benchmark/);
  assert.match(main, /benchmark: state\.benchmark,\n/);
});

/* ------------------------------------------------------------------ prd/50 */

function trendPayload(overrides = {}) {
  return {
    ok: true, metric: 'flow_efficiency', label: 'Flow efficiency', unit: 'ratio',
    higher_is_better: true, bucket: 'week',
    definition: 'active ÷ elapsed over the PRs opened in the bucket',
    available: true,
    points: [
      { start: '2026-01-05T09:00:00Z', end: '2026-01-12T00:00:00Z',
        bucket_start: '2026-01-05T00:00:00Z', bucket_end: '2026-01-12T00:00:00Z',
        label: '5 Jan', in_progress: false, available: true, value: 0.25, n: 5 },
      { start: '2026-01-12T00:00:00Z', end: '2026-01-19T00:00:00Z',
        bucket_start: '2026-01-12T00:00:00Z', bucket_end: '2026-01-19T00:00:00Z',
        label: '12 Jan', in_progress: false, available: true, value: 0.3, n: 4 },
      // a week with no PR: the line must break, not drop to zero
      { start: '2026-01-19T00:00:00Z', end: '2026-01-26T00:00:00Z',
        bucket_start: '2026-01-19T00:00:00Z', bucket_end: '2026-01-26T00:00:00Z',
        label: '19 Jan', in_progress: false, available: false, value: null, n: 0 },
      { start: '2026-01-26T00:00:00Z', end: '2026-02-02T00:00:00Z',
        bucket_start: '2026-01-26T00:00:00Z', bucket_end: '2026-02-02T00:00:00Z',
        label: '26 Jan', in_progress: true, available: true, value: 0.35, n: 2 },
    ],
    summary: {
      // prd/63 B8: the headline is the latest *settled* bucket (12 Jan), not
      // the in-progress one (26 Jan) — which is plotted and labelled only.
      first: 0.25, latest: 0.3, change: 0.05, buckets: 4,
      measured_buckets: 3, in_progress: ['26 Jan'],
      latest_label: '12 Jan', latest_n: 4, first_label: '5 Jan', first_n: 5,
      latest_provisional: false, settled_buckets: 2,
    },
    metrics: [
      { id: 'flow_efficiency', label: 'Flow efficiency', unit: 'ratio', higher_is_better: true },
      { id: 'cycle_median', label: 'Cycle time (median)', unit: 'hours', higher_is_better: false },
      { id: 'on_time', label: 'Sprint on-time delivery', unit: 'ratio', higher_is_better: true },
    ],
    note: null,
    ...overrides,
  };
}

test('the trend chart draws one point per bucket and marks the active one', () => {
  const payload = trendPayload();
  const svg = trendChart(payload);
  assert.match(svg, /class="trend-chart"/);
  assert.match(svg, /role="img"/);
  assert.match(svg, /data-trend-point="5 Jan"/);
  assert.match(svg, /data-trend-point="26 Jan"/);
  assert.match(svg, /data-in-progress="true"/);
  assert.match(svg, /in progress/);
  assert.match(svg, /5 Jan/);
  assert.match(svg, /26 Jan/);
  // values are in the metric's unit and the y axis labels say so
  assert.match(svg, /25\.0%/);
  // three circles: the empty week is a gap, not a zero
  assert.equal((svg.match(/<circle/g) || []).length, 3);
  // no polyline across the gap: two measured runs
  assert.equal((svg.match(/<polyline/g) || []).length, 2);
});

test('the trend geometry is time-proportional and leaves nulls as gaps', () => {
  const geometry = trendGeometry(trendPayload());
  assert.equal(geometry.points.length, 4);
  assert.equal(geometry.points[2].y, null);
  assert.ok(geometry.points[0].x < geometry.points[1].x);
  assert.ok(geometry.points[1].x < geometry.points[3].x);
  assert.equal(geometry.min, 0.25);
  assert.equal(geometry.max, 0.35);
  assert.deepEqual(trendGeometry({ points: [] }).points, []);
});

test('metric values render in their own unit and the change is stated', () => {
  assert.equal(trendValue(0.253, 'ratio'), '25.3%');
  assert.equal(trendValue(30, 'hours'), '3 d 6 h');
  assert.equal(trendValue(4, 'count'), '4');
  assert.equal(trendValue(null, 'count'), '–');
  assert.equal(trendChange({ change: 0.1 }, 'ratio'), '+10.0 pts');
  assert.equal(trendChange({ change: -30 }, 'hours'), '−3 d 6 h');
  assert.equal(trendChange({ change: 2 }, 'count'), '+2');
  assert.equal(trendChange({ change: 0 }, 'ratio'), 'no change');
});

test('the trend page has metric tabs, a bucket switch and the table', () => {
  const html = trendMarkup(trendPayload(), { bucket: 'week' });
  assert.match(html, /data-trend-metric="flow_efficiency"/);
  assert.match(html, /aria-selected="true"/);
  assert.match(html, /data-trend-metric="cycle_median"/);
  assert.match(html, /data-trend-bucket="week"/);
  assert.match(html, /data-trend-bucket="sprint"/);
  assert.match(html, /class="table compact trend-table"/);
  assert.match(html, /data-trend-row="26 Jan"/);
  assert.match(html, /<tr data-trend-row="26 Jan"\s+class="in-progress">/);
  // an empty store is a stated result, not a blank chart
  assert.match(trendMarkup({ available: false, points: [], metrics: [], note: 'no SPRINT object in this store' }),
    /no SPRINT object in this store/);
  assert.match(trendMarkup(null), /Loading the trend/);
});

test('the trend stats and Markdown quote the latest settled bucket', () => {
  const stats = trendStats(trendPayload());
  // prd/63 B8: latest = 12 Jan (n=4), never the in-progress 26 Jan point
  assert.match(stats, /30\.0%<\/b> latest \(12 Jan · n=4\)/);
  assert.match(stats, /3<\/b>\/4 buckets measured/);
  assert.match(stats, /\+5\.0 pts since the first measured bucket/);
  assert.match(stats, /26 Jan<\/b> in progress \(not in the headline\)/);
  assert.doesNotMatch(stats, /35\.0%<\/b> latest/);
  const markdown = trendMarkdown(trendPayload());
  assert.match(markdown, /# Flow efficiency per week/);
  assert.match(markdown, /\| 26 Jan \(in progress\) \| 35\.0% \| 2 \|/);
  assert.match(trendTabs(trendPayload()), /Flow efficiency/);
});

test('prd/50 is wired as a view: nav, route, endpoint and tab state', () => {
  const html = read('index.html');
  assert.match(html, /id="page-trend"/);
  assert.match(html, /id="trend-body"/);
  assert.match(html, /data-page="trend"/);
  const main = read('main.js');
  assert.match(main, /trend: \{ title: 'Trend'/);
  assert.match(main, /\/api\/trend\?metric=/);
  assert.match(main, /if \(page === 'trend'\) renderTrendPage\(\)/);
  assert.match(main, /trendMetric: state\.trendMetric/);
  assert.match(main, /trendBucket: state\.trendBucket/);
  assert.match(read('route.js'), /'queue', 'trend', 'rules'/);
});

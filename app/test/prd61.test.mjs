/* prd/61 — intervention-aware trend: annotations, before/after, stability.
 *
 * The whole surface is pure markup, so these tests pin:
 *   - the chart's annotation hairlines (at the same time scale as the series),
 *     the calendar span bands and the "counted, not drawn" rule;
 *   - the four-outcome verdict pill (and the rule that `can't tell` is a
 *     neutral outline, never a failure colour);
 *   - both n's and the CI in the before/after panel, with the MWU p only in a
 *     tooltip — never a naked p-value;
 *   - the stability strip's chips, the Little's Law chip and the overload flag
 *     that refuses a forecast;
 *   - the phone markup (a tappable 44 px marker, no hover-only affordance, the
 *     before/after table as a two-column grid) and the Markdown export.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  INTERVAL_NOTE, OUTCOMES, OUTCOME_ORDER, annotationCounts,
  annotationEmptyMarkup, annotationList, annotationMarkdown, annotationRecordMarkup,
  annotationSummary, beforeAfterMarkdown, beforeAfterPanel, ciBar, compareValue,
  outcomePill, stabilityMarkdown, stabilityStrip, verdictSentence,
} from '../intervention.js';
import { cumulativeFlowLegend, trendChart, trendGeometry, trendMarkdown, trendMarkup } from '../trend.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/* ------------------------------------------------------------------ fixtures */

function payload(overrides = {}) {
  return {
    ok: true,
    metric: 'cycle_median',
    label: 'Cycle time (median)',
    definition: 'median PR open → merged hours over the PRs opened in the bucket',
    unit: 'hours',
    bucket: 'week',
    available: true,
    points: [
      { label: '8 Sep', bucket_start: '2026-09-07T00:00:00Z', start: '2026-09-07T00:00:00Z', end: '2026-09-14T00:00:00Z', in_progress: false, available: true, value: 30, n: 9 },
      { label: '15 Sep', bucket_start: '2026-09-14T00:00:00Z', start: '2026-09-14T00:00:00Z', end: '2026-09-21T00:00:00Z', in_progress: false, available: true, value: 28, n: 7 },
      { label: '22 Sep', bucket_start: '2026-09-21T00:00:00Z', start: '2026-09-21T00:00:00Z', end: '2026-09-28T00:00:00Z', in_progress: false, available: true, value: 25, n: 8 },
      { label: '29 Sep', bucket_start: '2026-09-28T00:00:00Z', start: '2026-09-28T00:00:00Z', end: '2026-10-05T00:00:00Z', in_progress: false, available: true, value: 24, n: 6 },
      { label: '6 Oct', bucket_start: '2026-10-05T00:00:00Z', start: '2026-10-05T00:00:00Z', end: '2026-10-12T00:00:00Z', in_progress: true, available: true, value: 23, n: 3 },
    ],
    annotations: [
      { id: 'a1', date: '2026-10-08', kind: 'review_sla', value: 4, label: 'review SLA 4 h', note: 'adopted after the retro', policy: true, in_window: true },
      { id: 'a2', date: '2026-07-01', kind: 'wip_limit', value: 6, label: 'WIP limit 6', note: null, policy: true, in_window: false },
    ],
    spans: [
      { kind: 'holiday', start: '2026-08-20', end: '2026-08-20', label: 'taasiseseisvumispäiv', in_window: false },
      { kind: 'holiday', start: '2026-09-10', end: '2026-09-25', label: 'team offsite', in_window: true },
      { kind: 'vacation', start: '2026-10-01', end: '2026-10-02', label: 'LaNo on vacation', person: 'LaNo', in_window: true },
    ],
    annotation_counts: { policy: 1, holiday: 1, vacation: 1, inside: 3, outside: 1, total: 2, spans_outside: 1 },
    kinds: [
      { id: 'review_sla', label: 'review SLA', unit: 'h', numeric: true },
      { id: 'wip_limit', label: 'WIP limit', unit: null, numeric: true },
      { id: 'size_cap', label: 'PR size cap', unit: 'commits', numeric: true },
      { id: 'sentinel_added', label: 'sentinel added', unit: null, numeric: false },
      { id: 'sentinel_rotation', label: 'sentinel rotation', unit: null, numeric: false },
      { id: 'refinement_change', label: 'refinement change', unit: null, numeric: false },
      { id: 'other', label: 'other', unit: null, numeric: false },
    ],
    stability: {
      available: true,
      bucket: 'week',
      flag: 'filling',
      reason: 'queue grows faster than the exit rate',
      refuses_forecast: false,
      buckets: 13,
      min_buckets: 4,
      wip: { slope_per_week: 1.0, from: 6, to: 17, mean: 11.38, trend: 'rising' },
      throughput: { slope_per_week: 0.286, mean: 8.15, trend: 'rising', unit: 'PRs merged per week' },
      little: { implied_weeks: 1.4, measured_weeks: 0.67, ratio: 2.09, flag: 'dead_wip', dead_items: 9, note: "Little's Law implies 1.4 wk of cycle time but the measured (Kaplan–Meier) median is 0.7 wk: 9 items have no live PR; they inflate WIP without ever departing." },
      arrivals_departures: { arrivals_per_week: 10.08, departures_per_week: 8.15, gap_items: 25, sentence: 'Arrivals 10.1/wk vs departures 8.2/wk (+25 items).' },
      note: "The queue is filling: WIP grows 3.5× faster than throughput. Little's Law says cycle time will rise; treat any cycle-time gain in this window with suspicion.",
    },
    summary: { min_n: 5, measured_buckets: 4, buckets: 5, latest_label: '29 Sep', latest: 24, latest_n: 6, in_progress: ['6 Oct'], below_min: [], low_n_after_headline: [] },
    ...overrides,
  };
}

function compare(overrides = {}) {
  return {
    ok: true,
    metric: 'cycle_median',
    label: 'PR cycle',
    unit: 'hours',
    higher_is_better: false,
    basis: 'PRs created before/after the split, created → merged',
    split: '2026-10-08T00:00:00Z',
    before: { n: 63, p50: 24.79, p85: 32 },
    after: { n: 43, p50: 24.74, p85: 36.18 },
    diff: -0.05,
    ci: [-3.49, 3.63],
    p_value: 0.7379,
    test: 'mann_whitney_u',
    test_method: 'normal',
    outcome: 'no_clear_change',
    reason: 'tight_around_zero',
    censored_before: 0,
    censored_after: 8,
    buckets_before: 8,
    buckets_after: 6,
    min_items: 8,
    trials: 10000,
    annotation: { id: 'a1', date: '2026-10-08', label: 'review SLA 4 h', note: 'adopted after the retro' },
    note: 'No clear change: the interval is tight around zero (inside ±20 % of the before median). 8 open item(s) are excluded and counted: a running age is not a completed duration.',
    metrics: [
      { id: 'story_cycle', label: 'Story cycle', unit: 'hours', higher_is_better: false, basis: 'stories created before/after the split, created → done' },
      { id: 'cycle_median', label: 'PR cycle', unit: 'hours', higher_is_better: false, basis: 'PRs created before/after the split, created → merged' },
    ],
    ...overrides,
  };
}

/* --------------------------------------------------------- chart annotation */

test('the chart draws a marker at the annotation date on the series time scale', () => {
  const data = payload();
  const geometry = trendGeometry(data);
  const svg = trendChart(data);
  const at = geometry.xAt(Date.parse('2026-10-08T00:00:00Z'));
  assert.match(svg, /class="trend-chart"/);
  assert.match(svg, /class="trend-annotation"/);
  assert.match(svg, /data-annotation-id="a1"/);
  assert.match(svg, new RegExp(`<line x1="${at}" y1="[\\d.]+" x2="${at}"`));
  assert.match(svg, /data-kind="review_sla"/);
  // a 44 px invisible hit area: tappable on a phone, not hover-only
  assert.match(svg, /class="annotation-hit"/);
  assert.match(svg, /width="44"/);
  assert.match(svg, /tabindex="0"/);
  assert.match(svg, /review SLA 4 h · 2026-10-08 · “adopted after the retro”/);
  // the out-of-window annotation is counted, never drawn
  assert.doesNotMatch(svg, /data-annotation-id="a2"/);
});

test('the chart bands a holiday span and labels it only when two buckets wide', () => {
  const svg = trendChart(payload());
  assert.match(svg, /class="trend-span holiday"/);
  assert.match(svg, /data-span-kind="vacation"/);
  assert.match(svg, /fill="[^"]+" opacity="0.08"/);
  // the two-day offsite is wider than two buckets and carries its label
  assert.match(svg, /team offsite/);
  // the one-day, out-of-window holiday is not drawn at all
  assert.doesNotMatch(svg, /taasiseseisvumispäiv/);
});

test('the cumulative-flow metric draws the four bands and a legend', () => {
  const bands = (created) => [
    { id: 'created', label: 'created', value: created },
    { id: 'started', label: 'started', value: created - 2 },
    { id: 'in_review', label: 'in review', value: created - 4 },
    { id: 'merged', label: 'merged', value: created - 6 },
  ];
  const flow = payload({
    metric: 'cumulative_flow',
    label: 'Cumulative flow',
    points: [10, 14, 18].map((created, index) => ({
      label: `w${index}`, bucket_start: `2026-09-0${7 + index * 7}T00:00:00Z`,
      start: `2026-09-0${7 + index * 7}T00:00:00Z`, end: `2026-09-1${4 + index * 7}T00:00:00Z`,
      in_progress: false, available: true, value: created, n: created, bands: bands(created),
    })),
  });
  const svg = trendChart(flow);
  assert.match(svg, /class="trend-band"/);
  for (const band of ['created', 'started', 'in_review', 'merged']) {
    assert.match(svg, new RegExp(`data-band="${band}"`));
  }
  const legend = cumulativeFlowLegend(flow);
  assert.match(legend, /cumulative PRs by stage/);
  assert.match(legend, /data-band="merged"/);
});

/* -------------------------------------------------------------- annotations */

test('the under-chart line counts policy changes and spans, then the outside', () => {
  const summary = annotationSummary(payload());
  assert.match(summary, /1 policy change · 1 holiday · 1 vacation in this window/);
  assert.match(summary, /2 outside the window, counted not drawn/);
  const counts = annotationCounts(payload());
  assert.deepEqual(counts, { policy: 1, holiday: 1, vacation: 1, outside: 2 });
  assert.match(annotationList(payload()), /review SLA 4 h/);
  assert.match(annotationList(payload()), /data-annotation-delete="a1"/);
  // an annotation outside the window is not a readable row either
  assert.doesNotMatch(annotationList(payload()), /WIP limit 6/);
});

test('the empty state is a sentence with the affordance, not a blank panel', () => {
  const html = annotationEmptyMarkup(payload().kinds);
  assert.match(html, /No policy changes recorded; a change you did not write down is indistinguishable from noise\./);
  assert.match(html, /data-annotation-record/);
  assert.match(html, /data-annotation-form/);
  assert.match(html, /note \(optional\)/);
  // the menu is the closed vocabulary from the payload, not a local list
  for (const kind of payload().kinds) assert.match(html, new RegExp(`value="${kind.id}"`));
  assert.match(html, /never scraped from Bitbucket or Jira/);
});

/* ------------------------------------------------------------- before/after */

test('the four outcomes have exact labels and `can\'t tell` is neutral', () => {
  assert.deepEqual(OUTCOME_ORDER, ['better', 'worse', 'no_clear_change', 'cant_tell']);
  assert.equal(OUTCOMES.better.label, 'better');
  assert.equal(OUTCOMES.worse.label, 'worse');
  assert.equal(OUTCOMES.no_clear_change.label, 'no clear change');
  assert.equal(OUTCOMES.cant_tell.label, "can't tell at this n");
  assert.match(outcomePill('better'), /outcome-pill positive/);
  assert.match(outcomePill('worse'), /outcome-pill critical/);
  assert.match(outcomePill('no_clear_change'), /outcome-pill neutral/);
  const cant = outcomePill('cant_tell');
  assert.match(cant, /outcome-pill neutral/);
  assert.doesNotMatch(cant, /critical/);
  assert.match(cant, /can&#39;t tell at this n|can't tell at this n/);
});

test('the panel shows both n\'s, the CI and the verdict; p only in a tooltip', () => {
  const html = beforeAfterPanel(compare(), { annotations: payload().annotations });
  assert.match(html, /<h3>Did it work\?<\/h3>/);
  assert.match(html, /data-before-after="no_clear_change"/);
  assert.match(html, /No clear change: the interval is tight around zero/);
  assert.match(html, /<td class="num" data-label="n">63<\/td>/);
  assert.match(html, /<td class="num" data-label="n">43<\/td>/);
  assert.match(html, /3 d 0 h/);
  assert.match(html, /data-ci-low="-3\.49"/);
  assert.match(html, /data-ci-high="3\.63"/);
  assert.match(html, /95 % CI/);
  assert.match(html, /title="Mann–Whitney U p=0\.7379/);
  // the p-value is not naked text anywhere
  assert.doesNotMatch(html.replace(/<[^>]+>/g, ''), /p=0\.7379/);
  assert.match(html, /0 open item\(s\) before and 8 after are excluded and counted/);
  assert.match(html, /not an experiment/);
  assert.match(html, /PRs created before\/after the split/);
  // the select offers every dated change, and the split picker is wired
  assert.match(html, /data-compare-split/);
  assert.match(html, /data-compare-metric/);
  assert.match(html, /review SLA 4 h · 2026-10-08/);
});

test('a small-n comparison draws no CI bar and says why', () => {
  const thin = compare({
    before: { n: 5, p50: 24, p85: 30 }, after: { n: 4, p50: 20, p85: 26 },
    ci: null, outcome: 'cant_tell', reason: 'small_n', p_value: 0.4,
    note: "Can't tell at this n: 5 items before, 4 after — fewer than the 8 per side this comparison needs.",
  });
  const html = beforeAfterPanel(thin, { annotations: [] });
  assert.match(html, /no interval: fewer than 8 finished items on one side/);
  assert.doesNotMatch(html, /data-ci-low/);
  assert.match(html, /can(?:&#39;|')t tell at this n/);
});

test('the CI bar is centred on zero and formats in the metric unit', () => {
  const html = ciBar(compare());
  assert.match(html, /class="ci-zero"/);
  assert.match(html, /class="ci-range"/);
  assert.match(html, /<b>−3 h 29 min … \+3 h 38 min<\/b>/);
  assert.equal(compareValue(-9.9, 'hours', { signed: true }), '−1 d 1 h');
  assert.equal(compareValue(0.3, 'ratio', { signed: true }), '+30.0 %');
  assert.equal(compareValue(null, 'hours'), '–');
  assert.match(verdictSentence(compare()), /No clear change/);
});

/* ---------------------------------------------------------------- stability */

test('the stability strip names the flag, its chips and the Little\'s Law gap', () => {
  const html = stabilityStrip(payload().stability);
  assert.match(html, /data-stability-flag="filling"/);
  assert.match(html, /data-forecast-refused="false"/);
  assert.match(html, /WIP ↗ \+1\.0\/wk/);
  assert.match(html, /6 → 17/);
  assert.match(html, /throughput ↗ 8\.2\/wk/);
  assert.match(html, /arrivals > departures \(\+25\)/);
  assert.match(html, /implied 1\.4 wk vs measured 0\.67 wk/);
  assert.match(html, /data-little="dead_wip"/);
  assert.match(html, /Little&#39;s Law says cycle time will rise|Little's Law says cycle time will rise/);
  assert.match(html, /inflate WIP without ever departing/);
});

test('an overloaded queue refuses a forecast; insufficient history says so', () => {
  const overload = stabilityStrip({
    ...payload().stability, flag: 'overload', refuses_forecast: true,
  });
  assert.match(overload, /data-stability-flag="overload"/);
  assert.match(overload, /data-forecast-refused="true"/);
  assert.match(overload, /class="stability-strip overload"/);
  const thin = stabilityStrip({ available: false, flag: 'insufficient', note: 'Stability needs at least 4 settled week buckets; this window has 3.' });
  assert.match(thin, /data-stability-flag="insufficient"/);
  assert.match(thin, /insufficient history/);
  assert.match(thin, /at least 4 settled week buckets/);
  assert.match(stabilityMarkdown(payload().stability), /Stability: \*\*filling\*\*/);
});

/* ------------------------------------------------------ page and phone wiring */

test('the trend page renders the annotation layer, the panel and the strip', () => {
  const html = trendMarkup(payload(), { bucket: 'week', compare: compare() });
  assert.match(html, /class="trend-chart"/);
  assert.match(html, /class="annotation-summary"/);
  assert.match(html, /class="annotation-list"/);
  assert.match(html, /class="before-after"/);
  assert.match(html, /class="stability-strip/);
  assert.match(html, /data-annotation-form/);
  // no annotations → the empty state, never a blank block
  const empty = trendMarkup(payload({ annotations: [], annotation_counts: { policy: 0, holiday: 0, vacation: 0, outside: 0, inside: 0, total: 0, spans_outside: 0 } }), {});
  assert.match(empty, /No policy changes recorded/);
  assert.doesNotMatch(empty, /class="before-after"/);
});

test('the Markdown export carries the verdict, both n\'s, the CI and stability', () => {
  const text = trendMarkdown(payload(), { compare: compare() });
  assert.match(text, /## Annotations/);
  assert.match(text, /2026-10-08 — review SLA 4 h — “adopted after the retro”/);
  assert.match(text, /## Did it work\? PR cycle — review SLA 4 h/);
  assert.match(text, /\| before \| 63 \| 3 d 0 h \| 4 d 0 h \|/);
  assert.match(text, /\| after \| 43 \| 3 d 0 h \| 4 d 4 h \|/);
  assert.match(text, /\*\*no clear change\*\*/);
  assert.match(text, /95 % CI on the median difference: −3 h 29 min … \+3 h 38 min/);
  assert.match(text, /Stability: \*\*filling\*\*/);
  assert.match(text, /Little's Law: implied 1\.4 wk vs measured 0\.67 wk/);
  const annotations = annotationMarkdown(payload());
  assert.match(annotations, /WIP limit 6 \(outside the window\)/);
});

test('prd/61 is wired as a view and the phone keeps no hover-only affordance', () => {
  const html = read('index.html');
  assert.match(html, /id="page-trend"/);
  const main = read('main.js');
  assert.match(main, /\/api\/trend\/compare\?metric=/);
  assert.match(main, /\/api\/annotations/);
  assert.match(main, /recordAnnotation/);
  assert.match(main, /deleteAnnotation/);
  assert.match(main, /data-annotation-form/);
  assert.match(main, /compareMetric/);
  const css = read('style.css');
  assert.match(css, /\.trend-annotation \.annotation-hit \{ pointer-events: all; \}/);
  assert.match(css, /\.trend-span \{ pointer-events: none; \}/);
  assert.match(css, /\.outcome-pill\.positive/);
  // the phone block is present (it is not the last media block: prd/62's is)
  assert.match(css, /\.before-after-table tbody \{ display: grid/);
  assert.match(css, /\.annotation-form input, \.annotation-form select \{ height: 40px/);
  // the exact honesty language is in the panel, not paraphrased
  assert.match(INTERVAL_NOTE, /The interval is the answer\./);
});

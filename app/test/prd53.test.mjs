/* prd/53 — worklog honesty: logged vs points, logged vs active, and the trend.
 *
 * The backend metric and its per-week buckets are pinned in
 * `tests/test_worklog_trend.py`; this file pins the *card*: the headline names
 * the effort gap (not lateness), the chart shows the flatness by point size and
 * the logged-vs-active bars, the sparkline plots the per-week correlation, and
 * the pseudonym legend names roles (sentinel / bot / author), never identities.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  effortGap, fmtR, fmtRate, flatness, latenessLine, pointsBars, trendSeries,
  trendSparkline, trendVerdict, worklogHeadline, worklogMarkdown, worklogPanel,
} from '../worklog.js';
import { legendMarkup } from '../queues.js';
import { rulesMarkdown, rulesMarkup } from '../rules.js';
import { overviewMarkup } from '../tables.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

// The QA store's shape: r ≈ −0.05, a 13-pt story logs 2.5 h, 0.49× of active.
const METRIC = {
  available: true,
  worklogs: 199,
  stories_with_worklogs: 85,
  logged_hours: 416.5,
  active_hours: 846.4,
  logged_to_active_ratio: 0.49,
  points_logged_correlation: -0.05,
  points_logged_n: 6,
  points_active_correlation: 0.1,
  points_active_n: 6,
  median_logged_hours: 4,
  median_hours_per_point: 1.6,
  logged_by_points: { 1: 4, 2: 4, 3: 3, 5: 5, 8: 4, 13: 2.5 },
  mean_delay_days: 1.47,
  median_delay_days: 0.96,
  late_share: 0.025,
  trend: [
    { week_start: '2026-09-14T00:00:00Z', label: '2026-09-14', stories: 3,
      correlation: -0.1, median_hours_per_point: 2, logged_hours: 35, current: false },
    { week_start: '2026-09-21T00:00:00Z', label: '2026-09-21', stories: 4,
      correlation: 0.2, median_hours_per_point: 1.6, logged_hours: 28, current: true },
  ],
};

const SUGGESTION = {
  id: 'jira-worklog-mismatch', rank: 1, severity: 'warning',
  metric: 'worklog_cross_check',
  title: "Logged hours don't track story points (r = -0.05)",
  suggestion: 'Treat logged hours as a note, not a measure of effort.',
  impact_hours: 0, confidence: 0.1, scope: { kind: 'team' },
  evidence: {
    points_logged_correlation: -0.05, points_logged_n: 6,
    logged_hours: 416.5, active_hours: 846.4, ratio: 0.49,
    median_logged_hours: 4, median_hours_per_point: 1.6,
  },
};

test('the headline is the effort gap; lateness is a secondary line', () => {
  assert.equal(flatness(METRIC), 'flat');
  const headline = worklogHeadline(METRIC);
  assert.match(headline, /do not track story points \(r = −0\.05 over 6 stories\)/);
  assert.match(headline, /the median story logs 4 h whatever its size \(1\.6 h\/pt\)/);
  assert.match(headline, /52 d 0 h against 105 d 6 h of active PR time \(0\.49×\)/);
  // the old headline: lateness, with "secondary" printed next to it
  assert.match(latenessLine(METRIC), /1\.5 calendar days mean logging delay, 3% more than a week late \(secondary\)/);
  assert.equal(fmtR(-0.05), '−0.05');
  assert.equal(fmtR(null), '–');
  assert.equal(fmtRate(1.6), '1.6 h/pt');
});

test('the card shows logged-vs-points and logged-vs-active', () => {
  const bars = pointsBars(METRIC);
  assert.deepEqual(bars.map((bar) => bar.points), [1, 2, 3, 5, 8, 13]);
  assert.equal(bars[bars.length - 1].hours, 2.5);
  // the tallest bar is the 5-pt story; the 13-pt bar is shorter, not longer
  assert.equal(bars.find((bar) => bar.points === 5).share, 1);
  assert.ok(bars.find((bar) => bar.points === 13).share < 0.6);
  const gap = effortGap(METRIC);
  assert.equal(gap.ratio, 0.49);
  assert.equal(gap.active, 846.4);
  assert.equal(round(gap.loggedShare), round(gap.logged / gap.active));
  const html = worklogPanel(METRIC);
  assert.match(html, /data-worklog-panel/);
  assert.match(html, /data-worklog-points="13"/);
  assert.match(html, /data-worklog-headline/);
  assert.match(html, /data-worklog-late/);
  assert.match(html, /0\.49×/);
  assert.match(html, /calendar days/);
  // no pseudonym or identity leaks into the card
  assert.doesNotMatch(html, /FrMo|BiBi|LaAd/);
});

function round(value) {
  return Math.round(value * 1000) / 1000;
}

test('the per-week trend answers "is logging getting more trustworthy?"', () => {
  const points = trendSeries(METRIC);
  assert.equal(points.length, 2);
  assert.equal(points[1].current, true);
  assert.equal(trendVerdict(points), 'improving');
  assert.equal(trendVerdict(points.map((point) => ({ ...point, correlation: -0.05 }))), 'flat');
  assert.equal(trendVerdict([points[0]]), 'not enough weeks');
  const svg = trendSparkline(points);
  assert.match(svg, /<svg class="worklog-spark"/);
  assert.match(svg, /<polyline class="spark-line"/);
  assert.match(svg, /data-trend-week="2026-09-21T00:00:00Z"/);
  assert.match(svg, /data-trend-r="0.2"/);
  assert.match(svg, /r = −0\.10 · 3 stories/);
  const html = worklogPanel(METRIC);
  assert.match(html, /data-worklog-trend/);
  assert.match(html, /improving · 2 of 2 weeks with an r/);
  // the sparkline is a time axis: a bucket with no r is a gap, not a zero dot
  const gapped = trendSparkline([...points, { ...points[1], index: 2, correlation: null }]);
  assert.equal((gapped.match(/class="spark-node/g) || []).length, 2);
});

test('the Rules card carries the panel and the Markdown export carries the trend', () => {
  const data = {
    level: 'team', sample: { prs: 10, days: 90 }, hours_mode: 'business',
    as_of: '2026-11-18T00:00:00Z', note: '',
    metrics: { worklog_cross_check: METRIC },
    suggestions: [SUGGESTION],
  };
  const html = rulesMarkup(data);
  assert.ok(html.indexOf('worklog-panel') > html.indexOf('data-rule="jira-worklog-mismatch"'));
  assert.match(html, /points ↔ logged r/);
  assert.match(html, /median per point/);
  const markdown = rulesMarkdown(data);
  assert.match(markdown, /## Worklog honesty/);
  assert.match(markdown, /logged ↔ points r by week: 2026-09-14 −0\.10 · 2026-09-21 0\.20 \(improving\)/);
  assert.match(worklogMarkdown(METRIC).join('\n'), /median logged hours by points: 1 pt 4 h/);
  // a store with no worklogs renders no panel and no section
  assert.equal(worklogPanel({ available: false }), '');
  assert.deepEqual(worklogMarkdown({ available: false }), []);
});

test('the pseudonym legend names roles, never identities', () => {
  const legend = {
    people: [
      { person: 'FrMo', role: 'sentinel', sentinel: true, bot: false },
      { person: 'BiBi', role: 'bot', sentinel: false, bot: true },
      { person: 'LaAd', role: 'story owner', sentinel: false, bot: false },
      { person: 'AlNg', role: 'author', sentinel: false, bot: false },
    ],
    note: 'roles, not names: the store holds pseudonyms only (prd/02)',
  };
  const html = legendMarkup({ legend });
  assert.match(html, /who is who/);
  assert.match(html, /data-role="sentinel"/);
  assert.match(html, /<span class="legend-role">sentinel<\/span>/);
  assert.match(html, /data-role="bot"/);
  assert.match(html, /data-role="author"/);
  assert.match(html, /pseudonyms only/);
  // the legend reaches Overview (the landing view), not only the Queue footnote
  const overview = overviewMarkup({
    delivery: { rows: [], counts: { open: 0, blocked: 0, oldest_waiting: 0, all: 0 } },
    queues: { level: 'person', streams: [], legend },
    blocking: null,
  });
  assert.match(overview, /people-legend/);
  assert.match(overview, /FrMo/);
  // and the Rules page renders the server's legend next to the worklog card
  assert.match(read('main.js'), /legendMarkup\(\{ legend: data\.legend \}\)/);
  assert.match(read('main.js'), /payload\["legend"\]|data\.legend/);
});

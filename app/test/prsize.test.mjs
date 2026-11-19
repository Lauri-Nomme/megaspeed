/* PR-size calibration (prd/39) — pure arithmetic + panel markup.
 *
 * The endpoint is pinned against the store in tests/test_rules.py; these tests
 * pin the half the browser owns: normalising a tenant's caps, counting the PRs
 * a cap pair flags (a PR over both caps counts once), drawing the buckets, and
 * the panel's sliders/markers/count.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_SIZE_CAPS, SIZE_CAP_MAX, SIZE_CAP_MIN, capPercent, capsQuery,
  flagCounts, histogramBars, normalizeSizeCaps, prSizePanel, rememberSizeCaps,
  sizeCapsForTenant,
} from '../prsize.js';
import { rulesMarkup } from '../rules.js';

const ROWS = [
  { object_id: 'PR:a#1', commits: 12, diff_size: 500 },
  { object_id: 'PR:a#2', commits: 2, diff_size: 50 },
  { object_id: 'PR:a#3', commits: 1, diff_size: null },
];

function payload(overrides = {}) {
  return {
    ok: true,
    n: 3,
    caps: { commits: 10, lines: 400 },
    defaults: { commits: 10, lines: 400 },
    commits: {
      edges: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
      counts: [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1],
      max: 12,
      values: [1, 2, 12],
    },
    lines: {
      edges: [0, 50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 550],
      counts: [0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0],
      max: 500,
      values: [50, 500],
      available: true,
      n: 2,
    },
    rows: ROWS,
    large_prs: 1,
    over_commits_cap: 1,
    over_lines_cap: 1,
    ...overrides,
  };
}

test('caps normalise to positive integers with the stated defaults', () => {
  assert.deepEqual(normalizeSizeCaps(null), DEFAULT_SIZE_CAPS);
  assert.deepEqual(normalizeSizeCaps({}), DEFAULT_SIZE_CAPS);
  assert.deepEqual(normalizeSizeCaps({ commits: 4.6, lines: '900' }),
    { commits: 5, lines: 900 });
  assert.deepEqual(normalizeSizeCaps({ commits: 0, lines: -3 }),
    { commits: SIZE_CAP_MIN, lines: SIZE_CAP_MIN });
  assert.deepEqual(normalizeSizeCaps({ commits: 10 ** 9 }),
    { commits: SIZE_CAP_MAX, lines: DEFAULT_SIZE_CAPS.lines });
});

test('caps persist per tenant, like scope/timeframe', () => {
  let map = {};
  assert.deepEqual(sizeCapsForTenant(map, 'del'), DEFAULT_SIZE_CAPS);
  map = rememberSizeCaps(map, 'del', { commits: 6, lines: 250 });
  map = rememberSizeCaps(map, 'fix', { commits: 3, lines: 120 });
  assert.deepEqual(sizeCapsForTenant(map, 'del'), { commits: 6, lines: 250 });
  assert.deepEqual(sizeCapsForTenant(map, 'fix'), { commits: 3, lines: 120 });
  // an unknown tenant gets the default, never another tenant's calibration
  assert.deepEqual(sizeCapsForTenant(map, 'other'), DEFAULT_SIZE_CAPS);
  assert.equal(capsQuery({ commits: 6, lines: 250 }), 'size_commits=6&size_lines=250');
});

test('a PR over both caps is counted once in the large total', () => {
  const counts = flagCounts(ROWS, { commits: 1, lines: 50 });
  assert.equal(counts.large, 2);          // #1 (both) + #2 (50 is not > 50? 2 > 1)
  assert.equal(counts.total, 3);
  assert.equal(counts.overCommits, 2);    // 12 and 2
  assert.equal(counts.overLines, 1);      // 500; 50 is exactly the cap
  // the two dimension counts overlap on #1, so summing them would say 3
  assert.equal(counts.overCommits + counts.overLines, 3);
  assert.equal(flagCounts(ROWS, { commits: 10, lines: 400 }).large, 1);
  assert.equal(flagCounts(ROWS, { commits: 50, lines: 900 }).large, 0);
});

test('sliding a cap re-buckets the large count exactly', () => {
  // the same rows the panel receives; every step is a pure re-count
  assert.equal(flagCounts(ROWS, { commits: 10, lines: 400 }).large, 1);
  assert.equal(flagCounts(ROWS, { commits: 2, lines: 400 }).large, 1);
  assert.equal(flagCounts(ROWS, { commits: 1, lines: 400 }).large, 2);
  assert.equal(flagCounts(ROWS, { commits: 10, lines: 40 }).large, 2);
  assert.equal(flagCounts(ROWS, { commits: 10, lines: 500 }).large, 1);
});

test('histogram bars are positioned on the bucket edges', () => {
  const bars = histogramBars(payload().commits);
  assert.equal(bars.length, payload().commits.counts.length);
  assert.equal(bars.reduce((sum, bar) => sum + bar.count, 0), 3);
  // the first bar starts at 0 and the axis ends at the last edge (13)
  assert.equal(bars[0].left, 0);
  assert.equal(bars[bars.length - 1].left + bars[bars.length - 1].width, 100);
  const tallest = bars.reduce((best, bar) => (bar.height > best.height ? bar : best));
  assert.equal(tallest.count, 1);
  assert.equal(tallest.height, 100);
});

test('the cap marker sits on the cap edge', () => {
  const edges = [0, 50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 550];
  assert.equal(capPercent(edges, 400), (400 / 550) * 100);
  assert.equal(capPercent(edges, 0), 0);
  assert.equal(capPercent(edges, 999), 100);
  assert.equal(capPercent([], 10), 0);
});

test('the panel draws both histograms, both sliders and the live count', () => {
  const html = prSizePanel(payload());
  assert.match(html, /id="pr-size-panel"/);
  assert.match(html, /data-pr-size-large>1</);
  assert.match(html, /data-pr-size-total>3</);
  assert.match(html, /data-pr-size="commits"/);
  assert.match(html, /data-pr-size="lines"/);
  assert.match(html, /id="pr-size-commits"/);
  assert.match(html, /id="pr-size-lines"/);
  assert.match(html, /data-pr-size-marker="commits"/);
  assert.match(html, /data-pr-size-marker="lines"/);
  assert.match(html, /data-pr-size-reset/);
  // the same payload at a tighter calibration renders a different count
  const tighter = prSizePanel(payload(), { caps: { commits: 1, lines: 50 } });
  assert.match(tighter, /data-pr-size-large>2</);
  assert.match(tighter, /cap <b data-pr-size-cap-label="commits">1</);
});

test('a store without diff size disables the line slider but keeps the cap', () => {
  const html = prSizePanel(payload({
    lines: { ...payload().lines, available: false, values: [], counts: [], n: 0 },
  }));
  assert.match(html, /changed lines are not captured/);
  assert.match(html, /data-pr-size="lines"[^>]*disabled/);
  assert.match(html, /data-pr-size-cap-label="lines">400/);
});

test('the panel sits next to the size rule, or after the list', () => {
  const data = {
    level: 'team',
    sample: { prs: 3, days: 90 },
    metrics: {},
    suggestions: [
      {
        id: 'review-sla', rank: 1, severity: 'warning', metric: 'time_to_first_review',
        title: 'Review is slow', suggestion: 'Review sooner', impact_hours: 10,
        confidence: 0.8, evidence: {}, scope: { kind: 'team' },
      },
      {
        id: 'pr-size', rank: 2, severity: 'warning', metric: 'pr_size',
        title: 'PRs are large', suggestion: 'Cap size', impact_hours: 5,
        confidence: 0.5, evidence: {}, scope: { kind: 'team' },
      },
      {
        id: 'flow-efficiency', rank: 3, severity: 'info', metric: 'flow_efficiency',
        title: 'Flow is low', suggestion: 'Wait less', impact_hours: 1,
        confidence: 0.2, evidence: {}, scope: { kind: 'team' },
      },
    ],
  };
  const html = rulesMarkup(data, { prSize: payload(), caps: { commits: 10, lines: 400 } });
  const rule = html.indexOf('data-rule="pr-size"');
  const panel = html.indexOf('data-pr-size-panel');
  const after = html.indexOf('data-rule="flow-efficiency"');
  assert.ok(rule >= 0 && panel > rule && panel < after);

  // no size rule in the window: the calibration is still reachable
  const noRule = rulesMarkup({ ...data, suggestions: data.suggestions.slice(0, 1) },
    { prSize: payload() });
  assert.ok(noRule.indexOf('data-pr-size-panel') > noRule.indexOf('data-rule="review-sla"'));
});

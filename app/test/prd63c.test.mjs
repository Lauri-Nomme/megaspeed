/* prd/63 — v0.11.0 verification pass: the 2 not-fixed, the 12 partials and the
 * regressions it found. One regression per item.
 *
 * The V-A4 case is built from the *serialized* API shape (`JSON.parse` of a
 * JSON text whose keys are `"1.0"`), because a numeric-key fixture is a shape
 * the wire never produces — that is exactly why the original test passed.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { pointsBars, worklogPanel, worklogMarkdown } from '../worklog.js';
import {
  approvalsLabel, deliveryMarkdown, overviewMarkup, scopeEmptyAttention,
  scopedDeliveryRows,
} from '../tables.js';
import { trendErrorMarkup, trendMarkup, trendStats } from '../trend.js';
import { rulesMarkdown, rulesMarkup, metricLabel, segmentLabel, evidenceValue } from '../rules.js';
import { scopeBanner } from '../scope.js';
import { DAY, HOUR, fmtCalendarDuration } from '../format.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const row = (overrides = {}) => ({
  object_id: 'PR:DEL/web#52', number: 52, repo: 'DEL/web', repo_short: 'web',
  tickets: ['DEL-197'], ticket: 'DEL-197', story_tickets: [],
  author: 'CaOs', state: 'open', ball: 'review', ball_label: 'review',
  phase: 'review', waiting_since: '2026-11-16T10:10:00Z',
  waiting_hours: 16, age_hours: 100, blocked: false, stale: true,
  stale_reasons: ['no events for 2 days'],
  waiting_on: ['FrMo'], waiting_roles: { FrMo: 'sentinel' },
  reviewers_approved: 1, reviewers_total: 2, dependents: 0, impact_hours: 0,
  unobserved: false, ...overrides,
});

const delivery = (rows = [row()]) => ({
  rows,
  counts: {
    open: rows.length, blocked: 0, oldest_waiting: 0, all: rows.length,
    waiting_week_hours: 40, stale: rows.filter((r) => r.stale).length,
  },
  done_open: { available: true, count: 0, rows: [] },
  snapshot: '2026-11-18T00:10:44Z',
  wait_benchmark: { available: false, note: 'N=2 < 10' },
});

/* --------------------------------------------------------- V-A4 worklog keys */

test('V-A4: the worklog chart reads the keys the API actually serialises', () => {
  // The real wire shape: JSON object keys are strings, and the points column
  // comes back as a float, so the API used to send "1.0" / "13.0". This test
  // starts from a JSON text, not a JS object with numeric keys.
  const wire = JSON.parse(`{
    "available": true,
    "logged_by_points": {"1.0": 5.5, "2.0": 3.5, "3.0": 4.0, "5.0": 3.5, "8.0": 5.0, "13.0": 3.0},
    "logged_hours": 100, "active_hours": 200, "logged_to_active_ratio": 0.5,
    "points_logged_correlation": -0.05, "points_logged_n": 6,
    "median_logged_hours": 4, "median_hours_per_point": 1.6,
    "late_share": 0.02, "mean_delay_days": 1.5
  }`);
  const bars = pointsBars(wire);
  assert.deepEqual(bars.map((bar) => bar.points), [1, 2, 3, 5, 8, 13]);
  assert.deepEqual(bars.map((bar) => bar.hours), [5.5, 3.5, 4, 3.5, 5, 3]);
  // no bucket is a zero stub, and the tallest bar is the full height
  assert.ok(bars.every((bar) => bar.hours > 0));
  assert.equal(Math.max(...bars.map((bar) => bar.share)), 1);
  const html = worklogPanel(wire);
  assert.match(html, /data-worklog-points="13"[^>]*title="13 pt stories log a median 3 h"/);
  assert.doesNotMatch(html, />0 min</);
  assert.match(worklogMarkdown(wire).join('\n'), /13 pt 3 h/);
});

/* ------------------------------------------- V-A11 scoped "needs attention" */

test('V-A11: a repo-scoped Overview lists the 2-day orphan it names above', () => {
  const web = row();
  const api = row({
    object_id: 'PR:DEL/api#78', number: 78, repo: 'DEL/api', repo_short: 'api',
    waiting_hours: 60, stale: false, waiting_on: ['DaKo'],
    waiting_roles: { DaKo: 'review' }, reviewers_approved: 0,
  });
  const data = delivery([web, api]);
  const scope = { kind: 'repo', value: 'DEL/web', label: 'web' };
  const html = overviewMarkup({
    delivery: data, queues: { level: 'queue', streams: [] }, blocking: { edges: [] },
    scope, base: '',
  });
  // the chase card names the orphan…
  assert.match(html, /1 orphaned approval/);
  assert.match(html, /#52/);
  // …and "needs attention" now lists it instead of saying nothing is waiting
  assert.match(html, /data-pr="PR:DEL\/web#52"/);
  assert.doesNotMatch(html, /No open item is waiting right now/);
  // the other repo's PR is not in this scope's attention list
  assert.doesNotMatch(html, /data-pr="PR:DEL\/api#78"/);
  // an actually-empty scoped list says "in this scope", never "right now"
  assert.match(scopeEmptyAttention(true, scope, { open: 0 }), /No open PR in web/);
  assert.match(scopeEmptyAttention(true, scope, { open: 2 }), /No open item in web needs a nudge/);
  assert.equal(scopeEmptyAttention(false, null, { open: 0 }), 'No open item is waiting right now.');
});

/* ------------------------------------------------------- F1 trend error state */

test('F1: a failed trend fetch renders an error state, not a spinner', () => {
  const html = trendMarkup(null, { error: 'unknown query parameter(s): repo' });
  assert.match(html, /data-trend-error="true"/);
  assert.match(html, /role="alert"/);
  assert.match(html, /The trend did not load/);
  assert.match(html, /unknown query parameter\(s\): repo/);
  assert.match(html, /data-trend-retry/);
  assert.doesNotMatch(html, /Loading the trend…/);
  assert.equal(trendErrorMarkup('').includes('data-trend-error'), true);
  // and the banner no longer claims a repo-scoped Trend is unfiltered
  const banner = scopeBanner({ kind: 'repo', value: 'DEL/delivery-web' }, 'trend');
  assert.match(banner, /this view \(trend\) is filtered to it/);
  assert.doesNotMatch(banner, /this view \(trend\) is not filtered/);
});

/* ------------------------------------------------ F6/F7/F16 Delivery surfaces */

test('F6: the Markdown export title names the active tab', () => {
  const rows = [row()];
  const md = deliveryMarkdown(rows, 'x', '', {
    tab: 'blocked', counts: { all: 8 },
  });
  assert.match(md, /^# Delivery list — Blocked: 1 of 8/);
  const all = deliveryMarkdown(rows, 'x', '', { tab: 'all', counts: { all: 1 } });
  assert.match(all, /^# Delivery list — 1 open item/);
  const oldest = deliveryMarkdown(rows, 'x', '', {
    tab: 'oldest', counts: { all: 8 },
  });
  assert.match(oldest, /^# Delivery list — Oldest waiting: 1 of 8/);
});

test('F7: the scoped Delivery counts come from the scoped rows', () => {
  const web = row();
  const api = row({
    object_id: 'PR:DEL/api#78', number: 78, repo: 'DEL/api', repo_short: 'api',
    stale: true, unobserved: true, waiting_on: ['DaKo'],
  });
  const data = delivery([web, api]);
  const scope = { kind: 'person', value: 'FrMo', label: 'FrMo' };
  const scoped = scopedDeliveryRows(data, scope);
  assert.equal(scoped.length, 1);
  assert.equal(scoped.filter((r) => r.stale).length, 1);
  // the source builds the honesty line from this set, not from data.counts
  const main = read('main.js');
  assert.match(main, /const scopedRows = scopedDeliveryRows\(data, deliveryScope\(\)\)/);
  assert.match(main, /\$\{stale\} stale\$\{unobservedNote\}/);
  assert.doesNotMatch(main, /data\.counts\.unobserved \|\| 0\} unobserved/);
});

test('F16: the snapshot age is stated once, and "unobserved" drops when it is all rows', () => {
  const main = read('main.js');
  // the Delivery assumption line carries the timestamp but not the age
  const line = main.slice(main.indexOf("$('delivery-assumption').textContent"));
  assert.match(line.slice(0, 700), /snapshot \$\{fmtStamp\(Date\.parse\(data\.snapshot\)\)\}/);
  assert.doesNotMatch(line.slice(0, 700), /snapshotAge\(\)/);
  // the honesty line suppresses "N unobserved" when N == every scoped row
  assert.match(main, /unobserved < scopedRows\.length/);
});

/* ---------------------------------------------------- F8/V-A5 trend min-N */

test('F8/V-A5: the trend headline respects the minimum cohort and marks censoring', () => {
  const payload = (summary) => ({
    metric: 'cycle_median', label: 'Cycle time (median)', unit: 'hours',
    bucket: 'week', points: [
      { label: '9 Nov', value: 49, n: 3, in_progress: false, censored: 5 },
    ],
    summary,
  });
  const censored = trendStats(payload({
    latest: 49, latest_label: '9 Nov', latest_n: 3, min_n: 5,
    measured_buckets: 14, buckets: 14, change: 2,
    latest_censored: true, latest_censored_n: 5, in_progress: [],
  }));
  assert.match(censored, /right-censored/);
  assert.match(censored, /5 from that bucket still open/);
  // a floor breach is never a headline: say the floor and the skipped bucket
  const low = trendStats(payload({
    latest: null, latest_label: null, latest_n: null, min_n: 5,
    measured_buckets: 14, buckets: 14, change: null, in_progress: [],
    below_min: [{ label: '9 Nov', n: 3 }],
  }));
  assert.match(low, /no bucket meets the n≥5 floor/);
  assert.match(low, /the latest measured bucket, 9 Nov, has n=3/);
  assert.doesNotMatch(low, /<b>49<\/b> latest/);
  // a skipped newer bucket is named, not hidden
  const skipped = trendStats(payload({
    latest: 31, latest_label: '2 Nov', latest_n: 7, min_n: 5,
    measured_buckets: 14, buckets: 14, change: -1,
    low_n_after_headline: ['9 Nov'], in_progress: [],
  }));
  assert.match(skipped, /1 newer bucket below n≥5 \(9 Nov\)/);
});

/* ------------------------------------------------------- F28 rules surfaces */

test('F28: the Markdown grammar pluralises the excluded cohort', () => {
  const md = rulesMarkdown({
    level: 'team',
    sample: { prs: 7, days: 90, stories: 0, threads: 0 },
    dataset: { prs: 138, prs_outside_window: 7, days: 103 },
    hours_mode: 'business', as_of: '2026-11-18T00:10:44Z',
    suggestions: [], metrics: {}, rework: null, note: '',
  }, '');
  assert.match(md, /7 PRs opened over 90 calendar days of a 103-day dataset/);
  assert.match(md, /7 PRs opened before the window are not in the cohort/);
  assert.doesNotMatch(md, /and is not in the cohort/);
  // a single excluded PR reads "is not in the cohort"
  const one = rulesMarkdown({
    level: 'team',
    sample: { prs: 1, days: 30, stories: 0, threads: 0 },
    dataset: { prs: 2, prs_outside_window: 1, days: 103 },
    hours_mode: 'business', as_of: '2026-11-18T00:10:44Z',
    suggestions: [], metrics: {}, rework: null, note: '',
  }, '');
  assert.match(one, /1 PR opened before the window is not in the cohort/);
  assert.match(one, /1 PR opened over 30 calendar days/);
});

test('F28: the people toggle filters, and says so when nothing is person-scoped', () => {
  const teamRule = {
    id: 'team-1', rank: 1, severity: 'warning', metric: 'stale_prs',
    title: 'Team rule', suggestion: 'Do the team thing.', evidence: {},
    impact_hours: 1, confidence: 0.5, scope: { kind: 'team' },
  };
  const personRule = {
    id: 'person-1', rank: 2, severity: 'warning', metric: 'reviewer_responsiveness',
    title: 'Person rule', suggestion: 'Do the person thing.', evidence: {},
    impact_hours: 1, confidence: 0.5, scope: { kind: 'person', person: 'FrMo' },
  };
  const people = rulesMarkup({
    level: 'person', suggestions: [teamRule, personRule], metrics: {},
  });
  assert.match(people, /Person rule/);
  assert.doesNotMatch(people, /Team rule/);
  const empty = rulesMarkup({ level: 'person', suggestions: [teamRule], metrics: {} });
  assert.doesNotMatch(empty, /Team rule/);
  assert.match(empty, /No person-level rule crosses its threshold/);
  assert.match(empty, /data-rule-level="team"/);
  assert.match(empty, /show the 1 team rule/);
  // the export follows the same filter
  const md = rulesMarkdown({
    level: 'person', suggestions: [teamRule], metrics: {}, sample: { prs: 1, days: 1 },
    dataset: {}, hours_mode: 'business', as_of: '2026-01-01T00:00:00Z', note: '',
  }, '');
  assert.match(md, /1 team rule is on the team tab/);
  assert.doesNotMatch(md, /## 1\. Team rule/);
});

/* ------------------------------------------------------- V-A10 / V-B prose */

test('V-A10: the review-round card is a correlation, labelled as one', () => {
  const html = rulesMarkup({
    level: 'team',
    suggestions: [{
      id: 'round-cap', rank: 4, severity: 'warning', metric: 'rework_loops',
      title: 'Review rounds track cycle time (r = 0.62)',
      suggestion: 'Cap review rounds at 2.',
      evidence: { relationship: 'correlation', rounds_cycle_correlation: 0.62 },
      impact_hours: 3, confidence: 0.5, scope: { kind: 'team' },
    }],
    metrics: {},
  });
  assert.match(html, /data-relationship="correlation"/);
  assert.match(html, /not proof that one causes the other/);
});

test('V-B: rendered prose uses human labels and one duration language', () => {
  assert.equal(segmentLabel('created_ready'), 'created → ready');
  assert.equal(evidenceValue('segment_key', 'created_ready'), 'created → ready');
  assert.equal(metricLabel('pr_size'), 'PR size');
  assert.equal(metricLabel('worklog_cross_check'), 'worklog');
  // a long wall-clock span is whole calendar days, not "802 calendar days 1 h"
  assert.equal(fmtCalendarDuration(802 * DAY + HOUR), '802 calendar days');
  assert.equal(fmtCalendarDuration(3 * DAY + 12 * HOUR), '3 calendar days 12 h');
  // the rendered source carries no docs references in payload prose
  const trend = read('trend.js');
  assert.doesNotMatch(trend, /concepts\.md|rules\.flow_efficiency|rules\.cycle_time/);
});

/* --------------------------------------------------------- the regressions */

test('regression: the drawer reads the approvals the API carries', () => {
  assert.equal(approvalsLabel({ approved: ['DaKo'], reviewers_approved: 1 }), 'DaKo');
  assert.equal(
    approvalsLabel({ reviewers_approved: 1, reviewers_total: 2 }),
    '1 approval (the store holds the count, not the names)',
  );
  assert.equal(approvalsLabel({ reviewers_approved: 0 }), 'none');
  assert.equal(approvalsLabel(null), 'none');
  const main = read('main.js');
  assert.doesNotMatch(main, /\(row\.approved \|\| \[\]\)/);
  assert.doesNotMatch(main, /\(pr\.approved \|\| \[\]\)/);
});

test('regression: plural commits, the topbar and the sticky header (CSS guards)', () => {
  const css = read('style.css');
  // the top bar never wraps: one line, the range truncates instead
  assert.match(css, /\.topbar \{[\s\S]{0,160}flex-wrap: nowrap/);
  assert.match(css, /#range \{[\s\S]{0,200}text-overflow: ellipsis/);
  assert.match(css, /\.tenant-pill \.tenant-name, \.tenant-pill \.tenant-meta \{\s*white-space: nowrap/);
  // the delivery header sticks at its own scroll container's top, not 96 px in
  assert.match(css, /#delivery-table \{[\s\S]{0,160}overflow: auto/);
  assert.match(css, /#delivery-table \.table th \{ top: 0; \}/);
  // F23: the policy duration cell and the ticket cycle/push cells are wide enough
  assert.match(css, /\.table\.compact\.policy-table \.c-dur \{ width: 120px/);
  assert.match(css, /\.ticket-prs \.c-dur \{ width: 132px; white-space: normal/);
  assert.match(css, /\.ticket-prs \.c-push \{ width: 160px; white-space: normal/);
  // F25: the carry-over links are 44 px tap targets on a phone
  assert.match(css, /\.sprint-carry-table td a[\s\S]{0,200}min-height: 44px/);
});

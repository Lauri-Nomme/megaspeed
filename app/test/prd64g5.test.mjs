/* prd/64 G5 — Rules correctness & polish (F-05, F-06a, F-18, F-19).
 *
 * Each test fails without its fix:
 *
 * - F-05 `rulesStats()` counted the team rules behind an empty `people` tab and
 *   the empty state printed its sentence twice.
 * - F-06a `ruleItems()` re-sorted by impact only, so the rows the rule is
 *   *about* (`affected`) could sit below unaffected context rows.
 * - F-18 `evidenceValue()` printed raw floats for `hours` and
 *   `hours_per_extra_round`.
 * - F-19 the Markdown export said "counted in #7" on rule #7 itself.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  evidenceValue, ruleItems, ruleRow, rulesMarkdown, rulesMarkup, rulesStats,
} from '../rules.js';

function teamRule(id, overrides = {}) {
  return {
    id, rank: 1, severity: 'warning', metric: 'survival',
    title: `${id} title`, suggestion: `${id} rule`, impact_hours: 160,
    confidence: 0.5, evidence: { prs: 4, median_hours: 60 },
    scope: { kind: 'team' }, ...overrides,
  };
}

function personRule(id, person, overrides = {}) {
  return teamRule(id, {
    id, metric: 'reviewer_responsiveness', impact_hours: 40, severity: 'info',
    scope: { kind: 'person', person }, ...overrides,
  });
}

/* ---------------------------------------------------------------- F-05 ---- */

test('F-05 the people tab stats count the person rules, not the team rules', () => {
  const data = {
    level: 'person',
    sample: { prs: 12, days: 90 },
    dataset: { prs: 14, days: 100 },
    // the team-wide pool: what the strip used to print over an empty person list
    at_stake: { hours: 2600, overlap_hours: 900, pools: [] },
    suggestions: [
      personRule('reviewer-AaAa', 'AaAa', {
        at_stake: { pool: 'pr_wait', hours: 40, attributed_hours: 30, pool_rules: 3 },
      }),
      personRule('reviewer-BbBb', 'BbBb', {
        at_stake: { pool: 'pr_wait', hours: 40, attributed_hours: 20, pool_rules: 3 },
      }),
      teamRule('flow-efficiency', { rank: 2, severity: 'critical' }),
      teamRule('round-cap', { rank: 3 }),
    ],
  };
  const stats = rulesStats(data);
  assert.match(stats, /<b>2<\/b> person rules/);
  assert.match(stats, /<span class="muted">\(2 team\)<\/span>/);
  assert.doesNotMatch(stats, /<b>4<\/b> rules/);
  // the team pool's ceiling is not the person tab's number
  assert.doesNotMatch(stats, /2\.6k h/);
  assert.match(stats, /<b class="">0<\/b> critical/);       // both person rules are info
  assert.match(stats, /≈6 d 2 h<\/b> at stake/);    // 30 + 20 attributed hours
});

test('F-05 an empty people tab says 0 person rules (N team), never the team numbers', () => {
  const data = {
    level: 'person',
    sample: { prs: 124, days: 90 },
    dataset: { prs: 131, days: 103.6 },
    at_stake: { hours: 2600, overlap_hours: 900, pools: [] },
    suggestions: [
      teamRule('flow-efficiency', { rank: 1, severity: 'critical' }),
      teamRule('round-cap', { rank: 2 }),
      teamRule('jira-worklog-mismatch', { rank: 3, impact_hours: 0 }),
    ],
  };
  const stats = rulesStats(data);
  assert.match(stats, /<b>0<\/b> person rules/);
  assert.match(stats, /\(3 team\)/);
  assert.match(stats, /<b class="">0<\/b> critical/);
  assert.match(stats, /≈0 min<\/b> at stake/);
  assert.doesNotMatch(stats, /2\.6k h/);
  assert.doesNotMatch(stats, /counted once/);
});

test('F-05 the empty person state prints its sentence once', () => {
  const data = {
    level: 'person',
    sample: { prs: 10, days: 30 }, dataset: { prs: 10, days: 30 },
    at_stake: { hours: 0, overlap_hours: 0, pools: [] },
    metrics: {},
    suggestions: [teamRule('flow-efficiency'), teamRule('round-cap')],
  };
  const html = rulesMarkup(data);
  const hits = html.match(/No person-level rule crosses its threshold in this window/g) || [];
  assert.equal(hits.length, 1);
  assert.match(html, /show the 2 team rules →/);
});

/* -------------------------------------------------------------- F-06a ---- */

test('F-06a cycle-time-censoring leads with its affected open PRs', () => {
  const suggestion = teamRule('cycle-time-censoring', {
    rank: 5, metric: 'survival', title: '3 open PRs already waited past the target',
    evidence: { open_over_target: 3, target_hours: 72 },
  });
  const metric = {
    rows: [
      // unaffected, but the oldest waits: impact alone put these first
      { kind: 'pr', id: 'web#8', tickets: ['DEL-8'], hours: 448, affected: false,
        censored: true, declined: true },
      { kind: 'pr', id: 'web#18', tickets: ['DEL-18'], hours: 346, affected: false,
        censored: true, declined: true },
      { kind: 'pr', id: 'web#53', tickets: ['DEL-53'], hours: 128, affected: true,
        censored: true },
      { kind: 'pr', id: 'web#55', tickets: ['DEL-55'], hours: 104, affected: true,
        censored: true },
      { kind: 'pr', id: 'web#60', tickets: ['DEL-60'], hours: 96, affected: true,
        censored: true },
    ],
  };
  const items = ruleItems(suggestion, { survival: metric });
  assert.equal(items.length, 5);
  assert.deepEqual(items.slice(0, 3).map((item) => item.label),
    ['web#53', 'web#55', 'web#60']);
  assert.ok(items.slice(0, 3).every((item) => item.affected));
  // within the affected block the worst wait still leads
  assert.deepEqual(items.slice(3).map((item) => item.label), ['web#8', 'web#18']);
});

test('F-06a jira-worklog-mismatch leads with its 4 affected stories', () => {
  const suggestion = teamRule('jira-worklog-mismatch', {
    rank: 7, metric: 'worklog_cross_check', impact_hours: 0,
    title: "Logged hours don't track story points",
    evidence: { points_logged_correlation: -0.072 },
  });
  const story = (key, hours, affected) => ({
    kind: 'story', key, tickets: [key], logged_hours: hours,
    worklogs: 2, late_worklogs: 0, affected,
  });
  const metric = {
    rows: [
      story('DEL-100', 120, false),
      story('DEL-167', 48, true),
      story('DEL-101', 100, false),
      story('DEL-147', 40, true),
      story('DEL-102', 90, false),
      story('DEL-187', 36, true),
      story('DEL-103', 80, false),
      story('DEL-199', 30, true),
    ],
  };
  const items = ruleItems(suggestion, { worklog_cross_check: metric });
  assert.deepEqual(items.slice(0, 4).map((item) => item.label),
    ['DEL-167', 'DEL-147', 'DEL-187', 'DEL-199']);
  assert.ok(items.slice(0, 4).every((item) => item.affected));
});

test('F-06a flow-efficiency leads with its affected PRs', () => {
  const suggestion = teamRule('flow-efficiency', {
    rank: 4, metric: 'flow_efficiency', evidence: { target_efficiency: 0.25 },
  });
  const pr = (id, elapsed, active, affected) => ({
    kind: 'pr', id, tickets: [`DEL-${id.split('#')[1]}`],
    elapsed_hours: elapsed, active_hours: active,
    efficiency: active / elapsed, affected,
  });
  const metric = {
    prs: [
      pr('web#1', 200, 100, false),     // 50 % efficient, the longest wait
      pr('web#2', 40, 2, true),         // 5 % efficient — the rule's evidence
      pr('web#3', 150, 90, false),
      pr('web#4', 30, 1, true),
    ],
  };
  const items = ruleItems(suggestion, { flow_efficiency: metric });
  assert.deepEqual(items.map((item) => [item.label, item.affected]),
    [['web#2', true], ['web#4', true], ['web#1', false], ['web#3', false]]);
});

test('F-06a the drill summary counts the same affected rows it lists first', () => {
  const suggestion = teamRule('cycle-time-censoring', {
    rank: 5, metric: 'survival', evidence: { open_over_target: 2 },
  });
  const data = {
    level: 'team', sample: { prs: 3, days: 90 }, dataset: { prs: 3, days: 90 },
    as_of: '2026-11-18T00:10:44Z', note: '',
    at_stake: { hours: 0, overlap_hours: 0, pools: [] },
    suggestions: [suggestion],
    metrics: {
      survival: {
        rows: [
          { kind: 'pr', id: 'web#1', tickets: ['DEL-1'], hours: 300, affected: false },
          { kind: 'pr', id: 'web#2', tickets: ['DEL-2'], hours: 100, affected: true },
          { kind: 'pr', id: 'web#3', tickets: ['DEL-3'], hours: 90, affected: true },
        ],
      },
    },
  };
  const html = rulesMarkup(data);
  assert.match(html, /<span>3 items<\/span>/);
  assert.match(html, /2 affected · worst wait first/);
  const affectedRows = html.match(/<tr class="affected"/g) || [];
  assert.equal(affectedRows.length, 2);
  // the first item row in the drill-down is an affected one
  const firstRow = html.slice(html.indexOf('<tbody>'));
  assert.match(firstRow, /data-drill-item="web#2"/);
  assert.ok(firstRow.indexOf('data-drill-item="web#2"')
    < firstRow.indexOf('data-drill-item="web#1"'));
});

/* ---------------------------------------------------------------- F-18 ---- */

test('F-18 hours evidence chips are durations, never raw floats', () => {
  // rule 04: "per extra round: ≈6 h 1 min", not "6.01"
  assert.equal(evidenceValue('hours_per_extra_round', 6.01), '≈6 h 1 min');
  // rule 10: "hours: ≈35 d 3 h", not "283.6"
  assert.equal(evidenceValue('hours', 283.6), '≈35 d 3 h');
  // the guard covers every hours-shaped key, including a new one
  for (const key of [
    'hours', 'hours_per_round', 'hours_per_refinement', 'hours_per_extra_round',
    'median_wait_hours', 'total_hours', 'hours_to_merge', 'session_hours',
  ]) {
    const text = evidenceValue(key, 6.01);
    assert.doesNotMatch(text, /^-?\d+\.\d+$/, `${key} printed a bare float: ${text}`);
    assert.match(text, /≈/, `${key} is a duration and takes the shared unit`);
  }
});

test('F-18 rules 04 and 10 render their chips in the one duration language', () => {
  const roundCap = ruleRow(teamRule('round-cap', {
    rank: 4, metric: 'rework_loops', title: 'Review rounds track cycle time',
    evidence: { prs_over_target: 24, round_cap: 2, hours_per_extra_round: 6.01 },
  }));
  assert.match(roundCap, /<dt>per extra round<\/dt>\s*<dd class="mono">≈6 h 1 min<\/dd>/);
  assert.doesNotMatch(roundCap, />6\.01</);

  const sentinelTail = ruleRow(teamRule('sentinel-tail', {
    rank: 10, metric: 'sentinel_tail', title: 'The sentinel is the only holder',
    evidence: { hours: 283.6, excess: 233.6, items: 3 },
  }));
  assert.match(sentinelTail, /<dt>hours<\/dt>\s*<dd class="mono">≈35 d 3 h<\/dd>/);
  assert.doesNotMatch(sentinelTail, />283\.6</);
});

/* ---------------------------------------------------------------- F-19 ---- */

test('F-19 the Markdown export never tells rule #7 it is counted in #7', () => {
  const data = {
    level: 'team', sample: { prs: 85, days: 90 }, dataset: { prs: 85, days: 90 },
    as_of: '2026-11-18T00:10:44Z', note: '', metrics: {},
    at_stake: { hours: 160, overlap_hours: 0, pools: [{ pool: 'worklog' }] },
    suggestions: [
      teamRule('jira-worklog-mismatch', {
        rank: 7, metric: 'worklog_cross_check', impact_hours: 0, confidence: 1,
        title: "Logged hours don't track story points (r = -0.072)",
        evidence: { points_logged_correlation: -0.072 },
        // the API used to stamp this on the zero-hour rule itself
        at_stake: {
          pool: 'worklog', hours: 0, attributed_hours: 0, pool_hours: 0,
          pool_rules: 1, attributed_to: 'jira-worklog-mismatch', attributed_to_rank: 7,
        },
      }),
    ],
  };
  const markdown = rulesMarkdown(data);
  assert.match(markdown, /^## 7\./m);
  assert.doesNotMatch(markdown, /counted in #7/);
  assert.match(markdown, /- impact: ≈0 min \(confidence 1\)/);
});

test('F-19 a genuine shared-pool note still travels in the export', () => {
  const data = {
    level: 'team', sample: { prs: 10, days: 30 }, dataset: { prs: 10, days: 30 },
    as_of: '2026-11-18T00:10:44Z', note: '', metrics: {},
    at_stake: { hours: 160, overlap_hours: 40, pools: [{ pool: 'pr_wait' }] },
    suggestions: [
      teamRule('flow-efficiency', {
        rank: 1, impact_hours: 160,
        at_stake: {
          pool: 'pr_wait', hours: 160, attributed_hours: 60, pool_hours: 160,
          pool_rules: 2, attributed_to: null,
        },
      }),
      teamRule('round-cap', {
        rank: 4, metric: 'rework_loops', impact_hours: 40,
        at_stake: {
          pool: 'pr_wait', hours: 40, attributed_hours: 0, pool_hours: 160,
          pool_rules: 2, attributed_to: 'flow-efficiency', attributed_to_rank: 1,
        },
      }),
    ],
  };
  const markdown = rulesMarkdown(data);
  assert.match(markdown, /counted in #1/);
  // ...and a self-reference is suppressed even if the API ever sends one
  const selfRef = rulesMarkdown({
    ...data,
    suggestions: [{
      ...data.suggestions[0],
      at_stake: { ...data.suggestions[0].at_stake, attributed_to: 'flow-efficiency',
        attributed_to_rank: 1 },
    }],
  });
  assert.doesNotMatch(selfRef, /counted in #1/);
});

/* The Rules page's evidence drill-down (prd/26 P5).
 *
 * A suggestion's evidence is a number ("16 PRs past 3 rounds"); the drill-down
 * is the *items* behind it. These tests pin the pure extractor (`ruleItems`)
 * and the rendered markup: the whole list, affected first, each item a link to
 * its ticket page, and a stated cap when the list is longer than the render.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { DRILL_LIMIT, metricRows, ruleItems, rulesMarkdown, rulesMarkup } from '../rules.js';

const REVIEW_METRIC = {
  rows: [
    {
      kind: 'pr', id: 'web#2', object_id: 'PR:DEL/web#2', number: 2,
      tickets: ['DEL-167'], reviewer: 'RrRr', first_review_hours: 100,
      over_target: true, affected: true,
    },
    {
      kind: 'pr', id: 'web#1', object_id: 'PR:DEL/web#1', number: 1,
      tickets: ['DEL-160'], reviewer: 'AaAa', first_review_hours: 10,
      over_target: false, affected: false,
    },
    {
      kind: 'pr', id: 'web#3', object_id: 'PR:DEL/web#3', number: 3,
      tickets: ['DEL-176'], reviewer: null, first_review_hours: null,
      missing_review: true, affected: false,
    },
  ],
};

function suggestion(overrides = {}) {
  return {
    id: 'review-sla', rank: 1, severity: 'warning', metric: 'time_to_first_review',
    title: 'Time to first review is 40 h (target < 24 h)',
    suggestion: 'Adopt a review SLA.', impact_hours: 48, confidence: 0.15,
    evidence: { prs: 3, over_target: 1, median_hours: 40, target_hours: 24 },
    scope: { kind: 'team' },
    ...overrides,
  };
}

test('the whole item list is the evidence, affected first', () => {
  const items = ruleItems(suggestion(), { time_to_first_review: REVIEW_METRIC });
  assert.equal(items.length, 3);
  assert.deepEqual(items.map((item) => item.label), ['web#2', 'web#1', 'web#3']);
  assert.equal(items[0].affected, true);
  assert.equal(items[0].value, '4 d 4 h');       // 100 h
  assert.equal(items[0].detail, 'review by RrRr');
  assert.equal(items[0].href, '#ticket/DEL-167');
  assert.equal(items[2].value, 'no review yet');
});

test('a PR links the ticket that owns it', () => {
  const items = ruleItems(suggestion(), { time_to_first_review: REVIEW_METRIC });
  assert.equal(items[0].key, 'DEL-167');
  assert.equal(items[0].kind, 'pr');
  assert.equal(items[0].href, '#ticket/DEL-167');
});

test('flow_efficiency still reads its per-PR list from prs', () => {
  const metric = { prs: [{ id: 'web#9', tickets: ['DEL-9'], efficiency: 0.5, elapsed_hours: 10, active_hours: 5, affected: true }] };
  assert.equal(metricRows(metric).length, 1);
  const items = ruleItems(
    suggestion({ metric: 'flow_efficiency', evidence: {} }),
    { flow_efficiency: metric },
  );
  assert.equal(items[0].value, '50%');
  assert.match(items[0].detail, /elapsed/);
});

test('a person-scoped rule drills into that person only', () => {
  const metric = {
    rows: [
      { kind: 'pr', id: 'web#1', tickets: ['DEL-1'], person: 'XxXx', hours: 55, waits: 2, affected: true },
      { kind: 'pr', id: 'web#2', tickets: ['DEL-2'], person: 'YyYy', hours: 30, waits: 1, affected: true },
    ],
  };
  const items = ruleItems(
    suggestion({
      id: 'reviewer-XxXx', metric: 'reviewer_responsiveness',
      scope: { kind: 'person', person: 'XxXx' }, evidence: {},
    }),
    { reviewer_responsiveness: metric },
  );
  assert.equal(items.length, 1);
  assert.equal(items[0].label, 'web#1');
  assert.match(items[0].detail, /XxXx/);
});

test('the markup expands to a dense table of links, not a paragraph', () => {
  const data = {
    level: 'team',
    sample: { prs: 3, days: 30 },
    hours_mode: 'business',
    as_of: '2026-11-17T00:00:00Z',
    note: '',
    metrics: { time_to_first_review: REVIEW_METRIC },
    suggestions: [suggestion()],
  };
  const html = rulesMarkup(data, { base: '' });
  assert.match(html, /<details class="rule-drill" data-rule-drill="review-sla">/);
  assert.match(html, /3 items/);
  assert.match(html, /1 affected/);
  assert.match(html, /data-drill-item="web#2"/);
  assert.match(html, /class="affected"/);
  assert.match(html, /href="#ticket\/DEL-167"/);
  assert.match(html, /data-ticket="DEL-167"/);
  assert.match(html, /data-drill-item="web#3"/);
  // every item is rendered, not only the affected ones
  assert.equal((html.match(/data-drill-item=/g) || []).length, 3);
});

test('a long list states the cap and its cost', () => {
  const rows = Array.from({ length: DRILL_LIMIT + 5 }, (_, index) => ({
    kind: 'pr', id: `web#${index}`, tickets: [`DEL-${index}`],
    hours: 10, affected: false,
  }));
  const data = {
    level: 'team', sample: { prs: 1, days: 1 }, hours_mode: 'wall-clock',
    as_of: '2026-11-17T00:00:00Z', note: '',
    metrics: { rework_loops: { rows } },
    suggestions: [suggestion({ id: 'rework-loops', metric: 'rework_loops', evidence: {} })],
  };
  const html = rulesMarkup(data, { base: '' });
  assert.equal((html.match(/data-drill-item=/g) || []).length, DRILL_LIMIT);
  assert.match(html, new RegExp(`showing the first ${DRILL_LIMIT} of ${DRILL_LIMIT + 5}`));
  assert.match(html, /metrics\.rework_loops\.rows/);
});

test('a metric with no rows renders no drill-down at all', () => {
  const data = {
    level: 'team', sample: { prs: 1, days: 1 }, hours_mode: 'wall-clock',
    as_of: '2026-11-17T00:00:00Z', note: '', metrics: {},
    suggestions: [suggestion({ id: 'review-tail', metric: 'reviewer_responsiveness', evidence: {} })],
  };
  const html = rulesMarkup(data, { base: '' });
  assert.doesNotMatch(html, /rule-drill/);
});

test('the Markdown export carries the items as links', () => {
  const data = {
    level: 'team', sample: { prs: 3, days: 30 }, hours_mode: 'business',
    as_of: '2026-11-17T00:00:00Z', note: '',
    metrics: { time_to_first_review: REVIEW_METRIC },
    suggestions: [suggestion()],
  };
  const markdown = rulesMarkdown(data, 'http://x/');
  assert.match(markdown, /- items \(3\): \[web#2\]\(http:\/\/x\/#ticket\/DEL-167\), \[web#1\]/);
  assert.match(markdown, /\[web#3\]\(http:\/\/x\/#ticket\/DEL-176\)/);
});

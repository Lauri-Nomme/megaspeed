/* prd/65 group C (N-09) — the censoring sentence prints exactly once.
 *
 * The structured counts (`censored_before`/`censored_after`, and their sum
 * `censored`) are the source of truth; `note` is the verdict sentence only.
 * The panel prints the detailed disclosure below the table, and an older
 * payload that still carries the suffix in `note` is de-duplicated there.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  beforeAfterMarkdown, beforeAfterPanel, censoringSentence, verdictSentence,
} from '../intervention.js';

// Contiguous in both the full sentence and the old `note` suffix.
const DISCLOSURE = 'are excluded and counted: a running age is not a completed duration.';

function compare(overrides = {}) {
  return {
    ok: true,
    metric: 'cycle_median',
    label: 'PR cycle',
    unit: 'hours',
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
    censored_before: 7,
    censored_after: 5,
    censored: 12,
    buckets_before: 8,
    buckets_after: 6,
    annotation: { id: 'a1', date: '2026-10-08', label: 'review SLA 4 h', note: null },
    note: 'No clear change: the interval is tight around zero (inside ±20 % of the before median).',
    ...overrides,
  };
}

const occurrences = (text, needle) => text.split(needle).length - 1;

test('censored items on both sides print the sentence once, below the table', () => {
  const html = beforeAfterPanel(compare(), { annotations: [] });
  assert.equal(occurrences(html, DISCLOSURE), 1);
  assert.match(
    html,
    /<p class="panel-note censored" data-censored="12">7 open item\(s\) before and 5 after are excluded and counted/,
  );
  // the top verdict keeps only the outcome, never the censoring copy
  const top = html.match(/<p class="verdict-sentence">([^<]*)<\/p>/)?.[1] || '';
  assert.doesNotMatch(top, /excluded and counted/);
  assert.match(top, /No clear change/);
});

test('a legacy note suffix is stripped, never printed twice', () => {
  const legacy = compare({
    note: 'No clear change: the interval is tight around zero (inside ±20 % of the before median). '
      + `12 open item(s) ${DISCLOSURE}`,
  });
  assert.equal(
    verdictSentence(legacy),
    'No clear change: the interval is tight around zero (inside ±20 % of the before median).',
  );
  const html = beforeAfterPanel(legacy, { annotations: [] });
  assert.equal(occurrences(html, DISCLOSURE), 1);
});

test('structured counts stay available and the Markdown export names them once', () => {
  const data = compare();
  assert.equal(data.censored_before, 7);
  assert.equal(data.censored_after, 5);
  assert.equal(data.censored, 12);
  assert.equal(
    censoringSentence(data),
    '7 open item(s) before and 5 after are excluded and counted: a running age is not a completed duration.',
  );
  const text = beforeAfterMarkdown(data);
  assert.equal(occurrences(text, DISCLOSURE), 1);
  assert.match(text, /7 open item\(s\) before and 5 after/);
});

test('the no-censoring case renders no censoring line and keeps the verdict', () => {
  const clean = compare({ censored_before: 0, censored_after: 0, censored: 0 });
  const html = beforeAfterPanel(clean, { annotations: [] });
  assert.equal(occurrences(html, DISCLOSURE), 0);
  assert.doesNotMatch(html, /panel-note censored/);
  assert.match(html, /No clear change/);
  assert.equal(censoringSentence(clean), '');
});

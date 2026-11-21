/* prd/65 N-10 — the Policy lab quotes the same primary guardrail as Rules.
 *
 * `labGuardrail()` used to hardcode `rounds_mean`, so `cap PRs at 3 commits`
 * (`size3`) printed a flat `rounds 1.5 → 1.5` in the lab while Rules quoted the
 * worsening packaging metric `PRs per 100 commits 20.2 → 42.2` — the entry the
 * payload flags `worse === true`, and the one `advice.GUARDRAIL_PRIMARY["size3"]`
 * selects. These tests pin the selection rule (worsening entry → rounds mean →
 * first modelled fact), the warning tint on the worsening pair, and that a
 * preset whose primary is rounds is unchanged.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { guardrailCell, labGuardrail, policyMarkdown, abMarkup } from '../policy.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

/** The `size3` guardrail block, as the del-combined store reports it (QA N-10). */
const SIZE3_GUARDRAILS = {
  modelled: [
    { id: 'rounds_mean', label: 'rounds', baseline: 1.5, scenario: 1.5,
      delta: 0, better: 'lower', worse: false },
    { id: 'rounds_p85', label: 'rounds p85', baseline: 3, scenario: 3,
      delta: 0, better: 'lower', worse: false },
    { id: 'prs_per_100_commits', label: 'PRs per 100 commits',
      baseline: 20.2, scenario: 42.2, delta: 22, better: 'lower', worse: true,
      note: 'packaging, not throughput' },
    { id: 'escalated_share', label: 'reviews escalated', baseline: 0,
      scenario: 0, delta: 0, better: 'lower', worse: false },
    { id: 'declined_per_100', label: 'declined per 100 PRs', baseline: 12.9,
      scenario: 13.3, delta: 0.4, better: 'lower', worse: false },
  ],
  held_constant: [
    { id: 'reopen', label: 'reopened', baseline: 0.055 },
    { id: 'failed_verification', label: 'failed verification', baseline: 0.041 },
  ],
  not_modelled: [{ id: 'bug_share', reason: 'no issue type' }],
};

const compareWith = (modelled) => ({ guardrails: {
  modelled,
  held_constant: [{ id: 'reopen', label: 'reopened', baseline: 0.055 }],
} });

/** An A/B payload around a compare block, for the end-to-end table render. */
function abPayload(compare) {
  return {
    baseline: {
      ok: true,
      predicted: {
        cycle_hours: { median: 40, p90: 100 },
        lead_hours: { median: 50 },
        flow_efficiency: { aggregate: 0.3 },
        throughput_work_per_day: 5,
        throughput_per_day: 1,
        spread: { replications: 6 },
        work_unit: 'commits',
      },
      bottleneck: { person: 'AaAa', utilisation: 0.8 },
    },
    scenario: {
      predicted: {
        cycle_hours: { median: 34, p90: 90 },
        lead_hours: { median: 54 },
        flow_efficiency: { aggregate: 0.34 },
        throughput_work_per_day: 5.2,
        throughput_per_day: 1.3,
        work_unit: 'commits',
      },
      scenario: { name: 'scenario' },
      bottleneck: { person: 'AaAa', utilisation: 0.84 },
    },
    compare,
  };
}

/** The whole compare block, as the lab returns it for `size3`. */
function fullCompare() {
  return {
    cycle_median_hours: { baseline: 40, scenario: 34, percent: -15,
      direction: 'down', better: true },
    cycle_p90_hours: { baseline: 100, scenario: 90, percent: -10,
      direction: 'down', better: true },
    flow_efficiency: { baseline: 0.3, scenario: 0.34, percent: 13,
      direction: 'up', better: true },
    throughput_work_per_day: { baseline: 5, scenario: 5.2, percent: 4,
      direction: 'up', better: true },
    throughput_per_day: { baseline: 1, scenario: 1.3, percent: 30,
      direction: 'up', better: true },
    lead_hours: { baseline: 50, scenario: 54, percent: 8,
      direction: 'up', better: false },
    guardrails: SIZE3_GUARDRAILS,
    verdict: 'cycle 15.0 % shorter', verdict_tone: 'good',
  };
}

test('N-10: size3 leads with the worsening packaging metric, not the flat rounds mean', () => {
  const compare = { guardrails: SIZE3_GUARDRAILS };
  assert.equal(
    labGuardrail(compare),
    'guardrail: PRs per 100 commits 20.2 → 42.2 · reopen held at 5.5 %',
    'the lab must quote the same primary guardrail Rules quotes for size3',
  );
  // the observed pre-fix line is gone
  assert.doesNotMatch(labGuardrail(compare), /rounds 1\.5 → 1\.5/);
});

test('N-10: only the worsening baseline → scenario pair carries the warning tint', () => {
  const compare = { guardrails: SIZE3_GUARDRAILS };
  const html = guardrailCell(compare);
  assert.match(html, /data-policy-guardrail/);
  // the same class Rules uses for the tint — one colour, both surfaces
  assert.match(html, /<b class="guardrail-warn">20\.2 → 42\.2<\/b>/);
  // the held-constant half is never tinted
  assert.doesNotMatch(html, /guardrail-warn">5\.5/);
  assert.match(html, /reopen held at 5\.5 %/);
  // the tint is the class rules.js already styles; no lab-only colour
  assert.match(read('rules.js'), /class="guardrail-warn"/);
});

test('N-10: the A/B table under the cycle-time row shows the tinted metric', () => {
  const html = abMarkup(abPayload(fullCompare()));
  assert.match(html, /data-policy-guardrail/);
  assert.match(html,
    /PRs per 100 commits <b class="guardrail-warn">20\.2 → 42\.2<\/b>/);
});

test('N-10: a preset whose primary is rounds is unchanged', () => {
  // `cap review at 2 rounds`: rounds move, nothing worsens
  const compare = compareWith([
    { id: 'rounds_mean', label: 'rounds', baseline: 1.57, scenario: 0.95,
      delta: -0.62, better: 'lower', worse: false },
    { id: 'rounds_p85', label: 'rounds p85', baseline: 3, scenario: 1,
      delta: -2, better: 'lower', worse: false },
    { id: 'prs_per_100_commits', label: 'PRs per 100 commits',
      baseline: 20.1, scenario: 20.4, delta: 0.3, better: 'lower',
      worse: false },
  ]);
  assert.equal(
    labGuardrail(compare),
    'guardrail: rounds 1.57 → 0.95 · reopen held at 5.5 %',
  );
  assert.doesNotMatch(guardrailCell(compare), /guardrail-warn/);
});

test('N-10: with no worsening entry the rounds mean is the fallback, wherever it sits', () => {
  const compare = compareWith([
    { id: 'prs_per_100_commits', label: 'PRs per 100 commits',
      baseline: 20.1, scenario: 19.5, worse: false },
    { id: 'rounds_mean', label: 'rounds', baseline: 1.6, scenario: 0.8,
      worse: false },
    { id: 'rounds_p85', label: 'rounds p85', baseline: 3, scenario: 2 },
  ]);
  assert.equal(
    labGuardrail(compare),
    'guardrail: rounds 1.6 → 0.8 · reopen held at 5.5 %',
  );
});

test('N-10: without a rounds mean the first modelled fact is used', () => {
  const compare = compareWith([
    { id: 'escalated_share', label: 'reviews escalated', baseline: 0,
      scenario: 0.37, worse: false },
    { id: 'declined_per_100', label: 'declined per 100 PRs', baseline: 12.9,
      scenario: 13.3, worse: false },
  ]);
  assert.equal(
    labGuardrail(compare),
    'guardrail: reviews escalated 0 → 0.37 · reopen held at 5.5 %',
  );
  assert.doesNotMatch(guardrailCell(compare), /guardrail-warn/);
});

test('N-10: the markdown export quotes the same guardrail and carries no markup', () => {
  const data = abPayload(fullCompare());
  data.provenance = 'calibrated on 100 PRs';
  const markdown = policyMarkdown(data);
  assert.match(markdown,
    /_guardrail: PRs per 100 commits 20\.2 → 42\.2 · reopen held at 5\.5 %_/);
  assert.doesNotMatch(markdown, /guardrail-warn/);
});

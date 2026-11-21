/* prd/64 G2 — no-Jira honesty in the Policy lab (F-03).
 *
 * A store with no Jira layer has no measured `created → ready` segment, so the
 * API reports `calibration.refinement.hours: null` and marks the scenario
 * `scenario_available: false`. The page must render "not measurable (no Jira
 * layer)" and disable the refine knob/preset — it must never price the policy
 * at a default and show a delta.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  calibrationMarkup, defaultScenario, policyConfigMarkup, policyMarkdown,
  policyMarkup, refinementUnavailable,
} from '../policy.js';

function calibration(refinement) {
  return {
    ok: true,
    prs: { n: 131, merged: 106 },
    window: { business_days: 63.4, business_hours: 507.1 },
    arrival: { per_business_day: 2.067, mean_interarrival_hours: 3.87 },
    review_service: { pooled: { median: 2.4 } },
    sentinel: { observed: { mean_hours: 9.23 }, utilisation: 0.49 },
    rework: { probability: 0.49, push_class: { content: 0.834 } },
    pr_size: { commits: { median: 5 } },
    stack_depth: { available: false },
    queueing: { note: 'attention + queue + service' },
    ...(refinement === undefined ? {} : { refinement }),
  };
}

function baseline() {
  return {
    ok: true,
    predicted: {
      cycle_hours: { median: 22.43, p90: 36.04 },
      flow_efficiency: { aggregate: 0.15 },
      throughput_per_day: 1.74,
      throughput_work_per_day: 12.4,
      work_unit: 'commits',
      lead_hours: { median: 22.43 },
      spread: { replications: 6, median_min: 20.9, median_max: 23.1 },
    },
    bottleneck: { person: 'sentinel:1', utilisation: 0.48 },
    resources: [],
    warnings: [],
  };
}

function payload(overrides = {}) {
  return {
    ok: true,
    available: true,
    hours_mode: 'business',
    provenance: 'calibrated from 131 PRs, 2026-08-17→2026-11-16',
    seed: 20260817,
    calibration: calibration({
      hours: null, n: 0, basis: 'no measured created → ready segment in this cohort',
    }),
    baseline: baseline(),
    validation: {
      ok: true, status: 'validated', tolerance: 0.2,
      historical_median_hours: 24.77, predicted_median_hours: 22.43,
      percent_off: -9.4,
    },
    scenario: null,
    compare: null,
    ...overrides,
  };
}

const unmeasured = calibration({
  hours: null, n: 0, basis: 'no measured created → ready segment in this cohort',
});
const measured = calibration({
  hours: 26.8, n: 100, basis: 'median created → ready segment of this cohort',
});

test('prd/64 F-03: null refinement hours means the knob is unavailable', () => {
  assert.equal(refinementUnavailable(unmeasured), true);
  assert.equal(refinementUnavailable(measured), false);
  // no calibration in hand: the pure helper leaves the control alone
  assert.equal(refinementUnavailable(undefined), false);
});

test('the refine knob is disabled and says "not measurable (no Jira layer)"', () => {
  const markup = policyConfigMarkup(defaultScenario(), unmeasured);
  const refine = markup.match(/<input[^>]*data-knob="refine_before_start"[^>]*>/)[0];
  assert.match(refine, /disabled/);
  assert.match(refine, /aria-disabled="true"/);
  // a scenario serialised from an earlier state cannot smuggle it back in
  assert.doesNotMatch(refine, / checked/);
  assert.match(markup, /id="policy-knob-refine_before_start-note"[^>]*>not measurable \(no Jira layer\)/);
  // the other knobs stay usable: only refinement lacks a measured input
  const wip = markup.match(/<select[^>]*data-knob="wip_limit"[^>]*>/)[0];
  assert.doesNotMatch(wip, /disabled/);
});

test('the refine preset is disabled with the same reason', () => {
  const markup = policyConfigMarkup(defaultScenario(), unmeasured);
  const preset = markup.match(/<button[^>]*data-preset="refine"[^>]*>/)[0];
  assert.match(preset, /disabled/);
  assert.match(preset, /title="not measurable \(no Jira layer\)"/);
  const other = markup.match(/<button[^>]*data-preset="sla4"[^>]*>/)[0];
  assert.doesNotMatch(other, /disabled/);
});

test('a measured refinement keeps the knob enabled', () => {
  const markup = policyConfigMarkup(defaultScenario(), measured);
  const refine = markup.match(/<input[^>]*data-knob="refine_before_start"[^>]*>/)[0];
  assert.doesNotMatch(refine, /disabled/);
  assert.doesNotMatch(markup, /not measurable \(no Jira layer\)/);
});

test('without a calibration argument the standalone helper is unchanged', () => {
  const markup = policyConfigMarkup(defaultScenario());
  const refine = markup.match(/<input[^>]*data-knob="refine_before_start"[^>]*>/)[0];
  assert.doesNotMatch(refine, /disabled/);
});

test('the calibrated facts render "not measurable (no Jira layer)"', () => {
  assert.match(calibrationMarkup(unmeasured), /not measurable \(no Jira layer\)/);
  assert.match(calibrationMarkup(measured), /≈26\.8 h created → ready/);
});

test('an unavailable scenario is stated on the page, never shown as a delta', () => {
  const data = payload({
    scenario_available: false,
    scenario_unavailable_reason: 'no measured refinement segments',
    scenario_unavailable: {
      reason: 'no measured refinement segments',
      scenario: { refine_before_start: true },
    },
    note: 'the scenario was not run: no measured refinement segments',
  });
  const markup = policyMarkup(data, { ...defaultScenario(), refine_before_start: true });
  assert.match(markup, /id="policy-scenario-unavailable"/);
  assert.match(markup, /not measurable/);
  assert.match(markup, /no measured refinement segments/);
  assert.match(markup, /This scenario is not modelled, so no delta is shown/);
  // the knob is disabled because the calibration has no measured segment
  assert.match(
    markup.match(/<input[^>]*data-knob="refine_before_start"[^>]*>/)[0],
    /disabled/,
  );
});

test('Copy as Markdown states the unavailable scenario', () => {
  const data = payload({
    scenario_available: false,
    scenario_unavailable_reason: 'no measured refinement segments',
  });
  const markdown = policyMarkdown(data);
  assert.match(markdown, /not measurable: no measured refinement segments/);
  assert.match(markdown, /the scenario was not modelled/);
});

/* Policy lab (prd/23): the pure parts of the page.
 *
 * The contract this file pins is the feature's honesty: **every predicted
 * number carries `≈`**, the validation gate decides whether a scenario is
 * shown at all, and a delta smaller than the model's own spread is reported as
 * "no clear effect" rather than as a win. The page wiring (fetch/state/events)
 * is guarded source-level at the bottom.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  abMarkup, calibrationMarkup, defaultScenario, isBaselineScenario, KNOBS,
  policyConfigMarkup, policyMarkdown, policyMarkup, predHours, predPercent,
  predRate, provenanceLine, resourcesMarkup, scenarioQuery, validationMarkup,
} from '../policy.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

function calibration(overrides = {}) {
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
    ...overrides,
  };
}

function result(overrides = {}) {
  return {
    ok: true,
    predicted: {
      cycle_hours: { median: 22.43, p90: 36.04 },
      flow_efficiency: { aggregate: 0.15 },
      throughput_per_day: 1.74,
      lead_hours: { median: 22.43 },
      spread: { replications: 6, median_min: 20.9, median_max: 23.1 },
    },
    bottleneck: { person: 'sentinel:1', utilisation: 0.48 },
    resources: [
      { person: 'sentinel:1', role: 'sentinel', utilisation: 0.48,
        mean_wait_hours: 1.2, peak_queue: 2, requests: 110 },
    ],
    warnings: [],
    ...overrides,
  };
}

function payload(overrides = {}) {
  return {
    ok: true,
    available: true,
    hours_mode: 'business',
    provenance: 'calibrated from 131 PRs, 2026-08-17→2026-11-16',
    seed: 20260817,
    calibration: calibration(),
    baseline: result(),
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

// ------------------------------------------------------------------ numbers
test('every predicted number is marked ≈, never a bare value', () => {
  assert.equal(predHours(22.43), '≈22 h 26 min');
  assert.equal(predHours(null), '≈–');
  assert.equal(predPercent(0.15), '≈15%');
  assert.equal(predRate(1.74), '≈1.74 /d');
  for (const value of [predHours(1), predHours(0), predPercent(0), predRate(0)]) {
    assert.match(value, /^≈/);
  }
});

// ---------------------------------------------------------------- scenarios
test('the baseline scenario is recognised, so no scenario is sent', () => {
  assert.equal(isBaselineScenario(defaultScenario()), true);
  assert.equal(isBaselineScenario({ ...defaultScenario(), sentinels: 2 }), false);
  assert.equal(isBaselineScenario({ ...defaultScenario(), review_first: true }), false);
  assert.equal(scenarioQuery(defaultScenario()), '');
  const query = new URLSearchParams(scenarioQuery({ ...defaultScenario(), wip_limit: 1 }));
  assert.deepEqual(JSON.parse(query.get('scenario')).wip_limit, 1);
});

test('every knob the model accepts is on the page, and nothing else', () => {
  const markup = policyConfigMarkup(defaultScenario());
  for (const knob of KNOBS) {
    assert.match(markup, new RegExp(`data-knob="${knob.key}"`), `${knob.key} is missing`);
  }
  // the closed set the API accepts (server/analysis) must match the page
  const allowed = new Set([
    'wip_limit', 'sentinels', 'review_sla_hours', 'size_cap_commits',
    'stack_depth_cap', 'review_first',
  ]);
  assert.deepEqual(new Set(KNOBS.map((knob) => knob.key)), allowed);
});

// ------------------------------------------------------------ the gate
test('a validated baseline shows the mismatch, pass or fail', () => {
  const markup = validationMarkup(payload());
  assert.match(markup, /baseline validated/);
  assert.match(markup, /≈22 h 26 min/);
  assert.match(markup, /1 d 0 h/);
  assert.match(markup, /−9\.4 %/);
});

test('a failed baseline withholds the scenario numbers', () => {
  const failed = payload({
    validation: {
      ok: false, status: 'failed', tolerance: 0.2,
      historical_median_hours: 24.77, predicted_median_hours: 50.31,
      percent_off: 103.1,
    },
    scenario: null,
    blocked: 'baseline did not validate, so the scenario was not run',
  });
  const markup = policyMarkup(failed, { ...defaultScenario(), sentinels: 2 });
  assert.match(markup, /baseline failed/);
  assert.match(markup, /scenario numbers are withheld/);
  assert.match(markup, /\+103\.1 %/);
  // the A/B table is not rendered: there is nothing honest to put in it
  assert.doesNotMatch(markup, /policy-table/);
});

test('an uncalibrated store says why instead of showing numbers', () => {
  const markup = validationMarkup({
    ok: false, available: false, note: 'no reviewer response was observed',
  });
  assert.match(markup, /not calibrated/);
  assert.match(markup, /no reviewer response/);
});

// ------------------------------------------------------------------ A/B
test('the A/B puts baseline and scenario side by side with deltas last', () => {
  const data = payload({
    scenario: { ...result(), scenario: { name: 'scenario', sentinels: 2 } },
    compare: {
      cycle_median_hours: { baseline: 22.43, scenario: 21.94, absolute: -0.49,
        percent: -2.2, direction: 'down', better: true },
      cycle_p90_hours: { baseline: 36.04, scenario: 34.75, absolute: -1.29,
        percent: -3.6, direction: 'down', better: true },
      flow_efficiency: { baseline: 0.15, scenario: 0.151, absolute: 0.001,
        percent: 0.6, direction: 'up', better: true },
      throughput_per_day: { baseline: 1.74, scenario: 1.69, absolute: -0.05,
        percent: -3.0, direction: 'down', better: false },
      lead_hours: { baseline: 22.43, scenario: 21.94, absolute: -0.49,
        percent: -2.2, direction: 'down', better: true },
      bottleneck: { baseline: 'sentinel:1', scenario: 'sentinel:1',
        baseline_utilisation: 0.48, scenario_utilisation: 0.40 },
      within_noise: false,
    },
  });
  const markup = abMarkup(data);
  assert.match(markup, /cycle time · median/);
  assert.match(markup, /≈22 h 26 min/);
  // a shorter cycle is good, a lower throughput is not — the colour must differ
  assert.match(markup, /c-delta good/);
  assert.match(markup, /c-delta bad/);
  assert.match(markup, /−2\.2 %/);
});

test('a delta inside the model spread is called "no clear effect"', () => {
  const data = payload({
    scenario: { ...result(), scenario: { name: 'scenario', sentinels: 2 } },
    compare: {
      cycle_median_hours: { baseline: 22.43, scenario: 21.94, absolute: -0.49,
        percent: -2.2, direction: 'down', better: true },
      cycle_p90_hours: {}, flow_efficiency: {}, throughput_per_day: {},
      lead_hours: {}, bottleneck: {}, within_noise: true,
    },
  });
  assert.match(abMarkup(data), /smaller than the run-to-run spread/);
});

test('the bottleneck row names the server and its utilisation', () => {
  const markup = resourcesMarkup(result());
  assert.match(markup, /sentinel:1/);
  assert.match(markup, /≈48%/);
  assert.match(markup, /≈1 h 12 min/);
});

// ------------------------------------------------------------- provenance
test('the provenance line states window, replications and the ≈ contract', () => {
  const line = provenanceLine(payload());
  assert.match(line, /calibrated from 131 PRs/);
  assert.match(line, /business hours/);
  assert.match(line, /6 replications/);
  assert.match(line, /every predicted number is an estimate/);
});

test('calibration facts are shown, and an unmeasurable stack says so', () => {
  const markup = calibrationMarkup(calibration());
  assert.match(markup, /131/);
  assert.match(markup, /not measurable \(no ref digests\)/);
  assert.match(markup, /9\.23 h observed/);
});

test('the markdown export carries the gate verdict and the ≈ marker', () => {
  const text = policyMarkdown(payload());
  assert.match(text, /baseline validated/);
  assert.match(text, /≈22 h 26 min/);
  assert.match(text, /Every predicted number is an estimate/);
});

// ------------------------------------------------------- page wiring
test('the policy page is wired: it fetches /api/simulate with the scenario', () => {
  const main = read('main.js');
  assert.match(main, /from '\/policy\.js'/);
  assert.match(main, /\/api\/simulate\?/);
  assert.match(main, /scenarioQuery\(state\.policyScenario\)/);
  assert.match(main, /if \(page === 'policy'\) renderPolicyPage\(\)/);
  assert.match(main, /event\.target\.closest\('#policy-run'\)/);
  assert.match(main, /event\.target\.closest\('#policy-copy'\)/);
});

test('the policy page is no longer a stub', () => {
  const html = read('index.html');
  assert.match(html, /id="policy-body"/);
  assert.doesNotMatch(html, /Not built in this slice/);
});

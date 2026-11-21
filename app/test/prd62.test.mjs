/* prd/62 — rolled first-pass yield and a guardrail beside every speed lever.
 *
 * The Rules page answers "what should we change first?"; the yield panel is the
 * *cost side* of that answer. These tests pin the pure markup: the waterfall
 * order and rates, the neutral rolled figure, the warning dot (only below the
 * lab-implied yield), the stability tiles with the bug-share dash and reason,
 * the guardrail on every mapped lever and in the Policy lab, the ticket's
 * four-step pass line, and the phone stacking.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  firstPassPanels, fmtShare, stabilityPanel, yieldMarkdown, yieldPanel,
} from '../yield.js';
import { guardrailLine, rulesMarkdown, rulesMarkup } from '../rules.js';
import { guardrailCell, labGuardrail, abMarkup } from '../policy.js';
import { yieldPassLine } from '../ticket.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const STEP = (id, label, clean, n, rate, signals, items = []) => ({
  id, label, window: `${id} window`, clean_n: clean, n, rate,
  available: true, signals, items, rate_hidden: false,
});

const YIELD = {
  available: true,
  cohort: 'completed stories',
  n: 87,
  min_deliveries: 10,
  steps: [
    STEP('refine', 'Refined', 87, 87, 1,
      ['ready → backlog', 'pointed after started']),
    STEP('develop', 'Developed', 66, 87, 0.759,
      ['in_review → in_progress'], ['DEL-101']),
    STEP('review', 'Reviewed', 11, 87, 0.126,
      ['zero review rounds across every merged PR'], ['DEL-102', 'DEL-104']),
    STEP('verify', 'Verified', 78, 87, 0.897,
      ['in_verification → in_progress', 'done → ready'], ['DEL-105', 'DEL-106']),
  ],
  rolled: 0.086,
  clean_joint: 8,
  joint_rate: 0.092,
  no_pr: 0,
  basis: '%C&A = the share of items that left this step complete and accurate; ' +
    'Rolled %C&A is the product of the four steps; the joint count is the ' +
    'measured clean first pass and can differ slightly.',
  note: 'only 8 of 87 stories went through without a loop-back',
  implied: {
    steps: { review: 0.511 },
    basis: "the lab's fitted rework probability; it does not model the other steps",
  },
};

const STABILITY = {
  available: true,
  n: 87,
  min_deliveries: 10,
  reopen: {
    label: 'reopened after done', count: 5, rate: 0.057,
    items: ['DEL-105', 'DEL-137', 'DEL-153', 'DEL-169'],
    signals: ['done → ready'],
  },
  failed_verification: {
    label: 'failed verification', count: 4, rate: 0.046,
    items: ['DEL-106', 'DEL-132', 'DEL-158', 'DEL-171'],
    signals: ['in_verification → in_progress'],
  },
  review_bounce: {
    label: 'review bounced', count: 21, rate: 0.241, items: [],
    signals: ['in_review → in_progress'],
  },
  bug_share: {
    available: false,
    count: null,
    rate: null,
    reason: "the store's issue objects carry no issue type",
  },
  rework_proxy: {
    label: 'rework proxy', count: 9, rate: 0.103,
    basis: 'reopened or failed verification / deliveries',
    proxy: true,
    note: "not DORA's rework rate: this store has no deploy or incident data",
  },
};

const METRICS = { yield: YIELD, stability: STABILITY };

const GUARDRAIL_MODELLED = {
  modelled: true,
  label: 'rounds',
  text: 'rounds 1.6 → 0.8 (model); reopen 5.7 % and failed verification 4.6 % ' +
    'are held constant — the model does not rework the code, only the review loop',
  worsens: false,
  parts: [
    { text: 'rounds 1.6 → 0.8 (model)', tone: 'neutral' },
    { text: 'reopen 5.7 % and failed verification 4.6 % are held constant', tone: 'neutral' },
  ],
};

const GUARDRAIL_HELD = {
  modelled: false,
  label: 'reopen',
  text: 'held constant: reopen 5.7 % and failed verification 4.6 % — the model ' +
    'does not rework the code',
  worsens: false,
  parts: [
    { text: 'held constant: reopen 5.7 % and failed verification 4.6 %', tone: 'neutral' },
  ],
};

const GUARDRAIL_WARN = {
  modelled: true,
  label: 'PRs per 100 commits',
  text: 'PRs per 100 commits 16.2 → 26.4 (model); reopen 5.7 % and failed ' +
    'verification 4.6 % are held constant — the model does not rework the code',
  worsens: true,
  parts: [
    { text: 'PRs per 100 commits 16.2 → 26.4 (model)', tone: 'warning' },
    { text: 'reopen 5.7 % and failed verification 4.6 % are held constant', tone: 'neutral' },
  ],
};

function suggestion(id, guardrail) {
  return {
    id, rank: 1, severity: 'warning', metric: 'rework_loops',
    title: 'a rule', suggestion: 'do the thing', impact_hours: 10,
    confidence: 0.5, evidence: { rounds: 2 }, scope: { kind: 'team' },
    policy_lab: guardrail
      ? {
        policy: 'roundcap2', label: 'cap review at 2 rounds', note: 'the lab supports this',
        contradiction: false, supports: true, guardrail,
      }
      : undefined,
  };
}

/* ---- the waterfall ---------------------------------------------------- */

test('the rate formatter is the PRD’s two-significant-digit copy', () => {
  assert.equal(fmtShare(1), '100 %');
  assert.equal(fmtShare(0.759), '76 %');
  assert.equal(fmtShare(0.126), '13 %');
  assert.equal(fmtShare(0.897), '90 %');
  assert.equal(fmtShare(0.086), '8.6 %');
  assert.equal(fmtShare(0.057), '5.7 %');
  assert.equal(fmtShare(0.046), '4.6 %');
  assert.equal(fmtShare(0.241), '24 %');
  assert.equal(fmtShare(0.103), '10 %');
  assert.equal(fmtShare(null), '–');
});

test('the waterfall is refine → develop → review → verify → rolled', () => {
  const html = yieldPanel(METRICS);
  assert.match(html, /id="first-pass-yield"/);
  const order = ['refine', 'develop', 'review', 'verify'].map(
    (id) => html.indexOf(`data-yield-step="${id}"`),
  );
  for (let index = 1; index < order.length; index += 1) {
    assert.ok(order[index] > order[index - 1], 'step order');
  }
  assert.ok(html.indexOf('data-yield-rolled-cell') > order[order.length - 1]);
  // the PRD's rates, in the waterfall
  assert.match(html, /REFINED|Refined/);
  assert.match(html, /100 %/);
  assert.match(html, /13 %/);
  assert.match(html, /8\.6 %/);
  // the joint count is printed next to the product and explained
  assert.match(html, /8<\/b> of 87 stories/);
  assert.match(html, /joint count/);
});

test('the rolled figure is neutral — a number, not a state', () => {
  const html = yieldPanel(METRICS);
  const rolled = html.slice(html.indexOf('class="yield-rolled"'));
  assert.ok(!/critical|warning|blocked|healthy/.test(rolled.split('</li>')[0]));
  assert.match(html, /data-rolled="0\.086"/);
});

test('a warning dot appears only below the lab-implied yield', () => {
  const html = yieldPanel(METRICS);
  assert.equal((html.match(/data-yield-below=/g) || []).length, 1);
  const review = html.slice(
    html.indexOf('data-yield-step="review"'),
    html.indexOf('data-yield-step="verify"'),
  );
  assert.match(review, /data-yield-below="0\.511"/);
  // no dot on the steps at or above the implied 51 %
  assert.ok(!/data-yield-below/.test(
    html.slice(html.indexOf('data-yield-step="refine"'),
      html.indexOf('data-yield-step="review"')),
  ));
  // without an implied yield there is no dot at all
  const plain = yieldPanel({
    ...METRICS,
    yield: { ...YIELD, implied: { steps: {}, basis: 'the lab did not run' } },
  });
  assert.ok(!/data-yield-below/.test(plain));
});

test('clicking a step opens its loop-back list, each a ticket link', () => {
  const html = yieldPanel(METRICS, { base: 'http://host/' });
  assert.match(html, /<details class="yield-drill">/);
  assert.match(html, /data-yield-item="DEL-102"/);
  assert.match(html, /href="http:\/\/host\/#ticket\/DEL-102"/);
});

test('a store with no pipeline says why, instead of a perfect score', () => {
  const html = yieldPanel({
    yield: { available: false, note: 'no STORY objects in this store' },
  });
  assert.match(html, /no STORY objects in this store/);
  assert.match(html, /unavailable/);
  // a missing panel is '' so the Rules page does not claim an empty section
  assert.equal(yieldPanel({}), '');
});

/* ---- the stability panel --------------------------------------------- */

test('the stability tiles name their cohort and n', () => {
  const html = stabilityPanel(STABILITY);
  assert.match(html, /id="stability"/);
  assert.match(html, /data-stability="reopen"/);
  assert.match(html, /data-stability="failed_verification"/);
  assert.match(html, /data-stability="review_bounce"/);
  assert.match(html, /data-stability="bug_share"/);
  assert.match(html, /5\.7 %/);
  assert.match(html, /4\.6 %/);
  assert.match(html, /24 %/);
  assert.match(html, /5 of 87/);
  assert.match(html, /title="Reopened: 5 of 87 deliveries/);
});

test('the bug share is a dash with its reason, never summary_class', () => {
  const html = stabilityPanel(STABILITY);
  const tile = html.slice(html.indexOf('data-stability="bug_share"'));
  assert.match(tile, /—/);
  assert.match(tile, /no issue type/);
  assert.ok(!/summary class|summary_class/i.test(html));
});

test('the rework proxy is printed and labelled a proxy', () => {
  const html = stabilityPanel(STABILITY);
  assert.match(html, /data-rework-proxy/);
  assert.match(html, /Rework proxy 10 %/);
  assert.match(html, /not DORA's rework rate/);
  assert.match(html, /no deploy or incident data/);
});

test('both panels render in the order the page reads', () => {
  const html = firstPassPanels(METRICS);
  assert.ok(html.indexOf('id="first-pass-yield"') < html.indexOf('id="stability"'));
});

/* ---- the guardrail ---------------------------------------------------- */

test('every mapped lever carries a guardrail, and held constant is stated', () => {
  const suggestions = [
    suggestion('round-cap', GUARDRAIL_MODELLED),
    suggestion('pr-size', GUARDRAIL_WARN),
    suggestion('sentinel-tail', GUARDRAIL_HELD),
    // a rule with no lab mapping is silent, never given an invented cost
    suggestion('comment-tax', null),
  ];
  const data = {
    level: 'team', suggestions, metrics: METRICS, sample: { prs: 3, days: 30 },
    hours_mode: 'business', as_of: '2026-11-18T00:00:00Z', note: '',
  };
  const html = rulesMarkup(data);
  assert.equal((html.match(/data-guardrail=/g) || []).length, 3);
  assert.match(html, /data-guardrail="modelled"/);
  assert.match(html, /data-guardrail="held_constant"/);
  assert.match(html, /rounds 1\.6 → 0\.8 \(model\)/);
  assert.match(html, /held constant: reopen 5\.7 %/);
  assert.match(html, /PRs per 100 commits <b class="guardrail-warn">16\.2 → 26\.4/);
  // only the worsening number is tinted, never the whole sentence
  assert.match(html, /guardrail-warn">16\.2 → 26\.4/);
  assert.ok(!/guardrail-warn">1\.6/.test(html));
});

test('the guardrail line is emitted for the rule and in the export', () => {
  const line = guardrailLine(suggestion('round-cap', GUARDRAIL_MODELLED));
  assert.match(line, /GUARDRAIL/);
  assert.match(line, /does not rework the code/);
  assert.equal(guardrailLine(suggestion('round-cap', null)), '');
  const markdown = rulesMarkdown({
    level: 'team', suggestions: [suggestion('round-cap', GUARDRAIL_MODELLED)],
    metrics: METRICS, sample: { prs: 3, days: 30 }, dataset: {},
    hours_mode: 'business', as_of: '2026-11-18T00:00:00Z', note: '',
  });
  assert.match(markdown, /- guardrail: rounds 1\.6 → 0\.8 \(model\)/);
  assert.match(markdown, /reopen 5\.7 %/);
});

test('the Policy lab puts the same guardrail under the cycle delta', () => {
  const compare = {
    cycle_median_hours: { baseline: 40, scenario: 34, percent: -15, direction: 'down', better: true },
    cycle_p90_hours: { baseline: 100, scenario: 90, percent: -10, direction: 'down', better: true },
    flow_efficiency: { baseline: 0.3, scenario: 0.34, percent: 13, direction: 'up', better: true },
    throughput_work_per_day: { baseline: 5, scenario: 5.2, percent: 4, direction: 'up', better: true },
    throughput_per_day: { baseline: 1, scenario: 1.3, percent: 30, direction: 'up', better: true },
    lead_hours: { baseline: 50, scenario: 54, percent: 8, direction: 'up', better: false },
    guardrails: {
      modelled: [
        { id: 'rounds_mean', label: 'rounds', baseline: 1.6, scenario: 0.8 },
      ],
      held_constant: [
        { id: 'reopen', label: 'reopened', baseline: 0.057 },
      ],
      not_modelled: [{ id: 'bug_share', reason: 'no issue type' }],
    },
    verdict: 'cycle 15.0 % shorter', verdict_tone: 'good',
  };
  assert.equal(
    labGuardrail(compare),
    'guardrail: rounds 1.6 → 0.8 · reopen held at 5.7 %',
  );
  assert.match(guardrailCell(compare), /data-policy-guardrail/);
  const data = {
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
  const html = abMarkup(data);
  assert.match(html, /data-policy-guardrail/);
  assert.match(html, /guardrail: rounds 1\.6 → 0\.8 · reopen held at 5\.7 %/);
  // a compare with no guardrails (an older payload) stays silent, not broken
  assert.equal(labGuardrail({ cycle_median_hours: {} }), '');
});

/* ---- the ticket drawer ------------------------------------------------ */

test('the ticket shows this story’s four-step pass', () => {
  const payload = {
    yield: {
      steps: [
        { id: 'refine', label: 'Refined', clean: true, signals: [] },
        { id: 'develop', label: 'Developed', clean: false, signals: [] },
        { id: 'review', label: 'Reviewed', clean: true, signals: [] },
        { id: 'verify', label: 'Verified', clean: true, signals: [] },
      ],
      rolled_clean: false,
    },
  };
  const html = yieldPassLine(payload);
  assert.match(html, /data-pass-step="refine"/);
  assert.match(html, /Refined clean/);
  assert.match(html, /Developed bounced/);
  assert.match(html, /a loop-back somewhere/);
  assert.match(html, /data-rolled-clean="false"/);
  assert.equal(yieldPassLine({}), '');
  // a story with no merged PR has no review pass to measure
  const unmeasured = yieldPassLine({
    yield: {
      steps: [
        { id: 'refine', label: 'Refined', clean: true, available: true, signals: [] },
        { id: 'develop', label: 'Developed', clean: true, available: true, signals: [] },
        { id: 'review', label: 'Reviewed', clean: false, available: false,
          signals: [], note: 'no merged PR in this store' },
        { id: 'verify', label: 'Verified', clean: true, available: true, signals: [] },
      ],
      rolled_clean: false, available: false,
    },
  });
  assert.match(unmeasured, /Reviewed not measured/);
  assert.match(unmeasured, /not measurable for this story/);
});

/* ---- the export and the phone ----------------------------------------- */

test('the Markdown export carries the step table and the stability rates', () => {
  const lines = yieldMarkdown(METRICS, 'http://host/');
  const text = lines.join('\n');
  assert.match(text, /## First-pass yield \(rolled %C&A 8\.6 %\)/);
  assert.match(text, /\| Refined \| 100 % \| 87 \/ 87 \|/);
  assert.match(text, /\| Reviewed \| 13 % \| 11 \/ 87 \|/);
  assert.match(text, /Only 8 of 87 stories went through without a loop-back/);
  assert.match(text, /### Stability/);
  assert.match(text, /- Reopened: 5\.7 % \(5 of 87\)/);
  assert.match(text, /\[DEL-105\]\(http:\/\/host\/#ticket\/DEL-105\)/);
  assert.match(text, /Bug share: — \(.*no issue type/);
  assert.match(text, /Rework proxy 10 %/);
});

test('the phone stacks the waterfall and the stability tiles 2 × 2', () => {
  const css = read('style.css');
  const phone = css.slice(css.lastIndexOf('@media (max-width: 640px)'));
  assert.match(phone, /\.yield-waterfall \{ flex-direction: column/);
  assert.match(phone, /\.stability-tiles \{ grid-template-columns: 1fr 1fr/);
  assert.match(phone, /\.yield-rolled \{/);
});

/* prd/58 — the constraint verdict and the exploit → elevate ladder.
 *
 * The Overview card must answer with **one** sentence on **one** clock, and the
 * Rules page must carry the same verdict as a banner with the lower-ranked
 * levers behind "next constraint". These tests pin the sentence (numbers, not a
 * point-estimate headline), the rung order, the blocked elevation's readable
 * reason, the next-constraint disclosure, the Rules banner + collapsed non-rung
 * rules, the phone markup (no chart, no overflow) and the Markdown export.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  constraintSentence, currentRung, isDisclosedByDefault, ladderMarkup,
  nextConstraint, nextLine, poolHours, poolRows, ruleRung, tieNote,
  verdictBanner, verdictCard, verdictMarkdown,
} from '../verdict.js';
import { rulesMarkup, rulesStats } from '../rules.js';

const FRMO = {
  person: 'FrMo', role: 'sentinel', hours: 961.27,
  all_phase_hours: 1882.38, items: ['#52', '#55', '#60'],
};
const CAOS = {
  person: 'CaOs', role: 'reviewer', hours: 186.53,
  all_phase_hours: 495.76, items: [],
};

function rung(step, state, overrides = {}) {
  return {
    step, label: step[0].toUpperCase() + step.slice(1), state, reason: null,
    rules: [], labs: [], note: '', claims: 'lab', ...overrides,
  };
}

export const VERDICT = {
  ok: true,
  as_of: '2026-11-18T00:10:44Z',
  hours_mode: 'business',
  available: true,
  constraint: {
    station: 'review', label: 'Approval gate', wait_label: 'review',
    removable_hours: 619.1, live_hours: 237.54, tail_hours: 381.56,
    history_hours: 3328.7, n_completed: 87, p85_hours: 70.78, open_n: 4,
    clock: 'story', hours_kind: 'story', tied_with: [],
    basis: 'PR open → merge',
    items: [
      { key: 'DEL-176', hours: 144, pr: '#60', holders: ['FrMo'], orphaned: true },
      { key: 'DEL-198', hours: 35.37, pr: '#78', holders: ['ErLi', 'FrMo'], orphaned: false },
      { key: 'DEL-197', hours: 32, pr: '#51', holders: ['DaKo', 'FrMo'], orphaned: false },
      { key: 'DEL-199', hours: 26.17, pr: '#79', holders: ['BoFe', 'FrMo'], orphaned: false },
    ],
  },
  pools: [
    { station: 'review', label: 'Approval gate', removable_hours: 619.1, live_hours: 237.54, tail_hours: 381.56 },
    { station: 'verify', label: 'Verification', removable_hours: 80.85, live_hours: 0, tail_hours: 80.85 },
    { station: 'start', label: 'Starting work', removable_hours: 41.19, live_hours: 0, tail_hours: 41.19 },
    { station: 'refine', label: 'Refinement', removable_hours: 33.34, live_hours: 0, tail_hours: 33.34 },
  ],
  holders: {
    clock: 'pr-holder',
    people: [FRMO, CAOS],
    peer_range: [142.2, 230.8],
    current: [{ pr: '#53', ticket: 'DEL-167', hours: 168.3, holders: ['CaOs', 'FrMo'], ball: 'review', stale: true }],
    available: true,
    note: 'Holder hours are person-hours with the ball in review, priced on each holder\'s own calendar — not response time.',
  },
  adjacent: [
    { kind: 'dead', n: 9, hours: 2903.5, hours_kind: 'story', clock: 'story', sentence: '9 stories sat in review with no live PR.' },
    { kind: 'unmerged', n: 3, hours: 344.9, hours_kind: 'pr-holder', clock: 'pr-holder', sentence: '3 PRs are still open.' },
  ],
  exploit: {
    n: 3, hours: 451.9, note: 'The model does not see the ticket-level wait.',
    items: [
      { kind: 'pr', pr: '#53', ticket: 'DEL-167', hours: 168.3, holders: ['CaOs', 'FrMo'], orphaned: false, stale: true },
      { kind: 'pr', pr: '#55', ticket: 'DEL-168', hours: 152, holders: ['FrMo'], orphaned: true, stale: true },
      { kind: 'pr', pr: '#60', ticket: 'DEL-176', hours: 131.6, holders: ['FrMo'], orphaned: true, stale: true },
    ],
    orphans: [
      { pr: '#55', ticket: 'DEL-168', hours: 152, holders: ['FrMo'], stale: true },
      { pr: '#60', ticket: 'DEL-176', hours: 131.6, holders: ['FrMo'], stale: true },
      { pr: '#52', ticket: 'DEL-197', hours: 16, holders: ['FrMo'], stale: false },
    ],
  },
  ladder: [
    rung('exploit', 'open', {
      rules: [
        { id: 'stale-prs', title: '8 stale PRs', metric: 'stale_prs', severity: 'critical', impact_hours: 499, lab: { note: 'the lab has no verdict for this policy in this window' } },
        { id: 'round-cap', title: 'Review rounds track cycle time — cap them at 2', metric: 'rework_loops', severity: 'warning', impact_hours: 144.5, lab: { note: 'the policy lab models this at −13 % cycle (the lab\'s best cap is 2 rounds — the same cap the rule recommends)' } },
      ],
      labs: [
        { id: 'sla4', label: 'review SLA 4 h', verdict: 'supported', cycle_percent: -9, note: 'the policy lab models this at −9 % cycle (the lab escalates an overdue review to any free reviewer)' },
        { id: 'roundcap2', label: 'cap review at 2 rounds', verdict: 'supported', cycle_percent: -12.7, note: 'the policy lab models this at −13 % cycle (the lab\'s best cap is 2 rounds — the same cap the rule recommends)' },
      ],
      note: 'review SLA 4 h: the policy lab models this at −9 % cycle; cap review at 2 rounds: the policy lab models this at −13 % cycle',
      claims: 'rules',
      items: [
        { pr: '#53', ticket: 'DEL-167', hours: 168.3, holders: ['CaOs', 'FrMo'], orphaned: false },
        { pr: '#55', ticket: 'DEL-168', hours: 152, holders: ['FrMo'], orphaned: true },
        { pr: '#60', ticket: 'DEL-176', hours: 131.6, holders: ['FrMo'], orphaned: true },
      ],
    }),
    rung('subordinate', 'open', {
      rules: [
        { id: 'flow-efficiency', title: 'Flow efficiency is 18%', metric: 'flow_efficiency', severity: 'warning', impact_hours: 831.6, lab: { note: 'the policy lab finds no clear effect' } },
        { id: 'pr-size', title: 'Cap PRs at 3 commits', metric: 'pr_size', severity: 'warning', impact_hours: 120, lab: { note: 'the policy lab models this at +99 % cycle' } },
      ],
      labs: [{ id: 'size3', label: 'cap PRs at 3 commits', verdict: 'not a measured win', cycle_percent: 98.7, note: 'the policy lab models this at +99 % cycle' }],
      note: 'cap PRs at 3 commits: the policy lab models this at +99 % cycle',
    }),
    rung('elevate', 'blocked', {
      reason: 'exploit first: 3 items are still owed',
      labs: [{ id: 'sentinel2', label: 'add a sentinel', verdict: 'no clear effect', cycle_percent: -5, note: 'the policy lab finds no clear effect (−5 % cycle, within its run-to-run spread)' }],
      note: 'add a sentinel: the policy lab finds no clear effect (−5 % cycle, within its run-to-run spread)',
    }),
  ],
  next: [
    { station: 'verify', label: 'Verification', removable_hours: 80.85 },
    { station: 'start', label: 'Starting work', removable_hours: 41.19 },
    { station: 'refine', label: 'Refinement', removable_hours: 33.34 },
  ],
  current_rung: 'exploit',
  rule_rungs: {
    by_rule: {
      'stale-prs': 'exploit', 'round-cap': 'exploit',
      'flow-efficiency': 'subordinate', 'pr-size': 'subordinate',
      'sprint-on-time': 'measured', 'time-to-merge': 'measured',
    },
    families: [{ prefix: 'reviewer-', rung: 'elevate' }, { prefix: 'vacation-', rung: 'subordinate' }],
    order: ['exploit', 'subordinate', 'elevate'],
    labels: { exploit: 'Exploit', subordinate: 'Subordinate', elevate: 'Elevate' },
    measured: ['sprint-on-time', 'time-to-merge'],
  },
  liveness: { open: 13, live: 4, dead: 8, unobserved: 1, unmerged: 3 },
  note: 'Pool = business hours on the team calendar; story segments for refinement and verification, the PR ball clock for the review detail.',
  pool_note: 'Pool = business hours on the team calendar; story segments for refinement and verification, the PR ball clock for the review detail.',
  holder_note: 'Holder hours are person-hours with the ball in review (the PR ball clock) — not response time, and never added to the story-clock pool.',
  basis: 'one canonical clock: station pools are business hours on the story clock; the review detail\'s holders use the PR ball clock.',
};

function suggestion(id, rank, overrides = {}) {
  return {
    id, rank, severity: 'warning', metric: 'm',
    title: `${id} rule`, suggestion: `${id} action`,
    impact_hours: 100, confidence: 1,
    evidence: { prs: 3 }, scope: { kind: 'team' },
    ...overrides,
  };
}

function rulesData(overrides = {}) {
  return {
    level: 'team',
    sample: { prs: 8, days: 90 },
    dataset: { prs: 8, days: 90 },
    hours_mode: 'business',
    as_of: '2026-11-18T00:10:44Z',
    note: '',
    metrics: {},
    jira: { available: true },
    at_stake: { hours: 1075, overlap_hours: 0, pools: [] },
    suggestions: [
      suggestion('stale-prs', 1, { impact_hours: 499 }),
      suggestion('flow-efficiency', 2, { impact_hours: 831 }),
      suggestion('round-cap', 3, { impact_hours: 144 }),
      suggestion('sprint-on-time', 4, { impact_hours: 685 }),
      suggestion('pr-size', 5, { impact_hours: 120 }),
      suggestion('time-to-merge', 6, { impact_hours: 280 }),
    ],
    ...overrides,
  };
}

/* ---------------------------------------------------------------- the card */

test('the card renders one claim with its numbers, not a point-estimate headline', () => {
  const sentence = constraintSentence(VERDICT);
  assert.match(sentence, /^The constraint is the approval gate\./);
  assert.match(sentence, /619 h of removable review wait/);
  assert.match(sentence, /238 h currently owed and 382 h of abnormal tail/);
  assert.match(sentence, /#60 \(DEL-176\) has been in review/);
  assert.match(sentence, /waiting on FrMo/);
  // the sentence holds round business-hour figures; an estimate would carry ≈
  assert.doesNotMatch(sentence, /≈/);

  const html = verdictCard(VERDICT);
  assert.match(html, /id="verdict"/);
  assert.match(html, /data-verdict-sentence/);
  assert.match(html, /THE CONSTRAINT/);
  assert.match(html, /The constraint is the approval gate/);
  assert.match(html, /87 completed stories · 13 open PRs · story clock · as of/);
  // the four-row definition list, holders labelled with the PR clock
  assert.match(html, /data-pool="removable"/);
  assert.match(html, /data-pool="owed"/);
  assert.match(html, /data-pool="tail"/);
  assert.match(html, /data-pool="holders"[\s\S]*PR clock/);
  assert.match(html, /above p85/);
  // "which clock and why" is on the card, not in a tooltip
  assert.match(html, /data-verdict-clock/);
  assert.match(html, /story clock/);
  assert.match(html, /never added to the pool/);
});

test('the rungs are ordered exploit → subordinate → elevate, and elevate is blocked', () => {
  const html = ladderMarkup(VERDICT);
  const order = [...html.matchAll(/data-rung="([a-z]+)"/g)].map((match) => match[1]);
  assert.deepEqual(order, ['exploit', 'subordinate', 'elevate']);
  // the blocked rung is one readable line with the reason and no action
  const blocked = html.slice(html.indexOf('data-rung="elevate"'));
  assert.match(blocked, /exploit first: 3 items are still owed/);
  assert.match(blocked, /No capacity change is proposed while the exploit items are owed/);
  assert.match(blocked, /data-rung-state="blocked"/);
  assert.match(blocked, /class="pill neutral rung-state"/);
  // never red: a blocked elevation is a decision, not a fire
  assert.doesNotMatch(blocked, /class="pill (critical|blocked)/);
  // the lab's verdict is quoted, in words
  assert.match(blocked, /no clear effect/);
  // the exploit rung names the items it owes (the ladder's data source)
  const exploit = html.slice(0, html.indexOf('data-rung="subordinate"'));
  assert.match(exploit, /#53/);
  assert.match(exploit, /#55/);
  assert.match(exploit, /#60/);
  assert.match(exploit, /orphaned approval/);
});

test('the next constraint sits behind a native disclosure', () => {
  assert.deepEqual(nextConstraint(VERDICT).map((row) => row.station),
    ['verify', 'start', 'refine']);
  assert.equal(nextLine(VERDICT),
    'verification 0 h owed now (81 h removable) · ' +
    'starting work 0 h owed now (41 h removable) · ' +
    'refinement 0 h owed now (33 h removable)');
  const html = verdictCard(VERDICT);
  const details = html.match(/<details class="verdict-next" data-next-constraint>([\s\S]*?)<\/details>/);
  assert.ok(details, 'a <details> next-constraint disclosure');
  assert.match(details[1], /data-next-station="verify"/);
  assert.match(details[1], /81 h/);
  // the constraint itself is not "next"
  assert.doesNotMatch(details[1], /data-next-station="review"/);
});

test('a tie is named, and no pool clears a week is stated instead of invented', () => {
  const tied = {
    ...VERDICT,
    constraint: {
      ...VERDICT.constraint,
      tied_with: [{ station: 'verify', label: 'Verification', removable_hours: 580 }],
    },
  };
  assert.match(tieNote(tied), /Tied \(within 10 %\): Verification/);
  assert.match(verdictCard(tied), /data-verdict-tie/);

  const quiet = {
    ...VERDICT,
    available: false,
    constraint: null,
    note: 'No constraint worth naming this week: the largest removable pool is 36 h, under one waiting week (40 h). The full ranking is below.',
    ladder: [],
    current_rung: null,
  };
  const html = verdictCard(quiet);
  assert.match(html, /No constraint worth naming this week/);
  assert.match(html, /no verdict/);
  assert.doesNotMatch(html, /data-verdict-ladder/);
  // the ranking is still available (the disclosure is the honest fallback)
  assert.match(html, /data-next-constraint/);
});

test('poolHours keeps the card in hours and the ranking readable', () => {
  assert.equal(poolHours(619.1), '619 h');
  assert.equal(poolHours(380), '380 h');
  assert.equal(poolHours(1234), '≈1.2k h');
  assert.deepEqual(poolRows(VERDICT).map((row) => row.id),
    ['removable', 'owed', 'tail', 'holders', 'oldest']);
});

test('the banner is one line with a jump to the ladder', () => {
  const html = verdictBanner(VERDICT);
  assert.match(html, /data-verdict-banner/);
  assert.match(html, /The constraint is the approval gate\./);
  assert.match(html, /619 h of removable wait/);
  assert.match(html, /238 h owed now on 4 stories/);
  assert.match(html, /story clock/);
  assert.match(html, /data-go="overview" data-anchor="verdict"/);
});

test('the phone markup has no chart and forces no horizontal scroll', () => {
  const html = verdictCard(VERDICT);
  assert.doesNotMatch(html, /<canvas/);
  assert.doesNotMatch(html, /overflow/);
  // two rows on a phone: removable + the oldest item
  assert.match(html, /class="vrow phone-keep" data-pool="removable"/);
  assert.match(html, /class="vrow phone-only phone-keep" data-pool="oldest"/);
  assert.match(html, /class="vrow phone-hide" data-pool="tail"/);
  // the holder row moves into the exploit rung on a phone
  assert.match(html, /class="rung-holder verdict-phone-only"/);
  assert.match(html, /FrMo 961 h · peers 142 h–231 h/);
});

test('the Markdown export carries the verdict, the ladder and the lab note', () => {
  const markdown = verdictMarkdown(VERDICT);
  assert.match(markdown, /^# The constraint/);
  assert.match(markdown, /The constraint is the approval gate\./);
  assert.match(markdown, /619 h/);
  assert.match(markdown, /## Exploit — open/);
  assert.match(markdown, /## Elevate — blocked/);
  assert.match(markdown, /_exploit first: 3 items are still owed_/);
  assert.match(markdown, /- policy lab: .*no clear effect/);
  assert.match(markdown, /- policy lab: .*−13 % cycle/);
  assert.match(markdown, /_Next: verification 0 h owed now \(81 h removable\)/);
});

/* --------------------------------------------------------------- the rungs */

test('the rule → rung map places every suggestion and never hides a diagnostic', () => {
  assert.equal(ruleRung('stale-prs', VERDICT), 'exploit');
  assert.equal(ruleRung('reviewer-AaAa', VERDICT), 'elevate');
  assert.equal(ruleRung('vacation-AaAa-2026-01-01', VERDICT), 'subordinate');
  assert.equal(ruleRung('sprint-on-time', VERDICT), 'measured');
  assert.equal(ruleRung('something-new', VERDICT), null);
  assert.equal(currentRung(VERDICT), 'exploit');
  // current rung + measured are shown; a lower rung is disclosed
  assert.equal(isDisclosedByDefault({ id: 'round-cap' }, VERDICT), true);
  assert.equal(isDisclosedByDefault({ id: 'sprint-on-time' }, VERDICT), true);
  assert.equal(isDisclosedByDefault({ id: 'flow-efficiency' }, VERDICT), false);
  // an unplaceable rule is never hidden
  assert.equal(isDisclosedByDefault({ id: 'new-rule' }, VERDICT), true);
});

/* ------------------------------------------------------- Rules disclosure */

test('Rules shows the banner and the current rung, the rest behind next constraint', () => {
  const data = rulesData();
  const html = rulesMarkup(data, { verdict: VERDICT });
  assert.match(html, /data-verdict-banner/);
  assert.match(html, /data-rules-disclosure/);
  assert.match(html, /showing the Exploit rung and 2 measured diagnostics; 2 levers behind the next constraint/);

  const detailsAt = html.indexOf('data-next-constraint-rules');
  assert.ok(detailsAt > 0, 'the lower-ranked levers are behind a disclosure');
  const visible = html.slice(0, detailsAt);
  const hidden = html.slice(detailsAt);
  // current rung's levers stay expanded
  assert.match(visible, /data-rule="stale-prs"/);
  assert.match(visible, /data-rule="round-cap"/);
  // measured diagnostics keep their ranked position in the list
  assert.match(visible, /data-rule="sprint-on-time"/);
  assert.match(visible, /data-rule="time-to-merge"/);
  // the next-constraint rung's levers are collapsed
  assert.match(hidden, /data-rule="flow-efficiency"/);
  assert.match(hidden, /data-rule="pr-size"/);
  assert.doesNotMatch(visible, /data-rule="flow-efficiency"/);
  assert.match(hidden, /Rules for the next constraint \(2\)/);
  // no rank, no at-stake number changed: the same ranks are rendered as before
  assert.match(html, /rule-rank mono" aria-hidden="true">0?2</);
  assert.match(html, /data-rule-show-all/);
});

test('the Show all rules toggle restores the full list with no rank change', () => {
  const data = rulesData();
  const collapsed = rulesMarkup(data, { verdict: VERDICT });
  const expanded = rulesMarkup(data, { verdict: VERDICT, showAllRules: true });
  for (const suggestion of data.suggestions) {
    assert.match(expanded, new RegExp(`data-rule="${suggestion.id}"`));
  }
  assert.doesNotMatch(expanded, /data-next-constraint-rules/);
  assert.match(expanded, /showing all 6 rules/);
  // the stats strip (the at-stake headline) is untouched by disclosure
  assert.equal(rulesStats(data), rulesStats({ ...data, showAllRules: true }));
});

test('without a verdict Rules keeps its old full list', () => {
  const html = rulesMarkup(rulesData());
  assert.doesNotMatch(html, /data-verdict-banner/);
  assert.doesNotMatch(html, /data-next-constraint-rules/);
  for (const id of ['stale-prs', 'flow-efficiency', 'sprint-on-time']) {
    assert.match(html, new RegExp(`data-rule="${id}"`));
  }
});

/* prd/68 G4 — quick UI fixes (F-01, F-05, F-09).
 *
 * - F-01: the Queue page crashed on every 0-response window under `scope=all`
 *   because `responseTable()` dereferenced a null `scope`. The legend,
 *   assumption and honesty below the table never mounted.
 * - F-05: a float `window_days` was passed to `fmtImpact()` and printed as
 *   `≈1.9k h` — a day count wearing the hour unit.
 * - F-09: the no-current-queue exploit rung printed the same disclaimer twice.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { esc } from '../format.js';
import { filteredPages } from '../scope.js';
import { legendMarkup, responseTable } from '../queues.js';
import { evidenceLabel, evidenceValue, ruleRow } from '../rules.js';
import { ladderMarkup, verdictCard } from '../verdict.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const MAIN = read('main.js');

/** The source of one top-level `function name(...) { ... }` in main.js. */
function functionSource(name) {
  const start = MAIN.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `main.js has no function ${name}()`);
  const end = MAIN.indexOf('\n}\n', start);
  assert.ok(end > start, `main.js function ${name}() never closes`);
  return MAIN.slice(start, end + 2);
}

/** Compile one extracted function with the identifiers it closes over. */
function compile(name, scope = {}) {
  const names = Object.keys(scope);
  const values = names.map((key) => scope[key]);
  return new Function(...names, `${functionSource(name)}; return ${name};`)(...values);
}

/* ---------------------------------------------------------------- F-01 ---- */

// The shape the `demo-tlla` corpus returns: a Jira-only store has no reviewer
// queue, so `people` is empty while the response object itself is truthy.
const EMPTY_QUEUE = {
  streams: [],
  buckets: 1,
  bucket_hours: 6,
  window: { start: '2026-08-24T00:00:00Z', end: '2026-11-22T00:00:00Z', days: 90 },
  assumption: 'every PR waits on someone',
  diagnosis_note: 'no PR layer in this dataset',
  totals: { edges: 0 },
  response: {
    people: [],
    reviewer_median_hours: null,
    sentinel_after_peers: true,
  },
  legend: {
    people: [{ person: 'LaNo', role: 'author', prs_authored: 0, approvals: 0, stories_owned: 12 }],
    note: '1 person with no activity in this window is hidden — roles, not names.',
  },
};

test('F-01: a 0-response response object with scope:null does not throw', () => {
  const html = responseTable(EMPTY_QUEUE, { scope: null });
  assert.equal(typeof html, 'string');
  // the unscoped empty state may be blank or a plain sentence — never `scope undefined`
  assert.doesNotMatch(html, /undefined/);
  assert.doesNotMatch(html, /for scope/);
  assert.match(html, /No review requests in this window\./);
});

test('F-01: the scoped empty state still names the scope (prd/63 F26 intact)', () => {
  const html = responseTable(EMPTY_QUEUE, {
    scope: { kind: 'person', value: 'LaNo', label: 'LaNo' },
  });
  assert.match(html, /No review requests for scope LaNo in this window/);
  assert.doesNotMatch(html, /undefined/);
});

test('F-01: renderQueuePage mounts legend, assumption and honesty on 0 responses', () => {
  const mounted = {};
  const host = () => ({
    set innerHTML(value) { mounted[this.id] = value; },
    get innerHTML() { return mounted[this.id] || ''; },
    textContent: '',
    id: '',
  });
  const hosts = {
    'queue-response': Object.assign(host(), { id: 'queue-response' }),
    'queue-legend': Object.assign(host(), { id: 'queue-legend' }),
    'queue-assumption': { textContent: '', id: 'queue-assumption' },
  };
  const state = {
    queues: EMPTY_QUEUE,
    queueLevel: 'person',
    benchmark: true,
    // `scope=all` — the exact live repro; `filteredPages` returns an empty set,
    // so renderQueuePage passes `scope: null` to responseTable().
    scope: { kind: 'all', value: null, label: 'everything' },
  };
  let honesty = '';
  let chart = null;
  const renderQueuePage = compile('renderQueuePage', {
    state,
    esc,
    filteredPages,
    responseTable,
    legendMarkup,
    queueMatchesScope: () => true,
    queueChart: { setData: (data) => { chart = data; } },
    $: (id) => hosts[id] || null,
    setHonesty: (text) => { honesty = text; },
  });

  assert.doesNotThrow(() => renderQueuePage());
  assert.match(hosts['queue-response'].innerHTML, /No review requests in this window\./);
  assert.match(hosts['queue-legend'].innerHTML, /who is who/);
  assert.match(hosts['queue-legend'].innerHTML, /LaNo/);
  assert.match(hosts['queue-assumption'].textContent, /calendar days/);
  assert.match(hosts['queue-assumption'].textContent, /Little's law/);
  assert.match(honesty, /0 streams/);
  assert.match(honesty, /whole team/);
  assert.ok(chart, 'the chart still receives the payload');
});

/* ---------------------------------------------------------------- F-05 ---- */

test('F-05: float day counts render as days, never as an hour pool', () => {
  assert.equal(evidenceValue('window_days', 1880.4), '1880.4 calendar days');
  assert.equal(evidenceValue('sprint_length_days', 14.5), '14.5 calendar days');
  // `limit_days` is derived from business hours (analysis/rules.py) — it keeps
  // the business-day unit rather than claiming calendar days.
  assert.equal(evidenceValue('limit_days', 2.93), '2.9 business days');
  for (const text of [
    evidenceValue('window_days', 1880.4),
    evidenceValue('sprint_length_days', 14.5),
    evidenceValue('limit_days', 2.93),
  ]) {
    assert.doesNotMatch(text, /≈|k h|\d h\b/, `${text} used the hour unit`);
    assert.match(text, /days?$/);
  }
  // integer day counts are unchanged
  assert.equal(evidenceValue('window_days', 28), '28');
  // real hour keys keep the one duration language
  assert.equal(evidenceValue('hours', 283.6), '≈35 d 3 h');
  assert.match(evidenceValue('median_wait_hours', 17.02), /^≈/);
});

test('F-05: the carry-over card prints "1880.4 calendar days"', () => {
  const html = ruleRow({
    id: 'jira-carry-over', rank: 2, severity: 'warning', metric: 'carry_over',
    title: '617 of 2001 stories carried over', impact_hours: 120,
    evidence: { window_days: 1880.4, carried_over: 617, window_sprints: 99 },
  });
  assert.match(html, /<dt>window days<\/dt>\s*<dd class="mono">1880\.4 calendar days<\/dd>/);
  assert.doesNotMatch(html, /≈1\.9k h/);
  assert.equal(evidenceLabel('window_days'), 'window days');
});

/* ---------------------------------------------------------------- F-09 ---- */

// The `no-current-queue` ladder on a store without a PR layer: the exploit rung
// has no lab verdict, and the payload's `note` is the honesty sentence.
const EXPLOIT_SENTENCE = 'The model does not see the ticket-level wait, so the exploit rung is the rules\' claim, not the lab\'s.';

const NO_QUEUE_LADDER = {
  available: true,
  constraint: null,
  current_rung: null,
  note: 'No current queue is visible in this window: 0 h owed now. This is history, not a live constraint.',
  pools: [],
  holders: { people: [] },
  ladder: [
    {
      step: 'exploit', label: 'Exploit', state: 'open', claims: 'rules',
      rules: [{ id: 'jira-waiting-time', title: 'Waiting time is the ticket wait', metric: 'waiting_time_decomposition' }],
      labs: [],
      note: EXPLOIT_SENTENCE,
      items: [],
    },
    {
      step: 'subordinate', label: 'Subordinate', state: 'done',
      rules: [], labs: [],
      note: 'the lab has no verdict for this policy in this window',
      items: [],
    },
    {
      step: 'elevate', label: 'Elevate', state: 'blocked',
      reason: 'exploit first: 1 rule on the exploit rung',
      rules: [], labs: [],
      note: 'the lab has no verdict for this policy in this window',
      items: [],
    },
  ],
};

const countOf = (text, needle) => text.split(needle).length - 1;

test('F-09: the no-queue exploit rung prints one disclaimer line, not two', () => {
  const html = verdictCard(NO_QUEUE_LADDER);
  assert.match(html, /data-verdict-state="no-queue"/);
  assert.equal(countOf(html, 'does not see the ticket-level wait'), 1,
    'the honesty sentence appeared more than once');
  // the lab line names the missing verdict instead of repeating the disclaimer
  const exploit = html.slice(html.indexOf('data-rung="exploit"'), html.indexOf('data-rung="subordinate"'));
  assert.match(exploit, /<span class="lab-tag mono">policy lab<\/span> the lab has no verdict for this policy in this window/);
  assert.match(exploit, /<p class="rung-honesty">/);
  // the elevate reason is carried through as the payload sends it
  const elevate = html.slice(html.indexOf('data-rung="elevate"'));
  assert.match(elevate, /exploit first: 1 rule on the exploit rung/);
});

test('F-09: a rung with a lab verdict still quotes it and keeps the honesty line', () => {
  const withLabs = {
    ...NO_QUEUE_LADDER,
    ladder: [{
      ...NO_QUEUE_LADDER.ladder[0],
      labs: [{
        id: 'sla4', label: 'review SLA 4 h', verdict: 'supported', cycle_percent: -9,
        note: 'the policy lab models this at −9 % cycle',
      }],
      note: 'review SLA 4 h: the policy lab models this at −9 % cycle',
    }],
  };
  const html = ladderMarkup(withLabs);
  assert.match(html, /review SLA 4 h −9 % cycle/);
  assert.equal(countOf(html, 'does not see the ticket-level wait'), 1);
  assert.doesNotMatch(html, /the lab has no verdict for this policy in this window/);
});

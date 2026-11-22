/* prd/68 G1 — the aging fixes (F-03 backend, F-06, F-08, F-13, F-14 label,
 * F-16) that the Jira-only `demo-tlla` corpus exposed.
 *
 * main.js is the wired page, not an importable module: the decisions it owns
 * are extracted from its source and exercised with stubs (the same way the
 * navigation and prd/64 guards work). The aging half imports the real module.
 *
 * Each test fails on `fb046ec`:
 *   · F-06 `only=live` painted the "see re-plan" note above an absent section;
 *   · F-08 the chip and the honesty line labelled `counts.dead` "no live PR";
 *   · F-13 Sources printed `PRs 0 kept of 0 seen` and the combined-tenant join
 *     on a dataset with no PR layer;
 *   · F-14 the stage cell rendered the raw `in_verification` key;
 *   · F-16 a `#scope` value with no `:` became `the undefined queue`.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { agingMarkdown, agingMarkup, agingScopedCounts, agingTable, scopedAgingPayload } from '../aging.js';
import { SCOPE_KINDS } from '../route.js';
import { scopeBanner } from '../scope.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const MAIN = read('main.js');
const main = () => MAIN;

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

/* --------------------------------------------------------------- fixtures */
const HOLDER = (person, hours = 120) => [{ person, role: 'owner', hours }];

function block() {
  return {
    id: 'story_cycle', p50: 90, p75: 104, p85: 120, p95: 172,
    n: 40, min_n: 20, available: true,
    source: 'self-calibrated', source_label: 'self-calibrated (p85 of 40 stories)',
    basis: 'holder-priced, business hours',
  };
}

function storyRow(id, hours, {
  band = 'very_old', live = 'dead', holders = HOLDER('LaNo'), stage = 'cycle',
} = {}) {
  return {
    kind: 'story', object_id: `STORY:${id}`, ticket: id, key: id, pr: null,
    stage, age_hours: hours, calendar_days: Math.round(hours / 8), band,
    multiple: hours / 120, cohort_n: 40, holder: holders, live,
    reason: live === 'dead' ? 'no open PR and no human event for 10 business days' : null,
    open_prs: [], dependents: 0, hours_at_risk: 0,
    href: `#ticket/${id}`, basis: 'story cycle',
  };
}

function replanItem(id, { live = 'dead', person = 'LaNo', hours = 120 } = {}) {
  return {
    object_id: `STORY:${id}`, kind: 'story', ticket: id, pr: null, age_hours: hours,
    reason: live === 'dead'
      ? 'no open PR and no human event for 10 business days'
      : 'no open PR, but a human acted recently',
    holder: HOLDER(person, hours), live, href: `#ticket/${id}`,
  };
}

function payload({
  rows = [], replan = [], counts = null, hidden = null, stage = 'cycle',
} = {}) {
  return {
    ok: true,
    as_of: '2026-11-22T00:14:11Z',
    kind: 'story',
    stage,
    active_threshold: 'story_cycle',
    thresholds: [block()],
    rows,
    counts: counts || {
      open: rows.length,
      live: rows.filter((row) => row.live === 'live').length,
      dead: rows.filter((row) => row.live === 'dead').length,
      no_live_pr: replan.length,
      over_p50: 0,
      over_p85: 0,
      over_p95: 0,
      insufficient: 0,
      chase: 0,
      min_n: 20,
    },
    hidden: hidden || { outside_window: 0, no_live_pr: 0, total: 0, note: null },
    window: { start: '2026-08-24T00:14:11Z', end: '2026-11-22T00:14:11Z' },
    story_stages: ['cycle', 'in_progress', 'in_verification'],
    feed: {
      chase: [], items: [], replan, hours_at_risk: 0,
      threshold_source: 'p85 of 40 stories (120 h)', threshold_hours: 120,
    },
    note: 'An age is a censored duration.',
  };
}

/* ------------------------------------------------------------------ F-06 */
test('prd/68 F-06: `only=live` still renders the window-scoped re-plan feed it points at', () => {
  // the live shape: 0 rows on screen, 103 no-live-PR items below the table
  const items = Array.from({ length: 103 }, (_, index) => replanItem(`DEL-${700 + index}`));
  const data = payload({
    rows: [],
    replan: items,
    hidden: {
      outside_window: 136, no_live_pr: 103, total: 239,
      note: '239 open items are outside this window or have no live PR — see re-plan',
    },
    counts: { open: 0, live: 0, dead: 0, no_live_pr: 103, over_p85: 0, insufficient: 0 },
  });
  const html = agingMarkup(data, { onlyLive: true });
  assert.match(html, /data-aging-hidden>239 open items [^<]*— see re-plan/);
  assert.match(html, /id="aging-replan"/, 'the note names a section that renders');
  assert.match(html, /No live PR — close or re-plan/);
  assert.match(html, /103 items not waiting on a nudge/);
  assert.equal((html.match(/data-replan=/g) || []).length, 103);
  // and the chip names the same population as the table below it
  assert.match(html, /data-aging-counts>0 open · 0 past p85 · 103 no live PR</);
});

test('prd/68 F-06: a person scope filters the re-plan feed even under `only=live`', () => {
  const data = payload({
    rows: [],
    replan: [replanItem('DEL-701'), replanItem('DEL-702', { person: 'MiMe' })],
    hidden: { outside_window: 0, no_live_pr: 2, total: 2, note: '2 open items have no live PR — see re-plan' },
    counts: { open: 0, live: 0, dead: 0, no_live_pr: 2, over_p85: 0, insufficient: 0 },
  });
  const rowFilter = (row) => (row.holder || []).some((holder) => holder.person === 'LaNo');
  const scoped = scopedAgingPayload(data, { onlyLive: true, rowFilter });
  assert.equal(scoped.rows.length, 0, 'no live row is on screen');
  assert.deepEqual(scoped.feed.replan.map((item) => item.ticket), ['DEL-701']);
  assert.equal(scoped.counts.no_live_pr, 1);

  const html = agingMarkup(data, { onlyLive: true, rowFilter });
  assert.match(html, /id="aging-replan"/);
  assert.equal((html.match(/data-replan=/g) || []).length, 1);
  assert.match(html, /data-aging-counts>0 open · 0 past p85 · 1 no live PR</);
});

/* ------------------------------------------------------------------ F-08 */
test('prd/68 F-08: the chip says "no live PR" for dead + unobserved, not dead alone', () => {
  const dead = Array.from({ length: 50 }, (_, index) => storyRow(`DEL-${100 + index}`, 400));
  const unobserved = Array.from({ length: 53 }, (_, index) => storyRow(
    `DEL-${300 + index}`, 200, { live: 'unobserved', holders: HOLDER('MiMe') },
  ));
  const rows = [...dead, ...unobserved];
  const replan = rows.map((row) => replanItem(row.ticket, { live: row.live, person: row.holder[0].person }));
  const data = payload({
    rows,
    replan,
    counts: { open: 103, live: 0, dead: 50, no_live_pr: 103, over_p85: 21, insufficient: 0 },
  });
  const html = agingMarkup(data);
  assert.match(html, /103 no live PR/);
  assert.doesNotMatch(html, /50 no live PR/);
  // the abandoned-only number is still available under its own name
  assert.equal(data.counts.dead, 50);

  // the client-side count (one scope) is the same population as the feed
  const counted = agingScopedCounts(rows, data);
  assert.equal(counted.dead, 50);
  assert.equal(counted.no_live_pr, 103);

  // the tab's honesty line reads the same field
  assert.match(main(), /c\.no_live_pr/);
  assert.doesNotMatch(main(), /c\.dead[^\n]*no live PR/);
});

/* ------------------------------------------------------------------ F-13 */
test('prd/68 F-13: a Jira-only Sources page states its layers, not a PR layer it lacks', () => {
  const layerFacts = compile('layerFacts');
  const sourcesRunNote = compile('sourcesRunNote');
  const jira = {
    prs: 0, commits: 0, events: 33425,
    stories: 2320, epics: 71, sprints: 106,
    prs_seen: 0, prs_kept: 0, prs_dropped: 0,
    runs: [{ source: 'jira-cloud', as_of: '2026-11-22T00:14:11Z', prs_seen: 0, prs_kept: 0, prs_dropped: 0 }],
  };
  assert.deepEqual(layerFacts(jira), [
    ['PR layer', 'not in this dataset'],
    ['stories', '2320'],
    ['epics', '71'],
    ['sprints', '106'],
  ]);
  const note = sourcesRunNote(jira);
  assert.match(note, /1 ingest run from jira-cloud built this dataset/);
  assert.match(note, /Jira half only/);
  assert.match(note, /no PR layer to join to/);
  assert.doesNotMatch(note, /combined tenant/);
  assert.doesNotMatch(note, /PRs had no ticket key/);

  // a combined tenant keeps the join sentence, and its PR facts come back
  const combined = {
    ...jira, prs: 5240, commits: 9000, prs_seen: 5240, prs_kept: 5240, prs_dropped: 0,
    runs: [{ source: 'bitbucket' }, { source: 'jira-cloud' }],
  };
  const facts = layerFacts(combined);
  assert.equal(facts[0][0], 'PRs');
  assert.ok(facts.some(([key]) => key === 'dropped'));
  assert.match(sourcesRunNote(combined), /2 ingest runs from bitbucket \+ jira-cloud/);
  assert.match(sourcesRunNote(combined), /combined tenant joins the PR half/);

  // the page renders those helpers instead of its own PR-centric rows
  assert.match(main(), /\.\.\.layerFacts\(meta\)/);
  assert.match(main(), /esc\(sourcesRunNote\(meta\)\)/);
  assert.doesNotMatch(main(), /\n    \['PRs',/);
});

/* ------------------------------------------------------------------ F-14 */
test('prd/68 F-14: the stage cell reads "in progress" / "in verification"', () => {
  const data = payload({
    rows: [
      storyRow('DEL-810', 420, { stage: 'in_progress', live: 'unobserved' }),
      storyRow('DEL-811', 300, { stage: 'in_verification', live: 'dead' }),
    ],
  });
  const html = agingTable(data);
  assert.match(html, /<span class="mono">in progress<\/span>/);
  assert.match(html, /<span class="mono">in verification<\/span>/);
  assert.doesNotMatch(html, /in_progress|in_verification/);
  // the export cannot disagree with the table
  const markdown = agingMarkdown(payload({
    rows: [storyRow('DEL-811', 300, { stage: 'in_verification' })],
    stage: 'in_verification',
  }));
  assert.match(markdown, /in verification/);
  assert.doesNotMatch(markdown, /in_verification/);
});

/* ------------------------------------------------------------------ F-16 */
test('prd/68 F-16: a malformed scope is the whole team, never "the undefined queue"', () => {
  const normalizeScope = compile('normalizeScope', { SCOPE_KINDS });
  const all = { kind: 'all', value: null, label: 'everything' };
  // the QA repro: the select's value had no `:`, so both halves were empty
  assert.deepEqual(normalizeScope('', undefined), all);
  assert.deepEqual(normalizeScope(undefined, undefined), all);
  // a value outside the closed kind set is not a scope either
  assert.deepEqual(normalizeScope('queue', 'undefined'), all);
  assert.deepEqual(normalizeScope('person', ''), all);
  assert.deepEqual(normalizeScope('person', '   '), all);
  assert.deepEqual(normalizeScope('all', 'all'), all);
  // the closed kinds with a value are kept, and a value may hold a colon
  assert.deepEqual(normalizeScope('person', 'LaNo'),
    { kind: 'person', value: 'LaNo', label: 'LaNo' });
  assert.deepEqual(normalizeScope(' repo ', 'ENG/delivery-api'),
    { kind: 'repo', value: 'ENG/delivery-api', label: 'ENG/delivery-api' });

  // setScope coerces before anything paints the banner, and validateScope too
  assert.match(main(), /\(\{ kind, value \} = normalizeScope\(kind, value\)\)/);
  const validate = functionSource('validateScope');
  assert.match(validate, /normalizeScope\(state\.scope\.kind, state\.scope\.value\)/);
  // and the banner the QA screenshot showed ("the undefined queue") is gone
  assert.equal(scopeBanner(normalizeScope('', undefined), 'delivery'), '');
  assert.equal(scopeBanner(normalizeScope('queue', 'undefined'), 'delivery'), '');
});

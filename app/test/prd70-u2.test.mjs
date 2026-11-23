/* prd/70 U2 — scope & chrome (F-02, F-04, F-08, F-12 dates/Sources, F-15).
 *
 * Each case fails without its fix:
 *   · F-02 `buildScopeOptions`/`validateScope` only read `state.events` (the
 *     newest 40,000 of 76,213), so `DmMa`, `IvZi` and reviewer-only `LeMi2`
 *     were rejected as "does not exist";
 *   · F-04 the forecast card asked `/api/forecast` without `days`, so `1q`
 *     was evaluated over the store's 260 weeks (CV 0.87 → "erratic");
 *   · F-08 the status pill said "40000 events" beside "76213 events";
 *   · F-12 a 2021→2026 range printed without years, and `All` ended 22 days in
 *     the future because the timeline pads its domain;
 *   · F-15 the Jira ingest run read `0 / 0 / 0` in PR columns and hid its
 *     2,391 issues and 33,425 events.
 *
 * main.js is the wired page, not an importable module: the decisions it owns
 * are extracted from its source and exercised with stubs (same pattern as the
 * prd/68 guards). format.js is imported for real.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { DAY, HOUR, fmtCount, fmtDaySpan, fmtRange } from '../format.js';
import { streamIsQueue } from '../scope.js';

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

/* ------------------------------------------------------------- F-02 roster */

/** The combined corpus as the client sees it: capped events, full objects. */
function store() {
  return {
    meta: { bots: ['BoT'], events: 76213 },
    events: [
      { actor: 'LaNo' }, { actor: 'LaNo' }, { actor: 'system' }, { actor: 'BoT' },
    ],
    objects: [
      // PERSON rows: the full identity table, including reviewers who never
      // act and historical actors whose events fall outside the 40k slice.
      { object_type: 'PERSON', object_id: 'PERSON:DmMa', actor: 'DmMa', attributes: { actor_kind: 'human' } },
      { object_type: 'PERSON', object_id: 'PERSON:IvZi', actor: 'IvZi', attributes: { actor_kind: 'human' } },
      { object_type: 'PERSON', object_id: 'PERSON:LeMi2', actor: 'LeMi2', attributes: { actor_kind: 'human' } },
      { object_type: 'PERSON', object_id: 'PERSON:BoT', actor: 'BoT', attributes: { actor_kind: 'bot' } },
      { object_type: 'STORY', object_id: 'STORY:DEMO-1', actor: 'DmMa' },
      { object_type: 'PR', object_id: 'PR:DEMO/widget#1', actor: 'IvZi' },
      { object_type: 'SPRINT', object_id: 'SPRINT:1', actor: null },
    ],
    queues: {
      streams: [
        { id: 'LeMi2', role: 'reviewer', people: ['LeMi2'] },
        { id: 'review', role: 'synthetic', people: [] },
        { id: 'ZzZz', role: 'reviewer', people: ['ZzZz'] },
      ],
    },
    aging: {
      rows: [{
        holder: [{ person: 'HoLd' }, 'StRg'],
        waiting_on: ['WaIt'],
        waiting_roles: { RoLe: 'review' },
      }],
    },
    scope: { kind: 'all', value: null, label: 'everything' },
  };
}

test('prd/70 F-02: the roster is the store, not the newest 40k events', () => {
  const state = store();
  const personRoster = compile('personRoster', { state, streamIsQueue });
  const roster = personRoster();
  // historical actors with no loaded events are still real people
  assert.ok(roster.has('DmMa'));
  assert.ok(roster.has('IvZi'));
  // a reviewer who only ever appears as `event_objects` / STORY owner
  assert.ok(roster.has('LeMi2'));
  // queue streams and aging holders are identities too
  assert.ok(roster.has('ZzZz'));
  assert.ok(roster.has('HoLd'));
  assert.ok(roster.has('StRg'));
  assert.ok(roster.has('WaIt'));
  assert.ok(roster.has('RoLe'));
  // and the loaded events still count (LaNo has 2)
  assert.equal(roster.get('LaNo'), 2);
  // but a bot, the synthetic role stream and `system` are not people scopes
  assert.equal(roster.has('BoT'), false);
  assert.equal(roster.has('review'), false);
  assert.equal(roster.has('system'), false);
});

test('prd/70 F-02: validateScope accepts DmMa/IvZi/LeMi2 and rejects a stranger', () => {
  const state = store();
  const personRoster = compile('personRoster', { state, streamIsQueue });
  const normalizeScope = (kind, value) => {
    const k = String(kind ?? '').trim();
    const v = String(value ?? '').trim();
    if (k === 'all' || !v) return { kind: 'all', value: null, label: 'everything' };
    return { kind: k, value: v, label: v };
  };
  const validateScope = compile('validateScope', {
    state,
    normalizeScope,
    ROLE_QUEUES: ['author', 'review', 'ready', 'sentinel', 'CI', 'calendar'],
    availableRepos: () => [],
    personRoster,
  });
  for (const person of ['DmMa', 'IvZi', 'LeMi2']) {
    state.scope = { kind: 'person', value: person, label: person };
    assert.equal(validateScope(), true, `${person} is a real person scope`);
  }
  // a value this store never names is still a fallback, stated
  state.scope = { kind: 'person', value: 'NoPe', label: 'NoPe' };
  assert.equal(validateScope(), false);
  assert.deepEqual(state.scope, { kind: 'all', value: null, label: 'everything' });
  // a bot is never a person scope, even though its PERSON row exists
  state.scope = { kind: 'person', value: 'BoT', label: 'BoT' };
  assert.equal(validateScope(), false);
});

test('prd/70 F-02: a person: deep link is not rejected before the data arrives', () => {
  const state = { meta: null, events: [], objects: [], queues: null, aging: null };
  const personRoster = compile('personRoster', { state, streamIsQueue });
  const normalizeScope = (kind, value) => ({ kind, value, label: value });
  const validateScope = compile('validateScope', {
    state,
    normalizeScope,
    ROLE_QUEUES: ['author', 'review', 'ready', 'sentinel', 'CI', 'calendar'],
    availableRepos: () => [],
    personRoster,
  });
  state.scope = { kind: 'person', value: 'DmMa', label: 'DmMa' };
  // no roster yet: keep the link, `load()` re-validates after /api/objects
  assert.equal(validateScope(), true);
  assert.equal(state.scope.value, 'DmMa');
});

test('prd/70 F-02: #scope offers DmMa, IvZi and LeMi2', () => {
  const state = store();
  const personRoster = compile('personRoster', { state, streamIsQueue });
  let html = '';
  const scopeSelect = { set innerHTML(value) { html = value; }, set value(_) {} };
  const buildScopeOptions = compile('buildScopeOptions', {
    state,
    personRoster,
    esc: (value) => String(value),
    $: () => scopeSelect,
    availableRoles: () => [],
    filteredPages: () => new Set(),
    availableRepos: () => [],
    repoLabel: (value) => value,
  });
  buildScopeOptions();
  assert.match(html, /value="person:DmMa"/);
  assert.match(html, /value="person:IvZi"/);
  assert.match(html, /value="person:LeMi2"/);
  assert.doesNotMatch(html, /value="person:BoT"/);
  assert.doesNotMatch(html, /value="person:system"/);
  // the busiest loaded person still sorts first, then the roster by name
  assert.ok(html.indexOf('person:LaNo') < html.indexOf('person:DmMa'));
});

/* ----------------------------------------------------------- F-04 forecast */

test('prd/70 F-04: /api/forecast carries the active timeframe\'s days', () => {
  const forecastQuery = compile('forecastQuery', {
    timeframeDays: () => 90,
    allDays: () => 1882,
  });
  assert.equal(forecastQuery({ target: 'sprint:18568' }),
    'target=sprint%3A18568&days=90');
  assert.equal(
    forecastQuery({ target: 'sprint:18568', by: '2026-12-01', n: 41 }),
    'target=sprint%3A18568&days=90&by=2026-12-01&n=41',
  );
  // `All` is still the whole store, never the API's 90-day default
  const all = compile('forecastQuery', {
    timeframeDays: () => null,
    allDays: () => 1882,
  });
  assert.equal(all({ target: 'sprint:18568' }), 'target=sprint%3A18568&days=1882');
  assert.equal(all({}), 'days=1882');
  // the card and the deadline control both ask through that one helper
  assert.match(functionSource('loadForecast'), /forecastQuery\(/);
  assert.match(main(), /data-forecast-by-input[\s\S]{0,400}forecastQuery\(/);
  // and a timeframe change refetches the card (a new window is a new question)
  assert.match(functionSource('ensureData'), /loadForecast\(loadSeq\)/);
});

/* ---------------------------------------------------------------- F-08 bar */

test('prd/70 F-08: the status pill names loaded-of-total, not 40000 vs 76213', () => {
  const state = {
    meta: { events: 76213 },
    eventTotal: 76213,
    events: new Array(40000).fill({}),
    tenant: 'demo-tlla',
  };
  const liveStatusText = compile('liveStatusText', {
    state, isPhone: () => false, fmtCount,
  });
  assert.equal(liveStatusText(), 'live · 40,000 of 76,213 events loaded · demo-tlla');
  // loading the full range reconciles to one number, the store's own
  state.events = new Array(76213).fill({});
  assert.equal(liveStatusText(), 'live · 76,213 events · demo-tlla');
  // the phone keeps the bare liveness dot (counts live in #provenance)
  const phone = compile('liveStatusText', { state, isPhone: () => true, fmtCount });
  assert.equal(phone(), 'live');
  // the provenance line and the status read the same snapshot age helper
  assert.match(main(), /Snapshot from \$\{snapshotAge\(\)\}/);
  assert.match(main(), /\(\$\{snapshotAge\(\)\}\)/);
});

/* ------------------------------------------------------------- F-12 dates */

test('prd/70 F-12: a range that crosses years prints both years', () => {
  const start = Date.parse('2021-09-28T00:00:00Z');
  const end = Date.parse('2026-11-21T00:00:00Z');
  assert.equal(fmtDaySpan(start, end), '28 Sep 2021 → 21 Nov 2026');
  assert.equal(fmtRange(start, end), '28 Sep 2021 → 21 Nov 2026');
  // one autumn inside one year stays short
  assert.equal(
    fmtDaySpan(Date.parse('2026-09-28T00:00:00Z'), Date.parse('2026-11-21T00:00:00Z')),
    '28 Sep → 21 Nov',
  );
  // a short window that crosses a year boundary is unambiguous too
  assert.equal(
    fmtRange(Date.parse('2026-12-20T10:00:00Z'), Date.parse('2027-01-05T09:00:00Z')),
    '20 Dec 2026 10:00 → 5 Jan 2027 09:00',
  );
  // this is the exact Sources store span the QA screenshot showed yearless
  const sourcesSpan = fmtDaySpan(Date.parse('2021-09-28T00:00:00Z'), Date.parse('2026-11-21T00:00:00Z'));
  assert.notEqual(sourcesSpan, '28 Sep → 21 Nov');
  // Sources and the top bar use the same formatter
  assert.match(main(), /\['store span', `\$\{meta\.first_ts && meta\.last_ts/);
  assert.match(main(), /fmtDaySpan\(window\.start, window\.end\)/);
});

test('prd/70 F-12: `All` prints the data range, never the padded future end', () => {
  const state = {
    meta: {
      first_ts: '2021-09-28T02:00:00Z',
      last_ts: '2026-11-21T12:00:00Z',
      as_of: '2026-11-22T00:14:11Z',
    },
  };
  const dataWindow = compile('dataWindow', { state });
  // the timeline's `fit()` pads 1.5 % each side: 2026-12-14 is 22 days ahead
  const padded = {
    start: Date.parse('2021-09-20T00:00:00Z'),
    end: Date.parse('2026-12-14T00:00:00Z'),
  };
  const shown = dataWindow(padded.start, padded.end);
  assert.equal(new Date(shown.start).toISOString().slice(0, 10), '2021-09-28');
  assert.equal(new Date(shown.end).toISOString().slice(0, 10), '2026-11-21');
  assert.equal(fmtRange(shown.start, shown.end), '28 Sep 2021 → 21 Nov 2026');
  // a window inside the data is untouched (a zoom is still the zoom)
  const inside = { start: Date.parse('2026-10-01T00:00:00Z'), end: Date.parse('2026-11-01T00:00:00Z') };
  assert.deepEqual(dataWindow(inside.start, inside.end), inside);
  // the timeline paints the clamped window, not the raw view
  assert.match(main(), /stats\.total \? dataWindow\(stats\.start, stats\.end\)/);
  assert.match(main(), /const shown = dataWindow\(view\.start, view\.end\)/);
});

/* ----------------------------------------------------------- F-15 sources */

function combined() {
  return {
    meta: { stories: 2320, epics: 71, prs: 2574 },
    counts: { total: 76213, by_repo: { 'DEMO/widget': 42788, null: 33425 }, by_activity: {} },
  };
}

test('prd/70 F-15: the Jira run reports its issues and events, not zero PRs', () => {
  const { meta, counts } = combined();
  const sourceRunRow = compile('sourceRunRow', {});
  const jira = sourceRunRow({
    source: 'jira-cloud', as_of: '2026-11-22T00:14:11Z',
    prs_seen: 0, prs_kept: 0, prs_dropped: 0,
  }, meta, counts);
  assert.equal(jira.kind, 'jira');
  assert.deepEqual(jira.issues, { seen: 2391, kept: 2391 });
  // no PR columns at all: the row cannot read 0 / 0 / 0
  assert.equal(jira.prs, null);
  assert.equal(jira.dropped, null);
  // 33,425 Jira events (the repo-less bucket), 42,788 PR events
  assert.equal(jira.events, 33425);

  const hans = sourceRunRow({
    source: 'hans', as_of: '2026-11-22T05:00:44Z',
    prs_seen: 10733, prs_kept: 2574, prs_dropped: 8159,
  }, meta, counts);
  assert.equal(hans.kind, 'pr');
  assert.deepEqual(hans.prs, { seen: 10733, kept: 2574 });
  assert.equal(hans.issues, null);
  assert.equal(hans.dropped, 8159);
  assert.equal(hans.events, 42788);
});

test('prd/70 F-15: a newer payload wins; a missing split is honest', () => {
  const sourceRunRow = compile('sourceRunRow', {});
  const explicit = sourceRunRow({
    source: 'jira-cloud', prs_seen: 0,
    issues_seen: 2391, issues_kept: 2391, events_written: 33425,
  }, {}, null);
  assert.deepEqual(explicit.issues, { seen: 2391, kept: 2391 });
  assert.equal(explicit.events, 33425);
  // nothing to derive from: `—`, never a made-up 0
  const unknown = sourceRunRow({ source: 'jira-cloud', prs_seen: 0 }, {}, null);
  assert.equal(unknown.issues.seen, null);
  assert.equal(unknown.events, null);
});

test('prd/70 F-15: the Sources table has per-source columns and event counts', () => {
  const source = functionSource('renderSourcesPage');
  assert.match(source, /<th class="num">issues<\/th>/);
  assert.match(source, /<th class="num">PRs<\/th>/);
  assert.match(source, /<th class="num">events<\/th>/);
  assert.match(source, /runs\.map\(\(run\) => \{/);
  assert.match(source, /data-source="\$\{esc\(run\.source\)\}"/);
  assert.match(source, /sourceRunRow\(run, meta, state\.counts\)/);
  // the old PR-only header is gone
  assert.doesNotMatch(source, /prs\s*\n?\s*\? '<th class="num">PRs seen/);
  // thousands separated for the 5-digit counts the QA reader sees
  assert.equal(fmtCount(2391), '2,391');
  assert.equal(fmtCount(33425), '33,425');
  assert.equal(fmtCount(76213), '76,213');
});

/* A guard for the two format constants the year rule depends on. */
test('prd/70 F-12: the multi-year threshold is ~11 months, not a day count', () => {
  const near = Date.parse('2026-01-01T00:00:00Z');
  // 300 days inside one year keeps short labels
  assert.equal(fmtDaySpan(near, near + 300 * DAY), '1 Jan → 28 Oct');
  // more than ~11 months needs the year even within one calendar year
  assert.match(fmtDaySpan(near, near + 340 * DAY), /2026/);
  // a 30-day window keeps its clocks (no regression in the short branch)
  const end = Date.parse('2026-10-01T10:00:00Z');
  assert.equal(fmtRange(end - 30 * DAY, end), '1 Sep 10:00 → 1 Oct 10:00');
  // and a sub-hour window still shows both clock ends
  assert.equal(fmtRange(end - 2 * HOUR, end), '1 Oct 08:00 → 1 Oct 10:00');
});

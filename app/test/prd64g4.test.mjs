/* prd/64 G4 — state & overlay bugs in app/main.js (F-04, F-09, F-10, F-11,
 * F-12, F-13).
 *
 * main.js is the wired page, not an importable module, so the decisions it owns
 * are extracted from its source and exercised with stubs (the same way the
 * navigation guards work). Each test names one finding and fails on the code
 * before the fix.
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { PHASE } from '../timeline.js';
import { detailMarkup, entityMarkdown } from '../tables.js';

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

/* ------------------------------------------------------------ F-04 levels */

test('F-04: Overview queries its own canonical levels, so toggles cannot move it', () => {
  const canonicalQueues = { level: 'person', streams: [{ id: 'FrMo' }] };
  const canonicalBlocking = { level: 'queue', totals: { hours: 304 }, edges: [] };

  function render(queueLevel, blockingLevel) {
    const calls = { markup: null };
    const state = {
      verdict: null,
      delivery: null,
      overviewQueues: canonicalQueues,
      overviewBlocking: canonicalBlocking,
      // the Queue/Blocking toggles may be anything; Overview must not read them
      queueLevel,
      blockingLevel,
      scope: { kind: 'all', value: null, label: 'everything' },
    };
    const renderOverviewPage = compile('renderOverviewPage', {
      state,
      $: () => ({ set innerHTML(value) { calls.html = value; } }),
      verdictCard: () => '',
      overviewMarkup: (args) => { calls.markup = args; return ''; },
      overviewCounts: () => null,
      setHonesty: () => {},
      appBase: () => '',
      OVERVIEW_QUEUE_LEVEL: 'person',
    });
    renderOverviewPage();
    return calls.markup;
  }

  const before = render('person', 'queue');
  const toggled = render('queue', 'person');
  // the same canonical payloads and the same levels are passed in both states
  assert.equal(before.queues, canonicalQueues);
  assert.equal(before.blocking, canonicalBlocking);
  assert.equal(before.queueLevel, 'person');
  assert.deepEqual(toggled, before);

  // the page body cannot reach the toggled payloads at all
  const src = functionSource('renderOverviewPage');
  assert.match(src, /queues: state\.overviewQueues/);
  assert.match(src, /blocking: state\.overviewBlocking/);
  assert.match(src, /queueLevel: OVERVIEW_QUEUE_LEVEL/);
  assert.doesNotMatch(src, /state\.queueLevel|state\.blockingLevel/);
  assert.doesNotMatch(src, /queues: state\.queues|blocking: state\.blocking/);
});

test('F-04: the canonical Overview payloads are fetched at the canonical level', () => {
  assert.match(MAIN, /const OVERVIEW_QUEUE_LEVEL = 'person';/);
  assert.match(MAIN, /const OVERVIEW_BLOCKING_LEVEL = 'queue';/);
  const fetch = functionSource('ensureData');
  assert.match(fetch, /const blockingKey = `blocking:\$\{tenant\}:\$\{state\.timeframe\}:\$\{OVERVIEW_BLOCKING_LEVEL\}/);
  assert.match(fetch, /const queueKey = `queues:\$\{tenant\}:\$\{state\.timeframe\}:\$\{OVERVIEW_QUEUE_LEVEL\}/);
  assert.match(fetch, /\/api\/blocking\?level=\$\{OVERVIEW_BLOCKING_LEVEL\}/);
  assert.match(fetch, /\/api\/queues\?level=\$\{OVERVIEW_QUEUE_LEVEL\}/);
  assert.match(fetch, /state\.overviewBlocking = cached\(blockingKey\)/);
  assert.match(fetch, /state\.overviewQueues = cached\(queueKey\)/);
});

/* -------------------------------------------------------- F-09 route notice */

test('F-09: an unknown ?view= lands on the default entry view, not the stored page', () => {
  const unknownRouteView = compile('unknownRouteView', {
    PAGES: { overview: {}, delivery: {}, timeline: {} },
  });
  assert.equal(unknownRouteView('bogus'), true);
  assert.equal(unknownRouteView('timeline'), false);
  assert.equal(unknownRouteView(null), false);
  // a ticket hash is its own destination, even beside a stale `view=`
  assert.equal(unknownRouteView('bogus', true), false);
  assert.match(MAIN, /if \(unknownRouteView\(rawRouteView, Boolean\(initialRoute\.ticket\)\)\) \{\n  state\.page = DEFAULT_ENTRY_PAGE;/);
});

test('F-09: the tenant load appends its notice and edits clear it', () => {
  // loadTenants() must not wipe the invalid-view/timeframe notice from boot
  const tenants = functionSource('loadTenants');
  assert.match(tenants, /No dataset “\$\{requested\}”/);
  assert.match(tenants, /\{ append: true \}/);

  // a user edit to timeframe clears the route notice; a route restore does not
  const notices = [];
  const state = {
    sprint: null, page: 'overview', timeframe: '1w', timeframeCustom: false,
    tfWindow: null, cache: { clear() {} },
  };
  const setTimeframe = compile('setTimeframe', {
    TIMEFRAMES: { '1q': { days: 90 }, '4w': { days: 28 } },
    state,
    timeline: { setSprintFocus() {} },
    renderSprintStrip() {},
    renderTimeframe() {},
    applyTimeframeToTimeline() {},
    ensureData: () => Promise.resolve(),
    renderPage() {},
    setStatus() {},
    setNotice: (text) => notices.push(text),
  });
  setTimeframe('1q');
  assert.deepEqual(notices, ['']);
  setTimeframe('4w', { silent: true });
  assert.deepEqual(notices, ['']);
  // scope and tenant edits clear too
  assert.match(functionSource('setScope'), /if \(!silent\) setNotice\(''\);/);
  assert.match(functionSource('setTenant'), /if \(!silent\) setNotice\(''\);/);
});

/* ------------------------------------------------------ F-10 unpressed time */

test('F-10: off the Timeline the preset stays pressed despite a sprint lens', () => {
  function render(page, timeframeCustom) {
    const buttons = ['1w', '4w', '1q', 'all'].map((tf) => ({
      dataset: { tf },
      attrs: {},
      classes: new Set(),
      setAttribute(key, value) { this.attrs[key] = value; },
      classList: { toggle(name, on) { if (on) this.owner.classes.add(name); else this.owner.classes.delete(name); } },
    }));
    for (const button of buttons) button.classList.owner = button;
    const chip = { hidden: false, attrs: {}, setAttribute(key, value) { this.attrs[key] = value; } };
    const range = { textContent: '' };
    const elements = {
      timeframe: { querySelectorAll: () => buttons },
      'tf-custom': chip,
      range,
    };
    const state = { page, timeframe: '1q', timeframeCustom };
    const renderTimeframe = compile('renderTimeframe', {
      state,
      $: (id) => elements[id],
      presetWindow: () => ({ start: 0, end: 1 }),
      dataWindow: (start, end) => ({ start, end }),
      fmtRange: () => '20 Aug → 18 Nov',
      syncRangeMeta() {},
      timeline: { captureView: () => ({ start: 0, end: 1 }) },
    });
    renderTimeframe();
    return {
      pressed: buttons.filter((button) => button.attrs['aria-pressed'] === 'true')
        .map((button) => button.dataset.tf),
      lit: buttons.filter((button) => button.classes.has('on')).map((button) => button.dataset.tf),
      chip,
    };
  }

  // a sprint lens left custom on Timeline...
  const timeline = render('timeline', true);
  assert.deepEqual(timeline.pressed, []);
  assert.equal(timeline.chip.hidden, false);

  // ...must not leave every preset unpressed on a view that queries the preset
  const overview = render('overview', true);
  assert.deepEqual(overview.pressed, ['1q']);
  assert.deepEqual(overview.lit, ['1q']);
  assert.equal(overview.chip.hidden, true);
  assert.match(MAIN, /const custom = state\.page === 'timeline' && state\.timeframeCustom;/);
});

/* --------------------------------------------------- F-11 dead inspector actions */

test('F-11: a #delivery-detail chip acts on state.selection', () => {
  const detailEntity = compile('detailEntity');
  const rowEntity = { row: { object_id: 'PR:DEL/delivery-api#66' } };
  const drawerEntity = { pr: { id: 'PR:other#1' } };
  const closed = { closest: () => null };
  const open = { closest: (selector) => (selector === '#drawer' ? {} : null) };
  const state = { selection: rowEntity, drawerEntity: null };

  // desktop inspector: the drawer is closed, the row is the selection
  assert.equal(detailEntity(closed, state), rowEntity);
  // inside the drawer the drawer's entity still wins
  assert.equal(detailEntity(open, { selection: rowEntity, drawerEntity }), drawerEntity);
  // no selection falls back to the drawer entity, as before
  assert.equal(detailEntity(closed, { selection: null, drawerEntity }), drawerEntity);

  const handler = MAIN.slice(MAIN.indexOf("const pivot = event.target.closest('[data-detail]')"));
  assert.match(handler, /const entity = detailEntity\(pivot, state\);/);
  assert.match(handler, /if \(action === 'copy'\) \{\n      await copyMarkdown\(entity\);/);
  assert.match(handler, /const scope = drawerTimelineScope\(entity\);/);
  assert.match(handler, /entity\?\.row\?\.object_id/);
  assert.match(handler, /focusBlockingRow\(selectedRow\)/);
});

test('F-11: Copy as Markdown never flashes "copied" for an empty export', () => {
  const fn = functionSource('copyMarkdown');
  assert.match(fn, /const markdown = entity \? entityMarkdown/);
  assert.match(fn, /if \(!markdown\) \{[\s\S]*?nothing selected to copy as Markdown[\s\S]*?return;/);
  assert.match(fn, /await copyText\(markdown\);/);
  assert.doesNotMatch(fn, /copyText\(''\)|copyText\(entity \?/);
  // a #delivery-detail row entity exports the row's own link, not ""
  const entity = {
    row: {
      object_id: 'PR:DEL/delivery-api#66', number: 66, repo_short: 'delivery-api',
      tickets: ['DEL-183'], author: 'BoFe', state: 'open', blocked: true,
      waiting_since: '2026-11-13T13:48:00Z', waiting_hours: 49, age_hours: 49,
    },
  };
  const markdown = entityMarkdown(entity, 'wall-clock', 'http://127.0.0.1:8798/?tenant=del-combined');
  assert.match(markdown, /DEL-183/);
  assert.match(markdown, /#66 delivery-api/);
  assert.match(MAIN, /function focusBlockingRow\(row\)/);
});

/* --------------------------------------------------------- F-12 drawer facts */

test('F-12: every drawer <dt> is unique and the measured tail explains #66', () => {
  const constLine = MAIN.match(/const MEASURED_PHASES = \[[^\]]*\];/)[0];
  const measuredPhaseFacts = new Function(
    'PHASE',
    `${constLine}\n${functionSource('measuredPhaseFacts')}\nreturn measuredPhaseFacts;`,
  )(PHASE);

  // #66: the live status says the author has the ball, but the 2 d 1 h is in
  // the unobserved tail, so the measured `waiting` phase is truly empty.
  const phaseFacts = measuredPhaseFacts({
    hoursOf: (phase) => (phase === 'unobserved' ? '2 d 1 h' : '—'),
  });
  const facts = [
    ['waiting on author', 'yes'],
    ['CI', 'not running'],
    ['rebasing', 'no'],
    ['no events since', '2026-11-13 13:48 UTC'],
    ...phaseFacts,
  ];
  const html = detailMarkup({
    overline: 'PULL REQUEST',
    row: {
      object_id: 'PR:DEL/delivery-api#66', number: 66, repo_short: 'delivery-api',
      tickets: ['DEL-183'], author: 'BoFe', state: 'open',
    },
    facts,
  });
  const keys = [...html.matchAll(/<dt>(.*?)<\/dt>/g)].map((match) => match[1]);
  assert.equal(new Set(keys).size, keys.length, `duplicate <dt> keys: ${keys.join(', ')}`);

  // the live status rows are the only bare keys, and they sit above PHASES
  for (const bare of ['waiting on author', 'CI', 'rebasing', 'no events since']) {
    assert.equal(keys.filter((key) => key === bare).length, 1, bare);
  }
  assert.ok(keys.includes('PHASES'));
  assert.ok(keys.indexOf('PHASES') > keys.indexOf('no events since'));
  for (const measured of [
    'waiting on author · measured', 'in progress · measured', 'in review · measured',
    'CI · measured', 'rebasing · measured',
  ]) {
    assert.equal(keys.filter((key) => key === measured).length, 1, measured);
  }

  // yes / — / unobserved tail: one explanation, no contradiction
  assert.match(html, /<dt>waiting on author<\/dt><dd class="mono">yes<\/dd>/);
  assert.match(html, /<dt>waiting on author · measured<\/dt><dd class="mono">—<\/dd>/);
  assert.match(html, /<dt>unobserved \(no events since the snapshot\)<\/dt><dd class="mono">2 d 1 h<\/dd>/);

  // the live fact never carries the duration (that is the phase/tail row's job)
  assert.match(MAIN, /\['waiting on author', row\.author_open \? 'yes' : 'no'\]/);
  assert.doesNotMatch(MAIN, /waiting on author', row\.author_open \? `yes · \$\{fmtHours/);
});

/* -------------------------------------------------- F-13 sprint lens window */

test('F-13: clearing the sprint lens restores the active preset window', () => {
  const state = { sprint: '1', timeframeCustom: true };
  const calls = { applied: 0, rendered: 0, strip: 0, synced: 0, saved: 0 };
  let focus = '1';
  const clearSprintFocus = compile('clearSprintFocus', {
    state,
    timeline: { setSprintFocus: (id) => { focus = id; } },
    applyTimeframeToTimeline: () => { calls.applied += 1; },
    renderTimeframe: () => { calls.rendered += 1; },
    renderSprintStrip: () => { calls.strip += 1; },
    syncUrl: () => { calls.synced += 1; },
    saveUI: () => { calls.saved += 1; },
  });
  clearSprintFocus();

  assert.equal(state.sprint, null);
  assert.equal(focus, null);
  assert.equal(state.timeframeCustom, false);
  assert.equal(calls.applied, 1);
  assert.equal(calls.rendered, 1);
  assert.equal(calls.strip, 1);
  // the clear path re-applies the preset after dropping the lens
  const fn = functionSource('clearSprintFocus');
  assert.ok(fn.indexOf('state.sprint = null') < fn.indexOf('applyTimeframeToTimeline();'));
  // clicking the active sprint row toggles the same lens off
  assert.match(MAIN, /String\(state\.sprint\) === String\(sprintRow\.dataset\.sprintId\)[\s\S]*?clearSprintFocus\(\);/);
});

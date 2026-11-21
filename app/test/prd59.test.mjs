/* prd/59 — aging WIP: in-flight age against the team's own finished work.
 *
 * The Delivery list's Aging tab must answer one question with one clock per
 * row: how old is this item compared with how long *this* team's finished work
 * takes? These tests pin the strip's ticks (p50/p85/p95, with n), the band
 * vocabulary and its colours (state only), the ranked chase order, the
 * insufficient-history state, the phone markup (tick line, no chart, no
 * overflow) and the Markdown export order. The wiring assertions cover the tab
 * and the endpoint call that only exist in main.js/index.html.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  BAND_LABEL, agingChart, agingCount, agingMarkdown, agingMarkup, agingRows,
  agingTable, agingTicks, agingViewOptions, bandBar, bandClass, chaseItems,
  holderLabel, multipleLabel, parseAgingView, thresholdOf, tickLine,
  workingDaysLabel,
} from '../aging.js';
import { TABS } from '../tables.js';
import { parseRoute, serializeRoute, ROUTE_TABS } from '../route.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

function row(objectId, ticket, hours, band, {
  kind = 'pr', pr = null, stage = 'review', multiple = null, live = 'live',
  holder = [], dependents = 0, risk = null, calendarDays = 3, repo = 'delivery-api',
} = {}) {
  return {
    kind,
    object_id: objectId,
    ticket,
    pr: pr || objectId.slice(objectId.lastIndexOf('#')),
    repo,
    repo_short: repo,
    stage,
    age_hours: hours,
    calendar_days: calendarDays,
    band,
    multiple: multiple ?? (hours / 22.66),
    rank_pct: 1,
    cohort_n: 106,
    holder,
    live,
    reason: null,
    dependents,
    hours_at_risk: risk ?? Math.max(0, hours - 22.66),
    href: `#ticket/${ticket}`,
    basis: 'review',
  };
}

const FRMO = [{ person: 'FrMo', role: 'sentinel', hours: 144.2 }];

function payload(overrides = {}) {
  const rows = [
    row('PR:DEL/delivery-api#60', 'DEL-176', 144.2, 'very_old', { pr: '#60', holder: FRMO, dependents: 1, risk: 121.5 }),
    row('PR:DEL/delivery-api#53', 'DEL-167', 192.0, 'very_old', { pr: '#53', holder: [
      { person: 'CaOs', role: 'review', hours: 192 }, { person: 'FrMo', role: 'sentinel', hours: 192 },
    ], risk: 169.3 }),
    row('PR:DEL/delivery-api#55', 'DEL-168', 154.7, 'very_old', { pr: '#55', holder: FRMO, risk: 132.0 }),
    row('PR:DEL/delivery-api#66', 'DEL-183', 2.1, 'normal', { pr: '#66', holder: [
      { person: 'BoFe', role: 'author', hours: 2.1 }, { person: 'FrMo', role: 'sentinel', hours: 2.1 },
    ] }),
  ];
  return {
    ok: true,
    as_of: '2026-11-18T00:10:44Z',
    kind: 'pr',
    stage: 'review',
    active_threshold: 'review_time',
    thresholds: [{
      id: 'review_time', label: 'Time in review', unit: 'hours',
      p50: 13.0, p75: 18.3, p85: 22.7, p95: 31.2, n: 106, min_n: 20,
      available: true, source: 'self-calibrated', source_label: 'p85 of 106 merged PRs (22.7 h)',
      basis: 'merged PRs, role-level, holder clock',
    }],
    rows,
    counts: { open: 4, live: 4, dead: 0, over_p50: 3, over_p85: 3, over_p95: 3, insufficient: 0, chase: 3 },
    review_p75: { value: 18.3, n: 106, available: true },
    feed: {
      chase: ['PR:DEL/delivery-api#53', 'PR:DEL/delivery-api#55', 'PR:DEL/delivery-api#60'],
      items: [
        { object_id: 'PR:DEL/delivery-api#53', ticket: 'DEL-167', age_hours: 192, band: 'very_old', holder: [], hours_at_risk: 169.3, href: '#ticket/DEL-167' },
        { object_id: 'PR:DEL/delivery-api#55', ticket: 'DEL-168', age_hours: 154.7, band: 'very_old', holder: [], hours_at_risk: 132.0, href: '#ticket/DEL-168' },
        { object_id: 'PR:DEL/delivery-api#60', ticket: 'DEL-176', age_hours: 144.2, band: 'very_old', holder: [], hours_at_risk: 121.5, href: '#ticket/DEL-176' },
      ],
      hours_at_risk: 422.8,
      threshold_source: 'p85 of 106 merged PRs (22.7 h)',
      replan: [],
    },
    stages: ['review', 'cycle'],
    note: 'Ages are business hours on the holder’s calendar.',
    ...overrides,
  };
}

// --------------------------------------------------------------------- ticks
test('prd/59: the strip renders the three ticks with their labels and n', () => {
  const data = payload();
  const ticks = agingTicks(data);
  assert.deepEqual(ticks.map((tick) => tick.id), ['p50', 'p75', 'p85', 'p95']);
  const html = agingChart(data);
  assert.match(html, /<svg/);
  for (const id of ['p50', 'p85', 'p95']) {
    assert.match(html, new RegExp(`data-tick="${id}"`), `tick ${id} missing`);
  }
  assert.match(html, /p50/);
  assert.match(html, /p85/);
  assert.match(html, /n = 106 finished item/);
  // one dot per in-flight row, no connecting line and no area
  assert.equal((html.match(/data-aging-dot=/g) || []).length, 4);
  assert.doesNotMatch(html, /<path|<polyline|<polygon/);
  // the tick line names all the percentiles the block carries
  assert.match(tickLine(data), /p50/);
  assert.match(tickLine(data), /p85/);
  assert.equal(thresholdOf(data), data.thresholds[0]);
});

test('prd/59: exactly p85 is aging — the band vocabulary is the API’s', () => {
  // The server owns the band; the view must not invent a different one.
  assert.deepEqual(Object.keys(BAND_LABEL), ['normal', 'watch', 'aging', 'very_old', 'insufficient']);
  assert.equal(bandClass('aging'), 'waiting');
  assert.equal(bandClass('very_old'), 'blocked');
  // colour = state only: normal and watch stay neutral
  assert.equal(bandClass('normal'), 'neutral');
  assert.equal(bandClass('watch'), 'neutral');
  assert.equal(bandClass('insufficient'), 'neutral');
});

test('prd/59: the row’s band bar puts the item on the p50/p85/p95 scale', () => {
  const data = payload();
  const html = bandBar(data.rows[1], data); // 192 h, very old
  assert.match(html, /class="band-bar band-very_old"/);
  for (const id of ['p50', 'p85', 'p95']) {
    assert.match(html, new RegExp(`data-tick="${id}"`));
  }
  // the mark is at the item's own position, to the right of the p95 tick
  const mark = Number(/(?:class="mark"|class="mark" )? style="left:([\d.]+)%"/.exec(html)?.[1]
    || /left:([\d.]+)%"\><\/b>/.exec(html)?.[1]);
  assert.ok(mark > 0 && mark <= 100);
});

// ---------------------------------------------------------------- the list
test('prd/59: the list is ranked by hours at risk, not by key or age', () => {
  const data = payload();
  const order = agingRows(data).map((entry) => entry.pr);
  assert.deepEqual(order, ['#53', '#55', '#60', '#66']);
  assert.deepEqual(chaseItems(data).map((item) => item.ticket), ['DEL-167', 'DEL-168', 'DEL-176']);
  assert.equal(agingCount(data), 4);
  assert.match(multipleLabel(data.rows[0]), /6\.4×/);
  assert.equal(multipleLabel({ multiple: null }), '—');
});

test('prd/59: every row links to #ticket/KEY and names its holder', () => {
  const html = agingTable(payload());
  assert.match(html, /href="#ticket\/DEL-176"/);
  assert.match(html, /href="#ticket\/DEL-167"/);
  assert.match(html, /FrMo \(sentinel\)/);
  assert.match(html, /CaOs \(review\) \+ FrMo \(sentinel\)/);
  assert.equal(holderLabel({ holder: [] }), '—');
  // the band pill is the only coloured band mark: very old is critical
  assert.match(html, /data-band-pill="very_old">very old/);
  assert.match(html, /pill neutral" data-band-pill="normal">normal/);
});

test('prd/59: only-live hides abandoned work and the count follows', () => {
  const dead = row('STORY:DEL-114', 'DEL-114', 516, 'very_old', {
    kind: 'story', stage: 'in_review', live: 'dead', calendarDays: 92,
  });
  const data = payload({ kind: 'story', stage: 'story_cycle', rows: [dead, payload().rows[0]] });
  assert.equal(agingRows(data).length, 2);
  assert.equal(agingRows(data, { onlyLive: true }).length, 1);
  const html = agingMarkup(data, { onlyLive: true });
  assert.match(html, /data-aging-only aria-pressed="true"/);
  assert.doesNotMatch(html, /data-aging="STORY:DEL-114"/);
});

test('prd/59: insufficient history replaces the chart instead of drawing one', () => {
  const data = payload({
    thresholds: [{
      id: 'review_time', label: 'Time in review', p50: null, p85: null, p95: null,
      n: 12, min_n: 20, available: false, source: 'self-calibrated',
      note: 'insufficient history: 12 finished items (needs 20) — no percentile shown',
      basis: 'merged PRs',
    }],
    rows: [{ ...row('PR:x#1', 'DEL-1', 40, 'insufficient', { pr: '#1', risk: 0 }), multiple: null }],
  });
  const html = agingChart(data);
  assert.doesNotMatch(html, /<svg/);
  assert.match(html, /insufficient history/i);
  assert.match(html, /12 finished items \(needs 20\)/);
  assert.match(html, /no percentile shown/);
  // the row says insufficient, and there is no fabricated multiple
  const table = agingTable(data);
  assert.match(table, /data-band="insufficient"/);
  assert.doesNotMatch(table, /\d×/);
});

// -------------------------------------------------------------------- phone
test('prd/59 phone: a tick line, no chart, no horizontal overflow', () => {
  const phone = agingChart(payload(), { phone: true });
  assert.doesNotMatch(phone, /<svg/);
  assert.match(phone, /data-aging-ticks/);
  assert.match(phone, /p85/);
  // a phone row keeps the ticket key and both units, in a two-line shape
  const table = agingTable(payload());
  assert.match(table, /data-label="age"/);
  assert.match(table, /data-label="holder"/);
  assert.match(table, /data-label="state"/);
  const css = read('style.css');
  assert.match(css, /@media \(max-width: 640px\) \{[\s\S]*\.aging \.c-band, \.aging \.c-kind, \.aging \.c-stage, \.aging \.c-mult \{ display: none; \}/);
  assert.match(css, /\.aging-controls \.chip \{ min-height: 44px; \}/);
  // the second line of a stacked row is the working-day span
  assert.equal(workingDaysLabel(464), '58 working days');
});

// ------------------------------------------------------------------ markdown
test('prd/59: Copy as Markdown exports the ranked list in table order', () => {
  const text = agingMarkdown(payload(), { base: 'http://localhost:8766/' });
  const lines = text.split('\n');
  const rows = lines.filter((line) => line.startsWith('| very old') || line.startsWith('| normal'));
  assert.equal(rows.length, 4);
  assert.match(rows[0], /DEL-167/);      // 192 h first
  assert.match(rows[1], /DEL-168/);
  assert.match(rows[2], /DEL-176/);
  assert.match(rows[3], /DEL-183/);
  assert.match(text, /# Aging — PR review/);
  assert.match(text, /n = 106/);
  assert.match(text, /8\.5×/);           // 192 / 22.7
  assert.match(text, /\[DEL-167\]\(http:\/\/localhost:8766\/#ticket\/DEL-167\)/);
});

// ------------------------------------------------------------- wiring + view
test('prd/59: the Aging tab sits beside the existing Delivery lists', () => {
  assert.deepEqual(TABS.map((tab) => tab.id), ['blocked', 'oldest', 'aging', 'all']);
  assert.equal(TABS.find((tab) => tab.id === 'aging').label, 'Aging');
  const main = read('main.js');
  assert.match(main, /state\.tab = tab\.dataset\.tab/);
  assert.match(main, /agingMarkup\(state\.aging/);
  assert.match(main, /\/api\/aging\?kind=/);
  assert.match(main, /data-aging-only/);
  assert.match(main, /data-aging-view/);
  assert.match(main, /reloadAging\(\)/);
  assert.match(main, /data-tab="aging"|go\.dataset\.tab/);
  // the table and the tab count share one scope filter
  assert.match(main, /function agingMatchesScope\(row, scope\)/);
  assert.match(main, /rows\.filter\(\(row\) => agingMatchesScope\(row, scope\)\)/);
  // the tab is route state, so `?tab=aging` is shareable
  assert.match(main, /tab: state\.page === 'delivery' \? state\.tab : null/);
});

test('prd/59: the view selector offers story cycle, PR review, PR cycle and stages', () => {
  const story = payload({
    kind: 'story', stages: ['cycle', 'in_progress', 'in_review'],
    story_stages: ['cycle', 'in_progress', 'in_review'],
  });
  const options = agingViewOptions(story).map((option) => option.value);
  assert.deepEqual(options, [
    'story:cycle', 'pr:review', 'pr:cycle', 'story:in_progress', 'story:in_review',
  ]);
  // a PR payload carries the story stages too, so the control never loses them
  const pr = payload({ story_stages: ['cycle', 'in_review'] });
  assert.deepEqual(agingViewOptions(pr).map((option) => option.value), [
    'story:cycle', 'pr:review', 'pr:cycle', 'story:in_review',
  ]);
  assert.deepEqual(parseAgingView('story:in_review'), { kind: 'story', stage: 'in_review' });
  assert.deepEqual(parseAgingView('pr:review'), { kind: 'pr', stage: 'review' });
  assert.deepEqual(parseAgingView('nonsense'), { kind: 'story', stage: 'nonsense' });
  // the tab's own markup carries the controls
  const html = agingMarkup(payload(), { view: 'pr:review' });
  assert.match(html, /data-aging-view/);
  assert.match(html, /<option value="pr:review" selected>PR review<\/option>/);
  assert.match(html, /data-aging-source/);
  assert.match(html, /p85 of 106 merged PRs/);
});

test('prd/59: `?tab=aging` round-trips and an unknown tab is ignored', () => {
  assert.deepEqual(ROUTE_TABS, ['blocked', 'oldest', 'aging', 'all']);
  const query = serializeRoute({ tenant: 'del-combined', view: 'delivery', tab: 'aging' });
  assert.match(query, /tab=aging/);
  assert.equal(parseRoute(query, '').tab, 'aging');
  assert.equal(parseRoute('?view=delivery&tab=bogus', '').tab, null);
});

test('prd/59: the honesty line states the clock, the cohort and censoring', () => {
  const html = agingMarkup(payload());
  assert.match(html, /person on vacation accrues none/);
  assert.match(html, /Calendar days in brackets/);
  assert.match(html, /censored duration/);
  assert.match(html, /never add it to a cycle-time median/);
  // no live PR rows are never in the chase list
  const data = payload({
    rows: [row('STORY:DEL-114', 'DEL-114', 516, 'very_old', { kind: 'story', live: 'dead' })],
    feed: {
      chase: [], items: [], hours_at_risk: 0,
      replan: [{ object_id: 'STORY:DEL-114', ticket: 'DEL-114', age_hours: 516, reason: 'no open PR and no human event for 10 business days', href: '#ticket/DEL-114' }],
    },
  });
  const markup = agingMarkup(data);
  assert.match(markup, /No live PR — close or re-plan/);
  assert.match(markup, /data-replan="STORY:DEL-114"/);
});

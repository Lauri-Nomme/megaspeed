/* prd/65 group A — the v0.12.0 final-QA fixes on the Aging tab, the phone Trend
 * bar and the annotation confirmation (bugs N-01, N-03, N-04, N-05, N-06, N-07,
 * N-08).
 *
 * Each test fails without its fix:
 *   · N-01 repo scope dropped every story row client-side (badge 0, chip 7);
 *   · N-03 ten Trend tabs overflowed a 412 px phone to 717 px;
 *   · N-04 the /ws watcher echo replaced "recorded …" ~1 s after the write;
 *   · N-05 person/role scope filtered the table but not chip/chart/honesty/re-plan;
 *   · N-06 desktop percentile tick labels overlapped by up to 45 px;
 *   · N-07 the censoring and vacation sentences printed twice;
 *   · N-08 `story` and `91 calendar days` were clipped to `sto…`/`91 calendar…`.
 *
 * The geometric assertions replay the harness measurements taken at 1600×1000
 * (Chromium, del-combined): tick boxes p50 194–269.9, p85 281.9–357.7,
 * p95 369.7–445.6 (12 px gaps, no intersection); `.c-kind` 68/68,
 * `.c-age` 195/195, re-plan WHY 708/708 — no scrollWidth > clientWidth.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  agingChart, agingMarkup, agingRows, agingScopedCounts, agingTable,
  replanList, scopedAgingPayload,
} from '../aging.js';
import { matchesScope } from '../tables.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const main = () => read('main.js');
const css = () => read('style.css');

/** The source text of one function declaration in main.js (braces balanced). */
function extract(src, name) {
  const start = src.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `function ${name} not found in main.js`);
  let depth = 0;
  for (let i = src.indexOf('{', start); i < src.length; i += 1) {
    const ch = src[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (!depth) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unclosed function ${name}`);
}

/** `agingMatchesScope` from main.js, wired to the real `matchesScope`. */
function agingMatchesScope() {
  const body = extract(main(), 'agingMatchesScope');
  return new Function('matchesScope', `${body}\nreturn agingMatchesScope;`)(matchesScope);
}

/** `@media (max-width: 640px) { … }` blocks, brace balanced. */
function phoneBlocks(source) {
  const blocks = [];
  const needle = '@media (max-width: 640px)';
  for (let at = source.indexOf(needle); at >= 0; at = source.indexOf(needle, at + 1)) {
    let depth = 0;
    for (let i = source.indexOf('{', at); i < source.length; i += 1) {
      if (source[i] === '{') depth += 1;
      else if (source[i] === '}') {
        depth -= 1;
        if (!depth) { blocks.push(source.slice(at, i + 1)); break; }
      }
    }
  }
  return blocks;
}

const FRMO = [{ person: 'FrMo', role: 'sentinel', hours: 120 }];
const CAOS = [{ person: 'CaOs', role: 'review', hours: 120 }];

function storyRow(id, hours, band, { holders = FRMO, live = 'live', age = null } = {}) {
  // A story aging row carries no `repo`/`repo_short` (analysis/aging.py
  // `_story_rows`) — that is exactly what broke N-01.
  return {
    kind: 'story',
    object_id: `STORY:${id}`,
    ticket: id,
    key: id,
    pr: null,
    stage: 'cycle',
    age_hours: age == null ? hours : age,
    calendar_days: Math.round((hours / 8) * 1.4),
    band,
    multiple: hours / 20,
    cohort_n: 40,
    holder: holders,
    live,
    reason: live === 'dead' ? 'no open PR and no human event for 10 business days' : null,
    open_prs: [],
    dependents: 0,
    hours_at_risk: Math.max(0, hours - 20),
    href: `#ticket/${id}`,
    basis: 'story cycle',
  };
}

function prRow(id, hours, band, repo, holders = FRMO) {
  return {
    kind: 'pr',
    object_id: `PR:${repo}#${id}`,
    ticket: `DEL-${id}`,
    pr: `#${id}`,
    repo,
    repo_short: repo.split('/').pop(),
    stage: 'review',
    age_hours: hours,
    calendar_days: 4,
    band,
    multiple: hours / 22.7,
    cohort_n: 106,
    holder: holders,
    live: 'live',
    reason: null,
    open_prs: [`PR:${repo}#${id}`],
    dependents: 0,
    hours_at_risk: Math.max(0, hours - 22.7),
    href: `#ticket/DEL-${id}`,
    basis: 'review',
  };
}

function payload(rows, { kind = 'story', replan = [], chase = [], counts = null } = {}) {
  return {
    ok: true,
    as_of: '2026-11-18T00:10:44Z',
    kind,
    stage: kind === 'pr' ? 'review' : 'cycle',
    active_threshold: kind === 'pr' ? 'review_time' : 'story_cycle',
    thresholds: [{
      id: kind === 'pr' ? 'review_time' : 'story_cycle',
      p50: kind === 'pr' ? 13 : 90,
      p75: kind === 'pr' ? 18.3 : 104,
      p85: kind === 'pr' ? 22.7 : 120,
      p95: kind === 'pr' ? 31.2 : 172,
      n: kind === 'pr' ? 106 : 40,
      min_n: 20,
      available: true,
      source: 'self-calibrated',
      source_label: 'self-calibrated (p85 of 40 stories)',
      basis: 'holder-priced, business hours',
    }],
    rows,
    counts: counts || {
      open: rows.length,
      live: rows.filter((row) => row.live === 'live' || row.live === 'unmerged').length,
      dead: rows.filter((row) => row.live === 'dead').length,
      over_p50: 0,
      over_p85: rows.filter((row) => row.band === 'aging' || row.band === 'very_old').length,
      over_p95: rows.filter((row) => row.band === 'very_old').length,
      insufficient: 0,
      chase: chase.length,
      min_n: 20,
    },
    story_stages: ['cycle', 'in_review', 'done'],
    feed: {
      chase,
      items: chase.map((id) => ({ object_id: id, ticket: id, age_hours: 200, band: 'very_old', holder: FRMO, hours_at_risk: 180, href: `#ticket/${id}` })),
      replan: replan.map((id) => ({ object_id: id, ticket: id, age_hours: 300, reason: 'no open PR and no human event for 10 business days', href: `#ticket/${id}` })),
      hours_at_risk: 180,
      threshold_source: 'p85 of 40 stories (120 h)',
    },
    note: 'An age is a censored duration — the item has not finished yet; compare it with finished work, never add it to a cycle-time median. Ages are business hours on the holder’s calendar; a person on vacation accrues none. Calendar days in brackets.',
  };
}

const sevenStories = () => ([
  storyRow('DEL-101', 516, 'very_old'),
  storyRow('DEL-102', 400, 'very_old', { live: 'dead', holders: [] }),
  storyRow('DEL-103', 300, 'very_old', { live: 'dead', holders: [] }),
  storyRow('DEL-104', 200, 'very_old', { live: 'dead', holders: [] }),
  storyRow('DEL-105', 150, 'very_old', { live: 'dead', holders: [] }),
  storyRow('DEL-106', 100, 'normal'),
  storyRow('DEL-107', 40, 'normal'),
]);

// --------------------------------------------------------------------- N-01
test('prd/65 N-01: a repo scope keeps the story rows the server already filtered', () => {
  const matches = agingMatchesScope();
  const scope = { kind: 'repo', value: 'DEL/delivery-api', label: 'DEL/delivery-api' };
  const stories = sevenStories();

  // story rows carry no repo; the endpoint's `?repo=` is the scope, so they stay
  assert.ok(stories.every((row) => row.repo === undefined));
  assert.ok(stories.every((row) => matches(row, scope) === true));

  // a PR row still proves its repo — the page must not trust a name it has
  const kept = prRow(60, 150, 'very_old', 'DEL/delivery-api');
  const other = prRow(61, 150, 'very_old', 'DEL/other-api');
  assert.equal(matches(kept, scope), true);
  assert.equal(matches(other, scope), false);
  // and a person scope still filters both kinds by holder
  assert.equal(matches(stories[1], { kind: 'person', value: 'FrMo' }), false);
  assert.equal(matches(stories[0], { kind: 'person', value: 'FrMo' }), true);

  // badge, table, chip and dots agree at 7 (the QA repro said 0 / 0 / 7 / 7)
  const data = payload(stories, {
    replan: ['DEL-102', 'DEL-103', 'DEL-104', 'DEL-105'],
    chase: ['DEL-101'],
  });
  const rowFilter = (row) => matches(row, scope);
  const html = agingMarkup(data, { rowFilter, view: 'story:cycle' });
  assert.equal(agingRows(data).filter(rowFilter).length, 7, 'tab badge');
  assert.equal((html.match(/class="aging-row"/g) || []).length, 7, 'table rows');
  assert.equal((html.match(/data-aging-dot=/g) || []).length, 7, 'strip dots');
  assert.match(html, /data-aging-counts>7 open · 5 past p85 · 4 no live PR</);
  assert.doesNotMatch(html, /data-aging-empty/);
  assert.equal(scopedAgingPayload(data, { rowFilter }).counts.open, 7);
});

// --------------------------------------------------------------------- N-03
test('prd/65 N-03: the phone Trend metric bar wraps instead of pushing the page to 717 px', () => {
  const source = css();
  const blocks = phoneBlocks(source);
  assert.ok(blocks.length, 'the phone media query exists');
  const wrap = blocks.filter((block) => /#trend-metrics\s*\{[^}]*flex-wrap:\s*wrap/.test(block));
  assert.equal(wrap.length, 1, '#trend-metrics wraps inside a @media (max-width: 640px) block');
  assert.match(wrap[0], /#trend-metrics\s*\{[^}]*max-width:\s*100%/);
  // no phone block keeps the bar on one line
  for (const block of blocks) {
    assert.doesNotMatch(block, /#trend-metrics[^{]*\{[^}]*flex-wrap:\s*nowrap/);
    assert.doesNotMatch(block, /#trend-metrics[^{]*\{[^}]*white-space:\s*nowrap/);
  }
  // the desktop block is untouched: the wrap lives only in the phone query
  const desktop = source.slice(0, source.indexOf('@media (max-width: 640px)'));
  assert.doesNotMatch(desktop, /#trend-metrics/);
});

// --------------------------------------------------------------------- N-04
test('prd/65 N-04: the watcher echo cannot overwrite the annotation confirmation', async () => {
  const names = ['setStatus', 'flashStatus', 'flashStatusHeld', 'refreshStatus'];
  const bodies = names.map((name) => extract(main(), name)).join('\n');
  const statusElement = { textContent: '', className: '' };
  const state = { meta: { as_of: '2026-11-18T00:10:44Z' }, events: [1], tenant: 'del-combined' };
  const live = 'live · 2529 events · del-combined';
  const factory = new Function('$', 'state', 'liveStatusText', `
    let statusTimer = 0;
    let statusHeldUntil = 0;
    ${bodies}
    return { setStatus, flashStatus, flashStatusHeld, refreshStatus };
  `);
  const ui = factory((id) => (id === 'status' ? statusElement : null), state, () => live);

  ui.flashStatusHeld('recorded review SLA 4 h', 'ok', 1500);
  assert.equal(statusElement.textContent, 'recorded review SLA 4 h');
  await new Promise((resolve) => setTimeout(resolve, 1000)); // the /ws echo tick
  ui.refreshStatus(); // what load() does when the watcher broadcasts `changed`
  assert.equal(statusElement.textContent, 'recorded review SLA 4 h', 'the echo kept the flash');

  // another writer's change still lands as an explicit status, and the next
  // liveness refresh after that is not suppressed
  ui.setStatus('offline', 'stale');
  assert.equal(statusElement.textContent, 'offline');
  ui.refreshStatus();
  assert.equal(statusElement.textContent, live);

  // the confirmation ends on its own timer, so the bar never keeps claiming it
  ui.flashStatusHeld('annotation deleted', 'ok', 20);
  ui.refreshStatus();
  assert.equal(statusElement.textContent, 'annotation deleted');
  await new Promise((resolve) => setTimeout(resolve, 40));
  ui.refreshStatus();
  assert.equal(statusElement.textContent, live);

  // both write paths use the held flash, and the echo paths go through it
  const source = main();
  const record = source.slice(source.indexOf('async function recordAnnotation'));
  assert.match(record.slice(0, 2000), /flashStatusHeld\(`recorded /);
  const remove = source.slice(source.indexOf('async function deleteAnnotation'));
  assert.match(remove.slice(0, 2000), /flashStatusHeld\('annotation deleted'\)/);
  const fast = source.slice(source.indexOf('if (!force && state.stamp === stamp'));
  assert.ok(fast.length > 100);
  assert.match(fast.slice(0, 600), /refreshStatus\(\);/);
});

// --------------------------------------------------------------------- N-05
test('prd/65 N-05: person scope makes chip, dots, honesty and re-plan agree with the table', () => {
  const matches = agingMatchesScope();
  const rows = [
    ...sevenStories(),
    prRow(53, 192, 'very_old', 'DEL/delivery-api', CAOS), // the one CaOs waits on
  ];
  const data = payload(rows, {
    kind: 'story',
    replan: ['DEL-102', 'DEL-103', 'DEL-104', 'DEL-105'],
    chase: ['PR:DEL/delivery-api#53'],
  });
  const rowFilter = (row) => matches(row, { kind: 'person', value: 'CaOs' });
  const html = agingMarkup(data, { rowFilter });

  // chip, dots and table read the scoped payload …
  assert.match(html, /data-aging-counts>1 open · 1 past p85/);
  assert.equal((html.match(/data-aging-dot=/g) || []).length, 1);
  assert.equal((html.match(/class="aging-row"/g) || []).length, 1);
  // … and the re-plan list does not keep the team's dead stories
  assert.doesNotMatch(html, /id="aging-replan"/);
  assert.equal(agingRows(data).filter(rowFilter).length, 1, 'tab badge');

  // the honesty line's source of truth: one scoped payload, every surface
  const scoped = scopedAgingPayload(data, { rowFilter });
  assert.equal(scoped.counts.open, 1);
  assert.equal(scoped.counts.over_p85, 1);
  assert.equal(scoped.feed.items.length, 1);
  assert.equal(scoped.feed.replan.length, 0);
  assert.match(main(), /const a = scopedAgingPayloadNow\(sprintKeys\)/);

  // Story cycle under the same scope agrees at 0 everywhere
  const none = (row) => matches(row, { kind: 'person', value: 'CaOs' }) && row.kind === 'story';
  const empty = agingMarkup(data, { rowFilter: none });
  assert.match(empty, /data-aging-counts>0 open · 0 past p85/);
  assert.equal((empty.match(/data-aging-dot=/g) || []).length, 0);
  assert.equal((empty.match(/class="aging-row"/g) || []).length, 0);
  assert.match(empty, /data-aging-empty/);
  assert.doesNotMatch(empty, /id="aging-replan"/);
  const scopedEmpty = scopedAgingPayload(data, { rowFilter: none });
  assert.equal(scopedEmpty.counts.open, 0);
  assert.equal(scopedEmpty.feed.items.length, 0);
  assert.equal(scopedEmpty.feed.replan.length, 0);
});

// --------------------------------------------------------------------- N-06
test('prd/65 N-06: percentile tick labels are one flow row and cannot intersect', () => {
  const data = payload(sevenStories());
  const html = agingChart(data);
  // the labels are no longer absolutely positioned at their x
  assert.doesNotMatch(html, /aging-tick-labels/);
  assert.doesNotMatch(html, /data-tick="[^"]+"\s+style="left:/);
  const row = /<div class="aging-ticks" data-aging-ticks>([\s\S]*?)<\/div>/.exec(html);
  assert.ok(row, 'the strip opens with a tick row');
  const labels = [...row[1].matchAll(/<span[^>]*>([^<]+)<\/span>/g)].map((m) => m[1].trim());
  assert.deepEqual(labels, ['p50 11 d 2 h', 'p75 13 d 0 h', 'p85 15 d 0 h', 'p95 21 d 4 h']);
  // the durations stay readable on hover
  assert.match(html, /title="p50 = 11 d 2 h"/);

  // flow layout: the boxes of the harness run (Chromium, 1600×1000, 6.36 px per
  // monospace glyph at 10.5 px, 12 px gap) never intersect
  const GAP = 12;
  const CHAR = 6.36;
  const WIDTH = 1500;
  let x = 0;
  let y = 0;
  let lineH = 0;
  const boxes = labels.map((text) => {
    const w = text.length * CHAR;
    if (x + w > WIDTH) { x = 0; y += 18; }
    const box = { text, l: x, r: x + w, t: y, b: y + 15 };
    x += w + GAP;
    lineH = Math.max(lineH, box.b);
    return box;
  });
  for (let i = 0; i < boxes.length; i += 1) {
    assert.ok(boxes[i].r <= WIDTH, `${boxes[i].text} fits the axis`);
    for (let j = i + 1; j < boxes.length; j += 1) {
      const a = boxes[i];
      const b = boxes[j];
      const hit = a.l < b.r && b.l < a.r && a.t < b.b && b.t < a.b;
      assert.equal(hit, false, `${a.text} must not overlap ${b.text}`);
    }
  }
  // and the stylesheet no longer positions a label at a percentage
  const source = css();
  assert.doesNotMatch(source, /\.aging-tick-labels/);
  assert.match(source, /\.aging-ticks\s*\{[^}]*display:\s*flex[^}]*flex-wrap:\s*wrap/);

  // the four-label PR-review row is the QA worst case (30–45 px of overlap)
  const prs = payload([
    prRow(60, 150, 'very_old', 'DEL/delivery-api'),
    prRow(53, 192, 'very_old', 'DEL/delivery-api', CAOS),
  ], { kind: 'pr' });
  const pr = agingChart(prs);
  const prRowText = /<div class="aging-ticks" data-aging-ticks>([\s\S]*?)<\/div>/.exec(pr)[1];
  const prLabels = [...prRowText.matchAll(/<span[^>]*>([^<]+)<\/span>/g)].map((m) => m[1].trim());
  assert.equal(prLabels.length, 4);
  let px = 0;
  for (const label of prLabels) {
    const w = label.length * CHAR;
    assert.ok(px + w <= WIDTH);
    px += w + GAP;
  }
});

// --------------------------------------------------------------------- N-07
test('prd/65 N-07: the censoring and vacation sentences render exactly once', () => {
  const html = agingMarkup(payload(sevenStories()), {});
  const count = (needle) => html.split(needle).length - 1;
  assert.equal(count('An age is a censored duration'), 1);
  assert.equal(count('a person on vacation accrues none'), 1);
  assert.equal(count('Calendar days in brackets'), 1);
  // the payload's own note is not appended a second time
  assert.doesNotMatch(html, /cycle-time median\. An age is a censored duration/);
  // and the assumption bar no longer repeats it either
  assert.doesNotMatch(main(), /\$\{agingData\.note\}/);
});

// --------------------------------------------------------------------- N-08
test('prd/65 N-08: the desktop aging columns fit their content and the re-plan reason wraps', () => {
  const source = css();
  const widthOf = (selector) => {
    const rule = new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`).exec(source);
    assert.ok(rule, `${selector} rule`);
    return Number(/width:\s*(\d+)px/.exec(rule[1])[1]);
  };
  // `story` needs 59 px of content (QA: scrollWidth 59 > clientWidth 58);
  // `64 d 4 h · 91 calendar days` needs more than 170 px. Both now fit.
  assert.ok(widthOf('.aging .c-kind') >= 68, 'kind fits `story`');
  assert.ok(widthOf('.aging .c-age') >= 195, 'age fits the business-hours + calendar-day pair');

  // the re-plan WHY cell was `nowrap` + ellipsis (170/619): it wraps now, and
  // it takes the panel's spare width
  assert.match(source, /\.aging-replan td\.c-who\s*\{[^}]*white-space:\s*normal/);
  assert.match(source, /\.aging-replan \.table \.c-who\s*\{[^}]*width:\s*auto/);
  assert.doesNotMatch(source, /\.aging-replan td\.c-who\s*\{[^}]*text-overflow:\s*ellipsis/);

  // the markup that the widths must fit: a real `story` row and its age cell
  const html = agingTable(payload(sevenStories()), {});
  assert.match(html, /<td class="c-kind"[^>]*><span class="mono">story<\/span><\/td>/);
  assert.match(html, /109 calendar days|calendar days/);
  const replan = replanList(payload([], { replan: ['DEL-102'] }));
  assert.match(replan, /no open PR and no human event for 10 business days/);
  assert.doesNotMatch(replan, /h…|…</);
});

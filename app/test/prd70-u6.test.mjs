/* prd/70 U6 — polish regressions.
 *
 * F-13: the Aging list's TICKET / PR cell clipped every PR row to
 *       `DEMO-58511 #1…` at 1600 px (`.aging .c-id { width: 144px }`).
 * F-14: an Epic page rendered open child stories as `open · open`.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { epicMarkdown, epicMarkup } from '../ticket.js';
import { agingTable } from '../aging.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const css = () => read('style.css');

/* ---------------------------------------------------------------- F-13 */

const WIDTH = (source, selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const rule = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`).exec(source);
  assert.ok(rule, `${selector} rule`);
  return Number(/width:\s*(\d+)px/.exec(rule[1])[1]);
};

function prRow({ ticket = 'DEMO-58511', pr = '#11021', repo = 'widget' } = {}) {
  return {
    object_id: `PR:${pr}`, kind: 'pr', ticket, pr, repo_short: repo,
    stage: 'review', age_hours: 130, calendar_days: 8, band: 'aging', live: 'live',
    holder: [{ person: 'review queue' }], multiple: 1.2, hours_at_risk: 130,
    dependents: 2,
  };
}

function agingPayload(rows) {
  return {
    kind: 'pr', stage: 'review', active_threshold: 'self',
    thresholds: [{ id: 'self', available: true, n: 286, p50: 60, p85: 120, p95: 200,
      source: 'self', source_label: 'calibrated' }],
    counts: { open: rows.length, over_p85: 0 }, hidden: {}, window: {},
    rows, feed: { replan: [] },
  };
}

test('prd/70 F-13: the aging id column fits the whole key + PR number at 1600 px', () => {
  const source = css();
  const id = WIDTH(source, '.aging .c-id');
  // The row renders `DEMO-58511 #11021 widget` (25 glyphs) in a monospace
  // cell; the 144 px cell left it 192 px of text in a 124 px box, so every PR
  // row truncated to `#1…`. 6.6 px/glyph is the conservative figure the
  // neighbouring prd/68 F-14 test uses for `--row-font` monospace.
  const needed = 'DEMO-58511 #11021 widget'.length * 6.6;
  assert.ok(id >= needed + 16, `id column ${id} px holds ${needed.toFixed(0)} px of text`);
  // …and the whole table still fits the 1600×1000 delivery panel (clientWidth
  // 1048), so nothing scrolls sideways to compensate.
  const total = ['.aging .c-band', '.aging .c-kind', '.aging .c-id', '.aging .c-stage',
    '.aging .c-age', '.aging .c-mult', '.aging .c-holder', '.aging .c-live']
    .map((selector) => WIDTH(source, selector))
    .reduce((sum, width) => sum + width, 0);
  assert.ok(total <= 1048, `aging columns total ${total} px`);
  // The id takes its width from the row's own tighter padding, not from the
  // 10 px full-width-table default — and only on a desktop, where the table
  // has fixed widths at all (a phone card sets its own padding and resets
  // every column to `width: auto`).
  assert.match(source, /@media \(min-width: 641px\) \{\s*\.aging th, \.aging td \{ padding: 0 8px; \}/);
  assert.match(source, /\.aging \.c-id, \.aging \.c-age, \.aging \.c-holder, \.aging \.c-live \{ width: auto; \}/);
});

test('prd/70 F-13: a PR row carries the key, the PR number and the repo', () => {
  // The cell is a CSS problem, not a markup one: the full id must be in the
  // DOM (and in that order) or no width can reveal it.
  const html = agingTable(agingPayload([prRow({ pr: '#11091' })]), { base: '', onlyLive: true });
  const cell = /<td class="c-id"[^>]*>([\s\S]*?)<\/td>/.exec(html);
  assert.ok(cell, 'id cell');
  const text = cell[1].replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
  assert.equal(text, 'DEMO-58511 #11091 widget');
});

/* ---------------------------------------------------------------- F-14 */

const epic = (stories) => ({
  key: 'DEMO-56715', kind: 'epic',
  answer: 'DEMO-56715 is an epic — a parent grouping of 2 stories.',
  epic: { state: 'open', story_count: stories.length, points: 5, stories },
});

const OPEN_CHILD = {
  key: 'DEMO-58516', state: 'open', censored: true, points: 3, cycle_hours: null,
};
const DONE_CHILD = {
  key: 'DEMO-58515', state: 'done', censored: true, points: 2, cycle_hours: 20,
};

test('prd/70 F-14: an open child story renders one state token, not `open · open`', () => {
  const html = epicMarkup(epic([OPEN_CHILD, DONE_CHILD]));
  assert.doesNotMatch(html, /open · open/);
  assert.match(html, /<span class="pill waiting">open<\/span>/);
  // a child that is still open under another state keeps the censoring suffix
  assert.match(html, /<span class="pill waiting">done · open<\/span>/);
});

test('prd/70 F-14: the Epic page and its Markdown export say the same state', () => {
  const payload = epic([OPEN_CHILD, DONE_CHILD]);
  const md = epicMarkdown(payload, '');
  assert.doesNotMatch(md, /open · open/);
  assert.match(md, /\| \[DEMO-58516\]\([^)]*\) \| open \|/);
  assert.match(md, /\| \[DEMO-58515\]\([^)]*\) \| done · open \|/);
});

/* ------------------------------------------------- F-03: epic-only PRs */

/** One PR that names the epic key and no story — the dead end QA found. */
const EPIC_PR = {
  object_id: 'PR:11030', number: 11030, repo: 'DEMO/widget', state: 'open',
  author: 'DmMa', ticket_keys: ['DEMO-56715'], opened: '2026-11-01T09:00:00Z',
  closed: null, censored: true, commit_count: 3, cycle_hours: 130,
  review_rounds: 2, push_classes: { initial: 1 }, intervals: [],
  opened_after_close: false, approved_unmerged: null,
};

test('prd/70 F-03: an epic lists the PRs that name it, in the story page\'s table', () => {
  const payload = {
    ...epic([OPEN_CHILD, DONE_CHILD]),
    prs: [EPIC_PR], pr_count: 1,
  };
  const html = epicMarkup(payload);
  // the section and its clickable rows: the same `.ticket-prs` table a story
  // page renders, so the row opens the PR drawer and the columns mean the same
  assert.match(html, /data-epic-prs="1"/);
  assert.match(html, /pull requests · 1/);
  assert.match(html, /<table class="table ticket-prs">/);
  assert.match(html, /<tr class="pr-row" data-pr="PR:11030"/);
  assert.match(html, /<span class="mono id">#11030<\/span>/);
  assert.match(html, /<span class="repo">widget<\/span>/);
  // the child stories keep their own (single) state token
  assert.doesNotMatch(html, /open · open/);
  assert.match(html, /<span class="pill waiting">open<\/span>/);
});

test('prd/70 F-03: an epic with no PR of its own does not render an empty section', () => {
  const html = epicMarkup({ ...epic([OPEN_CHILD]), prs: [], pr_count: 0 });
  assert.doesNotMatch(html, /data-epic-prs/);
  assert.doesNotMatch(html, /ticket-prs/);
  assert.doesNotMatch(html, /pull requests/);
});

test('prd/70 F-03: the Epic Markdown export lists the same PRs as the page', () => {
  const md = epicMarkdown({
    ...epic([OPEN_CHILD, DONE_CHILD]), prs: [EPIC_PR], pr_count: 1,
  }, '');
  assert.match(md, /## pull requests \(1\)/);
  assert.match(md, /- #11030 DEMO\/widget · open \(right-censored\) · 16 d 2 h · 2 review rounds/);
  assert.doesNotMatch(epicMarkdown(epic([OPEN_CHILD]), ''), /## pull requests/);
});

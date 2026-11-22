/* prd/67 W-03 — the Aging tab is window-scoped, defaults to live, and says so.
 *
 * The bug: `/api/aging?days=90` on a Jira-only store returned 239 rows, all
 * dead/unobserved, up to 1,243 calendar days old. The server now hides a story
 * that did not move in the window; these tests pin the client half — the
 * `only=live` default, the "N open items are outside this window …" note, and
 * an empty state that names the window and the hidden count.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  agingHidden, agingHiddenNote, agingMarkup, agingTable, agingWindowLabel,
  agingMarkdown,
} from '../aging.js';

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const WINDOW = { start: '2026-10-23T00:00:00Z', end: '2026-11-22T00:00:00Z' };

function row(objectId, ticket, hours, band, { kind = 'story', live = 'live' } = {}) {
  return {
    kind,
    object_id: objectId,
    ticket,
    pr: null,
    stage: 'cycle',
    age_hours: hours,
    calendar_days: Math.round(hours / 24),
    band,
    multiple: 2.5,
    rank_pct: 1,
    cohort_n: 22,
    holder: [],
    live,
    reason: null,
    dependents: 0,
    hours_at_risk: 12.1,
    href: `#ticket/${ticket}`,
    basis: 'completed stories’ created → done',
  };
}

const BLOCK = {
  id: 'story_cycle', label: 'Story cycle', unit: 'hours',
  p50: 30, p85: 60, p95: 90, n: 22, min_n: 20, available: true,
  source: 'self-calibrated', source_label: 'p85 of 22 completed stories (60 h)',
  basis: 'completed stories’ created → done',
};

function payload({ rows = [], hidden = {}, window = WINDOW } = {}) {
  return {
    ok: true,
    kind: 'story',
    stage: 'story_cycle',
    active_threshold: 'story_cycle',
    window,
    thresholds: [BLOCK],
    rows,
    counts: { open: rows.length, live: rows.filter((r) => r.live === 'live').length },
    hidden: { outside_window: 0, no_live_pr: 0, total: 0, note: null, ...hidden },
    feed: { chase: [], items: [], replan: [], hours_at_risk: 0 },
    story_stages: ['cycle'],
    note: 'Ages are business hours.',
  };
}

// ------------------------------------------------------------------- default
test('prd/67 W-03: the Aging tab opens on only=live', () => {
  const main = read('main.js');
  // the state default, next to the aging view controls
  assert.match(main, /agingView: 'story:cycle',[\s\S]{0,200}?agingOnly: true,/);
  // the request and every renderer follow that state (the toggle still works)
  assert.match(main, /&only=\$\{state\.agingOnly \? 'live' : 'all'\}/);
  assert.match(main, /state\.agingOnly = !state\.agingOnly/);
  assert.match(main, /agingMarkup\(state\.aging, \{[\s\S]{0,200}?onlyLive: state\.agingOnly/);
});

// --------------------------------------------------------------------- copy
test('prd/67 W-03: the note says how many open items the window/live filter hid', () => {
  assert.equal(agingHiddenNote(payload()), '');
  assert.equal(
    agingHiddenNote(payload({ hidden: { outside_window: 8 } })),
    '8 open items are outside this window — see re-plan',
  );
  assert.equal(
    agingHiddenNote(payload({ hidden: { no_live_pr: 9 } })),
    '9 open items have no live PR — see re-plan',
  );
  assert.equal(
    agingHiddenNote(payload({ hidden: { outside_window: 8, no_live_pr: 1 } })),
    '9 open items are outside this window or have no live PR — see re-plan',
  );
  assert.equal(
    agingHiddenNote(payload({ hidden: { outside_window: 1 } })),
    '1 open item is outside this window — see re-plan',
  );
  // the numbers are read from the payload, not recomputed from the rows
  assert.deepEqual(
    agingHidden(payload({ hidden: { outside_window: 8, no_live_pr: 1 } })),
    { outside_window: 8, no_live_pr: 1, total: 9 },
  );
});

test('prd/67 W-03: the tab renders the note above the list', () => {
  const html = agingMarkup(payload({
    rows: [row('STORY:DEL-400', 'DEL-400', 70, 'aging')],
    hidden: { outside_window: 8, no_live_pr: 1 },
  }));
  assert.match(html, /data-aging-hidden>9 open items are outside this window or have no live PR — see re-plan</);
  // nothing hidden → no note at all
  const quiet = agingMarkup(payload({ rows: [row('STORY:DEL-400', 'DEL-400', 70, 'aging')] }));
  assert.doesNotMatch(quiet, /data-aging-hidden/);
});

// --------------------------------------------------------------- empty state
test('prd/67 W-03: an empty list names the window and the hidden count', () => {
  const html = agingTable(payload({
    hidden: { outside_window: 8, no_live_pr: 1 },
  }), { onlyLive: true });
  assert.match(html, /data-aging-empty/);
  assert.match(html, /with a live PR/);
  assert.match(html, /Window: 23 Oct 2026 → 22 Nov 2026\./);
  assert.match(html, /9 open items are outside this window or have no live PR — see re-plan\./);
  assert.equal(agingWindowLabel(payload()), '23 Oct 2026 → 22 Nov 2026');
  // a payload without a window (an old server) still renders the sentence
  assert.equal(agingWindowLabel(payload({ window: null })), '');
  const bare = agingTable(payload({ window: null }));
  assert.match(bare, /real result\./);
  assert.doesNotMatch(bare, /Window:/);
});

test('prd/67 W-03: the Markdown export carries the same hidden count', () => {
  const text = agingMarkdown(payload({
    rows: [row('STORY:DEL-400', 'DEL-400', 70, 'aging')],
    hidden: { outside_window: 8 },
  }));
  assert.match(text, /_8 open items are outside this window — see re-plan \(window 23 Oct 2026 → 22 Nov 2026\)_/);
});

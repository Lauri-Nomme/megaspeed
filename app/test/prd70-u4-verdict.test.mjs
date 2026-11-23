/* prd/70 U4 — the verdict card's next ranking (F-05) and holder row (F-06).
 *
 * F-05: the `Next:` footer printed only the reference pool while the rows were
 * ordered by hours owed now, so `verification 634 h` sat above
 * `refinement ≈2.3k h` under a caption claiming one ranking. The line now
 * prints both measures and the caption names the actual order.
 *
 * F-06: the holder row printed `· peers 0–4624 h` for the top *reviewer*, a
 * range whose upper bound was that person's own total (the store has no
 * sentinel). A peer is now `top reviewer …` with no self-referential range; the
 * range stays for a sentinel, where it means something.
 *
 * Run: node --test app/test/*.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  holderLine, holderSummary, nextConstraint, nextLine, nextMarkup, poolRows,
  verdictMarkdown,
} from '../verdict.js';

/** The QA's `1q` shape: one live tail, three historical pools. */
const QA_VERDICT = {
  ok: true,
  as_of: '2026-11-23T08:00:00Z',
  available: true,
  constraint: {
    station: 'review', label: 'Approval gate', wait_label: 'review',
    removable_hours: 1560, live_hours: 62, tail_hours: 1498,
    n_completed: 88, open_n: 5, p85_hours: 70, clock: 'story',
    items: [],
  },
  pools: [
    { station: 'review', label: 'Approval gate', removable_hours: 1560, live_hours: 62, tail_hours: 1498, n_completed: 88 },
    { station: 'verify', label: 'Verification', removable_hours: 634, live_hours: 8, tail_hours: 626, n_completed: 87 },
    { station: 'refine', label: 'Refinement', removable_hours: 2312, live_hours: 0, tail_hours: 2312, n_completed: 87 },
    { station: 'start', label: 'Starting work', removable_hours: 1817, live_hours: 0, tail_hours: 1817, n_completed: 87 },
    { station: 'plan', label: 'Sprint planning', removable_hours: 1490, live_hours: 0, tail_hours: 1490, n_completed: 87 },
  ],
  holders: { clock: 'pr-holder', people: [], peer_range: null, current: [], available: false, note: '' },
  ladder: [],
};

const PEER = { person: 'MaRa', role: 'reviewer', hours: 4623.7, all_phase_hours: 4623.7, items: [] };
const CAOS = { person: 'CaOs', role: 'reviewer', hours: 186.5, all_phase_hours: 495.8, items: [] };
const SENTINEL = { person: 'FrMo', role: 'sentinel', hours: 961.3, all_phase_hours: 1882.4, items: [] };

test('F-05: the next ranking is owed-now first, then the reference pool', () => {
  const order = nextConstraint(QA_VERDICT).map((row) => row.station);
  assert.deepEqual(order, ['verify', 'refine', 'start', 'plan']);
  // the order is what the caption claims: owed now desc, then removable desc
  const rows = nextConstraint(QA_VERDICT);
  for (let index = 1; index < rows.length; index += 1) {
    const above = rows[index - 1];
    const below = rows[index];
    assert.ok(Number(above.live_hours) >= Number(below.live_hours),
      `${above.station} owes at least as much as ${below.station}`);
    if (Number(above.live_hours) === Number(below.live_hours)) {
      assert.ok(Number(above.removable_hours) >= Number(below.removable_hours),
        `${above.station}'s pool is at least ${below.station}'s`);
    }
  }
});

test('F-05: the summary prints both measures, so 634 h never looks mis-sorted', () => {
  const line = nextLine(QA_VERDICT);
  assert.equal(line,
    'verification 8 h owed now (634 h removable) · ' +
    'refinement 0 h owed now (≈2.3k h removable) · ' +
    'starting work 0 h owed now (≈1.8k h removable)');
  // the claim is stated, not implied
  assert.match(line, /owed now/);
  assert.match(line, /removable/);
});

test('F-05: the expanded table shares the summary order and names the method', () => {
  const html = nextMarkup(QA_VERDICT);
  const order = [...html.matchAll(/data-next-station="([a-z]+)"/g)].map((match) => match[1]);
  assert.deepEqual(order, ['verify', 'refine', 'start', 'plan']);
  assert.match(html, /ranked by hours owed now first and\s+then by the reference removable pool/);
  assert.doesNotMatch(html, /ranked by the same clock and\s+method as the constraint/);
  // the markdown export quotes the same line (one source)
  assert.match(verdictMarkdown(QA_VERDICT), /_Next: verification 8 h owed now/);
});

test('F-06: a peer top holder is labelled, never compared with itself', () => {
  const verdict = {
    ...QA_VERDICT,
    holders: {
      clock: 'pr-holder', people: [PEER, CAOS], peer_range: [186.5, 4623.7],
      current: [], available: true, note: '',
    },
  };
  // the store has no sentinel: the range's upper bound IS the named person
  assert.equal(holderSummary(verdict.holders), 'top reviewer MaRa ≈4.6k h');
  assert.equal(holderLine(verdict), 'top reviewer MaRa ≈4.6k h');
  assert.doesNotMatch(holderSummary(verdict.holders), /peers/);
  assert.doesNotMatch(holderSummary(verdict.holders), /4624/);
  const holders = poolRows(verdict).find((row) => row.id === 'holders');
  assert.equal(holders.value, 'top reviewer MaRa ≈4.6k h');
  assert.equal(holders.clock, 'PR clock');
});

test('F-06: a sentinel keeps its peer range (there it is a real comparison)', () => {
  const holders = {
    clock: 'pr-holder', people: [SENTINEL, CAOS], peer_range: [186.5, 230.8],
    current: [], available: true, note: '',
  };
  assert.equal(holderSummary(holders), 'FrMo 961 h · peers 187 h–231 h');
  assert.equal(holderLine({ holders }), 'FrMo 961 h · peers 187 h–231 h');
});

test('F-06: a window with no observed review hours says so', () => {
  const holders = {
    clock: 'pr-holder',
    people: [{ ...PEER, hours: 0 }, { ...CAOS, hours: 0 }],
    peer_range: null, current: [], available: false, note: '',
  };
  assert.equal(holderSummary(holders), 'no review hours in this window');
  assert.equal(holderLine({ holders }), '');
});

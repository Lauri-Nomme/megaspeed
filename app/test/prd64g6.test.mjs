/* prd/64 G6 — ticket detail honesty (F-14, F-15) and polish (F-21, F-22).
 *
 * The ticket page must carry the findings the summary views link to:
 * a PR opened after the ticket closed (F-14), a PR every reviewer approved and
 * that closed unmerged (F-15), human boundary-chip labels (F-21), and one clock
 * for the worklog delay + its bucket (F-22).
 *
 * Run: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  attributeFacts, boundaryChips, eventsList, prFindingText, prTable,
  ticketMarkup, ticketStats,
} from '../ticket.js';

function pr(overrides = {}) {
  return {
    object_id: 'PR:DEL/delivery-api#66', number: 66, repo: 'DEL/delivery-api',
    state: 'open', censored: true, opened: '2026-11-13T11:42:57Z',
    closed: null, cycle_hours: 19.28, review_rounds: 0,
    push_classes: {}, intervals: [], approved_unmerged: null,
    opened_after_close: false, opened_after_close_hours: 0,
    ...overrides,
  };
}

function segments(overrides = {}) {
  return {
    rows: Array.from({ length: 10 }, (_, index) => ({
      key: `s${index}`, label: `segment ${index}`, available: index < 7,
      hours: index < 7 ? 8 : null, share: index < 7 ? 1 / 7 : null,
      efficiency: index < 7 ? 0.2 : null, missing: ['merged'],
      reason: 'no merged PR names this key',
    })),
    measured: 7,
    total_hours: 56,
    cycle_hours: 260,
    largest: 'created_ready',
    largest_label: 'created → ready',
    largest_share: 0.298,
    boundaries: { done: { ts: null, available: false, reason: 'still open' } },
    explained_share: 0.216,
    ...overrides,
  };
}

function openStoryPayload(overrides = {}) {
  return {
    ok: true, key: 'DEL-161', available: true, kind: 'story',
    hours_mode: 'business', snapshot: '2026-11-18T00:00:00Z',
    story: {
      key: 'DEL-161', points: 2, state: 'open', censored: true,
      created: '2026-10-02T10:00:00Z', closed: null, cycle_hours: 260,
    },
    segments: segments(),
    prs: [pr({
      number: 34, state: 'declined', censored: false,
      opened: '2026-10-09T11:03:00Z', closed: '2026-11-16T00:03:20Z',
      cycle_hours: 202.95,
    })],
    pr_count: 1,
    open_prs: [],
    change: {},
    sprints: { memberships: [] },
    worklog: {}, worklogs: [], events: [], event_count: 0,
    answer: '78 % of the cycle is unmeasured.',
    answer_kind: 'partial',
    quirk: { count: 0, clamped: [], order: [] },
    yield: null,
    ...overrides,
  };
}

test('F-15: an open story headlines its age, the measured sum, and "of measured"', () => {
  const stats = ticketStats(openStoryPayload());
  assert.match(stats, /<b>32 d 4 h<\/b> age \(open\)/);
  assert.match(stats, /<b>7 d 0 h<\/b> measured across <b>7<\/b>\/10 segments/);
  // the largest share is priced against the 7 d measured sum, not the 32 d age
  assert.match(stats, /most in <b>created → ready<\/b> \(30% of measured\)/);
  assert.doesNotMatch(stats, /32 d 4 h<\/b> cycle/);
});

test('F-15: a completed, fully measured story keeps the plain cycle + share', () => {
  const rows = segments().rows.map((row) => ({
    ...row, available: true, hours: 8, share: 0.1,
  }));
  const stats = ticketStats(openStoryPayload({
    key: 'DEL-183',
    story: { ...openStoryPayload().story, state: 'done', censored: false },
    segments: segments({
      rows, measured: 10, total_hours: 80, cycle_hours: 80,
      largest_share: 0.1,
      boundaries: { done: { ts: '2026-11-04T15:46:27Z', available: true } },
    }),
  }));
  assert.match(stats, /<b>10 d 0 h<\/b> cycle/);
  assert.match(stats, /<b>10<\/b>\/10 segments/);
  assert.match(stats, /\(10%\)/);
  assert.doesNotMatch(stats, /of measured/);
});

test('F-14: the #66 row states the PR opened after the ticket closed', () => {
  const html = prTable({
    key: 'DEL-183', available: true, pr_count: 2,
    prs: [
      pr({ number: 65, state: 'merged', censored: false, object_id: 'PR:x#65' }),
      pr({
        opened_after_close: true, opened_after_close_hours: 52,
        object_id: 'PR:x#66',
      }),
    ],
  });
  assert.match(html, /data-pr-after-close="true"/);
  assert.match(html, /opened 6 d 4 h after the ticket closed — the work cannot have been part of the delivered story/);
  // the PR opened before the close carries no such sentence
  const before = html.slice(0, html.indexOf('data-pr-after-close'));
  assert.doesNotMatch(before, /after the ticket closed/);
  assert.doesNotMatch(prFindingText(pr()).join(' '), /after the ticket closed/);
});

test('F-15: the #34 row names the approvers, the bot closer and the hours lost', () => {
  const html = prTable({
    key: 'DEL-161', available: true, pr_count: 2,
    prs: [
      pr({
        number: 34, state: 'declined', censored: false, object_id: 'PR:x#34',
        approved_unmerged: {
          approved_by: ['DaKo', 'FrMo'], closed_by: 'BiBi', bot_closed: true,
          hours: 187, approved_at: '2026-10-13T10:12:10Z',
          closed_at: '2026-11-16T00:03:20Z',
        },
      }),
      pr({ number: 35, state: 'merged', censored: false, object_id: 'PR:x#35' }),
    ],
  });
  assert.match(html, /data-pr-approved-unmerged="true"/);
  assert.match(html, /approved by DaKo \+ FrMo, closed unmerged by the bot BiBi — 23 d 3 h lost/);
  assert.doesNotMatch(prFindingText(pr({ number: 35, state: 'merged' })).join(' '), /approved by/);
});

test('F-15: an unnamed closer is stated without inventing a bot', () => {
  const text = prFindingText(pr({
    number: 35,
    approved_unmerged: { approved_by: ['DaKo', 'FrMo'], closed_by: null, bot_closed: false, hours: 100 },
  }));
  assert.deepEqual(text, [
    'approved by DaKo + FrMo, closed unmerged after approval — 12 d 4 h lost',
  ]);
  assert.doesNotMatch(text.join(' '), /bot/);
});

test('F-14/F-15: the ticket narrative renders the same sentences', () => {
  const html = ticketMarkup(openStoryPayload({
    answer: 'PR #34 was approved by DaKo + FrMo and closed unmerged by the bot BiBi — 23 d 3 h lost from the final approval.',
  }));
  assert.match(html, /id="ticket-answer"[^>]*>PR #34 was approved by DaKo \+ FrMo and closed unmerged by the bot BiBi — 23 d 3 h lost from the final approval\./);
});

test('F-21: boundary chips and their titles carry human labels, not snake_case', () => {
  const html = boundaryChips({
    segments: {
      boundaries: {
        pr_open: { ts: '2026-10-02T09:00:00Z', available: true },
        reviewer_added: { ts: '2026-10-02T10:00:00Z', available: true },
        in_verification: { ts: null, available: false, reason: 'no status change to in verification' },
        done: {
          ts: '2026-10-03T10:00:00Z', available: true,
          order_quirk: { after: 'merged', after_ts: '2026-10-03T09:00:00Z', reason: 'x' },
        },
      },
      boundary_order: ['pr_open', 'reviewer_added', 'in_verification', 'done'],
    },
  });
  // the DOM carries the human label; CSS text-transform uppercases it (prd/10)
  assert.match(html, />pr open</);
  assert.match(html, />reviewer added</);
  assert.match(html, />in verification</);
  assert.doesNotMatch(html, />pr_open</);
  assert.doesNotMatch(html, />in_verification</);
  // every tooltip is human too (`the pr open boundary`, not `the pr_open boundary`)
  const titles = [...html.matchAll(/title="([^"]*)"/g)].map((match) => match[1]);
  assert.ok(titles.length >= 3);
  for (const title of titles) assert.doesNotMatch(title, /_/, title);
  assert.ok(titles.includes('the pr open boundary'));
  // the raw key stays in the data attribute for selectors
  assert.match(html, /data-boundary="pr_open"/);
});

test('F-22: the logged-late duration and its bucket use the calendar clock', () => {
  const facts = attributeFacts({
    log_delay_seconds: 76.25 * 3600, log_delay_bucket: 'within_week',
  });
  const byKey = Object.fromEntries(facts.map((fact) => [fact.key, fact.text]));
  assert.equal(byKey.log_delay_seconds, '3 calendar days 4 h');
  assert.equal(byKey.log_delay_bucket, 'within week');
  // the old 8-business-hour clock printed "9 d 4 h" for the same 76 h, which
  // read as longer than the one-week bucket
  assert.notEqual(byKey.log_delay_seconds, '9 d 4 h');
  const html = eventsList({
    events: [{
      ts: '2026-10-12T15:45:00Z', activity: 'worklogged', actor: 'CaOs',
      attributes: { log_delay_seconds: 76.25 * 3600, log_delay_bucket: 'within_week' },
    }],
  });
  assert.match(html, /logged late by<\/span>\s*<span class="v mono">3 calendar days 4 h<\/span>/);
  assert.match(html, /log delay<\/span>\s*<span class="v mono">within week<\/span>/);
});

test('F-22: a delay past one calendar week is labelled in calendar days too', () => {
  const facts = attributeFacts({
    log_delay_seconds: 9 * 86_400, log_delay_bucket: 'late',
  });
  const byKey = Object.fromEntries(facts.map((fact) => [fact.key, fact.text]));
  assert.equal(byKey.log_delay_seconds, '9 calendar days');
  assert.equal(byKey.log_delay_bucket, 'late');
  // 9 calendar days is > 7 calendar days, so "late" is consistent
  assert.ok(9 * 86_400 > 7 * 86_400);
});

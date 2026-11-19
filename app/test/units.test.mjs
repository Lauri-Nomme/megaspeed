/* prd/35: one time unit, one clock — at the view boundary.
 *
 * The formatters are pinned in `format.test.mjs`; this file pins what the
 * views do with them:
 *   - the delivery list renders a business-hour wait as `Nd Nh` (d = 8 h);
 *   - the PR drawer renders the same kind of span as *calendar days* and says
 *     its timestamps are UTC;
 *   - the ticket page states the clock once (UTC timestamps, business hours on
 *     the team calendar).
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { DAY, HOUR, fmtCalendarDuration, fmtHours } from '../format.js';
import { deliveryTable, detailMarkup } from '../tables.js';
import { clockNote, ticketMarkup } from '../ticket.js';

function row(overrides = {}) {
  return {
    object_id: 'PR:DEL/web#52', repo_short: 'web', number: 52,
    tickets: ['DEL-197'], ticket: 'DEL-197', author: 'AlNg', state: 'open',
    ball: 'review', ball_label: 'review', phase: 'review',
    waiting_since: '2026-11-13T12:07:00Z', waiting_hours: 168,
    age_hours: 400, blocked: false, dependents: 0, impact_hours: 0,
    ...overrides,
  };
}

test('the delivery list renders business hours with d = 8', () => {
  const html = deliveryTable({ rows: [row()] }, 'all');
  assert.match(html, /21 d 0 h/);          // 168 business hours
  assert.doesNotMatch(html, /7 d 0 h/);    // the old 24 h divisor
});

test('the PR drawer calls a wall-clock span "calendar days", never d', () => {
  const span = 90 * DAY + 9 * HOUR;
  const markup = detailMarkup({
    pr: {
      ticket: 'DEL-197', number: 52, repo: 'DEL/web', actor: 'AlNg',
      state: 'open', closed: null, epic: '', commits: [],
    },
    facts: [['cycled', `${fmtCalendarDuration(span)} (calendar time)`]],
    clockNote: 'timestamps are UTC · this drawer’s spans are elapsed calendar time; ' +
      'the delivery list and ticket page price waits in business hours on the team calendar',
  });
  assert.match(markup, /90 calendar days 9 h/);
  assert.doesNotMatch(markup, /90 d/);
  assert.match(markup, /timestamps are UTC/);
  // the same 90-day span as *business* time is 5 working days a week, and the
  // formatter would say "65 d …" only if it were (wrongly) handed wall clock
  assert.match(fmtHours(5 * 8), /^5 d 0 h$/);
});

test('the ticket page states the clock where the numbers are', () => {
  const business = clockNote({
    hours_mode: 'business',
    assumption: 'business hours (Mon–Fri 09:00–17:00 Europe/Tallinn; holidays and vacations excluded)',
  });
  assert.match(business, /timestamps are UTC/);
  assert.match(business, /business hours on the team calendar/);
  assert.match(business, /Europe\/Tallinn/);
  assert.match(business, /a 0 h span can be an evening/);
  const wall = clockNote({ hours_mode: 'wall-clock' });
  assert.match(wall, /timestamps are UTC/);
  assert.match(wall, /wall-clock hours/);
  // the note is actually on the page, once, under the ticket head
  const html = ticketMarkup({
    key: 'DEL-1',
    story: { state: 'done', points: 3, assignee: 'AaAa',
      created: '2026-01-05T09:00:00Z', closed: null },
    segments: { rows: [], boundaries: { done: { ts: null } }, heuristic: '' },
    hours_mode: 'business',
    assumption: 'business hours (Mon–Fri 09:00–17:00 Europe/Tallinn)',
    prs: [], open_prs: [], pr_count: 0,
    sprints: { memberships: [] }, worklogs: [], worklog: {}, events: [],
    event_count: 0, answer: '', answer_kind: 'measured',
    snapshot: '2026-03-01T00:00:00Z',
  });
  assert.match(html, /id="ticket-clock"/);
  assert.match(html, /timestamps are UTC/);
});

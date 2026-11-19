/* One unit, one clock (prd/35; concepts.md §1).
 *
 * The contract this file pins:
 *   - `d` = 8 business hours = one working day, everywhere a duration is
 *     rendered (168 business hours is `21 d 0 h`, not `7 d`);
 *   - an elapsed wall-clock span is "calendar days", never `d`;
 *   - the two formatters never produce the same letter for different clocks.
 *
 * Run with: node --test app/test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  BUSINESS_DAY, DAY, HOUR, fmtAge, fmtBucket, fmtCalendarDuration, fmtCalendarHours,
  fmtDuration, fmtHours,
} from '../format.js';

test('a day is 8 business hours: 168 bh is 21 d 0 h', () => {
  assert.equal(BUSINESS_DAY, 8 * HOUR);
  assert.equal(fmtDuration(168 * HOUR), '21 d 0 h');
  assert.equal(fmtHours(168), '21 d 0 h');
  // the old divisor rendered 168 h as "7 d" — that is the bug prd/35 fixes
  assert.notEqual(fmtHours(168), '7 d 0 h');
  assert.equal(fmtDuration(40 * HOUR), '5 d 0 h');      // one working week
  assert.equal(fmtDuration(8 * HOUR), '1 d 0 h');
  assert.equal(fmtDuration(72 * HOUR), '9 d 0 h');      // the 72 h target
  assert.equal(fmtDuration(9 * HOUR), '1 d 1 h');
  // under one working day it stays hours, never a fractional day
  assert.equal(fmtDuration(7 * HOUR + 30 * 60_000), '7 h 30 min');
});

test('a wall-clock span is "calendar days", never d', () => {
  assert.equal(fmtCalendarDuration(90 * DAY), '90 calendar days');
  assert.equal(fmtCalendarDuration(3 * DAY + 12 * HOUR), '3 calendar days 12 h');
  assert.equal(fmtCalendarDuration(DAY), '1 calendar day');
  assert.equal(fmtCalendarDuration(5 * HOUR), '5 h');
  // the letter `d` is reserved for business days; a calendar span never uses it
  for (const ms of [DAY, 7 * DAY, 90 * DAY, 400 * DAY]) {
    assert.doesNotMatch(fmtCalendarDuration(ms), /\b\d+ d\b/);
    assert.match(fmtCalendarDuration(ms), /calendar day/);
  }
  // the same 7-day span reads as 5 working days of business time, not "7 d"
  assert.equal(fmtCalendarDuration(7 * DAY), '7 calendar days');
  assert.equal(fmtHours(5 * 8), '5 d 0 h');
  assert.equal(fmtCalendarHours(7 * 24), '7 calendar days');
});

test('freshness and bucket widths also say "calendar"', () => {
  assert.equal(fmtAge(3 * DAY, 4 * DAY), '1 calendar day ago');
  assert.equal(fmtAge(2 * DAY, 5 * DAY), '3 calendar days ago');
  assert.equal(fmtBucket(DAY), '1 calendar day');
  assert.equal(fmtBucket(2 * DAY), '2 calendar days');
  assert.equal(fmtBucket(6 * HOUR), '6 h');
});

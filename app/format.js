/* Shared formatting for every megaspeed view.
 *
 * Units are spaced with a slash ("246 /h") and numbers are tabular everywhere
 * (prd/10 §3.4). Durations are business hours when the tenant has a calendar
 * (prd/19) and wall-clock otherwise; every view labels which mode answered.
 *
 * concepts.md §1 (prd/35): **one unit, one clock**. A `d` is 8 *business*
 * hours — one working day — never 24 h and never a calendar day. An elapsed
 * wall-clock span is a different quantity and is rendered by
 * `fmtCalendarDuration` with the word "calendar", so the two can never share
 * the letter `d`.
 */

export const MINUTE = 60_000;
export const HOUR = 60 * MINUTE;
/** Wall-clock day: date math only (`fmtDay`, windows), never a duration unit. */
export const DAY = 24 * HOUR;
/** concepts.md §1: one working day = 8 business hours. */
export const BUSINESS_HOURS_PER_DAY = 8;
export const BUSINESS_DAY = BUSINESS_HOURS_PER_DAY * HOUR;

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export const pad = (n) => String(n).padStart(2, '0');

export function fmtDay(ms) {
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

export function fmtDayYear(ms) {
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

export function fmtClock(ms) {
  const d = new Date(ms);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

/** ISO-ish stamp: "2026-11-15 16:05 UTC" (mono, comparable). */
export function fmtStamp(ms) {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

/** Compact absolute date for tables: "2026-09-15 09:34". */
export function fmtAbsolute(ms) {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

/** A timeframe in the top bar: day-granular only when the window is wide. */
export function fmtRange(start, end) {
  const span = end - start;
  if (span <= 2 * DAY) return `${fmtDay(start)} ${fmtClock(start)} → ${fmtDay(end)} ${fmtClock(end)}`;
  if (span <= 45 * DAY) return `${fmtDay(start)} ${fmtClock(start)} → ${fmtDay(end)} ${fmtClock(end)}`;
  return `${fmtDay(start)} → ${fmtDay(end)}`;
}

export function fmtTick(ms, step) {
  const d = new Date(ms);
  if (step >= DAY) return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

/** Duration from milliseconds of *business* time: "18 min", "4 h 20 min", "21 d 0 h".
 *
 * concepts.md §1: `d = h / 8`, so 168 business hours render as `21 d 0 h`
 * (four working weeks), not `7 d`. Never pass a wall-clock span here — use
 * `fmtCalendarDuration` so the label says "calendar".
 */
export function fmtDuration(ms) {
  if (!isFinite(ms) || ms < MINUTE) return '0 min';
  const mins = Math.round(ms / MINUTE);
  if (mins < 60) return `${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < BUSINESS_HOURS_PER_DAY) {
    return `${hours} h${mins % 60 ? ` ${mins % 60} min` : ''}`;
  }
  const days = Math.floor(hours / BUSINESS_HOURS_PER_DAY);
  const rest = hours % BUSINESS_HOURS_PER_DAY;
  return `${days} d ${rest} h`;
}

/** Duration from hours (the analysis API's unit, already business hours). */
export function fmtHours(hours) {
  return fmtDuration((Number(hours) || 0) * HOUR);
}

/**
 * An elapsed **wall-clock** span: "3 h", "5 calendar days 4 h".
 *
 * concepts.md §1 / prd/35: a calendar day is not a `d`, so this formatter
 * spells the word out. The PR drawer and the timeline tooltips use it for the
 * time between two timestamps; business-hour durations use `fmtDuration`.
 */
export function fmtCalendarDuration(ms) {
  if (!isFinite(ms) || ms < MINUTE) return '0 min';
  const mins = Math.round(ms / MINUTE);
  if (mins < 60) return `${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} h${mins % 60 ? ` ${mins % 60} min` : ''}`;
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  // prd/63 V-B: a long wall-clock span is stated in whole calendar days. The
  // hour remainder on a 802-day lifecycle ("802 calendar days 1 h") mixed two
  // units in one sentence and added nothing; under a week the remainder stays.
  if (days >= 7) return `${days} calendar day${days === 1 ? '' : 's'}`;
  return `${days} calendar day${days === 1 ? '' : 's'}${rest ? ` ${rest} h` : ''}`;
}

/**
 * A whole number of elapsed **calendar** days ("92 calendar days").
 *
 * The one way a view renders a day count it holds in days (the aging row's
 * `calendar_days`): the letter `d` is a business day (concepts.md §1), and a
 * duration is never divided by 24 outside this file (prd/45 conformance).
 */
export function fmtCalendarDays(days) {
  const value = Math.max(0, Math.round(Number(days) || 0));
  return `${value} calendar day${value === 1 ? '' : 's'}`;
}

/** Elapsed wall-clock hours (the timeline's unit) as a calendar duration. */
export function fmtCalendarHours(hours) {
  return fmtCalendarDuration((Number(hours) || 0) * HOUR);
}

export function fmtAge(ms, now = Date.now()) {
  const delta = Math.max(0, now - ms);
  if (delta < MINUTE) return 'just now';
  if (delta < HOUR) return `${Math.round(delta / MINUTE)} min ago`;
  if (delta < DAY) return `${Math.round(delta / HOUR)} h ago`;
  // wall clock: say "calendar", never the business-hour `d` (prd/35).
  // prd/63 F22: "3 calendar days ago" counts whole calendar dates, so 3 d 15 h
  // must not round up to 4. Under a week the remainder is stated too; past
  // that, whole calendar days are the readable fact.
  const days = Math.floor(delta / DAY);
  const rest = Math.round((delta - days * DAY) / HOUR);
  if (days < 7) {
    return `${days} calendar day${days === 1 ? '' : 's'}${rest ? ` ${rest} h` : ''} ago`;
  }
  return `${days} calendar days ago`;
}

export function fmtBucket(ms) {
  if (ms < HOUR) return `${Math.max(1, Math.round(ms / MINUTE))} min`;
  if (ms < DAY) return `${Math.round(ms / HOUR)} h`;
  const days = Math.round(ms / DAY);
  return `${days} calendar day${days === 1 ? '' : 's'}`;
}

/** Honest compact notation: 20.4k, ≈5.9M. */
export function fmtCompact(n) {
  const value = Number(n) || 0;
  if (Math.abs(value) >= 1e6) return `${(value / 1e6).toFixed(1).replace(/\.0$/, '')}M`;
  if (Math.abs(value) >= 1e4) return `${(value / 1e3).toFixed(1).replace(/\.0$/, '')}k`;
  return String(Math.round(value));
}

export const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export function rgba(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/** Human label for a handover endpoint id. */
export function whoLabel(id) {
  if (id === 'review') return 'review queue';
  if (id === 'author') return 'author';
  if (id === 'CI') return 'CI';
  if (id === 'ready') return 'ready to merge';
  if (id === 'calendar') return 'calendar';
  return id;
}

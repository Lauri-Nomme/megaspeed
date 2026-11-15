/* Shared formatting for every megaspeed view.
 *
 * Units are spaced with a slash ("246 /h") and numbers are tabular everywhere
 * (prd/10 §3.4). Durations are business hours when the tenant has a calendar
 * (prd/19) and wall-clock otherwise; every view labels which mode answered.
 */

export const MINUTE = 60_000;
export const HOUR = 60 * MINUTE;
export const DAY = 24 * HOUR;

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

/** Duration from milliseconds: "18 min", "4 h 20 min", "61 d 7 h". */
export function fmtDuration(ms) {
  if (!isFinite(ms) || ms < MINUTE) return '0 min';
  const mins = Math.round(ms / MINUTE);
  if (mins < 60) return `${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} h${mins % 60 ? ` ${mins % 60} min` : ''}`;
  const days = Math.floor(hours / 24);
  return `${days} d ${hours % 24} h`;
}

/** Duration from hours (the analysis API's unit). */
export function fmtHours(hours) {
  return fmtDuration((Number(hours) || 0) * HOUR);
}

export function fmtAge(ms, now = Date.now()) {
  const delta = Math.max(0, now - ms);
  if (delta < MINUTE) return 'just now';
  if (delta < HOUR) return `${Math.round(delta / MINUTE)} min ago`;
  if (delta < DAY) return `${Math.round(delta / HOUR)} h ago`;
  return `${Math.round(delta / DAY)} d ago`;
}

export function fmtBucket(ms) {
  if (ms < HOUR) return `${Math.max(1, Math.round(ms / MINUTE))} min`;
  if (ms < DAY) return `${Math.round(ms / HOUR)} h`;
  return `${Math.round(ms / DAY)} d`;
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
  if (id === 'calendar') return 'calendar';
  return id;
}

/* Benchmark contrast (prd/57): one duration next to its cohort median.
 *
 * The server attaches a `benchmark` block to every per-item duration —
 * `{available, median, multiple, n, basis, note}` — computed by one helper
 * (`analysis/benchmark.py`). This module renders that block in the one form the
 * concept specifies:
 *
 *     123 h (median 30 h · 4.1×)
 *
 * The multiple is always suppressed when the server says the cohort is too
 * small (N < 10) or no measured value exists, so a view can call this
 * unconditionally and never draw a conclusion from a handful of items. The
 * toggle is the caller's: `enabled: false` returns '' without changing the
 * payload.
 */

import { esc, fmtHours } from './format.js';

/** The server's threshold, mirrored so the client can fail closed. */
export const BENCHMARK_MIN_N = 10;

/** Is there a real median + multiple to show? */
export function benchmarkReady(entry) {
  if (!entry || !entry.available) return false;
  if (entry.median == null || entry.multiple == null) return false;
  const min = Number(entry.min_n || BENCHMARK_MIN_N);
  return Number(entry.n || 0) >= min;
}

/** "4.1×" — one decimal, the concept's form. */
export function multipleLabel(multiple) {
  const value = Number(multiple);
  if (!isFinite(value)) return '';
  return `${(Math.round(value * 10) / 10).toFixed(1)}×`;
}

/** "median 30 h · 4.1×" (no parentheses; callers add them where needed). */
export function benchmarkText(entry, { format = fmtHours } = {}) {
  if (!benchmarkReady(entry)) return '';
  return `median ${format(entry.median)} · ${multipleLabel(entry.multiple)}`;
}

/** The tooltip: always names the cohort and its size (prd/57 §3). */
export function benchmarkTitle(entry) {
  if (!entry) return '';
  if (!benchmarkReady(entry)) return entry.note || 'no cohort benchmark available';
  return `vs ${entry.basis || 'the cohort median'} (N=${entry.n})`;
}

/**
 * The inline contrast after a value: ` (median 30 h · 4.1×)`.
 *
 * `format` lets a surface keep its own unit renderer (the ticket segments show
 * hours, so they pass `segmentHours`); both render on the shared business-hour
 * clock, so the median and the value never disagree about what an hour is.
 */
export function benchmarkInline(entry, { enabled = true, format = fmtHours } = {}) {
  if (!enabled) return '';
  const text = benchmarkText(entry, { format });
  if (!text) return '';
  return ` <span class="benchmark-contrast" data-benchmark="on" title="${esc(benchmarkTitle(entry))}">(${esc(text)})</span>`;
}

/** A suppressed contrast, for a title/tooltip that must say why (never drawn). */
export function benchmarkNote(entry) {
  if (!entry || benchmarkReady(entry)) return '';
  return entry.note || '';
}

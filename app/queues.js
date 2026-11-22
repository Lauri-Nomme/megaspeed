/* Queue view — per-person (or per-queue) WIP over time (prd/09 View 3).
 *
 * The point is the diagnosis, not the curve: a server with a long wait and a
 * high queue is *overloaded*; the same wait with a short queue is *slow*. These
 * need different fixes (capacity vs tooling), so the diagnosis is stated in
 * words next to the numbers, with `≈` where a wall-clock reading would bias it
 * (waits are business hours when the tenant has a calendar, prd/19).
 *
 * Small multiples share one x scale, so rows are comparable at a glance.
 */

import { HOUR, fmtStamp, fmtCalendarDuration, fmtHours, esc, rgba } from './format.js';
import { benchmarkInline } from './benchmark.js';
import { streamRoleLabel } from './scope.js';
import { color, onPalette } from './theme.js';

const ROW_H = 62;
const GUTTER = 232;
const AXIS_H = 26;
const BOTTOM = 22;
// B15: a small right-hand y scale, outside the plotted area so labels never
// collide with the data or the gutter's name/diagnosis text.
const Y_AXIS_W = 34;

/* Every hue is a theme token (app/theme.js): the WIP fill is the info accent,
 * completions are neutral — green is reserved for the 'healthy' state. */
const C = {
  wip: '', done: '', strong: '', label: '', muted: '', faint: '',
  hairline: '', rule: '', grid: '', mean: '', rowSelected: '', rowHover: '',
};

export const DIAGNOSIS = {
  overloaded: { token: '--critical', tintToken: '--critical-veil', label: 'overloaded', color: '', tint: '' },
  slow: { token: '--warning', tintToken: '--warning-veil', label: 'slow', color: '', tint: '' },
  healthy: { token: '--healthy', tintToken: '--healthy-veil', label: 'healthy', color: '', tint: '' },
};

/** Resolve the chart's colours from the theme tokens (dark fallbacks first). */
export function applyPalette() {
  C.wip = color('--info-accent');
  C.done = color('--canvas-label');
  C.strong = color('--canvas-strong');
  C.label = color('--canvas-label');
  C.muted = color('--canvas-muted');
  C.faint = color('--canvas-faint');
  C.hairline = color('--canvas-hairline');
  C.rule = color('--canvas-row-rule');
  C.grid = color('--canvas-grid-faint');
  C.mean = color('--canvas-mean');
  C.rowSelected = color('--canvas-row-selected');
  C.rowHover = color('--canvas-row-hover-soft');
  for (const diagnosis of Object.values(DIAGNOSIS)) {
    diagnosis.color = color(diagnosis.token);
    diagnosis.tint = color(diagnosis.tintToken);
  }
}
onPalette(applyPalette);

export class QueueChart {
  constructor({ canvas, tip, onSelect }) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.tip = tip;
    this.onSelect = onSelect;
    this.data = null;
    this.streams = [];
    this.hover = null;
    this.selected = null;
    this.renderQueued = false;
    // prd/28: a phone has no hover; tap-and-hold shows the same tooltip.
    this._touchAt = 0;
    this._holdTimer = 0;
    this._bind();
  }

  setData(data) {
    this.data = data;
    this.streams = (data && data.streams) || [];
    if (this.selected && !this.streams.some((s) => s.id === this.selected)) this.selected = null;
    // B13: a tooltip describes the data that produced it. Level/tenant/page
    // changes replace that data, so the old tooltip must not linger.
    this.hover = null;
    if (this.tip) this.tip.hidden = true;
    this.schedule();
  }

  select(id) {
    this.selected = this.selected === id ? null : id;
    this.schedule();
  }

  schedule() {
    if (this.renderQueued) return;
    this.renderQueued = true;
    requestAnimationFrame(() => {
      this.renderQueued = false;
      this.render();
    });
  }

  geometry() {
    const width = this.canvas.parentElement.clientWidth || 900;
    const rows = Math.max(1, this.streams.length);
    const height = AXIS_H + rows * ROW_H + BOTTOM;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { width, height, plotW: Math.max(120, width - GUTTER - Y_AXIS_W - 10), plotH: rows * ROW_H };
  }

  render() {
    const ctx = this.ctx;
    const g = this.geometry();
    ctx.clearRect(0, 0, g.width, g.height);
    ctx.fillStyle = color('--canvas-bg');
    ctx.fillRect(0, 0, g.width, g.height);
    this.hits = [];

    if (!this.streams.length) {
      ctx.fillStyle = C.muted;
      ctx.font = '500 12px Inter, system-ui, sans-serif';
      const message = (this.data && this.data.emptyText) || 'No measured waits in this window.';
      ctx.fillText(message, GUTTER, AXIS_H + 24);
      return;
    }

    const buckets = this.data.buckets;
    const start = Date.parse(this.data.window.start);
    const end = Date.parse(this.data.window.end);
    const bucketMs = (end - start) / Math.max(1, buckets);
    const maxWip = Math.max(1, ...this.streams.flatMap((s) => s.wip));
    const xAt = (i) => GUTTER + (i / Math.max(1, buckets - 1)) * g.plotW;

    // shared time axis
    ctx.font = '500 10px "JetBrains Mono", ui-monospace, monospace';
    ctx.fillStyle = C.muted;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    // prd/28: a phone's plot is ~136px wide; five 16-character stamps would
    // overlap into an unreadable smear, so the narrow axis says date only and
    // keeps two or three ticks.
    const phone = g.width <= 640;
    const ticks = phone ? (g.plotW >= 300 ? 3 : 2) : 5;
    for (let i = 0; i < ticks; i += 1) {
      const t = start + ((end - start) * i) / (ticks - 1);
      const x = GUTTER + (i / (ticks - 1)) * g.plotW;
      ctx.textAlign = i === 0 ? 'left' : i === ticks - 1 ? 'right' : 'center';
      ctx.fillText(phone ? fmtStamp(t).slice(0, 10) : fmtStamp(t).slice(0, 16), x, 12);
    }
    ctx.strokeStyle = C.hairline;
    ctx.beginPath();
    ctx.moveTo(GUTTER, AXIS_H - 2.5);
    ctx.lineTo(GUTTER + g.plotW, AXIS_H - 2.5);
    ctx.stroke();

    this.streams.forEach((stream, row) => {
      const top = AXIS_H + row * ROW_H;
      const chartH = ROW_H - 16;
      const base = top + chartH;
      const isHover = this.hover && this.hover.stream === stream.id;
      const isSelected = this.selected === stream.id;
      if (isHover || isSelected) {
        ctx.fillStyle = isSelected ? C.rowSelected : C.rowHover;
        ctx.fillRect(0, top, g.width, ROW_H);
      }
      ctx.strokeStyle = C.rule;
      ctx.beginPath();
      ctx.moveTo(0, Math.round(top + ROW_H) - 0.5);
      ctx.lineTo(g.width, Math.round(top + ROW_H) - 0.5);
      ctx.stroke();

      const summary = stream.summary;
      const diagnosis = DIAGNOSIS[summary.diagnosis] || DIAGNOSIS.healthy;

      // gutter: name, role, diagnosis, numbers
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      ctx.font = '600 12px Inter, system-ui, sans-serif';
      ctx.fillStyle = C.strong;
      ctx.fillText(stream.id, 14, top + 16);
      const nameW = ctx.measureText(stream.id).width;
      ctx.font = '500 10px "JetBrains Mono", ui-monospace, monospace';
      ctx.fillStyle = C.muted;
      const role = stream.people && stream.people.length
        ? `${streamRoleLabel(stream)} · ${stream.people.length} ppl`
        : streamRoleLabel(stream);
      // prd/28: on a phone the diagnosis pill moves to the gutter's right edge
      // so it no longer paints over the first buckets of the sparkline.
      const pillText = `≈ ${diagnosis.label}`;
      const pillW = phone ? Math.max(76, Math.ceil(ctx.measureText(pillText).width) + 14) : 76;
      const pillX = phone ? GUTTER - pillW - 8 : 194;
      // the role sits right after the name (not at a fixed x) so the pill's
      // width never truncates it to `senti…`
      const roleX = phone ? Math.round(14 + nameW + 8) : 92;
      ctx.fillText(phone ? fitText(ctx, role, pillX - 6 - roleX) : role, roleX, top + 16);
      // diagnosis pill
      ctx.fillStyle = diagnosis.tint;
      roundRect(ctx, pillX, top + 9, pillW, 15, 7);
      ctx.fill();
      ctx.fillStyle = diagnosis.color;
      ctx.fillText(pillText, pillX + 7, top + 17);

      ctx.font = '500 10.5px "JetBrains Mono", ui-monospace, monospace';
      ctx.fillStyle = C.label;
      ctx.fillText(
        `${summary.mean_wip.toFixed(1)} wip · ≈${summary.mean_wait_hours.toFixed(1)} h mean · ${summary.throughput_per_day.toFixed(2)}/d`,
        14, top + 34,
      );
      ctx.fillStyle = C.faint;
      ctx.fillText(`${summary.items} PRs · peak ${summary.peak_wip}`, 14, top + 47);

      // B15: the y scale. Shared across rows (maxWip), labelled at 0, half and
      // the top, with a faint gridline at each step so WIP can be read without
      // hovering.
      const yAxisX = GUTTER + g.plotW + 6;
      for (const tick of wipTicks(maxWip)) {
        const y = base - (tick / maxWip) * chartH;
        if (tick > 0) {
          ctx.strokeStyle = C.grid;
          ctx.beginPath();
          ctx.moveTo(GUTTER, Math.round(y) + 0.5);
          ctx.lineTo(GUTTER + g.plotW, Math.round(y) + 0.5);
          ctx.stroke();
        }
        ctx.font = '500 9px "JetBrains Mono", ui-monospace, monospace';
        ctx.fillStyle = C.faint;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.fillText(fmtWipTick(tick), yAxisX, y);
      }

      // WIP area
      const stepX = g.plotW / Math.max(1, buckets - 1);
      ctx.beginPath();
      ctx.moveTo(xAt(0), base);
      stream.wip.forEach((value, i) => {
        const y = base - (value / maxWip) * chartH;
        ctx.lineTo(xAt(i), y);
      });
      ctx.lineTo(xAt(buckets - 1), base);
      ctx.closePath();
      ctx.fillStyle = rgba(C.wip, 0.16);
      ctx.fill();
      ctx.beginPath();
      stream.wip.forEach((value, i) => {
        const y = base - (value / maxWip) * chartH;
        if (i === 0) ctx.moveTo(xAt(i), y); else ctx.lineTo(xAt(i), y);
      });
      ctx.strokeStyle = rgba(C.wip, 0.85);
      ctx.lineWidth = 1.25;
      ctx.stroke();

      // completions: small ticks under the baseline
      ctx.fillStyle = rgba(C.done, 0.75);
      let totalDone = 0;
      stream.completions.forEach((value, i) => {
        if (!value) return;
        totalDone += value;
        const h = Math.min(4, 1 + value * 1.5);
        ctx.fillRect(xAt(i) - 1, base + 2, 2, h);
      });

      // mean line
      const mean = summary.mean_wip;
      if (mean > 0) {
        const y = base - (mean / maxWip) * chartH;
        ctx.strokeStyle = C.mean;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(GUTTER, y);
        ctx.lineTo(GUTTER + g.plotW, y);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      this.hits.push({ stream, top, bottom: top + ROW_H, base, chartH, stepX, bucketMs, start });
    });

    // axis label for the x scale
    ctx.font = '500 10px "JetBrains Mono", ui-monospace, monospace';
    ctx.fillStyle = C.muted;
    ctx.textAlign = 'left';
    // prd/63 F26: one interval label. The chart divides the window into N
    // equal buckets (≈23 h 44 min), while the payload's sampling basis is the
    // stated bucket_hours (24 h). Quote the payload's number here too, so the
    // canvas and the header cannot disagree.
    const basisMs = this.data && this.data.bucket_hours
      ? this.data.bucket_hours * HOUR
      : bucketMs;
    const basisLabel = this.data && this.data.bucket_hours
      ? `${this.data.bucket_hours} h`
      : fmtCalendarDuration(basisMs);
    ctx.fillText(
      phone
        ? `WIP every ${basisLabel} · scale 0–${fmtWipTick(maxWip)}`
        : `WIP sampled every ${basisLabel} · ` +
          `${this.data && this.data.hours_mode === 'business' ? 'business hours' : 'wall-clock'} · ` +
          `WIP scale 0–${fmtWipTick(maxWip)} items (shared) · completions in grey`,
      phone ? 14 : GUTTER,
      g.height - 8,
    );
  }

  hitTest(x, y) {
    for (const hit of this.hits || []) {
      if (y >= hit.top && y <= hit.bottom && x >= 0) return hit;
    }
    return null;
  }

  showHover(hit, x, y) {
    if (!hit) {
      this.hover = null;
      this.tip.hidden = true;
      this.schedule();
      return;
    }
    const stream = hit.stream;
    const index = Math.max(0, Math.min(stream.wip.length - 1, Math.round((x - GUTTER) / hit.stepX)));
    const at = hit.start + index * hit.bucketMs;
    this.hover = { stream: stream.id, index };
    this.tip.innerHTML = `
      <div class="row"><span class="act">${esc(stream.id)}</span>
        <span class="muted">${esc(streamRoleLabel(stream))}</span></div>
      <div class="row"><span class="k">at</span><span class="v">${esc(fmtStamp(at))}</span></div>
      <div class="row"><span class="k">wip</span><span class="v">${stream.wip[index] ?? 0} waiting</span></div>
      <div class="row"><span class="k">arrived</span><span class="v">${stream.arrivals[index] ?? 0}</span></div>
      <div class="row"><span class="k">left</span><span class="v">${stream.completions[index] ?? 0}</span></div>
      <div class="row"><span class="k">mean wait</span><span class="v">≈ ${esc(fmtHours(stream.summary.mean_wait_hours))}</span></div>
      ${stream.role === 'person' && stream.summary.solo_hours != null
        ? `<div class="row"><span class="k" title="the part of the total where this person was the only open ball — the marginal load; person-hours charge every concurrent holder">solo</span><span class="v">${esc(fmtHours(stream.summary.solo_hours))}${
          stream.summary.solo_share != null ? ` · ${Math.round(stream.summary.solo_share * 100)}%` : ''}</span></div>`
        : ''}`;
    this.tip.hidden = false;
    const stage = this.canvas.parentElement;
    const w = this.tip.offsetWidth || 300;
    const h = this.tip.offsetHeight || 100;
    this.tip.style.left = `${Math.min(Math.max(8, x + 14), stage.clientWidth - w - 8)}px`;
    this.tip.style.top = `${Math.min(Math.max(8, y + 14), stage.clientHeight - h - 8)}px`;
    this.schedule();
  }

  _bind() {
    const canvas = this.canvas;
    canvas.addEventListener('mousemove', (ev) => {
      if (this._touchAt && Date.now() - this._touchAt < 800) return;
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      this.showHover(x >= GUTTER ? this.hitTest(x, y) : null, x, y);
    });
    canvas.addEventListener('mouseleave', () => {
      if (this._touchAt && Date.now() - this._touchAt < 800) return;
      this.hover = null;
      this.tip.hidden = true;
      this.schedule();
    });
    canvas.addEventListener('click', (ev) => {
      const rect = canvas.getBoundingClientRect();
      const hit = this.hitTest(ev.clientX - rect.left, ev.clientY - rect.top);
      if (!hit) return;
      this.selected = this.selected === hit.stream.id ? null : hit.stream.id;
      this.schedule();
      if (this.onSelect) this.onSelect(hit.stream);
    });
    canvas.addEventListener('keydown', (ev) => {
      if (!this.streams.length) return;
      const index = this.streams.findIndex((s) => s.id === (this.hover?.stream || this.selected));
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowRight') {
        const stream = this.streams[(index + 1 + this.streams.length) % this.streams.length];
        this.selected = stream.id;
        if (this.onSelect) this.onSelect(stream);
        this.schedule();
        ev.preventDefault();
      } else if (ev.key === 'ArrowUp' || ev.key === 'ArrowLeft') {
        const stream = this.streams[(index - 1 + this.streams.length) % this.streams.length];
        this.selected = stream.id;
        if (this.onSelect) this.onSelect(stream);
        this.schedule();
        ev.preventDefault();
      } else if (ev.key === 'Escape') {
        this.selected = null;
        this.schedule();
      }
    });
    // prd/28: tap-and-hold is the touch equivalent of hover on the WIP chart.
    const local = (ev) => {
      const rect = canvas.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    };
    let start = null;
    canvas.addEventListener('pointerdown', (ev) => {
      if (ev.pointerType === 'mouse') return;
      this._touchAt = Date.now();
      const point = local(ev);
      start = { ...point, id: ev.pointerId };
      clearTimeout(this._holdTimer);
      this._holdTimer = setTimeout(() => {
        if (!start) return;
        const hit = point.x >= GUTTER ? this.hitTest(point.x, point.y) : null;
        this.showHover(hit, point.x, point.y);
      }, 450);
    });
    canvas.addEventListener('pointermove', (ev) => {
      if (!start || ev.pointerId !== start.id) return;
      const point = local(ev);
      if (Math.hypot(point.x - start.x, point.y - start.y) > 10) {
        clearTimeout(this._holdTimer);
        start = null;
      }
    });
    const end = (ev) => {
      if (!start || ev.pointerId !== start.id) return;
      clearTimeout(this._holdTimer);
      start = null;
    };
    canvas.addEventListener('pointerup', end);
    canvas.addEventListener('pointercancel', end);

    new ResizeObserver(() => this.schedule()).observe(canvas.parentElement);
  }
}

/** B15: 0, half and the top of a shared WIP scale. */
export function wipTicks(max) {
  const top = Math.max(1, Number(max) || 1);
  return [...new Set([0, top / 2, top])].sort((a, b) => a - b);
}

export function fmtWipTick(value) {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

/**
 * prd/42 M1: the response-time comparison, as a column.
 *
 * The queue chart answers "who holds the most wait"; this table answers "who
 * takes longest to answer a review request". The sentinel's median is shown
 * against the peers' median, so the 8× gap is one glance rather than a
 * raw-store computation. A reviewer who never answered is counted, not
 * dropped — that is the difference between "slow" and "asleep".
 *
 * prd/43 §2: the sentinel is a *precedence* gate, so their median is measured
 * from the last peer answer (their ball only arrives then), not from "added
 * as reviewer". The header says which clock each row uses, and the "never"
 * column carries the age of the oldest unanswered request so the two forgotten
 * PRs are a quantity, not a count.
 */
export function responseTable(data, { benchmark = true, scope = null } = {}) {
  const response = data && data.response;
  let people = (response && response.people) || [];
  if (scope && scope.kind !== 'all') {
    const value = String(scope.value || '').toLowerCase();
    people = people.filter((row) => (scope.kind === 'person'
      ? String(row.person).toLowerCase() === value
      : (scope.kind === 'role'
        ? String(row.role).toLowerCase() === value
          || (value === 'sentinel' && row.role === 'sentinel')
        : true)));
  }
  // prd/63 F26: an empty scoped table is a result, not a blank panel.
  // prd/68 F-01: `scope` is null on the unscoped Queue page (`scope=all`), so
  // the scoped sentence must not dereference `scope.label` — it used to throw a
  // TypeError here and leave the legend, assumption and honesty blank below.
  if (!people.length && response && scope) {
    return `<section class="response-panel" aria-labelledby="response-title">
      <div class="response-head"><h2 class="policy-h" id="response-title">Response time</h2>
        <span class="muted">added as reviewer → first response</span></div>
      <p class="empty-row">No review requests for scope ${esc(scope.label || scope.value)} in this window —
        the person has no reviewer queue here.</p></section>`;
  }
  // prd/68 F-01: unscoped and empty — say so plainly. An empty result is a
  // result (a Jira-only store has no reviewer queue); it is not a crash.
  if (!people.length && response) {
    return `<section class="response-panel" aria-labelledby="response-title">
      <div class="response-head"><h2 class="policy-h" id="response-title">Response time</h2>
        <span class="muted">added as reviewer → first response</span></div>
      <p class="empty-row">No review requests in this window.</p></section>`;
  }
  if (!people.length) return '';
  const peer = response.reviewer_median_hours;
  const gateAware = response.sentinel_after_peers !== false;
  const rows = people.map((row) => {
    const ratio = row.role === 'sentinel' && peer && row.median_hours
      ? `×${(row.median_hours / peer).toFixed(1)}`
      : '';
    const lag = row.sentinel_lag_median_hours != null
      ? `${esc(fmtHours(row.sentinel_lag_median_hours))}`
      : '—';
    const clock = row.role === 'sentinel' && gateAware
      ? 'last peer answer → first response (the sentinel cannot act earlier)'
      : 'added as reviewer → first response';
    const neverTitle = row.never_oldest_hours != null
      ? `asked but never answered · oldest open request ${fmtHours(row.never_oldest_hours)} — only open PRs whose gate opened are counted`
      : 'asked but never answered — only open PRs whose gate opened are counted';
    // prd/63 F20/V-A8: the hint promised "click a row to scope" but these rows
    // did nothing. Make the row a real pivot, and split the unrequited count
    // into "only on this sentinel" vs "another holder still owes".
    const only = row.never_only_holder || 0;
    const others = row.never_other_holder || 0;
    const neverText = row.never
      ? `${row.never}${only ? ` <span class="muted" title="no peer and no author owes — the sentinel is the only holder">(${only} only)</span>` : ''}${others ? ` <span class="muted" title="a peer or the author must act first">+${others}</span>` : ''}`
      : '0';
    return `<tr data-response-person="${esc(row.person)}" data-response-role="${esc(row.role)}"
        class="response-row ${row.role === 'sentinel' ? 'sentinel' : ''}"
        tabindex="0" role="button"
        title="click to scope the queue view to ${esc(row.person)}">
      <td class="c-who" data-label="person"><span class="mono">${esc(row.person)}</span>
        ${row.role === 'sentinel' ? '<span class="pill warning" title="mandatory approver on every PR">sentinel</span>' : ''}</td>
      <td class="c-dur num" data-label="median response" title="${esc(clock)}">${
        row.median_hours == null ? '—' : esc(fmtHours(row.median_hours))}${
        benchmarkInline(row.response_benchmark, { enabled: benchmark })}${
        ratio ? ` <span class="muted" title="the sentinel's median ÷ the peer reviewers' median response">${ratio} peers</span>` : ''}</td>
      <td class="c-dur num" data-label="mean">${
        row.mean_hours == null ? '—' : `≈${esc(fmtHours(row.mean_hours))}`}</td>
      <td class="c-dur num" data-label="p90">${
        row.p90_hours == null ? '—' : esc(fmtHours(row.p90_hours))}</td>
      <td class="c-dur num" data-label="never" title="${esc(neverTitle)}">${neverText}${
        row.never_oldest_hours != null
          // prd/64 F-16: the age belongs to the *total*, not to the "+5" it sat
          // beside; "(oldest of 8: 21 d 4 h)" cannot be read as "+5 aged 21 d 4 h".
          ? ` <span class="muted">(oldest of ${row.never}: ${esc(fmtHours(row.never_oldest_hours))})</span>` : ''}</td>
      <td class="c-dur num" data-label="peer → sentinel">${lag}</td>
    </tr>`;
  }).join('');
  const canonical = response.sentinel_wait || {};
  const neverExcluded = response.never_excluded || {};
  const excluded = (neverExcluded.closed_prs || 0) + (neverExcluded.gate_not_open || 0);
  return `<section class="response-panel" aria-labelledby="response-title">
    <div class="response-head">
      <h2 class="policy-h" id="response-title">Response time</h2>
      <span class="muted">added as reviewer → first response${
        gateAware ? ' · sentinel: last peer answer first' : ''}${
        peer != null ? ` · peer median ${esc(fmtHours(peer))}` : ''}</span>
    </div>
    ${canonical.definition
      ? `<p class="panel-note response-definition" data-sentinel-wait="${
        canonical.median_hours == null ? '' : canonical.median_hours}">
        <b>sentinel wait</b> (the one canonical number): ${esc(canonical.definition)}${
        canonical.median_hours == null ? '' : ` — median ${esc(fmtHours(canonical.median_hours))} over ${canonical.n} request${canonical.n === 1 ? '' : 's'}`}${
        excluded ? ` · ${excluded} unowed request${excluded === 1 ? '' : 's'} excluded (PR already closed or peer gate never opened)` : ''}.</p>`
      : ''}
    <table class="table compact response-table"><thead><tr>
      <th class="c-who">person</th><th class="c-dur num">median</th>
      <th class="c-dur num">mean</th><th class="c-dur num">p90</th>
      <th class="c-dur num" title="asked but never answered — only open PRs whose gate opened">never</th>
      <th class="c-dur num" title="last peer approval → sentinel approval">peer → sentinel</th>
    </tr></thead><tbody>${rows}</tbody></table>
    <p class="panel-note">${esc((response && response.basis) || '')}. ${
      esc((response && response.sentinel_lag_basis) || '')}.</p>
  </section>`;
}

/** prd/42 M7: a pseudonym legend by role — never a name (prd/02). */
export function legendMarkup(data) {
  const legend = data && data.legend;
  const people = (legend && legend.people) || [];
  if (!people.length) return '';
  const chips = people.map((row) => `<span class="legend-person" data-role="${esc(row.role)}"
      title="${esc(`${row.person}: ${row.role} · ${row.prs_authored} PRs authored · ${row.approvals} approvals · ${row.stories_owned} stories owned`)}">
      <span class="mono">${esc(row.person)}</span>
      <span class="legend-role">${esc(row.role)}</span></span>`).join('');
  return `<div class="people-legend" aria-label="pseudonym roles">
    <span class="legend-label">who is who</span>${chips}
    <span class="legend-note">${esc((legend && legend.note) || '')}</span></div>`;
}

/** Shorten text with an ellipsis so a canvas label never paints past its box. */
function fitText(ctx, text, maxWidth) {
  if (maxWidth <= 0) return '';
  if (ctx.measureText(text).width <= maxWidth) return text;
  let value = text;
  while (value.length > 1 && ctx.measureText(`${value}…`).width > maxWidth) value = value.slice(0, -1);
  return `${value}…`;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

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

import { fmtStamp, fmtDuration, fmtHours, esc, rgba } from './format.js';

const ROW_H = 62;
const GUTTER = 232;
const AXIS_H = 26;
const BOTTOM = 22;

const WIP = '#63B1FF';
const DONE = '#B9BBC0';   // neutral: green is reserved for the 'healthy' state

export const DIAGNOSIS = {
  overloaded: { label: 'overloaded', color: '#F05C70', tint: 'rgba(240,92,112,.14)' },
  slow: { label: 'slow', color: '#FBCD58', tint: 'rgba(251,205,88,.14)' },
  healthy: { label: 'healthy', color: '#73CF55', tint: 'rgba(115,207,85,.12)' },
};

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
    this._bind();
  }

  setData(data) {
    this.data = data;
    this.streams = (data && data.streams) || [];
    if (this.selected && !this.streams.some((s) => s.id === this.selected)) this.selected = null;
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
    return { width, height, plotW: Math.max(120, width - GUTTER - 18), plotH: rows * ROW_H };
  }

  render() {
    const ctx = this.ctx;
    const g = this.geometry();
    ctx.clearRect(0, 0, g.width, g.height);
    ctx.fillStyle = '#151924';
    ctx.fillRect(0, 0, g.width, g.height);
    this.hits = [];

    if (!this.streams.length) {
      ctx.fillStyle = '#8A8D93';
      ctx.font = '500 12px Inter, system-ui, sans-serif';
      ctx.fillText('No measured waits in this window.', GUTTER, AXIS_H + 24);
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
    ctx.fillStyle = '#8A8D93';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    const ticks = 5;
    for (let i = 0; i < ticks; i += 1) {
      const t = start + ((end - start) * i) / (ticks - 1);
      const x = GUTTER + (i / (ticks - 1)) * g.plotW;
      ctx.textAlign = i === 0 ? 'left' : i === ticks - 1 ? 'right' : 'center';
      ctx.fillText(fmtStamp(t).slice(0, 16), x, 12);
    }
    ctx.strokeStyle = 'rgba(255,255,255,.06)';
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
        ctx.fillStyle = isSelected ? 'rgba(99,177,255,.08)' : 'rgba(255,255,255,.025)';
        ctx.fillRect(0, top, g.width, ROW_H);
      }
      ctx.strokeStyle = 'rgba(255,255,255,.035)';
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
      ctx.fillStyle = '#E8E9EB';
      ctx.fillText(stream.id, 14, top + 16);
      ctx.font = '500 10px "JetBrains Mono", ui-monospace, monospace';
      ctx.fillStyle = '#8A8D93';
      const role = stream.people && stream.people.length
        ? `${stream.role} · ${stream.people.length} ppl`
        : stream.role;
      ctx.fillText(role, 92, top + 16);
      // diagnosis pill
      const pillX = 194;
      ctx.fillStyle = diagnosis.tint;
      roundRect(ctx, pillX, top + 9, 76, 15, 7);
      ctx.fill();
      ctx.fillStyle = diagnosis.color;
      ctx.fillText(`≈ ${diagnosis.label}`, pillX + 7, top + 17);

      ctx.font = '500 10.5px "JetBrains Mono", ui-monospace, monospace';
      ctx.fillStyle = '#B9BBC0';
      ctx.fillText(
        `${summary.mean_wip.toFixed(1)} wip · ≈${summary.mean_wait_hours.toFixed(1)} h mean · ${summary.throughput_per_day.toFixed(2)}/d`,
        14, top + 34,
      );
      ctx.fillStyle = '#6B6F76';
      ctx.fillText(`${summary.items} PRs · peak ${summary.peak_wip}`, 14, top + 47);

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
      ctx.fillStyle = rgba(WIP, 0.16);
      ctx.fill();
      ctx.beginPath();
      stream.wip.forEach((value, i) => {
        const y = base - (value / maxWip) * chartH;
        if (i === 0) ctx.moveTo(xAt(i), y); else ctx.lineTo(xAt(i), y);
      });
      ctx.strokeStyle = rgba(WIP, 0.85);
      ctx.lineWidth = 1.25;
      ctx.stroke();

      // completions: small ticks under the baseline
      ctx.fillStyle = rgba(DONE, 0.75);
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
        ctx.strokeStyle = 'rgba(232,233,235,.22)';
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
    ctx.fillStyle = '#8A8D93';
    ctx.textAlign = 'left';
    ctx.fillText(
      `WIP sampled every ${fmtDuration(bucketMs)} · ` +
        `${this.data && this.data.hours_mode === 'business' ? 'business hours' : 'wall-clock'} · ` +
        'completions in grey',
      GUTTER, g.height - 8,
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
        <span class="muted">${esc(stream.role)}</span></div>
      <div class="row"><span class="k">at</span><span class="v">${esc(fmtStamp(at))}</span></div>
      <div class="row"><span class="k">wip</span><span class="v">${stream.wip[index] ?? 0} waiting</span></div>
      <div class="row"><span class="k">arrived</span><span class="v">${stream.arrivals[index] ?? 0}</span></div>
      <div class="row"><span class="k">left</span><span class="v">${stream.completions[index] ?? 0}</span></div>
      <div class="row"><span class="k">mean wait</span><span class="v">≈ ${esc(fmtHours(stream.summary.mean_wait_hours))}</span></div>`;
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
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      this.showHover(x >= GUTTER ? this.hitTest(x, y) : null, x, y);
    });
    canvas.addEventListener('mouseleave', () => {
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
    new ResizeObserver(() => this.schedule()).observe(canvas.parentElement);
  }
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

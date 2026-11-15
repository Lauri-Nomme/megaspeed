/* megaspeed timeline — custom Canvas 2D renderer (no chart library, no build).
 *
 * LTR time axis, swimlanes, activity markers, PR lifecycle bars, semantic-ish
 * LOD (aggregate when several events land in one pixel column), zoom + pan.
 *
 * Everything it draws is a ticket key, a PR id, a commit hash, a pseudonym, a
 * timestamp or a closed label. There is no raw text in the data model.
 */

const COLORS = {
  critical: '#F05C70',
  warning: '#FBCD58',
  healthy: '#73CF55',
  info: '#63B1FF',
  cat1: '#73CF55',
  cat2: '#53C9DF',
  cat3: '#B565E8',
  cat4: '#E25AB5',
  cat5: '#E8B84A',
  cat6: '#C04B56',
  cat7: '#8A8D93',
  teal: '#3CB49A',
  text1: '#E8E9EB',
  text2: '#919397',
  text3: '#606165',
  hairline: 'rgba(255,255,255,.06)',
  canvas: '#151924',
};

export const ACTIVITY_ORDER = [
  'pr_opened', 'pr_pushed', 'comment_added', 'pr_approved', 'pr_unapproved',
  'pr_needs_work', 'pr_merged', 'pr_declined', 'reviewer_changed',
];

export const ACTIVITY_COLOR = {
  pr_opened: COLORS.cat2,
  pr_pushed: COLORS.cat3,
  comment_added: COLORS.cat7,
  pr_approved: COLORS.healthy,
  pr_unapproved: COLORS.critical,
  pr_needs_work: COLORS.warning,
  pr_merged: COLORS.teal,
  pr_declined: COLORS.cat6,
  reviewer_changed: COLORS.cat4,
};

const PHASE = {
  waiting: { color: COLORS.warning, label: 'waiting on author' },
  review: { color: COLORS.info, label: 'in review' },
  active: { color: COLORS.cat3, label: 'in progress' },
  done: { color: '#8A8D93', label: 'closed' },
};

const METRICS = {
  S: { lane: 15, bar: 11, label: 10, marker: 2.2 },
  M: { lane: 22, bar: 15, label: 11, marker: 3 },
  L: { lane: 30, bar: 21, label: 12, marker: 3.8 },
};

const GUTTER = 164;
const AXIS_H = 34;
const HIST_H = 38;
const HEADER_H = 22;
const SECTION_GAP = 18;
const BOTTOM = 14;

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const STEPS = [
  MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE,
  HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR, DAY, 2 * DAY, 7 * DAY, 14 * DAY, 28 * DAY, 91 * DAY,
];

const pad = (n) => String(n).padStart(2, '0');

function fmtTick(ms, step) {
  const d = new Date(ms);
  if (step >= DAY) return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

export function fmtStamp(ms) {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
}

export function fmtDay(ms) {
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}

function fmtDuration(ms) {
  if (ms < MINUTE) return '0 min';
  const mins = Math.round(ms / MINUTE);
  if (mins < 60) return `${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} h ${mins % 60 ? `${mins % 60} min` : ''}`.trim();
  const days = Math.floor(hours / 24);
  return `${days} d ${hours % 24} h`;
}

function rgba(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function phaseOf(activity) {
  switch (activity) {
    case 'pr_opened':
    case 'pr_pushed':
    case 'pr_approved':
      return 'review';
    case 'comment_added':
    case 'pr_needs_work':
    case 'pr_unapproved':
      return 'waiting';
    case 'pr_merged':
    case 'pr_declined':
      return 'done';
    default:
      return null;
  }
}

export class Timeline {
  constructor({ canvas, tip, onStats }) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.tip = tip;
    this.onStats = onStats;
    this.density = 'M';
    this.dim = 'actor';
    this.hidden = new Set();
    this.events = [];
    this.prs = [];
    this.now = Date.now();
    this.domain = { start: 0, end: 1 };
    this.view = { start: 0, end: 1 };
    this.hits = [];
    this.hover = null;
    this.drag = null;
    this.dirty = false;
    this.raf = 0;
    this._bind();
  }

  // ---------------------------------------------------------------- data
  setData(events, objects, meta = {}) {
    this.events = events
      .map((e) => ({ ...e, t: Date.parse(e.ts) }))
      .sort((a, b) => a.t - b.t);
    const prs = new Map();
    for (const obj of objects) {
      if (obj.object_type !== 'PR') continue;
      prs.set(obj.object_id, {
        id: obj.object_id,
        number: obj.pr_id,
        repo: (obj.repo || '').split('/').pop(),
        keys: obj.ticket_keys || [],
        created: Date.parse(obj.created_at),
        closed: obj.closed_at ? Date.parse(obj.closed_at) : null,
        state: obj.state || 'open',
        actor: obj.actor,
        events: [],
      });
    }
    for (const event of this.events) {
      const pr = prs.get(`PR:${event.repo}#${event.pr_id}`);
      if (pr) pr.events.push(event);
    }
    this.prs = [...prs.values()].sort((a, b) => a.created - b.created);
    this.now = meta.as_of ? Date.parse(meta.as_of) : (meta.last_ts ? Date.parse(meta.last_ts) : Date.now());
    const times = this.events.map((e) => e.t).concat(this.prs.map((p) => p.created));
    const start = Math.min(...times);
    const end = Math.max(...times, ...this.prs.map((p) => p.closed || this.now));
    const padMs = Math.max((end - start) * 0.015, 10 * MINUTE);
    this.domain = { start: start - padMs, end: end + padMs };
    this.view = { ...this.domain };
    this.fit();
  }

  setDensity(d) {
    this.density = d;
    this.schedule();
  }

  setLanes(dim) {
    this.dim = dim;
    this.schedule();
  }

  toggleActivity(activity) {
    if (this.hidden.has(activity)) this.hidden.delete(activity);
    else this.hidden.add(activity);
    this.schedule();
  }

  visibleEvents() {
    return this.events.filter((e) => !this.hidden.has(e.activity));
  }

  // ---------------------------------------------------------------- view
  fit() {
    this.view = { ...this.domain };
    this.schedule();
  }

  span() {
    return Math.max(this.view.end - this.view.start, 60_000);
  }

  zoom(factor, anchorPx = null) {
    const g = this.geom;
    const span = this.span();
    const anchor = anchorPx == null ? (g ? g.gutter + g.plotW / 2 : this.canvas.clientWidth / 2) : anchorPx;
    const t = this.timeAt(anchor);
    let next = span * factor;
    next = Math.max(10 * MINUTE, Math.min(next, (this.domain.end - this.domain.start) * 2));
    const ratio = (anchor - (g ? g.gutter : 0)) / Math.max(1, g ? g.plotW : this.canvas.clientWidth);
    this.view = { start: t - next * ratio, end: t + next * (1 - ratio) };
    this.clampView();
    this.schedule();
  }

  panPixels(dx) {
    const g = this.geom;
    if (!g) return;
    const dt = (dx / g.plotW) * this.span();
    this.view.start -= dt;
    this.view.end -= dt;
    this.clampView();
    this.schedule();
  }

  clampView() {
    const span = Math.min(this.span(), (this.domain.end - this.domain.start) * 2);
    const slack = Math.max(span * 0.2, HOUR);
    const lo = this.domain.start - slack;
    const hi = this.domain.end + slack - span;
    let start = Math.min(this.view.start, hi);
    start = Math.max(start, lo);
    this.view = { start, end: start + span };
  }

  timeAt(px) {
    const g = this.geom;
    if (!g) return this.view.start;
    const x = Math.min(Math.max(px, g.gutter), g.gutter + g.plotW);
    return this.view.start + ((x - g.gutter) / g.plotW) * this.span();
  }

  px(t) {
    const g = this.geom;
    return g.gutter + ((t - this.view.start) / this.span()) * g.plotW;
  }

  // ---------------------------------------------------------------- lanes
  laneRows() {
    const events = this.visibleEvents();
    const counts = new Map();
    for (const e of events) {
      const key = this.laneKey(e);
      counts.set(key, (counts.get(key) || 0) + 1);
    }
    let keys = [...counts.keys()];
    if (this.dim === 'activity') {
      keys = keys.sort((a, b) => ACTIVITY_ORDER.indexOf(a) - ACTIVITY_ORDER.indexOf(b));
    } else if (this.dim === 'object') {
      keys = keys.sort((a, b) => Number(a.replace('#', '')) - Number(b.replace('#', '')));
    } else {
      keys = keys.sort((a, b) => (counts.get(b) - counts.get(a)) || a.localeCompare(b));
    }
    return keys.map((key) => ({ key, count: counts.get(key) }));
  }

  laneKey(event) {
    if (this.dim === 'actor') return event.actor;
    if (this.dim === 'activity') return event.activity;
    return `#${event.pr_id}`;
  }

  // ---------------------------------------------------------------- layout
  layout() {
    const m = METRICS[this.density];
    const lanes = this.laneRows();
    const barRows = this.prs;
    const height = AXIS_H + HIST_H + HEADER_H + lanes.length * m.lane + SECTION_GAP +
      HEADER_H + barRows.length * m.bar + BOTTOM;
    const width = this.canvas.parentElement.clientWidth || 1200;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.geom = {
      width, height, gutter: GUTTER, plotW: Math.max(80, width - GUTTER - 10),
      axisH: AXIS_H, histH: HIST_H, headerH: HEADER_H, laneH: m.lane, barH: m.bar,
      label: m.label, marker: m.marker,
      laneTop: AXIS_H + HIST_H + HEADER_H,
      barTop: AXIS_H + HIST_H + HEADER_H + lanes.length * m.lane + SECTION_GAP + HEADER_H,
      lanes, barRows,
    };
    return this.geom;
  }

  // ---------------------------------------------------------------- draw
  schedule() {
    this.dirty = true;
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = 0;
      if (this.dirty) this.render();
    });
  }

  render() {
    this.dirty = false;
    const ctx = this.ctx;
    const g = this.layout();
    const m = METRICS[this.density];
    this.hits = [];

    ctx.clearRect(0, 0, g.width, g.height);
    ctx.fillStyle = COLORS.canvas;
    ctx.fillRect(0, 0, g.width, g.height);

    this.drawTimeBands(ctx, g);
    this.drawAxis(ctx, g);
    this.drawHistogram(ctx, g);
    this.drawLanes(ctx, g, m);
    this.drawBars(ctx, g, m);

    // gutter edge + axis baseline
    ctx.strokeStyle = COLORS.hairline;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(g.gutter + 0.5, 0);
    ctx.lineTo(g.gutter + 0.5, g.height);
    ctx.stroke();

    if (this.onStats) {
      this.onStats({
        start: this.view.start,
        end: this.view.end,
        visible: this.visibleEvents().length,
        total: this.events.length,
        span: this.span(),
        bucketMs: this.span() / g.plotW,
        aggregated: this.aggregated > 0,
        openPrs: this.prs.filter((p) => p.state === 'open').length,
      });
    }
  }

  drawTimeBands(ctx, g) {
    const dayWidth = g.plotW / (this.span() / DAY);
    if (dayWidth < 26) return;
    const first = Math.floor(this.view.start / DAY) * DAY;
    for (let t = first; t < this.view.end; t += DAY) {
      const d = new Date(t);
      const weekend = d.getUTCDay() === 0 || d.getUTCDay() === 6;
      const x0 = Math.max(g.gutter, this.px(t));
      const x1 = Math.min(g.gutter + g.plotW, this.px(t + DAY));
      if (x1 <= x0) continue;
      if (weekend) {
        ctx.fillStyle = 'rgba(255,255,255,.022)';
        ctx.fillRect(x0, AXIS_H, x1 - x0, g.height - AXIS_H);
      }
      const night0 = this.px(t + 18 * HOUR);
      const night1 = this.px(t + DAY);
      if (night1 > night0) {
        ctx.fillStyle = 'rgba(0,0,0,.18)';
        ctx.fillRect(night0, AXIS_H, night1 - night0, g.height - AXIS_H);
      }
      const night2 = this.px(t);
      const night3 = this.px(t + 8 * HOUR);
      if (night3 > night2) {
        ctx.fillStyle = 'rgba(0,0,0,.18)';
        ctx.fillRect(night2, AXIS_H, night3 - night2, g.height - AXIS_H);
      }
    }
  }

  drawAxis(ctx, g) {
    const span = this.span();
    let step = STEPS[STEPS.length - 1];
    for (const candidate of STEPS) {
      if (span / candidate <= 9) { step = candidate; break; }
    }
    this.tickStep = step;

    const first = Math.ceil(this.view.start / step) * step;
    ctx.font = `500 ${g.label}px "JetBrains Mono", ui-monospace, monospace`;
    ctx.textBaseline = 'middle';

    let lastDayLabel = null;
    for (let t = first; t <= this.view.end; t += step) {
      const x = this.px(t);
      if (x < g.gutter || x > g.gutter + g.plotW) continue;
      const major = step >= DAY || new Date(t).getUTCHours() === 0;
      ctx.strokeStyle = major ? 'rgba(255,255,255,.10)' : 'rgba(255,255,255,.045)';
      ctx.beginPath();
      ctx.moveTo(Math.round(x) + 0.5, AXIS_H - 6);
      ctx.lineTo(Math.round(x) + 0.5, g.height - BOTTOM);
      ctx.stroke();

      if (step < DAY && new Date(t).getUTCHours() === 0) {
        const day = fmtDay(t);
        if (day !== lastDayLabel) {
          lastDayLabel = day;
          ctx.fillStyle = COLORS.text2;
          ctx.textAlign = 'left';
          ctx.fillText(day, x + 4, 10);
        }
      }
      ctx.fillStyle = major ? COLORS.text2 : COLORS.text3;
      ctx.textAlign = 'left';
      ctx.fillText(fmtTick(t, step), x + 4, AXIS_H - 12);
    }
  }

  drawHistogram(ctx, g) {
    const events = this.visibleEvents().filter((e) => e.t >= this.view.start && e.t <= this.view.end);
    const buckets = new Map();
    let max = 0;
    for (const e of events) {
      const x = Math.floor(this.px(e.t));
      const n = (buckets.get(x) || 0) + 1;
      buckets.set(x, n);
      if (n > max) max = n;
    }
    const top = AXIS_H + 6;
    const height = HIST_H - 14;
    ctx.font = `500 ${Math.max(9, g.label - 1)}px "JetBrains Mono", ui-monospace, monospace`;
    ctx.fillStyle = COLORS.text3;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillText('events', g.gutter - 10, top + height / 2);

    for (const [x, n] of buckets) {
      const h = Math.max(2, Math.round((n / Math.max(1, max)) * height));
      ctx.fillStyle = n > 1 ? 'rgba(99,177,255,.55)' : 'rgba(99,177,255,.30)';
      ctx.fillRect(x, top + height - h, 2, h);
    }
    ctx.strokeStyle = COLORS.hairline;
    ctx.beginPath();
    ctx.moveTo(g.gutter, AXIS_H + HIST_H - 0.5);
    ctx.lineTo(g.width, AXIS_H + HIST_H - 0.5);
    ctx.stroke();
  }

  drawLanes(ctx, g, m) {
    const events = this.visibleEvents().filter((e) => e.t >= this.view.start && e.t <= this.view.end);
    const rows = g.lanes;

    ctx.font = `500 ${g.label}px "JetBrains Mono", ui-monospace, monospace`;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'right';
    ctx.fillStyle = COLORS.text3;
    ctx.fillText(this.dim === 'actor' ? 'ACTORS' : this.dim === 'object' ? 'PULL REQUESTS' : 'ACTIVITIES',
      g.gutter - 10, AXIS_H + HIST_H + 11);
    ctx.textAlign = 'left';
    ctx.fillStyle = COLORS.text2;
    ctx.font = `600 ${Math.max(10, g.label - 1)}px Inter, system-ui, sans-serif`;
    ctx.fillText(`${rows.length}`, g.gutter + 4, AXIS_H + HIST_H + 11);

    rows.forEach((row, i) => {
      const y = g.laneTop + i * g.laneH;
      if (i % 2 === 0) {
        ctx.fillStyle = 'rgba(255,255,255,.012)';
        ctx.fillRect(g.gutter, y, g.plotW, g.laneH);
      }
      ctx.strokeStyle = 'rgba(255,255,255,.035)';
      ctx.beginPath();
      ctx.moveTo(g.gutter, Math.round(y + g.laneH) - 0.5);
      ctx.lineTo(g.width, Math.round(y + g.laneH) - 0.5);
      ctx.stroke();

      ctx.font = `500 ${g.label}px "JetBrains Mono", ui-monospace, monospace`;
      ctx.fillStyle = this.hover && this.hover.laneKey === row.key ? COLORS.text1 : COLORS.text2;
      ctx.textAlign = 'right';
      const label = this.dim === 'object' ? row.key : row.key;
      ctx.fillText(label.length > 18 ? `${label.slice(0, 17)}…` : label, g.gutter - 10, y + g.laneH / 2);
      ctx.textAlign = 'left';
      ctx.fillStyle = COLORS.text3;
      ctx.font = `500 ${Math.max(9, g.label - 2)}px "JetBrains Mono", ui-monospace, monospace`;
      ctx.fillText(String(row.count), g.gutter + 4, y + g.laneH / 2);
    });

    // markers, with per-pixel aggregation
    const index = new Map(rows.map((r, i) => [r.key, i]));
    const perPixel = new Map();
    for (const e of events) {
      const lane = index.get(this.laneKey(e));
      if (lane == null) continue;
      const x = Math.round(this.px(e.t));
      const key = `${lane}:${x}`;
      const bucket = perPixel.get(key);
      if (bucket) bucket.items.push(e);
      else perPixel.set(key, { lane, x, items: [e] });
    }
    let aggregated = 0;
    for (const bucket of perPixel.values()) {
      const y = g.laneTop + bucket.lane * g.laneH + g.laneH / 2;
      if (bucket.items.length > 1) {
        aggregated += bucket.items.length;
        const step = this.span() / g.plotW;
        const fromX = this.px(Math.min(...bucket.items.map((e) => e.t)));
        const toX = Math.max(this.px(Math.max(...bucket.items.map((e) => e.t))), fromX + 2);
        ctx.fillStyle = rgba(COLORS.info, 0.7);
        ctx.fillRect(fromX, y - g.laneH / 2 + 3, Math.max(2, toX - fromX), g.laneH - 6);
        this.hits.push({ kind: 'bucket', x0: fromX, x1: toX, y0: y - g.laneH / 2, y1: y + g.laneH / 2, items: bucket.items, step });
        continue;
      }
      const event = bucket.items[0];
      const color = ACTIVITY_COLOR[event.activity] || COLORS.cat7;
      const r = g.marker;
      ctx.beginPath();
      ctx.arc(bucket.x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      if (this.hover && this.hover.event && this.hover.event.key === event.key) {
        ctx.strokeStyle = COLORS.text1;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      this.hits.push({ kind: 'event', x: bucket.x, y, r: r + 4, event });
    }
    this.aggregated = aggregated;
  }

  drawBars(ctx, g, m) {
    ctx.font = `600 ${Math.max(10, g.label - 1)}px Inter, system-ui, sans-serif`;
    ctx.textAlign = 'right';
    ctx.fillStyle = COLORS.text3;
    ctx.fillText('PR LIFECYCLE', g.gutter - 10, g.barTop - 11);
    ctx.textAlign = 'left';
    ctx.fillStyle = COLORS.text2;
    ctx.fillText(`${g.barRows.length}`, g.gutter + 4, g.barTop - 11);

    g.barRows.forEach((pr, i) => {
      const y = g.barTop + i * g.barH;
      const isHover = this.hover && this.hover.pr === pr;
      if (i % 2 === 0 || isHover) {
        ctx.fillStyle = isHover ? 'rgba(99,177,255,.07)' : 'rgba(255,255,255,.012)';
        ctx.fillRect(g.gutter, y, g.plotW, g.barH);
      }

      ctx.font = `500 ${g.label}px "JetBrains Mono", ui-monospace, monospace`;
      ctx.textAlign = 'right';
      ctx.fillStyle = isHover ? COLORS.text1 : COLORS.text2;
      const key = pr.keys[0] ? `${pr.keys[0]} ` : '';
      const label = `${key}#${pr.number} ${pr.repo}`;
      ctx.fillText(label.length > 20 ? label.slice(0, 19) + '…' : label, g.gutter - 10, y + g.barH / 2);

      const segments = this.segments(pr);
      const barY = y + 2;
      const barH = g.barH - 4;
      for (const seg of segments) {
        const x0 = Math.max(g.gutter, this.px(seg.from));
        const x1 = Math.min(g.gutter + g.plotW, this.px(seg.to));
        if (x1 <= x0) continue;
        const color = PHASE[seg.phase].color;
        ctx.fillStyle = rgba(color, seg.phase === 'done' ? 0.22 : 0.34);
        ctx.fillRect(x0, barY, Math.max(1, x1 - x0), barH);
        // a saturated cap on the left edge of the segment keeps phase
        // boundaries legible without turning the bar into a solid block
        ctx.fillStyle = rgba(color, 0.9);
        ctx.fillRect(x0, barY, Math.min(2, x1 - x0), barH);
      }
      // event ticks inside the bar
      if (barH >= 9) {
        for (const event of pr.events) {
          if (event.t < this.view.start || event.t > this.view.end) continue;
          if (this.hidden.has(event.activity)) continue;
          const x = this.px(event.t);
          ctx.fillStyle = rgba(ACTIVITY_COLOR[event.activity] || COLORS.cat7, 0.85);
          ctx.fillRect(x, barY, 1.5, barH);
        }
      }
      // right-censored open PRs end with a triangle at the snapshot time
      const end = pr.closed || this.now;
      if (!pr.closed) {
        const x = Math.min(g.gutter + g.plotW, this.px(end));
        ctx.fillStyle = rgba(COLORS.warning, 0.75);
        ctx.beginPath();
        ctx.moveTo(x, barY);
        ctx.lineTo(x + 4, barY + barH / 2);
        ctx.lineTo(x, barY + barH);
        ctx.closePath();
        ctx.fill();
      }
      this.hits.push({
        kind: 'bar', x0: this.px(pr.created), x1: this.px(end), y0: y, y1: y + g.barH, pr,
      });
    });
  }

  segments(pr) {
    const events = pr.events.filter((e) => !this.hidden.has(e.activity));
    if (!events.length) return [];
    const segments = [];
    let from = events[0].t;
    let phase = phaseOf(events[0].activity) || 'review';
    for (let i = 1; i < events.length; i++) {
      const event = events[i];
      const next = phaseOf(event.activity);
      if (event.t <= from) {
        if (next) phase = next;
        continue;
      }
      segments.push({ from, to: event.t, phase });
      from = event.t;
      if (next) phase = next;
    }
    const end = pr.closed || this.now;
    if (end > from) segments.push({ from, to: end, phase });
    return segments;
  }

  // ---------------------------------------------------------------- hover
  hitTest(cx, cy) {
    for (let i = this.hits.length - 1; i >= 0; i--) {
      const hit = this.hits[i];
      if (hit.kind === 'event') {
        if (Math.hypot(cx - hit.x, cy - hit.y) <= hit.r) return hit;
      } else if (cx >= hit.x0 - 1 && cx <= hit.x1 + 1 && cy >= hit.y0 && cy <= hit.y1) {
        return hit;
      }
    }
    return null;
  }

  showHover(hit, cx, cy) {
    if (!hit) {
      this.tip.hidden = true;
      this.hover = null;
      return;
    }
    this.hover = hit.kind === 'bar' ? { pr: hit.pr } : hit.kind === 'event' ? { event: hit.event } : {};
    if (hit.kind === 'event') {
      const e = hit.event;
      const attrs = Object.entries(e.attributes || {});
      this.tip.innerHTML = `
        <div class="row"><span class="act" style="color:${ACTIVITY_COLOR[e.activity] || COLORS.cat7}">${esc(e.activity)}</span>
          <span class="muted">${esc(fmtStamp(e.t))}</span></div>
        <div class="row"><span class="k">actor</span><span class="v">${esc(e.actor)}</span></div>
        <div class="row"><span class="k">objects</span><span class="v">${e.objects.map(esc).join('<br>')}</span></div>
        ${attrs.length ? `<div class="row"><span class="k">attrs</span><span class="v">${attrs
          .map(([k, v]) => `${esc(k)}=${esc(v)}`).join('<br>')}</span></div>` : ''}`;
    } else if (hit.kind === 'bucket') {
      const first = hit.items[0];
      const last = hit.items[hit.items.length - 1];
      this.tip.innerHTML = `
        <div class="row"><span class="act">${hit.items.length} events</span>
          <span class="muted">≤ ${esc(fmtDuration(hit.step))} apart</span></div>
        <div class="row"><span class="k">lane</span><span class="v">${esc(this.laneKey(first))}</span></div>
        <div class="row"><span class="k">window</span><span class="v">${esc(fmtStamp(first.t))}<br>${esc(fmtStamp(last.t))}</span></div>
        <div class="row"><span class="k">zoom</span><span class="v">zoom in for individual events</span></div>`;
    } else {
      const pr = hit.pr;
      const segments = this.segments(pr);
      const totals = new Map();
      for (const seg of segments) totals.set(seg.phase, (totals.get(seg.phase) || 0) + (seg.to - seg.from));
      const total = segments.length ? segments[segments.length - 1].to - segments[0].from : 0;
      this.tip.innerHTML = `
        <div class="row"><span class="act">#${pr.number} ${esc(pr.repo)}</span>
          <span class="muted">${esc(pr.state)}${pr.closed ? '' : ' · right-censored'}</span></div>
        <div class="row"><span class="k">tickets</span><span class="v">${esc(pr.keys.join(' ')) || '—'}</span></div>
        <div class="row"><span class="k">author</span><span class="v">${esc(pr.actor)}</span></div>
        <div class="row"><span class="k">cycle</span><span class="v">${esc(fmtDuration(total))}</span></div>
        ${[...totals.entries()].map(([phase, ms]) =>
          `<div class="row"><span class="k"></span><span class="v" style="color:${PHASE[phase].color}">${esc(PHASE[phase].label)} ${esc(fmtDuration(ms))}</span></div>`).join('')}`;
    }
    this.tip.hidden = false;
    const stage = this.canvas.parentElement;
    const w = this.tip.offsetWidth || 320;
    const h = this.tip.offsetHeight || 120;
    const left = Math.min(Math.max(8, cx + 14), stage.clientWidth - w - 8);
    const top = Math.min(Math.max(8, cy + 14), stage.clientHeight - h - 8);
    this.tip.style.left = `${left}px`;
    this.tip.style.top = `${top}px`;
  }

  // ---------------------------------------------------------------- input
  _bind() {
    const canvas = this.canvas;

    canvas.addEventListener('mousemove', (ev) => {
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left;
      const y = ev.clientY - rect.top;
      if (this.drag) {
        const dx = x - this.drag.x;
        this.drag.x = x;
        this.panPixels(dx);
        this.tip.hidden = true;
        return;
      }
      this.showHover(this.hitTest(x, y), x, y);
      this.schedule();
    });

    canvas.addEventListener('mouseleave', () => {
      this.hover = null;
      this.tip.hidden = true;
      this.schedule();
    });

    canvas.addEventListener('mousedown', (ev) => {
      const rect = canvas.getBoundingClientRect();
      this.drag = { x: ev.clientX - rect.left };
      canvas.classList.add('dragging');
    });

    window.addEventListener('mouseup', () => {
      if (!this.drag) return;
      this.drag = null;
      this.canvas.classList.remove('dragging');
    });

    canvas.addEventListener('dblclick', (ev) => {
      const rect = canvas.getBoundingClientRect();
      this.zoom(0.5, ev.clientX - rect.left);
    });

    canvas.addEventListener('wheel', (ev) => {
      if (ev.ctrlKey || ev.metaKey) return; // leave browser zoom alone
      ev.preventDefault();
      const rect = canvas.getBoundingClientRect();
      if (ev.shiftKey) {
        this.panPixels(-ev.deltaY / 2);
        return;
      }
      const factor = Math.exp(Math.max(-0.6, Math.min(0.6, ev.deltaY * 0.0015)));
      this.zoom(factor, ev.clientX - rect.left);
    }, { passive: false });

    new ResizeObserver(() => this.schedule()).observe(canvas.parentElement);
  }
}

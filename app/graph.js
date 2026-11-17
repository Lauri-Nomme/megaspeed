/* Blocking map — the weighted directed handover graph (prd/09 View 2).
 *
 * Edge X → Y means "X waited on Y for N hours" (X is the party that handed the
 * item over, Y is the party that held the ball). Thickness ∝ hours. Direction is
 * read from colour relative to the hovered/selected node:
 *
 *   teal  #3CB4AA — who this node waits on (outbound)
 *   amber #D9A24E — who is waiting on this node (inbound)
 *
 * The layout is deterministic and independent of the lens: switching lens
 * recolours the same positions (prd/10 §6 "lenses recolor without relayout").
 */

import { fmtHours, esc, rgba } from './format.js';

function lensText(lens, value) {
  if (lens.unit === 'h') return fmtHours(value);
  if (lens.unit === 'h/d') return `${(Number(value) || 0).toFixed(1)} h/d`;
  return String(Math.round(Number(value) || 0));
}

const TEAL = '#3CB4AA';
const AMBER = '#D9A24E';
const EDGE = 'rgba(70,160,200,.30)';
const LABEL = '#E8E9EB';
const MUTED = '#8A8D93';
const NODE_FILL = '#191C29';
// 13 + 15 * sqrt(1); the collision pass uses this constant instead of the
// per-lens radius so a lens switch recolours without relayout.
const MAX_RADIUS = 28;

export const LENSES = {
  hours: { label: 'wait hours', hint: 'measured wait time attributed to the node', value: (n) => n.hours, unit: 'h' },
  out: { label: 'waiting on others', hint: 'time this node spent waiting on someone else', value: (n) => n.out_hours, unit: 'h' },
  wip: { label: 'WIP now', hint: 'open items currently waiting on this node', value: (n) => n.wip_now, unit: '' },
  intensity: { label: 'wait / day', hint: 'measured wait hours landing on this node per day', value: (n) => n.per_day, unit: 'h/d' },
  mean: { label: 'mean wait', hint: 'mean hours per measured wait', value: (n) => (n.count ? n.hours / n.count : 0), unit: 'h' },
};

const ROLE_COLOR = {
  author: '#B565E8',
  review: '#63B1FF',
  sentinel: '#E8B84A',
  CI: '#53C9DF',
  calendar: '#8A8D93',
};

function lensColor(t) {
  // blue → teal → green → amber → red, the reserved lens gradient
  const stops = [
    [31, 58, 102], [40, 120, 164], [60, 180, 156],
    [156, 212, 90], [242, 193, 78], [240, 138, 60], [232, 69, 90],
  ];
  const clamped = Math.max(0, Math.min(1, t));
  const pos = clamped * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(pos));
  const f = pos - i;
  const mix = (a, b) => Math.round(a + (b - a) * f);
  return `rgb(${mix(stops[i][0], stops[i + 1][0])},${mix(stops[i][1], stops[i + 1][1])},${mix(stops[i][2], stops[i + 1][2])})`;
}

export class BlockingMap {
  constructor({ canvas, tip, onSelect }) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.tip = tip;
    this.onSelect = onSelect;
    this.graph = null;
    this.lens = 'hours';
    this.nodes = [];
    this.edges = [];
    this.hover = null;
    this.selected = null;
    this.renderQueued = false;
    // prd/28: the phone view transform — pan/zoom without touching the layout.
    this.pan = { x: 0, y: 0 };
    this.zoom = 1;
    this._pointers = new Map();
    this._pinch = null;
    this._holdTimer = 0;
    this._touchAt = 0;
    this._lastTap = 0;
    this._bind();
  }

  setData(graph) {
    const previous = new Map(this.nodes.map((n) => [n.id, n]));
    this.graph = graph;
    const nodes = graph.nodes.map((node, index) => {
      const old = previous.get(node.id);
      return {
        ...node,
        x: old ? old.x : 0.5 + 0.42 * Math.cos((index / Math.max(1, graph.nodes.length)) * Math.PI * 2),
        y: old ? old.y : 0.5 + 0.42 * Math.sin((index / Math.max(1, graph.nodes.length)) * Math.PI * 2),
      };
    });
    this.nodes = nodes;
    const byId = new Map(nodes.map((n) => [n.id, n]));
    this.edges = graph.edges.map((edge) => ({ ...edge, a: byId.get(edge.from), b: byId.get(edge.to) }))
      .filter((edge) => edge.a && edge.b);
    if (this.selected && !byId.has(this.selected)) this.selected = null;
    // B13: a tooltip from the previous graph must not survive a level switch.
    this.hover = null;
    if (this.tip) this.tip.hidden = true;
    // only re-run the simulation when the node set changed
    const key = nodes.map((n) => n.id).sort().join('|');
    if (key !== this._layoutKey) {
      this._layoutKey = key;
      this.relax();
    }
    this.schedule();
  }

  setLens(lens) {
    if (!LENSES[lens]) return;
    this.lens = lens;
    this.schedule();
  }

  select(id) {
    this.selected = this.selected === id ? null : id;
    this.schedule();
    return this.selected;
  }

  /** Deterministic force relaxation in normalised space. */
  relax() {
    const nodes = this.nodes;
    if (!nodes.length) return;
    const iterations = 320;
    const edges = this.edges;
    const maxHours = Math.max(1, ...edges.map((e) => e.hours));
    // Short-range collision floor. Without it, nodes with near-identical edge
    // profiles collapse onto one point (and, once clamped, onto one corner).
    const minDist = 0.085;
    const maxRepel = 0.006;
    for (let step = 0; step < iterations; step += 1) {
      const alpha = 1 - step / iterations;
      // repulsion
      for (let i = 0; i < nodes.length; i += 1) {
        for (let j = i + 1; j < nodes.length; j += 1) {
          const a = nodes[i];
          const b = nodes[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let dist = Math.hypot(dx, dy);
          if (dist < 1e-4) { dx = 0.01 * (i - j); dy = 0.01; dist = Math.hypot(dx, dy); }
          // Cap the inverse-square term: uncapped, a dense cluster hurls every
          // node to the plot edge, where the clamp stacks them on top of each
          // other — the opposite of what repulsion is meant to achieve.
          const force = Math.min(maxRepel, (0.00005 * alpha) / (dist * dist));
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          a.x -= fx; a.y -= fy;
          b.x += fx; b.y += fy;
          if (dist < minDist) {
            const push = (minDist - dist) * 0.5 * alpha;
            const nx = dx / dist;
            const ny = dy / dist;
            a.x -= nx * push; a.y -= ny * push;
            b.x += nx * push; b.y += ny * push;
          }
        }
      }
      // springs
      for (const edge of edges) {
        const weight = Math.sqrt(edge.hours / maxHours);
        const rest = 0.52 - 0.22 * weight;
        const dx = edge.b.x - edge.a.x;
        const dy = edge.b.y - edge.a.y;
        const dist = Math.max(1e-4, Math.hypot(dx, dy));
        const force = (dist - rest) * 0.06 * (0.4 + weight) * alpha;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        edge.a.x += fx; edge.a.y += fy;
        edge.b.x -= fx; edge.b.y -= fy;
      }
      // centring
      for (const node of nodes) {
        node.x += (0.5 - node.x) * 0.012 * alpha;
        node.y += (0.5 - node.y) * 0.012 * alpha;
        node.x = Math.min(0.93, Math.max(0.07, node.x));
        node.y = Math.min(0.9, Math.max(0.1, node.y));
      }
    }
  }

  /** Split labels that still overlap at the current canvas size.
   *
   * The force layout works in normalised space and cannot know how wide a
   * label renders, so two nodes with near-identical edge profiles can land on
   * one point (or on the same clamped corner). This deterministic pixel-space
   * pass treats each node, its name and its lens value as one box and pushes
   * colliding pairs apart until they clear. Only render positions move; the
   * graph data is untouched.
   */
  separate(g) {
    const nodes = this.nodes;
    if (nodes.length < 2) return;
    // A hidden page has no box to lay out in; keep the last real positions.
    if (!this.canvas.parentElement || !this.canvas.parentElement.clientWidth) return;
    // A panned/zoomed graph is a camera over a fixed layout; re-separating in
    // screen space would rewrite the layout under the user's fingers.
    if (this.zoom !== 1 || this.pan.x || this.pan.y) return;
    const ctx = this.ctx;
    const previousFont = ctx.font;
    ctx.font = '600 12px Inter, system-ui, sans-serif';
    const maxRadius = this.maxRadius(g);
    const boxes = nodes.map((node) => {
      const p = this.nodePos(node, g);
      const labelW = ctx.measureText(node.id).width;
      return {
        node,
        x: p.x,
        y: p.y,
        // half extents: the largest ring and the centred name label
        hx: Math.max(maxRadius, labelW / 2) + 8,
        // ring + name above + lens value below
        hy: maxRadius + 22,
      };
    });
    ctx.font = previousFont;
    const minX = g.padX * 0.5;
    const maxX = g.width - g.padX * 0.5;
    const minY = g.padY * 0.5;
    const maxY = g.height - g.padY * 0.8;
    const iterations = 80;
    for (let step = 0; step < iterations; step += 1) {
      let moved = false;
      for (let i = 0; i < boxes.length; i += 1) {
        for (let j = i + 1; j < boxes.length; j += 1) {
          const a = boxes[i];
          const b = boxes[j];
          const dx = b.x - a.x;
          const dy = b.y - a.y;
          const ox = a.hx + b.hx - Math.abs(dx);
          const oy = a.hy + b.hy - Math.abs(dy);
          if (ox <= 0 || oy <= 0) continue;
          moved = true;
          if (ox <= oy) {
            const dir = dx === 0 ? (step % 2 === 0 ? 1 : -1) : Math.sign(dx);
            const push = (ox / 2 + 0.5) * dir;
            a.x -= push;
            b.x += push;
          } else {
            const dir = dy === 0 ? (step % 2 === 0 ? 1 : -1) : Math.sign(dy);
            const push = (oy / 2 + 0.5) * dir;
            a.y -= push;
            b.y += push;
          }
        }
      }
      for (const box of boxes) {
        box.x = Math.min(maxX, Math.max(minX, box.x));
        box.y = Math.min(maxY, Math.max(minY, box.y));
      }
      if (!moved) break;
    }
    for (const box of boxes) {
      const normalized = this.normPoint(box.x, box.y, g);
      box.node.x = normalized.x;
      box.node.y = normalized.y;
    }
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
    const height = Math.max(420, this.canvas.parentElement.clientHeight || 620);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    this.canvas.style.height = `${height}px`;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // prd/28: the desktop padding is 90px each side; a 412px phone would lose
    // half its plot to it, so the canvas keeps the room for the rings instead.
    const phone = width <= 640;
    const padX = phone ? 40 : 90;
    const padY = phone ? 44 : 60;
    return { width, height, plotW: width - padX * 2, plotH: height - padY * 2, padX, padY, phone };
  }

  maxRadius(g) {
    return g && g.phone ? 20 : MAX_RADIUS;
  }

  /** Normalised node position -> screen pixels, through the touch transform. */
  nodePos(node, g) {
    const x = g.padX + node.x * g.plotW;
    const y = g.padY + node.y * g.plotH;
    const cx = g.width / 2;
    const cy = g.height / 2;
    return {
      x: cx + (x - cx) * this.zoom + this.pan.x,
      y: cy + (y - cy) * this.zoom + this.pan.y,
    };
  }

  /** Screen pixels -> normalised node space (the inverse of nodePos). */
  normPoint(x, y, g) {
    const cx = g.width / 2;
    const cy = g.height / 2;
    const rawX = cx + (x - this.pan.x - cx) / this.zoom;
    const rawY = cy + (y - this.pan.y - cy) / this.zoom;
    return { x: (rawX - g.padX) / g.plotW, y: (rawY - g.padY) / g.plotH };
  }

  /** Back to the fitted layout: the phone equivalent of "Fit". */
  resetView() {
    this.pan = { x: 0, y: 0 };
    this.zoom = 1;
    this.schedule();
  }

  render() {
    const ctx = this.ctx;
    const g = this.geometry();
    ctx.clearRect(0, 0, g.width, g.height);
    ctx.fillStyle = '#151924';
    ctx.fillRect(0, 0, g.width, g.height);

    if (!this.nodes.length) {
      ctx.fillStyle = '#8A8D93';
      ctx.font = '500 12px Inter, system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('No measured handover in this window.', g.width / 2, g.height / 2 - 8);
      ctx.fillStyle = '#6B6F76';
      ctx.font = '500 11px "JetBrains Mono", ui-monospace, monospace';
      ctx.fillText('widen the timeframe, or wait for more events', g.width / 2, g.height / 2 + 12);
      this.hits = [];
      return;
    }

    const lens = LENSES[this.lens];
    const values = this.nodes.map((n) => lens.value(n) || 0);
    const maxValue = Math.max(1e-9, ...values);
    const focus = this.hover || this.selected;
    this.hits = [];

    // radius is needed by the collision pass, so size the nodes first
    this.nodes.forEach((node, index) => {
      node._r = Math.min(this.maxRadius(g), 13 + 15 * Math.sqrt((values[index] || 0) / maxValue));
    });
    this.separate(g);

    // edges first
    for (const edge of this.edges) {
      const a = this.nodePos(edge.a, g);
      const b = this.nodePos(edge.b, g);
      const weight = Math.sqrt(edge.hours / Math.max(1, ...this.edges.map((e) => e.hours)));
      let color = EDGE;
      if (focus) {
        if (edge.from === focus) color = rgba(TEAL, 0.85);
        else if (edge.to === focus) color = rgba(AMBER, 0.85);
        else color = 'rgba(120,130,150,.12)';
      }
      // stop the line at the rings so the arrowhead is never hidden by a node
      const ra = (edge.a._r || 14) + 4;
      const rb = (edge.b._r || 14) + 14;
      const ux = (b.x - a.x) / Math.max(1e-6, Math.hypot(b.x - a.x, b.y - a.y));
      const uy = (b.y - a.y) / Math.max(1e-6, Math.hypot(b.x - a.x, b.y - a.y));
      const x0 = a.x + ux * ra;
      const y0 = a.y + uy * ra;
      const x1 = b.x - ux * rb;
      const y1 = b.y - uy * rb;
      // a slight bow keeps reciprocal edges apart
      const mx = (x0 + x1) / 2 + (y1 - y0) * 0.08;
      const my = (y0 + y1) / 2 - (x1 - x0) * 0.08;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1 + weight * 6;
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.quadraticCurveTo(mx, my, x1, y1);
      ctx.stroke();
      // arrowhead at the target end, along the curve's tangent
      const angle = Math.atan2(y1 - my, x1 - mx);
      const size = 8 + weight * 6;
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(x1 + ux * 2, y1 + uy * 2);
      ctx.lineTo(x1 - size * Math.cos(angle - 0.42), y1 - size * Math.sin(angle - 0.42));
      ctx.lineTo(x1 - size * Math.cos(angle + 0.42), y1 - size * Math.sin(angle + 0.42));
      ctx.closePath();
      ctx.fill();
      this.hits.push({
        kind: 'edge', edge,
        x0: Math.min(x0, x1) - 8, x1: Math.max(x0, x1) + 8,
        y0: Math.min(y0, y1) - 8, y1: Math.max(y0, y1) + 8,
      });
    }

    // nodes
    this.nodes.forEach((node, index) => {
      const p = this.nodePos(node, g);
      const value = values[index] || 0;
      const radius = node._r;
      const roleColor = ROLE_COLOR[node.id] || (node.kind === 'person' ? '#B9BBC0' : '#8C98D4');
      const isFocus = focus === node.id;
      const dimmed = focus && !isFocus && !this.edges.some(
        (e) => (e.from === focus && e.to === node.id) || (e.to === focus && e.from === node.id),
      );

      ctx.beginPath();
      ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
      ctx.fillStyle = NODE_FILL;
      ctx.fill();
      ctx.lineWidth = isFocus ? 2.5 : 1.5;
      ctx.strokeStyle = dimmed ? 'rgba(255,255,255,.12)' : lensColor(value / maxValue);
      ctx.stroke();
      // role tick: a small arc in the category hue, so state never rides on
      // the lens colour alone
      ctx.beginPath();
      ctx.arc(p.x, p.y, radius + 4, -Math.PI * 0.9, -Math.PI * 0.1);
      ctx.strokeStyle = dimmed ? 'rgba(255,255,255,.06)' : roleColor;
      ctx.lineWidth = 2;
      ctx.stroke();

      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.font = '600 12px Inter, system-ui, sans-serif';
      ctx.fillStyle = dimmed ? '#6B6F76' : LABEL;
      ctx.fillText(node.id, p.x, p.y - 1);
      ctx.font = '500 10px "JetBrains Mono", ui-monospace, monospace';
      ctx.fillStyle = dimmed ? '#4E5258' : MUTED;
      ctx.fillText(`${lens.value(node).toFixed(lens.unit === 'h/d' ? 1 : 0)}${lens.unit}`, p.x, p.y + radius + 13);

      this.hits.push({ kind: 'node', node, x: p.x, y: p.y, r: radius + 6 });
    });

    this.drawOverlay(ctx, g, lens);
  }

  drawOverlay(ctx, g, lens) {
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.font = '600 11px Inter, system-ui, sans-serif';
    ctx.fillStyle = '#8A8D93';
    ctx.fillText('LEGEND', 18, g.height - 62);
    ctx.font = '500 10.5px "JetBrains Mono", ui-monospace, monospace';
    const lines = [
      [TEAL, 'waits on (outbound)'],
      [AMBER, 'waited on by (inbound)'],
      ['rgba(120,130,150,.9)', 'thickness ∝ measured wait'],
    ];
    lines.forEach(([color, text], i) => {
      const y = g.height - 46 + i * 14;
      ctx.fillStyle = color;
      ctx.fillRect(18, y - 7, 14, 2);
      ctx.fillStyle = '#B9BBC0';
      ctx.fillText(text, 38, y - 2);
    });
    // lens caption, top-left
    ctx.font = '600 10px Inter, system-ui, sans-serif';
    ctx.fillStyle = '#8A8D93';
    ctx.fillText(`LENS · ${lens.label.toUpperCase()}`, 18, 20);
    ctx.font = '500 11px "JetBrains Mono", ui-monospace, monospace';
    ctx.fillStyle = '#B9BBC0';
    ctx.fillText('ring colour = lens value · arc = role', 18, 34);
  }

  hitTest(x, y) {
    for (let i = this.hits.length - 1; i >= 0; i -= 1) {
      const hit = this.hits[i];
      if (hit.kind === 'node') {
        if (Math.hypot(x - hit.x, y - hit.y) <= hit.r) return hit;
      }
    }
    for (const hit of this.hits) {
      if (hit.kind === 'edge' && x >= hit.x0 && x <= hit.x1 && y >= hit.y0 && y <= hit.y1) return hit;
    }
    return null;
  }

  showHover(hit, cx, cy) {
    if (!hit) {
      this.hover = null;
      this.tip.hidden = true;
      this.schedule();
      return;
    }
    this.hover = hit.kind === 'node' ? hit.node.id : null;
    const lens = LENSES[this.lens];
    if (hit.kind === 'node') {
      const node = hit.node;
      const value = lens.value(node);
      const role = node.kind === 'person' ? node.role || 'person' : `${node.kind} queue`;
      this.tip.innerHTML = `
        <div class="row"><span class="act">${esc(node.id)}</span><span class="muted">${esc(role)}</span></div>
        <div class="row"><span class="k">${esc(lens.label)}</span>
          <span class="v" style="color:${lensColor(value / Math.max(1e-9, Math.max(...this.nodes.map((n) => lens.value(n) || 0))))}">${esc(lensText(lens, value))}</span></div>
        <div class="row"><span class="k">waited on</span><span class="v">${fmtHours(node.hours)} · ${node.count} waits</span></div>
        <div class="row"><span class="k">waits on</span><span class="v">${fmtHours(node.out_hours)}</span></div>
        <div class="row"><span class="k">wip now</span><span class="v">${node.wip_now}</span></div>
        ${node.people && node.people.length ? `<div class="row"><span class="k">people</span><span class="v">${esc(node.people.join(' '))}</span></div>` : ''}
        <div class="row"><span class="k">click</span><span class="v">open its queue</span></div>`;
    } else {
      const edge = hit.edge;
      this.tip.innerHTML = `
        <div class="row"><span class="act">${esc(edge.from)} → ${esc(edge.to)}</span></div>
        <div class="row"><span class="k">waited</span><span class="v">${fmtHours(edge.hours)}</span></div>
        <div class="row"><span class="k">waits</span><span class="v">${edge.count} · ${edge.prs} PRs</span></div>
        <div class="row"><span class="k">mean</span><span class="v">${fmtHours(edge.mean_hours)}</span></div>`;
    }
    this.tip.hidden = false;
    const stage = this.canvas.parentElement;
    const w = this.tip.offsetWidth || 320;
    const h = this.tip.offsetHeight || 120;
    this.tip.style.left = `${Math.min(Math.max(8, cx + 14), stage.clientWidth - w - 8)}px`;
    this.tip.style.top = `${Math.min(Math.max(8, cy + 14), stage.clientHeight - h - 8)}px`;
    this.schedule();
  }

  _recentTouch() {
    return this._touchAt > 0 && Date.now() - this._touchAt < 800;
  }

  _pinchState() {
    const points = [...this._pointers.values()];
    if (points.length < 2) return null;
    const [a, b] = points;
    return {
      dist: Math.hypot(a.x - b.x, a.y - b.y),
      x: (a.x + b.x) / 2,
      y: (a.y + b.y) / 2,
    };
  }

  _clampPan(g) {
    const limitX = g.plotW * 0.8 + 80;
    const limitY = g.plotH * 0.8 + 80;
    this.pan.x = Math.max(-limitX, Math.min(limitX, this.pan.x));
    this.pan.y = Math.max(-limitY, Math.min(limitY, this.pan.y));
  }

  _bind() {
    const canvas = this.canvas;
    canvas.addEventListener('mousemove', (ev) => {
      if (this._recentTouch()) return;
      const rect = canvas.getBoundingClientRect();
      this.showHover(this.hitTest(ev.clientX - rect.left, ev.clientY - rect.top), ev.clientX - rect.left, ev.clientY - rect.top);
    });
    canvas.addEventListener('mouseleave', () => {
      if (this._recentTouch()) return;
      this.hover = null;
      this.tip.hidden = true;
      this.schedule();
    });
    canvas.addEventListener('click', (ev) => {
      if (this._recentTouch()) return;
      const rect = canvas.getBoundingClientRect();
      const hit = this.hitTest(ev.clientX - rect.left, ev.clientY - rect.top);
      if (hit && hit.kind === 'node') {
        this.selected = this.selected === hit.node.id ? null : hit.node.id;
        this.schedule();
        if (this.onSelect) this.onSelect(hit.node);
      } else {
        this.selected = null;
        this.schedule();
      }
    });
    canvas.addEventListener('keydown', (ev) => {
      if (!this.nodes.length) return;
      const index = this.nodes.findIndex((n) => n.id === (this.hover || this.selected));
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') {
        const node = this.nodes[(index + 1 + this.nodes.length) % this.nodes.length];
        this.selected = node.id;
        if (this.onSelect) this.onSelect(node);
        this.schedule();
        ev.preventDefault();
      } else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') {
        const node = this.nodes[(index - 1 + this.nodes.length) % this.nodes.length];
        this.selected = node.id;
        if (this.onSelect) this.onSelect(node);
        this.schedule();
        ev.preventDefault();
      } else if (ev.key === 'Escape') {
        this.selected = null;
        this.schedule();
      }
    });
    // prd/28 §2: pan + tap on the blocking map. One finger moves the camera,
    // two fingers pinch, a tap selects a node (its queue opens), a hold shows
    // the same tooltip hover gives the desktop, and a double-tap resets.
    const local = (ev) => {
      const rect = canvas.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    };
    const clearHold = () => {
      clearTimeout(this._holdTimer);
      this._holdTimer = 0;
    };

    canvas.addEventListener('pointerdown', (ev) => {
      if (ev.pointerType === 'mouse') return;
      this._touchAt = Date.now();
      ev.preventDefault();
      try { canvas.setPointerCapture(ev.pointerId); } catch { /* not fatal */ }
      const point = local(ev);
      this._pointers.set(ev.pointerId, {
        ...point,
        startX: point.x, startY: point.y,
        lastX: point.x, lastY: point.y,
        moved: false, held: false, t: Date.now(),
      });
      if (this._pointers.size > 1) {
        clearHold();
        for (const pointer of this._pointers.values()) pointer.moved = true;
        this._pinch = this._pinchState();
        this.tip.hidden = true;
        return;
      }
      clearHold();
      this._holdTimer = setTimeout(() => {
        const pointer = this._pointers.get(ev.pointerId);
        if (!pointer || pointer.moved || this._pointers.size > 1) return;
        pointer.held = true;
        this.showHover(this.hitTest(pointer.x, pointer.y), pointer.x, pointer.y);
      }, 450);
    });

    canvas.addEventListener('pointermove', (ev) => {
      if (ev.pointerType === 'mouse') return;
      const pointer = this._pointers.get(ev.pointerId);
      if (!pointer) return;
      ev.preventDefault();
      const point = local(ev);
      pointer.x = point.x;
      pointer.y = point.y;
      if (this._pointers.size > 1) {
        const next = this._pinchState();
        if (this._pinch && next && next.dist > 0) {
          const g = this.geometry();
          const cx = g.width / 2;
          const cy = g.height / 2;
          const zoom = Math.min(3, Math.max(0.5, this.zoom * (next.dist / this._pinch.dist)));
          const applied = zoom / this.zoom;
          const mx = next.x - cx;
          const my = next.y - cy;
          this.pan.x = mx - (mx - this.pan.x) * applied;
          this.pan.y = my - (my - this.pan.y) * applied;
          this.zoom = zoom;
          this._clampPan(g);
          this.tip.hidden = true;
          this.schedule();
        }
        this._pinch = next;
        pointer.lastX = pointer.x;
        pointer.lastY = pointer.y;
        return;
      }
      if (!pointer.moved && Math.hypot(point.x - pointer.startX, point.y - pointer.startY) > 8) {
        pointer.moved = true;
        clearHold();
      }
      if (pointer.moved) {
        this.pan.x += point.x - pointer.lastX;
        this.pan.y += point.y - pointer.lastY;
        const g = this.geometry();
        this._clampPan(g);
        this.tip.hidden = true;
        this.schedule();
      }
      pointer.lastX = point.x;
      pointer.lastY = point.y;
    });

    const endPointer = (ev) => {
      if (ev.pointerType === 'mouse') return;
      const pointer = this._pointers.get(ev.pointerId);
      if (!pointer) return;
      clearHold();
      this._pointers.delete(ev.pointerId);
      if (this._pointers.size < 2) this._pinch = null;
      if (this._pointers.size === 1) {
        const rest = [...this._pointers.values()][0];
        rest.startX = rest.x;
        rest.startY = rest.y;
        rest.lastX = rest.x;
        rest.lastY = rest.y;
        rest.moved = true;
        return;
      }
      if (this._pointers.size > 0) return;
      if (pointer.moved || pointer.held) return;
      if (Date.now() - pointer.t > 600) return;
      const hit = this.hitTest(pointer.x, pointer.y);
      if (!hit || hit.kind !== 'node') {
        // a double-tap on empty space puts the camera back on the layout
        if (Date.now() - this._lastTap < 320) {
          this._lastTap = 0;
          this.resetView();
        } else {
          this._lastTap = Date.now();
          this.selected = null;
          this.schedule();
        }
        return;
      }
      this.selected = this.selected === hit.node.id ? null : hit.node.id;
      this.schedule();
      if (this.onSelect) this.onSelect(hit.node);
    };
    canvas.addEventListener('pointerup', endPointer);
    canvas.addEventListener('pointercancel', endPointer);

    new ResizeObserver(() => this.schedule()).observe(canvas.parentElement);
  }
}

/* megaspeed boot: fetch the pseudonymised log, wire the timeline, keep the
 * WebSocket harness alive (eval + screenshot), refresh on store changes.
 *
 * No framework, no build step. Everything rendered is a key, pseudonym,
 * timestamp or closed label — the API cannot return anything else.
 */

import { Timeline, ACTIVITY_ORDER, ACTIVITY_COLOR, fmtDay, fmtStamp } from '/timeline.js';

const $ = (id) => document.getElementById(id);

const state = {
  meta: null,
  events: [],
  objects: [],
  offline: false,
};

const timeline = new Timeline({
  canvas: $('timeline'),
  tip: $('tip'),
  onStats: (stats) => {
    $('range').textContent = `${fmtDay(stats.start)} → ${fmtDay(stats.end)}`;
    const bits = [];
    if (stats.aggregated) {
      bits.push(`aggregated into ${fmtBucket(stats.bucketMs)} buckets — zoom in for individual events`);
    }
    if (stats.openPrs) {
      bits.push(`${stats.openPrs} PRs still open are right-censored at the snapshot time`);
    }
    $('honesty').textContent = bits.join(' · ');
  },
});

// ------------------------------------------------------------------ chrome
function renderLegend() {
  const counts = new Map();
  for (const event of state.events) counts.set(event.activity, (counts.get(event.activity) || 0) + 1);
  const order = [...counts.keys()].sort((a, b) => {
    const ia = ACTIVITY_ORDER.indexOf(a);
    const ib = ACTIVITY_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  const legend = $('legend');
  legend.innerHTML = '';
  for (const activity of order) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.activity = activity;
    button.innerHTML = `<span class="swatch" style="background:${ACTIVITY_COLOR[activity] || '#8A8D93'}"></span>` +
      `<span>${activity}</span><span class="count">${counts.get(activity)}</span>`;
    if (timeline.hidden.has(activity)) button.classList.add('off');
    button.addEventListener('click', () => {
      timeline.toggleActivity(activity);
      button.classList.toggle('off');
    });
    legend.appendChild(button);
  }
}

function renderProvenance() {
  const meta = state.meta;
  if (!meta) return;
  const age = meta.as_of ? fmtAge(Date.parse(meta.as_of)) : 'unknown age';
  $('provenance').textContent =
    `Snapshot from ${age}: ${meta.events} events, ${meta.prs} PRs, ${meta.commits} commits · ` +
    `${meta.source || 'unknown source'}, ${meta.prs_dropped} PRs without a ticket key dropped`;
}

function setStatus(text, cls = '') {
  const el = $('status');
  el.textContent = text;
  el.className = `status ${cls}`.trim();
}

function fmtBucket(ms) {
  if (ms < 3600_000) return `${Math.max(1, Math.round(ms / 60_000))} min`;
  if (ms < 86_400_000) return `${Math.round(ms / 3600_000)} h`;
  return `${Math.round(ms / 86_400_000)} d`;
}

function fmtAge(then) {
  const delta = Math.max(0, Date.now() - then);
  if (delta < 60_000) return 'just now';
  if (delta < 3600_000) return `${Math.round(delta / 60_000)} min ago`;
  if (delta < 86_400_000) return `${Math.round(delta / 3600_000)} h ago`;
  return `${Math.round(delta / 86_400_000)} d ago`;
}

// -------------------------------------------------------------------- data
async function load() {
  try {
    const [health, events, objects] = await Promise.all([
      fetch('/api/health', { cache: 'no-store' }).then((r) => r.json()),
      fetch('/api/events', { cache: 'no-store' }).then((r) => r.json()),
      fetch('/api/objects', { cache: 'no-store' }).then((r) => r.json()),
    ]);
    if (!health.ok) {
      $('provenance').textContent = health.error || 'store unavailable';
      $('empty').hidden = false;
      $('empty').querySelector('.muted').textContent =
        health.hint || 'Run python3 -m ingest --fixture --n 50 and reload.';
      setStatus('no store', 'stale');
      return;
    }
    state.meta = health;
    state.events = Array.isArray(events) ? events : [];
    state.objects = Array.isArray(objects) ? objects : [];
    timeline.setData(state.events, state.objects, health);
    renderLegend();
    renderProvenance();
    $('empty').hidden = state.events.length > 0;
    setStatus(`live · ${state.events.length} events`, 'live');
    window.__megaspeedReady = true;
  } catch (err) {
    setStatus('offline', 'stale');
    $('provenance').textContent = `could not reach the API: ${err.message}`;
  }
}

// ------------------------------------------------------------------ harness
let socket = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${proto}://${location.host}/ws`);

  socket.onopen = () => {
    setStatus('live · ' + state.events.length + ' events', 'live');
  };

  socket.onclose = () => {
    setStatus('reconnecting', 'stale');
    setTimeout(connect, 1500);
  };

  socket.onerror = () => {
    try { socket.close(); } catch { /* ignore */ }
  };

  socket.onmessage = (message) => {
    let payload;
    try {
      payload = JSON.parse(message.data);
    } catch {
      return;
    }
    if (payload.event === 'changed') {
      clearTimeout(connect._refresh);
      connect._refresh = setTimeout(load, 250);
    } else if (payload.eval != null) {
      runEval(payload.eval, payload.code);
    }
  };
}

setInterval(() => {
  if (socket && socket.readyState === 1) socket.send(JSON.stringify({ ping: Date.now() }));
}, 25_000);

async function runEval(id, code) {
  let result;
  try {
    let value;
    try {
      // Indirect eval returns the completion value of the last expression, like
      // a console: `1 + 1`, `document.title`, `window.__megaspeed.state`.
      value = (0, eval)(code);
    } catch (err) {
      if (!(err instanceof SyntaxError)) throw err;
      // Top-level `await` is not valid in eval; retry as an async function body
      // (where a value needs an explicit `return`).
      const AsyncFunction = Object.getPrototypeOf(async function noop() {}).constructor;
      value = new AsyncFunction(code)();
    }
    if (value && typeof value.then === 'function') value = await value;
    result = { ok: true, data: value === undefined ? null : value };
  } catch (err) {
    result = { ok: false, error: String((err && err.message) || err) };
  }
  const text = JSON.stringify({ evalres: id, ...result });
  if (text.length <= 150_000) {
    socket.send(text);
    return;
  }
  const size = 100_000;
  const count = Math.ceil(text.length / size);
  for (let i = 0; i < count; i += 1) {
    socket.send(JSON.stringify({
      evalchunk: { id, i, n: count, data: text.slice(i * size, (i + 1) * size) },
    }));
  }
}

/* Screenshot bridge.
 *
 * Default path renders the real DOM: clone the element, swap the <canvas> for
 * an <img> of its bitmap, inline the stylesheet, and paint the whole thing
 * through an SVG foreignObject. Chromium handles this natively, so there is no
 * html2canvas dependency and no build step.
 *
 * Fallback (if the DOM render throws or yields nothing): composite the canvas
 * bitmap into the element's box. The canvas is the app's dense content, so the
 * fallback still shows the data.
 */
window.__megaspeedShot = async function megaspeedShot(selector = '.app', options = {}) {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`selector not found: ${selector}`);
  const scale = Math.min(2, Number(options.scale) || window.devicePixelRatio || 1);
  const rect = element.getBoundingClientRect();

  const out = document.createElement('canvas');
  out.width = Math.max(1, Math.round(rect.width * scale));
  out.height = Math.max(1, Math.round(rect.height * scale));
  const ctx = out.getContext('2d');
  ctx.fillStyle = getComputedStyle(document.body).backgroundColor || '#191B1C';
  ctx.fillRect(0, 0, out.width, out.height);

  try {
    const image = await domToImage(element, rect);
    ctx.drawImage(image, 0, 0, out.width, out.height);
    return out.toDataURL('image/png');
  } catch (err) {
    const canvas = element.tagName === 'CANVAS' ? element : element.querySelector('canvas');
    if (!canvas) throw err;
    const canvasRect = canvas.getBoundingClientRect();
    ctx.drawImage(
      canvas,
      (canvasRect.left - rect.left) * scale,
      (canvasRect.top - rect.top) * scale,
      canvasRect.width * scale,
      canvasRect.height * scale,
    );
    return out.toDataURL('image/png');
  }
};

async function domToImage(element, rect) {
  const clone = element.cloneNode(true);
  // backdrop-filter does not survive SVG foreignObject rounding and smears the
  // canvas around the tooltip; flatten it in the clone only.
  for (const node of clone.querySelectorAll('*')) {
    node.style.backdropFilter = 'none';
    node.style.webkitBackdropFilter = 'none';
  }
  const sourceCanvas = element.querySelector('canvas');
  const cloneCanvas = clone.querySelector('canvas');
  if (sourceCanvas && cloneCanvas) {
    const img = document.createElement('img');
    img.src = sourceCanvas.toDataURL('image/png');
    img.style.width = `${sourceCanvas.clientWidth}px`;
    img.style.height = `${sourceCanvas.clientHeight}px`;
    img.style.display = 'block';
    cloneCanvas.replaceWith(img);
  }
  clone.style.width = `${rect.width}px`;
  clone.style.height = `${rect.height}px`;
  clone.style.margin = '0';

  const css = await fetch('/style.css', { cache: 'force-cache' }).then((r) => r.text());
  const wrapper = document.createElement('div');
  wrapper.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
  wrapper.setAttribute(
    'style',
    `width:${rect.width}px;height:${rect.height}px;background:#191B1C;` +
    'font-family:Inter,"Segoe UI",Roboto,sans-serif;font-size:13px;color:#E8E9EB;',
  );
  const style = document.createElement('style');
  style.textContent = css;
  wrapper.appendChild(style);
  wrapper.appendChild(clone);

  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${rect.width}" height="${rect.height}">` +
    `<foreignObject x="0" y="0" width="100%" height="100%">` +
    new XMLSerializer().serializeToString(wrapper) +
    '</foreignObject></svg>';

  const image = new Image();
  image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  await image.decode();
  return image;
}

// Exposed so the eval bridge can drive the UI: __megaspeed.timeline.zoom(0.5).
window.__megaspeed = {
  timeline,
  get state() { return state; },
  refresh: load,
  setLanes: (dim) => { $('lanes').value = dim; timeline.setLanes(dim); },
  setDensity: (d) => {
    for (const button of $('density').querySelectorAll('button')) {
      button.classList.toggle('on', button.dataset.d === d);
    }
    timeline.setDensity(d);
  },
  stamp: fmtStamp,
};

// ------------------------------------------------------------------ controls
$('zoom-in').addEventListener('click', () => timeline.zoom(0.6));
$('zoom-out').addEventListener('click', () => timeline.zoom(1 / 0.6));
$('fit').addEventListener('click', () => timeline.fit());
$('lanes').addEventListener('change', (ev) => timeline.setLanes(ev.target.value));
$('density').addEventListener('click', (ev) => {
  const button = ev.target.closest('button');
  if (!button) return;
  window.__megaspeed.setDensity(button.dataset.d);
});

load().then(connect);

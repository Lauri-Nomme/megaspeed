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
    const fn = new Function(`return (async () => {\n${code}\n})()`);
    const value = await fn();
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

/* Screenshot bridge. Captures the canvas render (the dense view) inside the
 * requested element's box. There is no html2canvas dependency on purpose:
 * the canvas *is* the app's content, and a build-free bundle is the point. */
window.__megaspeedShot = function megaspeedShot(selector = '.app') {
  const element = document.querySelector(selector);
  if (!element) throw new Error(`selector not found: ${selector}`);
  const canvas = element.tagName === 'CANVAS' ? element : element.querySelector('canvas');
  if (!canvas) throw new Error(`no canvas inside ${selector}`);
  const rect = element.getBoundingClientRect();
  const canvasRect = canvas.getBoundingClientRect();
  const scale = Math.min(2, window.devicePixelRatio || 1);
  const out = document.createElement('canvas');
  out.width = Math.max(1, Math.round(rect.width * scale));
  out.height = Math.max(1, Math.round(rect.height * scale));
  const ctx = out.getContext('2d');
  ctx.fillStyle = getComputedStyle(document.body).backgroundColor || '#191B1C';
  ctx.fillRect(0, 0, out.width, out.height);
  ctx.drawImage(
    canvas,
    (canvasRect.left - rect.left) * scale,
    (canvasRect.top - rect.top) * scale,
    canvasRect.width * scale,
    canvasRect.height * scale,
  );
  return out.toDataURL('image/png');
};

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

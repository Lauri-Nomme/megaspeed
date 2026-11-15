/* megaspeed boot: fetch the pseudonymised log, route the views, keep the
 * WebSocket harness alive (eval + screenshot), refresh on store changes.
 *
 * No framework, no build step. Everything rendered is a key, pseudonym,
 * timestamp or closed label — the API cannot return anything else.
 *
 * Views and their one question (prd/10 §2):
 *   overview   What needs attention?     delivery list (triage)
 *   delivery   What needs attention?     triage table + detail panel
 *   timeline   Where does the time go?   stream explorer (canvas)
 *   blocking   Who is blocking whom?     weighted handover graph (canvas)
 *   queue      Who is slow vs overloaded? WIP per person (canvas)
 *   policy     What should we change first? (stub, next slice)
 */

import { Timeline, ACTIVITY_ORDER, ACTIVITY_COLOR, PHASE, PHASE_ORDER } from '/timeline.js';
import { BlockingMap, LENSES } from '/graph.js';
import { QueueChart } from '/queues.js';
import {
  DAY, fmtAge, fmtBucket, fmtDay, fmtHours, fmtRange, fmtStamp, esc, whoLabel,
} from '/format.js';
import {
  TABS, tabCounts, tabRows, deliveryTable, overviewMarkup, detailMarkup,
  impactSentence, entityMarkdown, deliveryMarkdown,
} from '/tables.js';

const $ = (id) => document.getElementById(id);
const LS_KEY = 'megaspeed.ui.v1';

const PAGES = {
  overview: { title: 'Overview', question: 'What needs attention?' },
  delivery: { title: 'Delivery list', question: 'What needs attention?' },
  timeline: { title: 'Timeline', question: 'Where does the time go?' },
  blocking: { title: 'Blocking map', question: 'Who is blocking whom?' },
  queue: { title: 'Queue', question: 'Who is overloaded, and who is merely slow?' },
  policy: { title: 'Policy lab', question: 'What should we change first?' },
  sources: { title: 'Sources', question: 'What is this screen made of?' },
};

const TIMEFRAMES = {
  '1w': { days: 7, label: 'the last week' },
  '4w': { days: 28, label: 'the last four weeks' },
  '1q': { days: 90, label: 'the last quarter' },
  all: { days: null, label: 'the full window' },
};

const state = {
  meta: null,
  stamp: '',
  events: [],
  objects: [],
  counts: null,
  eventCapped: false,
  objectCapped: false,
  page: 'timeline',
  scope: { kind: 'all', value: null, label: 'everything' },
  timeframe: '1q',
  tab: 'blocked',
  delivery: null,
  queues: null,
  blocking: null,
  blockingLevel: 'queue',
  queueLevel: 'person',
  lens: 'hours',
  cache: new Map(),
  selection: null,
  drawerEntity: null,
  emptyReason: '',
};

const timeline = new Timeline({
  canvas: $('timeline'),
  tip: $('tip'),
  onStats: (stats) => {
    $('range').textContent = fmtRange(stats.start, stats.end);
    const bits = [];
    if (stats.aggregated) bits.push(`aggregated into ${fmtBucket(stats.bucketMs)} buckets — zoom in for individual events`);
    if (stats.openPrs) bits.push(`${stats.openPrs} PRs still open are right-censored at the snapshot time`);
    bits.push(`${stats.visible} of ${stats.total} events in view`);
    if (state.eventCapped && state.counts && state.counts.total > state.events.length) {
      bits.push(`only the first ${state.events.length} of ${state.counts.total} events are loaded`);
    }
    $('honesty').textContent = bits.join(' · ');
    $('empty').hidden = !(stats.allHidden || !!state.emptyReason);
    if (stats.allHidden) {
      $('empty').querySelector('p').textContent = 'Every activity is hidden — click a legend key to show it again.';
    }
  },
  onSelect: (pr) => openPrDrawer(pr),
  onSelection: (range) => renderSelection(range),
});

const blockingMap = new BlockingMap({
  canvas: $('blocking'),
  tip: $('blocking-tip'),
  onSelect: (node) => {
    state.selection = { node: node.id };
    renderBlockingStats();
    // drill-down: a node is a doorway to its queue time series
    openNodeQueue(node);
  },
});

const queueChart = new QueueChart({
  canvas: $('queues'),
  tip: $('queue-tip'),
  onSelect: (stream) => {
    if (stream.role === 'synthetic') return;
    setScopeFromQueue(stream);
  },
});

// ------------------------------------------------------------------ helpers
function ageOf(iso) {
  return iso ? fmtAge(Date.parse(iso)) : 'unknown age';
}

function cached(key) {
  return state.cache.get(key);
}

async function fetchJSON(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  let body = null;
  try { body = await response.json(); } catch { body = null; }
  return { response, body };
}

class HttpError extends Error {
  constructor(status, body, url) {
    super(`${url} → ${status}`);
    this.status = status;
    this.body = body || {};
    this.url = url;
  }
}

async function getJSON(url) {
  const { response, body } = await fetchJSON(url);
  if (!response.ok) throw new HttpError(response.status, body, url);
  if (body === null) throw new Error(`${url} → invalid JSON`);
  return body;
}

function timeframeDays() {
  return TIMEFRAMES[state.timeframe].days;
}

function timeframeQuery() {
  const days = timeframeDays();
  return days ? `days=${days}` : '';
}

/** Keep the sticky table header / detail panel below the (wrapping) meta line. */
function syncChromeHeight() {
  const meta = $('meta');
  const bottom = 40 + meta.offsetHeight + 2;
  document.documentElement.style.setProperty('--meta-bottom', `${bottom}px`);
}

function setStatus(text, cls = '') {
  $('status').textContent = text;
  $('status').className = `status ${cls}`.trim();
}

// -------------------------------------------------------------------- views
function renderScopeBanner() {
  const banner = $('scopebanner');
  const scope = state.scope;
  if (scope.kind === 'all') {
    banner.hidden = true;
    banner.textContent = '';
    return;
  }
  banner.hidden = false;
  if (scope.kind === 'person') {
    banner.textContent = `Scope ${scope.value}: timeline events and triage rows are filtered to this person; ` +
      'the blocking map, queue series and policy lab still show everything. Waits are measured over the whole log.';
  } else {
    banner.textContent = `Scope the ${scope.value} queue: the delivery list and queue view are filtered to it; ` +
      'the timeline and blocking map still show everything, so the wait that lands in this queue stays visible.';
  }
}

function renderPage() {
  const page = state.page;
  for (const section of document.querySelectorAll('.page')) {
    section.hidden = section.id !== `page-${page}`;
  }
  for (const item of document.querySelectorAll('.nav-item')) {
    const active = item.dataset.page === page;
    item.classList.toggle('on', active);
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  }
  $('page-title').textContent = PAGES[page].title;
  $('page-question').textContent = PAGES[page].question;
  renderScopeBanner();

  if (page === 'timeline') renderTimelinePage();
  if (page === 'delivery') renderDeliveryPage();
  if (page === 'blocking') renderBlockingPage();
  if (page === 'queue') renderQueuePage();
  if (page === 'overview') renderOverviewPage();
  if (page === 'sources') renderSourcesPage();
  saveUI();
}

async function showPage(page) {
  if (!PAGES[page]) return;
  state.page = page;
  renderPage();
  try {
    await ensureData(page);
  } catch (err) {
    setStatus(`view failed: ${err.message}`, 'stale');
    return;
  }
  renderPage();
}

async function ensureData(page) {
  const q = timeframeQuery();
  const stamp = state.meta ? state.meta.as_of : 'none';
  if (page === 'delivery' || page === 'overview') {
    const key = `delivery:${state.timeframe}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(`/api/delivery?${q}`));
    state.delivery = cached(key);
  }
  if (page === 'blocking' || page === 'overview') {
    const key = `blocking:${state.timeframe}:${state.blockingLevel}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(`/api/blocking?level=${state.blockingLevel}&${q}`));
    state.blocking = cached(key);
  }
  if (page === 'queue' || page === 'overview') {
    const key = `queues:${state.timeframe}:${state.queueLevel}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(`/api/queues?level=${state.queueLevel}&${q}`));
    state.queues = cached(key);
  }
}

// ----------------------------------------------------------------- timeline
function renderSelection(range) {
  const chip = $('selection');
  if (!range) {
    chip.hidden = true;
    return;
  }
  chip.hidden = false;
  $('selection-range').textContent =
    `${fmtRange(range.start, range.end)} · ${fmtHours((range.end - range.start) / 3_600_000)}`;
  chip.dataset.start = String(range.start);
  chip.dataset.end = String(range.end);
}

function renderTimelinePage() {
  renderLegend();
  renderPhaseLegend();
  timeline.schedule();
}

function renderLegend() {
  const counts = timeline.activityCounts();
  const order = [...counts.keys()].sort((a, b) => {
    const ia = ACTIVITY_ORDER.indexOf(a);
    const ib = ACTIVITY_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
  $('legend').innerHTML = order.map((activity) => {
    const activityState = timeline.stateOf(activity);
    const pressed = activityState === 'on' ? 'true' : activityState === 'faint' ? 'mixed' : 'false';
    return `<button type="button" data-activity="${esc(activity)}" class="${activityState}"
        aria-pressed="${pressed}" title="click: on → faint → hide. Filtering never deletes data.">
      <span class="swatch" style="background:${ACTIVITY_COLOR[activity] || '#8A8D93'}"></span>
      <span>${esc(activity)}</span><span class="count">${counts.get(activity)}</span></button>`;
  }).join('');
}

function renderPhaseLegend() {
  const stats = timeline.phaseStats();
  $('phase-legend').innerHTML = PHASE_ORDER.map((phase) => {
    const entry = stats.get(phase);
    const zero = !entry || entry.hours < 0.05;
    const swatch = phase === 'unobserved'
      ? `<i class="hatch" style="--c:${PHASE[phase].color}"></i>`
      : `<i style="background:${PHASE[phase].color}"></i>`;
    // "closed" is an end cap, not usually a duration; say which it is
    const short = (hours) => (hours >= 48 ? `${Math.round(hours / 24)} d` : `${Math.max(1, Math.round(hours))} h`);
    const value = phase === 'closed'
      ? (zero ? 'end cap' : short(entry.hours))
      : (zero ? '0' : short(entry.hours));
    const title = `${PHASE[phase].hint}${entry ? ` — ${fmtHours(entry.hours)} measured in this window` : ''}`;
    return `<span class="${zero ? 'zero' : ''}" title="${esc(title)}">
      ${swatch}${esc(PHASE[phase].label)}
      <span class="mono muted">${value}</span></span>`;
  }).join('');
}

// ----------------------------------------------------------------- delivery
function renderDeliveryPage() {
  const data = state.delivery;
  const counts = scopedTabCounts(data);
  if (data && state.tab !== 'all') {
    // the actionable subset is the default, but never an empty screen
    const available = state.tab === 'blocked' ? counts.blocked : counts.oldest;
    if (!available && counts.all) state.tab = counts.oldest ? 'oldest' : 'all';
  }
  $('delivery-tabs').innerHTML = TABS.map((tab) => `
    <button type="button" role="tab" data-tab="${tab.id}"
      aria-selected="${state.tab === tab.id}" class="${state.tab === tab.id ? 'on' : ''}">
      ${esc(tab.label)} <span class="num">${counts[tab.id]}</span></button>`).join('');
  $('delivery-table').innerHTML = data
    ? deliveryTable(data, state.tab, { scope: deliveryScope() })
    : '<div class="empty-row">Loading the triage list…</div>';
  $('delivery-assumption').textContent = data
    ? `${data.assumption} · snapshot ${fmtStamp(Date.parse(data.snapshot))} · ` +
      `${data.counts.open} open items, ${data.counts.waiting_over_week} of them waiting over a week. ` +
      `${data.impact_note}.`
    : '';
  renderDeliveryDetail();
}

/** Counts follow the scope, so the tab number always matches the rows below it. */
function scopedTabCounts(data) {
  if (!data) return { blocked: 0, oldest: 0, all: 0 };
  const rows = data.rows.filter((row) => matchesScopeRow(row));
  return {
    blocked: rows.filter((row) => row.blocked).length,
    oldest: rows.filter((row) => !row.blocked && row.waiting_hours >= 72).length,
    all: rows.length,
  };
}

function matchesScopeRow(row) {
  const scope = deliveryScope();
  if (!scope) return true;
  if (scope.kind === 'person') {
    return row.author === scope.value || row.waiting_for === scope.value || row.ball === scope.value;
  }
  if (scope.kind === 'role') {
    if (scope.value === 'author') return row.author === row.waiting_for;
    if (scope.value === 'review') return row.phase === 'review' && row.author !== row.waiting_for;
  }
  return true;
}

function deliveryScope() {
  if (state.scope.kind === 'all') return null;
  return { kind: state.scope.kind, value: state.scope.value, label: state.scope.label };
}

function renderDeliveryDetail() {
  const container = $('delivery-detail');
  const row = state.selection && state.selection.row;
  if (!row) {
    container.innerHTML = '<p class="empty-detail">Select a row to see its impact, facts and pivot chips.</p>';
    return;
  }
  container.innerHTML = detailMarkup({
    overline: 'PULL REQUEST',
    row,
    impact: impactSentence(row),
    facts: [
      ['ticket', row.tickets.join(' ')],
      ['waiting on', whoLabel(row.ball_label || row.ball)],
      ['waiting since', `${fmtStamp(Date.parse(row.waiting_since))} (${fmtHours(row.waiting_hours)})`],
      ['phase', row.phase],
      ['age', fmtHours(row.age_hours)],
      ['commits in log', row.commits],
      ['dependents', row.dependents ? `${row.dependents} PR(s) on the same ticket` : 'none'],
      ['impact', row.dependents ? `≈${fmtHours(row.impact_hours)}` : '—'],
    ],
    children: [...row.tickets, row.id],
  });
}

// ----------------------------------------------------------------- blocking
function buildLensButtons() {
  $('blocking-lens').innerHTML = Object.entries(LENSES).map(([id, lens]) => `
    <button type="button" data-lens="${id}" aria-pressed="${state.lens === id}"
      title="${esc(lens.hint)}" class="${state.lens === id ? 'on' : ''}">${esc(lens.label)}</button>`).join('');
  $('blocking-lens').hidden = false;
}

function renderBlockingPage() {
  const data = state.blocking;
  blockingMap.setData(data || { nodes: [], edges: [] });
  blockingMap.setLens(state.lens);
  $('blocking-assumption').textContent = data
    ? `${data.assumption} · ${data.window.days} days in view · edge = “X waited on Y” · ${data.attribution}. Direction colour is relative to the selected node.`
    : '';
  renderBlockingStats();
}

function renderBlockingStats() {
  const data = state.blocking;
  const strip = $('blocking-stats');
  if (!data) {
    strip.innerHTML = '';
    return;
  }
  const totals = data.totals || {};
  const selected = blockingMap.selected;
  const node = selected ? data.nodes.find((n) => n.id === selected) : null;
  strip.innerHTML = node
    ? `<span class="stat-inline"><b class="mono">${esc(node.id)}</b></span>
       <span class="stat-inline">waited on <b>${fmtHours(node.hours)}</b></span>
       <span class="stat-inline">waits on <b>${fmtHours(node.out_hours)}</b></span>
       <span class="stat-inline">WIP <b>${node.wip_now}</b></span>`
    : `<span class="stat-inline"><b>${totals.edges || 0}</b> edges</span>
       <span class="stat-inline"><b>${fmtHours(totals.hours || 0)}</b> measured wait</span>
       <span class="stat-inline">mean <b>${fmtHours(totals.mean_wait_hours || 0)}</b></span>
       <span class="stat-inline"><b>${totals.nodes || 0}</b> nodes</span>`;
}

async function openNodeQueue(node) {
  if (node.kind === 'role' || node.kind === 'person') {
    if (node.id === 'review' || node.id === 'sentinel' || node.id === 'author') {
      setScope('role', node.id, { silent: true });
    } else {
      setScope('person', node.id, { silent: true });
    }
  }
  await showPage('queue');
}

// -------------------------------------------------------------------- queue
function renderQueuePage() {
  const data = state.queues;
  const streams = data ? data.streams.filter((stream) => queueMatchesScope(stream)) : [];
  queueChart.setData(data ? { ...data, streams } : { streams: [], buckets: 1, window: { start: '', end: '' } });
  $('queue-assumption').textContent = data
    ? `${data.assumption} · ${data.bucket_hours} h buckets over ${data.window.days} days. ` +
      `Little's law: WIP ≈ throughput × wait. Overloaded = long wait and a standing queue; slow = long wait with little WIP. ` +
      `${data.diagnosis_note}.`
    : '';
}

function queueMatchesScope(stream) {
  if (state.scope.kind === 'all') return true;
  if (state.scope.kind === 'person') return stream.id === state.scope.value;
  if (state.scope.kind === 'role') {
    if (state.scope.value === 'sentinel') return stream.id === 'sentinel';
    if (state.scope.value === 'review') return stream.id === 'review';
    if (state.scope.value === 'author') return stream.id === 'author';
  }
  return true;
}

// ----------------------------------------------------------------- overview
function renderOverviewPage() {
  $('overview').innerHTML = overviewMarkup({
    delivery: state.delivery,
    queues: state.queues,
    blocking: state.blocking,
  });
}

// ------------------------------------------------------------------ sources
/** The store the server is actually reading, never a path baked into the UI. */
function storeLabel(meta) {
  const raw = String(meta.store || '').replace(/\\/g, '/').replace(/\/+$/, '');
  if (!raw) return '— (the API did not report one)';
  const parts = raw.split('/').filter(Boolean);
  // Show the repo-relative tail (`data/<store>.duckdb`) even when the API
  // reports an absolute path, so the label is short and can never drift.
  const at = parts.lastIndexOf('data');
  const rel = at >= 0 ? parts.slice(at).join('/') : parts.slice(-2).join('/');
  return `${rel} (local, read-only)`;
}

function renderSourcesPage() {
  const meta = state.meta || {};
  const rows = [
    ['store', storeLabel(meta)],
    ['source', meta.source || '—'],
    ['snapshot', meta.as_of ? `${fmtStamp(Date.parse(meta.as_of))} (${ageOf(meta.as_of)})` : '—'],
    ['events', String(meta.events ?? '—')],
    ['PRs', `${meta.prs ?? '—'} kept of ${meta.prs_seen ?? '—'} seen`],
    ['commits', String(meta.commits ?? '—')],
    ['dropped', `${meta.prs_dropped ?? '—'} PRs had no ticket key`],
    ['window', `${meta.first_ts ? fmtDay(Date.parse(meta.first_ts)) : '—'} → ${meta.last_ts ? fmtDay(Date.parse(meta.last_ts)) : '—'}`],
    ['duration basis', 'wall-clock hours; the working-hours calendar is a later slice'],
  ];
  $('sources-body').innerHTML = `<dl class="facts">${rows.map(([key, value]) =>
    `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>
    <p class="panel-note">The store holds ticket keys, PR ids, commit hashes, actor pseudonyms,
      timestamps and closed attribute labels only. No title, description, comment, message or diff
      is stored, so no view can render one.</p>`;
}

// --------------------------------------------------------------- selection
function findDeliveryRow(objectId) {
  return state.delivery && state.delivery.rows
    ? state.delivery.rows.find((row) => row.object_id === objectId)
    : null;
}

function selectRow(objectId) {
  const row = findDeliveryRow(objectId);
  state.selection = row ? { row } : null;
  renderDeliveryDetail();
  for (const tr of document.querySelectorAll('#delivery-table tr.row')) {
    tr.classList.toggle('selected', tr.dataset.id === objectId);
  }
}

function openPrDrawer(pr) {
  const segments = timeline.lifecycle(pr);
  const totals = timeline.phases(pr);
  const cycle = segments.length ? segments[segments.length - 1].to - segments[0].from : 0;
  const facts = [
    ['tickets', pr.keys.join(' ') || '—'],
    ['author', pr.actor],
    ['state', pr.state + (pr.closed ? '' : ' · right-censored')],
    ['cycled', `${fmtHours(cycle / 3_600_000)} (measured)`],
    ['commits', String(pr.commits.length)],
    ...["waiting", "active", "review", "ci", "rebasing", "unobserved"].map((phase) => [
      phase === 'unobserved' ? 'no events since' : PHASE[phase].label,
      totals.get(phase) ? fmtHours(totals.get(phase) / 3_600_000) : '—',
    ]),
  ];
  openDrawer({
    overline: 'PULL REQUEST',
    pr,
    facts,
    children: [...pr.keys, `${pr.repo}#${pr.number}`],
    impact: pr.closed ? 'Closed: its wait is history, not a live bottleneck.' : undefined,
  });
}

function openDrawer(entity) {
  state.drawerEntity = entity;
  const drawer = $('drawer');
  drawer.innerHTML = `<button type="button" class="drawer-close" data-drawer="close" aria-label="close">×</button>` +
    detailMarkup(entity);
  drawer.hidden = false;
}

function closeDrawer() {
  state.drawerEntity = null;
  $('drawer').hidden = true;
}

// --------------------------------------------------------------- pivot menu
function openMenu(x, y, entity, label) {
  const menu = $('menu');
  state.menuEntity = entity;
  const author = entity && (entity.author || (entity.pr && entity.pr.actor) || (entity.row && entity.row.author));
  const items = [
    author ? ['person', `Scope to ${author}`] : null,
    ['timeline', 'Open in timeline'],
    ['copy', 'Copy as Markdown'],
    ['sep', ''],
    ['clear', 'Clear scope · whole team'],
  ].filter(Boolean);
  menu.innerHTML = `<div class="menu-head">${esc(label || 'pivot')}</div>` + items.map(([action, text]) =>
    action === 'sep' ? '<div class="sep"></div>'
      : `<button type="button" data-menu="${action}">${esc(text)}</button>`).join('');
  menu.hidden = false;
  const width = menu.offsetWidth || 232;
  const height = menu.offsetHeight || 140;
  menu.style.left = `${Math.min(x, window.innerWidth - width - 8)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - height - 8)}px`;
  const first = menu.querySelector('button');
  if (first) first.focus();
}

function closeMenu() {
  $('menu').hidden = true;
  state.menuEntity = null;
}

function entityForTarget(target, point) {
  const row = target.closest && target.closest('tr.row[data-id]');
  if (row) {
    const record = findDeliveryRow(row.dataset.id);
    if (record) {
      return {
        entity: { row: record, author: record.author },
        label: `${record.tickets.join(' ') || '—'} #${record.number} ${record.repo_short}`,
      };
    }
  }
  if (target.id === 'timeline' && point) {
    const rect = target.getBoundingClientRect();
    const hit = timeline.hitTest(point.x - rect.left, point.y - rect.top);
    if (hit && (hit.kind === 'bar' || hit.kind === 'interval') && hit.pr) {
      return {
        entity: { pr: hit.pr, author: hit.pr.actor, row: findDeliveryRow(hit.pr.id) },
        label: `${hit.pr.keys[0] ? `${hit.pr.keys[0]} ` : ''}#${hit.pr.number} ${hit.pr.repo}`,
      };
    }
  }
  return null;
}

// ------------------------------------------------------------------- scope
function buildScopeOptions() {
  const counts = new Map();
  for (const event of state.events) counts.set(event.actor, (counts.get(event.actor) || 0) + 1);
  const options = [['all:all', 'everything']];
  [...counts.entries()].sort((a, b) => b[1] - a[1])
    .forEach(([actor]) => options.push([`person:${actor}`, `person ${actor}`]));
  options.push(['role:review', 'queue review'], ['role:sentinel', 'queue sentinel'], ['role:author', 'queue author']);
  $('scope').innerHTML = options.map(([value, label]) =>
    `<option value="${esc(value)}">${esc(label)}</option>`).join('');
  $('scope').value = `${state.scope.kind}:${state.scope.value || 'all'}`;
}

function setScope(kind, value, { silent = false } = {}) {
  state.scope = {
    kind,
    value: kind === 'all' ? null : value,
    label: kind === 'all' ? 'everything' : String(value),
  };
  buildScopeOptions();
  applyScopeToTimeline();
  renderPage();
  if (!silent) setStatus(kind === 'all' ? 'scope cleared' : `scoped to ${state.scope.label}`, 'live');
}

function setScopeFromQueue(stream) {
  if (stream.role === 'synthetic') return;
  if (stream.role === 'sentinel') setScope('role', 'sentinel');
  else setScope('person', stream.id);
}

function scopedEvents() {
  if (state.scope.kind !== 'person') return state.events;
  return state.events.filter((event) => event.actor === state.scope.value);
}

function scopedObjects(events) {
  if (state.scope.kind === 'all') return state.objects;
  if (state.scope.kind !== 'person') return state.objects;
  const person = state.scope.value;
  const ids = new Set(events.map((event) => `PR:${event.repo}#${event.pr_id}`));
  return state.objects.filter((obj) => {
    if (obj.object_type === 'PR') return ids.has(obj.object_id) || obj.actor === person;
    if (obj.object_type === 'COMMIT') return ids.has(`PR:${obj.repo}#${obj.pr_id}`);
    return true;
  });
}

function applyScopeToTimeline() {
  const events = scopedEvents();
  timeline.setData(events, scopedObjects(events), state.meta || {});
}

// --------------------------------------------------------------- timeframe
function setTimeframe(id) {
  if (!TIMEFRAMES[id]) return;
  state.timeframe = id;
  state.cache.clear();
  for (const button of $('timeframe').querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.dataset.tf === id));
    button.classList.toggle('on', button.dataset.tf === id);
  }
  if (state.page === 'timeline') applyTimeframeToTimeline();
  ensureData(state.page).then(renderPage).catch((err) => setStatus(`view failed: ${err.message}`, 'stale'));
}

function applyTimeframeToTimeline() {
  const days = timeframeDays();
  if (!days) {
    timeline.fit();
    return;
  }
  const end = timeline.now;
  timeline.setWindow(end - days * DAY, end);
}

// ----------------------------------------------------------------- density
function setDensity(density) {
  timeline.setDensity(density);
  document.body.dataset.density = density;
  for (const button of $('density').querySelectorAll('button')) {
    const active = button.dataset.d === density;
    button.classList.toggle('on', active);
    button.setAttribute('aria-pressed', String(active));
  }
  saveUI();
}

// ------------------------------------------------------------------- data
function renderProvenance() {
  const meta = state.meta;
  if (!meta) return;
  syncChromeHeight();
  const capped = state.objectCapped ? ` · objects capped at ${state.objects.length}` : '';
  $('provenance').textContent =
    `Snapshot from ${ageOf(meta.as_of)}: ${meta.events} events, ${meta.prs} PRs, ${meta.commits} commits · ` +
    `${meta.source || 'unknown source'}, ${meta.prs_dropped} PRs without a ticket key dropped${capped}`;
  $('nav-count').textContent = `${meta.events} events · ${meta.prs} PRs`;
}

function setEmpty(reason) {
  state.emptyReason = reason || '';
  $('empty').hidden = !reason;
  if (reason) {
    $('empty').querySelector('p').textContent = reason;
    $('empty').querySelector('.muted').textContent =
      'Run python3 -m ingest --fixture --n 50 and reload.';
  }
}

/* Paged loading (GAP-04).
 *
 * `/api/events` and `/api/objects` take `limit`/`offset` and report the total
 * in `X-Total-Count`, so the first page paints immediately and the rest
 * streams in. `/api/events/counts` gives the histogram and the true window
 * totals without a single event row, which is what the honesty line uses.
 */
const EVENT_PAGE = 2000;
const EVENT_MAX = 40000;
const OBJECT_PAGE = 5000;
const OBJECT_MAX = 60000;
const RETRY_LIMIT = 20;

let loadSeq = 0;
let retryTimer = 0;
let retryCount = 0;

function totalOf(response, fallback) {
  const header = Number(response.headers.get('X-Total-Count'));
  return Number.isFinite(header) && header >= 0 ? header : fallback;
}

async function pageAll(url, pageSize, max, onPage) {
  let offset = 0;
  let total = Infinity;
  while (offset < total && offset < max) {
    const { response, body } = await fetchJSON(`${url}${url.includes('?') ? '&' : '?'}limit=${pageSize}&offset=${offset}`);
    if (!response.ok) throw new HttpError(response.status, body, url);
    if (!Array.isArray(body)) throw new Error(`${url} → unexpected payload`);
    total = totalOf(response, total === Infinity ? body.length : total);
    if (!body.length) break;
    offset += body.length;
    const enough = await onPage(body, offset, total);
    if (enough) break;
    if (body.length < pageSize) break;
  }
  return { loaded: offset, total: Number.isFinite(total) ? total : offset };
}

function storeBusy(error) {
  return Boolean(error && error.status === 503 && error.body && error.body.retryable);
}

function retryWhenFree() {
  if (retryCount >= RETRY_LIMIT) {
    setStatus('ingest is holding the store — reload when it finishes', 'stale');
    return;
  }
  retryCount += 1;
  clearTimeout(retryTimer);
  retryTimer = setTimeout(() => load({ force: true }), Math.min(8000, 400 * retryCount));
}

async function load({ force = false } = {}) {
  const seq = ++loadSeq;
  let health;
  try {
    health = await getJSON('/api/health');
  } catch (err) {
    if (storeBusy(err)) {
      setStatus('ingest running — waiting for the store', 'stale');
      retryWhenFree();
      return;
    }
    if (err.status === 503) {
      $('provenance').textContent = err.body.error || 'store unavailable';
      setEmpty('No store yet.');
      setStatus('no store', 'stale');
      return;
    }
    setStatus('offline', 'stale');
    $('provenance').textContent = `could not reach the API: ${err.message}`;
    return;
  }
  retryCount = 0;
  if (!health.ok) {
    $('provenance').textContent = health.error || 'store unavailable';
    setEmpty('No store yet.');
    setStatus('no store', 'stale');
    state.meta = null;
    state.stamp = '';
    return;
  }

  const stamp = `${health.as_of || ''}|${health.events ?? ''}`;
  if (!force && state.stamp === stamp && state.events.length) {
    // Same snapshot as last time (e.g. the store file was touched by a no-op
    // run): nothing to refetch, so nothing is refetched.
    state.meta = health;
    renderProvenance();
    setStatus(`live · ${state.events.length} events`, 'live');
    window.__megaspeedReady = true;
    return;
  }

  setEmpty('');
  state.meta = health;
  state.stamp = stamp;
  const previousView = timeline.captureView();
  try {
    // Cheap first: window totals + activity histogram, no event rows.
    try {
      state.counts = await getJSON('/api/events/counts?granularity=day');
    } catch (err) {
      if (storeBusy(err)) throw err;
      state.counts = null;
    }
    if (seq !== loadSeq) return;

    state.events = [];
    state.eventCapped = false;
    const events = await pageAll('/api/events', EVENT_PAGE, EVENT_MAX, async (page, loaded, total) => {
      if (seq !== loadSeq) return true;
      state.events.push(...page);
      state.eventCapped = loaded < total;
      // paint the first page straight away, then keep the canvas still
      if (loaded <= EVENT_PAGE) {
        applyScopeToTimeline();
        renderLegend();
        renderPhaseLegend();
        setStatus(`loading events ${loaded} of ${total}`, 'live');
      }
      return false;
    });
    if (seq !== loadSeq) return;
    state.eventCapped = events.loaded < events.total;

    state.objects = [];
    const objects = await pageAll('/api/objects', OBJECT_PAGE, OBJECT_MAX, async (page) => {
      if (seq !== loadSeq) return true;
      state.objects.push(...page);
      return false;
    });
    if (seq !== loadSeq) return;
    state.objectCapped = objects.loaded < objects.total;
  } catch (err) {
    if (seq !== loadSeq) return;
    if (storeBusy(err)) {
      setStatus('ingest running — waiting for the store', 'stale');
      retryWhenFree();
      return;
    }
    setStatus('offline', 'stale');
    $('provenance').textContent = `could not reach the API: ${err.message}`;
    return;
  }

  state.cache.clear();
  applyScopeToTimeline();
  if (previousView) timeline.restoreView(previousView);
  buildScopeOptions();
  renderProvenance();
  renderLegend();
  renderPhaseLegend();
  if (state.page !== 'timeline') await ensureData(state.page);
  renderPage();
  syncChromeHeight();
  setStatus(`live · ${state.events.length} events`, 'live');
  window.__megaspeedReady = true;
}

// ------------------------------------------------------------------ harness
let socket = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${proto}://${location.host}/ws`);

  socket.onopen = () => {
    if (state.meta) setStatus(`live · ${state.events.length} events`, 'live');
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
 * through an SVG foreignObject. Chromium handles that natively, so there is no
 * html2canvas dependency and no build step.
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
  // HTML comments may contain "--", which is illegal inside an XML comment; the
  // clone is serialised into an SVG foreignObject, so drop them all.
  const walker = document.createTreeWalker(clone, NodeFilter.SHOW_COMMENT);
  const comments = [];
  while (walker.nextNode()) comments.push(walker.currentNode);
  for (const comment of comments) comment.remove();
  // backdrop-filter does not survive SVG foreignObject rounding and smears the
  // canvas around the tooltip; flatten it in the clone only.
  for (const node of clone.querySelectorAll('*')) {
    node.style.backdropFilter = 'none';
    node.style.webkitBackdropFilter = 'none';
  }
  // B9: a cloned <select> only carries the `selected` attribute, not the live
  // value. Copy the state that is actually on screen.
  const liveFields = element.querySelectorAll('select, input, textarea');
  const cloneFields = clone.querySelectorAll('select, input, textarea');
  cloneFields.forEach((field, index) => {
    const live = liveFields[index];
    if (!live) return;
    if (field.tagName === 'SELECT') {
      for (const option of field.options) option.selected = option.value === live.value;
    } else if (field.type === 'checkbox' || field.type === 'radio') {
      field.checked = live.checked;
    } else {
      field.setAttribute('value', live.value);
    }
  });
  // every canvas in the element, in document order (timeline, graph, queues)
  const liveCanvases = [...element.querySelectorAll('canvas')];
  const cloneCanvases = [...clone.querySelectorAll('canvas')];
  cloneCanvases.forEach((target, index) => {
    const source = liveCanvases[index];
    if (!source || !source.width || !source.height) return;
    const img = document.createElement('img');
    img.src = source.toDataURL('image/png');
    img.style.width = `${source.clientWidth}px`;
    img.style.height = `${source.clientHeight}px`;
    img.style.display = 'block';
    target.replaceWith(img);
  });
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
  // Sticky/fixed chrome has no scroll container inside foreignObject; flatten it
  // so the capture lays out like the live page instead of stacking at y=0.
  style.textContent = `${css}
    .shot .topbar, .shot .sidebar, .shot .meta, .shot .table th, .shot .detail { position: static !important; }
    .shot .sidebar { height: auto !important; }
    .shot .drawer { position: static !important; width: auto; box-shadow: none; }
    .shot .app { min-height: 0 !important; }
    .shot .table th { top: auto !important; }`;
  clone.classList.add('shot');
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

// Exposed so the eval bridge can drive the UI.
window.__megaspeed = {
  timeline,
  blockingMap,
  queueChart,
  get state() { return state; },
  refresh: load,
  setPage: showPage,
  setScope,
  setTimeframe,
  setLanes: (dim) => { $('lanes').value = dim; timeline.setLanes(dim); renderPhaseLegend(); },
  setDensity,
  stamp: fmtStamp,
};

// ------------------------------------------------------------------- events
$('zoom-in').addEventListener('click', () => timeline.zoom(0.6));
$('zoom-out').addEventListener('click', () => timeline.zoom(1 / 0.6));
$('fit').addEventListener('click', () => timeline.fit());
$('selection-apply').addEventListener('click', () => {
  const chip = $('selection');
  const start = Number(chip.dataset.start);
  const end = Number(chip.dataset.end);
  if (end > start) timeline.setWindow(start, end);
});
$('selection-clear').addEventListener('click', () => timeline.clearSelection());
$('lanes').addEventListener('change', (event) => {
  timeline.setLanes(event.target.value);
  renderPhaseLegend();
  saveUI();
});
$('density').addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button) return;
  setDensity(button.dataset.d);
});
$('scope').addEventListener('change', (event) => {
  const [kind, value] = event.target.value.split(':');
  setScope(kind, value);
});
$('timeframe').addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (button) setTimeframe(button.dataset.tf);
});

$('blocking-level').addEventListener('change', async (event) => {
  state.blockingLevel = event.target.value;
  state.cache.clear();
  await ensureData('blocking');
  renderBlockingPage();
});
$('queue-level').addEventListener('change', async (event) => {
  state.queueLevel = event.target.value;
  state.cache.clear();
  await ensureData('queue');
  renderQueuePage();
});

document.addEventListener('click', async (event) => {
  const menuItem = event.target.closest('#menu [data-menu]');
  if (menuItem) {
    const entity = state.menuEntity;
    const action = menuItem.dataset.menu;
    closeMenu();
    if (action === 'copy') await copyMarkdown(entity);
    else if (action === 'clear') { setScope('all', 'all'); await showPage('timeline'); }
    else if (action === 'person' && entity) {
      const author = entity.author || (entity.pr && entity.pr.actor) || (entity.row && entity.row.author);
      if (author) setScope('person', author);
    } else if (action === 'timeline') {
      await showPage('timeline');
    }
    return;
  }
  if (!event.target.closest('#menu')) closeMenu();
  if (event.target.closest('#delivery-copy')) {
    const data = state.delivery;
    const rows = data ? tabRows(data, state.tab).filter((row) => matchesScopeRow(row)) : [];
    await copyText(deliveryMarkdown(rows, data ? `${data.assumption}; snapshot ${data.snapshot}` : ''));
    return;
  }
  const nav = event.target.closest('.nav-item');
  if (nav) {
    await showPage(nav.dataset.page);
    return;
  }
  const legend = event.target.closest('[data-activity]');
  if (legend) {
    timeline.cycleActivity(legend.dataset.activity);
    renderLegend();
    renderPhaseLegend();
    return;
  }
  const tab = event.target.closest('#delivery-tabs [data-tab]');
  if (tab) {
    state.tab = tab.dataset.tab;
    renderDeliveryPage();
    saveUI();
    return;
  }
  const lens = event.target.closest('[data-lens]');
  if (lens) {
    state.lens = lens.dataset.lens;
    buildLensButtons();
    blockingMap.setLens(state.lens);
    renderBlockingStats();
    return;
  }
  const row = event.target.closest('tr.row');
  if (row && row.dataset.id) {
    selectRow(row.dataset.id);
    return;
  }
  const go = event.target.closest('[data-go]');
  if (go) {
    await showPage(go.dataset.go);
    return;
  }
  if (event.target.closest('[data-drawer="close"]')) {
    closeDrawer();
    return;
  }
  const pivot = event.target.closest('[data-detail]');
  if (pivot) {
    const action = pivot.dataset.detail;
    if (action === 'copy') {
      await copyMarkdown(state.drawerEntity);
    } else if (action === 'timeline') {
      const author = state.drawerEntity?.pr?.actor || state.selection?.row?.author;
      closeDrawer();
      if (author) setScope('person', author, { silent: true });
      await showPage('timeline');
    } else if (action === 'blocking') {
      closeDrawer();
      await showPage('blocking');
    }
    return;
  }
});

document.addEventListener('contextmenu', (event) => {
  const found = entityForTarget(event.target, { x: event.clientX, y: event.clientY });
  if (!found) return;
  event.preventDefault();
  openMenu(event.clientX, event.clientY, found.entity, found.label);
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeDrawer();
    closeMenu();
    return;
  }
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const row = event.target.closest && event.target.closest('tr.row[data-id]');
  if (row) {
    event.preventDefault();
    selectRow(row.dataset.id);
  }
});

async function copyMarkdown(entity) {
  await copyText(entity ? entityMarkdown(entity) : '');
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    setStatus('copied as Markdown', 'live');
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); setStatus('copied as Markdown', 'live'); }
    catch { setStatus('copy failed — select the text manually', 'stale'); }
    area.remove();
  }
}

// ------------------------------------------------------------------- state
function saveUI() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      page: state.page,
      density: timeline.density,
      lanes: timeline.dim,
      timeframe: state.timeframe,
      tab: state.tab,
      lens: state.lens,
      blockingLevel: state.blockingLevel,
      queueLevel: state.queueLevel,
      activity: [...timeline.activityState.entries()],
      scope: state.scope,
    }));
  } catch { /* private mode: not worth a banner */ }
}

function restoreUI() {
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(LS_KEY) || 'null'); } catch { stored = null; }
  if (!stored) {
    setDensity('M');
    return;
  }
  if (PAGES[stored.page]) state.page = stored.page;
  if (TIMEFRAMES[stored.timeframe]) state.timeframe = stored.timeframe;
  if (stored.tab) state.tab = stored.tab;
  if (LENSES[stored.lens]) state.lens = stored.lens;
  if (stored.blockingLevel) state.blockingLevel = stored.blockingLevel;
  if (stored.queueLevel) state.queueLevel = stored.queueLevel;
  if (stored.lanes) { $('lanes').value = stored.lanes; timeline.setLanes(stored.lanes); }
  if (Array.isArray(stored.activity)) {
    for (const [activity, value] of stored.activity) timeline.setActivityState(activity, value);
  }
  if (stored.scope && stored.scope.kind) {
    state.scope = stored.scope;
  }
  if (stored.density) setDensity(stored.density);
  for (const button of $('timeframe').querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.dataset.tf === state.timeframe));
    button.classList.toggle('on', button.dataset.tf === state.timeframe);
  }
}

// -------------------------------------------------------------------- boot
restoreUI();
syncChromeHeight();
window.addEventListener('resize', syncChromeHeight);
buildLensButtons();
renderPage();
load().then(() => {
  showPage(state.page);
  connect();
  applyTimeframeToTimeline();
});

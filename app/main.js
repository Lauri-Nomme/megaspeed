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
 *   rules      What should we change first? ranked, evidence-backed rules
 *   policy     What should we change first? (simulation stub, next slice)
 */

import { Timeline, ACTIVITY_ORDER, ACTIVITY_COLOR, PHASE, PHASE_ORDER } from '/timeline.js';
import { BlockingMap, LENSES } from '/graph.js';
import { QueueChart } from '/queues.js';
import {
  DAY, fmtAge, fmtBucket, fmtCompact, fmtDay, fmtHours, fmtRange, fmtStamp, esc, whoLabel,
} from '/format.js';
import {
  TABS, tabCounts, tabRows, deliveryTable, overviewMarkup, detailMarkup,
  impactSentence, entityMarkdown, deliveryMarkdown,
} from '/tables.js';
import { rulesMarkup, rulesStats, rulesMarkdown } from '/rules.js';
import { annotateRow, epicKeySet, keyListsOf, rowTicket } from '/tickets.js';
import {
  ROLE_QUEUES, filteredPages, queueLevelForNode, scopeBanner, scopeForNode, scopeForStream,
} from '/scope.js';

const $ = (id) => document.getElementById(id);
const LS_KEY = 'megaspeed.ui.v1';

const PAGES = {
  overview: { title: 'Overview', question: 'What needs attention?' },
  delivery: { title: 'Delivery list', question: 'What needs attention?' },
  timeline: { title: 'Timeline', question: 'Where does the time go?' },
  blocking: { title: 'Blocking map', question: 'Who is blocking whom?' },
  queue: { title: 'Queue', question: 'Who is overloaded, and who is merely slow?' },
  rules: { title: 'Rules', question: 'What should we change first?' },
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
  tenants: [],
  tenant: null,
  scopes: {},
  events: [],
  objects: [],
  counts: null,
  eventCapped: false,
  eventLimit: 40000,
  eventTotal: 0,
  objectCapped: false,
  epicKeys: new Set(),
  page: 'timeline',
  scope: { kind: 'all', value: null, label: 'everything' },
  timeframe: '1q',
  // B6: the timeline's window is the timeframe only until the user brushes or
  // zooms. `timeframeCustom` says the control and the chart have diverged, and
  // `tfWindow` remembers the window the active preset actually produced (the
  // domain clamp means "1q" is not exactly 90 days on a short store).
  timeframeCustom: false,
  tfWindow: null,
  tab: 'blocked',
  // B11: an explicitly chosen tab (or a restored one) is never auto-switched.
  tabPinned: false,
  delivery: null,
  queues: null,
  blocking: null,
  rules: null,
  rulesLevel: 'team',
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
    // B6: the top bar follows the timeline's real window, and says "custom"
    // when a brush or a zoom has taken it away from the active timeframe.
    noteTimelineView(stats);
    if (state.page === 'timeline') {
      $('range').textContent = stats.total ? fmtRange(stats.start, stats.end) : 'no events';
    }
    // B7: the timeline's disclosure describes the timeline only. Every other
    // view writes its own line in render*Page(); an async repaint of a hidden
    // canvas must not overwrite it.
    if (state.page !== 'timeline') return;
    const bits = [];
    if (stats.aggregated) bits.push(`aggregated into ${fmtBucket(stats.bucketMs)} buckets — zoom in for individual events`);
    if (stats.openPrs) bits.push(`${stats.openPrs} PRs still open are right-censored at the snapshot time`);
    bits.push(`${stats.visible} of ${stats.total} events in view`);
    if (state.eventCapped) {
      bits.push(`only the newest ${fmtCompact(state.events.length)} of ${fmtCompact(state.eventTotal)} events are loaded`);
    }
    setHonesty(bits.join(' · '));
    renderLoadAll();
    updateTimelineEmpty(stats);
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

/** G5: `All` means the whole store, not the API's 90-day default. */
function allDays() {
  const meta = state.meta;
  if (!meta || !meta.first_ts || !meta.as_of) return null;
  const span = Date.parse(meta.as_of) - Date.parse(meta.first_ts);
  if (!(span > 0)) return null;
  // +1 day so the oldest event is inside the window, never one tick outside it.
  return Math.ceil(span / DAY) + 1;
}

function timeframeQuery() {
  const days = timeframeDays() ?? allDays();
  return days ? `days=${days}` : '';
}

/** The absolute window the active timeframe describes, from the snapshot. */
function presetWindow() {
  const meta = state.meta;
  if (!meta || !meta.as_of) return null;
  const end = Date.parse(meta.as_of);
  const days = timeframeDays();
  if (!days) {
    return meta.first_ts ? { start: Date.parse(meta.first_ts), end } : null;
  }
  return { start: end - days * DAY, end };
}

/* B6: the timeframe control is the top-bar contract for "when". A preset shows
 * pressed; a brush or a zoom shows a `custom` chip instead of leaving a preset
 * pressed over a different window. `#range` is the timeline's window and is
 * never painted onto another view: those views show the preset window they
 * really query. */
function renderTimeframe() {
  const custom = state.timeframeCustom;
  for (const button of $('timeframe').querySelectorAll('button[data-tf]')) {
    const active = !custom && button.dataset.tf === state.timeframe;
    button.setAttribute('aria-pressed', String(active));
    button.classList.toggle('on', active);
  }
  const chip = $('tf-custom');
  if (chip) {
    // The custom window belongs to the timeline; other views use the preset
    // they query, so the chip would contradict the range beside it (B6).
    chip.hidden = !custom || state.page !== 'timeline';
    chip.setAttribute('aria-pressed', String(custom));
  }
  if (state.page !== 'timeline') {
    const window = presetWindow();
    $('range').textContent = window ? fmtRange(window.start, window.end) : '—';
  } else if (custom) {
    const view = timeline.captureView();
    if (view) $('range').textContent = fmtRange(view.start, view.end);
  }
}

function noteTimelineView(stats) {
  // Nothing on the canvas: the preset is what the page asked for, and the empty
  // overlay (B10) explains the blank, so do not claim a custom window.
  if (!stats.total) {
    if (state.timeframeCustom) {
      state.timeframeCustom = false;
      renderTimeframe();
    }
    return;
  }
  const applied = state.tfWindow;
  const custom = !applied || Math.abs(stats.start - applied.start) > 1000
    || Math.abs(stats.end - applied.end) > 1000;
  if (custom !== state.timeframeCustom) {
    state.timeframeCustom = custom;
    renderTimeframe();
  }
}

/* B10: a zero-event timeline is a state, not a blank canvas. The fixture hint
 * only belongs to an empty store (B19); a scope or a zoom that hides everything
 * says what to do about it. */
function updateTimelineEmpty(stats) {
  const empty = $('empty');
  if (state.emptyReason) {
    empty.hidden = false;
    empty.querySelector('p').textContent = state.emptyReason;
    return;
  }
  if (stats.allHidden) {
    empty.hidden = false;
    empty.querySelector('p').textContent = 'Every activity is hidden — click a legend key to show it again.';
    empty.querySelector('.muted').textContent = 'Filtering de-emphasises or hides marks; it never deletes data.';
    return;
  }
  if (!stats.total) {
    empty.hidden = false;
    if (state.scope.kind !== 'all') {
      empty.querySelector('p').textContent = `No events for scope ${state.scope.label} in this dataset.`;
      empty.querySelector('.muted').textContent = 'Clear the scope in the top bar to see the whole team.';
    } else {
      empty.querySelector('p').textContent = 'No events in this window.';
      empty.querySelector('.muted').textContent = 'The store has no events to show; import a dataset, then reload.';
    }
    return;
  }
  if (!stats.visible) {
    empty.hidden = false;
    empty.querySelector('p').textContent = 'No events in this window.';
    empty.querySelector('.muted').textContent = 'Zoom out, press Home, or pick a wider timeframe.';
    return;
  }
  empty.hidden = true;
}

/* Every read is tenant-scoped (prd/12): the selected tenant is a query
 * parameter, not a different server, so one tab can only ever see one dataset
 * at a time and a switch is one refetch rather than a reload. */
function tenantQuery() {
  return state.tenant ? `tenant=${encodeURIComponent(state.tenant)}` : '';
}

function withTenant(url) {
  const query = tenantQuery();
  if (!query) return url;
  return `${url}${url.includes('?') ? '&' : '?'}${query}`;
}

/** Keep the sticky table header / detail panel below the (wrapping) meta line. */
function syncChromeHeight() {
  const meta = $('meta');
  const bottom = 40 + meta.offsetHeight + 2;
  document.documentElement.style.setProperty('--meta-bottom', `${bottom}px`);
}

let statusTimer = 0;

function liveStatusText() {
  if (!state.meta) return 'connecting';
  return `live · ${state.events.length} events${state.tenant ? ` · ${state.tenant}` : ''}`;
}

function setStatus(text, cls = '') {
  clearTimeout(statusTimer);
  $('status').textContent = text;
  $('status').className = `status ${cls}`.trim();
}

/* B13: feedback like "copied as Markdown" is transient. It returns to the live
 * state on its own, so the top-right never keeps claiming an old action. */
function flashStatus(text, cls = 'live', ms = 2500) {
  setStatus(text, cls);
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => {
    if (state.meta) setStatus(liveStatusText(), 'live');
    else setStatus('connecting');
  }, ms);
}

// -------------------------------------------------------------------- views
/** The one honesty line (B7): each view states its own cohort, or clears it. */
function setHonesty(text) {
  $('honesty').textContent = text || '';
}

/** B3: the banner is derived from the filters the views really apply. */
function renderScopeBanner() {
  const banner = $('scopebanner');
  const text = scopeBanner(state.scope, state.page);
  banner.hidden = !text;
  banner.textContent = text;
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
  syncLevelSelects();
  renderScopeBanner();
  setHonesty('');
  renderTimeframe();
  if (page === 'timeline') renderTimelinePage();
  if (page === 'delivery') renderDeliveryPage();
  if (page === 'blocking') renderBlockingPage();
  if (page === 'queue') renderQueuePage();
  if (page === 'rules') renderRulesPage();
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
  const tenant = state.tenant || 'none';
  if (page === 'delivery' || page === 'overview') {
    const key = `delivery:${tenant}:${state.timeframe}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(withTenant(`/api/delivery?${q}`)));
    state.delivery = cached(key);
  }
  if (page === 'blocking' || page === 'overview') {
    const key = `blocking:${tenant}:${state.timeframe}:${state.blockingLevel}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(withTenant(`/api/blocking?level=${state.blockingLevel}&${q}`)));
    state.blocking = cached(key);
  }
  if (page === 'queue' || page === 'overview') {
    const key = `queues:${tenant}:${state.timeframe}:${state.queueLevel}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(withTenant(`/api/queues?level=${state.queueLevel}&${q}`)));
    state.queues = cached(key);
  }
  if (page === 'rules') {
    const key = `rules:${tenant}:${state.timeframe}:${state.rulesLevel}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(withTenant(`/api/insights?level=${state.rulesLevel}&${q}`)));
    state.rules = cached(key);
  }
}

// ------------------------------------------------------------------- levels
/* B8: the level state and the <select> that labels it are one thing. Every
 * path that changes a level goes through these setters, and restoreUI()
 * reconciles the selects with the restored state, so the toolbar can never
 * contradict the chart it labels. */
const BLOCKING_LEVELS = ['queue', 'person'];
const QUEUE_LEVELS = ['person', 'queue'];

function syncLevelSelects() {
  const blocking = $('blocking-level');
  if (blocking && BLOCKING_LEVELS.includes(state.blockingLevel)) blocking.value = state.blockingLevel;
  const queue = $('queue-level');
  if (queue && QUEUE_LEVELS.includes(state.queueLevel)) queue.value = state.queueLevel;
}

function setBlockingLevel(level) {
  if (!BLOCKING_LEVELS.includes(level)) return false;
  state.blockingLevel = level;
  syncLevelSelects();
  return true;
}

function setQueueLevel(level) {
  if (!QUEUE_LEVELS.includes(level)) return false;
  state.queueLevel = level;
  syncLevelSelects();
  return true;
}

// ------------------------------------------------------------------ tenants
/* The tenant list is the server's `data/*.duckdb` directory, so a dataset that
 * was just imported shows up here without a config edit. Each row carries its
 * own PR/event counts: two environments with the same name pattern are only
 * distinguishable by what is in them. */
function currentTenant() {
  return state.tenants.find((tenant) => tenant.name === state.tenant) || null;
}

function tenantSummary(tenant) {
  if (!tenant) return '';
  if (tenant.ok === false) return tenant.retryable ? 'ingest running' : 'unreadable';
  return `${tenant.prs} PRs · ${tenant.events} events`;
}

function tenantTitle(tenant) {
  if (!tenant) return 'dataset (tenant): one pseudonymised store per environment';
  const store = String(tenant.store || '').replace(/\\/g, '/').split('/').slice(-2).join('/');
  return `tenant ${tenant.name} — ${store}` +
    (tenant.source ? ` · ${tenant.source}` : '') +
    (tenant.last_ts ? ` · last event ${fmtDay(Date.parse(tenant.last_ts))}` : '');
}

async function loadTenants() {
  const data = await getJSON('/api/tenants');
  const list = Array.isArray(data.tenants) ? data.tenants : [];
  state.tenants = list;
  const names = new Set(list.map((tenant) => tenant.name));
  if (!state.tenant || !names.has(state.tenant)) {
    // No stored choice, or it was deleted: fall back to the server's default,
    // which is the first tenant by name.
    state.tenant = data.default && names.has(data.default)
      ? data.default
      : (list[0] ? list[0].name : null);
  }
  renderTenantSwitcher();
  return list;
}

function renderTenantSwitcher() {
  const tenant = currentTenant();
  $('tenant-name').textContent = state.tenant || 'none';
  $('tenant-meta').textContent = tenantSummary(tenant);
  $('tenant-button').classList.toggle('empty', !tenant);
  $('tenant-button').title = tenantTitle(tenant);
  if (!$('tenant-menu').hidden) renderTenantMenu();
}

function renderTenantMenu() {
  const menu = $('tenant-menu');
  if (!state.tenants.length) {
    menu.innerHTML = '<p class="tenant-empty">No datasets yet. An import lands in ' +
      '<code>data/&lt;tenant&gt;.duckdb</code>; reload once it exists.</p>';
    return;
  }
  menu.innerHTML = state.tenants.map((tenant) => `
    <button type="button" role="menuitemradio" data-tenant="${esc(tenant.name)}"
      aria-checked="${tenant.name === state.tenant}" class="${tenant.name === state.tenant ? 'on' : ''}"
      title="${esc(tenantTitle(tenant))}">
      <span class="tenant-opt-name mono">${esc(tenant.name)}</span>
      <span class="tenant-opt-meta">${esc(tenantSummary(tenant))}</span>
      <span class="tenant-opt-mark" aria-hidden="true">${tenant.name === state.tenant ? '✓' : ''}</span>
    </button>`).join('');
}

function toggleTenantMenu(open) {
  const menu = $('tenant-menu');
  const next = open === undefined ? menu.hidden : Boolean(open);
  if (next) renderTenantMenu();
  menu.hidden = !next;
  $('tenant-button').setAttribute('aria-expanded', String(next));
  if (next) {
    const first = menu.querySelector('button');
    if (first) first.focus();
  }
}

/** Drop every view's data so a stale row from another tenant cannot linger. */
function resetTenantData() {
  state.meta = null;
  state.stamp = '';
  state.events = [];
  state.objects = [];
  state.counts = null;
  state.delivery = null;
  state.blocking = null;
  state.queues = null;
  state.rules = null;
  state.selection = null;
  state.drawerEntity = null;
  state.emptyReason = '';
  state.eventCapped = false;
  state.eventTotal = 0;
  state.eventLimit = EVENT_MAX;
  state.epicKeys = new Set();
  state.cache.clear();
  $('load-all').hidden = true;
  // B12: a brush chip (and every hover/selection) belongs to the dataset it was
  // made in. Clear it with the data so "Set timeframe" cannot apply a window
  // from another tenant.
  timeline.clearSelection();
  timeline.setTicketIndex({ epicKeys: state.epicKeys });
  timeline.setData([], [], {});
  blockingMap.setData({ nodes: [], edges: [] });
  queueChart.setData({ streams: [], buckets: 1, window: { start: '', end: '' } });
  $('selection').hidden = true;
  $('queue-tip').hidden = true;
  $('blocking-tip').hidden = true;
  closeMenu();
  $('nav-count').textContent = '—';
}

async function setTenant(name, { silent = false } = {}) {
  if (!name) return;
  toggleTenantMenu(false);
  if (name === state.tenant && state.meta) return;
  // Scope is per tenant (prd/12): pseudonyms are independent identity spaces,
  // so a person scoped in one dataset means nothing in the next.
  if (state.tenant) state.scopes[state.tenant] = state.scope;
  state.tenant = name;
  state.scope = state.scopes[name] || { kind: 'all', value: null, label: 'everything' };
  resetTenantData();
  closeDrawer();
  renderTenantSwitcher();
  renderScopeBanner();
  saveUI();
  setStatus(`loading ${name}…`, 'live');
  await load({ force: true });
  if (state.tenant !== name) return; // a later switch won
  applyTimeframeToTimeline();
  renderPage();
  if (!silent && state.meta) {
    setStatus(liveStatusText(), 'live');
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
  // Returning to the timeline with a preset active shows that preset again;
  // a custom brush/zoom window is the user's window and is left alone (B6).
  if (!state.timeframeCustom) applyTimeframeToTimeline();
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
  // B2: label each row with its own ticket key, not the shared epic link. The
  // API names the parent keys it excluded from impact; union with the store
  // census so the table and the impact sentence agree.
  if (data && data.rows) {
    const epics = new Set([
      ...(state.epicKeys || []),
      ...(Array.isArray(data.parent_keys) ? data.parent_keys : []),
    ]);
    for (const row of data.rows) annotateRow(row, epics);
  }
  const counts = scopedTabCounts(data);
  if (data && state.tab !== 'all' && !state.tabPinned) {
    // The actionable subset is the default, but never an empty screen. An
    // explicit choice (or a restored tab) is never overridden: `Blocked 0`
    // must be selectable and say "nothing blocked" (B11).
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
  // B9: the detail panel must not describe a row the table no longer contains.
  // A scope change (or a refresh) re-renders the table, so re-resolve the
  // selection against the new rows and drop it when it is out of scope.
  if (state.selection && state.selection.row) {
    const selected = state.selection.row;
    const current = data && data.rows.find((row) => row.object_id === selected.object_id);
    if (!current || !matchesScopeRow(current)) state.selection = null;
    else state.selection.row = current;
  }
  const selectedId = state.selection && state.selection.row ? state.selection.row.object_id : null;
  for (const tr of document.querySelectorAll('#delivery-table tr.row')) {
    tr.classList.toggle('selected', tr.dataset.id === selectedId);
  }
  if (data) {
    const filtered = filteredPages(state.scope).has('delivery');
    setHonesty(
      `${counts.all} open item${counts.all === 1 ? '' : 's'} in view · ` +
      `${counts.blocked} blocked · ${counts.oldest} waiting over 3 days · ` +
      `impact = dependents × current wait (≈) · ` +
      (filtered ? `filtered to ${state.scope.label}` : 'whole team'),
    );
  }
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
      ['ticket', rowTicket(row)],
      ...(row.epic ? [['epic', row.epic]] : []),
      ['waiting on', whoLabel(row.ball_label || row.ball)],
      ['waiting since', `${fmtStamp(Date.parse(row.waiting_since))} (${fmtHours(row.waiting_hours)})`],
      ['phase', row.phase],
      ['age', fmtHours(row.age_hours)],
      ['commits in log', row.commits],
      ['dependents', row.dependents
        ? `${row.dependents} other open PR${row.dependents === 1 ? '' : 's'} on ${rowTicket(row)}`
        : 'none'],
      ['impact', row.dependents ? `≈${fmtHours(row.impact_hours)}` : '—'],
    ],
    children: [rowTicket(row), row.epic, row.id].filter(Boolean),
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
  if (data) {
    const totals = data.totals || {};
    const scoped = state.scope.kind !== 'all';
    setHonesty(
      `${totals.nodes || 0} nodes · ${totals.edges || 0} edges · ` +
      `${fmtHours(totals.hours || 0)} measured wait in the ${data.window.days} d window · ` +
      (scoped ? 'not filtered by scope — every handover stays visible' : 'whole team'),
    );
  }
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
  // B5: the drill-down must land on the level that can show the clicked node.
  // A person node opens the person-level chart; a role/queue node the queue
  // level — otherwise the queue view is scoped to someone no stream matches.
  setQueueLevel(queueLevelForNode(node));
  const scope = scopeForNode(node);
  if (scope) setScope(scope.kind, scope.value, { silent: true });
  await showPage('queue');
}

// -------------------------------------------------------------------- queue
function renderQueuePage() {
  const data = state.queues;
  const streams = data ? data.streams.filter((stream) => queueMatchesScope(stream)) : [];
  // An empty chart under a scope is a doorway, not a dead end: say which level
  // would show it (B5's failure mode, reached from the scope select).
  let emptyText = '';
  if (data && data.streams.length && !streams.length && filteredPages(state.scope).has('queue')) {
    const other = state.queueLevel === 'queue' ? 'people' : 'queues';
    emptyText = `No stream matches scope ${state.scope.label} at this level — switch level to ${other}.`;
  }
  queueChart.setData(data ? { ...data, streams, emptyText } : { streams: [], buckets: 1, window: { start: '', end: '' } });
  $('queue-assumption').textContent = data
    ? `${data.assumption} · ${data.bucket_hours} h buckets over ${data.window.days} days. ` +
      `Little's law: WIP ≈ throughput × wait. Overloaded = long wait and a standing queue; slow = long wait with little WIP. ` +
      `${data.diagnosis_note}.`
    : '';
  if (data) {
    const filtered = filteredPages(state.scope).has('queue');
    setHonesty(
      `${streams.length} stream${streams.length === 1 ? '' : 's'} · ` +
      `WIP sampled every ${data.bucket_hours} h over ${data.window.days} days · ` +
      (filtered ? `filtered to ${state.scope.label}` : 'whole team'),
    );
  }
}

function queueMatchesScope(stream) {
  if (state.scope.kind === 'all') return true;
  if (state.scope.kind === 'person') return stream.id === state.scope.value;
  if (state.scope.kind === 'role') {
    // Role/queue scopes match the stream id, including the synthetic `author`
    // and `review` streams (B4).
    return String(stream.id).toLowerCase() === String(state.scope.value).toLowerCase();
  }
  return true;
}

// -------------------------------------------------------------------- rules
/* The Policy-lab page (prd/17): a ranked list of "change this" rules. Team
 * level is the flow tax; people level adds per-person rules, which carry a
 * pseudonym and never a raw name (the store cannot hold one). */
function buildRulesLevels() {
  const levels = [['team', 'team'], ['person', 'people (pseudonyms)']];
  $('rules-level').innerHTML = levels.map(([id, label]) => `
    <button type="button" data-rule-level="${id}" aria-pressed="${state.rulesLevel === id}"
      class="${state.rulesLevel === id ? 'on' : ''}">${esc(label)}</button>`).join('');
}

function renderRulesPage() {
  buildRulesLevels();
  const data = state.rules;
  $('rules-list').innerHTML = data
    ? rulesMarkup(data)
    : '<div class="empty-row">Loading the rule engine…</div>';
  $('rules-stats').innerHTML = data ? rulesStats(data) : '';
  // Each view owns its disclosure (B7): on this page say what the cohort is,
  // so "aggregated into 2 h buckets" never describes a rule.
  setHonesty(data
    ? `${data.sample.prs} PRs opened in the window are the cohort · ` +
      `${data.sample.threads} classified threads · impact is an estimate (≈)`
    : '');
  $('rules-assumption').textContent = data
    ? `${data.assumption} · snapshot ${fmtStamp(Date.parse(data.as_of))} · ` +
      `targets: first review < ${fmtHours(data.targets.first_review_hours)}, ` +
      `cycle < ${fmtHours(data.targets.cycle_hours)}, ` +
      `${data.targets.max_review_rounds} rounds max · ` +
      `size cap ${data.targets.size_commit_cap} commits / ${data.targets.size_line_cap} lines · ` +
      `one review round ≈ ${fmtHours(data.targets.hours_per_round)} ` +
      `(${data.targets.hours_per_round_basis}) · ranked by impact × confidence.` +
      (data.metrics && data.metrics.pr_size && data.metrics.pr_size.diff_size.pending
        ? ' Diff size is not captured yet, so the size metric uses commit count only.'
        : '')
    : '';
}

async function setRulesLevel(level) {
  if (level === state.rulesLevel) return;
  state.rulesLevel = level;
  state.cache.clear();
  await ensureData('rules');
  renderRulesPage();
  saveUI();
}

// ----------------------------------------------------------------- overview
function renderOverviewPage() {
  $('overview').innerHTML = overviewMarkup({
    delivery: state.delivery,
    queues: state.queues,
    blocking: state.blocking,
    queueLevel: state.queueLevel,
  });
  // B7: the overview is a whole-team summary; say so instead of inheriting a
  // disclosure about a different view.
  const counts = state.delivery ? state.delivery.counts : null;
  setHonesty(counts
    ? `${counts.open} open items · ${counts.blocked} blocked · ` +
      `whole team in the window${state.scope.kind === 'all' ? '' : ' — the overview is not scope-filtered'}`
    : '');
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
  const tenant = currentTenant() || {};
  const rows = [
    ['tenant', meta.tenant || state.tenant || '—'],
    ['store', storeLabel(meta)],
    ['source', meta.source || '—'],
    ['snapshot', meta.as_of ? `${fmtStamp(Date.parse(meta.as_of))} (${ageOf(meta.as_of)})` : '—'],
    ['events', String(meta.events ?? '—')],
    ['PRs', `${meta.prs ?? '—'} kept of ${meta.prs_seen ?? '—'} seen`],
    ['commits', String(meta.commits ?? '—')],
    ['dropped', `${meta.prs_dropped ?? '—'} PRs had no ticket key`],
    ['window', `${meta.first_ts ? fmtDay(Date.parse(meta.first_ts)) : '—'} → ${meta.last_ts ? fmtDay(Date.parse(meta.last_ts)) : '—'}`],
    ['duration basis', meta.calendar && meta.calendar.hours_mode === 'business'
      ? `business hours — ${meta.calendar.working_hours?.default || '24/7'} ${meta.calendar.timezone || ''}, holidays and vacations excluded`
      : 'wall-clock hours; no working-hours calendar configured'],
  ];
  $('sources-body').innerHTML = `<dl class="facts">${rows.map(([key, value]) =>
    `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>
    <p class="panel-note">The store holds ticket keys, PR ids, commit hashes, actor pseudonyms,
      timestamps and closed attribute labels only. No title, description, comment, message or diff
      is stored, so no view can render one.</p>
    <p class="panel-note">A tenant is one dataset: <code>data/&lt;tenant&gt;.duckdb</code>. Tenants are
      independent identity spaces — the same pseudonym in two datasets need not be the same person,
      and no view joins across them.</p>` +
    (state.tenants.length > 1
      ? `<p class="panel-note">${state.tenants.length} datasets on this server${tenant.ok === false ? ' (this one is unreadable)' : ''}. Switch with the tenant pill in the top bar.</p>`
      : '');
  // B7: provenance is this view's disclosure, not the timeline's.
  setHonesty(state.meta
    ? `tenant ${state.meta.tenant || state.tenant || '—'} · ` +
      `snapshot ${fmtStamp(Date.parse(state.meta.as_of))} · read-only, pseudonymised store`
    : '');
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
    ['ticket', pr.ticket || '—'],
    ...(pr.epic ? [['epic', pr.epic]] : []),
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
    children: [pr.ticket, pr.epic, `${pr.repo}#${pr.number}`].filter(Boolean),
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
        label: `${rowTicket(record) || '—'} #${record.number} ${record.repo_short}`,
      };
    }
  }
  if (target.id === 'timeline' && point) {
    const rect = target.getBoundingClientRect();
    const hit = timeline.hitTest(point.x - rect.left, point.y - rect.top);
    if (hit && (hit.kind === 'bar' || hit.kind === 'interval') && hit.pr) {
      return {
        entity: { pr: hit.pr, author: hit.pr.actor, row: findDeliveryRow(hit.pr.id) },
        label: `${hit.pr.ticket ? `${hit.pr.ticket} ` : ''}#${hit.pr.number} ${hit.pr.repo}`,
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
  // Every closed role queue is a scope option, so a role scope set from the
  // queue chart or a graph node always has a matching <option> (B4).
  options.push(...ROLE_QUEUES.map((role) => [`role:${role}`, `queue ${role}`]));
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
  // B10: a scope is valid only in this tenant's data. Reset an unknown person
  // (or role) instead of leaving every view blank with no explanation.
  const valid = validateScope();
  buildScopeOptions();
  applyScopeToTimeline();
  renderPage();
  if (!silent) {
    if (!valid) flashStatus('that scope has no events in this dataset — showing everything', 'stale');
    else flashStatus(kind === 'all' ? 'scope cleared' : `scoped to ${state.scope.label}`, 'live');
  }
}

function setScopeFromQueue(stream) {
  // B4: a stream named after a role (`author`, `review`, `CI`) is a queue, not a
  // person. Scoping to `person:author` blanked the timeline and the top-bar
  // select; scope to the role instead.
  const scope = scopeForStream(stream);
  if (!scope) return;
  setScope(scope.kind, scope.value);
}

function scopedEvents() {
  if (state.scope.kind !== 'person') return state.events;
  return state.events.filter((event) => event.actor === state.scope.value);
}

/** A person scope is only meaningful inside one tenant's identity space. */
function validateScope() {
  const person = state.scope.kind === 'person'
    && !state.events.some((event) => event.actor === state.scope.value);
  const role = state.scope.kind === 'role'
    && !ROLE_QUEUES.some((queue) => queue.toLowerCase() === String(state.scope.value || '').toLowerCase());
  if (person || role) {
    state.scope = { kind: 'all', value: null, label: 'everything' };
    return false;
  }
  return true;
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
  // The epic census is store-wide: a scope may hide most PRs, but the label
  // must still prefer each PR's own ticket over the shared epic link (B2).
  timeline.setTicketIndex({ epicKeys: state.epicKeys });
  timeline.setData(events, scopedObjects(events), state.meta || {});
}

// --------------------------------------------------------------- timeframe
function setTimeframe(id) {
  if (!TIMEFRAMES[id]) return;
  state.timeframe = id;
  state.timeframeCustom = false;
  state.tfWindow = null;
  state.cache.clear();
  renderTimeframe();
  if (state.page === 'timeline') applyTimeframeToTimeline();
  ensureData(state.page).then(renderPage).catch((err) => setStatus(`view failed: ${err.message}`, 'stale'));
}

function applyTimeframeToTimeline() {
  // An empty event set has a placeholder 1-day domain; fitting or clamping a
  // preset to it would leave a preset pressed over a window it did not choose
  // (B6/B10). Leave the view alone and let the empty state explain it.
  if (!timeline.events.length) {
    state.tfWindow = null;
    return;
  }
  const days = timeframeDays();
  if (!days) {
    timeline.fit();
  } else {
    const end = timeline.now;
    timeline.setWindow(end - days * DAY, end);
  }
  state.tfWindow = { ...timeline.view };
  state.timeframeCustom = false;
  renderTimeframe();
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
    `${meta.tenant ? `${meta.tenant} · ` : ''}` +
    `Snapshot from ${ageOf(meta.as_of)}: ${meta.events} events, ${meta.prs} PRs, ${meta.commits} commits · ` +
    `${meta.source || 'unknown source'}, ${meta.prs_dropped} PRs without a ticket key dropped${capped}`;
  $('nav-count').textContent = `${meta.events} events · ${meta.prs} PRs`;
  const calendar = meta.calendar || {};
  const basis = $('timebasis');
  if (basis) {
    const business = calendar.hours_mode === 'business';
    basis.textContent = business ? 'business hours' : 'wall-clock hours';
    basis.title = business
      ? `prd/19: ${calendar.assumption || 'business hours'}` +
        ` (${calendar.overrides || 0} overrides, ${calendar.vacations || 0} vacations)`
      : (calendar.error || 'no working-hours calendar configured');
  }
}

function setEmpty(reason, hint) {
  state.emptyReason = reason || '';
  $('empty').hidden = !reason;
  if (reason) {
    $('empty').querySelector('p').textContent = reason;
    $('empty').querySelector('.muted').textContent =
      hint || 'Run python3 -m ingest --fixture --n 50 and reload.';
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
    // Ask for no more than the cap allows: a full page would overshoot it.
    const want = Math.min(pageSize, Math.max(1, max - offset));
    const { response, body } = await fetchJSON(`${url}${url.includes('?') ? '&' : '?'}limit=${want}&offset=${offset}`);
    if (!response.ok) throw new HttpError(response.status, body, url);
    if (!Array.isArray(body)) throw new Error(`${url} → unexpected payload`);
    total = totalOf(response, total === Infinity ? body.length : total);
    if (!body.length) break;
    offset += body.length;
    const enough = await onPage(body, offset, total);
    if (enough) break;
    if (body.length < want) break;
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

/* NFR-2: the default view is the recent window, not the oldest slice.
 *
 * `/api/events` is paged newest-first (`order=desc`), so the cap keeps the most
 * recent rows: with a 40k cap on a much larger store the timeline still opens
 * on "now" instead of the oldest 40k events. `state.eventLimit` is the only
 * knob; the "Load the full range" action raises it and says what it costs.
 */
async function loadEvents(seq) {
  state.events = [];
  state.eventCapped = false;
  const events = await pageAll(
    withTenant('/api/events?order=desc'),
    EVENT_PAGE,
    state.eventLimit,
    async (page, loaded, total) => {
      if (seq !== loadSeq) return true;
      state.events.push(...page);
      state.eventTotal = total;
      state.eventCapped = loaded < total;
      // paint the first page straight away, then keep the canvas still
      if (loaded <= EVENT_PAGE) {
        applyScopeToTimeline();
        renderLegend();
        renderPhaseLegend();
        setStatus(`loading events ${loaded} of ${total}`, 'live');
      }
      return false;
    },
  );
  if (seq !== loadSeq) return null;
  state.eventCapped = events.loaded < events.total;
  state.eventTotal = events.total;
  // Paged newest-first, kept chronological: scope counts, exports and the
  // timeline domain all read oldest → newest.
  state.events.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  return events;
}

/* PR objects first, then everything else. `/api/objects` orders by
 * `object_type`, so a capped page of a commit-heavy store can stop before the
 * PR rows and leave the timeline without lanes. PRs are few, so fetch them
 * unconditionally and merge the rest by object id. */
async function loadObjects(seq) {
  const byId = new Map();
  const add = (rows) => {
    for (const row of rows) if (!byId.has(row.object_id)) byId.set(row.object_id, row);
  };
  await pageAll(withTenant('/api/objects?type=PR'), OBJECT_PAGE, OBJECT_MAX, async (page) => {
    if (seq !== loadSeq) return true;
    add(page);
    return false;
  });
  if (seq !== loadSeq) return null;
  const objects = await pageAll(withTenant('/api/objects'), OBJECT_PAGE, OBJECT_MAX, async (page) => {
    if (seq !== loadSeq) return true;
    add(page);
    return false;
  });
  if (seq !== loadSeq) return null;
  state.objects = [...byId.values()];
  state.objectCapped = objects.loaded < objects.total;
  // B2: the epic-key census is store-wide, so a scope that hides most PRs
  // cannot turn the shared epic key back into a label.
  state.epicKeys = epicKeySet(keyListsOf(state.objects));
  return objects;
}

/** Show the cap and offer to lift it, with the cost stated (QA round 2, G5). */
function renderLoadAll() {
  const button = $('load-all');
  if (!button) return;
  const total = Math.max(state.eventTotal, state.counts ? state.counts.total || 0 : 0);
  const capped = state.eventCapped && total > state.events.length;
  button.hidden = !capped;
  if (!capped) return;
  button.textContent = `Load the full range · ${fmtCompact(state.events.length)} of ${fmtCompact(total)} events`;
  button.title = `The timeline holds the newest ${state.events.length} of ${total} events. ` +
    `Load all ${total} (${fmtCompact(total - state.events.length)} more rows) instead.`;
}

async function loadFullRange() {
  if (!state.eventCapped) return;
  const total = Math.max(state.eventTotal, state.counts ? state.counts.total || 0 : 0);
  if (!total) return;
  const seq = ++loadSeq;
  const view = timeline.captureView();
  state.eventLimit = Math.max(total, state.events.length);
  $('load-all').hidden = true;
  setStatus(`loading all ${fmtCompact(total)} events…`, 'live');
  try {
    await loadEvents(seq);
  } catch (err) {
    if (seq !== loadSeq) return;
    state.eventLimit = EVENT_MAX;
    renderLoadAll();
    setStatus(`could not load the full range: ${err.message}`, 'stale');
    return;
  }
  if (seq !== loadSeq) return;
  applyScopeToTimeline();
  if (view) timeline.restoreView(view);
  buildScopeOptions();
  renderLegend();
  renderPhaseLegend();
  renderPage();
  setStatus(liveStatusText(), 'live');
}

async function load({ force = false } = {}) {
  const seq = ++loadSeq;
  let health;
  try {
    await loadTenants();
  } catch (err) {
    setStatus('offline', 'stale');
    $('provenance').textContent = err.status === 404
      // A UI newer than the server it is served by (a long-running process
      // started before tenants existed): say so instead of "404".
      ? 'this server does not know about tenants yet — restart it, then reload'
      : `could not reach the API: ${err.message}`;
    return;
  }
  if (seq !== loadSeq) return;
  if (!state.tenant) {
    resetTenantData();
    $('provenance').textContent = 'No datasets — import one into data/<tenant>.duckdb, then reload.';
    setEmpty('No datasets yet.');
    setStatus('no data', 'stale');
    return;
  }
  try {
    health = await getJSON(withTenant('/api/health'));
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
    if (err.status === 404) {
      // The tenant was deleted (or renamed) between the list and the read.
      $('provenance').textContent = `dataset ${state.tenant} is gone — pick another dataset`;
      setEmpty(`No dataset named ${state.tenant}.`);
      setStatus('tenant missing', 'stale');
      state.tenant = null;
      try { await loadTenants(); } catch { /* offline: the store watcher retries */ }
      if (state.tenant && seq === loadSeq) await load({ force: true });
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

  const stamp = `${health.tenant || ''}|${health.as_of || ''}|${health.events ?? ''}`;
  if (!force && state.stamp === stamp && state.events.length) {
    // Same snapshot as last time (e.g. the store file was touched by a no-op
    // run): nothing to refetch, so nothing is refetched.
    state.meta = health;
    renderProvenance();
    setStatus(liveStatusText(), 'live');
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
      state.counts = await getJSON(withTenant('/api/events/counts?granularity=day'));
    } catch (err) {
      if (storeBusy(err)) throw err;
      state.counts = null;
    }
    if (seq !== loadSeq) return;

    await loadEvents(seq);
    if (seq !== loadSeq) return;
    await loadObjects(seq);
    if (seq !== loadSeq) return;
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
  validateScope();
  // A tenant with an empty store is a reachable screen (the switcher lists it),
  // so say why it is empty instead of drawing a blank timeline.
  if (state.events.length) {
    setEmpty('');
  } else {
    setEmpty(
      `Dataset ${state.tenant} holds no events.`,
      `Nothing has been imported into data/${state.tenant}.duckdb yet, or that import is empty. ` +
        'Switch datasets with the tenant pill in the top bar.',
    );
  }
  applyScopeToTimeline();
  if (previousView) timeline.restoreView(previousView);
  buildScopeOptions();
  renderTenantSwitcher();
  renderProvenance();
  renderLegend();
  renderPhaseLegend();
  renderLoadAll();
  if (state.page !== 'timeline') await ensureData(state.page);
  renderPage();
  syncChromeHeight();
  setStatus(liveStatusText(), 'live');
  window.__megaspeedReady = true;
}

// ------------------------------------------------------------------ harness
let socket = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${proto}://${location.host}/ws`);

  socket.onopen = () => {
    if (state.meta) setStatus(liveStatusText(), 'live');
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
  // B21: a cloned <select> only carries the `selected` *attribute*, not the
  // live value; a property assignment does not survive XML serialisation. Copy
  // the option state as attributes so the capture shows the value on screen.
  const liveFields = element.querySelectorAll('select, input, textarea');
  const cloneFields = clone.querySelectorAll('select, input, textarea');
  cloneFields.forEach((field, index) => {
    const live = liveFields[index];
    if (!live) return;
    if (field.tagName === 'SELECT') {
      for (const option of field.options) {
        const selected = option.value === live.value;
        option.selected = selected;
        if (selected) option.setAttribute('selected', 'selected');
        else option.removeAttribute('selected');
      }
    } else if (field.type === 'checkbox' || field.type === 'radio') {
      field.checked = live.checked;
      if (live.checked) field.setAttribute('checked', 'checked');
      else field.removeAttribute('checked');
    } else {
      field.setAttribute('value', live.value);
    }
  });
  // B21: fixed-position overlays (drawer, menu) are part of the screen but not
  // of the element's flow. Place their clones at the coordinates they occupy on
  // screen instead of letting the `.shot` rules flatten them below the page.
  for (const live of [document.getElementById('drawer'), document.getElementById('menu')]) {
    if (!live || live.hidden || live === element) continue;
    const target = clone.id === live.id ? clone : clone.querySelector(`#${live.id}`);
    if (!target) continue;
    const r = live.getBoundingClientRect();
    target.style.setProperty('position', 'absolute', 'important');
    target.style.setProperty('left', `${r.left - rect.left}px`, 'important');
    target.style.setProperty('top', `${r.top - rect.top}px`, 'important');
    target.style.setProperty('right', 'auto', 'important');
    target.style.setProperty('bottom', 'auto', 'important');
    target.style.width = `${r.width}px`;
    target.style.height = `${r.height}px`;
    target.style.zIndex = '30';
  }
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
    `position:relative;width:${rect.width}px;height:${rect.height}px;background:#191B1C;` +
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
  get tenants() { return state.tenants; },
  refresh: load,
  setPage: showPage,
  setScope,
  setTimeframe,
  setTenant,
  loadFullRange,
  // Test hook: lower the event cap so the capped path (and its "load the full
  // range" affordance) can be exercised on a small fixture.
  setEventLimit: (limit) => {
    state.eventLimit = Math.max(1, Number(limit) || EVENT_MAX);
    return state.eventLimit;
  },
  setLanes: (dim) => { $('lanes').value = dim; timeline.setLanes(dim); renderPhaseLegend(); },
  setDensity,
  setRulesLevel,
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
  if (!setBlockingLevel(event.target.value)) return;
  state.cache.clear();
  await ensureData('blocking');
  renderBlockingPage();
  saveUI();
});
$('queue-level').addEventListener('change', async (event) => {
  if (!setQueueLevel(event.target.value)) return;
  state.cache.clear();
  await ensureData('queue');
  renderQueuePage();
  saveUI();
});
$('load-all').addEventListener('click', () => { loadFullRange(); });

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
  if (event.target.closest('#tenant-button')) {
    toggleTenantMenu();
    return;
  }
  const tenantOption = event.target.closest('#tenant-menu [data-tenant]');
  if (tenantOption) {
    await setTenant(tenantOption.dataset.tenant);
    return;
  }
  if (!event.target.closest('#tenant-switcher')) toggleTenantMenu(false);
  if (event.target.closest('#delivery-copy')) {
    const data = state.delivery;
    const rows = data ? tabRows(data, state.tab).filter((row) => matchesScopeRow(row)) : [];
    await copyText(deliveryMarkdown(rows, data ? `${data.assumption}; snapshot ${data.snapshot}` : ''));
    return;
  }
  if (event.target.closest('#rules-copy')) {
    await copyText(rulesMarkdown(state.rules));
    return;
  }
  const ruleLevel = event.target.closest('[data-rule-level]');
  if (ruleLevel) {
    await setRulesLevel(ruleLevel.dataset.ruleLevel);
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
    state.tabPinned = true;
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
    toggleTenantMenu(false);
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
  const hoursMode = (state.delivery && state.delivery.hours_mode) || 'wall-clock';
  await copyText(entity ? entityMarkdown(entity, hoursMode) : '');
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    flashStatus('copied as Markdown', 'live');
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); flashStatus('copied as Markdown', 'live'); }
    catch { flashStatus('copy failed — select the text manually', 'stale'); }
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
      rulesLevel: state.rulesLevel,
      activity: [...timeline.activityState.entries()],
      scope: state.scope,
      tenant: state.tenant,
      // scope is per tenant (prd/12): remember each dataset's own lens
      scopes: state.scopes,
    }));
  } catch { /* private mode: not worth a banner */ }
}

function restoreUI() {
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(LS_KEY) || 'null'); } catch { stored = null; }
  if (!stored) {
    setDensity('M');
    syncLevelSelects();
    return;
  }
  // The tenant must be restored before anything reads state.scope: the scope
  // stored at the top level belongs to that dataset.
  if (typeof stored.tenant === 'string' && stored.tenant) state.tenant = stored.tenant;
  if (stored.scopes && typeof stored.scopes === 'object') {
    for (const [name, scope] of Object.entries(stored.scopes)) {
      if (scope && scope.kind) state.scopes[name] = scope;
    }
  }
  if (PAGES[stored.page]) state.page = stored.page;
  if (TIMEFRAMES[stored.timeframe]) state.timeframe = stored.timeframe;
  if (stored.tab) {
    state.tab = stored.tab;
    state.tabPinned = true;
  }
  if (LENSES[stored.lens]) state.lens = stored.lens;
  if (stored.blockingLevel && BLOCKING_LEVELS.includes(stored.blockingLevel)) {
    state.blockingLevel = stored.blockingLevel;
  }
  if (stored.queueLevel && QUEUE_LEVELS.includes(stored.queueLevel)) {
    state.queueLevel = stored.queueLevel;
  }
  if (stored.rulesLevel === 'team' || stored.rulesLevel === 'person') {
    state.rulesLevel = stored.rulesLevel;
  }
  if (stored.lanes) { $('lanes').value = stored.lanes; timeline.setLanes(stored.lanes); }
  if (Array.isArray(stored.activity)) {
    for (const [activity, value] of stored.activity) timeline.setActivityState(activity, value);
  }
  const scoped = state.tenant && state.scopes[state.tenant];
  if (scoped && scoped.kind) {
    state.scope = scoped;
  } else if (stored.scope && stored.scope.kind) {
    state.scope = stored.scope;
  }
  if (stored.density) setDensity(stored.density);
  renderTimeframe();
  // B8: the restored level state is the source of truth — make the <select>
  // that labels it agree, or the toolbar contradicts the chart.
  syncLevelSelects();
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

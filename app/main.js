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
 *   trend      Is it getting better?     one metric per week/sprint (SVG)
 *   blocking   Who is blocking whom?     weighted handover graph (canvas)
 *   queue      Who is slow vs overloaded? WIP per person (canvas)
 *   rules      What should we change first? ranked, evidence-backed rules
 *   policy     What should we change first? calibrated simulation + A/B
 */

import { Timeline, ACTIVITY_ORDER, ACTIVITY_COLOR, PHASE, PHASE_ORDER } from '/timeline.js';
import { BlockingMap, LENSES, blockingText } from '/graph.js';
import { QueueChart } from '/queues.js';
import { legendMarkup, responseTable } from '/queues.js';
import {
  DAY, fmtAge, fmtBucket, fmtCalendarDuration, fmtCalendarHours, fmtCompact, fmtCount,
  fmtDay, fmtDaySpan, fmtHours, fmtRange, fmtStamp, esc, sprintLabel, whoLabel,
} from '/format.js';
import {
  TABS, tabCounts, tabRows, deliveryTable, overviewMarkup, overviewCounts,
  detailMarkup, impactSentence, entityMarkdown, deliveryMarkdown, reviewStateLine,
  matchesScope, waitingLabel, weekLabel, scopeTargets, doneOpenMarkup,
  scopedDeliveryRows, approvalsLabel,
} from '/tables.js';
import {
  agingCount, agingMarkdown, agingMarkup, agingRows, parseAgingView,
  scopedAgingPayload,
} from '/aging.js';
import { rulesMarkup, rulesStats, rulesMarkdown } from '/rules.js';
import { verdictCard, verdictMarkdown } from '/verdict.js';
import { sprintStrip } from '/sprints.js';
import { ticketMarkdown, ticketMarkup, ticketStats } from '/ticket.js';
import { trendMarkdown, trendMarkup, trendStats } from '/trend.js';
import {
  defaultScenario, isBaselineScenario, policyMarkdown, policyMarkup, predHours,
  PRESETS, readScenario, scenarioQuery,
} from '/policy.js';
import { annotateRow, epicKeySet, keyListsOf, rowTicket, ticketFromQuery, ticketKey } from '/tickets.js';
/* prd/80: titles arrive from Jira via the operator's browser and never from
 * this server. The pure vocabulary, the cache and the transports are imported
 * here; the hydrator runs at the end of every view render. */
import {
  TITLE_TTL_DAYS, ageDays, coverage as titleCoverage, formatAge, normalizeKey,
  readTitlesConfig, readTitlesPref, storeTitlesConfig, storeTitlesPref,
  titlesEnabled,
} from '/titles.js';
import { getTitles, mergeTitles, openCache } from '/titles-cache.js';
import {
  annotateDetailTitle, hydrateTitles, listenTitlesMessage, titlesMarkup,
  wireTitlePopover, wireTitlesPanel,
} from '/titles-panel.js';
import { bookmarkletCode } from '/titles-probe.js';
import { versionLabel, versionTitle } from '/version.js';
import {
  DEFAULT_SIZE_CAPS, capPercent, capsQuery, flagCounts, normalizeSizeCaps,
  rememberSizeCaps, sizeCapsForTenant,
} from '/prsize.js';
import {
  ROLE_QUEUES, drawerTimelineScope, filteredPages, queueLevelForNode, repoLabel,
  scopeBanner, scopeForNode, scopeForStream,
} from '/scope.js';
import {
  bindLongPress, hintText, initMobile, isPhone, phoneEntryLanes, phoneEntryPage,
} from '/mobile.js';
import { pageTitle, parseRoute, SCOPE_KINDS, serializeRoute, ticketFromHash } from '/route.js';
import { createHistory, planSync, routeKey } from '/history.js';
import { collapseYah, yahPath } from '/yah.js';
import {
  DEFAULT_THEME, applyTheme, color, normalizeTheme, otherTheme, readTheme,
  refreshPalette, storeTheme, themeGlyph, themeLabel,
} from '/theme.js';

const $ = (id) => document.getElementById(id);
const LS_KEY = 'megaspeed.ui.v1';
/* prd/64 F-09: the view a bare link — and an unusable `?view=` — lands on. */
const DEFAULT_ENTRY_PAGE = 'overview';
/* prd/64 F-04: the Overview's headline tiles query their own canonical levels,
 * so the Queue/Blocking toggles cannot mutate another page's numbers. The
 * measured wait is the role-level handover graph ("each handover once"); the
 * overloaded/diagnosis tile is the person-level chart, because "overloaded"
 * is a person aggregate (docs/concepts.md). Each tile names that basis. */
const OVERVIEW_QUEUE_LEVEL = 'person';
const OVERVIEW_BLOCKING_LEVEL = 'queue';

const PAGES = {
  overview: { title: 'Overview', question: 'What needs attention?' },
  delivery: { title: 'Delivery list', question: 'What needs attention?' },
  timeline: { title: 'Timeline', question: 'Where does the time go?' },
  ticket: { title: 'Ticket', question: 'Why did this ticket take this long?' },
  blocking: { title: 'Blocking map', question: 'Who is blocking whom?' },
  queue: { title: 'Queue', question: 'Who is overloaded, and who is merely slow?' },
  trend: { title: 'Trend', question: 'Is it getting better?' },
  rules: { title: 'Rules', question: 'What should we change first?' },
  policy: { title: 'Policy lab', question: 'What should we change first?' },
  sources: { title: 'Sources', question: 'What is this screen made of?' },
  // prd/80: the importer and the probe bench. A real view: the sidebar's
  // Workspace → Titles, not a console workflow.
  titles: { title: 'Titles', question: 'What was this work?' },
};

const TIMEFRAMES = {
  '1w': { days: 7, label: 'the last week' },
  '4w': { days: 28, label: 'the last four weeks' },
  '1q': { days: 90, label: 'the last quarter' },
  all: { days: null, label: 'the full window' },
};

const state = {
  meta: null,
  // prd/32: the server's build identity (`/api/meta`), fetched once at boot.
  build: null,
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
  // prd/38 #24: the nav's first item is the front door ("what needs attention?"),
  // so Overview is the default view; Timeline is one click away.
  page: 'overview',
  // prd/10 §3.1: dark is the default; light is the stored (or linked) choice.
  theme: DEFAULT_THEME,
  scope: { kind: 'all', value: null, label: 'everything' },
  timeframe: '1q',
  // prd/39: the PR-size caps are a per-tenant setting (default 10 / 400). The
  // map keeps every tenant's choice; `sizeCaps` is the active tenant's.
  sizeCaps: { ...DEFAULT_SIZE_CAPS },
  sizeCapsMap: {},
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
  // prd/59: the aging view on the Delivery list. `agingView` encodes the kind
  // and stage (`story:cycle`, `pr:review`, `story:in_review`), and the two
  // controls are per-user preferences, like the benchmark toggle.
  aging: null,
  agingView: 'story:cycle',
  // prd/67 W-03: an honest empty list beats hundreds of abandoned rows, so the
  // tab opens on `only=live`; the toggle still shows the all-time tail.
  agingOnly: true,
  agingSource: 'self',
  queues: null,
  blocking: null,
  // prd/64 F-04: the Overview's own payloads, always fetched at the canonical
  // levels above. The Queue/Blocking pages keep `queues` / `blocking` at the
  // level their own toggle selects.
  overviewQueues: null,
  overviewBlocking: null,
  // prd/26 P6: the sprint lens (`/api/sprints`): bands, scope, carry-over.
  sprints: null,
  // prd/60: the forecast card (`/api/forecast?target=sprint:<id>`), the sprint
  // it describes, and the user's inline target override.
  forecast: null,
  forecastSprint: null,
  forecastTarget: null,
  // prd/50: the trend view (`/api/trend`) and the metric/bucket being shown.
  trend: null,
  // prd/63 F1: a failed trend fetch is an error state in the body, never a
  // spinner. `trendError` is the message; cleared by a successful fetch.
  trendError: null,
  trendMetric: 'flow_efficiency',
  trendBucket: 'week',
  // prd/61: the "Did it work?" panel's selected split and metric, plus the
  // compare payload (item-level before/after) for that split.
  compare: null,
  compareSplit: '',
  compareMetric: 'story_cycle',
  // prd/57: the benchmark contrast is on by default and has a per-view toggle.
  benchmark: true,
  // prd/37 §4: the selected sprint (id) — filters the story lanes, marks the
  // band and rides in the URL so the lens is shareable.
  sprint: null,
  rules: null,
  rulesLevel: 'team',
  // prd/58: the constraint verdict (Overview card + Rules banner) and the
  // persisted "show all rules" toggle that restores the full list.
  verdict: null,
  showAllRules: false,
  // prd/39: the two PR-size distributions the Rules panel draws.
  prSize: null,
  policy: null,
  policyScenario: defaultScenario(),
  // prd/63 F21: a scenario run has a visible loading state (the model takes a
  // moment; the page used to look frozen).
  policyLoading: false,
  blockingLevel: 'queue',
  queueLevel: 'person',
  lens: 'hours',
  cache: new Map(),
  selection: null,
  drawerEntity: null,
  // prd/25: the ticket detail page. `ticket` is the last payload, `ticketKey`
  // the key it describes (so a late response for another key is dropped), and
  // `ticketFrom` is where the back chip returns to.
  ticket: null,
  ticketKey: null,
  ticketError: null,
  ticketFrom: 'overview',
  // prd/38 #18: one snapshot age string per render, so the provenance line and
  // the Sources row cannot say "9 h ago" and "10 h ago" on the same page.
  snapshotAge: null,
  emptyReason: '',
  // prd/63 F14: the one-line notice for a silent fallback (unknown tenant,
  // view, timeframe or scope), cleared on the next real navigation.
  notice: '',
  // prd/80: titles resolved in the browser. `titles` is the hydrated cache
  // (Map key -> {summary, type, fetched_at}); `titlesStore` is the IndexedDB
  // handle (opened lazily); the token is MEMORY ONLY — never localStorage, a
  // URL or a cookie we set (rule 7). `titlesSeq` lets a pending fetch know it
  // was superseded by the off switch.
  titles: new Map(),
  titlesStore: null,
  titlesTenant: null,
  titlesEnabled: true,
  titlesSite: '',
  titlesCloudId: '',
  titlesToken: '',
  titlesExpiry: 0,
  titlesSeq: 0,
};

/* prd/48: the in-app entry stack (see history.js). Every distinct view state
 * is one entry, so the browser Back button and the top-bar Back button both
 * walk the app's own steps. `restoringRoute` marks the popstate/hashchange/boot
 * path: the browser is the one that moved, so nothing may be pushed. */
const nav = createHistory();
let restoringRoute = false;

const timeline = new Timeline({
  canvas: $('timeline'),
  // prd/33 §3: the lane list is virtualised — the canvas is one viewport tall
  // and this container scrolls a spacer of the full lane height.
  scroller: $('timeline-scroll'),
  spacer: $('lane-spacer'),
  tip: $('tip'),
  onStats: (stats) => {
    // B6: the top bar follows the timeline's real window, and says "custom"
    // when a brush or a zoom has taken it away from the active timeframe.
    noteTimelineView(stats);
    if (state.page === 'timeline') {
      const shown = stats.total ? dataWindow(stats.start, stats.end) : null;
      $('range').textContent = shown ? fmtRange(shown.start, shown.end) : 'no events';
      syncRangeMeta();
    }
    // B7: the timeline's disclosure describes the timeline only. Every other
    // view writes its own line in render*Page(); an async repaint of a hidden
    // canvas must not overwrite it.
    if (state.page !== 'timeline') return;
    const bits = [];
    if (stats.dim === 'story') {
      bits.push(stats.nested
        ? `${stats.storyLanes} stories · ${stats.prLanes} PRs nested beneath them`
        : `${stats.storyLanes} stories · PRs collapsed — zoom in to nest them`);
    }
    if (stats.aggregated) bits.push(`aggregated into ${fmtBucket(stats.bucketMs)} buckets — zoom in for individual events`);
    if (stats.openPrs) bits.push(`${stats.openPrs} PRs still open are right-censored at the snapshot time`);
    bits.push(`${stats.visible} of ${stats.total} events in view`);
    // prd/56 §6: the canvas is a wall-clock gantt; the header's clock is
    // business hours. Say which is which instead of showing "calendar days"
    // lifecycles under a "business hours" chip.
    bits.push('canvas spans are calendar time (wall clock); table durations are business hours (1 d = 8 h)');
    if (state.eventCapped) {
      bits.push(`only the newest ${fmtCompact(state.events.length)} of ${fmtCompact(state.eventTotal)} events are loaded`);
    }
    setHonesty(bits.join(' · '));
    renderLoadAll();
    updateTimelineEmpty(stats);
  },
  onSelect: (pr) => openPrDrawer(pr),
  onTicket: (key) => openTicket(key),
  onSelection: (range) => renderSelection(range),
  // prd/26 P6: a band is a doorway to the sprint's own window
  onSprint: (sprint) => scopeToSprint(sprint),
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

/* prd/38 #18: the snapshot age is computed once per store version and reused.
 * Two calls minutes apart used to round "9 h 50 min" and "10 h 5 min" to 9 h and
 * 10 h on the same page; one string cannot disagree with itself. */
function snapshotAge() {
  const meta = state.meta;
  if (!meta || !meta.as_of) return 'unknown age';
  if (!state.snapshotAge) state.snapshotAge = ageOf(meta.as_of);
  return state.snapshotAge;
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

/**
 * prd/49: a repo scope is the one scope the API applies, because the Rules page
 * and the policy lab recompute on that repo's cohort (person/role/ticket scopes
 * filter the returned rows client-side). Empty for every other scope.
 */
function repoQuery() {
  if (state.scope.kind !== 'repo' || !state.scope.value) return '';
  return `repo=${encodeURIComponent(state.scope.value)}`;
}

/** The timeframe + repo filters as one query fragment (may be empty). */
function scopedQuery() {
  return [timeframeQuery(), repoQuery()].filter(Boolean).join('&');
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

/**
 * prd/70 F-12: the window `#range` prints, clamped to the data.
 *
 * `timeline.fit()` pads the domain by 1.5 % on each side, so `All` ended 22 days
 * in the future ("28 Sep → 14 Dec") while every other page said "→ 22 Nov". The
 * range is a statement about the data, so it never claims time the store does
 * not hold; a custom window inside the data is untouched.
 */
function dataWindow(start, end) {
  const meta = state.meta || {};
  const first = meta.first_ts ? Date.parse(meta.first_ts) : null;
  const last = meta.last_ts
    ? Date.parse(meta.last_ts)
    : (meta.as_of ? Date.parse(meta.as_of) : null);
  if (!(start < end)) return { start, end };
  return {
    start: first && start < first ? first : start,
    end: last && end > last ? last : end,
  };
}

/* B6: the timeframe control is the top-bar contract for "when". A preset shows
 * pressed; a brush or a zoom shows a `custom` chip instead of leaving a preset
 * pressed over a different window. `#range` is the timeline's window and is
 * never painted onto another view: those views show the preset window they
 * really query. */
function renderTimeframe() {
  // prd/64 F-10: a sprint lens (or a brush/zoom) is a *timeline* window. On
  // every other view the preset is what the data queries, so the preset stays
  // pressed; only the timeline can show a custom chip.
  const custom = state.page === 'timeline' && state.timeframeCustom;
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
    if (view) {
      const shown = dataWindow(view.start, view.end);
      $('range').textContent = fmtRange(shown.start, shown.end);
    }
  }
  syncRangeMeta();
}

/** prd/28: the phone hides the top bar's range pill; the meta line carries it,
 * so the answer to "when am I looking at" is never lost with the space. */
function syncRangeMeta() {
  const meta = $('range-meta');
  if (!meta) return;
  const phone = isPhone();
  meta.hidden = !phone;
  if (phone) meta.textContent = $('range').textContent;
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
  const yah = $('yah');
  // prd/48: the YAH strip is sticky chrome on a desktop; on a phone it scrolls
  // with the page, so it must not push the sticky table headers down.
  const yahH = yah && !yah.hidden && !isPhone() ? yah.offsetHeight : 0;
  document.documentElement.style.setProperty('--yah-h', `${yahH}px`);
  const bottom = 40 + yahH + meta.offsetHeight + 2;
  document.documentElement.style.setProperty('--meta-bottom', `${bottom}px`);
}

let statusTimer = 0;
/* prd/65 N-04: a write of ours bumps the store stamp, so the `/ws` watcher
 * broadcasts `changed` ~1 s later and the reload that follows would replace
 * "recorded review SLA 4 h" with the live line almost immediately. The
 * confirmation is held for its normal flash duration: `refreshStatus()` (the
 * liveness update every reload ends with) leaves the bar alone while it is
 * fresh. Only the status *text* is held — the echo still reloads the data, and
 * another writer's change still refreshes the view; it simply cannot talk over
 * a confirmation the user just earned. */
let statusHeldUntil = 0;

/** Hold a confirmation on the status bar for `ms`, then return to live. */
function flashStatusHeld(text, cls = 'ok', ms = 4000) {
  flashStatus(text, cls, ms);
  // after flashStatus: `setStatus` clears the hold, so arm it last.
  statusHeldUntil = Date.now() + ms;
}

/** The liveness refresh a successful load ends with (respects a held flash). */
function refreshStatus() {
  if (Date.now() < statusHeldUntil) return;
  setStatus(liveStatusText(), 'live');
}

function liveStatusText() {
  if (!state.meta) return 'connecting';
  // prd/28: the phone's top bar is two rows of context; the counts live in the
  // provenance line, so the status is just the liveness dot and its word.
  if (isPhone()) return 'live';
  // prd/70 F-08: `state.events` is the *loaded* slice (newest 40,000), while
  // the provenance line reports the store's full event count. Saying
  // "40000 events" here beside "76213 events" there was two answers to one
  // question; name what is loaded and what the store holds.
  const total = Math.max(
    Number(state.eventTotal) || 0,
    Number((state.meta && state.meta.events) || 0),
  );
  const loaded = state.events.length;
  const counts = total && loaded < total
    ? `${fmtCount(loaded)} of ${fmtCount(total)} events loaded`
    : `${fmtCount(total || loaded)} events`;
  return `live · ${counts}${state.tenant ? ` · ${state.tenant}` : ''}`;
}

function setStatus(text, cls = '') {
  clearTimeout(statusTimer);
  // prd/65 N-04: any status the app says on purpose (a load, an error, another
  // action) ends a held confirmation; only the reload's liveness refresh goes
  // through `refreshStatus()`, and that one respects the hold.
  statusHeldUntil = 0;
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

// -------------------------------------------------------------------- route
/* P-2 (docs/papercuts.md): the address bar is the view state. One helper builds
 * the href and one writes it, so every navigation path — tenant switch, view
 * change, ticket, scope, timeframe — updates the same string. `#ticket/KEY`
 * stays the ticket route; the query carries what used to be session-local. */
function routeFields() {
  // prd/42 P10: the sprint lens is a timeline filter. A Queue/Blocking/Rules
  // URL must not keep `sprint=2&from=&to=` describing a view that ignores it.
  // prd/63 F30: the Delivery list honours it too (the rows are filtered to the
  // sprint's stories), so a `sprint=` deep link there is no longer decorative.
  const usesSprint = state.page === 'timeline' || state.page === 'delivery';
  const sprint = usesSprint ? sprintRange() : { start: null, end: null };
  const usesTrend = state.page === 'trend';
  return {
    tenant: state.tenant,
    // A ticket page without its key is a transient boot state, not a place to
    // link: omit the view rather than encode an empty ticket page.
    view: state.page === 'ticket' && !state.ticketKey ? null : state.page,
    ticket: state.page === 'ticket' ? state.ticketKey : null,
    timeframe: state.timeframe,
    scope: state.scope,
    // prd/59: the Delivery subset is route state, so `?tab=aging` is a link.
    tab: state.page === 'delivery' ? state.tab : null,
    // prd/37 §4: a selected sprint travels with its measured range, so the lens
    // (filter + band + window) is shareable, not just the window.
    sprint: usesSprint ? state.sprint : null,
    from: Number.isFinite(sprint.start) ? sprint.start : null,
    to: Number.isFinite(sprint.end) ? sprint.end : null,
    // prd/63 F30: the Trend metric + bucket; prd/63 F21: the lab scenario.
    trendMetric: usesTrend ? state.trendMetric : null,
    trendBucket: usesTrend ? state.trendBucket : null,
    policy: state.page === 'policy' && !isBaselineScenario(state.policyScenario)
      ? state.policyScenario : null,
  };
}

function routeHref() {
  return serializeRoute({
    ...routeFields(),
    // dark is the default, so only a light view puts anything in the URL: an
    // existing dark link is byte-identical to what it was before themes.
    theme: state.theme === 'light' ? 'light' : null,
    // prd/80: titles are on by default too, so only the screen-share state
    // (`?titles=off`) costs characters — and a shared link opens hidden.
    titles: state.titlesEnabled ? null : 'off',
  });
}

/** The canonical URL of the state on screen, including the path. */
function locationHref() {
  return `${location.pathname}${location.search}${location.hash}`;
}

/* prd/48: the address bar records history instead of overwriting it. Every
 * distinct view state — view, tenant, scope, timeframe, sprint lens — is a
 * `pushState` entry, so Back walks the app instead of leaving it. A restore
 * (browser Back/Forward, a pasted hash, boot) never pushes: the browser moved,
 * and `planSync` rewrites the entry instead. `replaceState` stays for the
 * refinements of the entry the user is already on (a sprint's measured
 * `from`/`to`, the theme, the canonical spelling of a link). */
function syncUrl() {
  if (typeof history === 'undefined' || typeof location === 'undefined') return;
  const plan = planSync(nav, {
    href: `${location.pathname}${routeHref()}`,
    current: locationHref(),
    key: routeKey(routeFields()),
    restoring: restoringRoute,
  });
  if (plan.action === 'push') history.pushState({ ms: plan.index }, '', plan.href);
  else if (plan.action === 'replace') history.replaceState({ ms: plan.index }, '', plan.href);
  renderBack();
}

/* prd/48: the first paint is step 0, not a navigation. Seeding the stack with
 * the URL the browser already has keeps the in-app Back disabled until the user
 * actually moves — a fresh deep link lands with nothing to pop. */
function seedHistory() {
  if (typeof history === 'undefined' || typeof location === 'undefined') return;
  const href = locationHref();
  nav.seed(href, routeKey(routeFields()));
  history.replaceState({ ms: 0 }, '', href);
  renderBack();
}

/* prd/48: the top-bar Back button. It is disabled at the first entry — the one
 * place the app can safely say "there is nothing behind you here". */
function renderBack() {
  const button = $('history-back');
  if (!button) return;
  const can = nav.canBack();
  const steps = Math.max(0, nav.index);
  button.disabled = !can;
  button.setAttribute('aria-disabled', can ? 'false' : 'true');
  button.title = can
    ? `back — ${steps} step${steps === 1 ? '' : 's'} back in this app`
    : 'back — this is the first step in this app';
  button.setAttribute('aria-label', button.title);
}

/* One in-app step back. The browser owns the entries, so this walks the real
 * history and `popstate` restores the previous state through `applyRoute()` —
 * the same path the browser's own Back button takes. */
function inAppBack() {
  if (!nav.canBack()) return;
  history.back();
}

/* prd/48: the browser moved to one of our entries. Trust its index (tagged as
 * `{ ms }` on every push/replace); an entry we cannot place becomes the first
 * one, so the stack never claims a step it did not record. */
function restoreFromLocation(event) {
  if (typeof location === 'undefined') return;
  const entryState = event && event.state;
  const index = entryState && Number.isFinite(entryState.ms) ? entryState.ms : -1;
  const route = parseRoute(location.search, location.hash);
  nav.restore(index, locationHref(), routeKey(route));
  renderBack();
}

/* P-1: the tab title names the tenant and the view, never the static title. */
function syncTitle() {
  if (typeof document === 'undefined') return;
  const page = PAGES[state.page];
  document.title = pageTitle({
    tenant: state.tenant,
    view: state.page,
    ticket: state.page === 'ticket' ? state.ticketKey : null,
    title: page ? page.title : null,
  });
}

// ---------------------------------------------------------------------- YAH
/* prd/48: the YAH bar is *location* — `<tenant> · <view> · <ticket> · <PR>` —
 * beside the Back button's *history*. On a ticket page the view level is the
 * view it was opened from, so the path is also the way back to that list. */
function drawerPr() {
  const entity = state.drawerEntity;
  if (!entity) return null;
  const pr = entity.pr || entity.row || entity;
  if (!pr || pr.number == null) return null;
  return { number: pr.number, repo: pr.repo || pr.repo_short || null };
}

function yahSteps() {
  const onTicket = state.page === 'ticket' && Boolean(state.ticketKey);
  const view = onTicket ? (state.ticketFrom || 'overview') : state.page;
  return yahPath({
    tenant: state.tenant,
    view,
    viewLabel: PAGES[view] ? PAGES[view].title : view,
    ticket: onTicket ? state.ticketKey : null,
    pr: drawerPr(),
    timeframe: state.timeframe,
    scope: state.scope,
    sprint: state.page === 'timeline' ? state.sprint : null,
  });
}

function renderYah() {
  const bar = $('yah');
  const path = $('yah-path');
  if (!bar || !path) return;
  const steps = yahSteps();
  const shown = isPhone() ? collapseYah(steps) : steps;
  bar.hidden = shown.length === 0;
  path.innerHTML = shown.map((step, index) => {
    const label = `<span class="yah-label">${esc(step.label)}</span>`;
    const sep = index ? '<span class="yah-sep" aria-hidden="true">·</span>' : '';
    if (index === shown.length - 1) {
      return `${sep}<span class="yah-here" aria-current="location" title="${esc(step.title)}">${label}</span>`;
    }
    return `${sep}<button type="button" class="yah-step" data-yah="${esc(step.level)}"`
      + ` data-yah-href="${esc(step.href)}" title="${esc(step.title)}">${label}</button>`;
  }).join('');
  syncChromeHeight();
}

/* A jump up the path is a new step (never Back): the history keeps both where
 * the user was and where they went. */
async function yahJump(level) {
  closeDrawer();
  if (level === 'tenant') {
    await showPage('overview');
    return;
  }
  if (level === 'view') {
    const view = state.page === 'ticket' ? (state.ticketFrom || 'overview') : state.page;
    await showPage(view);
    return;
  }
  if (level === 'ticket' && state.ticketKey) {
    await openTicket(state.ticketKey);
  }
}

// -------------------------------------------------------------------- views
/** The one honesty line (B7): each view states its own cohort, or clears it. */
function setHonesty(text) {
  // prd/42 P7: the top bar already ends with the clock basis ("business hours"
  // / "wall-clock hours"), so a disclosure that repeats it produced
  // "· business hours · · business hours". Strip the duplicate suffix; the
  // canonical basis stays in #timebasis.
  let value = text || '';
  value = value.replace(/\s*·\s*(business|wall-clock) hours\s*$/i, '');
  $('honesty').textContent = value;
  // prd/63 F29: an empty honesty line left its separator behind ("· ·
  // business hours"). Hide the separator with the line.
  const dot = $('dot-honesty');
  if (dot) dot.hidden = !value;
}

/* prd/63 F14: one line for every silent fallback (tenant, view, timeframe,
 * scope). A notice is not an error page: the view loaded, but it says what was
 * substituted. `setNotice('')` clears it. */
function setNotice(text, { append = false } = {}) {
  const next = text || '';
  if (!next) state.notice = '';
  else if (append && state.notice && !state.notice.includes(next)) {
    state.notice = `${state.notice} · ${next}`;
  } else {
    state.notice = next;
  }
  const el = $('route-notice');
  if (!el) return;
  el.hidden = !state.notice;
  el.textContent = state.notice;
}

/** B3: the banner is derived from the filters the views really apply. */
function renderScopeBanner() {
  const banner = $('scopebanner');
  const text = scopeBanner(state.scope, state.page);
  banner.hidden = !text;
  banner.textContent = text;
  // prd/63 F15: the selector stays usable (so the scope can be cleared from
  // here), but on a view that ignores it, it says so instead of looking active.
  const control = $('scope');
  if (!control) return;
  const applies = state.scope.kind === 'all'
    || filteredPages(state.scope).has(state.page);
  control.classList.toggle('scope-inert', !applies);
  control.title = applies || state.scope.kind === 'all'
    ? 'scope the views by person, queue, ticket or repo'
    : `this view is not filterable by scope ${state.scope.label} — it still shows the whole team`;
}

/* prd/28 §2: the hint line names the gesture the device actually has. A phone
 * has no wheel, no arrow keys and no right-click, so it must not be told to
 * use them; a desktop keeps the modifiers it already knows. */
function renderHints() {
  const phone = isPhone();
  const timelineHint = $('timeline-hint');
  if (timelineHint) timelineHint.textContent = hintText('timeline', phone);
  const deliveryHint = $('delivery-hint');
  if (deliveryHint) deliveryHint.textContent = hintText('delivery', phone);
  const blockingHint = $('blocking-hint');
  if (blockingHint) blockingHint.textContent = hintText('blocking', phone);
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
  renderHints();
  $('page-title').textContent = page === 'ticket' && state.ticketKey
    ? state.ticketKey
    : PAGES[page].title;
  $('page-question').textContent = PAGES[page].question;
  // P-1/P-2: the tab and the address bar follow every render, so no navigation
  // path (tenant switch, scope, timeframe, drawer) can leave either one
  // describing the previous view. prd/48: the YAH strip follows too.
  syncTitle();
  syncUrl();
  renderYah();
  syncLevelSelects();
  renderBenchmarkToggles();
  renderScopeBanner();
  setHonesty('');
  renderTimeframe();
  if (page === 'timeline') renderTimelinePage();
  if (page === 'ticket') renderTicketPage();
  if (page === 'delivery') renderDeliveryPage();
  if (page === 'blocking') renderBlockingPage();
  if (page === 'queue') renderQueuePage();
  if (page === 'trend') renderTrendPage();
  if (page === 'rules') renderRulesPage();
  if (page === 'policy') renderPolicyPage();
  if (page === 'overview') renderOverviewPage();
  if (page === 'sources') renderSourcesPage();
  if (page === 'titles') renderTitlesPage();
  // prd/80: the render tail. One explicit hydration call per view render (not a
  // MutationObserver), after every view's markup exists and before the
  // preference is saved.
  renderTitlesChrome();
  saveUI();
}

async function showPage(page) {
  if (!PAGES[page]) return;
  // prd/38 #19: a PR drawer belongs to the view that opened it. Any view change
  // closes it (desktop and phone), so it can never float over a different view.
  if (page !== state.page) {
    closeDrawer();
    // prd/63 F14: a fallback notice describes the link that landed, not every
    // later view; an explicit navigation clears it.
    setNotice('');
  }
  // Leaving the ticket route needs no special case: renderPage() re-serialises
  // the URL from `state.page`, so the ticket hash (and `view=ticket`) drop off
  // together with the page (P-2).
  state.page = page;
  renderPage();
  try {
    await ensureData(page);
  } catch (err) {
    setStatus(`view failed: ${err.message}`, 'stale');
    // prd/63 F1: a failed fetch is a stated result in the body too. The status
    // pill is one line at the top; the chart area used to keep saying
    // "Loading the trend…" forever. Only Trend has this failure mode (every
    // other view's payload is cached or renders an empty state).
    if (page === 'trend') {
      state.trend = null;
      state.trendError = err && err.message ? err.message : 'the request failed';
      if (state.page === 'trend') renderPage();
    }
    return;
  }
  if (page === 'trend') state.trendError = null;
  renderPage();
}

async function ensureData(page) {
  const q = scopedQuery();
  const repo = repoQuery();
  const stamp = state.meta ? state.meta.as_of : 'none';
  const tenant = state.tenant || 'none';
  if (page === 'delivery' || page === 'overview') {
    const key = `delivery:${tenant}:${state.timeframe}:${repo}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(withTenant(`/api/delivery?${q}`)));
    state.delivery = cached(key);
  }
  if (page === 'delivery') {
    // prd/59: one request per (kind, stage, only, threshold source), cached like
    // the delivery list; the tab count and the table read the same payload.
    const view = parseAgingView(state.agingView);
    const agingKey = `aging:${tenant}:${state.timeframe}:${repo}:${view.kind}:${view.stage}:${
      state.agingOnly ? 'live' : 'all'}:${state.agingSource}:${stamp}`;
    if (!cached(agingKey)) {
      state.cache.set(agingKey, await getJSON(withTenant(
        `/api/aging?kind=${encodeURIComponent(view.kind)}` +
        `&stage=${encodeURIComponent(view.stage)}` +
        `&only=${state.agingOnly ? 'live' : 'all'}` +
        `&thresholds=${encodeURIComponent(state.agingSource)}&${q}`,
      )));
    }
    state.aging = cached(agingKey);
  }
  if (page === 'blocking') {
    const key = `blocking:${tenant}:${state.timeframe}:${state.blockingLevel}:${repo}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(withTenant(`/api/blocking?level=${state.blockingLevel}&${q}`)));
    state.blocking = cached(key);
  }
  if (page === 'queue') {
    const key = `queues:${tenant}:${state.timeframe}:${state.queueLevel}:${repo}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(withTenant(`/api/queues?level=${state.queueLevel}&${q}`)));
    state.queues = cached(key);
  }
  if (page === 'overview') {
    // prd/64 F-04: Overview never reads the levels the Queue/Blocking pages
    // happen to have selected. It asks its own canonical questions — the
    // role-level handover graph for the measured wait, the person-level chart
    // for "overloaded" — and its labels quote those payloads' basis.
    const blockingKey = `blocking:${tenant}:${state.timeframe}:${OVERVIEW_BLOCKING_LEVEL}:${repo}:${stamp}`;
    if (!cached(blockingKey)) {
      state.cache.set(blockingKey, await getJSON(withTenant(
        `/api/blocking?level=${OVERVIEW_BLOCKING_LEVEL}&${q}`,
      )));
    }
    state.overviewBlocking = cached(blockingKey);
    const queueKey = `queues:${tenant}:${state.timeframe}:${OVERVIEW_QUEUE_LEVEL}:${repo}:${stamp}`;
    if (!cached(queueKey)) {
      state.cache.set(queueKey, await getJSON(withTenant(
        `/api/queues?level=${OVERVIEW_QUEUE_LEVEL}&${q}`,
      )));
    }
    state.overviewQueues = cached(queueKey);
  }
  if (page === 'trend') {
    // prd/50: one endpoint per metric+bucket; the cache key names both, so
    // switching a tab is one cheap request and going back is free.
    // prd/63 A1: the repo scope is part of the answer, so it is part of the
    // cache key too — a repo switch must not serve the previous cohort's trend.
    const key = `trend:${tenant}:${state.timeframe}:${state.trendMetric}:${state.trendBucket}:${repo}:${stamp}`;
    if (!cached(key)) {
      state.cache.set(key, await getJSON(withTenant(
        `/api/trend?metric=${encodeURIComponent(state.trendMetric)}` +
        `&bucket=${encodeURIComponent(state.trendBucket)}&${q}`,
      )));
    }
    state.trend = cached(key);
    // prd/61: one compare per (metric, split). The default split is the latest
    // in-window policy change; a click on a marker moves it. Cached by the same
    // (tenant, timeframe, bucket, repo, snapshot) key as the trend.
    const entries = (state.trend && state.trend.annotations) || [];
    if (!entries.length) {
      state.compare = null;
    } else {
      const policy = entries.filter((entry) => entry.policy !== false && entry.in_window !== false);
      const split = entries.some((entry) => entry.date === state.compareSplit)
        ? state.compareSplit
        : (policy.length ? policy[policy.length - 1].date : entries[entries.length - 1].date);
      state.compareSplit = split;
      const ckey = `compare:${tenant}:${state.timeframe}:${state.trendBucket}:${
        state.compareMetric}:${split}:${repo}:${stamp}`;
      if (!cached(ckey)) {
        state.cache.set(ckey, await getJSON(withTenant(
          `/api/trend/compare?metric=${encodeURIComponent(state.compareMetric)}` +
          `&split=${encodeURIComponent(split)}` +
          `&bucket=${encodeURIComponent(state.trendBucket)}&${q}`,
        )));
      }
      state.compare = cached(ckey);
    }
  }
  if (page === 'overview' || page === 'rules') {
    // prd/58: one verdict per (tenant, window, repo); the Rules banner and the
    // Overview card are the same answer, never a second computation.
    const key = `verdict:${tenant}:${state.timeframe}:${repo}:${stamp}`;
    if (!cached(key)) state.cache.set(key, await getJSON(withTenant(`/api/verdict?${q}`)));
    state.verdict = cached(key);
  }
  if (page === 'rules') {
    // prd/39: the distributions are cap-independent; the size *rule* is not, so
    // the insights key carries the chosen caps (a slider move is a new answer).
    const sizeKey = `prsize:${tenant}:${stamp}`;
    if (!cached(sizeKey)) {
      state.cache.set(sizeKey, await getJSON(withTenant('/api/pr-size')));
    }
    state.prSize = cached(sizeKey);
    const caps = capsQuery(state.sizeCaps);
    const key = `rules:${tenant}:${state.timeframe}:${state.rulesLevel}:${caps}:${repo}:${stamp}`;
    if (!cached(key)) {
      state.cache.set(key, await getJSON(withTenant(
        `/api/insights?level=${state.rulesLevel}&${q}&${caps}`,
      )));
    }
    state.rules = cached(key);
  }
  if (page === 'timeline') {
    // prd/70 F-04: the forecast card is evaluated over the active window, so a
    // timeframe change is a new question to the same (server-cached) endpoint.
    await loadForecast(loadSeq);
  }
  if (page === 'ticket') {
    // prd/25: one ticket's cycle. The cache key includes the key, so two ticket
    // pages cannot serve each other's payload.
    if (!state.ticketKey) return;
    const key = `ticket:${tenant}:${state.ticketKey}:${stamp}`;
    if (!cached(key)) {
      state.cache.set(key, await getJSON(
        withTenant(`/api/ticket/${encodeURIComponent(state.ticketKey)}`),
      ));
    }
    state.ticket = cached(key);
    state.ticketError = null;
  }
  if (page === 'policy') {
    // prd/23: the scenario is part of the cache key — two knob settings are two
    // different predictions, and re-running the same one must not refetch.
    const scenario = scenarioQuery(state.policyScenario);
    const key = `policy:${tenant}:${state.timeframe}:${scenario}:${repo}:${stamp}`;
    if (!cached(key)) {
      const url = `/api/simulate?${q}${scenario ? `&${scenario}` : ''}`;
      state.cache.set(key, await getJSON(withTenant(url)));
    }
    state.policy = cached(key);
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
  const requested = state.tenant;
  if (!state.tenant || !names.has(state.tenant)) {
    // No stored choice, or it was deleted: fall back to the server's default,
    // which is the first tenant by name.
    state.tenant = data.default && names.has(data.default)
      ? data.default
      : (list[0] ? list[0].name : null);
    // prd/63 F14: a `?tenant=nope` link used to rewrite the URL and load a
    // different dataset without a word. Say which one is on screen instead.
    if (requested && requested !== state.tenant) {
      setNotice(
        `No dataset “${requested}” on this server — showing ${state.tenant || 'none'}. `
        + 'Datasets are the names under data/<name>.duckdb.',
        { append: true },
      );
    }
  }
  // prd/39: the active tenant's stored size calibration (default 10 / 400).
  state.sizeCaps = sizeCapsForTenant(state.sizeCapsMap, state.tenant);
  renderTenantSwitcher();
  return list;
}

function renderTenantSwitcher() {
  const tenant = currentTenant();
  $('tenant-name').textContent = state.tenant || 'none';
  $('tenant-meta').textContent = tenantSummary(tenant);
  $('tenant-button').classList.toggle('empty', !tenant);
  $('tenant-button').title = tenantTitle(tenant);
  // P-1: the tab names the dataset as soon as the switcher does.
  syncTitle();
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
  state.overviewBlocking = null;
  state.overviewQueues = null;
  state.sprints = null;
  state.forecast = null;
  state.forecastSprint = null;
  state.forecastTarget = null;
  state.sprint = null;
  state.rules = null;
  state.prSize = null;
  state.selection = null;
  state.drawerEntity = null;
  state.ticket = null;
  state.ticketKey = null;
  state.ticketError = null;
  state.snapshotAge = null;
  state.emptyReason = '';
  state.eventCapped = false;
  state.eventTotal = 0;
  state.eventLimit = EVENT_MAX;
  state.epicKeys = new Set();
  // prd/80: titles are per tenant; another dataset's cache must not paint here.
  state.titles = new Map();
  state.titlesTenant = null;
  state.cache.clear();
  $('load-all').hidden = true;
  // B12: a brush chip (and every hover/selection) belongs to the dataset it was
  // made in. Clear it with the data so "Set timeframe" cannot apply a window
  // from another tenant.
  timeline.clearSelection();
  timeline.setTicketIndex({ epicKeys: state.epicKeys });
  timeline.setData([], [], {});
  timeline.setSprints(null);
  timeline.setSprintFocus(null);
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
  // prd/64 F-09: a user edit is the end of the link that produced a fallback
  // notice. Clear it before the switch's own notice (if any) replaces it.
  if (!silent) setNotice('');
  if (name === state.tenant && state.meta) return;
  // prd/25: a ticket page survives a dataset switch — the key is looked up in
  // the new store, and its absence there is a stated 404, not a blank page.
  const reopen = state.page === 'ticket' ? state.ticketKey : null;
  // Scope is per tenant (prd/12): pseudonyms are independent identity spaces,
  // so a person scoped in one dataset means nothing in the next.
  if (state.tenant) state.scopes[state.tenant] = state.scope;
  const previousScope = state.scope;
  state.tenant = name;
  state.scope = state.scopes[name] || { kind: 'all', value: null, label: 'everything' };
  // prd/63 F14: datasets are independent identity spaces, so a person/role
  // scope cannot follow the switch. Say that instead of resetting silently.
  if (previousScope.kind !== 'all' && state.scope.kind === 'all') {
    setNotice(
      `Dataset switch cleared the ${previousScope.kind} scope “${previousScope.label}” — `
      + 'pseudonyms are per dataset, so it cannot be carried across.',
    );
  }
  // prd/39: the size calibration is per tenant too (two datasets can need
  // different knees); the stored choice wins, the default is 10 / 400.
  state.sizeCaps = sizeCapsForTenant(state.sizeCapsMap, name);
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
  if (reopen) await openTicket(reopen);
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
    `${fmtRange(range.start, range.end)} · ${fmtCalendarDuration(range.end - range.start)}`;
  chip.dataset.start = String(range.start);
  chip.dataset.end = String(range.end);
}

function renderTimelinePage() {
  renderLegend();
  renderPhaseLegend();
  renderSprintStrip();
  const band = state.sprint != null ? sprintById(state.sprint) : null;
  if (band) {
    // prd/37 §4: the selected sprint filters the lanes and marks the band; its
    // measured range is the window *until the user pans/zooms inside it*.
    timeline.setSprintFocus(state.sprint);
    if (!state.timeframeCustom) {
      state.timeframeCustom = true;
      timeline.setWindow(Date.parse(band.band_start), Date.parse(band.band_end));
      renderTimeframe();
    }
  } else if (!state.timeframeCustom) {
    // Returning to the timeline with a preset active shows that preset again;
    // a custom brush/zoom window is the user's window and is left alone (B6).
    applyTimeframeToTimeline();
  }
  timeline.schedule();
}

/* prd/26 P6: the sprint strip under the timeline — the sprint view without a
 * second page. Each row sets the timeframe to that sprint's measured band, so
 * "what was in sprint 2" is one click from the bands. */
function renderSprintStrip() {
  const host = $('sprint-strip');
  if (!host) return;
  const markup = state.sprints
    ? sprintStrip(state.sprints, {
      base: appBase(), focus: state.sprint,
      forecast: state.forecast, forecastTarget: state.forecastTarget,
    })
    : '';
  host.innerHTML = markup;
  host.hidden = !markup;
}

/* prd/37 §4: the sprint lens is a filter, not a time window. Selecting S
 * filters the story lanes to S's stories, marks S's band, sets the window to
 * its measured range and writes `?sprint=<id>&from=&to=` so the view is
 * shareable. Clearing restores every story lane. */
function sprintById(id) {
  if (id == null || !state.sprints) return null;
  return (state.sprints.sprints || [])
    .find((row) => String(row.sprint_id) === String(id)) || null;
}

/** The selected sprint's measured range, in ms (null when no sprint is chosen). */
function sprintRange() {
  const band = sprintById(state.sprint);
  if (!band) return { start: null, end: null };
  return { start: Date.parse(band.band_start), end: Date.parse(band.band_end) };
}

function focusSprint(id, { start = null, end = null, sync = true } = {}) {
  const sprint = sprintById(id);
  if (!sprint) return false;
  state.sprint = sprint.sprint_id;
  timeline.setSprintFocus(state.sprint);
  const from = Number.isFinite(start) ? start : Date.parse(sprint.band_start);
  const to = Number.isFinite(end) ? end : Date.parse(sprint.band_end);
  if (Number.isFinite(from) && Number.isFinite(to)) {
    timeline.setWindow(from, to);
    // The sprint window is not a preset: leave `tfWindow` pointing at the preset
    // so the top bar says "custom" (B6) instead of claiming the sprint is "1q".
    state.timeframeCustom = true;
  }
  renderTimeframe();
  renderSprintStrip();
  if (sync) syncUrl();
  saveUI();
  return true;
}

function clearSprintFocus({ sync = true } = {}) {
  if (state.sprint == null) return;
  state.sprint = null;
  timeline.setSprintFocus(null);
  // prd/64 F-13: focusing a sprint made the window custom. Clearing the lens
  // must restore the preset the top bar still names (1q), not leave the canvas
  // parked on the sprint's dates under a `custom` chip.
  state.timeframeCustom = false;
  applyTimeframeToTimeline();
  renderTimeframe();
  renderSprintStrip();
  if (sync) syncUrl();
  saveUI();
}

function scopeToSprint(sprint) {
  if (!sprint) return;
  const id = sprint.id != null ? sprint.id : sprint.sprint_id;
  // prd/63 F24: the band/row is a toggle — a second click clears the lens.
  if (state.sprint != null && String(state.sprint) === String(id)) {
    clearSprintFocus();
    return;
  }
  if (!focusSprint(id, { start: sprint.start, end: sprint.end })) return;
  flashStatus(`sprint ${sprintLabel(sprint)} lens · ${timelineStatsLine()}`, 'live');
}

function timelineStatsLine() {
  const band = sprintById(state.sprint);
  if (!band) return 'filtered';
  const unmerged = band.unmerged || 0;
  return `${band.done_by_end ?? band.done ?? 0} of ${band.story_count || 0} stories done by end` +
    (unmerged ? ` · ${unmerged} Done in Jira with an unmerged PR` : '');
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
      <span class="swatch" style="background:${ACTIVITY_COLOR[activity] || color('--cat-7')}"></span>
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
    // prd/35: the timeline's phases are elapsed wall-clock, so they say
    // "calendar" and never reuse the business-hour `d`. One shared formatter,
    // never an ad-hoc `${hours} h` (the conformance test enforces it).
    const value = phase === 'closed'
      ? (zero ? 'end cap' : fmtCalendarHours(entry.hours))
      : (zero ? '0' : fmtCalendarHours(entry.hours));
    const title = `${PHASE[phase].hint}${entry ? ` — ${fmtCalendarHours(entry.hours)} elapsed in this window` : ''}`;
    return `<span class="${zero ? 'zero' : ''}" title="${esc(title)}">
      ${swatch}${esc(PHASE[phase].label)}
      <span class="mono muted">${value}</span></span>`;
  }).join('');
}

// ----------------------------------------------------------------- delivery
/** prd/63 F30: the story keys of the selected sprint lens (empty = no lens). */
function sprintStoryKeys() {
  const band = sprintById(state.sprint);
  return new Set((band && band.stories) || []);
}

function rowInSprint(row, keys) {
  if (!keys || !keys.size) return true;
  return [...(row.tickets || []), ...(row.story_tickets || []), rowTicket(row)]
    .filter(Boolean)
    .some((key) => keys.has(String(key)));
}

function renderDeliveryPage() {
  const data = state.delivery;
  // prd/63 F30: a `?sprint=` deep link on Delivery keeps the pill pressed on
  // the Timeline but used to leave the rows unfiltered. Filter to the sprint's
  // stories, the same membership the timeline lens uses.
  const sprintKeys = state.page === 'delivery' ? sprintStoryKeys() : new Set();
  // prd/70 F-16: the lens is named by the same label every other surface uses
  // (`S352`, else `S4392`), never by a number the store did not parse.
  const lensLabel = sprintKeys.size
    ? sprintLabel(sprintById(state.sprint) || state.sprint)
    : null;
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
  const counts = scopedTabCounts(data, sprintKeys);
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
  const aging = state.tab === 'aging';
  // prd/65 N-05: the scope + sprint lens as one predicate — the tab badge, the
  // table, the chip, the dots, the honesty line and the re-plan list all read it.
  const agingFilter = agingRowFilter(sprintKeys);
  $('delivery-table').innerHTML = data
    ? (aging
      ? agingMarkup(state.aging, {
        base: appBase(),
        phone: isPhone(),
        onlyLive: state.agingOnly,
        view: state.agingView,
        source: state.agingSource,
        // the table follows the same scope + sprint lens as the tab count, so
        // the number and the rows below it cannot disagree
        rowFilter: agingFilter,
      })
      : deliveryTable(data, state.tab, {
        scope: deliveryScope(),
        benchmark: state.benchmark,
        base: appBase(),
        rowFilter: sprintKeys.size ? (row) => rowInSprint(row, sprintKeys) : null,
        sprintLabel: lensLabel,
      }))
    : '<div class="empty-row">Loading the triage list…</div>';
  // prd/63 B9: the Overview's "Done in Jira, PR open →" now lands on a list
  // that actually contains the set, scoped like the rest of this page.
  // prd/59: the Aging tab is its own list; the Done-open panel belongs to the
  // PR tables below it, not to the aging chart.
  const extra = $('delivery-extra');
  if (extra) {
    extra.innerHTML = data && !aging
      ? doneOpenMarkup(data, {
        base: appBase(), scope: deliveryScope(), id: 'done-open-delivery',
      })
      : '';
  }
  // prd/59: the aging tab is priced on its own payload's clock; the delivery
  // table keeps its wait-hours sentence. Never mix the two.
  // prd/65 N-07: the censoring/vacation sentences are stated once, in the tab
  // body (`.aging-meta` / `.aging-censor`), so this bar no longer repeats the
  // payload's `note` — the same two sentences a second time.
  const agingData = state.aging;
  $('delivery-assumption').textContent = aging
    ? (agingData
      ? `${agingData.assumption} · snapshot ${fmtStamp(Date.parse(agingData.as_of))} · ` +
        `${agingData.thresholds_mode === 'fixed' ? 'fixed thresholds' : 'self-calibrated thresholds'}`
      : '')
    : (data
      ? `${data.assumption} · snapshot ${fmtStamp(Date.parse(data.snapshot))} ` +
        `(every age on this page is priced to it) · ` +
        (state.scope.kind === 'all'
          ? `${data.counts.open} open items, ${data.counts.waiting_over_week} of them waiting over a week`
          : `${scopedDeliveryRows(data, deliveryScope()).length} open items in this scope ` +
            `(${data.counts.open} team-wide), ${scopedDeliveryRows(data, deliveryScope())
              .filter((row) => !row.blocked && row.waiting_hours >=
                Number((data.counts && data.counts.waiting_week_hours) || 40)).length} of them waiting over a week`) +
        `. ${data.impact_note}.`
      : '');
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
    // prd/49: a person/role scope means "waiting on it"; a repo scope means
    // "this repository's PRs" — the label must not reuse the wrong sentence.
    const scopeSuffix = state.scope.kind === 'repo'
      ? `the ${repoLabel(state.scope.value)} repo`
      : `${state.scope.label} (waiting on it)`;
    // prd/63 D: a pressed "median ×" chip with no contrast must say why
    // (the delivery wait cohort is the open-PR window, often N < 10).
    const waitBench = data.wait_benchmark;
    const benchNote = !state.benchmark
      ? 'median × hidden'
      : (waitBench && !waitBench.available && waitBench.note
        ? `median × unavailable — ${waitBench.note}`
        : '');
    // prd/63 F7/F16: stale and unobserved are tiles of *this view*, not the
    // team's. Scope both, and drop "unobserved" when it describes every row
    // (the table footnote already states what the flag means).
    const scopedRows = scopedDeliveryRows(data, deliveryScope());
    const stale = scopedRows.filter((row) => row.stale).length;
    const unobserved = scopedRows.filter((row) => row.unobserved).length;
    const unobservedNote = unobserved && unobserved < scopedRows.length
      ? ` · ${unobserved} unobserved`
      : '';
    if (aging) {
      // prd/59: the tab's honesty line is its own — the cohort and its n, the
      // band counts from the same rows, and the scope sentence.
      // prd/65 N-05: "the same rows" is literal — the counts, the chase item
      // count and the chip read the payload scoped once by `agingFilter`, so
      // `person:CaOs` cannot say 1 in the badge and 8 in the honesty line.
      const a = scopedAgingPayloadNow(sprintKeys) || {};
      const c = a.counts || {};
      setHonesty(
        `${c.open || 0} in flight · ${c.live || 0} live · ${c.over_p85 || 0} past p85` +
        `${c.no_live_pr ? ` · ${c.no_live_pr} no live PR` : ''}` +
        `${c.insufficient ? ` · ${c.insufficient} insufficient history` : ''} · ` +
        `${a.kind === 'pr' ? 'PR clock' : 'story clock'} · ` +
        (filtered ? `filtered to ${scopeSuffix}` : 'whole team') +
        ` · chase = age − p85, ranked (${(a.feed && a.feed.items || []).length} item${
          (a.feed && a.feed.items || []).length === 1 ? '' : 's'})`,
      );
      renderDeliveryDetail();
      return;
    }
    setHonesty(
      `${counts.all} open item${counts.all === 1 ? '' : 's'} in view · ` +
      `${counts.blocked} blocked · ${counts.oldest} waiting over a week (${weekLabel(data)}) · ` +
      `${stale} stale${unobservedNote} · ` +
      `impact = dependents × current wait (≈) · ` +
      (sprintKeys.size ? `sprint ${lensLabel} lens: ${sprintKeys.size} stories · ` : '') +
      (filtered ? `filtered to ${scopeSuffix}` : 'whole team') +
      (benchNote ? ` · ${benchNote}` : ''),
    );
  }
  renderDeliveryDetail();
  // prd/80: a tab/filter/sort repaint is a render too — re-annotate the new
  // anchors (the cache is already in memory, so this is a Map lookup each).
  renderTitles();
}

/** prd/59: refetch the aging payload after a control change, then repaint. */
async function reloadAging() {
  await ensureData('delivery');
  renderDeliveryPage();
  saveUI();
}

/** Counts follow the scope, so the tab number always matches the rows below it. */
function scopedTabCounts(data, sprintKeys = null) {
  const agingTotal = scopedAgingRows(sprintKeys).length;
  if (!data) return { blocked: 0, oldest: 0, aging: agingTotal, all: 0 };
  const rows = data.rows.filter((row) => matchesScopeRow(row)
    && (!sprintKeys || !sprintKeys.size || rowInSprint(row, sprintKeys)));
  // prd/41 B3: the same 40-business-hour week the payload states, never a
  // second threshold local to this view.
  const week = Number((data.counts && data.counts.waiting_week_hours) || 40);
  return {
    blocked: rows.filter((row) => row.blocked).length,
    oldest: rows.filter((row) => !row.blocked && row.waiting_hours >= week).length,
    // prd/59: the tab count is the aging payload's own rows, filtered by the
    // same scope and sprint lens, so it cannot disagree with the table below it.
    aging: agingTotal,
    all: rows.length,
  };
}

/**
 * prd/59: an aging row under the page's scope. The row's holders are its
 * `holder` entries (person + role) and its ticket is its story key, so the
 * same `matchesScope` answers "waiting on X" and "this repo/ticket" here.
 *
 * The repo rule (prd/65 N-01): `/api/aging?repo=…` is applied *server-side* —
 * the store joins each story to its PRs and returns only the stories that have
 * a PR in that repo — and a story row carries neither `repo` nor `repo_short`
 * (`analysis/aging.py` `_story_rows`). Re-testing the missing field here would
 * filter every story to `false` while the chip and the strip (which read the
 * payload, not this predicate) still said 7. One rule: for `kind === 'story'`
 * under a repo scope the server's answer *is* the scope; PR rows keep the
 * field check because they do carry their repository.
 */
function agingMatchesScope(row, scope) {
  if (!scope) return true;
  if (scope.kind === 'repo' && row && row.kind === 'story') return true;
  return matchesScope({
    ...row,
    tickets: [row.ticket].filter(Boolean),
    waiting_on: (row.holder || []).map((holder) => holder.person),
    waiting_roles: Object.fromEntries(
      (row.holder || []).map((holder) => [holder.person, holder.role]),
    ),
  }, scope);
}

/**
 * prd/65 N-05: one predicate for every surface of the Aging tab — the tab
 * badge, the table, the count chip, the strip dots, the honesty line and the
 * re-plan list — so a person/role scope cannot filter one and leave another
 * quoting the team's numbers.
 */
function agingRowFilter(sprintKeys = null) {
  const scope = deliveryScope();
  return (row) => agingMatchesScope(row, scope)
    && (!sprintKeys || !sprintKeys.size
      || (Boolean(row.ticket) && sprintKeys.has(String(row.ticket))));
}

/** The aging payload the tab renders: the server's answer, scoped once. */
function scopedAgingPayloadNow(sprintKeys = null) {
  return scopedAgingPayload(state.aging, {
    onlyLive: state.agingOnly,
    rowFilter: agingRowFilter(sprintKeys),
  });
}

/** The aging rows the active scope and sprint lens keep (prd/59 tab count). */
function scopedAgingRows(sprintKeys = null) {
  const payload = state.aging;
  if (!payload) return [];
  const scope = deliveryScope();
  let rows = agingRows(payload, { onlyLive: state.agingOnly });
  if (scope) rows = rows.filter((row) => agingMatchesScope(row, scope));
  if (sprintKeys && sprintKeys.size) {
    rows = rows.filter((row) => row.ticket && sprintKeys.has(String(row.ticket)));
  }
  return rows;
}

function matchesScopeRow(row) {
  return matchesScope(row, deliveryScope());
}

function deliveryScope() {
  if (state.scope.kind === 'all') return null;
  return { kind: state.scope.kind, value: state.scope.value, label: state.scope.label };
}

function renderDeliveryDetail() {
  const container = $('delivery-detail');
  if (state.tab === 'aging') {
    // prd/59: an aging row is a doorway (#ticket/KEY), not a PR drawer row;
    // the detail column states that instead of inviting a selection that is
    // not there.
    container.classList.remove('has-selection');
    container.innerHTML = `<p class="empty-detail">The aging list opens each item’s ticket page.
      The strip shows in-flight age against the team’s own p50/p85/p95; the rows are
      ranked by hours at risk (age − p85).</p>`;
    return;
  }
  const row = state.selection && state.selection.row;
  // prd/28: on a phone the panel is a bottom sheet; it exists only while a row
  // is selected, so the list keeps the full viewport until the user asks for
  // detail. On desktop the class is inert and the column is unchanged.
  container.classList.toggle('has-selection', Boolean(row));
  if (!row) {
    container.innerHTML = '<p class="empty-detail">Select a row to see its impact, facts and pivot chips.</p>';
    return;
  }
  container.innerHTML = `<div class="sheet-handle" aria-hidden="true"></div>
    <button type="button" class="sheet-close" data-sheet-close aria-label="close the detail panel">×</button>` +
    detailMarkup({
    overline: 'PULL REQUEST',
    row,
    impact: impactSentence(row),
    facts: [
      ['ticket', rowTicket(row)],
      ...(row.epic ? [['epic', row.epic]] : []),
      // prd/40: the three facts the old single ball label could not say
      ['state', reviewStateLine(row)],
      ['waiting on', waitingLabel(row)],
      ...(row.waiting_roles && Object.keys(row.waiting_roles).length
        ? [['holder roles', Object.entries(row.waiting_roles)
          .map(([person, role]) => `${person} (${role})`).join(', ')]]
        : []),
      ...(row.stale ? [['stale', (row.stale_reasons || []).join('; ')]] : []),
      ...(row.unobserved ? [['unobserved since', row.unobserved_since]] : []),
      // prd/57: the wait is read against the window cohort's median.
      ['waiting', fmtHours(row.waiting_hours), row.wait_benchmark],
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
  }, { benchmark: state.benchmark });
}

/** prd/28: closing the phone's detail sheet clears the selection it showed. */
function clearDeliverySelection() {
  state.selection = null;
  renderDeliveryDetail();
  for (const tr of document.querySelectorAll('#delivery-table tr.row')) {
    tr.classList.remove('selected');
  }
}

// -------------------------------------------------------------------- ticket
/* prd/25: the ticket detail page. It is a route (`#ticket/DEL-167`), not a
 * modal, so every ticket key anywhere in the app can be a real anchor and a
 * reload lands on the same page. The one-line answer is the page's contract:
 * it names where the time went, or the boundary the store is missing. */
function appBase() {
  if (typeof location === 'undefined') return '';
  const base = `${location.origin}${location.pathname}`;
  // prd/63 A4: every generated link (Markdown exports, ticket anchors, the
  // chase action) must name the tenant it was copied from. Without `?tenant=`
  // a link opened on the default dataset — a different store with different
  // numbers for the same key.
  const tenant = tenantQuery();
  return tenant ? `${base}?${tenant}` : base;
}

function renderTicketPage() {
  const body = $('ticket-body');
  if (state.ticketError) {
    body.innerHTML = `<div class="rules-empty">
      <p>${esc(state.ticketError)}</p>
      <p class="muted">A ticket is a STORY object or a PR.ticket_keys entry. Check the key,
        or switch to a dataset that holds it.</p></div>`;
  } else {
    body.innerHTML = state.ticket
      ? ticketMarkup(state.ticket, { base: appBase(), benchmark: state.benchmark })
      : '<p class="empty-row">Loading the ticket…</p>';
  }
  $('ticket-stats').innerHTML = state.ticket
    ? ticketStats(state.ticket, { benchmark: state.benchmark })
    : '';
  $('ticket-back').textContent =
    `← ${PAGES[state.ticketFrom] ? PAGES[state.ticketFrom].title : 'back'}`;
  const data = state.ticket;
  if (data) {
    if (data.kind === 'epic') {
      const epic = data.epic || {};
      setHonesty(
        `epic ${data.key} · ${epic.story_count || 0} child stories · ` +
        `not a story, so it has no cycle of its own`,
      );
      $('ticket-assumption').textContent =
        `${data.assumption} · snapshot ${fmtStamp(Date.parse(data.snapshot))} · ` +
        'an epic groups stories; each child story carries its own cycle.';
      return;
    }
    const missing = (data.segments && data.segments.missing) || [];
    const total = ((data.segments && data.segments.rows) || []).length;
    // prd/63 F17: without a STORY object there are no story segments to
    // measure; say what the PR side does have instead of "0/8 segments".
    setHonesty(data.story
      ? `${data.pr_count} PR${data.pr_count === 1 ? '' : 's'} joined on ${data.key} · ` +
        `${data.segments.measured}/${total} segments measured · ` +
        (data.hours_mode === 'business' ? 'business hours' : 'wall-clock hours') +
        (missing.length ? ` · missing boundaries: ${missing.join(', ')}` : '')
      : `no STORY object for ${data.key} in this dataset · ` +
        `${data.pr_count} PR${data.pr_count === 1 ? '' : 's'} measured from the code · ` +
        (data.hours_mode === 'business' ? 'business hours' : 'wall-clock hours'),
    );
    $('ticket-assumption').textContent =
      `${data.assumption} · snapshot ${fmtStamp(Date.parse(data.snapshot))} · ` +
      'a segment needs both boundaries; an absent one is named, never estimated.';
  } else if (state.ticketError) {
    setHonesty('the store cannot answer for this key');
    $('ticket-assumption').textContent = '';
  }
}

async function openTicket(key) {
  // prd/63 A2: a deep link (#ticket/…) used to send anything to the server and
  // leave "Loading the ticket…" if it was not a key. Normalise the case, then
  // reject a malformed key client-side with a stated error — never a spinner.
  const wanted = ticketFromQuery(key);
  if (!wanted) {
    if (state.page !== 'ticket') {
      state.ticketFrom = state.page;
      closeDrawer();
    }
    const shown = ticketKey(key).toUpperCase() || String(key || '');
    state.ticketKey = shown;
    state.ticket = null;
    state.ticketError =
      `“${shown}” is not a ticket key — a key looks like PROJ-123.`;
    state.page = 'ticket';
    renderPage();
    return;
  }
  if (state.page !== 'ticket') {
    state.ticketFrom = state.page;
    closeDrawer();
  }
  state.ticketKey = wanted;
  state.ticket = null;
  state.ticketError = null;
  state.page = 'ticket';
  // prd/48: the ticket is a history entry of its own — the key changed, so the
  // render's syncUrl pushes. A back/forward restore goes through `applyRoute()`
  // with `restoringRoute` set, so the entry the browser already holds is
  // rewritten instead of duplicated. renderPage() writes the whole route —
  // tenant + view in the query, the key in the hash (P-2).
  renderPage();
  try {
    await ensureData('ticket');
  } catch (err) {
    if (state.ticketKey !== wanted) return;
    state.ticket = null;
    // prd/63 A2: a 404/400 is a stated result. The page's helper line below
    // says what a ticket is, so the server's hint is not repeated verbatim.
    if (err.status === 404) {
      state.ticketError =
        `No ticket ${wanted} in dataset ${state.tenant || 'this store'}.`;
    } else if (err.status === 400) {
      state.ticketError =
        `That key is not a valid ticket in dataset ${state.tenant || 'this store'}: `
        + `${(err.body && err.body.error) || err.message}.`;
    } else {
      state.ticketError = `Could not load ticket ${wanted}: ${err.message}`;
    }
  }
  if (state.page === 'ticket' && state.ticketKey === wanted) renderPage();
}

/* P-2: back/forward, and a `#ticket/KEY` typed or pasted into the bar, land
 * here. Only state that actually differs is moved, so a popstate followed by
 * its own hashchange is a no-op the second time. prd/48: the whole restore is
 * flagged, so no render inside it can push a duplicate entry. */
async function applyRoute() {
  if (typeof location === 'undefined') return;
  const route = parseRoute(location.search, location.hash);
  const wasRestoring = restoringRoute;
  restoringRoute = true;
  try {
    // prd/10 §3.1: a `?theme=` link is a destination like any other; an absent
    // parameter keeps the stored preference (dark is the default either way).
    if (route.theme && route.theme !== state.theme) setTheme(route.theme, { save: false });
    // prd/80: `?titles=off` is a destination, not a preference flip — visiting
    // a colleague's hidden-titles link must not rewrite this browser's toggle
    // (the same rule prd/63 F29 set for `?theme=`).
    if (route.titles === 'off' && state.titlesEnabled) {
      setTitlesEnabled(false, { save: false });
    } else if (route.titles === 'on' && !state.titlesEnabled) {
      setTitlesEnabled(true, { save: false });
    }
    if (route.tenant && route.tenant !== state.tenant) {
      await setTenant(route.tenant, { silent: true });
    }
    if (route.scope
      && (route.scope.kind !== state.scope.kind || route.scope.value !== state.scope.value)) {
      setScope(route.scope.kind, route.scope.value, { silent: true });
    }
    if (route.timeframe && TIMEFRAMES[route.timeframe] && route.timeframe !== state.timeframe) {
      setTimeframe(route.timeframe, { silent: true });
    }
    // prd/63 F30/F21: a shared Trend tab pair or lab scenario is route state.
    // prd/59: a `?tab=aging` link selects the Aging subset (and pins it, so the
    // auto-switch cannot move the reader off the linked view).
    if (route.tab && route.tab !== state.tab) {
      state.tab = route.tab;
      state.tabPinned = true;
    }
    if (route.metric && route.metric !== state.trendMetric) state.trendMetric = route.metric;
    if (route.bucket && route.bucket !== state.trendBucket) state.trendBucket = route.bucket;
    if (route.policy) {
      state.policyScenario = { ...defaultScenario(), ...route.policy };
    }
    // prd/37 §4: a sprint link restores the filter, the band and the range.
    if (route.sprint != null) {
      if (state.sprints) {
        if (!focusSprint(route.sprint, { start: route.from, end: route.to, sync: false })) {
          state.sprint = null;
        }
      } else {
        // the payload is not loaded yet; load() applies it once the bands exist
        state.sprint = route.sprint;
      }
    } else if (state.sprint != null) {
      state.sprint = null;
      timeline.setSprintFocus(null);
      renderSprintStrip();
    }
    if (route.ticket) {
      if (state.page !== 'ticket' || state.ticketKey !== route.ticket) {
        await openTicket(route.ticket);
      }
      return;
    }
    if (state.page === 'ticket') {
      await showPage(route.view || state.ticketFrom || 'overview');
      return;
    }
    if (route.view && route.view !== state.page) await showPage(route.view);
  } finally {
    restoringRoute = wasRestoring;
  }
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
    ? `${data.assumption} · ${data.window.days} calendar days in view · edge = “X waited on Y” · ${data.attribution}.${data.hours_note ? ` ${data.hours_note}.` : ''} Direction colour is relative to the selected node.`
    : '';
  if (data) {
    const totals = data.totals || {};
    const scoped = state.scope.kind !== 'all';
    // prd/49: a repo scope is answered by the API, so this graph really is
    // filtered; a person/role scope is not (a handover is never one person's).
    const repoFiltered = scoped && state.scope.kind === 'repo';
    setHonesty(
      `${totals.nodes || 0} nodes · ${totals.edges || 0} edges · ` +
      `${fmtHours(totals.hours || 0)} measured wait in the ${data.window.days} calendar-day window · ` +
      // prd/63 F20: people and role levels total differently (concurrent
      // holders are charged to each at person level); the Overview uses the
      // role-level number. Name the basis instead of showing two "mean"s.
      (data.level === 'person'
        ? 'person level charges every concurrent holder, so it exceeds the role total; the Overview headline uses role-level queue wait · '
        : 'role level counts each handover once; the Overview headline uses this basis · ') +
      (repoFiltered
        ? `filtered to the ${repoLabel(state.scope.value)} repo`
        : (scoped ? 'not filtered by scope — every handover stays visible' : 'whole team')),
    );
  }
  renderBlockingStats();
  // prd/56 §3: the same graph as text, so the numbers are readable without
  // hovering the canvas (and waits render as "d h", never "1348h").
  const view = $('blocking-view');
  if (view) view.innerHTML = blockingText(data, state.lens);
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
       <span class="stat-inline"><b>${fmtHours(totals.hours || 0)}</b> queue-wait hours</span>
       <span class="stat-inline">mean <b>${fmtHours(totals.mean_wait_hours || 0)}</b></span>
       <span class="stat-inline"><b>${totals.nodes || 0}</b> nodes</span>
       <span class="stat-inline muted">handover wait — not response time</span>`;
}

/* prd/64 F-11: the inspector's Blocking chip acts on the selected row, so the
 * map opens with that row's station (or holder) selected instead of unpanned on
 * the whole graph. Passing `select()` only when the node differs keeps it a
 * focus rather than a toggle-off. */
function focusBlockingRow(row) {
  if (!row) return false;
  const candidates = [
    row.ball_label, row.ball, ...(row.waiting_on || []),
    ...Object.keys(row.waiting_roles || {}), row.phase,
  ].filter(Boolean);
  const id = candidates.find((candidate) =>
    blockingMap.nodes.some((node) => String(node.id) === String(candidate)));
  if (!id) return false;
  if (blockingMap.selected !== id) blockingMap.select(id);
  renderBlockingStats();
  return true;
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
  // prd/42 M1/M7: the response-time column and the pseudonym legend sit under
  // the chart, so "who is the constraint" is answerable without the raw store.
  const responseHost = $('queue-response');
  // prd/63 F26: the response table follows the same scope as the chart. With
  // `person:LaAd` the page said "0 streams" while the table still listed every
  // reviewer. Pass the scope so the table is empty (and says why) too.
  if (responseHost) responseHost.innerHTML = data
    ? responseTable(data, {
      benchmark: state.benchmark,
      scope: filteredPages(state.scope).has('queue') ? state.scope : null,
    })
    : '';
  const legendHost = $('queue-legend');
  if (legendHost) legendHost.innerHTML = data ? legendMarkup(data) : '';
  // prd/63 F26: one explanatory paragraph, not the same sentence twice. The
  // diagnosis note already carries the metric/baseline statements.
  $('queue-assumption').textContent = data
    ? `${data.assumption} · ${data.bucket_hours} h buckets over ${data.window.days} calendar days. ` +
      `Little's law: WIP ≈ throughput × wait. ${data.diagnosis_note}.`
    : '';
  if (data) {
    const filtered = filteredPages(state.scope).has('queue');
    setHonesty(
      `${streams.length} stream${streams.length === 1 ? '' : 's'} · ` +
      `WIP sampled every ${data.bucket_hours} h over ${data.window.days} calendar days · ` +
      // prd/63 C11: name this clock. The response table below is a different
      // metric (added-as-reviewer → first response), never "the constraint".
      `this table/mean is queue wait (person-hours) — response time is the table below · ` +
      (filtered ? `filtered to ${state.scope.label}` : 'whole team'),
    );
  }
}

function queueMatchesScope(stream) {
  if (state.scope.kind === 'all') return true;
  // prd/49: the API answered this view for the repo cohort; every returned
  // stream is already that repo's person/queue, so nothing is filtered here.
  if (state.scope.kind === 'repo') return true;
  const value = String(state.scope.value || '').toLowerCase();
  if (state.scope.kind === 'person') return String(stream.id).toLowerCase() === value;
  if (state.scope.kind === 'role') {
    // Role/queue scopes match the stream id, including the synthetic `author`
    // and `review` streams (B4) — and `role:sentinel` matches the sentinel's
    // own person stream at the person level, not only the `sentinel` queue.
    if (String(stream.id).toLowerCase() === value) return true;
    return value === 'sentinel' && stream.role === 'sentinel';
  }
  return true;
}

// -------------------------------------------------------------------- trend
/* prd/50: one headline metric per week (or sprint), evaluated by the same
 * functions as the headline. The chart is inline SVG; the table below it is the
 * same series, so a reader never needs to hover. The bucket that contains the
 * snapshot is labelled "in progress" — provisional, not hidden. */
function renderTrendPage() {
  const data = state.trend;
  $('trend-body').innerHTML = trendMarkup(data, {
    bucket: data ? data.bucket : state.trendBucket,
    // prd/63 F1: the body states a failed fetch; it never keeps the spinner.
    error: state.trendError,
    // prd/61: the before/after panel beside the annotations.
    compare: state.compare,
  });
  $('trend-stats').innerHTML = data && !state.trendError ? trendStats(data) : '';
  $('trend-assumption').textContent = state.trendError
    ? `the /api/trend request failed — nothing on this page is stale data from a previous metric`
    : (data
      ? `${data.definition} · ${data.assumption} · snapshot ${fmtStamp(Date.parse(data.as_of))} · ` +
        `${data.bucket} buckets: the first/last is clipped to the analysis window; ` +
        'the bucket containing the snapshot is in progress.'
      : '');
  if (data && !state.trendError) {
    const summary = data.summary || {};
    setHonesty(
      `${summary.measured_buckets || 0} of ${summary.buckets || 0} ${data.bucket} buckets measured · ` +
      `${(summary.in_progress || []).length
        ? `in progress: ${(summary.in_progress || []).join(', ')}`
        : 'every bucket is complete'} · ` +
      `same cohort and calendar as the headline (${data.hours_mode} hours)`,
    );
  }
}

async function setTrendView({ metric = null, bucket = null } = {}) {
  if (metric) state.trendMetric = metric;
  if (bucket) state.trendBucket = bucket;
  state.trend = null;
  state.trendError = null;
  renderPage();
  try {
    await ensureData('trend');
  } catch (err) {
    setStatus(`trend failed: ${err.message}`, 'stale');
    // prd/63 F1: the chart area says the request failed (with a retry), not
    // "Loading the trend…" forever.
    state.trendError = err && err.message ? err.message : 'the request failed';
    if (state.page === 'trend') renderPage();
    return;
  }
  state.trendError = null;
  if (state.page === 'trend') renderPage();
  saveUI();
}

/* prd/61: the before/after panel's split (an annotation date) or metric. */
async function setTrendCompare({ split = null, metric = null } = {}) {
  if (split) state.compareSplit = split;
  if (metric) state.compareMetric = metric;
  state.compare = null;
  if (state.page === 'trend') renderPage();
  try {
    await ensureData('trend');
  } catch (err) {
    setStatus(`before/after failed: ${err.message}`, 'stale');
    return;
  }
  if (state.page === 'trend') renderPage();
  saveUI();
}

/* prd/61: one annotation, posted as first-party metadata (never scraped). */
async function recordAnnotation(form) {
  const data = new FormData(form);
  const raw = data.get('value');
  const payload = {
    kind: data.get('kind'),
    date: data.get('date'),
    value: raw === null || String(raw).trim() === '' ? null : Number(raw),
    note: String(data.get('note') || '').trim() || null,
  };
  if (!payload.date) {
    flashStatus('a change needs a date', 'stale');
    return;
  }
  // A repo scope rides only when the page is scoped to one repo.
  if (state.scope && state.scope.kind === 'repo') payload.scope = state.scope.value;
  try {
    const { response, body } = await fetchJSON(withTenant('/api/annotations'), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new HttpError(response.status, body, '/api/annotations');
    state.compareSplit = payload.date;
    // The annotation lives in the store, so every cached trend/compare answer
    // may be stale; clear the client cache and ask again.
    state.cache.clear();
    state.trend = null;
    state.compare = null;
    await ensureData('trend');
    renderPage();
    // prd/65 N-04: held, so the /ws watcher echo ~1 s later cannot replace this
    // confirmation with the live line before the reader has seen it.
    flashStatusHeld(`recorded ${(body.annotation && body.annotation.label) || 'the change'}`);
  } catch (err) {
    flashStatus(`could not record the change: ${err.message}`, 'stale');
  }
}

/* prd/61: delete is the second click of the inline confirm. */
async function deleteAnnotation(id) {
  try {
    const { response, body } = await fetchJSON(
      withTenant(`/api/annotations/${encodeURIComponent(id)}`),
      { method: 'DELETE' },
    );
    if (!response.ok) throw new HttpError(response.status, body, `/api/annotations/${id}`);
    state.cache.clear();
    state.trend = null;
    state.compare = null;
    state.compareSplit = '';
    await ensureData('trend');
    renderPage();
    flashStatusHeld('annotation deleted');
  } catch (err) {
    flashStatus(`could not delete the annotation: ${err.message}`, 'stale');
  }
}

// ------------------------------------------------------- benchmark contrast
/* prd/57: every per-view duration carries its cohort median ×; the toggle is
 * per view (delivery, ticket, queue) but one preference, so the surfaces never
 * disagree about whether the contrast is on. */
function renderBenchmarkToggles() {
  for (const button of document.querySelectorAll('[data-benchmark-toggle]')) {
    button.setAttribute('aria-pressed', String(state.benchmark));
    button.classList.toggle('on', state.benchmark);
    button.title = state.benchmark
      ? 'showing the cohort median × next to each duration — click to hide it'
      : 'benchmark contrast hidden — click to show the cohort median ×';
  }
}

function setBenchmark(on) {
  state.benchmark = Boolean(on);
  saveUI();
  renderPage();
  // The drawer is a separate surface; repaint it with the new preference too.
  if (state.drawerEntity && $('drawer') && !$('drawer').hidden) {
    $('drawer').innerHTML = '<div class="sheet-handle" aria-hidden="true"></div>'
      + '<button type="button" class="drawer-close" data-drawer="close" aria-label="close">×</button>'
      + detailMarkup(state.drawerEntity, { benchmark: state.benchmark });
  }
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
  // prd/53 §2: the pseudonym legend sits next to the worklog card, so "who is
  // the sentinel" is answerable here without opening Queue. Roles only.
  const legend = data && data.legend && (data.legend.people || []).length
    ? `<div class="rules-legend">${legendMarkup({ legend: data.legend })}</div>`
    : '';
  $('rules-list').innerHTML = data
    ? `${legend}${rulesMarkup(data, {
      base: appBase(),
      prSize: state.prSize,
      caps: state.sizeCaps,
      verdict: state.verdict,
      showAllRules: state.showAllRules,
    })}`
    : '<div class="empty-row">Loading the rule engine…</div>';
  // prd/39: the panel's count/markers follow the sliders without a re-render.
  updatePrSizePanel();
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
      `size cap ${data.targets.size_commit_cap} commit${data.targets.size_commit_cap === 1 ? '' : 's'} / ${data.targets.size_line_cap} lines · ` +
      `one review round ≈ ${fmtHours(data.targets.hours_per_round)} ` +
      `(${data.targets.hours_per_round_basis}) · ranked by impact × confidence; ` +
      // prd/42 P2: severity is a tie-break, not the rank.
      `severity breaks ties, so a critical rule with low confidence can rank below a warning one.` +
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

/* prd/39: the live half of the PR-size panel. The count is recomputed from the
 * API's per-PR rows (not from the histograms), so a slider move is exact; only
 * the rule's own evidence needs a refetch, and that happens on release. */
function updatePrSizePanel() {
  const payload = state.prSize;
  const root = $('pr-size-panel');
  if (!payload || !root) return;
  const caps = normalizeSizeCaps(state.sizeCaps);
  const counts = flagCounts(payload.rows, caps);
  const set = (selector, value) => {
    for (const node of root.querySelectorAll(selector)) node.textContent = String(value);
  };
  set('[data-pr-size-large]', counts.large);
  set('[data-pr-size-total]', counts.total);
  for (const kind of ['commits', 'lines']) {
    const series = payload[kind] || {};
    const cap = caps[kind];
    set(`[data-pr-size-cap="${kind}"]`, cap);
    set(`[data-pr-size-cap-label="${kind}"]`, cap);
    const marker = root.querySelector(`[data-pr-size-marker="${kind}"]`);
    if (marker) marker.style.left = `${capPercent(series.edges, cap).toFixed(2)}%`;
    const slider = root.querySelector(`[data-pr-size="${kind}"]`);
    if (slider && !slider.disabled) slider.value = String(cap);
  }
  const split = root.querySelector('[data-pr-size-split]');
  if (split) {
    split.textContent = ` · ${counts.overCommits} over commits · ${counts.overLines} over changed lines`;
  }
  // prd/63 D: moving the sliders must either fire the pr_size rule or say why
  // it does not — "the same caps drive the pr_size rule" was a promise the page
  // never kept when the rule's condition failed.
  const why = root.querySelector('[data-pr-size-why]');
  if (why) why.textContent = prSizeWhy();
}

/** Why the pr_size rule does or does not appear at the chosen caps. */
function prSizeWhy() {
  const metrics = state.rules && state.rules.metrics;
  const ps = metrics && metrics.pr_size;
  if (!ps) return '';
  const suggestion = ((state.rules && state.rules.suggestions) || [])
    .find((entry) => entry.id === 'pr-size');
  if (suggestion) {
    return `At these caps the PR-size rule fires: ${suggestion.title}.`;
  }
  if (Number(ps.large_prs) < 2 || Number(ps.small_prs) < 2) {
    return `No PR-size rule at these caps: only ${ps.large_prs || 0} large and ` +
      `${ps.small_prs || 0} small PRs — a large-vs-small comparison needs at least 2 of each.`;
  }
  const large = ps.large_median_first_review_hours;
  const small = ps.small_median_first_review_hours;
  if (large == null || small == null) {
    return 'No PR-size rule at these caps: a group has no measured first review, so there is nothing to compare.';
  }
  if (!(large > small)) {
    return `No PR-size rule at these caps: large PRs' first reviews are not slower ` +
      `(${fmtHours(large)} vs ${fmtHours(small)} for smaller ones).`;
  }
  return 'No PR-size rule at these caps.';
}

function persistSizeCaps() {
  state.sizeCapsMap = rememberSizeCaps(state.sizeCapsMap, state.tenant, state.sizeCaps);
  saveUI();
}

function setSizeCaps(next, { refetch = false } = {}) {
  state.sizeCaps = normalizeSizeCaps(next);
  persistSizeCaps();
  updatePrSizePanel();
  if (!refetch) return;
  // The size rule's evidence quotes the caps, so re-ask once the slider is
  // released (not on every pixel of the drag, which would replace the input).
  state.cache.clear();
  ensureData('rules').then(() => {
    if (state.page === 'rules') renderRulesPage();
  }).catch((err) => setStatus(`size calibration failed: ${err.message}`, 'stale'));
}

// ------------------------------------------------------------------- policy
/* The Policy lab (prd/23): a calibrated SimPy model, a validation gate, and one
 * scenario A/B. The page never shows a predicted number without the `≈` marker
 * and the provenance line; when the baseline fails the gate, the scenario is
 * not run and the mismatch is the result. */
function renderPolicyPage() {
  const data = state.policy;
  const loading = state.policyLoading
    ? `<p class="policy-loading" role="status" data-policy-loading="true">
         running the calibrated model… <span class="muted">(the numbers below are the previous run)</span></p>`
    : '';
  $('policy-body').innerHTML = loading + policyMarkup(data, state.policyScenario);
  const baseline = data && data.baseline && data.baseline.ok ? data.baseline : null;
  $('policy-stats').innerHTML = data && data.validation
    ? `<span class="stat-inline"><b class="${data.validation.ok ? '' : 'attention'}">${
        data.validation.status === 'validated' ? 'validated' : 'not validated'}</b> baseline</span>
      <span class="stat-inline"><b>${baseline ? predHours(baseline.predicted.cycle_hours.median) : '–'}</b> cycle median</span>
      <span class="stat-inline"><b>${data.calibration && data.calibration.prs ? data.calibration.prs.n : 0}</b> PRs calibrated</span>`
    : '';
  setHonesty(data
    ? `predictions, not measurements — every number is ≈ and the model is ` +
      `validated against the store's own cycle-time median`
    : '');
}

async function runPolicyScenario(scenario) {
  state.policyScenario = scenario;
  state.cache.clear();
  // prd/63 F21: show the run, then repaint with its result; the URL carries
  // the scenario so a colleague can reopen it.
  state.policyLoading = true;
  renderPolicyPage();
  syncUrl();
  try {
    await ensureData('policy');
  } catch (err) {
    state.policyLoading = false;
    setStatus(`policy lab failed: ${err.message}`, 'stale');
    return;
  } finally {
    state.policyLoading = false;
  }
  renderPolicyPage();
  syncUrl();
  saveUI();
}

// ----------------------------------------------------------------- overview
function renderOverviewPage() {
  // prd/58: the verdict is the first thing on the page — one claim above the
  // chase card; the tiles and the triage list follow it.
  $('overview').innerHTML = verdictCard(state.verdict, { base: appBase() })
    + overviewMarkup({
    delivery: state.delivery,
    // prd/64 F-04: the canonical overview payloads, never the Queue/Blocking
    // page's current level. The tiles and diagnosis therefore stay put when a
    // toggle is flipped elsewhere, and their basis line is the truth.
    queues: state.overviewQueues,
    blocking: state.overviewBlocking,
    queueLevel: OVERVIEW_QUEUE_LEVEL,
    scope: state.scope,
    base: appBase(),
  });
  // B7 + prd/56 §4: the overview really does narrow under a scope, so its
  // disclosure says which scope rather than claiming it shows the whole team.
  // prd/63 B7: the header counts come from the same filtered set as the tiles.
  const counts = state.delivery ? overviewCounts(state.delivery, state.scope) : null;
  setHonesty(counts
    ? `${counts.open} open item${counts.open === 1 ? '' : 's'} · ${counts.blocked} blocked · ` +
      (state.scope.kind === 'all'
        ? 'whole team in the window'
        : `filtered to ${state.scope.kind} ${state.scope.label} — tiles and rows are the same set`)
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

/**
 * prd/68 F-13: the dataset's own layers — PR rows only when a PR layer exists.
 *
 * On a Jira-only tenant the facts read `PRs 0 kept of 0 seen` and
 * `dropped: 0 PRs had no ticket key`: two true-zero statements about a layer
 * the store does not have, beside no mention of the 2,320 stories it does have.
 * The missing layer is stated once, and the story/epic/sprint counts are shown.
 */
function layerFacts(meta) {
  const facts = [];
  if (Number(meta.prs)) {
    facts.push(['PRs', `${meta.prs_kept ?? '—'} kept of ${meta.prs_seen ?? '—'} seen`]);
    facts.push(['commits', String(meta.commits ?? '—')]);
    facts.push(['dropped', `${meta.prs_dropped ?? '—'} PRs had no ticket key`]);
  } else {
    facts.push(['PR layer', 'not in this dataset']);
  }
  for (const key of ['stories', 'epics', 'sprints']) {
    if (meta[key] != null) facts.push([key, String(meta[key])]);
  }
  return facts;
}

/**
 * prd/68 F-13: the ingest note names the source(s) the store was built from.
 *
 * One Jira ingest run is not "the combined tenant [that] joins the PR half to
 * the Jira ticket half": say which source built the dataset, and when there are
 * no PRs, that the Jira half is all of it.
 */
function sourcesRunNote(meta) {
  const runs = Array.isArray(meta.runs) ? meta.runs : [];
  if (!runs.length) return '';
  const names = [...new Set(runs.map((run) => run && run.source).filter(Boolean))];
  const named = names.length ? ` from ${names.join(' + ')}` : '';
  const plural = runs.length === 1 ? '' : 's';
  if (!Number(meta.prs)) {
    return `${runs.length} ingest run${plural}${named} built this dataset; it is the Jira half only — there is no PR layer to join to.`;
  }
  if (runs.length === 1) return `1 ingest run${named} built this dataset.`;
  return `${runs.length} ingest runs${named} built this dataset; the combined tenant joins the PR half to the Jira ticket half on ticket keys.`;
}

/**
 * prd/70 F-15: one ingest run's contribution, per source.
 *
 * The Sources table used PR columns for every run, so the Jira run read
 * `0 / 0 / 0` while its 2,391 issues and its events were invisible. A run that
 * saw PRs reports PRs; a Jira run reports issues. `/api/health`'s `runs` array
 * carries only the PR counters, so:
 *   - issues come from the Jira layer's own object counts (stories + epics),
 *     which is the same 2,391 the run kept;
 *   - events come from `/api/events/counts.by_repo`: Jira events carry no repo,
 *     PR events do, and that split is exact on the combined store.
 * A newer payload that does carry `issues_seen` / `events_written` wins.
 */
function sourceRunRow(run, meta = {}, counts = null) {
  const number = (value) => {
    if (value == null || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  };
  const run0 = run || {};
  const prSide = Number(run0.prs_seen || 0) > 0;
  const jiraSide = !prSide;
  const derivedIssues = Number(meta.stories || 0) + Number(meta.epics || 0);
  const issues = jiraSide
    ? {
      seen: number(run0.issues_seen) ?? (derivedIssues || null),
      kept: number(run0.issues_kept) ?? (derivedIssues || null),
    }
    : null;
  const prs = prSide
    ? { seen: number(run0.prs_seen), kept: number(run0.prs_kept) }
    : null;
  // Events: prefer the per-run counter; else split the counts histogram by repo.
  let events = number(run0.events_written);
  if (events == null && counts) {
    const byRepo = counts.by_repo || {};
    let repo = 0;
    let repoLess = null;
    for (const [key, value] of Object.entries(byRepo)) {
      const n = Number(value) || 0;
      if (key === 'null' || key === 'None' || key === '') repoLess = n;
      else repo += n;
    }
    if (repoLess == null && counts.total != null) {
      repoLess = Math.max(0, (Number(counts.total) || 0) - repo);
    }
    events = prSide ? repo : repoLess;
  }
  return {
    source: run0.source || '—',
    as_of: run0.as_of || null,
    kind: prSide ? 'pr' : 'jira',
    issues,
    prs,
    dropped: prSide ? number(run0.prs_dropped) : null,
    events: events == null ? null : events,
  };
}

function renderSourcesPage() {
  const meta = state.meta || {};
  const tenant = currentTenant() || {};
  const rows = [
    ['tenant', meta.tenant || state.tenant || '—'],
    ['build', versionLabel(state.build) || '—'],
    ['store', storeLabel(meta)],
    ['source', meta.source || '—'],
    // prd/38 #18: one age string, the same one the provenance line uses.
    ['snapshot', meta.as_of ? `${fmtStamp(Date.parse(meta.as_of))} (${snapshotAge()})` : '—'],
    ['events', String(meta.events ?? '—')],
    ...layerFacts(meta),
    // prd/63 F27: the store span is not the analysis window. The top bar and
    // Rules query the chosen timeframe (1q), so Sources states that window
    // once, and the store span separately.
    ['analysis window', (() => {
      const window = presetWindow();
      const label = TIMEFRAMES[state.timeframe] ? TIMEFRAMES[state.timeframe].label : 'the window';
      return window
        ? `${fmtDaySpan(window.start, window.end)} (${label})`
        : '—';
    })()],
    ['store span', `${meta.first_ts && meta.last_ts
      ? fmtDaySpan(Date.parse(meta.first_ts), Date.parse(meta.last_ts))
      : meta.first_ts
        ? fmtDaySpan(Date.parse(meta.first_ts), Date.parse(meta.first_ts))
        : '—'} (every event the store holds)`],
    ['duration basis', meta.calendar && meta.calendar.hours_mode === 'business'
      ? `business hours — ${meta.calendar.working_hours?.default || '24/7'} ${meta.calendar.timezone || ''}, holidays and vacations excluded`
      : 'wall-clock hours; no working-hours calendar configured'],
  ];
  // prd/63 F27/V-B: a combined tenant is built from several ingest runs. List
  // each source, not only the latest run's name.
  // prd/70 F-15: per-source columns — issues for the Jira run, PRs for the PR
  // run — plus the events each contributed. The row never claims a zero count
  // for a layer its source does not have.
  const runs = Array.isArray(meta.runs) ? meta.runs : [];
  const pair = (group) => {
    if (!group) return '—';
    const kept = group.kept == null ? '—' : fmtCount(group.kept);
    const seen = group.seen == null ? '—' : fmtCount(group.seen);
    return `${kept} kept of ${seen} seen`;
  };
  const sourceTable = runs.length
    ? `<table class="table compact source-runs"><thead><tr>
        <th>source</th><th>snapshot</th><th class="num">issues</th>
        <th class="num">PRs</th><th class="num">dropped</th><th class="num">events</th>
      </tr></thead><tbody>${
        runs.map((run) => {
          const row = sourceRunRow(run, meta, state.counts);
          return `<tr data-source="${esc(run.source)}">
          <td class="mono">${esc(row.source)}</td>
          <td class="mono">${row.as_of ? esc(fmtStamp(Date.parse(row.as_of))) : '—'}</td>
          <td class="num">${pair(row.issues)}</td>
          <td class="num">${pair(row.prs)}</td>
          <td class="num">${row.dropped == null ? '—' : fmtCount(row.dropped)}</td>
          <td class="num">${row.events == null ? '—' : fmtCount(row.events)}</td></tr>`;
        }).join('')}</tbody></table>
      <p class="panel-note">${esc(sourcesRunNote(meta))}</p>`
    : '';
  $('sources-body').innerHTML = `<dl class="facts">${rows.map(([key, value]) =>
    `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('')}</dl>
    ${sourceTable}
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

// ---------------------------------------------------------------- titles (prd/80)
/* The feature's page wiring, in one place. What it is built around:
 *
 *   - **No title ever transits megaspeed.** There is no title request from this
 *     file at all: the only network client is the panel's transport C, which
 *     calls the configured Jira site or `api.atlassian.com` and nothing else.
 *     The cache lives in IndexedDB, the render is `textContent`, and the export
 *     paths (Markdown/CSV) never call the hydrator.
 *   - **One deterministic call per render**, never a `MutationObserver`:
 *     `renderTitles()` at the end of every view render and after a sort/filter
 *     repaint.
 *   - **Off means off:** with the toggle off, `renderTitles()` removes the
 *     annotations and returns before reading a single title, so no summary text
 *     is in the DOM.
 *
 * A browser that never imported anything renders exactly what it rendered
 * before this feature existed: no node, no request.
 */

let titlesPanel = null;
let titlesPopover = null;

/** Every ticket key this tenant's store knows: coverage, and the fetch scope. */
function titleKeySet() {
  const keys = new Set();
  const add = (value) => {
    const key = normalizeKey(value);
    if (key) keys.add(key);
  };
  for (const obj of state.objects || []) {
    for (const key of obj.ticket_keys || []) add(key);
    const id = String(obj.object_id || '');
    const at = id.indexOf(':');
    if (at >= 0) add(id.slice(at + 1));
  }
  for (const key of state.epicKeys || []) add(key);
  for (const row of (state.delivery && state.delivery.rows) || []) {
    for (const key of row.tickets || []) add(key);
  }
  for (const row of (state.sprints && state.sprints.rows) || []) add(row.key);
  if (state.ticketKey) add(state.ticketKey);
  return [...keys].sort();
}

/** The top-bar kill switch, beside the theme toggle (prd/80 §The toggle). */
function renderTitlesToggle() {
  const button = $('titles-toggle');
  if (!button) return;
  const on = state.titlesEnabled;
  const label = on
    ? 'titles on — click to hide ticket titles'
    : 'titles off — click to show ticket titles';
  button.textContent = 'Tt';
  button.setAttribute('aria-pressed', String(on));
  button.setAttribute('aria-label', label);
  button.title = label;
  button.classList.toggle('on', on);
}

/**
 * The cache → DOM hop. Called by `renderTitles()` only.
 *
 * `loadTitles()` reads the tenant's cached rows from IndexedDB (a local read,
 * not a request). It then renders. Nothing here can reach the network.
 */
async function loadTitles() {
  const tenant = state.tenant;
  const generation = state.titlesSeq;
  if (!tenant) {
    state.titles = new Map();
    state.titlesTenant = null;
    renderTitles();
    return;
  }
  try {
    if (!state.titlesStore) state.titlesStore = await openCache();
    const map = await getTitles(tenant, titleKeySet(), state.titlesStore);
    // A tenant switch or the off switch while the read was in flight: the rows
    // belong to nobody on screen, so the paint is dropped.
    if (state.tenant !== tenant || generation !== state.titlesSeq) return;
    state.titles = map;
    state.titlesTenant = tenant;
  } catch {
    if (generation !== state.titlesSeq) return;
    state.titles = new Map();
    state.titlesTenant = tenant;
  }
  renderTitles();
}

/**
 * Annotate what is on screen. The ticket keys stay the only visible text in an
 * inline table cell; only the epic page's ticket list asks for the muted second
 * line (`data-ticket-line`), so column widths and row heights do not move.
 */
function renderTitles() {
  const page = $('page-title');
  // A render replaces the anchors the popover was anchored to, and a closed
  // popover keeps no title text (rule 8).
  if (titlesPopover) titlesPopover.close();
  if (!state.titlesEnabled) {
    // Rule 8: off means off. Remove every annotation and return before a
    // single title is read — not even a Map lookup.
    hydrateTitles(document.getElementById('main'), { enabled: false });
    annotateDetailTitle(page, { enabled: false });
    return;
  }
  hydrateTitles(document.getElementById('main'), {
    enabled: true,
    titles: state.titles,
  });
  // The ticket page keeps the key as its h1 and adds the summary as a subtitle,
  // so that page answers "where did the time go for *this* ticket".
  const key = state.page === 'ticket' ? String(state.ticketKey || '').toUpperCase() : '';
  annotateDetailTitle(page, {
    key,
    title: key && state.titles.size ? state.titles.get(key) || null : null,
    enabled: true,
  });
}

/** Coverage/freshness straight from the hydrated map (the panel's status block). */
function titleStatus() {
  const keys = titleKeySet();
  const rows = [...state.titles.entries()].map(([key, value]) => ({ key, ...value }));
  const view = titleCoverage(rows, keys);
  let oldest = null;
  let newest = null;
  for (const row of rows) {
    if (!row.fetched_at) continue;
    if (oldest == null || row.fetched_at < oldest) oldest = row.fetched_at;
    if (newest == null || row.fetched_at > newest) newest = row.fetched_at;
  }
  const now = Date.now();
  const phrase = (stamp) => {
    const days = ageDays(stamp, now);
    return days == null ? '' : formatAge(days);
  };
  const oldestDays = ageDays(oldest, now);
  return {
    coverage: view,
    keys,
    oldest,
    newest,
    oldestText: phrase(oldest),
    newestText: phrase(newest),
    stale: oldestDays != null && oldestDays > TITLE_TTL_DAYS,
  };
}

/** Repaint the status lines after a merge, without rebuilding the inputs. */
function paintTitleStatus() {
  const status = titleStatus();
  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  setText('titles-coverage', status.coverage.text);
  setText('titles-freshness', [status.oldestText ? `oldest ${status.oldestText}` : '',
    status.newestText ? `newest ${status.newestText}` : ''].filter(Boolean).join(' · ')
    || `no title cached for ${state.tenant || 'this dataset'}`);
}

/**
 * The `?view=titles` body: the importer, the status board and the probe bench.
 * `titlesMarkup()` is pure; the interactions were bound once to the container by
 * `wireTitlesPanel()`, so replacing this innerHTML never loses a handler.
 */
function renderTitlesPage() {
  const body = $('titles-body');
  if (!body) return;
  const status = titleStatus();
  const keys = status.keys;
  const origin = (() => {
    try { return window.location.origin; } catch { return ''; }
  })();
  const href = state.titlesSite
    ? bookmarkletCode({ site: state.titlesSite, targetOrigin: origin, keys })
    : '';
  let note = '';
  if (!state.titlesSite) {
    note = 'Set the Jira site in step 3 first — the bookmarklet bakes it in, '
      + 'along with this dataset\'s ticket keys.';
  } else if (!keys.length) {
    note = 'This dataset has no ticket keys yet, so the snippet would fetch nothing. '
      + 'Open a dataset with stories or epics first.';
  } else {
    note = `The snippet carries ${keys.length} key${keys.length === 1 ? '' : 's'} and requests `
      + 'summary and issuetype only. Open Jira, click the bookmark, and this panel receives '
      + 'the rows (origin-checked) and merges them.';
  }
  body.innerHTML = titlesMarkup({
    enabled: state.titlesEnabled,
    tenant: state.tenant || '',
    site: state.titlesSite,
    cloudId: state.titlesCloudId,
    coverageText: status.coverage.text,
    keyCount: keys.length,
    oldestText: status.oldestText,
    newestText: status.newestText,
    stale: status.stale,
    bookmarkletHref: href,
    bookmarkletNote: note,
  });
}

/** One call from every render tail: repaint the toggle, then hydrate. */
function renderTitlesChrome() {
  renderTitlesToggle();
  renderTitles();
}

/** The topbar switch; the panel mirrors it and vice versa. */
function setTitlesEnabled(enabled, { save = true } = {}) {
  state.titlesEnabled = Boolean(enabled);
  // A pending transport-C fetch must not paint after the switch: bump the
  // generation and the merge callback drops its render.
  state.titlesSeq += 1;
  if (save) storeTitlesPref(state.titlesEnabled);
  renderTitlesToggle();
  // Paint from the in-memory map immediately (the cache is not cleared by the
  // switch), then re-read it — a first enable on a fresh tab has nothing yet.
  renderTitles();
  if (state.titlesEnabled) loadTitles();
  if (state.page === 'titles') renderTitlesPage();
  syncUrl();
}

/** The panel's dependencies. The store is opened lazily, once per session. */
async function titlesStore() {
  if (!state.titlesStore) state.titlesStore = await openCache();
  return state.titlesStore;
}

function initTitles() {
  // The stored preference first: a browser that hid titles sees keys only, even
  // before any data loads.
  state.titlesEnabled = titlesEnabled(readTitlesPref());
  const config = readTitlesConfig();
  state.titlesSite = config.site;
  state.titlesCloudId = config.cloudId;
  renderTitlesToggle();

  // ONE delegated popover for the whole document (hover and focus), keyed by
  // `data-ticket`, so a re-render costs nothing.
  titlesPopover = wireTitlePopover(document, {
    isEnabled: () => state.titlesEnabled,
    titleFor: (key) => (state.titlesEnabled ? state.titles.get(key) || null : null),
  });

  // The bookmarklet handshake: merge only a message from the configured Jira
  // origin whose payload has the agreed shape. Anything else is ignored.
  listenTitlesMessage(window, {
    site: () => state.titlesSite,
    onRows: async (rows) => {
      const merged = await mergeTitles(state.tenant, rows, await titlesStore());
      await loadTitles();
      if (state.page === 'titles') {
        paintTitleStatus();
        const line = document.getElementById('titles-import-line');
        if (line) {
          line.textContent = `bookmarklet: imported ${merged.imported} · skipped ${merged.skipped}`
            + ` · ${merged.unchanged} unchanged`;
        }
      }
    },
  });

  const body = $('titles-body');
  if (body) {
    titlesPanel = wireTitlesPanel(body, {
      store: () => titlesStore(),
      fetch: typeof fetch === 'function' ? fetch : null,
      state: () => ({
        tenant: state.tenant || '',
        enabled: state.titlesEnabled,
        site: state.titlesSite,
        cloudId: state.titlesCloudId,
        token: state.titlesToken,
        keys: titleKeySet(),
        expiresAt: state.titlesExpiry,
        targetOrigin: (() => {
          try { return window.location.origin; } catch { return ''; }
        })(),
      }),
      onToggle: (enabled) => setTitlesEnabled(enabled),
      onConfig: ({ site, cloudId }) => {
        state.titlesSite = String(site || '').trim();
        state.titlesCloudId = String(cloudId || '').trim();
        storeTitlesConfig({ site: state.titlesSite, cloudId: state.titlesCloudId });
        if (state.page === 'titles') renderTitlesPage();
      },
      onToken: ({ token, expiresAt }) => {
        state.titlesToken = String(token || '');
        state.titlesExpiry = Number(expiresAt) || 0;
      },
      onMerged: async () => {
        // The rows are already merged; this only re-reads the cache and
        // re-hydrates. When titles are off nothing is painted: the generation
        // check inside loadTitles() drops a stale paint.
        const generation = state.titlesSeq;
        await loadTitles();
        if (generation !== state.titlesSeq) return;
        if (state.titlesEnabled && state.page === 'titles') paintTitleStatus();
      },
      onForget: async () => {
        state.titles = new Map();
        renderTitles();
        paintTitleStatus();
      },
    });
  }
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

/* prd/64 F-12: the drawer answers two different questions that used to share
 * keys: the live status (`waiting on author: yes`, `CI: not running`) and the
 * measured phase spans (`waiting on author · measured: —`). The spans sit under
 * a PHASES subheading with distinct keys, so no key appears twice and the
 * unobserved tail explains why a `yes` can sit beside a measured `—`. */
const MEASURED_PHASES = ['waiting', 'active', 'review', 'ci', 'rebasing', 'unobserved'];

function measuredPhaseFacts({ hoursOf, benchmarks = {}, calendar = false }) {
  return [
    ['PHASES', calendar ? 'measured spans · calendar time' : 'measured spans · business hours'],
    ...MEASURED_PHASES.map((phase) => [
      phase === 'unobserved'
        ? 'unobserved (no events since the snapshot)'
        : `${PHASE[phase].label} · measured`,
      hoursOf(phase),
      benchmarks[phase] || null,
    ]),
  ];
}

function openPrDrawer(pr, row = null) {
  // A timeline click has no row in hand; the delivery payload usually knows the
  // same PR, and its review facts are what make the drawer complete (M6).
  if (!row) row = findDeliveryRow(pr.id);
  const segments = timeline.lifecycle(pr);
  const totals = timeline.phases(pr);
  const cycle = segments.length ? segments[segments.length - 1].to - segments[0].from : 0;
  // prd/43 §5: the phase spans come from the *server's* business-hour intervals
  // when the delivery row carries them, exactly the numbers the ticket-page
  // drawer shows. Calendar-time phases stay as the fallback for a PR outside the
  // delivery payload, and the clock note says which one is on screen.
  const phaseHours = row && row.phase_hours && Object.keys(row.phase_hours).length
    ? row.phase_hours : null;
  // prd/42 M6: the drawer is complete — reviewers, approvals, open threads and
  // the last event are facts, not dashes. prd/42 P6: the cycle is one canonical
  // duration (business hours) when the delivery row can price it, never a
  // second "calendar days" spelling of the same number.
  const cycleHours = row && row.age_hours != null
    ? fmtHours(row.age_hours)
    : (pr.cycle_hours != null ? fmtHours(pr.cycle_hours) : null);
  const facts = [
    ['ticket', pr.ticket || '—'],
    ...(pr.epic ? [['epic', pr.epic]] : []),
    ['author', pr.actor],
    ['state', pr.state + (pr.closed ? '' : ' · right-censored')],
    ...(row ? [
      ['reviewers', row.reviewers_total
        ? `${row.reviewers_approved || 0}/${row.reviewers_total} approved · ${row.reviewers_pending || 0} pending`
        : 'no roster in the store'],
      ['approvals', approvalsLabel(row)],
      ['waiting on', waitingLabel(row) || '—'],
      // prd/56 §1: the drawer says how long *this* wait has run, not only where
      // the PR is now. The current wait starts when the last event handed the
      // ball over; it is priced in business hours like every other duration.
      ['waiting since', row.waiting_since
        ? `${fmtStamp(Date.parse(row.waiting_since))} (${fmtHours(row.waiting_hours)})`
        : '—'],
      ['waiting', row.waiting_hours == null ? '—' : fmtHours(row.waiting_hours)],
      ['phase', row.phase || '—'],
      ['open threads', String(row.open_threads ?? 0)],
      ['last event', row.last_event || row.waiting_since
        ? `${fmtStamp(Date.parse(row.last_event || row.waiting_since))} · ${row.last_event_activity || row.kind || ''}`
        : '—'],
    ] : []),
    ...(cycleHours
      ? [['cycle (business hours)', cycleHours]]
      : [['cycled', `${fmtCalendarDuration(cycle)} (calendar time)`]]),
    // prd/43 §5: the row's count is the PR's COMMIT objects — the same count
    // the ticket-page drawer and the size rule read.
    ['commits', String(row && row.commits != null
      ? row.commits
      : (pr.commit_count ?? (pr.commits ? pr.commits.length : 0)))],
    ...(row ? [
      // prd/64 F-12: live status only. The duration belongs to the measured
      // span below (and, for #66, to the unobserved tail, not a wait phase).
      ['waiting on author', row.author_open ? 'yes' : 'no'],
      ['CI', row.ci ? 'running' : 'not running'],
      ['rebasing', row.rebasing ? 'yes' : 'no'],
      ['no events since', row.unobserved
        ? fmtStamp(Date.parse(row.unobserved_since || row.waiting_since))
        : 'events observed through the last event'],
    ] : []),
    ...measuredPhaseFacts({
      hoursOf: (phase) => (phaseHours
        ? (phaseHours[phase] != null ? fmtHours(phaseHours[phase]) : '—')
        : (totals.get(phase) ? fmtCalendarDuration(totals.get(phase)) : '—')),
      benchmarks: (row && row.phase_benchmarks) || {},
      calendar: !phaseHours,
    }),
    ...(phaseHours ? [['phase total', fmtHours(Object.values(phaseHours).reduce(
      (sum, value) => sum + (Number(value) || 0), 0))]] : []),
  ];
  openDrawer({
    overline: 'PULL REQUEST',
    ticket: pr.ticket || null,
    pr,
    facts,
    // prd/35: state the clock once, where the numbers are.
    clockNote: phaseHours
      ? 'timestamps are UTC · cycle, wait and phase spans are business hours on the team calendar'
      : 'timestamps are UTC · cycle, wait and age are business hours on the team calendar; ' +
        'the phase spans are elapsed calendar time',
    children: [pr.ticket, pr.epic, `${pr.repo}#${pr.number}`].filter(Boolean),
    impact: pr.closed ? 'Closed: its wait is history, not a live bottleneck.' : undefined,
  });
}

function openDrawer(entity) {
  state.drawerEntity = entity;
  // prd/63 F19: remember what had focus, move focus into the dialog, and put it
  // back on close — a row click used to leave focus behind the drawer.
  if (typeof document !== 'undefined') {
    const active = document.activeElement;
    state.drawerOpener = active && active !== document.body ? active : null;
  }
  const drawer = $('drawer');
  drawer.innerHTML = `<div class="sheet-handle" aria-hidden="true"></div>` +
    `<button type="button" class="drawer-close" data-drawer="close" aria-label="close">×</button>` +
    detailMarkup(entity, { benchmark: state.benchmark });
  drawer.hidden = false;
  // prd/42 B2: the drawer is beside the content, not on top of the list
  // inspector. The class lets the shell reserve its width on desktop.
  if (typeof document !== 'undefined' && $('app')) $('app').classList.add('drawer-open');
  // prd/63 F19: the drawer is a dialog; focus lands in it, not on the row.
  if (typeof drawer.focus === 'function') drawer.focus({ preventScroll: true });
  // prd/48: an open PR is the deepest level of the YAH path.
  renderYah();
}

/* prd/38 #12: a ticket's PR row is a doorway, not a dead end. The ticket payload
 * carries the server's intervals, so the drawer can price the PR without the
 * timeline's event port (and without colliding with its lifecycle cache). */
function openTicketPrDrawer(pr) {
  if (!pr) return;
  const key = state.ticketKey;
  const phases = new Map();
  for (const interval of pr.intervals || []) {
    const hours = Number(interval.hours) || 0;
    if (hours <= 0) continue;
    // prd/55 §7: the unobserved tail is a real span; include it so the phase
    // list sums to the PR's cycle instead of stopping at the last event.
    phases.set(interval.phase, (phases.get(interval.phase) || 0) + hours);
  }
  const phaseTotal = [...phases.values()].reduce((sum, value) => sum + value, 0);
  const classes = Object.entries(pr.push_classes || {})
    .map(([name, count]) => `${name} ×${count}`).join(' · ') || '—';
  const facts = [
    ['ticket', key || '—'],
    ['author', pr.author || '—'],
    ['state', `${pr.state}${pr.censored ? ' · right-censored' : ''}`],
    // prd/55 §7: the phase total is the cycle the phase list sums to.
    ['cycle (sum of phases)', phaseTotal ? fmtHours(phaseTotal) : '—', pr.cycle_benchmark],
    // prd/42 M6: the review facts the ticket payload now carries.
    ['reviewers', pr.reviewers_total
      ? `${pr.reviewers_approved || 0}/${pr.reviewers_total} approved · ${pr.reviewers_pending || 0} pending`
      : 'no roster in the store'],
    ['approvals', approvalsLabel(pr)],
    ['waiting on', (pr.waiting_on || []).join(' + ') || 'nobody'],
    ['waiting since', pr.waiting_since
      ? `${fmtStamp(Date.parse(pr.waiting_since))} (${fmtHours(pr.waiting_hours)})`
      : '—'],
    ['waiting', pr.waiting_hours == null ? '—' : fmtHours(pr.waiting_hours)],
    ['open threads', String(pr.open_threads ?? 0)],
    ['last event', pr.last_event
      ? `${fmtStamp(Date.parse(pr.last_event))} · ${pr.last_event_activity || ''}`
      : '—'],
    ['commits', String(pr.commit_count ?? 0)],
    ['review rounds', String(pr.review_rounds ?? 0)],
    ['push classes', classes],
    ['waiting on author', pr.author_open ? 'yes' : 'no'],
    ['CI', pr.ci ? 'running' : 'not running'],
    ['rebasing', pr.phase === 'rebasing' ? 'yes' : 'no'],
    ['no events since', pr.censored && pr.last_event
      ? `${fmtStamp(Date.parse(pr.last_event))} · ${pr.phase}`
      : 'events observed through the last event'],
    ...measuredPhaseFacts({
      hoursOf: (phase) => (phases.get(phase) ? fmtHours(phases.get(phase)) : '—'),
      // prd/57: each drawer phase is read against the merged-PR cohort.
      benchmarks: pr.phase_benchmarks || {},
    }),
    ['phase total', phaseTotal ? fmtHours(phaseTotal) : '—'],
  ];
  openDrawer({
    overline: 'PULL REQUEST',
    // prd/43 §3: the ticket-page drawer names its ticket at the top level too,
    // so the Timeline chip scopes even if the PR object lost its key.
    ticket: key,
    pr: {
      id: pr.object_id,
      object_id: pr.object_id,
      number: pr.number,
      repo: pr.repo,
      repoFull: pr.repo,
      ticket: key,
      actor: pr.author,
      state: pr.state,
      closed: pr.state !== 'open',
      created: Date.parse(pr.opened),
      commits: [],
      commit_count: pr.commit_count ?? 0,
      intervals: pr.intervals,
    },
    facts,
    clockNote: 'timestamps are UTC · these spans are business hours on the team calendar',
    children: [key, `${pr.repo}#${pr.number}`].filter(Boolean),
  });
}

/* prd/38 #12: a delivery row opens the same PR drawer. The timeline holds the
 * full PR (events, commits, lifecycle) when it is loaded; a row outside the
 * loaded slice falls back to the row's own facts instead of a dead click. */
function openDeliveryPrDrawer(row) {
  if (!row) return;
  const pr = timeline.prs.find((candidate) => candidate.id === row.object_id);
  if (pr) {
    openPrDrawer(pr, row);
    return;
  }
  openDrawer({
    overline: 'PULL REQUEST',
    row,
    facts: [
      ['ticket', rowTicket(row) || '—'],
      ['author', row.author || '—'],
      ['state', row.state || 'open'],
      ['reviewers', row.reviewers_total
        ? `${row.reviewers_approved || 0}/${row.reviewers_total} approved · ${row.reviewers_pending || 0} pending`
        : 'no roster in the store'],
      ['approvals', approvalsLabel(row)],
      ['waiting on', waitingLabel(row)],
      ['open threads', String(row.open_threads ?? 0)],
      ['last event', row.last_event || row.waiting_since
        ? `${fmtStamp(Date.parse(row.last_event || row.waiting_since))} · ${row.last_event_activity || row.kind || ''}`
        : '—'],
      ['waiting since', `${fmtStamp(Date.parse(row.waiting_since))} (${fmtHours(row.waiting_hours)})`],
      // prd/57: the wait and each measured phase are read against their cohorts.
      ['waiting', fmtHours(row.waiting_hours), row.wait_benchmark],
      // prd/64 F-12: the measured spans are their own group with distinct keys,
      // and the live status above stays the only bare `waiting on author`.
      ...measuredPhaseFacts({
        hoursOf: (phase) => (row.phase_hours && row.phase_hours[phase] != null
          ? fmtHours(row.phase_hours[phase]) : '—'),
        benchmarks: row.phase_benchmarks || {},
      }),
      ['age', fmtHours(row.age_hours)],
      ['waiting on author', row.author_open ? 'yes' : 'no'],
      ['CI', row.ci ? 'running' : 'not running'],
      ['rebasing', row.rebasing ? 'yes' : 'no'],
      ['no events since', row.unobserved
        ? fmtStamp(Date.parse(row.unobserved_since || row.waiting_since))
        : 'events observed through the last event'],
      ['commits in log', String(row.commits ?? 0)],
    ],
    impact: impactSentence(row),
    children: [rowTicket(row), row.id].filter(Boolean),
  });
}

function closeDrawer() {
  const wasOpen = !$('drawer').hidden;
  const opener = state.drawerOpener;
  state.drawerEntity = null;
  state.drawerOpener = null;
  $('drawer').hidden = true;
  if (typeof document !== 'undefined' && $('app')) $('app').classList.remove('drawer-open');
  // prd/63 F19: Esc or a click behind the drawer returns focus to the row (or
  // button) that opened it, when it is still on screen.
  if (wasOpen && opener && typeof opener.focus === 'function'
    && document.contains(opener) && (opener.offsetParent !== null || opener === document.body)) {
    opener.focus({ preventScroll: true });
  }
  // prd/48: the PR level leaves the YAH path with the drawer.
  renderYah();
}

// --------------------------------------------------------------- pivot menu
function openMenu(x, y, entity, label) {
  const menu = $('menu');
  state.menuEntity = entity;
  // prd/56 §2: "Scope to" must offer the *holder* (the person the item waits
  // on), not only its author. On #53 the useful pivot is FrMo/CaOs, never the
  // author — and a row with several holders offers each, never a made-up role.
  const items = [
    ...scopeTargets(entity).map((target) => ['person', target.label, target.person]),
    ['timeline', 'Open in timeline'],
    ['copy', 'Copy as Markdown'],
    ['sep', ''],
    ['clear', 'Clear scope · whole team'],
  ].filter(Boolean);
  menu.innerHTML = `<div class="menu-head">${esc(label || 'pivot')}</div>` + items.map(([action, text, person]) =>
    action === 'sep' ? '<div class="sep"></div>'
      : `<button type="button" data-menu="${action}"${
        person ? ` data-person="${esc(person)}"` : ''}>${esc(text)}</button>`).join('');
  menu.hidden = false;
  const width = menu.offsetWidth || 232;
  const height = menu.offsetHeight || 140;
  // prd/56 §2: the menu is `position: fixed` (style.css .menu) and opens at the
  // pointer, clamped so it never leaves the viewport.
  menu.style.left = `${Math.max(8, Math.min(x, window.innerWidth - width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(y, window.innerHeight - height - 8))}px`;
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
/** Roles this dataset actually has a queue/owner for (no dead-end options). */
function availableRoles() {
  const seen = new Set(['author', 'review', 'ready']);
  const events = state.events || [];
  if (events.some((event) => event.activity === 'build_status')) seen.add('CI');
  if (events.some((event) => (event.attributes || {}).sentinel === true)) seen.add('sentinel');
  // a queue stream or a delivery row can name a role even when no event did
  for (const stream of (state.queues && state.queues.streams) || []) {
    seen.add(String(stream.id));
  }
  for (const row of (state.delivery && state.delivery.rows) || []) {
    if (row.ball) seen.add(String(row.ball));
    for (const role of Object.values(row.waiting_roles || {})) seen.add(String(role));
  }
  return ROLE_QUEUES.filter((role) => seen.has(role));
}

/** Repositories this tenant actually has PRs for (prd/49: no dead-end options). */
function availableRepos() {
  const repos = new Set();
  for (const object of state.objects || []) {
    if (object.object_type === 'PR' && object.repo) repos.add(String(object.repo));
  }
  for (const event of state.events || []) {
    if (event.repo) repos.add(String(event.repo));
  }
  return [...repos].sort();
}

/**
 * prd/70 F-02: every person this store can scope to, not just the loaded slice.
 *
 * `state.events` is the newest 40,000 rows of the store; on the combined corpus
 * that hid 75 historical actors (`DmMa`, `IvZi`) and every reviewer who only
 * appears as a link target (`PERSON:LeMi2`). The roster is the union of the
 * store's PERSON objects, STORY/TICKET/PR owners, queue streams, aging holders
 * and the loaded events, so a deep link resolves without loading all events.
 *
 * Returns `name → loaded-event count` (0 when none of their events are in the
 * slice), so the select can still rank the busiest people first.
 */
function personRoster() {
  const counts = new Map();
  for (const event of state.events || []) {
    const actor = String(event.actor ?? '').trim();
    if (actor) counts.set(actor, (counts.get(actor) || 0) + 1);
  }
  const people = new Set();
  const add = (value) => {
    const name = String(value ?? '').trim();
    if (!name || name === 'system' || name === 'null' || name === 'undefined') return;
    people.add(name);
  };
  for (const object of state.objects || []) {
    if (!object) continue;
    // concepts.md §2: a bot's PERSON row (actor_kind "bot") is not a person.
    if (object.object_type === 'PERSON' && object.attributes
      && object.attributes.actor_kind === 'bot') continue;
    if (object.object_type === 'PERSON' || object.object_type === 'STORY'
      || object.object_type === 'TICKET' || object.object_type === 'PR') {
      add(object.actor);
    }
  }
  for (const stream of (state.queues && state.queues.streams) || []) {
    if (!stream) continue;
    if (!streamIsQueue(stream)) add(stream.id);
    for (const person of stream.people || []) add(person);
  }
  for (const row of (state.aging && state.aging.rows) || []) {
    if (!row) continue;
    for (const holder of row.holder || []) {
      add(typeof holder === 'string' ? holder : holder && holder.person);
    }
    for (const person of row.waiting_on || []) add(person);
    for (const person of Object.keys(row.waiting_roles || {})) add(person);
  }
  for (const event of state.events || []) add(event.actor);
  for (const bot of (state.meta && state.meta.bots) || []) people.delete(bot);
  const roster = new Map();
  for (const name of people) roster.set(name, counts.get(name) || 0);
  return roster;
}

function buildScopeOptions() {
  // prd/70 F-02: the roster, not the 40k event slice (see `personRoster`).
  const roster = personRoster();
  const options = [['all:all', 'everything']];
  [...roster.entries()]
    .sort((a, b) => (b[1] - a[1]) || a[0].localeCompare(b[0]))
    .forEach(([actor]) => options.push([`person:${actor}`, `person ${actor}`]));
  // Every role queue this dataset can show is a scope option, so a role scope
  // set from the queue chart or a graph node always has a matching <option>
  // (B4) — and a role with no data is not offered as a dead end.
  // prd/63 F15: a role that filters no view (`queue ready`) was a selectable
  // control with zero effect; only offer roles some view actually applies.
  options.push(...availableRoles()
    .filter((role) => filteredPages({ kind: 'role', value: role }).size > 0)
    .map((role) => [`role:${role}`, `queue ${role}`]));
  // prd/49: a repo is a first-class scope dimension beside person and role. The
  // option value is the store's full repo string (the API filter's exact
  // match); the label is the short name people say.
  options.push(...availableRepos().map((repo) => [`repo:${repo}`, `repo ${repoLabel(repo)}`]));
  // prd/38 #20: a ticket scope set by the "Timeline" chip is a real option, so
  // the select can show it (and the user can clear it like any other scope).
  if (state.scope.kind === 'ticket' && state.scope.value) {
    options.splice(1, 0, [`ticket:${state.scope.value}`, `ticket ${state.scope.value}`]);
  }
  $('scope').innerHTML = options.map(([value, label]) =>
    `<option value="${esc(value)}">${esc(label)}</option>`).join('');
  $('scope').value = `${state.scope.kind}:${state.scope.value || 'all'}`;
}

/**
 * prd/68 F-16: a scope is a closed kind *with* a value, or the whole team.
 *
 * The `#scope` select's own change handler split its value on `:`; a value
 * outside the options (a stale option string, an empty selection) arrived as
 * `setScope('', undefined)` and passed validation, so the banner read
 * "the undefined queue". Anything the router does not name is `all`.
 */
function normalizeScope(kind, value) {
  const k = String(kind == null ? '' : kind).trim();
  const v = value == null ? '' : String(value).trim();
  if (k === 'all' || !SCOPE_KINDS.includes(k) || !v) {
    return { kind: 'all', value: null, label: 'everything' };
  }
  return { kind: k, value: v, label: v };
}

function setScope(kind, value, { silent = false } = {}) {
  // prd/68 F-16: coerce before anything reads the scope (the banner, the page
  // filter, the URL), so no view can render "the undefined queue".
  ({ kind, value } = normalizeScope(kind, value));
  // prd/64 F-09: a scope edit answers a new question, so a boot/route fallback
  // notice no longer describes the screen. A route restore is not an edit.
  if (!silent) setNotice('');
  const wasRepo = state.scope.kind === 'repo';
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
  // prd/49: the repo scope is answered by the API (Rules and the Lab recompute),
  // so entering or leaving it must refetch the current view — a person/role
  // scope is applied client-side and does not.
  const nowRepo = state.scope.kind === 'repo';
  if (wasRepo || nowRepo) {
    state.cache.clear();
    ensureData(state.page)
      .then(() => renderPage())
      .catch((err) => setStatus(`scope failed: ${err.message}`, 'stale'));
  }
  if (!silent) {
    if (!valid) flashStatus('that scope is not in this dataset — showing everything', 'stale');
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
  if (state.scope.kind === 'person') {
    return state.events.filter((event) => event.actor === state.scope.value);
  }
  // prd/49: a repo scope is the PR's repository — every event carries it.
  if (state.scope.kind === 'repo') {
    return state.events.filter((event) => String(event.repo || '') === String(state.scope.value));
  }
  if (state.scope.kind === 'ticket') {
    const key = String(state.scope.value || '').toUpperCase();
    const prIds = ticketPrIds(key);
    const wanted = new Set([`STORY:${key}`, `TICKET:${key}`]);
    return state.events.filter((event) => (event.objects || [])
      .some((id) => wanted.has(id) || prIds.has(id)));
  }
  return state.events;
}

/** The store's PR object ids that name ``key`` (prd/38 #20 ticket scope). */
function ticketPrIds(key) {
  const wanted = String(key || '').toUpperCase();
  const ids = new Set();
  for (const object of state.objects || []) {
    if (object.object_type !== 'PR') continue;
    if ((object.ticket_keys || []).some((ticket) => String(ticket).toUpperCase() === wanted)) {
      ids.add(object.object_id);
    }
  }
  return ids;
}

/** A person scope is only meaningful inside one tenant's identity space. */
function validateScope() {
  // prd/68 F-16: an unknown kind (or a non-`all` kind with no value) is not a
  // scope at all — coerce it to the whole team, so no label can read
  // "the undefined queue".
  const normalized = normalizeScope(state.scope.kind, state.scope.value);
  if (normalized.kind !== state.scope.kind
    || normalized.value !== state.scope.value) {
    state.scope = normalized;
    return normalized.kind === 'all';
  }
  const bots = new Set((state.meta && state.meta.bots) || []);
  // prd/70 F-02: validate against the store's full roster (PERSON objects,
  // story/PR owners, queue streams, aging holders and the loaded events) — the
  // 40k event slice is not the identity space. Before any data has arrived the
  // roster cannot answer, so the scope is kept and `load()` re-validates it
  // once the objects are in (a `person:` deep link is not a bad link).
  const roster = personRoster();
  const personReady = roster.size > 0 || state.events.length > 0;
  const person = state.scope.kind === 'person'
    && (state.scope.value === 'system'
      || bots.has(state.scope.value)
      || (personReady && !roster.has(state.scope.value)));
  const role = state.scope.kind === 'role'
    && !ROLE_QUEUES.some((queue) => queue.toLowerCase() === String(state.scope.value || '').toLowerCase());
  const ticket = state.scope.kind === 'ticket'
    && !String(state.scope.value || '').trim();
  // prd/49: a repo scope is valid only when this store has PRs in it.
  const repo = state.scope.kind === 'repo'
    && !availableRepos().includes(String(state.scope.value || ''));
  if (person || role || ticket || repo) {
    state.scope = { kind: 'all', value: null, label: 'everything' };
    return false;
  }
  return true;
}

function scopedObjects(events) {
  if (state.scope.kind === 'all') return state.objects;
  // prd/49: keep the objects that belong to the repo. An object without a repo
  // (a STORY/TICKET/SPRINT) is not a PR-side fact, so it stays for the labels.
  if (state.scope.kind === 'repo') {
    const repo = String(state.scope.value);
    return state.objects.filter((object) => !object.repo || String(object.repo) === repo);
  }
  if (state.scope.kind === 'ticket') {
    const key = String(state.scope.value || '').toUpperCase();
    const prIds = ticketPrIds(key);
    const epics = new Set();
    for (const object of state.objects || []) {
      if (object.object_type !== 'STORY') continue;
      const storyKey = ticketKey(object.object_id) || (object.ticket_keys || [])[0];
      if (String(storyKey || '').toUpperCase() === key) {
        const epic = object.attributes && object.attributes.epic_key;
        if (epic) epics.add(epic);
      }
    }
    return state.objects.filter((object) => {
      if (object.object_type === 'PR') return prIds.has(object.object_id);
      if (object.object_type === 'COMMIT') {
        return prIds.has(`PR:${object.repo}#${object.pr_id}`);
      }
      if (object.object_type === 'STORY' || object.object_type === 'TICKET') {
        const storyKey = ticketKey(object.object_id) || (object.ticket_keys || [])[0];
        return String(storyKey || '').toUpperCase() === key;
      }
      if (object.object_type === 'EPIC') {
        return epics.has(ticketKey(object.object_id) || (object.ticket_keys || [])[0]);
      }
      return false;
    });
  }
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
  // prd/26 P6: bands and story membership are store-wide (a sprint's own scope
  // is not the visible event window), so they are set after the data.
  timeline.setSprints(state.sprints);
  // prd/37 §4: the selected sprint filters the story lanes
  timeline.setSprintFocus(state.sprint);
}

// --------------------------------------------------------------- timeframe
function setTimeframe(id, { silent = false } = {}) {
  if (!TIMEFRAMES[id]) return;
  // prd/64 F-09: choosing a timeframe answers the link's fallback. A route
  // restore passes `silent` so a deep link's notice survives its own state.
  if (!silent) setNotice('');
  // a preset timeframe leaves the sprint lens: the lens is a filter with its
  // own measured range, not a preset (prd/37 §4)
  if (state.sprint != null) {
    state.sprint = null;
    timeline.setSprintFocus(null);
    renderSprintStrip();
  }
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

// ------------------------------------------------------------------- theme
/* prd/10 §3.1: the theme is a first-class alternative, not a skin. The switch
 * is three steps and no data work:
 *   1. `data-theme` on <html> — CSS re-points every token;
 *   2. `refreshPalette()` — the canvas modules re-read the resolved tokens;
 *   3. repaint the current view — legends and segment bars carry inline
 *      colours, and the canvases need one frame at the new palette.
 * Nothing is refetched: the same marks are recoloured. */
function renderThemeButton() {
  const button = $('theme-toggle');
  if (!button) return;
  const light = state.theme === 'light';
  const label = themeLabel(state.theme);
  button.textContent = themeGlyph(state.theme);
  button.setAttribute('aria-pressed', String(light));
  button.setAttribute('aria-label', label);
  button.title = label;
}

function setTheme(theme, { save = true, repaint = true } = {}) {
  const next = normalizeTheme(theme) || DEFAULT_THEME;
  state.theme = applyTheme(next, document.documentElement);
  refreshPalette(document.documentElement);
  renderThemeButton();
  if (save) storeTheme(state.theme);
  if (repaint) {
    timeline.schedule();
    blockingMap.schedule();
    queueChart.schedule();
    // the legends, the ticket segment bar and the activity swatches are HTML
    // with the colour baked into an inline style: re-render them from state
    renderPage();
  }
  return state.theme;
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
    `Snapshot from ${snapshotAge()}: ${meta.events} events, ${meta.prs} PRs in the dataset, ${meta.commits} commits · ` +
    `${meta.source || 'unknown source'}, ${meta.prs_dropped} PRs without a ticket key dropped${capped}`;
  $('nav-count').textContent = `${meta.events} events · ${meta.prs} PRs`;
  const calendar = meta.calendar || {};
  const basis = $('timebasis');
  if (basis) {
    const business = calendar.hours_mode === 'business';
    basis.textContent = business ? 'business hours · 1 d = 8 h' : 'wall-clock hours';
    basis.title = business
      ? `prd/19: ${calendar.assumption || 'business hours'}` +
        ` (${calendar.overrides || 0} overrides, ${calendar.vacations || 0} vacations)`
      : (calendar.error || 'no working-hours calendar configured');
  }
}

/* prd/32: the build identity — the tag + short SHA of the server serving this
 * page. It is code identity, not tenant data, so it is fetched once at boot and
 * never refetched on a tenant switch. A server older than this page has no
 * `/api/meta`; the line then stays hidden (the provenance line already reports
 * an unreachable API). */
async function loadVersion() {
  try {
    state.build = await getJSON('/api/meta');
    renderVersion();
  } catch {
    state.build = null;
  }
}

function renderVersion() {
  const el = $('app-version');
  if (!el) return;
  const label = versionLabel(state.build);
  el.textContent = label;
  el.hidden = !label;
  el.title = versionTitle(state.build);
  // The Sources page lists what the screen is made of; if it is already open,
  // let it pick the build row up too.
  if (state.page === 'sources') renderSourcesPage();
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

/* prd/26 P6: the sprint lens is one small payload for the whole store — the
 * bands must not shrink to the top-bar timeframe, because a sprint's scope is
 * the store's, not the visible event window. A server older than the endpoint
 * (404) or a store without the Jira layer simply draws no sprint furniture. */
async function loadSprints(seq) {
  try {
    const payload = await getJSON(withTenant('/api/sprints'));
    if (seq !== loadSeq) return null;
    state.sprints = payload && payload.available ? payload : null;
  } catch (err) {
    if (seq !== loadSeq) return null;
    state.sprints = null;
  }
  return state.sprints;
}

/* prd/70 F-04: the forecast is evaluated over the active timeframe's window.
 *
 * `/api/forecast` without `days` falls back to the whole store when the tenant
 * has a Jira layer (260 weeks on demo-tlla), so `1q` was refused as "erratic"
 * (all-time CV 0.87) even though the 90-day CV is 0.45 and the forecast is
 * available. The window is the same one every other card asks for.
 */
function forecastQuery({ target = null, by = null, n = null } = {}) {
  const query = new URLSearchParams();
  if (target != null) query.set('target', String(target));
  const days = timeframeDays() ?? allDays();
  if (days) query.set('days', String(days));
  if (by) query.set('by', String(by));
  if (n != null) query.set('n', String(n));
  return query.toString();
}

/* prd/60: the forecast is one endpoint (`/api/forecast`) scoped to the active
 * sprint — the same scope prd/52's at-risk block has (the two entry points open
 * one card). A store with no sprint layer, or a server older than the endpoint,
 * simply draws no card — the sprint strip below still works. The payload is
 * cached server-side, keyed by its own store/calendar stamp, so this is one
 * cheap request per view. */
async function loadForecast(seq) {
  const active = state.sprints ? state.sprints.active : null;
  const sid = active;
  if (sid == null) {
    state.forecast = null;
    state.forecastSprint = null;
    return null;
  }
  const changedSprint = state.forecastSprint !== sid;
  try {
    const query = forecastQuery({ target: `sprint:${sid}` });
    const payload = await getJSON(withTenant(`/api/forecast?${query}`));
    if (seq !== loadSeq) return null;
    state.forecast = payload && payload.ok ? payload : null;
    state.forecastSprint = sid;
    const requested = payload && payload.request ? payload.request.n : null;
    // The target is view state: keep the user's override for the same sprint
    // (the card re-reads the pre-computed quantiles), but a new sprint starts
    // from its own requested scope.
    if (changedSprint || state.forecastTarget == null || requested == null) {
      state.forecastTarget = requested;
    }
  } catch (err) {
    if (seq !== loadSeq) return null;
    state.forecast = null;
    state.forecastSprint = null;
  }
  return state.forecast;
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
    // P-2: with no dataset there is no tenant to name — the bar must not keep
    // a stale one from the URL or the stored preference.
    syncTitle();
    syncUrl();
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
    state.snapshotAge = null;
    renderProvenance();
    // prd/65 N-04: this is the path the annotation write's own `/ws` echo
    // takes, so it must not talk over the "recorded …" confirmation.
    refreshStatus();
    // prd/80: the cache read is local, not a request; it is safe on this path
    // too, so a reload after an import annotates immediately.
    await loadTitles();
    window.__megaspeedReady = true;
    return;
  }

  setEmpty('');
  state.meta = health;
  state.snapshotAge = null;
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
    await loadSprints(seq);
    if (seq !== loadSeq) return;
    await loadForecast(seq);
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
  // prd/63 F14: a `?scope=person:Zed` that this dataset does not know falls
  // back to everything — say so, with the value that was dropped.
  const dropped = `${state.scope.kind} ${state.scope.value || ''}`.trim();
  const scopeKept = validateScope();
  if (!scopeKept && dropped !== 'all') {
    setNotice(
      `Scope “${dropped}” does not exist in dataset ${state.tenant} — showing everything.`,
      { append: true },
    );
  }
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
  // prd/37 §4: a `?sprint=` link waits for the data *and* the bands, then
  // focuses — after setData, so the measured range is not reset to the domain.
  if (state.sprint != null) {
    if (!focusSprint(state.sprint, { sync: false })) state.sprint = null;
  }
  buildScopeOptions();
  renderTenantSwitcher();
  renderProvenance();
  renderLegend();
  renderPhaseLegend();
  renderLoadAll();
  // prd/63 A2: a ticket page owns its own fetch (openTicket) and its own
  // 404/400 state. Fetching it here threw out of load() on an unknown key, so
  // __megaspeedReady never fired and the page sat on "Loading the ticket…".
  if (state.page !== 'timeline' && state.page !== 'ticket') await ensureData(state.page);
  // prd/80: the tenant's cached titles are read once the objects are in (the
  // key set comes from them). IndexedDB, not a request.
  await loadTitles();
  renderPage();
  syncChromeHeight();
  refreshStatus();
  window.__megaspeedReady = true;
}

// ------------------------------------------------------------------ harness
let socket = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${proto}://${location.host}/ws`);

  socket.onopen = () => {
    if (state.meta) refreshStatus();
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
  const elementRect = element.getBoundingClientRect();
  // prd/28: a phone review is a viewport, not a 3,000px scroll: `viewport: true`
  // captures exactly what fits on the screen (fixed sheets included).
  const viewport = options.viewport === true;
  const rect = viewport
    ? { left: 0, top: 0, width: window.innerWidth, height: window.innerHeight }
    : elementRect;

  const out = document.createElement('canvas');
  out.width = Math.max(1, Math.round(rect.width * scale));
  out.height = Math.max(1, Math.round(rect.height * scale));
  const ctx = out.getContext('2d');
  ctx.fillStyle = getComputedStyle(document.body).backgroundColor || color('--bg-app');
  ctx.fillRect(0, 0, out.width, out.height);

  try {
    const image = await domToImage(element, rect, { viewport, elementRect });
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

async function domToImage(element, rect, { viewport = false, elementRect = null } = {}) {
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
  // prd/28: the delivery detail panel is a bottom sheet on a phone, so it is
  // part of the visible screen too. On desktop it stays in the page flow (the
  // pre-phone capture); the drawer and the pivot menu are overlays everywhere.
  const overlays = [document.getElementById('drawer'), document.getElementById('menu')];
  if (isPhone()) overlays.push(document.getElementById('delivery-detail'));
  for (const live of overlays) {
    if (!live || live.hidden || live === element) continue;
    if (getComputedStyle(live).display === 'none') continue;
    const target = clone.id === live.id ? clone : clone.querySelector(`#${live.id}`);
    if (!target) continue;
    const r = live.getBoundingClientRect();
    if (!r.width || !r.height) continue;
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
  const fullRect = viewport && elementRect ? elementRect : rect;
  clone.style.width = `${fullRect.width}px`;
  clone.style.height = `${fullRect.height}px`;
  clone.style.margin = '0';
  if (viewport) {
    // show the part of the page the user is actually looking at; the wrapper
    // (viewport-sized, overflow hidden) does the clipping
    clone.style.marginTop = `${-window.scrollY}px`;
    clone.style.marginLeft = `${-window.scrollX}px`;
  }

  const css = await fetch('/style.css', { cache: 'force-cache' }).then((r) => r.text());
  const wrapper = document.createElement('div');
  wrapper.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
  // the capture inherits the live page's ground and ink, so it is a still frame
  // of the theme the user is looking at, not a hard-coded dark one
  const liveStyle = getComputedStyle(document.body);
  wrapper.setAttribute(
    'style',
    `position:relative;width:${rect.width}px;height:${rect.height}px;` +
    `background:${liveStyle.backgroundColor || color('--bg-app')};` +
    'font-family:Inter,"Segoe UI",Roboto,sans-serif;font-size:13px;' +
    `color:${liveStyle.color || color('--text-primary')};` +
    (viewport ? 'overflow:hidden;' : ''),
  );
  const style = document.createElement('style');
  // Sticky/fixed chrome has no scroll container inside foreignObject; flatten it
  // so the capture lays out like the live page instead of stacking at y=0.
  style.textContent = `${css}
    /* a capture is a still frame: never mid-animation, never mid-transition */
    .shot, .shot * { animation: none !important; transition: none !important; }
    .shot:not(.viewport) .topbar, .shot:not(.viewport) .sidebar, .shot:not(.viewport) .meta,
    .shot:not(.viewport) .table th, .shot:not(.viewport) .detail { position: static !important; }
    .shot .sidebar { height: auto !important; }
    .shot:not(.viewport) .drawer { position: static !important; width: auto; box-shadow: none; }
    .shot .app { min-height: 0 !important; }
    .shot .table th { top: auto !important; }
    /* prd/28: a phone capture is the viewport, not the off-canvas drawer. The
       drawer is painted only when it is open, as an overlay on the page. */
    .shot .scrim { display: none !important; }
    @media (max-width: 640px) {
      .nav-open .shot .scrim {
        display: block !important;
        position: absolute !important;
        inset: 0 !important;
        background: var(--scrim-bg) !important;
        z-index: 20 !important;
      }
      .shot .sidebar { display: none !important; }
      .nav-open .shot .sidebar {
        display: flex !important;
        position: absolute !important;
        left: 0 !important; top: 0 !important; bottom: auto !important;
        width: min(82vw, 312px) !important;
        height: ${rect.height}px !important;
        z-index: 50 !important;
        visibility: visible !important;
        transform: none !important;
      }
    }`;
  clone.classList.add('shot');
  if (viewport) clone.classList.add('viewport');
  wrapper.className = document.body.className;
  // prd/10 §3.1: the tokens are keyed off `[data-theme]`, and inside the SVG
  // foreignObject the clone is the root — <html> is not in scope. Carry the
  // theme onto the wrapper or every capture is painted with the dark palette.
  wrapper.setAttribute('data-theme', document.documentElement.dataset.theme || 'dark');
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
  get mobile() { return mobile; },
  get state() { return state; },
  get tenants() { return state.tenants; },
  refresh: load,
  setPage: showPage,
  setScope,
  setTimeframe,
  setTenant,
  loadFullRange,
  // prd/25: the harness opens a ticket page the way a link does.
  openTicket,
  // P-2: the shareable URL and the route restore, exposed so the harness can
  // check a deep link without reloading the page.
  get url() { return routeHref(); },
  applyRoute,
  // prd/48: the in-app history depth and the location path. The harness walks
  // Back with `back()` and reads the YAH bar without a screenshot.
  get history() { return { depth: nav.depth, index: nav.index, canBack: nav.canBack() }; },
  back: () => inAppBack(),
  get yah() { return yahSteps().map((step) => step.label); },
  get ticket() { return state.ticket; },
  // Test hook: lower the event cap so the capped path (and its "load the full
  // range" affordance) can be exercised on a small fixture.
  setEventLimit: (limit) => {
    state.eventLimit = Math.max(1, Number(limit) || EVENT_MAX);
    return state.eventLimit;
  },
  setLanes: (dim) => { $('lanes').value = dim; timeline.setLanes(dim); renderPhaseLegend(); },
  setDensity,
  // prd/10 §3.1: the harness flips the theme the same way the pill does, so a
  // screenshot pair is one call apart.
  setTheme,
  get theme() { return state.theme; },
  setRulesLevel,
  setTrendView,
  setBenchmark,
  get benchmark() { return state.benchmark; },
  get trend() { return state.trend; },
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
$('theme-toggle').addEventListener('click', () => {
  setTheme(otherTheme(state.theme));
});
// prd/80: the titles switch — immediate, no reload, no data touched. The URL
// mirrors it (`?titles=off`) so a screen-shared link opens hidden.
$('titles-toggle').addEventListener('click', () => {
  setTitlesEnabled(!state.titlesEnabled);
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

/* prd/39: `input` updates the live count; `change` fires on release, when the
 * rule's evidence can be refetched without replacing the slider mid-drag. */
document.addEventListener('input', (event) => {
  const slider = event.target.closest('[data-pr-size]');
  if (!slider) return;
  setSizeCaps(
    { ...state.sizeCaps, [slider.dataset.prSize]: Number(slider.value) },
    { refetch: false },
  );
});

document.addEventListener('change', async (event) => {
  // prd/60: the target control re-reads the payload's pre-computed quantiles —
  // a re-render, never a new simulation and never an interpolation.
  const forecastTarget = event.target.closest('[data-forecast-target-input]');
  if (forecastTarget) {
    const value = Math.round(Number(forecastTarget.value));
    if (!Number.isFinite(value) || value < 0) return;
    state.forecastTarget = value;
    renderSprintStrip();
    return;
  }
  // prd/60: a new deadline is a new question to the same endpoint (a different
  // window legitimately gives a different interval); the target input above is
  // the pure re-render.
  const forecastBy = event.target.closest('[data-forecast-by-input]');
  if (forecastBy && state.forecastSprint != null && forecastBy.value) {
    const query = forecastQuery({
      target: `sprint:${state.forecastSprint}`,
      by: forecastBy.value,
      n: state.forecastTarget,
    });
    try {
      const payload = await getJSON(withTenant(`/api/forecast?${query}`));
      if (payload && payload.ok) {
        state.forecast = payload;
        state.forecastTarget = payload.request ? payload.request.n : state.forecastTarget;
        renderSprintStrip();
      }
    } catch (err) {
      setStatus(`forecast failed: ${err.message}`, 'stale');
    }
    return;
  }
  // prd/61: the before/after panel's split and metric are two selects on one
  // block; changing either asks the same endpoint one new question.
  const compareSplit = event.target.closest('[data-compare-split]');
  if (compareSplit) {
    await setTrendCompare({ split: compareSplit.value });
    return;
  }
  const compareMetric = event.target.closest('[data-compare-metric]');
  if (compareMetric) {
    await setTrendCompare({ metric: compareMetric.value });
    return;
  }
  // prd/59: the aging cohort (story cycle / PR review / PR cycle / a stage) and
  // the threshold source are two selects on one tab.
  const agingView = event.target.closest('[data-aging-view]');
  if (agingView) {
    state.agingView = agingView.value;
    await reloadAging();
    syncUrl();
    return;
  }
  const agingSource = event.target.closest('[data-aging-source]');
  if (agingSource) {
    state.agingSource = agingSource.value === 'fixed' ? 'fixed' : 'self';
    await reloadAging();
    return;
  }
  const slider = event.target.closest('[data-pr-size]');
  if (!slider) return;
  setSizeCaps(
    { ...state.sizeCaps, [slider.dataset.prSize]: Number(slider.value) },
    { refetch: true },
  );
});

/* prd/38: the top-bar "go to ticket" box resolves a key to its route. */
$('ticket-jump').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('ticket-jump-input');
  const key = ticketFromQuery(input.value);
  if (!key) {
    flashStatus('not a ticket key — try DEL-155', 'stale');
    input.focus();
    return;
  }
  input.value = '';
  await openTicket(key);
});

/* prd/61: "Record a change" posts one first-party annotation. */
document.addEventListener('submit', async (event) => {
  const form = event.target.closest('[data-annotation-form]');
  if (!form) return;
  event.preventDefault();
  await recordAnnotation(form);
});

/* prd/64 F-11: the desktop Delivery list's inspector (#delivery-detail) renders
 * the same chips as the drawer, but the drawer is closed there, so
 * `state.drawerEntity` is null. Resolve the row the chip is attached to: inside
 * the drawer the drawer's entity, otherwise the selected row. */
function detailEntity(pivot, ui) {
  return pivot && pivot.closest('#drawer')
    ? ui.drawerEntity
    : (ui.selection || ui.drawerEntity);
}

document.addEventListener('click', async (event) => {
  // prd/63 F19: a click on the page behind an open drawer closes it (Esc
  // already did; the pointer path was missing).
  const drawerEl = $('drawer');
  if (drawerEl && !drawerEl.hidden
    && !event.target.closest('#drawer')
    && !event.target.closest('#menu')) {
    closeDrawer();
  }
  const menuItem = event.target.closest('#menu [data-menu]');
  if (menuItem) {
    const entity = state.menuEntity;
    const action = menuItem.dataset.menu;
    closeMenu();
    if (action === 'copy') await copyMarkdown(entity);
    // prd/63 A5: clearing the scope stays where the user is; jumping to the
    // timeline made "Clear scope" a navigation, not a scope undo.
    else if (action === 'clear') { setScope('all', 'all'); }
    else if (action === 'person' && entity) {
      const person = menuItem.dataset.person
        || entity.author || (entity.pr && entity.pr.actor) || (entity.row && entity.row.author);
      if (person) setScope('person', person);
    } else if (action === 'timeline') {
      // prd/63 A5: "Open in timeline" acts on the row that was right-clicked —
      // the same ticket/PR scope and focus the drawer's Timeline chip uses.
      const scope = drawerTimelineScope(entity);
      const prId = entity?.pr?.id || entity?.row?.object_id || null;
      if (scope) setScope(scope.kind, scope.value, { silent: true });
      await showPage('timeline');
      if (prId) timeline.focusPr(prId);
    }
    return;
  }
  if (!event.target.closest('#menu')) closeMenu();
  // prd/48: Back pops one in-app entry; a YAH segment jumps up the path (a
  // new step — the history keeps both where the user was and where they went).
  if (event.target.closest('#history-back')) {
    inAppBack();
    return;
  }
  const yahStep = event.target.closest('#yah [data-yah]');
  if (yahStep) {
    await yahJump(yahStep.dataset.yah);
    return;
  }
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
    if (state.tab === 'aging') {
      // prd/59: the export is the ranked list, with each item's band, holder
      // and ×p85 — the same order the table shows.
      const keys = sprintStoryKeys();
      await copyText(agingMarkdown(state.aging, {
        base: appBase(),
        onlyLive: state.agingOnly,
        rowFilter: agingRowFilter(keys),
      }));
      return;
    }
    const data = state.delivery;
    const rows = data ? tabRows(data, state.tab).filter((row) => matchesScopeRow(row)) : [];
    await copyText(deliveryMarkdown(
      rows,
      data ? `${data.assumption}; snapshot ${data.snapshot}` : '',
      appBase(),
      { tab: state.tab, counts: data ? scopedTabCounts(data, sprintStoryKeys()) : null },
    ));
    return;
  }
  if (event.target.closest('#rules-copy')) {
    await copyText(rulesMarkdown(state.rules, appBase()));
    return;
  }
  if (event.target.closest('[data-verdict-copy]')) {
    // prd/58: the card's own export — the claim, the ladder and the lab notes.
    await copyText(verdictMarkdown(state.verdict, appBase()));
    return;
  }
  if (event.target.closest('[data-rule-show-all]')) {
    // prd/58: the disclosure toggle is a preference, like the density or the
    // benchmark contrast. It changes what is shown, never a rank or at_stake.
    state.showAllRules = !state.showAllRules;
    saveUI();
    renderPage();
    return;
  }
  if (event.target.closest('#ticket-copy')) {
    await copyText(state.ticket ? ticketMarkdown(state.ticket, appBase()) : '');
    return;
  }
  // prd/26 P6: a sprint row (or a band, via the canvas callback) sets the
  // timeframe to that sprint's measured window.
  if (event.target.closest('[data-sprint-clear]')) {
    clearSprintFocus();
    flashStatus('sprint lens cleared — every story lane is back', 'live');
    return;
  }
  const sprintRow = event.target.closest('[data-sprint-start]');
  if (sprintRow) {
    // prd/63 F24: clicking the selected sprint again deselects it (the row is
    // a toggle, which its aria-pressed already promised).
    if (state.sprint != null
      && String(state.sprint) === String(sprintRow.dataset.sprintId)) {
      clearSprintFocus();
      flashStatus('sprint lens cleared — every story lane is back', 'live');
      return;
    }
    const start = Number(sprintRow.dataset.sprintStart);
    const end = Number(sprintRow.dataset.sprintEnd);
    if (Number.isFinite(start) && Number.isFinite(end)) {
      scopeToSprint({
        id: sprintRow.dataset.sprintId, start, end,
      });
    }
    return;
  }
  const forecastToggler = event.target.closest('[data-forecast-risk-toggle]');
  if (forecastToggler) {
    const rows = forecastToggler.parentElement
      && forecastToggler.parentElement.querySelector('[data-forecast-phone-rows]');
    if (rows) {
      rows.hidden = !rows.hidden;
      forecastToggler.setAttribute('aria-expanded', String(!rows.hidden));
    }
    return;
  }
  // prd/60: both entry points open the same card. The "Will it land?" button on
  // prd/52's block scrolls to it (the card is already scoped to the active
  // sprint, the same scope the at-risk list has).
  const forecastOpen = event.target.closest('[data-forecast-open]');
  if (forecastOpen) {
    if (!state.forecast) await loadForecast(loadSeq);
    renderSprintStrip();
    const card = $('forecast-card');
    if (card) {
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      card.classList.add('flash');
      setTimeout(() => card.classList.remove('flash'), 700);
    }
    return;
  }
  // prd/60: the refusals hand off to their owners — prd/61's stability strip for
  // an overloaded queue, prd/52/59's named rows for a small set.
  const handoff = event.target.closest('[data-forecast-handoff]');
  if (handoff) {
    if (handoff.dataset.forecastHandoff === 'prd61') {
      await showPage('trend');
      await setTrendView({ metric: 'throughput_stories', bucket: 'week' });
    } else {
      const risk = $('sprint-at-risk');
      if (risk) risk.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    return;
  }
  if (event.target.closest('#ticket-back')) {
    await showPage(state.ticketFrom || 'overview');
    return;
  }
  const ticket = event.target.closest('[data-ticket]');
  if (ticket) {
    event.preventDefault();
    await openTicket(ticket.dataset.ticket);
    return;
  }
  if (event.target.closest('#policy-copy')) {
    await copyText(policyMarkdown(state.policy));
    return;
  }
  if (event.target.closest('#policy-run')) {
    const body = $('policy-body');
    await runPolicyScenario(readScenario(body));
    return;
  }
  if (event.target.closest('#policy-reset')) {
    await runPolicyScenario(defaultScenario());
    return;
  }
  const preset = event.target.closest('[data-preset]');
  if (preset) {
    const chosen = PRESETS.find((entry) => entry.id === preset.dataset.preset);
    // prd/63 F21: presets compose (the helper says they do). A preset merges
    // onto the current knobs; Reset to baseline is the way to clear.
    if (chosen) await runPolicyScenario({ ...state.policyScenario, ...chosen.scenario });
    // prd/54: the Overview chase card links straight to the lab's review-SLA
    // preset, so one click lands on the scenario already run.
    if (preset.dataset.go) await showPage(preset.dataset.go);
    return;
  }
  const ruleLevel = event.target.closest('[data-rule-level]');
  if (ruleLevel) {
    await setRulesLevel(ruleLevel.dataset.ruleLevel);
    return;
  }
  // prd/50: a metric tab or the week/sprint bucket is one cached request.
  const trendMetric = event.target.closest('[data-trend-metric]');
  if (trendMetric) {
    await setTrendView({ metric: trendMetric.dataset.trendMetric });
    return;
  }
  const trendBucket = event.target.closest('[data-trend-bucket]');
  if (trendBucket) {
    await setTrendView({ bucket: trendBucket.dataset.trendBucket });
    return;
  }
  // prd/63 F1: after a failed fetch the error state offers a retry.
  if (event.target.closest('[data-trend-retry]')) {
    // drop the cached failure (none is cached) and ask again
    state.cache.clear();
    await setTrendView();
    return;
  }
  if (event.target.closest('#trend-copy')) {
    await copyText(trendMarkdown(state.trend, { compare: state.compare }));
    return;
  }
  // prd/61: delete is a two-step inline confirm (no modal).
  const remove = event.target.closest('[data-annotation-delete]');
  if (remove) {
    if (remove.dataset.confirm === 'true') {
      await deleteAnnotation(remove.dataset.annotationDelete);
    } else {
      remove.dataset.confirm = 'true';
      remove.textContent = 'delete? click to confirm';
      remove.classList.add('danger');
    }
    return;
  }
  // prd/61: a chart marker selects that change in the before/after panel.
  const marker = event.target.closest('[data-annotation-id]');
  if (marker && marker.dataset.date) {
    await setTrendCompare({ split: marker.dataset.date });
    return;
  }
  // prd/57: the per-view benchmark toggle (delivery / ticket / queue).
  if (event.target.closest('[data-benchmark-toggle]')) {
    setBenchmark(!state.benchmark);
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
    // prd/59: `?tab=aging` is a shareable destination (the Overview tile and a
    // pasted URL land on the same subset), so the tab is route state.
    syncUrl();
    return;
  }
  // prd/59: "Only live" hides abandoned work — a view choice, not a refetch of
  // the delivery list.
  const agingOnly = event.target.closest('[data-aging-only]');
  if (agingOnly) {
    state.agingOnly = !state.agingOnly;
    await reloadAging();
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
  // prd/39: a slider drag updates the count live; the rule refetches on release.
  const sizeSlider = event.target.closest('[data-pr-size]');
  if (sizeSlider) {
    setSizeCaps(
      { ...state.sizeCaps, [sizeSlider.dataset.prSize]: Number(sizeSlider.value) },
      { refetch: false },
    );
    return;
  }
  if (event.target.closest('[data-pr-size-reset]')) {
    setSizeCaps({ ...DEFAULT_SIZE_CAPS }, { refetch: true });
    flashStatus(`size caps reset to ${DEFAULT_SIZE_CAPS.commits} / ${DEFAULT_SIZE_CAPS.lines}`, 'live');
    return;
  }
  // prd/63 C12: the chase action opens the PR drawer (the same holder facts
  // the row shows), not only the ticket page. The ticket link stays a doorway.
  const chaseOpen = event.target.closest('[data-chase-open]');
  if (chaseOpen) {
    const target = findDeliveryRow(chaseOpen.dataset.chaseOpen);
    if (target) openDeliveryPrDrawer(target);
    return;
  }
  // prd/63 V-B: the ticket page links out to the views that answer "where does
  // this time go", scoped to the ticket (and to its PRs outside the table).
  const ticketOpen = event.target.closest('[data-ticket-open]');
  if (ticketOpen && state.ticketKey) {
    const scope = { kind: 'ticket', value: state.ticketKey, label: state.ticketKey };
    setScope(scope.kind, scope.value, { silent: true });
    await showPage(ticketOpen.dataset.ticketOpen);
    return;
  }
  const ticketPrOpen = event.target.closest('[data-ticket-pr-open]');
  if (ticketPrOpen) {
    const pr = ((state.ticket && state.ticket.prs) || [])
      .find((candidate) => candidate.object_id === ticketPrOpen.dataset.ticketPrOpen);
    if (pr) openTicketPrDrawer(pr);
    return;
  }
  // prd/38 #12: a ticket's PR row opens the PR drawer.
  const prRow = event.target.closest('tr.pr-row[data-pr]');
  if (prRow) {
    const pr = ((state.ticket && state.ticket.prs) || [])
      .find((candidate) => candidate.object_id === prRow.dataset.pr);
    openTicketPrDrawer(pr);
    return;
  }
  // prd/63 F20: the response-time rows are pivots too (the hint promised it).
  const responseRow = event.target.closest('[data-response-person]');
  if (responseRow) {
    const person = responseRow.dataset.responsePerson;
    const role = responseRow.dataset.responseRole;
    if (role === 'sentinel' || role === 'synthetic') setScope('role', role);
    else setScope('person', person);
    return;
  }
  const row = event.target.closest('tr.row');
  if (row && row.dataset.id) {
    selectRow(row.dataset.id);
    // prd/38 #12 + regression fix: the delivery list has its own inline detail
    // panel (the inspector), so a desktop delivery row must NOT also open the
    // PR drawer — that stacked two panels for the same PR. Only the overview
    // (which has no inspector) opens the drawer from a row.
    if (state.page === 'overview') {
      openDeliveryPrDrawer(findDeliveryRow(row.dataset.id));
    }
    return;
  }
  const go = event.target.closest('[data-go]');
  if (go) {
    // prd/59: the Overview tile "waiting over a week" lands on the Aging tab,
    // so the destination names the subset it wants.
    if (go.dataset.tab) {
      state.tab = go.dataset.tab;
      state.tabPinned = true;
    }
    await showPage(go.dataset.go);
    // prd/63 B9: a link can name the section it lands on (the Delivery page's
    // Done-in-Jira list), so the target is visible instead of merely "on that
    // page somewhere".
    if (go.dataset.anchor) {
      const anchor = document.getElementById(go.dataset.anchor);
      if (anchor && anchor.scrollIntoView) anchor.scrollIntoView({ block: 'start' });
    }
    return;
  }
  if (event.target.closest('[data-drawer="close"]')) {
    closeDrawer();
    return;
  }
  const pivot = event.target.closest('[data-detail]');
  if (pivot) {
    // prd/64 F-11: the desktop Delivery list's inspector (#delivery-detail)
    // renders the same chips as the drawer, but the drawer is closed there, so
    // `state.drawerEntity` is null. `detailEntity` resolves the row the chip is
    // attached to: inside the drawer the drawer's entity, otherwise the row.
    const entity = detailEntity(pivot, state);
    const action = pivot.dataset.detail;
    if (action === 'copy') {
      await copyMarkdown(entity);
    } else if (action === 'timeline') {
      // prd/38 #20 + prd/43 §3: scope to the ticket/PR the drawer came from —
      // never to the author. The entity's ticket can live at the top level (the
      // ticket-page drawer), on the PR (timeline/delivery) or on the row; one
      // helper reads all three, so the chip scopes the same from every opener.
      const scope = drawerTimelineScope(entity);
      const prId = entity?.pr?.id || entity?.row?.object_id || null;
      const keepRow = state.selection && state.selection.row ? state.selection.row : null;
      closeDrawer();
      if (scope) {
        setScope(scope.kind, scope.value, { silent: true });
      }
      if (keepRow) state.selection = { row: keepRow };
      await showPage('timeline');
      if (prId) timeline.focusPr(prId);
    } else if (action === 'blocking') {
      const selectedRow = entity && entity.row ? entity.row : null;
      closeDrawer();
      await showPage('blocking');
      focusBlockingRow(selectedRow);
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
  const prRow = event.target.closest && event.target.closest('tr.pr-row[data-pr]');
  if (prRow) {
    event.preventDefault();
    const pr = ((state.ticket && state.ticket.prs) || [])
      .find((candidate) => candidate.object_id === prRow.dataset.pr);
    openTicketPrDrawer(pr);
    return;
  }
  const responseRow = event.target.closest && event.target.closest('[data-response-person]');
  if (responseRow) {
    event.preventDefault();
    const role = responseRow.dataset.responseRole;
    if (role === 'sentinel' || role === 'synthetic') setScope('role', role);
    else setScope('person', responseRow.dataset.responsePerson);
    return;
  }
  const row = event.target.closest && event.target.closest('tr.row[data-id]');
  if (row) {
    event.preventDefault();
    selectRow(row.dataset.id);
    if (state.page === 'overview') {
      openDeliveryPrDrawer(findDeliveryRow(row.dataset.id));
    }
  }
});

async function copyMarkdown(entity) {
  const hoursMode = (state.delivery && state.delivery.hours_mode) || 'wall-clock';
  // prd/44 DES-5: the calendar fingerprint makes the copied number re-derivable
  const calendarId = (state.delivery && state.delivery.calendar_id) || '';
  const markdown = entity ? entityMarkdown(entity, hoursMode, appBase(), calendarId) : '';
  // prd/64 F-11: an empty export is not a copy. The old path wrote `""` to the
  // clipboard and still flashed "copied as Markdown".
  if (!markdown) {
    flashStatus('nothing selected to copy as Markdown', 'stale');
    return;
  }
  await copyText(markdown);
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
      // The ticket route lives in the URL hash, not in localStorage: a saved
      // `ticket` page without its key would reopen an empty page (prd/25).
      page: state.page === 'ticket' ? state.ticketFrom : state.page,
      density: timeline.density,
      lanes: timeline.dim,
      timeframe: state.timeframe,
      tab: state.tab,
      // prd/59: the aging cohort and its two controls are preferences too.
      agingView: state.agingView,
      agingOnly: state.agingOnly,
      agingSource: state.agingSource,
      lens: state.lens,
      blockingLevel: state.blockingLevel,
      queueLevel: state.queueLevel,
      rulesLevel: state.rulesLevel,
      // prd/58: the "show all rules" disclosure state is a preference too.
      showAllRules: state.showAllRules,
      policyScenario: state.policyScenario,
      // prd/50: the trend tab; prd/57: the benchmark contrast preference.
      trendMetric: state.trendMetric,
      trendBucket: state.trendBucket,
      // prd/61: the before/after panel's metric is a preference; the split
      // defaults to the latest recorded change.
      compareMetric: state.compareMetric,
      benchmark: state.benchmark,
      activity: [...timeline.activityState.entries()],
      scope: state.scope,
      tenant: state.tenant,
      // scope is per tenant (prd/12): remember each dataset's own lens
      scopes: state.scopes,
      // prd/39: the PR-size calibration is a per-tenant preference too
      sizeCaps: state.sizeCapsMap,
    }));
  } catch { /* private mode: not worth a banner */ }
}

// Did a saved UI preference exist? A first phone visit is list-first (prd/28).
let uiRestored = false;

function restoreUI() {
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(LS_KEY) || 'null'); } catch { stored = null; }
  if (!stored) {
    setDensity('M');
    syncLevelSelects();
    return;
  }
  uiRestored = true;
  // The tenant must be restored before anything reads state.scope: the scope
  // stored at the top level belongs to that dataset.
  if (typeof stored.tenant === 'string' && stored.tenant) state.tenant = stored.tenant;
  if (stored.scopes && typeof stored.scopes === 'object') {
    for (const [name, scope] of Object.entries(stored.scopes)) {
      if (scope && scope.kind) state.scopes[name] = scope;
    }
  }
  if (stored.sizeCaps && typeof stored.sizeCaps === 'object') {
    // prd/39: one calibration per tenant, normalised on read (an old or
    // hand-edited value cannot become an invalid cap).
    for (const [name, caps] of Object.entries(stored.sizeCaps)) {
      state.sizeCapsMap[name] = normalizeSizeCaps(caps);
    }
  }
  state.sizeCaps = sizeCapsForTenant(state.sizeCapsMap, state.tenant);
  if (PAGES[stored.page] && stored.page !== 'ticket') state.page = stored.page;
  if (TIMEFRAMES[stored.timeframe]) state.timeframe = stored.timeframe;
  if (stored.tab) {
    state.tab = stored.tab;
    state.tabPinned = true;
  }
  if (typeof stored.agingView === 'string' && /^(story|pr):[a-z_]+$/.test(stored.agingView)) {
    state.agingView = stored.agingView;
  }
  if (typeof stored.agingOnly === 'boolean') state.agingOnly = stored.agingOnly;
  if (stored.agingSource === 'fixed' || stored.agingSource === 'self') {
    state.agingSource = stored.agingSource;
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
  if (typeof stored.showAllRules === 'boolean') state.showAllRules = stored.showAllRules;
  if (['flow_efficiency', 'cycle_median', 'on_time', 'wait_hours', 'merged', 'open']
    .includes(stored.trendMetric)) {
    state.trendMetric = stored.trendMetric;
  }
  if (stored.compareMetric === 'story_cycle' || stored.compareMetric === 'cycle_median') {
    state.compareMetric = stored.compareMetric;
  }
  if (stored.trendBucket === 'week' || stored.trendBucket === 'sprint') {
    state.trendBucket = stored.trendBucket;
  }
  if (typeof stored.benchmark === 'boolean') state.benchmark = stored.benchmark;
  if (stored.policyScenario && typeof stored.policyScenario === 'object') {
    state.policyScenario = { ...defaultScenario(), ...stored.policyScenario };
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

/* prd/64 F-09: a URL that names an unknown view must land on the default entry
 * view, not on a stale stored page the link never asked for. A ticket hash is
 * its own valid destination and keeps the page. */
function unknownRouteView(rawView, hasTicket = false) {
  return Boolean(rawView && !PAGES[rawView] && !hasTicket);
}

// -------------------------------------------------------------------- boot
restoreUI();
/* P-2: a pasted URL wins over the stored preference — that is what a deep link
 * is for. Tenant, view, ticket, timeframe and scope are applied before the
 * first paint, so boot never flashes a different view first. */
const initialRoute = parseRoute(
  typeof location !== 'undefined' ? location.search : '',
  typeof location !== 'undefined' ? location.hash : '',
);
// prd/63 F14: a link with an unknown view, timeframe or scope is a fallback,
// not a silent default. State which part of the link could not be used.
(() => {
  try {
    const params = new URLSearchParams(location.search);
    const view = params.get('view');
    const tf = params.get('tf');
    const scopeRaw = params.get('scope');
    const problems = [];
    if (view && !PAGES[view]) problems.push(`view “${view}”`);
    if (tf && !TIMEFRAMES[tf]) problems.push(`timeframe “${tf}”`);
    if (scopeRaw && scopeRaw !== 'all' && !parseRoute(location.search, '').scope) {
      problems.push(`scope “${scopeRaw}”`);
    }
    if (problems.length) {
      setNotice(
        `This link names ${problems.join(' and ')}, which this build does not know — `
        + 'showing the default view, timeframe and scope.',
        { append: true },
      );
    }
  } catch { /* no URL: nothing to report */ }
})();
if (initialRoute.tenant) state.tenant = initialRoute.tenant;
if (initialRoute.timeframe && TIMEFRAMES[initialRoute.timeframe]) {
  state.timeframe = initialRoute.timeframe;
}
if (initialRoute.scope) state.scope = initialRoute.scope;
if (initialRoute.sprint != null) state.sprint = initialRoute.sprint;
if (initialRoute.tab) {
  state.tab = initialRoute.tab;
  state.tabPinned = true;
}
// prd/63 F30/F21: the Trend tab pair and the lab scenario win over storage.
if (initialRoute.metric) state.trendMetric = initialRoute.metric;
if (initialRoute.bucket) state.trendBucket = initialRoute.bucket;
if (initialRoute.policy) state.policyScenario = { ...defaultScenario(), ...initialRoute.policy };
/* prd/64 F-09: an unusable `?view=` names a fallback, so the page must really
 * be the default entry view — not a stale stored page the link never asked for
 * (the notice promised "the default view"). A ticket hash is its own valid
 * destination and keeps the page. */
const rawRouteView = (() => {
  try { return new URLSearchParams(location.search).get('view'); } catch { return null; }
})();
if (unknownRouteView(rawRouteView, Boolean(initialRoute.ticket))) {
  state.page = DEFAULT_ENTRY_PAGE;
}
if (initialRoute.view && PAGES[initialRoute.view]) state.page = initialRoute.view;
if (initialRoute.ticket) {
  state.page = 'ticket';
  state.ticketKey = initialRoute.ticket;
}
/* prd/10 §3.1: the theme is settled before the first paint. The inline script
 * in index.html has already set `data-theme` from the URL or localStorage (so
 * there is no flash); this runs the same path the toggle does, so the canvas
 * palette is resolved from the tokens before anything draws. A `?theme=` link
 * is a deliberate destination, so it is remembered — otherwise the next reload
 * would silently disagree with the screen (the same rule tenant links follow). */
// prd/63 F29: a `?theme=` link is a *view*, not a preference change. Visiting
// a colleague's light link must not flip this user's stored default to light;
// only the toggle writes the preference (`setTheme(..., { save: true })`, the
// click handler's default).
setTheme(
  initialRoute.theme
    || normalizeTheme(document.documentElement.dataset.theme)
    || readTheme()
    || DEFAULT_THEME,
  { save: false, repaint: false },
);
/* prd/80: the titles switch, its popover and the panel are wired before the
 * first paint. The stored preference is a browser preference; a `?titles=off`
 * link wins for *this view* without rewriting it (as `?theme=` does), and a
 * browser that never imported anything still renders nothing new. */
initTitles();
if (initialRoute.titles === 'off') state.titlesEnabled = false;
else if (initialRoute.titles === 'on') state.titlesEnabled = true;
renderTitlesToggle();
// prd/28 §3: phone is list-first. A first visit lands on the Delivery list;
// a stored choice (the user opened Timeline last time) is respected. An
// explicit view in the URL is a deliberate destination and always wins.
// The first phone timeline opens on stories, so its Jira keys are on screen
// immediately instead of below the actor lanes.
if (isPhone() && !uiRestored && !initialRoute.view) {
  state.page = phoneEntryPage(null);
  const lanes = phoneEntryLanes(null);
  $('lanes').value = lanes;
  timeline.setLanes(lanes);
}
// The phone shell: off-canvas sidebar + bottom sheets. Desktop markup is
// untouched; every rule that does something lives under the 640px breakpoint.
const mobile = initMobile({
  onSheetDismiss: (sheet) => {
    if (sheet && sheet.id === 'drawer') closeDrawer();
    else if (sheet && sheet.id === 'delivery-detail') clearDeliverySelection();
  },
});
// prd/28 §2: tap-and-hold is the touch pivot gesture (the right-click menu).
bindLongPress((target, x, y) => {
  const found = entityForTarget(target, { x, y });
  if (found) openMenu(x, y, found.entity, found.label);
});
// prd/25 + P-2: a direct link (`/#ticket/DEL-167`, with or without the query)
// opens the ticket page after the dataset has loaded, exactly as a click does.
// Back/forward and a hand-edited hash both re-apply the whole route. prd/48:
// the entry stack learns which entry the browser moved to *before* the route
// is applied, so renderBack() is already right when the view repaints.
window.addEventListener('hashchange', () => { applyRoute(); });
window.addEventListener('popstate', (event) => {
  restoreFromLocation(event);
  applyRoute();
});
syncChromeHeight();
window.addEventListener('resize', () => {
  syncChromeHeight();
  renderHints();
  renderYah();
  syncRangeMeta();
});
buildLensButtons();
// prd/48: the URL the page opened with is step 0 of the in-app history. Boot
// is not a navigation: the app may adopt a tenant or view from the stored
// preference, and that canonicalisation rewrites step 0 instead of stacking a
// second entry (a fresh landing must open with Back disabled).
restoringRoute = true;
seedHistory();
renderPage();
// prd/32: the build identity is independent of the store, so it loads even
// when there is no dataset to read.
loadVersion();
load().then(async () => {
  if (state.page === 'ticket' && state.ticketKey) {
    await openTicket(state.ticketKey);
  } else {
    await showPage(state.page);
  }
  connect();
  // a sprint lens already set its measured window; a preset must not override it
  if (state.sprint == null) applyTimeframeToTimeline();
}).finally(() => {
  // the first view is on screen: from here on, a URL change is a real step
  restoringRoute = false;
  renderBack();
});

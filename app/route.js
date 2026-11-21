/* Shareable view state (docs/papercuts.md P-1/P-2).
 *
 * The address bar is the source of truth for "where am I": one small query
 * string names the dataset, the view and the two top-bar controls, and the
 * hash keeps the ticket route it already had. Copying the URL therefore lands a
 * colleague in the same place, in the same tenant, without a reload.
 *
 *   /?tenant=del-combined&view=timeline&tf=1q&scope=all
 *   /?tenant=del-combined&view=ticket&tf=1q&scope=all#ticket/DEL-167
 *   /?tenant=del-combined&view=delivery&repo=delivery-api   (prd/49)
 *
 * The parameter names are the ones the API already uses (`?tenant=<name>`), so
 * the URL and the request agree. A bare `#ticket/DEL-167` still resolves: the
 * ticket hash wins over `view`, and a missing tenant means "whatever the
 * server or the stored preference resolves to". `view` is kept on the ticket
 * URL too, so the query alone always answers "which view".
 *
 * Nothing here touches the DOM, the network or history: the whole scheme
 * round-trips in `node --test` without a browser.
 */

import { THEMES, normalizeTheme } from './theme.js';

/** The views the router knows (mirrors PAGES in main.js). */
export const ROUTE_VIEWS = [
  'overview', 'delivery', 'timeline', 'ticket',
  'blocking', 'queue', 'trend', 'rules', 'policy', 'sources',
];

/** The closed set of scope kinds (scope.js owns the behaviour). */
export const SCOPE_KINDS = ['person', 'role', 'ticket', 'repo'];

/* prd/10 §3.1: dark is the default, so a dark view is the absence of a
 * parameter. `?theme=light` is a shareable light view; `?theme=dark` is
 * accepted (it is what the toggle writes when it leaves light) and resolves to
 * the default. theme.js owns the closed set; this only spells it in the URL. */
export const ROUTE_THEMES = THEMES;

export const TITLE_SUFFIX = ' — megaspeed';
export const FALLBACK_TITLE = 'megaspeed';

/** The ticket key in a `#ticket/KEY` hash, or null. Malformed escapes are not a key. */
export function ticketFromHash(hash = '') {
  const match = /^#ticket\/(.+)$/.exec(String(hash || ''));
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
}

/** `person:AbCd` / `role:review` / `all` → the scope shape the app uses. */
export function parseScope(raw) {
  if (raw == null || raw === '') return null;
  if (raw === 'all') return { kind: 'all', value: null, label: 'everything' };
  const at = raw.indexOf(':');
  if (at < 0) return null;
  const kind = raw.slice(0, at);
  const value = raw.slice(at + 1);
  if (!value || !SCOPE_KINDS.includes(kind)) return null;
  return { kind, value, label: value };
}

function scopeParam(scope) {
  if (!scope || !scope.kind || scope.kind === 'all') return 'all';
  if (scope.value == null || scope.value === '') return 'all';
  return `${scope.kind}:${scope.value}`;
}

function normalizeView(view) {
  return ROUTE_VIEWS.includes(view) ? view : null;
}

/**
 * Serialise the view state into `?query#hash` (no path). Every field is
 * optional, so a caller can encode only what it knows.
 */
export function serializeRoute({
  tenant = null, view = null, ticket = null, timeframe = null, scope = null,
  sprint = null, from = null, to = null, theme = null,
  trendMetric = null, trendBucket = null, policy = null,
} = {}) {
  const params = new URLSearchParams();
  if (tenant) params.set('tenant', String(tenant));
  if (view) params.set('view', String(view));
  if (timeframe) params.set('tf', String(timeframe));
  // prd/63 F30: the Trend tab pair is view state, not a session-local setting;
  // a shared link opens the same metric and granularity.
  if (trendMetric) params.set('metric', String(trendMetric));
  if (trendBucket) params.set('bucket', String(trendBucket));
  // prd/63 F21: a policy-lab scenario is shareable too (compact JSON).
  if (policy) params.set('policy', typeof policy === 'string' ? policy : JSON.stringify(policy));
  // prd/37 §4: the sprint lens is a filter with a range — both go in the URL so
  // a copied link lands on the same sprint, the same stories and the same window.
  if (sprint != null && sprint !== '') params.set('sprint', String(sprint));
  if (from != null && to != null) {
    params.set('from', String(from));
    params.set('to', String(to));
  }
  // `all` is explicit: a link must reproduce "everything", not inherit the
  // recipient's stored person scope.
  if (scope && scope.kind) {
    // prd/49: a repo scope is written as `repo=`, the parameter the API's own
    // endpoints take, so a copied link is also a valid API filter. `parseRoute`
    // accepts both forms; the other scope kinds stay in `scope=`.
    if (scope.kind === 'repo' && scope.value) {
      params.set('repo', String(scope.value));
    } else {
      params.set('scope', scopeParam(scope));
    }
  }
  // dark is the default, so it costs no characters and old links keep working
  if (normalizeTheme(theme) === 'light') params.set('theme', 'light');
  const query = params.toString();
  const hash = ticket ? `#ticket/${encodeURIComponent(ticket)}` : '';
  return `${query ? `?${query}` : ''}${hash}`;
}

/**
 * Parse a `location.search` + `location.hash` pair. Absent parameters are
 * `null` (keep whatever the stored preference says); an unknown `view` is
 * ignored rather than trusted.
 *
 * prd/49: a bare `?repo=ENG/delivery-api` is accepted as the same scope the
 * `?scope=repo:…` form carries, so an API-style link (the endpoints take
 * `repo=`) lands on the repo-scoped view too.
 */
/** The Trend metrics prd/50 exposes (mirrors app/trend.js / analysis/trend.py). */
export const ROUTE_TREND_METRICS = [
  'flow_efficiency', 'cycle_median', 'on_time', 'wait_hours', 'merged', 'open',
];

export function parseRoute(search = '', hash = '') {
  const params = new URLSearchParams(String(search || ''));
  const ticket = ticketFromHash(hash);
  const repo = params.get('repo') || null;
  const policy = params.get('policy') || '';
  return {
    tenant: params.get('tenant') || null,
    // A ticket hash is the ticket view, whatever a stale `view` says.
    view: ticket ? 'ticket' : normalizeView(params.get('view')),
    ticket,
    timeframe: params.get('tf') || null,
    // prd/63 F30: unknown values are ignored (the default tab shows), never
    // trusted into a broken request.
    metric: ROUTE_TREND_METRICS.includes(params.get('metric') || '')
      ? params.get('metric') : null,
    bucket: ['week', 'sprint'].includes(params.get('bucket') || '')
      ? params.get('bucket') : null,
    // prd/63 F21: a compact scenario object, or null when absent/invalid.
    policy: (() => {
      if (!policy) return null;
      try {
        const parsed = JSON.parse(policy);
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
          ? parsed : null;
      } catch {
        return null;
      }
    })(),
    scope: parseScope(params.get('scope')) || (repo
      ? { kind: 'repo', value: repo, label: repo }
      : null),
    // an id, not free text: anything else means "no sprint lens"
    sprint: /^\d+$/.test(params.get('sprint') || '') ? Number(params.get('sprint')) : null,
    from: /^\d+$/.test(params.get('from') || '') ? Number(params.get('from')) : null,
    to: /^\d+$/.test(params.get('to') || '') ? Number(params.get('to')) : null,
    // null means "no opinion": keep the stored preference (dark by default).
    theme: normalizeTheme(params.get('theme')),
  };
}

/**
 * The browser tab title: `<tenant> · <view title> — megaspeed`, and
 * `<tenant> · DEL-167 — megaspeed` on a ticket. A missing tenant (no dataset
 * yet, or the list has not loaded) leaves the name off instead of printing a
 * dangling separator.
 */
export function pageTitle({ tenant = null, view = null, ticket = null, title = null } = {}) {
  const label = String(ticket || title || view || '').trim();
  const head = tenant ? `${tenant} · ${label}`.trim() : label;
  return head ? `${head}${TITLE_SUFFIX}` : FALLBACK_TITLE;
}

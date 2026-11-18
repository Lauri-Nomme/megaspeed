/* Shareable view state (docs/papercuts.md P-1/P-2).
 *
 * The address bar is the source of truth for "where am I": one small query
 * string names the dataset, the view and the two top-bar controls, and the
 * hash keeps the ticket route it already had. Copying the URL therefore lands a
 * colleague in the same place, in the same tenant, without a reload.
 *
 *   /?tenant=del-combined&view=timeline&tf=1q&scope=all
 *   /?tenant=del-combined&view=ticket&tf=1q&scope=all#ticket/DEL-167
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

/** The views the router knows (mirrors PAGES in main.js). */
export const ROUTE_VIEWS = [
  'overview', 'delivery', 'timeline', 'ticket',
  'blocking', 'queue', 'rules', 'policy', 'sources',
];

/** The closed set of scope kinds (scope.js owns the behaviour). */
export const SCOPE_KINDS = ['person', 'role'];

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
} = {}) {
  const params = new URLSearchParams();
  if (tenant) params.set('tenant', String(tenant));
  if (view) params.set('view', String(view));
  if (timeframe) params.set('tf', String(timeframe));
  // `all` is explicit: a link must reproduce "everything", not inherit the
  // recipient's stored person scope.
  if (scope && scope.kind) params.set('scope', scopeParam(scope));
  const query = params.toString();
  const hash = ticket ? `#ticket/${encodeURIComponent(ticket)}` : '';
  return `${query ? `?${query}` : ''}${hash}`;
}

/**
 * Parse a `location.search` + `location.hash` pair. Absent parameters are
 * `null` (keep whatever the stored preference says); an unknown `view` is
 * ignored rather than trusted.
 */
export function parseRoute(search = '', hash = '') {
  const params = new URLSearchParams(String(search || ''));
  const ticket = ticketFromHash(hash);
  return {
    tenant: params.get('tenant') || null,
    // A ticket hash is the ticket view, whatever a stale `view` says.
    view: ticket ? 'ticket' : normalizeView(params.get('view')),
    ticket,
    timeframe: params.get('tf') || null,
    scope: parseScope(params.get('scope')),
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

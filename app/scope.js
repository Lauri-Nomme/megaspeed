/* What a scope actually filters, per view.
 *
 * prd/10 §4.2 calls the scope banner "the single most important honesty
 * device", so the sentence is derived from the filters the code really applies
 * instead of a hardcoded string (QA round 2, B3). Two rules keep it true:
 *
 *   - `FILTERED_PAGES` is the single place that says which views narrow to a
 *     scope. `renderScopeBanner()` reads it, and the banner never claims a
 *     view shows everything when it does not.
 *   - a queue stream whose id is a closed role (`author`, `review`, `CI`, …)
 *     is a role queue even when the API labels it `person`: clicking it must
 *     scope to the queue, not to a person literally named "author" (B4).
 *
 * Pure functions only, so they are unit-testable without a browser.
 */

import { rowTicket } from './tickets.js';

export const ROLE_QUEUES = ['author', 'review', 'ready', 'sentinel', 'CI', 'calendar'];

/** Pages that narrow their rows/series when a scope is active. */
const PERSON_PAGES = ['timeline', 'delivery', 'queue', 'overview'];
const ROLE_PAGES = {
  author: ['delivery', 'queue', 'overview'],
  review: ['delivery', 'queue', 'overview'],
  // prd/34 §3: a PR waiting only on the sentinel must show under
  // `role:sentinel` on the delivery list too, not only in the queue chart.
  sentinel: ['delivery', 'queue', 'overview'],
  ci: ['queue', 'overview'],
  calendar: ['queue', 'overview'],
};

export const VIEW_NAMES = {
  overview: 'overview',
  delivery: 'delivery list',
  timeline: 'timeline',
  blocking: 'blocking map',
  queue: 'queue view',
  rules: 'rules',
  sources: 'sources',
};

/* prd/38 #20: the Timeline chip from a PR/ticket scopes to *that ticket*, not
 * to the author. A ticket scope narrows the two views that can answer "where
 * did this ticket's time go": the timeline (its events) and the delivery list
 * (the open PRs that name it). */
const TICKET_PAGES = ['timeline', 'delivery', 'overview'];

export function isRoleQueue(id) {
  const value = String(id ?? '').toLowerCase();
  return ROLE_QUEUES.some((role) => role.toLowerCase() === value);
}

/** True when a queue-chart row is a queue/role, not an individual person. */
export function streamIsQueue(stream) {
  if (!stream) return false;
  return stream.role === 'synthetic' || isRoleQueue(stream.id);
}

/** The role label to show next to a stream: a role queue is never "a person". */
export function streamRoleLabel(stream) {
  if (!stream) return '';
  if (isRoleQueue(stream.id)) return 'role';
  return stream.role || 'person';
}

/** The scope a queue row should set: role for a queue, person otherwise. */
export function scopeForStream(stream) {
  if (!stream || !stream.id) return null;
  if (streamIsQueue(stream)) return { kind: 'role', value: stream.id, label: stream.id };
  return { kind: 'person', value: stream.id, label: stream.id };
}

/** The scope a blocking-map node should set. */
export function scopeForNode(node) {
  if (!node || !node.id) return null;
  if (node.kind === 'person' && !isRoleQueue(node.id)) {
    return { kind: 'person', value: node.id, label: node.id };
  }
  if (node.kind === 'person' || node.kind === 'role' || node.kind === 'synthetic' || node.kind === 'queue') {
    return { kind: 'role', value: node.id, label: node.id };
  }
  return null;
}

/** The queue-chart level a blocking node's drill-down lands on (B5). */
export function queueLevelForNode(node) {
  return node && node.kind === 'person' && !isRoleQueue(node.id) ? 'person' : 'queue';
}

/**
 * prd/43 §3: the scope a drawer's Timeline chip opens.
 *
 * A PR drawer can be opened from three places (the timeline, the Delivery list,
 * the ticket page), and the same chip must scope to the *ticket* the drawer
 * belongs to — never to the author, and never to "everything". The ticket key
 * lives in different places depending on the opener: `entity.ticket` (the
 * ticket-page drawer carries the key it was opened from), `pr.ticket` (a
 * timeline/delivery PR), or the row's own keys. Reading all three in one place
 * is what makes the ticket-page chip scope like the Delivery-list one.
 *
 * Pure, so the fallback chain is unit-testable without a browser.
 */
export function drawerTimelineScope(entity) {
  if (!entity) return null;
  const pr = entity.pr || null;
  const row = entity.row || null;
  const ticket = String(
    entity.ticket
    || (pr && pr.ticket)
    || (row ? rowTicket(row) : '')
    || '',
  ).trim();
  if (ticket) return { kind: 'ticket', value: ticket, label: ticket };
  const prId = (pr && pr.id) || (row && row.object_id) || null;
  const author = (pr && pr.actor) || (row && row.author) || null;
  if (prId && author) return { kind: 'person', value: author, label: author };
  return null;
}

/** The pages whose content narrows under this scope. */
export function filteredPages(scope) {
  if (!scope || scope.kind === 'all') return new Set();
  if (scope.kind === 'person') return new Set(PERSON_PAGES);
  if (scope.kind === 'ticket') return new Set(TICKET_PAGES);
  if (scope.kind === 'role') {
    return new Set(ROLE_PAGES[String(scope.value || '').toLowerCase()] || []);
  }
  return new Set();
}

function list(ids) {
  const names = ids.map((id) => VIEW_NAMES[id] || id);
  if (!names.length) return 'none';
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/** What a person/role/ticket scope means *in this view* (prd/34 §3: one meaning each). */
export function scopeMeaning(scope, pageId) {
  if (!scope || scope.kind === 'all') return 'everything';
  if (scope.kind === 'ticket') {
    if (pageId === 'delivery') return `the open PRs that name ${scope.value}`;
    if (pageId === 'timeline') return `events for ${scope.value} and its PRs`;
    if (pageId === 'overview') return `the open PRs and people waiting on ${scope.value}`;
    return `ticket ${scope.value}`;
  }
  if (scope.kind === 'person') {
    // Delivery and Queue ask "who is the item waiting on"; the timeline is an
    // event log, so there the person scope filters *activity by* them.
    if (pageId === 'delivery' || pageId === 'queue') return `waiting on ${scope.value}`;
    if (pageId === 'timeline') return `events by ${scope.value}`;
    if (pageId === 'overview') return `items waiting on ${scope.value}`;
    return `person ${scope.value}`;
  }
  return `the ${scope.value} queue`;
}

/**
 * The banner sentence for one view: names this view's state first, then what
 * is and is not filtered, so it can never contradict the chart on screen.
 */
export function scopeBanner(scope, pageId) {
  if (!scope || scope.kind === 'all') return '';
  const filtered = filteredPages(scope);
  const here = filtered.has(pageId);
  const name = VIEW_NAMES[pageId] || pageId;
  const subject = scope.kind === 'ticket'
    ? `ticket ${scope.value} (${scopeMeaning(scope, pageId)})`
    : scope.kind === 'person'
      ? `person ${scope.value} (${scopeMeaning(scope, pageId)})`
      : `the ${scope.value} queue`;
  const inside = [...filtered];
  const outside = Object.keys(VIEW_NAMES).filter((id) => !filtered.has(id));
  const bits = [
    `Scope ${subject}`,
    `this view (${name}) is ${here ? 'filtered to it' : 'not filtered — it still shows the whole team'}`,
    `filtered: ${list(inside)}`,
    `not filtered: ${list(outside)}`,
  ];
  if (scope.kind === 'person') bits.push('waits are measured over the whole log');
  return bits.join(' · ');
}

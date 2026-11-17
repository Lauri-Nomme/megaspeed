/* Ticket keys: one meaningful story key per PR.
 *
 * The store can carry more than one ticket key per PR. When a team links every
 * PR to the same epic (on `del`: `DEL-1` on all 131 PRs), that key is a
 * tracking link, not a story ticket. Taking `keys[0]` therefore labels every
 * PR with the epic and hides the real ticket (QA round 2, B2), and the same
 * key then pollutes grouping, export and impact.
 *
 * The census below is data-driven rather than a hardcoded key list, and it
 * mirrors the server's parent-key heuristic (analysis/ballcourt.py): a key on
 * at least a quarter of the PRs (and on at least three of them, in a corpus
 * larger than that) is an epic/tracking link and is demoted out of the label.
 * A PR is never left without a key — if every key looks shared, the most
 * specific one wins, and a single-key PR always keeps its key.
 *
 * These functions are pure so they can be unit-tested without a browser.
 */

export const EPIC_SHARE = 0.25;
export const EPIC_MIN_PRS = 3;

/** The set of keys that look like epic/tracking links across `keyLists`. */
export function epicKeySet(keyLists, { share = EPIC_SHARE, minPrs = EPIC_MIN_PRS } = {}) {
  const counts = new Map();
  let prs = 0;
  for (const keys of keyLists || []) {
    const unique = new Set((keys || []).filter(Boolean));
    if (!unique.size) continue;
    prs += 1;
    for (const key of unique) counts.set(key, (counts.get(key) || 0) + 1);
  }
  const epics = new Set();
  // Too small a corpus to tell a shared epic from a real shared ticket: report
  // the dependency rather than guess it away.
  if (prs <= minPrs) return epics;
  const threshold = Math.max(minPrs, Math.ceil(share * prs));
  for (const [key, count] of counts) if (count >= threshold) epics.add(key);
  return epics;
}

/** The PR's own (story) ticket key — never the shared epic link when there is one. */
export function ownTicket(keys, epics = null) {
  const list = (keys || []).filter(Boolean);
  if (!list.length) return '';
  const candidates = list.filter((key) => !(epics && epics.has(key)));
  if (candidates.length) return candidates[candidates.length - 1];
  // No census (or every key is shared): prefer the most specific key, i.e. one
  // that is not a prefix of another key on the same PR (`DEL-167` over `DEL-1`).
  const specific = list.filter((key) => !list.some((other) => other !== key && other.startsWith(key)));
  if (specific.length) return specific[specific.length - 1];
  return list[list.length - 1];
}

/** The epic/tracking key on this PR, when the census knows one. */
export function epicTicket(keys, epics = null) {
  if (!epics || !epics.size) return '';
  const found = (keys || []).find((key) => epics.has(key));
  return found || '';
}

/** `{ ticket, epic }` for one PR's key list. */
export function ticketFields(keys, epics = null) {
  return { ticket: ownTicket(keys, epics), epic: epicTicket(keys, epics) };
}

/** Annotate an API row in place (delivery rows carry `tickets`). */
export function annotateRow(row, epics = null) {
  if (!row) return row;
  const fields = ticketFields(row.tickets, epics);
  row.ticket = fields.ticket;
  row.epic = fields.epic;
  return row;
}

/** Read a row's own ticket key, tolerating rows that were never annotated. */
export function rowTicket(row) {
  if (!row) return '';
  if (row.ticket) return row.ticket;
  return ownTicket(row.tickets, null);
}

/** Key lists of every PR object in an `/api/objects` page. */
export function keyListsOf(objects) {
  return (objects || [])
    .filter((obj) => obj && obj.object_type === 'PR')
    .map((obj) => obj.ticket_keys || []);
}

/* ------------------------------------------------------------------ links
 *
 * prd/25: a ticket key is a doorway, not a label. Every surface that prints
 * one renders an anchor to the ticket detail page, so "why did DEL-167 take
 * three weeks" is one click from wherever the key appears. The hash route
 * (`#ticket/DEL-167`) needs no server route and survives a reload; a Markdown
 * export passes a base so the pasted link opens the same page.
 */

/** The store's key shape (mirrors the server's TICKET_KEY_RE). */
export const TICKET_KEY_RE = /^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$/;

/** Strip a `STORY:` / `TICKET:` object-id prefix. */
export function ticketKey(value) {
  const text = String(value == null ? '' : value).trim();
  return text.replace(/^(STORY|TICKET):/, '');
}

/** Is this whole string a ticket key (and not a sentence containing one)? */
export function isTicketKey(value) {
  return TICKET_KEY_RE.test(ticketKey(value));
}

/** The in-app route for a ticket. `base` is the page URL for copied Markdown. */
export function ticketHref(key, base = '') {
  return `${base}#ticket/${encodeURIComponent(ticketKey(key))}`;
}

/** An anchor for a ticket key; `cls` lets the caller match its surface. */
export function ticketLink(key, { base = '', cls = 'ticket-link', label = null } = {}) {
  const clean = ticketKey(key);
  if (!clean) return '';
  return `<a class="${cls}" href="${esc(ticketHref(clean, base))}" data-ticket="${esc(clean)}"` +
    ` title="open the ticket detail page for ${esc(clean)}">${esc(label || clean)}</a>`;
}

/** Escape plain text and turn any whole ticket-key token into a link. */
export function linkifyTickets(text, base = '') {
  const source = String(text == null ? '' : text);
  return source.split(/(\b[A-Z][A-Z0-9_]{0,9}-\d{1,6}\b)/).map((part) =>
    TICKET_KEY_RE.test(part) ? ticketLink(part, { base }) : esc(part)).join('');
}

/** Markdown links for whole ticket-key tokens (for "Copy as Markdown"). */
export function markdownLinkTickets(text, base = '') {
  return String(text == null ? '' : text)
    .replace(/\b[A-Z][A-Z0-9_]{0,9}-\d{1,6}\b/g, (key) =>
      isTicketKey(key) ? `[${key}](${ticketHref(key, base)})` : key);
}

/** Minimal HTML escape (tickets.js is imported by tests without format.js). */
function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}


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

/* prd/70 F-03: one canonical primary ticket per PR.
 *
 * The census below only demotes a key shared by a quarter of the PRs; a real
 * EPIC object named by three PRs (`DEMO-56715`) stays at index 0, and a PR can
 * name an old closed story next to the open one (`#11096` = open `DEMO-60130`
 * + 2023-done `DEMO-9714`). The store's own object rows say which key is a
 * story, which is an epic and which stories are still open, so the pick is
 * data-driven exactly like the server's `analysis.ballcourt.primary_ticket` —
 * one rule, so the label and the ticket page it opens cannot disagree.
 */

/** Ticket kinds that are work items (`TICKET` is the key dimension). */
export const STORY_KINDS = new Set(['STORY', 'TICKET']);
export const EPIC_KIND = 'EPIC';
/** The one live story state the store writes (a resolution makes it closed). */
export const LIVE_TICKET_STATE = 'open';

/** key → kind / key → state, refreshed from the store's object rows. */
let KIND_BY_KEY = null;
let STATE_BY_KEY = null;

function lookup(table, key) {
  if (!table) return undefined;
  if (table instanceof Map) return table.get(key);
  return table[key];
}

/** `DEMO-60130` → `60130`; `-1` when the key carries no number. */
export function ticketNumber(key) {
  const text = String(key == null ? '' : key);
  const suffix = text.slice(text.lastIndexOf('-') + 1);
  return /^\d+$/.test(suffix) ? Number(suffix) : -1;
}

/**
 * The one canonical ticket key for a PR (mirrors `primary_ticket`).
 *
 * Preference order: a `STORY`/`TICKET` key over an `EPIC`; a live (`open`)
 * story over a closed one; the most recent story (higher ticket number) on a
 * tie. `''` when the PR names no key — a key the index does not know is still
 * the PR's best evidence, so it is never dropped.
 */
export function primaryTicket(keys, { kindByKey = null, stateByKey = null } = {}) {
  const usable = (keys || [])
    .map((key) => String(key == null ? '' : key).trim())
    .filter(Boolean);
  if (!usable.length) return '';
  const rank = (key) => {
    const kind = String(lookup(kindByKey, key) || '').toUpperCase();
    const state = String(lookup(stateByKey, key) || '').trim().toLowerCase();
    return [
      STORY_KINDS.has(kind) ? 0 : (kind === EPIC_KIND ? 2 : 1),
      state === LIVE_TICKET_STATE ? 0 : (state ? 2 : 1),
      -ticketNumber(key),
    ];
  };
  return usable.reduce((best, key) => {
    const a = rank(key);
    const b = rank(best);
    return (a[0] - b[0] || a[1] - b[1] || a[2] - b[2]) < 0 ? key : best;
  });
}

/**
 * Refresh the kind/state index from an `/api/objects` page.
 *
 * `keyListsOf` (the client's one store census call) feeds this, so the ticket
 * helpers below read the same object rows the server does. A page with no
 * STORY/EPIC/TICKET rows clears the index and the helpers fall back to the
 * census rule, never to a guess.
 */
export function indexTickets(objects) {
  const kinds = new Map();
  const states = new Map();
  for (const obj of objects || []) {
    if (!obj) continue;
    const type = String(obj.object_type || '').toUpperCase();
    if (!STORY_KINDS.has(type) && type !== EPIC_KIND) continue;
    let keys = (obj.ticket_keys || []).filter(Boolean);
    if (!keys.length) {
      const id = String(obj.object_id || '');
      if (id.includes(':')) keys = [id.slice(id.indexOf(':') + 1)];
    }
    for (const raw of keys) {
      const key = String(raw || '').trim();
      if (!key) continue;
      const known = kinds.get(key);
      // a STORY row outranks a bare TICKET key dimension for the same key
      if (known === 'STORY' && type !== 'STORY') continue;
      kinds.set(key, type);
      if (obj.state) states.set(key, String(obj.state));
    }
  }
  KIND_BY_KEY = kinds.size ? kinds : null;
  STATE_BY_KEY = states.size ? states : null;
  return { kinds, states };
}

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

/** The epic/tracking key on this PR: the store's EPIC object, else the census. */
export function epicTicket(keys, epics = null) {
  const list = (keys || []).filter(Boolean);
  if (KIND_BY_KEY) {
    const known = list.find((key) => lookup(KIND_BY_KEY, key) === EPIC_KIND);
    if (known) return known;
  }
  if (!epics || !epics.size) return '';
  const found = list.find((key) => epics.has(key));
  return found || '';
}

/**
 * The canonical ticket key: the store index when it is loaded, else the census.
 *
 * Kept apart from `ownTicket` (which stays a pure census rule for the callers
 * that pass their own epic set), so the row labels and the ticket page links
 * use one data-driven answer (prd/70 F-03).
 */
export function canonicalTicket(keys, epics = null) {
  const primary = primaryTicket(keys, {
    kindByKey: KIND_BY_KEY,
    stateByKey: STATE_BY_KEY,
  });
  return primary || ownTicket(keys, epics);
}

/** `{ ticket, epic }` for one PR's key list. */
export function ticketFields(keys, epics = null) {
  return { ticket: canonicalTicket(keys, epics), epic: epicTicket(keys, epics) };
}

/** Annotate an API row in place (delivery rows carry `tickets`). */
export function annotateRow(row, epics = null) {
  if (!row) return row;
  const fields = ticketFields(row.tickets, epics);
  row.ticket = fields.ticket;
  row.epic = fields.epic;
  return row;
}

/** Read a row's own ticket key, tolerating a row that was never annotated. */
export function rowTicket(row) {
  if (!row) return '';
  if (row.ticket) return row.ticket;
  return canonicalTicket(row.tickets, null);
}

/**
 * Key lists of every PR object in an `/api/objects` page.
 *
 * Also refreshes the ticket index from the STORY/EPIC/TICKET rows on the same
 * page (prd/70 F-03): this is the one call that sees the store's whole object
 * census, and the canonical pick below needs it to know a story from an epic.
 */
export function keyListsOf(objects) {
  indexTickets(objects);
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

/**
 * Resolve a "go to ticket" box / pasted link to a key, or `''`.
 *
 * Accepts the forms people actually paste: `DEL-155`, `del-155`,
 * `STORY:DEL-155`, `TICKET:DEL-155`, `#ticket/DEL-155` and a full
 * `…/#ticket/DEL-155` URL. Anything else is not a key (the caller says so
 * instead of navigating to a 404).
 */
export function ticketFromQuery(raw) {
  let text = String(raw == null ? '' : raw).trim();
  const hash = text.indexOf('#ticket/');
  if (hash >= 0) text = text.slice(hash + '#ticket/'.length);
  try {
    text = decodeURIComponent(text);
  } catch { /* a malformed escape is simply not a key */ }
  const key = ticketKey(text.toUpperCase());
  return TICKET_KEY_RE.test(key) ? key : '';
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


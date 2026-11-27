/* Ticket titles, resolved in the browser (prd/80). **Pure: no DOM, no network.**
 *
 * A title is ticket *content*, so it never comes from megaspeed. It travels
 * Jira → the operator's browser → the screen. This module is the vocabulary
 * every transport shares:
 *
 *   - the two fields that may be requested at all (`TITLE_FIELDS`),
 *   - the three shapes Jira actually emits (CSV export, REST JSON, the
 *     bookmarklet payload) reduced to one import row `{key, summary, type}`,
 *   - the closed `type` vocabulary and its 14 px Jira glyph,
 *   - the cache's age/coverage arithmetic,
 *   - the `postMessage` shape check.
 *
 * >>> No user field is ever requested or stored. <<<
 * `assignee`, `reporter`, `creator` and every other user-valued field are
 * forbidden: a real name printed beside a store pseudonym is exactly the
 * `LaNo → person` map the pseudonymisation exists to prevent (prd/02). The
 * allowlist below is not a default; it is the whole list.
 *
 * `app/test/prd80-titles.test.mjs` asserts the allowlist, the parsers and the
 * "only summary/issuetype" rule.
 */

/** The literal allowlist. Nothing else is requested, parsed into the cache or shown. */
export const TITLE_FIELDS = ['summary', 'issuetype'];

/** A title older than this is rendered, but its age is stated (prd/80 §Cache). */
export const TITLE_TTL_DAYS = 30;

/** The same ticket-key grammar tickets.js uses; a title for anything else is dropped. */
export const TICKET_KEY_RE = /^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$/;

/** Own localStorage key, like `megaspeed.theme` — a preference, never a secret. */
export const TITLES_KEY = 'megaspeed.titles';
/** The non-secret transport configuration (site + cloud id). The token is never here. */
export const TITLES_CONFIG_KEY = 'megaspeed.titles.config';

export const TITLE_TYPES = ['epic', 'story', 'task', 'bug', 'subtask', 'other'];

/** The closed vocabulary, tolerant of Jira's spelling of Sub-task. */
const TYPE_ALIASES = {
  epic: 'epic',
  story: 'story',
  task: 'task',
  bug: 'bug',
  defect: 'bug',
  subtask: 'subtask',
  'sub task': 'subtask',
};

function text(value) {
  return String(value == null ? '' : value).trim();
}

/** A ticket key, upper-cased and validated, or `''`. */
export function normalizeKey(value) {
  const key = text(value).toUpperCase();
  return TICKET_KEY_RE.test(key) ? key : '';
}

/** `'Sub-task'` / `'SUBTASK'` / `'Sub task'` → the closed type id. */
export function normalizeType(name) {
  const raw = text(name).toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ');
  return TYPE_ALIASES[raw] || 'other';
}

/** The human name for a type id (the caller supplies it as the icon's a11y name). */
export function typeLabel(type) {
  switch (normalizeType(type)) {
    case 'epic': return 'Epic';
    case 'story': return 'Story';
    case 'task': return 'Task';
    case 'bug': return 'Bug';
    case 'subtask': return 'Sub-task';
    default: return 'Other';
  }
}

/* Jira's own glyph + palette at 14 px. Emoji (🟣/🟢/🔵) are indistinguishable at
 * that size and do not read as Jira, so the glyph is inline SVG. It is our own
 * constant string — no ticket text ever passes through it — and the caller
 * supplies the accessible name (`aria-label`), because the SVG is decorative. */
const GLYPHS = {
  epic: '<path d="M9.6 1 2.8 9h3.9l-1 6 7.5-8.2H9.2l1.6-5.8Z" fill="#6554C0"/>',
  story: '<path d="M4 1.8h8a1 1 0 0 1 1 1v11.4l-5-3-5 3V2.8a1 1 0 0 1 1-1Z" fill="#36B37E"/>',
  task: '<rect x="2" y="2" width="12" height="12" rx="2.4" fill="#4BADE8"/>'
    + '<path d="m4.9 8.1 2 2 4.2-4.4" fill="none" stroke="#fff" stroke-width="1.6"'
    + ' stroke-linecap="round" stroke-linejoin="round"/>',
  bug: '<path d="M5.2 3.1 3.2 1.2M10.8 3.1l2-1.9" stroke="#E5493A" stroke-width="1.3"'
    + ' stroke-linecap="round"/><circle cx="8" cy="8.6" r="4.4" fill="#E5493A"/>'
    + '<path d="M8 4.4v8.4" stroke="#fff" stroke-width="1.1" stroke-linecap="round"/>',
  subtask: '<path d="M3 13V7.2A3.2 3.2 0 0 1 6.2 4h6.2" fill="none" stroke="#4BADE8"'
    + ' stroke-width="1.7" stroke-linecap="round"/><path d="m10.2 1.6 3 2.4-3 2.4"'
    + ' fill="none" stroke="#4BADE8" stroke-width="1.6" stroke-linecap="round"'
    + ' stroke-linejoin="round"/>',
  other: '<rect x="3" y="3" width="10" height="10" rx="2.2" fill="#6B778C"/>',
};

/** A 14 px inline SVG for the type; `aria-hidden` — the caller names it. */
export function typeGlyph(type) {
  const id = TITLE_TYPES.includes(type) ? type : normalizeType(type);
  return `<svg class="type-glyph" data-type="${esc(id)}" width="14" height="14"`
    + ' viewBox="0 0 16 16" aria-hidden="true" focusable="false"'
    + `>${GLYPHS[id] || GLYPHS.other}</svg>`;
}

/* ---------------------------------------------------------------- parsing */

/* Column names as Jira's own export writes them. `Issue key` and `Issue Key`
 * are both seen; the type column is `Issue type` in the CSV export and
 * `Issue Type` in some locales/exports. */
const COLUMN_ALIASES = {
  key: ['issue key', 'issuekey', 'key', 'ticket', 'ticket key'],
  summary: ['summary', 'title'],
  type: ['issue type', 'issuetype', 'type'],
};

function columnName(value) {
  return text(value).toLowerCase().replace(/[\s_]+/g, ' ').trim();
}

function columnIndex(header, names) {
  for (let i = 0; i < header.length; i += 1) {
    if (names.includes(columnName(header[i]))) return i;
  }
  return -1;
}

/** RFC-4180-ish CSV → records of fields (quoted commas, newlines, `""`). */
export function csvRecords(source) {
  const src = String(source == null ? '' : source).replace(/^\uFEFF/, '');
  const records = [];
  let record = [];
  let field = '';
  let quoted = false;
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    if (quoted) {
      if (ch === '"') {
        if (src[i + 1] === '"') { field += '"'; i += 2; continue; }
        quoted = false;
        i += 1;
        continue;
      }
      field += ch;
      i += 1;
      continue;
    }
    if (ch === '"') { quoted = true; i += 1; continue; }
    if (ch === ',') { record.push(field); field = ''; i += 1; continue; }
    if (ch === '\r') { i += 1; continue; }
    if (ch === '\n') {
      record.push(field);
      records.push(record);
      record = [];
      field = '';
      i += 1;
      continue;
    }
    field += ch;
    i += 1;
  }
  if (field !== '' || record.length) {
    record.push(field);
    records.push(record);
  }
  return records;
}

/**
 * Jira *Export → CSV (all fields)* (transport A).
 *
 * Returns `{rows:[{key, summary, type}], skipped, error?}`. `type` is the
 * closed vocabulary, not Jira's free-text name. A row that is not a ticket key,
 * or that has no summary, is counted and skipped — never half-written.
 */
export function parseCsv(source) {
  const records = csvRecords(source);
  if (!records.length) return { rows: [], skipped: 0 };
  const header = records[0];
  const keyAt = columnIndex(header, COLUMN_ALIASES.key);
  const summaryAt = columnIndex(header, COLUMN_ALIASES.summary);
  const typeAt = columnIndex(header, COLUMN_ALIASES.type);
  const body = records.slice(1).filter((row) => row.some((cell) => text(cell) !== ''));
  if (keyAt < 0 || summaryAt < 0) {
    return { rows: [], skipped: body.length, error: 'no key/summary column' };
  }
  const rows = [];
  let skipped = 0;
  for (const record of body) {
    const key = normalizeKey(record[keyAt]);
    const summary = text(record[summaryAt]);
    if (!key || !summary) { skipped += 1; continue; }
    rows.push({ key, summary, type: typeAt < 0 ? 'other' : normalizeType(record[typeAt]) });
  }
  return { rows, skipped };
}

/** One REST issue / flat row / bookmarklet row → `{key, summary, type}`, or null. */
export function titleRow(item) {
  if (!item || typeof item !== 'object') return null;
  const fields = item.fields && typeof item.fields === 'object' ? item.fields : null;
  const key = normalizeKey(item.key || item.issue_key || item.issueKey);
  const summary = text(item.summary != null
    ? item.summary
    : (fields ? fields.summary : ''));
  if (!key || !summary) return null;
  const rawType = item.type != null
    ? item.type
    : (fields
      ? (fields.issuetype && (fields.issuetype.name || fields.issuetype))
      : '');
  return { key, summary, type: normalizeType(rawType) };
}

function rowsFromList(list) {
  const rows = [];
  let skipped = 0;
  for (const item of list) {
    const row = titleRow(item);
    if (row) rows.push(row);
    else skipped += 1;
  }
  return { rows, skipped };
}

/**
 * The other two shapes (transport A/B):
 *
 *   - the Jira REST shape: `{issues:[{key, fields:{summary, issuetype:{name}}}]}`
 *   - the flat / bookmarklet shape: `[{key, summary, type}]`
 *   - the bookmarklet envelope: `{type:'megaspeed.titles', v:1, rows:[…]}`
 *
 * Malformed JSON is an empty import, never a throw: the panel reports nothing
 * imported rather than a stack.
 */
export function parseJson(source) {
  let data;
  try {
    data = JSON.parse(String(source == null ? '' : source));
  } catch {
    return { rows: [], skipped: 0, error: 'not JSON' };
  }
  if (Array.isArray(data)) return rowsFromList(data);
  if (data && typeof data === 'object') {
    if (Array.isArray(data.issues)) return rowsFromList(data.issues);
    if (Array.isArray(data.rows)) return rowsFromList(data.rows);
    if (data.payload && Array.isArray(data.payload.rows)) return rowsFromList(data.payload.rows);
  }
  return { rows: [], skipped: 0, error: 'unknown shape' };
}

/** Pick the parser that fits the text: JSON if it parses, CSV otherwise. */
export function parseTitles(source) {
  const raw = text(source);
  if (!raw) return { rows: [], skipped: 0 };
  if (raw.startsWith('{') || raw.startsWith('[')) {
    const parsed = parseJson(raw);
    if (!parsed.error) return parsed;
  }
  return parseCsv(raw);
}

/**
 * The origin/scheme check for the bookmarklet handshake (transport B).
 *
 * The payload is accepted only when it came from the configured Jira origin and
 * has exactly the agreed shape: `{type:'megaspeed.titles', v:1, rows:[…]}`
 * with every row a `{key, summary, type}` whose key is a real ticket key. A
 * message from any other origin is ignored, not logged (a screen-shared tab
 * must not fill the console).
 */
export function readTitlesMessage(event, expectedOrigin) {
  if (!event || !expectedOrigin) return null;
  if (text(event.origin) !== text(expectedOrigin)) return null;
  const data = event.data;
  if (!data || typeof data !== 'object') return null;
  if (data.type !== 'megaspeed.titles' || data.v !== 1) return null;
  if (!Array.isArray(data.rows)) return null;
  const parsed = rowsFromList(data.rows);
  return parsed.rows.length ? parsed : null;
}

/**
 * The receiver's window name (prd/80). The panel claims it while the Titles
 * view is open, and the bookmarklet reuses the window of that name instead of
 * opening a duplicate — and, crucially, instead of depending on a popup the
 * browser may block.
 */
export const TITLES_WINDOW_NAME = 'megaspeed-titles';

/**
 * Why a `message` did or did not become rows.
 *
 * The receiver used to be silent on rejection, which made the three common
 * failures indistinguishable from "the operator never clicked": a bookmarklet
 * window the browser blocked, a site field naming the wrong Jira, and an empty
 * payload. The panel prints this explanation (prd/80 §The Titles panel).
 */
export function describeMessage(event, expectedOrigin) {
  const origin = text(event && event.origin) || '';
  const expected = text(expectedOrigin) || '';
  const base = { origin, expected, rows: [], skipped: 0 };
  if (!expected) return { ...base, accepted: false, reason: 'no-site' };
  if (origin !== expected) return { ...base, accepted: false, reason: 'origin' };
  const data = event ? event.data : null;
  if (!data || typeof data !== 'object' || data.type !== 'megaspeed.titles' || data.v !== 1) {
    return { ...base, accepted: false, reason: 'shape' };
  }
  const parsed = readTitlesMessage(event, expected);
  if (!parsed) return { ...base, accepted: false, reason: 'empty' };
  return { ...base, accepted: true, reason: 'ok', rows: parsed.rows, skipped: parsed.skipped };
}

/** One line for the panel: what the last message was, and why it was dropped. */
export function messageLine(described) {
  if (!described || typeof described !== 'object') return '';
  if (described.accepted) {
    const n = described.rows.length;
    return `bookmarklet: ${n} row${n === 1 ? '' : 's'} from ${described.origin}`;
  }
  const from = described.origin || 'an unknown origin';
  if (described.reason === 'no-site') {
    return `a message arrived from ${from} — set the Jira site above so it can be accepted`;
  }
  if (described.reason === 'origin') {
    return `message from ${from} rejected — the site field says ${described.expected}`;
  }
  if (described.reason === 'empty') return `message from ${from} carried no usable rows`;
  return `message from ${from} was not a titles payload`;
}


/* -------------------------------------------------------------- arithmetic */

/** Days since `fetchedAt`, floored, or null. */
export function ageDays(fetchedAt, now = Date.now()) {
  const stamp = Number(fetchedAt);
  if (!Number.isFinite(stamp) || stamp <= 0) return null;
  const delta = Number(now) - stamp;
  if (!Number.isFinite(delta)) return null;
  return Math.floor(delta / 86_400_000);
}

/** Is this cached title older than the TTL? Unknown ages count as fresh. */
export function isStale(fetchedAt, { now = Date.now(), ttlDays = TITLE_TTL_DAYS } = {}) {
  const days = ageDays(fetchedAt, now);
  return days != null && days > ttlDays;
}

/** "today" / "1 day old" / "41 days old" — the popover footer's age phrase. */
export function formatAge(days) {
  if (days == null || days === '') return '';
  const value = Number(days);
  if (!Number.isFinite(value) || value < 0) return '';
  const whole = Math.floor(value);
  if (whole <= 0) return 'today';
  return whole === 1 ? '1 day old' : `${whole} days old`;
}

/** "412 of 600 keys have a title" — the panel's coverage line. */
export function coverage(rows, keys) {
  const have = new Set();
  for (const row of rows || []) {
    const key = normalizeKey(row && row.key);
    if (key) have.add(key);
  }
  const wanted = new Set();
  for (const raw of keys || []) {
    const key = normalizeKey(raw);
    if (key) wanted.add(key);
  }
  const total = wanted.size;
  let covered = 0;
  for (const key of wanted) if (have.has(key)) covered += 1;
  return {
    covered,
    total,
    percent: total ? Math.round((covered / total) * 100) : 0,
    text: `${covered} of ${total} keys have a title`,
  };
}

/** "imported 412 · skipped 3 · 1886 unchanged" — one honest import line. */
export function importLine({ imported = 0, skipped = 0, unchanged = 0 } = {}) {
  const n = (value) => Number(value) || 0;
  return `imported ${n(imported)} · skipped ${n(skipped)} · ${n(unchanged)} unchanged`;
}

/* ------------------------------------------------------------------- toggle */

/** `'on' | 'off' | null` — anything else is not a preference. */
export function normalizeTitlesPref(value) {
  const raw = text(value).toLowerCase();
  return raw === 'on' || raw === 'off' ? raw : null;
}

/** The toggle's boolean reading; an absent/unknown preference means on. */
export function titlesEnabled(pref) {
  return normalizeTitlesPref(pref) !== 'off';
}

function safeStorage() {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export function readTitlesPref(storage = safeStorage()) {
  try {
    return normalizeTitlesPref(storage && storage.getItem(TITLES_KEY));
  } catch {
    return null;
  }
}

export function storeTitlesPref(enabled, storage = safeStorage()) {
  const value = enabled ? 'on' : 'off';
  try {
    if (storage) storage.setItem(TITLES_KEY, value);
  } catch { /* private mode: the toggle still works for this session */ }
  return value;
}

/** The non-secret transport configuration; the token never appears here. */
export function readTitlesConfig(storage = safeStorage()) {
  try {
    const raw = storage && storage.getItem(TITLES_CONFIG_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (!parsed || typeof parsed !== 'object') return { site: '', cloudId: '' };
    return { site: text(parsed.site), cloudId: text(parsed.cloudId) };
  } catch {
    return { site: '', cloudId: '' };
  }
}

export function storeTitlesConfig({ site = '', cloudId = '' } = {}, storage = safeStorage()) {
  const value = { site: text(site), cloudId: text(cloudId) };
  try {
    if (storage) storage.setItem(TITLES_CONFIG_KEY, JSON.stringify(value));
  } catch { /* private mode */ }
  return value;
}

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

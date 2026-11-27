/* The title cache (prd/80 §Cache): IndexedDB, one object store, `{summary, type,
 * fetched_at}` per key. **No DOM, no network.**
 *
 * Why not `localStorage`: it is synchronous, ~5 MB capped, and would sit beside
 * unrelated UI state. IndexedDB is the right shape for "a few thousand rows of
 * optional text" and it survives a reload without touching the app's settings.
 *
 * The storage engine is injectable on purpose: every function takes a tiny
 * store object —
 *
 *     get(key)      -> value | undefined
 *     put(key, v)
 *     remove(key)
 *     entries()     -> [ [key, value], … ]
 *
 * — so `node --test` runs the whole merge/forget/tenant logic against
 * `memoryStore()` without a browser, and `openCache()` is the only code that
 * knows IndexedDB exists (and it degrades to memory when there is none).
 *
 * The value is exactly `{summary, type, fetched_at}`. Nothing else is written,
 * ever: a description or a user name reaching browser storage is the category
 * of data prd/02 forbids, and the cache must not become the place it lands.
 */

import { TICKET_KEY_RE, normalizeKey, normalizeType } from './titles.js';

/** Same names the PRD uses, so a devtools inspect matches the doc. */
export const TITLES_DB = 'megaspeed.titles';
export const TITLES_STORE = 'titles';

/** `${tenant}|${ticketKey}` — per tenant, so `Forget titles` is one prefix. */
export function cacheKey(tenant, key) {
  return `${tenant}|${key}`;
}

function tenantOf(composite) {
  const at = String(composite).indexOf('|');
  return at < 0 ? '' : String(composite).slice(0, at);
}

function ticketOf(composite) {
  const at = String(composite).indexOf('|');
  return at < 0 ? '' : String(composite).slice(at + 1);
}

/** Only the three fields; a hand-edited or older value cannot smuggle more in. */
function cleanValue(value) {
  if (!value || typeof value !== 'object') return null;
  const summary = String(value.summary == null ? '' : value.summary).trim();
  if (!summary) return null;
  const stamp = Number(value.fetched_at);
  return {
    summary,
    type: normalizeType(value.type),
    fetched_at: Number.isFinite(stamp) && stamp > 0 ? stamp : 0,
  };
}

async function call(store, name, ...args) {
  const fn = store && (store[name] || (name === 'remove' ? store.delete : null));
  if (typeof fn !== 'function') return undefined;
  return fn.apply(store, args);
}

async function entriesOf(store) {
  const result = await call(store, 'entries');
  if (!result) return [];
  if (result instanceof Map) return [...result.entries()];
  if (typeof result[Symbol.iterator] === 'function') return [...result];
  return [];
}

/** A Map-backed store: the test double, and the fallback when IndexedDB is absent. */
export function memoryStore(initial = null) {
  const map = new Map(initial || []);
  return {
    name: 'memory',
    map,
    get: async (key) => map.get(key),
    put: async (key, value) => { map.set(key, value); },
    remove: async (key) => { map.delete(key); },
    entries: async () => [...map.entries()],
  };
}

/* ------------------------------------------------------------- IndexedDB */

function requestOf(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('IndexedDB request failed'));
  });
}

/**
 * Open (and, on first use, create) the `titles` object store. Resolves to the
 * same tiny store interface the tests fake. A browser with IndexedDB disabled
 * (or a `file://` page) gets the in-memory store, so the feature degrades to
 * "titles for this tab" rather than throwing on boot.
 */
export async function openCache({
  factory = typeof indexedDB === 'undefined' ? null : indexedDB,
  dbName = TITLES_DB,
  storeName = TITLES_STORE,
} = {}) {
  if (!factory) return memoryStore();
  let db;
  try {
    db = await new Promise((resolve, reject) => {
      const request = factory.open(dbName, 1);
      request.onupgradeneeded = () => {
        const upgraded = request.result;
        if (!upgraded.objectStoreNames.contains(storeName)) upgraded.createObjectStore(storeName);
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('IndexedDB open failed'));
      request.onblocked = () => reject(new Error('IndexedDB blocked by another tab'));
    });
  } catch {
    return memoryStore();
  }
  return {
    name: dbName,
    get: (key) => new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, 'readonly');
      const request = tx.objectStore(storeName).get(key);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    }),
    put: (key, value) => new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, 'readwrite');
      tx.objectStore(storeName).put(value, key);
      tx.oncomplete = () => resolve(value);
      tx.onerror = () => reject(tx.error);
    }),
    remove: (key) => new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, 'readwrite');
      tx.objectStore(storeName).delete(key);
      tx.oncomplete = () => resolve(true);
      tx.onerror = () => reject(tx.error);
    }),
    entries: () => new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, 'readonly');
      const request = tx.objectStore(storeName).getAllKeys();
      const values = tx.objectStore(storeName).getAll();
      tx.oncomplete = () => resolve(request.result.map((key, index) => [key, values.result[index]]));
      tx.onerror = () => reject(tx.error);
    }),
  };
}

/* --------------------------------------------------------------- the cache */

/**
 * The cached titles for `keys`, as `Map(ticketKey -> {summary, type, fetched_at})`.
 * A key with no row is simply absent, so the renderer draws nothing for it.
 */
export async function getTitles(tenant, keys, store) {
  const wanted = new Set();
  for (const raw of keys || []) {
    const key = normalizeKey(raw);
    if (key) wanted.add(key);
  }
  const out = new Map();
  if (!store || !wanted.size) return out;
  await Promise.all([...wanted].map(async (key) => {
    const value = cleanValue(await call(store, 'get', cacheKey(tenant, key)));
    if (value) out.set(key, value);
  }));
  return out;
}

/**
 * Merge, never replace (prd/80 §Cache): a partial export overwrites the keys it
 * carries and leaves every other row alone, so it cannot blank the cache.
 * Idempotent: importing the same rows twice reports `unchanged` the second time
 * and writes nothing.
 *
 * Returns `{imported, unchanged, skipped}` for the panel's one-line report.
 */
export async function mergeTitles(tenant, rows, store, { now = Date.now() } = {}) {
  const result = { imported: 0, unchanged: 0, skipped: 0 };
  if (!store) {
    result.skipped = (rows || []).length;
    return result;
  }
  for (const row of rows || []) {
    const key = normalizeKey(row && row.key);
    const summary = String((row && row.summary) == null ? '' : row.summary).trim();
    if (!key || !summary || !TICKET_KEY_RE.test(key)) {
      result.skipped += 1;
      continue;
    }
    const composite = cacheKey(tenant, key);
    const stamp = Number(row && row.fetched_at);
    const next = {
      summary,
      type: normalizeType(row && row.type),
      fetched_at: Number.isFinite(stamp) && stamp > 0 ? stamp : now,
    };
    const prev = cleanValue(await call(store, 'get', composite));
    // The same summary and type is not an import: it keeps the original
    // `fetched_at`, so re-importing an old export does not fake freshness.
    if (prev && prev.summary === next.summary && prev.type === next.type) {
      result.unchanged += 1;
      continue;
    }
    await call(store, 'put', composite, next);
    result.imported += 1;
  }
  return result;
}

/** Drop one tenant's rows; every other tenant's cache is untouched. */
export async function clearTitles(tenant, store) {
  if (!store) return 0;
  const prefix = `${tenant}|`;
  let removed = 0;
  for (const [composite] of await entriesOf(store)) {
    if (!String(composite).startsWith(prefix)) continue;
    await call(store, 'remove', composite);
    removed += 1;
  }
  return removed;
}

/** Every cached row of one tenant, as `[{key, summary, type, fetched_at}]`. */
export async function titlesOf(tenant, store) {
  if (!store) return [];
  const rows = [];
  for (const [composite, value] of await entriesOf(store)) {
    if (tenantOf(composite) !== String(tenant)) continue;
    const clean = cleanValue(value);
    const key = ticketOf(composite);
    if (!clean || !key) continue;
    rows.push({ key, ...clean });
  }
  return rows;
}

/** `{total, oldest, newest, byType}` — what the panel's status block prints. */
export async function stats(tenant, store) {
  const rows = await titlesOf(tenant, store);
  const byType = {};
  let oldest = null;
  let newest = null;
  for (const row of rows) {
    byType[row.type] = (byType[row.type] || 0) + 1;
    if (!row.fetched_at) continue;
    if (oldest == null || row.fetched_at < oldest) oldest = row.fetched_at;
    if (newest == null || row.fetched_at > newest) newest = row.fetched_at;
  }
  return { tenant, total: rows.length, oldest, newest, byType };
}

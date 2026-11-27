/* The title transports (prd/80 §Transports): probes, the CORS-gateway client
 * and the bookmarklet snippet. **No DOM. The one thing this module may touch is
 * the network, and only two origins are ever contacted.**
 *
 *   - the configured Jira site — `https://<site>/_edge/tenant_info`, and the
 *     `rest/api/3/search/jql` call the bookmarklet makes *from the Jira tab*;
 *   - `https://api.atlassian.com` — the CORS-whitelisted gateway that makes a
 *     pasted OAuth access token work from this page.
 *
 * Nothing else, ever. There is no megaspeed endpoint here, no same-origin URL
 * and no proxy: ticket content must not transit the megaspeed server (prd/80
 * §The invariant). `isAllowedTitleUrl()` is that rule in code and
 * `app/test/prd80-titles.test.mjs` asserts it with a recording `fetch`.
 *
 * Transport B (the bookmarklet) is a pure *string*: `bookmarkletCode()` builds
 * the snippet so the panel can drag it, copy it, and a test can snapshot it.
 *
 * Everything returns `{ok, status, error}` and never throws: the panel must be
 * able to print "CORS blocked" rather than a stack trace.
 */

import { TITLES_WINDOW_NAME, TITLE_FIELDS, normalizeKey, titleRow } from './titles.js';

/** The only non-Jira origin the feature may contact. */
export const GATEWAY_ORIGIN = 'https://api.atlassian.com';
/** Jira Cloud's token lifetime: one hour per paste, and nothing refreshes it here. */
export const TOKEN_TTL_SECONDS = 3600;
/** Atlassian's JQL search caps a page at 100; 50 keeps every batch small. */
export const TITLE_BATCH = 50;

const CORS_ERROR = 'cors-or-network';

/** `acme.atlassian.net` / `https://acme.atlassian.net/` → `https://acme.atlassian.net`. */
export function siteBase(site) {
  let raw = String(site == null ? '' : site).trim();
  if (!raw) return '';
  if (!/^https?:\/\//i.test(raw)) raw = `https://${raw}`;
  try {
    const url = new URL(raw);
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return '';
    return url.origin;
  } catch {
    return '';
  }
}

/** Jira Cloud's CORS-open cloud-id door, on the configured site only. */
export function tenantInfoUrl(site) {
  const base = siteBase(site);
  return base ? `${base}/_edge/tenant_info` : '';
}

/** The gateway URL for a Jira REST path (`/rest/api/3/...`). */
export function gatewayUrl(cloudId, path = '') {
  const id = String(cloudId == null ? '' : cloudId).trim();
  if (!id) return '';
  return `${GATEWAY_ORIGIN}/ex/jira/${encodeURIComponent(id)}${path}`;
}

/**
 * The allowlist: a title request may go to the configured Jira site or to
 * `api.atlassian.com`, and nowhere else. A relative URL (so: megaspeed itself)
 * is never allowed, and neither is `api.atlassian.com.evil.example`.
 */
export function isAllowedTitleUrl(url, site = '') {
  const target = String(url == null ? '' : url);
  if (!/^https?:\/\//i.test(target)) return false;
  if (target.startsWith(`${GATEWAY_ORIGIN}/`)) return true;
  const base = siteBase(site);
  if (!base) return false;
  return target === base || target.startsWith(`${base}/`);
}

function fetchImplOf(fetchImpl) {
  if (typeof fetchImpl === 'function') return fetchImpl;
  return typeof fetch === 'function' ? fetch : null;
}

/**
 * One guarded request. `site` is the configured Jira origin the URL must belong
 * to; the gateway is always allowed. A blocked target is returned as an error,
 * never sent — so the allowlist is enforced at the one place a URL is used.
 */
async function titleFetch(url, init, fetchImpl, site = '') {
  if (!isAllowedTitleUrl(url, site)) {
    return { ok: false, status: 0, error: 'blocked-target', url };
  }
  const doFetch = fetchImplOf(fetchImpl);
  if (!doFetch) return { ok: false, status: 0, error: CORS_ERROR, url };
  try {
    const response = await doFetch(url, init || {});
    const status = Number(response && response.status) || 0;
    let type = '';
    try {
      const headers = response && response.headers;
      type = headers && typeof headers.get === 'function' ? String(headers.get('content-type') || '') : '';
    } catch { type = ''; }
    let body = '';
    try {
      body = response && typeof response.text === 'function' ? await response.text() : '';
    } catch { body = ''; }
    return { ok: status >= 200 && status < 300, status, type, body, url };
  } catch {
    // A browser CORS rejection is a `TypeError` with no status; an offline
    // browser looks the same from here. One honest error for both.
    return { ok: false, status: 0, error: CORS_ERROR, url };
  }
}

/**
 * A one-line sample of a probe response: status, content type and size only.
 *
 * Deliberately **not** a field from the body. `/myself` returns a display name
 * and an e-mail address; printing either in the panel would put a real person
 * next to a store pseudonym — the de-anonymisation channel prd/80 §Fields
 * forbids. The panel needs to know the call worked, not who answered it.
 */
export function probeSample({ status = 0, type = '', body = '' } = {}) {
  const ctype = String(type || '').split(';')[0].trim() || 'no content-type';
  return `HTTP ${Number(status) || 0} · ${ctype} · ${String(body || '').length} bytes`;
}

function statusError(status) {
  if (status === 401 || status === 403) return 'unauthorized';
  if (status === 429) return 'rate-limited';
  return 'http-error';
}

/** `https://<site>/_edge/tenant_info` → `{ok, cloudId, status, error}`. */
export async function discoverCloudId(site, fetchImpl = null) {
  const url = tenantInfoUrl(site);
  if (!url) return { ok: false, cloudId: '', status: 0, error: 'no-site' };
  const result = await titleFetch(url, { method: 'GET' }, fetchImpl, site);
  if (result.error) {
    return { ok: false, cloudId: '', status: result.status, error: result.error };
  }
  let id = '';
  try {
    const data = JSON.parse(result.body);
    id = data && typeof data.id === 'string' ? data.id : '';
  } catch { id = ''; }
  return {
    ok: Boolean(id), cloudId: id, status: result.status, error: id ? '' : 'no-cloud-id',
  };
}

function authHeaders(token, mode = 'bearer') {
  const secret = String(token == null ? '' : token).trim();
  if (!secret) return {};
  const scheme = mode === 'basic' ? 'Basic' : 'Bearer';
  return { Authorization: `${scheme} ${secret}` };
}

/**
 * Probe 2 (transport C): does the gateway answer a browser `fetch` at all?
 * Returns `{ok, status, sample, error}`. A CORS rejection is
 * `{ok:false, status:0, error:'cors-or-network'}` — the panel says "CORS
 * blocked", the console stays clean.
 */
export async function testGateway({ cloudId, token, mode = 'bearer' } = {}, fetchImpl = null) {
  const url = gatewayUrl(cloudId, '/rest/api/3/myself');
  if (!url) return { ok: false, status: 0, sample: '', error: 'no-cloud-id' };
  const result = await titleFetch(url, {
    method: 'GET',
    headers: { Accept: 'application/json', ...authHeaders(token, mode) },
  }, fetchImpl);
  if (result.error) {
    return {
      ok: false, status: result.status || 0, sample: '', error: result.error,
    };
  }
  return {
    ok: result.ok,
    status: result.status,
    sample: probeSample(result),
    error: result.ok ? '' : statusError(result.status),
  };
}

/** Probe 3: the same call with `Authorization: Basic …` and an API token. */
export async function testBasicGateway({ cloudId, token } = {}, fetchImpl = null) {
  return testGateway({ cloudId, token, mode: 'basic' }, fetchImpl);
}

/** Unique, validated, upper-cased keys — never free text in a JQL string. */
export function titleKeys(keys) {
  const out = [];
  const seen = new Set();
  for (const raw of keys || []) {
    const key = normalizeKey(raw);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(key);
  }
  return out;
}

/** **Only** `key` is interpolated, and only after the ticket-key grammar. */
export function titleJql(keys) {
  const safe = titleKeys(keys);
  return safe.length ? `key in (${safe.join(',')})` : '';
}

/** Split the key list into batches of at most `size` (≤ 50). */
export function titleBatches(keys, size = TITLE_BATCH) {
  const safe = titleKeys(keys);
  const width = Math.max(1, Math.min(Number(size) || TITLE_BATCH, TITLE_BATCH));
  const batches = [];
  for (let i = 0; i < safe.length; i += width) batches.push(safe.slice(i, i + width));
  return batches;
}

/**
 * Fetch titles for `keys` through the CORS gateway (transport C).
 *
 * The request body is the allowlist and nothing else: `fields: TITLE_FIELDS`
 * (`['summary','issuetype']`) plus an exact `key in (…)` JQL. Batches of ≤ 50.
 * Returns `{ok, status, rows, batches, skipped, error}`; `rows` is already the
 * shared import shape, so a 401 halfway through still merges what arrived.
 */
export async function fetchTitles({
  cloudId, token, keys, mode = 'bearer', batchSize = TITLE_BATCH,
} = {}, fetchImpl = null) {
  const batches = titleBatches(keys, batchSize);
  const rows = [];
  let skipped = 0;
  if (!batches.length) {
    return { ok: true, status: 0, rows, batches: 0, skipped, error: '' };
  }
  const url = gatewayUrl(cloudId, '/rest/api/3/search/jql');
  if (!url) {
    return { ok: false, status: 0, rows, batches: 0, skipped, error: 'no-cloud-id' };
  }
  for (const batch of batches) {
    const result = await titleFetch(url, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        ...authHeaders(token, mode),
      },
      body: JSON.stringify({
        jql: titleJql(batch),
        fields: [...TITLE_FIELDS],
        maxResults: batch.length,
      }),
    }, fetchImpl);
    if (result.error) {
      return {
        ok: false, status: result.status || 0, rows, batches: batches.length, skipped, error: result.error,
      };
    }
    if (!result.ok) {
      return {
        ok: false,
        status: result.status,
        rows,
        batches: batches.length,
        skipped,
        error: statusError(result.status),
      };
    }
    let issues = [];
    try {
      const data = JSON.parse(result.body);
      issues = data && Array.isArray(data.issues) ? data.issues : [];
    } catch { issues = []; }
    for (const issue of issues) {
      const row = titleRow(issue);
      if (row) rows.push(row);
      else skipped += 1;
    }
  }
  return { ok: true, status: 200, rows, batches: batches.length, skipped, error: '' };
}

/**
 * The bookmarklet source (transport B) — a **pure string**, so the panel can
 * drag it, copy it, and a test can snapshot it.
 *
 * It runs on the Jira tab, where the operator's existing session is
 * same-origin, and therefore needs no token, no secret and no CORS favour:
 *
 *   1. `POST /rest/api/3/search/jql` with `credentials:'same-origin'` and
 *      `fields: ['summary','issuetype']` — the two fields and nothing else;
 *   2. `window.open(targetOrigin + '/?view=titles')`;
 *   3. `postMessage({type:'megaspeed.titles', v:1, rows}, targetOrigin)`.
 *
 * The key list is baked into the snippet (the store's own epic/ticket set at
 * generation time), in batches of 50. The message is re-sent for ~20 s because
 * the opened tab may still be booting; the panel's merge is idempotent, so a
 * duplicate is a no-op.
 */
export function bookmarkletCode({ site = '', targetOrigin = '', keys = [] } = {}) {
  const base = siteBase(site);
  const origin = String(targetOrigin == null ? '' : targetOrigin).replace(/\/+$/, '');
  const wanted = titleKeys(keys);
  const body = '(function(){'
    + `var T=${JSON.stringify(TITLE_FIELDS)};`
    + `var K=${JSON.stringify(wanted)};`
    + `var S=${JSON.stringify(base)};`
    + `var O=${JSON.stringify(origin)};`
    + `var N=${JSON.stringify(TITLES_WINDOW_NAME)};`
    + 'var rows=[],sent=-1,tries=0,w=null;'
    // prd/80: reuse a megaspeed tab that has claimed the name instead of always
    // opening a popup. A popup the browser blocks used to leave `w` null and the
    // whole snippet silently did nothing; now the blocked case is said out loud
    // and the rows are handed over on the clipboard (the panel has a paste box).
    + 'try{w=window.open("",N);}catch(e){w=null;}'
    + 'if(w){try{if(!w.location.href||w.location.href==="about:blank")'
    + '{w.location.href=O+"/?view=titles";}}catch(e){}}'
    + 'function payload(){return {type:"megaspeed.titles",v:1,rows:rows};}'
    + 'function send(){if(!w||w.closed)return;'
    + 'if(rows.length===sent)return;sent=rows.length;'
    + 'try{w.postMessage(payload(),O);}catch(e){}}'
    + 'function fallback(){var t=JSON.stringify(payload());'
    + 'var msg="megaspeed could not open a window for the titles. They are on your clipboard (or in the next dialog): paste them into the Titles panel.";'
    + 'try{if(navigator.clipboard&&navigator.clipboard.writeText){'
    + 'navigator.clipboard.writeText(t).then(function(){alert(msg);},function(){'
    + 'window.prompt(msg,t);});return;}}catch(e){}'
    + 'window.prompt(msg,t);}'
    + 'function pump(){send();if(++tries<20)setTimeout(pump,1000);}'
    + 'var chain=Promise.resolve();'
    + 'for(var i=0;i<K.length;i+=50){(function(batch){'
    + 'chain=chain.then(function(){'
    + 'return fetch(S+"/rest/api/3/search/jql",{method:"POST",credentials:"same-origin",'
    + 'headers:{"Content-Type":"application/json","Accept":"application/json"},'
    + 'body:JSON.stringify({jql:"key in ("+batch.join(",")+")",fields:T,maxResults:50})})'
    + '.then(function(r){return r.json();})'
    + '.then(function(d){(d&&d.issues||[]).forEach(function(issue){'
    + 'var f=issue.fields||{},t=f.issuetype||{};'
    + 'rows.push({key:issue.key,summary:f.summary||"",type:t.name||""});});send();});'
    + '});})(K.slice(i,i+50));}'
    + 'chain.then(function(){if(!w||w.closed)fallback();},'
    + 'function(e){try{console.warn("megaspeed titles: "+((e&&e.message)||e));}catch(x){}'
    + 'if(!w||w.closed)fallback();});'
    + 'pump();'
    + '})();';
  return `javascript:${body}`;
}

/** The console fallback: the same code without the `javascript:` scheme. */
export function bookmarkletSnippet(options = {}) {
  return bookmarkletCode(options).replace(/^javascript:/, '');
}

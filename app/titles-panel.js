/* The Titles panel (prd/80 §The Titles panel) and the DOM half of hydration.
 *
 * `?view=titles` is not a console workflow: it is simultaneously
 *
 *   - the importer (transport A: a CSV/JSON export dropped or pasted),
 *   - the status board (coverage, freshness, staleness),
 *   - the probe bench (transport B: the draggable bookmarklet; transport C:
 *     cloud-id discovery + the pasted token + the two gateway probes),
 *   - the danger zone (Forget titles) and the mirror of the topbar toggle.
 *
 * `titlesMarkup()` is pure — a view object in, an HTML string out — so the
 * panel can be asserted without a browser. `wireTitlesPanel()` delegates from
 * the container, so re-rendering its innerHTML never loses a handler.
 *
 * The hydration half is here too, because it is the feature's only DOM module:
 * `hydrateTitles()` annotates the `[data-ticket]` anchors `ticketLink()` already
 * emits (no call site changes), `annotateDetailTitle()` adds the ticket page's
 * subtitle, and `wireTitlePopover()` is ONE delegated listener pair on the
 * document keyed by `data-ticket` — so a re-render costs nothing.
 *
 * Rendering rules (prd/80 §Rules 5/8): summary text is only ever written with
 * `textContent` / `createTextNode`; the only `innerHTML` is `typeGlyph()`, our
 * own constant SVG. With the toggle off nothing is annotated at all.
 */

import {
  readTitlesMessage, TITLE_FIELDS, TITLE_TTL_DAYS, ageDays, formatAge,
  importLine, parseTitles, typeGlyph, typeLabel,
} from './titles.js';
import { clearTitles, mergeTitles } from './titles-cache.js';
import {
  TOKEN_TTL_SECONDS, bookmarkletSnippet, discoverCloudId, fetchTitles,
  testBasicGateway, testGateway,
} from './titles-probe.js';

/** A tap-and-hold opens the popover on touch; a plain tap still navigates. */
export const TITLE_LONGPRESS_MS = 350;

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

/** The probed error, in words an operator can act on (no stack traces). */
export function probeMessage(label, result = {}) {
  if (!result) return `${label}: no answer`;
  if (result.error === 'cors-or-network') {
    return `${label}: CORS blocked — the browser refused the response. Use the bookmarklet or an import.`;
  }
  if (result.error === 'blocked-target') {
    return `${label}: refused — the title client only calls the Jira site and api.atlassian.com.`;
  }
  if (result.error === 'unauthorized') {
    return `${label}: HTTP ${result.status || 401} — the token is wrong or expired.`;
  }
  if (result.error === 'rate-limited') {
    return `${label}: HTTP 429 — Jira is rate-limiting this token; wait a minute.`;
  }
  if (result.error === 'no-site') return `${label}: set the Jira site first (for example acme.atlassian.net).`;
  if (result.error === 'no-cloud-id') return `${label}: no cloud id — press Discover (or paste one).`;
  if (!result.ok) {
    return `${label}: HTTP ${result.status || 0}${result.error ? ` (${result.error})` : ''}.`;
  }
  return `${label}: ${result.sample || 'answered'}`;
}

/** "43 min" style countdown, or '' when there is no token. */
export function expiryText(expiresAt, now = Date.now()) {
  const at = Number(expiresAt);
  if (!Number.isFinite(at) || at <= 0) return '';
  const left = Math.max(0, Math.round((at - Number(now)) / 1000));
  if (left <= 0) return 'the token has expired — paste a new one; nothing refreshes in the browser';
  const minutes = Math.floor(left / 60);
  const seconds = left % 60;
  const when = minutes >= 1 ? `${minutes} min` : `${seconds} s`;
  return `token expires in ${when} · nothing refreshes in the browser (no client secret here)`;
}

/**
 * The panel body. Every input is a preference or a transport setting; the token
 * is typed here but lives in memory only (the markup never carries a value).
 */
export function titlesMarkup({
  enabled = true,
  tenant = '',
  site = '',
  cloudId = '',
  coverageText = '0 of 0 keys have a title',
  keyCount = 0,
  oldestText = '',
  newestText = '',
  stale = false,
  bookmarkletHref = '',
  bookmarkletNote = '',
  statusText = '',
  expiryLabel = '',
  probeText = '',
} = {}) {
  const href = bookmarkletHref || '';
  const coverageLine = String(coverageText || '').trim() || '0 of 0 keys have a title';
  const freshness = [
    oldestText ? `oldest ${oldestText}` : '',
    newestText ? `newest ${newestText}` : '',
  ].filter(Boolean).join(' · ');
  return `
  <div class="titles-panel">
    <section class="titles-block" aria-labelledby="titles-status-h">
      <h2 id="titles-status-h">Where the titles are</h2>
      <p class="titles-coverage" id="titles-coverage">${esc(coverageLine)}</p>
      <p class="muted mono" id="titles-freshness">${esc(freshness || `no title cached for ${tenant || 'this dataset'}`)}</p>
      ${stale ? '<p class="titles-warn" role="status">some titles are older than '
        + `${TITLE_TTL_DAYS} days — they still render, and the popover states their age.</p>` : ''}
      <p class="muted" id="titles-status-line" role="status">${esc(statusText)}</p>
      <p class="titles-note">Titles live in this browser only. Nothing on this page requests, stores,
        or serves a title: Jira → this browser → the screen. The megaspeed server never sees one.</p>
    </section>

    <section class="titles-block" aria-labelledby="titles-import-h">
      <h2 id="titles-import-h">1 · Import an export</h2>
      <p class="muted">Transport A — zero auth, zero CORS, and the permanent fallback.
        Jira → <em>Export → CSV (all fields)</em>, or a JQL JSON dump.</p>
      <div class="titles-row">
        <label class="control">file
          <input type="file" id="titles-file" accept=".csv,.json,text/csv,application/json">
        </label>
      </div>
      <label class="sr-only" for="titles-paste">paste a CSV or JSON export</label>
      <textarea id="titles-paste" class="titles-paste" rows="4" spellcheck="false"
        placeholder="Issue key,Summary,Issue type&#10;DEMO-347,&quot;empty queue, retried&quot;,Bug"></textarea>
      <div class="titles-row">
        <button type="button" class="chip" id="titles-import">Import pasted text</button>
        <span class="hint">a malformed line is counted and skipped — never a partial write</span>
      </div>
      <p class="titles-import-line mono" id="titles-import-line" role="status"></p>
    </section>

    <section class="titles-block" aria-labelledby="titles-bookmarklet-h">
      <h2 id="titles-bookmarklet-h">2 · The bookmarklet (recommended)</h2>
      <p class="muted">Transport B — it runs on your Jira tab with the session you already have,
        so there is no token to mint and no CORS to negotiate. It requests
        ${esc(TITLE_FIELDS.join(' and '))} only.</p>
      <div class="titles-row">
        <a class="chip titles-bookmarklet" id="titles-bookmarklet"
           href="${esc(href)}" draggable="true">drag me to your bookmarks bar</a>
        <button type="button" class="chip" id="titles-copy-snippet">Copy snippet</button>
      </div>
      <p class="muted" id="titles-bookmarklet-note">${esc(bookmarkletNote
        || `Open Jira, click the bookmark, and this panel opens with the run's titles.
            No snippet yet — set the Jira site above the token box and the store's keys
            are baked into it.`)}</p>
    </section>

    <section class="titles-block" aria-labelledby="titles-token-h">
      <h2 id="titles-token-h">3 · Paste an access token</h2>
      <p class="muted">Transport C — the only one that works with Jira closed.
        <code>api.atlassian.com</code> is CORS-whitelisted for OAuth access tokens;
        the token lasts about an hour and <strong>nothing refreshes it in the browser</strong>
        (a refresh needs the client secret, which must not be here).</p>
      <div class="titles-row">
        <label class="control">Jira site
          <input type="text" id="titles-site" value="${esc(site)}"
                 placeholder="acme.atlassian.net" spellcheck="false" autocomplete="off">
        </label>
        <label class="control">cloud id
          <input type="text" id="titles-cloud" value="${esc(cloudId)}"
                 placeholder="00000000-0000-0000-0000-000000000000" spellcheck="false" autocomplete="off">
        </label>
        <button type="button" class="chip" id="titles-discover">Discover</button>
      </div>
      <div class="titles-row">
        <label class="control">access token
          <input type="password" id="titles-token" value="" autocomplete="off"
                 spellcheck="false" placeholder="never stored — memory only">
        </label>
        <button type="button" class="chip" id="titles-test">Test gateway</button>
        <button type="button" class="chip" id="titles-test-basic">Test Basic</button>
        <button type="button" class="chip" id="titles-fetch">Fetch titles</button>
      </div>
      <p class="titles-probe mono" id="titles-probe" role="status">${esc(probeText)}</p>
      <p class="muted" id="titles-expiry">${esc(expiryLabel)}</p>
    </section>

    <section class="titles-block titles-danger" aria-labelledby="titles-danger-h">
      <h2 id="titles-danger-h">4 · Keep or forget</h2>
      <div class="titles-row">
        <button type="button" class="chip" id="titles-toggle" aria-pressed="${enabled ? 'true' : 'false'}">
          Titles ${enabled ? 'on' : 'off'}
        </button>
        <button type="button" class="chip danger" id="titles-forget">Forget titles</button>
      </div>
      <p class="muted">Forget drops this dataset's cached titles from this browser.
        ${keyCount ? `${keyCount} keys in this dataset.` : ''}</p>
    </section>
  </div>`;
}

/* ------------------------------------------------------------------- wiring */

function safeStore(deps) {
  const value = deps && typeof deps.store === 'function' ? deps.store() : deps && deps.store;
  return Promise.resolve(value || null);
}

function setText(root, id, text) {
  const el = root.querySelector(`#${id}`);
  if (el) el.textContent = text;
}

/**
 * Wire the panel once. All handlers delegate from `root`, so a re-render may
 * replace its innerHTML freely. Returns `{refresh, destroy}`.
 *
 * `deps`:
 *   store      — a titles-cache store (or a function returning one / a promise)
 *   fetch      — the fetch implementation for the probes (tests inject a fake)
 *   state()    — `{tenant, enabled, site, cloudId, token, keys, expiresAt}`
 *   onToggle(enabled)        — the topbar toggle is the same switch
 *   onMerged(result, rows)   — after an import/probe, so the app re-hydrates
 *   onConfig({site, cloudId})— persist the non-secret transport settings
 *   onForget(removed)        — after Forget titles
 *   onToken({token, expiresAt}) — memory only; never persisted
 *   now()      — injectable clock for the expiry countdown (tests)
 */
export function wireTitlesPanel(root, deps = {}) {
  if (!root || typeof root.querySelector !== 'function') {
    return { refresh() {}, destroy() {} };
  }
  const doc = root.ownerDocument || (typeof document === 'undefined' ? null : document);
  const fetchImpl = deps.fetch || null;
  const now = typeof deps.now === 'function' ? deps.now : () => Date.now();
  let ticker = 0;

  const snapshot = () => (typeof deps.state === 'function' ? deps.state() : (deps.state || {}));

  function readSite() {
    const input = root.querySelector('#titles-site');
    return input ? String(input.value || '').trim() : snapshot().site || '';
  }

  function readCloud() {
    const input = root.querySelector('#titles-cloud');
    return input ? String(input.value || '').trim() : snapshot().cloudId || '';
  }

  function readToken() {
    const input = root.querySelector('#titles-token');
    return input ? String(input.value || '').trim() : snapshot().token || '';
  }

  async function runImport(text) {
    const raw = String(text == null ? '' : text);
    const state = snapshot();
    if (!raw.trim()) {
      setText(root, 'titles-import-line', importLine({}));
      return { imported: 0, unchanged: 0, skipped: 0 };
    }
    const parsed = parseTitles(raw);
    const result = await mergeTitles(state.tenant, parsed.rows, await safeStore(deps));
    const line = importLine({
      imported: result.imported,
      skipped: (parsed.skipped || 0) + (result.skipped || 0),
      unchanged: result.unchanged,
    });
    setText(root, 'titles-import-line', line);
    if (typeof deps.onMerged === 'function') deps.onMerged(result, parsed.rows);
    return result;
  }

  function paintExpiry() {
    const state = snapshot();
    const expiresAt = Number(state.expiresAt) || 0;
    setText(root, 'titles-expiry', expiryText(expiresAt, now()));
    if (!expiresAt) return;
    if (now() >= expiresAt) {
      // Said once, then no retry: a refresh needs a client secret that is not here.
      if (ticker) { clearInterval(ticker); ticker = 0; }
    }
  }

  function startCountdown(expiresAt) {
    paintExpiry();
    if (typeof setInterval !== 'function') return;
    if (ticker) clearInterval(ticker);
    ticker = setInterval(paintExpiry, 15_000);
    if (ticker && typeof ticker.unref === 'function') ticker.unref();
  }

  async function onProbe(kind) {
    const state = snapshot();
    const cloudId = readCloud();
    const token = readToken();
    const site = readSite();
    if (kind === 'discover') {
      const found = await discoverCloudId(site, fetchImpl);
      const input = root.querySelector('#titles-cloud');
      if (found.ok && input) input.value = found.cloudId;
      setText(root, 'titles-probe', found.ok
        ? `Discover: cloud id ${found.cloudId}`
        : probeMessage('Discover', found));
      if (found.ok && typeof deps.onConfig === 'function') {
        deps.onConfig({ site, cloudId: found.cloudId });
      }
      return;
    }
    if (kind === 'test') {
      const result = await testGateway({ cloudId, token }, fetchImpl);
      setText(root, 'titles-probe', probeMessage('Test gateway', result));
      return;
    }
    if (kind === 'basic') {
      const result = await testBasicGateway({ cloudId, token }, fetchImpl);
      setText(root, 'titles-probe', probeMessage('Test Basic', result));
      return;
    }
    if (kind === 'fetch') {
      const result = await fetchTitles({ cloudId, token, keys: state.keys || [] }, fetchImpl);
      setText(root, 'titles-probe', result.ok
        ? `Fetch titles: ${result.rows.length} rows in ${result.batches} batch(es), `
          + `${result.skipped || 0} skipped`
        : probeMessage('Fetch titles', result));
      if (result.rows && result.rows.length) {
        const merged = await mergeTitles(state.tenant, result.rows, await safeStore(deps));
        setText(root, 'titles-import-line', importLine({
          imported: merged.imported, skipped: merged.skipped, unchanged: merged.unchanged,
        }));
        if (typeof deps.onMerged === 'function') deps.onMerged(merged, result.rows);
      }
      if (result.ok && typeof deps.onToken === 'function') {
        const expiresAt = now() + TOKEN_TTL_SECONDS * 1000;
        deps.onToken({ token, expiresAt });
        startCountdown(expiresAt);
      }
      return;
    }
    if (kind === 'toggle') {
      if (typeof deps.onToggle === 'function') deps.onToggle(!snapshot().enabled);
      return;
    }
    if (kind === 'forget') {
      const removed = await clearTitles(snapshot().tenant, await safeStore(deps));
      setText(root, 'titles-import-line', `forgot ${removed} cached title${removed === 1 ? '' : 's'}`);
      if (typeof deps.onForget === 'function') deps.onForget(removed);
    }
  }

  async function onFile(input) {
    const file = input && input.files && input.files[0];
    if (!file) return;
    let text = '';
    try {
      text = typeof file.text === 'function'
        ? await file.text()
        : await new Promise((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result || ''));
          reader.readAsText(file);
        });
    } catch { text = ''; }
    await runImport(text);
  }

  async function onClick(event) {
    const target = event.target && event.target.closest ? event.target : null;
    if (!target) return;
    const button = target.closest('[id^="titles-"]');
    if (!button) return;
    const id = button.id;
    if (id === 'titles-import') {
      const box = root.querySelector('#titles-paste');
      await runImport(box ? box.value : '');
      return;
    }
    if (id === 'titles-copy-snippet') {
      const state = snapshot();
      const snippet = bookmarkletSnippet({
        site: readSite(),
        targetOrigin: state.targetOrigin || '',
        keys: state.keys || [],
      });
      try {
        if (typeof navigator !== 'undefined' && navigator.clipboard) {
          await navigator.clipboard.writeText(snippet);
        }
      } catch { /* clipboard denied: the snippet is still in the bookmarklet href */ }
      setText(root, 'titles-probe', 'snippet copied — paste it into the Jira tab console if dragging the bookmark is blocked');
      return;
    }
    if (id === 'titles-discover') return onProbe('discover');
    if (id === 'titles-test') return onProbe('test');
    if (id === 'titles-test-basic') return onProbe('basic');
    if (id === 'titles-fetch') return onProbe('fetch');
    if (id === 'titles-toggle') return onProbe('toggle');
    if (id === 'titles-forget') return onProbe('forget');
  }

  function onChange(event) {
    const target = event.target;
    if (!target || !target.id) return;
    if (target.id === 'titles-file') { onFile(target); return; }
    if (target.id === 'titles-site' || target.id === 'titles-cloud') {
      if (typeof deps.onConfig === 'function') {
        deps.onConfig({ site: readSite(), cloudId: readCloud() });
      }
    }
  }

  root.addEventListener('click', onClick);
  root.addEventListener('change', onChange);

  return {
    refresh() {
      paintExpiry();
    },
    importText: runImport,
    destroy() {
      if (ticker && typeof clearInterval === 'function') clearInterval(ticker);
      ticker = 0;
      root.removeEventListener('click', onClick);
      root.removeEventListener('change', onChange);
    },
  };
}

/* ------------------------------------------------------- message handshake */

/**
 * The bookmarklet's `postMessage` receiver (prd/80 §The Titles panel).
 *
 * Accepted only when the message came from the configured Jira origin *and* has
 * the agreed shape (`{type:'megaspeed.titles', v:1, rows:[…]}`, every row a
 * `{key, summary, type}` with a real ticket key). Anything else — another
 * origin, another type, a malformed row — is ignored, not logged: a
 * screen-shared tab must not fill its console.
 *
 * Returns an unlisten function.
 */
export function listenTitlesMessage(win, { site = '', onRows } = {}) {
  if (!win || typeof win.addEventListener !== 'function') return () => {};
  const configured = () => {
    const raw = typeof site === 'function' ? site() : site;
    const text = String(raw == null ? '' : raw).trim();
    if (!text) return '';
    try {
      return new URL(/^https?:\/\//i.test(text) ? text : `https://${text}`).origin;
    } catch { return ''; }
  };
  const handler = (event) => {
    // Compare against the Jira origin, never against our own: the message comes
    // from the Jira tab, and only from there.
    const accepted = readTitlesMessage(event, configured());
    if (!accepted) return;
    if (typeof onRows === 'function') onRows(accepted.rows, accepted.skipped);
  };
  win.addEventListener('message', handler);
  return () => win.removeEventListener('message', handler);
}

/* ---------------------------------------------------------------- hydration */

function clearAnnotations(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return 0;
  let removed = 0;
  for (const node of root.querySelectorAll('.ticket-title, .ticket-title-sub')) {
    if (node && node.parentElement && typeof node.parentElement.removeChild === 'function') {
      node.parentElement.removeChild(node);
      removed += 1;
    }
  }
  for (const el of root.querySelectorAll('[data-title-resolved]')) {
    // Restore the anchor's own tooltip so "off" is the unannotated page.
    if (el.dataset) {
      if (el.dataset.titleNative != null) {
        if (el.dataset.titleNative) el.setAttribute('title', el.dataset.titleNative);
        else el.removeAttribute('title');
      }
      delete el.dataset.titleNative;
      delete el.dataset.titleResolved;
    }
    if (el.removeAttribute) el.removeAttribute('data-title-resolved');
  }
  return removed;
}

/** One inline line: icon (our constant) + summary as a text node. */
function titleLineOf(doc, key, entry) {
  const line = doc.createElement('span');
  line.className = 'ticket-title';
  line.setAttribute('title', `${key} — ${entry.summary}`);
  const icon = doc.createElement('span');
  icon.className = 'ticket-title-icon';
  // Our own constant SVG — never a title. The summary below is a text node.
  icon.innerHTML = typeGlyph(entry.type);
  icon.setAttribute('aria-hidden', 'true');
  line.appendChild(icon);
  line.appendChild(doc.createTextNode(entry.summary));
  return line;
}

/**
 * Annotate the existing `[data-ticket]` anchors. One call per view render
 * (prd/80 §Render), never a `MutationObserver`.
 *
 * With `enabled: false` this removes every annotation and returns before
 * reading a single title — off means off (rule 8). With no cache rows it does
 * nothing at all, so an unconfigured page is unchanged.
 *
 * Returns the number of anchors annotated.
 */
export function hydrateTitles(root, { enabled = true, titles = null } = {}) {
  if (!root || typeof root.querySelectorAll !== 'function') return 0;
  if (!enabled) {
    clearAnnotations(root);
    return 0;
  }
  if (!titles || typeof titles.get !== 'function' || !titles.size) {
    clearAnnotations(root);
    return 0;
  }
  const doc = root.ownerDocument
    || (root.createElement ? root : null)
    || (typeof document === 'undefined' ? null : document);
  if (!doc || typeof doc.createElement !== 'function') return 0;
  let annotated = 0;
  for (const el of root.querySelectorAll('[data-ticket]')) {
    const key = String((el.dataset && el.dataset.ticket) || '').trim().toUpperCase();
    const entry = key ? titles.get(key) : null;
    if (!entry || !entry.summary) continue;
    if (el.dataset) {
      if (el.dataset.titleNative == null) {
        el.dataset.titleNative = el.getAttribute('title') || '';
      }
      el.dataset.titleResolved = '1';
    }
    // Spell the marker as an attribute too, so `clearAnnotations()` can find it
    // in any DOM (dataset reflection is a browser convenience, not a contract).
    if (el.setAttribute) el.setAttribute('data-title-resolved', '1');
    // The native tooltip is the fallback where a popover cannot anchor
    // (a canvas, a crowded cell): `DEMO-347 — parser crash on an empty queue`.
    el.setAttribute('title', `${key} — ${entry.summary}`);
    el.setAttribute('aria-expanded', 'false');
    annotated += 1;
    // The muted second line is opt-in per surface (the epic page's ticket
    // list). Inline table cells keep the key only, so column widths and row
    // heights do not move.
    if (el.dataset && el.dataset.ticketLine && el.parentElement
      && typeof el.parentElement.insertBefore === 'function') {
      const line = titleLineOf(doc, key, entry);
      el.parentElement.insertBefore(line, el.nextSibling || null);
    }
  }
  return annotated;
}

/**
 * The ticket page's subtitle (prd/80 §Render): the key stays the `h1`, the
 * summary is a muted line under it. Returns the subtitle element, or null when
 * there is nothing to show — in which case the markup is exactly today's.
 */
export function annotateDetailTitle(h1, { key = '', title = null, enabled = true } = {}) {
  if (!h1 || !h1.parentElement) return null;
  const parent = h1.parentElement;
  const existing = typeof parent.querySelector === 'function'
    ? parent.querySelector('.ticket-title-sub')
    : null;
  const wanted = String(key || '').trim().toUpperCase();
  const shown = String(h1.textContent || '').trim().toUpperCase();
  if (!enabled || !title || !title.summary || !wanted || shown !== wanted) {
    if (existing && parent.removeChild) parent.removeChild(existing);
    return null;
  }
  const doc = h1.ownerDocument || (typeof document === 'undefined' ? null : document);
  if (!doc || typeof doc.createElement !== 'function') return null;
  const node = existing || doc.createElement('p');
  node.className = 'ticket-title-sub';
  node.setAttribute('title', `${wanted} — ${title.summary}`);
  node.setAttribute('data-title-kind', 'detail');
  node.textContent = '';
  const icon = doc.createElement('span');
  icon.className = 'ticket-title-sub-icon';
  icon.innerHTML = typeGlyph(title.type);
  icon.setAttribute('aria-hidden', 'true');
  node.appendChild(icon);
  node.appendChild(doc.createTextNode(title.summary));
  if (!existing && typeof parent.insertBefore === 'function') {
    parent.insertBefore(node, h1.nextSibling || null);
  }
  return node;
}

/* ------------------------------------------------------------------ popover */

/**
 * ONE delegated popover for the whole document (prd/80 §Render). The key stays
 * the only visible text, so column widths, row heights and density are
 * unchanged; hover or focus opens a 2-line summary with the type icon and an
 * honest footer. Esc closes, touch uses a 350 ms long-press so a tap still
 * navigates; a title is written with `textContent` only.
 *
 * `titleFor(key)` returns a cached `{summary, type, fetched_at}` or null;
 * `isEnabled()` is the toggle, read at open time.
 */
export function wireTitlePopover(doc, { titleFor, isEnabled = () => true } = {}) {
  if (!doc || typeof doc.addEventListener !== 'function') return { close() {} };
  let pop = null;
  let anchor = null;
  let timer = 0;
  let pressed = null;
  let swallow = false;

  function ensurePop() {
    if (pop || !doc.body) return pop;
    pop = doc.createElement('div');
    pop.className = 'title-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', 'ticket title from your Jira session');
    pop.setAttribute('aria-expanded', 'false');
    pop.hidden = true;
    doc.body.appendChild(pop);
    return pop;
  }

  function close() {
    if (anchor && anchor.setAttribute) anchor.setAttribute('aria-expanded', 'false');
    anchor = null;
    if (pop) {
      pop.hidden = true;
      pop.setAttribute('aria-expanded', 'false');
      // Rule 8: with titles off (or closed) no summary text stays in the DOM.
      pop.textContent = '';
    }
  }

  function place(box, el) {
    if (!el || typeof el.getBoundingClientRect !== 'function' || !box.style) return;
    const rect = el.getBoundingClientRect();
    const view = doc.documentElement || {};
    const width = Math.min(340, Math.max(220, Math.round(rect.width || 220)));
    const maxLeft = (Number(view.clientWidth) || 1200) - width - 8;
    const left = Math.max(8, Math.min(Math.round(rect.left), maxLeft));
    box.style.position = 'fixed';
    box.style.left = `${left}px`;
    box.style.top = `${Math.round(rect.bottom + 6)}px`;
    box.style.width = `${width}px`;
  }

  function show(target) {
    if (typeof isEnabled === 'function' && !isEnabled()) return false;
    const el = target && typeof target.closest === 'function'
      ? target.closest('[data-ticket]')
      : null;
    const key = el && el.dataset ? String(el.dataset.ticket || '').trim().toUpperCase() : '';
    const entry = key && typeof titleFor === 'function' ? titleFor(key) : null;
    if (!el || !entry || !entry.summary) { close(); return false; }
    const box = ensurePop();
    if (!box) return false;
    box.textContent = '';
    const head = doc.createElement('p');
    head.className = 'title-pop-head';
    const type = doc.createElement('span');
    type.className = 'title-pop-type';
    // our own constant SVG; the type *name* below is text
    type.innerHTML = typeGlyph(entry.type);
    type.setAttribute('aria-hidden', 'true');
    const label = doc.createElement('span');
    label.className = 'title-pop-label';
    label.textContent = typeLabel(entry.type);
    const keyEl = doc.createElement('span');
    keyEl.className = 'title-pop-key mono';
    keyEl.textContent = key;
    head.appendChild(type);
    head.appendChild(label);
    head.appendChild(keyEl);
    const summary = doc.createElement('p');
    summary.className = 'title-pop-summary';
    summary.textContent = entry.summary;
    const foot = doc.createElement('p');
    foot.className = 'title-pop-foot';
    const days = ageDays(entry.fetched_at);
    foot.textContent = days != null && days > TITLE_TTL_DAYS
      ? `from your Jira session · not stored · imported from an export, ${formatAge(days)}`
      : 'from your Jira session · not stored';
    box.appendChild(head);
    box.appendChild(summary);
    box.appendChild(foot);
    box.hidden = false;
    box.setAttribute('aria-expanded', 'true');
    if (anchor && anchor !== el) anchor.setAttribute('aria-expanded', 'false');
    anchor = el;
    el.setAttribute('aria-expanded', 'true');
    place(box, el);
    return true;
  }

  function onOver(event) {
    if (!event || !event.target) return;
    show(event.target);
  }

  function onOut(event) {
    if (!event || !event.target || typeof event.target.closest !== 'function') return;
    if (event.relatedTarget && anchor && anchor.contains && anchor.contains(event.relatedTarget)) return;
    if (event.target.closest('[data-ticket]')) close();
  }

  function onKey(event) {
    if (event && event.key === 'Escape') close();
  }

  function onDown(event) {
    if (!event || event.pointerType === 'mouse' || typeof setTimeout !== 'function') return;
    const target = event.target;
    const el = target && typeof target.closest === 'function' ? target.closest('[data-ticket]') : null;
    if (!el) return;
    pressed = el;
    swallow = false;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = 0;
      if (pressed && show(pressed)) swallow = true;
    }, TITLE_LONGPRESS_MS);
  }

  function cancelPress() {
    if (timer && typeof clearTimeout === 'function') clearTimeout(timer);
    timer = 0;
    pressed = null;
  }

  function onClick(event) {
    if (!swallow) return;
    swallow = false;
    if (event && typeof event.preventDefault === 'function') {
      event.preventDefault();
      event.stopPropagation();
    }
  }

  doc.addEventListener('mouseover', onOver);
  doc.addEventListener('mouseout', onOut);
  doc.addEventListener('focusin', onOver);
  doc.addEventListener('focusout', onOut);
  doc.addEventListener('keydown', onKey);
  doc.addEventListener('pointerdown', onDown, { passive: true });
  doc.addEventListener('pointerup', cancelPress, { passive: true });
  doc.addEventListener('pointercancel', cancelPress, { passive: true });
  doc.addEventListener('click', onClick, true);

  return {
    close,
    destroy() {
      cancelPress();
      doc.removeEventListener('mouseover', onOver);
      doc.removeEventListener('mouseout', onOut);
      doc.removeEventListener('focusin', onOver);
      doc.removeEventListener('focusout', onOut);
      doc.removeEventListener('keydown', onKey);
      doc.removeEventListener('pointerdown', onDown);
      doc.removeEventListener('pointerup', cancelPress);
      doc.removeEventListener('pointercancel', cancelPress);
      doc.removeEventListener('click', onClick, true);
      if (pop && pop.parentElement) pop.parentElement.removeChild(pop);
      pop = null;
    },
  };
}

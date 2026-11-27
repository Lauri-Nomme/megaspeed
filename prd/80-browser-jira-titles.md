# 80 — Ticket titles, resolved in the browser

Status: **draft; Jira content never transits megaspeed (hard rule).**

## Problem

The store is key-only on purpose (prd/02: keys allowed, ticket **content**
forbidden), and the UI follows it (prd/08: "tooltips and labels render only
keys, pseudonyms, and closed attributes"). The consequence is now the biggest
usability gap left: at the ticket and epic level the product says *"DEMO-347
took 21 days and 6 of them were waiting for a reviewer"* and the reader cannot
tell which work that was. `DEMO-347` does not tell you whether to chase a
payment bug, a parser crash, or a doc tweak. A dozen words of human-written
text per key is the difference between a dashboard and an action.

## The invariant

A title is ticket content. So:

> **The title never comes from megaspeed.** It travels Jira → the operator's
> browser → the screen. It is never stored in the store, served by the API,
> proxied by the server, written into an export, a metric, or an LLM view.
> The content does not transit the megaspeed server at all.

`--verify` and the canary are unchanged; a store bundle copied to another
machine still has zero titles.

## Why the obvious implementation cannot work

A "connect with a token" panel that calls Jira from the megaspeed page is
blocked by the browser, not by our code:

- **Cloud's site REST sends no CORS headers.** Atlassian's own answer: *"the
  problem why we don't support CORS directly on your site host/domain is that
  we accept session based authentication … the only way that you can bypass
  CORS would be by hosting a proxy."*
- **OAuth 2.0 3LO needs a client secret** at the token exchange, and PKCE is not
  honoured (`OAUTH20-2491`), so a public browser client cannot complete 3LO.
- **Web Workers do not help.** CORS is enforced per **origin**, not per thread;
  a worker inherits the page's origin and the same policy.
- **Service Workers do not help either.** They can intercept their own origin's
  requests, but a cross-origin response is readable only under CORS;
  `mode: 'no-cors'` yields an **opaque** response (status `0`, body unreadable).
- Same for `XMLHttpRequest`, JSONP (Jira has no callback), and
  `credentials: 'include'` (Cloud grants neither `Allow-Credentials` nor CORS).

The complete set of CORS bypasses is: an extension with host permissions, a
proxy, or **execution inside Jira's own origin**. Excluding the first two, the
bookmarklet is not a workaround — it is the mechanism.

But one CORS-enabled door does exist, and it is worth having:

> `api.atlassian.com/ex/jira/{cloudId}` is CORS-whitelisted for browser
> `fetch`/XHR with **OAuth 2.0 authorization-code access tokens**.

So a **pasted 3LO access token** works from the page. Its `expires_in` is
**3600 s** and refreshing it needs the `client_secret`, which must not live in
the browser — hence "one hour of freshness per paste", a manual refresh, not a
connection. It is the fallback for an operator who has a token but no Jira tab.

## Transports

| # | Transport | Credential | Transit | Freshness | Verdict |
|---|---|---|---|---|---|
| **A** | **CSV/JSON import** — *Export → CSV (all fields)* or a JQL JSON dump, dropped on the panel | none | none | manual | **Ship first.** Zero auth, zero CORS, permanent fallback. |
| **B** | **Bookmarklet** — runs *on the Jira tab* (same origin, so no CORS), `POST /rest/api/3/search/jql` with the live session, then `window.open` + `postMessage` into the megaspeed tab | the browser's existing Jira session | Jira → browser → megaspeed | one click | **Recommended.** No token to mint, no paste, no install. |
| **C** | **Pasted 3LO access token** against the CORS gateway | access token | none | ~1 h | Fallback: works with no Jira tab; refresh needs an out-of-browser helper. |
| — | extension | — | — | — | **Out.** |
| — | server proxy | — | **excluded** | — | Content must not transit megaspeed. |
| — | server-side fetch, titles at rest | — | excluded | — | A shadow Jira; what prd/02 exists to prevent. |
| — | workers / service workers | — | — | — | Do not bypass CORS (see above). |

All three feed the **same import shape** — `[{key, summary, type}]` — so the
renderer, cache, toggle and tests are transport-independent. Only the
"how do titles get here" panel differs.

### B, in detail (the one-click path)

```
Jira tab (signed in)
  └ click bookmarklet ──► fetch(rest/api/3/search/jql, credentials:'same-origin')
                          fields=summary,issuetype only
        ├─ window.open('…/#titles') or focus an existing megaspeed tab
        └ postMessage({type:'megaspeed.titles', payload})   ← cross-origin allowed
megaspeed tab ──► merge into IndexedDB ──► hydrate
```

- No token, no secret, no clipboard, no install. The credential is the session
  the operator already has.
- Only the Jira *origin* is contacted, and only `summary`/`issuetype` are
  requested (§fields).
- **Risk:** a deployment whose CSP forbids `javascript:` URLs blocks a bookmark
  click. One-line test before we depend on it; the same snippet pasted into the
  console always works, and transport A always works.

### C, in detail (the pasted token)

- The operator mints an access token **outside megaspeed** — a local helper that
  holds the `client_secret` (the `client_secret` never enters the browser, and
  a refresh token is never stored in `localStorage`: that would be a long-lived,
  XSS-extractable org credential, and this tool's premise is not holding
  secrets).
- Paste once → the gateway answers → titles merge → the panel shows
  "expires in 43 min".
- Cloud ID discovery: `https://<site>.atlassian.net/_edge/tenant_info` (CORS
  open), so the operator pastes only the token.
- **Probe first (30 s), because it decides whether C is real:** see §Probes.

## Fields — exactly two

| field | verdict |
|---|---|
| `summary` | the point of the feature |
| `issuetype` | cheap, stable, and makes the row recognisably Jira |
| `status` | **no** — the store already has workflow state from real transitions; a live copy would disagree with the chart beside it |
| `priority` | **no** — unused in the UI |
| `assignee` | **forbidden.** A real name next to a store pseudonym is a de-anonymisation channel: it builds the `LaNo → person` map the pseudonymisation exists to prevent. No user field is ever requested. |

Literal allowlist, tested: `TITLE_FIELDS = ['summary', 'issuetype']`.
`description`, `comment`, `attachment`, `assignee`, `reporter` are never
requested — receiving a description by accident would put the one category of
data prd/02 forbids into browser storage.

## Cache

- **IndexedDB** object store `titles`, key `` `${tenant}|${ticketKey}` ``.
  Not `localStorage`: synchronous, 5 MB-capped, and it would sit beside
  unrelated UI state.
- **Value:** `{summary, type, fetched_at}` — nothing else, ever.
- **Merge, don't replace:** an import overwrites the keys it carries and leaves
  the rest, so a partial export cannot blank the cache.
- **Staleness:** render immediately; if `fetched_at` is older than 30 days the
  popover footer says "from an export · 41 days old". A stale title is useful; a
  silently stale one is a trap.
- **Per tenant.** **Forget titles** drops the store.

## Render

`hydrateTitles(root)` runs once at the end of each view render — an explicit
call, not a global `MutationObserver`, so it is deterministic and testable:
`root.querySelectorAll('[data-ticket]')` → cache lookup → annotate. Every key in
the app already flows through `ticketLink()` (15 call sites) and carries
`data-ticket`, so **no call site changes**.

- **Type icon:** 14 px inline SVG matched to Jira's glyph and palette — Epic
  `#6554C0` (lightning), Story `#36B37E` (bookmark), Task `#4BADE8`
  (square-check), Bug `#E5493A` (bug), Sub-task `#4BADE8` (corner arrow),
  unknown `#6B778C`. `aria-label` = the type name. SVG over emoji: 🟣/🟢/🔵 are
  indistinguishable at 14 px and do not read as Jira.
- **Default: popover on hover and focus.** The key stays the only visible text,
  so **column widths, row heights and density are unchanged** — the property
  that keeps 15 surfaces and their node tests safe. Content: summary (≤2 lines),
  type icon + name, footer "from your Jira session · not stored".
- **Ticket detail page:** key as h1, **summary as a subtitle line**. Highest
  value: that page exists to answer "where did the time go for *this* ticket".
- **Epic / ticket list:** title as a **muted second line under the key** — there
  the title *is* the content — with an acceptance check that the delivery
  table's row height and column widths do not move.
- **Native `title` attribute** where a popover cannot anchor (graph/canvas):
  `DEMO-347 — parser crash on an empty queue`.
- **Unresolved keys:** nothing. No shimmer, no placeholder churn.
- **Keyboard/touch:** focusable, Enter/Space opens, Esc closes
  (`aria-expanded`, `role="dialog"`); touch uses a 350 ms long-press so a tap
  still navigates.
- **Coverage** in the panel: "412 of 600 keys have a title".

## The toggle

A small topbar control beside the theme toggle: **Titles — on/off**.

- **Off** removes every title, icon, subtitle and popover from the DOM and stops
  a pending refresh. One click, immediate, no reload.
- Persisted per browser (a preference, not a secret) and mirrored as
  `?titles=off`, so a screen-shared link opens already hidden.
- A browser that never imported sees keys and nothing else: the out-of-the-box
  page is byte-identical to today.

## Rules that must hold (each is a test)

1. **No title transits the megaspeed server.** The resolver cannot call
   same-origin endpoints; the server has no titles route; a test asserts the
   client's title `fetch` targets only the Jira origin (gateway or site).
2. Requested fields are exactly `['summary','issuetype']` — asserted.
3. No user field is requested or stored.
4. No server endpoint returns a title; `--verify` and the canary unchanged.
5. Titles render as `textContent`, never `innerHTML`; `<script>` in a summary
   renders as text.
6. Titles never reach metrics, labels, exports (Markdown/CSV/print), the policy
   lab or any LLM view: the resolver must not import the metrics/store modules,
   and the export path must not call `hydrateTitles`.
7. No token, secret or refresh token in `localStorage`, a URL, or a cookie we
   set; the pasted token lives in memory only.
8. Off means off: with the toggle off, no summary text is in the DOM (asserted).

## The Titles panel — `?view=titles`

A real view in the app (sidebar → **Workspace → Titles**), not a console
workflow. It is simultaneously the importer, the status board and the probe
bench, so nobody copy-pastes JavaScript to find out whether a transport works.

**Status:** keys in the tenant, keys with a title, coverage ("412 of 600"),
`fetched_at` of the oldest/newest entry, and a staleness warning past 30 days.

**Import (transport A):** a file input plus a paste box. Both accept the three
shapes (§Phases); a malformed line is counted and skipped, never a partial
write. Shown as "imported 412 · skipped 3 · 1886 unchanged".

**Bookmarklet (transport B):** a draggable `javascript:` anchor ("drag me to
the bookmarks bar") — our own pages set no CSP header, so the link renders and
drags normally — plus a "copy snippet" button for the console fallback and one
sentence of instructions. The snippet fetches `summary,issuetype` for the
store's epic set from the Jira tab, then `window.open`s this panel and
`postMessage`s the payload; the panel validates `event.origin` against the
configured Jira origin and the payload shape before merging.

**Token (transport C):** cloudId field with a **Discover** button
(`/_edge/tenant_info`, CORS-open), a token field (type=password, never
persisted), **Test gateway** (probe 2, prints the HTTP status and a one-line
sample), and **Fetch titles** for the keys this tenant has. Expiry countdown;
when it lapses the panel says so once and does not retry.

**Danger zone:** **Forget titles** (drops the tenant's object store), and the
titles on/off toggle mirrored from the topbar so the whole feature is
manageable from one page.

**The topbar toggle** stays a small control beside the theme toggle (the
screen-share kill switch); the panel is where the feature is configured.

## Probes (before dispatch, ~2 minutes)

1. **Bookmarklet CSP:** drag a `javascript:alert(1)` bookmark onto the Jira
   toolbar and click it on a Jira page. Fires ⇒ B is viable.
2. **Gateway CORS (transport C):**
   ```js
   fetch(`https://api.atlassian.com/ex/jira/${cloudId}/rest/api/3/myself`,
         { headers: { Authorization: `Bearer ${token}` } }).then(r => r.json())
   ```
   A JSON body ⇒ C is viable; a CORS error ⇒ C is dead, use A/B.
3. **Basic-on-gateway (undocumented):** the same call with
   `Authorization: Basic …` and an API token. If it answers, C gains a
   long-lived credential; do not design around it until it does.

## Phases

- **P0 (≈1 day) — import + render.** Parser for the three shapes Jira actually
  emits (`Issue key,Summary,Issue type`; `key,fields.summary,fields.issuetype.name`;
  the `postMessage` payload), IndexedDB merge, `hydrateTitles`, type icons,
  popover, ticket-detail subtitle, epic-list second line, toggle, forget,
  coverage. This is the feature.
- **P1 (≈half day) — bookmarklet + handshake.** The snippet, the
  `window.open`/`postMessage` receiver (origin-checked, payload-shape-checked),
  and the panel copy. Gate on probe 1.
- **P2 (≈half day) — pasted token.** Gateway client with cloudId discovery, the
  expiry countdown, and a loud "no refresh in the browser" note. Gate on probe 2.

## Open questions

1. **Scope:** the same epic/ticket set the store covers (comparable coverage), or
   a JQL the operator pastes? Default: the store's own epic set.
2. **Is transport C worth building** if B works? (It is the only one that works
   with Jira closed; otherwise skip it.)
3. Anything beyond Jira (Confluence)? Out of scope — asking so it is not
   implied.

## Acceptance

- **Unconfigured is byte-identical to today:** no request, no DOM change, every
  existing node test green.
- **Configured:** every `[data-ticket]` resolves within one frame; the popover
  appears on hover and focus; the delivery table's row heights and column widths
  are asserted unchanged.
- **Ticket detail** shows the summary subtitle; the epic list shows the muted
  second line.
- **Toggle off** removes every title from the DOM.
- **Rules** all green, especially: nothing in the API schema, nothing in
  `--verify`, nothing in an export, no title request to the megaspeed origin.
- **Degradation:** a malformed CSV line is skipped with a count and never a
  partial write; a `postMessage` from an unexpected origin is ignored; an expired
  token says so once and keeps the keys; a stale cache renders with its age.
- Store contract, `--verify`, `tests/` and `app/test` suites unchanged.

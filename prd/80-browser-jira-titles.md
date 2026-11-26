# 80 — Ticket titles, resolved in the browser

Status: **draft; needs one answer set (§8) before dispatch.**

## Problem

The store is key-only on purpose (prd/02: keys allowed, ticket **content**
forbidden), and the UI follows it (prd/08: "tooltips and labels render only
keys, pseudonyms, and closed attributes"). The consequence is now the biggest
usability gap left: at the ticket and epic level the product says *"DEMO-347
took 21 days and 6 of them were waiting for a reviewer"* and the reader cannot
tell which work that was. Flow numbers over a list of opaque keys do not
produce an action; `DEMO-347` does not tell you whether to chase a payment bug,
a parser crash, or a doc tweak.

The only thing missing is a **dozen words of human-written text per key**. That
text exists in Jira, which every reader of this tool is already logged into.

## The invariant question, answered first

A title is ticket content. Storing it in the store, or serving it from the API,
breaks prd/02 and prd/08 and would fail `--verify`. So the design is not
"store titles somewhere cheap" — it is:

> **The title never comes from megaspeed. It travels Jira → the operator's
> browser → the screen, under the operator's own credentials, and is never
> written to the store, the API, an export, a metric, or an LLM view.**

Under that constraint the resolver is not a relaxation of prd/02; it is the
only shape that satisfies it *and* gives the reader a title. A store bundle
copied to another machine still has zero titles. `--verify` still passes. The
canary still passes. A second operator on a fresh profile sees keys until they
connect their own session.

What it **does** change, and must be written down rather than glossed:

- The rendered page is no longer "keys only" **for an authenticated viewer**, so
  a screenshot of a connected megaspeed shows real summaries. Mitigation: the
  resolver is off until the operator connects it, a one-click "Forget titles"
  exists, and exports never include titles (§5).
- "Titles are not sensitive" is an assumption we should not make silently — some
  epics are embargoed. §8 Q3 asks.

## What already exists (why this is cheap)

- **One choke point.** Every rendered key goes through `ticketLink()` in
  `app/tickets.js` (15 call sites) and carries `data-ticket="DEMO-347"`. A
  post-render hydration pass needs **no call-site change**.
- **A popover vocabulary.** `--popover-bg`, `--radius-popover`,
  `--shadow-popover` already exist in `app/style.css`; tooltips already float
  over the graph and benchmark cards.
- **The keys are already client-side.** `/api/objects` and the ticket detail
  payload carry `ticket_keys`; nothing new needs to be exposed.
- **The Jira client shape is already modelled.** `ingest/jira.py` (DC,
  `/rest/api/2`, Bearer PAT or Basic) and `ingest/jira_cloud.py` (Cloud,
  `/rest/api/3`, Bearer PAT or Basic) define exactly the two transports a
  browser resolver must speak. The server-side code is *not* reused at runtime
  (see P2), but its field discovery and auth shapes are the reference.

## Decision

A **client-side title resolver**, default **off**, with one visible control in
the topbar beside the theme toggle. It maps `ticket_key → summary` for the keys
already on screen and renders them as a **hover/focus popover**, never as
inline text by default.

Nothing is stored server-side. The cache is the browser's, per tenant, with a
TTL and a clear button.

## Transports (one resolver, four ways to fill it)

| # | Transport | Credential | Needs | Verdict |
|---|---|---|---|---|
| P0 | **File import** — Jira CSV/JSON drop, or a dump produced by the ingest | none | nothing | **Ship first.** Zero auth, zero CORS, permanent fallback. Proves the UX in a day. |
| P1 | **Direct fetch** (browser → Jira REST) | PAT (DC) / API token (Cloud) | CORS: DC needs an admin **allowlist entry** for the megaspeed origin; Cloud needs `api.atlassian.com/ex/jira/{cloudId}` (which is CORS-enabled) or a verified site-REST path | Best UX; **gate on a 5-line probe** (§7) before it is built. |
| P2 | **Same-origin proxy** `POST /api/jira/titles` | the operator's token, forwarded | token transits megaspeed | Fallback when CORS is impossible and the org allows it. Strictly no-store, no-log, allowlisted upstream. |
| P3 | **Browser extension** | PAT | host permission bypasses CORS | Last resort; not planned. |

Deliberately **not** a transport: server-side service-account fetch with titles
at rest, even in a sidecar database. That is the one option that turns a key-only
system into a shadow Jira, and it is the thing prd/02 exists to prevent.

A fifth, free option is worth stating because it may need no code at all: if
megaspeed is served from the **same registrable domain** as Jira, a
`credentials: 'include'` fetch can reuse the SSO cookie. It still needs Jira's
CORS allowlist and `Access-Control-Allow-Credentials`, and SameSite handling
makes it fragile — treat it as a bonus if the probe passes, never as the plan.

## 1. Auth (P1/P2)

- **DC:** `Authorization: Bearer <PAT>`, read-only PAT, no write scope.
- **Cloud:** Basic `base64(email + ':' + api_token)` — the method Atlassian
  documents for scripts — or OAuth 2.0 3LO if the org forbids personal tokens
  (3LO needs a client secret, so it forces P2).
- **Token lifetime:** memory by default; optional `sessionStorage` for the tab;
  **never** `localStorage`, never a cookie we set, never a URL, never a query
  string (a `Referer` would leak it).
- **Scope:** one configured Jira origin, matched exactly at request time. No
  user-supplied base URL after setup (that is an SSRF hole in P2).

## 2. Fields

An explicit allowlist, in one constant, with a test that asserts it:

```js
export const TITLE_FIELDS = ['summary'];                  // required
export const OPTIONAL_FIELDS = ['issuetype', 'status', 'priority', 'assignee'];
```

`description`, `comment`, `attachment` and everything else are **never
requested**. Asking for a title and receiving the description by accident would
put the one category of data prd/02 forbids into the browser's storage; the
allowlist is the enforcement point, and the test is cheap.

Display is `summary` only, plus the optional fields in the popover footer if
they come back. Titles are truncated to 200 chars, newlines collapsed.

## 3. Cache

- **Store:** IndexedDB (`titles` object store), key `` `${tenant}|${ticketKey}` ``.
  Not `localStorage`: it is synchronous, 5 MB-capped, and would put a title in
  the same origin as unrelated UI state.
- **Value:** `{summary, type, status, status_category, fetched_at, updated}`.
- **TTL:** 24 h default. Stale-while-revalidate: render the cached title
  immediately, refresh in the background when opened.
- **Per tenant:** switching tenant does not leak titles across datasets.
- **Budget:** resolve only keys **in the rendered view** (plus the ticket page's
  PRs), batches of ≤50 keys via one `key in (...)` search, one in-flight batch,
  exponential backoff on `429` honouring `Retry-After`, and a hard cap (300 keys)
  per view so a 5,000-row epic list cannot become 100 API calls.
- **Clear:** "Forget titles" drops the object store, and the token with it.

## 4. Render

`hydrateTitles(root)` runs once at the end of each view render (an explicit
call, not a global `MutationObserver` — deterministic and testable):

```
root.querySelectorAll('[data-ticket]') → cache lookup → set title / aria-label
```

- **Default: popover on hover and focus.** The key stays the only visible text,
  so column widths, row heights and the density of every table are unchanged —
  the cheapest possible change and the one that cannot break the layout.
  Content: summary (≤2 lines), type/status/assignee, **Open in Jira ↗**, footer
  "from your Jira session · not stored".
- **Native `title` attribute** as the fallback (it already exists on
  `ticketLink`): replace "open the ticket detail page" with
  `DEMO-347 — parser crash on an empty queue` when resolved. Slow and
  inaccessible, so it is the graph/canvas fallback, not the primary.
- **Ticket detail page is the exception:** `app/ticket.js` has room, so the
  header shows the key as the h1 and the **summary as a subtitle line**. That is
  the single highest-value placement: the page exists to answer "where did the
  time go for this ticket", and the reader needs to know which ticket.
- **Optional inline mode, off by default:** `DEMO-347 · parser crash on an…`
  clipped in the wide tables (delivery, sprints, epics) only, `text-overflow:
  ellipsis`, muted, `max-width: 28ch`, never wrapping, never on axis labels or
  badges.
- **Unresolved keys:** nothing at all. No shimmer, no placeholder, no "loading"
  churn in tables — the popover says "not resolved (no access or not fetched)"
  only when asked.
- **Keyboard/touch:** focusable, Enter/Space opens, Esc closes
  (`aria-expanded`, `role="dialog"` for the interactive popover); touch uses a
  350 ms long-press so a tap still navigates.
- **Coverage line** in the connect panel: "412 of 600 keys resolved".

## 5. Rules that must hold (each is a test)

1. Token: memory or `sessionStorage`; never `localStorage`, IndexedDB, a URL, or
   a cookie megaspeed sets.
2. Token goes to exactly one origin, matched exactly; no wildcard, no
   post-setup URL changes.
3. Requested fields are the §2 allowlist; `description`/`comment`/`attachment`
   absent — asserted.
4. No server endpoint returns a title. `--verify` and the canary tests are
   unchanged.
5. Titles are rendered as text nodes (`textContent`), never `innerHTML`; a title
   containing `<script>` renders as text.
6. Titles never reach metrics, labels, exports (Markdown/CSV/print), the policy
   lab, or any LLM view. The resolver module must not import the metrics/store
   modules (a dependency test), and the export render path must not call
   `hydrateTitles`.
7. "Forget titles" clears both stores; tenant switch re-keys.
8. Zero network requests before the operator clicks connect (a test with a
   stubbed `fetch`).

## 6. Phases

- **P0 — file import + render (≈1 day).** `TITLES_FIELDS` parser for the two
  shapes Jira actually exports (`key,summary` CSV; the REST JSON), client-side
  parse, IndexedDB write, popovers and the ticket-detail subtitle. Ships real
  value with no auth decision, and stays as the fallback for a locked-down site.
- **P1 — direct fetch (≈1–2 days, gated on §7).** Connect panel, PAT/Basic,
  batching, TTL, backoff, coverage.
- **P2 — proxy (≈1 day, only if P1's probe fails).** `POST /api/jira/titles`
  behind an env flag: request `{keys:[…]}`, token in a header, response
  `no-store`, upstream origin from config only, 5 s timeout, ≤50 keys, **no
  logging of the header**, and no disk write anywhere in the path.
- **P3 — not planned.**

## 7. The gate for P1 (5 minutes, no code)

From the megaspeed origin, in the console:

```js
fetch('https://<jira-host>/rest/api/2/myself', {
  headers: { Authorization: 'Bearer <PAT>' },
}).then(r => r.json()).then(console.log).catch(e => console.error('CORS:', e))
```

A JSON answer means P1 is viable. A CORS error means DC needs an admin allowlist
entry for the megaspeed origin (Jira DC → *Whitelist*), and if the admin will not
add one, we go straight to P2 or stay on P0. **Do not build P1 on the assumption
that it works.**

## 8. Open questions (these decide the phase order)

1. **Jira deployment:** Cloud or Data Center? If DC, can an admin add the
   megaspeed origin to the CORS allowlist? (Decides P1 vs P2 vs P0-only.)
2. **Credential policy:** is a personal PAT/API token in a browser acceptable to
   the org, or must it be SSO/OAuth — i.e. is the proxy mandatory?
3. **Are summaries themselves sensitive** (embargoed epics)? If yes: popover
   only, no inline mode, and a "blur titles" default for shared screens.
4. **First surfaces:** my order is ticket detail header, then delivery/epic
   tables, then queues/verdict (where a title turns "chase 3 reviews" into an
   action). Agree?
5. **Tenant scope:** one Jira site per tenant, or several? (The cache key is
   `tenant|key`; a multi-site tenant needs `site|key`.)

## Acceptance

- **Unconfigured is byte-identical to today:** no extra request, no DOM change,
  every existing node test green.
- **Configured:** every `[data-ticket]` in a rendered view resolves from cache
  within one frame; the popover appears on hover and on focus; no layout shift
  in any table (assert row heights and column widths unchanged).
- **Ticket detail** shows the summary as a subtitle; without a resolver it shows
  exactly what it shows today.
- **Rules §5** all green, especially: no title in `--verify`, none in the API
  schema, none in an export, none in storage before connect.
- **Degradation:** 401 shows one inline "your Jira session expired" and keeps
  the keys; 403 on one key leaves it unresolved without failing the batch; 429
  backs off and retries once; offline renders the cache.
- Store contract, `--verify`, and all `tests/` + `app/test` suites unchanged.

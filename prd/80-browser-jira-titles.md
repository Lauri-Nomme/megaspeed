# 80 — Ticket titles, resolved in the browser

Status: **draft; the Jira content never transits megaspeed (hard rule).**

## Problem

The store is key-only on purpose (prd/02: keys allowed, ticket **content**
forbidden), and the UI follows it (prd/08: "tooltips and labels render only
keys, pseudonyms, and closed attributes"). The consequence is now the biggest
usability gap left: at the ticket and epic level the product says *"DEMO-347
took 21 days and 6 of them were waiting for a reviewer"* and the reader cannot
tell which work that was. `DEMO-347` does not tell you whether to chase a
payment bug, a parser crash, or a doc tweak. A dozen words of human-written
text per key is the whole difference between a dashboard and an action.

## The invariant, and the constraint that forces the design

A title is ticket content. So:

> **The title never comes from megaspeed.** It travels Jira → the operator's
> browser → the screen. It is never stored in the store, served by the API,
> proxied by the server, written into an export, a metric, or an LLM view. The
> content does not transit the megaspeed server at all.

This is now the load-bearing constraint, and it rules out the obvious
implementation:

**A "connect with a token" panel inside the megaspeed page cannot work against
Jira Cloud.** Verified, not assumed:

- Jira Cloud's site REST (`https://<site>.atlassian.net/rest/api/3/…`) **does
  not send CORS headers**; Atlassian's answer is that the only bypass is a
  proxy. A browser `fetch` from the megaspeed origin is blocked before it
  returns a body.
- OAuth 2.0 3LO **requires a client secret** at the token-exchange step, and
  PKCE is not honoured (ATL `OAUTH20-2491`). A public browser client cannot
  complete 3LO without a server holding the secret — i.e. without a proxy,
  which is excluded.
- The 3LO gateway (`api.atlassian.com/ex/jira/{cloudId}`) is CORS-enabled, but
  it accepts OAuth access tokens, not API tokens, so it does not reopen the
  path on its own.

Consequence: the resolver is fed by a **file/clipboard import** by default, and
an optional **browser extension** for people who want it automatic. Both keep
Jira's content entirely out of megaspeed. A store bundle copied to another
machine still has zero titles; `--verify` and the canary still pass.

What it **does** change, written down rather than glossed: a connected megaspeed
screenshot shows real summaries, so the feature ships with a visible
**hide/show toggle** and a "Forget titles" action, and exports never include a
title.

## What already exists (why this is cheap)

- **One choke point.** Every rendered key goes through `ticketLink()` in
  `app/tickets.js` (15 call sites) and already carries `data-ticket="DEMO-347"`.
  A post-render hydration pass needs **no call-site change**.
- **A popover vocabulary.** `--popover-bg`, `--radius-popover`,
  `--shadow-popover` exist in `app/style.css`; tooltips already float over the
  graph and the benchmark cards.
- **The keys are already client-side.** `/api/objects` and the ticket detail
  payload carry `ticket_keys`; nothing new needs to be exposed.
- **The topbar** already hosts the theme toggle; the titles toggle goes beside
  it.

## Decision

A **client-side title resolver**, off until fed, per tenant, cached in
IndexedDB. Two fields only — `summary` and `issuetype` — rendered as a small
type icon plus a hover/focus popover.

### Why exactly two fields

| field | verdict |
|---|---|
| `summary` | the point of the feature |
| `issuetype` | cheap, stable, and turns a popover into a recognisable Jira row |
| `status` | **no** — the store already holds workflow state, from the actual transitions; a second, live copy would disagree with the chart beside it |
| `priority` | **no** — unused anywhere in the UI |
| `assignee` | **forbidden.** Importing a real name next to a store pseudonym is a de-anonymisation channel: it would let anyone build the `LaNo → real person` map the whole pseudonymisation exists to prevent. The store's own actors stay pseudonymous; the resolver never asks for a user field. |

The allowlist is literal and tested: `TITLE_FIELDS = ['summary', 'issuetype']`.
`description`, `comment`, `attachment`, `assignee`, `reporter` are **never**
requested — receiving a description by accident would put the one category of
data prd/02 forbids into the browser's storage.

## Transports

| # | Transport | Credential | Transit | Verdict |
|---|---|---|---|---|
| P0a | **CSV/JSON import** — Jira *Export → CSV (all fields)* or a JQL JSON dump, dropped on the connect panel | none | none | **Ship first.** No auth, no CORS, no extension; works today; permanent fallback. |
| P0b | **Bookmarklet** — runs *on the Jira tab* (same origin, so no CORS), fetches `key,summary,issuetype` for a JQL, copies JSON to the clipboard, pasted into megaspeed | the browser's existing Jira SSO session | none (Jira → clipboard → megaspeed) | Best effort/freshness ratio. No token ever enters megaspeed. |
| P1 | **Browser extension** (unpacked or enterprise-policy installed) — background script uses host permissions to call Jira REST directly | PAT or the SSO cookie | Jira ↔ extension; never megaspeed | The only *automatic* path. Optional. |
| — | direct `fetch` from the page | — | — | **Impossible** (CORS + 3LO secret). Not planned. |
| — | server proxy | — | **excluded** | Not planned; content must not transit megaspeed. |
| — | server-side fetch with titles at rest | — | excluded | A shadow Jira; the thing prd/02 exists to prevent. |

P0a and P0b are one feature: a `key,summary,issuetype` table the browser parses
and caches. The CSV path is the zero-friction default; the bookmarklet removes
the column-drift and staleness a hand-export has.

## Cache

- **IndexedDB** object store `titles`, key `` `${tenant}|${ticketKey}` ``.
  Not `localStorage`: synchronous, 5 MB-capped, and it would sit in the same
  origin as unrelated UI state.
- **Value:** `{summary, type, fetched_at}` — no other field, ever.
- **Merge, don't replace:** an import overwrites the keys it carries and leaves
  the rest; a partial export cannot blank the cache.
- **Staleness:** show the cached title immediately; if `fetched_at` is older
  than 30 days, the popover footer says "from an export · 41 days old". A stale
  title is useful; a silently stale one is a trap.
- **Per tenant.** Switching tenant never shows another dataset's titles.
- **Forget titles** drops the object store.

## Render

`hydrateTitles(root)` runs once at the end of each view render — an explicit
call, not a global `MutationObserver`, so it is deterministic and testable:
`root.querySelectorAll('[data-ticket]')` → cache lookup → annotate.

- **Type icon:** a 14 px inline SVG matched to Jira's own glyph and palette —
  Epic `#6554C0` (lightning), Story `#36B37E` (bookmark), Task `#4BADE8`
  (square-check), Bug `#E5493A` (bug), Sub-task `#4BADE8` (corner arrow),
  unknown `#6B778C` (square). `aria-label` is the type name; SVG over emoji
  because 🟣/🟢/🔵 are indistinguishable at 14 px and do not match Jira.
- **Default: popover on hover and focus.** The key stays the only visible text,
  so **column widths, row heights and density are unchanged** — the property
  that makes this safe across 15 surfaces with existing tests. Content:
  summary (≤2 lines), type icon + name, footer "from your Jira export · not
  stored server-side".
- **Ticket detail page:** the header shows the key as the h1 and the **summary
  as a subtitle line**. Highest-value placement: the page exists to answer
  "where did the time go for this ticket".
- **Epic / ticket list:** the title as a **muted second line under the key**,
  because there the title *is* the content — with an acceptance check that the
  delivery table's row height and column widths do not change.
- **Native `title` attribute** as the fallback for surfaces that cannot anchor a
  popover (graph/canvas): `DEMO-347 — parser crash on an empty queue`.
- **Unresolved keys:** nothing — no shimmer, no placeholder churn.
- **Keyboard/touch:** focusable, Enter/Space opens, Esc closes
  (`aria-expanded`, `role="dialog"`); touch uses a 350 ms long-press so a tap
  still navigates.
- **Coverage** in the connect panel: "412 of 600 keys have a title".

## The toggle

A small topbar control beside the theme toggle: **Titles — on/off**.

- **Off** hides every title, the type icons, the ticket-detail subtitle and the
  popover, and stops the extension asking. One click, immediate, no reload.
- Persisted per browser (a UI preference, not a secret) and reflected in the URL
  as `?titles=off` so a screen-shared link can be opened already-hidden.
- On is the default **once titles exist**; a browser that never imported sees
  keys and nothing else, so the out-of-the-box page is byte-identical to today.

## Rules that must hold (each is a test)

1. **No title ever transits the megaspeed server.** The resolver module cannot
   call same-origin endpoints; the server has no titles endpoint; a test asserts
   the client's only title `fetch` targets a Jira/extension URL or `blob:`.
2. Requested fields are exactly `['summary', 'issuetype']` — asserted.
3. No user field is ever requested or stored (`assignee`, `reporter`).
4. No server endpoint returns a title; `--verify` and the canary are unchanged.
5. Titles are rendered as text nodes (`textContent`), never `innerHTML`; a
   title containing `<script>` renders as text.
6. Titles never reach metrics, labels, exports (Markdown/CSV/print), the policy
   lab or any LLM view: the resolver must not import the metrics/store modules,
   and the export path must not call `hydrateTitles`.
7. Token (extension path only): memory or extension storage, never
   `localStorage`, never a URL, never a cookie megaspeed sets.
8. Off means off: with the toggle off, no title text is in the DOM (a test
   asserts `document.body.textContent` contains no summary).

## Phases

- **P0 (≈1 day) — import + render.** CSV/JSON/JSON-clipboard parser
  (`Issue key`, `Summary`, `Issue Type` / `key`, `fields.summary`,
  `fields.issuetype.name`), IndexedDB merge, `hydrateTitles`, type icons,
  popover, ticket-detail subtitle, epic-list second line, the toggle, "forget",
  coverage. This is the feature; everything after is freshness.
- **P1 (≈1–2 days, optional) — bookmarklet.** ~30 lines of JS run on the Jira
  tab: JQL → `POST /rest/api/3/search/jql` same-origin → clipboard JSON. Ships
  as a copyable snippet plus a `javascript:` bookmark; documented in the connect
  panel, so no install step.
- **P2 (optional, only if P0/P1's freshness is not enough) — extension.**
  Manifest V3, `host_permissions` for the Jira origin, a toolbar action that
  refreshes the current view's keys and writes them via the same import shape.
  Documented as "unpacked or enterprise-installed"; not required for the
  feature to work.

## Open questions

1. **Scope of the import:** every epic plus its children (the ingest's own scope),
   or a JQL the operator pastes? Default: the same epic/ticket set the store
   covers, so coverage is comparable.
2. **Refresh habit:** is a weekly/monthly CSV drop acceptable, or do we go
   straight to the bookmarklet (P1) as the documented default? (The CSV path
   still ships either way.)
3. **Ticketing only, or also Confluence?** Out of scope here; asking so it is
   not silently implied.

## Acceptance

- **Unconfigured is byte-identical to today:** no request, no DOM change, no
  new node in the topbar until the resolver has data; all existing node tests
  green.
- **Configured:** every `[data-ticket]` in a rendered view resolves within one
  frame; the popover appears on hover and on focus; no layout shift in the
  delivery table (row heights and column widths asserted unchanged).
- **Ticket detail** shows the summary subtitle; the epic list shows the muted
  second line.
- **Toggle off** removes every title from the DOM and stops the extension.
- **Rules §"Rules that must hold"** all green, especially: nothing in the API
  schema, nothing in `--verify`, nothing in an export, no title request to the
  megaspeed origin.
- **Degradation:** a malformed CSV line is skipped with a count, never a partial
  write; a stale cache renders with its age; a missing key shows the key alone.
- Store contract, `--verify`, `tests/` and `app/test` suites unchanged.

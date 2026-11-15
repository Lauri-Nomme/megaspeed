# QA round 2 — web UI/UX + functional requirements

Scope: the live app at `http://127.0.0.1:8766/` (tenant `del`), checked against
`prd/09-web-ui.md`, `prd/10-ux-look-feel.md` and `prd/17-analysis-rules.md`.
Commit `eac203d`; headless Chromium 1600×1000 @ dpr 1; data `1316 events ·
131 PRs · 467 commits · 7 open PRs`, snapshot `2026-11-15T17:46:40Z`; also the
`megaspeed` fixture tenant (420 events / 43 PRs) and the empty `live-probe`
tenant. Method: `POST /api/eval` for DOM/state + real mouse/wheel/keyboard
events, `POST /api/screenshot` for each view (evidence in
`/tmp/megaspeed-shots/qa2-*.png`, not committed).

Notes on the environment:
- The running server was 6 commits behind the working tree and returned **404
  for `/api/insights`** until restarted. A long-running process can therefore
  serve new static app files with an old API and no version handshake (G12).
- A second browser page was attached to the same server for part of the run.
  Eval broadcasts to every page, so the eval calls below are gated on
  `location.hostname === 'localhost'` to make responses deterministic.
- `__megaspeedShot` does not serialise a `<select>`'s live value or fixed-position
  overlays: screenshots can show the first option / omit the drawer (B21). The
  select values quoted below were read from the live DOM, not from screenshots.

## Coverage

| # | Check | Result | Note |
| --- | --- | --- | --- |
| 1 | Shell: top bar, title + question, provenance | **partial** | All present; theme and refresh controls missing (G8) |
| 2 | Design tokens, graphite/navy grounds, hairlines | **pass** | Tokens match prd/10 exactly; body `#191B1C`, canvas `#151924` |
| 3 | Typography: 20/600 title, 13px question, tabular numbers, mono keys | **partial** | Tabular ✓, mono for ids/timestamps ✓, ticket keys are sans (B20), section labels 10px not 11px (G11) |
| 4 | Semantic colour: tinted pills, zero not red, direction pair | **partial** | Pills/zero ✓, teal/amber ✓; the honesty line is permanently warning-yellow (B14) |
| 5 | Density S/M/L | **partial** | Timeline lanes 15/22/30px, table rows 24/30/36px; Rules rows identical in all three (B18); control height fixed (G11) |
| 6 | Scope banner names what is / isn't filtered | **partial** | Renders and is readable; the person-scope sentence is false for the queue view (B3) |
| 7 | Timeline lanes (actors / PRs / queues) | **pass** | 7 actor lanes, 131 repo-qualified PR lanes, 2 queue lanes |
| 8 | Timeline event markers + hover (ts, activity, actor, objects, attrs) | **pass** | Tooltip verified |
| 9 | Gantt lifecycle bars + phase legend | **pass** | Segments, hatch for unobserved, end caps |
| 10 | Zoom / pan / Fit / keyboard | **pass** | Wheel, drag, −/+/Fit, +/−/arrows/Home; 93 d → 10 min |
| 11 | Histogram strip + brush → selection chip → Set timeframe | **pass** | Brush and apply work |
| 12 | Timeline honesty (aggregated / censored / truncated) | **partial** | Text is correct on Timeline, leaks onto every other view (B7); no cost-labelled "load full range" (G5) |
| 13 | Timeline filters: epic, sprint, role, phase, search | **fail** | Only the activity legend + timeframe exist (G2) |
| 14 | Semantic zoom epic → story → PR → event | **fail** | Flat PR lanes only; no story/epic objects in the store (G3) |
| 15 | Delivery tabs + counts + rows | **partial** | Counts follow scope; `Blocked 0` is unselectable (B11) |
| 16 | Delivery detail: impact sentence first, facts, pivot chips, Copy | **partial** | Panel works; impact is wrong by orders of magnitude (B1), keeps out-of-scope rows (B9), menu lacks the three groups (G7) |
| 17 | Blocking map render, lens recolor, hover, direction pair | **pass** | Layout identical across lenses; teal outbound / amber inbound verified |
| 18 | Blocking map hierarchy, pan/zoom, node drill-down | **partial** | Flat nodes, no zoom/fly-to (G4); drill-down can land on an empty queue (B5) |
| 19 | Queue view: levels, WIP, throughput, diagnosis, row click | **partial** | WIP + completions + overloaded/slow ✓; no y-axis (B15); `author` row mis-scopes (B4) |
| 20 | Rules: ranked list, severity, evidence, impact, copy, levels | **partial** | 4 team rules ranked correctly; `people` level is a no-op (B16); 5/9 metrics silently absent (G6) |
| 21 | Overview: counts, needs attention, waits, queue diagnosis, links | **partial** | Links work; "overloaded queues" counts people when the queue level is `people` (B17) |
| 22 | Sources: provenance facts, read-only/pseudonymised notes | **partial** | Facts are correct; no Rebuild/read time/cadence (G8) |
| 23 | Tenant switcher: list, counts, switch, isolation, per-tenant scope | **partial** | 1316 ↔ 420 ↔ 0 events; scope per tenant restored; selection chip not cleared (B12) |
| 24 | `/api/insights` contract + ranking | **pass** | `{generated_at, as_of, hours_mode, sample, suggestions[…]}`, ranked by impact × confidence |
| 25 | `/api/events` pagination + `/api/events/counts` histogram | **pass** | `limit/offset`, `X-Total-Count`, 91 daily buckets summing to 1316 |
| 26 | Empty states (empty store, empty subset, empty rules) | **partial** | Full sentences ✓; zero events after scope/zoom shows a blank canvas with no message (B10) |
| 27 | Keyboard + a11y | **partial** | Canvas keys, row Enter, focus-visible, reduced-motion ✓; static aria-labels on two canvases, no keyboard pivot (G10) |

## Bugs

### B1 — Impact is wrong by orders of magnitude (HIGH)

Repro: tenant `del` → **Delivery list** → any row (e.g. `#53 delivery-api`).

Expected: impact = **open** PRs that share a real ticket × current wait, with
duplicate keys removed. With 7 open PRs this should be a handful of dependents
and a plausible number of hours.

Actual: `dependents: 131`, `impact_hours: 82248.4` → the table and detail panel
show **`≈3427 d 0 h`** and the sentence "131 other **open** PRs share its ticket".
The store has only 7 open PRs. `analysis/ballcourt.py` builds the ticket map from
all PRs (merged/declined included) and sums `len(tickets[key]) - 1` across every
key, so the count is all 131 PRs and the shared key `DEL-1` inflates it further.
Evidence: `qa2-02-delivery.png`.

Impact: the headline triage number ("what does this wait cost?") is unusable and
directly contradicts the store's own `counts.open: 7`.

### B2 — A shared `DEL-1` key pollutes every ticket-labelled surface (HIGH)

Repro: any view that shows ticket keys.

Expected: one meaningful ticket key per PR (or the epic shown separately), so
`DEL-167` can be compared with `DEL-168`.

Actual: every one of the 131 PRs carries `ticket_keys: ["DEL-1", "DEL-1xx"]`.
The Delivery list shows `DEL-1 DEL-167 #53`, and the timeline bar label takes
only `keys[0]`, so **every bar reads `DEL-1 #n repo`** and the real key is hidden
(`app/timeline.js` → `` `${pr.keys[0]} …` ``). The same key is used for
ticket-based impact, grouping and export. Evidence: `qa2-01-timeline.png`,
`qa2-02-delivery.png`.

Impact: ticket-level comparison is impossible in the timeline, the table's first
key is noise, and B1 is a direct consequence. If `DEL-1` is an epic link it
should not be in the PR's story-ticket list; if it is a parse artifact it should
not reach the store.

### B3 — The scope banner lies about the queue view (HIGH, honesty)

Repro: scope to a person (e.g. `AlNg`) → open **Queue**.

Expected (banner): "timeline events and triage rows are filtered to this person;
the blocking map still shows everything" — or filter the queue and say so.

Actual: the banner says "the blocking map, **queue series** and policy lab still
show everything", but `renderQueuePage()` filters streams through
`queueMatchesScope`, so the chart shows **only AlNg**. Evidence:
`qa2-07-queue.png`.

Impact: prd/10 §4.2 calls this "the single most important honesty device"; it
tells the user a view is unfiltered when it is filtered.

### B4 — The Queue "author" row scopes to a person called `author` (HIGH)

Repro: tenant `del` → **Queue** → level `queues` → click the **author** row.

Expected: scope to the author **queue** (role), as the banner's role variant
describes.

Actual: `state.scope = {kind:"person", value:"author"}`. The banner reads
"Scope author: … filtered to this person", the top-bar scope `<select>` goes
blank, and the timeline renders **0 events on a blank canvas** with no empty
message (B10). `setScopeFromQueue()` only special-cases `sentinel`; the API
reports the `author` stream as `role: "person"`, so it falls into the person
branch. Evidence: `qa2-23-author-scope.png`.

Impact: a single click on a normal queue row produces a nonsense scope and a
blank primary view.

### B5 — Blocking-map drill-down can land on an empty queue (HIGH)

Repro: **Queue** → level `queues`; **Blocking map** → level `people`; click a
person node (e.g. `AlNg`).

Expected: the queue view opens scoped to that person, with their stream visible.

Actual: it opens the Queue page with `scope = person AlNg` but
`queueLevel = "queue"`, so `queueMatchesScope` matches no stream and the chart
shows **"No measured waits in this window."** (a stale tooltip from the previous
queue view is still floating). `openNodeQueue()` scopes the person but never
changes the queue level. Evidence: `qa2-19-drilldown-empty.png`.

Impact: the documented "click a node → its queue time series" doorway silently
shows an empty screen.

### B6 — The timeframe control does not reflect brush/zoom (MEDIUM)

Repro: Timeline → brush the histogram → **Set timeframe**, or zoom to a 6 h
window with the wheel; then read the top bar.

Expected: a `custom` timeframe state, or the active button updated, so the
control matches the window in `#range`.

Actual: `1q`/`All` stays pressed while `#range` shows e.g. `10 Sep → 30 Sep` or
`1 Oct 02:00 → 1 Oct 08:00`; the range label is the timeline's view and persists
on Delivery/Blocking/Queue/Overview/Sources. A scope change that empties the
event set also collapses the window to 1 day while `1w` stays pressed.
Evidence: `qa2-22-empty-window.png`, `qa2-23-author-scope.png`.

Impact: the user cannot tell which timeframe the page is actually using; other
views keep using the old global timeframe.

### B7 — The honesty line leaks across views (MEDIUM, honesty)

Repro: visit Timeline (note the disclosure), then Delivery, Blocking, Queue,
Overview or Sources. Then visit Rules and go back to Queue/Sources.

Expected: each view states its own cohort/window/aggregation, or the line is
cleared.

Actual: Delivery/Blocking/Queue/Overview/Sources keep the timeline sentence
("aggregated into 2 h buckets — zoom in for individual events · … of … events in
view"); after visiting Rules they show the Rules cohort sentence ("7 PRs opened
in the window are the cohort · 0 classified threads …") on the Queue and Sources
pages. Only the timeline's `onStats` and `renderRulesPage()` write `#honesty`;
the other views never set their own. Evidence: `qa2-10-sources.png`,
`qa2-18-empty-queue.png`.

Impact: every non-timeline page carries a disclosure about a different view,
which is worse than no disclosure.

### B8 — Restored level selects disagree with the state (MEDIUM)

Repro: set Blocking `level = people`, Queue `level = queues`, Density `L`; reload.

Expected: the two selects show `people` and `queues`.

Actual: `state.blockingLevel === "person"` while `#blocking-level.value ===
"queue"`; `state.queueLevel === "queue"` while `#queue-level.value === "person"`.
`restoreUI()` restores the state but never assigns the select values (the lanes
select is assigned). The first click on the already-active level therefore looks
like a no-op. Verified from the live DOM (screenshots misrender selects, B21).

Impact: the control contradicts the graph/chart it labels; confusing and
misleading when reading the level off the toolbar.

### B9 — The Delivery detail panel keeps an out-of-scope row (MEDIUM)

Repro: Delivery → select `#53` → scope to a different person (`BoFe`).

Expected: the panel clears (or the row stays visible/selected).

Actual: the table drops to 2 rows and no row is selected, but the panel still
shows `#53` (author AlNg) with its impact sentence. `setScope` re-renders the
table but not the detail; `renderDeliveryDetail()` uses the stale
`state.selection.row`.

Impact: the panel describes an item the table no longer contains, so a reader can
attribute the numbers to the wrong scope.

### B10 — A zero-event timeline has no empty state; invalid scopes are not reset (MEDIUM)

Repro: reproduce B4, or set `scope = person author`, or zoom into an empty
window.

Expected: the "No events in this window" overlay (or "Every activity is hidden")
and/or `validateScope()` resetting the invalid person scope.

Actual: 0 events, `#empty` stays hidden, the canvas is blank; the only clue is
the small honesty line "0 of 0 events in view". `validateScope()` runs only on
load, not on `setScope`. Evidence: `qa2-23-author-scope.png`.

Impact: the primary view can look broken with no explanation.

### B11 — The `Blocked 0` tab cannot be selected (MEDIUM)

Repro: Delivery list → click **Blocked 0**.

Expected: an empty-state sentence ("Nothing blocked — good") or a disabled tab.

Actual: `renderDeliveryPage()` immediately switches `state.tab` back to
`oldest`, so the click appears to do nothing and the tab never shows as selected.

Impact: a control that looks interactive silently refuses input; the user cannot
tell whether the tab is broken or there are no blocked items.

### B12 — The brush selection chip survives a tenant switch (LOW/MEDIUM)

Repro: brush a range on `del` (chip `10 Sep → 30 Sep`) → switch to `live-probe`
(0 events).

Expected: selection cleared with the tenant's data.

Actual: the chip remains and "Set timeframe" would apply a window from another
dataset; `resetTenantData()` clears `state.selection` but not
`timeline.selection` / the chip. Evidence: `qa2-14-empty-tenant.png`.

Impact: stale cross-dataset state on an empty tenant; the chip is a lie about
what is selected.

### B13 — Transient status and tooltips never revert (LOW)

Repro: click **Copy as Markdown**, then navigate; or hover the queue chart, then
change level/page.

Expected: the status returns to `live · …`; the tooltip hides when its data
changes.

Actual: the top-right stays `copied as Markdown` (the tenant/live state is lost);
the queue tooltip stays on screen after the level switch and after a tenant
switch (`#queue-tip.hidden === false`). Evidence: `qa2-19-drilldown-empty.png`
(stale `review … 6 d 15 h` tooltip over an empty chart).

Impact: stale feedback is read as current state.

### B14 — The honesty line is permanently warning-yellow (MEDIUM, design)

Repro: any view; inspect `#honesty`.

Expected: neutral secondary text; amber reserved for state that needs attention
(prd/10 §1.5 and §3.3).

Actual: `#honesty { color: var(--warning) }` → computed `rgb(251, 205, 88)` on
every page. The disclosure is informational, so every screen reads as an alert
and amber stops meaning "warning".

### B15 — The Queue chart has no y-axis or scale (MEDIUM)

Repro: Queue → any level.

Expected: a y scale for "items waiting" (ticks/labels or a labelled gridline),
so WIP can be read without hovering.

Actual: horizontal gridlines only; the WIP magnitude is available only in the
hover tooltip and the one-line summary (`9.5 wip · ≈159.5 h mean · 1.38/d`).
Evidence: `qa2-07-queue.png`, `qa2-08-queue-levels.png`.

Impact: "who is overloaded" cannot be read at a glance; the chart is decorative
without the tooltip.

### B16 — The Rules `people` level is a no-op (MEDIUM)

Repro: Rules → **people (pseudonyms)**.

Expected: per-person rules, or an explicit "no person-level signal in this
window" state.

Actual: the same 4 team rules on `del` (same 5 on the fixture); every reviewer
has `flagged: false`, so `templates()` emits nothing person-level. The Markdown
export still says "(person)" in its heading while listing team rules.
Evidence: API `level=person` returns only `{kind:"team"}` scopes.

Impact: the toggle looks broken and the export mislabels the cohort.

### B17 — "Overloaded queues" counts people when the queue level is `people` (MEDIUM)

Repro: Queue → level `people` → Overview.

Expected: the stat is labelled for the level (e.g. "overloaded people") or the
Overview pins the queue level.

Actual: `3 OVERLOADED QUEUES` with the panel listing `BiBi`, `review`, `AlNg` —
people counted as queues. `overviewMarkup()` uses whatever `state.queueLevel`
the Queue page last had; `renderQueuePage()`/Overview share it silently.

Impact: a headline count changes meaning depending on a different page's setting.

### B18 — Density does not reach Rules / Overview / Sources (LOW)

Repro: Rules → cycle S/M/L and measure a rule row.

Expected: row height + font + control height scale together (prd/10 §3.5).

Actual: the first rule is `106.36px` in S, M and L. Delivery rows do scale
(24/30/36), the timeline canvas scales, but Rules/Overview/Sources do not; the
toolbar controls stay 24px in every mode.

Impact: the density setting only half-works; "S" does not make the Rules list
denser.

### B19 — Copy and pluralisation nits (LOW)

- "**1 PRs** merged with no approval" (Rules, fixture).
- "**1 rules**" (Rules stats at 1w).
- "**1 OVERLOADED QUEUES**" (Overview).
- "131 **PR(s)** on the same ticket" (detail facts).
- Fixed targets rendered as estimates: "target **≈3 d 0 h**", "hours per round
  **≈2 d 11 h**" (Rules evidence).
- All-hidden timeline keeps the fixture hint: "Run `python3 -m ingest --fixture
  --n 50` and reload."
- Delivery Markdown's status column exports the phase (`review`) while the table
  shows the state pill (`waiting 1w+`).

### B20 — Ticket keys are not mono in the Delivery table (LOW, typography)

prd/10 §3.4 reserves mono for ticket keys. `.table .keys` is link-coloured sans;
only `#53` is mono. The timeline bar labels and the tooltip objects do use mono,
so the table is the odd one out.

### B21 — The screenshot bridge drops overlays and select values (LOW, QA)

Repro: open the drawer, capture `.app`; or set a select to a non-first option and
capture.

Expected: the capture matches the screen.

Actual: the fixed-position drawer is absent from `.app` (only `#drawer` captures
it), and a `<select>` renders its first option regardless of the live value
(`qa2-05-blocking-person.png` shows `role / queue` while the live value is
`person`). `domToImage()` sets `option.selected` as a property, which does not
survive XML serialisation, and the `.shot .drawer { position: static }` rule puts
the drawer below the captured flow. Evidence: `qa2-16-drawer.png` vs
`qa2-16b-drawer-only.png`.

Impact: visual QA evidence can contradict the live UI; reviewers may file or
dismiss bugs on a false screenshot.

## Gaps

### G1 — Missing views promised by the navigation model

`prd/10 §2` lists React (Overview, Open questions, Rebase waste), Flow (Timeline,
Sprints, Flow efficiency), Blocking (Blocking map, Queues, Sentinels), Policy
(Simulation, Drivers), Workspace (Sources, Identity, Settings). The app ships
Overview, Delivery list, Timeline, Blocking map, Queue, Rules, Policy lab (stub)
and Sources. Open questions, Rebase waste, Sprints, Flow efficiency, Sentinels,
Drivers, Identity and Settings do not exist; Policy lab is an honest stub.
Impact: several "answers one question" entry points are missing; Rules is the only
"what should we change" surface.

### G2 — No filters beyond the activity legend and timeframe

prd/09 view 1 requires epic, sprint, role, phase, time range and activity-type
filters, plus search. Only activity on/faint/hide and the 1w/4w/1q/All timeframe
exist. Impact: an investigator cannot narrow by sprint/epic/person/phase, which
is the core "whole first, then narrow" interaction.

### G3 — No story/epic objects, no semantic zoom, no epic nesting

Object lanes are flat PR rows; the store has PR/COMMIT/TICKET/THREAD/PRREV only
(no STORY/EPIC/SPRINT). prd/09 asks for objects nested under their epic and
epic → story → PR → event semantic zoom. Impact: the timeline cannot answer
"which epic is slow" and the object view is 131 unrelated rows.

### G4 — The blocking map is a static flat graph

No pan/zoom/fly-to, no role/queue → person nested packing, no group borders for
widespread waits, no edge particles, and no detail panel: a node click navigates
away to Queue (and can land empty, B5). Impact: the spatial view does not scale
past a handful of nodes and loses the "camera fly to keeps orientation" model.

### G5 — Scale and progressive cost are unproven

The canvas lays out one row per PR (131 PRs → 2,273px at M, 3,115px at L) with no
virtualisation or semantic aggregation; 10k events / 1k PRs would produce a
very tall page. The event cap (40k) is disclosed but there is no cost-labelled
"Load the full range" action, and the `All` timeframe passes no `days` to
`/api/delivery|blocking|queues|insights`, so on a store older than 90 days "All"
silently means the API default 90 days while the timeline fits the whole range.
Impact: the "usable from 10k events down to 10" requirement is untested and the
"All" label can be dishonest on a longer store.

### G6 — Rules silently omits 5 of the 9 metrics

On `del` the API emits only cycle-time, rework-loops, review-tail and
conformance-rounds; no rule fires for rebase-waste, comment-tax, pr-size,
reviewer-responsiveness or sentinel-queue. Rebase/comment/size have no data
(the store predates `push_class`/LLM classification/diff size,
`sample.threads: 0`); reviewer-responsiveness has no flagged person. The
assumption line mentions diff size only. Impact: a reader cannot tell whether a
rule is healthy or simply has no data; "0 critical" overstates confidence.

### G7 — Pivot/export coverage is partial

The three-group pivot menu (act here / take elsewhere / view) exists only on
Delivery rows and timeline bars, and only with Scope/Open/Copy/Clear. Blocking
nodes, blocking edges, queue rows and Overview rows have no pivot menu and no
"Copy as Markdown"; there is no keyboard path to open the menu (no Shift+F10),
no "Only this one / Leave it out / Fly to it", and no "Set as A / Set as B"
selection chip. Impact: prd/10 §6 "pivot-anywhere" and "export as text, a primary
affordance" are only half delivered.

### G8 — Global context controls and provenance actions are missing

The top bar has no theme toggle (no light theme exists) and no refresh cadence
(`off | 1m | 1h`). The provenance line has no *Rebuild* action and no read time.
Impact: two of the five top-bar controls in prd/10 §4.1 and two of the four
provenance items in §4.4 are absent.

### G9 — Queue/throughput presentation is incomplete

Throughput is drawn as grey ticks, not a paired series; there is no y-axis or
legend swatch for WIP vs completions; the synthetic `review`/`author` streams
are modelled inconsistently (`review` is synthetic and unclickable, `author` is
`person` and mis-scopes — B4). Sentinel/CI/calendar/ready→refined queues do not
appear because the store has no such ball states. Impact: the "overloaded vs
merely slow" diagnosis is harder to read than the data allows.

### G10 — Accessibility is partial on the canvases

The timeline canvas has a rich dynamic `aria-label`; the blocking and queue
canvases have static labels ("blocking map", "queue WIP over time") with no
counts/selection. There is no keyboard path to the pivot menu, the tenant menu
does not support arrow keys, and the scope banner is not announced (`role`
absent). Focus-visible and reduced-motion are implemented. Impact: the dense
views are usable by keyboard but not described to assistive tech.

### G11 — Visual nits against the design language

No hairline divider under the page header (§4.4); the scope banner sits below the
page title rather than directly under the top bar (§4.2); Sources is capped at
`max-width: 760px` instead of full-bleed (§3.5); section labels are 10px not the
specified 11px (§3.4); units are not spaced as `246 /h` (queue shows `1.09/d`);
density does not scale control height. Impact: small but cumulative drift from
the "dense, full-bleed, hairlines" language.

### G12 — Deployment/version handshake is missing

The long-running server served the new static app while `/api/insights` was still
404; nothing in the UI shows a build/API version or warns that the process is
older than the assets. Impact: a deploy can leave the Rules view broken with only
a generic "view failed: … 404" status; QA must restart the process to test the
newest slice.

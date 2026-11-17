# QA — mobile viewport (390 × 844 and 412 × 915)

Verification of `prd/28-mobile.md` against the live app. `prd/28` now requires the
UI to be "optimal on both a desktop monitor and a OnePlus IN2025-class phone
(~412×915 CSS viewport)", with a definition of done: every view usable at
412×915, no horizontal overflow, all controls reachable, tap targets ≥44 px,
pinch/pan on the timeline and graph, ticket detail fully usable. This report
measures how far the current build is from that.

Two phone viewports were measured: **390 × 844** (iPhone-class) and **412 × 915**
(the prd/28 DoD size). Headless Chromium via `harness/browser.py` at dpr 2;
tenant `del-combined` (`2161 events · 131 PRs · 656 commits · 100 stories · 3
sprints`, `as_of 2026-11-17`). Method: `/api/eval` to read layout
(`getBoundingClientRect`, computed styles, canvas internals) and to dispatch the
events a finger would produce, `/api/screenshot` for visual confirmation.

> Harness note (for reproducibility): the phone browser was attached to a
> **second server on `:8767`** pointing at the same read-only store
> (`MEGASPEED_HARNESS_TOKEN_FILE=/tmp/opencode/token-8767`), because `/api/eval`
> broadcasts to every trusted client and two browsers on one port race for the
> answer. The 1600-wide browser on `:8766` was left untouched. To redo it:
>
> ```bash
> MEGASPEED_HARNESS_TOKEN_FILE=/tmp/opencode/token-8767 \
>   .venv/bin/python -m server --port 8767 &
> MEGASPEED_BROWSER_PROFILE=/tmp/opencode/mobile-profile \
>   .venv/bin/python harness/browser.py --url http://127.0.0.1:8767/ \
>   --width 412 --height 915 --device-scale-factor 2 &
> # then POST /api/eval and /api/screenshot with x-megaspeed-token: $(cat /tmp/opencode/token-8767)
> ```
>
> Evidence PNGs (not committed): `/tmp/opencode/mobile-timeline.png`,
> `mobile-timeline-stage.png`, `mobile-timeline-drawer.png`, `mobile-drawer.png`,
> `mobile-delivery.png`, `mobile-blocking.png`, `mobile-blocking-person.png`,
> `mobile-queue.png`, `mobile-rules.png`, `mobile-rules-drill.png`,
> `mobile-ticket.png`, the `mobile-412-*.png` set at the DoD size, plus
> `*-fold.png` crops of the first screen.

## Headline numbers

| measure | 390 × 844 | 412 × 915 (DoD) | consequence |
| --- | --- | --- | --- |
| document `scrollWidth` | **913 px** (2.34 ×) | **913 px** (2.22 ×) | horizontal overflow on *every* view — fails DoD bullet 1 |
| sidebar | **180 px** (46 % of width) | **180 px** (44 %) | main column is 210 / 232 px |
| top bar | one 40 px row, content to **x = 913** | same | scope / timeframe / density / status start off-screen; no wrap |
| control height (`--control-h`) | **24 px** (22–27 px measured) | same | 49 interactive elements on Timeline alone are under 44 × 44 — fails DoD bullet 1 |
| timeline canvas | **210 × 2295** | **232 × 2295** | ~2.5 screens tall; plot is 80 px wide, partly off-canvas |
| timeline plot clipped by the canvas | 42.5 % visible (75/131 bars off-canvas) | 70 % visible (26/107 bars off-canvas) | newest data drawn outside the canvas, no scroll |
| delivery table | 720 px in 210 | 720 px in 232 | `Ticket / PR` column = **0 px** |
| ticket PR table | 970 px in 210 | 970 px in 232 | document grows to **1166 px** |
| `overflow-x` rules in `style.css` | **0** | **0** | nothing ever gets a horizontal scroll container |

(Bar counts differ between the two runs because the timeframe window differs;
the defect is the same and scales with `canvasWidth − 176`.)

## Per-view findings

### Shell (sidebar, top bar, scope / tenant / density)

**Works:** the sidebar's 8 nav buttons are reachable and 26 px tall (below the
44 px guideline, but reliably tappable); nav state and page titles are correct;
the tenant menu opens on tap.

**Breaks**

- **Top bar does not wrap.** `.topbar` is a single 40 px flex row; at 390 px its
  content measures 913 px (at 412 px it is still 913 px). `scope` sits at
  x = 311–480, `timeframe` at 496–720, `density` at 752–830, `status` at
  846–913 — i.e. four of the five global controls are entirely off-screen. They
  are only reachable by panning the *document* (`maxScrollLeft = 776`), and after
  panning the brand, tenant pill, scope, sidebar and main column have all
  scrolled off to the left. There is no horizontal scroll container on the bar
  itself.
- **Sidebar is fixed-width and never collapses.** 180 px of a 390 px screen is
  permanently spent on navigation; the content column is 210 px.
- **Tenant menu is clipped:** 300 px wide anchored at x = 109 → right edge 409,
  so 19 px (and the scrollbar of long lists) falls outside the viewport. Rows are
  27 px tall.
- Density buttons are 26 × 24 px — three targets inside 78 px, which is below
  reliable touch size even when you can reach them.

Screenshot: `mobile-timeline-fold.png` (every other screenshot shows the same
clipped bar).

### Timeline (canvas, lanes, zoom/pan)

**Works:** the data renders; lanes/labels are legible; `−` / `+` / `Fit` zoom
buttons exist (27 × 24 px), so zooming does not *require* a wheel; tapping a wide
bar opens the PR drawer (verified: tap → `PULL REQUEST · DEL-107 #8`).

**Breaks — this is the worst view**

- **57 % of the timeline is drawn outside the canvas.** `GUTTER` is a hard
  constant (`app/timeline.js:61`, `const GUTTER = 176`) and
  `plotW = Math.max(80, width - GUTTER - 10)`. On a 210 px canvas the plot is
  floored at 80 px starting at x = 176, so the plot's right edge is at **x = 256
  while the canvas ends at 210**. Measured on the live view: **75 of 131 PR bars
  and 217 of 373 histogram buckets are entirely off-canvas** — invisible and
  untappable, with no scroll container to reach them. Only 42.5 % of the plot is
  inside the canvas. At 412 × 915 the same arithmetic gives a 232 px canvas, a
  176 → 256 plot and **26 of 107 bars** off-canvas (70 % of the plot visible).
- **Bars are sub-pixel-ish targets.** In the 210 px canvas: 131 PR bars, width
  min 2 px / p25 2.7 px / **median 4.35 px** / p90 7.1 px, height 15 px; 373
  histogram buckets at 2 px each. Tapping a specific PR is luck, not selection.
- **Mouse-only input.** `app/timeline.js` binds `mousemove`, `mousedown`,
  `window.mouseup`, `dblclick` and `wheel` — no `pointer*`/`touch*` listeners, and
  `getComputedStyle(#timeline).touchAction === "auto"`. A finger drag therefore
  scrolls the page instead of panning (pan needs `mousedown` → `mousemove` →
  `mouseup`, which the browser does not synthesise for drags); wheel zoom is
  impossible; double-tap zoom is a browser gesture, not the app's; and hover
  tooltips (`mousemove`) never appear on touch. The in-app hint still advertises
  it: *"drag to pan · wheel to zoom · arrows pan · +/− zoom · Home fits"*.
- **The canvas is 2295 px tall** — 8 actor lanes (22 px each) *plus* a
  131-row PR section (15 px bars) drawn one under the other — and the page is
  3612 px tall; the toolbar above it stacks to **435 px** because the legend
  and phase-legend chips wrap at 210 px. The first screen of the Timeline view
  shows no canvas at all — the plot starts 1316 px down.
- `Load the full range`, the selection chips (`Set timeframe`, `×`) and the
  sprint strip are all present but squeezed into the same 210 px column; sprint
  rows measure 426–517 px wide inside it and overflow.

Screenshots: `mobile-timeline.png` (whole page), `mobile-timeline-fold.png`
(first screen), `mobile-timeline-stage.png` (the canvas: labels take 176 of
210 px, the data is the sliver on the right).

### Delivery (table overflow)

**Works:** tabs (`Blocked / Oldest waiting / All`) are tappable (26 px); row tap
selects and fills the detail panel (verified: tap → `PULL REQUEST · DEL-183 #66
delivery-api`), and that panel correctly stacks *below* the table (the
`max-width: 1180px` breakpoint already collapses `.split`). `Copy as Markdown`
is present (clipboard path not exercised here).

**Breaks**

- **The table cannot fit and cannot scroll.** Fixed column widths total
  122 + 132 + 92 + 156 + 96 + 122 = **720 px** inside a **210 px** container
  (**232 px** at 412 × 915 — the table width does not change).
  `.table-wrap` has `min-width: 0` and no `overflow-x` (there is none anywhere in
  the stylesheet), so the extra 510 px is simply clipped by the page edge.
- **The identifying column collapses to 0 px.** `table-layout: fixed` with
  `.c-id { width: auto }` after six fixed columns leaves `Ticket / PR` at
  **computed width 0** — the ticket key and PR number are the one thing you
  cannot see (identical at 412 × 915). Only `Status`, a sliver of `Ticket / PR`
  and part of `Waiting on` are on screen.
- Right-click-to-pivot (`contextmenu` handler, advertised in the toolbar hint
  *"click a row to inspect · right-click to pivot"*) has no touch equivalent.
- Rows are 30 px tall — fine for reading, under the 44 px touch guideline.

Screenshot: `mobile-delivery.png`, `mobile-delivery-fold.png`.

### Blocking map (graph interaction)

**Works:** the graph is drawn to the available width and — uniquely among the
canvas views — **is close to usable**: node hit radii measure 23–34 px
(≈46–68 px diameter) with a minimum node-to-node distance of 56 px at 390 × 844
(70 px at 412 × 915), so taps do not collide. Tapping a node selects it and
navigates to its queue (verified: tap `FrMo` → Queue scoped to `person FrMo`).
Labels are legible at 210 px.

**Breaks**

- **Hover tooltips are unreachable.** Node/edge detail (waits, hours, mean,
  "click: open its queue") is only in the `mousemove` tooltip; on touch there is
  no hover, so the numbers behind a node are not available before/after tapping.
- **The stats strip is unreadable:** `wait hours / waiting on others / WIP now /
  wait / day` and the values (`39 edges`, `359 d 10 h`, `mean 5 h 54 min`,
  `8 nodes`) wrap into a broken two-column jumble at 210 px (see screenshot).
- 8 nodes at person level in 210 × 522 px (232 × 566 at 412 × 915; hit radii
  23–34 px, minimum node spacing 70 px): the graph works but is cramped, and the
  assumption paragraph below it becomes ~25 lines of 12 px text.

Screenshots: `mobile-blocking.png` (queue level), `mobile-blocking-person.png`.

### Queue (WIP over time)

**Works:** rows are **62 px** tall and tapping one really does scope the app
(verified: `scope all → person BiBi`); labels (person / role) are readable.

**Breaks:** the chart is a canvas list drawn for a wide layout — the per-row
metrics and sparkline are **clipped at the canvas edge** (`0.08/…`, `1.36/d…`)
with no horizontal scroll, so half of each row's information is missing. The
canvas is 210 × 544; the toolbar hint and the assumption text stack above/below
it.

Screenshot: `mobile-queue.png`, `mobile-queue-fold.png`.

### Rules (drill-down)

**Works:** the rules list renders, ranks and severities are visible, the
evidence drill-down **opens on tap** (`<details>` / `<summary>`, 74 px tall) —
the interaction itself is touch-safe. `Copy as Markdown` is present (clipboard
path not exercised here).

**Breaks**

- The rule card's text column is **127 px wide** at 390 (149 px at 412 × 915 —
  42 px rank column + padding inside the main column), so titles break into 3–5
  words per line and the page is **6562 CSS px tall** (6066 at 412 × 915).
- The drill-down table is **311 px wide inside a 127 px wrapper** (149 px at
  412 × 915; `.drill-wrap` has `overflow-y: auto` only) — the `detail` and
  `measured` columns are cut off.
- The stats strip is mangled the same way as Blocking (`8 rules`, `0 critical`,
  `≈3.8k h at stake`, `125 PRs · 85 stories · 90 d…`).

Screenshots: `mobile-rules.png`, `mobile-rules-fold.png`, `mobile-rules-drill.png`.

### Ticket detail (`#ticket/DEL-167`)

**Works:** the one-line answer, the eight-segment cycle bar, the segment legend
and the boundary timestamps all render readably at 210 px. `← back` and
`Copy as Markdown` are reachable. This is the second-most readable view.

**Breaks**

- **The PR table is 970 px wide** (`.ticket-prs` fixed widths 190/150/150/80/70/150/180)
  inside a 210 px panel and drags the whole document to `scrollWidth = 1166`
  (identical at 412 × 915, where the panel is 232 px). It is completely
  off-screen; you can only see it by panning the page ~6 × viewport widths,
  losing the sidebar and the ticket header.
- The stats strip (`85 h cycle · 8/8 segments · 2 PRs · 8 points · …`) is 239 px
  in a 210 px box → the last two stats are clipped.
- Page is 2526 CSS px tall.

Screenshots: `mobile-ticket.png`, `mobile-ticket-fold.png`.

### PR drawer (opened from a timeline bar)

**Works — the one surface that is genuinely phone-usable.** `.drawer` is
`width: 384px; right: 0` → 384 × 804 at x = 6, i.e. effectively a full-screen
sheet: facts table reads well, related chips wrap, actions (`Timeline`,
`Blocking map`, `Copy as Markdown`) fit. The close button is 26 × 24 px.

Screenshot: `mobile-drawer.png`.

## Verdict

**The build does not meet any of prd/28's four definition-of-done bullets.** At
both 390 × 844 and 412 × 915: the page overflows horizontally on every view
(`scrollWidth` 913), several controls are off-screen, tap targets are 22–27 px,
the timeline and graph have no touch gestures, and the ticket detail — the
PRD's stated primary phone use case — has its PR table 970 px wide in a 232 px
panel. Reading surfaces do work (the ticket answer and cycle bar, the rule
narrative, the PR drawer, the blocking graph), so this is a "not built yet",
not a "fundamentally wrong" gap.

The five worst breakages, in order:

1. **Timeline draws its newest data outside the canvas.** `GUTTER` is a hard 176
   (`app/timeline.js:61`) and `plotW = Math.max(80, width - GUTTER - 10)`, so on a
   210 px canvas the plot runs 176 → 256 and is clipped at 210: **75 of 131 PR
   bars and 217 of 373 histogram buckets are invisible and untappable** (at 412:
   26 of 107 bars). Input is mouse-only as well.
2. **Delivery's `Ticket / PR` column computes to 0 px** (`table-layout: fixed`
   after six fixed columns summing to 720 px in a 232 px box), so the table loses
   the identifier it exists to show — and there is no `overflow-x` anywhere to
   scroll it into view.
3. **Ticket detail's PR table is 970 px in a 232 px panel**, dragging the document
   to 1166 px; the stats strip is 239 px in 232. This is the page prd/28 says must
   be "fully usable one-handed".
4. **The top bar is a 40 px non-wrapping row whose content measures 913 px**:
   scope, timeframe, density and status are off-screen, reachable only by panning
   the document, which scrolls the sidebar and content away.
5. **The fixed 180 px sidebar leaves 210–232 px for everything else**, and no
   element in the app has a horizontal scroll container — wide content is clipped,
   not scrollable.

What prd/28's plan already covers, and where it needs to be more specific:

- Its breakpoints/shell/list-first plan (§Phone layout 1–4) addresses the shell,
  tables, tap targets and the bottom sheet — the right shape.
- It does **not** mention the canvas-overflow defect: a responsive breakpoint
  alone will not fix it, because `GUTTER` is a constant and the 80 px plot floor
  overflows any canvas narrower than 266 px. This also affects a narrow *desktop*
  window, not just phones.
- Its "tables: stack rows into key-value cards (or horizontal scroll with a
  pinned first column)" needs the `.c-id` zero-width bug fixed first — a pinned
  column that computes to 0 px pins nothing.
- Its "detail panel → bottom sheet" is partly already true: `.drawer` is 384 px
  and behaves like a full-screen sheet today (the one genuinely usable surface),
  and the delivery detail panel already stacks below 1180 px. The work is the
  *sidebar*, not the drawer.
- Not in prd/28: hover-only tooltips on the blocking map and timeline (tap must
  show them), the 2295 px canvas height (8 lanes + 131 bar rows drawn in one
  canvas), and `layout()`'s `|| 1200` fallback when the page is hidden (a
  redraw-while-hidden artifact that re-lays-out correctly on return, so latent
  only).

## What phone support would actually require

Mapped to prd/28's own plan, with the specifics the measurement adds:

1. **Responsive shell (prd/28 §1).** Off-canvas sidebar + hamburger below
   ~900 px so `.main` gets the full width (232 → 412); wrap the top bar into two
   rows or a controls sheet, keeping `status` visible; add
   `html, body { overflow-x: hidden }` and move overflow onto each surface.
2. **Stop clipping data (prd/28 §4).** `overflow-x: auto` on `.table-wrap`,
   `.drill-wrap`, `.ticket-prs`, `.sprint-strip`; give `.c-id` a non-zero
   `min-width`; below 640 px replace the 7-column delivery/PR tables with
   two-line list rows (key first) or key-value cards.
3. **Timeline: make the plot fit, then make it touchable (prd/28 §2).** Make
   `GUTTER` scale (`min(96px, 28 % of width)`) and delete the `Math.max(80, …)`
   floor so `gutter + plotW <= canvasWidth` always holds; cap the canvas height
   on phone (lane filter or virtualisation) instead of 2295 px; hide the
   legend/phase-legend behind a toggle (they consume 435 px of toolbar at 210 px
   wide); add `touch-action: none` + `pointerdown/move/up` for one-finger pan and
   two-pointer pinch, keeping the mouse path; show the tooltip on tap.
4. **Tap targets ≥44 px (prd/28 DoD).** `--control-h` is 24 px and nav rows are
   26 px: raise both under the phone breakpoint (49 sub-44 px targets on Timeline
   alone). Density "M" on a phone should mean a ≥40 px row.
5. **Blocking map: keep the tap, add the detail.** It is the closest view to
   working (node hit radii 23–34 px, min node spacing 70 px, tap → scoped Queue
   verified); it needs tap-to-show-tooltip instead of `mousemove`-only, and the
   stats strip reflowed (it currently wraps into an unreadable jumble).
6. **Ticket detail first (prd/28 §3).** The answer/bar/legend are already
   readable; fix the PR table + stats strip and this page meets the PRD's primary
   phone use case with the least work.

## Out of scope (per prd/28)

Native apps; a tablet-specific layout (640–1024 px stays the medium breakpoint).

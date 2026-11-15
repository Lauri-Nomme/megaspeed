# QA — Timeline web UI

Scope: the single existing view (`http://127.0.0.1:8766/`, `app/index.html` +
`app/main.js` + `app/timeline.js`), measured against `prd/09-web-ui.md` and
`prd/10-ux-look-feel.md`.

Environment: commit `700a177`, headless Chromium 1600×1000 @ dpr 1, fixture store
`420 events · 43 PRs · 182 commits · 8 actors · 15 tickets`, `as_of
2026-11-15T16:05:45Z`, source `fixture`, `prs_seen 50 / kept 43 / dropped 7`.
Method: `/api/eval` to inspect DOM + internal state and to dispatch real
mouse/wheel events, `/api/screenshot` for visual confirmation of each finding.
Evidence PNGs (not committed): `/tmp/opencode/ui-01-baseline.png`,
`ui-02-tooltip.png`, `ui-03-zoomed-in.png`, `ui-04-object-lanes.png`,
`ui-05-density-S-hidden.png`, `ui-06-all-hidden.png`, `ui-07-bar-tooltip.png`,
`ui-08-overzoomed.png`, `ui-crop-right-axis.png`.

## Coverage

| # | Check | Result | Note |
| --- | --- | --- | --- |
| 1 | Shell (top bar, title, question, provenance) | **partial** | All present, but provenance/honesty is below the fold and half the top-bar controls are static text (B2, G4) |
| 2 | Design language (graphite/navy, tokens, mono, tabular) | **partial** | Grounds and tokens are correct; tertiary text fails 4.5:1 and reserved hues are reused (B8, B12) |
| 3 | Lanes (8 actor lanes, switchable) | **partial** | 8 actor lanes ✓, selector exists ✓, but no *queue* dimension and the object dimension is wrong (B1, G1) |
| 4 | Event markers + hover tooltip | **pass** | Coloured by `activity`; tooltip shows ts, activity, actor, objects, attrs |
| 5 | Gantt bars with phase segments | **partial** | Bars render and segment, but only 2 of the 5 spec phases can ever appear (B3) |
| 6 | Zoom / pan | **pass** | −/+/Fit, wheel zoom at cursor, drag pan, shift-wheel pan, double-click zoom |
| 7 | Filters | **fail** | Only legend hide; no epic/sprint/role/phase/time-range/search (G4) |
| 8 | Data honesty | **partial** | Disclosures are correct and specific, but out of view (B2) |
| 9 | Density | **pass** | S/M/L re-scales lane, bar, label and canvas (741 / 969 / 1291 px); not persisted (B14) |
| 10 | Other views (blocking, queue, delivery, policy) | **fail** | None exist (G6–G9) |

---

## Bugs

### B1 — Object lanes merge distinct PRs and drop the repo from the label — HIGH

Repro: `#lanes` → `PRs` (or `window.__megaspeed.setLanes("object")`).

Expected: one lane per PR — 43 lanes, each identified unambiguously (repo + `#number`).

Actual: the header reads `PULL REQUESTS · 25` while the footer on the same screen
says `43 PRs`. `laneKey()` returns `` `#${event.pr_id}` `` (no repo), and the
fixture has 18 PR numbers that occur in both `ENG/mobile` and `ENG/platform`
(`#100 … #124`, e.g. `mobile#106` and `platform#106`). Events from two unrelated
PRs are therefore drawn in one row labelled `#106`: 43 PRs collapse into 25 lanes
and no lane is unique.
The bar section below shows the same PRs correctly as `PROJ-104 #106 mobile` /
`PROJ-104 #106 platform`, so the two panels disagree.
Evidence: `ui-04-object-lanes.png`.

Impact: any conclusion drawn in object mode ("this PR was in review for 20 days")
can be a mix of two PRs, and the count in the header contradicts the provenance line.

### B2 — Provenance and honesty lines render below the fold — HIGH

Repro: load the page at 1600×1000 in the default M density and look at the viewport.

Expected (`10` §7, build checklist): the snapshot/provenance line and the
aggregation/censoring disclosure are visible "in the main flow, not in tooltips",
on the same screen as the numbers they qualify.

Actual: `.foot` sits at y = 1126–1193 on a 1000 px viewport — the whole
`Snapshot from 17 min ago: 420 events, 43 PRs, 182 commits · fixture, 7 PRs
without a ticket key dropped · aggregated into 1 h buckets · 8 PRs still open are
right-censored` block is off-screen until you scroll. The canvas alone is 969 px
tall at M density (1291 px at L), so this happens by default on a laptop screen.
Evidence: `ui-01-baseline.png`, `ui-08-overzoomed.png` (footer only visible
because the screenshot captures the full element, not the viewport).

Impact: the single "never lie about the data" device is the first thing to fall
off screen — the numbers are read, the caveats are not.

### B3 — The gantt can only ever show 2 of the 5 lifecycle phases; the legend advertises a phase that never renders — MEDIUM

Repro: hover any lifecycle bar, then compare the segment colours with the
`in review / waiting on author / closed` legend in the toolbar.

Expected: segments for waiting / active / in review / CI / rebasing, with a legend
key for every colour that is painted.

Actual: `phaseOf()` maps only onto `review` and `waiting`. Across all 43 PRs there
are 385 segments: **228 `review`, 157 `waiting`, 0 `active`, 0 `done`.** So:
* `PHASE.active` ("in progress", `#B565E8`) is dead code and is absent from the
  legend even though purple `pr_pushed` ticks appear inside the bars;
* the `closed` (#8A8D93) key in the toolbar legend is never painted — segments stop
  at `pr.closed`, so `done` is unreachable;
* there is no CI and no rebasing phase at all, although the spec names both;
* `pr_pushed` is classified as `in review`, so author rework reads as review time —
  all 52 pushes in the fixture land in the "in review" bucket;
* the last segment of every PR is "time since the last event", painted in the last
  phase colour and indistinguishable from an observed phase. PR `#106 mobile` shows
  a single uninterrupted `waiting on author` block of 61.1 days after 2026-09-15
  (`ui-07-bar-tooltip.png`), i.e. the bar presents absence of evidence as a measured
  two-month wait.

Impact: the page's question is "where does the time go?" and the bar — the one
element that answers it — can only distinguish two states, one of which is
mis-labelled. This silently biases every "review is the bottleneck" reading.

### B4 — Legend filters reorder the lanes and rewrite the gantt, instead of de-emphasising — MEDIUM

Repro: at the fit view, click the `pr_merged` key in the activity legend.

Expected (`10` §6, §10): *filters de-emphasise, don't delete*; layout is stable so
spatial memory survives.

Actual: the actor lanes re-sort, because `laneRows()` orders by *visible* event
count: `ToBr, KrSa, MaTa, PeMe, …` becomes `ToBr, MaTa, KrSa, PeMe, …`. Hidden
activities are also removed from `visibleEvents()`, so the PR bars are re-segmented
from the remaining events — a filter on markers changes the derived lifecycle bars:
hiding `comment_added` takes PR `123 mobile` from 16 phase segments to 13 and
deletes the `waiting` stretches that were bounded by a comment. No grey/faint/hide
choice is offered; the only value at the right is deletion.
Evidence: `ui-05-density-S-hidden.png` (note the swapped KrSa/MaTa rows and the
missing purple ticks inside the bars).

Impact: two people looking at the same screen with one key toggled see different
row orders and different cycle-time splits; comparisons across filter states are not
valid.

### B5 — Any data refresh throws away zoom, pan and (implicitly) the user's question — MEDIUM

Repro: zoom into a day, then let the store change (or call
`window.__megaspeed.refresh()`).

Expected: live ingest updates the data underneath the current view.

Actual: `load() → setData() → this.fit()` — the view jumps from a 10.05-day window
back to the full 77.54-day domain. The page is wired to reload on every
`{"event":"changed"}` WebSocket message, so with refresh cadence enabled this
happens on every ingest.

Impact: an investigator who has zoomed to the week in question loses the window
periodically, with no notice and no restore.

### B6 — Tooltips survive the state that produced them — MEDIUM

Repro: hover an event marker, then click a legend key (or zoom) without touching
the canvas again.

Expected: the tooltip is dismissed or recomputed when the underlying data/view
changes.

Actual: `#tip` keeps its old HTML and position. In `ui-06-all-hidden.png` the
tooltip still reads `3 events / LANE PeMe / ZOOM zoom in for individual events`
while the `PeMe` lane no longer exists and the canvas is empty; in
`ui-08-overzoomed.png` a `pr_needs_work` tooltip is still pinned after a two-step
zoom-out.

Impact: it is possible to read a tooltip about an event that is not on screen, and
to screenshot it as if it were.

### B7 — Axis labels overlap each other and are clipped at the right edge — MEDIUM

Repro: zoom to a ~12 h window that crosses midnight (e.g. `9 Oct 17:00 → 10 Oct
05:00`).

Expected: one legible tick row, with the date and the clock time distinguishable,
and no glyph drawn outside the canvas.

Actual: the day label is painted at y = 10 and the hour label at y = 22 (12 px
apart, 11 px type) so `10 Oct` and `00:00` stack into a single two-line smear at
the edge. Labels are drawn left-aligned at `x + 4` with no right-edge guard, so any
tick within ~40 px of `gutter + plotW` is cut off by the canvas boundary — the
screenshot shows a bare `10` over `00` sliced by the viewport edge.
Evidence: `ui-03-zoomed-in.png` and `ui-crop-right-axis.png` (4× crop).

Impact: at the zoom levels where you actually read individual events, the axis
label for "now" is the one that is unreadable.

### B8 — No keyboard path, no accessible state, and secondary labels under 4.5:1 — MEDIUM

Repro: `Tab` through the page; inspect `#timeline`; measure `--text-tertiary`.

Expected (`10` §11): full keyboard path, state not conveyed by colour alone,
contrast ≥ 4.5:1.

Actual:
* `#timeline` has `tabindex = -1`, no `role`, no keydown handler anywhere in
  `app/` — pan, zoom, hover and selection are mouse-only, and the canvas exposes
  only `aria-label="event timeline"`;
* legend toggles and S/M/L buttons have no `aria-pressed`, so the "pr_merged is
  off" state is conveyed by strikethrough and opacity only;
* `--text-tertiary #606165` on `--bg-app #191B1C` = **2.79:1** and on
  `--canvas-bg #151924` = **2.84:1** — that is the colour of `ACTORS · 8`,
  `PR LIFECYCLE · 43`, the axis tick labels, the footer hint and the legend counts.

Impact: the section headers that tell you what you are looking at are the
least legible text on the page, and the view is unusable without a pointer.

### B9 — The screenshot/export bridge misreports control state — LOW

Repro: set lanes to `PRs`, then take a screenshot of `.app`.

Expected: the captured image matches the live DOM (`#lanes` = `object`, shown as
`PRs`).

Actual: the image shows `actors`. `domToImage()` builds the clone with
`cloneNode(true)`, which copies only the `selected` *attribute*; the live
selection lives on the property, so every cloned `<select>` falls back to its
first option. Confirmed directly: live `value="object"`, clone `value="actor"`,
`cloneIndex=0`, no option carries `selected`.
Evidence: `ui-04-object-lanes.png` (canvas says `PULL REQUESTS · 25`, top bar says
`actors`).

Impact: screenshots are the sharing mechanism ("copy as image", bug reports, this
QA), and they can show a different state than the one being discussed.

### B10 — Zoom-out is unbounded to 2× the data domain — LOW

Repro: press `−` twice from the Fit view.

Expected: zooming out stops at the full window (`Fit`), ~77.5 days.

Actual: `zoom()` clamps to `2 × domain`, so the view becomes `31 Jul → 2 Jan`
(155 days) for data from 1 Sep to 2 Nov: roughly 40 % of the canvas is empty on
each side, with no marker of where the data starts and no hint that `Fit` exists.
Evidence: `ui-08-overzoomed.png`.

### B11 — The timeframe readout is day-granular only — LOW

Repro: zoom past ~1 day.

Expected (`10` §4.1): the top bar states the timeframe you are looking at.

Actual: `#range` uses `fmtDay()` at both ends, so a 10-minute window reads
`15 Oct → 15 Oct`; at hour-level zoom there is no clock time anywhere in the top
bar, and a view that does not cross midnight shows no date on the axis either.

### B12 — Reserved hues are reused for categories — LOW

Repro: compare `app/timeline.js` `ACTIVITY_COLOR` / `PHASE` with the tokens in
`prd/10-ux-look-feel.md` §3.2–3.3.

Expected: section 3.3.5 — *waits on* teal `#3CB4AA` / *waited on by* amber
`#D9A24E` are a fixed pair, "never reuse these two hues for anything else"; colour
means state, encoding, interactivity or category, one job each.

Actual: `pr_merged` is painted `#3CB49A` (the teal direction hue);
`--warning #FBCD58` is simultaneously the "needs attention" state, the
`pr_needs_work` category and the `waiting on author` phase; `#63B1FF` is the
`in review` phase and also the aggregated-bucket marker, which has no legend key.

### B13 — Hiding every activity leaves an unexplained blank canvas — LOW

Repro: click all nine legend keys off.

Expected: an empty state that says nothing matches ("No events in this window" is
already implemented in the markup), or an auto-reset.

Actual: `ACTORS · 0`, an empty histogram and empty rows — but all 43 PR labels are
still listed with no bars, the orphan right-censored triangles are still painted at
the snapshot time, and `#empty` stays `hidden` (it is only wired to
`events.length === 0` at load). The stale tooltip also remains (B6).
Evidence: `ui-06-all-hidden.png`.

### B14 — Density, lane dimension and zoom are not remembered — LOW

Repro: set S density and `PRs` lanes, reload.

Expected (`10` §3.5): density is a user setting.

Actual: everything resets to M / actors / fit — `localStorage` is empty, nothing is
stored in the URL, and there is no settings surface.

---

## Gaps

Each item below is required by `09-web-ui.md` or `10-ux-look-feel.md` and is absent
from `app/`; impact is one line.

| # | Missing | Spec | Impact |
| --- | --- | --- | --- |
| G1 | **Queue lane dimension.** The lane selector offers `actors / PRs / activities`; `queues` (review, CI, ready→refined) does not exist. `activities` is not a spec dimension and duplicates the legend. | 09 §View 1 | You cannot see *where work waits* — the whole point of the swimlanes; queue time is only inferable from bar colours. |
| G2 | **Semantic zoom / nesting.** Lanes are flat; no epic → story → PR → event drill, no grouping headers, no `STORY` objects at all (store has only `PR / TICKET / COMMIT / PERSON`). | 09 §View 1, 10 §5.1 | At 420 events the overview is fine, but at 10k there is no way to move between levels; epics are invisible. |
| G3 | **Histogram brush → selection chip.** The histogram strip is a read-only sparkline: no hover, no click, no drag, no `Set timeframe / Set as A / Set as B / ×` chip. | 09 §View 1; 10 §5.1, §6 | Time-window selection — the primary gesture of a timeline — is unavailable; panning is the only way to change the window. |
| G4 | **Filters.** No epic, sprint, role, phase, time-range or activity-type filter (legend hide is not a filter), no search box, and no top-bar *controls*: `scope everything` and `TIMEFRAME 31 Aug → 16 Nov` are static text, with no refresh cadence, no theme toggle, no scope banner. | 09 §View 1; 10 §4.1–4.2 | Every question ("is rebase churn eating us?") has to be answered by eye across all 420 events; there is no way to narrow, and nothing tells you what a narrowed view would exclude. |
| G5 | **Navigation.** No sidebar and no other pages — no Overview, Sprints, Flow efficiency, Sentinels, Sources, Identity, Settings. | 10 §2, §4.3 | The product reads as a one-off chart rather than the four-question tool; nothing carries scope between views, so "pivot" is impossible. |
| G6 | **Blocking map.** No node/edge graph, no wait-hour weighting, no role→person hierarchy, no lens switcher, no drill-down. | 09 §View 2 | "Who is blocking whom?" is unanswerable; dependency chains (the fixture has stacked PRs) are invisible. |
| G7 | **Queue view.** No WIP-vs-throughput time series, no per-person/queue overload diagnosis. | 09 §View 3 | Cannot separate "slow server" from "overloaded server" (Little's law) — i.e. cannot tell a staffing problem from a process problem. |
| G8 | **Delivery list.** No triage table, no `Blocked / Oldest waiting / All` counts, no impact ranking, no detail panel. | 10 §5.3 | "What needs attention?" cannot be answered at all; the actionable subset has to be found by hunting bars. |
| G9 | **Policy lab / simulation.** No scenario config (WIP limits, sentinel rotation, PR size cap, review SLA, stack depth, refinement cadence), no predicted-vs-historical, no A/B. | 09 §View 4 (priority #2); 10 §5.5 | "What should we change first?" is unanswerable — the tool diagnoses but cannot recommend. |
| G10 | **Detail panel and click selection.** Clicking a bar or marker does nothing (`mousedown` only arms a pan); no overline/name/context, no impact sentence, no children, no pivot chips. | 10 §5.4, §6 | No entity can be inspected; hover is the only way to read a single PR, and it disappears the moment you move the mouse. |
| G11 | **Pivot-anywhere menu and selection model.** No right-click menu (act here / take elsewhere / view), no `Copy as Markdown`, no select / shift-click / drag-box, no selection chips. | 10 §6, §10 | You cannot move an investigation from one view to another, or take a finding out of the tool into a ticket or a chat. |
| G12 | **Light theme.** Tokens are dark-only; no toggle. | 10 §3.1 | The "light theme exists as a toggle" requirement is unmet (minor, but blocks the checklist item). |
| G13 | **Commits and tickets are never rendered.** 182 commits and 15 tickets are in the store and fetched; `timeline.js` draws PRs only. | 09 §View 1 (objects) | Commit-level churn — the evidence for "rebase churn is eating us" — is not shown anywhere, even though it is already loaded in the browser. |
| G14 | **Cost-labelled progressive loading.** No "Load the full range" / "Scan the full quarter" action, no truncated-list disclosure, no LOD banner above the canvas (the aggregation sentence lives only in the footer, B2). | 09 §View 1; 10 §5.1, §7 | The honesty scaffolding for large windows is missing, so it will have to be retrofitted when the fixture is replaced by a real quarter of data. |

---

## What already works (so it is not lost in the list)

* Grounds are exactly per token: page `#191B1C`, plot `#151924`, hairlines only, no
  cards, no gradients; mono for keys/pseudonyms/timestamps; `tabular-nums` globally.
* Shell shape is right: 40 px top bar, `Timeline` / `Where does the time go?`, one
  hairline divider, viewport-wide full-bleed canvas.
* Event markers are coloured by `activity`, with a clickable nine-key legend that
  carries counts; hover shows `(ts, activity, actor pseudonym, objects, closed
  attributes)`; overlapping events collapse into a bucket tooltip that discloses
  `≤ 1 h 18 min apart … zoom in for individual events`.
* PR bars are right-censored-aware: open PRs carry an amber end-cap and the tooltip
  says `open · right-censored`, with a per-phase cycle breakdown.
* Zoom/pan is first-class and frame-driven: wheel zoom anchored at the cursor,
  −/+/Fit, drag pan, shift-wheel pan, double-click zoom, 10-minute minimum window.
* Honesty content is genuinely good (aged snapshot, source `fixture`, dropped PRs,
  aggregation bucket, right-censoring) — it is only the *placement* that fails (B2).
* S/M/L density really re-lays-out canvas, lanes, bars and labels together.

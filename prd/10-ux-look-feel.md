# 10 — Design language & UX spec

This document defines how the megaspeed interface **looks, feels and behaves**.
It is about the *form*, not the *data model* (that's `03-data-model.md`) or the
*views' contents* (`09-web-ui.md`). It is the design language every screen is
built from.

## 1. Philosophy

Five beliefs produce every decision:

1. **Every screen answers one question.** A page has a title and, directly
   beneath it, a plain-language question: *"Where does the time go?"*, *"Who is
   blocking whom?"*, *"What should we change first?"*. The question is a
   contract: anything on the page that doesn't serve it moves elsewhere.
2. **The user is an investigator, not a browser.** They arrive with a suspicion
   ("rebase churn is eating us", "the sentinel is the bottleneck") and need to
   confirm or reject it fast. So we show the **whole first** (totals, the full
   timeline, the whole blocking graph), then let them narrow; every object is a
   doorway to another view already scoped to it; and scope/timeframe follow them
   across pages.
3. **Never lie about the data.** Process mining is full of places to fudge:
   censored items, sampled/aggregated timelines, stale snapshots, estimates.
   The UI says so in plain sentences, in the main flow, not in tooltips.
4. **Density is respect.** This is a power tool for a team that lives in these
   numbers. Tight, single-line, full-bleed tables; minimal chrome; density is a
   user setting (S / M / L), not a designer's decree.
5. **Color means something, or it isn't there.** Structure is neutral graphite.
   Color is reserved for **state** (blocked, warning, healthy), **encoding**
   (a wait-hour lens mapped to a gradient), **interactivity** (links, the one
   recommended action), and **category** (activity/entity type). If something is
   red, it needs attention.

## 2. The pages (questions, not features)

Navigation is a set of questions:

| Page | Question | Archetype |
| --- | --- | --- |
| Overview | *What needs attention?* | Delivery list (triage) |
| Timeline | *Where does the time go?* | Stream / timeline explorer |
| Blocking | *Who is blocking whom?* | Blocking map (spatial) |
| Policy lab | *What should we change first?* | Simulation + A/B |

Sidebar groups by **intent**, not data type:

- **React** — Overview, Open questions, Rebase waste.
- **Flow** — Timeline, Sprints, Flow efficiency.
- **Blocking** — Blocking map, Queues, Sentinels.
- **Policy** — Simulation, Drivers.
- **Workspace** — Sources, Identity, Settings.

## 3. Visual foundations

### 3.1 Theme

- **Dark first.** A cool, slightly blue-tinted graphite — never pure black,
  never warm grey.
- Two "grounds": the **UI ground** (neutral graphite for pages, panels, tables)
  and the **canvas ground** (a deeper **navy** for spatial views — the timeline
  and the blocking map). Navy reads as "explorable space", graphite reads as
  "document". A light theme exists as a toggle; the craft is in the dark one.

### 3.2 Tokens

```
--bg-app: #191B1C;        --bg-sidebar: #1E2023;
--bg-surface: #232528;    --bg-surface-raised: #292B2E;
--bg-hover: #303235;      --bg-control: #2F3132;   --bg-control-active: #3B3F42;
--border-hairline: rgba(255,255,255,.06);  --border-control: #3A3D41;
--text-primary: #E8E9EB;  --text-secondary: #919397;  --text-tertiary: #606165;

--canvas-bg: #151924;     --canvas-overlay: rgba(22,24,37,.92);
--canvas-edge: rgba(70,160,200,.30);  --canvas-region-label: #8C98D4;

--critical: #F05C70; --critical-tint: #47282F; --critical-border: #A02C4C;
--warning: #FBCD58;  --warning-tint: #3D3524;
--healthy: #73CF55;
--info-accent: #63B1FF; --info-bg: #252B31;

--link: #94B4D4; --link-strong: #7FB0EA;
--primary-tint-bg: #1B3046; --primary-tint-fg: #68C0FC;
--focus-ring: #68C0FC;

--series-default: #238FF9; --series-warning: #FBCD58; --series-error: #F15D71;
--dir-waits-on: #3CB4AA;   /* teal: who this item is waiting on (outbound) */
--dir-waited-by: #D9A24E;  /* amber: who is waiting on this item (inbound)  */
--code-timestamp: #80B4F0; --code-number: #ECBC70;

--lens-gradient: linear-gradient(90deg,
    #1F3A66 0%, #2878A4 25%, #3CB49C 45%,
    #9CD45A 60%, #F2C14E 75%, #F08A3C 88%, #E8455A 100%);

--cat-1:#73CF55; --cat-2:#53C9DF; --cat-3:#B565E8; --cat-4:#E25AB5;
--cat-5:#E8B84A; --cat-6:#C04B56; --cat-7:#8A8D93;

--radius-control:6px; --radius-popover:8px; --radius-pill:999px;
--shadow-popover: 0 8px 24px rgba(0,0,0,.45);
```

### 3.3 Color rules

1. **Tinted pills for state.** A status badge uses a low-alpha tint of the state
   color as background and the full-saturation color for the dot and label. Never
   a solid saturated fill with white text.
2. **Neutral states are neutral.** "Waiting" and "Closed" are outlined grey
   pills. Only states that need attention get color.
3. **Zero is not red.** "0 blocked" is plain white. Red appears only for counts
   > 0.
4. **Links are soft.** Desaturated blue, so a list of many links stays readable.
5. **Direction has a fixed pair.** *Waits on* (outbound) is teal, *waited on by*
   (inbound) is amber — everywhere: tables, graph edges, legends. Never reuse
   these two hues for anything else.

### 3.4 Typography

- UI: a neutral grotesque sans (Inter / SF Pro / Segoe / Roboto), **tabular
  figures everywhere**.
- Mono: for ticket keys, commit hashes, PR ids, raw timestamps.
- Page title 20 px/600; the question subtitle 13 px/secondary; section labels
  11 px UPPERCASE with +0.06em tracking; body 13 px; table cells 13 px;
  captions 12 px; stat numbers 15–17 px/700.
- **Italic carries context, not emphasis** — used for secondary descriptive
  lines (e.g. a parent epic under a story title).
- Numbers first, labels second, label smaller: **`21,801`** events · **`328`**
  waits.
- Units spaced with a slash: `246 /h`, `5.2k events /d`, `1 h 6 min`.
- Honest compact notation: `≈5.9M`, `20.4k` (with `≈` when estimated).

### 3.5 Spacing, density, elevation

- Base unit 4 px. Three density modes (S/M/L) scale row height + font + control
  height together; **M is default**.
- **Full-bleed** data pages — no max-width reading column.
- **Hairlines, not cards.** Separation from 1 px dividers and background steps.
  Cards appear only for floating overlays on a canvas and for menus.
- Elevation has exactly three layers: ground (flat), overlay (floating cards on
  canvas), popover (menus/tooltips, 8 px radius + soft shadow).

## 4. Application shell

### 4.1 Top bar (global context only)

A slim ~40 px strip holding context that applies to every page, and nothing else:

1. **Scope** (funnel): the active lens — *Team*, an *epic*, a *sprint*, a
   *person* — or "everything".
2. **Timeframe** (clock): `1w`, `1sprint`, `1q`, custom range.
3. **Refresh**: `off | 1m | 1h` (incremental ingest cadence).
4. **Theme** (moon/sun) and **Density** (`S | M | L`).

No page-specific actions live here. The user always knows *where, when, through
which lens* they are looking.

### 4.2 Scope banner

When a non-default scope is active, a full-width single line under the top bar
states it in words — **including what it does and does not filter**:
*"Scope EPIC-42: waits and events are filtered to it; the blocking map and the
Policy lab show everything."* This is the single most important honesty device.

### 4.3 Sidebar

~180 px, grouped by intent (React / Flow / Blocking / Policy / Workspace).
UPPERCASE tracked group labels in secondary; colored 16 px line icons per item.
Selected item = filled rounded background, no left bar.

### 4.4 Page header

Title + question subtitle + hairline divider. Then (for complex pages): view
tabs, a toolbar (lens, layout, search, actions), and a right-aligned provenance
line (snapshot age, size, read time, *Rebuild*).

## 5. Views (archetypes mapped to megaspeed)

### 5.1 Timeline — *"Where does the time go?"*  (#1 output)

A **stream explorer**: LTR time axis, full width.

- **Lanes (swimlanes)**, switchable by dimension: *actors* (person / sentinel /
  CI / calendar), *objects* (epic → story → PR), *queues* (review, CI,
  ready→refined).
- **Events** as markers colored by activity type; hover shows
  `(ts, activity, actor pseudonym, objects, attributes)`.
- **Object bars (gantt-style)**: each Story/PR as a horizontal bar segmented by
  phase (waiting / active / in-review / CI / rebasing), so "where the time went"
  is visible at a glance.
- **Timeline histogram** strip above the lanes; **brush a range** → a selection
  chip appears (*Set timeframe*, *Set as A*, *Set as B*, ×).
- **Zoom in/out** with level-of-detail: coarse zoom = aggregated buckets
  (disclosed: *"aggregated at 1 d; zoom to see individual events"*), fine zoom =
  individual events. Semantic zoom: epic → story → PR → event.
- **Honesty banner** when the visible window is aggregated or truncated, with a
  cost-labelled *"Load the full range"* action.

### 5.2 Blocking map — *"Who is blocking whom?"*

A **spatial** view on navy canvas: the weighted directed graph `X → Y` = "X
waited on Y for N business hours".

- **Hierarchy**: role/queue → person (pseudonym). Nested packing; group borders
  encode state (dashed coral ring = group with widespread waits).
- **Edges**: translucent; thickness ∝ hours. Direction by color: **teal =
  waits-on (outbound)**, **amber = waited-on-by (inbound)**. Flowing particles
  along edges in live mode show "ball in court" direction.
- **Lens switcher** recolors the *same layout* — wait-hours, WIP, throughput,
  flow efficiency, utilization — never relayouts.
- **Hover**: name + context line + the lens value in the lens's own color.
- **Stats strip** overlay: bold numbers, muted nouns, red only if > 0.
- **Legend** (bottom-left): what one mark means, in words.
- **Drill-down**: click a node → its queue view in the detail panel.

### 5.3 Delivery list — *"What needs attention?"*

A **triage table** of items needing action, defaulting to the actionable subset:

- Subset tabs with counts: `Blocked 12 | Oldest waiting 34 | All 189`.
- Dense single-line rows: status pill · mono ticket/PR id · title (flex,
  ellipsis) · **absolute start + right-aligned human duration** · impact
  (blank when zero/unknown).
- Row = PR or story; sorted by "most downstream impact" (dependents × wait),
  not just age.
- Right-click → pivot menu; click → detail panel.

### 5.4 Detail panel

Right column for a selected Story / PR / Person:

- UPPERCASE overline (`STORY`, `PR`, `PERSON`), bold name, italic context line
  (parent epic · author, or role · team).
- Key-values right-aligned (en dash `–` for missing): points, review rounds,
  open questions, cycle time.
- **Impact sentence first, table second**: *"When this PR waits: 3 stacked PRs
  and 2 stories are blocked, directly or within one hop."*
- Children list with colored dots + soft links.
- Footer pivot chips (`Timeline | Blocking map | Copy as Markdown`).

### 5.5 Policy lab — *"What should we change first?"*

Simulation + comparison:

- Scenario config: WIP limits, sentinel rotation/addition, PR size cap, review
  SLA, stack depth, async-refinement cadence.
- **A/B comparison is a first-class mode**: mark two scenarios A and B, see the
  delta in cycle-time distribution and flow efficiency.
- Every "predicted" number is marked `≈`; the model's provenance (calibrated
  from which window) is stated.

## 6. Interaction model

- **Three nested contexts**: global **scope** (top bar), **timeframe** (top bar
  / brush), **selection** (click / shift-click / drag-box). Pivoting carries the
  selection into a *scoped open*; promoting it to global scope is a separate,
  explicitly-named action.
- **Pivot-anywhere**: every entity (row, node, bar, link) has a three-group
  right-click menu — act here (*Only this one*, *Leave it out*, *Fly to it*),
  take elsewhere (*Scope to it*, *Open scoped to it → …*, *Copy*), and the view
  itself (*Whole team*, *Clear scope*).
- **Selection creates commands**: brushing the histogram or picking rows spawns
  an in-place chip, never a modal.
- **Filters de-emphasize, don't delete** (Grey / Faint / Hide).
- **Lenses recolor without relayout** — spatial memory is preserved.
- **Progressive cost**: default queries are bounded; the expensive full version
  is one explicit, cost-labelled click away.
- **Export as text**: any list/panel → "Copy as Markdown" for tickets, chat, or
  an LLM. A primary affordance, not hidden.

## 7. Data honesty (build it in from the start)

| Situation | Disclosure |
| --- | --- |
| Estimated metric | prefix `≈` |
| Censored data (survival analysis) | *"N items still open are treated as right-censored"* |
| Aggregated/zoomed-out timeline | *"aggregated at 1 d buckets"* |
| Truncated list | *"shows the newest 1,000 of the window"* |
| Snapshot from incremental ingest | provenance line: *"Snapshot from 29 min ago: 84k events, 1,240 PRs, ingested incrementally. Rebuild"* |
| Scope filters unevenly | scope banner naming what is / isn't filtered |
| Zero | white, not red — "0 blocked" is not a problem |

The cost of honesty is one short muted line; the cost of dishonesty is lost
trust in every number on screen.

## 8. Voice & microcopy

Calm, factual, concise, slightly conversational. Sentence case everywhere
(UPPERCASE only for section labels/overlines). No exclamation marks, no emoji,
no "Oops!".

- Page subtitles as questions: *"Where does the time go?"*
- Conversational verbs: *"Fly to this PR"*, *"Only this epic"*, *"Leave it out"*,
  *"Scope to this person"*.
- Paired opposites: *"Scope the console to it"* / *"Clear the console's scope"*.
- Consequence sentences: *"When it waits: 3 PRs and 2 stories block on it."*
- Teaching placeholders: *"Filter: ticket, PR, person, phase"*; *"Words, -word,
  field:value"*.
- Cost-explicit actions: *"Load the full range"*, *"Scan the full quarter"*.

## 9. Motion & liveness

- Camera "fly to" on the blocking map: 400–800 ms eased pan/zoom (keeps
  orientation).
- Edge particles show wait direction without numbers.
- Lens switch: 200–300 ms color cross-fade, **no layout movement**.
- Menus/tooltips: 100–150 ms fade.
- Row hover: instant.
- Respect `prefers-reduced-motion`; live mode never moves what the user is
  hovering.

## 10. Anti-patterns

- Card grids with shadows for data → dense tables.
- Decorative gradients/glows in the chrome.
- Solid red/green badges with white text → tinted pills.
- Coloring zero values or neutral states.
- Hiding non-matching items by default → grey them out.
- "Load more" without cost/coverage → say how much and what it costs.
- Unlabelled sampling, truncation, or staleness.
- Generic "Search…" placeholders.
- Title Case buttons, ALL CAPS buttons, jargon verbs.
- Modals for investigation → panels, menus, in-place chips.
- Changing layout when switching lenses.
- Truncating ticket keys or hashes the user needs to compare.

## 11. Build checklist (per screen)

- Title + question subtitle + hairline divider; fits an archetype.
- Global context (scope, timeframe, refresh, theme, density) visible in the top
  bar; scope banner names its exceptions.
- Counts before content; actionable subset is the default; estimates use `≈`;
  censoring/sampling/caps stated in a meta line or banner; full query is
  explicit and cost-labelled; provenance shown.
- Neutral graphite chrome; semantic color only; tinted pills; right-aligned
  tabular numbers; mono for keys/hashes/timestamps; works in S/M/L and light.
- Pivot menu on every entity; selections create chips; filters de-emphasize;
  modifier gestures written in a hint line; "Copy as Markdown" available.
- Sentence case, conversational verbs, no exclamations; empty/no-impact states
  are full sentences.
- Contrast ≥ 4.5:1; state never conveyed by color alone; full keyboard path;
  reduced-motion support.

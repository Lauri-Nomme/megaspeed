# 09 — Web interface & visualization

The product is not a pile of CSVs or a cron of static reports. The primary
deliverable is an **interactive web interface** over the pseudonymised event
log. It lives entirely in the **pseudonymised zone**: read-only, no credentials
to the source systems, and it renders only keys, pseudonyms, and closed
attributes — **never raw content** (see `02-pseudonymization.md`).

## Output priorities

1. **Timeline visualization** — LTR timeline of events and actors, gantt-style
   object bars, and blocking visualization, with zoom in/out. **Ships first.**
2. **Simulation in the web UI** — run calibrated scenarios from the browser and
   compare them. **Ships second.**

## View 1 — Timeline (LTR) · #1 priority

- **Left-to-right time axis**; horizontal distance = time.
- **Lanes (swimlanes)**, switchable by dimension:
  - *actors* — one row per person / queue (sentinel group, `CI`, `calendar`);
  - *objects* — one row per Story / PR, nested under its epic;
  - *queues* — one row per waiting queue (ready→refined, review, CI).
- **Events** as markers on the axis, coloured by `activity` type; hover shows
  `(ts, activity, actor pseudonym, related objects, closed attributes)`.
- **Object bars (gantt-style)** — each Story/PR as a horizontal bar segmented by
  phase colour (waiting / in-progress / in-review / CI / rebasing). This is the
  "~gantt": not task-dependency scheduling, but **lifecycle bars** so you can see
  at a glance where each item's time went.
- **Zoom in/out** on the time axis, from a full quarter down to one PR's hours,
  with **level-of-detail**: coarse zoom aggregates to counts/densities, fine zoom
  shows individual events. Semantic zoom too: epic → story → PR → event.
- **Filters**: epic, sprint, role, phase, time range, activity type.

## View 2 — Blocking visualization

- The weighted directed graph `X → Y` ("X waited on Y for N business hours"),
  edge thickness ∝ hours waited.
- Default at **role/queue level**; person-level only under pseudonyms.
- Click a node → its **queue time series** (WIP vs throughput over time),
  surfacing the "slow server vs overloaded" diagnosis (Little's law).

## View 3 — Queue view

- Per-person/queue time series of items waiting, paired with throughput.
- The working surface for "who is overloaded vs who is slow".

## View 4 — Simulation · #2 priority

- Configure the calibrated **SimPy** model from the browser: WIP limits, sentinel
  rotation/addition, PR size cap, review SLA/time-box, stack depth,
  async-refinement cadence.
- Render **predicted vs historical** cycle-time distributions, flow-efficiency
  deltas, and bottleneck shifts; side-by-side scenario comparison.
- The engine runs Python (SimPy) behind the API; the UI only configures and
  renders. Results are JSON and cached.

## Interaction & constraints

- **Read-only** over the pseudonymised store; no write path from the UI.
- **No raw content anywhere** — tooltips/labels are keys + pseudonyms + closed
  attributes only; the canary test extends to every rendered view.
- **Zoom/pan are first-class** — the timeline must stay usable from ~10k events
  down to ~10; level-of-detail aggregation keeps it responsive.

## Technology (with reasoning)

**The principle: snappiness and density are won in the rendering core, not the
framework.** The dense views (timeline, gantt, graph) are **canvas/WebGL
islands** that bypass the DOM; the framework only hosts chrome (toolbars,
filters, panels) and holds debounced selection state. Pan/zoom runs on a
`requestAnimationFrame` loop, never through framework re-renders. "Intuitive +
high density" is also partly a UX problem the stack can't buy: good defaults,
progressive disclosure, and level-of-detail so we never dump 10k glyphs on
screen at once.

| Concern | Choice | Why |
| --- | --- | --- |
| Backend | thin **read-only** API (FastAPI) over DuckDB | exposes the pseudonymised store as JSON; no write path, so the UI cannot leak raw content |
| App shell | **SvelteKit (TS)** — SolidJS or vanilla TS + Vite as equivalents | compiled, no VDOM, tiny runtime, fast startup; a small single-purpose tool doesn't need React's ecosystem, and the VDOM is the wrong fit for 60 fps pan/zoom |
| Timeline + gantt | **custom Canvas 2D renderer** (own transform + LOD); escalate to **PixiJS/WebGL** only if profiling demands | off-the-shelf chart/gantt libs can't do semantic zoom + event LOD + swimlane intervals; canvas is where density and fps come from |
| Blocking graph | **sigma.js** (WebGL) | weighted directed graphs with thousands of nodes/edges stay interactive; Cytoscape/D3-SVG choke at this density |
| Queue / time-series | **uPlot** (canvas) | tiny, ultra-fast time-series; the correct default over ECharts for the snappy brief |
| Simulation | Python **SimPy** behind the API | reuses the existing model; UI only configures + renders |
| Zero-server fallback | **DuckDB-WASM** + static Parquet | fully local single-user option, no API at all |

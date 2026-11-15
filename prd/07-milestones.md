# 07 — Milestones (delivery slices)

Each slice is independently valuable and lands a coherent piece of the
pipeline. This is a **design-time ordering** — the point of this document is the
plan, not the code. Nothing here is implemented yet.

## Priorities

**Ingest** (first → last): Bitbucket Data Center (#1) → Jira Cloud + Tempo (#2)
→ Slack (#3, de-prioritised).

**Outputs** (first → last): timeline visualization (#1) → simulation in the web
UI (#2). Both ship through the web interface (see `09-web-ui.md`).

## Slice 0 — Bootstrap (done)

Name, rationale, doc skeleton, this ordering. → committed.

## Slice 1 — Bitbucket spine: activities + push classification

Pull the PR activities stream and a local clone per repo; compute `git
patch-id`/`range-diff` push classification. **This alone answers the review and
rebase questions** (approvals lost on pure rebase, rework loop causes) and is
the cleanest data we have. No LLM needed.

## Slice 2 — Jira Cloud layer + Tempo: changelog, sprints, worklogs, links

Issue snapshots, full changelog, sprints, Tempo worklogs, dev-status/remote
links. Adds the ticket layer, carry-over, point churn, and the story↔PR glue.

## Slice 3 — Identity resolution + working-hours calendar

The raw-zone resolution table and the per-person business-hours calendar. Turns
"wall-clock time" into "business-hours wait", which the later slices depend on.

## Slice 4 — Local classifier + pseudonymisation layer goes live

Deterministic classifiers + the cheap local LLM with the closed vocabulary.
**This is the gate**: no LLM-based analysis is allowed to run until this layer
is in place and the canary/schema tests pass. From here on, everything
downstream is pseudonymised.

## Slice 5 — Slack linkage (metadata) — de-prioritised

Ticket/PR regex + LLM linkage for the remainder; bot filtering; DM
metadata-only (or skipped). Adds the question/answer and Slack-discussion
signals. **Not an early priority** — this can land after the #1 and #2 outputs
ship.

## Slice 6 — OCEL assembly + web UI timeline (**#1 output**)

Build the object-centric event log; compute flow efficiency and the
waiting-time segment breakdown. Ship the **interactive web UI**: LTR timeline
of events and actors, gantt-style object bars, zoom in/out — the primary
deliverable.

## Slice 7 — Ball-in-court + blocking & queue views in the web UI

The state machine, the weighted handover graph, and per-person queues
(Little's law). Add the **blocking visualization** and **queue view** to the
web UI — the "who is blocking whom" answer, at role/queue level.

## Slice 8 — Rework loops + cascade cost + conformance

Review-round cause mix, stacked-PR cascade cost on the DAG, conformance
violations.

## Slice 9 — Drivers + simulation engine

Survival analysis of cycle-time drivers; SimPy model calibrated from the data;
the daily "what should I do next" prioritisation report. The simulation engine
runs in Python behind the API, ready to be exposed in the UI.

## Slice 10 — Simulation in the web UI (**#2 output**)

Expose the calibrated SimPy model in the browser: configure scenario parameters
(WIP limits, sentinel rotation, PR size cap, review SLA, stack depth), run, and
compare predicted vs historical cycle-time distributions side by side.

## Dependencies

```
S1 ─┬─► S2 ─► S3 ─► S4 (pseudonymisation gate) ─► S6 ─► S7 ─► S8 ─► S9 ─► S10
    └─► S5 ──────────────────────────────────┘
```

S4 is the hard dependency for everything that touches an LLM; S1–S3 and S5 can
proceed in parallel behind it. S6 ships the #1 output (timeline UI), S10 the #2
output (simulation UI).

## Cross-cutting: incremental ingest & re-analysis

Every slice's scraper is **incremental** (cursor/watermark, idempotent by
natural key), and the event log is **immutable-append** with `as_of`-reproducible
snapshots, so the system can be re-run and re-analysed as more time passes —
not just exported once. See `06-architecture.md`.

## Definition of done (per slice)

- pseudonymisation contract holds (schema + canary tests green),
- SQL event-building is auditable,
- output is reproducible from `classifier_version` + content hash,
- report is at role/queue level with pseudonyms,
- ingest is incremental and re-analysis is `as_of`-reproducible.

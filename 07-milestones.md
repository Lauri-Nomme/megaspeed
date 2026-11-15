# 07 — Milestones (delivery slices)

Each slice is independently valuable and lands a coherent piece of the
pipeline. This is a **design-time ordering** — the point of this document is the
plan, not the code. Nothing here is implemented yet.

## Slice 0 — Bootstrap (done)

Name, rationale, doc skeleton, this ordering. → committed.

## Slice 1 — Bitbucket spine: activities + push classification

Pull the PR activities stream and a local clone per repo; compute `git
patch-id`/`range-diff` push classification. **This alone answers the review and
rebase questions** (approvals lost on pure rebase, rework loop causes) and is
the cleanest data we have. No LLM needed.

## Slice 2 — Jira layer: changelog, sprints, links

Issue snapshots, full changelog, sprints, dev-status/remote links. Adds the
ticket layer, carry-over, point churn, and the story↔PR glue.

## Slice 3 — Identity resolution + working-hours calendar

The raw-zone resolution table and the per-person business-hours calendar. Turns
"wall-clock time" into "business-hours wait", which the later slices depend on.

## Slice 4 — Local classifier + pseudonymisation layer goes live

Deterministic classifiers + the cheap local LLM with the closed vocabulary.
**This is the gate**: no LLM-based analysis is allowed to run until this layer
is in place and the canary/schema tests pass. From here on, everything
downstream is pseudonymised.

## Slice 5 — Slack linkage (metadata)

Ticket/PR regex + LLM linkage for the remainder; bot filtering; DM
metadata-only (or skipped). Adds the question/answer and Slack-discussion
signals.

## Slice 6 — OCEL assembly + waiting-time decomposition

Build the object-centric event log; compute flow efficiency and the
waiting-time segment breakdown. First visible "where does the time go" report.

## Slice 7 — Ball-in-court + blocking graph + queue view

The state machine, the weighted handover graph, and per-person queues
(Little's law). The "who is blocking whom" answer, presented at role/queue
level.

## Slice 8 — Rework loops + cascade cost + conformance

Review-round cause mix, stacked-PR cascade cost on the DAG, conformance
violations.

## Slice 9 — Drivers + simulation + daily report

Survival analysis of cycle-time drivers; SimPy model calibrated from the data;
policy experiments; the daily "what should I do next" prioritisation report.

## Dependencies

```
S1 ─┬─► S2 ─► S3 ─► S4 (pseudonymisation gate) ─► S6 ─► S7 ─► S8 ─► S9
    └─► S5 ──────────────────────────────────┘
```

S4 is the hard dependency for everything that touches an LLM; S1–S3 and S5 can
proceed in parallel behind it.

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

# 73 — Performance long-tail v2 (draft; measure before dispatch)

Status: **draft / not dispatched.** Requires a fresh profile on the current
76k-event combined store; several items from `docs/review-performance.md` were
already fixed since that review.

## Already fixed (do not re-open)

- checkpoint rewrites (the incremental spool) — 1.1;
- serial LLM classification (worker pool) — 1.4;
- `/api/insights` 12 MB payload (drill cap) — 4.1 (payload only; compute still open);
- rules drill-down payload size — 5.3 (row building may remain).

## Candidates (profile first, then keep the ones that still bite)

| id | area | item | note |
|---|---|---|---|
| P1 | analysis | `queue_series` WIP sampling O(intervals × buckets) | the review's CRITICAL; check the current bucketing |
| P2 | analysis/server | `/api/insights` recomputes the same panels ~58× and has no result cache | payload is 1.1 MB now; measure TTFB at `days=1882` |
| P3 | server | `/api/ticket/{key}` rebuilds the Jira layer per request | measure on a ticket with many children |
| P4 | server | bulk `/api/events` relation lookup | one query vs N per event |
| P5 | UI | canvas 65,535 px cliff | draw in segments / keep a viewport transform |
| P6 | UI | rules drill-down O(n²) row building | after the cap |
| P7 | UI/server | initial load: 40k of 76k events | is the cap the right product choice for >100k? |

## Method

1. Copy the live store to a QA sandbox; run the server with a profiler (endpoint
   timings p50/p95, SQL `EXPLAIN ANALYZE`, browser performance trace).
2. Write a short `docs/perf-v018.md` with the measured numbers and drop any
   candidate that is already below its budget.
3. Dispatch **file-disjoint** units only after that:
   - analysis (`analysis/ballcourt.py``queue_series`),
   - server (`server/app.py` caches/relations/ticket),
   - UI (`app/timeline.js`, `app/rules.js`).
4. Acceptance is a measured budget per endpoint/surface, not a code shape.

## Non-goals

No schema changes, no new store tables, no behaviour change in the numbers.

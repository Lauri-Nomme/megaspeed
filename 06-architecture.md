# 06 — Architecture

## Components

```
                     RAW ZONE (local, no cloud, no external model API)
  ┌───────────────┐   ┌────────────────┐   ┌──────────────────────────────┐
  │ scrapers      │──►│ raw staging     │──►│ local classifier             │
  │ jira/bitbucket│   │ (transient, TTL)│   │  · deterministic (patch-id,  │
  │ /slack        │   └────────────────┘   │    regex, schema)             │
  └───────────────┘                        │  · cheap LLM (Ollama/llama.cpp,│
                                           │    JSON-schema constrained)   │
                                           └──────────────┬───────────────┘
                                                          │ keys + pseudonyms + labels only
                                                          ▼
                PSEUDONYMISED ZONE (shareable, LLM-safe)
  ┌─────────────────────────┐   ┌────────────────────┐   ┌──────────────────────────────┐
  │ primary store            │──►│ analysis (SQL)      │──►│ web UI (read-only)            │
  │ DuckDB + Parquet (OCEL)  │   │ + optional open-    │   │  · LTR timeline (events,      │
  │ no free-text columns     │   │   source LLM over   │   │    actors, gantt bars, zoom)  │
  └─────────────────────────┘   │   the labels only    │   │  · blocking & queue views     │
                                └────────────────────┘   │  · simulation (#2)             │
                                                         └──────────────────────────────┘
```

## Technology choices (with reasoning)

| Concern | Choice | Why |
| --- | --- | --- |
| Event store | **DuckDB + Parquet** | local OLAP, zero-ops, fast on a single machine, and the **schema is the pseudonymisation contract** |
| Event-building | **SQL** (dbt-style ordered `.sql`) | auditable, reviewable, reproducible without raw data |
| OCEL / process mining | **PM4Py** (OCEL 2.0) | canonical implementation of the object-centric format |
| Local classification LLM | **Ollama / llama.cpp**, qwen2.5/llama3.2 3B or gemma3/phi-class, temp 0, JSON-schema constrained | cheap, local; **this is what performs raw→generalised classification**; the schema enforces the closed vocabulary |
| Analysis LLM (open-source) | any open model, on the pseudonymised view only | it never sees raw text because it never has it |
| Simulation | **SimPy** (or Simod for BPMN) | discrete-event, calibrated from mined distributions |
| Web UI | **SvelteKit** shell + **Canvas 2D / WebGL** rendering core (sigma.js for graphs, uPlot for time-series), read-only **FastAPI** over DuckDB (DuckDB-WASM fallback) | canvas/WebGL for density + fps; read-only so the UI cannot leak raw content |
| Scrapers | small per-source CLI (Python), rate-limited, resumable, **cursor/watermark-based incremental** | simple, auditable, run in the raw zone only |

## The boundary, enforced three ways

1. **Storage** — raw and pseudonymised live in physically separate paths; the
   pseudonymised store has no free-text content columns (schema-enforced).
2. **Network** — the raw zone has credentials to Jira/Bitbucket/Slack; the
   analysis zone has **none** and cannot reach the raw zone.
3. **Policy** — raw staging auto-deletes (TTL); the pseudonymised store is the
   only thing that may be backed up, shared, or handed to an LLM.

The classifier emits, alongside every event, `classifier_version` and a content
hash, so outputs are reproducible and tunable without retaining raw text.

The web UI is a **read-only** client of the pseudonymised store: it has no
credentials to the source systems and no write path, so it cannot leak raw
content even by accident (see `09-web-ui.md`).

## Deployment target

One machine (or one container) for the raw zone, one for the pseudonymised
zone. No multi-tenant, no external services in v1. This is a single-team,
single-company, self-hosted tool.

## Incremental ingest & re-analysis

The system is **append-only over time**, not a one-shot export. Two concerns:

**Incremental ingest.** Each scraper keeps a cursor/watermark (`updatedAt` per
issue, the Bitbucket activities-stream cursor, `dateStarted` per Tempo worklog,
Slack `ts`) and fetches only what changed since the last run. Idempotency by
natural key (issue key + changelog ID, PR + activity ID, worklog ID, Slack
`ts`) so a re-run never double-counts events.

**Re-analysis as time passes.** New data doesn't invalidate the pipeline, it
extends it:

- the event log is immutable-append; derived tables are rebuilt from it;
- survival analysis treats open items as *right-censored* at each analysis
  `as_of` date, so metrics stay correct as items close over time;
- the classifier is versioned (`classifier_version`), so a new classifier
  version can be re-run over re-fetched raw data and produce a new column of
  labels without mutating history;
- snapshots carry an `as_of` watermark so "what did we know on day T" is
  reproducible.

## Failure modes the design must survive

- **Classifier drift** — taxonomy versioning; `other` bucket queues cases for
  taxonomy extension rather than free-text fallback.
- **LLM over-eagerness** — constrained decoding + schema rejection at parse time
  (a label outside the set is dropped, not stored).
- **Identity churn** — people leave/rename; pseudonyms are stable per person via
  the raw-zone resolution table.
- **Partial/duplicate ingest** — resumable scrapers + cursor/watermark +
  idempotency-by-natural-key so a re-run never double-counts events.

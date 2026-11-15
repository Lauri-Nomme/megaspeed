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
  ┌─────────────────────────┐   ┌────────────────────┐   ┌──────────────────────┐
  │ primary store            │──►│ analysis (SQL)      │──►│ reports               │
  │ DuckDB + Parquet (OCEL)  │   │ + optional open-    │   │ flow eff, blocking     │
  │ no free-text columns     │   │   source LLM over   │   │ graph, daily "next"    │
  └─────────────────────────┘   │   the labels only    │   └──────────────────────┘
                                └────────────────────┘
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
| Scrapers | small per-source CLI (Python), rate-limited, resumable | simple, auditable, run in the raw zone only |

## The boundary, enforced three ways

1. **Storage** — raw and pseudonymised live in physically separate paths; the
   pseudonymised store has no free-text content columns (schema-enforced).
2. **Network** — the raw zone has credentials to Jira/Bitbucket/Slack; the
   analysis zone has **none** and cannot reach the raw zone.
3. **Policy** — raw staging auto-deletes (TTL); the pseudonymised store is the
   only thing that may be backed up, shared, or handed to an LLM.

The classifier emits, alongside every event, `classifier_version` and a content
hash, so outputs are reproducible and tunable without retaining raw text.

## Deployment target

One machine (or one container) for the raw zone, one for the pseudonymised
zone. No multi-tenant, no external services in v1. This is a single-team,
single-company, self-hosted tool.

## Failure modes the design must survive

- **Classifier drift** — taxonomy versioning; `other` bucket queues cases for
  taxonomy extension rather than free-text fallback.
- **LLM over-eagerness** — constrained decoding + schema rejection at parse time
  (a label outside the set is dropped, not stored).
- **Identity churn** — people leave/rename; pseudonyms are stable per person via
  the raw-zone resolution table.
- **Partial scrapes** — resumable scrapers + `as_of` watermark so a re-run
  doesn't double-count events.

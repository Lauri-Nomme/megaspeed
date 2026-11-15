# PLAN — megaspeed implementation

This is the working implementation plan. Design rationale lives in `prd/`.
State/progress lives in `STATE.md`.

## Stack (decided)

| Layer | Choice | Note |
| --- | --- | --- |
| Frontend | **SvelteKit (TS)** + canvas/WebGL core | custom Canvas 2D for timeline/gantt, sigma.js for the blocking graph, uPlot for time-series. Dark graphite/navy per `prd/10-ux-look-feel.md` |
| Backend | **Python + FastAPI (uvicorn)** | read-only query API over DuckDB + WebSocket |
| Store | **DuckDB** (+ Parquet) | schema is the pseudonymisation contract: no free-text content columns |
| Ingest | **Python**, per-source | Bitbucket DC first (see below) |
| Harness | mosaic-style **eval/screenshot bridge** | `/ws` + `POST /api/eval` + `POST /api/screenshot`, curl-drivable, for fast UI iteration |
| Deploy | **nginx** via `megaspeed.example.com` | reverse proxy to the local dev server |

## Repository layout (target)

```
megaspeed/
  prd/                    # design docs (read-only for implementation)
  PLAN.md, STATE.md
  megaspeed.example.com     # nginx site config
  ingest/                 # Python: Bitbucket DC -> DuckDB (pseudonymised)
  server/                 # Python: FastAPI + WS + eval/screenshot harness
  app/                    # SvelteKit: timeline UI (#1), then blocking/labs
  fixtures/               # synthetic Bitbucket data (no creds needed)
```

## First deliverable (this phase)

1. **Bitbucket import** — PRs, commits, and PR activities/events, **filtered by
   Jira ticket keys** (key prefixes found in branch names, commit messages, PR
   title/description). Filter regex is configurable. Includes a **fixture mode**
   so the whole pipeline runs end-to-end without credentials.
2. **Web UI** — the Timeline view (#1 output): LTR events, actor/object/queue
   swimlanes, gantt-style bars, zoom, dark graphite/navy per the UX spec.
3. **Harness** — the mosaic-style eval/screenshot bridge so I can drive the
   browser via `curl` and iterate on the UI by screenshot.

## Pseudonymisation contract (carried from `prd/02`)

The DuckDB store may contain only **ticket keys, PR ids, commit hashes,
pseudonymous actor ids, timestamps, and enumerated activity labels**. Raw
content (commit messages, PR titles/descriptions, diffs) is read locally
**only** to extract the ticket key for filtering, then discarded — never
stored, never sent to any LLM. No free-text content columns in the schema.

## Ingest scope for the first slice

- Bitbucket **Data Center** REST: PR list + `/activities` stream (OPENED,
  RESCOPED, COMMENTED, APPROVED, UNAPPROVED, NEEDS_WORK, MERGED, DECLINED,
  reviewer changes), PR metadata, commit list per PR.
- Ticket-key filter: configurable regex (default `\b[A-Z]+-\d+\b`), applied to
  branch name, commit messages, PR title/description. Only PRs (and their
  commits/events) matching any key are kept. This gives focus for testing.
- Identity: map Bitbucket actor slug → pseudonym (raw-zone resolution table;
  store pseudonym only).

## Execution model

- **Code work is delegated to subagents**, not written here.
  - Primary model: `opencode/deepseek-v4.1-flash-free`.
  - Fallback (if exhausted): `opencode-go/deepseek-v4.1-flash`.
- I drive the UI via the harness (`curl` → `/api/eval` / `/api/screenshot`), not
  by reading code by hand.

## Phase order (from `prd/07-milestones.md`, first deliverable only)

- **S0 (done)**: git repo at `megaspeed/`, history retained.
- **S1 — Bitbucket import + walking skeleton**: ingest (fixture-first) →
  DuckDB; FastAPI `/api/events`; SvelteKit timeline rendering from it; harness.
- **S2+**: Jira Cloud + Tempo, identity/calendar, classifier + pseudonymisation
  gate, then the remaining views (blocking map, queue, delivery list, policy lab).

## Deploy

- `megaspeed.example.com`: nginx `upstream megaspeed { … }` → local dev server;
  `server_name megaspeed.example.com`; TLS via `example.com` wildcard; WebSocket
  upgrade headers (required for `/ws` and SvelteKit HMR).
- Fill in the dev-server host:port when the server actually runs (see `STATE.md`).

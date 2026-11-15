# megaspeed — developer guide

Walking skeleton of the process-mining tool: **Bitbucket import (ticket-key
filtered) → pseudonymised DuckDB store → read-only API + WebSocket eval/screenshot
bridge → dark canvas timeline.**

Everything below runs locally against fixtures. No credentials, no build step,
no Node.

```
fixtures / Bitbucket DC ──► ingest/ ──► data/megaspeed.duckdb ──► server/ ──► app/
   (raw zone: read + discard)      (pseudonymised: keys, pseudonyms, labels)   (read-only)
```

---

## 1. Quickstart

```bash
cd megaspeed
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python -m ingest --fixture --n 50      # -> data/megaspeed.duckdb
.venv/bin/python -m server                       # -> http://127.0.0.1:8766
```

Open <http://127.0.0.1:8766/>.

The server binds **127.0.0.1:8766** because the nginx site
`megaspeed.example.com` proxies to `127.0.0.1:8766` (`megaspeed.example.com`,
`upstream megaspeed`). Override with `--host` / `--port` or
`MEGASPEED_HOST` / `MEGASPEED_PORT`.

Requirements file: `duckdb`, `fastapi`, `uvicorn`, `websockets`. The last one is
explicit because uvicorn does **not** bundle a WebSocket implementation, and
without it `/ws` silently degrades to a 404.

---

## 2. Ingest

```bash
# fixture (default mode) — generate 50 PRs in memory, filter, store
.venv/bin/python -m ingest --fixture --n 50

# replay a committed bundle instead of generating
.venv/bin/python -m ingest --fixture-file fixtures/bitbucket-dc-fixture.json

# regenerate the committed bundle (deterministic, seed 20261115)
.venv/bin/python -m ingest --write-fixtures --n 50

# re-verify the contract of an existing store
.venv/bin/python -m ingest --verify

# machine-readable
.venv/bin/python -m ingest --fixture --n 50 --json
```

Output:

```
PRs            50 seen -> 43 kept, 7 dropped (no ticket key)
events         420 emitted, 420 new (idempotent by natural key)
key found in   branch=15, commit=7, description=7, title=14
contract       OK — no free text, no canaries
```

### The ticket-key filter

`ingest/config.toml`:

```toml
[filter]
ticket_regex = '\b[A-Z]+-\d+\b'
key_shape    = '^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$'
```

`ticket_regex` is matched against the source **branch name**, **PR title**, **PR
description** and every **commit message**. Only PRs with at least one match are
kept; their commits/events come along. `key_shape` is a second, non-configurable-
in-spirit gate: a match that does not look like a ticket key is dropped, so a
sloppy regex cannot smuggle prose into the `ticket_keys` column.

Override at runtime: `MEGASPEED_TICKET_REGEX='\bDYNA-\d+\b'`.

### Incremental & idempotent

Every event has a stable natural key (`project/repo#pr:activity-id`), declared as
the primary key, and inserts are `INSERT OR IGNORE`. Re-running the pipeline
prints `0 new` and changes nothing. Each row carries `as_of` (the ingest
watermark) and each run appends to `ingest_runs` with `source_watermark` (newest
PR `updatedDate` seen), so "what did we know on day T" is answerable.

### Live Bitbucket Data Center

The client is written against DC 9.x REST (see `ingest/bitbucket.py`,
`ingest/transport.py`); creds come from the environment only:

```bash
export MEGASPEED_BB_BASE_URL=http://127.0.0.1:7990
export MEGASPEED_BB_USER=admin
export MEGASPEED_BB_PASSWORD=admin-lab-pw

# read-only diagnostics: auth, paging, shapes, filter decisions, nothing written
.venv/bin/python -m ingest --live --repo FIX/golden --probe

# ... plus map the reducer over the corpus in memory (no store write)
.venv/bin/python -m ingest --live --repo FIX/golden --probe --probe-bypass-filter

# real ingest into a separate store
MEGASPEED_DB=data/live.duckdb .venv/bin/python -m ingest --live --repo FIX/golden
```

`--probe` on the lab corpus prints 10 PRs / 18 commits / 0 matched by the ticket
filter (that corpus has no Jira keys, by design), and with
`--probe-bypass-filter` maps 48 events in memory. Nothing is written and the
identity map is not saved. **Fixture mode stays the default path.**

`MEGASPEED_BB_TOKEN` (bearer) is preferred over user/password when available.

---

## 3. Store schema (`data/megaspeed.duckdb`)

DDL lives in `ingest/schema.sql` — read it as the pseudonymisation contract.

| Table | Columns | Notes |
| --- | --- | --- |
| `events` | `event_key` PK, `ts`, `activity`, `actor`, `repo`, `pr_id`, `attributes` (JSON), `classifier_version`, `content_hash`, `as_of` | one row per OCEL-ish event |
| `event_objects` | `event_key`, `object_type`, `object_id` | event ↔ object relation (OCEL) |
| `objects` | `object_type`, `object_id`, `repo`, `pr_id`, `ticket_keys` (array), `actor`, `state`, `created_at`, `closed_at`, `head_commit`, `message_class` | `PR` / `COMMIT` / `TICKET` / `PERSON` |
| `ingest_runs` | `run_id`, `source`, `as_of`, `source_watermark`, `classifier_version`, `prs_seen`, `prs_kept`, `prs_dropped`, `events_written`, `events_skipped` | provenance per run |

There is **no** column for a title, description, branch name, commit message,
comment body, diff or free-text summary. `attributes` holds only keys from
`ingest/taxonomy.py` with enumerated or numeric values.

### How the guarantee is enforced

1. **Allowlist** (`ingest/store.py:ALLOWED`) — an unknown table or column fails
   `verify()`, and ingest runs `verify()` before exiting successfully.
2. **Shape** — no stored string may contain whitespace (prose does, identifiers
   do not); arrays are checked element-wise.
3. **Canary** — every human-authored fixture field contains a `zzcanary…` token;
   if one appears anywhere in the store, ingest fails.

```bash
.venv/bin/python -m ingest --verify     # exit 1 + the offending column if broken
.venv/bin/python -m unittest discover -s tests -v
```

`tests/test_contract.py` covers the pseudonym scheme, collision suffixes, the
filter's second gate, message classification, idempotency and the canary.

---

## 4. API

```bash
curl -s localhost:8766/api/health | jq
curl -s 'localhost:8766/api/events?from=2026-10-01&to=2026-10-31' | jq '.[0]'
curl -s 'localhost:8766/api/objects?type=PR' | jq '.[0]'
```

`GET /api/events?from=&to=` → list of

```json
{"key":"ENG/platform#110:act:110016","ts":"2026-09-01T09:42:00Z",
 "activity":"pr_opened","actor":"ToBr",
 "objects":["PR:ENG/platform#110","TICKET:PROJ-105"],
 "attributes":{},"repo":"ENG/platform","pr_id":110,"as_of":"..."}
```

`GET /api/health` → `{"ok":true,"store":"…","events":420,"prs":43,…}` (503 with a
hint if no store). `GET /` serves `app/` with no build step.

### WebSocket harness (`/ws`)

Mirrors the mosaic protocol so the same muscle memory (and curl) works:

| Direction | Message |
| --- | --- |
| server → browser | `{"event":"hello"}` on connect |
| server → browser | `{"event":"changed"}` when the store file changes |
| server → browser | `{"eval": <id>, "code": "…"}` |
| browser → server | `{"evalres": <id>, "ok": true, "data": …}` |
| browser → server | `{"evalchunk": {"id":…,"i":…,"n":…,"data":"…"}}` for big results |
| browser → server | `{"ping": <ts>}` → `{"pong": <ts>}` |

`POST /api/eval` runs JS in the connected browser:

```bash
curl -s -X POST localhost:8766/api/eval -H 'content-type: application/json' \
  -d '{"code":"window.__megaspeed.state.events.length"}'
# {"ok":true,"data":420,"error":null}
```

Code is evaluated as a console expression (completion value is returned) and may
return a promise; top-level `await` falls back to an async function body, where
a value needs an explicit `return`. With no browser attached:

```bash
curl -s -X POST localhost:8766/api/eval -H 'content-type: application/json' -d '{"code":"1+1"}'
# {"ok":false,"data":null,"error":"no browser connected"}
```

`POST /api/screenshot` captures a selector and returns base64 PNG, or writes it:

```bash
curl -s -X POST localhost:8766/api/screenshot -H 'content-type: application/json' \
  -d '{"selector":".app","out":"/tmp/opencode/megaspeed.png"}'
# {"ok":true,"selector":".app","out":"/tmp/opencode/megaspeed.png","bytes":256169,"mime":"image/png"}

curl -s -X POST localhost:8766/api/screenshot -H 'content-type: application/json' \
  -d '{"selector":"#timeline"}' | jq -r .data | base64 -d > /tmp/opencode/canvas.png
```

The page implements `window.__megaspeedShot(selector)`:

- **default**: clone the element, swap `<canvas>` for an `<img>` of its bitmap,
  inline `style.css`, render through an SVG `foreignObject` → real DOM
  screenshot (chrome, legend, tooltip included), and
- **fallback**: composite the canvas bitmap into the element's box.

Chunking kicks in above ~150 kB so a ~250 kB PNG survives one WebSocket.

Any connected browser works — including a plain headless Chromium pointed at the
page, which is what makes the harness CI-friendly:

```bash
chrome --headless=new --no-sandbox --window-size=1600,1200 http://127.0.0.1:8766/
```

---

## 5. App

`app/` is vanilla ES modules + one `<canvas>`. No npm, no bundler, no framework
(SvelteKit is a later shell).

- LTR time axis, swimlanes switchable between **actors** / **PRs** / **activities**.
- Event markers coloured by activity; hover shows `ts, activity, actor,
  objects, attributes` — pseudonyms and keys only.
- **PR lifecycle bars**: gantt-style segments (`in review`, `waiting on author`,
  `closed`), with event ticks and a right-censor arrow for open PRs.
- **Zoom/pan**: wheel to zoom at the cursor, drag to pan, `+ / − / Fit`,
  double-click to zoom in; `Shift`+wheel pans.
- **Level of detail**: when several events land in one pixel column they collapse
  into a density bar, and the footer says at what bucket size. Weekend/night
  shading appears when a day is wide enough to read.
- Density `S / M / L`, clickable activity legend (hide/show), provenance and
  honesty lines from `/api/health` (`as_of`, counts, dropped PRs, censoring).

`window.__megaspeed` exposes `{timeline, state, refresh, setLanes, setDensity}`
for the eval bridge, e.g.:

```bash
curl -s -X POST localhost:8766/api/eval -H 'content-type: application/json' -d '{
  "code":"const t=window.__megaspeed.timeline; t.view={start:Date.parse(\"2026-10-05\"),end:Date.parse(\"2026-10-12\")}; t.schedule(); \"ok\""}'
```

---

## 6. Layout

```
megaspeed/
  ingest/      Bitbucket DC client + transport (fixture | http), ticket filter,
               pseudonym identity table, reducer, DuckDB store + contract guard
  server/      FastAPI read API, /ws harness bridge, static app
  app/         index.html, style.css, main.js, timeline.js (canvas)
  fixtures/    synthetic DC bundle + generator notes
  tests/       the pseudonymisation gate as unittest
  data/        *.duckdb (gitignored)
  var/         identity_map.json — RAW ZONE, gitignored, never served
  docs/DEV.md  this file
```

---

## 7. Deviations & notes

- **No SvelteKit / no npm build** in this slice: `app/` is plain JS + canvas,
  served by FastAPI. Deliberate — the timeline core is the risky part and it has
  no framework dependency. The SvelteKit shell can host the same renderer later.
- **DuckDB is the store** (not sqlite); FastAPI + uvicorn are used as specified.
- **`requests` is not a dependency**: the live transport is stdlib `urllib`
  (basic/bearer auth, retries). Fixture mode never touches the network stack.
- **`push_class`** is `other` in live mode until a local clone + `git patch-id`
  is wired. Fixtures *simulate* the classifier output (see
  `fixtures/README.md`); no raw message is ever used to derive the label.
- **DC `REVIEWED` / `UPDATED` activities are ignored** — they are outside the
  closed vocabulary. `--probe` counts them as `ignored` so taxonomy gaps are
  visible rather than silent.
- **Branch names and repo slugs**: branch names are read for the filter and
  discarded; only the matched keys survive. Repo slugs (`ENG/platform`) are kept
  as system identifiers, like PR ids.
- **Screenshots need a browser**: `/api/screenshot` returns
  `{"ok": false, "error": "no browser connected"}` without one. That is the
  correct CI behaviour, not a failure.
- **Pseudonym map** (`var/identity_map.json`) is raw-zone data: gitignored,
  never served, never opened by `server/`. Delete it and re-ingest to rebuild.

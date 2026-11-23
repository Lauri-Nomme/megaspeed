# megaspeed — developer guide

Walking skeleton of the process-mining tool: **Bitbucket import (ticket-key
filtered) → pseudonymised DuckDB store → read-only API + WebSocket eval/screenshot
bridge → dark canvas timeline.**

The default path runs locally against fixtures; the live path reads the
`DEL` corpus from the licensed Bitbucket DC lab on `:7991`. No build step, no
Node.

```
fixtures / Bitbucket DC ──► ingest/ ──► data/megaspeed.duckdb  (fixture)
   (raw zone: read + discard)      │     data/del.duckdb        (live DEL)
                                   └──► server/ ──► app/ (read-only)
                                      pseudonymised: keys, pseudonyms, labels
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
`MEGASPEED_HOST` / `MEGASPEED_PORT`. Through nginx the app is
<https://megaspeed.example.com/>.

The default store is the fixture (`data/megaspeed.duckdb`). The real DEL corpus
lives in `data/del.duckdb`; point the server at it with `MEGASPEED_DB`:

```bash
# foreground
MEGASPEED_DB=data/del.duckdb .venv/bin/python -m server

# the nginx-facing instance: persistent, screenshot sandbox under /tmp/opencode
MEGASPEED_DB=data/del.duckdb \
MEGASPEED_SHOT_DIR=/tmp/opencode/megaspeed-shots \
  nohup .venv/bin/python -m server >/tmp/opencode/megaspeed-8766.log 2>&1 &

# restart it
fuser -k 8766/tcp; sleep 2
MEGASPEED_DB=data/del.duckdb MEGASPEED_SHOT_DIR=/tmp/opencode/megaspeed-shots \
  nohup .venv/bin/python -m server >/tmp/opencode/megaspeed-8766.log 2>&1 &
curl -s localhost:8766/api/health | jq '.events, .prs, .source'
```

Attach the harness browser once:

```bash
nohup .venv/bin/python harness/browser.py >/tmp/opencode/megaspeed-browser.log 2>&1 &
until curl -s localhost:8766/api/health | grep -q '"browsers":[1-9]'; do sleep 1; done
```

It reconnects `/ws` on its own and reloads when the socket stays down, but a
server restart that switches `MEGASPEED_DB` can leave the tab showing the
previous store until you force one reload:

```bash
curl -s -X POST localhost:8766/api/eval -H 'content-type: application/json' \
  -d '{"code":"location.reload()"}'
```

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
events         417 emitted, 417 new, 0 rewritten, 0 re-derived
objects        435
key found in   branch=15, commit=7, description=7, title=14
contract       OK — no free text, no canaries
```

`0 rewritten` / `0 re-derived` are the incremental counters: a re-run after a
classifier bump can rewrite existing events, and `re-derived` counts events whose
attributes changed because a later activity answered an earlier question.

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
`ingest/transport.py`); creds come from the environment only. There are two
local DC 9.4.18 labs:

| port | lab | corpus | ticket keys |
| --- | --- | --- | --- |
| **7990** | `bb-lab-a` | `FIX/golden` | none, by design |
| **7991** | `bb-lab-b` (licensed) | `DEL` (`delivery-api`, `delivery-web`) | `DEL-*` |

`7990` only exercises transport, paging and reducer mechanics; it is not the DEL
corpus. The real integration target is **7991**:

```bash
export MEGASPEED_BB_BASE_URL=http://127.0.0.1:7991
export MEGASPEED_BB_USER=admin
export MEGASPEED_BB_PASSWORD=admin-lab-pw
export MEGASPEED_TICKET_PROJECTS=DEL
export MEGASPEED_DB=data/del.duckdb
export MEGASPEED_IDENTITY=data/identity.json   # persistent raw zone, git-ignored

# real ingest of the DEL corpus (131 PRs -> 1316 events)
.venv/bin/python -m ingest --live --repo DEL/delivery-api --repo DEL/delivery-web

# re-verify the contract; --verify also reads MEGASPEED_TICKET_PROJECTS
.venv/bin/python -m ingest --verify
```

Expected DEL run: `131 seen -> 131 kept, 0 dropped`, `1316 events`, `1415`
object rows counted by the reducer (`1248` unique rows in `objects`), and
`contract OK`. The identity map is written once and reused, so pseudonyms stay
stable across runs.

`MEGASPEED_TICKET_PROJECTS` is required for any live run: the committed default
is the fixture project set (`PROJ,OPS,ARCH`), and without `DEL` every DEL PR is
dropped. It also gates `--verify`, which checks every stored ticket key against
the configured project prefixes.

Read-only diagnostics against either lab (nothing written, identity map not
saved):

```bash
# FIX/golden mechanics: 10 PRs / 18 commits / 0 matched by the ticket filter
.venv/bin/python -m ingest --live --repo FIX/golden --probe

# ... plus map the reducer over the corpus in memory (48 events on FIX/golden)
.venv/bin/python -m ingest --live --repo FIX/golden --probe --probe-bypass-filter

# DEL shapes and activity counts
.venv/bin/python -m ingest --live --repo DEL/delivery-api --repo DEL/delivery-web --probe
```

**Fixture mode stays the default path.**

`MEGASPEED_BB_TOKEN` (bearer) is preferred over user/password when available.

### Text labels: raw-zone trace and label-only relabel

`--hans-scrape` / `--live` runs classify comment prose with the local model and
write **every** call to `<state-dir>/classify-trace.jsonl` (raw zone: the full
text, request body, response including thinking, timings, and the PR/thread/
comment ids). It never enters the bundle, which carries labels only.

Re-determine the labels later, without re-walking git, and import the result as
a patch:

```bash
# replay the trace (or a capture that predates the trace) through a model
.venv/bin/python -m ingest relabel --trace export/hans.state/classify-trace.jsonl \
    --bundle export/hans.tgz --out export/hans-labels.tgz \
    --llm-url http://127.0.0.1:11434 --llm-model qwen3:8b --no-llm-think

# ... or, for a bundle whose scrape ran before the trace existed:
.venv/bin/python -m ingest relabel --hans-scrape ~/dev/hans/scrape/<capture> \
    --bundle export/hans.tgz --out export/hans-labels.tgz \
    --llm-url http://127.0.0.1:11434 --llm-model qwen3:8b

# apply it (auto-detected; THREAD labels + question phases only, idempotent)
.venv/bin/python -m ingest import export/hans-labels.tgz --db data/demo.duckdb
```

Thinking is on by default (the trace is a diagnostic record); `--no-llm-think`
trades it for throughput. Every call prints one timestamped line with the label
or the full error.

On a CPU box, batch and resume: `--batch-size 8` labels eight same-kind texts per
request, and labels are checkpointed to `<out>.labels.jsonl` after every batch.
`Ctrl-C`, a reboot, or `touch <out>.stop` stops cleanly; re-run the same command
to continue where it left off. Relabel runs up to `--llm-workers` requests in
flight (the container needs `OLLAMA_NUM_PARALLEL` for that to help).

Identity maps are per tenant and must be shared across sources: scraping Jira on
one machine and Bitbucket on another produces two maps, and a person can end up
with two pseudonyms (`LaNo` from `"Lauri Nomme"`, `NoLa` from `"Nomme, Lauri"`).
Copy one map to the other side and pass `--identity`. Every model call is also written to
`<out>.trace.jsonl` (full request/response incl. thinking; `--no-call-trace`
disables it). That trace is raw zone — never transfer it.

### Jira sprint metadata refresh (`--sprints-only`)

The team's sprints are named `Sprint 352`-style; the scrape parses the number
into `sprint_no` (prd/70 F-16) and never stores the name. Refreshing that number
on an existing store does **not** need a full re-scrape (every issue, worklog and
changelog) for a handful of sprint rows:

```bash
# Data Center. Add --fixture to exercise it on the synthetic corpus.
.venv/bin/python -m ingest scrape --jira --sprints-only \
    --out export/demo-sprints.tgz
# Cloud: --jira-cloud --sprints-only, reading MEGASPEED_JIRA_CLOUD_BASE_URL
# (the local emulation proxy in dev). No --live needed: this is a direct read.

.venv/bin/python -m ingest import export/demo-sprints.tgz --db data/demo.duckdb
```

The tgz holds **SPRINT objects only** — no events, no issues, worklogs or
changelogs — with `manifest.sprint_refresh: true` and `watermark_ts: null`. The
scrape reads the board and its sprint list (two requests per project), reduces
them through the same reducer as a full scrape (so the rows and `sprint_no` are
identical), needs no identity map or pepper (a SPRINT carries no actor) and
writes no state (no watermark, no issue/sprint cursor). Import upserts the rows,
runs the same `--verify` contract and reports `mode: sprint-refresh`; it
deliberately does **not** write an `ingest_runs` row or move
`source_watermark`, so the next full scrape's cursor is unchanged. Re-running it
is harmless.

---

## 3. Store schema (`data/megaspeed.duckdb`)

DDL lives in `ingest/schema.sql` — read it as the pseudonymisation contract.

| Table | Columns | Notes |
| --- | --- | --- |
| `events` | `event_key` PK, `ts`, `activity`, `actor`, `repo`, `pr_id`, `attributes` (JSON), `classifier_version`, `content_hash`, `as_of` | one row per OCEL-ish event |
| `event_objects` | `event_key`, `object_type`, `object_id` | event ↔ object relation (OCEL) |
| `objects` | `object_type`, `object_id`, `repo`, `pr_id`, `ticket_keys` (array), `actor`, `state`, `created_at`, `closed_at`, `head_commit`, `message_class`, `attributes` (JSON) | `PR` / `COMMIT` / `TICKET` / `PERSON` / `PRREV` / `THREAD` |
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

| Endpoint | What it returns |
| --- | --- |
| `GET /api/health` | store status + provenance (`events`, `prs`, `commits`, `as_of`, `source`, `prs_seen/kept/dropped`, `browsers`) |
| `GET /api/events` | a page of the pseudonymised event log |
| `GET /api/events/counts` | histogram + window totals, no event rows |
| `GET /api/objects` | a page of `PR` / `COMMIT` / `TICKET` / `PERSON` / `PRREV` / `THREAD` objects |
| `GET /api/blocking` | weighted directed handover graph (`level=queue\|person`, `days=`) |
| `GET /api/queues` | per-person / per-queue WIP, arrivals and completions series |
| `GET /api/delivery` | triage list of open PRs, what they wait on and for how long |

`GET /api/events?from=&to=&repo=&actor=&activity=&ticket=&pr_id=&limit=&offset=&order=`
→ list of

```json
{"key":"DEL/delivery-api#53:act:530016","ts":"2026-10-20T13:55:53Z",
 "activity":"pr_opened","actor":"AlNg",
 "objects":["PR:DEL/delivery-api#53","TICKET:DEL-1","TICKET:DEL-167"],
 "attributes":{},"repo":"DEL/delivery-api","pr_id":53,"as_of":"..."}
```

Filters are applied in SQL and are never silently ignored: an unknown query
parameter is a **400** that lists the valid ones, and an `activity` outside the
closed vocabulary is a 400 too.

**Paging.** `limit` + `offset` page the log (`limit` 1–200000); `order=asc|desc`
orders by `ts` (default `asc`). Every paged response carries
`X-Total-Count` (matching rows before paging), `X-Limit` and `X-Offset`, so a
client never downloads the log to count it. `/api/objects` pages the same way.

**Date-only `to` is inclusive.** `from=2026-08-17&to=2026-11-15` includes all of
15 Nov; the server turns that upper bound into the last representable naive UTC
timestamp of the day. An ISO datetime is used exactly, e.g.
`to=2026-11-15T12:00:00Z`.

**`GET /api/events/counts?granularity=hour|day|week`** is the cheap window
sizing call: UTC-aligned `buckets` (Monday-aligned for `week`), `total`,
`by_activity`, `by_repo`, `first_ts`, `last_ts`, and the closed
`activity_vocabulary`. It accepts the same `from/to/repo/actor/activity/ticket/pr_id`
filters. The UI uses it for the honesty line and the histogram before it pages
event rows.

`GET /api/health` over the live DEL store looks like:

```json
{"ok":true,"store":"…/data/del.duckdb","events":1316,"prs":131,"commits":467,
 "as_of":"2026-11-15T17:46:40Z","source":"bitbucket-dc",
 "prs_seen":131,"prs_kept":131,"prs_dropped":0,"browsers":1}
```

A missing store is a 503 with a hint; a store held by an ingest run is a
retryable 503 (`{"retryable":true}` + `Retry-After`), which the UI waits out.
`GET /` serves `app/` with no build step.

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
# {"ok":true,"data":1316,"error":null}
```

Code is evaluated as a console expression (completion value is returned) and may
return a promise; top-level `await` falls back to an async function body, where
a value needs an explicit `return`. With no browser attached:

```bash
curl -s -X POST localhost:8766/api/eval -H 'content-type: application/json' -d '{"code":"1+1"}'
# {"ok":false,"data":null,"error":"no browser connected"}
```

`POST /api/screenshot` captures a selector and returns base64 PNG, or writes it
to disk. **`out` is sandboxed**: writes are only allowed inside
`MEGASPEED_SHOT_DIR` (default `<tmp>/megaspeed-shots`, i.e.
`/tmp/megaspeed-shots`; the live 8766 instance sets it to
`/tmp/opencode/megaspeed-shots`). A bare file name is resolved inside that
directory; an absolute path outside it, or a `..` that escapes it, is a **400**.
The response's `out` holds the resolved path.

```bash
# bare name -> inside MEGASPEED_SHOT_DIR
curl -s -X POST localhost:8766/api/screenshot -H 'content-type: application/json' \
  -d '{"selector":".app","out":"del-timeline.png"}'
# {"ok":true,"selector":".app","out":"/tmp/opencode/megaspeed-shots/del-timeline.png","bytes":559384,"mime":"image/png"}

# no out: return base64 only
curl -s -X POST localhost:8766/api/screenshot -H 'content-type: application/json' \
  -d '{"selector":"#timeline"}' | jq -r .data | base64 -d > /tmp/opencode/canvas.png
```

If the server runs with a different `MEGASPEED_SHOT_DIR`, put screenshots there
(or read the `out` path the response reports). The default sandbox is deliberate:
`/api/screenshot` is curl-drivable and must not be able to write anywhere on the
host.

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

Views in the sidebar: **Overview**, **Delivery list**, **Timeline**, **Blocking
map**, **Queue**, **Policy lab** (stub) and **Sources** (provenance). The scope
selector (person) and timeframe (`1w / 4w / 1q / all`) apply across views.

- LTR time axis, swimlanes switchable between **actors** / **PRs** / **queues**
  (`actors` is the default; DEL renders 7 actor lanes, the bot actor included).
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
- **Delivery list** tabs are scoped counts (`Blocked` / `Oldest waiting` / `All`):
  the tab numbers follow the selected person, and the table shows what each open
  PR waits on, for how long, and its `≈` impact.
- **Blocking map** is the weighted handover graph (`hours` / `waiting on others` /
  `WIP now` / `wait/day` / `mean wait` lenses; queue or person level).
- **Queue** is per-person/queue WIP small multiples with arrivals, completions
  and an overloaded/slow diagnosis; the timeline is right-censored at the
  snapshot.

`window.__megaspeed` exposes `{timeline, blockingMap, queueChart, state,
refresh, setPage, setScope, setTimeframe, setLanes, setDensity, stamp}` for the
eval bridge, e.g.:

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
  analysis/    ballcourt.py — read-only interval reconstruction behind /api/blocking,
               /api/queues and /api/delivery
  server/      FastAPI read API, /ws harness bridge, static app
  app/         index.html, style.css, main.js, tables.js, graph.js, queues.js,
               timeline.js, format.js
  harness/     browser.py — persistent headless browser for /api/eval + /api/screenshot
  corpus/      DEL corpus generator / backdater / verifier for the Bitbucket lab
  fixtures/    synthetic DC bundle + generator notes
  tests/       the pseudonymisation gate as unittest
  data/        *.duckdb + identity.json (gitignored)
  var/         identity_map.json fallback location — RAW ZONE, gitignored, never served
  docs/DEV.md  this file
```

The live DEL store is `data/del.duckdb` with its identity map at
`data/identity.json`; both stay out of git.

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
- **DC `UPDATED` / `REVIEWED` activities are handled**, not ignored. `UPDATED`
  carries `addedReviewers` / `removedReviewers` and becomes
  `reviewer_changed {change: added|removed}`; a reviewer-only `UPDATED` never
  starts a review round, but a rescope-shaped `UPDATED` is treated as a push.
  `REVIEWED` (a reviewer completing a review) becomes
  `reviewer_changed {change: reviewed}` because the closed vocabulary has no
  `pr_reviewed` yet. Unknown actions are still ignored on purpose; `--probe`
  counts them as `ignored` so taxonomy gaps stay visible.
- **Known corpus gaps** (not ingest bugs): DEL never emits `removedReviewers`, so
  `reviewer_changed{change=removed}` has no live cases; `merge_commit` is absent
  because DEL's `MERGED` has no commit block; `THREAD` state is always `open`
  (the corpus has no resolutions); `sentinel=false` everywhere (DEL has no
  sentinel-team slugs).
- **Branch names and repo slugs**: branch names are read for the filter and
  discarded; only the matched keys survive. Repo slugs (`DEL/delivery-api`) are
  kept as system identifiers, like PR ids. The DEL store has 131 PRs, 1316
  events, 467 commits and 101 `DEL-*` tickets.
- **Screenshots need a browser**: `/api/screenshot` returns
  `{"ok": false, "error": "no browser connected"}` without one. That is the
  correct CI behaviour, not a failure. When a browser is attached, `out` must
  stay inside `MEGASPEED_SHOT_DIR` (see §4).
- **Pseudonym map** (`data/identity.json` for live DEL, `var/identity_map.json`
  by default) is raw-zone data: gitignored, never served, never opened by
  `server/`. Delete it and re-ingest to rebuild.

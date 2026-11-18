# Execution-performance review — megaspeed

Adversarial, measurement-first review of every layer. Read-only: no source file was
modified. All numbers below were produced by the scripts in `/tmp/opencode/perf/`
(re-runnable; see *Method*), against the real stores and against synthetic stores
scaled from them.

---

## Method

**Stores**

| store | events | PRs | stories | how |
|---|---|---|---|---|
| `data/del-combined.duckdb` | 2,529 | 131 | 100 | the real demo dataset |
| `stores/big10.duckdb` | 25,290 | 1,310 | 1,000 | 10 replicas of del-combined |
| `stores/big20.duckdb` | 50,580 | 2,620 | 2,000 | 20 replicas |
| `stores/big40.duckdb` | 101,160 | 5,240 | 4,000 | 40 replicas |

Replicas are produced by `make_big_store.py`, which rewrites every identifier into a
unique but *shape-valid* one (repo slug, `DEL-<n>`, `#<pr>`, sha, ref digest) so the
store keeps the same distributions, the same per-PR/per-story shape, and the same
pseudonymisation shapes. That isolates **size** from **data mix**.

**What was measured, and how**

* **In-process** (`bench_analysis.py`): every analysis entry point, median of 3 runs,
  with the real business-hours calendar attached (`config/calendar.default.yaml`).
* **HTTP** (`bench_api.py`, `bench_harness.py`): a second server on **port 8789**
  (`MEGASPEED_TENANTS_DIR=/tmp/opencode/perf/tenants`), cold = first request (which
  also builds the analysis cache), warm = median of the rest. The live server on 8766
  was never touched.
* **Browser** (`bench_ui.py`, `bench_ui_render.py`): headless chromium (the repo's own
  playwright build), 1600×1000, counting every response body the page receives and
  timing first paint, the background full load, each view, a full canvas render, and
  the rules drill-down.
* **Scrape** (`bench_scrape_checkpoint.py`, `bench_gitclass*.py`, `bench_classify.py`,
  `bench_jira_scrape.py`): the fixture transport through the real `ingest.scrape`
  pipeline; a real local bare git remote (100 branches × 3 commits) for the git cache;
  a local stub `/api/chat` with a fixed latency for the LLM path (the real box is not
  reachable from here, and its latency is a property of the box, not the code).
* **Import** (`bench_import.py`): real tgz bundles through `import_bundle`, timed by
  phase.
* **Query plans**: `EXPLAIN` + wall-clock for every hot read on the 101k-event store,
  plus an A/B with an ART index created on the side (a copy, never the source).

All timings are on the same machine, same process class; ratios matter more than
absolute milliseconds.

---

## Top findings (ranked)

| # | Sev | Layer | Problem | Evidence | Fix |
|---|---|---|---|---|---|
| 1 | **CRITICAL** | Scrape | Every checkpoint rewrites the **whole** bundle (gzip+tar of everything so far) → O(N²) scrape | 4,000 PRs: **45.0 s** with checkpoints vs **2.96 s** without (15.2×, and growing); 67% of the profile is `_write_bundle_atomic`; Jira has the same shape (2,000 stories: 4.32 s vs 0.63 s) | Checkpoint to append-only part files (or a time budget), build the tgz once at the end |
| 2 | **CRITICAL** | Analysis | `queue_series` samples WIP by looping **all buckets per interval** → O(intervals × buckets) | `/api/queues?bucket_hours=1`: **19.3 s** @1,310 PRs, **88.5 s** @5,240 PRs; default buckets already 3.0 s. Difference-array rewrite gives **byte-identical** output: 3,051→152 ms default, 78,070→497 ms at 1 h | Replace the inner bucket loop with a difference array + prefix sum |
| 3 | **CRITICAL** | UI | Timeline canvas height grows linearly with PRs/stories (no virtualization) until it exceeds Chrome's 65,535 px limit — then it **silently paints nothing** | 131 PRs → 2,317 px / 3.7 ms; 1,310 → 20,002 px / 213 ms; 5,240 → 78,930 px and **alpha=0 at every probed pixel**; story mode 4,000 stories → 174,354 px (a canvas that size cannot be painted at all) | Virtualize lanes to the viewport; cap/paginate the canvas height |
| 4 | **HIGH** | Analysis | `/api/insights` recomputes everything per request, with ~58× redundant calendar math and 4× redundant per-story cycle rebuilds | 3.5 s @5,240 PRs (cold **and** warm), 22.4 MB payload; 93,400 `business_hours` calls for **1,615** unique arguments; `_cycle` 4.0×/story. Memo prototype: **1,713 → 864 ms** (2.0×) | Memoize `business_hours` + `Cycle` per request; cache the computed payload by store stamp |
| 5 | **HIGH** | Server+UI | The browser downloads the **whole (capped) store** and the whole insights payload, uncompressed | 5,240 PRs: initial load **41.1 MB / 5.7 s** (14.1 MB events + 25.5 MB objects) + **21.4 MB** insights = **62.7 MB**; gzip takes 22.4 MB → **1.1 MB** (5%) and 35.2 MB → 2.0 MB — the server has no compression | Add gzip/brotli middleware; then make the drill-down lists lazy |
| 6 | **HIGH** | Scrape | The git cache spawns **26 git processes per PR** (30 with thread anchors) and **fetches per PR**; a warm cache does not reduce the per-PR work at all | 26.1 runs/PR cold **and** warm; 55–105 ms/PR with a local `file://` remote; 1 fetch/PR; 13 `cat-file`, 3 `merge-base`, 3 `diff`. Memoizing presence+merge-base: 14.1 runs/PR, **1.5×** | Memoize `_present`/`_merge_base`; batch the fetch per list page instead of per PR |
| 7 | **HIGH** | Scrape | LLM classification is strictly sequential, one TCP connection per comment, no memoization | max in-flight = **1**, 200 TCP connections for 200 comments, 51.6 ms/comment against a 50 ms stub (i.e. pure serialization); 8,000 comments at a realistic 2 s = **4.4 h** | Bounded thread pool (4–8) + `keep-alive` + content-hash memo |
| 8 | **HIGH** | Server | `/api/ticket/{key}` rebuilds the **entire** Jira layer per request (the ballcourt is cached, the Jira half is not) | 194–376 ms of a ~300 ms request @4,000 stories for an **11.7 KB** payload; `load_jira` = 344 ms (45,880 `json.loads`) | Cache `JiraData` + threads/bots/sizes next to `_analysis_cache`, same stamp key |
| 9 | **HIGH** | Server | Bulk `/api/events` looks up relations in 500-key `IN` chunks (202 round trips inside DuckDB) | `limit=200000` @101k events: **2.86 s / 35.2 MB**; the chunked relation lookup alone is **1.78 s** vs **0.30 s** as one `JOIN` (6×) | One `con.values(...)` + `JOIN event_objects` |
| 10 | MED | UI | One full timeline render is 138–460 ms at the event cap — every zoom/pan/resize frame | 40,000 events: 138–152 ms (actor), 460 ms (story); the draw calls themselves total ~17 ms, the rest is canvas allocation/clear of a 79k–174k px bitmap | Virtualize; draw only the visible viewport |
| 11 | MED | UI | The rules drill-down builds up to 200 rows **per rule** before the user opens anything | 7 rules → **1,400 `<tr>` + 1,400 links** in the DOM at first render (10,288 nodes total) | Build on `toggle`, or cap to the affected items |
| 12 | MED | Server | `/api/simulate` recomputes 6 replications from scratch for every scenario, and the UI sends one per knob change (no debounce) | 7.2 s @5,240 PRs (reps=8); 0.97 s even at reps=1; calibrate 432 ms is repeated on every cache miss | Debounce + cache calibration separately; show the baseline first |
| 13 | MED | Scrape | The reducer holds every event/object/relation in memory until the end of the run | +2.7 KB per event (n=1,000: +23 MB; n=4,000: +65 MB) → **~2–2.8 GB at 1M events**, plus a full serialized copy during each checkpoint | Stream/partition the bundle per repo, or spill to a local JSONL |
| 14 | MED | Harness | Screenshots stop working on a large tenant, with a confusing error | `.app` at 5,240 PRs is 80,709 px tall → the capture canvas is over the limit → `toDataURL` returns empty → `"browser returned an unexpected data URL"` in 51 ms; at 1,310 PRs the same shot is 4.5 MB / 706 ms | Capture the viewport by default, or tile the element |
| 15 | LOW | Analysis | `stack._depth` / `_descendants` recurse once per stack level with no cap | 300 → 10.4 ms, 600 → 27.9 ms, 900 → 70.8 ms (O(n²)), **1,500 → RecursionError** → HTTP 500 | Iterative DFS + explicit depth cap |
| 16 | LOW | Server | `objects` pagination re-sorts the table per page; the UI needs 14 pages | 36 ms/page at offset 50k; keyset pagination 8.8 ms (4×). An ART index **did not help** (36.9 vs 35.8 ms) | Keyset paging, or (better) stop fetching all objects |

---

## 1. Scrape

### 1.1 Checkpoint rewrites the whole bundle — CRITICAL

**Where** `ingest/scrape.py:1231` (`checkpoint()` → `_write_bundle_atomic`),
`ingest/bundle.py:162` (`write_bundle` → `_tamperproof_tar` → gzip),
called from `ingest/scrape.py:664` every `DEFAULT_CHECKPOINT_EVERY = 50` kept PRs
and at every repo boundary. `ingest/scrape_jira.py` has the identical pattern
(`_checkpoint` every 50 issues).

**Problem** Each checkpoint serializes **every event and object accumulated so far**
into JSONL, tars it, gzips it, and atomically renames it over the previous bundle. The
`_refresh(out)` call before it also rebuilds the whole object list (`_merge_objects`
over all objects). Total work is Σ over checkpoints of the bundle size → **O(N²)** in
events, with a gzip of the whole corpus every 50 PRs.

**Evidence** (`bench_scrape_checkpoint.py`, fixture transport, full scrape, only the
checkpoint frequency differs):

| PRs | events | checkpoint every 50 | no checkpoints | overhead | bundle |
|---|---|---|---|---|---|
| 500 | 4,145 | 1.05 s | 0.27 s | 3.9× | 284 KB |
| 1,000 | 8,369 | 3.20 s | 0.59 s | 5.5× | 569 KB |
| 2,000 | 16,804 | 11.85 s | 1.32 s | 9.0× | 1,138 KB |
| 4,000 | 33,775 | **45.00 s** | **2.96 s** | **15.2×** | 2,293 KB |

The overhead ratio doubles as N doubles — the signature of O(N²). `cProfile` at 2,000
PRs: 21.6 s total, of which `_write_bundle_atomic` 14.5 s (37 calls) and `zlib.compress`
6.0 s. The same test on the Jira side (cloned stories):

| stories | every 50 | none | overhead |
|---|---|---|---|
| 250 | 0.18 s | 0.07 s | 2.7× |
| 500 | 0.45 s | 0.13 s | 3.4× |
| 1,000 | 1.28 s | 0.27 s | 4.7× |
| 2,000 | 4.32 s | 0.63 s | 6.8× |

**Severity: CRITICAL.** A real DEL-sized corpus (thousands of PRs) spends the large
majority of its scrape wall time re-compressing the same data, and the cost grows with
the square of the corpus — the one shape that does not survive a bigger repo.

**Fix** (see §6.1).

### 1.2 REST fan-out and pagination — no problem found

`ingest/bitbucket.py` + `ingest/scrape.py` issue exactly:

| n | kept PRs | requests | per kept PR | by kind |
|---|---|---|---|---|
| 100 | 86 | 174 | 2.02 | activities 86, commits 86, list 2 |
| 400 | 343 | 690 | 2.01 | activities 343, commits 343, list 4 |

One list page per 100 PRs (the cheap pre-filter runs on list rows), then exactly two
detail calls per **kept** PR, and zero for filtered-out PRs. Pagination is
`start`/`nextPageStart` with `isLastPage`, plus an early stop when a whole page is
older than the watermark. Jira is 1.03 requests/story. **This is the right shape** — no
N+1, no re-fetch of unchanged PRs beyond the deliberate overlap window.

The only cost is the deliberate politeness delay (`min_interval=0.2` on live runs):
2 requests/PR × 0.2 s = 0.44 s/PR floor, i.e. ~7 min per 1,000 PRs. That is a policy
choice, not a defect; it is worth stating in the scrape report because it dominates the
live wall time once §1.1 is fixed.

### 1.3 Shallow git cache — HIGH

**Where** `ingest/gitclass.py`: `annotate` (patch-id / range-diff), `annotate_diff_size`
(numstat), `annotate_threads` (one diff per comment anchor), all called per PR from
`ingest/scrape.py:641-656`.

**Problem** Per PR the classifier runs ~26 `git` subprocesses and **one network fetch**,
and the warm cache only avoids re-downloading objects — it does not reduce the per-PR
process count at all.

**Evidence** (`bench_gitclass*.py`, real bare remote, 100 branches × 3 commits,
`file://` so the fetch is as cheap as it can be):

```
cold cache, n=100   11.5 s   115 ms/PR   2606 git runs (26.1/PR)  fetches 100
warm cache, n=100   10.6 s   106 ms/PR   2602 git runs (26.0/PR)  fetches 100
warm again, n=100   11.7 s   117 ms/PR   2602 git runs (26.0/PR)  fetches 100
```

Breakdown per PR: `cat-file` 13.0, `update-ref` 3.0, `merge-base` 3.0, `diff` 3.0,
`patch-id` 2.0, `fetch` 1.0, `range-diff` 1.0. With 5 review comments per PR
(`annotate_threads` on, i.e. the live path with text classification):
**30.2 runs/PR, 55 ms/PR**. Every `cat-file` and `merge-base` answer is recomputed for
the same sha within the same PR.

**Severity: HIGH.** Over a real smart-HTTP remote each `fetch` is a round trip
(~50–200 ms), so 1,000 PRs pay 1,000 round trips plus ~26,000 process spawns.

**Fix** (see §6.7).

### 1.4 LLM classification is serial — HIGH

**Where** `ingest/classify.py:519` (`OllamaClassifier.classify`), called per comment and
per description from `ingest/pipeline.py:649/668/689`.

**Problem** One blocking HTTP POST per text, a fresh TCP connection each time, no
batching, no parallelism, no memoization of identical bodies. The circuit breaker is a
good safety net, but it does nothing for the normal case.

**Evidence** (`bench_classify.py`, local stub `/api/chat` with a fixed 50 ms latency,
real client):

```
K=200 delay=50ms -> 10.33 s (51.6 ms/comment, ideal 50 ms)
max in-flight requests: 1   TCP connections opened: 200   requests: 200
```

Throughput is exactly 1/latency: the client adds ~3% overhead and zero concurrency. At
a realistic CPU-MoE latency of 2 s/comment, a 1,000-PR corpus with 8 comments each
(8,000 calls) is **4.4 hours** of pure serial waiting. The run is also fully blocked
while it waits (no overlap with the git cache work).

**Severity: HIGH.**

**Fix** (see §6.8).

### 1.5 Scrape memory — MED

**Where** `ingest/scrape.py` (`Collected` accumulator), `_refresh` copies.

**Evidence** current RSS delta attributable to the reducer (`/proc/self/statm`,
fixture transport, checkpoints off):

| n | events | ΔRSS | per event |
|---|---|---|---|
| 1,000 | 8,369 | +23 MB | 2.7 KB |
| 2,000 | 16,804 | +47 MB | 2.7 KB |
| 4,000 | 33,775 | +65 MB | 1.9 KB |

Linear, but at **2–2.7 KB/event**: 1M events ≈ 2–2.8 GB, plus the full JSONL copy and
the gzip output during each checkpoint. The bundle also exists as one `bytes` object
before it is written (`bundle._tamperproof_tar` builds the whole tar.gz in a
`BytesIO`), so peak memory is ~2× the serialized size on top of the Python objects.

**Severity: MED** (a 100k-event corpus is ~250 MB — fine; the ceiling is real but far
away).

### 1.6 `pepper.py`, `transport.py` — no problem found

HMAC peppering is per-value and negligible in the profile. The HTTP transport retries
429/5xx and resets with `1.5**attempt` (1, 1.5, 2.25 s) and a 30 s timeout — bounded,
no unbounded loop, no sleep storm.

---

## 2. Import / store

**Verdict: no performance problem found in this layer.** Numbers, so the "no" is
auditable (`bench_import.py`, real tgz, empty store, then a second identical import):

| PRs | events | bundle | read | pre-write gate | import | re-import | verify |
|---|---|---|---|---|---|---|---|
| 500 | 4,145 | 0.3 MB | 60 ms | 27 ms | 508 ms | 472 ms | 100 ms |
| 1,000 | 8,369 | 0.6 MB | 126 ms | 53 ms | 885 ms | 881 ms | 138 ms |
| 2,000 | 16,804 | 1.2 MB | 285 ms | 107 ms | 1,685 ms | 1,838 ms | 172 ms |
| 4,000 | 33,775 | 2.3 MB | 590 ms | 250 ms | 3,314 ms | 3,926 ms | 283 ms |

* **Batching works**: ~10 µs/event end to end (one `INSERT … SELECT` per 5,000 rows),
  exactly linear. The `BATCH_ROWS = 5_000` staging relation is the right size.
* **The pre-write gate is cheap**: 7.4 µs/row, ~7% of import, linear.
* **Re-import is not free** (0.9 s for a no-op 1,000-PR bundle) because the gate, the
  write path and `verify()` all run in full; that is the documented "validate before
  write" contract, and at 1M events it would be ~15 s. Acceptable.
* **`verify()`** scans every string column of every table with regex predicates:
  283 ms for 33.8k events (8 µs/event), inside the transaction. Fine at this scale.

### 2.1 Query plans / indexes — no index is missing

`EXPLAIN` on the 101k-event store shows every hot read is a **sequential scan**
(DuckDB is columnar; there are no secondary indexes, only the ART primary keys):

| query | plan | 101k events |
|---|---|---|
| `count(*)` all | seq scan | 0.3 ms |
| page `ORDER BY ts, event_key LIMIT 2000` | seq scan + Top-N | 9.1 ms |
| `count(*) WHERE ts range` | seq scan + filter | 0.8 ms |
| ticket filter (`EXISTS` on `event_objects`) | seq scan + hash semi-join | 4.6 ms |
| relations `IN` (2,000 keys) | ART index on `event_objects` PK | 11.9 ms |
| `objects` page `ORDER BY type,id LIMIT 5000 OFFSET 50000` | seq scan + Top-N | 36.7 ms |
| `objects` `list_contains(ticket_keys, ?)` | seq scan | 4.1 ms |
| histogram `GROUP BY date_trunc` | hash aggregate | 2.1 ms |

A/B with an ART index created on a copy: `objects` pagination **36.9 ms with** the
index vs **35.8 ms without** (DuckDB still prefers the scan), and `events` page plans
did not change. **Do not add indexes** — the cost is payload volume and Python, not SQL.
Keyset pagination is 4× faster than `OFFSET` (8.8 vs 36.7 ms) but that only matters
because the UI fetches all 60k objects (§5.2).

---

## 3. Analysis

Scaling is **linear** for almost everything — the layer is algorithmically sound; the
problems are constant-factor redundancy and two quadratic loops.

| entry point | 131 PRs / 100 stories | 1,310 / 1,000 | 5,240 / 4,000 |
|---|---|---|---|
| `ballcourt.load` (cold) | 26.6 ms | 267 ms | 1,219 ms |
| `jira.load` | 9.3 ms | 74.5 ms | 344 ms |
| `rules.insights` | 37 ms | 400 ms | **3,537 ms** |
| ↳ payload | 0.59 MB | 6.0 MB | **22.4 MB** |
| `jira.waiting_time_decomposition` | 10.4 ms | 108 ms | — |
| `ballcourt.queue_series` (default 24 h) | 81 ms | 781 ms | **3,027 ms** |
| `ballcourt.blocking_graph` | 1.7 ms | 17 ms | 89 ms |
| `simulation.calibrate` | 19 ms | 104 ms | 432 ms |
| `simulation.simulate` reps=8 | 61 ms | 908 ms | 7,248 ms |
| `tickets.ticket(key)` | 0.8 ms | 4.4 ms | 1.0 ms |

`carry_over` (0.1→1.8 ms), `point_churn` (0.2→3.2 ms), `sprints` (0.6→8.1 ms),
`stack.metrics` (0.1→1.3 ms), Kaplan–Meier (`survival.py`: bucket by time + one sort —
O(n log n), no issue), and the 8/9-segment decomposition (linear, 27 µs/story) are all
fine.

### 3.1 `queue_series` WIP sampling is O(intervals × buckets) — CRITICAL

**Where** `analysis/ballcourt.py:791-796`:

```python
for b in range(buckets):                      # per interval!
    at = start + timedelta(hours=(b + 0.5) * bucket_hours)
    if interval.start <= at < interval.end:
        entry["wip"][b] += 1
```

**Problem** Every measured interval is tested against **every bucket**. `buckets`
comes from the requested window (`days`, up to 3,650) and `bucket_hours` (min 1), so a
client controls the product.

**Evidence** (endpoint, port 8789):

| tenant | `level=person` (24 h buckets) | `bucket_hours=1` |
|---|---|---|
| del-combined (131 PRs) | 96 ms | **1,989 ms** |
| big10 (1,310 PRs) | 1,078 ms | **19,307 ms** |
| big40 (5,240 PRs) | 3,027 ms | **88,452 ms** |

At big40 that is 21,000 intervals × 2,222 buckets × ~2 holders ≈ 93M Python iterations
for a 127 KB response. The **default** view (24 h buckets, 90 days) already costs 3 s.

**Prototype** (`proto_queue_wip.py` / `proto_queue_full.py`): replace the inner loop
with a per-stream difference array over the sorted bucket midpoints (`bisect` the
interval ends, prefix-sum at the end). The **complete** `queue_series` payload is
identical (`identical=True` for every level and bucket width) and:

```
WIP sampling alone, level=person, 1 h buckets
  current 76,466 ms   diff-array 102 ms
full queue_series at 5,240 PRs
  level=person  default 24 h : 3,051 ms ->  152 ms  (20x)
  level=person  1 h          : 78,070 ms ->  497 ms  (157x)
  level=queue   default 24 h : 1,848 ms ->  106 ms  (17x)
  level=queue   1 h          : 46,545 ms ->  318 ms  (146x)
```

**Severity: CRITICAL.** It is the slowest endpoint in the product, it is reachable with
a plain GET, and a handful of concurrent requests will pin every worker thread for over
a minute.

### 3.2 `/api/insights` recomputes the same things ~58× — HIGH

**Where** `analysis/rules.py:2351` (`insights` → `metrics` → 14 metric functions),
`analysis/jira.py:667` (`waiting_time_decomposition`), `analysis/jira.py:518`
(`_cycle`), `analysis/calendar.py:227` (`business_hours`).

**Problem** Each metric independently asks the court/calendar for the same durations and
each metric re-derives each story's `Cycle`. `business_hours` walks day by day, so it is
not cheap to repeat.

**Evidence** (instrumented at 5,240 PRs / 4,000 stories):

```
business_hours calls = 93,400   unique(person, t1, t2) = 1,615   → 57.8× repeat
  (one argument tuple is requested 160 times)
_cycle calls = 16,000           stories = 4,000                  → 4.0× per story
```

`business_hours` 0.97 s cumulative, `_cycle` 1.07 s cumulative — about 2 s of a ~3.5 s
request (profiled total 3.8 s). A monkeypatched prototype (dict memo on `business_hours`, dict memo on `_cycle`)
takes the whole request from **1,713 ms to 864 ms (2.0×)** with identical output.

**Severity: HIGH** — it is the heaviest interactive endpoint and it is recomputed for
every request (`warm` 3,537 ms vs `cold` 3,454 ms: there is no HTTP-level cache).

### 3.3 Simulation: linear but expensive, and re-run per scenario — MED

`simulate` is linear in `replications` (61 / 908 / 7,248 ms for 1 / 8 reps at
del-combined / big10 / big40) — a SimPy run is inherently proportional to the event
count, and 6 replications are a deliberate statistical choice. The problem is not the
simulation, it is that `/api/simulate` re-runs calibration (432 ms at big40) plus all
replications for **every distinct scenario string**, and the UI fires one per knob
change with no debounce (`app/policy.js:114` → `app/main.js:548`). See §5.3.

### 3.4 Deep stacks blow the recursion limit — LOW

`analysis/stack.py:153` (`_depth`) and `:332` (`_descendants`) recurse once per stack
level and copy a `frozenset` path per frame. Synthetic single-chain stacks:

| depth | `stack.metrics` |
|---|---|
| 300 | 10.4 ms |
| 600 | 27.9 ms |
| 900 | 70.8 ms |
| 1,500 | **RecursionError** → HTTP 500 |

Real stack depths are ~2–5, so this is not a practical problem; the fix is cheap
(iterative DFS + a depth cap) and it removes a 500 from the error surface.

### 3.5 `ticket()` itself is cheap — the caller is not

`tickets.ticket()` is 0.8–4.4 ms and its payload is 11.7 KB at every size. The 300 ms
`/api/ticket` latency is entirely `rules.load_jira(con, court)` rebuilding the whole
Jira layer per request (§4.2).

---

## 4. Server

Endpoint latency on port 8789 (cold = first request, warm = median of 2–3):

**del-combined (2,529 events / 131 PRs)**

| endpoint | cold | warm | bytes |
|---|---|---|---|
| `/api/health` | 29 ms | 18 ms | 761 |
| `/api/events?limit=100` | 38 ms | 21 ms | 29 KB |
| `/api/events?ticket=DEL-167` | 115 ms | 20 ms | 9 KB |
| `/api/events/counts` | 102 ms | 21 ms | 5 KB |
| `/api/objects?type=PR&limit=100` | 32 ms | 32 ms | 41 KB |
| `/api/blocking` | 67 ms | 17 ms | 1.5 KB |
| `/api/queues` | 100 ms | 96 ms | 7 KB |
| `/api/insights` | 114 ms | 115 ms | 538 KB |
| `/api/sprints` | 41 ms | 42 ms | 38 KB |
| `/api/simulate` | 90 ms | 1 ms | 17 KB |
| `/api/ticket/DEL-167` | 36 ms | 41 ms | 11 KB |

**big40 (101,160 events / 5,240 PRs / 4,000 stories)**

| endpoint | cold | warm | bytes |
|---|---|---|---|
| `/api/health` | 90 ms | 19 ms | 756 |
| `/api/events?limit=100` | 685 ms | 31 ms | 23 KB |
| `/api/events/counts` | 315 ms | 22 ms | 7 KB |
| `/api/objects?type=PR&limit=100` | 1,260 ms | 54 ms | 41 KB |
| `/api/blocking` | 2,192 ms | 89 ms | 1.5 KB |
| `/api/queues` | 3,036 ms | 3,027 ms | 9 KB |
| `/api/delivery` | 52 ms | 49 ms | 181 KB |
| `/api/insights` | 3,454 ms | **3,537 ms** | **22.4 MB** |
| `/api/sprints` | 481 ms | 349 ms | 1.5 MB |
| `/api/simulate` | 6,176 ms | 2 ms | 18 KB |
| `/api/ticket/DEL-167` | 286 ms | 418 ms | 11 KB |

The tenant/store cache (`_analysis_cache`) works: only the first analysis request pays
`ballcourt.load` (1.2 s at big40); page switches reuse it. That is the one thing this
layer gets right.

### 4.1 `/api/insights` has no result cache and a 22 MB payload — HIGH

Covered in §3.2 for CPU. For the payload: the 22.4 MB (whole window, no `days`; the UI
asks for `days=90` and gets 21.4 MB) is spread over the per-item row lists that the
drill-down needs:

| key | size |
|---|---|
| `metrics.comment_tax.rows` | 4.83 MB |
| `metrics.reviewer_responsiveness.rows` | 4.28 MB |
| `metrics.flow_efficiency.prs` | 2.49 MB |
| `metrics.sentinel_queue.rows` | 1.87 MB |
| `metrics.rework_loops.rows` | 1.68 MB |
| `metrics.time_to_first_review.rows` | 1.65 MB |
| `metrics.cycle_time.rows` | 1.63 MB |
| `metrics.survival.rows` | 1.63 MB |
| `metrics.pr_size.rows` | 1.61 MB |
| `metrics.waiting_time_decomposition.rows` | 1.37 MB |
| … 5 more | ~1.5 MB |

**None of it is compressed.** gzip (level 6) takes the same response from **22.4 MB to
1.1 MB (5%)** and `/api/events?limit=200000` from **35.2 MB to 2.0 MB**. There is no
`GZipMiddleware` and no `Content-Encoding` on any response.

### 4.2 `/api/ticket/{key}` rebuilds the Jira layer per request — HIGH

`server/app.py:722` calls `insights_rules.load_jira(con, court)` on **every** ticket
request. At 4,000 stories that is 344 ms of Python (45,880 `json.loads`, 41,797
`iso()` calls) to render one 11.7 KB payload — 194–376 ms measured, vs 1.0 ms for
`ticket()` itself. `/api/insights` and `/api/sprints` pay the same 344 ms, plus
`load_threads` (50 ms), `load_bots` (2.5 ms), `load_pr_sizes` (13 ms) = **~410 ms of
re-loading per request** on top of the compute. The ballcourt cache shows exactly where
this belongs.

### 4.3 Bulk `/api/events` relation lookup — HIGH

**Where** `server/query.py:127` (`_relations`, 500-key `IN` chunks), reached from `list_events` (143).

At `limit=200000` on the 101k-event store: **2,864 ms / 35.2 MB**. The relation lookup
alone, measured in isolation:

```
chunked IN (500) × 202            1,779 ms   298,640 rows
single JOIN against values relation  295 ms   298,640 rows   (6.0×)
```

`ingest/pipeline.py:877` already uses the join form for the same job — this is a
consistency fix, not an invention.

### 4.4 Harness (`/api/eval`, `/api/screenshot`) — MED

* `eval` round trip is **7 ms** for a trivial expression; a 14 MB result string comes
  back in 651 ms and a 23 MB one in 1,071 ms (chunked at 200 kB/frame). The bridge holds
  one send lock across all frames, so a big result also delays `changed` broadcasts for
  about a second. Acceptable, worth knowing.
* `screenshot` scales with the element height, which the timeline makes enormous:
  `.app` at 1,310 PRs = 1600×20,684 → **4.5 MB PNG in 706 ms**; at 5,240 PRs `.app` is
  **80,709 px tall** and the capture fails in 51 ms with
  `"browser returned an unexpected data URL"` (the output canvas is over the limit, so
  `toDataURL` returns `data:,`). QA screenshots of a large tenant are impossible today.

---

## 5. UI (`app/*.js`)

Measured in headless chromium at 1600×1000, counting every response body.

### 5.1 First paint is fine; the full load is not — HIGH

| | del-combined | big10 | big40 |
|---|---|---|---|
| first paint (1 page of events) | 237 ms | 318 ms | 286 ms |
| background full load settles | 0.46 s | 2.2 s | **5.7 s** |
| events fetched | 2,529 | 25,290 | 40,000 (capped) |
| objects fetched | 1,608 | 16,008 | 60,000 (capped) |
| bytes (initial) | 1.5 MB | **15.6 MB** | **41.1 MB** |
| total bytes incl. views | 2.0 MB | 20.9 MB | **62.7 MB** |

`app/main.js:1587` (`EVENT_PAGE`) / `pageAll` pages `/api/events` newest-first up to
`EVENT_MAX = 40,000`, and `loadObjects` (`:1677`) pages `/api/objects` twice (PRs first, then everything) up to
`OBJECT_MAX = 60,000` — 20 + 14 requests at big40, each one a full `count(*)` + sort on
the server. The cap protects the *event* side but the object side (25.5 MB) is the
larger half, and most of it is COMMIT/THREAD rows the timeline never draws. The
timeline needs lanes, not the 12-column object rows with their `attributes` JSON
(~26–30% of the bytes: 1.13 MB vs 0.79 MB per 5,000 rows measured).

### 5.2 Canvas redraw and the 65,535 px cliff — CRITICAL

**Where** `app/timeline.js:765` (`layout()` sets `canvas.height` from the lane/bar row
count), `render()` (`clearRect` + full repaint).

**Problem** The canvas is as tall as the content: one bar row per PR, one lane per
actor/object/story. Nothing is virtualized, so the bitmap grows linearly with the store
and every frame clears and repaints all of it.

**Evidence** (forced `layout()`+`render()`, median of 5–10, then pixel probe):

| store | PRs | actor canvas | actor render | story canvas | story render |
|---|---|---|---|---|---|
| del-combined | 131 | 1,420×2,317 | **3.7 ms** | 1,420×2,336 | 2.9 ms |
| big10 | 1,310 | 1,420×20,002 | **213 ms** | 1,420×43,014 | 463 ms |
| big20 | 2,620 | 1,420×39,652 | — | 1,420×85,540 | — |
| big40 | 5,240 | 1,420×**78,930** | 148–152 ms | 1,420×**174,354** | 460 ms |

Pixel probe on the big40 canvas: `getImageData(700, 100)`, `(700, h/2)`, `(700, h-10)`
all return **alpha = 0** — the timeline is **completely blank**, not merely slow. A
fresh synthetic canvas of 1,420×174,354 cannot be painted either (fill red → read back
0,0,0,0). Chrome's per-side limit is 65,535 px, so the cliff is at roughly **4,300 PRs**
in actor mode and **~1,900 stories** in story mode. At big20 (39,652 px) the canvas
paints normally, which brackets the threshold.

Below the cliff the cost is still a problem: a single render at the 40k-event cap is
138 ms (actor) to 460 ms (story). The draw primitives themselves are cheap
(`drawHistogram` 4.2 ms, `drawLanes` 10.4 ms, `phaseStats` 0.5 ms, `visibleCount` 0.3 ms)
— the 138 ms is canvas allocation/clear of a 40k-px-tall bitmap, i.e. the missing
virtualization.

### 5.3 Rules drill-down and O(n²) row building — MED

* `app/rules.js:204` renders up to `DRILL_LIMIT = 200` items **per suggestion** into
  `<details>` markup at page render, before the user opens anything. At big40: 7 rules →
  **1,400 `<tr>` and 1,400 `<a>` nodes** (10,288 DOM nodes on the page). Opening them is
  free (1 ms) because the work already happened.
* `app/timeline.js:689` builds an "epic" row with
  `const members = stories.filter(entry => entry.epic === epic)` **inside** the loop over
  stories — O(stories²) when epics are shown. At 4,000 stories with many epic groups,
  `storyRows()` takes **88 ms** and produces 7,920 rows. (This is also what pushes the
  story canvas to 174k px.)
* No other quadratic client loops were found: `lifecycle()` is memoized per PR
  (`_life`), `px()`/`timeAt()` are O(1), the histogram buckets by pixel column.

### 5.4 Data fetched per view

`ensureData` (`app/main.js:505`) is per-page and cached by
`(tenant, timeframe, level, as_of)`: `delivery` 0.18 MB, `blocking` 1.5 KB,
`queues` 9 KB, `sprints` 1.5 MB, `insights` 21.4 MB (UI window), `ticket` 11 KB. Only the insights
view is heavy, and it is heavy on both sides (3.5 s server + 22 MB transfer). The
`/api/sprints` payload (1.5 MB at 4,000 stories) is the second largest and is fetched on
**every** page load (`loadSprints` is unconditional), even on the timeline.

---

## 6. Concrete fixes

### 6.1 (CRITICAL) Stop rewriting the whole bundle at every checkpoint

`ingest/scrape.py:1231` + `ingest/bundle.py:162`.

* **Change**: make the checkpoint append-only. Keep the run's rows in
  `<out>.parts/part-0001.jsonl` (events) / `part-0001.objects.jsonl`, and have
  `write_bundle` stream those parts into the tar at the **end** of the run. `resume`
  already reads the partial bundle through `read_bundle`, so teach `read_bundle` to
  accept the parts directory; the tgz contract at the end is unchanged.
* **Cheaper interim**: make the checkpoint cadence a time budget
  (`--checkpoint-every 50` → "at most once per 60 s, plus every repo boundary"). At
  4,000 PRs that alone takes 45 s → ~6 s.
* **Expected impact**: scrape time O(N²) → O(N); measured 15.2× at 4,000 PRs and the
  ratio is still rising. Jira gets the same fix (`scrape_jira._checkpoint`).
* **Follow-up item**: "scrape: append-only checkpoints" with a test that asserts the
  number of full-bundle serializations per run is 1 (instrument `write_bundle`).

### 6.2 (CRITICAL) Difference-array WIP sampling in `queue_series`

`analysis/ballcourt.py:791`. Replace the per-interval bucket loop with:

```python
midpoints = [start + timedelta(hours=(b + 0.5) * bucket_hours) for b in range(buckets)]
# per stream: a (buckets+1)-length int array
lo = max(0, bisect.bisect_left(midpoints, interval.start))
hi = min(buckets, bisect.bisect_left(midpoints, interval.end))
if lo < hi:
    d[lo] += 1; d[hi] -= 1
# after the loop: prefix-sum each d into entry["wip"]
```

* **Expected impact** (measured on the complete function, identical payload):
  `/api/queues?bucket_hours=1` at 5,240 PRs **88.5 s → ~0.5 s**; the default view
  **3.0 s → ~0.15 s**. (The WIP sampling itself drops 750×; the rest of `queue_series`
  — the per-interval stream/dict work — is the remaining 150 ms and is linear.)
* Also cap `buckets` (e.g. 2,000) and return the actual `bucket_hours` used, so a
  request cannot ask for an unbounded series.
* **Follow-up item**: "queues: O(intervals+buckets) WIP" + a test that compares the
  diff-array output against the naive loop on a fixture.

### 6.3 (CRITICAL) Virtualize the timeline canvas

`app/timeline.js:765` / `render()`.

* **Change**: size the canvas to the viewport (`height = clientHeight`), keep a
  `scrollTop` → row-index mapping, and draw only the rows intersecting the viewport
  (plus a small overscan). Bars/lanes/hit-testing all work off the same row index.
* **Expected impact**: render cost becomes O(visible rows) — ~4 ms instead of
  138–460 ms — and the 65,535 px cliff disappears entirely (the canvas is never taller
  than the window). It also fixes the blank-timeline bug at >4,300 PRs and the
  screenshot failure at 80k px.
* **Follow-up item**: "timeline: virtualized lanes" with a browser test that asserts
  `canvas.height <= viewport` and that a 5k-PR store renders non-blank pixels.

### 6.4 (HIGH) Memoize calendar math and per-story cycles

`analysis/calendar.py:227` (via `BallCourt.business_hours`), `analysis/jira.py:518`.

* **Change**: a per-request memo on the court —
  `court.business_hours(person, t1, t2)` keyed by the tuple (1,615 unique values instead
  of 93,400 calls) — and a `cycle` slot on `Story` filled by `_cycle` on first use
  (1 instead of 4 calls per story). Both are pure functions of immutable inputs, so the
  cache can live for the life of the `BallCourt`.
* **Expected impact**: the compute half measured **1,713 ms → 864 ms** (2.0×) with
  identical output; §6.5 then removes the ~0.4 s of per-request reloading on top.
* **Follow-up item**: "insights: memoize calendar + cycle".

### 6.5 (HIGH) Cache the Jira layer next to the ballcourt

`server/app.py:722` (ticket), `:964` (insights), `:1005` (sprints), `:744` (`_ballcourt`);
`analysis/rules.py:1514` (`load_jira`).

* **Change**: extend `_analysis_cache` to a per-`(tenant, stamp, calendar)` bundle
  `{court, jira, threads, bots, sizes}` built once, and have `/api/ticket`,
  `/api/insights` and `/api/sprints` read it. It is invalidated by the same file stamp,
  so an ingest still refreshes it.
* **Expected impact**: `/api/ticket` 300–418 ms → **~5 ms**; `/api/insights` −410 ms;
  `/api/sprints` −410 ms.
* **Follow-up item**: "server: one cached analysis bundle per store version".

### 6.6 (HIGH) Compress responses

`server/app.py` (app construction).

* **Change**: `app.add_middleware(GZipMiddleware, minimum_size=1024)` (or brotli in
  nginx). Measured ratios: insights **22.4 MB → 1.1 MB (5%)**, events **35.2 MB →
  2.0 MB**, objects page **1.75 MB → 0.24 MB (14%)**, sprints 1.52 MB → 0.05 MB.
* **Expected impact**: the 62.7 MB UI load at 5,240 PRs becomes **~5.5 MB** (41.1 MB
  initial → ~4.4 MB, insights 21.4 MB → ~1.1 MB). This is the cheapest single win in the
  product and it needs no API change.
* **Follow-up item**: "server: gzip responses" + a test asserting
  `Content-Encoding: gzip` for a large JSON body.

### 6.7 (HIGH) Memoize the git cache's presence/merge-base checks and batch fetches

`ingest/gitclass.py:325, 616, 770, 794`.

* **Change**: (a) `self._present_cache[(path, sha)]` and
  `self._merge_base_cache[(path, a, b)]` for the run — measured 26.1 → **14.1 git
  runs/PR, 1.5× faster**; (b) fetch the refs/heads and wanted commits for a whole list
  page (or repo) in one `git fetch` instead of one per PR — removes ~99% of the network
  round trips; (c) reuse `_head_timeline`'s presence checks in `_changed_at` (currently
  2 more `cat-file` per comment).
* **Expected impact**: with a real remote, per-PR git wall time is dominated by the
  fetch; batching turns 1,000 fetches into ~10.
* **Follow-up item**: "gitcache: memoize + batch fetch" with a test that counts
  `_run` calls per PR.

### 6.8 (HIGH) Classify comments concurrently and memoize by content hash

`ingest/classify.py:519`.

* **Change**: a small bounded `ThreadPoolExecutor` (4–8, the Ollama box's parallel
  capacity) over the comment batch of one PR, plus a `dict[blake2b(text), labels]`
  cache for the run. Keep the circuit breaker.
* **Expected impact**: throughput scales with the pool (4× at 4 workers) and the
  duplicate-text hit rate (template comments, "ping", approvals) is usually high.
  8,000 comments at 2 s: 4.4 h → ~1.1 h at 4 workers, less with memo hits.
* **Follow-up item**: "classify: concurrent + memoized" with a stub-server test that
  asserts max in-flight > 1 and that a repeated text makes one request.

### 6.9 (HIGH) One JOIN for event relations

`server/query.py:127` (`_relations`).

* **Change**: `rel = con.values(*[(k,) for k in keys]); SELECT ... FROM event_objects
  JOIN rel ON event_objects.event_key = rel.col0` (the form already used in
  `ingest/pipeline.py:877`), chunked only if the key list is enormous.
* **Expected impact**: `limit=200000` 2.86 s → **~1.4 s** (relation lookup 1.78 s →
  0.30 s measured).
* **Follow-up item**: "events: single-join relations".

### 6.10 (MED) Debounce the policy lab and cache calibration

`app/main.js:548`, `server/app.py:1017`.

* Debounce knob changes 300 ms; split `/api/simulate` into `calibrate` (432 ms at 5,240
  PRs, cache by store stamp + window) and `simulate` (cache by calibration + scenario +
  seed + reps). Every *new* scenario currently pays the 432 ms calibration again on top
  of its ~7 s of replications, and a slider drag creates one scenario per pixel of
  travel.

### 6.11 (MED) Stop shipping the whole object table to the timeline

`app/main.js:1677` (`loadObjects`).

* Gzip (§6.6) is the main win on this path (25.5 MB → ~3.5 MB); a projection to the
  fields the timeline uses (`object_id`, `type`, `ticket_keys`, `repo`, `pr_id`, `state`,
  `created_at`, `closed_at`) removes another ~30% raw (measured 1.13 → 0.79 MB per 5,000
  rows) — worth doing, but not the headline.
* The structural fix is to stop paging the whole table: the timeline draws the PRs it
  has events for, so a `/api/lanes` (or `?ids=`/`?pr=` filter) that returns only those
  objects makes the load independent of the COMMIT/THREAD count.
* Also make `/api/objects` keyset-paged (`WHERE (object_type, object_id) > (?, ?)`)
  rather than `OFFSET`: 36.7 ms → 8.8 ms per page measured.

### 6.12 (LOW) Iterative DAG walks

`analysis/stack.py:153, 332`: replace the recursion with an explicit stack and cap the
depth (e.g. 200) so a pathological store returns a labelled "depth capped" result
instead of a 500.

---

## 7. What is *not* a problem (with the numbers that say so)

* **REST fan-out** — 2.01 requests per kept PR, 0 for filtered PRs, 1 list page/100 PRs,
  1.03 requests/story for Jira. No N+1 anywhere in the scrape.
* **Import throughput** — 10 µs/event, linear to 33.8k events; the pre-write gate is 7%
  of it; `verify()` 8 µs/event. The batching design is correct.
* **Missing indexes** — there is nothing to index that DuckDB would use: an ART index on
  `objects(object_type, object_id)` made pagination *slower* (36.9 vs 35.8 ms), and all
  hot reads are 0.3–37 ms seq scans at 101k events. The SQL is not the bottleneck; the
  payload is.
* **Analysis complexity** — every metric is linear in store size (ballcourt 26.6 →
  1,219 ms for 40× the PRs; decomposition 10.4 → 108 ms for 10× the stories; KM survival
  is one bucket + one sort). The 8/9-segment decomposition over all stories costs
  27 µs/story — a non-issue.
* **Server store cache** — the ballcourt is cached per (tenant, stamp, calendar) and
  page switches do not rebuild it (only the first request after an ingest pays 1.2 s at
  5k PRs). The pattern is right; it is just not applied to the Jira half.
* **`/api/events` and `/api/objects` SQL** — 21–31 ms warm at 101k events with a
  `count(*)` and a sort per page. Fine.
* **Small-store UX** — at the live 2,529-event / 131-PR store every endpoint is
  < 120 ms, the first paint is 237 ms, the whole UI is 2.0 MB and a timeline render is
  4.3 ms. **None of the critical findings above are visible on the demo dataset** — they
  all appear between 1× and 10× its size.

---

## 8. Reproduction

```
# stores
.venv/bin/python /tmp/opencode/perf/make_big_store.py 10 /tmp/opencode/perf/stores/big10.duckdb
# server (never 8766)
MEGASPEED_TENANTS_DIR=/tmp/opencode/perf/tenants MEGASPEED_TENANT=big10 \
  MEGASPEED_HARNESS_TOKEN_FILE=/tmp/opencode/perf/token \
  .venv/bin/python -m uvicorn server.app:app --port 8789
# layers
.venv/bin/python /tmp/opencode/perf/bench_analysis.py data/del-combined.duckdb
.venv/bin/python /tmp/opencode/perf/bench_api.py 8789 big40 2
.venv/bin/python /tmp/opencode/perf/bench_ui.py http://127.0.0.1:8789/ big40
.venv/bin/python /tmp/opencode/perf/bench_scrape_checkpoint.py
.venv/bin/python /tmp/opencode/perf/bench_jira_scrape.py
.venv/bin/python /tmp/opencode/perf/bench_gitclass_breakdown.py 50
.venv/bin/python /tmp/opencode/perf/bench_classify.py 0.05 200
.venv/bin/python /tmp/opencode/perf/bench_import.py
.venv/bin/python /tmp/opencode/perf/bench_harness.py 8789
.venv/bin/python /tmp/opencode/perf/proto_queue_wip.py /tmp/opencode/perf/stores/big40.duckdb 1
.venv/bin/python /tmp/opencode/perf/proto_queue_full.py /tmp/opencode/perf/stores/big40.duckdb
```

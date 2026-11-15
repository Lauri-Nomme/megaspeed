# Adversarial review — megaspeed

**Scope:** `prd/`, `ingest/`, `scrape/`, `server/`, `app/`, `analysis/`, `docs/` (earlier QA
reports), `.github/workflows/`. Reviewed at `6a8a426` and re-verified at `e6024b9`;
prd/19 (business hours) landed *while* this review was running, so the findings that
no longer apply were dropped (see "What held up"). Companion to
`docs/review-product.md`, which judges capability completeness; this one is
code-level and adversarial.
**Method:** read the design docs against the code, then *tried to break it*: ran the
test suites, ran the scrape→tgz→import path end to end, hand-crafted a malicious
transfer bundle, drove the API/WS surface with curl + a raw websocket client,
synthesised a 60k-event store for the UI cap, and timed the import path at three
sizes. Every finding below has a reproduction or a file:line. Nothing in this repo
was modified; only this file was added.

**Baseline:** `.venv/bin/python -m unittest discover -s tests` → 266 tests OK (67 s);
`scrape/tests` → 46 OK. `--verify` passes on `data/del.duckdb`. The happy path
(fixture → tgz → import → verify → all six API views) works. The findings are about
what the happy path hides.

## Verdict

The pipeline is real and the design is unusually coherent, but the load-bearing
claims are **enforced in the wrong place or not at all**:

- the pseudonymisation contract is *audited after the data is already merged* (and
  the audit cannot see single-token leaks) — SEC-1;
- the airgap watermark, which prd/11 §3 calls "not a correctness concern", **silently
  destroys the history of any PR first seen after the watermark** — FR-1;
- the "read-only UI cannot leak" invariant is contradicted by an unauthenticated
  arbitrary-JS endpoint that any websocket client can impersonate — SEC-2;
- the two numbers the product is *for* (timeline at scale, import throughput) fall
  over at exactly the size the PRDs target — NFR-1, NFR-2.

Severity counts: **6 High, 15 Medium, 8 Low**.

| ID | Sev | One line |
| --- | --- | --- |
| SEC-1 | High | Bundle that carries raw names/branch names imports clean; `--verify` says OK; API serves it |
| SEC-2 | High | `/api/eval` + `/ws`: unauthenticated arbitrary JS, no Origin check, results spoofable |
| FR-1 | High | First ingest of an older PR drops its entire history, then marks it settled forever |
| NFR-1 | High | Import is ~9 ms/event (100k events ≈ 15 min, 1M ≈ 2.5 h) |
| NFR-2 | High | Timeline loads the *oldest* 40k events; on a big tenant the default view is empty |
| FR-2 | High | Pre-filter silently drops description-only matches if the list row omits `description` |
| FR-3 | Medium | Mock-fallback labels are stamped `classifier_version=ollama-v1` |
| FR-4 | Medium | Bot comments count as "first review"; headline latency metric is corruptible |
| FR-5 | Medium | Review rounds are not representable in the event model |
| FR-6 | Medium | Watermark is per `source`, not per scraper/project: cross-project bundles collide |
| NFR-3 | Medium | LLM re-classifies every comment of every touched PR on every run (1.9 s each) |
| NFR-4 | Medium | `/api/events` has no default cap: one unauthenticated GET can return 200k rows |
| NFR-5 | Medium | Analysis cache: single global slot, unbounded memory, not single-flighted |
| NFR-6 | Medium | Git fetches (and lazy blob fetches) are outside the "request budget" |
| SEC-3 | Medium | `MEGASPEED_LLM_URL` is unvalidated: raw text goes wherever it points |
| SEC-4 | Medium | Lab credentials committed; auth header on git's argv |
| SEC-5 | Medium | CI: floating action tags, third-party release action with `contents: write`, unsigned assets |
| DES-1 | Medium | `THREAD` is per *comment*, not per review thread |
| DES-2 | Medium | Closed vocabulary can't express a review round or a CI wait |
| DES-3 | Medium | `thread_outcome` collapses "unknown" to `acted_on` (flatters the metric) |
| DES-4 | Medium | PRD-vs-code drift: prd/12 has no `--tenant`, prd/02/11/16 claims are not enforced |
| FR-7, FR-8, NFR-7, NFR-8, SEC-6, SEC-7, DES-5…DES-7 | Low | see sections |

---

## FR — functional holes

### FR-1 · **High** · First ingest of an older PR silently loses its whole history, then marks it settled

`ingest/scrape.py:742-743`:

```python
cutoff = previous - timedelta(hours=overlap_hours) if previous else None
events = collected.events if cutoff is None else [e for e in events if e["ts"] >= cutoff]
```

The reducer always emits **all** activities of a PR it reads, but this filter keeps only
those newer than `watermark − 72 h`. That is fine for a PR that was already in the store.
It is fatal for a PR that is **first seen after the watermark** — which is exactly the
prd/18 workflow ("add the team's ticket keys to `MEGASPEED_TICKET_KEYS`") and the normal
case for any long-lived branch whose key appears late.

Repro (crafted bundle: PR 2 opened 100 days before PR 1, key `DEL-2` added to the scope
only in run 2):

```
run1: watermark 2026-09-21T14:14:20Z kept 1 events 1
run2: kept 1 emitted 0 collected 2 settled 1
PR2 events that crossed: []
run3: skipped_settled 0 skipped_watermark 1 events 0
```

The reducer produced 2 events for PR 2; both were dropped; PR 2 was recorded
`done=true`; every later run skips it. The only signal is a CLI line
`events 0 emitted (of 2 collected; watermark overlap)` that is indistinguishable from
normal dedupe noise. prd/11 §3 explicitly promises "the watermark is a
*checkpoint/efficiency* concern, not a correctness one" — here it is a correctness bug.

**Fix:** apply the cutoff only to PRs that already have a `prs.json` entry with a
`ticket_keys` non-empty *and* whose events are known to be in the store; emit the full
history for a PR with no prior entry (or add `--recheck-new` / per-PR watermarks). At
minimum, warn per PR: `PR X: 7 of 9 events older than the watermark were dropped — run
--full`.

### FR-2 · **High** · Pre-filter silently drops description-only matches when the list row omits `description`

`ingest/scrape.py:94,516-524,566-574`. The cheap pre-filter matches on
`title + fromRef.displayId + description` and drops the PR with **zero** further
requests. The "do I need the detail endpoint?" guard is
`_DETAIL_FIELDS = ("createdDate", "state", "author", "fromRef", "toRef", "properties")`
— `description` is not in it. So any source whose list projection carries those six
fields but not `description` (a DC config/version variant, a proxy that strips large
fields) loses every PR whose only key is in the description, with no detail request and
no warning.

Repro (fixture list rows with `description` stripped):

```
prs_seen 3 kept 0 dropped 3
requests by kind {'list': 1}
events emitted: 0
```

`prs_dropped` counts it as "no ticket key", which is a lie. The existing `lean_list`
test does not catch this because its trimmed projection also drops `createdDate`, so it
takes the detail path.

**Fix:** add `description` to `_DETAIL_FIELDS`, or treat "no key on the cheap fields" as
"fetch the detail record once, then drop" when `prefilter` is on and the row lacks
`description`; and count it as a distinct stat (`prs_dropped_no_description`) so the
report can't hide it.

### FR-3 · **Medium** · Mock-fallback labels are stamped as `ollama-v1`

`ingest/classify.py:483-500`: any per-call failure (timeout, non-JSON, out-of-vocabulary)
degrades *that text* to `MockClassifier`, and after 5 consecutive failures the breaker
opens and **the rest of the run is mock** (`down=True`). `ingest/pipeline.py:537` then
writes `attributes["classifier_version"] = self.classifier.version`, which is the
wrapper's `"ollama-v1"` regardless. So a scrape that classified 100 % of its comments
with the keyword mock produces THREAD objects that claim a 30B model labelled them.

The run-level manifest does carry `down`/`fallbacks`, but that is a single run's report,
not per-label provenance — and the tgz is the only thing that survives the airgap.
prd/02/16 sell `classifier_version` as the thing that makes re-classification
replayable.

**Fix:** have `classify()` return the label set *and* the backend that produced it; write
`classifier_version = f"{backend}-{version}"` per THREAD (and per question event), and
refuse to open the breaker silently in a production run (`--fail-on-llm-down`).

### FR-4 · **Medium** · Bot comments count as the "first review"

`analysis/rules.py:165-176` (`_first_review`) excludes the PR author but not bots;
`time_to_first_review` and `cycle_time` are the two headline numbers in prd/17 §1-2. A
`bitbucket.system-user` stale-PR notice or a CI comment therefore becomes "the first
review", and the review latency the product reports is the bot's latency. `load_bots()`
exists but is applied only to *person-level* rules (`rules.py:899,926,933`).

**Fix:** filter `actor_kind=bot` out of `REVIEW_ACTIONS` in `_first_review`, and report
the bot-sourced share of events next to the metric so a bot-heavy corpus is visible.

### FR-5 · **Medium** · Review rounds are not representable, so `rework_loops` measures something else

`REVIEWED` (a reviewer completing a review) is mapped to `reviewer_changed`
(`ingest/taxonomy.py:PR_ACTIVITY_MAP`, `pipeline.py:452-460`) because the closed
vocabulary has no `pr_reviewed`. prd/05's "count review rounds per PR and classify each
round" is then approximated by `len(pr_pushed) + len(pr_unapproved)`
(`rules.py:433-465`), which counts *pushes* and *approval losses*, not rounds. A review
that ends in a mandatory comment and no push is invisible; a rebase push is counted as a
round. The p90-rounds number quoted in STATE ("p90 3 rounds") is therefore a push count.

**Fix:** extend the vocabulary (`pr_reviewed`), or explicitly rename the metric
(`push_and_unapprove_count`) so the number doesn't borrow the credibility of "rounds".

### FR-6 · **Medium** · The import watermark is per `source` string, not per scraper or project

`ingest/importer.py:31-46`: `_previous_watermark` selects
`max(source_watermark) FROM ingest_runs WHERE source = ?`, and `source` is only
`bitbucket-dc` or `fixture`. prd/12 says each airgapped scraper instance is a tenant, but
nothing binds a bundle to a tenant/scraper identity. Consequences:

- two scrapers (two projects, two environments) importing into one tenant share one
  watermark → the second bundle is **rejected** with "watermark goes backwards" even
  though its data is disjoint;
- conversely, two scrapers' pseudonym spaces are independent (prd/12 says so) but the
  same tenant can receive both, silently mixing two identity spaces (see SEC-7).

**Fix:** key the watermark on `(source, scraper_id)` or `(source, repo)` — the scraper
already knows its repos — and put a `scraper_id` + identity-map fingerprint in the
manifest so import can refuse a bundle from a foreign identity space.

### FR-7 · **Low** · `--tenant` does not exist; a legal store can be invisible

prd/12 specifies `import --tenant <name>`; the CLI only has `--db data/<name>.duckdb`
(`ingest/importer.py:build_parser`). The tenant name is the file stem, so a store called
`data/DEL production.duckdb` fails `tenants.NAME_RE` and is **silently absent** from
`/api/tenants` — the directory is the registry, and discovery never reports what it
skipped. A typo'd import lands in a tenant nobody can see.

**Fix:** add `--tenant`; log/annotate skipped files in `/api/tenants`
(`{"name": "...", "ok": false, "error": "illegal tenant name"}`).

### FR-8 · **Low** · `approvals_lost` look-ahead stops at any comment; `approvals` is dead code

`pipeline.py:757-800`: the scan from a `pr_pushed` stops at the first
`comment_added`/`pr_needs_work`/etc. A push that loses an approval *and* attracts a
comment before the `UNAPPROVED` activity is classified `approvals_lost=false` — a false
negative in the one metric prd/04 says is the highest-value finding. Meanwhile
`pipeline.py:318,404-427` maintains an `approvals` dict that is never read.

**Fix:** bound the look-ahead by time (e.g. 60 s) or by the *next* push only, and delete
`approvals`.

---

## NFR — performance, scale, reliability, maintainability

### NFR-1 · **High** · Import throughput is ~9 ms per event (per-row `executemany`)

Measured on this box with the real import path (`store.write_events` / `write_objects`,
DuckDB, same process):

| events | `write_events` | per row | `write_objects` |
| --- | --- | --- | --- |
| 3 306 | 28.3 s | 8.6 ms | 11.0 s |
| 10 014 | 90.0 s | 9.0 ms | 35.2 s |

(whole `ingest import` command: 11.2 s for an 850-event bundle, 42.4 s for the
3 306-event one — the rest is schema, verify and derived attributes.)

Scaling is linear with a very large constant: **100k events ≈ 15 min, 1M ≈ 2.5 h**, on
top of which `fill_derived` + `recompute_derived` add a full table scan per PR
(NFR-7). `store.py` uses `con.executemany(INSERT_EVENT, [...])`, which the DuckDB Python
client executes one statement at a time; `INSERT OR IGNORE`/`INSERT OR REPLACE` against a
primary key adds a scan per row.

**Fix:** stage the bundle rows into a temp table (`con.register` a pyarrow/pandas frame,
or `COPY tmp FROM 'events.jsonl'`) and do one `INSERT ... SELECT ... WHERE NOT EXISTS` /
`MERGE`; `write_objects` likewise. Expect 50-100× on this path. This is the difference
between a 100k-event tenant being a coffee break and an afternoon.

### NFR-2 · **High** · The timeline loads the *oldest* 40k events; on a big tenant the default view is empty

`app/main.js:942` `EVENT_MAX = 40000`, `pageAll()` pages `/api/events` with
`limit=2000&offset=…` and **no time filter**; `server/query.py` orders `ts ASC` by
default. So the cap keeps the *first* 40 000 events of the log, not the newest.

Measured on a synthetic 60k-event store (2 years of history, 7 624 events in the last 90
days):

```
total 60000
first 40k (UI cap, ORDER BY ts ASC) newest ts: 2026-04-18 05:03:00
snapshot (max ts):                            2026-12-10 07:43:00
events in last 90 days: 7624
```

The default timeframe is `1q` (`app/main.js:46`), so on this tenant the timeline renders
**zero events** while the honesty line says "only the first 40000 of 60000 events are
loaded". The analysis views (blocking/queues/delivery/rules) are computed server-side
over the whole store, so the canvas and the numbers disagree. prd/10 §5.1 also requires
a cost-labelled *"Load the full range"* action and a truncation banner; neither exists.

**Fix:** make the timeline query window-shaped (`from`/`to` from the timeframe, `order=desc`,
`limit` bounded) and/or page backwards from `max(ts)`; add the missing
"Load the full range" affordance. A 40k cap is defensible; an *oldest-first* cap is a
silent lie about the most recent quarter.

### NFR-3 · **Medium** · The LLM cost is O(all comments of every touched PR) per run

`pipeline.py:reduce()` re-classifies every `COMMENTED` activity on every read of a PR
(`_comment_labels`), plus every reply of every question thread (`_reply_answer`), plus
the description. At the benchmarked 1.9 s/comment (CPU, `scrape/bench/llm-bench.md`) a
single new comment on a PR with 30 comments costs ~1 min, and a full 10k-comment project
costs ~5.3 h *per scrape*. There is no label cache keyed by comment id/content hash, no
batching, no concurrency. `annotate_threads` adds one `git diff` (and, with
`--filter=blob:none`, one lazy blob fetch) per anchored thread on top.

**Fix:** persist a scrape-side label cache `(comment_id, content_hash) → labels` (prd/16
already allows a raw-text cache); classify only comments not in the cache; batch the
`/api/chat` calls (Ollama supports parallel requests) or at least pipeline them.

### NFR-4 · **Medium** · No default cap on `/api/events` and `/api/objects`

`server/app.py:events()`: `limit = _int_param(..., default=None, maximum=200_000)`, and
`query.list_events` then emits every matching row (plus one 500-placeholder `IN` query per 500 rows against `event_objects`). One unauthenticated `GET /api/events` on a 200k-event
tenant returns ~60 MB of JSON and builds 200k dicts; `GET /api/objects` likewise. The UI
never hits this (it always pages), but the endpoint is the documented API and is exposed
behind nginx.

**Fix:** default `limit` to something small (2 000) and require an explicit
`limit`/`offset` walk; return `X-Total-Count` (already) so clients can page.

### NFR-5 · **Medium** · The analysis cache is one global slot, unbounded, and not single-flighted

`server/app.py:111,627-636`: `_analysis_cache` holds **one** `BallCourt` keyed by
`(tenant, file stamp)`. With the 3 tenants in production, switching tenants on every page
switch rebuilds the full court (`analysis/ballcourt.py:load` reads the whole event log and
materialises an `Interval` object per event pair — millions of Python objects at 100k
events). The rebuild happens *outside* `_analysis_lock`, so N concurrent requests each
rebuild it (FastAPI runs sync endpoints in a threadpool).

**Fix:** key the cache per tenant with a small LRU, and single-flight the rebuild with a
per-key `threading.Lock`/`Event`.

### NFR-6 · **Medium** · Git traffic is invisible to the "request budget"

prd/14 sells the scrape as "cheap enough to run in the airgap" and reports `requests`.
But `gitclass` adds, per *kept* PR, at least one `git fetch` of the branch refspecs
(`_ensure_heads` → `_refspecs`), plus per-commit fetches, plus `--deepen` rounds up to
`git_max_depth=1024` with a 300 s timeout each, plus one lazy blob fetch per diff under
`--filter=blob:none`. None of that is in `client.requests` or `requests_by_kind`; it is
only in `push_class.fetches`, which the budget discussion ignores. On a 300-PR run that
is ≥300 HTTP round-trips *in addition to* the ~900 REST calls, and the deepening loop can
make a single unclassifiable push cost minutes.

**Fix:** count git invocations in the same budget and surface a combined
`network_ops` figure; cache branch tips per run (one fetch per repo, not per PR); cap the
deepening attempts per run.

### NFR-7 · **Low** · Derived attributes cost one full scan per PR

`pipeline.fill_derived` and `recompute_derived` issue
`SELECT … FROM events WHERE repo = ? AND pr_id = ?` once per touched PR (measured 1.1 ms
per PR on a 10k-event table, i.e. ~10 ms at 100k). For a 10k-PR full import that is
100-200 s of pure overhead, twice. **Fix:** one grouped query (or read the whole
`(repo, pr_id, event_key, ts, activity, attributes)` set once and derive in memory).

### NFR-8 · **Low** · CI never runs the main test suite

`.github/workflows/build-scraper.yml` runs `scrape/tests` (46) and a fixture smoke test.
The 266 tests in `tests/` — the pseudonymisation contract, the reducer, tenants, rules —
never run in CI, and the workflow triggers on `ingest/**`. A change that breaks the
importer (or the store contract) ships as a green scraper release. **Fix:** add a `test`
job on all pushes/PRs (`unittest discover -s tests`), independent of the build filter.

---

## Security

### SEC-1 · **High** · The pseudonymisation gate runs after the merge, and it cannot see single-token leaks

This is the airgap's receiving end, i.e. the one place the PRD says the boundary is
enforced ("A failing tgz is rejected, not merged", prd/11 §6). Two independent problems.

**(a) The content contract is checked after the write.** `ingest/importer.py:64-90`:

```python
check_watermark(con, bundle)          # structure/watermark only
fill_derived(con, bundle.events)
written, rewritten = write_events(...)
write_objects(con, bundle.objects)
recompute_derived(...)
write_run(...)
report = verify(con, ...)             # <- the contract check, after the data is in
```

Repro — a bundle with a *detected* violation (`objects.actor = "Lauri Nomme"`), imported
into a fresh store:

```
PSEUDONYMISATION CONTRACT VIOLATED:
  - objects.actor: 1 value(s) contain whitespace
contract       VIOLATED
EXIT=1
rows with the raw name still in the store: 1
events: 64
```

Exit code 1, but the raw name is already in the analysis zone and `/api/objects` serves
it. The main side "rejects" by printing; it does not reject.

**(b) Bundle validation checks almost nothing.** `ingest/bundle.py::_event_row/_object_row`
validate timestamps, `activity ∈ ACTIVITIES`, `object_type ∈ OBJECT_TYPES`, non-empty
`objects[]` — and nothing else. Attributes, actor shapes, hashes and ticket keys are not
checked before write. `store.verify()` then shape-checks only `events.actor`,
`events.content_hash`, `events.repo`, `objects.repo`, `ingest_runs.source`
(`store.py:shape_checks`). **`objects.actor`, `objects.head_commit`, `objects.pr_id`,
`events.event_key`, `ingest_runs.run_id`, `objects.state` (non-PR/THREAD) are unchecked.**

Repro — a bundle with a raw human name in `objects.actor` and a **branch name** in
`objects.head_commit` (both explicitly forbidden by prd/02):

```
bundle         /tmp/…/tampered.tgz  sha256 328a6b53fca267b3…
events         64 loaded, 64 new, …
contract       OK — no free text, no canaries
EXIT=0
```

and the API serves it:

```
[('PR', 'PR:ENG/platform#100', 'LauriNomme', 'feature/secret-acquisition-q4')]
```

The scraper is the *less trusted* side of the boundary (it sits next to the source, may be
a stale artifact, may be compromised), and the main side accepts anything it says that is
JSON-shaped. Note this is a different attack from QA-REPORT BUG-05 (tampering with a
store you already have): here nothing but the tgz crosses, and every gate passes.

**Fix (in order):** (1) validate the bundle *before* opening the store for writing —
attributes against `taxonomy.check_attributes`/`check_object_attributes`, actor/pseudonym
shape, hash shape, key shape/scope, no-whitespace, canary — and reject (exit 2) with no
writes; (2) then run `verify()` inside a transaction and roll back on failure
(`BEGIN; …; verify; COMMIT`/`ROLLBACK`); (3) close the column holes by shape-checking
*every* VARCHAR column (allowlist-driven: each column gets a predicate, not a hand-written
list), including `objects.actor`, `objects.head_commit`, `events.event_key`,
`ingest_runs.run_id`; (4) replace "no whitespace" with positive shape checks — it is a
proxy for "is prose", and it is wrong for exactly the interesting leaks (emails, URLs,
branch slugs, camelCase names, tokens).

### SEC-2 · **High** · `/api/eval` + `/ws`: unauthenticated arbitrary JS, no Origin check, spoofable results

`server/app.py:891` (`POST /api/eval`), `server/app.py:985` (`/ws`),
`server/harness.py:116` (`eval` broadcasts to every client), `app/main.js:1177-1200`
(`(0, eval)(code)` / `new AsyncFunction(code)`).

There is no authentication, no Origin check on the websocket, and no client identity.
Anyone who can reach the port (nginx fronts it per STATE.md) can:

1. **Execute arbitrary JS** in whatever browser is attached (`harness/browser.py`, the
   persistent headless chromium, started with `--no-sandbox`). The code runs in the page
   context, so it has `fetch`: it can read the whole store through the API and POST it
   anywhere, or reach internal hosts from the server's network. That is a data-exfiltration
   path over the "read-only, cannot leak" UI (prd/06).
2. **Impersonate the browser.** Any websocket client is treated as a legitimate client.
   Proven with a 20-line client sending `Origin: https://evil.example`:

```
0.0s hello: {"event":"hello","app":"megaspeed","clients":1}
0.5s FRAME: {"eval":3,"code":"1+1"}
0.5s sent spoofed result
0.5s eval http: {"ok":true,"data":"SPOOFED-BY-ANY-CLIENT","error":null}
```

So an attacker page in a developer's browser (cross-site WebSocket hijacking — WS is not
subject to CORS) can receive every eval the operator sends and answer it, i.e. control
what `/api/eval` and `/api/screenshot` report. QA-REPORT BUG-12 noted "the first answer
wins" as a UX wart; the security property is that *the first answer may be an attacker's*.

**Fix:** bind the bridge to a per-process random token (`?token=` on `/ws` and a header on
`/api/eval`), check `Origin`/`Host`, and disable `/api/eval` + `/api/screenshot` unless
`MEGASPEED_HARNESS=1` (default off in the nginx-facing instance). Also drop
`--no-sandbox` from `harness/browser.py`, and document that the bridge is a dev-only
capability, not part of the read-only product surface.

### SEC-3 · **Medium** · The LLM endpoint is unvalidated, so "local by construction" is a convention

`ingest/classify.py:512-535` posts the **raw comment/description text** to
`{self.url}/api/chat` with stdlib `urllib`. `self.url` comes from `MEGASPEED_LLM_URL` /
`--llm-url` / `[classify].llm_url` (`config.py`) with no validation whatsoever. prd/02's
central invariant is "no human-authored text ever crosses the local boundary"; the only
thing enforcing it is that nobody mistypes the URL. A single env var (or a shared
`config.toml` in a repo, or a CI variable) sends every comment in the corpus to an
arbitrary host, and `llm_timeout=180` will happily wait for it. The default is also plain
`http://` over the flannel network, so the text is on the wire in clear.

**Fix:** refuse a non-loopback/non-RFC1918 host unless `--allow-remote-llm` is passed
explicitly (and log it loudly); prefer a unix socket; at minimum require `https` for
anything not on `127.0.0.1`.

### SEC-4 · **Medium** · Committed lab credentials; auth header on git's argv

- `ingest/config.toml:69,76`: `export MEGASPEED_BB_PASSWORD=admin-lab-pw` (twice) in a
  repo that says "Everything here is non-secret" and "Nothing secret is committed". It is
  a lab box, but it is a real credential pair (`admin`/`admin-lab-pw`) for
  `127.0.0.1:7990/7991`, and it normalises committing credentials. The same file publishes
  the internal LLM IP (`10.43.36.10`) and the lab topology.
- `ingest/gitclass.py:127-137,767`: the Bitbucket token is passed as
  `git -c http.extraHeader=Authorization: Bearer <token>` on the **command line**, so it is
  readable by any local user via `/proc/<pid>/cmdline` / `ps` for the duration of every
  fetch. It is correctly kept out of `.git/config`, which shows the intent.

**Fix:** drop the password from the file (`${MEGASPEED_BB_PASSWORD}` placeholder in the
comment only); pass the header through a credential helper or `GIT_CONFIG_*` env
(`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`) instead of argv.

### SEC-5 · **Medium** · CI / release supply chain

`.github/workflows/build-scraper.yml`:

- `actions/checkout@v4`, `actions/setup-python@v5`, `actions/upload-artifact@v4`,
  `softprops/action-gh-release@v2` are **floating major tags**, not commit SHAs. A moved
  tag (or a compromised maintainer account) executes arbitrary code in a job with
  `permissions: contents: write` that publishes a release asset other machines will run.
  The third-party action is the highest-value target and gets the write token.
- `permissions: contents: write` is set workflow-wide, so the `pull_request` job (same-repo
  branches) also holds a write token it never needs.
- Build deps are installed from PyPI with no lock/hashes (`pyinstaller>=6.10,<7`), so the
  artifact's provenance is "whatever PyPI served that day".
- The published release asset (a binary that will be carried into an airgapped network)
  has **no checksum, signature or attestation**; `scrape/build.sh` computes no manifest.
  Consumers cannot verify what they run.

**Fix:** pin every action to a full SHA (with a comment naming the version), scope
`permissions` per job (`contents: read` for build, `contents: write` only on the release
step via a separate job), `pip install --require-hashes` with a lock file, and publish a
`sha256sum` + build provenance (`actions/attest-build-provenance`) next to the binary.

### SEC-6 · **Low** · Absolute store paths are published; screenshot sandbox resolves at import

- `/api/health` returns `"store": "/abs/path/data/del.duckdb"` and `/api/tenants` returns
  `"store": str(path)` per tenant — while BUG-07's fix deliberately keeps paths out of
  error bodies ("an error body is not the place to publish the server's home directory").
  The rule is applied to one surface and not the other.
- `SHOT_DIR` defaults to `tempfile.gettempdir()/megaspeed-shots` and is `.resolve()`d at
  import; if `/tmp/megaspeed-shots` is a symlink (another local user on a shared host), the
  sandbox root becomes the symlink target and `_shot_path` happily writes there.

**Fix:** return the tenant *name* and a relative store label only; create `SHOT_DIR` with
`0o700` and refuse a pre-existing symlink.

### SEC-7 · **Low** · The pseudonym scheme is reversible by anyone with the roster

prd/02's scheme is "first two letters of given name + first two of surname" (`LaNo`), and
the code implements it faithfully (`ingest/pseudonym.py:24-31`). But that is
*initial-based*, not *random*: for a known team, `LaNo` names Lauri Nomme, and combined
with `repo` slugs, ticket keys and per-person review latency, the store is effectively
re-identifying. The PRD acknowledges this risk for ticket keys and not for pseudonyms
("pseudonyms keep the analysis from leaking who-is-who" — they don't). Also: losing the
identity map silently re-pseudonymises everyone (`pseudonym.py:IdentityTable`), and
nothing at import detects that the new bundle's `LaNo` is a different person than the
stored `LaNo` — the map is the one irreplaceable artifact (prd/11 §5) and there is no
continuity check.

**Fix:** use a keyed hash (`HMAC(tenant_key, actor_key)`) truncated to a stable opaque id,
or random ids persisted in the map; add an identity-map fingerprint to the manifest and
refuse a bundle whose fingerprint differs from the tenant's.

---

## Design

### DES-1 · **Medium** · `THREAD` is one object per *comment*, not per review thread

`pipeline.py:336,371` keys threads by `comment["id"]`, so every reply in a Bitbucket
comment thread becomes its own `THREAD` object with its own `thread_outcome`. prd/03
promises `ReviewThread {id, pr, anchor-class, severity, state, resolved_by}` with
parent/replies; the store cannot answer "which thread did this comment belong to" or
"how many threads were rejected on this PR" without guessing. `thread_outcome` is
computed from the *parent* comment's resolution plus the classifier's read of *that one
comment* (`pipeline.py:516-560`), so a thread whose resolution was driven by a reply is
attributed to the opening comment.

**Fix:** one `THREAD` object per `comment.id` **root** (follow
`comment.parentId`/`comment.comments`), with the replies as events referencing it; store
`commenter` and `resolver` as separate closed attributes.

### DES-2 · **Medium** · The closed vocabulary can't express a review round or a CI wait

`taxonomy.ACTIVITIES` has `build_status` and `rebased`, but nothing emits them; there is
no synthetic `CI` actor in any event (`ballcourt.CI` exists as a constant with no
producer). And `REVIEWED → reviewer_changed` (FR-5). So two of prd/05's core
constructions — "review rounds" and "while CI runs, the ball is with `CI`" — are
unrepresentable, and the analysis substitutes proxies. The blocking graph therefore never
shows the CI queue the PRD says is a hidden bottleneck.

**Fix:** add `pr_reviewed` and emit `build_status` from the commit build-status endpoint
(or explicitly declare both out of scope in prd/03 so the model stops promising them).

### DES-3 · **Medium** · "Deterministic first, LLM second" is inverted for `thread_outcome`, and unknown resolves to *success*

`classify.thread_outcome` (`classify.py:539-575`) needs `anchor_changed`, which needs a
git cache, an anchor path, and both heads present. In a REST-only scrape (or for
"general" comments, or when the head is gone) it is `None`, and then:

```python
return {"acted": "acted_on", "rejected": "rejected", "withdrawn": "walked_back"}.get(intent, "acted_on")
```

`intent` defaults to `"unclear"` → **`acted_on`**. So the "wasted rounds" metric
(`comment_tax`) systematically under-counts: every thread whose outcome we could not
determine is booked as a success. That is the direction of bias that makes the tool
flatter the team, which is exactly what prd/10 §7 ("never lie about the data") is meant to
prevent.

**Fix:** add an `unknown` member to `THREAD_OUTCOME` (or leave the attribute absent) and
report the classified share next to the metric; only `acted_on` when a deterministic
signal says so.

### DES-4 · **Medium** · PRD-vs-code drift: several "done" claims are not implemented or not enforced

| PRD claim | Reality |
| --- | --- |
| prd/02 "attempting to insert free text is a build/test failure" | a whitespace heuristic, post-write (SEC-1) |
| prd/11 §6 "a failing tgz is rejected, not merged" | merged, then reported (SEC-1) |
| prd/12 "Import targets a tenant (`--tenant <name>`)" | `--db data/<name>.duckdb` (FR-7) |
| prd/13 "self-contained build artifact" | PyInstaller onefile; the zipapp fallback ships *source* and silently omits `store/importer/live` — a different artifact with different behaviour |
| prd/16 "classifier_version records which classifier produced the labels" | the wrapper's version, even when every label came from the mock (FR-3) |
| prd/14 "the scrape's request budget" | excludes git traffic (NFR-6) |
| prd/09 "read-only, no write path, cannot leak" | `/api/eval` arbitrary JS (SEC-2) |

Each is small; together they mean a reader of `prd/` believes the system has properties it
does not have. The docs are the product's contract here (AGENTS.md rule 2/3 treats them as
normative).

**Fix:** either implement, or move to an explicit "Not implemented / accepted gap"
section per PRD with a link to the finding. A `STATE.md` line is not enough when the PRD
still reads as a promise.

### DES-5 · **Low** · A business-hours number cannot be re-derived from the snapshot alone

prd/19's calendar is main-side, mutable config (`config/calendar.default.yaml` +
`data/<tenant>/calendar.yaml`), applied at read time. Every response says
`hours_mode: "business"`, and `/api/calendar` shows the *current* effective calendar, but
neither the payload nor the store carries a calendar **fingerprint** (hash/version) —
so a saved report, a "Copy as Markdown", or an exported chart cannot be re-derived later
once someone edits a vacation or a holiday. prd/02's reproducibility claim ("outputs are
reproducible from `classifier_version` + content hash") quietly gained a third input that
is neither versioned nor stored, and prd/10 §7 asks for provenance on every estimated
number.

**Fix:** put a short `calendar_id` (hash of the merged config) in every analysis payload
and in `/api/health`, and key the analysis cache on it (the cache already keys on the
files' mtimes — the same value, exposed).

### DES-6 · **Low** · `objects.actor` means a different thing per object type

`PR.actor` = author, `COMMIT.actor` = author, `THREAD.actor` = **resolver**
(`pipeline.py:516-560`), `PERSON.actor` = self, `PRREV.actor` = pusher. A generic reader
(`/api/objects?actor=LaNo`) therefore returns "PRs they wrote, commits they authored, and
threads *someone else* resolved" as if they were one relation. It also means prd/15 Q4
("who posts BS comments") cannot be answered from the object table.

**Fix:** give the object table explicit role columns (`author`, `resolver`, `commenter`)
or make `actor` mean "subject" uniformly and add `by` for the actor of the transition.

### DES-7 · **Low** · Dead code and one-sided invariants

`pipeline.Reducer.approvals` (maintained, never read), `pipeline._ACTIVITY_ORDER` (a
regex tiebreak that only works for `act:` keys — `desc:q`, `t<id>:a` sort first, so those
events tie-break as `(0, "", 0)`), the ball-attribution rule re-implemented in
`rules.reviewer_responsiveness` instead of reusing `Interval.waited_on` (two copies that
can drift), `config.DEFAULT_TICKET_KEYS`/`DEFAULT_BOT_SLUGS` duplicated in
`config.toml`, `ingest.store.list_events` (superseded by `server.query.list_events` but
still exported and used by tests). Not bugs; each is a place where two copies of a rule
can drift.

---

## What I tried that held up

Worth recording, because it bounds the findings above:

- **Ticket scoping semantics** (prd/18): exact-set beats prefix, env ∪ file, malformed
  entry is a hard error, `--verify` rejects out-of-set keys. Tested; correct.
- **Idempotency**: re-importing the same bundle is a no-op (`events_new 0`,
  `events_rewritten 0`) and re-derives nothing; natural keys are sound.
- **Tenant path traversal**: `?tenant=../t1` → `404 {"valid":["del"]}`. `NAME_RE` with
  `\Z` is the right call; screenshot `out` is resolved and confined.
- **SQL injection**: every filter is a bound parameter; `granularity` is mapped through a
  dict; unknown query params are 400 with the valid list. No injection found.
- **Deterministic classification**: `git patch-id`/`range-diff` with real-git tests, and
  the fallback ladder (`other` + reason counter) is honest.
- **Closed-vocabulary parsing**: `parse_labels` is strict (missing/extra/enum/bool all
  raise), and the Ollama `format` schema is the same object the tests assert on. The
  classifier *cannot* return prose — the leak risk is elsewhere (SEC-1/SEC-3).
- **Canary/schema tests** are real (fixtures plant `zzcanary…` in every human field and
  the tests assert absence), they just cover the fixture path only.
- **Store-busy handling**: 503 + `Retry-After` + UI retry loop is well done.
- **`--verify` on the DEL store** passes, and the earlier QA report's fixes (BUG-01..14)
  are genuinely in the code, not just in the report.
- **prd/19 business hours** (which landed during this review) is the counter-example to
  DES-4: `yaml.safe_load`, cross-midnight windows rejected with a named error, half-days
  as the morning, vacations with `ignore_vacations` for the "would have worked" price,
  DST via `zoneinfo`, a broken config degrades to wall-clock *and says so*, and 647
  lines of tests pin the arithmetic. That is the standard the rest of the PRDs deserve.

---

## Appendix — reproductions

The scratch scripts were throwaway (under `/tmp/opencode/`, outside the repo); the
mechanism is described in each finding. The two that need construction are reproduced
here so they can be re-run from a clean checkout.

**SEC-1** — a bundle that carries a raw name and a branch name, with valid checksums
(the writer is the project's own `_tamperproof_tar`, so this is a *well-formed* bundle,
exactly what a buggy or hostile scraper would emit):

```python
import hashlib, json, sys, tarfile
sys.path.insert(0, ".")
from ingest.bundle import (read_bundle, _tamperproof_tar, MEMBERS,
                           MANIFEST, EVENTS, OBJECTS, SUMS)

b = read_bundle("export/del.tgz")                       # any real bundle
objects = [dict(o) for o in b.objects]
objects[0]["actor"] = "LauriNomme"                      # no whitespace -> passes verify
next(o for o in objects if o["object_type"] == "PR")["head_commit"] = "feature/secret-q4"

with tarfile.open("export/del.tgz") as tar:
    events = tar.extractfile("export/events.jsonl").read()
    manifest = tar.extractfile("export/manifest.json").read()
payload = {EVENTS: events, MANIFEST: manifest,
           OBJECTS: "".join(json.dumps(o, sort_keys=True, separators=(",", ":")) + "\n"
                            for o in objects).encode()}
payload[SUMS] = "".join(f"{hashlib.sha256(payload[n]).hexdigest()}  {n}\n"
                        for n in (MANIFEST, EVENTS, OBJECTS)).encode()
open("export/tampered.tgz", "wb").write(_tamperproof_tar([(n, payload[n]) for n in MEMBERS]))
```

```console
$ .venv/bin/python -m ingest import export/tampered.tgz --db /tmp/t.duckdb
contract       OK — no free text, no canaries        # exit 0
$ .venv/bin/python -c "import duckdb; print(duckdb.connect('/tmp/t.duckdb', read_only=True) \
    .execute(\"select actor, head_commit from objects where actor='LauriNomme'\").fetchall())"
[('LauriNomme', 'feature/secret-q4')]
```

Swap `"LauriNomme"` for `"Lauri Nomme"` and the same import prints
`contract VIOLATED` / exit 1 **after** writing the row — see the finding.

**FR-1** — two PRs where the second is first seen in run 2 (the prd/18 "add a key" case);
`_record` / `RecordingTransport` come from `scrape/tests/test_optimization.py`:

```python
import sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")                 # the `ingest` package
sys.path.insert(0, "scrape/tests")      # _record / RecordingTransport
from test_optimization import _record, RecordingTransport
from ingest.config import Config
from ingest.scrape import scrape

MS, DAY = 1000, 86_400_000
root = Path(tempfile.mkdtemp())
def cfg(keys):
    return Config(db_path=root/"u.duckdb", identity_path=root/"id.json",
                  ticket_regex=r"\b[A-Z]+-\d+\b",
                  key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
                  repos=[("DEL", "huge")], ticket_projects=("DEL",), ticket_keys=keys)

pr1 = _record(1, state="OPEN", key="DEL-1", updated_minute=100_000)
base = pr1["pull_request"]["updatedDate"]
pr2 = _record(2, state="MERGED", key="DEL-2", updated_minute=100_001)
pr2["pull_request"]["createdDate"] = base - 100*DAY
pr2["pull_request"]["closedDate"] = base - 90*DAY
for a in pr2["activities"]:                    # whole history is 90-100 days old
    a["createdDate"] = base - 100*DAY + (a["id"] % 100) * MS
bundle = {"schema": "megaspeed.fixture/1", "projects": [{"key": "DEL", "repos": [
    {"slug": "huge", "pull_requests": [pr2, pr1]}]}]}      # NEWEST first

scrape(cfg(("DEL-1",)), RecordingTransport(bundle), "fixture",
       out=root/"o.tgz", state_dir=root/"st", overlap_hours=72)
r = scrape(cfg(("DEL-1", "DEL-2")), RecordingTransport(bundle), "fixture",
           out=root/"o.tgz", state_dir=root/"st", overlap_hours=72)
print(r["prs_kept"], r["events"], r["events_collected"], r["prs_settled"])
```

```console
$ .venv/bin/python repro_fr1.py
1 0 2 1        # kept 1 PR, 0 events emitted of 2 collected, 1 marked settled
```

**SEC-2** — any websocket client is accepted as "the browser" and its answer is trusted:

```python
import asyncio, json, urllib.request, websockets
async def main():
    async with websockets.connect("ws://127.0.0.1:8766/ws",
                                  additional_headers={"Origin": "https://evil.example"}) as ws:
        print(await ws.recv())                      # {"event":"hello",...}
        asyncio.get_running_loop().run_in_executor(None, lambda: urllib.request.urlopen(
            urllib.request.Request("http://127.0.0.1:8766/api/eval",
                data=b'{"code":"1+1"}', headers={"content-type": "application/json"},
                method="POST")).read())
        msg = json.loads(await ws.recv())           # {"eval": N, "code": "1+1"}
        await ws.send(json.dumps({"evalres": msg["eval"], "ok": True, "data": "SPOOFED"}))
asyncio.run(main())
```

**Measured numbers** (this box, `.venv`, `data/` untouched):

| claim | measurement |
| --- | --- |
| import writes | 3 306 events → 28.3 s (`write_events`) + 11.0 s (`write_objects`); 10 014 → 90.0 s + 35.2 s |
| UI cap | 60 000 synthetic events: the first 40 000 (`ORDER BY ts ASC`) end 2026-04-18; snapshot 2026-12-10; 7 624 events in the last 90 days |
| derived attrs | `fill_derived`, 100 PRs over a 10k-event table → 0.11 s (≈1.1 ms/PR) |
| suites | 266 tests OK (67 s), 46 scrape tests OK |

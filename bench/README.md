# bench — the performance-review harness

The scripts that produced the numbers in `docs/review-performance.md`, committed so the review can
be re-run. They were captured from the review session (originally `/tmp/opencode/perf/`) and lightly
normalised to be self-locating: each resolves the repo root from its own path, so it works from any
checkout. Generated artifacts (stores, bundles, payload dumps, logs) are gitignored.

## Prerequisites

- The repo venv (`../.venv/bin/python`) and a `del-combined` store at `data/del-combined.duckdb`.
- For API benches: a running server (the `.sh` wrappers start one on demand).

## Building a stress store

The real store is 2,529 events / 131 PRs / 100 stories. `make_big_store.py` replicates it K times with
uniquified identifiers (same distributions, K× scale):

```bash
../.venv/bin/python make_big_store.py 40          # -> stores/big40.duckdb (~5,240 PRs, 4,000 stories)
```

`big10`/`big20`/`big40` were the review's scale points. `bbNNN`/`impNNN`/`prof`/`idx` under `stores/`
were the scrape/import/gitcache scale points (built by the corresponding `bench_*.py`).

## Script → finding map

| Script | Reproduces (`docs/review-performance.md`) |
|---|---|
| `make_big_store.py` | the synthetic stores for every finding |
| `bench_analysis.py`, `wt_insights.py`, `wt_bench.py`, `wt_dump_payloads.py` | §analysis — per-metric compute, insights payload |
| `proto_queue_wip.py`, `proto_queue_full.py` | `queue_series` O(intervals×buckets) prototype |
| `bench_api.py`, `wt_api.sh`, `wt_api_before.sh` | API timings + sizes (queues/insights/events/ticket) |
| `bench_ticket_cache.py`, `run_ticket_bench.sh`, `fingerprint.py`, `run_fingerprint.sh` | `/api/ticket` cold-path + payload identity |
| `bench_scrape_checkpoint.py`, `bench_checkpoint_wt.py`, `prof_ckpt.py`, `prof_ckpt4k.py`, `bench_jira_scrape.py`, `bench_jira_scrape_wt.py` | scrape checkpoint O(N²) → incremental |
| `bench_gitclass.py`, `bench_gitclass_breakdown.py`, `bench_gitclass_wt.py`, `dbg_git*.py` | git-cache subprocesses per PR |
| `bench_classify.py`, `bench_classify_wt.py` | LLM classification concurrency |
| `bench_import.py` | import/store write cost |
| `bench_ui.py`, `bench_ui_render.py`, `bench_harness.py` | timeline canvas render ms / blank-past-65k |
| `e2e_dump.py` | end-to-end scrape → import byte-identity |
| `dbg_spool.py`, `dbg_growth*.py` | checkpoint spool growth debug |

The `wt_*` scripts run against a detached "before" worktree vs the patched build (they reference a
worktree path you must recreate — see the header comment in each).

## Re-running the headline numbers

```bash
../.venv/bin/python make_big_store.py 40                # stores/big40.duckdb
# start a server on a scratch port against stores/big40.duckdb, then:
bash wt_api.sh                                          # or wt_api_before.sh against the unpatched build
```

Expected deltas are the "before/after" columns in the report (e.g. `/api/queues?bucket_hours=1`:
88 s → 458 ms; `/api/insights` 22.4 MB → 1.14 MB gzip; checkpoints 44.6 s → 5.7 s at 4,000 PRs).

The scripts assume `bench/stores/`, `bench/tcache/`, etc. exist — they are created on demand and
gitignored.

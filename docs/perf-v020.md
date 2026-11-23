# Performance profile — v0.20.5 (2026-11-24)

Store: 75,537 events · 12,938 threads · 200 people · window `days=1882`.
Server: v0.20.2+ (same analysis code as v0.20.5). Machine: 28 cores, local.

## Endpoints (warm, 3 calls each)

| endpoint | time | note |
|---|---:|---|
| `/api/insights?days=1882` | **1.12–1.17 s** | 1.23 MB payload |
| `/api/insights?days=120` | 0.42–0.56 s | first call 1.97 s |
| `/api/queues?days=120` | 0.09 s | |
| `/api/delivery?days=120` | 0.08 s | |
| `/api/verdict?days=120` | 0.02 s | |

The old 12 MB insights payload is gone (prd/70 F-11); compute is ~1 s warm.

## Cold first request: 25 s unprofiled, 40.6 s under cProfile

```
_advice_payload                34.6 s   (85%)
  simulation.simulate          33.5 s
    5 scenarios × 30 runs, 4,010 steps, 2.79M SimPy events
  policy_lab.calibrate          ~0.4 s
rules.metrics                   2.8 s
_analysis_bundle                2.5 s   (store load, threads, bots, Jira)
jira.metrics                    1.6 s
```

So the bundle build is cheap; the **policy-lab cross-check** is the cold cost.
It is cached in-process (`_advice_cache`) per
`(tenant, store stamp, calendar, window, repo)`, which is why every later call
is ~1 s. A restart, a re-import, a calendar edit, or a **different `days`
window** pays it again.

## prd/73 candidates after measurement

| id | status |
|---|---|
| P1 `queue_series` WIP sampling | **not visible** in the profile; no action without a new profile that shows it |
| P2 insights recompute / payload | payload fixed; warm compute 1.1 s; the remaining cost is the cold lab, see P8 |
| P3 `/api/ticket` rebuild | not measured (needs a key with many children) |
| P4 bulk `/api/events` | not measured (my first attempt returned an empty body — wrong params, not a slow path) |
| P5 canvas cliff | UI, not measured here |
| P6 drill-down O(n²) | covered by the cap; not measured |
| P7 initial 40k of 76k events | product decision, not a perf bug |

### P8 (new) — policy lab cold start

Options, in increasing risk:

1. **Pre-warm** the default tenant/window in a background thread at server
   start. Simple, but it competes for CPU with the first requests and still
   takes ~25 s before the lab is ready.
2. **Persist** the lab report keyed by store/calendar/window **and a policy
   version**; a restart is warm. Needs a cache-invalidation story (rules change,
   SimPy change) or it can serve a stale number.
3. **Fewer runs** (30 → 10) or fewer scenarios in the cross-check. That is a
   statistics decision, not an engineering one: the CI on the comparison must
   stay meaningful (prd/43, prd/62).
4. **Window-independent lab**: compute once over the full store and reuse for
   any `days`. Changes semantics — the calibration is window-scoped today.

Recommendation: decide (1) vs (2) with the product owner; do not touch the
simulation until then. The warm path is already fine.

## Test-suite note

The suite is not part of this profile. It runs **1,571 passed / 21 skipped in
22.7 s** at `pytest -n 8 --dist loadfile` (prd/73's "measure first" applies to
tests too; the per-test timings live in `/tmp/opencode/test-times2.txt`).

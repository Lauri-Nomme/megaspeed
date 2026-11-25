# 77 — The policy-lab result survives a restart

Status: **implemented (v0.21.1).**

## Problem

The first `/api/insights` request per `(store, calendar, window, repo)` spends
~12 s of real time (33 s under cProfile) in the policy-lab SimPy simulation:
five scenarios × 30 runs, ~2.8M events (`docs/perf-v020.md`). The result is
cached **in memory only** (`_advice_cache`), so every server restart, every
re-import and every new `days` window pays it again. On this store the lab's
final report is “no validated result” — a deterministic verdict — yet the
simulation still runs to reach it, so the page waits for a number it will not
show.

## Decision

Persist the computed report to
`<tenants-dir>/.cache/advice/<key>.json` and read it before computing.

- **Key**: tenant, store path + stamp (mtime/size), calendar stamps, window
  start/end, repo, and `ADVICE_DISK_VERSION`.
- **What is persisted**: a *computed* report, including the deterministic
  “no validated result”. What is **not** persisted is an **exception** path
  (a SimPy edge, a broken store): `_compute_advice` returns
  `(report, cacheable)` and only `cacheable=True` is written, so a transient
  failure is retried on the next run.
- **Write**: atomic (`tmp` + `os.replace`), best-effort — a read-only tenants
  dir must not break Rules.
- **Reads**: memory cache first, then disk; a corrupt/foreign file is ignored
  and recomputed.
- **Invalidation**: the store and calendar stamps are in the key, so an import
  or a calendar edit is a miss; a change to the simulation or the report shape
  must bump `ADVICE_DISK_VERSION` (a code review point, commented at the
  constant).
- **Payload**: aggregates only; no prose, no raw text. The cache lives next to
  the stores, hidden (`.cache/`), and is not a tenant (`tenantreg` scans
  `*.duckdb`).

## Non-goals

- No change to the simulation, its run count or the calibration (a statistics
  decision, prd/43/prd/62).
- No startup pre-warm; the disk cache makes the first request after a restart
  warm without spending CPU before anyone asks.
- No cross-tenant sharing (the key includes the tenant and store).

## Acceptance

- A **second process** with an empty memory cache loads the report from disk;
  `_compute_advice` is called zero times.
- A different window, a changed store or a changed calendar is a new key.
- A corrupt cache file is ignored; an exception result is not written.
- The response payload is byte-identical to the fresh computation.
- `pytest tests/test_advice_cache.py` covers all of the above.

## Files

`server/app.py` (`_advice_disk_path`, `_read_advice_disk`,
`_write_advice_disk`, `_advice_payload`, `_compute_advice`),
`tests/test_advice_cache.py`.

## Measured (2026-11-25, `demo-tlla`, `days=1882`)

| | before | after |
|---|---:|---:|
| first request after a restart | ~12 s | **3.2 s** |
| second request (same process) | ~1 s | ~1 s |

Verified on the live server: the disk file's mtime did not change on the warm
request, i.e. the report was loaded, not recomputed.

The 33 s cProfile figure inflates the SimPy path ~2.5×; the real cold cost is
the ~12 s the page waits.

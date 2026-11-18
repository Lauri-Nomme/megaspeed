# 33 — Performance hardening (from the adversarial review)

Findings from `docs/review-performance.md`, ranked. Fix them with regression tests that pin the
behaviour (and, where measurable, a timing/byte-equivalence guard).

## CRITICAL

1. **Scrape checkpoints rewrite the whole bundle** every 50 PRs (`ingest/scrape.py:1231` →
   `write_bundle` gzip+tar of everything): 4,000 PRs = 45 s vs 2.96 s without (15×). Make the
   checkpoint incremental — persist only what changed (or a temp that's finalised once), same
   crash-resume guarantee, ~O(new work) per checkpoint.
2. **`queue_series` is O(intervals × buckets)** (`analysis/ballcourt.py:791`): `/api/queues?bucket_hours=1`
   = 19.3 s @1,310 PRs / 88.5 s @5,240. Rewrite with a difference array (add at start, subtract at end)
   — byte-identical output, 3,051→152 ms default and 78,070→497 ms at 1 h.
3. **Timeline canvas blanks past Chrome's 65,535 px limit** (`app/timeline.js:765`): 5,240 PRs = 78,930 px
   with alpha 0 everywhere (story mode 174,354 px). Virtualise lane rows — draw only what's in view (or
   cap canvas height + scroll the lanes) — so large stores stay renderable.

## HIGH

4. **`/api/insights` recomputes per request** (3.5 s / 22.4 MB @5,240 PRs): 93,400 `business_hours` calls
   for 1,615 unique tuples (58× repeat) and `_cycle` 4× per story. Memoise both (per store-version cache);
   identical output, ~half the compute.
5. **No compression middleware**: the browser pulls the whole store + insights uncompressed (62.7 MB
   @5,240 PRs). Add gzip for JSON (insights 22.4→1.1 MB, whole UI ~5.5 MB).
6. **git cache = 26 subprocesses + 1 fetch per PR** (warm cache doesn't help). Memoise the per-(repo,
   head) push_class/diff computation (14/PR).
7. **LLM classification is serial** (max in-flight = 1 → 4.4 h per 8k comments at 2 s). Bound the
   concurrency to the backend's capacity.
8. **`/api/ticket` rebuilds the Jira layer per request** (300–418 ms for 11.7 KB). Cache per
   (tenant, key, store-version), same invalidation as the other caches.
9. **Bulk `/api/events` relation lookups** use a chunked `IN` (1.78 s) where one `JOIN` (0.30 s) works.

## Definition of done

- Each fix has a regression test (correctness +, where stable, a byte-equivalence or a "no regress"
  timing guard); the 786 + 75 + 126 suites stay green.
- The measured numbers from the report improve as claimed, checked against `data/del-combined.duckdb`
  and the synthetic stress stores under `/tmp/opencode/perf/`.

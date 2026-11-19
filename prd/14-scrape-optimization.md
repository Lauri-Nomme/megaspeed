# 14 — Scrape optimization (huge projects)

A huge Bitbucket project makes a full scrape (list all PRs + fetch each PR's activities + commits)
too slow and too request-heavy. The scrape must be cheap enough to run in the airgap, and must be
**focused on PRs & commits that pertain to a set of tickets**.

## Optimizations (layered)

1. **Ticket-key pre-filter (the big win).** The PR-list endpoint (paginated `limit=100`) returns
   summary fields — `id`, `title`, `state`, `fromRef.displayId` (source branch), `updatedDate`,
   `author`, **and `description`** — without the per-PR activities / commits. Keep a PR only if its
   `title`, source branch, **or description** matches a target ticket key. Only matching PRs get the
   expensive activities + commits fetches (no per-PR detail fetch is needed for the filter itself).
2. **Incremental watermark.** Maintain a watermark = max `updatedDate` processed (per repo, in
   local state). On an incremental scrape, skip any PR whose `updatedDate < watermark`.
3. **Closed-PR finality.** A MERGED/DECLINED PR with `updatedDate < watermark` is *settled* — its
   event history is stable (no new commits/approvals after close, barring rare late comments).
   Scrape it once and mark it done; only OPEN (or recently-updated) PRs are re-checked each run.
4. **NEWEST-first + early termination.** Request the PR list `order=NEWEST`. **Verified on DC 9.4.18:
   `order=NEWEST` sorts by `updatedDate` descending** (not createdDate). Paginate NEWEST-first and
   **stop as soon as a full page has every `updatedDate < watermark`** — every older page is even
   older, so the rest of the project can be skipped entirely. This is what makes a huge project
   tractable: the scrape only walks back to the watermark, then stops.

## Local state (scraper side)

- `<state-dir>/watermark.json` — per-repo `updatedDate` cursor (never goes backwards).
- `<state-dir>/prs.json` — per PR: `{state, updated_date, done}` (settled + fully scraped).

## Progress reporting

Long scrapes (huge projects) must report progress to **stderr**, by default every **30 s**
(configurable, e.g. `--progress-interval`), a single line like:

    [scrape] 02:15 elapsed · 3,214 PRs seen · 412 kept · 2,802 skipped · 1,041 req · 5.2 PR/s · 4.3 req/s

- Fields: elapsed wall-clock, PRs seen / kept / dropped (or skipped-by-filter), requests made, events
  emitted, and throughput (PR/s and req/s).
- Goes to **stderr** (never the tgz or piped stdout); `--quiet` disables it.
- Emitted on a timer, not per PR (so it doesn't flood); the final summary still prints at the end.

## Notes

- **Boundary rule — use `<`, never `<=`.** `updatedDate` is not unique (ties are common), so a PR
  sitting *exactly* at the watermark is ambiguous (it may or may not have been processed). The safe
  split is: **needs processing = `updatedDate >= watermark`**; **already done / safe to stop =
  `updatedDate < watermark`**. Using `<=` to decide "stop" risks a false-negative when a full page
  lands exactly on the watermark. Re-processing boundary ties is harmless (import is idempotent).
- **Verified against BB DC 9.4.18**: the PR list supports `order=NEWEST`/`OLDEST` (by `updatedDate`),
  pagination via `start`/`isLastPage`/`nextPageStart`, and each row carries `description`,
  `fromRef`, `state`, `closedDate`, `updatedDate`. No server-side "since" or text-search — but the
  `updatedDate` sort gives us the equivalent for early termination.
- The scrape reuses the ingest's ticket matcher (`MEGASPEED_TICKET_PROJECTS` / key regex), applied
  **early (on the list: title + branch + description)**, not just at store time.
- Correctness is unchanged: the tgz + import + `--verify` contract are identical; only *which*
  requests are made changes.
- **Build status (prd/44 DES-2)**: each kept PR costs **one** extra request,
  `/rest/build-status/1.0/commits/{head}` — the only source of the `build_status` events that make
  prd/40's `ci` flag live. A forge without the module (404) is treated as "no CI data", not a
  failure; the request is counted as `build-status` in `requests_by_kind`.

---

## Not implemented / accepted gap (prd/44 DES-4)

- **"The scrape's request budget"** covers **REST traffic only** (`requests` /
  `requests_by_kind`). The git cache traffic (clone/fetch/patch-id/range-diff for
  `push_class`, diff size and thread anchors) is issued by a separate process and
  is not counted in the budget/progress line (NFR-6). A scrape that looks
  affordable by the REST counter can still be expensive in git fetches.

Tracking: [`docs/review-adversarial.md`](../docs/review-adversarial.md) → **NFR-6**, **DES-4**.

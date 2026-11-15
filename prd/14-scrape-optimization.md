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

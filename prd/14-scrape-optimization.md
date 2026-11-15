# 14 — Scrape optimization (huge projects)

A huge Bitbucket project makes a full scrape (list all PRs + fetch each PR's activities + commits)
too slow and too request-heavy. The scrape must be cheap enough to run in the airgap, and must be
**focused on PRs & commits that pertain to a set of tickets**.

## Optimizations (layered)

1. **Ticket-key pre-filter (the big win).** The PR-list endpoint (paginated `limit=100`) returns
   cheap summary fields — `id`, `title`, `state`, `fromRef.displayId` (source branch),
   `updatedDate`, `author` — without the per-PR detail / activities / commits. Keep a PR only if
   its `title` **or** source branch matches a target ticket key. Only matching PRs get the
   expensive detail + activities + commits fetches. (Our convention puts keys in branch + title +
   commit + description, so title+branch catches them.)
2. **Incremental watermark.** Maintain a watermark = max `updatedDate` processed (per repo, in
   local state). On an incremental scrape, skip any PR whose `updatedDate <= watermark`.
3. **Closed-PR finality.** A MERGED/DECLINED PR with `updatedDate <= watermark` is *settled* — its
   event history is stable (no new commits/approvals after close, barring rare late comments).
   Scrape it once and mark it done; only OPEN (or recently-updated) PRs are re-checked each run.
4. **NEWEST-first.** Request the PR list `order=NEWEST`, so a time-limited scrape still captures
   the most recent activity first (early termination stays correct thanks to the watermark).

## Local state (scraper side)

- `<state-dir>/watermark.json` — per-repo `updatedDate` cursor (never goes backwards).
- `<state-dir>/prs.json` — per PR: `{state, updated_date, done}` (settled + fully scraped).

## Notes

- BB DC's PR list has no server-side "since" or text-search, so the filter is client-side on the
  cheap list fields — still a huge saving vs. fetching activities/commits for every PR.
- The scrape reuses the ingest's ticket matcher (`MEGASPEED_TICKET_PROJECTS` / key regex), applied
  **early (on the list)**, not just at store time.
- Correctness is unchanged: the tgz + import + `--verify` contract are identical; only *which*
  requests are made changes.

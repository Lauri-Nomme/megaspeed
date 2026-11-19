# 44 — Cross-QA reconciliation: the still-unfixed, still-un-PRDd findings

A reconciliation pass across every QA/review doc (`QA-REPORT`, `QA-UI-REPORT`, `QA-round2`,
`QA-mobile`, `QA-ux-phone`, `QA-exploratory`, `QA-fresh`, `QA-verify`, `review-product`,
`review-adversarial`, `review-round2`, `review-integration`, `review-performance`). Most findings are
fixed and PRD'd (prd/25–43). This PRD covers what is **still open and has no PRD**.

## 1. The unfixed, un-PRDd findings (reconciled)

| # | Source | Severity | Finding | Status |
|---|---|---|---|---|
| NFR-2 | review-round2 | Med | The Jira scrape re-fetches **every sprint's membership on every run** (`scrape_jira.py:415-418`) — no cursor, no date filter, O(all sprints) requests forever, even on incremental runs. | **unfixed** |
| DES-2 | review-adversarial | Med | `build_status` is never emitted (0 in both corpora), so **CI wait is unrepresentable** — the `ci` flag prd/40 added is latent; the documented "CI is a hidden bottleneck" is invisible. | **unfixed** |
| DES-5 | review-adversarial | Low | No **calendar fingerprint** in the payload — a saved report / "Copy as Markdown" / exported chart can't be re-derived after a vacation/holiday edit. | **unfixed** |
| DES-4 | review-adversarial | Med | **PRD↔code drift**: several `prd/` claims aren't implemented or enforced, but read as promises (no "accepted gap" marker). | **unfixed** |
| DES-6 | review-adversarial | Low | `objects.actor` means a different thing per type (author / resolver / self / pusher), so a generic `?actor=` query mixes them. | **unfixed** |
| — | review-performance | Low | Perf long-tail deliberately deferred: classification content-hash run-level memo, per-list-page fetch batching, `/api/queues` bucket cap. | deferred, no PRD |
| — | prd/26 | Low | Refresh cadence + rebuild/read-time in the provenance line. | deferred, no PRD |

## 2. Root causes

1. **Scrape cursors are uneven.** The Bitbucket side has per-repo `updatedDate` cursors; the Jira
   sprint/board side has none, so the cheap incremental path still pays a full sprint-membership walk.
2. **The closed vocabulary stops short of the real gates.** `build_status` exists in the taxonomy but
   nothing produces it, so a whole concurrency dimension (CI) is absent from the model even though
   prd/40 already added the `ci` flag to receive it.
3. **Reproducibility is claimed but not keyed.** The calendar is a third, unversioned input to every
   business-hours number — prd/02's "reproducible from classifier_version + content hash" is silently
   false for any figure that touched the calendar.
4. **Docs drift because the "not implemented" state has no home.** Each unfixed review item should be
   an explicit "accepted gap" in the PRD it concerns, not a silent promise.
5. **One column, many meanings.** `objects.actor` is overloaded; the model never disambiguates.

## 3. Improvement items (each with a test)

1. **Sprint-membership cursor** (`ingest/scrape_jira.py`): page sprints from the watermark
   (`startDate >= watermark - N`, active+future + last K closed), cache sprint metadata by id in the
   state dir, re-read membership only for sprints whose `completeDate` moved. Test: an incremental run
   fetches no closed-sprint memberships twice.
2. **Emit `build_status`** (ingest): map the commit build-status activity to `build_status` events, so
   the `ci` flag prd/40 added becomes live; surface the CI queue in the blocking graph. Test: a
   synthetic build-status stream produces `ci`-phase intervals and a CI node.
3. **Calendar fingerprint** (`analysis/calendar.py`, `server/app.py`): expose a short `calendar_id`
   (hash of the merged calendar config) in every analysis payload + `/api/health`, and include it in
   the "Copy as Markdown"/export provenance so a figure can be re-derived later. Test: a calendar edit
   changes the id and invalidates the cache.
4. **"Accepted gap" markers** (docs): add an explicit "Not implemented / accepted gap" section to each
   PRD that still reads as a promise but isn't (prd/02 free-text test, prd/13 artifact, prd/14 budget,
   prd/09 read-only `/api/eval`), linking the finding.
5. **Disambiguate `objects.actor`** (`store.py`/docs): document (and, where cheap, split) the per-type
   meaning of `actor`; a generic `?actor=` query states what it returns.
6. **Perf long-tail** (deferred, pick up opportunistically): classification content-hash run-level
   memo, per-list-page fetch batching, `/api/queues` bucket cap — see `review-performance.md`.
7. **Provenance line** (prd/26 deferred): refresh cadence + rebuild/read-time next to the snapshot.

## 4. Definition of done

Each item lands with a regression test and conforms to `docs/concepts.md`; the PRD-vs-code drift items
resolve the doc to match reality (implement or mark accepted-gap). Keep the suites green.

## Out of scope (blocked, not a code fix)

Tempo install (org licence) · production creds (`MEGASPEED_BB_*`/`_JIRA_*`) · full advice reconciliation
automation (the lab auto-running a scenario per rule) — a larger follow-up to prd/43.

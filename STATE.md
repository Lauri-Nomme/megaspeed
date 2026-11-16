# STATE — megaspeed · LIVE

Live behind nginx (→ `127.0.0.1:8766`), tenants. GitHub: `Lauri-Nomme/megaspeed` (private, scrubbed).
Tests: **321** (`tests/`) + **46** (`scrape/tests`) green. Worktrees: `ui-fixes`(merged), `jira-lab`(merged), `jira-ingest`(running).

## Scrape (airgapped) — full value in one pass

Ticket-set scoping · pre-filter + watermark + settled-PR finality · `push_class` (patch-id) · diff size ·
LLM classification via `qwen3-30b-a3b` (precision). Import = pre-write gate, batched (~90×). Business-hours
calendar (overrides/vacations/EE holidays) + vacation-blocking insight.

## Analysis → suggested rules — FIRES on real data now

`data/del2.duckdb` (the re-seeded corpus, full pipeline) → 1484 events, push_class
content=186/pure_rebase=28/conflict=12, 375 classified threads, diff size 131/131.
Analysis (business hours): **rework-loops** (p90 4 rounds), **comment-tax** (15% of threads rejected/walked
back, top type `question`), **conformance-rounds** fire. `rebase-waste` populated but honestly silent (0
approvals lost on pure rebase); `pr_size` populated but silent (large PRs aren't slower — honest negative).

## Jira

**Jira DC lab** up (`jira-lab` :8090, Jira 9.17.5, DEL project/epic/100 stories/sprints/native worklogs;
Tempo fell back to native worklogs). **Jira ingest** being built (worktree `jira-ingest`).

## Scrape reliability bugs found (QUEUED — from the 2 h scrape that died 3×)

1. `transport.py` doesn't retry `ConnectionResetError` → kills the run.
2. A crash loses everything (no incremental bundle/watermark save).
3. A 404 on `/pull-requests/{id}/commits` aborts the scrape (should skip the PR).
4. Silent partial-bundle success (5 PRs → "successful" bundle, exit 0).
5. Scrape raced an in-flight corpus rebuild (concurrency).

→ Fix after `jira-ingest` lands (both touch `ingest/`).

## Next / roadmap

- [ ] Merge `jira-ingest`; fix the 5 scrape-reliability bugs.
- [ ] Jira layer analysis (waiting-time decomposition, carry-over, worklog cross-check) once Story/Epic/Sprint land.
- [ ] Stacked-PR cascade · flow-efficiency/survival · simulation.

## Open (production)

- [ ] Production Bitbucket DC URL + auth (`MEGASPEED_BB_*`). [ ] Jira keys (`MEGASPEED_TICKET_KEYS`).
- [ ] Lab license expired (read-only works; pushes need clock-shift shim).

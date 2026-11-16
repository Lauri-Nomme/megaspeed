# STATE — megaspeed · LIVE

Live behind nginx (→ `127.0.0.1:8766`), tenants. GitHub: `Lauri-Nomme/megaspeed` (private, scrubbed).
Tests: **429** (`tests/`) + **65** (`scrape/tests`) green. CI builds a tagged release of the scraper.

## Scrape (airgapped) — full value in one pass, now reliable

- **Bitbucket**: ticket-set scoping · pre-filter + watermark + settled-PR finality · `push_class` (patch-id)
  · diff size · LLM classification (`comment_type`/`thread_outcome`/`question`+`phase`) via `qwen3-30b-a3b`.
- **Jira**: Story/Epic/Sprint objects + status/sprint/assign/point/worklog events, cross-source identity
  (one pseudonym per human across Jira+Bitbucket), worklog-noise signal (`log_delay_bucket`).
- **Reliability**: transport retries resets/broken-pipe · incremental checkpointing + crash resume ·
  per-PR failure skips (not aborts) · partial-bundle guard · quiescence check. Import = pre-write gate, batched (~90×).

## Analysis → suggested rules

9 + 2 metrics → ranked suggestions. Business-hours calendar (overrides/vacations/EE holidays) +
vacation-blocking. **Flow efficiency** (18.8% on del3 — 81% waiting, 91% of that review) + **Kaplan–Meier
survival** (censored cycle time, +41% bias on the slow store). On `del3`: flow-efficiency, rework-loops,
comment-tax, conformance-rounds, cycle-time-censoring fire; rebase-waste/pr_size/sentinel are honest
negatives (data doesn't justify them).

## In progress (worktree)

- [ ] **Jira-layer analysis** (`jira-analysis`) — waiting-time decomposition (8 segments), carry-over,
      point churn, worklog cross-check, Jira conformance.

## Next / roadmap

- [ ] Stacked-PR cascade (emit the stack DAG in ingest + cascade cost in analysis).
- [ ] Simulation (policy lab — what-if).
- [ ] Re-point the live 8766 server at the richer `del3` store (optional; it still serves the old `del`).

## Labs

- Bitbucket: bb-lab-a :7990 (FIX/golden), bb-lab-b :7991 (DEL, re-seeded, full features).
- Jira DC: `jira-lab` :8090 (DEL project/epic/100 stories/sprints/native worklogs; Tempo fell back to native).

## Open (production)

- [ ] Production Bitbucket DC + Jira URL/auth (configurable via `MEGASPEED_BB_*`/`MEGASPEED_JIRA_*`).
- [ ] Jira keys (`MEGASPEED_TICKET_KEYS`). [ ] Lab licenses expired (read-only works; pushes need clock-shift).

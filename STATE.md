# STATE — megaspeed · LIVE

Live behind nginx (→ `127.0.0.1:8766`), tenants. GitHub: `Lauri-Nomme/megaspeed` (private, scrubbed).
Tests: **497** (`tests/`) + **65** (`scrape/tests`) + **57** (node) green — except `test_jira_live` (env: jira-lab licence expired).

## Scrape (airgapped) — full value, reliable

- **Bitbucket**: ticket-set scoping · pre-filter + watermark + settled-PR finality · `push_class` (patch-id) ·
  diff size · LLM classification (via `qwen3-30b-a3b` on precision). Reliability: reset retries, checkpoint/
  crash-resume, per-PR skip, partial-bundle guard, quiescence check. Import = pre-write gate, batched (~90×).
- **Jira**: Story/Epic/Sprint objects + status/sprint/assign/point/worklog events, cross-source identity
  (one pseudonym per human), worklog-noise (`log_delay_bucket`).

## Analysis → suggested rules (the whole pipeline answers prd/15 Q1–Q4)

- Ball-in-court → blocking/queue/delivery · **business-hours calendar** (overrides/vacations/EE holidays) ·
  vacation-blocking · **flow efficiency** (18.8% — 81% waiting, 91% review) · **Kaplan–Meier survival** ·
  **Jira-layer** (8-segment waiting decomposition, carry-over, point churn, worklog cross-check) ·
  **stacked-PR cascade** (ref digests + DAG + cascade) · **simulation** (calibrated SimPy + validation gate).
- On `del3`: flow-efficiency, rework-loops, comment-tax, conformance-rounds, cycle-time-censoring fire;
  rebase-waste / pr-size / sentinel / stacked are **honest negatives** (data doesn't justify them).
- **Simulation** validates (baseline −11.3% vs history). Strongest lever: **review SLA 2 h → −18%** cycle;
  WIP-1 → −14% (but +lead time); sentinel +1 → −9% (within noise); **size cap → +170%** (honest negative).
  Bottleneck is always the sentinel gate, not headcount → "attention latency, not more reviewers".

## In progress

- Nothing running. Roadmap complete except: combined Bitbucket+Jira store (blocked on jira-lab licence),
  production wiring.

## Next (pick when you're back)

1. **Re-license `jira-lab`** (licence reverted → `/rest/agile` 403) → then build a **combined
   Bitbucket+Jira store** so the Jira-layer analysis fires on real data (del3 has PRs only).
2. Production Bitbucket DC + Jira URL/auth (`MEGASPEED_BB_*` / `MEGASPEED_JIRA_*`) + ticket keys
   (`MEGASPEED_TICKET_KEYS`).
3. Optional polish: re-point 8766 at `del3` (already a tenant, just switch), point the UI at the richest store.

## Worktree note

Branches all merged to `main`. Leftover worktrees (`ui-fixes`, `jira-lab`, `jira-ingest`, `flow-survival`,
`jira-analysis`, `stacked-cascade`, `simulation`) can be pruned with `git worktree prune`.

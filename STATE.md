# STATE — megaspeed · LIVE

Live behind nginx (→ `127.0.0.1:8766`), default tenant `del-combined` (Bitbucket + Jira, backdated).
GitHub: `Lauri-Nomme/megaspeed` (private, scrubbed).
Tests: **706** (`tests/`) + **73** (`scrape/tests`) + **101** (node `app/test`) green.

## Scrape (airgapped) — full value, reliable

- **Bitbucket**: ticket-set scoping · pre-filter + watermark + settled-PR finality · `push_class` (patch-id) ·
  diff size · LLM classification (`qwen3-30b-a3b` on precision). Reliability: reset retries, checkpoint/
  crash-resume, per-PR skip, partial-bundle guard, quiescence check. Import = pre-write gate, batched (~90×).
- **Jira**: Story/Epic/Sprint objects + status/sprint/assign/point/worklog events, cross-source identity
  (one pseudonym per human), worklog-noise (`log_delay_bucket`).
- **Workflow stages (prd/29)**: configurable status→stage projection — `backlog`/`ready`/`in_progress`/
  `in_review`/`in_verification`/`done`/`other` — via `[jira.stages]` (TOML) or `MEGASPEED_JIRA_STAGES`.
  Precedence: config name map → Jira category → name rules. The demo store (`del-combined`) is now
  generated through the install's real workflow (Story: OPEN→READY FOR REFINEMENT→REFINED→IN PROGRESS→
  IN REVIEW→IN VERIFICATION→CLOSED; Bug: NEW→TRIAGED→…→RESOLVED), so `in_review`/`in_verification`
  boundaries and the QA segment are measured on live data (e.g. DEL-167 `verification→done` = 8.0 h).
- **Scope**: `MEGASPEED_TICKET_KEYS` > `MEGASPEED_JIRA_EPICS` (resolves epics → children, one extra search)
  > `MEGASPEED_JIRA_PROJECTS`. Scope rides the manifest → `--verify` enforces it with only the epic env set.
- **Cloud Jira** (`--jira-cloud`): `proxy/cloud_jira_proxy.py` emulates Cloud v3 over the DC lab
  (Bearer PAT, cursor pagination, name-discovered custom fields, Tempo stub); verified byte-equivalent.

## Analysis → suggested rules (answers prd/15 Q1–Q4)

- Ball-in-court → blocking/queue/delivery · **business-hours calendar** (overrides/vacations/EE holidays) ·
  vacation-blocking · **flow efficiency** · **Kaplan–Meier survival** · **Jira-layer** (waiting
  decomposition — 8 segments, or 9 with the QA split — carry-over, point churn, worklog cross-check) ·
  **stacked-PR cascade** · **simulation** (calibrated SimPy + validation gate).
- **Simulation** validates baseline within ±20% (median) on both `del3` and `del-combined`, all 8 seeds.
  `QUEUE_RECOVERY=0.6` (new, disclosed) fixes the median under-prediction — root cause was the Poisson
  P-K queue over-estimate, not `SERVICE_SHARE`.

## UI — desktop-first AND phone (prd/28)

- **Ticket-centric view** `#ticket/KEY` + `GET /api/ticket/{key}` — "why did DEL-167 take 3 weeks",
  8-segment decomposition, honesty-censored prose.
- **Rules drill-down** (every suggestion expands to its full item list) · **sprint lens** (`/api/sprints`,
  bands/scope/carry-over) · timeline/blocking/queue/delivery/overview/sources/policy-lab.
- **Responsive** (≤640px): off-canvas drawer, two-row top bar, bottom-sheet detail, pinch/pan/tap touch,
  list-first entry + card-stacked tables. Desktop (1600px) and medium (900px) verified pixel-identical.

## Deferred / not urgent

1. **Tempo** install in jira-lab (cloud add-on absent, currently stubbed over native worklogs).
3. **Production creds**: `MEGASPEED_BB_*` / `MEGASPEED_JIRA_*` / `MEGASPEED_JIRA_CLOUD_*` (DC labs only now).
4. **Keepalive cron** for jira-lab (licence = 3 h rolling window per restart; re-arm via
   `sudo nerdctl restart jira-lab`).
5. Light theme / theme toggle / refresh cadence / rebuild-read-time provenance (prd/26 deferred).

## Important

- `jira-lab` container's application data is bind-mounted from
  `~/dev/megaspeed-wt/jira-lab/jira-lab/home/jira-lab` (UID 2001). **Do not delete that directory** — it
  is the live DC lab's home, even though the worktree record is gone. All other worktrees pruned.
- Harness token: `MEGASPEED_HARNESS_TOKEN` / `MEGASPEED_HARNESS_TOKEN_FILE`; the default token file is
  created exclusively (first writer wins) so concurrent servers no longer clobber each other.

# STATE — megaspeed · LIVE

Live behind nginx (→ `127.0.0.1:8766`), default tenant `del-combined` (Bitbucket + Jira, backdated).
GitHub: `Lauri-Nomme/megaspeed` (private, scrubbed).
Tests: **1403** (`tests/`) + **99** (`scrape/tests`) + **553** (node `app/test`) green.

## v0.12.0 — the decision layer (prd/58–64)

- **Constraint verdict (prd/58)** — an Overview-top card names ONE constraint by one canonical clock:
  *Approval gate*, removable **619 h** (live 237 h + tail 382 h), with the exploit→subordinate→elevate
  ladder. Exploit is open (chase #53/#55/#60), elevate stays **blocked** while items are owed; the
  sentinel lab says "no clear effect" rather than inventing a win. Rules keeps lower-ranked levers
  behind "Rules for the next constraint".
- **Aging WIP (prd/59)** — in-flight age against the team's own completed-work p50/p85/p95, the chase
  list (ranked by hours past p85), and the fixed stale/sentinel constants replaced by self-calibrated
  thresholds (`MEGASPEED_AGING_THRESHOLDS=fixed` restores the old ones).
- **Intervention-aware trend (prd/61)** — team annotations in one allowlisted store table (the sole
  first-party free-text carve-out; imported content is still rejected), before/after distributions with
  bootstrap CI + Mann-Whitney and the four honest outcomes ("can't tell at this n"), and Theil-Sen
  stability flags (`filling` today). prd/60's forecast will refuse on `overload`.
- **Rolled first-pass yield (prd/62)** — %C&A per step and rolled (7.6 % at 90 d, 8.6 % at 120 d),
  a DORA-style stability panel (reopen 5.5 %, failed verification 4.1 %, review bounce 23.3 %), and a
  measured guardrail beside every speed lever; "held constant" is printed with its baseline.
- **Throughput forecast (prd/60)** — a seeded 10,000-resample bootstrap over weekly **stories**
  delivered (never points), with the interval as the headline (`20–30 of 30 · by 15 Dec`, P50 26,
  24.5 % chance of all 30), the P85 date, the at-risk adjustment, and three honest refusals
  (sparse / erratic / WIP overload — reading prd/61's flag). del-combined is `filling`: it forecasts,
  but says the interval is optimistic and to plan at P85.
- **QA hardening (prd/64)** — all 22 findings of the v0.11.1 free-exploring pass: phone clipping/touch
  targets, the `person:CaOs` holder leak, no fabricated refinement cost on a Jira-less store, Overview
  no longer mutates with a Queue toggle, affected-first rule drills, ticket detail age-vs-measured and
  post-close PRs, sprint on-time floor by committed stories, and the baked version.
- **Release QA (prd/65)** — the 10 findings on the new surfaces: repo-scoped aging rows, one
  censoring sentence, the forecast's degenerate-label collision, the phone Trend bar, the annotation
  flash echo, scope-consistent aging counts/chart/re-plan, non-colliding percentile labels, the
  policy-lab guardrail parity, and the desktop column widths. `docs/QA-v012-final.md` records
  22/22 + 10/10 FIXED, 0 regressions, desktop and phone, both themes.

## v0.15.1 — visible text-classifier endpoint and fallback reason

A run with a wrong `--llm-url` silently fell back to mock for all 15,540 texts
(the compiled default is the precision-network box). The start line now prints
`ollama:model@url`, the first fallback prints its reason once, and
`text_classifier.last_error`/`url` ride in the manifest, so a 100 %-fallback run
cannot look healthy at the end.

## v0.15.0 — key-scoped hans capture

A hans capture is its own scope (keyless PRs are kept), which made a 10,733-PR
repo capture classify every PR's comments. `--keys-only` restores the ordinary
ticket scope: a PR whose keys are not in `MEGASPEED_TICKET_KEYS` /
`MEGASPEED_TICKET_KEYS_FILE` is dropped **before** activities and text
classification (the cheap key prefilter runs first; the commit log is still read
when the list row has no key). The team's 2,391 resolved keys are in
`ollama-export/demo-team-keys.txt` (sha256 9f490819…).

## v0.14.5 — hans merge-base accelerator + cheap list/limit

Three fixes from the real-capture profile:
- `--limit` was ignored on the hans path (only `--n` capped it), so `--limit 1`
  walked the whole capture;
- `HansTransport._list_values()` computed `megaspeedDiffBase` for **every row in
  the list page** before the limit could stop (two git processes per row);
- per-PR `git merge-base` spawns are replaced by hans's libgit2 **batch**
  accelerator (`tools/bb-merge-base`, pre-built per platform): one process,
  all (target, head) pairs (`[hans-merge] 10 pair(s) in 0.00s` on golden10),
  with a git fallback. Merged-PR parents stay lazily resolved with one
  `rev-list --ancestry-path`. `MEGASPEED_GIT_TRACE=1` logs every remaining git
  call and the accelerator batch.

## v0.14.4 — hans --limit honoured + git tracing

`--limit` was ignored on the hans path (only the legacy `--n` capped it), so
`--limit 1` walked the whole capture. `--limit` now caps every source. With
`MEGASPEED_GIT_TRACE=1`, every mirror git command prints its duration and output
size, so a slow step in a large capture is visible instead of a blank console.

## v0.14.3 — hans merge-base in one query

`HansTransport._merge_parent()` walked the target's entire first-parent chain and
spawned `git merge-base --is-ancestor` **per commit** to find the merge; on a
multi-year mirror the first merged PR alone could take hours (python busy, git
idle between spawns). It now asks
`git rev-list --first-parent --ancestry-path <head>..<target>` once (plus
`rev-parse <merge>^1`) and caches the chain for the old-semantics fallback.

## v0.14.2 — scrape progress with a slow text classifier

The Bitbucket/hans scrape only ticked at PR boundaries, so a 30B Ollama batch on
the first PR (up to 180 s per text) looked like a hang. It now prints an immediate
start line (source · repos · output · classifier mode/model/workers), a
"classifying text" line for the first PRs, and a per-text heartbeat (first text,
then every 10), while fixture runs stay silent. No bundle change.

## v0.14.1 — v0.14.0 QA fixes (prd/68)

16 findings from the Jira-only QA: the Queue crash on an unscoped empty response, dateless/future
sprints (`dates unknown`, focus filters, no inverted bands), person scope on Aging (Jira assignee as
holder), the policy lab falsely disabling refine, day counts printed as hours, the re-plan section under
`only=live`, the no-live-PR count, the duplicate verdict disclaimer, the false “every ticket merged”,
PR-only trend tabs on a 0-PR store (now unavailable; cumulative flow plots story bands), the epic
Markdown export, Sources’ PR-only copy, aging-column clipping and timeline label/phone overflow.

## v0.14.0 — window scoping (prd/67)

The first Jira-only corpus (2,320 stories, 106 sprints, 252 people, no PRs) exposed four all-time views
in a windowed product: timeline actor lanes and the who-is-who legend listed every person ever seen
(now window-scoped, with a hidden count); the aging table returned every open story regardless of the
window (now active-in-window, live by default, dead rows in re-plan); and the constraint verdict named a
station with **0 h owed** as "the constraint" because it ranked the historical tail (now owed-now first;
when nothing is owed it says "no current queue" and lists the historical tails). The carry-over card now
states both scopes: "7 of 36 in the 1 sprint closed in this window" beside "1,607 across 99 sprints".

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
- **Team scoping (prd/66)**: `MEGASPEED_JIRA_TEAMS` is an **AND** criterion alongside projects / epics /
  keys (teams *within* those), resolved once through the team field. "Team" is not a standard Jira field:
  it is the Atlassian Teams custom field (`com.atlassian.teams:rm-teams-custom-field-team`), discovered
  by name or pinned with `MEGASPEED_JIRA_TEAM_FIELD`. On Cloud the Teams field compares a team **id**
  (`"Team[Team]" in (<id>)`), so the inspector ranks the populated field first and prints the name, the
  id and the JQL; names are resolved through JQL autocomplete where supported. `python3 -m
  ingest.jira_fields --issue KEY` (or `--list`) prints the field id and the exact settings to use. Cloud auth
  accepts either an account e-mail + API token (Basic) or a Bearer PAT; a blocked `/serverInfo` falls
  back to `/myself`, and a total 401/403 prints the method sent and the env vars to fix.
- **Cloud Jira** (`--jira-cloud`): `proxy/cloud_jira_proxy.py` emulates Cloud v3 over the DC lab
  (Bearer PAT, cursor pagination, name-discovered custom fields, Tempo stub); verified byte-equivalent.
- **Tempo worklogs (prd/30)**: DC + Cloud read Tempo timesheets (`/rest/tempo-timesheets/4/worklogs/{key}`)
  via `--tempo` / `MEGASPEED_JIRA_TEMPO` / `[jira] tempo`; reduced `worklogged` events byte-identical to
  native; Tempo Cloud `createdAt`/`startDate` mapped to the native shape. (Real Tempo install = licence.)
- **Hans import (prd/31)**: `--hans-scrape <dir>` ingests Bitbucket from a hans scrape dir (REST dumps +
  git mirror), deriving commits/diff/push_class from the mirror and comments from activities; source
  label `hans` (the only source allowed to import a keyless PR).

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
- **Rules drill-down** (every suggestion expands to its full item list, impact-sorted; the at-stake
  headline counts each cost pool once) · **sprint lens** (`/api/sprints`, bands + story filter + URL,
  done-by-end/late/never, carry-over at the close) · timeline/blocking/queue/delivery/overview/sources/
  policy-lab (work-unit throughput + a verdict per scenario row).
- **Rules ↔ policy-lab reconciliation (prd/43)**: each rule whose primary advice is a lab preset carries
  the lab's own verdict ("models this at +N % cycle — not a measured win", "no clear effect", or the
  supported %), a **review-round-cap** rule ranks on the measured rounds↔cycle correlation (r ≈ 0.6),
  and the page states the lab's best scenario. The sentinel's response clock is gate-aware (last peer
  approval → response, ~6 h), the ticket-page Timeline chip scopes to the ticket, the overview tile
  counts people only, and sprint "by end" excludes Jira-done stories whose PRs never merged.
- **Constraint verdict (prd/58)**: Overview opens with **one** claim — the station with the largest
  *removable* wait pool (live in-station age + the completed stories' hours above the station's own p85)
  on the story clock, with its PR-ball-clock holder split, its ties and its adjacent findings (no-live-PR
  stories, Done-in-Jira-but-unmerged PRs). The exploit → subordinate → elevate ladder carries the policy
  lab's own verdict per rung and **blocks** elevate while exploit items are owed; Rules shows the same
  verdict as a banner, keeps the current rung's levers and the `measured` diagnostics in place, and
  discloses every other lever behind "Rules for the next constraint" (a persisted Show-all toggle).
  On del-combined the verdict is review / approval gate: 619 h removable (238 live + 382 tail), not
  refinement's 2,420 h of history; exploit #53/#55/#60, round cap −13 %, SLA −9 %, sentinel "no clear
  effect".
- **Responsive** (≤640px): off-canvas drawer, two-row top bar, bottom-sheet detail, pinch/pan/tap touch,
  list-first entry + card-stacked tables. Desktop (1600px) and medium (900px) verified pixel-identical.
- **Phone UX (post-review)**: ticket keys visible on the timeline (epic→story→PR hierarchy at every zoom,
  `DEL-100 #1` PR rows), one-finger vertical scroll over charts with horizontal-pan + pinch preserved, no
  right-edge clipping or vertical word-wrap (`scrollWidth` 412 on every view; `docs/QA-ux-phone.md`).
- **Versioning (prd/32)**: `v0.9.0` tagged on main; `/api/meta` + the meta strip show `v0.9.0 · <sha>`
  (git describe → `VERSION` file → hardcoded fallback, `scraper-*` tags excluded).

## Review model (prd/40, the middle ground)

- **Participant-status vector + debt + ready** in `analysis/ballcourt.py`: `status[r]` per roster
  member, `debt` (whose objection stands), a `ci` flag; `pending`/`author_open`/`ready`/`ball` are
  derived. `holders` = author-while-in-debt ∪ pending is the truth; `ball` is a priority label.
- Measured on the corpora (merged PRs, wall-clock): standing-approval person-hours **1380 → 0**
  (del-combined) and **411 → 0** (megaspeed); push-unapproval author hours **48 → 0** / **146 → 0**;
  `ready` carved out of review wait **1340 h** / **1689 h**; sentinel pending hours **8620 → 4063** /
  **823 → 562** with `sentinel_after_peers` (default on, `MEGASPEED_SENTINEL_AFTER_PEERS=0` off).
- Per-holder calendar pricing (`holder_hours`), episode-level responsiveness/sentinel medians,
  `solo_hours`, pending-at-merge `unrequired`, `ready`/`doubly_blocked`/`ready_since` in the delivery
  rows, and the one-line PR drawer state. `_rounds` follows `concepts.md` §6 (del-combined 250 → 188,
  megaspeed 87 → 48) in both `rules` and `jira`.

## Performance (prd/33, from the adversarial review)

- **queue_series** difference-array (82.7 s → 419 ms @1 h buckets, byte-identical) · **business_hours/`_cycle`
  memoization** (1.94× on `/api/insights`) · **gzip middleware** (22.4 MB → 1.14 MB) · **`/api/ticket` cache**
  (warm 0.33 s → 15 ms) · **relations JOIN** (2.79 s → 1.34 s).
- **Scrape**: incremental checkpoints (4,000 PRs 44.6 s → 5.7 s) · git-cache memoization (26 → 11 git
  runs/PR, warm 0 fetches) · bounded LLM concurrency (`--llm-workers`, 3.5×).
- **Timeline canvas virtualisation**: canvas is a window on the lane list (bounded height, only visible
  rows drawn) — 5,240 PRs render in ~4 ms instead of blanking past the 65,535 px limit; desktop + phone.
- **`/api/ticket` cold-path**: a per-store analysis bundle (court + Jira layer) is built once and shared
  across `/api/ticket`/`/api/insights`/`/api/sprints` — new keys drop 41.7→15.5 ms (6× at 1k stories).
- **Light theme + toggle**: design tokens on `:root` (dark, unchanged) overridden by `[data-theme="light"]`;
  canvases recolour via tokens; toggle in the top bar (44px on phone); persisted + `?theme=` deep link.

## Deferred / not urgent

1. **Tempo install** in jira-lab (code path done; real install blocked on the org's Tempo licence —
   the Marketplace JAR download is empty without one).
2. **Production creds**: `MEGASPEED_BB_*` / `MEGASPEED_JIRA_*` / `MEGASPEED_JIRA_CLOUD_*` (DC labs only now).
3. **Perf long-tail** (deliberately deferred): classification content-hash run-level memo, per-list-page
   fetch batching, `/api/queues` bucket cap for huge windows — see `docs/review-performance.md`.

## Exploratory QA — findings fixed and re-verified

Terminology is pinned in **`docs/concepts.md`**. The exploratory test (`docs/QA-exploratory.md`, 25
findings) was classified into PRDs and implemented: prd/34 (bots/sentinel/scope), prd/35 (d=8 h),
prd/36 (lifecycle labels, reopens, 9/9, per-story), prd/37 (sprint done-by-end, additive at-stake,
impact-sorted drill-downs, stale-PR/sentinel-tail/sprint-on-time rules), prd/38 (clickable PR rows,
ticket search, landing, drawer, sources), prd/39 (PR-size histogram + sliders).

A **fresh adversarial QA** (`docs/QA-fresh.md`, same three questions, no prior findings) confirmed
~20 of 25 fixed, then a second-order pass landed: prd/41 (abandoned zombie PRs, threshold/footer
counts, tied-segment + "at stake (assumed)", #53 0/2 pending, policy verdict signs, sprint drill-down
scope, solo-hours) and prd/42 (response-time comparison, done-but-open list, ticket benchmark, PR-drawer
completeness, size-vs-cycle, policy knobs, sprint scope-creep). Comparison: `docs/QA-comparison.md`.

## Important

- `jira-lab` container's application data is bind-mounted from
  `~/dev/megaspeed-wt/jira-lab/jira-lab/home/jira-lab` (UID 2001). **Do not delete that directory** — it
  is the live DC lab's home, even though the worktree record is gone. All other worktrees pruned.
- Harness token: `MEGASPEED_HARNESS_TOKEN` / `MEGASPEED_HARNESS_TOKEN_FILE`; the default token file is
  created exclusively (first writer wins) so concurrent servers no longer clobber each other.

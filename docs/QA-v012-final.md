# Final Verification + Adversarial QA Pass — megaspeed `v0.12.0` RC

- **Commit SHA tested:** `19287c5` (`19287c59`, app reports `v0.11.1-27-g19287c59` via git checkout and `v0.12.0` via `git archive` / `VERSION`)
- **Tenants verified:** `del-combined` (2,529 events · 131 PRs · 100 stories · 3 sprints) and `del3` (1,484 events · 131 PRs · 0 stories · 0 sprints), isolated under `/tmp/opencode/qa-v012-final/`
- **Viewports & themes:** Desktop (`1600×1000`) and Phone (`412×915`), Dark and Light themes
- **Automated test suites:** Python `tests` (all pass), Python `scrape/tests` (all pass), Node `app/test/*.mjs` (469/469 pass)
- **Screenshots:** `/tmp/opencode/qa-v012-final/shots/01-overview-verdict-dark.png` … `11-phone-forecast-dark.png`

---

## 1. Executive Verdict

**Verdict: Conditional ship (shippable as `v0.12.0` once 2 new-surface bugs are triaged/fixed) — all 22 `prd/64` findings are 100 % FIXED with zero regressions to `v0.11.1` surfaces, while the new `v0.12.0` decision layer (`prd/58`–`62`) has 2 functional bugs on the new Aging and Forecast surfaces (`N-01` repo-scoped story aging rows filter to 0 client-side; `N-02` degenerate `30–30` forecast bar overlaps end labels as `"3030"` and omits `Copy as Markdown`).**

---

## 2. `prd/64` Checklist (`F-01` … `F-22`)

Every finding from `prd/64-v011-explore-fixes.md` and `docs/QA-explore-v011.md` was reproduced against the `19287c5` build.

**Summary counts:** **22 FIXED · 0 PARTIAL · 0 NOT FIXED**

| ID | Original Finding | Status | Verification Evidence (`19287c5`) |
| --- | --- | --- | --- |
| **F-01** | Phone (`412×915`) Delivery list & Queue response cards clip values off-screen (`min-width: 1120px` / `900px`) | **FIXED** | At `412×915` (dark & light), `documentElement.scrollWidth === 412`, `.table.delivery` `scrollWidth === 412`, `.response-table` `scrollWidth === 412`. Every cell bounding box has `left: 21, right: 391, width: 370` (`anyOffscreen: false`). See `07-phone-delivery-dark.png`, `08-phone-queue-dark.png`. |
| **F-02** | Phone tap targets `< 44px` (Blocking summary, policy knob row, YAH, ticket jump, skip link, timeline summary) | **FIXED** | Measured bounding boxes at `412×915`: `#blocking-view summary` `412×44px` (`min-height: 44px`), `.policy-knob` rows `388×61..83px` (`min-height: 44px`), `.yah-step` `100×44px`, `#ticket-jump-input` `344×44px`, `#ticket-jump button` `44×44px`, `.skip-link` `116×44px`, `#sprint-strip summary` `328×44px` & `356×44px`. |
| **F-03** | `del3` fabricates 1 h refinement and headlines `"refine before start — −25 % cycle"` | **FIXED** | On `del3`, `/api/simulate` returns `calibration.refinement: {"hours": null, "n": 0, "basis": "no measured created → ready segment in this cohort"}`. `/api/insights` ranks `best: {"id": "sla4", "label": "review SLA 4 h", "cycle_percent": -8.3}`. In UI (`?tenant=del3&view=policy`), `#policy-knob-refine_before_start` and `[data-preset="refine"]` both have `disabled === true` and render `"not measurable (no Jira layer)"`. `del-combined` remains `+82 % cycle — not a measured win`. |
| **F-04** | Overview tiles + subtitle mutate after toggling Queue/Blocking level | **FIXED** | Overview fetches canonical `OVERVIEW_QUEUE_LEVEL = 'person'` and `OVERVIEW_BLOCKING_LEVEL = 'queue'` into `state.overviewQueues` / `state.overviewBlocking`. After switching Queue to `queue` and Blocking to `person` and returning to Overview, the DOM text is byte-identical (`≈304 d 6 h MEASURED WAIT · role-level handover graph, each handover once`, `0 OVERLOADED PEOPLE`). |
| **F-05** | Rules `people (pseudonyms)` tab keeps team stats and repeats empty message twice | **FIXED** | Clicking `people (pseudonyms)` updates `#rules-stats` to `"0 person rules (10 team) 0 critical ≈0 min at stake …"` and renders `"No person-level rule crosses its threshold in this window"` exactly once (`emptyMsgCount === 1`). |
| **F-06** | Rules drills lose `affected`-first order; `round-cap` marks 0 affected; sprint `still open` unaffected | **FIXED** | Verified all rule drill-downs on `del-combined` (`1q`): every drill-down has `affectedFirst: true` (monotonic `tr.affected` ordering). `round-cap` (`REVIEW ROUNDS`) marks `24 affected` of `124 items`; `cycle-time-censoring` (`TIME TO MERGE`) lists the 3 affected open PRs (`#53`, `#55`, `#60`) at rows 1–3; `worklog` lists its 4 affected stories (`DEL-167`, `DEL-147`, `DEL-187`, `DEL-199`) at rows 1–4; `flow-efficiency` lists its 64 affected PRs first; `sprint-on-time` (`SPRINT DELIVERY`) marks all `15 affected` of `15 items` (12 late + 3 still open). |
| **F-07** | `person:CaOs` includes PR `#66` (`DEL-183`), which waits on `BoFe + FrMo` | **FIXED** | Under `scope=person:CaOs`, Delivery list shows `Blocked 0 · Oldest waiting 1 · All 1` containing only `DEL-167 #53 delivery-api` (`CaOs (review) + FrMo (sentinel)`). PR `#66` is excluded on both Overview and Delivery list, and Overview shows `0 BLOCKED`. |
| **F-08** | `Done in Jira, PR open` panel ignores `role:*` scopes (`role:sentinel` showed `"none"`) | **FIXED** | Under `scope=role:sentinel`, `Done in Jira, PR open` on both Overview and Delivery list renders all 3 matching rows (`DEL-183 #66`, `DEL-167 #53`, `DEL-168 #55`). |
| **F-09** | Invalid `?view=` notice overwritten by `loadTenants()`, inherits stale page, and sticks on edits | **FIXED** | Navigating to `/?tenant=nonexistent&view=bogus&tf=99y` with `localStorage.getItem('megaspeed.page') === 'delivery'` lands on `state.page === 'overview'` and displays both notices joined in `#route-notice`: `"This link names view “bogus” and timeframe “99y”… · No dataset “nonexistent” on this server — showing del-combined…"`. Changing timeframe to `4w` clears `#route-notice` (`hidden: true`). |
| **F-10** | Leaving Timeline with an active Sprint lens leaves all timeframe preset buttons unpressed | **FIXED** | Activating `S1` on Timeline shows `tf-custom` and unpresses presets on Timeline; switching to Overview hides `tf-custom`, shows `20 Aug → 18 Nov`, and sets `1q` to `aria-pressed="true"` (`.on`). |
| **F-11** | Desktop `#delivery-detail` inspector buttons (`Timeline`, `Blocking map`, `Copy as Markdown`) do nothing | **FIXED** | Selecting `#66` on desktop Delivery list (drawer closed) and clicking `#delivery-detail [data-detail="copy"]` copies `## [DEL-183](http://127.0.0.1:8794/?tenant=del-combined#ticket/DEL-183) #66 delivery-api…`; clicking `Timeline` navigates to `timeline` with `scope: {kind: "ticket", value: "DEL-183"}`; clicking `Blocking map` navigates to `blocking` and focuses the node. |
| **F-12** | PR drawer repeats 4 `<dt>` facts with contradictory values (`waiting on author: yes · 2 d 1 h` vs `—`) | **FIXED** | Opening `#66`'s drawer produces 26 `<dt>` keys with `allUnique: true`. Live status shows `waiting on author: yes`, `CI: not running`, `rebasing: no`, `no events since: 2026-11-13 13:48 UTC`, followed by `PHASES: measured spans · business hours`, `waiting on author · measured: —`, `in review · measured: 2 h 5 min`, and `unobserved (no events since the snapshot): 2 d 1 h`. |
| **F-13** | Clearing the Sprint lens leaves Timeline trapped in the sprint window under `custom` | **FIXED** | Clicking `[data-sprint-clear]` (or toggling the active sprint row) resets `state.sprint = null`, `state.timeframeCustom = false`, hides `tf-custom`, re-presses `1q`, and restores `#range` to `20 Aug → 18 Nov`. |
| **F-14** | `DEL-183` ticket page omits that PR `#66` opened 6 d 4 h after the ticket closed | **FIXED** | `#ticket/DEL-183` narrative and `#66` PR row both state: `"PR #66 opened 6 d 4 h after the ticket closed — the work cannot have been part of the delivered story"` with pill `opened after the ticket closed`. See `06-ticket-del183-light.png`. |
| **F-15** | `DEL-161` headlines `32 d 4 h cycle` beside `2 d 0 h (30%)` and omits approved-unmerged `#34` | **FIXED** | `#ticket/DEL-161` stats strip reads: `"32 d 4 h age (open) · 7 d 0 h measured across 7/10 segments · 1 PR · 2 points · most in created → ready (30% of measured)"`. Both narrative and `#34` PR row state: `"approved by DaKo + FrMo and closed unmerged by the bot BiBi — 23 d 3 h lost"`. |
| **F-16** | Queue `NEVER` cell `8 (3 only) +5 (21 d 4 h)` reads as if `+5` is aged `21 d 4 h` | **FIXED** | Queue response-time table renders `FrMo sentinel` `NEVER` cell as: `"8 (3 only) +5 (oldest of 8: 21 d 4 h)"`. |
| **F-17** | Trend `Sprint on-time delivery` permanently fails `n≥2` floor on `bucket=sprint` | **FIXED** | Tab is titled `"Sprint story delivery share"`. On `bucket=sprint`, `#trend-stats` headlines: `"50.0% latest (S2 · 15/30 stories) · 2/3 buckets measured · −10.0 pts since S1 · S3 in progress (not in the headline)"`. On `bucket=week`, it keeps the `n≥2` closed-sprint floor (`"– no bucket meets the n≥2 floor (the latest measured bucket, 5 Oct, has n=1)"`). |
| **F-18** | Rules 04 (`round-cap`) and 10 (`sentinel-tail`) print raw floats (`6.01`, `283.6`) in evidence chips | **FIXED** | Rule 05 (`REVIEW ROUNDS`, `round-cap`) renders `per extra round: ≈6 h 1 min`; Rule 10 (`SENTINEL TAIL`) renders `hours: ≈35 d 3 h`. |
| **F-19** | Rules Markdown export self-attributes rule #7 (`"counted in #7"`) | **FIXED** | Exporting Rules as Markdown outputs `## 7. Logged hours don't track story points (r = -0.072)` with `- impact: ≈0 min (confidence 1)` and no `"counted in #7"` (`hasSelfRef: false`), while legitimate shared-pool rules retain `counted in #3`. |
| **F-20** | `VERSION` and `server/version.py:__version__` still say `v0.9.0` | **FIXED** | `VERSION` is `v0.12.0` and `server/version.py:__version__` is `"0.12.0"`. Exporting a `.git`-free tree via `git archive HEAD` and calling `server.version.resolve_version()` returns `{'version': 'v0.12.0', 'tag': 'v0.12.0', 'sha': '', 'source': 'version-file'}`. |
| **F-21** | Ticket detail boundary chips render raw `snake_case` (`PR_OPEN`, `REVIEWER_ADDED`, `IN_VERIFICATION`) | **FIXED** | On `#ticket/DEL-155` and `#ticket/DEL-183`, boundary chips render `PR OPEN`, `REVIEWER ADDED`, `IN REVIEW`, `FIRST REVIEW`, `IN VERIFICATION` with matching space-separated tooltips (`"the pr open boundary"`). |
| **F-22** | Ticket detail worklog row says `log delay: within week` beside `logged late by: 9 d 4 h` | **FIXED** | On `#ticket/DEL-155`, the `2026-10-12 15:45 worklogged` row now prices both on the calendar clock: `"log delay within week · logged late by 3 calendar days 4 h · time spent 3 h"`. |

---

## 3. Verification of New `v0.12.0` Surfaces (`prd/58`–`62`)

### 3.1 Constraint Verdict Card & Ladder (`prd/58`)
- **Overview top card (`#verdict`) & `/api/verdict`:** Loads cleanly on `del-combined` (`1q`):
  - **Sentence:** *"The constraint is the approval gate. 619 h of removable review wait: 238 h currently owed and 382 h of abnormal tail. #60 (DEL-176) has been in review 18 d 0 h, waiting on FrMo."*
  - **Meta & Pools:** `87 completed stories · 8 open PRs · story clock · as of 2026-11-18 00:10`. Definition list shows `REMOVABLE 619 h`, `OWED NOW 238 h on 4 stories`, `TAIL 382 h (above p85 71 h)`, `HOLDERS (PR clock) FrMo 961 h · peers 142–231 h`.
  - **Ladder:**
    - `Exploit` (`open`, warning pill): lists 3 rules + 3 longest current waits (`#53 DEL-167 21 d 0 h`, `#55 DEL-168 19 d 0 h orphaned approval`, `#60 DEL-176 16 d 3 h orphaned approval`), lab line `refine before start +82 % cycle — not a measured win · review SLA 4 h −9 % cycle · cap review at 2 rounds −13 % cycle`, and honesty line *"The model does not see the ticket-level wait, so the exploit rung is the rules' claim, not the lab's."*
    - `Subordinate` (`open`, neutral pill): 2 rules, `lab: cap PRs at 3 commits +289 % cycle — not a measured win`.
    - `Elevate` (`blocked`, neutral outline pill — never red): `exploit first: 3 items are still owed · lab: add a sentinel: no clear effect (−3 % cycle)` and honesty note *"No capacity change is proposed while the exploit items are owed: run the chase list first, then re-read this rung."*
  - **Next constraint `<details>`:** `Next: verification 81 h · starting work 41 h · refinement 33 h` (expands to 5-station table: Verification 81 h, Starting work 41 h, Refinement 33 h, Sprint planning 22 h, Development 14 h).
  - **Rules banner & disclosure:** Rules page shows one-line banner (`the constraint · The constraint is the approval gate. 619 h of removable wait — 238 h owed now on 4 stories. story clock · 2026-11-18 · see the ladder →`), expands the Exploit rung + 3 measured diagnostics, and collapses the 2 Subordinate levers inside `<details>Rules for the next constraint (2)</details>` with a working `Show all rules` toggle.
  - **No-Jira refusal (`del3`):** Refuses honestly with `available: false`, `"no verdict: the window has 0 completed stories (needs 20)"`.
  - **Phone (`412×915`):** Drops to 2 pool rows (`REMOVABLE 619 h`, `OLDEST #60 DEL-176 · 18 d 0 h`), moves holder split into the Exploit rung, ladder targets are `44px / 56px` tall, `scrollWidth === 412`.

### 3.2 Aging WIP Tab (`prd/59`)
- **Delivery list `Aging 13` tab & `/api/aging`:**
  - **Story cycle (default):** 13 open stories (`9 past p85 · 8 no live PR`), `self-calibrated · p85 of 87 completed stories (127.51 h)`, ticks `p50 11 d 2 h · p85 15 d 7 h · p95 21 d 4 h`. Top row `DEL-114` (`64 d 4 h · 91 calendar days · 4.0× · dead · very old`), `DEL-176` (`25 d 0 h · 1.6× · FrMo (sentinel) · live · very old`). Below the table, `No live PR — close or re-plan` lists the 9 non-live stories.
  - **PR review (`pr:review`):** 8 open PRs (`5 past p85`), `p85 of 104 merged PRs (23.43 h)`, `review_p75 = 18.26 h (2 d 2 h)`. Ranked by `hours_at_risk = age − p85`: `#53` (`23 d 4 h · 8.0×`), `#55` (`19 d 2 h · 6.6×`), `#60` (`17 d 0 h · 5.8×`), `#78` (`3 d 1 h · 1.1×`), `#51` (`3 d 0 h · 1.1×`).
  - **Controls:** `Only live` filters story cycle from 13 rows down to the 4 live stories (`DEL-176`, `DEL-198`, `DEL-197`, `DEL-199`). `Threshold source: fixed (5 bd, 24 h)` switches to `fixed · fixed (40 h)` (`8 open · 3 past p85`).
  - **Rules integration:** `stale-prs` quotes `threshold source: p85 of 104 merged PRs (23.43 h)` (`limit: ≈2 d 7 h`); `sentinel-tail` quotes `threshold source: p85 of 87 completed stories' review segment (59.44 h)` (`threshold: ≈7 d 3 h`).
  - **Issues found on Aging tab:** See `N-01`, `N-05`, `N-06`, `N-07`, `N-08` in §4.

### 3.3 Probabilistic Throughput Forecast (`prd/60`, added in `19287c5`)
- **API & determinism:**
  - `GET /api/forecast?target=sprint:3&days=90` returns `how_many: {p15: 30, p50: 30, p85: 30}`, `p_reach_target: 0.9943`, `when: {p50_weeks: 1, p85_weeks: 2, p50_date: "2026-11-24", p85_date: "2026-12-01"}`, `delivered_now: 25, remaining: 5`, `seed: 1262306561`.
  - `GET /api/forecast?target=open&n=30&horizon_weeks=4&days=90` returns `how_many: {p15: 20, p50: 26, p85: 30}`, `p_reach_target: 0.2452`, and `warnings: [{code: "wip_filling", sentence: "WIP rose 6 → 15 (+1.0/wk) while throughput rose +0.3/wk — the interval is optimistic; plan at P85."}]`.
- **Headline, precomputed target re-render & deadline change:**
  - At the top of `#sprint-strip` on Timeline, `#forecast-card` renders headline **`30–30 of 30 · by 27 Nov`**, subline `P50 30 · 99 % chance all 30 · 85 % by 1 Dec · 10,000 resamples · 13 weeks`, and scope caveat *"this forecasts the scope as of today; 23 stories were added after the sprint started (adopted scope); scope added later is not in the interval"*.
  - Changing `Target` from `30` to `35` re-renders immediately from `p_reach_curve` and `uncapped` without a network call or re-simulation (`seed` stays `1262306561`, headline updates to `34–35 of 35 · by 27 Nov`, `P50 35 · 82 % chance all 35`).
  - Clicking `Will it land?` (`[data-forecast-open="3"]`) on the `Sprint 3 at risk` panel scrolls to and flashes `#forecast-card`.
- **Refusal & delegation states (no numbers rendered):**
  - **Sparse (`days=7` or `target=sprint:2` backtest):** Renders `data-forecast-state="refused"`, `data-forecast-numbers="none"`, exact copy `"Not enough history to forecast: 1 week observed, 8 needed."` (and `"4 weeks observed, 8 needed"` on S2 backtest), with no bar and no numbers.
  - **Erratic (`days=26`, CV 0.62 > 0.60):** Renders `data-forecast-state="refused"`, `data-forecast-numbers="none"`, exact copy `"Throughput is erratic (CV 0.62): the range would be so wide it would look like a forecast and behave like a guess."` with no bar and no numbers.
  - **Overload:** `del-combined` has `stability.flag = "filling"` (WIP +1.0/wk, throughput +0.33/wk), which emits a warning rather than a refusal. The `overload` refusal path (`"WIP rose X → Y while throughput did not follow — Little's Law says cycle time will grow, so a throughput forecast would be optimistic. Fix the queue first (prd/61)."`) is verified in `tests/test_forecast.py` and `app/test/prd60.test.mjs` (`unverified visually on del-combined without mutating the store`).
  - **Small-set delegation (`n=27` on S3 → `remaining=2 < 5`):** Renders `data-forecast-state="delegate"`, `data-forecast-numbers="none"`, `"Not forecastable: a 2-item set is a risk question, not a throughput question"`, lists the prd/52 at-risk rows, and provides `See the at-risk list (prd/52/59)`.
- **At-risk reconciliation & Phone layout (`412×915`):**
  - `What the model can't see` shows `5 at risk · 5 left`, lists `#53 Done in Jira, PR unmerged · CaOs + FrMo · 21 d 0 h`, `#55 Done in Jira, PR unmerged · FrMo · 19 d 0 h`, `#66 Done in Jira, PR unmerged · BoFe + FrMo · 2 d 1 h`, and `delivered if the blocked items hold: 27` (30 minus the 3 `done-but-unmerged` items), reconciling with `Sprint 3 at risk` below it.
  - On phone (`412×915`), `.forecast-desktop` is hidden and `.forecast-phone` renders 3 lines (`30–30 of 30`, `by 27 Nov · P50 30`, `5 at risk →` with `44px` tap target height) + a `32px` bar (`360×32px`), `scrollWidth === 412`. Tapping `5 at risk →` toggles the risk rows open in place.
- **Issues found on Forecast card:** See `N-02` in §4.

### 3.4 Intervention-Aware Trend (`prd/61`)
- **Annotations write/reload/delete & store verification:**
  - Empty state renders: *"No policy changes recorded; a change you did not write down is indistinguishable from noise."* + `Record a change`.
  - Submitting `kind=review_sla`, `value=4`, `date=2026-10-08`, `note="adopted after the retro"` via the UI form writes to DuckDB (`POST /api/annotations`), renders the blue vertical hairline + axis dot on the SVG chart (`g.trend-annotation` with 44px hit area), lists `review SLA 4 h · 2026-10-08 · “adopted after the retro”`, and updates the under-chart line to `1 policy change · 1 holiday in this window · 17 outside the window, counted not drawn`.
  - Reloading the browser (`location.reload()`) preserves the annotation from the DuckDB `annotations` table.
  - Clicking `delete` triggers the two-step inline confirmation (`"delete? click to confirm"`), and the second click deletes the row (`DELETE /api/annotations/{id}`) and restores the empty state.
- **"Did it work?" before/after panel (`/api/trend/compare`):**
  - With split `2026-10-08` on `del-combined` (`1q`), `Story cycle` shows `before: n=48, p50=12 d 1 h (96.95 h), p85=15 d 2 h`, `after: n=25, p50=10 d 3 h (83.0 h), p85=13 d 0 h`, `median change: −1 d 6 h`, `95 % CI −3 d 0 h … +6 h 21 min`, outcome pill **`can't tell at this n`** (neutral outline), sentence *"Can't tell at this n: 48 items before, 25 after — the interval spans zero, so this data cannot separate the change from noise"*, and censoring disclosure (`7 open item(s) before and 5 after are excluded and counted`). Mann–Whitney U p-value is in the tooltip only, never a headline.
  - Switching the panel's metric select to `PR cycle` (`cycle_median`) shows `before: n=61, p50=3 d 0 h, p85=4 d 5 h`, `after: n=39, p50=3 d 0 h, p85=4 d 0 h`, `95 % CI −3 h 50 min … +3 h 11 min`, outcome **`no clear change`** (`tight_around_zero`).
- **Stability strip & Cumulative flow:**
  - Stability strip renders 4 chips: `WIP ↗ +1.0/wk 6 → 15`, `throughput ↗ 8.3/wk (+0.3/wk)`, `arrivals > departures (+20)`, `implied 1.38 wk vs measured 0.67 wk` (`dead_wip`), and both sentences (*"The queue is filling: WIP grows 3.0× faster than throughput…"* and *"Little's Law implies 1.4 wk of cycle time but the measured (Kaplan–Meier) median is 0.7 wk: 9 items have no live PR; they inflate WIP without ever departing."*).
  - `Cumulative flow` tab renders all 4 stage polylines (`created`, `started`, `in_review`, `merged`) and the stage legend; the `20 Aug` holiday span (`taasiseseisvumispäev`) renders automatically from the calendar.
- **Issues found on Trend:** See `N-03`, `N-04`, `N-09` in §4.

### 3.5 First-Pass Yield, Stability & Guardrails (`prd/62`)
- **First-pass yield waterfall (`#first-pass-yield` on Rules):**
  - Renders above the rules list: `REFINED 100 % (73 / 73) · DEVELOPED 77 % (56 / 73) · REVIEWED 11 % (8 / 73, warning dot: below the lab-implied yield of 50 %) · VERIFIED 90 % (66 / 73) → ROLLED 7.6 % (6 / 73)` with sentence *"Only 6 of 73 stories went through without a loop-back."* (Note: on `1q` / 90 days, the window cohort is 73 completed stories; on `All`, it is 87 completed stories with `100 % · 76 % · 13 % · 90 % → ROLLED 8.6 %, 8 / 87`).
  - Clicking each step expands its `<details>` loop-back ticket links (`DEVELOPED` lists 17 tickets; `REVIEWED` lists 65 tickets; `VERIFIED` lists 7 tickets).
- **Stability panel (`#stability` on Rules):**
  - Renders 4 tiles over 73 deliveries (`1q`): `REOPENED 5.5 % (4 of 73: DEL-137, DEL-153, DEL-169, DEL-185)`, `FAILED VERIFICATION 4.1 % (3 of 73: DEL-132, DEL-158, DEL-171)`, `REVIEW BOUNCED 23 % (17 of 73)`, `BUG SHARE — (the store's issue objects carry no issue type)`, and footer `Rework proxy 9.6 % (reopened or failed verification / deliveries) — not DORA's rework rate: this store has no deploy or incident data.`
- **Guardrails on Rules & Policy lab:**
  - Every mapped rule card on Rules renders a `GUARDRAIL` block naming both the modelled metric and the held-constant baselines:
    - `rule-round-cap`: `rounds 1.5 → 0.96 (model) · reopen 5.5 % and failed verification 4.1 % are held constant — the model does not rework the code, only the review loop`
    - `rule-jira-waiting-time`: `rounds 1.5 → 0.52 (model) · reopen 5.5 % and failed verification 4.1 % are held constant…`
    - `rule-sentinel-tail`: `rounds 1.5 → 1.57 (model) · reopen 5.5 % and failed verification 4.1 % are held constant…`
    - `rule-flow-efficiency` (inside `Rules for the next constraint`): `PRs per 100 commits 20.2 → 42.2 (model) [warning-tinted] · reopen 5.5 % and failed verification 4.1 % are held constant…`
  - Every preset in Policy lab renders a guardrail line under the cycle-time median delta (`guardrail: rounds 1.5 → … · reopen held at 5.5 %`). See `N-10` for a minor consistency discrepancy on the `size3` preset in Policy lab vs Rules.
- **Ticket pass line (`#ticket/DEL-183`, `#ticket/DEL-161`, `#ticket/DEL-155`):**
  - Renders `FIRST PASS: Refined clean · Developed clean · Reviewed bounced · Verified clean — a loop-back somewhere` on completed stories, and `Reviewed not measured — not measurable for this story` on unmerged `DEL-161`.

---

## 4. New-Surface & Free-Exploration Findings (Ranked)

### [N-01] BUG · Delivery list (`Aging` tab) · Repo scope (`scope=repo:*`) filters all story aging rows to 0 client-side while tab header and chip disagree
- **Severity:** BUG (New surface — `prd/59`)
- **View:** Delivery list → Aging tab (`?tenant=del-combined&view=delivery&tab=aging&scope=repo:DEL/delivery-api`) at both `1600×1000` and `412×915`.
- **Repro:**
  1. On `del-combined`, select **`repo delivery-api`** (`repo:DEL/delivery-api`) in the top bar `scope` dropdown.
  2. Navigate to **Delivery list** and click the **`Aging`** tab (default stage: `Story cycle`).
- **Observed:**
  - The server endpoint `/api/aging?kind=story&stage=cycle&repo=DEL%2Fdelivery-api` already filters stories by repo and returns **7 open stories** (`counts: {open: 7, over_p85: 5, dead: 4}`).
  - In the UI, the Aging toolbar chip (`[data-aging-counts]`) says **`7 open · 5 past p85 · 4 no live PR`** and the strip chart draws **7 dots**, but the Aging tab badge says **`Aging 0`** and the table renders empty: **`"Nothing in flight matches this view — an empty list here is a real result."`**
  - Root cause: `renderDeliveryPage()` (`app/main.js:1406`) and `scopedAgingRows()` (`line 1566`) pass every row through `agingMatchesScope(row, deliveryScope())` → `matchesScope(row, scope)` (`app/tables.js:225`). For `scope.kind === 'repo'`, `matchesScope()` checks `[row.repo, row.repo_short]`. Story rows returned by `/api/aging?kind=story` do **not** carry `repo` or `repo_short` (only `kind: "story"`, `ticket`, `key`, `open_prs`), so `matchesScope()` returns `false` for every story row!
- **Expected:** Since `/api/aging` already applies `?repo=` server-side (or if `matchesScope` is called client-side), `agingMatchesScope()` should return `true` for `scope.kind === 'repo'` when `row.kind === 'story'` (or story rows in `/api/aging` should carry their joined `repos`), so the table shows the 7 repo-scoped stories matching the chart and count chip.

### [N-02] BUG / PAPERCUT · Timeline (`Forecast` card) · Degenerate interval (`30–30 of 30`) overlaps `.forecast-ends` labels into `"3030"` and card has no `Copy as Markdown` export
- **Severity:** BUG / PAPERCUT (New surface — `prd/60`)
- **View:** Timeline → `#forecast-card` (`?tenant=del-combined&view=timeline`), Desktop (`1600×1000`) and Phone (`412×915`), Dark and Light themes.
- **Screenshots:** `/tmp/opencode/qa-v012-final/shots/09-timeline-forecast-dark.png`, `10-timeline-forecast-light.png`, `11-phone-forecast-dark.png`.
- **Repro:**
  1. Open **Timeline** on `del-combined` (where active Sprint 3 has 25 of 30 stories already delivered and 2 weeks remaining, so `P15 = 30, P50 = 30, P85 = 30`).
  2. Look at the bottom-right corner under the `#forecast-card` interval bar.
  3. Look for a `Copy as Markdown` affordance on `#forecast-card`.
- **Observed:**
  1. **Overlapping labels (`"3030"`):** In `forecastBar()` (`app/forecast.js:95-112`), when `high === scaleLow` (`30 === 30`), `point` is `true` and `pos(value)` returns `100` for all three spans (`p15`, `p50`, `p85`). All three `<span>30</span>` elements are positioned at `left: 100.00%`. Because `.forecast-ends span` uses `transform: translateX(-50%)`, `:first-child` uses `translateX(0)` (extending right past 100%), and `:last-child` uses `translateX(-100%)` (extending left from 100%), the three `"30"` labels collide on top of each other and render as a garbled **`3030`** at the right edge of the bar on both desktop and phone.
  2. **No `Copy as Markdown` on forecast card:** Unlike the Verdict card (`[data-verdict-copy]`), Aging tab (`#delivery-copy`), Trend (`#trend-copy`), Rules (`#rules-copy`), Policy lab (`#policy-copy`), and Ticket detail (`#ticket-copy`), `app/forecast.js` has no `forecastMarkdown()` or copy button.
- **Expected:**
  1. When `view.p15 === view.p50 && view.p50 === view.p85` (or when two quantile positions are within a few percent of each other), deduplicate or collapse the `.forecast-ends` labels so `"30"` is printed once at the right edge instead of three overlapping `"30"`s.
  2. Add a `Copy as Markdown` chip (or include the forecast block in the page's export) preserving `?tenant=`.

### [N-03] PAPERCUT · Trend (`412×915` Phone) · `#trend-metrics` 10-tab segmented bar overflows horizontally (`scrollWidth: 717px` vs `412px` viewport)
- **Severity:** PAPERCUT (New surface — `prd/61`)
- **View:** Trend (`?view=trend`) at `412×915` phone viewport.
- **Repro:**
  1. Open **Trend** at `412×915` (`?tenant=del-combined&view=trend`).
  2. Measure `document.documentElement.scrollWidth` and `#trend-metrics.getBoundingClientRect()`.
- **Observed:** `prd/61` expanded `#trend-metrics` from 6 tabs to 10 tabs (`Flow efficiency`, `Cycle time (median)`, `Cycle time (p85)`, `Sprint story delivery share`, `Wait hours`, `Merged PRs`, `Open PRs`, `Stories delivered`, `PRs opened`, `Stories created`, `Cumulative flow`). Because `.seg` does not `flex-wrap: wrap` (or scroll inside an `overflow-x: auto` container) in `@media (max-width: 640px)`, `#trend-metrics` is `717px` wide and pushes `document.documentElement.scrollWidth` to **`717px`** on a `412px` phone, violating `prd/61` §Phone (*"No horizontal scroll"*). (Every other view in the app has `documentElement.scrollWidth === 412`).
- **Expected:** In `@media (max-width: 640px)`, give `#trend-metrics` `flex-wrap: wrap` (or `max-width: 100%; overflow-x: auto`) so the Trend page does not horizontally overflow the 412px phone viewport.

### [N-04] PAPERCUT · Trend (`Record a change` / `delete`) · Recording or deleting an annotation triggers a full event/object reload (`load()`) via WebSocket `changed` broadcast
- **Severity:** PAPERCUT (New surface — `prd/61`)
- **View:** Trend (`?view=trend`).
- **Repro:**
  1. Open **Trend** (`?view=trend&metric=cycle_median`), or select any non-default tab such as `Cycle time (median)`.
  2. Open `Record a change`, enter a date/kind, and submit (or delete an existing annotation).
  3. Watch the page approximately 1 second after the `POST /api/annotations` or `DELETE` completes.
- **Observed:**
  - `POST /api/annotations` calls `_bump_store_stamp(path)` on the server (`server/app.py:2129`).
  - One second later, `_watch_store()` (`server/app.py:2639`) sees the bumped stamp and broadcasts `{"event": "changed", "tenants": ["del-combined"]}` over `/ws`.
  - The browser's `socket.onmessage` (`app/main.js:3507`) calls `load()`, which checks `if (!force && state.stamp === stamp && state.events.length)`. Wait — because `health.as_of` and `health.events` did not change, `load()` hits the fast-return path when `state.events.length > 0`, **except** if `state.trendMetric` was not synced to the URL or if a reload occurs. Even more noticeably, during `recordAnnotation()` (`app/main.js:2084`), `state.cache.clear()` + `ensureData('trend')` already refreshed the trend, so the `/ws` `changed` broadcast 1s later triggers a redundant `/api/tenants` + `/api/health` round-trip and flashes the status bar from `"recorded review SLA 4 h"` back to `"live · 2529 events · del-combined"` after 1 second.
- **Expected:** `recordAnnotation()` / `deleteAnnotation()` can update or suppress the watcher echo (or keep the `"recorded …"` flash status for its normal duration) so the user's confirmation message is not immediately overwritten 1s later by the store watcher.

### [N-05] PAPERCUT · Delivery list (`Aging` tab) · Scope filter (`person:*` / `role:*`) filters the Aging table and tab badge but leaves `[data-aging-counts]`, strip chart dots, `#honesty`, and `#aging-replan` unscoped
- **Severity:** PAPERCUT (New surface — `prd/59`)
- **View:** Delivery list → Aging tab under `scope=person:CaOs` or `scope=role:sentinel`.
- **Repro:**
  1. Select `person CaOs` in the top bar `scope` dropdown.
  2. Go to **Delivery list** → **`Aging`** tab and switch Stage between `PR review` and `Story cycle`.
- **Observed:**
  - On `PR review`, the tab badge says `Aging 1` and the table shows 1 row (`#53`), **but** the toolbar count chip `[data-aging-counts]` says `8 open · 5 past p85`, the strip chart plots all `8` team-wide dots, and `#honesty` says `8 in flight · 8 live · 5 past p85 · PR clock · filtered to CaOs (waiting on it) · chase = age − p85, ranked (5 items)`.
  - On `Story cycle`, the tab badge says `Aging 0` and the main table shows 0 rows, **but** `#aging-replan` below it still renders all 9 team-wide `"No live PR — close or re-plan"` stories!
- **Expected:** Pass `rowFilter` (or the filtered `rows`) into `agingChart()`, `[data-aging-counts]`, `#honesty`, and `replanList()` so all four elements on the Aging tab reflect the active scope consistently with the table and tab badge.

### [N-06] POLISH · Delivery list (`Aging` tab) · Strip chart percentile tick labels (`p50`, `p75`, `p85`, `p95`) overlap horizontally on desktop
- **Severity:** POLISH (New surface — `prd/59`)
- **View:** Delivery list → Aging tab (`1600×1000` desktop), both `Story cycle` and `PR review`.
- **Screenshot:** `/tmp/opencode/qa-v012-final/shots/03-delivery-aging-dark.png`.
- **Repro:**
  1. Open **Delivery list** → **`Aging`** tab on desktop (`1600×1000`).
  2. Inspect `.aging-tick-labels span` bounding rects above the strip chart.
- **Observed:** Because `.aging-strip` scales its x-axis to the oldest item (`516 h` on stories; `188.1 h` on PRs), the finished-work percentiles (`13 h .. 31 h` on PRs; `90 h .. 172 h` on stories) cluster in the left 6 %–31 % of the axis. Each tick label (`p50 11 d 2 h`, `p85 15 d 7 h`, `p95 21 d 4 h`) is `~76px` wide and positioned with `position: absolute; transform: translateX(-50%)`:
  - On `Story cycle`: `p50` (`left: 320.9px, right: 396.8px`) overlaps `p85` (`left: 389.4px, right: 465.3px`) by **7.4px**.
  - On `PR review`: `p50` (`right: 295.5px`), `p75` (`left: 250.9px, right: 320.5px`), `p85` (`left: 276.8px, right: 346.4px`), and `p95` (`left: 315.8px`) all overlap by **30–45px** into an unreadable jumble.
- **Expected:** Either render the tick labels as an inline flex row above the chart (like `.aging-ticks` on phone) with short `p50`/`p85`/`p95` markers on the axis, or stagger/clamp adjacent label x-positions when their bounding boxes collide.

### [N-07] POLISH · Delivery list (`Aging` tab) · `.aging-meta` duplicates the vacation/calendar sentence and the censoring sentence twice
- **Severity:** POLISH (New surface — `prd/59`)
- **View:** Delivery list → Aging tab (`?view=delivery&tab=aging`).
- **Screenshot:** `/tmp/opencode/qa-v012-final/shots/03-delivery-aging-dark.png`.
- **Repro:**
  1. Open **Delivery list** → **`Aging`** tab and read the two paragraphs directly below the strip chart (`.aging-meta` and `.aging-censor`).
- **Observed:** `agingMarkup()` (`app/aging.js:316-319`) renders:
  ```html
  <p class="aging-meta">Ages are business hours on the holder’s calendar; a person on vacation accrues none. Calendar days in brackets. ${esc(payload.note || '')}</p>
  <p class="aging-censor">An age is a censored duration — the item has not finished yet; compare it with finished work, never add it to a cycle-time median.</p>
  ```
  Because `/api/aging` sets `payload.note` to `"An age is a censored duration — the item has not finished yet; compare it with finished work, never add it to a cycle-time median. Ages are business hours on the holder's calendar; a person on vacation accrues none. Calendar days in brackets."`, both sentences appear **twice** back-to-back!
- **Expected:** Do not append `payload.note` inside `.aging-meta` when `.aging-meta` and `.aging-censor` already hardcode the exact two sentences from `payload.note`.

### [N-08] POLISH · Delivery list (`Aging` tab) · Desktop table columns clip `"story"` to `"sto…"` and `"91 calendar days"` to `"91 calendar…"`
- **Severity:** POLISH (New surface — `prd/59`)
- **View:** Delivery list → Aging tab at `1600×1000`.
- **Screenshot:** `/tmp/opencode/qa-v012-final/shots/03-delivery-aging-dark.png`.
- **Repro:**
  1. Open **Delivery list** → **`Aging`** tab (`Story cycle`) at `1600×1000`.
  2. Inspect the `KIND`, `AGE`, and `No live PR — close or re-plan` `WHY` columns.
- **Observed:**
  - `.aging .c-kind { width: 58px }` (`app/style.css:2632`) with cell padding leaves insufficient inner width for 5 monospace characters (`scrollWidth: 59 > clientWidth: 58`), truncating `"story"` to **`"sto…"`** on every row.
  - `.aging .c-age { width: 170px }` (`app/style.css:2635`) truncates `"64 d 4 h 91 calendar days"` to **`"64 d 4 h 91 calendar…"`**.
  - In `#aging-replan`, `.table.compact td` has `white-space: nowrap; overflow: hidden; text-overflow: ellipsis`, truncating the `WHY` column (`clientWidth: 170, scrollWidth: 619`) to **`"no open PR and no h…"`**.
- **Expected:** Widen `.aging .c-kind` to `68px` and `.aging .c-age` to `195px` (or allow wrap), and set `.aging-replan .c-who { white-space: normal; }` so the reason sentence is readable.

### [N-09] POLISH · Trend (`Before/after` panel) · Censoring disclosure sentence is printed twice when `censored_before + censored_after > 0`
- **Severity:** POLISH (New surface — `prd/61`)
- **View:** Trend (`?view=trend`), "Did it work?" panel with an active annotation.
- **Screenshot:** `/tmp/opencode/qa-v012-final/shots/04-trend-intervention-dark.png`.
- **Repro:**
  1. Record an annotation on Trend (e.g., `2026-10-08`, `review SLA 4 h`).
  2. Inspect the "Did it work?" panel text.
- **Observed:**
  - `analysis/before_after.py:601-606` appends `" 12 open item(s) are excluded and counted: a running age is not a completed duration."` to `payload["note"]` (which renders in `.verdict-sentence` at the top of the panel).
  - Then `beforeAfterPanel()` (`app/intervention.js:151`) also renders `<p class="panel-note censored">7 open item(s) before and 5 after are excluded and counted: a running age is not a completed duration.</p>` below the table.
- **Expected:** Keep the detailed `<p class="panel-note censored">` line below the table and omit the duplicate suffix from `payload["note"]` (or vice versa).

### [N-10] POLISH · Policy lab vs. Rules (`prd/62` Guardrails) · Policy lab `cap PRs at 3 commits` (`size3`) guardrail line shows `rounds` instead of `PRs per 100 commits`
- **Severity:** POLISH (New surface — `prd/62`)
- **View:** Policy lab (`?view=policy`) vs. Rules (`?view=rules`).
- **Repro:**
  1. Open **Rules**, expand `Rules for the next constraint (2)`, and look at the guardrail on `flow-efficiency` (mapped to `size3`, `cap PRs at 3 commits`). It shows: **`GUARDRAIL: PRs per 100 commits 20.2 → 42.2 (model) · reopen 5.5 % and failed verification 4.1 % are held constant`**.
  2. Open **Policy lab** and click the **`cap PRs at 3 commits`** preset (`[data-preset="size3"]`). Look at the guardrail line under `cycle time · median`.
- **Observed:** In Policy lab, `labGuardrail(compare)` (`app/policy.js:260`) hardcodes `modelled.find((entry) => entry.id === 'rounds_mean') || modelled[0]`, so under `cap PRs at 3 commits` it prints **`guardrail: rounds 1.5 → 1.5 · reopen held at 5.5 %`** instead of the worsening `PRs per 100 commits 20.2 → 42.2` guardrail that `analysis/advice.py` (`GUARDRAIL_PRIMARY["size3"] = "prs_per_100_commits"`) selects on the Rules page.
- **Expected:** `labGuardrail()` in `app/policy.js` should prefer any modelled guardrail with `entry.worse === true` (or match `GUARDRAIL_PRIMARY` for the active scenario) so Policy lab and Rules highlight the same `PRs per 100 commits` packaging side-effect when capping PR size.

---

## 5. Regressions (Features that worked in `v0.11.1` and broke in `v0.12.0`)

**None (0 regressions on `v0.11.1` surfaces).**
- All 22 findings from `v0.11.1` (`F-01`..`F-22`) are verified fixed.
- Existing views (`Overview`, `Delivery list` Blocked/Oldest/All tabs, `Ticket detail`, `Timeline`, `Blocking map`, `Queue`, `Rules`, `Policy lab`, `Sources`), navigation (`Back` stack, `YAH` bar, deep links, `?theme=light`), right-click pivot menus, drawers, keyboard Escape/Enter handling, and cross-view number reconciliations continue to work without regression.

---

## 6. Screenshot Index

All screenshots captured from the isolated verification server on `19287c5` and saved under `/tmp/opencode/qa-v012-final/shots/`:

| File | Viewport | Theme | Surface / Evidence |
| --- | --- | --- | --- |
| `01-overview-verdict-dark.png` | `1600×1000` | Dark | Overview with `THE CONSTRAINT` verdict card, `Exploit` / `Subordinate` / `Elevate (blocked)` ladder, expanded `Next` stations table, chase card, and canonical tiles (`F-04`). |
| `02-rules-yield-dark.png` | `1600×1000` | Dark | Rules page with constraint verdict banner, `First-pass yield` waterfall (`7.6 %`, `6 / 73`), `Stability` tiles (`5.5 %`, `4.1 %`, `23 %`, `—`), `GUARDRAIL` lines on levers, formatted evidence chips (`F-18`), and `Rules for the next constraint (2)`. |
| `03-delivery-aging-dark.png` | `1600×1000` | Dark | Delivery list `Aging 13` tab (`Story cycle`), strip chart with `p50`/`p85`/`p95` ticks (`N-06`), 3-stop band bars, `No live PR — close or re-plan` table, and duplicate meta text (`N-07`, `N-08`). |
| `04-trend-intervention-dark.png` | `1600×1000` | Dark | Trend page with recorded `review SLA 4 h · 2026-10-08` annotation marker, `20 Aug` holiday span, `Did it work?` before/after table + CI bar (`can't tell at this n`), and Little's Law stability strip (`filling`, `dead_wip`). |
| `05-overview-light.png` | `1600×1000` | Light | Overview in Light theme verifying contrast and token palette across the new Verdict card, ladder pills, chase card, and diagnosis panels. |
| `06-ticket-del183-light.png` | `1600×1000` | Light | Ticket detail `#ticket/DEL-183` in Light theme showing `FIRST PASS` line (`prd/62`), `PR #66 opened 6 d 4 h after the ticket closed` (`F-14`), and space-separated boundary chips (`F-21`). |
| `07-phone-delivery-dark.png` | `412×915` | Dark | Phone Delivery list (`All 8` tab) proving `F-01` fix (`scrollWidth === 412`, all values aligned at right edge of 412px cards) and `F-02` 44px topbar/YAH/jump targets. |
| `08-phone-queue-dark.png` | `412×915` | Dark | Phone Queue view & Response time stacked cards proving `F-01` fix (`scrollWidth === 412`) and `F-16` (`8 (3 only) +5 (oldest of 8: 21 d 4 h)`). |
| `09-timeline-forecast-dark.png` | `1600×1000` | Dark | Timeline `#sprint-strip` with `FORECAST` card (`30–30 of 30 · by 27 Nov`, `What the model can't see`, `delivered if the blocked items hold: 27`, `Will it land?` button, and `"3030"` end-label overlap `N-02`). |
| `10-timeline-forecast-light.png` | `1600×1000` | Light | Timeline `#sprint-strip` and `FORECAST` card in Light theme with `S1 lens` active. |
| `11-phone-forecast-dark.png` | `412×915` | Dark | Phone `#sprint-strip` and `FORECAST` card (`30–30 of 30`, `by 27 Nov · P50 30`, `5 at risk →` expanded in place, 32px bar, `scrollWidth === 412`). |

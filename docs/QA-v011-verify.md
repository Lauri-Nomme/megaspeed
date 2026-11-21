# megaspeed v0.11.0 — verification of the prd/63 checklist

Verification pass (not a fresh hunt) of **every** finding in `prd/63-v010-qa-fixes.md`
(F1–F30, V-A1…A12, V-B, V-C1…C8) at tag `v0.11.0` (`eee40f94`, tree clean, build tag read from the UI/`/api/meta`).

- Own server on `:8795` (`:8791` was already taken by someone else's v0.9.0 process, untouched) against copies of
  `del-combined` + `del3`; the live `:8766` server and all source were left alone.
- Evidence = DOM text / computed style from Playwright (1600×1000 and 412×915 touch, dark + light) plus
  `/api/eval` through the harness browser for the V-A4 spot check; `/api/*` payloads for the numbers.
- Screenshots / scripts: `/tmp/opencode/qa-v011/*.png`, `/tmp/opencode/qa-v011/t/*.py`.
- Each item's original repro (from `docs/QA-explore.md` / `docs/QA-v010.md`) was re-run.
- Regression sweep: 2 tenants × 9 views × 8 scopes scanned for `undefined/NaN/null/[object`, stuck "Loading…" and page
  errors → **none** (one hit during the run was my own harness `eval` being routed to a parallel page; re-run clean).
- Docs-only / deferred items (F18, V-C1/2/4/5/7/8) are N/A: they are explicitly other PRDs.

## 1. Checklist

| id | verdict | evidence |
| --- | --- | --- |
| **F1** | **PARTIAL** | `view=trend&repo=DEL/delivery-web` now renders (`33.7 % latest (9 Nov · n=4)`; `/api/trend?repo=` → 200, repo-scoped N's). **But** a failed `/api/trend` (forced 400/500) still shows "Loading the trend…" forever — only the status pill says `view failed: …` (`showPage` catch → `setStatus`, no body error state). Also the scope banner on Trend under a repo scope still says "this view (trend) is not filtered — it still shows the whole team" although the data is repo-scoped. |
| **F2** | FIXED | `#ticket/NOPE-1`, `DEL-9999`, `DEL-0167` → "No ticket X in dataset del-combined. A ticket is a STORY object or a PR.ticket_keys entry…" + `← Overview`; `foo bar` → "“FOO BAR” is not a ticket key — a key looks like PROJ-123"; page ready. |
| **F3** | FIXED | Ticket DEL-183 `.worklog-track` 164×8, fills 105 px / 162 px (logged 7 h vs active 1 d 2 h); same in light theme. CSS collision gone (Rules chart renamed `.worklog-bar`). (The Rules chart's *values* are V-A4.) |
| **F4** | FIXED | Row menu → "Open in timeline" lands on `scope=ticket:DEL-167`, PR lanes focused; "Clear scope" stays on Delivery (`view=delivery&scope=all`). (Minor: "Clear scope · whole team" is still offered when the scope is already `all`.) |
| **F5** | FIXED | `chase →` now a styled `chip chase-link` button: dark 8.02 : 1, light 6.14 : 1; it opens the PR drawer with holder facts. |
| **F6** | PARTIAL | Links now carry the tenant (`[DEL-183](…/?tenant=del3#ticket/DEL-183)`, also for del-combined). The export title is still "Delivery list — 1 open item" on the Blocked tab (3 / 8 on the other tabs) — not "Blocked: 1 of 8". |
| **F7** | PARTIAL | Overview recomputes on scope: `person:LaAd` → "0 open items · 0 blocked … tiles and rows are the same set", MEASURED WAIT "≈0 min … charged to LaAd", Done-in-Jira "No ticket in this scope … — 3 team-wide", label "(items waiting on LaAd)". **Still wrong on Delivery:** scope LaAd → "0 open items in view · … **4 stale · 8 unobserved**" (team-wide counts). |
| **F8** | PARTIAL | In-progress bucket is excluded and labelled ("16 Nov in progress (not in the headline)", label no longer clipped), Merged PRs `10 latest (9 Nov · n=10)`, axis 0–15, N on every headline. Not done: no minimum-N rule (`50.0 % latest (5 Oct · n=1)`; `2 d 1 h latest (9 Nov · n=3)` with `−6 h 35 min since the first measured bucket` = n=3 vs n=3, no censored/low-N marker); table row `– in pr…` clipped; plot fills ~60 % of width. |
| **F9** | FIXED | Delivery rows show `blocked stale` / `waiting 1w+ stale` pills unclipped; Delivery list now has the "Done in Jira, PR open (3)" section the Overview link points at. |
| **F10** | FIXED | Chase card: "3 orphaned approvals … (Rules counts the 2 older than its 24 h sentinel-tail threshold)"; Rules #02 "24 stories … (90.0-day window; the sprint lens counts the whole store)". |
| **F11** | FIXED | DEL-183 #66: Delivery "BoFe (author) + FrMo (sentinel)" = inspector "waiting on / holder roles" = Overview Done-in-Jira "BoFe + FrMo". (API row still carries a stray `waiting_for: "CaOs"` — not rendered.) |
| **F12** | FIXED | Honesty line: "median × unavailable — cohort too small (N=8 < 10) — no benchmark shown"; chip renamed "cohort median ×". |
| **F13** | FIXED | DEL-100 PR #2 reads `63 d 0 h declined` — no `(median … · 20.4×)`. |
| **F14** | FIXED | Notices for `tenant=nope`, `view=bogus`, `tf=zzz`, `scope=person:Zed` ("…does not exist in dataset del-combined — showing everything"), and a tenant switch ("Dataset switch cleared the person scope “FrMo”…"); `/api/meta?tenant=del3` → 200, `tenant=nope` → 404 with valid list. |
| **F15** | FIXED | `queue ready` no longer offered; per-view banner says "this view (rules) is not filtered — it still shows the whole team" with filtered / not-filtered lists for person, repo and role scopes. (Exception: Trend + repo, see F1.) |
| **F16** | PARTIAL | Per-row `unobserved` pill gone (shown only in inspector/drawer where it carries a duration). But the Delivery honesty line still prints "8 unobserved" (all 8), and the snapshot age appears on Delivery in both the header and the footer. |
| **F17** | FIXED | DEL-183 boundaries chronological (`READY 13:00` → `REFINED 13:20`), quirk flag (`!` + "1 boundary quirk") on DEL-117; TICKET EVENTS attributes are labelled facts (`from backlog to ready`, `time spent 3 h`); header `change kind other`; del3 DEL-183 explains "no STORY object … PR rows are the only evidence" instead of "0/8 segments". |
| **F18** | N/A | Annotation UI is prd/61, not a v0.10/v0.11 deliverable. |
| **F19** | FIXED | `skip to content` link works (focus → `#main`); Enter on a row focuses `#drawer`; Esc returns focus to the row; click-behind closes the drawer. |
| **F20** | FIXED | Blocking people vs role: footers state the basis ("person-hours charge every concurrent holder…"); Overview tile "role-level handover graph, each handover once"; Queue response-table rows are `role=button` and scope on click (→ `scope=person:AlNg`). |
| **F21** | FIXED | Presets compose ("Presets add to the current scenario"; sentinel+SLA 4 h+cap 2 → −22.6 % median), run state "running the calibrated model… (the numbers below are the previous run)", scenario round-trips in `&policy=` and restores on reload. |
| **F22** | FIXED | "Snapshot from 3 calendar days 16 h ago" (snapshot 18 Nov 00:10 UTC viewed 21 Nov). |
| **F23** | PARTIAL | Fixed: chase WHO OWES, Done-in-Jira NOTE pill, Queue `(median 3 h 2 min …)`, Delivery status chips, Sprint-at-risk (plain text, no pills). **Still clipped:** Policy "who is busy" MEAN WAIT `≈2 h 45 …` (cell 90 px / text 102 px, `overflow:hidden; ellipsis`) — the exact original repro; ticket PR table CYCLE `3 d 3 h (me…` and PUSH CLASSES `pure_rebas…`; Sources timestamps; Delivery table needs horizontal scroll at 1600 so IMPACT is off-screen. |
| **F24** | FIXED | Canvas starts at y≈358 and ends at 977 of 1000 (above the prose); S3 "8 Oct → 18 Nov · plan 2026-11-27"; clicking the selected sprint row again clears it (`aria-pressed` true→false, `sprint=` dropped). |
| **F25** | PARTIAL | `docSW=412` on all 9 views + tickets (was 419/415); legend is a single scrollable strip; build tag now inline in the meta line (not `position:fixed`); Queue/Delivery tables scroll inside their wrapper; sprint rows fit. **Tap targets:** at-risk keys are 59×44 but the 27 carry-over ticket links are 55×19 (<44). |
| **F26** | FIXED | One interval label ("WIP sampled every 24 h"); `person:LaAd` → "0 streams" and the response table reads "No review requests for scope LaAd…"; `person:AlNg` → a one-row table; one explanatory paragraph. |
| **F27** | FIXED | Lab: plain-language notes, composing presets, running state, scenario in URL. Sources: one "analysis window 20 Aug → 18 Nov" vs "store span 27 Jul → 16 Nov" labelled, per-source rows (`bitbucket-dc`, `jira-fixture`). |
| **F28** | PARTIAL | Fixed: slider rails stable (24 / 800 through 5 → 3 → 1), and a `pr_size` rule now fires at cap 1 ("At these caps the PR-size rule fires…"; 11 rules) / explains why not at 10/400. **Not fixed:** the Markdown export grammar is unchanged in source (`app/rules.js:547` → "7 PRs opened before the window and is not in the cohort"); the people toggle still just prints "No person-level rule crosses its threshold…; the team rules below still apply." and repeats team rules; the snake-case id tags (`SPRINT_DELIVERY`) are unchanged (titles are human sentences). New: "or 1 commits". |
| **F29** | FIXED | No empty-slot separators (only wrap-orphaned `·`); "Or adopt a review SLA" block padded 14 px; `?theme=light` link no longer persists (next plain visit = dark); the toggle persists. |
| **F30** | FIXED | `metric=` / `bucket=` round-trip on Trend (verified for all six metrics, week + sprint); `view=delivery&sprint=3` filters ("sprint S3 lens: 30 stories", 4 open items in view) and `sprint=2` → 0 items; URL carries `sprint/from/to`. |
| **V-A1** | FIXED | Each surface names its metric ("queue-wait, not response time", "role-level handover graph", "response time: last peer answer → first response"); single verdict deferred to prd/58 as specified. |
| **V-A2** | FIXED | "3 orphaned approvals — approved by peers, waiting only on FrMo" = #55, #60, #52 (no #53/#66); `chase →` opens a drawer with holder/phase facts; Delivery waiting-on = Overview who-owes. |
| **V-A3** | FIXED | Strip "S2 … 5 committed at start · 25 adopted"; Rules #01 "only 5 were committed at the start, 25 adopted mid-sprint (44 % of points delivered)". |
| **V-A4** | **NOT FIXED** | Rules → #07 → "Logged hours vs story points": every bucket still reads `1 pt 0 min … 13 pt 0 min`, bars 4 % height, directly under "the median story logs 3 h 30 min". The CSS collision is fixed, but the data is not: `logged_by_points` keys come from the API as `"1.0"`, `"2.0"`…, while `app/worklog.js` looks up `byPoints[String(Number("1.0"))]` = `"1"` → 0. Confirmed in the harness (`/api/eval`): `bars:["1 pt 0 min h=4%", …]`. The JS unit test fixture uses numeric keys (`{1: 4, 2: 4, …}`), which JSON never produces, so it passes. API values are right (5.5 / 3.5 / 4.0 / 3.5 / 5.0 / 3.0 h). |
| **V-A5** | PARTIAL | See F8: "0 latest / −2" and negative tick fixed; low-N / censored latest bucket still headline-grade (n=1 / n=3). |
| **V-A6** | FIXED | DEL-167 headline: `13th fastest of 87 to Jira done … 62% of the change unmerged` + red chip; prose "62 % of the story's change (160 of 258 changed lines) is in an unmerged PR, so this is not a shipped change". |
| **V-A7** | FIXED | DEL-155 "most in review rounds (62%) — 7 d 4 h with no PR open" and segment note "7 d 4 h with no PR open"; DEL-169 "verification → done (the reopen loop)" + "2 d 0 h is the reopen loop"; page reconciles "6 h 41 min inside the segments … 0 min on the PR clock". |
| **V-A8** | FIXED | Queue FrMo NEVER `8 (3 only) +5 (21 d 4 h)` — 3 waiting only on the sentinel, 5 with another holder owing. |
| **V-A9** | FIXED | S2 "15 of 30 by end · 52 pts" **and** "23 finished in the window · 84 pts (8 carried in)"; Rules #01 "the team finished 23 stories (84 pts) in its dates". |
| **V-A10** | PARTIAL | #08/#09/#10 now carry "The lab does not support this as a lever … treat it as a hypothesis" and a POLICY LAB badge with the lab's % (+82 / +289 / −3). #04 still says "so the cap is a measured lever, not a style preference" — not labelled as a correlation; the card order is unchanged (#10 critical still last, not demoted). |
| **V-A11** | **NOT FIXED** | Original repro unchanged: scope `repo delivery-web` → Overview "What to do now: 1 orphaned approval DEL-197 #52 · 2 d 0 h" while "Needs attention" says **"No open item is waiting right now."** with 2 open items. Expected "none in this scope"-style wording ("none waiting over a week"). |
| **V-A12** | FIXED | People diagnosis: all six "≈ healthy"; tile "0 OVERLOADED PEOPLE · 0 slow · … a flag needs > 2 d 1 h vs the 1 d 1 h baseline". |
| **V-B** | PARTIAL | Fixed: carry-over is a table + "27 never re-committed"; ticket tables in d/h; Policy `REQUESTS / RUN ≈106.7`; `cohort median ×`, `×3.1 peers`, `change kind other`; ticket "THIS TICKET ELSEWHERE" links (Timeline / Delivery rows / Blocking map / each PR); `#ticket/del-167` resolves; `live` badges on live-state rules; "1 boundary quirk" stat. Remaining: prose still mixes hours and d/h (`took 73.31 h`, `200 h`, `60.15 h`; stale drill `2.41 business days`; Timeline lifecycle `802 calendar days 1 h`); developer jargon still in footers (`concepts.md §1` on Trend, `concepts.md §6` and `created_ready` / `pr_size` on Rules, `rules.flow_efficiency aggregate_efficiency` on Trend). |
| **V-C1** | N/A | → prd/58 (constraint verdict). |
| **V-C2** | N/A | → prd/49 (side-by-side repo comparison). |
| **V-C3** | FIXED | Window velocity per sprint + plan-day grace ("23 finished in the window · 84 pts (8 carried in)", "5 committed at start"). |
| **V-C4** | N/A | → prd/52/60 (landing forecast). |
| **V-C5** | N/A | → prd/62 (first-pass yield). |
| **V-C6** | PARTIAL | Links to PRs/Timeline/Delivery/Blocking done. "Waiting for a reviewer to be added" is **not** its own interval: DEL-167 still reads `PR open → first review 0 min` while PR #53 sat 3 business days unreviewed. |
| **V-C7** | N/A | → prd/50/61 (trend significance, per-repo trend, verdict). |
| **V-C8** | N/A | → prd/59 (aging/stale report). |

## 2. Summary

51 ids (30 F + 12 V-A + V-B + 8 V-C):

| verdict | count | ids |
| --- | --- | --- |
| FIXED | **30** | F2 F3 F4 F5 F9 F10 F11 F12 F13 F14 F15 F17 F19 F20 F21 F22 F24 F26 F27 F29 F30 · V-A1 A2 A3 A6 A7 A8 A9 A12 · V-C3 |
| PARTIAL | **12** | F1 F6 F7 F8 F16 F23 F25 F28 · V-A5 V-A10 · V-B · V-C6 |
| NOT FIXED | **2** | **V-A4** (worklog bars read "0 min"), **V-A11** ("No open item is waiting right now" above a 2-day orphan) |
| N/A | **7** | F18 · V-C1 V-C2 V-C4 V-C5 V-C7 V-C8 |

Still broken, by importance:
1. **V-A4** — Rules worklog chart: all six buckets "0 min" (key `"1.0"` vs `"1"` in `app/worklog.js`; unit-test fixture hides it). One-line fix + a test built from the real API shape.
2. **V-A11** — scoped Overview "No open item is waiting right now." (repo delivery-web).
3. **F1** — no error state on a failed trend fetch (spinner forever) and a banner that says Trend is unfiltered under a repo scope.
4. **F6** export title, **F7** Delivery team-wide `stale/unobserved` counts under a person scope, **F23** Policy `≈2 h 45 …` and the ticket PR table clip, **F28** export grammar / people toggle, **F8/V-A5** min-N rule, **V-C6** reviewer-wait interval, **V-A10** #04 correlation wording, **V-B** residual mixed units / jargon, **F16** "8 unobserved", **F25** 19 px carry-over links.

## 3. Regressions / new findings

None of these is a crash, dead end or wrong headline number; no `undefined/NaN` leak and no stuck spinner on any of the 144 view × scope × tenant combinations. New or newly-visible issues:

1. **Scope banner contradicts Trend under a repo scope** (introduced by the F1 fix). `?view=trend&repo=DEL/delivery-web` — banner: "this view (trend) is not filtered — it still shows the whole team", but the page shows repo numbers (`9 Nov · n=4` vs team `n=9`; `/api/trend?repo=` differs from the unscoped call). The filtered/not-filtered lists in the banner omit Trend.
2. **Delivery list "All" tab: the sticky header floats into the rows** at 1600×1000 (`.table th{position:sticky;top:var(--meta-bottom)}` inside the non-scrolling `.table-wrap{overflow-x:auto}`: header drawn at y≈405 over DEL-167's row; hit-testing there returns the `<th>`, so right-click on those rows hits the header). CSS is unchanged since v0.10.0 (so not a regression of this release), but the "All" tab is the main list. Repro: `/?tenant=del-combined&view=delivery` → **All 8**.
3. **PR drawer / inspector "approvals: none"** for a PR that is `1/2 approved` (DEL-168 #55: "reviewers 1/2 approved · 1 pending" and "approvals none"): the UI reads `row.approved`, which the delivery API row does not have (`app/main.js:2127/2235/2310`).
4. **Topbar wraps on a ticket-scoped Timeline** (long range label "5 Oct 20:01 → 18 Nov 15:29" and the tenant pill break onto two lines). Cosmetic.
5. Markdown export "PR(s)" grammar also yields "1 commits" in the PR-size rule sentence at cap 1 (F28 follow-on).
6. `/api/delivery` rows still expose three different "who" answers for #66 (`waiting_for: "CaOs"`, `waiting_on: [BoFe, FrMo]`, `ball_label: "author"`); the UI uses `waiting_on` consistently, so only API consumers see it.

## 4. Verdict — is v0.11.0 shippable?

**Yes as a release, with one caveat; no as "prd/63 done".**
The two high-severity dead ends are gone (F1 hang, F2 hang), the misleading-number group is overwhelmingly fixed
(orphan/holder, sprint committed/adopted + window velocity, trend latest, declined benchmark, segment labels,
the 6-of-6 diagnosis), the phone layout no longer overflows, and nothing regressed into a crash or a leaked
`undefined`. The caveat is **V-A4**: the Rules worklog chart still prints six `0 min` bars under a "3 h 30 min"
headline — the very symptom the checklist lists as an A-item — and it is a one-line fix. The definition of done
("re-running both QA passes finds none of the A/B items") is therefore **not** met: V-A4 and V-A11 still
reproduce, plus the partials above. Recommended before tagging a "fixed" release: V-A4 (with a real-shape test),
V-A11 wording, F1 error state + the Trend banner, F6 export title, F7 Delivery counts, F28 grammar, Policy cell width.

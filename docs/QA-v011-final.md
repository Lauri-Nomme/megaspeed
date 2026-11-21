# megaspeed v0.11.0 — final verification of the prd/63 checklist

Final verification pass of **every** finding in `prd/63-v010-qa-fixes.md`
(F1–F30, V-A1…A12, V-B, V-C1…C8) at tag `v0.11.0` (`aec1ebdc`, tree clean, build tag `v0.11.0 · aec1ebdc` read from the UI sidebar footer and `/api/meta`).

- Own server on `:8797` against copies of `del-combined.duckdb` + `del3.duckdb` in `/tmp/opencode/qa-v011f/`; the live `:8766` server and all source were left alone.
- Evidence = DOM text / computed styles via the harness (`/api/eval` + `/api/screenshot`) at 1600×1000 and Playwright at 412×915 (touch/mobile), dark and light themes, plus `/api/*` payloads.
- Scratch & screenshots: `/tmp/opencode/qa-v011f/*.json`, `/tmp/opencode/qa-v011f/shots/*.png`.
- All 14 previously PARTIAL / NOT FIXED items (V-A4, V-A11, F1, F6, F7, F8/V-A5, F16, F23, F25, F28, V-A10, V-B, V-C6) and the 4 regressions from `docs/QA-v011-verify.md` were re-tested against their original repros, along with all 30 previously FIXED items.
- Full regression sweep: 2 tenants × 9 views × 8 scopes (135 combinations) scanned for `undefined`/`NaN`/`[object`, stuck `"Loading the …"` states, and `pageerror` events → **0 leaks, 0 page errors**.
- Deferred items belonging to separate PRDs (F18, V-C1, V-C2, V-C4, V-C5, V-C7, V-C8) are marked **N/A** with their target PRD.

---

## 1. Checklist

| id | verdict | evidence |
| --- | --- | --- |
| **F1** | **FIXED** | `/?tenant=del-combined&view=trend&scope=repo:DEL/delivery-web` renders `30.0% latest (19 Oct · n=6)` (`GET /api/trend?...&repo=DEL%2Fdelivery-web` → 200); the scope banner now states `Scope repo delivery-web … · this view (trend) is filtered to it · filtered: overview, delivery list, timeline, blocking map, queue view, trend, rules and policy lab`; and a failed `/api/trend` (500) renders `The trend did not load. /api/trend?… → 500` with a `Retry the trend` button (`[data-trend-retry]`), never a stuck spinner. |
| **F2** | **FIXED** | `#ticket/NOPE-1`, `DEL-9999`, `DEL-0167` → `"No ticket NOPE-1 in dataset del-combined. A ticket is a STORY object or a PR.ticket_keys entry…"` + `← Overview`; `#ticket/foo%20bar` → `"“FOO BAR” is not a ticket key — a key looks like PROJ-123."`; page ready immediately. |
| **F3** | **FIXED** | Ticket DEL-183 `.worklog-track` renders at 164×8 px with fills 105 px (`logged 7 h`) and 162 px (`active (PR) 1 d 2 h`); duplicate `.worklog-bars` CSS collision is gone. |
| **F4** | **FIXED** | Right-click row menu → `"Open in timeline"` scopes to `scope=ticket:DEL-167` with PR lanes focused; `"Clear scope · whole team"` clears to `scope=all` in place on `view=delivery` without jumping to Timeline. |
| **F5** | **FIXED** | `chase →` is a styled `button.chip.chase-link` (`rgb(148, 180, 212)` on dark card surface = 8.02 : 1 contrast, light theme 6.14 : 1) and opens the PR drawer with holder facts. |
| **F6** | **FIXED** | Copy-as-Markdown on `del3` carries `?tenant=del3` on every link (`[DEL-183](http://127.0.0.1:8797/?tenant=del3#ticket/DEL-183)`), and the export title names the active tab: `# Delivery list — Blocked: 1 of 8`, `# Delivery list — Oldest waiting: 3 of 8`, and `# Delivery list — 8 open items` on All. |
| **F7** | **FIXED** | Every tile and honesty line recomputes on scope: `person:LaAd` on Overview → `0 BLOCKED`, `0 WAITING OVER A WEEK · of 0 open items`, `≈0 min MEASURED WAIT · queue-wait hours charged to LaAd`, `No ticket in this scope is Done in Jira with its PR open — 3 team-wide`; on Delivery, the meta honesty line now uses scoped counts (`person:LaAd` → `0 open items in view · 0 blocked · 0 waiting over a week (5 d = 40 h) · 0 stale`; `person:BoFe` → `2 open items in view · 1 blocked · 0 waiting over a week · 1 stale`). |
| **F8** | **FIXED** | In-progress bucket (`16 Nov in progress (not in the headline)`) is excluded from headlines; count y-axes clamp at 0; minimum cohort floors (`n≥5` for PR metrics, `n≥2` for sprint on-time delivery) are enforced (`Cycle time (median)` → `3 d 0 h latest (2 Nov · n=11) · 1 newer bucket below n≥5 (9 Nov)`; `Sprint on-time delivery` → `– no bucket meets the n≥2 floor (the latest measured bucket, 5 Oct, has n=1)`); right-censored buckets are marked with hollow SVG dots (`.trend-dot.censored`), table annotations (`32.1% right-censored (5 open)`), and headline notes (`right-censored · 5 from that bucket still open`); no table cell is clipped. |
| **F9** | **FIXED** | Delivery status cells render `blocked stale` and `waiting 1w+ stale` unclipped, and the Delivery page includes the `"Done in Jira, PR open"` section that Overview links to. |
| **F10** | **FIXED** | Overview chase card reconciles with Rules inline: `"3 orphaned approvals … (Rules counts the 2 older than its 24 h sentinel-tail threshold)"`; Rules #02 states `"24 stories committed but not done by their sprint's close (90.0-day window; the sprint lens counts the whole store)"`. |
| **F11** | **FIXED** | DEL-183 #66 shows the same holder set across all surfaces: Delivery `"BoFe (author) + FrMo (sentinel)"`, inspector `"waiting on / holder roles"`, and Overview Done-in-Jira `"BoFe + FrMo"`. |
| **F12** | **FIXED** | Pressing `cohort median ×` on Delivery updates the honesty line to `"median × unavailable — cohort too small (N=8 < 10) — no benchmark shown"`. |
| **F13** | **FIXED** | Ticket DEL-100 PR #2 (`declined`) displays `63 d 0 h declined` with no benchmark multiple (`20.4×` removed). |
| **F14** | **FIXED** | Fallback notices render for `tenant=nope`, `view=bogus`, `tf=zzz`, `scope=person:Zed` (`"…does not exist in dataset del-combined — showing everything"`), and across tenant switches (`"Dataset switch cleared the person scope…"`). `/api/meta?tenant=del3` returns 200; unknown tenants return 404 with valid options. |
| **F15** | **FIXED** | `queue ready` is removed from the scope selector; the scope banner explicitly lists filtered vs. unfiltered views for the active scope and marks `#scope` `.scope-inert` when the current view is not filtered by that scope kind. |
| **F16** | **FIXED** | Per-row `unobserved` pills are omitted when true for every row, the Delivery honesty line drops `"8 unobserved"` when all scoped rows are unobserved (`8 open items in view · 1 blocked · 3 waiting over a week (5 d = 40 h) · 4 stale`), and the relative snapshot age (`Snapshot from 3 calendar days 17 h ago`) is stated once in `#meta` while the Delivery footer prints only the UTC timestamp (`snapshot 2026-11-18 00:10 UTC`). |
| **F17** | **FIXED** | Boundaries render chronologically (`READY 13:00` → `REFINED 13:20` on DEL-183) with a `!` quirk flag (`"1 boundary quirk"` on DEL-117); TICKET EVENTS attributes are human-labelled facts (`from backlog to ready`, `time spent 3 h`); header says `change kind other`; `del3` DEL-183 explains `"no STORY object … PR rows are the only evidence"` instead of `"0/8 segments"`. |
| **F18** | **N/A** | Trend intervention annotation UI is specified in **prd/61**. |
| **F19** | **FIXED** | `"skip to content"` link focuses `#main`; pressing Enter on a table row opens and focuses `#drawer`; Esc closes the drawer and restores focus to the originating row; clicking behind the drawer closes it. |
| **F20** | **FIXED** | Blocking footers explain person-hours vs. role-level handover graph; Overview tile specifies `"queue-wait hours (role-level handover graph, each handover once)"`; Queue response-time table rows have `role="button"` and click-to-scope to that person. |
| **F21** | **FIXED** | Policy lab presets compose (`"Presets add to the current scenario"`), running state displays `"running the calibrated model… (the numbers below are the previous run)"`, and the scenario round-trips in the URL (`&policy=`). |
| **F22** | **FIXED** | Snapshot age states exact days and hours (`"Snapshot from 3 calendar days 17 h ago"`) instead of rounding 3.6 days up to `"4 calendar days ago"`. |
| **F23** | **FIXED** | At 1600×1000, zero cells clip across Overview, Delivery, Queue, Policy lab (`MEAN WAIT` column widened to 120 px so `≈2 h 45 min` renders in full), Ticket PR table (`CYCLE` 132 px and `PUSH CLASSES` 160 px wrap cleanly so `3 d 3 h` and `content ×2 · pure_rebase ×1` are unclipped), and Sources timestamps (`sw <= cw` on every cell). |
| **F24** | **FIXED** | Timeline canvas starts at y = 332 px (well above the fold at 1600×1000); S3 strip shows both snapshot and plan end (`8 Oct → 18 Nov · plan 2026-11-27`); clicking an active sprint row toggles it off. |
| **F25** | **FIXED** | At 412×915 (phone), `document.documentElement.scrollWidth === 412` across all 9 views + Ticket; legend scrolls horizontally; the build tag lives statically in the sidebar footer (`position: static`); and all 27 carry-over ticket links in `.sprint-carry-table` meet the 44 px minimum tap target height (`59×44` to `67×44` px). |
| **F26** | **FIXED** | Queue uses a single sampling interval label (`"WIP sampled every 24 h over 90 calendar days"`), filters the response-time table on person scope (`person:LaAd` → `"No review requests for scope LaAd…"`), and prints the explanatory paragraph once. |
| **F27** | **FIXED** | Policy lab uses plain-language notes, composing presets, loading indicator, and URL state; Sources distinguishes `"analysis window 20 Aug → 18 Nov"` from `"store span 27 Jul → 16 Nov"` and renders per-source rows (`bitbucket-dc` and `jira-fixture`). |
| **F28** | **FIXED** | PR-size slider rails stay stable (max 24 / 800) and fire/explain the PR-size rule; Markdown export grammar is pluralised (`"124 PRs opened over 90 calendar days of a 103.6-day dataset — 7 PRs opened before the window are not in the cohort"`, and `"1 commit"` at cap 1 instead of `"1 commits"`); and clicking the `people (pseudonyms)` toggle filters out team rules (`"No person-level rule crosses its threshold in this window; the team tab names the team-level rules. show the 10 team rules →"`). |
| **F29** | **FIXED** | No empty-slot `· ·` separators in `#meta`; `"Or adopt a review SLA…"` block has 14 px padding; visiting `?theme=light` applies light mode for the visit without overwriting the stored theme preference. |
| **F30** | **FIXED** | Trend encodes `metric=` and `bucket=` in the URL across all 6 metrics and week/sprint toggles; Delivery honours `sprint=` deep links and filters to the sprint's stories. |
| **V-A1** | **FIXED** | Every surface names its metric basis (`"queue-wait, not response time"`, `"role-level handover graph, each handover once"`, `"response time: last peer answer → first response"`); single constraint verdict is **prd/58** as specified. |
| **V-A2** | **FIXED** | Overview `"3 orphaned approvals — approved by peers, waiting only on FrMo"` lists only #55, #60, and #52; `chase →` opens the PR drawer with holder/phase breakdown; Delivery and Overview show identical holders. |
| **V-A3** | **FIXED** | Sprint start-day commitment grace counts stories added on start day (`S2 … 5 committed at start · 25 adopted`), and Rules #01 states `"(44% of points delivered)"`. |
| **V-A4** | **FIXED** | Rules #07 `"Logged hours vs story points"` parses float-string JSON keys (`"1.0"`…`"13.0"`) and renders the real medians (`1 pt 6 h 30 min`, `2 pt 3 h`, `3 pt 4 h`, `5 pt 3 h 30 min`, `8 pt 5 h`, `13 pt 3 h`) with proportional bar heights instead of `0 min` stubs. |
| **V-A5** | **FIXED** | Same as F8: in-progress bucket excluded, count axes clamped at 0, minimum-N floors (`n≥5` / `n≥2`) enforced on headlines with skipped newer low-N buckets named, and right-censored buckets marked in the chart, table, and headline. |
| **V-A6** | **FIXED** | DEL-167 headline badges `62% of the change unmerged` (`13th fastest of 87 to Jira done · … · 62% of the change unmerged`), and the answer prose states `"62 % of the story's change (160 of 258 changed lines) is in an unmerged PR, so this is not a shipped change"`. |
| **V-A7** | **FIXED** | DEL-155 headline and segment state `"most in review rounds (62%) — 7 d 4 h with no PR open"`; DEL-169 labels `"verification → done (the reopen loop)"` (`2 d 0 h is the reopen loop`); and active time reconciles `"1 d 3 h inside the segments … 1 d 2 h on the PR clock"` (or `6 h 41 min` vs `0 min` on DEL-169) with an inline explanation of both clocks. |
| **V-A8** | **FIXED** | Queue FrMo `NEVER` column reads `8 (3 only) +5 (21 d 4 h)`, separating the 3 PRs waiting only on the sentinel from the 5 where a peer or author also owes. |
| **V-A9** | **FIXED** | Sprint strip and Rules #01 show both membership delivery and window velocity crediting carry-overs: `15 of 30 by end · 52 pts` and `23 finished in the window · 84 pts (8 carried in)`. |
| **V-A10** | **FIXED** | Rules #08, #09, and #10 carry a warning box (`"The lab does not support this as a lever … treat it as a hypothesis, not a proven fix"`) plus a `POLICY LAB` badge with the lab's % (`+82%`, `+289%`, `−3%`); Rule #04 carries a `correlation` badge (`[data-relationship="correlation"]`) and states `"That is a correlation, not a proven cause — rounds and cycle co-vary by construction"`. |
| **V-A11** | **FIXED** | Under `scope=repo:DEL/delivery-web`, Overview `"Needs attention"` now lists the scoped union of blocked, over-a-week, orphaned-approval, and stale items (`in review · DEL-197 #52 delivery-web · 2 d 0 h`), matching the `"1 orphaned approval"` card above it; an actually-empty scope (`person:LaAd`) says `"No open PR in LaAd — nothing is waiting, blocked or orphaned in this scope."` |
| **V-A12** | **FIXED** | People diagnosis compares against a >2× baseline (`"0 OVERLOADED PEOPLE · 0 slow · … a flag needs > 2 d 1 h vs the 1 d 1 h baseline"`), marking all 6 people `≈ healthy` instead of flagging everyone. |
| **V-B** | **FIXED** | Carry-over is a structured table (`27 never re-committed`) with 44 px links; durations use a single business-hours formatter (`9 d 1 h`, `3 d 2 h`, `24 d 0 h`) across ticket prose, headers, and Rules #06 stale drill-downs while long wall-clock lifecycle spans use whole calendar days (`802 calendar days`, not `802 calendar days 1 h`); Policy lab shows per-run requests (`REQUESTS / RUN ≈106.7`); cryptic labels are clarified (`cohort median ×`, `×3.1 peers`, `change kind other`, `live` badges, `1 boundary quirk`); internal doc references (`prd/21`, `concepts.md`, `aggregate_efficiency`, `created_ready`) are stripped from rendered UI prose; `#ticket/del-167` normalises to uppercase; and `"THIS TICKET ELSEWHERE"` links to Timeline, Delivery rows, Blocking map, and each PR. |
| **V-C1** | **N/A** | Single constraint verdict + "what first" selector → **prd/58**. |
| **V-C2** | **N/A** | Side-by-side repo comparison → **prd/49**. |
| **V-C3** | **FIXED** | Sprint window velocity (stories/points finished in the sprint's dates, crediting carry-overs) + start-day commitment grace (`5 committed at start · 25 adopted`, `23 finished in the window · 84 pts (8 carried in)`). |
| **V-C4** | **N/A** | Active-sprint landing forecast → **prd/52** / **prd/60**. |
| **V-C5** | **N/A** | Rework as a rate / first-pass yield → **prd/62**. |
| **V-C6** | **FIXED** | Ticket page `"THIS TICKET ELSEWHERE"` links to `Timeline`, `Delivery rows`, `Blocking map`, and each PR (`#52 delivery-api`, `#53 delivery-api`); and `"waiting for a reviewer"` (`pr_open_reviewer_added`: `PR_OPEN` → `REVIEWER_ADDED`) is now its own 10th decomposition segment and boundary, distinct from `reviewer added → first review`. |
| **V-C7** | **N/A** | Trend significance shading, per-repo/per-person trend comparison, text verdict → **prd/50** / **prd/61**. |
| **V-C8** | **N/A** | Aging/stale report including approved-then-declined PRs on Overview → **prd/59**. |

---

## 2. Counts

Total checklist IDs: **51** (30 F + 12 V-A + 1 V-B + 8 V-C):

| verdict | count | ids |
| --- | --- | --- |
| **FIXED** | **44** | F1, F2, F3, F4, F5, F6, F7, F8, F9, F10, F11, F12, F13, F14, F15, F16, F17, F19, F20, F21, F22, F23, F24, F25, F26, F27, F28, F29, F30 · V-A1, V-A2, V-A3, V-A4, V-A5, V-A6, V-A7, V-A8, V-A9, V-A10, V-A11, V-A12 · V-B · V-C3, V-C6 |
| **PARTIAL** | **0** | — |
| **NOT FIXED** | **0** | — |
| **N/A** | **7** | F18 (→ prd/61) · V-C1 (→ prd/58), V-C2 (→ prd/49), V-C4 (→ prd/52,60), V-C5 (→ prd/62), V-C7 (→ prd/50,61), V-C8 (→ prd/59) |

---

## 3. Remaining PARTIAL or NOT FIXED items

**None.** Every A, B, C, and in-scope D item in `prd/63-v010-qa-fixes.md` is now **FIXED**.

---

## 4. Regressions check

First, all **4 regressions** (plus the 2 minor follow-ons) identified in `docs/QA-v011-verify.md` were re-tested and confirmed **fixed**:

1. **Scope banner under repo-scoped Trend (`/?view=trend&scope=repo:DEL/delivery-web`) — FIXED:** The banner now says `"this view (trend) is filtered to it · filtered: overview, delivery list, timeline, blocking map, queue view, trend, rules and policy lab"`.
2. **Delivery list "All" tab sticky header floating into rows at 1600×1000 — FIXED:** `#delivery-table .table th { top: 0; }` inside `#delivery-table { overflow: auto; }` keeps the header at the top of its scroll container. `document.elementFromPoint(midX, midY)` hit-testing across all 8 rows in the "All 8" table (and the 3 rows in "Done in Jira, PR open") returns `TD` (`hitInTh: false`) for every row.
3. **PR drawer `"approvals: none"` on a 1/2-approved PR (DEL-168 #55) — FIXED:** Clicking `chase →` on DEL-168 #55 opens the drawer showing `reviewers: 1/2 approved · 1 pending` and `approvals: ErLi`.
4. **Topbar wrap on a ticket-scoped Timeline (`/?view=timeline&scope=ticket:DEL-167`) — FIXED:** `.topbar` has `flex-wrap: nowrap` and `#range` truncates with `text-overflow: ellipsis`; topbar height stays at exactly `40 px` (single line).
5. **`"1 commits"` grammar at PR-size cap 1 — FIXED:** Setting the commits slider to `1` renders and exports `"1 commit"` (`has1Commits: false`).

Second, a sweep across **135 view × scope × tenant combinations** at 1600×1000 and all 10 views at 412×915 found **zero new regressions** (no JS errors, no `undefined`/`NaN`/`[object` text, no stuck spinners, no horizontal overflow, and no cell clipping).

---

## 5. Overall verdict — is v0.11.0 shippable?

**Yes — v0.11.0 is shippable and meets the `prd/63` Definition of Done in full.**
All 44 in-scope checklist items (100% of Section A, Section B, Section C, and in-scope Section D items) are **FIXED**, all 4 prior regressions are resolved, and 0 PARTIAL or NOT FIXED items remain.

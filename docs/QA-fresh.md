# QA — fresh exploratory user-journey test (delivery lead, desktop 1600×1000)

Build `v0.9.0 · ff65d934`, tenant `del-combined` (snapshot 2026-11-18 00:10 UTC, 2529 events, 131 PRs,
100 stories, 3 sprints). Own server on `:8786` against a copy of `data/del-combined.duckdb`, headless
Chromium through `/api/eval` + `/api/screenshot`. The live `:8766` server and all source were left alone.

Method: each of the three questions was answered by clicking through the UI from the root URL. The
answer text came out of the DOM (`/api/eval`). Every claim was then re-derived independently from the
DuckDB copy with a script of my own. That script has its own business-hours calculator: Europe/Tallinn,
09:00-17:00, Mon-Fri, Estonian holidays. Where I list the UI's claims, "Right", "Misleading" and "Wrong"
are my verdicts.

Name mapping (pseudonyms are never explained in the UI): AlNg=alice, BoFe=bob, CaOs=carol, DaKo=dave,
ErLi=erin, **FrMo=frank (sentinel, on 131/131 PRs)**, LaAd = the 8th person who creates/points/sprints
stories, BiBi = bot. "d" in durations such as "21 d 0 h" = 8 business hours.

Screenshots: `/tmp/opencode/qa-fresh/*.png` (copies of `/tmp/opencode/qa-fresh/shots/`). Scripts:
`/tmp/opencode/qa-fresh/*.py`.

---

## One-line answers

1. **DEL-167 / DEL-155.** DEL-167 is *not* really done. Jira closed it on 19 Oct, but PR #53 (160 lines) has never had an
   approval or a merge, and has sat idle 21 business days since 19 Oct 13:43. The "73 h / refinement 36 %" headline hides
   that, and DEL-167 is actually one of the faster stories (rank 73 of 87). DEL-155 (200 h, the slowest story of 87) was
   mostly a 60 h gap with no PR open plus sentinel waits, not "62 % review rounds".
2. **Stuck in review.** 8 open PRs. Three wait on Frank as the *only* holder (#55 19 d, #60 16 d, #52 2 d). #53 has never
   been approved by anyone. Four are in normal reviewer ping-pong. Frank is the systematic constraint: he answers in about
   20 h against about 2.5 h for the peers, and holds the largest wait per PR (15 h against 9-13 h) once four bot-closed
   zombie PRs are discounted. The UI's own people ranking points at AlNg/DaKo instead, wrongly (bug B1).
3. **What first.** The evidence favours a **review SLA / chasing orphaned approvals**. The model's best single scenario is
   a 4 h review SLA (about -9 % cycle). A second sentinel alone is "no clear effect" (-3 %). Smaller PRs are very harmful
   in the model (+289 % at a 3-commit cap, sentinel ρ 122 %), and PR size barely correlates with cycle time in the data
   (r = 0.20). Refinement and review-round caps cannot be tested in the lab. **Sprint 2 did not deliver**: 15 of 30 stories
   (52 of 118 points, 44 %) by close, 12 late (slip 56 business days), 3 never.

---

## Question 1 — "DEL-167 PR #53 waiting over a week, Jira says Done. Why did DEL-167 (8 pts) take what it took, and is it really done? And DEL-155?"

### Journey

1. `/` → **Overview**. Needs-attention table has `waiting 1w+ · DEL-167 #53 delivery-api · 21 d 0 h`
   (`01-overview.png`).
2. Click the `DEL-167` link → `#ticket/DEL-167` ("Why did this ticket take this long?") (`02-ticket-167.png`).
3. Click the PR #53 row → right-hand PR drawer (`03-pr53-drawer.png`).
4. Sidebar → **Delivery list** → `All 8` → click the DEL-167 row → list inspector + drawer
   (`04b-delivery-all.png`, `05-list-167-selected.png`, `06-list-inspector.png`).
5. Drawer → **Timeline** pivot → timeline scoped to `ticket:DEL-167` (`16-timeline-from-drawer.png`).
6. Go-to-ticket box / hash → `#ticket/DEL-155` (`07-ticket-155.png`).

### What the UI says (quoted)

- Overview: "waiting 1w+ DEL-167 #53 … 21 d 0 h".
- Ticket DEL-167: "**Done in Jira, but PR #53 is still open (right-censored); the cycle is measured to the ticket's done,
  not to that PR. DEL-167 took 73.31 h from created to done: most of it went to created → ready (26.67 h, 36 % of the
  measured cycle), and 16 % of the measured time was active work.**" Header: `73.3 h cycle · 9/9 segments · 2 PRs · 8 points ·
  most in created → ready (36 %)`. PR table: `#52 merged 13.6 h`, `#53 open · censored 192 h`.
- List inspector (hidden under the drawer, see B5): "state **1/2 reviewers pending** · waiting on **CaOs** · stale: **open
  24 business days (limit 5); ticket DEL-167 is Done in Jira but this PR is open** · waiting since 2026-10-19 13:43 UTC (21 d
  0 h) · phase active".
- PR drawer: cycle `24 d 0 h`, in review `2 d 3 h`, `waiting on author —`, `CI —`, `no events since —`.
- Ticket DEL-155: "**DEL-155 took 200 h … most of it went to review rounds (123.55 h, 62 %) … 60.15 h of it had no PR open — PR
  #31 started 7.51875 business days after PR #30 merged.**" Sprint card: "carried over · 1 late · #2 finished late".

### Independent cross-check (DuckDB)

| Claim | Check | Verdict |
|---|---|---|
| DEL-167 created→done 73.3 h, 9 segments sum 73.4 | Re-computed from the STORY events: 73.31 h business | **Right** |
| "created→ready 26.7 h" | Tue 6 Oct 11:20 UTC → Fri 9 Oct 14:00 UTC = 26.67 h | **Right** |
| PR #53 waiting 21 d 0 h | last event 19 Oct 13:43:38 → snapshot = 168.3 business h = 21.03 d | **Right** |
| PR #53 age 24 d / "192 h" | 192.0 bh | **Right** (3 spellings of one number: see P6) |
| "Done in Jira but PR #53 still open" | Story closed 19 Oct 12:38; #53's 2nd push is 13:43 the *same day*; never approved, never merged | **Right, but understated** |
| "waiting on CaOs; 1/2 reviewers pending" | #53 reviewers added 19 Oct: FrMo + CaOs. **Zero approvals from either.** CaOs left one `mandatory` thread, resolved `walked_back` within 1.5 h. FrMo never acted | **Misleading** (should be 0/2 pending, Frank also owes) |
| DEL-167 cycle "73 h, most in refinement" answers "why did it take what it took" | Story created→done rank **73 of 87** (≈15th fastest); median of all stories 89.7 h; 8-pt median 109 h. Also the ticket decomposition only follows PR #52 (merged in 13.6 h); #53's 192 h open time is not in any segment | **Misleading** (the 73 h is for the easy half; the real delay is the unmerged PR) |
| "Cycle measured to Jira done, not to that PR" | Created → snapshot is 242.7 bh and still running | Right but never quantified |
| DEL-155 200 h | created 24 Sep 15:00 → 29 Oct 16:14: 200.0 bh; **slowest of 87** stories, 2.2× median | **Right** (rank/benchmark never stated) |
| DEL-155 "most in review rounds, 62 %" | Segment = first review (5 Oct) → approval (27 Oct) = 123.6 h. PR #30 merged 9 Oct, PR #31 opened 20 Oct. **60.15 h of that has no PR open** (re-derived: 4.15 h + 40 h + 8 h + 8 h). The two PRs' own cycles are 30.8 h + 38.3 h = 69 h. PR #31: AlNg approved 22 Oct, sentinel FrMo only 27 Oct (3 business days) | **Misleading headline**; the parenthetical admits it, the headline doesn't |
| "7.51875 business days" | 60.15 / 8 = 7.519 | Right value, **wrong formatting**, and the sentence has no separator ("…work 60.15 h of it…") |

### Verdict — accuracy: mostly right numbers, wrong emphasis. Helpfulness: medium.

What works: the ticket page *does* flag "Done in Jira but PR #53 still open", and the timeline scoped to the ticket
(`16-timeline-from-drawer.png`) shows the PR #53 bar stretching 5 weeks with a hatched "no events since 29 calendar
days". That is the most persuasive picture in the product. The per-ticket decomposition, the "reopened ×1" handling
(DEL-153) and the error states for unknown keys are careful.

What fails the lead: the headline sentence answers "where did the *measured* 73 h go?" and never says (a) DEL-167 was
faster than typical, (b) that the real problem is a 24-business-day orphan PR waiting on reviewers, (c) who must act
(CaOs *and* FrMo), (d) that three tickets (DEL-167, DEL-168, DEL-183) are Done in Jira with unmerged code. That last fact
is only in Rules #8 (no ticket keys there) and in the inspector under the drawer.

---

## Question 2 — "What is stuck in review right now, and who is the constraint — Frank (sentinel) or the reviewers?"

### Journey

1. **Overview** → "Needs attention" (4 rows) and "People diagnosis" (`01-overview.png`).
2. **Delivery list** → `All 8` (`04b-delivery-all.png`).
3. **Queue** → level `people` (`10-queue.png`).
4. **Blocking map** → `role / queue` (`11-blocking.png`).
5. **Rules** → #9 `SENTINEL_TAIL` (`12-rules.png`).
6. **Policy lab** → "WHO IS BUSY" table (`14-policylab.png`).

### What the UI says (quoted)

- Delivery list, all 8 open PRs: #66 `blocked` on author, #51 DaKo, #52 **FrMo**, #53 CaOs, #55 **FrMo**, #60 **FrMo**,
  #79 BoFe, #78 ErLi. Three are `waiting 1w+ stale` (#53 21 d, #55 19 d, #60 16 d 3 h).
- Overview: "3 WAITING OVER 9 D of 8 open items · 4 stale", "review waited on author **207 d 7 h** / author waited on review
  167 d 5 h / sentinel waited on ready 70 d 3 h", people: `AlNg overloaded ≈3 d 5 h, FrMo overloaded ≈3 d 0 h, DaKo
  overloaded ≈3 d 0 h, BoFe overloaded, ErLi slow, CaOs slow`.
- Rules #9 (critical): "**2 items waiting only on FrMo for up to 19 d (threshold 3 d)** — Add a second sentinel or a rotation
  … a mandatory approval that sits on one person past a day of work is capacity, not latency".
- Queue: FrMo `6.0 wip · ≈24.8 h mean · 1.30/d · 122 PRs · peak 10`, AlNg `≈29.8 h`.
- Policy lab "who is busy": `sentinel:1 ≈55 % partly busy, mean wait ≈2 h 45 min` against reviewers 26-55 min, 24-30 %
  "idle".

### Independent cross-check

Open-PR state, replayed from the event log:

| PR | Ground truth | UI | Verdict |
|---|---|---|---|
| #55 DEL-168 | ErLi approved 21 Oct 15:37; Frank pending; idle 152 bh | waiting on FrMo 19 d | **Right** |
| #60 DEL-176 | BoFe approved 26 Oct; Frank pending; idle 131.6 bh | FrMo 16 d 3 h | **Right** |
| #52 web DEL-197 | CaOs approved 13 Nov; Frank pending 16 bh | FrMo 2 d 0 h | **Right** |
| #53 DEL-167 | nobody approved; FrMo never opened it | CaOs; "1/2 pending" | **Misleading** |
| #78 DEL-198 | ErLi's approval lost by a later push; ball with ErLi | ErLi | **Right** |
| #51 web DEL-197 | DaKo's approval lost by push | DaKo | **Right** |
| #79 DEL-199 | BoFe left a mandatory comment, author pushed 13 Nov; ball with BoFe | BoFe | **Right** |
| #66 DEL-183 | CaOs mandatory comment open; BoFe owes; **and Jira closed DEL-183 on 4 Nov, 9 days before the PR was opened** | blocked / author, stale | **Right** |

Counts: 3 of 8 wait on Frank alone, 1 on "reviewers" (but really also Frank), 3 on peers/author after a normal
push-comment cycle, 1 blocked. Frank is listed as reviewer on **8/8** open PRs and none has a sentinel approval.

Who is the constraint (all 131 PRs, my own business-hours measurements):

| Measure | Frank | Peers |
|---|---|---|
| response after being added as reviewer | mean **20.4 h**, median 20.6, max 35.5 | 2.2-2.8 h means (AlNg 2.7, BoFe 2.2, CaOs 2.8, DaKo 2.4, ErLi 2.4) |
| last peer approval → sentinel approval | mean 6.6 h, p90 12.9 h | n/a |
| PRs routed through | 131 asked, 107 approved, 24 never (17 declined + 8 open − overlap) | 18-35 approvals each |
| merged without sentinel approval | 0 / 106 | n/a |
| approvals per week | 4-13, steady through 12 Nov | n/a |

So in aggregate Frank is a *steady, moderate* gate: every PR waits about 20 h for him against about 2.5 h for each
peer. He is not saturated (policy lab ρ ≈ 55 %). The three long-stuck items are orphans: during the three weeks that
#55 and #60 sat with him he approved about 30 other PRs. Their waits (152 and 132 h) are 4× his worst-ever turnaround.
That is a "forgotten notification" problem, not capacity.

Does the UI's people ranking hold? **No** (see B1). Charging each *person* the time of all intervals they were a pending
holder, the four bot-closed zombie PRs (delivery-api#2, delivery-web#8, #18, #17: declined by BiBi on 16 Nov after 233-488
business hours) contribute:

| Person | UI-style mean wait per PR | Excluding those 4 PRs |
|---|---|---|
| FrMo | 26.5 h (UI shows 24.8 at its 90-day window) | **15.0 h** |
| AlNg | 29.2 h ("overloaded", #1) | **8.9 h** |
| DaKo | 25.3 h ("overloaded") | **9.6 h** |
| BoFe | 14.1 h ("overloaded") | 8.9 h |
| CaOs | 12.8 h ("slow") | 12.8 h |
| ErLi | 9.4 h ("slow") | 9.5 h |

Same artefact in the blocking map: of the 1687 "author-held" hours, 1513 h (90 %) belong to those four PRs. The
headline "review waited on author 207 d 7 h" is therefore mostly dead PRs, not slow authors; real author turnaround
is about 174 h across 40 episodes. `/api/blocking` reports `abandoned_hours: 0` although the code is meant to treat bot-declined
PRs as abandoned (`analysis/ballcourt.py` around l.741: the flag is only set on intervals ended by a declined event; the
final interval to `closed_ts` is built in the "tail" branch with no `abandoned`). I did not confirm the root cause
beyond that observation.

### Verdict — accuracy: the item list is **right**; the "who is the constraint" ranking is **wrong**. Helpfulness: good for "what", poor for "who".

The delivery list (waiting-on person, phase, waiting since, stale reasons) is accurate for all 8 PRs and needs no
cross-reading. A lead can answer "what is stuck" in two clicks. But the "constraint" answer is contradictory across views
(Overview ranks AlNg first, Queue ranks AlNg first, Rules says "second sentinel", Policy lab says that does nothing). The
only view that sets Frank against reviewers by *response time* (2.5 h vs 20 h) does not exist; I had to compute it.

---

## Question 3 — "What should we change first — refinement, review-round caps, a second sentinel, smaller PRs — and did sprint 2 actually deliver?"

### Journey

1. **Rules** (timeframe `1q`; then `All`, `4w`, `1w`) — read the 10-11 ranked rules; open the SPRINT_DELIVERY drill-down
   (`12-rules.png`, `15-rule-sprint-drill.png`).
2. **Rules → PR SIZE panel**: default 10 / 400, then 5 / 151 (`13-prsize.png`).
3. **Timeline** → sprint strip → click **S2** → "S2 lens" (`08-timeline.png`, `09-sprint2-lens.png`).
4. **Policy lab** → presets "add a sentinel", "review SLA 4 h", "WIP limit 1", "review first", "cap PRs at 3 commits", plus
   combined runs (`14-policylab.png`).

### What the UI says (quoted)

- Rules, 1q, rank order: 01 refine (created → ready **29 %**, "the median story waits 3.4 d … Refine before the sprint"),
  02 comment tax (15 %), 03 flow efficiency 20 %, 04 critical **"S2 delivered 15 of 30 committed stories (50 %) by its
  close — 12 finished late, 3 still open"** (late ≈56 d 0 h), 05 carry-over 24, 06 rework "**Review loops run to 3 rounds
  at p90 (cap 2)**", 08 stale, 09 critical sentinel tail "add a second sentinel".
- PR size at 10 / 400: "**7 of 131 PRs are flagged large · 3 over commits · 4 over changed lines**" (no rule fires).
  At 5 / 151 the rule fires: "PRs over 151 changed lines or 5 commits get first reviews in **3 h vs 2.5 h** for smaller ones
  … ≈2 d 6 h at stake".
- Timeline sprint strip: "S2 closed 11 Sep → 7 Oct · 30 stories · 118 pts · 15 of 30 by end · 12 late · 3 open · ↻ 15 out";
  "0/2 sprints on time"; S2 lens: "15 of 30 done by end · 12 late · 3 still open · 56 d 0 h of slip".
- Policy lab (baseline validated ≈3 d 1 h vs 3 d 0 h measured, +0.9 %), single runs:

| Scenario | Cycle median | Lead time | UI verdict |
|---|---|---|---|
| add a sentinel | -3.0 % | -3.0 % | "no clear effect … smaller than the run-to-run spread" |
| review SLA 4 h | **-9.3 %** (p90 -13.7 %) | -9.3 % | "mixed" (delivered work -8.4 %) |
| WIP limit 1 | -11.4 % | **+257 %** | "not a net win for delivery" |
| review first | -1.9 % | -1.9 % | "no clear effect" |
| cap PRs at 3 commits | **+289 %** | +289 % | "slower on both clocks"; sentinel ρ **122 %** |
| sentinel 2 + SLA 4 h | -8.3 % | -8.3 % | "helps on both cycle time and delivered work" |
| sentinel 2 + cap 3 commits | +29 % | +29 % | "no net win" |
| sentinel 3 + cap 5 commits | +2.7 % | +2.7 % | "no clear effect" |

### Independent cross-check

| Claim | Check | Verdict |
|---|---|---|
| S2: 30 stories, 118 pts, 15 done by end, 12 late, 3 never | Replayed `sprint_added` + status events: 15/12/3 on 30 stories, 118 pts; by points 52 pts (44 %) by end, 58 late, 8 never | **Right** |
| "56 d 0 h of slip" | Sum over 12 late stories of (first `done` − sprint end) = 448.0 bh = 56.0 d. The final-done basis gives 58.1 d (DEL-153 reopened) | **Right** (reopens understated, flagged only on the ticket page) |
| Per-story slips (DEL-155 16 d 0 h; DEL-152 6 h 12) | Both re-derived exactly | **Right** |
| "27 carried over, 20 late" (Timeline) vs "24 stories, 18 late, 75 in sprint" (Rules #5) | S1 12 + S2 15 = 27 out, 8 + 12 = 20 late, 90 in sprints. Rules shows the *window-filtered* 24 / 18 / 75 | Right for each window, **two different numbers for one fact** |
| Rule 04 title "S2 delivered 15 of 30" with a 27-item drill-down | Items are S1 + S2 (S1 4 never, 8 late), not S2 only | **Misleading** |
| S3 "28 of 30 by end" | S3 is still active (plan 27 Nov); "by end" = so far, 118 of 125 points done | Misleading wording |
| Refinement 29 % / median 3.4 d (1q) | 87 stories: median created→ready **26.7 bh = 3.3 d**, sum 2420 h vs total 8635 h = 28 % | **Right** |
| Rule 01 is "what to change first" | The top rule **changes with timeframe**: All → "29 % in the **review rounds** segment" (2828 h of 9689 h, "Cap review rounds"); 1q → created→ready; 4w → SPRINT_DELIVERY first; 1w → 4 rules | **Unstable**, see B4 |
| "review rounds" segment size | Includes time with no PR open: 619 h of the 2828 h (22 %) per its own evidence field `no_pr_open_hours`. DEL-155 is the example | **Misleading label** |
| Comment tax: 367 threads, 54 wasted (29 rejected + 25 walked back), 15 % | all-time 382 threads: 32 rejected, 25 walked back, 26 acted on, **292 unresolved**, 7 unclassified; window-filtered numbers fit | **Right**, but 76 % of threads are "unresolved" |
| Rework "median 1, p90 3, 24 PRs over 2" | `review_rounds` on merged PRs: median **2**, p90 3, **31** over 2 (30×3 + 1×4); the UI's cohort includes open/declined PRs, a different basis | Different basis, **not reconcilable from the UI** |
| "One review round ≈ 1 d 6 h" | median cycle ÷ rounds: 13.8 h (UI 14.55 h = "1 d 6 h" with 8 h days) | **Right** |
| PR size 7 / 3 / 4 at 10 / 400 | 7 flagged, 3 over commits, 4 over lines; same | **Right** |
| "Smaller PRs get faster first reviews, 3 h vs 2.5 h" | 29 minutes. Merged-PR cycle by size: ≤50 lines 17 h, 50-100 24 h, 100-200 28 h, **>400 lines (n = 3) 25 h = overall median 24.8 h**. corr(lines, cycle) = 0.20, corr(commits, cycle) = 0.16, **corr(rounds, cycle) = 0.60** | **Right** and honest (≈2 d 6 h at stake is the smallest rule), but the panel never shows size vs cycle; the 6 large merged PRs merged in 16-35 h |
| Rule 09 "second sentinel … shortens every one at once" | Policy lab says no clear effect (-3 %); Frank's normal latency is 20 h (max 35.5 h) and the two stale PRs are orphans; rule confidence 0.1, ranked #9 despite "critical" | **Contradicted by the product's own model** |
| Rule 06 "cap review rounds at 2" | Stated target is "3 rounds max"; p90 = 3 ⇒ within target. `max_review_rounds: 3`, title says "cap 2". Lab has no round-cap knob | **Untestable / inconsistent** |
| Policy lab "cap at 3 commits +289 %" | The knob doubles PR count (+117 %) and every PR needs a sentinel approval, so the sentinel saturates (ρ 122 %); plausible, and consistent with sentinel being the only serial gate | Plausible, but **contradicts the PR_SIZE rule's advice** |

### Verdict — accuracy: numbers are good; recommendations are inconsistent across Rules ↔ Policy lab. Helpfulness: the *answer to "what first" is not derivable*.

Sprint question: fully answerable and correct in two clicks (Timeline strip → S2 lens, or Rules #4). Answer: **no, it
delivered 50 % of stories / 44 % of points**; the 12 late stories slipped between 4.6 business hours and 16 business days (DEL-155 is the outlier); 3 never finished.
A lead gets this faster than from Jira. One caveat the UI does not surface: 25 of the 30 S2 `sprint_added` events happen more
than a day after the sprint started (DEL-153 was added 28 Sep, 17 days in), so "committed" overstates the commitment. The UI
has no "added after start" figure. S2's 15 carried-over stories do **not** appear in S3 (0 stories share two sprints), so the
Rules advice "carry unfinished stories back to the backlog **instead of silently re-committing**" describes something that
did not happen.

"What first" is the weak part. The Rules view ranks refinement first (at 1q) while the evidence supporting it is a
near-tie with another segment, flips under a different timeframe, and measures an *elapsed backlog dwell* (created → ready),
not delivery effort. The Policy lab contradicts the sentinel rule and the PR-size rule, and cannot test refinement or
review-round caps at all, two of the four options the lead named.

---

## Consolidated findings — ranked

### Bugs (wrong or contradictory numbers)

**B1. People diagnosis / blocking map are inflated by bot-closed zombie PRs (`abandoned_hours: 0`).** Views: Overview
("review waited on author 207 d 7 h", "People diagnosis"), Queue (`people`), Blocking map. Repro: `/api/blocking` →
`totals.abandoned_hours = 0`; replay: 4 PRs declined by `BiBi` on 16 Nov (api#2, web#8, #18, #17) hold 1513 of 1687
"author-held" business hours and charge AlNg/DaKo/BoFe/FrMo. Dropping them gives AlNg 29.2 → 8.9 h, DaKo 25.3 → 9.6 h,
BoFe 14.1 → 8.9 h, FrMo 26.5 → 15.0 h, so AlNg/DaKo would not be "overloaded" and Frank would be a clear #1. The lead is sent
after the wrong people.

**B2. Delivery list: the row-click drawer covers the inspector.** Delivery list → any row at 1600 px width.
`aside#delivery-detail` (x 1228-1600, y 204) is hidden under `.drawer` (x 1216+). The inspector carries the best facts
("stale: open 24 business days; ticket DEL-167 is Done in Jira but this PR is open", "1/2 reviewers pending", holder roles),
the drawer shows `—` for waiting-on-author/CI/rebasing (and, on the ticket page, for no-events-since) (`05-list-167-selected.png`). Two panels for one PR with different
units (inspector: business 21 d; drawer: "34 calendar days 8 h"/"29 calendar days 10 h").

**B3. Four different thresholds for the same three items.** Overview card "3 WAITING OVER 9 D"; row pills "waiting
1w+" (tooltip: 40 business hours = 5 d); Delivery list header "3 waiting over 3 days"; Delivery list footer "8 open items,
**1** of them waiting over a week" (`/api/delivery` `counts.waiting_over_week: 1`, `oldest_waiting: 3`). The footer is
simply wrong given 168 / 152 / 132 h waits.

**B4. Rules #1 is unstable and mislabelled.** Rules → timeframe `1q` / `All` / `4w` / `1w`: #1 = created→ready (29 %) /
review rounds (29.2 %) / SPRINT_DELIVERY / SPRINT_DELIVERY. The two top segments are within 1 point and the UI does not
say so. The "review rounds" segment includes 22 % no-PR time (`no_pr_open_hours` 619 h of 2828 h), so "Cap review rounds"
is partly advice about nobody having opened a PR. "≈1.2k h at stake" is an *assumed* half of the largest segment
(`impact_basis`), shown as a measurement-sounding "at stake".

**B5. DEL-155 headline mislabels a no-PR gap as review rounds.** `#ticket/DEL-155`: "most of it went to review rounds
(123.55 h, 62 %)"; 60.15 h of that has no PR open (PR #31 opened 7.5 business days after #30 merged). Real review
time is about 69 h across both PRs. Also "7.51875 business days" and a missing separator ("…work 60.15 h of it…").

**B6. #53 "waiting on CaOs / 1/2 reviewers pending".** Delivery list inspector / `/api/delivery`:
`reviewers_total 2, reviewers_pending 1, waiting_on [CaOs]`. Neither CaOs nor FrMo ever approved (0/2), CaOs's blocker was
walked back, and the sentinel is not shown as owing anything. Chasing CaOs alone will not close the PR.

**B7. Policy lab verdict wording.** Policy lab → "add a sentinel" → PR count row: "Δ −1.1 %" but verdict "PR count 1.1 %
**higher**" (also −6.4 % "higher" for review SLA 4 h). Review SLA 4 h verdict: "mixed: cycle 9.3 % shorter and delivered
work 8.4 % less, **but delivered work 8.4 % less**". Scenario with 2-3 sentinels still shows bottleneck `sentinel:1`.

**B8. Rule 04's headline is S2-only but its drill-down is S1 + S2 (27 items); sprint numbers differ between views.**
Rules #4/#5 (24 / 18 / 75 stories, window-filtered) against Timeline strip (27 / 20 / 90). S3 shows "28 of 30 by end" while
active.

### Papercuts

- **P1. Rework rule vs its own target.** Rules #6: "Review loops run to 3 rounds at p90 (cap 2)" while the footer says
  "targets: … 3 rounds max". Median rounds 1 (UI) vs 2 (`pr_merged.review_rounds`). The basis is not reconcilable from the UI.
- **P2. Severity ≠ rank.** Rules view: `critical` rules (#4, #5, #8, #9) are ranked below `warning` rules (#1-3) because the
  rank is impact × confidence; Sentinel tail (confidence 0.1) and Stale PRs (0.2) end up last although they describe the
  PRs stuck right now. Not explained on screen.
- **P3. "overloaded / slow" labels are relative to the window median.** 4 of 6 people (plus the sentinel) are "overloaded"
  by construction. The people-level Queue row `review role synthetic` (11 PRs, "slow") has no explanation.
- **P4. Person-level load charges parallel holders.** FrMo 3044 person-hours at Queue (solo 1375) against the blocking-map
  sentinel node 614 h. Two answers to "how much does Frank hold" 5× apart, no note linking them.
- **P5. PR-size slider off-by-one.** Rules → PR SIZE: lines slider steps 1, 51, 101, …, so the cap reads `151` / `401`
  (default shows 400, slider value 401); round caps like 100 / 150 / 200 cannot be set (`13-prsize.png`).
- **P6. One PR, five durations.** #53: "21 d 0 h" (waiting, Overview/list), "24 d 0 h" (age, drawer on ticket page), "192 h"
  (ticket table), "34 calendar days 8 h" (drawer from list), "29 calendar days 10 h" no events, "in review 802 calendar
  days" in the Timeline legend (a sum over all PRs, not a meaningful magnitude).
- **P7. Duplicated/odd text.** Header strip repeats "· business hours · · business hours" on every view; Overview
  people-chip text is clipped ("overloaded (queue wai"); Overview "≈521 d 6 h MEASURED WAIT" (sum of all handovers) is
  not comparable with anything and not actionable.
- **P8. Rule examples have no ticket keys.** Rules #8: "delivery-api#53, #55, #60, #66" while every other view is
  ticket-first.
- **P9. Epic page.** `#ticket/DEL-1` → "no STORY object … 0/9 segments … missing boundaries: created, refined, ready, …" and
  131 PRs. Reasonable, but epics are not first-class. DEL-199's header says "76.3 h cycle" while the text says its 3
  measured segments cover 30.16 h.
- **P10. Stale URL params / scope control.** After clicking a sprint lens, the URL keeps `sprint=2&from=…&to=…` on
  Queue/Blocking/Rules although the view is the full quarter (and the "S2 lens" chip is gone); the scope selector offers
  `person FrMo` on Overview, which "is not filtered" (stated honestly in a banner, but the control is global).
- **P11. Jargon.** "unobserved" (tooltip explains), "sentinel waited on ready 70 d 3 h" (reads as sentinel idle; means PRs
  waited at the merge-ready step), `1 never / 2 late` suffixes in the sprint list (sprint number + outcome, unlabelled).

### Missing features that blocked the journey

- **M1. A "who is the constraint" view per person *by response time* / PR touch** (Frank's 20 h vs peers' 2.5 h). Only
  derivable from raw events.
- **M2. Peer benchmark on the ticket page**: "DEL-167 is 73rd of 87; 8-pt median 109 h"; "DEL-155 is the slowest of 87".
- **M3. Policy-lab knobs for refinement (created→ready) and review-round caps**, two of the four options the lead
  listed; a "chase orphaned approvals / reminder" knob would match what the data shows.
- **M4. "Done in Jira, PR open" as a first-class list** with ticket keys (DEL-167, DEL-168, DEL-183) and who owes each;
  DEL-183's PR was opened 9 days *after* its ticket was closed, which no view calls out.
- **M5. Sprint scope-creep ("added after start": 25 of 30 in S2)** and carry-over *destination* (stories never reappear in
  the next sprint).
- **M6. PR drawer lacks reviewers, approvals, open threads, last event** (`—` for waiting-on-author / CI / rebasing, and for no-events-since on the ticket-page drawer).
- **M7. Pseudonym legend** (who is sentinel, who is PO), ticket summaries (by design absent).
- **M8. Size-vs-cycle evidence next to the PR-size sliders.**

---

## What the journey feels like

Fast and honest at the micro level: from the Overview, "DEL-167 is waiting three weeks" is two clicks away, the ticket page
immediately says "Done in Jira, but PR #53 is still open", the Delivery list names the exact person each PR is waiting on
and it is right in 7 of 8 cases, and the timeline scoped to the ticket is a one-glance story. Unknown tickets, reopened
stories, calendar assumptions and scope banners ("this view is not filtered") are all handled with unusual care, and the
sprint answer is exactly right and easy to find.

It gets shaky when the lead asks the *comparative* questions. "Why did DEL-167 take what it took" is answered with the
measured half of the story and no benchmark, and "who is the constraint / what should we change first" produces three
different answers in three views: People diagnosis blames AlNg/DaKo (an artefact of four bot-closed PRs), Rules says "second
sentinel" (the lab says that does nothing), PR size says "cap PRs" (the lab says that makes things four times worse).
A lead would walk away with a correct list of stuck PRs, a correct sprint verdict, and a **wrong or at least
unreconciled ranking of causes** unless they cross-check against the raw store, as I did.

If I had to give the lead the shortest honest advice from the data: (1) put an owner and an alert on orphaned approvals (#53, #55,
#60: three items, 16-21 business days idle, not capacity); (2) try a review SLA of about 4 h (the only scenario with a
real predicted gain, -9 %); (3) do not cap PR size without more sentinel capacity; (4) stop calling Jira "Done" when the PR is
open (3 of 8 open PRs belong to Done tickets); (5) treat the "refine first" recommendation as unproven.

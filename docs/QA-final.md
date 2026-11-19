# QA — final exploratory user-journey test (delivery lead, desktop 1600×1000)

Build `v0.9.0 · 4a29a271`, tenant `del-combined` (snapshot 2026-11-18 00:10 UTC; 2529 events, 131 PRs, 100 stories, 3 sprints;
the UI says "Snapshot from 2 calendar days ago"). Own server on `:8788` against a copy of `data/del-combined.duckdb`, headless
Chromium through `/api/eval` + `/api/screenshot`. The live `:8766` server and all source were left alone (`git status` clean).
I started from the root URL with no prior knowledge of the product and answered six questions by clicking. Every number quoted
below was read out of the DOM / `/api/*` and then re-derived from DuckDB with my own business-hours calculator (Europe/Tallinn,
09:00-17:00, Mon-Fri, Estonian holidays, DST handled). In my verdicts "Right" means the UI number reproduces,
"Misleading" means the number is right but the framing or headline points the lead the wrong way, and "Wrong" means the number
is not defensible. A "d" in a UI duration is 8 business hours.

Name mapping (never explained in the UI): FrMo = frank (sentinel on 131/131 PRs), AlNg/BoFe/CaOs/DaKo/ErLi = alice/bob/carol/dave/erin,
LaAd = story creator/pointer (not a PR author), BiBi = stale bot.

Screenshots: `/tmp/opencode/qa-final/*.png` (also `/tmp/opencode/qa-final/shots/`); scripts `db.py`, `ev.sh`, `shot.sh` alongside.

---

## One-line answers

1. **DEL-167 / DEL-155.** DEL-167 is *not* really done. Jira closed it on 19 Oct, but PR #53 (160 lines) has no approval and has been idle
   21 business days; the "73.3 h, 13th fastest of 87" answer only measures the easy PR #52. DEL-155 is the slowest of 87 stories (200 h):
   a 60 h gap with no PR open between its two PRs, plus 3 business days waiting for the sentinel on #31.
2. **Stuck in review.** 8 open PRs. The 3 aged ones all have FrMo as holder (#55 19 d and #60 16 d solo, #53 shared with CaOs);
   the other 5 are normal 2-day reviews. Frank is a **tail risk** (two forgotten items), not a throughput constraint: his gate takes 6 h
   median and 22 h at the very worst over 107 merged PRs, and the peer review loop is a bigger slice of PR cycle (44 % vs 26 %).
3. **What first.** The data supports a **review-round cap at 2 (−13 % cycle) / a 4 h review SLA (−9 %)** plus a "chase orphaned
   approvals" habit. A second sentinel is "no clear effect" (−3 %). Smaller PRs are "+289 %" in the lab but that is a model artefact,
   and size barely correlates with cycle (r = 0.20). Refinement (29 % of story cycle) cannot be tested. **Sprint 2 did not deliver:**
   15 of 30 stories (52 of 118 points, 44 %) by its close, 12 finished late, 3 never. And 25 of those 30 were added *after* the sprint started.
4. **Rework.** 5 stories were reopened after Done (DEL-105/137/153/169/185, +16-22 h each, 88 h in total), 4 failed verification and
   went back to in-progress, 21 bounced from review. 31 of 106 merged PRs went 3+ review rounds, and the review loop is 44 % of PR cycle.
   The UI only shows the PR-level rule (whose "cap at 3" binds zero PRs) and per-ticket "reopened ×1"; no aggregate of story-level rework exists.
5. **Will sprint 3 land (ends 27 Nov)?** 25/30 by end; 5 stories / 25 points (20 %) are at risk: DEL-167, 168, 183 (Done in Jira, PRs
   unmerged, 18 pts), DEL-176 (stuck on Frank, 5 pts), DEL-161 (orphaned, 2 pts). The counts are right but the at-risk list has to be
   assembled by hand across 3 views, and DEL-161's story is invisible everywhere.
6. **Can we trust worklogs?** Not for effort. Logged hours are uncorrelated with points (r = −0.05; median 4 h whether the story is 1 or 13 pts)
   and total 0.49× of active PR time. The UI's headline blames *lateness* (1.5 d mean), which is the minor problem (2.5 % are > 1 week late).

**Do the answers reconcile across views?** The counts do: 8 open, 3 aged (21 d 0 h / 19 d 0 h / 16 d 3 h), S2 15/30, S3 25/30, 0/2 approved,
r values. The *diagnoses* do not. "How much of a constraint is Frank" is 1.2× (Overview people diagnosis), 2.2× (blocking map, role level),
3.1× (response time), or 5.6× (blocking map, people level), and the Rules page's #1 recommendation contradicts its own banner.

---

## Question 1 — "DEL-167 PR #53 waiting over a week, Jira says Done. Why did DEL-167 (8 pts) take what it took, and is it really done? (And the month-long DEL-155?)"

### Journey
Overview (`01-overview.png`) → needs-attention row "waiting 1w+ DEL-167 #53 21 d 0 h" → `#ticket/DEL-167` (`02-ticket-167.png`) → PR #53 row
→ PR drawer (`03-pr53-drawer.png`) → Delivery list → All 8 → DEL-167 row → inspector + drawer (`04-delivery-167.png`) → `#ticket/DEL-155`
(`05-ticket-155.png`) → "Copy as Markdown".

### Quoted answers
- Overview: "waiting 1w+ DEL-167 #53 delivery-api 21 d 0 h"; "Done in Jira, PR open — DEL-167 #53 … CaOs + FrMo … unmerged".
- Ticket: "13th of 87 fastest · median 90.1 h · 8-pt median 109 h". "**Done in Jira, but PR #53 is still open (right-censored); the cycle is measured
  to the ticket's done, not to that PR. DEL-167 took 73.31 h from created to done: most of it went to created → ready (26.67 h, 36 % of the measured
  cycle). 16 % of the measured time was active work.**"
- PR drawer: "reviewers 0/2 approved · 2 pending · approvals none · waiting on CaOs + FrMo · open threads 2 · last event 2026-10-19 13:43 UTC".
- Delivery inspector: "Waiting 21 d 0 h on CaOs + FrMo … stale: open 24 business days (limit 5); ticket DEL-167 is Done in Jira but this PR is open".
- DEL-155: "**DEL-155 took 200 h … most of it went to review rounds (123.55 h, 62 %), 60.15 h of which had no PR open. … PR #31 started 7.5 business days after PR #30 merged.**"

### Cross-check (DuckDB)
| Claim | Evidence | Verdict |
|---|---|---|
| DEL-167 73.31 h, 13th of 87, median 90.1, 8-pt median 109 | recomputed: 73.31 h; rank 13/87; median 90.07; 8-pt median 109.0 (n=9) | **Right** |
| 9 segments (26.7/7.4/16/0.6/0/11.2/2.5/1/8 h) | re-derived every boundary pair by hand: all match to 0.1 h | **Right** |
| PR #53 waiting 21 d 0 h; age 24 d; 192 h | last event 19 Oct 13:43 → snapshot = 168.3 bh = 21.0 d; open since 14 Oct 16:10 = 192.0 bh | **Right** |
| "0/2 approved, waiting on CaOs + FrMo" | #53: reviewers added 19 Oct 09:48 (CaOs + FrMo); zero `pr_approved` by anyone; CaOs' one mandatory thread was resolved `walked_back` | **Right** (the 1/2 bug of the previous round is fixed) |
| "Done in Jira" | story → done 19 Oct 12:38; #53's second push is 13:43 the same day. #52 (98 lines) merged 16 Oct; #53 (160 lines) was *opened* 14 Oct 16:10, got its first review 19 Oct, never approved | **Right**, but the ticket page never says "no approvals, 21 d idle" (only the drawer does) |
| "why did it take what it took" = created→ready 36 % | the 73 h is the story to *Jira*-done and the benchmark says it is a fast story. The real elapsed cost (created → today, PR #53 unmerged) is 242 bh and still running; the decomposition only follows PR #52 (13.6 h) | **Misleading** headline; honest caveat sentence present |
| DEL-155 200 h, 87th of 87 | slowest of 87 stories, 2.2× median | **Right** (wording "87th of 87 fastest", see P1) |
| 60.15 h with no PR open, 7.5 bd | #30 merged 9 Oct 09:51 UTC → #31 opened 20 Oct 17:04 UTC = 60.15 bh = 7.52 d | **Right** |
| "most in review rounds 62 %" | 123.6 h = first review 5 Oct → final approval 27 Oct; 60 h of it is the PR gap, and #31 also waited 3 business days for FrMo (AlNg approved 22 Oct 15:31, FrMo 27 Oct 10:32) | **Right now**, the clause makes it honest |
| logged 6 h vs active 10.3 h, 0.58 | worklogs 4 h + 2 h; the second one was logged 12 days late (27 Oct) | **Right** |

### Verdict
Accuracy: high. Helpfulness: good — the previous round's three weaknesses (no benchmark, 1/2 pending, "7.51875 days") are fixed and DEL-155 now
names its gap. What still grinds: (a) the Done-in-Jira ticket page headline answers "why 73 h" but not "is it really done"; that answer (0/2 approved,
CaOs + FrMo owe it, 21 d) lives in the drawer; (b) the Markdown export of the ticket (`Copy as Markdown`) contains the segment table but not
"PR #53: 0/2 approved, waiting 21 d on CaOs + FrMo"; (c) the PR drawer for #53 shows "in progress 3 h 55 min · in review 2 d 3 h · unobserved —" for a PR
that is 24 d old: the 21 days of waiting appear nowhere in the drawer's phase list. Consistent across Overview/list/ticket/drawer except
phase: the list says phase "active" for #53 (the author's last push) while it is waiting on reviewers (P9).

---

## Question 2 — "What is stuck in review right now, and who is the constraint — Frank (sentinel) or the reviewers?"

### Journey
Overview → Delivery list (All 8) → Queue (`06-queue.png`) → Blocking map role level (`07-blocking.png`), sentinel node (`08-blocking-sentinel.png`) → Rules #08
SENTINEL_TAIL + drill-down → Policy lab "who is busy" (`13-policy-cap2.png`) → Overview scoped to FrMo (`18-overview-frmo.png`).

### Quoted answers
- Delivery list: 8 open; FrMo is a holder on all 8: solo on #55 (19 d 0 h), #60 (16 d 3 h), #52 web (2 d 0 h); with CaOs on #53 (21 d); with a peer on #51/#78/#79; #66 waits on the author ("blocked").
- Overview "People diagnosis": FrMo "≈ overloaded ≈1 d 3 h"; ErLi/AlNg/CaOs/BoFe/DaKo "≈ slow ≈1 d 0 h-1 d 1 h".
- Queue: FrMo "2.6 wip · ≈11.1 h mean · peak 6 · ≈ overloaded"; peers 8.7-9.1 h; response time FrMo median 6 h 2 min **×3.1**, peers 1 h 34-2 h 37; "NEVER 24 (63 d 0 h)".
- Blocking map: "author waited on review 167 d 5 h · review waited on sentinel 52 d 1 h · sentinel waited on ready 46 d 7 h"; sentinel node "waited on 75 d 0 h · 122 waits".
- Rules #08: "**2 items waiting only on FrMo for up to 19 d** … Add a second sentinel or a rotation … POLICY LAB: no clear effect (−3 %)".
- Policy lab: sentinel "≈55 % partly busy", reviewers "≈24-30 % idle".

### Cross-check
| Claim | Evidence | Verdict |
|---|---|---|
| 8 open PRs and who holds each | all 8 re-derived from events. #55: ErLi approved 21 Oct, nothing from Frank; #60: BoFe approved 26 Oct, nothing from Frank; #52 web: CaOs approved 13 Nov 15:49; #51 and #78: approvals cleared by pushes; #66: open mandatory thread by CaOs, author owes | **Right** (8/8) |
| Waits 21 d / 19 d / 16 d 3 h | 168.3 / 152.0 / 131.0 bh | **Right** |
| Frank median 6 h 2 min, 3.1× peers | my gate calc (last peer approval → Frank's approval, 107 PRs): median 6.25 h, mean 6.61, p90 12.9, max 22.3 h | **Right** |
| "NEVER 24 (63 d 0 h)" for FrMo | 24 = 16 declined PRs + 8 open PRs on which Frank never acted. Of the 16, 7 were closed by the bot BiBi on 16 Nov (the "63 d" is api#2 opened 17 Aug), and others were declined by their authors within a day or two. Only 3 of the open PRs were actually in Frank's court | **Misleading**: the same bot-closed PRs are excluded from the blocking map (`abandoned_hours` 2223 h, never shown in the UI) but counted here |
| FrMo "overloaded ≈1 d 3 h" (11.1 h mean) | his own gate wait is 5.7 h at role level (Queue "sentinel"), 6.6 h by my calc, 4.9 h in the blocking map. The 11.1 h starts when he was *added as reviewer*, i.e. includes the peer phase the Queue footnote says is "not charged to them" | **Misleading** (~2× overstatement of his own wait) |
| Blocking map people level: FrMo 1,337 person-h vs ≈183-263 h for each peer | person-level "charges every concurrent holder" (footnote). Role level says sentinel 614 h vs review 1,378 h for five people | **Misleading** unless the footnote is read |
| Who holds the most delivery time | merged PRs, my decomposition (mean 25.8 bh): wait for first peer 4.3 h (17 %); review rounds 11.2 h (44 %); Frank's gate 6.6 h (26 %); approval → merge 3.7 h (14 %) | context: Frank is the largest *single-person* stage, the review loop is the largest stage overall |
| "forgotten items" tail | every merged PR had a gate wait ≤ 22 h; #55/#60 are 131-152 h. The tail is 2 PRs that fell off his radar | explains why the lab shows "no clear effect" for a 2nd sentinel |
| sentinel "≈55 % partly busy" vs peers "≈27 % idle" | ρ is a calendar share. The chip text "≈27 % idle" reads as 73 % busy (P3) | **Misleading wording** |

### Verdict
Accuracy of the *list*: perfect. Accuracy of "who is the constraint": the product says four different things depending on the view
(Overview: "FrMo overloaded, rest slow", i.e. 1.2× the next person; the same Overview scoped to `person FrMo` flips to a queue diagnosis where the *review* role is
overloaded at ≈1 d 7 h mean and the sentinel role at only 5 h 42 min (`18-overview-frmo.png`); Rules: Frank is a critical tail; Policy lab: "partly busy" and a 2nd sentinel does nothing). A lead can reach the right
answer ("Frank is a tail-risk bottleneck, fix by chasing #55 / #60 / #52 today, not by hiring a second sentinel") only by reading all five views and
ignoring two headline labels. Helpfulness: good for "what", mixed for "who". Consistency: counts yes, diagnoses no.

---

## Question 3 — "What should we change first to ship faster — refinement, review-round caps, a second sentinel, smaller PRs — and did sprint 2 actually deliver?"

### Journey
Rules (`10-rules.png`, drill-downs `17-rules-drill.png`) → timeframe 1w/4w/All → PR-size sliders → Policy lab, every preset (`12-policy.png`, `13-policy-cap2.png`)
→ Timeline sprint strip (`11-timeline.png`).

### Quoted answers
- Rules banner: "the lab's best scenario is cap review at 2 rounds — −13 % cycle". Then rule **01** WAITING_TIME_DECOMPOSITION: "The team spends 29 % of ticket cycle time in
  the created → ready segment … Fix that phase first" (at stake "≈1.2k h (assumed)"), lab: "+82 % cycle — not a measured win (cannot see the ticket-level wait)".
- #10 REWORK_LOOPS: "Review rounds track cycle time (r = 0.62) — cap them at 3", with "PRs over target 0 · excess rounds 0 · max rounds 3".
- #08 SENTINEL_TAIL: "Add a second sentinel" / lab −3 %, "no clear effect". #03 FLOW_EFFICIENCY: "smaller PRs …" / lab "+289 %".
- PR SIZE panel: "7 of 131 PRs large (10 commits / 400 lines)… r = 0.16 commits · 0.20 lines · 0.61 rounds".
- Sprint 2 (#04): "S2 delivered 15 of 30 committed stories (50 %) by its close — 12 finished late, 3 still open".
- Timeline strip: "S2 closed 11 Sep → 7 Oct · 30 stories · 118 pts · **+25 after start** · 15 of 30 by end · 12 late · 3 open · ↻ 15 out".

### Cross-check
| Claim | Evidence | Verdict |
|---|---|---|
| Lab numbers (−3 / −9 / −13 / −11 / +82 / +289 %), baseline ≈3 d 1 h vs 3 d 0 h measured | re-ran every preset in the UI; measured median PR cycle 24.77 bh = 3 d 0.8 h | **Right** (model validated within 0.9 %) |
| created → ready 29 %, median ≈3 d 2 h | 87 stories: median 26.67 h, mean 27.8 h, 27.7 % of summed cycle | **Right** (the text says "3.4 d", the metric block "3 d 2 h"; 26.67 h = 3.3 d) |
| r = 0.16 / 0.20 / 0.61, 7 flagged, sliders | recomputed over 106 merged PRs: 0.158 / 0.196 / 0.596; 3 PRs > 10 commits, 4 > 400 lines, 7 union; at cap 5: 42 over commits | **Right** |
| "cap review rounds at 3" is "a measured lever" | merged PRs by rounds: 1 → 45, 2 → 30, 3 → 30, 4 → 1 (the 4 is outside the 90-day cohort). In the cohort max = 3, so a cap of 3 binds **zero** PRs; a cap of 2 would bind 29 % of merged PRs | **Misleading**: the card's target (3) is not the lever the lab measured (2) |
| S2 15 of 30, 12 late, 3 never; late 56 d 0 h | story-level: done by 7 Oct 16:00: 15 (52 of 118 pts); `late_hours` 448.04 bh reproduces; never = DEL-138/142/143 | **Right** |
| "committed" | only 2 of 30 stories were in S2 at its start (UI +25 "after start", my count 28 with the first day). A story added on day 20 and finished on day 22 is "delivered" but was never a commitment. S1 and S3 are the same (28 of 30 added after start) | **Misleading**: the Rules card says "committed" and "50 %"; points-weighted it is 44 % |
| "WAITING_TIME #1" | flips to "review rounds 29 % tied with created→ready" on `All`; 1w/4w pick other rules first | **fragile** |

### Verdict
The pieces are individually correct and the caveats are printed. But the page answers "what first?" three ways: #01 refinement (impact *assumed*, the lab
says +82 %), the banner says review-round cap at 2, #10 says cap at 3 (binds nothing), #08 says second sentinel (lab: no effect) and #03 says smaller PRs (lab:
blow-up, and my data says size correlates 0.2). The honest conclusion — the lab's best single lever is the **review-round cap at 2 (−13 %)** then
**review SLA 4 h (−9 %)**, refinement is untestable, a second sentinel does nothing, smaller PRs are not supported — is nowhere stated in one place.
"Did sprint 2 deliver?" → "No: 50 % by stories / 44 % by points", correct, but with an unexplained "committed" denominator.
Helpfulness: medium; consistency: poor between the banner, cards 01/08/10 and the lab.

---

## Question 4 (mine) — "How much rework is hiding in the stories? Which Done stories came back (DEL-153, DEL-185 …), what did it cost, and is PR-level rework the same thing?"

Why: 30 backward status changes exist in the store (21 `in_review → in_progress`, 4 `in_verification → in_progress`, 5 `done → ready`); the product advertises a REWORK_LOOPS rule.

### Journey
Rules (#10 REWORK_LOOPS + 124-item drill-down) → `#ticket/DEL-153` (`14-ticket-153.png`) → `#ticket/DEL-185` (`15-ticket-185.png`) → API for the other three reopens.

### Quoted answers
- Rules #10 (see Q3). Drill-down lists 23 PRs "3 pushes · 3 rounds".
- DEL-153 header: "reopened ×1 … took 110.51 h from created to the first done and 127.51 h to the final done (reopened 1 time)".
- DEL-185: "**most of it went to sprint → in progress (128.75 h, 61 % of the measured cycle)**" for a story with a **104.7 h** cycle.

### Cross-check
| Claim | Evidence | Verdict |
|---|---|---|
| DEL-105/137/153/169/185 reopened ×1, cost +22/16/17/16/17 h | `done → ready` on 27 Aug, 23 Sep, 8 Oct, 22 Oct, 9 Nov; all five reproduce (first done vs final done) | **Right**; 88 h in all |
| Four stories failed verification (DEL-106, 132, 158, 171: `in_verification → in_progress`) and 21 stories bounced from review | present in events; **no view aggregates or flags them** and their ticket pages carry no "reopened" marker | **Missing** |
| DEL-185 "sprint → in progress 128.75 h, 61 %" | the story's sprint boundary is **8 Oct 09:00** (`sprint_added`, actor `system`, `at_start: true`) but it was created **23 Oct 11:00**. Segments sum to 209.7 h = 200 % of the 104.7 h cycle (`explained_share 2.0029`, `clamped 0`). Same for DEL-117 (178 %) and DEL-151 (174 %) | **Wrong** headline (also skews rule #01's "sprint → in progress") |
| REWORK_LOOPS r = 0.62, +6 h/round | my r over the 106 merged PRs = 0.596, slope 6.56 h/round; 50 % of merged PRs have ≥2 rounds (median 2) while the card says "median rounds 1" (cohort includes open/declined PRs) | **Right**, median is **Misleading** |
| "hours per round ≈1 d 6 h" on the same card as "each extra round adds ≈6 h" | 10 h = median cycle ÷ rounds vs 6 h = regression slope | **Confusing** |

### Verdict
Accuracy: per-ticket reopen costs are right; PR-level stats are right; the decomposition has a real bug on 3 stories. Helpfulness: weak — a lead asking
"how much rework?" gets a PR-only rule whose cap binds zero PRs, and no story-level rework number (5 reopened + 4 failed verification + 21 review bounces).
Consistency: the Policy lab calibrates "rework ≈50 % of PRs" (matches), but nothing connects it to the story reopen flag.

---

## Question 5 (mine) — "Will sprint 3 (ends 27 Nov, 7 business days after the snapshot) land? Which stories are at risk — DEL-176, DEL-161, DEL-167/168/183 — and what has to happen?"

### Journey
Timeline sprint strip (S3 row) → Overview → Delivery list → `#ticket/DEL-176` and `#ticket/DEL-161` (`16-ticket-161.png`) → Rules (searched for an S3 card).

### Quoted answers
- Timeline: "S3 active 8 Oct → 18 Nov 40.6 calendar days · 30 stories · 125 pts · +23 after start · **25 of 30 by end · 3 unmerged · 2 open** · plan 2026-11-27"; "10 stories out of sprint scope (10 never planned)".
- Overview "Done in Jira, PR open": DEL-183 #66 (PR opened 6 d 4 h *after* the ticket closed), DEL-167 #53, DEL-168 #55.
- DEL-176: "Still open in Jira, with PR #60 open too … the 6 of 9 segments it can measure cover 68.4 h, most of it in created → ready (24 h, 35 %)".
- DEL-161: "no merged PR names this key; the 6 of 9 segments … cover 56.2 h"; PR table: "#34 delivery-web declined 203 h · 0 rounds".

### Cross-check
| Claim | Evidence | Verdict |
|---|---|---|
| S3 25 of 30 by end, 3 unmerged (18 pts), 2 open (7 pts) | 25 done + merged; DEL-167/168/183 Jira-done with open PRs; DEL-161 (2 pts) and DEL-176 (5 pts) open | **Right** |
| S3 is on track | 25 pts at risk of 125 (20 %): #53 has no approval, #55/#60 sit with FrMo for 16-19 d, #66 has an open mandatory thread by CaOs, DEL-161 needs a new PR | not stated anywhere |
| DEL-161 | PR #34 was approved by DaKo (12 Oct) **and** FrMo (13 Oct), never merged, closed by the bot on 16 Nov after 34 days. The story is open with no live PR | **invisible**: Overview/Delivery list only show open PRs; the ticket page shows "declined 203 h, 0 rounds" and no mention of approvals or the bot |
| DEL-176 "most in created → ready 35 %" | 66 % of its 200 h cycle is *not measured* (final approval → merge missing) and that missing part is PR #60 waiting 16 d 3 h on FrMo | **Misleading** headline for open stories |
| S3 vs S1/S2 | S3 plan = 51 days vs 24.3 / 26.3 for S1/S2 | not flagged; "25 of 30" is not comparable |
| Rules has an S3 card | `SPRINT_DELIVERY`/`CARRY_OVER` only cover closed sprints. At `1w` the critical card is still "S2 delivered 50 %" (S2 closed six weeks before that window) | **Gap** |

### Verdict
Accuracy of every number: right. Helpfulness: partial — a lead sees "25 of 30" quickly but must visit Overview, Delivery list and two ticket pages to learn
which five stories are the risk, and one of them (DEL-161) is not surfaced at all because "stories open without a live PR" is not a list. Consistency:
Timeline "3 unmerged" = Overview "Done in Jira, PR open 3" — good.

---

## Question 6 (mine) — "Can we trust the worklogs? Do the logged hours reflect effort — e.g. DEL-167 logged 6 h for 8 points — and what does the product's worklog-vs-active check say?"

### Journey
Rules #09 (1q window shows one WORKLOG card; `All` shows two) → ticket pages (DEL-167/155/153/161) → API `/api/insights` metrics.

### Quoted answers
- #09 (1q): "**Logging is 1.5 days late on average — don't trust the logged hours** … mean delay 1.47 · median 0.96 · p90 3.99 · late share 2 % · untrusted ≈1 d 0 h · worklogs 169 · logged ≈44 d 5 h".
- `All`: extra card "**Logged hours differ from active PR time by 0.49×** — The team logs 0.49× less time than the PRs show as active work … Agree what a worklog means before either is used for planning".
- Ticket pages: DEL-167 "logged 6 h · active (PR) 10.3 h · 0.58 · logged a week late 50 % (1)"; DEL-155 0.29; DEL-161 1.69.

### Cross-check
| Claim | Evidence | Verdict |
|---|---|---|
| delay mean 1.47-1.52 d, median 0.96-0.97, p90 3.96-3.99; late 2-3 % | 199 worklogs: mean 1.52, median 0.97, p90 3.96; buckets same-day 63 / next-day 85 / ≤week 46 / late 5 (2.5 %). The 1q window counts the 85 stories created in the window = 169 worklogs | **Right** |
| "don't trust the logged hours" | only 5 of 199 (2.5 %) are > 7 d late, against a 25 % threshold; untrusted = 10.45 h of 416.5 h. The headline fires on mean delay 1.5 d vs a 1.0 d target | **Misleading** headline |
| logged ÷ active = 0.49 (appears only on `All`; band 0.5-2.0) | 416.5 h logged vs 846.4 h active PR time | **Right**, but at 1q the ratio is just over the band and the card disappears; same data flips the verdict |
| (not in UI) logged hours vs effort | r(points, logged) = **−0.05**; median 4 h per story for 1, 2, 3, 5, 8 and 13 pt alike (13-pt stories logged 2-3 h); r(cycle, logged) = 0.10 | **Missing** — this is the actual honesty problem |
| "0.49× less" | means "49 % as much" (51 % less) | wording |
| DEL-155 "13 % active" vs "17 h active (PR)" of 200 h | 13 % uses the decomposition's per-segment active time, 17 h/200 h = 8.5 % is PR-level active; two "active" figures, one page | **Confusing** |

### Verdict
Numbers right, emphasis wrong. The product tells the lead to worry about *late* logging, but the data says logging is mostly timely and **meaningless**
(flat in story points). The ratio check is the right tool but is borderline-gated (0.49 vs 0.50) and absent at the default window.

---

## Ranked list of remaining bugs, papercuts and missing features

### Wrong / misleading numbers
1. **"How constrained is Frank" has five different magnitudes** (Overview 11.1 h mean = 1.2× peers; Queue role view 5.7 h; blocking-map role level 4.9 h/wait, 614 h; blocking-map people level 1,337 h = 5.6× any peer; response median 6.0 h = 3.1×). The Overview "FrMo ≈ overloaded 1 d 3 h" includes the peer phase that the Queue footnote says is not charged to the sentinel.
   Repro: Overview → People diagnosis; Queue (level people vs queues); Blocking map level role vs people (`bq.json`, `bp.json` in scratch).
2. **Queue → Response time "NEVER 24 (63 d 0 h)" for FrMo** counts 16 declined PRs (7 closed by the bot, the rest by their authors within days) and 8 open PRs that were not his turn; only 3 are really his. The blocking map already excludes bot-closed PRs (API `abandoned_hours` 2223 h, never displayed); this table does not. Repro: Queue → bottom table, FrMo row.
3. **Decomposition accepts out-of-order boundaries**: DEL-185 (sprint 8 Oct before created 23 Oct), DEL-117, DEL-151 → a "most in sprint → in progress 61 %" headline with segments summing to 174-200 % of the cycle. Repro: `#ticket/DEL-185`; `/api/ticket/DEL-185` → `explained_share: 2.0029`, `clamped: 0`.
4. **Rules page contradicts itself on "what first"**: card #01 (impact assumed, lab +82 %) ranks above the banner's own best lever; #10 recommends "cap at 3" which binds 0 PRs (max rounds = 3, lab's lever is 2); #08 recommends a second sentinel where the lab says "no effect"; #03 recommends smaller PRs where the lab says +289 % (a model artefact: every split PR needs the single sentinel again). On `All`, #01 flips to "review rounds (tied)" and its "POLICY LAB +81 %" annotation (a *refinement* result) is attached to a *review-rounds* card. Repro: Rules at `1q` vs `All`.
5. **"Committed" in sprint cards**: 25-28 of every sprint's 30 stories were added after it started (S2: `+25 after start` in the Timeline strip, not in the Rules card); the 50 % is stories, 44 % is points. S3 is a 51-day sprint (S1/S2 24-26 days) with no flag.
6. **Open-story headlines hide the real wait**: DEL-176's headline says "created → ready 35 %" while 66 % of its cycle is unmeasured and is PR #60 waiting 16 d on FrMo. Same for the "Done in Jira" DEL-167 headline (the PR is only visible in the drawer).
7. **WORKLOG card headline** "don't trust the logged hours" fires on a 1.5 d mean delay when only 2.5 % are > 1 week late; the real defect (logged hours flat vs points, r = −0.05; 0.49× of active) is gated at 0.50 and absent at 1q.
8. Rule #01 text "median story waits 3.4 d" vs the metric block "≈3 d 2 h" (26.7 h = 3.3 d); REWORK card "median rounds 1" vs 2 over merged PRs; "hours per round 1 d 6 h" next to "each extra round adds ≈6 h".

### UI / papercuts
- **Delivery list**: selecting a row opens the inspector *and* the drawer with the same facts; the inspector sits on top of the table and truncates the TICKET / PR column to "…" and hides IMPACT; the header "TICKET / PR" overlaps "WAITING ON" (`04-delivery-167.png`). Status chip cells are truncated ("in review …", "waiting 1w+ …", i.e. "stale"/"unobserved" flags are cut).
- **Queue response-time table truncates its numbers at 1600 px** ("6 h 2 m…", "≈6 h 40…", "24 (63 …") (`06-queue.png`); the footnote paragraph is repeated twice.
- Wording: "87th of 87 fastest" / "74th of 87 fastest" (it means slowest; use "slowest of 87"); "logs 0.49× less"; Policy-lab chips "≈27 % idle" for ρ = 27 % busy (reads as 73 % busy); "sentinel waited on ready 46 d 7 h" (means PRs waited for merge after approval); "unobserved" on all 8 rows with no inline definition; "d" = 8 h only defined once on the Overview; Timeline lifecycle in "calendar days" (802 d "in review") under a "business hours" header.
- **Phase "active"** for #53 (only row) although it waits on CaOs + FrMo and "waiting on author: no".
- **Drawer** lacks "waiting since / waiting 21 d" and its phase list sums to ~2 d 7 h for a 24 d PR.
- Timeframe: `1w` shows 3 critical rules (S2 delivery, stale PRs, sentinel tail) unrelated to the week; default `1q` is 90 days against a 103.6-day dataset, silently dropping 7 PRs while the header says 131.
- Context menu on a Delivery-list row offers "Scope to ErLi" (the author) but never the holder (FrMo); with a synthetic contextmenu event the menu rendered at the bottom-left, not at the pointer (not confirmed with a real mouse).
- Blocking map is canvas-only: no text/table of nodes and edges, so numbers are readable only after hover or by scripting; node labels use "1348h" whereas every other view uses "d h".
- Scope banner "this view is not filtered" appears on non-filtered pages, yet setting `person FrMo` still changes Overview content (people → queue diagnosis, 304 d → 340 d measured wait).
- "Copy as Markdown" for a Done-in-Jira ticket omits the open-PR wait/approval state; on a page where several panes share the label, a script-level find picks the hidden Delivery list's button (harmless for users).

### Missing features
- **No repo dimension** anywhere (`/api/events`/`objects` accept `repo`, but Scope, Queue, Blocking, Rules, Lab do not). "Is delivery-api or delivery-web slower?" cannot be answered. (From the store: merged median cycle 25.0 h vs 24.0 h, first response 2.9 vs 2.0 h, sentinel gate 5.8 vs 6.3 h, so no real difference; all 3 aged PRs are api.)
- **No trend / time series** (flow efficiency, cycle time, sprint delivery by week); every figure is a single window aggregate.
- **No story-level rework aggregate** (reopened, failed verification, review bounce) and no "open story without a live PR" or "approved but never merged" list (DEL-161).
- **No sprint-at-risk / forecast** for the active sprint; rules only judge closed sprints.
- **No worklog-vs-points** view; **no pseudonym legend** (who is the sentinel is only inferable from the Queue "Who is who").
- No "review SLA / chase" action surfaced for the one thing the data supports (nudging #55 / #60 / #52 today).

---

## Did the product get better, and what still grinds

Compared with the previous exploratory round (`docs/QA-fresh.md`), these are fixed and verified: the ticket page now benchmarks ("13th of 87 fastest · median 90.1 h · 8-pt median 109 h");
the PR drawer shows reviewers/approvals/open threads/last event ("0/2 approved"); the DEL-155 headline names the 60 h no-PR gap and "7.5 business days" (no more "7.51875");
"Done in Jira, PR open" is a first-class Overview list with who owes each and the DEL-183 "PR opened 6 d 4 h after the ticket closed" call-out; the blocking map no longer
blames peers for bot-closed zombies; response times (median/×3.1 and "last peer answer first") exist; rule #01 admits ties and carries "assumed" labels; the lab caveats
("not a measured win", "cannot see the forgotten-item tail") are printed on the cards; the sprint strip shows scope creep; error states and calendar assumptions are exemplary. Every
arithmetic claim I re-derived (>50 numbers) reproduced; there were no wrong totals except the DEL-117/151/185 decomposition.

What still grinds: the product is now *accurate but not decisive*. Each view is internally right, but a lead asking "who is the constraint?" or "what first?" gets
different answers in different views because the metrics use different clocks (person-level vs role-level vs response, "committed" = ever-added, "overloaded" = relative to the median) and the cards do not
point at each other. The three aged PRs, the DEL-167 verdict, S2's 15/30 and the lab numbers are all findable in two or three clicks; the conclusions that connect them
(Frank is a tail risk, the review loop is the larger cost, round-cap at 2 is the best lever, worklogs are noise, DEL-161 is an orphan) must be assembled by the reader.
Layout debt remains at 1600 px (Delivery list inspector over the table, truncated Queue table). Highest-leverage next fixes: reconcile the sentinel numbers (#1, #2 above), make rule #10
target the lab's lever, validate boundary order in the decomposition, and add a repo scope plus a story-level rework list.

# QA — verification user-journey test (fresh eyes, desktop 1600×1000)

Build `v0.9.0-51-g4db72050`, tenant `del-combined` (snapshot 2026-11-18 00:10 UTC, 2529 events, 131 PRs, 100 stories,
3 sprints). Own server on `:8787` against a copy of `data/del-combined.duckdb`, headless Chromium through `/api/eval` +
`/api/screenshot`. The live `:8766` server and all source were left alone; only this file and `/tmp/opencode/qa-verify/`
were written.

Method: each question was answered by clicking through the UI from the root URL. The answer text came out of the DOM
(`/api/eval`). Every claim was then re-derived from the DuckDB copy by scripts of my own (`/tmp/opencode/qa-verify/*.py`),
with an independent business-hours calculator: Europe/Tallinn (EEST→EET on 25 Oct), 09:00-17:00, Mon-Fri, 20 Aug holiday.
"d" in UI durations is an 8-hour business day. Names: AlNg/BoFe/CaOs/DaKo/ErLi = authors who also review,
**FrMo = frank (sentinel, mandatory approver on 131/131 PRs)**, LaAd = the person who creates, points and sprints stories.

Screenshots (`/tmp/opencode/qa-verify/shots/`): `01-overview`, `02-ticket-167`, `03-pr53-drawer`, `05-list-167`,
`06-ticket-155`, `07-timeline-167` (the unscoped bug), `08-timeline-scoped-167`, `09-queue`, `10-blocking`,
`11-rules`, `13-policy-lab`, `14-policy-lab-roundcap`, `15-blocking-people`, `17-timeline-s2`.

Verdict key: **Right** = matches the store; **Misleading** = number is correct but the framing or label leads to the wrong
conclusion; **Wrong** = does not match the store.

---

## One-line answers

1. **DEL-167 / DEL-155.** DEL-167 is **not really done**: Jira closed it on 19 Oct, but PR #53 (160 of the story's 258 lines, 62%)
   was never approved or merged. It is stuck on CaOs + FrMo, last touched 19 Oct 13:43, and has been idle 21 business days.
   The 73 h "cycle" is real but measures only to the Jira close, and 26.7 h (36%) of it was created → ready. DEL-155
   (200 h, the slowest of 87) is 35 calendar days: 21 h refinement, a first PR that merged on 9 Oct, **60 h with no PR open**, then
   a second PR by a different author that waited about 19.5 h for Frank after a peer approved it.
2. **Stuck in review now.** 8 open PRs. Three wait on Frank alone, with every peer already done: #55 (19 d), #60 (16 d), #52 (2 d). #53 waits on CaOs and
   Frank and its ticket is already Done. #66 is blocked on its author and was opened 6 d after its ticket closed.
   #51, #78 and #79 are ordinary 2-day reviewer ping-pong. **Frank is the constraint**, per person and in the tail: he holds 5× any
   peer's wait (1323 h vs about 250 h). The *size* of the "×10.7 slower" headline is overstated (see bug R2), and the review role as a whole holds
   the largest pool of wait.
3. **What first.** Evidence says **cap review rounds (the lab's best result, −13% cycle, +4.7% delivered work), and chase orphaned
   sentinel approvals**. A review SLA is second (−9%, a "mixed" verdict). A second sentinel alone shows "no clear effect" (−3%).
   Smaller PRs are harmful in the model (+289%, sentinel ρ 122%) and weak in the data (r = 0.20). The lab's "refine before start" also
   blows up (+82%). **Sprint 2 did not deliver**: 15 of 30 stories (52 of 118 points) by its close, 12 late (56 d of slip), 3 never.
   **The Rules page and the Policy lab give contradictory "what first" answers** (bug R1).

**Do the answers reconcile across views?** Mostly yes on facts and no on advice.
- *Facts.* Overview, Delivery list, ticket page, PR drawer, Rules and Timeline agree on the DEL-167 story, the open-PR list, and the sprint 2 numbers.
  Overview people-diagnosis and Queue now agree on Frank (the old AlNg/DaKo bug is fixed).
- *Constraint.* Direction agrees everywhere (Frank / sentinel), but the severity and the remedy differ (Rules says "add a second sentinel", the lab says "no clear effect").
- *Advice.* Rules and the Policy lab contradict each other on refinement, smaller PRs and a second sentinel.
- *Detail.* A few detail numbers differ between the two drawers for the same PR.

---

## Question 1 — "DEL-167 PR #53 waiting over a week, Jira says Done. Why did DEL-167 (8 pts) take what it took, and is it really done? (And the month-long DEL-155?)"

### Journey
1. `/` → **Overview**. "Needs attention" lists `waiting 1w+ DEL-167 #53 delivery-api 21 d 0 h`. A "Done in Jira, PR open" panel lists 3 items (`01-overview`).
2. Click the `DEL-167` link → `#ticket/DEL-167` "Why did this ticket take this long?" (`02-ticket-167`).
3. Click the PR #53 row → PR drawer (`03-pr53-drawer`).
4. Delivery list → "All 8" → click DEL-167 → list inspector plus drawer (`05-list-167`).
5. Drawer → **Timeline** chip → scoped timeline `ticket:DEL-167`, 27 of 27 events (`08-timeline-scoped-167`). The same chip from the ticket page's drawer does not scope (bug B1).
6. Go-to-ticket → `#ticket/DEL-155` (`06-ticket-155`). An unknown key (`DEL-999`) gives a clean "No ticket DEL-999 in dataset".

### Quoted answer
- Ticket page: "**Done in Jira, but PR #53 is still open (right-censored); the cycle is measured to the ticket's done, not to that PR.
  DEL-167 took 73.31 h from created to done: most of it went to created → ready (26.67 h, 36% of the measured cycle).**"
  Header: `73.3 h cycle · 13th of 87 fastest · median 90.1 h · 8-pt median 109 h`. PR table: `#52 merged 13.6 h`, `#53 open · censored 192 h, rounds 2`.
- Delivery inspector: "Waiting 21 d 0 h on CaOs + FrMo … stale: open 24 business days (limit 5); **ticket DEL-167 is Done in Jira but this PR is open**".
- Rules #7 STALE_PRS: "one whose ticket is already Done is finished work the team cannot deliver or count".
- DEL-155: "took 200 h … most of it went to review rounds (123.55 h, 62%), **60.15 h of which had no PR open** … PR #31 started 7.5 business days after PR #30 merged."

### Cross-check (store)

| Claim | Store | Verdict |
|---|---|---|
| DEL-167 created 6 Oct 11:20 → done 19 Oct 12:38 = 73.3 h | independent calc 73.31 h | Right |
| created → ready 26.67 h (36%) | 6 Oct 14:20 local → 9 Oct 17:00 local = 26.67 h | Right |
| 13th of 87 fastest, median 90.1 h, 8-pt median 109 h | 13 / 87, 90.07 h, 109.0 h | Right |
| PR #53 open, never approved, 5 commits, 2 rounds | `pr_opened` 14 Oct 16:10; no `pr_approved`; 5 COMMIT objects; 2 `pr_pushed` | Right |
| "waiting 21 d 0 h" | last event 19 Oct 13:43 → snapshot = 21 d 0.3 h | Right |
| #53 opened 48 min after #52 and holds 62% of the story's lines | 160 of 98+160 lines; **not stated anywhere in the UI** | Missing |
| Done 19 Oct 12:38, yet #53 had 2 pushes and a comment at 13:13-13:43 the same day | confirmed | Right, but the UI does not call out "work continued after Done" |
| "13th of 87 fastest" as a headline | The rank rests on a Jira Done that ignores the unmerged PR | **Misleading** (sits next to the censored note but is not itself flagged) |
| DEL-155 200 h, rank 87/87, created → ready 20.75 h, review 123.55 h | 200.0 h, 87, 20.75, 123.53 | Right |
| "60.15 h no PR open" and "7.5 business days after #30 merged" | #30 merged 9 Oct 09:51 → #31 opened 20 Oct 17:04 = 60.15 h = 7.52 d | Right |
| Header chip "most in review rounds (62%)" | real PR review time is only about 63 h (#30 27.9 h + #31 35.5 h); 60 h is a gap | **Misleading** (the sentence under it is honest, the chip and headline are not) |
| DEL-155 sentinel wait | AlNg approved #31 22 Oct 15:31, Frank 27 Oct 10:32 = 19.5 h; not mentioned for the ticket | Missing |
| Overview "Done in Jira, PR open: 3" (DEL-167/168/183) | matches the store; DEL-183 #66 opened 52.7 h (6 d 4 h) after Done | Right |

**Verdict (accuracy):** all numbers are correct and the key caveat ("Done in Jira, but PR #53 is still open") now sits in the first sentence of the answer. **Consistency:**
the ticket page, Overview, list inspector, drawer and Rules agree on state, ticket, waiting-on (CaOs + FrMo), age (24 d = 192 h) and the closed time.
Three differences remain (bugs D1-D3): the two drawers disagree on "commits" and the in-progress/in-review spans, and the list
labels the last event differently. Answer to "is it really done": **no**, and the UI says so, but never quantifies how much of the story is unshipped.

---

## Question 2 — "What is stuck in review right now, and who is the constraint — Frank (sentinel) or the reviewers?"

### Journey
Overview ("Where waits accumulate", "People diagnosis") → Delivery list "All 8" → Queue (people) → Blocking map (role, then people) → Rules → Policy lab.

### Quoted answers per view
- **Overview**: "8 open items · 1 blocked · 3 waiting over a week". People diagnosis: `FrMo ≈ overloaded ≈1 d 3 h; ErLi/AlNg/CaOs/BoFe/DaKo ≈ slow ≈1 d 0-1 h`.
  Where waits accumulate: `author waited on review 167 d 5 h · review waited on sentinel 52 d 1 h · sentinel waited on ready 46 d 7 h`.
- **Delivery list (All 8)**: #66 blocked (author); #51 DaKo+FrMo 2 d 4 h; #52 FrMo 2 d 0 h; **#53 CaOs+FrMo 21 d 0 h; #55 FrMo 19 d 0 h; #60 FrMo 16 d 3 h**; #79 BoFe+FrMo 2 d 1 h; #78 ErLi+FrMo 2 d 0 h.
- **Queue**: FrMo `overloaded`, 2.6 wip. Response: `FrMo median 2 d 5 h ×10.7, never 24, PEER → SENTINEL 5 h 45 min` against peers at about 1.5-2.6 h.
- **Rules #8 SENTINEL_TAIL (critical)**: "2 items waiting only on FrMo for up to 19 d (threshold 3 d) … Add a second sentinel or a rotation."
- **Policy lab**: bottleneck `sentinel:1 ≈55% busy` ("partly busy"); reviewers 24-30% busy, "idle". Sentinel mean wait ≈2 h 45 min, reviewers 26-55 min.
- **Blocking map**: role level, `review` 1348 h > `sentinel` 600 h > `ready` 376 h > `author` 115 h. People level, **`FrMo` 1323 h vs 183-252 h for each peer** (`10-blocking`, `15-blocking-people`).

### Cross-check (store)

| Claim | Store | Verdict |
|---|---|---|
| The 8 open PRs and each "waiting on / since / how long" | every row re-derived. #55: ErLi approved 21 Oct 15:37, Frank never, 152 h = 19 d. #60: BoFe approved 26 Oct 11:23, 131.6 h = 16 d 3 h. #52: CaOs approved 13 Nov. #51: DaKo's approval lost by a push. #66: CaOs's mandatory comment unanswered. **#53 has no approval from anyone.** | Right (8/8) |
| Rules: "2 items waiting only on FrMo up to 19 d", hours 283.6 | 152.0 + 131.6 = 283.6 | Right |
| Queue: FrMo median 20.6 h (UI "2 d 5 h"), mean 20.4, never 24; peers 1.6-2.7 h | my recompute: 20.63 / 20.37 / 24; AlNg 2.67, BoFe 1.59, CaOs 1.78, DaKo 1.90, ErLi 1.98 | Right |
| "FrMo ×10.7 slower than peers" as evidence of overload | He **never** approves before the last peer (0 of 106 merged PRs). The clock starts at "added as reviewer", so it includes the peer phase (median 14.2 h). Time from last peer approval to Frank is a median **6.0 h** (p90 12.9 h, **max under 24 h**); the UI's own last column says 5 h 45 min. | **Misleading** |
| Overview "waiting on CaOs + FrMo / DaKo + FrMo / ErLi + FrMo" | Frank is gated behind the peer on these rows (4 of 7). Frank-only rows are #52, #55, #60. | **Misleading** (lists Frank as a holder where he cannot act) |
| People blocking: FrMo 1323 h, 5× any peer | `/api/blocking?level=person`: FrMo 1337 h (solo 841 h) vs 183-263 h | Right |
| Role blocking: review 1348 h largest | 5 reviewers pooled (about 275 h each). Totals by role-level 304 d vs people-level 340 d | **Misleading** unless the reader knows it is a pool |
| Policy lab sentinel ρ 55%, gate 5.76 h | consistent with 6.0 h median / 5 h 45 min Queue | Right |
| Typical sentinel phase vs peer phase (merged PRs) | peer phase (open → last peer approval) 1644 h, sentinel phase 699 h, plus 283.6 h on the 2 stuck items | context: reviewers plus authors hold more of the *volume*, Frank holds the *tail* |

### Consistency of the constraint answer across views
- **Overview people-diagnosis = Queue**: identical (FrMo overloaded, ≈1 d 3 h = 11.1 h; the five peers "slow" ≈9 h). The earlier AlNg/DaKo bug is gone. ✔
- **Rules**: names FrMo (SENTINEL_TAIL); its "people" toggle says "No person-level rule crosses its threshold" even though a person is named at team level and Overview calls one person overloaded. ✘ (papercut)
- **Policy lab**: agrees the sentinel is *the bottleneck*, but calls him "≈55% busy, partly busy" (the footnote reconciles "busy" vs "overloaded"), and says the second sentinel yields "no clear effect" while Rules #8 says "critical, add one". ✘ on the remedy.
- **Blocking map**: role view points at `review`, people view at `FrMo`. ✘ (no hint these differ by aggregation).
- Overview tile "longest mean wait ≈2 d 6 h" under "1 overloaded person" is the synthetic `review` stream (22.6 h), not any person (max 1 d 3 h in the table beneath). ✘ (see bug R4; source `app/tables.js:323`).

**Verdict:** the "what is stuck" list is exact. The "who is the constraint" answer is directionally consistent (Frank) but **overstated** in the Queue's ×10.7 and the "waiting on …+ FrMo" lists. The honest finding, which no view states plainly, is: *Frank adds a median 6 h to a PR (≈3 peer responses), and two PRs have been left untouched by him for 16-19 days while none of the 106 merged PRs waited over a day. It is a serial gate with a forgotten-item tail, not a saturated reviewer.*

---

## Question 3 — "What should we change first to ship faster — refinement, review-round caps, a second sentinel, smaller PRs — and did sprint 2 actually deliver?"

### Journey
Rules (all 9 + PR SIZE panel) → Policy lab: run the presets **add a sentinel**, **review SLA 4 h**, **cap PRs at 3 commits**, **refine before start**, **cap review at 2 rounds** (`13-policy-lab`, `14-policy-lab-roundcap`) → Timeline sprint strip and S2 lens (`17-timeline-s2`) → ticket pages' sprint chips.

### What each view recommends

| Lever | Rules | Policy lab | Store check |
|---|---|---|---|
| Refinement | **#1 (warning, ranked first)**: "29% of ticket cycle in created → ready. Refine before the sprint … fix that phase first." Impact ≈1.2k h is flagged `assumed` ("half the largest segment's hours — not a measured saving"). | "refine before start" → median cycle **+82%**, flow efficiency −74%, verdict "blow-up … no net win" | created → ready is genuinely the largest segment (e.g. DEL-167 36%); the lab models refinement as *added* work on the PR clock (open → merge) and cannot see the ticket-level benefit |
| Review-round cap | not a rule (only "3 rounds max" as a target); PR SIZE panel shows **r = 0.61 for review rounds** | "cap review at 2 rounds" → **−13.0% median, −15.8% p90, +4.7% delivered work** — best result | r(rounds, cycle) = 0.60. Merged median 18.8 h at 1 round, 25.5 h at 2, 31.8 h at 3, so about 6.5 h per extra round. A 2-round cap saves at most about 6 h on 30 of 106 PRs, so −13% is optimistic but the right direction. |
| Second sentinel | #8 (critical): "Add a second sentinel or a rotation" | −3.0% median, "no clear effect … smaller than run-to-run spread" | the lab cannot see the 16-19 d tail |
| Smaller PRs | #3 text: "a first-response SLA, **smaller PRs**, and a daily review block"; PR SIZE panel: "weak, so capping size is not a proven lever by itself" | 3-commit cap → **+289%**, sentinel ρ **122%** | r commits 0.16, r lines 0.20 (UI 0.16 / 0.20): Right. Median PR is 5 commits; every split PR needs a full Frank approval. |
| Review SLA | #3: "first-response SLA" | 4 h → −9.3% cycle, but "delivered work 8.4% less" (verdict "mixed") | n/a. Odd: a policy that shortens cycle by 9% yet cuts delivered work by 8% needs an explanation the UI does not give. |

### Sprint 2 — cross-check

| Claim | Where | Store | Verdict |
|---|---|---|---|
| S2: 30 stories, 118 pts, **15 done by end, 12 late, 3 never** | Timeline strip; S2 lens; Rules #4 | 15 / 12 / 3; 118 pts; the 3 never are DEL-138/142/143 | Right, and identical in all three places |
| "56 d 0 h of slip" | S2 lens; Rules #4 | `/api/sprints` `late_hours` 448.04 = 56.0 d. My own calc gives 58.1 d because I used each story's *last* done; DEL-153 was done on 8 Oct, reopened, done again on 12 Oct, and the UI uses the first done. | Right (definition) — but ticket pages use the last done (DEL-153), so the same story has two done dates in the UI |
| "50% delivered" (Rules) | Rules #4 | by stories 15/30; **by points 52/118 = 44%** | Right, but story-count framing is generous (points never shown) |
| 0 of 2 closed sprints on time; 27 carried over / 20 late | Timeline strip | 12 + 15 = 27; 8 + 12 = 20 late; 4 + 3 = 7 never | Right |
| "24 carried over / 75 stories in a sprint" | Rules #5 | window-limited; the page itself says "the sprint lens counts every store story, so it can be larger" | Right (explained; still a visible 24 vs 27) |
| S3 "28 of 30 by end · 2 open · plan 27 Nov" | Timeline strip | 28 stories are Jira-Done, **but 3 of them (DEL-167, 168, 183) have unmerged PRs**, so truly delivered = 25. "by end" is shown for a sprint that has not ended. | **Misleading** |
| Ticket pages' sprint chip "done by end" | DEL-167 (S3), DEL-155 (S2 "finished late") | DEL-155 finished 22 days after S2 closed: "finished late" Right; DEL-167's "done by end" on an active sprint is odd | Right/Misleading |

**Verdict (accuracy):** every sprint number is correct and the three sprint surfaces agree. S2 clearly did not deliver (S1 60%, S2 50%, S3 pending). **Consistency of the advice: poor** — see R1.

---

## Remaining bugs, papercuts and missing features (ranked)

**R1 (High) — Rules and Policy lab give contradictory "what should we change first" answers.** Both pages carry the same subtitle.
Repro: Rules → #1 "refine first (assumed ≈1.2k h)"; Policy lab → "refine before start" → +82% / "no net win".
Likewise Rules #8 "add a second sentinel (critical)" vs lab −3% "no clear effect"; and Rules #3 "smaller PRs" vs Rules PR SIZE "not proven" vs lab +289%.
The one lever the data supports most strongly (review-round cap: r 0.61, lab −13%) is **not a Rules item at all**.
Rules ranks an `impact assumed` rule first. Suggest: reconcile in copy (the lab models refinement as added work and cannot see ticket-level wait; the lab cannot see the orphan tail),
add a "round cap" rule, and rank measured-impact rules above assumed ones.

**R2 (High) — "FrMo ×10.7 slower / overloaded" overstates Frank's own latency.** Queue → Response time. His clock starts at "added as reviewer" but he never acts before the last peer (0/106),
so 14 h of the 20.6 h is the peer phase. Real post-gate lag: median 6.0 h, p90 12.9 h, max under 24 h on merged PRs. The red badge, "overloaded", and Overview/Delivery "waiting on X + FrMo" (4 of 7 rows where he is not yet actionable)
all inherit this. The real, un-flagged finding is the 2 forgotten PRs (#55, #60). Suggest: show "actionable now" holders, and an "untouched by sentinel since N d" alert.

**B1 (High) — Timeline chip from a PR drawer opened on the *ticket page* does not scope.** Repro: `#ticket/DEL-167` → click the #53 row → drawer → "Timeline": opens the whole team
timeline (2421 of 2529 events, scope `everything`, no banner, `07-timeline-167`). From the Delivery-list drawer the same chip works (`ticket:DEL-167`, 27 events). Reproduced twice.

**R4 (Medium) — Overview tile "1 overloaded person · 5 slow · longest mean wait ≈2 d 6 h"** takes `streams[0]`, the synthetic `review` stream (22.6 h), not a person (max FrMo ≈1 d 3 h, shown in the table below it).
The counts exclude queues but this figure does not (`app/tables.js:323`).

**D1-D3 (Medium) — same PR, two drawers, different numbers.** PR #53: ticket-page drawer shows `in progress 3 h 55 min · in review 2 d 3 h · commits 5 · last event comment_added`;
list drawer shows `3 h 25 min · 4 calendar days 18 h · commits 5` and the list inspector says `commits in log 2`, last event `author_reply`. A business-hours span (3 h 55) cannot exceed the calendar span (3 h 25) of the same interval, so these are different intervals under one name.
The store has 5 commits.

**S3 (Medium) — sprint lens trusts Jira Done.** S3 "28 of 30 by end" includes DEL-167/168/183 whose PRs are unmerged (true 25), and says "by end" for an active sprint (also on ticket-page chips).

**P1 (Medium) — people vs role blocking views disagree on the headline.** Role: `review` 1348 h is the biggest node; people: `FrMo` 1323 h is 5× anyone. The Overview "measured wait ≈304 d 6 h" is the role total; people view says 340 d 0 h. A `review` synthetic node (11 PRs, 248 h) also appears in the people graph unexplained.

**M1 (Medium, missing) — no "how much of the story is unshipped" and no "work continued after Done".** DEL-167: 62% of lines unmerged and 2 pushes plus comments an hour after Done, but this is only inferable from the PR table. DEL-183's ticket page does not say its PR opened 6 d after the ticket closed (Overview does).

**H1 (Low-Med) — DEL-155 header chip "most in review rounds (62%)"** (and the matching rule text) counts a 60 h no-PR gap and sentinel waits as review rounds. The ticket sentence below it corrects this, the chip does not. Also "87th of 87 fastest" reads backwards; "slowest of 87" is clearer. The "13th of 87 fastest" for DEL-167 is not marked as unreliable.

**L1 (Low) — layout.**
- Delivery list: with the inspector and the drawer both open they duplicate each other and the TICKET/PR column collapses to "…" (`05-list-167`).
- Queue response table values are clipped with "…" at 1600 px (`09-queue`).
- Timeline with a custom range makes the header wrap (`17-timeline-s2`).
- S2 lens says "the timeline shows this sprint's stories" but the PR lifecycle still lists all 131 PRs from DEL-100.

**L2 (Low) — copy / definitions.**
- Rules COMMENT_TAX "≈1 d 6 h per round" is median cycle ÷ rounds; the marginal cost per extra round in the data is about 6.5 h, so the ≈98 d at stake is roughly 2× generous.
- Two different "active" figures on one ticket page (e.g. DEL-167 "16% active" ≈ 11.7 h vs "active (PR) 10.3 h").
- Rules "people" toggle says nothing crosses a threshold, though SENTINEL_TAIL names a person.
- Reopened stories (DEL-105/137/153/169/185) use the first done in sprint slip and the last done in ticket cycle.
- Review SLA 4 h reports −8.4% delivered work with no explanation.

---

## Did the product get better?

**Yes, clearly, on correctness and on the DEL-167 story; not yet on advice.** Compared with `docs/QA-fresh.md` (same dataset):

- ✔ The Overview people-diagnosis now names **FrMo** (it used to point at AlNg/DaKo) and matches the Queue to the hour.
- ✔ The DEL-167 ticket page now leads with "Done in Jira, but PR #53 is still open", flags the PR as "right-censored", and the Overview has a "Done in Jira, PR open" panel (3 items) with who owes and a "PR opened after ticket closed" note for DEL-183.
- ✔ DEL-155 now says "60.15 h of which had no PR open" and "PR #31 started 7.5 business days after PR #30 merged" (both verified).
- ✔ The sprint surfaces are consistent: Timeline strip, S2 lens, Rules and `/api/sprints` all say 15 / 12 / 3 / 118 pts / 56 d 0 h. The carry-over count mismatch (24 vs 27) is now explained on the page.
- ✔ The Policy lab gained the knobs the question asked about (refine before start, review-round cap, sentinels, PR size) and a validated baseline (+0.9% vs measured). Its verdicts ("no clear effect", "blow-up") are honest.
- ✘ New contradictions appeared *because* the lab now covers more levers: Rules and the lab disagree on refinement, second sentinel and smaller PRs (R1).
- ✘ The headline "Frank ×10.7 / overloaded" is still more dramatic than the data (R2), and one Timeline pivot silently fails (B1).

All figures were re-derived from `data/del-combined.duckdb` (copy); scripts and screenshots are in `/tmp/opencode/qa-verify/`.

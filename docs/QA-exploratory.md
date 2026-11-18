# QA — exploratory user-journey test (three questions on the `del-combined` team)

Method: I played a delivery lead who has just opened megaspeed on the pseudonymised `DEL` team
(`data/del-combined.duckdb`: 100 stories, 131 PRs, 3 sprints, 6 people + sentinel approver). I started at the root URL
and clicked only. I worked in headless Chromium at 1600×1000, driven through `/api/eval`, `/api/screenshot` and `/ws`.
Each answer was cross-checked against the DuckDB store with my own scripts.

* Build under test: `v0.9.0-25-g3e3315d1`. It ran on its own server (`:8784`, a copy of the store at
  `/tmp/opencode/explore/del-combined.duckdb`). The live `:8766` server was not touched.
* Scripts and artefacts: `/tmp/opencode/explore/` (`tix.py` ticket timelines, `bh.py` business-hours calculator,
  `bc.py` ball-court loader, `ev.sh`, `shot.sh`, `click.sh`, `mclick.sh`, `insights.json`).
* Screenshots: `/tmp/opencode/explore/shots/*.png` (index at the end).
* Not tested: the phone viewport (412×915). The harness page cannot be resized from `/api/eval`. Everything below is desktop.
* **Business-hours checker.** My `bh.py` reimplements the calendar (Mon–Fri 09–17 Europe/Tallinn). Timestamps in the UI and
  the store are **UTC**, so 14:20 UTC is 17:20 local, after hours. Estonian holidays also matter: 20 Aug was a public holiday.
  I first thought `DEL-105`'s "0 h" segments were wrong. After adding the holiday they matched. I only report a number as
  wrong if it still disagrees after that.

---

## 1. The three questions, and why the data surfaces them

What the data looks like, from the store and `corpus/state.md`:

* 100 stories. 25 have backward status moves: 21 `in_review→in_progress`, 4 `in_verification→in_progress`, and
  **5 reopened** (`done→ready`: DEL-105, 137, 153, 169, 185).
* 31 stories have two PRs.
* **Open PRs: 8.** Three are old: #53 (DEL-167), #55 (DEL-168), #60 (DEL-176). `DEL-167` and `DEL-168` are *Done* in Jira.
* **A bot, `BiBi`** (`actor_kind: bot`), declined **7 PRs at one instant (2026-11-16 00:03:20)**. They were 37–90 days old
  (`delivery-api#2` 90.4 d, `delivery-web#8` 80.4 d, `#18` 61.5 d, `#24` 54.5 d, `api#42` 46.3 d, `web#17` 44.4 d, `#34` 37.5 d).
* **Sentinel `FrMo`** is the mandatory last approver on every PR. He approved 107 of 107 PRs *after* a human, never first.
* Sprints: S1 and S2 are closed with stories still open and others finished weeks after the sprint end. S3 is active.

| # | Question (as the lead would put it) | Why it surfaces |
|---|---|---|
| **Q1** | **"The overview says `DEL-167` PR #53 has been waiting over a week, yet Jira says DEL-167 is Done. Why did DEL-167 (8 pts) take what it took, and is it really done? (And the month-long `DEL-155`?)"** | DEL-167 is Done 19 Oct while its second PR #53 has been open and unapproved since 14 Oct. DEL-155 (5 pts, S2) took 35 calendar days with 22 of them in `in_review`. DEL-105 was done, reopened and done again. |
| **Q2** | **"What is stuck in review right now, and who is the constraint — is it Frank (sentinel) or the reviewers?"** | The store has 3 open PRs older than 3 weeks. `#55` and `#60` have one human approval and are only missing Frank. 7 zombie PRs were bot-declined after months. |
| **Q3** | **"What should we change first to ship faster — refinement, review-round caps, a second sentinel, smaller PRs — and did sprint 2 actually deliver?"** | Rules and Policy lab are the product's answer. Sprint 2 shows "27 of 30 done" on its chip, but 12 of those finished after the sprint ended. |

---

## 2. Q1 — "Why did DEL-167 take this long, and is it really done?"

### Journey (clicks)

1. Open `/`. It redirects to `?tenant=del-combined&view=timeline&tf=1q&scope=all`, the **Timeline**, a dense page with 16 event chips,
   sprint chips and 131 PR rows. The nav labels the first item "React → Overview", but the app does not land there (`s00-root.png`).
2. Click **Overview**. The answer starts here: *Needs attention → "waiting 1w+ DEL-167 #53 delivery-api 7 d 0 h"* (`q1-01-overview.png`).
3. Click the **DEL-167** link and land on `#ticket/DEL-167` (`q1-02-ticket-DEL-167.png`).
4. Click the PR row **#53**. **Nothing happens.** The rows are not links and have `cursor:auto`. That is a dead end.
5. Go back via **Delivery list → All 8 → row DEL-167 #53** (`q1-04`, `q1-05`). The panel says
   *"Waiting 7 d 0 h on review queue; no other open PR shares ticket DEL-167."*
6. Click the **Timeline** chip in that panel. It does not show the PR. It scopes the whole page to `person AlNg`, the author,
   and loses the selection (`q1-06-timeline-from-row.png`).
7. For `DEL-155` I reached the ticket through Rules → expand rule 1 → link `#ticket/DEL-155` (`q1-08`). The other routes are
   a Timeline story-lane label click (works, tooltip "click to open the ticket detail page") or typing the hash. There is
   no ticket search or jump box.
8. For `DEL-105` I typed the hash and checked how a reopened ticket looks (`q1-09`).

### What the UI said

DEL-167:

* "**DEL-167 took 73.31 h from created to done: most of it went to ready → refined (26.67 h, 36% of the measured cycle),
  and 16% of the measured time was active work.**"
* Header chips: "73.3 h cycle · **9/8 segments** · 2 PRs · 8 points".
* Segments: ready→refined 26.7 h, refined→sprint 7.4, sprint→in progress 16, in progress→PR open 0.6, PR open→first review 0,
  review rounds 11.2, final approval→merge 2.5, merge→verification 1, verification→done 8.
* PR table: `#52 merged 13.6 h` and `#53 open · censored 192 h`.

DEL-155:

* "**DEL-155 took 200 h … most of it went to review rounds (123.55 h, 62%)**."

### Independent cross-check

| Claim | Raw / recomputed | Verdict |
|---|---|---|
| DEL-167 created→done 73.31 bh | `bh(Oct 6 11:20Z → Oct 19 12:38Z)` = 73.31 | **Right** |
| All 9 DEL-167 segment values | Recomputed from `events` with the Tallinn calendar: 26.67 / 7.37 / 16.0 / 0.6 / 0 / 11.15 / 2.5 / 1.0 / 8.0 | **Right**. "PR open → first review 0 h" looks wrong (14:22Z and 16:09Z are 1 h 47 min apart) but both times are after 17:00 EEST, so 0 business hours is correct. The UI never says the clock is Tallinn. |
| DEL-155 `review rounds` 123.6 h | first review 5 Oct 14:22Z → approval 27 Oct 10:32Z = 123.53 bh | **Arithmetic right, meaning wrong.** PR #30 is open for 30.8 bh and **merged 9 Oct**. PR #31 **opened 20 Oct**. The **60.1 bh between them have no PR in review**, but the segment counts them as "review rounds". Real review time is about 69 h of 123.6 h. The one-line answer should say "second PR started 11 days after the first merged". |
| Segment label `ready → refined` | `/api/insights … bounds: ["created","refined"]`. The real READY→REFINED gap is **0.2 h mean** (e.g. DEL-167: 14:00 ready, 14:20 refined). The 26.7 h is *created→refined*. | **Mislabelled.** It drives rule #1 and every ticket's headline. |
| DEL-167 "is it done?" | `objects`: DEL-167 `done` (closed 19 Oct); PR #53 `open`, 0 human approvals, 0 sentinel approvals, last event 19 Oct. The ticket page headline does not mention #53. | **Misleading by omission.** #53 appears only as a table row marked "open · censored 192 h". Segments are computed only from PR #52, the PR that merged. |
| DEL-105 reopened | `done 26 Aug → ready 27 Aug → … → done 31 Aug`. The UI says "took 108.67 h from created to done" but the same header says "closed 2026-08-31". The DONE marker is 26 Aug. Created→first done is 108.67 bh (matches the headline); created→final close is 130.7 bh (my calc, 20 Aug holiday excluded), +22 bh that the headline omits. | **Wrong / inconsistent.** The reopen is not mentioned anywhere (`grep -i reopen app/ticket.js analysis/*.py` finds nothing). 5 stories are affected. |
| "9/8 segments measured" | `app/main.js:987` hard-codes `/8`; there are 9 segments (the strip above says "9 SEGMENTS") | **Bug** |
| "logged a week late 50% (1)" on DEL-155 | `jira.py:1395` counts `within_week + late`. DEL-155's flagged worklog was 3.2 days late. | **Mislabelled** |
| `7 d 0 h` for #53 (Overview, Delivery list) | 168.3 *business* hours ÷ 24. Waiting since 19 Oct = **29 calendar days / 21 working days**. | **Right under its own clock, misleading to read.** "d" means 24 business hours here. DEL-155 "cycle 8 d 8 h" is five calendar weeks. |

### Verdict — Q1

* **Accuracy: 4/5.** Every number I recomputed from raw events matched. The failures are *interpretation*: the "ready → refined"
  label, "review rounds" swallowing idle gaps between PRs, and a headline that uses only the PR that merged.
* **Helpfulness: 3/5.** The 9-segment bar and the one-sentence answer are the right idea, and for single-PR tickets
  they are good. They *under-explain* the two interesting tickets: DEL-167 (Done with an open PR) and DEL-155 (idle gap between
  PRs). The user has to read the table and the events themselves.
* **Journey: 5–6 clicks to the ticket, then a dead end** (PR rows not clickable). Getting from the ticket to "what is wrong
  with PR #53?" is impossible by clicking.

---

## 3. Q2 — "What's stuck in review, and who's the constraint?"

### Journey (clicks)

1. **Overview** (`q1-01-overview.png`). It shows tiles ("1 BLOCKED · 3 WAITING OVER 3 DAYS · ≈112 d 2 h MEASURED WAIT ·
   **3 OVERLOADED PEOPLE · 5 slow · longest mean wait ≈13 d 5 h**"), a *Needs attention* list, *Where waits accumulate*
   ("author waited on review 56 d 3 h · sentinel waited on review 23 d 17 h · review waited on sentinel 17 d 9 h") and
   *People diagnosis* (BiBi overloaded ≈13 d 5 h, FrMo overloaded ≈1 d 9 h, AlNg/ErLi/BoFe/DaKo slow).
2. **Blocking map** (role/queue level; `q2-01-blocking-map.png`). Three nodes: review 1917 h, sentinel 600 h, author 173 h.
3. Click the **sentinel** node. It jumps to **Queue**, scope `role:sentinel` (`q2-02-queue-sentinel.png`): "1.2 wip · ≈5.7 h mean ·
   105 PRs · peak 3 · ≈ overloaded".
4. Set scope back to everything, set **Queue → level: people** (`q2-03-queue-people.png`):
   BiBi 4.4 wip / 317.6 h mean (overloaded), **FrMo 8.4 wip / 33.0 h mean (overloaded)**, humans 0.5–0.8 wip, ~8–10 h (slow), review (role) overloaded.
5. Click the FrMo row. The scope becomes `person:FrMo`.
6. **Delivery list** with scope FrMo. **"0 open items in view … Nothing in this subset for scope FrMo"** (`q2-04-delivery-FrMo-empty.png`).
   With scope `role:sentinel` the list shows the whole team (8 items) while the banner says "whole team". With scope BoFe it
   shows PRs *authored* by BoFe.
7. Timeline PR lane → click the 90-day-old `DEL-100 #2` bar. The drawer says "Closed: its wait is history, not a live
   bottleneck. cycled 90 d 9 h (measured) … in review 89 d 14 h" (`q2-05-zombie-pr-drawer.png`).

### What the UI answered

* Stuck now: DEL-183 #66 (blocked, waiting on BoFe 17 h), DEL-167 #53 (7 d 0 h), DEL-168 #55 (6 d 8 h), DEL-176 #60 (5 d 11 h),
  plus four fresh ones.
* Constraint: review queue, and **people: BiBi and FrMo overloaded**.

### Independent cross-check (store + the repo's own `analysis.ballcourt` replay)

| Claim | Raw | Verdict |
|---|---|---|
| Stuck PR ages (#53 168 bh, #55 152 bh, #60 131.6 bh, #66 17.2 bh) | My `bh()`: 168.3 / 152.0 / 131.6 / 17.2 | **Right** (#60 shows 5 d 11 h; 131.6 h is 5 d 11.6 h) |
| `#66` blocked | Open mandatory comment by CaOs, no reply | **Right** |
| `#55` and `#60` "waiting on **review queue**" | The replay holds `ball_people=('FrMo',)` for both: one human approval each (ErLi 21 Oct, BoFe 26 Oct) and nothing from Frank since | **Right wait, wrong/blurry owner.** The user is never told the owner is the sentinel. |
| Blocking-map totals | Edge sums match. The Delivery-list tails (168 / 152 / 132 h) are `unobserved` per the module docstring and are **excluded from the blocking graph**. | The map therefore *omits the three most actionable waits*. |
| **BiBi "overloaded, ≈13 d 5 h"** | `BiBi` is a **bot** that declined 7 PRs on 16 Nov. The replay charges it 2223 person-hours across those 7 PRs because it is the party that finally "responded". | **Wrong.** An automation janitor is ranked the #1 constraint. It also sets the Overview headline "longest mean wait ≈13 d 5 h". |
| Zombie share | 7 declined-by-bot PRs = **2305 of 5314 measured wait-hours (43%)**. They also give FrMo 2079 of his 4318 person-hours. | **Distorts everything downstream.** The "in review 807 d" legend in the Timeline is inflated by the same 7 PRs. |
| "FrMo overloaded" | Store: median **6.3 bh** from last human approval to Frank's approval, p90 12.9; median 21.6 bh from PR open; Frank is always last (107/107). The Policy-lab sim says sentinel ≈55% busy, mean wait 2 h 45. `sentinel_queue` metric: median 2.1 h vs 24 h target, so Rules raises **no** sentinel rule. | **Overstated, and the views contradict each other.** Frank is a *tail* problem (2 PRs, 4 weeks), not a throughput constraint. |
| Overview "3 OVERLOADED PEOPLE · 5 slow" | The list shows 2 overloaded and 4 slow. In the Queue view the third "overloaded" is the synthetic **`review` role**, and CaOs (slow) is missing from the list. | **Count mismatch** (the tile counts a queue as a person). |
| Role vs person sentinel numbers | Role view: 1.2 WIP / 5.7 h. Person view: FrMo 8.4 WIP / 33 h. The person view charges Frank from the moment a PR opens, concurrent with the human reviewers. | **Two numbers for the same actor, unexplained.** |

### Verdict — Q2

* **Accuracy: 3/5.** Real things were found: the three genuinely stuck PRs, correctly aged, and the review queue as the main
  wait bucket. Two headline claims are wrong or inflated: the bot "overloaded" and the FrMo number. The model's own docstring
  excludes the tails, so the map omits what matters now.
* **Helpfulness: 2.5/5.** The Overview is the right front door. But "who holds #55 and #60?" cannot be answered by clicking.
  The Delivery list says "review queue", the person scope gives 0 items, and the Blocking map ignores the tails.
* **What the lead should conclude** (from the data): Frank is not a chronic constraint. About 3 PRs have fallen off everyone's
  radar for 3–5 weeks, and 7 more rotted for 1–3 months until a bot cleaned them up. A *stale-PR* rule is missing.

---

## 4. Q3 — "What should we change first? Did S2 deliver?"

### Journey (clicks)

1. **Rules** (`q3-01-rules.png`). 7 rules, 0 critical, "≈3.8k h at stake". Expand rule 1 → "85 items" list with links (`q3-02`).
   **Rules → people** toggle: "No person-level rule crosses its threshold in this window" (`q3-07`).
2. **Policy lab** (`q3-03-policy-lab.png`). Baseline validated ≈1 d 1 h predicted vs 1 d 0 h measured. I ran the presets:

| Scenario | median cycle | p90 | note |
|---|---|---|---|
| add a sentinel (2) | −3.0 % | −4.8 % | UI: "smaller than the run-to-run spread … read it as 'no clear effect'" (`q3-04`) |
| review SLA 4 h | −9.3 % | −13.7 % | throughput −6.4 % |
| WIP limit 1 | −11.4 % | −16.0 % | **lead time +257.6 % (≈3 d 17 h)** |
| review first | −1.9 % | −6.4 % | "no clear effect" |
| cap PRs at 3 commits | **+289 %** | +311 % | flow efficiency −60 %, **sentinel ≈122 % busy** |
| 2 sentinels + cap 3 commits (by hand) | +29 % | +57.5 % | reviewers 49–62 % busy, peak queue 18 (`q3-05`) |

3. **Timeline → click the S2 sprint chip** (`q3-06-sprint-S2-lens.png`).

### What the UI said

* Rule 1: "**The team spends 30% of ticket cycle time in the ready → refined segment** … The largest wait is **100.1 d per story** …"
* Rules 2–7: rework loops (p90 4 rounds, 38 PRs over 2), comment tax (15 % rejected/walked-back), flow efficiency 20 %,
  conformance (16 PRs past 3 rounds), survival (3 open PRs past target), worklog lateness 1.5 d.
* Sprint chips: "S1 30 stories · 112 pts · **26 done** · S2 … **27 done** · S3 28 done", "**0 carried over**".

### Independent cross-check

| Claim | Raw | Verdict |
|---|---|---|
| Rule 1 "30 %, ≈2.4 k h, 85 stories" | `analysis` totals: ready_refined 2402 h / 8046 h = 29.9 %. My recompute of created→pointed = 28.6 bh per story (2714 h on 95 stories). | **Right number, wrong words.** "100.1 d per story" is 2402 h ÷ 24 = the *total* in 24-h days. Per story it is ≈ 28 h. The label is created→refined (see Q1). Advice "turns into a carry-over" is moot: carry-over metric is 0. |
| Review round counts (38 over 2, p90 4, excess 56, **16 past 3 rounds**) | The store's own `pr_merged.review_rounds`: 30 merged PRs over 2, **max 4, only 1 PR over 3**. Rules derive rounds as pushes+unapprovals (19 of 100 merged PRs get 1–2 more rounds than the store's attribute). | **Definition drift, undisclosed.** Under the store's definition the "16 PRs" headline would be 1. |
| "≈3.8 k h at stake" | Sum of the cards: 1201 + 727 + 702 + 694 + 234 + 280 = 3838 h. Rule 5 (excess over 3 rounds, 18 × 12.98 h) is a subset of rule 2 (excess over 2, 56 × 12.98 h). Rules 2–4 price the same review rounds. | **Not additive: double counted.** |
| Rule 3 comment tax (367 threads, 54 wasted: 29 rejected + 25 walked back) | `THREAD` objects in the window: 28 rejected + 24 walked back (all-time 32 + 25). | **Right** within window effects |
| Rule 7 worklogs (mean 1.47 d, 169 logs) | All 199 worklogs: mean 1.52 d | **Right** |
| Rule 6 survival "3 open PRs past target" | #53, #55, #60 | **Right**, and the best rule on the page. But target "3 d 0 h" is 72 *business* hours = 9 working days, and "oldest open age ≈ 8 d 0 h" is a 29-day-old PR. |
| Rule 4 "smaller PRs … move flow efficiency" vs Policy lab "cap PRs at 3 commits" | Lab: median cycle +289 %, flow efficiency −60 %, sentinel 122 % busy | **The two views contradict each other** and nothing reconciles them. |
| Policy-lab `cap PRs` headline | Throughput "+117.5 %" and cycle compare *many small PRs* with *few big PRs* | **Apples to oranges.** Throughput counts PRs, not delivered work. |
| Policy-lab `WIP limit 1` | cycle −11.4 % shown in the headline row; lead time +257.6 % is two rows down, with no verdict line | **Easy to misread as a win.** |
| Sprint chips "S1 26 done / S2 27 done" | Store: stories done **by sprint end**: S1 18/30 (70/112 pts = 63 %), **S2 15/30 (52/118 pts = 44 %)**. Finished *after* the sprint ended: S1 8, S2 12 (e.g. DEL-155 on 29 Oct, 22 days late). Never finished: S1 4, S2 3. | **Misleading.** "done" means "done today". The 20 late stories are de-facto carry-over without a sprint move. |
| "0 carried over" | `carry_over` = "more than one distinct `sprint_added` sprint id". No story was re-added. | **Technically right, blind to the real carry-over.** |
| S2 lens | The timeframe changes to 11 Sep → 7 Oct, but the story lanes still list all 100 stories (S1's first, with no events in view). The URL stays `tf=1q`. | Lens is a time window, not a sprint filter, and is not shareable. |

### Verdict — Q3

* **Accuracy: 3.5/5.** Most evidence counts are correct under their own definitions. The text/definition problems are real: a wrong
  per-story claim, a mislabelled segment, rounds defined differently from the store, and non-additive stakes.
* **Helpfulness: 3.5/5.** Rules plus the lab *are* actionable. The honest "no clear effect" and "predictions, not measurements"
  banners deserve credit, and the lab shows that adding a sentinel alone does little while smaller PRs would saturate the
  reviewers. But the product never says *"fix the stale-PR / sentinel tail first"*, the one thing the data points to.
  There is no rule for stale/zombie PRs, none for sentinel tails, and nothing about sprint on-time delivery.
* **Journey: smooth** (Rules → expand → link to ticket; presets + Run). The expanded lists are sorted by id rather than by
  impact: the worst offender DEL-129 (42.8 h) is row 15 of 85, even though the header says "affected first".

---

## 5. Bugs, papercuts and missing features (ranked)

### A. Wrong or misleading answers (fix first)

1. **A bot is ranked the #1 constraint.** *Overview → People diagnosis*, *Queue → people* and the Overview headline "longest mean
   wait ≈13 d 5 h". `BiBi` (`actor_kind: bot`) is charged 2223 person-hours for 7 PRs it merely declined. Repro:
   `/api/queues?level=person` → `BiBi overloaded, mean_wait 317.6 h`. Also hide or flag bots in the scope select
   (`person BiBi`, `person system`, `queue CI`, `queue calendar` with no data).
2. **Zombie PRs inflate every wait total.** 7 PRs open 37–90 days = 43 % of all measured wait-hours; FrMo's person-hours roughly
   double. The default views never separate "idle until a bot closed it" from real review wait.
3. **`ready → refined` is really *created → refined*.** Label in `analysis/jira.py:82/103`, `ticket.js:27` (called "backlog"
   there). The real ready→refined gap is ~0.2 h. It makes rule #1 and the ticket headline read wrongly.
4. **"Review rounds" absorbs idle time between two PRs.** *Ticket DEL-155*: 123.6 h shown, ≈60 h of it has no PR open. The headline
   says "most of it went to review rounds (62 %)".
5. **Ticket headline ignores the open PR and the ticket's Done state.** *Ticket DEL-167*: Done + PR #53 open 192 h appears only
   as a table row. The Overview's "waiting 1w+" and the ticket page never connect.
6. **Rule 1 text: "100.1 d per story".** It is the 2.4 k-hour total ÷ 24, not per story (≈ 28 h/story).
7. **Sprint chips count "done today", not "done by sprint end"; "0 carried over" hides it.** S2 delivered 15/30 stories (44 % of
   points) by its end, but the chip reads "27 done". 20 stories finished after S1/S2 closed.
8. **Cross-view contradictions.** Overview/Queue: FrMo overloaded (33 h mean, 8.4 WIP). Policy lab: sentinel 55 % busy, 2 h 45 wait.
   Rules: `sentinel_queue` median 2.1 h, no rule. Rules #4 says "smaller PRs", Policy lab says cap-3 is +289 % worse.
9. **Delivery list scope is inconsistent and dead-ends.** `scope=person:FrMo` → "0 open items"; `scope=role:sentinel` → not applied
   ("whole team"); `scope=person:BoFe` → PRs *authored by* BoFe (the Queue view uses the same control for "waiting on BoFe").
   "Waiting on" always says "review queue", so the sentinel is never named even when the replay says
   `ball_people=('FrMo',)` (#55, #60).
10. **Overview "3 OVERLOADED PEOPLE · 5 slow"** counts the synthetic `review` role as a person and lists only 6 of 8 rows (CaOs missing).

### B. Papercuts

11. **One unit letter, three clocks.** "d" = 24 *business* hours in Overview/Delivery/Rules ("7 d 0 h" = 29 calendar days; "cycle
    < 3 d" = 9 working days). The PR drawer uses *wall-clock* (`DEL-100 #2: 90 d 9 h`) even on a page that says "business hours".
    Timestamps are UTC while business hours are Tallinn, so "PR open → first review 0 h" next to timestamps 1 h 47 apart looks like
    a bug.
12. **Ticket → PR is a dead end.** The PR rows on `#ticket/…` are not clickable (`ticket.js:177` has no handler). The Overview row only links
    the ticket key, not "#53".
13. **Reopens are invisible.** Five stories were reopened (done→ready). DEL-105 shows a DONE marker on 26 Aug, a "closed 31 Aug" header, and
    a "took 108.67 h" headline.
14. **`9/8 segments measured`** (`app/main.js:987` hard-codes `/8`; there are 9).
15. **Rules headline "≈3.8 k h at stake" is not additive** (rule 5 ⊂ rule 2; rules 2–4 overlap).
16. **Rounds definition drift**: rules count pushes+unapprovals ("16 PRs past 3 rounds"), the store's `review_rounds` has 1.
17. **Rules drill-downs ("affected first") are sorted by key, not by impact**; the biggest item is row 15 of 85.
18. **`Sources`: "PRs 131 kept of 0 seen"** (`prs_seen` is 0 for this tenant). "Snapshot from 9 h ago" vs "(10 h ago)" on the same page.
19. **A PR drawer stays open when you change views** (I opened `DEL-100 #2` on the Timeline, then it covered Sources).
20. **Delivery-list "Timeline" chip scopes to the author**, not to the PR/ticket, and drops the selection.
21. **Sprint lens** doesn't filter story lanes, doesn't highlight the chosen sprint, and the custom range isn't in the URL.
22. **"logged a week late"** includes `within_week` (2–7 d late).
23. **Policy lab**: the 5 selects have no `name`/`id`/label (hard to test, poor a11y); the "WHO IS BUSY" table lists `sentinel:1` and a
    0 %-busy `FrMo` as two rows; the cap-PR throughput compares different units; WIP-limit lead-time blow-up has no verdict line.
24. **Landing page**: the app opens on the densest view (Timeline) though the nav's first item is "Overview — what needs attention?".
25. **Overview "Needs attention" shows 4 of 8 open items** with no "+4 more" hint beyond the small link.

### C. Missing features the journeys wanted

* A ticket search / "go to ticket" box (reaching DEL-155 took a Rules drill-down).
* A **stale / zombie PR** rule and Delivery-list filter ("open > N business days", "ticket Done but PR open", "waiting only on <person>").
* A sentinel-tail rule or card (the data has 2 PRs waiting only on Frank for 4 weeks).
* Name the owner in "waiting on" (the replay already has it); clickable PR rows everywhere.
* Treat bots separately (exclude from people diagnosis; optionally count bot-closed PRs as "abandoned", not "waits").
* Reopen / QA-bounce visibility on the ticket page (25 of 100 stories have backward moves).
* Sprint "done by end vs late vs never" breakdown; rule for late completion.
* A "combine scenarios" explanation in the Policy lab (the lab says cap-3 alone saturates Frank; the natural next step is
  sentinel + SLA, and you have to compose it by hand).

---

## 6. What the journey feels like

* **Onboarding to first answer: fast.** The Overview answers "what needs attention?" in one screen, in plain language, and the ticket
  page answers "why did this take so long" with a sentence plus a coloured bar. The page subtitles are questions, which helps. The
  honesty features stand out: "predictions, not measurements", "no clear effect", "a segment needs both boundaries; an absent one is
  named, never estimated", and the footer that always names the clock.
* **Where it shines.** Segment arithmetic and PR waits are correct. I could not break the ticket decomposition (all numbers matched
  once I used the Tallinn calendar and holidays). The Policy lab is calibrated (+0.9 % vs measured) and says when a result is noise.
  Blocking-map node → Queue is a one-click drill that works.
* **Where it grinds.** Cross-view *consistency*. The same actor is "overloaded", "55 % busy" and "no rule" in three places. The same
  PR is "7 d" and "90 d" depending on the page. The scope control means different things per view and silently does nothing in
  one (`role:sentinel` on the Delivery list). The clickable thread breaks at the most useful junction (ticket → PR). And the loudest
  numbers (BiBi, FrMo) are artefacts of a bot-closed batch of stale PRs, the one thing the product doesn't call out.
* **Rough count to answer each question:** Q1 ≈ 6 clicks to the ticket, then 2 manual cross-checks. Q2 ≈ 8 clicks and a dead end
  (Frank's items). Q3 ≈ 4 clicks to rules and 6 runs in the lab, with no synthesis.

---

## 7. Screenshot index (`/tmp/opencode/explore/shots/`)

| File | What |
|---|---|
| `s00-root.png` | root URL = Timeline landing |
| `q0-tenant-menu.png` | tenant switcher (one tenant) |
| `q1-01-overview.png` | Overview, "Needs attention / Where waits accumulate / People diagnosis" |
| `q1-02-ticket-DEL-167.png` | ticket DEL-167 page (9 segments, "9/8", PR #53 open·censored) |
| `q1-04-delivery-list.png`, `q1-05-delivery-row-167.png` | Delivery list + panel for #53 |
| `q1-06-timeline-from-row.png` | "Timeline" chip → scope `person AlNg` |
| `q1-07-timeline-stories.png`, `q1-11-objlanes.png`, `q1-10-timeline-click-story.png` | timeline lanes |
| `q1-08-ticket-DEL-155.png` | DEL-155 ticket ("review rounds 62 %") |
| `q1-09-ticket-DEL-105-reopen.png` | DEL-105 (reopened, "closed 31 Aug" vs 26 Aug marker) |
| `q2-01-blocking-map.png` | Blocking map (role/queue) |
| `q2-02-queue-sentinel.png`, `q2-03-queue-people.png` | Queue, sentinel scope and people level (BiBi, FrMo overloaded) |
| `q2-04-delivery-FrMo-empty.png` | Delivery list scope FrMo = 0 items |
| `q2-05-zombie-pr-drawer.png` | PR drawer for DEL-100 #2 (90 d, wall-clock) |
| `q2-06-sources-stale-drawer.png` | Sources page with stale drawer + "131 kept of 0 seen" |
| `q3-01-rules.png`, `q3-02-rule1-drill.png`, `q3-07-rules-people.png` | Rules, drill-down and people toggle |
| `q3-03-policy-lab.png`, `q3-04-sim-add-sentinel.png`, `q3-05-sim-2sentinels-cap3.png` | Policy lab |
| `q3-06-sprint-S2-lens.png` | Sprint-lens (time range only) |

# QA comparison — same three questions, before (`QA-exploratory`) vs after (`QA-fresh`)

Both passes asked the identical three questions of the `del-combined` team. `QA-fresh.md` was produced
with **no knowledge of the prior findings**. This maps the first pass's 25 findings to the second.

## Verdict

**Substantially better.** Of the 25 original findings, ~20 are fixed; the three questions now get
markedly more truthful answers (sprint delivery is exact, the constraint is Frank-by-wait not
Frank-by-capacity, and the "Done but PR open" state is surfaced). The fixes also **introduced a small
set of new issues** — mostly in the new code paths (abandoned-PR detection, the new time labels, the
policy-lab verdict text, carry-over counts) — and exposed a couple that were always there but hidden.

## Fixed (no longer reproduced in the fresh pass)

- **#1 bot as a person constraint** — bots are out of person aggregates (but see B1 for the residue).
- **#3 `ready → refined` mislabel** → `created → ready` (now reads "refinement").
- **#5 headline ignores the open PR** → "Done in Jira, but PR #53 is still open (right-censored)".
- **#6 "100.1 d per story"** → per-story statistic.
- **#7 sprint "done today"** → done-by-end (S2 now reads 15/30 by close, 12 late, 3 never — exact).
- **#9 scope dead-ends** (`person:FrMo` → 0 items) · **#10 "3 overloaded" role-as-person** · **#11 "d" = 24h**
  → 8h working day · **#12 ticket→PR dead end** · **#13 reopens invisible** · **#14 "9/8"** · **#15 at-stake
  double-count** · **#16 rounds drift** · **#17 drill-down key-sorted** · **#18 "131 kept of 0 seen"** ·
  **#19 drawer stays open** · **#20 timeline-chip scopes to author** · **#21 sprint lens** · **#22 "logged a
  week late"** · **#24 landing on Timeline** · **#25 "+N more"**.

## Gaps left (old problems, only partially fixed)

- **#2 zombie/stale PRs still inflate totals** → **B1**. Bot-*closed* PRs are meant to be `abandoned`, but
  `abandoned_hours` is 0 and the four bot-declined PRs still make up 1,513 of 1,687 author-held hours —
  so "review waited on author 207 d" is mostly dead PRs, and AlNg/DaKo still look overloaded.
- **#4 "review rounds" absorbs the no-PR gap** → **B5**. The segment data now carries the gap, but the
  DEL-155 *headline* still says "62 % review rounds" and hides the 60 h with no PR open.
- **#8 cross-view contradictions** → partly resolved, but **B3/B8** show *new* inconsistencies (below).

## New issues introduced or newly uncovered

- **B1** abandoned-PR detection not firing (see above).
- **B2** the PR drawer now *covers* the delivery inspector that holds the best facts ("Done in Jira but PR
  open", "open 24 business days").
- **B3** the same three PRs are labelled "over 9 d" / "1w+" / "over 3 days" / "1 of them over a week" —
  the 8h-day change wasn't applied consistently across the overview tile, list and footer.
- **B4** the top "what to change first" rule flips with the timeframe (refinement at 1q, review rounds at
  All, sprint delivery at 4w/1w); top segments are within ~1 point and the "review rounds" segment still
  includes 22 % no-PR time.
- **B5** unformatted `7.51875 business days` + a missing sentence separator on the DEL-155 answer.
- **B6** #53 shows "1/2 reviewers pending, waiting on CaOs" — but neither reviewer has approved.
- **B7** policy-lab verdict text is wrong: "PR count 1.1 % higher" next to Δ −1.1 %, and a duplicated
  "but delivered work 8.4 % less".
- **B8** carry-over counts disagree between the Timeline strip and Rules (27/20/90 vs 24/18/75), and the
  sprint rule's drill-down lists S1 *and* S2 stories under an S2 headline.

## Missing features (still absent, surfaced by both passes)

- No view comparing Frank's (sentinel) response time against peers' (the constraint answer is inferable
  but not shown).
- No per-story benchmark on the ticket page ("73rd of 87 by speed").
- Policy lab still lacks refinement / review-round-cap knobs.
- No "Done in Jira, but PR open" list with ticket keys (DEL-183 was closed 9 days before its PR opened).

## Net

The product is meaningfully more truthful and more useful: the headline answers are right where they were
wrong, the journey is shorter (search, clickable rows, landing on Overview), and the sprint answer is
exact. The remaining work is a *second-order* pass: make the abandoned/stale signal actually exclude the
bot-closed PRs, reconcile the time labels and carry-over counts across views, fix the policy-lab verdict
text and the #53 reviewer-pending count, and add the three missing comparison features. Each is concrete
and small; none is the structural problem the first pass found.

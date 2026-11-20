# 47 — Complete the final-QA backlog + navigation

prd/46 covers the top correctness items (out-of-order segments, one sentinel-wait number, Rules internal
consistency, "committed"=at-start, worklog reframe, some layout). This PRD covers **every remaining
finding** from `docs/QA-final.md` plus the navigation gap. Conform to `docs/concepts.md`.

## A. Diagnosis consistency (the "different clocks" residue)

1. **Open-story headlines hide the real wait** — DEL-176's headline says "created → ready 35 %" while 66 %
   of its cycle is unmeasured and PR #60 waits 16 d on FrMo; same for DEL-167. The headline must state
   the unmeasured/censored share and name the open PR's wait.
2. **Misattached lab annotation** — on timeframe `All`, the review-rounds card carries a "+81 %" (a
   *refinement*) note. Attach each lab note to the card it actually measures.
3. **Text inconsistencies** — "median story waits 3.4 d" vs "≈3 d 2 h" (same number); rework "median
   rounds 1" vs "2" (two bases); "hours per round 1 d 6 h" vs "each extra round ≈6 h". One number, one
   basis, one rendering.
4. **Phase "active" is wrong for #53** (waits on CaOs + FrMo, "waiting on author: no"). The phase label
   must match the ball.

## B. Sprint / window honesty

5. **Sprint-length flag** — S3 is a 51-day sprint (S1/S2 24–26 d); flag the outlier.
6. **Timeframe honesty** — `1w` shows critical rules unrelated to the week; default `1q` (90 d) silently
   drops 7 PRs against a 103.6-day dataset while the header says 131. State the window-vs-dataset and
   don't show rules whose cohort is outside the window.

## C. UI papercuts

7. **Drawer honesty** — add "waiting since / waiting 21 d"; make the phase list sum to the cycle.
8. **Context menu** — "Scope to" should offer the *holder* (FrMo), not just the author (ErLi); render at
   the pointer.
9. **Blocking map text view** — add a node/edge table (not canvas-only), and render waits in "d h" like
   every other view, not "1348h".
10. **Scope banner** — "this view is not filtered" is false when `person FrMo` still changes Overview;
    fix the banner to match what the scope actually does.
11. **Copy-as-Markdown** — include the open-PR wait/approval state for a Done-in-Jira ticket.
12. **Wording** — "logs 0.49× less" → a clear phrase; "sentinel waited on ready" → "PRs waited for merge
    after approval"; define "unobserved" inline; define "d" wherever used; stop showing timeline
    lifecycles in "calendar days" under a "business hours" header.

## D. Missing features (larger; can be split)

13. **Repo scope** — add `repo` to Scope/Queue/Blocking/Rules/Lab ("is delivery-api or delivery-web
    slower?").
14. **Trend / time series** — flow efficiency, cycle time, sprint delivery by week (not just a window
    aggregate).
15. **Story-level rework** — a list of reopened / failed-verification / review-bounced stories, and an
    "approved but never merged" list (DEL-161).
16. **Sprint-at-risk / forecast** — for the active sprint, which stories are at risk (Jira-done-unmerged,
    stuck-on-a-person, un-merged).
17. **Worklog-vs-points view + pseudonym legend** — logged hours against points (r ≈ −0.05), and a legend
    naming the sentinel/roles (prd/02-safe).
18. **Review SLA / chase action** — surface the one thing the data supports (nudge #55/#60/#52 today).

## E. Navigation (new — the reported gap)

19. **In-app back + browser back between views.** Today view changes use `history.replaceState` (so the
    URL stays current but history isn't recorded), and only ticket opens `pushState` — so the browser
    Back button exits the app to the previous *site* instead of the previous *view*, and there is no
    in-app back affordance. Fix: record view/tenant/scope/timeframe changes as history entries
    (`pushState`) so Back walks the in-app navigation; add a small in-app Back affordance (top bar) that
    pops the in-app history, and keep the deep-link URL working (a fresh link still lands correctly).
    Preserve the existing "Back returns from a ticket to the page it opened from" behaviour.

## Definition of done

Each item has a regression test (or, for features, a small test) and conforms to `docs/concepts.md`.
Keep 963 + 99 + 231 green. A-E are independent; D can be split if needed.

# 43 — Reconcile the Rules and Policy-lab advice (and close the last gaps)

The verification pass (`docs/QA-verify.md`) confirms the *facts* reconcile across views, but the two
**advice** surfaces contradict each other, and the strongest lever isn't surfaced. Conform to
`docs/concepts.md`.

## 1. Rules ↔ Policy-lab cross-check (the headline)

The Rules view ranks "refine first" / "add a sentinel" / "smaller PRs" with impacts it partly flags as
*assumed*, while the calibrated simulation says refine = **+82 %**, a second sentinel = **no clear
effect**, smaller PRs = **+289 %**, and the real lever is a **review-round cap (−13 %, r = 0.6)** — which
is not a Rules item at all.

**Fix**: make the two surfaces agree, in both directions:
- a rule whose advice the simulation contradicts carries a note: "the policy lab models this at +N %
  cycle — not a measured win" (and its "at stake (assumed)" is clearly an assumption, not a saving);
- the Rules view surfaces the levers the simulation *does* support, including a **review-round-cap**
  rule (which the data backs: r = 0.6, the strongest correlation the QA measured);
- ideally a one-line "the lab's best scenario is X" cross-reference on the Rules page.

## 2. Frank's response clock is gate-aware

The sentinel's response time starts at "added as reviewer", but the sentinel never acts before the last
peer (0/106). **Fix**: the sentinel's response clock (in the M1 response-time view and the "×N slower"
label) starts at *last-peer-answered*, consistent with `sentinel_after_peers`. His true lag is ~6 h, not
~20 h; the real story is the two orphans (#55, #60) he hasn't touched for 16–19 days.

## 3. Timeline chip scopes from the ticket-page drawer

`#ticket/DEL-167` → click PR #53 row → "Timeline" opens the whole-team timeline (the Delivery-list
drawer's same chip scopes correctly). **Fix**: the ticket-page drawer's Timeline chip scopes to the
ticket.

## 4. Overview "longest mean wait" tile counts the synthetic review stream

The "longest mean wait ≈2 d 6 h" under "1 overloaded person" reads the synthetic `review` stream as a
person (`app/tables.js:323`). **Fix**: people tiles count people only.

## 5. One PR, one set of numbers across drawers

#53 shows different commit counts (2 vs 5) and different in-progress/in-review hours between the list
inspector and the drawer; sprint 3 "28 of 30 by end" counts three stories with unmerged PRs (true 25).
**Fix**: one canonical source for each PR fact, and "by end" excludes stories whose PRs never merged.

## Tests

One regression each: the cross-check note on a contradicted rule; the review-round-cap rule fires (r =
0.6, −13 %); the sentinel clock is gate-aware (~6 h not ~20 h); the ticket-page Timeline chip scopes; the
overview tile counts people only; #53 has one commit count; sprint "by end" excludes unmerged PRs.

## Out of scope

Full automated reconciliation (the lab auto-running a scenario per rule) is a later, larger feature; this
PRD adds the cross-check + the missing review-round rule.

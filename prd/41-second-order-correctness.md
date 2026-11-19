# 41 — Second-order correctness (the numbers that are still wrong)

The fresh QA pass (`docs/QA-fresh.md`) found the structural fixes landed, but a set of second-order
correctness bugs remain. Each is small and located. Conform to `docs/concepts.md`.

## B1 — bot-closed PRs still inflate the people diagnosis (`abandoned_hours = 0`)

Four PRs (`delivery-api#2`, `delivery-web#8`, `#18`, `#17`) were declined by the bot `BiBi` on 16 Nov
after 233–488 business hours, and hold **1,513 of 1,687 "author-held" hours** — so "review waited on
author 207 d" is mostly dead PRs, and AlNg/DaKo/BoFe look "overloaded" when they aren't.
`analysis/ballcourt.py`: the `abandoned` flag is only set on intervals ended by a declined event; the
final tail interval (to `closed_ts`) is built without it. **Fix**: mark the tail interval `abandoned`
when the PR was bot-declined, so those hours go to `totals.abandoned_hours` and are excluded from the
person-level ranking (the people diagnosis then shows FrMo as the clear #1, not AlNg/DaKo).

## B3 — four thresholds for the same three items; the footer is wrong

Overview card "3 WAITING OVER 9 D", row pills "waiting 1w+" (tooltip 40 bh = 5 d), list header "3
waiting over 3 days", footer "8 open items, **1** of them waiting over a week" — but #53/#55/#60 are all
> 1 week (168/152/132 h). **Fix**: one consistent threshold set (a week = 40 business hours), and
`counts.waiting_over_week` must be 3, not 1.

## B4 — Rules #1 is unstable and its "at stake" is an assumption dressed as a measurement

The top rule flips with the timeframe (created→ready at 1q, review rounds at All, sprint-delivery at
4w/1w); the top two segments are within ~1 point and the UI doesn't say so; "≈1.2k h at stake" is an
*assumed half* (`impact_basis`) of the largest segment shown as if measured; the "review rounds" segment
is 22 % no-PR-open time. **Fix**: (a) a "statistically tied" note when the top segments are within ~1
point; (b) label the `impact_basis` at-stake as an assumption, not a measurement; (c) surface the
no-PR-open share in the "review rounds" rule.

## B5 — DEL-155 headline mislabels the no-PR gap; broken formatting

"most of it went to review rounds (123.55 h, 62 %)" — but 60.15 h had no PR open. Also renders
"7.51875 business days" and a missing sentence separator. **Fix**: the headline names the gap when it is
material ("…62 % review rounds, 60 h of which had no PR open"), round the day, and join the sentences.

## B6 — #53 "1/2 reviewers pending, waiting on CaOs" is wrong

Neither CaOs nor FrMo ever approved (0/2). **Fix**: a reviewer whose blocker was `walked_back` and who
never approved still counts as pending (status `none`), so #53 is 0/2 pending and `waiting_on` names both
CaOs *and* FrMo.

## B7 — policy-lab verdict text is wrong

"PR count 1.1 % higher" next to Δ −1.1 % (sign inversion), and a duplicated "but delivered work 8.4 %
less". **Fix**: generate the verdict sentence from the actual signed deltas.

## B8 — sprint drill-down scope + count consistency

Rule 04's headline is S2-only but its drill-down lists S1+S2 (27 items); the Timeline strip and Rules
show different carry-over counts (27/20/90 vs 24/18/75). **Fix**: the drill-down matches the headline's
scope, and the carry-over count is one number (or the window is stated).

## P1 / P4 / P5 — small correctness papercuts

- **P1** rework rule: "cap 2" vs the footer "3 rounds max" — reconcile to one target.
- **P4** person-level load charges parallel holders (FrMo 3,044 vs 614 h, 5×): make `solo_hours` the
  primary person-level figure (or add an explicit note linking total vs solo).
- **P5** PR-size line slider off-by-one (steps 1/51/101 → cap reads 151/401): fix the step/rounding so
  round caps (100/150/200) are settable.

## Tests

One regression each: abandoned tail hours excluded; `waiting_over_week = 3`; the tied-at-stake note +
assumption label; the DEL-155 headline + formatting; #53 0/2 pending with both names; policy verdict
sign; sprint drill-down scope + carry-over consistency; rework target; solo-hours primary; slider values.

## Out of scope (next PRD)

The missing *features* (response-time comparison, ticket benchmark, done-but-open list, PR-drawer
completeness, policy knobs, sprint scope-creep) and the remaining presentation papercuts.

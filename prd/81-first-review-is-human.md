# 81 — A first review is a human review remark, not a bot comment

Status: **draft; dispatch.**

## Problem

`time_to_first_review` reports a **median of 1.8 business minutes** and only
**6 PRs over the 24-hour target** — while the honest 85th percentile of a human
review remark is **12 hours**. The number is not measuring people.

Measured on the live store (snapshot 2026-11-22, 2,560 PRs with a first review):

| actor | kind | PRs where it *is* the first review | median |
|---|---|---:|---:|
| **CaGi41** | **bot** | **1,566 (61%)** | 7.0 min |
| LaNo | human | 340 | 7.6 min |
| MaPa | human | 146 | 3.5 min |
| FuSu | human | 96 | 4.2 min |
| **AgJu** | **bot** | **88** | **2.0 min** |
| others | human | 272 | — |

**65% of all PRs have a bot as their "first reviewer".** Both bots emit only
`comment_added`, with `{"actor_kind": "bot", "anchor_class": "general",
"replies": 0, "severity": "suggestion", "thread_state": "open"}` and **no
`comment_kind`** (bots never enter the classifier). `BiBi` and `system` are the
other declared bots.

Two defects compound:

1. **`_first_review` does not exclude bots.** `analysis/jira.py:728` takes the
   first `comment_added`/`pr_approved` by any non-author. It does not look at
   `actor_kind` at all, and `court.bots` (which lists all four) is only applied
   downstream, in `by_reviewer`.
2. **It does not check whether the comment is a review.** Since v0.24 the store
   labels every thread (`comment_kind`: `review` 6,740 · `ci_notice` 2,257 ·
   `admin` 2,100 · `bot` 1,709 · `bot_request` 112 · `ci_finding` 20). A
   rebase note, an admin remark or a CI notice by a human counts as a "review"
   today; only 285 of 906 human first-reviews are labelled `review`.

Business-hours pricing finishes the illusion: a bot comment lands minutes after
open, often outside 09–17, so 6.6 wall-clock minutes become 0.03 business hours.

**This is measurement nonsense, not a tuning problem**: the metric's headline
(6 PRs over target) is contradicted by its own drill-down rows.

## Rule

A **first review** is the earliest event that is all of:

1. **by a human** — the actor is not in `court.bots` (i.e. `actor_kind != "bot"`;
   the store's own declaration, not a name list);
2. **not the author** (unchanged);
3. **a review remark** — either a `pr_approved` (§no thread), or a
   `comment_added` whose linked THREAD has `comment_kind == "review"`.

Excluded, explicitly:

- any bot actor on any activity;
- thread kinds `admin`, `bot_request`, `ci_notice`, `ci_finding`, `bot`;
- a `comment_added` with **no resolvable thread label** — it is *first
  anything*, not a first review. The metric reports how many PRs that left
  without a review (`unlabeled_skipped`) instead of silently accepting them.

**Deliberately not built: "first feedback".** A metric that starts at a CI
comment measures the CI bot's speed, which nobody here controls or cares about.
One number, correctly defined.

## Consequences (state them, do not hide them)

- `time_to_first_review.median_hours`, `p90` and `over_target` move to a human
  scale (about 0.19 h median / 12 h p90 expected on the live store).
- The payload gains `bots_skipped` (PRs whose first non-author event was a bot
  but a later human review exists), `unlabeled_skipped`, and `missing` keeps its
  meaning ("no review observed"), which will now be **larger and honest**.
- **Story segmentation shifts.** `pr_open_first_review` and `review_rounds` are
  bounded by `first_review` (`analysis/jira.py` `_pr_boundaries`). With bots
  removed the "in review (first)" segment grows and the churn segment shrinks —
  that is the correct split (waiting for a *human* vs rework after one). The
  review-round count itself (`ballcourt.review_round_events`) only counts
  push-induced unapprovals/rebase-after-approval, so it is **unaffected in
  definition** but will be recomputed against the new boundary.
- Any previously reported review number (the epic review, the 5.4-vs-2.26 round
  comparison) must be re-measured after this change; the old figures are retired.

## Implementation

Everything is analysis-side: **no store migration, no API shape change beyond
the metric payload, no ingest change.**

1. **`analysis/ballcourt.py` `load()`** — the event loader already maps a
   comment event to its THREAD for `thread_outcome` (`THREAD_OUTCOME_SQL` +
   `THREAD_LINK_SQL`). Read `comment_kind` from the *same* THREAD attribute rows
   and put it on the event dict (`event["comment_kind"]`), exactly as
   `thread_outcome` is put there. Absent column / absent label ⇒ `None`.
2. **`analysis/jira.py` `_first_review(pr_state, bots)`** — take the bot set
   (from the court, not a literal) and apply §Rule. Return the event (not just
   the timestamp) or `None`; keep the public `first_review()` wrapper in step.
   Add a small helper so `_pr_boundaries` and `rules.py` agree on one definition.
3. **`analysis/rules.py` `time_to_first_review`** — count skipped bots and
   unlabeled comments, keep `by_reviewer` (already human-only), update the
   docstring and the `basis`/`note` text so the UI says "first **human review
   remark**; bots, admin and CI comments excluded", and expose
   `bots_skipped`/`unlabeled_skipped`.
4. **UI copy** — wherever the metric's note/legend is rendered (`rules.js`), say
   "first human review remark" rather than "first reviewer action". No layout
   change.

## Tests

In `tests/test_jira_analysis.py` (the analysis unit level) and
`tests/test_rules.py` (the metric level), on fixtures:

- a PR whose only non-author comment is a **bot** → `first_review is None`;
  `missing_review` true; `bots_skipped` counts it.
- bot first, human review later → the **human's** timestamp is the boundary.
- author comment → never.
- `comment_kind=admin` / `bot_request` / `ci_notice` comment, then a `review`
  one → the `review` one is the boundary.
- a human `pr_approved` with no thread → the boundary (approvals are reviews).
- a `comment_added` with no thread label → skipped and reported as `unlabeled`.
- segmentation: `pr_open_first_review` is `None`, not `0`, when only bots
  commented, and `review_rounds` starts after the human review.
- the metric payload carries `bots_skipped`, `unlabeled_skipped`, and a
  `by_reviewer` with no bot in it.

## Acceptance

- On the live store after a restart: **no PR's first review is by an actor in
  `court.bots`** (assert over every row), `n` drops from 2,560, `median_hours`
  rises to a human scale, and `over_target` stops contradicting the drill-down.
- `bots_skipped` ≈ 1,654 and `unlabeled_skipped` is reported (expect ≤ 617) —
  both visible in the payload, so the exclusion is auditable rather than a
  silent filter.
- `tests/`, `app/test` and `scrape/tests` green; no store change; `--verify`
  unchanged.

## Open question (answer only if it bites)

If `unlabeled_skipped` turns out to be most of the missing PRs, the fix is to
**resolve those threads** (deleted comments have no THREAD object), not to
loosen the rule. The rule stays "a human review remark or a human approval".

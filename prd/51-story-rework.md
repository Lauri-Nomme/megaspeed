# 51 — Story-level rework view

## User journey
A lead asks "how much rework is hiding in the stories, and where?". The data has 5 reopened-after-Done
stories, 4 failed-verification bounces, 21 review bounces, and an "approved but never merged" orphan
(DEL-161). The UI shows the PR-level rework rule and per-ticket reopen flags, but there is no story-level
aggregate — so "which Done stories came back, and what did it cost" can't be answered.

## Main design
A "Rework" panel (on Overview or Rules) that lists the story-level rework signals: reopened (done→ready),
failed verification (in_verification→in_progress), review bounces (in_review→in_progress), and
"approved but never merged" (both reviewers approved, PR never merged). Each row is a `#ticket/KEY` link
with the count/hours. One backend helper over the status history + PR state (reuse the stage events and
`review_rounds`), surfaced in `/api/insights` or a new `/api/rework`.

## Definition of done
The rework panel lists reopened/failed-verification/bounced/unmerged-orphan stories with keys and hours,
matching a manual recompute (5/4/21/DEL-161 on del-combined). Covered by a backend test + a node test.

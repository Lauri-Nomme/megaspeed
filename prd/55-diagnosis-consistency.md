# 55 — Diagnosis consistency & honesty fixes

The remaining "different clocks" correctness fixes from `docs/QA-final.md` (group A + B of the old
prd/47). Each is small; together they make the diagnosis layer agree with itself.

## User journey
A lead reads the Overview, Rules and ticket pages and expects one number per concept. Instead they see:
DEL-176's headline says "created → ready 35 %" while 66 % of its cycle is unmeasured and PR #60 waits
16 d on FrMo; a review-rounds card carries a "+81 %" note that actually belongs to refinement; "median
story waits 3.4 d" next to "≈3 d 2 h"; "median rounds 1" vs "2"; #53 is labelled "active" while it waits
on two reviewers.

## Main design (one fix each)

1. **Open-story headline honesty** — the ticket headline states the unmeasured/censored share and names
   the open PR's wait, never just the measured segment ("66 % unmeasured · PR #60 waiting 16 d on FrMo").
2. **Correct lab-note attribution** — attach each lab note to the metric it actually models (the +81 %
   refinement note must not ride a review-rounds card).
3. **One number, one basis** — reconcile the text pairs to a single rendering ("median story waits
   3.3 d"; rework rounds on one basis with the basis stated; per-round hours on the same basis as
   "each extra round adds ≈6 h").
4. **Correct phase label** — a PR waiting on reviewers is never `active`; the phase derives from the
   ball (prd/40 status vector).
5. **Sprint-length flag** — mark a sprint whose length is an outlier (S3 = 51 d vs S1/S2 24–26 d).
6. **Timeframe honesty** — `1w` doesn't list rules whose cohort is outside the window; the default
   `1q` states the window-vs-dataset and the header never claims a PR count that the window dropped.

## Definition of done
Each fix has a regression test (the DEL-176 headline, the note attribution, the number pairs, the #53
phase, the sprint flag, the timeframe cohort). Conform to `docs/concepts.md`.

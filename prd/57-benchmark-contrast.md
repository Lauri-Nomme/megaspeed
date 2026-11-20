# 57 — Benchmark contrast: every specific number next to its cohort median

The product is "accurate but not decisive" (every QA pass): it reports a number's *value* but not its
*meaning*. A story "in review 123 h" tells you nothing until you know the median is 30 h — then it's
clearly an outlier, not a systemic problem. This PRD adds that contrast as a first-class layer.

## The concept (add to `docs/concepts.md` §6)

**Benchmark contrast** — any per-item duration shown in the UI is rendered with its cohort median and a
multiple, so the reader sees "is this normal, and by how much". Form: `123 h (median 30 h · 4.1×)`, with
the multiple computed from the same cohort and calendar the metric already uses. It answers "is this a
one-off or the norm" — the decisive half the current numbers lack.

## User journeys

1. A lead opens `#ticket/DEL-155` and reads "review rounds 123 h". With the contrast it reads
   "review rounds 123 h — **4.1× the 30 h median**", so they know it's an outlier, not "the team's
   reviews are slow". Then the advice (chase the orphan, not "fix review") is obvious.
2. A lead reads the PR drawer "in review 2 d 3 h" and sees "median 1 d 4 h" — a normal PR, so they stop
   worrying about it.
3. A lead reads the delivery list "waiting on FrMo 16 d" and sees "median sentinel wait 6 h" — the orphan
   is instantly visible against the gate's norm.

## Main design

1. **Server-side medians**: for each per-item duration the UI already shows (the 8/9 segment hours, the
   cycle, the PR drawer phases, the delivery-list wait, the response times), compute a cohort median on
   the same basis (completed stories for cycle/segments, merged PRs for review phases, the window cohort
   for waits) and attach `median` + `multiple` to the payload. One helper, one definition, reused.
2. **UI rendering**: render the contrast as an inline "median · N×" (subtle, tertiary) on the ticket
   segments, the PR drawer, and the delivery list — not on aggregate totals (which are already cohort
   figures). A per-view toggle turns the contrast on/off so it never clutters.
3. **Honesty**: the multiple is always labelled with its basis ("vs median of N completed stories");
   when N is too small (< 10) the contrast is suppressed rather than shown on a meaningless sample.

## Definition of done

The ticket segments, PR drawer phases, and delivery-list wait each show a cohort median + multiple that
matches a manual recompute; a small-N cohort suppresses the contrast; the toggle works; covered by
backend (median/multiple per metric) + node (rendering + suppress) tests.

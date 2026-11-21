# 37 — Sprint, rules & policy-lab consistency

The planning and "what to change" surfaces give technically-correct numbers under inconsistent or
unstated definitions. Conform to `docs/concepts.md` §6.

## Findings solved (docs/QA-exploratory.md)

- **#7** sprint chips count "done today", not "done by sprint end" (S2 reads "27 done", really 15/30 =
  44 % of points by its close); "0 carried over" hides the 20 stories finished late.
- **#15** "≈3.8 k h at stake" is not additive (rule 5 ⊂ rule 2; rules 2–4 price the same review rounds).
- **#17** rules drill-downs ("affected first") are sorted by key, not impact — the worst item is row 15
  of 85.
- **#21** the sprint lens is a time window, not a sprint filter: it doesn't filter story lanes, doesn't
  highlight the chosen sprint, and the custom range isn't in the URL.
- **#23** Policy lab: "cap PRs" throughput compares *many small PRs* with *few big PRs* (apples to
  oranges); "WIP limit 1" shows cycle −11.4 % in the headline while lead time +257.6 % has no verdict
  line; the selects lack labels (a11y).

## Fix (conforms to concepts.md §6)

1. **Sprint "done" by end** — a story is "done in S" iff it reached `done` by S's end; show the
   **done-by-end / done-late / never** breakdown, and derive carry-over from "committed but not done by
   S's end" (not only from an explicit re-add). Beside the membership verdict, show the **window
   velocity**: every story/point that reached done inside the sprint's dates, with carry-overs from
   earlier sprints credited (`window_done` / `window_carry_in`). "Committed" uses the sprint's start
   **day**, not its exact start second (plan-day grace).
2. **At-stake additive** — each at-stake hour is attributed to exactly one rule (the most specific); a
   rule that is a subset of another is no longer double-counted in the headline total.
3. **Drill-down sorted by impact** (wait-hours descending), so "affected first" is literally true.
4. **Sprint lens filters + highlights** the chosen sprint's stories, marks its band, and puts the range
   in the URL (shareable).
5. **Policy-lab honesty** — "cap PRs" reports a work-unit throughput (delivered work, not PR count);
   every scenario row gets a verdict line including lead-time blow-ups; selects get labels.
6. **New rules** the data wants: a **stale/zombie PR** rule (open > N business days, or ticket Done but
   PR open), a **sentinel-tail** rule (items waiting only on the sentinel past a threshold), and a
   **sprint on-time delivery** rule. A "combine scenarios" hint in the lab (compose sentinel + SLA, not
   one knob).

## Tests

- Sprint chip counts done-by-end (a story finished after the close is "late", not "done"); at-stake sums
  without overlap; drill-down is impact-sorted; the sprint lens filters + URL-round-trips; the three new
  rules fire on the del-combined data (stale PRs, sentinel tail, sprint late).

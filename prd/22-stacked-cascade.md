# 22 — Stacked-PR cascade

PRs stacked on each other (A's target branch == B's source branch) amplify rebase/review waste: a
lower PR's update/merge forces every PR above it to rebase and re-review — the cost compounds up the
stack (prd/05 §cascade).

## Ingest

Store each PR's `fromRef`/`toRef` branch names (if not already persisted) so the stack DAG is derivable.

## Analysis

1. **Build the stack DAG** — `A → B` when `A.toRef == B.fromRef`.
2. **Cascade cost** — for each lower-PR merge/update (a `pr_merged` or a `push_class` rebase), the
   rebases + re-review time it triggered on every PR above it; run a critical path over the DAG.
3. **Metric + suggestion** — `stacked-cascade`: "N stacked PRs; a lower-PR update triggered X rebases
   and Y h of re-review up-stack → merge stacks bottom-up / cap stack depth." Plus a `stack-depth`
   distribution (median/max depth).

Business hours via the calendar; wall-clock otherwise.

## Tests

Synthetic: a 3-PR stack (C on B on A), A merges → B and C rebase + re-review; assert the DAG, the
cascade cost, and the emitted suggestion. Keep 458 + 65 green.

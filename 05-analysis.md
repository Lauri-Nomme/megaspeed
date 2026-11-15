# 05 — Analysis: who blocks whom, and what to change

The analysis layer reads **only the pseudonymised event log**. The techniques
below are standard process mining / queueing theory applied to it.

## Ball-in-court state machine

At every moment, each open object waits on a specific person or group:

- **PR after a push** — the ball is with every reviewer whose approval is still
  needed for the merge condition. The sentinel group counts as one queue.
- **After a blocker comment or NEEDS_WORK** — the ball is with the author.
- **After the author replies to a thread** — with the person who commented.
- **While CI runs** — with the synthetic `CI` actor.
- **Story with unanswered questions** — with whoever is expected to answer.
- **Ready-to-refine** — with the refinement `calendar` actor.

Integrating time over these intervals yields the blocking graph.

## Handover / blocking graph

A directed, weighted graph where edge `X → Y` means **"X waited on Y for N
business hours"**. This is a more honest version of van der Aalst's
handover-of-work network: it captures not just "who handed to whom" but *who sat
waiting on whom*.

Presented at **role/queue level** with pseudonyms — never as a public
person-by-person ranking (see `08-risks.md`).

## Queue view per person

A time series of items waiting on each person. With **Little's law**
(wait time = WIP ÷ throughput), distinguish:

- a **slow server** (long service time, low WIP), vs
- an **overloaded server** (high WIP, normal service time).

These need different fixes: coaching/tooling vs. adding capacity / reducing load.
Queueing theory (Kingman's formula) predicts waits grow *sharply* as reviewer
utilisation approaches 100% — a little slack for sentinels may outperform every
other intervention.

## Waiting-time decomposition

Split cycle time into named segments and compute **flow efficiency**
(active ÷ elapsed):

```
ready→refined · refined→sprint · sprint→in-progress · in-progress→PR open ·
PR open→first review · review rounds · final approval→merge · merge→done
```

- "Active" signals: commits, comments, pushes.
- Worklogs: used **only** as a cross-check (we measure their noise directly).
- `ready→refined` is mostly batching delay (weekly refinement) — a candidate
  for async refinement.

## Rework loops

Count review rounds per PR; classify each round by cause:

- mandatory comment (blocker),
- suggestion the author chose to take,
- rebase (pure or with conflict),
- conflict.

The *cause mix* matters more than the overall rework rate: rebase-driven rework
is often avoidable (stack discipline), suggestion-driven rework is choice,
mandatory-driven rework is real defect risk.

## Cascade cost on the dependency DAG

For stacked PRs, find every event where merging/updating a lower PR triggered a
rebase above it plus the re-review time that followed. Run **critical-path
analysis** over the Jira "blocks" links + stack edges for the epic. This is
where a single bottom-of-stack merge silently costs many hours up-stack.

## Conformance checking

Compare the intended process with what actually happened:

- work started without refinement,
- story points changed *after* work started,
- PR merged with no +1,
- story carried over from one sprint to the next.

Conformance violations are cheap, concrete wins: they point at process drift
rather than at people.

## Drivers of cycle time

Use **survival analysis** (Kaplan–Meier, Cox regression), **not plain averages**,
because items still open at export time are *censored* data — averages over
censored data are systematically wrong.

Candidate covariates: PR size, reviewer count, *which* sentinel, open-question
count at refinement, phase where questions were found, time of day PR opened,
stack depth.

## Finding the best course of action

1. **Fit a simulation.** From mined data: arrival rates, per-activity
   per-person service-time distributions, rework probabilities, reviewer
   availability. Build a discrete-event model (SimPy; or Simod for BPMN).
   Validate against historical cycle-time distributions.
2. **Test policies *before* inflicting them on the team:**
   - review before starting new work ("stop starting, start finishing"),
   - rotate / add sentinels,
   - review time-boxes / SLA,
   - PR size cap,
   - shallower stacks, or merge stacks bottom-up fast,
   - async refinement between meetings,
   - per-developer WIP limit.
3. **Daily prioritisation rule.** For each person, list items waiting on them,
   ranked by downstream unblock value: number of dependent PRs/stories × expected
   wait, weighted by cost of delay (WSJF-like). This turns the analysis into a
   practical "what should I do next" report — the product's daily output.

## The question/answer lens

Because questions are first-class objects (phase-tagged), we can measure:

- how long questions stay open, and who answers them;
- **in which phase** questions are discovered — a question found in *review*
  costs far more than one found in *refinement*. Shifting that distribution
  left is one of the strongest levers in the whole system, and it's a
  *system* change (better refinement, a "Q:" convention, sub-task types), not a
  person change.

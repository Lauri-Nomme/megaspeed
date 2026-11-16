# 23 — Simulation / policy lab

The discrete-event simulation that answers "what would happen if we changed X" — so a policy can be
tested *before* it's inflicted on the team (prd/05 §"Finding the best course of action", prd/09 View 4).

## Purpose

Fit a model calibrated from the mined store, then let the user run counterfactual scenarios (WIP limits,
sentinel changes, size caps, review SLAs) and see the predicted impact on cycle time, flow efficiency,
and the bottleneck — side by side against the historical baseline.

## Calibration (all derivable from the store)

- **Arrival** — PRs opened per day (empirical, per repo/project).
- **Author service time** — the author-ball interval distribution (time the author demonstrably works:
  commits/pushes inside the interval).
- **Reviewer service time** — per-reviewer review-ball interval distribution (time each reviewer holds
  the ball while reviewing).
- **Rework** — probability of a rework round (PRs with >1 round ÷ PRs) + the `push_class` distribution
  (content / pure_rebase / rebase_conflict), which decides whether a re-review is a *waste* rebase.
- **Reviewer pool** — PERSON objects + their business-hours calendar (overrides, vacations, holidays).
- **Sentinel** — the sentinel actor(s) + their service time (the mandatory-approval gate).
- **PR size** — commits/lines distribution, correlated with author service time.
- **Stack depth** — distribution of how deep PRs stack.

## Model (SimPy discrete-event)

- A PR generator draws inter-arrival times from the arrival distribution.
- Each PR: author work (service time) → review (a reviewer *resource*, capacity 1; busy → queue) →
  [rework? author re-work + re-review, with the rework probability] → sentinel approval (mandatory) →
  merge.
- Reviewers/sentinels are resources with their own service-time distributions and calendars; work is
  scheduled in business hours.

## Policy knobs (scenarios)

WIP limit per author · sentinel count (1 vs 2, or rotation) · review SLA/time-box · PR size cap ·
stack-depth cap · "review before starting new work".

## Outputs

- Predicted **cycle-time distribution** (median/p90), **flow efficiency**, **bottleneck** (which
  reviewer/queue hits ~100% utilisation), queue lengths.
- **A/B compare**: baseline vs one (or more) scenarios, deltas side by side.

## Validation (the honesty device)

Run the *baseline* scenario and compare its predicted cycle-time distribution to the historical one.
If they disagree beyond a tolerance, the model is **not trustworthy** and the UI says so (with the
mismatch), rather than presenting confident-looking but wrong numbers.

## UI (Policy lab)

- Scenario config (the knobs) → "Run" → predicted distribution + bottleneck + flow efficiency.
- A/B compare two scenarios. Every predicted number is prefixed `≈`; a provenance line states the
  calibration window + PR count ("calibrated from 128 PRs, 2026-08→11").

## Out of scope (v1)

Multi-repo/multi-team co-simulation · Jira ticket-flow simulation (PR review loop first) · the full
8-segment lifecycle · network/load effects.

## Validation gate (definition of done)

The baseline model must reproduce the historical cycle-time median within a stated tolerance (e.g. ±20 %)
on `del3` before any scenario result is shown. That's the whole point of the feature.

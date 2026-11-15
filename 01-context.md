# 01 — Context, goals, non-goals

## The problem

Software delivery throughput is a black box. We know cycle time is too long,
but not *where* the time goes: handoff queues, review latency, rebase churn,
question-answer round-trips, or carry-over between sprints. When two people
disagree about "what's blocking us", both are arguing from anecdote.

We want to replace anecdote with a **fact base mined from the tools we already
use** (Jira, Bitbucket, Slack), and turn it into a **recommendation engine** for
the team, not a ranking of people.

## The setting (as observed)

The delivery process has this shape:

- **Ticket layer.** Multiple epics; one is the focus. An epic is broken into
  stories by one person, then marked *ready to refine*. Stories carry open
  questions. Refinement happens once a week with the team; more questions
  surface, some get answered, points are assigned (1 point = 8 h) → status
  *refined*.
- **Sprint layer.** Refined stories are assigned to two-week sprints. Devs pick
  stories up, may discover more questions, discuss in Slack, clarify goals, then
  set the story *in progress* when they have capacity.
- **PR layer.** Work produces commits and a PR. Reviewers are assigned; some are
  *sentinels* whose approval is mandatory before merge, a normal reviewer's +1
  is encouraged. Reviewers comment (some suggestions, some mandatory), authors
  and reviewers read/answer, optionally new commits push, **existing approvals
  are lost**, review restarts. Loop until the merge condition is met.
- **Stacking.** Stacked PRs and conflicts force dependent PRs to be rebased,
  which loses reviews.
- **Worklogs.** Time is logged on tickets, but with delay and coarse accuracy —
  so worklogs are a *cross-check*, not the primary signal.

This is not one process with one case ID. It is several overlapping processes
(story, PR, review round, question) that only touch each other in specific ways.
Forcing it into a flat per-story trace would destroy the signal we need.

## Goals (measurable)

1. **Waiting-time decomposition** — split cycle time into named segments
   (ready→refined, refined→sprint, sprint→in-progress, in-progress→PR open,
   PR open→first review, review rounds, final approval→merge, merge→done) and
   compute **flow efficiency** (active time ÷ elapsed).
2. **Blocking graph** — a directed, weighted graph `X → Y` = "X waited on Y for
   N business hours", from a ball-in-court state machine over every open object.
3. **Queue view** — a per-person/queue time series of items waiting on them, so
   we can distinguish "slow service" from "overloaded" (Little's law).
4. **Rework causes** — count review rounds per PR and classify each round by
   cause (mandatory comment, suggestion the author accepted, rebase, conflict).
5. **Cascade cost** — the cost of stacked-PR rebases propagating up the
   dependency DAG.
6. **Conformance** — where the real process deviates from the intended one
   (work started before refinement, points changed after start, merged with no
   +1, carry-over).
7. **Cycle-time drivers** — survival analysis (Kaplan–Meier / Cox) on censored
   data, identifying which covariates actually move cycle time.
8. **Policy recommendations** — a discrete-event simulation calibrated from the
   mined data, used to test interventions *before* inflicting them on the team,
   plus a daily "what should I do next" prioritisation report.

## Deliverable

The product is an **interactive web interface** over the pseudonymised event
log. Two outputs, in priority order:

1. **Timeline visualization** (ship first) — a left-to-right timeline of events
   and actors, gantt-style object bars, and blocking visualization, with zoom
   in/out.
2. **Simulation in the web UI** (ship second) — run calibrated scenarios from
   the browser and compare them.

See `09-web-ui.md`.

## Non-goals

- **Not a people-ranking tool.** Output is presented at role/queue level with
  pseudonyms. The tool must be defensible to HR and the works council, not a
  stack-rank.
- **Not real-time alerting** (a "PR has waited 3h" pager). A future phase, not
  this one.
- **Not a general BI/reporting warehouse.** Narrowly about delivery flow.
- **Not a workflow replacement.** We don't re-implement Jira/Bitbucket; we
  observe them.
- **Not a content archive.** Raw descriptions, comments, commit messages, and
  diffs are never retained in the analysable store. See
  `02-pseudonymization.md`.
- **Not an LLM that reads ticket prose downstream.** The open-source LLM used
  for analysis sees only pseudonymised, classified events — never source text.

## Scope of the first release

One team, one focus epic, but mined against **the team's full history** (a
single epic is too small a sample). Sources, in **ingest priority order**:
**Bitbucket Data Center** (#1), **Jira Cloud** (+ **Jira Cloud Tempo** for
worklogs, #2), **Slack** (#3, de-prioritised). The Bitbucket activities stream
+ patch-id push classification is the cleanest signal and is the spine of
slice 1.

## Constraints that shape everything

1. **Pseudonymisation by construction** (the special requirement) — the primary
   datastore may contain only ticket keys, abbreviated actor IDs, and
   *enumerated classifications* of content; never the content itself.
2. **Classification runs locally** on a cheap LLM; the open-source LLM doing
   analysis operates on the pseudonymised view only.
3. **Determinism first** — anything classifiable with `git patch-id`, regex, or
   schema (not an LLM) is classified that way. LLM only for natural-language
   signals.
4. **Reviewability** — event-building logic lives in SQL so a human can audit
   exactly how a raw event became a pseudonymised one.

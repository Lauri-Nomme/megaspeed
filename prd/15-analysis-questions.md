# 15 — Analysis questions → metrics → suggestions

Maps the "how do I …" questions to (a) what megaspeed can answer **today**, (b) the gap, and
(c) the rule/suggestion the metric is meant to inform. Everything routes through the pseudonymised,
closed-vocabulary event log; person-level results are presented at role/queue level (prd/08).

## Q1 — Is a reviewer slow / do they prefer their own PR work?

- **Today**: *partially.* Ball-in-court (analysis/ballcourt.py) already yields per-person wait hours,
  the handover graph (`author→reviewer` = hours waited), queue WIP (Little's-law slow vs overloaded),
  and review-latency (THREAD open→resolved, where resolution is present).
- **Gap**: no explicit **reviewer-responsiveness** metric. Need, per person: median `PR-ready-for-review →
  first review action` (comment/approve), and a **self-vs-other contrast** — hours spent as *author* on own
  PRs vs hours spent as *reviewer* on others'. "Prefers own work" = high review-latency + low own-cycle-time
  (or review-ball ≪ author-ball). Pure analysis of existing events — no new data.
- **Suggestion it informs**: review SLA ("first response < 24 h, median"); *review before starting new work*
  (stop-starting-start-finishing); sentinel rotation.

## Q2 — Conflict / rebase / lost-review lag?

- **Today**: *partially.* We count rescopes (`pr_pushed`), lost approvals (`pr_unapproved`), and review rounds.
- **Gap**: **`push_class`** (content vs pure_rebase vs rebase_conflict) is unimplemented — it needs
  `git patch-id`/`range-diff` against a local clone on the scrape side (GAP-01). Without it we can't
  attribute a lost approval to *rebase* vs *real rework*. This is the highest-leverage missing piece.
- **Suggestion it informs**: don't rebase unless necessary; keep auto-unapprove tied to patch-id change only
  (the hans finding); distinguish "avoidable rebase waste" from "real rework".

## Q3 — Stacked PRs (worse)?

- **Today**: *no.* The dependency data exists (target-branch relationships, stacked branches in the corpus),
  but the **cascade-cost** analysis (prd/05 §cascade) is not implemented.
- **Gap**: build the stack DAG and measure, for each lower-PR merge/update, the rebases + re-review time it
  triggers up-stack. Critical-path over the DAG for the epic.
- **Suggestion it informs**: merge stacks bottom-up fast; cap stack depth; treat stack-head latency as the
  bottleneck metric.

## Q4 — Who posts BS comments (nits / out-of-scope / needless questions)?

- **Today**: *no.* Comments are events (`comment_added`) and THREAD objects, but there is **no classification**
  of comment *type* or thread *outcome*.
- **Gap (the interesting one)**: two classifications, both scrape-side via the local cheap LLM (closed
  vocabulary, per prd/02 + prd/11 airgap):
  1. **comment type** ∈ {blocker, suggestion, question, nit, out-of-scope, optimization} (severity already
     partially available from DC `BLOCKER`/task markers);
  2. **thread outcome** ∈ {acted-on, rejected, walked-back, unresolved} — derivable from the thread's
     resolution state (`resolvedDate`/`resolver`) + whether the diff changed at the comment's anchor.
  Then measure **review-cycle cost by comment type**: a *rejected* or *walked-back* nit/question costs a full
  review round for zero value.
- **On "blame the poster":** we *can* classify the outcome, but we should **not** attribute it to individuals
  — that's a GDPR/works-council/Goodhart landmine (prd/08). The principled form is a **system metric**: the
  share of review cycles consumed by each comment type, aggregated at role/queue level, so the *rule* is
  "nits/out-of-scope go on the ticket, not the PR" and "questions get resolved in refinement, not review".
- **Suggestion it informs**: comment guidelines; nit/out-of-scope → ticket; question → refinement; a
  "comment-type → cycle-time" heatmap to prove which class of comment is the tax.

## What else (additional metrics → suggestions)

- **Time-to-first-review + time-to-merge** (median, not mean) — the two headline lag numbers; first-review is
  the strongest predictor of velocity (Google's finding).
- **PR size vs review latency** — big diffs get slow, shallow reviews → a PR size cap.
- **Question-phase distribution** — a question discovered in *review* costs ~10× one found in *refinement*;
  shift it left (needs the LLM classifier above).
- **Conformance violations** — work started before refinement, merged without +1, points changed after start.
- **Reviewer load balance** — Little's law to separate "slow server" from "overloaded server" (different fixes).
- **Sentinel queue** specifically — the mandatory-approval queue is usually the real bottleneck.

## Literature / competing products (brief)

- **Process mining** (van der Aalst): handover-of-work networks + queueing (Little's law, Kingman) — the
  theoretical spine; megaspeed is an object-centric (OCEL) instance of it.
- **DORA**: lead-time-for-changes etc. — megaspeed's "cycle time" is a finer-grained, per-object version.
- **Eng-productivity BI** (LinearB, Swarmia, Pluralsight Flow, Jellyfish, Waydev, Code Climate Velocity,
  CodeScene): they surface cycle time / time-to-first-review / review load — but as *aggregate BI*, not
  object-centric event mining. None do rebase-waste, stacked-PR cascade, or comment-outcome attribution.
- **AI code review** (CodeRabbit, Graphite Agent, Qodo, GitStream, Ellipsis): they already do
  **severity/type classification** of comments ("define severity so the AI doesn't flood you with nits") and
  track an **acceptance rate** (% of findings that lead to a change). This is the closest existing thing to
  Q4 — but it's *pre-merge automation*, not retrospective analysis. megaspeed's local-LLM classifier is the
  same idea, applied to *measure* the tax instead of just avoiding it.
- **Stacked-PR tooling** (Graphite, Aviator, Mergify, Trunk): they *solve* stacking/merge queues; megaspeed
  *measures* the cost of the current (unmanaged) process.

**Net for Q4:** "classify comment type + thread outcome" is real and precedented (AI reviewers do type/severity;
  "nitpick" is a named anti-pattern; Google-style review guidelines exist). "Blame the original poster" is
  *not* — no one ships that, for good reason. The value megaspeed can uniquely add is **retrospective,
  pseudonymised, role-level measurement** of *which* comment classes burn the most review cycles.

# Concepts — the single source of truth for what the numbers mean

This pins down every term the UI and API use, so the same concept is defined, derived and presented
identically everywhere. It exists because exploratory testing found the product giving *accurate raw
numbers* but *inconsistent meanings* for the same words across views. The rule: **one term, one
definition** — the implementation must conform to this file, and any view that presents a concept
must use the name and derivation specified here.

Each entry: **Definition** (what it is) · **Derivation** (how it's computed from the store) ·
**Purpose** (what question it answers).

---

## 1. Time and units

### Business hour (`h`)
- **Definition**: one hour of the team's business calendar — a calendar with per-person schedules,
  overrides, vacations and Estonian holidays (incl. half-days).
- **Derivation**: the business-hours calendar (`analysis/calendar.py`) prices a wall-clock span by the
  intersection of `[t1, t2)` with the relevant person's (or the team's) working time.
- **Purpose**: the unit of *every* duration the product reports for "how long did X take". Elapsed
  wall-clock time is never the headline.

### Day (`d`)
- **Definition**: **24 business hours**. It is *never* a calendar day.
- **Derivation**: `d = h / 24`; rendered as `Nd Nh` (e.g. `7 d 0 h` = 168 business hours).
- **Purpose**: a human-scale rendering of business hours. **Any place that means a calendar day must
  say "calendar day" explicitly** (the PR drawer's wall-clock span is the one legitimate exception —
  it is labelled as calendar time, not "d").

### Person-hour
- **Definition**: an hour of *one* person's business time (a `business_hours` span priced on that
  person's calendar).
- **Derivation**: sum of per-person business hours over the relevant spans.
- **Purpose**: the unit of *load* and *at stake* — the cost of one person's attention. It is not the
  same as elapsed team time (two people blocked for one hour = two person-hours).

---

## 2. Actors

### Person
- **Definition**: a human, identified only by a stable abbreviated pseudonym (`LaNo`), one pseudonym
  per human across Bitbucket and Jira.
- **Derivation**: cross-source identity (prd/02); Bitbucket slug/email and Jira user are merged into
  one pseudonym.
- **Purpose**: the unit of attribution for work, review, waiting and constraint.

### Bot
- **Definition**: an automation account, not a human. Known via `MEGASPEED_BOT_SLUGS` plus Jira's
  `SERVICE` user type and other deterministic signals (e.g. a single actor declining many stale PRs
  in one instant).
- **Derivation**: `classify_actor` → `bot`.
- **Purpose**: **bots must never appear as a constraint, a reviewer, or an owner of wait-hours.** They
  contribute activity for the timeline, but their "work" and their queues are excluded from every
  person-level aggregate (constraint ranking, "overloaded", at-stake). *Fix target: the exploratory
  run charged an automation 2,223 person-hours and ranked it #1 — that must not happen.*

### Sentinel
- **Definition**: a *human* who is the mandatory approver on (typically) every PR — not a bot, and a
  distinct role from a reviewer.
- **Derivation**: configured via `MEGASPEED_SENTINEL_SLUGS`; matched by slug.
- **Purpose**: a sentinel is a named, real constraint — the queue in front of them is exactly the kind
  of bottleneck the product must *surface by name* (`"waiting on <sentinel>"`), not hide as
  "waiting on review queue".

### Constraint / bottleneck / "overloaded"
- **Definition**: the actor or queue that a PR (or story) is *currently waiting on*, ranked by the
  wait-hours it causes. "Overloaded" = a constraint whose queued wait exceeds a stated threshold.
- **Derivation**: ball-in-court attribution — for each open interval, the ball is with the set of
  people the item waits on; wait-hours are summed per person (humans only, sentinel included and
  named).
- **Purpose**: answer "who is the constraint right now". It must be *consistent* with the other
  views: if the queue view says a person is overloaded, the policy-lab's utilisation of that same
  person must not say they're 55% idle without explaining the different lens (queue wait vs calendar
  utilisation are different metrics and must be labelled as such, never both called "load").

---

## 3. Work items

### Ticket
- **Definition**: a Jira issue, `STORY:<KEY>` (also `TICKET:<KEY>` in the store's vocabulary for the
  ticket-key dimension).
- **Derivation**: issue → object; the ticket key is the join to PRs (`PR.ticket_keys`).
- **Purpose**: the unit the process-mining questions are asked about ("why did DEL-167 take 3 weeks").

### Story / Bug / Epic / Task
- **Definition**: issue types. Story and Bug are the work items the workflow stages describe; Epic is
  a parent grouping stories (`story.epic_key`); Task/Improvement/Technical Task count as stories.
- **Purpose**: the epic→story→PR hierarchy on the timeline, and the sprint-scope grouping.

### Pull request (PR)
- **Definition**: a Bitbucket PR, `PR:<repo>#<n>`, with state `open`/`merged`/`declined` and a link to
  one or more tickets (`ticket_keys`).
- **Purpose**: the unit of code-level work, review, merge; nested under its story on the ticket view.

### Sprint
- **Definition**: a two-week planning window with `start`, `planned_end`, and a completion (or
  snapshot for the active sprint).
- **Derivation**: Jira sprint objects + `sprint_added`/`sprint_removed` memberships.
- **Purpose**: the planning unit for the sprint lens (bands, scope, carry-over).

---

## 4. Workflow stages

A configurable status→stage projection (prd/29) maps each workflow status name to a closed stage set:

| stage | meaning |
|---|---|
| `backlog` | not ready to be worked |
| `ready` | refined/triaged, ready to start |
| `in_progress` | being developed |
| `in_review` | under review |
| `in_verification` | under QA/verification |
| `done` | closed |

**Purpose**: the Jira-side lifecycle. `status_changed` events carry `from_status`/`to_status` as these
stage labels (never the raw status name — prd/02). Precedence: configured map → Jira's own category →
name rules.

---

## 5. The ticket lifecycle and its segments

The 8-segment (9 with the QA split) decomposition of a story's cycle. Boundaries are **measured, never
assumed**: a segment whose two boundaries the store can't supply is reported as *missing*, and the
answer states its honesty ("6 of 8 segments measured … right-censored").

| segment | exact boundaries |
|---|---|
| **created → ready** | `created` → first `ready` (a `refined`/`triaged` status), else first `pointed` |
| **ready → sprint** | `ready` → first `sprint_added` |
| **sprint → in progress** | first `sprint_added` → first status→`in_progress` |
| **in progress → PR open** | `started` → first PR `created` |
| **PR open → first review** | first PR open → first reviewer action on it |
| **review rounds** | first review → final approval |
| **final approval → merge** | last approval → PR `merged` |
| **merge → verification** | `merged` → first status→`in_verification` (present only when the store has a QA stage) |
| **verification → done** | first `in_verification` → first status→`done` (or story close) |

**Label honesty** (fix targets from the exploratory run):
- The first segment's **UI label is `created → ready`**, *not* `ready → refined` — refinement is the
  *work* that happens inside it, not its name.
- A segment's hours are the business hours between its boundaries. If a segment overlaps a gap with
  no PR open (e.g. "review rounds" containing 60 h of no-PR-open time), that must be stated, not
  folded silently into the segment's headline.

### Cycle time
- **Definition**: `created` → `done` (or the snapshot, right-censored).
- **Purpose**: the headline "how long did this take".

### Censoring
- **Definition**: a ticket/PR whose cycle is cut off by the snapshot is *right-censored* (open PR,
  open story), and the view says so; a segment it can't measure is *missing* and named.
- **Purpose**: never present an incomplete cycle as complete.

---

## 6. Metrics (and how to present them)

### Flow efficiency
- **Definition**: active hours ÷ measured cycle hours (the share of the cycle spent demonstrably
  working, not waiting).
- **Derivation**: union of measured work spans (author PR intervals, worklog durations) ÷ the cycle.

### Wait hours / queue time
- **Definition**: business hours an item spends with the ball *not* in the author's court.
- **Purpose**: the cost of waiting, attributed to the queue/person holding the ball.

### At stake
- **Definition**: the wait-hours a change would eliminate, summed over the items it would unblock.
- **Derivation**: per-item current wait × impact, summed. **It must not double-count** (an item may be
  waiting on several people; its hours are attributed once, not once per blocker).

### "Per story" vs aggregate
- **Definition**: a *per-story* figure is a statistic (median/mean) over stories; an *aggregate* is the
  total across stories. They must never be conflated: a total rendered as "100.1 d per story" (total
  ÷ 24 with a "per story" label) is a bug — the label must match the statistic.

### Sprint "done" / scope / carry-over
- **Definition**: a story is *done in sprint S* if it reached `done` **by S's end** (not "by now").
  Scope-in = committed at S start; scope-out = never committed or removed to backlog; *carried over* =
  committed to >1 sprint.
- **Derivation**: `sprint_added`/`sprint_removed` + the `done` boundary, compared to S's start/end.
- **Purpose**: the sprint lens answers "did S deliver what it committed", so "done" must be
  measured at S's close. *Fix target: "27 done" counted by today, not by S2's end (really 15/30).*

### Constraint ranking (delivery/queue)
- **Definition**: open items ranked by who they wait on and the wait-hours that costs.
- **Derivation**: per §2 constraint; **bots excluded, sentinel named**; a "mean wait N d" must use the
  §1 "d" (24 business hours).

### Rules / suggestions
- **Definition**: the ranked "speed up delivery" suggestions, each with its evidence count and the
  items behind it (drill-down).
- **Purpose**: answer "what should we change first". Each rule's evidence must agree with the other
  views' numbers for the same concept (a "smaller PRs" rule must not contradict the policy-lab's
  size-cap scenario — if they disagree, the metric they use is different and must be named).

---

## 7. Data model & pseudonymisation (prd/02 — non-negotiable)

- The store and every API response contain only **ticket keys, abbreviated actor pseudonyms, timestamps
  and enumerated closed-vocabulary labels**. No raw description, comment, diff, commit message, or
  workflow status name ever crosses into the store or the API.
- All classifications are a projection onto a closed set (`taxonomy.py`); "other" is the explicit
  "could not decide" escape hatch, never free text.

---

## How to use this file

- A PRD that changes a metric must state which concept it changes and confirm it conforms to the
  definition here (or amend this file first).
- A view that presents a number must use the **name** from this file for that number. If two views show
  different numbers under the same name, one of them is a bug.

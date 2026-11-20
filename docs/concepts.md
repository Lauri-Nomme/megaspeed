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
- **Definition**: **8 business hours — one working day.** It is *never* a calendar day, and never
  24 hours.
- **Derivation**: `d = h / 8` (a team working day is 8 h); `w = d / 5` (a working week = 5 d = 40 h).
  Rendered as `Nd Nh` (e.g. `21 d 0 h` = 168 business hours = 4 working weeks).
- **Purpose**: a human-scale rendering of business hours that matches how people read "a day" of work.
  **Any place that means a calendar day must say "calendar day" explicitly** (the PR drawer's
  wall-clock span is the one legitimate exception — it is labelled as calendar time, not "d").
- **Rationale (the correction):** "d = 24 h" is wrong — 24 business hours is 3 working days, not one,
  so `168 h → "7 d"` read as a week when it is actually 4 working weeks. `d = 8 h` makes `40 h = 5 d =
  one calendar week`, which is the unit people expect.
- **Waiting week**: a working week is **5 d = 40 business hours**. It is the single threshold behind
  every "waiting over a week" / "waiting 1w+" label (Overview card, row pill, list header, footer,
  `counts.waiting_over_week`); a view may not invent its own.

### Person-hour
- **Definition**: an hour of *one* person's business time (a `business_hours` span priced on that
  person's calendar).
- **Derivation**: sum of per-person business hours over the relevant spans.
- **Purpose**: the unit of *load* and *at stake* — the cost of one person's attention. It is not the
  same as elapsed team time (two people blocked for one hour = two person-hours).

### Lead time vs cycle time
- **Definition**: *cycle time* = `created` → `done` (the ticket's own span, §5). *Lead time* = from
  "work requested" to "delivered to the customer" — the queue *before* the ticket starts, **plus** the
  cycle. In the simulation, lead time is cycle time + the arrival queue.
- **Purpose**: the two must never be conflated. A WIP cap can shorten cycle time while *blowing up* lead
  time (more waiting to start); the policy lab reports both, never one as a proxy for the other.

### Throughput
- **Definition**: items delivered per unit time (PRs/week, or delivered *work* — points/stories — per
  week).
- **Derivation**: merged items in the window ÷ window length. When comparing scenarios, throughput must
  count *work delivered*, not *PR count* (many small PRs ≠ more delivered work).

### WIP (work in progress)
- **Definition**: the number of items concurrently in a state/queue (or the cap on that number).
- **Derivation**: the `queue_series` difference array — an interval contributes +1 from its start bucket
  to its end bucket; the prefix sum is the WIP at each bucket.
- **Purpose**: the queue view's "wip / mean / peak", and the policy-lab's WIP-limit knob.

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

### Author / reviewer / approver (roles)
- **Definition**: *author* = the PR's owner (holds the ball while working). *Reviewer* = anyone asked to
  review (the roster in `reviewer_changed`). *Approver* = a reviewer who has approved. *Sentinel* is a
  distinct mandatory approver (§2 Sentinel).
- **Purpose**: attribution. A PR in review waits on **every** reviewer asked who has not yet answered
  (`status[r] = none`), not whichever one acts first; a reviewer who answers (approve / needs-work /
  reviewed) leaves the pending set, an added reviewer joins it, a removed one leaves it. A push does
  **not** re-pend a standing approval (only `approvals_lost` does), and a push-induced unapproval
  returns the reviewer to pending, never the author (§2 "status vector").

### "Ball in court" / waiting on / status vector
- **Definition**: every PR has a **participant-status vector** — one status per roster member,
  `status[r] ∈ {none, approved, needs_work, reviewed}` (Bitbucket's own participant status, which is
  what the events record) — plus a **debt set** `debt ⊆ R` (the reviewers whose objection still stands)
  and a `ci` flag. *Everything else is derived, never stored*:
  - `author_open = debt ≠ ∅` — the author owes a fix;
  - `P = {r : status[r] = none}` — the pending reviewers;
  - `ready = ¬author_open ∧ P = ∅ ∧ R ≠ ∅` — every reviewer answered, not merged;
  - `doubly_blocked = author_open ∧ P ≠ ∅` — fixing one side does not unblock the PR;
  - `ball ∈ {author, review, ready}` — a **priority-ordered label** over those two dimensions;
  - `holders = {author if author_open} ∪ P` — **the truth**. `ball` is only a label; when both
    dimensions are non-empty both sets are reported (`holders`), and the label picks `author`.
- **Derivation**: `analysis/ballcourt.py` replays the event stream with the transitions in the table
  below. No event flips a ball: it changes one participant's status, or the debt set, or the CI flag.
- **Transitions** (prd/40):

  | event | effect |
  |---|---|
  | `pr_approved` (r) | `status[r]=approved`; `debt −= r`; touches no one else — an approval does **not** cancel another reviewer's objection |
  | `pr_needs_work` / manual `pr_unapproved` / mandatory comment (r) | `status[r]=needs_work`; `debt += r` — an objection is an answer, so r is not also pending |
  | `pr_unapproved` `cause=push` (r) | `status[r]=none` (r owes); the author's debt is **untouched** |
  | `pr_pushed` / `rebased` | `debt := ∅`; `status[r]=none` for `needs_work`/`reviewed` reviewers; for **all** reviewers iff `approvals_lost`; a `pure_rebase` with no loss is a head move → **no change** |
  | `comment_added` by the author | `debt := ∅`; `needs_work`/`reviewed` → `none` |
  | `reviewer_changed` added (r) | `status[r]=none` (pending); moves nothing else — a reviewer added mid-debt does not move the ball |
  | `reviewer_changed` removed (r) | drop r |
  | `build_status` | set the `ci` flag, cleared by the build result; **never exclusive** — a reviewer can still be pending while CI runs |

- **Sentinel gate** (`sentinel_after_peers`, default on): the sentinel is a *precedence* constraint,
  not a peer. A sentinel's pending clock opens only after every peer reviewer has answered; until then
  the sentinel is neither charged nor blocking. Configurable off for teams whose sentinel reviews in
  parallel with peers. "Answered" means **responded at least once**: a peer whose verdict was withdrawn
  (a `walked_back` blocker) or staled by a push has had their turn, so the sentinel's clock opens and
  both owe a verdict. A peer who has never responded at all keeps the gate closed.
- **Who owes vs who is charged**: the gate defers *charging* (the person-hour projections and the
  single named `ball_label`), never the answer to "who must act". The delivery row's `waiting_on`,
  `reviewers_pending` and `reviewers_approved` count **every** reviewer with `status = none`, the
  deferred sentinel included — so a PR nobody approved reads "0/2 approved, 2/2 pending" and names
  both people.
- **Attribution**: a person-level wait is attributed to **every holder** — the author while their debt
  stands, plus every pending reviewer — as *person-hours*, priced on **that person's own calendar**
  (a reviewer on vacation is not charged the hours someone else worked). A 10 h wait on two reviewers
  is 10 h for *each*, so the person-level total exceeds the wall-clock wait; role/queue level counts it
  once under `review` (or `ready`). `solo_hours` is the part of a person's total where they were the
  *only* open ball: **read `solo_hours` as the marginal load and the total as the shared wait it came
  from** — the payloads state this next to both numbers. `ready` is its own state: the merge click is
  the author's, but nobody's review wait.
- **Ex-post honesty**: a reviewer still pending when a PR merged was, provably, not required; those
  episodes are tagged `unrequired` (listed, not blamed) and excluded from the responsiveness median.
- **Purpose**: every "waiting on …" / "where waits accumulate" / constraint answer. The owner must be
  **named** (a specific person or role), never the generic "review queue" when the ball is with one
  person.


### Blocked vs needs-work
- **Definition**: a PR is *blocked* when a mandatory review comment is unanswered; *needs work* when a
  reviewer marks `NEEDS_WORK`. Both mean the ball is back with the author; "blocked" is the stronger
  signal (a blocker comment).
- **Derivation**: `pr_needs_work` / blocker `comment_type` → `blocked`.

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
  one or more tickets (`ticket_keys`). Its **commit count is the number of COMMIT objects it owns** —
  one canonical fact read by the size rule, the flow-efficiency rows, the delivery row and both PR
  drawers; the per-interval commit count is evidence inside a phase, never the PR's total.
- **Purpose**: the unit of code-level work, review, merge; nested under its story on the ticket view.

### Sprint
- **Definition**: a two-week planning window with `start`, `planned_end`, and a completion (or
  snapshot for the active sprint).
- **Derivation**: Jira sprint objects + `sprint_added`/`sprint_removed` memberships.
- **Purpose**: the planning unit for the sprint lens (bands, scope, carry-over).

### Review thread
- **Definition**: a PR comment thread (inline or general), classified into a closed `comment_type`
  (blocker / suggestion / question / nit / out-of-scope / optimization) and a `thread_outcome`
  (acted_on / rejected / walked_back / unresolved).
- **Derivation**: raw comment text → local LLM → closed label; the text is discarded (prd/02).
- **Purpose**: the "comment tax" metric — how much review effort was wasted on rejected/walked-back
  threads.

### Commit
- **Definition**: a git commit object, `COMMIT:<sha>`, classified by `message_class` (feature/fix/…).
- **Purpose**: the author's evidence of work (a PR interval is `active` when it contains commits).

### Tenant
- **Definition**: one pseudonymised store (`data/<name>.duckdb`) — a self-contained dataset scoped by
  the scrape's ticket/epic/project filter. The UI's tenant switcher selects it; every API read is
  tenant-scoped (`?tenant=`).
- **Purpose**: the unit of isolation — one team/environment per tenant, so analyses never mix.

### `objects.actor` — one column, several roles
- **Definition**: `actor` is the pseudonym associated with an object **in that type's role**, not a
  single "who did it" predicate. The source of truth is `ingest.store.ACTOR_ROLE_BY_TYPE`:

  | object type | `actor` means |
  |---|---|
  | `PR` | author (the PR's owner) |
  | `COMMIT` | git author |
  | `THREAD` | **resolver** (who closed it, never who opened it) |
  | `PERSON` | self (the row *is* the pseudonym) |
  | `PRREV` | pusher of that revision |
  | `STORY` / `EPIC` | owner (assignee, falling back to reporter) |
  | `TICKET` / `SPRINT` | none — a key dimension / a planning window |

- **Generic query**: `GET /api/objects?actor=LaNo` returns **all** of the above as one set — PRs they
  authored, commits they authored, threads they resolved, their PERSON row and revisions they pushed.
  Read it as "objects this person is the actor of in that type's role", never as "authored by" or
  "worked on". A view that wants one relation must pass `type=` as well; "who posts BS comments" is
  the thread's **openers** (`comment_added.actor` on the event log), not `THREAD.actor`.
- **Purpose**: stops the same filter from being read as one meaning across types (prd/44 DES-6).

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

### Out-of-order boundaries
- **Definition**: a boundary the store dates outside the `created → done` window, or before its
  lifecycle predecessor (e.g. `sprint_added` projected at the sprint's start, before the story was
  created — DEL-185). For segment arithmetic the boundaries are clamped into the cycle and made
  non-decreasing (cumulative max), so each segment is inside the cycle and the shares sum to ≤ 100 %.
  The raw derivation stays available (`boundaries`), and every clamped boundary is reported as a
  **data quirk** with the raw stamp and the clamped one; the ticket answer names it as a quirk, never
  a finding.
- **Purpose**: the decomposition cannot show "61 % in sprint → in progress" for a 100 h cycle whose
  segments sum to 200 %.

### Snapshot / as-of
- **Definition**: the store is a point-in-time capture; `as_of` is when it was taken. Any open item is
  measured up to the snapshot, and the provenance line names the snapshot time.
- **Purpose**: the honesty clock — a number is always "as of the snapshot", never "live".

### PR phase (the ball-court model)
- **Definition**: at any moment a PR is in exactly one phase — `waiting` (ball not with the author),
  `active` (author working, evidenced by commits), `review` (ball with reviewers), `ci` (a build is
  running and nobody holds the ball), `rebasing` (an interval ended in a rebase push), `closed`
  (merged/declined), or `unobserved` (no events since the snapshot). The phase stays a *derived
  priority label*: `ready` (§2) is a state, not a phase, so "all approved, not merged" still reads
  `review` here — read the interval's `ready`/`ball` fields to separate merge latency from review wait.
  CI is a flag too, never exclusive: a reviewer pending during a build keeps the `review` phase.
  When the phase *is* `ci` (a build running and nobody holding the ball), the blocking graph carries
  the wait under a synthetic `CI` node — the CI queue is a real, named bottleneck, not invisible.
- **Derivation**: `analysis/ballcourt.py` `PHASE_ORDER`, from commits/events per interval.
- **Purpose**: the timeline's coloured bars and the ball-in-court attribution. `unobserved` is *not* a
  real phase and is excluded from the blocking graph — but its tails (a PR with no recent events, e.g.
  the 3–5-week-old waits) must not be silently dropped from "what's stuck now" (§prd/34).

### Push class
- **Definition**: what a push *was* — `content` (new code), `pure_rebase` (same patch id, only moved
  commits), `rebase_conflict` (rebase that resolved conflicts), or `other`.
- **Derivation**: `ingest/gitclass.py` (patch-id + range-diff).
- **Purpose**: distinguishes real rework from a rebase; a `pure_rebase` is a *head move*, not a
  re-review (§Review round).

### Review cycle
- **Definition**: one trip of the status vector from "open/review" through "reviews answered" back to
  pending. A new cycle starts when a verdict is staled (a push, an `approvals_lost` reset, an author
  reply) — not when a reviewer answers.
- **Purpose**: the unit behind "review rounds" — see §6 Review round.

---

## 6. Metrics (and how to present them)

### Flow efficiency
- **Definition**: active hours ÷ measured cycle hours (the share of the cycle spent demonstrably
  working, not waiting).
- **Derivation**: union of measured work spans (author PR intervals, worklog durations) ÷ the cycle.

### Review round
- **Definition**: one discrete re-review pass — the author produces new work for review, *or* approvals
  are lost and must be re-earned.
- **Derivation**: `rounds = # pr_pushed (after first review) + # pr_unapproved with cause ≠ "push"
  (after first review)`. A push-induced `pr_unapproved` (`cause="push"`) is the *same* round as the
  push that caused it, not a second one; a `pure_rebase` push that lost no approval is not a round;
  a push before anyone has reviewed is setup, not rework. `analysis/ballcourt.review_round_events`
  is the single implementation both the rules page and the ticket view read, and the excluded events
  are reported (`excluded_*`) so the correction is visible.
- **Purpose**: the `rework_loops`/`conformance` rules and the ticket view's round count. **One
  definition everywhere** — the store's `review_rounds` and the analysis must agree. A rebase that
  loses approvals is **one** round (not two); a target-shift/manual approval-loss with no new push is
  **one** round.
- **Note**: a `pure_rebase` push (same patch id, no content change, no approval loss) is a *head move*,
  not a re-review — see `push_class`.

### Wait hours / queue time
- **Definition**: business hours an item spends with the ball *not* in the author's court.
- **Purpose**: the cost of waiting, attributed to the queue/person holding the ball.

### Sentinel wait (the canonical number)
- **Definition**: **last peer answer → the sentinel's first response** (approval, needs-work,
  unapproval or comment), priced on the sentinel's calendar. The gate is precedence-aware: the
  sentinel's clock opens only once every peer has answered (`sentinel_after_peers`), so the peer phase
  is not charged to them. One sample per PR; a request that was never owed (the PR merged or was
  declined/closed before their turn, or the peer gate never opened) is not a "never answered".
- **Derivation**: `analysis/ballcourt.response_times()["sentinel_wait"]`; the Rules `sentinel_queue`
  metric quotes the same block.
- **Purpose**: the one "how long does the sentinel take" number. Any other sentinel figure — the
  person-level queue mean (person-hours from *added as reviewer*, peer phase included), the blocking
  map's person-hours, the policy lab's fitted gate mean, the current wait on the tail items — is a
  **different metric** and must be labelled as one.

### At stake
- **Definition**: the wait-hours a change would eliminate, summed over the items it would unblock.
- **Derivation**: per-item current wait × impact, summed. **It must not double-count** (an item may be
  waiting on several people; its hours are attributed once, not once per blocker). A figure derived
  from an assumption (e.g. "half the largest segment") is **labelled as an assumption** in the payload
  and the UI — never presented as a measured saving.

### "Per story" vs aggregate
- **Definition**: a *per-story* figure is a statistic (median/mean) over stories; an *aggregate* is the
  total across stories. They must never be conflated: an aggregate total mislabelled "per story" (e.g.
  "100.1 d per story" for a whole-team total) is a bug — the label must match the statistic.

### Benchmark contrast (prd/57)
- **Definition**: any **per-item** duration shown in the UI is rendered next to its **cohort median** and a
  **multiple**, so the reader sees whether it is normal and by how much: `123 h (median 30 h · 4.1×)`.
  It answers "is this a one-off or the norm", not "what is the total" — so it is never drawn on an
  aggregate figure (those are already cohort statistics).
- **Derivation**: one helper (`analysis/benchmark.py` `contrast`) computes `median` and
  `multiple = value ÷ median` from the cohort the metric already uses, on the same business-hours clock
  and calendar:
  - the **cycle** and each **segment** → completed stories in the store (§9 Cohort benchmark);
  - a **PR phase** and the **PR cycle** → merged PRs;
  - the **delivery-list wait** → the window cohort of open PRs;
  - a **response time** → every reviewer answer in the window.
  The payload carries `n`, `min_n` and a `basis` phrase; the view labels the multiple with that basis
  ("vs median of N completed stories"). A cohort below **10** (`BENCHMARK_MIN_N`) is **suppressed** with
  the reason rather than compared — a multiple over three items is noise. A censored value (an open
  story's running age, an open PR's cycle) is suppressed too, never compared with completed work.
- **Purpose**: the decisive half the raw numbers lacked — "review rounds 123 h" becomes "4.1× the 30 h
  median", and "most of it went to review" becomes "this item is the outlier". A per-view toggle
  hides the contrast when it is not wanted.

### Trend (time series, prd/50)
- **Definition**: one headline metric evaluated **per bucket** — a calendar week (Monday 00:00 UTC, the
  same alignment the event histogram uses) or a sprint's measured band (start → end, or start → snapshot
  while active).
- **Derivation**: **no new metric**. Each point calls the same function the window figure comes from
  (`rules.flow_efficiency`, `rules.cycle_time`, `jira.sprint_delivery`, …) on the bucket's own window,
  with the same cohort rule ("items created in the window") and the same calendar. The point's `start` /
  `end` are the evaluation window (the first/last bucket is clipped to the analysis window); `bucket_start`
  is the calendar edge. The bucket containing the snapshot is marked **in progress** — its cohort is
  still accumulating, so its value is provisional, not hidden; an active sprint's bucket is in progress
  for the same reason.
- **Purpose**: "is flow efficiency improving week over week?" and "did delivery get better after the
  review-SLA change?" — a trend must be a sequence of the *same* number, never a second definition.

### Sprint "done" / scope / carry-over
- **Definition**: a story is *done in sprint S* if it reached `done` **by S's end** (not "by now")
  **and no PR it names is still open**. A story whose Jira status is Done while one of its PRs is open
  is reported as `unmerged` (the same fact as §9 "Done in Jira, PR open") and is **not** counted as
  delivered; it is not a carry-over either (it was never re-committed), so the sprint shows the three
  delivered outcomes plus `unmerged` rather than silently trusting the workflow label.
  Scope-in = committed at S start; scope-out = never committed or removed to backlog; *carried over* =
  committed to a **closed** sprint but not done by its close — it finished late, or is still open. An
  explicit `sprint_added` re-add into a second sprint is reported separately (`explicit_readd`), never
  as the only carry-over.
- **Derivation**: `sprint_added`/`sprint_removed` + the `done` boundary, compared to S's start/end.
  Each story's sprint outcome is one of `by_end`, `late`, `never`; carry-over's wait is the business
  hours from the first missed close to `done` (or the snapshot while still open).
- **Purpose**: the sprint lens answers "did S deliver what it committed", so "done" must be
  measured at S's close, and "0 carried over" must not hide the stories that finished late. *Fix
  target: "27 done" counted by today, not by S2's end (really 15/30).*
- **Window**: a carry-over count is over the **stories created in the chosen timeframe** (the sprint
  lens counts every story in the store). Both are correct for their scope, so the window travels with
  the number — two views must not show different counts for "carried over" without stating it.

### Constraint ranking (delivery/queue)
- **Definition**: open items ranked by who they wait on and the wait-hours that costs.
- **Derivation**: per §2 constraint; **bots excluded, sentinel named**; a "mean wait N d" must use the
  §1 "d" (8 business hours = one working day).

### Rules / suggestions
- **Definition**: the ranked "speed up delivery" suggestions, each with its evidence count and the
  items behind it (drill-down).
- **Purpose**: answer "what should we change first". Each rule's evidence must agree with the other
  views' numbers for the same concept (a "smaller PRs" rule must not contradict the policy-lab's
  size-cap scenario — if they disagree, the metric they use is different and must be named).
- **Policy-lab cross-check (prd/43)**: a rule whose *primary advice* is one of the lab's preset
  policies carries the lab's own verdict in `policy_lab.note` — a policy the model makes slower says
  **"the policy lab models this at +N % cycle — not a measured win"**, a within-noise result says "no
  clear effect", and a supported one is quoted as the model's percentage. The payload's
  `policy_lab.note` names **the lab's best scenario** (the one-line cross-reference on the Rules page).
  The note is computed from the same `/api/simulate` comparison the Policy page shows, never a second
  number that could drift.

### The rule metrics (one definition each, used identically by the rule and any view that quotes it)

| metric | what it measures | derivation |
|---|---|---|
| **rework loops** | review rounds per PR, by push class | §Review round; `content`/`rebase_conflict` pushes and non-push unapprovals |
| **comment tax** | review effort wasted on rejected/walked-back threads | `thread_outcome ∈ {rejected, walked_back}` ÷ threads |
| **conformance** | merged with 0 approvals; PRs over N rounds | approvals = 0; rounds > MAX_REVIEW_ROUNDS (3) |
| **survival** | time-to-merge (Kaplan–Meier) | full population, open/declined **right-censored**; not the merged-only median |
| **stacked cascade** | stacked PRs and the rebase cascade when a lower PR merges | ref digests + DAG over PRs |
| **sentinel queue** | the sentinel's wait vs its target | median sentinel wait vs 24 h target |
| **vacation blocking** | wait caused by a person's absence | calendar overrides/vacations overlapping a wait |
| **rebase waste** | rebase effort that produced no new review value | `rebase_conflict` pushes |
| **PR size** | PRs over the size caps | > the tenant's commit cap or changed-line cap (defaults 10 commits / 400 changed lines; prd/39) |
| **point churn** | stories re-pointed after work started | `pointed` events after `started` |
| **worklog cross-check** | logged hours vs active hours (and the log delay) | worklogs ÷ active hours; `log_delay_bucket` = created−started gap |
| **carry-over** | stories committed to a closed sprint but not done by its close | §Sprint carry-over |
| **stale PR** | PRs open past N business days, or ticket Done with the PR open | open age > 5 business days, or `done` ticket + open PR |
| **sentinel tail** | items whose only holder is the sentinel, past a threshold | sole-holder episode > 24 business hours |
| **sprint on-time** | done-by-end ÷ committed, per closed sprint | §Sprint "done" |
| **cycle-time censoring** | the merged-only median vs the censored Kaplan–Meier | survival metric, surfaced next to the median |

Each metric that measures items reports them (`metrics.<name>.rows`) so the drill-down lists the actual
PRs/stories behind the number, **sorted by impact** (wait-hours descending), not by key.

### Simulation concepts

- **Baseline**: the calibrated model replaying the observed history; the validation gate requires its
  predicted median cycle to match the measured history within ±20 % before any scenario is shown.
- **Scenario / policy knob**: a what-if change — `sentinels` (mandatory-approval resources),
  `review_sla_hours` (escalate reviews queued past N h), WIP limit, PR size cap. The output is a
  **predicted distribution**, never a measurement; the UI says so.
- **Utilisation / ρ / "busy"**: the calendar share a resource is occupied. *Different* from "overloaded"
  (queue wait) — §2. `SERVICE_SHARE`/`QUEUE_RECOVERY`/`ATTENTION_MAX_SIGMA` are the disclosed calibration
  constants, not hidden knobs.

---

## 7. Simulation

The policy lab (prd/23): a validated SimPy model of the delivery pipeline. It answers "what would this
change do", with honesty gates — the baseline must reproduce history within ±20 %, a scenario is labelled
a prediction, and a "no clear effect" result is stated rather than dressed up. Scenarios change
utilisation (ρ) and therefore queue; lead time and cycle time are reported **separately** (§1).

---

## 8. Closed vocabularies (taxonomy.py — the only labels that cross into the store)

The pseudonymisation contract (§10) means every classification is a projection onto one of these finite
sets; "other" is the explicit "could not decide", never free text.

| vocabulary | values |
|---|---|
| activity | `created`, `status_changed`, `assigned`, `pointed`, `sprint_added`, `sprint_removed`, `comment_added`, `worklogged` (Jira) · `pr_opened`, `pr_pushed`, `pr_approved`, `pr_unapproved`, `pr_needs_work`, `pr_merged`, `pr_declined`, `reviewer_changed`, `build_status` (PR) · `question_raised`, `question_answered`, `rebased` (cross-source) |
| status stage | backlog, ready, in_progress, in_review, in_verification, done, other |
| resolution | done, wont_do, duplicate, other (an unresolved story is `open`) |
| log delay bucket | same_day, next_day, within_week, late |
| summary class | feature, fix, refactor, test, docs, chore, spike, other |
| comment type | blocker, suggestion, question, nit, out_of_scope, optimization |
| thread outcome | acted_on, rejected, walked_back, unresolved |
| push class | content, pure_rebase, rebase_conflict, other |
| build result | success, failed, in_progress, other (`build_status`; `success`/`failed` clear the `ci` flag) |
| unapprove cause | push, manual |
| object state | open, merged, declined |
| actor kind | bot, human |
| object types | PR, COMMIT, THREAD, PRREV, PERSON, STORY, TICKET, EPIC, SPRINT |

---

## 9. Second-order metrics (prd/42)

These are comparisons, not new units. Each names its cohort and its basis wherever it is shown.

### Response time
- **Definition**: the business hours from a person *being added as a reviewer* to their first response —
  an approval, needs-work, unapproval or comment. A person asked twice and answering once has one sample
  and one `never`; `never` is not a zero. For a **sentinel** the clock opens at the **last peer
  answer** — any response, not only an approval (their ball only arrives then, `sentinel_after_peers` §2),
  so the peer phase is not charged to them.
- **Derivation**: `ballcourt.response_times` reads the `reviewer_changed` add events and the first
  responder action by that person, priced on the responder's own calendar. An unanswered request keeps
  its age (`never_oldest_hours`), so a forgotten orphan is a quantity, not just a count.
- **Purpose**: answer "who is the constraint" by response, next to the queue-wait ranking (which answers
  "who holds the most wait"). The two are different metrics and are labelled as such.

### Cohort benchmark
- **Definition**: a story's rank among the store's **completed** story cycles: rank 1 is the *fastest*;
  "13th of 87 fastest" means 12 stories were faster. The same-points median is the cohort of completed
  stories with the same estimate.
- **Derivation**: every story with created and done boundaries, business hours on the story's own
  calendar. An open story is censored and has no completed rank (mixing the two changed the cohort from
  87 to 100 and moved every rank).
- **Purpose**: read a cycle ("73 h") against its cohort instead of in a vacuum.

### Done in Jira, PR open
- **Definition**: an *open* PR whose ticket has a final done boundary. `opened after the ticket closed`
  is the stronger case: the PR was opened after Jira closed the ticket, so the work cannot have been part
  of the delivered story.
- **Derivation**: `tickets.done_but_open` joins open PRs to `jira.done_keys`; `waiting_on` is the delivery
  row's holder set.
- **Purpose**: the first-class list of finished work the team cannot deliver or count.

### Scope added after start
- **Definition**: a story added to a sprint after the sprint's measured start without the store's
  `at_start` membership marker. It was not part of the commitment the sprint was planned with.
- **Derivation**: `sprint_added` events compared with the band's start.
- **Purpose**: separate "committed" from "planned", so a delivery share is not read as a broken promise
  the team never made.

### Carry-over destination
- **Definition**: for a story committed to a closed sprint but not done by its close, the **next** sprint
  (by band start) and whether the story reappeared in it. `never reappears` means it was silently
  dropped, not re-committed.
- **Derivation**: the story's membership list and the sprint bands' order.
- **Purpose**: complete the carry-over fact — it is not only "how many", but "where did they go".

### PR-size correlation
- **Definition**: Pearson `r` between a PR's size (commits or changed lines) and its cycle, over **closed**
  PRs. Review rounds are reported next to it because they explain the cycle far better in the QA data.
- **Purpose**: keep "cap PRs" from being read as a proven lever when the data says size is weak.

### Epic
- **Definition**: a parent grouping of stories, not a work item. It has no created → done cycle; its
  children do.
- **Derivation**: `EPIC` objects and the stories whose `epic_key` names them.
- **Purpose**: the epic page states it is not a story and lists the children, instead of rendering
  "0/9 segments" as if the ticket were broken.

---

## 10. Data model & pseudonymisation (prd/02 — non-negotiable)

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
- **This file is enforced, not just referenced.** `tests/test_conformance.py` is the conformance test
  (prd/45): it fails when a business duration divides by 24 (a day is `BUSINESS_HOURS_PER_DAY`), when a
  duration string is built outside the shared formatters (`app/format.js` `fmtDuration` /
  `fmtCalendarDuration`, `analysis/rules.py` `_human_hours`), or when a stale segment/status label
  reappears. The JS half of the same guard lives in `app/test/units.test.mjs`. Amend this file *and*
  that test together.

# Review model — adversarial analysis of "single ball + pending set" (A) vs "one ball per actor" (B)

*Design analysis only. No source was modified. Every number below was produced by replaying either
`analysis/ballcourt.py` itself (`_timeline` / `_state_after`, model A as shipped) or a scratch re-implementation
of the alternatives, in `/tmp/opencode/review-model/` (`engine.py`, `scen.py`, `cal.py`, `compare2.py`, `stats.py`).
Corpora: `data/del-combined.duckdb` (131 PRs, 106 merged — a seeded corpus, every merge has exactly 2
approvers, no `needs_work`, 17 declined zombies closed by a bot) and `data/megaspeed.duckdb` (43 PRs, 29 merged —
the realistic one: 12 `needs_work`, variable approver counts). "merged PRs" = `state == merged`; hours are
wall-clock (no calendar) unless a section says otherwise.*

## TL;DR

* **B is not a different model; it is A's data model with the freeze removed.** "N reviewer balls + 1 author
  ball" is exactly `(author_open: bool, pending: set)` — the pending set that `_state_after` already keeps *is* a
  set of per-reviewer balls. B's N+1 balls are not an explosion in the data model; the only things B really
  changes are (i) the freeze (`pr_needs_work` → author, pending not charged) and (ii) the single-valued
  `ball`/`phase`/`kind`/`by` fields every consumer reads.
* **The freeze is the smallest of A's problems.** Measured: reviewers frozen while the author held the ball are
  **8 % (del-combined) / 22 % (megaspeed)** of person-hours. Bigger, and *shared by B as specified*:
  1. a push re-pends the whole roster, including approvers whose approval **still stands** and including pure
     rebases — **9 % / 15 %** of A's person-hours are charged to someone holding a standing approval;
  2. "everyone approved, nobody merged" is booked as **review wait** (`ball=review`, empty pending → falls back
     to the synthetic `review` queue): **17 % / 44 %** of all review-ball wall hours;
  3. a push-induced `pr_unapproved` (`cause=push`) hands the ball to the **author** who just pushed
     (48 h / 146 h of author-ball hours are this artefact).
* **B's union semantics cannot answer "who is the constraint".** Parallel obligations are an AND gate; the
  constraint is the *last finisher*, not the sum. B needs critical-path/solo attribution on top, and that is
  computable from the same bitmask — i.e. from the middle ground.
* **Recommendation: neither A nor B-literal — the middle ground "C": a participant-status vector + two independent
  dimensions (author debt, reviewer pending) + an explicit `ready` state, priced per holder, aggregated per
  wait *episode*.** It fixes everything in the table below that B fixes, plus the things B does not (standing
  approvals, merge-ready, push-induced unapproval, per-holder calendar, episode fragmentation), at roughly
  the cost of editing `_state_after` and `_holders` and adding two fields to `Interval`.

---

## 1. The two formalisations

Notation: `R` = roster (reviewers asked), `P ⊆ R` = pending, `a` = author, `s` = sentinel (a reviewer with
`attributes.sentinel`, tracked in `BallCourt.sentinels`). Events are the closed vocabulary
(`pr_opened, pr_pushed, rebased, pr_approved, pr_unapproved, pr_needs_work, comment_added{severity}, reviewer_changed{added|removed|reviewed}, build_status, pr_merged/declined`).

### 1.1 Model A (as shipped — `ballcourt._state_after`)

State: `S = (ball, by, kind, since, R, P)`, `ball ∈ {review, author, CI}` (`'calendar'` and `'sentinel'`
exist as constants/branches but **no transition ever produces them** — dead code in `_holders`, `_person_for`,
`waited_on`).

| event | `R`, `P` | `ball` |
|---|---|---|
| `pr_opened`, `pr_pushed`, `rebased` | `P := R` (everyone, **push class ignored**, standing approvals ignored) | `review` |
| `pr_approved` (r) | `R += r`, `P -= r` | **`review`, unconditionally** |
| `pr_needs_work` / `pr_unapproved` (r) | `P -= r` | `author` (**also for `cause=push`**) |
| `reviewer_changed` added | `R, P += people` | **`review`, unconditionally** (the known edge case 2) |
| `reviewer_changed` removed | `R, P -= people` | `review` |
| `reviewer_changed` reviewed (r) | `R += r`, `P -= r` | **`review`, unconditionally** (not in the known list) |
| `comment_added` by author | `P := R` | `review` |
| `comment_added` by r, `mandatory` | – | `author` |
| `comment_added` by r, suggestion | – | unchanged |
| `build_status` | – | **`CI`, and nothing ever leaves CI except the next reviewer/author event** |

"Blocked by X" in A, per interval `[since, next_event)` — `_holders`:

* `ball == author` → `{author}`; `ball ∈ {CI, calendar}` → `∅`;
* `ball == review` → `P` **∪ {actor of the next event, if it is a reviewer-ish action or a comment}**
  (the "responder retrofit": it adds whoever acts next even if they were never on the roster — **including a bot
  that closes the PR**; del-combined: **9 549 person-hours are charged to bots** this way, mostly zombie PRs
  declined by `BiBi`);
* empty `P` and no responder → `holders` falls back to `waited_on = 'review'` (the synthetic queue).

Derived answers:

| question | A's answer |
|---|---|
| who is the constraint | `blocking_graph(level=person)`/`queue_series`: Σ over intervals of `interval.hours` for each `holders` member (person-hours); `level=queue`: one `review` wait per PR |
| where did the wait go | `phase` per interval (`waiting/active/review/ci/rebasing`) — **one** label, from `_phase_for` |
| how long | `Interval.hours` — **one** duration per interval, priced on `waited_on` = the *responder's* calendar (`load()`: `calendar.business_hours(interval.waited_on, …)`) |
| review rounds | **not derived from the ball at all**: `rules._rounds` = `#pr_pushed + #pr_unapproved`; `jira.py:597` = same, after first review |
| responsiveness | `rules.reviewer_responsiveness`: one sample per *interval* per holder, median per person |
| sentinel queue | `rules.sentinel_queue`: same per-interval samples, filtered to `court.sentinels` |

### 1.2 Model B (proposed — one ball per actor)

State per PR: a ball `b_x ∈ {open(since_x, by_x), closed}` for each `x ∈ {a} ∪ R ∪ {s}`. PR-level ball =
`{x : b_x open}` (union). My formal reading of the proposal:

| event | effect |
|---|---|
| `pr_opened` | `b_a` closed; `b_r` open ∀r∈R |
| reviewer `added` r | `b_r` opens (**independent of author state** — fixes edge case 2 by construction) |
| r approves | `b_r` closes |
| r needs-work / mandatory comment | `b_r` closes, **`b_a` opens** (`by = r`); other `b_x` untouched (no freeze) |
| author push / reply | `b_a` closes; "a push resets the reviewers' balls": `b_r` reopens ∀r (**this is the clause to attack**) |
| merge | all close |

"Blocked by X" = "the time slice where merging is blocked by X's own (in)action" = `b_x` open. Person-hours
`= Σ_x |b_x|`; wall-clock PR wait = `|⋃_x b_x|`.

**Representation theorem (the key observation).** Map B's state to `(debt = [b_a open], P = {r : b_r open})`.
That *is* A's `(ball, P)` with the `ball` enum replaced by the bit `debt` and the freeze deleted. Conversely, A's
`ball` is a function of `(debt, P)` except where A flips it wrongly. So B's "N+1 balls" costs **N+1 bits per
interval**, not N+1 timelines; interval count stays `≤ events + 1`. Any cost B adds sits in the *consumers* of
`ball`, not in the state.

Derived answers for B as proposed: constraint = ranking by `|b_x|`; wait = `|⋃ b_x|`; person-hours =
`Σ|b_x|` (so a PR-level concurrency factor `Σ|b_x| / |⋃ b_x|` falls out for free); rounds = unspecified
(natural: "number of times a reviewer ball reopened"); responsiveness = `|b_r|` episodes.

---

## 2. Adversarial scenario matrix

Legend for hours: `A` = shipped `_timeline` output (actually executed). `B-lit` = B with "push reopens every
reviewer" taken literally (plus the `cause=push` special-case, otherwise B inherits A's S2a bug). `C` = the
middle ground defined in §4. Times are hours from `pr_opened`; `RA…RD` reviewers, `AU` author.

### S1 — 3 reviewers; A approves, B needs-work, C silent; author fixes; B approves; D added mid-author-work

Events: open+roster{RA,RB,RC} t0; RA ✔ t2; RB ✘(needs-work) t5; **RD added t7 (author still fixing)**;
AU pushes t9; RB ✔ t12; RC ✔ t30; RD ✔ t31; merge t40.

| | RA | RB | RC | RD | author | ready/merge |
|---|---|---|---|---|---|---|
| **A** | **33** | 8 | 28 | 24 | 2 | – |
| **B-lit** | **33** | 8 | 30 | 24 | 4 | – |
| **C** | 2 | 8 | 30 | 24 | 4 | 9 |

A's trace: 5–7 `ball=author` (RC frozen: 2 h under-charged, RC & RD hidden); 7–9 `ball=review` with holders
`{RC,RD}` while the phase is `active` (author mid-fix — edge case 2: label says review, phase says active, author
hours understated); at 9 the push re-pends `{RA,RB,RC,RD}` and RA — who approved at t2 and whose approval stands —
is charged **31 h more**, including 31–40 where he is the *only* holder of a PR that is actually ready to merge.

**Verdict: A wrong, B-literal also wrong (same RA bug, same missing ready), C right.** A names **RA — the
first reviewer to approve — as the constraint (33 h)**; the silent reviewer RC is 28 h. B-literal fixes
the freeze and the edge case but keeps `RA = 33`. *The headline answer ("who is the constraint") is wrong in both A
and B-literal for the same reason, which is not the freeze.*

Critical-path read (what actually delayed the merge): RC's ball ran 0→30, but RD (added at 7) ran 7→31, so the PR
was held by RD from 30 to 31; had RC answered instantly the merge would not have moved (RD blocks to 31), whereas
had RD answered instantly the merge would have been ≥1 h earlier. **"RC is the constraint" and "RD is the
constraint" are both defensible depending on the question (latency vs. marginal delay) — no ball topology answers
that; see §3.1.**

### S1b — B needs-work, then A approves while the author is still fixing

Events: roster{RA,RB,RC} t0; RB ✘ t2; **RA ✔ t4**; AU push t20; RB ✔ t22; RC ✔ t23; merge t24.

| | RA | RB | RC | author |
|---|---|---|---|---|
| A | 6 | 4 | 21 | **2** |
| C | 4 | 4 | 23 | **18** |

`pr_approved` moves the ball to `review` unconditionally, so *any* approval from anyone cancels the author's
needs-work debt: A says the author held the ball for 2 h, the truth is 18 h (RB's objection stood until t20).
**Measured on megaspeed: 26 of 32 author-ball (`needs_work`/`blocker`) intervals are ended by a `pr_approved`, not
by an author event** (follow-on review intervals totalling 3 739 h). The author-ball is systematically cut short.
**A wrong; B right.** (B only needs the one-line "approve closes `b_r`, doesn't touch `b_a`".)

### S2a — an early approver is re-required after a rebase (approvals reset)

Events: RA ✔ t1; AU pushes (pure_rebase, `approvals_lost=true`) t10 and the store emits `pr_unapproved
cause=push` by RA at the same instant; RA re-approves t20; RS ✔ t21; merge t22.

| | RA | RS | author | queue |
|---|---|---|---|---|
| **A** | **1** | 11 | **10** | 1 |
| B-lit (with `cause=push` special case) | 11 | 21 | 0 | – |
| C | 11 | 21 | 0 | – |

The push (10) re-pends everyone, the zero-length review interval is dropped (`event["_ts"] > current["since"]`
is false), then the `pr_unapproved` flips the ball to `author` for 10 h. **A charges the author for the 10 h in
which RA owed a re-approval; RA's 10 h vanish.** Data: this exact artefact is 24/24 unapprovals in del-combined and
35/35 in megaspeed (all `cause=push`) → **48 h / 146 h of author-ball on merged PRs (7.6 % and 27 %)** are the author being billed
for the reviewer's wait.
**A wrong. B right only if `cause=push` is special-cased** — the literal event rule "a reviewer who acts closes
their ball; unapproval opens the author's" reproduces A's error. Both models need the same `cause`-aware branch.

### S2b — rebase, approvals retained (`approvals_lost=false`), merge 40 h later

Events: RA ✔ t1, RS ✔ t2, AU pure_rebase push t10, merge t50.

| | RA | RS | queue | merge-ready |
|---|---|---|---|---|
| **A** | **41** | **42** | 8 | – |
| B-literal (reset incl. rebase) | 41 | 42 | – | – |
| C (reset only verdicts invalidated) | 1 | 2 | – | 48 |

Both reviewers hold approvals the push did not remove; A re-pends them for the rest of the PR life (they can
never "answer" — Bitbucket has no second approve event), and the 8 h before the push are unowned (`queue`).
`pure_rebase` is documented (`concepts.md` §5 "Push class") as *a head move, not a re-review*, yet
`_state_after` treats it like new code. **A wrong; B-literal wrong; C right.** Measured over merged PRs:
**9 % (del-combined) and 15 % (megaspeed) of all A person-hours are charged to a reviewer whose approval was
standing at that moment.** (And 26 + 3 pure-rebase pushes after first review in the two corpora are counted as
review rounds by `_rounds` — see §5.)

### S3 — sentinel (mandatory, always last) waits while the author is also reworking

Events: roster{RP, RS(sentinel)} t0; RP ✘ t4; AU push t10; RP ✔ t12; RS ✔ t15; merge t16.

| | RP | RS | author |
|---|---|---|---|
| **A** | 6 | 9 | 6 |
| **B-lit / C (sentinel = ordinary reviewer)** | 6 | **15** | 6 |
| B/C + `sentinel_after_peers` gate | 6 | **3** | 6 |

* In A the sentinel is charged 0–4 (before the peer has even looked) and 10–15; frozen 4–10.
* In B/C-without-gating he is charged 0–15 — including 4–10 *concurrently with the author fixing* — which is
  precisely "is the sentinel blocking concurrently?": B answers **yes, always**, A answers "yes before the
  first needs-work, no during it". Neither can express the policy "the sentinel only acts after the peers".
* The log can't tell policy from habit — but the data is suggestive: in del-combined the sentinel approved
  **last on 107/107** multi-approver PRs; in megaspeed on 11/23. **63 % (5 790 of 9 250 h) of the sentinel's pending hours
  in del-combined, 45 % in megaspeed (297/657), are concurrent with an un-answered peer.** `sentinel_queue`
  (`rules.py:1309`) therefore may be pricing time the sentinel was not expected to act.
* The mandatory-last rule is a **precedence constraint** (`b_s` opens only when all other `b_r` are closed): a DAG,
  not a union. **B (a union of independent balls) cannot express it either; it needs a per-actor `after:` gate.**

**Verdict: both wrong without a `sentinel_gated` config; B is *not* the fix, a gate is.** Hedge: if there is no
precedence policy, B/C's "blocked concurrently" is the more truthful answer.

### S4 — CI/build gating while a reviewer is pending

Events: roster{RA} t0; `build_status` t1 and t1.5 (green); RA ✔ t20; merge t21.

A: `0–1 review{RA}`, **`1–20 ball=CI`** (19 h), then `queue`. `build_status` sets `ball=CI`, and **nothing resets
it when the build finishes** — the only exits are the next reviewer/author event. A silent reviewer's 19 h are
booked to "CI" (and `holders = ∅`, so no person is charged and `reviewer_responsiveness` drops the wait).
**A wrong — and structurally unable to be right:** CI and review genuinely run *concurrently* (CI running while
RA is pending is the normal case); a mutually-exclusive enum cannot say "CI running **and** RA pending".
**B right** (CI = its own actor ball, closed by the build result). Caveat that deflates this: the DC activity
map (`taxonomy.PR_ACTIVITY_MAP`) emits **no `build_status` events at all** — both real stores have 0 — so this is a
latent landmine, not a current distortion. It is the one scenario where *concurrency itself* (not the freeze)
is the argument for B's shape; the right shape is "CI is a flag in the mask", not "CI is a ball in a union of
people".

### S5 — reviewer added while the ball is with the author (known edge case 2)

Events: RA ✘ t2; **RB added t3**; AU pushes t30; RA ✔ t32; RB ✔ t33; merge t34.

| | RA | RB | author | queue |
|---|---|---|---|---|
| **A** | 4 | **30** | **1** | 1 |
| B-lit / C | 4 | 30 | **28** | – |

A flips to `review{RB}` at t3: **author-held time 3–30 is relabelled "review" with RB charged 27 h**, the phase is
simultaneously `active`, the author is credited 1 h of a 28 h fix, and RB becomes the constraint. prd/34's proposed
fix (added reviewer joins `P`, ball stays with the author, charge starts at the push) trades this for the freeze
(RB charged 0 until t30 → RB 4 h) — a *third* answer. B/C charge RB 27 h concurrently (RB "owes a review" but was
never expected to look at code the author is about to change).

**Verdict: A wrong. B/C better, but B's "RB is blocking from t3" is a stretch — RB cannot meaningfully act before
the push; the truthful split is author 28 h *critical*, RB 27 h *slack*.** This is the best scenario for the
critical-path framing (§3.1), and the one where B's union ("in work AND in review") is *true but not useful*.

### S6 — reviewer "reviewed" but didn't approve

Events: roster{RA,RB}; RB `reviewed` t5; RA ✔ t6; merge t40.

`reviewed` removes RB from `P` and (A) flips the ball to review unconditionally; neither model knows whether
`reviewed` means "no objection", "comment only" or "needs follow-up" (DC's REVIEWED is a status with no verdict).
Both treat him as answered; **t6–t40 is `(queue)` 34 h in A** (`final approval → close`) vs `ready` in C — a merge
latency booked as review wait. **Both wrong in the same way on the ambiguity; A additionally wrong on the 34 h.**
Open question for the user: is a `reviewed`-without-verdict reviewer "answered" (current) or "still owes a verdict"?

### S7 — optional reviewer who never answers

Events: roster{RA, RO}; RA ✔ t3; merge t4 (or t30).

A and B-lit both charge RO the whole PR (4 h / 30 h) as a blocker. The merge happened without him: **for a closed PR
a reviewer still pending at merge is provably non-blocking** (for k-of-n approvals, or optional reviewers,
that is the *normal* case). Measured: **13 of 29 merged PRs in megaspeed merged with someone still pending (4 of them with the
sentinel), carrying 1 492 person-hours — 49 % of all person-hours either model assigns on merged PRs there.**
(del-combined: 0/106, which is why that corpus can't show the problem — its synthetic merge rule is
"two approvals".) **Both wrong; B does not help.** B's definition "blocked by X's own (in)action" is *ill-defined* here:
without the merge-gate predicate (required approvals `k`, required reviewers) a ball is just an *obligation
to respond*, not a *blocker*. Ex-post (closed PRs) the fix is free: charge pending-at-merge reviewers as
"unrequired" not "blocking". Ex-ante (open PRs) it needs `min_approvals` / required-reviewer config.

### S8 — reviewer on vacation (calendar pricing)

`load()` prices the whole interval once, on `interval.waited_on` (= the *responder* — `ball_person`), then the
person-level projections reuse that single `interval.hours` for every holder. (`analysis/calendar` run with
Mon–Fri 09–17 UTC, RC on vacation Mon–Fri; executed in `cal.py`.)

* **S8a** (RB ✔ Fri 17:00 = t104; RC ✔ Mon next week): interval 0–104 priced on RB = **40 bh for each holder → RC is
  charged 40 bh while on vacation**; own calendar = 0 bh. (A *second* signal, `vacation_blocking`, then adds the
  vacation hours back separately — two views, two different prices for the same wait.)
* **S8b** (RC approves first, from a phone, on Wednesday during his vacation): interval 0–53 is priced on RC = **0 bh for
  everyone** — RB's 21 business hours in that window disappear (flow efficiency's `elapsed = Σ interval.hours`
  loses them too).

**A wrong (price depends on who answers first). B right *by construction*** — one ball, one owner, one calendar:
`|b_x|` is priced on `x`'s calendar. But the fix is not B-specific: store per-holder business seconds on the
interval (`holder_hours: {person: bh}`) and the pending-set model is equally right. Residual: the *PR-level*
union has no single calendar (price it on the default window, as the queue-level wait already is).

### S9 — person-hours coherence

A: "10 h on 2 reviewers = 10 h each, person-level total > wall-clock". B: same by definition
(`Σ|b_x| ≥ |⋃b_x|`), but B can state the equivalent claim *and its complement*:

```
Σ_x |b_x|   =  Σ_slices k(s)·len(s)          person-hours  (load, "cost of attention")
|⋃ b_x|     =  Σ_{k≥1} len(s)                wall-clock blocked time
|{k=0}|     =  PR open but no ball            merge-ready / unowned (A's `queue` fallback, un-named)
Σ/⋃         =  mean concurrent balls           the "concurrency factor"
solo_x      =  Σ_{s: b_x is the only open ball} len(s)     marginal blocking attributable to x alone
```

Coherent and strictly richer than A — but A's pending set already yields all of it from `(debt,P)`; only the
`k = 0` bucket is missing (and currently mis-booked as review wait, §2 S2b/S6). **Equal expressive power; the
difference is the missing name for `k=0`.**

### S10 — ball explosion (N reviewers → N+1 balls)

* **State/storage**: N+1 bits per interval; `≤ events+1` intervals; trivially bounded. No explosion.
* **Observed bound**: roster size ≤ 3 (del-combined: 114× 2, 17× 3), ≤ 4 (megaspeed: 23× 2, 12× 3, 2× 4). Even a
  CODEOWNERS-style 10-person roster is 11 bits.
* **Where it does bite** — all in *consumers*: (1) `Interval.ball`, `.kind`, `.by`, `.phase` are single-valued;
  callers: `blocking_graph`, `queue_series`, `delivery_list` (`ball`, `ball_label`, `waiting_for`, `waiting_since`,
  `blocked`), `tickets.py:121-126`, `stack.py:326`, `rules.py:371,670,935,1331`, `jira.py:601,1384`; (2) the
  client duplicates the state machine in `app/timeline.js:advanceState/phaseFor` — **and the JS copy has no roster
  or pending set at all** (it already can't render holders); every transition change has to land in two languages;
  (3) the gantt has one colour per instant — N+1 concurrent lanes don't fit one bar; (4) `by` (the edge origin of
  the blocking graph) is per interval, B needs it per ball; (5) test matrix: invariants over `2^N` pending subsets
  per transition rather than a handful of scenarios.
* **Aggregation hazard**: Σ over concurrent balls double-counts any "total hours" quoted next to a wall-clock
  figure; `concepts.md` §6 "At stake … must not double-count" already warns, and B makes it the default
  behaviour of every sum.

### S11 — scenarios where A and B give *different answers* (not just a re-labelling)

| scenario | A says | B says | which is truthful |
|---|---|---|---|
| S1b (approve during author debt) | author held ball 2 h | author held 18 h | **B** |
| S5 (added mid-work) | RB is the constraint, author 1 h | author 28 h, RB 27 h concurrent | **B**, with the slack caveat |
| S1 (silent RC during author work) | RC charged 2 h less (frozen 5–7) | RC charged continuously | **B for blocking; A defensible for responsiveness** (see §3.4) |
| S4 (CI + pending reviewer) | CI absorbs the reviewer's 19 h | both concurrent | **B** (latent) |
| S8 (vacation) | priced on responder | priced on own calendar | **B**, achievable in A with per-holder hours |
| S2a (push-induced unapprove) | author | reviewer | **B only if `cause` handled** |
| S1/S2b (standing approvals re-pended) | RA is the constraint | same | **neither** |
| S7 (optional reviewer) | RO blocks | RO blocks | **neither** |
| S3 (sentinel last) | sentinel blocks except during rework | blocks always | **neither** (needs gate) |
| S6 / merge wait (all answered, not merged) | `review` queue, 34 h | no ball open (k=0) — unowned | **neither names it; C names it `ready`** |

So B *does* change answers — S1b, S5 and (latent) S4/S8 — **but the three most frequent real-data distortions are
not in this column**: standing-approval re-pend (9–15 % of person-hours), merge-ready booked as review (17–44 % of
review-ball hours) and push-induced unapproval (8–27 % of author-ball hours). B-literal inherits all three.

### S12 — measurement artefacts that neither topology addresses

* **Fragmentation of waits.** `reviewer_responsiveness` (and `sentinel_queue`) append one sample *per
  interval* per holder. Every event by anyone — a suggestion comment, another reviewer's approval, a reviewer
  added — splits the interval, so one 30 h wait on RC becomes 6 samples of 0–18 h. Measured: **median per-interval
  wait = 2.96 h vs. median per-episode wait = 28.4 h in del-combined (≈10× understated)**; 2.45 h vs 3.78 h in megaspeed.
  The `waits` dict in the same function already aggregates per `(person, PR)` for the drill-down — the headline
  median does not. B makes this *worse* (more split points), C fixes it with episode aggregation.
* **Responder retrofit charges whoever acts next** (bot closers: 9 549 person-hours in del-combined at the
  person-level graph; `bots` is only filtered in the suggestion layer, `rules.py:1760-1794`).
* **`_rounds` ≠ `concepts.md` §6**: counts `pr_pushed` + `pr_unapproved` including pure rebases, pre-first-review
  pushes and push-induced unapprovals (double count). Documented definition vs `rules._rounds`:
  del-combined **188 vs 250 (+33 %)**, megaspeed **48 vs 87 (+81 %)**. `jira.py:597` is a third variant (after first
  review, but also double-counts `cause=push`). This is independent of A/B (rounds don't read the ball).

---

## 3. Attacks on B specifically

### 3.1 The union answers "who has a ball", not "who is the constraint"

Parallel obligations to merge are an **AND gate**. Counting `|b_x|` per actor ranks by *obligation duration*, not by
*delay caused*. In S5 the truthful statement is "author 28 h critical; RB 27 h slack"; in S1 "RC latency 30 h, but
the last finisher was RD". B's union says "in work AND in review" and ranks both, which is true and uninformative.
To make B useful you must add **solo/critical-path attribution** (`solo_x`, §2 S9) — computed from the same
bitmask, i.e. *not a property of having N balls*. And the critical path is only defined **ex post** (closed PRs); for an
open PR "who is the constraint right now" under an AND gate is ill-posed (each blocker is a necessary condition;
the binding one is whoever finishes last, unknown yet). A's single ball hid this by picking one; B surfaces it and
then needs a tie-breaker.

### 3.2 "Blocked by X's action" is not a property of the log

(§2 S6, S7.) Optional reviewers, k-of-n approvals, `reviewed`-without-verdict and sentinel precedence all
change *who* blocks. B's definition requires the **merge-gate predicate** (`min_approvals`, required reviewers,
sentinel-last, no-needs-work, CI-green, unresolved tasks) — which is not in the store. Without it B ≡ "obligation to
respond", which is what the pending set already is. With it, the thing that matters is the **gate**, and the "balls"
are merely its unsatisfied conjuncts; A with a gate evaluator would be just as right.
Real-data size of the problem: **13/29 merges in megaspeed finished with reviewers pending (1 492 person-hours,
49 %)**.

### 3.3 "A push resets the reviewers' balls" is A's biggest bug restated

Taken literally it re-pends standing approvals and pure rebases (§2 S1, S2b: RA 33 h under B-literal, identical to
A). The correct rule is *per verdict*: a push stales `needs_work` (and `approvals_lost`-flagged approvals); it does not
stale an approval the store says was kept. That rule is a **status vector**, not a ball topology.

### 3.4 Removing the freeze isn't free: it changes the meaning of "responsiveness"

A's freeze has a behavioural justification: a reviewer who sees "RB needs work" usually waits for the fix; charging
RC for silence on code known to change penalises the right behaviour (S1, S5: RB "owes" a review of a diff the author
is about to replace). B/C's no-freeze is the right *blocking* clock ("what delayed the merge") but the wrong
*latency* clock ("how fast does RC respond when asked"). **Two questions, two clocks** — which neither model
separates. Cheap fix: keep per-reviewer `asked_at` and `reviewable_since` (last verdict-staling push) and let
responsiveness use the latter. A single union of balls can't carry both.

### 3.5 No owner for the empty set

`k = 0` (all approved, PR open) happens for **44 % of megaspeed's / 17 % of del-combined's review-ball wall hours**
(1 494 / 3 368 h; 1 881 / 10 765 h). B-literal has no ball here; A books it as `review` + synthetic `review` queue,
which reads as "reviewers are slow". It is almost certainly the author (merge click) or an un-modelled gate
(release train, merge checks). In megaspeed *more than half* of the time a PR spends in "someone owes something"
(`C_review_wall` 1 528 h vs `merge_ready` 1 689 h) is **after the last reviewer answered**. That is the largest
single signal in the data and neither model names it until `ready` is an explicit state.

### 3.6 Cost-benefit

Beyond the data model (cheap), B requires: multi-valued `ball`/`by`/`kind`/`phase` in `Interval`; a per-ball
`since`/`by` in `delivery_list`, a new lane model in the gantt; changes in both Python and `app/timeline.js`; a
rewrite of each `ball == REVIEW` / `phase` consumer. For answers that, outside S1b/S5/S8, are not different. And
because `_holders` already returns a set and `delivery_list` already exports `waiting_on: [...]`, the API *already
speaks multi-holder*; B only adds "author ∈ waiting_on".

### 3.7 When "in work AND in review" *is* useful

* S5/S1b: the author has an open objection **and** a reviewer hasn't looked: the PR page should say both
  ("needs rework by AU · RD not yet reviewed") — a *display* fact.
* CI + review (S4): genuinely concurrent processes.
* A "PR is doubly blocked" flag: debt ∧ P≠∅ ⇒ fixing one doesn't unblock the PR.
That is an *annotation* (one extra boolean/chip), not a different ownership model.

---

## 4. The middle ground ("C"): status vector + two dimensions + `ready`

Keep `Interval.ball` as the *primary label*, but derive it from an honest state instead of from event flips.

**State** per PR: `status[r] ∈ {none, approved, needs_work, reviewed}` per roster member (Bitbucket's participant
status, which is what the store's events really are), `debt ⊆ R` (reviewers whose objection — `needs_work`,
manual `unapproved`, `mandatory` comment — still stands), plus `since`.

**Transitions** (no ball flipping):

* approve r → `status[r]=approved`, `debt −= r`; **does not touch the ball of anyone else**;
* `needs_work`/mandatory/manual-unapprove r → `debt += r`;
* `unapproved cause=push` → `status[r]=none` (r owes), **author debt untouched**;
* **push** → `debt := ∅`; `status[r]=none` for `needs_work`/`reviewed` reviewers; for **all** reviewers iff
  `approvals_lost` (already in the push attributes); *pure_rebase without loss: no change*;
* author reply → `debt := ∅`, `needs_work`/`reviewed` → `none`;
* `added` → `status[r]=none` (pending; does not move anything), `removed` → drop.

**Derived** (not stored): `author_open = debt ≠ ∅`; `P = {r : status[r] = none}`;
`ready = ¬author_open ∧ P = ∅ ∧ R ≠ ∅` — **an explicit state held by the author** (the merge click) distinct from author
*debt*; `doubly_blocked = author_open ∧ P ≠ ∅`.
`ball = author` if `author_open`; else `review` if `P ≠ ∅`; else `ready`. `holders` = `{author}∩debt ∪ P` — **both
sets always reported** (so `ball` is just a priority-ordered label; both holders are in `holders`).

**Per-holder pricing**: `Interval.holder_hours = {person: business hours on *that person's* calendar}`;
`Interval.hours` stays the default-window figure for the role/queue level (fixes S8 without B).

**Aggregation**: per-`(person, PR)` *episodes* (consecutive intervals where the person is a holder), not per
interval, for responsiveness/sentinel medians (S12). Add `solo_hours` (where only that holder's bit is set) next
to total hours for the constraint ranking (§3.1). Ex-post tag: reviewers still pending at merge are `unrequired`
(listed, not blamed).

**Optional, config-driven** (not required for the core fix): `min_approvals` (k-of-n gate) and `sentinel_after_peers`
(precedence) so open-PR "who is blocking" drops optional reviewers/sentinel-early; `reviewable_since` for the
responsiveness clock (§3.4); CI as a flag in the mask rather than a ball (`CI` bit set by `build_status`, cleared by
the build result — and **never exclusive**).

This is *the same data* as B (author bit + N reviewer bits) but with Bitbucket semantics for what resets a bit, one
extra named state (`ready`), per-holder price and episode aggregation. Everything B fixes (S1b, S5, S8, S4) is fixed
here; so is everything B-literal inherits (S1/S2a/S2b/S6) — and it is a strict subset of B's UI/API surface
(no multi-lane gantt, no per-ball `by`: the blocking-graph `by` can be the actor that *last changed* the mask).

Quantified effect on the two corpora (merged PRs, wall-clock; "C" = scratch `engine.replay`):

| | del-combined A → C | megaspeed A → C |
|---|---|---|
| person-hours (review) | 15 161 → 15 040 (−1 %; offsetting errors) | 2 472 → 2 133 (−14 %) |
| person-hours charged to standing approvals | 1 380 (9 %) → 0 | 371 (15 %) → 0 |
| person-hours pending *during* author debt (the freeze) | not charged → 1 163 (7.7 %) | not charged → 464 (22 %) |
| author-ball / debt wall hours | 630 → 581 | 535 → 491 |
| `ready` (all gates satisfied, not merged) | hidden in review → **1 340 h** | hidden in review → **1 689 h** |
| review-ball wall hours (someone owes) | 10 765 → 9 250 | 3 368 → 1 528 (**−55 %**) |

(The near-zero net change in del-combined's person-hours is two large opposing errors cancelling — the
freeze under-count and the standing-approval over-count — which is the strongest argument that the A-vs-B debate on
the freeze alone is the wrong frame.)

---

## 5. Recommendation and consequences

**Adopt C. Do not adopt B as specified.** Reasons, in order:

1. **B ≡ A minus the freeze** (representation theorem, §1.2). Delivering "multiple balls" buys the freeze fix
   (S1b/S5) — worth 8–22 % of person-hours — and nothing else; the three largest distortions (standing-approval re-pend 9–15 %,
   merge-ready booked as review 17–44 %, push-induced unapproval 8–27 % of author-ball hours) are inherited by B-literal
   verbatim.
2. **A is mostly *fine in shape*** — the pending set was the right insight. It is wrong in *transitions* (§1.1:
   approve/added/reviewed/unapproval flip the ball; push ignores class and standing approvals; `ready` unnamed;
   CI exclusive) and in *pricing/aggregation* (responder-priced, per-interval samples). All of that is fixable
   without changing the representation.
3. **B's union cannot answer the constraint question** (§3.1) without solo/critical-path attribution, which is
   a mask computation, not a topology.
4. B's cost is paid in the consumers (single-valued `ball`/`phase`, duplicated JS state machine, gantt lanes) for
   answers the pending set already encodes.

**When B would win**: if CI/builds, required-checks and merge gates are ingested and need to be shown concurrently with
people (S4) — then "a flag per gate in a mask" (C's extension) generalises to B's N+1 balls and the
representation is already ready. B is the *destination* of C if the gate list grows; it is not a prerequisite.

### Concrete consequences of adopting C

**Data model (`analysis/ballcourt.py`)**
* `_state_after`: replace the `P`-by-event bookkeeping with `status[r]`/`debt`; remove `ball` flips on
  `pr_approved`, `reviewer_changed`, `pr_unapproved(cause=push)`; read `push_class` and `approvals_lost`;
  add `ready`; delete dead `'sentinel'/'calendar'` branches or implement them (calendar ball: only if a
  vacation-delegation signal is wanted — `vacation_blocking` currently covers it).
* `Interval` gains `author_open: bool`, `holder_hours: dict[str, float]`, `ready: bool` (or `ball='ready'`); `ball`
  becomes a derived priority label for back-compat; `holders` returns author ∪ P.
* `_holders`: drop the responder retrofit for non-roster actors, or restrict it to human, non-closing actors (bot
  charged 9 549 person-hours today).
* `load()`: price per holder (`holder_hours`) instead of once on `waited_on`.
* `build_status`: set a `ci` flag, not `ball=CI`; clear it on the build result (needed *before* any DC build-status
  ingestion lands).

**Analysis / metrics**

| metric | change |
|---|---|
| **constraint** (`blocking_graph`, `queue_series`, `delivery_list`) | author joins `waiting_on` while `debt`; standing approvers leave; `ready` becomes a named node/queue (authors), `solo_hours` beside total; role/queue level counts a PR once per slice; person-hours stay explicitly person-hours |
| **wait hours** (role level `review`) | drops by the merge-ready share (17–44 % on the corpora); `ready` is reported separately — expect Rules/Queue "review wait" to look much lower and "merge latency" to appear |
| **review rounds** | independent of the ball; fix `_rounds` to the documented definition (after first review, `pure_rebase` without loss excluded, `cause=push` unapprovals not double-counted): −25 % / −45 % in the corpora; align `jira.py:597`. Optional per-reviewer "re-reviews demanded" = number of times `status[r]` was staled |
| **responsiveness** (`reviewer_responsiveness`) | episode-level samples (≈10× median change in del-combined), approvers no longer charged for a standing approval, reviewers charged during author debt unless `reviewable_since` is used for latency (decision needed, §3.4) |
| **sentinel queue** | same episode fix; with `sentinel_after_peers` the clock starts when the last peer answers; ex-post `unrequired` when the PR merged without the sentinel (4 of 29 in megaspeed) |
| flow efficiency / `phase` | unchanged: keep `phase` as the derived priority label; the new `doubly_blocked` annotation is additive |
| vacation blocking | `person in interval.holders` stays true; hours priced per holder → can drop the second pricing |

**API / UI**
* `delivery_list` rows: `ball` (derived label, may now be `ready`), `waiting_on` (already a list; may include the
  author), `doubly_blocked: bool`, `ready_since`. No new multi-lane gantt; the PR drawer gets a one-line "needs
  rework by X · Y/Z reviewers pending · ready to merge since T". Stated `hours_kind` per aggregate (person-hours
  vs wall-hours vs solo-hours) — the payload already carries `hours_kind`.
* `app/timeline.js:advanceState/phaseFor`: either port the status-vector logic or (better) stop duplicating the
  state machine and read phases from the server's intervals; the JS copy has no roster today, so it already
  disagrees with the server.
* `concepts.md` §2 "ball in court / pending set": rewrite around "status vector + debt + ready"; drop "open
  subtlety" note; document that `ball` is a label and `holders` is the truth.

**Tests** (AGENTS.md rule 1): one regression per scenario here — S1/S1b (per-actor hours), S2a/S2b (cause/standing
approvals/pure rebase), S4 (CI flag clears), S5 (added mid-debt), S7 (pending-at-merge `unrequired`), S8 (per-holder
price), S12 (episode median), plus an invariant test `Σ_x|b_x| ≥ |⋃b_x|` and `Σ solo_x ≤ |⋃b_x|`.

### Open questions for the product owner (the model can't decide these from the log)
1. Is the sentinel an ordinary concurrent reviewer or *after* the peers? (`sentinel_after_peers`) — data says the
   sentinel is last 100 %/48 % of the time.
2. What does a `reviewed`-without-verdict mean (answered vs still owed)?
3. Is `ready`/merge latency the author's, a gate's, or a release train's? (decides whose node it is on the graph.)
4. Is *responsiveness* "since asked" (blocking clock) or "since the code was reviewable" (reviewable clock)?
5. Is `min_approvals` knowable per repo (branch permissions)? Without it, open-PR "blocking reviewers" is
   an upper bound.

### Honest limits of this analysis
* Replays are from two corpora; `del-combined` is seeded (uniform "2 approvals" merge rule, no `needs_work`,
  bot-closed zombie PRs) so its totals understate S7 and overstate S12; `megaspeed` is the more realistic one.
* `C` in the numbers is my scratch replay (`/tmp/opencode/review-model/engine.py`), not a reviewed implementation;
  figures such as "9–15 %" are with `approvals_lost`/`cause` semantics taken from the store as given.
* "Per-episode" median is an approximation (consecutive intervals with the holder present, same PR, no
  intervening author-ball interval).
* Counterfactual (critical-path) claims assume other balls' timings are unchanged and ignore reopen cascades.

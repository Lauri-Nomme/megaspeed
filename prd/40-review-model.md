# 40 — Review model: participant-status vector + author debt + `ready` (the middle ground)

Implements the recommendation of `docs/review-model-analysis.md` — neither the current single-ball state
machine (A) nor a literal "one ball per actor" union (B), but the **middle ground (C)**: an honest
per-participant status vector plus an author-debt set, from which the single `ball` label is *derived*.
Conforms to `docs/concepts.md` §2 (which this PRD also rewrites).

## Why (from the analysis, with measured sizes)

The current model's *shape* (the pending set) is right; its *transitions* are wrong. The three largest
distortions are inherited by B-literal and fixed here:

1. a push re-pends the whole roster incl. **standing approvals** and `pure_rebase` (9–15 % of person-hours);
2. "everyone approved, not merged" is booked as **review wait** (17–44 % of review-ball hours) — it must be a
   named `ready` state;
3. a push-induced `pr_unapproved` (`cause=push`) hands the ball to the **author who just pushed** (8–27 % of
   author-ball hours).

Plus the freeze (pending reviewers stop being charged while the author works — 8–22 % of person-hours),
responder-priced intervals, and per-interval (not per-episode) responsiveness samples (≈10× understated median).

## Target model (C)

**State per PR**: `status[r] ∈ {none, approved, needs_work, reviewed}` for each roster member `r` (Bitbucket's
participant status, which is what the store's events really are), plus `debt ⊆ R` (reviewers whose objection —
`needs_work`, manual `unapproved`, mandatory comment — still stands), plus `since`.

**Transitions** (no ball-flipping — only the status vector changes):

| event | effect |
|---|---|
| `pr_approved` (r) | `status[r]=approved`; `debt −= r`; touches no one else |
| `pr_needs_work` / manual `pr_unapproved` / mandatory comment (r) | `debt += r` |
| `pr_unapproved` `cause=push` (r) | `status[r]=none` (r owes); **author debt untouched** |
| `pr_pushed` / `rebased` | `debt := ∅`; `status[r]=none` for `needs_work`/`reviewed` reviewers; for **all** reviewers iff `approvals_lost` is set on the push; a `pure_rebase` with no loss → **no change** |
| `comment_added` by author | `debt := ∅`; `needs_work`/`reviewed` → `none` |
| `reviewer_changed` added (r) | `status[r]=none` (pending); **moves nothing else** |
| `reviewer_changed` removed (r) | drop r |
| `build_status` | set a `ci` flag; cleared by the build result; **never exclusive** (a reviewer can still be pending) |

**Derived, not stored**: `author_open = debt ≠ ∅`; `P = {r : status[r]=none}`;
`ready = ¬author_open ∧ P = ∅ ∧ R ≠ ∅` (an explicit "all answered, not merged" state — owned by the author's
merge click); `doubly_blocked = author_open ∧ P ≠ ∅`. The `ball` label is a **priority-ordered projection**:
`author` if `author_open`, else `review` if `P ≠ ∅`, else `ready`. `holders = {author if author_open} ∪ P` — both
sets are always reported, so `ball` is just a label and `holders` is the truth.

**Sentinel gate — the product decision: the sentinel is always last.** `sentinel_after_peers = True` (default):
the sentinel's pending clock opens only when every *peer* reviewer has answered (`status ∈ {approved,
needs_work, reviewed}`). Until then the sentinel is not charged (and not "blocking"). Configurable to `False`
for teams where the sentinel reviews in parallel with peers.

**Pricing / aggregation**:
- `Interval.holder_hours = {person: business hours on *that person's* calendar}`; `Interval.hours` stays the
  default-window figure for the role/queue level (fixes the responder-pricing bug, S8).
- responsiveness / sentinel-queue sample per `(person, PR)` **episode** (consecutive intervals where the person
  is a holder), not per interval (S12).
- `solo_hours` = hours where a holder is the *only* open ball, reported next to total hours for the constraint
  ranking (the AND-gate fix, §3.1).
- ex-post: reviewers still pending at merge are tagged `unrequired` (listed, not blamed) — S7.

**`_rounds` correction (independent of the ball, but in scope)**: count review rounds per the documented
definition (`concepts.md` §6) — after first review, `pure_rebase`-without-loss excluded, `cause=push` unapprovals
not double-counted; align `jira.py` and `rules.py`. (Measured drift: +33 % / +81 % today.)

## Consequences per metric

- **constraint / queue / delivery**: author joins `waiting_on` while `debt`; standing approvers leave;
  `ready` becomes a named node/queue (author); `solo_hours` beside total; a person-level sum stays
  person-hours, a wall-clock sum is labelled as such.
- **wait hours (review)**: drops by the merge-ready share; `ready` reported separately (expect "review wait" to
  fall and "merge latency" to appear).
- **responsiveness**: episode-level median; approvers not charged for standing approvals; reviewers charged
  during author debt (blocking clock). A `reviewable_since` latency clock is a documented follow-up, not this PRD.
- **sentinel queue**: episode-level; with `sentinel_after_peers` the clock starts when the last peer answers;
  `unrequired` when a PR merged without the sentinel.
- **flow efficiency / phase**: unchanged (phase stays the derived priority label; `doubly_blocked` is additive).
- **vacation blocking**: priced per holder, so the separate second pricing can go.

## API / UI

- `delivery_list` rows gain `ball` (may now be `ready`), `doubly_blocked: bool`, `ready_since`; `waiting_on`
  may include the author. No new multi-lane gantt; the PR drawer gets "needs rework by X · Y/Z reviewers
  pending · ready to merge since T". Stated `hours_kind` per aggregate (person vs wall vs solo).
- `app/timeline.js`: stop duplicating the state machine (its copy has no roster and already disagrees with the
  server) — read phases from the server's intervals instead of re-deriving them.
- Rewrite `docs/concepts.md` §2 around "status vector + debt + ready"; document that `ball` is a label and
  `holders` is the truth.

## Definition of done

- Every scenario in the analysis's matrix has a regression test: S1/S1b (per-actor hours + no approve-cancels-
  debt), S2a/S2b (`cause=push`, standing approvals, `pure_rebase`), S4 (CI flag clears, non-exclusive), S5
  (added mid-debt doesn't move the ball), S7 (pending-at-merge `unrequired`), S8 (per-holder calendar), S12
  (episode median), plus invariants `Σ_x |b_x| ≥ |⋃ b_x|` and `Σ solo_x ≤ |⋃ b_x|`.
- On `data/del-combined.duckdb` and `data/megaspeed.duckdb`, the three headline distortions drop to ~0
  (standing-approval and push-induced-unapproval person-hours) and the `ready` bucket is populated (1.3–1.7k h).
- `sentinel_after_peers` charges the sentinel only after the last peer answers, on both corpora.
- 835+ Python / 96 scrape / 150 node suites stay green (updating the existing ball-model tests, not deleting).

## Out of scope (documented follow-ups)

`min_approvals` (k-of-n) config; `reviewable_since` responsiveness clock; ingesting `build_status`/merge-gate
data (CI is a latent flag today — the corpora have zero `build_status` events); the `reviewed`-without-verdict
semantics (keep current "answered" behaviour, flag it in the UI).

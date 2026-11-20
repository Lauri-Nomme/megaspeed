"""Ball-in-court reconstruction, handover graph, queues and triage over the log.

The event log says *what happened*. This module infers the thing the product is
actually about: **who had the ball** at every moment, and therefore who was
waiting on whom.

    events ──► state machine ──► intervals [start, end) with
                                    phase  = waiting | active | review | ci |
                                             rebasing | closed | unobserved
                                    ball   = the party holding the item
                                    by     = the actor who handed it over

Everything downstream (the gantt phases in the timeline, the blocking graph, the
queue series, the delivery list) is a projection of these intervals, so all four
views agree by construction.

Assumption, stated in every API response: durations are **business hours** when a
calendar is attached (prd/19, ``analysis.calendar``) and **wall-clock hours**
otherwise. Business hours exclude nights, weekends, holidays and the person's
vacations, so a wait that spans a weekend no longer counts two idle days. The
calendar is opt-in at load time: without one, the numbers are elapsed time and
the response says ``hours_mode: "wall-clock"``. The UI labels whichever mode
answered rather than pretending the two are the same.

Measured vs. inferred (the honesty line for this module):

* a period where the author holds the ball and the store contains commits for
  the PR is `active` — commits are evidence of work;
* the same period with no commits is `waiting` — absence of evidence, labelled
  as such in the tooltip;
* after the last event of an open PR nothing is measured at all: that tail is
  `unobserved`, never a phase, and it is excluded from the blocking graph.

**Attribution semantics** (prd/40). A PR waits on *every* reviewer who was
asked and has not answered yet, not on whichever reviewer happened to act
first. The state is a **participant-status vector**, not a ball that flips:

* ``status[r] ∈ {none, approved, needs_work, reviewed}`` for every roster
  member ``r`` (Bitbucket's participant status, which is what the events are);
* ``debt ⊆ R`` — the reviewers whose objection (needs-work, manual unapproval,
  mandatory comment) still stands, i.e. the author owes them a fix;
* ``ci`` — a flag, never a ball: a build can run while a reviewer is pending.

Everything else is derived (``_derive``), never stored:

    author_open   = debt ≠ ∅
    P             = {r : status[r] = none}            (the pending set)
    ready         = ¬author_open ∧ P = ∅ ∧ R ≠ ∅      (all answered, not merged)
    doubly_blocked= author_open ∧ P ≠ ∅
    ball          = author | review | ready            (a priority label)
    holders       = {author if author_open} ∪ P        (the truth)

The ``ball`` label is a projection for the single-valued consumers; ``holders``
is what the person-level projections (blocking graph, queue series,
responsiveness) charge. Consequences:

* an approval does not cancel someone else's objection, a push does not re-pend
  a standing approval, and a push-induced ``pr_unapproved`` leaves the author's
  debt untouched (prd/40 transitions);
* person-level hours are *person-hours*: a 10 h wait on two reviewers is 10 h of
  blocking for each, so the person-level total is larger than the wall-clock
  wait it came from (role/queue level still counts it once, under ``review``);
* business hours are priced **per holder** (``Interval.holder_hours``): a
  reviewer on vacation is not charged the hours someone else worked. The
  interval-level ``hours`` stays the default-window figure for queue/role sums;
* when the store has no roster (an old scrape, a REST-only source) the interval
  falls back to the responder — the previous behaviour — and the wait stays on
  the synthetic ``review`` queue rather than being invented onto a person.

**Sentinel gate** (``sentinel_after_peers``, default on): the sentinel is a
precedence constraint, not a peer. A sentinel's pending clock opens only after
every *peer* reviewer has **responded at least once**; until then the sentinel is
neither charged nor blocking. A peer whose verdict was withdrawn (a
``walked_back`` blocker) or staled by a push has had their turn, so the gate
opens and both owe a verdict. Configurable off for teams whose sentinel reviews
in parallel.

**Bots** (``concepts.md`` §2): an ``actor_kind = bot`` account is never a
constraint, a reviewer or the owner of wait-hours. Bot actors are dropped from
every interval's holder set (``BallCourt.bots``), so the person-level
projections cannot charge them, while the interval's ``by``/``kind`` keep the
activity on the timeline. A PR whose only "responder" was a bot therefore has
no human holder — the honest answer is "abandoned", not "a human waited".
"""

from __future__ import annotations

import bisect
import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from ingest.store import iso, to_naive_utc
from analysis.calendar import Calendar

ASSUMPTION = "wall-clock hours (no working-hours calendar configured)"

SNAPSHOT_PHASE = "unobserved"
REVIEW = "review"
CI = "CI"
CALENDAR = "calendar"
#: The named "all gates satisfied, not merged" state (prd/40): the author owns
#: the merge click, so it is a queue at role level and nobody's person-hours.
READY = "ready"

#: Labels that are not a person. Person-level projections skip them and the
#: queue-level ones keep them as their own node/stream.
SYNTHETIC = (REVIEW, CI, CALENDAR, READY)

PHASE_ORDER = ("waiting", "active", "review", "ci", "rebasing", "closed", SNAPSHOT_PHASE)

# Participant status (prd/40): Bitbucket's per-reviewer status, which is what
# the closed-vocabulary events actually record.
STATUS_NONE = "none"
STATUS_APPROVED = "approved"
STATUS_NEEDS_WORK = "needs_work"
STATUS_REVIEWED = "reviewed"
#: A push stales these verdicts; an approval is only staled by ``approvals_lost``.
STALE_ON_PUSH = (STATUS_NEEDS_WORK, STATUS_REVIEWED)

#: A ``build_status`` event whose ``result``/``state`` is one of these *clears*
#: the CI flag; any other build event (or none) sets it. CI is a flag, never a
#: ball, so a reviewer stays pending while the build runs (prd/40, S4).
BUILD_RESULTS = frozenset({"success", "failed", "passed", "green", "red"})


# activities that mean "a reviewer did something", i.e. the interval *before*
# them was review time
REVIEWER_ACTIONS = {"pr_approved", "pr_unapproved", "pr_needs_work", "reviewer_changed"}
# prd/42 M1: a reviewer's *response* is any verdict or comment they leave — the
# first thing they do after being added, not only an approval.
RESPONSE_ACTIONS = {"pr_approved", "pr_needs_work", "pr_unapproved", "comment_added"}
# activities that mean "the author did something"
AUTHOR_ACTIONS = {"pr_pushed", "rebased"}
TERMINAL = {"pr_merged", "pr_declined"}

# A ticket key on this share of the corpus (and at least this many PRs) is a
# parent/epic link, not a story dependency. Stated in the delivery payload.
PARENT_KEY_SHARE = 0.25
PARENT_KEY_MIN_PRS = 3

#: concepts.md §1: a business day is 8 business hours. The stale-PR signal
#: (prd/34 §5) is "open longer than this many *business* days", never calendar
#: days.
BUSINESS_HOURS_PER_DAY = 8.0
STALE_BUSINESS_DAYS = 5.0
#: prd/46 §2: the one canonical definition of the sentinel wait. Every surface
#: that shows a sentinel number either quotes this clock or names its own
#: (queue wait, person-hours, current wait).
SENTINEL_WAIT_DEFINITION = (
    "sentinel wait = last peer answer → the sentinel's first response "
    "(approval, needs-work or comment), priced on the sentinel's calendar; "
    "the gate is precedence-aware, so the peer phase is not charged to them"
)
#: concepts.md §1: a working week is 5 d = **40 business hours**. It is the one
#: threshold behind every "waiting over a week" label (prd/41 B3): the Overview
#: card, the row pill, the list header and the footer all read this number.
WAITING_WEEK_HOURS = 5.0 * BUSINESS_HOURS_PER_DAY


def _hours(seconds: float) -> float:
    return round(max(0.0, seconds) / 3600.0, 2)


@dataclass(slots=True)
class Interval:
    """One stretch of time during which the same set of parties held a PR."""

    object_id: str
    repo: str
    number: int
    tickets: tuple[str, ...]
    author: str
    start: datetime
    end: datetime
    phase: str
    ball: str               # 'author' | 'review' | 'ready' | 'CI' | 'calendar'
    ball_person: str | None  # the person who actually responded, when known
    by: str                 # actor who handed the ball over
    kind: str               # opened | push:content | needs_work | approved | ...
    evidence: str | None    # commits | push | approval | none
    commits: int
    open: bool
    # concurrent ball-holders at person resolution (prd/40): the author while
    # their debt stands, plus every reviewer still pending. ``ball`` is only a
    # priority label over this set.
    ball_people: tuple[str, ...] = ()
    # the reviewer roster at the time (asked, not necessarily answered), so a
    # consumer can say "2/3 reviewers pending" without replaying the log
    roster: tuple[str, ...] = ()
    # prd/41 B6: every reviewer who still owes a verdict (``status = none``),
    # including a sentinel whose charging clock the precedence gate defers. The
    # delivery list names all of them; ``ball_people`` stays the gated set the
    # person-hour projections charge.
    pending_all: tuple[str, ...] = ()
    #: reviewers whose verdict stands (``status = approved``), for "0/2 approved"
    approved: tuple[str, ...] = ()
    # the two independent dimensions the label is derived from
    author_open: bool = False   # debt ≠ ∅: a reviewer's objection still stands
    ready: bool = False         # every reviewer answered, not merged
    ci: bool = False            # a build is running (never exclusive)
    # business hours (prd/19): seconds inside the *default* working window, set
    # by ``load`` when a calendar is attached. ``None`` means wall-clock.
    business_seconds: float | None = None
    # business hours per holder on *that person's* calendar (prd/40, S8). Filled
    # by ``load``; empty without a calendar (``hours`` is then the wall clock).
    holder_hours: dict[str, float] = field(default_factory=dict)
    #: concepts.md §2 / prd/34 §5: the item was closed by a *bot* — an abandoned
    #: PR, not a wait a human owed. It stays on the timeline and is excluded
    #: from every person-level aggregate.
    abandoned: bool = False
    #: prd/43 §5: the *raw* activity of the event that opened this interval
    #: (``comment_added``), next to ``kind``'s derived label (``author_reply``).
    #: The ticket view and the PR drawer quote this one, so they cannot label
    #: the same last event differently.
    activity: str = ""

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())

    @property
    def hours(self) -> float:
        if self.business_seconds is not None:
            return _hours(self.business_seconds)
        return _hours(self.seconds)

    def hours_for(self, person: str) -> float:
        """The interval's business hours on ``person``'s own calendar."""
        if person in self.holder_hours:
            return self.holder_hours[person]
        return self.hours

    @property
    def measured(self) -> bool:
        return self.phase != SNAPSHOT_PHASE

    @property
    def waited_on(self) -> str | None:
        """Who the ball was with, at role resolution.

        Person-level consumers read :attr:`holders` (or :attr:`owner` for the
        named single holder); this stays the queue/role label so a role sum
        counts one wait per PR however many reviewers were asked.
        """
        if self.ball_person:
            return self.ball_person
        if self.ball == "author":
            return self.author
        if self.ball in (REVIEW, "sentinel"):
            return REVIEW
        return self.ball

    @property
    def owner(self) -> str | None:
        """The one person holding the ball, or ``None`` when it is a queue.

        ``concepts.md`` §2: when the ball is with a single person the answer
        must *name* them ("waiting on FrMo"), never say "review queue".
        """
        people = self.person_holders
        return people[0] if len(people) == 1 else None

    @property
    def ball_label(self) -> str:
        """The named owner when there is exactly one, else the priority label.

        A doubly-blocked PR (author debt + pending reviewers) keeps its
        ``ball`` label — naming only one of the two sides would hide the other.
        """
        if self.owner is not None and (
            len(self.holders) == 1 or self.ball == "author"
        ):
            return self.owner
        if self.ball in (REVIEW, "sentinel"):
            return REVIEW
        return self.ball

    @property
    def holders(self) -> tuple[str, ...]:
        """Every party the ball was concurrently with (person resolution).

        Falls back to :attr:`waited_on` only when the store has no roster at
        all (an old scrape, a REST-only source): then the single responder is
        the only evidence of who was asked. With a roster, an empty holder set
        is a real answer — ``ready`` means nobody owes anything, and an
        ``abandoned`` item (closed by a bot) is nobody's wait.
        """
        if self.abandoned:
            return ()
        if self.ball_people or self.roster:
            return self.ball_people
        waited = self.waited_on
        return (waited,) if waited else ()

    @property
    def person_holders(self) -> tuple[str, ...]:
        """The holders that are people (no synthetic queue labels)."""
        return tuple(p for p in self.holders if p not in SYNTHETIC)

    @property
    def solo(self) -> bool:
        """True when exactly one party holds the ball (marginal blocking)."""
        return len(self.holders) == 1


@dataclass(slots=True)
class PRState:
    pr: dict
    events: list[dict]
    commits: list[datetime]
    intervals: list[Interval]


#: A reviewer action that starts the first review (concepts.md §6, prd/40).
REVIEW_ACTIONS = ("comment_added", "pr_approved")


def first_review(state: PRState) -> dict | None:
    """The first reviewer action on a PR: comment or approval, never the author."""
    opened = state.pr["created_ts"]
    return next(
        (
            event for event in state.events
            if event["_ts"] >= opened
            and event["activity"] in REVIEW_ACTIONS
            and event["actor"] != state.pr["actor"]
        ),
        None,
    )


def review_round_events(state: PRState) -> tuple[list[dict], list[dict]]:
    """The events that are review *rounds*, per ``concepts.md`` §6.

    ``rounds = #pr_pushed (after first review) + #pr_unapproved with
    cause ≠ "push" (after first review)``. Three exclusions, each a measured
    over-count in the old ``#pr_pushed + #pr_unapproved``:

    * pre-first-review pushes are setup, not rework;
    * a ``pure_rebase`` that lost no approval is a head move, not a re-review;
    * a push-induced ``pr_unapproved`` (``cause="push"``) is the *same* round as
      the push that caused it, not a second one.

    ``rules._rounds`` and ``analysis.jira._cycle`` both read this function, so
    the ticket view and the rules page cannot disagree with the concept doc.
    """
    first = first_review(state)
    if first is None:
        return [], []
    cutoff = first["_ts"]
    pushes: list[dict] = []
    for event in state.events:
        if event["activity"] != "pr_pushed" or event["_ts"] < cutoff:
            continue
        attrs = event.get("attributes") or {}
        if attrs.get("push_class") == "pure_rebase" and not attrs.get("approvals_lost"):
            continue
        pushes.append(event)
    unapprovals = [
        event for event in state.events
        if event["activity"] == "pr_unapproved"
        and event["_ts"] >= cutoff
        and (event.get("attributes") or {}).get("cause") != "push"
    ]
    return pushes, unapprovals


def pending_at_close(bc: "BallCourt") -> dict[str, frozenset[str]]:
    """``object_id -> reviewers still pending at merge`` for merged PRs (S7).

    A reviewer who never answered a PR that merged anyway was, ex post, not
    required — so their hours are tagged ``unrequired`` instead of blamed.
    Declined PRs are not included: nobody merged them, so "not required" would
    be a different (and unmeasured) claim.
    """
    last: dict[str, Interval] = {}
    for interval in bc.intervals:
        previous = last.get(interval.object_id)
        if previous is None or interval.end >= previous.end:
            last[interval.object_id] = interval
    out: dict[str, frozenset[str]] = {}
    for state in bc.prs:
        if state.pr["state"] != "merged" or state.pr["closed_ts"] is None:
            continue
        tail = last.get(state.pr["object_id"])
        if tail is None:
            continue
        out[state.pr["object_id"]] = frozenset(
            person for person in tail.person_holders if person != state.pr["actor"]
        )
    return out


@dataclass
class BallCourt:
    intervals: list[Interval]
    prs: list[PRState]
    snapshot: datetime
    sentinels: set[str]
    events: int
    calendar: Calendar | None = None
    calendar_error: str | None = None
    #: prd/40: the sentinel's pending clock opens only after every peer answered.
    sentinel_after_peers: bool = True
    #: concepts.md §2: ``actor_kind = bot`` pseudonyms. They are already dropped
    #: from every interval's holders; the set travels so the projections that
    #: read an author (``_episodes(kind="author")``, the ``by`` side of an edge)
    #: can skip them too.
    bots: set[str] = field(default_factory=set)

    @property
    def open_prs(self) -> list[PRState]:
        return [pr for pr in self.prs if not pr.pr.get("closed_at")]

    @property
    def hours_mode(self) -> str:
        """``business`` with a calendar (prd/19), ``wall-clock`` without one."""
        return "business" if self.calendar is not None else "wall-clock"

    @property
    def calendar_id(self) -> str:
        """Short fingerprint of the calendar that priced this payload (prd/44).

        ``none`` when there is no calendar (wall-clock hours have no config to
        fingerprint), so every payload carries the same shape either way.
        """
        return self.calendar.calendar_id() if self.calendar is not None else "none"

    @property
    def assumption(self) -> str:
        if self.calendar is not None:
            return self.calendar.describe()
        return ASSUMPTION

    def business_hours(self, person: str | None, t1: datetime, t2: datetime) -> float:
        """Working hours in ``[t1, t2)`` for ``person``; wall-clock without a calendar."""
        if self.calendar is None:
            if t1 is None or t2 is None or t2 <= t1:
                return 0.0
            return (t2 - t1).total_seconds() / 3600.0
        return self.calendar.business_hours(person, t1, t2)

    def calendar_warning(self) -> dict:
        """A broken calendar config, for the payload to carry next to the numbers.

        Empty when the config loaded (or when no config exists at all): the
        fallback to wall-clock is then the intended behaviour, not an error.
        """
        if self.calendar_error:
            return {"calendar_error": self.calendar_error}
        return {}

    def role_of(self, actor: str) -> str:
        """Coarse role for graph grouping: sentinel or reviewer."""
        return "sentinel" if actor in self.sentinels else "review"

    @property
    def repos(self) -> list[str]:
        """The PR repositories this store holds, sorted (prd/49).

        The scope control offers exactly these, and a ``repo`` query parameter
        outside the set is a 400 at the endpoint rather than an empty view.
        """
        return sorted({state.pr["repo"] for state in self.prs if state.pr.get("repo")})


# --------------------------------------------------------------------------- #
# reconstruction
# --------------------------------------------------------------------------- #
def _derive(
    reviewers: set[str],
    status: dict[str, str],
    debt: set[str],
    sentinels: frozenset[str] | set[str] = frozenset(),
    sentinel_after_peers: bool = True,
    answered: frozenset[str] | set[str] = frozenset(),
) -> dict:
    """The derived state (prd/40): pending set, debt flag, ready, ball label.

    ``P = {r : status[r] = none}``. With ``sentinel_after_peers`` a sentinel is
    removed from ``P`` while a *peer* is still pending: the sentinel is a
    precedence constraint, so their clock opens only when the peers answered.
    ``ball`` is a priority-ordered *label*; ``holders`` (the author while their
    debt stands, plus ``P``) is the truth.

    ``answered`` (prd/41 B6) is the set of reviewers who have acted at least
    once. The gate defers the sentinel only while a peer has *never responded*:
    a peer whose verdict was withdrawn (a ``walked_back`` blocker) or staled by
    a push has answered, so the sentinel's turn has come and both owe a verdict.
    ``pending_all`` is that ungated set — who owes, as opposed to who is charged.
    """
    pending_all = {r for r in reviewers if status.get(r, STATUS_NONE) == STATUS_NONE}
    pending = set(pending_all)
    if sentinel_after_peers and sentinels:
        peers = pending - sentinels
        if peers - set(answered):
            pending &= peers
    author_open = bool(debt)
    ready = (not author_open) and not pending and bool(reviewers)
    if author_open:
        ball = "author"
    elif pending or not reviewers:
        ball = REVIEW
    else:
        ball = READY
    return {
        "pending": frozenset(pending),
        "pending_all": frozenset(pending_all),
        "author_open": author_open,
        "ready": ready,
        "ball": ball,
    }


def _state_after(
    pr: dict,
    state: dict,
    event: dict,
    sentinels: frozenset[str] | set[str] = frozenset(),
    sentinel_after_peers: bool = True,
) -> dict | None:
    """Advance the participant-status state machine by one event (prd/40).

    State: ``reviewers`` (the roster), ``status[r]`` (Bitbucket's participant
    status), ``debt`` (whose objection still stands), ``answered`` (who has
    responded at least once, for the sentinel gate) and the ``ci`` flag.
    Everything else — pending, author_open, ready, ball — is derived by
    :func:`_derive`; no transition flips a ball, it only changes a status.
    """
    activity = event["activity"]
    actor = event["actor"]
    attrs = event.get("attributes") or {}
    author = pr["actor"]
    since = event["_ts"]

    reviewers = set(state.get("reviewers") or ())
    status = dict(state.get("status") or {})
    debt = set(state.get("debt") or ())
    answered = set(state.get("answered") or ())
    ci = bool(state.get("ci"))

    def enlist(person: str) -> None:
        if person and person != author:
            reviewers.add(person)
            status.setdefault(person, STATUS_NONE)

    # -- roster / verdict bookkeeping (a status change, never a ball flip) ----
    if activity == "reviewer_changed":
        people = {p for p in (event.get("people") or ()) if p and p != author}
        change = attrs.get("change")
        if change == "added":
            # an added reviewer is pending; it moves nothing else (S5)
            for person in people:
                enlist(person)
        elif change == "removed":
            for person in people:
                reviewers.discard(person)
                status.pop(person, None)
                debt.discard(person)
                answered.discard(person)
        elif change == "reviewed" and actor != author:
            # a completed review proves the person was asked; it is an answer
            enlist(actor)
            status[actor] = STATUS_REVIEWED
            answered.add(actor)
    elif activity == "pr_approved" and actor != author:
        enlist(actor)
        status[actor] = STATUS_APPROVED
        answered.add(actor)
        # an approval clears *this* reviewer's objection, not the author's debt
        # from someone else (S1b)
        debt.discard(actor)
    elif activity == "pr_needs_work" and actor != author:
        enlist(actor)
        status[actor] = STATUS_NEEDS_WORK
        answered.add(actor)
        debt.add(actor)
    elif activity == "pr_unapproved" and actor != author:
        enlist(actor)
        answered.add(actor)
        if attrs.get("cause") == "push":
            # the push withdrew the approval: the reviewer owes a new verdict,
            # and the author owes nothing for it (S2a)
            status[actor] = STATUS_NONE
        else:
            # a manual unapproval is an objection, like needs-work: the author
            # owes a fix, the reviewer does not owe a fresh look yet
            status[actor] = STATUS_NEEDS_WORK
            debt.add(actor)

    if activity in TERMINAL:
        return None

    # -- event-specific state changes -----------------------------------------
    if activity == "pr_opened":
        for person in reviewers:
            status[person] = STATUS_NONE
        debt.clear()
        ci = False
        kind = activity
    elif activity in ("pr_pushed", "rebased"):
        push_class = attrs.get("push_class") or ("pure_rebase" if activity == "rebased" else "other")
        approvals_lost = bool(attrs.get("approvals_lost"))
        # a pure rebase that kept every approval is a head move: no change (S2b)
        if not (push_class == "pure_rebase" and not approvals_lost):
            debt.clear()
            for person in reviewers:
                if approvals_lost or status.get(person) in STALE_ON_PUSH:
                    status[person] = STATUS_NONE
        kind = (f"push:{attrs.get('push_class') or 'other'}"
                if activity == "pr_pushed" else "rebase")
    elif activity == "comment_added":
        if actor == author:
            # an author reply answers the objections: debt cleared, staled
            # verdicts become pending again
            debt.clear()
            for person in reviewers:
                if status.get(person) in STALE_ON_PUSH:
                    status[person] = STATUS_NONE
            kind = "author_reply"
        elif attrs.get("severity") == "mandatory":
            # a blocker comment is an objection, not a fresh obligation: the
            # author owes the fix, the reviewer is not also "pending"
            enlist(actor)
            answered.add(actor)
            if (event.get("thread_outcome") or "") == "walked_back":
                # prd/41 B6: the reviewer withdrew the blocker themselves, so
                # nothing is owed to them and they gave no verdict — they are
                # pending (status none), never an answered "needs work"
                status[actor] = STATUS_NONE
                debt.discard(actor)
                kind = "blocker_comment_withdrawn"
            else:
                status[actor] = STATUS_NEEDS_WORK
                debt.add(actor)
                kind = "blocker_comment"
        else:
            # a suggestion does not move the ball: keep whoever has it
            kind = "comment"
    elif activity == "build_status":
        result = str(attrs.get("result") or attrs.get("state") or "").strip().lower()
        # the build result clears the flag; CI is never exclusive (S4)
        ci = result not in BUILD_RESULTS
        kind = "ci"
    elif activity in ("pr_approved", "pr_needs_work", "pr_unapproved", "reviewer_changed"):
        kind = activity
    elif activity in ("question_raised", "question_answered"):
        kind = activity
    else:
        # outside this slice's vocabulary but must not crash: keep the label
        derived = _derive(
            reviewers, status, debt, sentinels, sentinel_after_peers, answered
        )
        previous = {
            "by": state.get("by", actor),
            "kind": state.get("kind", activity),
            "since": state.get("since", since),
        }
        return {
            **state,
            "reviewers": frozenset(reviewers),
            "status": status,
            "debt": frozenset(debt),
            "answered": frozenset(answered),
            "ci": ci,
            **derived,
            **previous,
            "activity": activity,
        }

    derived = _derive(
        reviewers, status, debt, sentinels, sentinel_after_peers, answered
    )
    return {
        "reviewers": frozenset(reviewers),
        "status": status,
        "debt": frozenset(debt),
        "answered": frozenset(answered),
        "ci": ci,
        **derived,
        "by": actor,
        "kind": kind,
        "since": since,
        # prd/43 §5: the raw activity, so every surface names the last event the
        # same way (``kind`` is the derived label the phase logic reads).
        "activity": activity,
    }


def _phase_for(state: dict, pr_author: str, next_event: dict, commits_in: int) -> tuple[str, str]:
    """The phase of the interval *before* ``next_event``, and its evidence.

    ``phase`` stays the derived priority label (prd/40 leaves it unchanged): it
    is ``ci`` only when a build is running and *nobody* holds the ball, so a
    reviewer pending during a build keeps their ``review`` phase (S4). A
    ``ready`` interval stays ``review`` here; consumers read ``Interval.ready``.
    """
    if state.get("ci") and not state.get("author_open") and not state.get("pending"):
        return "ci", "build status"
    activity = next_event["activity"]
    actor = next_event["actor"]
    if activity == "pr_pushed":
        push_class = (next_event.get("attributes") or {}).get("push_class") or "other"
        phase = "rebasing" if push_class in ("pure_rebase", "rebase_conflict") else "active"
        return phase, f"{push_class.replace('_', ' ')} push"
    if activity == "rebased":
        return "rebasing", "rebase"
    if activity == "pr_merged" or activity == "pr_declined":
        return "review", "final approval → close"
    if state.get("author_open"):
        if activity in AUTHOR_ACTIONS or actor == pr_author:
            return "active", "author responded"
        if commits_in:
            return "active", f"{commits_in} commit{'s' if commits_in != 1 else ''}"
        return "waiting", "no commits"
    if activity in REVIEWER_ACTIONS or activity == "comment_added":
        return "review", f"{activity.replace('_', ' ')}"
    if commits_in:
        return "active", f"{commits_in} commit{'s' if commits_in != 1 else ''}"
    return "review", "no reviewer action"


def _person_for(pr: dict, state: dict, next_event: dict,
                bots: frozenset[str] | set[str] = frozenset()) -> str | None:
    """Best available person for the waiting side of an interval.

    ``review`` is a queue, so the responder is only known ex post: it is the
    reviewer who eventually acted. That is a measured fact (this wait ended with
    that person's action), not an assignment of blame — and it is *not* the only
    party the item waited on: :func:`_holders` carries the concurrent holders.
    A bot responder is never a person to name (concepts.md §2).
    """
    ball = state.get("ball")
    if ball == "author":
        return None if pr["actor"] in bots else pr["actor"]
    if ball == CI:
        return None
    if ball in (REVIEW, "calendar"):
        actor = next_event["actor"]
        if actor in bots:
            return None
        if actor != pr["actor"] and next_event["activity"] in REVIEWER_ACTIONS:
            return actor
        if next_event["activity"] == "comment_added" and actor != pr["actor"]:
            return actor
        return None
    return ball


def _holders(pr: dict, state: dict, next_event: dict | None,
             bots: frozenset[str] | set[str] = frozenset()) -> tuple[str, ...]:
    """Every party the ball was concurrently with (prd/40).

    That is ``{author if author_open} ∪ P`` — both dimensions, always. With no
    roster in the store (an old scrape) the interval falls back to the responder
    who ends it, which is the only evidence of who was asked; with a roster the
    status vector is the truth, so a reviewer who acts without having been asked
    is not retro-charged for the wait someone else held.

    Bots are dropped here, once: every person-level projection reads
    ``holders``, so an automation account can never be charged a wait
    (concepts.md §2). Its activity stays on the interval's ``by``/``kind``.
    An item a bot *declined* is abandoned, not owed by anyone.
    """
    if (
        next_event is not None
        and next_event["activity"] == "pr_declined"
        and next_event["actor"] in bots
    ):
        return ()
    author = pr["actor"]
    people = {
        p for p in (state.get("pending") or ())
        if p and p != author and p not in bots
    }
    if state.get("author_open") and author not in bots:
        people.add(author)
    if not state.get("reviewers") and next_event is not None:
        actor = next_event["actor"]
        activity = next_event["activity"]
        if actor != author and actor not in bots and (
            activity in REVIEWER_ACTIONS or activity == "comment_added"
        ):
            people.add(actor)
    return tuple(sorted(people))


def _timeline(
    state: PRState,
    snapshot: datetime,
    open_pr: bool,
    sentinels: frozenset[str] | set[str] = frozenset(),
    sentinel_after_peers: bool = True,
    bots: frozenset[str] | set[str] = frozenset(),
) -> None:
    pr = state.pr
    commits = state.commits
    author = pr["actor"]
    person_events = [e for e in state.events if e["_ts"] >= pr["created_ts"]]
    if not person_events:
        return
    # prd/41 B1: a PR the automation closed is abandoned, not a human wait. The
    # close often arrives as a bot comment plus the decline at the same instant,
    # so the stretch that *ends* at that close is the tail — and it must carry
    # the flag, not only the interval an explicit ``pr_declined`` ended.
    bot_declines = [
        event["_ts"] for event in person_events
        if event["activity"] == "pr_declined" and event["actor"] in bots
    ]
    declined_ts = min(bot_declines) if bot_declines else None

    def facts(state_dict: dict) -> dict:
        """The per-interval participant fields every branch shares."""
        return {
            "roster": tuple(sorted(state_dict.get("reviewers") or ())),
            "pending_all": tuple(sorted(
                person for person in (state_dict.get("pending_all") or ())
                if person not in bots
            )),
            "approved": tuple(sorted(
                person for person, value in (state_dict.get("status") or {}).items()
                if value == STATUS_APPROVED and person not in bots
            )),
            "author_open": bool(state_dict.get("author_open")),
            "ready": bool(state_dict.get("ready")),
            "ci": bool(state_dict.get("ci")),
        }

    current: dict | None = None
    for index, event in enumerate(person_events):
        if current is not None and event["_ts"] > current["since"]:
            start = current["since"]
            end = event["_ts"]
            commits_in = sum(1 for c in commits if start < c <= end)
            phase, evidence = _phase_for(current, author, event, commits_in)
            state.intervals.append(
                Interval(
                    object_id=pr["object_id"], repo=pr["repo"], number=pr["pr_id"],
                    tickets=tuple(pr.get("ticket_keys") or []), author=author,
                    start=start, end=end, phase=phase,
                    ball=current["ball"],
                    ball_person=_person_for(pr, current, event, bots),
                    ball_people=_holders(pr, current, event, bots),
                    by=current["by"], kind=current["kind"], evidence=evidence,
                    commits=commits_in, open=open_pr,
                    activity=current.get("activity", ""),
                    abandoned=bool(
                        declined_ts is not None and event["_ts"] >= declined_ts
                    ),
                    **facts(current),
                )
            )
        current = _state_after(pr, current or {}, event, sentinels, sentinel_after_peers)

    # the tail: observational censoring, never a measured phase
    tail_start = current["since"] if current else pr["created_ts"]
    if current is None:
        return
    end = pr["closed_ts"] or snapshot
    if end > tail_start:
        if not open_pr:
            phase, evidence = "closed", "closed after the last event"
        else:
            phase, evidence = SNAPSHOT_PHASE, "no events since"
        state.intervals.append(
            Interval(
                object_id=pr["object_id"], repo=pr["repo"], number=pr["pr_id"],
                tickets=tuple(pr.get("ticket_keys") or []), author=author,
                start=tail_start, end=end, phase=phase,
                ball=current["ball"], ball_person=None,
                ball_people=_holders(pr, current, None, bots),
                by=current["by"],
                kind=current["kind"], evidence=evidence, commits=0, open=open_pr,
                activity=current.get("activity", ""),
                # prd/41 B1: the tail to ``closed_ts`` of a bot-declined PR is
                # the abandoned stretch too, so its hours never reach a person.
                abandoned=bool(declined_ts is not None),
                **facts(current),
            )
        )



EVENT_SQL = (
    "SELECT event_key, ts, activity, actor, repo, pr_id, CAST(attributes AS VARCHAR) AS attrs"
    " FROM events ORDER BY ts ASC, event_key ASC"
)
# Who an event is *about* (product review #7): ``reviewer_changed`` carries the
# added/removed/reviewed person as a PERSON object link, not as the actor.
LINK_SQL = (
    "SELECT event_key, object_id FROM event_objects"
    " WHERE object_type = 'PERSON' ORDER BY event_key, object_id"
)
# prd/41 B6: the review-thread outcome is what says a mandatory blocker was
# withdrawn (``walked_back``) instead of standing. A comment event links to its
# THREAD object, and the THREAD carries the closed-vocabulary outcome.
THREAD_LINK_SQL = (
    "SELECT event_key, object_id FROM event_objects"
    " WHERE object_type = 'THREAD' ORDER BY event_key, object_id"
)
THREAD_OUTCOME_SQL = (
    "SELECT object_id, CAST(attributes AS VARCHAR) FROM objects"
    " WHERE object_type = 'THREAD' ORDER BY object_id"
)
OBJECT_SQL = (
    "SELECT object_type, object_id, repo, pr_id, ticket_keys, actor, state, created_at, closed_at"
    " FROM objects ORDER BY object_type, object_id"
)
# prd/22: the PR's source/target refs as one-way digests (never branch names).
REF_SQL = (
    "SELECT object_id, CAST(attributes AS VARCHAR) FROM objects WHERE object_type = 'PR'"
)
PERSON_SQL = (
    "SELECT object_id, CAST(attributes AS VARCHAR) FROM objects WHERE object_type = 'PERSON'"
)


def _has_column(con, table: str, column: str) -> bool:
    """Older stores predate a column; reads must degrade, not crash."""
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'main' AND table_name = ?",
        [table],
    ).fetchall()
    return any(row[0] == column for row in rows)


def load_bots(con) -> set[str]:
    """Pseudonyms whose ``actor_kind`` is ``bot`` (concepts.md §2).

    The store's ``PERSON.attributes.actor_kind`` is the projection of
    ``classify_actor`` (prd/02), so this is the one place the analysis learns
    which accounts are automation. A store ingested before the classification
    has no ``attributes`` column at all: then the set is empty and nothing is
    silently treated as a bot.
    """
    if not _has_column(con, "objects", "attributes"):
        return set()
    bots: set[str] = set()
    for object_id, attrs in con.execute(PERSON_SQL).fetchall():
        attributes = json.loads(attrs) if attrs else {}
        if attributes.get("actor_kind") == "bot":
            bots.add(str(object_id).partition(":")[2])
    return bots


def _pr_refs(con) -> dict[str, dict]:
    """``object_id -> {source_ref, target_ref}`` for the PRs that carry them.

    Absent on a store ingested before prd/22 persisted the refs; the stack
    analysis reads that as "stack depth unknown" (``available: false``), never
    as "no stacks". The values are digests: comparable, never displayed.
    """
    columns = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'main' AND table_name = 'objects'"
        ).fetchall()
    }
    if "attributes" not in columns:
        return {}
    refs: dict[str, dict] = {}
    for object_id, attrs in con.execute(REF_SQL).fetchall():
        payload = json.loads(attrs) if attrs else {}
        entry = {key: payload[key] for key in ("source_ref", "target_ref") if payload.get(key)}
        if entry:
            refs[object_id] = entry
    return refs


def _snapshot(conn) -> datetime:
    """The ingest watermark: what the store knew, and when."""
    row = conn.execute("SELECT max(as_of) FROM ingest_runs").fetchone()
    if row and row[0]:
        return to_naive_utc(row[0])
    row = conn.execute("SELECT max(ts) FROM events").fetchone()
    return to_naive_utc(row[0]) if row and row[0] else datetime.utcnow()


def load(
    conn,
    calendar: Calendar | None = None,
    calendar_error: str | None = None,
    sentinel_after_peers: bool = True,
) -> BallCourt:
    """Read the store and reconstruct every PR's participant-status timeline.

    Reads the tables directly (rather than through ``ingest.store``'s read
    helpers) so the analysis views depend only on the schema — the
    pseudonymisation contract — and not on ingest refactors.

    ``calendar`` (prd/19) converts each interval to business hours. The
    interval-level ``hours`` is priced on the **default window** (the role/queue
    figure); ``holder_hours`` prices each holder on *their own* calendar, so a
    reviewer on vacation is not charged the hours someone else worked (S8).
    Without a calendar every duration stays wall-clock and ``hours_mode`` says
    so.

    ``sentinel_after_peers`` (prd/40) is the precedence policy: when true (the
    default) the sentinel's pending clock opens only after every peer answered.
    """
    snapshot = _snapshot(conn)
    people_by_event: dict[str, list[str]] = defaultdict(list)
    for event_key, object_id in conn.execute(LINK_SQL).fetchall():
        person = object_id.partition(":")[2]
        if person:
            people_by_event[event_key].append(person)
    # prd/41 B6: a comment event's THREAD outcome (walked_back / rejected / …).
    # Absent on a store ingested before the thread classifier: then nothing is
    # treated as withdrawn and the mandatory-comment behaviour is unchanged.
    thread_outcome: dict[str, str] = {}
    if _has_column(conn, "objects", "attributes"):
        for object_id, attrs in conn.execute(THREAD_OUTCOME_SQL).fetchall():
            payload = json.loads(attrs) if attrs else {}
            outcome = payload.get("thread_outcome")
            if outcome:
                thread_outcome[object_id] = outcome
    outcome_by_event: dict[str, str] = {}
    if thread_outcome:
        for event_key, object_id in conn.execute(THREAD_LINK_SQL).fetchall():
            outcome = thread_outcome.get(object_id)
            if outcome:
                outcome_by_event[event_key] = outcome
    events = []
    for key, ts, activity, actor, repo, pr_id, attrs in conn.execute(EVENT_SQL).fetchall():
        events.append({
            "key": key,
            "ts": iso(ts),
            "_ts": ts,
            "activity": activity,
            "actor": actor,
            "repo": repo,
            "pr_id": pr_id,
            "attributes": json.loads(attrs) if attrs else {},
            "people": tuple(people_by_event.get(key, ())),
            "thread_outcome": outcome_by_event.get(key),
        })
    objects = []
    for otype, oid, repo, pr_id, keys, actor, state, created, closed in conn.execute(OBJECT_SQL).fetchall():
        objects.append({
            "object_type": otype,
            "object_id": oid,
            "repo": repo,
            "pr_id": pr_id,
            "ticket_keys": list(keys or []),
            "actor": actor,
            "state": state,
            "created_at": created,
            "closed_at": closed,
        })

    pr_objects: dict[str, dict] = {}
    commits_by_pr: dict[tuple[str, int], list[datetime]] = defaultdict(list)
    for obj in objects:
        if obj["object_type"] == "PR":
            pr_objects[obj["object_id"]] = {
                "object_id": obj["object_id"],
                "repo": obj["repo"] or "",
                "pr_id": obj["pr_id"],
                "ticket_keys": obj["ticket_keys"],
                "actor": obj["actor"] or "system",
                "state": obj["state"] or "open",
                "created_ts": to_naive_utc(obj["created_at"]) if obj["created_at"] else snapshot,
                "closed_ts": to_naive_utc(obj["closed_at"]) if obj["closed_at"] else None,
            }
        elif obj["object_type"] == "COMMIT" and obj["repo"] and obj["pr_id"] is not None:
            if obj["created_at"]:
                commits_by_pr[(obj["repo"], int(obj["pr_id"]))].append(to_naive_utc(obj["created_at"]))
    # prd/22: the stack edges are ref-digest equality, so they ride on the PR.
    for object_id, refs in _pr_refs(conn).items():
        if object_id in pr_objects:
            pr_objects[object_id].update(refs)

    by_pr: dict[str, list[dict]] = defaultdict(list)
    sentinels: set[str] = set()
    for event in events:
        if (event.get("attributes") or {}).get("sentinel") is True:
            # the role is declared on the sentinel's own action; on a roster
            # event it is the person the event is about, not the author who
            # asked them
            sentinels.add(event["actor"])
            sentinels.update(event.get("people") or ())
        key = f"PR:{event['repo']}#{event['pr_id']}"
        if key in pr_objects:
            by_pr[key].append(event)

    states: list[PRState] = []
    bots = load_bots(conn)
    for object_id, pr in pr_objects.items():
        pr_events = sorted(by_pr.get(object_id, []), key=lambda e: (e["_ts"], e["key"]))
        commits = sorted(commits_by_pr.get((pr["repo"], pr["pr_id"]), []))
        state = PRState(pr=pr, events=pr_events, commits=commits, intervals=[])
        _timeline(
            state, snapshot, open_pr=pr["closed_ts"] is None,
            sentinels=sentinels, sentinel_after_peers=sentinel_after_peers,
            bots=bots,
        )
        states.append(state)

    intervals = [i for s in states for i in s.intervals]
    intervals.sort(key=lambda i: (i.start, i.object_id))
    for interval in intervals:
        if calendar is not None:
            # the role/queue figure stays the default-window duration (prd/40):
            # a queue has no single calendar, and a person-level sum must use
            # the holder's own.
            interval.business_seconds = calendar.business_hours(
                None, interval.start, interval.end
            ) * 3600.0
        interval.holder_hours = {
            person: (
                calendar.business_hours(person, interval.start, interval.end)
                if calendar is not None else interval.hours
            )
            for person in interval.holders
        }
    return BallCourt(
        intervals=intervals, prs=states, snapshot=snapshot, sentinels=sentinels,
        events=len(events), calendar=calendar, calendar_error=calendar_error,
        sentinel_after_peers=sentinel_after_peers, bots=bots,
    )


def scope_to_repo(bc: BallCourt, repo: str) -> BallCourt:
    """A repo-scoped slice of the court (prd/49).

    The store already carries each PR's ``repo``, so a repo scope is a filter on
    the same reconstruction, not a second analysis: the PRs, their intervals and
    their PR-side events narrow to ``repo`` and every projection downstream
    (delivery list, blocking graph, queues, rules, the policy lab) therefore
    recomputes against that repo's cohort.

    The *roles* do not narrow: sentinels and bots are store-wide identities
    (``concepts.md`` §2), so a PR's mandatory approver is still the same person
    in a one-repo slice. The Jira side is scoped separately
    (:func:`analysis.jira.scope_to_repo`) because a story is only attributable to
    a repo through a PR that names it.
    """
    wanted = str(repo or "")
    prs = [state for state in bc.prs if state.pr.get("repo") == wanted]
    object_ids = {state.pr["object_id"] for state in prs}
    intervals = [interval for interval in bc.intervals if interval.object_id in object_ids]
    return replace(
        bc,
        prs=prs,
        intervals=intervals,
        # the PR-side event count of the slice: the scope changes the cohort,
        # so the payload must not quote the whole log's size
        events=sum(len(state.events) for state in prs),
    )


# --------------------------------------------------------------------------- #
# projections
# --------------------------------------------------------------------------- #
def _queue_of(bc: BallCourt, person: str, pr: dict | None, side: str) -> str:
    """Contextual role label for an edge endpoint."""
    if person in SYNTHETIC:
        return person
    if pr and person == pr.get("actor"):
        return "author"
    return bc.role_of(person)


def blocking_graph(bc: BallCourt, level: str = "queue", window: tuple[datetime, datetime] | None = None) -> dict:
    """Weighted directed graph: ``X -> Y`` = "X waited on Y for N hours"."""
    start, end = window or (bc.intervals[0].start if bc.intervals else bc.snapshot, bc.snapshot)
    edges: dict[tuple[str, str], dict] = {}
    nodes: dict[str, dict] = {}
    prs = {s.pr["object_id"]: s.pr for s in bc.prs}
    role_people: dict[str, set[str]] = defaultdict(set)

    def endpoint(person: str, pr: dict | None, side: str) -> tuple[str, str]:
        if level == "person":
            if person in SYNTHETIC:
                return person, "synthetic"
            return person, "person"
        queue = _queue_of(bc, person, pr, side)
        if person not in SYNTHETIC and person not in bc.bots:
            role_people[queue].add(person)
        return queue, "role"

    measured = [i for i in bc.intervals if i.measured and i.start < end and i.end > start]
    # prd/34 §5: an interval that ended with a bot closing the PR is "idle until
    # automation cleaned it up", not review wait — excluded and reported.
    abandoned_hours = round(sum(i.hours for i in measured if i.abandoned), 1)
    measured = [i for i in measured if not i.abandoned]
    for interval in measured:
        pr = prs.get(interval.object_id)
        waited_on = interval.waited_on
        # Person level: every concurrent holder is charged the wait, on their
        # own calendar (prd/40). Role/queue level keeps one edge per wait — the
        # queue is the same whether one reviewer or three were asked.
        if level == "person":
            targets = interval.holders
        else:
            targets = (waited_on,) if waited_on else ()
        # prd/44 DES-2: a build running with nobody holding the ball is the CI
        # queue, not review wait. The CI node is a real inbound wait ("the
        # author waited on CI for N h"), so the graph can finally show it.
        ci_wait = level == "queue" and interval.phase == "ci"
        if ci_wait:
            targets = (CI,)
        if not targets:
            continue
        # concepts.md §2: a bot handover is not a person handover. The wait it
        # ends still belongs to the human side, so attribute the edge to the
        # PR's author when that is a human; a bot-authored PR has no person
        # source and contributes no person-level edge.
        handed_by = interval.author if ci_wait else interval.by
        if handed_by in bc.bots:
            handed_by = interval.author if interval.author not in bc.bots else None
        if handed_by is None:
            continue
        x, xkind = endpoint(handed_by, pr, "by")
        for person in targets:
            hours = interval.hours_for(person)
            y, ykind = endpoint(person, pr, "ball")
            if x == y:
                continue
            edge = edges.setdefault((x, y), {"from": x, "to": y, "hours": 0.0, "count": 0, "prs": set()})
            edge["hours"] += hours
            edge["count"] += 1
            edge["prs"].add(interval.object_id)
            solo = level == "person" and interval.solo
            for node_id, kind in ((x, xkind), (y, ykind)):
                entry = nodes.setdefault(node_id, {
                    "id": node_id, "label": node_id, "kind": kind, "hours": 0.0,
                    "out_hours": 0.0, "count": 0, "wip_now": 0, "per_day": 0.0,
                    "solo_hours": 0.0, "people": [],
                })
                if node_id == y:
                    entry["hours"] += hours       # waited on by others (inbound)
                    entry["count"] += 1
                    # marginal blocking: the hours this party was the *only*
                    # open ball, next to the total (the AND-gate caveat, §3.1)
                    if solo:
                        entry["solo_hours"] += hours
                else:
                    entry["out_hours"] = entry.get("out_hours", 0.0) + hours

    # current WIP per node: open PRs whose latest measured interval waits on it
    latest: dict[str, Interval] = {}
    for interval in bc.intervals:
        if not interval.open or interval.phase == SNAPSHOT_PHASE:
            continue
        previous = latest.get(interval.object_id)
        if previous is None or interval.start >= previous.start:
            latest[interval.object_id] = interval
    for interval in latest.values():
        if interval.phase == SNAPSHOT_PHASE:
            continue
        waited_on = interval.waited_on
        if level == "person":
            targets = interval.holders
        else:
            targets = (waited_on,) if waited_on else ()
        for person in targets:
            node_id, _kind = endpoint(person, prs.get(interval.object_id), "ball")
            if node_id in nodes:
                nodes[node_id]["wip_now"] += 1

    days = max((end - start).total_seconds() / 86400.0, 1e-9)
    out_edges = []
    for edge in edges.values():
        hours = round(edge["hours"], 1)
        out_edges.append({
            "from": edge["from"],
            "to": edge["to"],
            "hours": hours,
            "count": edge["count"],
            "mean_hours": round(hours / edge["count"], 1) if edge["count"] else 0.0,
            "per_day": round(hours / days, 2),
            "prs": len(edge["prs"]),
        })
    for entry in nodes.values():
        if entry["kind"] == "role":
            entry["people"] = sorted(role_people.get(entry["id"], set()))
        entry["hours"] = round(entry["hours"], 1)
        entry["out_hours"] = round(entry.get("out_hours", 0.0), 1)
        entry["solo_hours"] = round(entry.get("solo_hours", 0.0), 1)
        # prd/41 P4: person-level hours charge every concurrent holder, so a
        # person who is one of several reviewers looks busier than the wait they
        # alone caused. solo_share makes the two numbers linkable at a glance.
        entry["solo_share"] = (
            round(entry["solo_hours"] / entry["hours"], 4)
            if entry["hours"] > 0 else None
        )
        entry["per_day"] = round(entry["hours"] / days, 2)
    out_edges.sort(key=lambda e: e["hours"], reverse=True)

    total = round(sum(e["hours"] for e in out_edges), 1)
    return {
        "ok": True,
        "hours_mode": bc.hours_mode,
        "assumption": bc.assumption,
        **bc.calendar_warning(),
        "attribution": (
            "a wait is attributed to every party the ball was concurrently with "
            "(the author while their debt stands, plus every pending reviewer), "
            "priced on each holder's own calendar; solo_hours is the part where "
            "that party was the only open ball"
            if level == "person" else
            "a wait is attributed to the party the ball was with; the review "
            "queue counts one wait per PR, however many reviewers were asked"
        ),
        "hours_kind": "person-hours" if level == "person" else "wait-hours",
        "level": level,
        "bots_excluded": sorted(bc.bots),
        # prd/41 P4: the two person-level figures are different questions and
        # the payload says how they relate, so total and solo are never read as
        # the same "load".
        "hours_note": (
            "person-hours charge every concurrent holder, so one wait with three "
            "holders is counted three times; solo_hours is the part where that "
            "person was the only open ball — read solo as the marginal load and "
            "hours as the shared wait it came from. This is not the sentinel's "
            "response time (Queue → response time: last peer answer → first "
            "response), which is a different metric"
            if level == "person" else
            "wait-hours count each wait once, under the role/queue that held it; "
            "this is not the sentinel's response time (a different metric)"
        ),
        "window": {"start": iso(start), "end": iso(end), "days": round(days, 1)},
        "nodes": sorted(nodes.values(), key=lambda n: (-n["hours"], n["id"])),
        "edges": out_edges,
        "totals": {
            "nodes": len(nodes),
            "edges": len(out_edges),
            "hours": total,
            "abandoned_hours": abandoned_hours,
            "mean_wait_hours": round(total / max(1, sum(e["count"] for e in out_edges)), 1),
            "events": bc.events,
        },
    }


def queue_series(
    bc: BallCourt,
    level: str = "person",
    window: tuple[datetime, datetime] | None = None,
    bucket_hours: float | None = None,
) -> dict:
    """Per-person (or per-role) WIP over time, arrivals and completions.

    Little's law: ``W = L / λ``. A high mean wait with high WIP is an
    *overloaded* server; the same wait with low WIP is a *slow* server. Waits are
    business hours when a calendar is attached (prd/19); the WIP sampling stays
    on the wall clock, so a weekend shows a flat queue — which is what it is.

    At person level every concurrent holder is charged the wait, on their own
    calendar (prd/40): a PR waiting on three reviewers arrives in all three
    queues, so the per-person ``hours`` are person-hours. ``solo_hours`` is the
    part of that where the holder was the only open ball. The queue level still
    counts one wait per PR under the role/queue name — ``ready`` is its own
    queue now, not review wait.
    """
    start, end = window or (bc.intervals[0].start if bc.intervals else bc.snapshot, bc.snapshot)
    span_hours = max(1.0, (end - start).total_seconds() / 3600.0)
    # Little's law divides by the hours the queue could actually be served in.
    service_hours = max(1e-9, bc.business_hours(None, start, end))
    if bucket_hours is None:
        bucket_hours = 24.0 if span_hours > 24 * 12 else max(1.0, round(span_hours / 60.0, 1))
    buckets = max(1, int(span_hours // bucket_hours) + 1)
    prs = {s.pr["object_id"]: s.pr for s in bc.prs}

    def key_for(person: str, pr: dict) -> str:
        if level == "person":
            return person
        return _queue_of(bc, person, pr, "ball")

    streams: dict[str, dict] = {}

    def stream(name: str) -> dict:
        if name in SYNTHETIC:
            role = "synthetic"
        elif name in bc.sentinels:
            role = "sentinel"
        else:
            role = "person"
        return streams.setdefault(name, {
            "id": name,
            "role": role,
            "people": [] if level == "person" else [],
            "wip": [0] * buckets,
            "arrivals": [0] * buckets,
            "completions": [0] * buckets,
            "hours": 0.0,
            "solo_hours": 0.0,
            "count": 0,
            "prs": set(),
        })

    measured = [i for i in bc.intervals if i.measured and i.start < end and i.end > start]
    # prd/34 §5: idle-until-a-bot-closed-it is not review wait; exclude it from
    # the queues and report the hours next to the totals.
    abandoned_hours = round(sum(i.hours for i in measured if i.abandoned), 1)
    measured = [i for i in measured if not i.abandoned]
    # WIP is sampled at the bucket midpoints, so the sample points are the same
    # for every interval: build them once and use a *difference array* (add at
    # the first covered bucket, subtract after the last) instead of testing every
    # interval against every bucket. The prefix sum after the loop is the same
    # series the naive O(intervals × buckets) loop produced.
    midpoints = (
        [start + timedelta(hours=(b + 0.5) * bucket_hours) for b in range(buckets)]
        if measured else []
    )
    wip_diff: dict[str, list[int]] = {}
    for interval in measured:
        pr = prs.get(interval.object_id) or {}
        # Person level charges every concurrent holder (product review #7);
        # the queue level keeps one wait per PR under the role/queue name.
        if level == "person":
            people = interval.holders
        elif interval.phase == "ci":
            # prd/44 DES-2: CI running with nobody holding the ball is its own
            # queue. It is never exclusive with a review (concepts.md §2), but
            # when the phase *is* ci no person owns the wait.
            people = (CI,)
        else:
            people = (interval.waited_on,) if interval.waited_on else ()
        for person in people:
            if not person:
                continue
            if level == "person" and person in bc.bots:
                # concepts.md §2: a bot is not a person queue. The holder set
                # already excludes them; this is the stated invariant.
                continue
            name = key_for(person, pr)
            entry = stream(name)
            if level == "queue" and person not in SYNTHETIC:
                entry["people"].append(person)
            entry["hours"] += interval.hours_for(person)
            if level == "person" and interval.solo:
                entry["solo_hours"] += interval.hours_for(person)
            entry["count"] += 1
            entry["prs"].add(interval.object_id)
            if not interval.open:
                entry.setdefault("served", set()).add(interval.object_id)

            # arrivals / departures land in the bucket that contains the timestamp
            for stamp, series in ((interval.start, entry["arrivals"]), (interval.end, entry["completions"])):
                b = int((stamp - start).total_seconds() // (bucket_hours * 3600))
                if 0 <= b < buckets:
                    series[b] += 1
            # WIP counts one item per bucket whose midpoint falls in [start, end)
            diff = wip_diff.get(name)
            if diff is None:
                diff = wip_diff[name] = [0] * (buckets + 1)
            lo = bisect.bisect_left(midpoints, interval.start)
            hi = bisect.bisect_left(midpoints, interval.end)
            if lo < hi:
                diff[lo] += 1
                diff[hi] -= 1

    # prefix-sum the per-stream difference arrays into the WIP series
    for name, diff in wip_diff.items():
        entry = streams[name]
        running = 0
        wip = entry["wip"]
        for b in range(buckets):
            running += diff[b]
            wip[b] = running

    # The window length is a *calendar* span (it is reported as such, `days`);
    # the queue rate is per *business* day (concepts.md §1), so it divides by
    # BUSINESS_HOURS_PER_DAY, never 24. The two are separate on purpose.
    calendar_days = (end - start).total_seconds() / 86400.0
    business_days = span_hours / BUSINESS_HOURS_PER_DAY
    # global mean wait, for a relative diagnosis that works on any data size
    all_hours = [i.hours for i in measured]
    baseline = sorted(all_hours)[len(all_hours) // 2] if all_hours else 0.0
    out = []
    for entry in streams.values():
        items = len(entry["prs"])
        mean_hours = entry["hours"] / items if items else 0.0
        mean_wip = sum(entry["wip"]) / max(1, len(entry["wip"]))
        queue_length = entry["hours"] / service_hours
        served = len(entry.get("served") or ())
        per_day = served / business_days if business_days else 0.0
        if mean_hours > baseline * 1.25 and queue_length >= 1.0:
            diagnosis = "overloaded"
        elif mean_hours > baseline * 1.25:
            diagnosis = "slow"
        elif queue_length >= 2.0:
            diagnosis = "overloaded"
        else:
            diagnosis = "healthy"
        out.append({
            "id": entry["id"],
            "role": entry["role"],
            "people": sorted(set(entry["people"])),
            "wip": entry["wip"],
            "arrivals": entry["arrivals"],
            "completions": entry["completions"],
            "summary": {
                "mean_wip": round(mean_wip, 2),
                "peak_wip": max(entry["wip"] or [0]),
                "mean_queue_length": round(queue_length, 2),
                "items": items,
                "intervals": entry["count"],
                "hours": round(entry["hours"], 1),
                "solo_hours": round(entry.get("solo_hours", 0.0), 1),
                "solo_share": (
                    round(entry.get("solo_hours", 0.0) / entry["hours"], 4)
                    if entry["hours"] > 0 else None
                ),
                "mean_wait_hours": round(mean_hours, 1),
                "throughput_per_day": round(per_day, 2),
                "diagnosis": diagnosis,
                # concepts.md §2/§6: "overloaded" is queue wait, never calendar
                # utilisation. The name travels with the number so the policy
                # lab's "busy" (ρ) can never be read as the same metric.
                "diagnosis_kind": "queue-wait",
                "prs": items,
            },
        })
    out.sort(key=lambda s: (-s["summary"]["mean_wait_hours"], s["id"]))
    return {
        "ok": True,
        "hours_mode": bc.hours_mode,
        "assumption": bc.assumption,
        **bc.calendar_warning(),
        "diagnosis_note": (
            "queue wait, not calendar utilisation: 'overloaded' = long wait with "
            "a standing queue, 'slow' = long wait with little WIP, both measured "
            "against the median wait in this window; person-level mean wait is "
            "person-hours from 'added as reviewer' to the answer, so it includes "
            "the peer phase and is NOT the sentinel wait (Queue → response time: "
            "last peer answer → first response); the policy lab's 'busy' is "
            "calendar share, a third metric"
        ),
        "diagnosis_kind": "queue-wait",
        "level": level,
        "hours_kind": "person-hours" if level == "person" else "wait-hours",
        "bots_excluded": sorted(bc.bots),
        # prd/41 P4: total and solo answer different questions (shared wait vs
        # marginal load); the note travels with the numbers.
        "hours_note": (
            "person-hours charge every concurrent holder; solo_hours is the part "
            "where that person was the only open ball"
            if level == "person" else
            "wait-hours count each wait once, under the role/queue that held it"
        ),
        "window": {
            "start": iso(start), "end": iso(end), "days": round(calendar_days, 1),
            "service_hours": round(service_hours, 1),
        },
        "bucket_hours": bucket_hours,
        "buckets": buckets,
        "baseline_mean_wait_hours": round(baseline, 1),
        # prd/42 M1: the comparative answer ("who is the constraint") needs a
        # response-time column, not only a queue-wait ranking. It is computed
        # from the same events/intervals and travels with the queue payload.
        "response": response_times(bc, (start, end)),
        "streams": out,
        "totals": {
            "items": sum(s["summary"]["items"] for s in out),
            "hours": round(sum(s["summary"]["hours"] for s in out), 1),
            "abandoned_hours": abandoned_hours,
            "streams": len(out),
        },
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 2)


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(0.9 * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def response_times(bc: BallCourt, window: tuple[datetime, datetime] | None = None) -> dict:
    """prd/42 M1: how long each person takes to answer a review request.

    For every ``reviewer_changed`` add event, the clock opens at the add and
    stops at that person's first response (approval, needs-work, unapproval or
    comment). A reviewer who never answered is counted as ``never`` — excluded
    from the median, never silently treated as a zero. The sentinel's
    peer→sentinel lag is reported separately: it is the *precedence* wait after
    the last peer approval, not a response to being added.

    The samples are business hours on **the responder's own calendar**
    (concepts.md §1/§2), so a reviewer on vacation is not charged for the
    hours the team worked.
    """
    start, end = window or (
        min((i.start for i in bc.intervals), default=bc.snapshot), bc.snapshot
    )
    sentinels = set(bc.sentinels)
    bots = set(bc.bots)
    samples: dict[str, list[float]] = defaultdict(list)
    never: dict[str, int] = defaultdict(int)
    never_since: dict[str, datetime] = {}
    sentinel_lags: dict[str, list[float]] = defaultdict(list)
    gate_aware_n = 0
    # prd/46 §2: the canonical sentinel-wait samples, one per (sentinel, PR),
    # so the Queue table, the Rules card and the blocking map can quote the
    # same series instead of three clocks.
    sentinel_rows: list[dict] = []
    never_closed = 0
    never_gate_closed = 0
    # prd/40: a reviewer still pending when a PR merged was, ex post, not
    # required; the canonical sentinel wait leaves those samples out.
    unrequired = pending_at_close(bc)
    for state in bc.prs:
        author = state.pr["actor"]
        still_open = state.pr.get("closed_ts") is None
        added: dict[str, datetime] = {}
        answered: set[str] = set()
        approvals: list[tuple[datetime, str]] = []
        # prd/43 §2: the gate opens when every *peer has answered* — a comment
        # or needs-work counts, not only an approval (concepts.md §2).
        peer_responses: list[datetime] = []
        for event in state.events:
            activity = event["activity"]
            actor = event["actor"]
            attrs = event.get("attributes") or {}
            if activity == "reviewer_changed" and attrs.get("change") == "added":
                for person in event.get("people") or ():
                    if person and person != author and person not in bots:
                        added.setdefault(person, event["_ts"])
            if actor in bots or actor == author:
                continue
            responded = activity in RESPONSE_ACTIONS or (
                activity == "reviewer_changed"
                and attrs.get("change") == "reviewed"
            )
            if responded and actor not in answered and (
                actor in added
                # a store with no roster at all (an old scrape, a REST source):
                # the sentinel's wait can only be timed from the PR opening.
                or (actor in sentinels and not added)
            ):
                answered.add(actor)
                # prd/43 §2: the sentinel is a *precedence* constraint. With
                # `sentinel_after_peers` their ball only opens once every peer
                # has answered, so timing them from "added" prices the peer
                # phase as the sentinel's latency (QA: 20 h vs a true ~6 h).
                clock_start = added.get(actor) or state.pr["created_ts"]
                if actor in sentinels and bc.sentinel_after_peers:
                    peers = [
                        stamp for stamp in peer_responses
                        if stamp <= event["_ts"]
                    ]
                    if peers and clock_start is not None and clock_start < max(peers):
                        clock_start = max(peers)
                        if actor in added and clock_start > added[actor]:
                            gate_aware_n += 1
                if clock_start is not None and start <= clock_start < end:
                    hours = bc.business_hours(actor, clock_start, event["_ts"])
                    required = (
                        actor not in unrequired.get(state.pr["object_id"], ())
                    )
                    if required or actor not in sentinels:
                        samples[actor].append(hours)
                    if actor in sentinels and required:
                        sentinel_rows.append({
                            "person": actor,
                            "object_id": state.pr["object_id"],
                            "number": state.pr["pr_id"],
                            "repo": state.pr["repo"],
                            "tickets": list(state.pr.get("ticket_keys") or ()),
                            "start": clock_start,
                            "end": event["_ts"],
                            "hours": round(hours, 2),
                        })
            if responded and actor not in sentinels:
                peer_responses.append(event["_ts"])
            if activity == "pr_approved":
                approvals.append((event["_ts"], actor))
        for person in added:
            if person in answered:
                continue
            # prd/46 §2: "never answered" is only a wait the person *owed*.
            # A PR that merged or was declined/closed by anyone (the bot
            # included) was never their turn — the ex-post rule prd/40 already
            # applies to the blocking map. And a sentinel whose peer gate never
            # opened has not had the ball yet.
            if not still_open:
                never_closed += 1
                continue
            if person in sentinels and bc.sentinel_after_peers:
                peers_added = {
                    peer for peer in added
                    if peer not in sentinels and peer != person
                }
                if not peers_added.issubset(answered):
                    never_gate_closed += 1
                    continue
            never[person] += 1
            # prd/43 §2: keep the forgotten-item tail quantified — the gate
            # opened (or the person was asked) and nothing has answered.
            opened = added[person]
            if person in sentinels and bc.sentinel_after_peers and peer_responses:
                opened = max([added[person], *peer_responses])
            current = never_since.get(person)
            if current is None or opened < current:
                never_since[person] = opened
        # peer approval -> sentinel approval: the gate's own latency
        for stamp, actor in approvals:
            if actor not in sentinels:
                continue
            peers = [
                peer_stamp for peer_stamp, peer in approvals
                if peer not in sentinels and peer_stamp <= stamp
            ]
            if peers and start <= stamp < end:
                sentinel_lags[actor].append(
                    bc.business_hours(actor, max(peers), stamp)
                )

    people: list[dict] = []
    for person in sorted(set(samples) | set(never) | set(sentinel_lags)):
        if person in bots:
            continue
        values = samples.get(person, [])
        lags = sentinel_lags.get(person, [])
        since = never_since.get(person)
        people.append({
            "person": person,
            "role": "sentinel" if person in sentinels else "reviewer",
            "n": len(values),
            "median_hours": _median(values),
            "mean_hours": round(sum(values) / len(values), 2) if values else None,
            "p90_hours": _p90(values),
            "max_hours": round(max(values), 2) if values else None,
            "never": never.get(person, 0),
            # prd/43 §2: the orphan tail, quantified: the oldest unanswered
            # request and how long it has waited (business hours to the snapshot).
            "never_oldest_hours": (
                round(bc.business_hours(person, since, bc.snapshot), 2)
                if since is not None else None
            ),
            "sentinel_lag_n": len(lags),
            "sentinel_lag_median_hours": _median(lags),
            "sentinel_lag_p90_hours": _p90(lags),
        })
    people.sort(key=lambda row: (
        -(row["median_hours"] if row["median_hours"] is not None else -1),
        row["person"],
    ))
    sentinel_values = [
        hours for person in sentinels for hours in samples.get(person, ())
    ]
    sentinel_never = sum(never.get(person, 0) for person in sentinels)
    sentinel_since = [
        never_since[person] for person in sentinels if person in never_since
    ]
    sentinel_wait = {
        "definition": SENTINEL_WAIT_DEFINITION,
        "gate_aware": bool(bc.sentinel_after_peers),
        "sentinels": sorted(sentinels),
        "n": len(sentinel_values),
        "median_hours": _median(sentinel_values),
        "mean_hours": (
            round(sum(sentinel_values) / len(sentinel_values), 2)
            if sentinel_values else None
        ),
        "p90_hours": _p90(sentinel_values),
        "max_hours": round(max(sentinel_values), 2) if sentinel_values else None,
        "never": sentinel_never,
        "never_oldest_hours": (
            round(min(
                bc.business_hours(person, since, bc.snapshot)
                for person, since in (
                    (person, never_since[person]) for person in sentinels
                    if person in never_since
                )
            ), 2)
            if sentinel_since else None
        ),
        "requests": [
            {
                **row,
                "start": iso(row["start"]),
                "end": iso(row["end"]),
            }
            for row in sorted(
                sentinel_rows, key=lambda item: (-item["hours"], item["object_id"])
            )
        ],
    }
    reviewer_values = [
        row["median_hours"] for row in people
        if row["median_hours"] is not None and row["role"] != "sentinel"
    ]
    return {
        "people": people,
        # prd/46 §2: the canonical sentinel-wait number. Every other surface
        # either quotes this block or labels its own clock as a different metric.
        "sentinel_wait": sentinel_wait,
        "sentinels": sorted(sentinels),
        "bots_excluded": sorted(bots),
        "sentinel_after_peers": bc.sentinel_after_peers,
        "gate_aware_n": gate_aware_n,
        # prd/46 §2: "never answered" excludes requests that were never owed —
        # the PR merged or was declined/closed (bot or author) before their turn,
        # or a peer gate that never opened. The counts are stated, not dropped.
        "never_excluded": {
            "closed_prs": never_closed,
            "gate_not_open": never_gate_closed,
        },
        "reviewer_median_hours": _median(reviewer_values),
        "basis": (
            "hours from 'added as reviewer' to that person's first response "
            "(approval, needs-work, unapproval or comment), priced on the "
            "responder's calendar; the sentinel's clock opens at the last peer "
            "answer (their ball only arrives after the peers), so the peer "
            "phase is not charged to them; a reviewer who never answered is "
            "counted under 'never' only while the PR is still open and, for a "
            "sentinel, only once the peer gate has opened — a merged or "
            "declined PR was never their turn, and the age of the oldest open "
            "request is stated"
        ),
        "sentinel_lag_basis": (
            "hours from the last peer approval to the sentinel's approval; "
            "the precedence gate's own wait, not a response to being added"
        ),
        "hours_mode": bc.hours_mode,
    }


def person_legend(bc: BallCourt, stories: list | None = None) -> dict:
    """prd/42 M7: who is who, by role, without leaving the pseudonym contract.

    The store holds no real names (prd/02), so the legend can only say *role*:
    sentinel (mandatory approver), bot, author (opened PRs), reviewer
    (approvals) and story owner (assigned Jira stories). That is enough to
    answer "who is the sentinel" — the one person on every PR — without
    identifying anybody.
    """
    authored: dict[str, int] = defaultdict(int)
    approvals: dict[str, int] = defaultdict(int)
    reviews: dict[str, int] = defaultdict(int)
    for state in bc.prs:
        author = state.pr["actor"]
        if author not in bc.bots:
            authored[author] += 1
        for event in state.events:
            actor = event["actor"]
            if actor in bc.bots or actor == author:
                continue
            if event["activity"] == "pr_approved":
                approvals[actor] += 1
            if event["activity"] in RESPONSE_ACTIONS:
                reviews[actor] += 1
    owned: dict[str, int] = defaultdict(int)
    for story in stories or ():
        actor = getattr(story, "actor", None)
        if actor:
            owned[actor] += 1
    everyone = set(authored) | set(approvals) | set(reviews) | set(owned) | set(bc.bots)
    people = []
    for person in sorted(everyone):
        if person in bc.bots:
            role = "bot"
        elif person in bc.sentinels:
            role = "sentinel"
        elif owned.get(person) and not (authored.get(person) or approvals.get(person)):
            role = "story owner"
        elif authored.get(person):
            role = "author"
        else:
            role = "reviewer"
        people.append({
            "person": person,
            "role": role,
            "sentinel": person in bc.sentinels,
            "bot": person in bc.bots,
            "prs_authored": authored.get(person, 0),
            "approvals": approvals.get(person, 0),
            "review_responses": reviews.get(person, 0),
            "stories_owned": owned.get(person, 0),
        })
    people.sort(key=lambda row: (
        {"sentinel": 0, "bot": 1, "story owner": 2, "author": 3, "reviewer": 4}[row["role"]],
        -row["approvals"], row["person"],
    ))
    return {
        "people": people,
        "note": (
            "roles, not names: the store holds pseudonyms only (prd/02). "
            "sentinel = mandatory approver on every PR; bot = automation, never "
            "a constraint; story owner = assigned Jira stories, not PRs"
        ),
    }


def _parent_keys(prs: dict[str, dict]) -> set[str]:
    """Ticket keys shared by so much of the corpus that they are parent links.

    Every DEL PR carries its epic key (``DEL-1``) next to its story key, so
    counting dependents per key made every open PR "block 130 others" (QA round
    2, B1/B2). There is no epic flag in the store, so this is a stated v1
    heuristic: a key attached to at least a quarter of the corpus (and to at
    least :data:`PARENT_KEY_MIN_PRS` PRs, in a corpus larger than that) is a
    parent/epic link, not a story dependency. It is reported as ``parent_keys``
    in the payload so a reader can check the classification instead of trusting
    it.
    """
    counts: dict[str, int] = defaultdict(int)
    for pr in prs.values():
        for key in pr.get("ticket_keys") or []:
            counts[key] += 1
    total = len(prs)
    # Too small a corpus to tell an epic from a real shared ticket: report the
    # dependency rather than guess it away.
    if total <= PARENT_KEY_MIN_PRS:
        return set()
    threshold = max(PARENT_KEY_MIN_PRS, int(total * PARENT_KEY_SHARE + 0.999))
    return {key for key, count in counts.items() if count >= threshold}


def delivery_list(
    bc: BallCourt,
    window: tuple[datetime, datetime] | None = None,
    stale_business_days: float = STALE_BUSINESS_DAYS,
    done_tickets: set[str] | None = None,
) -> dict:
    """The triage list: open PRs ranked by what waiting on them costs.

    ``waiting_on`` is the truth (concepts.md §2): every human holder — the
    pending reviewers and, while an objection stands, the author — with bots
    excluded. When exactly one person holds the ball, ``ball_label`` **names
    them** (``FrMo``), never "review queue"; with several holders it stays the
    priority label and ``waiting_roles`` says which roles are involved.

    ``stale`` (prd/34 §5) flags a PR open longer than ``stale_business_days``
    *business* days (8 h each), or one whose ticket is already Done in Jira
    (``done_tickets``) while the PR is still open. ``unobserved`` marks a PR
    whose tail has no events since the snapshot: the ball-court excludes that
    tail from the blocking graph, so this list is where the stuck tail stays
    visible.
    """
    start, end = window or (bc.snapshot - timedelta(days=90), bc.snapshot)
    done_tickets = done_tickets or set()
    prs = {s.pr["object_id"]: s.pr for s in bc.prs}
    states = {s.pr["object_id"]: s for s in bc.prs}
    last_event: dict[str, Interval] = {}   # includes the unobserved tail
    last_measured: dict[str, Interval] = {}
    # prd/43 §5: one canonical count per PR — its COMMIT objects. Summing the
    # intervals' commits dropped a commit that landed at the open instant, so
    # the list said "2" while the ticket drawer (len(state.commits)) said "5".
    commit_totals: dict[str, int] = {
        object_id: len(state.commits) for object_id, state in states.items()
    }
    # prd/43 §5: the same business-hour phase spans the ticket drawer shows, so
    # "in review" is one number on every surface (not calendar time here and
    # business hours there).
    phase_hours: dict[str, dict[str, float]] = {}
    for object_id, state in states.items():
        spans: dict[str, float] = defaultdict(float)
        for interval in state.intervals:
            if interval.hours > 0:
                # prd/55 §7: include the unobserved tail, so the drawer's phase
                # list sums to the PR's cycle instead of hiding the 21-day wait.
                spans[interval.phase] += round(interval.hours, 2)
        phase_hours[object_id] = {
            phase: round(hours, 2) for phase, hours in spans.items()
        }
    for interval in bc.intervals:
        previous = last_event.get(interval.object_id)
        if previous is None or interval.end >= previous.end:
            last_event[interval.object_id] = interval
        if not interval.measured:
            continue
        previous = last_measured.get(interval.object_id)
        if previous is None or interval.end >= previous.end:
            last_measured[interval.object_id] = interval

    # Dependents are *other open* PRs on the same story ticket (QA round 2, B1):
    # closed PRs cannot be blocked by a wait, and the shared epic key is not a
    # dependency. A PR is counted once even when two of its keys match.
    parent_keys = _parent_keys(prs)
    open_tickets: dict[str, set[str]] = defaultdict(set)
    for object_id, pr in prs.items():
        if pr["closed_ts"] is not None:
            continue
        for key in pr.get("ticket_keys") or []:
            if key not in parent_keys:
                open_tickets[key].add(object_id)

    rows = []
    for object_id, pr in prs.items():
        if pr["closed_ts"] is not None:
            continue
        tail = last_event.get(object_id)
        current = last_measured.get(object_id)
        # the current wait starts when the last event handed the ball over
        waiting_since = tail.start if tail else (current.start if current else pr["created_ts"])
        # everyone the item is waiting on right now (prd/40): the pending
        # reviewers and, while a reviewer's objection stands, the author too.
        # ``holders`` already excludes bots (concepts.md §2).
        author_open = bool(tail.author_open if tail else (current.author_open if current else False))
        roster = tail.roster if tail else (current.roster if current else ())
        # prd/41 B6: who *owes* is every reviewer with status ``none`` — the
        # sentinel included even while the precedence gate defers its charging
        # clock — plus the author while their debt stands. ``holders`` stays the
        # gated set behind ``ball_label``/the person-hour projections.
        latest = tail if tail is not None else current
        owes = tuple(
            person for person in (latest.pending_all if latest is not None else ())
            if person not in SYNTHETIC
        )
        if roster or owes:
            waiting_on = sorted(
                set(owes) | ({pr["actor"]} if author_open and pr["actor"] not in bc.bots else set())
            )
        else:
            # no roster in the store (an old scrape): the responder is the only
            # evidence of who was asked
            waiting_on = [
                person for person in (tail.holders if tail else (current.holders if current else ()))
                if person not in SYNTHETIC
            ]
        pending = [person for person in waiting_on if person != pr["actor"]]
        approved = list(latest.approved if latest is not None else ())
        ball = (tail.ball if tail else None) or (current.ball if current else "unknown")
        # Name the owner when exactly one person holds the ball (concepts.md §2):
        # the sentinel, a reviewer, or the author. Several holders keep the
        # priority label; ``waiting_roles`` says which queues they are.
        if ball in (REVIEW, "sentinel"):
            role_label = REVIEW
        else:
            role_label = ball
        owner = (tail.owner if tail else None) or (current.owner if current else None)
        waiting_for = owner or role_label
        waiting_roles = {
            person: (
                "author" if person == pr["actor"]
                else ("sentinel" if person in bc.sentinels else "review")
            )
            for person in waiting_on
        }
        ball_label = (
            (tail.ball_label if tail else None)
            or (current.ball_label if current else None)
            or role_label
        )
        waiting_hours = _hours(
            bc.business_hours(waiting_for, waiting_since, bc.snapshot) * 3600.0
        )
        age_hours = _hours(
            bc.business_hours(None, pr["created_ts"], bc.snapshot) * 3600.0
        )
        story_keys = [k for k in (pr.get("ticket_keys") or []) if k not in parent_keys]
        dependents = len(
            set().union(*(open_tickets.get(key, set()) for key in story_keys))
            - {object_id}
        ) if story_keys else 0
        kind = (tail.kind if tail else None) or "no_events"
        # prd/34 §5: the stale signal. Both reasons are stated per row so the
        # rule (prd/37) and this list quote the same fact.
        age_business_days = round(age_hours / BUSINESS_HOURS_PER_DAY, 2)
        done_keys = sorted(key for key in story_keys if key in done_tickets)
        stale_reasons = []
        if age_business_days > stale_business_days:
            stale_reasons.append(
                f"open {age_business_days:g} business days (limit {stale_business_days:g})"
            )
        if done_keys:
            stale_reasons.append(
                f"ticket {', '.join(done_keys)} is Done in Jira but this PR is open"
            )
        unobserved = bool(tail is not None and tail.phase == SNAPSHOT_PHASE)
        # prd/55 §4: the phase derives from the ball, never from the interval
        # that just ended. #53's last measured phase was `active` (the author's
        # final push) while two reviewers held the ball for 21 days; the list
        # called it active and "waiting on author: no" on the same row.
        latest = tail if tail is not None else current
        if latest is not None and latest.pending_all:
            phase_now = "review"
        elif latest is not None and latest.ci and not latest.author_open:
            phase_now = "ci"
        elif latest is not None and (latest.author_open or latest.ball == "author"):
            phase_now = latest.phase if latest.phase in ("active", "rebasing") else (
                current.phase if current is not None else "active"
            )
        elif latest is not None and latest.ball == READY:
            phase_now = "review"
        else:
            phase_now = current.phase if current else "no events"
        rows.append({
            "object_id": object_id,
            "id": f"{pr['repo'].split('/')[-1]}#{pr['pr_id']}",
            "repo": pr["repo"],
            "repo_short": pr["repo"].split("/")[-1],
            "number": pr["pr_id"],
            "tickets": pr.get("ticket_keys") or [],
            "story_tickets": story_keys,
            "author": pr["actor"],
            "state": pr["state"],
            "ball": ball,
            "waiting_for": waiting_for,
            "waiting_on": waiting_on,
            "waiting_roles": waiting_roles,
            "ball_label": ball_label,
            "stale": bool(stale_reasons),
            "stale_reasons": stale_reasons,
            "stale_business_days": stale_business_days,
            "age_business_days": age_business_days,
            "unobserved": unobserved,
            "unobserved_since": iso(tail.start) if unobserved else None,
            "author_open": author_open,
            "doubly_blocked": bool(author_open and pending),
            "ready_since": iso(waiting_since) if ball == READY else None,
            "reviewers_total": len(roster),
            "reviewers_pending": len(pending),
            "reviewers_approved": len(approved),
            "phase": phase_now,
            "last_phase": current.phase if current else "no events",
            "kind": kind,
            # prd/43 §5: the drawer's "last event" names the *raw* activity the
            # ticket view also quotes (``comment_added``, not ``author_reply``),
            # so the two drawers cannot label the same event differently.
            "last_event": iso(tail.start) if tail is not None else None,
            "last_event_activity": (
                (tail.activity or kind) if tail is not None else kind
            ),
            "last_event_actor": tail.by if tail is not None else None,
            "ci": bool(latest.ci if latest is not None else False),
            "rebasing": bool(current is not None and current.phase == "rebasing"),
            "waiting_since": iso(waiting_since),
            "waiting_hours": waiting_hours,
            "age_hours": age_hours,
            "blocked": ball != READY and kind in (
                "pr_needs_work", "pr_unapproved", "needs_work", "blocker_comment", "unapproved"
            ),
            "dependents": dependents,
            "impact_hours": round(dependents * waiting_hours, 1),
            "measured_hours": round(current.hours, 1) if current else 0.0,
            "commits": commit_totals.get(object_id, 0),
            # prd/43 §5: business-hour spans by phase, the same source the
            # ticket drawer reads, so "in review" cannot be a calendar span here
            # and a business span there.
            "phase_hours": phase_hours.get(object_id) or {},
            "in_window": bool(tail and tail.start < end and tail.end > start),
        })

    rows.sort(key=lambda r: (-r["blocked"], -r["impact_hours"], -r["waiting_hours"]))
    blocked = [r for r in rows if r["blocked"]]
    # prd/41 B3: one threshold — a working week is 40 business hours
    # (concepts.md §1) — behind every "waiting over a week" label.
    oldest = [
        r for r in rows
        if not r["blocked"] and r["waiting_hours"] >= WAITING_WEEK_HOURS
    ]
    ready = [r for r in rows if r["ball"] == READY]
    return {
        "ok": True,
        "hours_mode": bc.hours_mode,
        "assumption": bc.assumption,
        **bc.calendar_warning(),
        "impact_note": (
            "impact = other OPEN PRs on the same story ticket × current wait (≈); "
            "shared parent/epic keys are excluded and a PR counts once"
        ),
        "parent_keys": sorted(parent_keys),
        "parent_key_note": (
            f"v1 heuristic: a ticket key on >= {int(PARENT_KEY_SHARE * 100)}% of PRs "
            f"(min {PARENT_KEY_MIN_PRS}) is treated as a parent/epic link"
        ),
        "snapshot": iso(bc.snapshot),
        "hours_kind": "wait-hours",
        "bots_excluded": sorted(bc.bots),
        "scope_note": (
            "the person scope means 'waiting on X' here and in the queue view: a "
            "row matches when X is one of its holders (pending reviewer or the "
            "author whose debt stands), not when X authored it"
        ),
        "stale_note": (
            f"stale = open > {stale_business_days:g} business days "
            f"({BUSINESS_HOURS_PER_DAY:g} h each) or a ticket that is Done in "
            "Jira while its PR is still open"
        ),
        "unobserved_note": (
            "unobserved = no events since the snapshot: the blocking graph "
            "excludes that tail, so the row stays here rather than disappearing"
        ),
        "window": {"start": iso(start), "end": iso(end)},
        "counts": {
            "open": len(rows),
            "blocked": len(blocked),
            "ready": len(ready),
            "doubly_blocked": len([r for r in rows if r["doubly_blocked"]]),
            "oldest_waiting": len(oldest),
            "all": len(rows),
            # prd/41 B3: the footer's number and the row pill read the same
            # threshold; the constant travels so no view hardcodes its own.
            "waiting_over_week": len(
                [r for r in rows if r["waiting_hours"] >= WAITING_WEEK_HOURS]
            ),
            "waiting_week_hours": WAITING_WEEK_HOURS,
            "waiting_week_days": round(
                WAITING_WEEK_HOURS / BUSINESS_HOURS_PER_DAY, 1
            ),
            "stale": len([r for r in rows if r["stale"]]),
            "unobserved": len([r for r in rows if r["unobserved"]]),
        },
        "rows": rows,
    }


def window_from_events(bc: BallCourt, days: float | None) -> tuple[datetime, datetime]:
    """Turn a timeframe (days, or None for everything) into a clamp on the data."""
    first = min((i.start for i in bc.intervals), default=bc.snapshot)
    if not days:
        return first, bc.snapshot
    start = max(first, bc.snapshot - timedelta(days=days))
    return start, bc.snapshot

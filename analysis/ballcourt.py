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

**Attribution semantics** (product review #7). A PR in review waits on *every*
reviewer who was asked and has not answered yet, not on whichever reviewer
happened to act first. The scrape records the roster as ``reviewer_changed``
events (``added`` / ``removed`` / ``reviewed``, each carrying the person as an
object link); this module replays that roster and keeps a *pending* set per
review cycle:

* a review cycle starts when the ball moves back to review (opened, pushed,
  rebased, author reply) — at that point everyone on the roster owes a response;
* a reviewer who acts (approve, needs-work, unapprove, reviewed) leaves the
  pending set for the rest of that cycle;
* an added reviewer joins the pending set, a removed one leaves it.

``Interval.ball_people`` is therefore the set of **concurrent ball-holders**,
and the person-level projections (blocking graph, queue series, responsiveness)
attribute the wait to each of them — a slow non-responder on a three-reviewer PR
is visible instead of being hidden behind the first responder. Consequences:

* person-level hours are *person-hours*: a 10 h wait on two reviewers is 10 h of
  blocking for each, so the person-level total is larger than the wall-clock
  wait it came from (role/queue level still counts it once, under ``review``);
* when the store has no roster (an old scrape, a REST-only source) the interval
  falls back to the responder — the previous behaviour — and the wait stays on
  the synthetic ``review`` queue rather than being invented onto a person;
* an interval has one duration: business hours are priced with the responder's
  calendar (or the default window for a queue wait), not per holder.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from ingest.store import iso, to_naive_utc
from analysis.calendar import Calendar

ASSUMPTION = "wall-clock hours (no working-hours calendar configured)"

SNAPSHOT_PHASE = "unobserved"
REVIEW = "review"
CI = "CI"
CALENDAR = "calendar"

PHASE_ORDER = ("waiting", "active", "review", "ci", "rebasing", "closed", SNAPSHOT_PHASE)

# activities that mean "a reviewer did something", i.e. the interval *before*
# them was review time
REVIEWER_ACTIONS = {"pr_approved", "pr_unapproved", "pr_needs_work", "reviewer_changed"}
# activities that mean "the author did something"
AUTHOR_ACTIONS = {"pr_pushed", "rebased"}
TERMINAL = {"pr_merged", "pr_declined"}

# A ticket key on this share of the corpus (and at least this many PRs) is a
# parent/epic link, not a story dependency. Stated in the delivery payload.
PARENT_KEY_SHARE = 0.25
PARENT_KEY_MIN_PRS = 3

def _hours(seconds: float) -> float:
    return round(max(0.0, seconds) / 3600.0, 2)


@dataclass(slots=True)
class Interval:
    """One stretch of time during which a single party held a single PR."""

    object_id: str
    repo: str
    number: int
    tickets: tuple[str, ...]
    author: str
    start: datetime
    end: datetime
    phase: str
    ball: str               # pseudonym | 'review' | 'CI' | 'calendar'
    ball_person: str | None  # the person who actually responded, when known
    by: str                 # actor who handed the ball over
    kind: str               # opened | push:content | needs_work | approved | ...
    evidence: str | None    # commits | push | approval | none
    commits: int
    open: bool
    # concurrent ball-holders at person resolution (product review #7): every
    # reviewer who was asked and had not answered yet, not just the responder.
    ball_people: tuple[str, ...] = ()
    # business hours (prd/19): seconds inside the person's working time, set by
    # ``load`` when a calendar is attached. ``None`` means wall-clock.
    business_seconds: float | None = None

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())

    @property
    def hours(self) -> float:
        if self.business_seconds is not None:
            return _hours(self.business_seconds)
        return _hours(self.seconds)

    @property
    def measured(self) -> bool:
        return self.phase != SNAPSHOT_PHASE

    @property
    def waited_on(self) -> str | None:
        """Who the ball was with, at person resolution when the store allows it."""
        if self.ball_person:
            return self.ball_person
        if self.ball == "author":
            return self.author
        if self.ball in ("review", "sentinel"):
            return REVIEW
        return self.ball

    @property
    def holders(self) -> tuple[str, ...]:
        """Every party the ball was concurrently with (person resolution).

        Falls back to :attr:`waited_on` so a store without a reviewer roster
        keeps the old, single-holder answer instead of inventing people.
        """
        if self.ball_people:
            return self.ball_people
        waited = self.waited_on
        return (waited,) if waited else ()



@dataclass(slots=True)
class PRState:
    pr: dict
    events: list[dict]
    commits: list[datetime]
    intervals: list[Interval]


@dataclass
class BallCourt:
    intervals: list[Interval]
    prs: list[PRState]
    snapshot: datetime
    sentinels: set[str]
    events: int
    calendar: Calendar | None = None
    calendar_error: str | None = None

    @property
    def open_prs(self) -> list[PRState]:
        return [pr for pr in self.prs if not pr.pr.get("closed_at")]

    @property
    def hours_mode(self) -> str:
        """``business`` with a calendar (prd/19), ``wall-clock`` without one."""
        return "business" if self.calendar is not None else "wall-clock"

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


# --------------------------------------------------------------------------- #
# reconstruction
# --------------------------------------------------------------------------- #
def _state_after(pr: dict, state: dict, event: dict) -> dict | None:
    """Advance the ball-in-court state machine by one event.

    Also maintains the two sets the attribution needs: ``reviewers`` (the roster
    this PR has accumulated) and ``pending`` (who still owes a response in the
    current review cycle).
    """
    activity = event["activity"]
    actor = event["actor"]
    attrs = event.get("attributes") or {}
    author = pr["actor"]
    since = event["_ts"]

    reviewers = set(state.get("reviewers") or ())
    pending = set(state.get("pending") or ())

    # -- roster bookkeeping (product review #7) -------------------------------
    if activity == "reviewer_changed":
        people = {p for p in (event.get("people") or ()) if p and p != author}
        change = attrs.get("change")
        if change == "added":
            reviewers |= people
            pending |= people
        elif change == "removed":
            reviewers -= people
            pending -= people
        elif change == "reviewed" and actor != author:
            # a completed review proves the person was asked
            reviewers.add(actor)
            pending.discard(actor)
    elif activity in ("pr_approved", "pr_needs_work", "pr_unapproved") and actor != author:
        reviewers.add(actor)
        pending.discard(actor)

    base = {"reviewers": frozenset(reviewers), "pending": frozenset(pending)}

    # -- ball transitions -----------------------------------------------------
    if activity in TERMINAL:
        return None
    if activity in ("pr_opened", "pr_pushed", "rebased"):
        # the ball goes back to review: everyone on the roster owes a response
        base["pending"] = frozenset(reviewers)
    if activity in ("pr_opened", "pr_pushed", "pr_approved", "reviewer_changed"):
        kind = activity
        if activity == "pr_pushed":
            kind = f"push:{attrs.get('push_class') or 'other'}"
        return {**base, "ball": REVIEW, "by": actor, "kind": kind, "since": since}
    if activity in ("pr_unapproved", "pr_needs_work"):
        return {**base, "ball": "author", "by": actor, "kind": activity, "since": since}
    if activity == "comment_added":
        if actor == author:
            # an author reply hands it back to the reviewers: a new response round
            return {
                **base, "pending": frozenset(reviewers),
                "ball": REVIEW, "by": actor, "kind": "author_reply", "since": since,
            }
        if attrs.get("severity") == "mandatory":
            return {**base, "ball": "author", "by": actor, "kind": "blocker_comment", "since": since}
        # a suggestion does not move the ball: keep whoever has it
        previous = state.get("ball") if state else REVIEW
        return {**base, "ball": previous or REVIEW, "by": actor, "kind": "comment", "since": since}
    if activity == "rebased":
        return {**base, "ball": REVIEW, "by": actor, "kind": "rebase", "since": since}
    if activity == "build_status":
        return {**base, "ball": CI, "by": actor, "kind": "ci", "since": since}
    if activity in ("question_raised", "question_answered"):
        previous = state.get("ball") if state else REVIEW
        return {**base, "ball": previous or REVIEW, "by": actor, "kind": activity, "since": since}
    # anything else is outside this slice's vocabulary but must not crash
    return {**state, **base}



def _phase_for(state: dict, pr_author: str, next_event: dict, commits_in: int) -> tuple[str, str]:
    """The phase of the interval *before* ``next_event``, and its evidence."""
    ball = state["ball"]
    if ball == CI:
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
    if ball == "author":
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


def _person_for(pr: dict, state: dict, next_event: dict) -> str | None:
    """Best available person for the waiting side of an interval.

    ``review`` is a queue, so the responder is only known ex post: it is the
    reviewer who eventually acted. That is a measured fact (this wait ended with
    that person's action), not an assignment of blame — and it is *not* the only
    party the item waited on: :func:`_holders` carries the concurrent holders.
    """
    if state["ball"] == "author":
        return pr["actor"]
    if state["ball"] == CI:
        return None
    if state["ball"] in ("review", "calendar"):
        actor = next_event["actor"]
        if actor != pr["actor"] and next_event["activity"] in REVIEWER_ACTIONS:
            return actor
        if next_event["activity"] == "comment_added" and actor != pr["actor"]:
            return actor
        return None
    return state["ball"]


def _holders(pr: dict, state: dict, next_event: dict | None) -> tuple[str, ...]:
    """Every party the ball was concurrently with (product review #7).

    For a review wait that is the *pending* roster — the reviewers who were
    asked and had not answered when the interval started — plus whoever acted at
    the end of it (a responder who was never on the roster was still a holder).
    With no roster in the store this is empty and :attr:`Interval.holders`
    falls back to the responder, i.e. the old behaviour.
    """
    ball = state.get("ball")
    author = pr["actor"]
    if ball == "author":
        return (author,)
    if ball in (CI, CALENDAR):
        return ()
    if ball in (REVIEW, "sentinel"):
        people = {p for p in (state.get("pending") or ()) if p and p != author}
        if next_event is not None:
            actor = next_event["actor"]
            activity = next_event["activity"]
            if actor != author and (
                activity in REVIEWER_ACTIONS or activity == "comment_added"
            ):
                people.add(actor)
        return tuple(sorted(people))
    return (ball,) if ball else ()



def _timeline(state: PRState, snapshot: datetime, open_pr: bool) -> None:
    pr = state.pr
    commits = state.commits
    author = pr["actor"]
    person_events = [e for e in state.events if e["_ts"] >= pr["created_ts"]]
    if not person_events:
        return

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
                    ball=current["ball"], ball_person=_person_for(pr, current, event),
                    ball_people=_holders(pr, current, event),
                    by=current["by"], kind=current["kind"], evidence=evidence,
                    commits=commits_in, open=open_pr,
                )
            )
        current = _state_after(pr, current or {}, event)

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
                ball_people=_holders(pr, current, None),
                by=current["by"],
                kind=current["kind"], evidence=evidence, commits=0, open=open_pr,
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
OBJECT_SQL = (
    "SELECT object_type, object_id, repo, pr_id, ticket_keys, actor, state, created_at, closed_at"
    " FROM objects ORDER BY object_type, object_id"
)


def _snapshot(conn) -> datetime:
    """The ingest watermark: what the store knew, and when."""
    row = conn.execute("SELECT max(as_of) FROM ingest_runs").fetchone()
    if row and row[0]:
        return to_naive_utc(row[0])
    row = conn.execute("SELECT max(ts) FROM events").fetchone()
    return to_naive_utc(row[0]) if row and row[0] else datetime.utcnow()


def load(conn, calendar: Calendar | None = None, calendar_error: str | None = None) -> BallCourt:
    """Read the store and reconstruct every PR's ball-in-court timeline.

    Reads the tables directly (rather than through ``ingest.store``'s read
    helpers) so the analysis views depend only on the schema — the
    pseudonymisation contract — and not on ingest refactors.

    ``calendar`` (prd/19) converts each interval to business hours, using the
    person the ball was with: a reviewer's own override, or the default window
    for an unattributed queue/CI wait. Without it every duration stays
    wall-clock and ``hours_mode`` says so.
    """
    snapshot = _snapshot(conn)
    people_by_event: dict[str, list[str]] = defaultdict(list)
    for event_key, object_id in conn.execute(LINK_SQL).fetchall():
        person = object_id.partition(":")[2]
        if person:
            people_by_event[event_key].append(person)
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

    by_pr: dict[str, list[dict]] = defaultdict(list)
    sentinels: set[str] = set()
    for event in events:
        if (event.get("attributes") or {}).get("sentinel") is True:
            sentinels.add(event["actor"])
        key = f"PR:{event['repo']}#{event['pr_id']}"
        if key in pr_objects:
            by_pr[key].append(event)

    states: list[PRState] = []
    for object_id, pr in pr_objects.items():
        pr_events = sorted(by_pr.get(object_id, []), key=lambda e: (e["_ts"], e["key"]))
        commits = sorted(commits_by_pr.get((pr["repo"], pr["pr_id"]), []))
        state = PRState(pr=pr, events=pr_events, commits=commits, intervals=[])
        _timeline(state, snapshot, open_pr=pr["closed_ts"] is None)
        states.append(state)

    intervals = [i for s in states for i in s.intervals]
    intervals.sort(key=lambda i: (i.start, i.object_id))
    if calendar is not None:
        for interval in intervals:
            interval.business_seconds = calendar.business_hours(
                interval.waited_on, interval.start, interval.end
            ) * 3600.0
    return BallCourt(
        intervals=intervals, prs=states, snapshot=snapshot, sentinels=sentinels,
        events=len(events), calendar=calendar, calendar_error=calendar_error,
    )


# --------------------------------------------------------------------------- #
# projections
# --------------------------------------------------------------------------- #
def _queue_of(bc: BallCourt, person: str, pr: dict | None, side: str) -> str:
    """Contextual role label for an edge endpoint."""
    if person in (REVIEW, CI, CALENDAR):
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
            if person in (REVIEW, CI, CALENDAR):
                return person, "synthetic"
            return person, "person"
        queue = _queue_of(bc, person, pr, side)
        if person not in (REVIEW, CI, CALENDAR):
            role_people[queue].add(person)
        return queue, "role"

    measured = [i for i in bc.intervals if i.measured and i.start < end and i.end > start]
    for interval in measured:
        pr = prs.get(interval.object_id)
        waited_on = interval.waited_on
        # Person level: every concurrent holder is charged the wait (product
        # review #7). Role/queue level keeps one edge per wait — the queue is
        # the same whether one reviewer or three were asked.
        if level == "person":
            targets = interval.holders
        else:
            targets = (waited_on,) if waited_on else ()
        if not targets:
            continue
        x, xkind = endpoint(interval.by, pr, "by")
        hours = interval.hours
        for person in targets:
            y, ykind = endpoint(person, pr, "ball")
            if x == y:
                continue
            edge = edges.setdefault((x, y), {"from": x, "to": y, "hours": 0.0, "count": 0, "prs": set()})
            edge["hours"] += hours
            edge["count"] += 1
            edge["prs"].add(interval.object_id)
            for node_id, kind in ((x, xkind), (y, ykind)):
                entry = nodes.setdefault(node_id, {
                    "id": node_id, "label": node_id, "kind": kind, "hours": 0.0,
                    "out_hours": 0.0, "count": 0, "wip_now": 0, "per_day": 0.0, "people": [],
                })
                if node_id == y:
                    entry["hours"] += hours       # waited on by others (inbound)
                    entry["count"] += 1
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
        entry["per_day"] = round(entry["hours"] / days, 2)
    out_edges.sort(key=lambda e: e["hours"], reverse=True)

    total = round(sum(e["hours"] for e in out_edges), 1)
    return {
        "ok": True,
        "hours_mode": bc.hours_mode,
        "assumption": bc.assumption,
        **bc.calendar_warning(),
        "attribution": (
            "a wait is attributed to every party the ball was concurrently with; "
            "at person level that is every reviewer still pending on the PR (a "
            "wait on N reviewers is N person-hours, not just the first responder)"
            if level == "person" else
            "a wait is attributed to the party the ball was with; the review "
            "queue counts one wait per PR, however many reviewers were asked"
        ),
        "hours_kind": "person-hours" if level == "person" else "wait-hours",
        "level": level,
        "window": {"start": iso(start), "end": iso(end), "days": round(days, 1)},
        "nodes": sorted(nodes.values(), key=lambda n: (-n["hours"], n["id"])),
        "edges": out_edges,
        "totals": {
            "nodes": len(nodes),
            "edges": len(out_edges),
            "hours": total,
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

    At person level every concurrent holder is charged the wait (product review
    #7): a PR waiting on three reviewers arrives in all three queues, so the
    per-person ``hours`` are person-hours. The queue level still counts one wait
    per PR under the role/queue name.
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
        if name in (REVIEW, CI, CALENDAR):
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
            "count": 0,
            "prs": set(),
        })

    measured = [i for i in bc.intervals if i.measured and i.start < end and i.end > start]
    for interval in measured:
        pr = prs.get(interval.object_id) or {}
        # Person level charges every concurrent holder (product review #7);
        # the queue level keeps one wait per PR under the role/queue name.
        if level == "person":
            people = interval.holders
        else:
            people = (interval.waited_on,) if interval.waited_on else ()
        for person in people:
            if not person:
                continue
            name = key_for(person, pr)
            entry = stream(name)
            if level == "queue" and person not in (REVIEW, CI, CALENDAR):
                entry["people"].append(person)
            entry["hours"] += interval.hours
            entry["count"] += 1
            entry["prs"].add(interval.object_id)
            if not interval.open:
                entry.setdefault("served", set()).add(interval.object_id)

            # arrivals / departures land in the bucket that contains the timestamp
            for stamp, series in ((interval.start, entry["arrivals"]), (interval.end, entry["completions"])):
                b = int((stamp - start).total_seconds() // (bucket_hours * 3600))
                if 0 <= b < buckets:
                    series[b] += 1
            # WIP is sampled at bucket midpoints: each item in the queue counts once
            for b in range(buckets):
                at = start + timedelta(hours=(b + 0.5) * bucket_hours)
                if interval.start <= at < interval.end:
                    entry["wip"][b] += 1

    days = span_hours / 24.0
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
        per_day = served / days if days else 0.0
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
                "mean_wait_hours": round(mean_hours, 1),
                "throughput_per_day": round(per_day, 2),
                "diagnosis": diagnosis,
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
            "heuristic vs. the median wait in this window; queue length divides "
            "wait-hours by the hours the queue could be served in"
        ),
        "level": level,
        "window": {
            "start": iso(start), "end": iso(end), "days": round(days, 1),
            "service_hours": round(service_hours, 1),
        },
        "bucket_hours": bucket_hours,
        "buckets": buckets,
        "baseline_mean_wait_hours": round(baseline, 1),
        "streams": out,
        "totals": {
            "items": sum(s["summary"]["items"] for s in out),
            "hours": round(sum(s["summary"]["hours"] for s in out), 1),
            "streams": len(out),
        },
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


def delivery_list(bc: BallCourt, window: tuple[datetime, datetime] | None = None) -> dict:
    """The triage list: open PRs ranked by what waiting on them costs."""
    start, end = window or (bc.snapshot - timedelta(days=90), bc.snapshot)
    prs = {s.pr["object_id"]: s.pr for s in bc.prs}
    last_event: dict[str, Interval] = {}   # includes the unobserved tail
    last_measured: dict[str, Interval] = {}
    commit_totals: dict[str, int] = defaultdict(int)
    for interval in bc.intervals:
        previous = last_event.get(interval.object_id)
        if previous is None or interval.end >= previous.end:
            last_event[interval.object_id] = interval
        if not interval.measured:
            continue
        commit_totals[interval.object_id] += interval.commits
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
        waiting_for = (tail.waited_on if tail else None) or (current.waited_on if current else None)
        # everyone the item is waiting on right now (product review #7): an open
        # PR in review lists its pending reviewers, not just the queue name.
        waiting_on = [
            person for person in (tail.holders if tail else (current.holders if current else ()))
            if person not in (REVIEW, CI, CALENDAR)
        ]
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
            "ball": tail.ball if tail else "unknown",
            "waiting_for": waiting_for,
            "waiting_on": waiting_on,
            "ball_label": waiting_for or (tail.ball if tail else "unknown"),
            "phase": current.phase if current else "no events",
            "kind": kind,
            "waiting_since": iso(waiting_since),
            "waiting_hours": waiting_hours,
            "age_hours": age_hours,
            "blocked": kind in ("pr_needs_work", "pr_unapproved", "needs_work", "blocker_comment", "unapproved"),
            "dependents": dependents,
            "impact_hours": round(dependents * waiting_hours, 1),
            "measured_hours": round(current.hours, 1) if current else 0.0,
            "commits": commit_totals.get(object_id, 0),
            "in_window": bool(tail and tail.start < end and tail.end > start),
        })

    rows.sort(key=lambda r: (-r["blocked"], -r["impact_hours"], -r["waiting_hours"]))
    blocked = [r for r in rows if r["blocked"]]
    oldest = [r for r in rows if not r["blocked"] and r["waiting_hours"] >= 72]
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
        "window": {"start": iso(start), "end": iso(end)},
        "counts": {
            "open": len(rows),
            "blocked": len(blocked),
            "oldest_waiting": len(oldest),
            "all": len(rows),
            "waiting_over_week": len([r for r in rows if r["waiting_hours"] >= 168]),
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

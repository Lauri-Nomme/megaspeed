"""Jira-layer analysis (prd/21): where the *ticket* waits.

The PR half of the store answers "how long did the code wait". The Jira half
answers the question before it: a story sits in a backlog, gets refined, waits
for a sprint, starts, opens a PR, goes through review, merges, and only then is
it closed. prd/21 calls that the **waiting-time decomposition**:

    created ─► refined ─► sprint ─► in progress ─► PR open ─► first review
            ─► review rounds ─► final approval ─► merge ─► done
    └ ready→refined ┘└ refined→sprint ┘└ sprint→in-progress ┘└ in-progress→PR-open ┘
                     └ PR-open→first-review ┘└ review rounds ┘
                              └ final-approval→merge ┘└ merge→done ┘

The eight segments are built from three sources, joined on the story key:

* the story's own events — ``created``, ``status_changed`` (both ends are
  status *categories*, never a workflow name), ``pointed``, ``assigned``,
  ``sprint_added`` / ``sprint_removed``, ``worklogged``;
* its Sprint objects (start / planned end / completion);
* its PRs, via ``PR.ticket_keys`` — the same key on both sides of the store, so
  the join is the store's own identity resolution and not a heuristic.

Everything else in this module follows from those rows:

======================  ==========================================================
``waiting_time_decomposition``  the 8 segments, hours + per-segment flow efficiency
``carry_over``          stories added to more than one sprint
``point_churn``         ``pointed`` events after the story started
``worklog_cross_check`` ``log_delay_bucket`` distribution + logged vs active hours
``jira_conformance``    started before refined, re-pointed after start, carry-over
======================  ==========================================================

Honesty rules this module follows, in the same spirit as ``ballcourt``:

* **Boundaries are measured, not assumed.** A segment whose two boundaries the
  store cannot show is ``available: false`` and counted in ``missing`` (with
  which boundary was missing) — never filled with an estimate. The refinement
  signal is the first ``pointed`` event (an estimate is set in refinement);
  the payload says so.
* **Business hours when a calendar is attached, wall-clock otherwise** — the
  same ``court.business_hours`` helper every other metric uses, so the two
  halves of the store are priced on one clock.
* **Flow efficiency credits evidence, not elapsed time.** Active time inside a
  segment is the union of measured work spans (PR intervals where the author
  was demonstrably working, worklog durations) plus a stated attention cost per
  touch. It is clamped to the segment, and the clamp count is reported.
* **No Jira data is a result, not an error.** A store with no STORY objects
  reports ``available: false`` with a note on every Jira metric and emits no
  suggestion — the PR-side metrics are unaffected.

Read-only: this module reads the schema directly, like ``ballcourt``, so the
analysis views never depend on ingest refactors.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from analysis import ballcourt
from analysis.effort import (
    ACTIVE_PHASES, TICKET_TOUCHES, TOUCH_ACTIVITIES, TOUCH_HOURS,
)
from ingest.store import iso, to_naive_utc

# --------------------------------------------------------------------------- #
# the eight segments (prd/21)
# --------------------------------------------------------------------------- #
SEGMENTS: tuple[tuple[str, str], ...] = (
    ("ready_refined", "ready → refined"),
    ("refined_sprint", "refined → sprint"),
    ("sprint_in_progress", "sprint → in progress"),
    ("in_progress_pr_open", "in progress → PR open"),
    ("pr_open_first_review", "PR open → first review"),
    ("review_rounds", "review rounds"),
    ("final_approval_merge", "final approval → merge"),
    ("merge_done", "merge → done"),
)
SEGMENT_KEYS = tuple(key for key, _ in SEGMENTS)
SEGMENT_LABEL = dict(SEGMENTS)
#: Which two boundaries delimit each segment. A segment is measured only when
#: both exist for that story.
SEGMENT_BOUNDS: dict[str, tuple[str, str]] = {
    "ready_refined": ("created", "refined"),
    "refined_sprint": ("refined", "sprint"),
    "sprint_in_progress": ("sprint", "started"),
    "in_progress_pr_open": ("started", "pr_open"),
    "pr_open_first_review": ("pr_open", "first_review"),
    "review_rounds": ("first_review", "approval"),
    "final_approval_merge": ("approval", "merged"),
    "merge_done": ("merged", "done"),
}

NO_JIRA_NOTE = (
    "no Jira data in this store (no STORY objects): the ticket layer needs the "
    "Jira ingest (prd/04). The PR-side metrics are unaffected."
)

# v1 targets, stated in every payload that uses them (never a hidden constant).
CARRY_OVER_SHARE_TARGET = 0.10     # ≤ 10% of stories may move between sprints
POINT_CHURN_SHARE_TARGET = 0.10    # ≤ 10% may be re-pointed after work started
WORKLOG_DELAY_TARGET_DAYS = 1.0    # logging same/next day is the honest case
WORKLOG_LATE_SHARE_TARGET = 0.25   # > 25% logged a week late is not a record
WORKLOG_RATIO_LOW = 0.5            # logged ÷ active below this: under-logged
WORKLOG_RATIO_HIGH = 2.0           # above this: logged time is not the work
SEGMENT_SHARE_TARGET = 0.25        # one segment over a quarter of the cycle
SEGMENT_SHARE_CRITICAL = 0.40

REVIEW_ACTIONS = ("comment_added", "pr_approved")
LOG_DELAY_BUCKETS = ("same_day", "next_day", "within_week", "late")
#: What one refinement costs when the store cannot measure it (a fallback, and
#: stated as such in the payload).
DEFAULT_REFINEMENT_HOURS = 1.0


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _median(values: list[float], digits: int = 2) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], digits)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, digits)


def _percentile(values: list[float], q: float, digits: int = 2) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return round(ordered[index], digits)


def _p90(values: list[float]) -> float | None:
    return _percentile(values, 0.9)


def _share(part: float, whole: float) -> float:
    return round(part / whole, 4) if whole else 0.0


def _note(data: "JiraData", empty: str) -> str | None:
    """Why a metric is unavailable: the store's own reason, else the local one."""
    return data.note or empty


def _attrs(event: dict) -> dict:
    return event.get("attributes") or {}


# --------------------------------------------------------------------------- #
# the store model
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Story:
    """One STORY object with its events and the PRs that name its key."""

    object_id: str
    key: str
    actor: str | None
    state: str | None
    created: datetime | None
    closed: datetime | None
    points: int | None
    epic_key: str | None
    summary_class: str | None
    events: list[dict] = field(default_factory=list)
    prs: list = field(default_factory=list)

    @property
    def first_event(self) -> datetime | None:
        return min((event["_ts"] for event in self.events), default=None)

    @property
    def start(self) -> datetime | None:
        """When the story existed: the object timestamp, else its first event."""
        return self.created or self.first_event

    def of(self, activity: str) -> list[dict]:
        return [event for event in self.events if event["activity"] == activity]


@dataclass
class JiraData:
    """Everything the ticket layer knows: stories, sprints, epics, events."""

    stories: list[Story] = field(default_factory=list)
    sprints: dict[str, dict] = field(default_factory=dict)
    epics: int = 0
    events: int = 0
    note: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.stories)

    def summary(self) -> dict:
        return {
            "available": self.available,
            "stories": len(self.stories),
            "epics": self.epics,
            "sprints": len(self.sprints),
            "events": self.events,
            "note": self.note or (
                "STORY/SPRINT/EPIC objects and the story-key join to PRs"
            ),
        }


EMPTY = JiraData(note=NO_JIRA_NOTE)

OBJECT_SQL = (
    "SELECT object_type, object_id, ticket_keys, actor, state, created_at, closed_at"
    " FROM objects WHERE object_type IN ('STORY','EPIC','SPRINT')"
    " ORDER BY object_type, object_id"
)
OBJECT_SQL_ATTRS = (
    "SELECT object_type, object_id, ticket_keys, actor, state, created_at, closed_at,"
    " CAST(attributes AS VARCHAR) AS attrs FROM objects"
    " WHERE object_type IN ('STORY','EPIC','SPRINT') ORDER BY object_type, object_id"
)
STORY_EVENT_SQL = (
    "SELECT e.event_key, e.ts, e.activity, e.actor, CAST(e.attributes AS VARCHAR) AS attrs,"
    " eo.object_id FROM events e"
    " JOIN event_objects eo ON eo.event_key = e.event_key"
    " WHERE eo.object_type = 'STORY' ORDER BY e.ts ASC, e.event_key ASC"
)
SPRINT_LINK_SQL = (
    "SELECT event_key, object_id FROM event_objects"
    " WHERE object_type = 'SPRINT' ORDER BY event_key, object_id"
)


def _has_column(con, table: str, column: str) -> bool:
    """Older stores predate a column; reads must degrade, not 503."""
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'main' AND table_name = ?",
        [table],
    ).fetchall()
    return any(row[0] == column for row in rows)


def _sprint_id(object_id: str) -> int | None:
    """``SPRINT:PROJ#3`` -> ``3`` (the id the events carry as an attribute)."""
    text = str(object_id).partition("#")[2]
    return int(text) if text.isdigit() else None


def load(con, court: ballcourt.BallCourt | None = None) -> JiraData:
    """Read the Jira half of the store and join it to the PR half on the key.

    ``court`` supplies the PR timelines; without it the PR-derived segments are
    simply unavailable (a ticket-only store still reports the ticket segments).
    """
    has_attrs = _has_column(con, "objects", "attributes")
    rows = con.execute(OBJECT_SQL_ATTRS if has_attrs else OBJECT_SQL).fetchall()
    sprints: dict[str, dict] = {}
    stories: list[Story] = []
    epics = 0
    for row in rows:
        otype, oid, keys, actor, state, created, closed = row[:7]
        attributes = json.loads(row[7]) if has_attrs and len(row) > 7 and row[7] else {}
        if otype == "SPRINT":
            sprints[oid] = {
                "object_id": oid,
                "id": _sprint_id(oid),
                "state": state,
                "start": to_naive_utc(created) if created else None,
                "end": to_naive_utc(closed) if closed else None,
                "planned_end": attributes.get("end_date"),
                "goal_class": attributes.get("goal_class"),
            }
        elif otype == "EPIC":
            epics += 1
        elif otype == "STORY":
            key = (list(keys or [None]))[0] or str(oid).partition(":")[2]
            stories.append(Story(
                object_id=oid,
                key=str(key),
                actor=actor,
                state=state,
                created=to_naive_utc(created) if created else None,
                closed=to_naive_utc(closed) if closed else None,
                points=attributes.get("points"),
                epic_key=attributes.get("epic_key"),
                summary_class=attributes.get("summary_class"),
            ))
    if not stories:
        return JiraData(sprints=sprints, epics=epics, note=NO_JIRA_NOTE)

    by_id = {story.object_id: story for story in stories}
    sprint_links: dict[str, list[str]] = defaultdict(list)
    for event_key, object_id in con.execute(SPRINT_LINK_SQL).fetchall():
        sprint_links[event_key].append(object_id)
    for key, ts, activity, actor, attrs, object_id in con.execute(STORY_EVENT_SQL).fetchall():
        story = by_id.get(object_id)
        if story is None:
            continue
        story.events.append({
            "key": key,
            "ts": iso(ts),
            "_ts": ts,
            "activity": activity,
            "actor": actor,
            "attributes": json.loads(attrs) if attrs else {},
            # The sprint a membership event is about, as the object link: the
            # attribute carries the id too, but the link is the store's own
            # answer when a row was written by an older reducer.
            "sprints": tuple(sprint_links.get(key, ())),
        })
    for story in stories:
        story.events.sort(key=lambda event: (event["_ts"], event["key"]))

    prs_by_key: dict[str, list] = defaultdict(list)
    if court is not None:
        for state in court.prs:
            for key in state.pr.get("ticket_keys") or ():
                prs_by_key[str(key)].append(state)
    for story in stories:
        story.prs = prs_by_key.get(story.key, [])

    return JiraData(
        stories=stories,
        sprints=sprints,
        epics=epics,
        events=sum(len(story.events) for story in stories),
    )


def cohort(data: JiraData, window: tuple[datetime, datetime]) -> list[Story]:
    """Stories created in the window — the denominator every Jira metric reports.

    Same rule as the PR cohort (``rules.cohort``: created in the window), so the
    two halves of the payload always describe the same period.
    """
    start, end = window
    return [
        story for story in data.stories
        if story.start is not None and start <= story.start <= end
    ]


# --------------------------------------------------------------------------- #
# boundaries and evidence spans
# --------------------------------------------------------------------------- #
def _first_status(story: Story, to_status: str, after: datetime | None = None) -> datetime | None:
    """Earliest ``status_changed`` whose *to* category is ``to_status``."""
    found = None
    for event in story.events:
        if event["activity"] != "status_changed":
            continue
        if _attrs(event).get("to_status") != to_status:
            continue
        if after is not None and event["_ts"] < after:
            continue
        if found is None or event["_ts"] < found:
            found = event["_ts"]
    return found


def _first_event_at(story: Story, activity: str) -> datetime | None:
    return min((event["_ts"] for event in story.of(activity)), default=None)


def _first_review(pr_state) -> datetime | None:
    """First reviewer action on a PR (comment or approval, never the author)."""
    opened = pr_state.pr["created_ts"]
    for event in pr_state.events:
        if (
            event["_ts"] >= opened
            and event["activity"] in REVIEW_ACTIONS
            and event["actor"] != pr_state.pr["actor"]
        ):
            return event["_ts"]
    return None


def _pr_merge(pr_state) -> datetime | None:
    pr = pr_state.pr
    return pr["closed_ts"] if pr["state"] == "merged" else None


@dataclass(slots=True)
class Cycle:
    """One story's boundaries, plus the evidence spans the efficiency needs."""

    story: Story
    person: str | None
    boundaries: dict[str, datetime | None]
    spans: list[tuple[datetime, datetime, str | None]]   # measured work
    touches: list[datetime]                              # point-in-time evidence
    review_rounds: int
    missing: list[str]


def _cycle(story: Story) -> Cycle:
    """Derive the eight-segment boundaries for one story.

    The refinement signal is the first ``pointed`` event: Jira's changelog
    records the estimate when it is *set*, which is what refinement produces.
    A story refined but never pointed has no refinement boundary — reported as
    missing rather than guessed from the status history.
    """
    created = story.start
    refined = _first_event_at(story, "pointed")
    sprint = _first_event_at(story, "sprint_added")
    started = _first_status(story, "in_progress")
    done = _first_status(story, "done", after=started)
    if done is None:
        done = story.closed

    prs = list(story.prs)
    pr_open = min((state.pr["created_ts"] for state in prs), default=None)
    reviews = [_first_review(state) for state in prs]
    first_review = min((ts for ts in reviews if ts is not None), default=None)
    merged = max((ts for ts in (_pr_merge(state) for state in prs) if ts is not None), default=None)
    approvals = [
        event["_ts"] for state in prs for event in state.events
        if event["activity"] == "pr_approved"
        and (merged is None or event["_ts"] <= merged)
    ]
    approval = max(approvals, default=None)

    boundaries = {
        "created": created,
        "refined": refined,
        "sprint": sprint,
        "started": started,
        "pr_open": pr_open,
        "first_review": first_review,
        "approval": approval,
        "merged": merged,
        "done": done,
    }
    missing = [name for name, value in boundaries.items() if value is None]

    # -- measured work: PR intervals where the author was working -----------
    spans: list[tuple[datetime, datetime, str | None]] = []
    touches: list[datetime] = []
    rounds = 0
    for state in prs:
        author = state.pr["actor"]
        for interval in state.intervals:
            if interval.phase in ACTIVE_PHASES:
                spans.append((interval.start, interval.end, author))
        for event in state.events:
            if event["activity"] in TOUCH_ACTIVITIES:
                touches.append(event["_ts"])
            if (
                event["activity"] in ("pr_pushed", "pr_unapproved")
                and first_review is not None and event["_ts"] >= first_review
            ):
                rounds += 1
    # -- measured work: worklog durations (ticket side) ---------------------
    for event in story.of("worklogged"):
        attributes = _attrs(event)
        seconds = attributes.get("time_spent_seconds") or 0
        delay = attributes.get("log_delay_seconds") or 0
        start = event["_ts"] - timedelta(seconds=int(delay))
        spans.append((start, start + timedelta(seconds=int(seconds)), event["actor"]))
    # -- point-in-time evidence: ticket-layer changes -----------------------
    touches.extend(
        event["_ts"] for event in story.events
        if event["activity"] in TICKET_TOUCHES
    )

    person = story.actor or (prs[0].pr["actor"] if prs else None)
    return Cycle(
        story=story,
        person=person,
        boundaries=boundaries,
        spans=spans,
        touches=sorted(touches),
        review_rounds=rounds,
        missing=missing,
    )


def _active_hours(
    court: ballcourt.BallCourt,
    cycle: Cycle,
    t1: datetime,
    t2: datetime,
) -> float:
    """Evidence of work inside ``[t1, t2)``, in hours.

    Sums the business hours of every measured work span that overlaps the
    segment (a PR interval with commits/pushes, a worklog's own duration), then
    credits :data:`TOUCH_HOURS` per touch that lands strictly inside the segment
    and outside those spans — a boundary event ends the segment, so it belongs
    to neither side. A span is priced with its own person's calendar; where two
    people's spans overlap the caller clamps the total to the segment, and the
    clamp count travels in the payload.
    """
    total = 0.0
    covered: list[tuple[datetime, datetime]] = []
    for start, end, person in cycle.spans:
        low, high = max(start, t1), min(end, t2)
        if high > low:
            total += court.business_hours(person, low, high)
            covered.append((low, high))
    for stamp in cycle.touches:
        if t1 < stamp < t2 and not any(low <= stamp <= high for low, high in covered):
            total += TOUCH_HOURS
    return total


# --------------------------------------------------------------------------- #
# 1. waiting-time decomposition (prd/21 §1)
# --------------------------------------------------------------------------- #
def waiting_time_decomposition(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
) -> dict:
    """The eight segments: hours, share of cycle, and flow efficiency each.

    Per segment the payload reports the hours the cohort spent in it, its share
    of the measured total (that is the "the team spends X% of cycle time in the
    sprint→in-progress segment" number), the aggregate flow efficiency
    (Σ active ÷ Σ elapsed) and the median per-story efficiency. A segment with
    no measurable story is ``n: 0`` rather than zero hours, so "no data" never
    reads as "no wait".
    """
    stories = cohort(data, window)
    segments: dict[str, dict] = {
        key: {
            "hours": 0.0, "active_hours": 0.0, "n": 0, "missing": 0,
            "inverted": 0, "efficiencies": [], "label": SEGMENT_LABEL[key],
        }
        for key in SEGMENT_KEYS
    }
    missing_boundaries: dict[str, int] = defaultdict(int)
    rows: list[dict] = []
    cycle_total = 0.0
    clamped = 0
    decomposed = 0
    rounds_total = 0

    for story in stories:
        cycle = _cycle(story)
        for name in cycle.missing:
            missing_boundaries[name] += 1
        end = cycle.boundaries["done"] or court.snapshot
        begin = cycle.boundaries["created"]
        cycle_hours = (
            round(court.business_hours(cycle.person, begin, end), 2)
            if begin is not None and end is not None and end > begin else 0.0
        )
        cycle_total += cycle_hours
        row: dict = {"key": story.key, "cycle_hours": cycle_hours, "segments": {}}
        measured = 0
        for key in SEGMENT_KEYS:
            left, right = SEGMENT_BOUNDS[key]
            t1 = cycle.boundaries.get(left)
            t2 = cycle.boundaries.get(right)
            entry = segments[key]
            if t1 is None or t2 is None:
                entry["missing"] += 1
                row["segments"][key] = None
                continue
            if t2 < t1:
                # out-of-order data (a PR opened before the ticket moved): the
                # honest answer is "no wait here", and the inversion is counted
                entry["inverted"] += 1
                row["segments"][key] = 0.0
                measured += 1
                continue
            elapsed = round(court.business_hours(cycle.person, t1, t2), 2)
            active = round(_active_hours(court, cycle, t1, t2), 2)
            if active > elapsed:
                active = elapsed
                clamped += 1
            entry["hours"] += elapsed
            entry["active_hours"] += active
            entry["n"] += 1
            if elapsed > 0:
                entry["efficiencies"].append(round(active / elapsed, 4))
            row["segments"][key] = elapsed
            measured += 1
        if measured:
            decomposed += 1
        rows.append(row)
        rounds_total += cycle.review_rounds

    total_hours = round(sum(entry["hours"] for entry in segments.values()), 2)
    out_segments: dict[str, dict] = {}
    for key in SEGMENT_KEYS:
        entry = segments[key]
        hours = round(entry["hours"], 2)
        active = round(entry["active_hours"], 2)
        out_segments[key] = {
            "label": entry["label"],
            "hours": hours,
            "share": _share(hours, total_hours),
            "active_hours": active,
            "efficiency": round(active / hours, 4) if hours > 0 else None,
            "median_efficiency": _median(entry["efficiencies"], 4),
            "n": entry["n"],
            "missing": entry["missing"],
            "inverted": entry["inverted"],
            "bounds": list(SEGMENT_BOUNDS[key]),
        }
    largest = max(SEGMENT_KEYS, key=lambda key: (out_segments[key]["hours"], key))
    return {
        "available": any(entry["n"] for entry in segments.values()),
        "stories": len(stories),
        "stories_decomposed": decomposed,
        "segments": out_segments,
        "order": list(SEGMENT_KEYS),
        "total_hours": total_hours,
        "cycle_hours": round(cycle_total, 2),
        "explained_share": _share(total_hours, cycle_total),
        "largest": largest,
        "largest_label": SEGMENT_LABEL[largest],
        "largest_hours": out_segments[largest]["hours"],
        "largest_share": out_segments[largest]["share"],
        "largest_efficiency": out_segments[largest]["efficiency"],
        "review_rounds": rounds_total,
        "missing_boundaries": dict(sorted(missing_boundaries.items())),
        "clamped": clamped,
        "hours_mode": court.hours_mode,
        "rows": rows,
        "heuristic": (
            "refined = the first 'pointed' event (the estimate refinement "
            "produces); sprint = the first 'sprint_added'; in progress / done = "
            "the status *category* transitions; PR open / first review / "
            "approval / merge come from the story's PRs via PR.ticket_keys. A "
            "segment needs both boundaries, otherwise it is counted as missing"
        ),
        "efficiency_note": (
            "active = measured work spans inside the segment (PR intervals with "
            "commits/pushes, worklog durations) plus "
            f"{TOUCH_HOURS} h per touch (status change, re-point, sprint move, "
            "comment, approval), clamped to the segment; elapsed is business "
            "hours of the story assignee's calendar"
        ),
        "note": _note(
            data,
            "no story in this window had two measurable boundaries, so no "
            "segment could be priced",
        ),
    }


# --------------------------------------------------------------------------- #
# 2. carry-over (prd/21 §2)
# --------------------------------------------------------------------------- #
def carry_over(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
) -> dict:
    """Stories added to more than one sprint — "the sprint plan isn't honest".

    A story counts when its ``sprint_added`` events name **distinct** sprint
    ids: that is a story that was committed to one sprint and finished in
    another. A move out to the backlog and back into the *same* sprint is not
    carry-over (reported as ``readded_same_sprint`` context instead).

    ``wait_hours`` per row is the time the story sat in a sprint before work
    started (first ``sprint_added`` → first in-progress, or the snapshot when it
    never started): the commitment that did not turn into progress.
    """
    stories = cohort(data, window)
    rows: list[dict] = []
    readded = 0
    for story in stories:
        added = story.of("sprint_added")
        if not added:
            continue
        ids: list[int] = []
        for event in added:
            sid = _attrs(event).get("sprint_id")
            if sid is not None:
                ids.append(int(sid))
                continue
            for linked in event.get("sprints") or ():
                found = _sprint_id(linked)
                if found is not None:
                    ids.append(found)
        distinct = sorted(set(ids))
        if len(ids) > len(distinct):
            readded += 1
        if len(distinct) <= 1:
            continue
        started = _first_status(story, "in_progress")
        end = started or story.closed or court.snapshot
        first_added = min(event["_ts"] for event in added)
        wait = (
            round(court.business_hours(story.actor, first_added, end), 2)
            if end > first_added else 0.0
        )
        rows.append({
            "key": story.key,
            "sprints": distinct,
            "added_events": len(added),
            "first_added": iso(first_added),
            "started": iso(started) if started else None,
            "wait_hours": wait,
            "points": story.points,
        })
    rows.sort(key=lambda row: (-row["wait_hours"], row["key"]))
    carried = len(rows)
    with_sprints = sum(1 for story in stories if story.of("sprint_added"))
    return {
        "available": with_sprints > 0,
        "stories": len(stories),
        "stories_in_a_sprint": with_sprints,
        "carried_over": carried,
        "share": _share(carried, with_sprints),
        "share_of_stories": _share(carried, len(stories)),
        "wait_hours": round(sum(row["wait_hours"] for row in rows), 2),
        "median_wait_hours": _median([row["wait_hours"] for row in rows]),
        "sprints_involved": len({sid for row in rows for sid in row["sprints"]}),
        "readded_same_sprint": readded,
        "rows": rows,
        "target_share": CARRY_OVER_SHARE_TARGET,
        "heuristic": (
            "a story is carried over when its sprint_added events name more than "
            "one distinct sprint id; wait_hours is first sprint_added → first "
            "in-progress (or the snapshot)"
        ),
        "note": _note(
            data,
            "no 'sprint_added' event in this window, so there is no sprint "
            "membership to check for carry-over",
        ),
    }


# --------------------------------------------------------------------------- #
# 3. point churn (prd/21 §3)
# --------------------------------------------------------------------------- #
def point_churn(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
) -> dict:
    """``pointed`` events *after* the story started — the estimate moved mid-flight.

    Points are set in refinement and should be stable by the time work starts.
    A re-point after the first in-progress transition means the sprint plan was
    built on a number that was not final (prd/21 §3, and the Jira side of
    conformance §5).
    """
    stories = cohort(data, window)
    rows: list[dict] = []
    pointed_stories = 0
    churned_events = 0
    for story in stories:
        pointed = story.of("pointed")
        if pointed:
            pointed_stories += 1
        started = _first_status(story, "in_progress")
        if started is None:
            continue
        after = [event for event in pointed if event["_ts"] > started]
        if not after:
            continue
        churned_events += len(after)
        last = after[-1]
        rows.append({
            "key": story.key,
            "events": len(after),
            "started": iso(started),
            "last_pointed": iso(last["_ts"]),
            "points": _attrs(last).get("points", story.points),
            "object_points": story.points,
        })
    churned = len(rows)
    return {
        "available": pointed_stories > 0,
        "stories": len(stories),
        "stories_pointed": pointed_stories,
        "churned": churned,
        "churned_events": churned_events,
        "share": _share(churned, pointed_stories),
        "rows": rows,
        "target_share": POINT_CHURN_SHARE_TARGET,
        "heuristic": (
            "a 'pointed' event with a timestamp after the first status_changed "
            "to in_progress is a re-point after work started"
        ),
        "note": _note(
            data,
            "no 'pointed' event in this window, so the store cannot tell "
            "whether an estimate moved",
        ),
    }


# --------------------------------------------------------------------------- #
# 4. worklog cross-check (prd/21 §4)
# --------------------------------------------------------------------------- #
def worklog_cross_check(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
) -> dict:
    """Worklog hours vs the activity signal, and the logging-delay distribution.

    Two independent questions, because they fail differently:

    * **when** the work was logged — the ``log_delay_bucket`` distribution
      (same_day / next_day / within_week / late) and the mean/median/p90 gap.
      Late logging is a *measurement* problem: the hours may be right, but the
      timing is reconstructed from memory, so nothing can be said about when
      the work happened.
    * **how much** was logged vs what the PR side can see — the author's active
      PR time (commits, pushes, rebases). A large ratio means the worklog and
      the code disagree, in one direction or the other.

    ``active_hours`` is measured, not inferred: PR intervals in an active or
    rebasing phase. A story with no PR contributes nothing to it, which is why
    the ratio is reported with the population it was computed over.
    """
    stories = cohort(data, window)
    buckets = {name: 0 for name in LOG_DELAY_BUCKETS}
    delays_days: list[float] = []
    logged_hours = 0.0
    worklogs = 0
    stories_with_worklogs = 0
    active_hours = 0.0
    stories_with_prs = 0
    for story in stories:
        entries = story.of("worklogged")
        if entries:
            stories_with_worklogs += 1
        for event in entries:
            attributes = _attrs(event)
            seconds = attributes.get("time_spent_seconds") or 0
            logged_hours += float(seconds) / 3600.0
            worklogs += 1
            bucket = attributes.get("log_delay_bucket")
            if bucket in buckets:
                buckets[bucket] += 1
            delay = attributes.get("log_delay_seconds")
            if delay is not None:
                delays_days.append(float(delay) / 86400.0)
        if story.prs:
            stories_with_prs += 1
            for state in story.prs:
                for interval in state.intervals:
                    if interval.phase in ACTIVE_PHASES:
                        active_hours += interval.hours

    cycle_hours = 0.0
    for story in stories:
        cycle = _cycle(story)
        begin = cycle.boundaries["created"]
        end = cycle.boundaries["done"] or court.snapshot
        if begin is not None and end is not None and end > begin:
            cycle_hours += court.business_hours(cycle.person, begin, end)

    late = buckets["within_week"] + buckets["late"]
    untrusted_share = _share(late, worklogs)
    ratio = round(logged_hours / active_hours, 2) if active_hours > 0 else None
    return {
        "available": worklogs > 0,
        "stories": len(stories),
        "stories_with_worklogs": stories_with_worklogs,
        "stories_with_prs": stories_with_prs,
        "worklogs": worklogs,
        "logged_hours": round(logged_hours, 2),
        "active_hours": round(active_hours, 2),
        "cycle_hours": round(cycle_hours, 2),
        "logged_to_active_ratio": ratio,
        "logged_to_cycle_ratio": (
            round(logged_hours / cycle_hours, 2) if cycle_hours > 0 else None
        ),
        "buckets": {name: buckets[name] for name in LOG_DELAY_BUCKETS},
        "bucket_shares": {
            name: _share(buckets[name], worklogs) for name in LOG_DELAY_BUCKETS
        },
        "late_share": untrusted_share,
        "untrusted_hours": round(logged_hours * untrusted_share, 2),
        "mean_delay_days": (
            round(sum(delays_days) / len(delays_days), 2) if delays_days else None
        ),
        "median_delay_days": _median(delays_days),
        "p90_delay_days": _p90(delays_days),
        "delay_target_days": WORKLOG_DELAY_TARGET_DAYS,
        "late_share_target": WORKLOG_LATE_SHARE_TARGET,
        "ratio_low": WORKLOG_RATIO_LOW,
        "ratio_high": WORKLOG_RATIO_HIGH,
        "heuristic": (
            "delay = worklog.created − worklog.started, bucketed by the ingest "
            "(same_day / next_day / within_week / late); active_hours is PR "
            "intervals in an active or rebasing phase, so a story with no PR "
            "adds nothing to the denominator"
        ),
        "note": _note(data, "no 'worklogged' event in this window"),
    }


# --------------------------------------------------------------------------- #
# 5. conformance, Jira side (prd/21 §5)
# --------------------------------------------------------------------------- #
def conformance(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
    carry_metric: dict | None = None,
    churn_metric: dict | None = None,
) -> dict:
    """Started before refinement, re-pointed after start, carry-over.

    The ticket-layer conformance question is "did the process happen in the
    order it says it does". Three deterministic violations the store can prove:

    * **started before refined** — a status → in-progress before the first
      ``pointed`` event (work began on an unestimated story);
    * **re-pointed after start** — the estimate moved under the work (§3);
    * **carried over** — the story was committed to a sprint it did not finish
      in (§2).

    A story with an in-progress transition and *no* ``pointed`` event at all is
    not counted as "started before refined": the store cannot tell refinement
    from a missing estimate, so it is reported separately as
    ``started_unrefined`` rather than silently folded into the violation.
    """
    stories = cohort(data, window)
    started_before: list[str] = []
    started_unrefined: list[str] = []
    pr_before_start: list[str] = []
    for story in stories:
        cycle = _cycle(story)
        started = cycle.boundaries["started"]
        if started is None:
            continue
        refined = cycle.boundaries["refined"]
        if refined is None:
            started_unrefined.append(story.key)
        elif started < refined:
            started_before.append(story.key)
        pr_open = cycle.boundaries["pr_open"]
        if pr_open is not None and pr_open < started:
            pr_before_start.append(story.key)
    carry = carry_metric if carry_metric is not None else carry_over(court, data, window)
    churn = churn_metric if churn_metric is not None else point_churn(court, data, window)
    started = sum(1 for story in stories if _first_status(story, "in_progress"))
    violations = len(started_before) + churn["churned"] + carry["carried_over"]
    return {
        "available": bool(stories),
        "stories": len(stories),
        "stories_started": started,
        "started_before_refined": len(started_before),
        "started_before_refined_share": _share(len(started_before), started),
        "started_unrefined": len(started_unrefined),
        "points_changed_after_start": churn["churned"],
        "carried_over": carry["carried_over"],
        "pr_opened_before_start": len(pr_before_start),
        "violations": violations,
        "examples": sorted(started_before)[:5],
        "unrefined_examples": sorted(started_unrefined)[:5],
        "heuristic": (
            "refined = first 'pointed'; started = first status_changed to "
            "in_progress; a story with no pointed event is reported as "
            "started_unrefined, not as a violation, because the store cannot "
            "tell refinement from a missing estimate"
        ),
        "note": _note(data, "no story in this window"),
    }


# --------------------------------------------------------------------------- #
# refinement cost (the impact basis for churn / conformance)
# --------------------------------------------------------------------------- #
def refinement_cost(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
) -> dict:
    """What one refinement costs, in hours: the median ready→refined segment.

    A re-point or an unrefined start means the estimate was not settled when it
    should have been, so the price of the rule is one more refinement. Measured
    from the cohort's own ready→refined segments; falls back to a stated
    default when the store has none.
    """
    spans: list[float] = []
    for story in cohort(data, window):
        cycle = _cycle(story)
        begin = cycle.boundaries["created"]
        refined = cycle.boundaries["refined"]
        if begin is None or refined is None or refined < begin:
            continue
        spans.append(round(court.business_hours(cycle.person, begin, refined), 2))
    measured = _median(spans)
    return {
        "hours": measured if measured is not None else DEFAULT_REFINEMENT_HOURS,
        "n": len(spans),
        "basis": (
            "median ready→refined segment of this cohort"
            if measured is not None
            else "default: the cohort has no measured refinement segment"
        ),
    }


# --------------------------------------------------------------------------- #
# the bundle
# --------------------------------------------------------------------------- #
def metrics(
    court: ballcourt.BallCourt,
    data: JiraData | None,
    window: tuple[datetime, datetime],
) -> dict:
    """Every prd/21 metric, in one deterministic pass."""
    data = data or EMPTY
    carry = carry_over(court, data, window)
    churn = point_churn(court, data, window)
    return {
        "jira": data.summary(),
        "waiting_time_decomposition": waiting_time_decomposition(court, data, window),
        "carry_over": carry,
        "point_churn": churn,
        "worklog_cross_check": worklog_cross_check(court, data, window),
        "jira_conformance": conformance(
            court, data, window, carry_metric=carry, churn_metric=churn
        ),
        "refinement_cost": refinement_cost(court, data, window),
    }

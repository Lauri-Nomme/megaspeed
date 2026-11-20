"""Jira-layer analysis (prd/21): where the *ticket* waits.

The PR half of the store answers "how long did the code wait". The Jira half
answers the question before it: a story sits in a backlog, gets refined, waits
for a sprint, starts, opens a PR, goes through review, merges, and only then is
it closed. prd/21 calls that the **waiting-time decomposition**:

    created ─► ready ─► sprint ─► in progress ─► PR open ─► first review
            ─► review rounds ─► final approval ─► merge ─► done
    └ created→ready ┘└ ready→sprint ┘└ sprint→in-progress ┘└ in-progress→PR-open ┘
                     └ PR-open→first-review ┘└ review rounds ┘
                              └ final-approval→merge ┘└ merge→done ┘

prd/29 adds the workflow's own stages: when the store's ``[jira.stages]`` map
names a QA status, ``merge→done`` splits into ``merge→verification`` and
``verification→done`` so "time in QA" is measured rather than folded into
"done". The ``ready``, ``in_review`` and ``in_verification`` boundaries come
from the status stages; without a map they do not exist and are reported
missing (an unconfigured store keeps the eight segments and today's numbers).

The eight segments are built from three sources, joined on the story key:

* the story's own events — ``created``, ``status_changed`` (both ends are status
  *stages*, never a workflow name), ``pointed``, ``assigned``,
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
``sprints``             the sprint lens (prd/26 P6): bands, scope, carry-over
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
from ingest.taxonomy import normalize_stage

# --------------------------------------------------------------------------- #
# the eight segments (prd/21)
# --------------------------------------------------------------------------- #
SEGMENTS: tuple[tuple[str, str], ...] = (
    ("created_ready", "created → ready"),
    ("ready_sprint", "ready → sprint"),
    ("sprint_in_progress", "sprint → in progress"),
    ("in_progress_pr_open", "in progress → PR open"),
    ("pr_open_first_review", "PR open → first review"),
    ("review_rounds", "review rounds"),
    ("final_approval_merge", "final approval → merge"),
    ("merge_done", "merge → done"),
)
#: prd/29: when the store's workflow has an ``in_verification`` stage, the last
#: segment splits in two — the wait from the merge to QA picking the ticket up,
#: and the QA time itself. The other seven stay where they are.
VERIFICATION_SEGMENTS: tuple[tuple[str, str], ...] = (
    ("merge_verification", "merge → verification"),
    ("verification_done", "verification → done"),
)
SEGMENT_KEYS = tuple(key for key, _ in SEGMENTS)
SEGMENT_LABEL = dict(SEGMENTS) | dict(VERIFICATION_SEGMENTS)
#: Which two boundaries delimit each segment. A segment is measured only when
#: both exist for that story. The first segment is ``created → ready``
#: (concepts.md §5): refinement is the work inside it, not its name; the
#: ``ready`` boundary falls back to the first ``pointed`` event in ``_cycle``.
SEGMENT_BOUNDS: dict[str, tuple[str, str]] = {
    "created_ready": ("created", "ready"),
    "ready_sprint": ("ready", "sprint"),
    "sprint_in_progress": ("sprint", "started"),
    "in_progress_pr_open": ("started", "pr_open"),
    "pr_open_first_review": ("pr_open", "first_review"),
    "review_rounds": ("first_review", "approval"),
    "final_approval_merge": ("approval", "merged"),
    "merge_done": ("merged", "done"),
    "merge_verification": ("merged", "in_verification"),
    "verification_done": ("in_verification", "done"),
}


def segment_keys(boundaries: dict, *, verification: bool = False) -> tuple[str, ...]:
    """The segment order for one story (or one store).

    The eight base segments, except that the last one is replaced by
    ``merge_verification`` + ``verification_done`` when the workflow has an
    ``in_verification`` stage (prd/29). ``verification`` says the *store* has the
    stage (so a story that never reached QA shows the segment as missing, with
    ``in_verification`` named) while the per-story ``boundaries`` says whether
    this story reached it.
    """
    if verification or boundaries.get("in_verification") is not None:
        return SEGMENT_KEYS[:-1] + tuple(key for key, _ in VERIFICATION_SEGMENTS)
    return SEGMENT_KEYS


NO_JIRA_NOTE = (
    "no Jira data in this store (no STORY objects): the ticket layer needs the "
    "Jira ingest (prd/04). The PR-side metrics are unaffected."
)

# v1 targets, stated in every payload that uses them (never a hidden constant).
CARRY_OVER_SHARE_TARGET = 0.10     # ≤ 10% of stories may move between sprints
#: prd/42 M5: a story added within the sprint's first day is planning jitter,
#: not scope creep. The QA run measured "more than a day after the start" (25
#: of 30 in S2); the grace period reproduces that fact exactly.
SPRINT_SCOPE_CREEP_GRACE = timedelta(days=1)
POINT_CHURN_SHARE_TARGET = 0.10    # ≤ 10% may be re-pointed after work started
WORKLOG_DELAY_TARGET_DAYS = 1.0    # logging same/next day is the honest case
WORKLOG_LATE_SHARE_TARGET = 0.25   # > 25% logged a week late is not a record
WORKLOG_RATIO_LOW = 0.5            # logged ÷ active below this: under-logged
WORKLOG_RATIO_HIGH = 2.0           # above this: logged time is not the work
SEGMENT_SHARE_TARGET = 0.25        # one segment over a quarter of the cycle
SEGMENT_SHARE_CRITICAL = 0.40
#: prd/41 B4: two segments whose shares differ by less than this many
#: *percentage points* are a statistical tie, not a ranking. The rule says so
#: instead of presenting "the largest segment" as the one thing to fix.
SEGMENT_TIE_POINTS = 1.0
#: prd/37: a sprint "delivered on time" when this share of its committed stories
#: reached done by its close. Stated in the rule and the sprint payload.
SPRINT_ON_TIME_TARGET = 0.80
#: A one- or two-story sprint is not evidence about planning; say so, do not rank.
SPRINT_MIN_STORIES = 3

REVIEW_ACTIONS = ("comment_added", "pr_approved")
LOG_DELAY_BUCKETS = ("same_day", "next_day", "within_week", "late")
#: concepts.md §1: a day is 8 business hours, never 24 and never a calendar day.
BUSINESS_HOURS_PER_DAY = 8.0
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


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation, or ``None`` when there is no variance to correlate."""
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    x = [float(value) for value in xs[:n]]
    y = [float(value) for value in ys[:n]]
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y))
    var_x = sum((a - mean_x) ** 2 for a in x)
    var_y = sum((b - mean_y) ** 2 for b in y)
    if var_x <= 0 or var_y <= 0:
        return None
    return round(cov / math.sqrt(var_x * var_y), 3)


def _note(data: "JiraData", empty: str, available: bool = False) -> str | None:
    """Why a metric is unavailable: the store's own reason, else the local one.

    A metric that *did* measure something carries no note: the "no … event in
    this window" line is the explanation for an unavailable result, and printing
    it next to a populated number reads as a contradiction (prd/25). The store's
    own note still wins, because "no Jira data" is true even when a local
    population is non-empty.
    """
    if data.note:
        return data.note
    return None if available else empty


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
    #: The store's as-of time, set by ``load`` from the ball-court. It bounds an
    #: open PR's span ("no PR open" gaps cannot be computed without an end).
    snapshot: datetime | None = None
    # Filled by ``_cycle`` on first use. The derivation is a pure function of
    # the story's own events/PRs, and one payload asks the same story for its
    # cycle four times, so the slot is the memo. Excluded from ``repr``/``eq``
    # so the cached value neither changes what a Story compares equal to nor
    # recurses through ``Cycle.story``. A store reload builds new Story objects,
    # which is exactly the invalidation the other caches use.
    _cycle_cache: Cycle | None = field(default=None, repr=False, compare=False)

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
    #: prd/42 P9: the epic objects themselves (key, state, timestamps), so the
    #: ticket page can say "this is an epic, not a story" instead of rendering
    #: a story page with 0/9 segments.
    epic_objects: dict[str, dict] = field(default_factory=dict)
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


def event_sprint_id(event: dict) -> int | None:
    """The sprint an event is about: its ``sprint_id`` attribute, else its link.

    The attribute is what the reducer writes; the ``SPRINT:`` object link is the
    store's own answer when a row was written by an older reducer. One helper,
    so the sprint lens and the ticket view cannot read membership differently.
    """
    sid = _attrs(event).get("sprint_id")
    if sid is not None:
        try:
            return int(sid)
        except (TypeError, ValueError):
            return None
    for linked in event.get("sprints") or ():
        found = _sprint_id(linked)
        if found is not None:
            return found
    return None


def load(con, court: ballcourt.BallCourt | None = None) -> JiraData:
    """Read the Jira half of the store and join it to the PR half on the key.

    ``court`` supplies the PR timelines; without it the PR-derived segments are
    simply unavailable (a ticket-only store still reports the ticket segments).
    """
    has_attrs = _has_column(con, "objects", "attributes")
    rows = con.execute(OBJECT_SQL_ATTRS if has_attrs else OBJECT_SQL).fetchall()
    sprints: dict[str, dict] = {}
    stories: list[Story] = []
    epic_objects: dict[str, dict] = {}
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
            key = str((list(keys or [None]))[0] or str(oid).partition(":")[2])
            epic_objects[key] = {
                "key": key,
                "object_id": oid,
                "state": state,
                "created": to_naive_utc(created) if created else None,
                "closed": to_naive_utc(closed) if closed else None,
                "summary_class": attributes.get("summary_class"),
            }
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
        return JiraData(
            sprints=sprints, epics=epics, epic_objects=epic_objects,
            note=NO_JIRA_NOTE,
        )

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
        if court is not None:
            story.snapshot = court.snapshot

    return JiraData(
        stories=stories,
        sprints=sprints,
        epics=epics,
        epic_objects=epic_objects,
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


def story_for(data: JiraData | None, key: str) -> Story | None:
    """The STORY object whose key is ``key`` (prd/25's ticket detail lookup).

    A store may hold a story key as ``STORY:DEL-167`` or the bare key; the
    comparison is on the key the object itself carries, never a prefix match, so
    ``DEL-16`` cannot resolve ``DEL-167``.
    """
    if data is None:
        return None
    wanted = str(key or "").strip()
    for story in data.stories:
        if story.key == wanted:
            return story
    return None


def done_keys(data: JiraData | None) -> set[str]:
    """Story keys that reached ``done`` — the "ticket Done but PR open" signal.

    prd/34 §5 / prd/37: a PR still open under a story that Jira already closed
    is a stale-PR fact the delivery list must be able to flag. ``done`` is the
    cycle's own final done boundary, so a reopened story that is open again is
    *not* reported as Done.
    """
    if data is None:
        return set()
    return {
        story.key for story in data.stories
        if _cycle(story).boundaries["done"] is not None
    }


def uses_verification(data: JiraData | None) -> bool:
    """Does this store's workflow have an ``in_verification`` stage? (prd/29)

    True when any story reached it. A store scraped with a stage map that names
    a QA status does; an unconfigured store never does (its QA status projects
    onto ``in_progress``), which is exactly how the unconfigured path keeps
    today's eight segments.
    """
    if data is None:
        return False
    return any(_first_status(story, "in_verification") for story in data.stories)


# --------------------------------------------------------------------------- #
# boundaries and evidence spans
# --------------------------------------------------------------------------- #
def _first_status(story: Story, to_status: str, after: datetime | None = None) -> datetime | None:
    """Earliest ``status_changed`` whose *to* stage is ``to_status``.

    A pre-prd/29 store wrote ``todo`` where the stage vocabulary writes
    ``backlog``; ``normalize_stage`` makes both read the same (prd/29).
    """
    found = None
    for event in story.events:
        if event["activity"] != "status_changed":
            continue
        if normalize_stage(_attrs(event).get("to_status")) != to_status:
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


# Public wrappers for the ticket detail view (prd/25). The cohort metrics use
# the private helpers; a per-ticket read needs the same answers, not a second
# implementation that could disagree with them.
def first_review(pr_state) -> datetime | None:
    return _first_review(pr_state)


def pr_merge(pr_state) -> datetime | None:
    return _pr_merge(pr_state)


def sprint_id(object_id: str) -> int | None:
    return _sprint_id(object_id)


def attributes(event: dict) -> dict:
    return _attrs(event)


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
    #: Gaps *between* two PRs, labelled with the PRs on each side, so the
    #: answer can say "the second PR started N days after the first merged".
    pr_gaps: list[dict] = field(default_factory=list)
    #: ``done → ready`` (or any leave-done) moves after the first done (prd/36 §4).
    reopens: list[dict] = field(default_factory=list)
    #: prd/46 §1: the boundaries *as measured for the segment arithmetic* — every
    #: boundary clamped into ``[created, done-or-snapshot]`` and made
    #: non-decreasing along the lifecycle. ``boundaries`` stays the raw
    #: derivation (conformance and the PR pages read it); the segments read this
    #: one, so a segment can never be longer than the cycle and the shares can
    #: never sum past 100 %.
    segment_bounds: dict[str, datetime | None] = field(default_factory=dict)
    #: prd/46 §1: every boundary the clamp had to move, with the raw timestamp,
    #: the value used and the reason. A data quirk, named as one.
    out_of_order: list[dict] = field(default_factory=list)


def no_pr_open_spans(story: Story, t1: datetime, t2: datetime) -> list[tuple[datetime, datetime]]:
    """Parts of ``[t1, t2)`` between two PRs — i.e. with no PR open.

    Only the gaps *between* PRs are reported: "no PR open" before the first PR
    (refinement, sprint, waiting to start) and after the last merge (QA, the
    close) is the normal shape of the cycle, not the finding. The finding is a
    segment that spans an interval where a second PR had not started yet.
    """
    gaps: list[tuple[datetime, datetime]] = []
    for gap in between_pr_gaps(story):
        low = max(gap["start"], t1)
        high = min(gap["end"], t2)
        if high > low:
            gaps.append((low, high))
    return gaps


def between_pr_gaps(story: Story) -> list[dict]:
    """The gaps between consecutive PRs, with the PRs on each side."""
    prs = sorted(
        (
            (state.pr["created_ts"], state.pr["closed_ts"] or story.snapshot, state.pr["pr_id"])
            for state in story.prs
            if state.pr["created_ts"] is not None
        ),
        key=lambda item: (item[0], item[2]),
    )
    gaps: list[dict] = []
    for (start, end, number), (next_start, _next_end, next_number) in zip(prs, prs[1:]):
        if end is None or next_start is None or next_start <= end:
            continue
        gaps.append({
            "start": end,
            "end": next_start,
            "from_pr": number,
            "to_pr": next_number,
        })
    return gaps


def _ordered_segment_bounds(
    boundaries: dict[str, datetime | None],
    end: datetime | None,
) -> tuple[dict[str, datetime | None], list[dict]]:
    """Order the lifecycle boundaries so the segments tile the cycle (prd/46 §1).

    The store can hand over boundaries out of order — DEL-185's ``sprint_added``
    is dated at the sprint's start (8 Oct) while the story was created on 23 Oct,
    because the membership event is projected at the sprint boundary. Drawn
    literally, that makes ``sprint → in progress`` a negative/overlapping
    segment and the eight shares sum to 174–200 % of the cycle.

    Two rules, applied in lifecycle order:

    * every boundary is clamped into ``[created, done-or-snapshot]`` — a
      segment can never be longer than the cycle;
    * the sequence is made non-decreasing (``b[i] = max(b[i], b[i-1])``) — no
      segment can overlap its neighbour.

    Nothing is silently "fixed": each moved boundary is returned as a quirk
    (raw value, clamped value, reason), and the answer names it as a data quirk
    rather than a finding. ``boundaries`` keeps the raw derivation so
    conformance can still report that the events are out of order.
    """
    ordered = dict(boundaries)
    quirks: list[dict] = []
    start = boundaries.get("created")
    chain = (
        "created", "ready", "sprint", "started", "pr_open",
        "first_review", "approval", "merged", "in_verification", "done",
    )
    previous_name: str | None = None
    previous: datetime | None = None
    for name in chain:
        raw = boundaries.get(name)
        if raw is None:
            continue
        value = raw
        reason: str | None = None
        precedes: str | None = None
        if start is not None and value < start:
            value = start
            reason = "before_cycle"
            precedes = "created"
        elif end is not None and name != "done" and value > end:
            value = end
            reason = "after_cycle"
            precedes = "done"
        if previous is not None and value < previous:
            value = previous
            reason = reason or "before_predecessor"
            precedes = previous_name
        if reason is not None:
            quirks.append({
                "boundary": name,
                "ts": iso(raw),
                "clamped_to": iso(value),
                "reason": reason,
                "precedes": precedes,
            })
        ordered[name] = value
        previous_name, previous = name, value
    return ordered, quirks


def _cycle(story: Story) -> Cycle:
    """Derive the segment boundaries for one story (memoised on the story).

    The refinement signal is the first status → ``ready`` before work starts,
    falling back to the first ``pointed`` event (Jira's changelog records the
    estimate when it is *set*, which is what refinement produces). A story with
    neither has no refinement boundary — reported as missing rather than guessed.

    prd/29 adds the workflow's own stages as boundaries:

    * ``ready`` — the first status → ``ready`` (the story is refined/triaged),
      falling back to the first ``pointed`` for a workflow with no such status;
    * ``in_review`` / ``in_verification`` — the first status → those stages.
      They only exist when the store was projected with a stage map, so an
      unconfigured store reports them missing (which is the honest answer: its
      workflow was collapsed into three categories).

    prd/36 adds two facts the old derivation could not state: the *first* and
    *final* done (a story can be reopened, §4) and the gaps between its PRs
    (a segment can span time with no PR open, §2).

    Every metric that needs a boundary asks for the cycle, so one payload
    derived the same four cycles per story. The result depends only on the
    story's immutable-at-load events and PRs, so it is kept on the story
    (``Story._cycle_cache``): a store reload creates new stories and therefore
    a new cache, the same invalidation the server's other caches use.
    """
    cached = story._cycle_cache
    if cached is not None:
        return cached
    created = story.start
    pointed = _first_event_at(story, "pointed")
    sprint = _first_event_at(story, "sprint_added")
    started = _first_status(story, "in_progress")
    # The refinement boundary is the first status → ``ready`` *before* work
    # started: a ``done → ready`` reopen (prd/36 §4) moves a finished story back
    # to ready, which is not the refinement the first segment measures.
    ready_stamps = sorted(
        event["_ts"] for event in story.events
        if event["activity"] == "status_changed"
        and normalize_stage(_attrs(event).get("to_status")) == "ready"
    )
    ready_status = next(
        (stamp for stamp in ready_stamps if started is None or stamp <= started),
        None,
    )
    # ``refined`` stays the estimate (byte-compatible with prd/21), falling back
    # to the workflow's "ready" status for a store that refines without pointing.
    refined = pointed or ready_status
    ready = ready_status or refined
    in_review = _first_status(story, "in_review")
    in_verification = _first_status(story, "in_verification")
    # prd/36 §4: a story can be Done more than once. ``done`` is the *final*
    # done (the end state the ticket really reached); ``first_done`` is the end
    # of the first cycle, and the difference between them is the reopen.
    done_stamps = sorted(
        event["_ts"] for event in story.events
        if event["activity"] == "status_changed"
        and normalize_stage(_attrs(event).get("to_status")) == "done"
        and (started is None or event["_ts"] >= started)
    )
    first_done = done_stamps[0] if done_stamps else None
    reopens = [
        {
            "ts": event["_ts"],
            "from": normalize_stage(_attrs(event).get("from_status")),
            "to": normalize_stage(_attrs(event).get("to_status")),
        }
        for event in story.events
        if event["activity"] == "status_changed"
        and normalize_stage(_attrs(event).get("from_status")) == "done"
        and normalize_stage(_attrs(event).get("to_status")) != "done"
        and (first_done is None or event["_ts"] >= first_done)
    ]
    # prd/36 §4: the *final* done is the end state only when the story has not
    # been reopened after it. A story done → ready and left open is still open:
    # the first done is reported, the cycle stays right-censored.
    reopened_and_open = bool(
        reopens and done_stamps and reopens[-1]["ts"] >= done_stamps[-1]
    )
    if reopened_and_open:
        done = None
    else:
        done = done_stamps[-1] if done_stamps else story.closed

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
        "ready": ready,
        "refined": refined,
        "sprint": sprint,
        "started": started,
        "in_review": in_review,
        "in_verification": in_verification,
        "pr_open": pr_open,
        "first_review": first_review,
        "approval": approval,
        "merged": merged,
        "done": done,
        "first_done": first_done,
    }
    # ``first_done`` is reported but is not a segment boundary of its own; a
    # story that never closed must not gain a "missing" entry for it.
    missing = [
        name for name, value in boundaries.items()
        if value is None and name != "first_done"
    ]

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
        # prd/40 + concepts.md §6: one definition of a review round, shared
        # with rules._rounds — after the first review, no pure rebase that lost
        # no approval, no push-induced unapproval counted twice.
        pushes, unapprovals = ballcourt.review_round_events(state)
        rounds += len(pushes) + len(unapprovals)
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
    cycle_end = done or story.snapshot
    segment_bounds, out_of_order = _ordered_segment_bounds(boundaries, cycle_end)
    cycle = Cycle(
        story=story,
        person=person,
        boundaries=boundaries,
        spans=spans,
        touches=sorted(touches),
        review_rounds=rounds,
        missing=missing,
        pr_gaps=between_pr_gaps(story),
        reopens=reopens,
        segment_bounds=segment_bounds,
        out_of_order=out_of_order,
    )
    story._cycle_cache = cycle
    return cycle


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


def cycle_for(story: Story) -> Cycle:
    """Public name for the boundary/evidence derivation (prd/25 detail view)."""
    return _cycle(story)


def active_hours(
    court: ballcourt.BallCourt,
    cycle: Cycle,
    t1: datetime,
    t2: datetime,
) -> float:
    """Public name for the measured-work accounting inside one segment."""
    return _active_hours(court, cycle, t1, t2)


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
    verification = uses_verification(data)
    keys = segment_keys({}, verification=verification)
    segments: dict[str, dict] = {
        key: {
            "hours": 0.0, "active_hours": 0.0, "n": 0, "missing": 0,
            "inverted": 0, "efficiencies": [], "label": SEGMENT_LABEL[key],
            # prd/36 §2: the part of the segment with no PR open at all
            "no_pr_open_hours": 0.0,
        }
        for key in keys
    }
    missing_boundaries: dict[str, int] = defaultdict(int)
    rows: list[dict] = []
    cycle_total = 0.0
    clamped = 0
    decomposed = 0
    rounds_total = 0
    reopened_stories = 0
    out_of_order: list[str] = []
    per_story_largest: list[float] = []

    for story in stories:
        cycle = _cycle(story)
        for name in cycle.missing:
            missing_boundaries[name] += 1
        if cycle.reopens:
            reopened_stories += 1
        if cycle.out_of_order:
            out_of_order.append(story.key)
        end = cycle.boundaries["done"] or court.snapshot
        begin = cycle.boundaries["created"]
        cycle_hours = (
            round(court.business_hours(cycle.person, begin, end), 2)
            if begin is not None and end is not None and end > begin else 0.0
        )
        cycle_total += cycle_hours
        row: dict = {"key": story.key, "cycle_hours": cycle_hours, "segments": {}}
        measured = 0
        for key in keys:
            left, right = SEGMENT_BOUNDS[key]
            # prd/46 §1: the *ordered* boundaries price the segment; the raw
            # ones decide "missing" and name an inversion. That way the shares
            # sum to at most the cycle while the store's out-of-order data is
            # still reported (an inverted segment stays a 0 h inversion).
            t1_raw = cycle.boundaries.get(left)
            t2_raw = cycle.boundaries.get(right)
            t1 = cycle.segment_bounds.get(left)
            t2 = cycle.segment_bounds.get(right)
            entry = segments[key]
            if t1_raw is None or t2_raw is None:
                entry["missing"] += 1
                row["segments"][key] = None
                continue
            if t2_raw < t1_raw:
                entry["inverted"] += 1
            elapsed = round(court.business_hours(cycle.person, t1, t2), 2)
            active = round(_active_hours(court, cycle, t1, t2), 2)
            if active > elapsed:
                active = elapsed
                clamped += 1
            no_pr = round(sum(
                court.business_hours(cycle.person, low, high)
                for low, high in no_pr_open_spans(story, t1, t2)
            ), 2)
            entry["hours"] += elapsed
            entry["active_hours"] += active
            entry["no_pr_open_hours"] += no_pr
            entry["n"] += 1
            if elapsed > 0:
                entry["efficiencies"].append(round(active / elapsed, 4))
            row["segments"][key] = elapsed
            if no_pr:
                row.setdefault("no_pr_open", {})[key] = no_pr
            measured += 1
        if measured:
            decomposed += 1
        rows.append(row)
        rounds_total += cycle.review_rounds

    total_hours = round(sum(entry["hours"] for entry in segments.values()), 2)
    out_segments: dict[str, dict] = {}
    for key in keys:
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
            # prd/36 §2: hours in this segment with no PR open — stated, never
            # folded silently into the segment's headline
            "no_pr_open_hours": round(entry["no_pr_open_hours"], 2),
            "no_pr_open_share": _share(entry["no_pr_open_hours"], hours),
            "n": entry["n"],
            "missing": entry["missing"],
            "inverted": entry["inverted"],
            "bounds": list(SEGMENT_BOUNDS[key]),
        }
    largest = max(keys, key=lambda key: (out_segments[key]["hours"], key))
    # prd/41 B4: the top two segments are often within a point of each other;
    # naming one as "the" bottleneck would be a ranking the data does not
    # support, so the tie is stated.
    runners = sorted(
        (key for key in keys if key != largest),
        key=lambda key: (-out_segments[key]["hours"], key),
    )
    runner_up = runners[0] if runners else None
    runner_share = out_segments[runner_up]["share"] if runner_up else None
    tie_points = (
        round((out_segments[largest]["share"] - runner_share) * 100.0, 2)
        if runner_share is not None else None
    )
    tied = bool(tie_points is not None and tie_points <= SEGMENT_TIE_POINTS)
    for row in rows:
        # prd/26 P5: which stories fed the largest segment, and by how much
        row["largest_hours"] = row["segments"].get(largest)
        row["affected"] = bool(row["largest_hours"])
        if row["largest_hours"]:
            per_story_largest.append(row["largest_hours"])
    largest_examples = [
        key for key, hours in sorted(
            (
                (row["key"], row["segments"].get(largest))
                for row in rows
                if row["segments"].get(largest)
            ),
            key=lambda item: (-item[1], item[0]),
        )[:5]
    ]
    available = any(entry["n"] for entry in segments.values())
    return {
        "available": available,
        "stories": len(stories),
        "stories_decomposed": decomposed,
        "segments": out_segments,
        "order": list(keys),
        "total_hours": total_hours,
        "cycle_hours": round(cycle_total, 2),
        "explained_share": _share(total_hours, cycle_total),
        # prd/46 §1: an out-of-order boundary is a data quirk, counted and named
        # (never silently drawn as an overlapping segment). The hours above are
        # clamped to the cycle, so shares cannot sum past 100 %.
        "out_of_order_stories": len(out_of_order),
        "out_of_order_examples": sorted(out_of_order)[:5],
        "largest": largest,
        "largest_label": SEGMENT_LABEL[largest],
        "largest_hours": out_segments[largest]["hours"],
        "largest_share": out_segments[largest]["share"],
        "largest_efficiency": out_segments[largest]["efficiency"],
        # prd/41 B4: the runner-up and the tie verdict travel with the largest
        # segment, so no view can quote one as the bottleneck without the other
        "runner_up": runner_up,
        "runner_up_label": SEGMENT_LABEL.get(runner_up) if runner_up else None,
        "runner_up_hours": out_segments[runner_up]["hours"] if runner_up else None,
        "runner_up_share": runner_share,
        "tie_points": tie_points,
        "tie_threshold_points": SEGMENT_TIE_POINTS,
        "tied": tied,
        # concepts.md §6: "per story" is a statistic over stories, never the
        # cohort total divided by anything. The rule quotes this number.
        "largest_per_story_hours": _median(per_story_largest),
        "largest_per_story_mean_hours": (
            round(sum(per_story_largest) / len(per_story_largest), 2)
            if per_story_largest else None
        ),
        "largest_per_story_basis": "median across the stories that measured it",
        "largest_examples": largest_examples,
        "review_rounds": rounds_total,
        "reopened_stories": reopened_stories,
        "reopens": reopened_stories,
        "no_pr_open_hours": round(
            sum(entry["no_pr_open_hours"] for entry in out_segments.values()), 2
        ),
        "missing_boundaries": dict(sorted(missing_boundaries.items())),
        "clamped": clamped,
        "hours_mode": court.hours_mode,
        "rows": rows,
        "heuristic": (
            "created → ready = created to the first status → ready, falling back "
            "to the first 'pointed' event (the estimate refinement produces); "
            "ready → sprint = that same ready boundary to the first "
            "'sprint_added'; in progress / done = the status *stage* transitions "
            "(the Jira category when no [jira.stages] map is configured); in "
            "review / in verification bound the review and QA waits when the "
            "workflow map names those stages; PR open / first review / approval / "
            "merge come from the story's PRs via PR.ticket_keys. A segment needs "
            "both boundaries, otherwise it is counted as missing"
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
            available,
        ),
    }


# --------------------------------------------------------------------------- #
# 2. carry-over (prd/21 §2, prd/37 §1)
# --------------------------------------------------------------------------- #
def _sprint_index(data: JiraData) -> dict[int, dict]:
    """Sprint id -> SPRINT object (a membership without one stays unknown)."""
    return {
        sprint["id"]: sprint
        for sprint in data.sprints.values()
        if sprint.get("id") is not None
    }


def sprint_boundary(sprint: dict | None, court) -> datetime | None:
    """The moment a sprint's plan closed: its measured end, else the snapshot.

    concepts.md §6 / prd/37: a story is "done in S" iff it reached done **by
    S's end**, not "by now". An active sprint has no end yet, so its boundary is
    the snapshot and nothing can be late in it.
    """
    if sprint is None:
        return None
    return sprint.get("end") or (court.snapshot if court is not None else None)


def sprint_closed(sprint: dict | None) -> bool:
    """Has this sprint's end passed?

    The store's ``state`` is the authority when a completion timestamp was lost:
    a sprint Jira closed is closed, even if the store kept no ``closed_at``.
    """
    if sprint is None:
        return False
    return sprint.get("end") is not None or sprint.get("state") == "closed"


def _done_at(story: Story) -> datetime | None:
    """When the story first reached done (the status event, else its close)."""
    return _first_status(story, "done") or (
        story.closed if story.state == "done" else None
    )


def story_unmerged_prs(story: Story) -> list[str]:
    """The story's PRs that are still open — the code never merged (prd/43 §5).

    Jira "Done" is a workflow label, not delivery: DEL-167 was closed while PR
    #53 was still open (and DEL-168/183 likewise). A story whose own PR is open
    is finished *paperwork*, not merged code, so the sprint's "done by end" must
    not count it. A **declined** PR is not counted here: it is a closed decision,
    not a delivery still in flight (concepts.md §9 "Done in Jira, PR open"). A
    story with no PR at all is not affected — there is no code evidence to
    contradict the ticket.
    """
    return [
        state.pr["object_id"] for state in story.prs
        if state.pr.get("closed_ts") is None
    ]


def sprint_outcomes(court, data: JiraData) -> tuple[dict[str, dict], dict[str, dict],
                                                    dict[int, dict]]:
    """Membership + per-sprint outcome for every story in the store.

    The one derivation the sprint lens, carry-over and the on-time rule share
    (concepts.md §6). For each story and each sprint it was committed to:

    * ``by_end`` — reached done at or before the sprint's close;
    * ``late``   — reached done after the close;
    * ``never``  — had not reached done at the snapshot;
    * ``unknown``— the store has no SPRINT object (hence no boundary) for the id.

    Returns ``(memberships, outcomes, boundaries)`` where ``outcomes[key]`` is
    ``{"done_at", "outcomes": {sprint_id: outcome}}``.
    """
    index = _sprint_index(data)
    memberships: dict[str, dict] = {}
    boundaries: dict[int, dict] = {}
    for sid, sprint in index.items():
        boundaries[sid] = {
            "sprint": sprint,
            "end": sprint_boundary(sprint, court),
            "closed": sprint_closed(sprint),
        }
    outcomes: dict[str, dict] = {}
    for story in data.stories:
        membership = _membership(story)
        memberships[story.key] = membership
        done = _done_at(story)
        unmerged = story_unmerged_prs(story)
        per: dict[int, str] = {}
        for sid in membership["sprints"]:
            boundary = (boundaries.get(sid) or {}).get("end")
            if boundary is None:
                per[sid] = "unknown"
            elif done is None:
                per[sid] = "never"
            elif done <= boundary:
                per[sid] = "by_end"
            else:
                per[sid] = "late"
        outcomes[story.key] = {
            "done_at": done,
            "outcomes": per,
            # prd/43 §5: Jira done ≠ delivered; the sprint counts split these.
            "unmerged_prs": unmerged,
            "delivered": bool(done is not None and not unmerged),
        }
    return memberships, outcomes, boundaries


def _missed_sprints(membership: dict, outcome: dict, boundaries: dict) -> list[int]:
    """Closed sprints a story was committed to but did not finish by the close."""
    missed: list[int] = []
    for sid in membership["sprints"]:
        boundary = boundaries.get(sid)
        if boundary is None or not boundary["closed"]:
            continue
        if outcome["outcomes"].get(sid) in ("late", "never"):
            missed.append(sid)
    return missed


def _slip_hours(court, story: Story, outcome: dict, boundaries: dict,
                missed: list[int]) -> float:
    """Business hours from the first missed close to done (or the snapshot)."""
    if not missed:
        return 0.0
    start = min(boundaries[sid]["end"] for sid in missed if boundaries[sid]["end"])
    end = outcome["done_at"] or (court.snapshot if court is not None else None)
    if start is None or end is None or end <= start:
        return 0.0
    return round(court.business_hours(story.actor, start, end), 2)


def carry_over(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
) -> dict:
    """Stories committed to a sprint but not done by its close (prd/37 §1).

    The old derivation only counted an explicit ``sprint_added`` re-add into a
    second sprint, so the stories that finished *late* (or never) while still
    attached to the sprint they missed were invisible — the "0 carried over"
    that hid 20 late stories in the del-combined data. The definition now
    follows concepts.md §6: a story is carried over when a **closed** sprint it
    was committed to ended before the story reached ``done``.

    ``wait_hours`` per row is the slip: from the first missed sprint's close to
    ``done`` (or the snapshot while it is still open), priced on the assignee's
    calendar. ``status`` says whether the story finished late or never;
    ``explicit_readds`` keeps the old multi-sprint count as context, never as
    the headline.
    """
    stories = cohort(data, window)
    memberships, outcomes, boundaries = sprint_outcomes(court, data)
    rows: list[dict] = []
    readded = 0
    for story in stories:
        membership = memberships[story.key]
        outcome = outcomes[story.key]
        added = story.of("sprint_added")
        if not added:
            continue
        if len(membership["sprints"]) > 1:
            readded += 1
        missed = _missed_sprints(membership, outcome, boundaries)
        if not missed:
            continue
        wait = _slip_hours(court, story, outcome, boundaries, missed)
        done = outcome["done_at"]
        rows.append({
            "kind": "story",
            "object_id": story.object_id,
            "key": story.key,
            "tickets": [story.key],
            "sprints": membership["sprints"],
            "missed_sprints": missed,
            "status": "late" if done is not None else "never",
            "done_at": iso(done) if done else None,
            "added_events": len(added),
            "first_added": iso(min(event["_ts"] for event in added)),
            "started": iso(_first_status(story, "in_progress")) if _first_status(story, "in_progress") else None,
            "wait_hours": wait,
            "slip_hours": wait,
            "points": story.points,
            # prd/26 P5: every row in this metric is a carried-over story
            "affected": True,
        })
    rows.sort(key=lambda row: (-row["wait_hours"], row["key"]))
    carried = len(rows)
    with_sprints = sum(1 for story in stories if memberships[story.key]["sprints"])
    late = sum(1 for row in rows if row["status"] == "late")
    never = carried - late
    return {
        "available": with_sprints > 0,
        "stories": len(stories),
        "stories_in_a_sprint": with_sprints,
        # prd/41 B8: the cohort is the stories *created in the window*, while
        # the sprint lens counts every story in the store. Both are right for
        # their scope; the window travels so the two counts can never be read as
        # one fact.
        "window": {
            "start": iso(window[0]),
            "end": iso(window[1]),
            "days": round((window[1] - window[0]).total_seconds() / 86400.0, 1),
        },
        "carried_over": carried,
        "share": _share(carried, with_sprints),
        "share_of_stories": _share(carried, len(stories)),
        "late": late,
        "never": never,
        "wait_hours": round(sum(row["wait_hours"] for row in rows), 2),
        # the slip of the stories that *did* finish late: the recoverable part
        # the carry-over rule prices (never-done stories are still open work)
        "late_hours": round(
            sum(row["wait_hours"] for row in rows if row["status"] == "late"), 2
        ),
        "median_wait_hours": _median([row["wait_hours"] for row in rows]),
        "sprints_involved": len({sid for row in rows for sid in row["missed_sprints"]}),
        # the old signal, kept as context: an explicit re-add into another sprint
        "explicit_readds": readded,
        "readded_same_sprint": sum(
            1 for story in stories
            if len(story.of("sprint_added")) > len(memberships[story.key]["sprints"])
        ),
        "rows": rows,
        "target_share": CARRY_OVER_SHARE_TARGET,
        "heuristic": (
            "a story is carried over when a closed sprint it was committed to "
            "ended before the story reached done; wait_hours is that close → "
            "done (or the snapshot), priced on the assignee's calendar"
        ),
        "note": _note(
            data,
            "no 'sprint_added' event in this window, so there is no sprint "
            "membership to check for carry-over",
            with_sprints > 0,
        ),
    }


# --------------------------------------------------------------------------- #
# 2a. sprint on-time delivery (prd/37 §1, §6)
# --------------------------------------------------------------------------- #
def _added_at_start(story: Story, sid: int, sprint_index: dict) -> bool:
    """Was this story in sprint ``sid`` when the sprint started? (prd/46 §4)

    ``at_start`` is the store's own membership-at-start marker (prd/42 M5); a
    ``sprint_added`` stamped at or before the sprint's start is the same fact
    from the timestamp. Anything later was *adopted* mid-sprint, not committed.
    """
    start = (sprint_index.get(sid) or {}).get("start")
    for event in story.of("sprint_added"):
        if event_sprint_id(event) != sid:
            continue
        return bool(_attrs(event).get("at_start")) or (
            start is not None and event["_ts"] <= start
        )
    return False


def sprint_delivery(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
) -> dict:
    """Did each closed sprint deliver what it committed, measured at its close?

    One row per **closed** sprint (an active sprint has not had its end yet, so
    nothing in it is late). The counts are the three outcomes of concepts.md §6
    — ``done_by_end`` / ``done_late`` / ``never_done`` — and ``late_hours`` is
    the business time between the close and the eventual done, the cost of the
    slip. The rules page turns the worst sprint into the on-time-delivery rule.
    """
    memberships, outcomes, boundaries = sprint_outcomes(court, data)
    sprint_index = _sprint_index(data)
    sprints: list[dict] = []
    rows: list[dict] = []
    for sid in sorted(boundaries):
        boundary = boundaries[sid]
        sprint = boundary["sprint"]
        if not boundary["closed"]:
            continue
        members = [
            story for story in data.stories
            if sid in memberships[story.key]["sprints"]
        ]
        if not members:
            continue
        counts = {"by_end": 0, "late": 0, "never": 0, "unmerged": 0}
        points = {"by_end": 0, "late": 0, "never": 0, "unmerged": 0}
        # prd/46 §4: "committed" is the scope at the sprint's start; a story
        # added mid-sprint was adopted, not committed.
        committed_at_start = adopted_count = 0
        committed_points = adopted_points = 0
        committed_done = adopted_done = 0
        late_hours = 0.0
        for story in members:
            outcome = outcomes[story.key]["outcomes"].get(sid)
            if outcome not in counts:
                continue
            at_start = _added_at_start(story, sid, sprint_index)
            if at_start:
                committed_at_start += 1
                committed_points += story.points or 0
            else:
                adopted_count += 1
                adopted_points += story.points or 0
            # prd/43 §5: Jira "done by end" is not delivery when the story's
            # PRs never merged. The count moves to `unmerged`, so the committed
            # total is unchanged but "delivered" is the true one.
            delivered = story.key in outcomes and outcomes[story.key]["delivered"]
            if outcome == "by_end" and not delivered:
                counts["unmerged"] += 1
                points["unmerged"] += story.points or 0
                rows.append({
                    "kind": "story",
                    "object_id": story.object_id,
                    "key": story.key,
                    "tickets": [story.key],
                    "sprint_id": sid,
                    "status": "unmerged",
                    "points": story.points,
                    "slip_hours": None,
                    "done_at": iso(outcomes[story.key]["done_at"]),
                    "unmerged_prs": list(outcomes[story.key]["unmerged_prs"]),
                    "affected": True,
                })
                continue
            counts[outcome] += 1
            points[outcome] += story.points or 0
            if outcome == "by_end":
                if at_start:
                    committed_done += 1
                else:
                    adopted_done += 1
            if outcome == "late":
                done = outcomes[story.key]["done_at"]
                start = boundary["end"]
                if start is not None and done is not None and done > start:
                    late_hours += court.business_hours(story.actor, start, done)
                rows.append({
                    "kind": "story",
                    "object_id": story.object_id,
                    "key": story.key,
                    "tickets": [story.key],
                    "sprint_id": sid,
                    "status": "late",
                    "points": story.points,
                    "slip_hours": round(
                        court.business_hours(story.actor, start, done), 2
                    ) if start is not None and done is not None else 0.0,
                    "done_at": iso(done),
                    "affected": True,
                })
            elif outcome == "never":
                rows.append({
                    "kind": "story",
                    "object_id": story.object_id,
                    "key": story.key,
                    "tickets": [story.key],
                    "sprint_id": sid,
                    "status": "never",
                    "points": story.points,
                    "slip_hours": None,
                    "done_at": None,
                    "affected": False,
                })
        committed = sum(counts.values())
        delivered = counts["by_end"]
        share = _share(delivered, committed)
        sprint_row = {
            "sprint_id": sid,
            "object_id": sprint.get("object_id"),
            "state": sprint.get("state"),
            "start": iso(sprint["start"]) if sprint.get("start") else None,
            "end": iso(boundary["end"]) if boundary["end"] else None,
            "planned_end": sprint.get("planned_end"),
            "stories": committed,
            "points": sum(points.values()),
            "done_by_end": delivered,
            # prd/46 §4: scope at the start vs added after it. `stories` stays
            # the whole scope the sprint ended with; "committed" is the plan.
            "committed": committed_at_start,
            "adopted": adopted_count,
            "committed_points": committed_points,
            "adopted_points": adopted_points,
            "committed_done_by_end": committed_done,
            "adopted_done_by_end": adopted_done,
            # prd/46 §4 / QA: 50 % by stories is not 50 % by points; both are
            # stated so the denominator cannot be read as the other.
            "points_share": _share(points["by_end"], sum(points.values())),
            # prd/43 §5: Jira-done by the close but the code never merged
            "done_by_end_jira": counts["by_end"] + counts["unmerged"],
            "unmerged": counts["unmerged"],
            "unmerged_points": points["unmerged"],
            "done_late": counts["late"],
            "never_done": counts["never"],
            "done_by_end_points": points["by_end"],
            "late_points": points["late"],
            "never_points": points["never"],
            "done_share": share,
            "late_hours": round(late_hours, 2),
            "on_time": committed > 0 and delivered == committed,
            "measured": committed >= SPRINT_MIN_STORIES,
        }
        sprints.append(sprint_row)
    # the drill-down is one row per (story, sprint), worst slip first
    rows.sort(key=lambda row: (
        -(row.get("slip_hours") or 0.0), row["key"], row["sprint_id"]
    ))
    measured = [row for row in sprints if row["measured"]]
    worst = min(
        measured,
        key=lambda row: (row["done_share"], -row["stories"], row["sprint_id"]),
        default=None,
    )
    late_rows = [row for row in rows if row["status"] == "late"]
    never_rows = [row for row in rows if row["status"] == "never"]
    return {
        "available": bool(sprints),
        "target_share": SPRINT_ON_TIME_TARGET,
        "min_stories": SPRINT_MIN_STORIES,
        "closed_sprints": len(sprints),
        "measured_sprints": len(measured),
        "on_time_sprints": sum(1 for row in sprints if row["on_time"]),
        "stories": sum(row["stories"] for row in sprints),
        "done_by_end": sum(row["done_by_end"] for row in sprints),
        "committed": sum(row["committed"] for row in sprints),
        "adopted": sum(row["adopted"] for row in sprints),
        "unmerged": sum(row["unmerged"] for row in sprints),
        "done_late": len(late_rows),
        "never_done": len(never_rows),
        "late_hours": round(sum(row.get("slip_hours") or 0.0 for row in late_rows), 2),
        "worst": worst,
        "sprints": sprints,
        "rows": rows,
        "note": (
            None if sprints else
            "no closed sprint with committed stories in this store: on-time "
            "delivery is measured at a sprint's close, so an active sprint has "
            "no verdict yet"
        ),
    }


# --------------------------------------------------------------------------- #
# 2b. the sprint lens (prd/26 P6)
# --------------------------------------------------------------------------- #
def _membership(story: Story) -> dict:
    """One story's sprint membership: the sprints, the current one, the marks.

    ``sprint_added`` is the commitment; ``sprint_removed`` takes it back. The
    **current** sprint is the last add whose sprint was not removed after it, so
    a story pulled from a sprint back to the backlog reads as out of scope
    rather than as still committed.

    ``carried_over`` follows :func:`carry_over`: more than one *distinct* sprint
    in the adds. A move out and back into the same sprint is not carry-over.
    """
    adds: list[tuple[datetime, int, bool]] = []
    removes: dict[int, datetime] = {}
    for event in story.of("sprint_added"):
        sid = event_sprint_id(event)
        if sid is not None:
            adds.append((event["_ts"], sid, bool(_attrs(event).get("at_start"))))
    for event in story.of("sprint_removed"):
        sid = event_sprint_id(event)
        if sid is not None:
            removes[sid] = max(removes.get(sid, event["_ts"]), event["_ts"])
    sprints: list[int] = []
    for _, sid, _ in adds:
        if sid not in sprints:
            sprints.append(sid)
    current: int | None = None
    for ts, sid, _ in adds:
        if removes.get(sid) is not None and removes[sid] > ts:
            continue
        current = sid
    return {
        "sprints": sprints,
        "first_sprint": sprints[0] if sprints else None,
        "last_sprint": sprints[-1] if sprints else None,
        "current_sprint": current,
        "added": iso(adds[0][0]) if adds else None,
        "removed": iso(max(removes.values())) if removes else None,
        "at_start": any(at_start for _, _, at_start in adds),
        "carried_over": len(sprints) > 1,
        "adds": len(adds),
    }


def sprints(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
) -> dict:
    """The sprint lens (prd/26 P6): bands, scope and carry-over.

    Three questions, one payload:

    * **bands** — one per SPRINT object over its measured window: ``start`` to
      its completion (``closed_at``) for a closed sprint, ``start`` to the
      snapshot for the active one. ``planned_end`` is the plan, never the band:
      an active sprint's band ends where the data ends, so it cannot pretend to
      have run to a date the store has not seen.
    * **scope** — of the stories created in the window, how many were ever
      committed to a sprint and how many were not (``out_of_sprint`` lists
      them). Per sprint, the committed stories, their points, and how many are
      done — the sprint's own scope, not the window's.
    * **carry-over** — a story whose ``sprint_added`` names more than one
      distinct sprint. The mark is per story row and per sprint
      (``carried_in`` / ``carried_out``), so the timeline can draw it on the
      lane and the strip can count it.

    Sprint membership is computed over **every** story in the store (a sprint's
    own scope must not shrink to whatever the timeframe happens to include),
    while ``scope`` and ``rows`` describe the cohort created in the window.

    prd/37 §1: "done" is measured **at the sprint's close**, never "by now".
    Each band carries the ``done_by_end`` / ``done_late`` / ``never_done``
    breakdown (stories and points), and carry-over is derived from "committed
    but not done by the close", not only from an explicit re-add into a second
    sprint — the stories that finished late while still attached to the sprint
    they missed are the ones the old count hid.
    """
    stories = list(data.stories)
    in_window = cohort(data, window)
    memberships, outcomes, boundaries = sprint_outcomes(court, data)
    sprint_index = _sprint_index(data)

    # -- per-sprint scope ---------------------------------------------------
    def _empty_sprint_row(sprint: dict | None, sid: int) -> dict:
        band_start = sprint.get("start") if sprint else None
        band_end = (sprint.get("end") or court.snapshot) if sprint else None
        boundary = boundaries.get(sid) or {}
        return {
            "sprint_id": sid,
            "object_id": sprint.get("object_id") if sprint else None,
            "state": sprint.get("state") if sprint else None,
            "start": iso(sprint["start"]) if sprint and sprint.get("start") else None,
            "end": iso(sprint["end"]) if sprint and sprint.get("end") else None,
            "planned_end": sprint.get("planned_end") if sprint else None,
            "goal_class": sprint.get("goal_class") if sprint else None,
            "open": bool(sprint and sprint.get("end") is None),
            # prd/37: the close "done by end" is measured at
            "closed": bool(boundary.get("closed")),
            "band_start": iso(band_start) if band_start else None,
            # an active sprint's band ends at the data, not at the plan
            "band_end": iso(band_end) if band_start else None,
            "days": (
                round((band_end - band_start).total_seconds() / 86400.0, 1)
                if band_start and band_end and band_end > band_start else None
            ),
            "stories": [],
            "current_stories": [],
            "carried_in": [],
            "carried_out": [],
            # prd/42 M5: a story added after the sprint's start was not part of
            # the commitment the sprint was planned with. `at_start` is the
            # store's own membership-at-start signal; a later add without it is
            # the scope-creep fact.
            "added_after_start_stories": [],
            "committed_stories": [],
            "adopted_stories": [],
            "committed_points": 0,
            "adopted_points": 0,
            "points": 0,
            "done_points": 0,
            "done": 0,
            "done_by_end": 0,
            "unmerged": 0,
            "done_late": 0,
            "never_done": 0,
            "late_points": 0,
            "never_points": 0,
            "unmerged_points": 0,
            "late_hours": 0.0,
            "open_stories": 0,
        }

    sprint_rows: dict[int, dict] = {}
    for sid, sprint in sorted(
        ((sid, sprint) for sid, sprint in data.sprints.items()
         if sprint.get("id") is not None),
        key=lambda item: (
            item[1].get("start") or datetime.max, item[0]
        ),
    ):
        sprint_rows[sprint["id"]] = _empty_sprint_row(sprint, sprint["id"])

    by_key = {story.key: story for story in stories}
    for key, membership in memberships.items():
        story = by_key.get(key)
        if story is None:
            continue
        outcome = outcomes[key]
        for index, sid in enumerate(membership["sprints"]):
            row = sprint_rows.get(sid)
            if row is None:
                # a membership for a sprint object the store did not keep:
                # still a fact, reported without a band rather than dropped
                row = sprint_rows[sid] = _empty_sprint_row(None, sid)
                row["note"] = (
                    "no SPRINT object carries this id; the membership event does"
                )
            row["stories"].append(key)
            if membership["current_sprint"] == sid:
                row["current_stories"].append(key)
            # prd/46 §4: committed at the start vs adopted mid-sprint. One
            # definition shared with the sprint-delivery rule (`_added_at_start`).
            points = story.points or 0
            if _added_at_start(story, sid, sprint_index):
                row["committed_stories"].append(key)
                row["committed_points"] += points
            else:
                row["adopted_stories"].append(key)
                row["adopted_points"] += points
            # prd/42 M5: an add after the sprint's measured start, without the
            # `at_start` marker, is scope added mid-sprint.
            band_start = (sprint_index.get(sid) or {}).get("start")
            for event in story.of("sprint_added"):
                if event_sprint_id(event) != sid:
                    continue
                at_start = bool(_attrs(event).get("at_start"))
                if (
                    not at_start and band_start is not None
                    and event["_ts"] > band_start + SPRINT_SCOPE_CREEP_GRACE
                    and key not in row["added_after_start_stories"]
                ):
                    row["added_after_start_stories"].append(key)
            verdict = outcome["outcomes"].get(sid)
            delivered = bool(outcome.get("delivered"))
            row["points"] += points
            if verdict == "by_end" and not delivered:
                # prd/43 §5: done in Jira, PR unmerged — not delivered work
                row["unmerged"] += 1
                row["unmerged_points"] += points
            elif verdict == "by_end":
                row["done"] += 1
                row["done_by_end"] += 1
                row["done_points"] += points
            elif verdict == "late":
                row["done_late"] += 1
                row["late_points"] += points
                done = outcome["done_at"]
                start = (boundaries.get(sid) or {}).get("end")
                if start is not None and done is not None and done > start:
                    row["late_hours"] += court.business_hours(story.actor, start, done)
            elif verdict == "never":
                row["never_done"] += 1
                row["never_points"] += points
                row["open_stories"] += 1
            if verdict in ("late", "never") and (boundaries.get(sid) or {}).get("closed"):
                row["carried_out"].append(key)
            if index > 0:
                previous = membership["sprints"][index - 1]
                previous_boundary = boundaries.get(previous) or {}
                if (
                    previous_boundary.get("closed")
                    and outcome["outcomes"].get(previous) in ("late", "never")
                ):
                    row["carried_in"].append(key)

    bands: list[dict] = []
    for sid in sorted(
        sprint_rows,
        key=lambda sid: (
            sprint_rows[sid]["start"] or "9999", sid
        ),
    ):
        row = sprint_rows[sid]
        row["stories"].sort()
        row["current_stories"].sort()
        row["carried_in"].sort()
        row["carried_out"].sort()
        row["story_count"] = len(row["stories"])
        row["current_count"] = len(row["current_stories"])
        row["added_after_start_stories"].sort()
        row["added_after_start"] = len(row["added_after_start_stories"])
        row["added_after_start_share"] = _share(
            row["added_after_start"], row["story_count"]
        )
        # prd/46 §4: the planned scope and the adopted scope, counted from the
        # same at-start signal the sprint-delivery rule uses.
        row["committed_stories"].sort()
        row["adopted_stories"].sort()
        row["committed"] = len(row["committed_stories"])
        row["adopted"] = len(row["adopted_stories"])
        row["committed_share"] = _share(row["committed"], row["story_count"])
        committed = (
            row["done_by_end"] + row["done_late"] + row["never_done"]
            + row["unmerged"]
        )
        row["done_share"] = _share(row["done_by_end"], committed)
        row["on_time"] = committed > 0 and row["done_by_end"] == committed
        row["late_hours"] = round(row["late_hours"], 2)
        bands.append(row)

    # -- scope over the window's cohort -------------------------------------
    # "in a sprint" is the *current* commitment: a story pulled back to the
    # backlog is out of scope again, and is reported separately rather than
    # counted as planned because it was planned once.
    ever = [story for story in in_window if memberships[story.key]["sprints"]]
    planned = [
        story for story in in_window
        if memberships[story.key]["current_sprint"] is not None
    ]
    unplanned = [
        story for story in in_window
        if memberships[story.key]["current_sprint"] is None
    ]
    never_added = [story for story in unplanned if not memberships[story.key]["sprints"]]
    removed = [story for story in unplanned if memberships[story.key]["sprints"]]
    carried = [
        story for story in in_window
        if _missed_sprints(memberships[story.key], outcomes[story.key], boundaries)
    ]

    rows: list[dict] = []
    for story in in_window:
        membership = memberships[story.key]
        outcome = outcomes[story.key]
        missed = _missed_sprints(membership, outcome, boundaries)
        done = outcome["done_at"]
        rows.append({
            "kind": "story",
            "object_id": story.object_id,
            "key": story.key,
            "tickets": [story.key],
            "state": story.state,
            "points": story.points,
            "created": iso(story.start) if story.start else None,
            **membership,
            # prd/37: carry-over is "committed but not done by the close"; the
            # multi-sprint re-add is kept separately as `explicit_readd`.
            "explicit_readd": membership["carried_over"],
            "carried_over": bool(missed),
            "missed_sprints": missed,
            "sprint_outcomes": {
                str(sid): verdict for sid, verdict in outcome["outcomes"].items()
            },
            "done_at": iso(done) if done else None,
            "slip_hours": _slip_hours(court, story, outcome, boundaries, missed),
            "in_sprint": membership["current_sprint"] is not None,
            "affected": bool(missed),
        })
    rows.sort(key=lambda row: (
        not row["carried_over"],
        -row["slip_hours"],
        row["first_sprint"] if row["first_sprint"] is not None else 10 ** 9,
        row["key"],
    ))

    ordered_bands = [
        band for band in bands if band.get("sprint_id") is not None
    ]

    def _destination(sid: int | None, sprints: list[int]) -> tuple[int | None, bool | None]:
        """The next sprint a carried-over story landed in, and whether it did.

        prd/42 M5: "carried over" is only half the fact — the other half is
        where it went. A story that never reappears in the *next* sprint was
        silently dropped, not re-committed.
        """
        if sid is None:
            return None, None
        index = next(
            (i for i, band in enumerate(ordered_bands)
             if band["sprint_id"] == sid),
            None,
        )
        if index is None or index + 1 >= len(ordered_bands):
            return None, None
        nxt = ordered_bands[index + 1]["sprint_id"]
        return nxt, nxt in sprints

    carry_rows = []
    for row in rows:
        if not row["carried_over"]:
            continue
        destination, reappears = _destination(
            row["missed_sprints"][-1] if row["missed_sprints"] else None,
            row["sprints"],
        )
        carry_rows.append({
            "kind": "story",
            "object_id": row["object_id"],
            "key": row["key"],
            "tickets": [row["key"]],
            "sprints": row["sprints"],
            "missed_sprints": row["missed_sprints"],
            "first_sprint": row["first_sprint"],
            "last_sprint": row["last_sprint"],
            "points": row["points"],
            "added": row["added"],
            "done_at": row["done_at"],
            "status": "late" if row["done_at"] else "never",
            "wait_hours": row["slip_hours"],
            # prd/42 M5: where the story went next (or that it went nowhere).
            "destination_sprint": destination,
            "reappears": reappears,
            "affected": True,
        })
    late = sum(1 for row in carry_rows if row["status"] == "late")
    never_reappeared = sum(
        1 for row in carry_rows if row["reappears"] is False
    )
    no_destination = sum(
        1 for row in carry_rows if row["reappears"] is None
    )
    delivery = sprint_delivery(court, data, window)

    available = bool(data.sprints) or any(
        membership["sprints"] for membership in memberships.values()
    )
    if not data.sprints and not available:
        note = (
            "no SPRINT object and no 'sprint_added' event in this store: the "
            "sprint lens needs the Jira ingest (prd/04)"
        )
    elif not data.sprints:
        note = (
            "the store holds sprint memberships but no SPRINT objects: "
            "membership is shown without bands"
        )
    elif not planned:
        note = (
            "no story created in this window was committed to a sprint: the "
            "bands are the store's sprints, the scope is the window's cohort"
        )
    else:
        note = None
    return {
        "available": available,
        "sprints": bands,
        "count": len(bands),
        # prd/41 B8: scope and carry-over count the stories created in this
        # window; the bands are the store's sprints. The window travels so a
        # reader can reconcile this view with a window-filtered Rules number.
        "window": {
            "start": iso(window[0]),
            "end": iso(window[1]),
            "days": round((window[1] - window[0]).total_seconds() / 86400.0, 1),
        },
        # "active" is the store's own state, not merely a missing completion:
        # a closed sprint whose completion timestamp was lost still has a band
        # that must stop at the snapshot, but it is not the current sprint.
        "active": next(
            (
                row["sprint_id"] for row in bands
                if row["state"] == "active" or (row["open"] and not row["state"])
            ),
            None,
        ),
        "scope": {
            "stories": len(in_window),
            "in_sprint": len(planned),
            "out_of_sprint": len(unplanned),
            "in_share": _share(len(planned), len(in_window)),
            "out_share": _share(len(unplanned), len(in_window)),
            # the two reasons a story is out of scope, never folded together
            "never_in_sprint": len(never_added),
            "removed_to_backlog": len(removed),
            # the drill-down list: every story not currently in a sprint
            "unplanned": sorted(story.key for story in unplanned),
        },
        # prd/37 §1: the per-sprint done-by-end / late / never breakdown, the
        # same numbers the on-time rule quotes.
        "delivery": delivery,
        "carry_over": {
            "count": len(carried),
            # the same denominator as metrics.carry_over: stories ever committed
            "share": _share(len(carried), len(ever)),
            "stories": sorted(story.key for story in carried),
            "late": late,
            "never": len(carry_rows) - late,
            # prd/42 M5: the destination half of carry-over.
            "reappeared": sum(1 for row in carry_rows if row["reappears"] is True),
            "never_reappeared": never_reappeared,
            "no_next_sprint": no_destination,
            "sprints_involved": len({
                sid for row in carry_rows for sid in row["missed_sprints"]
            }),
            "rows": carry_rows,
            "target_share": CARRY_OVER_SHARE_TARGET,
        },
        "rows": rows,
        "membership_basis": "all stories in the store",
        "heuristic": (
            "a story is in a sprint when a 'sprint_added' names it; the current "
            "sprint is the last add not removed afterwards; done-by-end is "
            "measured at the sprint's close (concepts.md §6); carried over = "
            "committed to a closed sprint but not done by its close; a band "
            "spans a SPRINT object's start to its completion, or to the "
            "snapshot while active"
        ),
        "note": note,
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
            "kind": "story",
            "object_id": story.object_id,
            "key": story.key,
            "tickets": [story.key],
            "events": len(after),
            "started": iso(started),
            "last_pointed": iso(last["_ts"]),
            "points": _attrs(last).get("points", story.points),
            "object_points": story.points,
            "affected": True,
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
            pointed_stories > 0,
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
    rows: list[dict] = []
    # prd/46 §5: does the logged effort track the story's size at all?
    points_logged: list[tuple[float, float]] = []
    points_active: list[tuple[float, float]] = []
    for story in stories:
        entries = story.of("worklogged")
        if entries:
            stories_with_worklogs += 1
        story_hours = 0.0
        story_active = 0.0
        story_delays: list[float] = []
        story_late = 0
        for event in entries:
            attributes = _attrs(event)
            seconds = attributes.get("time_spent_seconds") or 0
            story_hours += float(seconds) / 3600.0
            logged_hours += float(seconds) / 3600.0
            worklogs += 1
            bucket = attributes.get("log_delay_bucket")
            if bucket in buckets:
                buckets[bucket] += 1
                # prd/36 §5: "a week late" is the ``late`` bucket (> 7 days);
                # ``within_week`` is 2–7 days and must not be counted as it.
                if bucket == "late":
                    story_late += 1
            delay = attributes.get("log_delay_seconds")
            if delay is not None:
                delays_days.append(float(delay) / 86400.0)
                story_delays.append(float(delay) / 86400.0)
        if entries:
            # prd/26 P5: the stories behind "logging is N days late", so the
            # rule opens into the worklogs that make the number.
            rows.append({
                "kind": "story",
                "object_id": story.object_id,
                "key": story.key,
                "tickets": [story.key],
                "state": story.state,
                "worklogs": len(entries),
                "logged_hours": round(story_hours, 2),
                "late_worklogs": story_late,
                "mean_delay_days": (
                    round(sum(story_delays) / len(story_delays), 2)
                    if story_delays else None
                ),
                "affected": story_late > 0,
            })
        if story.prs:
            stories_with_prs += 1
            for state in story.prs:
                for interval in state.intervals:
                    if interval.phase in ACTIVE_PHASES:
                        active_hours += interval.hours
                        story_active += interval.hours
        if entries and story.points is not None:
            points_logged.append((float(story.points), story_hours))
            points_active.append((float(story.points), story_active))

    points_logged_r = _pearson(
        [points for points, _ in points_logged],
        [hours for _, hours in points_logged],
    )
    points_active_r = _pearson(
        [points for points, _ in points_active],
        [hours for _, hours in points_active],
    )

    cycle_hours = 0.0
    for story in stories:
        cycle = _cycle(story)
        begin = cycle.boundaries["created"]
        end = cycle.boundaries["done"] or court.snapshot
        if begin is not None and end is not None and end > begin:
            cycle_hours += court.business_hours(cycle.person, begin, end)

    late = buckets["late"]
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
        # prd/46 §5: the honesty signal — logged hours do not track the story's
        # size (r ≈ −0.05 on the QA store), so a worklog is a note, not effort.
        "points_logged_correlation": points_logged_r,
        "points_logged_n": len(points_logged),
        "points_active_correlation": points_active_r,
        "points_active_n": len(points_active),
        # median logged hours by point size: "flat across 1–13 points" is the
        # plain-language form of the correlation.
        "logged_by_points": {
            str(points): _median([hours for size, hours in points_logged if size == points])
            for points in sorted({size for size, _ in points_logged})
        },
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
        "rows": sorted(
            rows,
            key=lambda row: (not row["affected"], -row["late_worklogs"],
                             -(row["mean_delay_days"] or 0.0), row["key"]),
        ),
        "heuristic": (
            "delay = worklog.created − worklog.started, bucketed by the ingest "
            "(same_day / next_day / within_week / late); 'logged a week late' is "
            "the late bucket (> 7 days) only, never within_week (2–7 d); "
            "active_hours is PR intervals in an active or rebasing phase, so a "
            "story with no PR adds nothing to the denominator; "
            "points_logged_correlation is Pearson r between a story's points and "
            "its logged hours — near zero means the logs do not measure effort"
        ),
        "note": _note(data, "no 'worklogged' event in this window", worklogs > 0),
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

    prd/29 adds the stage boundaries to the same check: a story that was *in
    review* in Jira with no PR naming its key is reported as
    ``in_review_without_pr`` — the ticket claims a review the store cannot see.
    """
    stories = cohort(data, window)
    started_before: list[str] = []
    started_unrefined: list[str] = []
    pr_before_start: list[str] = []
    in_review_without_pr: list[str] = []
    rows: list[dict] = []
    for story in stories:
        cycle = _cycle(story)
        started = cycle.boundaries["started"]
        in_review = cycle.boundaries["in_review"]
        if in_review is not None and not story.prs:
            in_review_without_pr.append(story.key)
            rows.append({
                "kind": "story",
                "object_id": story.object_id,
                "key": story.key,
                "tickets": [story.key],
                "state": story.state,
                "points": story.points,
                "started": iso(started) if started else None,
                "in_review": iso(in_review),
                "violations": ["in_review_without_pr"],
                "affected": True,
            })
        if started is None:
            continue
        refined = cycle.boundaries["refined"]
        violations: list[str] = []
        if refined is None:
            started_unrefined.append(story.key)
        elif started < refined:
            started_before.append(story.key)
            violations.append("started_before_refined")
        pr_open = cycle.boundaries["pr_open"]
        if pr_open is not None and pr_open < started:
            pr_before_start.append(story.key)
        if violations:
            # prd/26 P5: the story behind each violation, so the rule can be
            # opened into the tickets that broke the order.
            rows.append({
                "kind": "story",
                "object_id": story.object_id,
                "key": story.key,
                "tickets": [story.key],
                "state": story.state,
                "points": story.points,
                "started": iso(started),
                "refined": iso(refined) if refined else None,
                "violations": violations,
                "affected": True,
            })
    carry = carry_metric if carry_metric is not None else carry_over(court, data, window)
    churn = churn_metric if churn_metric is not None else point_churn(court, data, window)
    started = sum(1 for story in stories if _first_status(story, "in_progress"))
    violations = (
        len(started_before) + churn["churned"] + carry["carried_over"]
        + len(in_review_without_pr)
    )
    # the churn and carry-over rows already carry the story key; merge them in
    # so one drill-down answers "which tickets" for every Jira conformance rule
    for row in churn["rows"]:
        rows.append({
            "kind": "story", "key": row["key"], "tickets": [row["key"]],
            "object_id": f"STORY:{row['key']}", "points": row.get("points"),
            "started": row.get("started"), "refined": row.get("last_pointed"),
            "violations": ["re_pointed_after_start"], "affected": True,
        })
    for row in carry["rows"]:
        rows.append({
            "kind": "story", "key": row["key"], "tickets": [row["key"]],
            "object_id": f"STORY:{row['key']}", "points": row.get("points"),
            "sprints": row.get("sprints"),
            "violations": ["carried_over"], "affected": True,
        })
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
        "in_review_without_pr": len(in_review_without_pr),
        "violations": violations,
        "examples": sorted(started_before)[:5],
        "unrefined_examples": sorted(started_unrefined)[:5],
        "in_review_examples": sorted(in_review_without_pr)[:5],
        "rows": rows,
        "heuristic": (
            "refined = first 'pointed' (fallback: first status → ready); "
            "started = first status_changed to in_progress; a story with no "
            "pointed event is reported as started_unrefined, not as a "
            "violation, because the store cannot tell refinement from a missing "
            "estimate; in_review_without_pr = a status → in_review with no PR "
            "naming the key (the workflow map must name a review status for "
            "this boundary to exist)"
        ),
        "note": _note(data, "no story in this window", bool(stories)),
    }


# --------------------------------------------------------------------------- #
# refinement cost (the impact basis for churn / conformance)
# --------------------------------------------------------------------------- #
def refinement_cost(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
) -> dict:
    """What one refinement costs, in hours: the median created → ready segment.

    A re-point or an unrefined start means the estimate was not settled when it
    should have been, so the price of the rule is one more refinement. Measured
    from the cohort's own created → ready segments; falls back to a stated
    default when the store has none.
    """
    spans: list[float] = []
    for story in cohort(data, window):
        cycle = _cycle(story)
        begin = cycle.boundaries["created"]
        ready = cycle.boundaries["ready"]
        if begin is None or ready is None or ready < begin:
            continue
        spans.append(round(court.business_hours(cycle.person, begin, ready), 2))
    measured = _median(spans)
    return {
        "hours": measured if measured is not None else DEFAULT_REFINEMENT_HOURS,
        "n": len(spans),
        "basis": (
            "median created → ready segment of this cohort"
            if measured is not None
            else "default: the cohort has no measured created → ready segment"
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
        # prd/37: done-by-end / late / never per closed sprint, and the on-time
        # share the sprint lens and the on-time rule both quote.
        "sprint_delivery": sprint_delivery(court, data, window),
        "point_churn": churn,
        "worklog_cross_check": worklog_cross_check(court, data, window),
        "jira_conformance": conformance(
            court, data, window, carry_metric=carry, churn_metric=churn
        ),
        "refinement_cost": refinement_cost(court, data, window),
    }

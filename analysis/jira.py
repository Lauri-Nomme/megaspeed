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
missing (an unconfigured store keeps the same segments and today's numbers).

The segments are built from three sources, joined on the story key:

* the story's own events — ``created``, ``status_changed`` (both ends are status
  *stages*, never a workflow name), ``pointed``, ``assigned``,
  ``sprint_added`` / ``sprint_removed``, ``worklogged``;
* its Sprint objects (start / planned end / completion);
* its PRs, via ``PR.ticket_keys`` — the same key on both sides of the store, so
  the join is the store's own identity resolution and not a heuristic.

Everything else in this module follows from those rows:

======================  ==========================================================
``waiting_time_decomposition``  the segments, hours + per-segment flow efficiency
``carry_over``          stories added to more than one sprint
``point_churn``         ``pointed`` events after the story started
``worklog_cross_check`` ``log_delay_bucket`` distribution + logged vs active hours
``jira_conformance``    started before refined, re-pointed after start, carry-over
``rework``              story-level reopened / verification / bounce / orphan signals
``sprints``             the sprint lens (prd/26 P6): bands, scope, carry-over,
                        and the active sprint's at-risk forecast (prd/52)
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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from analysis import ballcourt
from analysis.effort import (
    ACTIVE_PHASES, TICKET_TOUCHES, TOUCH_ACTIVITIES, TOUCH_HOURS,
)
from ingest.store import iso, to_naive_utc
from ingest.taxonomy import normalize_stage

# --------------------------------------------------------------------------- #
# the closed segment vocabulary (prd/21)
# --------------------------------------------------------------------------- #
# prd/63 V-C6: "waiting for a reviewer to be added" is its own interval. A PR
# can sit for days before anyone is asked to review it — DEL-167 #53 waited
# three business days with no reviewer on the roster — and that wait used to be
# hidden inside "PR open → first review". The boundary is the first reviewer
# *add* event on any of the story's PRs; a store that asks reviewers at open
# supplies no add event, and the boundary falls back to `pr_open` (a measured
# zero, not a missing boundary).
SEGMENTS: tuple[tuple[str, str], ...] = (
    ("created_ready", "created → ready"),
    ("ready_sprint", "ready → sprint"),
    ("sprint_in_progress", "sprint → in progress"),
    ("in_progress_pr_open", "in progress → PR open"),
    ("pr_open_reviewer_added", "waiting for a reviewer"),
    ("pr_open_first_review", "reviewer added → first review"),
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
    "pr_open_reviewer_added": ("pr_open", "reviewer_added"),
    "pr_open_first_review": ("reviewer_added", "first_review"),
    "review_rounds": ("first_review", "approval"),
    "final_approval_merge": ("approval", "merged"),
    "merge_done": ("merged", "done"),
    "merge_verification": ("merged", "in_verification"),
    "verification_done": ("in_verification", "done"),
}


def segment_bounds_for(
    cycle: "Cycle", key: str,
) -> tuple[datetime | None, datetime | None, datetime | None, datetime | None]:
    """The ordered and the raw boundaries of one segment (prd/46 §1).

    ``(t1, t2, raw1, raw2)``: the ordered pair prices the segment, the raw pair
    names an inversion. One reader, so the ticket rows and the cohort benchmark
    measure the same span.
    """
    left, right = SEGMENT_BOUNDS[key]
    raw1 = cycle.boundaries.get(left)
    raw2 = cycle.boundaries.get(right)
    return (
        (cycle.segment_bounds.get(left) or raw1),
        (cycle.segment_bounds.get(right) or raw2),
        raw1,
        raw2,
    )


def segment_hours(
    court: ballcourt.BallCourt, cycle: "Cycle", key: str,
) -> float | None:
    """Business hours of one segment; ``None`` when a boundary is missing.

    The single segment-hours derivation: the ticket bar, its cohort median and
    the decomposition all call this, so "review rounds" cannot mean one span on
    the page and another in the benchmark. An out-of-order pair (raw right
    before raw left) is an empty segment — the quirk is named separately.
    """
    t1, t2, raw1, raw2 = segment_bounds_for(cycle, key)
    if t1 is None or t2 is None:
        return None
    if raw1 is not None and raw2 is not None and raw2 < raw1:
        return 0.0
    return round(court.business_hours(cycle.person, t1, t2), 2)


def segment_keys(boundaries: dict, *, verification: bool = False) -> tuple[str, ...]:
    """The segment order for one story (or one store).

    The base segments, except that the last one is replaced by
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
    "Jira ingest. The PR-side metrics are unaffected."
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


def human_hours(hours: float | None) -> str:
    """A duration in the same rendering every view uses ("3 d 2 h").

    concepts.md §1: ``d = 8 business hours``; the hours part is the remainder
    after whole working days, never a rounded decimal. This is the Python twin
    of ``app/format.js`` ``fmtHours`` so a rule title and the metric block it
    quotes cannot show "3.4 d" and "3 d 2 h" for the same number.
    """
    if hours is None:
        return "—"
    minutes = round(float(hours) * 60)
    if minutes < 60:
        return f"{minutes} min"
    whole, rest_minutes = divmod(minutes, 60)
    if whole < int(BUSINESS_HOURS_PER_DAY):
        return f"{whole} h" + (f" {rest_minutes} min" if rest_minutes else "")
    days, rest = divmod(whole, int(BUSINESS_HOURS_PER_DAY))
    return f"{days} d {rest} h"


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
    #: prd/62: the Jira *issue type* (story/bug/task …), when the store carries
    #: it. Never ``summary_class`` — that classifies the summary text and is a
    #: different fact (using it for the bug share would be a category error).
    issue_type: str | None = None
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
                # prd/70 F-16: the number parsed at scrape time (`Sprint 352` ->
                # 352), absent for a store scraped before the attribute existed.
                "sprint_no": attributes.get("sprint_no"),
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
                issue_type=(
                    attributes.get("issue_type") or attributes.get("issuetype")
                ),
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


def scope_to_repo(data: JiraData, repo: str) -> JiraData:
    """The ticket layer of one repo cohort (prd/49).

    A story is attributable to a repo only through a PR that **names it**, so a
    repo slice keeps the stories joined to a PR in ``repo`` and drops the rest —
    a story with no PR anywhere is not part of any repo's delivery cohort. Each
    surviving story keeps only that repo's PR states, so its worklog-vs-active
    and PR-open gaps are measured on the scoped cohort, not on every repo's PRs.

    Sprints and epics survive unchanged: they are planning windows and parent
    links, not repo-specific objects. Every cloned story recomputes its cycle
    (the memo is per story, and its PR list changed).
    """
    wanted = str(repo or "")
    stories: list[Story] = []
    for story in data.stories:
        prs = [state for state in story.prs if state.pr.get("repo") == wanted]
        if not prs:
            continue
        stories.append(replace(story, prs=prs, _cycle_cache=None))
    return replace(
        data,
        stories=stories,
        events=sum(len(story.events) for story in stories),
    )


def story_cycle_cohort(
    court: ballcourt.BallCourt, data: JiraData | None,
) -> list[dict]:
    """Completed stories' ``created → done`` hours, with their points (prd/57).

    This is the cohort a ticket's own cycle is read against (concepts.md §9
    "Cohort benchmark" / prd/42 M2) and the one the benchmark contrast quotes:
    an open story is censored, so it is not in the cohort. The cycle is priced
    on the assignee's calendar — the same basis ``tickets._segment_rows`` and
    ``jira.waiting_time_decomposition`` use.
    """
    if data is None:
        return []
    rows: list[dict] = []
    for story in data.stories:
        cycle = _cycle(story)
        begin = cycle.boundaries.get("created")
        done = cycle.boundaries.get("done")
        if begin is None or done is None or done <= begin:
            continue
        rows.append({
            "key": story.key,
            "hours": round(court.business_hours(story.actor, begin, done), 2),
            "points": story.points,
        })
    return rows


def segment_cohort(
    court: ballcourt.BallCourt, data: JiraData | None,
) -> dict[str, list[float]]:
    """Per-segment hours over completed stories — each segment's own cohort.

    One sample per completed story per segment, measured by
    :func:`segment_hours` (the same ordered boundaries the ticket bar draws), so
    a segment's median and the ticket's own hours are the same quantity.
    """
    if data is None:
        return {}
    verification = uses_verification(data)
    out: dict[str, list[float]] = defaultdict(list)
    for story in data.stories:
        cycle = _cycle(story)
        begin = cycle.boundaries.get("created")
        done = cycle.boundaries.get("done")
        if begin is None or done is None or done <= begin:
            continue
        for key in segment_keys(cycle.boundaries, verification=verification):
            hours = segment_hours(court, cycle, key)
            if hours is not None:
                out[key].append(hours)
    return dict(out)


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
    today's segments.
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
        "reviewer_added", "first_review", "approval", "merged",
        "in_verification", "done",
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
    # prd/63 V-C6: when the first reviewer was *asked*. The roster add is its
    # own event; a store that adds reviewers at open has none, so the boundary
    # is the open instant (the wait is a measured zero). A late add of an
    # extra reviewer after the first review is not the first ask: clamp it so
    # the two waits cannot invert (the ordering pass would otherwise move the
    # whole span into the review segment).
    adds = sorted(
        event["_ts"] for state in prs for event in state.events
        if event["activity"] == "reviewer_changed"
        and _attrs(event).get("change") == "added"
    )
    reviewer_added = adds[0] if adds else pr_open
    if pr_open is not None and reviewer_added is not None and reviewer_added < pr_open:
        reviewer_added = pr_open
    if first_review is not None and reviewer_added is not None and reviewer_added > first_review:
        reviewer_added = first_review
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
        "reviewer_added": reviewer_added,
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
    """The segments: hours, share of cycle, and flow efficiency each.

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


def story_done_at(story: Story) -> datetime | None:
    """Public name for when a story first reached done (prd/61 cumulative flow)."""
    return _done_at(story)


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


def _store_carry_context(delivery: dict | None) -> tuple[int | None, int | None]:
    """Carried-over stories across **every** closed sprint, and how many that is.

    prd/67 W-05: the rule's own number is the window's cohort, while the sprint
    lens counts the whole store — a window-scoped 7 read as a store total. Both
    scopes travel together, and the store half is read from
    :func:`sprint_delivery`'s per-closed-sprint rows (``done_late`` +
    ``never_done``), never from a second walk of the store. A store with no
    closed sprint says ``None``, not 0.
    """
    rows = (delivery or {}).get("sprints") or []
    if not rows:
        return None, None
    carried = sum(
        (row.get("done_late") or 0) + (row.get("never_done") or 0)
        for row in rows
    )
    return carried, len(rows)


def _sprints_closed_in(
    window: tuple[datetime, datetime],
    boundaries: dict[int, dict],
    memberships: dict[str, dict],
) -> int:
    """Closed sprints with committed stories whose close falls in the window.

    prd/67 W-05: the title names "the 1 sprint closed in this window" so the
    count cannot be read as the store's own sprint total. The universe is the
    same one :func:`sprint_delivery` measures — a closed sprint with at least one
    committed story — so the window count and ``store_sprints`` never disagree
    about what a sprint is. A closed sprint with no recorded end closes at the
    snapshot, which is the window's own end.
    """
    committed = {
        sid for membership in memberships.values() for sid in membership["sprints"]
    }
    start, end = window
    return sum(
        1 for sid, boundary in boundaries.items()
        if sid in committed and boundary["closed"] and boundary["end"] is not None
        and start <= boundary["end"] <= end
    )


def carry_over(
    court: ballcourt.BallCourt,
    data: JiraData,
    window: tuple[datetime, datetime],
    delivery: dict | None = None,
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

    prd/67 W-05: the count is the **window's cohort**, so the payload states the
    whole store beside it — ``store_carried_over`` / ``store_sprints`` from the
    delivery rows, and ``window_sprints`` for the sprints that closed inside the
    window. No number of the metric itself changes.
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
    # prd/67 W-05: both scopes, side by side. The window half is this cohort;
    # the store half is the delivery rows' own per-closed-sprint accounting.
    store_carried, store_sprints = _store_carry_context(
        delivery if delivery is not None else sprint_delivery(court, data, window)
    )
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
        # prd/67 W-05: the window's own sprint count, so "1 sprint in this
        # window" is visible without opening the drill-down, and the store's
        # whole scope sits beside it. Both are counts of *closed* sprints.
        "window_sprints": _sprints_closed_in(window, boundaries, memberships),
        "store_carried_over": store_carried,
        "store_sprints": store_sprints,
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
# 1b. story-level rework (prd/51)
# --------------------------------------------------------------------------- #
#: The backward status flips that are rework, as ``(from_stage, to_stage)``.
#: ``reopened`` is the PRD's exact definition (done → ready); the other two are
#: the workflow's own stage bounces, read from the same status history the
#: segments use.
REWORK_FLIPS: dict[str, tuple[str, str]] = {
    "reopened": ("done", "ready"),
    "failed_verification": ("in_verification", "in_progress"),
    "review_bounces": ("in_review", "in_progress"),
}
#: Where a bounce is over: the story reached the stage it fell out of again
#: (or finished). A review bounce may recover straight into verification, so
#: that stage ends the loop too.
REWORK_RECOVERY: dict[str, frozenset[str]] = {
    "failed_verification": frozenset({"in_verification", "done"}),
    "review_bounces": frozenset({"in_review", "in_verification", "done"}),
}


def _status_events(story: Story) -> list[dict]:
    """The story's ``status_changed`` events, in time order."""
    return [event for event in story.events if event["activity"] == "status_changed"]


def _rework_loops(
    court: ballcourt.BallCourt,
    story: Story,
    from_stage: str,
    to_stage: str,
    recovered: frozenset[str],
) -> list[dict]:
    """Every backward flip and the loop it opened.

    ``hours`` is the business time between the flip and the next status that
    returns to the stage the story fell out of (or ``done``, or the snapshot):
    the fix plus the re-review, never the whole remaining cycle. A story with
    no further event keeps its loop open to the snapshot, so a forgotten
    bounce is a quantity rather than a bare count.
    """
    events = _status_events(story)
    end = _cycle(story).boundaries.get("done") or story.snapshot
    loops: list[dict] = []
    for index, event in enumerate(events):
        attributes = _attrs(event)
        if normalize_stage(attributes.get("from_status")) != from_stage:
            continue
        if normalize_stage(attributes.get("to_status")) != to_stage:
            continue
        recovered_at = None
        for later in events[index + 1:]:
            if normalize_stage(_attrs(later).get("to_status")) in recovered:
                recovered_at = later["_ts"]
                break
        if recovered_at is None:
            recovered_at = end
        hours = (
            round(court.business_hours(story.actor, event["_ts"], recovered_at), 2)
            if recovered_at is not None and recovered_at > event["_ts"]
            else 0.0
        )
        loops.append({
            "ts": event["_ts"],
            "recovered": recovered_at,
            "hours": hours,
        })
    return loops


def _rework_group(label: str, note: str, rows: list[dict]) -> dict:
    """One signal group: its rows, the flip/PR count and the total hours."""
    rows.sort(key=lambda row: (
        -float(row.get("hours") or 0.0), row["key"], row.get("number") or 0
    ))
    return {
        "label": label,
        "note": note,
        "count": sum(int(row.get("count") or 0) for row in rows),
        "stories": len({row["key"] for row in rows}),
        "hours": round(sum(float(row.get("hours") or 0.0) for row in rows), 2),
        "rows": rows,
        "affected": bool(rows),
    }


def rework(court: ballcourt.BallCourt, data: JiraData | None) -> dict:
    """Story-level rework: reopened, failed verification, bounces, orphans.

    The PR-side ``rework_loops`` rule measures rounds *inside* a PR. This is
    the story-level aggregate the QA run had to assemble from raw events:

    * **reopened** — a ``done → ready`` flip (``count`` is the flips, ``hours``
      the business time from the first done to the final one, or the snapshot
      while it is open again);
    * **failed verification** — ``in_verification → in_progress``; ``hours`` is
      the loop back to verification (or done);
    * **review bounces** — ``in_review → in_progress``; the loop back to review
      (verification ends it too, since the story left review);
    * **approved but never merged** — every reviewer on a PR approved and the
      PR never merged (DEL-161: both approved, the bot declined it 34 days
      later). ``hours`` is final approval → the close (or the snapshot).

    The cohort is **every story in the store**, not the Rules window: the
    signals are a property of a story's whole life, and a 90-day window would
    silently drop the oldest reopen (DEL-105) from the 5 the QA measured. The
    payload says so in ``scope``.
    """
    data = data or EMPTY
    reopened: list[dict] = []
    verification: list[dict] = []
    bounces: list[dict] = []
    unmerged: list[dict] = []

    for story in data.stories:
        cycle = _cycle(story)
        flips = [
            event for event in _status_events(story)
            if normalize_stage(_attrs(event).get("from_status")) == "done"
            and normalize_stage(_attrs(event).get("to_status")) == "ready"
        ]
        if flips:
            first_done = cycle.boundaries.get("first_done") or flips[0]["_ts"]
            final_done = cycle.boundaries.get("done")
            end = final_done or story.snapshot
            hours = (
                round(court.business_hours(story.actor, first_done, end), 2)
                if first_done is not None and end is not None and end > first_done
                else 0.0
            )
            reopened.append({
                "kind": "story",
                "object_id": story.object_id,
                "key": story.key,
                "tickets": [story.key],
                "points": story.points,
                "count": len(flips),
                "hours": hours,
                "first_done": iso(first_done) if first_done else None,
                "final_done": iso(final_done) if final_done else None,
                "open": final_done is None,
                "events": [
                    {"ts": iso(event["_ts"]), "from": "done", "to": "ready"}
                    for event in flips
                ],
                "affected": True,
            })

        for group, rows in (
            ("failed_verification", verification),
            ("review_bounces", bounces),
        ):
            from_stage, to_stage = REWORK_FLIPS[group]
            loops = _rework_loops(
                court, story, from_stage, to_stage, REWORK_RECOVERY[group]
            )
            if not loops:
                continue
            rows.append({
                "kind": "story",
                "object_id": story.object_id,
                "key": story.key,
                "tickets": [story.key],
                "points": story.points,
                "count": len(loops),
                "hours": round(sum(loop["hours"] for loop in loops), 2),
                "events": [
                    {
                        "ts": iso(loop["ts"]),
                        "from": from_stage,
                        "to": to_stage,
                        "recovered": iso(loop["recovered"]) if loop["recovered"] else None,
                        "hours": loop["hours"],
                    }
                    for loop in loops
                ],
                "affected": True,
            })

        for state in story.prs:
            pr = state.pr
            if pr.get("state") == "merged":
                continue
            latest = state.intervals[-1] if state.intervals else None
            if latest is None or not latest.roster:
                continue
            approved = set(latest.approved or ())
            if not approved or approved != set(latest.roster):
                continue
            approvals = [
                event["_ts"] for event in state.events
                if event["activity"] == "pr_approved"
            ]
            if not approvals:
                continue
            approved_at = max(approvals)
            closed_at = pr.get("closed_ts")
            end = closed_at or court.snapshot
            hours = (
                round(court.business_hours(pr["actor"], approved_at, end), 2)
                if end is not None and end > approved_at else 0.0
            )
            closed_by = next(
                (
                    event["actor"] for event in reversed(state.events)
                    if event["activity"] in ("pr_declined", "pr_closed")
                ),
                None,
            )
            unmerged.append({
                "kind": "pr",
                "object_id": pr["object_id"],
                "key": story.key,
                "tickets": [story.key],
                "id": f"{pr['repo'].split('/')[-1]}#{pr['pr_id']}",
                "number": pr["pr_id"],
                "repo": pr["repo"],
                "repo_short": pr["repo"].split("/")[-1],
                "state": pr.get("state"),
                "author": pr.get("actor"),
                "points": story.points,
                "count": 1,
                "hours": hours,
                "approved_by": sorted(approved),
                "approved_at": iso(approved_at),
                "closed_at": iso(closed_at) if closed_at else None,
                "closed_by": closed_by,
                "bot_closed": bool(closed_by and closed_by in (court.bots or set())),
                "waiting_on": [pr["actor"]] if pr.get("actor") else [],
                "affected": True,
            })

    groups = {
        "reopened": _rework_group(
            "reopened after done",
            "a done → ready flip: the story came back after it was closed",
            reopened,
        ),
        "failed_verification": _rework_group(
            "failed verification",
            "in verification → in progress: QA sent the story back",
            verification,
        ),
        "review_bounces": _rework_group(
            "review bounces",
            "in review → in progress: review sent the story back",
            bounces,
        ),
        "approved_unmerged": _rework_group(
            "approved but never merged",
            "every reviewer approved and the PR never merged",
            unmerged,
        ),
    }
    total = sum(group["count"] for group in groups.values())
    hours = round(sum(group["hours"] for group in groups.values()), 2)
    return {
        "available": bool(data.stories),
        "scope": (
            "every story in the store unless a repo scope narrows it (not the "
            "Rules window)"
        ),
        "stories": len(data.stories),
        **groups,
        "count": total,
        "hours": hours,
        "basis": (
            "reopened = done → ready flips, priced first done → final done (or "
            "the snapshot); a verification/review bounce is priced from the "
            "backward flip to the next return to that stage (or done); an "
            "approved-but-unmerged PR is priced final approval → its close (or "
            "the snapshot)"
        ),
        "note": _note(
            data,
            "no STORY objects in this store: there is no status history to "
            "derive rework from",
            bool(data.stories),
        ),
    }


# --------------------------------------------------------------------------- #
# 2. first-pass yield and stability (prd/62)
# --------------------------------------------------------------------------- #
#: Below this many deliveries a rate is not shown, counts only — the same floor
#: ``benchmark.BENCHMARK_MIN_N`` uses for a cohort benchmark.
MIN_DELIVERIES = 10

#: prd/62 §"Step signals": the four pipeline steps a completed story passes,
#: in lifecycle order. ``id`` is the closed vocabulary the UI and the API share.
YIELD_STEPS: tuple[tuple[str, str, str], ...] = (
    ("refine", "Refined", "created → ready"),
    ("develop", "Developed", "ready → first review"),
    ("review", "Reviewed", "first review → approval"),
    ("verify", "Verified", "merge → verification → done"),
)
#: The exact transitions whose *absence* makes a step clean. The table is the
#: one place the definition lives; the UI prints these as the step's signals.
YIELD_SIGNALS: dict[str, tuple[str, ...]] = {
    "refine": ("ready → backlog", "pointed after started"),
    "develop": ("in_review → in_progress",),
    # a story with two PRs must pass review twice; clean only if every merged
    # PR had zero rounds (concepts.md §6 Review round / prd/40).
    "review": ("zero review rounds across every merged PR",),
    "verify": ("in_verification → in_progress", "done → ready"),
}
ZERO_LOOPBACK_NOTE = (
    "no loop-back event in this store — check the workflow emits ready, "
    "in_review and in_verification transitions"
)


def _is_completed(story: Story) -> bool:
    """A delivery: the story has a measured ``created → done`` cycle."""
    cycle = _cycle(story)
    begin = cycle.boundaries.get("created")
    done = cycle.boundaries.get("done")
    return begin is not None and done is not None and done > begin


def _status_pairs(story: Story) -> set[tuple[str, str]]:
    return {
        (
            normalize_stage(_attrs(event).get("from_status")),
            normalize_stage(_attrs(event).get("to_status")),
        )
        for event in _status_events(story)
    }


def _merged_prs(story: Story) -> list:
    return [state for state in story.prs if state.pr.get("state") == "merged"]


def _yield_round_count(state) -> int:
    pushes, unapprovals = ballcourt.review_round_events(state)
    return len(pushes) + len(unapprovals)


def _story_step_clean(story: Story) -> dict[str, bool]:
    """Was this story clean at each step? (the exact transitions, one place)

    ``review`` is clean only if the story has a merged PR **and** every merged
    PR had zero review rounds: a story with two PRs must pass review twice.
    """
    pairs = _status_pairs(story)
    started = _first_status(story, "in_progress")
    pointed_after_start = any(
        started is not None and event["_ts"] > started
        for event in story.of("pointed")
    )
    merged = _merged_prs(story)
    return {
        "refine": (
            ("ready", "backlog") not in pairs and not pointed_after_start
        ),
        "develop": ("in_review", "in_progress") not in pairs,
        "review": bool(merged) and all(
            _yield_round_count(state) == 0 for state in merged
        ),
        "verify": (
            ("in_verification", "in_progress") not in pairs
            and ("done", "ready") not in pairs
        ),
    }


def _step_available(step: str, cohort: list[Story]) -> bool:
    """Does the store carry the transitions that could falsify this step?

    A step whose falsifying transitions do not exist in the store reports
    ``unfalsifiable`` and ``rate: null`` rather than a perfect score (prd/62).
    """
    if not cohort:
        return False
    if step == "refine":
        # a refinement is observable when the store emits a ready stage or a
        # point-change event: without either, neither signal can be falsified
        return any(
            any(to == "ready" for _, to in _status_pairs(story))
            or bool(story.of("pointed"))
            for story in cohort
        )
    if step == "develop":
        # no story ever reached in_review: the workflow has no review stage in
        # the store, so "review → in progress" cannot be observed
        return any(
            ("in_review", "in_progress") in _status_pairs(story)
            or any(to == "in_review" for _, to in _status_pairs(story))
            for story in cohort
        )
    if step == "review":
        return any(_merged_prs(story) for story in cohort)
    if step == "verify":
        # a done transition is always observable on a completed cohort; the QA
        # half is only falsifiable when the workflow has the stage
        return any(
            ("done", "ready") in _status_pairs(story)
            or any(to == "done" for _, to in _status_pairs(story))
            for story in cohort
        )
    return False


def first_pass_yield(
    court: ballcourt.BallCourt,
    data: JiraData | None,
    window: tuple[datetime, datetime] | None = None,
    implied: dict | None = None,
) -> dict:
    """%C&A per step and VSM's **rolled %C&A** over completed stories (prd/62).

    The cohort is the deliveries created inside the window (a completed story,
    ``created → done``). Each step's %C&A is the share of that cohort which left
    the step without a loop-back *into* it — the transitions in
    :data:`YIELD_SIGNALS`. The rolled figure is the product of the four step
    rates, and the **joint clean count** (stories clean at every step) is
    reported next to it because a story can loop at more than one step, so the
    product and the joint count can differ slightly.

    ``review`` has its own denominator: only stories with a merged PR can pass
    review; a Jira-only story is excluded and the payload says how many. Below
    :data:`MIN_DELIVERIES` the counts are reported without a percentage, and a
    step whose falsifying transitions are not in the store is ``unfalsifiable``
    (``rate: null``) rather than a perfect score.

    ``implied`` is the per-step yield the policy lab's fitted model implies (the
    server supplies it). It is a *threshold for a warning dot*, never a
    measurement: a step below it is marked, never recoloured.
    """
    data = data or EMPTY
    start, end = window or (None, None)
    cohort = [
        story for story in data.stories
        if _is_completed(story)
        and (start is None or (story.start is not None and start <= story.start <= end))
    ]
    n = len(cohort)
    no_pr = sum(1 for story in cohort if not _merged_prs(story))
    clean = {story.key: _story_step_clean(story) for story in cohort}
    steps: list[dict] = []
    rates: dict[str, float | None] = {}
    for step, label, step_window in YIELD_STEPS:
        available = _step_available(step, cohort)
        if step == "review":
            members = [story for story in cohort if _merged_prs(story)]
            step_n = len(members)
        else:
            members = cohort
            step_n = n
        bounced = [
            story.key for story in members if not clean[story.key][step]
        ]
        clean_n = step_n - len(bounced)
        hidden = step_n < MIN_DELIVERIES
        rate = (
            round(clean_n / step_n, 3)
            if available and not hidden and step_n
            else None
        )
        rates[step] = rate
        entry = {
            "id": step,
            "label": label,
            "window": step_window,
            "clean_n": clean_n if available else None,
            "n": step_n,
            "rate": rate,
            "available": available,
            "unfalsifiable": not available,
            "rate_hidden": hidden and available,
            "signals": list(YIELD_SIGNALS[step]),
            "items": bounced,
        }
        if not available:
            entry["note"] = (
                f"no {step} signal in this store: the transitions that would "
                f"make it unclean ({', '.join(YIELD_SIGNALS[step])}) do not occur"
            )
        elif hidden:
            entry["note"] = (
                f"{step_n} deliveries — too few for a rate "
                f"(minimum {MIN_DELIVERIES})"
            )
        elif available and not bounced and step_n:
            entry["note"] = ZERO_LOOPBACK_NOTE
        steps.append(entry)
    rolled = (
        round(_product(rates[step] for step, _, _ in YIELD_STEPS), 3)
        if n >= MIN_DELIVERIES and all(rates[step] is not None for step, _, _ in YIELD_STEPS)
        else None
    )
    # prd/68 F-07: a step that is not measured (no merged PR → review
    # unfalsifiable) cannot be part of a joint clean count. The old code summed
    # `all(clean.values())`, so every story read as unclean at review and the
    # panel printed "0 / 84" for a cohort that simply has no PR layer.
    unavailable = [
        (entry["id"], entry["label"]) for entry in steps if not entry["available"]
    ]
    clean_joint = (
        None if unavailable
        else sum(1 for story in cohort if all(clean[story.key].values()))
    )
    rolled_reason = None
    if rolled is None:
        if any(step_id == "review" for step_id, _ in unavailable):
            rolled_reason = (
                "Rolled %C&A is unavailable because Reviewed is not measured "
                "in this store (no merged PRs)"
            )
        elif unavailable:
            labels = ", ".join(label for _, label in unavailable)
            rolled_reason = (
                f"Rolled %C&A is unavailable because {labels} "
                f"{'is' if len(unavailable) == 1 else 'are'} not measured in "
                "this store"
            )
        elif n < MIN_DELIVERIES:
            rolled_reason = (
                f"{n} deliveries — too few for a rate "
                f"(minimum {MIN_DELIVERIES})"
            )
    payload = {
        "available": bool(cohort),
        "cohort": "completed stories",
        "n": n,
        "min_deliveries": MIN_DELIVERIES,
        "steps": steps,
        "rolled": rolled,
        "rolled_reason": rolled_reason,
        "clean_joint": clean_joint,
        "joint_rate": (
            round(clean_joint / n, 3)
            if clean_joint is not None and n else None
        ),
        "no_pr": no_pr,
        "basis": (
            "%C&A = the share of items that left this step complete and "
            "accurate — no loop-back into it. Rolled %C&A is the product of the "
            "four steps; the joint count is the measured clean first pass and "
            "can differ slightly because a story can loop at more than one step."
        ),
        "note": _note(
            data,
            "no STORY objects in this store: there is no pipeline to measure "
            "first-pass yield over",
            bool(cohort),
        ),
    }
    if rolled is not None:
        payload["note"] = (
            f"only {clean_joint} of {n} stories went through without a loop-back"
        )
    payload["implied"] = implied or {
        "steps": {},
        "basis": (
            "the policy lab did not run for this request, so no step is marked "
            "below an implied yield"
        ),
    }
    return payload


def _product(values) -> float:
    """The product of the available rates (an empty product is 1)."""
    out = 1.0
    for value in values:
        if value is None:
            return 0.0
        out *= value
    return out


#: The stability signals a story's status history can produce (prd/51's rework
#: flips, read as a rate over deliveries) plus the rework proxy.
STABILITY_FLIPS: tuple[tuple[str, str, str, str], ...] = (
    ("reopen", "reopened after done", "done", "ready"),
    ("failed_verification", "failed verification", "in_verification", "in_progress"),
    ("review_bounce", "review bounced", "in_review", "in_progress"),
)


def _flip_count(story: Story, from_stage: str, to_stage: str) -> int:
    return sum(
        1 for event in _status_events(story)
        if normalize_stage(_attrs(event).get("from_status")) == from_stage
        and normalize_stage(_attrs(event).get("to_status")) == to_stage
    )


def stability(
    court: ballcourt.BallCourt,
    data: JiraData | None,
    window: tuple[datetime, datetime] | None = None,
) -> dict:
    """DORA's instability factor from tracker data, labelled (prd/62).

    Three **direct** loop-back rates over the same deliveries the yield panel
    uses (a flip count ÷ deliveries), plus a labelled **proxy** for rework and
    an explicit "unavailable" for the bug share. None of these is DORA's
    change-failure or rework rate: the store has no deploy or incident source,
    and ``bug_share`` needs the Jira issue type, which the store does not carry
    — ``summary_class`` classifies the summary text, never the issue type, so
    it is never substituted.

    ``zero_approval_merges`` is the measured baseline the policy lab's guardrail
    quotes (the model's mandatory gate makes it 0 by construction).
    """
    data = data or EMPTY
    start, end = window or (None, None)
    cohort = [
        story for story in data.stories
        if _is_completed(story)
        and (start is None or (story.start is not None and start <= story.start <= end))
    ]
    n = len(cohort)
    hidden = n < MIN_DELIVERIES
    out: dict = {"available": bool(cohort), "n": n, "min_deliveries": MIN_DELIVERIES}
    counts: dict[str, int] = {}
    for field, label, from_stage, to_stage in STABILITY_FLIPS:
        count = sum(_flip_count(story, from_stage, to_stage) for story in cohort)
        counts[field] = count
        entry = {
            "label": label,
            "count": count,
            "rate": round(count / n, 3) if n and not hidden else None,
            "signals": [f"{from_stage} → {to_stage}"],
        }
        # each signal's stories, as a drill-down (prd/62's step drill-down)
        entry["items"] = sorted(
            story.key for story in cohort
            if _flip_count(story, from_stage, to_stage)
        )
        if hidden:
            entry["note"] = (
                f"{n} deliveries — too few for a rate (minimum {MIN_DELIVERIES})"
            )
        out[field] = entry
    typed = [story for story in cohort if story.issue_type]
    if not typed:
        out["bug_share"] = {
            "available": False,
            "count": None,
            "rate": None,
            "reason": "the store's issue objects carry no issue type",
        }
    elif len(typed) < n:
        out["bug_share"] = {
            "available": False,
            "count": None,
            "rate": None,
            "reason": (
                f"only {len(typed)} of {n} stories carry an issue type in this "
                "store"
            ),
        }
    else:
        bugs = sum(
            1 for story in cohort
            if str(story.issue_type or "").strip().lower() in ("bug", "defect")
        )
        out["bug_share"] = {
            "available": True,
            "label": "bug share",
            "count": bugs,
            "n": n,
            "rate": round(bugs / n, 3) if n and not hidden else None,
            "basis": "bug-type stories ÷ deliveries",
            "proxy": True,
            "note": (
                "a labelled proxy for a rework rate: a bug-type story is not "
                "proof that the bug came from a failed change"
            ),
        }
    proxy = counts["reopen"] + counts["failed_verification"]
    out["rework_proxy"] = {
        "label": "rework proxy",
        "count": proxy,
        "rate": round(proxy / n, 3) if n and not hidden else None,
        "basis": "reopened or failed verification / deliveries",
        "proxy": True,
        "note": (
            "not DORA's rework rate: this store has no deploy or incident data"
        ),
    }
    merged_in_window = [
        state for state in court.prs
        if (start is None or start <= state.pr["created_ts"] <= end)
        and state.pr["state"] == "merged"
    ]
    no_approval = [
        state for state in merged_in_window
        if not any(event["activity"] == "pr_approved" for event in state.events)
    ]
    out["zero_approval_merges"] = {
        "label": "merged with no approval",
        "count": len(no_approval),
        "n": len(merged_in_window),
        "rate": (
            round(len(no_approval) / len(merged_in_window), 3)
            if merged_in_window else None
        ),
        "items": sorted(state.pr["object_id"] for state in no_approval),
    }
    out["basis"] = (
        "each direct rate is a loop-back count ÷ deliveries in the window; the "
        "bug share and the rework proxy are labelled proxies, never DORA's "
        "change-failure or rework rate (no deploy/incident source)"
    )
    out["note"] = _note(
        data,
        "no STORY objects in this store: there is no status history to derive "
        "loop-back rates from",
        bool(cohort),
    )
    return out


def story_yield(court: ballcourt.BallCourt, story: Story | None) -> dict:
    """One story's four-step pass, for the ticket drawer (prd/62).

    A story with no merged PR has no review pass to measure: that step is
    ``available: false`` and the ticket says so, rather than reading "bounced"
    for a step the store cannot falsify.
    """
    if story is None:
        return {"steps": [], "rolled_clean": False, "available": False}
    clean = _story_step_clean(story)
    merged = _merged_prs(story)
    steps = []
    for step, label, _ in YIELD_STEPS:
        available = step != "review" or bool(merged)
        entry = {
            "id": step,
            "label": label,
            "clean": bool(clean[step]),
            "available": available,
            "signals": list(YIELD_SIGNALS[step]),
        }
        if not available:
            entry["note"] = (
                "no merged PR in this store, so there is no review pass to measure"
            )
        steps.append(entry)
    return {
        "steps": steps,
        "rolled_clean": all(clean.values()) and all(
            step["available"] for step in steps
        ),
        "available": all(step["available"] for step in steps),
    }


# --------------------------------------------------------------------------- #
# 2a. sprint on-time delivery (prd/37 §1, §6)
# --------------------------------------------------------------------------- #
def _added_at_start(story: Story, sid: int, sprint_index: dict) -> bool:
    """Was this story in sprint ``sid`` when the sprint started? (prd/46 §4)

    ``at_start`` is the store's own membership-at-start marker (prd/42 M5); a
    ``sprint_added`` on the sprint's **start day** is the same fact from the
    timestamp. Anything later was *adopted* mid-sprint, not committed.

    prd/63 C13: the comparison is by day, not by exact timestamp. A planning
    session that adds a story at 09:30 to a sprint starting 09:00 was still
    part of the plan; the old strict compare counted S2 as "2 committed, 28
    adopted" when five were in the room at the start.
    """
    start = (sprint_index.get(sid) or {}).get("start")
    for event in story.of("sprint_added"):
        if event_sprint_id(event) != sid:
            continue
        if _attrs(event).get("at_start"):
            return True
        return start is not None and event["_ts"].date() <= start.date()
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
        end = boundary["end"]
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
                # prd/64 F-06b: a story still open at the sprint's close missed
                # it exactly like a late one, so it is affected too. The status
                # stays `never` so the drill-down can say *why* it missed
                # ("still open") instead of folding both misses together.
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
                    "affected": True,
                })
        committed = sum(counts.values())
        delivered = counts["by_end"]
        share = _share(delivered, committed)
        # prd/63 V-A9/V-C3: the lens above answers "did the sprint's members
        # finish?" It does not answer "how much did the team ship in these
        # dates?" — carry-overs that were never re-committed belong to no
        # sprint's membership. Count every story whose done boundary falls in
        # the sprint's window, and split the ones this sprint did not plan.
        window_done = window_carry_in = window_points = window_carry_points = 0
        start = sprint.get("start")
        for story in data.stories:
            outcome = outcomes.get(story.key) or {}
            done_at = outcome.get("done_at")
            if done_at is None or end is None or not (start < done_at <= end):
                continue
            story_points = story.points or 0
            window_done += 1
            window_points += story_points
            if sid not in memberships[story.key]["sprints"]:
                window_carry_in += 1
                window_carry_points += story_points
        sprint_row = {
            "sprint_id": sid,
            # prd/70 F-16: the drill-down's `S352`, from the same attribute.
            "sprint_no": sprint.get("sprint_no"),
            "object_id": sprint.get("object_id"),
            "state": sprint.get("state"),
            "start": iso(sprint["start"]) if sprint.get("start") else None,
            "end": iso(boundary["end"]) if boundary["end"] else None,
            "planned_end": sprint.get("planned_end"),
            # prd/55 §5: the sprint-length flag travels with the delivery row,
            # so its "N of M by end" cannot be read beside a normal sprint's.
            "planned_days": None,
            "length_outlier": False,
            "length_note": None,
            "stories": committed,
            "points": sum(points.values()),
            "done_by_end": delivered,
            # prd/63 V-A9/V-C3: team velocity per sprint *window* — every story
            # that reached done inside the dates, including carry-overs this
            # sprint never planned. `window_carry_in` is the credited part.
            "window_done": window_done,
            "window_points": window_points,
            "window_carry_in": window_carry_in,
            "window_carry_in_points": window_carry_points,
            "window_own": window_done - window_carry_in,
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
    _flag_sprint_lengths(sprints)
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
def _sprint_datetime(value) -> datetime | None:
    """Parse a sprint timestamp (ISO string with ``Z`` or a date-only plan)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    try:
        if len(text) == 10:
            parsed = datetime.fromisoformat(text)
            return parsed.replace(hour=16)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return None


def _flag_sprint_lengths(rows: list[dict]) -> None:
    """Mark a sprint whose planned length is an outlier (prd/55 §5).

    The comparison is the *plan* (start → planned_end), not the measured band:
    an active sprint's band stops at the snapshot, so its measured days would
    understate a long plan. A row with no plan keeps ``planned_days: None`` and
    is simply not compared.
    """
    for row in rows:
        start = _sprint_datetime(row.get("start"))
        planned = _sprint_datetime(row.get("planned_end"))
        if start is not None and planned is not None and planned > start:
            row["planned_days"] = round(
                (planned - start).total_seconds() / 86400.0, 1
            )
        else:
            row["planned_days"] = None
    lengths = [
        row["planned_days"] for row in rows
        if row.get("planned_days") is not None
    ]
    for row in rows:
        days = row.get("planned_days")
        others = sorted(
            value for value in lengths if value is not None and value != days
        ) if len(lengths) > 1 else []
        # compare against the median of the *other* sprints, so one long sprint
        # cannot drag the baseline it is measured against
        if len(others) >= 2:
            middle = len(others) // 2
            median = (
                others[middle] if len(others) % 2
                else round((others[middle - 1] + others[middle]) / 2.0, 1)
            )
            if median > 0 and days is not None and days >= 1.5 * median and days - median >= 5:
                row["length_outlier"] = True
                row["length_note"] = (
                    f"planned {days:g} calendar days vs "
                    f"{others[0]:g}–{others[-1]:g} for the other sprints — "
                    "its delivery share is not comparable"
                )
                continue
        row["length_outlier"] = False
        row["length_note"] = None


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


# --------------------------------------------------------------------------- #
# 2c. sprint-at-risk (prd/52)
# --------------------------------------------------------------------------- #
#: The risk classes, in presentation order: a Jira-done story with unmerged code
#: is the strongest lie, a person-held PR is next, an orphaned approval after
#: that, and "no live PR at all" is the remaining gap.
RISK_REASONS: tuple[tuple[str, str], ...] = (
    ("done-but-unmerged", "Done in Jira, PR unmerged"),
    ("stuck-on", "stuck on a person"),
    ("approved-unmerged", "approved but never merged"),
    ("no-live-pr", "no live PR"),
)
RISK_ORDER = {reason: index for index, (reason, _) in enumerate(RISK_REASONS)}
RISK_LABEL = dict(RISK_REASONS)


def _latest_interval(state):
    """The last measured interval — the status vector that holds the ball now."""
    return state.intervals[-1] if state.intervals else None


def _pr_holders(court: ballcourt.BallCourt, state) -> list[str]:
    """Who owes this PR now: pending reviewers plus the author while in debt.

    The same holder set the delivery list reports (concepts.md §2): the
    status vector is the truth, bots are excluded, and a PR nobody holds is
    empty rather than guessed.
    """
    latest = _latest_interval(state)
    if latest is None:
        return []
    holders = set(latest.pending_all or ())
    author = state.pr.get("actor")
    if latest.author_open and author and author not in (court.bots or set()):
        holders.add(author)
    return sorted(holders)


def _pr_wait(court: ballcourt.BallCourt, state) -> tuple[float | None, datetime | None]:
    """The current wait on an open PR: its last handover → the snapshot.

    Priced on the holder's own calendar when one person holds it, exactly as
    the delivery row does; a closed PR has no current wait.
    """
    latest = _latest_interval(state)
    pr = state.pr
    if latest is None or pr.get("closed_ts") is not None:
        return None, None
    end = court.snapshot
    start = latest.start
    if start is None or end is None or end <= start:
        return None, None
    return round(court.business_hours(latest.owner, start, end), 2), start


def _approved_wait(
    court: ballcourt.BallCourt, state,
) -> tuple[float | None, datetime | None]:
    """Final approval → the PR's close (or the snapshot)."""
    approvals = [
        event["_ts"] for event in state.events
        if event["activity"] == "pr_approved"
    ]
    if not approvals:
        return None, None
    start = max(approvals)
    end = state.pr.get("closed_ts") or court.snapshot
    if end is None or end <= start:
        return None, start
    return round(court.business_hours(state.pr.get("actor"), start, end), 2), start


def _risk_row(court: ballcourt.BallCourt, story: Story) -> dict:
    """Classify one not-delivered story of the sprint (prd/52).

    The four classes reuse facts the product already has — the cycle's final
    ``done`` (Jira says done), the PR status vector (who owes, fully approved),
    and the same "Done in Jira, PR open" predicate the delivery list uses:

    * ``done-but-unmerged`` — Jira done while a named PR is open;
    * ``stuck-on`` — a live PR whose ball is with one or more people;
    * ``approved-unmerged`` — every reviewer approved and the PR never merged
      (DEL-161: the bot declined it after both approvals);
    * ``no-live-pr`` — no open PR and no orphaned approval: the work has no
      code in flight at all.
    """
    cycle = _cycle(story)
    done_at = cycle.boundaries.get("done")
    live = [
        state for state in story.prs
        if state.pr.get("closed_ts") is None
    ]
    approved_unmerged = []
    for state in story.prs:
        if state.pr.get("state") == "merged":
            continue
        latest = _latest_interval(state)
        if latest is None or not latest.roster:
            continue
        approved = set(latest.approved or ())
        if approved and approved == set(latest.roster):
            approved_unmerged.append(state)

    progress = []
    for state in story.prs:
        latest = _latest_interval(state)
        wait, since = _pr_wait(court, state)
        progress.append({
            "object_id": state.pr["object_id"],
            "number": state.pr["pr_id"],
            "repo": state.pr["repo"],
            "repo_short": state.pr["repo"].split("/")[-1],
            "state": state.pr.get("state"),
            "open": state.pr.get("closed_ts") is None,
            "author": state.pr.get("actor"),
            "opened": (
                iso(state.pr.get("created_ts"))
                if state.pr.get("created_ts") is not None else None
            ),
            "who": _pr_holders(court, state) if state.pr.get("closed_ts") is None else [],
            "waiting_hours": wait,
            "waiting_since": iso(since) if since else None,
            "approved": sorted(latest.approved or ()) if latest else [],
            "roster": sorted(latest.roster or ()) if latest else [],
            "last_event": iso(state.events[-1]["_ts"]) if state.events else None,
        })

    reason: str
    who: list[str]
    wait: float | None = None
    since: datetime | None = None
    detail: dict = {}

    if done_at is not None and live:
        reason = "done-but-unmerged"
        open_rows = [row for row in progress if row["open"]]
        holder_set = sorted({person for row in open_rows for person in row["who"]})
        waits = [row["waiting_hours"] for row in open_rows if row["waiting_hours"] is not None]
        wait = max(waits) if waits else None
        who = holder_set
        detail = {
            "prs": [row["number"] for row in open_rows],
            "done_at": iso(done_at),
            # prd/42 M4 / QA: a PR opened after Jira closed the ticket cannot
            # have been part of the delivered story (DEL-183).
            "opened_after_close": [
                row["number"] for row in open_rows
                if row.get("opened") and row["opened"] > iso(done_at)
            ],
        }
    elif approved_unmerged:
        state = max(
            approved_unmerged,
            key=lambda item: (_approved_wait(court, item)[0] or 0.0),
        )
        reason = "approved-unmerged"
        wait, since = _approved_wait(court, state)
        who = [state.pr["actor"]] if state.pr.get("actor") else []
        closed_by = next(
            (
                event["actor"] for event in reversed(state.events)
                if event["activity"] in ("pr_declined", "pr_closed")
            ),
            None,
        )
        latest = _latest_interval(state)
        detail = {
            "pr": state.pr["pr_id"],
            "repo": state.pr["repo"],
            "state": state.pr.get("state"),
            "approved_by": sorted(latest.approved or ()) if latest else [],
            "closed_at": iso(state.pr.get("closed_ts")) if state.pr.get("closed_ts") else None,
            "closed_by": closed_by,
            "bot_closed": bool(closed_by and closed_by in (court.bots or set())),
        }
    elif live:
        reason = "stuck-on"
        open_rows = [row for row in progress if row["open"]]
        holder_set = sorted({person for row in open_rows for person in row["who"]})
        waits = [row["waiting_hours"] for row in open_rows if row["waiting_hours"] is not None]
        wait = max(waits) if waits else None
        who = holder_set
        detail = {"prs": [row["number"] for row in open_rows]}
    else:
        reason = "no-live-pr"
        who = [story.actor] if story.actor else []
        detail = {
            "prs": [row["number"] for row in progress],
            "merged_prs": [
                row["number"] for row in progress if row["state"] == "merged"
            ],
        }

    return {
        "kind": "story",
        "object_id": story.object_id,
        "key": story.key,
        "tickets": [story.key],
        "points": story.points,
        "reason": reason,
        "label": RISK_LABEL[reason],
        "who": who,
        "waiting_hours": wait,
        "waiting_since": iso(since) if since else None,
        "detail": detail,
        "prs": progress,
        "affected": True,
    }


def _at_risk(court: ballcourt.BallCourt, sid: int, band: dict,
             by_key: dict[str, Story], outcomes: dict) -> dict:
    """The active sprint's forecast: each not-delivered story, why, who owes."""
    rows: list[dict] = []
    points = 0
    for key in band["stories"]:
        story = by_key.get(key)
        if story is None:
            continue
        outcome = outcomes[key]
        verdict = outcome["outcomes"].get(sid)
        if verdict is None:
            continue
        # what the band counts as delivered is exactly what is not at risk
        if verdict == "by_end" and outcome.get("delivered"):
            continue
        risk = _risk_row(court, story)
        risk["verdict"] = verdict
        rows.append(risk)
        points += story.points or 0
    rows.sort(key=lambda row: (
        RISK_ORDER[row["reason"]],
        -(row.get("points") or 0),
        -(row.get("waiting_hours") or 0.0),
        row["key"],
    ))
    return {
        "available": True,
        "sprint_id": sid,
        "sprint_no": band.get("sprint_no"),
        "planned_end": band.get("planned_end"),
        "band_end": band.get("band_end"),
        "stories": band.get("story_count") or 0,
        "done_by_end": band.get("done_by_end") or 0,
        "points": band.get("points") or 0,
        "count": len(rows),
        "points_at_risk": points,
        "share": _share(len(rows), band.get("story_count") or 0),
        "points_share": _share(points, band.get("points") or 0),
        "rows": rows,
        "basis": (
            "the active sprint's not-delivered stories (Jira done with an open "
            "PR, a live PR held by people, an approved-but-unmerged PR, or no "
            "live PR), with the current wait priced to the snapshot"
        ),
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
        start = sprint.get("start") if sprint else None
        end = sprint.get("end") if sprint else None
        state = sprint.get("state") if sprint else None
        # prd/68 F-02: a future sprint has not run. Its band is its plan
        # (start → planned end), never start → snapshot (which would invert the
        # range and paint a Q4 placeholder over the current sprint). A dateless
        # sprint has no band at all; its membership is still real.
        future = state == "future" or (
            start is not None and start > court.snapshot
        )
        band_start = start
        if start is None:
            band_end = None
        elif end is not None:
            band_end = end
        elif future:
            planned = _sprint_datetime(sprint.get("planned_end")) if sprint else None
            band_end = planned if planned is not None and planned > start else None
        else:
            # an active sprint (or a closed one whose completion was lost)
            # stops at the snapshot; the state, not the band, says which
            band_end = court.snapshot
        boundary = boundaries.get(sid) or {}
        return {
            "sprint_id": sid,
            # prd/70 F-16: `S352` needs the number, not the id; `None` when the
            # store was scraped before the name was parsed.
            "sprint_no": sprint.get("sprint_no") if sprint else None,
            "object_id": sprint.get("object_id") if sprint else None,
            "state": state,
            "start": iso(start) if start else None,
            "end": iso(end) if end else None,
            "planned_end": sprint.get("planned_end") if sprint else None,
            "goal_class": sprint.get("goal_class") if sprint else None,
            "open": bool(sprint and end is None and state != "future"),
            # prd/37: the close "done by end" is measured at
            "closed": bool(boundary.get("closed")),
            # prd/68 F-02: the UI must be able to say "dates unknown" and
            # "unknown" without guessing from an absent field.
            "date_known": start is not None and end is not None,
            "dates_known": band_start is not None and band_end is not None,
            "state_known": state is not None,
            "band_start": iso(band_start) if band_start else None,
            # an active sprint's band ends at the data, not at the plan
            "band_end": iso(band_end) if band_end else None,
            "days": (
                round((band_end - band_start).total_seconds() / 86400.0, 1)
                if band_start and band_end and band_end > band_start else None
            ),
            # prd/55 §5: a sprint whose planned length is an outlier makes
            # "25 of 30" incomparable with its neighbours. The plan is
            # start → planned_end; the band may stop at the snapshot.
            "planned_days": None,
            "length_outlier": False,
            "length_note": None,
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
        # prd/68 F-02: a future placeholder with no stories is not a band. It
        # stays in the list (its id is real) but must not paint the canvas.
        if row.get("state") == "future" and not row["story_count"]:
            row["band_start"] = None
            row["band_end"] = None
            row["dates_known"] = False
            row["days"] = None
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
        # prd/63 V-A9/V-C3: the sprint *window's* velocity, beside the
        # membership verdict — every story that reached done in the dates,
        # including carry-overs this sprint never planned.
        window_start = _sprint_datetime(row.get("start"))
        window_end = _sprint_datetime(row.get("band_end") or row.get("end"))
        window_done = window_carry = window_points = window_carry_points = 0
        if window_start is not None and window_end is not None:
            for story in data.stories:
                done_at = (outcomes.get(story.key) or {}).get("done_at")
                if done_at is None or not (window_start < done_at <= window_end):
                    continue
                story_points = story.points or 0
                window_done += 1
                window_points += story_points
                if sid not in memberships[story.key]["sprints"]:
                    window_carry += 1
                    window_carry_points += story_points
        row["window_done"] = window_done
        row["window_points"] = window_points
        row["window_carry_in"] = window_carry
        row["window_carry_in_points"] = window_carry_points
        row["window_own"] = window_done - window_carry
        bands.append(row)

    # prd/55 §5: flag a sprint whose length is an outlier against its siblings.
    # S3 plans 51 calendar days while S1/S2 planned 24–26; without the flag its
    # "25 of 30 by end" reads as comparable delivery. One definition, carried by
    # both the sprint lens and the on-time rule.
    _flag_sprint_lengths(bands)

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
            "sprint lens needs the Jira ingest"
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
    # "active" is the store's own state, not merely a missing completion: a
    # closed sprint whose completion timestamp was lost still has a band that
    # must stop at the snapshot, but it is not the current sprint.
    active_sid = next(
        (
            row["sprint_id"] for row in bands
            if row["state"] == "active"
            or (
                row["open"] and not row["state"]
                and _sprint_datetime(row.get("start")) is not None
                and _sprint_datetime(row.get("start")) <= court.snapshot
            )
        ),
        None,
    )
    # prd/52: the forecast belongs to the active sprint only — a closed sprint
    # has a verdict, not a risk. The rows are the not-delivered stories the
    # band already counts, classified with the same PR facts as the delivery
    # list (done-in-Jira, holders, approvals).
    at_risk = (
        _at_risk(court, active_sid, sprint_rows[active_sid], by_key, outcomes)
        if active_sid is not None and active_sid in sprint_rows else
        {
            "available": False,
            "sprint_id": None,
            "sprint_no": None,
            "rows": [],
            "note": (
                "no active sprint in this store: at-risk is a forecast for the "
                "sprint in flight, while a closed sprint has a measured verdict"
            ),
            "basis": (
                "the active sprint's not-delivered stories; a closed sprint has "
                "no at-risk list"
            ),
        }
    )
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
        "active": active_sid,
        "at_risk": at_risk,
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
            "measured at the sprint's close; carried over = "
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
def _size_key(points) -> str:
    """A story-point size as a plain label: ``1``/``13``, never ``1.0``.

    prd/63 V-A4: the points column comes back from DuckDB as a float, so
    ``str(points)`` serialised ``"1.0"`` while every reader looks up ``"1"``.
    A size is a name (the story was pointed at 1), not a measured quantity, so
    an integral value is written without a decimal tail. A genuinely
    fractional size (a half-point story) keeps its decimal.
    """
    try:
        value = float(points)
    except (TypeError, ValueError):
        return str(points)
    if value.is_integer():
        return str(int(value))
    return str(points)


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
    # prd/53 §3: the same metric per week, so "is logging getting more
    # trustworthy?" has an answer and not only "is it trustworthy now".
    timeline: list[tuple[datetime | None, float, float]] = []
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
            timeline.append((story.start, float(story.points), story_hours))

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
    # prd/53 §3: the per-week trend of the same gap. A bucket is the week the
    # story started (Monday 00:00 UTC); its correlation is the same Pearson r
    # over the stories that started in it (``None`` with fewer than three), and
    # ``median_hours_per_point`` is the per-story logged ÷ points median. Dense
    # buckets between the first and last story, so the sparkline has a real time
    # axis; the most recent week is marked.
    per_point_all = [hours / points for points, hours in points_logged if points > 0]
    trend: list[dict] = []
    if timeline:
        def week_floor(stamp: datetime) -> datetime:
            day = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
            return day - timedelta(days=day.weekday())

        weeks = [week_floor(start) for start, _points, _hours in timeline if start]
        cursor, last_week = min(weeks), max(weeks)
        while cursor <= last_week:
            entries = [
                (points, hours) for start, points, hours in timeline
                if start is not None and week_floor(start) == cursor
            ]
            per_point = [hours / points for points, hours in entries if points > 0]
            trend.append({
                "week_start": iso(cursor),
                "week_end": iso(cursor + timedelta(days=7)),
                "label": cursor.strftime("%Y-%m-%d"),
                "stories": len(entries),
                "logged_hours": round(sum(hours for _points, hours in entries), 2),
                "correlation": (
                    _pearson(
                        [points for points, _hours in entries],
                        [hours for _points, hours in entries],
                    ) if len(entries) >= 2 else None
                ),
                "median_hours_per_point": _median(per_point),
                "median_logged_hours": _median([hours for _points, hours in entries]),
                "current": cursor == last_week,
            })
            cursor += timedelta(days=7)
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
        # prd/53 §1: the gap in plain numbers — median logged hours and median
        # logged hours per point. A flat h/pt across 1–13 point stories is the
        # "a 13-pt story logs 2–3 h" finding.
        "median_logged_hours": _median([hours for _points, hours in points_logged]),
        "median_hours_per_point": _median(per_point_all),
        # median logged hours by point size: "flat across 1–13 points" is the
        # plain-language form of the correlation.
        # prd/63 V-A4: the keys are the *story sizes in plain numbers* — "1",
        # "13", never "1.0". DuckDB returns the points column as a float, so
        # ``str(points)`` used to serialise "1.0"; the card looked up "1" and
        # every bucket read 0 min. A size is a label, not a measurement, so it
        # is normalised once here (the JS reader is defensive too).
        "logged_by_points": {
            _size_key(points): _median(
                [hours for size, hours in points_logged if size == points]
            )
            for points in sorted({size for size, _ in points_logged})
        },
        # prd/53 §3: the historical trend, per week, of the same gap.
        "trend": trend,
        "trend_note": (
            "weekly buckets by the week the story started (Monday 00:00 UTC, "
            "dense between the first and last story); correlation is the same "
            "points ↔ logged-hours Pearson r over that week's stories (None "
            "when the week has fewer than three — a two-point r is ±1 by "
            "construction), and median_hours_per_point is the per-story logged "
            "÷ points median; the last bucket is the most recent week"
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
    from the cohort's own created → ready segments. A store with no measured
    segment (no Jira layer, or no story with a refinement boundary) reports
    ``hours: None`` and ``n: 0`` — there is no default to invent. A scenario
    that needs this input is then unavailable, not modelled at a made-up cost.
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
        "hours": measured,
        "n": len(spans),
        "basis": (
            "median created → ready segment of this cohort"
            if measured is not None
            else "no measured created → ready segment in this cohort"
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
    # prd/67 W-05: the carry-over metric states the store's whole scope beside
    # its window cohort, and it reads that scope from these same delivery rows —
    # one pass, no second walk of the store.
    delivery = sprint_delivery(court, data, window)
    carry = carry_over(court, data, window, delivery=delivery)
    churn = point_churn(court, data, window)
    return {
        "jira": data.summary(),
        "waiting_time_decomposition": waiting_time_decomposition(court, data, window),
        "carry_over": carry,
        # prd/37: done-by-end / late / never per closed sprint, and the on-time
        # share the sprint lens and the on-time rule both quote.
        "sprint_delivery": delivery,
        "point_churn": churn,
        "worklog_cross_check": worklog_cross_check(court, data, window),
        "jira_conformance": conformance(
            court, data, window, carry_metric=carry, churn_metric=churn
        ),
        "refinement_cost": refinement_cost(court, data, window),
    }

"""Ticket-centric view (prd/25): one ticket's whole cycle, not one PR's.

The delivery list, the timeline and the rules all describe PRs. The question a
team actually asks — *why did DEL-167 take three weeks?* — is about the
**ticket**: its eight segments (backlog, refinement, sprint, work, review,
merge), the PRs that carry it, the sprints it was committed to, and the hours
logged against it. This module assembles exactly that one ticket from the two
halves of the store:

    STORY:<key>  ── events (created / status / sprint / pointed / worklog)
        │
        ├── PR.ticket_keys  ── the store's own join, no heuristic
        └── SPRINT:<id>     ── sprint membership objects

Everything here is a projection of :mod:`analysis.jira`: the boundaries and the
active-work accounting come from ``jira.cycle_for`` / ``jira.active_hours``, so
the per-ticket answer can never disagree with the cohort metric (prd/21 §1).
The one thing this module adds is the **prose answer**: a single sentence that
either names where the time went or names the boundary the store is missing —
never an estimate standing in for a measurement (prd/10 §7).

Read-only: it reads the already-loaded ``BallCourt`` and ``JiraData``, so the
API needs no extra query and the ticket page costs one rebuild of the same
interval timeline every other view uses.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from analysis import ballcourt
from analysis import jira
from analysis.effort import ACTIVE_PHASES
from ingest.store import iso

#: Why a boundary is absent, in the words the detail page shows. These are
#: explanations, not guesses: each names the event that would have produced the
#: boundary, so the reader knows what the store is missing.
BOUNDARY_REASON: dict[str, str] = {
    "created": "the story has no created event or object timestamp",
    "refined": "no 'pointed' event (the estimate refinement produces)",
    "sprint": "no 'sprint_added' event",
    "started": "no status change to in progress",
    "pr_open": "no PR names this key",
    "first_review": "no reviewer action on its PRs",
    "approval": "no approval on its PRs",
    "merged": "no merged PR names this key",
    "done": "no status change to done, and the story is still open",
}

#: The boundary order the payload reports (the nine timestamps of prd/25).
BOUNDARY_ORDER = (
    "created", "refined", "sprint", "started", "pr_open", "first_review",
    "approval", "merged", "done",
)

PR_REVIEW_ACTIONS = ("pr_pushed", "pr_unapproved")


def normalize_key(key: str) -> str:
    """Accept ``DEL-167``, ``STORY:DEL-167`` or ``TICKET:DEL-167``."""
    text = str(key or "").strip()
    for prefix in ("STORY:", "TICKET:"):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _iso(value: datetime | None) -> str | None:
    return iso(value) if value is not None else None


# --------------------------------------------------------------------------- #
# PRs joined on the story key
# --------------------------------------------------------------------------- #
def _pr_states(court: ballcourt.BallCourt | None, key: str) -> list:
    """Every PR that names ``key`` — the same join ``jira.load`` uses."""
    if court is None:
        return []
    return [
        state for state in court.prs
        if key in (state.pr.get("ticket_keys") or ())
    ]


def _pr_rows(court: ballcourt.BallCourt, states: list) -> list[dict]:
    rows: list[dict] = []
    for state in states:
        pr = state.pr
        first = jira.first_review(state)
        merged = jira.pr_merge(state)
        rounds = sum(
            1 for event in state.events
            if event["activity"] in PR_REVIEW_ACTIONS
            and first is not None and event["_ts"] >= first
        )
        pushes = [
            {
                "ts": _iso(event["_ts"]),
                "push_class": (event.get("attributes") or {}).get("push_class") or "other",
            }
            for event in state.events if event["activity"] == "pr_pushed"
        ]
        classes = Counter(push["push_class"] for push in pushes)
        intervals = [
            {
                "phase": interval.phase,
                "from": _iso(interval.start),
                "to": _iso(interval.end),
                "hours": round(interval.hours, 2),
                "ball": interval.ball,
                "waited_on": interval.waited_on,
                "measured": interval.measured,
                "kind": interval.kind,
                "evidence": interval.evidence,
                "commits": interval.commits,
                "open": interval.open,
            }
            for interval in state.intervals
        ]
        active = round(sum(
            interval["hours"] for interval in intervals
            if interval["measured"] and interval["phase"] in ACTIVE_PHASES
        ), 2)
        opened = pr["created_ts"]
        closed = pr["closed_ts"]
        end = closed or court.snapshot
        cycle = (
            round(court.business_hours(pr["actor"], opened, end), 2)
            if opened is not None and end is not None and end > opened else 0.0
        )
        rows.append({
            "object_id": pr["object_id"],
            "number": pr["pr_id"],
            "repo": pr["repo"],
            "state": pr["state"],
            "author": pr["actor"],
            "ticket_keys": list(pr.get("ticket_keys") or ()),
            "opened": _iso(opened),
            "closed": _iso(closed),
            "cycle_hours": cycle,
            "censored": closed is None,
            "censored_at": _iso(court.snapshot) if closed is None else None,
            "review_rounds": rounds,
            "first_review": _iso(first),
            "merged": _iso(merged),
            "active_hours": active,
            "pushes": pushes,
            "push_classes": dict(sorted(classes.items())),
            "intervals": intervals,
        })
    rows.sort(key=lambda row: (row["opened"] or "", row["object_id"]))
    return rows


# --------------------------------------------------------------------------- #
# the eight segments for one story
# --------------------------------------------------------------------------- #
def _segment_rows(court: ballcourt.BallCourt, cycle: jira.Cycle | None) -> list[dict]:
    rows: list[dict] = []
    for key in jira.SEGMENT_KEYS:
        left, right = jira.SEGMENT_BOUNDS[key]
        t1 = cycle.boundaries.get(left) if cycle is not None else None
        t2 = cycle.boundaries.get(right) if cycle is not None else None
        row: dict = {
            "key": key,
            "label": jira.SEGMENT_LABEL[key],
            "bounds": [left, right],
            "from": _iso(t1),
            "to": _iso(t2),
            "hours": None,
            "share": None,
            "active_hours": None,
            "efficiency": None,
            "available": False,
            "inverted": False,
            "clamped": False,
            "missing": [],
            "reason": None,
        }
        if t1 is None or t2 is None:
            row["missing"] = [
                name for name, value in ((left, t1), (right, t2)) if value is None
            ]
            row["reason"] = "; ".join(
                BOUNDARY_REASON[name] for name in row["missing"]
            )
            rows.append(row)
            continue
        row["available"] = True
        if t2 < t1:
            # out-of-order data: both boundaries exist, the segment is empty.
            row["inverted"] = True
            row["hours"] = 0.0
            row["active_hours"] = 0.0
            row["reason"] = (
                "the boundaries are out of order (the right boundary precedes the left)"
            )
            rows.append(row)
            continue
        elapsed = round(court.business_hours(cycle.person, t1, t2), 2)
        active = round(jira.active_hours(court, cycle, t1, t2), 2)
        if active > elapsed:
            active = elapsed
            row["clamped"] = True
        row["hours"] = elapsed
        row["active_hours"] = active
        row["efficiency"] = round(active / elapsed, 4) if elapsed > 0 else None
        rows.append(row)
    return rows


def _segments(court: ballcourt.BallCourt, cycle: jira.Cycle | None) -> dict:
    rows = _segment_rows(court, cycle)
    measured = [row for row in rows if row["available"]]
    total = round(sum(row["hours"] for row in measured), 2)
    active = round(sum(row["active_hours"] for row in measured), 2)
    for row in measured:
        row["share"] = round(row["hours"] / total, 4) if total else 0.0
    boundaries = {
        name: {
            "ts": _iso(cycle.boundaries.get(name)) if cycle is not None else None,
            "available": bool(cycle is not None and cycle.boundaries.get(name) is not None),
            "reason": (
                BOUNDARY_REASON[name]
                if cycle is None or cycle.boundaries.get(name) is None else None
            ),
        }
        for name in BOUNDARY_ORDER
    }
    begin = cycle.boundaries.get("created") if cycle is not None else None
    end = (cycle.boundaries.get("done") or court.snapshot) if cycle is not None else None
    cycle_hours = (
        round(court.business_hours(cycle.person, begin, end), 2)
        if cycle is not None and begin is not None and end is not None and end > begin
        else None
    )
    largest = max(measured, key=lambda row: (row["hours"], row["key"])) if measured else None
    missing = [name for name in BOUNDARY_ORDER
               if cycle is None or cycle.boundaries.get(name) is None]
    return {
        "order": list(jira.SEGMENT_KEYS),
        "rows": rows,
        "boundaries": boundaries,
        "measured": len(measured),
        "missing_segments": [row["key"] for row in rows if not row["available"]],
        "missing": missing,
        "total_hours": total,
        "active_hours": active,
        "efficiency": round(active / total, 4) if total > 0 else None,
        "cycle_hours": cycle_hours,
        "explained_share": round(total / cycle_hours, 4) if cycle_hours else None,
        "largest": largest["key"] if largest else None,
        "largest_label": largest["label"] if largest else None,
        "largest_hours": largest["hours"] if largest else None,
        "largest_share": largest["share"] if largest else None,
        "clamped": sum(1 for row in rows if row["clamped"]),
        "hours_mode": court.hours_mode,
        "heuristic": (
            "the same boundaries and active-work accounting as the prd/21 "
            "decomposition: refined = first 'pointed', sprint = first "
            "'sprint_added', status categories for started/done, and the "
            "story's PRs (joined on PR.ticket_keys) for open/review/merge"
        ),
    }


# --------------------------------------------------------------------------- #
# sprints, worklogs and events
# --------------------------------------------------------------------------- #
def _sprint_id(event: dict) -> int | None:
    attrs = jira.attributes(event)
    sid = attrs.get("sprint_id")
    if sid is not None:
        try:
            return int(sid)
        except (TypeError, ValueError):
            return None
    for linked in event.get("sprints") or ():
        found = jira.sprint_id(linked)
        if found is not None:
            return found
    return None


def _sprint_rows(data: jira.JiraData, story: jira.Story | None) -> dict:
    if story is None:
        return {"memberships": [], "carried_over": False, "sprints_involved": 0}
    by_id = {
        sprint["id"]: sprint for sprint in data.sprints.values()
        if sprint.get("id") is not None
    }
    rows: dict[int, dict] = {}
    order: list[int] = []
    for event in story.of("sprint_added"):
        sid = _sprint_id(event)
        if sid is None:
            continue
        if sid not in rows:
            meta = by_id.get(sid) or {}
            rows[sid] = {
                "sprint_id": sid,
                "object_id": meta.get("object_id"),
                "state": meta.get("state"),
                "start": _iso(meta.get("start")),
                "end": _iso(meta.get("end")),
                "planned_end": meta.get("planned_end"),
                "added": None,
                "removed": None,
                "at_start": False,
            }
            order.append(sid)
        if rows[sid]["added"] is None:
            rows[sid]["added"] = _iso(event["_ts"])
            rows[sid]["at_start"] = bool(jira.attributes(event).get("at_start"))
    for event in story.of("sprint_removed"):
        sid = _sprint_id(event)
        if sid in rows and rows[sid]["removed"] is None:
            rows[sid]["removed"] = _iso(event["_ts"])
    memberships = [rows[sid] for sid in order]
    return {
        "memberships": memberships,
        "carried_over": len(order) > 1,
        "sprints_involved": len(order),
    }


def _worklogs(story: jira.Story | None, pr_rows: list[dict]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    for event in story.of("worklogged") if story is not None else []:
        attrs = jira.attributes(event)
        seconds = int(attrs.get("time_spent_seconds") or 0)
        delay = int(attrs.get("log_delay_seconds") or 0)
        rows.append({
            "ts": _iso(event["_ts"]),
            "hours": round(seconds / 3600.0, 2),
            "seconds": seconds,
            "log_delay_hours": round(delay / 3600.0, 2),
            "bucket": attrs.get("log_delay_bucket"),
            "actor": event["actor"],
        })
    rows.sort(key=lambda row: row["ts"] or "")
    logged = round(sum(row["hours"] for row in rows), 2)
    active = round(sum(pr["active_hours"] for pr in pr_rows), 2)
    buckets = {name: 0 for name in jira.LOG_DELAY_BUCKETS}
    for row in rows:
        if row["bucket"] in buckets:
            buckets[row["bucket"]] += 1
    late = buckets["within_week"] + buckets["late"]
    return rows, {
        "worklogs": len(rows),
        "logged_hours": logged,
        "active_hours": active,
        "logged_to_active_ratio": round(logged / active, 2) if active > 0 else None,
        "buckets": buckets,
        "late_share": round(late / len(rows), 4) if rows else None,
        "note": (
            None if rows else
            "no 'worklogged' event names this ticket, so logged time cannot be "
            "compared with the PR-side active hours"
        ),
    }


def _events(story: jira.Story | None) -> list[dict]:
    if story is None:
        return []
    return [
        {
            "key": event["key"],
            "ts": _iso(event["_ts"]),
            "activity": event["activity"],
            "actor": event["actor"],
            "attributes": event.get("attributes") or {},
        }
        for event in story.events
    ]


# --------------------------------------------------------------------------- #
# the prose answer (prd/10 §7: honesty in a sentence)
# --------------------------------------------------------------------------- #
def _answer(
    key: str,
    story: jira.Story | None,
    segments: dict,
    prs: list[dict],
) -> tuple[str, str]:
    """One sentence: where the time went, or which boundary the store lacks."""
    measured = [row for row in segments["rows"] if row["available"]]
    missing = [row for row in segments["rows"] if not row["available"]]
    censored = story is not None and segments["boundaries"]["done"]["ts"] is None
    suffix = (
        "; the story is still open, so the cycle is right-censored at the snapshot"
        if censored else ""
    )
    if story is None:
        if prs:
            plural = "s" if len(prs) != 1 else ""
            return (
                f"the store has no STORY object for {key}: its {len(prs)} PR{plural} "
                "can be shown, but the ticket's own waiting cannot be decomposed.",
                "story_missing",
            )
        return f"the store has no story and no PR for {key}.", "story_missing"
    if not measured:
        first = missing[0]
        boundary = first["missing"][0]
        return (
            f"the store cannot say where {key}'s time went because no segment has "
            f"both boundaries — the {boundary} boundary is missing "
            f"({first['reason']}){suffix}.",
            "unmeasurable",
        )
    total = segments["total_hours"]
    biggest = max(measured, key=lambda row: (row["hours"], row["key"]))
    percent = round(100 * (biggest["share"] or 0))
    if missing:
        first = missing[0]
        boundary = first["missing"][0]
        return (
            f"the store cannot say how long the {first['label']} segment took "
            f"because the {boundary} boundary is missing ({first['reason']}); the "
            f"{len(measured)} of 8 segments it can measure cover {total:g} h, most "
            f"of it in {biggest['label']} ({biggest['hours']:g} h, {percent}%)"
            f"{suffix}.",
            "partial",
        )
    efficiency = segments["efficiency"]
    active = f"{round(100 * efficiency)}%" if efficiency is not None else "no"
    cycle = segments["cycle_hours"]
    span = f"{cycle:g} h" if cycle is not None else "the measured window"
    return (
        f"{key} took {span} from created to done: most of it went to "
        f"{biggest['label']} ({biggest['hours']:g} h, {percent}% of the measured "
        f"cycle), and {active} of the measured time was active work.",
        "measured",
    )


# --------------------------------------------------------------------------- #
# the ticket
# --------------------------------------------------------------------------- #
def ticket(
    court: ballcourt.BallCourt,
    data: jira.JiraData | None,
    key: str,
) -> dict | None:
    """The full cycle of one ticket, or ``None`` when the store has never seen it.

    ``None`` is the API's 404: a key with no story *and* no PR is not a ticket
    the store can answer about. A story without PRs (or a PR without a story) is
    a real, partially measured ticket and is returned with the gap named.
    """
    data = data or jira.EMPTY
    wanted = normalize_key(key)
    if not wanted:
        return None
    story = jira.story_for(data, wanted)
    states = _pr_states(court, wanted)
    if story is None and not states:
        return None

    cycle = jira.cycle_for(story) if story is not None else None
    segments = _segments(court, cycle)
    prs = _pr_rows(court, states)
    sprints = _sprint_rows(data, story)
    worklog_rows, worklog = _worklogs(story, prs)
    events = _events(story)
    answer, answer_kind = _answer(wanted, story, segments, prs)

    story_row = None
    if story is not None:
        story_row = {
            "key": story.key,
            "object_id": story.object_id,
            "points": story.points,
            "state": story.state,
            "epic_key": story.epic_key,
            "summary_class": story.summary_class,
            "assignee": story.actor,
            "created": _iso(cycle.boundaries["created"]),
            "closed": _iso(story.closed),
            "cycle_hours": segments["cycle_hours"],
            "censored": segments["boundaries"]["done"]["ts"] is None,
        }

    return {
        "ok": True,
        "key": wanted,
        "available": story is not None,
        "snapshot": _iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "assumption": court.assumption,
        **court.calendar_warning(),
        "story": story_row,
        "segments": segments,
        "prs": prs,
        "pr_count": len(prs),
        "sprints": sprints,
        "worklogs": worklog_rows,
        "worklog": worklog,
        "events": events,
        "event_count": len(events),
        "answer": answer,
        "answer_kind": answer_kind,
        "note": (
            None if story is not None else
            "no STORY object carries this key: the PR rows are shown, but the "
            "ticket's own segments and events are not in the store"
        ),
    }

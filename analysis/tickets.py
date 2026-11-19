"""Ticket-centric view (prd/25): one ticket's whole cycle, not one PR's.

The delivery list, the timeline and the rules all describe PRs. The question a
team actually asks — *why did DEL-167 take three weeks?* — is about the
**ticket**: its segments (backlog, refinement, sprint, work, review, merge,
plus the prd/29 verification split when the workflow names a QA stage), the PRs
that carry it, the sprints it was committed to, and the hours logged against it.
This module assembles exactly that one ticket from the two halves of the store:

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
    "ready": (
        "no status change to a ready stage, and no 'pointed' event "
        "(the estimate refinement produces)"
    ),
    "refined": "no 'pointed' event (the estimate refinement produces)",
    "sprint": "no 'sprint_added' event",
    "started": "no status change to in progress",
    "in_review": (
        "no status change to in review (a [jira.stages] map must name the "
        "workflow's review status)"
    ),
    "in_verification": (
        "no status change to in verification (a [jira.stages] map must name "
        "the workflow's QA status)"
    ),
    "pr_open": "no PR names this key",
    "first_review": "no reviewer action on its PRs",
    "approval": "no approval on its PRs",
    "merged": "no merged PR names this key",
    "done": "no status change to done, and the story is still open",
}

#: The boundary order the payload reports (prd/25's nine timestamps, plus the
#: prd/29 stage boundaries).
BOUNDARY_ORDER = (
    "created", "refined", "ready", "sprint", "started", "in_review",
    "in_verification", "pr_open", "first_review", "approval", "merged", "done",
)

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
        # concepts.md §6 / prd/40: one review-round definition everywhere — the
        # same function the rules page and the ticket cycle read.
        pushes_rounds, unapprovals = ballcourt.review_round_events(state)
        rounds = len(pushes_rounds) + len(unapprovals)
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
                # the named owner when one person holds the ball (concepts.md §2)
                "waited_on": interval.ball_label,
                "waiting_on": list(interval.person_holders),
                "measured": interval.measured,
                "kind": interval.kind,
                "evidence": interval.evidence,
                "commits": interval.commits,
                "open": interval.open,
            }
            for interval in state.intervals
        ]
        # prd/42 M6: the drawer's review facts come from the last interval's
        # status vector — reviewers asked, who approved, who still owes — and
        # the PR's last event, so "waiting on author / CI / rebasing" is never a
        # dash when the store knows the answer.
        latest = state.intervals[-1] if state.intervals else None
        waiting_on = sorted(
            set(latest.pending_all if latest is not None else ())
            | (
                {pr["actor"]}
                if latest is not None and latest.author_open
                and pr["actor"] not in court.bots else set()
            )
        )
        last_event = state.events[-1] if state.events else None
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
            "commit_count": len(state.commits),
            "censored": closed is None,
            "censored_at": _iso(court.snapshot) if closed is None else None,
            "review_rounds": rounds,
            "first_review": _iso(first),
            "merged": _iso(merged),
            "active_hours": active,
            "pushes": pushes,
            "push_classes": dict(sorted(classes.items())),
            "intervals": intervals,
            "reviewers": list(latest.roster if latest is not None else ()),
            "reviewers_total": len(latest.roster if latest is not None else ()),
            "reviewers_approved": len(latest.approved if latest is not None else ()),
            "reviewers_pending": len(latest.pending_all if latest is not None else ()),
            "approved": list(latest.approved if latest is not None else ()),
            "waiting_on": waiting_on,
            "author_open": bool(latest.author_open if latest is not None else False),
            "ci": bool(latest.ci if latest is not None else False),
            "phase": latest.phase if latest is not None else "no events",
            "last_event": _iso(last_event["_ts"]) if last_event is not None else None,
            "last_event_activity": last_event["activity"] if last_event is not None else None,
            "last_event_actor": last_event["actor"] if last_event is not None else None,
            "open_threads": 0,
        })
    rows.sort(key=lambda row: (row["opened"] or "", row["object_id"]))
    return rows


# --------------------------------------------------------------------------- #
# the segments for one story
# --------------------------------------------------------------------------- #
def _segment_rows(court: ballcourt.BallCourt, cycle: jira.Cycle | None,
                  *, verification: bool = False) -> list[dict]:
    rows: list[dict] = []
    keys = jira.segment_keys(
        cycle.boundaries if cycle is not None else {}, verification=verification
    )
    for key in keys:
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
            "no_pr_open_hours": None,
            "no_pr_open_share": None,
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
        no_pr = round(sum(
            court.business_hours(cycle.person, low, high)
            for low, high in jira.no_pr_open_spans(cycle.story, t1, t2)
        ), 2)
        row["hours"] = elapsed
        row["active_hours"] = active
        row["efficiency"] = round(active / elapsed, 4) if elapsed > 0 else None
        row["no_pr_open_hours"] = no_pr
        row["no_pr_open_share"] = round(no_pr / elapsed, 4) if elapsed > 0 else 0.0
        rows.append(row)
    return rows


def _segments(court: ballcourt.BallCourt, cycle: jira.Cycle | None,
              *, verification: bool = False) -> dict:
    rows = _segment_rows(court, cycle, verification=verification)
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
    # prd/36 §4: a reopened story reports *both* done boundaries — the first
    # cycle and the final one — never silently only one.
    first_done = cycle.boundaries.get("first_done") if cycle is not None else None
    final_done = cycle.boundaries.get("done") if cycle is not None else None
    cycle_first_done_hours = (
        round(court.business_hours(cycle.person, begin, first_done), 2)
        if cycle is not None and begin is not None and first_done is not None
        and first_done > begin else None
    )
    largest = max(measured, key=lambda row: (row["hours"], row["key"])) if measured else None
    missing = [name for name in BOUNDARY_ORDER
               if cycle is None or cycle.boundaries.get(name) is None]
    return {
        "order": [row["key"] for row in rows],
        "rows": rows,
        "boundaries": boundaries,
        "measured": len(measured),
        "missing_segments": [row["key"] for row in rows if not row["available"]],
        "missing": missing,
        "total_hours": total,
        "active_hours": active,
        "efficiency": round(active / total, 4) if total > 0 else None,
        "cycle_hours": cycle_hours,
        "cycle_first_done_hours": cycle_first_done_hours,
        "first_done": _iso(first_done),
        "final_done": _iso(final_done),
        "reopened": bool(cycle is not None and cycle.reopens),
        "reopen_count": len(cycle.reopens) if cycle is not None else 0,
        "reopens": [
            {"ts": _iso(item["ts"]), "from": item["from"], "to": item["to"]}
            for item in (cycle.reopens if cycle is not None else [])
        ],
        # prd/36 §2: the "no PR open" sub-span, stated per segment and summed
        "no_pr_open_hours": round(
            sum(row["no_pr_open_hours"] or 0.0 for row in measured), 2
        ),
        "pr_gaps": [
            {
                "from": _iso(gap["start"]),
                "to": _iso(gap["end"]),
                "hours": round(court.business_hours(cycle.person, gap["start"], gap["end"]), 2),
                "from_pr": gap["from_pr"],
                "to_pr": gap["to_pr"],
            }
            for gap in (cycle.pr_gaps if cycle is not None else [])
        ],
        "explained_share": round(total / cycle_hours, 4) if cycle_hours else None,
        "largest": largest["key"] if largest else None,
        "largest_label": largest["label"] if largest else None,
        "largest_hours": largest["hours"] if largest else None,
        "largest_share": largest["share"] if largest else None,
        "largest_no_pr_open_hours": (
            largest["no_pr_open_hours"] if largest else None
        ),
        "clamped": sum(1 for row in rows if row["clamped"]),
        "hours_mode": court.hours_mode,
        "heuristic": (
            "the same boundaries and active-work accounting as the prd/21 "
            "decomposition: created → ready = the first status → ready, "
            "falling back to the first 'pointed' estimate; sprint = first "
            "'sprint_added', status stages for started/in-review/in-verification/"
            "done, and the story's PRs (joined on PR.ticket_keys) for "
            "open/review/merge"
        ),
    }


# --------------------------------------------------------------------------- #
# sprints, worklogs and events
# --------------------------------------------------------------------------- #
def _sprint_id(event: dict) -> int | None:
    """The sprint an event is about (the shared reader in ``analysis.jira``)."""
    return jira.event_sprint_id(event)


def _sprint_rows(court, data: jira.JiraData, story: jira.Story | None) -> dict:
    """The ticket's sprint memberships with their done-by-end outcome (prd/37).

    ``carried_over`` is the lens definition, not the multi-sprint re-add: the
    story missed the close of a **closed** sprint it was committed to (finished
    late, or still open). Each membership says which of the three it was, so the
    ticket chip and the sprint strip cannot disagree.
    """
    if story is None:
        return {
            "memberships": [], "carried_over": False, "sprints_involved": 0,
            "late": 0, "never": 0,
        }
    by_id = {
        sprint["id"]: sprint for sprint in data.sprints.values()
        if sprint.get("id") is not None
    }
    done = jira._done_at(story)
    unmerged_prs = jira.story_unmerged_prs(story)
    rows: dict[int, dict] = {}
    order: list[int] = []
    for event in story.of("sprint_added"):
        sid = _sprint_id(event)
        if sid is None:
            continue
        if sid not in rows:
            meta = by_id.get(sid) or {}
            boundary = jira.sprint_boundary(meta, court)
            closed = jira.sprint_closed(meta)
            if boundary is None:
                outcome = "unknown"
            elif done is None:
                outcome = "never"
            elif done <= boundary and unmerged_prs:
                # prd/43 §5: the ticket chip cannot say "done by end" while the
                # story's code is still unmerged (DEL-167) — that is the same
                # fact the delivery list calls "Done in Jira, PR open".
                outcome = "unmerged"
            elif done <= boundary:
                outcome = "by_end"
            else:
                outcome = "late"
            slip = 0.0
            if outcome == "late" and done is not None and boundary is not None:
                slip = round(court.business_hours(story.actor, boundary, done), 2)
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
                "closed": closed,
                "outcome": outcome,
                "done_by_end": outcome == "by_end",
                "unmerged": outcome == "unmerged",
                "unmerged_prs": list(unmerged_prs),
                "slip_hours": slip,
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
    carried = [
        row for row in memberships
        if row["closed"] and row["outcome"] in ("late", "never")
    ]
    return {
        "memberships": memberships,
        "carried_over": bool(carried),
        "sprints_involved": len(order),
        "late": sum(1 for row in carried if row["outcome"] == "late"),
        "never": sum(1 for row in carried if row["outcome"] == "never"),
        # not a carry-over (the story was not re-committed), but not delivered
        # either: the chip and the sprint strip both show it separately.
        "unmerged": sum(1 for row in memberships if row["outcome"] == "unmerged"),
        "missed_sprints": [row["sprint_id"] for row in carried],
        "explicit_readd": len(order) > 1,
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
    late = buckets["late"]
    return rows, {
        "worklogs": len(rows),
        "logged_hours": logged,
        "active_hours": active,
        "logged_to_active_ratio": round(logged / active, 2) if active > 0 else None,
        "buckets": buckets,
        # prd/36 §5: "logged a week late" is the ``late`` bucket (> 7 days);
        # within_week (2–7 d) is not a week late and is reported separately.
        "late_share": round(late / len(rows), 4) if rows else None,
        "late_worklogs": late,
        "within_week_worklogs": buckets["within_week"],
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


#: prd/41 B5: a no-PR-open stretch inside the headline segment is *material*
#: when it is at least one business day — below that it is noise, above it the
#: headline must say the segment was partly a missing PR, not review.
MATERIAL_GAP_HOURS = 8.0


def _gap_notes(biggest: dict, segments: dict) -> tuple[str, str]:
    """The no-PR-open gap, as a headline clause and a follow-up sentence.

    prd/41 B5: "most of it went to review rounds (62 %)" hid that 60 h of the
    segment had no PR open at all. A material gap is named in the headline
    itself (", 60 h of which had no PR open"); the largest overlapping PR gap
    becomes its own sentence with a rounded business-day count.
    """
    hours = biggest.get("no_pr_open_hours") or 0.0
    if hours <= 0:
        return "", ""
    clause = ""
    if hours >= MATERIAL_GAP_HOURS:
        clause = f", {hours:g} h of which had no PR open"
    parts = []
    if hours < MATERIAL_GAP_HOURS:
        parts.append(f"{hours:g} h of that segment had no PR open.")
    window_from = biggest.get("from") or ""
    window_to = biggest.get("to") or ""
    overlapping = [
        gap for gap in (segments.get("pr_gaps") or [])
        if gap.get("from") and gap.get("to")
        and gap["from"] <= window_to and window_from <= gap["to"]
    ]
    if overlapping:
        gap = max(overlapping, key=lambda item: item["hours"])
        days = round(gap["hours"] / jira.BUSINESS_HOURS_PER_DAY, 1)
        parts.append(
            f"PR #{gap['to_pr']} started {days:g} business day"
            f"{'' if days == 1 else 's'} after PR #{gap['from_pr']} merged."
        )
    return clause, " ".join(parts)


# --------------------------------------------------------------------------- #
# cohort benchmark (prd/42 M2)
# --------------------------------------------------------------------------- #
def _benchmark(court: ballcourt.BallCourt, data: jira.JiraData,
               story: jira.Story | None, segments: dict) -> dict:
    """Where this story's cycle sits among its peers.

    "73 h" means nothing without a cohort. Rank 1 is the *fastest* story, so
    "13th of 87 fastest" reads as "faster than 74 stories". The same-points
    median answers "is 73 h slow for an 8-point story?" — the comparison a lead
    actually makes.

    The cohort is **completed** stories only: an open story's "cycle" is
    censored at the snapshot, and mixing the two made the DEL-167 cohort 100
    instead of the 87 completed stories (and moved every rank).
    """
    current = segments.get("cycle_hours")
    censored = bool(segments.get("boundaries", {}).get("done", {}).get("ts") is None)
    if story is None or current is None:
        return {
            "available": False,
            "note": "the story has no measured created → done cycle, so it has no cohort rank",
        }
    if censored:
        return {
            "available": False,
            "note": (
                "the story is still open, so its cycle is right-censored and "
                "has no completed cohort rank"
            ),
        }
    cycles: list[tuple[str, float, int | None]] = []
    for peer in data.stories:
        cycle = jira._cycle(peer)
        begin = cycle.boundaries.get("created")
        done = cycle.boundaries.get("done")
        if begin is None or done is None or done <= begin:
            continue
        hours = round(court.business_hours(peer.actor, begin, done), 2)
        cycles.append((peer.key, hours, peer.points))
    if not cycles:
        return {"available": False, "note": "no completed story cycle in the cohort"}
    ordered = sorted(cycles, key=lambda item: (item[1], item[0]))
    rank = 1 + sum(1 for item in ordered if item[1] < current)
    same_points = [
        hours for key, hours, points in ordered
        if story.points is not None and points == story.points
    ]
    return {
        "available": True,
        "rank": rank,
        "n": len(ordered),
        "faster": max(0, rank - 1),
        "slower": max(0, len(ordered) - rank),
        "percentile": round(rank / len(ordered), 4),
        "median_hours": _median([item[1] for item in ordered]),
        "points": story.points,
        "points_n": len(same_points),
        "points_median_hours": _median(same_points),
        "censored": False,
        "basis": (
            "rank = 1 + completed stories with a shorter measured created → done "
            "cycle; the cohort is every completed story in the store (open "
            "stories are censored and excluded)"
        ),
    }


# --------------------------------------------------------------------------- #
# "Done in Jira, PR open" (prd/42 M4)
# --------------------------------------------------------------------------- #
def done_but_open(court: ballcourt.BallCourt, data: jira.JiraData,
                  delivery: dict | None = None) -> dict:
    """Open PRs whose ticket is already Done in Jira — with who owes each.

    This is the first-class list the QA run had to reconstruct by hand: three
    items where Jira says Done but the code was never merged. The extra case is
    ``opened_after_close`` — a PR opened *after* its ticket was closed (DEL-183:
    nine days later), which means the work could not have been part of the
    delivered story at all. Ticket keys travel with every row so the list links
    to ``#ticket/KEY`` like every other view.
    """
    done = jira.done_keys(data)
    by_key = {story.key: story for story in data.stories}
    rows_by_id = {
        row["object_id"]: row for row in ((delivery or {}).get("rows") or [])
    }
    out: list[dict] = []
    for state in court.prs:
        pr = state.pr
        if pr["closed_ts"] is not None:
            continue
        for key in pr.get("ticket_keys") or ():
            story = by_key.get(key)
            if key not in done or story is None:
                continue
            closed = jira._cycle(story).boundaries.get("done")
            opened = pr["created_ts"]
            after = bool(closed is not None and opened is not None and opened > closed)
            row = rows_by_id.get(pr["object_id"]) or {}
            out.append({
                "key": key,
                "object_id": pr["object_id"],
                "id": f"{pr['repo'].split('/')[-1]}#{pr['pr_id']}",
                "repo": pr["repo"],
                "repo_short": pr["repo"].split("/")[-1],
                "number": pr["pr_id"],
                "state": pr["state"],
                "author": pr["actor"],
                "ticket_closed": _iso(closed),
                "pr_opened": _iso(opened),
                "opened_after_close": after,
                "opened_after_close_hours": (
                    round(court.business_hours(pr["actor"], closed, opened), 2)
                    if after else 0.0
                ),
                "waiting_on": list(row.get("waiting_on") or ()),
                "waiting_hours": row.get("waiting_hours"),
                "age_hours": row.get("age_hours"),
                "stale_reasons": list(row.get("stale_reasons") or ()),
                "reviewers_approved": row.get("reviewers_approved"),
                "reviewers_total": row.get("reviewers_total"),
            })
    out.sort(key=lambda row: (
        not row["opened_after_close"],
        -(row["waiting_hours"] or 0),
        row["key"],
    ))
    return {
        "available": bool(data.stories),
        "count": len(out),
        "opened_after_close": sum(1 for row in out if row["opened_after_close"]),
        "rows": out,
        "note": (
            "a ticket is Done when its final done boundary exists; the PR is "
            "still open at the snapshot. 'PR opened after the ticket closed' is "
            "a stronger finding: the work cannot have been part of the delivered "
            "story"
        ),
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 2)


# --------------------------------------------------------------------------- #
# epics are not stories (prd/42 P9)
# --------------------------------------------------------------------------- #
def _epic(court: ballcourt.BallCourt, data: jira.JiraData,
          key: str, epic: dict) -> dict:
    """A minimal epic page: the parent, its children and why it has no cycle.

    An epic is a grouping, not a work item: it has no created → done cycle of
    its own, and rendering one as "0/9 segments" made the product look broken.
    The page now says what it is and lists the child stories with their own
    cycles, so the epic is a doorway rather than a dead end.
    """
    children = [
        story for story in data.stories
        if (story.epic_key or "") == key
    ]
    child_rows = []
    for story in children:
        cycle = jira._cycle(story)
        begin = cycle.boundaries.get("created")
        done = cycle.boundaries.get("done")
        end = done or court.snapshot
        hours = (
            round(court.business_hours(story.actor, begin, end), 2)
            if begin is not None and end is not None and end > begin else None
        )
        child_rows.append({
            "key": story.key,
            "state": story.state,
            "points": story.points,
            "cycle_hours": hours,
            "censored": done is None,
        })
    child_rows.sort(key=lambda row: (row["cycle_hours"] is None, row["cycle_hours"] or 0, row["key"]))
    story_count = len(child_rows)
    open_count = sum(1 for row in child_rows if row["censored"])
    return {
        "ok": True,
        "key": key,
        "kind": "epic",
        "available": False,
        "snapshot": _iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "assumption": court.assumption,
        **court.calendar_warning(),
        "epic": {
            "key": key,
            "object_id": epic.get("object_id"),
            "state": epic.get("state"),
            "created": _iso(epic.get("created")),
            "closed": _iso(epic.get("closed")),
            "stories": child_rows,
            "story_count": story_count,
            "open_stories": open_count,
            "points": sum(row["points"] or 0 for row in child_rows),
        },
        "story": None,
        "end_state": "epic",
        "open_prs": [],
        "reopened": False,
        "reopen_count": 0,
        "segments": {"rows": [], "boundaries": {}, "measured": 0, "missing": []},
        "prs": [],
        "pr_count": 0,
        "sprints": {"memberships": []},
        "worklogs": [],
        "worklog": {},
        "events": [],
        "event_count": 0,
        "answer": (
            f"{key} is an epic — a parent grouping of {story_count} "
            f"stor{'y' if story_count == 1 else 'ies'}, not a story. It has no "
            "created → done cycle of its own; open a child story for the cycle, "
            "or read the children below."
        ),
        "answer_kind": "epic",
        "note": (
            "an epic is not a work item: the eight story segments describe "
            "stories, and an epic has none"
        ),
    }


# --------------------------------------------------------------------------- #
# the prose answer (prd/10 §7: honesty in a sentence)
# --------------------------------------------------------------------------- #
def _after_prefix(prefix: str, body: str) -> str:
    """Join the prefix and the body without a lowercase sentence after a stop.

    prd/41 B5: "… (right-censored). the store cannot say …" is a join bug; a
    sentence that follows a full stop starts with a capital.
    """
    if prefix.endswith(". ") and body[:1].islower():
        body = body[0].upper() + body[1:]
    return f"{prefix}{body}"


def _answer(
    key: str,
    story: jira.Story | None,
    segments: dict,
    prs: list[dict],
) -> tuple[str, str]:
    """One sentence: where the time went, or which boundary the store lacks.

    prd/36 makes the sentence honest about four things the old one hid:

    * the **real end state** — a Done ticket with an open PR says so
      ("Done in Jira, but PR #53 is still open (right-censored)");
    * **reopens** — both the created→first-done and created→final-done spans;
    * a **multi-PR idle gap** inside the largest segment ("N h of it had no PR
      open: PR #53 started 11 business days after PR #52 merged");
    * the boundary a segment is missing, as before.
    """
    measured = [row for row in segments["rows"] if row["available"]]
    missing = [row for row in segments["rows"] if not row["available"]]
    open_prs = [pr for pr in prs if pr.get("censored")]
    censored = story is not None and segments["boundaries"]["done"]["ts"] is None
    suffix = (
        " The story is still open, so the cycle is right-censored at the snapshot."
        if censored else ""
    )
    # prd/36 §3: the end state is stated, and an open PR is disclosed rather
    # than left to a table row.
    prefix = ""
    if open_prs and not censored:
        numbers = ", ".join(f"#{pr['number']}" for pr in open_prs)
        prefix = (
            f"Done in Jira, but PR {numbers} is still open (right-censored); "
            "the cycle is measured to the ticket's done, not to that PR. "
        )
    elif open_prs and censored:
        numbers = ", ".join(f"#{pr['number']}" for pr in open_prs)
        prefix = f"Still open in Jira, with PR {numbers} open too (right-censored). "
    if story is None:
        if prs:
            plural = "s" if len(prs) != 1 else ""
            return (
                f"the store has no STORY object for {key}: its {len(prs)} PR{plural} "
                "can be shown, but the ticket's own waiting cannot be decomposed.",
                "story_missing",
            )
        return f"the store has no story and no PR for {key}.", "story_missing"
    reopened = bool(segments.get("reopened"))
    # prd/36 §4: a reopened story always states both boundaries, even when a
    # segment is missing and the answer is otherwise partial.
    reopen_note = ""
    if reopened:
        first_hours = segments.get("cycle_first_done_hours")
        final_hours = segments.get("cycle_hours")
        reopens = segments.get("reopen_count") or 1
        if censored:
            reopen_note = (
                f" {key} was reopened {reopens} time"
                f"{'' if reopens == 1 else 's'}: {first_hours:g} h from created to "
                f"the first done, and still open at the snapshot "
                f"({final_hours:g} h)."
            )
        else:
            reopen_note = (
                f" {key} was reopened {reopens} time"
                f"{'' if reopens == 1 else 's'}: {first_hours:g} h from created to "
                f"the first done and {final_hours:g} h to the final done."
            )
    if not measured:
        first = missing[0]
        boundary = first["missing"][0]
        body = (
            f"the store cannot say where {key}'s time went because no "
            f"segment has both boundaries — the {boundary} boundary is missing "
            f"({first['reason']}).{reopen_note}{suffix}"
        )
        return _after_prefix(prefix, body), "unmeasurable"
    total = segments["total_hours"]
    biggest = max(measured, key=lambda row: (row["hours"], row["key"]))
    percent = round(100 * (biggest["share"] or 0))
    # prd/41 B5: a no-PR-open gap is part of the headline when it is material,
    # and the day count is rounded to one decimal (never "7.51875").
    gap_clause, gap_sentence = _gap_notes(biggest, segments)
    if missing:
        first = missing[0]
        boundary = first["missing"][0]
        body = (
            f"the store cannot say how long the {first['label']} segment "
            f"took because the {boundary} boundary is missing "
            f"({first['reason']}); the {len(measured)} of {len(segments['rows'])} "
            f"segments it can measure cover {total:g} h, most of it in "
            f"{biggest['label']} ({biggest['hours']:g} h, {percent}%)"
            f"{gap_clause}."
            f"{(' ' + gap_sentence) if gap_sentence else ''}"
            f"{reopen_note}{suffix}"
        )
        return _after_prefix(prefix, body), "partial"
    efficiency = segments["efficiency"]
    active = f"{round(100 * efficiency)}%" if efficiency is not None else "no"
    if reopened:
        first_hours = segments.get("cycle_first_done_hours")
        final_hours = segments.get("cycle_hours")
        reopens = segments.get("reopen_count") or 1
        if censored:
            span = (
                f"{key} took {first_hours:g} h from created to the first done, "
                f"then was reopened and is still open ({final_hours:g} h to the "
                "snapshot)"
            )
        else:
            span = (
                f"{key} took {first_hours:g} h from created to the first done and "
                f"{final_hours:g} h to the final done (reopened {reopens} "
                f"time{'' if reopens == 1 else 's'})"
            )
    else:
        cycle = segments["cycle_hours"]
        span = (
            f"{key} took {cycle:g} h from created to done"
            if cycle is not None else f"{key}'s cycle is the measured window"
        )
    body = (
        f"{span}: most of it went to {biggest['label']} "
        f"({biggest['hours']:g} h, {percent}% of the measured cycle)"
        f"{gap_clause}. {active} of the measured time was active work."
        f"{(' ' + gap_sentence) if gap_sentence else ''}{suffix}"
    )
    return _after_prefix(prefix, body), "measured"


# --------------------------------------------------------------------------- #
# the ticket
# --------------------------------------------------------------------------- #
def ticket(
    court: ballcourt.BallCourt,
    data: jira.JiraData | None,
    key: str,
    threads: list[dict] | None = None,
) -> dict | None:
    """The full cycle of one ticket, or ``None`` when the store has never seen it.

    ``None`` is the API's 404: a key with no story *and* no PR is not a ticket
    the store can answer about. A story without PRs (or a PR without a story) is
    a real, partially measured ticket and is returned with the gap named. An
    epic key is answered as an epic (prd/42 P9), never as a story with 0/9
    segments.

    ``threads`` (the review threads from ``rules.load_threads``) lets each PR
    row carry its open-thread count, which the PR drawer shows (prd/42 M6).
    """
    data = data or jira.EMPTY
    wanted = normalize_key(key)
    if not wanted:
        return None
    story = jira.story_for(data, wanted)
    states = _pr_states(court, wanted)
    # prd/42 P9: an epic key is a parent grouping, not a story — even when every
    # PR in the corpus names it as its epic link (DEL-1 has 131).
    epic = (data.epic_objects or {}).get(wanted)
    if story is None and epic is not None:
        return _epic(court, data, wanted, epic)
    if story is None and not states:
        return None

    cycle = jira.cycle_for(story) if story is not None else None
    # prd/29: the store's workflow decides whether the merge→done segment is
    # split around verification, so a ticket that never reached QA still shows
    # the QA segment as missing (with the boundary named) instead of silently
    # folding it into "done".
    segments = _segments(court, cycle, verification=jira.uses_verification(data))
    prs = _pr_rows(court, states)
    open_threads: dict[tuple[str, int], int] = {}
    for thread in threads or ():
        if thread.get("closed_at") is not None:
            continue
        pair = (thread.get("repo"), thread.get("pr_id"))
        open_threads[pair] = open_threads.get(pair, 0) + 1
    for row in prs:
        row["open_threads"] = open_threads.get((row["repo"], row["number"]), 0)
    sprints = _sprint_rows(court, data, story)
    worklog_rows, worklog = _worklogs(story, prs)
    events = _events(story)
    answer, answer_kind = _answer(wanted, story, segments, prs)
    benchmark = _benchmark(court, data, story, segments)

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
            "reopened": segments["reopened"],
        }

    open_prs = [
        {
            "number": pr["number"],
            "repo": pr["repo"],
            "state": pr["state"],
            "opened": pr["opened"],
            "censored_at": pr["censored_at"],
            "cycle_hours": pr["cycle_hours"],
        }
        for pr in prs if pr.get("censored")
    ]
    if story is None:
        end_state = "story_missing"
    elif segments["boundaries"]["done"]["ts"] is None:
        end_state = "open"
    elif open_prs:
        end_state = "done_with_open_pr"
    else:
        end_state = "done"

    return {
        "ok": True,
        "key": wanted,
        "kind": "story",
        "available": story is not None,
        "snapshot": _iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "assumption": court.assumption,
        **court.calendar_warning(),
        "story": story_row,
        "end_state": end_state,
        "open_prs": open_prs,
        "reopened": segments["reopened"],
        "reopen_count": segments["reopen_count"],
        "segments": segments,
        "prs": prs,
        "pr_count": len(prs),
        # prd/42 M2: "73 h" is read against its cohort, not in a vacuum.
        "benchmark": benchmark,
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

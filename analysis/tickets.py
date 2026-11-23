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
from analysis import benchmark
from analysis import jira
from analysis.effort import ACTIVE_PHASES
from ingest.store import iso

#: The benchmark-contrast bases (prd/57 §3: every multiple is labelled with
#: what it is a multiple of).
CYCLE_BENCHMARK_BASIS = "median created → done over completed stories"
SEGMENT_BENCHMARK_BASIS = "median of the same segment over completed stories"

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
    "reviewer_added": "no reviewer is named on the PR's roster (a store that asks reviewers at open has none)",
    "first_review": "no reviewer action on its PRs",
    "approval": "no approval on its PRs",
    "merged": "no merged PR names this key",
    "done": "no status change to done, and the story is still open",
}

#: The boundary order the payload reports (prd/25's nine timestamps, plus the
#: prd/29 stage boundaries).
BOUNDARY_ORDER = (
    "created", "refined", "ready", "sprint", "started", "in_review",
    "in_verification", "pr_open", "reviewer_added", "first_review", "approval",
    "merged", "done",
)

#: The order the *clock* should show (prd/63 F17). It differs from
#: ``BOUNDARY_ORDER``: the workflow's verification stage follows the merge
#: (concepts.md §5), so "in_verification before PR open" is a correct sequence,
#: not an inversion. A boundary is flagged only when it precedes its
#: predecessor on this list.
BOUNDARY_CLOCK_ORDER = (
    "created", "refined", "ready", "sprint", "started", "pr_open",
    "reviewer_added", "in_review", "first_review", "approval", "merged",
    "in_verification", "done",
)

#: Only these boundaries have a strict, one-way dependency. `ready`/`refined`
#: and the PR events vs the workflow's `in_review` status can legitimately land
#: in either order, so flagging them would invent a quirk (prd/63 F17).
BOUNDARY_STRICT_ORDER = (
    "created", "started", "pr_open", "approval", "merged", "in_verification",
    "done",
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


def _ball_phase(latest) -> str:
    """The PR's current phase label, derived from the ball (prd/55 §4).

    ``latest.phase`` names the interval that just *ended*; it can be ``active``
    because the author pushed last while the reviewers have held the ball ever
    since. The state vector is the truth: pending reviewers make it ``review``
    (``ready`` is a state, not a phase — concepts.md §5), and only an author
    with no reviewer pending is ``active``.
    """
    if latest is None:
        return "no events"
    if latest.ci and not latest.author_open and not latest.pending_all:
        return "ci"
    if latest.pending_all:
        return "review"
    if latest.author_open or latest.ball == "author":
        return "active"
    if latest.ball == ballcourt.READY or latest.ready:
        return "review"
    return latest.phase


def _approved_unmerged(court: ballcourt.BallCourt, state,
                       latest) -> dict | None:
    """A PR every reviewer approved and that closed without a merge (prd/51).

    This is the same predicate ``jira.rework`` uses for its
    ``approved_unmerged`` group — the last status vector's roster is non-empty
    and every reviewer on it approved, the PR never merged, and it is closed
    (an *open* fully-approved PR can still merge, so it is not a lost delivery
    yet). The fields are the group's own: who approved, when, who closed it,
    whether that closer is a bot, and the business hours from the final
    approval to the close (DEL-161: #34 approved by DaKo + FrMo, declined by
    the bot 23 d 3 h later).

    ``None`` when the finding does not apply, so a normal merged PR carries
    nothing on the ticket page.
    """
    pr = state.pr
    if pr.get("state") == "merged" or pr.get("closed_ts") is None:
        return None
    if latest is None or not latest.roster:
        return None
    approved = set(latest.approved or ())
    if not approved or approved != set(latest.roster):
        return None
    approvals = [
        event["_ts"] for event in state.events
        if event["activity"] == "pr_approved"
    ]
    if not approvals:
        return None
    approved_at = max(approvals)
    closed_at = pr["closed_ts"]
    if closed_at <= approved_at:
        return None
    closed_by = next(
        (
            event["actor"] for event in reversed(state.events)
            if event["activity"] in ("pr_declined", "pr_closed")
        ),
        None,
    )
    return {
        "approved_by": sorted(approved),
        "approved_at": _iso(approved_at),
        "closed_at": _iso(closed_at),
        # the store may not name the closer (an old scrape); the page then
        # says "closed unmerged after approval" and invents no bot (prd/64 F-15).
        "closed_by": closed_by,
        "bot_closed": bool(closed_by and closed_by in (court.bots or set())),
        "hours": round(
            court.business_hours(pr["actor"], approved_at, closed_at), 2
        ),
    }


def _pr_rows(court: ballcourt.BallCourt, states: list,
             ticket_closed: datetime | None = None) -> list[dict]:
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
        pending = sorted(
            set(latest.pending_all if latest is not None else ())
            | (
                {pr["actor"]}
                if latest is not None and latest.author_open
                and pr["actor"] not in court.bots else set()
            )
        )
        # prd/70 F-09: the same attribution Aging and Delivery read. With no
        # human holder the item waits on the live queue, which this list names
        # instead of leaving it empty (the page used to print "on nobody").
        attribution = ballcourt.holder_attribution(
            latest,
            author=pr["actor"],
            sentinels=court.sentinels,
            bots=court.bots,
        )
        waiting_on = pending or [attribution["label"]]
        # prd/55 §1/§7: the drawer and the headline must be able to state the
        # *current* wait, not only the measured phases. The wait opens when the
        # last interval handed the ball over and ends at the snapshot while the
        # PR is open; it is priced on the one holder's calendar when there is
        # exactly one (concepts.md §1/§2), else on the team calendar.
        last_event = state.events[-1] if state.events else None
        opened = pr["created_ts"]
        closed = pr["closed_ts"]
        end = closed or court.snapshot
        waiting_since = (
            latest.start if latest is not None and closed is None else None
        )
        waiting_hours = None
        if waiting_since is not None and end is not None and end >= waiting_since:
            holder = latest.owner if latest is not None else None
            waiting_hours = round(
                court.business_hours(holder, waiting_since, end), 2
            )
        # prd/55 §7: the phase list must tile the PR's whole span. The
        # unobserved tail is a real span (not a measured phase) and is included
        # here so "in review 2 d + unobserved —" cannot hide 21 waiting days.
        phase_hours: dict[str, float] = {}
        for interval in state.intervals:
            if interval.hours <= 0:
                continue
            phase_hours[interval.phase] = round(
                phase_hours.get(interval.phase, 0.0) + round(interval.hours, 2), 2
            )
        active = round(sum(
            interval["hours"] for interval in intervals
            if interval["measured"] and interval["phase"] in ACTIVE_PHASES
        ), 2)
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
            # prd/64 F-14: the ticket page must flag a PR opened *after* the
            # story closed — the same finding the Overview/Delivery done-open
            # panel carries, priced on the same clock (the PR author's
            # business hours). A PR opened before the close carries nothing.
            "opened_after_close": bool(
                ticket_closed is not None and opened is not None
                and opened > ticket_closed
            ),
            "opened_after_close_hours": (
                round(court.business_hours(pr["actor"], ticket_closed, opened), 2)
                if (ticket_closed is not None and opened is not None
                    and opened > ticket_closed) else 0.0
            ),
            # prd/64 F-15: approved by every reviewer and closed unmerged —
            # the delivery the story lost (DEL-161 #34).
            "approved_unmerged": _approved_unmerged(court, state, latest),
            "cycle_hours": cycle,
            "commit_count": len(state.commits),
            # prd/63 V-A6: the change size and whether it shipped. These are the
            # PR's own counts (never the diff), so the notion of "how much of
            # the story's change is unmerged" is measurable.
            "lines_added": int(pr.get("lines_added") or 0),
            "lines_removed": int(pr.get("lines_removed") or 0),
            "lines_changed": int(pr.get("lines_added") or 0)
                + int(pr.get("lines_removed") or 0),
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
            # prd/70 F-09: the queue fallback (``review queue`` / ``ready to
            # merge``) and its role, so the page can label the holder with the
            # same words Delivery and Aging use.
            "holder_label": attribution["label"],
            "holder_role": attribution["role"],
            "holder_queue": bool(attribution["queue"]),
            "waiting_since": _iso(waiting_since),
            "waiting_hours": waiting_hours,
            "author_open": bool(latest.author_open if latest is not None else False),
            "ci": bool(latest.ci if latest is not None else False),
            "phase": latest.phase if latest is not None else "no events",
            # prd/55 §4: the phase comes from the ball, not from whatever the
            # last measured interval happened to end with. A PR whose pending
            # reviewers hold the ball is `review`, never `active` just because
            # the author's last push opened the interval.
            "phase_now": _ball_phase(latest),
            "phase_hours": phase_hours,
            "phase_total_hours": round(sum(phase_hours.values()), 2),
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
        # prd/46 §1: the ordered boundary prices the segment, the raw one names
        # the inversion. A boundary the clamp moved is a data quirk, not a
        # finding, and the shares can never sum past the cycle.
        t1, t2, t1_raw, t2_raw = jira.segment_bounds_for(cycle, key) if cycle is not None else (
            None, None, None, None,
        )
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
            # prd/63 V-A7: the part of this segment that is a reopen/rework
            # loop (first done → final done), so "verification → done 31 %" can
            # say it is the loop instead of implying the QA stage was slow.
            "rework_hours": None,
            "rework_share": None,
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
        if t2_raw < t1_raw:
            # out-of-order data: both boundaries exist, the segment is empty.
            row["inverted"] = True
            row["hours"] = 0.0
            row["active_hours"] = 0.0
            row["reason"] = (
                "the boundaries are out of order (the right boundary precedes the left)"
            )
            rows.append(row)
            continue
        elapsed = jira.segment_hours(court, cycle, key)
        active = round(jira.active_hours(court, cycle, t1, t2), 2)
        if active > elapsed:
            active = elapsed
            row["clamped"] = True
        no_pr = round(sum(
            court.business_hours(cycle.person, low, high)
            for low, high in jira.no_pr_open_spans(cycle.story, t1, t2)
        ), 2)
        # prd/63 V-A7: a reopened story's last segment contains the whole
        # reopen/rework loop; price the overlap here so the label can name it.
        rework = 0.0
        if cycle is not None and cycle.reopens:
            loop_from = cycle.boundaries.get("first_done")
            loop_to = cycle.boundaries.get("done") or court.snapshot
            if loop_from is not None and loop_to is not None and loop_to > loop_from:
                low, high = max(t1, loop_from), min(t2, loop_to)
                if high > low:
                    rework = round(court.business_hours(cycle.person, low, high), 2)
        row["hours"] = elapsed
        row["active_hours"] = active
        row["efficiency"] = round(active / elapsed, 4) if elapsed > 0 else None
        row["no_pr_open_hours"] = no_pr
        row["no_pr_open_share"] = round(no_pr / elapsed, 4) if elapsed > 0 else 0.0
        row["rework_hours"] = rework
        row["rework_share"] = round(rework / elapsed, 4) if elapsed > 0 else 0.0
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
    quirks = list(cycle.out_of_order) if cycle is not None else []
    quirk_by_boundary = {quirk["boundary"]: quirk for quirk in quirks}
    bounds = (
        (cycle.segment_bounds or cycle.boundaries) if cycle is not None else {}
    )
    boundaries = {
        name: {
            "ts": _iso(bounds.get(name)),
            "available": bool(cycle is not None and bounds.get(name) is not None),
            "reason": (
                BOUNDARY_REASON[name]
                if cycle is None or cycle.boundaries.get(name) is None else None
            ),
            # prd/46 §1: the raw stamp and why the clamp moved it, so the chip
            # can say "out of order" instead of drawing a different timestamp
            # with no explanation.
            "raw_ts": _iso(cycle.boundaries.get(name)) if cycle is not None else None,
            "out_of_order": quirk_by_boundary.get(name),
        }
        for name in BOUNDARY_ORDER
    }
    # prd/63 F17: the lifecycle order and the clock can disagree (DEL-183's
    # `refined` is 20 min *after* `ready`; `in_verification` precedes
    # `pr_open`). The payload says which boundary is out of order and what it
    # should follow, so the chips can be drawn chronologically with a flag
    # instead of silently asserting a false sequence.
    previous_name = None
    previous_ts = None
    for name in BOUNDARY_STRICT_ORDER:
        entry = boundaries.get(name) or {}
        if not entry.get("ts"):
            continue
        stamp = bounds.get(name)
        if stamp is not None and previous_ts is not None and stamp < previous_ts:
            entry["order_quirk"] = {
                "after": previous_name,
                "after_ts": _iso(previous_ts),
                "reason": (
                    f"the {name} boundary is dated before {previous_name}, "
                    "so the lifecycle order and the clock disagree"
                ),
            }
        # Compare each pair, not the last well-ordered boundary: a single
        # inversion must not cascade into five flags.
        previous_name, previous_ts = name, stamp
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
        # prd/46 §1: raw boundaries the segment order had to clamp, each with
        # the reason. Empty means every boundary was already in lifecycle order.
        "out_of_order": quirks,
        "boundaries_clamped": sorted(quirk_by_boundary),
        "largest": largest["key"] if largest else None,
        "largest_label": (
            f"{largest['label']} (the reopen loop)"
            if largest and (largest.get("rework_hours") or 0) >= 0.5 * (largest["hours"] or 1)
            else (largest["label"] if largest else None)
        ),
        "largest_hours": largest["hours"] if largest else None,
        "largest_share": largest["share"] if largest else None,
        "largest_no_pr_open_hours": (
            largest["no_pr_open_hours"] if largest else None
        ),
        # prd/63 V-A7: reopened stories name the rework loop, not only the
        # segment it fell into.
        "rework_hours": round(
            sum(row["rework_hours"] or 0.0 for row in measured), 2
        ),
        "rework_segments": [
            {"key": row["key"], "label": row["label"], "hours": row["rework_hours"]}
            for row in measured if (row["rework_hours"] or 0) > 0
        ],
        # prd/63 F17: the chip order a reader can trust (available stamps by
        # clock, then the missing ones), plus every out-of-order pair.
        "boundary_order": [
            name for name in sorted(
                (name for name, entry in boundaries.items() if entry.get("ts")),
                key=lambda name: boundaries[name]["ts"],
            )
        ] + [name for name in BOUNDARY_ORDER if not boundaries.get(name, {}).get("ts")],
        "boundary_quirks": [
            {"boundary": name, **entry["order_quirk"]}
            for name, entry in boundaries.items() if entry.get("order_quirk")
        ],
        "clamped": sum(1 for row in rows if row["clamped"]),
        "hours_mode": court.hours_mode,
        "heuristic": (
            "created → ready is the first status → ready (falling back to the "
            "first estimate); sprint is the first sprint membership; started, "
            "in review, in verification and done come from the workflow; and "
            "the story's own PRs supply open, first review and merge. Active "
            "time is the measured work inside each segment."
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
                # prd/70 F-16: the chip prints `S352`, the same label as the
                # sprint strip; `None` on a store scraped before the parse.
                "sprint_no": meta.get("sprint_no"),
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


def _change(pr_rows: list[dict]) -> dict:
    """How much of a story's *change* shipped (prd/63 V-A6).

    The PR line counts are the store's only size fact. A Done-in-Jira story
    whose PR is still open is not a delivered story, and a headline that reads
    only the ticket's ``created → done`` clock ("13th fastest") brags about work
    that is 62 % unmerged. This is the share that corrects it: lines changed in
    open PRs ÷ lines changed in all the story's PRs.
    """
    total = sum(int(pr.get("lines_changed") or 0) for pr in pr_rows)
    open_total = sum(
        int(pr.get("lines_changed") or 0) for pr in pr_rows
        if pr.get("censored")
    )
    merged = sum(
        int(pr.get("lines_changed") or 0) for pr in pr_rows
        if not pr.get("censored")
    )
    return {
        "available": total > 0,
        "lines_total": total,
        "lines_merged": merged,
        "lines_unmerged": open_total,
        "unmerged_share": round(open_total / total, 4) if total else None,
        "open_prs": sorted(
            pr["number"] for pr in pr_rows if pr.get("censored")
        ),
        "note": (
            None if total else
            "the store has no changed-line counts for this ticket's PRs, so "
            "the unmerged share of the change cannot be measured"
        ),
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
        clause = f", {jira.human_hours(hours)} of which had no PR open"
    parts = []
    if hours < MATERIAL_GAP_HOURS:
        parts.append(f"{jira.human_hours(hours)} of that segment had no PR open.")
    window_from = biggest.get("from") or ""
    window_to = biggest.get("to") or ""
    overlapping = [
        gap for gap in (segments.get("pr_gaps") or [])
        if gap.get("from") and gap.get("to")
        and gap["from"] <= window_to and window_from <= gap["to"]
    ]
    if overlapping:
        gap = max(overlapping, key=lambda item: item["hours"])
        # prd/63 V-B: one duration language. The gap is business hours already,
        # so it is rendered by the shared "Nd Nh" formatter — "2 d 4 h", not
        # a second unit ("12.5 business days") inside a business-hours story.
        parts.append(
            f"PR #{gap['to_pr']} started {jira.human_hours(gap['hours'])} "
            f"after PR #{gap['from_pr']} merged."
        )
    return clause, " ".join(parts)


# --------------------------------------------------------------------------- #
# cohort benchmark (prd/42 M2)
# --------------------------------------------------------------------------- #
def _benchmark(court: ballcourt.BallCourt, data: jira.JiraData,
               story: jira.Story | None, segments: dict,
               cohorts: dict | None = None) -> dict:
    """Where this story's cycle sits among its peers.

    "73 h" means nothing without a cohort. Rank 1 is the *fastest* story, so
    "13th of 87 fastest" reads as "faster than 74 stories". The same-points
    median answers "is 73 h slow for an 8-point story?" — the comparison a lead
    actually makes.

    The cohort is **completed** stories only: an open story's "cycle" is
    censored at the snapshot, and mixing the two made the DEL-167 cohort 100
    instead of the 87 completed stories (and moved every rank).

    prd/57 adds the benchmark contrast: the cycle's ``multiple`` of the cohort
    median, from the one :func:`analysis.benchmark.contrast` helper. ``cohorts``
    is the pre-built block (``benchmark_cohorts``) the payload shares with the
    segment contrasts, so one request computes the cohort once.
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
    peers = (cohorts or benchmark_cohorts(court, data)).get("stories") or []
    if not peers:
        return {"available": False, "note": "no completed story cycle in the cohort"}
    ordered = sorted(peers, key=lambda item: (item["hours"], item["key"]))
    rank = 1 + sum(1 for item in ordered if item["hours"] < current)
    same_points = [
        item["hours"] for item in ordered
        if story.points is not None and item["points"] == story.points
    ]
    return {
        "available": True,
        "rank": rank,
        "n": len(ordered),
        "faster": max(0, rank - 1),
        "slower": max(0, len(ordered) - rank),
        "percentile": round(rank / len(ordered), 4),
        "median_hours": _median([item["hours"] for item in ordered]),
        "points": story.points,
        "points_n": len(same_points),
        "points_median_hours": _median(same_points),
        "censored": False,
        # prd/57: "73 h" read as "4.1× the 30 h median" — same cohort, same clock.
        "contrast": benchmark.contrast(
            current, [item["hours"] for item in ordered],
            basis=CYCLE_BENCHMARK_BASIS,
        ),
        "basis": (
            "rank = 1 + completed stories with a shorter measured created → done "
            "cycle; the cohort is every completed story in the store (open "
            "stories are censored and excluded)"
        ),
    }


# --------------------------------------------------------------------------- #
# benchmark contrast (prd/57): one cohort per per-item duration the page shows
# --------------------------------------------------------------------------- #
def benchmark_cohorts(court: ballcourt.BallCourt, data: jira.JiraData | None) -> dict:
    """Every cohort the ticket page reads, built once per store version (prd/57).

    * ``stories`` — completed stories' cycles (rank + cycle contrast);
    * ``segments`` — per-segment hours over the same completed stories;
    * ``phases`` — per-phase hours over merged PRs (the PR drawer);
    * ``pr_cycles`` — merged PRs' created → merged hours.

    The server caches this block in the per-store analysis bundle, so a new
    ticket key never re-derives 4,000 stories' segments.
    """
    data = data or jira.EMPTY
    stories = jira.story_cycle_cohort(court, data)
    return {
        "stories": stories,
        "segments": jira.segment_cohort(court, data),
        "phases": ballcourt.phase_cohort(court),
        "pr_cycles": ballcourt.pr_cycle_cohort(court),
    }


def _attach_benchmarks(segments: dict, prs: list[dict], cohorts: dict) -> None:
    """Attach ``median`` + ``multiple`` to each per-item duration (prd/57)."""
    for row in segments.get("rows") or ():
        if row.get("hours") is None:
            continue
        row["benchmark"] = benchmark.contrast(
            row["hours"], cohorts["segments"].get(row["key"], ()),
            basis=SEGMENT_BENCHMARK_BASIS,
        )
    for pr in prs:
        # A censored PR has no completed cycle; the contrast is suppressed with
        # its reason rather than comparing a running age to merged PRs.
        # prd/63 B10: a declined/abandoned PR is not delivered work either. Its
        # span can hit the window cap (DEL-100 #2: 504 h = 63 business days) and
        # benchmarking that against the merged-PR median invented a 20× "slow".
        declined = str(pr.get("state") or "").lower() != "merged" and not pr.get("censored")
        if declined:
            block = benchmark.contrast(
                None, cohorts["pr_cycles"], basis=ballcourt.PR_CYCLE_BASIS,
            )
            block.update({
                "declined": True,
                "available": False,
                "median": None,
                "multiple": None,
                "note": (
                    "declined PR — closed without merging, so it is not "
                    "benchmarked against merged PRs"
                ),
            })
            pr["cycle_benchmark"] = block
            # The phases of an abandoned PR are real spans, but the merged-PR
            # phase cohort is not their comparison. Suppress the contrast too.
            pr["phase_benchmarks"] = {
                phase: {
                    "available": False,
                    "median": None,
                    "multiple": None,
                    "n": len(cohorts["phases"].get(phase, ())),
                    "min_n": benchmark.BENCHMARK_MIN_N,
                    "basis": ballcourt.PHASE_BENCHMARK_BASIS,
                    "declined": True,
                    "note": "declined PR — not benchmarked against merged PRs",
                }
                for phase in (pr.get("phase_hours") or {})
            }
            continue
        pr["cycle_benchmark"] = benchmark.contrast(
            None if pr.get("censored") else pr.get("cycle_hours"),
            cohorts["pr_cycles"], basis=ballcourt.PR_CYCLE_BASIS,
        )
        pr["phase_benchmarks"] = {
            phase: benchmark.contrast(
                hours, cohorts["phases"].get(phase, ()),
                basis=ballcourt.PHASE_BENCHMARK_BASIS,
            )
            for phase, hours in (pr.get("phase_hours") or {}).items()
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
    # prd/70 F-03: one canonical ticket per PR. A PR that names an old done
    # story next to the one it is actually working (#11096) is not "Done in
    # Jira, PR open" — that is the *open* story's PR; listing it here also made
    # the panel label it with the key Aging did not use.
    kind_by_key, state_by_key = ballcourt.ticket_maps(data)
    rows_by_id = {
        row["object_id"]: row for row in ((delivery or {}).get("rows") or [])
    }
    out: list[dict] = []
    for state in court.prs:
        pr = state.pr
        if pr["closed_ts"] is not None:
            continue
        canonical = ballcourt.primary_ticket(
            pr.get("ticket_keys") or (),
            kind_by_key=kind_by_key,
            state_by_key=state_by_key,
        )
        for key in pr.get("ticket_keys") or ():
            story = by_key.get(key)
            if key != canonical or key not in done or story is None:
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
                # prd/70 F-09: the same queue attribution Aging and the ticket
                # page print; without it this panel's "who owes" fell back to a
                # dash on a row Delivery labels "review queue".
                "holder_label": row.get("holder_label"),
                "holder_role": row.get("holder_role"),
                "ball_label": row.get("ball_label"),
                "ball": row.get("ball"),
                "waiting_roles": dict(row.get("waiting_roles") or {}),
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
    has_prs = bool(court.prs)
    if not has_prs:
        unavailable = (
            "unavailable: this dataset holds no pull requests, so whether a "
            "Done ticket's code was merged cannot be checked here"
        )
    elif not data.stories:
        unavailable = (
            "unavailable: this dataset holds no Jira stories, so there is no "
            "Done ticket to check against the PRs"
        )
    else:
        unavailable = None
    return {
        # prd/68 F-10: the panel joins both layers. A Jira-only store has 0 PRs,
        # and an empty row list there is *unmeasured*, not "every ticket has its
        # code merged".
        "available": unavailable is None,
        "count": len(out),
        "opened_after_close": sum(1 for row in out if row["opened_after_close"]),
        "rows": out,
        "unavailable_reason": unavailable,
        "note": unavailable or (
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
          key: str, epic: dict, cohorts: dict | None = None) -> dict:
    """A minimal epic page: the parent, its children and why it has no cycle.

    An epic is a grouping, not a work item: it has no created → done cycle of
    its own, and rendering one as "0/9 segments" made the product look broken.
    The page now says what it is and lists the child stories with their own
    cycles, so the epic is a doorway rather than a dead end.

    prd/57: a child's completed cycle carries the same cohort contrast as a
    ticket's, so the epic list is read against the store, not in a vacuum.
    """
    children = [
        story for story in data.stories
        if (story.epic_key or "") == key
    ]
    cycles = [row["hours"] for row in (cohorts or {}).get("stories") or ()]
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
            "cycle_benchmark": benchmark.contrast(
                None if done is None else hours, cycles,
                basis=CYCLE_BENCHMARK_BASIS,
            ),
        })
    child_rows.sort(key=lambda row: (row["cycle_hours"] is None, row["cycle_hours"] or 0, row["key"]))
    story_count = len(child_rows)
    open_count = sum(1 for row in child_rows if row["censored"])
    # prd/70 F-03: a PR that names *only* an epic key has no story page to
    # link to, so its key is the epic's — and the epic page must then list it
    # instead of being the dead end the QA run found (0 PRs for DEMO-56715
    # while #10865 and #10803 named it). The join is the store's own
    # (`PR.ticket_keys`), the same one a story page uses.
    prs = _pr_rows(court, _pr_states(court, key))
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
        "prs": prs,
        "pr_count": len(prs),
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
            + (
                f" {len(prs)} pull request{' names' if len(prs) == 1 else 's name'}"
                f" this epic directly."
                if prs else ""
            )
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


def _quirk_note(segments: dict) -> str:
    """One sentence naming every out-of-order boundary as a data quirk (prd/46 §1)."""
    quirks = segments.get("out_of_order") or []
    order = segments.get("boundary_quirks") or []
    if not quirks and not order:
        return ""
    if not quirks and order:
        # prd/63 F17: a boundary dated before its lifecycle predecessor is the
        # same kind of data quirk, even when the segment clamp did not move it.
        phrases = [
            f"the {item['boundary']} boundary follows {item.get('after')} on the "
            "lifecycle but is dated earlier"
            for item in order
        ]
        return (
            " Data quirk: " + "; ".join(phrases)
            + " — the events are shown in clock order with the flag, not "
            "reordered, and this is not a finding."
        )
    phrases = []
    for quirk in quirks:
        reason = quirk.get("reason")
        if reason == "before_cycle":
            phrase = f"the {quirk['boundary']} boundary ({quirk['ts'][:10]}) precedes created"
        elif reason == "after_cycle":
            phrase = f"the {quirk['boundary']} boundary ({quirk['ts'][:10]}) follows done"
        else:
            phrase = (
                f"the {quirk['boundary']} boundary ({quirk['ts'][:10]}) "
                f"precedes {quirk.get('precedes') or 'its predecessor'}"
            )
        phrases.append(phrase)
    return (
        " Data quirk: " + "; ".join(phrases) +
        f" — clamped to the cycle ({', '.join(sorted({q['boundary'] for q in quirks}))}), "
        "so the segment shares sum to at most 100 % and this is not a finding."
    )


def _open_pr_state(open_prs: list[dict]) -> str:
    """The open PRs' current wait and approval state, in one clause (prd/55 §1).

    A ticket headline that stops at "created → ready 35 %" hides the fact that
    most of the cycle is *unmeasured* and that the missing part is one PR
    waiting on a named person. This is the clause that names it:
    ``PR #60 (1/2 approved · waiting 16 d 3 h on FrMo)``.
    """
    parts = []
    for pr in open_prs:
        bits = []
        approved = pr.get("reviewers_approved")
        total = pr.get("reviewers_total")
        if total:
            bits.append(f"{approved or 0}/{total} approved")
        if pr.get("waiting_hours") is not None:
            who = (
                " + ".join(pr.get("waiting_on") or [])
                or pr.get("holder_label")
                or ballcourt.REVIEW_QUEUE
            )
            bits.append(
                f"waiting {jira.human_hours(pr['waiting_hours'])} on {who}"
            )
        detail = f" ({' · '.join(bits)})" if bits else ""
        parts.append(f"PR #{pr['number']}{detail}")
    return " · ".join(parts)


def _change_note(change: dict | None) -> str:
    """The unmerged share of the change, as one clause (prd/63 V-A6)."""
    change = change or {}
    share = change.get("unmerged_share")
    if not change.get("available") or not share:
        return ""
    return (
        f" {round(share * 100)} % of the story's change "
        f"({change.get('lines_unmerged', 0)} of {change.get('lines_total', 0)} "
        "changed lines) is in an unmerged PR, so this is not a shipped change."
    )


def _opened_after_close_note(prs: list[dict]) -> str:
    """Name every PR opened after the ticket closed (prd/64 F-14).

    The Overview/Delivery done-open panel already calls the PR opened after
    Jira closed the ticket "a stronger finding: the work cannot have been part
    of the delivered story". The ticket page must say the same thing, with the
    same business-hour duration, so the summary row and the page it links to
    cannot disagree.
    """
    parts = [
        f"PR #{pr['number']} opened "
        f"{jira.human_hours(pr.get('opened_after_close_hours'))} after the ticket "
        "closed — the work cannot have been part of the delivered story."
        for pr in prs if pr.get("opened_after_close")
    ]
    return (" " + " ".join(parts)) if parts else ""


def _approved_unmerged_note(prs: list[dict]) -> str:
    """Name every approved-but-never-merged PR, with its lost hours (F-15).

    The classification is the ``approved_unmerged`` one ``analysis/jira.py``
    derives for Rules and Sprint-at-risk; this only renders the same finding on
    the ticket page, with the same business-hour clock. When the store cannot
    name the closer the sentence says so rather than inventing a bot.
    """
    parts = []
    for pr in prs:
        finding = pr.get("approved_unmerged")
        if not finding:
            continue
        who = " + ".join(finding.get("approved_by") or ()) or "every reviewer"
        lost = jira.human_hours(finding.get("hours"))
        closer = finding.get("closed_by")
        if closer:
            by = f"the bot {closer}" if finding.get("bot_closed") else closer
            close = f"closed unmerged by {by}"
        else:
            close = "closed unmerged after approval"
        parts.append(
            f"PR #{pr['number']} was approved by {who} and {close} — {lost} "
            "lost from the final approval."
        )
    return (" " + " ".join(parts)) if parts else ""


def _answer(
    key: str,
    story: jira.Story | None,
    segments: dict,
    prs: list[dict],
    change: dict | None = None,
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
    # prd/46 §1: an out-of-order boundary is a data quirk, named as one — never
    # left to read as a finding ("most of the cycle in sprint → in progress").
    quirk_note = _quirk_note(segments)
    # prd/36 §3: the end state is stated, and an open PR is disclosed rather
    # than left to a table row. prd/55 §1: the headline also states how much of
    # the cycle is *unmeasured* and what the open PR is waiting on — "the 6 of 9
    # segments ... created → ready 35 %" alone is a misleading answer for an
    # open story whose missing 66 % is one PR parked with a named reviewer.
    explained = segments.get("explained_share")
    unmeasured = (
        round(100 * (1.0 - float(explained)))
        if explained is not None and float(explained) < 1.0 else 0
    )
    pr_state = _open_pr_state(open_prs) if open_prs else ""
    after_close = _opened_after_close_note(prs)
    unmerged_note = _approved_unmerged_note(prs)
    censor_note = ""
    if censored and unmeasured:
        censor_note = f"{unmeasured} % unmeasured"
        if pr_state:
            censor_note += f" · {pr_state}"
        censor_note += ". "
    elif pr_state:
        censor_note = f"{pr_state}. "
    prefix = ""
    if open_prs and not censored:
        numbers = ", ".join(f"#{pr['number']}" for pr in open_prs)
        prefix = (
            f"Done in Jira, but PR {numbers} is still open (right-censored); "
            "the cycle is measured to the ticket's done, not to that PR."
        )
        prefix += after_close
        prefix += _change_note(change)
        if censor_note:
            prefix += f" {censor_note}"
        if not prefix.endswith(" "):
            prefix += " "
    elif open_prs and censored:
        numbers = ", ".join(f"#{pr['number']}" for pr in open_prs)
        prefix = f"Still open in Jira, with PR {numbers} open too (right-censored)."
        prefix += _change_note(change) + " "
        if censor_note:
            prefix += censor_note
    elif censored and unmeasured:
        prefix = f"{unmeasured} % of the cycle is unmeasured. "
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
                f"{'' if reopens == 1 else 's'}: {jira.human_hours(first_hours)} "
                "from created to the first done, and still open at the snapshot "
                f"({jira.human_hours(final_hours)})."
            )
        else:
            reopen_note = (
                f" {key} was reopened {reopens} time"
                f"{'' if reopens == 1 else 's'}: {jira.human_hours(first_hours)} "
                "from created to the first done and "
                f"{jira.human_hours(final_hours)} to the final done."
            )
    if not measured:
        first = missing[0]
        boundary = first["missing"][0]
        body = (
            f"the store cannot say where {key}'s time went because no "
            f"segment has both boundaries — the {boundary} boundary is missing "
            f"({first['reason']}).{reopen_note}{unmerged_note}{suffix}{quirk_note}"
        )
        return _after_prefix(prefix, body), "unmeasurable"
    total = segments["total_hours"]
    biggest = max(measured, key=lambda row: (row["hours"], row["key"]))
    percent = round(100 * (biggest["share"] or 0))
    # prd/63 V-A7: "verification → done 31 %" was the reopen loop, not the QA
    # stage. Name the loop when it is what the segment mostly contains.
    if (biggest.get("rework_hours") or 0) >= 0.5 * (biggest.get("hours") or 1):
        biggest = dict(biggest)
        biggest["label"] = f"{biggest['label']} (the reopen loop)"
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
            f"segments it can measure cover {jira.human_hours(total)}, most of it "
            f"in {biggest['label']} ({jira.human_hours(biggest['hours'])}, {percent}% "
            "of measured)"
            f"{gap_clause}."
            f"{(' ' + gap_sentence) if gap_sentence else ''}"
            f"{reopen_note}{unmerged_note}{suffix}{quirk_note}"
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
                f"{key} took {jira.human_hours(first_hours)} from created to the "
                "first done, then was reopened and is still open "
                f"({jira.human_hours(final_hours)} to the snapshot)"
            )
        else:
            span = (
                f"{key} took {jira.human_hours(first_hours)} from created to the "
                f"first done and {jira.human_hours(final_hours)} to the final done "
                f"(reopened {reopens} time{'' if reopens == 1 else 's'})"
            )
    else:
        cycle = segments["cycle_hours"]
        span = (
            f"{key} took {jira.human_hours(cycle)} from created to done"
            if cycle is not None else f"{key}'s cycle is the measured window"
        )
    body = (
        f"{span}: most of it went to {biggest['label']} "
        f"({jira.human_hours(biggest['hours'])}, {percent}% of the measured cycle)"
        f"{gap_clause}. {active} of the measured time was active work."
        f"{(' ' + gap_sentence) if gap_sentence else ''}{unmerged_note}{suffix}{quirk_note}"
    )
    # prd/63 V-A6: a Done-in-Jira story with an open PR has a *measured ticket
    # cycle* but a censored change; the kind says so, so the page never calls
    # the delivery "measured".
    kind = "done_with_open_pr" if (open_prs and not censored) else "measured"
    return _after_prefix(prefix, body), kind


# --------------------------------------------------------------------------- #
# the ticket
# --------------------------------------------------------------------------- #
def ticket(
    court: ballcourt.BallCourt,
    data: jira.JiraData | None,
    key: str,
    threads: list[dict] | None = None,
    benchmarks: dict | None = None,
) -> dict | None:
    """The full cycle of one ticket, or ``None`` when the store has never seen it.

    ``None`` is the API's 404: a key with no story *and* no PR is not a ticket
    the store can answer about. A story without PRs (or a PR without a story) is
    a real, partially measured ticket and is returned with the gap named. An
    epic key is answered as an epic (prd/42 P9), never as a story with 0/9
    segments.

    ``threads`` (the review threads from ``rules.load_threads``) lets each PR
    row carry its open-thread count, which the PR drawer shows (prd/42 M6).
    ``benchmarks`` is the pre-built ``benchmark_cohorts`` block (prd/57); the
    server caches it per store version, a direct caller gets it computed here.
    """
    data = data or jira.EMPTY
    wanted = normalize_key(key)
    if not wanted:
        return None
    story = jira.story_for(data, wanted)
    states = _pr_states(court, wanted)
    cohorts = benchmarks or benchmark_cohorts(court, data)
    # prd/42 P9: an epic key is a parent grouping, not a story — even when every
    # PR in the corpus names it as its epic link (DEL-1 has 131).
    epic = (data.epic_objects or {}).get(wanted)
    if story is None and epic is not None:
        return _epic(court, data, wanted, epic, cohorts)
    if story is None and not states:
        return None

    cycle = jira.cycle_for(story) if story is not None else None
    # prd/29: the store's workflow decides whether the merge→done segment is
    # split around verification, so a ticket that never reached QA still shows
    # the QA segment as missing (with the boundary named) instead of silently
    # folding it into "done".
    segments = _segments(court, cycle, verification=jira.uses_verification(data))
    prs = _pr_rows(
        court, states,
        cycle.boundaries.get("done") if cycle is not None else None,
    )
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
    answer, answer_kind = _answer(wanted, story, segments, prs, change=_change(prs))
    benchmark = _benchmark(court, data, story, segments, cohorts)
    # prd/57: each segment and each PR phase carries its cohort median + multiple.
    _attach_benchmarks(segments, prs, cohorts)

    change = _change(prs)
    # prd/63 V-A7: two clocks called "active". The segment efficiency counts
    # measured work in the story's segments (PR intervals *plus* worklog
    # durations and a fixed cost per ticket touch); the worklog panel's
    # "active (PR)" counts only PR intervals with commits in business hours.
    # State both, and why they differ, instead of showing "8 % active" next to
    # "active (PR) 0 h".
    story_active = {
        "segment_hours": segments.get("active_hours"),
        "pr_hours": worklog.get("active_hours"),
        "worklog_hours": worklog.get("logged_hours"),
        "efficiency": segments.get("efficiency"),
        "note": (
            "the segment figure counts measured work inside the story: PR "
            "intervals where the author was working, worklog durations, and a "
            "fixed attention cost per ticket change; the PR-clock figure counts "
            "only PR intervals with commits inside business hours. A push after "
            "hours is real work but prices 0 h on the PR clock."
        ),
    }

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
            # prd/55 §1/§11: the ticket headline and its Markdown export state
            # the open PR's wait and approval position, not only that it exists.
            "waiting_on": list(pr.get("waiting_on") or ()),
            # prd/70 F-09: the shared holder attribution ("review queue" /
            # "ready to merge" when no human holds the ball).
            "holder_label": pr.get("holder_label"),
            "holder_role": pr.get("holder_role"),
            "holder_queue": pr.get("holder_queue"),
            "waiting_since": pr.get("waiting_since"),
            "waiting_hours": pr.get("waiting_hours"),
            "reviewers_approved": pr.get("reviewers_approved"),
            "reviewers_total": pr.get("reviewers_total"),
            "reviewers_pending": pr.get("reviewers_pending"),
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
        # prd/62: this story's four-step pass — refined / developed / reviewed /
        # verified, with the signals whose absence made each step clean.
        "yield": jira.story_yield(court, story),
        "segments": segments,
        "prs": prs,
        "pr_count": len(prs),
        # prd/63 V-A6/A7: the unmerged share of the change and the two active
        # clocks, so a headline cannot brag over unshipped work or leave two
        # "active" numbers unexplained.
        "change": change,
        "active": story_active,
        # prd/63 F17/V-B: a visible quirk flag, not only prose.
        "quirk": {
            "count": len(segments.get("boundaries_clamped") or ())
                + len(segments.get("boundary_quirks") or ()),
            "clamped": list(segments.get("boundaries_clamped") or ()),
            "order": list(segments.get("boundary_quirks") or ()),
        },
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

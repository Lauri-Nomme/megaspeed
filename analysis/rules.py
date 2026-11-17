"""Analysis → suggested rules (prd/17): the "what should we change first" layer.

The event log already knows *what happened* (``ballcourt`` reconstructs who held
the ball). This module turns that into the product's headline output: a ranked
list of concrete, plain-language changes, each with its evidence and an
estimated cost in hours.

    events/objects ──► metrics ──► rule templates ──► ranked suggestions
                       (prd/17 §Metrics)  (threshold+severity)

Every metric prd/17 lists is computed from the store, in one pass:

======================  ==========================================================
``time_to_first_review`` first reviewer action (comment/approval) − ``pr_opened``
``cycle_time``           ``pr_merged`` − ``pr_opened`` (median + p90)
``flow_efficiency``      active ÷ elapsed per PR, and the waiting-time split
``survival``             Kaplan–Meier time-to-merge with open/declined censored
``reviewer_responsiveness``  per person: review-ball vs author-ball hours
``rework_loops``         review rounds per PR, split by ``push_class``
``rebase_waste``         approvals lost where the causing push was ``pure_rebase``
``comment_tax``          threads by ``comment_type`` × ``thread_outcome``
``conformance``          merged with 0 approvals; PRs over N rounds
``sentinel_queue``       median wait on the mandatory approval (when roles exist)
``pr_size``              commits per PR (+ diff size when captured) vs review latency
``vacation_blocking``    per vacation: items that waited on the person, no delegation
``stacked_cascade``      prd/22: stack depth (median/max) + the rebases and re-review
                         hours a lower-PR update forced up every stack above it
``waiting_time_decomposition``  prd/21: the 8 ticket segments, hours + efficiency each
``carry_over``           prd/21: stories added to more than one sprint
``point_churn``          prd/21: ``pointed`` events after the story started
``worklog_cross_check``  prd/21: ``log_delay_bucket`` + logged vs active hours
``jira_conformance``     prd/21: started before refined, re-pointed after start
======================  ==========================================================

Every suggestion is ``{id, metric, title, evidence, suggestion, severity,
impact_hours}`` plus the ranking metadata (``rank``, ``confidence``,
``impact_score``, ``sample``, ``scope``). Ranking is **impact hours ×
confidence**, where confidence is a sample-size ramp (``n / 20``, capped at 1).
Person-level rules carry a pseudonym in ``scope`` and never a raw name — the
store cannot hold one.

Thresholds are v1 defaults, stated in the response and in the UI rather than
hidden: 24 h to first review, 72 h to merge, 3 review rounds, 10 commits or 400
changed lines for the size cap, 25% flow efficiency (below 10% is critical), and
one re-review round ≈ the median per-round share of a PR's cycle in the same
window. Flow efficiency credits each activity touch with 0.5 h of attention
rather than the whole wait it ended (see :func:`flow_efficiency`); the constant
is in the payload so the number can be recomputed. Diff size is optional: the
scrape-side capture (prd/16) is a separate task, so until ``PR.attributes``
carries ``lines_added``/``lines_removed`` (or ``diff_size``) the size metric uses
commit count alone and reports ``diff_size_pending: true``.

Censoring is treated properly, not noted away (product review #5): ``cycle_time``
keeps its merged-only median but carries the Kaplan–Meier estimate next to it,
and ``survival`` is the full-population metric (open PRs right-censored at the
snapshot, declined at their close).

Duration assumption: **business hours** when a calendar is attached (prd/19,
``analysis.calendar``), **wall-clock hours** otherwise. Every metric that turns
two timestamps into hours goes through ``court.business_hours(person, t1, t2)``,
so a wait over a weekend or a vacation no longer counts idle days; the response
says which mode answered (``hours_mode``). The calendar is attached by the
server per tenant (``data/<tenant>/calendar.yaml``), never by this module.

Read-only: this module reads the schema directly, like ``ballcourt``, so the
analysis views never depend on ingest refactors.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

from analysis import ballcourt
from analysis import jira as jira_analysis
from analysis import stack as stack_analysis
from analysis.effort import ACTIVE_PHASES, TOUCH_ACTIVITIES, TOUCH_HOURS
from analysis.jira import (
    CARRY_OVER_SHARE_TARGET, POINT_CHURN_SHARE_TARGET, SEGMENT_SHARE_CRITICAL,
    SEGMENT_SHARE_TARGET, WORKLOG_DELAY_TARGET_DAYS, WORKLOG_LATE_SHARE_TARGET,
    WORKLOG_RATIO_HIGH, WORKLOG_RATIO_LOW,
)
from analysis.survival import Observation, kaplan_meier
from ingest.store import iso, to_naive_utc, utcnow

# v1 targets. Stated in every response that uses them (never a hidden constant).
TARGET_FIRST_REVIEW_HOURS = 24.0
TARGET_CYCLE_HOURS = 72.0
MAX_REVIEW_ROUNDS = 3
# Flow efficiency (prd/05): the share of a PR's business-hour lifetime spent in
# active work. 25% is the common "good" threshold for software delivery; below
# 10% the process is almost all queue.
TARGET_FLOW_EFFICIENCY = 0.25
CRITICAL_FLOW_EFFICIENCY = 0.10
# An activity touch (a review comment, an approval, a push) is point-in-time:
# it ends a wait, it does not fill it. Crediting the whole wait it ended would
# make every reviewed PR 100% active, so a touch is worth this fixed attention
# cost instead — stated, reported, and recomputable from the payload.
# ``TOUCH_HOURS`` and the phase/touch vocabularies are shared with the
# ticket-layer segments (prd/21) so the two efficiencies cannot drift; they live
# in ``analysis.effort``.


def _count(n: int, noun: str) -> str:
    """`1 PR` / `2 PRs` — titles are read as sentences, so they agree."""
    return f"{n} {noun}" + ("" if n == 1 else "s")
# A PR is "large" past either cap. 400 changed lines is the common review-size
# guideline; commits are the coarse proxy until diff size is captured.
SIZE_COMMIT_CAP = 10
SIZE_LINE_CAP = 400
# Confidence ramps from 0 to 1 over this many observations (prd/17 ranking).
CONFIDENCE_SAMPLE = 20
DEFAULT_ROUND_HOURS = 24.0

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

REVIEW_ACTIONS = ("comment_added", "pr_approved")

# prd/21: what to change when one segment of the ticket cycle dominates. The
# advice is per segment, because "wait less" is not a rule.
SEGMENT_ADVICE = {
    "ready_refined": (
        "Refine before the sprint, not during it: a story that waits for a "
        "refinement slot is not ready to be committed, so the wait turns into "
        "a carry-over. Refine a story only when the team is ready to size it "
        "and write its acceptance criteria in one sitting."
    ),
    "refined_sprint": (
        "Plan the sprint from the refined backlog: a pointed story that waits "
        "weeks for a sprint is a scheduling problem, not a work problem. Keep "
        "the refined queue short enough that a story enters the next sprint "
        "after it is refined."
    ),
    "sprint_in_progress": (
        "Start committed work on day one. A story that enters a sprint and "
        "then waits is queue, not progress: cap the sprint's work in progress "
        "and pull the next story only when one finishes."
    ),
    "in_progress_pr_open": (
        "Open the PR early (as a draft) as soon as the approach is settled, so "
        "review and CI can start while the work continues. A story in progress "
        "with no PR is invisible to the reviewers who could unblock it."
    ),
    "pr_open_first_review": (
        "Adopt a first-response target: every PR gets a first review inside a "
        "day, and the review queue is triaged each morning. The wait for the "
        "first look, not the review itself, is what this segment measures."
    ),
    "review_rounds": (
        "Cap review rounds: if a PR needs a third pass, the author and reviewer "
        "agree the remaining blockers in one conversation instead of another "
        "comment round. Nits and out-of-scope notes belong on the ticket."
    ),
    "final_approval_merge": (
        "Merge on approval: enable auto-merge, or make merging part of the "
        "reviewer's action. An approved PR that waits is finished work the "
        "team cannot deliver or count."
    ),
    "merge_done": (
        "Close the ticket when the PR merges: the story is done at the merge, "
        "and a stale open ticket distorts every cycle-time number after it."
    ),
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _hours(seconds: float) -> float:
    return round(max(0.0, seconds) / 3600.0, 2)


def _span_hours(court: ballcourt.BallCourt, person: str | None,
                t1: datetime, t2: datetime) -> float:
    """Business hours between two timestamps, for the person who held the ball.

    One helper for every metric that turns a duration into hours, so
    ``hours_mode`` cannot drift between metrics: the calendar is applied in
    exactly one place (``BallCourt.business_hours``) and falls back to elapsed
    time when the tenant has none.
    """
    return _hours(court.business_hours(person, t1, t2) * 3600.0)


def _median(values: list[float], digits: int = 2) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], digits)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, digits)


def _percentile(values: list[float], q: float, digits: int = 2) -> float | None:
    """Nearest-rank percentile: the value at or above ``q`` of the sample."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return round(ordered[index], digits)


def _p90(values: list[float]) -> float | None:
    """Nearest-rank p90: the value at or above 90% of the sample."""
    return _percentile(values, 0.9)


def _confidence(n: int) -> float:
    """Sample-size confidence in [0.05, 1]: a 20-observation metric is trusted."""
    return round(max(0.05, min(1.0, max(0, n) / CONFIDENCE_SAMPLE)), 2)


def _human_hours(hours: float | None) -> str:
    """Plain-language duration for titles: "41 h", "3.4 d"."""
    if hours is None:
        return "—"
    if hours >= 48:
        return f"{round(hours / 24.0, 1):g} d"
    return f"{round(hours, 1):g} h"


def _push_class(event: dict) -> str:
    return (event.get("attributes") or {}).get("push_class") or "other"


def _overlap(interval, start: datetime, end: datetime) -> bool:
    return interval.measured and interval.start < end and interval.end > start


def window_for(
    court: ballcourt.BallCourt,
    days: float | None = None,
    jira: jira_analysis.JiraData | None = None,
) -> tuple[datetime, datetime]:
    """The analysis window: items *created* inside it are the cohort.

    Unlike ``ballcourt.window_from_events`` this includes the earliest PR
    creation, so a PR with no events yet still belongs to an "all time" window.
    ``jira`` extends the earliest edge to the first story: a Jira-only store has
    no PRs and no intervals, so without it "all time" would collapse to the
    snapshot and the ticket metrics would report an empty window on a full
    store.
    """
    first_pr = min((s.pr["created_ts"] for s in court.prs), default=None)
    first_interval = min((i.start for i in court.intervals), default=None)
    first_story = min(
        (story.start for story in (jira.stories if jira else ()) if story.start),
        default=None,
    )
    first = min(
        (t for t in (first_pr, first_interval, first_story) if t is not None),
        default=court.snapshot,
    )
    if not days:
        return first, court.snapshot
    return max(first, court.snapshot - timedelta(days=float(days))), court.snapshot


def cohort(court: ballcourt.BallCourt, window: tuple[datetime, datetime]) -> list:
    """PRs opened in the window — the denominator every metric reports."""
    start, end = window
    return [state for state in court.prs if start <= state.pr["created_ts"] <= end]


def _rounds(state) -> tuple[list[dict], list[dict]]:
    pushes = [e for e in state.events if e["activity"] == "pr_pushed"]
    unapprovals = [e for e in state.events if e["activity"] == "pr_unapproved"]
    return pushes, unapprovals


def _first_review(state) -> dict | None:
    """The first reviewer action on a PR: comment or approval, never the author."""
    pr = state.pr
    opened = pr["created_ts"]
    return next(
        (
            event for event in state.events
            if event["_ts"] >= opened
            and event["activity"] in REVIEW_ACTIONS
            and event["actor"] != pr["actor"]
        ),
        None,
    )


def round_cost_hours(court: ballcourt.BallCourt, window: tuple[datetime, datetime]) -> float:
    """What one extra review round costs, in elapsed hours.

    A rejected thread or a lost approval sends the PR back through review *and*
    back to the author. The cheapest honest estimate from this store is the
    median per-round share of a PR's measured cycle (``cycle ÷ its rounds``);
    when no PR has closed yet it falls back to the median review-ball wait, then
    to a stated default. The basis is reported next to the number.
    """
    start, end = window
    per_round: list[float] = []
    for state in cohort(court, window):
        pr = state.pr
        if pr["closed_ts"] is None:
            continue
        pushes, unapprovals = _rounds(state)
        rounds = len(pushes) + len(unapprovals)
        if rounds <= 0:
            continue
        per_round.append(
            _span_hours(court, None, pr["created_ts"], pr["closed_ts"]) / rounds
        )
    if per_round:
        return max(1.0, _median(per_round) or DEFAULT_ROUND_HOURS)
    waits = [
        i.hours for i in court.intervals
        if i.ball == ballcourt.REVIEW and _overlap(i, start, end)
    ]
    return max(1.0, _median(waits) or DEFAULT_ROUND_HOURS)


ROUND_COST_BASIS = "median PR cycle time ÷ its review rounds"


# --------------------------------------------------------------------------- #
# store readers
# --------------------------------------------------------------------------- #
THREAD_SQL = (
    "SELECT object_id, repo, pr_id, state, created_at, closed_at,"
    " CAST(attributes AS VARCHAR) AS attrs FROM objects WHERE object_type = 'THREAD'"
    " ORDER BY object_id"
)
PR_SIZE_SQL = (
    "SELECT object_id, CAST(attributes AS VARCHAR) AS attrs"
    " FROM objects WHERE object_type = 'PR' ORDER BY object_id"
)


def _has_attributes(con) -> bool:
    """Older stores predate ``objects.attributes``; reads must degrade, not 503."""
    rows = con.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = 'main' AND table_name = 'objects'"
    ).fetchall()
    return any(row[0] == "attributes" for row in rows)


def load_threads(con) -> list[dict]:
    """Review threads with their closed labels (prd/16): type + outcome.

    Only ``comment_type`` / ``thread_outcome`` make the "comment tax" metric
    possible; a store ingested before the text classifier has neither, and the
    metric reports ``available: false`` rather than inventing a label.
    """
    if not _has_attributes(con):
        return []
    out: list[dict] = []
    for object_id, repo, pr_id, state, created, closed, attrs in con.execute(THREAD_SQL).fetchall():
        attributes = json.loads(attrs) if attrs else {}
        out.append({
            "object_id": object_id,
            "repo": repo,
            "pr_id": pr_id,
            "state": state,
            "created_at": to_naive_utc(created) if created else None,
            "closed_at": to_naive_utc(closed) if closed else None,
            "comment_type": attributes.get("comment_type"),
            "thread_outcome": attributes.get("thread_outcome"),
            "severity": attributes.get("severity"),
            "anchor_class": attributes.get("anchor_class"),
        })
    return out


def load_bots(con) -> set[str]:
    """Pseudonyms that are bots (``actor_kind`` on the PERSON object).

    A bot's review queue is not a person to coach, so person-level rules skip
    them; the metric still counts their waits, because a wait is a wait.
    """
    if not _has_attributes(con):
        return set()
    rows = con.execute(
        "SELECT object_id, CAST(attributes AS VARCHAR) FROM objects WHERE object_type = 'PERSON'"
    ).fetchall()
    bots: set[str] = set()
    for object_id, attrs in rows:
        attributes = json.loads(attrs) if attrs else {}
        if attributes.get("actor_kind") == "bot":
            bots.add(str(object_id).partition(":")[2])
    return bots


def _diff_size(attributes: dict) -> int | None:
    """Changed lines: ``lines_added + lines_removed`` or a combined ``diff_size``.

    Both are optional (the scrape-side capture is a separate task); a missing
    value is ``None``, never an invented zero.
    """
    added = attributes.get("lines_added")
    removed = attributes.get("lines_removed")
    if (
        isinstance(added, int) and not isinstance(added, bool)
        and isinstance(removed, int) and not isinstance(removed, bool)
    ):
        return max(0, added) + max(0, removed)
    size = attributes.get("diff_size")
    if isinstance(size, int) and not isinstance(size, bool):
        return max(0, size)
    return None


def load_pr_sizes(con) -> dict[str, dict]:
    """``PR object_id -> {diff_size, lines_added, lines_removed}`` when present.

    Commit counts come from the COMMIT objects (``ballcourt`` already groups
    them); this reader only supplies the optional diff size.
    """
    if not _has_attributes(con):
        return {}
    out: dict[str, dict] = {}
    for object_id, attrs in con.execute(PR_SIZE_SQL).fetchall():
        attributes = json.loads(attrs) if attrs else {}
        diff_size = _diff_size(attributes)
        if diff_size is None and attributes.get("lines_added") is None:
            continue
        out[object_id] = {
            "diff_size": diff_size,
            "lines_added": attributes.get("lines_added"),
            "lines_removed": attributes.get("lines_removed"),
        }
    return out


# --------------------------------------------------------------------------- #
# metrics (prd/17 §Metrics) — each one is independently testable
# --------------------------------------------------------------------------- #
def time_to_first_review(court: ballcourt.BallCourt, window) -> dict:
    """``min(first reviewer comment/approval) − pr_opened`` per PR."""
    samples: list[float] = []
    by_reviewer: dict[str, list[float]] = defaultdict(list)
    missing = 0
    for state in cohort(court, window):
        pr = state.pr
        first = _first_review(state)
        if first is None:
            missing += 1
            continue
        hours = _span_hours(court, first["actor"], pr["created_ts"], first["_ts"])
        samples.append(hours)
        by_reviewer[first["actor"]].append(hours)
    return {
        "n": len(samples),
        "missing": missing,
        "median_hours": _median(samples),
        "p90_hours": _p90(samples),
        "target_hours": TARGET_FIRST_REVIEW_HOURS,
        "over_target": sum(1 for h in samples if h > TARGET_FIRST_REVIEW_HOURS),
        "by_reviewer": {
            person: {"n": len(values), "median_hours": _median(values)}
            for person, values in sorted(by_reviewer.items())
        },
    }


def cycle_time(court: ballcourt.BallCourt, window) -> dict:
    """``pr_merged.ts − pr_opened.ts`` for PRs merged in the cohort.

    The median/p90 are **conditional on merging**: an open PR has no end yet, so
    it cannot enter the sample, and a declined PR never will. Dropping them
    silently is exactly the censoring bias prd/05 warns about (product review
    #5), so the metric reports the excluded population next to the number:

    * ``censored.open`` — still running at the snapshot (right-censored): their
      current age is a lower bound on the cycle time they will have;
    * ``censored.declined`` — closed without merging: excluded by definition,
      not censored.

    ``n`` stays the merged sample the median is computed over; ``cohort`` is the
    denominator. Nothing here pretends the median is a full-population number.
    """
    samples: list[float] = []
    open_ages: list[float] = []
    declined = 0
    states = cohort(court, window)
    for state in states:
        pr = state.pr
        if pr["closed_ts"] is None:
            open_ages.append(_span_hours(court, None, pr["created_ts"], court.snapshot))
            continue
        if pr["state"] != "merged":
            declined += 1
            continue
        samples.append(_span_hours(court, None, pr["created_ts"], pr["closed_ts"]))
    return {
        "n": len(samples),
        "median_hours": _median(samples),
        "p90_hours": _p90(samples),
        "target_hours": TARGET_CYCLE_HOURS,
        "over_target": sum(1 for h in samples if h > TARGET_CYCLE_HOURS),
        "cohort": len(states),
        "censored": {
            "open": len(open_ages),
            "declined": declined,
            "open_median_age_hours": _median(open_ages),
            "open_over_target": sum(1 for h in open_ages if h > TARGET_CYCLE_HOURS),
            "note": (
                "median/p90 are over merged PRs only; open PRs are right-censored "
                "(still running) and declined PRs never merge, so neither is in "
                "the sample — metrics.survival is the censoring-aware estimate"
            ),
        },
        # product review #5: the proper estimate travels next to the merged-only
        # one, so a reader cannot quote the flattering number alone.
        "survival_median_hours": time_to_merge(court, window)["median_hours"],
        "survival_estimator": "Kaplan–Meier",
    }


def _within(stamp: datetime, spans: list[tuple[datetime, datetime]]) -> bool:
    """Is ``stamp`` inside one of ``spans`` (inclusive, so a boundary counts)?"""
    return any(start <= stamp <= end for start, end in spans)


def flow_efficiency(court: ballcourt.BallCourt, window) -> dict:
    """Flow efficiency (prd/05): active ÷ elapsed, per PR and for the process.

    The waiting-time decomposition asks one blunt question — of a PR's lifetime,
    how much was *work* and how much was *queue*? The interval model already
    answers it per phase, so this metric only has to classify:

    * **active** — the author is demonstrably working: an ``active`` interval
      (commits inside it, a content push, an author reply) or a ``rebasing``
      one (a rebase is work, even when it is rework — reported separately as
      ``rework_hours``);
    * **waiting** — everything else: ``review``, ``ci``, an author holding the
      ball with no commits, and the unobserved tail of an open PR (no events
      since; the honest label for "nothing happened").

    Reviewer comments and approvals are **point-in-time touches**, not
    durations: they end a wait, they do not fill it. Crediting the wait a touch
    ended would make every reviewed PR ~100% active, so a touch that is not
    already inside measured work is credited with :data:`TOUCH_HOURS` of
    attention instead. Both numbers travel in the payload
    (``active_hours`` + ``touch_hours`` = ``working_hours``), so a reader who
    disagrees with the constant can recompute the headline from the same facts.

    ``efficiency`` is per PR; ``aggregate_efficiency`` is the process number
    (Σ working ÷ Σ elapsed) and is what a long PR pulls down. ``elapsed`` is the
    sum of the PR's measured intervals — business hours when a calendar is
    attached, so numerator and denominator share one clock and active can never
    exceed elapsed by construction.
    """
    states = cohort(court, window)
    by_pr: dict[str, list] = defaultdict(list)
    for interval in court.intervals:
        by_pr[interval.object_id].append(interval)

    rows: list[dict] = []
    efficiencies: list[float] = []
    measured: list[float] = []
    elapsed_all: list[float] = []
    active_all: list[float] = []
    waiting_all: list[float] = []
    total = {"elapsed": 0.0, "active": 0.0, "touch": 0.0, "working": 0.0,
             "waiting": 0.0, "rework": 0.0}
    waiting_by_phase: dict[str, float] = defaultdict(float)
    touches_total = 0
    credited_total = 0
    clamped = 0
    active_intervals = 0

    for state in states:
        pr = state.pr
        intervals = by_pr.get(pr["object_id"], [])
        elapsed = sum(i.hours for i in intervals)
        if not intervals:                      # no events at all: the age is all we know
            elapsed = _span_hours(
                court, None, pr["created_ts"], pr["closed_ts"] or court.snapshot
            )
        active = 0.0
        rework = 0.0
        spans: list[tuple[datetime, datetime]] = []
        for interval in intervals:
            if interval.phase in ACTIVE_PHASES:
                active += interval.hours
                active_intervals += 1
                spans.append((interval.start, interval.end))
                if interval.phase == "rebasing":
                    rework += interval.hours
            else:
                waiting_by_phase[interval.phase] += interval.hours
        touches = [
            event for event in state.events
            if event["activity"] in TOUCH_ACTIVITIES and event["_ts"] >= pr["created_ts"]
        ]
        credited = [event for event in touches if not _within(event["_ts"], spans)]
        touch_hours = len(credited) * TOUCH_HOURS
        working = active + touch_hours
        if working > elapsed:
            working = elapsed
            clamped += 1
        waiting = max(0.0, elapsed - active)
        efficiency = round(working / elapsed, 4) if elapsed > 0 else None

        total["elapsed"] += elapsed
        total["active"] += active
        total["touch"] += touch_hours
        total["working"] += working
        total["waiting"] += waiting
        total["rework"] += rework
        touches_total += len(touches)
        credited_total += len(credited)
        if elapsed > 0 and efficiency is not None:
            efficiencies.append(efficiency)
            measured.append(round(active / elapsed, 4))
            elapsed_all.append(elapsed)
            active_all.append(active)
            waiting_all.append(waiting)

        rows.append({
            "object_id": pr["object_id"],
            "id": f"{pr['repo'].split('/')[-1]}#{pr['pr_id']}",
            "repo": pr["repo"],
            "number": pr["pr_id"],
            "author": pr["actor"],
            "state": pr["state"] if pr["closed_ts"] else "open",
            "elapsed_hours": round(elapsed, 2),
            "active_hours": round(active, 2),
            "rework_hours": round(rework, 2),
            "touch_hours": round(touch_hours, 2),
            "working_hours": round(working, 2),
            "waiting_hours": round(waiting, 2),
            "efficiency": efficiency,
            "measured_efficiency": (
                round(active / elapsed, 4) if elapsed > 0 else None
            ),
            "touches": len(touches),
            "credited_touches": len(credited),
            "commits": sum(i.commits for i in intervals),
        })

    rows.sort(key=lambda row: (
        row["efficiency"] if row["efficiency"] is not None else 2.0, row["object_id"]
    ))
    elapsed_total = total["elapsed"]
    aggregate = round(total["working"] / elapsed_total, 4) if elapsed_total > 0 else None
    by_state: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row["efficiency"] is not None:
            by_state[row["state"]].append(row["efficiency"])
    waiting_total = sum(waiting_by_phase.values())
    return {
        "available": active_intervals > 0,
        "n": len(efficiencies),
        "median_efficiency": _median(efficiencies, 4),
        "mean_efficiency": (
            round(sum(efficiencies) / len(efficiencies), 4) if efficiencies else None
        ),
        "p10_efficiency": _percentile(efficiencies, 0.1, 4),
        "p90_efficiency": _percentile(efficiencies, 0.9, 4),
        "aggregate_efficiency": aggregate,
        "measured_median_efficiency": _median(measured, 4),
        "elapsed_hours": round(elapsed_total, 1),
        "active_hours": round(total["active"], 1),
        "rework_hours": round(total["rework"], 1),
        "touch_hours": round(total["touch"], 1),
        "working_hours": round(total["working"], 1),
        "waiting_hours": round(total["waiting"], 1),
        "median_elapsed_hours": _median(elapsed_all),
        "median_active_hours": _median(active_all),
        "median_waiting_hours": _median(waiting_all),
        "touches": touches_total,
        "credited_touches": credited_total,
        "touch_hours_per_touch": TOUCH_HOURS,
        "waiting_breakdown": {
            phase: {
                "hours": round(waiting_by_phase.get(phase, 0.0), 1),
                "share": (
                    round(waiting_by_phase.get(phase, 0.0) / waiting_total, 2)
                    if waiting_total > 0 else 0.0
                ),
            }
            for phase in ("review", "ci", "waiting", "unobserved", "closed")
        },
        "by_state": {
            state_name: {
                "n": len(values),
                "median_efficiency": _median(values, 4),
            }
            for state_name, values in sorted(by_state.items())
        },
        "clamped": clamped,
        "target_efficiency": TARGET_FLOW_EFFICIENCY,
        "critical_efficiency": CRITICAL_FLOW_EFFICIENCY,
        "heuristic": (
            "active = intervals where the ball-holder left evidence of work "
            "(commits, a push, a rebase, an author reply) plus "
            f"{TOUCH_HOURS} h per activity touch (comment/approval/needs-work) "
            "that ends a queue wait; everything else — review, CI, an idle "
            "author, the unobserved tail — is waiting"
        ),
        "elapsed_basis": (
            "sum of the PR's measured intervals, in business hours when a "
            "calendar is attached"
        ),
        "accounting": (
            "working = active + touch; waiting is the measured queue time, so "
            "working + waiting = elapsed + touch_hours — a touch credit is "
            "attention spent inside time that is otherwise waiting, so it is "
            "added to active without being removed from the queue it interrupted"
        ),
        "prs": rows,
    }


def time_to_merge(court: ballcourt.BallCourt, window) -> dict:
    """Kaplan–Meier time-to-merge (prd/05, goal 7) over the opened cohort.

    ``cycle_time`` can only average the PRs that merged; this metric keeps the
    ones that did not as **right-censored** observations (open PRs at the
    snapshot, declined PRs at their close time) and fits the survival curve, so
    the median is the full-population estimate instead of a merged-only one.

    Reported next to it, never instead of it:

    * ``merged_only_median_hours`` — the old number, for comparison;
    * ``understatement_hours`` — how much longer the survival median is;
    * ``censored`` — how many observations were censored, their ages, and the
      excess wait of the open PRs already past the cycle-time target (the
      population a merged-only median cannot see);
    * ``quantiles`` and a small ``curve`` — enough to draw the estimate.

    ``hours_mode`` is the court's: business hours with a calendar, wall-clock
    without one, and the same ``_span_hours`` helper every other metric uses.
    """
    states = cohort(court, window)
    observations: list[Observation] = []
    merged: list[float] = []
    open_ages: list[float] = []
    open_excess = 0.0
    declined = 0
    for state in states:
        pr = state.pr
        if pr["closed_ts"] is None:
            hours = _span_hours(court, None, pr["created_ts"], court.snapshot)
            open_ages.append(hours)
            open_excess += max(0.0, hours - TARGET_CYCLE_HOURS)
            observations.append(Observation(hours, False, pr["object_id"]))
            continue
        hours = _span_hours(court, None, pr["created_ts"], pr["closed_ts"])
        if pr["state"] == "merged":
            merged.append(hours)
            observations.append(Observation(hours, True, pr["object_id"]))
        else:
            declined += 1
            observations.append(Observation(hours, False, pr["object_id"]))

    fit = kaplan_meier(observations)
    merged_median = _median(merged)
    understatement = (
        round(fit["median_hours"] - merged_median, 2)
        if fit["median_hours"] is not None and merged_median is not None
        else None
    )
    understatement_share = (
        round(fit["median_hours"] / merged_median - 1.0, 3)
        if fit["median_hours"] is not None and merged_median
        else None
    )
    return {
        "estimator": "Kaplan–Meier",
        "n": fit["n"],
        "events": fit["events"],
        "cohort": len(states),
        "median_hours": fit["median_hours"],
        "median_reached": fit["median_reached"],
        "quantiles": fit["quantiles"],
        "curve": fit["curve"],
        "merged_only_median_hours": merged_median,
        "merged_only_p90_hours": _p90(merged),
        "understatement_hours": understatement,
        "understatement_share": understatement_share,
        "target_hours": TARGET_CYCLE_HOURS,
        "events_over_target": sum(1 for h in merged if h > TARGET_CYCLE_HOURS),
        "censored": {
            "open": len(open_ages),
            "declined": declined,
            "total": len(open_ages) + declined,
            "open_median_age_hours": _median(open_ages),
            "open_over_target": sum(1 for h in open_ages if h > TARGET_CYCLE_HOURS),
            "oldest_open_age_hours": round(max(open_ages), 2) if open_ages else None,
            "open_excess_hours": round(open_excess, 2),
            "note": (
                "open PRs are right-censored at the snapshot (their age is a "
                "lower bound on the merge time they will have); declined PRs "
                "are censored at their close time"
            ),
        },
        "note": fit["note"],
    }


def reviewer_responsiveness(court: ballcourt.BallCourt, window) -> dict:
    """Per person: hours the ball sat in *their* review queue vs on their PRs.

    Review-ball hours are attributed to **every reviewer the ball was
    concurrently with** — the pending roster from ``ballcourt`` (product review
    #7) — not only to whoever happened to answer first. A reviewer who never
    responds therefore accumulates the waits they held instead of disappearing
    behind the first responder; the responder is included even when the store
    has no roster for them. Author-ball hours are the intervals where the person
    held the ball on their own PR. "Prefers own work" is the contrast: review
    latency high while their own cycle is low.
    """
    start, end = window
    review: dict[str, list[float]] = defaultdict(list)
    author: dict[str, list[float]] = defaultdict(list)
    for interval in court.intervals:
        if not _overlap(interval, start, end):
            continue
        if interval.ball == ballcourt.REVIEW:
            for person in interval.holders:
                if person in (ballcourt.REVIEW, ballcourt.CI, ballcourt.CALENDAR):
                    continue  # an unattributed queue wait is not a person's
                review[person].append(interval.hours)
        elif interval.ball == "author":
            author[interval.author].append(interval.hours)

    people = []
    for person in sorted(set(review) | set(author)):
        review_hours = review.get(person, [])
        author_hours = author.get(person, [])
        review_median = _median(review_hours)
        author_median = _median(author_hours)
        flagged = bool(
            review_median is not None
            and review_median >= TARGET_FIRST_REVIEW_HOURS
            # a contrast needs both sides; without author-ball data this is a
            # pure reviewer, and blaming them would be an artefact of the data
            and author_median is not None
            and author_median * 2 <= review_median
        )
        people.append({
            "person": person,
            "review_n": len(review_hours),
            "review_median_hours": review_median,
            "author_n": len(author_hours),
            "author_median_hours": author_median,
            "flagged": flagged,
        })

    review_medians = [p["review_median_hours"] for p in people if p["review_median_hours"] is not None]
    all_review = [h for values in review.values() for h in values]
    all_author = [h for values in author.values() for h in values]
    return {
        "people": people,
        "flagged": [p["person"] for p in people if p["flagged"]],
        "reviewers": len(review),
        "review_n": len(all_review),
        "review_median_hours": _median(all_review),
        "author_n": len(all_author),
        "author_median_hours": _median(all_author),
        "spread_hours": (
            round(max(review_medians) - min(review_medians), 2)
            if len(review_medians) >= 2 else None
        ),
    }


def rework_loops(court: ballcourt.BallCourt, window) -> dict:
    """Review rounds per PR (``pr_pushed`` + ``pr_unapproved``), by push class."""
    rounds: list[int] = []
    by_class: dict[str, dict] = defaultdict(lambda: {"pushes": 0, "prs": set()})
    for state in cohort(court, window):
        pushes, unapprovals = _rounds(state)
        rounds.append(len(pushes) + len(unapprovals))
        classes = set()
        for event in pushes:
            push_class = _push_class(event)
            by_class[push_class]["pushes"] += 1
            classes.add(push_class)
        for push_class in classes:
            by_class[push_class]["prs"].add(state.pr["object_id"])
    return {
        "n": len(rounds),
        "total_rounds": sum(rounds),
        "mean_rounds": round(sum(rounds) / len(rounds), 2) if rounds else None,
        "median_rounds": _median([float(r) for r in rounds]),
        "p90_rounds": _p90([float(r) for r in rounds]),
        "prs_over_2": sum(1 for r in rounds if r > 2),
        "prs_over_max": sum(1 for r in rounds if r > MAX_REVIEW_ROUNDS),
        "excess_rounds": sum(max(0, r - 2) for r in rounds),
        "max_rounds": MAX_REVIEW_ROUNDS,
        "by_push_class": {
            push_class: {"pushes": entry["pushes"], "prs": len(entry["prs"])}
            for push_class, entry in sorted(by_class.items())
        },
    }


def rebase_waste(court: ballcourt.BallCourt, window) -> dict:
    """Approvals lost (``pr_unapproved``) attributed to the causing push class."""
    lost: dict[str, int] = defaultdict(int)
    prs: set[str] = set()
    unapprovals = 0
    for state in cohort(court, window):
        last_push_class: str | None = None
        for event in state.events:
            activity = event["activity"]
            if activity == "pr_pushed":
                last_push_class = _push_class(event)
            elif activity == "pr_unapproved":
                unapprovals += 1
                prs.add(state.pr["object_id"])
                attributes = event.get("attributes") or {}
                if attributes.get("cause") == "manual":
                    lost["manual"] += 1
                else:
                    lost[last_push_class or "other"] += 1
    return {
        "n": unapprovals,
        "prs": len(cohort(court, window)),
        "prs_affected": len(prs),
        "lost_on_pure_rebase": lost.get("pure_rebase", 0),
        "lost_on_rebase_conflict": lost.get("rebase_conflict", 0),
        "lost_on_content": lost.get("content", 0),
        "lost_on_other": lost.get("other", 0),
        "lost_manual": lost.get("manual", 0),
        "share_pure_rebase": round(lost.get("pure_rebase", 0) / unapprovals, 2) if unapprovals else 0.0,
    }


def comment_tax(court: ballcourt.BallCourt, threads: list[dict], window) -> dict:
    """Threads grouped by ``comment_type`` × ``thread_outcome``.

    A ``rejected`` or ``walked_back`` thread is a full review round spent for
    zero value — the "wasted rounds" of prd/17. Threads without a
    ``comment_type`` (stores ingested before the text classifier) are counted
    but not classified, so the metric can say "not available yet" honestly.

    ``acted_on`` is not a free pass (DES-3): the scrape only labels it when a
    deterministic signal (the diff moved at the anchor) or a definite classifier
    read says so. A thread whose outcome could not be determined is
    ``unresolved`` — the bucket that keeps the wasted-round count from being
    flattered — and the payload says what ``unresolved`` means so it is not read
    as "nobody looked at it".
    """
    start, end = window
    cohort_ids = {state.pr["object_id"] for state in cohort(court, window)}
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_type: dict[str, dict] = defaultdict(lambda: {"threads": 0, "wasted": 0})
    outcomes: dict[str, int] = defaultdict(int)
    considered = 0
    classified = 0
    wasted = 0
    for thread in threads:
        in_cohort = (
            thread.get("pr_id") is not None
            and f"PR:{thread.get('repo')}#{thread['pr_id']}" in cohort_ids
        )
        if not in_cohort:
            created = thread.get("created_at")
            if created is None or not (start <= created <= end):
                continue
        considered += 1
        comment_type = thread.get("comment_type")
        outcome = thread.get("thread_outcome")
        if not comment_type:
            continue
        classified += 1
        outcome = outcome or "unresolved"
        matrix[comment_type][outcome] += 1
        outcomes[outcome] += 1
        by_type[comment_type]["threads"] += 1
        if outcome in ("rejected", "walked_back"):
            wasted += 1
            by_type[comment_type]["wasted"] += 1
    return {
        "available": classified > 0,
        "threads": considered,
        "classified": classified,
        "wasted": wasted,
        "wasted_share": round(wasted / classified, 2) if classified else 0.0,
        "rejected": outcomes.get("rejected", 0),
        "walked_back": outcomes.get("walked_back", 0),
        "unresolved": outcomes.get("unresolved", 0),
        "acted_on": outcomes.get("acted_on", 0),
        "outcome_note": (
            "acted_on means a deterministic signal (the diff moved at the anchor) "
            "or a definite classifier read; unresolved includes threads whose "
            "outcome could not be determined, so wasted_share is a floor, not a "
            "flattering estimate"
        ),
        "by_type": {
            comment_type: {"threads": entry["threads"], "wasted": entry["wasted"]}
            for comment_type, entry in sorted(by_type.items())
        },
        "matrix": {
            comment_type: dict(sorted(by_outcome.items()))
            for comment_type, by_outcome in sorted(matrix.items())
        },
    }


def conformance(court: ballcourt.BallCourt, window) -> dict:
    """Merged with no approval; PRs over N review rounds."""
    merged = 0
    without_approval: list[str] = []
    over_rounds: list[tuple[str, int]] = []
    for state in cohort(court, window):
        approvals = [e for e in state.events if e["activity"] == "pr_approved"]
        pushes, unapprovals = _rounds(state)
        rounds = len(pushes) + len(unapprovals)
        if state.pr["state"] == "merged":
            merged += 1
            if not approvals:
                without_approval.append(state.pr["object_id"])
        if rounds > MAX_REVIEW_ROUNDS:
            over_rounds.append((state.pr["object_id"], rounds))
    return {
        "n": len(cohort(court, window)),
        "merged": merged,
        "merged_without_approval": len(without_approval),
        "prs_over_max_rounds": len(over_rounds),
        "excess_rounds": sum(rounds - MAX_REVIEW_ROUNDS for _, rounds in over_rounds),
        "max_rounds": MAX_REVIEW_ROUNDS,
        "examples": sorted(without_approval)[:5],
    }


def pr_size(court: ballcourt.BallCourt, window, sizes: dict[str, dict] | None = None) -> dict:
    """Commits per PR (COMMIT objects) and diff size when captured, vs latency.

    Diff size is optional: ``sizes`` comes from :func:`load_pr_sizes`, which
    returns an entry only when the PR object carries ``lines_added`` /
    ``lines_removed`` or ``diff_size``. Until the scrape-side capture lands the
    metric is honest about it (``diff_size.pending``) and the size cap is on
    commits alone.
    """
    sizes = sizes or {}
    rows: list[dict] = []
    for state in cohort(court, window):
        pr = state.pr
        info = sizes.get(pr["object_id"]) or {}
        diff_size = info.get("diff_size")
        commits = len(state.commits)
        first = _first_review(state)
        pushes, unapprovals = _rounds(state)
        rows.append({
            "object_id": pr["object_id"],
            "commits": commits,
            "diff_size": diff_size,
            "large": bool(
                commits > SIZE_COMMIT_CAP
                or (diff_size is not None and diff_size > SIZE_LINE_CAP)
            ),
            "first_review_hours": (
                _span_hours(court, first["actor"], pr["created_ts"], first["_ts"])
                if first is not None else None
            ),
            "rounds": len(pushes) + len(unapprovals),
        })

    large = [row for row in rows if row["large"]]
    small = [row for row in rows if not row["large"]]
    diff_sizes = [row["diff_size"] for row in rows if row["diff_size"] is not None]
    commits = [float(row["commits"]) for row in rows]

    def latency(group: list[dict]) -> float | None:
        return _median([
            row["first_review_hours"] for row in group
            if row["first_review_hours"] is not None
        ])

    return {
        "n": len(rows),
        "commits": {
            "median": _median(commits),
            "p90": _p90(commits),
            "max": int(max(commits)) if commits else 0,
            "total": int(sum(commits)),
        },
        "diff_size": {
            "available": bool(diff_sizes),
            "pending": not diff_sizes,
            "n": len(diff_sizes),
            "median": _median([float(value) for value in diff_sizes]),
            "p90": _p90([float(value) for value in diff_sizes]),
            "max": int(max(diff_sizes)) if diff_sizes else 0,
        },
        "commits_cap": SIZE_COMMIT_CAP,
        "lines_cap": SIZE_LINE_CAP,
        "over_commits_cap": sum(1 for row in rows if row["commits"] > SIZE_COMMIT_CAP),
        "over_lines_cap": sum(
            1 for row in rows
            if row["diff_size"] is not None and row["diff_size"] > SIZE_LINE_CAP
        ),
        "large_prs": len(large),
        "small_prs": len(small),
        "large_share": round(len(large) / len(rows), 2) if rows else 0.0,
        "large_median_first_review_hours": latency(large),
        "small_median_first_review_hours": latency(small),
        "large_median_rounds": _median([float(row["rounds"]) for row in large]),
        "small_median_rounds": _median([float(row["rounds"]) for row in small]),
    }


def sentinel_queue(court: ballcourt.BallCourt, window) -> dict:
    """Median wait on the mandatory approval, when sentinel roles exist."""
    start, end = window
    sentinels = set(court.sentinels)
    if not sentinels:
        return {
            "available": False,
            "sentinels": [],
            "n": 0,
            "median_hours": None,
            "p90_hours": None,
            "target_hours": TARGET_FIRST_REVIEW_HOURS,
            "by_person": {},
        }
    waits: dict[str, list[float]] = defaultdict(list)
    for interval in court.intervals:
        if not _overlap(interval, start, end):
            continue
        if interval.ball == ballcourt.REVIEW:
            for person in interval.holders:
                if person in sentinels:
                    waits[person].append(interval.hours)
    all_waits = [hours for values in waits.values() for hours in values]
    return {
        "available": bool(all_waits),
        "sentinels": sorted(sentinels),
        "n": len(all_waits),
        "median_hours": _median(all_waits),
        "p90_hours": _p90(all_waits),
        "target_hours": TARGET_FIRST_REVIEW_HOURS,
        "by_person": {
            person: {"n": len(values), "median_hours": _median(values)}
            for person, values in sorted(waits.items())
        },
    }


def vacation_blocking(court: ballcourt.BallCourt, window) -> dict:
    """Vacation-blocking signal (prd/19): what waited on a person while away.

    For each configured vacation, the items whose ball sat with that person
    during the vacation are the ones the absence blocked. ``hours`` is the
    working time those items lost — business hours with the vacation ignored,
    because a vacation's own business hours are zero by definition — and an item
    counts as *delegated* when someone else held its ball during the same
    period. ``blocked_*`` is what nobody picked up: the pattern the PRD wants
    surfaced ("someone goes on vacation and things block").
    """
    start, end = window
    calendar = court.calendar
    if calendar is None:
        return {
            "available": False,
            "vacations": [],
            "n": 0,
            "items": 0,
            "hours": 0.0,
            "blocked_items": 0,
            "blocked_hours": 0.0,
            "delegated_items": 0,
            "note": "no calendar configured, so no vacations are known",
        }

    pseudo = {ballcourt.REVIEW, ballcourt.CI, ballcourt.CALENDAR}
    rows: list[dict] = []
    for vacation in calendar.vacation_list():
        person = vacation["person"]
        v_start, v_end = calendar.day_bounds(
            date.fromisoformat(vacation["start"]), date.fromisoformat(vacation["end"])
        )
        if v_end <= start or v_start >= end:
            continue  # outside the window
        blocked = [
            interval for interval in court.intervals
            if interval.measured
            and person in interval.holders
            and interval.start < v_end and interval.end > v_start
            and _overlap(interval, start, end)
        ]
        per_item: dict[str, float] = defaultdict(float)
        for interval in blocked:
            low = max(interval.start, v_start)
            high = min(interval.end, v_end)
            per_item[interval.object_id] += calendar.business_hours(
                person, low, high, ignore_vacations=True
            )
        delegated = {
            interval.object_id for interval in court.intervals
            if interval.measured
            and any(
                holder not in pseudo and holder != person
                for holder in interval.holders
            )
            and interval.object_id in per_item
            and interval.start < v_end and interval.end > v_start
        }
        blocked_items = [oid for oid in per_item if oid not in delegated]
        rows.append({
            **vacation,
            "items": len(per_item),
            "hours": round(sum(per_item.values()), 1),
            "delegated_items": len(set(per_item) & delegated),
            "blocked_items": len(blocked_items),
            "blocked_hours": round(sum(per_item[oid] for oid in blocked_items), 1),
            "prs": sorted(per_item)[:10],
        })

    rows.sort(key=lambda row: (-row["blocked_hours"], row["person"], row["start"]))
    return {
        "available": True,
        "vacations": rows,
        "n": len(rows),
        "items": sum(row["items"] for row in rows),
        "hours": round(sum(row["hours"] for row in rows), 1),
        "blocked_items": sum(row["blocked_items"] for row in rows),
        "blocked_hours": round(sum(row["blocked_hours"] for row in rows), 1),
        "delegated_items": sum(row["delegated_items"] for row in rows),
        "note": (
            "hours are the working time an item lost while it waited on the "
            "person, priced with that person's vacation ignored"
        ),
    }


def metrics(court: ballcourt.BallCourt, threads: list[dict], window,
            sizes: dict[str, dict] | None = None,
            jira: jira_analysis.JiraData | None = None) -> dict:
    """Every prd/17 metric, in one deterministic pass.

    The Jira half (prd/21) is merged in under its own keys; with no Jira data in
    the store every one of those metrics is ``available: false`` with the same
    note, so "the ticket layer is missing" is a stated result rather than a
    silent zero.
    """
    return {
        "time_to_first_review": time_to_first_review(court, window),
        "cycle_time": cycle_time(court, window),
        "flow_efficiency": flow_efficiency(court, window),
        "survival": time_to_merge(court, window),
        "reviewer_responsiveness": reviewer_responsiveness(court, window),
        "rework_loops": rework_loops(court, window),
        "rebase_waste": rebase_waste(court, window),
        "comment_tax": comment_tax(court, threads, window),
        "conformance": conformance(court, window),
        "sentinel_queue": sentinel_queue(court, window),
        "pr_size": pr_size(court, window, sizes),
        "vacation_blocking": vacation_blocking(court, window),
        "stacked_cascade": stack_analysis.metrics(court, window),
        "review_cost": {"hours_per_round": round_cost_hours(court, window)},
        # prd/21: the ticket layer. Keys are distinct from the PR-level ones so
        # a reader can never confuse "merged with no approval" (conformance)
        # with "started before refinement" (jira_conformance).
        **jira_analysis.metrics(court, jira, window),
    }


def load_jira(con, court: ballcourt.BallCourt) -> jira_analysis.JiraData:
    """The Jira half of the store, joined to the PR timelines on the story key."""
    return jira_analysis.load(con, court)


# --------------------------------------------------------------------------- #
# suggestion engine: one metric -> one or more rule templates
# --------------------------------------------------------------------------- #
def _suggestion(
    sid: str,
    metric: str,
    title: str,
    evidence: dict,
    suggestion: str,
    severity: str,
    impact_hours: float,
    sample: int,
    scope: dict | None = None,
) -> dict:
    impact = round(max(0.0, float(impact_hours)), 1)
    confidence = _confidence(sample)
    return {
        "id": sid,
        "metric": metric,
        "title": title,
        "evidence": evidence,
        "suggestion": suggestion,
        "severity": severity,
        "impact_hours": impact,
        "confidence": confidence,
        "impact_score": round(impact * confidence, 1),
        "sample": sample,
        "scope": scope or {"kind": "team"},
    }


def templates(
    m: dict,
    level: str = "team",
    round_hours: float = DEFAULT_ROUND_HOURS,
    bots: set[str] | None = None,
) -> list[dict]:
    """Metric values -> concrete rules (threshold + severity + impact)."""
    bots = bots or set()
    out: list[dict] = []

    # 1. time_to_first_review — the strongest single predictor of velocity.
    tfr = m["time_to_first_review"]
    median = tfr["median_hours"]
    p90 = tfr["p90_hours"]
    if median is not None and median > TARGET_FIRST_REVIEW_HOURS:
        severity = "critical" if median >= 3 * TARGET_FIRST_REVIEW_HOURS else "warning"
        out.append(_suggestion(
            "review-sla", "time_to_first_review",
            f"Time to first review is {_human_hours(median)} (target < 24 h)",
            {
                "median_hours": median,
                "p90_hours": p90,
                "target_hours": TARGET_FIRST_REVIEW_HOURS,
                "prs": tfr["n"],
                "over_target": tfr["over_target"],
                "no_review_yet": tfr["missing"],
            },
            "Adopt a review SLA: every PR gets a first response within 24 h. Triage the "
            "review queue each morning so nothing waits a full day for a first look.",
            severity, (median - TARGET_FIRST_REVIEW_HOURS) * tfr["n"], tfr["n"],
        ))
    elif p90 is not None and p90 > 2 * TARGET_FIRST_REVIEW_HOURS:
        # the median is fine but the tail is not: a cheaper, more targeted rule
        out.append(_suggestion(
            "review-tail", "time_to_first_review",
            f"The median first review meets 24 h, but the slowest 10% wait {_human_hours(p90)}",
            {
                "median_hours": median,
                "p90_hours": p90,
                "target_hours": TARGET_FIRST_REVIEW_HOURS,
                "prs": tfr["n"],
                "over_target": tfr["over_target"],
            },
            "Triage the stragglers: the median is healthy, so a daily pass over PRs older "
            "than a day removes the long tail without changing the process.",
            "info", (p90 - TARGET_FIRST_REVIEW_HOURS) * tfr["over_target"] * 0.5,
            tfr["n"],
        ))

    # 2. cycle_time / time_to_merge.
    cyc = m["cycle_time"]
    if cyc["median_hours"] is not None and cyc["median_hours"] > TARGET_CYCLE_HOURS:
        severity = "critical" if cyc["median_hours"] >= 7 * 24 else "warning"
        out.append(_suggestion(
            "cycle-time", "cycle_time",
            f"Cycle time is {_human_hours(cyc['median_hours'])} median (target < 3 d)",
            {
                "median_hours": cyc["median_hours"],
                "p90_hours": cyc["p90_hours"],
                "target_hours": TARGET_CYCLE_HOURS,
                "merged_prs": cyc["n"],
                "over_target": cyc["over_target"],
                # product review #5: the median is over merged PRs only
                "cohort_prs": cyc["cohort"],
                "open_censored_prs": cyc["censored"]["open"],
                "open_censored_over_target": cyc["censored"]["open_over_target"],
                "censoring": (
                    "merged-only; open PRs are right-censored — metrics.survival "
                    "is the Kaplan–Meier estimate over the full cohort"
                ),
                "survival_median_hours": cyc["survival_median_hours"],
            },
            "Cap work in progress and split large PRs: a PR that opens and merges inside "
            "72 h stops review and rebase waits from compounding.",
            severity, (cyc["median_hours"] - TARGET_CYCLE_HOURS) * cyc["n"], cyc["n"],
        ))

    # 2b. flow_efficiency — the waiting-time decomposition's headline (prd/05).
    fe = m["flow_efficiency"]
    process_efficiency = fe["aggregate_efficiency"]
    if (
        fe["available"]
        and process_efficiency is not None
        and process_efficiency < TARGET_FLOW_EFFICIENCY
    ):
        percent = round(process_efficiency * 100)
        waiting_percent = 100 - percent
        severity = (
            "critical" if process_efficiency < CRITICAL_FLOW_EFFICIENCY else "warning"
        )
        bucket, entry = max(
            fe["waiting_breakdown"].items(), key=lambda item: (item[1]["hours"], item[0])
        )
        bucket_label = {
            "review": "the review queue",
            "ci": "CI",
            "waiting": "an idle author",
            "unobserved": "the unobserved tail",
            "closed": "the close tail",
        }.get(bucket, bucket)
        excess = sum(
            max(
                0.0,
                row["waiting_hours"]
                - row["elapsed_hours"] * (1 - TARGET_FLOW_EFFICIENCY),
            )
            for row in fe["prs"]
            if row["elapsed_hours"] > 0
        )
        median_percent = round((fe["median_efficiency"] or 0.0) * 100)
        # When the median PR is efficient but the process is not, the gap is the
        # long PRs — worth saying, because it changes which fix to pick.
        gap = (
            f" The median PR is at {median_percent}%, so the gap to the process "
            "number is the long PRs: split or close those first."
            if fe["median_efficiency"] is not None
            and fe["median_efficiency"] >= TARGET_FLOW_EFFICIENCY
            else ""
        )
        out.append(_suggestion(
            "flow-efficiency", "flow_efficiency",
            f"Flow efficiency is {percent}% — the process spends {waiting_percent}% "
            "of cycle time waiting",
            {
                "aggregate_efficiency": process_efficiency,
                "median_efficiency": fe["median_efficiency"],
                "waiting_share": round(1 - process_efficiency, 2),
                "median_elapsed_hours": fe["median_elapsed_hours"],
                "median_waiting_hours": fe["median_waiting_hours"],
                "largest_waiting_bucket": bucket_label,
                "largest_waiting_share": entry["share"],
                "prs": fe["n"],
                "target_efficiency": TARGET_FLOW_EFFICIENCY,
                "touch_hours_per_touch": TOUCH_HOURS,
            },
            f"Only {percent}% of a PR's life is hands-on time and {waiting_percent}% "
            f"is queue, mostly {bucket_label}. Shrink the queue rather than speeding "
            "up the work: a first-response SLA, smaller PRs, and a daily review block "
            f"move flow efficiency more than asking people to type faster.{gap}",
            severity, excess, fe["n"],
        ))

    # 2c. survival / time-to-merge — the censoring-aware estimate (prd/05, goal 7).
    sv = m["survival"]
    if sv["median_hours"] is not None and sv["median_hours"] > TARGET_CYCLE_HOURS:
        severity = "critical" if sv["median_hours"] >= 7 * 24 else "warning"
        out.append(_suggestion(
            "time-to-merge", "survival",
            f"Time to merge is {_human_hours(sv['median_hours'])} median with open "
            "and declined PRs censored in (target < 3 d)",
            {
                "survival_median_hours": sv["median_hours"],
                "merged_only_median_hours": sv["merged_only_median_hours"],
                "open_censored_prs": sv["censored"]["open"],
                "declined_censored_prs": sv["censored"]["declined"],
                "cohort_prs": sv["cohort"],
                "merged_prs": sv["events"],
                "p25_hours": sv["quantiles"]["p25"],
                "p75_hours": sv["quantiles"]["p75"],
                "target_hours": TARGET_CYCLE_HOURS,
                "estimator": sv["estimator"],
            },
            "Plan against the survival estimate, not the merged-only median: the PRs "
            "that linger are exactly the ones a merged-only number cannot see. Close, "
            "split, or decline the ones that sit open, and treat 'still open after "
            "3 days' as a queue rather than as work in progress.",
            severity, (sv["median_hours"] - TARGET_CYCLE_HOURS) * sv["n"], sv["n"],
        ))

    # 2d. cycle-time-censoring — a merged-only median that hides the open tail
    # (product review #5). Fires only when the headline number *looks* healthy
    # while the censored population is already past the target.
    if (
        sv["censored"]["open_over_target"] > 0
        and sv["merged_only_median_hours"] is not None
        and sv["merged_only_median_hours"] <= TARGET_CYCLE_HOURS
    ):
        over = sv["censored"]["open_over_target"]
        oldest = sv["censored"]["oldest_open_age_hours"]
        severity = (
            "critical"
            if oldest is not None and oldest >= 3 * TARGET_CYCLE_HOURS
            else "warning"
        )
        out.append(_suggestion(
            "cycle-time-censoring", "survival",
            f"The merged-only median "
            f"({_human_hours(sv['merged_only_median_hours'])}) meets 3 d, but "
            f"{_count(over, 'open PR')} already waited past it",
            {
                "merged_only_median_hours": sv["merged_only_median_hours"],
                "survival_median_hours": sv["median_hours"],
                "open_censored_prs": sv["censored"]["open"],
                "open_over_target": over,
                "oldest_open_age_hours": oldest,
                "cohort_prs": sv["cohort"],
                "target_hours": TARGET_CYCLE_HOURS,
                "estimator": sv["estimator"],
            },
            "Do not read the merged-only median as the team's cycle time: a PR that "
            "has not merged cannot enter it, so the items that linger are invisible. "
            "Close, split, or decline the open PRs past the target — an open PR is a "
            "wait with no end yet — and track time-to-merge with the survival estimate.",
            severity, sv["censored"]["open_excess_hours"], max(1, sv["censored"]["open"]),
        ))

    # 3. reviewer_responsiveness — pseudonyms only, team or person granularity.
    resp = m["reviewer_responsiveness"]
    if level == "person":
        for person in resp["people"]:
            if not person["flagged"] or person["person"] in bots:
                continue
            review_median = person["review_median_hours"]
            author_median = person["author_median_hours"]
            severity = "critical" if review_median >= 3 * TARGET_FIRST_REVIEW_HOURS else "warning"
            out.append(_suggestion(
                f"reviewer-{person['person']}", "reviewer_responsiveness",
                f"Review waits on {person['person']} run {_human_hours(review_median)} "
                f"vs {_human_hours(author_median)} on their own PRs",
                {
                    "person": person["person"],
                    "review_median_hours": review_median,
                    "review_intervals": person["review_n"],
                    "author_median_hours": author_median,
                    "author_intervals": person["author_n"],
                    "target_hours": TARGET_FIRST_REVIEW_HOURS,
                },
                f"Give {person['person']} a review block before new work: their own PRs "
                f"cycle in {_human_hours(author_median)}, while items wait "
                f"{_human_hours(review_median)} for their first look. Review before "
                "starting, not after finishing.",
                severity, (review_median - TARGET_FIRST_REVIEW_HOURS) * person["review_n"],
                person["review_n"],
                scope={"kind": "person", "person": person["person"]},
            ))
    else:
        flagged = [
            p for p in resp["people"] if p["flagged"] and p["person"] not in bots
        ]
        if len(flagged) >= 2 and resp["review_n"]:
            worst = max(flagged, key=lambda p: (p["review_median_hours"], p["person"]))
            fastest = min(
                (
                    p for p in resp["people"]
                    if p["review_median_hours"] is not None and p["person"] not in bots
                ),
                key=lambda p: (p["review_median_hours"], p["person"]),
            )
            excess = sum(
                (p["review_median_hours"] - TARGET_FIRST_REVIEW_HOURS) * p["review_n"]
                for p in flagged
            )
            out.append(_suggestion(
                "reviewer-spread", "reviewer_responsiveness",
                f"{len(flagged)} reviewers hold review waits above 24 h "
                f"(worst {_human_hours(worst['review_median_hours'])})",
                {
                    "flagged_reviewers": len(flagged),
                    "reviewers": resp["reviewers"],
                    "worst_median_hours": worst["review_median_hours"],
                    "fastest_median_hours": fastest["review_median_hours"],
                    "spread_hours": resp["spread_hours"],
                    "review_intervals": resp["review_n"],
                    "target_hours": TARGET_FIRST_REVIEW_HOURS,
                },
                "Balance the reviewer roster and give the slowest queues a daily review "
                "block: review latency is concentrated in a few people, so rotating the "
                "roster moves it more than asking everyone to review faster.",
                "warning", excess, resp["review_n"],
            ))

    # 4. rework_loops.
    rw = m["rework_loops"]
    if rw["excess_rounds"] > 0 and (rw["p90_rounds"] or 0) > 2:
        out.append(_suggestion(
            "rework-loops", "rework_loops",
            f"Review loops run to {round(rw['p90_rounds'])} rounds at p90 (cap 2)",
            {
                "prs": rw["n"],
                "median_rounds": rw["median_rounds"],
                "p90_rounds": rw["p90_rounds"],
                "prs_over_2": rw["prs_over_2"],
                "excess_rounds": rw["excess_rounds"],
                "hours_per_round": round_hours,
            },
            "Cap review rounds at two: if a PR needs a third round, the owner and reviewer "
            "agree the remaining blockers in one pass instead of another comment round.",
            "warning", rw["excess_rounds"] * round_hours, rw["n"],
        ))

    # 5. rebase_waste.
    rb = m["rebase_waste"]
    if rb["lost_on_pure_rebase"] > 0:
        lost = rb["lost_on_pure_rebase"]
        share = rb["share_pure_rebase"]
        severity = "critical" if lost >= 5 or share >= 0.5 else "warning"
        out.append(_suggestion(
            "rebase-waste", "rebase_waste",
            f"{lost} approval{'s' if lost != 1 else ''} lost on pure rebases",
            {
                "approvals_lost": lost,
                "unapprovals": rb["n"],
                "share_of_unapprovals": share,
                "rebase_conflict_lost": rb["lost_on_rebase_conflict"],
                "prs_affected": rb["prs_affected"],
                "hours_per_round": round_hours,
            },
            "Don't rebase approved PRs unless the target moved in a conflicting file; tie "
            "auto-unapprove to a content change (patch-id), not to any push.",
            severity, lost * round_hours, rb["prs"],
        ))

    # 5b. stacked_cascade (prd/22) — the rebase cost a stack walks up. A rule
    # needs priced hours: rebases whose review round never finished are reported
    # by the metric (unfinished_rounds) but cannot carry an impact estimate.
    sc = m["stacked_cascade"]
    if sc["available"] and sc["re_review_hours"] > 0:
        rebases = sc["cascaded_rebases"]
        hours = sc["re_review_hours"]
        severity = (
            "critical" if hours >= 3 * round_hours
            else ("warning" if hours >= round_hours else "info")
        )
        critical = sc["critical_path"]
        deepest = sc["depth"]["max"]
        out.append(_suggestion(
            "stacked-cascade", "stacked_cascade",
            f"{_count(sc['stacked_prs'], 'stacked PR')}; a lower-PR update triggered "
            f"{_count(rebases, 'rebase')} and {_human_hours(hours)} of re-review up-stack",
            {
                "stacked_prs": sc["stacked_prs"],
                "stacks": sc["stacks"],
                "triggers": sc["triggers"],
                "rebases": rebases,
                "re_review_hours": hours,
                "depth_median": sc["depth"]["median"],
                "depth_max": deepest,
                "critical_path_hours": critical["hours"],
                "unfinished_rounds": sc["unfinished_rounds"],
                "hours_per_round": round_hours,
            },
            "Merge stacks bottom-up: land the bottom PR first and retarget or rebase the "
            "ones above it in the same pass, and cap stack depth at "
            f"{stack_analysis.MAX_STACK_DEPTH}. Every update to a "
            "lower PR reopens review on everything above it, so a tall stack turns one "
            "merge into a chain of rebases and re-reviews.",
            severity, hours, sc["stacked_prs"] or sc["triggers"],
        ))

    # 6. comment_tax.
    ct = m["comment_tax"]
    if ct["available"] and ct["wasted"] >= 3 and ct["wasted_share"] >= 0.1:
        share = ct["wasted_share"]
        severity = "critical" if ct["wasted"] >= 5 and share >= 0.3 else "warning"
        top_type = max(ct["by_type"].items(), key=lambda item: (item[1]["wasted"], item[0]))[0]
        out.append(_suggestion(
            "comment-tax", "comment_tax",
            f"{round(share * 100)}% of review threads were rejected or walked back",
            {
                "threads": ct["classified"],
                "wasted": ct["wasted"],
                "share": share,
                "rejected": ct["rejected"],
                "walked_back": ct["walked_back"],
                "top_type": top_type,
                "hours_per_round": round_hours,
            },
            "Move nits and out-of-scope comments to the ticket and settle questions in "
            "refinement: a rejected or walked-back thread costs a full review round for "
            "no change.",
            severity, ct["wasted"] * round_hours, ct["classified"],
        ))

    # 7. conformance.
    cf = m["conformance"]
    if cf["merged_without_approval"] > 0:
        out.append(_suggestion(
            "conformance-unapproved", "conformance",
            f"{_count(cf['merged_without_approval'], 'PR')} merged with no approval",
            {
                "merged": cf["merged"],
                "merged_without_approval": cf["merged_without_approval"],
                "prs": cf["n"],
                "hours_per_round": round_hours,
            },
            "Enforce the +1 rule in branch permissions: no merge without an approving "
            "review, hotfixes included. A follow-up review is fine; a missing one is not.",
            "critical", cf["merged_without_approval"] * round_hours,
            max(cf["merged"], 1),
        ))
    if cf["prs_over_max_rounds"] > 0:
        out.append(_suggestion(
            "conformance-rounds", "conformance",
            f"{_count(cf['prs_over_max_rounds'], 'PR')} ran past {cf['max_rounds']} review rounds",
            {
                "prs": cf["n"],
                "prs_over_max_rounds": cf["prs_over_max_rounds"],
                "max_rounds": cf["max_rounds"],
                "excess_rounds": cf["excess_rounds"],
                "hours_per_round": round_hours,
            },
            f"Escalate after {cf['max_rounds']} rounds: the owner and reviewer agree the "
            "remaining blockers in one pass instead of another comment round.",
            "warning", cf["excess_rounds"] * round_hours, cf["n"],
        ))

    # 8. sentinel_queue — only when role data exists.
    sq = m["sentinel_queue"]
    if sq["available"] and sq["median_hours"] is not None and sq["median_hours"] > TARGET_FIRST_REVIEW_HOURS:
        severity = "critical" if sq["median_hours"] >= 3 * TARGET_FIRST_REVIEW_HOURS else "warning"
        out.append(_suggestion(
            "sentinel-queue", "sentinel_queue",
            f"The mandatory-approval queue waits {_human_hours(sq['median_hours'])} median",
            {
                "sentinels": len(sq["sentinels"]),
                "waits": sq["n"],
                "median_hours": sq["median_hours"],
                "p90_hours": sq["p90_hours"],
                "target_hours": TARGET_FIRST_REVIEW_HOURS,
            },
            "Rotate or add sentinels: the mandatory approval is the longest single wait, "
            "so one more sentinel shortens the tail of every PR that needs one.",
            severity, (sq["median_hours"] - TARGET_FIRST_REVIEW_HOURS) * sq["n"], sq["n"],
        ))

    # 9. pr_size — commits (+ diff size when captured) vs review latency.
    # The effect is measured on *first-review latency*, not review rounds: rounds
    # count pushes, and pushes add commits, so "large PRs bounce more" would be
    # partly circular. Rounds stay in the metric as context, never as the rule.
    ps = m["pr_size"]
    if ps["large_prs"] >= 2 and ps["small_prs"] >= 2:
        large_latency = ps["large_median_first_review_hours"]
        small_latency = ps["small_median_first_review_hours"]
        if (
            large_latency is not None and small_latency is not None
            and large_latency > small_latency
        ):
            impact = (large_latency - small_latency) * ps["large_prs"]
            if impact > 0:
                pending = ps["diff_size"]["pending"]
                cap = (
                    f"{ps['commits_cap']} commits"
                    if pending
                    else f"{ps['lines_cap']} changed lines or {ps['commits_cap']} commits"
                )
                title = (
                    f"PRs over {cap} get first reviews in "
                    f"{_human_hours(large_latency)} vs {_human_hours(small_latency)} "
                    "for smaller ones"
                )
                severity = (
                    "critical"
                    if small_latency > 0 and large_latency >= 3 * small_latency
                    and ps["large_prs"] >= 3
                    else "warning"
                )
                suggestion = (
                    f"Adopt a PR size cap: split work so a PR stays under "
                    f"{ps['commits_cap']} commits"
                    + ("" if pending else f" and {ps['lines_cap']} changed lines")
                    + ". Large PRs wait longer for a first review; a smaller diff is "
                    "easier to pick up and finish."
                    + (
                        " Diff size is not captured yet, so the cap is on commits for now."
                        if pending else ""
                    )
                )
                out.append(_suggestion(
                    "pr-size", "pr_size", title,
                    {
                        "commits_cap": ps["commits_cap"],
                        "lines_cap": ps["lines_cap"],
                        "large_prs": ps["large_prs"],
                        "small_prs": ps["small_prs"],
                        "large_median_first_review_hours": large_latency,
                        "small_median_first_review_hours": small_latency,
                        "large_median_rounds": ps["large_median_rounds"],
                        "small_median_rounds": ps["small_median_rounds"],
                        "median_commits": ps["commits"]["median"],
                        "p90_commits": ps["commits"]["p90"],
                        "diff_size_pending": pending,
                        "hours_per_round": round_hours,
                    },
                    suggestion, severity, impact, ps["large_prs"],
                ))

    # 10. vacation_blocking — the "someone goes on vacation and things block"
    # pattern (prd/19). A vacation is a person fact, so the team-level rule
    # aggregates periods without naming anyone; the person level names the
    # pseudonym it already has in scope.
    vb = m["vacation_blocking"]
    if vb["available"] and vb["blocked_items"] > 0:
        if level == "person":
            for vacation in vb["vacations"]:
                if vacation["blocked_items"] <= 0 or vacation["person"] in bots:
                    continue
                severity = (
                    "critical"
                    if vacation["blocked_hours"] >= 40 or vacation["blocked_items"] >= 5
                    else "warning"
                )
                out.append(_suggestion(
                    f"vacation-{vacation['person']}-{vacation['start']}",
                    "vacation_blocking",
                    f"{vacation['person']} on vacation {vacation['start']} → "
                    f"{vacation['end']}: {vacation['blocked_items']} "
                    f"item{'s' if vacation['blocked_items'] != 1 else ''} waited "
                    f"{_human_hours(vacation['blocked_hours'])} with no delegation",
                    {
                        "person": vacation["person"],
                        "start": vacation["start"],
                        "end": vacation["end"],
                        "business_days": vacation["business_days"],
                        "items": vacation["items"],
                        "blocked_items": vacation["blocked_items"],
                        "blocked_hours": vacation["blocked_hours"],
                        "delegated_items": vacation["delegated_items"],
                    },
                    f"Name a delegate before {vacation['person']} goes away: hand open "
                    "review requests to a backup reviewer on the last working day, and "
                    "add the backup to the PR's reviewers so the ball moves while they "
                    "are on vacation.",
                    severity, vacation["blocked_hours"], vacation["items"],
                    scope={"kind": "person", "person": vacation["person"]},
                ))
        else:
            worst = max(
                (v for v in vb["vacations"] if v["blocked_items"] > 0),
                key=lambda v: (v["blocked_hours"], v["person"]),
            )
            severity = (
                "critical"
                if vb["blocked_hours"] >= 40 or vb["blocked_items"] >= 5
                else "warning"
            )
            out.append(_suggestion(
                "vacation-blocking", "vacation_blocking",
                f"{vb['blocked_items']} items waited "
                f"{_human_hours(vb['blocked_hours'])} on someone on vacation "
                "with no delegation",
                {
                    "vacations": vb["n"],
                    "blocked_items": vb["blocked_items"],
                    "blocked_hours": vb["blocked_hours"],
                    "delegated_items": vb["delegated_items"],
                    "worst_business_days": worst["business_days"],
                    "worst_blocked_hours": worst["blocked_hours"],
                    "worst_blocked_items": worst["blocked_items"],
                },
                "Make delegation part of taking leave: before a vacation, hand open "
                "review requests to a named backup and add them to the PR's reviewers. "
                "An item whose ball sits with someone who is away waits for their "
                "return, not for a decision.",
                severity, vb["blocked_hours"], vb["blocked_items"],
            ))

    # ----------------------------------------------------------------------- #
    # 11-15. the Jira layer (prd/21) — the ticket half of the flow
    # ----------------------------------------------------------------------- #
    # 11. waiting_time_decomposition — where the ticket's time goes.
    wtd = m["waiting_time_decomposition"]
    if wtd["available"] and wtd["largest_share"] >= SEGMENT_SHARE_TARGET:
        segment = wtd["largest"]
        hours = wtd["largest_hours"]
        share = wtd["largest_share"]
        percent = round(share * 100)
        severity = "critical" if share >= SEGMENT_SHARE_CRITICAL else "warning"
        advice = SEGMENT_ADVICE.get(segment, "Shrink the biggest queue.")
        out.append(_suggestion(
            "jira-waiting-time", "waiting_time_decomposition",
            f"The team spends {percent}% of ticket cycle time in the "
            f"{wtd['largest_label']} segment",
            {
                "segment": wtd["largest_label"],
                "segment_hours": hours,
                "share": share,
                "cycle_hours": wtd["total_hours"],
                "stories": wtd["stories_decomposed"],
                "flow_efficiency": wtd["largest_efficiency"],
                "impact_basis": "half the largest segment's hours",
                "examples": ", ".join(wtd.get("largest_examples") or []),
            },
            f"{advice} The largest wait is {_human_hours(hours)} per story in "
            f"the {wtd['largest_label']} segment, {percent}% of the measured "
            "ticket cycle. Fix that phase first: it is the cheapest hour to "
            "remove, because nothing is being worked on during it.",
            severity, hours / 2.0, wtd["stories_decomposed"],
        ))

    # 12. carry_over — "the sprint plan isn't honest".
    co = m["carry_over"]
    if (
        co["available"]
        and co["carried_over"] > 0
        and (co["share"] >= CARRY_OVER_SHARE_TARGET or co["carried_over"] >= 3)
    ):
        carried = co["carried_over"]
        severity = (
            "critical" if co["share"] >= 0.25 or carried >= 5 else "warning"
        )
        out.append(_suggestion(
            "jira-carry-over", "carry_over",
            f"{carried} stor{'y' if carried == 1 else 'ies'} carried over "
            "between sprints",
            {
                "carried_over": carried,
                "stories_in_a_sprint": co["stories_in_a_sprint"],
                "share": co["share"],
                "sprints": co["sprints_involved"],
                "wait_hours": co["wait_hours"],
                "median_wait_hours": co["median_wait_hours"],
                "target_share": co["target_share"],
                # prd/25: ticket keys in evidence are doorways to the detail page
                "examples": ", ".join(row["key"] for row in co["rows"][:5]),
            },
            "Plan from a refined backlog and finish what you commit: a story "
            "carried into a second sprint takes capacity from the next plan "
            "twice. Split the story until it fits one sprint, and leave it in "
            "the backlog until it does — a carry-over is a commitment the "
            "team already knew it would break.",
            severity, co["wait_hours"], co["stories_in_a_sprint"] or carried,
        ))

    # 13. point_churn — the estimate moved after the work started.
    pc = m["point_churn"]
    rc = m["refinement_cost"]
    if (
        pc["churned"] > 0
        and (pc["share"] >= POINT_CHURN_SHARE_TARGET or pc["churned"] >= 3)
    ):
        churned = pc["churned"]
        severity = "critical" if pc["share"] >= 0.25 else "warning"
        out.append(_suggestion(
            "jira-point-churn", "point_churn",
            f"{churned} stor{'y' if churned == 1 else 'ies'} re-pointed after "
            "work started",
            {
                "churned": churned,
                "stories_pointed": pc["stories_pointed"],
                "share": pc["share"],
                "repoint_events": pc["churned_events"],
                "hours_per_refinement": rc["hours"],
                "refinement_basis": rc["basis"],
                "examples": ", ".join(row["key"] for row in pc["rows"][:5]),
            },
            "Settle the estimate in refinement: a re-point after work starts "
            "means the sprint was planned on a number that was not final, so "
            "the burn-down and the capacity both lie. If the story is bigger "
            "than the estimate, split it rather than re-pointing it mid-flight.",
            severity, churned * rc["hours"], pc["stories_pointed"] or churned,
        ))

    # 14. worklog_cross_check — how much to trust the logged hours.
    wc = m["worklog_cross_check"]
    if wc["available"]:
        delay = wc["mean_delay_days"]
        if delay is not None and delay >= WORKLOG_DELAY_TARGET_DAYS:
            severity = (
                "critical"
                if delay >= 7 or wc["late_share"] >= WORKLOG_LATE_SHARE_TARGET
                else "warning"
            )
            out.append(_suggestion(
                "jira-worklog-noise", "worklog_cross_check",
                f"Logging is {round(delay, 1)} days late on average — don't "
                "trust the logged hours",
                {
                    "mean_delay_days": delay,
                    "median_delay_days": wc["median_delay_days"],
                    "p90_delay_days": wc["p90_delay_days"],
                    "late_share": wc["late_share"],
                    "untrusted_hours": wc["untrusted_hours"],
                    "worklogs": wc["worklogs"],
                    "logged_hours": wc["logged_hours"],
                    "target_days": wc["delay_target_days"],
                },
                "Log the hours on the day they are worked: a worklog written a "
                "week later is reconstructed from memory, so it can support a "
                "total but never a timeline. Until logging is same-day, read "
                "logged hours as an estimate of effort, not as evidence of when "
                "the work happened.",
                severity, 0.0, wc["worklogs"],
            ))
        elif wc["late_share"] >= WORKLOG_LATE_SHARE_TARGET:
            late_percent = round(wc["late_share"] * 100)
            out.append(_suggestion(
                "jira-worklog-noise", "worklog_cross_check",
                f"{late_percent}% of worklogs are a week or more late — don't "
                "trust the logged hours",
                {
                    "late_share": wc["late_share"],
                    "late_worklogs": wc["buckets"]["within_week"] + wc["buckets"]["late"],
                    "untrusted_hours": wc["untrusted_hours"],
                    "worklogs": wc["worklogs"],
                    "logged_hours": wc["logged_hours"],
                    "target_days": wc["delay_target_days"],
                },
                "Log the hours on the day they are worked, or accept that the "
                "worklog is a weekly estimate: a log written after the fact "
                "cannot tell you when the work happened, which is what the "
                "timeline needs.",
                "warning", 0.0, wc["worklogs"],
            ))
        ratio = wc["logged_to_active_ratio"]
        if ratio is not None and (
            ratio >= WORKLOG_RATIO_HIGH or ratio <= WORKLOG_RATIO_LOW
        ):
            severity = (
                "critical"
                if ratio >= 5 or ratio <= 0.2 else "warning"
            )
            direction = "more" if ratio > 1 else "less"
            out.append(_suggestion(
                "jira-worklog-mismatch", "worklog_cross_check",
                f"Logged hours differ from active PR time by {ratio:g}×",
                {
                    "logged_hours": wc["logged_hours"],
                    "active_hours": wc["active_hours"],
                    "ratio": ratio,
                    "stories_with_worklogs": wc["stories_with_worklogs"],
                    "stories_with_prs": wc["stories_with_prs"],
                },
                f"The team logs {ratio:g}× {direction} time than the PRs show "
                "as active work. One of the two numbers is wrong: either the "
                "worklog counts meetings, reviews and waiting, or the PR is the "
                "last ten percent of a long story. Agree what a worklog means "
                "before either is used for planning.",
                severity, 0.0, wc["worklogs"],
            ))

    # 15. jira_conformance — did the process happen in the order it claims?
    jc = m["jira_conformance"]
    if jc["available"] and (
        jc["started_before_refined"] > 0 or jc["points_changed_after_start"] > 0
    ):
        parts = []
        if jc["started_before_refined"]:
            parts.append(
                f"{jc['started_before_refined']} stor"
                f"{'y' if jc['started_before_refined'] == 1 else 'ies'} started "
                "before refinement"
            )
        if jc["points_changed_after_start"]:
            parts.append(
                f"{jc['points_changed_after_start']} re-pointed after start"
            )
        violations = (
            jc["started_before_refined"] + jc["points_changed_after_start"]
        )
        severity = (
            "critical"
            if jc["started_before_refined_share"] >= 0.25 or violations >= 5
            else ("warning" if violations >= 2 else "info")
        )
        out.append(_suggestion(
            "jira-conformance", "jira_conformance",
            "; ".join(parts),
            {
                "stories": jc["stories"],
                "stories_started": jc["stories_started"],
                "started_before_refined": jc["started_before_refined"],
                "started_unrefined": jc["started_unrefined"],
                "points_changed_after_start": jc["points_changed_after_start"],
                "carried_over": jc["carried_over"],
                "pr_opened_before_start": jc["pr_opened_before_start"],
                "hours_per_refinement": rc["hours"],
                "examples": ", ".join(
                    sorted(set(jc["examples"]) | set(jc["unrefined_examples"]))
                ),
            },
            "Run the ticket in the order the process describes: refine and "
            "estimate, then start. Work that begins on an unestimated story "
            "cannot be forecast, and a re-point after the start invalidates "
            "the sprint plan it was built on. If the story is not ready, leave "
            "it in the backlog — starting it is what makes the plan wrong.",
            severity, violations * rc["hours"], jc["stories_started"] or 1,
        ))

    return out


# --------------------------------------------------------------------------- #
# the ranked list
# --------------------------------------------------------------------------- #
def rank(suggestions: list[dict]) -> list[dict]:
    """Order by impact × confidence, then severity, then a stable id."""
    ordered = sorted(
        suggestions,
        key=lambda s: (-s["impact_score"], SEVERITY_RANK.get(s["severity"], 9), s["id"]),
    )
    for index, suggestion in enumerate(ordered, 1):
        suggestion["rank"] = index
    return ordered


def insights(
    court: ballcourt.BallCourt,
    threads: list[dict],
    window: tuple[datetime, datetime],
    level: str = "team",
    generated_at: datetime | None = None,
    bots: set[str] | None = None,
    sizes: dict[str, dict] | None = None,
    jira: jira_analysis.JiraData | None = None,
) -> dict:
    """The API payload: metrics + ranked, evidence-backed suggestions."""
    start, end = window
    computed = metrics(court, threads, window, sizes, jira)
    round_hours = computed["review_cost"]["hours_per_round"]
    suggestions = rank(templates(computed, level, round_hours, bots))
    days = round(max(0.0, (end - start).total_seconds()) / 86400.0, 1)
    return {
        "ok": True,
        "generated_at": iso(generated_at or utcnow()),
        "as_of": iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "assumption": court.assumption,
        **court.calendar_warning(),
        "level": level,
        # prd/21: the ticket layer's availability travels next to the numbers, so
        # a store without the Jira ingest says so instead of reporting zeroes.
        "jira": computed["jira"],
        "window": {"start": iso(start), "end": iso(end), "days": days},
        "sample": {
            "prs": len(cohort(court, window)),
            "days": days,
            "merged": computed["cycle_time"]["n"],
            # product review #5: open PRs cannot contribute a cycle time; the
            # count travels with every cycle-time number so it is never read as
            # a full-population median.
            "open_censored": computed["cycle_time"]["censored"]["open"],
            "declined_censored": computed["cycle_time"]["censored"]["declined"],
            "reviewed": computed["time_to_first_review"]["n"],
            "threads": computed["comment_tax"]["classified"],
            "flow_efficiency": computed["flow_efficiency"]["n"],
            "survival": computed["survival"]["n"],
            # prd/22: how many PRs sit on another PR's branch (0 is a result, not
            # a missing measurement -- see metrics.stacked_cascade.available)
            "stacked_prs": computed["stacked_cascade"]["stacked_prs"],
            # the Jira cohort (stories created in the window) and the population
            # the decomposition could actually measure
            "stories": computed["waiting_time_decomposition"]["stories"],
            "stories_decomposed": computed["waiting_time_decomposition"]["stories_decomposed"],
            "jira_available": computed["jira"]["available"],
        },
        "targets": {
            "first_review_hours": TARGET_FIRST_REVIEW_HOURS,
            "cycle_hours": TARGET_CYCLE_HOURS,
            "max_review_rounds": MAX_REVIEW_ROUNDS,
            "size_commit_cap": SIZE_COMMIT_CAP,
            "size_line_cap": SIZE_LINE_CAP,
            "flow_efficiency": TARGET_FLOW_EFFICIENCY,
            "flow_efficiency_touch_hours": TOUCH_HOURS,
            "hours_per_round": round_hours,
            "hours_per_round_basis": ROUND_COST_BASIS,
            # prd/22: the stack-depth cap the advice names
            "max_stack_depth": stack_analysis.MAX_STACK_DEPTH,
            # prd/21 targets, stated rather than hidden
            "jira_carry_over_share": CARRY_OVER_SHARE_TARGET,
            "jira_point_churn_share": POINT_CHURN_SHARE_TARGET,
            "jira_segment_share": SEGMENT_SHARE_TARGET,
            "jira_segment_share_critical": SEGMENT_SHARE_CRITICAL,
            "jira_worklog_delay_days": WORKLOG_DELAY_TARGET_DAYS,
            "jira_worklog_late_share": WORKLOG_LATE_SHARE_TARGET,
            "jira_worklog_ratio": [WORKLOG_RATIO_LOW, WORKLOG_RATIO_HIGH],
        },
        "metrics": computed,
        "suggestions": suggestions,
        "note": (
            "impact_hours estimates the wait the rule would remove, in "
            f"{court.hours_mode} hours, using the measured review-round cost in "
            "this window; ranking is impact × confidence (sample size). Person "
            "rules carry pseudonyms only. The Jira-layer rules (prd/21) price "
            "ticket waits with the same calendar and report "
            "available: false when the store has no ticket layer. The stacked-PR "
            "rule (prd/22) derives the stack DAG from ref digests -- never branch "
            "names -- and reports available: false when the store predates them."
        ),
    }

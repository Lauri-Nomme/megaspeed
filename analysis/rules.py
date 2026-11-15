"""Analysis → suggested rules (prd/17): the "what should we change first" layer.

The event log already knows *what happened* (``ballcourt`` reconstructs who held
the ball). This module turns that into the product's headline output: a ranked
list of concrete, plain-language changes, each with its evidence and an
estimated cost in hours.

    events/objects ──► metrics ──► rule templates ──► ranked suggestions
                       (prd/17 §Metrics)  (threshold+severity)

Eight metrics are computed from the store exactly as prd/17 lists them:

======================  ==========================================================
``time_to_first_review`` first reviewer action (comment/approval) − ``pr_opened``
``cycle_time``           ``pr_merged`` − ``pr_opened`` (median + p90)
``reviewer_responsiveness``  per person: review-ball vs author-ball hours
``rework_loops``         review rounds per PR, split by ``push_class``
``rebase_waste``         approvals lost where the causing push was ``pure_rebase``
``comment_tax``          threads by ``comment_type`` × ``thread_outcome``
``conformance``          merged with 0 approvals; PRs over N rounds
``sentinel_queue``       median wait on the mandatory approval (when roles exist)
``pr_size``              commits per PR (+ diff size when captured) vs review latency
======================  ==========================================================

Every suggestion is ``{id, metric, title, evidence, suggestion, severity,
impact_hours}`` plus the ranking metadata (``rank``, ``confidence``,
``impact_score``, ``sample``, ``scope``). Ranking is **impact hours ×
confidence**, where confidence is a sample-size ramp (``n / 20``, capped at 1).
Person-level rules carry a pseudonym in ``scope`` and never a raw name — the
store cannot hold one.

Thresholds are v1 defaults, stated in the response and in the UI rather than
hidden: 24 h to first review, 72 h to merge, 3 review rounds, 10 commits or 400
changed lines for the size cap, and one re-review round ≈ the median per-round
share of a PR's cycle in the same window. Diff size is optional: the scrape-side
capture (prd/16) is a separate task, so until ``PR.attributes`` carries
``lines_added``/``lines_removed`` (or ``diff_size``) the size metric uses commit
count alone and reports ``diff_size_pending: true``.

Duration assumption: **wall-clock hours**, inherited from ``ballcourt``. The
working-hours calendar (prd/03) does not exist yet, so every number is elapsed
time and the response says ``hours_mode: "wall-clock"``.

Read-only: this module reads the schema directly, like ``ballcourt``, so the
analysis views never depend on ingest refactors.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta

from analysis import ballcourt
from ingest.store import iso, to_naive_utc, utcnow

ASSUMPTION = ballcourt.ASSUMPTION
HOURS_MODE = "wall-clock"

# v1 targets. Stated in every response that uses them (never a hidden constant).
TARGET_FIRST_REVIEW_HOURS = 24.0
TARGET_CYCLE_HOURS = 72.0
MAX_REVIEW_ROUNDS = 3
# A PR is "large" past either cap. 400 changed lines is the common review-size
# guideline; commits are the coarse proxy until diff size is captured.
SIZE_COMMIT_CAP = 10
SIZE_LINE_CAP = 400
# Confidence ramps from 0 to 1 over this many observations (prd/17 ranking).
CONFIDENCE_SAMPLE = 20
DEFAULT_ROUND_HOURS = 24.0

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

REVIEW_ACTIONS = ("comment_added", "pr_approved")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _hours(seconds: float) -> float:
    return round(max(0.0, seconds) / 3600.0, 2)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 2)


def _p90(values: list[float]) -> float | None:
    """Nearest-rank p90: the value at or above 90% of the sample."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1))
    return round(ordered[index], 2)


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


def window_for(court: ballcourt.BallCourt, days: float | None = None) -> tuple[datetime, datetime]:
    """The analysis window: PRs *opened* inside it are the cohort.

    Unlike ``ballcourt.window_from_events`` this includes the earliest PR
    creation, so a PR with no events yet still belongs to an "all time" window.
    """
    first_pr = min((s.pr["created_ts"] for s in court.prs), default=None)
    first_interval = min((i.start for i in court.intervals), default=None)
    first = min((t for t in (first_pr, first_interval) if t is not None), default=court.snapshot)
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
            _hours((pr["closed_ts"] - pr["created_ts"]).total_seconds()) / rounds
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
        hours = _hours((first["_ts"] - pr["created_ts"]).total_seconds())
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
    """``pr_merged.ts − pr_opened.ts`` for PRs merged in the cohort."""
    samples: list[float] = []
    for state in cohort(court, window):
        pr = state.pr
        if pr["state"] != "merged" or pr["closed_ts"] is None:
            continue
        samples.append(_hours((pr["closed_ts"] - pr["created_ts"]).total_seconds()))
    return {
        "n": len(samples),
        "median_hours": _median(samples),
        "p90_hours": _p90(samples),
        "target_hours": TARGET_CYCLE_HOURS,
        "over_target": sum(1 for h in samples if h > TARGET_CYCLE_HOURS),
    }


def reviewer_responsiveness(court: ballcourt.BallCourt, window) -> dict:
    """Per person: hours the ball sat in *their* review queue vs on their PRs.

    Review-ball hours are attributed to the reviewer whose action ended the
    wait (a measured fact: "this wait ended with that person"), never to a
    roster assignment. Author-ball hours are the intervals where the person
    held the ball on their own PR. "Prefers own work" is the contrast: review
    latency high while their own cycle is low.
    """
    start, end = window
    review: dict[str, list[float]] = defaultdict(list)
    author: dict[str, list[float]] = defaultdict(list)
    for interval in court.intervals:
        if not _overlap(interval, start, end):
            continue
        if interval.ball == ballcourt.REVIEW and interval.ball_person:
            review[interval.ball_person].append(interval.hours)
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
                _hours((first["_ts"] - pr["created_ts"]).total_seconds())
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
        if interval.ball == ballcourt.REVIEW and interval.ball_person in sentinels:
            waits[interval.ball_person].append(interval.hours)
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


def metrics(court: ballcourt.BallCourt, threads: list[dict], window,
            sizes: dict[str, dict] | None = None) -> dict:
    """Every prd/17 metric, in one deterministic pass."""
    return {
        "time_to_first_review": time_to_first_review(court, window),
        "cycle_time": cycle_time(court, window),
        "reviewer_responsiveness": reviewer_responsiveness(court, window),
        "rework_loops": rework_loops(court, window),
        "rebase_waste": rebase_waste(court, window),
        "comment_tax": comment_tax(court, threads, window),
        "conformance": conformance(court, window),
        "sentinel_queue": sentinel_queue(court, window),
        "pr_size": pr_size(court, window, sizes),
        "review_cost": {"hours_per_round": round_cost_hours(court, window)},
    }


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
            },
            "Cap work in progress and split large PRs: a PR that opens and merges inside "
            "72 h stops review and rebase waits from compounding.",
            severity, (cyc["median_hours"] - TARGET_CYCLE_HOURS) * cyc["n"], cyc["n"],
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
            f"{cf['merged_without_approval']} PRs merged with no approval",
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
            f"{cf['prs_over_max_rounds']} PRs ran past {cf['max_rounds']} review rounds",
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
) -> dict:
    """The API payload: metrics + ranked, evidence-backed suggestions."""
    start, end = window
    computed = metrics(court, threads, window, sizes)
    round_hours = computed["review_cost"]["hours_per_round"]
    suggestions = rank(templates(computed, level, round_hours, bots))
    days = round(max(0.0, (end - start).total_seconds()) / 86400.0, 1)
    return {
        "ok": True,
        "generated_at": iso(generated_at or utcnow()),
        "as_of": iso(court.snapshot),
        "hours_mode": HOURS_MODE,
        "assumption": ASSUMPTION,
        "level": level,
        "window": {"start": iso(start), "end": iso(end), "days": days},
        "sample": {
            "prs": len(cohort(court, window)),
            "days": days,
            "merged": computed["cycle_time"]["n"],
            "reviewed": computed["time_to_first_review"]["n"],
            "threads": computed["comment_tax"]["classified"],
        },
        "targets": {
            "first_review_hours": TARGET_FIRST_REVIEW_HOURS,
            "cycle_hours": TARGET_CYCLE_HOURS,
            "max_review_rounds": MAX_REVIEW_ROUNDS,
            "size_commit_cap": SIZE_COMMIT_CAP,
            "size_line_cap": SIZE_LINE_CAP,
            "hours_per_round": round_hours,
            "hours_per_round_basis": ROUND_COST_BASIS,
        },
        "metrics": computed,
        "suggestions": suggestions,
        "note": (
            "impact_hours estimates the wait the rule would remove, using wall-clock "
            "hours and the measured review-round cost in this window; ranking is "
            "impact × confidence (sample size). Person rules carry pseudonyms only."
        ),
    }

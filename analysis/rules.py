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
``waiting_time_decomposition``  prd/21: the ticket segments, hours + efficiency each
                         (prd/29 splits merge→done around verification)
``carry_over``           prd/21: stories added to more than one sprint
``point_churn``          prd/21: ``pointed`` events after the story started
``worklog_cross_check``  prd/21: ``log_delay_bucket`` + logged vs active hours
``jira_conformance``     prd/21: started before refined, re-pointed after start
======================  ==========================================================

Every suggestion is ``{id, metric, title, evidence, suggestion, severity,
impact_hours}`` plus the ranking metadata (``rank``, ``confidence``,
``impact_score``, ``sample``, ``scope``). Each metric that measures items also
reports them: ``metrics.<name>.rows`` is the per-item list (a PR, a story or a
thread) with the numbers that metric used, its ``object_id`` and the ``tickets``
it names, so the Rules page can expand a suggestion into the items behind it
instead of stopping at a count (prd/26 P5). Ranking is **impact hours ×
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

from analysis import advice
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
#: prd/46 §3: the *advice* target is the cap the policy lab's best scenario
#: uses (2 rounds) — a cap of 3 binds no PR in the 90-day cohort, so the rule
#: must not recommend it. The conformance metric keeps ``MAX_REVIEW_ROUNDS`` (3)
#: as its own, older threshold; the payload names both.
REVIEW_ROUND_CAP_TARGET = 2
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
# guideline; commits are the coarse proxy until diff size is captured. These are
# the *defaults*: prd/39 makes the caps a per-tenant setting, so every metric and
# rule takes a ``caps`` override and reports the caps it used.
SIZE_COMMIT_CAP = 10
SIZE_LINE_CAP = 400
# The API validates the same bounds; the analysis clamps defensively so a caller
# that bypasses the endpoint cannot turn a typo into "every PR is large".
SIZE_CAP_MIN = 1
SIZE_CAP_MAX = 10_000
# A histogram is for reading a knee, not for storing every value: ~18 buckets
# over [0, max] with the cap forced onto an edge, so the marker sits on a bar
# boundary. The raw sorted values still travel in the payload for exact re-bucketing.
SIZE_HISTOGRAM_BUCKETS = 18
# Confidence ramps from 0 to 1 over this many observations (prd/17 ranking).
CONFIDENCE_SAMPLE = 20
DEFAULT_ROUND_HOURS = 24.0

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

REVIEW_ACTIONS = ("comment_added", "pr_approved")

# prd/21: what to change when one segment of the ticket cycle dominates. The
# advice is per segment, because "wait less" is not a rule.
SEGMENT_ADVICE = {
    "created_ready": (
        "Refine before the sprint, not during it: a story that waits for a "
        "refinement slot is not ready to be committed, so the wait turns into "
        "a carry-over. Refine a story only when the team is ready to size it "
        "and write its acceptance criteria in one sitting."
    ),
    "ready_sprint": (
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
        "Cap review rounds: if a PR needs a fourth pass, the author and reviewer "
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
    # prd/29: the verification split. "merge → verification" is the wait for QA
    # to pick the ticket up; "verification → done" is the QA time itself.
    "merge_verification": (
        "Hand the merge straight to verification: a merged PR with no QA owner "
        "waits for someone to notice it. Put the QA item in the verifier's "
        "queue at merge time, with the build to check and what 'verified' means."
    ),
    "verification_done": (
        "Give verification a service level: a ticket in QA for longer than the "
        "change takes to test means the QA queue, not the test, is the "
        "bottleneck. Cap the queue in verification and test against the "
        "acceptance criteria the refinement already wrote."
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


def _confidence(n: int) -> float:
    """Sample-size confidence in [0.05, 1]: a 20-observation metric is trusted."""
    return round(max(0.05, min(1.0, max(0, n) / CONFIDENCE_SAMPLE)), 2)


def _cap(value, default: int) -> int:
    """One validated size cap: an int in ``[SIZE_CAP_MIN, SIZE_CAP_MAX]``."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if isinstance(value, bool):            # True is not a cap
        return default
    return max(SIZE_CAP_MIN, min(SIZE_CAP_MAX, number))


def size_caps(caps: dict | None = None) -> dict:
    """The PR-size caps a metric/rule evaluates against (prd/39).

    ``None`` or a missing key means the v1 default, so every existing caller
    keeps the old 10 / 400 behaviour; the endpoint passes the tenant's chosen
    caps and the rule re-evaluates against them. The response always carries the
    caps it used, so a number can never be read against the wrong threshold.
    """
    caps = caps or {}
    return {
        "commits": _cap(caps.get("commits"), SIZE_COMMIT_CAP),
        "lines": _cap(caps.get("lines"), SIZE_LINE_CAP),
    }


def default_size_caps() -> dict:
    """The stated v1 defaults, for the UI's "reset" and the API payload."""
    return {"commits": SIZE_COMMIT_CAP, "lines": SIZE_LINE_CAP}


#: concepts.md §1 (prd/35): one `d` is 8 business hours — one working day,
#: never 24 h and never a calendar day. One definition (`ballcourt`), re-exported
#: here for the rule formatter; never a second literal.
BUSINESS_HOURS_PER_DAY = ballcourt.BUSINESS_HOURS_PER_DAY
#: concepts.md §1: a working week is 5 d = 40 business hours. The one threshold
#: behind every "waiting over a week" label and the critical-severity gate.
WAITING_WEEK_HOURS = ballcourt.WAITING_WEEK_HOURS


def _human_hours(hours: float | None) -> str:
    """Plain-language duration for titles: "41 h", "3 d 2 h" (d = 8 business hours).

    prd/55 §3: one rendering for one number — this delegates to the shared
    ``jira.human_hours`` (the Python twin of ``fmtHours``), so a rule title can
    never say "3.4 d" where the metric block says "3 d 2 h".
    """
    return jira_analysis.human_hours(hours)


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
    """The events that count as review rounds (``concepts.md`` §6, prd/40).

    One definition for the whole product: after the first review, excluding a
    ``pure_rebase`` that lost no approval and excluding a push-induced
    ``pr_unapproved`` (the same round as its push). ``ballcourt`` owns it so the
    ticket view (``analysis/jira``) cannot drift from the rules page.
    """
    return ballcourt.review_round_events(state)


def _pr_row(pr: dict, **extra) -> dict:
    """One PR as a drill-down item (prd/26 P5).

    Every metric that measures PRs reports its items in this one shape, so the
    Rules page can expand any suggestion into the *same* dense table and link
    each row: ``object_id`` is the store's identity, ``tickets`` are the keys
    the PR names (the UI links the PR's own story page), and
    ``repo``/``number``/``id`` are its label. A metric adds its own numbers on
    top (``hours``, ``rounds``, ``over_target``, …) and marks the rows the rule
    is about with ``affected``.
    """
    row = {
        "kind": "pr",
        "object_id": pr["object_id"],
        "repo": pr["repo"],
        "number": pr["pr_id"],
        "id": f"{pr['repo'].split('/')[-1]}#{pr['pr_id']}",
        "tickets": list(pr.get("ticket_keys") or ()),
        "state": pr["state"] if pr["closed_ts"] else "open",
    }
    row.update(extra)
    return row


def _drill_order(rows: list[dict]) -> list[dict]:
    """Impact first: wait-hours descending, then affected, then a stable identity.

    concepts.md §6 / prd/37 §3: a drill-down is "the items behind the number",
    and the worst item must be the first row — sorted by the wait it carries,
    never by key. ``affected`` breaks ties in favour of the items the rule is
    about, so a metric that mixes in unaffected context rows still leads with
    the evidence.
    """
    rows.sort(key=lambda row: (
        -float(row.get("hours") or row.get("wait_hours") or row.get("slip_hours") or 0.0),
        not row.get("affected", False),
        str(row.get("id") or row.get("key") or row.get("object_id") or ""),
    ))
    return rows


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


def _merged_round_pairs(court: ballcourt.BallCourt, window) -> list[tuple[float, float]]:
    """(rounds, cycle hours) for merged PRs in the window with a cycle."""
    pairs: list[tuple[float, float]] = []
    for state in cohort(court, window):
        pr = state.pr
        if pr["closed_ts"] is None or pr["state"] != "merged":
            continue
        pushes, unapprovals = _rounds(state)
        rounds = len(pushes) + len(unapprovals)
        if pr["closed_ts"] <= pr["created_ts"]:
            continue
        pairs.append((
            float(rounds),
            _span_hours(court, None, pr["created_ts"], pr["closed_ts"]),
        ))
    return pairs


def _slope(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None
    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    variance = sum((value - mean_x) ** 2 for value in xs)
    if variance <= 0:
        return None
    return round(
        sum((x - mean_x) * (y - mean_y) for x, y in pairs) / variance, 2
    )


ROUND_COST_BASIS = "median PR cycle time ÷ its review rounds"
ROUND_SLOPE_BASIS = (
    "marginal hours per extra round (regression over merged PRs) — the same "
    "basis as 'each extra round adds' on the round-cap rule"
)


def review_cost(court: ballcourt.BallCourt, window) -> dict:
    """One price for a review round, on one basis (prd/55 §3).

    The Rules page used to quote two numbers for the same concept: the target
    line's "one review round ≈ 1 d 6 h" (median cycle ÷ its rounds) and the
    round-cap card's "each extra round adds ≈ 6 h" (the regression slope). The
    slope is the number the rule actually ranks on, so it is the number the
    target line quotes too; the median-per-round figure remains the fallback
    when there are too few merged PRs for a slope.
    """
    pairs = _merged_round_pairs(court, window)
    slope = _slope(pairs)
    if slope is not None and slope > 0:
        return {
            "hours_per_round": max(1.0, slope),
            "basis": ROUND_SLOPE_BASIS,
            "n": len(pairs),
        }
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
        return {
            "hours_per_round": max(1.0, _median(per_round) or DEFAULT_ROUND_HOURS),
            "basis": ROUND_COST_BASIS,
            "n": len(per_round),
        }
    start, end = window
    waits = [
        i.hours for i in court.intervals
        if i.ball == ballcourt.REVIEW and _overlap(i, start, end)
    ]
    return {
        "hours_per_round": max(1.0, _median(waits) or DEFAULT_ROUND_HOURS),
        "basis": "median review-queue wait (no closed PR with rounds yet)",
        "n": len(waits),
    }


def round_cost_hours(court: ballcourt.BallCourt, window: tuple[datetime, datetime]) -> float:
    """What one review round costs, in elapsed hours (one basis, see above)."""
    return review_cost(court, window)["hours_per_round"]


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

    One implementation for the whole product (``ballcourt.load_bots``): the
    ball-court uses it to keep bots out of every holder set, and the rules use
    it to keep them out of the person-level aggregates. A bot's queue is not a
    person to coach, and its waits are not a human's.
    """
    return ballcourt.load_bots(con)


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
    """``min(first reviewer comment/approval) − pr_opened`` per PR.

    ``by_reviewer`` is a person aggregate, so bots are excluded from it
    (concepts.md §2); their PRs still enter the sample, because the wait a
    human caused (or waited through) is a fact.
    """
    samples: list[float] = []
    by_reviewer: dict[str, list[float]] = defaultdict(list)
    missing = 0
    rows: list[dict] = []
    for state in cohort(court, window):
        pr = state.pr
        first = _first_review(state)
        if first is None:
            missing += 1
            rows.append(_pr_row(
                pr, reviewer=None, first_review_hours=None, over_target=False,
                missing_review=True, affected=False,
            ))
            continue
        hours = _span_hours(court, first["actor"], pr["created_ts"], first["_ts"])
        samples.append(hours)
        if first["actor"] not in court.bots:
            by_reviewer[first["actor"]].append(hours)
        rows.append(_pr_row(
            pr, reviewer=first["actor"], first_review_hours=hours,
            over_target=hours > TARGET_FIRST_REVIEW_HOURS,
            missing_review=False, affected=hours > TARGET_FIRST_REVIEW_HOURS,
        ))
    return {
        "n": len(samples),
        "missing": missing,
        "median_hours": _median(samples),
        "p90_hours": _p90(samples),
        "target_hours": TARGET_FIRST_REVIEW_HOURS,
        "over_target": sum(1 for h in samples if h > TARGET_FIRST_REVIEW_HOURS),
        "bots_excluded": sorted(court.bots),
        "by_reviewer": {
            person: {"n": len(values), "median_hours": _median(values)}
            for person, values in sorted(by_reviewer.items())
        },
        # prd/26 P5: the items behind the number, so "16 PRs past 3 rounds" can
        # be opened into the 16 PRs.
        "rows": _drill_order(rows),
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
    rows: list[dict] = []
    states = cohort(court, window)
    for state in states:
        pr = state.pr
        if pr["closed_ts"] is None:
            hours = _span_hours(court, None, pr["created_ts"], court.snapshot)
            open_ages.append(hours)
            rows.append(_pr_row(
                pr, hours=hours, censored=True, declined=False, merged=False,
                over_target=hours > TARGET_CYCLE_HOURS,
                affected=hours > TARGET_CYCLE_HOURS,
            ))
            continue
        hours = _span_hours(court, None, pr["created_ts"], pr["closed_ts"])
        if pr["state"] != "merged":
            declined += 1
            rows.append(_pr_row(
                pr, hours=hours, censored=True, declined=True, merged=False,
                over_target=False, affected=False,
            ))
            continue
        samples.append(hours)
        rows.append(_pr_row(
            pr, hours=hours, censored=False, declined=False, merged=True,
            over_target=hours > TARGET_CYCLE_HOURS,
            affected=hours > TARGET_CYCLE_HOURS,
        ))
    return {
        "n": len(samples),
        "median_hours": _median(samples),
        "p90_hours": _p90(samples),
        "target_hours": TARGET_CYCLE_HOURS,
        "over_target": sum(1 for h in samples if h > TARGET_CYCLE_HOURS),
        "cohort": len(states),
        "rows": _drill_order(rows),
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
            "kind": "pr",
            "object_id": pr["object_id"],
            "id": f"{pr['repo'].split('/')[-1]}#{pr['pr_id']}",
            "repo": pr["repo"],
            "number": pr["pr_id"],
            # prd/26 P5: the drill-down links the PR's own story page
            "tickets": list(pr.get("ticket_keys") or ()),
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
            # one canonical count: the PR's COMMIT objects. Summing the
            # intervals' commits dropped a commit that landed at the open
            # instant (prd/43 §5: one PR, one set of numbers).
            "commits": len(state.commits),
            # the same "excess waiting" the flow-efficiency rule prices, per PR
            "affected": bool(
                elapsed > 0
                and waiting > elapsed * (1 - TARGET_FLOW_EFFICIENCY)
            ),
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
    rows: list[dict] = []
    for state in states:
        pr = state.pr
        if pr["closed_ts"] is None:
            hours = _span_hours(court, None, pr["created_ts"], court.snapshot)
            open_ages.append(hours)
            open_excess += max(0.0, hours - TARGET_CYCLE_HOURS)
            observations.append(Observation(hours, False, pr["object_id"]))
            rows.append(_pr_row(
                pr, hours=hours, event=False, censored=True, declined=False,
                over_target=hours > TARGET_CYCLE_HOURS,
                affected=hours > TARGET_CYCLE_HOURS,
            ))
            continue
        hours = _span_hours(court, None, pr["created_ts"], pr["closed_ts"])
        if pr["state"] == "merged":
            merged.append(hours)
            observations.append(Observation(hours, True, pr["object_id"]))
            rows.append(_pr_row(
                pr, hours=hours, event=True, censored=False, declined=False,
                over_target=hours > TARGET_CYCLE_HOURS,
                affected=hours > TARGET_CYCLE_HOURS,
            ))
        else:
            declined += 1
            observations.append(Observation(hours, False, pr["object_id"]))
            rows.append(_pr_row(
                pr, hours=hours, event=False, censored=True, declined=True,
                over_target=False, affected=False,
            ))

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
        "rows": _drill_order(rows),
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


def _episodes(court: ballcourt.BallCourt, window, kind: str = "review") -> list[dict]:
    """Per-``(person, PR)`` wait *episodes*, not per interval (prd/40, S12).

    One sample per consecutive run of intervals in which the person is a
    holder. Every event by anyone splits an interval, so a 30 h wait on one
    reviewer used to become six samples of 0–18 h (≈10× understated median);
    the drill-down in this module already aggregated per ``(person, PR)``, the
    headline median did not.

    ``kind="review"`` uses the pending set (every holder who is not the
    author); ``kind="author"`` uses the author's own open debt. The hours are
    priced on the holder's calendar. Bots are not people (concepts.md §2): the
    holder sets already exclude them, and a bot *author* is skipped here too.
    """
    start, end = window
    by_pr: dict[str, list] = defaultdict(list)
    for interval in court.intervals:
        if _overlap(interval, start, end):
            by_pr[interval.object_id].append(interval)
    out: list[dict] = []
    for object_id, intervals in by_pr.items():
        intervals.sort(key=lambda i: i.start)
        open_episodes: dict[str, dict] = {}
        for interval in intervals:
            if kind == "author":
                people = (
                    [interval.author]
                    if interval.author_open and interval.author not in court.bots
                    else []
                )
            else:
                people = [p for p in interval.person_holders if p != interval.author]
            current = set(people)
            for person in list(open_episodes):
                if person not in current:
                    out.append(open_episodes.pop(person))
            for person in current:
                entry = open_episodes.get(person)
                if entry is None:
                    entry = open_episodes[person] = {
                        "person": person,
                        "object_id": object_id,
                        "hours": 0.0,
                        "intervals": 0,
                        "longest_hours": 0.0,
                        "first_ts": interval.start,
                        "last_ts": interval.end,
                    }
                hours = interval.hours_for(person)
                entry["hours"] += hours
                entry["intervals"] += 1
                entry["longest_hours"] = max(entry["longest_hours"], hours)
                entry["last_ts"] = interval.end
        out.extend(open_episodes.values())
    return out


def _empty_wait() -> dict:
    return {"hours": 0.0, "required_hours": 0.0, "unrequired_hours": 0.0,
            "waits": 0, "required_waits": 0, "longest_hours": 0.0}


def reviewer_responsiveness(court: ballcourt.BallCourt, window) -> dict:
    """Per person: hours the ball sat in *their* review queue vs on their PRs.

    Review-ball hours are attributed to **every reviewer the ball was
    concurrently with** — the pending roster from ``ballcourt`` (prd/40) — not
    only to whoever happened to answer first, and priced on that reviewer's own
    calendar. Samples are **episodes**, not intervals (S12), so one long wait is
    one sample however many events split it.

    A reviewer still pending when the PR merged was, ex post, not required
    (S7): their episodes are tagged ``unrequired`` and left out of the median
    and the flag, but still listed.
    """
    start, end = window
    unrequired = ballcourt.pending_at_close(court)
    review: dict[str, list[float]] = defaultdict(list)
    author: dict[str, list[float]] = defaultdict(list)
    unrequired_by_person: dict[str, dict] = defaultdict(_empty_wait)
    pr_meta = {state.pr["object_id"]: state.pr for state in court.prs}
    # prd/26 P5: the waits behind a reviewer rule, aggregated per (person, PR)
    # — the PR is the item an investigator opens, not each interval.
    waits: dict[tuple[str, str], dict] = {}
    for episode in _episodes(court, window, "review"):
        person = episode["person"]
        object_id = episode["object_id"]
        entry = waits.get((person, object_id))
        if entry is None:
            entry = waits[(person, object_id)] = {
                "person": person,
                "object_id": object_id,
                **_empty_wait(),
                "first_ts": episode["first_ts"],
                "last_ts": episode["last_ts"],
            }
        entry["hours"] += episode["hours"]
        entry["waits"] += 1
        entry["first_ts"] = min(entry["first_ts"], episode["first_ts"])
        entry["last_ts"] = max(entry["last_ts"], episode["last_ts"])
        if person in unrequired.get(object_id, ()):
            entry["unrequired_hours"] += episode["hours"]
            unrequired_by_person[person]["hours"] += episode["hours"]
            unrequired_by_person[person]["waits"] += 1
        else:
            entry["required_hours"] += episode["hours"]
            entry["required_waits"] += 1
            entry["longest_hours"] = max(entry["longest_hours"], episode["hours"])
            review[person].append(episode["hours"])
    for episode in _episodes(court, window, "author"):
        author[episode["person"]].append(episode["hours"])

    rows: list[dict] = []
    for entry in waits.values():
        pr = pr_meta.get(entry["object_id"])
        if pr is None:
            continue
        rows.append(_pr_row(
            pr, person=entry["person"], ball=ballcourt.REVIEW,
            hours=round(entry["hours"], 2), waits=entry["waits"],
            required_hours=round(entry["required_hours"], 2),
            unrequired_hours=round(entry["unrequired_hours"], 2),
            unrequired=entry["required_waits"] == 0,
            longest_hours=round(entry["longest_hours"], 2),
            from_ts=iso(entry["first_ts"]), to_ts=iso(entry["last_ts"]),
            affected=entry["longest_hours"] >= TARGET_FIRST_REVIEW_HOURS,
        ))

    people = []
    for person in sorted(set(review) | set(author) | set(unrequired_by_person)):
        if person in court.bots:
            continue
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
            "unrequired_n": unrequired_by_person[person]["waits"],
            "unrequired_hours": round(unrequired_by_person[person]["hours"], 2),
            "flagged": flagged,
        })

    review_medians = [p["review_median_hours"] for p in people if p["review_median_hours"] is not None]
    all_review = [h for values in review.values() for h in values]
    all_author = [h for values in author.values() for h in values]
    unrequired_episodes = sum(v["waits"] for v in unrequired_by_person.values())
    unrequired_hours = sum(v["hours"] for v in unrequired_by_person.values())
    return {
        "people": people,
        "flagged": [p["person"] for p in people if p["flagged"]],
        "reviewers": len(review),
        "bots_excluded": sorted(court.bots),
        "review_n": len(all_review),
        "review_median_hours": _median(all_review),
        "author_n": len(all_author),
        "author_median_hours": _median(all_author),
        "unrequired_n": unrequired_episodes,
        "unrequired_hours": round(unrequired_hours, 2),
        "unrequired_note": (
            "reviewers still pending when the PR merged were not required "
            "(listed, not blamed); their episodes are excluded from the median"
        ),
        "spread_hours": (
            round(max(review_medians) - min(review_medians), 2)
            if len(review_medians) >= 2 else None
        ),
        "rows": _drill_order(rows),
    }



def rework_loops(court: ballcourt.BallCourt, window) -> dict:
    """Review rounds per PR, by push class (``concepts.md`` §6, prd/40).

    prd/43 §1: the metric also measures whether rounds *explain* the cycle —
    Pearson ``r`` and the marginal hours per extra round over **merged** PRs
    with a measured cycle. That is the evidence the review-round-cap rule
    ranks on (the QA measured r ≈ 0.6, the strongest of the size levers).
    """
    rounds: list[int] = []
    by_class: dict[str, dict] = defaultdict(lambda: {"pushes": 0, "prs": set()})
    rows: list[dict] = []
    excluded_pure_rebase = 0
    excluded_push_unapprovals = 0
    excluded_pre_first_review = 0
    # (rounds, cycle hours): merged PRs only — an open PR's cycle is censored,
    # and a declined one is not delivered work (same cohort as pr_size's r).
    pairs: list[tuple[int, float]] = []
    cycle_by_rounds: dict[int, list[float]] = defaultdict(list)
    merged_rounds: list[int] = []
    for state in cohort(court, window):
        pushes, unapprovals = _rounds(state)
        count = len(pushes) + len(unapprovals)
        rounds.append(count)
        pr = state.pr
        if pr["state"] == "merged" and pr["closed_ts"] is not None:
            merged_rounds.append(count)
            cycle = _span_hours(court, None, pr["created_ts"], pr["closed_ts"])
            pairs.append((count, cycle))
            cycle_by_rounds[count].append(cycle)
        # what the old #pr_pushed + #pr_unapproved would have counted, so the
        # correction is visible instead of looking like missing data
        first = ballcourt.first_review(state)
        for event in state.events:
            activity = event["activity"]
            if activity not in ("pr_pushed", "pr_unapproved"):
                continue
            if first is None or event["_ts"] < first["_ts"]:
                excluded_pre_first_review += 1
                continue
            attrs = event.get("attributes") or {}
            if activity == "pr_pushed":
                if attrs.get("push_class") == "pure_rebase" and not attrs.get("approvals_lost"):
                    excluded_pure_rebase += 1
            elif attrs.get("cause") == "push":
                excluded_push_unapprovals += 1
        classes = set()
        for event in pushes:
            push_class = _push_class(event)
            by_class[push_class]["pushes"] += 1
            classes.add(push_class)
        for push_class in classes:
            by_class[push_class]["prs"].add(state.pr["object_id"])
        rows.append(_pr_row(
            state.pr, rounds=count, pushes=len(pushes),
            unapprovals=len(unapprovals), push_classes=sorted(classes),
            over_max_rounds=count > MAX_REVIEW_ROUNDS,
            affected=count > MAX_REVIEW_ROUNDS,
        ))
    xs = [float(value) for value, _ in pairs]
    ys = [cycle for _, cycle in pairs]
    correlation = _pearson(xs, ys)
    slope: float | None = None
    if len(pairs) >= 3:
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        variance = sum((value - mean_x) ** 2 for value in xs)
        if variance > 0:
            slope = round(
                sum((x - mean_x) * (y - mean_y) for x, y in pairs) / variance, 2
            )
    return {
        "n": len(rounds),
        "total_rounds": sum(rounds),
        "mean_rounds": round(sum(rounds) / len(rounds), 2) if rounds else None,
        "median_rounds": _median([float(r) for r in rounds]),
        # prd/55 §3: the headline "median rounds 1" counted open/declined PRs
        # while the r and the slope are merged-only; the merged median is 2.
        # Both travel with an explicit basis, and the rule quotes the merged one.
        "median_rounds_merged": _median([float(r) for r in merged_rounds]),
        "median_rounds_all": _median([float(r) for r in rounds]),
        "rounds_median_basis": (
            "median over merged PRs (the same cohort as r and the slope); "
            "median_rounds_all includes open and declined PRs"
        ),
        "p90_rounds": _p90([float(r) for r in rounds]),
        # prd/46 §3: the advice target is the lab's best-scenario cap (2); the
        # conformance threshold (3) is reported next to it, never instead of it.
        "prs_over_target": sum(1 for r in rounds if r > REVIEW_ROUND_CAP_TARGET),
        "prs_over_max": sum(1 for r in rounds if r > MAX_REVIEW_ROUNDS),
        "excess_rounds": sum(max(0, r - REVIEW_ROUND_CAP_TARGET) for r in rounds),
        "target_rounds": REVIEW_ROUND_CAP_TARGET,
        "max_rounds": MAX_REVIEW_ROUNDS,
        # prd/43 §1: is the round count the lever? Measured on merged PRs only.
        "rounds_cycle_n": len(pairs),
        "rounds_cycle_correlation": correlation,
        "rounds_cycle_slope_hours": slope,
        "median_cycle_by_rounds": {
            str(count): _median(values)
            for count, values in sorted(cycle_by_rounds.items())
        },
        "rounds_cycle_basis": (
            "Pearson r between a PR's review rounds (concepts.md §6) and its "
            "created → merged cycle, over MERGED PRs with a measured cycle; "
            "the slope is the marginal hours per extra round"
        ),
        "excluded_pure_rebase": excluded_pure_rebase,
        "excluded_push_unapprovals": excluded_push_unapprovals,
        "excluded_pre_first_review": excluded_pre_first_review,
        "round_note": (
            "rounds = #pr_pushed (after first review, excluding a pure rebase "
            "that lost no approval) + #pr_unapproved with cause != push "
            "(after first review); the excluded events are counted above"
        ),
        "rows": _drill_order(rows),
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
    rows: list[dict] = []
    for state in cohort(court, window):
        last_push_class: str | None = None
        lost_here: dict[str, int] = defaultdict(int)
        for event in state.events:
            activity = event["activity"]
            if activity == "pr_pushed":
                last_push_class = _push_class(event)
            elif activity == "pr_unapproved":
                unapprovals += 1
                prs.add(state.pr["object_id"])
                attributes = event.get("attributes") or {}
                cause = "manual" if attributes.get("cause") == "manual" else (last_push_class or "other")
                lost[cause] += 1
                lost_here[cause] += 1
        if lost_here:
            # prd/26 P5: only the PRs that actually lost an approval — the
            # items the "don't rebase approved PRs" rule is about.
            rows.append(_pr_row(
                state.pr, unapprovals=sum(lost_here.values()),
                lost=dict(sorted(lost_here.items())),
                lost_on_pure_rebase=lost_here.get("pure_rebase", 0),
                affected=True,
            ))
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
        "rows": _drill_order(rows),
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
    pr_meta = {
        f"PR:{state.pr['repo']}#{state.pr['pr_id']}": state.pr for state in court.prs
    }
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_type: dict[str, dict] = defaultdict(lambda: {"threads": 0, "wasted": 0})
    outcomes: dict[str, int] = defaultdict(int)
    considered = 0
    classified = 0
    wasted = 0
    rows: list[dict] = []
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
        wasted_here = outcome in ("rejected", "walked_back")
        if wasted_here:
            wasted += 1
            by_type[comment_type]["wasted"] += 1
        pr = pr_meta.get(f"PR:{thread.get('repo')}#{thread['pr_id']}")
        rows.append({
            "kind": "thread",
            "object_id": thread["object_id"],
            "repo": thread.get("repo"),
            "number": thread.get("pr_id"),
            "id": f"{(thread.get('repo') or '').split('/')[-1]}#{thread.get('pr_id')}",
            # the PR's own keys, so the row links the ticket that owns the thread
            "tickets": list((pr or {}).get("ticket_keys") or ()),
            "comment_type": comment_type,
            "outcome": outcome,
            "wasted": wasted_here,
            "affected": wasted_here,
            "created_at": iso(thread["created_at"]) if thread.get("created_at") else None,
        })
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
        "rows": _drill_order(rows),
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
    rows: list[dict] = []
    for state in cohort(court, window):
        approvals = [e for e in state.events if e["activity"] == "pr_approved"]
        pushes, unapprovals = _rounds(state)
        rounds = len(pushes) + len(unapprovals)
        unapproved = state.pr["state"] == "merged" and not approvals
        if state.pr["state"] == "merged":
            merged += 1
            if unapproved:
                without_approval.append(state.pr["object_id"])
        if rounds > MAX_REVIEW_ROUNDS:
            over_rounds.append((state.pr["object_id"], rounds))
        if unapproved or rounds > MAX_REVIEW_ROUNDS:
            rows.append(_pr_row(
                state.pr, merged=state.pr["state"] == "merged",
                approvals=len(approvals), rounds=rounds,
                merged_without_approval=unapproved,
                over_max_rounds=rounds > MAX_REVIEW_ROUNDS,
                affected=True,
            ))
    return {
        "n": len(cohort(court, window)),
        "merged": merged,
        "merged_without_approval": len(without_approval),
        "prs_over_max_rounds": len(over_rounds),
        "excess_rounds": sum(rounds - MAX_REVIEW_ROUNDS for _, rounds in over_rounds),
        "max_rounds": MAX_REVIEW_ROUNDS,
        "examples": sorted(without_approval)[:5],
        "rows": _drill_order(rows),
    }


def pr_size(court: ballcourt.BallCourt, window,
            sizes: dict[str, dict] | None = None,
            caps: dict | None = None) -> dict:
    """Commits per PR (COMMIT objects) and diff size when captured, vs latency.

    Diff size is optional: ``sizes`` comes from :func:`load_pr_sizes`, which
    returns an entry only when the PR object carries ``lines_added`` /
    ``lines_removed`` or ``diff_size``. Until the scrape-side capture lands the
    metric is honest about it (``diff_size.pending``) and the size cap is on
    commits alone.

    prd/39: ``caps`` is the tenant's calibration (``{"commits": n, "lines": n}``);
    the metric and the rule that reads it use exactly these caps, and the payload
    reports them next to the defaults. A PR is large iff ``commits > cap`` or
    ``changed_lines > line_cap`` — the same definition :func:`pr_size_distribution`
    draws.
    """
    sizes = sizes or {}
    chosen = size_caps(caps)
    commit_cap = chosen["commits"]
    line_cap = chosen["lines"]
    rows: list[dict] = []
    for state in cohort(court, window):
        pr = state.pr
        info = sizes.get(pr["object_id"]) or {}
        diff_size = info.get("diff_size")
        commits = len(state.commits)
        first = _first_review(state)
        pushes, unapprovals = _rounds(state)
        large = bool(
            commits > commit_cap
            or (diff_size is not None and diff_size > line_cap)
        )
        rows.append(_pr_row(
            pr,
            commits=commits,
            diff_size=diff_size,
            large=large,
            first_review_hours=(
                _span_hours(court, first["actor"], pr["created_ts"], first["_ts"])
                if first is not None else None
            ),
            rounds=len(pushes) + len(unapprovals),
            # the drill-down is about the over-cap PRs, but the whole list is
            # shown so the "large vs small" comparison can be checked
            affected=large,
        ))

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
        "commits_cap": commit_cap,
        "lines_cap": line_cap,
        "caps": chosen,
        "default_caps": default_size_caps(),
        "over_commits_cap": sum(1 for row in rows if row["commits"] > commit_cap),
        "over_lines_cap": sum(
            1 for row in rows
            if row["diff_size"] is not None and row["diff_size"] > line_cap
        ),
        "large_prs": len(large),
        "small_prs": len(small),
        "large_share": round(len(large) / len(rows), 2) if rows else 0.0,
        "large_median_first_review_hours": latency(large),
        "small_median_first_review_hours": latency(small),
        "large_median_rounds": _median([float(row["rounds"]) for row in large]),
        "small_median_rounds": _median([float(row["rounds"]) for row in small]),
        "rows": _drill_order(rows),
    }


def _histogram_edges(values: list[int], cap: int,
                     buckets: int = SIZE_HISTOGRAM_BUCKETS) -> list[int]:
    """Nice histogram edges over ``[0, max(values, cap)]``, including the cap.

    Bucket widths are chosen from a small "nice" set so the axis is readable
    (1, 2, 5, 10, 25, 50, 100, …), and the cap is forced onto an edge so its
    marker sits on a bar boundary rather than inside a bar. The final edge is
    above the maximum, so the last bucket is closed at the top.
    """
    top = max([int(cap), *(int(v) for v in values if v is not None)] or [0])
    top = max(1, top)
    if top <= buckets:
        step = 1
    else:
        raw = top / buckets
        nice = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500,
                1_000, 2_000, 2_500, 5_000, 10_000)
        step = next((size for size in nice if size >= raw), nice[-1])
    edges = list(range(0, top + step + 1, step))
    if 0 <= cap <= edges[-1] and cap not in edges:
        edges = sorted({*edges, int(cap)})
    if len(edges) < 2:
        edges = [0, 1]
    return edges


def histogram(values: list[int], edges: list[int]) -> list[int]:
    """Count ``values`` into ``[edges[i], edges[i+1])``; the last bucket is closed."""
    counts = [0] * (len(edges) - 1)
    for value in values:
        number = int(value)
        for index in range(len(edges) - 1):
            if edges[index] <= number < edges[index + 1]:
                counts[index] += 1
                break
        else:
            if number >= edges[-1]:
                counts[-1] += 1
    return counts


def pr_size_distribution(court: ballcourt.BallCourt, window,
                         sizes: dict[str, dict] | None = None,
                         caps: dict | None = None) -> dict:
    """The two size distributions behind the caps (prd/39).

    A PR contributes its commit count (COMMIT objects, the same count
    :func:`pr_size` uses) and, when the store captured it, its changed lines
    (``lines_added + lines_removed``). The response carries:

    * ``commits`` / ``lines`` — bucket ``edges`` + ``counts`` for drawing, the
      sorted raw ``values`` for exact client-side re-bucketing at any cap, the
      ``max``, and (for lines) whether the store captured diff size at all;
    * the chosen ``caps`` and the stated ``defaults``;
    * ``large_prs`` at those caps, split into ``over_commits_cap`` /
      ``over_lines_cap`` (a PR over both is counted once in ``large_prs``);
    * ``rows`` — one item per PR, so the UI's drill-down and the live count use
      the same per-PR facts.

    Bucket edges include the cap, so the marker always lands on a boundary.
    """
    sizes = sizes or {}
    chosen = size_caps(caps)
    rows: list[dict] = []
    for state in cohort(court, window):
        pr = state.pr
        info = sizes.get(pr["object_id"]) or {}
        diff_size = info.get("diff_size")
        commits = len(state.commits)
        pushes, unapprovals = _rounds(state)
        closed = pr["closed_ts"] is not None
        end = pr["closed_ts"] or court.snapshot
        cycle = (
            round(court.business_hours(pr["actor"], pr["created_ts"], end), 2)
            if pr["created_ts"] is not None and end > pr["created_ts"] else None
        )
        rows.append({
            "kind": "pr",
            "object_id": pr["object_id"],
            "id": f"{pr['repo'].split('/')[-1]}#{pr['pr_id']}",
            "repo": pr["repo"],
            "number": pr["pr_id"],
            "tickets": list(pr.get("ticket_keys") or ()),
            "state": pr["state"] if pr["closed_ts"] else "open",
            "commits": commits,
            "diff_size": diff_size,
            "rounds": len(pushes) + len(unapprovals),
            "cycle_hours": cycle,
            "censored": not closed,
        })
    rows.sort(key=lambda row: row["object_id"])

    commit_values = [row["commits"] for row in rows]
    line_values = [row["diff_size"] for row in rows if row["diff_size"] is not None]
    commit_edges = _histogram_edges(commit_values, chosen["commits"])
    line_edges = _histogram_edges(line_values, chosen["lines"])
    # prd/42 M8: size vs cycle, so "cap PRs" is not mistaken for a proven lever.
    # Measured on *merged* PRs only: an open PR's cycle is censored at the
    # snapshot, and a declined PR (usually a bot-closed zombie) has a long idle
    # cycle with few review rounds — including either flips the correlation's
    # sign (measured: rounds +0.61 merged, −0.23 with declined included).
    closed_rows = [
        row for row in rows
        if row["state"] == "merged" and row["cycle_hours"] is not None
    ]
    def correlation(key: str) -> float | None:
        values = [
            (row[key], row["cycle_hours"]) for row in closed_rows
            if row.get(key) is not None
        ]
        return _pearson([value for value, _ in values], [cycle for _, cycle in values])
    correlations = {
        "n": len(closed_rows),
        "commits": correlation("commits"),
        "lines": correlation("diff_size"),
        "rounds": correlation("rounds"),
        "basis": (
            "Pearson r over MERGED PRs with a measured created → merged cycle "
            "(open PRs are censored and declined PRs are not delivered work); "
            "rounds = the review rounds (concepts.md §6). A weak r means size is "
            "not the lever, even when the size rule fires."
        ),
    }
    large = [
        row for row in rows
        if row["commits"] > chosen["commits"]
        or (row["diff_size"] is not None and row["diff_size"] > chosen["lines"])
    ]
    large_ids = {row["object_id"] for row in large}
    for row in rows:
        row["large"] = row["object_id"] in large_ids
    over_commits = sum(1 for row in rows if row["commits"] > chosen["commits"])
    over_lines = sum(
        1 for row in rows
        if row["diff_size"] is not None and row["diff_size"] > chosen["lines"]
    )
    return {
        "n": len(rows),
        "caps": chosen,
        "defaults": default_size_caps(),
        "commits": {
            "edges": commit_edges,
            "counts": histogram(commit_values, commit_edges),
            "max": max(commit_values) if commit_values else 0,
            "values": sorted(commit_values),
        },
        "lines": {
            "edges": line_edges,
            "counts": histogram(line_values, line_edges),
            "max": max(line_values) if line_values else 0,
            "values": sorted(line_values),
            "available": bool(line_values),
            "n": len(line_values),
        },
        "large_prs": len(large),
        "small_prs": len(rows) - len(large),
        "large_share": round(len(large) / len(rows), 2) if rows else 0.0,
        "over_commits_cap": over_commits,
        "over_lines_cap": over_lines,
        # prd/42 M8: the evidence the sliders need next to them.
        "correlations": correlations,
        "rows": rows,
        "definition": "large = commits > commit cap OR changed lines > line cap",
        "note": (
            "changed lines come from the PR object's lines_added + lines_removed "
            "(or diff_size); a store ingested before the diff-size capture reports "
            "lines.available: false and calibrates on commits alone"
        ),
    }


def sentinel_queue(court: ballcourt.BallCourt, window) -> dict:
    """The sentinel wait, one number from one clock (prd/46 §2).

    This metric used to average *review-ball episodes* while the Queue's
    response table timed "last peer answer → first response". A lead saw 5.7 h,
    6.0 h and 11.1 h for the same person and could not tell they were three
    metrics. It now quotes ``ballcourt.response_times``'s canonical
    ``sentinel_wait`` block verbatim — the gate-aware first-response clock — and
    carries its definition in the payload.
    """
    sentinels = set(court.sentinels)
    definition = ballcourt.SENTINEL_WAIT_DEFINITION
    if not sentinels:
        return {
            "available": False,
            "sentinels": [],
            "n": 0,
            "median_hours": None,
            "p90_hours": None,
            "target_hours": TARGET_FIRST_REVIEW_HOURS,
            "by_person": {},
            "rows": [],
            "definition": definition,
        }
    response = ballcourt.response_times(court, window)
    canonical = response["sentinel_wait"]
    pr_meta = {state.pr["object_id"]: state.pr for state in court.prs}
    rows: list[dict] = []
    for request in canonical.get("requests", ()):
        pr = pr_meta.get(request["object_id"])
        if pr is None:
            continue
        rows.append(_pr_row(
            pr, person=request["person"], hours=request["hours"], waits=1,
            longest_hours=request["hours"],
            from_ts=request["start"], to_ts=request["end"],
            affected=request["hours"] > TARGET_FIRST_REVIEW_HOURS,
        ))
    by_person: dict[str, dict] = {}
    for request in canonical.get("requests", ()):
        entry = by_person.setdefault(
            request["person"], {"hours": [], "n": 0}
        )
        entry["hours"].append(request["hours"])
        entry["n"] += 1
    return {
        "available": bool(canonical["n"]),
        "sentinels": sorted(sentinels),
        "n": canonical["n"],
        "median_hours": canonical["median_hours"],
        "p90_hours": canonical["p90_hours"],
        "target_hours": TARGET_FIRST_REVIEW_HOURS,
        "sentinel_after_peers": court.sentinel_after_peers,
        # prd/46 §2: requests closed by a merge/decline before the sentinel's
        # turn are counted, not charged (they were never owed).
        "unrequired_n": response.get("never_excluded", {}).get("closed_prs", 0),
        "by_person": {
            person: {
                "n": entry["n"],
                "median_hours": _median(entry["hours"]),
            }
            for person, entry in sorted(by_person.items())
        },
        "rows": _drill_order(rows),
        # prd/46 §2: the definition travels with the number, so the Rules card
        # and the Queue table cannot be read as two clocks.
        "definition": definition,
        "never": canonical.get("never", 0),
        "never_oldest_hours": canonical.get("never_oldest_hours"),
        "never_excluded": response.get("never_excluded", {}),
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
            "rows": [],
            "note": "no calendar configured, so no vacations are known",
        }

    pseudo = {ballcourt.REVIEW, ballcourt.CI, ballcourt.CALENDAR}
    pr_meta = {state.pr["object_id"]: state.pr for state in court.prs}
    rows: list[dict] = []
    drill: list[dict] = []
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
        for object_id, hours in per_item.items():
            pr = pr_meta.get(object_id)
            if pr is None:
                continue
            delegated_here = object_id in delegated
            drill.append(_pr_row(
                pr, person=person, hours=round(hours, 2),
                delegated=delegated_here, affected=not delegated_here,
                vacation_start=vacation["start"], vacation_end=vacation["end"],
            ))

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
        # prd/26 P5: every item that waited, not the ten the vacation row lists
        "rows": _drill_order(drill),
        "note": (
            "hours are the working time an item lost while it waited on the "
            "person, priced with that person's vacation ignored"
        ),
    }


def stale_prs(court: ballcourt.BallCourt, window,
              done_tickets: set[str] | None = None) -> dict:
    """The stale/zombie-PR signal, priced (prd/34 §5, prd/37 §6).

    Reuses ``ballcourt.delivery_list`` so the rule and the delivery list quote
    the *same* fact: a PR is stale when it has been open longer than
    ``STALE_BUSINESS_DAYS`` business days (8 h each, concepts.md §1), or when
    its ticket is already Done in Jira while the PR is open. The second reason
    is finished work that was never delivered, so its whole current wait is
    recoverable; the first prices only the age past the limit.
    """
    limit_days = ballcourt.STALE_BUSINESS_DAYS
    limit_hours = limit_days * ballcourt.BUSINESS_HOURS_PER_DAY
    delivery = ballcourt.delivery_list(
        court, window, stale_business_days=limit_days, done_tickets=done_tickets,
    )
    rows: list[dict] = []
    for row in delivery["rows"]:
        reasons = list(row.get("stale_reasons") or ())
        ticket_done = any("Done in Jira" in reason for reason in reasons)
        age_hours = float(row.get("age_hours") or 0.0)
        waiting_hours = float(row.get("waiting_hours") or 0.0)
        age_excess = max(0.0, age_hours - limit_hours)
        impact = max(age_excess, waiting_hours) if ticket_done else age_excess
        rows.append({
            "kind": "pr",
            "object_id": row["object_id"],
            "repo": row["repo"],
            "number": row["number"],
            "id": row["id"],
            "tickets": list(row.get("tickets") or ()),
            "state": row["state"],
            "hours": round(impact, 2),
            "age_hours": round(age_hours, 2),
            "age_business_days": row.get("age_business_days"),
            "waiting_hours": round(waiting_hours, 2),
            "limit_days": limit_days,
            "ticket_done": ticket_done,
            "reasons": reasons,
            "affected": bool(row.get("stale")),
        })
    stale = [row for row in rows if row["affected"]]
    over_age = sum(1 for row in stale if row["age_business_days"] > limit_days)
    ticket_done_open = sum(1 for row in stale if row["ticket_done"])
    oldest = max((row["age_hours"] for row in stale), default=None)
    created = {state.pr["object_id"]: state.pr["created_ts"] for state in court.prs}
    stale_created = [
        created[row["object_id"]] for row in stale
        if created.get(row["object_id"]) is not None
    ]
    return {
        "available": True,
        "open_prs": len(rows),
        "stale_prs": len(stale),
        "over_age": over_age,
        "ticket_done_open": ticket_done_open,
        "limit_days": limit_days,
        "limit_hours": limit_hours,
        "oldest_age_hours": oldest,
        # prd/55 §6: when every stale item was opened before the window, the
        # rule's cohort is outside it and a 1-week view must not present it.
        "cohort_end": iso(max(stale_created)) if stale_created else None,
        "hours": round(sum(row["hours"] for row in stale), 2),
        "rows": _drill_order(rows),
        "note": (
            f"stale = open longer than {_human_hours(limit_days * 8)} "
            "or the ticket is Done in Jira while the PR is open; hours = the "
            "age past the limit, or the current wait when the ticket is Done"
        ),
    }


#: prd/37 §6: an item waiting *only* on the sentinel past this many business
#: hours is the sentinel-tail the rule names (24 h = 3 working days).
SENTINEL_TAIL_HOURS = 24.0


def sentinel_tail(court: ballcourt.BallCourt, window) -> dict:
    """Open items waiting **only** on the sentinel, past a stated threshold.

    ``sentinel_queue`` prices every wait the mandatory approver caused;
    this metric isolates the tail where the sentinel is the *sole* holder —
    no peer reviewer pending, no author debt — so the fix is unambiguous
    (another sentinel, or a rotation) and no reviewer is charged for it.
    """
    sentinels = set(court.sentinels)
    if not sentinels:
        return {
            "available": False,
            "sentinels": [],
            "threshold_hours": SENTINEL_TAIL_HOURS,
            "n": 0,
            "tail_items": 0,
            "hours": 0.0,
            "excess_hours": 0.0,
            "median_hours": None,
            "p90_hours": None,
            "longest_hours": None,
            "rows": [],
        }
    pr_meta = {state.pr["object_id"]: state.pr for state in court.prs}
    by_pr: dict[str, list] = defaultdict(list)
    for interval in court.intervals:
        by_pr[interval.object_id].append(interval)
    rows: list[dict] = []
    since_values: list[datetime] = []
    for object_id, intervals in by_pr.items():
        pr = pr_meta.get(object_id)
        if pr is None or pr["closed_ts"] is not None:
            continue
        intervals.sort(key=lambda interval: interval.start)
        tail: list = []
        for interval in reversed(intervals):
            holders = [person for person in interval.person_holders]
            if (
                len(holders) == 1
                and holders[0] in sentinels
                and not interval.author_open
            ):
                tail.append(interval)
            else:
                break
        if not tail:
            continue
        person = tail[0].person_holders[0]
        hours = sum(interval.hours_for(person) for interval in tail)
        since = tail[-1].start
        since_values.append(since)
        rows.append(_pr_row(
            pr,
            person=person,
            hours=round(hours, 2),
            waiting_since=iso(since),
            threshold_hours=SENTINEL_TAIL_HOURS,
            affected=hours > SENTINEL_TAIL_HOURS,
        ))
    rows = _drill_order(rows)
    tail_rows = [row for row in rows if row["affected"]]
    waits = [row["hours"] for row in rows]
    return {
        "available": True,
        "sentinels": sorted(sentinels),
        "threshold_hours": SENTINEL_TAIL_HOURS,
        "n": len(rows),
        "tail_items": len(tail_rows),
        "hours": round(sum(row["hours"] for row in tail_rows), 2),
        "excess_hours": round(
            sum(row["hours"] - SENTINEL_TAIL_HOURS for row in tail_rows), 2
        ),
        "median_hours": _median(waits),
        "p90_hours": _p90(waits),
        "longest_hours": round(max(waits), 2) if waits else None,
        # prd/55 §6: the tail's cohort is when the wait started; if every tail
        # item began waiting before the window, a 1-week view omits the rule.
        "cohort_end": iso(max(since_values)) if since_values else None,
        "rows": rows,
        "note": (
            "an item is in the sentinel tail when the sentinel is its only "
            "human holder (no peer reviewer pending, no author debt) and that "
            f"wait exceeds {SENTINEL_TAIL_HOURS:g} business hours"
        ),
    }


def metrics(court: ballcourt.BallCourt, threads: list[dict], window,
            sizes: dict[str, dict] | None = None,
            jira: jira_analysis.JiraData | None = None,
            caps: dict | None = None) -> dict:
    """Every prd/17 metric, in one deterministic pass.

    The Jira half (prd/21) is merged in under its own keys; with no Jira data in
    the store every one of those metrics is ``available: false`` with the same
    note, so "the ticket layer is missing" is a stated result rather than a
    silent zero. ``caps`` is the tenant's PR-size calibration (prd/39).
    """
    done_tickets = jira_analysis.done_keys(jira) if jira is not None else set()
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
        # prd/37 §6: the two rules the data wanted on top of the signals
        "sentinel_tail": sentinel_tail(court, window),
        "stale_prs": stale_prs(court, window, done_tickets),
        "pr_size": pr_size(court, window, sizes, caps=caps),
        "vacation_blocking": vacation_blocking(court, window),
        "stacked_cascade": stack_analysis.metrics(court, window),
        "review_cost": review_cost(court, window),
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


def _iso_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return None


def _cohort_in_window(cohort_end, window) -> bool:
    """Does a rule's cohort reach into the analysis window? (prd/55 §6)

    A sprint card describes the stories a *closed* sprint committed. At a 1-week
    timeframe, S2 closed six weeks before the window: listing "S2 delivered
    50 %" answers a question the window does not ask. A cohort that ended
    before the window starts is therefore not listed.
    """
    if window is None:
        return True
    end = _iso_dt(cohort_end)
    return end is None or end >= window[0]


def templates(
    m: dict,
    level: str = "team",
    round_hours: float = DEFAULT_ROUND_HOURS,
    bots: set[str] | None = None,
    window: tuple[datetime, datetime] | None = None,
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
        severity = "critical" if cyc["median_hours"] >= WAITING_WEEK_HOURS else "warning"
        out.append(_suggestion(
            "cycle-time", "cycle_time",
            f"Cycle time is {_human_hours(cyc['median_hours'])} median "
            f"(target < {_human_hours(TARGET_CYCLE_HOURS)})",
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
        severity = "critical" if sv["median_hours"] >= WAITING_WEEK_HOURS else "warning"
        out.append(_suggestion(
            "time-to-merge", "survival",
            f"Time to merge is {_human_hours(sv['median_hours'])} median with open "
            f"and declined PRs censored in (target < {_human_hours(TARGET_CYCLE_HOURS)})",
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
            f"({_human_hours(sv['merged_only_median_hours'])}) meets "
            f"{_human_hours(TARGET_CYCLE_HOURS)}, but "
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

    # 4. rework_loops — the review-round cap (prd/43 §1).
    # This is the lever the data supports most strongly: the QA measured
    # r ≈ 0.6 between rounds and cycle, and the lab's 2-round preset is its
    # best result. It fires on excess rounds (the measured saving) or on a
    # strong correlation (the lever is real even when no PR breached 3 yet).
    rw = m["rework_loops"]
    correlation = rw.get("rounds_cycle_correlation")
    correlation_n = rw.get("rounds_cycle_n") or 0
    lever = (
        correlation is not None and correlation >= 0.4 and correlation_n >= 5
    )
    if rw["excess_rounds"] > 0 or lever:
        slope = rw.get("rounds_cycle_slope_hours")
        # the marginal slope is a better price than cycle ÷ rounds only when the
        # correlation says the slope is real; otherwise use the stated cost.
        measured_slope = (
            slope if slope is not None and slope > 0
            and correlation is not None and correlation >= 0.3 else None
        )
        hours_per_round = measured_slope if measured_slope is not None else round_hours
        if correlation is not None and correlation_n >= 5:
            title = (
                f"Review rounds track cycle time (r = {correlation:.2f}) — "
                f"cap them at {REVIEW_ROUND_CAP_TARGET}"
            )
        else:
            title = (
                f"Review loops run to {round(rw['p90_rounds'] or 0)} rounds at p90 "
                f"(target {REVIEW_ROUND_CAP_TARGET} max)"
            )
        evidence = (
            f" Each extra round adds ≈{_human_hours(hours_per_round)} of cycle "
            f"in this store (r = {correlation:.2f} over {correlation_n} merged "
            f"PRs), so the cap is a measured lever, not a style preference."
            if correlation is not None and correlation_n >= 3 else ""
        )
        out.append(_suggestion(
            "round-cap", "rework_loops",
            title,
            {
                "prs": rw["n"],
                # prd/55 §3: the card states the basis of the median. The
                # merged median is the cohort r/slope use; the all-PR median is
                # shown beside it rather than silently replacing it.
                "median_rounds_merged": rw.get("median_rounds_merged"),
                "median_rounds_all": rw.get("median_rounds_all"),
                "p90_rounds": rw["p90_rounds"],
                "prs_over_target": rw["prs_over_target"],
                "excess_rounds": rw["excess_rounds"],
                # prd/46 §3: the cap the rule recommends is the lab's best
                # scenario (2), not the conformance threshold (3).
                "round_cap": REVIEW_ROUND_CAP_TARGET,
                "hours_per_extra_round": (
                    hours_per_round if correlation is not None else None
                ),
                # prd/43 §1: the correlation the cap is ranked on
                "rounds_cycle_correlation": correlation,
                "rounds_cycle_n": correlation_n,
            },
            f"Cap review rounds at {REVIEW_ROUND_CAP_TARGET}: if a PR needs one more "
            "round, the owner and reviewer agree the remaining blockers in one "
            f"pass instead of another comment round.{evidence}",
            "warning", rw["excess_rounds"] * hours_per_round, rw["n"],
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

    # 7b. stale_prs (prd/34 §5 signal, prd/37 §6 rule) — zombie PRs.
    # The delivery list already flags them; this is the ranked "what to change"
    # version, priced by the age past the limit (or the whole wait when the
    # ticket is already Done and only the merge is missing).
    sp = m["stale_prs"]
    if sp["stale_prs"] > 0 and _cohort_in_window(sp.get("cohort_end"), window):
        oldest = sp["oldest_age_hours"] or 0.0
        severity = (
            "critical"
            if sp["ticket_done_open"] > 0 or oldest >= 2 * sp["limit_hours"]
            else "warning"
        )
        reasons = []
        if sp["over_age"]:
            reasons.append(
                f"{sp['over_age']} open past "
                f"{_human_hours(sp['limit_days'] * 8)}"
            )
        if sp["ticket_done_open"]:
            reasons.append(f"{sp['ticket_done_open']} with the ticket already Done")
        examples = ", ".join(
            # prd/42 P8: every example is ticket-first, so the evidence can
            # link to #ticket/KEY like every other view.
            (
                f"{row['tickets'][-1]} {row['id']}"
                if row.get("tickets") else row["id"]
            )
            for row in sp["rows"] if row["affected"]
        )
        out.append(_suggestion(
            "stale-prs", "stale_prs",
            f"{_count(sp['stale_prs'], 'stale PR')} — " + " and ".join(reasons),
            {
                "stale_prs": sp["stale_prs"],
                "open_prs": sp["open_prs"],
                "over_age": sp["over_age"],
                "ticket_done_open": sp["ticket_done_open"],
                "limit_days": sp["limit_days"],
                "oldest_age_hours": oldest,
                "recoverable_hours": sp["hours"],
                # prd/63 V-B: stale PRs are a live state; `1w` does not window
                # them, and the card says so rather than looking inconsistent.
                "live_state": True,
                "examples": examples,
            },
            "Close, land, or decline stale PRs in the weekly review: a PR open "
            "longer than a working week is a wait with no end, and one whose "
            "ticket is already Done is finished work the team cannot deliver or "
            "count. Both cost cycle time every day they stay open.",
            severity, sp["hours"], max(sp["stale_prs"], 1),
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
                # prd/46 §2: the canonical clock, named on the card, and the
                # current wait it does NOT measure.
                "clock": "last peer answer → first response",
                "never_open": sq.get("never"),
            },
            "Rotate or add sentinels: the mandatory approval is the longest single wait, "
            "so one more sentinel shortens the tail of every PR that needs one.",
            severity, (sq["median_hours"] - TARGET_FIRST_REVIEW_HOURS) * sq["n"], sq["n"],
        ))

    # 8b. sentinel_tail (prd/37 §6) — the items waiting *only* on the sentinel.
    # The queue rule prices the median; this one names the stuck tail where the
    # sentinel is the sole holder, so the fix (another sentinel / a rotation)
    # cannot be confused with "review is slow".
    st = m["sentinel_tail"]
    if st["available"] and st["tail_items"] > 0 and _cohort_in_window(
        st.get("cohort_end"), window
    ):
        longest = st["longest_hours"] or 0.0
        severity = "critical" if longest >= 3 * SENTINEL_TAIL_HOURS else "warning"
        names = ", ".join(st["sentinels"])
        out.append(_suggestion(
            "sentinel-tail", "sentinel_tail",
            f"{_count(st['tail_items'], 'item')} waiting only on "
            f"{names} for up to {_human_hours(longest)} (threshold "
            f"{_human_hours(SENTINEL_TAIL_HOURS)})",
            {
                "sentinels": len(st["sentinels"]),
                "tail_items": st["tail_items"],
                "open_items_waiting_on_a_sentinel": st["n"],
                "hours": st["hours"],
                "excess_hours": st["excess_hours"],
                "median_hours": st["median_hours"],
                "longest_hours": longest,
                "threshold_hours": st["threshold_hours"],
                "person": names,
                # prd/46 §2: this is a *current* wait, not the response clock.
                "clock": "current wait on the sentinel, not their response time",
                # prd/63 V-B: a live-state card describes the snapshot, not
                # the chosen timeframe; the badge says so.
                "live_state": True,
                # prd/42 P8: ticket keys, not bare PR ids, so the rule's
                # examples are links like every other view's.
                "examples": ", ".join(
                    (
                        f"{row['tickets'][-1]} {row['id']}"
                        if row.get("tickets") else row["id"]
                    )
                    for row in st["rows"] if row["affected"]
                ),
            },
            "Add a second sentinel or a rotation: these items have no other "
            "holder, so nobody else can move them. A mandatory approval that "
            "sits on one person past a day of work is capacity, not latency — "
            "another approver shortens every one of them at once.",
            severity, st["excess_hours"], st["tail_items"],
            scope={"kind": "role", "role": "sentinel"},
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
        per_story = wtd.get("largest_per_story_hours")
        per_story_text = (
            f"The median story waits {_human_hours(per_story)} in the "
            f"{wtd['largest_label']} segment"
            if per_story is not None
            else f"The largest wait is {_human_hours(hours)} in the "
                 f"{wtd['largest_label']} segment"
        )
        # prd/41 B4(a): the top two segments are often within a point. Say it,
        # so "fix that phase first" is not a ranking the data cannot support.
        tie_note = ""
        if wtd.get("tied") and wtd.get("runner_up_label"):
            tie_note = (
                f" {wtd['largest_label']} and {wtd['runner_up_label']} are "
                f"statistically tied ({share * 100:.1f}% vs "
                f"{(wtd.get('runner_up_share') or 0.0) * 100:.1f}%, within "
                f"{wtd.get('tie_threshold_points', 1):g} point): treat them as "
                "one problem, not as a first and a second."
            )
        # prd/41 B4(c): the segment is not all review — the part with no PR open
        # is a missing PR, and the rule must not fold it into "review rounds".
        segment_entry = wtd["segments"][segment]
        no_pr_hours = segment_entry.get("no_pr_open_hours") or 0.0
        no_pr_share = segment_entry.get("no_pr_open_share") or 0.0
        gap_note = ""
        if no_pr_hours > 0:
            gap_note = (
                f" {_human_hours(no_pr_hours)} of it ({round(no_pr_share * 100)}%) "
                "had no PR open at all — that is a missing PR, not a review round."
            )
        closing = (
            "Fix both together — the ranking between them is inside the noise."
            if tie_note else
            "Fix that phase first: it is the cheapest hour to remove, because "
            "nothing is being worked on during it."
        )
        out.append(_suggestion(
            "jira-waiting-time", "waiting_time_decomposition",
            f"The team spends {percent}% of ticket cycle time in the "
            f"{wtd['largest_label']} segment"
            + (f" (tied with {wtd['runner_up_label']})" if tie_note else ""),
            {
                "segment": wtd["largest_label"],
                # prd/26 P5: the drill-down needs the segment key, not just the
                # label, to pick the column out of metrics…rows[].segments
                "segment_key": wtd["largest"],
                # the cohort total is an aggregate; the per-story figure is a
                # statistic over stories (concepts.md §6). Both are labelled.
                "segment_hours": hours,
                "per_story_hours": per_story,
                "per_story_basis": wtd.get("largest_per_story_basis"),
                "share": share,
                "cycle_hours": wtd["total_hours"],
                "stories": wtd["stories_decomposed"],
                "flow_efficiency": wtd["largest_efficiency"],
                "no_pr_open_hours": no_pr_hours,
                "no_pr_open_share": no_pr_share,
                "runner_up": wtd.get("runner_up_label"),
                "runner_up_share": wtd.get("runner_up_share"),
                "tied": bool(wtd.get("tied")),
                "tie_points": wtd.get("tie_points"),
                # prd/41 B4(b): the at-stake figure is half the segment, an
                # assumption — never a measured saving.
                "impact_basis": (
                    "assumption: half the largest segment's hours — not a "
                    "measured saving"
                ),
                "impact_assumed": True,
                "examples": ", ".join(wtd.get("largest_examples") or []),
            },
            f"{advice} {per_story_text}, {percent}% of the measured ticket "
            f"cycle.{gap_note}{tie_note} {closing}",
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
        # prd/41 B8: the sprint lens counts every story in the store; this rule
        # counts the stories created in the chosen timeframe. Both numbers are
        # right for their scope, so the scope is stated with the number.
        cohort_window = co.get("window") or {}
        scope_note = (
            f" (stories created in the {cohort_window.get('days')}-day window; the "
            "sprint lens counts every story in the store)"
            if cohort_window.get("days") is not None else ""
        )
        out.append(_suggestion(
            "jira-carry-over", "carry_over",
            f"{carried} stor{'y' if carried == 1 else 'ies'} committed but not "
            "done by their sprint's close"
            + (f" ({cohort_window.get('days')}-day window; the sprint lens "
               "counts the whole store)" if cohort_window.get("days") is not None else ""),
            {
                "carried_over": carried,
                "late": co["late"],
                "never": co["never"],
                "stories_in_a_sprint": co["stories_in_a_sprint"],
                "share": co["share"],
                "sprints": co["sprints_involved"],
                "late_hours": co["late_hours"],
                "wait_hours": co["wait_hours"],
                "median_wait_hours": co["median_wait_hours"],
                "target_share": co["target_share"],
                # prd/41 B8: the window this count is filtered to
                "window_days": cohort_window.get("days"),
                "window_start": cohort_window.get("start"),
                "window_end": cohort_window.get("end"),
                # prd/25: ticket keys in evidence are doorways to the detail page
                "examples": ", ".join(row["key"] for row in co["rows"][:5]),
            },
            "Plan from a refined backlog and finish what you commit: a story "
            "that misses its sprint's close is late (it still cost the slip) "
            "or still open (it takes capacity from the next plan). Split the "
            "story until it fits one sprint, and leave it in the backlog until "
            "it does — a carry-over is a commitment the team already knew it "
            "would break."
            + (f" This count is the stories created in the {cohort_window.get('days')}"
               "-day window; the sprint lens counts every story in the store, "
               "so it can be larger." if cohort_window.get("days") is not None else ""),
            severity, co["late_hours"], co["stories_in_a_sprint"] or carried,
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
    # prd/46 §5: the headline is the honesty defect the QA measured — logged
    # hours do not track story points (r ≈ −0.05) and total a fraction of the
    # active PR time. Lateness is a secondary line on the same card, not the
    # reason to distrust the number.
    wc = m["worklog_cross_check"]
    if wc["available"]:
        delay = wc["mean_delay_days"]
        ratio = wc["logged_to_active_ratio"]
        correlation = wc.get("points_logged_correlation")
        weak = correlation is not None and abs(correlation) < 0.3
        out_of_band = ratio is not None and (
            ratio >= WORKLOG_RATIO_HIGH or ratio <= WORKLOG_RATIO_LOW
        )
        lateness = None
        if delay is not None:
            late_percent = round(wc["late_share"] * 100)
            lateness = (
                f"{round(delay, 1)} d mean delay · {late_percent}% more than a "
                "week late (secondary)"
            )
        if weak or out_of_band:
            severity = (
                "critical"
                if (ratio is not None and (ratio >= 5 or ratio <= 0.2))
                else "warning"
            )
            if weak:
                title = (
                    "Logged hours don't track story points "
                    f"(r = {correlation:g})"
                )
                # prd/53 §1: the same card states the second half of the gap —
                # the logged-vs-active ratio — when it is also out of band.
                if out_of_band and ratio is not None and 0 < ratio < 1:
                    title += (
                        f" and are {round(ratio * 100)} % of active PR time"
                    )
            elif ratio is not None and ratio < 1:
                # prd/56 §6: "0.49× less" is ambiguous (49 % as much? 51 %
                # less?). State the share directly.
                title = (
                    f"Logged hours are {round(ratio * 100)} % of active PR time "
                    f"({ratio:g}×) — the team logs about "
                    f"{round((1 - ratio) * 100)} % less than the PRs show"
                )
            else:
                title = f"Logged hours are {ratio:g}× the active PR time"
            evidence = {
                "worklogs": wc["worklogs"],
                "stories_with_worklogs": wc["stories_with_worklogs"],
                "logged_hours": wc["logged_hours"],
                "active_hours": wc["active_hours"],
                "ratio": ratio,
                "points_logged_correlation": correlation,
                "points_logged_n": wc.get("points_logged_n"),
                "points_active_correlation": wc.get("points_active_correlation"),
                # prd/53 §1: the gap in plain numbers next to the correlation.
                "median_logged_hours": wc.get("median_logged_hours"),
                "median_hours_per_point": wc.get("median_hours_per_point"),
            }
            if lateness:
                evidence["lateness"] = lateness
            out.append(_suggestion(
                "jira-worklog-mismatch", "worklog_cross_check",
                title,
                evidence,
                "Treat logged hours as a note, not a measure of effort: a "
                "worklog that is flat across 1–13 point stories cannot size, "
                "forecast or compare work, and the active PR time is a different "
                "clock. Agree what a worklog means (time spent? effort? "
                "attendance?) before either is used for planning. Logging is "
                "mostly timely, which is why lateness is the minor problem.",
                severity, 0.0, wc["worklogs"],
            ))
        elif delay is not None and (
            delay >= 7 or wc["late_share"] >= WORKLOG_LATE_SHARE_TARGET
        ):
            # Only when lateness is genuinely extreme and nothing above fired.
            severity = "critical" if delay >= 7 else "warning"
            out.append(_suggestion(
                "jira-worklog-noise", "worklog_cross_check",
                f"Logging is {round(delay, 1)} days late on average",
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
                "total but never a timeline.",
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
                # prd/29: a ticket in review that no PR names (Jira-level
                # cross-check of the PR review; reported, not ranked)
                "in_review_without_pr": jc["in_review_without_pr"],
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

    # 16. sprint on-time delivery (prd/37 §1) — did the sprint deliver what it
    # committed, measured at its close (concepts.md §6)? This is the rule the
    # sprint chip quotes: done-by-end, not done-by-now.
    sd = m["sprint_delivery"]
    worst = sd.get("worst") if sd.get("available") else None
    if worst is not None and not _cohort_in_window(worst.get("end"), window):
        # prd/55 §6: the sprint closed before the chosen window. Its cards are
        # not this window's evidence, so they are not listed.
        worst = None
    if worst is not None and worst["done_share"] < sd["target_share"]:
        committed = worst["stories"]
        delivered = worst["done_by_end"]
        late = worst["done_late"]
        share = worst["done_share"]
        severity = (
            "critical"
            if share < 0.6 or late >= 5
            else "warning"
        )
        never = worst["never_done"]
        unmerged = worst.get("unmerged", 0)
        committed_at_start = worst.get("committed", 0)
        adopted = worst.get("adopted", 0)
        points_share = worst.get("points_share")
        title = (
            f"S{worst['sprint_id']} delivered {delivered} of {committed} "
            f"{'committed ' if not adopted else ''}stories "
            f"({round(share * 100)}%) by its close"
        )
        # prd/46 §4: the denominator is the scope the sprint ended with; name
        # how much of it was ever a commitment, so "50 %" cannot read as
        # "half the plan shipped".
        if adopted:
            title += (
                f" — only {committed_at_start} were committed at the start, "
                f"{adopted} adopted mid-sprint"
            )
        if points_share is not None and abs(points_share - share) >= 0.02:
            # prd/63 C13: this is the *delivered* share by points, not the
            # adopted share; "44 % by points" next to "28 adopted" read as if
            # the adopted work were 44 % of the points.
            title += f" ({round(points_share * 100)}% of points delivered)"
        if late:
            title += f" — {late} finished late"
        if unmerged:
            title += f", {unmerged} Done in Jira with an unmerged PR"
        if never:
            title += f", {never} still open"
        # prd/63 V-A9/V-C3: the membership verdict is not the team's output.
        # State the work that finished inside the sprint's dates, and credit
        # the carry-overs nobody re-committed.
        window_done = worst.get("window_done") or 0
        if window_done > delivered:
            carried_in = worst.get("window_carry_in") or 0
            title += (
                f"; the team finished {window_done} stories "
                f"({worst.get('window_points') or 0} pts) in its dates"
                + (f", {carried_in} carried in from an earlier sprint" if carried_in else "")
            )
        if worst.get("length_outlier"):
            # prd/55 §5: S3's 50-day plan is not S1/S2's 24–26, so its share is
            # not a like-for-like delivery number.
            title += (
                f" — a {worst.get('planned_days'):g}-day sprint, not comparable "
                "with its 24–26-day neighbours"
            )
        out.append(_suggestion(
            "sprint-on-time", "sprint_delivery",
            title,
            {
                "sprint": f"S{worst['sprint_id']}",
                "stories": committed,
                "points": worst["points"],
                "committed": committed_at_start,
                "adopted": adopted,
                "committed_points": worst.get("committed_points", 0),
                "adopted_points": worst.get("adopted_points", 0),
                "points_share": points_share,
                "done_by_end": delivered,
                # prd/63 V-A9/V-C3: the window velocity beside the membership
                # verdict, so "15 of 30" cannot be read as "15 shipped".
                "window_done": worst.get("window_done"),
                "window_points": worst.get("window_points"),
                "window_carry_in": worst.get("window_carry_in"),
                "window_own": worst.get("window_own"),
                # prd/43 §5: the Jira-done count and the "code never merged"
                # count that was excluded from delivery
                "done_by_end_jira": worst.get("done_by_end_jira", delivered),
                "unmerged": unmerged,
                "done_late": late,
                "never_done": never,
                "done_share": share,
                "target_share": sd["target_share"],
                "late_hours": worst["late_hours"],
                "closed_sprints": sd["closed_sprints"],
                "measured_sprints": sd["measured_sprints"],
                "on_time_sprints": sd["on_time_sprints"],
                # prd/55 §5: the length flag is evidence only when it is one.
                "sprint_length_days": (
                    worst.get("planned_days") if worst.get("length_outlier") else None
                ),
                "sprint_length_outlier": True if worst.get("length_outlier") else None,
                "sprint_length_note": (
                    worst.get("length_note") if worst.get("length_outlier") else None
                ),
            },
            "Size the sprint to what the team finishes, not what it starts: "
            "measure scope at the close (done by end, not done by now) and "
            "carry the unfinished stories back to the backlog instead of "
            "silently re-committing them. A sprint that closes at "
            f"{round(share * 100)}% teaches the team to distrust every plan it "
            "makes.",
            severity, sd["late_hours"], committed,
            # prd/41 B8: the headline is about S{n}, so the drill-down must be
            # that sprint's items — not every closed sprint's.
            scope={"kind": "sprint", "sprint": worst["sprint_id"]},
        ))

    return out


# --------------------------------------------------------------------------- #
# the ranked list
# --------------------------------------------------------------------------- #
#: prd/37 §2 (concepts.md §6 "At stake"): rules in one **cost pool** price the
#: same hours. A pool's total is the broadest estimate in it, allocated from the
#: most specific rule outwards, so a subset rule (rebase-waste ⊂ rework-loops)
#: owns its hours and the broader rule only the remainder. The headline sums the
#: pools — each hour lands on exactly one rule.
#:
#: ``(pool, specificity)``; lower specificity is the narrower rule.
STAKE_RULES = {
    # PR-level waiting: every one of these prices the same PR hours
    "rebase-waste": ("pr_wait", 1),
    "conformance-unapproved": ("pr_wait", 2),
    "sentinel-tail": ("pr_wait", 3),
    "pr-size": ("pr_wait", 4),
    "review-tail": ("pr_wait", 5),
    "conformance-rounds": ("pr_wait", 6),
    "round-cap": ("pr_wait", 7),
    # the old id, kept so a cached/older payload still attributes once
    "rework-loops": ("pr_wait", 7),
    "comment-tax": ("pr_wait", 8),
    "stale-prs": ("pr_wait", 9),
    "cycle-time-censoring": ("pr_wait", 10),
    "vacation-blocking": ("pr_wait", 11),
    "stacked-cascade": ("pr_wait", 12),
    "sentinel-queue": ("pr_wait", 13),
    "review-sla": ("pr_wait", 14),
    "reviewer-spread": ("pr_wait", 15),
    "cycle-time": ("pr_wait", 16),
    "time-to-merge": ("pr_wait", 17),
    "flow-efficiency": ("pr_wait", 18),
    # the ticket layer's own cycle (a different unit of account: story segments)
    "jira-waiting-time": ("ticket_wait", 20),
    # the sprint plan's slip
    "jira-carry-over": ("sprint_late", 1),
    "sprint-on-time": ("sprint_late", 2),
    # refinement effort
    "jira-point-churn": ("refinement", 1),
    "jira-conformance": ("refinement", 2),
    # the log is not evidence of when work happened (zero impact, kept separate)
    "jira-worklog-noise": ("worklog", 1),
    "jira-worklog-mismatch": ("worklog", 2),
}


def _stake_rule(sid: str) -> tuple[str, int]:
    """``(pool, specificity)`` for a rule id; unknown rules are their own pool."""
    if sid.startswith("reviewer-"):
        return "pr_wait", 3
    if sid.startswith("vacation-"):
        return "pr_wait", 11
    return STAKE_RULES.get(sid, (f"rule:{sid}", 1))


def attribute_at_stake(suggestions: list[dict]) -> dict:
    """Attribute every at-stake hour to exactly one rule (prd/37 §2).

    A cost pool is a set of rules that price the same hours. Its total is the
    **broadest** estimate in the pool (never the sum), allocated from the most
    specific rule outwards: a subset rule keeps its own hours, and the broader
    rule is credited only with the remainder. So ``rebase-waste`` is not added
    on top of ``rework-loops``, and the headline is the sum of the pools — not
    of the rule estimates.

    Each suggestion keeps its measured ``hours`` and gets an ``at_stake`` block:
    ``attributed_hours`` (what the headline counts for it) and ``attributed_to``
    (the broader rule that owns the remainder, or ``None``).
    """
    for suggestion in suggestions:
        pool, specificity = _stake_rule(suggestion["id"])
        scope = suggestion.get("scope") or {}
        # a person-scoped rule is narrower than the team rule in the same pool
        if scope.get("kind") == "person":
            specificity = max(1, specificity - 2)
        suggestion["at_stake"] = {
            "pool": pool,
            "specificity": specificity,
            "hours": suggestion["impact_hours"],
            "attributed_hours": 0.0,
            "attributed_to": None,
        }
    pools: dict[str, list[dict]] = defaultdict(list)
    for suggestion in suggestions:
        pools[suggestion["at_stake"]["pool"]].append(suggestion)

    summary: list[dict] = []
    for pool, group in sorted(pools.items()):
        ceiling = round(max(s["impact_hours"] for s in group), 1)
        ordered = sorted(
            group,
            key=lambda s: (
                s["at_stake"]["specificity"], s["sample"], -s["impact_hours"], s["id"]
            ),
        )
        remaining = ceiling
        for suggestion in ordered:
            if remaining <= 0:
                break
            attributed = round(min(suggestion["impact_hours"], remaining), 1)
            suggestion["at_stake"]["attributed_hours"] = attributed
            remaining = round(remaining - attributed, 1)
        # the rule that owns the pool is the one the headline counts most of
        # (ties go to the broadest estimate); every fully-absorbed rule points
        # there, so "counted in #N" is never a dead end.
        owner = max(
            ordered,
            key=lambda s: (
                s["at_stake"]["attributed_hours"],
                s["impact_hours"],
                -s["at_stake"]["specificity"],
            ),
        )
        for suggestion in ordered:
            if suggestion["at_stake"]["attributed_hours"] <= 0:
                suggestion["at_stake"]["attributed_to"] = owner["id"]
        pool_total = round(sum(s["at_stake"]["attributed_hours"] for s in ordered), 1)
        for suggestion in ordered:
            suggestion["at_stake"]["pool_hours"] = pool_total
            suggestion["at_stake"]["pool_rules"] = len(ordered)
            if suggestion["at_stake"]["attributed_to"]:
                suggestion["at_stake"]["attributed_to_rank"] = owner.get("rank")
        total = pool_total
        summary.append({
            "pool": pool,
            "hours": total,
            "ceiling_hours": ceiling,
            "owner": owner["id"],
            "owner_rank": owner.get("rank"),
            "rules": [
                {
                    "id": suggestion["id"],
                    "rank": suggestion.get("rank"),
                    "hours": suggestion["impact_hours"],
                    "attributed_hours": suggestion["at_stake"]["attributed_hours"],
                }
                for suggestion in sorted(
                    ordered,
                    key=lambda s: (-s["at_stake"]["attributed_hours"],
                                   s.get("rank") or 999),
                )
            ],
        })

    own_total = round(sum(s["impact_hours"] for s in suggestions), 1)
    total = round(sum(row["hours"] for row in summary), 1)
    return {
        "hours": total,
        "rules": len(suggestions),
        "attributed_rules": sum(
            1 for s in suggestions if s["at_stake"]["attributed_hours"] > 0
        ),
        "overlap_hours": round(max(0.0, own_total - total), 1),
        "pools": summary,
        "note": (
            "each at-stake hour is attributed once: a pool of rules that price "
            "the same hours is counted at its broadest estimate, allocated from "
            "the most specific rule outwards (a subset rule such as "
            "rebase-waste is not added to rework-loops). Per-rule estimates "
            "still show the hours that rule measured; the headline does not "
            "add them twice"
        ),
    }


def rank(suggestions: list[dict], *, lab_aware: bool = False) -> list[dict]:
    """Order by impact × confidence, then severity, then a stable id.

    ``lab_aware`` (prd/46 §3) demotes a rule whose own policy-lab note
    contradicts its advice (the lab models it slower, or sees no clear effect)
    below every rule the lab does not contradict. The measured ``impact_score``
    is unchanged — only the presented order changes, so the page cannot rank
    "refine first" above its own banner's lever.
    """
    def contradicted(suggestion: dict) -> int:
        if not lab_aware:
            return 0
        lab = suggestion.get("policy_lab") or {}
        return 1 if lab.get("contradiction") else 0

    ordered = sorted(
        suggestions,
        key=lambda s: (
            contradicted(s),
            -s["impact_score"],
            SEVERITY_RANK.get(s["severity"], 9),
            s["id"],
        ),
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
    caps: dict | None = None,
    policy_lab: dict | None = None,
) -> dict:
    """The API payload: metrics + ranked, evidence-backed suggestions.

    ``caps`` is the tenant's PR-size calibration (prd/39): the ``pr_size`` metric,
    its rule and the drill-down all evaluate against it, and ``targets`` reports
    the chosen caps so the page cannot quote a threshold the rule did not use.

    ``policy_lab`` is the Rules ↔ Policy-lab cross-check (prd/43 §1): the lab's
    own preset results, which :func:`analysis.advice.attach` stamps onto the
    matching rules. The server computes it (and caches it); a caller that does
    not pass it gets a stated no-result block, never an invented note.
    """
    start, end = window
    chosen_caps = size_caps(caps)
    computed = metrics(court, threads, window, sizes, jira, caps=chosen_caps)
    round_hours = computed["review_cost"]["hours_per_round"]
    suggestions = rank(templates(computed, level, round_hours, bots, window=window))
    # prd/43 §1: the lab's verdict travels with the rule it bears on.
    cross_check = policy_lab if policy_lab is not None else advice.unavailable(
        "the policy lab was not run for this request"
    )
    advice.attach(suggestions, cross_check)
    # prd/46 §3: a contradicted rule is demoted below the rules the lab does not
    # contradict, so the #1 the page presents agrees with its own banner. The
    # impact scores stay the measured ones; only the position changes.
    suggestions = rank(suggestions, lab_aware=True)
    # prd/37 §2: the headline total counts each cost pool once (most specific).
    at_stake = attribute_at_stake(suggestions)
    days = round(max(0.0, (end - start).total_seconds()) / 86400.0, 1)
    # prd/55 §6: the window is not the dataset. The default 1q window is 90
    # days on a 103.6-day store, so 7 PRs are outside it; the header must say
    # "124 of 131", never let the dataset census (131) stand for the cohort.
    dataset_start = window_for(court, None, jira)[0]
    dataset_prs = len(court.prs)
    in_window = len(cohort(court, window))
    dataset_days = (
        round(max(0.0, (court.snapshot - dataset_start).total_seconds()) / 86400.0, 1)
        if dataset_start is not None else days
    )
    return {
        "ok": True,
        "generated_at": iso(generated_at or utcnow()),
        "as_of": iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "assumption": court.assumption,
        **court.calendar_warning(),
        "level": level,
        # prd/43 §1: the one-line "the lab's best scenario is X" cross-reference
        "policy_lab": cross_check,
        # prd/21: the ticket layer's availability travels next to the numbers, so
        # a store without the Jira ingest says so instead of reporting zeroes.
        "jira": computed["jira"],
        "window": {"start": iso(start), "end": iso(end), "days": days},
        # prd/55 §6: the window-vs-dataset fact, so no view claims the dataset's
        # PR count as the cohort's (or silently drops the difference).
        "dataset": {
            "prs": dataset_prs,
            "prs_in_window": in_window,
            "prs_outside_window": max(0, dataset_prs - in_window),
            "days": dataset_days,
            "window_days": days,
            "start": iso(dataset_start) if dataset_start is not None else None,
            "end": iso(court.snapshot),
        },
        "sample": {
            "prs": in_window,
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
            "size_commit_cap": chosen_caps["commits"],
            "size_line_cap": chosen_caps["lines"],
            "size_default_commit_cap": SIZE_COMMIT_CAP,
            "size_default_line_cap": SIZE_LINE_CAP,
            "flow_efficiency": TARGET_FLOW_EFFICIENCY,
            "flow_efficiency_touch_hours": TOUCH_HOURS,
            "hours_per_round": round_hours,
            "hours_per_round_basis": computed["review_cost"].get(
                "basis", ROUND_COST_BASIS
            ),
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
        # prd/51: the story-level rework aggregate (reopened / failed
        # verification / review bounces / approved-but-never-merged) rides the
        # Rules payload; it is derived from the same status history and PR
        # vectors the metrics read, over every story in the store.
        "rework": jira_analysis.rework(court, jira),
        "suggestions": suggestions,
        # prd/37 §2: the additive headline (sums each cost pool once) next to
        # the per-rule estimates, so the total is auditable.
        "at_stake": at_stake,
        "note": (
            "impact_hours estimates the wait the rule would remove, in "
            f"{court.hours_mode} hours, using the measured review-round cost in "
            "this window; ranking is impact × confidence (sample size). Person "
            "rules carry pseudonyms only. The Jira-layer rules (prd/21) price "
            "ticket waits with the same calendar and report "
            "available: false when the store has no ticket layer. The stacked-PR "
            "rule (prd/22) derives the stack DAG from ref digests -- never branch "
            "names -- and reports available: false when the store predates them. "
            "prd/37: at_stake.hours is additive -- each cost pool is counted once, "
            "allocated from its most specific rule, so the per-rule estimates are "
            "not meant to be summed; at_stake.pools shows the allocation."
        ),
    }

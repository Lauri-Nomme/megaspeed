"""Trend / time series (prd/50): the same metrics, one bucket at a time.

Every figure the product reports is a single window aggregate, so a lead cannot
answer "is flow efficiency improving week over week?". This module buckets the
*existing* metrics by week (or by sprint) and returns one point per bucket. It
defines **no new metric**: each point is the same function the window figure
comes from — ``rules.flow_efficiency``, ``rules.cycle_time``,
``jira.sprint_delivery`` — evaluated on the bucket's own window, with the same
cohort rule ("items created in the window") and the same business-hours
calendar. That is why a trend can never disagree with the headline.

The bucket boundaries are presentation, not analysis:

* ``week`` — Monday 00:00 UTC to the next Monday (the same alignment
  ``/api/events/counts`` uses), clipped to the analysis window;
* ``sprint`` — each SPRINT object's measured band (start → end, or start →
  snapshot while it is active).

The bucket that contains the snapshot is marked ``in_progress``: its cohort is
still accumulating, so its value is provisional — the UI labels it, never hides
it. The active sprint is named in the payload when one exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from analysis import aging as aging_view
from analysis import annotations as annotation_view
from analysis import ballcourt
from analysis import jira as jira_analysis
from analysis import rules
from analysis import stability as stability_view
from ingest.store import iso

#: One working week = 5 working days = 40 business hours (concepts.md §1). The
#: trend's *calendar* bucket is 7 calendar days; the unit label never conflates
#: the two.
WEEK_DAYS = 7
MICROSECOND = timedelta(microseconds=1)

BUCKETS = ("week", "sprint")
DEFAULT_BUCKET = "week"
DEFAULT_METRIC = "flow_efficiency"

#: prd/63 F8/V-A5: the smallest cohort a bucket may have and still be quoted as
#: the headline. A weekly ratio/median over 1–3 PRs is a noise number: "50.0 %
#: latest (n=1)" and "−6 h 35 min since the first bucket" (n=3 vs n=3) read as
#: a trend when they are one item. Five is the smallest group where a median is
#: not just the middle item; the counts are exact and need no such floor; a
#: sprint bucket's cohort is sprints, so two closed sprints is the minimum to
#: compare.
MIN_COHORT_N = 5
MIN_N_BY_METRIC = {
    "flow_efficiency": MIN_COHORT_N,
    "cycle_median": MIN_COHORT_N,
    "cycle_p85": MIN_COHORT_N,
    "wait_hours": MIN_COHORT_N,
    "on_time": 2,
    # a count is exact: a week with zero merges is a measurement, not a sample
    "merged": 0,
    "open": 0,
    # prd/61 additions: the stability strip's series and the cumulative bands.
    # Counts are exact (a zero week is a measurement); throughput_stories is a
    # cohort median-like number and keeps the same floor as the others.
    "throughput_stories": MIN_COHORT_N,
    "arrival_prs": 0,
    "arrival_stories": 0,
    "cumulative_flow": 0,
}

#: prd/64 F-17: a sprint bucket holds exactly one sprint, so its bucket count is
#: always 1 and an ``n ≥ 2`` bucket floor can never be met there. For ``on_time``
#: the sample of a sprint bucket is the sprint's own committed stories; five is
#: the floor the rest of the module already uses. Week buckets keep the ``n ≥ 2``
#: closed-sprint rule (`MIN_N_BY_METRIC["on_time"]`), because a week bucket can
#: hold several sprints.
SPRINT_BUCKET_MIN_STORIES = MIN_COHORT_N

#: The metric catalogue. ``definition`` names the function the point quotes, so
#: "same cohort, no new definitions" is stated in the payload, not just here.
#: ``min_n`` is the bucket cohort a headline needs (see ``MIN_COHORT_N``); it
#: travels with the metric so the UI can say "n < 5" in the metric's own words.
METRICS: dict[str, dict] = {
    "flow_efficiency": {
        "label": "Flow efficiency",
        "unit": "ratio",
        "higher_is_better": True,
        "definition": (
            "active ÷ elapsed over the PRs opened in the bucket — the same "
            "flow-efficiency number as the headline"
        ),
    },
    "cycle_median": {
        "label": "Cycle time (median)",
        "unit": "hours",
        "higher_is_better": False,
        "definition": (
            "median PR open → merged hours over the PRs opened in the bucket "
            "— the same cycle time as the headline"
        ),
    },
    "cycle_p85": {
        "label": "Cycle time (p85)",
        "unit": "hours",
        "higher_is_better": False,
        "definition": (
            "p85 of the same PR created → merged hours cohort — "
            "rules.cycle_time's p85, the tail the headline median hides"
        ),
    },
    "on_time": {
        # prd/64 F-17: the tab names the *plotted quantity* (a sprint's share of
        # committed stories delivered by its close). "Sprints on time (≥80 %)"
        # on Timeline/Rules counts sprints that reached the target — a different
        # question, and one the trend never answers.
        "label": "Sprint story delivery share",
        "unit": "ratio",
        "higher_is_better": True,
        "definition": (
            "stories done by the sprint's close ÷ the sprint's committed "
            "stories — the share of a sprint's stories delivered on time. This "
            "is not the 'sprints on time (≥80 %)' count on Timeline/Rules: that "
            "answers how many sprints reached the target share"
        ),
    },
    "wait_hours": {
        "label": "Wait hours",
        "unit": "hours",
        "higher_is_better": False,
        "definition": (
            "waiting hours over the PRs opened in the bucket — the same "
            "wait total as the headline"
        ),
    },
    "merged": {
        "label": "Merged PRs",
        "unit": "count",
        "higher_is_better": True,
        "definition": "PRs merged inside the bucket (throughput)",
    },
    "open": {
        "label": "Open PRs",
        "unit": "count",
        "higher_is_better": False,
        "definition": (
            "PRs open at the bucket's end (the snapshot for the active bucket) "
            "— the work in progress the queue view samples"
        ),
    },
    # -- prd/61: the stability strip's series and the cumulative bands ------- #
    "throughput_stories": {
        "label": "Stories delivered",
        "unit": "count",
        "higher_is_better": True,
        "definition": (
            "stories whose done boundary falls inside the bucket — the prd/60 "
            "delivered population (Jira done with no open PR)"
        ),
    },
    "arrival_prs": {
        "label": "PRs opened",
        "unit": "count",
        "higher_is_better": False,
        "definition": "PRs created inside the bucket (the arrival rate)",
    },
    "arrival_stories": {
        "label": "Stories created",
        "unit": "count",
        "higher_is_better": False,
        "definition": "stories created inside the bucket (the ticket arrival rate)",
    },
    "cumulative_flow": {
        "label": "Cumulative flow",
        "unit": "count",
        "higher_is_better": True,
        "definition": (
            "cumulative PRs created / started / in review / merged (and stories "
            "created / started / in review / done), binned on the bucket's end"
        ),
    },
}


def metric_ids() -> list[dict]:
    """The catalogue the UI builds its metric tabs from (kept in one place)."""
    return [
        {"id": key, "label": value["label"], "unit": value["unit"],
         "higher_is_better": value["higher_is_better"],
         "min_n": value.get("min_n")}
        for key, value in METRICS.items()
    ]


for _metric_id, _min_n in MIN_N_BY_METRIC.items():
    METRICS[_metric_id]["min_n"] = _min_n


def min_n_rule(metric: str, bucket: str) -> tuple[int, str]:
    """The headline floor for one metric × bucket kind: ``(floor, basis)``.

    ``basis`` names the point field the floor counts. It is ``"n"`` everywhere
    except ``on_time`` on ``bucket=sprint``, where the bucket *is* one sprint
    (``n = 1`` by construction) and the sample is the sprint's committed stories
    — so the floor is ``≥ 5`` stories, not ``≥ 2`` buckets (prd/64 F-17). The
    bucket count is not a sample size there, and quoting it made the headline
    permanently read "no bucket meets the n≥2 floor".
    """
    if metric == "on_time" and bucket == "sprint":
        return SPRINT_BUCKET_MIN_STORIES, "stories"
    min_n_value = METRICS[metric].get("min_n")
    return int(MIN_COHORT_N if min_n_value is None else min_n_value), "n"


# --------------------------------------------------------------------------- #
# bucket boundaries
# --------------------------------------------------------------------------- #
def _floor_week(moment: datetime) -> datetime:
    """The Monday 00:00 that starts ``moment``'s week (UTC, naive like the store)."""
    return (moment - timedelta(days=moment.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def week_bounds(window: tuple[datetime, datetime]) -> list[tuple[datetime, datetime]]:
    """Dense Monday-aligned buckets covering the window (``end`` exclusive)."""
    start, end = window
    cursor = _floor_week(start)
    out: list[tuple[datetime, datetime]] = []
    while cursor <= end:
        out.append((cursor, cursor + timedelta(days=WEEK_DAYS)))
        cursor += timedelta(days=WEEK_DAYS)
    return out


def _sprint_datetime(value) -> datetime | None:
    """A sprint boundary: a datetime, an ISO stamp or a date-only plan."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(hour=16)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def sprint_bounds(
    court: ballcourt.BallCourt, data: jira_analysis.JiraData,
) -> list[dict]:
    """One bucket per SPRINT object: its measured band, active → snapshot.

    Sorted by start; a sprint whose start the store lost is skipped (it has no
    bucket to place points in, and inventing one is exactly what this product
    must not do). An active sprint's band ends at the snapshot and the bucket is
    ``in_progress``.
    """
    out: list[dict] = []
    for sprint in data.sprints.values():
        start = _sprint_datetime(sprint.get("start"))
        if start is None:
            continue
        end = _sprint_datetime(sprint.get("end"))
        closed = end is not None
        out.append({
            "start": start,
            # a sprint's close is the bucket's last instant, so the boundary is
            # inclusive (unlike a week's exclusive Monday edge)
            "end": end if closed else court.snapshot,
            "exclusive_end": False,
            "in_progress": not closed,
            "sprint_id": sprint.get("id"),
            "label": (
                f"S{sprint.get('id')}" if sprint.get("id") is not None else "sprint"
            ),
        })
    out.sort(key=lambda row: (row["start"], row["sprint_id"] or 0))
    return out


def _label(moment: datetime, bucket: str) -> str:
    if bucket == "week":
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        return f"{moment.day} {months[moment.month - 1]}"
    return moment.strftime("%d %b")


def bucket_bounds(
    court: ballcourt.BallCourt,
    data: jira_analysis.JiraData | None,
    window: tuple[datetime, datetime],
    bucket: str = DEFAULT_BUCKET,
) -> list[dict]:
    """The dense bucket rows a metric is evaluated on (prd/50, prd/61).

    Shared by the trend, the stability strip and the cumulative-flow series so
    the three cannot disagree about where a week (or a sprint) ends.
    """
    if bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of {sorted(BUCKETS)}")
    if bucket == "week":
        return [
            {"start": low, "end": high, "exclusive_end": True,
             "in_progress": high > court.snapshot,
             "sprint_id": None, "label": _label(low, "week")}
            for low, high in week_bounds(window)
        ]
    return sprint_bounds(court, data or jira_analysis.EMPTY)


# --------------------------------------------------------------------------- #
# one point per bucket, from the existing metric functions
# --------------------------------------------------------------------------- #
def _open_in_bucket(
    court: ballcourt.BallCourt, start: datetime, end_inclusive: datetime,
) -> int:
    """PRs opened in the bucket that are still open (right-censored at the snapshot).

    prd/63 F8/V-A5: the latest cycle bucket is survivor-biased — only the PRs
    that already merged can enter the median. Counting the still-open cohort
    lets the point carry a censored marker instead of looking settled.
    """
    return sum(
        1 for state in court.prs
        if state.pr["created_ts"] is not None
        and start <= state.pr["created_ts"] <= end_inclusive
        and state.pr["closed_ts"] is None
    )


def _point(
    metric: str,
    court: ballcourt.BallCourt,
    start: datetime,
    end_inclusive: datetime,
    *,
    sprints: list[dict] | None = None,
    outcomes: dict[str, dict] | None = None,
    stories: list | None = None,
    flow: dict | None = None,
    point: datetime | None = None,
) -> tuple[float | None, int, dict]:
    """``(value, n, extra)`` for one bucket — the same function as the headline."""
    window = (start, end_inclusive)
    if metric in ("flow_efficiency", "wait_hours"):
        block = rules.flow_efficiency(court, window)
        value = (
            block["aggregate_efficiency"] if metric == "flow_efficiency"
            else block["waiting_hours"]
        )
        return value, int(block["n"]), {
            "active_hours": block["active_hours"],
            "elapsed_hours": block["elapsed_hours"],
            "working_hours": block["working_hours"],
            "unavailable": not block["available"],
            "censored": _open_in_bucket(court, start, end_inclusive),
        }
    if metric in ("cycle_median", "cycle_p85"):
        block = rules.cycle_time(court, window)
        value = (
            block["median_hours"] if metric == "cycle_median"
            else block["p85_hours"]
        )
        return value, int(block["n"]), {
            "median_hours": block["median_hours"],
            "p85_hours": block["p85_hours"],
            "p90_hours": block["p90_hours"],
            "cohort": block["cohort"],
            "censored_open": block["censored"]["open"],
            "censored": int(block["censored"]["open"]),
        }
    if metric == "merged":
        count = sum(
            1 for state in court.prs
            if state.pr["closed_ts"] is not None
            and state.pr["state"] == "merged"
            and start <= state.pr["closed_ts"] <= end_inclusive
        )
        return count, count, {}
    if metric == "open":
        point = min(end_inclusive, court.snapshot)
        count = sum(
            1 for state in court.prs
            if state.pr["created_ts"] is not None
            and state.pr["created_ts"] <= point
            and (
                state.pr["closed_ts"] is None
                or state.pr["closed_ts"] > point
            )
        )
        return count, count, {}
    if metric == "throughput_stories":
        count = sum(
            1 for outcome in (outcomes or {}).values()
            if outcome.get("done_at") is not None
            and start <= outcome["done_at"] <= end_inclusive
            and outcome.get("delivered", True)
        )
        return count, count, {}
    if metric == "arrival_prs":
        count = sum(
            1 for state in court.prs
            if state.pr["created_ts"] is not None
            and start <= state.pr["created_ts"] <= end_inclusive
        )
        return count, count, {}
    if metric == "arrival_stories":
        count = sum(
            1 for story in (stories or ())
            if story.start is not None and start <= story.start <= end_inclusive
        )
        return count, count, {}
    if metric == "cumulative_flow":
        block = dict(flow or {})
        return block.get("created") or 0, int(block.get("created") or 0), block
    if metric == "on_time":
        rows = [
            row for row in sprints or ()
            if row.get("end")
            and start <= _sprint_datetime(row["end"]) <= end_inclusive
        ]
        stories = sum(int(row.get("stories") or 0) for row in rows)
        delivered = sum(int(row.get("done_by_end") or 0) for row in rows)
        value = round(delivered / stories, 4) if stories else None
        return value, len(rows), {
            "stories": stories,
            "done_by_end": delivered,
            "late": sum(int(row.get("done_late") or 0) for row in rows),
            "never_done": sum(int(row.get("never_done") or 0) for row in rows),
            "unmerged": sum(int(row.get("unmerged") or 0) for row in rows),
            "sprints": [row.get("sprint_id") for row in rows],
        }
    raise ValueError(f"unknown trend metric {metric!r}")


def _flow_at(
    court: ballcourt.BallCourt,
    data: jira_analysis.JiraData,
    point: datetime,
) -> dict:
    """The four cumulative bands of both populations at one bucket end.

    PRs: created (opened) · started (an author push/comment) · in review (a
    reviewer's first action, ``ballcourt.first_review``) · merged. Stories:
    created · started (a status event into in-progress or later) · in review
    (in-review/in-verification/done) · done. One population per band, and the
    PR band is the point's value (the widest series).
    """
    pr = {"created": 0, "started": 0, "in_review": 0, "merged": 0}
    for state in court.prs:
        created = state.pr["created_ts"]
        if created is None or created > point:
            continue
        pr["created"] += 1
        if any(
            event["_ts"] <= point
            and event["activity"] in ("pr_pushed", "comment_added")
            for event in state.events
        ):
            pr["started"] += 1
        review = ballcourt.first_review(state)
        if review is not None and review["_ts"] <= point:
            pr["in_review"] += 1
        if (
            state.pr["closed_ts"] is not None
            and state.pr["state"] == "merged"
            and state.pr["closed_ts"] <= point
        ):
            pr["merged"] += 1
    story = {"created": 0, "started": 0, "in_review": 0, "done": 0}
    for item in data.stories:
        created = item.start
        if created is None or created > point:
            continue
        story["created"] += 1
        stages = {
            stage for stamp, stage in aging_view.stage_events(item) if stamp <= point
        }
        if stages & {"in_progress", "in_review", "in_verification", "done"}:
            story["started"] += 1
        if stages & {"in_review", "in_verification", "done"}:
            story["in_review"] += 1
        done = jira_analysis.story_done_at(item)
        if done is not None and done <= point:
            story["done"] += 1
    return {
        "created": pr["created"],
        "bands": [
            {"id": "created", "label": "created", "value": pr["created"]},
            {"id": "started", "label": "started", "value": pr["started"]},
            {"id": "in_review", "label": "in review", "value": pr["in_review"]},
            {"id": "merged", "label": "merged", "value": pr["merged"]},
        ],
        "story_bands": [
            {"id": "created", "label": "created", "value": story["created"]},
            {"id": "started", "label": "started", "value": story["started"]},
            {"id": "in_review", "label": "in review", "value": story["in_review"]},
            {"id": "done", "label": "done", "value": story["done"]},
        ],
        "population": "pr",
        "point": iso(point),
    }


def trend(
    court: ballcourt.BallCourt,
    data: jira_analysis.JiraData | None,
    window: tuple[datetime, datetime],
    metric: str = DEFAULT_METRIC,
    bucket: str = DEFAULT_BUCKET,
    *,
    annotations: list[dict] | None = None,
    spans: list[dict] | None = None,
) -> dict:
    """The time series for one metric, one point per bucket.

    Raises ``ValueError`` for an unknown metric/bucket (the endpoint maps it to
    a 400 that lists the valid ones — a filter is never silently ignored).

    prd/61: the same payload also carries the team's dated annotations (marked
    ``in_window``; one outside the window is counted, never drawn), the calendar
    spans (holidays/vacations, derived from the business-hours calendar), and
    the stability strip. The series itself is unchanged: every point still calls
    the same metric function the headline does.
    """
    data = data or jira_analysis.EMPTY
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {sorted(METRICS)}")
    start, end = window
    bounds = bucket_bounds(court, data, window, bucket)

    sprints = (
        jira_analysis.sprint_delivery(court, data, window).get("sprints") or []
        if metric == "on_time" else None
    )
    outcomes = (
        jira_analysis.sprint_outcomes(court, data)[1]
        if metric in ("throughput_stories", "cumulative_flow") else None
    )
    stories = list(data.stories) if metric == "arrival_stories" else None
    flow: dict = {}
    if metric == "cumulative_flow":
        for row in bounds:
            low = max(row["start"], start)
            high = min(row["end"], end)
            if high <= low and not (not row["exclusive_end"] and high == low):
                continue
            end_inclusive = high - MICROSECOND if row["exclusive_end"] else high
            flow[row["start"]] = _flow_at(
                court, data, min(end_inclusive, court.snapshot)
            )
    points: list[dict] = []
    for row in bounds:
        low = max(row["start"], start)
        high = min(row["end"], end)
        if high <= low and not (not row["exclusive_end"] and high == low):
            continue
        end_inclusive = high - MICROSECOND if row["exclusive_end"] else high
        value, n, extra = _point(
            metric, court, low, end_inclusive, sprints=sprints,
            outcomes=outcomes, stories=stories, flow=flow.get(row["start"]),
            point=min(end_inclusive, court.snapshot),
        )
        points.append({
            "start": iso(low),
            "end": iso(high),
            # the calendar edge and the evaluation window are both stated: the
            # first/last bucket is clipped to the analysis window, so "5 Jan"
            # is the label while the recompute uses `start`/`end`.
            "bucket_start": iso(row["start"]),
            "bucket_end": iso(row["end"]),
            "label": row["label"],
            "in_progress": bool(row["in_progress"]),
            "available": value is not None,
            "value": value,
            "n": n,
            "sprint_id": row.get("sprint_id"),
            **extra,
        })

    measured = [point for point in points if point["available"]]
    # prd/63 B8: the bucket containing the snapshot is still accumulating, so
    # it is not the headline and never a delta. "Merged PRs: 0 latest, −2 since
    # the first bucket" was an empty in-progress week plotted as a collapse;
    # the latest *measured* (settled) bucket is the headline.
    # prd/63 F8/V-A5: a bucket also needs the metric's minimum cohort. The old
    # rule quoted any settled bucket, so "50.0 % latest (n=1)" and "2 d 1 h
    # latest (9 Nov · n=3)" headlined noise and compared n=3 with n=3. The
    # headline is the newest settled bucket that clears the floor; a bucket
    # below it stays drawn and is named as skipped, never silently promoted.
    # prd/64 F-17: the floor's *basis* depends on the bucket kind — a sprint
    # bucket of `on_time` is one sprint, so it is judged on its committed
    # stories, not on a bucket count that can only ever be 1.
    min_n, min_n_basis = min_n_rule(metric, bucket)

    def cohort(point: dict) -> int:
        return int(point.get(min_n_basis) or 0)

    settled = [point for point in measured if not point["in_progress"]]
    below_min = [point for point in settled if cohort(point) < min_n]
    eligible = [point for point in settled if cohort(point) >= min_n]
    # A one-week (or one-sprint) store has no settled bucket: fall back to a
    # provisional *in-progress* point only when it clears the same floor, and
    # say so (``latest_provisional``).
    provisional = [
        point for point in measured
        if point["in_progress"] and cohort(point) >= min_n
    ]
    headline = eligible or provisional
    first = headline[0] if headline else None
    latest = headline[-1] if headline else None
    change = None
    if first is not None and latest is not None and first is not latest:
        change = round(float(latest["value"]) - float(first["value"]), 4)
    # The newest settled bucket when it is not the headline, so the UI can say
    # "9 Nov (n=3) is below the n≥5 floor" instead of hiding it.
    latest_settled = settled[-1] if settled else None
    skipped_after = (
        [point["label"] for point in settled
         if latest is not None and point["start"] > latest["start"]]
        if latest is not None else [point["label"] for point in settled]
    )
    active_sprint = next(
        (
            sprint for sprint in data.sprints.values()
            if not jira_analysis.sprint_closed(sprint)
        ),
        None,
    )
    entries, annotation_counts = annotation_view.view_entries(
        list(annotations or ()), window,
    )
    span_rows = (
        list(spans) if spans is not None
        else annotation_view.calendar_spans(court, window)
    )
    annotation_counts["holiday"] = sum(
        1 for span in span_rows
        if span.get("kind") == "holiday" and span.get("in_window", True)
    )
    annotation_counts["vacation"] = sum(
        1 for span in span_rows
        if span.get("kind") == "vacation" and span.get("in_window", True)
    )
    annotation_counts["spans_outside"] = sum(
        1 for span in span_rows if not span.get("in_window", True)
    )
    return {
        "metric": metric,
        **METRICS[metric],
        "bucket": bucket,
        # prd/64 F-17: the *effective* floor travels with the bucket kind, not
        # only with the metric, and says what it counts (a sprint bucket's
        # floor counts its committed stories, not its bucket count).
        "min_n": min_n,
        "min_n_basis": min_n_basis,
        "available": bool(points),
        "points": points,
        "annotations": entries,
        "spans": span_rows,
        "annotation_counts": annotation_counts,
        # the "Record a change" menu's closed vocabulary (one source of truth)
        "kinds": annotation_view.catalogue(),
        "stability": stability_view.payload(
            court, data, window, bucket=bucket, bounds=bounds,
        ),
        "summary": {
            "first": first["value"] if first else None,
            "latest": latest["value"] if latest else None,
            "change": change,
            "buckets": len(points),
            "measured_buckets": len(measured),
            # prd/63 B8: the headline names its bucket and its cohort size, and
            # says when it had to fall back to a provisional in-progress point.
            "latest_label": latest["label"] if latest else None,
            "latest_n": latest["n"] if latest else None,
            # prd/64 F-17: the on_time extras the headline quotes as
            # "15/30 stories" so a sprint bucket's sample is never shown as n=1.
            "latest_stories": latest.get("stories") if latest else None,
            "latest_done_by_end": latest.get("done_by_end") if latest else None,
            "first_label": first["label"] if first else None,
            "first_n": first["n"] if first else None,
            "latest_provisional": bool(latest and latest["in_progress"]),
            "settled_buckets": len(settled),
            # prd/63 F8/V-A5: the minimum-N rule and the censored marker are
            # facts in the payload, so every consumer states the same floor.
            # prd/64 F-17: ``min_n_basis`` says what the floor counts ("n" or a
            # sprint bucket's "stories"), so the UI never prints "n≥2" for a
            # sprint bucket it is not measuring by buckets.
            "min_n": min_n,
            "min_n_basis": min_n_basis,
            "latest_settled_label": (
                latest_settled["label"] if latest_settled else None
            ),
            "latest_settled_n": latest_settled["n"] if latest_settled else None,
            "below_min": [
                {
                    "label": point["label"],
                    "n": point["n"],
                    # the floor's quantity, for a bucket kind whose floor is not
                    # the bucket count (sprint buckets of `on_time`)
                    **({"stories": int(point.get("stories") or 0)}
                       if point.get("stories") is not None else {}),
                }
                for point in below_min
            ],
            "low_n_after_headline": skipped_after if latest is not None else [],
            "latest_censored": bool(latest and latest.get("censored")),
            "latest_censored_n": (
                int(latest.get("censored") or 0) if latest else 0
            ),
            "in_progress": [
                point["label"] for point in points if point["in_progress"]
            ],
        },
        "active_sprint": (
            {
                "id": active_sprint.get("id"),
                "object_id": active_sprint.get("object_id"),
                "start": iso(_sprint_datetime(active_sprint.get("start"))),
                "planned_end": active_sprint.get("planned_end"),
            }
            if active_sprint is not None else None
        ),
        "metrics": metric_ids(),
        "window": {
            "start": iso(start),
            "end": iso(end),
            "days": round((end - start).total_seconds() / 86400.0, 1),
        },
        "note": (
            "no SPRINT object carries a start in this store, so the sprint "
            "bucket has no boundaries — use bucket=week"
            if bucket == "sprint" and not bounds else
            (
                None if measured else
                "no point in this window has a measured value for the metric; "
                "the buckets are still drawn so an empty week is visible"
            )
        ),
    }

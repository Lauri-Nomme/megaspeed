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

from analysis import ballcourt
from analysis import jira as jira_analysis
from analysis import rules
from ingest.store import iso

#: One working week = 5 working days = 40 business hours (concepts.md §1). The
#: trend's *calendar* bucket is 7 calendar days; the unit label never conflates
#: the two.
WEEK_DAYS = 7
MICROSECOND = timedelta(microseconds=1)

BUCKETS = ("week", "sprint")
DEFAULT_BUCKET = "week"
DEFAULT_METRIC = "flow_efficiency"

#: The metric catalogue. ``definition`` names the function the point quotes, so
#: "same cohort, no new definitions" is stated in the payload, not just here.
METRICS: dict[str, dict] = {
    "flow_efficiency": {
        "label": "Flow efficiency",
        "unit": "ratio",
        "higher_is_better": True,
        "definition": (
            "active ÷ elapsed over the PRs opened in the bucket "
            "(rules.flow_efficiency aggregate_efficiency)"
        ),
    },
    "cycle_median": {
        "label": "Cycle time (median)",
        "unit": "hours",
        "higher_is_better": False,
        "definition": (
            "median created → merged hours over the PRs opened in the bucket "
            "(rules.cycle_time median_hours)"
        ),
    },
    "on_time": {
        "label": "Sprint on-time delivery",
        "unit": "ratio",
        "higher_is_better": True,
        "definition": (
            "done by the sprint's close ÷ committed stories, for the closed "
            "sprints whose close falls in the bucket (jira.sprint_delivery)"
        ),
    },
    "wait_hours": {
        "label": "Wait hours",
        "unit": "hours",
        "higher_is_better": False,
        "definition": (
            "waiting hours over the PRs opened in the bucket "
            "(rules.flow_efficiency waiting_hours)"
        ),
    },
    "merged": {
        "label": "Merged PRs",
        "unit": "count",
        "higher_is_better": True,
        "definition": "PRs merged inside the bucket (throughput, concepts.md §1)",
    },
    "open": {
        "label": "Open PRs",
        "unit": "count",
        "higher_is_better": False,
        "definition": (
            "PRs open at the bucket's end (the snapshot for the active bucket) "
            "— the WIP the queue view samples"
        ),
    },
}


def metric_ids() -> list[dict]:
    """The catalogue the UI builds its metric tabs from (kept in one place)."""
    return [
        {"id": key, "label": value["label"], "unit": value["unit"],
         "higher_is_better": value["higher_is_better"]}
        for key, value in METRICS.items()
    ]


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


# --------------------------------------------------------------------------- #
# one point per bucket, from the existing metric functions
# --------------------------------------------------------------------------- #
def _point(
    metric: str,
    court: ballcourt.BallCourt,
    start: datetime,
    end_inclusive: datetime,
    *,
    sprints: list[dict] | None = None,
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
        }
    if metric == "cycle_median":
        block = rules.cycle_time(court, window)
        return block["median_hours"], int(block["n"]), {
            "p90_hours": block["p90_hours"],
            "cohort": block["cohort"],
            "censored_open": block["censored"]["open"],
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


def trend(
    court: ballcourt.BallCourt,
    data: jira_analysis.JiraData | None,
    window: tuple[datetime, datetime],
    metric: str = DEFAULT_METRIC,
    bucket: str = DEFAULT_BUCKET,
) -> dict:
    """The time series for one metric, one point per bucket.

    Raises ``ValueError`` for an unknown metric/bucket (the endpoint maps it to
    a 400 that lists the valid ones — a filter is never silently ignored).
    """
    data = data or jira_analysis.EMPTY
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {sorted(METRICS)}")
    if bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of {sorted(BUCKETS)}")
    start, end = window
    if bucket == "week":
        bounds = [
            {"start": low, "end": high, "exclusive_end": True,
             "in_progress": high > court.snapshot,
             "sprint_id": None, "label": _label(low, "week")}
            for low, high in week_bounds(window)
        ]
    else:
        bounds = sprint_bounds(court, data)
        if not bounds:
            bounds = []

    sprints = (
        jira_analysis.sprint_delivery(court, data, window).get("sprints") or []
        if metric == "on_time" else None
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
    # the latest *measured* (settled) bucket is the headline. If every measured
    # bucket is in progress (a one-week store) there is nothing settled to
    # quote: keep the provisional point but say so.
    settled = [point for point in measured if not point["in_progress"]]
    headline = settled or measured
    first = next((point for point in headline), None)
    latest = next((point for point in reversed(headline)), None)
    change = None
    if first is not None and latest is not None and first is not latest:
        change = round(float(latest["value"]) - float(first["value"]), 4)
    active_sprint = next(
        (
            sprint for sprint in data.sprints.values()
            if not jira_analysis.sprint_closed(sprint)
        ),
        None,
    )
    return {
        "metric": metric,
        **METRICS[metric],
        "bucket": bucket,
        "available": bool(points),
        "points": points,
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
            "first_label": first["label"] if first else None,
            "first_n": first["n"] if first else None,
            "latest_provisional": bool(latest and latest["in_progress"]),
            "settled_buckets": len(settled),
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

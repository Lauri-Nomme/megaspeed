"""Stability flags (prd/61): is the queue growing faster than it drains?

A before/after answers "did the number move?"; it cannot warn that the number is
about to get worse for a mechanical reason. This module reads the trend's own
buckets with **Theil–Sen** slopes (a median of pairwise slopes: robust to one
bad week, no fitted line to defend) and says one of four things:

``overload`` · ``filling`` · ``stable`` · ``draining``

The distinction that matters is Little's Law (concepts §1): cycle time =
WIP ÷ throughput. If WIP grows while throughput does not, the queue is
*lengthening* and any cycle-time gain in the window is suspect — prd/60 must
refuse a forecast on ``overload`` for exactly that reason. ``filling`` says the
same thing more mildly: the queue still grows, but so do exits.

Population discipline: WIP is **open PRs at the bucket end** and throughput is
**PRs merged inside the bucket** — one population, so the implied cycle time
from Little's Law is comparable with the measured (Kaplan–Meier) PR cycle. The
story throughput series is available as its own metric (``throughput_stories``)
and is never mixed into this ratio.

No store writes; every number is a projection of the ball-court and the ticket
layer. The slope is per **week** (7 calendar days; the unit label says so).
"""

from __future__ import annotations

from datetime import timedelta

from analysis import rules
from ingest.store import iso

#: A bucket series shorter than this cannot support a slope: four points is the
#: smallest number where one outlier cannot define the line with Theil–Sen.
MIN_BUCKETS = 4
#: WIP moves more than this many items per week and the queue is "growing".
WIP_SLOPE = 0.25
#: Throughput at or below this per week counts as "flat" for the overloading
#: reading (a merge rate that crawls does not drain a growing queue).
THROUGHPUT_SLOPE = 0.05
#: "WIP grows more than this many times faster than throughput" = filling.
FILLING_RATIO = 2.0
#: Implied vs measured cycle divergence beyond this factor is labelled dead WIP.
DEAD_WIP_RATIO = 2.0
#: One working week = 5 days × 8 business hours (concepts §1).
BUSINESS_HOURS_PER_WEEK = 40.0

WEEK = timedelta(days=7)
MICROSECOND = timedelta(microseconds=1)


def median(values) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def theil_sen(xs, ys) -> float | None:
    """The median of pairwise slopes — the robust trend of one series."""
    x = [float(value) for value in xs]
    y = [float(value) for value in ys]
    if len(x) != len(y) or len(x) < 2:
        return None
    slopes = []
    for i in range(len(x)):
        for j in range(i + 1, len(x)):
            if x[j] != x[i]:
                slopes.append((y[j] - y[i]) / (x[j] - x[i]))
    value = median(slopes)
    return None if value is None else round(value, 3)


def _direction(slope: float | None, threshold: float) -> str:
    if slope is None:
        return "flat"
    if slope > threshold:
        return "rising"
    if slope < -threshold:
        return "falling"
    return "flat"


def refuses_forecast(flag: str) -> bool:
    """Only an overloaded queue refuses a forecast (the flag prd/60 reads)."""
    return flag == "overload"


def classify(wip_slope: float | None,
             throughput_slope: float | None) -> tuple[str, str]:
    """The combined reading from the two Theil–Sen slopes (prd/61).

    Pure, so the vocabulary and its precedence are testable without a store.
    ``overload`` is the only flag that refuses a forecast (prd/60 reads it).
    """
    if wip_slope is None:
        return "insufficient", "no_slope"
    if wip_slope > WIP_SLOPE and (throughput_slope or 0) <= THROUGHPUT_SLOPE:
        return "overload", "queue grows while exits do not"
    if (
        wip_slope > WIP_SLOPE
        and (throughput_slope or 0) > THROUGHPUT_SLOPE
        and wip_slope > FILLING_RATIO * (throughput_slope or 0)
    ):
        return "filling", "queue grows faster than the exit rate"
    if abs(wip_slope) <= WIP_SLOPE:
        return "stable", "WIP is flat"
    if wip_slope < -WIP_SLOPE and (throughput_slope or 0) >= 0:
        return "draining", "queue shrinks while exits hold"
    # WIP and throughput grow together: the absolute queue is longer, but it is
    # not growing relative to the exit rate. Say so rather than call a rising
    # queue "stable" without the reason.
    return "stable", "WIP and throughput grow together"


def _series(court, data, window, bounds: list[dict]) -> list[dict]:
    """Per settled bucket: WIP at the end, throughput inside, arrivals inside.

    Uses ``trend._point`` (the same functions the chart quotes) rather than a
    second implementation — the strip and the chart cannot disagree.
    """
    from analysis import trend as trend_view  # local: avoids an import cycle

    start, end = window
    rows: list[dict] = []
    for row in bounds:
        low = max(row["start"], start)
        high = min(row["end"], end)
        if high <= low and not (not row["exclusive_end"] and high == low):
            continue
        end_inclusive = high - MICROSECOND if row["exclusive_end"] else high
        wip, _n, _extra = trend_view._point("open", court, low, end_inclusive)
        merged, _n, _extra = trend_view._point("merged", court, low, end_inclusive)
        arrivals, _n, _extra = trend_view._point(
            "arrival_prs", court, low, end_inclusive
        )
        weeks = max(1.0 / 7.0, (high - low).total_seconds() / (7 * 86400.0))
        rows.append({
            "label": row.get("label"),
            "start": iso(row["start"]),
            "end": iso(high),
            "_start": row["start"],
            "_end": high,
            "weeks": weeks,
            "wip": int(wip or 0),
            "throughput": int(merged or 0),
            "arrivals": int(arrivals or 0),
            "in_progress": bool(row["in_progress"]),
            "sprint_id": row.get("sprint_id"),
        })
    return rows


def _little(court, data, window, mean_wip: float, mean_throughput: float) -> dict:
    """Implied cycle (WIP ÷ throughput) vs the Kaplan–Meier median (weeks)."""
    implied = (
        mean_wip / mean_throughput
        if mean_wip is not None and mean_throughput and mean_throughput > 0
        else None
    )
    km = rules.time_to_merge(court, window)
    measured_hours = km.get("median_hours")
    measured = (
        measured_hours / BUSINESS_HOURS_PER_WEEK
        if measured_hours is not None else None
    )
    ratio = (
        implied / measured
        if implied is not None and measured and measured > 0 else None
    )
    flag = (
        "dead_wip"
        if ratio is not None and ratio > DEAD_WIP_RATIO else "ok"
    )
    dead = 0
    if flag == "dead_wip":
        try:
            from analysis import aging as aging_view
            # prd/59's "no live PR" list: dead + unobserved items with no open
            # PR — the work that inflates WIP without ever departing.
            dead = len(aging_view.replan(
                aging_view.in_flight(court, data, "story")
            ))
        except Exception:  # noqa: BLE001 — the sentence is optional, never a read failure
            dead = 0
    note = None
    if flag == "dead_wip":
        note = (
            f"Little's Law implies {implied:.1f} wk of cycle time but the "
            f"measured (Kaplan–Meier) median is {measured:.1f} wk"
            + (f": {dead} items have no live PR; they inflate WIP without ever "
               "departing." if dead else ".")
        )
    return {
        "implied_weeks": round(implied, 2) if implied is not None else None,
        "measured_weeks": round(measured, 2) if measured is not None else None,
        "ratio": round(ratio, 2) if ratio is not None else None,
        "flag": flag,
        "dead_items": dead,
        "estimator": "Kaplan–Meier",
        "note": note,
    }


def payload(court, data, window, *, bucket: str = "week",
            bounds: list[dict] | None = None) -> dict:
    """The stability strip: WIP/throughput slopes, Little's Law, arrivals gap.

    The in-progress bucket is excluded: its cohort is still accumulating, so a
    slope through it would read the snapshot's cut-off as a collapse.
    """
    if bounds is None:
        from analysis import trend as trend_view
        bounds = trend_view.bucket_bounds(court, data, window, bucket)
    rows = _series(court, data, window, bounds)
    settled = [row for row in rows if not row["in_progress"]]
    # A bucket before the team's first activity is not a measurement of zero:
    # the analysis window's first Monday can be empty while the first PR is a
    # week later. Leading all-empty buckets would bend the slope towards
    # "growth from nothing", so the slope starts at the first bucket with data
    # (the chart still draws the empty ones). The count of dropped buckets is
    # stated, never silently forgotten.
    leading_empty = 0
    active = list(settled)
    while active and not (active[0]["wip"] or active[0]["throughput"]
                          or active[0]["arrivals"]):
        active.pop(0)
        leading_empty += 1
    if len(active) < MIN_BUCKETS:
        return {
            "available": False,
            "bucket": bucket,
            "flag": "insufficient",
            "refuses_forecast": False,
            "buckets": len(active),
            "min_buckets": MIN_BUCKETS,
            "leading_empty": leading_empty,
            "points": [
                {key: value for key, value in row.items() if not key.startswith("_")}
                for row in rows
            ],
            "note": (
                f"Stability needs at least {MIN_BUCKETS} settled {bucket} buckets; "
                f"this window has {len(active)}."
            ),
        }

    first = active[0]["_start"]
    xs = [(row["_start"] - first).total_seconds() / (7 * 86400.0) for row in active]
    wip_values = [row["wip"] for row in active]
    throughput_values = [row["throughput"] for row in active]
    wip_slope = theil_sen(xs, wip_values)
    throughput_slope = theil_sen(xs, throughput_values)
    total_weeks = sum(row["weeks"] for row in active) or 1.0
    mean_wip = sum(wip_values) / len(wip_values)
    mean_throughput = sum(throughput_values) / total_weeks

    if wip_slope is None:
        flag, reason = "insufficient", "no_slope"
    else:
        flag, reason = classify(wip_slope, throughput_slope)

    ratio = (
        wip_slope / throughput_slope
        if wip_slope is not None and throughput_slope and throughput_slope > 0
        else None
    )
    if flag == "overload":
        note = (
            "The queue is overloading: WIP grows "
            f"{(ratio or 0):.1f}× faster than throughput. Little's Law says "
            "cycle time will rise; treat any cycle-time gain in this window "
            "with suspicion."
        )
    elif flag == "filling":
        note = (
            "The queue is filling: WIP grows "
            f"{(ratio or 0):.1f}× faster than throughput. Little's Law says "
            "cycle time will rise; treat any cycle-time gain in this window "
            "with suspicion."
        )
    elif flag == "draining":
        note = (
            "The queue is draining: WIP falls "
            f"{abs(wip_slope):.1f} items/wk while throughput holds at "
            f"{mean_throughput:.1f}/wk."
        )
    else:
        note = (
            "WIP is stable "
            f"({(wip_slope or 0):+.1f} items/wk) against throughput "
            f"{mean_throughput:.1f}/wk; the queue is not the story."
            if reason == "WIP is flat" else
            "WIP and throughput grow together, so the queue is not growing "
            "relative to the exit rate; the absolute length is longer."
        )

    arrivals = sum(row["arrivals"] for row in active)
    departures = sum(throughput_values)
    arrivals_per_week = arrivals / total_weeks
    gap = arrivals - departures
    cumulative_gap = []
    running_arrivals = running_departures = 0
    for row in active:
        running_arrivals += row["arrivals"]
        running_departures += row["throughput"]
        cumulative_gap.append({
            "label": row["label"],
            "gap": running_arrivals - running_departures,
        })
    gap_sentence = (
        f"Arrivals {arrivals_per_week:.1f}/wk vs departures "
        f"{mean_throughput:.1f}/wk ({gap:+d} item{'s' if gap != 1 else ''})."
    )

    little = _little(court, data, window, mean_wip, mean_throughput)
    return {
        "available": True,
        "bucket": bucket,
        "flag": flag,
        "reason": reason,
        # prd/60 reads exactly this: only an overloaded queue refuses a forecast.
        "refuses_forecast": refuses_forecast(flag),
        "buckets": len(active),
        "min_buckets": MIN_BUCKETS,
        "leading_empty": leading_empty,
        "wip": {
            "slope_per_week": wip_slope,
            "from": wip_values[0],
            "to": wip_values[-1],
            "mean": round(mean_wip, 2),
            "trend": _direction(wip_slope, WIP_SLOPE),
            "population": "open PRs at the bucket end",
        },
        "throughput": {
            "slope_per_week": throughput_slope,
            "mean": round(mean_throughput, 2),
            "trend": _direction(throughput_slope, THROUGHPUT_SLOPE),
            "unit": "PRs merged per week",
        },
        "little": little,
        "arrivals_departures": {
            "arrivals_per_week": round(arrivals_per_week, 2),
            "departures_per_week": round(mean_throughput, 2),
            "gap_items": gap,
            "sentence": gap_sentence,
            "cumulative": cumulative_gap,
        },
        "thresholds": {
            "wip_slope_per_week": WIP_SLOPE,
            "throughput_slope_per_week": THROUGHPUT_SLOPE,
            "filling_ratio": FILLING_RATIO,
            "dead_wip_ratio": DEAD_WIP_RATIO,
        },
        "points": [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in rows
        ],
        "window": {
            "start": active[0]["start"],
            "end": active[-1]["end"],
        },
        "note": note,
    }

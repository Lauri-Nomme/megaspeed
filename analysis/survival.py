"""Kaplan–Meier survival analysis for censored cycle times (prd/05, goal 7).

A PR that is still open at export time has **no** merge time yet. Dropping it
from a cycle-time average (or a median) is not a neutral choice: the items that
linger are exactly the ones a merged-only number cannot see, so the average is
systematically optimistic. Survival analysis keeps them as **right-censored**
observations — "it survived at least this long" — and estimates the
time-to-merge distribution from what is known.

    observations ──► Kaplan–Meier ──► S(t), median, quantiles, a small curve

The estimator, stated once so the numbers can be audited:

    S(t) = Π over event times t_i <= t of (1 − d_i / n_i)

where ``d_i`` is the number of merges at ``t_i`` and ``n_i`` the number of PRs
still at risk (open or not yet merged) just before ``t_i``. Censoring at ``t_i``
does not change ``S``; it only removes that PR from the risk set afterwards.
An event and a censoring at the same instant are handled with the standard
convention: the event is counted first, the censoring leaves the risk set after.

Declined PRs are treated as censored at their close time, not as events: they
did not merge, and whether they *would* have merged later is unobservable. That
is a competing risk folded into censoring — the usual KM approximation, and it
makes the estimate slightly optimistic rather than silently dropping them.

No dependency on the store: this module takes numbers. ``analysis.rules`` turns
PR timestamps into business hours (prd/19) and calls it, so ``hours_mode`` stays
consistent with every other metric.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

# A "small" survival curve: enough points to draw, not the whole risk set.
MAX_CURVE_POINTS = 40

NOTE = (
    "Kaplan–Meier on time-to-merge; open PRs are right-censored at the snapshot "
    "and declined PRs at their close time (a competing risk folded into "
    "censoring), so the estimate includes the items a merged-only median drops"
)


@dataclass(frozen=True, slots=True)
class Observation:
    """One PR's time-to-merge: ``event`` merged at ``time``, else censored."""

    time: float
    event: bool
    key: str = ""


def _empty() -> dict:
    return {
        "n": 0,
        "events": 0,
        "censored": 0,
        "median_hours": None,
        "median_reached": False,
        "quantiles": {"p25": None, "p50": None, "p75": None},
        "curve": [],
        "note": NOTE,
    }


def _quantile(steps: list[tuple[float, float]], q: float) -> float | None:
    """The smallest time whose survival is at or below ``1 − q``."""
    threshold = 1.0 - q
    for hours, survival in steps:
        if survival <= threshold + 1e-12:
            return hours
    return None


def _downsample(steps: list[dict], median_hours: float | None,
                max_points: int) -> list[dict]:
    """Keep the curve drawable: even stride, never dropping the median step."""
    if len(steps) <= max_points:
        return steps
    stride = (len(steps) - 1) / (max_points - 1)
    keep = {round(index * stride) for index in range(max_points)}
    keep.add(0)
    keep.add(len(steps) - 1)
    if median_hours is not None:
        for index, step in enumerate(steps):
            if step["hours"] >= median_hours:
                keep.add(index)
                break
    return [step for index, step in enumerate(steps) if index in keep]


def kaplan_meier(observations: list[Observation],
                 max_points: int = MAX_CURVE_POINTS) -> dict:
    """Fit the Kaplan–Meier estimator to ``observations`` (times, censoring).

    Deterministic: ties are grouped, an event at a time precedes a censoring at
    the same time, and the output is rounded so two identical inputs always
    produce the same payload.
    """
    if not observations:
        return _empty()

    grouped: dict[float, list[int]] = defaultdict(lambda: [0, 0])  # time -> [events, censored]
    for observation in observations:
        bucket = grouped[max(0.0, float(observation.time))]
        bucket[0 if observation.event else 1] += 1

    total = len(observations)
    at_risk = total
    survival = 1.0
    steps: list[tuple[float, float]] = []      # (hours, survival) unrounded
    curve: list[dict] = []
    for hours in sorted(grouped):
        events, censored = grouped[hours]
        if events:
            survival *= 1.0 - events / at_risk
            steps.append((hours, survival))
            curve.append({
                "hours": round(hours, 2),
                "survival": round(survival, 4),
                "at_risk": at_risk,
                "events": events,
                "censored": censored,
            })
        at_risk -= events + censored

    median_hours = _quantile(steps, 0.5)
    return {
        "n": total,
        "events": sum(bucket[0] for bucket in grouped.values()),
        "censored": sum(bucket[1] for bucket in grouped.values()),
        "median_hours": round(median_hours, 2) if median_hours is not None else None,
        "median_reached": median_hours is not None,
        "quantiles": {
            name: round(value, 2) if value is not None else None
            for name, value in (
                ("p25", _quantile(steps, 0.25)),
                ("p50", median_hours),
                ("p75", _quantile(steps, 0.75)),
            )
        },
        "curve": _downsample(curve, median_hours, max_points),
        "note": NOTE,
    }

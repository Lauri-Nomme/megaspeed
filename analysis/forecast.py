"""Probabilistic throughput forecast (prd/60): *how many, by when*.

prd/52 classifies open stories; it cannot say "how many of 30 land by 27 Nov".
This module answers that question with a **bootstrap over weekly stories
delivered** — a count, never points (concepts.md §1: points and logged hours do
not track time in this store, r ≈ −0.05) — and refuses with a reason when its
assumptions break:

* ``sparse``   — fewer than :data:`MIN_WEEKS` settled weeks of history;
* ``erratic``  — CV > :data:`MAX_CV` (or a flat zero series): the interval
  would be so wide it would look like a forecast and behave like a guess;
* ``overload`` — prd/61's stability flag is ``overload`` (WIP rising while
  throughput is flat or falling). Little's Law says cycle time will grow, so a
  throughput forecast would be optimistic. ``filling`` only warns.

The answer is an **interval**, not a point: ``how_many`` is P15/P50/P85 of the
simulated delivered count, capped at the target, and ``p_reach_target`` is the
share of trials whose *uncapped* total reaches it (a capped P85 of "30" can hide
a 29 % chance). The "by" date uses P85 because a date is a promise; the
"how many" uses P15 for the same reason (prd/59's 85 % edge convention).

The simulation is **seeded**, so two fetches of the same store, window and
request return the identical interval. It is an empirical bootstrap: weekly
counts are resampled with replacement, with no normal assumption.

One consequence worth spelling out for the UI: capping is monotone, so for any
target ``m`` the capped quantile is ``min(uncapped_quantile, m)``. That lets the
card re-render a changed target from the payload's pre-computed quantiles and
:func:`p_reach_curve` — the client never interpolates and never re-simulates.

No store writes; every number is a projection of the ball-court, the ticket
layer and the store's own SPRINT objects.
"""

from __future__ import annotations

import bisect
import math
import random
import zlib
from datetime import date, datetime, timedelta

from analysis import jira as jira_analysis
from analysis import stability as stability_view
from analysis import trend as trend_view
from ingest.store import iso

#: A forecast needs at least this many settled weeks. Eight is the first sample
#: where the p15–p85 span is a range rather than two adjacent observations.
MIN_WEEKS = 8
#: Coefficient of variation above which the distribution is "erratic" — the
#: interval would swallow the answer.
MAX_CV = 0.6
#: 10,000 resamples: ~2 ms for ≤26 periods, and stable to the integer.
TRIALS = 10_000
#: "How many" is read at the cautious edge (prd/59's convention).
HOW_MANY_Q = 0.15
#: A date is a promise, so "by" is read at the confident edge.
WHEN_Q = 0.85
WHEN_P95 = 0.95
#: Explicit forecast horizons longer than this are not a throughput question.
MAX_HORIZON_WEEKS = 52
DEFAULT_HORIZON_WEEKS = 26
#: A target smaller than this is a risk question (prd/52/59), not throughput.
MIN_TARGET = 5
#: The evidence behind "not points": points vs logged hours in this store.
POINTS_HOURS_R = -0.05
#: Story points and logged hours are a caveat, not the forecast's input.
UNIT_STORIES = "stories delivered / week"
UNIT_PRS = "PRs merged / week"

_WEEK = timedelta(days=7)
_DAY = timedelta(days=1)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: The refusal vocabulary (prd/60 §Data model). ``overload`` is prd/61's flag.
REFUSAL_CODES = ("sparse", "erratic", "overload")


# --------------------------------------------------------------------------- #
# small shared helpers
# --------------------------------------------------------------------------- #
def quantile(values, q: float) -> int | float | None:
    """Nearest-rank quantile — the same rule ``rules._percentile`` uses.

    ``ceil(q·n)`` with a 1-based index and clamped endpoints, so a quantile is
    always an observed value (never an interpolation) and equals a manual
    recompute exactly.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def mean(values) -> float | None:
    return sum(values) / len(values) if values else None


def sd(values) -> float | None:
    """Population standard deviation — the spread of the observed weeks.

    The bootstrap resamples the *observed* weeks, so the sample *is* the
    population; the PRD's mean 6.7 / sd 2.5 / CV 0.38 are computed this way.
    """
    if not values:
        return None
    centre = mean(values)
    return math.sqrt(sum((value - centre) ** 2 for value in values) / len(values))


def cv(values) -> float | None:
    spread = sd(values)
    centre = mean(values)
    if spread is None or not centre:
        return None
    return spread / centre


def seed_for(tenant: str, as_of: str | None, n: int, periods: int,
             horizon: int) -> int:
    """The deterministic seed: one interval per store stamp and request."""
    key = f"{tenant}|{as_of or ''}|{n}|{periods}|{horizon}"
    return zlib.crc32(key.encode("utf-8"))


def _num(value, digits: int = 2):
    return round(float(value), digits) if value is not None else None


def periods_to_date(period_index: int, start: datetime | date | str) -> str | None:
    """The last day of period ``period_index`` counted from ``start``.

    ``period 5`` from 30 Nov is **3 Jan** (30 Nov + 7×5 − 1 days), because a
    period is a closed week whose last day is the honest "by" date.
    """
    moment = parse_moment(start)
    if moment is None or period_index is None or period_index < 1:
        return None
    return (moment + _WEEK * period_index - _DAY).date().isoformat()


def parse_moment(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 16)
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(hour=16)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return None


def parse_by(value) -> str:
    """``sprint`` or an ISO date; anything else is a 400 (never a silent default)."""
    text = (value or "").strip()
    if not text or text == "sprint":
        return "sprint"
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        raise ValueError("by must be 'sprint' or an ISO date (YYYY-MM-DD)") from None


def day_label(value) -> str:
    """``27 Dec`` — the card's date label (never a raw ISO in prose)."""
    moment = parse_moment(value)
    if moment is None:
        return ""
    return f"{moment.day} {_MONTHS[moment.month - 1]}"


def interval_label(how_many: dict | None) -> str:
    """``22–30`` (en dash) — the interval *is* the headline."""
    if not how_many:
        return ""
    return f"{how_many.get('p15')}–{how_many.get('p85')}"


# --------------------------------------------------------------------------- #
# the throughput series
# --------------------------------------------------------------------------- #
def _delivered_at(story, outcomes: dict) -> datetime | None:
    """When a story was *delivered*: its final done boundary and no open PR.

    prd/37 / concepts.md §6: Jira done with a named PR still open is
    ``unmerged``, not delivered — the throughput series must not disagree with
    the sprint lens. A story's ``done`` is the **final** done (prd/36: a reopen
    puts the finish later), which is also the boundary the sprint lens quotes.
    """
    if jira_analysis.story_unmerged_prs(story):
        return None
    outcome = outcomes.get(story.key) or {}
    cycle = jira_analysis.cycle_for(story)
    return cycle.boundaries.get("done") or outcome.get("done_at")


def throughput(court, data, window) -> dict:
    """Weekly delivered **counts** — the bootstrap's only input.

    Monday 00:00 UTC buckets (the event histogram alignment, prd/50), clipped to
    the analysis window. A bucket's count is stories whose delivered boundary
    falls inside it; the bucket containing the snapshot is ``in_progress`` and
    excluded from the sample (a partial week is not a week). Leading empty
    buckets are dropped and counted: the window's first Monday can precede the
    team's first delivery, and a run of zeros before the team existed would bend
    the bootstrap towards "nothing lands" — the same honesty rule prd/61's
    stability strip uses. Zero weeks *inside* the active span stay (they are
    data, not missing buckets).
    """
    if court is None:
        return {
            "values": [],
            "buckets": [],
            "n": 0,
            "mean": None,
            "sd": None,
            "cv": None,
            "basis": UNIT_STORIES,
            "unit": "stories",
            "leading_empty": 0,
            "source": "stories",
            "note": "no store: nothing to measure",
        }
    story_basis = bool(data is not None and getattr(data, "stories", None))
    outcomes: dict[str, dict] = {}
    stories = []
    if story_basis:
        _memberships, outcomes, _boundaries = jira_analysis.sprint_outcomes(
            court, data
        )
        stories = list(data.stories)

    bounds = trend_view.bucket_bounds(court, data if story_basis else None, window)
    start, end = window
    buckets: list[dict] = []
    for row in bounds:
        low = max(row["start"], start)
        high = min(row["end"], end)
        if high <= low:
            continue
        if story_basis:
            count = 0
            for story in stories:
                done_at = _delivered_at(story, outcomes)
                if done_at is not None and low <= done_at < high:
                    count += 1
        else:
            count = sum(
                1 for state in court.prs
                if state.pr["closed_ts"] is not None
                and state.pr.get("state") == "merged"
                and low <= state.pr["closed_ts"] < high
            )
        in_progress = bool(row.get("in_progress"))
        buckets.append({
            "start": iso(low),
            "end": iso(high),
            "count": count,
            "in_progress": in_progress,
        })

    settled = [bucket for bucket in buckets if not bucket["in_progress"]]
    leading_empty = 0
    while settled and settled[0]["count"] == 0:
        settled.pop(0)
        leading_empty += 1
    values = [int(bucket["count"]) for bucket in settled]
    centre = mean(values)
    spread = sd(values)
    return {
        "values": values,
        "buckets": buckets,
        "n": len(values),
        "n_weeks": len(values),
        "mean": _num(centre),
        "sd": _num(spread),
        "cv": _num(cv(values)),
        "basis": UNIT_STORIES if story_basis else UNIT_PRS,
        "unit": "stories" if story_basis else "PRs",
        "leading_empty": leading_empty,
        "source": "stories" if story_basis else "merged_prs",
        "note": (
            None if story_basis else
            "no Jira layer in this store: the forecast reads merged PRs and its "
            "unit says PRs, never stories"
        ),
    }


# --------------------------------------------------------------------------- #
# refusals
# --------------------------------------------------------------------------- #
def refusal_sentence(code: str, *, series: dict | None = None,
                     wip: dict | None = None) -> str:
    """The exact refusal copy from prd/60 — never a paraphrase."""
    series = series or {}
    wip = wip or {}
    if code == "sparse":
        observed = series.get("n", 0)
        return (
            f"Not enough history to forecast: {observed} "
            f"week{'s' if observed != 1 else ''} observed, {MIN_WEEKS} needed."
        )
    if code == "erratic":
        return (
            f"Throughput is erratic (CV {series.get('cv') or 0:.2f}): the range "
            "would be so wide it would look like a forecast and behave like a "
            "guess."
        )
    if code == "overload":
        values = wip.get("wip") or {}
        low = values.get("from")
        high = values.get("to")
        return (
            f"WIP rose {low} → {high} while throughput did not follow — "
            "Little's Law says cycle time will grow, so a throughput forecast "
            "would be optimistic. Fix the queue first (prd/61)."
        )
    return ""


def assess(series: dict, wip: dict | None = None) -> dict:
    """Can this series honestly be forecast? The three refusals, plus warnings.

    ``wip`` is prd/61's stability payload. Only ``overload`` refuses; ``filling``
    is a warning because the exits are still growing. A missing Jira layer does
    not refuse — it changes the *basis* to merged PRs and says so (the unit is
    then PRs, never stories).
    """
    reasons: list[dict] = []
    warnings: list[dict] = []
    values = list(series.get("values") or [])
    n = len(values)
    spread = cv(values)
    flat = sd(values) == 0 if values else True

    if n < MIN_WEEKS:
        reasons.append({
            "code": "sparse",
            "reason": "insufficient_history",
            "sentence": refusal_sentence("sparse", series=series),
            "minimum": MIN_WEEKS,
        })
    if n >= 2 and (flat or (spread is not None and spread > MAX_CV)):
        reasons.append({
            "code": "erratic",
            "reason": "erratic",
            "sentence": refusal_sentence("erratic", series=series),
            "minimum": None,
        })

    flag = (wip or {}).get("flag")
    if (wip or {}).get("refuses_forecast") or flag == "overload":
        reasons.append({
            "code": "overload",
            "reason": "wip_overload",
            "sentence": refusal_sentence("overload", wip=wip),
            "minimum": None,
        })
    elif flag == "filling":
        values_block = (wip or {}).get("wip") or {}
        throughput_block = (wip or {}).get("throughput") or {}
        warnings.append({
            "code": "wip_filling",
            "sentence": (
                f"WIP rose {values_block.get('from')} → "
                f"{values_block.get('to')} "
                f"({_signed(values_block.get('slope_per_week'))}/wk) while "
                f"throughput rose {_signed(throughput_block.get('slope_per_week'))}"
                "/wk — the interval is optimistic; plan at P85."
            ),
        })
    if series.get("source") == "merged_prs":
        warnings.append({
            "code": "pr_basis",
            "sentence": (
                "no Jira layer: the forecast reads merged PRs per week, so the "
                "unit is PRs, never stories"
            ),
        })

    return {
        "available": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "minimum_weeks": MIN_WEEKS,
        "max_cv": MAX_CV,
        "flag": flag,
    }


def _signed(value) -> str:
    if value is None:
        return "–"
    number = float(value)
    sign = "+" if number >= 0 else "−"
    return f"{sign}{abs(number):.1f}"


# --------------------------------------------------------------------------- #
# the Monte Carlo
# --------------------------------------------------------------------------- #
def p_reach_curve(sums, maximum: int | None = None) -> list[dict]:
    """Exact ``p(uncapped total ≥ n)`` for every integer target up to the max.

    The capped count hides confidence, so the card always prints p(reach).
    Precomputing the whole curve (from the same trials) is what lets a target
    change re-render **without re-simulating or interpolating**.
    """
    if not sums:
        return []
    top = int(max(sums)) if maximum is None else int(maximum)
    top = max(0, min(top, 10_000))
    ordered = sorted(sums)
    total = len(ordered)
    # index of the first sum >= n via binary search, so the curve is O(top·log n)
    curve = []
    for target in range(0, top + 1):
        reached = total - bisect.bisect_left(ordered, target)
        curve.append({"n": target, "p": round(reached / total, 4)})
    return curve


def _sums(series_values: list[int], periods: int, trials: int,
          rng: random.Random) -> list[int]:
    return [
        sum(rng.choice(series_values) for _ in range(periods))
        for _ in range(trials)
    ]


def forecast(series: dict, n: int, periods: int, seed: int, *,
             horizon_weeks: int = DEFAULT_HORIZON_WEEKS,
             trials: int = TRIALS, delivered_now: int = 0,
             start: datetime | str | None = None) -> dict:
    """The bootstrap: P15/P50/P85 of *how many*, p(reach), and P85/when.

    ``delivered_now`` is added to each trial's simulated deliveries before the
    cap, so an in-flight sprint forecasts the **total** it will have finished,
    not a fresh set of ``periods`` from zero. All quantiles are capped at ``n``;
    ``uncapped`` is carried because the client re-caps it for a changed target.
    """
    values = [int(value) for value in series.get("values") or []]
    rng = random.Random(seed)
    sums = _sums(values, max(0, periods), trials, rng)
    totals = [delivered_now + value for value in sums]
    capped = [min(value, n) for value in totals]
    how_many = {
        "p15": quantile(capped, HOW_MANY_Q),
        "p50": quantile(capped, 0.50),
        "p85": quantile(capped, WHEN_Q),
    }
    uncapped = {
        "p15": quantile(totals, HOW_MANY_Q),
        "p50": quantile(totals, 0.50),
        "p85": quantile(totals, WHEN_Q),
    }
    reached = sum(1 for value in totals if value >= n)
    # "when": cumulate draws until the running total covers the *remaining*
    # target. A trial that never gets there inside the horizon is counted in
    # `unfinished_share`, never hidden.
    remaining = max(0, n - delivered_now)
    finished: list[int] = []
    unfinished = 0
    horizon = max(1, int(horizon_weeks))
    if remaining == 0:
        finished = [0] * trials
    else:
        for _ in range(trials):
            running = 0
            landed = None
            for week in range(horizon):
                running += rng.choice(values)
                if running >= remaining:
                    landed = week + 1
                    break
            if landed is None:
                unfinished += 1
            else:
                finished.append(landed)

    def when_quantile(q: float):
        rank = math.ceil(q * trials)
        if rank <= 0:
            return 0
        if rank > len(finished):
            return None
        return sorted(finished)[rank - 1]

    where = {
        "p50_weeks": when_quantile(0.50),
        "p85_weeks": when_quantile(WHEN_Q),
        "p95_weeks": when_quantile(WHEN_P95),
        "unfinished_share": round(unfinished / trials, 4) if trials else 0.0,
    }
    for key, weeks in (("p50", where["p50_weeks"]),
                       ("p85", where["p85_weeks"]),
                       ("p95", where["p95_weeks"])):
        where[f"{key}_date"] = periods_to_date(weeks, start) if weeks else None
    where["start"] = (
        parse_moment(start).date().isoformat()
        if parse_moment(start) is not None else None
    )
    where["horizon_weeks"] = horizon
    return {
        "how_many": how_many,
        "uncapped": uncapped,
        "p_reach_target": round(reached / trials, 4) if trials else 0.0,
        "when": where,
        "trials": trials,
        "seed": seed,
        "method": "bootstrap resample of weekly counts",
        "capped_at": n,
        "delivered_now": delivered_now,
        "periods": periods,
        "_sums": totals,
    }


def _risk_rows(risk: dict | None, limit: int = 3) -> list[dict]:
    """prd/52's rows in the card's compact shape (name, holder, wait)."""
    rows = list((risk or {}).get("rows") or [])
    return [
        {
            "kind": row.get("reason"),
            "key": row.get("key"),
            "pr": (
                f"#{row['detail']['prs'][0]}"
                if (row.get("detail") or {}).get("prs") else None
            ),
            "holder": " + ".join(row.get("who") or []) or None,
            "hours": row.get("waiting_hours"),
            "label": row.get("label"),
        }
        for row in rows[:limit]
    ]


def adjust(forecast_block: dict, risk: dict | None, n: int) -> dict:
    """prd/52's classes as *adjustments*, never inputs.

    Two counts, never blended: the model's number (``how_many_if_the_at_risk_
    merge``) and the ceiling with the items whose delivery is in doubt removed
    (``how_many_if_they_do_not``). ``done-unmerged`` and ``no-live-pr`` are the
    classes with no confirmed code path to delivery; a ``stuck-on`` or
    ``approved-unmerged`` item is live work the model assumes lands (that is the
    point of naming its holder on the "can't see" line).
    """
    rows = list((risk or {}).get("rows") or [])
    doubtful = [
        row for row in rows
        if row.get("reason") in ("done-but-unmerged", "no-live-pr")
    ]
    ceiling = max(0, n - len(doubtful))
    uncapped = forecast_block.get("uncapped") or {}
    if_they_do_not = {
        "p15": min(uncapped.get("p15") or 0, ceiling),
        "p50": min(uncapped.get("p50") or 0, ceiling),
        "p85": min(uncapped.get("p85") or 0, ceiling),
    }
    delivered_now = int(forecast_block.get("delivered_now") or 0)
    return {
        "delivered_now": delivered_now,
        "remaining": max(0, n - delivered_now),
        "at_risk": _risk_rows(risk),
        "at_risk_count": len(rows),
        "how_many_if_the_at_risk_merge": dict(forecast_block.get("how_many") or {}),
        # the ceiling with the doubtful items removed; the model's distribution
        # is unchanged, so the capped quantiles are the exact same trials
        "how_many_if_they_do_not": if_they_do_not,
        "ceiling_if_they_do_not": ceiling,
        "doubtful": len(doubtful),
        "delivered_if_the_blocked_items_hold": ceiling,
    }


def method_sentence(series: dict, trials: int = TRIALS) -> str:
    """The exact provenance sentence (prd/60 §Honesty language)."""
    n = series.get("n") or 0
    weeks = f"{trials:,} resamples of {n} week{'s' if n != 1 else ''}"
    return (
        f"Method: {weeks} of {series.get('unit') or 'stories'} delivered "
        f"(mean {series.get('mean') if series.get('mean') is not None else 0:.1f}, "
        f"sd {series.get('sd') if series.get('sd') is not None else 0:.1f}, "
        f"CV {series.get('cv') if series.get('cv') is not None else 0:.2f}). "
        "Not points — points and logged hours do not track time here "
        f"(r ≈ −{abs(POINTS_HOURS_R):.2f})."
    )


def edge_caveat() -> str:
    """The 85 % edge, stated once — exact copy (prd/60)."""
    return (
        "Both numbers are the 85 % edge: \"by\" uses P85 because a date is a "
        "promise; \"how many\" uses P15 for the same reason. A count is capped "
        "at the target, so a P85 of \"30\" can hide a 29 % chance of reaching "
        "30 — the p(reach) line says which it is."
    )


# --------------------------------------------------------------------------- #
# the whole answer
# --------------------------------------------------------------------------- #
def sprint_scope(court, data, sid: int) -> dict:
    """One sprint's committed scope and delivered count (the sprint lens' own).

    ``delivered`` is prd/37's "done by the close/snapshot and no open PR", the
    same number the sprint strip prints, so the forecast's ``delivered_now`` can
    never disagree with the band above it.
    """
    sprint = next(
        (row for row in data.sprints.values() if row.get("id") == sid),
        None,
    )
    if sprint is None:
        raise ValueError(f"unknown sprint {sid}")
    memberships, outcomes, boundaries = jira_analysis.sprint_outcomes(court, data)
    stories = [
        story for story in data.stories
        if sid in (memberships.get(story.key) or {}).get("sprints", [])
    ]
    delivered = 0
    unmerged = 0
    for story in stories:
        outcome = outcomes.get(story.key) or {}
        if outcome.get("outcomes", {}).get(sid) == "by_end":
            if outcome.get("delivered"):
                delivered += 1
            else:
                unmerged += 1
    added_after_start = 0
    start = jira_analysis._sprint_datetime(sprint.get("start"))
    for story in stories:
        for event in story.of("sprint_added"):
            if jira_analysis.event_sprint_id(event) != sid:
                continue
            at_start = bool(jira_analysis.attributes(event).get("at_start"))
            if (
                not at_start and start is not None
                and event["_ts"] > start + timedelta(days=1)
            ):
                added_after_start += 1
                break
    return {
        "sprint": sprint,
        "sprint_id": sid,
        "stories": len(stories),
        "delivered": delivered,
        "unmerged": unmerged,
        "remaining": max(0, len(stories) - delivered),
        "start": start,
        "end": jira_analysis._sprint_datetime(sprint.get("end")),
        "planned_end": sprint.get("planned_end"),
        "state": sprint.get("state"),
        "added_after_start": added_after_start,
    }


def resolve_request(court, data, *, target: str | None, n: int | None,
                    by: str | None, as_of: datetime,
                    periods: int | None = None,
                    default_periods: int = 4) -> dict:
    """Turn the endpoint's target/date into ``n``, ``periods``, ``by``, ``start``.

    ``target=sprint:<id>`` forecasts that sprint's committed scope from its own
    dates; ``open`` forecasts every open story; ``epic:<key>`` the epic's open
    stories. ``by=sprint`` means the sprint's planned close; an ISO date is the
    deadline. The anchor is the later of the snapshot and the sprint's start, so
    a future sprint counts its own weeks and an in-flight sprint counts the
    weeks that are left.
    """
    stories_all = list(getattr(data, "stories", []) or [])

    deadline: datetime | None = None
    anchor = as_of
    scope: dict | None = None
    resolved_target = target or "open"
    if resolved_target.startswith("sprint:"):
        try:
            sid = int(resolved_target.split(":", 1)[1])
        except ValueError:
            raise ValueError("target=sprint:<id> needs an integer sprint id") from None
        scope = sprint_scope(court, data, sid)
        n = scope["stories"] if n is None else n
        if by in (None, "", "sprint"):
            raw = scope["planned_end"] or scope["end"]
            deadline = parse_moment(raw)
        if scope["start"] is not None and scope["start"] > anchor:
            anchor = scope["start"]
    elif resolved_target.startswith("epic:"):
        key = resolved_target.split(":", 1)[1]
        stories = [
            story for story in stories_all
            if story.epic_key == key and jira_analysis.cycle_for(story).boundaries.get("done") is None
        ]
        n = len(stories) if n is None else n
    elif resolved_target in ("open", ""):
        stories = [
            story for story in stories_all
            if jira_analysis.cycle_for(story).boundaries.get("done") is None
        ]
        n = len(stories) if n is None else n
    else:
        raise ValueError(
            "target must be 'open', 'epic:<key>' or 'sprint:<id>'"
        )

    if by not in (None, "", "sprint"):
        deadline = parse_moment(by)
        if deadline is None:
            raise ValueError("by must be 'sprint' or an ISO date (YYYY-MM-DD)")
    if deadline is not None:
        days = (deadline.date() - anchor.date()).days
        periods = max(1, math.ceil(days / 7.0)) if periods is None else periods
        by_iso = deadline.date().isoformat()
    else:
        periods = default_periods if periods is None else periods
        by_iso = (anchor + _WEEK * periods - _DAY).date().isoformat()
    delivered_now = int((scope or {}).get("delivered") or 0)
    cutoff = None
    backtest = False
    if scope and scope.get("end") is not None and scope["end"] <= as_of:
        # A closed sprint has a measured verdict. Asking the model about it is a
        # **backtest**: use only the history that existed at its start, and
        # forecast its own scope to its own close (the PRD's "the same model
        # applied to sprint 2's start would have refused").
        backtest = True
        anchor = scope["start"] or anchor
        deadline = scope["end"]
        delivered_now = 0
        periods = max(1, math.ceil(
            (deadline.date() - anchor.date()).days / 7.0
        ))
        by_iso = deadline.date().isoformat()
        cutoff = anchor.isoformat()
    return {
        "target": resolved_target,
        "n": int(n or 0),
        "periods": max(1, int(periods or 1)),
        "by": by_iso,
        "start": anchor,
        "delivered_now": delivered_now,
        "sprint": scope,
        "cutoff": cutoff,
        "backtest": backtest,
    }


def payload(court, data, window, *, n: int, periods: int,
            target: str | None = None, by: str | None = None,
            horizon_weeks: int = DEFAULT_HORIZON_WEEKS,
            risk: dict | None = None, tenant: str = "",
            as_of: str | None = None, trials: int = TRIALS,
            sprint: dict | None = None, delivered_now: int = 0,
            series: dict | None = None, wip: dict | None = None,
            start: datetime | str | None = None,
            backtest: bool = False) -> dict:
    """Assemble one forecast answer; ``available:false`` carries the reason.

    A refused or delegated forecast carries **no numbers**: the UI renders the
    sentence (and, for a small set, prd/52's risk rows), never a zero dressed up
    as an interval.
    """
    series = series if series is not None else throughput(court, data, window)
    wip = wip if wip is not None else stability_view.payload(court, data, window)
    assessment = assess(series, wip)
    request = {
        "n": n,
        "periods": periods,
        "by": by,
        "target": target,
        "horizon_weeks": horizon_weeks,
        "confidence": "P15–P85",
    }
    sprint = sprint or {}
    base = {
        "throughput": series,
        "assess": assessment,
        "wip_flag": wip.get("flag"),
        "wip": {
            "flag": wip.get("flag"),
            "from": (wip.get("wip") or {}).get("from"),
            "to": (wip.get("wip") or {}).get("to"),
            "slope_per_week": (wip.get("wip") or {}).get("slope_per_week"),
        },
        "request": request,
        "method": method_sentence(series, trials),
        "caveat": edge_caveat(),
        "seed_recipe": "crc32(tenant|as_of|n|periods|horizon)",
        "scope_added_after_start": (
            sprint.get("added_after_start")
            or sprint.get("added_after_start_stories") or 0
        ),
        "min_weeks": MIN_WEEKS,
        "max_cv": MAX_CV,
        "backtest": bool(backtest),
    }
    # prd/60: a small set is a risk question first — the prd/52 rows name the
    # item and its holder, which is a better answer than "not enough history".
    if n <= 0:
        base.update({
            "available": False,
            "forecast": None,
            "adjust": None,
            "delegate": {
                "to": "prd/52",
                "reason": "nothing to forecast",
                "rows": _risk_rows(risk),
            },
            "note": "nothing to forecast: the target has no items",
        })
        return base

    remaining = max(0, n - delivered_now)
    if n < MIN_TARGET or remaining < MIN_TARGET:
        reason = (
            f"a {remaining}-item set is a risk question, not a throughput "
            "question"
        )
        base.update({
            "available": True,
            "forecast": None,
            "adjust": None,
            "delegate": {
                "to": "prd/52",
                "reason": reason,
                "rows": _risk_rows(risk),
            },
            "note": (
                f"{reason}; the prd/52 at-risk rows name the item and its holder "
                "(prd/52/59)"
            ),
        })
        return base

    if not assessment["available"]:
        base.update({
            "available": False,
            "forecast": None,
            "adjust": None,
            "delegate": None,
            "note": assessment["reasons"][0]["sentence"] if assessment["reasons"]
            else None,
        })
        return base

    seed = seed_for(tenant, as_of, n, periods, horizon_weeks)
    block = forecast(
        series, n, periods, seed, horizon_weeks=horizon_weeks,
        trials=trials, delivered_now=delivered_now,
        start=start or as_of,
    )
    curve = p_reach_curve(block.pop("_sums"), maximum=max(
        n, int(block["uncapped"]["p85"] or 0)
    ))
    block["p_reach_curve"] = curve
    block["headline"] = (
        f"{interval_label(block['how_many'])} of {n}"
        + (f" · by {day_label(by)}" if by and by != "sprint" else "")
    )
    block["subline"] = (
        f"P50 {block['how_many']['p50']} · "
        f"{round(block['p_reach_target'] * 100)} % chance all {n} · "
        f"{trials:,} resamples · {series.get('n')} weeks"
    )
    adjustment = adjust(block, risk, n)
    note = None
    if periods > (series.get("n") or 0):
        note = (
            f"forecasting {periods} weeks from {series.get('n')} observed — the "
            "range is the answer"
        )
    base.update({
        "available": True,
        "forecast": block,
        "adjust": adjustment,
        "delegate": None,
        "note": note,
    })
    return base

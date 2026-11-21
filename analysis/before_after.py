"""Before/after comparison (prd/61): did the change work — or can't we tell?

A trend shows *that* a number moved; it cannot say whether the move is bigger
than the noise. This module compares the **item-level distribution** either side
of a split (a dated annotation): PRs created before vs after, stories created
before vs after. Never bucket aggregates — a weekly mean hides the distribution
and shrinks the sample to one number per week.

Two statistics, in this order of importance:

* the **difference of medians** with a percentile-bootstrap 95 % interval
  (10k resamples, deterministic seed derived from the same cache key as the
  trend, so a cached and a cold call agree);
* a two-sided **Mann–Whitney U** p (distribution-free rank test), reported in
  the payload/tooltip — never as the headline.

The answer is one of four outcomes and nothing else (concepts.md §6):

``better`` · ``worse`` · ``no clear change`` · ``can't tell at this n``

A wide interval is not a failed test: it is the honest answer that this sample
cannot separate the change from everything else that also changed. The payload
always carries both ``n``s, the interval and the reason, so no view can show a
bare p-value.

The censoring rule is the product's usual one (prd/55): an open item's running
age is **not** a completed duration, so it is excluded from both sides and
counted (``censored_before``/``censored_after``, with ``censored`` as their
sum). The basis string travels with the answer so it cannot be mistaken for
the bucket series; the panel prints the disclosure once, below the table.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date, datetime

from analysis import jira as jira_analysis
from analysis import rules, trend

#: Per side: below this many finished items the difference of medians is noise.
MIN_ITEMS = 8
#: Per side: a comparison over fewer buckets cannot separate a change from a
#: seasonal/noisy week, so it is a stated "short window", not a number.
MIN_BUCKETS = 3
#: The smallest change this product acts on (the lab's ±20 % gate, concepts §7).
NOISE_BAND = 0.20
#: Percentile-bootstrap resamples.
TRIALS = 10_000

BETTER = "better"
WORSE = "worse"
NO_CLEAR_CHANGE = "no_clear_change"
CANT_TELL = "cant_tell"
OUTCOMES = (BETTER, WORSE, NO_CLEAR_CHANGE, CANT_TELL)

#: The metrics this panel compares, as item-level durations. PR cycle reuses the
#: ``cycle_median`` id the trend already uses; the story clock is its own
#: population and is named so the two can never be read as one series.
COMPARE_METRICS: dict[str, dict] = {
    "story_cycle": {
        "label": "Story cycle",
        "unit": "hours",
        "higher_is_better": False,
        "basis": "stories created before/after the split, created → done",
    },
    "cycle_median": {
        "label": "PR cycle",
        "unit": "hours",
        "higher_is_better": False,
        "basis": "PRs created before/after the split, created → merged",
    },
}

NOTE_INTERVAL = (
    "A before/after is not an experiment: it compares two samples of items "
    "either side of the change, with everything else that also changed. The "
    "interval is the answer."
)


def metric_ids() -> list[dict]:
    return [{"id": key, **value} for key, value in COMPARE_METRICS.items()]


# --------------------------------------------------------------------------- #
# statistics (pure; no store)
# --------------------------------------------------------------------------- #
def median(values) -> float | None:
    """The lower median: the order statistic at ``(n − 1) // 2``.

    It is always an item the team actually finished, never a value invented
    between two items, and it is the convention the PRD's evidence quotes
    (62 items → the 31st, not the mean of the 31st and 32nd).
    """
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    return ordered[(len(ordered) - 1) // 2]


def percentile(values, q: float) -> float | None:
    """Nearest-rank percentile (the same convention ``rules._percentile`` uses)."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _resample_median(rng: random.Random, values: list[float]) -> float:
    n = len(values)
    sample = [values[rng.randrange(n)] for _ in range(n)]
    sample.sort()
    return sample[(n - 1) // 2]


def bootstrap_ci(before, after, *, seed: int = 0, trials: int = TRIALS,
                 alpha: float = 0.05) -> list[float] | None:
    """Percentile-bootstrap CI for the difference of medians (after − before).

    Deterministic for a given ``seed`` (``random.Random``, not the global RNG),
    so a cached payload and a cold recompute agree — the PRD's cache contract.
    """
    left = [float(value) for value in before]
    right = [float(value) for value in after]
    if not left or not right:
        return None
    rng = random.Random(int(seed) & 0xFFFFFFFFFFFFFFFF)
    differences = []
    for _ in range(int(trials)):
        differences.append(
            _resample_median(rng, right) - _resample_median(rng, left)
        )
    differences.sort()
    low = differences[int(math.floor((alpha / 2.0) * (len(differences) - 1)))]
    high = differences[int(math.ceil((1.0 - alpha / 2.0) * (len(differences) - 1)))]
    return [round(low, 2), round(high, 2)]


def _exact_mann_whitney(n1: int, n2: int) -> list[float]:
    """The null distribution of U (counts) for tie-free samples."""
    # ways[i][j] is a list of counts indexed by U; bounded by i*j.
    rows = [[None] * (n2 + 1) for _ in range(n1 + 1)]
    for i in range(n1 + 1):
        for j in range(n2 + 1):
            size = i * j
            if i == 0 or j == 0:
                rows[i][j] = [1] + [0] * size
                continue
            current = [0] * (size + 1)
            up = rows[i - 1][j]
            left = rows[i][j - 1]
            for u in range(size + 1):
                total = 0
                if u < len(up):
                    total += up[u]
                if u - j >= 0 and u - j < len(left):
                    total += left[u - j]
                current[u] = total
            rows[i][j] = current
    return rows[n1][n2]


def mann_whitney(xs, ys) -> dict:
    """Two-sided Mann–Whitney U with an exact null for small, tie-free samples.

    ``U`` is the smaller of the two rank sums expressed as a U statistic, which
    is the standard two-sided statistic. The exact tail is used when the product
    is small enough to enumerate (no ties); otherwise the tie-corrected normal
    approximation with a continuity correction is used. The method travels in
    the payload so a reader can tell which one answered.
    """
    left = [float(value) for value in xs]
    right = [float(value) for value in ys]
    n1, n2 = len(left), len(right)
    if not n1 or not n2:
        return {"u": None, "p_value": None, "test": "mann_whitney_u",
                "method": None, "note": "empty sample"}
    tagged = [(value, 0) for value in left] + [(value, 1) for value in right]
    tagged.sort(key=lambda pair: pair[0])
    ranks: dict[int, float] = {}
    ties = []
    index = 0
    while index < len(tagged):
        end = index
        while end + 1 < len(tagged) and tagged[end + 1][0] == tagged[index][0]:
            end += 1
        average = (index + end + 2) / 2.0  # 1-based average rank
        ties.append(end - index + 1)
        for position in range(index, end + 1):
            ranks[position] = average
        index = end + 1
    rank_sum_left = sum(
        ranks[position] for position, (_, side) in enumerate(tagged) if side == 0
    )
    u1 = rank_sum_left - n1 * (n1 + 1) / 2.0
    u2 = n1 * n2 - u1
    u = min(u1, u2)
    has_ties = any(count > 1 for count in ties)
    if not has_ties and n1 * n2 <= 400:
        counts = _exact_mann_whitney(n1, n2)
        total = math.comb(n1 + n2, n1)
        observed = int(round(u))
        lower = sum(counts[: observed + 1]) / total
        upper = sum(counts[observed:]) / total
        p = min(1.0, 2.0 * min(lower, upper))
        method = "exact"
    else:
        mean = n1 * n2 / 2.0
        tie_term = sum(count ** 3 - count for count in ties) / (
            (n1 + n2) * (n1 + n2 - 1)
        ) if n1 + n2 > 1 else 0.0
        variance = (n1 * n2 / 12.0) * ((n1 + n2 + 1) - tie_term)
        if variance <= 0:
            return {"u": u, "p_value": 1.0, "test": "mann_whitney_u",
                    "method": "normal", "note": "no variance in the ranks"}
        z = (u - mean + 0.5) / math.sqrt(variance)
        p = math.erfc(abs(z) / math.sqrt(2.0))
        method = "normal"
    return {
        "u": round(u, 2),
        "p_value": round(min(1.0, max(0.0, p)), 4),
        "test": "mann_whitney_u",
        "method": method,
    }


# --------------------------------------------------------------------------- #
# the outcome rule
# --------------------------------------------------------------------------- #
def classify(*, n_before: int, n_after: int, median_before: float | None,
             median_after: float | None, ci: list[float] | None,
             higher_is_better: bool, buckets_before: int = MIN_BUCKETS,
             buckets_after: int = MIN_BUCKETS,
             confounder: str | None = None,
             min_items: int = MIN_ITEMS,
             min_buckets: int = MIN_BUCKETS,
             noise_band: float = NOISE_BAND) -> dict:
    """One of the four outcomes, in the PRD's stated order.

    1. too few items either side → ``cant_tell`` (``small_n``);
    2. too few buckets either side → ``cant_tell`` (``short_window``); another
       annotation within ±1 bucket → ``cant_tell`` (``confounded``);
    3. the CI excludes zero → ``better``/``worse`` by ``higher_is_better``;
    4. a sub-20 % shift whose interval lies inside ±20 % of the before median →
       ``no_clear_change`` ("tight around zero");
    5. otherwise → ``cant_tell`` (``wide_interval``).
    """
    if n_before < min_items or n_after < min_items:
        return {"outcome": CANT_TELL, "reason": "small_n"}
    if buckets_before < min_buckets or buckets_after < min_buckets:
        return {"outcome": CANT_TELL, "reason": "short_window"}
    if confounder:
        return {"outcome": CANT_TELL, "reason": "confounded"}
    if ci is None or median_before is None or median_after is None:
        return {"outcome": CANT_TELL, "reason": "wide_interval"}
    low, high = float(ci[0]), float(ci[1])
    if low > 0 or high < 0:
        improves = low > 0 if higher_is_better else high < 0
        return {
            "outcome": BETTER if improves else WORSE,
            "reason": "interval_excludes_zero",
        }
    band = abs(float(median_before)) * float(noise_band)
    inside = (
        abs(float(median_after) - float(median_before)) < band
        and low >= -band
        and high <= band
    )
    if inside:
        return {"outcome": NO_CLEAR_CHANGE, "reason": "tight_around_zero"}
    return {"outcome": CANT_TELL, "reason": "wide_interval"}


def _sentence(result: dict, unit: str) -> str:
    outcome, reason = result["outcome"], result["reason"]
    before, after = result["before"]["n"], result["after"]["n"]
    if reason == "small_n":
        return (
            f"Can't tell at this n: {before} item{'s' if before != 1 else ''} "
            f"before, {after} after — fewer than the {MIN_ITEMS} per side this "
            "comparison needs."
        )
    if reason == "short_window":
        return (
            "Can't tell at this n: the window is shorter than "
            f"{MIN_BUCKETS} buckets on one side, so a before/after would be one "
            "week's noise. Widen the window or move the split."
        )
    if reason == "confounded":
        return (
            "Two changes in the same week cannot be separated; move one "
            "annotation or widen the window."
            + (f" ({result['confounder']})" if result.get("confounder") else "")
        )
    if outcome == CANT_TELL:
        return (
            f"Can't tell at this n: {before} item{'s' if before != 1 else ''} "
            f"before, {after} after — the interval spans zero, so this data "
            "cannot separate the change from noise."
        )
    if outcome == NO_CLEAR_CHANGE:
        return (
            f"No clear change: the interval is tight around zero (inside "
            f"±{int(NOISE_BAND * 100)} % of the before median)."
        )
    return (
        f"{'Better' if outcome == BETTER else 'Worse'}: the whole 95 % "
        "interval is on one side of zero."
    )


# --------------------------------------------------------------------------- #
# the comparison payload
# --------------------------------------------------------------------------- #
def seed_for(*parts) -> int:
    """A stable integer seed from the trend cache key (never Python's hash)."""
    text = "|".join("" if part is None else str(part) for part in parts)
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def compare(items_before, items_after, *, higher_is_better: bool = False,
            seed: int = 0, buckets_before: int = MIN_BUCKETS,
            buckets_after: int = MIN_BUCKETS, confounder: str | None = None,
            censored_before: int = 0, censored_after: int = 0,
            metric: str = "cycle_median", unit: str = "hours",
            label: str | None = None, basis: str | None = None,
            trials: int = TRIALS) -> dict:
    """The four-outcome comparison of two item samples.

    ``items_*`` are **item-level durations** (floats), never bucket aggregates.
    The CI is computed only when both sides clear ``MIN_ITEMS``: below that the
    honest answer has no bar to draw.
    """
    before = [float(value) for value in items_before]
    after = [float(value) for value in items_after]
    spec = COMPARE_METRICS.get(metric, {})
    p50_before, p50_after = median(before), median(after)
    diff = (
        round(p50_after - p50_before, 2)
        if p50_before is not None and p50_after is not None else None
    )
    enough = (
        len(before) >= MIN_ITEMS
        and len(after) >= MIN_ITEMS
        and buckets_before >= MIN_BUCKETS
        and buckets_after >= MIN_BUCKETS
    )
    ci = bootstrap_ci(before, after, seed=seed, trials=trials) if enough else None
    test = mann_whitney(before, after) if before and after else {
        "u": None, "p_value": None, "test": "mann_whitney_u", "method": None,
        "note": "empty sample",
    }
    verdict = classify(
        n_before=len(before), n_after=len(after),
        median_before=p50_before, median_after=p50_after, ci=ci,
        higher_is_better=higher_is_better,
        buckets_before=buckets_before, buckets_after=buckets_after,
        confounder=confounder,
    )
    result = {
        "metric": metric,
        "label": label or spec.get("label") or metric,
        "unit": unit or spec.get("unit") or "hours",
        "higher_is_better": bool(higher_is_better),
        "basis": basis or spec.get("basis") or "",
        "before": {
            "n": len(before),
            "p50": _round(p50_before),
            "p85": _round(percentile(before, 0.85)),
        },
        "after": {
            "n": len(after),
            "p50": _round(p50_after),
            "p85": _round(percentile(after, 0.85)),
        },
        "diff": diff,
        "ci": ci,
        "p_value": test["p_value"],
        "test": test["test"],
        "test_method": test.get("method"),
        "outcome": verdict["outcome"],
        "reason": verdict["reason"],
        "confounder": confounder,
        "buckets_before": int(buckets_before),
        "buckets_after": int(buckets_after),
        "censored_before": int(censored_before),
        "censored_after": int(censored_after),
        "censored": int(censored_before) + int(censored_after),
        "min_items": MIN_ITEMS,
        "noise_band": NOISE_BAND,
        "trials": int(trials) if ci is not None else 0,
    }
    result["note"] = _sentence(result, result["unit"])
    return result


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


# --------------------------------------------------------------------------- #
# the store-facing service
# --------------------------------------------------------------------------- #
def _parse_split(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value or "").strip()
    if not text:
        raise ValueError("split is required (an ISO date, e.g. 2026-10-08)")
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise ValueError(f"split {text!r} is not an ISO date or timestamp") from None


def parse_split(value) -> datetime:
    """Public wrapper: an ISO date/timestamp for the split (400 on bad input)."""
    return _parse_split(value)


def _bucket_index(bounds: list[dict], stamp: datetime) -> int | None:
    """The bucket a moment belongs to (``None`` when it falls in no bucket)."""
    for index, row in enumerate(bounds):
        if row["start"] <= stamp < row["end"]:
            return index
    return None


def _settled(bounds: list[dict]) -> list[dict]:
    return [row for row in bounds if not row["in_progress"]]


def _items(court, data, window, metric: str) -> list[dict]:
    """One row per item created in the window: ``{start, hours|censored}``.

    The durations are the same business-hours spans the headline uses, so a
    comparison can never price a story on a different clock than the trend.
    """
    start, end = window
    rows: list[dict] = []
    if metric == "story_cycle":
        if data is None:
            return []
        _members, outcomes, _bounds = jira_analysis.sprint_outcomes(court, data)
        for story in data.stories:
            created = story.start
            if created is None or not (start <= created <= end):
                continue
            outcome = outcomes.get(story.key) or {}
            done = outcome.get("done_at")
            if done is None:
                rows.append({"start": created, "hours": None, "censored": True})
                continue
            rows.append({
                "start": created,
                "hours": court.business_hours(story.actor, created, done),
                "censored": False,
            })
        return rows
    if metric == "cycle_median":
        for state in court.prs:
            created = state.pr["created_ts"]
            if created is None or not (start <= created <= end):
                continue
            closed = state.pr["closed_ts"]
            if closed is None:
                rows.append({"start": created, "hours": None, "censored": True})
                continue
            if state.pr["state"] != "merged":
                continue  # declined: closed by definition, never a duration
            rows.append({
                "start": created,
                "hours": court.business_hours(None, created, closed),
                "censored": False,
            })
        return rows
    raise ValueError(f"unknown compare metric {metric!r}: metric must be one of "
                     f"{sorted(COMPARE_METRICS)}")


def compare_metric(court, data, window, *, metric: str, split,
                   bucket: str = "week", annotations: list[dict] | None = None,
                   tenant: str = "", stamp: object = None,
                   calendar_id: str = "none", selected_id: str | None = None,
                   trials: int = TRIALS) -> dict:
    """``GET /api/trend/compare``: the item-level before/after either side.

    Items created in the bucket containing the snapshot are excluded from **both**
    sides (the in-progress bucket is still accumulating, so it cannot be an
    "after" sample), exactly as it is excluded from the trend headline.
    """
    if metric not in COMPARE_METRICS:
        raise ValueError(
            f"metric must be one of {sorted(COMPARE_METRICS)}"
        )
    if bucket not in trend.BUCKETS:
        raise ValueError(f"bucket must be one of {sorted(trend.BUCKETS)}")
    spec = COMPARE_METRICS[metric]
    moment = _parse_split(split)
    bounds = trend.bucket_bounds(court, data, window, bucket)
    in_progress = {index for index, row in enumerate(bounds) if row["in_progress"]}
    split_index = _bucket_index(bounds, moment)

    before: list[float] = []
    after: list[float] = []
    buckets_before: set[int] = set()
    buckets_after: set[int] = set()
    censored_before = censored_after = 0
    excluded_in_progress = 0
    for row in _items(court, data, window, metric):
        index = _bucket_index(bounds, row["start"])
        if index is None or index in in_progress:
            excluded_in_progress += 1
            continue
        if row["start"] < moment:
            if row["censored"]:
                censored_before += 1
                continue
            before.append(row["hours"])
            buckets_before.add(index)
        else:
            if row["censored"]:
                censored_after += 1
                continue
            after.append(row["hours"])
            buckets_after.add(index)

    # Two changes in one bucket cannot be separated (prd/61 edge case).
    selected = next(
        (
            entry for entry in annotations or ()
            if entry.get("id") == selected_id
        ),
        None,
    )
    if selected is None:
        selected = next(
            (
                entry for entry in annotations or ()
                if entry.get("date") == moment.date().isoformat()
                and entry.get("kind") in annotations_catalogue_ids()
            ),
            None,
        )
    selected_id = selected.get("id") if selected else selected_id
    confounder = None
    if split_index is not None:
        for entry in annotations or ():
            if entry.get("id") == selected_id:
                continue
            if entry.get("kind") not in annotations_catalogue_ids():
                continue
            try:
                other = _bucket_index(bounds, _parse_split(entry.get("date")))
            except ValueError:
                continue
            if other is not None and abs(other - split_index) <= 1:
                confounder = (
                    f"{entry.get('label') or entry.get('kind')} is in the same "
                    "week as this split"
                )
                break

    seed = seed_for(tenant, stamp, calendar_id, metric, moment.isoformat(), bucket)
    payload = compare(
        before, after,
        higher_is_better=spec["higher_is_better"],
        seed=seed,
        buckets_before=len(buckets_before),
        buckets_after=len(buckets_after),
        confounder=confounder,
        censored_before=censored_before,
        censored_after=censored_after,
        metric=metric,
        unit=spec["unit"],
        label=spec["label"],
        basis=spec["basis"],
        trials=trials,
    )
    payload.update({
        "split": moment.isoformat() + "Z" if "T" in moment.isoformat() else moment.isoformat(),
        "annotation": (
            {
                "id": selected.get("id"),
                "date": selected.get("date"),
                "label": selected.get("label"),
                "note": selected.get("note"),
            }
            if selected else None
        ),
        "excluded_in_progress": excluded_in_progress,
        "note_interval": NOTE_INTERVAL,
    })
    # Censoring is disclosed once, below the table, from the structured counts
    # (``censored_before``/``censored_after``/``censored``). ``note`` stays the
    # verdict sentence so the panel does not print the disclosure twice.
    return payload


def annotations_catalogue_ids() -> tuple[str, ...]:
    """The annotation kinds (imported lazily to keep this module store-free)."""
    from analysis import annotations as annotation_view
    return annotation_view.kinds()

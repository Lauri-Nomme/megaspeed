"""Benchmark contrast (prd/57): one per-item duration next to its cohort median.

The product's numbers were accurate but not decisive: "in review 123 h" says
nothing until the reader knows the median is 30 h. This module is the one
helper behind every such contrast — ``123 h (median 30 h · 4.1×)`` — so the
multiple is computed the same way on every surface and can never drift.

The rule (``docs/concepts.md`` §6):

* the **cohort** is the population the metric already uses — completed stories
  for a cycle or a segment, merged PRs for a review phase, the window cohort
  for waits, every reviewer answer for responsiveness;
* the **median** is that cohort's median, in the same business hours and on the
  same calendar as the value;
* the **multiple** is ``value ÷ median``;
* a cohort smaller than :data:`BENCHMARK_MIN_N` **suppresses** the contrast
  (with the reason in ``note``) instead of drawing a conclusion from a handful
  of items.

Nothing here reads the store: the cohort builders that own a definition live
next to it (``analysis.jira`` for story segments/cycles, ``analysis.ballcourt``
for PR phases/waits/responses) and all of them end in :func:`contrast`.
"""

from __future__ import annotations

#: prd/57 §3: a cohort below this many items is not a benchmark, it is noise.
BENCHMARK_MIN_N = 10


def median(values: list[float]) -> float | None:
    """The cohort median, unrounded (callers round for display)."""
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def stats(values, *, basis: str, min_n: int = BENCHMARK_MIN_N) -> dict:
    """The cohort half of a contrast: its median, its size and its basis.

    A payload that shows a list of per-item values carries this block once
    (e.g. the delivery list's wait benchmark); each item then calls
    :func:`contrast` with the same cohort to get its own multiple.
    """
    cohort = [float(value) for value in values if value is not None]
    med = median(cohort)
    available = med is not None and med > 0 and len(cohort) >= min_n
    return {
        "available": available,
        "median": round(med, 2) if med is not None else None,
        "n": len(cohort),
        "min_n": min_n,
        "basis": basis,
        "note": (
            None if available else
            (
                f"cohort too small (N={len(cohort)} < {min_n}) — no benchmark shown"
                if len(cohort) < min_n and med is not None else
                "no measured value in the cohort, so there is no median"
            )
        ),
    }


def contrast(value, values, *, basis: str, min_n: int = BENCHMARK_MIN_N,
             item_n: int | None = None) -> dict:
    """One duration vs its cohort: ``median`` + ``multiple`` (or a stated reason).

    ``value`` is the per-item duration in the metric's own hours; ``values`` is
    the cohort the metric already reports (the same clock and calendar). The
    result is *always* shaped the same way, so a view can render or suppress it
    without branching, and the ``basis``/``n`` travel with it (prd/57 §3: the
    multiple is labelled with what it is a multiple of).

    ``item_n`` is for a value that is itself a median (a person's response
    time): a median over three samples is not a benchmark either, so it is
    suppressed on the same threshold.
    """
    block = stats(values, basis=basis, min_n=min_n)
    result = dict(block)
    result.update({
        "value": round(float(value), 2) if value is not None else None,
        "multiple": None,
    })
    if value is None:
        result["note"] = "no measured value to compare"
        return result
    if item_n is not None and item_n < min_n:
        result["note"] = (
            f"this value is a median of {item_n} sample"
            f"{'' if item_n == 1 else 's'} (N < {min_n}) — no benchmark shown"
        )
        return result
    if not result["available"]:
        # keep the stated reason (small cohort, zero median, no cohort)
        return result
    result["multiple"] = round(float(value) / result["median"], 2)
    return result

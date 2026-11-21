"""Analysis over the pseudonymised event log (prd/05-analysis.md).

Read-only, and it only ever sees what the store already restricts to: keys,
pseudonyms, PR ids, commit hashes, timestamps and closed attribute labels.

    calendar   — business hours: windows, holidays, vacations (prd/19)
    ballcourt  — reconstruct "who has the ball" for every PR, as intervals
    blocking   — weighted directed handover graph from those intervals
    queues     — per-person WIP / throughput series (Little's law)
    delivery   — the triage list ("what needs attention?")
    rules      — metrics + ranked, evidence-backed "change this" suggestions
    trend      — the same metrics, one bucket at a time (prd/50)
    annotations — dated, closed-vocabulary team changes in the store (prd/61)
    before_after — item-level comparison either side of a change (prd/61)
    stability  — Theil–Sen WIP/throughput slopes + Little's Law (prd/61)

Duration assumption: **business hours** when a calendar is attached (the server
loads ``config/calendar.default.yaml`` plus ``data/<tenant>/calendar.yaml``),
**wall-clock hours** when none is. Every response says which mode answered
(``hours_mode``), so a weekend-heavy wait can never be read as working time.
"""

from .ballcourt import (
    ASSUMPTION,
    BallCourt,
    Interval,
    blocking_graph,
    delivery_list,
    load,
    queue_series,
)
from .calendar import Calendar, CalendarError, business_hours

__all__ = [
    "ASSUMPTION",
    "BallCourt",
    "Calendar",
    "CalendarError",
    "Interval",
    "blocking_graph",
    "business_hours",
    "delivery_list",
    "load",
    "queue_series",
]

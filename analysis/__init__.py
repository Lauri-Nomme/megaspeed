"""Analysis over the pseudonymised event log (prd/05-analysis.md).

Read-only, and it only ever sees what the store already restricts to: keys,
pseudonyms, PR ids, commit hashes, timestamps and closed attribute labels.

    ballcourt  — reconstruct "who has the ball" for every PR, as intervals
    blocking   — weighted directed handover graph from those intervals
    queues     — per-person WIP / throughput series (Little's law)
    delivery   — the triage list ("what needs attention?")

Duration assumption for this slice: **wall-clock hours**. The per-person
working-hours calendar (prd/03) does not exist yet, so every "hours waited" is
elapsed time, weekends and nights included. Every response says so.
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

__all__ = [
    "ASSUMPTION",
    "BallCourt",
    "Interval",
    "blocking_graph",
    "delivery_list",
    "load",
    "queue_series",
]

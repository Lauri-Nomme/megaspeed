"""Wall-clock stamps for operator-facing lines.

A scrape is an hours-long unattended job: a line without a time cannot be lined
up with the model server's log, a container restart or another scrape on the
same box. Every progress/trace line therefore starts with an ISO-8601 UTC
stamp::

    [2026-11-22T07:12:03.421Z] [scrape] 02:15 elapsed · …

This module is deliberately stdlib-only: it is imported by the airgapped scrape
artifact as well as the import side.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

#: Injectable for tests: ``stamp(clock=lambda: datetime(2026, 1, 2, tzinfo=utc))``.
Clock = Callable[[], datetime]


def now() -> datetime:
    """Current UTC time, aware, microsecond precision."""
    return datetime.now(timezone.utc)


def iso(clock: Clock | None = None) -> str:
    """``2026-11-22T07:12:03.421Z`` -- millisecond UTC, sortable as text."""
    moment = (clock or now)()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def prefixed(text: str, clock: Clock | None = None) -> str:
    """``text`` with one ``[stamp]`` prefix; a line is never double-stamped."""
    return f"[{iso(clock)}] {text}"


__all__ = ["Clock", "iso", "now", "prefixed"]

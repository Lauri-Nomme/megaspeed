"""Timestamp helpers shared by both sides of the airgap.

Kept in a module that imports nothing but the standard library: the scrape side
(``ingest.scrape``) must be importable without DuckDB, so it cannot borrow these
from :mod:`ingest.store` -- :mod:`ingest.store` re-exports them for callers that
still import them from there.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Now, as a naive UTC datetime (the store's timestamp convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: datetime | int | float | str) -> datetime:
    """Accept epoch-ms, epoch-s, ISO-8601 or datetime; return naive UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # Bitbucket DC uses epoch milliseconds.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("empty timestamp")
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso(ts: datetime | None) -> str | None:
    """Naive UTC datetime -> ``2026-11-15T17:46:40Z`` (what the API emits)."""
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_precise(ts: datetime | None) -> str | None:
    """Like :func:`iso` but keeps microseconds.

    Used for a run's ``as_of`` so two runs in the same second still order
    correctly in ``ingest_runs`` (provenance is queried by ``as_of DESC``).
    """
    if ts is None:
        return None
    return ts.replace(tzinfo=timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

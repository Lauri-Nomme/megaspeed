"""Periodic progress reporting for long scrapes (prd/14).

Scraping a huge project takes minutes, so the operator needs to see that the run
is alive -- without a line per PR. This reporter emits **one** line to **stderr**
at most once per ``interval`` seconds (default 30):

    [scrape] 02:15 elapsed · 3,214 PRs seen · 412 kept · 2,802 skipped · 1,041 req · 4,120 events · 23.8 PR/s · 7.7 req/s

It never touches stdout or the bundle, so a piped ``--json`` report stays clean.
The gate is a cheap ``time.monotonic()`` comparison at natural loop boundaries
(no thread to start or shut down), and because it is *interval*-gated the
cadence is time-based, not PR-based: a fast project prints nothing at all.

``--quiet`` (or ``--progress-interval 0``) disables it; the final summary is
printed by the CLI as before, never by this module.
"""

from __future__ import annotations

import sys
import time
from typing import Callable, TextIO

#: Seconds between progress lines. Overridable by ``--progress-interval`` /
#: ``MEGASPEED_PROGRESS_INTERVAL``; ``0`` disables reporting.
DEFAULT_INTERVAL = 30.0


def format_elapsed(seconds: float) -> str:
    """``135`` -> ``02:15``; past an hour, ``1:02:15``."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class ProgressReporter:
    """Interval-gated progress lines on stderr."""

    def __init__(
        self,
        stream: TextIO | None = None,
        interval: float = DEFAULT_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.stream = sys.stderr if stream is None else stream
        self.interval = float(interval)
        self.clock = clock
        self.started = clock()
        self.last = self.started
        self.lines = 0

    @property
    def enabled(self) -> bool:
        return self.interval > 0

    def line(
        self,
        *,
        prs_seen: int,
        prs_kept: int,
        prs_skipped: int,
        requests: int,
        events: int,
        elapsed: float | None = None,
        unit: str = "PR",
    ) -> str:
        """One progress line. Pure formatting, so it is testable on its own.

        ``unit`` names the walked rows: ``PR`` for the Bitbucket half, ``issue``
        for the Jira half (the counters are shared, the noun is not).
        """
        seconds = (self.clock() - self.started) if elapsed is None else elapsed
        plural = unit if unit.endswith("s") else unit + "s"

        def rate(count: int) -> str:
            return f"{count / seconds:.1f}" if seconds > 0 else "—"

        return (
            f"[scrape] {format_elapsed(seconds)} elapsed"
            f" · {prs_seen:,} {plural} seen"
            f" · {prs_kept:,} kept"
            f" · {prs_skipped:,} skipped"
            f" · {requests:,} req"
            f" · {events:,} events"
            f" · {rate(prs_seen)} {unit}/s"
            f" · {rate(requests)} req/s"
        )

    def tick(self, *, unit: str = "PR", **counts: int) -> bool:
        """Print a line if the interval elapsed. Returns True when it printed.

        Callers invoke this at loop boundaries; the interval gate means a fast
        scrape prints nothing and a slow one prints at most one line per
        interval.
        """
        if not self.enabled:
            return False
        now = self.clock()
        if now - self.last < self.interval:
            return False
        self.last = now
        return self.emit(self.line(unit=unit, **counts))

    def emit(self, text: str) -> bool:
        self.stream.write(text + "\n")
        self.stream.flush()
        self.lines += 1
        return True

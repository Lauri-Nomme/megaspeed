"""Prototype: difference-array WIP sampling for ballcourt.queue_series.

Verifies the result is identical to the current O(intervals x buckets) loop and
measures the speed-up on a large store.
"""
import bisect
import statistics
import sys
import time
from datetime import timedelta

sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed")
from pathlib import Path

from ingest import store as pstore
from analysis import ballcourt as analyse, calendar as workcal

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/perf/stores/big40.duckdb"
BUCKET = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

cal = workcal.load(tenant="x", tenants_dir=Path("/tmp/opencode/perf/tenants"),
                   config_path=Path("/vokk/home/lauri/dev/megaspeed/config/calendar.default.yaml"))
con = pstore.connect(DB, read_only=True)
court = analyse.load(con, calendar=cal)
window = analyse.window_from_events(court, None)


def wip_current(court, window, bucket_hours, level="person"):
    start, end = window
    span_hours = max(1.0, (end - start).total_seconds() / 3600.0)
    buckets = max(1, int(span_hours // bucket_hours) + 1)
    streams = {}
    measured = [i for i in court.intervals if i.measured and i.start < end and i.end > start]
    for interval in measured:
        people = interval.holders if level == "person" else ((interval.waited_on,) if interval.waited_on else ())
        for person in people:
            if not person:
                continue
            entry = streams.setdefault(person, [0] * buckets)
            for b in range(buckets):
                at = start + timedelta(hours=(b + 0.5) * bucket_hours)
                if interval.start <= at < interval.end:
                    entry[b] += 1
    return streams


def wip_diff(court, window, bucket_hours, level="person"):
    start, end = window
    span_hours = max(1.0, (end - start).total_seconds() / 3600.0)
    buckets = max(1, int(span_hours // bucket_hours) + 1)
    midpoints = [start + timedelta(hours=(b + 0.5) * bucket_hours) for b in range(buckets)]
    diff: dict[str, list[int]] = {}
    measured = [i for i in court.intervals if i.measured and i.start < end and i.end > start]
    for interval in measured:
        people = interval.holders if level == "person" else ((interval.waited_on,) if interval.waited_on else ())
        if not people:
            continue
        lo = max(0, bisect.bisect_left(midpoints, interval.start))
        hi = min(buckets, bisect.bisect_left(midpoints, interval.end))
        if lo >= hi:
            continue
        for person in people:
            if not person:
                continue
            d = diff.setdefault(person, [0] * (buckets + 1))
            d[lo] += 1
            d[hi] -= 1
    out = {}
    for person, d in diff.items():
        running = 0
        row = []
        for b in range(buckets):
            running += d[b]
            row.append(running)
        out[person] = row
    return out


def timed(fn, n=3):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


for level in ("person", "queue"):
    a = wip_current(court, window, BUCKET, level)
    b = wip_diff(court, window, BUCKET, level)
    same = a.keys() == b.keys() and all(a[k] == b[k] for k in a)
    print(f"level={level} bucket={BUCKET}h identical={same} streams={len(a)} "
          f"buckets={len(next(iter(a.values()), []))}")
    print(f"  current {timed(lambda: wip_current(court, window, BUCKET, level)):8.0f} ms   "
          f"diff-array {timed(lambda: wip_diff(court, window, BUCKET, level)):8.0f} ms")

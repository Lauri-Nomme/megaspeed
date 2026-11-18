"""Prototype: full queue_series with difference-array WIP sampling.

Compares the complete payload against the shipped function (byte-for-byte) and
times both at the default bucket width and at 1 hour.
"""
import bisect
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import timedelta

sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed")
from pathlib import Path

from ingest import store as pstore
from analysis import ballcourt as analyse, calendar as workcal
from analysis.ballcourt import CALENDAR, CI, REVIEW, iso, _queue_of

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/perf/stores/big40.duckdb"
cal = workcal.load(tenant="x", tenants_dir=Path("/tmp/opencode/perf/tenants"),
                   config_path=Path("/vokk/home/lauri/dev/megaspeed/config/calendar.default.yaml"))
con = pstore.connect(DB, read_only=True)
court = analyse.load(con, calendar=cal)


def queue_series_fast(bc, level="person", window=None, bucket_hours=None):
    """Same as ballcourt.queue_series, with a difference array for WIP."""
    start, end = window or (bc.intervals[0].start if bc.intervals else bc.snapshot, bc.snapshot)
    span_hours = max(1.0, (end - start).total_seconds() / 3600.0)
    service_hours = max(1e-9, bc.business_hours(None, start, end))
    if bucket_hours is None:
        bucket_hours = 24.0 if span_hours > 24 * 12 else max(1.0, round(span_hours / 60.0, 1))
    buckets = max(1, int(span_hours // bucket_hours) + 1)
    midpoints = [start + timedelta(hours=(b + 0.5) * bucket_hours) for b in range(buckets)]
    prs = {s.pr["object_id"]: s.pr for s in bc.prs}

    def key_for(person, pr):
        if level == "person":
            return person
        return _queue_of(bc, person, pr, "ball")

    streams = {}

    def stream(name):
        if name in (REVIEW, CI, CALENDAR):
            role = "synthetic"
        elif name in bc.sentinels:
            role = "sentinel"
        else:
            role = "person"
        return streams.setdefault(name, {
            "id": name, "role": role, "people": [],
            "wip": [0] * buckets, "arrivals": [0] * buckets, "completions": [0] * buckets,
            "hours": 0.0, "count": 0, "prs": set(), "_diff": [0] * (buckets + 1),
        })

    measured = [i for i in bc.intervals if i.measured and i.start < end and i.end > start]
    for interval in measured:
        pr = prs.get(interval.object_id) or {}
        if level == "person":
            people = interval.holders
        else:
            people = (interval.waited_on,) if interval.waited_on else ()
        lo = max(0, bisect.bisect_left(midpoints, interval.start))
        hi = min(buckets, bisect.bisect_left(midpoints, interval.end))
        for person in people:
            if not person:
                continue
            name = key_for(person, pr)
            entry = stream(name)
            if level == "queue" and person not in (REVIEW, CI, CALENDAR):
                entry["people"].append(person)
            entry["hours"] += interval.hours
            entry["count"] += 1
            entry["prs"].add(interval.object_id)
            if not interval.open:
                entry.setdefault("served", set()).add(interval.object_id)
            for stamp, series in ((interval.start, entry["arrivals"]), (interval.end, entry["completions"])):
                b = int((stamp - start).total_seconds() // (bucket_hours * 3600))
                if 0 <= b < buckets:
                    series[b] += 1
            if lo < hi:
                entry["_diff"][lo] += 1
                entry["_diff"][hi] -= 1

    for entry in streams.values():
        running = 0
        for b in range(buckets):
            running += entry["_diff"][b]
            entry["wip"][b] = running
        entry.pop("_diff", None)

    days = span_hours / 24.0
    all_hours = [i.hours for i in measured]
    baseline = sorted(all_hours)[len(all_hours) // 2] if all_hours else 0.0
    out = []
    for entry in streams.values():
        items = len(entry["prs"])
        mean_hours = entry["hours"] / items if items else 0.0
        mean_wip = sum(entry["wip"]) / max(1, len(entry["wip"]))
        queue_length = entry["hours"] / service_hours
        served = len(entry.get("served") or ())
        per_day = served / days if days else 0.0
        if mean_hours > baseline * 1.25 and queue_length >= 1.0:
            diagnosis = "overloaded"
        elif mean_hours > baseline * 1.25:
            diagnosis = "slow"
        elif queue_length >= 2.0:
            diagnosis = "overloaded"
        else:
            diagnosis = "healthy"
        out.append({
            "id": entry["id"], "role": entry["role"], "people": sorted(set(entry["people"])),
            "wip": entry["wip"], "arrivals": entry["arrivals"], "completions": entry["completions"],
            "summary": {
                "mean_wip": round(mean_wip, 2), "peak_wip": max(entry["wip"] or [0]),
                "mean_queue_length": round(queue_length, 2), "items": items,
                "intervals": entry["count"], "hours": round(entry["hours"], 1),
                "mean_wait_hours": round(mean_hours, 1), "throughput_per_day": round(per_day, 2),
                "diagnosis": diagnosis, "prs": items,
            },
        })
    out.sort(key=lambda s: (-s["summary"]["mean_wait_hours"], s["id"]))
    return {
        "ok": True, "hours_mode": bc.hours_mode, "assumption": bc.assumption,
        **bc.calendar_warning(),
        "diagnosis_note": "heuristic vs. the median wait in this window; queue length divides "
                          "wait-hours by the hours the queue could be served in",
        "level": level,
        "window": {"start": iso(start), "end": iso(end), "days": round(days, 1),
                   "service_hours": round(service_hours, 1)},
        "bucket_hours": bucket_hours, "buckets": buckets,
        "baseline_mean_wait_hours": round(baseline, 1),
        "streams": out,
        "totals": {"items": sum(s["summary"]["items"] for s in out),
                   "hours": round(sum(s["summary"]["hours"] for s in out), 1),
                   "streams": len(out)},
    }


def t(fn, n=3):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter(); fn(); ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


for level in ("person", "queue"):
    for bh in (None, 1.0):
        a = analyse.queue_series(court, level, None, bh)
        b = queue_series_fast(court, level, None, bh)
        same = json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
        print(f"level={level:<6} bucket={bh or 'default'}h identical={same} buckets={a['buckets']} "
              f"| current {t(lambda: analyse.queue_series(court, level, None, bh)):8.0f} ms "
              f"| fast {t(lambda: queue_series_fast(court, level, None, bh)):7.1f} ms")

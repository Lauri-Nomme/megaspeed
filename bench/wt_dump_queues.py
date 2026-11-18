"""Dump queue_series payloads so two checkouts can be compared byte for byte.

Usage: wt_dump_queues.py <repo> <db> <out.json>
"""
import json
import sys
from pathlib import Path

REPO, DB, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, REPO)

from ingest import store as pstore
from analysis import ballcourt as analyse
from analysis import calendar as workcal

cal = workcal.load(
    tenant="bench", tenants_dir=Path("/tmp/opencode/perf/tenants"),
    config_path=Path(REPO) / "config" / "calendar.default.yaml",
)
con = pstore.connect(DB, read_only=True)
court = analyse.load(con, calendar=cal)

out = {}
for level in ("person", "queue"):
    for bucket_hours in (None, 1, 6, 24, 72):
        key = f"{level}:{bucket_hours}"
        out[key] = analyse.queue_series(court, level, None, bucket_hours)
Path(OUT).write_text(json.dumps(out, sort_keys=True, default=str), encoding="utf-8")
print(f"{OUT}: {len(out)} payloads, {len(json.dumps(out))} bytes")

"""`/api/ticket` cold vs warm-key latency + insights/sprints after a ticket.

Usage: bench_ticket_cache.py <port> <tenant> [n_keys]

Measures, against an already-running server (fresh process, no caches):
  * the first ticket request (builds the analysis bundle),
  * each of the next N distinct ticket keys (the cold-path recomputation),
  * `/api/insights?level=team` and `/api/sprints` after the ticket requests.
"""
import json
import statistics
import sys
import time
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8791"
TENANT = sys.argv[2] if len(sys.argv) > 2 else "del"
N = int(sys.argv[3]) if len(sys.argv) > 3 else 15
BASE = f"http://127.0.0.1:{PORT}"


def fetch(path):
    url = f"{BASE}{path}{'&' if '?' in path else '?'}tenant={TENANT}"
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=600) as r:
        body = r.read()
        code = r.status
    return (time.perf_counter() - t0) * 1000, body, code


def keys(n):
    """`n` real ticket keys from the store's own object table."""
    _, body, _ = fetch("/api/objects?type=STORY&limit=1000")
    rows = json.loads(body)
    out = []
    for row in rows:
        for key in row.get("ticket_keys") or []:
            if key not in out:
                out.append(key)
    return out[:n]


tickets = keys(N + 1)
first, body, code = fetch(f"/api/ticket/{tickets[0]}")
print(f"tenant={TENANT} keys={len(tickets)} status={code} bytes={len(body)}")
print(f"ticket first key  ({tickets[0]:<9}) {first:8.1f} ms")

later = []
for key in tickets[1:]:
    ms, payload, code = fetch(f"/api/ticket/{key}")
    later.append(ms)
    print(f"ticket later key  ({key:<9}) {ms:8.1f} ms  {len(payload):>7} B  {code}")
print(f"ticket later keys median {statistics.median(later):8.1f} ms   "
      f"max {max(later):8.1f} ms   n={len(later)}")

for path in ("/api/insights?level=team", "/api/sprints", "/api/insights?level=team"):
    ms, payload, code = fetch(path)
    print(f"{path:<28} {ms:8.1f} ms  {len(payload):>9} B  {code}")

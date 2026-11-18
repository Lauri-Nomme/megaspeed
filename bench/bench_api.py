"""Endpoint latency + payload size against a running megaspeed server.

Usage: bench_api.py <port> <tenant> [repeat]
Measures cold (first, no analysis cache) and warm latency, and response bytes.
"""
import json
import statistics
import sys
import time
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8789"
TENANT = sys.argv[2] if len(sys.argv) > 2 else "big10"
REPEAT = int(sys.argv[3]) if len(sys.argv) > 3 else 3
BASE = f"http://127.0.0.1:{PORT}"

PATHS = [
    "/api/health",
    "/api/events?limit=100",
    "/api/events?limit=100&offset=0&ticket=DEL-167",
    "/api/events/counts?granularity=day",
    "/api/objects?type=PR&limit=100",
    "/api/blocking",
    "/api/queues?level=person",
    "/api/queues?level=queue",
    "/api/queues?level=person&days=3650",
    "/api/delivery",
    "/api/insights?level=team",
    "/api/insights?level=person",
    "/api/sprints",
    "/api/simulate",
    "/api/ticket/DEL-167",
]


def fetch(path):
    url = f"{BASE}{path}{'&' if '?' in path else '?'}tenant={TENANT}"
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=600) as r:
        body = r.read()
        code = r.status
    return (time.perf_counter() - t0) * 1000, len(body), code


print(f"tenant={TENANT}  (cold = first request, which also builds the analysis cache)")
print(f"{'endpoint':<52} {'cold ms':>9} {'warm ms':>9} {'bytes':>10}")
for path in PATHS:
    try:
        cold, size, code = fetch(path)
        times = [fetch(path)[0] for _ in range(REPEAT - 1)]
        warm = statistics.median(times) if times else cold
        print(f"{path:<52} {cold:>9.0f} {warm:>9.0f} {size:>10}  {code}")
    except Exception as exc:
        print(f"{path:<52} ERROR {exc}")

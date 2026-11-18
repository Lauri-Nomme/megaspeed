"""Measure the /api/eval and /api/screenshot harness cost (loopback + token)."""
import json
import sys
import time
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8789"
TOKEN = open("/tmp/opencode/perf/token").read().strip()
BASE = f"http://127.0.0.1:{PORT}"


def post(path, body, timeout=300):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-megaspeed-token": TOKEN},
        method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    return (time.perf_counter() - t0) * 1000, payload


print(f"{'call':<46} {'ms':>9}  result")
for label, body in [
    ("eval: 1+1", {"code": "1+1"}),
    ("eval: JSON.stringify(state.events).length",
     {"code": "JSON.stringify(window.__megaspeed.state.events).length"}),
    ("eval: read 40k events' ts", {"code": "window.__megaspeed.state.events.map(e=>e.ts).length"}),
    ("screenshot: viewport (.app)", {"selector": ".app", "out": "shot-app.png"}),
    ("screenshot: timeline canvas", {"selector": "#timeline-canvas", "out": "shot-tl.png"}),
]:
    try:
        ms, res = post("/api/screenshot" if label.startswith("screenshot") else "/api/eval", body)
        data = res.get("data")
        size = len(data) if isinstance(data, str) else len(json.dumps(data or ""))
        print(f"{label:<46} {ms:>9.0f}  ok={res.get('ok')} bytes={size} {str(res.get('error') or '')[:60]}")
    except Exception as exc:
        print(f"{label:<46} ERROR {exc}")

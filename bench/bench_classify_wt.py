"""Measure OllamaClassifier throughput + concurrency (worktree, item 7).

Starts a local stub /api/chat that answers after a fixed delay (default 50 ms,
standing in for a CPU 30B MoE's seconds) and classifies K comments through the
real client: serial (workers=1, the old behaviour) vs the bounded pool.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WT = sys.argv[3] if len(sys.argv) > 3 else "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape"
sys.path.insert(0, WT)
from ingest import classify

DELAY = float(sys.argv[1]) if len(sys.argv) > 1 else 0.05
K = int(sys.argv[2]) if len(sys.argv) > 2 else 100
BATCH = 8  # comments per PR -- the unit the reducer batches

answer = json.dumps({"message": {"content": json.dumps({
    "comment_type": "suggestion", "thread_intent": "acted",
    "question": False, "answered": False, "phase": "review"})}}).encode()

state = {"open": 0, "max": 0, "requests": 0, "conns": 0}
lock = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        with lock:
            state["conns"] += 1

    def do_POST(self):
        with lock:
            state["open"] += 1
            state["max"] = max(state["max"], state["open"])
            state["requests"] += 1
        self.rfile.read(int(self.headers.get("content-length") or 0))
        time.sleep(DELAY)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)
        with lock:
            state["open"] -= 1

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{port}"

texts = [f"comment {i}: could you rename this variable?" for i in range(K)]


def run(workers: int) -> tuple[float, dict]:
    clf = classify.OllamaClassifier(url, "stub", timeout=30.0, workers=workers)
    with lock:
        state.update(open=0, max=0, requests=0, conns=0)
    t0 = time.perf_counter()
    for start in range(0, K, BATCH):
        batch = [(text, classify.KIND_COMMENT) for text in texts[start:start + BATCH]]
        clf.classify_many(batch)
    elapsed = time.perf_counter() - t0
    with lock:
        snapshot = dict(state)
    return elapsed, snapshot


for workers in (1, 2, 4, 8):
    elapsed, snapshot = run(workers)
    print(f"workers={workers} K={K} delay={DELAY*1000:.0f}ms -> {elapsed:5.2f}s "
          f"({elapsed/K*1000:5.1f} ms/comment) max in-flight={snapshot['max']} "
          f"requests={snapshot['requests']} conns={snapshot['conns']} "
          f"speedup={K*DELAY/elapsed:4.1f}x")
print(f"extrapolated: 1000 PRs x 8 comments = 8000 calls at {DELAY*1000:.0f}ms -> "
      f"serial {8000*DELAY/3600:.1f} h")

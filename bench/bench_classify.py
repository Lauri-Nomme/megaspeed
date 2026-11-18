"""Measure OllamaClassifier throughput: sequential calls, no connection reuse.

Starts a local stub /api/chat that answers after a fixed delay (default 50 ms,
standing in for a CPU 30B MoE's seconds) and classifies K comments through the
real client, so total time == K x latency if there is no parallelism.
"""
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed")
from ingest import classify

DELAY = float(sys.argv[1]) if len(sys.argv) > 1 else 0.05
K = int(sys.argv[2]) if len(sys.argv) > 2 else 100

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

clf = classify.OllamaClassifier(f"http://127.0.0.1:{port}", "stub", timeout=30.0)
texts = [f"comment {i}: could you rename this variable?" for i in range(K)]
t0 = time.perf_counter()
for text in texts:
    clf.classify(text, classify.KIND_COMMENT)
elapsed = time.perf_counter() - t0
print(f"K={K} delay={DELAY*1000:.0f}ms -> {elapsed:.2f}s "
      f"({elapsed/K*1000:.1f} ms/comment, ideal {DELAY*1000:.0f} ms)")
print(f"max in-flight requests: {state['max']}  TCP connections opened: {state['conns']} "
      f"requests: {state['requests']}")
print(f"extrapolated: 1000 PRs x 8 comments = 8000 calls -> "
      f"{8000*elapsed/K/3600:.1f} h at this latency")

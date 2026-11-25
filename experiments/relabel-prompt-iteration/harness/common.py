"""Shared harness pieces: request shape (copied from the v3 trace), cache, I/O.

Nothing here writes to the repo. The cache lives next to this file.
"""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import hashlib
import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ITER = os.environ.get("RELABEL_WORK", os.path.dirname(HERE))  # eval/gold live outside the repo
CACHE = os.path.join(ITER, "harness-cache.jsonl")
TRACE = ROOT + r"\export\hans-v3.trace.jsonl"
MANDATORY = ROOT + r"\relabel\relabel-mandatory-threads.txt"
URL = os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/chat"
MODEL = "qwen3:30b-a3b"

CLASSES = ["blocker", "nit", "optimization", "out_of_scope", "question", "suggestion"]
INTENTS = ["acted", "rejected", "unclear", "withdrawn"]
FIELDS = ["comment_type", "thread_intent", "question", "answered", "phase"]

# Fixed schema: identical to the v3 trace `request.format` for kind=comment.
SCHEMA = {
    "additionalProperties": False,
    "properties": {
        "answered": {"type": "boolean"},
        "comment_type": {"enum": CLASSES, "type": "string"},
        "phase": {"enum": ["review", "implementation"], "type": "string"},
        "question": {"type": "boolean"},
        "thread_intent": {"enum": INTENTS, "type": "string"},
    },
    "required": ["comment_type", "thread_intent", "question", "answered", "phase"],
    "type": "object",
}


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def user_message(text: str) -> str:
    return f"kind=comment\n---\n{text}"


def request_body(system: str, text: str) -> dict:
    return {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message(text)},
        ],
        "stream": False,
        "think": False,
        "format": SCHEMA,
        "options": {"temperature": 0, "seed": 0, "num_predict": 128},
    }


def cache_key(system: str, text: str, salt: str = "") -> str:
    k = sha(system) + ":" + sha(user_message(text))
    return k + (":" + salt if salt else "")


class Cache:
    """Append-only jsonl cache; last write per key wins; safe across resumes."""

    def __init__(self, path: str = CACHE):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue  # torn last line after a kill
                    self.data[r["key"]] = r

    def get(self, key):
        return self.data.get(key)

    def put(self, rec):
        with self.lock:
            self.data[rec["key"]] = rec
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_prompt(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def mandatory_ids():
    with open(MANDATORY, encoding="utf-8") as f:
        return {l.strip() for l in f if l.strip()}


def lookup(cache: Cache, system: str, rows, salt: str = ""):
    """Predictions for rows [{id,text}] from the cache only: {id: labels}."""
    out = {}
    for r in rows:
        rec = cache.get(cache_key(system, r["text"], salt))
        if rec and rec.get("labels"):
            out[r["id"]] = rec["labels"]
    return out

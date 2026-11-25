"""Write prompts/v3.txt (baseline) and preflight/rows.jsonl (40 openers)."""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from common import HERE, SCHEMA, TRACE, mandatory_ids, request_body, write_jsonl  # noqa: E402
from ingest.classify import system_prompt  # noqa: E402  (read-only import)

base = system_prompt("comment")
mand = mandatory_ids()
recs = {}
sys_mismatch = shape_mismatch = 0
with open(TRACE, encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        if r.get("kind") != "comment" or r["context"].get("role") != "opener":
            continue
        req = r["request"]
        if req["messages"][0]["content"] != base:
            sys_mismatch += 1
        mine = request_body(req["messages"][0]["content"], r["text"])
        if mine != req:
            shape_mismatch += 1
        recs[r["context"]["thread_id"]] = r  # last record per thread
print("openers", len(recs), "system!=classify.system_prompt:", sys_mismatch, "request!=harness shape:", shape_mismatch)

os.makedirs(os.path.join(HERE, "prompts"), exist_ok=True)
with open(os.path.join(HERE, "prompts", "v3.txt"), "w", encoding="utf-8", newline="") as f:
    f.write(base)

cand = [t for t, r in recs.items() if t not in mand and r.get("ok") and not r.get("fallback")]
cand.sort(key=lambda t: hashlib.sha256(("pre" + t).encode()).hexdigest())
pick = cand[:40]
os.makedirs(os.path.join(HERE, "preflight"), exist_ok=True)
write_jsonl(os.path.join(HERE, "preflight", "rows.jsonl"), [{"id": t, "text": recs[t]["text"]} for t in pick])
write_jsonl(os.path.join(HERE, "preflight", "trace_labels.jsonl"), [{"id": t, "labels": recs[t]["labels"]} for t in pick])
print("mandatory", len(mand), "candidates", len(cand), "picked", len(pick))

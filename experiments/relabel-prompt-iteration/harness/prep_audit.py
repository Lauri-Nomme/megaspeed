"""audit_rows.jsonl: {id, text} from the v3 trace for gold_audit ids (secondary check only)."""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HERE, ITER, TRACE, read_jsonl, write_jsonl

want = {r["id"] for r in read_jsonl(os.path.join(ITER, "gold_audit.jsonl"))}
out = {}
with open(TRACE, encoding="utf-8") as f:
    for line in f:
        r = json.loads(line)
        if r.get("kind") == "comment" and r["context"]["thread_id"] in want:
            out[r["context"]["thread_id"]] = r["text"]
write_jsonl(os.path.join(HERE, "audit_rows.jsonl"), [{"id": k, "text": v} for k, v in out.items()])
print("audit ids", len(want), "found", len(out))

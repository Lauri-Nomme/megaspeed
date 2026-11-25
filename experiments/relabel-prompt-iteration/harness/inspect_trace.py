import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, sys, collections, hashlib
sys.path.insert(0, ROOT)
from ingest.classify import system_prompt

p = ROOT + r"\export\hans-v3.trace.jsonl"
kinds = collections.Counter(); roles = collections.Counter(); keys = collections.Counter()
sysps = collections.Counter(); reqkeys = set(); opts = collections.Counter()
threads = collections.Counter(); ok = collections.Counter(); usr_prefix = collections.Counter()
textfield = collections.Counter()
for line in open(p, encoding="utf-8"):
    r = json.loads(line)
    kinds[r.get("kind")] += 1
    roles[(r.get("kind"), r["context"].get("role"))] += 1
    keys.update(r.keys())
    req = r.get("request") or {}
    reqkeys.update(req.keys())
    if req:
        m = req["messages"]
        sysps[(r.get("kind"), hashlib.sha256(m[0]["content"].encode()).hexdigest()[:12])] += 1
        opts[json.dumps(req.get("options"), sort_keys=True) + f" think={req.get('think')} stream={req.get('stream')}"] += 1
        usr_prefix[m[1]["content"].split("\n", 1)[0]] += 1
    if r.get("kind") == "comment" and r["context"].get("role") == "opener":
        threads[r["context"]["thread_id"]] += 1
    ok[(r.get("ok"), r.get("fallback"), bool(r.get("error")))] += 1
    for k in ("text", "input", "text_digest"):
        if k in r: textfield[k] += 1
print("kinds", kinds); print("roles", roles); print("keys", keys); print("reqkeys", reqkeys)
print("sys", sysps); print("opts", opts); print("usr", usr_prefix.most_common(5)); print("ok", ok)
print("textfields", textfield)
print("opener threads", len(threads), "multi", sum(1 for v in threads.values() if v > 1))
print("system_prompt(comment) sha", hashlib.sha256(system_prompt("comment").encode()).hexdigest()[:12])

import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, sys
sys.stdout.reconfigure(encoding="utf-8")
M = ROOT
W = WORK
def op(p):
    o = {}
    for l in open(p, encoding="utf-8"):
        t = json.loads(l)
        if not t.get("batch") and t["context"]["role"] == "opener": o[t["context"]["thread_id"]] = t["labels"]
    return o
mode = sys.argv[1]
if mode == "B":
    R = [json.loads(l) for l in open(W + r"\v3-audit-B.jsonl", encoding="utf-8")]
else:
    mand = {l.strip() for l in open(M + r"\relabel\relabel-mandatory-threads.txt") if l.strip()}
    R = [r for r in (json.loads(l) for l in open(M + r"\v3-audit.jsonl", encoding="utf-8")) if r["thread_id"] in mand]
v1 = op(M + r"\relabel\hans-relabel-202611222146.tgz.trace.jsonl"); v2 = op(M + r"\export\hans-relabel-v2.trace.jsonl")
lo, hi = int(sys.argv[2]), int(sys.argv[3]); cut = 260
for i, r in enumerate(R[lo:hi], start=lo):
    k = r["thread_id"]; a, b, c = v1[k], v2[k], r["labels"]
    mi = "withdrawn" if a["thread_intent"] == "withdrawn" else b["thread_intent"]
    x = []
    if a["question"] != c["question"]: x.append(f"q={int(a['question'])}>{int(c['question'])}")
    if mi != c["thread_intent"]: x.append(f"int={mi}>{c['thread_intent']}")
    if c["answered"]: x.append("ANS")
    t = " ".join(r["text"].split()); t = t if len(t) <= cut else t[:cut] + f"…[+{len(t)-cut}]"
    print(f"#{i} {r['old']}->{r['new']} (v1 {a['comment_type']}/v2 {b['comment_type']}) {' '.join(x)} | {t}")

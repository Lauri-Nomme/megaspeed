"""Verify the v1->v2 sample against both traces; census of v1->v2 changes. No text printed."""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, tarfile, collections as C
M = ROOT
V1T = M + r"\relabel\hans-relabel-202611222146.tgz.trace.jsonl"
V2T = M + r"\export\hans-relabel-v2.trace.jsonl"
SMP = M + r"\v2-audit-v1v2.jsonl"

def openers(p):
    out, desc = {}, {}
    for l in open(p, encoding="utf-8"):
        t = json.loads(l)
        if t.get("batch"):
            continue
        c = t["context"]
        if c["role"] == "opener":
            out[c["thread_id"]] = t          # last wins
        elif c["role"] == "description":
            desc[f"PR:{c['repo']}#{c['pr_id']}"] = t
    return out, desc

def patch(name):
    P, Q = {}, {}
    with tarfile.open(M + "\\export\\" + name) as tf:
        for l in tf.extractfile("export/labels.jsonl"):
            r = json.loads(l)
            (P if r["target"] == "thread" else Q)[r["object_id"]] = r
    return P, Q

v1, v1d = openers(V1T)
v2, v2d = openers(V2T)
S = [json.loads(l) for l in open(SMP, encoding="utf-8") if l.strip()]
print("v1 openers", len(v1), "v2 openers", len(v2), "both", len(set(v1) & set(v2)),
      "v2 only", len(set(v2) - set(v1)), "v1 only (skipped in v2)", len(set(v1) - set(v2)))
print("v2 label versions", C.Counter(t["label_version"] for t in v2.values()))
print("text same v1/v2", C.Counter(v1[k]["text"] == v2[k]["text"] for k in set(v1) & set(v2)))

chk = C.Counter()
for r in S:
    k = r["thread_id"]
    chk["in v1"] += k in v1; chk["in v2"] += k in v2
    chk["old==v1"] += k in v1 and r["old"] == v1[k]["labels"]["comment_type"]
    chk["new==v2"] += k in v2 and r["new"] == v2[k]["labels"]["comment_type"]
    chk["labels==v2"] += k in v2 and r["labels"] == v2[k]["labels"]
    chk["v1_labels==v1"] += k in v1 and r["v1_labels"] == v1[k]["labels"]
    chk["text==v2"] += k in v2 and r["text"] == v2[k]["text"]
print("sample", len(S), dict(chk))

for name in ["hans-relabel-v2.tgz", "hans-relabel-v2.clean.tgz"]:
    P, Q = patch(name)
    both = [k for k in set(v1) & set(v2) if k in P]
    ch = C.Counter((v1[k]["labels"]["comment_type"], v2[k]["labels"]["comment_type"]) for k in both)
    changed = sum(v for (a, b), v in ch.items() if a != b)
    print(f"\n{name}: thread rows {len(P)}, in both traces {len(both)}, v1->v2 type changed {changed}")
    print("  sample ids in patch", sum(r["thread_id"] in P for r in S))
    print("  patch label == v2 trace type", C.Counter(P[k]["labels"]["comment_type"] == v2[k]["labels"]["comment_type"] for k in both))
    print("  top", [(f"{a}->{b}", v) for (a, b), v in ch.most_common(40) if a != b])
    d1 = C.Counter(v1[k]["labels"]["comment_type"] for k in both)
    d2 = C.Counter(v2[k]["labels"]["comment_type"] for k in both)
    print("  dist v1/v2 (same threads)", {t: (d1[t], d2[t]) for t in sorted(d1 | d2)})
    i1 = C.Counter(v1[k]["labels"]["thread_intent"] for k in both); i2 = C.Counter(v2[k]["labels"]["thread_intent"] for k in both)
    print("  intent v1/v2", {t: (i1[t], i2[t]) for t in sorted(i1 | i2)})
    q = C.Counter((v1[k]["labels"]["question"], v2[k]["labels"]["question"]) for k in both)
    print("  question (v1,v2)", dict(q))
    a = C.Counter((v1[k]["labels"]["answered"], v2[k]["labels"]["answered"]) for k in both)
    print("  answered (v1,v2)", dict(a))
    print("  outcome rows", C.Counter((r["previous"].get("thread_outcome"), r["labels"].get("thread_outcome")) for r in P.values() if r["previous"].get("thread_outcome") != r["labels"].get("thread_outcome")).most_common(6))
    pq = {k: v for k, v in Q.items() if k.startswith("PR:")}
    print("  PR question rows", C.Counter(v.get("action") for v in pq.values()))
pd = C.Counter((v1d[k]["labels"]["question"], v2d[k]["labels"]["question"]) for k in set(v1d) & set(v2d))
print("\ndescriptions question (v1,v2)", dict(pd))

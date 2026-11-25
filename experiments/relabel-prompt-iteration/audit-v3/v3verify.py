import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, tarfile, collections as C
M = ROOT
def openers(p):
    out = {}; bad = C.Counter()
    for l in open(p, encoding="utf-8"):
        t = json.loads(l)
        if t.get("batch"): bad["batch"] += 1; continue
        if t["context"]["role"] == "opener":
            out[t["context"]["thread_id"]] = t
        bad[(t.get("label_version"), t.get("fallback"))] += 1
    return out, bad
def patch(p):
    P, Q = {}, {}
    with tarfile.open(p) as tf:
        print(p.split("\\")[-1], tf.getnames())
        for l in tf.extractfile("export/labels.jsonl"):
            r = json.loads(l); (P if r["target"] == "thread" else Q)[r["object_id"]] = r
    return P, Q
v1, _ = openers(M + r"\relabel\hans-relabel-202611222146.tgz.trace.jsonl")
v2, _ = openers(M + r"\export\hans-relabel-v2.trace.jsonl")
v3, b3 = openers(M + r"\export\hans-v3.trace.jsonl")
print("v3 trace records", dict(b3), "openers", len(v3), "in v2", len(set(v3) & set(v2)), "v3-only", len(set(v3) - set(v2)))
Pm, Qm = patch(M + r"\export\hans-relabel-merge.tgz")
P3, Q3 = patch(M + r"\export\hans-relabel-v3.tgz")
print("merge rows", len(Pm), dict(C.Counter(r.get("action") for r in Qm.values())), "v3 patch rows", len(P3), dict(C.Counter(r.get("action") for r in Q3.values())))
print("merge label versions", C.Counter(r["labels"].get("classifier_version") for r in Pm.values()))
S = [json.loads(l) for l in open(M + r"\v3-audit.jsonl", encoding="utf-8") if l.strip()]
rows = [r for r in S if "thread_id" in r]; foot = [r for r in S if "thread_id" not in r]
print("sample rows", len(rows), "footer", [json.dumps(f)[:1500] for f in foot])
print("keys", C.Counter(tuple(sorted(r)) for r in rows))
chk = C.Counter()
for r in rows:
    k = r["thread_id"]
    chk["in v3"] += k in v3; chk["in merge"] += k in Pm
    chk["new==v3"] += k in v3 and r["new"] == v3[k]["labels"]["comment_type"]
    chk["labels==v3"] += k in v3 and r["labels"] == v3[k]["labels"]
    chk["text==v3"] += k in v3 and r["text"] == v3[k]["text"]
    chk["old==merge"] += k in Pm and r["old"] == Pm[k]["labels"]["comment_type"]
    chk["old==v1"] += k in v1 and r["old"] == v1[k]["labels"]["comment_type"]
    chk["old==v2"] += k in v2 and r["old"] == v2[k]["labels"]["comment_type"]
    chk["old==mockprev"] += k in Pm and r["old"] == Pm[k]["previous"].get("comment_type")
    chk["old==new"] += r["old"] == r["new"]
print(dict(chk))
print("transitions", C.Counter((r["old"], r["new"]) for r in rows).most_common(30))
# merge vs v3 on all shared threads
sh = [k for k in v3 if k in Pm]
ch = C.Counter((Pm[k]["labels"]["comment_type"], v3[k]["labels"]["comment_type"]) for k in sh)
print("shared", len(sh), "changed", sum(v for (a, b), v in ch.items() if a != b))
print("top", [(f"{a}->{b}", v) for (a, b), v in ch.most_common(25) if a != b])

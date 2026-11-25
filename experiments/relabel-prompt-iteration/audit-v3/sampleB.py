"""Sample B: merge patch -> v3 PATCH (both post-guard). Same selection rule as
audit_hypotheses.py sample: group by (old,new), sort by thread_id, first 20."""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, tarfile, collections as C
M = ROOT
W = WORK
def patch(p):
    P = {}
    with tarfile.open(p) as tf:
        for l in tf.extractfile("export/labels.jsonl"):
            r = json.loads(l)
            if r["target"] == "thread": P[r["object_id"]] = r["labels"]["comment_type"]
    return P
Pm = patch(M + r"\export\hans-relabel-merge.tgz"); P3 = patch(M + r"\export\hans-relabel-v3.tgz")
v3 = {}
for l in open(M + r"\export\hans-v3.trace.jsonl", encoding="utf-8"):
    t = json.loads(l)
    if not t.get("batch") and t["context"]["role"] == "opener": v3[t["context"]["thread_id"]] = t
mand = {l.strip() for l in open(M + r"\relabel\relabel-mandatory-threads.txt") if l.strip()}
G = C.defaultdict(list)
for k, t in v3.items():
    if k in Pm and k in P3 and Pm[k] != P3[k]:
        G[(Pm[k], P3[k])].append(k)
print("changed (post-guard)", sum(map(len, G.values())), "mandatory among them", sum(k in mand for g in G.values() for k in g))
with open(W + r"\v3-audit-B.jsonl", "w", encoding="utf-8") as f:
    for (a, b), ks in sorted(G.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        ks.sort()
        for k in ks[:20]:
            f.write(json.dumps({"thread_id": k, "old": a, "new": b, "labels": v3[k]["labels"], "text": v3[k]["text"]}, ensure_ascii=False) + "\n")
        print(f"  {a:13}->{b:13} {len(ks):4} changed {min(20, len(ks)):3} sampled")
A = {json.loads(l)["thread_id"] for l in open(M + r"\v3-audit.jsonl", encoding="utf-8")}
B = {json.loads(l)["thread_id"] for l in open(W + r"\v3-audit-B.jsonl", encoding="utf-8")}
print("A", len(A), "B", len(B), "overlap", len(A & B), "B-only", len(B - A), "A-only", len(A - B), "A-only mandatory", len((A - B) & mand))

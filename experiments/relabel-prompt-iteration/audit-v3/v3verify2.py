import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import collections as C
exec(open(_os.path.join(HERE, "v3verify.py"), encoding="utf-8").read().split("S = [json.loads")[0])
S = [json.loads(l) for l in open(M + r"\v3-audit.jsonl", encoding="utf-8") if l.strip()]
mand = {l.strip() for l in open(M + r"\relabel\relabel-mandatory-threads.txt") if l.strip()}
sh = [k for k in v3 if k in Pm]
# does the merge follow the agreed spec?
def spec(k):
    a, b = v1[k]["labels"]["comment_type"], v2[k]["labels"]["comment_type"]
    if b == "out_of_scope" and a in ("suggestion", "question"): return b
    return a
ok = C.Counter()
for k in sh:
    m = Pm[k]["labels"]["comment_type"]
    ok[("mand" if k in mand else "free", m == spec(k), m == "blocker")] += 1
print("merge == spec (mand?, match, is blocker)", dict(ok))
bad = [k for k in sh if k not in mand and Pm[k]["labels"]["comment_type"] != spec(k)]
print("non-mandatory spec mismatches", len(bad), C.Counter((v1[k]["labels"]["comment_type"], v2[k]["labels"]["comment_type"], Pm[k]["labels"]["comment_type"]) for k in bad).most_common(10))
# changed set by mandatory
ch = [(k, Pm[k]["labels"]["comment_type"], v3[k]["labels"]["comment_type"]) for k in sh if Pm[k]["labels"]["comment_type"] != v3[k]["labels"]["comment_type"]]
print("changed", len(ch), "mandatory", sum(k in mand for k, *_ in ch))
print("changed non-mandatory top", C.Counter((a, b) for k, a, b in ch if k not in mand).most_common(15))
print("v3 patch == v3 trace", C.Counter((k in mand, P3[k]["labels"]["comment_type"] == v3[k]["labels"]["comment_type"]) for k in sh))
print("sample mandatory", sum(r["thread_id"] in mand for r in S), C.Counter((r["old"], r["new"]) for r in S if r["thread_id"] in mand).most_common(6))
# question/answered/intent in merge vs v1
qm = C.Counter((v1[k]["labels"]["question"], v3[k]["labels"]["question"]) for k in sh)
print("question v1->v3", dict(qm))
print("answered v3 true", sum(v3[k]["labels"]["answered"] for k in sh), "intent v3", C.Counter(v3[k]["labels"]["thread_intent"] for k in sh))
print("merge manifest", json.loads(tarfile.open(M + r"\export\hans-relabel-merge.tgz").extractfile("export/manifest.json").read()).get("text_classifier"))

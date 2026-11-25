"""Join trace openers/descriptions to patch rows and draw a deterministic
stratified sample. Raw text stays in the local sample file only."""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import hashlib, json, sys, collections as C
sys.path.insert(0, ROOT)
from ingest.classify import MockClassifier

D = ROOT + r"\relabel"
W = WORK
H = lambda s: hashlib.sha256(s.encode()).hexdigest()

def rows(p):
    with open(p, encoding="utf-8") as f:
        for l in f:
            if l.strip():
                yield json.loads(l)

patch = list(rows(W + r"\patch\export\labels.jsonl"))
th = {r["object_id"]: r for r in patch if r["target"] == "thread"}
qrow = {r["object_id"]: r for r in patch if r["target"] == "question"}
mand = {l.strip() for l in open(D + r"\relabel-mandatory-threads.txt") if l.strip()}
mock = MockClassifier()

recs, descs = {}, {}
for t in rows(D + r"\hans-relabel-202611222146.tgz.trace.jsonl"):
    if t.get("batch"):
        continue
    c = t["context"]
    if c["role"] == "opener":
        recs[c["thread_id"]] = t            # last call wins (= patch, verified)
    elif c["role"] == "description":
        descs[f"PR:{c['repo']}#{c['pr_id']}"] = t

items, mockcheck = [], C.Counter()
for tid, t in recs.items():
    r = th.get(tid)
    if not r:
        continue
    m = mock._labels_only(t["text"], "comment")
    mockcheck[m["comment_type"] == r["previous"]["comment_type"]] += 1
    q = qrow.get(tid)
    items.append(dict(
        id=tid, h=H(tid), n=len(t["text"]), mand=tid in mand,
        old=r["previous"]["comment_type"], new=r["labels"]["comment_type"],
        old_out=r["previous"].get("thread_outcome"), new_out=r["labels"].get("thread_outcome"),
        mock_intent=m["thread_intent"], mock_q=m["question"],
        intent=t["labels"]["thread_intent"], q=t["labels"]["question"],
        ans=t["labels"]["answered"], phase=t["labels"]["phase"],
        qflip=q["action"] if q else None, text=t["text"]))
print("mock reproduces stored previous comment_type:", dict(mockcheck))
print("PR question rows", sum(k.startswith("PR:") for k in qrow), "with description trace", sum(k in descs for k in qrow if k.startswith("PR:")))

dsel = []
for pid, t in descs.items():
    q = qrow.get(pid)
    dsel.append(dict(id=pid, h=H(pid), n=len(t["text"]), qflip=q["action"] if q else None,
                     mock_q=mock._labels_only(t["text"], "description")["question"],
                     q=t["labels"]["question"], phase=t["labels"]["phase"], text=t["text"]))

def take(pool, k):
    return sorted(pool, key=lambda x: x["h"])[:k]

S = []
def add(name, pool, k):
    for x in take(pool, k):
        S.append(dict(x, stratum=name))
    print(f"{name}: pool {len(pool)} took {min(k, len(pool))}")

nm = [x for x in items if not x["mand"]]        # type judgement on non-mandatory
for a, b in [("suggestion", "nit"), ("suggestion", "optimization"), ("question", "suggestion"),
             ("suggestion", "question"), ("suggestion", "blocker"), ("suggestion", "out_of_scope"),
             ("question", "nit"), ("blocker", "suggestion"), ("optimization", "suggestion"),
             ("blocker", "question")]:
    add(f"T:{a}->{b}", [x for x in nm if x["old"] == a and x["new"] == b], 20)
add("unchanged", [x for x in nm if x["old"] == x["new"]], 50)
add("mandatory", [x for x in items if x["mand"]], 30)
add("desc_qflip", [x for x in dsel if x["qflip"]], 30)
add("short3-20", [x for x in items if 3 <= x["n"] <= 20], 30)
add("O:acted_on->unresolved", [x for x in items if x["old_out"] == "acted_on" and x["new_out"] == "unresolved"], 20)
add("O:rejected->unresolved", [x for x in items if x["old_out"] == "rejected" and x["new_out"] == "unresolved"], 15)
add("O:walked_back->unresolved", [x for x in items if x["old_out"] == "walked_back" and x["new_out"] == "unresolved"], 10)
add("O:->acted_on/rejected(new)", [x for x in items if x["old_out"] != x["new_out"] and x["new_out"] != "unresolved"], 10)

# length stats
L = sorted(x["n"] for x in items)
print("opener length p10/p50/p90/max", [L[int(len(L) * p)] for p in (.1, .5, .9)], L[-1])
print("mandatory model types", C.Counter(x["new"] for x in items if x["mand"]))
print("non-mandatory model blocker", sum(x["new"] == "blocker" for x in nm), "of", len(nm))
print("intent dist model", C.Counter(x["intent"] for x in items), "mock", C.Counter(x["mock_intent"] for x in items))
print("outcome old", C.Counter(x["old_out"] for x in items), "new", C.Counter(x["new_out"] for x in items))

with open(W + r"\sample.jsonl", "w", encoding="utf-8") as f:
    for i, x in enumerate(S):
        x = dict(x, i=i); x.pop("h")
        f.write(json.dumps(x, ensure_ascii=False) + "\n")
print("sample", len(S))

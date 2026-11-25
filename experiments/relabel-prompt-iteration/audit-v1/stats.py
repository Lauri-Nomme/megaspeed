import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, sys, collections as C
D = ROOT + r"\relabel"
W = WORK

def rows(p):
    with open(p, encoding="utf-8") as f:
        for l in f:
            if l.strip():
                yield json.loads(l)

patch = list(rows(W + r"\patch\export\labels.jsonl"))
side = list(rows(D + r"\hans-relabel-202611222146.tgz.labels.jsonl"))
print("patch rows", len(patch), "side rows", len(side))
# compare side vs patch
print("cache", C.Counter((s.get("think"), s.get("model"), s.get("version"), s.get("kind")) for s in side))
print("cache keys", C.Counter(tuple(sorted(s)) for s in side))
print("unique digests", len({s["digest"] for s in side}))

th = [r for r in patch if r["target"] == "thread"]
qr = [r for r in patch if r["target"] == "question"]
print("thread", len(th), "question", len(qr), C.Counter(r.get("action") for r in qr))
print("other targets", C.Counter(r["target"] for r in patch))
old = C.Counter(r["previous"]["comment_type"] for r in th)
new = C.Counter(r["labels"]["comment_type"] for r in th)
print("type mock/model", {k: (old[k], new[k]) for k in sorted(set(old) | set(new))})
tr = C.Counter((r["previous"]["comment_type"], r["labels"]["comment_type"]) for r in th)
print("changed", sum(v for (a, b), v in tr.items() if a != b))
print("top transitions", [(f"{a}->{b}", v) for (a, b), v in tr.most_common(16) if a != b])
oc = C.Counter((r["previous"].get("thread_outcome"), r["labels"].get("thread_outcome")) for r in th)
print("outcome flips", sum(v for (a, b), v in oc.items() if a != b), [(f"{a}->{b}", v) for (a, b), v in oc.most_common() if a != b])
thtype = {r["object_id"]: r["previous"]["comment_type"] for r in th}
qf = C.Counter((("PR" if r["object_id"].startswith("PR:") else thtype.get(r["object_id"], "?")), r["action"]) for r in qr)
print("question flips by old type", sorted(qf.items()))

mand = set(l.strip() for l in open(D + r"\relabel-mandatory-threads.txt") if l.strip())
mt = [r for r in th if r["object_id"] in mand]
print("mandatory list", len(mand), "in patch", len(mt), "old blocker", sum(r["previous"]["comment_type"] == "blocker" for r in mt), "model blocker", sum(r["labels"]["comment_type"] == "blocker" for r in mt))
print("model blocker outside mandatory", sum(r["labels"]["comment_type"] == "blocker" and r["object_id"] not in mand for r in th))

# trace
n = 0; roles = C.Counter(); fb = 0; ok = 0; think = 0; keys = None; mism = 0; joined = 0
op = {}; batches = []; reqthink = C.Counter(); sent = C.Counter(); dig = C.Counter(); opdup = 0
thinkcalls = []
for t in rows(D + r"\hans-relabel-202611222146.tgz.trace.jsonl"):
    n += 1
    if t.get("batch"):
        batches.append((t["call"], t["ts"], t["count"], t["ok"], t["error"], t["timings"])); continue
    roles[t["context"]["role"]] += 1
    fb += bool(t.get("fallback")); ok += bool(t.get("ok"))
    th_ = t.get("thinking"); think += th_ is not None
    reqthink[(t["request"].get("think"), bool(th_))] += 1
    if th_ is not None: thinkcalls.append((t["call"], t["ts"], len(th_ or "")))
    sent[t.get("sent")] += 1; dig[t["text_digest"]] += 1
    if t["context"]["role"] == "opener":
        tid = t["context"]["thread_id"]
        opdup += tid in op
        op[tid] = t["labels"]
print("batch records", batches)
print("per-text", n - len(batches), "roles", roles, "fallback", fb, "ok", ok, "thinking!=None", think)
print("(request.think, has thinking)", reqthink, "sent", sent)
print("think calls range", thinkcalls[:2], thinkcalls[-2:], len(thinkcalls))
print("distinct digests", len(dig), "dup digest records", sum(v - 1 for v in dig.values()), "dup opener thread ids", opdup)
r2 = {r["object_id"]: r for r in th}
for tid, lab in op.items():
    if tid in r2:
        joined += 1
        mism += lab["comment_type"] != r2[tid]["labels"]["comment_type"]
print("openers", len(op), "joined", joined, "label mismatch trace vs patch", mism, "threads without opener", len(set(r2) - set(op)))

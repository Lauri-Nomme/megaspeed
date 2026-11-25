"""Ratings (3=v3 better, M=merge better, W=both wrong, N=no-fit, U=unclear) per row
of sample B (post-guard) and text-only verdicts for the 58 mandatory rows of A.
Then: A scored naively (as the tool produced it) vs B."""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, collections as C, math
M_ = ROOT
W_ = WORK
RB = ("M33U3333N3N3N3NNW3"[:0] +  # placeholder, real string below
      "")
RB = "".join([
    "M33U33NN3333N3NNW3N3"[:0],
])
# rows 0-19 q->sugg
r = {}
def put(start, s):
    for i, c in enumerate(s): r[start + i] = c
put(0,   "M33U33NN333N3N3NNWN3")
put(20,  "UNN3UNMN3M3UM3NMN33W")
put(40,  "UMNMMUMNMUU3NMNMUMMM")
put(60,  "NNUNNNNNUMM3NNNNNN3N")
put(80,  "MNN3NNNNNUMNNNNNNMNM")
put(100, "MM3NM3U33UU3NNU3M3MM")
put(120, "MUW3MUWWU3MUMMNMU3NM")
put(140, "NNN3N33NNN3N3N3NNUNN"[:20])
put(160, "33N3333N3MUU3U33MN33")
put(180, "NNNNNN3UMUNUNNNNNNUN")
put(200, "N" * 20)
put(220, "MUMWMWUMUMMUMU")
put(234, "NMMMU3NNN3N")
put(245, "UWMMMM3MMU")
put(255, "NNM33NNN")
put(263, "U3U333"[:0] + "UU3U33")
put(269, "33WNUW")
put(275, "NUU33")
put(280, "3MN3N")
put(285, "UMNNU")
put(290, "U3N")
put(293, "MMM")
put(296, "NU")
put(298, "MN")
put(300, "M")
put(301, "3")
B = [json.loads(l) for l in open(W_ + r"\v3-audit-B.jsonl", encoding="utf-8")]
assert len(r) == len(B) == 302, (len(r), len(B))
for i, x in enumerate(B): x["r"] = r[i]
# mandatory text-only: v3 label reasonable for the text? ok / wrong / unclear
mand = {l.strip() for l in open(M_ + r"\relabel\relabel-mandatory-threads.txt") if l.strip()}
A = [json.loads(l) for l in open(M_ + r"\v3-audit.jsonl", encoding="utf-8")]
AM = [x for x in A if x["thread_id"] in mand]
wrong = {6, 7, 15, 21, 22, 24, 25, 47, 48, 50}; unc = {0, 57}
# merge label (blocker) is text-right only where the text reads as a defect
merge_right = {7, 21}
for i, x in enumerate(AM):
    if i in unc: x["r"] = "U"
    elif i in merge_right: x["r"] = "M"
    elif i in wrong: x["r"] = "W"
    else: x["r"] = "3"        # v3 text label fine, store 'blocker' only by DC severity
rb = {x["thread_id"]: x["r"] for x in B}; rm = {x["thread_id"]: x["r"] for x in AM}
for x in A: x["r"] = rm.get(x["thread_id"]) or rb[x["thread_id"]]

def wilson(k, n, z=1.96):
    if n == 0: return (0, 0)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)

def table(rows, pools, label):
    G = C.defaultdict(list)
    for x in rows: G[(x["old"], x["new"])].append(x["r"])
    print(f"\n== {label}: {len(rows)} rows")
    tot = C.Counter(); w3 = wM = 0
    for (a, b), rs in sorted(G.items(), key=lambda kv: -pools.get(kv[0], 0)):
        c = C.Counter(rs); tot.update(c); p = pools.get((a, b), 0); n = len(rs)
        w3 += p * c["3"] / n; wM += p * c["M"] / n
        print(f"  {a:12}->{b:12} pool {p:4} n {n:3}  v3 {c['3']:2} merge {c['M']:2} W {c['W']:2} N {c['N']:2} U {c['U']:2}")
    d = tot["3"] + tot["M"]; lo, hi = wilson(tot["3"], d)
    print(f"  TOTAL {dict(tot)} decisive {d}: v3 {tot['3']} ({tot['3']/d:.0%}, 95% {lo:.0%}-{hi:.0%})  volume-weighted v3 ~{w3:.0f} vs merge ~{wM:.0f}")
    return G

import tarfile
def patch(p):
    P = {}
    with tarfile.open(p) as tf:
        for l in tf.extractfile("export/labels.jsonl"):
            q = json.loads(l)
            if q["target"] == "thread": P[q["object_id"]] = q["labels"]["comment_type"]
    return P
Pm = patch(M_ + r"\export\hans-relabel-merge.tgz"); P3 = patch(M_ + r"\export\hans-relabel-v3.tgz")
v3t = {}
for l in open(M_ + r"\export\hans-v3.trace.jsonl", encoding="utf-8"):
    t = json.loads(l)
    if not t.get("batch") and t["context"]["role"] == "opener": v3t[t["context"]["thread_id"]] = t["labels"]["comment_type"]
poolB = C.Counter((Pm[k], P3[k]) for k in v3t if k in Pm and Pm[k] != P3[k])
poolA = C.Counter((Pm[k], v3t[k]) for k in v3t if k in Pm and Pm[k] != v3t[k])
table(B, poolB, "Sample B (merge patch -> v3 patch, post-guard)")
table(A, poolA, "Sample A as produced (merge patch -> v3 trace, mandatory judged on text)")
print("\nmandatory text-only:", C.Counter(x["r"] for x in AM), "v3 types", C.Counter(x["new"] for x in AM))


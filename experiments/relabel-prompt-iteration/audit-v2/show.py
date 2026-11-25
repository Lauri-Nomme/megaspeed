import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, sys
sys.stdout.reconfigure(encoding="utf-8")
mand = {l.strip() for l in open(ROOT + r"\relabel\relabel-mandatory-threads.txt") if l.strip()}
S = [json.loads(l) for l in open(ROOT + r"\v2-audit-v1v2.jsonl", encoding="utf-8") if l.strip()]
lo, hi = int(sys.argv[1]), int(sys.argv[2]); cut = int(sys.argv[3]) if len(sys.argv) > 3 else 300
for i, r in enumerate(S[lo:hi], start=lo):
    a, b = r["v1_labels"], r["labels"]
    t = " ".join(r["text"].split()); t = t if len(t) <= cut else t[:cut] + f"…[+{len(t)-cut}]"
    extra = []
    for k in ("thread_intent", "question", "answered"):
        if a[k] != b[k]:
            extra.append(f"{k[:3]}={a[k]}>{b[k]}")
    print(f"#{i} {r['old']}->{r['new']}{' MAND' if r['thread_id'] in mand else ''} {' '.join(extra)} | {t}")

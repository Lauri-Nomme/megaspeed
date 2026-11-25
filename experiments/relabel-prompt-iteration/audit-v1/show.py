import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, sys
W = WORK
lo, hi = int(sys.argv[1]), int(sys.argv[2])
cut = int(sys.argv[3]) if len(sys.argv) > 3 else 420
sys.stdout.reconfigure(encoding="utf-8")
for l in open(W + r"\sample.jsonl", encoding="utf-8"):
    x = json.loads(l)
    if not lo <= x["i"] < hi:
        continue
    t = " ".join(x["text"].split())
    t = t if len(t) <= cut else t[:cut] + f"…[+{len(t)-cut}]"
    if x["stratum"] == "desc_qflip":
        print(f"#{x['i']} {x['stratum']} qflip={x['qflip']} mockQ={x['mock_q']} modelQ={x['q']} | {t}")
    else:
        print(f"#{x['i']} {x['stratum']} {x['old']}->{x['new']} int={x['mock_intent']}/{x['intent']} "
              f"q={int(x['mock_q'])}/{int(x['q'])} ans={int(x['ans'])} out={x['old_out']}->{x['new_out']}"
              f"{' MAND' if x['mand'] else ''} | {t}")

"""Which files under iter/ hold raw comment text? Prints file names and counts only."""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json, os
from pathlib import Path
ROOT = Path(WORK)
texts = set()
for l in open(ROOT + r"\export\hans-v3.trace.jsonl", encoding="utf-8"):
    t = json.loads(l)
    if not t.get("batch") and len(t.get("text") or "") >= 40:
        texts.add(t["text"])
probes = [x[:40] for x in texts]
for p in sorted(ROOT.rglob("*")):
    if not p.is_file():
        continue
    try:
        body = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    hits = sum(1 for s in probes if s in body)
    if hits:
        print(f"{hits:6}  {p.stat().st_size:>10}  {p.relative_to(ROOT)}")

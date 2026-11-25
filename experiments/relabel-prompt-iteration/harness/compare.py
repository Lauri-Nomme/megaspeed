"""Per-field flip rate between two prediction files (ids and numbers only).

  python compare.py runs/a.jsonl runs/b.jsonl
  python compare.py runs/a.jsonl trace          # vs labels in the v3 trace
"""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import FIELDS, TRACE, read_jsonl


def load(src, want=None):
    if src == "trace":
        out = {}
        with open(TRACE, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r.get("kind") != "comment":
                    continue
                tid = r["context"]["thread_id"]
                if want is None or tid in want:
                    out[tid] = r["labels"]
        return out
    return {r["id"]: r["labels"] for r in read_jsonl(src)}


def main():
    a = load(sys.argv[1])
    b = load(sys.argv[2], set(a))
    ids = sorted(set(a) & set(b))
    res = {"n": len(ids)}
    for f in FIELDS:
        d = [i for i in ids if a[i].get(f) != b[i].get(f)]
        res[f] = {"flips": len(d), "rate": round(len(d) / len(ids), 4) if ids else None}
    res["any_field"] = sum(1 for i in ids if any(a[i].get(f) != b[i].get(f) for f in FIELDS))
    res["type_transitions"] = dict(Counter(f"{a[i]['comment_type']}->{b[i]['comment_type']}" for i in ids
                                           if a[i]["comment_type"] != b[i]["comment_type"]))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()

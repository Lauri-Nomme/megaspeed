"""Run a system prompt over rows, 4 workers, cached + resumable.

  python run.py --prompt prompts/v3.txt --eval ../eval.jsonl --split dev --out runs/v3-dev.jsonl
  python run.py --prompt prompts/v3.txt --rows rows.jsonl --salt rep2 --out runs/x.jsonl

--split: train (includes dev), dev, holdout, all.  --salt gives a separate cache
namespace (used to bypass the cache for a determinism re-run, still resumable).
"""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CLASSES, INTENTS, URL, Cache, cache_key, load_prompt, read_jsonl, request_body, sha, write_jsonl


def in_split(row, split):
    s = row.get("split")
    if split == "all":
        return True
    if split == "train":
        return s in ("train", "dev")
    return s == split


def valid(lab):
    return (
        isinstance(lab, dict)
        and lab.get("comment_type") in CLASSES
        and lab.get("thread_intent") in INTENTS
        and isinstance(lab.get("question"), bool)
        and isinstance(lab.get("answered"), bool)
        and lab.get("phase") in ("review", "implementation")
    )


def call(body, timeout, retries):
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(URL, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            wall = (time.perf_counter() - t0) * 1000
            content = out.get("message", {}).get("content", "")
            lab = json.loads(content)
            if not valid(lab):
                raise ValueError("schema-invalid output")
            return lab, wall, None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"[:200]
            time.sleep(min(30, 2 ** attempt))
    return None, None, last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--eval")
    g.add_argument("--rows")
    ap.add_argument("--split", default="dev")
    ap.add_argument("--salt", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--retries", type=int, default=3)
    a = ap.parse_args()

    system = load_prompt(a.prompt)
    rows = read_jsonl(a.eval or a.rows)
    if a.eval:
        rows = [r for r in rows if in_split(r, a.split)]
    cache = Cache()
    todo = [r for r in rows if not cache.get(cache_key(system, r["text"], a.salt))]
    print(f"prompt {sha(system)[:12]}  rows {len(rows)}  cached {len(rows) - len(todo)}  to call {len(todo)}", flush=True)

    walls, errors = [], 0
    t0 = time.perf_counter()
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(call, request_body(system, r["text"]), a.timeout, a.retries): r for r in todo}
        for i, f in enumerate(as_completed(futs), 1):
            r = futs[f]
            lab, wall, err = f.result()
            if lab is None:
                errors += 1
                print(f"  error id={r['id']} {err}", flush=True)
                continue
            walls.append(wall)
            cache.put({"key": cache_key(system, r["text"], a.salt), "labels": lab, "wall_ms": round(wall),
                       "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
            if i % 20 == 0:
                el = time.perf_counter() - t0
                print(f"  {i}/{len(todo)}  {i / el:.3f} texts/s", flush=True)
    elapsed = time.perf_counter() - t0

    preds = []
    for r in rows:
        rec = cache.get(cache_key(system, r["text"], a.salt))
        if rec:
            preds.append({"id": r["id"], "labels": rec["labels"], "wall_ms": rec.get("wall_ms")})
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    write_jsonl(a.out, preds)
    stats = {"prompt_sha": sha(system)[:12], "rows": len(rows), "called": len(walls), "errors": errors,
             "elapsed_s": round(elapsed, 1),
             "texts_per_s": round(len(walls) / elapsed, 3) if walls and elapsed else None,
             "median_wall_ms": round(statistics.median(walls)) if walls else None,
             "p90_wall_ms": round(sorted(walls)[int(0.9 * (len(walls) - 1))]) if walls else None}
    print(json.dumps(stats))
    with open(a.out + ".stats.json", "w") as f:
        json.dump(stats, f)


if __name__ == "__main__":
    main()

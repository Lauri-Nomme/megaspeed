"""Score predictions against frozen gold; ids and numbers only.

Pred source spec: a run file (runs/x.jsonl) or prompt:<path>[:<salt>] (cache lookup).

  python score.py prompt:prompts/v3.txt --split dev
  python score.py prompt:prompts/c1.txt --split dev --vs prompt:prompts/v3.txt --noise noise.json
  python score.py prompt:prompts/v3.txt --split train --json out.json

Populations: ALL = gold != nofit (high+low conf), HIGH = gold != nofit and conf=high.
Row-count acceptance (amended brief) is evaluated on ALL when --vs is given.
"""
import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import argparse
import hashlib
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CLASSES, HERE, ITER, Cache, load_prompt, lookup, mandatory_ids, read_jsonl

GOLD_SHA = "7dfae73f3d8a82edd5bccebd5cc8db9138ab4f9d536976af8a76ed41c3ca0fd1"
AB = {"blocker": "blk", "nit": "nit", "optimization": "opt", "out_of_scope": "oos", "question": "qst", "suggestion": "sug"}


def qtype(lab):
    return {**lab, "question": lab["comment_type"] == "question"}


def in_split(s, split):
    if split == "all":
        return True
    if split == "train":
        return s in ("train", "dev")
    return s == split


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def verify_gold(path):
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if h != GOLD_SHA:
        sys.exit(f"GOLD CHECKSUM MISMATCH {h}")


def load_pred(spec, rows_by_id):
    if spec.startswith("prompt:"):
        parts = spec[len("prompt:"):].split("|")
        path, salt = parts[0], (parts[1] if len(parts) > 1 else "")
        return lookup(Cache(), load_prompt(path), list(rows_by_id.values()), salt)
    return {r["id"]: r["labels"] for r in read_jsonl(spec)}


def pop_metrics(ids, gold, pred):
    pairs = [(gold[i]["comment_type"], pred[i]["comment_type"]) for i in ids]
    m = {"n": len(ids), "correct": sum(g == p for g, p in pairs), "per_class": {}}
    for c in CLASSES:
        tp = sum(1 for g, p in pairs if g == c and p == c)
        fp = sum(1 for g, p in pairs if g != c and p == c)
        fn = sum(1 for g, p in pairs if g == c and p != c)
        P, R, F = prf(tp, fp, fn)
        m["per_class"][c] = {"tp": tp, "fp": fp, "fn": fn, "P": P, "R": R, "F1": F}
    m["macro_F1"] = sum(v["F1"] for v in m["per_class"].values()) / len(CLASSES)
    d = [i for i in ids if gold[i].get("is_defect")]
    m["defect_n"] = len(d)
    m["defect_tp"] = sum(pred[i]["comment_type"] == "blocker" for i in d)
    m["blocker_fp_nondefect"] = sum(1 for i in ids if pred[i]["comment_type"] == "blocker" and not gold[i].get("is_defect"))
    m["confusion"] = {g: dict(Counter(pred[i]["comment_type"] for i in ids if gold[i]["comment_type"] == g)) for g in CLASSES}
    for name, pr in (("raw", lambda x: x), ("qtype", qtype)):
        q = [(gold[i]["question"], pr(pred[i])) for i in ids]
        m[f"qflag_correct_{name}"] = sum(g == p["question"] for g, p in q)
        m[f"qflag_fp_nonqtype_{name}"] = sum(1 for g, p in q if p["comment_type"] != "question" and p["question"] and not g)
    m["intent_correct"] = sum(pred[i]["thread_intent"] == gold[i]["thread_intent"] for i in ids)
    return m


def metrics(ids, gold, pred, mand):
    allp = [i for i in ids if gold[i]["comment_type"] != "nofit"]
    high = [i for i in allp if gold[i].get("confidence") == "high"]
    low = [i for i in allp if gold[i].get("confidence") != "high"]
    nofit = [i for i in ids if gold[i]["comment_type"] == "nofit"]
    res = {"ALL": pop_metrics(allp, gold, pred), "HIGH": pop_metrics(high, gold, pred),
           "LOW": pop_metrics(low, gold, pred)}
    res["nofit_n"] = len(nofit)
    res["nofit_dist"] = dict(Counter(pred[i]["comment_type"] for i in nofit))
    res["nofit_qflag_true"] = sum(pred[i]["question"] for i in nofit)
    md = [i for i in ids if i in mand and gold[i].get("is_defect")]
    res["mandatory_defect_n"] = len(md)
    res["mandatory_defect_not_blocker"] = sum(pred[i]["comment_type"] != "blocker" for i in md)
    return res


def fmt_pop(m, name):
    L = []
    b = m["per_class"]["blocker"]
    L.append(f"[{name}] n={m['n']} correct={m['correct']} macroF1={m['macro_F1']:.3f} intent_correct={m['intent_correct']}")
    L.append(f"  blocker tp={b['tp']} fp={b['fp']} fn={b['fn']} P={b['P']:.3f} R={b['R']:.3f} F1={b['F1']:.3f}"
             f" | defect recall {m['defect_tp']}/{m['defect_n']} | blocker on non-defect {m['blocker_fp_nondefect']}")
    o = m["per_class"]["optimization"]
    L.append(f"  optimization tp={o['tp']} fp={o['fp']} fn={o['fn']} F1={o['F1']:.3f}")
    L.append(f"  qflag correct raw={m['qflag_correct_raw']}/{m['n']} qtype={m['qflag_correct_qtype']}/{m['n']}"
             f" | type!=q & q=true & gold q=false raw={m['qflag_fp_nonqtype_raw']} qtype={m['qflag_fp_nonqtype_qtype']}")
    L.append(f"  {'class':<13}{'tp':>4}{'fp':>4}{'fn':>4}{'P':>7}{'R':>7}{'F1':>7}")
    for c, v in m["per_class"].items():
        L.append(f"  {c:<13}{v['tp']:>4}{v['fp']:>4}{v['fn']:>4}{v['P']:>7.3f}{v['R']:>7.3f}{v['F1']:>7.3f}")
    L.append("  confusion gold\\pred " + " ".join(f"{AB[c]:>4}" for c in CLASSES))
    for g in CLASSES:
        L.append(f"   {g:<17}" + " ".join(f"{m['confusion'][g].get(c, 0):>4}" for c in CLASSES))
    return L


def fmt(res, title):
    L = [f"== {title}"]
    L += fmt_pop(res["ALL"], "ALL non-nofit")
    L += fmt_pop(res["HIGH"], "HIGH non-nofit")
    lo = res["LOW"]
    L.append(f"[LOW non-nofit] n={lo['n']} correct={lo['correct']} defect {lo['defect_tp']}/{lo['defect_n']}"
             f" blocker tp={lo['per_class']['blocker']['tp']} fp={lo['per_class']['blocker']['fp']}")
    L.append(f"[gold nofit] n={res['nofit_n']} pred dist " + " ".join(f"{AB[k]}={v}" for k, v in sorted(res["nofit_dist"].items()))
             + f" qflag_true={res['nofit_qflag_true']}")
    L.append(f"[mandatory] is_defect not blocker {res['mandatory_defect_not_blocker']}/{res['mandatory_defect_n']}")
    return "\n".join(L)


def accept(new, old, noise, target):
    """Row-count acceptance on ALL. Returns (ok, reasons, deltas)."""
    n, o = new["ALL"], old["ALL"]
    d = {
        "defect_tp": n["defect_tp"] - o["defect_tp"],
        "blocker_fp": n["per_class"]["blocker"]["fp"] - o["per_class"]["blocker"]["fp"],
        "opt_tp": n["per_class"]["optimization"]["tp"] - o["per_class"]["optimization"]["tp"],
        "opt_fp": n["per_class"]["optimization"]["fp"] - o["per_class"]["optimization"]["fp"],
        "qflag_raw": n["qflag_correct_raw"] - o["qflag_correct_raw"],
        "correct": n["correct"] - o["correct"],
        "nofit_blocker": new["nofit_dist"].get("blocker", 0) - old["nofit_dist"].get("blocker", 0),
        "nofit_question": new["nofit_dist"].get("question", 0) - old["nofit_dist"].get("question", 0),
    }
    d["class_tp"] = {c: n["per_class"][c]["tp"] - o["per_class"][c]["tp"] for c in CLASSES}
    why = []
    if target == "blocker":
        if d["defect_tp"] < 3: why.append(f"defect_tp {d['defect_tp']:+d} < +3")
        if d["blocker_fp"] > 1: why.append(f"blocker_fp {d['blocker_fp']:+d} > +1")
    else:
        if d["opt_tp"] < 2: why.append(f"opt_tp {d['opt_tp']:+d} < +2")
        if d["opt_fp"] > 1: why.append(f"opt_fp {d['opt_fp']:+d} > +1")
    for c, v in d["class_tp"].items():
        if v < -2: why.append(f"{c} tp {v:+d} < -2")
    if d["qflag_raw"] < -2: why.append(f"qflag {d['qflag_raw']:+d} < -2")
    d["nofit_class"] = {c: new["nofit_dist"].get(c, 0) - old["nofit_dist"].get(c, 0) for c in CLASSES}
    for c, v in d["nofit_class"].items():
        if v > 2: why.append(f"nofit->{c} {v:+d} > +2 (systematic)")
    nf = (noise or {}).get("defect_tp" if target == "blocker" else "opt_tp", 0)
    key = d["defect_tp"] if target == "blocker" else d["opt_tp"]
    if key <= nf: why.append(f"target delta {key:+d} within noise {nf}")
    return not why, why, d


def secondary(pred, gold_path, label, only_ids=None):
    rows = read_jsonl(gold_path)
    rows = [r for r in rows if r["id"] in pred and (only_ids is None or r["id"] in only_ids)]
    nn = [r for r in rows if r["comment_type"] != "nofit"]
    ok = sum(pred[r["id"]]["comment_type"] == r["comment_type"] for r in nn)
    gb = [r for r in nn if r["comment_type"] == "blocker"]
    pb = [r for r in nn if pred[r["id"]]["comment_type"] == "blocker"]
    tp = sum(pred[r["id"]]["comment_type"] == "blocker" for r in gb)
    return (f"[{label}] overlap={len(rows)} non-nofit={len(nn)} type agree={ok}/{len(nn)}"
            f" blocker tp={tp}/{len(gb)} pred_blocker={len(pb)}"), {"n": len(nn), "agree": ok, "blk_tp": tp, "blk_gold": len(gb), "blk_pred": len(pb)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pred")
    ap.add_argument("--vs")
    ap.add_argument("--target", default="blocker", choices=["blocker", "optimization"])
    ap.add_argument("--noise")
    ap.add_argument("--eval", default=os.path.join(ITER, "eval.jsonl"))
    ap.add_argument("--gold", default=os.path.join(ITER, "gold.jsonl"))
    ap.add_argument("--split", default="dev")
    ap.add_argument("--json")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    verify_gold(a.gold)
    ev = {r["id"]: r for r in read_jsonl(a.eval)}
    gold = {r["id"]: r for r in read_jsonl(a.gold)}
    ids = [i for i, r in ev.items() if in_split(r.get("split"), a.split) and i in gold]
    rows = {i: ev[i] for i in ids}
    mand = mandatory_ids() | {i for i, r in ev.items() if r.get("stratum") == "mandatory"}
    pred = load_pred(a.pred, rows)
    miss = [i for i in ids if i not in pred]
    ids = [i for i in ids if i in pred]
    res = metrics(ids, gold, pred, mand)
    out = {"split": a.split, "scored": len(ids), "missing": len(miss), "res": res}
    print(f"gold sha ok | split={a.split} scored={len(ids)} missing={len(miss)}")
    if not a.quiet:
        print(fmt(res, a.pred))
    s, out["irr"] = secondary(pred, os.path.join(ITER, "irr.jsonl"), "irr", set(ids))
    print(s)
    arows = os.path.join(HERE, "audit_rows.jsonl")
    if os.path.exists(arows):
        apred = load_pred(a.pred, {r["id"]: r for r in read_jsonl(arows)})
        s, out["audit"] = secondary(apred, os.path.join(ITER, "gold_audit.jsonl"), "gold_audit")
        print(s)
    if a.vs:
        opred = load_pred(a.vs, rows)
        oids = [i for i in ids if i in opred]
        ores = metrics(oids, gold, {i: opred[i] for i in oids}, mand)
        nres = metrics(oids, gold, pred, mand)
        noise = json.load(open(a.noise)) if a.noise else None
        ok, why, d = accept(nres, ores, noise, a.target)
        print(f"VS {a.vs} (n={len(oids)}): deltas {json.dumps(d)}")
        print("ACCEPT" if ok else "REJECT: " + "; ".join(why))
        out["vs"] = {"ok": ok, "why": why, "deltas": d}
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()

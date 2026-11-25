import os as _os  # archived experiment: paths come from the environment
ROOT = _os.environ.get("MEGASPEED_ROOT", ".")
WORK = _os.environ.get("RELABEL_WORK", "work")  # local raw-zone working dir
HERE = _os.path.dirname(_os.path.abspath(__file__))
import difflib, hashlib
from pathlib import Path
P = Path(WORK + r"\harness\prompts")
OUT = Path(ROOT + r"\relabel3\prompt-v3-iter-tested-prompts.md")
meta = {
    "v3": ("baseline (current `ollama-v3`, `ingest/classify.py`)", "—", "—", "final candidate (no edit passed)"),
    "c1": ("blocker: judge consequence not tone; hedged/question-form concrete wrong behaviour is blocker; 2 question-form examples",
           "defTP +1, blkFP +2, sug −4, qflag −4, nofit→qst +3", "not run (dev failed)", "no"),
    "c2": ("blocker: hedging does not lower the class (declarative only); 2 declarative blocker examples",
           "defTP +2, blkFP +4, sug −3, nofit→oos +4", "not run (FP already > +1)", "no"),
    "c3": ("blocker: c2 + exclusion (weak/missing test, fragile design, tests/CI failing are not blocker); 'might' dropped",
           "defTP +1, blkFP +3, nit −2, nofit→oos +5", "not run", "no"),
    "c4": ("blocker: c3 with the 2nd example replaced by an invented weak-test → suggestion counter-example",
           "defTP +2, blkFP +3, qflag −3, nofit→oos +6",
           "defTP +5 (16→21/32), blkFP +3 (4→7), question −3, qflag −3, nofit→oos +9, correct −2", "no"),
    "o1": ("optimization: stated benefit decides suggestion vs optimization; cost-motivated requests are optimization, question=false; 2 examples",
           "optTP +2, optFP +1, sug −4, nofit→qst +3, nofit→oos +5",
           "optTP +3 (4→7), optFP +1, sug −5, nofit→opt +5, nofit→oos +7, nofit→qst +3, correct +1", "no"),
    "o2": ("optimization: o1 limited to a reviewer asking for a change; an author describing their change is excluded",
           "optTP +2, optFP +1, defTP +1, sug −2, nofit→opt/oos/qst +3 each",
           "optTP +3, optFP +1, defTP +3, blkFP −1, sug −2, question −1, qflag −2, correct +3; nofit→opt +8 (8→16/97), nofit→oos +4, nofit→qst +3; HIGH blocker F1 .909→.970, opt F1 .750→.909",
           "no (nofit guard); best candidate for another round"),
}
base = (P / "v3.txt").read_text(encoding="utf-8")
L = ["# Prompt v3 iteration — all tested prompts", "",
     "Prompts tried by the iteration loop (`relabel3/prompt-iteration-brief-v2.md`; full scores in",
     "`relabel3/iter-report/report.md`). Model `qwen3:30b-a3b`, `think:false`, `temperature 0`, `seed 0`.",
     "Deltas are rows vs the v3 baseline re-run on the same server, population = gold ≠ nofit.",
     "Examples inside the prompts are invented by the iterator, not corpus text.", "",
     "Acceptance (any edit): blocker defect-TP ≥ +3 with blocker-FP ≤ +1 (or optimization TP ≥ +2 with FP ≤ +1),",
     "no class −2 rows net, question flag ≤ −2 rows, beyond the dev noise floor (7.1% type flips), and no gold-nofit class gaining > +2.", "",
     "| prompt | edit | dev Δ | train Δ | kept |", "|---|---|---|---|---|"]
for k in ["v3", "c1", "c2", "c3", "c4", "o1", "o2"]:
    e, d, t, kept = meta[k]
    L.append(f"| [{k}](#{k}) | {e} | {d} | {t} | {kept} |")
L.append("")
for k in ["v3", "c1", "c2", "c3", "c4", "o1", "o2"]:
    txt = (P / f"{k}.txt").read_text(encoding="utf-8")
    h = hashlib.sha256(txt.encode()).hexdigest()[:12]
    L += [f"## {k}", "", f"{meta[k][0]}  ", f"file `harness/prompts/{k}.txt`, sha256 `{h}`, {len(txt)} chars", ""]
    if k != "v3":
        diff = difflib.unified_diff(base.splitlines(), txt.splitlines(), "v3.txt", f"{k}.txt", lineterm="")
        L += ["Diff vs v3:", "", "```diff", *diff, "```", ""]
    L += ["Full prompt:", "", "```text", txt.rstrip("\n"), "```", ""]
OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
print(OUT, sum(1 for _ in OUT.open(encoding="utf-8")), "lines")

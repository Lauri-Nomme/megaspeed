# Relabel prompt iteration — archived experiment

This archives the tooling, prompts and aggregate scores from the label-quality work:
- three corpus audits (mock→v1, v1→v2, merge→v3);
- a prompt-iteration loop that ran against a local Ollama `qwen3:30b-a3b`.

**Nothing here holds review text or organisation-specific identifiers. Thread and PR ids
are the one exception: the reports cite them as evidence.** Eval sets, gold labels,
per-row model outputs and audit samples stay in a local raw-zone directory and are not
committed (see *Not included*). Every committed file passed two scans before it was added:
one for raw-text overlap with the traces, one for organisation-specific identifiers.

This is an experiment archive, not a feature. The app does not import it, and it has no
tests of its own.

## Layout

| path | what |
|---|---|
| `harness/` | model runner (`run.py`: 4 workers, retries, a cache keyed on prompt hash and text hash), scorer (`score.py`: per-class P/R/F1, confusion matrix, blocker defect recall, question-flag checks, an optional post-rule), `compare.py` (flip rates between runs), preflight and audit-row prep |
| `harness/prompts/` | `v3.txt` (the baseline, identical to `system_prompt("comment")` at `ollama-v3`) and the six tested edits: `c1`–`c4` for blocker, `o1`–`o2` for optimization |
| `harness/runs/*.json` | aggregate scores, noise floor and run stats only. The per-row `*.jsonl` outputs are gitignored |
| `gold/` | scaffolding for the blind gold set: stratified sampling, freeze and checksum, the import of audit verdicts. `gold.sha256` pins the frozen gold file, which is kept locally |
| `audit-v1/`, `audit-v2/`, `audit-v3/` | join, sample, verify and score scripts from the three audits. They print ids and counts, never text |
| `briefs/` | the audit and iteration briefs. `prompt-iteration-brief-v2.md` is the two-agent version that was actually run |
| `prompt-v3-iter-tested-prompts.md` | every tested prompt, each with its full text, a diff against v3 and its dev/train deltas |
| `reports/` | the three audit reports and the iteration report. Thread and PR ids are kept as evidence. A bot pseudonym, internal service and class names, local paths and the short verbatim comment fragments are replaced with paraphrases |

## Running

```
set MEGASPEED_ROOT=<repo root>            # traces under export/, the mandatory-thread list under relabel/
set RELABEL_WORK=<local raw-zone dir>     # eval.jsonl, gold.jsonl, irr.jsonl, gold_audit.jsonl, harness cache
python harness/run.py --prompt harness/prompts/v3.txt --eval %RELABEL_WORK%/eval.jsonl --split dev --out harness/runs/v3-dev.jsonl
python harness/score.py --pred harness/runs/v3-dev.jsonl --split dev
```

The audit scripts are kept for reference. They expect the file layout of the original
working directory under `RELABEL_WORK`.

## Results (numbers only)

### Corpus audits

The audits used about 20 rated rows per transition, with one rater.

| comparison | decisive rows (new : old) | finding |
|---|---|---|
| mock → v1 | 124 : 26 | The model beats the keyword mock on review text. Outcome flips were a patch-builder bug that dropped diff-derived outcomes. |
| v1 → v2 | 81 : 75 | Mixed. Better on intent and deferrals. It reintroduced interrogative requests as questions and de-escalated some defects. |
| merge → v3 (post-guard) | 65 : 61 | It fixes the request-as-question error (10 : 1 on the largest transition), but over-corrects blocker and optimization. |

**Comparison basis matters.** Sampling the new model's pre-guard trace against a
post-guard baseline made v3 look much better: 68% of decisive rows against 52%. That is
because guard-forced threads appear as changes.

### Gold set and second rater

- **Gold set:** 360 openers, labelled blind with the gold-only value `nofit` for text no
  class fits. `nofit` is 39% of rows and low confidence 37.5%.
- **Independent second rater** (90 rows): `comment_type` kappa 0.83; on rows both raters
  marked high-confidence, kappa 1.0. On rows where either said blocker or defect, they
  agreed on only 5 of 8.

### Iteration loop

- **Baseline (v3):** train blocker precision 0.80, recall 0.53 (high-confidence: 0.88 /
  0.94), defect recall 16/32. Holdout blocker P/R 0.83 / 0.63.
- **Noise floor:** dev comment-type flips between two identical runs 7.1%. The server
  reproduces only 90% of the corpus run's types for identical requests.
- **Edits:** 6 tried, 0 kept.
  - The blocker edits added false blockers, and their gains landed on low-confidence gold.
  - The best optimization edit (`o2`, +3 net correct rows) pushed no-fit text into
    `optimization`.
- **Recommendation:** keep v3. Retry `o2` after adding a deterministic filter that
  keeps notices and author status notes away from the model.

## Not included (and why)

- **Eval texts, gold and second-rater labels, per-row run outputs, the harness cache, and
  audit samples:** they contain raw review text or thread identifiers.
- **Audit scripts whose regexes name internal bot handles, and the deny-list scanner:**
  the scanner's list is itself made of identifiers.
- **The pre-existing `audit_hypotheses.py` sampler:** it contains organisation-specific
  names. It is untracked at the repo root and still needs its own clean-up before any
  commit.

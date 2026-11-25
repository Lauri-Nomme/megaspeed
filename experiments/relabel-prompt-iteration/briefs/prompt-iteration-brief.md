# Prompt-iteration brief (local auditor + containerized Ollama)

Goal: iterate the label prompt in **minutes**, not one 8 h corpus pass per day,
without overfitting to a handful of examples. You are the **judge and prompt
editor**; the Ollama box is the model under test. The repo is not modified by
this loop — you return a final prompt and scores; a human applies it.

## Environment

- Ollama: `http://localhost:11434` (or the container's host:port), model
  `qwen3:30b-a3b`, `think: false`, `temperature: 0`, `seed: 0`.
- The JSON schema is fixed (the same `comment_type`, `thread_intent`,
  `question`, `answered`, `phase` object the repo uses). Do not add fields or
  classes.
- Everything is local. Do not paste the corpus text into a report; use ids.

## Inputs you build once

1. **Eval set** `eval.jsonl` — ~400–600 openers, stratified:
   - the decisive rows from the v1/v2 and v3 audits (they have a judged gold
     label);
   - a fresh stratum per class (blocker, optimization, question, nit,
     out_of_scope, suggestion), 40 each, which you label **blind, text-only,
     once**, and then **freeze** as gold;
   - exclude bots, CI notices and admin notes; exclude DC-mandatory threads
     from the type metric (the guard overrides them) but keep a few to check
     the prompt does not de-escalate code defects.
2. **Split**: 70% `train`, 30% `holdout`, by `sha256(id) % 10 < 7`. The holdout
   is never used to choose a prompt; it is run only on the final candidate.
3. **Baseline**: the current `system_prompt` from `ingest/classify.py`
   (v3). Score it first; that is the number to beat.

## Loop (budget: 6 iterations, ~10 min each at 4 workers)

For each iteration:

1. Run the **train** split with the candidate prompt.
2. Score against gold:
   - per-class precision / recall / F1 and a 6×6 confusion matrix;
   - `blocker` recall on the defect stratum (the primary metric);
   - `question` type+flag consistency: `comment_type != question` ⇒
     `question=false` unless the text is a real information ask;
   - a “no class regresses by more than 3 points” check.
3. Inspect the disagreements (ids + the text in your local scratchpad). Write
   **one** prompt edit that targets the largest cluster; keep the edit small and
   general (a rule + one or two examples), not a list of thread ids.
4. Re-run the train split. Keep the edit only if the primary metric improves
   and no class regresses.

Rules that keep this honest:

- Do **not** tune to individual ids. If a rule only helps because it names a
  specific phrasing, it will not survive the corpus.
- Do **not** change the parts the audits already settled (question/nit/
  out_of_scope wording, the intent=unclear rule) unless the metrics show a
  regression.
- Do **not** run the holdout until the final candidate.
- Prefer the smallest prompt: an extra sentence that buys <1 point on the train
  set is noise.
- The v3 audit says blocker and optimization are the weak classes; start there.

## What to return

1. The final candidate prompt, as one fenced text block.
2. A unified diff against the current `system_prompt` (so a human can apply it
   in `ingest/classify.py` and bump the version to `ollama-v4`).
3. Scores: baseline vs final, train **and** holdout, per class; confusion
   matrix; the defect-stratum blocker recall.
4. The 10–15 worst remaining failures (ids + a one-line reason each), so the
   next iteration or a corpus audit knows where it still breaks.
5. A one-line recommendation: adopt (one confirming corpus pass), iterate
   further, or stop and keep the current prompt.

## Constraints

- No repo writes, no store writes, no commits; the output is a prompt + scores.
- No raw text in the report; ids and paraphrases only.
- If the Ollama box is unreachable, stop and say so; do not invent scores.
- If the eval set and the corpus disagree later, the corpus wins — this loop is
  a filter, not the final word.

# Prompt-iteration brief v2 (gold labeller + iterator, containerised Ollama)

Revised from `prompt-iteration-brief.md`. What changed and why:

| change | why |
|---|---|
| **Two separate agents.** A *gold labeller* labels the eval set blind. An *iterator* edits prompts and runs the model. The orchestrator only reads scores and ids. | When the agent that writes the prompt also writes the gold, the gold drifts toward the prompt. Keeping the orchestrator out of the text keeps it an uncontaminated rater for the corpus audit. |
| **Gold is labelled without model labels or the prompt.** It is frozen, and checksummed, before any model call. | The v1–v3 audits were comparative and done with labels visible. |
| **A `nofit` gold value** for notices, rebase/status notes, review requests and bot commands. | 28–39% of the audited rows were no-fit. Scoring them as type errors measures noise. |
| **A primary metric that is blocker F1 with a precision floor,** plus defect recall. Recall alone is not the metric. | v1 over-escalated and v3 de-escalated. Recall alone rewards the v1 failure. |
| **A realistic time budget, a small dev split and a prompt-hash cache.** | v3 measured a median of 11.9 s per call at 4 workers, about 0.33 texts/s. 400 texts take about 20 min, not 10. |
| **A determinism check.** | `temperature 0` with 4 concurrent workers may not reproduce exactly. A noise floor has to be known before an edit counts as a win. |
| **The question flag is scored separately and may be fixed in code.** | v3 shows type and flag decoupled (289 suggestions with `question=true`). That may be cheaper to fix deterministically than with a prompt. |

## Roles

- **Orchestrator** (the main session): writes this brief, launches the agents
  and reads their summaries. It never reads eval text.
- **Gold labeller** (subagent A): builds the eval set and labels it, then hands
  it off.
- **Iterator** (subagent B): runs the model, scores it and edits the prompt.
  It may **not** modify gold. It may list suspected gold errors (ids plus a
  reason) in its report. They are reviewed after the loop, never during it.

## Environment

- **Ollama:** `qwen3:30b-a3b`, `think: false`, `temperature: 0`, `seed: 0`,
  `num_predict: 128`. Use the same request shape as the v3 trace records,
  with `format` set to the fixed JSON schema.
- **Schema:** fixed, with the same five fields and the same enums. No new
  fields or classes.
- **Working directory for raw text:** outside the repo, at
  `<WORK>\`.
  Reports go to `<ROOT>\relabel3\iter-report\`, with ids and
  numbers only.
- **No repo writes, no store writes, no commits.**

## Step A — eval set and gold (subagent A)

1. **Pool:** the v3 trace openers (`export\hans-v3.trace.jsonl`, role
   `opener`; v2's skip set already applied), with the last record per thread.
   The text is the trace text.
2. **Draw a deterministic sample** with key `sha256("eval" + thread_id)`,
   taking the lowest keys per stratum, deduplicated:
   - for each class `c` of the six, **40** threads where *any* of v1, v2 or v3
     said `c`;
   - **40** "defect" threads: a defect regex hit (fails, NPE, wrong,
     incorrect, leak, not closed, race, doesn't work, broken, missing
     check/close) or any model `blocker`;
   - **60** uniform random threads (the prior);
   - **20** DC-mandatory threads (`relabel\relabel-mandatory-threads.txt`),
     which are a de-escalation check only;
   - everything else non-mandatory.
3. **Label blind.** The labeller sees only `id` and `text`: no model labels,
   no stratum, no prompt from `ingest/classify.py`. It assigns:
   - `comment_type`: one of the six classes, or `nofit`;
   - `question` (true/false);
   - `is_defect` (true/false: the text reports concrete wrong code behaviour);
   - `thread_intent`;
   - `confidence` (high/low).

   Use the vocabulary in section 2 of `relabel\relabel-audit-prompt.md`. If
   the text does not decide, give `confidence=low`. Do not invent ground truth.
4. **Second blind pass** over a random 15%, in a reshuffled order and a fresh
   reading, to report intra-rater agreement. Rows where the passes disagree get
   `confidence=low`.
5. **Split:**
   - `holdout` if `int(sha256(id), 16) % 10 >= 7`;
   - otherwise `train`, and within train a `dev` subset of about 150 rows
     (`% 10 < 3`).
6. **Freeze.** Write `eval.jsonl` (id, text, split, stratum) and
   `gold.jsonl` (id plus labels) into the scratchpad `iter\` directory, and
   `gold.sha256`. After this the gold is read-only.
7. **Optional extra gold:** the v3 audit's decisive per-row verdicts (sample
   B, encoded in the scratchpad's `score.py`) yield a `comment_type` gold label
   (`3` gives the v3 label, `M` the merge label). Keep these as a separate
   `gold_audit.jsonl`, because they were made with labels visible. Report them,
   but do not use them to accept edits.

## Step B — loop (subagent B)

0. **Preflight.**
   - Check that Ollama responds and the model is loaded. If not, **stop and
     report**; never invent scores.
   - Check `gold.sha256`.
   - Run the baseline (the current v3 `system_prompt` from `ingest/classify.py`,
     read-only) on `dev` **twice**. The label-flip rate between the two runs is
     the noise floor.
1. **Cache** results by `(sha256(system_prompt), text_digest)` in the
   scratchpad, so a re-run or resume never repeats a call.
2. **Each iteration (budget 6):**
   1. Write **one** small, general edit: a rule plus at most two examples
      that target the largest disagreement cluster on `dev`. Never name ids
      or verbatim corpus phrases.
   2. **Screen** on `dev` (about 7 min). If the change is within the noise
      floor, discard it.
   3. **Accept** on the full `train` (about 20 min) only if:
      - the primary metric improves by more than the noise floor;
      - no class F1 drops by more than 3 points;
      - the `nofit` rows show no new systematic label.
3. **Metrics,** on rows with gold ≠ `nofit` and `confidence=high` unless noted:
   - **primary:** blocker F1 with blocker precision ≥ baseline − 3, plus
     recall on `is_defect=true`;
   - per-class precision, recall and F1, with a 6×6 confusion matrix (plus a
     `nofit` row);
   - optimization F1, the second weak class;
   - question type+flag: flag accuracy, and the count of
     `comment_type≠question ∧ question=true` where gold `question=false`;
   - mandatory subset: the rate of `is_defect` rows not labelled blocker;
   - low-confidence rows reported separately.
4. **Settled wording** (the question/request rule, `nit:` prefix, out_of_scope
   deferral, intent=unclear/withdrawn) stays as it is unless a metric shows it
   regressing.
5. **Deterministic fixes are allowed as a proposal, not as prompt text.** If
   the question flag or `answered` on openers is cheaper to fix in code, say
   so, and score the prompt **with and without** the proposed post-rule.
6. **Holdout:** run it once, on the final candidate and on the baseline, after
   the loop ends.

## Return (iterator report, ids and numbers only)

1. The final candidate prompt (fenced), and a unified diff against the current
   `system_prompt`.
2. Baseline vs final on train **and** holdout:
   - per-class P/R/F1;
   - the confusion matrix;
   - blocker defect-recall;
   - the noise floor;
   - the question-flag metric, with and without any proposed post-rule.
3. A log per iteration: the edit (one line), dev and train deltas, and
   whether it was kept.
4. The 10–15 worst remaining failures (id plus a one-line paraphrased reason)
   and any suspected gold errors.
5. A one-line recommendation: adopt (one confirming corpus pass and a v4
   audit), iterate further, or keep v3.

## Constraints

- No raw text in any report or in the summary returned to the orchestrator.
- The corpus audit outranks this loop: it is a filter, not the final word.

# Audit task — is v3 better than the merged v1/v2 labels?

Inputs: `v3-audit.jsonl` (from `audit_hypotheses.py sample --trace
export\hans-v3.trace.jsonl --baseline-patch export\hans-relabel-merge.tgz`),
plus the previous report `relabel-audit-v2-report.md`.

`old` = the label now in the store (v1 comment_type, v2 `out_of_scope`, v1
question/answered/phase). `new` = v3. `labels` = the full v3 label set. Text is
the v3 trace's. Local only: ids and paraphrases in the report.

## What v3 changed (judge only these)

1. **question**: requests/proposals (“can we / should we / let’s / please”) are
   `suggestion`, `question=false`.
2. **blocker**: code behaviour only; docs, naming and missing tests are not
   blockers; security/race/resource-close are.
3. **nit**: an explicit `nit:`/`ubernit` prefix stays `nit` unless it describes
   a defect.
4. **optimization**: speed/memory/allocation/throughput, benchmarks included.
5. **intent**: `withdrawn` for “nvm / never mind / ignore that”; openers should
   be `answered=false`.

## Questions

1. Per transition (top 15): v3 better / merged better / both ok / both wrong /
   unclear. Volume-weighted verdict.
2. Did v3 fix the v2 losses without giving back v2’s wins?
   - `suggestion → question` should be gone; “can we/should we” probes.
   - `blocker → suggestion/question` (the leak/close remarks) should be fixed.
   - `nit → blocker` should no longer be documentation/naming/test gaps.
   - `out_of_scope` should keep v2’s gains.
   - `withdrawn` should recover (v2: 8 of 48 “nvm”).
3. New failure patterns introduced by v3 (over-correction counts as a failure).
4. Distribution against the merged store and against v1/v2.
5. Recommendation: merge v3 (which classes), keep the merge, or a v4 on one
   class. Prefer a **label-level merge** over a full import unless v3 is better
   across the board.

## Method / output

Same as the v2 audit: per-transition table with counts; separate “no-fit” text;
confidence and limitations; no raw text in the report. Use `unclear` when the
text does not decide.

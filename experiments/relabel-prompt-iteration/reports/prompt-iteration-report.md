# Prompt-iteration report (iterator, subagent B)

Ids and numbers only. The prompt fragments below are written for this report and contain no corpus text.

## 0. Setup and integrity

- Model `qwen3:30b-a3b` on `http://localhost:11434`. Request shape is the v3 trace's: `think:false`, `stream:false`, the fixed `format` schema, `options {temperature 0, seed 0, num_predict 128}`. The harness body is dict-equal to all 8036 v3 opener requests.
- Baseline = `system_prompt("comment")`, sha256 prefix `5306695b03e0`. It equals the v3 trace system message in 8036 of 8036 records.
- Gold `iter\gold.jsonl` sha256 `7dfae73f3d8a82edd5bccebd5cc8db9138ab4f9d536976af8a76ed41c3ca0fd1`. The scorer checks it on every run, and it matched every time. `eval.jsonl` sha256 is `fea1b22c695c4b2f46576373111cc0e5b0d64ed1b6fede272e4883fc1c1dc708`.
- The harness, cache and run outputs are in `<WORK>\harness\`. The scripts are `run.py`, `score.py` and `compare.py`, the prompts are under `prompts\` and the outputs under `runs\`.
- Scoring follows the amended brief:
  - **ALL** = gold ≠ nofit, high and low confidence. **HIGH** = gold ≠ nofit and confidence = high.
  - Acceptance is counted in rows on ALL.
  - "Defect TP" = predicted blocker on a gold `is_defect` row.
  - "Blocker FP" = predicted blocker on a non-nofit row whose gold type is not blocker.
- **nofit guard:** for any class, more than +2 gold-nofit rows newly predicted as that class counts as a "new systematic label". This is stricter than the amendment, which names only blocker and question. Results under the amendment-only reading are given too.
- **Throughput:** warm runs reach 0.33–0.45 texts/s. Fresh-prompt dev runs reach 0.18–0.24 texts/s, with a long p90 tail of up to 61 s. A dev screen takes about 9–10 min and a train run about 6 min more. There were 0 errors and 0 retries in every run.

## 1. Noise floors

**Dev** (112 rows): baseline v3 run twice, the second run under a separate cache salt.

| field | flips | rate |
|---|---|---|
| comment_type | 8 | 0.071 |
| thread_intent | 1 | 0.009 |
| question | 2 | 0.018 |
| answered | 0 | 0 |
| phase | 0 | 0 |

The type flips are suggestion→nit 3, suggestion→out_of_scope 1, suggestion→question 1, question→suggestion 1, nit→out_of_scope 1, nit→suggestion 1.

The same run pair in acceptance units:

| quantity | rerun delta (rows) |
|---|---|
| defect TP | 0 |
| blocker FP | 0 |
| optimization TP / FP | 0 / 0 |
| question-flag correct | −2 |
| suggestion TP | −2 |
| total correct | −2 |
| nofit→out_of_scope | +2 |
| nofit→suggestion | −2 |

- **Preflight** (40 non-mandatory openers): 1 of 40 fields flipped (the question flag), 0 type flips. Run 1 against the corpus v3 trace: type agreement 0.90, the other fields 0.95–0.975. The server does not reproduce the corpus labels, so all baselines here were re-run on this server.
- **Edit churn:** any one-rule prompt edit changes 15–20% of dev types (rerun noise is 7%). Most of the churn is gold-nofit rows moving off suggestion: −5 to −17 rows per edit, against −2 for a rerun.
- The train noise floor was not measured with a separate double run. The dev floor is used.

## 2. Final candidate prompt

**No edit passed acceptance, so the final candidate is the unchanged v3 prompt.** Diff against the current `system_prompt`: none.

```
(identical to ingest/classify.py system_prompt("comment"), sha256 prefix 5306695b03e0;
 copy at harness\prompts\v3.txt)
```

Best rejected candidates, for the record (diffs against v3):

**c4 (blocker), rejected on train:**
```diff
@@ blocker definition, after "is NOT a blocker (nit or suggestion)."
+                 Hedging does not lower the class: a remark that states or
+                 suspects concrete wrong behaviour (an exception, a wrong
+                 result, a case the code does not handle, state that gets
+                 corrupted) in the code under review is a blocker even when it
+                 is short or says "I think" or "probably". A weak or missing
+                 test, a design that feels fragile, or a report that tests or CI
+                 fail is NOT this case.
@@ examples
+{"comment_type":"blocker",...} -- "might be off by one, the loop stops before the last page"
+{"comment_type":"suggestion",...} -- "this test never asserts the error code, so a wrong code would go unnoticed"
```

**o2 (optimization), rejected on train only by the nofit guard:**
```diff
@@ optimization definition, after "a deferral is out_of_scope"
+                 The stated benefit decides suggestion vs optimization: a request
+                 or proposal whose point is less work (fewer allocations or
+                 copies, memory released sooner, a redundant pass or read, work
+                 inside a hot loop) is optimization, still question=false. This is
+                 for a reviewer asking for a change; an author describing what
+                 they changed or why is not optimization.
@@ examples
+{"comment_type":"optimization",...} -- "could we reuse one buffer instead of allocating a new one per call?"
+{"comment_type":"optimization",...} -- "the list gets sorted twice on this path"
```

## 3. Baseline (= final) on train and holdout

### Train (253 rows: ALL n=156, HIGH n=89, LOW n=67, gold nofit n=97)

| class | ALL tp/fp/fn | ALL P / R / F1 | HIGH P / R / F1 |
|---|---|---|---|
| blocker | 16/4/14 | 0.800 / 0.533 / 0.640 | 0.882 / 0.938 / 0.909 |
| nit | 31/10/9 | 0.756 / 0.775 / 0.765 | 0.962 / 0.862 / 0.909 |
| optimization | 4/1/5 | 0.800 / 0.444 / 0.571 | 1.000 / 0.600 / 0.750 |
| out_of_scope | 0/0/2 | 0 / 0 / 0 | 0 / 0 / 0 |
| question | 34/4/3 | 0.895 / 0.919 / 0.907 | 0.944 / 0.944 / 0.944 |
| suggestion | 32/20/6 | 0.615 / 0.842 / 0.711 | 0.720 / 0.947 / 0.818 |

- **Defect recall:** ALL 16/32, HIGH 15/16, LOW 1/16.
- **Blocker on non-defect rows:** ALL 4, HIGH 2.
- **Macro F1:** ALL 0.599, HIGH 0.722.
- **Type correct:** ALL 117/156, HIGH 78/89.

Confusion, ALL (rows = gold, columns = predicted):

| gold \ pred | blk | nit | opt | oos | qst | sug |
|---|---|---|---|---|---|---|
| blocker | 16 | 4 | 0 | 0 | 2 | 8 |
| nit | 1 | 31 | 0 | 0 | 1 | 7 |
| optimization | 0 | 1 | 4 | 0 | 0 | 4 |
| out_of_scope | 0 | 0 | 0 | 0 | 1 | 1 |
| question | 1 | 2 | 0 | 0 | 34 | 0 |
| suggestion | 2 | 3 | 1 | 0 | 0 | 32 |
| nofit (n=97) | 8 | 24 | 8 | 17 | 3 | 37 |

Confusion, HIGH:

| gold \ pred | blk | nit | opt | oos | qst | sug |
|---|---|---|---|---|---|---|
| blocker | 15 | 0 | 0 | 0 | 0 | 1 |
| nit | 1 | 25 | 0 | 0 | 0 | 3 |
| optimization | 0 | 0 | 3 | 0 | 0 | 2 |
| out_of_scope | 0 | 0 | 0 | 0 | 1 | 1 |
| question | 0 | 1 | 0 | 0 | 17 | 0 |
| suggestion | 1 | 0 | 0 | 0 | 0 | 18 |

- **Question flag:** ALL raw 149/156, with the `qtype` post-rule 148/156. Rows with type≠question, flag=true and gold flag=false: raw 1, qtype 0. HIGH: raw 87/89, qtype 86/89.
- **Gold-nofit rows:** 3 predicted question, 4 with the question flag set.
- **Mandatory** `is_defect` rows not labelled blocker: 1/3.
- **Secondary checks:** irr (59 overlapping ids, 41 non-nofit) type agreement 28/41, blocker 2/5. gold_audit (126 ids, label-visible) type agreement 53/126, blocker 5/31, predicted blocker 11.

### Holdout (107 rows: ALL n=62, HIGH n=35, LOW n=27, gold nofit n=45), run once

| class | ALL tp/fp/fn | ALL P / R / F1 | HIGH P / R / F1 |
|---|---|---|---|
| blocker | 5/1/3 | 0.833 / 0.625 / 0.714 | 1.000 / 0.667 / 0.800 |
| nit | 13/3/8 | 0.812 / 0.619 / 0.703 | 0.917 / 0.647 / 0.759 |
| optimization | 1/0/1 | 1.000 / 0.500 / 0.667 | 1.000 / 1.000 / 1.000 |
| out_of_scope | 1/1/3 | 0.500 / 0.250 / 0.333 | 1.000 / 0.333 / 0.500 |
| question | 8/1/0 | 0.889 / 1.000 / 0.941 | 0.667 / 1.000 / 0.800 |
| suggestion | 17/11/2 | 0.607 / 0.895 / 0.723 | 0.562 / 1.000 / 0.720 |

- **Defect recall:** ALL 6/9, HIGH 2/3, LOW 4/6.
- **Blocker on non-defect rows:** 0.
- **Macro F1:** ALL 0.680, HIGH 0.763.
- **Type correct:** ALL 45/62, HIGH 26/35.

Confusion, ALL:

| gold \ pred | blk | nit | opt | oos | qst | sug |
|---|---|---|---|---|---|---|
| blocker | 5 | 1 | 0 | 1 | 0 | 1 |
| nit | 0 | 13 | 0 | 0 | 1 | 7 |
| optimization | 0 | 1 | 1 | 0 | 0 | 0 |
| out_of_scope | 0 | 0 | 0 | 1 | 0 | 3 |
| question | 0 | 0 | 0 | 0 | 8 | 0 |
| suggestion | 1 | 1 | 0 | 0 | 0 | 17 |
| nofit (n=45) | 7 | 7 | 2 | 9 | 1 | 19 |

- **Question flag:** ALL raw 61/62, qtype 62/62. Rows with type≠question, flag=true and gold flag=false: raw 1, qtype 0.
- **Mandatory subset:** no `is_defect` rows.
- **Secondary checks:** irr (31 overlapping ids, 16 non-nofit) type agreement 10/16, blocker 0/2.

**Question-flag post-rule (`question := comment_type == "question"`).** It moves accuracy by −1 row on train and +1 row on holdout, and removes the 1 type/flag decoupling row in each split. v3 already fixed most of the decoupling, so the post-rule is optional and small. It is harmless as a code-side guard.

## 4. Iteration log (6 of 6 used)

Deltas are in rows against the v3 baseline on the same server, population ALL.

| # | edit (one line) | dev Δ | train Δ | kept |
|---|---|---|---|---|
| c1 | Judge consequence not tone; hedged or question-form remarks naming a concrete wrong behaviour are blocker; 2 question-form examples | defTP +1, blkFP +2, sug −4, qflag −4, nofit→qst +3 | not run (dev fails) | no |
| c2 | Hedging does not lower the class (declarative only); 2 declarative blocker examples | defTP +2, blkFP +4, sug −3, nofit→oos +4 | not run (train ⊇ dev, FP already > +1) | no |
| c3 | c2 plus an exclusion: weak or missing test, fragile design, tests/CI failing are not blocker; "might" dropped | defTP +1, blkFP +3, nit −2, nofit→oos +5 | not run (same reason) | no |
| c4 | c3 with the second example replaced by an invented weak-test → suggestion counter-example | defTP +2, blkFP +3, qflag −3, nofit→oos +6 | defTP **+5** (16→21/32), blkFP +3 (4→7), question −3, qflag −3, nofit→oos +9, correct −2 | no |
| o1 | Stated benefit decides suggestion vs optimization; cost-motivated requests are optimization with question=false; 2 examples | optTP +2, optFP +1, sug −4, nofit→qst +3, nofit→oos +5 | optTP **+3** (4→7), optFP +1, sug −5, nofit→opt +5, nofit→oos +7, nofit→qst +3, correct +1 | no |
| o2 | o1 limited to a reviewer asking for a change; an author describing their change is excluded | optTP +2, optFP +1, defTP +1, sug −2, nofit→opt/oos/qst +3 each | optTP **+3**, optFP +1, defTP +3, blkFP −1, sug −2, question −1, qflag −2, correct **+3**; nofit→opt **+8** (8→16/97), nofit→oos +4, nofit→qst +3 | no |

What the log shows:
- **c4:** all 5 extra defect hits are on low-confidence gold rows. HIGH defect recall stays at 15/16. Train HIGH blocker FP is unchanged at 2.
- **c2–c4 FPs:** the persistent dev FPs are a bot mutation-testing report, a remark that a test cannot detect a thrown exception, and one gold-question row that gold also marks `is_defect`.
- **o2 on train HIGH:** blocker F1 0.909→0.970, optimization F1 0.750→0.909, defect recall 15/16→16/16.
- **o2 on secondary checks:** irr type agreement 28/41, the same as baseline.
- **o2 under the amendment's named guards only** (nofit→blocker or question): it fails only on nofit→question +3, which every edit produced (+2 or +3) while a rerun produced 0. But the +8 nofit→optimization is a clear new systematic label: about 8% of gold-nofit rows become optimization. That alone justifies rejecting it.

## 5. Worst remaining failures (baseline, train) and suspected gold errors

High-confidence gold rows:

| id | gold → pred | reason (paraphrase) |
|---|---|---|
| THREAD:DEMO/widget#10506:3529785 | blocker → suggestion | bot finding of wrong path-prefix resolution, written as an analysis |
| THREAD:DEMO/widget#3141:1232703 | optimization → suggestion | proposal to remove a branch from an inner loop, with code |
| THREAD:DEMO/widget#10592:3594797 | optimization → suggestion | terse note on a heavy API used in a hot loop |
| THREAD:DEMO/widget#10488:3535750 | suggestion → blocker | bot finding about missing type filtering, read as a defect |
| THREAD:DEMO/widget#3391:1278519 | nit → blocker | stale doc comment read as a behaviour defect |
| THREAD:DEMO/widget#10224:3303359 | out_of_scope → suggestion | the deferral cue is missed |
| THREAD:DEMO/widget#4967:1535183 | out_of_scope → question | the deferral is phrased as a question |
| THREAD:DEMO/widget#3310:1262159 | question → nit | short information ask read as a style remark |
| THREAD:DEMO/widget#6431:1854688, #4898:1519975, #4752:1506951 | nit → suggestion | small cosmetic requests labelled suggestion |

Low-confidence gold defects missed (the hedged or question-form defect cluster):

| id | gold → pred | reason (paraphrase) |
|---|---|---|
| THREAD:DEMO/widget#4140:1401670 | blocker → suggestion | empty-input guard asked for, with the crash consequence named |
| THREAD:DEMO/widget#1885:998609 | blocker → nit | an IDE null warning is reported |
| THREAD:DEMO/widget#8085:2361553 | blocker → nit | a contract method was dropped |
| THREAD:DEMO/widget#6495:1874462 | blocker → suggestion | wrong time source for a duration |
| THREAD:DEMO/widget#2439:1110325 | blocker → suggestion | hedged note that a type is unhandled |
| THREAD:DEMO/widget#3725:1333421 | blocker → question | a wrong format pattern is asked about |

Suspected gold errors (review after the loop; gold was not modified):

| id | note |
|---|---|
| THREAD:DEMO/widget#5870:1718490 | gold question with is_defect=true (an acknowledged race window); the type and defect flag are inconsistent |
| THREAD:DEMO/widget#3381:1275719 | gold question with is_defect=true; the same tension |
| THREAD:DEMO/widget#6142:1780927 | gold suggestion with is_defect=true (a counter leak the reviewer calls minor) |
| THREAD:DEMO/widget#9418:2911569 | gold question; the text points out an injection risk, so blocker is arguable |
| THREAD:DEMO/widget#10357:3468009 | gold suggestion; a bot test-quality report, arguably nofit |
| THREAD:DEMO/widget#7067:2033838 | gold nofit high; reads like a reviewer's logic argument rather than a notice |

## 6. Recommendation

**Keep v3.** No single edit clears the row-count gates. The blocker misses are almost entirely low-confidence gold, where the second rater agrees on only 5/8 rows. Prompt edits churn about 15–20% of labels, mostly gold-nofit rows. o2 (optimization) is the only edit worth another round, and it needs an extra rule that keeps notices and author notes off optimization. A better route is a deterministic nofit/notice filter in code: then o2 could be re-screened on dev and train, with the untouched holdout still available to confirm it.

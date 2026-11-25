# v3 audit — model v3 vs the merged v1/v2 store

> Local, internal. Thread ids and paraphrases only. Rater: one LLM reviewer (Claude),
> text-only, against the closed vocabulary. Neither label set is treated as truth.

## Verdict

- **Mixed, and v3 should not be imported whole.** It wins clearly on its
  question rule, with requests phrased as questions now labelled `suggestion`.
  It also wins on `withdrawn`, and moderately on style. It **over-corrects
  blocker and optimization**: real code defects become `nit`, `suggestion` or
  `question`, and genuine cost remarks lose `optimization`.
- **Comment type, post-guard sample B:**
  - Decisive rows: **v3 better 65, merge better 61** (52%, Wilson 95%
    43–60%).
  - Volume-weighted: **v3 ≈ 375 vs merge ≈ 258** threads.
  - Almost all of the v3 margin comes from `question→suggestion` (339 threads,
    10:1).
  - v3 loses 31:9 on the blocker transitions
    (`blocker→suggestion/nit/question/out_of_scope`, `optimization→blocker`)
    and 15:2 on `optimization→suggestion/nit`.
- **Import:** a label-level merge. Take v3 for the question rule, `question→nit`
  and `withdrawn`. Keep the store for blocker, optimization and `answered`.
  Details under *Recommendation*. No v4 is needed for these classes. If
  `blocker` recall matters, a v4 on blocker alone is the next step.
- **Confidence: medium-low per transition** (n ≤ 20).
  - 119 of 302 rows (39%) are no-fit text: rebase and status notes, review
    requests, bot commands, author explanations. These dominate
    `nit→out_of_scope` (20/20), `out_of_scope→suggestion`, `nit→suggestion`
    and `optimization→nit` (14/20 each). Those transitions mostly shuffle text
    no class fits; they are noise, not wins.
  - One rater, one pass.

## Inputs and how they were checked

- **The given sample `v3-audit.jsonl` (A):** 326 rows. `new`, `labels` and
  `text` match the v3 trace and `old` matches the merge patch, all 326/326.
- **The merge patch follows the agreed rule on every non-mandatory thread:**
  v1 type, except v2 `out_of_scope` over v1 `suggestion`/`question`.
  0 mismatches in 7,509. Its manifest stamps every row
  `classifier_version: ollama-v2`, although `comment_type` is mostly v1.
  That is a provenance nit.
- **Sample A has a structural problem.** It compares the v3 **trace**, which
  is pre-guard, with the merge **patch**, which is post-guard.
  - 460 of its 1,828 "changes" are DC-mandatory threads. The merge forces
    them to `blocker` and the v3 patch forces them to `blocker` again, so
    nothing changes in the store.
  - 58 of A's 326 rows are such threads. They fill exactly the transitions
    the brief asks about: 16 of 20 `blocker→question` rows, 16/20
    `blocker→out_of_scope`, 9/20 `blocker→nit`, 8/20 `blocker→suggestion`,
    9/11 `blocker→optimization`.
- **Sample B, which I built:** merge patch → v3 **patch**, both post-guard.
  The selection rule is the same as `audit_hypotheses.py sample` (group by
  old→new, sort by thread id, first 20). 1,368 real changes, 302 rows, 0
  mandatory.
  - B equals A's 268 non-mandatory rows plus 34 rows that refill the blocker
    transitions.
  - The sampler file and the raw-text sample stay in the session scratchpad;
    nothing was written to the repo or the store.
- **Corpus probes** run over the 7,509 non-mandatory threads that the store
  and the v3 run share.
- **One v3 call fell back to the mock:** `THREAD:DEMO/widget#8935:2677711`.

## Sample A vs sample B — how much the comparison basis matters

| | sample A (as produced) | sample B (post-guard) |
|---|---|---|
| decisive v3 : merge | 106 : 51 (**68%**, 95% 60–74%) | 65 : 61 (**52%**, 43–60%) |
| volume-weighted threads v3 : merge | ≈ 675 : 308 | ≈ 375 : 258 |
| `blocker→suggestion` | 10 : 4 (pool 311) | 7 : 6 (pool 92) |
| `blocker→nit` | 10 : 3 (pool 164) | **3 : 7** (pool 62) |
| `blocker→question` | 12 : 3 (pool 97) | **0 : 7** (pool 14) |
| `blocker→out_of_scope` | 14 : 3 (pool 58) | 2 : 3 (pool 11) |

- **The mandatory rows are judged on their text.** Only 2 of 58 read as
  defects. Of the rest, 46 have a v3 label that fits the text, 8 are wrong
  and 2 are unclear. That is correct as a reading of the text, but none of it
  reaches the store.
- **A would have reversed the blocker verdict.** It credits v3 with fixing
  blocker de-escalation, when on real store changes v3 makes it worse.
- **Sampler fix (a code change, so with a regression test):** take `new` from
  the new run's **patch**, or drop mandatory threads, whenever the baseline is
  a post-guard patch.

## Per transition (sample B, post-guard; volume order)

| old (store) → new (v3) | changed | n | v3 better | merge better | both wrong | no-fit | unclear |
|---|---:|---:|---:|---:|---:|---:|---:|
| question → suggestion | 339 | 20 | **10** | 1 | 1 | 7 | 1 |
| suggestion → nit | 274 | 20 | 6 | 4 | 1 | 6 | 3 |
| optimization → suggestion | 153 | 20 | 1 | **10** | 0 | 4 | 5 |
| out_of_scope → suggestion | 106 | 20 | 2 | 2 | 0 | 14 | 2 |
| nit → suggestion | 98 | 20 | 1 | **4** | 0 | 14 | 1 |
| blocker → suggestion | 92 | 20 | 7 | 6 | 0 | 3 | 4 |
| blocker → nit | 62 | 20 | 3 | **7** | 3 | 2 | 5 |
| out_of_scope → nit | 57 | 20 | 6 | 0 | 0 | 13 | 1 |
| question → nit | 40 | 20 | **12** | 2 | 0 | 3 | 3 |
| optimization → nit | 39 | 20 | 1 | 1 | 0 | 14 | 4 |
| nit → out_of_scope | 26 | 20 | 0 | 0 | 0 | 20 | 0 |
| blocker → question | 14 | 14 | 0 | **7** | 2 | 0 | 5 |
| blocker → out_of_scope | 11 | 11 | 2 | 3 | 0 | 5 | 1 |
| optimization → blocker | 10 | 10 | 1 | **6** | 1 | 0 | 2 |
| suggestion → out_of_scope | 8 | 8 | 2 | 1 | 0 | 5 | 0 |
| 11 small transitions (≤6 each) | 39 | 39 | 11 | 7 | 2 | 9 | 10 |
| **total** | 1,368 | 302 | **65** | **61** | 10 | 119 | 47 |

`both ok` was 0, because every sampled row changed and in none of them were
both labels defensible.

## The classes v3 targeted

- **question (rule 1): fixed for the type, not for the flag.**
  - `question→suggestion` is 10:1. "Should we call X?", "could also include
    Y?", "check use of Z" are now suggestions (e.g.
    `THREAD:DEMO/widget#10047:3173276`, `THREAD:DEMO/widget#10188:3277982`).
  - Probe over 387 openers starting "can/could/should we", "let's" or
    "please": `comment_type=question` falls from 122 in the merge (v2: 141)
    to **42**.
  - **The `question` flag barely follows.** The same probe still has 170
    flagged, against 181 in the merge. v3 has **289 `suggestion` openers with
    `question=true`**, against 185 in v1 and 140 in v2. Rule 1 says requests
    are `question=false`, so the type and the flag are now inconsistent.
  - **Some over-correction:**
    - A real information ask ("what would you suggest instead?") became
      `suggestion`.
    - A request carrying a defect became `suggestion`, though both labels
      miss the defect (`THREAD:DEMO/widget#10281:3334218`).
    - Genuine wh-questions keep `question=true` in 573 of 689 cases. The
      merge has 624.
  - **The reverse direction still fails:** `suggestion→question` is 0:3
    ("should be mapped to X?", a log-and-rethrow remark,
    `THREAD:DEMO/widget#10238:3294246`, `THREAD:DEMO/widget#6404:1856519`).
  - **Bot commands are now better handled:** 7 of 30 are flagged as
    questions, against 13 in the merge.
- **blocker (rule 2): over-corrected.**
  - **Distribution:** `blocker` falls from 8.3% to **6.3%** (625→472).
  - **Defect probes** ("tests failing", NPE, "doesn't work", "is incorrect",
    "wrong"): 59→**40** blocker.
  - **On real defects v3 loses:**
    - `blocker→nit` 3:7. Code defects are now `nit`: wrong id referenced,
      wrong response returned, an IDE-flagged NPE, a build-plugin conflict,
      `ThreadLocal.remove()` doing nothing, "tests failing".
      Examples: `THREAD:DEMO/widget#10157:3238178`, `THREAD:DEMO/widget#1885:998609`,
      `THREAD:DEMO/widget#2884:1189583`, `THREAD:DEMO/widget#3478:1294827`.
    - `blocker→question` 0:7: wrong status codes, an unhandled NaN, an
      unclosed buffer (`THREAD:DEMO/widget#8987:2743702`), and random bytes
      left in a buffer.
    - `blocker→suggestion` 7:6: must catch a different exception, socket bind
      conflict, "getValue is incorrect"
      (`THREAD:DEMO/widget#10042:3169156`, `THREAD:DEMO/widget#2080:1063745`).
  - **On the leak, close, race and security probe** (72 openers), blocker is
    25→22. The rule's "security/race/resource-close are blockers" did not
    take.
  - **Docs, naming and test gaps are no longer blockers, as intended.** On the
    misleading-doc/naming probe, blocker is 7→6. On missing tests/coverage,
    21→10, with nit 3→10. Per the rule those should be `suggestion`, not
    `nit`. The test-gap rows in `blocker→nit` are 3 both wrong.
  - **New escalation is decent:** `nit→blocker` 3:0, `question→blocker` 2:0
    (2 both wrong), `suggestion→blocker` 2:1. Examples: "this is always false
    now", retries/tries mixed up, missing Azure conf
    (`THREAD:DEMO/widget#4582:1470427`, `THREAD:DEMO/widget#9432:2898085`,
    `THREAD:DEMO/widget#10682:3674038`).
  - **One rule self-contradiction:** a request for a reproducing test became
    `blocker` (`THREAD:DEMO/widget#10725:3695285`).
- **nit (rule 3): fixed.** Openers that start with `nit:` are 519/534 `nit`,
  against the merge's 517 and v2's 469.
  - `question→nit` is 12:2. These are naming and doc remarks phrased as
    questions, plus struck-through, self-answered questions.
  - `suggestion→nit` is 6:4 on real text. The losses are behavioural changes
    labelled `nit`: a null guard, a default value that changes behaviour, a
    missing cancellation check (`THREAD:DEMO/widget#10682:3768832`).
- **optimization (rule 4): still too strict.** "Benchmarks included" did not
  take.
  - `optimization` is 5.7%→**3.2%** (429→238).
  - `optimization→suggestion` is 1:10. Lost cost remarks include "would save
    resources", "costly and done twice", "avoid re-creating the object" and
    "Stream API in a hot loop" (`THREAD:DEMO/widget#10123:3236514`,
    `THREAD:DEMO/widget#10344:3393733`, `THREAD:DEMO/widget#10527:3547485`,
    `THREAD:DEMO/widget#10592:3594797`).
  - `optimization→blocker` is 1:6. Memory-twice, a lost limit pushdown and
    heap measurements became blockers (`THREAD:DEMO/widget#10682:3698158`,
    `THREAD:DEMO/widget#10921:3795485`, `THREAD:DEMO/widget#8738:2598713`).
  - On the perf-word probe (325 openers), optimization is 163→122.
- **out_of_scope: v2's gains are kept, roughly.**
  - Deferral probe (127): out_of_scope 53 in the merge, 48 in v3.
  - `suggestion→out_of_scope` is 2:1; "off-topic" and "follow-up PR" are
    right.
  - `out_of_scope→blocker` still fires on a remark that calls itself out of scope while listing bugs (`THREAD:DEMO/widget#9139:2798673`).
  - `out_of_scope` share 6.9%→5.4%. The loss is mostly no-fit text moving to
    `suggestion`/`nit`.
- **intent (rule 5): `withdrawn` recovered, `answered` partly fixed.**
  - On the 46 "nvm / never mind / ignore that" openers, `withdrawn` is 38 in
    v3, against 29 in the merge (v1) and 8 in v2 (e.g.
    `THREAD:DEMO/widget#2339:1088841`, `THREAD:DEMO/widget#2391:1110269`).
  - `answered=true` is 37, down from v2's 154 but still not 0 as the rule
    asks; v1 has 19.
  - `acted` 223→76 and `rejected` 13→11. Neither was rated against text in
    this sample.

## Distribution (7,509 non-mandatory shared threads)

| class | v1 | v2 | merge (store) | v3 |
|---|---:|---:|---:|---:|
| suggestion | 37.3% | 43.2% | 36.8% | 43.4% |
| question | 20.4% | 21.3% | 20.3% | **15.4%** |
| nit | 21.9% | 16.6% | 21.9% | **26.4%** |
| blocker | 8.3% | 8.8% | 8.3% | **6.3%** |
| out_of_scope | 6.4% | 6.5% | 6.9% | 5.4% |
| optimization | 5.7% | 3.6% | 5.7% | **3.2%** |
| `question=true` | 1,808 | 1,799 | 1,808 | 1,468 |
| intent acted / rejected / withdrawn | 713 / 326 / 65 | 232 / 13 / 8 | 223 / 13 / 65 | 76 / 11 / 48 |

- **The question drop is the intended fix.**
- **The prompt bugs are:**
  - the nit rise beyond the `question→nit` wins (defects moving to `nit`);
  - the blocker drop;
  - optimization staying at v2's low level.
- **On the mandatory threads**, v3's raw model says `blocker` for 67 of 527,
  against 92 for both v1 and v2. That is consistent with the general
  de-escalation; the guard fixes it in the store.

## New failure patterns

| pattern | evidence | suggested fix |
|---|---|---|
| **Defect reports softened to nit or question** | `blocker→nit` 3:7, `blocker→question` 0:7; defect probe 59→40 | The "not a blocker: docs/naming/tests" carve-out is generalising. Add positive examples ("returns the wrong value", "the IDE reports a null dereference here", "X is not closed", "tests failing") → blocker, next to the carve-out. |
| **Interrogative defects lose blocker** | "are the remaining bytes left uninitialised?", "will this buffer be closed?" → question | State that a question pointing at a concrete bug is a blocker with `question=false`. |
| **Type and flag decoupled** | 289 `suggestion` with `question=true` | Derive the flag in code: `question=false` when the request rule fires, or when `comment_type != question` and the text has no `?` outside code and URLs. |
| **Perf remarks labelled blocker or suggestion** | `optimization→suggestion` 1:10, `optimization→blocker` 1:6 | Add 2–3 optimization few-shots ("X is costly and done twice", "allocated over 1G heap", benchmark table) and say a performance regression is optimization unless results are wrong. |
| **Test gaps labelled nit** | missing-test probe nit 3→10 | Add a few-shot: "no test for X" → suggestion. |
| **`answered` still set on openers** | 37 | Force false for `role=opener` in code. |
| **Sampler compares pre-guard vs post-guard** | 460 phantom changes | Fix `audit_hypotheses.py sample`, with a test. |

## Recommendation

1. **Don't import v3 whole.** Run no v4 for question, nit or withdrawn; v3
   already fixes those.
2. **Label-level merge onto the current store**, with the DC guard, no
   outcome changes, and the same skip set:
   - **comment_type:** take v3 only where the store says `question`
     (`question→suggestion` 10:1, `question→nit` 12:2; about 380 threads).
     Keep the store everywhere else. In particular, keep blocker and
     optimization, and keep `nit→suggestion`.
   - **question flag:** on the threads whose type is taken from v3, take v3's
     flag too. Elsewhere keep the store. Do not import v3's flag globally,
     because it is inconsistent with its own types.
   - **thread_intent:** take v3's `withdrawn` (38/46 on the nvm probe, against
     29). Keep the store's `acted`/`rejected` until those are rated.
   - **answered:** keep the store (v1), or set false for openers.
   - **Optional:** take v3 `suggestion→nit` (6:4, low confidence) only where
     the text starts with `nit` or is a naming or doc remark.
3. **If blocker recall matters, run a v4 on blocker only.** Use v3's prompt
   with the carve-out narrowed and the positive defect, resource and
   interrogative examples above, plus the optimization few-shots. Re-audit
   only the `blocker↔*` and `optimization↔*` transitions, on a post-guard
   sample.
4. **Fix the sampler before the next audit** and record the
   `classifier_version` per label source in the merge manifest.

## Method notes

- 302 sample-B rows plus 58 mandatory rows were rated once, from the text,
  with both labels visible. Sample A was then scored from the same per-row
  ratings, so the A/B difference comes only from the comparison basis.
- Mandatory rows in A count as "v3 better" when v3's label fits the text and
  the text does not read as a defect.
- Volume weighting multiplies each transition's sampled rates by its number of
  changed threads.
- Probes are regex counts and are indicative only.
- Scripts and the raw-text sample B are in the session scratchpad:
  `<WORK>\`.
  They print ids and counts; only `v3-audit-B.jsonl` holds text.

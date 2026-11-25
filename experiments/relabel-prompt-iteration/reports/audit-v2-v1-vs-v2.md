# v2 audit — model v2 vs v1

> Local, internal. Thread ids and paraphrases only. Rater: one LLM reviewer (Claude),
> text-only, against the closed vocabulary. v1 is not treated as truth.

## Verdict

- **Mixed overall, and worse on `comment_type` by volume.** In the 352-row
  v1→v2 sample, 156 rows were decisive: **v2 better 81, v1 better 75**, about
  52%, Wilson 95% ≈ 44–60%. Weighted by how many threads each transition
  covers, the 19 largest transitions (1,056 of the 1,085 changes) come out at
  roughly **v2 better ≈ 183 vs v1 better ≈ 278** threads.
- **Where v2 wins:**
  - `thread_intent`: it stops guessing `rejected`/`acted` from the opener.
  - `out_of_scope` usage.
  - Escalating some real defects that v1 had as questions.
- **Where v2 loses:**
  - It reverses v1's best fix, so "should we / can we …?" suggestions go back
    to `question`.
  - It ignores explicit `nit:` prefixes.
  - It de-escalates real defects, including a credential-leak remark.
  - `optimization` now misses genuine cost remarks as often as it fixes
    over-reads.
  - `withdrawn` collapses.
  - `answered` jumps sevenfold.
- **Import:** don't import v2 as a whole. Merge at the label level: v1
  `comment_type`/`question`/`answered`, plus v2 `thread_intent` with v1's
  `withdrawn` kept. Use v2's deterministic skip set, which is the biggest
  real improvement. Details under *Recommendation*.
- **Confidence: medium-low per transition** (n = 10–20).
  - 98 of 352 rows (28%) are no-fit text: author status notes, rebase notices,
    review requests, bot commands. No class fits them, so they show neither
    model as better.
  - The no-fit share is highest in exactly the high-volume transitions
    (`nit→suggestion`, `nit→out_of_scope`, `out_of_scope→suggestion`,
    `question→suggestion`).
  - One rater, no second model pass.

## Inputs (verified)

- **`v2-audit-v1v2.jsonl`:** 352 rows, all verified against both traces.
  `old` and `v1_labels` match the v1 trace, and `new`, `labels` and `text`
  match the v2 trace, on 352/352 rows. No footer, so the counts come from the
  traces.
- **The earlier `v2-audit.jsonl` was not used.** Its `old` was the mock and its
  intent and other labels were v1's, as you confirmed.
- **v1→v2 comment_type changes: 1,085** on the 8,036 threads in both traces
  (13.5%).
- **v2 skipped 2,506 of v1's 10,542 openers.** 2,277 of them belong to the
  coverage/static-analysis bot pseudonym. Text is identical between v1 and v2 on all
  8,036 shared threads.
- **The patches agree.** `hans-relabel-v2.tgz` and `.clean.tgz` carry the same
  labels on these 8,036 threads.
  - Their 435 differences from the v2 trace are all DC-mandatory threads forced
    to `blocker`, so the guard works.
  - `.tgz` has 112 extra thread rows, from the separate 112-call trace, all by
    human pseudonyms. Those rows are **not audited**.
- **One v2 call fell back to the mock** after a timeout:
  `THREAD:DEMO/widget#8935:2677740`, `label_version mock-v1`.
- **The DC-mandatory threads in the sample (29) were judged on text only.**

## Per transition

"no-fit" is text that no vocabulary class fits. It is listed separately rather
than as `both wrong`, so it doesn't hide the decisive counts. Transitions are in
volume order.

| old → new | changed | n | v2 better | v1 better | both ok | both wrong | no-fit | unclear |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| nit → suggestion | 376 | 20 | 1 | **5** | 3 | 0 | 9 | 2 |
| optimization → suggestion | 127 | 20 | 9 | 9 | 0 | 0 | 0 | 2 |
| suggestion → question | 110 | 20 | 0 | **10** | 3 | 2 | 1 | 4 |
| out_of_scope → suggestion | 68 | 20 | **5** | 0 | 0 | 1 | 12 | 2 |
| question → suggestion | 50 | 20 | **5** | 1 | 0 | 0 | 12 | 2 |
| nit → blocker | 47 | 20 | 7 | 6 | 0 | 2 | 0 | 5 |
| nit → out_of_scope | 47 | 20 | 1 | 3 | 0 | 1 | 15 | 0 |
| blocker → suggestion | 40 | 20 | 4 | **7** | 0 | 0 | 2 | 7 |
| suggestion → out_of_scope | 40 | 20 | **7** | 1 | 1 | 0 | 6 | 5 |
| suggestion → blocker | 28 | 20 | **8** | 5 | 0 | 0 | 1 | 6 |
| question → blocker | 17 | 17 | **6** | 2 | 0 | 1 | 1 | 7 |
| optimization → blocker | 17 | 17 | 4 | 5 | 0 | 0 | 3 | 5 |
| blocker → nit | 16 | 16 | 2 | 2 | 0 | **8** | 3 | 1 |
| out_of_scope → nit | 15 | 15 | 4 | 0 | 0 | 1 | 10 | 0 |
| suggestion → nit | 14 | 14 | 4 | 3 | 0 | 0 | 7 | 0 |
| blocker → question | 13 | 13 | 2 | **4** | 0 | 1 | 2 | 4 |
| optimization → question | 11 | 11 | 1 | 2 | 3 | 1 | 2 | 2 |
| nit → question | 10 | 10 | 1 | **5** | 2 | 0 | 1 | 1 |
| optimization → nit | 10 | 10 | 2 | 2 | 0 | 0 | 3 | 3 |
| 8 small transitions (≤6 each) | 29 | 29 | 8 | 3 | 0 | 7 | 8 | 3 |
| **total** | 1,085 | 352 | **81** | **75** | 12 | 25 | 98 | 61 |

Some transitions are noise, as the brief anticipated. `nit→out_of_scope`
(15/20 no-fit), `out_of_scope→nit` (10/15) and most of `nit→suggestion` and
`out_of_scope→suggestion` are the same rebase notices, review requests and
"see link" notes moving between neighbouring classes. They are neither wins nor
losses. The comment_type verdict rests on the transitions with real review text:
`suggestion→question`, `blocker→suggestion`, the `→blocker` rows, `optimization`
and `nit→question`.

## The classes v2 targeted

- **Blocker escalation: partly works, but over-escalates and de-escalates.**
  - **Real defects escalated:** an off-by-one, a lost increment, a type
    mismatch, "flush twice loses results", a stale factory, "a decade should
    truncate to 2010", "this is always false now".
    - `question→blocker` 6:2, `suggestion→blocker` 8:5, `nit→blocker` 7:6.
    - Examples: `THREAD:DEMO/widget#10592:3594771`,
      `THREAD:DEMO/widget#2224:1071672`, `THREAD:DEMO/widget#3141:1237443`,
      `THREAD:DEMO/widget#4582:1470427`, `THREAD:DEMO/widget#4897:1518647`.
  - **Over-escalation:** misleading Javadoc, a misnamed method, a missing
    `@Nullable`, a wrong test name. The "statement that the code is wrong" rule
    is applied to *documentation* being wrong. That is 6 of the 20
    `nit→blocker` rows.
  - **De-escalation of real defects:** `blocker→suggestion` 4:7.
    - A remark that an input must be validated before building a URL, or credentials could leak to a foreign host, became a suggestion (`THREAD:DEMO/widget#10682:3674137`).
    - So did a missing `synchronized`, a closed-but-non-null builder reused
      later, and a remark that the change would make the query fail.
    - `blocker→question` 2:4: the leak and close remarks became questions.
  - **Test gaps are both wrong:** in `blocker→nit` (8 both wrong) "missing test
    / no coverage" was `blocker` in v1 and is `nit` in v2. It should be
    `suggestion`.
  - **Corpus figures:** leak and close remarks are 20→17 `blocker`; "tests
    failing" remarks are 27→24.
- **optimization: cost-only overshoots.**
  - The share is 445→282 (−37%).
  - `optimization→suggestion` is 9:9. v2 correctly drops non-cost technical
    remarks (prefilter accuracy, unreachable code, ASM frames). It wrongly
    drops remarks about a small speed-up, avoiding an object re-creation, paying a sort only once, and a loop that runs per record.
  - `optimization→blocker` is 4:5. Benchmark dumps, a responses-held-twice memory remark and an n² remark lost `optimization`.
  - On a perf-vocabulary probe over 272 texts, `optimization` fell 152→124.
- **out_of_scope: now used for deferrals, not disagreement.**
  - `suggestion→out_of_scope` is 7:1: a needed follow-up in another service, a removal planned for a separate PR, a remark flagged as unrelated to the ticket, and one flagged as outside the bugfix's scope.
  - `out_of_scope→suggestion` is 5:0 on real text.
  - One miss: a remark that calls itself out of scope while listing bugs became
    `blocker`.
  - The `rejected` coupling is gone. 346 `rejected→unclear` intent changes, and
    `rejected` is 13 corpus-wide.
- **question: v2 undoes v1's main win.**
  - `suggestion→question` is **0:10**. Requests to copy less, to store extra state, to declare an exception and to return a narrower type are
    requests, not information asks.
  - The v2 definition ("asks for information… not an imperative like
    rename/check") seems to have taught the model that any interrogative which
    is not an imperative is a question. Probes: "should we/this …?" is
    `question` in 61/84 (v1 51), and "can we …?" in 29/96 (v1 19).
  - `nit→question` is 1:5.
  - Good removals are kept: `question→suggestion` 5:1 on real text. It also
    fixes v1's bot-command questions, since 12/20 rows there are review
    requests now correctly `question=false`.
  - **Bot commands remain inconsistent:** 29 `@bot` commands are still in the
    v2 trace, so the skip was incomplete, and **9 are still `question`**. That
    breaks v2's own rule (e.g. `THREAD:DEMO/widget#10921:3798358`, `THREAD:DEMO/widget#10517:3539517`).
  - **Net flag changes:** 116 v1→v2 adds (34 have no `?` outside URLs) and 128
    removals.
- **intent: better on guessing, but `withdrawn` collapsed.**
  - The guesses are gone: `acted` 728→237 and `rejected` 361→13, mostly to
    `unclear`, which is correct for openers.
  - Of 15 sampled v2 `acted`, about 12 are text-supported: the author says
    added, fixed, renamed or rebased. Unsupported:
    - a note that someone will try another approach (`THREAD:DEMO/widget#10441:3488408`);
    - a closing note that moves the fix elsewhere (`THREAD:DEMO/widget#6057:1766325`);
    - an explicit refusal booked as `acted` (`THREAD:DEMO/widget#10752:3735311`).
  - Of the 13 `rejected`: about 7 are fine (decline notices). Wrong ones include
    benchmark results, a PR problem statement and a platform-specific test-failure report
    (`THREAD:DEMO/widget#6144:1804396`, `THREAD:DEMO/widget#9329:2837755`,
    `THREAD:DEMO/widget#9001:2700419`).
  - **Withdrawn collapsed.** On 48 openers with an explicit "never mind" or
    "nvm", v1 said `withdrawn` 29 times and v2 says it 8 times (31 `unclear`,
    9 `acted`). That is exactly the case where "the text itself states the
    outcome". Example: a struck-out question then "nvm, it's handled" became
    `acted` (`THREAD:DEMO/widget#3765:1339751`).

## Distribution (same 8,036 threads)

| class | v1 | v2 | change |
|---|---:|---:|---:|
| suggestion | 2,978 | 3,445 | +16% |
| question | 1,639 | 1,712 | +4% |
| nit | 1,722 | 1,300 | **−25%** |
| blocker | 717 | 756 | +5% |
| out_of_scope | 535 | 541 | 0% |
| optimization | 445 | 282 | **−37%** |
| intent acted / rejected / withdrawn | 728 / 361 / 68 | 237 / 13 / 8 | −67% / −96% / **−88%** |
| answered = true | 20 | **155** | ×7.8 |

These are prompt effects, not corpus facts:
- the `optimization` drop (an over-strict cost rule);
- part of the `nit` drop (explicit `nit:` prefixes fell from 526 to 477 `nit`);
- the `withdrawn` collapse;
- the `answered` explosion.

The `rejected` and `acted` drops are the intended fix.

## New failure patterns

| pattern | evidence | suggested fix |
|---|---|---|
| **Interrogative requests become questions again** | `suggestion→question` 0:10; "should we/can we …?" probes up | Restore v1's question wording. Add an example: "can we also show X in the message?" → suggestion, question=false. |
| **`answered` coupled to `acted`** | 155 true, 151 of them `acted`. These are author status notes on openers (fix and rebase notes), and one is a self-answer to a struck-out question. None answers someone else's question. | Openers should never set `answered`. Force it false for `role=opener`, or drop the field from the opener call. The v1 few-shot "good catch, fixed" → answered=true is the likely source. |
| **Explicit `nit:` ignored** | 59 of 544 openers starting with "nit" are now `suggestion` (v1: 13) | Add a rule that an explicit `nit`/`ubernit` prefix is `nit` unless the text describes a defect. |
| **Doc or naming "is wrong" escalated to blocker** | 6/20 `nit→blocker` | Narrow the blocker rule to *code* behaviour. A misleading comment, Javadoc or name is `nit`. |
| **Real defects de-escalated** | `blocker→suggestion` 4:7, including a credential-leak remark; leak/close probe 20→17 | Add examples for security, concurrency and resource-close remarks → blocker. The cost-only and imperative rules seem to pull hedged defect remarks (hedged "let's" or "I suspect" phrasing) down to suggestion. |
| **Cost-only too strict** | `optimization→suggestion` 9:9, `optimization→blocker` 4:5 | Wording: "optimization when the remark is about speed, memory or allocations; not when performance is only mentioned in passing." Benchmark results are optimization. |
| **Test gaps** | `blocker→nit` 8 both wrong | Add an example: "missing test / no coverage" → suggestion. |
| **Withdrawn missed** | explicit "nvm/never mind": `withdrawn` 29→8 | Add an example: struck-out remark plus "nvm" → withdrawn. |
| **Skip incomplete** | 29 `@bot` commands still labelled, 9 as question | Extend the deterministic skip to `@"svc-…"` and agent handles at the start of a line, or enforce the prompt's own @handle rule in code. |
| **Fallback** | 1 timeout → mock label in the v2 trace | Retry, or exclude it from the patch. |

## Recommendation

1. **Don't import v2 as a whole, and don't run a full third pass yet.** v2's
   gains are in `thread_intent` and `out_of_scope`. Its losses are in
   `question` and `blocker`, which are the labels the analyses rely on most.
2. **Import a merge.** This needs no model run. On v2's deterministic skip set,
   with the v0.19.1 DC guard and outcome changes held back as decided in the
   v1 audit:
   - **comment_type:** v1, except take v2's `out_of_scope` when v1 had
     `suggestion` or `question` (`suggestion→out_of_scope` was 7:1).
   - **question:** v1.
   - **answered:** v1.
   - **thread_intent:** v2, but keep v1's `withdrawn`.
3. **If a third run is wanted, limit it to `blocker`.** Use the v1 prompt plus
   v2's intent wording, a code-only blocker rule (docs, naming and test gaps
   excluded), explicit security, concurrency and close examples, and
   `answered=false` for openers. Re-audit only `→blocker` / `blocker→`
   transitions and the "should we/can we" probe.
4. **Patch hygiene:**
   - Pick `.clean.tgz` or `.tgz` explicitly; the 112 extra rows in `.tgz` are
     unaudited.
   - Fix the skip so it covers the 29 leftover bot commands.
   - Retry the one timeout fallback.

## Method notes

- One pass over all 352 rows. Each verdict comes from the text alone, with the
  v1 and v2 labels side by side.
- Corpus probes are regex counts over the 8,036 shared threads, and they are
  indicative only.
- The volume-weighted figure scales each transition's sampled v2/v1 rates by
  the number of changed threads in it.
- Scripts and outputs are in `<WORK>`;
  they print ids and counts, never text.

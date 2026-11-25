# Relabel audit — model vs mock (`hans-relabel-202611222146.tgz`)

> Local, internal. No raw review text below: thread/PR ids and paraphrases only.
> Rater: one LLM reviewer (Claude), judging the text alone against the prd/16 vocabulary.

## Verdict

- **comment_type: the model is clearly better on human review text.** It is not
  better on bot/CI notices or administrative notes. In 260 rated transition
  samples, 150 were decisive: **124 model better vs 26 mock better (83%; Wilson
  95% CI ≈ 76–88%)**. The other 110 were `both_ok` 2, `both_wrong` 68 (mostly
  bots) and `unclear` 40.
- **thread_outcome: the patch is a regression. Do not apply it.** The 1,582
  outcome flips are not the model being more careful. The patch builder throws
  away outcomes that were derived from the diff (see *Outcome flips*).
- **question: mostly better.** Removing the mock's `?`-rule questions is right in
  almost every sampled case. Added questions are right about 4:1 among decisive
  cases. The 6 `PR:` adds are not.
- **apply: subset.** Apply `comment_type` and question rows for non-bot threads,
  with the v0.19.1 DC-BLOCKER guard. Do **not** apply `thread_outcome` changes.
  Then change the prompt/schema (a non-review class and no opener-only intent)
  and re-run.
- **confidence: medium.**
  - There was one rater, though a blind re-label of 20 items agreed 19/20.
  - n=20 per transition, so roughly ±20 percentage points per row.
  - The transition pools exclude the 565 DC-mandatory threads, which were
    rated separately.
  - Outcomes cannot be verified from opener text.
  - 1,343 patch thread rows have no per-text trace record and could not be
    rated (see *Inputs*).

## Inputs and verified statistics

| check | prompt says | found |
|---|---|---|
| patch rows | 11,885 thread + 2,243 question (829 add / 1,414 delete) | **same** |
| type distribution, top transitions, question flips by old type, outcome flips | as in §3 | **same, exactly** |
| DC mandatory | 565 in patch; old 565 blocker; model 116 | **same** (572 in list) |
| trace records | 11,935, 0 batches | **11,946**: 11,943 per-text + **3 batch records** (calls 1, 10, 11; call 1 timed out after 180 s) |
| thinking | null everywhere (`--no-llm-think`) | **4 per-text records had `think: true`** (calls 2–7, startup). All 4 digests were later re-labelled with think off, and the last record wins, so the patch is unaffected. The label cache holds **65** `think: true` entries (12,000 entries, 11,934 digests). |
| fallbacks / ok | 0 / all | 0 fallbacks; all 11,943 per-text calls `ok` |
| join | — | 10,542 distinct opener thread ids join their thread row, with **0** label mismatches between trace and patch. 8 thread ids appear twice (resume); the last record wins. **1,343 thread rows have no per-text trace record.** The run replayed a capture, so identical texts and cache hits were not traced per thread. 844 of these rows are `suggestion→nit` and 391 are `suggestion→question`. They are unaudited. |
| mock baseline | keyword rules | Re-running `MockClassifier` on the trace text reproduces the stored `previous.comment_type` for **9,997/9,997** non-mandatory threads. All 545 mismatches are mandatory threads whose stored `blocker` came from DC severity. |

The model only ever sees the **opener** text (`kind=comment`, no replies, diff or
severity). That limits both labellers for `thread_intent`.

## Per-label / per-transition

Sample: deterministic, taking the lowest `sha256(thread_id)` in each pool, with
no cherry-picking. "no-fit" means a text that no vocabulary class fits: a bot/CI
notice, "rebased", a bare link, "added reviewer X", "LGTM", an emoji. It is
counted as `both_wrong`, because the mock's default `suggestion` is just as wrong
for it. Pool sizes cover joined, non-mandatory threads.

| transition | pool | n | model better | mock better | both ok | both wrong (no-fit) | unclear |
|---|---:|---:|---:|---:|---:|---:|---:|
| suggestion→nit (all) | 1,930 | 20 | 1 | 0 | 1 | 12 (12 bot) | 6 |
| suggestion→nit (**bots excluded**) | 799 | 20 | 7 | 3 | 1 | 3 (2) | 6 |
| suggestion→optimization (all) | 1,100 | 20 | 3 | 0 | 0 | 17 (17 bot) | 0 |
| suggestion→optimization (**bots excluded**) | 158 | 20 | 12 | 5 | 0 | 0 | 3 |
| question→suggestion | 917 | 20 | **20** | 0 | 0 | 0 | 0 |
| suggestion→question | 221 | 20 | 8 | 4 | 0 | 3 (3) | 5 |
| suggestion→blocker | 541 | 20 | 12 | 3 | 0 | 3 (2 bot) | 2 |
| suggestion→out_of_scope (all) | 498 | 20 | 3 | 1 | 0 | 11 (11) | 5 |
| suggestion→out_of_scope (**bots excluded**) | 332 | 20 | 8 | 2 | 0 | 5 (5) | 5 |
| question→nit | 372 | 20 | 12 | 2 | 0 | 5 (5) | 1 |
| blocker→suggestion | 30 | 20 | 16 | 0 | 0 | 2 (2) | 2 |
| optimization→suggestion | 166 | 20 | 8 | 5 | 0 | 4 (1) | 3 |
| blocker→question | 28 | 20 | 14 | 1 | 0 | 3 (3) | 2 |
| **transitions total** | | 260 | **124** | **26** | 2 | 68 | 40 |
| unchanged | 3,601 | 50 | – | – | 42 | 0 | 8 |
| short texts (3–20 chars) | 353 | 30 | 6 | 1 | 11¹ | 5 (5) | 7 |
| DC mandatory (text-only) | 555 | 30 | model label reasonable for the text: **22**; wrong: 6; unclear: 2 | | | | |

¹ 8 unchanged, plus 3 mandatory threads where the model's text label is right.

Per-label reading:

- **question → suggestion (918): a clear improvement (20/20).** These are
  requests phrased as questions ("could we…?", "should this…?"). The mock calls
  them questions because of the `?`.
- **suggestion → nit (2,774): mostly noise, then a modest gain.**
  - 1,093 of the 1,930 joined rows are the coverage bot, which no label fits.
  - On human text the model is right about 7:3 among decisive cases. The
    misses are short statements of a real defect labelled `nit`: wrong units
    in a test, a branch that is "always false now", a call that "doesn't work"
    without an unwrap. Example ids: `THREAD:DEMO/widget#3746:1336097`,
    `THREAD:DEMO/widget#4582:1470427`, `THREAD:DEMO/widget#9432:2898080`.
- **suggestion → optimization (1,105): 83% coverage-bot noise.**
  - 922 of the 1,100 joined rows are coverage reports. The model reads
    coverage percentages as performance (e.g. `THREAD:DEMO/widget#10371:3428643`).
  - On human text it is right 12:5. The misses are non-perf technical remarks:
    cancellation, timeouts, GC accounting.
- **suggestion → blocker (550): finds real defects.**
  - 12/20 are genuine defects that the mock missed: a leak, a wrong return on
    cancel, an uncompilable commit, failing tests, a null-path bug.
  - It also over-escalates: 3 coverage reports at 0%
    (`THREAD:DEMO/widget#6939:1992864`, `THREAD:DEMO/widget#4637:1480399`), one
    remark explicitly deferred to a follow-up, and 3 test-quality or cleanup
    asks.
  - Human-text precision is about 12/18, or 67%.
- **blocker recall (text-only, rough).** Across every sampled text I judged a
  real defect (n≈29), the model said `blocker` on about 19 (≈65%). The misses
  became `question` (`THREAD:DEMO/widget#10074:3195848`,
  `THREAD:DEMO/widget#5539:1659053`) or `nit` (`THREAD:DEMO/widget#6495:1873209`,
  `THREAD:DEMO/widget#3886:1357464`). The keyword mock caught almost none of
  them. This is not a pass for `blocker` on its own, but it is far above the
  mock.
- **blocker → suggestion / question (non-mandatory, 30 + 28): an improvement
  (30/31 decisive).** The mock's blocker regex fires on "regression", "break"
  and "blocking" inside ordinary suggestions and questions.
- **suggestion → out_of_scope (507): half no-fit.** About half are
  administrative notes: "declined to save CI", "closing, will reopen", "added
  reviewer", "fyi", "removed comment". Real cross-PR or follow-up pointers are
  right (8:2 on human text).
- **optimization → suggestion (166): mixed (8:5).** The mock's `cache|memory`
  keywords mislabel naming remarks. The model in turn drops some genuine perf
  remarks, such as "use X to reuse memory" or "Y is slow".
- **DC mandatory.** Only about 7–11 of the 30 texts read as blockers. The rest
  are naming, doc, test-gap or question comments. DC severity cannot be
  recovered from text, so the v0.19.1 guard is required and correct. The
  model's 116/565 is a fair text reading, not a defect.

## Question flips

| flip | patch rows | n | model better | mock better | both wrong / unclear |
|---|---:|---:|---:|---:|---:|
| thread delete (mock `?` → not a question) | 1,302 | 40 mock questions (q→sugg, q→nit); the model drops 29, keeps 11 | 26 | 2 | 1 unclear (the 11 kept are `both_ok`) |
| thread add (model question; type and flag judged together) | 628 + 111 + 72 + 10 + 2 | 40 (sugg→q, blocker→q) | 22 | 5 | 13 |
| PR description delete | 67 | 28 | **28** | 0 | 0 |
| PR description add | 6 | 2 | 0 | **2** | 0 |

- **The mock over-calls questions.**
  - 382 of its 2,924 thread questions have a `?` only inside a URL
    (`?since=`, `?commentId=`, `?at=`).
  - For descriptions it is 49 of the 67 deletes.
  - The rest are requests phrased as questions.
- **The model rarely misses a genuine question.** Sampled misses: a bare "?"
  (`THREAD:DEMO/widget#3711:1331773`) and an "is this intended?" on suspicious
  code (`THREAD:DEMO/widget#6495:1873209`).
- **Where adds go wrong:**
  - Imperatives: "check X" (`THREAD:DEMO/widget#10249:3309046`).
  - Self-withdrawals: "never mind" (`THREAD:DEMO/widget#9225:2782992`,
    `THREAD:DEMO/widget#3992:1380188`).
  - Author notes, e.g. "test for the fix".
  - Commands to an AI reviewer bot ("@… review / verify / explain"), which are
    instructions, not questions to a person (`THREAD:DEMO/widget#10276:3348696`,
    `THREAD:DEMO/widget#10603:3611666`, `THREAD:DEMO/widget#8257:3214323`). 88 of
    139 such bot commands are `question`.
- **PR adds are wrong.** Both sampled adds are plain statements
  (`PR:DEMO/widget#3937`, `PR:DEMO/widget#7732`).
- **Internal consistency is fine.** Only 3 threads have
  `comment_type=question` with `question=false`. 307 have `question=true` on a
  suggestion, which is legitimate for a suggestion with a real question in it.

## Outcome flips

- **Nothing in the sampled openers says "fixed / done / addressed".**
  0 of 45 sampled `acted_on→unresolved`, `rejected→unresolved` and
  `walked_back→unresolved` openers do. As a reading of the text, the model's
  `unclear` is honest. That does not make the new outcome right.
- **The old outcomes were not text-derived.**
  - 964 of the 1,022 old `acted_on` threads have mock intent `unclear`.
  - So do 440/468 `rejected` and 70/77 `walked_back`.
- **The old store had a diff signal at scrape time.** With an `unclear`
  intent, the pre-DES-3 `thread_outcome` can only yield `rejected` or
  `walked_back` through `anchor_changed=False` plus `resolver_is_commenter`.
  Those outcomes are deterministic.
- **The bundle simply does not persist `anchor_changed`.** It became a stored
  attribute in v0.16. `ingest/relabel.py:114` (`_outcome`) calls
  `thread_outcome(anchor_changed=None, …)`, which drops to the intent rung, and
  then to `unresolved` because of DES-3.
  - The **441 `rejected→unresolved` and 73 `walked_back→unresolved` flips
    discard diff-derived facts.**
  - The 980 `acted_on→unresolved` flips mix `anchor_changed=True` (correct)
    with the old "unclear ⇒ acted_on" default (the DES-3 bias). They cannot be
    told apart from this bundle.
- **The new definite outcomes are mostly wrong.** Of 10 sampled (out of 87),
  6 are wrong: `THREAD:DEMO/widget#7111:2056265`,
  `THREAD:DEMO/widget#5384:1616061`, `THREAD:DEMO/widget#9157:2779114`,
  `THREAD:DEMO/widget#2941:1199670`, `THREAD:DEMO/widget#8912:2670229`, and praise
  booked as `acted_on` (`THREAD:DEMO/widget#9194:2774559`). The cause is the
  intent coupling described under *Error patterns*.
- **Overall intent use.** The model gives 758 acted, 522 rejected, 68 withdrawn
  and 9,194 unclear. The mock gives 454, 91, 23 and 9,974.

## Distribution

| type | mock (all) | model (all) | mock (bots excl., 7,934) | model (bots excl.) |
|---|---:|---:|---:|---:|
| suggestion | 60.6% | 25.6% | 44.3% | 37.3% |
| question | 23.6% | 17.8% | 33.4% | 20.5% |
| nit | 4.8% | **31.9%** | 7.1% | 21.5% |
| optimization | 4.7% | **11.6%** | 6.6% | 5.0% |
| blocker | 5.7% | 7.0% | 7.9% | 9.0% |
| out_of_scope | 0.5% | 6.1% | 0.8% | 6.7% |

- **nit 31.9% and optimization 11.6% are inflated.** The cause is 2,247
  coverage-bot reports (1,093 nit, 922 optimization, 156 out_of_scope,
  76 blocker) plus 133 "rebased" notes.
- **On human text the shares are plausible.**
  - nit 21.5%: 543 openers literally start with "nit", and the model gives 525
    of those `nit`.
  - optimization 5%.
  - blocker 9%, at about 67% precision.
- **out_of_scope 6.7% is about half administrative no-fit.**
- **The mock's 33% question share is the `?` artefact.**

## Error patterns

| pattern | frequency | example id | suggested fix |
|---|---|---|---|
| **The bundle loses diff-derived outcomes** (builder, not model) | 441 rejected + 73 walked_back, plus part of 980 acted_on | `THREAD:DEMO/widget#10383:3458018`, `THREAD:DEMO/widget#8074:2357865` | When the bundle predates persisted `anchor_changed`, keep `previous.thread_outcome`. Only recompute when the attribute is present. Add a regression test. |
| **Bot/CI notices get arbitrary labels.** One bot pseudonym posts 2,277 openers (2,247 coverage reports, 29 static-analysis). The same template gets four different labels. | 21.6% of joined openers | `THREAD:DEMO/widget#7682:2222338` (nit), `THREAD:DEMO/widget#10371:3428643` (optimization), `THREAD:DEMO/widget#6939:1992864` (blocker) | Pre-filter deterministically, by bot actor or template, before the model. Give these threads no review label, or a new `other`/`notice` class. |
| **No-fit administrative or acknowledgement notes:** rebased, bare links, "added reviewer", fyi, declined, LGTM, emoji | ~25% of human out_of_scope; 5/20 of question→nit; 5/30 short texts | `THREAD:DEMO/widget#9557:2941103`, `THREAD:DEMO/widget#2015:1035228`, `THREAD:DEMO/widget#8469:2499662`, `THREAD:DEMO/widget#9319:2837013` | Add an `other` comment_type (taxonomy change, prd/02 review), or tell the prompt that a non-review note is `suggestion` with intent `unclear`. |
| **out_of_scope ⇒ rejected coupling.** The few-shot example pairs `out_of_scope` with `rejected`. | 512/719 out_of_scope openers have intent `rejected`; it drives most new `rejected` outcomes | `THREAD:DEMO/widget#8912:2670229` | Change that example's intent to `unclear`. |
| **Intent is asked of opener-only text.** The schema defines intent as "what became of the thread". The model also books author status notes ("removed X", "rebased") and praise as `acted`. | 758 acted, mostly on openers | `THREAD:DEMO/widget#8372:2468480`, `THREAD:DEMO/widget#9304:2844784` | Stop asking for intent on the opener. Either classify the last non-opener reply (the trace schema already has a `reply` role) or leave the outcome deterministic. |
| **Commands to the AI reviewer bot are labelled question.** | 88/139 | `THREAD:DEMO/widget#10603:3611666` | Pre-filter "@bot-command" openers, or mark them as `other`. |
| **Mild-worded defects labelled nit, or question** | ~3/20 human sugg→nit; ~10/29 true defects missed as blocker | `THREAD:DEMO/widget#4582:1470427`, `THREAD:DEMO/widget#3886:1357464` | Prompt: "a statement that the code is wrong, does not work, or misbehaves is a blocker even when phrased mildly or briefly." Add a few-shot example such as "this is always false now". |
| **Perf over-read on non-perf technical remarks** | 5/20 human sugg→opt | `THREAD:DEMO/widget#8526:2541585`, `THREAD:DEMO/widget#6085:1766343` | Prompt: optimization only when cost (time, memory, allocations) is the point. |
| **Explicitly deferred remark labelled blocker** ("can be handled in a follow-up") | 1/20 sugg→blocker | `THREAD:DEMO/widget#8947:2683928` | Prompt: a deferral to follow-up work is out_of_scope. |
| **Description question adds** | 6 rows, 2/2 sampled wrong | `PR:DEMO/widget#3937` | Drop PR adds, or require a `?` outside URLs or code. |

Other robustness checks:

- **No failure tied to code blocks.** 658 openers contain them. Of the 190
  `` ```suggestion `` blocks, 103 became suggestion and 75 nit, which is
  reasonable.
- **Long texts (>4 kB, 42) are mostly benchmark or AI-review dumps.** The
  labels are plausible (optimization, blocker).
- **Short texts are fine for real review fragments** ("typo?", "unused?",
  "nit: …"). They fail on no-fit fragments ("rebased", "the fix", "LGTM",
  emoji).
- **Estonian could not be assessed.** Only 3 openers contain Estonian.
- **No prompt-injection or format failures** were seen.

## Recommended next steps

1. **Do not import this patch as is.** Whatever else gets imported, the
   `thread_outcome` part (1,582 flips) has to be held back. Fix `_outcome` in
   `ingest/relabel.py` so a bundle without a persisted `anchor_changed` keeps
   the previous outcome. Ship the fix with a test.
2. **If something is imported now, rebuild the patch as a subset.** Apply the
   v0.19.1 guard and keep only:
   - `comment_type` for threads not opened by the bot pseudonym or by
     AI-bot-command openers;
   - thread question **deletes**;
   - thread question **adds**, except bot-command openers;
   - `PR:` question **deletes**.

   Skip the `PR:` adds and every outcome change. On the rated human text this
   gets the 83% decisive win without the 2,277 noise relabels.
3. **Prompt and schema v2, then re-run.** A prompt or model change means a
   fresh model pass, not a git walk.
   - Add a deterministic bot/notice pre-filter. It needs no model.
   - Add an `other` class, or an explicit rule for non-review notes.
   - Remove `thread_intent` from the opener call, or feed it the replies.
   - Fix the `out_of_scope`/`rejected` few-shot example.
   - Add a mild-defect blocker example and the perf-only rule for
     optimization.
4. **Re-scrape for real outcomes.** v0.16+ persists `anchor_changed`, so a
   later relabel can recompute outcomes without destroying them.
5. **Re-audit after the v2 run.** Use the same strata and seeds, and add a
   bots-excluded stratum for every transition. Also add a small dual-rated
   (human) subset for blocker recall, the one class where about 65% is not
   good enough on its own.

## Method notes

- Ratings were made from the text alone against the prd/16 definitions. The
  mock was not treated as ground truth.
- **Self-consistency:** 20 transition items were re-labelled blind, labels
  hidden, in a hash-shuffled order. 19/20 matched the first-pass verdict and 1
  stayed `unclear`. The model was not re-run at another temperature.
- The 1,343 untraced thread rows (see *Inputs*) are outside this audit. If
  their texts are duplicates of traced ones, the model labels them the same
  way.

Join and sample code (it holds no text; the sample file stayed in a local temp
directory):

```python
patch = list(rows(f"{W}/patch/export/labels.jsonl"))
th   = {r["object_id"]: r for r in patch if r["target"] == "thread"}
qrow = {r["object_id"]: r for r in patch if r["target"] == "question"}
recs, descs = {}, {}
for t in rows(TRACE):
    if t.get("batch"):               # 3 batch records: diagnostics only
        continue
    c = t["context"]
    if c["role"] == "opener":
        recs[c["thread_id"]] = t     # last call wins (== patch; 0 mismatches)
    elif c["role"] == "description":
        descs[f"PR:{c['repo']}#{c['pr_id']}"] = t
items = [dict(id=tid, h=sha256(tid), old=th[tid]["previous"]["comment_type"],
              new=th[tid]["labels"]["comment_type"],
              old_out=th[tid]["previous"]["thread_outcome"],
              new_out=th[tid]["labels"]["thread_outcome"],
              mock=MockClassifier()._labels_only(t["text"], "comment"),  # mock intent/q
              **t["labels"], qflip=(qrow.get(tid) or {}).get("action"), mand=tid in MAND)
         for tid, t in recs.items() if tid in th]
take = lambda pool, k: sorted(pool, key=lambda x: x["h"])[:k]   # deterministic
# strata: 20 x top-10 transitions (non-mandatory), 50 unchanged, 30 mandatory,
# 30 PR question flips, 30 texts of 3-20 chars, 55 outcome flips;
# + 3 x 20 bots-excluded supplements (key sha256("supp" + id)).
```

# Audit task — are the LLM review labels better than the deterministic mock?

You are an independent reviewer/LLM with access to a **raw-zone trace file** and a
**label patch tgz** on disk. Your job is to judge, from the actual comment text,
whether the new model labels are **higher or lower quality** than the old
deterministic mock labels, where they differ, and whether the patch should be
imported (it already has a deterministic safety guard, see below).

Work locally. The trace contains real internal review prose: **do not paste raw
text into any public repository or report**; use ids/pseudonyms, and paraphrase
or redact examples in the final deliverable.

\---

## 1\. System context

Megaspeed ingests Bitbucket + Jira into a pseudonymised event log. The only
component that reads human prose is the local text classifier. It projects a
comment or PR description onto a **closed vocabulary** (below) and the raw text is
then discarded. This patch is a **re-label** of an already-imported corpus:

* The original scrape used a deterministic **mock** classifier (keyword rules:
`crash`/`broken`→blocker, `?`→question, `nit`/`typo`→nit, perf words→optimization,
`fixed`/`done`→intent acted, etc.).
* A local **qwen3:30b-a3b** re-read every text with a JSON-schema-constrained prompt
(`temperature 0`, `seed 0`, **thinking disabled**, 4 workers, batch size 1).
* The patch corrects **only labels**; it does not re-emit events or rewrite source
activity. Question-status flips add/delete the derived `question\_raised` event.

**Inputs (paths; adjust to your machine):**

* `<trace>` = `export\\hans-relabel-202611222146.tgz.trace.jsonl` — 11,935 JSONL
records, one per model call.
* `<patch>` = `export\\hans-relabel-202611222146.tgz` — a tgz with
`export/manifest.json`, `export/labels.jsonl`, `export/sha256sums`.
* `<mandatory>` = `relabel-mandatory-threads.txt` — thread ids whose DC severity
is `BLOCKER` (572 in the store; 565 among the patched threads).
* The old mock labels are the `previous` object on each patch row; the new labels
are `labels` (same as `record\["labels"]` in the trace).

### Trace record (per text)

```json
{
  "ts": "...", "call": 123, "backend": "ollama", "label\_version": "ollama-v1",
  "model": "qwen3:30b-a3b", "url": "...", "kind": "comment" | "description",
  "context": {"repo": "DEMO/widget", "pr\_id": 11096, "role": "opener" | "reply" | "description",
              "thread\_id": "THREAD:DEMO/widget#11096:123", "comment\_id": 123,
              "activity\_id": 456, "ticket\_keys": \["DEMO-123"], "ts": "...",
              "actor": "LaNo", "commit\_ids": \["..."]},
  "text": "the exact raw text",
  "request": { "model": "...", "messages": \[...], "format": {...}, "think": false, ... },
  "response": { ... full Ollama answer ... },
  "content": "the JSON string the model answered",
  "thinking": null,                 // no-think run
  "timings": {"wall\_ms": 1234, "eval\_count": 30, ...},
  "labels": {"comment\_type": "nit", "thread\_intent": "acted", "question": false,
             "answered": false, "phase": "review"},
  "fallback": false, "ok": true, "error": ""
}
```

### Patch row (the old/new pairing)

```json
{"target": "thread", "object\_id": "THREAD:DEMO/widget#11096:123",
 "action": "set", "labels": {"comment\_type": "nit", "thread\_outcome": "unresolved",
 "classifier\_version": "ollama-v1"},
 "previous": {"comment\_type": "suggestion", "thread\_outcome": "unresolved",
              "classifier\_version": "mock-v1"}}
```

Question rows (`target: "question"`) have `action: add|delete|set`, `labels.phase`,
and either a `THREAD:...` or `PR:...` object. `add` means the model says question
where the mock did not; `delete` the reverse.

**Join:** a trace record with `context.role == "opener"` matches the thread row with
`object\_id == context.thread\_id`. Use the trace for `text`, `thread\_intent`,
`question`, `answered`, `phase`; use the patch for the old/new `comment\_type` and
`thread\_outcome`. Descriptions (`role == "description"`) have no thread row; their
question flips are the `PR:` question rows.

**Model reliability:** 0 fallbacks, 0 retries, 0 batches; `thinking` is null
because the run used `--no-llm-think`. The model never saw DC severity, the diff,
or the review outcome.

\---

## 2\. The closed vocabulary (judge against this, not your own taste)

`comment\_type`:

* **blocker** — must not merge until fixed: bug, data loss, security, broken contract.
* **suggestion** — an optional improvement the author may accept or decline.
* **question** — the reviewer asks for information; not a demand.
* **nit** — style, naming, typo, formatting; trivial, no behaviour change.
* **out\_of\_scope** — a valid remark about work that belongs in another ticket/PR.
* **optimization** — performance, memory, throughput, algorithmic cost.

`thread\_intent` (what became of the thread, not what it asks for):
`acted` (implemented/fixed), `rejected` (won’t fix/by design), `withdrawn`
(commenter takes it back), `unclear` (no answer yet).
`question`: true only when the text genuinely asks for information.
`answered`: true when this reply resolves someone else’s question.
`phase`: `implementation` for an author’s description/plan, `review` for a review
comment.

**Hard rule:** a thread whose **DC severity is `BLOCKER`** must end up `blocker`
regardless of text. The reducer enforced this; v0.19.1 now enforces it in the patch
builder **and** the importer. The raw model labels below are pre-guard; do **not**
count the model’s non-blocker label on a `<mandatory>` thread as a final-store
regression. You may still judge the model’s text-only label for the audit.

\---

## 3\. Starting statistics (verify, don’t trust)

The patch has **11,885 thread rows**, **2,243 question rows** (829 add, 1,414
delete), **0 fallbacks**, and 11,935 trace records. 8,107/11,885 threads changed
`comment\_type` (68.2%); 3,778 unchanged.

|type|mock|model|
|-|-:|-:|
|suggestion|7,203|3,042|
|question|2,806|2,115|
|nit|570|3,789|
|optimization|560|1,376|
|blocker|681|833|
|out\_of\_scope|65|730|

Top transitions: `suggestion→nit` 2,774 · `suggestion→optimization` 1,105 ·
`question→suggestion` 918 · `suggestion→question` 612 · `suggestion→blocker` 550 ·
`suggestion→out\_of\_scope` 507 · `question→nit` 380 · `blocker→suggestion` 211 ·
`optimization→suggestion` 166 · `blocker→question` 145.

Question flips by the old thread type: `question→delete` 1,302;
`suggestion→add` 628; `optimization→add` 111; `blocker→add` 72;
`blocker→delete` 45; `out\_of\_scope→add` 10; `nit→add` 2; 67 `PR:` deletes
(description questions) and 6 `PR:` adds.

Outcome flips: 1,582, of which **980 `acted\_on→unresolved`**, 441
`rejected→unresolved`, 73 `walked\_back→unresolved`, and smaller moves. The
diff anchor (`anchor\_changed`) is absent on this pre-v0.16 bundle, so outcome is
intent-driven, not diff-driven.

DC mandatory threads: 565 in the patch; the raw model made **116** blocker,
the old store had **565**. v0.19.1’s guard forces those 449 back to blocker on
import.

\---

## 4\. Questions to answer

1. **Overall:** are the model labels better, worse, or mixed vs the mock? Give a
per-category verdict, not one number.
2. **The big shifts:** are `suggestion→nit` (2,774) and `suggestion→optimization`
(1,105) improvements, or is the model over-reading style/perf? Is
`suggestion→blocker` (550) finding real defects or over-escalating?
3. **Questions:** the model removes 1,302 mock questions and adds 829. Compare
each sampled text to the model and mock calls: is the `?` mock over-calling,
or is the model missing genuine questions? Same for `question→suggestion` and
`suggestion→question`.
4. **Outcome:** 980 mock `acted\_on` become `unresolved`. Does the text say
“fixed/done/addressed” (mock right) or is the model’s caution honest? Check
the model’s `thread\_intent` and the reply structure.
5. **Distribution:** 31.9% `nit`, 11.6% `optimization`, 6.1% `out\_of\_scope`,
7.0% `blocker`. Is any class implausibly over/under-used for this corpus?
6. **Robustness:** short texts (`n=3–20`), code blocks, emoji, quoted commands,
Estonian/English, bot-like notices; any clear failure pattern?
7. **Recommendation:** import the patch (with the v0.19.1 guard), import only
some label types, change the prompt/schema/model and re-run (the label cache
makes a prompt/model change a fresh model pass, not a git walk), or leave the
mock labels?

## 5\. Method (sample, then rate)

Do **not** judge from the aggregate table alone.

1. Parse `<trace>` and `<patch>`; join openers to thread rows; keep
`(text, old\_type, new\_type, new\_intent, question, answered, phase)`.
2. Build a **deterministic stratified sample**: 20 per top transition (or all if
fewer), 50 random unchanged, 30 `<mandatory>` (text-only), 30 descriptions
(question flips), 30 short texts. Sort by thread id/hash; do not cherry-pick.
3. For each sampled text, decide the correct labels **from the text alone** with
the section-2 definitions, then compare:

   * `model\_better` (mock wrong, model right),
   * `mock\_better` (mock right, model wrong),
   * `both\_ok`, `both\_wrong`, `unclear`.
For `thread\_intent`, judge whether the text supports `acted`/`rejected`/
`withdrawn`/`unclear`; the outcome follows the reducer’s deterministic rule
where the diff is known (it is not, here).
4. Report **per-transition** rates with sample sizes and confidence; a 60%
overall accuracy hiding a 10% `blocker` recall is not a pass.
5. A second, independent pass on 20 samples (or a different temperature) is
useful for self-consistency; state if you did it.
6. Include a small code snippet you used to join/sample (no raw text in it).

## 6\. Output template (local markdown)

```
# Relabel audit — model vs mock

## Verdict
- overall: model better / mixed / worse
- apply: yes (with v0.19.1 guard) / subset / no / re-run with changes
- confidence + limitations

## Per-label / per-transition
| transition | n sampled | model better | mock better | both ok | both wrong | unclear |
...

## Question flips
... table + examples (ids only)

## Outcome flips
...

## Error patterns
- (pattern, frequency, example id, suggested prompt/schema fix)

## Recommended next steps
1. ...
```

## 7\. Constraints

* Read-only: do not modify the trace, patch, or any store.
* If a text is ambiguous, use `unclear`; do not invent ground truth.
* The mock is **not** ground truth: it is a keyword baseline. The audit is about
which label a careful human reviewer would assign.


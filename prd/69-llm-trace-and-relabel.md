# 69 — Raw-zone LLM trace and label-only re-classification

Status: shipped (v0.16.0)

## Problem

The text classifier (`prd/16`) reads raw prose once and drops it. When a run
came back with a label set nobody trusted — the 2026-11-22 hans scrape produced
15,540 calls and **15,540 fallbacks** because the model box was down — the only
way to get model labels was to re-walk git and the REST API for hours.

Worse, the failure was invisible: the run's progress lines said "classifying
text", the manifest said `fallbacks: 15540`, and nothing recorded *what* was
sent, *what* came back, or *why* a fallback happened. Diagnosing it required
reading the container log's start time.

## Goals

1. **Every classifier call is traceable.** Store the full input (text + exact
   request body, thinking flag and JSON schema included) and the full output
   (content, *thinking*, Ollama timings) in the **raw zone**, alongside metadata
   that ties it to the scrape's input: repo, PR id, PR commit ids, ticket keys,
   thread id, comment id, activity id, role (opener / reply / description).
2. **Every call is visible while it runs.** One timestamped progress line per
   call: where it is, what was tried (backend/model/URL, text digest), how long
   it took, and either the labels assigned or the **full** error.
3. **Thinking is a knob.** Diagnostic runs keep the model's deliberation in the
   trace; throughput runs disable it (`--no-llm-think` / `MEGASPEED_LLM_THINK=0`).
4. **Labels can be re-determined without re-scraping.** A small tgz that carries
   label corrections and nothing else: built from the trace, imported with the
   ordinary `ingest import`, applied as an `UPDATE`, never as a re-emit.
5. **The contract still holds.** The trace is raw-zone state like the identity
   map and the pepper: it never enters a bundle or the store. The patch is
   closed labels only, and the importer runs `--verify` before `COMMIT`.

## Design

### The trace (`ingest/llmtrace.py`)

`<state-dir>/classify-trace.jsonl`, one JSON object per call. Append-only and
flushed per record, so a killed scrape keeps every completed call; a torn last
line is skipped (and counted) by the reader. Records larger than 1 MiB keep the
labels and error but truncate the prose.

The scrape enables it for every non-fixture source (`MEGASPEED_CLASSIFY_TRACE=0`
or `--no-llm-trace` disables it). The bundle manifest records
`text_trace: {file, records, bytes}` — provenance, never a path or content.

The classifier owns the hook (`Classifier.on_call`); `MockClassifier` traces too,
so a fast deterministic run can be relabelled later without a model in the loop.

### Per-call logging

`ProgressReporter.emit` stamps every line with UTC milliseconds. The scrape
wires `on_call` to write the trace and emit one line:

```
[2026-11-22T07:12:03.421Z] [classify] DEMO/widget#11096 · thread=123 · comment=123 · opener · comment · ollama:qwen3:8b · 2.31s · text=abcdef01 n=27 · ok · comment_type=nit thread_intent=acted question=no answered=no phase=review
[2026-11-22T07:12:04.002Z] [classify] … · FAIL mock-v1 · urllib.error.URLError: <urlopen error [Errno 111] Connection refused>
```

The hans git/merge trace lines carry the same stamp.

### Replayable deterministic facts

A thread's `thread_outcome` is deterministic first (`resolved`, did the diff move
at the anchor, did the commenter resolve it) and only falls back to the model's
`thread_intent`. To let a patch recompute the outcome without the source, THREAD
objects now carry `anchor_changed` (absent when unknown) and
`resolver_is_commenter` — both closed booleans in `taxonomy`.

### The label patch (`ingest/labelpatch.py`)

A tamperproof tgz (`manifest.json`, `labels.jsonl`, `sha256sums`) whose rows are:

```json
{"target": "thread", "object_id": "THREAD:DEMO/widget#11096:123",
 "labels": {"comment_type": "nit", "thread_outcome": "unresolved",
            "classifier_version": "ollama-v1"}}
{"target": "question", "object_id": "THREAD:DEMO/widget#11096:123",
 "labels": {"phase": "review"}}
```

`apply_patch` merges the keys into the THREAD object's `attributes` and into the
`attributes` of the `question_raised` events linked to that object. It is
idempotent, reports rows that matched / were unchanged / were missing, and never
inserts or deletes a row.

A re-classification that flips "is this a question" rewrites the **derived**
event, never an occurred one: an `add` row carries the `question_raised` event to
insert (and a resolution `question_answered` when the thread was closed by
someone other than the asker), a `delete` row removes the derived
`question_raised`/`question_answered` events linked to the object. Both are
idempotent and stay label-only in the sense that matters: no source activity,
timestamp or relation is invented beyond the derived event the reducer would
have emitted. A trace that predates v0.16 lacks the activity id / author needed
to build the event, so those flips are counted as `question_unappliable` and
reported, never guessed.

### Relabel (`ingest relabel`)

```
python3 -m ingest relabel --trace export/hans.state/classify-trace.jsonl \
    --bundle export/hans.tgz --out export/hans-labels.tgz \
    --llm-url http://127.0.0.1:11434 --llm-model qwen3:8b [--no-llm-think]

# a bundle that predates the trace: read the texts from the capture instead
python3 -m ingest relabel --hans-scrape D:\wrk\hans\scrape\<capture> \
    --bundle export/hans.tgz --out export/hans-labels.tgz \
    --llm-url http://127.0.0.1:11434 --llm-model qwen3:8b
```

Replays the opening comment of each thread and each PR description (the texts
whose labels land on a stored row), deduplicating identical texts into one model
call and keeping one record per target (the last wins, so a resumed trace is
safe). It compares against the bundle and emits only rows that changed.

`--hans-scrape` exists for the run that came *before* the trace: the capture on
disk is the same source, it needs no model during the scrape, and this mode
reads only the PRs the bundle holds with the reducer's deterministic gating
(human author, `ADDED`, non-empty). It touches no git, so re-labelling a 2,574-PR
corpus costs the model calls and nothing else.

The import side auto-detects the patch (`kind: label-patch`) and prints what it
updated; the ordinary pseudonymisation gate runs before `COMMIT`.

## Tests

`tests/test_llmtrace.py` — stamps, trace writer/reader (torn line, truncation),
call-line wording (ok / failure / breaker / mock), the request/response/thinking
record, the two-argument test-double path, and the scrape wiring (trace file,
manifest, per-call lines, no trace for `--fixture`).

`tests/test_relabel.py` — patch roundtrip, closed-vocabulary and target
rejection, `is_label_patch` sniffing, apply/idempotence/missing rows, question
phase updates, the end-to-end replay → patch → import cycle, question flips,
`--limit`, and the CLI.

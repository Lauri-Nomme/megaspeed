# Local LLM benchmark — DEL comments (prd/16)

One-off, run by hand on 2026-11-16 against the **real** local model. Not a test:
the test suite uses the deterministic mock (prd/16 "Tests"). **No raw comment
text is recorded here** — only labels, counts and latencies (prd/02).

## Setup

| | |
| --- | --- |
| Endpoint | `http://10.43.36.10:11434` (k3s `home-iot/ollama-moe`, CPU-only) |
| Model | `qwen3-30b-a3b:2507` (Qwen3 30B MoE, ~3B active, Q4_K_M, 18.5 GB) |
| Decoding | `/api/chat`, `format` = the closed-vocabulary JSON schema, `temperature: 0`, `seed: 0`, `num_predict: 128` |
| Corpus | bb-lab-b `DEL/delivery-api` + `DEL/delivery-web` via the normal scrape client: **340 comments, 131 PR descriptions** |
| Sample | every *k*-th comment of the corpus walk (deterministic spread), 20 comments + the first 6 descriptions |
| Harness | throwaway script in `/tmp` (not committed); it prints labels/latency only, raw text died with the process |

## Latency

| Call | n | median | mean | max |
| --- | --- | --- | --- | --- |
| `comment` (47–92 chars) | 20 | **1.86 s** | 2.03 s | 5.45 s (first call = cold model load) |
| `description` (178–199 chars) | 6 | **1.45 s** | 1.55 s | 2.65 s (first call) |

Warm calls cluster tightly at 1.4–2.0 s; length barely matters (a 200-char
description is not slower than a 50-char comment — the prompt dominates). Zero
fallbacks, zero schema violations in 26 calls.

**Cost of a full DEL scrape:** 340 + 131 ≈ 471 calls ≈ **15 minutes** of CPU on
the precision box, single-threaded. That is acceptable for a one-pass scrape
(it runs next to the source while the REST/git work happens anyway), but it is
the reason the classifier is **not** on the import side and not in the tests.

## Quality spot-check (my own judgement vs the model)

20 comments, hand-read:

| Verdict | n | Notes |
| --- | --- | --- |
| agree with my read | 18 | |
| debatable | 2 | both are *reply-shaped* comments (see below) |

Specific observations (generalised, no text):

- **Blockers are reliable.** Both `BLOCKER`-severity comments came back
  `blocker`, and so did a `NORMAL`-severity one that explicitly asked for a
  test before merge — i.e. the model catches a blocker DC itself did not mark.
  Note the reducer does not *need* this: DC's `BLOCKER` marker wins
  deterministically; the model adds the unmarked cases.
- **Explicit nit prefixes are honoured.** A comment that opens by declaring
  itself a nit but is phrased as a question came back `nit`, while the mock's
  `?`-rule said `question`. The model is right: for the comment-tax metric this
  is a nit, not a question.
- **Out-of-scope closure is caught.** A comment that closes a thread by saying
  the work will not happen this quarter and pointing at a ticket came back
  `out_of_scope` + `rejected` — exactly the "valid ask, wrong vehicle" class
  prd/15 Q4 wants.
- **Thread intent was fixed by the prompt.** The first prompt iteration defined
  `acted` loosely ("what the thread wants"), and *requests* were labelled
  `acted` (6/20). Rewording it to "what **became** of the thread; a request with
  no answer is `unclear`" moved every request to `unclear` and every
  done/fixed statement to `acted` (0 mislabels in the re-run). Worth knowing: a
  small model needs the *state* framing, not the *intent* framing.
- **Reply-shaped comments land in the residual class.** An author's
  acknowledgement-and-fix reply classified as `suggestion` (the residual
  `comment_type`) with `thread_intent=acted`. The label that matters there is
  the intent/outcome, not `comment_type` — which is why `thread_outcome` is
  derived from the diff first and the text second.
- **A bot notice is misclassified.** A stale-PR auto-decline notice came back
  `out_of_scope`/`rejected`. It is not a review remark at all. Fix (implemented,
  deterministic): the reducer never sends a `bot`-authored comment to the
  classifier — bot-vs-human is a known-account decision (prd/02), so it is made
  *before* the model is asked. The bench harness called the model directly,
  which is why it shows up here.
- **Agreement with the mock backend: 16/20 (80%).** All four disagreements are
  cases where the mock is demonstrably wrong: the self-declared nit phrased as a
  question (mock: `question`), and the bot notice / out-of-scope closures (mock:
  `suggestion`, because it has no rule for "this is not happening"). The mock
  is a *test fixture and offline fallback*, not a competitor.

## What this means for the design

1. The schema is doing its job: 26/26 answers parsed, no free text, no retries.
2. ~2 s/comment is fine for one-pass scraping but rules out per-render or
   per-query classification; labels must be materialised in the tgz (they are).
3. The deterministic half of `thread_outcome` (git diff at the anchor) matters:
   the model's intent label is good enough to *break a tie*, not good enough to
   be the primary signal.
4. If the call count ever hurts, the obvious win is **batching** (N comments per
   call with an array schema) — the prompt, not the text, is the cost. Not done
   here: the interface is deliberately one text in, one label set out.

## End-to-end check (live scrape, real model, real git)

A 5-PR `DEL/delivery-api` scrape through the real path (`scrape()` → tgz →
`import_bundle` → `--verify`), with the Ollama backend, the push classifier and
the anchor diff all on:

- 14 classifier calls (9 comments + 5 descriptions), **0 fallbacks**, 2 bot
  notices skipped before the model was asked;
- `comment_type`: suggestion ×7, blocker ×1, question ×1; 1 `question_raised`
  event; `push_class`: content ×4; `--verify`: **ok**, no canary, no free text;
- wall clock ≈ 40 s for 5 PRs (mostly model latency + the shallow git fetch).

**Corpus caveat:** in this DEL lab data every comment is *unanchored*
(`anchor_class=general`, 11/11) and every thread is *open* (11/11). So on this
corpus `thread_outcome` is always `unresolved` and the anchor diff never fires
(`thread_changes: no_anchor=11`) — the `acted_on` / `rejected` / `walked_back`
branches are exercised by the fixture + unit tests (a real git repo with a
changed and an untouched anchor path), not by DEL. A corpus with resolved,
file-anchored threads is needed before the comment-tax numbers mean anything.

# 16 — Scrape-side classification: `push_class` + local LLM

The scrape must produce a **complete, finished, pseudonymised tgz in one pass** — the airgapped
env is visited once, and nothing downstream (import/analysis) ever needs a re-scrape.

## The "one finished" guarantee

- The scrape already fetches raw comment text + PR descriptions (it reads them to extract ticket
  keys). It now, **before discarding them**, (a) computes `push_class` (deterministic, from a local
  git clone) and (b) runs the local-LLM classifier over that same text.
- Local state (scrape-side, never transferred) keeps: watermark, identity map, the **git clone**
  (for incremental patch-id), and a **raw-text cache** — so a future classifier upgrade re-classifies
  from cache, not from a source re-fetch.
- The tgz carries only the finished labels (closed vocabulary) — never raw text.

## `push_class` — deterministic, via `git patch-id`

- **Not a full clone/mirror, and not REST-diff scraping.** The REST commits API returns metadata
  (no tree/diff); re-implementing `git patch-id` on Bitbucket's `compare/diff` JSON is fragile (line
  numbers/context shift across rebases, so a raw comparison misclassifies). The efficient, exact path
  is a **shallow/partial `git fetch` of only the refs the matching PRs touch** (delta-compressed pack),
  then local `git patch-id`.
- The scrape keeps a shallow, partial git repo per project as local state; it incrementally
  `git fetch`es the `fromRef`/`toRef`/`fromHash`/`toHash` it sees (`--depth` as needed,
  `--filter=blob:none`).
- For each RESCOPED (`pr_pushed`): `git patch-id --stable` of `target...old` vs `target...new`.
  - same patch-id → `pure_rebase`
  - different patch-id → `content`
  - different patch-id but `git range-diff` shows only conflict hunks → `rebase_conflict`
- Emit `attributes.push_class ∈ {content, pure_rebase, rebase_conflict}` (replaces live `other`; GAP-01).
- The shallow repo stays scrape-side; only `push_class` crosses.

## Local LLM classification

- A pluggable `classify(text, kind) -> {closed labels}` interface with two backends:
  - **OpenAI-compatible endpoint** (real) — the "precision box" at `http://10.222.1.99:4000/v1`
    (an OpenAI-compatible gateway; `GET /` answers `{"error":"Unknown channel requested"}` = it needs
    a known model/channel name + key). temperature 0, JSON-schema/grammar-constrained decoding.
    Configured via `MEGASPEED_LLM_URL` / `MEGASPEED_LLM_KEY` / `MEGASPEED_LLM_MODEL` (exact model name
    TBD — the gateway routes by channel name). Use it to test/iterate/benchmark.
  - **Mock** (deterministic) — for tests and no-LLM environments; must satisfy the same schema.
- Closed vocabulary (extends prd/02; `--verify` accepts it):
  - `comment_type ∈ {blocker, suggestion, question, nit, out_of_scope, optimization}`
  - `thread_outcome ∈ {acted_on, rejected, walked_back, unresolved}` — from the thread's resolution
    state (`resolvedDate`/`resolver`) plus whether the diff changed at the comment anchor.
  - `question_raised` / `question_answered` events, each with
    `phase ∈ {breakdown, refinement, sprint, implementation, review}`.
- Everything enumerated, no free text, no paraphrase (prd/02). A classifier version is recorded so
  re-classification is replayable from the raw-text cache.

## Emitted into the tgz

- `push_class` on `pr_pushed` events.
- `comment_type` + `thread_outcome` on `THREAD` objects.
- `question_raised` / `question_answered` events with `phase`.

## Tests (per AGENTS.md — every feature)

- `push_class`: deterministic; a test builds a real git repo and asserts content vs pure-rebase vs
  conflict classification.
- LLM: the **mock** backend drives all classifier tests; a contract test asserts mock and Ollama
  backends share the same label schema; `--verify` accepts the new vocabulary.

## Out of scope

Business-hours calendar · sentiment/personality · anything that would put raw text downstream.

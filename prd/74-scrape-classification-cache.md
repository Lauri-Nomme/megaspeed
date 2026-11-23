# 74 — Scrape classification cache (draft; after the relabel workflow settles)

Status: **implemented (v0.20.5).** `ingest/labelcache.py` holds one
digest-keyed, raw-text-free cache and a `CachedClassifier` wrapper; the scrape
uses `<state-dir>/classify-cache.jsonl` by default (`--classify-cache PATH`,
`--no-classify-cache`), and `ingest relabel` still defaults to
`<out>.labels.jsonl` — point both flags at one file to share it. A per-call
fallback is **not** cached under the model identity, so one transient failure
cannot become a permanent mock label.

## Problem

A scrape classifies each comment/description with the local model on every run.
The relabel path already checkpoints labels in `<out>.labels.jsonl`, keyed by
the text digest and stamped with model/think/version — but the **scrape** does
not read it. Re-scraping the same capture (or an incremental run that re-reads
a changed PR) pays the model again for texts whose label cannot have changed.

## Goal

One digest-keyed cache shared by the scrape and relabel:

- `<state-dir>/classify-cache.jsonl`, entries
  `{kind, digest, labels, model, think, version}` (no prose).
- The scrape consults it before a call; a hit is still written to the trace with
  `cached: true` (audit) but costs no request, and the bundle gets the same
  labels.
- The relabel uses the same file by default (today it writes
  `<out>.labels.jsonl`); `--labels-cache` remains the override.
- `--no-classify-cache` disables both read and write.

## Acceptance

- A second scrape of an unchanged fixture with a fake counter classifier makes
  **0** model calls and produces the same bundle labels.
- A changed text (digest differs) is classified.
- A different model/think/version is a miss, never a silent mix.
- The cache contains no raw prose; `--verify` unaffected.
- The trace has a `cached: true` record for every hit.

## Implementation notes (v0.20.5)

- `LabelCache` moved out of `relabel.py` into `ingest/labelcache.py`, unchanged.
- `CachedClassifier` consults the cache per text, calls the inner batch only for
  misses (worker pool preserved), traces every hit with `cached: true`, and
  reports `cache: {path, read, hits, misses}` in the manifest provenance.
- Only a non-mock, non-fixture source is wrapped; fixture/mock runs create no
  state, so tests and `--fixture` scrapes are unchanged.
- The relabel's cache path stays `<out>.labels.jsonl`; a scrape can reuse a
  relabel cache by passing `--classify-cache <out>.labels.jsonl` (same model /
  think / version identity).
- Acceptance is covered by `tests/test_labelcache.py`: repeat text = 0 calls,
  batch misses only, identity mismatch = miss, a fallback is never cached, a hit
  is traced, and a second scrape of the same fixture makes 0 model calls.

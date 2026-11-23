# 74 — Scrape classification cache (draft; after the relabel workflow settles)

Status: **draft / not dispatched.**

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

## Files

`ingest/classify.py` (hook/сache interface), `ingest/scrape.py` (wire the
state-dir cache), `ingest/relabel.py` (default to the shared path), tests.

## Why later

The relabel run is still producing the first model-labelled corpus. Once its
patch is imported, we will know the real hit-rate and whether the scrape needs
the cache at all (the relabel may remain the only model path).

# 72 — Sprint metadata refresh

Status: ready to dispatch (unit **U72**)

## Problem

`v0.18.0` added `sprint_no` (the number parsed from a Jira name like
`Sprint 352`) so every surface can show `S352` instead of `S4392`. Populating it
currently requires a full Jira re-scrape — 2,391 issues, worklogs and changelogs
— just to refresh 134 sprint records. The live store still has
`sprint_no: null`, so the UI keeps the `S<id>` fallback.

## Goal

A **sprint-only** Jira refresh that reads sprint metadata with the same client
and writes a tiny bundle of SPRINT objects; import upserts them without touching
the issue watermark or adding an ingest run.

```
python -m ingest scrape --jira-cloud --sprints-only \
  --out export/demo-sprints.tgz [--state-dir export/demo.state]
# DC: --jira --sprints-only
python -m ingest import export/demo-sprints.tgz --db data/demo.duckdb
```

## Behaviour

**Scrape side** (`ingest/scrape_jira.py` new `scrape_sprints`):
- Build the Jira client exactly as a normal scrape does (`_jira_transport`, DC or
  Cloud) and enumerate the configured boards' sprints (`JiraClient.sprints(board)`;
  Cloud equivalent).
- Reduce each sprint through the existing Jira reducer path (or a small shared
  helper) so the SPRINT object has the same shape and **`sprint_no`** parser as a
  full scrape. No issues, worklogs, changelogs or events.
- Write a normal `.tgz` with `events.jsonl` empty, SPRINT objects only, and
  `manifest.sprint_refresh = true`, `watermark_ts = null`, `counts.events = 0`,
  `counts.objects = N`, plus the usual source/provenance.
- No identity map/pepper is required (SPRINT objects carry no actor); do not
  create or modify the person identity state.

**Import side** (`ingest/importer.py`):
- `read_bundle` already accepts an empty events file; `validate_bundle` must
  accept no events and SPRINT objects.
- When `manifest.sprint_refresh` is true: **skip `write_run`** (this is not an
  ingest; it must not move `source_watermark` or add a zero-run) and skip the
  watermark check, but still upsert objects and run `verify` before `COMMIT`.
- Import report says `mode: sprint-refresh`, objects updated, no events.

## Tests

- fixture Jira transport with named sprints (`Sprint 352`, `S352`,
  `DEMO Sprint 352` → `352`; `Hardening` → none): sprint-only scrape writes 0
  events, N SPRINT objects, `sprint_no` set/absent as expected,
  `sprint_refresh: true`.
- import into a store: SPRINT rows updated with `sprint_no`; `ingest_runs` row
  count and `source_watermark` unchanged; no event/object other than SPRINTs
  changed; `verify` OK.
- a normal full bundle still imports exactly as before (no regression).
- `--jira --sprints-only` and `--jira-cloud --sprints-only` both route (Cloud can
  use the existing fixture/proxy).

## Files

`ingest/scrape_jira.py`, `ingest/scrape_cli.py`, `ingest/importer.py`,
`ingest/jira.py`/`jira_cloud.py` only if the sprint listing needs a wrapper,
`tests/test_sprint_refresh.py`, `docs/DEV.md`.

Do not touch `ingest/rekey.py` (prd/71), the analysis layer, or the UI.

## Rules

Work in the assigned worktree/branch; scoped commit; no push/tag; public-repo
identifier rule (`DEMO`/`demo-tlla`). Report the commit, tests, and the
bundle/API evidence. This is the cheap path to the live `S352` labels — it must
not require re-reading issues.

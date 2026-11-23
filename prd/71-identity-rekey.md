# 71 — Identity maintenance: one `rekey` command

Status: ready to dispatch (unit **U71**)

## Problem

Two identity repairs were done by hand in SQL (2026-11-23): the surname-first
Bitbucket pseudonyms were swapped (`NoLa→LaNo`), then every numeric-suffix
variant was merged into its base (`MaPa2→MaPa`, `BeWo2/3→BeWo`, `CaGi14/41→CaGi`,
…). The same work is still ahead for the laptop `identity.json`, and a future
merge/split will be needed again. The ad-hoc scripts are not tested, not
idempotent, and do not touch the identity map or a label patch.

## Goal

One auditable command that merges/rekeys pseudonyms across the **store**, the
**identity map**, and a **label patch**, with a dry-run evidence table and one
transaction.

```
python -m ingest rekey --db data/demo.duckdb \
  --identity export/demo.state/identity.json \
  [--map OLD=NEW ...] [--merge-suffixes] \
  [--patch export/labels.tgz --out export/labels.rekeyed.tgz] \
  [--dry-run | --apply] [--no-backup]
```

* **`--merge-suffixes`** — every actor/person matching `^([A-Za-z]{4})\d+$`
  maps to its base (`DEMO2`-style suffixes are allocation artifacts; the tenant
  has no real collisions). The base PERSON row is created if missing.
* **`--map OLD=NEW`** — explicit aliases (repeatable), for anything the suffix
  rule does not cover. A target may be an existing person (merge) or new (rename).
* **`--patch`** — rewrite only the added derived events' `actor` in a
  `label-patch` tgz; write to a **new** `--out` (never in place), re-validate.
* **`--identity`** — update each stored `pseudonym` that appears as a key of the
  mapping; report duplicates instead of silently collapsing unrelated entries
  unless they are the same mapped target.
* **`--dry-run` is the default.** `--apply` makes a timestamped backup
  (`<db>.pre-rekey-<UTC>.duckdb`, `--no-backup` to skip), runs one transaction,
  and ends with `verify`.

## Behaviour

For each `OLD→NEW`:

1. `event_objects` PERSON links whose object is `PERSON:OLD` → `PERSON:NEW`
   (dedupe `(event_key, object_type, object_id)` afterwards).
2. `events.actor = NEW` where `OLD`; `objects.actor = NEW` where `OLD`.
3. If `PERSON:NEW` exists, delete the now-orphan `PERSON:OLD`; else rename/create
   the row (copying `attributes.actor_kind`). Delete any PERSON row that has no
   event/object reference after the remap.
4. Update the identity map (if given) and rewrite the patch (if given).
5. `verify(con, key_shape, projects, imported_scope(con))` before `COMMIT`.

Idempotent: a second `--apply` reports `unchanged` and writes nothing.

Dry-run prints, per mapping: source split (BB events `repo IS NOT NULL` / Jira
`repo IS NULL`), PR/story counts, **shared ticket keys** between a candidate's
PRs and its target's stories (the evidence used for the 2026-11-23 merges), and
any cycle/collision (`A→B` while `B→A`) as a warning — never applied blindly.

## Tests

- synthetic store (fixture import + a few suffix actors/objects): dry-run writes
  nothing; apply merges events/objects/relations, creates a missing base, is
  idempotent, keeps event counts, leaves no orphan PERSON/relation, `verify` OK.
- explicit map + identity map: pseudonyms updated, duplicate-target reported.
- label patch: a patch with `action: add` events gets the new actor, validates,
  and the original file is untouched.
- cycle (`A→B`, `B→A`) is refused with a clear message.
- backup is created on `--apply`; `--no-backup` skips.

## Files

`ingest/rekey.py` (new), `ingest/__main__.py` (route `rekey`), `tests/test_rekey.py`.
Read-only reuse of `ingest.labelpatch`, `ingest.store`, `ingest.taxonomy`.
Do not touch scrape/importer/analysis files.

## Rules

Work in the assigned worktree/branch; scoped commit; no push/tag; public-repo
identifier rule applies (`DEMO`/`demo-tlla`, never a real prefix). Report the
commit, tests, and a dry-run sample on a fixture store.

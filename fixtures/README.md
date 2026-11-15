# Fixtures — synthetic Bitbucket Data Center data

`bitbucket-dc-fixture.json` is a generated bundle of **50 pull requests** across
two repos (`ENG/platform`, `ENG/mobile`), with 481 PR activities and 207 commits.
It exists so the whole pipeline runs end-to-end with no credentials.

Everything in it is fabricated: people (`lauri.nomme` … `mart.sepp`), tickets
(`PROJ-101` …), branch names and prose. Every human-authored string is also
tagged with a unique `zzcanary…` token so the pipeline can *prove* it discarded
raw text (see `ingest/store.py:verify`).

## Shape

The bundle is shaped like the DC REST API — paging envelopes, `user` objects,
`fromRef`, activity `action`s — so fixture mode and live mode share one client:

```json
{
  "schema": "megaspeed.fixture/1",
  "projects": [
    { "key": "ENG",
      "repos": [
        { "slug": "platform",
          "pull_requests": [
            { "pull_request": { "id": 110, "title": "…", "fromRef": {…}, "author": {…} },
              "activities": [ { "id": 110001, "action": "OPENED", "user": {…} }, … ],
              "commits":    [ { "id": "<sha>", "message": "…", "author": {…} }, … ] }
          ] } ] } ]
}
```

## Coverage on purpose

- Ticket keys appear in **branch**, **PR title**, **PR description** and
  **commit message** — and ~1 in 7 PRs has no key at all, so the filter has
  something to drop.
- All PR activity types: `OPENED`, `RESCOPED`, `COMMENTED`, `APPROVED`,
  `UNAPPROVED`, `NEEDS_WORK`, `REVIEWER_UPDATED`, `MERGED`, `DECLINED`.
- Outcomes: merged, declined, still open (right-censored in the UI).
- Timestamps are business minutes only (Mon–Fri, 08:00–18:00 UTC), Sept–Nov 2026.

## Regenerate

```bash
.venv/bin/python -m ingest --write-fixtures --n 50          # deterministic (seed 20261115)
.venv/bin/python -m ingest --write-fixtures --n 200 --seed 7
```

Ingest the committed bundle instead of generating in memory:

```bash
.venv/bin/python -m ingest --fixture-file fixtures/bitbucket-dc-fixture.json
```

## One non-Bitbucket field

`RESCOPED` activities carry an extra namespaced key:

```json
"megaspeedPatchId": { "push_class": "pure_rebase" }
```

That is **not** a DC field. It stands in for the local deterministic push
classifier (`git patch-id --stable`, prd/02) which needs a local clone we do not
have in fixture mode. Live mode never sets it, so `push_class` degrades to
`other` — honest, and reported by `--probe`.

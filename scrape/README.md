# Scraper: build, run, transfer (airgap)

The scrape side runs **next to the source** (which may be isolated/offline),
reads Bitbucket DC, classifies and pseudonymises locally, and writes a **tgz**
of already-pseudonymised rows. The main side imports that tgz into DuckDB. See
`prd/11-airgap-ingest.md` for the design and `prd/13-scraper-build-ci.md` for the
build/CI contract.

```
source ──► megaspeed-scrape ──► export/<name>.tgz ──► python3 -m ingest import ──► DuckDB
           (airgapped)          manifest.json                 (main side)          (analysis)
                                events.jsonl
                                objects.jsonl
                                sha256sums
```

## 1. Run the two halves

Scrape (this is the artifact; `python3 -m ingest scrape` is the same CLI):

```bash
export MEGASPEED_BB_BASE_URL=http://127.0.0.1:7991
export MEGASPEED_BB_USER=admin MEGASPEED_BB_PASSWORD=admin-lab-pw
export MEGASPEED_TICKET_PROJECTS=DEL
export MEGASPEED_REPOS=DEL/delivery-api,DEL/delivery-web

./scrape/dist/megaspeed-scrape --live \
  --out export/del.tgz --state-dir export/del.state
```

Import (main side; needs DuckDB, i.e. the repo venv):

```bash
MEGASPEED_TICKET_PROJECTS=DEL \
  .venv/bin/python -m ingest import export/del.tgz --db data/del.duckdb

.venv/bin/python -m ingest --verify --db data/del.duckdb
```

`--live` on the one-shot form (`python3 -m ingest --live ...`) is a **dev
shortcut**: it scrapes and imports back-to-back through a real bundle file. It
is not the production path (there is no airgap in one process).

### The Jira half (ticket layer)

Bitbucket gives the PR layer; Jira gives the **ticket layer** — Epic, Story and
Sprint objects plus status transitions and native worklogs — and the two join on
the ticket key (`DEL-100`). It is the same artifact and the same tgz contract,
selected with `--jira`:

```bash
export MEGASPEED_JIRA_BASE_URL=http://127.0.0.1:8090     # the DC 9.17 lab
export MEGASPEED_JIRA_USER=admin MEGASPEED_JIRA_PASSWORD=admin-lab-pw
export MEGASPEED_JIRA_PROJECTS=DEL MEGASPEED_TICKET_PROJECTS=DEL

./scrape/dist/megaspeed-scrape --jira --live --project DEL \
  --out export/del-jira.tgz --state-dir export/del.state
```

Both bundles are imported into the same store with the same
`MEGASPEED_IDENTITY` map, so a Jira assignee and a git author who are the same
human get the same pseudonym (prd/03 "Identity resolution").

What it reads (prd/04) and what it keeps:

| Jira | becomes |
| --- | --- |
| issue snapshot (type, epic link, points, assignee, reporter, created, resolution) | `EPIC` / `STORY` objects (summary → `summary_class`, never the summary) |
| changelog (`expand=changelog`) | `created`, `status_changed` (status *category*), `assigned`, `pointed`, `sprint_added` / `sprint_removed` |
| Agile sprints + sprint membership | `SPRINT` objects (start, planned end, completion, state, `goal_class`) and the `at_start` carry-over signal |
| native worklogs | `worklogged` with `time_spent_seconds`, `log_delay_seconds` = `created − started` and the `log_delay_bucket` — the **worklog-noise** signal |

Jira-side local state, next to the output and never in the tgz:

```
<state-dir>/
  watermark.json    # per-source cursor (shared with the Bitbucket scrape)
  jira_issues.json  # per-issue cursor: {updated, done} (resolved issues settle)
  identity.json     # RAW ZONE: Jira + Bitbucket identities -> pseudonym
```

Incremental: the JQL asks only for `updated >= watermark - overlap`, so a second
run of an unchanged project costs the metadata requests plus a page of issue
keys; an issue this scraper has never seen is always read in full.

### What the scraper keeps locally (never transferred)

```
<state-dir>/
  watermark.json   # per-source event cursor + per-repo updatedDate cursor
  prs.json         # per-PR cursor: {state, updated_date, done} (settled PRs)
  identity.json    # RAW ZONE: slug/email -> pseudonym
  git/             # RAW ZONE: one shallow, partial bare repo per repo, for push_class
```

* **Huge projects are cheap** (prd/14), in four layers:
  1. *Ticket-key pre-filter on the PR list.* The list is read with `limit=100`;
     each row already carries `title`, `fromRef.displayId`, `description`,
     `state` and `updatedDate`. A PR with no target ticket key on any of those
     costs **zero** further requests (no commits, no activities).
  2. *Per-repo `updatedDate` watermark.* A PR that has not moved since the last
     run is skipped.
  3. *Settled finality.* A MERGED/DECLINED PR that was scraped once at its
     current `updatedDate` is marked `done` in `prs.json` and never fetched
     again unless that date moves. Only recently-updated PRs are re-checked.
  4. *NEWEST-first with early termination.* DC 9.4.18 sorts `order=NEWEST` by
     `updatedDate` descending, so pagination stops at the first full page that
     is entirely at/below the watermark — a huge project is walked back only to
     the cursor.
* Incremental: only events newer than `watermark - --overlap-hours` (default
  `72`) are emitted; the emitted watermark never moves backwards. A run that
  changed nothing costs **one list request per repo and nothing else**.
* `--full` ignores both cursors (watermark + settled set) and re-scrapes
  everything — that is how the main side rebuilds a store. The ticket
  pre-filter still applies: an unmatched PR has nothing we would ever store.
* `--no-prefilter` restores the old path (fetch every PR's commits, then
  filter). It exists as an escape hatch and as the measured baseline; it is
  never the default.
* **Back up `identity.json`.** It is the only irreplaceable scrape-side state:
  losing it breaks pseudonym continuity (`LaNo` would become someone else).
  It is never put in the tgz, and no raw content ever leaves the box.
* **Text classification (prd/16) reads raw prose and emits labels only.** Every
  PR description and every human review comment is sent to the local classifier
  (`ingest/classify.py`) **before it is discarded**; what crosses is
  `comment_type` + `thread_outcome` on the THREAD object and
  `question_raised` / `question_answered` events with a `phase`. Backends:
  the local Ollama box (`MEGASPEED_LLM_URL` / `MEGASPEED_LLM_MODEL`, JSON-schema
  constrained decoding, temperature 0) for `--live`, and a deterministic
  keyword mock for `--fixture` (and as the per-call fallback, counted in the
  report, so a dead model never fails a scrape). `--no-text-classify` /
  `MEGASPEED_TEXT_CLASSIFY=0` turns it off; `--text-backend mock` forces the
  mock on a live run. Bot-authored comments are never sent to the model.
  `thread_outcome` is decided by the git cache first (did the diff move at the
  comment anchor?) and by the model's read of the text only when the diff is
  unknown. The classifier version is in the manifest (`text_classifier`) and on
  every THREAD object.
* **`push_class` (prd/16) is deterministic and local.** For every RESCOPED
  activity the scrape compares `git patch-id --stable` of `target...old` vs
  `target...new` in a **shallow, partial** bare repo under `<state-dir>/git/`
  (`--depth`, `--filter=blob:none`, only the refs/commits the matching PRs
  mention — never a mirror). Same patch id → `pure_rebase`; a different one is
  read through `git range-diff` → `rebase_conflict` (same commits, only a few
  changed lines) or `content`. The cache is incremental local state: the head a
  PR had last run is still there when the next rescope reports it as
  `previousFromHash`. Needs the `git` binary on the scrape box;
  `--no-push-class` / `MEGASPEED_PUSH_CLASS=0` turns it off, and a head the
  server no longer serves degrades that one push to `other` instead of failing.

Import is idempotent by `event_key`, so overlapping overlaps and re-transfers
are safe. A bundle is rejected (not merged) if the checksums, `schema_version`,
`classifier_version` or watermark monotonicity do not check out.

### Progress on a long run

A huge project takes minutes, so the scraper writes **one line to stderr** every
30 s (never to stdout, never into the tgz — a piped `--json` report stays clean):

```
[scrape] 02:15 elapsed · 3,214 PRs seen · 412 kept · 2,802 skipped · 1,041 req · 4,120 events · 23.8 PR/s · 7.7 req/s
```

The cadence is time-based, not per PR. `--progress-interval N` changes it (0
disables), `--quiet` silences it, and `MEGASPEED_PROGRESS_INTERVAL` sets the
default. The final summary still prints at the end.

## 2. Build

```bash
scrape/build.sh                 # -> scrape/dist/megaspeed-scrape (PyInstaller --onefile)
scrape/build.sh --zipapp        # stdlib zipapp fallback (needs python3 on target)
scrape/build.sh --pyinstaller   # force PyInstaller, fail if it is missing
PYTHON=/usr/bin/python3.12 scrape/build.sh
```

* Build deps: `scrape/requirements-build.txt` (`pyinstaller>=6.10`). The
  scraper itself is **stdlib only** — no DuckDB, no server deps.
* Artifact: **`scrape/dist/megaspeed-scrape`** (single file, ~10 MB, bundles
  Python and the `ingest` scrape modules). `dist/` and `build/` are gitignored.
* `scrape/build.sh` smoke-tests what it built: `--version`, then a real tiny
  scrape (`--fixture --n 2`) whose tgz is opened and checked. `--no-smoke`
  skips it.
* Nothing secret is baked in. Credentials come from the environment
  (`MEGASPEED_BB_TOKEN`, or `MEGASPEED_BB_USER`/`MEGASPEED_BB_PASSWORD`);
  ticket projects, bot slugs and repos from the environment or a runtime
  `--config <file>.toml`. The artifact ships no config file on purpose, so a
  live run cannot silently inherit the fixture project set (`PROJ,OPS,ARCH`).
* Local fallback check: `bash scrape/build.sh --zipapp` produces the same CLI
  as a 120 kB `.pyz`-style executable, for boxes where PyInstaller is not
  available.

## 3. CLI

```
megaspeed-scrape [--live | --fixture | --fixture-file PATH]
                 [--jira [--project KEY ...] [--limit N]]
                 [--out PATH.tgz] [--state-dir DIR] [--identity PATH]
                 [--overlap-hours N] [--full] [--no-prefilter]
                 [--no-push-class] [--no-text-classify] [--text-backend ollama|mock]
                 [--llm-url URL] [--llm-model MODEL]
                 [--progress-interval SECONDS] [--quiet] [--repo PROJECT/REPO ...]
                 [--config PATH] [--n N] [--seed N] [--probe [--probe-bypass-filter]]
                 [--json] [--version]
```

| flag | meaning |
| --- | --- |
| `--live` | real Bitbucket DC REST (`MEGASPEED_BB_BASE_URL` + creds) |
| `--fixture` / `--fixture-file` | synthetic source, no credentials (default generator: 50 PRs) |
| `--jira` | scrape Jira instead of Bitbucket (`MEGASPEED_JIRA_BASE_URL` + creds); combines with `--live` / `--fixture` / `--probe` |
| `--project KEY` | `--jira`: override the configured projects (repeatable; `MEGASPEED_JIRA_PROJECTS`) |
| `--limit N` | `--jira`: read at most N issues (dev/demo) |
| `--out` | bundle path, default `export/megaspeed-scrape.tgz` (`export/megaspeed-jira.tgz` for `--jira`) |
| `--state-dir` | watermark + prs/issues + identity map, default `<out>.state/` next to the bundle |
| `--identity` | use an existing identity map instead of `<state-dir>/identity.json` |
| `--overlap-hours` | re-emit this far before the watermark (default 72) |
| `--full` | ignore the watermark and the settled-PR / settled-issue sets |
| `--no-prefilter` | fetch every PR's commits before filtering (old, slower path) |
| `--no-push-class` | skip the deterministic `push_class` (no git cache) |
| `--no-text-classify` | skip comment/description labels (no local LLM) |
| `--text-backend` | `ollama` (default live) or `mock` (deterministic, no model) |
| `--llm-url` / `--llm-model` | local Ollama endpoint/model (`MEGASPEED_LLM_URL`, `MEGASPEED_LLM_MODEL`) |
| `--progress-interval` | seconds between stderr progress lines (default 30, `0` = off) |
| `--quiet` | no progress lines (the final summary still prints) |
| `--probe` | read-only diagnostics, writes no bundle |

`python3 -m ingest import <bundle.tgz> [--db PATH] [--json] [--no-verify]` is
the import CLI (`--no-verify` exists for debugging and is not for production).

## 4. CI

`.github/workflows/build-scraper.yml` runs on `push` to `main` and
`pull_request` to `main`, **only when the scraper component changed**:

```
ingest/**   scrape/**   .github/workflows/build-scraper.yml
scrape/requirements-build.txt   requirements*.txt
pyproject.toml   setup.py   setup.cfg   Makefile
```

It installs the build deps, runs the stdlib scrape-side tests
(`python -m unittest discover -s scrape/tests`), builds with
`bash scrape/build.sh --pyinstaller`, smoke-tests the artifact and uploads it as
`megaspeed-scrape-<sha>`. An app-only (`app/**`, `server/**`) change does not
trigger it.

## 5. Tests

```bash
python3 -m unittest discover -s scrape/tests -v      # tgz contract, watermark, pre-filter, no DuckDB
python3 -m unittest discover -s tests -v             # store contract + scrape->import path
```

`scrape/tests/test_optimization.py` is the huge-project suite: 500 PRs of which
only 10 carry a ticket key, counting requests at the transport. It asserts the
pre-filter (5 list + 10 commits + 10 activities, nothing for the other 490), the
watermark/settled skips (ties at the watermark are re-read, never dropped), early
termination (a full page strictly below the watermark ends pagination), the
list-level description match, and the old `--no-prefilter` cost for comparison.
`scrape/tests/test_progress.py` covers the stderr progress reporter (interval
gating, formatting, `--quiet` / `--progress-interval 0`, stdout purity).

## 6. End-to-end check (DEL corpus)

```bash
rm -rf /tmp/opencode/ms-e2e && mkdir -p /tmp/opencode/ms-e2e
export MEGASPEED_BB_BASE_URL=http://127.0.0.1:7991 MEGASPEED_BB_USER=admin
export MEGASPEED_BB_PASSWORD=admin-lab-pw MEGASPEED_TICKET_PROJECTS=DEL
export MEGASPEED_REPOS=DEL/delivery-api,DEL/delivery-web

./scrape/dist/megaspeed-scrape --live \
  --out /tmp/opencode/ms-e2e/del.tgz --state-dir /tmp/opencode/ms-e2e/del.state
.venv/bin/python -m ingest import /tmp/opencode/ms-e2e/del.tgz \
  --db /tmp/opencode/ms-e2e/del.duckdb
.venv/bin/python -m ingest --verify --db /tmp/opencode/ms-e2e/del.duckdb

# incremental: nothing changed -> 2 list requests, 0 events, 0 new on import
./scrape/dist/megaspeed-scrape --live \
  --out /tmp/opencode/ms-e2e/inc.tgz --state-dir /tmp/opencode/ms-e2e/del.state
.venv/bin/python -m ingest import /tmp/opencode/ms-e2e/inc.tgz \
  --db /tmp/opencode/ms-e2e/del.duckdb
```

Expected: `131` PRs kept, `1316` events, `contract OK`; the first run costs
`264` requests (2 list + 131 commits + 131 activities) and the incremental one
`2` (the listing only).

## 7. End-to-end check (Jira DEL corpus)

```bash
export MEGASPEED_JIRA_BASE_URL=http://127.0.0.1:8090
export MEGASPEED_JIRA_USER=admin MEGASPEED_JIRA_PASSWORD=admin-lab-pw
export MEGASPEED_JIRA_PROJECTS=DEL MEGASPEED_TICKET_PROJECTS=DEL

./scrape/dist/megaspeed-scrape --jira --live --project DEL \
  --out /tmp/opencode/ms-e2e/del-jira.tgz --state-dir /tmp/opencode/ms-e2e/del.state
.venv/bin/python -m ingest import /tmp/opencode/ms-e2e/del-jira.tgz \
  --db /tmp/opencode/ms-e2e/del.duckdb
.venv/bin/python -m ingest --verify --db /tmp/opencode/ms-e2e/del.duckdb
```

Expected on the seeded lab: `1` epic, `100` stories, `3` sprints, `199`
worklogs, `472` events, `contract OK`; the first run costs `110` requests
(metadata + 101 issues + 3 sprint memberships). The worklog-noise signal is
visible in `--json`: the lab backdated every log into its sprint but wrote it at
seed time, so most logs land in the `late` bucket.

`tests/test_jira_live.py` is that check as a test: it skips when the lab is not
reachable and asserts the counts above against the real DC 9.17.5.


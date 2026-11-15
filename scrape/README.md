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

### What the scraper keeps locally (never transferred)

```
<state-dir>/
  watermark.json   # per-source event cursor + per-repo updatedDate cursor
  prs.json         # per-PR cursor: {state, updated_date, done} (settled PRs)
  identity.json    # RAW ZONE: slug/email -> pseudonym
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
* Classification (deterministic today; the cheap local LLM later) runs here.
  A classifier upgrade is a scrape-side re-run, not a main-side re-analysis.
  The LLM is an external localhost service (Ollama/llama.cpp) — it is **not**
  bundled into the artifact.

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
                 [--out PATH.tgz] [--state-dir DIR] [--identity PATH]
                 [--overlap-hours N] [--full] [--no-prefilter]
                 [--progress-interval SECONDS] [--quiet] [--repo PROJECT/REPO ...]
                 [--config PATH] [--n N] [--seed N] [--probe [--probe-bypass-filter]]
                 [--json] [--version]
```

| flag | meaning |
| --- | --- |
| `--live` | real Bitbucket DC REST (`MEGASPEED_BB_BASE_URL` + creds) |
| `--fixture` / `--fixture-file` | synthetic source, no credentials (default generator: 50 PRs) |
| `--out` | bundle path, default `export/megaspeed-scrape.tgz` |
| `--state-dir` | watermark + prs + identity map, default `<out>.state/` next to the bundle |
| `--identity` | use an existing identity map instead of `<state-dir>/identity.json` |
| `--overlap-hours` | re-emit this far before the watermark (default 72) |
| `--full` | ignore the watermark and the settled-PR set |
| `--no-prefilter` | fetch every PR's commits before filtering (old, slower path) |
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

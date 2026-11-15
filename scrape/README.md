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
  watermark.json   # per-source cursor: max event ts emitted
  identity.json    # RAW ZONE: slug/email -> pseudonym
```

* Incremental: only events newer than `watermark - --overlap-hours` (default
  `72`) are emitted; the emitted watermark never moves backwards.
  `--full` ignores the cursor and re-scrapes everything.
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
                 [--overlap-hours N] [--full] [--repo PROJECT/REPO ...]
                 [--config PATH] [--n N] [--seed N] [--probe [--probe-bypass-filter]]
                 [--json] [--version]
```

| flag | meaning |
| --- | --- |
| `--live` | real Bitbucket DC REST (`MEGASPEED_BB_BASE_URL` + creds) |
| `--fixture` / `--fixture-file` | synthetic source, no credentials (default generator: 50 PRs) |
| `--out` | bundle path, default `export/megaspeed-scrape.tgz` |
| `--state-dir` | watermark + identity map, default `<out>.state/` next to the bundle |
| `--identity` | use an existing identity map instead of `<state-dir>/identity.json` |
| `--overlap-hours` | re-emit this far before the watermark (default 72) |
| `--full` | ignore the watermark |
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
python3 -m unittest discover -s scrape/tests -v      # tgz contract, watermark, no DuckDB
python3 -m unittest discover -s tests -v             # store contract + import path
```

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
# idempotent: the same bundle again reports 0 new / 0 rewritten
```

Expected: `131` PRs kept, `1316` events, `contract OK`.

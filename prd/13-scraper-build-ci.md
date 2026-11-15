# 13 — Scraper build & CI

The scraper must be a **self-contained build artifact** (buildable locally), and a GitHub
action rebuilds it on merge to `main` **only when the scraper component changed**.

## Build

- The scrape side (source → classify → pseudonymise → tgz) is near-stdlib: `urllib`, `json`,
  `gzip`/`tarfile`, plus the project reducer. The local LLM (future) is **not** bundled — it
  is a provisioned external service (Ollama/llama.cpp on localhost) the scraper calls over HTTP.
- Build = **PyInstaller `--onefile`** → a single binary `megaspeed-scrape`. Fallback (if
  PyInstaller is impractical): a stdlib **zipapp** (`python -m zipapp`).
- Build must **work locally**: `scrape/build.sh` (or a Makefile target) produces the artifact
  the same way CI does. No secrets are baked in — the scraper reads source creds / ticket
  projects / bot slugs from env or a runtime config file.

## CI (`.github/workflows/build-scraper.yml`)

- **Trigger**: `push` to `main` and `pull_request` to `main`.
- **Path filter** (the "if the component changed" gate): run only when `ingest/**`,
  `scrape/**`, the workflow file itself, or any build/requirements file changes.
- **Job**: checkout → setup-python → install build deps → run the same build → upload the
  artifact (Actions artifact / release asset).

## Acceptance

- `scrape/build.sh` works on this box and produces a runnable `megaspeed-scrape` binary.
- CI builds on a scrape-path change and skips on an app-only change.
- End-to-end: scrape bb-lab-b `DEL` → tgz, then import the tgz → store (131 PRs / 1316 events,
  `--verify` OK), proving the artifact runs in a "no-repo, no-venv" way.

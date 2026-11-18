# 32 — App versioning (tag + short SHA in the UI)

The app is near GA; it must show its build identity — the git **tag** and **short SHA** — somewhere
visible, and expose it on the API.

## Current state

- `server/app.py` hardcodes `FastAPI(..., version="0.1.0")` and exposes it on an endpoint.
- The scraper has `ingest.__version__`; there is no app-level version, and no semver tags (only
  `scraper-<sha>` tags).

## Work

1. **Resolve at startup**: the server resolves a version string — `git describe --tags --abbrev=8`
   (tag + short SHA, e.g. `v0.9.0-3-g88f72bb`), falling back to the hardcoded `__version__` when there is
   no `.git` (the airgapped tgz ships a baked `VERSION` file). Keep the pseudonymisation contract: the
   version is a code identity, not tenant data.
2. **Expose**: `GET /api/meta` (or extend the existing version-bearing endpoint) returns
   `{version, tag, sha, build_time}`; `/api/health` keeps its shape.
3. **Display**: show the version in the UI — the footer / a small line in the meta strip — on every page
   and on the phone layout (non-intrusive, per prd/10).
4. **Tag**: cut a first semver tag (e.g. `v0.9.0`) on the current `main` so `git describe` is clean.

## Definition of done

- The UI shows the tag + short SHA (e.g. `v0.9.0 · 88f72bb`) on desktop and phone.
- `/api/meta` returns the resolved tag/sha/version; a missing-git fallback works (VERSION file).
- Node tests cover the rendered version line; server tests cover the endpoint.
- 715 + 73 + 114 green.

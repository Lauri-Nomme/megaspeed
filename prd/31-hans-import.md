# 31 — Bitbucket import from a hans scrape

Import Bitbucket data from a **hans scrape directory** (the read-only REST + git-mirror capture produced
by `~/dev/hans`'s `bb-archiver scrape` during the migration) instead of the live REST API. The user
already holds this scrape, so megaspeed can ingest real PR/commit/comment/review data with no live
credentials.

## hans scrape layout (reference: `~/dev/hans/scrape/golden10/`)

```
rest/pull-requests_<PROJ>_<REPO>.json   # PR list page(s) — /rest/api/1.0/.../pull-requests
rest/pr_<id>.json                       # PR detail  — /rest/api/1.0/.../pull-requests/{id}
rest/pr_<id>_activities.json            # activities — /rest/api/1.0/.../pull-requests/{id}/activities
rest/project_<PROJ>.json · repo_<PROJ>_<REPO>.json · branches_*.json · tags_*.json · users.json
git/                                    # bare mirror: refs/heads, refs/tags, refs/pull-requests/*, stash-refs
index.json                              # manifest (scraped_at, base, project, repo, ids, prs, git, entities)
```

The REST dumps are the raw Bitbucket DC payloads (the same shape megaspeed's `HttpTransport` fetches),
so the reducer is unchanged — only the transport is new.

## Work

1. **`HansTransport`** (in `ingest/transport.py` or a new `ingest/hans.py`): serves megaspeed's Bitbucket
   REST paths (`pull-requests` list/detail, `activities`, `commits`) from the hans `rest/*.json` files,
   paginating the list the way `iter_pull_request_pages` expects. Reads `index.json` to enumerate PR ids.
2. **git mirror wiring**: the transport also exposes the `git/` mirror so `push_class` (patch-id) and
   diff-size classification work from the mirror refs (PR refs → head sha → local git), exactly as the
   live path's shallow git cache does.
3. **CLI/config**: a source flag (e.g. `--hans-scrape <dir>` + `MEGASPEED_HANS_SCRAPE`) that runs the
   Bitbucket half of the scrape offline from the directory, then imports through the normal bundle path.
4. **Pseudonymisation (prd/02)**: users come from `users.json`; comments/descriptions are classified then
   discarded exactly as the live path — the raw comment text never enters the store.

## Definition of done

- `python3 -m ingest scrape --hans-scrape ~/dev/hans/scrape/golden10 ...` produces a store whose PRs,
  commits, activities, reviewers and comment/thread classifications match a live scrape of the same repo
  (the golden10 corpus has a paired live REST capture to diff against).
- `push_class`/diff-size are populated from the mirror (not blank).
- A regression test imports a small hans scrape and asserts object/event counts + the pseudonymisation
  contract (`--verify` passes).
- 715 + 73 green.

## Out of scope

The migration archive assembly itself (that's `~/dev/hans`'s job); this is only the import into megaspeed.

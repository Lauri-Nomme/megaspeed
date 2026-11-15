# corpus state — what is currently in bb-lab-b

Generated 2026-11-15 (UTC) by `corpus/generate.py` + `corpus/backdate.py`,
verified with `corpus/verify.py` and `python3 -m ingest --live --probe`.
Snapshot: `corpus/build/summary.json` (not committed).

## Instance

* `bb-lab-b`, `atlassian/bitbucket:9.4.18-jdk21`, HTTP `http://127.0.0.1:7991`,
  SSH `:7998`, home `hans/lab/home/bb-lab-b`.
* Setup completed with `wizard.sh bb-lab-b` (the `setup.*` properties do not
  self-complete on 9.4.18); license posted from `hans/lab/license.txt`.
* **Licensed**: `GET /rest/api/1.0/admin/license` returns a COMMERCIAL,
  Data Center (`stash.DataCenter=true`), 10-user license; expiry is a rolling
  3-hour dev window (`LicenseExpiryDate=P3H`, refreshed when the license is
  (re)applied). Pushes are accepted — the generation pushed 131 feature
  branches and 107 `main` updates over HTTPS with the team credentials.
* `bb-lab-a` was not touched.

## Corpus

| | |
| --- | --- |
| Project / repos | `DEL` → `delivery-api` (79 PRs), `delivery-web` (52 PRs) |
| Users | alice, bob, carol, dave, erin (`lab-pw-<name>`) |
| Epic | `DEL-1` (referenced by all 131 descriptions) |
| Tickets | `DEL-100` … `DEL-199` (100); 31 tickets have 2 PRs |
| PRs | **131** — 107 merged (81.7%), 14 open (10.7%), 10 declined (7.6%) |
| Commits | 576 (341 api / 235 web), incl. 107 merge commits + 2 scaffolds |
| Activities | OPENED 131, COMMENTED 333, RESCOPED 202, APPROVED 241, UNAPPROVED 12, REVIEWED 4, MERGED 107, DECLINED 10, UPDATED 262 |
| Reviewers | alice 104 (sentinel/lead), bob 56, dave 39, carol 35, erin 28 |
| Approvals | 109 PRs with 2, 11 with 1, 11 with 0 |
| Stacks | 12 stacked PRs (7 merged, 5 open) |
| Needs work | 4 open PRs carry a `NEEDS_WORK` reviewer status |
| PR opened | 2026-08-17 10:57 → 2026-11-12 11:50 UTC |
| PR closed | 2026-08-19 11:51 → 2026-11-13 14:07 UTC |
| Commit dates | 2026-08-13 17:15 → 2026-11-12 17:07 UTC (~90 days) |
| PR lifetime | 23 h … 214 h, median 96 h |

### Shape of the interaction

* Authors rotate across the five users (24–27 PRs each); the reviewer is never
  the author. Most merged PRs have a normal reviewer plus alice as sentinel.
* 1–3 rounds of review: reviewer comment (`BLOCKER` for blocking ones) →
  author pushes a revision (`RESCOPED`) → author reply → approvals → merge.
* 12 `UNAPPROVED` events model "approvals cleared on push"; the reviewer
  re-approves before the merge.
* 10 declined PRs carry a closing comment explaining the decline.
* 14 PRs are still open: 4 need work, some approved but unmerged, a few
  waiting for a first review (they cluster in the last three weeks, which is
  what a real backlog looks like).
* Branch names, commit subjects, PR titles and descriptions all carry the
  ticket key; every description starts with `Epic: DEL-1`.

## Verification performed

* `GET /rest/api/1.0/users/admin` → `name=admin` (setup complete).
* `GET /rest/api/1.0/admin/license` → licensed DC, expiry in the future.
* `git push` over HTTPS as each of the five users (used throughout generation).
* `corpus/verify.py`: 100 tickets, 0 filter-contract problems, 131 PRs.
* `python3 -m ingest --live --probe --repo DEL/delivery-api --repo DEL/delivery-web`:
  269 requests, **131/131 PRs matched, 0 dropped**, 473 PR-attributed commits, activities
  within the expected vocabulary (`UPDATED` is reported as ignored/out-of-vocab,
  which is pre-existing ingest behaviour).
* `corpus/backdate.py --verify`: sampled PRs show `createdDate` equal to the
  plan (±1 s), activities ordered inside the window.

## Limitations

* PR/comment/approval timestamps are backdated by rewriting the embedded H2
  database (`shared/data/db.mv.db`) while the container is stopped — a lab-only
  trick, not supported by Atlassian; a Postgres-backed instance would need a
  different mechanism.
* Merge commits are backdated local merges pushed to `main`; Bitbucket detects
  the merge and flips the PR to `MERGED`. The `MERGED` activity is attributed
  to the pusher (= the PR author).
* The `UPDATED` activities that Bitbucket emits when reviewers are added are
  mapped to the nearest planned event (they carry no dedicated timestamp in the
  plan); they are out of the ingest vocabulary anyway.
* The database rewrite requires a container restart (~40 s) and clears
  in-memory caches; the Lucene search index is not touched.
* Ticket→repo split follows the 20 feature themes (12 backend, 8 frontend), so
  `delivery-api` has more PRs than `delivery-web` (79 vs 52).
* The 3-hour dev license is a rolling window: pushes stop working after it
  expires. Re-apply `hans/lab/license.txt` (admin UI / REST) or recreate the
  lab to renew it; no corpus data is lost either way.

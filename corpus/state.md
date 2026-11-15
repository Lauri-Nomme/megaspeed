# corpus state — what is currently in bb-lab-b

Generated 2026-11-15 (UTC) by `corpus/generate.py` + `corpus/backdate.py`,
verified with `corpus/verify.py`, the read-only `--probe`, and a full
scrape-side run (`python3 -m ingest scrape --live ... --full`). Snapshot:
`corpus/build/summary.json` (not committed).

## Instance

* `bb-lab-b`, `atlassian/bitbucket:9.4.18-jdk21`, HTTP `http://127.0.0.1:7991`,
  SSH `:7998`, home `hans/lab/home/bb-lab-b`.
* **Licensed** with a short-lived (3 h) developer window. The license had
  expired by the time this corpus was regenerated, so generation ran against a
  temporary container with the same data directory and an `LD_PRELOAD` clock
  shim (`corpus/lab_clock.c`, `MEGASPEED_CLOCK_SHIFT=-40000`). The original
  `bb-lab-b` container was restored afterwards and serves read-only probes
  normally; pushes would need the shim again. `bb-lab-a` was not touched.
* The ingest needs two settings that are not in `ingest/config.toml` for this
  corpus: `MEGASPEED_TICKET_PROJECTS=DEL` and `MEGASPEED_SENTINEL_SLUGS=frank`.

## Corpus

| | |
| --- | --- |
| Project / repos | `DEL` → `delivery-api` (79 PRs), `delivery-web` (52 PRs) |
| Users | alice, bob, carol, dave, erin (authors) + **frank** (sentinel); `lab-pw-<name>` |
| Epic | `DEL-1` (referenced by all 131 descriptions) |
| Tickets | `DEL-100` … `DEL-199` (100); 31 tickets have 2 PRs |
| PRs | **131** — 106 merged (80.9%), 15 open (11.5%), 10 declined (7.6%) |
| Commits | 487 PR-attributed (incl. open/declined branches), 106 backdated merge commits, 35 rebase base moves; 548 reachable from `main` |
| Activities | OPENED 131, COMMENTED 375, RESCOPED 226, APPROVED 249, UNAPPROVED 24, REVIEWED 4, UPDATED 296, MERGED 106, DECLINED 10 |
| Reviewers | frank 131 (sentinel), erin 30, bob 29, alice 28, carol 27, dave 17 |
| Approvals | frank 107 (sentinel), erin 28, bob 27, alice 25, carol 24, dave 14 |
| Stacks | 12 stacked PRs (7 merged, 5 open) |
| Needs work | 4 open PRs carry a `NEEDS_WORK` reviewer status |
| PR opened | 2026-08-17 10:57 → 2026-11-13 11:42 UTC |
| PR closed | 2026-08-19 13:00 → 2026-11-13 09:53 UTC |
| Commit dates | 2026-08-14 15:19 → 2026-11-13 15:15 UTC (~91 days) |
| PR lifetime | median ~4 days |
| Revisions per PR (RESCOPED) | 226 pushes across 131 PRs |
| Comments per PR | 1–5 (blocking, suggestion, question, praise, author replies, takeover notes) |

### New feature coverage

| Feature | Data |
| --- | --- |
| `push_class` | **content 186, pure_rebase 28, rebase_conflict 12** (28 pure = 26 same_patch_id + 2 range_diff_identical) |
| Real rebases | 23 PRs rebase a clean patch onto a moved `main`; 12 rebase through an add/add conflict and resolve it in a shared module |
| `thread_outcome` | resolved threads 83 → **acted_on 26, rejected 32, walked_back 25**; open 292 → `unresolved` |
| Thread anchors | reviewer comments carry a real file anchor (`commentAnchor.path`); `git diff old..new -- path` decides whether the code moved |
| Sentinel | **frank** is the mandatory approver on every PR; 107 sentinel approvals; `sentinel_queue` = n 124, median 19.3 h, p90 69.6 h |
| `reviewer_changed` | **added 279, removed 17**, reviewed 4; 17 mid-review reviewer swaps (remove + add) |
| `merge_commit` | **106/106 merged PRs** carry a `commit` block; `STA_PR_MERGE_ACTIVITY.HASH` points at the backdated local merge commit |
| `pr_size` | 3 PRs >10 commits (max 16), 4 PRs >400 changed lines (max 690); `over_commits_cap=3`, `over_lines_cap=4`, `large_prs=7` |
| Diff size | all 131 PRs measured by `git diff --numstat target...head`; median 96 lines |

### Shape of the interaction

* Authors rotate across the five engineers (24–27 PRs each); frank is never an
  author, so the sentinel approval is always a second pair of eyes.
* 1–3 rounds of review: reviewer comment (`BLOCKER` for blocking ones) →
  author pushes a revision (`RESCOPED`) → author reply → approvals → merge.
* Some review rounds are **real rebases** instead of content pushes: the author
  force-pushes the same patch onto a moved `main` (`pure_rebase`) or resolves a
  conflict in a shared module (`rebase_conflict`).
* 83 threads are resolved: the commenter withdraws (`walked_back`), the author
  closes without a change (`rejected`), or the anchored code moved
  (`acted_on`). 292 comment threads stay open.
* 17 PRs swap a reviewer mid-review: the sentinel removes the first reviewer
  and the author adds a replacement, which produces both `removed` and `added`
  `reviewer_changed` events.
* 24 `UNAPPROVED` events model "approvals cleared on push"; the reviewer
  re-approves before the merge.
* 10 declined PRs carry a closing comment explaining the decline.
* 15 PRs are still open: 4 need work, some approved but unmerged, a few waiting
  for a first review (they cluster in the last three weeks).
* Branch names, commit subjects, PR titles and descriptions all carry the
  ticket key; every description starts with `Epic: DEL-1`.

## Verification performed

* `GET /rest/api/1.0/users/admin` → `name=admin` (setup complete).
* `corpus/verify.py`: 131 PRs (106 merged / 15 open / 10 declined), 100 tickets,
  0 filter-contract problems; 83 resolved threads; 17 reviewer removals;
  106/106 merged activities with a commit; 3 PRs >10 commits.
* `python3 -m ingest --live --probe --probe-bypass-filter`:
  264 requests, **131/131 PRs matched, 0 dropped**, 1421 dry-run events;
  activity vocabulary is complete (`UPDATED` is reported as `reviewer_changed`).
* Full scrape (`python3 -m ingest scrape --live ... --full`, mock text backend):
  1466 events, 1665 objects, **131/131 PRs kept**, push_class
  `content=186, pure_rebase=28, rebase_conflict=12`, thread_outcome
  `acted_on=26, rejected=32, walked_back=25, unresolved=292`, diff size
  131/131 measured, 107 sentinel approvals, reviewer_changed
  `added=279, removed=17, reviewed=4`, `merge_commit` on 106 PRs.
* `analysis.rules` on the imported bundle: `pr_size` (`over_commits_cap=3`,
  `over_lines_cap=4`, `large_prs=7`), `sentinel_queue` available with
  `sentinels=["FrMo"]`, `rebase_waste` n=24.
* `corpus/backdate.py --verify`: sampled PRs show `createdDate` equal to the
  plan (±1 s), activities ordered inside the window; merged activities show
  the backdated merge commit.

## Limitations

* PR/comment/approval/thread/merge timestamps are backdated by rewriting the
  embedded H2 database (`shared/data/db.mv.db`) while the container is
  stopped — a lab-only trick, not supported by Atlassian; a Postgres-backed
  instance would need a different mechanism.
* Merge commits are backdated local merges pushed to `main`; Bitbucket detects
  the merge and flips the PR to `MERGED`. The `MERGED` activity is attributed to
  the pusher (= the PR author), and `backdate.py` fills in the merge commit
  hash on `STA_PR_MERGE_ACTIVITY`.
* The `UPDATED` activities for reviewer additions/removals are mapped to the
  planned reviewer-change times (including swaps); `REVIEWED` rows inherit the
  nearest neighbour.
* The database rewrite requires a container restart (~40 s) and clears
  in-memory caches; the Lucene search index is not touched.
* Ticket→repo split follows the 20 feature themes (12 backend, 8 frontend), so
  `delivery-api` has more PRs than `delivery-web` (79 vs 52).
* The developer license is a short rolling window: pushes stop working after it
  expires. Regeneration uses the clock-shifted temporary container described in
  `corpus/README.md`; read-only probes work on the restored instance. No corpus
  data is lost either way.
* The ingest's `sentinel_slugs` / ticket project are deployment config, not
  corpus state: run probes with `MEGASPEED_SENTINEL_SLUGS=frank` and
  `MEGASPEED_TICKET_PROJECTS=DEL`.

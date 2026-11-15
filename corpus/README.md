# `corpus/` — DEL test corpus generator (Bitbucket Data Center 9.4.18)

Generates a realistic, multi-person, multi-ticket pull-request corpus in the
lab instance **bb-lab-b** (`http://127.0.0.1:7991`) for testing megaspeed's
ingest + timeline: ~100 Jira-ticket keys spread over two repositories, six
team members (five authors plus a dedicated sentinel/release captain), ~3
months of working time, reviews, rescopes, real rebases, resolved review
threads, reviewer swaps, approvals, declines, stacks, and a few PRs that are
still open.

Everything is deterministic (seed `20261115`) and resumable.

## What it creates

| Item | Value |
| --- | --- |
| Project | `DEL` ("Delivery Platform") |
| Repos | `DEL/delivery-api` (Python), `DEL/delivery-web` (ES modules) |
| Epic | `DEL-1` — referenced by every PR description (`Epic: DEL-1`) |
| Tickets | `DEL-100` … `DEL-199` (100), 1–2 PRs each → 131 PRs |
| Team | `alice`, `bob`, `carol`, `dave`, `erin` (authors) + `frank` (sentinel) |
| Branches | `DEL-101-add-login-form-validation`, … (`-followup`, `-stack`) |
| Commits | 487 PR-attributed commits (incl. open/declined branches), 106 backdated merge commits, 35 rebase base moves; 548 reachable from `main`, Aug 14 – Nov 13 2026 |
| PR events | Aug 17 – Nov 13 2026 (opened / comments / resolutions / approvals / merge) |

Users (all with `PROJECT_WRITE`); passwords are `lab-pw-<name>`:

| login | name | email | role |
| --- | --- | --- | --- |
| `alice` | Alice Nguyen | alice.nguyen@delivery.example | engineer |
| `bob` | Bob Ferreira | bob.ferreira@delivery.example | engineer |
| `carol` | Carol Osei | carol.osei@delivery.example | engineer |
| `dave` | Dave Kowalski | dave.kowalski@delivery.example | engineer |
| `erin` | Erin Lindqvist | erin.lindqvist@delivery.example | engineer |
| `frank` | Frank Moreau | frank.moreau@delivery.example | release captain / **sentinel** |

(`admin` / `admin-lab-pw` is the lab sysadmin.) Frank never authors a PR, and
every merged PR carries his approval last: that is the mandatory sentinel
approval the `sentinel_queue` metric needs. The ingest only knows the role from
config, so a live scrape of this corpus must set
`MEGASPEED_SENTINEL_SLUGS=frank` (or `[review] sentinel_slugs = ["frank"]` in a
runtime config). `ingest/config.toml` is not part of `corpus/`.

### PR mix (actual run)

| state | count |
| --- | --- |
| MERGED | 106 |
| OPEN | 15 (4 of them with a `NEEDS_WORK` reviewer) |
| DECLINED | 10 |

* 107 PRs have two approvals (sentinel + one rotating reviewer), 10 have one,
  14 have none (declined / still waiting).
* 12 stacked PRs (5 open, 7 merged): the child branch is cut from the parent
  branch, opened against it, retargeted to `main` when the parent merges.
* 31 of the 100 tickets have a second PR (`-followup` or `-stack`).
* 24 explicit un-approvals (`UNAPPROVED`) simulate "approvals cleared on push";
  4 `REVIEWED` status transitions come from `NEEDS_WORK`.
* **Real rebases**: 23 PRs force-push the same patch onto a moved `main`
  (`pure_rebase`) and 12 rebase through a conflict (`rebase_conflict`). The
  base move is a real commit pushed to `main` before the rebase; the classifier
  sees the same patch id or a small range-diff.
* **Review-thread resolution**: 83 threads are resolved (21 by the commenter →
  `walked_back`, 32 by someone else → `rejected`, 26 where the anchored code
  moved → `acted_on`); 292 comment threads stay open (`unresolved`).
* **Reviewer swaps**: 17 PRs remove one reviewer (`removedReviewers`) and add a
  replacement mid-review, producing `reviewer_changed{change=removed}` and a
  second `added` event.
* **Merge commits**: all 106 MERGED activities carry a `commit` block. The
  backdated local merge commit hash is written into
  `STA_PR_MERGE_ACTIVITY.HASH` by `backdate.py`.
* **PR size variation**: 3 PRs have >10 commits (max 16) and 4 have a >400-line
  diff (max 690); `analysis.rules.pr_size` reports `over_commits_cap=3`,
  `over_lines_cap=4`, `large_prs=7`.
* Comment mix: blocking (`severity=BLOCKER`), suggestions, questions, praise,
  author replies, and reviewer-takeover notes. Review latency is hours-to-days;
  PR lifetime median is ~4 days.

## Scripts

```
corpus/generate.py    # users, project, repos, branches, commits, PRs, reviews
corpus/backdate.py    # rewrites PR/comment/approval/thread/merge timestamps in the H2 DB
corpus/verify.py      # checks the filter contract and prints/stores a summary
corpus/lab_clock.c    # lab-only LD_PRELOAD clock shim (see "License" below)
```

Run order (from `megaspeed/`):

```bash
python3 corpus/generate.py        # ~5 min; resumable, skips completed PRs
python3 corpus/backdate.py        # ~2 min; stops/starts bb-lab-b
python3 corpus/verify.py          # ~40 s; writes corpus/build/summary.json
```

Requirements: `bb-lab-b` running and **licensed** (git pushes are rejected
without a license), `git`, `python3` (stdlib only), a JDK plus the H2 jar for
backdating (`H2_JAR`, default `/tmp/opencode/h2-shell.jar`, extracted from the
image with `nerdctl cp bb-lab-b:/opt/atlassian/bitbucket/app/WEB-INF/lib/h2-2.1.214.jar`).

### License / the clock-shift workaround (lab-only)

The lab license is a short-lived developer window. Once it expires, REST reads
still work but **all git pushes are rejected** with
`License limit exceeded`; a regeneration therefore needs a clock that is inside
the license window. The host clock must not be changed (it is shared with
`bb-lab-a`). Instead, run the generator against a temporary container with the
same data directory and an `LD_PRELOAD` shim that shifts `CLOCK_REALTIME` for
that JVM only:

```bash
gcc -shared -fPIC -O2 -o /tmp/opencode/clock_shift.so corpus/lab_clock.c -ldl
sudo nerdctl stop bb-lab-b && sudo nerdctl rm bb-lab-b
sudo cp /tmp/opencode/clock_shift.so /vokk/home/lauri/dev/hans/lab/home/bb-lab-b/clock_shift.so
sudo nerdctl run -d --name bb-lab-b-gen --restart no \
  -p 7991:7990 -p 7998:7999 \
  -e JVM_MINIMUM_MEMORY=4g -e JVM_MAXIMUM_MEMORY=4g \
  -e LD_PRELOAD=/var/atlassian/application-data/bitbucket/clock_shift.so \
  -e MEGASPEED_CLOCK_SHIFT=-40000 \
  -v /vokk/home/lauri/dev/hans/lab/home/bb-lab-b:/var/atlassian/application-data/bitbucket \
  docker.io/atlassian/bitbucket:9.4.18-jdk21
# wait for /status == RUNNING, then:
BB_CONTAINER=bb-lab-b-gen python3 corpus/backdate.py
# finally restore the unshifted container:
sudo nerdctl stop bb-lab-b-gen && sudo nerdctl rm bb-lab-b-gen
sudo nerdctl run -d --name bb-lab-b --restart unless-stopped \
  -p 7991:7990 -p 7998:7999 -e JVM_MINIMUM_MEMORY=4g -e JVM_MAXIMUM_MEMORY=4g \
  -v /vokk/home/lauri/dev/hans/lab/home/bb-lab-b:/var/atlassian/application-data/bitbucket \
  docker.io/atlassian/bitbucket:9.4.18-jdk21
```

`MEGASPEED_CLOCK_SHIFT` is seconds (negative = past). The shim is lab-only and
never part of the ingest artifact. After the data is generated, the restored
(expired-license) instance is still fine for read-only probes/scrapes.

### Resumability / re-running

* `generate.py` keeps `build/plan.json` (deterministic plan) and
  `build/state.json` (per-PR progress). A rerun skips PRs marked done and
  reuses existing users/project/repos/branches/PRs.
* The plan carries the generated `merge_commit`, `thread_ids` and `resolved`
  timestamps per PR; `backdate.py` uses them to rewrite the H2 rows.
* `backdate.py` derives the mapping from the plan, so it is idempotent; it
  stops the container, edits a copy of `db.mv.db`, copies it back, restarts
  and verifies through REST. The pre-edit database is kept at
  `build/backdate/db.backup.mv.db` and restored automatically if anything fails.
* `verify.py` is read-only.

### Reset (regenerate from scratch)

```bash
# delete the corpus in Bitbucket (repos first, the project refuses otherwise)
for r in delivery-api delivery-web; do
  curl -u admin:admin-lab-pw -X DELETE \
    http://127.0.0.1:7991/rest/api/1.0/projects/DEL/repos/$r
done
curl -u admin:admin-lab-pw -X DELETE http://127.0.0.1:7991/rest/api/1.0/projects/DEL
rm -rf corpus/build
python3 corpus/generate.py && python3 corpus/backdate.py
```

If the license has expired, use the clock-shifted temporary container described
above and run `BB_CONTAINER=bb-lab-b-gen python3 corpus/backdate.py`.

`corpus/build/` is git-ignored (`build/` is in the repo-wide `.gitignore`).

## Timestamps: how backdating works (and its limits)

Bitbucket DC assigns `createdDate = now` to every PR/comment/approval; there is
no API to set it. The corpus therefore has two time layers:

1. **Git** — commits (including merge commits) are created with
   `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` and the acting user's name/e-mail,
   so history is genuinely spread over 2026-08-14 … 2026-11-13.
2. **PR events** — written by REST "now", then rewritten by `backdate.py`:
   * it stops the container and edits `shared/data/db.mv.db` (H2) directly;
   * every PR activity row (`STA_ACTIVITY.CREATED_TIMESTAMP`) is mapped to the
     planned event of the same kind (`OPENED`, `COMMENTED`, `RESCOPED`,
     `APPROVED`, `MERGED`, `DECLINED`); `UPDATED` rows map to the planned
     reviewer add/remove times (including reviewer swaps); `REVIEWED` rows
     inherit the nearest neighbour;
   * `BB_COMMENT` / `BB_COMMENT_THREAD` and the `STA_PULL_REQUEST`
     created/updated/closed/rescoped columns are updated too; resolved threads
     get their `RESOLVED_TIMESTAMP` rewritten to the planned resolution time;
   * `STA_PR_MERGE_ACTIVITY.HASH` is set to the backdated local merge commit,
     so the REST `MERGED` activity carries a real `commit` block;
   * the container is restarted and the result verified through REST
     (`createdDate` equals the plan, activities stay inside the window).

Known limitations:

* This is a lab-only trick on an **embedded H2** database. It is not supported
  by Atlassian, must never be pointed at a real instance, and would need a
  different mechanism for Postgres.
* Merge commits are created *locally* and pushed to `main`; Bitbucket then
  auto-detects the merge and flips the PR to `MERGED`. The `MERGED` activity is
  attributed to the user who pushed `main` (the PR author), and the activity's
  commit hash is filled in by `backdate.py`.
* The database rewrite resets Bitbucket's in-memory caches via the restart;
  search/Lucene indices are not touched (they are rebuilt lazily).
* Timestamps are UTC; git commit dates and PR dates may differ by a few
  seconds from the exact planned minute (rounding in the SQL writer).
* The three-hour dev license expires quickly. REST reads keep working; pushes
  need the clock-shift workaround above. `bb-lab-a` is never touched.

## Filter contract

Every PR matches `\b[A-Z]+-\d+\b` in **all** of:

* branch name — `DEL-137-saved-addresses-crud`
* commit messages — `DEL-137: saved addresses crud`, `DEL-137: address review feedback (rev 1)`
* PR title — `DEL-137: Saved addresses CRUD`
* PR description — `Epic: DEL-1` + `Ticket: DEL-137` + `Implements: DEL-137`

`python3 -m ingest --live --probe --repo DEL/delivery-api --repo DEL/delivery-web`
against bb-lab-b reports **all 131 PRs matched, 0 dropped**.

## What the ingest sees (verified run)

The scrape-side classifier needs the ticket project and the sentinel slug
(neither is in `ingest/config.toml` for `DEL`):

```bash
export MEGASPEED_BB_BASE_URL=http://127.0.0.1:7991
export MEGASPEED_BB_USER=admin MEGASPEED_BB_PASSWORD=admin-lab-pw
export MEGASPEED_TICKET_PROJECTS=DEL
export MEGASPEED_SENTINEL_SLUGS=frank
export MEGASPEED_TEXT_BACKEND=mock
.venv/bin/python -m ingest scrape --live \
  --repo DEL/delivery-api --repo DEL/delivery-web \
  --out /tmp/del.tgz --state-dir /tmp/del.state --full --quiet
```

Reported by that run (2026-11-15):

| Metric | Value |
| --- | --- |
| PRs | 131 seen → 131 kept, 0 dropped |
| events / objects | 1466 / 1665 |
| `push_class` | content 186, **pure_rebase 28**, **rebase_conflict 12** |
| push reasons | same_patch_id 26, range_diff_identical 2, conflict_hunks 12, commits_changed 186 |
| `thread_outcome` | acted_on 26, rejected 32, walked_back 25, unresolved 292 |
| `comment_type` | blocker 53, suggestion 282, question 36, nit 4 |
| `reviewer_changed` | added 279, **removed 17**, reviewed 4 |
| sentinel approvals | 107 (`sentinel=true`, all by frank) |
| `merge_commit` on PRs | 106 (all merged PRs) |
| diff size | 131 PRs measured (max 690 changed lines) |
| questions | 36 raised, 9 answered |

`analysis.rules.pr_size` on the imported bundle: `over_commits_cap=3`,
`over_lines_cap=4`, `large_prs=7`, median 5 commits / 96 changed lines.
`sentinel_queue`: available, sentinel `FrMo`, n=124, median 19.3 h, p90 69.6 h.

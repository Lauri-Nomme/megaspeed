# `corpus/` — DEL test corpus generator (Bitbucket Data Center 9.4.18)

Generates a realistic, multi-person, multi-ticket pull-request corpus in the
lab instance **bb-lab-b** (`http://127.0.0.1:7991`) for testing megaspeed's
ingest + timeline: ~100 Jira-ticket keys spread over two repositories, five
team members, ~3 months of working time, reviews, rescopes, approvals,
declines, stacks, and a few PRs that are still open.

Everything is deterministic (seed `20261115`) and resumable.

## What it creates

| Item | Value |
| --- | --- |
| Project | `DEL` ("Delivery Platform") |
| Repos | `DEL/delivery-api` (Python), `DEL/delivery-web` (ES modules) |
| Epic | `DEL-1` — referenced by every PR description (`Epic: DEL-1`) |
| Tickets | `DEL-100` … `DEL-199` (100), 1–2 PRs each → 131 PRs |
| Team | `alice`, `bob`, `carol`, `dave`, `erin` |
| Branches | `DEL-101-add-login-form-validation`, … (`-followup`, `-stack`) |
| Commits | ~576 (incl. 107 backdated merge commits), Aug 13 – Nov 12 2026 |
| PR events | Aug 17 – Nov 13 2026 (opened / comments / approvals / merge) |

Users (all with `PROJECT_WRITE`); passwords are `lab-pw-<name>`:

| login | name | email |
| --- | --- | --- |
| `alice` | Alice Nguyen | alice.nguyen@delivery.example |
| `bob` | Bob Ferreira | bob.ferreira@delivery.example |
| `carol` | Carol Osei | carol.osei@delivery.example |
| `dave` | Dave Kowalski | dave.kowalski@delivery.example |
| `erin` | Erin Lindqvist | erin.lindqvist@delivery.example |

(`admin` / `admin-lab-pw` is the lab sysadmin; alice doubles as the *sentinel*
approver, i.e. the team lead whose approval every merged PR needs — bob stands
in when alice is the author.)

### PR mix (actual run)

| state | count |
| --- | --- |
| MERGED | 107 |
| OPEN | 14 (4 of them with a `NEEDS_WORK` reviewer) |
| DECLINED | 10 |

* 109 PRs have two approvals (sentinel + one rotating reviewer), 11 have one,
  11 have none (declined / still waiting).
* 12 stacked PRs (5 open, 7 merged): the child branch is cut from the parent
  branch, opened against it, retargeted to `main` when the parent merges.
* 31 of the 100 tickets have a second PR (`-followup` or `-stack`).
* 12 explicit un-approvals (`UNAPPROVED`) simulate "approvals cleared on push";
  4 `REVIEWED` status transitions come from `NEEDS_WORK`.
* Comment mix: blocking (`severity=BLOCKER`), suggestions, questions, praise,
  plus author replies. Review latency is hours-to-days; PR lifetime median is
  ~4 days.

## Scripts

```
corpus/generate.py    # users, project, repos, branches, commits, PRs, reviews
corpus/backdate.py    # rewrites PR/comment/approval timestamps in the H2 DB
corpus/verify.py      # checks the filter contract and prints/stores a summary
```

Run order (from `megaspeed/`):

```bash
python3 corpus/generate.py        # ~2 min; resumable, skips completed PRs
python3 corpus/backdate.py        # ~2 min; stops/starts bb-lab-b
python3 corpus/verify.py          # ~20 s; writes corpus/build/summary.json
```

Requirements: `bb-lab-b` running and **licensed** (git pushes are rejected
without a license), `git`, `python3` (stdlib only), a JDK plus the H2 jar for
backdating (`H2_JAR`, default `/tmp/opencode/h2-shell.jar`, extracted from the
image with `nerdctl cp bb-lab-b:/opt/atlassian/bitbucket/app/WEB-INF/lib/h2-2.1.214.jar`).

### Resumability / re-running

* `generate.py` keeps `build/plan.json` (deterministic plan) and
  `build/state.json` (per-PR progress). A rerun skips PRs marked done and
  reuses existing users/project/repos/branches/PRs.
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

`corpus/build/` is git-ignored (`build/` is in the repo-wide `.gitignore`).

## Timestamps: how backdating works (and its limits)

Bitbucket DC assigns `createdDate = now` to every PR/comment/approval; there is
no API to set it. The corpus therefore has two time layers:

1. **Git** — commits (including merge commits) are created with
   `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE` and the acting user's name/e-mail,
   so history is genuinely spread over 2026-08-13 … 2026-11-12.
2. **PR events** — written by REST "now", then rewritten by `backdate.py`:
   * it stops `bb-lab-b` and edits `shared/data/db.mv.db` (H2) directly;
   * every PR activity row (`STA_ACTIVITY.CREATED_TIMESTAMP`) is mapped to the
     planned event of the same kind (`OPENED`, `COMMENTED`, `RESCOPED`,
     `APPROVED`, `MERGED`, `DECLINED`); `UPDATED`/`REVIEWED` rows inherit the
     nearest neighbour;
   * `BB_COMMENT` / `BB_COMMENT_THREAD` and the `STA_PULL_REQUEST`
     created/updated/closed/rescoped columns are updated too;
   * the container is restarted and the result verified through REST
     (`createdDate` equals the plan, activities stay inside the window).

Known limitations:

* This is a lab-only trick on an **embedded H2** database. It is not supported
  by Atlassian, must never be pointed at a real instance, and would need a
  different mechanism for Postgres.
* Merge commits are created *locally* and pushed to `main`; Bitbucket then
  auto-detects the merge and flips the PR to `MERGED`. The `MERGED` activity is
  therefore attributed to the user who pushed `main` (the PR author) — the same
  person Bitbucket would attribute a server-side merge to.
* The database rewrite resets Bitbucket's in-memory caches via the restart;
  search/Lucene indices are not touched (they are rebuilt lazily).
* Timestamps are UTC; git commit dates and PR dates may differ by a few
  seconds from the exact planned minute (rounding in the SQL writer).

## Filter contract

Every PR matches `\b[A-Z]+-\d+\b` in **all** of:

* branch name — `DEL-137-saved-addresses-crud`
* commit messages — `DEL-137: saved addresses crud`, `DEL-137: address review feedback (rev 1)`
* PR title — `DEL-137: Saved addresses CRUD`
* PR description — `Epic: DEL-1` + `Ticket: DEL-137` + `Implements: DEL-137`

`python3 -m ingest --live --probe --repo DEL/delivery-api --repo DEL/delivery-web`
against bb-lab-b reports **all 131 PRs matched, 0 dropped**.

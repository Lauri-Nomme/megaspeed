# jira-lab — Jira Data Center fixture for megaspeed ingest testing

A local, scripted Jira **Data Center** instance that mirrors the Bitbucket DC
corpus (`corpus/generate.py`, project `DEL`) so the ingest can pull
Story/Epic/Sprint + worklogs from Jira and join them with the PR data from the
Bitbucket labs (`~/dev/hans/lab/bb-lab-a|b`).

Everything here is unattended and repeatable: container bring-up, the Jira
setup wizard, the data seed and the verification are four scripts. No browser
clicks, no manual license entry.

| | |
|---|---|
| Container | `jira-lab` (nerdctl, `--restart unless-stopped`) |
| Image | `atlassian/jira-software:9.17.5-jdk17` (Jira Software DC 9.17 LTS) |
| URL | <http://localhost:8090> |
| Sysadmin | `admin` / `admin-lab-pw` |
| Data | `jira-lab/home/jira-lab` (bind mount of `$JIRA_HOME`) |
| License | Atlassian's public **timebomb** Jira Software DC license (10 users, non-expiring on 9.17.5) |

## Quick start

```bash
./jira-lab/lab-up.sh      # pull image, start container, wait for FIRST_RUN
./jira-lab/setup.py       # walk the setup wizard (license, base URL, admin)
./jira-lab/seed.py        # project DEL, epic DEL-1, DEL-100..199, sprints, worklogs
./jira-lab/backdate.py    # put the stories on the PR timeline + set story points
./jira-lab/verify.py      # REST checks (exit 0 = fixture is intact)
```

Total wall clock on this host: ~2 min for `lab-up.sh` + `setup.py`, ~15 s for
`seed.py` once the app is warm, ~1 min for `backdate.py` (it stops/restarts the
container once).

Reset to a blank instance (needed to re-arm the license window):

```bash
PURGE=1 ./jira-lab/lab-down.sh && ./jira-lab/lab-up.sh
./jira-lab/setup.py && ./jira-lab/seed.py && ./jira-lab/backdate.py
```

## Licensing (the one non-obvious part)

Jira has **no** unattended-setup properties file (unlike Bitbucket's
`bitbucket.properties` + `setup.*` keys), so the wizard is driven over HTTP by
`setup.py`. The license itself is Atlassian's published **3-hour timebomb
license** for "Jira Software Data Center, 10 users":

<https://developer.atlassian.com/platform/marketplace/timebomb-licenses-for-testing-server-apps/>

* no Atlassian account, no my.atlassian.com, no clock shifting, no patching of
  the product (the Bitbucket lab's `hack-unlicensed.sh` is *not* needed here —
  Jira has no unlicensed path, and it doesn't have to);
* the key lives in `jira-lab/license.txt` (single line) and is applied by the
  wizard's license step;
* **the 3-hour window is real — but it applies to the *application* license,
  not the platform one.** The startup dump (`___ License Details ___`) reports
  the *platform* license (`Evaluation: false`, `SEN-500`, no expiry date) and
  that one genuinely never expires; reading only that dump is what makes this
  trap easy to fall into. The Jira Software **application** license
  (`GET /rest/plugins/applications/1.0/installed/jira-software/license`) is
  refreshed by Jira **on every start** and expires exactly 3 hours after that
  start. Measured on this host:

  | Jira start | app license expiry | delta |
  |---|---|---|
  | 17 Nov 01:22:17 | 17 Nov 04:22:25 | 3 h 07 s |
  | 17 Nov 01:29:45 | 17 Nov 04:29:51 | 3 h 05 s |

  So the fixture is healthy for ~3 h of uptime at a time:
  * while the window is open everything works;
  * once it lapses the Software application stays **enabled** — so `/rest/agile`
    still exists and answers **403**, *not* 404 — while every `/rest/api/2`
    call keeps working. The instance looks fine and only boards/sprints break.
    This is **not** the "Jira Core" quirk below (that one 404s);
  * **repair: restart the container** (`sudo nerdctl restart jira-lab`) — that
    re-arms the window. `./jira-lab/setup.py` now detects the lapse and does
    exactly that (`--no-fix-license` skips it). Re-posting `license.txt` over
    REST is *not* an option: the UPM application-license endpoint rejects
    headless updates with 405/415, it only accepts the interactive
    *Manage apps → Jira Software → License* form;
  * a lapsed app license does **not** make Jira read-only and never touches
    data; it only closes Agile REST. (Read-only mode would mean the *platform*
    license expired, which does not happen with this key on 9.17.5.)
* 10 seats, 7 used (admin + alice/bob/carol/dave/erin/frank).

`jira-lab/app-license.txt` holds the matching *Data Center app* timebomb
license; it is unused because Tempo could not be installed (see below).

### Troubleshooting: `/rest/agile/1.0/board` → 403

| Symptom | Meaning | Fix |
|---|---|---|
| `/rest/api/2/*` 200, `/rest/agile/*` **403**, app `enabled` in the startup dump | the 3-hour **application** license window lapsed (uptime > 3 h) | `./jira-lab/setup.py`, or `sudo nerdctl restart jira-lab` |
| `/rest/agile/*` **404**, `/rest/api/2/project/type/software/accessible` 404 | the app was never enabled — the first-boot / "Jira Core" quirk | `./jira-lab/setup.py` (`--fix-app`, default) |
| `/rest/agile/*` **503** just after a start | plugins still warming up; `/status` says `RUNNING` too early | wait 30–60 s |

Diagnose the license in one shot (note `Accept: */*` — this endpoint answers
`406` to `Accept: application/json`):

```bash
curl -s -u admin:admin-lab-pw -H 'Accept: */*' \
  http://localhost:8090/rest/plugins/applications/1.0/installed/jira-software/license \
  | python3 -m json.tool | grep -E 'expiryDateString|expired|valid'
```

For a lab that must stay up longer than 3 h, restart it on a timer — restarts
are safe (data and license live in the bind-mounted home) and only cost ~40 s:

```cron
# host crontab: keep the 3-hour app-license window open
17 */2 * * * sudo nerdctl restart jira-lab >/dev/null 2>&1
```

## What is seeded

| Thing | Value |
|---|---|
| Project | `DEL` "Delivery Platform", company-managed Scrum (board type `scrum`) |
| Epic | `DEL-1` "Delivery platform epic" |
| Stories | `DEL-100` … `DEL-199` (100), same 20 themes/titles as the Bitbucket corpus |
| Assignees | `AUTHORS[(i * 7 + 2) % 5]` — exactly the Bitbucket PR author rotation |
| Epic link | every story linked to `DEL-1` (field id is discovered at runtime) |
| Sprints | `DEL Sprint 1` (closed, DEL-100..129, 2026-08-17 → 09-10), `DEL Sprint 2` (closed, DEL-130..159, 2026-09-11 → 10-07), `DEL Sprint 3` (active, DEL-160..189, 2026-10-08 → 11-27) |
| Backlog | DEL-190..199 |
| Statuses | after `backdate.py`: every story with a merged PR is `Done`, the rest `In Progress` (no fake `To Do`) |
| Story points | Fibonacci-like 1/2/3/5/8/13 from the story's changed lines (set by `backdate.py`) |
| Worklogs | 199 **native Jira worklogs**, 1–3 per story, authored by the assignee (sometimes a second engineer), backdated into each story's active window and logged same/next day |
| Timeline | `created` / refinement / sprint pull / in-progress / done follow the story's PRs (2026-08-17 … 11-12), not the seed instant |
| Users | alice, bob, carol, dave, erin, frank (+ admin), password `lab-pw-<name>`, Jira Software access |

Descriptions carry `Epic: DEL-1`, `Ticket: DEL-xxx`, `Area:` and `Repo:` — the
same shape as the Bitbucket PR descriptions, so both sources join on the ticket
key. The seed is deterministic (fixed RNG seed `20261115`) and idempotent:
re-running skips anything that already exists.

## Backdating onto the PR timeline

The seed writes every story at one instant, while the Bitbucket PRs the stories
are about were opened and merged between 2026-08-13 and 2026-11-12. Read
together, that makes the Jira-layer "merge → done" segment a fake ~36-day
artifact. `backdate.py` fixes the ticket side:

1. it reads each story's first PR open and last merged close from the megaspeed
   store (`data/del-combined.duckdb`, falling back to `data/del3.duckdb`);
2. it sets story points over REST (adding `Story Points` to the DEL screens
   first — the scrum template leaves it off), so Jira writes a real changelog
   row;
3. it stops `jira-lab`, edits the embedded H2 database, and restarts:
   * `JIRAISSUE.CREATED` / `UPDATED` / `RESOLUTIONDATE`,
   * the status changelog as the two-step `To Do → In Progress → Done` (the seed
     wrote a direct `To Do → Done`, which is why "sprint → in progress" had no
     data),
   * the `Sprint` and `Story Points` changelog timestamps,
   * `WORKLOG.STARTDATE` / `CREATED` (inside the story's active window, logged
     same/next day),
   * the sprint start / end / complete dates and the workflow history tables;
4. it restarts the container, rebuilds the search index and verifies through
   REST.

The plan is derived from the PR timeline and a fixed RNG seed, not from the
current timestamps, so re-running is idempotent. The intended shape per story
is: created in the backlog → refined (story points) → pulled into the sprint →
in progress shortly before the first PR opens → done shortly after the last PR
merges. A story whose PRs never merged stays `In Progress` (the honest state).

```bash
python3 jira-lab/backdate.py --plan-only   # print the timeline, change nothing
python3 jira-lab/backdate.py               # apply + verify
python3 jira-lab/backdate.py --verify      # only the REST check (container up)
```

After it runs, re-scrape Jira with `--full` (the backdated `updated` values are
older than the previous watermark) and re-import into the combined store:

```bash
MEGASPEED_JIRA_BASE_URL=http://127.0.0.1:8090 \
MEGASPEED_JIRA_USER=admin MEGASPEED_JIRA_PASSWORD=admin-lab-pw \
MEGASPEED_JIRA_PROJECTS=DEL MEGASPEED_TICKET_PROJECTS=DEL \
  .venv/bin/python -m ingest scrape --jira --live --project DEL --full \
    --out /tmp/jira.tgz --state-dir /tmp/jira.state --identity data/identity.json
MEGASPEED_TICKET_PROJECTS=DEL \
  .venv/bin/python -m ingest import /tmp/jira.tgz --db data/del-combined.duckdb
```

## Rebuilding the combined store on the real workflow

`jira-lab` runs Atlassian's default workflow (To Do / In Progress / Done), so a
live scrape of it cannot carry `in_review` / `in_verification`: both statuses are
category *indeterminate* and fold into `in progress`. Installing the
installation's seven-status Story / six-status Bug workflow into Jira DC 9.17 is
fragile (no public REST, a finicky XML import, a healthy lab at risk), so
`rebuild_combined.py` takes the deterministic path:

1. read each ticket's first PR open / last merge from the **Bitbucket** store
   (`data/del3.duckdb`, the unchanged 131-PR corpus);
2. generate the DEL Jira bundle on the real Story (`OPEN → READY FOR
   REFINEMENT → REFINED → IN PROGRESS → IN REVIEW → IN VERIFICATION → CLOSED`)
   and Bug (`NEW → TRIAGED → … → RESOLVED`) workflows, with the rework / reopen
   back-transitions, backdated onto those PRs (`ingest/jira_fixtures.py`,
   `python3 -m ingest.jira_fixtures --del --timeline-store …`);
3. scrape that bundle through the real fixture transport with `[jira.stages]`
   configured, so the stage labels ride the bundle and its manifest;
4. import it into a **copy** of the Bitbucket store under `data/identity.json`
   (one pseudonym per human across both sources), verify the contract and the
   QA segment, and only then replace `data/del-combined.duckdb`.

```bash
python3 jira-lab/rebuild_combined.py              # build + verify + replace
python3 jira-lab/rebuild_combined.py --check-only # re-read the stages/QA segment
```

The result carries `ready=105`, `in_review=130`, `in_verification=96`,
`done=92` status transitions, and the Jira layer measures the QA segment
(`verification → done`) separately from `merge → verification`.

## REST verification

`verify.py` runs 27 checks; the interesting endpoints are:

```bash
# epic + stories (JQL)
curl -s -u admin:admin-lab-pw \
  'http://localhost:8090/rest/api/2/search?jql=project=DEL%20AND%20issuetype=Story&maxResults=3&fields=summary,assignee,status'

# epic
curl -s -u admin:admin-lab-pw http://localhost:8090/rest/api/2/issue/DEL-1

# board + sprints (Agile REST)
curl -s -u admin:admin-lab-pw 'http://localhost:8090/rest/agile/1.0/board?projectKeyOrId=DEL'
curl -s -u admin:admin-lab-pw 'http://localhost:8090/rest/agile/1.0/board/1/sprint'

# sprint membership
curl -s -u admin:admin-lab-pw 'http://localhost:8090/rest/agile/1.0/sprint/3/issue?maxResults=2'

# worklogs (native, per issue)
curl -s -u admin:admin-lab-pw http://localhost:8090/rest/api/2/issue/DEL-100/worklog

# worklogs in bulk (issue search with the worklog field)
curl -s -u admin:admin-lab-pw \
  'http://localhost:8090/rest/api/2/search?jql=key%20in%20(DEL-100,DEL-101)&fields=worklog'
```

Observed result on the seeded lab:

```
  [ok] Jira version 9.17.5
  [ok] Jira Software DC license: 10 seats, 7 used
  [ok] Jira Software app license valid until 17 Nov 2026 (Jira grants 3 h per start)
  [ok] project DEL exists / project type 'software'
  [ok] epic DEL-1 = 'Delivery platform epic'
  [ok] story count = 100 (expected 100)
  [ok] keys are DEL-100..DEL-199
  [ok] 100/100 stories linked to DEL-1 (field customfield_10100)
  [ok] assignees match the Bitbucket author rotation
  [ok] story points set on 100/100 stories (Fibonacci 1/2/3/5/8/13)
  [ok] story created dates span 92 days (2026-08-06 .. 2026-11-06) — backdate.py put them on the PR timeline
  [ok] board 1 type 'scrum'
  [ok] sprint 'DEL Sprint 1' state=closed / 'DEL Sprint 2' state=closed / 'DEL Sprint 3' state=active
  [ok] sprint membership {'DEL Sprint 1': 30, 'DEL Sprint 2': 30, 'DEL Sprint 3': 30}
  [ok] 199 native worklogs across 100 stories
  [ok] worklog authors = {'alice': 45, 'bob': 40, 'carol': 38, 'dave': 38, 'erin': 38}
27/27 checks passed
```
## Tempo: NOT installed (documented fallback)

Tempo Timesheets could not be installed in this lab, and worklogs are therefore
**native Jira worklogs** (which is also what the Jira REST API exposes without
any add-on):

* the Marketplace artifact is not fetchable unattended — every documented
  download URL (`/download/apps/<id>/version/<build>`,
  `/download/plugins/<key>/versions/<id>`, the `binary` link from the
  Marketplace API) returns the Marketplace SPA shell or 404 without a browser
  session / Atlassian ID login, and Jira's UPM install-from-URL uses the same
  endpoint;
* the DC builds the public API exposes (Tempo 19.6/20.x) target Jira 10/11, not
  9.17; the Jira-9 line is not listed without paging deep into the version
  history;
* even with the jar, Tempo needs its own app license and its own setup wizard
  (Tempo Platform/Accounts bootstrap), which is more manual work than the lab
  warrants.

`jira-lab/app-license.txt` (Atlassian's public 3-hour DC **app** timebomb
license) is committed so a future attempt has the licensing piece ready:
`PUT /rest/plugins/1.0/<pluginKey>-key/license` with that key. If Tempo is ever
installed, its worklogs are the same records (`/rest/tempo-timesheets/4/worklogs`
reads the native worklog store), so the ingest contract does not change.

## Notes / known quirks

* **Jira Software app install ordering.** The `jira-software` image installs
  the Jira Software application from
  `WEB-INF/application-installation/jira-software-application` on the very
  first boot — which happens *before* the setup wizard, i.e. with no license in
  the database. Jira then writes `$JIRA_HOME/plugins/install-app-info` without
  enabling the app, and a plain restart does not re-run the installation: the
  instance stays "Jira Core" (no `/rest/agile`, no `software` project type).
  `setup.py` detects this (`/rest/api/2/project/type/software/accessible`),
  clears the marker, restarts once and re-checks. `--no-fix-app` skips it.
* **Jira Software app license is a 3-hour window per start.** Distinct from the
  install-ordering quirk above (that one 404s): once the instance has been up
  for ~3 h the *application* license lapses while the app stays enabled, so
  `/rest/agile/*` answers **403** and `/rest/api/2/*` is unaffected. A restart
  re-arms it; `setup.py` does that automatically. See
  *Troubleshooting: `/rest/agile/1.0/board` → 403* above.
* **The wizard's admin step** creates the account but re-renders the step
  instead of redirecting (reproduced on every fresh 9.17.5 install); `setup.py`
  notices the side effect and continues at the mail step.
* **Issue keys.** Jira numbers issues sequentially and exposes no API to set
  the counter, so `seed.py` creates 98 filler issues after `DEL-1` and deletes
  them again to make the stories land on `DEL-100..DEL-199`. The project only
  ever contains the epic plus the 100 stories.
* **Created/updated timestamps** are the seed time until `backdate.py` runs
  (Jira does not accept them via REST). `backdate.py` rewrites them directly in
  the H2 database, together with the changelog and worklog timestamps; see
  *Backdating onto the PR timeline* below.
* Restarting the container is safe (data and license live in the H2 DB under
  `jira-lab/home/jira-lab`); the Jira Software app stays enabled, and a restart
  is also what re-arms the 3-hour app-license window.
* `/rest/api/2/serverInfo` reports `"deploymentType": "Server"` — Jira reports
  that for every self-managed instance. This *is* Data Center: DC license,
  `cluster.properties` (`jira.node.id=jira-lab-node-1`, shared home under
  `$JIRA_HOME/shared`) and `ClusteringLauncher` in the log.
* No `-jdk21` tag exists for 9.17 (JDK 21 support starts with Jira 10); the
  image runs JDK 17.
* Ports: 8090 was chosen because 7990/7991/7999/7998 belong to the Bitbucket
  labs, 8766 to another local service, and 8080 is taken on this host.

## Files

| File | Purpose |
|---|---|
| `config.env` | ports, image tag, heap, sysadmin creds, license paths |
| `license.txt` | Jira Software DC 10-user 3-hour timebomb license |
| `app-license.txt` | DC app 3-hour timebomb license (for a Tempo attempt) |
| `lab-up.sh` | pull image, create/start `jira-lab`, wait for `/status` |
| `lab-down.sh` | remove the container (`PURGE=1` deletes the home) |
| `setup.py` | unattended setup wizard + Jira Software app repair + 3-hourly app-license re-arm |
| `seed.py` | idempotent DEL corpus seed (users, project, epic, stories, sprints, worklogs) |
| `backdate.py` | rewrite story/sprint/worklog timestamps onto the PR timeline + set story points |
| `verify.py` | read-only REST/Agile verification (27 checks) |

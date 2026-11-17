#!/usr/bin/env python3
"""Backdate the jira-lab fixture onto the Bitbucket PR timeline.

The seed (:mod:`jira-lab.seed`) creates the whole DEL corpus at one instant, so
every story's ``created`` / status transition / sprint membership / worklog
timestamp is the *seed* time (2026-11-16), while the Bitbucket PRs the stories
are about were opened and merged between 2026-08-13 and 2026-11-12.  Read
together that makes the Jira-layer "merge -> done" segment a fake ~36-day
artifact.

Jira's REST API cannot set ``created``, changelog timestamps or worklog
``started``/``created`` (only ``started`` on a *new* worklog).  The lab instance
is ours, so this script edits the embedded H2 database directly, the same way
``corpus/backdate.py`` does for Bitbucket:

1. make sure the instance is up; put "Story Points" on the DEL screens and set
   the estimate of every story over REST (so Jira writes a real changelog row);
2. stop ``jira-lab``;
3. copy ``database/h2db.mv.db`` to ``jira-lab/build/backdate/`` (the original
   is kept as ``db.backup.mv.db`` next to it);
4. derive one timeline per story from the PRs in the megaspeed store
   (``data/del-combined.duckdb``, falling back to ``data/del3.duckdb``):
   a story is refined before it is pulled into its sprint, starts shortly
   before its first PR opens and is done shortly after its last PR merges;
5. rewrite the H2 rows that the Jira ingest actually reads:
   * ``JIRAISSUE.CREATED`` / ``UPDATED`` / ``RESOLUTIONDATE``,
   * ``CHANGEGROUP.CREATED`` for the status, sprint and story-point changes,
   * ``CHANGEITEM`` rows for ``To Do -> In Progress -> Done`` (the seed only
     wrote the direct ``To Do -> Done`` transition for closed sprints, which is
     why "sprint -> in progress" had no data),
   * ``WORKLOG.STARTDATE`` / ``CREATED`` / ``UPDATED``,
   * ``AO_60DB71_SPRINT`` start / end / complete / activated dates,
   * the workflow tables (``OS_CURRENTSTEP`` / ``OS_HISTORYSTEP``) so the UI
     and the REST API agree with the changelog;
6. copy the database back, restart the container and verify through REST.

The mapping is derived from the plan (the PR timeline + fixed RNG seed), not
from the current timestamps, so a rerun is idempotent: it produces the same
rows.  Story points are Fibonacci-like (1/2/3/5/8/13) from the story's changed
lines, so they track the size of the work the PRs actually carry.

    python3 jira-lab/backdate.py                 # apply + verify
    python3 jira-lab/backdate.py --plan-only     # print the plan, change nothing
    python3 jira-lab/backdate.py --verify        # only verify (container up)
    python3 jira-lab/backdate.py --skip-points   # keep whatever points exist

Scope: this script owns ``jira-lab/`` and the ``jira-lab`` container.  It never
touches ``data/del.duckdb`` and never imports or rewrites the ingest/analysis
code -- the store is read read-only, only to learn each PR's open/merge dates.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BUILD = HERE / "build" / "backdate"
CONFIG = HERE / "config.env"

CONTAINER = os.environ.get("JIRA_NAME", "jira-lab")
NERDCTL = os.environ.get("NERDCTL_CMD", "sudo nerdctl")
H2_JAR = Path(os.environ.get("H2_JAR", "/tmp/opencode/h2-shell.jar"))
DB_REL = Path("database/h2db.mv.db")
DB_OWNER = os.environ.get("JIRA_DB_OWNER", "2001:2001")
BASE = os.environ.get("JIRA_BASE", "http://127.0.0.1:8090").rstrip("/")
JIRA_HOME_ENV = os.environ.get("JIRA_LAB_HOME", "")

STORE_CANDIDATES = (
    REPO / "data" / "del-combined.duckdb",
    REPO / "data" / "del3.duckdb",
)

# The fixed RNG seed of the corpus/seed, reused so the plan is deterministic.
SEED = 20261115
WORK_START, WORK_END = 9, 17
UTC = timezone.utc

#: Sprint windows.  They tile the PR window without overlapping and every
#: story's first PR opens at/after its sprint's start (the seed's membership
#: groups 100..129 / 130..159 / 160..189 are kept, so no membership move is
#: needed).  S1/S2 are closed, S3 is the active sprint at the snapshot.
SPRINTS = {
    "DEL Sprint 1": {"id": 1, "start": date(2026, 8, 17), "end": date(2026, 9, 10),
                     "state": "closed"},
    "DEL Sprint 2": {"id": 2, "start": date(2026, 9, 11), "end": date(2026, 10, 7),
                     "state": "closed"},
    "DEL Sprint 3": {"id": 3, "start": date(2026, 10, 8), "end": date(2026, 11, 27),
                     "state": "active"},
}
SPRINT_BY_ID = {spec["id"]: name for name, spec in SPRINTS.items()}
#: The active sprint is "now" at the seed snapshot; worklogs stop before it.
WORKLOG_CUTOFF = datetime(2026, 11, 13, 17, 0)
EPIC_CREATED = datetime(2026, 7, 27, 9, 0)
EARLIEST_CREATED = datetime(2026, 8, 3, 9, 0)

#: Story points from changed lines (added + removed across the story's PRs).
POINT_CAPS = ((40, 1), (80, 2), (150, 3), (250, 5), (400, 8))

# H2 status / resolution ids on the DEL scrum workflow (stable for this lab).
STATUS_TODO, STATUS_IN_PROGRESS, STATUS_DONE = "10000", "3", "10001"
RESOLUTION_DONE = "10000"
STEP_TODO, STEP_IN_PROGRESS, STEP_DONE = 1, 6, 11
ACTION_START, ACTION_DONE = 21, 31

#: ID ranges reserved for rows this script writes.  Deleting the range first
#: makes a rerun byte-identical instead of accumulating rows.
STATUS_GROUP_BASE = 1_000_000
STATUS_ITEM_BASE = 2_000_000
HISTORY_BASE = 3_000_000
POINTS_GROUP_BASE = 4_000_000
POINTS_ITEM_BASE = 5_000_000
POINTS_VALUE_BASE = 6_000_000


def log(msg: str) -> None:
    print(f"[backdate] {msg}", flush=True)


def sh(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({p.returncode}):\n{p.stderr or p.stdout}")
    return p


def nerdctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return sh(NERDCTL.split() + list(args), check=check)


# ---------------------------------------------------------------------------
# config / container / H2 plumbing
# ---------------------------------------------------------------------------
def config() -> dict:
    keys = ("SYSADMIN_USER", "SYSADMIN_PASSWORD", "JIRA_HTTP_PORT", "BASE_URL")
    if not CONFIG.exists():
        return {}
    code = ('. "$1" >/dev/null 2>&1; for k in %s; do '
            'printf "%%s=%%s\\n" "$k" "${!k}"; done' % " ".join(keys))
    p = subprocess.run(["bash", "-c", code, "_", str(CONFIG)],
                       capture_output=True, text=True)
    out = {}
    for line in p.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


CFG = config()
SYSADMIN = (CFG.get("SYSADMIN_USER", "admin"), CFG.get("SYSADMIN_PASSWORD", "admin-lab-pw"))


class Api:
    """The three REST calls this script needs (no Agile dependency)."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, data=None, ok=(200, 201, 204),
             retries: int = 3):
        url = self.base + path
        body = None
        headers = {"Accept": "application/json"}
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        tok = base64.b64encode(f"{SYSADMIN[0]}:{SYSADMIN[1]}".encode()).decode()
        headers["Authorization"] = "Basic " + tok
        last = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=body, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=120) as r:
                    txt = r.read().decode()
                    if r.status not in ok:
                        raise RuntimeError(f"{method} {path} -> {r.status}: {txt[:300]}")
                    return json.loads(txt) if txt.strip() else {}
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors="replace")
                if e.code in ok:
                    return json.loads(txt) if txt.strip() else {}
                if e.code >= 500 and attempt < retries - 1:
                    last = f"{e.code}: {txt[:200]}"
                    time.sleep(2)
                    continue
                raise RuntimeError(f"{method} {path} -> {e.code}: {txt[:400]}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = repr(e)
                if attempt < retries - 1:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"{method} {path} failed: {last}")
        raise RuntimeError(f"{method} {path} failed: {last}")

    def get(self, path, params=None, **kw):
        if params:
            path += "?" + urllib.parse.urlencode(params)
        return self.call("GET", path, **kw)

    def post(self, path, data=None, **kw):
        return self.call("POST", path, data=data, **kw)

    def put(self, path, data=None, **kw):
        return self.call("PUT", path, data=data, **kw)


def resolve_home() -> Path:
    """The host path of ``$JIRA_HOME`` (bind mount of the running container)."""
    if JIRA_HOME_ENV:
        return Path(JIRA_HOME_ENV).expanduser()
    try:
        out = nerdctl("inspect", CONTAINER, "--format", "{{json .Mounts}}").stdout
        mounts = json.loads(out)
        for mount in mounts:
            if mount.get("Destination") == "/var/atlassian/application-data/jira":
                return Path(mount["Source"])
    except Exception as exc:  # container may not exist yet
        log(f"could not resolve the container mount ({exc})")
    return HERE / "home" / CONTAINER


def container_running() -> bool:
    p = nerdctl("inspect", "-f", "{{.State.Running}}", CONTAINER, check=False)
    return p.returncode == 0 and p.stdout.strip() == "true"


def wait_ready(timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/status", timeout=5) as r:
                if "RUNNING" in r.read().decode():
                    break
        except Exception:
            pass
        time.sleep(5)
    else:
        raise RuntimeError("container did not report RUNNING")
    api = Api(BASE)
    while time.time() < deadline:
        try:
            if api.get("/rest/api/2/serverInfo").get("version"):
                return
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError("Jira REST did not answer after the restart")


def ensure_running() -> None:
    if container_running():
        return
    log(f"starting {CONTAINER}")
    nerdctl("start", CONTAINER)
    wait_ready()


def reindex(api: Api, timeout: int = 600) -> None:
    """Rebuild the Lucene index so JQL/search agrees with the edited rows.

    The ingest fetches each issue directly (the changelog is read from the DB),
    but the issue list is a search query; a stale index would report the old
    status/updated values to every other client (and to ``verify.py``).
    """
    try:
        api.call("POST", "/rest/api/2/reindex?type=BACKGROUND", ok=(200, 202))
    except RuntimeError as exc:
        log(f"reindex not started ({exc}); the direct issue reads are still correct")
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            progress = api.get("/rest/api/2/reindex/progress")
        except RuntimeError:
            progress = {}
        if progress.get("success") and int(progress.get("currentProgress") or 0) >= 100:
            log("search index rebuilt")
            return
        if progress.get("error"):
            log(f"reindex error: {progress['error']}")
            return
        time.sleep(2)
    log("reindex still running; direct issue reads are unaffected")


def run_sql(db: Path, script: str) -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    path = BUILD / "_script.sql"
    path.write_text(script)
    db = Path(db).resolve()
    base = str(db)[:-6] if str(db).endswith(".mv.db") else str(db)
    sh(["java", "-cp", str(H2_JAR), "org.h2.tools.RunScript",
        "-url", f"jdbc:h2:file:{base};MODE=LEGACY", "-user", "sa", "-password", "",
        "-script", str(path)])


def dump(db: Path, name: str, select: str) -> list[dict]:
    BUILD.mkdir(parents=True, exist_ok=True)
    out = BUILD / f"{name}.csv"
    out.unlink(missing_ok=True)
    escaped = select.replace("'", "''")
    run_sql(db, f"CALL CSVWRITE('{out}', '{escaped}');")
    with out.open(newline="") as f:
        return list(csv.DictReader(f))


def sql_ts(dt: datetime | None) -> str:
    if dt is None:
        return "NULL"
    return "TIMESTAMP '{}'".format(dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])


def epoch_ms(dt: datetime | None) -> str:
    if dt is None:
        return "NULL"
    return str(int(dt.replace(tzinfo=UTC).timestamp() * 1000))


# ---------------------------------------------------------------------------
# business-time helpers
# ---------------------------------------------------------------------------
def is_workday(day: date) -> bool:
    return day.weekday() < 5


def next_workday(day: date) -> date:
    day += timedelta(days=1)
    while not is_workday(day):
        day += timedelta(days=1)
    return day


def prev_workday(day: date) -> date:
    day -= timedelta(days=1)
    while not is_workday(day):
        day -= timedelta(days=1)
    return day


def business_days(start: date, end: date) -> list[date]:
    out, day = [], start
    while day <= end:
        if is_workday(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def shift_business_days(dt: datetime, days: int) -> datetime:
    """Move ``days`` business days, keeping the time of day (clamped 9-17)."""
    day = dt.date()
    step = 1 if days >= 0 else -1
    for _ in range(abs(days)):
        day = next_workday(day) if step > 0 else prev_workday(day)
    hour = min(WORK_END - 1, max(WORK_START, dt.hour))
    return datetime.combine(day, dtime(hour, dt.minute))


def add_business_hours(dt: datetime, hours: float) -> datetime:
    cur, remaining = dt, float(hours)
    while remaining > 0:
        if cur.hour < WORK_START:
            cur = cur.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
        end = cur.replace(hour=WORK_END, minute=0, second=0, microsecond=0)
        avail = (end - cur).total_seconds() / 3600.0
        if avail >= remaining:
            return cur + timedelta(hours=remaining)
        remaining -= max(0.0, avail)
        cur = datetime.combine(next_workday(cur.date()), dtime(WORK_START))
    return cur


def sub_business_hours(dt: datetime, hours: float) -> datetime:
    cur, remaining = dt, float(hours)
    while remaining > 0:
        if cur.hour > WORK_END:
            cur = cur.replace(hour=WORK_END, minute=0, second=0, microsecond=0)
        start = cur.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
        avail = (cur - start).total_seconds() / 3600.0
        if avail >= remaining:
            return cur - timedelta(hours=remaining)
        remaining -= max(0.0, avail)
        cur = datetime.combine(prev_workday(cur.date()), dtime(WORK_END))
    return cur


def at(day: date, hour: float) -> datetime:
    h = int(hour)
    m = int(round((hour - h) * 60))
    if m >= 60:
        h, m = h + 1, m - 60
    return datetime.combine(day, dtime(min(h, WORK_END - 1), m))


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------
@dataclass
class Worklog:
    id: int
    started: datetime
    created: datetime


@dataclass
class StoryPlan:
    key: str
    index: int
    issue_id: int
    workflow_id: int
    sprint: str | None
    points: int
    created: datetime
    refined: datetime
    added: datetime | None
    started: datetime
    done: datetime | None
    merged: datetime | None
    first_open: datetime
    lines: int
    worklogs: list[Worklog] = field(default_factory=list)

    @property
    def updated(self) -> datetime:
        stamps = [self.created, self.refined, self.started]
        if self.added:
            stamps.append(self.added)
        if self.done:
            stamps.append(self.done)
        stamps.extend(w.created for w in self.worklogs)
        return max(stamps)


def points_for(lines: int) -> int:
    for cap, points in POINT_CAPS:
        if lines <= cap:
            return points
    return 13


def read_pr_timeline(store: Path) -> dict[str, dict]:
    """Per story key: first PR open, last merged close, PR count, changed lines."""
    import duckdb  # only the main side has DuckDB; the scrape side never does

    con = duckdb.connect(str(store), read_only=True)
    try:
        rows = con.execute(
            "SELECT ticket_keys, state, created_at, closed_at,"
            " CAST(attributes AS VARCHAR) FROM objects WHERE object_type = 'PR'"
        ).fetchall()
    finally:
        con.close()
    out: dict[str, dict] = {}
    for keys, state, created, closed, attrs in rows:
        try:
            attributes = json.loads(attrs) if attrs else {}
        except (TypeError, json.JSONDecodeError):
            attributes = {}
        lines = int(attributes.get("lines_added") or 0) + int(attributes.get("lines_removed") or 0)
        for key in keys or ():
            key = str(key)
            if not key or key == "DEL-1":
                continue
            entry = out.setdefault(key, {"open": None, "merged": None, "prs": 0, "lines": 0})
            entry["prs"] += 1
            entry["lines"] += lines
            if created and (entry["open"] is None or created < entry["open"]):
                entry["open"] = created
            if state == "merged" and closed and (
                    entry["merged"] is None or closed > entry["merged"]):
                entry["merged"] = closed
    return out


def load_db_facts(db: Path) -> dict:
    """The rows the plan needs from the H2 database (read via CSV dumps)."""
    issues = dump(db, "issues",
                  "SELECT ID, ISSUENUM, WORKFLOW_ID FROM JIRAISSUE "
                  "WHERE ISSUENUM BETWEEN 100 AND 199 ORDER BY ISSUENUM")
    sprints = dump(db, "sprints",
                   "SELECT ID, NAME, CLOSED, STARTED FROM AO_60DB71_SPRINT ORDER BY ID")
    membership = dump(db, "membership",
                      "SELECT ISSUE, STRINGVALUE FROM CUSTOMFIELDVALUE "
                      "WHERE CUSTOMFIELD = 10104")
    points = dump(db, "points",
                  "SELECT ISSUE, NUMBERVALUE FROM CUSTOMFIELDVALUE "
                  "WHERE CUSTOMFIELD = 10106")
    worklogs = dump(db, "worklogs",
                    "SELECT ID, ISSUEID FROM WORKLOG ORDER BY ISSUEID, ID")
    return {
        "issues": {int(r["ISSUENUM"]): r for r in issues},
        "sprints": {int(r["ID"]): r for r in sprints},
        "membership": {int(r["ISSUE"]): int(r["STRINGVALUE"]) for r in membership
                       if (r.get("STRINGVALUE") or "").isdigit()},
        "points": {int(r["ISSUE"]): r.get("NUMBERVALUE") for r in points},
        "worklogs": [(int(r["ID"]), int(r["ISSUEID"])) for r in worklogs],
    }


def build_plan(timeline: dict[str, dict], facts: dict) -> list[StoryPlan]:
    plans: list[StoryPlan] = []
    for index in range(100):
        key = f"DEL-{100 + index}"
        row = facts["issues"].get(100 + index)
        if row is None:
            raise RuntimeError(f"{key} is missing from the Jira database")
        pr = timeline.get(key)
        if not pr or pr["open"] is None:
            raise RuntimeError(f"{key} has no PR in the store: cannot place it on the timeline")
        issue_id = int(row["ID"])
        workflow_id = int(row["WORKFLOW_ID"])
        sprint_id = facts["membership"].get(issue_id)
        sprint = SPRINT_BY_ID.get(sprint_id)
        spec = SPRINTS.get(sprint) if sprint else None
        rng = random.Random(SEED + index)

        first_open = pr["open"]
        merged = pr["merged"]
        points = points_for(pr["lines"])

        # in progress: a few business hours before the first PR opens, never
        # before the sprint that carries the story.
        started = sub_business_hours(first_open, rng.choice((1, 2, 3, 4, 6)))
        if spec:
            floor = datetime.combine(spec["start"], dtime(9, 30))
            if started < floor:
                started = floor
        if started >= first_open:
            started = sub_business_hours(first_open, 1)

        # sprint membership: pulled in 1-2 business days before work starts,
        # never before the sprint's own start.
        added = None
        if spec:
            added = shift_business_days(started, -rng.choice((1, 2)))
            floor = datetime.combine(spec["start"], dtime(9, 30))
            if added < floor:
                added = floor
            if added > started:
                added = started
        # refinement: 1-2 business days before the sprint pull (or the start
        # for a backlog story that was never sprinted).
        refined = shift_business_days(added or started, -rng.choice((1, 2)))
        refined = refined.replace(hour=rng.choice((10, 11, 13, 14, 15)),
                                  minute=rng.choice((0, 15, 30, 45)))
        # ready: the story sat in the backlog for 2-5 business days.
        created = shift_business_days(refined, -rng.choice((2, 3, 4, 5)))
        created = created.replace(hour=rng.choice((9, 10, 11, 14, 15)),
                                  minute=rng.choice((0, 20, 40)))
        if created < EARLIEST_CREATED:
            created = EARLIEST_CREATED
        if created >= refined:
            created = shift_business_days(refined, -1)

        # done: 1-12 business hours after the last merge; a story whose PRs
        # never merged stays in progress (the honest state).
        done = add_business_hours(merged, rng.choice((1, 2, 3, 4, 6, 8, 12))) if merged else None

        plan = StoryPlan(
            key=key, index=index, issue_id=issue_id, workflow_id=workflow_id,
            sprint=sprint, points=points, created=created, refined=refined,
            added=added, started=started, done=done, merged=merged,
            first_open=first_open, lines=pr["lines"],
        )

        # worklogs: 1-3 per story (the seed's own count), inside the story's
        # active window, logged same/next day (occasionally within the week).
        rows = [wid for wid, iid in facts["worklogs"] if iid == issue_id]
        n = 1 + index % 3
        if len(rows) != n:
            n = len(rows)
        window_end = done or WORKLOG_CUTOFF
        days = business_days(started.date(), min(window_end.date(), WORKLOG_CUTOFF.date()))
        if not days:
            days = [started.date()]
        for j, wid in enumerate(rows[:n]):
            day = days[min(len(days) - 1, (index * 3 + j * 7) % len(days))]
            when = at(day, 9.5 + rng.uniform(0, 6.5))
            if when < started:
                when = started + timedelta(minutes=30)
            if when >= window_end:
                when = max(started, window_end - timedelta(minutes=30))
            # Logged same day or up to three business days later, always at a
            # work-hour timestamp (the log-delay bucket is about the gap, but a
            # 02:34 "logged at" would look synthetic).  A deterministic handful
            # is logged a week+ late: that is the worklog-noise signal the
            # ingest measures, and a real team always has a few.
            delay = 8 if (index % 20 == 7 and j == 0) else rng.choice((0, 0, 1, 1, 1, 2))
            if delay == 0:
                logged = when
            else:
                logged = at(shift_business_days(when, delay).date(),
                            9.5 + rng.uniform(0, 6.5))
            if logged < when:
                logged = when
            if logged > datetime(2026, 11, 15, 17, 0):
                logged = datetime(2026, 11, 15, 17, 0)
            plan.worklogs.append(Worklog(id=wid, started=when, created=logged))
        plans.append(plan)
    return plans


# ---------------------------------------------------------------------------
# REST: story points (needs a real changelog row, so it must go through Jira)
# ---------------------------------------------------------------------------
def field_ids(api: Api) -> dict[str, str]:
    return {str(f.get("name")): str(f.get("id")) for f in api.get("/rest/api/2/field")}


def ensure_points_screen(api: Api, field_id: str) -> None:
    """Put Story Points on the DEL screens (the scrum template left it off)."""
    for screen in api.get("/rest/api/2/screens"):
        if not str(screen.get("name", "")).startswith("DEL"):
            continue
        for tab in api.get(f"/rest/api/2/screens/{screen['id']}/tabs"):
            path = f"/rest/api/2/screens/{screen['id']}/tabs/{tab['id']}/fields"
            if any(str(f.get("id")) == field_id for f in api.get(path)):
                continue
            api.post(path, data={"fieldId": field_id})
            log(f"  screen {screen['name']!r}: += Story Points")


def apply_points(api: Api, estimates: list[tuple[str, int]], field_id: str) -> None:
    for key, points in estimates:
        api.put(f"/rest/api/2/issue/{key}", data={"fields": {field_id: points}})
    log(f"story points set over REST for {len(estimates)} stories")


# ---------------------------------------------------------------------------
# SQL: rewrite the rows the ingest reads
# ---------------------------------------------------------------------------
def status_groups_sql(plan: StoryPlan) -> list[str]:
    gid_start = STATUS_GROUP_BASE + plan.index * 10
    cid_start = STATUS_ITEM_BASE + plan.index * 20
    gids = [gid_start + 1, gid_start + 2]
    out = [
        "DELETE FROM CHANGEITEM WHERE GROUPID IN (%d, %d);" % tuple(gids),
        "DELETE FROM CHANGEGROUP WHERE ID IN (%d, %d);" % tuple(gids),
        "INSERT INTO CHANGEGROUP (ID, ISSUEID, AUTHOR, CREATED) VALUES "
        f"({gids[0]}, {plan.issue_id}, 'JIRAUSER10000', {sql_ts(plan.started)});",
        "INSERT INTO CHANGEITEM (ID, GROUPID, FIELDTYPE, FIELD, OLDVALUE, OLDSTRING, "
        "NEWVALUE, NEWSTRING) VALUES "
        f"({cid_start + 1}, {gids[0]}, 'jira', 'status', '{STATUS_TODO}', 'To Do', "
        f"'{STATUS_IN_PROGRESS}', 'In Progress');",
    ]
    if plan.done:
        out += [
            "INSERT INTO CHANGEGROUP (ID, ISSUEID, AUTHOR, CREATED) VALUES "
            f"({gids[1]}, {plan.issue_id}, 'JIRAUSER10000', {sql_ts(plan.done)});",
            "INSERT INTO CHANGEITEM (ID, GROUPID, FIELDTYPE, FIELD, OLDVALUE, OLDSTRING, "
            "NEWVALUE, NEWSTRING) VALUES "
            f"({cid_start + 2}, {gids[1]}, 'jira', 'status', '{STATUS_IN_PROGRESS}', "
            f"'In Progress', '{STATUS_DONE}', 'Done');",
            "INSERT INTO CHANGEITEM (ID, GROUPID, FIELDTYPE, FIELD, OLDVALUE, OLDSTRING, "
            "NEWVALUE, NEWSTRING) VALUES "
            f"({cid_start + 3}, {gids[1]}, 'jira', 'resolution', NULL, NULL, "
            f"'{RESOLUTION_DONE}', 'Done');",
        ]
    return out


def workflow_sql(plan: StoryPlan) -> list[str]:
    hid_start = HISTORY_BASE + plan.index * 10
    out = [
        f"DELETE FROM OS_HISTORYSTEP WHERE ENTRY_ID = {plan.workflow_id};",
        "UPDATE OS_CURRENTSTEP SET "
        f"STEP_ID = {STEP_DONE if plan.done else STEP_IN_PROGRESS}, ACTION_ID = 0, "
        f"STATUS = 'Done', START_DATE = {sql_ts(plan.done or plan.started)}, "
        f"FINISH_DATE = NULL WHERE ENTRY_ID = {plan.workflow_id};",
        "INSERT INTO OS_HISTORYSTEP (ID, ENTRY_ID, STEP_ID, ACTION_ID, OWNER, "
        "START_DATE, DUE_DATE, FINISH_DATE, STATUS, CALLER) VALUES "
        f"({hid_start + 1}, {plan.workflow_id}, {STEP_TODO}, {ACTION_START}, NULL, "
        f"{sql_ts(plan.created)}, NULL, {sql_ts(plan.started)}, 'Not Done', NULL);",
    ]
    if plan.done:
        out += [
            "INSERT INTO OS_HISTORYSTEP (ID, ENTRY_ID, STEP_ID, ACTION_ID, OWNER, "
            "START_DATE, DUE_DATE, FINISH_DATE, STATUS, CALLER) VALUES "
            f"({hid_start + 2}, {plan.workflow_id}, {STEP_IN_PROGRESS}, {ACTION_DONE}, "
            f"NULL, {sql_ts(plan.started)}, NULL, {sql_ts(plan.done)}, 'Not Done', NULL);",
            "INSERT INTO OS_HISTORYSTEP (ID, ENTRY_ID, STEP_ID, ACTION_ID, OWNER, "
            "START_DATE, DUE_DATE, FINISH_DATE, STATUS, CALLER) VALUES "
            f"({hid_start + 3}, {plan.workflow_id}, {STEP_DONE}, {ACTION_DONE}, NULL, "
            f"{sql_ts(plan.done)}, NULL, {sql_ts(plan.done + timedelta(seconds=1))}, "
            "'Not Done', NULL);",
        ]
    return out


def points_sql(plan: StoryPlan, has_points_row: bool) -> list[str]:
    gid = POINTS_GROUP_BASE + plan.index
    cid = POINTS_ITEM_BASE + plan.index
    cvid = POINTS_VALUE_BASE + plan.index
    out = [
        # The REST PUT wrote a Story Points changelog row at "now"; drop every
        # points row for this issue and write exactly one at the refinement
        # time, so the ingest sees one `pointed` event (not a churn artefact).
        "DELETE FROM CHANGEITEM WHERE FIELD = 'Story Points' AND GROUPID IN "
        f"(SELECT ID FROM CHANGEGROUP WHERE ISSUEID = {plan.issue_id});",
        f"DELETE FROM CHANGEGROUP WHERE ISSUEID = {plan.issue_id} AND ID NOT IN "
        "(SELECT GROUPID FROM CHANGEITEM);",
        f"DELETE FROM CHANGEITEM WHERE GROUPID = {gid};",
        f"DELETE FROM CHANGEGROUP WHERE ID = {gid};",
        "INSERT INTO CHANGEGROUP (ID, ISSUEID, AUTHOR, CREATED) VALUES "
        f"({gid}, {plan.issue_id}, 'JIRAUSER10000', {sql_ts(plan.refined)});",
        "INSERT INTO CHANGEITEM (ID, GROUPID, FIELDTYPE, FIELD, OLDVALUE, OLDSTRING, "
        "NEWVALUE, NEWSTRING) VALUES "
        f"({cid}, {gid}, 'custom', 'Story Points', NULL, NULL, NULL, '{plan.points}');",
    ]
    if has_points_row:
        out.append(
            "UPDATE CUSTOMFIELDVALUE SET "
            f"NUMBERVALUE = {plan.points}, UPDATED = {epoch_ms(plan.refined)} "
            f"WHERE ISSUE = {plan.issue_id} AND CUSTOMFIELD = 10106;")
    else:
        out.append(
            "INSERT INTO CUSTOMFIELDVALUE (ID, ISSUE, CUSTOMFIELD, UPDATED, STRINGVALUE, "
            "NUMBERVALUE, TEXTVALUE, DATEVALUE, VALUETYPE) VALUES "
            f"({cvid}, {plan.issue_id}, 10106, {epoch_ms(plan.refined)}, NULL, "
            f"{plan.points}, NULL, NULL, NULL);")
    return out


def build_sql(plans: list[StoryPlan], facts: dict, points_rows: set[int]) -> str:
    stmts: list[str] = []
    for plan in plans:
        # the seed's direct To Do -> Done rows (and the no-op Done -> Done) go:
        # we write the two-step transition the workflow actually has.
        stmts.append(
            "DELETE FROM CHANGEITEM WHERE FIELD IN ('status', 'resolution') AND GROUPID IN "
            f"(SELECT ID FROM CHANGEGROUP WHERE ISSUEID = {plan.issue_id});")
        stmts.append(
            f"DELETE FROM CHANGEGROUP WHERE ISSUEID = {plan.issue_id} AND ID NOT IN "
            "(SELECT GROUPID FROM CHANGEITEM);")
        stmts += status_groups_sql(plan)
        stmts += workflow_sql(plan)
        stmts += points_sql(plan, plan.issue_id in points_rows)

        stmts.append(
            "UPDATE JIRAISSUE SET "
            f"CREATED = {sql_ts(plan.created)}, "
            f"UPDATED = {sql_ts(plan.updated)}, "
            f"ISSUESTATUS = '{STATUS_DONE if plan.done else STATUS_IN_PROGRESS}', "
            f"RESOLUTION = {repr(RESOLUTION_DONE) if plan.done else 'NULL'}, "
            f"RESOLUTIONDATE = {sql_ts(plan.done)} "
            f"WHERE ID = {plan.issue_id};")

        # the Sprint changelog row: the story was pulled into its sprint then.
        if plan.added:
            stmts.append(
                "UPDATE CHANGEGROUP SET CREATED = " + sql_ts(plan.added) + " WHERE ID IN "
                "(SELECT g.ID FROM CHANGEGROUP g JOIN CHANGEITEM i ON i.GROUPID = g.ID "
                f"WHERE g.ISSUEID = {plan.issue_id} AND i.FIELD = 'Sprint');")

        for worklog in plan.worklogs:
            stmts.append(
                "UPDATE WORKLOG SET "
                f"STARTDATE = {sql_ts(worklog.started)}, "
                f"CREATED = {sql_ts(worklog.created)}, "
                f"UPDATED = {sql_ts(worklog.created)} "
                f"WHERE ID = {worklog.id};")
            stmts.append(
                "UPDATE CHANGEGROUP SET CREATED = " + sql_ts(worklog.created) + " WHERE ID IN "
                "(SELECT GROUPID FROM CHANGEITEM WHERE FIELD = 'WorklogId' "
                f"AND OLDVALUE = '{worklog.id}');")

    # sprints: tiling windows, the closed ones completed on their planned end.
    for name, spec in SPRINTS.items():
        row = next((r for r in facts["sprints"].values()
                    if r["NAME"] == name), None)
        if row is None:
            raise RuntimeError(f"sprint {name!r} is missing from the Jira database")
        start = datetime.combine(spec["start"], dtime(9, 0))
        end = datetime.combine(spec["end"], dtime(17, 0))
        complete = None if spec["state"] == "active" else datetime.combine(
            spec["end"], dtime(16, 0))
        stmts.append(
            "UPDATE AO_60DB71_SPRINT SET "
            f"START_DATE = {epoch_ms(start)}, END_DATE = {epoch_ms(end)}, "
            f"COMPLETE_DATE = {epoch_ms(complete)}, ACTIVATED_DATE = {epoch_ms(start)}, "
            f"CLOSED = {'FALSE' if spec['state'] == 'active' else 'TRUE'}, "
            f"STARTED = TRUE WHERE ID = {int(row['ID'])};")

    # the epic predates its stories and is touched by the last one.
    last_updated = max(plan.updated for plan in plans)
    stmts.append(
        "UPDATE JIRAISSUE SET CREATED = " + sql_ts(EPIC_CREATED) + ", UPDATED = "
        + sql_ts(last_updated) + " WHERE ISSUENUM = 1;")

    # the custom field's bookkeeping (the UI shows "100 issues").
    stmts.append("UPDATE CUSTOMFIELD SET ISSUESWITHVALUE = 100, LASTVALUEUPDATE = "
                 + sql_ts(max(plan.refined for plan in plans))
                 + " WHERE ID = 10106;")
    return "\n".join(stmts) + "\n"


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
def _rest_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)


def verify_rest(api: Api, plans: list[StoryPlan], points_field: str) -> None:
    sample = [plans[0], plans[37], plans[62], plans[-1]]
    sample += [p for p in plans if p.sprint == "DEL Sprint 3" and p.done][:1]
    ok = True
    for plan in sample:
        issue = api.get(f"/rest/api/2/issue/{plan.key}",
                        {"expand": "changelog", "fields": f"created,updated,{points_field}"})
        fields = issue["fields"]
        created = _rest_ts(fields.get("created"))
        points = fields.get(points_field)
        histories = issue.get("changelog", {}).get("histories", [])
        pointed = [h for h in histories
                   if any(i.get("field") == "Story Points" for i in h.get("items", []))]
        statuses = [h for h in histories
                    if any(i.get("field") == "status" for i in h.get("items", []))]
        sprint = [h for h in histories
                  if any(i.get("field") == "Sprint" for i in h.get("items", []))]
        in_progress = [h for h in statuses
                       if any(i.get("toString") == "In Progress" for i in h.get("items", []))]
        done = [h for h in statuses
                if any(i.get("toString") == "Done" for i in h.get("items", []))]
        checks = [
            (created is not None and abs((created - plan.created).total_seconds()) < 1,
             f"created {created} vs {plan.created}"),
            (int(points or 0) == plan.points, f"points {points} vs {plan.points}"),
            (bool(pointed) and abs((_rest_ts(pointed[0]["created"]) - plan.refined).total_seconds()) < 1,
             f"pointed {_rest_ts(pointed[0]['created']) if pointed else None} vs {plan.refined}"),
            (bool(in_progress) and abs((_rest_ts(in_progress[0]["created"]) - plan.started).total_seconds()) < 1,
             f"in progress {_rest_ts(in_progress[0]['created']) if in_progress else None} vs {plan.started}"),
            ((bool(done) == bool(plan.done))
             and (not plan.done or abs((_rest_ts(done[0]["created"]) - plan.done).total_seconds()) < 1),
             f"done {_rest_ts(done[0]['created']) if done else None} vs {plan.done}"),
            ((bool(sprint) == bool(plan.added))
             and (not plan.added or abs((_rest_ts(sprint[0]["created"]) - plan.added).total_seconds()) < 1),
             f"sprint {_rest_ts(sprint[0]['created']) if sprint else None} vs {plan.added}"),
        ]
        good = all(passed for passed, _ in checks)
        ok &= good
        log(f"  {plan.key} {'OK' if good else 'MISMATCH'}")
        if not good:
            for passed, message in checks:
                if not passed:
                    log(f"    {message}")
    if not ok:
        raise RuntimeError("REST verification failed")
    log(f"REST verification OK on {len(sample)} sampled stories")


# ---------------------------------------------------------------------------
# plan persistence (the verify pass runs with the container up, when the H2
# file is locked, so it reads the plan the apply pass wrote)
# ---------------------------------------------------------------------------
def plan_to_json(plans: list[StoryPlan]) -> dict:
    def stamp(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "stories": [
            {
                "key": p.key, "index": p.index, "issue_id": p.issue_id,
                "workflow_id": p.workflow_id, "sprint": p.sprint,
                "points": p.points, "lines": p.lines,
                "created": stamp(p.created), "refined": stamp(p.refined),
                "added": stamp(p.added), "started": stamp(p.started),
                "done": stamp(p.done), "merged": stamp(p.merged),
                "first_open": stamp(p.first_open),
                "worklogs": [
                    {"id": w.id, "started": stamp(w.started), "created": stamp(w.created)}
                    for w in p.worklogs
                ],
            }
            for p in plans
        ]
    }


def plan_from_json(payload: dict) -> list[StoryPlan]:
    def stamp(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    return [
        StoryPlan(
            key=row["key"], index=int(row["index"]), issue_id=int(row["issue_id"]),
            workflow_id=int(row["workflow_id"]), sprint=row.get("sprint"),
            points=int(row["points"]), lines=int(row["lines"]),
            created=stamp(row["created"]), refined=stamp(row["refined"]),
            added=stamp(row.get("added")), started=stamp(row["started"]),
            done=stamp(row.get("done")), merged=stamp(row.get("merged")),
            first_open=stamp(row["first_open"]),
            worklogs=[Worklog(id=int(w["id"]), started=stamp(w["started"]),
                              created=stamp(w["created"]))
                      for w in row.get("worklogs") or []],
        )
        for row in payload["stories"]
    ]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--db", default=None, help="override the H2 file (default: container home)")
    ap.add_argument("--store", default=None, help="megaspeed DuckDB with the PR timelines")
    ap.add_argument("--plan-only", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--verify", action="store_true", help="only verify through REST")
    ap.add_argument("--skip-points", action="store_true", help="do not write story points")
    args = ap.parse_args()

    if not H2_JAR.exists():
        log(f"ERROR: H2 jar not found at {H2_JAR}")
        return 2

    store = Path(args.store) if args.store else next(
        (p for p in STORE_CANDIDATES if p.exists()), None)
    if store is None:
        log("ERROR: no megaspeed store found (data/del-combined.duckdb or data/del3.duckdb)")
        return 2
    log(f"PR timeline from {store}")

    home = resolve_home()
    db_path = Path(args.db) if args.db else home / DB_REL
    log(f"Jira home {home}")

    timeline = read_pr_timeline(store)
    log(f"{len(timeline)} stories with PRs")

    if args.verify:
        ensure_running()
        api = Api(args.base)
        plan_file = BUILD / "plan.json"
        if not plan_file.exists():
            log(f"ERROR: no plan at {plan_file}; run the backdate first")
            return 2
        plans = plan_from_json(json.loads(plan_file.read_text()))
        fields = field_ids(api)
        verify_rest(api, plans, fields.get("Story Points", "customfield_10106"))
        return 0

    if args.plan_only:
        # reads the H2 file directly: pass --db with a copy (or stop the
        # container first), because a running Jira holds the file lock.
        facts = load_db_facts(db_path)
        plans = build_plan(timeline, facts)
        print("key       sprint        points lines  created          refined          "
              "sprint-added     started          done             merged")
        for p in plans:
            print(f"{p.key:9s} {str(p.sprint):13s} {p.points:>6d} {p.lines:>5d}  "
                  f"{p.created:%Y-%m-%d %H:%M}  {p.refined:%Y-%m-%d %H:%M}  "
                  f"{(p.added.strftime('%Y-%m-%d %H:%M') if p.added else '—'):16s}  "
                  f"{p.started:%Y-%m-%d %H:%M}  "
                  f"{(p.done.strftime('%Y-%m-%d %H:%M') if p.done else '—'):16s}  "
                  f"{(p.merged.strftime('%Y-%m-%d %H:%M') if p.merged else '—'):16s}")
        return 0

    # --- 1. story points through REST (a real changelog row) ----------------
    ensure_running()
    api = Api(args.base)
    fields = field_ids(api)
    points_field = fields.get("Story Points")
    if points_field and not args.skip_points:
        estimates = [(f"DEL-{100 + index}", points_for(entry["lines"]))
                     for index, entry in enumerate(
                         timeline[f"DEL-{100 + index}"] for index in range(100))]
        ensure_points_screen(api, points_field)
        apply_points(api, estimates, points_field)
    elif not points_field:
        log("WARNING: no 'Story Points' field; skipping estimates")

    # --- 2. stop, copy, rewrite, copy back, start ---------------------------
    if sh(["sudo", "test", "-f", str(db_path)], check=False).returncode != 0:
        log(f"ERROR: database not found at {db_path}")
        return 2
    log(f"stopping {CONTAINER}")
    nerdctl("stop", CONTAINER)
    BUILD.mkdir(parents=True, exist_ok=True)
    backup = BUILD / "db.backup.mv.db"
    work_db = BUILD / "db.mv.db"
    for f in (backup, work_db):
        f.unlink(missing_ok=True)
    sh(["sudo", "cp", str(db_path), str(backup)])
    sh(["sudo", "cp", str(db_path), str(work_db)])
    sh(["sudo", "chown", os.environ.get("USER", "lauri"), str(work_db), str(backup)])
    log(f"database copied to {work_db} (backup: {backup})")

    try:
        # ids / membership / worklog ids come from the copy, not the live file.
        facts = load_db_facts(work_db)
        plans = build_plan(timeline, facts)
        log(f"plan: {len(plans)} stories, {sum(1 for p in plans if p.done)} done, "
            f"{sum(len(p.worklogs) for p in plans)} worklogs")
        points_rows = {int(r["ISSUE"]) for r in dump(
            work_db, "points_rows",
            "SELECT ISSUE FROM CUSTOMFIELDVALUE WHERE CUSTOMFIELD = 10106")}
        sql = build_sql(plans, facts, points_rows)
        (BUILD / "plan.json").write_text(json.dumps(plan_to_json(plans), indent=1) + "\n")
        (BUILD / "plan.tsv").write_text(
            "key\tsprint\tpoints\tlines\tcreated\trefined\tadded\tstarted\tdone\tmerged\n"
            + "\n".join(
                f"{p.key}\t{p.sprint or ''}\t{p.points}\t{p.lines}\t"
                f"{p.created.isoformat()}\t{p.refined.isoformat()}\t"
                f"{p.added.isoformat() if p.added else ''}\t{p.started.isoformat()}\t"
                f"{p.done.isoformat() if p.done else ''}\t"
                f"{p.merged.isoformat() if p.merged else ''}"
                for p in plans) + "\n")
        run_sql(work_db, sql)
        log(f"SQL applied ({len(sql.splitlines())} statements)")
    except Exception:
        log("backdate failed - restoring database and restarting")
        sh(["sudo", "cp", str(backup), str(db_path)])
        sh(["sudo", "chown", DB_OWNER, str(db_path)])
        nerdctl("start", CONTAINER, check=False)
        raise

    sh(["sudo", "cp", str(work_db), str(db_path)])
    sh(["sudo", "chown", DB_OWNER, str(db_path)])
    log("updated database copied back")
    nerdctl("start", CONTAINER)
    log("container started, waiting for readiness")
    wait_ready()
    reindex(Api(args.base))
    verify_rest(Api(args.base), plans, points_field or "customfield_10106")
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

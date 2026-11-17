#!/usr/bin/env python3
"""Seed the jira-lab Data Center instance with the DEL corpus.

The data mirrors ``corpus/generate.py`` (the Bitbucket DC corpus) so the two
labs can be joined on ticket keys:

* project ``DEL`` ("Delivery Platform"), company-managed Scrum,
* epic ``DEL-1``,
* stories ``DEL-100`` .. ``DEL-199`` (100 issues), the same 20 themes and the
  same author/assignee rotation as the Bitbucket PR corpus
  (``AUTHORS[(i * 7 + 2) % 5]``),
* three sprints (two closed, one active) with the stories distributed over
  them, plus the remainder in the backlog,
* native Jira worklogs on every story, authored by the assignee (and sometimes
  a second engineer), backdated into the corpus window 2026-08-17 .. 2026-11-12.

Everything is deterministic (fixed RNG seed) and idempotent: re-running skips
users, project, epic, issues, sprints and worklogs that already exist. The
issue keys are only reproducible on a fresh instance (Jira numbers issues
sequentially), so the script verifies DEL-100..DEL-199 after creation and
fails loudly if the sequence drifted.

Usage
-----
    python3 jira-lab/seed.py                 # everything, resumable
    python3 jira-lab/seed.py --limit 5       # first 5 stories (smoke test)
    python3 jira-lab/seed.py --skip-worklogs # issues/sprints only
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

BASE = os.environ.get("JIRA_BASE", "http://127.0.0.1:8090").rstrip("/")
PROJECT = "DEL"
PROJECT_NAME = "Delivery Platform"
EPIC_KEY = "DEL-1"
EPIC_SUMMARY = "Delivery platform epic"
SEED = 20261115
WORK_START, WORK_END = 9, 18
WINDOW_START = date(2026, 8, 17)   # Monday, same window as the Bitbucket corpus
WINDOW_END = date(2026, 11, 12)    # Thursday
UTC = timezone.utc

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.env"


def config() -> dict:
    """Read config.env through bash so ${VAR:-default} expansion is honoured."""
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

# ---------------------------------------------------------------------------
# team (identical to corpus/generate.py, plus the sentinel)
# ---------------------------------------------------------------------------
USERS = [
    # name, displayName, email, password
    ("alice", "Alice Nguyen", "alice.nguyen@delivery.example", "lab-pw-alice"),
    ("bob", "Bob Ferreira", "bob.ferreira@delivery.example", "lab-pw-bob"),
    ("carol", "Carol Osei", "carol.osei@delivery.example", "lab-pw-carol"),
    ("dave", "Dave Kowalski", "dave.kowalski@delivery.example", "lab-pw-dave"),
    ("erin", "Erin Lindqvist", "erin.lindqvist@delivery.example", "lab-pw-erin"),
    ("frank", "Frank Moreau", "frank.moreau@delivery.example", "lab-pw-frank"),
]
USER_BY_NAME = {u[0]: u for u in USERS}
AUTHORS = USERS[:5]          # the five engineers own the stories
SENTINEL = "frank"           # release captain: reviews, never owns a story

# 20 themes x 5 tickets (copied verbatim from corpus/generate.py).
THEMES = [
    ("auth", "Add login form validation", "api",
     "Reject malformed e-mail addresses before hitting the auth service."),
    ("auth", "Support password reset by e-mail", "web",
     "One-time reset tokens, 30 minute TTL, invalidate on use."),
    ("orders", "Show order tracking timeline", "web",
     "Render the dispatch, pickup and delivered event stream."),
    ("orders", "Allow cancelling an order before dispatch", "api",
     "The cancellation window ends when a courier is assigned."),
    ("payments", "Retry failed card payments", "api",
     "Exponential backoff for soft declines, at most three attempts."),
    ("payments", "Surface payment decline reasons", "web",
     "Map processor codes to customer readable messages."),
    ("dispatch", "Assign courier by delivery zone", "api",
     "Prefer couriers that are already inside the destination zone."),
    ("dispatch", "Recalculate courier ETA on traffic events", "api",
     "Consume traffic webhooks and publish ETA updates."),
    ("pricing", "Zone based delivery pricing", "api",
     "Base fee per zone plus a distance surcharge."),
    ("pricing", "Promo code stacking rules", "api",
     "At most one percentage and one fixed-amount promo per order."),
    ("notify", "SMS notifications for dispatch events", "api",
     "Provider fallback: primary gateway first, then the secondary."),
    ("notify", "Attach receipts to confirmation e-mails", "api",
     "Generate PDF receipts and attach them to the confirmation mail."),
    ("profile", "Saved addresses CRUD", "web",
     "List, add, edit and delete saved delivery addresses."),
    ("profile", "Account deletion request flow", "web",
     "Two step confirmation plus a 14 day grace period."),
    ("search", "Address autocomplete", "web",
     "Debounced geocoder lookups with keyboard navigation."),
    ("search", "Fuzzy restaurant search", "web",
     "Tolerate typos and diacritics in restaurant names."),
    ("reports", "Daily delivery report export", "api",
     "Scheduled CSV export per region, retained for 30 days."),
    ("reports", "Courier performance dashboard", "web",
     "On-time rate, average delivery time and cancellation share."),
    ("infra", "Health check endpoint", "api",
     "Liveness and readiness probes including database latency."),
    ("infra", "Structured request logging", "api",
     "JSON access logs with request id propagation."),
]

WORKLOG_COMMENTS = [
    "Implemented the first slice and wired the tests.",
    "Paired on the failure path; added regression coverage.",
    "Debugged the staging repro, root cause was the retry budget.",
    "Reviewed the spec with the product owner and adjusted the payload.",
    "Cleanup, docs and a small refactor before hand-off.",
]

SPRINTS = [
    # name, start, end, state, story offsets
    #
    # The windows tile the Bitbucket PR window (2026-08-17 .. 2026-11-12) and
    # every story's first PR opens at/after its sprint's start, so the
    # Jira-layer "sprint -> in progress" segment is never inverted.  They are
    # the same dates ``jira-lab/backdate.py`` writes to the sprint objects.
    ("DEL Sprint 1", date(2026, 8, 17), date(2026, 9, 10), "closed", range(0, 30)),
    ("DEL Sprint 2", date(2026, 9, 11), date(2026, 10, 7), "closed", range(30, 60)),
    ("DEL Sprint 3", date(2026, 10, 8), date(2026, 11, 27), "active", range(60, 90)),
]
# DEL-190..DEL-199 stay in the backlog.


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[seed] {msg}", flush=True)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def is_workday(d: date) -> bool:
    return d.weekday() < 5


def business_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if is_workday(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def at(d: date, hour: float) -> datetime:
    h = int(hour)
    m = int(round((hour - h) * 60))
    if m >= 60:
        h, m = h + 1, m - 60
    return datetime.combine(d, dtime(min(h, WORK_END - 1), m), tzinfo=UTC)


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, user=SYSADMIN, data=None,
             ok=(200, 201, 204), retries: int = 3, raw: bool = False):
        url = self.base + path
        body = None
        headers = {"Accept": "application/json"}
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        tok = base64.b64encode(f"{user[0]}:{user[1]}".encode()).decode()
        headers["Authorization"] = "Basic " + tok
        last = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=body, method=method, headers=headers)
                with urllib.request.urlopen(req, timeout=120) as r:
                    txt = r.read().decode()
                    if r.status not in ok:
                        raise RuntimeError(f"{method} {path} -> {r.status}: {txt[:300]}")
                    if raw:
                        return txt
                    return json.loads(txt) if txt.strip() else {}
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors="replace")
                if e.code in ok:
                    if raw:
                        return txt
                    return json.loads(txt) if txt.strip() else {}
                # 401 right after a restart (Jira still warming its auth cache)
                # and 5xx are retried; anything else is a real error.
                if (e.code >= 500 or e.code == 401) and attempt < retries - 1:
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

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, data=None, **kw):
        return self.call("POST", path, data=data, **kw)

    def put(self, path, data=None, **kw):
        return self.call("PUT", path, data=data, **kw)

    def exists(self, path) -> bool:
        try:
            self.call("GET", path)
            return True
        except RuntimeError:
            return False


# ---------------------------------------------------------------------------
# users / project / roles
# ---------------------------------------------------------------------------
def ensure_users(api: Api) -> None:
    for name, display, email, pw in USERS:
        if api.exists(f"/rest/api/2/user?username={urllib.parse.quote(name)}"):
            continue
        api.post("/rest/api/2/user", data={
            "name": name,
            "password": pw,
            "emailAddress": email,
            "displayName": display,
            "notification": False,
            "applicationKeys": ["jira-software"],
        })
        log(f"user {name} created")

    # The scrum template's permission scheme grants "Work on issues" to the
    # Developers role; make sure every lab user has it (needed to log work as
    # themselves). Groups vary per install, so a missing group is not fatal.
    for name, _, _, _ in USERS:
        try:
            api.post("/rest/api/2/group/user?groupname=jira-developers", data={"name": name})
        except RuntimeError as e:
            if "does not exist" not in str(e):
                log(f"  group add {name}: {e}")


def ensure_project(api: Api) -> dict:
    if api.exists(f"/rest/api/2/project/{PROJECT}"):
        return api.get(f"/rest/api/2/project/{PROJECT}")
    api.post("/rest/api/2/project", data={
        "key": PROJECT,
        "name": PROJECT_NAME,
        "description": "Delivery platform: API, web app and operations tooling",
        "projectTypeKey": "software",
        "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-scrum-template",
        "lead": SYSADMIN[0],
        "assigneeType": "PROJECT_LEAD",
    })
    log(f"project {PROJECT} created (company-managed scrum)")
    return api.get(f"/rest/api/2/project/{PROJECT}")


def ensure_roles(api: Api) -> None:
    roles = api.get(f"/rest/api/2/project/{PROJECT}/role")
    for role_name, role_url in roles.items():
        if role_name not in ("Developers", "Users"):
            continue
        # Jira returns absolute self links; Api.call wants a path.
        if role_url.startswith("http"):
            role_url = urllib.parse.urlparse(role_url).path
        role = api.get(role_url)
        have = {a.get("name") for a in role.get("actors", [])}
        for name, _, _, _ in USERS:
            if name in have:
                continue
            api.post(role_url, data={"user": [name]})
            log(f"  role {role_name} += {name}")


def issue_types(api: Api) -> dict:
    proj = api.get(f"/rest/api/2/project/{PROJECT}")
    return {it["name"].lower(): it["id"] for it in proj["issueTypes"]}


def fields_by_name(api: Api) -> dict:
    return {f["name"]: f["id"] for f in api.get("/rest/api/2/field")}


# ---------------------------------------------------------------------------
# issues
# ---------------------------------------------------------------------------
def ticket_plan(limit: int | None = None) -> list[dict]:
    rng = random.Random(SEED)
    days = business_days(WINDOW_START, WINDOW_END)
    tickets = []
    for i in range(100):
        area, title, kind, detail = THEMES[i % len(THEMES)]
        repo = "delivery-web" if kind == "web" else "delivery-api"
        day = days[min(len(days) - 1, (i * len(days)) // 100)]
        tickets.append({
            "key": f"DEL-{100 + i}", "index": i, "area": area, "title": title,
            "detail": detail, "repo": repo,
            "assignee": AUTHORS[(i * 7 + 2) % len(AUTHORS)][0],
            "open_at": iso(at(day, 9.0 + rng.uniform(0, 6.5))),
        })
    return tickets[:limit] if limit else tickets


def description(t: dict) -> str:
    return (
        "Epic: DEL-1\n\n"
        f"Ticket: {t['key']}\n\n"
        f"{t['detail']}\n\n"
        f"Area: {t['area']}\n"
        f"Repo: {t['repo']}\n"
    )


def ensure_epic(api: Api, types: dict, fields: dict) -> None:
    if api.exists(f"/rest/api/2/issue/{EPIC_KEY}"):
        return
    body = {
        "fields": {
            "project": {"key": PROJECT},
            "issuetype": {"id": types["epic"]},
            "summary": EPIC_SUMMARY,
            "description": "Delivery platform epic: API, web app and operations tooling.",
            "labels": ["epic"],
        }
    }
    if "Epic Name" in fields:
        body["fields"][fields["Epic Name"]] = "Delivery Platform"
    created = api.post("/rest/api/2/issue", data=body)
    if created.get("key") != EPIC_KEY:
        raise RuntimeError(f"epic landed on {created.get('key')}, expected {EPIC_KEY} "
                           "(seed a fresh instance: lab-down.sh PURGE=1)")
    log(f"epic {EPIC_KEY} created")


def ensure_key_sequence(api: Api, types: dict) -> None:
    """Burn the DEL-2..DEL-99 keys so the stories land on DEL-100..DEL-199.

    Jira numbers issues sequentially per project and offers no way to set the
    counter over REST. The Bitbucket corpus uses DEL-100..DEL-199, so after the
    epic (DEL-1) we create 98 filler issues and delete them again: the counter
    is monotonic, so the next real issue is DEL-100 and the project itself only
    ever contains the epic plus the 100 stories.
    """
    if api.exists("/rest/api/2/issue/DEL-100"):
        return

    filler = "key-sequence filler (created and deleted by seed.py)"
    # clean up leftovers from an interrupted run first
    jql = urllib.parse.quote(f'project={PROJECT} AND summary ~ "key-sequence filler"')
    leftovers = [i["key"] for i in
                 api.get(f"/rest/api/2/search?jql={jql}&maxResults=200&fields=key")
                 .get("issues", [])]
    if leftovers:
        log(f"removing {len(leftovers)} filler issue(s) from a previous run")
        delete_issues(api, leftovers)

    def create(n: int) -> list[str]:
        keys: list[str] = []
        while n > 0:
            batch = [{"fields": {"project": {"key": PROJECT},
                                 "issuetype": {"id": types["story"]},
                                 "summary": filler}} for _ in range(min(n, 50))]
            res = api.post("/rest/api/2/issue/bulk", data={"issueUpdates": batch})
            keys.extend(i["key"] for i in res.get("issues", []))
            n -= len(batch)
        return keys

    # Probe the counter with one issue, then burn up to DEL-99.
    first = create(1)[0]
    num = int(first.split("-")[1])
    if num > 99:
        raise RuntimeError(
            f"project counter already at {first}: cannot reach DEL-100. "
            "Start from a fresh instance (PURGE=1 ./jira-lab/lab-down.sh).")
    created = [first] + create(99 - num)
    if created[-1] != "DEL-99":
        raise RuntimeError(f"unexpected filler tail {created[-1]}")
    log(f"burned {len(created)} filler keys ({created[0]} .. {created[-1]})")
    delete_issues(api, created)


def delete_issues(api: Api, keys: list[str]) -> None:
    for i in range(0, len(keys), 100):
        chunk = keys[i:i + 100]
        try:
            api.post("/rest/api/2/bulk/delete",
                     data={"selectedIssueIdsOrKeys": chunk}, ok=(200, 204))
        except RuntimeError:
            for k in chunk:
                api.call("DELETE", f"/rest/api/2/issue/{k}", ok=(204, 200))


def ensure_issues(api: Api, types: dict, fields: dict, tickets: list[dict]) -> None:
    epic_link = fields.get("Epic Link")
    if not epic_link:
        raise RuntimeError("no 'Epic Link' field: project is not company-managed")
    for t in tickets:
        if api.exists(f"/rest/api/2/issue/{t['key']}"):
            continue
        priority = ("High", "Medium", "Low")[t["index"] % 3]
        body = {
            "fields": {
                "project": {"key": PROJECT},
                "issuetype": {"id": types["story"]},
                "summary": t["title"],
                "description": description(t),
                "assignee": {"name": t["assignee"]},
                "labels": [t["area"], f"repo-{t['repo']}"],
                "priority": {"name": priority},
                epic_link: EPIC_KEY,
            }
        }
        created = api.post("/rest/api/2/issue", data=body)
        if created.get("key") != t["key"]:
            raise RuntimeError(
                f"issue landed on {created.get('key')}, expected {t['key']} - the "
                "instance already has issues; seed a fresh one "
                "(PURGE=1 ./jira-lab/lab-down.sh) or accept the drift")
        log(f"  {t['key']} {t['title']} ({t['assignee']})")


# ---------------------------------------------------------------------------
# sprints
# ---------------------------------------------------------------------------
def board_id(api: Api) -> int:
    boards = api.get(f"/rest/agile/1.0/board?projectKeyOrId={PROJECT}")
    for b in boards.get("values", []):
        if b.get("type") == "scrum":
            return b["id"]
    raise RuntimeError(f"no scrum board for project {PROJECT}")


def ensure_sprints(api: Api, bid: int, tickets: list[dict]) -> dict:
    """Create the sprints (as future sprints) and put the stories in them.

    Jira's sprint-create bean accepts name/startDate/endDate/originBoardId/goal
    only; starting and completing happen through PUT on the sprint afterwards
    (see finalize_sprints).
    """
    existing = {s["name"]: s for s in
                api.get(f"/rest/agile/1.0/board/{bid}/sprint?maxResults=100").get("values", [])}
    out = {}
    for name, start, end, state, offsets in SPRINTS:
        sprint = existing.get(name)
        if sprint is None:
            sprint = api.post("/rest/agile/1.0/sprint", data={
                "name": name,
                "originBoardId": bid,
                "startDate": iso(datetime.combine(start, dtime(9, 0), tzinfo=UTC)),
                "endDate": iso(datetime.combine(end, dtime(17, 0), tzinfo=UTC)),
                "goal": f"{name} scope",
            })
            log(f"sprint {name} created ({sprint.get('state', 'future')})")
        out[name] = sprint
        keys = [tickets[i]["key"] for i in offsets if i < len(tickets)]
        if not keys:
            continue
        have = set()
        try:
            have = {i["key"] for i in
                    api.get(f"/rest/agile/1.0/sprint/{sprint['id']}/issue?maxResults=200")
                    .get("issues", [])}
        except RuntimeError:
            pass
        missing = [k for k in keys if k not in have]
        for i in range(0, len(missing), 25):
            api.post(f"/rest/agile/1.0/sprint/{sprint['id']}/issue",
                     data={"issues": missing[i:i + 25]})
        if missing:
            log(f"  {name} += {len(missing)} issues")
    return out


def finalize_sprints(api: Api, sprints: dict) -> None:
    """Start the active sprint, start+complete the closed ones (after the
    stories in them are Done, so completing does not push work to the backlog)."""
    for name, start, end, state, _ in SPRINTS:
        sprint = sprints.get(name)
        if not sprint:
            continue
        cur = sprint.get("state")
        base = {"name": name,
                "startDate": iso(datetime.combine(start, dtime(9, 0), tzinfo=UTC)),
                "endDate": iso(datetime.combine(end, dtime(17, 0), tzinfo=UTC)),
                "goal": f"{name} scope"}
        if state == "active":
            if cur != "active":
                api.put(f"/rest/agile/1.0/sprint/{sprint['id']}",
                        data={**base, "state": "active"})
                log(f"sprint {name} started")
            continue
        if cur == "closed":
            continue
        if cur != "active":
            api.put(f"/rest/agile/1.0/sprint/{sprint['id']}",
                    data={**base, "state": "active"})
        api.put(f"/rest/agile/1.0/sprint/{sprint['id']}", data={
            **base,
            "state": "closed",
            "completeDate": iso(datetime.combine(end, dtime(16, 0), tzinfo=UTC)),
        })
        log(f"sprint {name} closed")


# ---------------------------------------------------------------------------
# status transitions
# ---------------------------------------------------------------------------
def transitions(api: Api, key: str) -> dict:
    return {t["name"].lower(): t["id"] for t in
            api.get(f"/rest/api/2/issue/{key}/transitions").get("transitions", [])}


def transition(api: Api, key: str, name: str) -> None:
    ts = transitions(api, key)
    if name.lower() not in ts:
        return
    api.post(f"/rest/api/2/issue/{key}/transitions",
             data={"transition": {"id": ts[name.lower()]}})


def ensure_statuses(api: Api, tickets: list[dict]) -> None:
    rng = random.Random(SEED + 3)
    for t in tickets:
        try:
            cur = api.get(f"/rest/api/2/issue/{t['key']}?fields=status")["fields"]["status"]["name"]
        except RuntimeError:
            continue
        if cur.lower() != "to do":
            continue
        sprint = next((s for s in SPRINTS if t["index"] in s[4]), None)
        if sprint is None:
            continue
        state = sprint[3]
        if state == "closed":
            target = "done"
        else:
            roll = rng.random()
            target = "done" if roll < 0.3 else ("in progress" if roll < 0.7 else "to do")
        if target != "to do":
            transition(api, t["key"], target)
            if target == "done":
                transition(api, t["key"], "done")


# ---------------------------------------------------------------------------
# worklogs
# ---------------------------------------------------------------------------
def worklog_plan(t: dict) -> list[dict]:
    """Deterministic 1-3 worklogs per story inside its sprint window."""
    rng = random.Random(SEED + 1000 + t["index"])
    sprint = next((s for s in SPRINTS if t["index"] in s[4]), None)
    if sprint:
        days = business_days(sprint[1], sprint[2])
        # keep the active sprint's logs in the past (server "now" is 2026-11-16)
        days = [d for d in days if d <= date(2026, 11, 14)] or days
    else:
        days = business_days(WINDOW_START, WINDOW_END)
    out = []
    n = 1 + t["index"] % 3
    for j in range(n):
        day = days[min(len(days) - 1, (t["index"] * 3 + j * 7) % len(days))]
        author = t["assignee"]
        if j == 2 and t["index"] % 4 == 0:
            others = [u[0] for u in AUTHORS if u[0] != t["assignee"]]
            author = others[t["index"] % len(others)]
        out.append({
            "author": author,
            "started": iso(at(day, 9.5 + rng.uniform(0, 6.5))),
            "timeSpentSeconds": (1800, 3600, 5400, 7200, 10800, 14400)[
                (t["index"] + j) % 6],
            "comment": WORKLOG_COMMENTS[(t["index"] + j) % len(WORKLOG_COMMENTS)],
        })
    return out


def ensure_worklogs(api: Api, tickets: list[dict]) -> int:
    added = 0
    for t in tickets:
        plan = worklog_plan(t)
        try:
            have = api.get(f"/rest/api/2/issue/{t['key']}/worklog").get("worklogs", [])
        except RuntimeError:
            have = []
        seen = {(w["author"]["name"], w["started"][:16], w["timeSpentSeconds"])
                for w in have}
        for w in plan:
            key = (w["author"], w["started"][:16], w["timeSpentSeconds"])
            if key in seen:
                continue
            api.post(f"/rest/api/2/issue/{t['key']}/worklog?adjustEstimate=leave",
                     user=(w["author"], USER_BY_NAME[w["author"]][3]),
                     data={"started": w["started"],
                           "timeSpentSeconds": w["timeSpentSeconds"],
                           "comment": w["comment"]})
            added += 1
    log(f"worklogs added: {added}")
    return added


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--limit", type=int, default=None,
                    help="seed only the first N stories (smoke test)")
    ap.add_argument("--skip-worklogs", action="store_true")
    args = ap.parse_args()

    api = Api(args.base)
    info = api.get("/rest/api/2/serverInfo")
    log(f"target {args.base} Jira {info.get('version')} ({info.get('serverTitle')})")
    if not api.exists(f"/rest/api/2/user?username={SYSADMIN[0]}"):
        raise SystemExit("sysadmin not found - run jira-lab/setup.py first")

    ensure_users(api)
    ensure_project(api)
    ensure_roles(api)
    types = issue_types(api)
    fields = fields_by_name(api)
    ensure_epic(api, types, fields)
    ensure_key_sequence(api, types)

    tickets = ticket_plan(args.limit)
    ensure_issues(api, types, fields, tickets)
    sprints = ensure_sprints(api, board_id(api), tickets)
    ensure_statuses(api, tickets)
    finalize_sprints(api, sprints)
    if not args.skip_worklogs:
        ensure_worklogs(api, tickets)

    # summary
    jql = urllib.parse.quote(f"project={PROJECT} ORDER BY key ASC")
    total = api.get(f"/rest/api/2/search?jql={jql}&maxResults=0").get("total", 0)
    log(f"done: {total} issues in {PROJECT} (epic {EPIC_KEY} + {len(tickets)} stories)")
    log("verify with: python3 jira-lab/verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Verify the jira-lab fixture over the Jira REST + Agile REST APIs.

Checks the contract the megaspeed ingest relies on:

* Jira Software Data Center 9.17 is up, sysadmin can authenticate,
* project ``DEL`` exists and is a software project,
* epic ``DEL-1`` exists,
* stories ``DEL-100`` .. ``DEL-199`` exist exactly once each, all linked to
  ``DEL-1``, assigned to alice/bob/carol/dave/erin,
* three sprints exist (two closed, one active) with the expected story counts,
* every story has native worklogs, authored by the expected engineers.

Exit code 0 = all checks pass. Read-only: it never writes to the instance.

    python3 jira-lab/verify.py
    python3 jira-lab/verify.py --base http://localhost:8090
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.env"


def config() -> dict:
    import subprocess
    keys = ("SYSADMIN_USER", "SYSADMIN_PASSWORD", "BASE_URL", "JIRA_HTTP_PORT")
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
DEFAULT_BASE = CFG.get("BASE_URL") or f"http://localhost:{CFG.get('JIRA_HTTP_PORT', '8090')}"
SYSADMIN = (CFG.get("SYSADMIN_USER", "admin"), CFG.get("SYSADMIN_PASSWORD", "admin-lab-pw"))
PROJECT = "DEL"
EPIC = "DEL-1"
STORY_KEYS = [f"DEL-{100 + i}" for i in range(100)]
EXPECTED_ASSIGNEES = {f"DEL-{100 + i}": ("alice", "bob", "carol", "dave", "erin")[(i * 7 + 2) % 5]
                      for i in range(100)}
SPRINT_SPEC = {"DEL Sprint 1": "closed", "DEL Sprint 2": "closed", "DEL Sprint 3": "active"}

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    RESULTS.append((bool(ok), msg))
    print(f"  [{'ok' if ok else 'FAIL'}] {msg}", flush=True)


class Api:
    def __init__(self, base: str, user=SYSADMIN):
        self.base = base.rstrip("/")
        self.user = user

    def get(self, path: str, params: dict | None = None, accept: str = "application/json"):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        tok = base64.b64encode(f"{self.user[0]}:{self.user[1]}".encode()).decode()
        req = urllib.request.Request(url, headers={"Accept": accept,
                                                   "Authorization": "Basic " + tok})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read().decode()
                return r.status, (json.loads(body) if body.strip() else {})
        except urllib.error.HTTPError as e:
            return e.code, {}

    def pages(self, path: str, params: dict | None = None, key="issues"):
        start = 0
        while True:
            q = dict(params or {}, startAt=start, maxResults=100)
            code, page = self.get(path, q)
            if code != 200:
                raise RuntimeError(f"GET {path} -> {code}")
            yield from page.get(key, [])
            if page.get("isLast", True) or page.get("total", 0) <= start + len(page.get(key, [])):
                return
            start += len(page.get(key, []))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    api = Api(args.base)

    print(f"Jira REST verification against {args.base}\n")

    # --- server -----------------------------------------------------------
    print("server")
    code, info = api.get("/rest/api/2/serverInfo")
    check(code == 200, f"/rest/api/2/serverInfo -> {code}")
    check(info.get("version", "").startswith("9."), f"Jira version {info.get('version')}")
    check(info.get("serverTitle") == "DEL Lab", f"server title {info.get('serverTitle')!r}")
    code, me = api.get("/rest/api/2/myself")
    check(code == 200 and me.get("name") == SYSADMIN[0],
          f"sysadmin {SYSADMIN[0]} authenticates")
    code, roles = api.get("/rest/api/2/applicationrole")
    sw = [r for r in (roles if isinstance(roles, list) else []) if r.get("key") == "jira-software"]
    check(bool(sw) and sw[0].get("numberOfSeats", 0) >= 10,
          f"Jira Software DC license: {sw[0].get('numberOfSeats') if sw else '?'} seats, "
          f"{sw[0].get('userCount') if sw else '?'} used")
    code, app_lic = api.get("/rest/plugins/applications/1.0/installed/jira-software/license",
                            accept="*/*")
    check(code == 200 and bool(app_lic.get("valid")) and not app_lic.get("expired"),
          f"Jira Software app license valid until {app_lic.get('expiryDateString', '?')} "
          f"(Jira grants 3 h per start; ./jira-lab/setup.py re-arms it)")

    # --- project + epic ---------------------------------------------------
    print("\nproject")
    code, proj = api.get(f"/rest/api/2/project/{PROJECT}")
    check(code == 200 and proj.get("key") == PROJECT, f"project {PROJECT} exists")
    check(proj.get("projectTypeKey") == "software",
          f"project type {proj.get('projectTypeKey')!r}")
    code, epic = api.get(f"/rest/api/2/issue/{EPIC}")
    check(code == 200 and epic["fields"]["issuetype"]["name"] == "Epic",
          f"epic {EPIC} = {epic.get('fields', {}).get('summary')!r}")

    # --- stories ----------------------------------------------------------
    print("\nstories")
    # field ids differ per install; resolve them by name instead of guessing
    code, fields = api.get("/rest/api/2/field")
    epic_link = next((f["id"] for f in fields if f.get("name") == "Epic Link"), None)
    sprint_field = next((f["id"] for f in fields if f.get("name") == "Sprint"), None)
    jql = f"project={PROJECT} AND issuetype=Story ORDER BY key ASC"
    want = "summary,assignee,status,labels" + (f",{epic_link}" if epic_link else "")
    stories = list(api.pages("/rest/api/2/search", {"jql": jql, "fields": want}))
    keys = [s["key"] for s in stories]
    check(len(stories) == 100, f"story count = {len(stories)} (expected 100)")
    check(keys == STORY_KEYS, f"keys are DEL-100..DEL-199 (first {keys[:1]}, last {keys[-1:]})")
    if epic_link:
        def epic_key(v):
            # Jira returns the Epic Link either as {"key": ...} or as the bare key
            return v.get("key") if isinstance(v, dict) else v

        linked = [s for s in stories
                  if epic_key(s["fields"].get(epic_link)) == EPIC]
        check(len(linked) == 100, f"{len(linked)}/100 stories linked to {EPIC} "
                                  f"(field {epic_link})")
    else:
        check(False, "no 'Epic Link' field found")
    wrong = [s["key"] for s in stories
             if (s["fields"].get("assignee") or {}).get("name")
             != EXPECTED_ASSIGNEES[s["key"]]]
    check(not wrong, f"assignees match the Bitbucket author rotation"
                     + (f" (mismatches: {wrong[:5]})" if wrong else ""))
    names = sorted({(s["fields"].get("assignee") or {}).get("name") for s in stories})
    check(names == ["alice", "bob", "carol", "dave", "erin"], f"assignee set = {names}")

    # --- sprints ----------------------------------------------------------
    print("\nagile")
    code, boards = api.get("/rest/agile/1.0/board", {"projectKeyOrId": PROJECT})
    check(code == 200 and boards.get("total", 0) >= 1,
          f"/rest/agile/1.0/board -> {boards.get('total')} board(s)")
    board = (boards.get("values") or [{}])[0]
    check(board.get("type") == "scrum", f"board {board.get('id')} type {board.get('type')!r}")
    code, sprints = api.get(f"/rest/agile/1.0/board/{board.get('id')}/sprint",
                            {"maxResults": 50})
    by_name = {s["name"]: s for s in sprints.get("values", [])}
    for name, state in SPRINT_SPEC.items():
        s = by_name.get(name)
        check(s is not None and s.get("state") == state,
              f"sprint {name!r} state={s.get('state') if s else 'MISSING'} (expected {state})")
    counts = {}
    for name, s in by_name.items():
        issues = list(api.pages(f"/rest/agile/1.0/sprint/{s['id']}/issue", {"fields": "key"}))
        counts[name] = len(issues)
    check(counts.get("DEL Sprint 1") == 30 and counts.get("DEL Sprint 2") == 30
          and counts.get("DEL Sprint 3") == 30,
          f"sprint membership {counts}")
    # sprint field on an issue
    if sprint_field:
        code, iss = api.get(f"/rest/api/2/issue/DEL-160",
                            {"fields": sprint_field})
        sv = iss.get("fields", {}).get(sprint_field) or []
        names = [x.get("name") if isinstance(x, dict) else str(x) for x in sv]
        check(any("DEL Sprint 3" in n for n in names),
              f"DEL-160 carries sprint {names} ({sprint_field})")

    # --- statuses ---------------------------------------------------------
    print("\nstatuses")
    done = [s for s in stories if s["fields"]["status"]["name"] == "Done"]
    check(len(done) >= 60, f"{len(done)}/100 stories Done (closed sprints + active work)")

    # --- worklogs ---------------------------------------------------------
    print("\nworklogs")
    total = 0
    authors: dict[str, int] = {}
    missing: list[str] = []
    for s in stories:
        code, wl = api.get(f"/rest/api/2/issue/{s['key']}/worklog")
        logs = wl.get("worklogs", []) if code == 200 else []
        if not logs:
            missing.append(s["key"])
        total += len(logs)
        for w in logs:
            authors[w["author"]["name"]] = authors.get(w["author"]["name"], 0) + 1
    check(total >= 150, f"{total} native worklogs across 100 stories")
    check(not missing, f"every story has worklogs" + (f" (missing: {missing[:5]})" if missing else ""))
    check(set(authors) <= {"alice", "bob", "carol", "dave", "erin"} and len(authors) >= 4,
          f"worklog authors = {dict(sorted(authors.items()))}")
    code, sample = api.get("/rest/api/2/issue/DEL-100/worklog")
    if sample.get("worklogs"):
        w = sample["worklogs"][0]
        print(f"    sample DEL-100 worklog: {w['author']['name']} "
              f"{w['timeSpent']} started {w['started']} :: {w.get('comment')}")

    # --- summary ----------------------------------------------------------
    failed = [m for ok, m in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILED:")
        for m in failed:
            print(f"  - {m}")
        return 1
    print("PASS - jira-lab fixture matches the DEL corpus")
    return 0


if __name__ == "__main__":
    sys.exit(main())

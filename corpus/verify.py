#!/usr/bin/env python3
"""Verify the generated DEL corpus and print / store a summary.

Checks the filter contract (ticket key in branch + title + description, epic
reference in every description), the outcome mix, the date spread of PRs and
commits, and the review interaction (reviewers, comments, approvals).

    python3 corpus/verify.py            # human summary + build/summary.json
    python3 corpus/verify.py --json     # machine summary only
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "http://127.0.0.1:7991"
PROJECT = "DEL"
ADMIN = ("admin", "admin-lab-pw")
REPOS = ("delivery-api", "delivery-web")
KEY_RE = re.compile(r"\b[A-Z]+-\d+\b")
OUT = Path(__file__).resolve().parent / "build" / "summary.json"


def api(path: str, params: dict | None = None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " +
                   base64.b64encode(f"{ADMIN[0]}:{ADMIN[1]}".encode()).decode())
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def pages(path: str, params: dict | None = None):
    start = 0
    while True:
        q = dict(params or {})
        q.update({"start": start, "limit": 100})
        page = api(path, q)
        yield from page.get("values", [])
        if page.get("isLastPage", True):
            return
        start = page.get("nextPageStart", start + 100)


def dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    users = pages("/rest/api/1.0/users", {"limit": 100})
    team = {u["name"] for u in users if u["name"] in
            {"alice", "bob", "carol", "dave", "erin", "frank"}}

    summary = {"users": sorted(team), "repos": {}, "prs": [], "problems": []}
    states = collections.Counter()
    authors = collections.Counter()
    reviewers = collections.Counter()
    approvals = collections.Counter()
    action_counts = collections.Counter()
    ticket_prs: dict[str, int] = collections.Counter()
    opened_all, closed_all, commit_dates = [], [], []
    threads_resolved = threads_open = 0
    resolvers = collections.Counter()
    reviewer_additions = reviewer_removals = 0
    merged_with_commit = merged_without_commit = 0
    commits_per_pr: list[int] = []

    for repo in REPOS:
        prs = list(pages(f"/rest/api/1.0/projects/{PROJECT}/repos/{repo}/pull-requests",
                         {"state": "ALL"}))
        summary["repos"][repo] = {"pull_requests": len(prs)}
        for pr in prs:
            states[pr["state"]] += 1
            authors[pr["author"]["user"]["name"]] += 1
            branch = pr["fromRef"]["displayId"]
            title = pr["title"]
            desc = pr.get("description") or ""
            keys = set(KEY_RE.findall(branch)) | set(KEY_RE.findall(title)) | set(KEY_RE.findall(desc))
            if not keys:
                summary["problems"].append(f"no key: {repo} {branch}")
            if "Epic: DEL-1" not in desc:
                summary["problems"].append(f"no epic line: {repo} #{pr['id']}")
            for k in keys:
                if k != "DEL-1":  # the epic reference
                    ticket_prs[k] += 1

            parts = api(f"/rest/api/1.0/projects/{PROJECT}/repos/{repo}"
                        f"/pull-requests/{pr['id']}/participants")["values"]
            revs = [p for p in parts if p["role"] == "REVIEWER"]
            for p in revs:
                reviewers[p["user"]["name"]] += 1
                if p.get("approved"):
                    approvals[p["user"]["name"]] += 1

            acts = list(pages(f"/rest/api/1.0/projects/{PROJECT}/repos/{repo}"
                              f"/pull-requests/{pr['id']}/activities"))
            for a in acts:
                action_counts[a["action"]] += 1
                if a["action"] == "COMMENTED":
                    c = a.get("comment") or {}
                    if c.get("id") is not None:
                        resolved = (bool(c.get("threadResolved"))
                                    or str(c.get("state") or "OPEN").upper() != "OPEN")
                        if resolved:
                            threads_resolved += 1
                            resolver = (c.get("resolver") or {}).get("name")
                            if resolver:
                                resolvers[resolver] += 1
                        else:
                            threads_open += 1
                elif a["action"] == "UPDATED":
                    reviewer_additions += len(a.get("addedReviewers") or [])
                    reviewer_removals += len(a.get("removedReviewers") or [])
                elif a["action"] == "MERGED":
                    if (a.get("commit") or {}).get("id"):
                        merged_with_commit += 1
                    else:
                        merged_without_commit += 1
            commits = list(pages(f"/rest/api/1.0/projects/{PROJECT}/repos/{repo}"
                                 f"/pull-requests/{pr['id']}/commits"))
            commits_per_pr.append(len(commits))
            for c in commits:
                commit_dates.append(c["authorTimestamp"])
            opened_all.append(pr["createdDate"])
            if pr.get("closedDate"):
                closed_all.append(pr["closedDate"])

            summary["prs"].append({
                "repo": repo, "id": pr["id"], "state": pr["state"],
                "branch": branch, "author": pr["author"]["user"]["name"],
                "reviewers": [(p["user"]["name"], p["status"]) for p in revs],
                "opened": dt(pr["createdDate"]).isoformat(),
                "closed": dt(pr["closedDate"]).isoformat() if pr.get("closedDate") else None,
                "activities": len(acts), "commits": len(commits),
                "keys": sorted(keys),
            })

    out = {
        "users": sorted(team),
        "repos": summary["repos"],
        "pull_requests_total": sum(states.values()),
        "states": dict(states),
        "authors": dict(authors),
        "reviewers": dict(reviewers),
        "approvals": dict(approvals),
        "activity_types": dict(action_counts),
        "threads": {
            "resolved": threads_resolved,
            "open": threads_open,
            "resolvers": dict(resolvers),
        },
        "reviewer_changes": {
            "added": reviewer_additions,
            "removed": reviewer_removals,
        },
        "merge_commits": {
            "with_commit": merged_with_commit,
            "without_commit": merged_without_commit,
        },
        "commits_per_pr": {
            "max": max(commits_per_pr) if commits_per_pr else 0,
            "over_10": sum(1 for n in commits_per_pr if n > 10),
            "total": sum(commits_per_pr),
        },
        "tickets_with_prs": len(ticket_prs),
        "epic_reference_in_all_descriptions": not any(
            "no epic line" in p for p in summary["problems"]),
        "ticket_range": [min(ticket_prs), max(ticket_prs)] if ticket_prs else None,
        "prs_per_ticket": dict(sorted(ticket_prs.items())),
        "pr_opened_range": [dt(min(opened_all)).isoformat(), dt(max(opened_all)).isoformat()]
        if opened_all else None,
        "pr_closed_range": [dt(min(closed_all)).isoformat(), dt(max(closed_all)).isoformat()]
        if closed_all else None,
        "commit_range": [dt(min(commit_dates)).isoformat(), dt(max(commit_dates)).isoformat()]
        if commit_dates else None,
        "problems": summary["problems"],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1))

    if args.json:
        print(json.dumps(out, indent=1))
        return 0

    print("users:        ", ", ".join(out["users"]))
    print("repos:        ", out["repos"])
    print("PRs:          ", out["pull_requests_total"], out["states"])
    print("authors:      ", out["authors"])
    print("reviewers:    ", out["reviewers"])
    print("approvals:    ", out["approvals"])
    print("activities:   ", out["activity_types"])
    print("threads:      ", out["threads"])
    print("reviewer chg: ", out["reviewer_changes"])
    print("merge commits:", out["merge_commits"])
    print("commits/PR:   ", out["commits_per_pr"])
    print("tickets:      ", out["tickets_with_prs"], "ticket range", out["ticket_range"])
    print("PR opened:    ", out["pr_opened_range"])
    print("PR closed:    ", out["pr_closed_range"])
    print("commits:      ", out["commit_range"])
    print("problems:     ", len(out["problems"]))
    for p in out["problems"][:10]:
        print("   -", p)
    return 0 if not out["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())

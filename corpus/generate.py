#!/usr/bin/env python3
"""Generate a realistic multi-person, multi-ticket Bitbucket DC PR corpus.

Target: the licensed lab instance ``bb-lab-b`` (http://127.0.0.1:7991), project
``DEL``.  Everything is deterministic (fixed RNG seed) and resumable: the plan
lives in ``corpus/build/plan.json`` and per-PR progress in
``corpus/build/state.json``.  Re-running skips completed PRs and re-uses
existing users/projects/repos/PRs.

Data model
----------
* 5 team members (created through /rest/api/1.0/admin/users).
* project DEL with two repos: delivery-api (Python) / delivery-web (JS).
* epic DEL-1; tickets DEL-100..DEL-199 (100 tickets), 1-2 PRs per ticket.
* every PR title/branch/commit/description carries the ticket key, every
  description starts with ``Epic: DEL-1`` (the ingest's filter contract).
* ~3 months of planned working time (2026-08-17 .. 2026-11-12).  Git commits
  are authored/committed with backdated timestamps; PR/comment/approval events
  get server "now" timestamps from REST and are rewritten afterwards by
  ``backdate.py`` (see README).

Usage
-----
    python3 corpus/generate.py                 # everything, resumable
    python3 corpus/generate.py --limit 5       # first 5 tickets (smoke test)
    python3 corpus/generate.py --reset         # forget local progress (server
                                               # data is left alone; delete the
                                               # project by hand for a clean run)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

BASE = os.environ.get("BB_BASE", "http://127.0.0.1:7991").rstrip("/")
HOSTPORT = urllib.parse.urlparse(BASE).netloc
PROJECT = "DEL"
SYSADMIN = ("admin", "admin-lab-pw")
SEED = 20261115
WORK_START, WORK_END = 9, 18
WINDOW_START = date(2026, 8, 17)   # Monday
WINDOW_END = date(2026, 11, 12)    # Thursday (last working day in the window)
CUTOFF = datetime(2026, 11, 13, 17, 0, tzinfo=timezone.utc)
UTC = timezone.utc

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
GITDIR = BUILD / "git"
PLAN_FILE = BUILD / "plan.json"
STATE_FILE = BUILD / "state.json"

# ---------------------------------------------------------------------------
# team
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

# The five engineers author PRs; frank is the release captain / sentinel.
# Keeping the sentinel out of the author rotation is what makes the mandatory
# approval a real second pair of eyes rather than a self-approval.
AUTHORS = USERS[:5]

# Every PR needs frank's approval before it can merge.  The ingest side only
# knows that role from config (``[review] sentinel_slugs`` / env
# ``MEGASPEED_SENTINEL_SLUGS``), so a live scrape of this corpus must set
# ``MEGASPEED_SENTINEL_SLUGS=frank`` (see corpus/README.md).
SENTINEL = "frank"

# A handful of PRs are deliberately oversized so ``analysis.rules.pr_size``
# has something to fire on: three with >10 commits and three with a >400-line
# diff.  Indices are ticket offsets (DEL-100 + index).
LARGE_COMMIT_INDICES = {17, 45, 73}   # DEL-117, DEL-145, DEL-173
LARGE_DIFF_INDICES = {31, 59, 87}     # DEL-131, DEL-159, DEL-187

REPOS = {
    "delivery-api": {
        "name": "delivery-api",
        "description": "Delivery orchestration API (FastAPI services, couriers, pricing)",
    },
    "delivery-web": {
        "name": "delivery-web",
        "description": "Customer + courier web app (vanilla ES modules)",
    },
}

# 20 themes x 5 tickets. kind -> repo.
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

SLUGS = {
    "auth": "auth", "orders": "orders", "payments": "payments",
    "dispatch": "dispatch", "pricing": "pricing", "notify": "notifications",
    "profile": "profile", "search": "search", "reports": "reports",
    "infra": "platform",
}

COMMENTS_SUGGESTION = [
    "Nit: can we reuse `{helper}` here instead of duplicating the lookup?",
    "Suggestion: this branch is hard to follow - an early return would read better.",
    "Could we add a regression test for the empty payload case?",
    "Minor: rename `tmp` to something that says what it holds.",
    "Suggestion: pull the retry budget into a constant so ops can tune it.",
]
COMMENTS_BLOCKING = [
    "Blocking: this drops the retry budget for 5xx responses - please add a test before we merge.",
    "Blocking: the new column has no rollback path; can we ship it in two steps?",
    "Blocking: this changes the public payload shape for existing clients.",
    "Blocking: the timeout is hard coded at 2s, that will break the batch job.",
]
COMMENTS_QUESTION = [
    "What happens when {subject} is already assigned?",
    "Is this safe if two workers process the same event?",
    "Does this path still work for guest checkout?",
    "How does this behave when the geocoder times out?",
]
COMMENTS_PRISE = [
    "Nice, the error path is much clearer now.",
    "LGTM after the last commit - thanks for the quick turnaround.",
    "This is much easier to test now.",
]
AUTHOR_REPLIES = [
    "Good catch - pushed a fix in the latest commit.",
    "Added a test for that in the follow-up commit.",
    "Agreed, refactored it; the diff is smaller now.",
    "That case is handled by the caller, added a comment explaining why.",
]
DECLINE_REASONS = [
    "Closing: superseded by {other}, the new pricing model makes this obsolete.",
    "Closing: we are not doing per-zone surcharges this quarter; see {other}.",
    "Closing: conflicts with the courier assignment rewrite; re-open after {other}.",
]
NEEDS_WORK = [
    "Requesting changes: the migration needs a rollback path before we ship.",
    "Requesting changes: missing coverage for the failure branch.",
    "Requesting changes: this bypasses the feature flag, let's gate it.",
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[corpus] {msg}", flush=True)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def is_workday(d: date) -> bool:
    return d.weekday() < 5


def _clamp_forward(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    while not is_workday(dt.date()):
        dt = datetime.combine(dt.date() + timedelta(days=1), dtime(WORK_START), tzinfo=UTC)
    if dt.hour < WORK_START:
        dt = dt.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
    elif dt.hour >= WORK_END:
        dt = datetime.combine(dt.date() + timedelta(days=1), dtime(WORK_START), tzinfo=UTC)
        dt = _clamp_forward(dt)
    return dt


def advance(dt: datetime, hours: float) -> datetime:
    """Add working hours (Mon-Fri 09:00-18:00 UTC), skipping nights/weekends."""
    dt = _clamp_forward(dt)
    remaining = hours
    while remaining > 0:
        end_of_day = dt.replace(hour=WORK_END, minute=0, second=0, microsecond=0)
        avail = (end_of_day - dt).total_seconds() / 3600.0
        if remaining <= avail:
            return dt + timedelta(hours=remaining)
        remaining -= avail
        dt = _clamp_forward(end_of_day + timedelta(minutes=1))
    return dt


def rewind(dt: datetime, hours: float) -> datetime:
    """Subtract working hours, walking back over nights/weekends."""
    dt = _clamp_forward(dt)
    remaining = hours
    while remaining > 0:
        start_of_day = dt.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
        avail = (dt - start_of_day).total_seconds() / 3600.0
        if remaining <= avail:
            return dt - timedelta(hours=remaining)
        remaining -= avail
        prev = dt.date() - timedelta(days=1)
        while not is_workday(prev):
            prev -= timedelta(days=1)
        dt = datetime.combine(prev, dtime(WORK_END), tzinfo=UTC)
    return dt


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


def sh(cmd: list[str], cwd: Path | None = None, env: dict | None = None,
       check: bool = True) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=e,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({p.returncode}):\n{p.stderr or p.stdout}")
    return p


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, user=SYSADMIN, data=None,
             ok=(200, 201, 204), retries: int = 3):
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

    def pages(self, path: str, user=SYSADMIN, params: dict | None = None):
        start = 0
        while True:
            q = dict(params or {})
            q.update({"start": start, "limit": 100})
            page = self.call("GET", f"{path}?{urllib.parse.urlencode(q)}", user)
            yield from page.get("values", [])
            if page.get("isLastPage", True):
                return
            start = page.get("nextPageStart", start + 100)


# ---------------------------------------------------------------------------
# plan generation (deterministic)
# ---------------------------------------------------------------------------
def build_tickets() -> list[dict]:
    rng = random.Random(SEED)
    days = business_days(WINDOW_START, WINDOW_END)
    tickets = []
    for i in range(100):
        key = f"DEL-{100 + i}"
        area, title, kind, detail = THEMES[i % len(THEMES)]
        repo = "delivery-web" if kind == "web" else "delivery-api"
        day = days[min(len(days) - 1, (i * len(days)) // 100)]
        open_at = at(day, 9.0 + rng.uniform(0, 6.5))
        tickets.append({
            "key": key, "index": i, "area": area, "slug": SLUGS[area],
            "title": title, "detail": detail, "repo": repo,
            "author": AUTHORS[(i * 7 + 2) % len(AUTHORS)][0],
            "open_at": iso(open_at), "prs": [],
        })
    return tickets


def _reviewers(author: str, rng: random.Random) -> tuple[str, str]:
    sentinel = SENTINEL if author != SENTINEL else AUTHORS[0][0]
    pool = [u[0] for u in AUTHORS if u[0] not in (author, sentinel)]
    normal = pool[rng.randrange(len(pool))]
    return sentinel, normal


def build_prs(tickets: list[dict]) -> list[dict]:
    rng = random.Random(SEED + 1)
    prs: list[dict] = []
    for t in tickets:
        follow = rng.random() < 0.30
        stacked = follow and rng.random() < 0.30
        n = 2 if follow else 1
        for j in range(n):
            if j == 0:
                outcome = ("merged" if rng.random() < 0.86 else
                           ("declined" if rng.random() < 0.55 else "open"))
            else:
                outcome = ("merged" if rng.random() < 0.72 else
                           ("open" if rng.random() < 0.70 else "declined"))
            prs.append({
                "id": f"{t['key']}#{j + 1}",
                "ticket": t["key"],
                "repo": t["repo"],
                "author": t["author"] if j == 0
                          else AUTHORS[(t["index"] + 3) % len(AUTHORS)][0],
                "branch": f"{t['key']}-{t['title'].lower().replace(' ', '-')}"
                          + ("-followup" if j else ""),
                "outcome": outcome,
                "stacked_on": None,
                "ticket_title": t["title"],
                "area": t["area"],
                "slug": t["slug"],
                "detail": t["detail"],
                "number": j + 1,
                # pr_size: a few deliberately oversized PRs (first PR only).
                "large_commits": j == 0 and t["index"] in LARGE_COMMIT_INDICES,
                "large_diff": j == 0 and t["index"] in LARGE_DIFF_INDICES,
            })
        if follow:
            if stacked:
                prs[-1]["stacked_on"] = prs[-2]["id"]
                prs[-1]["branch"] = prs[-2]["branch"] + "-stack"
            t["prs"] = [prs[-2]["id"], prs[-1]["id"]]
        else:
            t["prs"] = [prs[-1]["id"]]
    return prs


def primary_path(repo: str, slug: str, ticket: str, rev: int) -> str:
    """The first file ``file_bundle`` creates for a revision (the anchor path)."""
    if repo == "delivery-api":
        tag = ticket.lower().replace("-", "_")
        return f"src/delivery_api/{slug}/{tag}_rev{rev}.py"
    stem = f"{ticket.replace('-', '_').lower()}-rev{rev}"
    return f"src/features/{slug}/{stem}.js"


def _thread_disposition(rng: random.Random) -> str:
    """What becomes of the first review thread (prd/15 Q4 / prd/16)."""
    roll = rng.random()
    if roll < 0.22:
        return "walked_back"   # the reviewer resolves their own remark
    if roll < 0.44:
        return "rejected"      # the author closes it without a change
    if roll < 0.74:
        return "acted_on"      # the author changes the anchored code
    return "unresolved"        # still open when the corpus snapshot ends


def plan_pr(pr: dict, open_at: datetime, actions_from: datetime,
            rng: random.Random, needs_work: bool = False) -> dict:
    author = pr["author"]
    sentinel, normal = _reviewers(author, rng)
    outcome = pr["outcome"]
    rounds = 1 if outcome != "merged" else rng.choices([1, 2, 3], [0.50, 0.35, 0.15])[0]
    if outcome == "open":
        rounds = 0 if needs_work else rng.choices([1, 2], [0.75, 0.25])[0]

    if pr.get("large_commits"):
        n_commits = rng.randint(12, 16)   # > pr_size's 10-commit cap
    elif pr.get("large_diff"):
        n_commits = 1                     # one commit carries a >400-line file
    else:
        n_commits = rng.randint(1, 3)
    commits, t = [], open_at
    for c in range(n_commits):
        t = rewind(t, rng.uniform(1.0, 7.0) if c else rng.uniform(0.2, 1.5))
        subject = f"{pr['ticket']}: {pr['ticket_title'].lower()}"
        if c:
            subject += f" (part {c + 1})"
        commits.append({"at": iso(t), "subject": subject, "n": c})
    commits.reverse()  # oldest first

    actions: list[dict] = []
    cur = actions_from
    approved: set[str] = set()

    def add(kind, when, actor, **kw):
        actions.append({"kind": kind, "at": iso(when), "actor": actor, **kw})
        if kind == "approve":
            approved.add(actor)
        elif kind == "unapprove":
            approved.discard(actor)

    # -- review thread ----------------------------------------------------
    anchor_path = primary_path(pr["repo"], pr["slug"], pr["ticket"],
                               (pr["number"] - 1) * 10 + 1)
    disposition = None
    if rng.random() < 0.90:
        cur = advance(cur, rng.uniform(1.0, 6.0))
        blocking = rng.random() < 0.35
        pool = COMMENTS_BLOCKING if blocking else rng.choice(
            [COMMENTS_SUGGESTION, COMMENTS_QUESTION, COMMENTS_PRISE])
        text = pool[rng.randrange(len(pool))].format(
            helper="resolve_zone()", subject="the courier")
        disposition = _thread_disposition(rng)
        if rounds == 0:
            # no push answers this thread before the snapshot: keep it open
            disposition = "unresolved"
        add("comment", cur, normal, text=text,
            severity="BLOCKER" if blocking else "NORMAL",
            anchor=anchor_path, thread=True, disposition=disposition)

    # -- rebase instead of a content push (real force-pushes) -------------
    # The classification is deterministic: same patch id -> pure_rebase, a
    # small conflict resolution -> rebase_conflict.  A rebase that also changes
    # the anchored code would be a content push, so acted_on threads keep the
    # content mode (the anchor must actually move).
    rebase_mode = None
    if (rounds >= 1 and not pr.get("stacked_on") and disposition != "acted_on"):
        bucket = int(hashlib.sha1(pr["id"].encode()).hexdigest()[:8], 16) % 100
        if bucket < 26:
            rebase_mode = "pure_rebase"
        elif bucket < 36:
            rebase_mode = "rebase_conflict"

    # -- optional reviewer swap (exercises removedReviewers) --------------
    active_normal = normal
    replacement = None
    swap = (outcome != "declined" and rounds >= 1 and not pr.get("stacked_on")
            and rng.random() < 0.12)
    if swap:
        pool = [u[0] for u in AUTHORS if u[0] not in (author, sentinel, normal)]
        replacement = pool[rng.randrange(len(pool))]

    for r in range(rounds):
        cur = advance(cur, rng.uniform(1.0, 8.0))
        mode = rebase_mode if r == 0 and rebase_mode else "content"
        # acted_on: the push must touch the file the reviewer anchored on.
        touch = bool(disposition == "acted_on" and r == 0 and mode == "content")
        add("push", cur, author, commit=r, mode=mode, touch=touch)
        if active_normal in approved and rng.random() < 0.45:
            # "reset approvals on push" is on for some projects: the reviewer's
            # approval is dismissed and has to be given again.
            add("unapprove", cur, active_normal)
        cur = advance(cur, rng.uniform(0.3, 1.5))
        if mode == "pure_rebase":
            reply = "Rebased onto main to pick up the latest changes."
        elif mode == "rebase_conflict":
            reply = "Rebased onto main and resolved the conflict in the shared module."
        else:
            reply = AUTHOR_REPLIES[rng.randrange(len(AUTHOR_REPLIES))]
        add("comment", cur, author, text=reply, severity="NORMAL")

        if r == 0 and disposition in ("walked_back", "rejected", "acted_on"):
            cur = advance(cur, rng.uniform(0.3, 1.5))
            resolver = normal if disposition != "rejected" else author
            add("resolve_thread", cur, resolver, thread=0)

        if r == 0 and swap:
            cur = advance(cur, rng.uniform(0.2, 0.8))
            add("remove_reviewer", cur, sentinel, reviewer=active_normal)
            approved.discard(active_normal)
            cur = advance(cur, rng.uniform(0.2, 0.8))
            add("add_reviewer", cur, author, reviewer=replacement)
            active_normal = replacement
            cur = advance(cur, rng.uniform(0.5, 2.0))
            add("comment", cur, active_normal,
                text="Taking over this review from the previous reviewer.",
                severity="NORMAL")

        if rng.random() < 0.45:
            cur = advance(cur, rng.uniform(0.5, 3.0))
            add("approve", cur, active_normal)
    if rounds == 0:
        cur = advance(cur, rng.uniform(1.0, 5.0))
        add("needs_work", cur, normal, text=NEEDS_WORK[rng.randrange(len(NEEDS_WORK))])
        add("needs_work_status", cur, normal)

    if outcome == "merged":
        if active_normal not in approved:
            cur = advance(cur, rng.uniform(1.5, 10.0))
            add("approve", cur, active_normal)
        cur = advance(cur, rng.uniform(1.0, 12.0))
        add("approve", cur, sentinel)          # mandatory sentinel approval last
        cur = advance(cur, rng.uniform(0.5, 8.0))
        add("merge", cur, author)
    elif outcome == "declined":
        cur = advance(cur, rng.uniform(2.0, 16.0))
        other = f"DEL-{100 + rng.randrange(0, 100):03d}"
        add("decline", cur, author,
            text=DECLINE_REASONS[rng.randrange(len(DECLINE_REASONS))].format(other=other))
    elif rounds > 0:  # open, under review
        if rng.random() < 0.5:
            cur = advance(cur, rng.uniform(1.0, 8.0))
            add("approve", cur, active_normal)
            if rng.random() < 0.6:
                cur = advance(cur, rng.uniform(0.5, 6.0))
                add("approve", cur, sentinel)

    # nothing may happen after the window: late PRs simply stay open
    late = [a for a in actions if parse_iso(a["at"]) > CUTOFF]
    if late:
        actions = [a for a in actions if parse_iso(a["at"]) <= CUTOFF]
        if any(a["kind"] in ("merge", "decline") for a in late):
            actions = [a for a in actions if a["kind"] not in ("merge", "decline")]
            pr["outcome"] = outcome = "open"

    return {**pr, "open_at": iso(open_at), "commits": commits,
            "actions": actions, "reviewers": [sentinel, normal],
            "sentinel": sentinel, "anchor_path": anchor_path,
            "thread_disposition": disposition, "replacement": replacement,
            "rebase_mode": rebase_mode}


# ---------------------------------------------------------------------------
# git workspace
# ---------------------------------------------------------------------------
class RepoGit:
    def __init__(self, slug: str):
        self.slug = slug
        self.path = GITDIR / slug
        self.url = f"{BASE}/scm/{PROJECT}/{slug}.git"

    def ensure(self) -> None:
        if not (self.path / ".git").exists():
            GITDIR.mkdir(parents=True, exist_ok=True)
            sh(["git", "clone", "-q", f"http://{SYSADMIN[0]}:{SYSADMIN[1]}@{HOSTPORT}"
                f"/scm/{PROJECT}/{self.slug}.git", str(self.path)])
        sh(["git", "fetch", "-q", "origin", "--prune"], cwd=self.path, check=False)

    def _ident_env(self, user: str, when: str) -> dict:
        display, email = USER_BY_NAME[user][1], USER_BY_NAME[user][2]
        return {"GIT_AUTHOR_NAME": display, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": display, "GIT_COMMITTER_EMAIL": email,
                "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}

    def make_branch(self, branch: str, base_ref: str, commits: list[dict],
                    author: str, files: list[tuple[str, str]],
                    extra_first: list[tuple[str, str]] | None = None) -> None:
        sh(["git", "fetch", "-q", "origin", "--prune"], cwd=self.path)
        sh(["git", "checkout", "-q", "-B", branch, f"origin/{base_ref}"], cwd=self.path)
        sh(["git", "reset", "-q", "--hard", f"origin/{base_ref}"], cwd=self.path)
        for i, c in enumerate(commits):
            batch = files[i * 2:(i + 1) * 2] or files
            if i == 0 and extra_first:
                batch = list(batch) + list(extra_first)
            for path, content in batch:
                p = self.path / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
                sh(["git", "add", path], cwd=self.path)
            sh(["git", "commit", "-q", "--allow-empty", "-m",
                c["subject"] + "\n\nEpic: DEL-1"], cwd=self.path,
               env=self._ident_env(author, c["at"]))

    def add_commit(self, branch: str, subject: str, when: str, author: str,
                   files: list[tuple[str, str]],
                   touch: str | None = None, touch_suffix: str = "") -> None:
        sh(["git", "checkout", "-q", branch], cwd=self.path)
        if touch:
            path = self.path / touch
            existing = path.read_text() if path.exists() else ""
            files = list(files) + [(touch, existing + touch_suffix)]
        for path, content in files:
            p = self.path / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            sh(["git", "add", path], cwd=self.path)
        sh(["git", "commit", "-q", "-m", subject + "\n\nEpic: DEL-1"], cwd=self.path,
           env=self._ident_env(author, when))

    def commit_to_main(self, subject: str, when: str, author: str,
                       files: list[tuple[str, str]]) -> None:
        """Move ``main`` forward (the base a rebase lands on)."""
        sh(["git", "fetch", "-q", "origin", "--prune"], cwd=self.path)
        sh(["git", "checkout", "-q", "main"], cwd=self.path)
        sh(["git", "reset", "-q", "--hard", "origin/main"], cwd=self.path)
        for path, content in files:
            p = self.path / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            sh(["git", "add", path], cwd=self.path)
        sh(["git", "commit", "-q", "--allow-empty", "-m",
            subject + "\n\nEpic: DEL-1"], cwd=self.path,
           env=self._ident_env(author, when))
        self.push("main", author)

    def rebase_branch(self, branch: str, author: str, when: str,
                      resolved: tuple[str, str] | None = None) -> None:
        """Rebase ``branch`` onto ``origin/main`` (optionally resolving a conflict).

        A clean rebase keeps the patch id (``pure_rebase``); writing the
        resolved content changes only a handful of lines (``rebase_conflict``).
        """
        display, email = USER_BY_NAME[author][1], USER_BY_NAME[author][2]
        committer_env = {"GIT_COMMITTER_NAME": display, "GIT_COMMITTER_EMAIL": email,
                         "GIT_COMMITTER_DATE": when, "GIT_EDITOR": "true"}
        sh(["git", "fetch", "-q", "origin", "--prune"], cwd=self.path)
        sh(["git", "checkout", "-q", branch], cwd=self.path)
        p = sh(["git", "rebase", "origin/main"], cwd=self.path,
               env=committer_env, check=False)
        if p.returncode != 0:
            if resolved is None:
                sh(["git", "rebase", "--abort"], cwd=self.path, check=False)
                raise RuntimeError(
                    f"unexpected rebase conflict on {branch}:\n{p.stderr or p.stdout}")
            path, content = resolved
            target = self.path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            sh(["git", "add", path], cwd=self.path)
            sh(["git", "rebase", "--continue"], cwd=self.path, env=committer_env)

    def merge_to_main(self, branch: str, pr_id: int, actor: str, when: str) -> str:
        """Merge the PR branch locally with a backdated merge commit, push main.

        Bitbucket detects that the branch is contained in main and flips the PR
        to MERGED (recording a MERGED activity) without creating a server-side
        merge commit, so the whole history keeps the planned timestamps.  The
        returned commit is written into ``STA_PR_MERGE_ACTIVITY`` by
        ``backdate.py`` so the MERGED activity carries a real merge commit.
        """
        sh(["git", "fetch", "-q", "origin", "--prune"], cwd=self.path)
        sh(["git", "checkout", "-q", "main"], cwd=self.path)
        sh(["git", "reset", "-q", "--hard", "origin/main"], cwd=self.path)
        msg = (f"Merge pull request #{pr_id} in {PROJECT}/{self.slug} "
               f"from {branch} to main")
        sh(["git", "merge", "-q", "--no-ff", f"origin/{branch}", "-m", msg],
           cwd=self.path, env=self._ident_env(actor, when))
        merge_commit = sh(["git", "rev-parse", "HEAD"], cwd=self.path).stdout.strip()
        self.push("main", actor)
        return merge_commit

    def push(self, branch: str, user: str) -> None:
        pw = USER_BY_NAME[user][3]
        sh(["git", "push", "-q", "-f",
            f"http://{user}:{pw}@{HOSTPORT}/scm/{PROJECT}/{self.slug}.git",
            f"refs/heads/{branch}:refs/heads/{branch}"], cwd=self.path)


def file_bundle(repo: str, area: str, slug: str, ticket: str, rev: int,
                detail: str) -> list[tuple[str, str]]:
    """Two plausible source files for one revision (unique per PR + revision)."""
    tag = ticket.lower().replace("-", "_")
    if repo == "delivery-api":
        mod = f"src/delivery_api/{slug}/{tag}_rev{rev}.py"
        test = f"tests/{slug}/test_{tag}_rev{rev}.py"
        src = (
            '"""' + detail + "\n\n" + ticket + " (revision " + str(rev) + ").\n\"\"\"\n\n"
            "from __future__ import annotations\n\n"
            "DEFAULT_RETRY_BUDGET = 3\n\n\n"
            "def apply(payload: dict, retry_budget: int = DEFAULT_RETRY_BUDGET) -> dict:\n"
            f'    """{detail}"""\n'
            "    if not payload:\n"
            '        raise ValueError("empty payload")\n'
            "    result = dict(payload)\n"
            '    result.setdefault("retry_budget", retry_budget)\n'
            '    result["handled_by"] = __name__\n'
            "    return result\n"
        )
        tst = (
            f"from delivery_api.{slug}.{tag}_rev{rev} import apply\n\n\n"
            "def test_apply_copies_payload():\n"
            '    assert apply({"a": 1})["a"] == 1\n\n\n'
            "def test_apply_rejects_empty():\n"
            "    try:\n"
            "        apply({})\n"
            "    except ValueError:\n"
            "        pass\n"
            "    else:\n"
            '        raise AssertionError("expected ValueError")\n'
        )
        return [(mod, src), (test, tst)]
    stem = f"{ticket.replace('-', '_').lower()}-rev{rev}"
    mod = f"src/features/{slug}/{stem}.js"
    test = f"src/features/{slug}/{stem}.test.js"
    src = (
        f"// {ticket} (revision {rev}) - {detail}\n"
        "export const RETRY_BUDGET = 3;\n\n"
        "export function apply(payload, retryBudget = RETRY_BUDGET) {\n"
        "  if (!payload) throw new Error('empty payload');\n"
        "  return { ...payload, retryBudget, handledBy: import.meta.url };\n"
        "}\n"
    )
    tst = (
        f"import {{ apply }} from './{stem}.js';\n\n"
        "describe('apply', () => {\n"
        "  it('copies the payload', () => {\n"
        "    expect(apply({ a: 1 }).a).toBe(1);\n"
        "  });\n\n"
        "  it('rejects an empty payload', () => {\n"
        "    expect(() => apply(null)).toThrow();\n"
        "  });\n"
        "});\n"
    )
    return [(mod, src), (test, tst)]


def shared_bundle(repo: str, slug: str, ticket: str,
                  variant: str = "") -> tuple[str, str, str, str]:
    """A file both the branch and a base move touch (the rebase conflict).

    ``(path, branch_content, main_content, resolved_content)``: the branch adds
    the first, main adds the second, and the rebase resolution keeps both sides.
    The difference between branch and resolved is a handful of lines, which is
    exactly what ``git range-diff`` reads as ``rebase_conflict``.
    """
    tag = ticket.lower().replace("-", "_") + variant
    if repo == "delivery-api":
        path = f"src/delivery_api/{slug}/{tag}_shared.py"
        branch = (
            f'"""Shared configuration for {ticket} (branch)."""\n\n'
            'SHARED = "branch"\n'
            "BRANCH_ONLY = True\n"
        )
        main = (
            f'"""Shared configuration for {ticket} (main)."""\n\n'
            'SHARED = "main"\n'
            "MAIN_ONLY = True\n"
            "EXTRA = 3\n"
        )
        resolved = (
            f'"""Shared configuration for {ticket} (merged)."""\n\n'
            'SHARED = "main+branch"\n'
            "MAIN_ONLY = True\n"
            "EXTRA = 3\n"
            "BRANCH_ONLY = True\n"
        )
        return path, branch, main, resolved
    path = f"src/features/{slug}/{tag}-shared.js"
    branch = (
        f"// Shared configuration for {ticket} (branch).\n"
        "export const SHARED = 'branch';\n"
        "export const BRANCH_ONLY = true;\n"
    )
    main = (
        f"// Shared configuration for {ticket} (main).\n"
        "export const SHARED = 'main';\n"
        "export const MAIN_ONLY = true;\n"
        "export const EXTRA = 3;\n"
    )
    resolved = (
        f"// Shared configuration for {ticket} (merged).\n"
        "export const SHARED = 'main+branch';\n"
        "export const MAIN_ONLY = true;\n"
        "export const EXTRA = 3;\n"
        "export const BRANCH_ONLY = true;\n"
    )
    return path, branch, main, resolved


def big_file_bundle(repo: str, slug: str, ticket: str,
                    variant: str = "") -> tuple[str, str]:
    """One ~460-line file so a single-commit PR still exceeds the 400-line cap."""
    tag = ticket.lower().replace("-", "_") + variant
    if repo == "delivery-api":
        path = f"src/delivery_api/{slug}/{tag}_report.py"
        head = [f'"""Generated report fields for {ticket}."""', ""]
        body = [f"REPORT_FIELD_{i} = {i}" for i in range(455)]
        tail = ["", "", "def render(payload: dict) -> dict:", "    return dict(payload)"]
        return path, "\n".join(head + body + tail) + "\n"
    path = f"src/features/{slug}/{tag}-report.js"
    head = [f"// Generated report fields for {ticket}.", ""]
    body = [f"export const REPORT_FIELD_{i} = {i};" for i in range(455)]
    tail = ["", "export function render(payload) {", "  return { ...payload };", "}"]
    return path, "\n".join(head + body + tail) + "\n"


# ---------------------------------------------------------------------------
# server bootstrap
# ---------------------------------------------------------------------------
def ensure_users(api: Api) -> None:
    for name, display, email, pw in USERS:
        try:
            u = api.call("GET", f"/rest/api/1.0/users/{name}")
            if u.get("name") == name:
                continue
        except RuntimeError:
            pass
        q = urllib.parse.urlencode({
            "name": name, "password": pw, "displayName": display,
            "emailAddress": email, "addToDefaultGroup": "true", "notify": "false"})
        api.call("POST", f"/rest/api/1.0/admin/users?{q}")
        log(f"user {name} created")


def ensure_project(api: Api) -> None:
    try:
        p = api.call("GET", f"/rest/api/1.0/projects/{PROJECT}")
        if p.get("key") == PROJECT:
            pass
    except RuntimeError:
        api.call("POST", "/rest/api/1.0/projects", data={
            "key": PROJECT, "name": "Delivery Platform",
            "description": "Delivery platform: API, web app and operations tooling",
            "public": False})
        log(f"project {PROJECT} created")
    for name, _, _, _ in USERS:
        api.call("PUT", f"/rest/api/1.0/projects/{PROJECT}/permissions/users?"
                 + urllib.parse.urlencode({"name": name, "permission": "PROJECT_WRITE"}),
                 ok=(200, 204))


def ensure_repos(api: Api) -> None:
    for slug, meta in REPOS.items():
        try:
            r = api.call("GET", f"/rest/api/1.0/projects/{PROJECT}/repos/{slug}")
            if r.get("slug") == slug:
                continue
        except RuntimeError:
            pass
        api.call("POST", f"/rest/api/1.0/projects/{PROJECT}/repos", data={
            "name": meta["name"], "scmId": "git", "forkable": True})
        log(f"repo {slug} created")


def seed_repo(api: Api, slug: str) -> None:
    """Make sure the repo has a main branch and is the default branch."""
    g = RepoGit(slug)
    GITDIR.mkdir(parents=True, exist_ok=True)
    if not (g.path / ".git").exists():
        sh(["git", "clone", "-q", f"http://{SYSADMIN[0]}:{SYSADMIN[1]}@{HOSTPORT}"
            f"/scm/{PROJECT}/{slug}.git", str(g.path)], check=False)
    has_main = (g.path / ".git").exists() and sh(
        ["git", "rev-parse", "--verify", "-q", "origin/main"], cwd=g.path,
        check=False).returncode == 0
    if not has_main:
        g.path.mkdir(parents=True, exist_ok=True)
        if not (g.path / ".git").exists():
            sh(["git", "init", "-q", "-b", "main"], cwd=g.path)
        else:  # cloned an empty repository: HEAD may point at an unborn branch
            sh(["git", "checkout", "-q", "-B", "main"], cwd=g.path)
        have_head = sh(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=g.path,
                       check=False).returncode == 0
        if have_head:
            log(f"repo {slug}: local main already has history")
        else:
            (g.path / "README.md").write_text(f"# {slug}\n\n{REPOS[slug]['description']}\n")
            (g.path / "docs").mkdir(exist_ok=True)
            (g.path / "docs" / "conventions.md").write_text(
                "# Conventions\n\n- Ticket keys in branch names and commit subjects.\n"
                "- One epic: DEL-1.\n")
            sh(["git", "add", "-A"], cwd=g.path)
            env = {"GIT_AUTHOR_NAME": "Lab Admin", "GIT_AUTHOR_EMAIL": "admin@lab.example.com",
                   "GIT_COMMITTER_NAME": "Lab Admin", "GIT_COMMITTER_EMAIL": "admin@lab.example.com",
                   "GIT_AUTHOR_DATE": "2026-08-10T09:00:00+00:00",
                   "GIT_COMMITTER_DATE": "2026-08-10T09:00:00+00:00"}
            sh(["git", "commit", "-q", "-m", "chore: repository scaffold"], cwd=g.path, env=env)
        sh(["git", "push", "-q", f"http://{SYSADMIN[0]}:{SYSADMIN[1]}@{HOSTPORT}"
            f"/scm/{PROJECT}/{slug}.git", "refs/heads/main:refs/heads/main"], cwd=g.path)
        log(f"repo {slug} seeded (main)")
    api.call("PUT", f"/rest/api/1.0/projects/{PROJECT}/repos/{slug}/default-branch",
             data={"id": "refs/heads/main"}, ok=(200, 204))
    g.ensure()


# ---------------------------------------------------------------------------
# PR execution
# ---------------------------------------------------------------------------
def find_pr(api: Api, repo: str, branch: str) -> dict | None:
    path = f"/rest/api/1.0/projects/{PROJECT}/repos/{repo}/pull-requests"
    for pr in api.pages(path, params={"state": "ALL"}):
        if (pr.get("fromRef") or {}).get("displayId") == branch:
            return pr
    return None


def pr_description(pr: dict) -> str:
    return (
        "Epic: DEL-1\n\n"
        f"Ticket: {pr['ticket']}\n\n"
        f"{pr['detail']}\n\n"
        f"Implements: {pr['ticket']}\n"
        "Testing: unit tests added next to the changed module.\n"
        "Risk: low, feature flagged.\n"
    )


def ensure_pr(api: Api, pr: dict) -> dict:
    existing = find_pr(api, pr["repo"], pr["branch"])
    if existing:
        return existing
    data = {
        "title": f"{pr['ticket']}: {pr['ticket_title']}",
        "description": pr_description(pr),
        "state": "OPEN",
        "fromRef": {"id": f"refs/heads/{pr['branch']}",
                    "repository": {"slug": pr["repo"], "project": {"key": PROJECT}}},
        "toRef": {"id": f"refs/heads/{pr.get('base_branch', 'main')}",
                  "repository": {"slug": pr["repo"], "project": {"key": PROJECT}}},
    }
    return api.call("POST", f"/rest/api/1.0/projects/{PROJECT}/repos/{pr['repo']}/pull-requests",
                    user=(pr["author"], USER_BY_NAME[pr["author"]][3]),
                    data=data, ok=(200, 201))


def add_reviewers(api: Api, pr: dict, pr_id: int) -> None:
    base = f"/rest/api/1.0/projects/{PROJECT}/repos/{pr['repo']}/pull-requests/{pr_id}"
    have = {p["user"]["name"] for p in api.call("GET", base + "/participants")["values"]}
    auth = (pr["author"], USER_BY_NAME[pr["author"]][3])
    for name in dict.fromkeys(pr["reviewers"]):
        if name in have:
            continue
        api.call("POST", base + "/participants", user=auth,
                 data={"user": {"name": name}, "role": "REVIEWER"}, ok=(200, 201))
        log(f"  {pr['id']} reviewer {name}")


def execute_pr(api: Api, git: RepoGit, pr: dict, pr_id: int | None,
               timeline: list[str]) -> dict:
    base = f"/rest/api/1.0/projects/{PROJECT}/repos/{pr['repo']}/pull-requests"
    rev_base = (pr["number"] - 1) * 10
    variant = f"_p{pr['number']}"
    shared = shared_bundle(pr["repo"], pr["slug"], pr["ticket"], variant) \
        if pr.get("rebase_mode") == "rebase_conflict" else None
    if pr_id is None:
        files: list[tuple[str, str]] = []
        for c in pr["commits"]:
            rev = rev_base + c["n"] + 1
            files.extend(file_bundle(pr["repo"], pr["area"], pr["slug"], pr["ticket"],
                                     rev, pr["detail"]))
        extra_first: list[tuple[str, str]] = []
        if shared:
            # the branch adds the same path the base move will add (conflict)
            extra_first.append((shared[0], shared[1]))
        if pr.get("large_diff"):
            extra_first.append(big_file_bundle(pr["repo"], pr["slug"], pr["ticket"],
                                               variant))
        base_ref = pr.get("base_branch", "main")
        git.make_branch(pr["branch"], base_ref, pr["commits"], pr["author"], files,
                        extra_first=extra_first)
        git.push(pr["branch"], pr["author"])
        created = ensure_pr(api, pr)
        pr_id = created["id"]
        log(f"  {pr['id']} PR #{pr_id} opened ({pr['branch']} -> {base_ref})")
        timeline.insert(0, iso(parse_iso(pr["open_at"])))

    # stacked PRs target the parent branch until the parent is merged; retarget
    # before the child's review starts so approvals/roles are recorded on main.
    if pr.get("base_branch", "main") != "main":
        cur = api.call("GET", f"{base}/{pr_id}")
        if (cur.get("toRef") or {}).get("displayId") != "main":
            api.call("PUT", f"{base}/{pr_id}", user=SYSADMIN, data={
                "version": cur["version"],
                "toRef": {"id": "refs/heads/main",
                          "repository": {"slug": pr["repo"],
                                         "project": {"key": PROJECT}}}})
            log(f"  {pr['id']} retargeted to main")

    add_reviewers(api, pr, pr_id)

    threads: list[int] = []
    resolved: list[dict] = []
    merge_commit: str | None = None
    touch_suffix = ("\n# review feedback applied\n" if pr["repo"] == "delivery-api"
                    else "\n// review feedback applied\n")

    for action in pr["actions"]:
        kind = action["kind"]
        actor = action["actor"]
        auth = (actor, USER_BY_NAME[actor][3])
        cur = api.call("GET", f"{base}/{pr_id}")
        if kind == "comment":
            data = {"text": action["text"],
                    "severity": action.get("severity", "NORMAL"),
                    "version": cur["version"]}
            if action.get("anchor"):
                data["anchor"] = {"path": action["anchor"], "line": 3,
                                  "lineType": "ADDED", "fileType": "TO"}
            created = api.call("POST", f"{base}/{pr_id}/comments", user=auth,
                               ok=(200, 201), data=data)
            if action.get("thread"):
                threads.append(created["id"])
        elif kind == "push":
            mode = action.get("mode", "content")
            when = action["at"]
            if mode == "pure_rebase":
                marker = (f"docs/sync/{pr['ticket'].lower()}-p{pr['number']}-"
                          f"{action.get('commit', 0)}.md")
                git.commit_to_main(
                    f"chore: sync main before {pr['ticket']} rebase",
                    when, SENTINEL,
                    [(marker, f"# main moved\n\n{pr['ticket']} rebased onto this tip.\n")])
                git.rebase_branch(pr["branch"], pr["author"], when)
                git.push(pr["branch"], pr["author"])
                log(f"  {pr['id']} pure rebase onto main")
            elif mode == "rebase_conflict":
                assert shared is not None
                path, _branch, main_content, resolved_content = shared
                git.commit_to_main(
                    f"chore: main-side {pr['ticket']} configuration",
                    when, SENTINEL, [(path, main_content)])
                git.rebase_branch(pr["branch"], pr["author"], when,
                                  resolved=(path, resolved_content))
                git.push(pr["branch"], pr["author"])
                log(f"  {pr['id']} rebase with conflict resolution")
            else:
                part = action.get("commit", 0) + 2
                rev = rev_base + len(pr["commits"]) + part - 1
                files = file_bundle(pr["repo"], pr["area"], pr["slug"], pr["ticket"],
                                    rev, pr["detail"])
                subject = f"{pr['ticket']}: address review feedback (rev {part - 1})"
                git.add_commit(
                    pr["branch"], subject, when, pr["author"], files,
                    touch=pr["anchor_path"] if action.get("touch") else None,
                    touch_suffix=touch_suffix)
                git.push(pr["branch"], pr["author"])
        elif kind == "resolve_thread":
            cid = threads[action["thread"]]
            comment = api.call("GET", f"{base}/{pr_id}/comments/{cid}", user=auth)
            api.call("PUT", f"{base}/{pr_id}/comments/{cid}", user=auth,
                     data={"version": comment["version"], "state": "RESOLVED"},
                     ok=(200, 204))
            resolved.append({"comment": cid, "at": action["at"]})
            log(f"  {pr['id']} thread {cid} resolved by {actor}")
        elif kind == "remove_reviewer":
            api.call("DELETE", f"{base}/{pr_id}/participants/{action['reviewer']}",
                     user=auth, ok=(200, 204))
            log(f"  {pr['id']} reviewer {action['reviewer']} removed")
        elif kind == "add_reviewer":
            api.call("POST", f"{base}/{pr_id}/participants", user=auth,
                     data={"user": {"name": action["reviewer"]}, "role": "REVIEWER"},
                     ok=(200, 201))
            log(f"  {pr['id']} reviewer {action['reviewer']} added")
        elif kind == "approve":
            api.call("POST", f"{base}/{pr_id}/approve", user=auth,
                     data={"lastReviewedCommit": ""}, ok=(200, 204))
        elif kind == "unapprove":
            api.call("DELETE", f"{base}/{pr_id}/approve", user=auth, ok=(200, 204))
        elif kind == "needs_work":
            api.call("POST", f"{base}/{pr_id}/comments", user=auth, ok=(200, 201), data={
                "text": action["text"], "severity": "BLOCKER", "version": cur["version"]})
        elif kind == "needs_work_status":
            api.call("PUT", f"{base}/{pr_id}/participants/{actor}", user=auth,
                     data={"status": "NEEDS_WORK"}, ok=(200, 204))
        elif kind == "merge":
            merge_commit = git.merge_to_main(pr["branch"], pr_id, actor, action["at"])
            for _ in range(40):
                cur = api.call("GET", f"{base}/{pr_id}")
                if cur["state"] == "MERGED":
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(f"PR {pr_id} did not auto-merge after pushing main")
            log(f"  {pr['id']} merged into main (backdated merge commit {merge_commit[:8]})")
        elif kind == "decline":
            api.call("POST", f"{base}/{pr_id}/comments", user=auth, ok=(200, 201), data={
                "text": action["text"], "version": cur["version"]})
            cur = api.call("GET", f"{base}/{pr_id}")
            api.call("POST", f"{base}/{pr_id}/decline?version={cur['version']}",
                     user=auth, ok=(200, 202))
            timeline.append(action["at"])
        else:
            raise RuntimeError(f"unknown action {kind}")
        timeline.append(action["at"])
        log(f"  {pr['id']} {kind} by {actor}")
    return {"pr_id": pr_id, "merge_commit": merge_commit,
            "thread_ids": threads, "resolved": resolved}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    global BASE, HOSTPORT
    ap = argparse.ArgumentParser(description="generate the DEL corpus")
    ap.add_argument("--limit", type=int, default=0, help="only the first N tickets")
    ap.add_argument("--reset", action="store_true", help="drop local plan/state")
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()

    BASE = args.base.rstrip("/")
    HOSTPORT = urllib.parse.urlparse(BASE).netloc
    BUILD.mkdir(parents=True, exist_ok=True)
    if args.reset:
        for f in (PLAN_FILE, STATE_FILE):
            f.unlink(missing_ok=True)
        shutil.rmtree(GITDIR, ignore_errors=True)

    api = Api(BASE)
    tickets = build_tickets()
    prs = build_prs(tickets)
    if args.limit:
        keep = {t["key"] for t in tickets[:args.limit]}
        prs = [p for p in prs if p["ticket"] in keep]
        tickets = [t for t in tickets if t["key"] in keep]

    rng = random.Random(SEED + 2)
    planned: list[dict] = []
    open_seen = 0
    for t in tickets:
        for pr in [p for p in prs if p["ticket"] == t["key"]]:
            if pr.get("stacked_on"):
                parent = next(p for p in planned if p["id"] == pr["stacked_on"])
                merges = [a["at"] for a in parent["actions"] if a["kind"] == "merge"]
                pr["base_branch"] = parent["branch"]
                open_at = advance(parse_iso(parent["open_at"]), rng.uniform(1.0, 4.0))
                actions_from = parse_iso(merges[-1]) if merges else open_at
            else:
                open_at = (parse_iso(t["open_at"]) if pr["number"] == 1
                           else advance(parse_iso(t["open_at"]), rng.uniform(24.0, 120.0)))
                actions_from = open_at
            needs_work = False
            if pr["outcome"] == "open":
                open_seen += 1
                needs_work = open_seen <= 4
            planned.append(plan_pr({**pr, "open_at": iso(open_at)}, open_at,
                                   actions_from, rng, needs_work))

    PLAN_FILE.write_text(json.dumps({"seed": SEED, "prs": planned}, indent=1))
    log(f"plan: {len(planned)} PRs over {len(tickets)} tickets -> {PLAN_FILE}")

    ensure_users(api)
    ensure_project(api)
    ensure_repos(api)
    for slug in REPOS:
        seed_repo(api, slug)

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    plan_out = json.loads(PLAN_FILE.read_text())

    for pr in planned:
        key = pr["id"]
        st = state.get(key, {})
        if st.get("done"):
            continue
        if pr.get("stacked_on") and not state.get(pr["stacked_on"], {}).get("pr_id"):
            log(f"{key}: parent not generated yet, skipping")
            continue
        log(f"{key} ({pr['repo']}, {pr['outcome']}, {pr['author']})")
        git = RepoGit(pr["repo"])
        git.ensure()
        timeline = st.get("timeline", [])
        result = execute_pr(api, git, pr, st.get("pr_id"), timeline)
        if pr["outcome"] == "merged":
            git.ensure()  # pick up the merge on main
        state[key] = {"done": True, "pr_id": result["pr_id"], "timeline": timeline,
                      "repo": pr["repo"], "outcome": pr["outcome"],
                      "merge_commit": result["merge_commit"],
                      "thread_ids": result["thread_ids"],
                      "resolved": result["resolved"]}
        STATE_FILE.write_text(json.dumps(state, indent=1))
        log(f"  {key} done (PR #{result['pr_id']})")

    for p in plan_out["prs"]:
        s = state.get(p["id"], {})
        p["pr_id"] = s.get("pr_id")
        p["timeline"] = s.get("timeline", [])
        p["merge_commit"] = s.get("merge_commit")
        p["thread_ids"] = s.get("thread_ids", [])
        p["resolved"] = s.get("resolved", [])
    PLAN_FILE.write_text(json.dumps(plan_out, indent=1))
    log(f"generated {sum(1 for v in state.values() if v.get('done'))}/{len(planned)} PRs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

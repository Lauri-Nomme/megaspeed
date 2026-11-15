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
]
USER_BY_NAME = {u[0]: u for u in USERS}

# alice is the team lead -> sentinel approver unless she authored the PR.
SENTINEL = "alice"

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
            "author": USERS[(i * 7 + 2) % len(USERS)][0],
            "open_at": iso(open_at), "prs": [],
        })
    return tickets


def _reviewers(author: str, rng: random.Random) -> tuple[str, str]:
    sentinel = SENTINEL if author != SENTINEL else "bob"
    pool = [u[0] for u in USERS if u[0] not in (author, sentinel)]
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
                "author": t["author"] if j == 0 else USERS[(t["index"] + 3) % 5][0],
                "branch": f"{t['key']}-{t['title'].lower().replace(' ', '-')}"
                          + ("-followup" if j else ""),
                "outcome": outcome,
                "stacked_on": None,
                "ticket_title": t["title"],
                "area": t["area"],
                "slug": t["slug"],
                "detail": t["detail"],
                "number": j + 1,
            })
        if follow:
            if stacked:
                prs[-1]["stacked_on"] = prs[-2]["id"]
                prs[-1]["branch"] = prs[-2]["branch"] + "-stack"
            t["prs"] = [prs[-2]["id"], prs[-1]["id"]]
        else:
            t["prs"] = [prs[-1]["id"]]
    return prs


def plan_pr(pr: dict, open_at: datetime, actions_from: datetime,
            rng: random.Random, needs_work: bool = False) -> dict:
    author = pr["author"]
    sentinel, normal = _reviewers(author, rng)
    outcome = pr["outcome"]
    rounds = 1 if outcome != "merged" else rng.choices([1, 2, 3], [0.50, 0.35, 0.15])[0]
    if outcome == "open":
        rounds = 0 if needs_work else rng.choices([1, 2], [0.75, 0.25])[0]

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

    if rng.random() < 0.90:
        cur = advance(cur, rng.uniform(1.0, 6.0))
        blocking = rng.random() < 0.35
        pool = COMMENTS_BLOCKING if blocking else rng.choice(
            [COMMENTS_SUGGESTION, COMMENTS_QUESTION, COMMENTS_PRISE])
        text = pool[rng.randrange(len(pool))].format(
            helper="resolve_zone()", subject="the courier")
        add("comment", cur, normal, text=text,
            severity="BLOCKER" if blocking else "NORMAL")

    for r in range(rounds):
        cur = advance(cur, rng.uniform(1.0, 8.0))
        add("push", cur, author, commit=r)
        if normal in approved and rng.random() < 0.45:
            # "reset approvals on push" is on for some projects: the reviewer's
            # approval is dismissed and has to be given again.
            add("unapprove", cur, normal)
        cur = advance(cur, rng.uniform(0.3, 1.5))
        add("comment", cur, author,
            text=AUTHOR_REPLIES[rng.randrange(len(AUTHOR_REPLIES))], severity="NORMAL")
        if rng.random() < 0.45:
            cur = advance(cur, rng.uniform(0.5, 3.0))
            add("approve", cur, normal)
    if rounds == 0:
        cur = advance(cur, rng.uniform(1.0, 5.0))
        add("needs_work", cur, normal, text=NEEDS_WORK[rng.randrange(len(NEEDS_WORK))])
        add("needs_work_status", cur, normal)

    if outcome == "merged":
        if normal not in approved:
            cur = advance(cur, rng.uniform(1.5, 10.0))
            add("approve", cur, normal)
        cur = advance(cur, rng.uniform(1.0, 12.0))
        add("approve", cur, sentinel)          # sentinel approval last
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
            add("approve", cur, normal)
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
            "sentinel": sentinel}


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
                    author: str, files: list[tuple[str, str]]) -> None:
        sh(["git", "fetch", "-q", "origin", "--prune"], cwd=self.path)
        sh(["git", "checkout", "-q", "-B", branch, f"origin/{base_ref}"], cwd=self.path)
        sh(["git", "reset", "-q", "--hard", f"origin/{base_ref}"], cwd=self.path)
        for i, c in enumerate(commits):
            for path, content in files[i * 2:(i + 1) * 2] or files:
                p = self.path / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
                sh(["git", "add", path], cwd=self.path)
            sh(["git", "commit", "-q", "--allow-empty", "-m",
                c["subject"] + "\n\nEpic: DEL-1"], cwd=self.path,
               env=self._ident_env(author, c["at"]))

    def add_commit(self, branch: str, subject: str, when: str, author: str,
                   files: list[tuple[str, str]]) -> None:
        sh(["git", "checkout", "-q", branch], cwd=self.path)
        for path, content in files:
            p = self.path / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            sh(["git", "add", path], cwd=self.path)
        sh(["git", "commit", "-q", "-m", subject + "\n\nEpic: DEL-1"], cwd=self.path,
           env=self._ident_env(author, when))

    def merge_to_main(self, branch: str, pr_id: int, actor: str, when: str) -> None:
        """Merge the PR branch locally with a backdated merge commit, push main.

        Bitbucket detects that the branch is contained in main and flips the PR
        to MERGED (recording a MERGED activity) without creating a server-side
        merge commit, so the whole history keeps the planned timestamps.
        """
        sh(["git", "fetch", "-q", "origin", "--prune"], cwd=self.path)
        sh(["git", "checkout", "-q", "main"], cwd=self.path)
        sh(["git", "reset", "-q", "--hard", "origin/main"], cwd=self.path)
        msg = (f"Merge pull request #{pr_id} in {PROJECT}/{self.slug} "
               f"from {branch} to main")
        sh(["git", "merge", "-q", "--no-ff", f"origin/{branch}", "-m", msg],
           cwd=self.path, env=self._ident_env(actor, when))
        self.push("main", actor)

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
    for name in dict.fromkeys(pr["reviewers"]):
        if name in have:
            continue
        api.call("POST", base + "/participants",
                 data={"user": {"name": name}, "role": "REVIEWER"}, ok=(200, 201))
        log(f"  {pr['id']} reviewer {name}")


def execute_pr(api: Api, git: RepoGit, pr: dict, pr_id: int | None,
               timeline: list[str]) -> int:
    base = f"/rest/api/1.0/projects/{PROJECT}/repos/{pr['repo']}/pull-requests"
    if pr_id is None:
        files: list[tuple[str, str]] = []
        rev_base = (pr["number"] - 1) * 10
        for c in pr["commits"]:
            rev = rev_base + c["n"] + 1
            files.extend(file_bundle(pr["repo"], pr["area"], pr["slug"], pr["ticket"],
                                     rev, pr["detail"]))
        base_ref = pr.get("base_branch", "main")
        git.make_branch(pr["branch"], base_ref, pr["commits"], pr["author"], files)
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

    for action in pr["actions"]:
        kind = action["kind"]
        actor = action["actor"]
        auth = (actor, USER_BY_NAME[actor][3])
        cur = api.call("GET", f"{base}/{pr_id}")
        if kind == "comment":
            api.call("POST", f"{base}/{pr_id}/comments", user=auth, ok=(200, 201), data={
                "text": action["text"], "severity": action.get("severity", "NORMAL"),
                "version": cur["version"]})
        elif kind == "push":
            part = action.get("commit", 0) + 2
            rev = rev_base + len(pr["commits"]) + part - 1
            files = file_bundle(pr["repo"], pr["area"], pr["slug"], pr["ticket"], rev,
                                pr["detail"])
            subject = f"{pr['ticket']}: address review feedback (rev {part - 1})"
            git.add_commit(pr["branch"], subject, action["at"], pr["author"], files)
            git.push(pr["branch"], pr["author"])
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
            git.merge_to_main(pr["branch"], pr_id, actor, action["at"])
            for _ in range(40):
                cur = api.call("GET", f"{base}/{pr_id}")
                if cur["state"] == "MERGED":
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(f"PR {pr_id} did not auto-merge after pushing main")
            log(f"  {pr['id']} merged into main (backdated merge commit)")
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
    return pr_id


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
        pr_id = execute_pr(api, git, pr, st.get("pr_id"), timeline)
        if pr["outcome"] == "merged":
            git.ensure()  # pick up the merge on main
        state[key] = {"done": True, "pr_id": pr_id, "timeline": timeline,
                      "repo": pr["repo"], "outcome": pr["outcome"]}
        STATE_FILE.write_text(json.dumps(state, indent=1))
        log(f"  {key} done (PR #{pr_id})")

    for p in plan_out["prs"]:
        s = state.get(p["id"], {})
        p["pr_id"] = s.get("pr_id")
        p["timeline"] = s.get("timeline", [])
    PLAN_FILE.write_text(json.dumps(plan_out, indent=1))
    log(f"generated {sum(1 for v in state.values() if v.get('done'))}/{len(planned)} PRs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

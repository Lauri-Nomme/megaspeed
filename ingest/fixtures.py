"""Synthetic Bitbucket Data Center data.

The fixture is *shaped like the DC REST API* (paging envelopes, user objects,
``fromRef``, activity actions, commit records) so that the fixture transport and
the live transport share one client and one parsing path. Action names and
fields are the real DC 9.x ones (``UPDATED`` + ``addedReviewers`` for reviewer
changes, ``RESCOPED`` + ``fromHash``/``toHash``, ``MERGED`` + ``commit``,
``REVIEWED``, ``commentAnchor`` on the activity, ``threadResolved`` /
``resolvedDate`` / ``resolver`` on comments). Nothing here is real: names,
tickets and prose are made up, and every free-text field is planted with a
``zzcanary…`` token so the pipeline can prove it discarded the text.

``pull-requests/{id}/activities`` items may carry a namespaced
``megaspeedPatchId`` block. That key is **not** a Bitbucket field: it stands in
for the output of the local deterministic push classifier (``git patch-id``,
prd/02) which needs a local clone we do not have in fixture mode. The live
client never sets it, and the pipeline falls back to ``push_class="other"``.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
BUSINESS_MINUTES_PER_DAY = 600  # 08:00 -> 18:00

# --- synthetic people (raw-zone only; only pseudonyms survive) -----------------
PEOPLE = [
    ("lauri.nomme", "Lauri Nomme"),
    ("toomas.bruno", "Toomas Bruno"),
    ("mari.tamm", "Mari Tamm"),
    ("kristjan.saar", "Kristjan Saar"),
    ("anna.kask", "Anna Kask"),
    ("peeter.mets", "Peeter Mets"),
    ("liisa.rand", "Liisa Rand"),
    ("mart.sepp", "Mart Sepp"),
]
SENTINELS = {"toomas.bruno", "kristjan.saar"}

# DC service accounts are user records with ``"type": "SERVICE"`` (no email).
# Real DC calls the built-in one ``bitbucket.system-user`` ("Bitbucket"); it
# comments on and auto-declines stale PRs, and must never count as a human.
BOT_USER = {
    "name": "bitbucket.system-user",
    "slug": "bitbucket.system-user",
    "displayName": "Bitbucket",
    "type": "SERVICE",
    "active": True,
}

REPOS = [("ENG", "platform"), ("ENG", "mobile")]

TICKETS = [f"PROJ-{n}" for n in (101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112)]
EXTRA_TICKETS = ["OPS-7", "ARCH-31", "PROJ-201"]

WORDS = (
    "retry backoff cache token parser queue scheduler webhook payload cursor "
    "shard header session worker limiter digest snapshot rollback upload "
    "stream index filter banner route schema throttle matrix ledger"
)
NOUNS = WORDS.split()


def _people() -> list[dict]:
    return [
        {"name": slug, "displayName": display, "emailAddress": f"{slug}@example.invalid",
         "type": "NORMAL", "active": True}
        for slug, display in PEOPLE
    ]


def _biz_time(total_minutes: int) -> datetime:
    days, rem = divmod(max(0, int(total_minutes)), BUSINESS_MINUTES_PER_DAY)
    day = BASE
    while days > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days -= 1
    return day + timedelta(minutes=rem)


def _epoch(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def _hash(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(40))


class _Prose:
    """Raw, human-looking text. Read for the ticket filter, then discarded."""

    def __init__(self, rng: random.Random, canary: str):
        self.rng = rng
        self.canary = canary

    def sentence(self, ticket: str | None) -> str:
        words = " ".join(self.rng.choice(NOUNS) for _ in range(self.rng.randint(3, 7)))
        head = f"{ticket}: " if ticket else ""
        return f"{head}{words} {self.canary}"

    def commit_message(self, ticket: str | None, kind: str) -> str:
        verbs = {
            "feature": "add", "fix": "fix", "refactor": "refactor", "test": "cover",
            "chore": "tidy", "docs": "document", "wip": "wip", "merge": "merge",
        }
        text = f"{verbs.get(kind, 'update')} {self.rng.choice(NOUNS)} {self.rng.choice(NOUNS)}"
        if ticket:
            text = f"{ticket} {text}"
        return f"{text}\n\n{self.canary} body paragraph."

    def description(self, ticket: str | None) -> str:
        parts = [self.sentence(ticket), self.sentence(None), f"checklist {self.canary}"]
        return "\n\n".join(parts)


def _comment(rng: random.Random, prose: _Prose, author: dict, cid: int, ticket: str | None,
             people: list[dict]) -> dict:
    """A DC 9.x comment record.

    Real shape: the *activity* carries ``commentAnchor``; the comment carries
    ``threadResolved`` / ``resolvedDate`` / ``resolver`` and nested ``comments``
    for replies. ``createdDate`` etc. are filled in by the caller once the
    surrounding activity has a timestamp.
    """
    severity = "BLOCKER" if rng.random() < 0.22 else "NORMAL"
    thread_open = rng.random() < 0.35
    replies = []
    if rng.random() < 0.3:
        replies.append(
            {
                "id": cid + 5000,
                "version": 0,
                "text": prose.sentence(ticket),
                "author": rng.choice(people),
                "createdDate": None,  # filled by caller (relative to parent)
                "updatedDate": None,
                "severity": "NORMAL",
                "state": "OPEN",
                "comments": [],
                "properties": {},
            }
        )
    return {
        "id": cid,
        "version": 0,
        "text": prose.sentence(ticket),
        "author": author,
        "createdDate": None,  # filled by caller
        "updatedDate": None,
        "severity": severity,
        "state": "OPEN" if thread_open else "RESOLVED",
        "threadResolved": not thread_open,
        "resolvedDate": None,  # filled by caller
        "resolver": None,      # filled by caller
        "comments": replies,
        "properties": {},
    }


def _make_pr(rng: random.Random, project: str, repo: str, pr_id: int, index: int) -> dict:
    people = _people()
    author = rng.choice(people)
    others = [p for p in people if p["name"] != author["name"]]
    reviewers = rng.sample(others, k=rng.choice([1, 2, 2, 3]))

    tickets = TICKETS + EXTRA_TICKETS
    ticket = rng.choice(tickets) if index % 7 != 3 else None  # ~1 in 7 has no key at all
    placement = ["branch", "title", "description", "commit", "branch", "title", "commit"][index % 7]
    canary = "zzcanary%08x" % rng.getrandbits(32)
    prose = _Prose(rng, canary)

    def key_in(where: str) -> str | None:
        return ticket if (ticket and placement == where) else None

    branch_ticket = key_in("branch")
    branch_slug = f"{rng.choice(['feature', 'bugfix', 'chore'])}/{(branch_ticket + '-') if branch_ticket else ''}{rng.choice(NOUNS)}-{rng.choice(NOUNS)}"
    title_ticket = key_in("title")
    desc_ticket = key_in("description")

    # Business-minute clock: a PR starts at a random point in the quarter and
    # every activity advances it. Weekends and nights simply do not exist.
    start_bm = rng.randint(0, 42 * BUSINESS_MINUTES_PER_DAY)
    cursor = 0

    rounds = rng.choice([1, 1, 2, 2, 2, 3, 4])
    outcome = rng.choices(["merged", "declined", "open"], weights=[72, 10, 18])[0]
    commit_ticket = key_in("commit")

    activities: list[dict] = []
    commits: list[dict] = []
    next_activity_id = pr_id * 1000 + rng.randint(1, 99)
    comment_id = pr_id * 100

    def add(action: str, advance: int, **extra) -> dict:
        nonlocal cursor, next_activity_id
        cursor += advance
        now = _biz_time(start_bm + cursor)
        activity = {"id": next_activity_id, "createdDate": _epoch(now), "action": action, **extra}
        next_activity_id += 1
        activities.append(activity)
        return activity

    def make_commits(n: int, offset_minutes: int, kind: str) -> list[dict]:
        nonlocal cursor
        made = []
        for _ in range(n):
            cursor += max(0, offset_minutes)
            ts = _biz_time(start_bm + cursor)
            commit = {
                "id": _hash(rng),
                "displayId": "",
                "author": author,
                "authorTimestamp": _epoch(ts),
                "committer": author,
                "committerTimestamp": _epoch(ts),
                "message": prose.commit_message(commit_ticket, kind),
            }
            commit["displayId"] = commit["id"][:7]
            commits.append(commit)
            made.append(commit)
        return made

    opened = add("OPENED", rng.randint(0, 30), user=author)
    make_commits(rng.randint(1, 2), 0, "feature")
    target_hash = _hash(rng)  # main's head; moves occasionally (target rescope)

    approved: set[str] = set()
    review_round = 0
    for round_index in range(rounds):
        if round_index > 0:
            kind = rng.choices(["content", "pure_rebase", "rebase_conflict"], weights=[60, 28, 12])[0]
            previous_from = commits[-1]["id"] if commits else None
            removed: list[dict] = []
            if previous_from and rng.random() < 0.35:
                removed = [next(c for c in commits if c["id"] == previous_from)]
            added = make_commits(rng.randint(1, 3), rng.randint(20, 180), "fix")
            previous_to = target_hash
            if rng.random() < 0.25:
                target_hash = _hash(rng)  # target branch itself moved
            add(
                "RESCOPED",
                rng.randint(60, 900),
                user=author,
                fromHash=added[-1]["id"],
                toHash=target_hash,
                previousFromHash=previous_from,
                previousToHash=previous_to,
                added={"commits": added, "total": len(added)},
                removed={"commits": removed, "total": len(removed)},
                # Not a DC field: stands in for the local deterministic push
                # classifier (git patch-id), which needs a clone (GAP-01).
                megaspeedPatchId={"push_class": kind},
            )
            if approved and rng.random() < 0.7:
                loser = rng.choice(sorted(approved))
                approved.discard(loser)
                add("UNAPPROVED", rng.randint(1, 40), user=_person(people, loser))

        if round_index == 0:
            # DC 9.x reviewer roster changes arrive as ACTION=UPDATED with
            # addedReviewers / removedReviewers (never "REVIEWER_UPDATED").
            if rng.random() < 0.3:
                newcomer = rng.choice([p for p in others if p not in reviewers])
                reviewers.append(newcomer)
                add("UPDATED", rng.randint(5, 60), user=author,
                    addedReviewers=[newcomer], removedReviewers=[])
            elif len(reviewers) > 1 and rng.random() < 0.2:
                leaver = rng.choice(reviewers)
                reviewers.remove(leaver)
                approved.discard(leaver["name"])
                add("UPDATED", rng.randint(5, 60), user=author,
                    addedReviewers=[], removedReviewers=[leaver])

        for _ in range(rng.randint(0, 2)):
            reviewer = rng.choice(reviewers)
            comment = _comment(rng, prose, reviewer, comment_id, ticket, people)
            comment_id += 1
            anchor = (
                {"path": "src/main.py", "line": rng.randint(1, 400), "lineType": "ADDED"}
                if rng.random() < 0.6
                else None
            )
            activity = add(
                "COMMENTED",
                rng.randint(10, 240),
                user=reviewer,
                comment=comment,
                commentAction="ADDED",
                **({"commentAnchor": anchor} if anchor else {}),
            )
            comment["createdDate"] = activity["createdDate"]
            comment["updatedDate"] = activity["createdDate"]
            for reply in comment["comments"]:
                reply["createdDate"] = activity["createdDate"] + rng.randint(1, 120) * 60_000
                reply["updatedDate"] = reply["createdDate"]
            if comment["threadResolved"]:
                comment["resolver"] = rng.choice(people)
                comment["resolvedDate"] = activity["createdDate"] + rng.randint(5, 300) * 60_000
            if rng.random() < 0.18:
                add("NEEDS_WORK", rng.randint(1, 15), user=reviewer)

        for reviewer in reviewers:
            if reviewer["name"] in approved or rng.random() > 0.75:
                continue
            approved.add(reviewer["name"])
            add("APPROVED", rng.randint(20, 500), user=reviewer)

        # A reviewer completing their review (DC: PUT .../review -> REVIEWED).
        if rng.random() < 0.3:
            add("REVIEWED", rng.randint(5, 120), user=rng.choice(reviewers))
        review_round += 1

    merge_commit = None
    closed_at = None
    if outcome == "merged":
        merged = add("MERGED", rng.randint(60, 1200), user=rng.choice(reviewers), autoMerge=False)
        merge_commit = {
            "id": _hash(rng),
            "displayId": "",
            "author": author,
            "authorTimestamp": merged["createdDate"],
            "committer": author,
            "committerTimestamp": merged["createdDate"],
            "message": (
                f"Merge pull request #{pr_id} in {project}/{repo} from {branch_slug} to main"
                f"\n\n{canary} merge body."
            ),
        }
        merge_commit["displayId"] = merge_commit["id"][:7]
        merged["commit"] = merge_commit
        closed_at = merged["createdDate"]
    elif outcome == "declined":
        if rng.random() < 0.6:
            # Stale auto-decline: the DC service user comments and declines.
            comment = _comment(rng, prose, BOT_USER, comment_id, ticket, people)
            comment_id += 1
            # An inactivity notice is a new open thread, not a resolved one.
            comment["threadResolved"] = False
            comment["state"] = "OPEN"
            activity = add("COMMENTED", rng.randint(30, 600), user=BOT_USER,
                           comment=comment, commentAction="ADDED")
            comment["createdDate"] = activity["createdDate"]
            comment["updatedDate"] = activity["createdDate"]
            declined = add("DECLINED", rng.randint(1, 60), user=BOT_USER)
        else:
            declined = add("DECLINED", rng.randint(60, 900), user=author)
        closed_at = declined["createdDate"]

    activities.sort(key=lambda a: (a["createdDate"], a["id"]))
    last_ts = max(
        [a["createdDate"] for a in activities]
        + [(a.get("comment") or {}).get("resolvedDate") or 0 for a in activities]
    )

    pr = {
        "id": pr_id,
        "version": len(activities),
        "title": f"{title_ticket + ': ' if title_ticket else ''}{rng.choice(NOUNS).capitalize()} for {rng.choice(NOUNS)}",
        "description": prose.description(desc_ticket),
        "state": {"merged": "MERGED", "declined": "DECLINED", "open": "OPEN"}[outcome],
        "open": outcome == "open",
        "closed": outcome != "open",
        "draft": False,
        "createdDate": opened["createdDate"],
        "updatedDate": last_ts,
        "closedDate": closed_at,
        "fromRef": {
            "id": f"refs/heads/{branch_slug}",
            "displayId": branch_slug,
            "type": "BRANCH",
            "latestCommit": commits[-1]["id"],
            "repository": {"slug": repo, "project": {"key": project}},
        },
        "toRef": {
            "id": "refs/heads/main",
            "displayId": "main",
            "type": "BRANCH",
            "latestCommit": target_hash,
            "repository": {"slug": repo, "project": {"key": project}},
        },
        "author": {"user": author, "role": "AUTHOR", "approved": False},
        "reviewers": [
            {"user": r, "role": "REVIEWER", "approved": r["name"] in approved} for r in reviewers
        ],
        "participants": [{"user": p, "role": "PARTICIPANT", "approved": False} for p in people],
        "properties": (
            {"mergeCommit": {"id": merge_commit["id"]}} if merge_commit else {}
        ),
        "links": {"self": [{"href": f"https://bitbucket.example.invalid/projects/{project}/repos/{repo}/pull-requests/{pr_id}"}]},
    }
    return {"pull_request": pr, "activities": activities, "commits": commits}


def _person(people: list[dict], slug: str) -> dict:
    return next(p for p in people if p["name"] == slug)


def generate(n: int = 50, seed: int = 20261115) -> dict:
    """Build a deterministic bundle of ``n`` pull requests across 2 repos."""
    by_repo: dict[tuple[str, str], list[dict]] = {r: [] for r in REPOS}
    for i in range(n):
        project, repo = REPOS[i % len(REPOS)]
        pr_id = 100 + i // len(REPOS)
        by_repo[(project, repo)].append(_make_pr(random.Random(seed + i * 7919), project, repo, pr_id, i))

    return {
        "schema": "megaspeed.fixture/1",
        "note": "Synthetic Bitbucket Data Center payloads. No real people, tickets or text.",
        "seed": seed,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "projects": [
            {
                "key": project,
                "repos": [
                    {"slug": repo, "pull_requests": by_repo[(project, repo)]}
                    for (proj, repo) in REPOS
                    if proj == project
                ],
            }
            for project in dict.fromkeys(p for p, _ in REPOS)
        ],
    }


def write_bundle(path: Path, n: int = 50, seed: int = 20261115) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bundle = generate(n=n, seed=seed)
    path.write_text(json.dumps(bundle, indent=1) + "\n")
    return path


if __name__ == "__main__":  # python3 -m ingest.fixtures [--n 50] [--out PATH]
    import argparse

    ap = argparse.ArgumentParser(description="write the synthetic Bitbucket fixture bundle")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=20261115)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "fixtures" / "bitbucket-dc-fixture.json"))
    args = ap.parse_args()
    print(write_bundle(Path(args.out), n=args.n, seed=args.seed))

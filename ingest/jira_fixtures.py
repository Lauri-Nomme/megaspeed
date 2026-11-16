"""Synthetic Jira Data Center data, shaped like the DC 9.17 REST API.

The fixture is *shaped like* ``/rest/api/2`` and ``/rest/agile/1.0`` (paging
envelopes, user records, changelog histories, the Greenhopper Sprint field blob,
native worklogs) so that the fixture transport and the live transport share one
client and one parsing path.

It mirrors the Bitbucket fixture on purpose: the same synthetic people and the
same ``PROJ-*`` ticket keys, so a store that imports both bundles has to resolve
*one* person and *one* ticket across the two sources (prd/03 "Identity
resolution", prd/04 "Dev-status / remote links").

Nothing here is real: every human-authored field (summary, description, sprint
goal, worklog comment) is planted with a ``zzcanary…`` token so the pipeline can
prove it discarded the text. Worklogs are backdated relative to the moment they
were written, so the ``created - started`` gap covers all four
``log_delay_bucket`` labels (prd/04 "worklog noise").
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .fixtures import PEOPLE

PROJECT = "PROJ"
EPIC_KEY = "PROJ-1"
SEED = 20261115

FIELDS = [
    {"id": "summary", "name": "Summary", "custom": False},
    {"id": "description", "name": "Description", "custom": False},
    {"id": "status", "name": "Status", "custom": False},
    {"id": "customfield_10100", "name": "Epic Link", "custom": True},
    {"id": "customfield_10104", "name": "Sprint", "custom": True},
    {"id": "customfield_10106", "name": "Story Points", "custom": True},
    {"id": "customfield_10111", "name": "Team", "custom": True},
]

STATUS_TODO = {"id": "10000", "name": "To Do", "statusCategory": {"key": "new", "name": "To Do"}}
STATUS_DOING = {
    "id": "10001",
    "name": "In Progress",
    "statusCategory": {"key": "indeterminate", "name": "In Progress"},
}
STATUS_DONE = {"id": "10002", "name": "Done", "statusCategory": {"key": "done", "name": "Done"}}
STATUSES = [STATUS_TODO, STATUS_DOING, STATUS_DONE]
RESOLUTION_DONE = {"id": "10000", "name": "Done"}

SPRINTS = [
    {
        "id": 1,
        "name": "PROJ Sprint 1",
        "state": "closed",
        "startDate": "2026-08-17T09:00:00.000Z",
        "endDate": "2026-09-11T17:00:00.000Z",
        "completeDate": "2026-09-11T17:30:00.000Z",
        "goal": "PROJ Sprint 1 scope",
        "originBoardId": 1,
    },
    {
        "id": 2,
        "name": "PROJ Sprint 2",
        "state": "closed",
        "startDate": "2026-09-14T09:00:00.000Z",
        "endDate": "2026-10-09T17:00:00.000Z",
        "completeDate": "2026-10-09T17:20:00.000Z",
        "goal": "PROJ Sprint 2 scope",
        "originBoardId": 1,
    },
    {
        "id": 3,
        "name": "PROJ Sprint 3",
        "state": "active",
        "startDate": "2026-11-02T09:00:00.000Z",
        "endDate": "2026-11-27T17:00:00.000Z",
        "goal": "PROJ Sprint 3 scope",
        "originBoardId": 1,
    },
]

# index -> sprint id (None = backlog). Mirrors the lab's three sprints + backlog.
MEMBERSHIP = {
    101: 1, 102: 1, 103: 1, 104: 1,
    105: 2, 106: 2, 107: 2, 108: 2,
    109: 3, 110: 3,
    111: None, 112: None,
}

# PROJ-104 is in sprint 1 from the start: Jira recorded no "Sprint" change for
# it, so the reducer has to synthesise an ``at_start`` sprint_added (prd/04).
AT_START = {"PROJ-104"}

THEMES = [
    ("auth", "Add login form validation"),
    ("auth", "Reject malformed e-mail addresses"),
    ("api", "Retry the token endpoint with backoff"),
    ("api", "Cache the session lookup"),
    ("web", "Render the queue page server-side"),
    ("web", "Add a keyboard shortcut for search"),
    ("ops", "Alert on queue depth"),
    ("ops", "Rotate the webhook secret"),
    ("api", "Paginate the audit endpoint"),
    ("web", "Show the diff size on the PR card"),
    ("api", "Fix the shard rebalancing race"),
    ("ops", "Document the rollback runbook"),
]

WORKLOG_COMMENTS = [
    "pairing on the parser",
    "wrote the failing test first",
    "debugged the retry loop",
    "review fixes",
    "cleanup and docs",
]

# created - started gap per worklog, in minutes: one per log_delay_bucket.
DELAY_MINUTES = [90, 26 * 60, 5 * 24 * 60, 61 * 24 * 60]


def _epoch_ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def _jira_ts(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}+0000"


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _people() -> list[dict]:
    """The Bitbucket fixture's people, as Jira user records."""
    return [
        {
            "self": f"http://jira.example.invalid/rest/api/2/user?username={slug}",
            "name": slug,
            "key": f"JIRAUSER{100 + index}",
            "emailAddress": f"{slug}@example.invalid",
            "displayName": display,
            "active": True,
            "timeZone": "Etc/UTC",
        }
        for index, (slug, display) in enumerate(PEOPLE)
    ]


def _sprint_field(sprint: dict) -> str:
    """The Greenhopper serialization DC 9.x returns for the Sprint field."""
    return (
        "com.atlassian.greenhopper.service.sprint.Sprint@"
        f"deadbeef[activatedDate={sprint.get('startDate')},"
        f"completeDate={sprint.get('completeDate') or '<null>'},"
        f"endDate={sprint['endDate']},goal={sprint['goal']},id={sprint['id']},"
        f"name={sprint['name']},rapidViewId={sprint['originBoardId']},"
        f"startDate={sprint['startDate']},state={sprint['state'].upper()}]"
    )


def _item(field_name: str, from_string=None, to_string=None, from_id=None, to_id=None) -> dict:
    return {
        "field": field_name,
        "fieldtype": "jira",
        "from": from_id,
        "fromString": from_string,
        "to": to_id,
        "toString": to_string,
    }


def _worklog(wid: int, author: dict, started: datetime, minutes: int, delay_minutes: int,
             canary: str, comment: str) -> dict:
    created = started + timedelta(minutes=delay_minutes)
    return {
        "self": f"http://jira.example.invalid/rest/api/2/issue/1/worklog/{wid}",
        "id": str(wid),
        "author": author,
        "updateAuthor": author,
        "comment": f"{comment} {canary}",
        "created": _jira_ts(created),
        "updated": _jira_ts(created),
        "started": _jira_ts(started),
        "timeSpent": f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m",
        "timeSpentSeconds": minutes * 60,
        "issueId": "1",
    }


def _story(rng: random.Random, number: int, people: list[dict], reporter: dict) -> dict:
    key = f"{PROJECT}-{number}"
    canary = "zzcanary%08x" % rng.getrandbits(32)
    area, title = THEMES[(number - 101) % len(THEMES)]
    sprint_id = MEMBERSHIP.get(number)
    sprint = next((s for s in SPRINTS if s["id"] == sprint_id), None)
    assignee = people[(number * 7 + 2) % 5]
    points = rng.choice([1, 2, 3, 5, 8, 13, None])
    if number == 103:
        # Jira's Story Points field is a float: a half point must be rounded to
        # whole points (and counted), never truncated to zero.
        points = 2.5
    history_id = number * 1000
    histories: list[dict] = []

    def history(ts: datetime, author: dict, *items: dict) -> None:
        nonlocal history_id
        history_id += 1
        histories.append(
            {
                "id": str(history_id),
                "created": _jira_ts(ts),
                "author": author,
                "items": list(items),
            }
        )

    created = _at("2026-08-10T09:00:00.000Z") + timedelta(hours=(number - 100) * 7)
    history(created, reporter, _item("summary", None, f"{title} {canary}"))

    # refinement: the estimate is set on the ticket, not at creation
    if points is not None:
        history(
            created + timedelta(days=2),
            reporter,
            _item("Story Points", None, str(points), None, str(points)),
        )

    started_work: datetime | None = None
    if sprint is not None:
        window_start = _at(sprint["startDate"])
        if key not in AT_START:
            # PROJ-105 starts in sprint 1 and is carried over to sprint 2 below.
            origin = SPRINTS[0] if number == 105 else sprint
            history(
                _at(origin["startDate"]) + timedelta(hours=rng.randint(1, 30)),
                reporter,
                _item("Sprint", None, origin["name"], None, str(origin["id"])),
            )
        started_work = window_start + timedelta(days=1, hours=rng.randint(0, 6))

    # status: closed sprints end Done, the active one is part-way, the backlog
    # is untouched. One story is reassigned mid-flight.
    status = STATUS_TODO
    resolution = None
    resolution_date = None
    if sprint is not None and sprint["state"] == "closed":
        end = _at(sprint["endDate"])
        history(
            started_work + timedelta(days=1),
            assignee,
            _item("status", STATUS_TODO["name"], STATUS_DOING["name"],
                  STATUS_TODO["id"], STATUS_DOING["id"]),
        )
        history(
            end - timedelta(days=1),
            assignee,
            _item("resolution", None, RESOLUTION_DONE["name"], None, RESOLUTION_DONE["id"]),
            _item("status", STATUS_DOING["name"], STATUS_DONE["name"],
                  STATUS_DOING["id"], STATUS_DONE["id"]),
        )
        status, resolution, resolution_date = STATUS_DONE, RESOLUTION_DONE, end - timedelta(days=1)
    elif sprint is not None and number % 2 == 1:
        history(
            started_work + timedelta(days=2),
            assignee,
            _item("status", STATUS_TODO["name"], STATUS_DOING["name"],
                  STATUS_TODO["id"], STATUS_DOING["id"]),
        )
        status = STATUS_DOING

    if number == 107:
        other = people[(number * 7 + 9) % len(people)]
        history(
            started_work + timedelta(days=3),
            reporter,
            _item("assignee", assignee["displayName"], other["displayName"],
                  assignee["name"], other["name"]),
        )
        assignee = other

    if number == 105:
        # A story carried over mid-flight: one changelog item that removes it
        # from sprint 1 and adds it to sprint 2 (prd/04 carry-over).
        target = next(s for s in SPRINTS if s["id"] == 2)
        history(
            _at(target["startDate"]) + timedelta(hours=2),
            reporter,
            _item("Sprint", SPRINTS[0]["name"], target["name"], "1", "2"),
        )

    # a couple of "Epic Child" items: Jira's own epic-link bookkeeping. The
    # reducer must read and drop them (they are not events in our vocabulary).
    if number in (101, 102):
        history(created + timedelta(minutes=5), reporter, _item("Epic Child", None, key))

    worklogs = []
    if started_work is not None:
        for index in range(1, rng.choice([1, 1, 2]) + 1):
            minutes = rng.choice([30, 60, 90, 120, 240])
            worklogs.append(
                _worklog(
                    number * 10 + index,
                    assignee if index == 1 else people[(number + index) % len(people)],
                    started_work + timedelta(days=index, hours=rng.randint(0, 4)),
                    minutes,
                    DELAY_MINUTES[(number + index) % len(DELAY_MINUTES)],
                    canary,
                    rng.choice(WORKLOG_COMMENTS),
                )
            )

    stamps = [created]
    stamps += [_at(h["created"]) for h in histories]
    stamps += [_at(w["created"]) for w in worklogs]
    if resolution_date is not None:
        stamps.append(resolution_date)
    updated = max(stamps)

    fields = {
        "summary": f"{title} {canary}",
        "description": (
            f"Epic: {EPIC_KEY}\n\nTicket: {key}\n\n{title}.\n\n"
            f"Area: {area}\nRepo: delivery-{area}\n\nzzcanary {canary}"
        ),
        "issuetype": {"id": "10001", "name": "Story", "subtask": False},
        "status": status,
        "resolution": resolution,
        "resolutiondate": _jira_ts(resolution_date) if resolution_date else None,
        "assignee": assignee,
        "reporter": reporter,
        "creator": reporter,
        "created": _jira_ts(created),
        "updated": _jira_ts(updated),
        "labels": [area, "repo-delivery"],
        "customfield_10100": EPIC_KEY,
        "customfield_10106": points,
        "customfield_10104": [_sprint_field(sprint)] if sprint else None,
        "worklog": {
            "startAt": 0,
            "maxResults": 20,
            "total": len(worklogs),
            "worklogs": worklogs,
        },
    }
    histories.sort(key=lambda h: h["created"])
    return {
        "id": str(10000 + number),
        "key": key,
        "self": f"http://jira.example.invalid/rest/api/2/issue/{10000 + number}",
        "fields": fields,
        "changelog": {
            "startAt": 0,
            "maxResults": len(histories),
            "total": len(histories),
            "histories": histories,
        },
    }


def _epic(rng: random.Random, people: list[dict], reporter: dict) -> dict:
    canary = "zzcanary%08x" % rng.getrandbits(32)
    created = _at("2026-08-09T09:00:00.000Z")
    histories = [
        {
            "id": "9001",
            "created": _jira_ts(created + timedelta(minutes=5)),
            "author": reporter,
            "items": [_item("Epic Child", None, f"{PROJECT}-101")],
        },
        {
            "id": "9002",
            "created": _jira_ts(created + timedelta(days=30)),
            "author": reporter,
            "items": [
                _item("status", STATUS_TODO["name"], STATUS_DOING["name"],
                      STATUS_TODO["id"], STATUS_DOING["id"]),
            ],
        },
    ]
    return {
        "id": "10000",
        "key": EPIC_KEY,
        "self": f"http://jira.example.invalid/rest/api/2/issue/10000",
        "fields": {
            "summary": f"Delivery platform epic {canary}",
            "description": f"Epic covering the delivery platform. zzcanary {canary}",
            "issuetype": {"id": "10000", "name": "Epic", "subtask": False},
            "status": STATUS_DOING,
            "resolution": None,
            "resolutiondate": None,
            "assignee": reporter,
            "reporter": reporter,
            "creator": reporter,
            "created": _jira_ts(created),
            "updated": _jira_ts(created + timedelta(days=30)),
            "labels": ["epic"],
            "customfield_10100": None,
            "customfield_10106": None,
            "customfield_10104": None,
            "worklog": {"startAt": 0, "maxResults": 20, "total": 0, "worklogs": []},
        },
        "changelog": {
            "startAt": 0,
            "maxResults": len(histories),
            "total": len(histories),
            "histories": histories,
        },
    }


def generate(n: int | None = None, seed: int = SEED) -> dict:
    """Build a deterministic Jira bundle: 1 epic, ``n`` stories, 3 sprints."""
    rng = random.Random(seed)
    people = _people()
    reporter = people[0]
    numbers = sorted(MEMBERSHIP)[: n if n else None]
    issues = [_epic(rng, people, reporter)]
    issues += [_story(rng, number, people, reporter) for number in numbers]
    sprint_issues: dict[str, list[str]] = {str(s["id"]): [] for s in SPRINTS}
    for issue in issues:
        sid = sprint_id_of(issue)
        if sid is not None:
            sprint_issues[str(sid)].append(issue["key"])
    return {
        "schema": "megaspeed.jira-fixture/1",
        "note": "Synthetic Jira Data Center payloads. No real people, tickets or text.",
        "seed": seed,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": PROJECT,
        "fields": FIELDS,
        "statuses": STATUSES,
        "boards": [
            {
                "id": 1,
                "self": "http://jira.example.invalid/rest/agile/1.0/board/1",
                "name": "PROJ board",
                "type": "scrum",
            }
        ],
        "sprints": SPRINTS,
        "sprint_issues": sprint_issues,
        "issues": issues,
    }


def sprint_id_of(issue: dict) -> int | None:
    from .jira import sprint_id

    return sprint_id((issue.get("fields") or {}).get("customfield_10104"))


def write_bundle(path: Path, n: int | None = None, seed: int = SEED) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(generate(n=n, seed=seed), indent=1) + "\n")
    return path


if __name__ == "__main__":  # python3 -m ingest.jira_fixtures [--n 12] [--out PATH]
    import argparse

    ap = argparse.ArgumentParser(description="write the synthetic Jira fixture bundle")
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "fixtures" / "jira-dc-fixture.json"),
    )
    args = ap.parse_args()
    print(write_bundle(Path(args.out), n=args.n, seed=args.seed))

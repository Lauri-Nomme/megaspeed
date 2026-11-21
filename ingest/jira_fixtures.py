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
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

from .fixtures import PEOPLE
from .jira import TEMPO
from .transport import BitbucketError, FixtureJiraTransport

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
    {"id": "customfield_10111", "name": "Team", "custom": True,
     "schema": {"type": "any",
                "custom": "com.atlassian.teams:rm-teams-custom-field-team"}},
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


# =========================================================================== #
# the install's *actual* workflow (prd/29) and the DEL corpus
# =========================================================================== #
# The generic PROJ fixture above keeps Jira's three status categories, which is
# what the pre-prd/29 path is tested against. The corpus below is the other half
# of the story: the real installation runs a seven-status Story workflow and a
# six-status Bug workflow, and the whole point of prd/29 is that ``in review``
# and ``in verification`` survive the projection as their own stages.
#
# Statuses are global in Jira (a workflow reuses them), so the two workflows
# share IN PROGRESS / IN REVIEW / IN VERIFICATION and differ at the ends.
WORKFLOW_STATUSES = [
    {"id": "20000", "name": "OPEN",
     "statusCategory": {"key": "new", "name": "To Do"}},
    {"id": "20001", "name": "READY FOR REFINEMENT",
     "statusCategory": {"key": "new", "name": "To Do"}},
    {"id": "20002", "name": "REFINED",
     "statusCategory": {"key": "new", "name": "To Do"}},
    {"id": "20003", "name": "IN PROGRESS",
     "statusCategory": {"key": "indeterminate", "name": "In Progress"}},
    {"id": "20004", "name": "IN REVIEW",
     "statusCategory": {"key": "indeterminate", "name": "In Progress"}},
    {"id": "20005", "name": "IN VERIFICATION",
     "statusCategory": {"key": "indeterminate", "name": "In Progress"}},
    {"id": "20006", "name": "CLOSED",
     "statusCategory": {"key": "done", "name": "Done"}},
    {"id": "20007", "name": "NEW",
     "statusCategory": {"key": "new", "name": "To Do"}},
    {"id": "20008", "name": "TRIAGED",
     "statusCategory": {"key": "new", "name": "To Do"}},
    {"id": "20009", "name": "RESOLVED",
     "statusCategory": {"key": "done", "name": "Done"}},
]
STATUS_BY_NAME = {status["name"]: status for status in WORKFLOW_STATUSES}

STORY_WORKFLOW = (
    "OPEN", "READY FOR REFINEMENT", "REFINED", "IN PROGRESS", "IN REVIEW",
    "IN VERIFICATION", "CLOSED",
)
BUG_WORKFLOW = (
    "NEW", "TRIAGED", "IN PROGRESS", "IN REVIEW", "IN VERIFICATION", "RESOLVED",
)
#: The back-transitions the install allows (rework / reopen). They are emitted
#: by the corpus below, not just documented.
WORKFLOW_TRANSITIONS = {
    "story": {
        "OPEN": ("READY FOR REFINEMENT", "CLOSED"),
        "READY FOR REFINEMENT": ("REFINED", "CLOSED"),
        "REFINED": ("IN PROGRESS", "CLOSED"),
        "IN PROGRESS": ("IN REVIEW", "CLOSED"),
        "IN REVIEW": ("IN VERIFICATION", "IN PROGRESS", "CLOSED"),
        "IN VERIFICATION": ("CLOSED", "IN PROGRESS"),
    },
    "bug": {
        "NEW": ("TRIAGED",),
        "TRIAGED": ("IN PROGRESS",),
        "IN PROGRESS": ("IN REVIEW",),
        "IN REVIEW": ("IN VERIFICATION", "IN PROGRESS"),
        "IN VERIFICATION": ("RESOLVED", "IN PROGRESS"),
        "RESOLVED": ("TRIAGED",),
    },
}
#: prd/29: normalised status name -> stage. This is what the scrape is run with
#: (``[jira.stages]`` / ``MEGASPEED_JIRA_STAGES``) so review and QA stay apart.
WORKFLOW_STAGE_MAP = {
    "open": "backlog",
    "ready for refinement": "backlog",
    "new": "backlog",
    "refined": "ready",
    "triaged": "ready",
    "in progress": "in_progress",
    "in review": "in_review",
    "in verification": "in_verification",
    "closed": "done",
    "resolved": "done",
}
#: The alias the corpus builder and the docs use.
DEL_STAGE_MAP = WORKFLOW_STAGE_MAP

# --------------------------------------------------------------------------- #
# DEL corpus: the same people / tickets / sprints as jira-lab/seed.py
# --------------------------------------------------------------------------- #
DEL_PROJECT = "DEL"
DEL_PROJECT_NAME = "Delivery Platform"
DEL_EPIC_KEY = "DEL-1"
DEL_EPIC_SUMMARY = "Delivery platform epic"
DEL_SEED = 20261115
DEL_TICKET_NUMBERS = tuple(range(100, 200))
#: ``DEL-100 .. DEL-199``; every key is referenced by the Bitbucket corpus.
DEL_WORK_START, DEL_WORK_END = 9, 17
DEL_UTC = timezone.utc
DEL_EARLIEST_CREATED = datetime(2026, 8, 3, 9, 0, tzinfo=DEL_UTC)
DEL_WORKLOG_CUTOFF = datetime(2026, 11, 13, 17, 0, tzinfo=DEL_UTC)
#: The snapshot the corpus is backdated against (the scrape's own ``as_of`` is
#: later; every generated timestamp stays below this line).
DEL_HORIZON = datetime(2026, 11, 17, 18, 0, tzinfo=DEL_UTC)
DEL_EPIC_CREATED = datetime(2026, 7, 27, 9, 0, tzinfo=DEL_UTC)

DEL_USERS = [
    # name, displayName, email (verbatim from jira-lab/seed.py, plus the admin
    # who creates the issues -- the same account the Bitbucket corpus uses).
    {"name": "admin", "displayName": "Lab Admin",
     "emailAddress": "admin@lab.example.com"},
    {"name": "alice", "displayName": "Alice Nguyen",
     "emailAddress": "alice.nguyen@delivery.example"},
    {"name": "bob", "displayName": "Bob Ferreira",
     "emailAddress": "bob.ferreira@delivery.example"},
    {"name": "carol", "displayName": "Carol Osei",
     "emailAddress": "carol.osei@delivery.example"},
    {"name": "dave", "displayName": "Dave Kowalski",
     "emailAddress": "dave.kowalski@delivery.example"},
    {"name": "erin", "displayName": "Erin Lindqvist",
     "emailAddress": "erin.lindqvist@delivery.example"},
    {"name": "frank", "displayName": "Frank Moreau",
     "emailAddress": "frank.moreau@delivery.example"},
]
DEL_AUTHORS = [user for user in DEL_USERS if user["name"] in
               ("alice", "bob", "carol", "dave", "erin")]
DEL_REPORTER = DEL_USERS[0]          # admin creates every issue, like the seed

# 20 themes x 5 tickets (copied verbatim from jira-lab/seed.py, which copied
# them from corpus/generate.py).
DEL_THEMES = [
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

DEL_WORKLOG_COMMENTS = [
    "Implemented the first slice and wired the tests.",
    "Paired on the failure path; added regression coverage.",
    "Debugged the staging repro, root cause was the retry budget.",
    "Reviewed the spec with the product owner and adjusted the payload.",
    "Cleanup, docs and a small refactor before hand-off.",
]

#: The same three sprints / windows as jira-lab/backdate.py: S1 and S2 closed,
#: S3 active, DEL-190..199 in the backlog. ``members`` are ticket indices.
DEL_SPRINTS = [
    {"id": 1, "name": "DEL Sprint 1", "state": "closed",
     "start": date(2026, 8, 17), "end": date(2026, 9, 10),
     "members": range(0, 30)},
    {"id": 2, "name": "DEL Sprint 2", "state": "closed",
     "start": date(2026, 9, 11), "end": date(2026, 10, 7),
     "members": range(30, 60)},
    {"id": 3, "name": "DEL Sprint 3", "state": "active",
     "start": date(2026, 10, 8), "end": date(2026, 11, 27),
     "members": range(60, 90)},
]
DEL_SPRINT_BY_INDEX = {
    index: spec for spec in DEL_SPRINTS for index in spec["members"]
}
#: Every 17th issue was in its sprint from the start: Jira recorded no "Sprint"
#: change, so the reducer has to synthesise an ``at_start`` sprint_added.
DEL_AT_START = {index for index in range(0, 100, 17)}
#: A deterministic minority are Bugs, so the Bug workflow (and its reopen) is
#: exercised too; the remaining 88 are Stories. Every issue still becomes one
#: STORY object and carries a Bitbucket ticket key.
DEL_BUG_INDICES = {index for index in range(0, 100) if index % 8 == 5}
#: Stories that went back from review to in progress (rework) ...
DEL_REWORK_INDICES = {index for index in range(0, 100) if index % 4 == 1}
#: ... and stories QA bounced back from verification (failed verification).
DEL_QA_REWORK_INDICES = {index for index in range(0, 100) if index % 13 == 6}
#: Two bugs were reopened after RESOLVED (bug RESOLVED -> TRIAGED).
DEL_REOPEN_INDICES = {index for index in DEL_BUG_INDICES if index % 16 == 5}


def _del_user(name: str) -> dict:
    """One Jira DC user record (the shape the REST API returns)."""
    record = next(user for user in DEL_USERS if user["name"] == name)
    return {
        "self": f"http://jira.example.invalid/rest/api/2/user?username={name}",
        "name": name,
        "key": f"JIRAUSER{10100 + DEL_USERS.index(record)}",
        "emailAddress": record["emailAddress"],
        "displayName": record["displayName"],
        "active": True,
        "timeZone": "Etc/UTC",
    }


def _del_people() -> dict[str, dict]:
    return {user["name"]: _del_user(user["name"]) for user in DEL_USERS}


def _del_sprint_record(spec: dict) -> dict:
    start = datetime.combine(spec["start"], dtime(9, 0), tzinfo=DEL_UTC)
    end = datetime.combine(spec["end"], dtime(17, 0), tzinfo=DEL_UTC)
    complete = (datetime.combine(spec["end"], dtime(16, 0), tzinfo=DEL_UTC)
                if spec["state"] == "closed" else None)
    return {
        "id": spec["id"],
        "name": spec["name"],
        "state": spec["state"],
        "startDate": _jira_ts(start),
        "endDate": _jira_ts(end),
        "completeDate": _jira_ts(complete) if complete else None,
        "goal": f"{spec['name']} scope",
        "originBoardId": 1,
    }


# -- business time (the same model as jira-lab/backdate.py) ------------------
def _del_is_workday(day: date) -> bool:
    return day.weekday() < 5


def _del_next_workday(day: date) -> date:
    day += timedelta(days=1)
    while not _del_is_workday(day):
        day += timedelta(days=1)
    return day


def _del_prev_workday(day: date) -> date:
    day -= timedelta(days=1)
    while not _del_is_workday(day):
        day -= timedelta(days=1)
    return day


def _del_business_days(start: date, end: date) -> list[date]:
    out, day = [], start
    while day <= end:
        if _del_is_workday(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def _del_shift_days(ts: datetime, days: int) -> datetime:
    day, step = ts.date(), (1 if days >= 0 else -1)
    for _ in range(abs(days)):
        day = _del_next_workday(day) if step > 0 else _del_prev_workday(day)
    hour = min(DEL_WORK_END - 1, max(DEL_WORK_START, ts.hour))
    return datetime.combine(day, dtime(hour, ts.minute), tzinfo=DEL_UTC)


def _del_add_hours(ts: datetime, hours: float) -> datetime:
    cur, remaining = ts, float(hours)
    while remaining > 0:
        if cur.hour < DEL_WORK_START:
            cur = cur.replace(hour=DEL_WORK_START, minute=0, second=0, microsecond=0)
        end = cur.replace(hour=DEL_WORK_END, minute=0, second=0, microsecond=0)
        available = (end - cur).total_seconds() / 3600.0
        if available >= remaining:
            return cur + timedelta(hours=remaining)
        remaining -= max(0.0, available)
        cur = datetime.combine(_del_next_workday(cur.date()),
                               dtime(DEL_WORK_START), tzinfo=DEL_UTC)
    return cur


def _del_sub_hours(ts: datetime, hours: float) -> datetime:
    cur, remaining = ts, float(hours)
    while remaining > 0:
        if cur.hour > DEL_WORK_END:
            cur = cur.replace(hour=DEL_WORK_END, minute=0, second=0, microsecond=0)
        start = cur.replace(hour=DEL_WORK_START, minute=0, second=0, microsecond=0)
        available = (cur - start).total_seconds() / 3600.0
        if available >= remaining:
            return cur - timedelta(hours=remaining)
        remaining -= max(0.0, available)
        cur = datetime.combine(_del_prev_workday(cur.date()),
                               dtime(DEL_WORK_END), tzinfo=DEL_UTC)
    return cur


def _del_at(day: date, hour: float) -> datetime:
    whole = int(hour)
    minute = int(round((hour - whole) * 60))
    if minute >= 60:
        whole, minute = whole + 1, minute - 60
    return datetime.combine(day, dtime(min(DEL_WORK_END - 1, whole), minute),
                            tzinfo=DEL_UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=DEL_UTC) if value.tzinfo is None else value.astimezone(DEL_UTC)


def _synthetic_timeline() -> dict[str, dict]:
    """A deterministic PR timeline for a fixture with no store to read.

    Real builds pass the Bitbucket store's timeline (first PR open / last merge
    per ticket), so the two halves line up. This fallback keeps the generator
    usable on its own -- tests that only care about the workflow projection do
    not need a store.
    """
    out: dict[str, dict] = {}
    for index in range(100):
        spec = DEL_SPRINT_BY_INDEX.get(index)
        if spec is not None:
            day = _del_next_workday(spec["start"])
            opened = _del_at(day, 10.0 + (index % 5))
            merged_day = _del_shift_days(opened, 2 + index % 5)
            merged = _del_at(merged_day.date(), 15.0)
            if index % 10 == 3:
                merged = None
        else:
            opened = _del_at(date(2026, 10, 20), 10.0 + (index % 5))
            merged = None if index % 10 == 3 else _del_at(date(2026, 10, 27), 15.0)
        out[f"DEL-{100 + index}"] = {
            "open": opened, "merged": merged, "prs": 1 + index % 2,
            "lines": 40 + index * 3,
        }
    return out


def _del_worklog(wid: int, author: dict, started: datetime, created: datetime,
                 minutes: int, canary: str, comment: str) -> dict:
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


def _del_history(histories: list[dict], history_id: int, ts: datetime,
                 author: dict, *items: dict) -> int:
    histories.append({
        "id": str(history_id),
        "created": _jira_ts(ts),
        "author": author,
        "items": list(items),
    })
    return history_id + 1


def _del_status_item(before: str, after: str) -> dict:
    old = STATUS_BY_NAME[before]
    new = STATUS_BY_NAME[after]
    return _item("status", old["name"], new["name"], old["id"], new["id"])


def _del_issue(index: int, timeline: dict[str, dict], people: dict[str, dict],
               sprints: dict[int, dict]) -> dict:
    """One DEL story/bug, backdated onto its Bitbucket PRs and the real workflow."""
    number = 100 + index
    key = f"DEL-{number}"
    rng = random.Random(DEL_SEED + index)
    canary = "zzcanary%08x" % rng.getrandbits(32)
    area, title, kind, detail = DEL_THEMES[index % len(DEL_THEMES)]
    repo = "delivery-web" if kind == "web" else "delivery-api"
    is_bug = index in DEL_BUG_INDICES
    workflow = BUG_WORKFLOW if is_bug else STORY_WORKFLOW

    pr = timeline.get(key) or {}
    first_open = _as_utc(pr.get("open"))
    merged = _as_utc(pr.get("merged"))
    if first_open is None:  # every DEL ticket has at least one PR in the corpus
        first_open = _del_at(date(2026, 10, 1), 10.0)

    spec = DEL_SPRINT_BY_INDEX.get(index)
    sprint = sprints.get(spec["id"]) if spec else None

    # in progress: a few business hours before the first PR opens, never before
    # the sprint that carries the story.
    started = _del_sub_hours(first_open, rng.choice((1, 2, 3, 4, 6)))
    floor = None
    if spec is not None:
        floor = datetime.combine(spec["start"], dtime(9, 30), tzinfo=DEL_UTC)
        if started < floor:
            started = floor
    if started >= first_open:
        started = _del_sub_hours(first_open, 1)

    # sprint membership: pulled in 1-2 business days before work starts.
    added = None
    if spec is not None:
        added = _del_shift_days(started, -rng.choice((1, 2)))
        if added < floor:
            added = floor
        if added > started:
            added = started

    # refinement: the status flips to REFINED, then the estimate is recorded on
    # the ticket; the backlog wait is the 2-5 business days before that.
    refined = _del_shift_days(added or started, -rng.choice((1, 2)))
    refined = refined.replace(hour=rng.choice((10, 11, 13, 14, 15)),
                              minute=rng.choice((0, 15, 30, 45)))
    # the status flips to REFINED, then the estimate is recorded on the ticket
    # (Jira writes one changelog row per change).
    refined_status = refined
    points_at = refined + timedelta(minutes=20)
    created = _del_shift_days(refined, -rng.choice((2, 3, 4, 5)))
    created = created.replace(hour=rng.choice((9, 10, 11, 14, 15)),
                              minute=rng.choice((0, 20, 40)))
    if created < DEL_EARLIEST_CREATED:
        created = DEL_EARLIEST_CREATED
    if created >= refined:
        created = _del_shift_days(refined, -1)
    ready_at = min(_del_shift_days(created, 1), refined - timedelta(hours=1))

    # review starts when the PR is opened for review, QA when the last PR lands.
    in_review = _del_add_hours(first_open, rng.choice((1, 2, 3)))
    in_verification = None
    done = None
    if merged is not None:
        in_verification = _del_add_hours(merged, rng.choice((1, 2, 3, 4)))
        in_verification = max(in_verification, _del_add_hours(in_review, 1))
        done = _del_add_hours(in_verification, rng.choice((2, 4, 6, 8, 12, 16)))
        if done > DEL_HORIZON:
            done = max(in_verification + timedelta(hours=2), DEL_HORIZON)

    # rework: review -> in progress -> review, before the merge (if it merged).
    rework = rereview = None
    if merged is not None and index in DEL_REWORK_INDICES:
        candidate = _del_add_hours(in_review, rng.choice((2, 3, 4)))
        back = _del_add_hours(candidate, rng.choice((1, 2)))
        if back < in_verification:
            rework, rereview = candidate, back

    # QA bounce: verification -> in progress -> review -> verification.
    qa_back = qa_review = qa_again = None
    if merged is not None and index in DEL_QA_REWORK_INDICES:
        candidate = _del_add_hours(in_verification, rng.choice((2, 3, 4)))
        back = _del_add_hours(candidate, rng.choice((1, 2)))
        again = _del_add_hours(back, rng.choice((1, 2)))
        if again < done:
            qa_back, qa_review, qa_again = candidate, back, again

    # the final status and its resolution. An unmerged PR is still open for
    # review, so the honest final status is IN REVIEW (no verification yet).
    final_status = workflow[-1] if merged is not None else "IN REVIEW"
    final_done = done
    resolution = None
    if merged is not None:
        resolution = ({"id": "10001", "name": "Fixed"} if is_bug
                      else {"id": "10000", "name": "Done"})

    # reopen (bug RESOLVED -> TRIAGED): the final RESOLVED is the *second* one.
    reopen_at = restarted = rereview_bug = reverify = None
    if merged is not None and index in DEL_REOPEN_INDICES:
        candidate_reopen = _del_add_hours(done, rng.choice((2, 3, 4)))
        candidate_restart = _del_add_hours(candidate_reopen, rng.choice((1, 2)))
        candidate_review = _del_add_hours(candidate_restart, rng.choice((2, 3, 4)))
        candidate_verify = _del_add_hours(candidate_review, rng.choice((2, 3, 4)))
        candidate_done = _del_add_hours(candidate_verify, rng.choice((4, 6, 8)))
        # A reopen that would run past the snapshot is left out rather than
        # emitting a status chain whose timestamps go backwards.
        if candidate_done <= DEL_HORIZON:
            reopen_at, restarted = candidate_reopen, candidate_restart
            rereview_bug, reverify = candidate_review, candidate_verify
            final_done = candidate_done
            final_status = "RESOLVED"

    histories: list[dict] = []
    history_id = number * 1000
    author = people[DEL_REPORTER["name"]]
    assignee = people[DEL_AUTHORS[(index * 7 + 2) % len(DEL_AUTHORS)]["name"]]
    points = points_for_lines(pr)

    # refinement bookkeeping: the status, then the estimate.
    history_id = _del_history(
        histories, history_id, points_at, author,
        _item("Story Points", None, str(points), None, str(points)),
    )
    if is_bug:
        history_id = _del_history(histories, history_id, refined_status, assignee,
                                  _del_status_item("NEW", "TRIAGED"))
    else:
        history_id = _del_history(histories, history_id, ready_at, author,
                                  _del_status_item("OPEN", "READY FOR REFINEMENT"))
        history_id = _del_history(histories, history_id, refined_status, assignee,
                                  _del_status_item("READY FOR REFINEMENT", "REFINED"))

    # sprint pull (skipped for the at-start tickets, which Jira never recorded)
    if sprint is not None and index not in DEL_AT_START:
        history_id = _del_history(
            histories, history_id, added, author,
            _item("Sprint", None, sprint["name"], None, str(sprint["id"])),
        )

    history_id = _del_history(histories, history_id, started, assignee,
                              _del_status_item("TRIAGED" if is_bug else "REFINED",
                                               "IN PROGRESS"))
    history_id = _del_history(histories, history_id, in_review, assignee,
                              _del_status_item("IN PROGRESS", "IN REVIEW"))
    if rework is not None:
        history_id = _del_history(histories, history_id, rework, assignee,
                                  _del_status_item("IN REVIEW", "IN PROGRESS"))
        history_id = _del_history(histories, history_id, rereview, assignee,
                                  _del_status_item("IN PROGRESS", "IN REVIEW"))
    if in_verification is not None:
        history_id = _del_history(histories, history_id, in_verification, assignee,
                                  _del_status_item("IN REVIEW", "IN VERIFICATION"))
        if qa_back is not None:
            history_id = _del_history(histories, history_id, qa_back, assignee,
                                      _del_status_item("IN VERIFICATION", "IN PROGRESS"))
            history_id = _del_history(histories, history_id, qa_review, assignee,
                                      _del_status_item("IN PROGRESS", "IN REVIEW"))
            history_id = _del_history(histories, history_id, qa_again, assignee,
                                      _del_status_item("IN REVIEW", "IN VERIFICATION"))
        if done is not None:
            history_id = _del_history(
                histories, history_id, done, assignee,
                _del_status_item("IN VERIFICATION", workflow[-1]),
                _item("resolution", None, resolution["name"], None, resolution["id"]),
            )
    if reopen_at is not None:
        history_id = _del_history(histories, history_id, reopen_at, assignee,
                                  _del_status_item("RESOLVED", "TRIAGED"))
        history_id = _del_history(histories, history_id, restarted, assignee,
                                  _del_status_item("TRIAGED", "IN PROGRESS"))
        history_id = _del_history(histories, history_id, rereview_bug, assignee,
                                  _del_status_item("IN PROGRESS", "IN REVIEW"))
        history_id = _del_history(histories, history_id, reverify, assignee,
                                  _del_status_item("IN REVIEW", "IN VERIFICATION"))
        history_id = _del_history(histories, history_id, final_done, assignee,
                                  _del_status_item("IN VERIFICATION", "RESOLVED"))

    # one mid-flight reassignment (the seed's PROJ-107 equivalent)
    final_assignee = assignee
    if index == 7:
        other = people[DEL_AUTHORS[(index * 7 + 9) % len(DEL_AUTHORS)]["name"]]
        changed = min(_del_add_hours(started, 12), (final_done or DEL_HORIZON) - timedelta(hours=1))
        history_id = _del_history(
            histories, history_id, changed, author,
            _item("assignee", assignee["displayName"], other["displayName"],
                  assignee["name"], other["name"]),
        )
        final_assignee = other

    # a couple of "Epic Child" bookkeeping items: read and dropped by the reducer
    if index in (0, 1):
        history_id = _del_history(histories, history_id, created + timedelta(minutes=5),
                                  author, _item("Epic Child", None, key))

    # worklogs: 1-3 per issue (199 in total, like the lab), inside the active
    # window, logged same/next day with a deterministic handful late.
    worklogs = []
    count = 1 + index % 3
    window_end = final_done or DEL_HORIZON
    days = _del_business_days(started.date(), min(window_end.date(), DEL_WORKLOG_CUTOFF.date()))
    if not days:
        days = [started.date()]
    for j in range(count):
        day = days[min(len(days) - 1, (index * 3 + j * 7) % len(days))]
        when = _del_at(day, 9.5 + rng.uniform(0, 6.5))
        if when < started:
            when = started + timedelta(minutes=30)
        if when >= window_end:
            when = max(started, window_end - timedelta(minutes=30))
        log_author = final_assignee
        if j == 2 and index % 4 == 0:
            others = [user for user in DEL_AUTHORS
                      if user["name"] != final_assignee["name"]]
            log_author = people[others[index % len(others)]["name"]]
        delay = 8 if (index % 20 == 7 and j == 0) else rng.choice((0, 0, 1, 1, 1, 2))
        logged = when if delay == 0 else _del_at(
            _del_shift_days(when, delay).date(), 9.5 + rng.uniform(0, 6.5))
        if logged < when:
            logged = when
        if logged > DEL_HORIZON:
            logged = DEL_HORIZON
        minutes = rng.choice((30, 60, 90, 120, 180, 240))
        worklogs.append(_del_worklog(
            number * 10 + j + 1, log_author, when, logged, minutes, canary,
            DEL_WORKLOG_COMMENTS[(index + j) % len(DEL_WORKLOG_COMMENTS)],
        ))

    points = points_for_lines(pr)
    stamps = [created, refined, refined_status, points_at, started, in_review]
    stamps += [value for value in (added, rework, rereview, in_verification,
                                   qa_back, qa_review, qa_again, done,
                                   reopen_at, final_done) if value]
    stamps += [_at(item["created"]) for item in histories]
    stamps += [_at(item["created"]) for item in worklogs]
    updated = min(max(stamps), DEL_HORIZON)

    fields = {
        "summary": f"{title} {canary}",
        "description": (
            f"Epic: {DEL_EPIC_KEY}\n\nTicket: {key}\n\n{detail}\n\n"
            f"Area: {area}\nRepo: {repo}\n\nzzcanary {canary}"
        ),
        "issuetype": ({"id": "10002", "name": "Bug", "subtask": False} if is_bug
                      else {"id": "10001", "name": "Story", "subtask": False}),
        "status": STATUS_BY_NAME[final_status],
        "resolution": resolution,
        "resolutiondate": _jira_ts(final_done) if final_done else None,
        "assignee": final_assignee,
        "reporter": author,
        "creator": author,
        "created": _jira_ts(created),
        "updated": _jira_ts(updated),
        "labels": [area, f"repo-{repo}"],
        "customfield_10100": DEL_EPIC_KEY,
        "customfield_10106": points,
        "customfield_10104": [_sprint_field(sprint)] if sprint else None,
        "worklog": {
            "startAt": 0,
            "maxResults": 20,
            "total": len(worklogs),
            "worklogs": worklogs,
        },
    }
    histories.sort(key=lambda history: history["created"])
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


def points_for_lines(pr: dict) -> int:
    """Fibonacci-like points from the story's changed lines (backdate's caps)."""
    lines = int((pr or {}).get("lines") or 0)
    for cap, points in ((40, 1), (80, 2), (150, 3), (250, 5), (400, 8)):
        if lines <= cap:
            return points
    return 13


def _del_epic(people: dict[str, dict]) -> dict:
    rng = random.Random(DEL_SEED + 999)
    canary = "zzcanary%08x" % rng.getrandbits(32)
    author = people[DEL_REPORTER["name"]]
    histories = [
        {
            "id": "9001",
            "created": _jira_ts(DEL_EPIC_CREATED + timedelta(minutes=5)),
            "author": author,
            "items": [_item("Epic Child", None, "DEL-100")],
        },
        {
            "id": "9002",
            "created": _jira_ts(datetime(2026, 8, 17, 9, 30, tzinfo=DEL_UTC)),
            "author": author,
            "items": [_del_status_item("OPEN", "IN PROGRESS")],
        },
    ]
    return {
        "id": "10000",
        "key": DEL_EPIC_KEY,
        "self": "http://jira.example.invalid/rest/api/2/issue/10000",
        "fields": {
            "summary": f"{DEL_EPIC_SUMMARY} {canary}",
            "description": f"Epic covering the delivery platform. zzcanary {canary}",
            "issuetype": {"id": "10000", "name": "Epic", "subtask": False},
            "status": STATUS_BY_NAME["IN PROGRESS"],
            "resolution": None,
            "resolutiondate": None,
            "assignee": author,
            "reporter": author,
            "creator": author,
            "created": _jira_ts(DEL_EPIC_CREATED),
            "updated": _jira_ts(datetime(2026, 8, 17, 9, 30, tzinfo=DEL_UTC)),
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


def generate_del(timeline: dict[str, dict] | None = None, *,
                 seed: int = DEL_SEED) -> dict:
    """The DEL corpus on the install's real Story + Bug workflows.

    ``timeline`` maps ``DEL-1xx`` -> ``{"open": datetime, "merged": datetime|None,
    "prs": int, "lines": int}``, normally read from the Bitbucket store by
    ``jira-lab/rebuild_combined.py``. Every ticket is backdated onto its PRs:
    created in the backlog, refined, pulled into its sprint, started just before
    the first PR opens, reviewed, verified after the last merge and closed after
    a measurable QA wait. Without a timeline a deterministic synthetic one is
    used (the workflow projection does not depend on the PR half).
    """
    timeline = timeline or _synthetic_timeline()
    people = _del_people()
    sprints = {spec["id"]: _del_sprint_record(spec) for spec in DEL_SPRINTS}
    issues = [_del_epic(people)]
    issues += [_del_issue(index, timeline, people, sprints) for index in range(100)]
    sprint_issues: dict[str, list[str]] = {str(spec["id"]): [] for spec in DEL_SPRINTS}
    for issue in issues:
        sid = sprint_id_of(issue)
        if sid is not None:
            sprint_issues[str(sid)].append(issue["key"])
    return {
        "schema": "megaspeed.jira-fixture/1",
        "note": ("Synthetic Jira Data Center payloads for the DEL corpus on the "
                 "install's actual Story + Bug workflows. No real people or text."),
        "seed": seed,
        "generated": _jira_ts(DEL_HORIZON),
        "project": DEL_PROJECT,
        "workflow": {
            "story": list(STORY_WORKFLOW),
            "bug": list(BUG_WORKFLOW),
            "transitions": WORKFLOW_TRANSITIONS,
            "stages": dict(WORKFLOW_STAGE_MAP),
        },
        "fields": FIELDS,
        "statuses": WORKFLOW_STATUSES,
        "boards": [
            {
                "id": 1,
                "self": "http://jira.example.invalid/rest/agile/1.0/board/1",
                "name": f"{DEL_PROJECT} board",
                "type": "scrum",
            }
        ],
        "sprints": [sprints[spec["id"]] for spec in DEL_SPRINTS],
        "sprint_issues": sprint_issues,
        "users": [_del_user(user["name"]) for user in DEL_USERS],
        "issues": issues,
    }


def write_del_bundle(path: Path, timeline: dict[str, dict] | None = None,
                     *, seed: int = DEL_SEED) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(generate_del(timeline, seed=seed), indent=1) + "\n")
    return path


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


# --------------------------------------------------------------------------- #
# Tempo (prd/30)
# --------------------------------------------------------------------------- #
#: Tempo Server/DC's worklog route. The lab cannot install Tempo (the Marketplace
#: artifact is empty and the add-on needs the org's licence), so fixture mode
#: serves Tempo's *shape* over the same worklog records.
_TEMPO_WORKLOGS = re.compile(r"^" + TEMPO + r"/worklogs/(?P<key>[^/]+)$")


def _tempo_id(value):
    """Tempo ids are integers; the native fixture carries them as strings."""
    text = "" if value is None else str(value)
    return int(text) if text.isdigit() else value


def tempo_worklog_rows(worklogs: list[dict], key: str) -> list[dict]:
    """Native DC worklogs -> the Tempo Server/DC worklog payload (prd/30).

    Tempo renames the fields the reducer reads: ``id`` -> ``worklogId`` and
    ``created``/``updated`` -> ``dateCreated``/``dateUpdated``. It also carries
    the raw ``comment``, which the ingest must never store (prd/02).
    """
    rows: list[dict] = []
    for worklog in worklogs:
        rows.append(
            {
                "self": f"http://jira.example.invalid{TEMPO}/worklogs/{worklog.get('id')}",
                "worklogId": _tempo_id(worklog.get("id")),
                "issueId": _tempo_id(worklog.get("issueId")),
                "issue": {"key": key, "id": _tempo_id(worklog.get("issueId"))},
                "author": dict(worklog.get("author") or {}),
                "started": worklog.get("started"),
                "dateCreated": worklog.get("created"),
                "dateUpdated": worklog.get("updated"),
                "timeSpentSeconds": worklog.get("timeSpentSeconds"),
                "startedDate": str(worklog.get("started") or "")[:10],
                "comment": worklog.get("comment"),
            }
        )
    return rows


class TempoFixtureTransport(FixtureJiraTransport):
    """The fixture transport plus Tempo Server/DC's worklog route (prd/30).

    Everything else is inherited unchanged: ``--jira --fixture --tempo`` reads
    the same issues and serves their worklogs through Tempo's shape, so the
    reduced events can be compared against the native path record by record.
    """

    def get(self, path: str, params: dict | None = None) -> dict:
        match = _TEMPO_WORKLOGS.match(path)
        if match:
            key = match["key"]
            issue = self._issues.get(key)
            if issue is None:
                raise BitbucketError(f"fixture has no issue {key}")
            block = (issue.get("fields") or {}).get("worklog") or {}
            return tempo_worklog_rows(list(block.get("worklogs") or []), key)
        return super().get(path, params)


if __name__ == "__main__":  # python3 -m ingest.jira_fixtures [--n 12] [--out PATH]
    import argparse

    ap = argparse.ArgumentParser(description="write the synthetic Jira fixture bundle")
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument(
        "--del", dest="del_corpus", action="store_true",
        help="write the DEL corpus on the install's actual Story + Bug workflows",
    )
    ap.add_argument(
        "--timeline-store", default=None, metavar="STORE.duckdb",
        help="--del: read each ticket's first PR open / last merge from this store",
    )
    ap.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent.parent / "fixtures" / "jira-dc-fixture.json"),
    )
    args = ap.parse_args()
    if args.del_corpus:
        timeline = None
        if args.timeline_store:
            # Lazy: the scrape side never imports DuckDB (prd/11).
            import duckdb

            con = duckdb.connect(str(args.timeline_store), read_only=True)
            try:
                rows = con.execute(
                    "SELECT ticket_keys, state, created_at, closed_at,"
                    " CAST(attributes AS VARCHAR) FROM objects WHERE object_type = 'PR'"
                ).fetchall()
            finally:
                con.close()
            timeline = {}
            for keys, state, created, closed, attributes in rows:
                try:
                    attrs = json.loads(attributes) if attributes else {}
                except (TypeError, json.JSONDecodeError):
                    attrs = {}
                lines = int(attrs.get("lines_added") or 0) + int(attrs.get("lines_removed") or 0)
                for key in keys or ():
                    if not str(key).startswith("DEL-") or str(key) == DEL_EPIC_KEY:
                        continue
                    entry = timeline.setdefault(
                        str(key), {"open": None, "merged": None, "prs": 0, "lines": 0})
                    entry["prs"] += 1
                    entry["lines"] += lines
                    if created and (entry["open"] is None or created < entry["open"]):
                        entry["open"] = created
                    if state == "merged" and closed and (
                            entry["merged"] is None or closed > entry["merged"]):
                        entry["merged"] = closed
        print(write_del_bundle(Path(args.out), timeline, seed=args.seed or DEL_SEED))
    else:
        print(write_bundle(Path(args.out), n=args.n, seed=args.seed or SEED))

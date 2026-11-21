"""Sprint-at-risk (prd/52): the active sprint's not-delivered stories.

The list reuses the done-by-end machinery (prd/37) and the "Done in Jira, PR
open" / holder facts (prd/34, prd/42 M4). The fixture has one story per risk
class, so every classification is pinned to a hand-computed timestamp:

* ENG-1 — Jira done, PR open with two pending reviewers: done-but-unmerged;
* ENG-2 — open, one reviewer still owing: stuck-on that person;
* ENG-3 — open, both reviewers approved but the PR was declined:
  approved-unmerged (the DEL-161 orphan);
* ENG-4 — open with no PR at all: no-live-pr;
* ENG-5 — done and merged: delivered, not at risk.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from analysis import ballcourt, jira, rules
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from server import app as app_module

REPO = "DEL/api"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
SPRINT_START = datetime(2026, 1, 19, 9, 0)
AS_OF = datetime(2026, 2, 20, 12, 0)
# a PR opened Mon 20 Jan 10:00, and the active sprint's snapshot
PR_OPEN = datetime(2026, 1, 20, 10, 0)


class Fixture:
    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self.opened: dict[tuple[str, int], datetime] = {}
        self._count = 0

    def _object(self, object_type: str, object_id: str, **kwargs) -> None:
        record = {
            "object_type": object_type,
            "object_id": object_id,
            "repo": None,
            "pr_id": None,
            "ticket_keys": [],
            "actor": None,
            "state": None,
            "created_at": None,
            "closed_at": None,
            "head_commit": None,
            "message_class": None,
            "attributes": {},
        }
        record.update(kwargs)
        self.objects.append(record)

    def sprint(self, sid: int, start: datetime, planned_end: str,
               state: str = "active", closed: datetime | None = None) -> str:
        object_id = f"SPRINT:DEL#{sid}"
        self._object(
            "SPRINT", object_id, state=state, created_at=start, closed_at=closed,
            attributes={"goal_class": "feature", "end_date": planned_end},
        )
        return object_id

    def story(self, key: str, created: datetime, points: int = 3,
              state: str = "open", closed: datetime | None = None,
              assignee: str = "AaAa") -> str:
        object_id = f"STORY:{key}"
        self._object(
            "STORY", object_id, ticket_keys=[key], actor=assignee, state=state,
            created_at=created, closed_at=closed,
            attributes={
                "summary_class": "feature",
                "epic_key": "DEL-1",
                "points": points,
            },
        )
        self.event(key, created, "created", actor=assignee)
        return object_id

    def event(self, key: str, ts: datetime, activity: str, actor: str = "AaAa",
              objects: list[str] | None = None, **attributes) -> None:
        self._count += 1
        event_key = f"{key}:e{self._count}"
        self.events.append({
            "event_key": event_key,
            "ts": ts,
            "activity": activity,
            "actor": actor,
            "repo": None,
            "pr_id": None,
            "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(event_key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        for object_id in objects or [f"STORY:{key}"]:
            self.relations.append((event_key, object_id.split(":", 1)[0], object_id))

    def status(self, key: str, ts: datetime, to_status: str,
               from_status: str) -> None:
        self.event(key, ts, "status_changed",
                   **{"from_status": from_status, "to_status": to_status})

    def add_to_sprint(self, key: str, ts: datetime, sid: int) -> None:
        self.event(
            key, ts, "sprint_added",
            objects=[f"STORY:{key}", f"SPRINT:DEL#{sid}"],
            sprint_id=sid, at_start=True,
        )

    def pr(self, number: int, author: str, opened: datetime,
           tickets: tuple[str, ...], state: str = "open",
           closed: datetime | None = None) -> None:
        self.opened[(REPO, number)] = opened
        self._object(
            "PR", f"PR:{REPO}#{number}", repo=REPO, pr_id=number,
            ticket_keys=list(tickets), actor=author, state=state,
            created_at=opened, closed_at=closed, head_commit="a" * 40,
        )
        self.pr_event(number, "pr_opened", author, 0)

    def pr_event(self, number: int, activity: str, actor: str, hours: float,
                 people: tuple[str, ...] = (), **attributes) -> None:
        self._count += 1
        event_key = f"{REPO}#{number}:{self._count}"
        self.events.append({
            "event_key": event_key,
            "ts": self.opened[(REPO, number)] + timedelta(hours=hours),
            "activity": activity,
            "actor": actor,
            "repo": REPO,
            "pr_id": number,
            "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(event_key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        self.relations.append((event_key, "PR", f"PR:{REPO}#{number}"))
        for person in people:
            self.relations.append((event_key, "PERSON", f"PERSON:{person}"))

    def reviewers(self, number: int, people: tuple[str, ...], hours: float = 0.0) -> None:
        for person in people:
            self.pr_event(number, "reviewer_changed", "AaAa", hours,
                          people=(person,), change="added")

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-sprint-at-risk",
                "source": "fixture",
                "as_of": self.as_of,
                "source_watermark": self.as_of,
                "classifier_version": "deterministic-v1",
                "prs_seen": len(self.opened),
                "prs_kept": len(self.opened),
                "prs_dropped": 0,
                "events_written": len(self.events),
                "events_skipped": 0,
                "issues_seen": sum(
                    1 for obj in self.objects if obj["object_type"] == "STORY"
                ),
                "issues_kept": sum(
                    1 for obj in self.objects if obj["object_type"] == "STORY"
                ),
                "issues_skipped": 0,
            })
        finally:
            con.close()
        return path


def done_story(fx: Fixture, key: str, done_at: datetime, points: int = 3) -> None:
    fx.story(key, T0, points=points, state="done", closed=done_at)
    fx.add_to_sprint(key, T0 + timedelta(hours=1), 3)
    fx.status(key, T0 + timedelta(hours=2), "in_progress", "ready")
    fx.status(key, done_at, "done", "in_progress")


def open_story(fx: Fixture, key: str, points: int = 3) -> None:
    fx.story(key, T0, points=points, state="open")
    fx.add_to_sprint(key, T0 + timedelta(hours=1), 3)
    fx.status(key, T0 + timedelta(hours=2), "in_progress", "ready")


def fixture() -> Fixture:
    fx = Fixture()
    # sprint 3 is the one in flight (planned to Feb 16; the band stops at the
    # Feb 20 snapshot)
    fx.sprint(3, SPRINT_START, "2026-02-16", state="active")

    # ENG-1: Jira done, PR #1 open with both reviewers still pending
    done_story(fx, "ENG-1", datetime(2026, 2, 2, 12, 0), points=5)
    fx.pr(1, "AaAa", PR_OPEN, ("ENG-1",))
    fx.reviewers(1, ("RrRr", "SsSs"))

    # ENG-2: open, PR #2 open — RrRr approved, SsSs still owes
    open_story(fx, "ENG-2", points=3)
    fx.pr(2, "AaAa", PR_OPEN, ("ENG-2",))
    fx.reviewers(2, ("RrRr", "SsSs"))
    fx.pr_event(2, "pr_approved", "RrRr", 2)

    # ENG-3: open, both reviewers approved, the PR declined — the orphan
    open_story(fx, "ENG-3", points=2)
    fx.pr(3, "AaAa", PR_OPEN, ("ENG-3",),
          state="declined", closed=PR_OPEN + timedelta(hours=30))
    fx.reviewers(3, ("RrRr", "SsSs"))
    fx.pr_event(3, "pr_approved", "RrRr", 2)
    fx.pr_event(3, "pr_approved", "SsSs", 3, sentinel=True)
    fx.pr_event(3, "pr_declined", "BbBb", 30)

    # ENG-4: open with no PR at all
    open_story(fx, "ENG-4", points=1)

    # ENG-5: done and merged — delivered, never at risk
    done_story(fx, "ENG-5", datetime(2026, 2, 3, 12, 0), points=5)
    fx.pr(5, "AaAa", PR_OPEN, ("ENG-5",),
          state="merged", closed=PR_OPEN + timedelta(hours=4))
    fx.reviewers(5, ("RrRr", "SsSs"))
    fx.pr_event(5, "pr_approved", "RrRr", 2)
    fx.pr_event(5, "pr_approved", "SsSs", 3, sentinel=True)
    fx.pr_event(5, "pr_merged", "AaAa", 4)
    return fx


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def view(self, fx: Fixture | None = None):
        (fx or fixture()).write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            data = jira.load(con, court)
            window = rules.window_for(court, None, data)
            return jira.sprints(court, data, window), court
        finally:
            con.close()


def row(payload: dict, key: str) -> dict:
    return next(row for row in payload["at_risk"]["rows"] if row["key"] == key)


class AtRiskTests(StoreCase):
    def test_the_five_not_delivered_stories_are_the_four_risk_classes(self):
        payload, _court = self.view()
        risk = payload["at_risk"]
        self.assertTrue(risk["available"])
        self.assertEqual(risk["sprint_id"], 3)
        self.assertEqual(risk["count"], 4)
        self.assertEqual(risk["stories"], 5)
        self.assertEqual(risk["done_by_end"], 1)
        self.assertEqual(risk["points_at_risk"], 11)   # 5 + 3 + 2 + 1
        self.assertEqual(
            [item["key"] for item in risk["rows"]],
            ["ENG-1", "ENG-2", "ENG-3", "ENG-4"],
        )
        self.assertEqual(
            [item["reason"] for item in risk["rows"]],
            ["done-but-unmerged", "stuck-on", "approved-unmerged", "no-live-pr"],
        )
        # the delivered, merged story is not at risk
        self.assertNotIn("ENG-5", {item["key"] for item in risk["rows"]})

    def test_done_but_unmerged_names_the_open_pr_and_its_holders(self):
        payload, court = self.view()
        item = row(payload, "ENG-1")
        self.assertEqual(item["reason"], "done-but-unmerged")
        self.assertEqual(item["who"], ["RrRr", "SsSs"])
        self.assertEqual(item["detail"]["prs"], [1])
        # the wait opens when the reviewers received the ball (the add events)
        # and runs to the snapshot — wall-clock here (no calendar)
        expected = round(
            (court.snapshot - PR_OPEN).total_seconds() / 3600.0, 2
        )
        self.assertEqual(item["waiting_hours"], expected)
        self.assertIsNotNone(item["detail"]["done_at"])

    def test_stuck_on_names_the_one_person_who_owes(self):
        payload, court = self.view()
        item = row(payload, "ENG-2")
        self.assertEqual(item["reason"], "stuck-on")
        self.assertEqual(item["who"], ["SsSs"])
        # the approval at +2 h handed the ball to the remaining reviewer
        expected = round(
            (court.snapshot - (PR_OPEN + timedelta(hours=2))).total_seconds() / 3600.0,
            2,
        )
        self.assertEqual(item["waiting_hours"], expected)

    def test_approved_but_never_merged_is_the_orphan(self):
        payload, court = self.view()
        item = row(payload, "ENG-3")
        self.assertEqual(item["reason"], "approved-unmerged")
        self.assertEqual(item["who"], ["AaAa"])        # the merge click / a new PR
        self.assertEqual(item["detail"]["approved_by"], ["RrRr", "SsSs"])
        self.assertEqual(item["detail"]["state"], "declined")
        self.assertEqual(item["detail"]["closed_by"], "BbBb")
        self.assertEqual(item["detail"]["bot_closed"], False)
        # final approval +3 h → the decline +30 h = 27 h
        self.assertEqual(item["waiting_hours"], 27.0)

    def test_no_live_pr_names_the_assignee(self):
        payload, _court = self.view()
        item = row(payload, "ENG-4")
        self.assertEqual(item["reason"], "no-live-pr")
        self.assertEqual(item["who"], ["AaAa"])
        self.assertIsNone(item["waiting_hours"])
        self.assertEqual(item["detail"]["prs"], [])

    def test_the_bands_own_count_matches_the_at_risk_list(self):
        payload, _court = self.view()
        band = next(
            row for row in payload["sprints"] if row["sprint_id"] == 3
        )
        # 30-story sprint shape: done + unmerged + open == the committed scope
        self.assertEqual(band["done_by_end"], 1)
        self.assertEqual(band["unmerged"], 1)
        self.assertEqual(band["never_done"], 3)
        self.assertEqual(
            band["done_by_end"] + band["unmerged"] + band["never_done"],
            band["story_count"],
        )
        self.assertEqual(
            payload["at_risk"]["count"], band["unmerged"] + band["never_done"]
        )


class NoActiveSprintTests(StoreCase):
    def test_a_closed_sprint_has_a_verdict_not_a_risk_list(self):
        fx = fixture()
        # turn the active sprint into a closed one
        for obj in fx.objects:
            if obj["object_type"] == "SPRINT":
                obj["state"] = "closed"
                obj["closed_at"] = AS_OF - timedelta(days=1)
        payload, _court = self.view(fx)
        self.assertIsNone(payload["active"])
        self.assertFalse(payload["at_risk"]["available"])
        self.assertIn("no active sprint", payload["at_risk"]["note"])


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #
class Reply:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode())


async def _call_asgi(app, path: str, query: str) -> Reply:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return Reply(start["status"], headers, body)


def get(path: str, **query: object) -> Reply:
    return asyncio.run(_call_asgi(app_module.app, path, urlencode(query)))


class AtRiskApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        fixture().write(cls.dir / "del.duckdb")
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            # same wall-clock prices as the direct recompute above
            mock.patch.object(
                app_module, "CALENDAR_CONFIG", cls.dir / "no-calendar.yaml"
            ),
            mock.patch.object(app_module, "LOG", False),
        ]
        for patch in cls.env:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.env):
            patch.stop()
        cls.tmp.cleanup()

    def setUp(self):
        app_module._analysis_cache = None

    def test_sprints_carry_the_at_risk_list(self):
        reply = get("/api/sprints", tenant="del", days=3650)
        self.assertEqual(reply.status, 200)
        body = reply.json()
        risk = body["at_risk"]
        self.assertTrue(risk["available"])
        self.assertEqual(risk["sprint_id"], 3)
        self.assertEqual(risk["count"], 4)
        self.assertEqual(
            {item["key"]: item["reason"] for item in risk["rows"]},
            {
                "ENG-1": "done-but-unmerged",
                "ENG-2": "stuck-on",
                "ENG-3": "approved-unmerged",
                "ENG-4": "no-live-pr",
            },
        )

    def test_the_active_sprint_flag_is_the_same_sprint(self):
        body = get("/api/sprints", tenant="del", days=3650).json()
        self.assertEqual(body["active"], body["at_risk"]["sprint_id"])


if __name__ == "__main__":
    unittest.main()

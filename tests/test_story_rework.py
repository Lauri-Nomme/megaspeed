"""Story-level rework (prd/51): reopened, failed verification, bounces, orphans.

The panel lists what the PR-level ``rework_loops`` rule cannot: the stories
that came back after Done, the ones QA sent back, the ones review bounced, and
the approved-but-never-merged orphan (DEL-161 in the QA store). Every number is
pinned against a synthetic store with hand-computed timestamps, and the API
half proves the panel actually rides ``/api/insights``.

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

REPO = "DEL/web"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = datetime(2026, 2, 2, 9, 0)


class Fixture:
    """A store with explicit status histories and PR vectors."""

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

    def pr(self, number: int, author: str, opened: datetime, tickets: tuple[str, ...],
           state: str = "open", closed: datetime | None = None) -> None:
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

    def reviewer(self, number: int, person: str, hours: float) -> None:
        self.pr_event(number, "reviewer_changed", "AaAa", hours,
                      people=(person,), change="added")

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-story-rework",
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


def fixture() -> Fixture:
    """Four rework signals, one of each, plus an ordinary story.

    * ENG-1 reopened: first done +10 h → final done +40 h = **30 h**;
    * ENG-2 failed verification: flip +12 h → back in verification +16 h = **4 h**;
    * ENG-3 review bounce: flip +12 h → back in review +18 h = **6 h**;
    * ENG-4 approved but never merged: final approval +12 h → declined +30 h =
      **18 h** (the DEL-161 orphan);
    * ENG-5 ordinary: done with a merged PR, never listed.
    """
    fx = Fixture()

    fx.story("ENG-1", T0, points=5, state="done", closed=T0 + timedelta(hours=40))
    fx.status("ENG-1", T0 + timedelta(hours=1), "ready", "backlog")
    fx.status("ENG-1", T0 + timedelta(hours=2), "in_progress", "ready")
    fx.status("ENG-1", T0 + timedelta(hours=10), "done", "in_progress")
    fx.status("ENG-1", T0 + timedelta(hours=20), "ready", "done")
    fx.status("ENG-1", T0 + timedelta(hours=21), "in_progress", "ready")
    fx.status("ENG-1", T0 + timedelta(hours=40), "done", "in_progress")

    fx.story("ENG-2", T0, points=3, state="done", closed=T0 + timedelta(hours=20))
    fx.status("ENG-2", T0 + timedelta(hours=1), "ready", "backlog")
    fx.status("ENG-2", T0 + timedelta(hours=2), "in_progress", "ready")
    fx.status("ENG-2", T0 + timedelta(hours=3), "in_review", "in_progress")
    fx.status("ENG-2", T0 + timedelta(hours=10), "in_verification", "in_review")
    fx.status("ENG-2", T0 + timedelta(hours=12), "in_progress", "in_verification")
    fx.status("ENG-2", T0 + timedelta(hours=14), "in_review", "in_progress")
    fx.status("ENG-2", T0 + timedelta(hours=16), "in_verification", "in_review")
    fx.status("ENG-2", T0 + timedelta(hours=20), "done", "in_verification")

    fx.story("ENG-3", T0, points=3, state="done", closed=T0 + timedelta(hours=22))
    fx.status("ENG-3", T0 + timedelta(hours=1), "ready", "backlog")
    fx.status("ENG-3", T0 + timedelta(hours=2), "in_progress", "ready")
    fx.status("ENG-3", T0 + timedelta(hours=10), "in_review", "in_progress")
    fx.status("ENG-3", T0 + timedelta(hours=12), "in_progress", "in_review")
    fx.status("ENG-3", T0 + timedelta(hours=18), "in_review", "in_progress")
    fx.status("ENG-3", T0 + timedelta(hours=20), "in_verification", "in_review")
    fx.status("ENG-3", T0 + timedelta(hours=22), "done", "in_verification")

    fx.story("ENG-4", T0, points=2)
    fx.status("ENG-4", T0 + timedelta(hours=1), "ready", "backlog")
    fx.status("ENG-4", T0 + timedelta(hours=2), "in_progress", "ready")
    fx.status("ENG-4", T0 + timedelta(hours=3), "in_review", "in_progress")
    fx.status("ENG-4", T0 + timedelta(hours=4), "in_verification", "in_review")
    fx.pr(7, "AaAa", T0 + timedelta(hours=5), ("ENG-4",),
          state="declined", closed=T0 + timedelta(hours=35))
    fx.reviewer(7, "RrRr", 5)
    fx.reviewer(7, "SsSs", 5)
    fx.pr_event(7, "pr_approved", "RrRr", 10)
    fx.pr_event(7, "pr_approved", "SsSs", 12, sentinel=True)
    fx.pr_event(7, "pr_declined", "BbBb", 30)

    fx.story("ENG-5", T0, points=1, state="done", closed=T0 + timedelta(hours=9))
    fx.status("ENG-5", T0 + timedelta(hours=1), "ready", "backlog")
    fx.status("ENG-5", T0 + timedelta(hours=2), "in_progress", "ready")
    fx.status("ENG-5", T0 + timedelta(hours=9), "done", "in_progress")
    fx.pr(8, "AaAa", T0 + timedelta(hours=3), ("ENG-5",),
          state="merged", closed=T0 + timedelta(hours=8))
    fx.reviewer(8, "RrRr", 3)
    fx.reviewer(8, "SsSs", 3)
    fx.pr_event(8, "pr_approved", "RrRr", 4)
    fx.pr_event(8, "pr_approved", "SsSs", 5, sentinel=True)
    fx.pr_event(8, "pr_merged", "AaAa", 5)
    return fx


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fx: Fixture | None = None):
        (fx or fixture()).write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            data = jira.load(con, court)
            return con, court, data
        finally:
            con.close()

    def rework(self, fx: Fixture | None = None):
        con, court, data = self.load(fx)
        return jira.rework(court, data)


class ReworkTests(StoreCase):
    def test_reopened_counts_the_flip_and_prices_first_to_final_done(self):
        payload = self.rework()
        group = payload["reopened"]
        self.assertEqual(group["count"], 1)
        self.assertEqual(group["stories"], 1)
        row = group["rows"][0]
        self.assertEqual(row["key"], "ENG-1")
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["hours"], 30.0)       # first done +10 → final +40
        self.assertFalse(row["open"])

    def test_failed_verification_prices_the_loop_back_to_qa(self):
        group = self.rework()["failed_verification"]
        self.assertEqual(group["count"], 1)
        self.assertEqual(group["rows"][0]["key"], "ENG-2")
        self.assertEqual(group["rows"][0]["hours"], 4.0)   # +12 → +16
        self.assertEqual(group["hours"], 4.0)

    def test_review_bounces_price_the_loop_back_to_review(self):
        group = self.rework()["review_bounces"]
        self.assertEqual(group["count"], 1)
        self.assertEqual(group["rows"][0]["key"], "ENG-3")
        self.assertEqual(group["rows"][0]["hours"], 6.0)   # +12 → +18

    def test_approved_but_never_merged_is_the_orphan_bucket(self):
        group = self.rework()["approved_unmerged"]
        self.assertEqual(group["count"], 1)
        self.assertEqual(group["stories"], 1)
        row = group["rows"][0]
        self.assertEqual(row["key"], "ENG-4")
        # the DEL-161 shape: both reviewers approved, the PR was declined
        self.assertEqual(row["approved_by"], ["RrRr", "SsSs"])
        self.assertEqual(row["state"], "declined")
        self.assertEqual(row["closed_by"], "BbBb")
        self.assertEqual(row["hours"], 18.0)       # approval +12 → declined +30
        self.assertEqual(row["waiting_on"], ["AaAa"])

    def test_the_headline_sums_the_four_groups(self):
        payload = self.rework()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["count"], 4)
        self.assertEqual(payload["hours"], 58.0)   # 30 + 4 + 6 + 18
        self.assertEqual(
            [payload[key]["count"] for key in (
                "reopened", "failed_verification", "review_bounces",
                "approved_unmerged",
            )],
            [1, 1, 1, 1],
        )
        # the scope is the whole store, not the Rules window
        self.assertIn("every story in the store", payload["scope"])

    def test_a_normal_merged_story_is_not_rework(self):
        keys = {
            row["key"]
            for group in ("reopened", "failed_verification", "review_bounces")
            for row in self.rework()[group]["rows"]
        }
        self.assertNotIn("ENG-5", keys)


class NoJiraTests(StoreCase):
    def test_a_store_without_the_ticket_layer_says_so(self):
        fx = Fixture()
        fx.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            payload = jira.rework(court, jira.load(con, court))
        finally:
            con.close()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["count"], 0)
        self.assertIn("no STORY objects", payload["note"])


# --------------------------------------------------------------------------- #
# the API: the panel rides /api/insights
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


class ReworkApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        fixture().write(cls.dir / "del.duckdb")
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            # the direct tests above price wall-clock; make the API answer on
            # the same clock so "matching a recompute" is one number
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

    def test_insights_carry_the_rework_panel(self):
        reply = get("/api/insights", tenant="del", days=3650)
        self.assertEqual(reply.status, 200)
        payload = reply.json()
        rework = payload["rework"]
        self.assertTrue(rework["available"])
        self.assertEqual(rework["count"], 4)
        self.assertEqual(rework["hours"], 58.0)
        self.assertEqual(rework["reopened"]["rows"][0]["key"], "ENG-1")
        self.assertEqual(rework["failed_verification"]["rows"][0]["key"], "ENG-2")
        self.assertEqual(rework["review_bounces"]["rows"][0]["key"], "ENG-3")
        self.assertEqual(rework["approved_unmerged"]["rows"][0]["key"], "ENG-4")

    def test_a_store_without_jira_reports_unavailable(self):
        # rewrite the tenant as a store with no STORY objects
        Fixture().write(self.dir / "ops.duckdb")
        body = get("/api/insights", tenant="ops", days=3650).json()
        self.assertFalse(body["rework"]["available"])
        self.assertIn("no STORY objects", body["rework"]["note"])

    def test_the_rules_payload_still_names_its_scope(self):
        body = get("/api/insights", tenant="del", days=3650).json()
        self.assertIn("every story in the store", body["rework"]["scope"])
        # the rework panel does not steal the PR-side heading
        self.assertEqual(body["rework"]["reopened"]["label"], "reopened after done")


if __name__ == "__main__":
    unittest.main()

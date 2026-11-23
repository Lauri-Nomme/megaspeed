"""prd/70 U7 (F-16): the sprint's own number, parsed at scrape time.

The team's Jira sprints are named `Sprint 352`-style, while the store keeps only
the id (`SPRINT:DEMO#4392`) and deliberately no name text (prd/02). The reducer
therefore parses a **closed** name shape into the integer ``sprint_no``; the
name itself is never stored. This file pins all three hops:

* the reducer writes ``sprint_no`` only when the name matches, and never the name;
* ``taxonomy.OBJECT_ATTRIBUTE_SPEC`` accepts it, so ``--verify`` passes;
* ``/api/sprints`` carries it on every sprint row (``None`` for a legacy store
  scraped before the attribute existed), and no name text reaches the payload.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from unittest import mock

from ingest import taxonomy
from ingest.config import Config
from ingest.jira_pipeline import JiraReducer, JiraStats, sprint_number
from ingest.pseudonym import IdentityTable
from ingest.store import connect, ensure_schema, verify, write_events, write_objects, write_run
from server import app as app_module

T0 = datetime(2026, 1, 5, 9, 0)
AS_OF = datetime(2026, 3, 1, 12, 0)


def _sprint(sid: int, name: str, *, start: str, end: str, state: str = "closed") -> dict:
    """The Jira sprint payload shape the reducer reads (fields + name)."""
    return {
        "id": sid,
        "name": name,
        "state": state,
        "startDate": start,
        "endDate": end,
        "completeDate": end if state == "closed" else None,
        "goal": "Ship the thing",
    }


# --------------------------------------------------------------------------- #
# the closed name -> number parser
# --------------------------------------------------------------------------- #
class SprintNumberTests(unittest.TestCase):
    def test_the_team_name_forms_parse_to_the_number(self):
        for name in (
            "Sprint 352",
            "sprint 352",
            "SPRINT 352",
            "S352",
            "s352",
            "DEMO Sprint 352",
            "DEL Sprint 2",
            "  Sprint   #352  ",
            "S3",
        ):
            with self.subTest(name=name):
                expected = 3 if name.strip() in ("S3",) else (
                    2 if "DEL" in name else 352
                )
                self.assertEqual(sprint_number(name), expected)
        self.assertEqual(sprint_number("Sprint 0"), 0)

    def test_a_name_outside_the_closed_shape_is_not_guessed_at(self):
        for name in (
            None,
            "",
            "   ",
            "Hardening",
            "Sprint",
            "Sprint 352 - hardening",
            "Sprint 3.5",
            "Sprint 352 of 12",
            "Q4 2026",
            "352",
        ):
            with self.subTest(name=name):
                self.assertIsNone(sprint_number(name))


class ReducerSprintNumberTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.reducer = JiraReducer(
            Config(
                db_path=root / "store.duckdb",
                identity_path=root / "identity.json",
                ticket_regex=r"\b[A-Z]+-\d+\b",
                key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
                repos=[],
                ticket_projects=("DEL",),
            ),
            IdentityTable(root / "identity.json"),
            AS_OF,
            epic_link_field="customfield_10100",
            sprint_field="customfield_10104",
            story_points_field="customfield_10106",
            status_categories={"10000": "new", "10002": "done"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _reduce(self, sprint: dict) -> dict:
        stats = JiraStats()
        self.reducer.reduce_sprint(sprint, "DEL", stats)
        return stats.objects[0]

    def test_a_named_sprint_stores_the_number_and_never_the_name(self):
        record = self._reduce(_sprint(
            4392, "DEMO Sprint 352",
            start="2026-01-05T09:00:00.000Z", end="2026-01-19T17:00:00.000Z",
        ))
        attributes = record["attributes"]
        self.assertEqual(attributes["sprint_no"], 352)
        # prd/02: no name text anywhere in the record
        dumped = json.dumps(record, default=str)
        self.assertNotIn("DEMO", dumped)
        self.assertNotIn("Sprint", dumped)
        self.assertNotIn("name", attributes)
        taxonomy.check_object_attributes(attributes)

    def test_an_unnamed_sprint_gets_no_number(self):
        for name in ("", "Hardening and cleanup", "Sprint 352 - hardening"):
            with self.subTest(name=name):
                record = self._reduce(_sprint(
                    18568, name,
                    start="2026-02-02T09:00:00.000Z", end="2026-02-16T17:00:00.000Z",
                ))
                self.assertNotIn("sprint_no", record["attributes"])
                if name:
                    self.assertNotIn(name, json.dumps(record, default=str))
                taxonomy.check_object_attributes(record["attributes"])

    def test_the_spec_accepts_only_a_non_negative_integer(self):
        self.assertEqual(
            taxonomy.check_object_attributes({"sprint_no": 352}), {"sprint_no": 352}
        )
        for bad in ("Sprint 352", "352", -1, 3.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(taxonomy.TaxonomyError):
                    taxonomy.check_object_attributes({"sprint_no": bad})


# --------------------------------------------------------------------------- #
# pipeline -> store -> /api/sprints
# --------------------------------------------------------------------------- #
def _write_store(path: Path, sprint_records: list[dict]) -> dict:
    """A tiny tenant store: the reducer's SPRINT rows + one story + memberships."""
    story = {
        "object_type": "STORY", "object_id": "STORY:DEL-100", "repo": None,
        "pr_id": None, "ticket_keys": ["DEL-100"], "actor": "AaAa",
        "state": "open", "created_at": T0, "closed_at": None,
        "head_commit": None, "message_class": None,
        "attributes": {"summary_class": "feature", "points": 3},
    }

    def _event(key: str, activity: str, **attributes) -> dict:
        return {
            "event_key": key, "ts": T0, "activity": activity, "actor": "AaAa",
            "repo": None, "pr_id": None, "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(key.encode(), digest_size=8).hexdigest(),
            "as_of": AS_OF,
        }

    created = _event("DEL-100:created", "created")
    added = _event("DEL-100:sprint", "sprint_added",
                   sprint_id=4392, at_start=True)
    moved = _event("DEL-100:sprint-legacy", "sprint_added",
                   sprint_id=18568, at_start=True)
    relations = [
        ("DEL-100:created", "STORY", "STORY:DEL-100"),
        ("DEL-100:sprint", "STORY", "STORY:DEL-100"),
        ("DEL-100:sprint", "SPRINT", "SPRINT:DEL#4392"),
        ("DEL-100:sprint-legacy", "STORY", "STORY:DEL-100"),
        ("DEL-100:sprint-legacy", "SPRINT", "SPRINT:DEL#18568"),
    ]
    con = connect(path)
    try:
        ensure_schema(con)
        write_events(con, [created, added, moved], relations)
        write_objects(con, [story, *sprint_records])
        write_run(con, {
            "run_id": "fixture-sprint-no",
            "source": "fixture",
            "as_of": AS_OF,
            "source_watermark": AS_OF,
            "classifier_version": "deterministic-v1",
            "prs_seen": 0,
            "prs_kept": 0,
            "prs_dropped": 0,
            "events_written": 3,
            "events_skipped": 0,
            "issues_seen": 1,
            "issues_kept": 1,
            "issues_skipped": 0,
        })
        return verify(con, ticket_projects=("DEL",))
    finally:
        con.close()


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


class SprintNumberApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        root = cls.dir / "reducer"
        root.mkdir()
        reducer = JiraReducer(
            Config(
                db_path=root / "store.duckdb",
                identity_path=root / "identity.json",
                ticket_regex=r"\b[A-Z]+-\d+\b",
                key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
                repos=[],
                ticket_projects=("DEL",),
            ),
            IdentityTable(root / "identity.json"),
            AS_OF,
            epic_link_field="customfield_10100",
            sprint_field="customfield_10104",
            story_points_field="customfield_10106",
            status_categories={"10000": "new", "10002": "done"},
        )
        stats = JiraStats()
        # the named sprint, and a legacy one whose name is empty
        reducer.reduce_sprint(_sprint(
            4392, "DEMO Sprint 352",
            start="2026-01-05T09:00:00.000Z", end="2026-01-19T17:00:00.000Z",
        ), "DEL", stats)
        reducer.reduce_sprint(_sprint(
            18568, "",
            start="2026-02-02T09:00:00.000Z", end="2026-02-16T17:00:00.000Z",
        ), "DEL", stats)
        cls.report = _write_store(cls.dir / "del.duckdb", stats.objects)
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
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

    def test_verify_accepts_the_new_attribute(self):
        self.assertTrue(self.report["ok"], self.report["problems"])

    def test_the_api_row_carries_the_number_and_never_the_name(self):
        reply = get("/api/sprints", tenant="del", days=3650)
        self.assertEqual(reply.status, 200)
        body = reply.json()
        self.assertTrue(body["available"])
        rows = {row["sprint_id"]: row for row in body["sprints"]}
        self.assertEqual(rows[4392]["sprint_no"], 352)
        # the legacy row still answers the key, with no invented number
        self.assertIn("sprint_no", rows[18568])
        self.assertIsNone(rows[18568]["sprint_no"])
        # the delivery drill-down carries it too, from the same attribute
        delivery = {row["sprint_id"]: row for row in body["delivery"]["sprints"]}
        self.assertEqual(delivery[4392]["sprint_no"], 352)
        self.assertIsNone(delivery[18568]["sprint_no"])
        # prd/02: the name text never reaches the payload
        raw = reply.body.decode()
        self.assertNotIn("DEMO", raw)
        self.assertNotIn("Sprint 352", raw)

    def test_no_active_sprint_reports_no_number(self):
        body = get("/api/sprints", tenant="del", days=3650).json()
        self.assertFalse(body["at_risk"]["available"])
        self.assertIsNone(body["at_risk"]["sprint_no"])
        self.assertIn("sprint_no", body["at_risk"])

    def test_the_ticket_membership_carries_the_number(self):
        reply = get("/api/ticket/DEL-100", tenant="del")
        self.assertEqual(reply.status, 200, reply.body)
        memberships = reply.json()["sprints"]["memberships"]
        by_id = {row["sprint_id"]: row for row in memberships}
        self.assertEqual(by_id[4392]["sprint_no"], 352)
        self.assertIn("sprint_no", by_id[18568])
        self.assertIsNone(by_id[18568]["sprint_no"])

    def test_the_rules_drill_down_rows_carry_the_number(self):
        body = get("/api/insights", tenant="del", days=3650).json()
        metrics = body["metrics"]
        delivery = {row["sprint_id"]: row for row in metrics["sprint_delivery"]["rows"]}
        self.assertEqual(delivery[4392]["sprint_no"], 352)
        self.assertIsNone(delivery[18568]["sprint_no"])
        carry = metrics["carry_over"]["rows"]
        self.assertTrue(carry, "the open story missed both closed sprints")
        row = carry[0]
        self.assertEqual(row["sprints"], [4392, 18568])
        # parallel lists: the id and the parsed number travel together
        self.assertEqual(row["sprint_nos"], [352, None])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""prd/68 G3 — no-PR honesty (F-04, F-07 UI, F-10, F-12).

The live corpus ``demo-tlla`` holds 2,320 stories and **0 PRs**. Every finding
here is the same mistake in a different place: a layer the dataset does not
carry was reported as a measured zero.

* **F-04** — ``calibrate()`` returned early when there was no review sample and
  omitted ``refinement``, so the Policy lab disabled ``refine before start``
  with "no Jira layer" on a store whose refinement segment *is* measured. The
  early return now carries the block, and the lab calibrates on the
  story-extended window when the PR court has no intervals (a Jira-only store
  collapses ``window_from_events`` to the snapshot).
* **F-07 (UI)** — the JS half is ``app/test/prd68-g3.test.mjs``.
* **F-10** — ``done_but_open()`` was ``available`` whenever there were stories,
  so a 0-PR store rendered a green "every ticket has its code merged". It now
  requires the PR layer and states "unavailable: this dataset holds no pull
  requests".
* **F-12** — an epic exports its own Markdown; the child rows this test pins
  are what that export renders.

    python3 -m unittest tests.test_prd68_g3 -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from analysis import ballcourt, jira, rules, simulation, tickets
from ingest.store import connect
from server import app as app_module
from tests.test_ticket_view import Fixture, StoreCase, T0, fixture as pr_fixture


def jira_only() -> Fixture:
    """2,320 stories' shape in miniature: an epic, two children, 0 PRs.

    Each story is created, refined to ``ready`` 4 h later and closed 20 h
    after creation, so the refinement segment is measurable and the court has
    no PR interval at all (the ``demo-tlla`` shape).
    """
    fx = Fixture()
    fx._object(
        "EPIC", "EPIC:DEL-1", ticket_keys=["DEL-1"], state="done",
        created_at=T0, closed_at=T0 + timedelta(days=3),
    )
    for key, points, ready_hours in (
        ("DEL-167", 8, 4), ("DEL-168", 3, 6),
    ):
        fx.story(key, T0, points=points, state="done",
                 closed=T0 + timedelta(hours=20))
        fx.status(key, T0 + timedelta(hours=ready_hours), "ready")
        fx.status(key, T0 + timedelta(hours=12), "in_progress",
                  from_status="ready")
        fx.status(key, T0 + timedelta(hours=20), "done",
                  from_status="in_progress")
    return fx


# --------------------------------------------------------------------------- #
# F-04 — the calibration keeps the refinement block on the early return
# --------------------------------------------------------------------------- #
class CalibrationRefinementTests(StoreCase):
    def test_a_jira_only_calibration_still_carries_the_measured_refinement(self):
        con, court, data = self.load(jira_only())
        try:
            window = rules.window_for(court, 90, data)
            cost = jira.refinement_cost(court, data, window)
            self.assertEqual(cost["hours"], 5.0)      # median of 4 h and 6 h
            calibration = simulation.calibrate(court, window, refinement=cost)
        finally:
            con.close()
        # no PRs, so there is no review sample: the lab is not calibrated …
        self.assertFalse(calibration["ok"])
        self.assertFalse(calibration["available"])
        self.assertEqual(calibration["prs"]["n"], 0)
        # … but the Jira layer is measured and must reach the UI (F-04 bug).
        self.assertIn("refinement", calibration)
        self.assertEqual(calibration["refinement"]["hours"], cost["hours"])
        self.assertEqual(calibration["refinement"]["n"], cost["n"])
        self.assertIn("created → ready", calibration["refinement"]["basis"])

    def test_truly_empty_refinement_stays_none_with_its_reason(self):
        con, court, data = self.load(jira_only())
        try:
            window = rules.window_for(court, 90, data)
            empty = jira.refinement_cost(court, data, (window[1], window[1]))
            self.assertIsNone(empty["hours"])
            calibration = simulation.calibrate(court, window, refinement=empty)
        finally:
            con.close()
        self.assertIn("refinement", calibration)
        self.assertIsNone(calibration["refinement"]["hours"])
        self.assertEqual(calibration["refinement"]["n"], 0)
        self.assertIn("no measured created → ready segment",
                      calibration["refinement"]["basis"])

    def test_without_a_refinement_argument_the_block_is_present_and_null(self):
        con, court, data = self.load(jira_only())
        try:
            calibration = simulation.calibrate(court, rules.window_for(court, 90, data))
        finally:
            con.close()
        self.assertIn("refinement", calibration)
        self.assertIsNone(calibration["refinement"]["hours"])
        self.assertIn("no Jira layer", calibration["refinement"]["basis"])

    def test_a_pr_store_keeps_both_the_calibration_and_the_refinement(self):
        con, court, data = self.load(pr_fixture())
        try:
            window = rules.window_for(court, None, data)
            cost = jira.refinement_cost(court, data, window)
            calibration = simulation.calibrate(court, window, conn=con, refinement=cost)
        finally:
            con.close()
        self.assertTrue(calibration["ok"])
        self.assertEqual(calibration["refinement"]["hours"], cost["hours"])


# --------------------------------------------------------------------------- #
# F-04 — the API the Policy lab reads
# --------------------------------------------------------------------------- #
class Reply:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    def json(self):
        return json.loads(self.body.decode())


async def _call(app, path: str, query: str) -> Reply:
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "", "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Reply(start["status"], body)


class PolicyLabApiTests(unittest.TestCase):
    """A 0-PR tenant's ``/api/simulate`` reports the measured refinement."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        jira_only().write(cls.dir / "demo.duckdb")
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

    def test_the_jira_only_lab_exposes_a_measured_refinement(self):
        reply = asyncio.run(_call(
            app_module.app, "/api/simulate", urlencode({"tenant": "demo", "days": 90}),
        ))
        self.assertEqual(reply.status, 200, reply.body)
        body = reply.json()
        calibration = body["calibration"]
        # the PR court is empty — the lab cannot validate — but the Jira
        # measurement is there, not a missing key (F-04).
        self.assertEqual(calibration["prs"]["n"], 0)
        refinement = calibration["refinement"]
        self.assertEqual(refinement["hours"], 5.0)
        self.assertGreaterEqual(refinement["n"], 2)
        self.assertNotIn("no Jira layer", refinement["basis"])
        self.assertIn("created → ready", refinement["basis"])


# --------------------------------------------------------------------------- #
# F-10 — Done in Jira, PR open on a store with no PRs
# --------------------------------------------------------------------------- #
class DoneButOpenTests(StoreCase):
    def test_a_zero_pr_store_is_unavailable_not_every_ticket_merged(self):
        con, court, data = self.load(jira_only())
        try:
            self.assertEqual(len(court.prs), 0)
            body = tickets.done_but_open(court, data)
        finally:
            con.close()
        self.assertFalse(body["available"])
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["count"], 0)
        self.assertIn("no pull requests", body["unavailable_reason"])
        self.assertIn("unavailable:", body["unavailable_reason"])
        # the panel's note is the same honest statement, never the claim that
        # every Done ticket has merged code
        self.assertEqual(body["note"], body["unavailable_reason"])
        self.assertNotIn("Every ticket", body["note"])
        self.assertNotIn("has its code merged", body["note"])

    def test_a_store_with_prs_keeps_the_measured_result(self):
        con, court, data = self.load(pr_fixture())
        try:
            body = tickets.done_but_open(court, data)
        finally:
            con.close()
        self.assertTrue(body["available"])
        self.assertIn("DEL-167", [row["key"] for row in body["rows"]])
        self.assertIsNone(body["unavailable_reason"])
        self.assertIn("a ticket is Done when", body["note"])

    def test_a_store_with_prs_but_no_stories_says_which_layer_is_missing(self):
        fx = Fixture()
        fx.pr(7, "AaAa", T0, tickets=("OPS-7",), closed=T0 + timedelta(hours=5))
        fx.pr_event(7, "pr_merged", "RrRr", 5)
        con, court, data = self.load(fx)
        try:
            body = tickets.done_but_open(court, data)
        finally:
            con.close()
        self.assertFalse(body["available"])
        self.assertIn("no Jira stories", body["unavailable_reason"])
        self.assertNotIn("no pull requests", body["unavailable_reason"])


# --------------------------------------------------------------------------- #
# F-12 — an epic page carries the child rows the Markdown export renders
# --------------------------------------------------------------------------- #
class EpicPayloadTests(StoreCase):
    def test_the_epic_payload_has_the_header_and_child_table_fields(self):
        con, court, data = self.load(jira_only())
        try:
            payload = tickets.ticket(court, data, "DEL-1")
        finally:
            con.close()
        self.assertEqual(payload["kind"], "epic")
        epic = payload["epic"]
        self.assertEqual(epic["story_count"], 2)
        self.assertEqual(epic["points"], 11)          # 8 + 3
        self.assertEqual(epic["state"], "done")
        self.assertEqual([row["key"] for row in epic["stories"]],
                         ["DEL-167", "DEL-168"])     # 20 h cycles, then key
        for row in epic["stories"]:
            self.assertIsNotNone(row["cycle_hours"])
            self.assertFalse(row["censored"])
            self.assertIn("points", row)
            self.assertIn("state", row)


if __name__ == "__main__":
    unittest.main()

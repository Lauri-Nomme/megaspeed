"""Trend / time series (prd/50): buckets from the same cohort and calendar.

Every assertion runs against a **synthetic store with hand-computed dates** —
no fixture generator, no randomness — so a moved bucket edge or a changed value
fails with the exact number that moved. The store is deliberately small and
spread over three weeks and three sprints:

* week 1 (5 Jan) — four PRs opened and merged in the week, plus one opened in
  the week and merged in week 5 (so the bucket is the *opened* cohort);
* week 2 (12 Jan) — two PRs opened and merged;
* week 3 (19 Jan) — one PR opened and still open at the snapshot;
* sprint 1 closes 18 Jan, sprint 2 closes 1 Feb, sprint 3 is active.

The endpoints are driven through the real ASGI app, so the metric/bucket
validation and the payload shape are pinned too.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from analysis import ballcourt, jira, rules, trend
from ingest.store import connect, to_naive_utc
from server import app as app_module
from tests.test_jira_analysis import Fixture, T0, AS_OF

MICROSECOND = timedelta(microseconds=1)


# --------------------------------------------------------------------------- #
# the synthetic store
# --------------------------------------------------------------------------- #
def three_week_fixture() -> Fixture:
    """The hand-computed store the tests assert against (see the module doc)."""
    fx = Fixture()
    # sprint 1 closes 18 Jan 23:00, sprint 2 closes 1 Feb 23:00, sprint 3 is active
    fx.sprint(1, T0, end=datetime(2026, 1, 18), closed=datetime(2026, 1, 18, 23))
    fx.sprint(2, datetime(2026, 1, 19), end=datetime(2026, 2, 1),
              closed=datetime(2026, 2, 1, 23))
    fx.sprint(3, datetime(2026, 2, 2), state="active")

    # -- PRs: four merged in week 1 ---------------------------------------- #
    for number in (1, 2, 3, 4):
        opened = T0 + timedelta(hours=number)
        fx.pr(number, "AaAa", opened, tickets=("OPS-1",),
              closed=opened + timedelta(hours=24))
        fx.pr_event(number, "comment_added", "RrRr", 8)
        fx.pr_event(number, "pr_pushed", "AaAa", 12, push_class="content")
        fx.pr_event(number, "pr_approved", "RrRr", 20)
        fx.pr_event(number, "pr_merged", "RrRr", 24)
    # -- PR 5: opened in week 1, merged in week 5 -------------------------- #
    opened5 = T0 + timedelta(days=2)
    fx.pr(5, "AaAa", opened5, tickets=("OPS-1",),
          closed=datetime(2026, 2, 4, 9))
    fx.pr_event(5, "comment_added", "RrRr", 8)
    fx.pr_event(5, "pr_pushed", "AaAa", 12, push_class="content")
    fx.pr_event(5, "pr_approved", "RrRr", 20)
    # -- PRs 6, 7: opened and merged in week 2 ----------------------------- #
    for number in (6, 7):
        opened = datetime(2026, 1, 12, 9) + timedelta(hours=number)
        fx.pr(number, "AaAa", opened, tickets=("OPS-1",),
              closed=opened + timedelta(hours=48))
        fx.pr_event(number, "comment_added", "RrRr", 8)
        fx.pr_event(number, "pr_pushed", "AaAa", 12, push_class="content")
        fx.pr_event(number, "pr_approved", "RrRr", 20)
        fx.pr_event(number, "pr_merged", "RrRr", 48)
    # -- PR 8: opened in week 3, still open at the snapshot ---------------- #
    fn = datetime(2026, 1, 19, 10)
    fx.pr(8, "AlNg", fn, tickets=("OPS-1",), state="open", closed=None)
    fx.pr_event(8, "comment_added", "RrRr", 8)

    # -- stories: sprint outcomes measured at each sprint's close ---------- #
    def story(key: str, created: datetime, done: datetime | None,
              sprint: int, added: datetime) -> None:
        fx.story(key, created, state="done" if done else "open", closed=done)
        fx.pointed(key, created + timedelta(hours=4), 3)
        fx.add_to_sprint(key, added, sprint)
        fx.status(key, created + timedelta(hours=8), "in_progress")
        if done is not None:
            fx.status(key, done, "done", from_status="in_progress")

    # sprint 1: one done by the close, one done late (the close is 18 Jan)
    story("ENG-1", T0, datetime(2026, 1, 10, 16), 1, T0 + timedelta(hours=2))
    story("ENG-2", T0, datetime(2026, 1, 25, 16), 1, T0 + timedelta(hours=2))
    # sprint 2: one done by the close, one never done
    story("ENG-3", datetime(2026, 1, 19, 9), datetime(2026, 1, 25, 16), 2,
          datetime(2026, 1, 19, 10))
    story("ENG-4", datetime(2026, 1, 20, 9), None, 2, datetime(2026, 1, 20, 10))
    # sprint 3 (active): done, but an active sprint has no verdict yet
    story("ENG-5", datetime(2026, 2, 2, 9), datetime(2026, 2, 10, 16), 3,
          datetime(2026, 2, 2, 10))
    return fx


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fx: Fixture | None = None):
        (fx or three_week_fixture()).write(self.path)
        con = connect(self.path, read_only=True)
        court = ballcourt.load(con)          # wall-clock: exact hours
        data = jira.load(con, court)
        window = rules.window_for(court, None, data)
        return con, court, data, window


def _bucket(point: dict) -> tuple[datetime, datetime]:
    """The evaluation window a point quotes (``end`` is exclusive)."""
    return to_naive_utc(point["start"]), to_naive_utc(point["end"]) - MICROSECOND


# --------------------------------------------------------------------------- #
# 1. bucket boundaries
# --------------------------------------------------------------------------- #
class BucketBoundaryTests(StoreCase):
    def test_weeks_start_on_monday_and_the_window_clips_the_first(self):
        con, court, data, window = self.load()
        try:
            payload = trend.trend(court, data, window, metric="cycle_median")
        finally:
            con.close()
        points = payload["points"]
        self.assertEqual(len(points), 8)
        self.assertEqual(
            [point["bucket_start"] for point in points[:3]],
            ["2026-01-05T00:00:00Z", "2026-01-12T00:00:00Z",
             "2026-01-19T00:00:00Z"],
        )
        # the analysis window starts at the first PR (09:00), so the first
        # bucket's evaluation window is clipped but its calendar edge is Monday
        self.assertEqual(points[0]["start"], "2026-01-05T09:00:00Z")
        self.assertEqual(points[0]["end"], "2026-01-12T00:00:00Z")
        self.assertEqual(points[1]["start"], "2026-01-12T00:00:00Z")

    def test_only_the_bucket_containing_the_snapshot_is_in_progress(self):
        con, court, data, window = self.load()
        try:
            payload = trend.trend(court, data, window, metric="cycle_median")
        finally:
            con.close()
        flags = [point["in_progress"] for point in payload["points"]]
        self.assertEqual(flags, [False] * 7 + [True])
        self.assertEqual(payload["summary"]["in_progress"], ["23 Feb"])

    def test_sprint_buckets_use_the_sprints_measured_band(self):
        con, court, data, window = self.load()
        try:
            payload = trend.trend(
                court, data, window, metric="on_time", bucket="sprint",
            )
        finally:
            con.close()
        self.assertEqual([point["label"] for point in payload["points"]],
                         ["S1", "S2", "S3"])
        self.assertEqual([point["in_progress"] for point in payload["points"]],
                         [False, False, True])
        self.assertEqual(payload["active_sprint"]["id"], 3)


# --------------------------------------------------------------------------- #
# 2. values match a recompute of the same metric function
# --------------------------------------------------------------------------- #
class TrendValueTests(StoreCase):
    def test_cycle_median_points_match_the_window_metric(self):
        con, court, data, window = self.load()
        try:
            payload = trend.trend(court, data, window, metric="cycle_median")
        finally:
            con.close()
        values = [point["value"] for point in payload["points"]]
        # week 1: five opened (four 24 h cycles + PR 5's long cycle) → 24 h
        self.assertEqual(values[0], 24.0)
        self.assertEqual(payload["points"][0]["n"], 5)
        # week 2: two opened, both 48 h
        self.assertEqual(values[1], 48.0)
        self.assertEqual(payload["points"][1]["n"], 2)
        # week 3: opened PR is still open → right-censored, no median
        self.assertIsNone(values[2])
        self.assertFalse(payload["points"][2]["available"])
        # every measured point equals the metric evaluated on its own window
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            for point in payload["points"]:
                if point["value"] is None:
                    continue
                expected = rules.cycle_time(court, _bucket(point))["median_hours"]
                self.assertEqual(point["value"], expected, point["label"])
        finally:
            con.close()

    def test_flow_efficiency_and_wait_hours_match_the_window_metric(self):
        con, court, data, window = self.load()
        try:
            flow = trend.trend(court, data, window, metric="flow_efficiency")
            waits = trend.trend(court, data, window, metric="wait_hours")
        finally:
            con.close()
        self.assertTrue(flow["points"][0]["available"])
        self.assertEqual(flow["points"][0]["n"], 5)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            for point in flow["points"]:
                if not point["available"]:
                    continue
                block = rules.flow_efficiency(court, _bucket(point))
                self.assertEqual(point["value"], block["aggregate_efficiency"])
            for point in waits["points"]:
                block = rules.flow_efficiency(court, _bucket(point))
                self.assertEqual(point["value"], block["waiting_hours"])
        finally:
            con.close()

    def test_merged_and_open_counts_are_the_items_the_bucket_holds(self):
        con, court, data, window = self.load()
        try:
            merged = trend.trend(court, data, window, metric="merged")
            opened = trend.trend(court, data, window, metric="open")
        finally:
            con.close()
        # PRs 1–4 merged in week 1; PR 5 merged in week 5 (4 Feb)
        self.assertEqual([point["value"] for point in merged["points"][:2]], [4, 2])
        self.assertEqual(merged["points"][4]["value"], 1)
        # PR 5 is open at the end of week 1; PRs 5 and 8 from week 3 on
        self.assertEqual(opened["points"][0]["value"], 1)
        self.assertEqual(opened["points"][2]["value"], 2)
        self.assertEqual(opened["points"][7]["value"], 1)

    def test_on_time_is_done_by_the_close_over_committed_per_sprint(self):
        con, court, data, window = self.load()
        try:
            weekly = trend.trend(court, data, window, metric="on_time")
            sprint = trend.trend(
                court, data, window, metric="on_time", bucket="sprint",
            )
        finally:
            con.close()
        # sprint 1 closes in the week of 12 Jan: 1 of 2 done by the close
        week2 = weekly["points"][1]
        self.assertEqual(week2["value"], 0.5)
        self.assertEqual(week2["n"], 1)
        self.assertEqual(week2["stories"], 2)
        self.assertEqual(week2["done_by_end"], 1)
        # sprint 2 closes in the week of 26 Jan: 1 of 2
        self.assertEqual(weekly["points"][3]["value"], 0.5)
        # the active sprint has no verdict: its bucket is in progress with no value
        self.assertIsNone(sprint["points"][2]["value"])
        self.assertTrue(sprint["points"][2]["in_progress"])

    def test_the_payload_names_the_metric_and_its_definition(self):
        con, court, data, window = self.load()
        try:
            payload = trend.trend(court, data, window, metric="cycle_median")
        finally:
            con.close()
        self.assertEqual(payload["metric"], "cycle_median")
        self.assertEqual(payload["unit"], "hours")
        self.assertIn("rules.cycle_time", payload["definition"])
        self.assertEqual(
            [entry["id"] for entry in payload["metrics"]],
            ["flow_efficiency", "cycle_median", "on_time", "wait_hours",
             "merged", "open"],
        )

    def test_a_store_without_sprints_explains_the_empty_sprint_bucket(self):
        fx = Fixture()
        fx.pr(1, "AaAa", T0, tickets=("OPS-1",),
              closed=T0 + timedelta(hours=4))
        con, court, data, window = self.load(fx)
        try:
            payload = trend.trend(court, data, window, metric="merged",
                                  bucket="sprint")
        finally:
            con.close()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["points"], [])
        self.assertIn("no SPRINT object", payload["note"])
        # the weekly bucket still works on a PR-only store
        con, court, data, window = self.load(fx)
        try:
            weekly = trend.trend(court, data, window, metric="merged")
        finally:
            con.close()
        self.assertEqual(weekly["points"][0]["value"], 1)

    def test_an_unknown_metric_or_bucket_is_a_value_error(self):
        con, court, data, window = self.load()
        try:
            with self.assertRaises(ValueError):
                trend.trend(court, data, window, metric="nope")
            with self.assertRaises(ValueError):
                trend.trend(court, data, window, metric="merged", bucket="nope")
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# 3. the endpoint (real ASGI app, temp tenant directory)
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
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return Reply(start["status"], headers, body)


def get(path: str, **query: object) -> Reply:
    return asyncio.run(_call_asgi(app_module.app, path, urlencode(query)))


class TrendApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        three_week_fixture().write(cls.dir / "trend.duckdb")
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "LOG", False),
            # wall-clock: the API tests must price hours exactly like the
            # analysis tests (a real calendar would turn 24 wall-clock h into
            # 8 business hours across an evening).
            mock.patch.object(
                app_module, "CALENDAR_CONFIG", cls.dir / "no-calendar.yaml",
            ),
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
        app_module._trend_cache.clear()

    def test_the_endpoint_returns_a_series_and_the_metric_catalogue(self):
        reply = get("/api/trend", tenant="trend", metric="cycle_median")
        self.assertEqual(reply.status, 200, reply.body)
        body = reply.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tenant"], "trend")
        self.assertEqual(body["metric"], "cycle_median")
        self.assertEqual(body["bucket"], "week")
        self.assertEqual(body["points"][0]["value"], 24.0)
        self.assertEqual(body["points"][0]["bucket_start"], "2026-01-05T00:00:00Z")
        self.assertEqual(len(body["metrics"]), 6)
        self.assertEqual(reply.headers.get("x-tenant"), "trend")
        self.assertEqual(body["calendar_id"], "none")
        self.assertEqual(body["hours_mode"], "wall-clock")

    def test_the_default_metric_is_flow_efficiency(self):
        body = get("/api/trend", tenant="trend").json()
        self.assertEqual(body["metric"], "flow_efficiency")

    def test_a_sprint_bucket_is_accepted(self):
        body = get("/api/trend", tenant="trend", metric="on_time",
                   bucket="sprint").json()
        self.assertEqual(body["bucket"], "sprint")
        self.assertEqual([point["label"] for point in body["points"]],
                         ["S1", "S2", "S3"])
        self.assertTrue(body["points"][2]["in_progress"])

    def test_an_unknown_metric_is_a_400_that_lists_the_valid_ones(self):
        reply = get("/api/trend", tenant="trend", metric="nope")
        self.assertEqual(reply.status, 400)
        body = reply.json()
        self.assertIn("metric must be one of", body["error"])
        self.assertIn("cycle_median", body["error"])
        # the `valid` list is the accepted query parameters, like every endpoint
        self.assertIn("metric", body.get("valid") or [])

    def test_an_unknown_bucket_is_a_400(self):
        reply = get("/api/trend", tenant="trend", bucket="fortnight")
        self.assertEqual(reply.status, 400)
        self.assertIn("bucket must be one of", reply.json()["error"])

    def test_an_unknown_query_parameter_is_a_400(self):
        reply = get("/api/trend", tenant="trend", nope="1")
        self.assertEqual(reply.status, 400)
        self.assertIn("nope", reply.json()["error"])

    def test_a_repeat_request_is_served_from_the_cache(self):
        first = get("/api/trend", tenant="trend", metric="merged")
        second = get("/api/trend", tenant="trend", metric="merged")
        self.assertEqual(first.status, 200)
        self.assertEqual(first.body, second.body)
        self.assertTrue(app_module._trend_cache)

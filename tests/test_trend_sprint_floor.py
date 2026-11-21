"""Trend min-N contract per bucket kind (prd/64 F-17).

A ``bucket=sprint`` trend holds exactly one sprint per bucket (``n = 1``), so a
floor counting *buckets* can never be met there: the old headline was
permanently ``– no bucket meets the n≥2 floor`` while the chart drew a rising
line. The sample of a sprint bucket is the sprint's own **committed stories**,
so ``on_time`` on ``bucket=sprint`` is judged on ``stories >= 5`` and the
headline can quote ``50.0 % latest (S2 · 15/30 stories) · −10.0 pts since S1``.
Week buckets keep the ``n ≥ 2`` closed-sprint rule.

The fixture is hand-computed: S1 commits 30 stories and delivers 18 by its close
(60.0 %), S2 commits 30 and delivers 15 (50.0 %), S3 is active (no verdict).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from analysis import ballcourt, jira, rules, trend
from ingest.store import connect
from server import app as app_module
from tests.test_jira_analysis import Fixture, T0
from tests.test_trend import get


def delivery_fixture(
    committed: tuple[int, int] = (30, 30),
    delivered: tuple[int, int] = (18, 15),
) -> Fixture:
    """Two closed sprints with hand-set committed/delivered counts + one active."""
    fx = Fixture()
    fx.sprint(1, T0, end=datetime(2026, 1, 18), closed=datetime(2026, 1, 18, 23))
    fx.sprint(2, datetime(2026, 1, 19), end=datetime(2026, 2, 1),
              closed=datetime(2026, 2, 1, 23))
    fx.sprint(3, datetime(2026, 2, 2), state="active")
    starts = {1: T0, 2: datetime(2026, 1, 19, 9)}
    # every "done" lands well before its sprint's close: by_end, not late
    closes = {1: datetime(2026, 1, 10, 16), 2: datetime(2026, 1, 25, 16)}
    number = 0
    for sid, (total, done) in enumerate(zip(committed, delivered), start=1):
        for index in range(total):
            number += 1
            key = f"ENG-{number}"
            created = starts[sid] + timedelta(minutes=index)
            done_at = closes[sid] + timedelta(minutes=index) if index < done else None
            fx.story(key, created, state="done" if done_at else "open",
                     closed=done_at)
            fx.pointed(key, created + timedelta(hours=1), 3)
            # added on the sprint's start day: the plan, not an adoption
            fx.add_to_sprint(key, created + timedelta(hours=2), sid)
            fx.status(key, created + timedelta(hours=3), "in_progress")
            if done_at is not None:
                fx.status(key, done_at, "done", from_status="in_progress")
    # the active sprint carries one story — and no verdict yet
    fx.story("ENG-99", datetime(2026, 2, 2, 9), state="open")
    fx.add_to_sprint("ENG-99", datetime(2026, 2, 2, 10), 3)
    return fx


class TrendCase(unittest.TestCase):
    committed: tuple[int, int] = (30, 30)
    delivered: tuple[int, int] = (18, 15)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"
        delivery_fixture(self.committed, self.delivered).write(self.path)
        self.con = connect(self.path, read_only=True)
        self.court = ballcourt.load(self.con)
        self.data = jira.load(self.con, self.court)
        self.window = rules.window_for(self.court, None, self.data)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def payload(self, bucket: str, metric: str = "on_time") -> dict:
        return trend.trend(self.court, self.data, self.window,
                           metric=metric, bucket=bucket)


class MinNContractTests(unittest.TestCase):
    """The floor and what it counts, per metric × bucket kind."""

    def test_a_sprint_bucket_counts_committed_stories_and_a_week_counts_sprints(self):
        self.assertEqual(trend.min_n_rule("on_time", "sprint"), (5, "stories"))
        self.assertEqual(trend.min_n_rule("on_time", "week"), (2, "n"))
        # every other metric keeps the bucket cohort floor it always had
        self.assertEqual(trend.min_n_rule("cycle_median", "week"), (5, "n"))
        self.assertEqual(trend.min_n_rule("cycle_median", "sprint"), (5, "n"))
        self.assertEqual(trend.min_n_rule("merged", "week"), (0, "n"))
        self.assertEqual(trend.min_n_rule("cumulative_flow", "sprint"), (0, "n"))


class SprintBucketHeadlineTests(TrendCase):
    def test_a_sprint_bucket_reports_its_share_and_the_delta_since_the_first(self):
        payload = self.payload("sprint")
        self.assertEqual(payload["min_n"], 5)
        self.assertEqual(payload["min_n_basis"], "stories")
        # the plotted quantity is each closed sprint's delivery share
        self.assertEqual([point["label"] for point in payload["points"]],
                         ["S1", "S2", "S3"])
        self.assertEqual([point["value"] for point in payload["points"][:2]],
                         [0.6, 0.5])
        summary = payload["summary"]
        self.assertEqual(summary["latest"], 0.5)
        self.assertEqual(summary["latest_label"], "S2")
        self.assertEqual(summary["first"], 0.6)
        self.assertEqual(summary["first_label"], "S1")
        self.assertEqual(summary["change"], -0.1)
        # the headline quotes the sprint's stories, never its bucket count
        self.assertEqual(summary["latest_n"], 1)
        self.assertEqual(summary["latest_stories"], 30)
        self.assertEqual(summary["latest_done_by_end"], 15)
        self.assertEqual(summary["min_n"], 5)
        self.assertEqual(summary["min_n_basis"], "stories")
        # both closed sprints clear the floor: nothing is skipped
        self.assertEqual(summary["below_min"], [])
        self.assertEqual(summary["low_n_after_headline"], [])
        # the active sprint is labelled, never headlined
        self.assertEqual(summary["in_progress"], ["S3"])
        self.assertFalse(summary["latest_provisional"])

    def test_a_sprint_under_five_committed_stories_is_low_n(self):
        # sprints that commit two stories each: below the story floor
        con, court, data, window = _small_sprints()
        try:
            payload = trend.trend(court, data, window, metric="on_time",
                                  bucket="sprint")
        finally:
            con.close()
        summary = payload["summary"]
        self.assertEqual(summary["min_n"], 5)
        self.assertEqual(summary["min_n_basis"], "stories")
        self.assertIsNone(summary["latest"])
        self.assertEqual(
            summary["below_min"],
            [{"label": "S1", "n": 1, "stories": 2},
             {"label": "S2", "n": 1, "stories": 2}],
        )
        self.assertEqual(summary["low_n_after_headline"], [])
        # the points are still drawn (a 2-story sprint is a real share, just a
        # tiny sample): 1 of 2 delivered by the close
        self.assertEqual([point["value"] for point in payload["points"][:2]],
                         [0.5, 0.5])

    def test_week_buckets_keep_the_n_of_two_closed_sprints_rule(self):
        payload = self.payload("week")
        self.assertEqual(payload["min_n"], 2)
        self.assertEqual(payload["min_n_basis"], "n")
        summary = payload["summary"]
        # one sprint closes per week: each bucket is n=1, below the n≥2 floor
        closed = [point for point in payload["points"] if point["value"] is not None]
        self.assertEqual([point["label"] for point in closed], ["12 Jan", "26 Jan"])
        self.assertEqual([point["n"] for point in closed], [1, 1])
        self.assertIsNone(summary["latest"])
        self.assertEqual([entry["n"] for entry in summary["below_min"]], [1, 1])

    def test_the_tab_names_the_plotted_quantity_not_the_sprints_on_time_count(self):
        payload = self.payload("sprint")
        self.assertEqual(payload["label"], "Sprint story delivery share")
        self.assertIn("committed", payload["definition"])
        # the other answer (the ≥80 % target count) lives on Timeline/Rules and
        # the definition says so, so the two numbers cannot be read as one
        self.assertIn("sprints on time (≥80 %)", payload["definition"])
        catalogue = {row["id"]: row for row in payload["metrics"]}
        self.assertEqual(catalogue["on_time"]["label"],
                         "Sprint story delivery share")
        # the weekly payload carries the same label: one quantity, one name
        self.assertEqual(self.payload("week")["label"],
                         "Sprint story delivery share")


class ShortSprintIsSkippedTests(TrendCase):
    committed = (30, 3)
    delivered = (18, 1)

    def test_a_later_sprint_below_the_floor_stays_drawn_and_is_named(self):
        payload = self.payload("sprint")
        summary = payload["summary"]
        # S2 commits 3 stories (1 delivered) → below the 5-story floor
        self.assertEqual(summary["latest"], 0.6)
        self.assertEqual(summary["latest_label"], "S1")
        self.assertEqual(summary["latest_stories"], 30)
        self.assertIsNone(summary["change"])
        self.assertEqual(summary["below_min"],
                         [{"label": "S2", "n": 1, "stories": 3}])
        self.assertEqual(summary["low_n_after_headline"], ["S2"])
        # still plotted, with its own value: named, never hidden
        s2 = next(point for point in payload["points"] if point["label"] == "S2")
        self.assertTrue(s2["available"])
        self.assertEqual(s2["value"], 0.3333)


def _small_sprints():
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "store.duckdb"
    delivery_fixture((2, 2), (1, 1)).write(path)
    con = connect(path, read_only=True)
    court = ballcourt.load(con)
    data = jira.load(con, court)
    window = rules.window_for(court, None, data)
    tmp.cleanup()  # the connection holds the file open; unlink happens on close
    return con, court, data, window


class SprintFloorApiTests(unittest.TestCase):
    """The endpoint carries the same floor and the story counts."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        delivery_fixture().write(cls.dir / "floor.duckdb")
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "LOG", False),
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

    def test_the_sprint_bucket_payload_quotes_the_story_floor(self):
        reply = get("/api/trend", tenant="floor", metric="on_time",
                    bucket="sprint")
        self.assertEqual(reply.status, 200, reply.body)
        body = reply.json()
        self.assertEqual(body["label"], "Sprint story delivery share")
        self.assertEqual(body["min_n"], 5)
        self.assertEqual(body["min_n_basis"], "stories")
        self.assertEqual(body["summary"]["latest"], 0.5)
        self.assertEqual(body["summary"]["latest_stories"], 30)
        self.assertEqual(body["summary"]["latest_done_by_end"], 15)

    def test_the_week_bucket_payload_keeps_the_sprint_count_floor(self):
        body = get("/api/trend", tenant="floor", metric="on_time").json()
        self.assertEqual(body["min_n"], 2)
        self.assertEqual(body["min_n_basis"], "n")


if __name__ == "__main__":
    unittest.main()

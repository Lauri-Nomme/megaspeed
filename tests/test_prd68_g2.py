"""prd/68 group G2 — sprints, trend and the no-PR backend honesty.

Covers the three server-side findings:

* **F-02** — an event-only sprint has no dates and no state; a future sprint is
  not a measured band, not the active sprint and not a trend bucket;
* **F-11** — PR-only count metrics are unavailable on a Jira-only store, and
  cumulative flow falls back to the story bands already computed;
* **F-07 (backend)** — when Reviewed is unfalsifiable the joint clean count is
  unknown (``None``), never ``0``; the other steps keep their counts.

    python3 -m unittest tests.test_prd68_g2 -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis import ballcourt, jira, rules, trend
from ingest.store import connect
from tests.test_jira_analysis import AS_OF, T0, Fixture


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fx: Fixture):
        fx.write(self.path)
        con = connect(self.path, read_only=True)
        court = ballcourt.load(con)
        data = jira.load(con, court)
        window = rules.window_for(court, None, data)
        return con, court, data, window

    @staticmethod
    def window(data, court):
        start = min(story.start for story in data.stories)
        return start, court.snapshot

    @staticmethod
    def by_sprint(payload: dict, sid: int) -> dict:
        return next(row for row in payload["sprints"] if row["sprint_id"] == sid)


def sprint_fixture() -> Fixture:
    """A closed sprint, the real active sprint, and a story in no SPRINT object."""
    fx = Fixture()
    fx.sprint(1, T0, end=T0 + timedelta(days=14),
              closed=T0 + timedelta(days=14, hours=7))
    fx.sprint(3, datetime(2026, 2, 3, 9, 0), state="active",
              end=datetime(2026, 2, 16))
    fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=20))
    fx.add_to_sprint("ENG-1", T0 + timedelta(hours=1), 1, at_start=True)
    fx.status("ENG-1", T0 + timedelta(hours=2), "in_progress")
    fx.status("ENG-1", T0 + timedelta(hours=20), "done",
              from_status="in_progress")
    # sprint 99 is referenced only by the event: no SPRINT object exists
    fx.add_to_sprint("ENG-1", T0 + timedelta(hours=3), 99)
    return fx


def future_fixture() -> Fixture:
    """The active sprint plus two future sprints (one with a story, one empty)."""
    fx = sprint_fixture()
    future = datetime(2026, 3, 2, 9, 0)
    # a planned sprint with one committed story: its band is start → plan
    fx.sprint(7, future, end=future + timedelta(days=14), state="future")
    fx.story("ENG-2", T0 + timedelta(days=2))
    fx.add_to_sprint("ENG-2", T0 + timedelta(days=2, hours=1), 7)
    # a placeholder with no stories: not a band at all
    fx.sprint(8, future + timedelta(days=20),
              end=future + timedelta(days=34), state="future")
    return fx


def jira_only_cohort(count: int = 12) -> Fixture:
    """Completed stories with the full Jira workflow, and no PR layer at all."""
    fx = Fixture(as_of=AS_OF)
    fx.sprint(1, T0, end=datetime(2026, 2, 2), closed=datetime(2026, 2, 2, 16))
    for index in range(count):
        created = T0 + timedelta(days=index)
        done = created + timedelta(hours=10)
        key = f"DEMO-{100 + index}"
        fx.story(key, created, state="done", closed=done)
        fx.pointed(key, created + timedelta(hours=1), 3)
        fx.status(key, created + timedelta(hours=2), "in_progress")
        fx.status(key, created + timedelta(hours=3), "in_review",
                  from_status="in_progress")
        fx.status(key, done, "done", from_status="in_review")
        fx.add_to_sprint(key, created + timedelta(hours=4), 1)
    return fx


class DatelessSprintTests(StoreCase):
    """prd/68 F-02: membership is real even when the SPRINT object is not."""

    def test_an_event_only_sprint_exposes_unknown_dates_and_state(self):
        con, court, data, _window = self.load(sprint_fixture())
        try:
            payload = jira.sprints(court, data, self.window(data, court))
        finally:
            con.close()
        row = self.by_sprint(payload, 99)
        self.assertIsNone(row["state"])
        self.assertFalse(row["state_known"])
        self.assertFalse(row["date_known"])
        self.assertFalse(row["dates_known"])
        self.assertIsNone(row["band_start"])
        self.assertIsNone(row["band_end"])
        # the membership is not dropped: the lens can still filter to it
        self.assertEqual(row["story_count"], 1)
        self.assertEqual(row["stories"], ["ENG-1"])

    def test_a_real_sprint_reports_known_dates_and_state(self):
        con, court, data, _window = self.load(sprint_fixture())
        try:
            payload = jira.sprints(court, data, self.window(data, court))
        finally:
            con.close()
        first = self.by_sprint(payload, 1)
        self.assertTrue(first["state_known"])
        self.assertTrue(first["date_known"])
        self.assertTrue(first["dates_known"])


class FutureSprintTests(StoreCase):
    """prd/68 F-02: a future sprint has not run and cannot be a band/bucket."""

    def test_a_future_sprint_band_is_its_plan_not_the_snapshot(self):
        con, court, data, _window = self.load(future_fixture())
        try:
            payload = jira.sprints(court, data, self.window(data, court))
        finally:
            con.close()
        sprint = self.by_sprint(payload, 7)
        self.assertEqual(sprint["state"], "future")
        self.assertFalse(sprint["open"])
        # forward range: the planned end, never start → snapshot (inverted)
        self.assertGreater(
            datetime.fromisoformat(sprint["band_end"].replace("Z", "")),
            datetime.fromisoformat(sprint["band_start"].replace("Z", "")),
        )
        self.assertNotEqual(
            sprint["band_end"], payload["window"]["end"],
        )
        # a future sprint is never the store's active sprint
        self.assertEqual(payload["active"], 3)

    def test_a_zero_story_future_placeholder_is_not_a_band(self):
        con, court, data, _window = self.load(future_fixture())
        try:
            payload = jira.sprints(court, data, self.window(data, court))
        finally:
            con.close()
        placeholder = self.by_sprint(payload, 8)
        self.assertEqual(placeholder["story_count"], 0)
        self.assertIsNone(placeholder["band_start"])
        self.assertIsNone(placeholder["band_end"])
        self.assertFalse(placeholder["dates_known"])

    def test_a_future_sprint_is_not_a_trend_bucket_or_the_active_sprint(self):
        con, court, data, window = self.load(future_fixture())
        try:
            payload = trend.trend(
                court, data, window, metric="on_time", bucket="sprint",
            )
        finally:
            con.close()
        labels = [point["label"] for point in payload["points"]]
        self.assertNotIn("S7", labels)
        self.assertNotIn("S8", labels)
        self.assertEqual(payload["active_sprint"]["id"], 3)
        self.assertEqual(payload["active_sprint_note"], "S3 in progress")

    def test_no_active_sprint_says_so_instead_of_naming_a_future_one(self):
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=14),
                  closed=T0 + timedelta(days=14))
        fx.sprint(7, datetime(2026, 3, 2, 9, 0), state="future",
                  end=datetime(2026, 3, 16))
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=20))
        con, court, data, window = self.load(fx)
        try:
            payload = trend.trend(
                court, data, window, metric="on_time", bucket="sprint",
            )
        finally:
            con.close()
        self.assertIsNone(payload["active_sprint"])
        self.assertEqual(payload["active_sprint_note"], "no sprint in progress")
        self.assertNotIn("S7", [point["label"] for point in payload["points"]])


class NoPrTrendTests(StoreCase):
    """prd/68 F-11: an absent PR layer is unavailable, not a measured zero."""

    def test_pr_count_metrics_are_unavailable_with_the_reason(self):
        con, court, data, window = self.load(jira_only_cohort())
        try:
            for metric in ("merged", "open", "arrival_prs"):
                payload = trend.trend(court, data, window, metric=metric)
                self.assertFalse(payload["available"], metric)
                self.assertEqual(
                    payload["unavailable_reason"], "no PR objects in this store"
                )
                self.assertEqual(payload["summary"]["measured_buckets"], 0)
                self.assertEqual(
                    payload["summary"]["unavailable_reason"],
                    "no PR objects in this store",
                )
                self.assertIn("no PR objects in this store", payload["note"])
                for point in payload["points"]:
                    self.assertFalse(point["available"], metric)
                    self.assertIsNone(point["value"], metric)
                    self.assertEqual(point["n"], 0, metric)
        finally:
            con.close()

    def test_cumulative_flow_falls_back_to_the_story_bands(self):
        con, court, data, window = self.load(jira_only_cohort())
        try:
            payload = trend.trend(
                court, data, window, metric="cumulative_flow",
            )
        finally:
            con.close()
        self.assertTrue(payload["available"])
        self.assertIsNone(payload["unavailable_reason"])
        point = payload["points"][-1]
        self.assertEqual(point["population"], "story")
        self.assertEqual(
            [band["id"] for band in point["bands"]],
            ["created", "started", "in_review", "done"],
        )
        self.assertGreater(point["value"], 0)
        # a cumulative series never decreases: created ≥ done at every bucket
        self.assertGreaterEqual(
            point["bands"][0]["value"], point["bands"][-1]["value"],
        )


class JointCleanTests(StoreCase):
    """prd/68 F-07: no measured review step means no joint clean count."""

    def test_a_jira_only_cohort_has_no_joint_clean_instead_of_zero(self):
        fx = jira_only_cohort(10)
        con, court, data, window = self.load(fx)
        try:
            payload = jira.first_pass_yield(court, data, window)
        finally:
            con.close()
        self.assertEqual(payload["n"], 10)
        steps = {step["id"]: step for step in payload["steps"]}
        self.assertFalse(steps["review"]["available"])
        self.assertTrue(steps["review"]["unfalsifiable"])
        self.assertIsNone(steps["review"]["clean_n"])
        # the other steps are measured and keep their full counts
        for step_id in ("refine", "develop", "verify"):
            self.assertTrue(steps[step_id]["available"], step_id)
            self.assertEqual(steps[step_id]["n"], 10, step_id)
            self.assertEqual(steps[step_id]["clean_n"], 10, step_id)
        # the joint count cannot be 0 when one of its steps was not measured
        self.assertIsNone(payload["clean_joint"])
        self.assertIsNone(payload["joint_rate"])
        self.assertIsNone(payload["rolled"])
        self.assertIn("Reviewed is not measured", payload["rolled_reason"])

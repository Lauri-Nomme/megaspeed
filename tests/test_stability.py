"""Stability flags (prd/61): Theil–Sen slopes, Little's Law, arrivals vs exits.

The slope and the flag vocabulary are pure and pinned here (a known +1.0/wk
series; every flag; only ``overload`` refusing a forecast). The store-facing
tests use a synthetic week series for the "insufficient history" branch and the
prd/61 evidence store, when present, for the del-combined ``filling`` pin.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis import ballcourt, calendar, jira, rules, stability, trend
from ingest.store import connect
from tests.test_jira_analysis import Fixture, T0
from tests.test_yield import _del_combined_path


class SlopeTests(unittest.TestCase):
    def test_theil_sen_on_a_known_series(self):
        self.assertEqual(stability.theil_sen([0, 1, 2, 3, 4], [6, 7, 8, 9, 10]), 1.0)
        self.assertEqual(stability.theil_sen([0, 1, 2], [2, 2, 2]), 0.0)
        self.assertIsNone(stability.theil_sen([0], [1]))

    def test_theil_sen_is_robust_to_one_bad_week(self):
        # a flat series with one outlier: the pairwise median stays near zero,
        # which is the whole reason for a rank-based slope.
        self.assertLess(abs(stability.theil_sen([0, 1, 2, 3, 4], [5, 5, 30, 5, 5])), 0.5)


class FlagTests(unittest.TestCase):
    def test_the_four_flags_and_their_precedence(self):
        self.assertEqual(stability.classify(1.0, 0.0)[0], "overload")
        self.assertEqual(stability.classify(1.0, 0.0)[1], "queue grows while exits do not")
        self.assertEqual(stability.classify(1.0, 0.29)[0], "filling")
        self.assertEqual(stability.classify(0.1, 0.4)[0], "stable")
        self.assertEqual(stability.classify(-0.5, 0.2)[0], "draining")
        # WIP and throughput grow together: not an overloading queue, and the
        # reason says so instead of a bare "stable".
        flag, reason = stability.classify(1.0, 0.6)
        self.assertEqual(flag, "stable")
        self.assertEqual(reason, "WIP and throughput grow together")
        self.assertEqual(stability.classify(None, 0.2)[0], "insufficient")

    def test_only_overload_refuses_a_forecast(self):
        # prd/60 reads ``refuses_forecast``; it must be true for overload only.
        flags = [
            stability.classify(wip, throughput)[0]
            for wip, throughput in ((1.0, 0.0), (1.0, 0.29), (0.1, 0.4), (-0.5, 0.2))
        ]
        self.assertEqual(flags, ["overload", "filling", "stable", "draining"])
        self.assertEqual(
            [stability.refuses_forecast(flag) for flag in flags],
            [True, False, False, False],
        )


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fx: Fixture, cal=None):
        fx.write(self.path)
        con = connect(self.path, read_only=True)
        court = ballcourt.load(con, calendar=cal)
        data = jira.load(con, court)
        window = rules.window_for(court, None, data)
        return con, court, data, window


def week_fixture(weeks: int = 5, per_week: int = 2) -> Fixture:
    """One merged PR per item, one week apart, so the bucket series is known."""
    fx = Fixture()
    number = 0
    for week in range(weeks):
        for offset in range(per_week):
            number += 1
            opened = T0 + timedelta(days=7 * week, hours=offset)
            fx.pr(number, "AaAa", opened, closed=opened + timedelta(hours=2))
            fx.pr_event(number, "pr_merged", "RrRr", hours=2)
    return fx


class StoreTests(StoreCase):
    def test_three_buckets_are_insufficient(self):
        con, court, data, window = self.load(week_fixture())
        try:
            bounds = trend.bucket_bounds(court, data, window, "week")[:3]
            payload = stability.payload(court, data, window, bucket="week",
                                        bounds=bounds)
        finally:
            con.close()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["flag"], "insufficient")
        self.assertFalse(payload["refuses_forecast"])
        self.assertIn("at least 4", payload["note"])

    def test_dead_wip_when_the_implied_cycle_dwarfs_the_measured_one(self):
        con, court, data, window = self.load(week_fixture())
        try:
            little = stability._little(court, data, window, 5.0, 1.0)
        finally:
            con.close()
        self.assertEqual(little["flag"], "dead_wip")
        self.assertGreater(little["ratio"], 2)
        self.assertIsNotNone(little["note"])
        self.assertIn("Little's Law", little["note"])

    def test_a_flat_queue_is_stable_and_does_not_refuse_a_forecast(self):
        con, court, data, window = self.load(week_fixture(weeks=6, per_week=2))
        try:
            payload = stability.payload(court, data, window, bucket="week")
        finally:
            con.close()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["flag"], "stable")
        self.assertFalse(payload["refuses_forecast"])
        self.assertEqual(payload["wip"]["slope_per_week"], 0.0)

    def test_leading_empty_buckets_are_not_measured_as_zero_growth(self):
        # the analysis window's first Monday is before the first PR; that empty
        # bucket must not bend the slope towards "growth from nothing".
        con, court, data, window = self.load(week_fixture(weeks=5, per_week=2))
        try:
            payload = stability.payload(court, data, window, bucket="week")
        finally:
            con.close()
        first_pr_week = T0.replace(hour=0, minute=0, second=0, microsecond=0) \
            - timedelta(days=T0.weekday())
        self.assertGreaterEqual(payload["leading_empty"], 0)
        self.assertEqual(payload["buckets"], len(
            [row for row in payload["points"] if not row["in_progress"]]
        ) - payload["leading_empty"])
        self.assertGreaterEqual(payload["window"]["start"][:10],
                                first_pr_week.date().isoformat())


@unittest.skipUnless(_del_combined_path() is not None,
                     "the del-combined store is not present")
class DelCombinedStabilityTests(unittest.TestCase):
    """The prd/61 evidence: WIP +1.0/wk, throughput +0.3/wk, filling."""

    @classmethod
    def setUpClass(cls):
        path = _del_combined_path()
        cls.calendar = calendar.load(
            config_path=Path(__file__).resolve().parent.parent / "config"
            / "calendar.default.yaml"
        )
        con = connect(path, read_only=True)
        try:
            cls.court = ballcourt.load(con, calendar=cls.calendar)
            cls.data = jira.load(con, cls.court)
        finally:
            con.close()
        cls.window = rules.window_for(cls.court, None, cls.data)

    def test_the_stability_strip_flags_filling(self):
        payload = stability.payload(self.court, self.data, self.window)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["flag"], "filling")
        self.assertFalse(payload["refuses_forecast"])
        self.assertAlmostEqual(payload["wip"]["slope_per_week"], 1.0, places=2)
        self.assertEqual(payload["wip"]["from"], 6)
        self.assertAlmostEqual(payload["throughput"]["slope_per_week"], 0.29,
                               places=2)
        self.assertAlmostEqual(payload["throughput"]["mean"], 8.15, places=1)
        self.assertEqual(payload["arrivals_departures"]["gap_items"], 25)
        self.assertIn("Little's Law says cycle time will rise", payload["note"])

    def test_little_law_diverges_and_names_dead_wip(self):
        payload = stability.payload(self.court, self.data, self.window)
        little = payload["little"]
        self.assertEqual(little["flag"], "dead_wip")
        self.assertGreaterEqual(little["ratio"], 2)
        self.assertAlmostEqual(little["implied_weeks"], 1.4, places=1)
        self.assertIn("no live PR", little["note"])


if __name__ == "__main__":
    unittest.main()

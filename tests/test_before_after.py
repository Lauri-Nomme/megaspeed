"""Before/after comparison (prd/61): four outcomes, a CI and honest censoring.

The statistics are tested directly (a seeded bootstrap, an exact Mann–Whitney
null), and the rule order is tested through ``classify`` so every outcome is
reachable without a store. The store-facing test proves the cohort basis: items
created either side of the split, open items censored, the in-progress bucket
excluded from both sides, and two annotations in one bucket confounding.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import math
import random
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis import ballcourt, before_after, jira, rules
from ingest.store import connect
from tests.test_jira_analysis import Fixture

SPLIT = datetime(2026, 2, 2)  # the Monday of the fifth settled week
START = datetime(2026, 1, 5, 9)


# --------------------------------------------------------------------------- #
# a dense synthetic store: three PRs and three stories per week, 7 weeks
# --------------------------------------------------------------------------- #
def comparison_fixture(before_hours: float = 24.0, after_hours: float = 12.0,
                       per_week: int = 3, weeks: int = 7,
                       open_after: int = 0) -> Fixture:
    fx = Fixture()
    number = 0
    for week in range(weeks):
        for offset in range(per_week):
            number += 1
            opened = START + timedelta(days=7 * week, hours=offset)
            hours = after_hours if week >= 4 else before_hours
            fx.pr(number, "AaAa", opened, closed=opened + timedelta(hours=hours))
            fx.pr_event(number, "pr_merged", "RrRr", hours=hours)
            # the story clock is its own population (created → done)
            key = f"ENG-{number}"
            fx.story(key, opened, state="done", closed=opened + timedelta(hours=hours))
            fx.status(key, opened + timedelta(hours=hours), "done")
    for extra in range(open_after):
        number += 1
        opened = START + timedelta(days=7 * 5, hours=6 + extra)
        fx.pr(number, "AaAa", opened, state="open", closed=None)
        fx.pr_event(number, "comment_added", "RrRr", hours=0)
    return fx


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


# --------------------------------------------------------------------------- #
# 1. the statistics
# --------------------------------------------------------------------------- #
class StatisticsTests(unittest.TestCase):
    def test_mann_whitney_exact_p_on_a_fully_separated_small_sample(self):
        # 8 vs 8, no overlap: U = 0 and the exact two-sided p is 2 / C(16, 8).
        result = before_after.mann_whitney(range(1, 9), range(9, 17))
        self.assertEqual(result["u"], 0.0)
        self.assertEqual(result["method"], "exact")
        self.assertEqual(result["p_value"], round(2 / math.comb(16, 8), 4))
        # a tie makes the exact table inapplicable; the normal approximation
        # still answers and says which method answered.
        tied = before_after.mann_whitney([1, 2, 2, 3, 4, 5, 6, 7],
                                         [2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(tied["method"], "normal")
        self.assertTrue(0 < tied["p_value"] <= 1)

    def test_the_bootstrap_is_deterministic_for_one_seed(self):
        before = [90, 92, 95, 98, 100, 101, 104, 108, 110, 120]
        after = [70, 72, 75, 80, 82, 84, 88, 90, 95, 99]
        first = before_after.bootstrap_ci(before, after, seed=42, trials=800)
        second = before_after.bootstrap_ci(before, after, seed=42, trials=800)
        self.assertEqual(first, second)
        self.assertLess(first[0], first[1])

    def test_the_ci_covers_the_true_20_percent_shift(self):
        # 200 simulated datasets; the true difference of medians is 20 h. A 95 %
        # percentile bootstrap should contain it in about 95 % of them. The
        # assertion is deliberately a band, not an exact count: this is a
        # coverage check of the estimator, not a fixture.
        rng = random.Random(20261008)
        covered = 0
        datasets = 200
        trials = 400
        for _ in range(datasets):
            before = [rng.gauss(100, 12) for _ in range(30)]
            after = [rng.gauss(120, 12) for _ in range(30)]
            ci = before_after.bootstrap_ci(before, after, seed=_, trials=trials)
            if ci[0] <= 20 <= ci[1]:
                covered += 1
        share = covered / datasets
        self.assertGreaterEqual(share, 0.90, f"coverage {share:.3f} is too low")
        self.assertLessEqual(share, 0.995, f"coverage {share:.3f} is implausible")

    def test_the_lower_median_is_an_observed_item(self):
        self.assertEqual(before_after.median([1, 2, 3, 4]), 2)
        self.assertEqual(before_after.median([1, 2, 3]), 2)
        self.assertEqual(before_after.median([]), None)
        # p85 uses the nearest-rank convention the headline's cycle_time uses
        self.assertEqual(before_after.percentile([1, 2, 3, 4], 0.85), 4)


# --------------------------------------------------------------------------- #
# 2. the four outcomes
# --------------------------------------------------------------------------- #
class OutcomeTests(unittest.TestCase):
    def test_better_and_worse_follow_higher_is_better(self):
        better = before_after.classify(
            n_before=20, n_after=20, median_before=24, median_after=12,
            ci=[-15, -9], higher_is_better=False,
        )
        self.assertEqual((better["outcome"], better["reason"]),
                         ("better", "interval_excludes_zero"))
        worse = before_after.classify(
            n_before=20, n_after=20, median_before=12, median_after=24,
            ci=[9, 15], higher_is_better=False,
        )
        self.assertEqual((worse["outcome"], worse["reason"]),
                         ("worse", "interval_excludes_zero"))
        # a metric where higher is better flips the verdict, not the interval
        self.assertEqual(
            before_after.classify(
                n_before=20, n_after=20, median_before=0.2, median_after=0.4,
                ci=[0.1, 0.25], higher_is_better=True,
            )["outcome"],
            "better",
        )

    def test_no_clear_change_is_a_tight_interval_around_zero(self):
        verdict = before_after.classify(
            n_before=63, n_after=43, median_before=24.79, median_after=24.74,
            ci=[-3.49, 3.63], higher_is_better=False,
        )
        self.assertEqual((verdict["outcome"], verdict["reason"]),
                         ("no_clear_change", "tight_around_zero"))

    def test_all_cant_tell_reasons_are_reachable(self):
        small = before_after.classify(
            n_before=5, n_after=20, median_before=24, median_after=12,
            ci=None, higher_is_better=False,
        )
        self.assertEqual((small["outcome"], small["reason"]), ("cant_tell", "small_n"))
        short = before_after.classify(
            n_before=20, n_after=20, median_before=24, median_after=12,
            ci=[-15, -9], higher_is_better=False,
            buckets_before=7, buckets_after=2,
        )
        self.assertEqual((short["outcome"], short["reason"]),
                         ("cant_tell", "short_window"))
        confounded = before_after.classify(
            n_before=20, n_after=20, median_before=24, median_after=12,
            ci=[-15, -9], higher_is_better=False,
            confounder="WIP limit 6 is in the same week as this split",
        )
        self.assertEqual((confounded["outcome"], confounded["reason"]),
                         ("cant_tell", "confounded"))
        wide = before_after.classify(
            n_before=62, n_after=25, median_before=92.91, median_after=83.0,
            ci=[-23.14, 5.96], higher_is_better=False,
        )
        self.assertEqual((wide["outcome"], wide["reason"]),
                         ("cant_tell", "wide_interval"))

    def test_the_noise_band_boundary_is_exactly_twenty_percent(self):
        # 100 → 120 is exactly +20 %: the band is *below* 20 %, so it is not a
        # "no clear change" — it needs an interval that excludes zero, else the
        # honest answer is that the data cannot tell.
        at_boundary = before_after.classify(
            n_before=20, n_after=20, median_before=100, median_after=120,
            ci=[-1, 21], higher_is_better=False,
        )
        self.assertEqual(at_boundary["outcome"], "cant_tell")
        inside = before_after.classify(
            n_before=20, n_after=20, median_before=100, median_after=119.9,
            ci=[-1, 19], higher_is_better=False,
        )
        self.assertEqual(inside["outcome"], "no_clear_change")

    def test_compare_carries_both_ns_the_ci_and_the_outcome(self):
        payload = before_after.compare(
            [24] * 12, [12] * 12, higher_is_better=False, seed=7, trials=300,
            metric="cycle_median", unit="hours",
        )
        self.assertEqual(payload["before"]["n"], 12)
        self.assertEqual(payload["after"]["n"], 12)
        self.assertEqual(payload["ci"], [-12, -12])
        self.assertEqual(payload["outcome"], "better")
        self.assertEqual(payload["test"], "mann_whitney_u")
        self.assertIn("better", payload["note"].lower())
        # below MIN_ITEMS there is no interval to draw, and the payload says so
        thin = before_after.compare([24] * 5, [12] * 5, higher_is_better=False)
        self.assertIsNone(thin["ci"])
        self.assertEqual(thin["reason"], "small_n")


# --------------------------------------------------------------------------- #
# 3. the store-facing comparison
# --------------------------------------------------------------------------- #
class CompareStoreTests(StoreCase):
    def test_items_are_split_by_creation_and_the_story_clock_is_its_own_population(self):
        con, court, data, window = self.load(comparison_fixture())
        try:
            story = before_after.compare_metric(
                court, data, window, metric="story_cycle", split=SPLIT,
                bucket="week", trials=400,
            )
            pr = before_after.compare_metric(
                court, data, window, metric="cycle_median", split=SPLIT,
                bucket="week", trials=400,
            )
        finally:
            con.close()
        self.assertEqual(story["before"]["n"], 12)
        self.assertEqual(story["after"]["n"], 9)
        self.assertEqual(story["before"]["p50"], 24.0)
        self.assertEqual(story["after"]["p50"], 12.0)
        self.assertEqual(story["outcome"], "better")
        self.assertIn("created → done", story["basis"])
        self.assertEqual(pr["before"]["n"], 12)
        self.assertEqual(pr["after"]["n"], 9)
        self.assertIn("created → merged", pr["basis"])
        self.assertGreaterEqual(story["buckets_before"], 3)
        self.assertGreaterEqual(story["buckets_after"], 3)

    def test_open_items_are_excluded_from_both_sides_and_counted(self):
        con, court, data, window = self.load(comparison_fixture(open_after=2))
        try:
            payload = before_after.compare_metric(
                court, data, window, metric="cycle_median", split=SPLIT,
                bucket="week", trials=300,
            )
        finally:
            con.close()
        self.assertEqual(payload["after"]["n"], 9)
        self.assertEqual(payload["censored_after"], 2)
        self.assertEqual(payload["censored"], 2)
        # N-09: the panel prints the disclosure once, from the structured
        # counts; the verdict note must not repeat it.
        self.assertNotIn("excluded and counted", payload["note"])

    def test_censored_items_on_both_sides_are_structured_not_repeated_in_note(self):
        fx = comparison_fixture(open_after=2)
        opened = START + timedelta(days=7 * 2, hours=8)
        fx.pr(98, "AaAa", opened, state="open", closed=None)
        fx.pr_event(98, "comment_added", "RrRr", hours=0)
        con, court, data, window = self.load(fx)
        try:
            payload = before_after.compare_metric(
                court, data, window, metric="cycle_median", split=SPLIT,
                bucket="week", trials=300,
            )
        finally:
            con.close()
        self.assertEqual(payload["censored_before"], 1)
        self.assertEqual(payload["censored_after"], 2)
        self.assertEqual(payload["censored"], 3)
        self.assertNotIn("excluded and counted", payload["note"])

    def test_no_censoring_leaves_the_note_a_plain_verdict(self):
        con, court, data, window = self.load(comparison_fixture())
        try:
            payload = before_after.compare_metric(
                court, data, window, metric="cycle_median", split=SPLIT,
                bucket="week", trials=300,
            )
        finally:
            con.close()
        self.assertEqual(payload["censored"], 0)
        self.assertNotIn("excluded and counted", payload["note"])
        self.assertIn("better", payload["note"].lower())

    def test_the_in_progress_bucket_never_appears_on_either_side(self):
        fx = comparison_fixture()
        # one PR created inside the bucket containing the snapshot
        opened = datetime(2026, 2, 23, 9)
        fx.pr(99, "AaAa", opened, closed=opened + timedelta(hours=4))
        fx.pr_event(99, "pr_merged", "RrRr", hours=4)
        con, court, data, window = self.load(fx)
        try:
            payload = before_after.compare_metric(
                court, data, window, metric="cycle_median", split=SPLIT,
                bucket="week", trials=300,
            )
        finally:
            con.close()
        self.assertEqual(payload["excluded_in_progress"], 1)
        self.assertEqual(payload["after"]["n"], 9)

    def test_a_second_annotation_in_the_same_bucket_forces_cant_tell(self):
        con, court, data, window = self.load(comparison_fixture())
        entries = [
            {"id": "a1", "date": "2026-02-02", "kind": "review_sla",
             "value": 4, "label": "review SLA 4 h"},
            {"id": "a2", "date": "2026-02-04", "kind": "wip_limit",
             "value": 6, "label": "WIP limit 6"},
        ]
        try:
            payload = before_after.compare_metric(
                court, data, window, metric="cycle_median", split=SPLIT,
                bucket="week", annotations=entries, selected_id="a1",
                trials=300,
            )
        finally:
            con.close()
        self.assertEqual(payload["outcome"], "cant_tell")
        self.assertEqual(payload["reason"], "confounded")
        self.assertIn("WIP limit 6", payload["note"])
        self.assertEqual(payload["annotation"]["label"], "review SLA 4 h")

    def test_a_one_week_window_is_a_stated_short_window(self):
        # 10 items per week: each side of a one-week split has enough *items*,
        # but only one bucket — the short-window rule, not the small-n rule.
        con, court, data, window = self.load(comparison_fixture(per_week=20))
        try:
            # one settled week (16–23 Feb), split mid-week: both sides have
            # plenty of items but only one bucket each.
            narrow = (START + timedelta(days=7 * 6), START + timedelta(days=7 * 7))
            week = before_after.compare_metric(
                court, data, narrow, metric="story_cycle",
                split=START + timedelta(days=7 * 6, hours=10),
                bucket="week", trials=300,
            )
        finally:
            con.close()
        self.assertEqual((week["outcome"], week["reason"]),
                         ("cant_tell", "short_window"))
        self.assertGreaterEqual(week["before"]["n"], 8)
        self.assertGreaterEqual(week["after"]["n"], 8)
        self.assertIsNone(week["ci"])

    def test_the_seed_is_derived_from_the_cache_key(self):
        one = before_after.seed_for("del-combined", 1, "cal", "cycle_median",
                                    SPLIT.isoformat(), "week")
        two = before_after.seed_for("del-combined", 1, "cal", "cycle_median",
                                    SPLIT.isoformat(), "week")
        other = before_after.seed_for("del-combined", 2, "cal", "cycle_median",
                                      SPLIT.isoformat(), "week")
        self.assertEqual(one, two)
        self.assertNotEqual(one, other)


if __name__ == "__main__":
    unittest.main()

"""Kaplan–Meier survival analysis for censored cycle times (prd/05, goal 7).

The estimator is tested on numbers alone, so the arithmetic is pinned
independently of the store: the plain median without censoring, the risk set
when items are censored, the tie convention (an event at an instant precedes a
censoring at that instant), a median that is never reached, and the downsampled
curve. The metric that feeds it from PR timestamps is tested in
``test_flow_survival.py``.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from analysis.survival import Observation, kaplan_meier


class KaplanMeierTests(unittest.TestCase):
    def test_without_censoring_it_is_the_plain_median(self):
        fit = kaplan_meier([
            Observation(hours, True) for hours in (10, 20, 30, 40)
        ])
        self.assertEqual(fit["n"], 4)
        self.assertEqual(fit["events"], 4)
        self.assertEqual(fit["censored"], 0)
        self.assertEqual(fit["median_hours"], 20.0)
        self.assertEqual(fit["quantiles"], {"p25": 10.0, "p50": 20.0, "p75": 30.0})
        self.assertEqual(
            [step["survival"] for step in fit["curve"]],
            [0.75, 0.5, 0.25, 0.0],
        )

    def test_censoring_keeps_the_item_at_risk_but_never_counts_it_as_a_merge(self):
        # 10 merged, 20 censored, 30 merged, 40 merged
        fit = kaplan_meier([
            Observation(10, True),
            Observation(20, False),
            Observation(30, True),
            Observation(40, True),
        ])
        self.assertEqual(fit["n"], 4)
        self.assertEqual(fit["events"], 3)
        self.assertEqual(fit["censored"], 1)
        # 1 − (1/4) then 1 − (1/2): the censored item leaves the risk set silently
        self.assertEqual(
            [step["survival"] for step in fit["curve"]], [0.75, 0.375, 0.0]
        )
        self.assertEqual([step["at_risk"] for step in fit["curve"]], [4, 2, 1])
        self.assertEqual(fit["median_hours"], 30.0)

    def test_an_event_at_the_same_time_as_a_censoring_counts_the_event_first(self):
        fit = kaplan_meier([
            Observation(10, True),
            Observation(20, True),
            Observation(20, False),
            Observation(30, True),
        ])
        # at 20: at risk 3 (both items), one merge -> 1/3, then the censored leaves
        self.assertEqual(fit["curve"][1]["at_risk"], 3)
        self.assertEqual(fit["curve"][1]["events"], 1)
        self.assertEqual(fit["curve"][1]["censored"], 1)
        # 0.75 at 10 h, then (1 − 1/3) of it at 20 h
        self.assertAlmostEqual(fit["curve"][1]["survival"], 0.5, places=4)
        self.assertEqual(fit["median_hours"], 20.0)

    def test_a_median_that_is_never_reached_is_reported_as_such(self):
        observations = [Observation(hours, True) for hours in (10, 20, 30)]
        observations += [Observation(40, False) for _ in range(7)]
        fit = kaplan_meier(observations)
        self.assertEqual(fit["n"], 10)
        self.assertEqual(fit["events"], 3)
        self.assertIsNone(fit["median_hours"])
        self.assertFalse(fit["median_reached"])
        # 0.9 → 0.8 → 0.7: the lower quartile is reached, the median is not
        self.assertEqual(fit["quantiles"]["p25"], 30.0)
        self.assertIsNone(fit["quantiles"]["p50"])
        self.assertIsNone(fit["quantiles"]["p75"])

    def test_the_curve_is_downsampled_but_keeps_the_median_step(self):
        observations = [Observation(float(hours), True) for hours in range(1, 101)]
        fit = kaplan_meier(observations, max_points=10)
        self.assertLessEqual(len(fit["curve"]), 11)
        self.assertEqual(fit["curve"][0]["hours"], 1.0)
        self.assertEqual(fit["curve"][-1]["hours"], 100.0)
        self.assertIn(50.0, [step["hours"] for step in fit["curve"]])
        self.assertEqual(fit["median_hours"], 50.0)

    def test_an_empty_sample_is_empty_not_an_error(self):
        fit = kaplan_meier([])
        self.assertEqual(fit["n"], 0)
        self.assertIsNone(fit["median_hours"])
        self.assertEqual(fit["curve"], [])


if __name__ == "__main__":
    unittest.main()

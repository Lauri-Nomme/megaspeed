"""prd/53 §3 — the worklog-vs-points gap over time.

"Do worklogs look like effort *now*" is one question; "is logging getting more
trustworthy?" is the one a lead actually asks before relying on the number. The
metric buckets the same points ↔ logged-hours correlation by the week the story
started, so the answer has a direction.

The regression pins the *recompute*: for hand-built stories across three weeks
the API's per-bucket correlation, median hours per point and median logged hours
must equal a Pearson/median computed independently from the same inputs.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from tests.test_jira_analysis import Fixture, StoreCase, T0, by_id


def pearson(pairs: list[tuple[float, float]]) -> float | None:
    """The textbook Pearson r, computed here so the test does not reuse the code."""
    if len(pairs) < 2:
        return None
    xs = [float(pair[0]) for pair in pairs]
    ys = [float(pair[1]) for pair in pairs]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    varx = sum((x - mx) ** 2 for x in xs)
    vary = sum((y - my) ** 2 for y in ys)
    if varx <= 0 or vary <= 0:
        return None
    return cov / (varx * vary) ** 0.5


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, 2)


#: (week offset in days, key, points, logged hours). Week 1 logs *more* on the
#: small story (r = −1), week 2 logs more on the big one (r = +1), week 3 has a
#: single story (no r — a gap on the axis, not a zero). Three stories a week
#: because a two-point correlation is degenerate (±1 by construction).
STORIES = [
    (0, "ENG-1", 1, 5.0),
    (0, "ENG-2", 3, 3.0),
    (0, "ENG-3", 5, 1.0),
    (7, "ENG-4", 1, 1.0),
    (7, "ENG-5", 3, 3.0),
    (7, "ENG-6", 5, 5.0),
    (14, "ENG-7", 3, 4.0),
]


class WorklogTrendTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=21))
        for offset, key, points, hours in STORIES:
            created = T0 + timedelta(days=offset, hours=1)
            fx.story(key, created, points=points)
            fx.worklog(key, created + timedelta(hours=2), hours)
        return fx

    def test_the_weekly_buckets_match_a_manual_recompute(self):
        metric = self.metric(self.fixture(), "worklog_cross_check")
        trend = metric["trend"]
        # one dense bucket per week between the first and the last story
        self.assertEqual(
            [row["week_start"][:10] for row in trend],
            ["2026-01-05", "2026-01-12", "2026-01-19"],
        )
        self.assertEqual([row["stories"] for row in trend], [3, 3, 1])
        self.assertEqual([row["current"] for row in trend], [False, False, True])

        by_week: dict[str, list[tuple[float, float]]] = {}
        for offset, _key, points, hours in STORIES:
            week = (T0 + timedelta(days=offset)).strftime("%Y-%m-%d")
            by_week.setdefault(week, []).append((float(points), float(hours)))

        for row in trend:
            pairs = by_week[row["label"]]
            expected = pearson(pairs)
            self.assertEqual(
                row["correlation"],
                None if expected is None else round(expected, 3),
            )
            self.assertEqual(
                row["median_hours_per_point"],
                median([hours / points for points, hours in pairs if points > 0]),
            )
            self.assertEqual(
                row["median_logged_hours"], median([hours for _points, hours in pairs])
            )
            self.assertEqual(
                row["logged_hours"], round(sum(hours for _points, hours in pairs), 2)
            )

        # the two planted weeks are the extremes; the verdict is a direction
        self.assertEqual(trend[0]["correlation"], -1.0)
        self.assertEqual(trend[1]["correlation"], 1.0)
        self.assertIsNone(trend[2]["correlation"])

    def test_the_overall_gap_and_the_card_evidence_agree(self):
        payload = self.payload(self.fixture())
        metric = payload["metrics"]["worklog_cross_check"]
        pairs = [(float(points), float(hours)) for _o, _k, points, hours in STORIES]
        self.assertEqual(
            round(metric["points_logged_correlation"], 6),
            round(pearson(pairs), 6),
        )
        self.assertEqual(
            metric["median_hours_per_point"],
            median([hours / points for points, hours in pairs]),
        )
        self.assertEqual(
            metric["median_logged_hours"], median([hours for _p, hours in pairs])
        )
        # the flat correlation means the effort-gap headline, not lateness
        suggestion = by_id(payload, "jira-worklog-mismatch")
        self.assertIsNotNone(suggestion)
        self.assertIn("don't track story points", suggestion["title"])
        self.assertEqual(
            suggestion["evidence"]["median_hours_per_point"],
            metric["median_hours_per_point"],
        )
        self.assertEqual(
            suggestion["evidence"]["median_logged_hours"],
            metric["median_logged_hours"],
        )


if __name__ == "__main__":
    unittest.main()

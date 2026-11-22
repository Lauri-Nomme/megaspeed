"""Progress-line wording and cadence (prd/14).

The reporter is shared by both scrape halves, so the noun must come from the
caller: Bitbucket walks **PRs**, Jira walks **issues**. A Jira run used to print
"57 PRs seen", which reads as if it had scraped pull requests.

    python3 -m unittest tests.test_progress -v
"""

from __future__ import annotations

import io
import unittest

from ingest.progress import ProgressReporter, format_elapsed


class ProgressLineTests(unittest.TestCase):
    def reporter(self) -> ProgressReporter:
        return ProgressReporter(stream=io.StringIO(), interval=0)

    def line(self, **kwargs) -> str:
        return self.reporter().line(
            prs_seen=57, prs_kept=57, prs_skipped=0, requests=90, events=402,
            elapsed=30, **kwargs,
        )

    def test_the_bitbucket_half_calls_them_prs(self):
        text = self.line()
        self.assertIn("57 PRs seen", text)
        self.assertIn("1.9 PR/s", text)
        self.assertNotIn("issue", text)

    def test_the_jira_half_calls_them_issues(self):
        text = self.line(unit="issue")
        self.assertIn("57 issues seen", text)
        self.assertIn("1.9 issue/s", text)
        self.assertNotIn("PRs", text)
        self.assertNotIn("PR/s", text)

    def test_a_plural_unit_is_not_doubled(self):
        self.assertIn("57 stories seen", self.line(unit="stories"))


class TickTests(unittest.TestCase):
    def test_the_interval_gate_and_the_noun(self):
        now = [100.0]
        stream = io.StringIO()
        reporter = ProgressReporter(stream=stream, interval=30, clock=lambda: now[0])
        counts = dict(prs_seen=10, prs_kept=10, prs_skipped=0, requests=12, events=30)

        self.assertFalse(reporter.tick(unit="issue", **counts))   # too early
        now[0] += 31
        self.assertTrue(reporter.tick(unit="issue", **counts))
        self.assertIn("10 issues seen", stream.getvalue())

    def test_a_disabled_reporter_prints_nothing(self):
        stream = io.StringIO()
        reporter = ProgressReporter(stream=stream, interval=0)
        self.assertFalse(reporter.tick(
            prs_seen=1, prs_kept=1, prs_skipped=0, requests=1, events=1
        ))
        self.assertEqual(stream.getvalue(), "")


class ElapsedTests(unittest.TestCase):
    def test_the_clock_shape(self):
        self.assertEqual(format_elapsed(0), "00:00")
        self.assertEqual(format_elapsed(135), "02:15")
        self.assertEqual(format_elapsed(3725), "1:02:05")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Progress reporting on stderr (prd/14 "Progress reporting").

A huge scrape must show that it is alive without a line per PR, and the line
must never contaminate the machine-readable output: progress goes to **stderr**,
the tgz and stdout stay clean, and the cadence is time-based (interval-gated).

    python3 -m unittest discover -s scrape/tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ingest.progress import DEFAULT_INTERVAL, ProgressReporter, format_elapsed
from ingest.scrape_cli import main


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ProgressReporterTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.clock = FakeClock()
        self.reporter = ProgressReporter(self.stream, interval=30, clock=self.clock)

    def tick(self, **counts) -> bool:
        counts.setdefault("prs_seen", 0)
        counts.setdefault("prs_kept", 0)
        counts.setdefault("prs_skipped", 0)
        counts.setdefault("requests", 0)
        counts.setdefault("events", 0)
        return self.reporter.tick(**counts)

    def lines(self) -> list[str]:
        return [line for line in self.stream.getvalue().splitlines() if line]

    def test_nothing_is_printed_before_the_interval(self):
        self.tick(prs_seen=5)
        self.clock.advance(29.9)
        self.tick(prs_seen=6)
        self.assertEqual(self.lines(), [])

    def test_one_line_per_interval_not_per_pr(self):
        for seen in range(1, 200):  # 199 ticks, no time passing
            self.clock.advance(0.1)
            self.tick(prs_seen=seen)
        # ~19.9 s of ticks -> still nothing printed
        self.assertEqual(self.lines(), [])

        self.clock.advance(11)  # crosses 30 s
        self.assertTrue(self.tick(prs_seen=200))
        self.clock.advance(1)
        self.assertFalse(self.tick(prs_seen=201))  # only 1 s later
        self.clock.advance(30)
        self.assertTrue(self.tick(prs_seen=202))
        self.assertEqual(len(self.lines()), 2)

    def test_line_fields_and_throughput(self):
        self.clock.advance(135)
        self.tick(prs_seen=3214, prs_kept=412, prs_skipped=2802, requests=1041, events=4120)
        line = self.lines()[0]
        # Every emitted line carries a UTC stamp so it can be aligned with the
        # model/server log; the payload after it is unchanged.
        self.assertRegex(line, r"^\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z\] ")
        self.assertEqual(
            line.split("] ", 1)[1],
            "[scrape] 02:15 elapsed · 3,214 PRs seen · 412 kept · 2,802 skipped"
            " · 1,041 req · 4,120 events · 23.8 PR/s · 7.7 req/s",
        )

    def test_zero_interval_disables_reporting(self):
        silent = ProgressReporter(self.stream, interval=0, clock=self.clock)
        self.assertFalse(silent.enabled)
        self.clock.advance(10_000)
        self.assertFalse(
            silent.tick(prs_seen=1, prs_kept=1, prs_skipped=0, requests=1, events=1)
        )
        self.assertEqual(self.lines(), [])

    def test_elapsed_formatting(self):
        self.assertEqual(format_elapsed(0), "00:00")
        self.assertEqual(format_elapsed(135), "02:15")
        self.assertEqual(format_elapsed(3725), "1:02:05")

    def test_default_interval_is_30_seconds(self):
        self.assertEqual(DEFAULT_INTERVAL, 30.0)


class ProgressCliTests(unittest.TestCase):
    """The CLI wiring: stderr only, `--quiet` and `--progress-interval 0` silent."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {
            "MEGASPEED_REPOS": "ENG/platform,ENG/mobile",
            "MEGASPEED_TICKET_PROJECTS": "PROJ,OPS,ARCH",
            "MEGASPEED_IDENTITY": str(self.root / "identity.json"),
        }
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main([
                "--fixture", "--n", "5",
                "--out", str(self.root / "out.tgz"),
                "--state-dir", str(self.root / "out.state"),
                "--json",
                *argv,
            ])
        return code, out.getvalue(), err.getvalue()

    def test_tiny_interval_reports_to_stderr_only(self):
        code, out, err = self.run_cli("--progress-interval", "0.000001")
        self.assertEqual(code, 0, err)
        self.assertIn("[scrape]", err)
        self.assertNotIn("[scrape]", out)
        report = json.loads(out)  # stdout stayed a single machine-readable document
        self.assertEqual(report["mode"], "scrape")
        line = next(line for line in err.splitlines() if "[scrape]" in line)
        self.assertRegex(
            line,
            r"^\[\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z\] \[scrape\]"
            r" \d\d:\d\d elapsed · [\d,]+ PRs seen · [\d,]+ kept"
            r" · [\d,]+ skipped · [\d,]+ req · [\d,]+ events"
            r" · [\d.]+ PR/s · [\d.]+ req/s$",
        )

    def test_quiet_is_silent(self):
        code, out, err = self.run_cli("--progress-interval", "0.000001", "--quiet")
        self.assertEqual(code, 0, err)
        self.assertNotIn("[scrape]", err)
        self.assertEqual(json.loads(out)["mode"], "scrape")

    def test_zero_interval_is_silent(self):
        code, out, err = self.run_cli("--progress-interval", "0")
        self.assertEqual(code, 0, err)
        self.assertNotIn("[scrape]", err)
        self.assertEqual(json.loads(out)["mode"], "scrape")

    def test_negative_interval_is_a_configuration_error(self):
        code, _out, err = self.run_cli("--progress-interval", "-1")
        self.assertEqual(code, 2)
        self.assertIn("--progress-interval must be >= 0", err)

    def test_the_default_run_prints_no_progress(self):
        # A small fixture scrape finishes well inside the 30 s interval.
        code, out, err = self.run_cli()
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["mode"], "scrape")


if __name__ == "__main__":
    unittest.main()

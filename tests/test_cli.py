"""--fixture-file is bounded by the bundle, not by the generator default (BUG-03).

``--fixture-file`` also set ``source="fixture"``, so the 50-PR fixture default
silently truncated a 60-PR bundle: only 50 PRs were read (9 never touched) and
the summary said ``51 seen`` because the limit check fired after the counter
incremented, breaking ``kept + dropped == seen``. Nothing warned.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ingest import fixtures
from ingest.__main__ import main

PRS = 55


class FixtureFileLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.bundle_path = fixtures.write_bundle(cls.root / "bundle.json", n=PRS, seed=3)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.db = self.root / f"store-{self._testMethodName}.duckdb"
        self.env = mock.patch.dict(
            os.environ,
            {
                "MEGASPEED_DB": str(self.db),
                "MEGASPEED_IDENTITY": str(self.root / f"id-{self._testMethodName}.json"),
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def ingest(self, *argv: str) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--fixture-file", str(self.bundle_path), "--db", str(self.db), "--json", *argv])
        payload = json.loads(out.getvalue()) if out.getvalue().strip() else {}
        return code, payload, err.getvalue()

    def test_bundle_is_replayed_in_full(self):
        code, report, err = self.ingest()
        self.assertEqual(code, 0, err)
        self.assertEqual(report["prs_seen"], PRS)
        self.assertEqual(report["prs_kept"] + report["prs_dropped"], report["prs_seen"])
        self.assertEqual(err, "")

    def test_explicit_n_caps_but_warns_when_it_exceeds_the_file(self):
        code, report, err = self.ingest("--n", str(PRS + 20))
        self.assertEqual(code, 0, err)
        self.assertEqual(report["prs_seen"], PRS)
        self.assertEqual(report["prs_kept"] + report["prs_dropped"], report["prs_seen"])
        self.assertIn("exceeds", err)
        self.assertIn(str(PRS), err)

    def test_explicit_n_within_the_file_is_honoured(self):
        code, report, err = self.ingest("--n", "7")
        self.assertEqual(code, 0, err)
        self.assertEqual(report["prs_seen"], 7)
        self.assertEqual(report["prs_kept"] + report["prs_dropped"], 7)
        self.assertEqual(err, "")

    def test_generated_fixture_honours_n_exactly(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--fixture", "--n", "5", "--db", str(self.db), "--json"])
        self.assertEqual(code, 0, err.getvalue())
        report = json.loads(out.getvalue())
        self.assertEqual(report["prs_seen"], 5)
        self.assertEqual(report["prs_kept"] + report["prs_dropped"], 5)

    def test_zero_or_negative_n_is_a_configuration_error(self):
        for value in ("0", "-3"):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(["--fixture", "--n", value, "--db", str(self.db), "--json"])
            self.assertEqual(code, 2)
            self.assertIn("at least 1", err.getvalue())

    def test_mismatched_fixture_repo_is_a_clean_error_not_a_traceback(self):
        # The bundle only has ENG/platform; the configured repos include
        # ENG/mobile, so the fixture transport reports a missing repo.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--fixture-file", str(self.bundle_path), "--repo", "ENG/nosuch",
                         "--db", str(self.db), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("bitbucket error:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()

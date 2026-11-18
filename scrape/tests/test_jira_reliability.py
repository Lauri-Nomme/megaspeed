"""Jira scrape-reliability regressions (adversarial review round 2: FR-1, FR-2).

Two findings, four tests -- each one fails without its fix:

1. **FR-1** -- a ``--limit`` run advanced the watermark from *every* row the
   search returned, including rows it never fetched, and kept walking after
   ``stop``. The next incremental run then asked for ``updated >= watermark - 72
   h`` and the issues below that cutoff were gone forever. Now the cap is a
   *stop* (mirrors ``scrape.py``'s ``if not truncated``), the manifest records
   ``truncated``, and the cursor is left where it was.
2. **FR-2** -- ``client.issue(key)`` was outside any ``try``: one 404 (a moved
   or deleted issue, a permission error, a 5xx past the retries) killed the
   whole run, and ``jira_issues.json`` was only written after the bundle, so a
   kill lost every per-issue cursor. Now one bad issue is skipped and counted,
   and the identity map + partial bundle + both cursors are checkpointed every
   ``checkpoint_every`` issues and at every project boundary, so a crash
   resumes.

    python3 -m unittest discover -s scrape/tests -v
"""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from ingest import jira_fixtures
from ingest.bundle import read_bundle, read_inflight
from ingest.config import Config
from ingest.progress import ProgressReporter
from ingest.scrape_jira import ISSUE_STATE_FILE, scrape
from ingest.transport import BitbucketError, FixtureJiraTransport

SOURCE = "jira-fixture"
#: ``/rest/api/2/issue/{key}`` -- the per-issue fetch (not ``/worklog``).
ISSUE_PATH = re.compile(r"^/rest/api/2/issue/([A-Z][A-Z0-9_]{0,9}-\d+)$")
NO_CUTOFF = 24 * 365  # wide enough to keep every re-scraped issue


def _config(root: Path) -> Config:
    return Config(
        db_path=root / "unused.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[],
        ticket_projects=("PROJ", "OPS", "ARCH"),
        jira_projects=("PROJ",),
    )


class Harness:
    """Forwards to the fixture transport, records requests, injects failures."""

    def __init__(self, bundle: dict, *, fail_issue: str | None = None,
                 crash_issue: str | None = None):
        self.inner = FixtureJiraTransport(bundle)
        self.fail_issue = fail_issue
        self.crash_issue = crash_issue
        self.paths: list[str] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.paths.append(path)
        match = ISSUE_PATH.match(path)
        key = match.group(1) if match else None
        if key is not None and key == self.fail_issue:
            # Exactly what HttpTransport raises for a 404 (JiraClient wraps it
            # into JiraError, which is what the per-issue block catches).
            raise BitbucketError(f"404 Not Found for {path}: issue does not exist")
        if key is not None and key == self.crash_issue:
            # A transport error is *handled* now (that is FR-2), so a crash has
            # to be process-level: Ctrl-C / SIGINT is not an Exception and is
            # not wrapped by JiraClient._get.
            raise KeyboardInterrupt(f"simulated kill -9 while fetching {key}")
        return self.inner.get(path, params)

    def fetched(self) -> set[str]:
        """The issue keys this run paid a per-issue request for."""
        return {
            match.group(1) for path in self.paths if (match := ISSUE_PATH.match(path))
        }


def _issue_keys(bundle) -> set[str]:
    """The ticket keys a bundle's events belong to (``PROJ-101:created`` -> key)."""
    return {str(event["event_key"]).split(":", 1)[0] for event in bundle.events}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _config(self.root)
        self.out = self.root / "export" / "jira.tgz"
        self.state_dir = self.root / "export" / "jira.state"
        self.fixture = jira_fixtures.generate()

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, transport=None, *, out: Path | None = None, **kwargs) -> dict:
        return scrape(
            self.cfg,
            transport or FixtureJiraTransport(self.fixture),
            SOURCE,
            out=out or self.out,
            state_dir=self.state_dir,
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
            **kwargs,
        )

    def state(self) -> dict:
        return json.loads((self.state_dir / "watermark.json").read_text())["sources"][SOURCE]

    def cursor(self) -> dict:
        return json.loads((self.state_dir / ISSUE_STATE_FILE).read_text())["sources"][SOURCE]


# --------------------------------------------------------------------------- #
# 1. FR-1: a capped run must not consume the issues it never read
# --------------------------------------------------------------------------- #
class CappedRunTests(_Base):
    def test_a_capped_run_does_not_advance_the_watermark(self):
        capped = self.root / "export" / "capped.tgz"
        report = self.ingest(limit=3, out=capped)

        self.assertEqual(report["issues_kept"], 3)
        self.assertTrue(report["truncated"])
        # The old code walked the rest of the page and took the max ``updated``
        # of every row (2027-01-05), which is what destroyed the other 10.
        self.assertIsNone(self.state()["watermark_ts"])
        self.assertIsNone(report["watermark"])
        bundle = read_bundle(capped)
        self.assertTrue(bundle.manifest["truncated"])
        self.assertTrue(bundle.manifest["run"]["truncated"])
        self.assertEqual(len(_issue_keys(bundle)), 3)

    def test_the_next_incremental_run_still_sees_every_issue(self):
        """The review's repro: limit=3 then a normal run lost 8 of 13 issues."""
        capped = self.root / "export" / "capped.tgz"
        self.ingest(limit=3, out=capped)
        second = self.ingest(out=self.root / "export" / "second.tgz")

        expected = {issue["key"] for issue in self.fixture["issues"]}
        self.assertFalse(second["truncated"])
        self.assertEqual(second["issues_seen"], len(expected))
        recovered = _issue_keys(read_bundle(capped)) | _issue_keys(
            read_bundle(self.root / "export" / "second.tgz")
        )
        self.assertEqual(recovered, expected)  # nothing lost
        self.assertEqual(self.state()["watermark_ts"], "2027-01-05T16:00:00Z")

    def test_a_capped_run_still_writes_its_bundle_and_its_cursor(self):
        capped = self.root / "export" / "capped.tgz"
        self.ingest(limit=3, out=capped)
        # the issues it *did* read are recorded, so the next run need not re-read
        # them once they fall below the overlap cutoff
        self.assertEqual(len(self.cursor()), 3)
        self.assertIs(self.state()["complete"], True)
        self.assertTrue(capped.is_file())


# --------------------------------------------------------------------------- #
# 2. FR-2: one bad issue is skipped, never the run
# --------------------------------------------------------------------------- #
class PerIssueFailureTests(_Base):
    def test_a_404_on_one_issue_is_skipped_while_the_others_succeed(self):
        log = io.StringIO()
        report = scrape(
            self.cfg,
            Harness(self.fixture, fail_issue="PROJ-103"),
            SOURCE,
            out=self.out,
            state_dir=self.state_dir,
            overlap_hours=NO_CUTOFF,
            progress=ProgressReporter(stream=log, interval=0),
        )

        # The run survives and the other 12 issues land.
        self.assertEqual(report["issues_kept"], 12)
        self.assertEqual(report["issues_failed"], 1)
        self.assertEqual([failure["key"] for failure in report["failures"]], ["PROJ-103"])
        self.assertIn("404", report["failures"][0]["error"])
        self.assertIn("skipped PROJ-103", log.getvalue())
        bundle = read_bundle(self.out)
        self.assertEqual(bundle.manifest["run"]["issues_failed"], 1)
        self.assertEqual(bundle.manifest["counts"]["issues_failed"], 1)
        self.assertNotIn("PROJ-103", _issue_keys(bundle))
        # Not marked done: the per-issue cursor has no entry, so a later run
        # inside the overlap window pays for it again (and heals the gap).
        self.assertNotIn("PROJ-103", self.cursor())
        self.assertIs(self.state()["complete"], True)

        retry = Harness(self.fixture)
        again = self.ingest(retry, out=self.root / "export" / "retry.tgz",
                            overlap_hours=NO_CUTOFF)
        self.assertEqual(again["issues_failed"], 0)
        self.assertIn("PROJ-103", retry.fetched())
        self.assertIn("PROJ-103", _issue_keys(read_bundle(self.root / "export" / "retry.tgz")))

    def test_a_failing_issue_does_not_stop_the_rest_of_the_page(self):
        report = self.ingest(Harness(self.fixture, fail_issue="PROJ-111"),
                             overlap_hours=NO_CUTOFF)
        # PROJ-111 is the *oldest* issue: it is the first row of the walk, so the
        # old code died before reading anything else.
        self.assertEqual(report["issues_kept"], 12)
        self.assertEqual(len(_issue_keys(read_bundle(self.out))), 12)


# --------------------------------------------------------------------------- #
# 3. FR-2: a crash checkpoints, and the next run resumes instead of restarting
# --------------------------------------------------------------------------- #
class CheckpointResumeTests(_Base):
    #: The 5th issue in ``updated ASC`` order -- two checkpoints have fired by
    #: then (checkpoint_every=2), so four issues are on disk when the kill lands.
    CRASH_ON = "PROJ-104"

    def crash(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self.ingest(Harness(self.fixture, crash_issue=self.CRASH_ON), checkpoint_every=2)

    def test_a_crash_mid_scrape_resumes_without_re_reading_checkpointed_issues(self):
        self.crash()

        # The crash left a *readable* in-flight checkpoint (the incremental
        # spool; the tgz is written once, when the run finishes) and an explicit
        # "unfinished" marker, and every issue it read has its events staged.
        self.assertFalse(self.out.exists())
        checkpoint = read_inflight(self.out)
        self.assertTrue(checkpoint.manifest["run"]["checkpoint"])
        checkpointed = _issue_keys(checkpoint)
        self.assertEqual(checkpointed, {"PROJ-111", "PROJ-112", "PROJ-1", "PROJ-103"})
        state = self.state()
        self.assertIs(state["complete"], False)
        self.assertEqual(state["runs"], 0)  # a checkpoint is not a run
        # A checkpoint never advances the (global) cursor: project B's older
        # issues would fall outside the next run's JQL window.
        self.assertIsNone(state["watermark_ts"])
        interrupted = state["last_run_id"]
        cursor = self.cursor()
        self.assertEqual(set(cursor), checkpointed)
        self.assertEqual({entry["run_id"] for entry in cursor.values()}, {interrupted})

        # The resume: no per-issue request for the four already on disk.
        resume = Harness(self.fixture)
        report = self.ingest(resume, checkpoint_every=2)

        self.assertTrue(report["resumed"])
        self.assertEqual(report["resumed_from"], str(self.out))
        self.assertEqual(report["events_carried"], len(checkpoint.events))
        # The resumed run does exactly the remaining work: the checkpointed
        # issues cost no per-issue request (the oldest are not even in the
        # checkpoint cursor's JQL window; the recent ones carry the run marker).
        self.assertFalse(checkpointed & resume.fetched())
        self.assertGreaterEqual(report["issues_skipped_resumed"], 1)
        self.assertLessEqual(report["issues_skipped_resumed"], len(checkpointed))
        self.assertEqual(
            report["issues_kept"], len(self.fixture["issues"]) - len(checkpointed)
        )
        self.assertEqual(
            report["issues_kept"] + report["issues_skipped"], report["issues_seen"]
        )

        # ... and the final bundle is the union: nothing the crash scraped is lost.
        final = read_bundle(self.out)
        self.assertFalse(final.manifest["run"]["checkpoint"])
        self.assertTrue(final.manifest["run"]["resumed"])
        self.assertEqual(
            _issue_keys(final), {issue["key"] for issue in self.fixture["issues"]}
        )
        # The relation merge is authoritative: no dangling object id survives.
        object_ids = {obj["object_id"] for obj in final.objects}
        self.assertTrue(final.events)
        for event in final.events:
            self.assertTrue(event["objects"], event["event_key"])
            for object_id in event["objects"]:
                self.assertIn(object_id, object_ids, event["event_key"])
        # A resumed run reports the counts of the *bundle*, not just its own half.
        clean = scrape(
            self.cfg,
            FixtureJiraTransport(self.fixture),
            SOURCE,
            out=self.root / "export" / "clean.tgz",
            state_dir=self.root / "export" / "clean.state",
            full=True,
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
        )
        self.assertEqual(final.manifest["counts"]["stories"], clean["counts"]["stories"])
        self.assertEqual(final.manifest["counts"]["events"], clean["counts"]["events"])
        state = self.state()
        self.assertIs(state["complete"], True)
        self.assertEqual(state["runs"], 1)  # one run, not N checkpoints
        # The in-flight spool is gone once the tgz is durable.
        self.assertFalse(Path(report["checkpoint_spool"]).exists())

    def test_checkpointing_can_be_disabled(self):
        report = self.ingest(checkpoint_every=0)
        self.assertEqual(report["issues_kept"], len(self.fixture["issues"]))
        self.assertIs(self.state()["complete"], True)


# --------------------------------------------------------------------------- #
# 4. FR-2: the CLI's --checkpoint-every reaches the Jira path (it did not)
# --------------------------------------------------------------------------- #
class JiraCheckpointCliTests(unittest.TestCase):
    def run_cli(self, root: Path, *extra: str) -> dict:
        import contextlib

        from ingest.scrape_cli import main

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([
                "--jira", "--fixture",
                "--out", str(root / "export" / "jira.tgz"),
                "--state-dir", str(root / "export" / "jira.state"),
                "--json", "--quiet", *extra,
            ])
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue())

    def test_the_flag_is_plumbed_to_the_jira_scrape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = self.run_cli(root, "--checkpoint-every", "0")
            self.assertEqual(report["checkpoint_every"], 0)
            state = json.loads(
                (root / "export" / "jira.state" / "watermark.json").read_text()
            )["sources"][SOURCE]
            self.assertIs(state["complete"], True)
            again = self.run_cli(root, "--checkpoint-every", "2")
            self.assertEqual(again["checkpoint_every"], 2)


if __name__ == "__main__":
    unittest.main()

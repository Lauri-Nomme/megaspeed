"""prd/44 NFR-2 — the sprint-membership cursor.

The Jira scrape used to read every sprint's membership on every run: one
``/rest/agile/1.0/sprint/{id}/issue`` request per sprint, forever, even though a
closed sprint's membership cannot change once its ``completeDate`` is final.
This pins the fix:

* the first (bootstrap) run reads every sprint;
* an incremental run reads only active/future sprints — **zero** closed-sprint
  membership requests;
* a closed sprint whose ``completeDate`` moves is re-read;
* the cache lives in the state dir, next to the issue cursor.

    python3 -m unittest discover -s scrape/tests -v
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from ingest import jira_fixtures
from ingest.config import Config
from ingest.scrape_jira import SPRINT_STATE_FILE, scrape
from ingest.transport import FixtureJiraTransport

SPRINT_ISSUE = re.compile(r"^/rest/agile/1\.0/sprint/(?P<id>\d+)/issue$")
SOURCE = "jira-fixture"


class RecordingJiraTransport:
    """The real fixture transport, plus the sprint-issue request log."""

    def __init__(self, bundle: dict):
        self.inner = FixtureJiraTransport(bundle)
        self.paths: list[str] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.paths.append(path)
        return self.inner.get(path, params)

    def sprint_ids(self) -> list[int]:
        return [int(match["id"]) for path in self.paths
                if (match := SPRINT_ISSUE.match(path))]


class SprintMembershipCursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=self.root / "store.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[],
            ticket_projects=("PROJ",),
            jira_projects=("PROJ",),
        )
        self.out = self.root / "export" / "jira.tgz"
        self.state_dir = self.root / "export" / "jira.state"
        self.fixture = jira_fixtures.generate()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scrape(self, fixture: dict | None = None) -> RecordingJiraTransport:
        transport = RecordingJiraTransport(fixture or self.fixture)
        scrape(self.cfg, transport, SOURCE, out=self.out,
               state_dir=self.state_dir)
        return transport

    def test_an_incremental_run_does_not_refetch_closed_sprint_memberships(self):
        first = self.run_scrape()
        self.assertEqual(sorted(first.sprint_ids()), [1, 2, 3])
        self.assertTrue((self.state_dir / SPRINT_STATE_FILE).exists())

        second = self.run_scrape()
        # only the active sprint's membership is re-read (S3); S1/S2 are closed
        # and their completeDate did not move, so they come from the cache.
        self.assertEqual(second.sprint_ids(), [3])

        # a third unchanged run is still one request, not three
        third = self.run_scrape()
        self.assertEqual(third.sprint_ids(), [3])

    def test_a_closed_sprint_whose_completion_moved_is_re_read(self):
        self.run_scrape()
        moved = jira_fixtures.generate()
        sprint = next(row for row in moved["sprints"] if int(row["id"]) == 2)
        sprint["completeDate"] = "2026-10-10T09:00:00.000Z"
        transport = self.run_scrape(moved)
        # S2 moved -> re-read; S3 is active -> always read; S1 is untouched.
        self.assertEqual(sorted(transport.sprint_ids()), [2, 3])

    def test_a_full_run_re_reads_every_sprint(self):
        self.run_scrape()
        transport = RecordingJiraTransport(self.fixture)
        scrape(self.cfg, transport, SOURCE, out=self.out,
               state_dir=self.state_dir, full=True)
        self.assertEqual(sorted(transport.sprint_ids()), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()

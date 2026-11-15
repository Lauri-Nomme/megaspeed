"""Incremental scrape -> import on a project with settled PRs (prd/14 §2,§3).

The scrape side now skips closed PRs it has already read (``prs.json``), so a
second run re-checks only the OPEN PRs. The import side must not notice: it is
idempotent by ``event_key``, so a re-import is 0 new / 0 rewritten, and a late
approval on an open PR still lands as exactly one new event.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ingest.config import Config
from ingest.pipeline import run
from ingest.store import connect
from ingest.transport import FixtureTransport

T0 = 1_790_000_000_000
HOUR = 3_600_000
HASH = "a" * 40
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}
GRACE = {"name": "grace", "displayName": "Grace Hopper", "type": "NORMAL"}


def _activity(activity_id: int, action: str, minute: int, user: dict, **extra) -> dict:
    return {
        "id": activity_id,
        "createdDate": T0 + minute * 60_000,
        "action": action,
        "user": user,
        **extra,
    }


def _pr(pr_id: int, state: str, activities: list[dict]) -> dict:
    closed = state != "OPEN"
    return {
        "pull_request": {
            "id": pr_id,
            "version": len(activities),
            "title": f"QA-{pr_id}: add the parser",
            "description": "zzcanary description",
            "state": state,
            "open": not closed,
            "closed": closed,
            "createdDate": T0,
            "updatedDate": max(a["createdDate"] for a in activities),
            "closedDate": max(a["createdDate"] for a in activities) if closed else None,
            "fromRef": {"latestCommit": HASH, "displayId": f"feature/QA-{pr_id}-thing"},
            "toRef": {"latestCommit": "c" * 40, "displayId": "main"},
            "author": {"user": ADA},
            "reviewers": [],
            "properties": {},
        },
        "activities": activities,
        "commits": [{
            "id": f"{pr_id:040d}",
            "displayId": f"{pr_id:040d}"[:7],
            "author": ADA,
            "authorTimestamp": T0,
            "committer": ADA,
            "committerTimestamp": T0,
            "message": f"QA-{pr_id} initial retry parser",
        }],
    }


def _bundle(*, approval_on_open_pr: bool = False) -> dict:
    records = [
        _pr(1, "MERGED", [
            _activity(101, "OPENED", 0, ADA),
            _activity(102, "MERGED", 30, GRACE),
        ]),
        _pr(2, "DECLINED", [
            _activity(201, "OPENED", 5, ADA),
            _activity(202, "DECLINED", 40, GRACE),
        ]),
    ]
    open_activities = [
        _activity(301, "OPENED", 10, ADA),
        _activity(302, "APPROVED", 20, GRACE),
    ]
    if approval_on_open_pr:
        # A late approval: the PR's updatedDate moves, so it is re-fetched.
        open_activities.append(_activity(303, "APPROVED", 90, ADA))
    records.append(_pr(3, "OPEN", open_activities))
    return {
        "schema": "megaspeed.fixture/1",
        "projects": [{"key": "QA", "repos": [{"slug": "qa", "pull_requests": records}]}],
    }


class IncrementalScrapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("QA", "qa")],
            ticket_projects=("QA",),
        )
        self.bundle_path = root / "export" / "qa.tgz"
        self.state_dir = root / "export" / "qa.state"

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, *, approval_on_open_pr: bool = False) -> dict:
        return run(
            self.cfg,
            FixtureTransport(_bundle(approval_on_open_pr=approval_on_open_pr)),
            "fixture",
            out=self.bundle_path,
            state_dir=self.state_dir,
        )

    def event_count(self) -> int:
        con = connect(self.cfg.db_path, read_only=True)
        try:
            return con.execute("SELECT count(*) FROM events").fetchone()[0]
        finally:
            con.close()

    def test_the_first_run_scrapes_every_matching_pr(self):
        first = self.ingest()
        self.assertTrue(first["verified"], first["problems"])
        self.assertEqual(first["prs_kept"], 3)
        self.assertEqual(first["prs_dropped"], 0)
        self.assertEqual(first["requests_by_kind"]["commits"], 3)
        self.assertEqual(first["prs_settled"], 2)  # the merged + the declined one
        self.assertEqual(self.event_count(), first["events_emitted"])

    def test_second_run_rechecks_only_the_watermark_boundary_and_adds_nothing(self):
        first = self.ingest()
        before = self.event_count()

        second = self.ingest()
        self.assertTrue(second["verified"], second["problems"])
        # PR 2 (declined) *is* the watermark -- updatedDate ties are ambiguous,
        # so it is read again; that is idempotent. PR 1 sits strictly below it
        # and is settled. PR 3 (open, below) is skipped as unchanged.
        self.assertEqual(second["prs_skipped_settled"], 1)
        self.assertEqual(second["prs_skipped_watermark"], 1)
        self.assertEqual(second["events_new"], 0)
        self.assertEqual(second["events_rewritten"], 0)
        self.assertEqual(self.event_count(), before)
        self.assertEqual(first["events_emitted"], before)

    def test_a_late_approval_on_the_open_pr_lands_while_settled_prs_stay_done(self):
        self.ingest()
        third = self.ingest(approval_on_open_pr=True)
        self.assertTrue(third["verified"], third["problems"])
        self.assertEqual(third["prs_skipped_settled"], 1)   # PR 1, strictly below
        self.assertEqual(third["prs_kept"], 2)              # the tie + the moved PR
        self.assertEqual(third["events_new"], 1)            # the new pr_approved
        con = connect(self.cfg.db_path, read_only=True)
        try:
            rows = con.execute(
                "SELECT count(*) FROM events WHERE pr_id = 3 AND activity = 'pr_approved'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(rows, 2)


if __name__ == "__main__":
    unittest.main()

"""Incremental ingest: derived attributes must converge across runs (BUG-02).

The store used to insert each event once (``INSERT OR IGNORE``) while computing
``approvals_lost`` from a run-local look-ahead. A fact that arrived in a later
ingest -- the ``UNAPPROVED`` caused by a push -- could therefore never repair
the earlier ``pr_pushed`` row, and bumping ``classifier_version`` could never
re-derive anything.

These tests replay the QA scenario: ingest a bundle, then re-ingest the same
bundle plus the later activity, and require the earlier event to be rewritten.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ingest import taxonomy
from ingest.config import Config
from ingest.pipeline import run
from ingest.store import connect
from ingest.transport import FixtureTransport

T0 = 1_790_000_000_000
HASH_A = "a" * 40
HASH_B = "b" * 40
GRACE = {"name": "grace", "displayName": "Grace Hopper", "type": "NORMAL"}
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}


def _activity(activity_id: int, action: str, minute: int, user: dict, **extra) -> dict:
    return {
        "id": activity_id,
        "createdDate": T0 + minute * 60_000,
        "action": action,
        "user": user,
        **extra,
    }


def _bundle(activities: list[dict]) -> dict:
    commit = {
        "id": HASH_A,
        "displayId": HASH_A[:7],
        "author": ADA,
        "authorTimestamp": T0,
        "committer": ADA,
        "committerTimestamp": T0,
        "message": "QA-1 initial retry parser",
    }
    pr = {
        "id": 20,
        "state": "OPEN",
        "createdDate": T0,
        "updatedDate": activities[-1]["createdDate"],
        "fromRef": {"latestCommit": HASH_B, "displayId": "feature/QA-1-thing"},
        "toRef": {"latestCommit": "c" * 40, "displayId": "main"},
        "author": {"user": ADA},
        "title": "QA-1 add the parser",
        "description": "zzcanary-description QA-1",
        "reviewers": [{"user": GRACE, "approved": True}],
    }
    return {
        "schema": "megaspeed.fixture/1",
        "projects": [
            {
                "key": "QA",
                "repos": [
                    {
                        "slug": "qa",
                        "pull_requests": [
                            {"pull_request": pr, "activities": activities, "commits": [commit]}
                        ],
                    }
                ],
            }
        ],
    }


def _activities(*, unapproved: bool) -> list[dict]:
    activities = [
        _activity(1, "OPENED", 0, ADA),
        _activity(2, "APPROVED", 10, GRACE),
        _activity(
            3, "RESCOPED", 20, ADA,
            fromHash=HASH_B, toHash="c" * 40,
            previousFromHash=HASH_A, previousToHash="c" * 40,
            added={"commits": [], "total": 0},
            removed={"commits": [], "total": 0},
            megaspeedPatchId={"push_class": "pure_rebase"},
        ),
    ]
    if unapproved:
        activities.append(_activity(4, "UNAPPROVED", 21, GRACE))
    return activities


class IncrementalTests(unittest.TestCase):
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

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, *, unapproved: bool) -> dict:
        return run(self.cfg, FixtureTransport(_bundle(_activities(unapproved=unapproved))), source="fixture")

    def push_attributes(self) -> dict:
        con = connect(self.cfg.db_path, read_only=True)
        try:
            rows = con.execute(
                "SELECT attributes FROM events WHERE activity = 'pr_pushed'"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 1)
        return json.loads(rows[0][0])

    def test_later_unapproved_rewrites_the_earlier_push(self):
        first = self.ingest(unapproved=False)
        self.assertTrue(first["verified"], first["problems"])
        self.assertIs(self.push_attributes()["approvals_lost"], False)

        second = self.ingest(unapproved=True)
        self.assertTrue(second["verified"], second["problems"])
        self.assertEqual(second["events_new"], 1)      # the UNAPPROVED itself
        self.assertEqual(second["events_rewritten"], 1)  # the earlier pr_pushed
        self.assertIs(self.push_attributes()["approvals_lost"], True)

    def test_rerun_after_convergence_is_a_noop(self):
        self.ingest(unapproved=False)
        self.ingest(unapproved=True)
        third = self.ingest(unapproved=True)
        self.assertEqual(third["events_new"], 0)
        self.assertEqual(third["events_rewritten"], 0)
        self.assertEqual(third["events_derived"], 0)

    def test_provenance_counters_tell_first_load_from_noop(self):
        first = self.ingest(unapproved=False)
        con = connect(self.cfg.db_path, read_only=True)
        try:
            row = con.execute(
                "SELECT events_written, events_skipped FROM ingest_runs ORDER BY as_of DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row[0], first["events_emitted"])
        self.assertEqual(row[1], 0)

        second = self.ingest(unapproved=False)
        con = connect(self.cfg.db_path, read_only=True)
        try:
            row = con.execute(
                "SELECT events_written, events_skipped FROM ingest_runs ORDER BY as_of DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], second["events_emitted"])

    def test_classifier_version_bump_is_replayable(self):
        self.ingest(unapproved=True)
        con = connect(self.cfg.db_path)
        try:
            total = con.execute("SELECT count(*) FROM events").fetchone()[0]
            con.execute(
                "UPDATE events SET classifier_version = 'deterministic-v0', attributes = '{}'"
            )
            stale = con.execute(
                "SELECT count(*) FROM events WHERE classifier_version = 'deterministic-v0'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(stale, total)

        replay = self.ingest(unapproved=True)
        self.assertEqual(replay["events_new"], 0)
        self.assertEqual(replay["events_rewritten"], total)
        con = connect(self.cfg.db_path, read_only=True)
        try:
            versions = {
                row[0] for row in con.execute("SELECT DISTINCT classifier_version FROM events").fetchall()
            }
        finally:
            con.close()
        self.assertEqual(versions, {taxonomy.CLASSIFIER_VERSION})
        self.assertIs(self.push_attributes()["approvals_lost"], True)


if __name__ == "__main__":
    unittest.main()

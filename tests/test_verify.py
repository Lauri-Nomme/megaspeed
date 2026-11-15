"""--verify must catch content-class leaks, not just container shape (BUG-05).

The QA report ran a table of tampering attacks against a valid store; several
passed with exit 0:

    UPDATE events SET actor='LauriNomme'                   -- raw name, no space
    UPDATE events SET content_hash='SomeRawCommitMessage…'  -- prose, no space
    UPDATE events SET classifier_version='v1-launched-by-jdoe'
    UPDATE events SET repo='ENG/lauri-private-repo'
    UPDATE objects SET state='MERGEDish', message_class='fixTheParser'
    UPDATE objects SET ticket_keys=['feature/interleaved']
    UPDATE ingest_runs SET source='bitbucket-prod-account-jdoe'
    DELETE FROM events WHERE event_key = min(...)           -- orphan rows

Every one of them must now fail the guard, and an untouched store must pass.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ingest import fixtures
from ingest.config import Config
from ingest.pipeline import run
from ingest.store import connect, verify
from ingest.transport import FixtureTransport


class VerifyTamperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.cfg = Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform"), ("ENG", "mobile")],
            ticket_projects=("PROJ", "OPS", "ARCH"),
        )
        report = run(cls.cfg, FixtureTransport(fixtures.generate(n=8, seed=7)), source="fixture")
        assert report["verified"], report["problems"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.con = connect(self.cfg.db_path)
        self.con.execute("BEGIN")

    def tearDown(self):
        self.con.execute("ROLLBACK")
        self.con.close()

    def assert_violation(self, sql: str, params: list | tuple = ()) -> list[str]:
        self.con.execute(sql, params)
        report = verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)
        self.assertFalse(report["ok"], f"verify blessed: {sql}")
        return report["problems"]

    def test_valid_store_passes(self):
        report = verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)
        self.assertTrue(report["ok"], report["problems"])

    def test_raw_camel_case_actor_is_rejected(self):
        problems = self.assert_violation("UPDATE events SET actor = 'LauriNomme'")
        self.assertTrue(any("events.actor" in p for p in problems), problems)

    def test_prose_content_hash_is_rejected(self):
        problems = self.assert_violation(
            "UPDATE events SET content_hash = 'SomeRawCommitMessageWithoutSpaces'"
        )
        self.assertTrue(any("content_hash" in p for p in problems), problems)

    def test_unknown_classifier_version_is_rejected(self):
        problems = self.assert_violation(
            "UPDATE events SET classifier_version = 'v1-launched-by-jdoe'"
        )
        self.assertTrue(any("classifier_version" in p for p in problems), problems)

    def test_repo_that_matches_no_pr_object_is_rejected(self):
        problems = self.assert_violation("UPDATE events SET repo = 'ENG/lauri-private-repo'")
        self.assertTrue(any("PR object" in p for p in problems), problems)

    def test_enum_columns_must_come_from_the_closed_set(self):
        problems = self.assert_violation(
            "UPDATE objects SET state = 'MERGEDish' WHERE object_type = 'PR'"
        )
        self.assertTrue(any("outside" in p and "state" in p for p in problems), problems)
        self.con.execute("ROLLBACK")
        self.con.execute("BEGIN")
        problems = self.assert_violation(
            "UPDATE objects SET message_class = 'fixTheParser' WHERE object_type = 'COMMIT'"
        )
        self.assertTrue(any("message_class" in p for p in problems), problems)

    def test_branch_name_never_counts_as_a_ticket_key(self):
        problems = self.assert_violation(
            "UPDATE objects SET ticket_keys = ['feature/interleaved'] WHERE object_type = 'TICKET'"
        )
        self.assertTrue(any("key_shape" in p or "projects" in p for p in problems), problems)

    def test_unconfigured_project_prefix_is_rejected(self):
        problems = self.assert_violation(
            "UPDATE objects SET ticket_keys = ['FIX-1'] WHERE object_type = 'TICKET'"
        )
        self.assertTrue(any("projects" in p for p in problems), problems)

    def test_unknown_source_is_rejected(self):
        problems = self.assert_violation(
            "UPDATE ingest_runs SET source = 'bitbucket-prod-account-jdoe'"
        )
        self.assertTrue(any("ingest_runs.source" in p for p in problems), problems)

    def test_orphan_event_objects_are_rejected(self):
        key = self.con.execute("SELECT min(event_key) FROM events").fetchone()[0]
        problems = self.assert_violation("DELETE FROM events WHERE event_key = ?", [key])
        self.assertTrue(any("missing event" in p for p in problems), problems)

    def test_event_without_objects_is_rejected(self):
        key = self.con.execute("SELECT min(event_key) FROM events").fetchone()[0]
        problems = self.assert_violation("DELETE FROM event_objects WHERE event_key = ?", [key])
        self.assertTrue(any("no object" in p for p in problems), problems)

    def test_inconsistent_actor_kind_is_rejected(self):
        row = self.con.execute(
            "SELECT event_key FROM events WHERE attributes->>'actor_kind' = 'bot' LIMIT 1"
        ).fetchone()
        if row is None:
            self.skipTest("fixture has no bot events")
        self.con.execute(
            "UPDATE events SET attributes = json_merge_patch(attributes, '{\"actor_kind\": \"human\"}')"
            " WHERE event_key = ?",
            [row[0]],
        )
        report = verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)
        self.assertFalse(report["ok"])
        self.assertTrue(any("both" in p for p in report["problems"]), report["problems"])

    def test_undeclared_table_and_column_are_still_rejected(self):
        problems = self.assert_violation("CREATE TABLE raw_comments (body VARCHAR)")
        self.assertTrue(any("undeclared table" in p for p in problems), problems)
        self.con.execute("ROLLBACK")
        self.con.execute("BEGIN")
        problems = self.assert_violation("ALTER TABLE events ADD COLUMN description VARCHAR")
        self.assertTrue(any("outside the allowlist" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()

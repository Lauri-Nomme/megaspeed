"""Object model: PRRevision and ReviewThread (GAP-02 / GAP-05).

prd/03 asks for a ``PRRevision`` per rescope (push_index, head_commit,
push_class) and a ``ReviewThread`` per comment thread (anchor-class, severity,
state, resolved_by). The reducer used to reduce threads to a snapshot on the
comment event and discard ``resolvedDate`` / ``resolver`` / ``threadResolved``
entirely, so review latency (comment -> resolution) was not measurable.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ingest import fixtures
from ingest.config import Config
from ingest.pipeline import run
from ingest.store import connect, list_objects, verify
from ingest.taxonomy import PUSH_CLASS
from ingest.transport import FixtureTransport


class ObjectModelTests(unittest.TestCase):
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
        )
        cls.bundle = fixtures.generate(n=24, seed=20261115)
        cls.report = run(cls.cfg, FixtureTransport(cls.bundle), source="fixture")
        cls.con = connect(cls.cfg.db_path, read_only=True)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def test_store_is_still_contract_clean(self):
        self.assertTrue(self.report["verified"], self.report["problems"])
        self.assertTrue(verify(self.con)["ok"])

    def test_one_pr_revision_per_push(self):
        revisions = list_objects(self.con, "PRREV")
        pushes = self.con.execute(
            "SELECT count(*) FROM events WHERE activity = 'pr_pushed'"
        ).fetchone()[0]
        self.assertEqual(len(revisions), pushes)
        self.assertTrue(revisions)
        for revision in revisions:
            self.assertRegex(
                revision["object_id"],
                r"^PRREV:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+:\d+$",
            )
            self.assertGreaterEqual(revision["attributes"]["push_index"], 1)
            self.assertIn(revision["attributes"]["push_class"], PUSH_CLASS)
            self.assertRegex(revision["head_commit"], r"^[0-9a-f]{40}$")
            self.assertEqual(revision["state"], None)

    def test_pushed_events_reference_their_revision(self):
        linked = self.con.execute(
            "SELECT count(*) FROM event_objects eo JOIN events e ON eo.event_key = e.event_key"
            " WHERE e.activity = 'pr_pushed' AND eo.object_type = 'PRREV'"
        ).fetchone()[0]
        pushes = self.con.execute("SELECT count(*) FROM events WHERE activity = 'pr_pushed'").fetchone()[0]
        self.assertEqual(linked, pushes)
        # and the revision ids are distinct per (PR, push index)
        rows = self.con.execute(
            "SELECT e.event_key, eo.object_id, e.attributes->>'push_index' FROM event_objects eo"
            " JOIN events e ON eo.event_key = e.event_key WHERE eo.object_type = 'PRREV'"
        ).fetchall()
        for event_key, object_id, push_index in rows:
            self.assertTrue(object_id.endswith(f":{push_index}"), (event_key, object_id, push_index))

    def test_review_threads_carry_resolution(self):
        threads = list_objects(self.con, "THREAD")
        self.assertTrue(threads)
        resolved = [t for t in threads if t["state"] == "resolved"]
        self.assertTrue(resolved, "fixture must contain resolved threads")
        for thread in resolved:
            self.assertIsNotNone(thread["created_at"])
            self.assertIsNotNone(thread["closed_at"])
            self.assertIsNotNone(thread["actor"], "resolved thread must name the resolver")
            self.assertIn(thread["attributes"]["severity"], ("mandatory", "suggestion"))
            self.assertIn(thread["attributes"]["anchor_class"], ("file", "general"))
            self.assertGreaterEqual(thread["closed_at"], thread["created_at"])
        for thread in threads:
            if thread["state"] == "open":
                self.assertIsNone(thread["closed_at"])

    def test_comment_events_reference_their_thread(self):
        comments = self.con.execute(
            "SELECT count(*) FROM events WHERE activity = 'comment_added'"
        ).fetchone()[0]
        linked = self.con.execute(
            "SELECT count(*) FROM event_objects eo JOIN events e ON eo.event_key = e.event_key"
            " WHERE e.activity = 'comment_added' AND eo.object_type = 'THREAD'"
        ).fetchone()[0]
        self.assertEqual(linked, comments)

    def test_merged_pr_records_the_merge_commit(self):
        merged = [
            pr for pr in list_objects(self.con, "PR") if pr["attributes"].get("merge_commit")
        ]
        self.assertTrue(merged)
        merge_events = self.con.execute(
            "SELECT count(*) FROM events WHERE activity = 'pr_merged'"
        ).fetchone()[0]
        self.assertEqual(len(merged), merge_events)
        for pr in merged:
            commit = next(
                o for o in list_objects(self.con, "COMMIT") if o["object_id"] == f"COMMIT:{pr['attributes']['merge_commit']}"
            )
            self.assertEqual(commit["message_class"], "merge")
            self.assertEqual(commit["repo"], pr["repo"])

    def test_object_attributes_are_closed_and_not_raw(self):
        payload = json.dumps(list_objects(self.con), default=str).lower()
        self.assertNotIn("zzcanary", payload)
        for obj in list_objects(self.con):
            self.assertNotIn("text", obj["attributes"])
            self.assertNotIn("path", obj["attributes"])



if __name__ == "__main__":
    unittest.main()

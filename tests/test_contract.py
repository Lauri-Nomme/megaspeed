"""The pseudonymisation gate, as tests.

    python3 -m unittest discover -s tests -v

These assert the contract, not the plumbing: no raw text in the store, no
canary anywhere, closed vocabularies only, stable pseudonyms, idempotent ingest,
and the ticket-key filter actually filtering.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ingest import fixtures, taxonomy
from ingest.pipeline import classify_message, KeyFilter, run
from ingest.pseudonym import IdentityTable, base_pseudonym
from ingest.store import connect, list_events, list_objects, verify
from ingest.transport import FixtureTransport


class PseudonymTests(unittest.TestCase):
    def test_base_scheme(self):
        self.assertEqual(base_pseudonym("lauri", "nomme"), "LaNo")
        self.assertEqual(base_pseudonym("", ""), "XxXx")

    def test_stable_and_collision_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = IdentityTable(Path(tmp) / "identity.json")
            first = table.pseudonym({"name": "lauri.nomme", "displayName": "Lauri Nomme"})
            self.assertEqual(first, "LaNo")
            # same person via a different source spelling -> same pseudonym
            self.assertEqual(table.pseudonym({"emailAddress": "lauri.nomme@example.invalid"}), "LaNo")
            second = table.pseudonym({"name": "lasse.nomm", "displayName": "Lasse Nomm"})
            self.assertEqual(second, "LaNo2")
            table.save()
            reloaded = IdentityTable(Path(tmp) / "identity.json")
            self.assertEqual(reloaded.pseudonym({"name": "lasse.nomm"}), "LaNo2")


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = KeyFilter.build(r"\b[A-Z]+-\d+\b", r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$")

    def test_matches_branch_title_commit(self):
        self.assertEqual(self.filter.keys("feature/PROJ-101-retry"), ["PROJ-101"])
        self.assertEqual(self.filter.keys("no key here", "OPS-7 fix"), ["OPS-7"])

    def test_rejects_non_keys_even_if_regex_matches(self):
        # a permissive regex must not be able to smuggle prose into the key column
        loose = KeyFilter.build(r"\b\w+-\w+\b", r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$")
        self.assertEqual(loose.keys("see the foo-BAR thing"), [])

    def test_message_class_is_a_label(self):
        self.assertEqual(classify_message("PROJ-1 fix the parser"), "fix")
        self.assertEqual(classify_message("wip"), "wip")
        self.assertEqual(classify_message("something else entirely"), "other")
        self.assertIn(classify_message("xyz"), taxonomy.MESSAGE_CLASS)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        from ingest.config import Config

        cls.cfg = Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform"), ("ENG", "mobile")],
            page_limit=7,  # force paging
            sentinel_slugs=("toomas.bruno",),
        )
        cls.bundle = fixtures.generate(n=50, seed=20261115)
        cls.report = run(cls.cfg, FixtureTransport(cls.bundle), source="fixture")
        cls.con = connect(cls.cfg.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def test_filter_kept_some_and_dropped_some(self):
        self.assertGreater(self.report["prs_kept"], 0)
        self.assertGreater(self.report["prs_dropped"], 0)
        self.assertEqual(self.report["prs_kept"] + self.report["prs_dropped"], self.report["prs_seen"])

    def test_contract_holds(self):
        report = verify(self.con)
        self.assertTrue(report["ok"], report["problems"])

    def test_no_raw_text_in_any_payload(self):
        payload = json.dumps(
            {"events": list_events(self.con), "objects": list_objects(self.con)},
            default=str,
        ).lower()
        self.assertNotIn("zzcanary", payload)
        # a smoke list of words that only exist in the raw fixture prose
        for canary_prefix in ("zzcanary", "checklist"):
            self.assertNotIn(canary_prefix, payload)

    def test_canaries_exist_in_the_fixture_but_not_the_store(self):
        raw = json.dumps(self.bundle)
        self.assertIn("zzcanary", raw)
        rows = self.con.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertGreater(rows, 100)

    def test_idempotent(self):
        before = self.con.execute("SELECT count(*) FROM events").fetchone()[0]
        second = run(self.cfg, FixtureTransport(self.bundle), source="fixture")
        after = self.con.execute("SELECT count(*) FROM events").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(second["events_new"], 0)

    def test_only_enumerated_activities(self):
        for (activity,) in self.con.execute("SELECT DISTINCT activity FROM events").fetchall():
            self.assertIn(activity, taxonomy.ACTIVITIES)

    def test_objects_carry_matched_ticket_keys(self):
        prs = list_objects(self.con, "PR")
        self.assertTrue(prs)
        for pr in prs:
            self.assertTrue(pr["ticket_keys"], pr["object_id"])
            for key in pr["ticket_keys"]:
                self.assertRegex(key, self.cfg.key_shape)
        commits = list_objects(self.con, "COMMIT")
        self.assertTrue(commits)
        self.assertTrue(all(c["message_class"] in taxonomy.MESSAGE_CLASS for c in commits))


class StoreSchemaTests(unittest.TestCase):
    def test_allowlist_matches_schema_sql(self):
        import re

        from ingest.store import ALLOWED

        raw = (Path(__file__).resolve().parent.parent / "ingest" / "schema.sql").read_text().lower()
        statements = re.sub(r"--[^\n]*", "", raw)  # strip comments, keep DDL only
        for table, columns in ALLOWED.items():
            self.assertIn(f"create table if not exists {table}", statements)
            for column in columns:
                self.assertIn(column, statements)
        for forbidden in ("title", "description", "body", "diff", "message", "text", "branch"):
            self.assertIsNone(
                re.search(rf"\b{forbidden}\b", statements),
                f"schema must not declare a {forbidden!r} column",
            )


if __name__ == "__main__":
    unittest.main()

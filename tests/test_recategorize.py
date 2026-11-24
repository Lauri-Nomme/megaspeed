"""prd/75: the deterministic re-categorisation of an imported store."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ingest.config import Config
from ingest.recategorize import recategorize
from ingest.store import connect, ensure_schema

T0 = "2026-09-01 10:00:00"


class RecategorizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform")],
        )
        con = connect(self.cfg.db_path)
        ensure_schema(con)
        try:
            con.execute(
                "INSERT INTO objects (object_type, object_id, repo, pr_id, ticket_keys)"
                " VALUES ('PR', 'PR:ENG/platform#1', 'ENG/platform', 1, ['PROJ-1'])"
            )
            for oid, kind in (("PERSON:GrHo", "human"), ("PERSON:CiBo", "bot")):
                con.execute(
                    "INSERT INTO objects (object_type, object_id, attributes) VALUES ('PERSON', ?, CAST(? AS JSON))",
                    [oid, json.dumps({"actor_kind": kind}, separators=(",", ":"))],
                )
            threads = {
                "THREAD:ENG/platform#1:101": {
                    "comment_type": "nit", "classifier_version": "mock-v1",
                    "thread_outcome": "unresolved", "severity": "suggestion",
                },
                "THREAD:ENG/platform#1:102": {
                    "comment_type": "suggestion", "classifier_version": "ollama-v1",
                    "severity": "suggestion",
                },
                "THREAD:ENG/platform#1:103": {
                    "comment_type": "nit", "classifier_version": "ollama-v1",
                    "severity": "suggestion",
                },
                "THREAD:ENG/platform#1:104": {
                    "comment_type": "nit", "classifier_version": "mock-v1",
                    "severity": "suggestion",
                },
            }
            for tid, attrs in threads.items():
                con.execute(
                    "INSERT INTO objects (object_type, object_id, attributes) VALUES ('THREAD', ?, CAST(? AS JSON))",
                    [tid, json.dumps(attrs, separators=(",", ":"))],
                )
            opener = {
                "THREAD:ENG/platform#1:101": "CiBo",
                "THREAD:ENG/platform#1:102": "GrHo",
                "THREAD:ENG/platform#1:103": "GrHo",
                "THREAD:ENG/platform#1:104": "CiBo",
            }
            for index, (tid, actor) in enumerate(opener.items(), start=1):
                key = f"ENG/platform#1:act:{index}"
                con.execute(
                    "INSERT INTO events (event_key, ts, activity, actor, repo, pr_id, attributes,"
                    " classifier_version, as_of) VALUES (?, ?, 'comment_added', ?, 'ENG/platform',"
                    " 1, CAST(? AS JSON), 'deterministic-v2', ?)",
                    # The first scrape wrote actor_kind=human even for the bot.
                    [key, T0, actor, json.dumps({"actor_kind": "human"}, separators=(",", ":")), T0],
                )
                con.execute(
                    "INSERT INTO event_objects (event_key, object_type, object_id) VALUES (?, 'THREAD', ?)",
                    [key, tid],
                )
        finally:
            con.close()
        self.trace = root / "trace.jsonl"
        rows = [
            ("THREAD:ENG/platform#1:101",
             "Partial code coverage [report](http://x).\nCoverage info for the first build:\n"
             "\ninstruction coverage is 0%"),
            ("THREAD:ENG/platform#1:102", "Rebased https://x/commits/abc"),
            ("THREAD:ENG/platform#1:103", "nit: rename this"),
        ]
        self.trace.write_text(
            "\n".join(
                json.dumps({"context": {"role": "opener", "thread_id": tid}, "text": text})
                for tid, text in rows
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _attrs(self, thread_id: str) -> dict:
        con = connect(self.cfg.db_path, read_only=True)
        try:
            raw = con.execute(
                "SELECT attributes FROM objects WHERE object_id = ?", [thread_id]
            ).fetchone()[0]
            return json.loads(raw)
        finally:
            con.close()

    def test_dry_run_writes_nothing(self):
        report = recategorize(self.cfg, trace_path=self.trace, apply=False)
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["applied"])
        self.assertEqual(report["kinds"]["ci_notice"], 1)
        self.assertEqual(report["kinds"]["admin"], 1)
        self.assertEqual(report["kinds"]["review"], 1)
        self.assertEqual(report["kinds"]["bot"], 1)
        self.assertEqual(self._attrs("THREAD:ENG/platform#1:101")["comment_type"], "nit")

    def test_apply_sets_kind_and_removes_non_review_labels(self):
        report = recategorize(self.cfg, trace_path=self.trace, apply=True)
        self.assertTrue(report["applied"])
        self.assertTrue(report["verified"], report.get("problems"))

        coverage = self._attrs("THREAD:ENG/platform#1:101")
        self.assertEqual(coverage["comment_kind"], "ci_notice")
        self.assertEqual(coverage["ci_template"], "coverage")
        self.assertEqual(coverage["coverage_min"], 0)
        self.assertTrue(coverage["coverage_zero"])
        # the deterministic outcome stays; the review label does not
        self.assertEqual(coverage["thread_outcome"], "unresolved")
        for key in ("comment_type", "classifier_version", "label_source"):
            self.assertNotIn(key, coverage)

        admin = self._attrs("THREAD:ENG/platform#1:102")
        self.assertEqual(admin["comment_kind"], "admin")
        self.assertNotIn("comment_type", admin)

        review = self._attrs("THREAD:ENG/platform#1:103")
        self.assertEqual(review["comment_kind"], "review")
        self.assertEqual(review["label_source"], "opener")
        self.assertEqual(review["comment_type"], "nit")

        # a bot thread with no text in the trace is still a bot
        self.assertEqual(self._attrs("THREAD:ENG/platform#1:104")["comment_kind"], "bot")

    def test_the_backup_is_taken_before_the_store_opens(self):
        # Windows cannot copy an open DuckDB file (WinError 32), so the copy
        # must happen before ``connect``; assert the ordering, not just the path.
        import ingest.recategorize as rec

        real_connect = rec.connect
        seen = {}

        def spy(path, *args, **kwargs):
            seen["backup"] = list(Path(self.tmp.name).glob(
                "store.duckdb.pre-recategorize-*"
            ))
            return real_connect(path, *args, **kwargs)

        with mock.patch.object(rec, "connect", spy):
            report = rec.recategorize(self.cfg, trace_path=self.trace, apply=True)
        self.assertTrue(report["backup"])
        self.assertTrue(seen["backup"], "backup must exist before connect()")

    def test_stale_event_actor_kind_is_repaired(self):
        recategorize(self.cfg, trace_path=self.trace, apply=True)
        con = connect(self.cfg.db_path, read_only=True)
        try:
            kinds = {
                row[0]: json.loads(row[1])["actor_kind"]
                for row in con.execute(
                    "SELECT actor, attributes FROM events WHERE activity='comment_added'"
                ).fetchall()
            }
        finally:
            con.close()
        self.assertEqual(kinds["CiBo"], "bot")
        self.assertEqual(kinds["GrHo"], "human")


if __name__ == "__main__":
    unittest.main()

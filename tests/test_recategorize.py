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
            # prd/79: ``objects.actor`` on a PR is its author (DES-6).
            con.execute(
                "INSERT INTO objects (object_type, object_id, repo, pr_id, ticket_keys, actor)"
                " VALUES ('PR', 'PR:ENG/platform#1', 'ENG/platform', 1, ['PROJ-1'], 'OtHe')"
            )
            con.execute(
                "INSERT INTO objects (object_type, object_id, repo, pr_id, ticket_keys, actor)"
                " VALUES ('PR', 'PR:ENG/platform#2', 'ENG/platform', 2, ['PROJ-2'], 'GrHo')"
            )
            con.execute(
                "INSERT INTO objects (object_type, object_id, repo, pr_id, ticket_keys, actor)"
                " VALUES ('PR', 'PR:ENG/platform#3', 'ENG/platform', 3, ['PROJ-3'], 'CiBo')"
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
                "THREAD:ENG/platform#1:105": {
                    "comment_type": "suggestion", "classifier_version": "ollama-v1",
                    "severity": "suggestion",
                },
                # prd/79: PR#2's author is GrHo.
                "THREAD:ENG/platform#2:201": {
                    "comment_type": "suggestion", "classifier_version": "ollama-v1",
                    "severity": "suggestion",
                },
                "THREAD:ENG/platform#2:202": {
                    "comment_type": "suggestion", "classifier_version": "ollama-v1",
                    "severity": "suggestion",
                },
                "THREAD:ENG/platform#2:203": {
                    "comment_type": "nit", "classifier_version": "ollama-v1",
                    "severity": "suggestion",
                },
                "THREAD:ENG/platform#2:204": {
                    "comment_type": "nit", "classifier_version": "ollama-v1",
                    "severity": "suggestion",
                },
                "THREAD:ENG/platform#2:205": {
                    "comment_type": "suggestion", "classifier_version": "ollama-v1",
                    "severity": "suggestion",
                },
                # prd/79: a bot-authored PR is still a bot, never an author note.
                "THREAD:ENG/platform#3:301": {
                    "comment_type": "nit", "classifier_version": "ollama-v1",
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
                "THREAD:ENG/platform#1:105": "GrHo",
                "THREAD:ENG/platform#2:201": "GrHo",
                "THREAD:ENG/platform#2:202": "GrHo",
                "THREAD:ENG/platform#2:203": "JoDo",
                "THREAD:ENG/platform#2:204": "GrHo",
                "THREAD:ENG/platform#2:205": "GrHo",
                "THREAD:ENG/platform#3:301": "CiBo",
            }
            for index, (tid, actor) in enumerate(opener.items(), start=1):
                pr_id = int(tid.partition("THREAD:")[2].partition("#")[2].partition(":")[0])
                key = f"ENG/platform#{pr_id}:act:{index}"
                con.execute(
                    "INSERT INTO events (event_key, ts, activity, actor, repo, pr_id, attributes,"
                    " classifier_version, as_of) VALUES (?, ?, 'comment_added', ?, 'ENG/platform',"
                    " ?, CAST(? AS JSON), 'deterministic-v2', ?)",
                    # The first scrape wrote actor_kind=human even for the bot.
                    [key, T0, actor, pr_id, json.dumps({"actor_kind": "human"}, separators=(",", ":")), T0],
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
            ("THREAD:ENG/platform#1:105", "please review"),
            # prd/79: GrHo authored PR#2.
            ("THREAD:ENG/platform#2:201",
             "This keeps the retry budget unchanged; the guard only covers the admin path."),
            ("THREAD:ENG/platform#2:202", "Should the guard also cover the retry path"),
            ("THREAD:ENG/platform#2:203", "the cache key is missing the model name"),
            # 204 has no opener text at all: the safe default must hold.
            ("THREAD:ENG/platform#2:205", "Why is the cache key shared?"),
            ("THREAD:ENG/platform#3:301", "the coverage report is stale"),
        ]
        records = [
            {"context": {"role": "opener", "thread_id": tid}, "text": text}
            for tid, text in rows
        ]
        # prd/78: a reply record must never be read as the opener. This one is a
        # notice shape; if _trace_texts used it, thread 103 would flip to admin.
        records.append({
            "context": {"role": "reply", "thread_id": "THREAD:ENG/platform#1:103"},
            "text": "closing this",
        })
        self.trace.write_text(
            "\n".join(json.dumps(record) for record in records),
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
        # 102 "Rebased", 105 "please review" (prd/78 notices) + 201 the PR#2
        # author's own note (prd/79).
        self.assertEqual(report["kinds"]["admin"], 3)
        self.assertEqual(report["author_notes"], 1)
        # 103, 202 (author question), 203 (reviewer), 204 (no text), 205 (author
        # question with "?") -- and no-text 204 is the safe default.
        self.assertEqual(report["kinds"]["review"], 5)
        # 104 (no text) and 301 (the bot-authored PR's own opener).
        self.assertEqual(report["kinds"]["bot"], 2)
        self.assertEqual(report["no_text"], {"bot": 1, "human": 1})
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

        # prd/78: a review request opener is admin and loses its model label
        request = self._attrs("THREAD:ENG/platform#1:105")
        self.assertEqual(request["comment_kind"], "admin")
        self.assertNotIn("comment_type", request)
        self.assertNotIn("classifier_version", request)

    def test_an_author_opener_is_admin(self):
        # prd/79: GrHo is PR#2's author; her explanation is not a review remark.
        recategorize(self.cfg, trace_path=self.trace, apply=True)
        note = self._attrs("THREAD:ENG/platform#2:201")
        self.assertEqual(note["comment_kind"], "admin")
        for key in ("comment_type", "classifier_version", "label_source"):
            self.assertNotIn(key, note)

    def test_an_author_question_stays_review(self):
        # The guard: a question from the author is still about the code. Both
        # the ``?`` form and the question-word form.
        recategorize(self.cfg, trace_path=self.trace, apply=True)
        for tid in ("THREAD:ENG/platform#2:202", "THREAD:ENG/platform#2:205"):
            review = self._attrs(tid)
            self.assertEqual(review["comment_kind"], "review", tid)
            self.assertEqual(review["label_source"], "opener", tid)

    def test_a_reviewer_opener_stays_review(self):
        recategorize(self.cfg, trace_path=self.trace, apply=True)
        review = self._attrs("THREAD:ENG/platform#2:203")
        self.assertEqual(review["comment_kind"], "review")
        self.assertEqual(review["comment_type"], "nit")
        self.assertEqual(review["label_source"], "opener")

    def test_no_text_is_never_an_author_note(self):
        # prd/79 "no text -> review": the safe default, never a guess.
        recategorize(self.cfg, trace_path=self.trace, apply=True)
        self.assertEqual(
            self._attrs("THREAD:ENG/platform#2:204")["comment_kind"], "review"
        )

    def test_a_bot_opener_is_never_an_author_note(self):
        # PR#3's author is CiBo; the bot's opener is still a bot, not admin.
        recategorize(self.cfg, trace_path=self.trace, apply=True)
        bot = self._attrs("THREAD:ENG/platform#3:301")
        self.assertEqual(bot["comment_kind"], "bot")
        self.assertNotIn("comment_type", bot)

    def test_a_second_apply_changes_nothing(self):
        # The kind is recomputed from the store, not accumulated: no author-note
        # marker is written, so the rule is idempotent.
        first = recategorize(self.cfg, trace_path=self.trace, apply=True)
        second = recategorize(self.cfg, trace_path=self.trace, apply=True)
        self.assertGreater(first["threads_changed"], 0)
        self.assertEqual(second["threads_changed"], 0)
        self.assertEqual(second["author_notes"], first["author_notes"])

    def test_a_reply_record_cannot_change_the_thread_kind(self):
        # Thread 103's opener is a review remark; the trace also holds a reply
        # "closing this". recategorize is opener-only (prd/78), so the reply is
        # ignored and the thread stays a labelled review remark.
        recategorize(self.cfg, trace_path=self.trace, apply=True)
        review = self._attrs("THREAD:ENG/platform#1:103")
        self.assertEqual(review["comment_kind"], "review")
        self.assertEqual(review["comment_type"], "nit")
        self.assertEqual(review["label_source"], "opener")

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

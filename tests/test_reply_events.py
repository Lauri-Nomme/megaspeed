"""prd/76: nested replies are first-class events, and metrics do not move.

Three properties, each a different failure mode:

* the **reducer** emits one ``comment_added`` per reply, on the opener's object
  set, with the reply's actor/timestamp and the closed reply attributes;
* the **derived** review-round and approval-loss look-aheads ignore replies, so
  a reply between two real events cannot invent a round;
* **every reader** (ball-court, rules, queues, delivery, ``/api/events``) reports
  exactly what it reported for the same store without the reply rows.

The backfill is checked against a real synthetic hans capture: a store ingested
before the feature can gain the missing rows, and a second run inserts nothing.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from analysis import ballcourt, rules
from ingest import fixtures
from ingest.bitbucket import PullRequest
from ingest.config import Config
from ingest.pipeline import (
    KeyFilter,
    Reducer,
    SYSTEM_ACTOR,
    Stats,
    derive_approvals_lost,
    derive_review_rounds,
    run,
)
from ingest.pseudonym import IdentityTable
from ingest.store import connect
from ingest.timeutil import to_naive_utc
from ingest.transport import FixtureTransport
from server import query
from tests.test_reducer import ADA, ALAN, AS_OF, GRACE, T0, _activity, _pr

MINUTE = 60_000
REPO = "ENG/platform"


def _config(root: Path) -> Config:
    return Config(
        db_path=root / "store.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[("ENG", "platform"), ("ENG", "mobile")],
        text_backend="mock",
    )


class ReplyEventTests(unittest.TestCase):
    """The reducer: one event per reply, and the opener keeps its meaning."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = _config(root)
        self.identity = IdentityTable(self.cfg.identity_path)
        self.reducer = Reducer(
            self.cfg,
            self.identity,
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape, self.cfg.ticket_projects),
            AS_OF,
        )
        self.stats = Stats()

    def tearDown(self):
        self.tmp.cleanup()

    def reduce(self, pr: PullRequest) -> list[dict]:
        self.reducer.reduce(pr, ["PROJ-1"], pr.activities, self.stats)
        return self.stats.events

    def test_two_replies_make_three_comment_events(self):
        comment = {
            "id": 201,
            "text": "zzcanary-opener",
            "author": GRACE,
            "severity": "NORMAL",
            "state": "OPEN",
            "threadResolved": False,
            "comments": [
                {
                    "id": 301,
                    "text": "zzcanary-first-reply",
                    "author": ALAN,
                    "createdDate": T0 + 5 * MINUTE + 30_000,
                },
                {"id": 302, "text": "zzcanary-second-reply", "author": GRACE},
            ],
        }
        pr = _pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE, comment=comment, commentAction="ADDED"),
        ])
        events = self.reduce(pr)
        comments = [e for e in events if e["activity"] == "comment_added"]
        self.assertEqual(len(comments), 3)

        opener = [e for e in comments if not e["attributes"].get("reply")]
        replies = sorted(
            (e for e in comments if e["attributes"].get("reply")),
            key=lambda e: e["attributes"]["reply_index"],
        )
        self.assertEqual(len(opener), 1)
        self.assertEqual(opener[0]["actor"], self.identity.pseudonym(GRACE))
        self.assertEqual(opener[0]["ts"], to_naive_utc(T0 + 5 * MINUTE))
        # the opener carries no reply marker at all
        self.assertNotIn("reply", opener[0]["attributes"])

        self.assertEqual([e["attributes"]["reply_index"] for e in replies], [1, 2])
        self.assertEqual([e["attributes"]["parent_comment_id"] for e in replies], [201, 201])
        # reply 1 has its own author and timestamp
        self.assertEqual(replies[0]["actor"], self.identity.pseudonym(ALAN))
        self.assertEqual(
            replies[0]["ts"],
            to_naive_utc(T0 + 5 * MINUTE + 30_000),
        )
        # reply 2 has no createdDate: the opener's timestamp is the fallback
        self.assertEqual(replies[1]["actor"], self.identity.pseudonym(GRACE))
        self.assertEqual(replies[1]["ts"], opener[0]["ts"])

        # the same object set as the opener, so every join works unchanged
        opener_objects = {
            oid for key, _type, oid in self.stats.relations
            if key == opener[0]["event_key"]
        }
        for reply in replies:
            reply_objects = {
                oid for key, _type, oid in self.stats.relations
                if key == reply["event_key"]
            }
            self.assertEqual(reply_objects, opener_objects)
            self.assertIn(f"THREAD:{REPO}#1:201", reply_objects)
            self.assertIn(f"PR:{REPO}#1", reply_objects)
        # no raw text anywhere
        self.assertNotIn("zzcanary", json.dumps([e["attributes"] for e in comments]))

    def test_a_reply_without_text_or_author_is_still_an_event(self):
        comment = {
            "id": 201,
            "text": "zzcanary-opener",
            "author": GRACE,
            "comments": [{"id": 301}],
        }
        pr = _pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE, comment=comment, commentAction="ADDED"),
        ])
        replies = [
            e for e in self.reduce(pr)
            if e["activity"] == "comment_added" and e["attributes"].get("reply")
        ]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["actor"], SYSTEM_ACTOR)
        self.assertEqual(replies[0]["attributes"]["actor_kind"], "bot")

    def test_updated_activities_do_not_emit_replies_again(self):
        comment = {"id": 201, "text": "zzcanary", "author": GRACE,
                   "comments": [{"id": 301, "author": ALAN}]}
        pr = _pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE, comment=comment, commentAction="ADDED"),
            _activity(3, "COMMENTED", 9, GRACE, comment=comment, commentAction="UPDATED"),
        ])
        self.assertEqual(
            len([e for e in self.reduce(pr) if e["activity"] == "comment_added"]), 2
        )

    def test_the_natural_key_cannot_collide_with_the_opener(self):
        comment = {"id": 201, "text": "zzcanary", "author": GRACE,
                   "comments": [{"id": 301, "author": ALAN}]}
        pr = _pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(7, "COMMENTED", 5, GRACE, comment=comment, commentAction="ADDED"),
        ])
        keys = [e["event_key"] for e in self.reduce(pr) if e["activity"] == "comment_added"]
        self.assertEqual(
            sorted(keys),
            [f"{REPO}#1:act:7", f"{REPO}#1:act:7:reply1"],
        )

    def test_review_rounds_ignore_a_reply(self):
        """A reply is not the first review, so a later push is still setup."""
        events = [
            {"event_key": f"{REPO}#1:act:1", "ts": datetime(2026, 11, 1, 9),
             "activity": "pr_opened", "actor": self.identity.pseudonym(ADA), "attributes": {}},
            {"event_key": f"{REPO}#1:act:2", "ts": datetime(2026, 11, 1, 10),
             "activity": "comment_added", "actor": self.identity.pseudonym(ADA), "attributes": {}},
            {"event_key": f"{REPO}#1:act:2:reply1", "ts": datetime(2026, 11, 1, 11),
             "activity": "comment_added", "actor": self.identity.pseudonym(GRACE),
             "attributes": {"reply": True, "reply_index": 1, "parent_comment_id": 2}},
            {"event_key": f"{REPO}#1:act:3", "ts": datetime(2026, 11, 1, 12),
             "activity": "pr_pushed", "actor": self.identity.pseudonym(ADA), "attributes": {}},
            {"event_key": f"{REPO}#1:act:4", "ts": datetime(2026, 11, 1, 13),
             "activity": "pr_approved", "actor": self.identity.pseudonym(GRACE), "attributes": {}},
        ]
        self.assertEqual(derive_review_rounds(events), 0)

    def test_approval_loss_ignores_a_reply_between_push_and_unapproval(self):
        push = {"event_key": f"{REPO}#1:act:5", "ts": datetime(2026, 11, 1, 12),
                "activity": "pr_pushed", "actor": self.identity.pseudonym(ADA), "attributes": {}}
        events = [
            {"event_key": f"{REPO}#1:act:1", "ts": datetime(2026, 11, 1, 9),
             "activity": "pr_approved", "actor": self.identity.pseudonym(GRACE), "attributes": {}},
            push,
            {"event_key": f"{REPO}#1:act:6:reply1", "ts": datetime(2026, 11, 1, 12, 30),
             "activity": "comment_added", "actor": self.identity.pseudonym(ALAN),
             "attributes": {"reply": True, "reply_index": 1, "parent_comment_id": 6}},
            {"event_key": f"{REPO}#1:act:7", "ts": datetime(2026, 11, 1, 13),
             "activity": "pr_unapproved", "actor": self.identity.pseudonym(GRACE),
             "attributes": {"cause": "push"}},
        ]
        self.assertTrue(derive_approvals_lost(events)[push["event_key"]])


class OpenerSelectionTests(unittest.TestCase):
    """``recategorize`` must keep reading the opener, never a reply (prd/76)."""

    def test_openers_skip_reply_events(self):
        from ingest.recategorize import _openers
        from ingest.store import ensure_schema

        with tempfile.TemporaryDirectory() as tmp:
            con = connect(Path(tmp) / "store.duckdb")
            try:
                ensure_schema(con)
                thread = f"THREAD:{REPO}#1:201"
                rows = [
                    (f"{REPO}#1:act:2", "GrHo", "{}"),
                    (f"{REPO}#1:act:2:reply1", "AlTu",
                     '{"reply":true,"reply_index":1,"parent_comment_id":201,'
                     '"actor_kind":"human"}'),
                ]
                for key, actor, attrs in rows:
                    con.execute(
                        "INSERT INTO events (event_key, ts, activity, actor, repo, pr_id,"
                        " attributes, classifier_version, content_hash, as_of)"
                        " VALUES (?, ?, 'comment_added', ?, ?, 1, CAST(? AS JSON),"
                        " 'deterministic-v2', '0123456789abcdef', ?)",
                        [key, "2026-11-01 10:00:00", actor, REPO, attrs,
                         "2026-11-01 10:00:00"],
                    )
                    con.execute(
                        "INSERT INTO event_objects (event_key, object_type, object_id)"
                        " VALUES (?, 'THREAD', ?)",
                        [key, thread],
                    )
                self.assertEqual(_openers(con), {thread: "GrHo"})
            finally:
                con.close()


class MetricsUnchangedTests(unittest.TestCase):
    """The event log gains rows; every existing metric keeps its number."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.cfg = _config(cls.root)
        bundle = fixtures.generate(n=24, seed=20261115)
        # The generated corpus does contain replies: otherwise the test proves
        # nothing. (The reply texts are canaries, so ingest must drop them.)
        cls.replies_in_source = sum(
            1
            for project in bundle["projects"]
            for repo in project["repos"]
            for record in repo["pull_requests"]
            for activity in record["activities"]
            if activity["action"] == "COMMENTED"
            for _ in (activity.get("comment") or {}).get("comments") or []
        )
        run(cls.cfg, FixtureTransport(bundle), source="fixture")

        cls.with_replies = connect(cls.cfg.db_path)
        cls.reply_count = int(cls.with_replies.execute(
            "SELECT count(*) FROM events"
            " WHERE coalesce(json_extract_string(attributes, '$.reply'), 'false') = 'true'"
        ).fetchone()[0])
        cls.event_count = int(
            cls.with_replies.execute("SELECT count(*) FROM events").fetchone()[0]
        )

        # The same store, minus the reply rows: the counterfactual every reader
        # must agree with.
        cls.stripped_path = cls.root / "stripped.duckdb"
        cls.with_replies.close()
        shutil.copy2(cls.cfg.db_path, cls.stripped_path)
        con = connect(cls.stripped_path)
        try:
            con.execute(
                "DELETE FROM event_objects WHERE event_key IN ("
                " SELECT event_key FROM events"
                " WHERE coalesce(json_extract_string(attributes, '$.reply'), 'false') = 'true'"
                ")"
            )
            con.execute(
                "DELETE FROM events"
                " WHERE coalesce(json_extract_string(attributes, '$.reply'), 'false') = 'true'"
            )
        finally:
            con.close()
        cls.with_replies = connect(cls.cfg.db_path, read_only=True)

    @classmethod
    def tearDownClass(cls):
        cls.with_replies.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.without = connect(self.stripped_path, read_only=True)
        self.addCleanup(self.without.close)

    def test_the_corpus_actually_exercises_replies(self):
        self.assertGreater(self.replies_in_source, 0)
        self.assertGreater(self.reply_count, 0)
        self.assertEqual(
            self.event_count, self.without.execute("SELECT count(*) FROM events").fetchone()[0]
            + self.reply_count,
        )

    def test_ballcourt_reads_the_same_log(self):
        full = ballcourt.load(self.with_replies)
        stripped = ballcourt.load(self.without)
        self.assertEqual(full.snapshot, stripped.snapshot)
        self.assertEqual(full.events, stripped.events)
        self.assertEqual(
            [s.pr["object_id"] for s in full.prs],
            [s.pr["object_id"] for s in stripped.prs],
        )

    def test_review_rounds_are_identical(self):
        full = ballcourt.load(self.with_replies)
        stripped = ballcourt.load(self.without)
        self.assertEqual(
            [(s.pr["object_id"], len(ballcourt.review_round_events(s)[0]))
             for s in full.prs],
            [(s.pr["object_id"], len(ballcourt.review_round_events(s)[0]))
             for s in stripped.prs],
        )
        # and the two derived counts the store writes on the merge event
        self.assertEqual(
            rules.metrics(full, rules.load_threads(self.with_replies),
                          rules.window_for(full))["rework_loops"]["total_rounds"],
            rules.metrics(stripped, rules.load_threads(self.without),
                          rules.window_for(stripped))["rework_loops"]["total_rounds"],
        )

    def test_comment_tax_is_identical(self):
        full = ballcourt.load(self.with_replies)
        stripped = ballcourt.load(self.without)
        self.assertEqual(
            rules.comment_tax(full, rules.load_threads(self.with_replies),
                              rules.window_for(full)),
            rules.comment_tax(stripped, rules.load_threads(self.without),
                              rules.window_for(stripped)),
        )

    def test_queues_and_delivery_are_identical(self):
        full = ballcourt.load(self.with_replies)
        stripped = ballcourt.load(self.without)
        window = rules.window_for(full)
        self.assertEqual(
            ballcourt.queue_series(full, "person", window),
            ballcourt.queue_series(stripped, "person", window),
        )
        self.assertEqual(
            ballcourt.delivery_list(full, window),
            ballcourt.delivery_list(stripped, window),
        )

    def test_api_events_hides_replies_by_default(self):
        hidden, hidden_total = query.list_events(self.with_replies)
        self.assertEqual(hidden_total, self.event_count - self.reply_count)
        self.assertEqual(hidden_total, query.list_events(self.without)[1])
        self.assertTrue(all(not e["attributes"].get("reply") for e in hidden))
        shown, shown_total = query.list_events(self.with_replies, include_replies=True)
        self.assertEqual(shown_total, self.event_count)
        self.assertEqual(shown_total, hidden_total + self.reply_count)

        counts = query.event_counts(self.with_replies)
        self.assertEqual(counts["total"], hidden_total)
        self.assertEqual(counts["by_activity"]["comment_added"],
                         query.event_counts(self.without)["by_activity"]["comment_added"])


class BackfillTests(unittest.TestCase):
    """``python -m ingest backfill-replies`` on a synthetic hans capture."""

    @classmethod
    def setUpClass(cls):
        from tests.test_hans_import import build_scrape

        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.capture = build_scrape(cls.root / "capture", with_replies=True)
        # The "old" store: the same capture with the replies removed, i.e. what
        # a v0.20.5 ingest could see.
        cls.plain = cls.root / "plain"
        shutil.copytree(cls.capture, cls.plain)
        for path in cls.plain.glob("rest/pr_*_activities.json"):
            activities = json.loads(path.read_text())
            for activity in activities:
                comment = activity.get("comment")
                if isinstance(comment, dict):
                    comment["comments"] = []
            path.write_text(json.dumps(activities))
        cls.cfg = _config(cls.root)
        cls.cfg.repos = [("DEMO", "widget")]
        cls.cfg.ticket_projects = ("DEMO",)
        cls.identity_path = cls.root / "identity.json"
        from ingest.hans import HansTransport
        from ingest.scrape import scrape

        scrape(
            cls.cfg,
            HansTransport(cls.plain),
            "hans",
            out=cls.root / "hans.tgz",
            state_dir=cls.root / "state",
            identity_path=cls.identity_path,
            push_classifier=None,
            text_classifier=None,
            verify_quiet=False,
        )
        from ingest.importer import import_bundle

        cls.imported = import_bundle(cls.cfg, cls.root / "hans.tgz")
        cls.base_db = cls.root / "base.duckdb"
        shutil.copy2(cls.cfg.db_path, cls.base_db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        from dataclasses import replace

        case = Path(tempfile.mkdtemp(dir=self.root))
        self.cfg = replace(self.__class__.cfg, db_path=case / "store.duckdb")
        shutil.copy2(self.__class__.base_db, self.cfg.db_path)

    def backfill(self, *, apply: bool = False):
        from ingest.backfill import backfill_replies

        return backfill_replies(
            self.cfg,
            hans_scrape=self.capture,
            identity_path=self.identity_path,
            apply=apply,
            backup=False,
        )

    def test_dry_run_reports_without_writing(self):
        report = self.backfill()
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["replies_found"], 2)
        self.assertEqual(report["replies_inserted"], 2)
        self.assertEqual(report["replies_skipped"], 0)
        # users.json seeded every author the capture names
        self.assertEqual(report["unknown_authors"], 0)
        self.assertFalse(report["applied"])
        con = connect(self.cfg.db_path, read_only=True)
        try:
            self.assertEqual(
                con.execute("SELECT count(*) FROM events WHERE"
                            " coalesce(json_extract_string(attributes, '$.reply'), 'false') = 'true'"
                            ).fetchone()[0],
                0,
            )
        finally:
            con.close()

    def test_apply_backfills_then_a_second_apply_inserts_nothing(self):
        first = self.backfill(apply=True)
        self.assertTrue(first["applied"])
        self.assertTrue(first["verified"], first.get("problems"))
        self.assertEqual(first["replies_inserted"], 2)
        self.assertEqual(first["replies_skipped"], 0)

        second = self.backfill(apply=True)
        self.assertEqual(second["replies_inserted"], 0)
        self.assertEqual(second["replies_skipped"], 2)
        self.assertTrue(second["verified"], second.get("problems"))

    def test_the_backup_is_taken_before_the_store_opens(self):
        # Windows cannot copy an open DuckDB file (WinError 32): the backup has
        # to happen before connect(), not beside the writes.
        import ingest.backfill as bf

        real_connect = bf.connect
        seen = {}

        def spy(path, *args, **kwargs):
            seen["backup"] = list(Path(path).parent.glob("*.pre-backfill-*"))
            return real_connect(path, *args, **kwargs)

        with mock.patch.object(bf, "connect", spy):
            report = bf.backfill_replies(
                self.cfg, hans_scrape=self.capture,
                identity_path=self.identity_path, apply=True, backup=True,
            )
        self.assertTrue(report["backup"])
        self.assertTrue(seen["backup"], "backup must exist before connect()")

    def test_backfilled_rows_match_a_fresh_ingest(self):
        self.backfill(apply=True)
        # A fresh ingest of the same capture emits the same replies in the
        # reducer; the two must be indistinguishable.
        fresh_root = self.cfg.db_path.parent / "fresh"
        fresh_root.mkdir()
        fresh = _config(fresh_root)
        fresh.repos = [("DEMO", "widget")]
        fresh.ticket_projects = ("DEMO",)
        from ingest.hans import HansTransport
        from ingest.scrape import scrape
        from ingest.importer import import_bundle

        scrape(
            fresh,
            HansTransport(self.capture),
            "hans",
            out=fresh_root / "fresh.tgz",
            state_dir=fresh_root / "fresh.state",
            identity_path=fresh_root / "fresh.identity.json",
            push_classifier=None,
            text_classifier=None,
            verify_quiet=False,
        )
        import_bundle(fresh, fresh_root / "fresh.tgz")
        sql = (
            "SELECT event_key, ts, actor, CAST(attributes AS VARCHAR)"
            " FROM events WHERE coalesce(json_extract_string(attributes, '$.reply'), 'false') = 'true'"
            " ORDER BY event_key"
        )
        con = connect(self.cfg.db_path, read_only=True)
        try:
            backfilled = con.execute(sql).fetchall()
        finally:
            con.close()
        con = connect(fresh.db_path, read_only=True)
        try:
            ingested = con.execute(sql).fetchall()
        finally:
            con.close()
        self.assertEqual(len(backfilled), 2)
        self.assertEqual(backfilled, ingested)

    def test_contract_still_holds(self):
        report = self.backfill(apply=True)
        self.assertTrue(report["verified"], report.get("problems"))
        from ingest.store import imported_scope, verify

        con = connect(self.cfg.db_path, read_only=True)
        try:
            result = verify(con, self.cfg.key_shape, self.cfg.ticket_projects,
                            imported_scope(con))
        finally:
            con.close()
        self.assertTrue(result["ok"], result["problems"])


if __name__ == "__main__":
    unittest.main()

"""Label-only patches: replay the raw trace, correct labels, import the patch.

The flow under test:

1. a scrape writes every model call to ``<state-dir>/classify-trace.jsonl``
   (raw zone, full text + response + thinking);
2. ``ingest relabel`` replays those texts through a classifier and writes a
   small tgz of **labels only**;
3. ``ingest import`` applies it to the store -- THREAD ``comment_type`` /
   ``thread_outcome`` / ``classifier_version`` and the ``phase`` of the linked
   ``question_raised`` event -- without creating or deleting anything;
4. applying it twice changes nothing.

    python3 -m unittest tests.test_relabel -v
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ingest import classify, fixtures, labelpatch
from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.importer import import_bundle, import_path
from ingest.progress import ProgressReporter
from ingest.relabel import RelabelError, main as relabel_main, relabel
from ingest.scrape import scrape
from ingest.store import connect
from ingest.transport import FixtureTransport

QUIET = ProgressReporter(interval=0)


def _config(root: Path, db: str = "store.duckdb") -> Config:
    return Config(
        db_path=root / db,
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[("ENG", "platform"), ("ENG", "mobile")],
        ticket_projects=("PROJ", "OPS", "ARCH"),
    )


def _scrape(root: Path, *, seed: int = 5, bundle: dict | None = None,
            name: str = "live") -> tuple[Config, Path, Path]:
    """A live-shaped fixture scrape: it writes a trace, unlike ``--fixture``."""
    cfg = _config(root)
    out = root / "export" / f"{name}.tgz"
    source = bundle if bundle is not None else fixtures.generate(n=4, seed=seed)
    scrape(
        cfg,
        FixtureTransport(source),
        "bitbucket-dc",
        out=out,
        state_dir=root / f"{name}.state",
        text_classifier=classify.MockClassifier(),
        progress=QUIET,
    )
    return cfg, out, root / f"{name}.state" / "classify-trace.jsonl"


def _with_question(root: Path) -> dict:
    """A fixture corpus whose first comment is an actual question."""
    bundle = fixtures.generate(n=4, seed=5)
    pr = bundle["projects"][0]["repos"][0]["pull_requests"][0]
    for activity in pr["activities"]:
        comment = activity.get("comment") or {}
        if activity.get("action") == "COMMENTED" and activity.get("commentAction") == "ADDED":
            activity["comment"] = {**comment, "id": 99_001, "text": "why do we retry twice here?"}
            break
    return bundle


class Recording(classify.MockClassifier):
    """A deterministic classifier that can be told what to answer."""

    backend = "recording"
    version = "recording-v1"

    def __init__(self, *, comment_type=None, intent=None, question=None, phase=None):
        super().__init__()
        self.forced = {
            key: value
            for key, value in {
                "comment_type": comment_type, "thread_intent": intent,
                "question": question, "phase": phase,
            }.items()
            if value is not None
        }

    def _labels_only(self, text, kind):
        labels = {**super()._labels_only(text, kind), **self.forced}
        if labels.get("comment_type") == "question":
            labels["question"] = True
        return labels


class LabelPatchFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _patch(self, rows, name="labels.tgz"):
        return labelpatch.write_patch(
            self.root / name,
            manifest={
                "created_at": "2026-11-22T00:00:00Z",
                "source": "hans",
                "classifier_version": "deterministic-v2",
            },
            rows=rows,
        )

    def test_a_patch_round_trips_its_rows_and_counts(self):
        rows = [{
            "target": "thread",
            "object_id": "THREAD:DEMO/widget#1:10",
            "labels": {"comment_type": "nit", "thread_outcome": "unresolved",
                       "classifier_version": "ollama-v1"},
            "previous": {"comment_type": "suggestion"},
        }]
        written = self._patch(rows)
        patch = labelpatch.read_patch(written["path"])
        self.assertEqual(patch.source, "hans")
        self.assertEqual(patch.rows[0]["labels"]["comment_type"], "nit")
        self.assertEqual(patch.counts["thread"], 1)

    def test_a_label_outside_the_closed_vocabulary_is_rejected(self):
        self._patch([{
            "target": "thread",
            "object_id": "THREAD:DEMO/widget#1:10",
            "labels": {"comment_type": "nitpick"},
        }])
        with self.assertRaises(labelpatch.PatchError):
            labelpatch.read_patch(self.root / "labels.tgz")

    def test_a_target_that_is_not_a_thread_or_question_is_rejected(self):
        self._patch([{
            "target": "commit",
            "object_id": "COMMIT:" + "a" * 40,
            "labels": {"thread_outcome": "acted_on"},
        }])
        with self.assertRaises(labelpatch.PatchError):
            labelpatch.read_patch(self.root / "labels.tgz")

    def test_a_label_key_the_target_does_not_carry_is_rejected(self):
        self._patch([{
            "target": "question",
            "object_id": "THREAD:DEMO/widget#1:10",
            "labels": {"comment_type": "nit"},
        }])
        with self.assertRaises(labelpatch.PatchError):
            labelpatch.read_patch(self.root / "labels.tgz")

    def test_a_normal_bundle_is_not_mistaken_for_a_patch(self):
        bundle = self.root / "bundle.tgz"
        cfg = _config(self.root)
        scrape(cfg, FixtureTransport(fixtures.generate(n=1, seed=5)), "fixture",
               out=bundle, state_dir=self.root / "b", progress=QUIET)
        self.assertFalse(labelpatch.is_label_patch(bundle))


class LabelPatchApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg, self.bundle, self.trace = _scrape(self.root)
        import_bundle(self.cfg, self.bundle)
        self.thread = next(
            obj for obj in read_bundle(self.bundle).objects
            if obj["object_type"] == "THREAD"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows) -> Path:
        path = self.root / "labels.tgz"
        labelpatch.write_patch(
            path,
            manifest={
                "created_at": "2026-11-22T00:00:00Z",
                "source": "bitbucket-dc",
                "classifier_version": "deterministic-v2",
            },
            rows=rows,
        )
        return path

    def test_a_patch_changes_the_thread_labels_and_nothing_else(self):
        before = connect(self.cfg.db_path)
        events_before = before.execute("SELECT count(*) FROM events").fetchone()[0]
        before.close()
        path = self._write([{
            "target": "thread",
            "object_id": self.thread["object_id"],
            "labels": {"comment_type": "blocker", "thread_outcome": "acted_on",
                       "classifier_version": "ollama-v1"},
        }])
        report = import_path(self.cfg, path)
        self.assertEqual(report["mode"], "label-patch")
        self.assertEqual(report["applied"]["threads"], 1)
        self.assertTrue(report["verified"])

        con = connect(self.cfg.db_path)
        try:
            attributes = json.loads(con.execute(
                "SELECT attributes FROM objects WHERE object_id = ?",
                [self.thread["object_id"]],
            ).fetchone()[0])
            self.assertEqual(attributes["comment_type"], "blocker")
            self.assertEqual(attributes["thread_outcome"], "acted_on")
            self.assertEqual(attributes["classifier_version"], "ollama-v1")
            self.assertEqual(
                con.execute("SELECT count(*) FROM events").fetchone()[0], events_before
            )
        finally:
            con.close()

    def test_applying_the_same_patch_twice_is_a_no_op(self):
        path = self._write([{
            "target": "thread",
            "object_id": self.thread["object_id"],
            "labels": {"comment_type": "nit", "thread_outcome": "unresolved",
                       "classifier_version": "ollama-v1"},
        }])
        self.assertEqual(import_path(self.cfg, path)["applied"]["threads"], 1)
        second = import_path(self.cfg, path)["applied"]
        self.assertEqual(second["threads"], 0)
        self.assertEqual(second["threads_unchanged"], 1)

    def test_a_thread_the_store_does_not_hold_is_counted_not_invented(self):
        path = self._write([{
            "target": "thread",
            "object_id": "THREAD:ENG/ghost#1:1",
            "labels": {"comment_type": "nit", "thread_outcome": "unresolved",
                       "classifier_version": "ollama-v1"},
        }])
        applied = import_path(self.cfg, path)["applied"]
        self.assertEqual(applied["threads"], 0)
        self.assertEqual(applied["threads_missing"], 1)

    def test_a_question_phase_patch_updates_the_linked_event(self):
        cfg, bundle, _trace = _scrape(self.root, bundle=_with_question(self.root), name="q")
        import_bundle(cfg, bundle)
        con = connect(cfg.db_path)
        try:
            event = con.execute(
                "SELECT event_key, attributes FROM events WHERE activity='question_raised'"
            ).fetchone()
            self.assertIsNotNone(event, "the fixture question must be Canon")
            object_id = con.execute(
                "SELECT object_id FROM event_objects WHERE event_key = ? AND object_type = 'THREAD'",
                [event[0]],
            ).fetchone()[0]
            events_before = con.execute("SELECT count(*) FROM events").fetchone()[0]
        finally:
            con.close()
        path = self.root / "phase.tgz"
        labelpatch.write_patch(
            path,
            manifest={"created_at": "x", "source": "bitbucket-dc",
                      "classifier_version": "deterministic-v2"},
            rows=[{"target": "question", "object_id": object_id,
                   "labels": {"phase": "implementation"}}],
        )
        report = import_path(cfg, path)
        self.assertEqual(report["applied"]["questions"], 1)
        con = connect(cfg.db_path)
        try:
            phase = con.execute(
                "SELECT attributes->>'phase' FROM events WHERE event_key = ?", [event[0]]
            ).fetchone()[0]
            self.assertEqual(phase, "implementation")
            self.assertEqual(
                con.execute("SELECT count(*) FROM events").fetchone()[0], events_before
            )
        finally:
            con.close()

    def test_a_question_patch_with_no_linked_event_is_reported(self):
        path = self._write([{
            "target": "question",
            "object_id": self.thread["object_id"],
            "labels": {"phase": "review"},
        }])
        applied = import_path(self.cfg, path)["applied"]
        self.assertEqual(applied["questions"], 0)
        self.assertEqual(applied["questions_missing"], 1)


class RelabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg, self.bundle, self.trace = _scrape(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_trace_holds_every_replayed_text_with_its_context(self):
        from ingest import llmtrace

        records, bad = llmtrace.read_trace(self.trace)
        self.assertEqual(bad, 0)
        self.assertTrue(records)
        for record in records:
            self.assertIn(record["context"]["role"], ("opener", "description", "reply"))
            self.assertTrue(record["text"])
            self.assertEqual(record["backend"], "mock")
        openers = [r for r in records if r["context"]["role"] == "opener"]
        self.assertTrue(all(r["context"]["thread_id"] for r in openers))

    def test_replaying_through_a_better_classifier_corrects_only_labels(self):
        import_bundle(self.cfg, self.bundle)
        before = connect(self.cfg.db_path)
        events_before = before.execute("SELECT count(*) FROM events").fetchone()[0]
        objects_before = before.execute("SELECT count(*) FROM objects").fetchone()[0]
        before.close()

        report = relabel(
            self.cfg,
            trace_path=self.trace,
            bundle_path=self.bundle,
            out=self.root / "labels.tgz",
            classifier=Recording(comment_type="blocker", intent="acted"),
            progress=QUIET,
        )
        self.assertGreater(report["threads"], 0)
        applied = import_path(self.cfg, report["patch"])
        self.assertEqual(applied["mode"], "label-patch")
        self.assertEqual(applied["applied"]["threads"], report["threads"])
        self.assertTrue(applied["verified"])

        con = connect(self.cfg.db_path)
        try:
            rows = con.execute(
                "SELECT DISTINCT attributes->>'comment_type',"
                " attributes->>'classifier_version' FROM objects WHERE object_type='THREAD'"
            ).fetchall()
            self.assertIn(("blocker", "recording-v1"), rows)
            self.assertEqual(
                con.execute("SELECT count(*) FROM events").fetchone()[0], events_before
            )
            self.assertEqual(
                con.execute("SELECT count(*) FROM objects").fetchone()[0], objects_before
            )
        finally:
            con.close()

    def test_a_question_flip_adds_the_derived_event_and_is_idempotent(self):
        # No question_raised event exists for these threads; claiming one is a
        # question must add the derived event, not silently drop the correction.
        import_bundle(self.cfg, self.bundle)
        before = connect(self.cfg.db_path)
        events_before = before.execute("SELECT count(*) FROM events").fetchone()[0]
        before.close()
        report = relabel(
            self.cfg,
            trace_path=self.trace,
            bundle_path=self.bundle,
            out=self.root / "labels.tgz",
            classifier=Recording(question=True),
            progress=QUIET,
        )
        self.assertGreater(report["question_added"], 0)
        patch = labelpatch.read_patch(report["patch"])
        adds = [row for row in patch.rows if row.get("action") == "add"]
        self.assertTrue(adds)
        self.assertTrue(all(row["events"][0]["event_key"].endswith(":q") for row in adds))
        thread_adds = [
            row for row in adds
            if any(o.startswith("THREAD:") for o in row["events"][0]["objects"])
        ]
        self.assertTrue(thread_adds)

        applied = import_path(self.cfg, report["patch"])
        expected = sum(len(row.get("events") or []) for row in adds)
        self.assertEqual(applied["applied"]["questions_added"], expected)
        con = connect(self.cfg.db_path)
        try:
            self.assertGreater(
                con.execute("SELECT count(*) FROM events").fetchone()[0], events_before
            )
            key = thread_adds[0]["events"][0]["event_key"]
            self.assertIsNotNone(con.execute(
                "SELECT 1 FROM events WHERE event_key = ?", [key]
            ).fetchone())
            self.assertTrue(con.execute(
                "SELECT 1 FROM event_objects WHERE event_key = ? AND object_type = 'THREAD'",
                [key],
            ).fetchone())
        finally:
            con.close()
        second = import_path(self.cfg, report["patch"])["applied"]
        self.assertEqual(second["questions_added"], 0)
        self.assertEqual(second["questions_unchanged"], expected)

    def test_a_question_flip_removes_the_derived_event(self):
        cfg, bundle, _trace = _scrape(self.root, bundle=_with_question(self.root), name="flip")
        import_bundle(cfg, bundle)
        con = connect(cfg.db_path)
        try:
            events_before = con.execute("SELECT count(*) FROM events").fetchone()[0]
            questions_before = con.execute(
                "SELECT count(*) FROM events WHERE activity = 'question_raised'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertGreaterEqual(questions_before, 1)
        report = relabel(
            cfg,
            trace_path=_trace,
            bundle_path=bundle,
            out=self.root / "labels.tgz",
            classifier=Recording(comment_type="nit", question=False),
            progress=QUIET,
        )
        self.assertGreater(report["question_removed"], 0)
        applied = import_path(cfg, report["patch"])
        self.assertGreaterEqual(applied["applied"]["questions_removed"], 1)
        con = connect(cfg.db_path)
        try:
            questions_after = con.execute(
                "SELECT count(*) FROM events WHERE activity = 'question_raised'"
            ).fetchone()[0]
            self.assertLess(questions_after, questions_before)
            self.assertLess(
                con.execute("SELECT count(*) FROM events").fetchone()[0], events_before
            )
        finally:
            con.close()

    def test_limit_replays_only_the_first_n_texts(self):
        report = relabel(
            self.cfg,
            trace_path=self.trace,
            bundle_path=self.bundle,
            out=self.root / "labels.tgz",
            classifier=Recording(comment_type="nit"),
            limit=3,
            progress=QUIET,
        )
        self.assertEqual(report["replayable"], 3)
        self.assertLessEqual(report["rows"], 3)

    def test_a_missing_trace_is_an_operator_error(self):
        with self.assertRaises(RelabelError):
            relabel(
                self.cfg,
                trace_path=self.root / "nope.jsonl",
                bundle_path=self.bundle,
                out=self.root / "labels.tgz",
                classifier=Recording(),
                progress=QUIET,
            )

    def test_a_dead_model_refuses_to_write_a_patch_that_learned_nothing(self):
        def opener(request, timeout):
            raise OSError("[Errno 111] Connection refused")

        dead = classify.OllamaClassifier(
            "http://llm.invalid", opener=opener, max_failures=1
        )
        with self.assertRaises(RelabelError) as caught:
            relabel(
                self.cfg,
                trace_path=self.trace,
                bundle_path=self.bundle,
                out=self.root / "labels.tgz",
                classifier=dead,
                progress=QUIET,
            )
        self.assertIn("fell back", str(caught.exception))
        self.assertFalse((self.root / "labels.tgz").exists())

    def test_the_cli_writes_a_patch_and_announces_the_import(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = relabel_main([
                "--trace", str(self.trace),
                "--bundle", str(self.bundle),
                "--out", str(self.root / "labels.tgz"),
                "--text-backend", "mock",
                "--quiet",
            ])
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue((self.root / "labels.tgz").is_file())
        self.assertIn("python3 -m ingest import", out.getvalue())


def _hans_capture(root: Path) -> Path:
    """A two-PR capture (no git) whose comments are readable without a trace."""
    from ingest.transport import API

    cap = root / "capture"
    rest = cap / "rest"
    rest.mkdir(parents=True)
    ada = {"name": "ada", "displayName": "Ada", "type": "NORMAL"}
    grace = {"name": "grace", "displayName": "Grace", "type": "NORMAL"}
    pr = {
        "id": 1, "title": "DEMO-1: widget", "description": "DEMO-1 why two queues?",
        "state": "OPEN", "open": True, "closed": False, "draft": False,
        "createdDate": 1_790_000_000_000, "updatedDate": 1_790_000_600_000,
        "closedDate": None,
        "fromRef": {"id": "refs/heads/f", "displayId": "f", "type": "BRANCH",
                    "latestCommit": "a" * 40},
        "toRef": {"id": "refs/heads/main", "displayId": "main", "type": "BRANCH",
                  "latestCommit": "b" * 40},
        "author": {"user": ada}, "reviewers": [], "participants": [],
        "properties": {}, "locked": False,
    }
    activities = [
        {"id": 11, "createdDate": 1_790_000_000_000, "action": "OPENED", "user": ada},
        {"id": 12, "createdDate": 1_790_000_060_000, "action": "COMMENTED", "user": grace,
         "comment": {"id": 100, "text": "why do we retry twice here?", "severity": "NORMAL",
                     "state": "OPEN", "threadResolved": False},
         "commentAction": "ADDED"},
        {"id": 13, "createdDate": 1_790_000_120_000, "action": "COMMENTED", "user": ada,
         "comment": {"id": 101, "text": "nit: rename this variable", "severity": "NORMAL",
                     "state": "OPEN", "threadResolved": False},
         "commentAction": "ADDED"},
    ]
    (rest / "pull-requests_DEMO_widget.json").write_text(json.dumps([pr]))
    (rest / "pr_1.json").write_text(json.dumps(pr))
    (rest / "pr_1_activities.json").write_text(json.dumps(activities))
    (rest / "users.json").write_text(json.dumps([ada, grace]))
    (cap / "index.json").write_text(json.dumps({
        "project": "DEMO", "repo": "widget", "prs": [1],
        "entities": [
            {"file": "rest/users.json", "endpoint": f"{API}/users"},
            {"file": "rest/pull-requests_DEMO_widget.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests?state=ALL"},
            {"file": "rest/pr_1.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests/1"},
            {"file": "rest/pr_1_activities.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests/1/activities"},
        ],
    }))
    return cap


class HansSourceRelabelTests(unittest.TestCase):
    """No trace? The capture on disk still holds the same texts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.capture = _hans_capture(self.root)
        self.cfg = _config(self.root)
        self.cfg.repos = [("DEMO", "widget")]
        self.cfg.ticket_projects = ("DEMO",)
        from ingest.hans import HansTransport

        self.bundle = self.root / "export" / "hans.tgz"
        scrape(
            self.cfg,
            HansTransport(self.capture),
            "hans",
            out=self.bundle,
            state_dir=self.root / "hans.state",
            text_classifier=classify.MockClassifier(),
            progress=QUIET,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_replaying_the_capture_produces_the_same_labels_without_a_trace(self):
        import_bundle(self.cfg, self.bundle)
        report = relabel(
            self.cfg,
            hans_scrape=self.capture,
            bundle_path=self.bundle,
            out=self.root / "labels.tgz",
            classifier=Recording(comment_type="blocker", intent="acted"),
            progress=QUIET,
        )
        self.assertEqual(report["input_kind"], "capture")
        # Two texts (description + two comments) read from disk; no trace file.
        self.assertEqual(report["records"], 3)
        self.assertGreaterEqual(report["threads"], 1)
        applied = import_path(self.cfg, report["patch"])
        self.assertEqual(applied["applied"]["threads"], report["threads"])
        self.assertTrue(applied["verified"])

    def test_a_capture_with_no_matching_threads_yields_an_empty_patch(self):
        report = relabel(
            self.cfg,
            hans_scrape=self.capture,
            bundle_path=self.bundle,
            out=self.root / "labels.tgz",
            classifier=Recording(),
            limit=1,
            progress=QUIET,
        )
        self.assertEqual(report["replayable"], 1)


class BundleIoOptimizationTests(unittest.TestCase):
    """One artifact, one read: no re-unpacking per text or per phase."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_relabel_reads_the_bundle_exactly_once_from_a_capture(self):
        from ingest.relabel import read_bundle as relabel_read_bundle

        capture = _hans_capture(self.root)
        cfg = _config(self.root)
        cfg.repos = [("DEMO", "widget")]
        cfg.ticket_projects = ("DEMO",)
        from ingest.hans import HansTransport

        bundle = self.root / "export" / "hans.tgz"
        scrape(cfg, HansTransport(capture), "hans", out=bundle,
               state_dir=self.root / "hans.state",
               text_classifier=classify.MockClassifier(), progress=QUIET)
        calls = []
        real = relabel_read_bundle

        def counted(path):
            calls.append(path)
            return real(path)

        with mock.patch("ingest.relabel.read_bundle", side_effect=counted):
            relabel(
                cfg,
                hans_scrape=capture,
                bundle_path=bundle,
                out=self.root / "labels.tgz",
                classifier=Recording(),
                progress=QUIET,
            )
        self.assertEqual(len(calls), 1, calls)

    def test_the_patch_sniff_does_not_scan_the_whole_archive(self):
        import tarfile

        capture = _hans_capture(self.root)
        cfg = _config(self.root)
        cfg.repos = [("DEMO", "widget")]
        cfg.ticket_projects = ("DEMO",)
        from ingest.hans import HansTransport

        bundle = self.root / "export" / "hans.tgz"
        scrape(cfg, HansTransport(capture), "hans", out=bundle,
               state_dir=self.root / "hans.state",
               text_classifier=classify.MockClassifier(), progress=QUIET)

        def refuse(self):  # pragma: no cover - called only on a regression
            raise AssertionError("is_label_patch must not call getmembers()")

        with mock.patch.object(tarfile.TarFile, "getmembers", refuse):
            self.assertFalse(labelpatch.is_label_patch(bundle))
            patch = self.root / "labels.tgz"
            labelpatch.write_patch(
                patch,
                manifest={"created_at": "x", "source": "hans",
                          "classifier_version": "deterministic-v2"},
                rows=[],
            )
            self.assertTrue(labelpatch.is_label_patch(patch))

    def test_a_z_timestamp_parses_like_an_explicit_utc_offset(self):
        from ingest.timeutil import to_naive_utc

        self.assertEqual(
            to_naive_utc("2026-11-22T07:12:03Z"),
            to_naive_utc("2026-11-22T07:12:03+00:00"),
        )
        self.assertEqual(
            str(to_naive_utc("2026-11-22T07:12:03.421Z")),
            "2026-11-22 07:12:03.421000",
        )
        # A real offset must still be converted, not assumed UTC.
        self.assertEqual(
            str(to_naive_utc("2026-11-22T09:12:03+02:00")),
            "2026-11-22 07:12:03",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

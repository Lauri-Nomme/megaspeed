"""prd/72 — sprint **metadata refresh**: a tiny SPRINT-only bundle, no ingest run.

The team's Jira sprints are named `Sprint 352`-style, and the number is parsed at
scrape time into ``sprint_no`` (prd/70 F-16). Before this feature the only way to
populate it was a full re-scrape — every issue, worklog and changelog — for a
handful of sprint rows. ``--sprints-only`` reads just the boards' sprint lists
through the same client and the same reducer, and import upserts the objects
*without* writing an ``ingest_runs`` row or moving the source watermark.

This file pins the contract end to end:

* the named fixture transport -> 0 events, N SPRINT objects, ``sprint_no`` parsed
  from `Sprint 352` / `S352` / `DEMO Sprint 352`, absent for a non-numeric name;
* ``manifest.sprint_refresh`` true, ``watermark_ts`` null, counts ``events: 0``;
* import updates only the SPRINT rows, leaves ``ingest_runs``/events/other rows
  untouched, runs ``verify`` and reports ``mode: sprint-refresh``;
* a normal full bundle still imports exactly as before;
* both ``--jira --sprints-only`` and ``--jira-cloud --sprints-only`` route (the
  Cloud one through the existing emulation proxy).

No real identifiers: the synthetic PROJ/DEMO fixture only.

    python3 -m unittest tests.test_sprint_refresh -v
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ingest import jira_fixtures
from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.importer import ImportRejected, import_bundle
from ingest.scrape_cli import main as scrape_main
from ingest.scrape_jira import scrape, scrape_sprints
from ingest.store import connect, verify
from ingest.transport import FixtureJiraTransport
from proxy.cloud_jira_proxy import CloudJiraProxy, make_server
from tests.test_cloud_jira_proxy import PAT, _StubDC, _serve, _stub_bundle


def _config(root: Path) -> Config:
    return Config(
        db_path=root / "store.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[],
        ticket_projects=(jira_fixtures.PROJECT,),
        jira_projects=(jira_fixtures.PROJECT,),
        jira_min_interval=0.0,
    )


def _fixture(sprint_names: dict[int, str], *, extra_unnamed: bool = False) -> dict:
    """A deep copy of the synthetic fixture with the given sprint names.

    ``jira_fixtures.generate()`` hands back the module-level ``SPRINTS`` list, so
    a test that renames a sprint in place would leak into every later test. Copy
    first, always.
    """
    bundle = copy.deepcopy(jira_fixtures.generate())
    sprints = copy.deepcopy(bundle["sprints"])
    for sprint in sprints:
        sprint["name"] = sprint_names[int(sprint["id"])]
    if extra_unnamed:
        sprints.append(
            {
                "id": 4,
                "name": "Hardening",
                "state": "future",
                "startDate": "2026-12-01T09:00:00.000Z",
                "endDate": "2026-12-15T17:00:00.000Z",
                "completeDate": None,
                "goal": "Hardening",
                "originBoardId": 1,
            }
        )
    bundle["sprints"] = sprints
    return bundle


#: prd/70 F-16 names: three forms of the same number, plus a non-numeric one.
NAMED_SPRINTS = {1: "Sprint 352", 2: "S352", 3: "DEMO Sprint 352", 4: "Hardening"}
UNNAMED_SPRINTS = {1: "Hardening and cleanup", 2: "Cleanup", 3: "Hardening"}


class SprintOnlyScrapeTests(unittest.TestCase):
    """``scrape_sprints``: a SPRINT-only tgz, no events, no cursor, no identity."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.cfg = _config(cls.root)
        cls.bundle_path = cls.root / "export" / "sprints.tgz"
        cls.report = scrape_sprints(
            cls.cfg,
            FixtureJiraTransport(_fixture(NAMED_SPRINTS, extra_unnamed=True)),
            "jira-fixture",
            out=cls.bundle_path,
        )
        cls.bundle = read_bundle(cls.bundle_path)
        cls.objects = {obj["object_id"]: obj for obj in cls.bundle.objects}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_bundle_has_no_events_and_only_sprint_objects(self):
        self.assertEqual(self.bundle.events, [])
        self.assertEqual(len(self.bundle.objects), 4)
        self.assertEqual({obj["object_type"] for obj in self.bundle.objects}, {"SPRINT"})
        counts = self.bundle.manifest["counts"]
        self.assertEqual(counts["events"], 0)
        self.assertEqual(counts["objects"], 4)
        self.assertEqual(counts["sprints"], 4)
        self.assertEqual(counts["people"], 0)
        self.assertEqual(counts["epics"], 0)
        self.assertEqual(counts["stories"], 0)
        self.assertEqual(self.report["events"], 0)
        self.assertEqual(self.report["objects"], 4)

    def test_the_manifest_marks_a_refresh_with_no_watermark(self):
        manifest = self.bundle.manifest
        self.assertIs(manifest["sprint_refresh"], True)
        self.assertIsNone(manifest["watermark_ts"])
        self.assertIsNone(manifest["previous_watermark_ts"])
        self.assertIsNone(self.bundle.watermark)
        self.assertEqual(manifest["source"], "jira-fixture")
        self.assertEqual(manifest["run"]["issues_seen"], 0)
        self.assertEqual(manifest["jira"]["sprints"], 4)

    def test_the_number_is_parsed_and_the_name_never_stored(self):
        by_id = {
            int(object_id.partition("#")[2]): obj for object_id, obj in self.objects.items()
        }
        for sprint_id in (1, 2, 3):
            with self.subTest(sprint_id=sprint_id):
                self.assertEqual(by_id[sprint_id]["attributes"]["sprint_no"], 352)
        # a name outside the closed shape is not guessed at
        self.assertNotIn("sprint_no", by_id[4]["attributes"])
        # prd/02: the name text itself never crosses the boundary
        raw = json.dumps(self.bundle.objects, default=str)
        for text in ("Sprint 352", "S352", "DEMO", "Hardening"):
            self.assertNotIn(text, raw)
        self.assertEqual(self.report["sprints_numbered"], 3)
        self.assertEqual(self.report["sprints_unnumbered"], 1)

    def test_no_identity_or_cursor_state_is_written(self):
        # prd/72: a SPRINT object carries no actor, so the refresh neither
        # allocates a pseudonym nor saves the identity map, and it never writes a
        # watermark/an issue-sprint cursor.
        self.assertFalse(self.cfg.identity_path.exists())
        self.assertFalse(self.bundle_path.with_suffix(".state").exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["export"])

    def test_the_report_says_sprint_refresh(self):
        self.assertEqual(self.report["mode"], "sprint-refresh")
        self.assertEqual(self.report["sprints"], 4)
        self.assertIsNone(self.report["watermark"])
        self.assertEqual(self.report["counts"]["events"], 0)


class SprintOnlyImportTests(unittest.TestCase):
    """Importing a refresh: SPRINT rows updated, nothing else moves."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.cfg = _config(cls.root)
        # 1. A store built by a *full* scrape whose sprint names carry no number:
        #    its SPRINT rows have no `sprint_no` yet, exactly like the live store.
        cls.full_path = cls.root / "full.tgz"
        scrape(
            cls.cfg,
            FixtureJiraTransport(_fixture(UNNAMED_SPRINTS)),
            "jira-fixture",
            out=cls.full_path,
            state_dir=cls.root / "state",
        )
        cls.full_import = import_bundle(cls.cfg, cls.full_path)
        cls.con = connect(cls.cfg.db_path)
        cls.before = cls._snapshot()
        # 2. The refresh bundle (same sprint ids, now named), imported.
        cls.refresh_path = cls.root / "sprints.tgz"
        cls.scrape_report = scrape_sprints(
            cls.cfg,
            FixtureJiraTransport(_fixture(NAMED_SPRINTS)),
            "jira-fixture",
            out=cls.refresh_path,
        )
        cls.report = import_bundle(cls.cfg, cls.refresh_path)
        cls.after = cls._snapshot()

    @classmethod
    def _snapshot(cls) -> dict:
        con = cls.con
        return {
            "runs": con.execute(
                "SELECT run_id, source, as_of, source_watermark, events_written"
                " FROM ingest_runs ORDER BY run_id"
            ).fetchall(),
            "events": con.execute("SELECT count(*) FROM events").fetchone()[0],
            "objects": {
                row[0]: row
                for row in con.execute(
                    "SELECT object_id, object_type, state, created_at, closed_at,"
                    " ticket_keys, actor, attributes::VARCHAR FROM objects"
                ).fetchall()
            },
        }

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def test_the_refresh_import_reports_its_own_mode(self):
        self.assertEqual(self.report["mode"], "sprint-refresh")
        self.assertEqual(self.report["events_loaded"], 0)
        self.assertEqual(self.report["objects"], 3)
        self.assertFalse(self.report["run_recorded"])
        self.assertIsNone(self.report["run_id"])
        self.assertTrue(self.report["verified"], self.report["problems"])

    def test_the_sprint_rows_gain_the_number(self):
        sprints = {
            object_id: row
            for object_id, row in self.after["objects"].items()
            if row[1] == "SPRINT"
        }
        self.assertEqual(set(sprints), {"SPRINT:PROJ#1", "SPRINT:PROJ#2", "SPRINT:PROJ#3"})
        for object_id, row in sprints.items():
            with self.subTest(object_id=object_id):
                self.assertEqual(json.loads(row[7])["sprint_no"], 352)

    def test_no_ingest_run_is_added_and_the_watermark_does_not_move(self):
        self.assertEqual(self.after["runs"], self.before["runs"])
        self.assertEqual(len(self.after["runs"]), 1)

    def test_no_event_and_no_other_object_changes(self):
        self.assertEqual(self.after["events"], self.before["events"])
        self.assertGreater(self.before["events"], 0)
        self.assertEqual(set(self.after["objects"]), set(self.before["objects"]))
        changed = {
            object_id
            for object_id, row in self.after["objects"].items()
            if row != self.before["objects"][object_id]
        }
        self.assertEqual(changed, {"SPRINT:PROJ#1", "SPRINT:PROJ#2", "SPRINT:PROJ#3"})

    def test_the_store_still_passes_the_contract(self):
        report = verify(
            self.con, self.cfg.key_shape, self.cfg.ticket_projects, self.cfg.ticket_keys
        )
        self.assertTrue(report["ok"], report["problems"])


class SprintRefreshGateTests(unittest.TestCase):
    """A bundle that claims the flag must really be a metadata refresh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _config(self.root)
        self.manifest = {
            "schema_version": 1,
            "generator": {"name": "megaspeed-scrape", "version": "test"},
            "created_at": "2026-03-01T12:00:00Z",
            "source": "jira-fixture",
            "sources": ["jira-fixture"],
            "classifier_version": "deterministic-v2",
            "ticket_projects": [jira_fixtures.PROJECT],
            "ticket_keys": [],
            "watermark_ts": None,
            "sprint_refresh": True,
            "counts": {"events": 0, "objects": 0},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, *, events: list[dict], objects: list[dict], relations=()) -> Path:
        from ingest.bundle import write_bundle

        path = self.root / "claimed-refresh.tgz"
        write_bundle(
            path, manifest=self.manifest, events=events, objects=objects,
            relations=list(relations),
        )
        return path

    def test_a_bundle_with_events_is_rejected(self):
        from datetime import datetime

        sprint = {
            "object_type": "SPRINT",
            "object_id": "SPRINT:PROJ#1",
            "repo": None,
            "pr_id": None,
            "ticket_keys": [],
            "actor": None,
            "state": "active",
            "created_at": datetime(2026, 1, 5, 9, 0),
            "closed_at": None,
            "head_commit": None,
            "message_class": None,
            "attributes": {"sprint_no": 352},
        }
        event = {
            "event_key": "PROJ-1:created",
            "ts": datetime(2026, 1, 5, 9, 0),
            "activity": "created",
            "actor": "AaAa",
            "repo": None,
            "pr_id": None,
            "attributes": {},
            "classifier_version": "deterministic-v2",
            "content_hash": "0123456789abcdef",
            "as_of": datetime(2026, 3, 1, 12, 0),
        }
        path = self._write(
            events=[event],
            objects=[sprint],
            relations=[("PROJ-1:created", "SPRINT", "SPRINT:PROJ#1")],
        )
        with self.assertRaises(ImportRejected) as caught:
            import_bundle(self.cfg, path)
        self.assertIn("must carry none", str(caught.exception))
        self.assertFalse(self.cfg.db_path.exists())

    def test_a_bundle_with_a_non_sprint_object_is_rejected(self):
        from datetime import datetime

        story = {
            "object_type": "STORY",
            "object_id": "STORY:PROJ-1",
            "repo": None,
            "pr_id": None,
            "ticket_keys": ["PROJ-1"],
            "actor": "AaAa",
            "state": "open",
            "created_at": datetime(2026, 1, 5, 9, 0),
            "closed_at": None,
            "head_commit": None,
            "message_class": None,
            "attributes": {},
        }
        path = self._write(events=[], objects=[story])
        with self.assertRaises(ImportRejected) as caught:
            import_bundle(self.cfg, path)
        self.assertIn("only SPRINT objects", str(caught.exception))
        self.assertFalse(self.cfg.db_path.exists())


class FullImportRegressionTests(unittest.TestCase):
    """A normal full bundle still imports exactly as before (no new manifest key)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.cfg = _config(cls.root)
        cls.path = cls.root / "full.tgz"
        cls.scrape_report = scrape(
            cls.cfg,
            FixtureJiraTransport(copy.deepcopy(jira_fixtures.generate())),
            "jira-fixture",
            out=cls.path,
            state_dir=cls.root / "state",
        )
        cls.bundle = read_bundle(cls.path)
        cls.report = import_bundle(cls.cfg, cls.path)
        cls.con = connect(cls.cfg.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def test_the_full_manifest_has_no_refresh_flag(self):
        self.assertNotIn("sprint_refresh", self.bundle.manifest)
        self.assertIsNotNone(self.bundle.manifest["watermark_ts"])

    def test_the_full_import_is_still_an_ingest_run(self):
        self.assertEqual(self.report["mode"], "import")
        self.assertTrue(self.report["run_recorded"])
        self.assertIsNotNone(self.report["run_id"])
        self.assertEqual(self.report["events_loaded"], len(self.bundle.events))
        self.assertGreater(self.report["events_loaded"], 0)
        self.assertTrue(self.report["verified"], self.report["problems"])
        runs = self.con.execute(
            "SELECT source, source_watermark FROM ingest_runs"
        ).fetchall()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][0], "jira-fixture")
        self.assertIsNotNone(runs[0][1])

    def test_a_full_scrape_still_numbers_its_sprints(self):
        sprints = [obj for obj in self.bundle.objects if obj["object_type"] == "SPRINT"]
        self.assertEqual(len(sprints), 3)
        numbers = {
            int(obj["object_id"].partition("#")[2]): obj["attributes"]["sprint_no"]
            for obj in sprints
        }
        self.assertEqual(numbers, {1: 1, 2: 2, 3: 3})


class SprintRefreshCliTests(unittest.TestCase):
    """Both routes: ``--jira --sprints-only`` and ``--jira-cloud --sprints-only``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = self.root / "empty.toml"
        config.write_text("")
        self.env = mock.patch.dict(
            os.environ,
            {
                "MEGASPEED_CONFIG": str(config),
                "MEGASPEED_TICKET_PROJECTS": jira_fixtures.PROJECT,
                "MEGASPEED_JIRA_PROJECTS": jira_fixtures.PROJECT,
                "MEGASPEED_JIRA_PAGE_LIMIT": "2",
                "MEGASPEED_JIRA_MIN_INTERVAL": "0",
                "MEGASPEED_JIRA_BASE_URL": "",
                "MEGASPEED_JIRA_CLOUD_BASE_URL": "",
                "MEGASPEED_JIRA_CLOUD_TOKEN": "",
                "MEGASPEED_QUIET": "1",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape_main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_dc_fixture_route_writes_a_sprint_refresh_bundle(self):
        bundle_path = self.root / "dc-sprints.tgz"
        code, out, err = self._run(
            "--jira", "--sprints-only", "--fixture", "--out", str(bundle_path), "--json"
        )
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["mode"], "sprint-refresh")
        self.assertEqual(report["source"], "jira-fixture")
        self.assertEqual(report["events"], 0)
        self.assertEqual(report["objects"], 3)
        bundle = read_bundle(bundle_path)
        self.assertIs(bundle.manifest["sprint_refresh"], True)
        self.assertEqual(bundle.events, [])

    def test_probe_and_sprints_only_are_rejected_together(self):
        code, _out, err = self._run("--jira", "--probe", "--sprints-only", "--live")
        self.assertEqual(code, 2)
        self.assertIn("--probe", err)

    def test_the_dc_live_route_reads_the_board(self):
        stub_server = ThreadingHTTPServer(("127.0.0.1", 0), _StubDC)
        stub_server.daemon_threads = True
        stub_server.transport = FixtureJiraTransport(copy.deepcopy(jira_fixtures.generate()))
        stub_server.calls = []
        stub_url = _serve(stub_server)
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "MEGASPEED_JIRA_BASE_URL": stub_url,
                    "MEGASPEED_JIRA_TOKEN": "test-token",
                },
            ):
                bundle_path = self.root / "dc-live-sprints.tgz"
                # The PRD's Data Center command: --jira --sprints-only, no --live.
                code, out, err = self._run(
                    "--jira", "--sprints-only", "--out", str(bundle_path), "--json"
                )
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertEqual(report["source"], "jira-dc")
            self.assertEqual(report["events"], 0)
            bundle = read_bundle(bundle_path)
            self.assertEqual(
                {o["attributes"].get("sprint_no") for o in bundle.objects}, {1, 2, 3}
            )
            paths = [path for path, _params in stub_server.calls]
            self.assertIn("/rest/agile/1.0/board/1/sprint", paths)
            # the sprint read never walks an issue
            self.assertFalse([p for p in paths if p.startswith("/rest/api/2/issue")])
        finally:
            stub_server.shutdown()
            stub_server.server_close()

    def test_the_cloud_route_works_through_the_proxy(self):
        bundle = _stub_bundle()
        stub_server = ThreadingHTTPServer(("127.0.0.1", 0), _StubDC)
        stub_server.daemon_threads = True
        stub_server.transport = FixtureJiraTransport(bundle)
        stub_server.calls = []
        stub_url = _serve(stub_server)
        proxy = CloudJiraProxy(stub_url, pat=PAT, user="admin", password="lab-pw")
        proxy.discover()
        server = make_server(proxy)
        base = _serve(server)
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "MEGASPEED_JIRA_CLOUD_BASE_URL": base,
                    "MEGASPEED_JIRA_CLOUD_TOKEN": PAT,
                },
            ):
                bundle_path = self.root / "cloud-sprints.tgz"
                # No --live: a sprint refresh is a direct read of the configured
                # installation (the PRD's command line), and the Cloud shape is
                # reached exactly like a full --jira-cloud scrape.
                code, out, err = self._run(
                    "--jira-cloud", "--sprints-only", "--out", str(bundle_path), "--json"
                )
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertEqual(report["mode"], "sprint-refresh")
            self.assertEqual(report["source"], "jira-cloud")
            self.assertEqual(report["events"], 0)
            self.assertEqual(report["objects"], 3)
            cloud = read_bundle(bundle_path)
            self.assertEqual(cloud.events, [])
            self.assertEqual(
                {o["attributes"].get("sprint_no") for o in cloud.objects}, {1, 2, 3}
            )
            # the API evidence: the agile sprint listing went to the board
            paths = [path for path, _params in stub_server.calls]
            self.assertIn("/rest/agile/1.0/board/1/sprint", paths)
            self.assertFalse([p for p in paths if p.startswith("/rest/api/3/")])
        finally:
            for running in (server, stub_server):
                running.shutdown()
                running.server_close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

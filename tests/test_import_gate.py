"""The import is a gate, not an auditor (SEC-1).

A tgz crosses the airgap from the *less trusted* side. It must be checked for
content class (pseudonyms, hashes, identifiers, closed attribute bags) **before**
the store is opened for writing, and the merge itself must be transactional.
These tests tamper with an otherwise-valid bundle and assert that:

* the import is rejected (non-zero CLI exit), and
* not one row -- not even the database file -- was created.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ingest import fixtures, importer
from ingest.bundle import read_bundle, write_bundle
from ingest.config import Config
from ingest.importer import ImportRejected, import_bundle
from ingest.scrape import scrape
from ingest.transport import FixtureTransport


class ImportGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.cfg = Config(
            db_path=cls.root / "source.duckdb",
            identity_path=cls.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform"), ("ENG", "mobile")],
            ticket_projects=("PROJ", "OPS", "ARCH"),
        )
        cls.source = cls.root / "export" / "valid.tgz"
        scrape(
            cls.cfg,
            FixtureTransport(fixtures.generate(n=8, seed=41)),
            "fixture",
            out=cls.source,
            state_dir=cls.root / "export" / "valid.state",
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _tampered(self, mutate, *, name: str) -> Path:
        bundle = read_bundle(self.source)
        objects = [dict(obj) for obj in bundle.objects]
        events = [dict(event) for event in bundle.events]
        manifest = dict(bundle.manifest)
        mutate(objects=objects, events=events, manifest=manifest)
        path = self.root / f"tampered-{name}.tgz"
        write_bundle(
            path,
            manifest=manifest,
            events=events,
            objects=objects,
            relations=bundle.relations,
        )
        return path

    def _fresh_config(self, name: str) -> Config:
        return Config(
            db_path=self.root / f"import-{name}.duckdb",
            identity_path=self.root / f"import-{name}.json",
            ticket_regex=self.cfg.ticket_regex,
            key_shape=self.cfg.key_shape,
            repos=list(self.cfg.repos),
            ticket_projects=self.cfg.ticket_projects,
        )

    def assert_rejected_without_writing(self, path: Path, name: str) -> None:
        cfg = self._fresh_config(name)
        with self.assertRaises(ImportRejected):
            import_bundle(cfg, path)
        self.assertFalse(cfg.db_path.exists(), "a rejected bundle must not create a store")

    def test_a_raw_human_name_in_objects_actor_is_rejected_before_writing(self):
        def mutate(*, objects, **_):
            pr = next(obj for obj in objects if obj["object_type"] == "PR")
            pr["actor"] = "LauriNomme"

        path = self._tampered(mutate, name="actor")
        self.assert_rejected_without_writing(path, "actor")

    def test_a_branch_name_in_head_commit_is_rejected_before_writing(self):
        def mutate(*, objects, **_):
            commit = next(obj for obj in objects if obj["object_type"] == "COMMIT")
            commit["head_commit"] = "feature/secret-acquisition-q4"

        path = self._tampered(mutate, name="head")
        self.assert_rejected_without_writing(path, "head")

    def test_prose_event_key_and_run_id_are_rejected_before_writing(self):
        def mutate(*, events, manifest, **_):
            events[0]["event_key"] = "feature/secret-acquisition-q4"
            manifest.setdefault("run", {})["run_id"] = "Lauri Nomme run"

        path = self._tampered(mutate, name="keys")
        self.assert_rejected_without_writing(path, "keys")

    def test_the_cli_exits_non_zero_and_leaves_no_store(self):
        def mutate(*, objects, **_):
            pr = next(obj for obj in objects if obj["object_type"] == "PR")
            pr["actor"] = "Lauri Nomme"

        path = self._tampered(mutate, name="cli")
        db = self.root / "import-cli.duckdb"
        env = {
            "MEGASPEED_REPOS": "ENG/platform,ENG/mobile",
            "MEGASPEED_TICKET_PROJECTS": "PROJ,OPS,ARCH",
        }
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = importer.main([str(path), "--db", str(db)])
        self.assertNotEqual(code, 0)
        self.assertIn("pseudonymisation contract", err.getvalue())
        self.assertFalse(db.exists())

    def test_the_valid_bundle_still_imports(self):
        cfg = self._fresh_config("valid")
        report = import_bundle(cfg, self.source)
        self.assertTrue(report["verified"], report["problems"])
        self.assertGreater(report["events_new"], 0)


if __name__ == "__main__":
    unittest.main()

"""Jira scrape-side contract (stdlib only, no DuckDB, no network).

The scraper artifact's own CI job can afford these (prd/13): a Jira fixture goes
through the real client + reducer and produces a tgz that is
pseudonymised *by construction* -- no raw text in any member, no identity map,
closed vocabularies only, and a watermark/issue cursor next to the output that
never enters the bundle.

    python3 -m unittest discover -s scrape/tests -v
"""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from ingest import jira_fixtures, taxonomy
from ingest.bundle import BUNDLE_DIR, EVENTS, MANIFEST, OBJECTS, SUMS, read_bundle
from ingest.config import Config
from ingest.scrape_jira import ISSUE_STATE_FILE, JiraIssueState, scrape
from ingest.taxonomy import EVENT_KEY_RE
from ingest.transport import FixtureJiraTransport


def _config(root: Path) -> Config:
    return Config(
        db_path=root / "store.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[],
        ticket_projects=("PROJ", "OPS", "ARCH"),
        jira_projects=("PROJ",),
    )


class JiraBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.cfg = _config(cls.root)
        cls.fixture = jira_fixtures.generate()
        cls.out = cls.root / "export" / "jira.tgz"
        cls.state_dir = cls.root / "export" / "jira.state"
        cls.report = scrape(
            cls.cfg,
            FixtureJiraTransport(cls.fixture),
            "jira-fixture",
            out=cls.out,
            state_dir=cls.state_dir,
        )
        cls.bundle = read_bundle(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_tgz_is_the_four_member_contract(self):
        import tarfile

        with tarfile.open(self.out, "r:gz") as tar:
            names = sorted(tar.getnames())
        self.assertEqual(
            names,
            [f"{BUNDLE_DIR}/{name}" for name in (EVENTS, MANIFEST, OBJECTS, SUMS)],
        )

    def test_no_identity_map_and_no_raw_text_in_any_byte(self):
        raw = gzip.decompress(self.out.read_bytes()).lower()
        self.assertNotIn(b"zzcanary", raw)
        self.assertNotIn(b"identity.json", raw)
        for phrase in (
            b"add login form validation",
            b"sprint 1 scope",
            b"pairing on the parser",
            b"in progress",
        ):
            self.assertNotIn(phrase, raw)
        # the fixture really does carry the canaries
        self.assertIn("zzcanary", json.dumps(self.fixture).lower())

    def test_manifest_names_the_source_and_the_counts_agree(self):
        manifest = self.bundle.manifest
        self.assertEqual(manifest["source"], "jira-fixture")
        self.assertEqual(manifest["sources"], ["jira-fixture"])
        self.assertEqual(manifest["classifier_version"], taxonomy.CLASSIFIER_VERSION)
        self.assertEqual(manifest["counts"]["events"], len(self.bundle.events))
        self.assertEqual(manifest["counts"]["objects"], len(self.bundle.objects))
        self.assertEqual(manifest["counts"]["worklogs"], self.report["worklogs"])
        self.assertEqual(manifest["jira"]["sprints"], 3)
        self.assertEqual(manifest["run"]["issues_kept"], self.report["issues_kept"])

    def test_every_event_is_closed_and_references_a_bundled_object(self):
        object_ids = {o["object_id"] for o in self.bundle.objects}
        self.assertTrue(self.bundle.events)
        for event in self.bundle.events:
            self.assertIn(event["activity"], taxonomy.ACTIVITIES)
            self.assertRegex(event["event_key"], EVENT_KEY_RE)
            self.assertIsNone(event["repo"])
            self.assertIsNone(event["pr_id"])
            self.assertTrue(event["objects"], event["event_key"])
            for object_id in event["objects"]:
                self.assertIn(object_id, object_ids)
            taxonomy.check_attributes(event["attributes"])

    def test_objects_are_closed_and_carry_their_keys(self):
        for obj in self.bundle.objects:
            self.assertIn(obj["object_type"], taxonomy.OBJECT_TYPES)
            taxonomy.check_object_attributes(obj["attributes"])
            if obj["object_type"] in ("STORY", "EPIC"):
                self.assertTrue(obj["ticket_keys"], obj["object_id"])
            if obj["object_type"] == "SPRINT":
                self.assertRegex(obj["object_id"], r"^SPRINT:[A-Z][A-Z0-9_]{0,9}#\d+$")

    def test_state_stays_next_to_the_output(self):
        watermark = json.loads((self.state_dir / "watermark.json").read_text())
        self.assertIn("jira-fixture", watermark["sources"])
        self.assertIsNotNone(watermark["sources"]["jira-fixture"]["watermark_ts"])
        state = JiraIssueState(self.state_dir / ISSUE_STATE_FILE)
        self.assertEqual(len(state.entries("jira-fixture")), len(self.fixture["issues"]))
        self.assertGreater(state.done_count("jira-fixture"), 0)
        self.assertEqual(
            sorted(p.name for p in self.state_dir.iterdir()),
            ["identity.json", ISSUE_STATE_FILE, "watermark.json"],
        )

    def test_the_bundle_is_deterministic_for_the_same_fixture(self):
        other = self.root / "export" / "again.tgz"
        report = scrape(
            self.cfg,
            FixtureJiraTransport(jira_fixtures.generate()),
            "jira-fixture",
            out=other,
            state_dir=self.root / "export" / "again.state",
        )
        again = read_bundle(other)
        self.assertEqual(
            [e["event_key"] for e in again.events],
            [e["event_key"] for e in self.bundle.events],
        )
        self.assertEqual(
            [(o["object_type"], o["object_id"]) for o in again.objects],
            [(o["object_type"], o["object_id"]) for o in self.bundle.objects],
        )
        self.assertEqual(report["events"], self.report["events"])

    def test_a_fixture_file_roundtrips(self):
        path = jira_fixtures.write_bundle(self.root / "committed.json", n=4)
        fixture = json.loads(path.read_text())
        report = scrape(
            self.cfg,
            FixtureJiraTransport(fixture),
            "jira-fixture",
            out=self.root / "export" / "file.tgz",
            state_dir=self.root / "export" / "file.state",
        )
        self.assertEqual(report["issues_seen"], len(fixture["issues"]))
        self.assertEqual(report["counts"]["stories"], 4)


if __name__ == "__main__":
    unittest.main()

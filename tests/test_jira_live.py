"""Live check against the Jira DC lab (skipped when the lab is not running).

The fixture tests prove the pipeline; this one proves it against a real Jira DC
9.17.5. It scrapes the ``DEL`` project of the local lab and asserts the shape of
the corpus the lab seeds: 1 epic, 100 stories, 3 sprints and 199 native
worklogs, then imports the bundle and runs the contract check.

    # lab up:  http://127.0.0.1:8090  (admin / admin-lab-pw)
    python3 -m unittest tests.test_jira_live -v

Overridable with the same environment variables the scraper uses
(``MEGASPEED_JIRA_BASE_URL`` / ``_USER`` / ``_PASSWORD`` / ``_PROJECTS``). When
the lab is unreachable the test skips -- it never fails a CI box that has no lab.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ingest import taxonomy
from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.importer import import_bundle
from ingest.scrape_jira import scrape
from ingest.store import connect, verify
from ingest.transport import HttpTransport

BASE_URL = os.environ.get("MEGASPEED_JIRA_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
USER = os.environ.get("MEGASPEED_JIRA_USER", "admin")
PASSWORD = os.environ.get("MEGASPEED_JIRA_PASSWORD", "admin-lab-pw")
PROJECT = os.environ.get("MEGASPEED_JIRA_PROJECTS", "DEL").partition(",")[0].strip().upper()
SOURCE = "jira-dc"

#: The seeded corpus (jira-lab/README.md): epic DEL-1 + DEL-100..199 in three
#: sprints, with one native worklog per story plus a few extra.
EXPECTED_EPIC = 1
EXPECTED_STORIES = 100
EXPECTED_SPRINTS = 3
EXPECTED_WORKLOGS = 199


def _lab_is_up() -> bool:
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    request = urllib.request.Request(
        f"{BASE_URL}/rest/api/2/serverInfo",
        headers={"Accept": "application/json", "Authorization": f"Basic {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


class LiveJiraLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _lab_is_up():
            raise unittest.SkipTest(
                f"Jira lab not reachable at {BASE_URL} (start jira-lab/lab-up.sh,"
                " or set MEGASPEED_JIRA_BASE_URL)"
            )
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.cfg = Config(
            db_path=cls.root / "store.duckdb",
            identity_path=cls.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[],
            ticket_projects=(PROJECT,),
            jira_projects=(PROJECT,),
            jira_base_url=BASE_URL,
            jira_auth_user=USER,
            jira_auth_password=PASSWORD,
            jira_min_interval=0.0,
        )
        transport = HttpTransport(BASE_URL, user=USER, password=PASSWORD)
        cls.bundle_path = cls.root / "del-jira.tgz"
        cls.report = scrape(
            cls.cfg,
            transport,
            SOURCE,
            out=cls.bundle_path,
            state_dir=cls.root / "del-jira.state",
        )
        cls.imported = import_bundle(cls.cfg, cls.bundle_path)
        cls.bundle = read_bundle(cls.bundle_path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_seeded_corpus_is_fully_scraped(self):
        counts = self.report["counts"]
        self.assertEqual(counts["epics"], EXPECTED_EPIC)
        self.assertEqual(counts["stories"], EXPECTED_STORIES)
        self.assertEqual(counts["sprints"], EXPECTED_SPRINTS)
        self.assertEqual(counts["worklogs"], EXPECTED_WORKLOGS)
        self.assertEqual(self.report["worklogs"], EXPECTED_WORKLOGS)

    def test_custom_fields_are_discovered(self):
        fields = self.report["fields"]
        self.assertEqual(fields["epic_link"], "customfield_10100")
        self.assertEqual(fields["sprint"], "customfield_10104")
        self.assertEqual(fields["story_points"], "customfield_10106")

    def test_the_sprints_are_the_seeded_ones(self):
        sprints = {o["object_id"]: o for o in self.bundle.objects if o["object_type"] == "SPRINT"}
        self.assertEqual(
            sorted(sprints), [f"SPRINT:{PROJECT}#1", f"SPRINT:{PROJECT}#2", f"SPRINT:{PROJECT}#3"]
        )
        self.assertEqual(sprints[f"SPRINT:{PROJECT}#1"]["state"], "closed")
        self.assertEqual(sprints[f"SPRINT:{PROJECT}#2"]["state"], "closed")
        self.assertEqual(sprints[f"SPRINT:{PROJECT}#3"]["state"], "active")
        self.assertIsNotNone(sprints[f"SPRINT:{PROJECT}#1"]["closed_at"])
        self.assertIsNone(sprints[f"SPRINT:{PROJECT}#3"]["closed_at"])

    def test_every_story_joins_to_its_ticket_key(self):
        stories = [o for o in self.bundle.objects if o["object_type"] == "STORY"]
        self.assertEqual(len(stories), EXPECTED_STORIES)
        self.assertEqual(
            sorted(s["ticket_keys"][0] for s in stories),
            [f"{PROJECT}-{number}" for number in range(100, 200)],
        )
        self.assertTrue(all(s["attributes"].get("epic_key") == f"{PROJECT}-1" for s in stories))

    def test_worklogs_carry_the_logging_delay(self):
        logged = [e for e in self.bundle.events if e["activity"] == "worklogged"]
        self.assertEqual(len(logged), EXPECTED_WORKLOGS)
        for event in logged:
            self.assertIn(event["attributes"]["log_delay_bucket"], taxonomy.LOG_DELAY_BUCKET)
            self.assertGreaterEqual(event["attributes"]["log_delay_seconds"], 0)
            self.assertGreater(event["attributes"]["time_spent_seconds"], 0)
        # The lab backdates its worklogs into the sprints but wrote them at seed
        # time, so the noise signal is loud -- that is exactly the point of it.
        self.assertIn("late", self.report["log_delays"])
        self.assertGreater(sum(self.report["log_delays"].values()), 0)

    def test_status_transitions_are_categories(self):
        changes = [e for e in self.bundle.events if e["activity"] == "status_changed"]
        self.assertTrue(changes)
        for event in changes:
            self.assertIn(event["attributes"]["to_status"], taxonomy.STATUS_CLASS)

    def test_the_bundle_imports_and_passes_the_contract(self):
        self.assertTrue(self.imported["verified"], self.imported["problems"])
        con = connect(self.cfg.db_path, read_only=True)
        try:
            report = verify(con, self.cfg.key_shape, self.cfg.ticket_projects)
            self.assertTrue(report["ok"], report["problems"])
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM objects WHERE object_type = 'STORY'"
                ).fetchone()[0],
                EXPECTED_STORIES,
            )
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM events WHERE activity = 'worklogged'"
                ).fetchone()[0],
                EXPECTED_WORKLOGS,
            )
            # no raw text: the lab's descriptions carry "Ticket: DEL-xxx"
            payload = json.dumps(
                con.execute("SELECT event_key, attributes FROM events").fetchall(), default=str
            ).lower()
            self.assertNotIn("ticket:", payload)
            self.assertNotIn("zzcanary", payload)
        finally:
            con.close()


class LiveJiraEpicScopeTests(unittest.TestCase):
    """prd/27 against the real lab: an epic set keeps the epic and its children.

    ``MEGASPEED_JIRA_EPICS=DEL-1`` must resolve to ``DEL-1`` + ``DEL-100..199``
    (one extra search), scrape exactly that union, and produce the *same* bundle
    as handing the resolved keys to ``MEGASPEED_TICKET_KEYS``.
    """

    EPIC = f"{PROJECT}-1"

    @classmethod
    def setUpClass(cls):
        if not _lab_is_up():
            raise unittest.SkipTest(
                f"Jira lab not reachable at {BASE_URL} (start jira-lab/lab-up.sh,"
                " or set MEGASPEED_JIRA_BASE_URL)"
            )
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

        def config(**overrides) -> Config:
            values = dict(
                db_path=cls.root / "store.duckdb",
                identity_path=cls.root / "identity.json",
                ticket_regex=r"\b[A-Z]+-\d+\b",
                key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
                repos=[],
                ticket_projects=(PROJECT,),
                jira_base_url=BASE_URL,
                jira_auth_user=USER,
                jira_auth_password=PASSWORD,
                jira_min_interval=0.0,
            )
            values.update(overrides)
            return Config(**values)

        transport = HttpTransport(BASE_URL, user=USER, password=PASSWORD)
        cls.cfg = config(jira_epics=(cls.EPIC,))
        cls.bundle_path = cls.root / "del-epic.tgz"
        cls.report = scrape(
            cls.cfg, transport, SOURCE, out=cls.bundle_path,
            state_dir=cls.root / "del-epic.state",
        )
        cls.imported = import_bundle(cls.cfg, cls.bundle_path)
        cls.bundle = read_bundle(cls.bundle_path)

        # The explicit-key-set path over the very same keys: same walk, so the
        # bundles must agree object for object.
        cls.keys_cfg = config(ticket_keys=tuple(cls.report["ticket_keys"]))
        cls.keys_bundle_path = cls.root / "del-keys.tgz"
        cls.keys_report = scrape(
            cls.keys_cfg, transport, SOURCE, out=cls.keys_bundle_path,
            state_dir=cls.root / "del-keys.state",
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_epic_and_its_children_are_what_is_kept(self):
        counts = self.report["counts"]
        self.assertEqual(self.report["jira_epics"], [self.EPIC])
        self.assertEqual(counts["epics"], EXPECTED_EPIC)
        self.assertEqual(counts["stories"], EXPECTED_STORIES)
        self.assertEqual(counts["worklogs"], EXPECTED_WORKLOGS)
        self.assertEqual(self.report["issues_seen"], EXPECTED_EPIC + EXPECTED_STORIES)

    def test_the_resolved_set_is_the_epic_plus_every_story(self):
        expected = {self.EPIC} | {
            f"{PROJECT}-{number}" for number in range(100, 100 + EXPECTED_STORIES)
        }
        self.assertEqual(set(self.report["ticket_keys"]), expected)
        self.assertEqual(set(self.bundle.ticket_keys), expected)
        self.assertEqual(self.bundle.manifest["jira_epics"], [self.EPIC])

    def test_it_costs_exactly_one_extra_search(self):
        self.assertEqual(
            self.report["requests_by_kind"]["search"],
            self.keys_report["requests_by_kind"]["search"] + 1,
        )

    def test_it_is_the_same_bundle_as_the_explicit_key_set(self):
        self.assertEqual(
            sorted(o["object_id"] for o in self.bundle.objects),
            sorted(o["object_id"] for o in read_bundle(self.keys_bundle_path).objects),
        )
        self.assertEqual(
            self.keys_report["counts"], self.report["counts"]
        )

    def test_the_import_and_the_verify_use_the_resolved_set(self):
        self.assertTrue(self.imported["verified"], self.imported["problems"])
        self.assertEqual(
            set(self.imported["ticket_keys"]), set(self.report["ticket_keys"])
        )
        con = connect(self.cfg.db_path, read_only=True)
        try:
            report = verify(
                con, self.cfg.key_shape, self.cfg.ticket_projects, self.bundle.ticket_keys
            )
            self.assertTrue(report["ok"], report["problems"])
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM objects WHERE object_type = 'STORY'"
                ).fetchone()[0],
                EXPECTED_STORIES,
            )
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()

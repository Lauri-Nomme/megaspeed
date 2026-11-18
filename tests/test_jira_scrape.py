"""Jira fixture -> scrape -> tgz -> import, with the contract checked at every step.

This is the Jira counterpart of the Bitbucket end-to-end tests: a deterministic
Jira DC payload goes through the *real* client + reducer, the tgz is read back,
imported into a DuckDB store, and ``--verify`` must pass. It asserts what the
pseudonymisation contract promises (no summary/description/comment/status name
anywhere) and what the analysis needs (Epic/Story/Sprint objects, the closed
event set, the worklog-noise gap).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ingest import fixtures, jira_fixtures, taxonomy
from ingest.__main__ import main as ingest_main
from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.importer import import_bundle
from ingest.jira_fixtures import PROJECT
from ingest.scrape_cli import main as scrape_main
from ingest.scrape_jira import JiraIssueState, jql_for, scrape
from ingest.store import EVENT_KEY_RE, connect, list_events, list_objects, verify
from ingest.transport import FixtureJiraTransport, FixtureTransport

JIRA_SOURCE = "jira-fixture"
WORKLOG_DELAYS = {"same_day", "next_day", "within_week", "late"}


def _config(root: Path, *, projects=("PROJ",)) -> Config:
    return Config(
        db_path=root / "store.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[],
        ticket_projects=("PROJ", "OPS", "ARCH"),
        jira_projects=tuple(projects),
    )


class FixtureScrapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.cfg = _config(cls.root)
        cls.bundle_path = cls.root / "export" / "jira.tgz"
        cls.fixture = jira_fixtures.generate()
        cls.report = scrape(
            cls.cfg,
            FixtureJiraTransport(cls.fixture),
            JIRA_SOURCE,
            out=cls.bundle_path,
            state_dir=cls.root / "export" / "jira.state",
        )
        cls.imported = import_bundle(cls.cfg, cls.bundle_path)
        cls.con = connect(cls.cfg.db_path)
        cls.bundle = read_bundle(cls.bundle_path)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    # -- objects ---------------------------------------------------------
    def test_objects_are_epic_story_sprint_and_people(self):
        counts = self.report["counts"]
        self.assertEqual(counts["epics"], 1)
        self.assertEqual(counts["stories"], len(jira_fixtures.MEMBERSHIP))
        self.assertEqual(counts["sprints"], 3)
        self.assertEqual(
            sorted({o["object_type"] for o in list_objects(self.con)}),
            ["EPIC", "PERSON", "SPRINT", "STORY"],
        )

    def test_every_story_carries_its_ticket_key(self):
        stories = list_objects(self.con, "STORY")
        self.assertTrue(stories)
        for story in stories:
            self.assertEqual(len(story["ticket_keys"]), 1)
            self.assertRegex(story["ticket_keys"][0], self.cfg.key_shape)
            self.assertEqual(story["object_id"], f"STORY:{story['ticket_keys'][0]}")
            self.assertIsNone(story["repo"])
            self.assertIsNone(story["pr_id"])
            self.assertIn(story["state"], taxonomy.ISSUE_STATE)

    def test_story_points_and_epic_link_are_on_the_object(self):
        stories = {s["ticket_keys"][0]: s for s in list_objects(self.con, "STORY")}
        self.assertEqual(stories["PROJ-101"]["attributes"]["epic_key"], "PROJ-1")
        self.assertEqual(stories["PROJ-101"]["attributes"]["points"], 5)
        # 2.5 story points are rounded to whole points, never truncated to 0
        self.assertEqual(stories["PROJ-103"]["attributes"]["points"], 3)
        self.assertEqual(self.report["points_rounded"], 2)
        # a resolved story is closed, an open one is not
        self.assertEqual(stories["PROJ-101"]["state"], "done")
        self.assertIsNotNone(stories["PROJ-101"]["closed_at"])
        self.assertEqual(stories["PROJ-111"]["state"], "open")
        self.assertIsNone(stories["PROJ-111"]["closed_at"])

    def test_sprints_carry_start_planned_end_completion_and_state(self):
        sprints = {s["object_id"]: s for s in list_objects(self.con, "SPRINT")}
        first = sprints["SPRINT:PROJ#1"]
        self.assertEqual(first["state"], "closed")
        self.assertEqual(first["created_at"], "2026-08-17T09:00:00Z")
        self.assertEqual(first["attributes"]["end_date"], "2026-09-11")
        self.assertEqual(first["closed_at"], "2026-09-11T17:30:00Z")
        active = sprints["SPRINT:PROJ#3"]
        self.assertEqual(active["state"], "active")
        self.assertIsNone(active["closed_at"])

    # -- events ----------------------------------------------------------
    def test_only_enumerated_activities_and_well_formed_keys(self):
        activities = {e["activity"] for e in list_events(self.con)}
        self.assertTrue(activities)
        self.assertTrue(activities <= taxonomy.ACTIVITIES, activities)
        for event in list_events(self.con):
            self.assertRegex(event["key"], EVENT_KEY_RE)
            self.assertTrue(event["objects"], event["key"])
            self.assertIsNone(event["repo"])
            self.assertIsNone(event["pr_id"])

    def test_every_event_references_an_object_in_the_bundle(self):
        object_ids = {o["object_id"] for o in list_objects(self.con)}
        for event in list_events(self.con):
            for object_id in event["objects"]:
                self.assertIn(object_id, object_ids, event["key"])

    def test_status_transitions_are_stages(self):
        rows = self.con.execute(
            "SELECT attributes FROM events WHERE activity = 'status_changed'"
        ).fetchall()
        self.assertTrue(rows)
        for (attributes,) in rows:
            payload = json.loads(attributes) if isinstance(attributes, str) else attributes
            self.assertIn(payload["from_status"], taxonomy.STATUS_STAGE)
            self.assertIn(payload["to_status"], taxonomy.STATUS_STAGE)
            self.assertNotEqual(payload["from_status"], payload["to_status"])

    def test_sprint_events_cover_removal_and_membership_at_start(self):
        added = self.con.execute(
            "SELECT event_key, attributes FROM events WHERE activity = 'sprint_added'"
        ).fetchall()
        removed = self.con.execute(
            "SELECT event_key, attributes FROM events WHERE activity = 'sprint_removed'"
        ).fetchall()
        self.assertTrue(added)
        self.assertTrue(removed)
        at_start = [
            key
            for key, attributes in added
            if json.loads(attributes if isinstance(attributes, str) else attributes).get("at_start")
        ]
        self.assertEqual(len(at_start), 1)
        self.assertTrue(at_start[0].startswith("PROJ-104:"))
        carried = [key for key, _a in removed]
        self.assertTrue(carried[0].startswith("PROJ-105:"))

    def test_worklogs_carry_the_created_minus_started_gap(self):
        rows = self.con.execute(
            "SELECT attributes FROM events WHERE activity = 'worklogged'"
        ).fetchall()
        self.assertEqual(len(rows), self.report["worklogs"])
        self.assertGreater(len(rows), 0)
        buckets = set()
        for (attributes,) in rows:
            payload = json.loads(attributes) if isinstance(attributes, str) else attributes
            self.assertIn(payload["log_delay_bucket"], taxonomy.LOG_DELAY_BUCKET)
            self.assertIsInstance(payload["log_delay_seconds"], int)
            self.assertGreaterEqual(payload["log_delay_seconds"], 0)
            self.assertGreater(payload["time_spent_seconds"], 0)
            buckets.add(payload["log_delay_bucket"])
        # the fixture spreads its logs over every bucket, so the signal is real
        self.assertEqual(buckets, WORKLOG_DELAYS)
        self.assertEqual(set(self.report["log_delays"]), WORKLOG_DELAYS)

    def test_one_worklogged_event_per_native_worklog(self):
        native = sum(
            len(((issue.get("fields") or {}).get("worklog") or {}).get("worklogs") or [])
            for issue in self.fixture["issues"]
        )
        self.assertEqual(native, self.report["worklogs"])
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM events WHERE activity = 'worklogged'"
            ).fetchone()[0],
            native,
        )

    # -- the contract ----------------------------------------------------
    def test_contract_holds_after_import(self):
        self.assertTrue(self.imported["verified"], self.imported["problems"])
        report = verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)
        self.assertTrue(report["ok"], report["problems"])

    def test_no_raw_jira_text_reaches_the_store(self):
        payload = json.dumps(
            {"events": list_events(self.con), "objects": list_objects(self.con)},
            default=str,
        ).lower()
        self.assertNotIn("zzcanary", payload)
        for phrase in (
            "add login form validation",   # a summary
            "delivery platform epic",      # the epic summary
            "sprint 1 scope",              # a sprint goal
            "pairing on the parser",       # a worklog comment
            "in progress",                 # a raw status name
            "rejected",                    # a raw resolution name
        ):
            self.assertNotIn(phrase, payload)
        # ... while the fixture really does contain them
        raw = json.dumps(self.fixture).lower()
        for phrase in ("zzcanary", "add login form validation", "pairing on the parser"):
            self.assertIn(phrase, raw)

    def test_bundle_manifest_names_the_jira_source_and_counts(self):
        self.assertEqual(self.bundle.source, JIRA_SOURCE)
        self.assertEqual(self.bundle.manifest["projects"], [PROJECT])
        self.assertEqual(self.bundle.manifest["jira"]["sprints"], 3)
        self.assertEqual(
            self.bundle.manifest["jira"]["fields"],
            {
                "epic_link": "customfield_10100",
                "sprint": "customfield_10104",
                "story_points": "customfield_10106",
            },
        )
        self.assertEqual(
            self.bundle.manifest["counts"]["worklogs"], self.report["worklogs"]
        )

    def test_import_is_idempotent(self):
        before = self.con.execute("SELECT count(*) FROM events").fetchone()[0]
        again = import_bundle(self.cfg, self.bundle_path)
        self.assertEqual(again["events_new"], 0)
        self.assertEqual(again["events_rewritten"], 0)
        self.assertEqual(
            self.con.execute("SELECT count(*) FROM events").fetchone()[0], before
        )


class IncrementalScrapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _config(self.root)
        self.bundle_path = self.root / "export" / "jira.tgz"
        self.state_dir = self.root / "export" / "jira.state"
        self.fixture = jira_fixtures.generate()

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, fixture=None, **kwargs) -> dict:
        return scrape(
            self.cfg,
            FixtureJiraTransport(fixture or self.fixture),
            JIRA_SOURCE,
            out=self.bundle_path,
            state_dir=self.state_dir,
            **kwargs,
        )

    def test_the_first_run_reads_every_issue(self):
        first = self.ingest()
        self.assertEqual(first["issues_seen"], len(self.fixture["issues"]))
        self.assertEqual(first["issues_kept"], len(self.fixture["issues"]))
        self.assertEqual(first["issues_skipped"], 0)

    def test_the_second_run_only_rechecks_the_watermark_boundary(self):
        self.ingest()
        second = self.ingest()
        total = len(self.fixture["issues"])
        # The JQL asks only for ``updated >= watermark - overlap``, so the second
        # run reads a handful of boundary issues instead of all of them.
        self.assertLess(second["issues_seen"], total)
        self.assertEqual(
            second["issues_kept"] + second["issues_skipped"], second["issues_seen"]
        )
        # re-importing the incremental bundle adds nothing
        import_bundle(self.cfg, self.bundle_path)
        third = import_bundle(self.cfg, self.bundle_path)
        self.assertEqual(third["events_new"], 0)

    def test_full_ignores_the_cursor(self):
        self.ingest()
        full = self.ingest(full=True)
        self.assertEqual(full["issues_kept"], len(self.fixture["issues"]))
        self.assertEqual(full["issues_skipped"], 0)

    def test_a_moved_issue_is_read_again(self):
        self.ingest()
        # A worklog logged later moves the issue's ``updated``: the issue comes
        # back and the new event lands, while the rest stay settled.
        moved = json.loads(json.dumps(self.fixture))
        story = next(i for i in moved["issues"] if i["key"] == "PROJ-111")
        story["fields"]["updated"] = "2027-02-01T12:00:00.000+0000"
        story["fields"]["worklog"]["worklogs"].append({
            "id": "99999",
            "author": story["fields"]["assignee"],
            "started": "2027-01-30T09:00:00.000+0000",
            "created": "2027-02-01T11:00:00.000+0000",
            "timeSpentSeconds": 1800,
            "comment": "zzcanary late log",
        })
        story["fields"]["worklog"]["total"] += 1
        second = self.ingest(fixture=moved)
        self.assertLess(second["issues_kept"], len(self.fixture["issues"]))
        import_bundle(self.cfg, self.bundle_path)
        rows = connect(self.cfg.db_path, read_only=True)
        try:
            self.assertEqual(
                rows.execute(
                    "SELECT count(*) FROM events WHERE event_key = 'PROJ-111:worklog:99999'"
                ).fetchone()[0],
                1,
            )
            # the moved issue was re-read, the untouched ones were not
            keys = {
                row[0]
                for row in rows.execute(
                    "SELECT DISTINCT split_part(event_key, ':', 1) FROM events"
                    " WHERE activity = 'worklogged'"
                ).fetchall()
            }
            self.assertIn("PROJ-111", keys)
        finally:
            rows.close()

    def test_issue_state_file_keeps_resolved_issues_done(self):
        self.ingest()
        state = JiraIssueState(self.state_dir / "jira_issues.json")
        done = state.done_count(JIRA_SOURCE)
        self.assertGreater(done, 0)
        self.assertLess(done, len(self.fixture["issues"]))

    def test_an_explicit_key_set_scopes_the_whole_run(self):
        # prd/18: on a shared project a prefix matches everyone's tickets, so a
        # key set replaces the project clause -- one query, one issue.
        self.cfg.ticket_keys = ("PROJ-101",)
        report = self.ingest()
        self.assertEqual(report["issues_seen"], 1)
        self.assertEqual(report["issues_kept"], 1)
        self.assertEqual(report["counts"]["stories"], 1)
        self.assertEqual(report["counts"]["epics"], 0)


class JqlTests(unittest.TestCase):
    def test_project_scope(self):
        jql = jql_for("DEL")
        self.assertIn('project = "DEL"', jql)
        self.assertIn("issuetype in (Epic, Story, Bug, Task)", jql)
        self.assertNotIn("updated >=", jql)

    def test_key_set_replaces_the_project_scope(self):
        jql = jql_for("DEL", keys=("DEL-100", "DEL-101"))
        self.assertIn("key in (DEL-100,DEL-101)", jql)
        self.assertNotIn("project =", jql)

    def test_since_is_a_jira_timestamp(self):
        from datetime import datetime

        jql = jql_for("DEL", since=datetime(2026, 11, 1, 8, 30))
        self.assertIn('updated >= "2026/11/01 08:30"', jql)


class CrossSourceIdentityTests(unittest.TestCase):
    """The Bitbucket and Jira bundles must resolve to *one* person per human."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=self.root / "store.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform"), ("ENG", "mobile")],
            ticket_projects=("PROJ", "OPS", "ARCH"),
            jira_projects=("PROJ",),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_pseudonym_per_human_across_both_sources(self):
        from ingest.pipeline import run as run_bitbucket

        run_bitbucket(
            self.cfg,
            FixtureTransport(fixtures.generate(n=12, seed=20261115)),
            source="fixture",
        )
        jira_bundle = self.root / "jira.tgz"
        scrape(
            self.cfg,
            FixtureJiraTransport(jira_fixtures.generate()),
            JIRA_SOURCE,
            out=jira_bundle,
            state_dir=self.root / "jira.state",
            identity_path=self.cfg.identity_path,
        )
        imported = import_bundle(self.cfg, jira_bundle)
        self.assertTrue(imported["verified"], imported["problems"])

        con = connect(self.cfg.db_path, read_only=True)
        try:
            # One PERSON object per pseudonym, whatever the source.
            persons = [o["object_id"] for o in list_objects(con, "PERSON")]
            self.assertEqual(len(persons), len(set(persons)))

            # The story reporter (lauri.nomme) is the same human as the git
            # author with that slug, so the pseudonym is identical.
            story_actors = {
                row[0]
                for row in con.execute(
                    "SELECT actor FROM objects WHERE object_type = 'STORY'"
                ).fetchall()
            }
            pr_actors = {
                row[0]
                for row in con.execute(
                    "SELECT actor FROM objects WHERE object_type = 'PR'"
                ).fetchall()
            }
            self.assertTrue(story_actors & pr_actors, (story_actors, pr_actors))
            self.assertIn("LaNo", story_actors & pr_actors)

            # And the ticket keys join the two layers.
            joined = con.execute(
                "SELECT count(DISTINCT s.ticket_keys[1]) FROM objects s"
                " JOIN objects p ON p.object_type = 'PR'"
                " WHERE s.object_type = 'STORY' AND list_contains(p.ticket_keys, s.ticket_keys[1])"
            ).fetchone()[0]
            self.assertGreater(joined, 0)
        finally:
            con.close()

    def test_the_identity_map_records_the_jira_link(self):
        from ingest.pipeline import run as run_bitbucket

        run_bitbucket(
            self.cfg,
            FixtureTransport(fixtures.generate(n=6, seed=7)),
            source="fixture",
        )
        scrape(
            self.cfg,
            FixtureJiraTransport(jira_fixtures.generate()),
            JIRA_SOURCE,
            out=self.root / "jira.tgz",
            state_dir=self.root / "jira.state",
            identity_path=self.cfg.identity_path,
        )
        identity = json.loads(self.cfg.identity_path.read_text())["actors"]
        linked = {k: v for k, v in identity.items() if v.get("linked_to")}
        self.assertTrue(linked, identity)
        for key, entry in linked.items():
            self.assertTrue(key.startswith("jira:"), key)
            self.assertEqual(identity[entry["linked_to"]]["pseudonym"], entry["pseudonym"])


class CliFixtureLimitTests(unittest.TestCase):
    """FR-8: ``--jira --fixture --limit N`` must actually read at most N issues.

    The CLI used to hand ``None`` to ``scrape_jira`` on the fixture path while
    the live path passed ``args.limit``, so a "dev/demo" cap silently read the
    whole fixture. Drive the real CLI (config + transport + reducer) twice and
    compare the reports: the capped run must emit fewer issues *and* say it was
    truncated, so the cursor is left alone for the next run.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # an empty config: the fixture needs no repos/credentials, and the
        # defaults (PROJ/OPS/ARCH) already match the Jira fixture project.
        self.config = self.root / "empty.toml"
        self.config.write_text("")
        self.env = mock.patch.dict(
            os.environ,
            {
                "MEGASPEED_CONFIG": str(self.config),
                "MEGASPEED_TICKET_PROJECTS": "PROJ",
                "MEGASPEED_QUIET": "1",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def scrape(self, *argv: str) -> dict:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape_main(
                [
                    "--jira", "--fixture", "--json",
                    "--out", str(self.root / f"jira-{len(argv)}.tgz"),
                    "--state-dir", str(self.root / f"jira-{len(argv)}.state"),
                    *argv,
                ]
            )
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue())

    def test_limit_caps_the_fixture_run(self):
        full = self.scrape("--n", "12")
        capped = self.scrape("--n", "12", "--limit", "3")

        self.assertGreater(full["issues_kept"], 3, full)
        self.assertEqual(capped["issues_kept"], 3)
        self.assertLess(capped["issues_kept"], full["issues_kept"])
        # the cap is a stop, not a silent success: the run reports it and the
        # watermark stays put so the unread issues are read next time.
        self.assertTrue(capped["truncated"])
        self.assertFalse(full["truncated"])

    def test_limit_above_the_fixture_size_reads_everything(self):
        full = self.scrape("--n", "5")
        generous = self.scrape("--n", "5", "--limit", "500")
        self.assertEqual(generous["issues_kept"], full["issues_kept"])
        self.assertFalse(generous["truncated"])


class _ProbeClientStub:
    """The Jira client surface ``_jira_probe`` drives, without a network."""

    def __init__(self, transport, page_limit=None, min_interval=None, tempo=False):
        self.transport = transport
        self.requests = 0
        self.requests_by_kind = {}
        self.tempo = tempo

    def check(self):
        return {"version": "9.12.0"}

    def field_ids(self):
        return {"Epic Link": "cf-epic", "Sprint": "cf-sprint", "Story Points": "cf-points"}

    def scrum_board(self, project):
        return 11

    def sprints(self, board):
        return [{"id": 7, "state": "active", "name": "Sprint 7"}]

    def search(self, jql, fields="key,updated"):
        return [{"key": "PROJ-1", "fields": {"issuetype": {"name": "Story"}}}]


class JiraProbeCliTests(unittest.TestCase):
    """``python3 -m ingest --jira --probe --live`` must reach the probe.

    ``_run_jira`` unpacked three values from ``_jira_transport`` (which returns
    four), so the dev-shortcut probe died with ``ValueError: too many values to
    unpack`` before printing a report. Drive the real CLI (parser + config +
    probe wiring) with a stubbed transport/client: no network, no store.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = self.root / "empty.toml"
        config.write_text("")
        self.env = mock.patch.dict(os.environ, {
            "MEGASPEED_CONFIG": str(config),
            "MEGASPEED_TICKET_PROJECTS": "PROJ",
            "MEGASPEED_QUIET": "1",
            "MEGASPEED_JIRA_BASE_URL": "http://jira.invalid",
            "MEGASPEED_JIRA_TOKEN": "test-token",
            "MEGASPEED_JIRA_CLOUD_BASE_URL": "",
            "MEGASPEED_JIRA_CLOUD_TOKEN": "",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def probe(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("ingest.scrape_cli.HttpTransport", lambda *a, **k: object()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ingest_main(["--jira", "--probe", "--live", "--json", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_probe_live_prints_a_report(self):
        with mock.patch("ingest.jira.JiraClient", _ProbeClientStub):
            code, out, err = self.probe()
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["version"], "9.12.0")
        self.assertEqual(report["boards"], {"PROJ": 11})
        self.assertEqual(report["sprints"]["PROJ"],
                         [{"id": 7, "state": "active", "name": "Sprint 7"}])
        self.assertEqual(report["issues"]["PROJ"], {"total": 1, "types": {"Story": 1}})
        self.assertEqual(report["fields"]["epic_link"], "cf-epic")

    def test_cloud_probe_uses_the_cloud_client(self):
        built = {"dc": 0, "cloud": 0}

        class Dc(_ProbeClientStub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                built["dc"] += 1

        class Cloud(_ProbeClientStub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                built["cloud"] += 1

            def check(self):
                return {"version": "1001.0.0"}

        env = mock.patch.dict(os.environ, {
            "MEGASPEED_JIRA_CLOUD_BASE_URL": "http://cloud.invalid",
            "MEGASPEED_JIRA_CLOUD_TOKEN": "cloud-token",
        })
        with env, mock.patch("ingest.jira.JiraClient", Dc), \
                mock.patch("ingest.jira_cloud.CloudJiraClient", Cloud):
            code, out, err = self.probe("--jira-cloud")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["version"], "1001.0.0")
        # the client the transport built is handed to the probe; building a DC
        # client here would talk the wrong API shape to a Cloud site.
        self.assertEqual(built, {"dc": 0, "cloud": 1})


if __name__ == "__main__":
    unittest.main()

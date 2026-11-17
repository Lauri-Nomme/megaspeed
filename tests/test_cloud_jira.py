"""The Jira **Cloud** source (prd/24): the v3 client and DC/Cloud equivalence.

Two layers:

* :class:`CloudJiraClientTests` -- the Cloud client in isolation, against a
  recording transport: v3 paths, Bearer auth (the transport's job), cursor
  pagination, the documented-field fallback, and the Tempo worklog source. No
  lab, no network.
* :class:`CloudLabEquivalenceTests` -- the real thing: scrape the DC lab with
  ``--jira`` and the same lab *through the emulation proxy* with
  ``--jira-cloud``, then diff the objects, events and relations. Skipped when
  the lab is not running, like ``tests/test_jira_live.py``.

    # lab up:  http://127.0.0.1:8090  (admin / admin-lab-pw)
    python3 -m unittest tests.test_cloud_jira -v
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.importer import import_bundle
from ingest.jira import request_kind
from ingest.jira_cloud import CLOUD_FIELD_IDS, CloudJiraClient
from ingest.scrape_cli import build_parser, main as scrape_main
from ingest.scrape_jira import scrape
from ingest.store import connect, verify
from ingest.transport import HttpTransport
from proxy.cloud_jira_proxy import CloudJiraProxy, make_server

BASE_URL = os.environ.get("MEGASPEED_JIRA_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
USER = os.environ.get("MEGASPEED_JIRA_USER", "admin")
PASSWORD = os.environ.get("MEGASPEED_JIRA_PASSWORD", "admin-lab-pw")
PROJECT = os.environ.get("MEGASPEED_JIRA_PROJECTS", "DEL").partition(",")[0].strip().upper()
PAT = "cloud-lab-pat"

#: The seeded lab corpus (jira-lab/README.md).
EXPECTED = {"epics": 1, "stories": 100, "sprints": 3, "worklogs": 199}


class _Recorder:
    """A transport that records the request and answers from a handler."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append((path, dict(params or {})))
        return self.handler(path, params or {})


class CloudJiraClientTests(unittest.TestCase):
    def test_search_uses_the_v3_cursor_endpoint(self):
        pages = {
            "": {"issues": [{"key": "A-1"}], "nextPageToken": "1", "isLast": False},
            "1": {"issues": [{"key": "A-2"}], "isLast": True},
        }
        transport = _Recorder(lambda path, params: pages[params.get("nextPageToken", "")])
        client = CloudJiraClient(transport, page_limit=1, list_page_limit=1)

        rows = list(client.search('project = "A"'))

        self.assertEqual([row["key"] for row in rows], ["A-1", "A-2"])
        self.assertEqual([call[0] for call in transport.calls], ["/rest/api/3/search/jql"] * 2)
        self.assertNotIn("nextPageToken", transport.calls[0][1])
        self.assertEqual(transport.calls[1][1]["nextPageToken"], "1")
        self.assertEqual(transport.calls[0][1]["fields"], "key,updated")
        self.assertEqual(transport.calls[0][1]["maxResults"], 1)

    def test_the_cursor_walk_stops_on_an_empty_page(self):
        transport = _Recorder(lambda path, params: {"issues": [], "nextPageToken": "9"})
        client = CloudJiraClient(transport, page_limit=5)
        self.assertEqual(list(client.search("project = A")), [])
        self.assertEqual(len(transport.calls), 1)

    def test_issue_and_worklog_use_v3_paths(self):
        transport = _Recorder(lambda path, params: {"key": "A-1"} if "worklog" not in path else
                              {"worklogs": [], "total": 0, "startAt": 0, "maxResults": 0})
        client = CloudJiraClient(transport)

        client.issue("A-1")
        client.worklogs("A-1")

        self.assertEqual(transport.calls[0][0], "/rest/api/3/issue/A-1")
        self.assertEqual(transport.calls[0][1]["expand"], "changelog")
        self.assertEqual(transport.calls[1][0], "/rest/api/3/issue/A-1/worklog")

    def test_field_ids_discover_first_and_fall_back_to_the_documented_ids(self):
        transport = _Recorder(lambda path, params: [
            {"id": "customfield_10100", "name": "Epic Link"},
            {"id": "customfield_10106", "name": "Story Points"},
        ])
        client = CloudJiraClient(transport)

        fields = client.field_ids()

        # Discovered on this (DC-shaped) site ...
        self.assertEqual(fields["Epic Link"], "customfield_10100")
        # ... and Cloud's documented id when the field is missing entirely.
        self.assertEqual(fields["Sprint"], CLOUD_FIELD_IDS["Sprint"])

    def test_tempo_is_an_alternative_worklog_source(self):
        transport = _Recorder(lambda path, params: [{"worklogId": 7, "timeSpentSeconds": 60}])
        client = CloudJiraClient(transport, tempo=True)

        worklog = client.worklogs("A-1")[0]
        self.assertEqual(worklog["worklogId"], 7)
        # The reducer keys the event on ``id``; Tempo's ``worklogId`` maps onto it.
        self.assertEqual(worklog["id"], "7")
        self.assertEqual(transport.calls[0][0], "/rest/tempo-timesheets/4/worklogs/A-1")

    def test_check_and_metadata_use_v3(self):
        transport = _Recorder(lambda path, params: {
            "/rest/api/3/serverInfo": {"version": "1001.0.0"},
            "/rest/api/3/status": [{"id": "1", "statusCategory": {"key": "new"}}],
        }.get(path, []))
        client = CloudJiraClient(transport)

        self.assertEqual(client.check()["version"], "1001.0.0")
        self.assertEqual(client.status_categories(), {"1": "new"})
        self.assertEqual([call[0] for call in transport.calls],
                         ["/rest/api/3/serverInfo", "/rest/api/3/status"])

    def test_a_cursor_search_is_counted_as_search(self):
        self.assertEqual(request_kind("/rest/api/3/search/jql"), "search")
        self.assertEqual(request_kind("/rest/api/3/search"), "search")
        self.assertEqual(request_kind("/rest/api/3/issue/A-1/worklog"), "worklog")

    def test_the_cli_flag_implies_jira(self):
        args = build_parser().parse_args(["--jira-cloud", "--live"])
        self.assertTrue(args.jira_cloud)
        # main() promotes it, so every --jira flag keeps its meaning.

    @staticmethod
    def _cli(argv: list[str]) -> tuple[int, str]:
        """Run the CLI with no Jira credentials in the environment."""
        env = {
            "MEGASPEED_JIRA_CLOUD_BASE_URL": "",
            "MEGASPEED_JIRA_CLOUD_TOKEN": "",
            "MEGASPEED_JIRA_BASE_URL": "",
            "MEGASPEED_JIRA_TOKEN": "",
            "MEGASPEED_JIRA_USER": "",
            "MEGASPEED_JIRA_PASSWORD": "",
        }
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err):
            return scrape_main(argv), err.getvalue()

    def test_the_cli_refuses_cloud_mode_without_a_url(self):
        code, err = self._cli(["--jira-cloud", "--live", "--out", "/tmp/never.tgz"])
        self.assertEqual(code, 2)
        self.assertIn("MEGASPEED_JIRA_CLOUD_BASE_URL", err)

    def test_the_cli_refuses_cloud_fixture_mode(self):
        # There is no Cloud fixture: the local Cloud is the proxy, over HTTP.
        code, err = self._cli(["--jira-cloud", "--fixture", "--out", "/tmp/never.tgz"])
        self.assertEqual(code, 2)
        self.assertIn("needs --live", err)


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


class CloudLabEquivalenceTests(unittest.TestCase):
    """``--jira-cloud`` through the proxy emits what ``--jira`` emits."""

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

        # -- DC: the existing --jira source, straight against the lab --------
        cls.dc = scrape(
            cls.cfg,
            HttpTransport(BASE_URL, user=USER, password=PASSWORD),
            "jira-dc",
            out=cls.root / "dc.tgz",
            state_dir=cls.root / "dc.state",
        )
        cls.dc_bundle = read_bundle(cls.root / "dc.tgz")

        # -- Cloud: the emulation proxy in front of the same lab ------------
        cls.proxy = CloudJiraProxy(BASE_URL, pat=PAT, user=USER, password=PASSWORD)
        cls.discovered = cls.proxy.discover()
        cls.server = make_server(cls.proxy)
        host, port = cls.server.server_address[:2]
        cls.proxy_url = f"http://{host}:{port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        # The real CLI path, so the flag, the env vars and the client wiring are
        # all covered -- not just the library call.
        cls.env = {
            "MEGASPEED_JIRA_CLOUD_BASE_URL": cls.proxy_url,
            "MEGASPEED_JIRA_CLOUD_TOKEN": PAT,
            "MEGASPEED_JIRA_BASE_URL": "",
            "MEGASPEED_JIRA_TOKEN": "",
            "MEGASPEED_JIRA_USER": "",
            "MEGASPEED_JIRA_PASSWORD": "",
            "MEGASPEED_JIRA_PROJECTS": PROJECT,
            "MEGASPEED_TICKET_PROJECTS": PROJECT,
            "MEGASPEED_IDENTITY": str(cls.root / "identity.json"),
            "MEGASPEED_DB": str(cls.root / "store.duckdb"),
            "MEGASPEED_TEXT_CLASSIFY": "0",
            "MEGASPEED_PROGRESS_INTERVAL": "0",
        }
        cls.cloud_bundle_path = cls.root / "cloud.tgz"
        out = io.StringIO()
        with mock.patch.dict(os.environ, cls.env), contextlib.redirect_stdout(out):
            cls.code = scrape_main(
                [
                    "--jira-cloud", "--live", "--project", PROJECT,
                    "--out", str(cls.cloud_bundle_path),
                    "--state-dir", str(cls.root / "cloud.state"),
                    "--quiet", "--json",
                ]
            )
        cls.stdout = out.getvalue()
        cls.cloud = json.loads(cls.stdout) if cls.code == 0 else {}
        cls.cloud_bundle = read_bundle(cls.cloud_bundle_path) if cls.code == 0 else None

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    # -- the CLI wiring --------------------------------------------------
    def test_the_cloud_cli_run_succeeds(self):
        self.assertEqual(self.code, 0, self.stdout)
        self.assertEqual(self.cloud["source"], "jira-cloud")
        self.assertEqual(self.cloud["fields"], {
            "epic_link": "customfield_10014",
            "sprint": "customfield_10020",
            "story_points": "customfield_10016",
        })

    def test_the_proxy_discovered_the_labs_field_ids(self):
        self.assertEqual(self.discovered, {
            "Epic Link": "customfield_10100",
            "Sprint": "customfield_10104",
            "Story Points": "customfield_10106",
            "Epic Name": "customfield_10102",
        })

    # -- equivalence -----------------------------------------------------
    def test_the_same_counts(self):
        self.assertEqual(self.cloud["counts"], self.dc["counts"])
        self.assertEqual(self.cloud["counts"]["epics"], EXPECTED["epics"])
        self.assertEqual(self.cloud["counts"]["stories"], EXPECTED["stories"])
        self.assertEqual(self.cloud["counts"]["sprints"], EXPECTED["sprints"])
        self.assertEqual(self.cloud["worklogs"], EXPECTED["worklogs"])
        for key in ("transitions", "points_changes", "sprint_changes",
                    "assigned_changes", "changelog_items", "changelog_ignored"):
            self.assertEqual(self.cloud[key], self.dc[key], key)

    def test_the_same_objects(self):
        self.assertEqual(_objects(self.cloud_bundle), _objects(self.dc_bundle))

    def test_the_same_events_modulo_timestamps(self):
        self.assertEqual(_events(self.cloud_bundle), _events(self.dc_bundle))

    def test_the_same_relations(self):
        self.assertEqual(sorted(self.cloud_bundle.relations), sorted(self.dc_bundle.relations))

    def test_the_same_worklog_noise(self):
        self.assertEqual(self.cloud["log_delays"], self.dc["log_delays"])

    # -- the bundle is a real bundle -------------------------------------
    def test_the_cloud_bundle_imports_and_passes_the_contract(self):
        imported = import_bundle(self.cfg, self.cloud_bundle_path)
        self.assertTrue(imported["verified"], imported["problems"])
        con = connect(self.cfg.db_path, read_only=True)
        try:
            report = verify(con, self.cfg.key_shape, self.cfg.ticket_projects)
            self.assertTrue(report["ok"], report["problems"])
            self.assertEqual(
                con.execute("SELECT count(*) FROM objects WHERE object_type = 'STORY'").fetchone()[0],
                EXPECTED["stories"],
            )
            payload = json.dumps(
                con.execute("SELECT event_key, attributes FROM events").fetchall(), default=str
            ).lower()
            self.assertNotIn("ticket:", payload)
            self.assertNotIn("zzcanary", payload)
        finally:
            con.close()


def _events(bundle) -> list[tuple]:
    """Event identity modulo timestamps and the raw-payload digest.

    ``content_hash`` digests the *raw* payload, and the Cloud shape is a
    different payload by definition, so it is the one field that must differ.
    Everything the store sees -- key, activity, actor, attributes, timestamp --
    must be identical.
    """
    return sorted(
        (
            event["event_key"],
            event["activity"],
            event["actor"],
            json.dumps(event["attributes"], sort_keys=True),
            str(event["ts"]),
        )
        for event in bundle.events
    )


def _objects(bundle) -> list[str]:
    return sorted(json.dumps(obj, sort_keys=True, default=str) for obj in bundle.objects)


if __name__ == "__main__":
    unittest.main()

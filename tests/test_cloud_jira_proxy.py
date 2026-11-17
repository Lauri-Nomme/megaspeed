"""The Cloud Jira emulation proxy (prd/24), tested over HTTP.

No lab required: the proxy runs in front of a **stub DC server** that serves the
synthetic DC fixture through the real :class:`FixtureJiraTransport`. The fixture
is relabelled to custom-field ids no Cloud site uses (``customfield_70001`` …),
so the field mapping can only work if it is *discovered by name* -- a hardcoded
DC id would fail every assertion here.

Two things are proved:

* the proxy translates a Cloud request into a DC one and the response back
  (auth, base path, field ids, pagination, users, the Tempo stub), and
* a ``--jira-cloud`` scrape through the proxy emits exactly the objects, events
  and relations the DC ``--jira`` scrape emits from the same data.

    python3 -m unittest tests.test_cloud_jira_proxy -v
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ingest import jira_fixtures
from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.jira_cloud import CloudJiraClient
from ingest.scrape_jira import scrape
from ingest.transport import BitbucketError, FixtureJiraTransport, HttpTransport
from proxy.cloud_jira_proxy import CloudJiraProxy, make_server

PAT = "cloud-lab-pat"
#: The placeholder base URL inside the synthetic DC fixture.
FIXTURE_BASE = "http://jira.example.invalid"
#: The fixture's DC ids, relabelled to ids no Cloud installation would report,
#: so that only name-based discovery can map them.
DC_IDS = {
    "customfield_10100": "customfield_70001",  # Epic Link
    "customfield_10104": "customfield_70004",  # Sprint
    "customfield_10106": "customfield_70006",  # Story Points
}
CLOUD_IDS = {"epic_link": "customfield_10014", "sprint": "customfield_10020",
             "story_points": "customfield_10016"}


def _stub_bundle() -> dict:
    """The synthetic DC fixture with unusual custom-field ids."""
    text = json.dumps(jira_fixtures.generate())
    for old, new in DC_IDS.items():
        text = text.replace(old, new)
    return json.loads(text)


class _StubDC(BaseHTTPRequestHandler):
    """A minimal Jira DC HTTP server backed by the fixture transport."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # keep test output clean
        pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        parsed = urlsplit(self.path)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        self.server.calls.append((parsed.path, params))  # type: ignore[attr-defined]
        try:
            payload = self.server.transport.get(parsed.path, params)  # type: ignore[attr-defined]
        except BitbucketError as exc:
            self._json(404, {"errorMessages": [str(exc)]})
            return
        # A real DC reports its own base URL in every ``self``; the fixture uses
        # a placeholder domain, so stand in for the lab here.
        base = f"http://{self.headers.get('Host')}"
        self._json(200, json.loads(json.dumps(payload).replace(FIXTURE_BASE, base)))

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(server) -> str:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def _request(url: str, token: str | None = PAT, scheme: str = "Bearer") -> tuple[int, object]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"{scheme} {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode() or "null"
        finally:
            exc.close()
        return exc.code, json.loads(body)


class ProxyTranslationTests(unittest.TestCase):
    """One proxy, one stub DC, every route the ingest uses."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = _stub_bundle()
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), _StubDC)
        cls.stub.daemon_threads = True
        cls.stub.transport = FixtureJiraTransport(cls.bundle)
        cls.stub.calls = []
        cls.stub_url = _serve(cls.stub)

        cls.proxy = CloudJiraProxy(cls.stub_url, pat=PAT, user="admin", password="lab-pw")
        cls.discovered = cls.proxy.discover()
        cls.server = make_server(cls.proxy)
        cls.base = _serve(cls.server)

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.stub):
            server.shutdown()
            server.server_close()

    def _calls(self, suffix: str) -> list[tuple[str, dict]]:
        return [(path, params) for path, params in self.stub.calls if path.endswith(suffix)]

    # -- auth ------------------------------------------------------------
    def test_a_bearer_pat_is_required_and_basic_is_rejected(self):
        self.assertEqual(_request(f"{self.base}/rest/api/3/serverInfo", token=None)[0], 401)
        self.assertEqual(
            _request(f"{self.base}/rest/api/3/serverInfo", token="wrong")[0], 401
        )
        # Cloud never accepts Basic on a PAT endpoint; neither does the proxy.
        self.assertEqual(
            _request(f"{self.base}/rest/api/3/serverInfo", token="admin:lab-pw", scheme="Basic")[0],
            401,
        )
        status, payload = _request(f"{self.base}/rest/api/3/serverInfo")
        self.assertEqual(status, 200)
        self.assertEqual(payload["deploymentType"], "Cloud")

    def test_the_401_body_is_cloud_shaped(self):
        status, payload = _request(f"{self.base}/rest/api/3/search", token=None)
        self.assertEqual(status, 401)
        self.assertIn("errorMessages", payload)

    # -- field mapping ---------------------------------------------------
    def test_field_ids_are_discovered_by_name_not_hardcoded(self):
        self.assertEqual(
            self.discovered,
            {
                "Epic Link": "customfield_70001",
                "Sprint": "customfield_70004",
                "Story Points": "customfield_70006",
            },
        )
        self.assertEqual(
            self.proxy.fields.cloud_to_dc,
            {
                "customfield_10014": "customfield_70001",
                "customfield_10020": "customfield_70004",
                "customfield_10016": "customfield_70006",
            },
        )
        # Both directions are used: requests map Cloud -> DC, responses DC -> Cloud.
        self.assertEqual(self.proxy.fields.to_dc("customfield_10016"), "customfield_70006")
        self.assertEqual(self.proxy.fields.to_cloud("customfield_70006"), "customfield_10016")
        self.assertEqual(self.proxy.fields.to_dc("summary"), "summary")
        self.assertEqual(
            self.proxy.fields.as_dict()["dc_by_name"]["Story Points"], "customfield_70006"
        )

    def test_the_field_list_reports_cloud_ids_only(self):
        status, fields = _request(f"{self.base}/rest/api/3/field")
        self.assertEqual(status, 200)
        by_name = {entry["name"]: entry["id"] for entry in fields}
        self.assertEqual(by_name["Epic Link"], "customfield_10014")
        self.assertEqual(by_name["Sprint"], "customfield_10020")
        self.assertEqual(by_name["Story Points"], "customfield_10016")
        # A DC-only custom field (the lab's "Team") has no Cloud counterpart here.
        self.assertNotIn("Team", by_name)
        self.assertNotIn("customfield_70001", json.dumps(fields))

    # -- path translation ------------------------------------------------
    def test_cloud_v3_paths_become_dc_v2_paths(self):
        _request(f"{self.base}/rest/api/3/search?jql=project%3DPROJ")
        _request(f"{self.base}/rest/api/3/issue/PROJ-101")
        paths = [path for path, _ in self.stub.calls]
        self.assertIn("/rest/api/2/search", paths)
        self.assertIn("/rest/api/2/issue/PROJ-101", paths)
        self.assertFalse([path for path in paths if path.startswith("/rest/api/3/")])

    # -- issue -----------------------------------------------------------
    def test_an_issue_uses_cloud_field_ids_and_no_dc_ones(self):
        status, issue = _request(f"{self.base}/rest/api/3/issue/PROJ-101?expand=changelog")
        self.assertEqual(status, 200)
        fields = issue["fields"]
        self.assertEqual(fields["customfield_10014"], jira_fixtures.EPIC_KEY)
        self.assertIn("customfield_10016", fields)
        self.assertIsInstance(fields["customfield_10020"], list)
        self.assertIsInstance(fields["customfield_10020"][0]["id"], int)
        self.assertFalse([key for key in fields if key in DC_IDS])
        self.assertIn("changelog", issue)

    def test_the_sprint_field_is_a_cloud_sprint_object(self):
        _, issue = _request(f"{self.base}/rest/api/3/issue/PROJ-101")
        sprint = issue["fields"]["customfield_10020"][0]
        self.assertEqual(sprint["id"], 1)
        self.assertEqual(sprint["state"], "closed")
        self.assertEqual(sprint["boardId"], 1)
        self.assertNotIn("com.atlassian.greenhopper", json.dumps(sprint))

    def test_users_are_cloud_shaped(self):
        _, issue = _request(f"{self.base}/rest/api/3/issue/PROJ-101")
        assignee = issue["fields"]["assignee"]
        self.assertIn("accountId", assignee)
        self.assertNotIn("name", assignee)
        self.assertNotIn("emailAddress", assignee)
        self.assertEqual(assignee["accountType"], "atlassian")

    def test_changelog_items_carry_the_cloud_field_id(self):
        _, issue = _request(f"{self.base}/rest/api/3/issue/PROJ-101?expand=changelog")
        items = [item for history in issue["changelog"]["histories"] for item in history["items"]]
        points = [item for item in items if item.get("field") == "Story Points"]
        self.assertTrue(points)
        self.assertTrue(all(item["fieldId"] == "customfield_10016" for item in points))

    def test_self_urls_point_at_the_proxy(self):
        _, issue = _request(f"{self.base}/rest/api/3/issue/PROJ-101")
        self.assertTrue(issue["self"].startswith(self.base + "/rest/api/3/"))
        self.assertNotIn(self.stub_url, issue["self"])

    # -- pagination ------------------------------------------------------
    def test_legacy_search_translates_startat_and_maxresults(self):
        jql = 'project = "PROJ" ORDER BY updated ASC'
        status, first = _request(
            f"{self.base}/rest/api/3/search?jql={urllib.parse.quote(jql)}"
            "&fields=key,updated&startAt=0&maxResults=5"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(first["issues"]), 5)
        self.assertEqual(first["total"], len(self.bundle["issues"]))
        _, second = _request(
            f"{self.base}/rest/api/3/search?jql={urllib.parse.quote(jql)}"
            "&fields=key,updated&startAt=10&maxResults=5"
        )
        self.assertEqual(len(second["issues"]), len(self.bundle["issues"]) - 10)
        sent = self._calls("/search")[-1][1]
        self.assertEqual(sent["startAt"], "10")
        self.assertEqual(sent["maxResults"], "5")

    def test_cursor_search_pages_with_nextpagetoken(self):
        jql = urllib.parse.quote('project = "PROJ" ORDER BY updated ASC')
        url = f"{self.base}/rest/api/3/search/jql?jql={jql}&fields=key,updated&maxResults=5"
        keys: list[str] = []
        pages = 0
        while True:
            status, page = _request(url)
            self.assertEqual(status, 200)
            pages += 1
            keys += [issue["key"] for issue in page["issues"]]
            if page.get("isLast"):
                self.assertNotIn("nextPageToken", page)
                break
            token = page["nextPageToken"]
            url = (
                f"{self.base}/rest/api/3/search/jql?jql={jql}&fields=key,updated"
                f"&maxResults=5&nextPageToken={token}"
            )
        self.assertEqual(len(keys), len(self.bundle["issues"]))
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(pages, 3)  # 13 issues at 5 per page

    def test_jql_custom_field_references_are_rewritten(self):
        jql = 'project = "PROJ" AND cf[10016] > 0'
        _request(f"{self.base}/rest/api/3/search?jql={urllib.parse.quote(jql)}")
        sent = self._calls("/search")[-1][1]
        self.assertIn("cf[70006]", sent["jql"])
        self.assertNotIn("cf[10016]", sent["jql"])

    def test_the_fields_parameter_is_rewritten_to_dc_ids(self):
        _request(f"{self.base}/rest/api/3/issue/PROJ-101?fields=customfield_10016,key")
        sent = self._calls("/issue/PROJ-101")[-1][1]
        self.assertEqual(sent["fields"], "customfield_70006,key")

    # -- worklog / agile / tempo ----------------------------------------
    def test_the_worklog_endpoint_is_translated(self):
        status, page = _request(f"{self.base}/rest/api/3/issue/PROJ-101/worklog")
        self.assertEqual(status, 200)
        self.assertTrue(page["worklogs"])
        worklog = page["worklogs"][0]
        self.assertIn("timeSpentSeconds", worklog)
        self.assertIn("accountId", worklog["author"])
        self.assertNotIn("emailAddress", worklog["author"])

    def test_board_and_sprint_paths_pass_through(self):
        status, boards = _request(f"{self.base}/rest/agile/1.0/board?projectKeyOrId=PROJ")
        self.assertEqual(status, 200)
        self.assertEqual(len(boards["values"]), 1)
        status, sprints = _request(f"{self.base}/rest/agile/1.0/board/1/sprint")
        self.assertEqual(status, 200)
        self.assertEqual([sprint["id"] for sprint in sprints["values"]], [1, 2, 3])

    def test_tempo_worklogs_map_onto_native_worklogs(self):
        status, rows = _request(f"{self.base}/rest/tempo-timesheets/4/worklogs/PROJ-101")
        self.assertEqual(status, 200)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["issue"]["key"], "PROJ-101")
        self.assertIsInstance(rows[0]["worklogId"], int)
        self.assertGreater(rows[0]["timeSpentSeconds"], 0)
        self.assertIn("accountId", rows[0]["author"])
        # Raw worklog text is never mapped through the stub.
        self.assertNotIn("comment", rows[0])

    def test_an_unknown_path_is_a_404(self):
        self.assertEqual(_request(f"{self.base}/rest/unknown/1.0/thing")[0], 404)


class CloudScrapeEquivalenceTests(unittest.TestCase):
    """``--jira-cloud`` through the proxy == ``--jira`` against the same data."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.bundle = _stub_bundle()
        cls.cfg = Config(
            db_path=cls.root / "store.duckdb",
            identity_path=cls.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[],
            ticket_projects=(jira_fixtures.PROJECT,),
            jira_projects=(jira_fixtures.PROJECT,),
            jira_min_interval=0.0,
        )

        # DC: the fixture transport, straight into the scrape.
        cls.dc = scrape(
            cls.cfg,
            FixtureJiraTransport(cls.bundle),
            "jira-fixture",
            out=cls.root / "dc.tgz",
            state_dir=cls.root / "dc.state",
        )
        cls.dc_bundle = read_bundle(cls.root / "dc.tgz")

        # Cloud: the same data behind the proxy, through the Cloud client.
        cls.stub = ThreadingHTTPServer(("127.0.0.1", 0), _StubDC)
        cls.stub.daemon_threads = True
        cls.stub.transport = FixtureJiraTransport(cls.bundle)
        cls.stub.calls = []
        cls.stub_url = _serve(cls.stub)
        cls.proxy = CloudJiraProxy(cls.stub_url, pat=PAT, user="admin", password="lab-pw")
        cls.proxy.discover()
        cls.server = make_server(cls.proxy)
        cls.base = _serve(cls.server)
        cls.transport = HttpTransport(cls.base, token=PAT)
        cls.client = CloudJiraClient(cls.transport, page_limit=5, list_page_limit=5, min_interval=0.0)
        cls.cloud = scrape(
            cls.cfg,
            cls.transport,
            "jira-cloud",
            out=cls.root / "cloud.tgz",
            state_dir=cls.root / "cloud.state",
            client=cls.client,
        )
        cls.cloud_bundle = read_bundle(cls.root / "cloud.tgz")

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.stub):
            server.shutdown()
            server.server_close()
        cls.tmp.cleanup()

    @staticmethod
    def _events(bundle) -> list[tuple]:
        """Event identity modulo timestamps and the raw-payload digest."""
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

    @staticmethod
    def _objects(bundle) -> list[str]:
        return sorted(json.dumps(obj, sort_keys=True, default=str) for obj in bundle.objects)

    def test_the_same_counts(self):
        for key in ("epics", "stories", "sprints", "worklogs", "events", "objects"):
            self.assertEqual(self.cloud["counts"][key], self.dc["counts"][key], key)
        self.assertEqual(self.cloud["worklogs"], self.dc["worklogs"])
        self.assertEqual(self.cloud["transitions"], self.dc["transitions"])
        self.assertEqual(self.cloud["sprint_changes"], self.dc["sprint_changes"])
        self.assertEqual(self.cloud["points_changes"], self.dc["points_changes"])

    def test_the_same_objects(self):
        self.assertEqual(self._objects(self.cloud_bundle), self._objects(self.dc_bundle))

    def test_the_same_events(self):
        self.assertEqual(self._events(self.cloud_bundle), self._events(self.dc_bundle))

    def test_the_same_relations(self):
        self.assertEqual(
            sorted(self.cloud_bundle.relations), sorted(self.dc_bundle.relations)
        )

    def test_the_field_ids_differ_by_shape_only(self):
        self.assertEqual(
            self.dc["fields"],
            {"epic_link": DC_IDS["customfield_10100"], "sprint": DC_IDS["customfield_10104"],
             "story_points": DC_IDS["customfield_10106"]},
        )
        self.assertEqual(self.cloud["fields"], CLOUD_IDS)

    def test_the_cloud_walk_used_cursor_pagination(self):
        # 13 issues at 5 per page: three search requests, all on the v3 cursor path.
        self.assertGreater(self.client.requests_by_kind["search"], 1)
        self.assertTrue(
            all(path == "/rest/api/2/search" for path, _ in self.stub.calls if "jql" in _)
        )

    def test_no_raw_text_reaches_the_cloud_bundle(self):
        payload = json.dumps(
            [event["attributes"] for event in self.cloud_bundle.events], default=str
        ).lower()
        self.assertNotIn("zzcanary", payload)
        self.assertNotIn("ticket:", payload)


if __name__ == "__main__":
    unittest.main()

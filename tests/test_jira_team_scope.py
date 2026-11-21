"""Team-scoped Jira ingest (prd/66): ``MEGASPEED_JIRA_TEAMS`` -> exact keys.

"Team" is **not** a standard Jira field: it is the Atlassian Teams custom field
(schema ``com.atlassian.teams:rm-teams-custom-field-team``), whose id differs
per site and whose value is a team object. A team set is resolved at scrape
start with one search (``cf[10111] in ("Platform")``) and is **AND-ed** with the
key/epic/project criterion (teams within these epics, teams within these
projects), then the run is an ordinary explicit-key-set walk, so the manifest,
import and ``--verify`` scope the store exactly like an epic or ticket-key scope
(prd/18, prd/27, prd/66).

The companion command ``python3 -m ingest.jira_fields`` prints the site's field
catalogue and one ticket's values, which is how the field id and the exact team
names are discovered.

    python3 -m unittest tests.test_jira_team_scope -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from ingest import jira_fixtures
from ingest.bundle import read_bundle
from ingest.config import Config, load_config, parse_teams
from ingest.importer import import_bundle
from ingest.jira import JiraClient
from ingest.jira_fields import (
    _auth_method,
    auth_help,
    inspect,
    probe_auth,
    render,
)
from ingest.progress import ProgressReporter
from ingest.scrape_jira import (
    JiraCollected,
    base_scope,
    build_manifest,
    intersect_team_keys,
    jql_for_teams,
    resolve_team_ids,
    resolve_team_keys,
    scrape,
    team_field_info,
    team_field_ref,
)
from ingest.store import connect, imported_scope
from ingest.transport import FixtureJiraTransport

JIRA_SOURCE = "jira-fixture"
REGEX = r"\b[A-Z]+-\d+\b"
SHAPE = r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$"
TEAM_FIELD = "customfield_10111"
STORIES = tuple(f"{jira_fixtures.PROJECT}-{number}" for number in sorted(jira_fixtures.MEMBERSHIP))


@contextlib.contextmanager
def _env(**overrides: str):
    """A MEGASPEED_*-free environment plus exactly ``overrides``."""
    clean = {key: value for key, value in os.environ.items() if not key.startswith("MEGASPEED_")}
    clean.update(overrides)
    with mock.patch.dict(os.environ, clean, clear=True):
        yield


def _config(root: Path, **overrides) -> Config:
    values = dict(
        db_path=root / "store.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=REGEX,
        key_shape=SHAPE,
        repos=[],
        ticket_projects=("PROJ", "OPS", "ARCH"),
        jira_projects=("PROJ",),
    )
    values.update(overrides)
    return Config(**values)


def _team_fixture(platform: tuple[str, ...] | None = None) -> dict:
    """The seeded corpus with team values: even issues Platform, odd Payments."""
    bundle = json.loads(json.dumps(jira_fixtures.generate()))
    platform = platform or ()
    for index, issue in enumerate(bundle["issues"]):
        value = platform[index % len(platform)] if platform else (
            "Platform" if index % 2 == 0 else "Payments"
        )
        issue["fields"][TEAM_FIELD] = {"id": f"team-{value.lower()}", "name": value}
    return bundle


def _keys_in(bundle: dict, team: str) -> tuple[str, ...]:
    return tuple(
        issue["key"] for issue in bundle["issues"]
        if (issue["fields"].get(TEAM_FIELD) or {}).get("name") == team
    )


# --------------------------------------------------------------------------- #
# 1. configuration
# --------------------------------------------------------------------------- #
class TeamConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "no-config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, **env: str) -> Config:
        with _env(MEGASPEED_CONFIG=str(self.config_path), MEGASPEED_REPOS="DEL/huge", **env):
            return load_config()

    def test_names_are_trimmed_deduplicated_and_case_preserved(self):
        self.assertEqual(
            parse_teams(" Platform , Payments,Platform,,  "),
            ("Platform", "Payments"),
        )

    def test_the_env_set_reaches_the_config(self):
        cfg = self.load(MEGASPEED_JIRA_TEAMS="Platform,Payments")
        self.assertEqual(cfg.jira_teams, ("Platform", "Payments"))
        self.assertEqual(cfg.jira_team_field, "")

    def test_the_field_can_be_pinned(self):
        cfg = self.load(MEGASPEED_JIRA_TEAMS="Platform", MEGASPEED_JIRA_TEAM_FIELD=TEAM_FIELD)
        self.assertEqual(cfg.jira_team_field, TEAM_FIELD)

    def test_no_teams_is_the_default(self):
        cfg = self.load()
        self.assertEqual(cfg.jira_teams, ())
        self.assertEqual(cfg.jira_team_field, "")

    def test_the_config_file_can_carry_teams_and_field(self):
        path = self.root / "config.toml"
        path.write_text(
            '[jira]\nteams = ["Platform", "Payments"]\n'
            f'team_field = "{TEAM_FIELD}"\n'
        )
        with _env(MEGASPEED_CONFIG=str(path), MEGASPEED_REPOS="DEL/huge"):
            cfg = load_config()
        self.assertEqual(cfg.jira_teams, ("Platform", "Payments"))
        self.assertEqual(cfg.jira_team_field, TEAM_FIELD)

    def test_an_absurdly_long_value_is_an_operator_error(self):
        with self.assertRaises(SystemExit) as caught:
            parse_teams("x" * 300)
        self.assertIn("MEGASPEED_JIRA_TEAMS", str(caught.exception.code))

    def test_a_team_set_does_not_need_a_prefix_list(self):
        cfg = self.load(MEGASPEED_JIRA_TEAMS="Platform", MEGASPEED_TICKET_PROJECTS="")
        self.assertEqual(cfg.ticket_projects, ())

    def test_cloud_email_plus_api_token_is_a_credential(self):
        # The standard Cloud method; a Bearer PAT is not the only option.
        cfg = self.load(
            MEGASPEED_JIRA_CLOUD_BASE_URL="https://acme.atlassian.net",
            MEGASPEED_JIRA_CLOUD_USER="me@acme.test",
            MEGASPEED_JIRA_CLOUD_API_TOKEN="tok",
        )
        self.assertTrue(cfg.has_jira_cloud_credentials)
        self.assertEqual(cfg.jira_cloud_user, "me@acme.test")


class BaseScopeTests(unittest.TestCase):
    """The key/epic/project criterion; teams are separate and AND-ed in."""

    def choice(self, **values):
        return base_scope(Config(
            db_path=Path("/tmp/x.duckdb"),
            identity_path=Path("/tmp/x.json"),
            ticket_regex=REGEX,
            key_shape=SHAPE,
            repos=[],
            **values,
        ))

    def test_projects_is_the_fallback(self):
        self.assertEqual(self.choice(jira_projects=("DEL",)), ("projects", ()))

    def test_a_team_does_not_replace_the_project_criterion(self):
        # prd/66: teams are AND-ed; the project criterion still exists.
        self.assertEqual(
            self.choice(jira_projects=("DEL",), jira_teams=("Platform",)),
            ("projects", ()),
        )

    def test_an_epic_set_is_the_key_criterion(self):
        self.assertEqual(
            self.choice(jira_epics=("DEL-1",), jira_teams=("Platform",)),
            ("epics", ("DEL-1",)),
        )

    def test_ticket_keys_remain_the_key_criterion(self):
        self.assertEqual(
            self.choice(
                ticket_keys=("DEL-9",),
                jira_epics=("DEL-1",),
                jira_teams=("Platform",),
            ),
            ("keys", ("DEL-9",)),
        )


class IntersectionTests(unittest.TestCase):
    def test_the_and_keeps_the_base_order(self):
        self.assertEqual(
            intersect_team_keys(
                ("PROJ-1", "PROJ-2", "PROJ-3"),
                ("PROJ-3", "PROJ-1"),
                teams=("Platform",), criterion="epic",
            ),
            ("PROJ-1", "PROJ-3"),
        )

    def test_an_empty_intersection_is_an_operator_error(self):
        with self.assertRaises(SystemExit) as caught:
            intersect_team_keys(
                ("PROJ-1",), ("PROJ-2",), teams=("Platform",), criterion="epic"
            )
        message = str(caught.exception.code)
        self.assertIn("AND filter", message)
        self.assertIn("epic", message)


# --------------------------------------------------------------------------- #
# 2. the resolution query and the field
# --------------------------------------------------------------------------- #
class TeamFieldTests(unittest.TestCase):
    FIELDS = {
        "Epic Link": "customfield_10100",
        "Team": TEAM_FIELD,
        "Team Name": "customfield_10200",
    }

    def test_the_field_is_discovered_by_name(self):
        self.assertEqual(team_field_ref("", self.FIELDS), (TEAM_FIELD, "cf[10111]"))

    def test_a_pinned_id_becomes_a_cf_reference(self):
        self.assertEqual(team_field_ref(TEAM_FIELD, {}), (TEAM_FIELD, "cf[10111]"))

    def test_a_pinned_cf_reference_is_kept(self):
        self.assertEqual(team_field_ref("cf[10111]", {}), (TEAM_FIELD, "cf[10111]"))

    def test_a_bare_number_is_a_field_id(self):
        self.assertEqual(team_field_ref("10111", {}), (TEAM_FIELD, "cf[10111]"))

    def test_a_pinned_name_survives_a_hidden_field_list(self):
        # Some sites hide fields from /field; the name form still queries.
        self.assertEqual(team_field_ref("Team", {}), ("Team", '"Team"'))

    def test_discovery_is_case_insensitive(self):
        self.assertEqual(team_field_ref("team", self.FIELDS), (TEAM_FIELD, "cf[10111]"))


class CloudTeamFieldTests(unittest.TestCase):
    """The Cloud Teams field: ``"Team[Team]"`` + team ids, not ``cf[...]`` names."""

    TEAM_ID = "00000000-0000-4000-8000-000000000001"
    FIELDS = {"Team": "customfield_17000", "Test Team": "customfield_17001"}
    ENTRIES = [
        {"id": "customfield_17001", "name": "Test Team",
         "schema": {"type": "string",
                    "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"}},
        {"id": "customfield_17000", "name": "Team",
         "schema": {"type": "any",
                    "custom": "com.atlassian.jira.plugin.system.customfieldtypes:atlassian-team"}},
    ]

    def test_the_cloud_team_uses_the_documented_reference(self):
        info = team_field_info("Team", self.FIELDS, self.ENTRIES, cloud=True)
        self.assertTrue(info["cloud_team"])
        self.assertEqual(info["ref"], '"Team[Team]"')
        self.assertEqual(info["id"], "customfield_17000")

    def test_a_select_field_called_team_keeps_the_custom_field_path(self):
        info = team_field_info("Test Team", self.FIELDS, self.ENTRIES, cloud=True)
        self.assertFalse(info["cloud_team"])
        self.assertEqual(info["ref"], "cf[17001]")

    def test_the_same_field_on_dc_keeps_the_custom_field_path(self):
        info = team_field_info("Team", self.FIELDS, self.ENTRIES, cloud=False)
        self.assertFalse(info["cloud_team"])
        self.assertEqual(info["ref"], "cf[17000]")

    def test_the_jql_is_the_documented_form(self):
        jql = jql_for_teams('"Team[Team]"', (self.TEAM_ID,), cloud_team=True)
        self.assertIn(f'"Team[Team]" in ({self.TEAM_ID})', jql)
        self.assertNotIn('"Team[Team]" in ("', jql)   # ids are bare, as Jira writes them

    def test_a_name_is_resolved_to_the_team_id(self):
        class Client:
            def field_suggestions(self, field_name, value):
                assert field_name == "Team"
                return [{"value": self.team_id, "displayName": NAME}]

        client = Client()
        client.team_id = self.TEAM_ID
        ids = resolve_team_ids(client, "Team", (NAME,))
        self.assertEqual(ids, (self.TEAM_ID,))

    def test_an_id_needs_no_request(self):
        class Client:
            def field_suggestions(self, field_name, value):
                raise AssertionError("an id must not be resolved")

        self.assertEqual(
            resolve_team_ids(Client(), "Team", (self.TEAM_ID,)), (self.TEAM_ID,)
        )

    def test_an_unresolved_name_survives_for_the_search_to_try(self):
        class Client:
            def field_suggestions(self, field_name, value):
                return []

        self.assertEqual(
            resolve_team_ids(Client(), "Team", ("Some Team",)), ("Some Team",)
        )


NAME = "[ACME] Platform Team Alpha (team-example)"


class TeamJqlTests(unittest.TestCase):
    def test_the_query_is_restricted_to_imported_issue_types(self):
        from ingest.scrape_jira import ISSUE_TYPES

        jql = jql_for_teams("cf[10111]", ("Platform",))
        self.assertIn("issuetype in (" + ", ".join(ISSUE_TYPES) + ")", jql)
        self.assertIn('cf[10111] in ("Platform")', jql)

    def test_several_teams_are_one_in_clause(self):
        self.assertIn(
            'cf[10111] in ("Platform","Payments")',
            jql_for_teams("cf[10111]", ("Platform", "Payments")),
        )

    def test_a_quote_or_backslash_in_a_name_is_escaped(self):
        jql = jql_for_teams('cf[10111]', ('Core "A" \\ B',))
        self.assertIn('"Core \\"A\\" \\\\ B"', jql)

    def test_an_explicit_project_list_is_and_ed_in(self):
        # prd/66: teams + projects = the intersection, in one query.
        jql = jql_for_teams("cf[10111]", ("Platform",), projects=("DEL", "FIX"))
        self.assertIn('project in ("DEL","FIX")', jql)
        self.assertLess(jql.index("issuetype"), jql.index("project in"))
        self.assertLess(jql.index("project in"), jql.index("cf[10111] in"))

    def test_the_name_form_is_quoted(self):
        self.assertIn('"Team" in ("Platform")', jql_for_teams('"Team"', ("Platform",)))


# --------------------------------------------------------------------------- #
# 3. the resolver
# --------------------------------------------------------------------------- #
class _SearchClient:
    """A client whose one search answers a fixed key list."""

    requests = 1

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.jql = ""
        self.fields = ""

    def search(self, jql: str, fields: str = "key,updated"):
        self.jql, self.fields, rows = jql, fields, self.rows
        yield from rows


class ResolveTeamKeysTests(unittest.TestCase):
    def test_the_keys_are_deduplicated_in_search_order(self):
        client = _SearchClient([{"key": "PROJ-1"}, {"key": "PROJ-2"}, {"key": "PROJ-1"}])
        self.assertEqual(
            resolve_team_keys(client, ("Platform",), "cf[10111]"),
            ("PROJ-1", "PROJ-2"),
        )
        self.assertIn('cf[10111] in ("Platform")', client.jql)
        self.assertEqual(client.fields, "key")

    def test_an_empty_match_is_an_operator_error_with_the_inspector(self):
        # A typo must not become a green, empty scrape.
        with self.assertRaises(SystemExit) as caught:
            resolve_team_keys(_SearchClient([]), ("Platfrom",), "cf[10111]")
        message = str(caught.exception.code)
        self.assertIn("Platfrom", message)
        self.assertIn("ingest.jira_fields", message)


# --------------------------------------------------------------------------- #
# 4. the scrape: a team set is an explicit key set
# --------------------------------------------------------------------------- #
class _ScrapeCase(unittest.TestCase):
    fixture: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def scrape(self, name: str = "jira", fixture: dict | None = None, **overrides):
        cfg = _config(self.root, **overrides)
        out = self.root / f"{name}.tgz"
        report = scrape(
            cfg,
            FixtureJiraTransport(fixture if fixture is not None else self.fixture),
            JIRA_SOURCE,
            out=out,
            state_dir=self.root / f"{name}.state",
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
        )
        return cfg, report, out


class TeamScrapeTests(_ScrapeCase):
    def test_a_team_set_resolves_to_its_issues(self):
        fixture = _team_fixture()
        expected = _keys_in(fixture, "Platform")
        _cfg, report, out = self.scrape(
            fixture=fixture, jira_teams=("Platform",)
        )
        self.assertEqual(set(report["ticket_keys"]), set(expected))
        self.assertEqual(report["jira_teams"], ["Platform"])
        self.assertEqual(report["jira_team_field"], TEAM_FIELD)
        self.assertEqual(report["projects"], ["PROJ"])
        manifest = read_bundle(out).manifest
        self.assertEqual(set(manifest["ticket_keys"]), set(expected))
        self.assertEqual(manifest["jira_teams"], ["Platform"])
        self.assertEqual(manifest["jira_team_ids"], [])   # DC compares names
        self.assertEqual(manifest["jira_team_field"], TEAM_FIELD)

    def test_a_team_and_the_same_explicit_keys_produce_the_same_store(self):
        fixture = _team_fixture()
        platform = _keys_in(fixture, "Platform")
        _c1, _r1, team_out = self.scrape("team", fixture=fixture, jira_teams=("Platform",))
        _c2, _r2, keys_out = self.scrape("keys", fixture=fixture, ticket_keys=platform)
        self.assertEqual(
            set(read_bundle(team_out).ticket_keys),
            set(read_bundle(keys_out).ticket_keys),
        )
        # Import both and compare the scope the store records.
        for name, path in (("team", team_out), ("keys", keys_out)):
            cfg = _config(self.root, db_path=self.root / f"{name}.duckdb",
                          identity_path=self.root / f"{name}-identity.json")
            import_bundle(cfg, path)
            con = connect(cfg.db_path, read_only=True)
            try:
                self.assertEqual(
                    imported_scope(con), tuple(sorted(platform)),
                    f"{name}: the store must carry the resolved scope",
                )
            finally:
                con.close()

    def test_a_misspelt_team_is_an_operator_error_not_an_empty_run(self):
        fixture = _team_fixture()
        with self.assertRaises(SystemExit) as caught:
            self.scrape(fixture=fixture, jira_teams=("Platfrom",))
        self.assertIn("ingest.jira_fields", str(caught.exception.code))

    def test_a_pinned_field_id_is_used_and_recorded(self):
        fixture = _team_fixture()
        _cfg, report, _out = self.scrape(
            fixture=fixture, jira_teams=("Payments",), jira_team_field=TEAM_FIELD
        )
        self.assertEqual(report["jira_team_field"], TEAM_FIELD)
        self.assertEqual(set(report["ticket_keys"]), set(_keys_in(fixture, "Payments")))

    def test_teams_and_explicit_keys_import_only_the_intersection(self):
        # prd/66: teams are an AND criterion, not a replacement for the keys.
        fixture = _team_fixture()
        platform = _keys_in(fixture, "Platform")
        asked = tuple(_keys_in(fixture, "Payments")) + tuple(platform[:2])
        _cfg, report, out = self.scrape(
            fixture=fixture, ticket_keys=asked, jira_teams=("Platform",)
        )
        self.assertEqual(set(report["ticket_keys"]), set(asked) & set(platform))
        self.assertEqual(
            set(report["ticket_keys"]), set(read_bundle(out).ticket_keys)
        )

    def test_teams_and_epics_import_only_the_intersection(self):
        fixture = _team_fixture()
        _cfg, report, _out = self.scrape(
            fixture=fixture,
            jira_epics=(jira_fixtures.EPIC_KEY,),
            jira_teams=("Payments",),
        )
        expected = set(_keys_in(fixture, "Payments"))
        self.assertEqual(set(report["ticket_keys"]), expected)
        self.assertNotIn(jira_fixtures.EPIC_KEY, expected)   # the epic is Platform

    def test_teams_and_projects_are_one_and_query(self):
        fixture = _team_fixture()
        _cfg, report, _out = self.scrape(
            fixture=fixture, jira_projects=("PROJ",), jira_teams=("Platform",)
        )
        self.assertEqual(set(report["ticket_keys"]), set(_keys_in(fixture, "Platform")))
        # The same team in a project it does not work in is an operator error,
        # not an empty green scrape.
        with self.assertRaises(SystemExit) as caught:
            self.scrape(
                "ops", fixture=fixture, jira_projects=("OPS",),
                jira_teams=("Platform",),
            )
        self.assertIn("in OPS", str(caught.exception.code))


# --------------------------------------------------------------------------- #
# 5. provenance in the manifest
# --------------------------------------------------------------------------- #
class ManifestProvenanceTests(unittest.TestCase):
    def test_the_team_request_rides_the_manifest(self):
        collected = JiraCollected(
            source="jira-dc",
            as_of=datetime(2026, 11, 22, 12, 0),
            run_id="run-1",
            ticket_keys=("DEL-1", "DEL-2"),
            team_keys=("Platform",),
            team_ids=("00000000-0000-4000-8000-000000000001",),
            team_field=TEAM_FIELD,
            ticket_projects=("DEL",),
            ticket_regex=REGEX,
        )
        manifest = build_manifest(
            collected, previous=None, watermark=None, overlap_hours=0, full=True
        )
        self.assertEqual(manifest["jira_teams"], ["Platform"])
        self.assertEqual(
            manifest["jira_team_ids"],
            ["00000000-0000-4000-8000-000000000001"],
        )
        self.assertEqual(manifest["jira_team_field"], TEAM_FIELD)
        self.assertEqual(manifest["ticket_keys"], ["DEL-1", "DEL-2"])


# --------------------------------------------------------------------------- #
# 6. the field inspector
# --------------------------------------------------------------------------- #
class _FieldClient:
    requests = 3

    def __init__(self, fields: list[dict], issue: dict | None = None):
        self._fields = fields
        self._issue = issue or {}

    def fields(self):
        return self._fields

    def check(self):
        return {"version": "9.17.5"}

    def _get(self, path: str, params: dict | None = None, kind: str | None = None):
        return {"displayName": "Lauri"}

    def issue(self, key: str, changelog: bool = True):
        return self._issue


class AuthProbeTests(unittest.TestCase):
    """Cloud auth: serverInfo can 403 while /myself works; both fail = diagnose."""

    class _Client:
        requests = 2

        def __init__(self, server_error=None, myself=None):
            self.server_error = server_error
            self.myself = myself

        def check(self):
            if self.server_error:
                raise RuntimeError(self.server_error)
            return {"version": "9.17.5"}

        def _get(self, path, params=None, kind=None):
            if self.myself is None:
                raise RuntimeError("403 Forbidden for " + path)
            return self.myself

    def _cfg(self, **values):
        return Config(
            db_path=Path("/tmp/x.duckdb"), identity_path=Path("/tmp/x.json"),
            ticket_regex=REGEX, key_shape=SHAPE, repos=[],
            jira_cloud_base_url="https://acme.atlassian.net", **values,
        )

    def test_a_serverinfo_block_falls_back_to_myself(self):
        client = self._Client("403 Forbidden for /rest/api/3/serverInfo",
                              {"displayName": "Lauri"})
        auth = probe_auth(client, cloud=True, cfg=self._cfg())
        self.assertTrue(auth["ok"])
        self.assertIsNone(auth["version"])
        self.assertIn("/myself", auth["via"])
        self.assertIn("Lauri", auth["warning"])

    def test_both_paths_failing_names_the_cloud_credential_options(self):
        client = self._Client("403 Forbidden for /rest/api/3/serverInfo")
        with self.assertRaises(SystemExit) as caught:
            probe_auth(client, cloud=True, cfg=self._cfg())
        message = str(caught.exception.code)
        self.assertIn("MEGASPEED_JIRA_CLOUD_USER", message)
        self.assertIn("MEGASPEED_JIRA_CLOUD_API_TOKEN", message)
        self.assertIn("MEGASPEED_JIRA_CLOUD_TOKEN", message)
        self.assertIn("auth sent: none", message)

    def test_the_method_is_named_but_the_secret_never_printed(self):
        cfg = self._cfg(jira_cloud_user="me@acme.test",
                        jira_cloud_api_token="super-secret-token")
        message = auth_help(cfg, cloud=True, errors=["403"])
        self.assertIn("Basic me@acme.test", message)
        self.assertNotIn("super-secret-token", message)
        self.assertEqual(
            _auth_method(cfg, True),
            "Basic me@acme.test (MEGASPEED_JIRA_CLOUD_API_TOKEN)",
        )

    def test_basic_cloud_credentials_are_recognised(self):
        cfg = self._cfg(jira_cloud_user="me@acme.test",
                        jira_cloud_api_token="tok")
        self.assertTrue(cfg.has_jira_cloud_credentials)
        self.assertFalse(self._cfg().has_jira_cloud_credentials)


class CloudFieldClient(_FieldClient):
    """A fake whose class name marks it Cloud (``inspect`` reads the scope)."""


class FieldInspectorTests(unittest.TestCase):
    FIELDS = [
        {"id": "summary", "name": "Summary", "schema": {"type": "string"}},
        {"id": "customfield_10100", "name": "Epic Link", "custom": True},
        {"id": TEAM_FIELD, "name": "Team", "custom": True,
         "schema": {"type": "any",
                    "custom": "com.atlassian.teams:rm-teams-custom-field-team"}},
    ]

    def test_the_team_field_is_identified_even_when_the_value_is_null(self):
        issue = {"key": "DEL-100", "fields": {
            "summary": "Add login form validation",
            "customfield_10100": "DEL-1",
            TEAM_FIELD: None,
        }}
        report = inspect(_FieldClient(self.FIELDS, issue), "DEL-100")
        candidates = report["team_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["id"], TEAM_FIELD)
        self.assertEqual(candidates[0]["schema"]["custom"],
                         "com.atlassian.teams:rm-teams-custom-field-team")
        self.assertTrue(candidates[0]["null"])
        self.assertEqual(report["hint"]["field"], TEAM_FIELD)
        text = render(report)
        self.assertIn("TEAM CANDIDATE(S)", text)
        self.assertIn(TEAM_FIELD, text)
        self.assertIn("MEGASPEED_JIRA_TEAM_FIELD=" + TEAM_FIELD, text)
        self.assertIn("value: — (null)", text)

    def test_a_team_value_is_rendered_with_its_name(self):
        issue = {"key": "DEL-100", "fields": {
            TEAM_FIELD: {"id": "team-platform", "name": "Platform"},
        }}
        report = inspect(_FieldClient(self.FIELDS, issue), "DEL-100")
        value = report["team_candidates"][0]["value"]
        self.assertEqual(value["name"], "Platform")
        self.assertIn("Platform", render(report))

    def test_the_catalogue_lists_every_field(self):
        report = inspect(_FieldClient(self.FIELDS), None, catalogue=True)
        self.assertEqual({row["name"] for row in report["fields"]},
                         {"Summary", "Epic Link", "Team"})
        self.assertIn("FIELDS (3 shown)", render(report))

    def test_a_site_without_a_team_field_says_so(self):
        fields = [{"id": "summary", "name": "Summary"}]
        report = inspect(_FieldClient(fields), None, catalogue=True)
        self.assertEqual(report["team_candidates"], [])
        self.assertIn("may not have", render(report))

    def test_the_auth_path_is_reported(self):
        report = inspect(
            _FieldClient(self.FIELDS), None, catalogue=True, version=None,
            auth={"ok": True, "via": "/rest/api/3/myself", "account": "Lauri",
                  "warning": (
                      "403 Forbidden for /rest/api/3/serverInfo — continuing via"
                      " /rest/api/3/myself as Lauri"
                  )},
        )
        text = render(report)
        self.assertIn("via /rest/api/3/myself as Lauri", text)
        self.assertIn("serverInfo", text)

    def test_the_populated_cloud_team_outranks_an_empty_test_team(self):
        team_id = "00000000-0000-4000-8000-000000000001"
        fields = [
            {"id": "customfield_17001", "name": "Test Team", "custom": True,
             "schema": {"type": "string",
                        "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"}},
            {"id": "customfield_17000", "name": "Team", "custom": True,
             "schema": {"type": "any",
                        "custom": "com.atlassian.jira.plugin.system.customfieldtypes:atlassian-team"}},
        ]
        issue = {"key": "PROJ-12345", "fields": {
            "customfield_17001": None,
            "customfield_17000": {"id": team_id, "name": NAME},
        }}
        report = inspect(CloudFieldClient(fields, issue), "PROJ-12345")
        best = report["team_candidates"][0]
        self.assertEqual(best["id"], "customfield_17000")
        self.assertEqual(best["value_name"], NAME)
        self.assertEqual(best["value_id"], team_id)
        hint = report["hint"]
        self.assertEqual(hint["field"], "customfield_17000")
        self.assertEqual(hint["team_id"], team_id)
        self.assertEqual(hint["field_name"], "Team")
        text = render(report)
        self.assertIn(f"team:  {NAME}", text)
        self.assertIn(f"id:    {team_id}", text)
        self.assertIn('"Team[Team]" in (', text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Epic scoping (prd/27): ``MEGASPEED_JIRA_EPICS`` -> epics + children.

An epic set is the middle ground between "the whole project" and "these exact
tickets": at scrape start each epic is resolved to its children with **one**
extra search (``"Epic Link" in (DEL-1, DEL-2)``) and the run is then scoped to
the union through the explicit key-set path (prd/18). The walk, the bundle
manifest and import/``--verify`` therefore cannot tell an epic-scoped run from
an explicit-key-set run -- which is exactly what the equivalence test asserts.

Precedence, most specific wins: ``MEGASPEED_TICKET_KEYS`` >
``MEGASPEED_JIRA_EPICS`` > ``MEGASPEED_JIRA_PROJECTS``.

The fixture corpus (``ingest/jira_fixtures.py``) is one epic ``PROJ-1`` with
twelve stories; a second epic (``PROJ-2``, three children, or no children at
all) is cloned in the tests that need one.

    python3 -m unittest tests.test_jira_epic_scope -v
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
from ingest.config import Config, load_config
from ingest.importer import import_bundle
from ingest.jira import JiraClient
from ingest.jira_cloud import CloudJiraClient
from ingest.progress import ProgressReporter
from ingest.scrape_jira import (
    jql_for,
    jql_for_epics,
    resolve_epic_keys,
    scrape,
)
from ingest.store import connect, ensure_schema, imported_scope, verify, write_run
from ingest.transport import FixtureJiraTransport

JIRA_SOURCE = "jira-fixture"
REGEX = r"\b[A-Z]+-\d+\b"
SHAPE = r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$"
EPIC = jira_fixtures.EPIC_KEY
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


def _two_epic_fixture() -> dict:
    """The seeded fixture plus a second epic, ``PROJ-2``, with three children."""
    bundle = json.loads(json.dumps(jira_fixtures.generate()))
    epic = next(issue for issue in bundle["issues"] if issue["key"] == EPIC)
    second = json.loads(json.dumps(epic))
    second["id"] = "10001"
    second["key"] = "PROJ-2"
    second["self"] = "http://jira.example.invalid/rest/api/2/issue/10001"
    second["fields"]["summary"] = "Second delivery epic zzcanary00000000"
    bundle["issues"].append(second)
    for issue in bundle["issues"]:
        if issue["key"] in ("PROJ-110", "PROJ-111", "PROJ-112"):
            issue["fields"]["customfield_10100"] = "PROJ-2"
    return bundle


def _childless_fixture() -> dict:
    """The seeded corpus with every child detached: the epic stands alone."""
    bundle = json.loads(json.dumps(jira_fixtures.generate()))
    for issue in bundle["issues"]:
        if issue["key"] != EPIC:
            issue["fields"]["customfield_10100"] = None
    return bundle


def _shape(bundle) -> dict:
    """What a bundle *means*: ids, labels and relations (not run timestamps)."""
    return {
        "objects": sorted(
            (
                obj["object_type"],
                obj["object_id"],
                json.dumps(obj.get("attributes") or {}, sort_keys=True),
                tuple(obj.get("ticket_keys") or ()),
            )
            for obj in bundle.objects
        ),
        "events": sorted((event["event_key"], event["activity"]) for event in bundle.events),
        "relations": sorted(bundle.relations),
        "ticket_keys": bundle.ticket_keys,
    }


class _ScrapeCase(unittest.TestCase):
    """A temp root and one helper that scrapes the fixture with a given scope."""

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
            # The resolution announces the scope it settled on; keep it out of
            # the test output (the CLI still prints it).
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
        )
        return cfg, report, out


# --------------------------------------------------------------------------- #
# 1. config plumbing
# --------------------------------------------------------------------------- #
class EpicConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # A path that does not exist -> no TOML is read, so only env decides.
        self.config_path = self.root / "no-config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, **env: str) -> Config:
        with _env(MEGASPEED_CONFIG=str(self.config_path), MEGASPEED_REPOS="DEL/huge", **env):
            return load_config()

    def test_the_env_set_is_normalised_and_deduplicated(self):
        cfg = self.load(MEGASPEED_JIRA_EPICS="del-1, DEL-2 ,del-1,,")
        self.assertEqual(cfg.jira_epics, ("DEL-1", "DEL-2"))

    def test_no_epics_is_the_default(self):
        self.assertEqual(self.load().jira_epics, ())

    def test_the_config_file_can_carry_the_set(self):
        path = self.root / "config.toml"
        path.write_text('[jira]\nepics = ["DEL-1", "DEL-2"]\n')
        with _env(MEGASPEED_CONFIG=str(path), MEGASPEED_REPOS="DEL/huge"):
            cfg = load_config()
        self.assertEqual(cfg.jira_epics, ("DEL-1", "DEL-2"))

    def test_a_malformed_epic_is_an_operator_error(self):
        # A silently dropped epic would scope the run to a *smaller* set than
        # the operator asked for -- the one failure mode that must not be quiet.
        with self.assertRaises(SystemExit) as caught:
            self.load(MEGASPEED_JIRA_EPICS="DEL-1,garbage")
        self.assertIn("MEGASPEED_JIRA_EPICS", str(caught.exception.code))

    def test_an_epic_set_does_not_need_a_prefix_list(self):
        cfg = self.load(MEGASPEED_JIRA_EPICS="DEL-1", MEGASPEED_TICKET_PROJECTS="")
        self.assertEqual(cfg.jira_epics, ("DEL-1",))
        self.assertEqual(cfg.ticket_projects, ())

    def test_a_ticket_set_still_wins_at_config_time(self):
        cfg = self.load(MEGASPEED_TICKET_KEYS="DEL-9", MEGASPEED_JIRA_EPICS="DEL-1")
        self.assertEqual(cfg.ticket_keys, ("DEL-9",))
        self.assertEqual(cfg.jira_epics, ("DEL-1",))


# --------------------------------------------------------------------------- #
# 2. the resolution query
# --------------------------------------------------------------------------- #
class EpicJqlTests(unittest.TestCase):
    def test_the_resolution_is_one_epic_link_search(self):
        self.assertEqual(jql_for_epics(("DEL-1", "DEL-2")), '"Epic Link" in (DEL-1,DEL-2)')

    def test_the_resolved_set_rides_the_existing_key_path(self):
        # prd/27: after resolution the run *is* an explicit-key-set run.
        jql = jql_for("DEL", keys=("DEL-1", "DEL-100", "DEL-101"))
        self.assertIn("key in (DEL-1,DEL-100,DEL-101)", jql)
        self.assertNotIn("project =", jql)

    def test_resolution_is_the_epic_plus_its_children(self):
        fixture = jira_fixtures.generate()
        client = JiraClient(FixtureJiraTransport(fixture))
        keys = resolve_epic_keys(client, (EPIC,))
        self.assertEqual(keys[0], EPIC)
        self.assertEqual(set(keys), {EPIC, *STORIES})
        self.assertEqual(len(keys), len(set(keys)))
        # One search, no per-issue request: the extra cost is exactly one page.
        self.assertEqual(client.requests_by_kind["search"], 1)

    def test_a_childless_epic_resolves_to_itself(self):
        fixture = json.loads(json.dumps(jira_fixtures.generate()))
        for issue in fixture["issues"]:
            issue["fields"]["customfield_10100"] = None
        client = JiraClient(FixtureJiraTransport(fixture))
        self.assertEqual(resolve_epic_keys(client, (EPIC,)), (EPIC,))

    def test_the_cloud_client_resolves_through_the_v3_search(self):
        # prd/24 + prd/27: the resolution goes through the *client*, so
        # ``--jira-cloud`` uses Cloud's cursor search without a branch.
        calls: list[tuple[str, dict]] = []

        def handler(path: str, params: dict) -> dict:
            calls.append((path, dict(params)))
            return {"issues": [{"key": "DEL-100"}, {"key": "DEL-101"}], "isLast": True}

        class _Transport:
            def get(self, path: str, params: dict | None = None) -> dict:
                return handler(path, params or {})

        client = CloudJiraClient(_Transport(), page_limit=10)
        self.assertEqual(
            resolve_epic_keys(client, ("DEL-1",)), ("DEL-1", "DEL-100", "DEL-101")
        )
        self.assertEqual(calls[0][0], "/rest/api/3/search/jql")
        self.assertEqual(calls[0][1]["jql"], '"Epic Link" in (DEL-1)')


# --------------------------------------------------------------------------- #
# 3. the scrape is scoped to the union
# --------------------------------------------------------------------------- #
class EpicScopedScrapeTests(_ScrapeCase):
    fixture = jira_fixtures.generate()

    def test_the_scope_is_the_epics_plus_their_children(self):
        _cfg, report, _out = self.scrape(jira_epics=(EPIC,))
        self.assertEqual(report["jira_epics"], [EPIC])
        self.assertEqual(set(report["ticket_keys"]), {EPIC, *STORIES})
        self.assertEqual(report["issues_seen"], 1 + len(STORIES))
        self.assertEqual(report["counts"]["epics"], 1)
        self.assertEqual(report["counts"]["stories"], len(STORIES))

    def test_epic_projects_are_what_gets_walked(self):
        # The epics name the project, so a stale MEGASPEED_JIRA_PROJECTS does not
        # decide where sprints come from (and an epic-scoped run needs no prefix
        # list at all).
        _cfg, report, _out = self.scrape(jira_epics=(EPIC,), jira_projects=("OPS",))
        self.assertEqual(report["projects"], ["PROJ"])
        self.assertEqual(report["counts"]["sprints"], 3)

    def test_the_resolved_set_rides_in_the_manifest(self):
        _cfg, _report, out = self.scrape(jira_epics=(EPIC,))
        bundle = read_bundle(out)
        self.assertEqual(bundle.manifest["jira_epics"], [EPIC])
        self.assertEqual(set(bundle.ticket_keys), {EPIC, *STORIES})

    def test_it_costs_exactly_one_extra_search(self):
        _cfg, epic_report, _out = self.scrape("epic", jira_epics=(EPIC,))
        _cfg, keys_report, _out = self.scrape(
            "keys", ticket_keys=tuple(resolve_epic_keys(
                JiraClient(FixtureJiraTransport(self.fixture)), (EPIC,)
            ))
        )
        self.assertEqual(
            epic_report["requests_by_kind"]["search"],
            keys_report["requests_by_kind"]["search"] + 1,
        )
        self.assertEqual(
            epic_report["requests_by_kind"]["issue"],
            keys_report["requests_by_kind"]["issue"],
        )

    def test_it_is_equivalent_to_the_explicit_key_set_path(self):
        _cfg, _report, epic_out = self.scrape("epic", jira_epics=(EPIC,))
        resolved = read_bundle(epic_out).ticket_keys
        _cfg, _report, keys_out = self.scrape("keys", ticket_keys=resolved)
        self.assertEqual(_shape(read_bundle(epic_out)), _shape(read_bundle(keys_out)))


class EpicPrecedenceTests(_ScrapeCase):
    fixture = _two_epic_fixture()

    def test_the_project_scope_reads_everything(self):
        # The baseline the epic scope is narrower than: both epics + all stories.
        _cfg, report, _out = self.scrape("project")
        self.assertEqual(report["issues_seen"], 2 + len(STORIES))

    def test_epics_take_precedence_over_projects(self):
        _cfg, report, _out = self.scrape(jira_epics=(EPIC,))
        self.assertEqual(report["issues_seen"], 1 + len(STORIES) - 3)
        self.assertNotIn("PROJ-2", report["ticket_keys"])
        self.assertNotIn("PROJ-110", report["ticket_keys"])

    def test_ticket_keys_take_precedence_over_epics(self):
        _cfg, report, _out = self.scrape(ticket_keys=("PROJ-101",), jira_epics=(EPIC,))
        self.assertEqual(report["jira_epics"], [])
        self.assertEqual(report["ticket_keys"], ["PROJ-101"])
        self.assertEqual(report["issues_seen"], 1)
        self.assertEqual(report["counts"]["epics"], 0)
        self.assertEqual(report["counts"]["stories"], 1)

    def test_two_epics_resolve_to_one_union(self):
        _cfg, report, _out = self.scrape(jira_epics=(EPIC, "PROJ-2"))
        self.assertEqual(report["jira_epics"], [EPIC, "PROJ-2"])
        self.assertEqual(report["issues_seen"], 2 + len(STORIES))
        self.assertEqual(
            set(report["ticket_keys"]), {EPIC, "PROJ-2", *STORIES}
        )

    def test_a_childless_epic_scopes_to_itself(self):
        _cfg, report, out = self.scrape(
            jira_epics=(EPIC,), fixture=_childless_fixture()
        )
        self.assertEqual(report["ticket_keys"], [EPIC])
        self.assertEqual(report["issues_seen"], 1)
        self.assertEqual(report["counts"]["epics"], 1)
        self.assertEqual(report["counts"]["stories"], 0)
        self.assertEqual(read_bundle(out).manifest["jira_epics"], [EPIC])


# --------------------------------------------------------------------------- #
# 4. import + --verify scope the same way
# --------------------------------------------------------------------------- #
class EpicImportTests(_ScrapeCase):
    fixture = jira_fixtures.generate()

    def test_the_import_enforces_the_resolved_set(self):
        cfg, report, out = self.scrape(jira_epics=(EPIC,))
        imported = import_bundle(cfg, out)
        self.assertTrue(imported["verified"], imported["problems"])
        self.assertEqual(set(imported["ticket_keys"]), set(report["ticket_keys"]))
        con = connect(cfg.db_path, read_only=True)
        try:
            self.assertEqual(set(imported_scope(con)), set(report["ticket_keys"]))
            ok = verify(con, cfg.key_shape, cfg.ticket_projects, imported_scope(con))
            self.assertTrue(ok["ok"], ok["problems"])
            # The same store rejected under a narrower set: --verify really does
            # enforce the scope, it does not merely read it.
            rejected = verify(con, cfg.key_shape, cfg.ticket_projects, (EPIC,))
            self.assertFalse(rejected["ok"])
        finally:
            con.close()

    def test_the_verify_cli_needs_only_the_epic_set(self):
        # The operator configured MEGASPEED_JIRA_EPICS, not MEGASPEED_TICKET_KEYS:
        # --verify takes the resolved set from the store's own runs.
        from ingest.__main__ import main

        cfg, _report, out = self.scrape(jira_epics=(EPIC,))
        import_bundle(cfg, out)
        base = {
            "MEGASPEED_CONFIG": str(self.root / "no-config.toml"),
            "MEGASPEED_DB": str(cfg.db_path),
            "MEGASPEED_JIRA_EPICS": EPIC,
            "MEGASPEED_TICKET_PROJECTS": "",
        }
        with _env(**base):
            out_buffer = io.StringIO()
            with contextlib.redirect_stdout(out_buffer):
                code = main(["--verify", "--json"])
        self.assertEqual(code, 0, out_buffer.getvalue())

        # An explicit env set still wins, and a wrong one still fails.
        with _env(**base, MEGASPEED_TICKET_KEYS="PROJ-999"):
            out_buffer = io.StringIO()
            with contextlib.redirect_stdout(out_buffer):
                code = main(["--verify", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out_buffer.getvalue())["ok"])


class ImportedScopeTests(unittest.TestCase):
    """``imported_scope`` -- what --verify reads when the env names no set."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.con = connect(self.root / "store.duckdb")
        ensure_schema(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def record(self, run_id: str, keys) -> None:
        write_run(
            self.con,
            {
                "run_id": run_id,
                "source": "jira-fixture",
                "as_of": datetime(2026, 11, 17, 9, 0, 0),
                "source_watermark": None,
                "classifier_version": "1",
                "ticket_keys": list(keys),
                "prs_seen": 0,
                "prs_kept": 0,
                "prs_dropped": 0,
                "events_written": 0,
                "events_skipped": 0,
            },
        )

    def test_no_runs_means_no_set(self):
        self.assertEqual(imported_scope(self.con), ())

    def test_key_scoped_runs_union(self):
        self.record("run-1", ["DEL-1", "DEL-100"])
        self.record("run-2", ["DEL-100", "DEL-101"])
        self.assertEqual(imported_scope(self.con), ("DEL-1", "DEL-100", "DEL-101"))

    def test_a_prefix_scoped_run_disables_the_set(self):
        self.record("run-1", ["DEL-1"])
        self.record("run-2", [])
        self.assertEqual(imported_scope(self.con), ())


if __name__ == "__main__":
    unittest.main()

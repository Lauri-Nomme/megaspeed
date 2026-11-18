"""Tempo worklogs on Jira Data Center (prd/30).

The lab cannot run Tempo (the Marketplace artifact is empty and the add-on needs
the org's Tempo licence), so these tests serve Tempo's *shape* over the fixture's
native worklog records:

* the DC client normalises ``worklogId`` -> ``id``, ``dateCreated`` ->
  ``created``, and drops Tempo's raw ``comment`` (prd/02);
* a fixture run through ``tempo=True`` reduces to the *same* ``worklogged``
  events and ``log_delay_bucket`` counts as the native-worklog run;
* ``--tempo`` / ``MEGASPEED_JIRA_TEMPO`` / ``[jira] tempo`` turn the path on.

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

from ingest import jira_fixtures
from ingest.bundle import read_bundle
from ingest.config import Config, load_config
from ingest.importer import import_bundle
from ingest.jira import TEMPO, JiraClient, request_kind, tempo_worklog
from ingest.scrape_cli import _jira_transport, build_parser, main as scrape_main
from ingest.scrape_jira import scrape
from ingest.store import connect, list_events, list_objects, verify
from ingest.transport import FixtureJiraTransport

SOURCE = "jira-fixture"


def _config(root: Path) -> Config:
    return Config(
        db_path=root / "store.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[],
        ticket_projects=("PROJ",),
        jira_projects=("PROJ",),
    )


class _Recorder:
    """A transport stub: records the path and returns a canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.payload(path, params) if callable(self.payload) else self.payload


class _RecordingTempoTransport(jira_fixtures.TempoFixtureTransport):
    """The Tempo fixture, with the requested paths kept for assertions."""

    def __init__(self, bundle: dict):
        super().__init__(bundle)
        self.calls: list[str] = []

    def get(self, path, params=None):
        self.calls.append(path)
        return super().get(path, params)


def _native_worklogs(bundle: dict, key: str) -> list[dict]:
    issue = next(i for i in bundle["issues"] if i["key"] == key)
    return list(((issue.get("fields") or {}).get("worklog") or {}).get("worklogs") or [])


# --------------------------------------------------------------------------- #
# 1. the DC client: Tempo shape -> native worklog shape
# --------------------------------------------------------------------------- #
class TempoClientTests(unittest.TestCase):
    def setUp(self):
        self.fixture = jira_fixtures.generate()
        self.transport = _RecordingTempoTransport(self.fixture)

    def test_tempo_worklogs_normalise_to_the_native_shape(self):
        client = JiraClient(self.transport, page_limit=50, min_interval=0, tempo=True)

        rows = client.worklogs("PROJ-101")

        native = _native_worklogs(self.fixture, "PROJ-101")
        self.assertTrue(rows)
        self.assertEqual(len(rows), len(native))
        for tempo_row, native_row in zip(rows, native):
            # ``worklogId`` (an int) becomes the reducer's ``id`` (a string).
            self.assertEqual(tempo_row["id"], str(native_row["id"]))
            self.assertEqual(tempo_row["timeSpentSeconds"], native_row["timeSpentSeconds"])
            self.assertEqual(tempo_row["started"], native_row["started"])
            # Tempo stamps creation ``dateCreated``; the reducer reads ``created``.
            self.assertEqual(tempo_row["created"], native_row["created"])
            self.assertEqual(tempo_row["author"], native_row["author"])
            # prd/02: raw worklog text never crosses into the ingest.
            self.assertNotIn("comment", tempo_row)
        self.assertEqual(self.transport.calls, [f"{TEMPO}/worklogs/PROJ-101"])
        self.assertEqual(client.requests_by_kind["worklog"], 1)

    def test_the_native_path_stays_the_default(self):
        transport = _RecordingTempoTransport(self.fixture)
        client = JiraClient(transport, page_limit=50, min_interval=0)

        rows = client.worklogs("PROJ-101")

        self.assertTrue(rows)
        self.assertIn("comment", rows[0])  # the native record is passed through
        self.assertEqual(transport.calls, ["/rest/api/2/issue/PROJ-101/worklog"])

    def test_a_wrapped_payload_is_accepted(self):
        row = {"worklogId": 7, "timeSpentSeconds": 60, "started": "2026-01-01T09:00:00.000+0000"}
        transport = _Recorder({"worklogs": [row]})
        client = JiraClient(transport, tempo=True)

        self.assertEqual(client.tempo_worklogs("A-1")[0]["id"], "7")
        self.assertEqual(transport.calls[0][0], f"{TEMPO}/worklogs/A-1")

    def test_tempo_rows_are_counted_as_worklog_requests(self):
        self.assertEqual(request_kind(f"{TEMPO}/worklogs/A-1"), "worklog")

    def test_normalisation_keeps_a_native_created_when_tempo_sends_both(self):
        row = tempo_worklog(
            {
                "worklogId": 9,
                "created": "2026-01-01T10:00:00.000+0000",
                "dateCreated": "2026-01-01T11:00:00.000+0000",
                "comment": "zzcanary",
            }
        )
        self.assertEqual(row["id"], "9")
        self.assertEqual(row["created"], "2026-01-01T10:00:00.000+0000")
        self.assertNotIn("comment", row)


class CloudTempoFieldMappingTests(unittest.TestCase):
    """A real Tempo *Cloud* payload uses ``createdAt`` + ``startDate``/``startTime``
    (not ``created``/``started``). The Cloud adapter must reconcile them, or every
    worklog collapses to ``created == started`` (all ``same_day``)."""

    def test_cloud_tempo_fields_map_to_the_native_shape(self):
        from ingest.jira_cloud import CloudJiraClient

        rows = [{
            "worklogId": 42,
            "timeSpentSeconds": 3600,
            "startDate": "2026-10-14",
            "startTime": "09:00:00",
            "createdAt": "2026-10-16T12:00:00.000+0000",
            "description": "zzcanary tempo comment",
            "author": {"accountId": "lauri", "displayName": "Lauri Nomme"},
        }]
        transport = _Recorder(rows)
        client = CloudJiraClient(transport, tempo=True)

        out = client.tempo_worklogs("DEL-100")

        self.assertEqual(out[0]["id"], "42")
        self.assertEqual(out[0]["created"], "2026-10-16T12:00:00.000+0000")
        self.assertEqual(out[0]["started"], "2026-10-14T09:00:00")
        # the raw comment/description text is not mapped into the reducer's shape.
        self.assertNotIn("comment", out[0])
        self.assertNotIn("description", out[0])


# --------------------------------------------------------------------------- #
# 2. the reducer: Tempo == native, event for event
# --------------------------------------------------------------------------- #
class TempoReducerEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.fixture = jira_fixtures.generate()
        cls.identity = cls.root / "identity.json"

        cls.native_report = cls._run(
            "native", FixtureJiraTransport(cls.fixture), client=None
        )
        cls.tempo_transport = _RecordingTempoTransport(cls.fixture)
        cls.tempo_client = JiraClient(
            cls.tempo_transport, page_limit=50, min_interval=0, tempo=True
        )
        cls.tempo_report = cls._run(
            "tempo", cls.tempo_transport, client=cls.tempo_client
        )
        cls.native_bundle = read_bundle(cls.root / "native.tgz")
        cls.tempo_bundle = read_bundle(cls.root / "tempo.tgz")

    @classmethod
    def _run(cls, name: str, transport, *, client) -> dict:
        root = cls.root / name
        return scrape(
            _config(root),
            transport,
            SOURCE,
            out=cls.root / f"{name}.tgz",
            state_dir=root / "state",
            identity_path=cls.identity,
            client=client,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def _worklogged(bundle) -> list[dict]:
        # ``as_of`` is the run's wall clock, not the record: everything else --
        # event key, timestamp, actor, objects, attributes, content hash -- must
        # match between the two sources.
        return sorted(
            (
                {key: value for key, value in event.items() if key != "as_of"}
                for event in bundle.events
                if event["activity"] == "worklogged"
            ),
            key=lambda event: event["event_key"],
        )

    def test_the_tempo_run_read_every_worklog_through_tempo(self):
        worklog_calls = [path for path in self.tempo_transport.calls if "/worklog" in path]
        self.assertTrue(worklog_calls)
        # Every worklog read is the Tempo route: with tempo on, the inline
        # native page is never the source.
        for path in worklog_calls:
            self.assertTrue(path.startswith(f"{TEMPO}/worklogs/"), path)

    def test_worklogged_events_are_identical(self):
        native = self._worklogged(self.native_bundle)
        tempo = self._worklogged(self.tempo_bundle)
        self.assertTrue(native)
        self.assertEqual(tempo, native)

    def test_log_delay_buckets_and_counts_match(self):
        self.assertEqual(self.tempo_report["log_delays"], self.native_report["log_delays"])
        self.assertEqual(self.tempo_report["worklogs"], self.native_report["worklogs"])
        self.assertEqual(
            self.tempo_report["counts"]["worklogs"], self.native_report["counts"]["worklogs"]
        )
        self.assertEqual(set(self.native_report["log_delays"]), {
            "same_day", "next_day", "within_week", "late",
        })

    def test_every_worklog_id_survives_the_rename(self):
        native_ids = {
            event["event_key"].split(":", 1)[1]
            for event in self.native_bundle.events
            if event["activity"] == "worklogged"
        }
        tempo_ids = {
            event["event_key"].split(":", 1)[1]
            for event in self.tempo_bundle.events
            if event["activity"] == "worklogged"
        }
        self.assertEqual(tempo_ids, native_ids)
        self.assertTrue(all(key.startswith("worklog:") for key in tempo_ids))

    def test_no_raw_tempo_comment_reaches_the_bundle_or_the_store(self):
        payload = json.dumps(
            {"events": self.tempo_bundle.events, "objects": self.tempo_bundle.objects},
            default=str,
        ).lower()
        self.assertNotIn("zzcanary", payload)
        self.assertNotIn("pairing on the parser", payload)

        root = self.root / "tempo"
        imported = import_bundle(_config(root), self.root / "tempo.tgz")
        self.assertTrue(imported["verified"], imported["problems"])
        con = connect(_config(root).db_path)
        try:
            stored = json.dumps(
                {"events": list_events(con), "objects": list_objects(con)}, default=str
            ).lower()
            self.assertNotIn("zzcanary", stored)
            self.assertNotIn("pairing on the parser", stored)
            self.assertTrue(verify(con, r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$", ("PROJ",))["ok"])
        finally:
            con.close()

    def test_a_config_driven_client_reads_tempo(self):
        """``jira_tempo`` on the Config reaches the client ``collect`` builds."""
        root = self.root / "config-driven"
        cfg = _config(root)
        cfg.jira_tempo = True
        transport = _RecordingTempoTransport(self.fixture)

        report = scrape(
            cfg, transport, SOURCE, out=root / "tempo.tgz", state_dir=root / "state"
        )

        worklog_calls = [path for path in transport.calls if "/worklog" in path]
        self.assertTrue(worklog_calls)
        for path in worklog_calls:
            self.assertTrue(path.startswith(f"{TEMPO}/worklogs/"), path)
        self.assertEqual(report["worklogs"], self.native_report["worklogs"])


# --------------------------------------------------------------------------- #
# 3. config + CLI
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _env(**overrides: str):
    clean = {
        key: value for key, value in os.environ.items() if not key.startswith("MEGASPEED_")
    }
    clean.update(overrides)
    with mock.patch.dict(os.environ, clean, clear=True):
        yield


class TempoConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.missing = self.root / "no-config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, **env: str) -> Config:
        with _env(MEGASPEED_CONFIG=str(self.missing), MEGASPEED_REPOS="DEL/huge", **env):
            return load_config()

    def test_tempo_is_off_by_default(self):
        self.assertFalse(self.load().jira_tempo)

    def test_the_env_flag_turns_it_on(self):
        self.assertTrue(self.load(MEGASPEED_JIRA_TEMPO="1").jira_tempo)
        self.assertTrue(self.load(MEGASPEED_JIRA_TEMPO="true").jira_tempo)

    def test_the_env_flag_turns_it_off(self):
        for value in ("0", "false", "no", "off", ""):
            self.assertFalse(self.load(MEGASPEED_JIRA_TEMPO=value).jira_tempo, value)

    def test_the_toml_flag_turns_it_on(self):
        path = self.root / "config.toml"
        path.write_text("[jira]\ntempo = true\n")
        with _env(MEGASPEED_CONFIG=str(path), MEGASPEED_REPOS="DEL/huge"):
            cfg = load_config()
        self.assertTrue(cfg.jira_tempo)

    def test_env_overrides_toml(self):
        path = self.root / "config.toml"
        path.write_text("[jira]\ntempo = true\n")
        with _env(MEGASPEED_CONFIG=str(path), MEGASPEED_REPOS="DEL/huge",
                  MEGASPEED_JIRA_TEMPO="0"):
            cfg = load_config()
        self.assertFalse(cfg.jira_tempo)


class TempoCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
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

    def test_the_flag_is_parsed(self):
        args = build_parser().parse_args(["--tempo", "--fixture"])
        self.assertTrue(args.tempo)
        # main() promotes it to --jira; the parser only records the flag.

    def test_the_flag_is_accepted_with_jira_cloud(self):
        args = build_parser().parse_args(["--tempo", "--jira-cloud", "--live"])
        self.assertTrue(args.tempo)
        self.assertTrue(args.jira_cloud)

    def test_the_live_dc_client_carries_the_flag(self):
        seen: dict = {}

        class _Stub:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)

            def check(self):
                return {"version": "9.17.5"}

        cfg = _config(self.root)
        cfg.jira_base_url = "http://jira.invalid"
        cfg.jira_auth_token = "t"
        args = build_parser().parse_args(["--jira", "--live", "--tempo"])
        with mock.patch("ingest.jira.JiraClient", _Stub), \
                mock.patch("ingest.scrape_cli.HttpTransport", lambda *a, **k: object()):
            _transport, source, _limit, _client = _jira_transport(cfg, args)
        self.assertEqual(source, "jira-dc")
        self.assertTrue(seen["tempo"])

    def test_the_live_cloud_client_carries_the_flag(self):
        seen: dict = {}

        class _Stub:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)

            def check(self):
                return {"version": "1001.0.0"}

        cfg = _config(self.root)
        cfg.jira_cloud_base_url = "http://cloud.invalid"
        cfg.jira_cloud_token = "t"
        args = build_parser().parse_args(["--jira-cloud", "--live", "--tempo"])
        with mock.patch("ingest.jira_cloud.CloudJiraClient", _Stub), \
                mock.patch("ingest.scrape_cli.HttpTransport", lambda *a, **k: object()):
            _transport, source, _limit, _client = _jira_transport(cfg, args)
        self.assertEqual(source, "jira-cloud")
        self.assertTrue(seen["tempo"])

    def scrape(self, name: str, *argv: str) -> dict:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scrape_main(
                [
                    "--jira", "--fixture", "--json",
                    "--out", str(self.root / f"{name}.tgz"),
                    "--state-dir", str(self.root / f"{name}.state"),
                    *argv,
                ]
            )
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue())

    def test_the_fixture_cli_runs_through_tempo(self):
        native = self.scrape("native", "--n", "8")
        tempo = self.scrape("tempo", "--n", "8", "--tempo")

        # Same records, same reduction: only the source of the worklogs changed.
        self.assertEqual(tempo["worklogs"], native["worklogs"])
        self.assertEqual(tempo["log_delays"], native["log_delays"])
        self.assertEqual(tempo["counts"]["worklogs"], native["counts"]["worklogs"])


if __name__ == "__main__":
    unittest.main()

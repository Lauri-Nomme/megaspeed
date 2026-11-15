"""Explicit ticket-set scoping (prd/18).

A prefix allowlist is wrong on a shared Jira project: ``DEL`` matches *everyone's*
tickets. ``MEGASPEED_TICKET_KEYS`` / ``MEGASPEED_TICKET_KEYS_FILE`` scope a run to
an explicit set of exact keys. When a set is configured it replaces the prefix
list for **both** filters:

* the scrape pre-filter on the cheap list row (title + branch + description,
  prd/14 §1), so only PRs for those keys cost any per-PR request, and
* the store-time filter, so nothing else can reach the store;
* ``--verify`` re-asserts the same set (a key outside it fails the contract).

Env and file are **unioned**; a malformed entry is a hard error (silently
dropping one would scope the run to a smaller set than the operator asked for).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from ingest.bundle import read_bundle
from ingest.config import (
    Config,
    load_config,
    load_ticket_keys_file,
    parse_ticket_keys,
)
from ingest.pipeline import KeyFilter
from ingest.scrape import scrape
from ingest.store import connect, verify
from ingest.transport import FixtureTransport

REGEX = r"\b[A-Z]+-\d+\b"
SHAPE = r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$"
T0 = 1_790_000_000_000
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}

_PR_PATH = re.compile(r"/pull-requests(?:/(\d+)(?:/(activities|commits))?)?$")


@contextlib.contextmanager
def _env(**overrides: str):
    """A MEGASPEED_*-free environment plus exactly ``overrides``."""
    clean = {key: value for key, value in os.environ.items() if not key.startswith("MEGASPEED_")}
    clean.update(overrides)
    with mock.patch.dict(os.environ, clean, clear=True):
        yield


def _record(pr_id: int, key: str | None) -> dict:
    """One fixture PR, shaped like a DC list row + its detail payloads."""
    title = f"{key}: thing {pr_id}" if key else f"plain thing {pr_id}"
    branch = f"feature/{key}-{pr_id}" if key else f"feature/plain-{pr_id}"
    return {
        "pull_request": {
            "id": pr_id,
            "version": 1,
            "title": title,
            "description": "zzcanary description",
            "state": "OPEN",
            "open": True,
            "closed": False,
            "createdDate": T0 + pr_id * 60_000,
            "updatedDate": T0 + pr_id * 60_000,
            "fromRef": {"displayId": branch, "latestCommit": "a" * 40},
            "toRef": {"displayId": "main", "latestCommit": "b" * 40},
            "author": {"user": ADA},
            "reviewers": [],
            "properties": {},
        },
        "activities": [
            {"id": pr_id * 100 + 1, "createdDate": T0 + pr_id * 60_000,
             "action": "OPENED", "user": ADA}
        ],
        "commits": [{
            "id": f"{pr_id:040d}", "displayId": f"{pr_id:040d}"[:7], "author": ADA,
            "authorTimestamp": T0 + pr_id * 60_000, "committer": ADA,
            "committerTimestamp": T0 + pr_id * 60_000, "message": "add the thing",
        }],
    }


def _bundle(*pairs: tuple[int, str | None]) -> dict:
    return {
        "schema": "megaspeed.fixture/1",
        "projects": [{
            "key": "DEL",
            "repos": [{
                "slug": "huge",
                "pull_requests": [_record(pr_id, key) for pr_id, key in pairs],
            }],
        }],
    }


def _kind(path: str) -> str:
    match = _PR_PATH.search(path)
    assert match, f"unexpected path {path}"
    if match.group(1) is None:
        return "list"
    return match.group(2) or "detail"


def _pr_id(path: str) -> int | None:
    match = _PR_PATH.search(path)
    return int(match.group(1)) if match and match.group(1) else None


class RecordingTransport:
    """Forwards to the fixture transport and remembers every request path."""

    def __init__(self, bundle: dict):
        self.inner = FixtureTransport(bundle)
        self.paths: list[str] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.paths.append(path)
        return self.inner.get(path, params)

    def kinds(self) -> Counter:
        return Counter(_kind(path) for path in self.paths)

    def touched_prs(self) -> set[int]:
        return {pid for path in self.paths if (pid := _pr_id(path)) is not None}


# --------------------------------------------------------------------------- #
# 1. parsing + config sources
# --------------------------------------------------------------------------- #
class TicketKeyParsingTests(unittest.TestCase):
    def test_keys_are_normalised_and_deduplicated(self):
        self.assertEqual(
            parse_ticket_keys("del-100, DEL-101 ,del-100,,"),
            ("DEL-100", "DEL-101"),
        )
        self.assertEqual(parse_ticket_keys(["DEL-1", " DEL-2 "]), ("DEL-1", "DEL-2"))
        self.assertEqual(parse_ticket_keys(""), ())

    def test_a_malformed_key_is_a_hard_error(self):
        for value in ("DEL-100,not-a-key", "DEL", "DEL-", "-100", "DEL-1-2"):
            with self.subTest(value=value), self.assertRaises(SystemExit) as caught:
                parse_ticket_keys(value)
            self.assertIn("bad ticket key", str(caught.exception))

    def test_a_file_holds_one_key_per_line_with_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.txt"
            path.write_text("# the team's tickets\nDEL-100\n\nDEL-101  # late addition\n")
            self.assertEqual(load_ticket_keys_file(path), ("DEL-100", "DEL-101"))

    def test_a_malformed_file_line_names_the_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys.txt"
            path.write_text("DEL-100\noops\n")
            with self.assertRaises(SystemExit) as caught:
                load_ticket_keys_file(path)
            self.assertIn("line 2", str(caught.exception))

    def test_a_missing_file_is_a_hard_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as caught:
                load_ticket_keys_file(Path(tmp) / "absent.txt")
            self.assertIn("cannot read", str(caught.exception))


class TicketKeyConfigTests(unittest.TestCase):
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

    def test_env_keys_are_the_scope(self):
        cfg = self.load(MEGASPEED_TICKET_KEYS="DEL-100,DEL-101")
        self.assertEqual(cfg.ticket_keys, ("DEL-100", "DEL-101"))

    def test_a_file_is_the_scope(self):
        path = self.root / "keys.txt"
        path.write_text("DEL-100\nDEL-101\n")
        cfg = self.load(MEGASPEED_TICKET_KEYS_FILE=str(path))
        self.assertEqual(cfg.ticket_keys, ("DEL-100", "DEL-101"))

    def test_env_and_file_are_unioned(self):
        path = self.root / "keys.txt"
        path.write_text("DEL-101\nDEL-102\n")
        cfg = self.load(
            MEGASPEED_TICKET_KEYS="DEL-100,DEL-101",
            MEGASPEED_TICKET_KEYS_FILE=str(path),
        )
        self.assertEqual(cfg.ticket_keys, ("DEL-100", "DEL-101", "DEL-102"))

    def test_a_malformed_entry_from_either_source_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.load(MEGASPEED_TICKET_KEYS="DEL-100,garbage")

        path = self.root / "keys.txt"
        path.write_text("garbage\n")
        with self.assertRaises(SystemExit):
            self.load(MEGASPEED_TICKET_KEYS_FILE=str(path))

    def test_a_key_set_does_not_need_a_prefix_list(self):
        cfg = self.load(MEGASPEED_TICKET_KEYS="DEL-100", MEGASPEED_TICKET_PROJECTS="")
        self.assertEqual(cfg.ticket_keys, ("DEL-100",))
        self.assertEqual(cfg.ticket_projects, ())
        self.assertEqual(
            KeyFilter.build(REGEX, SHAPE, cfg.ticket_projects, cfg.ticket_keys).keys("DEL-100"),
            ["DEL-100"],
        )

    def test_no_scope_at_all_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.load(MEGASPEED_TICKET_PROJECTS="", MEGASPEED_TICKET_KEYS="")


# --------------------------------------------------------------------------- #
# 2. matcher semantics
# --------------------------------------------------------------------------- #
class ExactTicketSetTests(unittest.TestCase):
    def test_only_listed_keys_match(self):
        key_filter = KeyFilter.build(REGEX, SHAPE, ("DEL",), ("DEL-100", "DEL-101"))
        self.assertTrue(key_filter.exact)
        self.assertEqual(
            key_filter.keys("DEL-100, DEL-101 and DEL-102 in the title"),
            ["DEL-100", "DEL-101"],
        )
        self.assertTrue(key_filter.accepts("DEL-100"))
        self.assertFalse(key_filter.accepts("DEL-102"))

    def test_the_set_takes_precedence_over_the_prefix_list(self):
        # DEL is a configured prefix, but DEL-999 is not in the explicit set.
        key_filter = KeyFilter.build(REGEX, SHAPE, ("DEL",), ("DEL-100",))
        self.assertEqual(key_filter.keys("DEL-999 fixes it"), [])
        self.assertEqual(key_filter.keys("DEL-100 fixes it"), ["DEL-100"])

    def test_without_a_set_the_prefix_list_still_applies(self):
        key_filter = KeyFilter.build(REGEX, SHAPE, ("DEL",))
        self.assertFalse(key_filter.exact)
        self.assertEqual(key_filter.keys("DEL-999 fixes it"), ["DEL-999"])
        self.assertEqual(key_filter.keys("QA-1 is another project"), [])

    def test_some_scope_is_required(self):
        with self.assertRaises(SystemExit):
            KeyFilter.build(REGEX, SHAPE, (), ())


# --------------------------------------------------------------------------- #
# 3. store-time filter (run -> bundle -> import -> verify)
# --------------------------------------------------------------------------- #
class StoreFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=self.root / "store.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=REGEX,
            key_shape=SHAPE,
            repos=[("DEL", "huge")],
            ticket_projects=("DEL",),
            ticket_keys=("DEL-12",),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_ingest(self) -> dict:
        from ingest.pipeline import run

        bundle = _bundle((1, "DEL-12"), (2, "DEL-99"))
        return run(
            self.cfg,
            FixtureTransport(bundle),
            "fixture",
            out=self.root / "export" / "del.tgz",
            state_dir=self.root / "export" / "del.state",
        )

    def test_only_the_listed_key_reaches_the_store(self):
        report = self.run_ingest()
        self.assertTrue(report["verified"], report["problems"])
        self.assertEqual(report["prs_seen"], 2)
        self.assertEqual(report["prs_kept"], 1)
        self.assertEqual(report["prs_dropped"], 1)
        self.assertEqual(report["ticket_keys"], ["DEL-12"])
        con = connect(self.cfg.db_path, read_only=True)
        try:
            tickets = {
                row[0]
                for row in con.execute(
                    "SELECT object_id FROM objects WHERE object_type = 'TICKET'"
                ).fetchall()
            }
        finally:
            con.close()
        self.assertEqual(tickets, {"TICKET:DEL-12"})

    def test_verify_accepts_the_set_and_rejects_a_key_outside_it(self):
        self.run_ingest()
        con = connect(self.cfg.db_path, read_only=True)
        try:
            accepted = verify(con, SHAPE, ("DEL",), ("DEL-12",))
            self.assertTrue(accepted["ok"], accepted["problems"])

            rejected = verify(con, SHAPE, ("DEL",), ("DEL-99",))
            self.assertFalse(rejected["ok"])
            self.assertTrue(
                any("outside the configured ticket keys" in p for p in rejected["problems"]),
                rejected["problems"],
            )
        finally:
            con.close()

    def test_the_verify_cli_reads_the_env_set(self):
        from ingest.__main__ import main

        self.run_ingest()
        base = {
            "MEGASPEED_CONFIG": str(self.root / "no-config.toml"),
            "MEGASPEED_REPOS": "DEL/huge",
            "MEGASPEED_DB": str(self.cfg.db_path),
        }
        with _env(**base, MEGASPEED_TICKET_KEYS="DEL-12"):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["--verify", "--json"])
        self.assertEqual(code, 0)

        with _env(**base, MEGASPEED_TICKET_KEYS="DEL-99"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["--verify", "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out.getvalue())["ok"])


# --------------------------------------------------------------------------- #
# 4. the scrape pre-filter uses the set (prd/14 §1 + prd/18)
# --------------------------------------------------------------------------- #
class ScrapePrefilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=self.root / "unused.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=REGEX,
            key_shape=SHAPE,
            repos=[("DEL", "huge")],
            ticket_projects=("DEL",),
            ticket_keys=("DEL-100", "DEL-101"),
        )
        self.out = self.root / "export" / "huge.tgz"

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_prs_for_the_set_are_fetched(self):
        # 1 and 2 are in the set; 3 has the DEL prefix but is not in the set;
        # 4 is another project entirely.
        transport = RecordingTransport(
            _bundle((1, "DEL-100"), (2, "DEL-101"), (3, "DEL-999"), (4, "QA-5"))
        )
        report = scrape(
            self.cfg,
            transport,
            "fixture",
            out=self.out,
            state_dir=self.root / "export" / "huge.state",
            overlap_hours=24 * 365,
        )

        self.assertTrue(os.path.exists(self.out))
        self.assertEqual(report["prs_seen"], 4)
        self.assertEqual(report["prs_kept"], 2)
        self.assertEqual(report["prs_dropped"], 2)
        # The list is cheap; only the two in-set PRs cost commits + activities.
        self.assertEqual(transport.kinds(), Counter({"list": 1, "commits": 2, "activities": 2}))
        self.assertEqual(transport.touched_prs(), {1, 2})

        bundle = read_bundle(self.out)
        self.assertEqual(bundle.ticket_keys, ("DEL-100", "DEL-101"))
        tickets = {o["object_id"] for o in bundle.objects if o["object_type"] == "TICKET"}
        self.assertEqual(tickets, {"TICKET:DEL-100", "TICKET:DEL-101"})
        self.assertEqual({event["pr_id"] for event in bundle.events}, {1, 2})

    def test_a_key_outside_the_set_is_never_fetched_even_from_the_description(self):
        bundle = _bundle((1, None), (2, None))
        records = bundle["projects"][0]["repos"][0]["pull_requests"]
        records[0]["pull_request"]["description"] = "Implements DEL-100 (in the set)"
        records[1]["pull_request"]["description"] = "Implements DEL-999 (not in the set)"

        transport = RecordingTransport(bundle)
        report = scrape(
            self.cfg,
            transport,
            "fixture",
            out=self.out,
            state_dir=self.root / "export" / "huge.state",
            overlap_hours=24 * 365,
        )
        self.assertEqual(report["prs_kept"], 1)
        self.assertEqual(transport.touched_prs(), {1})
        self.assertEqual(report["keys_by_placement"].get("description"), 1)


if __name__ == "__main__":
    unittest.main()

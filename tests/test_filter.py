"""Ticket-key filter: a configured project-prefix list, not a bare regex (BUG-04).

``UTF-8`` and ``SHA-256`` are perfectly key-shaped (``^[A-Z][A-Z0-9_]{0,9}-\\d+$``)
and a permissive ``[A-Z]+-\\d+`` regex happily matches them. The only thing that
tells them apart from a real key is the project prefix, so acceptance is now a
configured allowlist: ``MEGASPEED_TICKET_PROJECTS=DEL,FIX``.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ingest.config import DEFAULT_TICKET_PROJECTS, Config, parse_projects
from ingest.pipeline import KeyFilter, run
from ingest.store import connect, list_objects
from ingest.transport import FixtureTransport

SHAPE = r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$"
REGEX = r"\b[A-Z]+-\d+\b"
T0 = 1_790_000_000_000
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}


def _bundle(title: str, description: str, branch: str, message: str) -> dict:
    pr = {
        "id": 11,
        "state": "OPEN",
        "createdDate": T0,
        "updatedDate": T0,
        "fromRef": {"latestCommit": "a" * 40, "displayId": branch},
        "toRef": {"latestCommit": "b" * 40, "displayId": "main"},
        "author": {"user": ADA},
        "title": title,
        "description": description,
        "reviewers": [],
        "properties": {},
    }
    commit = {
        "id": "a" * 40,
        "author": ADA,
        "authorTimestamp": T0,
        "message": message,
    }
    return {
        "schema": "megaspeed.fixture/1",
        "projects": [
            {
                "key": "QA",
                "repos": [
                    {
                        "slug": "qa",
                        "pull_requests": [
                            {
                                "pull_request": pr,
                                "activities": [
                                    {"id": 1, "createdDate": T0, "action": "OPENED", "user": ADA}
                                ],
                                "commits": [commit],
                            }
                        ],
                    }
                ],
            }
        ],
    }


class TicketProjectFilterTests(unittest.TestCase):
    def test_technical_tokens_are_not_keys(self):
        key_filter = KeyFilter.build(REGEX, SHAPE, ("DEL", "FIX"))
        self.assertEqual(key_filter.keys("UTF-8 encoding", "SHA-256 digest", "AES-128", "ISO-8601"), [])
        self.assertEqual(key_filter.keys("feature/UTF-8-support"), [])

    def test_only_configured_projects_count(self):
        key_filter = KeyFilter.build(REGEX, SHAPE, ("DEL", "FIX"))
        self.assertEqual(key_filter.keys("DEL-12 fix the parser"), ["DEL-12"])
        self.assertEqual(key_filter.keys("FIX-7 and DEL-12"), ["DEL-12", "FIX-7"])
        self.assertEqual(key_filter.keys("PROJ-101 is not a configured project"), [])
        self.assertEqual(key_filter.keys("del-9 lowercase"), [])

    def test_prefix_list_is_required(self):
        with self.assertRaises(SystemExit):
            KeyFilter.build(REGEX, SHAPE, ())
        with self.assertRaises(SystemExit):
            parse_projects("")
        with self.assertRaises(SystemExit):
            parse_projects("not a project")
        self.assertEqual(parse_projects("del, FIX,del"), ("DEL", "FIX"))
        self.assertIn("PROJ", DEFAULT_TICKET_PROJECTS)

    def test_pr_with_only_a_technical_token_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(
                db_path=root / "store.duckdb",
                identity_path=root / "identity.json",
                ticket_regex=REGEX,
                key_shape=SHAPE,
                repos=[("QA", "qa")],
                ticket_projects=("DEL", "FIX"),
            )
            bundle = _bundle(
                title="UTF-8 encoding for the parser",
                description="zzcanary uses SHA-256 for digests",
                branch="feature/UTF-8-support",
                message="zzcanary support UTF-8",
            )
            report = run(cfg, FixtureTransport(bundle), source="fixture")
            self.assertEqual(report["prs_seen"], 1)
            self.assertEqual(report["prs_kept"], 0)
            self.assertEqual(report["prs_dropped"], 1)
            con = connect(cfg.db_path, read_only=True)
            try:
                self.assertEqual(con.execute("SELECT count(*) FROM events").fetchone()[0], 0)
                self.assertEqual(
                    con.execute("SELECT count(*) FROM objects WHERE object_type = 'TICKET'").fetchone()[0], 0
                )
                self.assertFalse(list_objects(con, "TICKET"))
            finally:
                con.close()

    def test_a_configured_prefix_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(
                db_path=root / "store.duckdb",
                identity_path=root / "identity.json",
                ticket_regex=REGEX,
                key_shape=SHAPE,
                repos=[("QA", "qa")],
                ticket_projects=("DEL", "FIX"),
            )
            bundle = _bundle(
                title="DEL-12 encoding for the parser",
                description="zzcanary description",
                branch="feature/DEL-12-support",
                message="DEL-12 use UTF-8",
            )
            report = run(cfg, FixtureTransport(bundle), source="fixture")
            self.assertEqual(report["prs_kept"], 1)
            con = connect(cfg.db_path, read_only=True)
            try:
                keys = [
                    row[0]
                    for row in con.execute(
                        "SELECT object_id FROM objects WHERE object_type = 'TICKET'"
                    ).fetchall()
                ]
                self.assertEqual(keys, ["TICKET:DEL-12"])
            finally:
                con.close()


class _ListWithoutDescription(FixtureTransport):
    """A source whose list projection omits ``description`` (FR-2)."""

    def get(self, path: str, params: dict | None = None) -> dict:
        payload = super().get(path, params)
        if path.endswith("/pull-requests"):
            return {
                **payload,
                "values": [
                    {key: value for key, value in row.items() if key != "description"}
                    for row in payload["values"]
                ],
            }
        return payload


class DescriptionOnlyMatchTests(unittest.TestCase):
    def _cfg(self, root: Path) -> Config:
        return Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=REGEX,
            key_shape=SHAPE,
            repos=[("QA", "qa")],
            ticket_projects=("DEL", "FIX"),
        )

    def test_a_key_only_in_the_description_survives_a_trimmed_list_row(self):
        """The pre-filter must fetch the detail record, not drop the PR (FR-2)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg(root)
            bundle = _bundle(
                title="no key in the title",
                description="This implements DEL-11 in the parser",
                branch="feature/no-key-here",
                message="no key in the commit either",
            )
            report = run(cfg, _ListWithoutDescription(bundle), source="fixture")

            self.assertEqual(report["prs_seen"], 1)
            self.assertEqual(report["prs_kept"], 1)
            self.assertEqual(report["prs_dropped"], 0)
            self.assertEqual(report["requests_by_kind"].get("detail"), 1)
            self.assertEqual(report["keys_by_placement"].get("description"), 1)
            con = connect(cfg.db_path, read_only=True)
            try:
                keys = [
                    row[0]
                    for row in con.execute(
                        "SELECT object_id FROM objects WHERE object_type = 'TICKET'"
                    ).fetchall()
                ]
                self.assertEqual(keys, ["TICKET:DEL-11"])
            finally:
                con.close()

    def test_a_trimmed_list_row_with_no_key_anywhere_is_still_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg(root)
            bundle = _bundle(
                title="no key",
                description="still no key, just prose",
                branch="feature/no-key",
                message="no key",
            )
            report = run(cfg, _ListWithoutDescription(bundle), source="fixture")
            self.assertEqual(report["prs_kept"], 0)
            self.assertEqual(report["prs_dropped"], 1)
            self.assertEqual(report["requests_by_kind"].get("detail"), 1)


if __name__ == "__main__":
    unittest.main()

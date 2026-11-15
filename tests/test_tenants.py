"""Tenants = parallel datasets (prd/12): the path rules and the ``?tenant=`` API.

A tenant **is** a store: ``<tenants-dir>/<name>.duckdb``. There is no registry
file to drift, so two things must hold:

* ``server/tenants.py`` derives the list from the directory — only regular
  ``*.duckdb`` files with a legal single-segment stem (never ``*.duckdb.wal``
  from an ingest run in flight, never a subdirectory, never a stray file);
* ``server/app.py`` answers every read endpoint from the store the request
  names, and an unknown name is a clean 404 that lists the real ones. A
  silent fallback would answer about the *wrong environment*, which is the one
  failure the feature exists to prevent — so it is pinned here.

The API half runs the real ASGI app against a temp tenant directory holding two
fixture stores with different data. No running server, no ``data/``, no
``httpx``: the app is driven directly through its ASGI interface, so routing,
query parsing and the error shapes are all exercised.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from ingest import fixtures
from ingest.config import Config
from ingest.pipeline import run
from ingest.store import connect, summary
from ingest.transport import FixtureTransport
from server import app as app_module
from server import tenants as tenantreg

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class Reply:
    """The parts of an ASGI response these tests assert on."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode())

    @property
    def text(self) -> str:
        return self.body.decode(errors="replace")


async def _call_asgi(app, path: str, query: str) -> Reply:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return Reply(start["status"], headers, body)


def get(path: str, **query: object) -> Reply:
    """One GET through the real app; ``query`` values become the query string."""
    qs = urlencode({key: value for key, value in query.items()})
    return asyncio.run(_call_asgi(app_module.app, path, qs))


def make_store(root: Path, name: str, n: int, seed: int) -> Path:
    """Ingest a fixture bundle into ``root/<name>.duckdb`` and return the store."""
    db = root / f"{name}.duckdb"
    cfg = Config(
        db_path=db,
        identity_path=root / f"identity-{name}.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[("ENG", "platform"), ("ENG", "mobile")],
    )
    report = run(cfg, FixtureTransport(fixtures.generate(n=n, seed=seed)), source="fixture")
    assert report["verified"], report["problems"]
    return db


def store_summary(path: Path) -> dict:
    con = connect(path, read_only=True)
    try:
        return summary(con)
    finally:
        con.close()


def event_keys(path: Path) -> set[str]:
    con = connect(path, read_only=True)
    try:
        return {row[0] for row in con.execute("SELECT event_key FROM events").fetchall()}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# path/name logic (server/tenants.py) — no DuckDB, no HTTP
# --------------------------------------------------------------------------- #
class TenantDiscoveryTests(unittest.TestCase):
    def test_stores_are_derived_from_the_directory_not_a_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # created out of order: discover must sort by name, not by mtime
            (root / "zeta.duckdb").write_bytes(b"")
            (root / "alpha.duckdb").write_bytes(b"")
            (root / "mid-1.duckdb").write_bytes(b"")
            (root / "mid.2.duckdb").write_bytes(b"")

            found = tenantreg.discover(root)

            self.assertEqual(list(found), ["alpha", "mid-1", "mid.2", "zeta"])
            self.assertEqual(found["alpha"], root / "alpha.duckdb")

    def test_a_wal_or_illegal_stem_is_never_a_tenant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "del.duckdb").write_bytes(b"")
            # an ingest run in flight: the store exists but is not readable yet
            (root / "del.duckdb.wal").write_bytes(b"")
            # a directory that merely ends in .duckdb is not a store
            (root / "stray.duckdb").mkdir()
            # illegal stems: not a single safe path segment
            (root / "bad name.duckdb").write_bytes(b"")
            (root / ".hidden.duckdb").write_bytes(b"")
            (root / "-lead.duckdb").write_bytes(b"")
            # not a store at all
            (root / "notes.txt").write_text("not a tenant")

            self.assertEqual(list(tenantreg.discover(root)), ["del"])

    def test_missing_or_unusable_directory_is_empty_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(tenantreg.discover(root / "nope"), {})
            afile = root / "afile"
            afile.write_text("x")
            self.assertEqual(tenantreg.discover(afile), {})


class TenantNameTests(unittest.TestCase):
    def test_safe_segments_are_accepted(self):
        for name in ("a", "del", "fix", "live-probe", "A1", "0day", "a_b.c", "a..b", "x" * 64):
            with self.subTest(name=name):
                self.assertTrue(tenantreg.valid_name(name))

    def test_traversal_and_illegal_characters_are_rejected(self):
        for name in (
            "", None, ".", "..", "...",
            ".hidden", "-lead", "_lead", "lead ", " lead",
            "../del", "del/../fix", "a/b", "dir/del", "a\\b",
            "bad name", "del\x00", "del\n", "dél", "del?", "del*",
            "x" * 65,
        ):
            with self.subTest(name=name):
                self.assertFalse(tenantreg.valid_name(name))


class TenantDefaultTests(unittest.TestCase):
    def setUp(self):
        self.paths = {
            "del": Path("/stores/del.duckdb"),
            "fix": Path("/stores/fix.duckdb"),
        }

    def test_preferred_wins_only_when_it_is_a_real_tenant(self):
        self.assertEqual(tenantreg.default_name(self.paths, "fix"), "fix")
        self.assertEqual(tenantreg.default_name(self.paths, "del"), "del")
        # a stale MEGASPEED_TENANT must not make every request a 404: the
        # default falls back to the first store by name instead
        self.assertEqual(tenantreg.default_name(self.paths, "ghost"), "del")
        self.assertEqual(tenantreg.default_name(self.paths, ".."), "del")
        self.assertEqual(tenantreg.default_name(self.paths, ""), "del")
        self.assertEqual(tenantreg.default_name(self.paths), "del")

    def test_no_stores_means_no_default(self):
        self.assertIsNone(tenantreg.default_name({}, "del"))
        self.assertIsNone(tenantreg.default_name({}))

    def test_env_maps_onto_the_preferred_name(self):
        # The env wiring lives in server.app's import-time constants; assert the
        # documented precedence in a fresh interpreter so the dev's own env and
        # an already-imported module cannot mask it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            db = root / "del.duckdb"

            tenant, directory = self._app_constants(
                {"MEGASPEED_TENANT": "fix", "MEGASPEED_DB": str(db)}
            )
            self.assertEqual(tenant, "fix", "MEGASPEED_TENANT wins")
            self.assertEqual(Path(directory), root, "tenants dir = the store's directory")

            tenant, _ = self._app_constants({"MEGASPEED_DB": str(db)})
            self.assertEqual(tenant, "del", "else the MEGASPEED_DB stem")

            tenant, _ = self._app_constants({})
            self.assertEqual(tenant, "", "no env is no preference (first by name)")

            tenant, directory = self._app_constants(
                {"MEGASPEED_TENANT": " fix ", "MEGASPEED_TENANTS_DIR": str(root / "tenants")}
            )
            self.assertEqual(tenant, "fix", "surrounding whitespace is stripped")
            self.assertEqual(Path(directory), (root / "tenants").resolve())

    @staticmethod
    def _app_constants(env: dict[str, str]) -> tuple[str, str]:
        clean = {key: value for key, value in os.environ.items() if not key.startswith("MEGASPEED_")}
        clean.update(env, MEGASPEED_QUIET="1")
        code = "import server.app as a; print(a.DEFAULT_TENANT); print(a.TENANTS_DIR)"
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            env=clean,
            capture_output=True,
            text=True,
            check=True,
        )
        tenant, directory = out.stdout.splitlines()
        return tenant, directory


# --------------------------------------------------------------------------- #
# the API, against a temp tenant directory
# --------------------------------------------------------------------------- #
class TenantApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        # two datasets with deliberately different sizes, so a mixed-up answer
        # is visible in the totals even when the row shapes match
        cls.del_store = make_store(cls.dir, "del", n=12, seed=101)
        cls.fix_store = make_store(cls.dir, "fix", n=5, seed=202)
        cls.del_summary = store_summary(cls.del_store)
        cls.fix_summary = store_summary(cls.fix_store)

        # things that must never be listed as tenants
        (cls.dir / "del.duckdb.wal").write_bytes(b"")
        (cls.dir / "bad name.duckdb").write_bytes(b"")
        (cls.dir / "stray.duckdb").mkdir()

        # TENANTS_DIR/DEFAULT_TENANT are import-time constants; point them at
        # the temp directory for this class and restore them afterwards.
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            # the corrupt-store test intentionally provokes a store error
            mock.patch.object(app_module, "LOG", False),
        ]
        for patch in cls.env:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.env):
            patch.stop()
        cls.tmp.cleanup()

    # -- the switcher list -------------------------------------------------- #
    def test_tenants_endpoint_describes_every_store(self):
        reply = get("/api/tenants")

        self.assertEqual(reply.status, 200)
        body = reply.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], 2)
        self.assertEqual([row["name"] for row in body["tenants"]], ["del", "fix"])
        self.assertEqual([row["name"] for row in body["tenants"] if row["default"]], ["del"])
        self.assertEqual(body["default"], "del")

        by_name = {row["name"]: row for row in body["tenants"]}
        self.assertNotIn("del.duckdb.wal", by_name)
        self.assertNotIn("bad name", by_name)
        self.assertNotIn("stray", by_name)
        for name, expected in (("del", self.del_summary), ("fix", self.fix_summary)):
            row = by_name[name]
            self.assertTrue(row["ok"], row)
            self.assertEqual(row["store"], str(self.dir / f"{name}.duckdb"))
            self.assertEqual(row["events"], expected["events"])
            self.assertEqual(row["prs"], expected["prs"])
            self.assertEqual(row["commits"], expected["commits"])
            self.assertEqual(row["source"], "fixture")
        # the two datasets really are different, or isolation proves nothing
        self.assertNotEqual(
            by_name["del"]["events"], by_name["fix"]["events"], "fixture tenants must differ"
        )

    # -- selecting a tenant ------------------------------------------------- #
    def test_tenant_param_selects_the_store(self):
        for name, store, expected in (
            ("del", self.del_store, self.del_summary),
            ("fix", self.fix_store, self.fix_summary),
        ):
            with self.subTest(tenant=name):
                reply = get("/api/events", tenant=name)
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.headers["x-tenant"], name)
                self.assertEqual(reply.headers["x-total-count"], str(expected["events"]))
                rows = reply.json()
                self.assertEqual(len(rows), expected["events"])
                self.assertTrue({row["key"] for row in rows} <= event_keys(store))

                health = get("/api/health", tenant=name)
                self.assertEqual(health.status, 200)
                self.assertEqual(health.json()["tenant"], name)
                self.assertEqual(health.json()["store"], str(store))
                self.assertEqual(health.json()["events"], expected["events"])

    def test_every_read_endpoint_honours_the_tenant(self):
        objects = get("/api/objects", tenant="fix", type="PR")
        self.assertEqual(objects.status, 200)
        self.assertEqual(objects.headers["x-tenant"], "fix")
        self.assertEqual(objects.headers["x-total-count"], str(self.fix_summary["prs"]))

        counts = get("/api/events/counts", tenant="fix")
        self.assertEqual(counts.status, 200)
        self.assertEqual(counts.json()["tenant"], "fix")
        self.assertEqual(counts.json()["total"], self.fix_summary["events"])
        self.assertEqual(
            sum(bucket["count"] for bucket in counts.json()["buckets"]),
            self.fix_summary["events"],
        )

        for path in ("/api/blocking", "/api/queues", "/api/delivery"):
            with self.subTest(path=path):
                self.assertEqual(get(path, tenant="fix").status, 200)

    def test_missing_or_empty_tenant_uses_the_default(self):
        for query in ({}, {"tenant": ""}):
            with self.subTest(query=query):
                reply = get("/api/events", limit=5, **query)
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.headers["x-tenant"], "del")
                self.assertEqual(reply.headers["x-total-count"], str(self.del_summary["events"]))

                health = get("/api/health", **query)
                self.assertEqual(health.status, 200)
                self.assertEqual(health.json()["tenant"], "del")
                self.assertEqual(health.json()["events"], self.del_summary["events"])

        listing = get("/api/tenants")
        self.assertEqual(listing.json()["default"], "del")

    def test_an_environment_default_wins_over_first_by_name(self):
        with mock.patch.object(app_module, "DEFAULT_TENANT", "fix"):
            listing = get("/api/tenants").json()
            self.assertEqual(listing["default"], "fix")
            self.assertEqual([row["name"] for row in listing["tenants"] if row["default"]], ["fix"])

            reply = get("/api/events")
            self.assertEqual(reply.headers["x-tenant"], "fix")
            self.assertEqual(reply.headers["x-total-count"], str(self.fix_summary["events"]))
            self.assertEqual(get("/api/health").json()["tenant"], "fix")

    # -- errors are clean and never a fallback ------------------------------ #
    def test_unknown_tenant_is_a_404_that_lists_the_real_ones(self):
        for path in ("/api/events", "/api/health", "/api/objects", "/api/blocking"):
            with self.subTest(path=path):
                reply = get(path, tenant="nope")
                self.assertEqual(reply.status, 404, reply.text)
                body = reply.json()
                self.assertFalse(body["ok"])
                self.assertIn("nope", body["error"])
                self.assertEqual(body["valid"], ["del", "fix"])

    def test_traversal_cannot_name_a_store(self):
        for name in ("..", "../del", "del/../fix", "../data/del", "/etc/passwd", ".hidden"):
            with self.subTest(tenant=name):
                reply = get("/api/events", tenant=name)
                self.assertEqual(reply.status, 404, reply.text)
                self.assertEqual(reply.json()["valid"], ["del", "fix"])

    def test_unknown_parameter_is_a_400(self):
        reply = get("/api/events", tenant="del", bogus="1")
        self.assertEqual(reply.status, 400)
        self.assertFalse(reply.json()["ok"])
        self.assertIn("bogus", reply.json()["error"])
        self.assertIn("tenant", reply.json()["valid"])

        reply = get("/api/health", depth="2")
        self.assertEqual(reply.status, 400)
        self.assertIn("depth", reply.json()["error"])

        # /api/tenants takes no parameters at all — including ?tenant=
        reply = get("/api/tenants", tenant="del")
        self.assertEqual(reply.status, 400)
        self.assertEqual(reply.json()["valid"], [])

    def test_a_corrupt_store_does_not_hide_the_healthy_ones(self):
        broken = self.dir / "broken.duckdb"
        broken.write_bytes(b"not a duckdb store")
        try:
            reply = get("/api/tenants")
            self.assertEqual(reply.status, 200)
            body = reply.json()
            by_name = {row["name"]: row for row in body["tenants"]}
            self.assertEqual(body["count"], 3)
            self.assertTrue(by_name["del"]["ok"])
            self.assertTrue(by_name["fix"]["ok"])
            self.assertFalse(by_name["broken"]["ok"])
            self.assertIn("error", by_name["broken"])
            # and it is still a real (if unhealthy) tenant for a targeted read
            self.assertEqual(get("/api/events", tenant="broken").status, 503)
        finally:
            broken.unlink()

    def test_a_new_store_appears_without_a_restart(self):
        late = make_store(self.dir, "zeta", n=3, seed=303)
        try:
            listing = get("/api/tenants").json()
            self.assertEqual([row["name"] for row in listing["tenants"]], ["del", "fix", "zeta"])
            self.assertEqual(listing["count"], 3)
            reply = get("/api/events", tenant="zeta")
            self.assertEqual(reply.status, 200)
            self.assertEqual(reply.headers["x-tenant"], "zeta")
            self.assertEqual(reply.headers["x-total-count"], str(store_summary(late)["events"]))
        finally:
            late.unlink()

    def test_no_stores_is_a_503_without_a_path_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp).resolve()
            with mock.patch.object(app_module, "TENANTS_DIR", empty):
                listing = get("/api/tenants")
                self.assertEqual(listing.status, 200)
                self.assertEqual(
                    {key: listing.json()[key] for key in ("ok", "default", "count", "tenants")},
                    {"ok": True, "default": None, "count": 0, "tenants": []},
                )

                reply = get("/api/health")
                self.assertEqual(reply.status, 503)
                self.assertFalse(reply.json()["ok"])
                self.assertNotIn(str(empty), reply.text, "a BUG-07 error body names no path")

                # naming one cannot silently read another dataset
                reply = get("/api/events", tenant="del")
                self.assertEqual(reply.status, 404)
                self.assertEqual(reply.json()["valid"], [])

    # -- isolation: no cache bleed between tenants -------------------------- #
    def test_interleaved_requests_never_read_another_tenant(self):
        del_keys = event_keys(self.del_store)
        fix_keys = event_keys(self.fix_store)
        expected = {"del": (self.del_store, del_keys), "fix": (self.fix_store, fix_keys)}

        for round_no in range(3):
            for name, (_store, keys) in expected.items():
                reply = get("/api/events", tenant=name, limit=10)
                self.assertEqual(reply.headers["x-tenant"], name, f"round {round_no}")
                self.assertEqual(reply.headers["x-total-count"], str(len(keys)))
                rows = reply.json()
                self.assertEqual(len(rows), min(10, len(keys)))
                self.assertTrue({row["key"] for row in rows} <= keys)

                counts = get("/api/events/counts", tenant=name).json()
                self.assertEqual(counts["tenant"], name)
                self.assertEqual(counts["total"], len(keys))

                health = get("/api/health", tenant=name).json()
                self.assertEqual(health["tenant"], name)
                self.assertEqual(health["events"], len(keys))

    def test_analysis_cache_is_keyed_by_tenant(self):
        """The ball-in-court build is cached per (tenant, store version)."""

        def fresh(name: str) -> dict:
            app_module._analysis_cache = None  # force a rebuild from that store
            reply = get("/api/blocking", tenant=name)
            self.assertEqual(reply.status, 200)
            return reply.json()

        expected = {name: fresh(name) for name in ("del", "fix")}

        for round_no in range(2):
            for name in ("del", "fix", "del", "fix"):
                reply = get("/api/blocking", tenant=name)
                self.assertEqual(reply.status, 200)
                self.assertEqual(reply.json(), expected[name], f"round {round_no} tenant {name}")
                self.assertEqual(app_module._analysis_cache[0][0], name)


if __name__ == "__main__":
    unittest.main()

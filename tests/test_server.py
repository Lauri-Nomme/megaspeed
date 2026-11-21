"""The round-1 API contract, pinned against the real ASGI app (G9).

Round-1 QA drove the server with a curl matrix and found real bugs in it
(BUG-07 store lock, BUG-08 eval/screenshot, BUG-09 date-only ``to``, BUG-13
silently ignored filters). The fixes landed, but the matrix was never promoted
to a test, so nothing stopped the contract from regressing. This file is that
matrix, run in-process against ``server.app`` — no running server, no ``data/``,
no ``httpx``: the app is driven through its ASGI interface so routing, query
parsing, headers and the error shapes are all exercised.

What is pinned:

* ``?tenant=`` answers from the named store and an unknown name is a 404 that
  lists the real ones (never a fallback — prd/12);
* every event filter is real: ``repo``, ``actor``, ``ticket``, ``activity``,
  ``pr_id``; an unknown parameter, an out-of-vocabulary activity or a bad
  ``order``/``limit`` is a 400 that lists the valid values (BUG-13);
* ``limit``/``offset``/``order`` page the log and ``X-Total-Count`` counts every
  matching row, not the page (GAP-04);
* a date-only ``to=`` includes that whole final day, and ``from == to`` (both
  date-only) is that day, not an empty range (BUG-09);
* ``POST /api/eval`` and ``POST /api/screenshot`` are a 401 without the harness
  token, and the token is what opens them (SEC-2);
* ``/api/events/counts`` returns a dense histogram whose totals agree with the
  log and the window totals (GAP-04).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from ingest import fixtures, taxonomy
from ingest.config import Config
from ingest.pipeline import run
from ingest.store import connect, summary, write_run
from ingest.transport import FixtureTransport
from server import app as app_module
from server.harness import BrowserBridge

TOKEN = "test-server-token"
TOKEN_HEADER = b"x-megaspeed-token"
# a 1x1 PNG: enough for the screenshot endpoint's base64 decode round-trip
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)


# --------------------------------------------------------------------------- #
# ASGI driver (no httpx in this venv: starlette's TestClient is unavailable)
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


async def _call_asgi(path: str, *, method: str = "GET", query: str = "",
                     body: bytes = b"", headers=None,
                     client=("127.0.0.1", 4321),
                     host=b"127.0.0.1:8766") -> Reply:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", host), *(headers or [])],
        "client": client,
        "server": (host.decode().split(":")[0], 8766),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app_module.app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Reply(
        start["status"],
        {k.decode().lower(): v.decode() for k, v in start["headers"]},
        payload,
    )


def call(path: str, **kwargs) -> Reply:
    return asyncio.run(_call_asgi(path, **kwargs))


def get(path: str, **query: object) -> Reply:
    """One GET through the real app; ``query`` values become the query string."""
    qs = urlencode({key: value for key, value in query.items()})
    return call(path, query=qs)


def post(path: str, payload: object = None, headers=None) -> Reply:
    body = json.dumps(payload).encode() if payload is not None else b""
    return call(
        path,
        method="POST",
        body=body,
        headers=[(b"content-type", b"application/json"), *(headers or [])],
    )


# --------------------------------------------------------------------------- #
# a temp tenant directory with two fixture stores
# --------------------------------------------------------------------------- #
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


class ServerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        # two datasets of different sizes: a mixed-up tenant answer is visible
        # in the totals even when the row shapes match
        cls.alpha = make_store(cls.dir, "alpha", n=12, seed=101)
        cls.beta = make_store(cls.dir, "beta", n=5, seed=202)
        cls.alpha_summary = summary(cls._connect(cls.alpha))
        cls.beta_summary = summary(cls._connect(cls.beta))
        cls.con = cls._connect(cls.alpha)

        cls.patches = [
            # TENANTS_DIR / DEFAULT_TENANT are import-time constants
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "bridge", BrowserBridge()),
            mock.patch.object(app_module, "LOG", False),
            mock.patch.dict(os.environ, {
                app_module.HARNESS_TOKEN_ENV: TOKEN,
                app_module.HARNESS_TOKEN_FILE_ENV: str(cls.dir / ".harness-token"),
            }),
        ]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.patches):
            patch.stop()
        cls.con.close()
        cls.tmp.cleanup()

    @staticmethod
    def _connect(path: Path):
        return connect(path, read_only=True)

    # -- small helpers ------------------------------------------------------ #
    def count(self, sql: str, params: list | None = None) -> int:
        return int(self.con.execute(sql, params or []).fetchone()[0])

    def events(self, **query) -> Reply:
        return get("/api/events", **query)

    # -- tenant scoping (prd/12) ------------------------------------------- #
    def test_tenant_param_selects_the_store_and_defaults_to_the_first(self):
        for name, expected in (("alpha", self.alpha_summary), ("beta", self.beta_summary)):
            with self.subTest(tenant=name):
                reply = self.events(tenant=name)
                self.assertEqual(reply.status, 200, reply.text)
                self.assertEqual(reply.headers["x-tenant"], name)
                self.assertEqual(reply.headers["x-total-count"], str(expected["events"]))
                self.assertEqual(len(reply.json()), expected["events"])

        # no ?tenant= (and an empty one) reads the default: first store by name
        for query in ({}, {"tenant": ""}):
            with self.subTest(query=query):
                reply = self.events(**query)
                self.assertEqual(reply.status, 200, reply.text)
                self.assertEqual(reply.headers["x-tenant"], "alpha")
                self.assertEqual(
                    reply.headers["x-total-count"], str(self.alpha_summary["events"])
                )
        # the two fixtures really differ, or scoping proves nothing
        self.assertNotEqual(
            self.alpha_summary["events"], self.beta_summary["events"]
        )

    def test_an_unknown_tenant_is_a_404_that_lists_the_real_ones(self):
        for name in ("nope", "..", "../alpha", "alpha/../beta", "/etc/passwd", ".hidden"):
            with self.subTest(tenant=name):
                reply = self.events(tenant=name)
                self.assertEqual(reply.status, 404, reply.text)
                body = reply.json()
                self.assertFalse(body["ok"])
                self.assertEqual(body["valid"], ["alpha", "beta"])
                self.assertIn(name, body["error"])

    # -- filters are real (BUG-13) ----------------------------------------- #
    def test_each_filter_narrows_the_log_to_matching_rows(self):
        cases = {
            "repo": ("ENG/mobile", "repo"),
            "actor": ("LaNo", "actor"),
            "activity": ("pr_merged", "activity"),
            "pr_id": (103, "pr_id"),
        }
        for param, (value, column) in cases.items():
            with self.subTest(param=param):
                expected = self.count(
                    f"SELECT count(*) FROM events WHERE {column} = ?", [value]
                )
                self.assertGreater(expected, 0, f"fixture has no {param}={value}")
                reply = self.events(**{param: value})
                self.assertEqual(reply.status, 200, reply.text)
                self.assertEqual(reply.headers["x-total-count"], str(expected))
                rows = reply.json()
                self.assertEqual(len(rows), expected)
                self.assertTrue(
                    all(row[column] == value for row in rows), rows[:3]
                )

    def test_the_ticket_filter_returns_the_story_and_its_pr_events(self):
        key = self.con.execute(
            "SELECT object_id FROM event_objects WHERE object_type = 'TICKET'"
            " ORDER BY object_id LIMIT 1"
        ).fetchone()[0].split(":", 1)[1]
        wanted = {f"TICKET:{key}", f"STORY:{key}"}
        expected = self.count(
            "SELECT count(*) FROM events e WHERE EXISTS ("
            " SELECT 1 FROM event_objects eo WHERE eo.event_key = e.event_key"
            " AND eo.object_type IN ('TICKET', 'STORY')"
            " AND eo.object_id IN (?, ?))",
            sorted(wanted),
        )
        self.assertGreater(expected, 0)

        for value in (key, f"TICKET:{key}", f"STORY:{key}"):
            with self.subTest(ticket=value):
                reply = self.events(ticket=value)
                self.assertEqual(reply.status, 200, reply.text)
                self.assertEqual(reply.headers["x-total-count"], str(expected))
                for row in reply.json():
                    self.assertTrue(wanted & set(row["objects"]), row)

    def test_an_unknown_parameter_is_a_400_that_lists_the_valid_ones(self):
        for path in ("/api/events", "/api/events/counts"):
            with self.subTest(path=path):
                reply = get(path, bogus="1")
                self.assertEqual(reply.status, 400, reply.text)
                body = reply.json()
                self.assertFalse(body["ok"])
                self.assertIn("bogus", body["error"])
                self.assertIn("tenant", body["valid"])
                self.assertIn("repo", body["valid"])

    def test_a_parameter_outside_the_closed_vocabulary_is_a_400(self):
        bad_activity = self.events(activity="deployed_to_prod")
        self.assertEqual(bad_activity.status, 400)
        self.assertEqual(bad_activity.json()["valid"], sorted(taxonomy.ACTIVITIES))

        for value in ("sideways", "ASC ", "descending"):
            with self.subTest(order=value):
                reply = self.events(order=value)
                self.assertEqual(reply.status, 400, reply.text)
                self.assertEqual(reply.json()["valid"], ["asc", "desc"])

        for param, value in (("limit", "0"), ("limit", "many"), ("offset", "-1"),
                             ("pr_id", "abc")):
            with self.subTest(param=param, value=value):
                reply = self.events(**{param: value})
                self.assertEqual(reply.status, 400, reply.text)
                self.assertIn(param, reply.json()["error"])

    # -- paging (GAP-04) ---------------------------------------------------- #
    def test_limit_offset_and_order_page_the_log_and_total_counts_every_row(self):
        total = self.alpha_summary["events"]
        full = self.events()
        self.assertEqual(full.headers["x-total-count"], str(total))
        rows = full.json()
        self.assertEqual(len(rows), total)

        first = self.events(limit=5)
        self.assertEqual(first.status, 200)
        self.assertEqual(len(first.json()), 5)
        self.assertEqual(first.headers["x-total-count"], str(total), "total is not the page")
        self.assertEqual(first.headers["x-limit"], "5")
        self.assertEqual(first.headers["x-offset"], "0")
        self.assertEqual(first.json(), rows[:5])

        second = self.events(limit=5, offset=5)
        self.assertEqual(len(second.json()), 5)
        self.assertEqual(second.headers["x-total-count"], str(total))
        self.assertEqual(second.headers["x-offset"], "5")
        self.assertEqual(second.json(), rows[5:10])
        self.assertFalse(
            {row["key"] for row in first.json()} & {row["key"] for row in second.json()},
            "a page must not repeat rows",
        )

        tail = self.events(offset=total - 2)
        self.assertEqual(len(tail.json()), 2)

        descending = self.events(limit=5, order="desc")
        self.assertEqual(len(descending.json()), 5)
        self.assertEqual(descending.headers["x-total-count"], str(total))
        self.assertEqual(descending.json(), list(reversed(rows[-5:])))
        # order is case-insensitive, and the whole log reverses too
        self.assertEqual(self.events(order="DESC").json(), list(reversed(rows)))

    # -- date-only upper bound (BUG-09) ------------------------------------- #
    def test_a_date_only_to_includes_that_whole_day(self):
        last = self.con.execute("SELECT max(ts) FROM events").fetchone()[0]
        day = last.date()
        next_day = day + timedelta(days=1)
        inclusive = self.count(
            "SELECT count(*) FROM events WHERE ts < ?", [next_day]
        )
        midnight = self.count(
            "SELECT count(*) FROM events WHERE ts <= ?",
            [datetime.combine(day, time())],
        )
        self.assertLess(midnight, inclusive, "the last day must hold events")

        reply = self.events(to=day.isoformat())
        self.assertEqual(reply.status, 200, reply.text)
        self.assertEqual(reply.headers["x-total-count"], str(inclusive))

        # the old behaviour dropped the whole final day; an explicit end-of-day
        # timestamp must agree with the date-only form
        self.assertEqual(
            self.events(to=f"{day.isoformat()}T23:59:59").headers["x-total-count"],
            str(inclusive),
        )
        self.assertLess(
            int(self.events(to=f"{day.isoformat()}T00:00:00").headers["x-total-count"]),
            inclusive,
        )

    def test_a_single_date_only_day_range_is_that_day(self):
        day = self.con.execute(
            "SELECT date_trunc('day', ts) FROM events"
            " GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 1"
        ).fetchone()[0].date()
        next_day = day + timedelta(days=1)
        expected = self.count(
            "SELECT count(*) FROM events WHERE ts >= ? AND ts < ?", [day, next_day]
        )
        self.assertGreater(expected, 1)

        reply = self.events(**{"from": day.isoformat(), "to": day.isoformat()})
        self.assertEqual(reply.status, 200, reply.text)
        self.assertEqual(reply.headers["x-total-count"], str(expected))
        rows = reply.json()
        self.assertEqual(len(rows), expected)
        self.assertTrue(all(row["ts"].startswith(day.isoformat()) for row in rows))

        # from == to on the last day is that day, not an empty range
        last_day = self.con.execute("SELECT max(ts) FROM events").fetchone()[0].date()
        last_day_count = self.count(
            "SELECT count(*) FROM events WHERE ts >= ? AND ts < ?",
            [last_day, last_day + timedelta(days=1)],
        )
        self.assertEqual(
            self.events(**{"from": last_day.isoformat(), "to": last_day.isoformat()})
            .headers["x-total-count"],
            str(last_day_count),
        )

    # -- the histogram (GAP-04) --------------------------------------------- #
    def test_event_counts_histogram_totals_agree_with_the_log(self):
        reply = get("/api/events/counts")
        self.assertEqual(reply.status, 200, reply.text)
        body = reply.json()
        total = self.alpha_summary["events"]

        self.assertEqual(body["tenant"], "alpha")
        self.assertEqual(body["granularity"], "day")
        self.assertEqual(body["bucket_ms"], 86_400_000)
        self.assertEqual(body["total"], total)
        self.assertEqual(sum(bucket["count"] for bucket in body["buckets"]), total)
        self.assertEqual(sum(body["by_activity"].values()), total)
        self.assertEqual(sum(body["by_repo"].values()), total)
        self.assertEqual(body["first_ts"], self.alpha_summary["first_ts"])
        self.assertEqual(body["last_ts"], self.alpha_summary["last_ts"])
        self.assertEqual(body["activity_vocabulary"], sorted(taxonomy.ACTIVITIES))
        self.assertTrue(set(body["by_activity"]) <= set(taxonomy.ACTIVITIES))

        # dense buckets: every day between the first and the last, no gaps
        first = date.fromisoformat(body["first_ts"][:10])
        last = date.fromisoformat(body["last_ts"][:10])
        self.assertEqual(len(body["buckets"]), (last - first).days + 1)
        days = [bucket["t"][:10] for bucket in body["buckets"]]
        self.assertEqual(days, [(first + timedelta(days=i)).isoformat()
                                for i in range(len(days))])

        weekly = get("/api/events/counts", granularity="week").json()
        self.assertEqual(weekly["granularity"], "week")
        self.assertEqual(weekly["bucket_ms"], 604_800_000)
        self.assertEqual(weekly["total"], total)
        hourly = get("/api/events/counts", granularity="hour").json()
        self.assertEqual(hourly["bucket_ms"], 3_600_000)
        self.assertEqual(hourly["total"], total)

    def test_event_counts_honours_filters_and_tenants(self):
        filtered = get("/api/events/counts", repo="ENG/mobile").json()
        expected = self.count("SELECT count(*) FROM events WHERE repo = ?", ["ENG/mobile"])
        self.assertEqual(filtered["total"], expected)
        self.assertEqual(filtered["by_repo"], {"ENG/mobile": expected})
        self.assertEqual(sum(bucket["count"] for bucket in filtered["buckets"]), expected)

        beta = get("/api/events/counts", tenant="beta").json()
        self.assertEqual(beta["tenant"], "beta")
        self.assertEqual(beta["total"], self.beta_summary["events"])

        bad = get("/api/events/counts", granularity="fortnight")
        self.assertEqual(bad.status, 400)
        self.assertIn("granularity", bad.json()["error"])

    # -- the harness bridge gate (SEC-2) ------------------------------------ #
    def test_eval_and_screenshot_are_401_without_the_harness_token(self):
        for path, payload in (
            ("/api/eval", {"code": "1+1"}),
            ("/api/screenshot", {"selector": ".app"}),
        ):
            with self.subTest(path=path):
                reply = post(path, payload)
                self.assertEqual(reply.status, 401, reply.text)
                body = reply.json()
                self.assertFalse(body["ok"])
                self.assertIn("token", body["error"])
        # nothing reached the browser: the gate is before the bridge
        self.assertEqual(app_module.bridge.clients, 0)

    def test_the_harness_token_is_what_opens_the_bridge(self):
        with mock.patch.object(
            app_module.bridge, "eval",
            mock.AsyncMock(return_value={"ok": True, "data": 2, "error": None}),
        ) as ev, mock.patch.object(
            app_module.bridge, "screenshot",
            mock.AsyncMock(return_value={"ok": True, "data": PNG_B64}),
        ):
            evaluated = post("/api/eval", {"code": "1+1"},
                             headers=[(TOKEN_HEADER, TOKEN.encode())])
            shot = post("/api/screenshot", {"selector": ".app"},
                        headers=[(TOKEN_HEADER, TOKEN.encode())])
        self.assertEqual(evaluated.status, 200, evaluated.text)
        self.assertEqual(evaluated.json()["data"], 2)
        ev.assert_awaited_once_with("1+1", 10.0)
        self.assertEqual(shot.status, 200, shot.text)
        self.assertTrue(shot.json()["ok"])
        self.assertEqual(shot.json()["mime"], "image/png")
        self.assertEqual(shot.json()["bytes"], len(base64.b64decode(PNG_B64)))


# --------------------------------------------------------------------------- #
# prd/33 #5 (gzip) and #8 (/api/ticket cache): the server-side performance fixes
# --------------------------------------------------------------------------- #
def decompressed(reply: Reply) -> bytes:
    """The body as the client sees it: gunzipped when the server compressed it."""
    if reply.headers.get("content-encoding") == "gzip":
        return gzip.decompress(reply.body)
    return reply.body


class ServerPerformanceTests(unittest.TestCase):
    """Compression and caching, pinned against the real ASGI app."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        cls.alpha = make_store(cls.dir, "alpha", n=12, seed=101)
        cls.patches = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "bridge", BrowserBridge()),
            mock.patch.object(app_module, "LOG", False),
            mock.patch.dict(os.environ, {
                app_module.HARNESS_TOKEN_ENV: TOKEN,
                app_module.HARNESS_TOKEN_FILE_ENV: str(cls.dir / ".harness-token"),
            }),
        ]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.patches):
            patch.stop()
        cls.tmp.cleanup()

    def setUp(self):
        app_module._analysis_cache = None
        with app_module._ticket_lock:
            app_module._ticket_cache.clear()

    # -- helpers ------------------------------------------------------------ #
    def ticket_key(self) -> str:
        con = connect(self.alpha, read_only=True)
        try:
            object_id = con.execute(
                "SELECT object_id FROM event_objects WHERE object_type = 'TICKET'"
                " ORDER BY object_id LIMIT 1"
            ).fetchone()[0]
        finally:
            con.close()
        return object_id.split(":", 1)[1]

    @staticmethod
    def bump(path: Path) -> None:
        """A new store version: a write/touch changes the (mtime_ns, size) stamp."""
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))

    # -- #5 gzip ------------------------------------------------------------ #
    def test_a_large_json_body_is_gzip_encoded_when_offered(self):
        plain = get("/api/events", limit=200)
        self.assertEqual(plain.status, 200, plain.text)
        self.assertNotIn("content-encoding", plain.headers)
        self.assertGreater(len(plain.body), 1024)

        zipped = call(
            "/api/events", query="limit=200",
            headers=[(b"accept-encoding", b"gzip")],
        )
        self.assertEqual(zipped.status, 200)
        self.assertEqual(zipped.headers.get("content-encoding"), "gzip")
        self.assertIn("accept-encoding", zipped.headers.get("vary", "").lower())
        # byte-identical once decoded, and actually smaller on the wire
        self.assertEqual(decompressed(zipped), plain.body)
        self.assertLess(len(zipped.body), len(plain.body))

    def test_a_small_reply_is_left_alone(self):
        plain = get("/api/meta")
        self.assertLess(len(plain.body), 1024)
        zipped = call("/api/meta", headers=[(b"accept-encoding", b"gzip")])
        self.assertNotIn("content-encoding", zipped.headers)
        self.assertEqual(zipped.body, plain.body)

    def test_the_harness_bridge_still_answers_a_gzip_client(self):
        payload = {"ok": True, "data": "x" * 4000, "error": None}
        with mock.patch.object(
            app_module.bridge, "eval", mock.AsyncMock(return_value=payload)
        ), mock.patch.object(
            app_module.bridge, "screenshot",
            mock.AsyncMock(return_value={"ok": True, "data": PNG_B64}),
        ):
            evaluated = post(
                "/api/eval", {"code": "'x'"},
                headers=[(TOKEN_HEADER, TOKEN.encode()),
                         (b"accept-encoding", b"gzip")],
            )
            shot = post(
                "/api/screenshot", {"selector": ".app"},
                headers=[(TOKEN_HEADER, TOKEN.encode()),
                         (b"accept-encoding", b"gzip")],
            )
        self.assertEqual(evaluated.status, 200, evaluated.text)
        self.assertEqual(evaluated.headers.get("content-encoding"), "gzip")
        self.assertEqual(json.loads(decompressed(evaluated))["data"], "x" * 4000)
        self.assertEqual(shot.status, 200, shot.text)
        self.assertTrue(json.loads(decompressed(shot))["ok"])

    # -- #8 /api/ticket cache ----------------------------------------------- #
    def test_a_repeated_ticket_request_is_served_from_cache(self):
        key = self.ticket_key()
        real = app_module.ticket_view.ticket
        with mock.patch.object(
            app_module.ticket_view, "ticket", side_effect=real
        ) as ticket:
            first = call(f"/api/ticket/{key}")
            second = call(f"/api/ticket/{key}")
            self.assertEqual(first.status, 200, first.text)
            self.assertEqual(second.body, first.body)
            self.assertEqual(ticket.call_count, 1)   # one build, not two

            # a new store version drops the entry (same invalidation as the court)
            self.bump(self.alpha)
            third = call(f"/api/ticket/{key}")
            self.assertEqual(third.body, first.body)
            self.assertEqual(ticket.call_count, 2)

    def test_an_unknown_ticket_is_cached_too(self):
        real = app_module.ticket_view.ticket
        with mock.patch.object(
            app_module.ticket_view, "ticket", side_effect=real
        ) as ticket:
            first = call("/api/ticket/ENG-999999")
            second = call("/api/ticket/ENG-999999")
        self.assertEqual(first.status, 404)
        self.assertEqual(second.status, 404)
        self.assertEqual(first.body, second.body)
        self.assertEqual(ticket.call_count, 1)

    def test_the_calendar_edit_invalidates_the_calendar_and_its_memo(self):
        path = self.dir / "alpha" / app_module.workcal.TENANT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        monday = datetime(2026, 3, 2, 9)      # a Monday, naive UTC
        path.write_text(
            'timezone: UTC\nworking_hours:\n  default: "09:00-17:00"\n',
            encoding="utf-8",
        )
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        first, error = app_module._calendar_for("alpha")
        self.assertIsNone(error)
        self.assertEqual(
            first.business_hours(None, monday, monday + timedelta(hours=8)), 8.0
        )
        # the same file version is answered from the cache (and its memo)
        self.assertIs(app_module._calendar_for("alpha")[0], first)

        path.write_text(
            'timezone: UTC\nworking_hours:\n  default: "10:00-14:00"\n',
            encoding="utf-8",
        )
        os.utime(path, ns=(2_000_000_000, 2_000_000_000))
        second, error = app_module._calendar_for("alpha")
        self.assertIsNone(error)
        self.assertIsNot(second, first)       # a new version, a new memo
        self.assertEqual(
            second.business_hours(None, monday, monday + timedelta(hours=8)), 4.0
        )


class AgingEndpointTests(unittest.TestCase):
    """prd/59: ``/api/aging`` — payload, scoping, cache and the stated fallback."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        cls.alpha = make_store(cls.dir, "alpha", n=12, seed=101)
        cls.beta = make_store(cls.dir, "beta", n=5, seed=202)
        cls.patches = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "bridge", BrowserBridge()),
            mock.patch.object(app_module, "LOG", False),
            mock.patch.dict(os.environ, {
                app_module.HARNESS_TOKEN_ENV: TOKEN,
                app_module.HARNESS_TOKEN_FILE_ENV: str(cls.dir / ".harness-token"),
            }),
        ]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.patches):
            patch.stop()
        cls.tmp.cleanup()

    def aging(self, **query):
        return get("/api/aging", **query)

    def test_the_payload_names_its_clock_cohort_and_chase_feed(self):
        reply = self.aging(tenant="alpha", kind="pr")
        self.assertEqual(reply.status, 200, reply.text)
        payload = reply.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(reply.headers["x-tenant"], "alpha")
        self.assertEqual(payload["tenant"], "alpha")
        for key in ("as_of", "hours_mode", "window", "thresholds", "rows",
                    "counts", "review_p75", "feed", "note", "bands", "min_n"):
            self.assertIn(key, payload)
        self.assertEqual(payload["kind"], "pr")
        self.assertEqual(payload["active_threshold"], "review_time")
        self.assertEqual(payload["bands"],
                         ["normal", "watch", "aging", "very_old", "insufficient"])
        # the selector is complete whatever cohort is on screen (prd/59)
        self.assertIn("cycle", payload["story_stages"])
        self.assertEqual(payload["min_n"], 20)
        self.assertIn("threshold_source", payload["feed"])
        self.assertIn("stale_prs", payload["rule_thresholds"])
        self.assertIn("sentinel_tail", payload["rule_thresholds"])
        for row in payload["rows"]:
            self.assertIn(row["band"], payload["bands"])
            self.assertTrue(row["href"].startswith("#ticket/"))

    def test_a_small_cohort_says_insufficient_instead_of_a_percentile(self):
        payload = self.aging(tenant="alpha", kind="pr").json()
        threshold = next(t for t in payload["thresholds"] if t["id"] == "review_time")
        if threshold["n"] < 20:
            self.assertFalse(threshold["available"])
            self.assertIsNone(threshold["p85"])
            self.assertIn("insufficient history", threshold["note"])
            # the rules fall back to the old constant, with the reason visible
            self.assertIn("insufficient history",
                          payload["rule_thresholds"]["stale_prs"]["source"])

    def test_each_tenant_is_answered_from_its_own_store(self):
        alpha = self.aging(tenant="alpha", kind="pr").json()
        beta = self.aging(tenant="beta", kind="pr").json()
        self.assertEqual(alpha["tenant"], "alpha")
        self.assertEqual(beta["tenant"], "beta")
        # the two fixtures differ in size, so the cohorts cannot be the same
        self.assertNotEqual(alpha["counts"]["open"], beta["counts"]["open"])

    def test_the_story_clock_is_the_default_and_the_stage_selector_is_answered(self):
        payload = self.aging(tenant="alpha").json()
        self.assertEqual(payload["kind"], "story")
        self.assertEqual(payload["active_threshold"], "story_cycle")
        self.assertIn("cycle", payload["stages"])
        stage = payload["stages"][-1]
        staged = self.aging(tenant="alpha", kind="story", stage=stage)
        self.assertEqual(staged.status, 200, staged.text)
        self.assertEqual(staged.json()["stage"], stage)

    def test_an_unknown_stage_or_kind_is_a_400_listing_the_valid_ones(self):
        for query in ({"stage": "nope"}, {"kind": "nope"}, {"only": "nope"}):
            with self.subTest(query=query):
                reply = self.aging(tenant="alpha", **query)
                self.assertEqual(reply.status, 400, reply.text)
                body = reply.json()
                self.assertFalse(body["ok"])
                self.assertIn("valid", body)

    def test_an_unknown_parameter_is_rejected_not_ignored(self):
        reply = self.aging(tenant="alpha", sort="age")
        self.assertEqual(reply.status, 400, reply.text)
        self.assertIn("sort", reply.json()["error"])

    def test_only_live_and_limit_narrow_the_rows_not_the_counts(self):
        full = self.aging(tenant="alpha", kind="pr", only="all").json()
        live = self.aging(tenant="alpha", kind="pr", only="live").json()
        self.assertLessEqual(len(live["rows"]), len(full["rows"]))
        self.assertTrue(all(row["live"] in ("live", "unmerged") for row in live["rows"]))
        # the fixture store is fully closed, so this pins the contract, not a
        # row count: the table is capped, the counts describe the whole cohort
        limited = self.aging(tenant="alpha", kind="story", limit=1).json()
        self.assertLessEqual(len(limited["rows"]), 1)
        unlimited = self.aging(tenant="alpha", kind="story").json()
        self.assertEqual(limited["counts"]["open"], unlimited["counts"]["open"])

    def test_a_repeated_request_is_served_from_cache(self):
        real = app_module.aging_view.payload
        with mock.patch.object(
            app_module.aging_view, "payload", side_effect=real,
        ) as payload:
            first = self.aging(tenant="alpha", kind="pr", days=30)
            second = self.aging(tenant="alpha", kind="pr", days=30)
            self.assertEqual(first.status, 200, first.text)
            self.assertEqual(second.body, first.body)
            self.assertEqual(payload.call_count, 1)     # one build, not two
            # a different clock is a different answer (and a different key)
            third = self.aging(tenant="alpha", kind="pr", stage="cycle", days=30)
            self.assertEqual(third.status, 200, third.text)
            self.assertEqual(payload.call_count, 2)

    def test_the_fixed_threshold_source_restores_the_constants(self):
        payload = self.aging(tenant="alpha", kind="pr", thresholds="fixed").json()
        self.assertEqual(payload["thresholds_mode"], "fixed")
        self.assertIn("fixed", payload["rule_thresholds"]["stale_prs"]["source"])
        self.assertEqual(payload["rule_thresholds"]["stale_prs"]["threshold_hours"], 40.0)


class SummaryRunTests(unittest.TestCase):
    """prd/38 #18: PR counts come from the run that saw PRs, not the latest run.

    A combined tenant has a Bitbucket run (PRs) followed by a Jira run (issues).
    The Jira run is the latest snapshot and legitimately reports 0 PRs seen;
    Sources used to print "131 kept of 0 seen" because it read that run.
    """

    def test_pr_counts_come_from_the_pr_bearing_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = make_store(root, "combined", n=8, seed=11)
            con = connect(db)
            try:
                pr_run = con.execute(
                    "SELECT as_of, prs_seen, prs_kept FROM ingest_runs"
                    " ORDER BY as_of DESC LIMIT 1"
                ).fetchone()
                later = pr_run[0] + timedelta(hours=2)
                write_run(con, {
                    "run_id": "fixture-jira-after",
                    "source": "jira-cloud",
                    "as_of": later,
                    "source_watermark": later,
                    "classifier_version": "deterministic-v1",
                    "prs_seen": 0,
                    "prs_kept": 0,
                    "prs_dropped": 0,
                    "events_written": 0,
                    "events_skipped": 0,
                    "issues_seen": 3,
                    "issues_kept": 3,
                    "issues_skipped": 0,
                })
                result = summary(con)
            finally:
                con.close()
            self.assertEqual(result["source"], "jira-cloud")   # snapshot is latest
            self.assertEqual(result["prs_seen"], int(pr_run[1]))
            self.assertEqual(result["prs_kept"], int(pr_run[2]))
            self.assertGreater(result["prs_seen"], 0)
            # prd/63 F27: every source run is reported, not only the latest.
            self.assertGreaterEqual(len(result["runs"]), 2)
            self.assertEqual(result["runs"][-1]["source"], "jira-cloud")
            self.assertTrue(all(row["source"] for row in result["runs"]))

    def test_a_store_with_no_pr_run_reports_zero_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = make_store(root, "jira-only", n=4, seed=12)
            con = connect(db)
            try:
                con.execute("UPDATE ingest_runs SET prs_seen = 0, prs_kept = 0, prs_dropped = 0")
                result = summary(con)
            finally:
                con.close()
            self.assertEqual(result["prs_seen"], 0)
            self.assertEqual(result["prs_kept"], 0)
            self.assertEqual(result["prs_dropped"], 0)


if __name__ == "__main__":
    unittest.main()

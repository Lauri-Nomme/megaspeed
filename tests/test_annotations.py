"""Annotations (prd/61): first-party dated changes in their own store table.

Two halves:

* the **contract** — the table round-trips, every kind renders a closed label,
  the ``note`` is stored verbatim (the ONE documented first-party free-text
  column), the store's ``verify`` still passes with it and still rejects a
  free-text column on any *content* table, and the canary still proves no
  scraped raw text reaches the store (including through the note);
* the **API** — POST/GET/DELETE round-trip, an unknown kind is a 400, a write
  invalidates the trend cache (the row lives in the store), and a read-only
  mirror answers 409 without losing its read path.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from analysis import annotations, ballcourt, calendar, jira, rules, trend
from ingest.store import (
    ALLOWED, CANARY, connect, ensure_schema, verify,
)
from server import app as app_module
from tests.test_before_after import comparison_fixture


# --------------------------------------------------------------------------- #
# 1. the store contract
# --------------------------------------------------------------------------- #
class StoreContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"
        # a real event row, so the content-table checks have something to alter
        from tests.test_jira_analysis import Fixture
        fx = Fixture()
        fx.pr(1, "AaAa", datetime(2026, 1, 5, 9),
              closed=datetime(2026, 1, 5, 17))
        fx.write(self.path)
        self.con = connect(self.path)
        ensure_schema(self.con)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_the_table_is_allowlisted_with_the_note_column(self):
        self.assertIn("annotations", ALLOWED)
        self.assertEqual(
            ALLOWED["annotations"],
            {"annotation_id", "created_at", "date", "kind", "value", "note", "scope"},
        )

    def test_a_note_round_trips_verbatim_in_the_table(self):
        note = "adopted after the retro; rollback is a 2-click revert"
        entry = annotations.save(self.con, {
            "date": "2026-10-08", "kind": "review_sla", "value": 4, "note": note,
        })
        self.assertEqual(entry["label"], "review SLA 4 h")
        self.assertEqual(entry["note"], note)
        stored = self.con.execute(
            "SELECT note FROM annotations WHERE annotation_id = ?", [entry["id"]]
        ).fetchone()[0]
        self.assertEqual(stored, note)
        rows = annotations.load(self.con)
        self.assertEqual([row["id"] for row in rows], [entry["id"]])
        # verify passes with the prose note (the documented carve-out)
        report = verify(self.con, ticket_projects=("ENG",))
        self.assertTrue(report["ok"], report["problems"])
        # delete removes exactly that row
        self.assertTrue(annotations.delete(self.con, entry["id"]))
        self.assertEqual(annotations.load(self.con), [])
        self.assertFalse(annotations.delete(self.con, entry["id"]))

    def test_every_kind_renders_a_closed_label(self):
        rendered = {}
        for kind in annotations.kinds():
            rendered[kind] = annotations.render({"kind": kind, "value": None})
            self.assertTrue(rendered[kind])
            self.assertNotIn("_", rendered[kind])
        self.assertEqual(rendered["review_sla"], "review SLA")
        self.assertEqual(annotations.render({"kind": "review_sla", "value": 4}),
                         "review SLA 4 h")
        self.assertEqual(annotations.render({"kind": "wip_limit", "value": 12}),
                         "WIP limit 12")
        self.assertEqual(annotations.render({"kind": "size_cap", "value": 15}),
                         "PR size cap 15 commits")
        self.assertEqual(len(annotations.catalogue()), 7)

    def test_an_unknown_kind_is_rejected(self):
        with self.assertRaises(annotations.AnnotationError):
            annotations.validate({"date": "2026-10-08", "kind": "deploy_freeze"})
        # the closed vocabulary is the taxonomy's, not a second copy
        from ingest import taxonomy
        self.assertEqual(set(annotations.kinds()), set(taxonomy.ANNOTATION_KINDS))

    def test_a_bad_date_value_or_scope_is_rejected(self):
        for entry in (
            {"date": "08/10/2026", "kind": "review_sla"},
            {"date": "2026-10-08", "kind": "review_sla", "value": "four"},
            {"date": "2026-10-08", "kind": "review_sla", "scope": "not a repo"},
        ):
            with self.assertRaises(annotations.AnnotationError):
                annotations.validate(entry)

    def test_the_canary_still_catches_scraped_text_in_the_note(self):
        annotations.save(self.con, {
            "date": "2026-10-08", "kind": "other",
            "note": f"{CANARY} this came from a raw comment",
        })
        report = verify(self.con, ticket_projects=("ENG",))
        self.assertFalse(report["ok"])
        self.assertTrue(any("canary" in problem for problem in report["problems"]),
                        report["problems"])

    def test_a_content_table_still_rejects_free_text(self):
        # the carve-out is one table, one column: the whitespace rule and the
        # column allowlist still hold for every *content* table.
        self.con.execute("UPDATE events SET actor = 'Lauri Nomme'")
        report = verify(self.con, ticket_projects=("ENG",))
        self.assertFalse(report["ok"])
        self.assertTrue(any("events.actor" in p and "whitespace" in p
                            for p in report["problems"]), report["problems"])
        self.con.execute("CREATE TABLE raw_comments (body VARCHAR)")
        report = verify(self.con, ticket_projects=("ENG",))
        self.assertFalse(report["ok"])
        self.assertTrue(any("undeclared table" in p for p in report["problems"]))

    def test_a_rolled_back_write_leaves_no_partial_row(self):
        self.con.execute("BEGIN")
        annotations.save(self.con, {
            "date": "2026-10-08", "kind": "review_sla", "value": 4,
        })
        self.con.execute("ROLLBACK")
        count = self.con.execute("SELECT count(*) FROM annotations").fetchone()[0]
        self.assertEqual(count, 0)


# --------------------------------------------------------------------------- #
# 2. calendar spans (derived, never entered twice)
# --------------------------------------------------------------------------- #
class SpanTests(unittest.TestCase):
    def test_the_20_august_holiday_and_a_vacation_become_spans(self):
        cal = calendar.load(
            config_path=Path(__file__).resolve().parent.parent / "config"
            / "calendar.default.yaml"
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.duckdb"
        from tests.test_jira_analysis import Fixture
        Fixture().write(path)
        con = connect(path, read_only=True)
        try:
            court = ballcourt.load(con, calendar=cal)
        finally:
            con.close()
        window = (datetime(2026, 8, 1), datetime(2026, 11, 30))
        spans = annotations.calendar_spans(court, window)
        holiday = next(span for span in spans if span["start"] == "2026-08-20")
        self.assertEqual(holiday["kind"], "holiday")
        self.assertEqual(holiday["label"], "taasiseseisvumispäev")
        self.assertTrue(holiday["in_window"])
        # July's holiday is outside the window: counted with the span's flag,
        # never silently drawn outside the plot.
        july = next(span for span in spans if span["start"] == "2026-06-24")
        self.assertFalse(july["in_window"])

    def test_a_tenant_vacation_is_a_span_with_its_pseudonym(self):
        cal = calendar.load(
            config_path=Path(__file__).resolve().parent.parent / "config"
            / "calendar.tenant.example.yaml"
        )
        if cal is None:
            self.skipTest("the tenant example calendar is absent")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "store.duckdb"
        from tests.test_jira_analysis import Fixture
        Fixture().write(path)
        con = connect(path, read_only=True)
        try:
            court = ballcourt.load(con, calendar=cal)
        finally:
            con.close()
        spans = annotations.calendar_spans(court, None)
        vacations = [span for span in spans if span["kind"] == "vacation"]
        for span in vacations:
            self.assertIsNotNone(span["person"])
            self.assertIn("vacation", span["label"])


# --------------------------------------------------------------------------- #
# 3. the API (through the real ASGI app)
# --------------------------------------------------------------------------- #
class Reply:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode())


async def _call_asgi(path, *, method="GET", query="", body=b"", headers=()):
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
        "headers": [(b"host", b"testserver"),
                    (b"content-type", b"application/json"), *headers],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app_module.app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return Reply(
        start["status"],
        {k.decode().lower(): v.decode() for k, v in start["headers"]},
        payload,
    )


def call(path, **kwargs):
    return asyncio.run(_call_asgi(path, **kwargs))


def get(path, **query):
    return call(path, query=urlencode(query))


def post(path, payload, **query):
    return call(path, method="POST", query=urlencode(query),
                body=json.dumps(payload).encode())


def delete(path, **query):
    return call(path, method="DELETE", query=urlencode(query))


class AnnotationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        comparison_fixture().write(cls.dir / "commits.duckdb")
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "LOG", False),
            mock.patch.object(app_module, "CALENDAR_CONFIG",
                              cls.dir / "no-calendar.yaml"),
        ]
        for patch in cls.env:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.env):
            patch.stop()
        cls.tmp.cleanup()

    def setUp(self):
        app_module._analysis_cache = None
        app_module._trend_cache.clear()
        app_module._compare_cache.clear()
        # one store shared by the class: each test starts with no annotations
        con = connect(self.dir / "commits.duckdb")
        try:
            con.execute("DELETE FROM annotations")
        finally:
            con.close()

    def test_the_api_round_trips_a_note_that_the_store_persists(self):
        reply = post("/api/annotations", {
            "date": "2026-02-02", "kind": "review_sla", "value": 4,
            "note": "adopted after the retro; rollback is a 2-click revert",
        }, tenant="commits")
        self.assertEqual(reply.status, 201, reply.body)
        entry = reply.json()["annotation"]
        self.assertEqual(entry["label"], "review SLA 4 h")
        self.assertEqual(entry["note"],
                         "adopted after the retro; rollback is a 2-click revert")
        listed = get("/api/annotations", tenant="commits").json()
        self.assertEqual([row["id"] for row in listed["annotations"]], [entry["id"]])
        self.assertEqual(len(listed["kinds"]), 7)
        # the row is in the store, and verify still passes
        con = connect(self.dir / "commits.duckdb", read_only=True)
        try:
            self.assertEqual(
                con.execute("SELECT note FROM annotations").fetchone()[0],
                entry["note"],
            )
            report = verify(con, ticket_projects=("ENG",))
            self.assertTrue(report["ok"], report["problems"])
        finally:
            con.close()
        gone = delete(f"/api/annotations/{entry['id']}", tenant="commits")
        self.assertEqual(gone.status, 200, gone.body)
        self.assertEqual(get("/api/annotations", tenant="commits").json()["annotations"], [])

    def test_an_unknown_kind_is_a_400_that_lists_the_vocabulary(self):
        reply = post("/api/annotations", {
            "date": "2026-02-02", "kind": "deploy_freeze",
        }, tenant="commits")
        self.assertEqual(reply.status, 400, reply.body)
        body = reply.json()
        self.assertIn("unknown annotation kind", body["error"])
        self.assertIn("review_sla", body["valid"])

    def test_an_unknown_annotation_is_a_404_not_a_silent_ok(self):
        reply = delete("/api/annotations/" + "deadbeef" * 4, tenant="commits")
        self.assertEqual(reply.status, 404, reply.body)

    def test_a_write_invalidates_the_trend_cache(self):
        # first request populates the cache; then a write must be visible on the
        # very next GET (the rows live in the store and the stamp was bumped).
        before = get("/api/trend", tenant="commits", metric="cycle_median").json()
        self.assertEqual(before["annotations"], [])
        created = post("/api/annotations", {
            "date": "2026-02-02", "kind": "review_sla", "value": 4,
        }, tenant="commits")
        self.assertEqual(created.status, 201)
        after = get("/api/trend", tenant="commits", metric="cycle_median").json()
        self.assertEqual(len(after["annotations"]), 1)
        self.assertTrue(after["annotations"][0]["in_window"])
        self.assertEqual(after["annotation_counts"]["policy"], 1)
        self.assertEqual(after["kinds"][0]["id"], "review_sla")

    def test_the_trend_payload_marks_out_of_window_annotations_counted_not_drawn(self):
        post("/api/annotations", {
            "date": "2025-01-01", "kind": "wip_limit", "value": 6,
        }, tenant="commits")
        payload = get("/api/trend", tenant="commits", metric="cycle_median").json()
        self.assertFalse(payload["annotations"][0]["in_window"])
        self.assertEqual(payload["annotation_counts"]["outside"], 1)
        self.assertEqual(payload["annotation_counts"]["policy"], 0)

    def test_a_cross_origin_write_is_refused_before_it_is_stored(self):
        # a browser on another origin must not be able to add a row to the store
        reply = call(
            "/api/annotations", method="POST", query="tenant=commits",
            body=json.dumps({"date": "2026-02-02", "kind": "other"}).encode(),
            headers=[(b"origin", b"https://evil.example")],
        )
        self.assertEqual(reply.status, 403, reply.body)
        self.assertEqual(
            get("/api/annotations", tenant="commits").json()["annotations"], []
        )
        # the same-origin UI (and curl, which sends no Origin) still writes
        same = call(
            "/api/annotations", method="POST", query="tenant=commits",
            body=json.dumps({"date": "2026-02-02", "kind": "other"}).encode(),
            headers=[(b"origin", b"http://testserver")],
        )
        self.assertEqual(same.status, 201, same.body)

    def test_a_read_only_mirror_still_reads_and_refuses_the_write(self):
        path = self.dir / "commits.duckdb"
        mode = path.stat().st_mode
        os.chmod(path, 0o444)
        try:
            self.assertEqual(get("/api/annotations", tenant="commits").status, 200)
            reply = post("/api/annotations", {
                "date": "2026-02-02", "kind": "other",
            }, tenant="commits")
            self.assertEqual(reply.status, 409, reply.body)
            self.assertTrue(reply.json()["unavailable"])
        finally:
            os.chmod(path, mode)


if __name__ == "__main__":
    unittest.main()

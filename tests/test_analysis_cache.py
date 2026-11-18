"""The per-store analysis bundle (perf review §6.5): one Jira load per store.

``/api/ticket/{key}`` has always cached its *payload* per key, but a cache miss
rebuilt the whole Jira layer — ``load_jira``, the stories, their events, the
sprints and the PR join — for that one key, and ``/api/insights`` /
``/api/sprints`` rebuilt the same layer again on every request. The layer is a
pure read of one *store version*, not of a key, so it now lives in a single
bundle keyed by ``(tenant, store stamp, calendar key)`` — the same identity the
ballcourt cache uses — and every later request, for any key and any endpoint,
reuses it.

These tests drive the real ASGI app against a synthetic store and count the
real loaders (a spy on ``analysis.rules.load_jira`` / ``analysis.ballcourt
.load``). They pin the three properties that make the cache safe:

* one ``load_jira`` per store across many ticket keys and across endpoints;
* a store write and a calendar edit each drop the bundle (the invalidation is
  the stamp, so an ingest run still refreshes it);
* a warm payload is byte-identical to a freshly built one, and equal to the
  uncached library call the old per-key path made.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from analysis import ballcourt, jira, tickets
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from server import app as app_module

REPO = "DEL/web"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = datetime(2026, 2, 1, 12, 0)
KEYS = ("DEL-167", "DEL-176", "DEL-181", "DEL-190", "DEL-201")


# --------------------------------------------------------------------------- #
# a synthetic store with several stories (no fixture generator, no randomness)
# --------------------------------------------------------------------------- #
class Fixture:
    """The minimum a store needs for both halves: stories, a sprint, two PRs."""

    def __init__(self, keys: tuple[str, ...] = KEYS, as_of: datetime = AS_OF):
        self.keys = keys
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
        self._count = 0

    def object(self, object_type: str, object_id: str, **kwargs) -> None:
        record = {
            "object_type": object_type,
            "object_id": object_id,
            "repo": None,
            "pr_id": None,
            "ticket_keys": [],
            "actor": None,
            "state": None,
            "created_at": None,
            "closed_at": None,
            "head_commit": None,
            "message_class": None,
            "attributes": {},
        }
        record.update(kwargs)
        self.objects.append(record)

    def event(self, key: str, ts: datetime, activity: str, actor: str = "AaAa",
              objects: list[str] | None = None, **attributes) -> None:
        self._count += 1
        event_key = f"{key}:e{self._count}"
        self.events.append({
            "event_key": event_key,
            "ts": ts,
            "activity": activity,
            "actor": actor,
            "repo": None,
            "pr_id": None,
            "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(event_key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        for object_id in objects or [f"STORY:{key}"]:
            self.relations.append((event_key, object_id.split(":", 1)[0], object_id))

    def story(self, key: str, offset_hours: int, points: int | None = 3) -> None:
        created = T0 + timedelta(hours=offset_hours)
        self.object(
            "STORY", f"STORY:{key}", ticket_keys=[key], actor="AaAa", state="done",
            created_at=created, closed_at=created + timedelta(hours=90),
            attributes={
                "summary_class": "feature",
                "epic_key": "DEL-1",
                **({"points": points} if points is not None else {}),
            },
        )
        self.event(key, created, "created")
        if points is not None:
            self.event(key, created + timedelta(hours=10), "pointed", points=points)
        self.event(key, created + timedelta(hours=20), "sprint_added",
                   objects=[f"STORY:{key}", "SPRINT:DEL#1"], sprint_id=1)
        self.event(key, created + timedelta(hours=30), "status_changed",
                   **{"from_status": "backlog", "to_status": "in_progress"})
        self.event(key, created + timedelta(hours=90), "status_changed",
                   **{"from_status": "in_progress", "to_status": "done"})

    def pr(self, number: int, opened: datetime, tickets: tuple[str, ...],
           closed: datetime) -> None:
        self.opened[number] = opened
        self.object(
            "PR", f"PR:{REPO}#{number}", repo=REPO, pr_id=number,
            ticket_keys=list(tickets), actor="AaAa", state="merged",
            created_at=opened, closed_at=closed, head_commit="a" * 40,
        )
        for hours, activity, actor in (
            (0, "pr_opened", "AaAa"),
            (10, "comment_added", "RrRr"),
            (20, "pr_pushed", "AaAa"),
            (30, "pr_approved", "RrRr"),
            (40, "pr_merged", "RrRr"),
        ):
            self._count += 1
            event_key = f"{REPO}#{number}:{self._count}"
            self.events.append({
                "event_key": event_key,
                "ts": opened + timedelta(hours=hours),
                "activity": activity,
                "actor": actor,
                "repo": REPO,
                "pr_id": number,
                "attributes": {"push_class": "content"} if activity == "pr_pushed" else {},
                "classifier_version": "deterministic-v1",
                "content_hash": hashlib.blake2b(event_key.encode(), digest_size=8).hexdigest(),
                "as_of": self.as_of,
            })
            self.relations.append((event_key, "PR", f"PR:{REPO}#{number}"))
            for ticket in tickets:
                self.relations.append((event_key, "TICKET", f"TICKET:{ticket}"))

    def run(self, run_id: str = "fixture-analysis-cache") -> dict:
        return {
            "run_id": run_id,
            "source": "fixture",
            "as_of": self.as_of,
            "source_watermark": self.as_of,
            "classifier_version": "deterministic-v1",
            "prs_seen": len(self.opened),
            "prs_kept": len(self.opened),
            "prs_dropped": 0,
            "events_written": len(self.events),
            "events_skipped": 0,
            "issues_seen": len(self.keys),
            "issues_kept": len(self.keys),
            "issues_skipped": 0,
        }

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, self.run())
        finally:
            con.close()
        return path


def build_store(path: Path, keys: tuple[str, ...] = KEYS) -> Path:
    """``keys`` stories in one sprint, with a merged PR for the first two."""
    fx = Fixture(keys)
    fx.object("SPRINT", "SPRINT:DEL#1", state="closed", created_at=T0,
              closed_at=T0 + timedelta(days=14), attributes={"end_date": "2026-01-19"})
    for offset, key in enumerate(keys):
        fx.story(key, offset * 2)
    for number, key in ((52, keys[0]), (53, keys[1] if len(keys) > 1 else keys[0])):
        opened = T0 + timedelta(hours=40)
        fx.pr(number, opened, (key,), opened + timedelta(hours=40))
    return fx.write(path)


# --------------------------------------------------------------------------- #
# ASGI driver (no httpx in this venv)
# --------------------------------------------------------------------------- #
class Reply:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode())


async def _call_asgi(path: str, query: str) -> Reply:
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

    await app_module.app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return Reply(start["status"], headers, body)


def get(path: str, **query: object) -> Reply:
    return asyncio.run(_call_asgi(path, urlencode(query)))


# --------------------------------------------------------------------------- #
# the bundle
# --------------------------------------------------------------------------- #
class AnalysisBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        cls.store = build_store(cls.dir / "del.duckdb")
        # a second dataset: a bundle is per store, so one tenant's cache must
        # never answer for another's
        cls.other = build_store(cls.dir / "ops.duckdb", keys=("OPS-7",))
        cls.patches = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "LOG", False),
        ]
        for patch in cls.patches:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.patches):
            patch.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.forget_everything()

    def tearDown(self):
        self.forget_everything()

    @staticmethod
    def forget_everything() -> None:
        """A fresh process: no bundle, no per-key payload, no calendar cache."""
        app_module._analysis_cache = None
        with app_module._ticket_lock:
            app_module._ticket_cache.clear()
        with app_module._calendar_lock:
            app_module._calendar_cache.clear()

    @contextlib.contextmanager
    def spies(self):
        """Count the real loaders without changing what they return."""
        real_jira = app_module.insights_rules.load_jira
        real_court = app_module.analyse.load
        real_threads = app_module.insights_rules.load_threads
        with mock.patch.object(
            app_module.insights_rules, "load_jira", side_effect=real_jira
        ) as load_jira, mock.patch.object(
            app_module.analyse, "load", side_effect=real_court
        ) as load_court, mock.patch.object(
            app_module.insights_rules, "load_threads", side_effect=real_threads
        ) as load_threads:
            yield load_jira, load_court, load_threads

    def ticket(self, key: str, tenant: str = "del") -> Reply:
        return get(f"/api/ticket/{key}", tenant=tenant)

    # -- one build per store ------------------------------------------------ #
    def test_load_jira_runs_once_for_many_ticket_keys(self):
        """N different keys, one Jira layer: the per-key cache is only a wrapper."""
        with self.spies() as (load_jira, load_court, _):
            bodies = {}
            for key in KEYS:
                reply = self.ticket(key)
                self.assertEqual(reply.status, 200, reply.body)
                bodies[key] = reply.body
            self.assertEqual(load_jira.call_count, 1)
            self.assertEqual(load_court.call_count, 1)
        # each key still answered for itself, not from the previous payload
        self.assertEqual(len(set(bodies.values())), len(KEYS))

    def test_the_jira_layer_is_shared_with_the_insights_and_sprint_endpoints(self):
        """Ticket, insights and sprints read one bundle; the side readers too."""
        with self.spies() as (load_jira, load_court, load_threads):
            self.assertEqual(self.ticket(KEYS[0]).status, 200)
            for _ in range(2):
                self.assertEqual(get("/api/insights", tenant="del").status, 200)
                self.assertEqual(get("/api/sprints", tenant="del").status, 200)
            self.assertEqual(load_jira.call_count, 1)
            self.assertEqual(load_court.call_count, 1)
            # insights-only readers are memoized per store version as well
            self.assertEqual(load_threads.call_count, 1)

    def test_an_insights_first_request_does_not_reload_for_the_ticket(self):
        """The order does not matter: whichever asks first builds the layer."""
        with self.spies() as (load_jira, _, _):
            self.assertEqual(get("/api/insights", tenant="del").status, 200)
            self.assertEqual(load_jira.call_count, 1)
            self.assertEqual(self.ticket(KEYS[1]).status, 200)
            self.assertEqual(load_jira.call_count, 1)

    def test_each_tenant_gets_its_own_bundle(self):
        with self.spies() as (load_jira, load_court, _):
            self.assertEqual(self.ticket(KEYS[0]).status, 200)
            other = self.ticket("OPS-7", tenant="ops")
            self.assertEqual(other.status, 200, other.body)
            self.assertEqual(load_jira.call_count, 2)
            self.assertEqual(load_court.call_count, 2)
            # the ops bundle answers about ops, and the del payload is untouched
            self.assertEqual(other.json()["key"], "OPS-7")
            self.assertEqual(app_module._analysis_cache[0][0], "ops")
            self.assertEqual(self.ticket(KEYS[0]).json()["key"], KEYS[0])

    # -- invalidation ------------------------------------------------------- #
    def test_a_store_write_drops_the_bundle(self):
        """An ingest run changes the stamp, so the next request reloads."""
        with self.spies() as (load_jira, load_court, _):
            self.assertEqual(self.ticket(KEYS[0]).status, 200)
            self.assertEqual(load_jira.call_count, 1)

            # a second run row: exactly what an ingest writes (size + mtime)
            con = connect(self.store)
            try:
                write_run(con, Fixture().run(run_id="second-run"))
            finally:
                con.close()

            self.assertEqual(self.ticket(KEYS[1]).status, 200)
            self.assertEqual(load_jira.call_count, 2)
            self.assertEqual(load_court.call_count, 2)

    def test_a_calendar_edit_drops_the_bundle(self):
        """The calendar files are part of the key, exactly like the court's."""
        tenant_dir = self.dir / "del"
        tenant_dir.mkdir(exist_ok=True)
        calendar = tenant_dir / "calendar.yaml"
        with self.spies() as (load_jira, load_court, _):
            self.assertEqual(self.ticket(KEYS[0]).status, 200)
            self.assertEqual(load_jira.call_count, 1)

            calendar.write_text(
                "timezone: Europe/Tallinn\n"
                "working_hours:\n"
                "  default: \"12:00–20:00\"\n",
                encoding="utf-8",
            )
            try:
                reply = self.ticket(KEYS[1])
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(load_jira.call_count, 2)
                self.assertEqual(load_court.call_count, 2)
            finally:
                calendar.unlink()

    # -- the payload is unchanged ------------------------------------------- #
    @staticmethod
    def stable(body: bytes) -> bytes:
        """A payload minus its clock reading: ``/api/insights`` has one."""
        document = json.loads(body)
        if isinstance(document, dict):
            document.pop("generated_at", None)
        return json.dumps(document, sort_keys=True).encode()

    def test_a_warm_payload_is_byte_identical_to_a_fresh_build(self):
        """The cache changes the cost, not one byte of the answer."""
        warm = {}
        for key in KEYS[:3]:
            warm[("ticket", key)] = self.ticket(key).body
        warm[("insights", "")] = get("/api/insights", tenant="del").body
        warm[("sprints", "")] = get("/api/sprints", tenant="del").body

        self.forget_everything()          # what a restarted server would have

        for (kind, key), body in warm.items():
            path = f"/api/ticket/{key}" if kind == "ticket" else f"/api/{kind}"
            reply = get(path, tenant="del")
            self.assertEqual(reply.status, 200, reply.body)
            self.assertEqual(self.stable(reply.body), self.stable(body),
                             f"{path} changed when it was cached")

    def test_the_api_payload_equals_the_uncached_library_call(self):
        """The old per-key path, called directly, is the reference answer."""
        con = connect(self.store, read_only=True)
        try:
            # the same court the server builds: the tenant's effective calendar
            calendar, error = app_module._calendar_for("del")
            court = ballcourt.load(con, calendar=calendar, calendar_error=error)
            data = jira.load(con, court)
            for key in KEYS[:3]:
                expected = tickets.ticket(court, data, key)
                expected["tenant"] = "del"
                reply = self.ticket(key)
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(reply.json(), expected)
        finally:
            con.close()

    def test_a_missing_key_still_reuses_the_bundle(self):
        with self.spies() as (load_jira, _, _):
            self.assertEqual(self.ticket("DEL-999").status, 404)
            self.assertEqual(self.ticket(KEYS[0]).status, 200)
            self.assertEqual(load_jira.call_count, 1)


if __name__ == "__main__":
    unittest.main()

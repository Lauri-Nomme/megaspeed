"""prd/67 W-02 — the "who is who" legend describes the window, not the store.

The live symptom was a Jira-only corpus (2,320 stories · 106 sprints · 252
people, no PRs): the Queue and Rules legend listed every person ever seen, most
of them ``0/0/0/0``, because ``person_legend()`` unioned all-time authors,
approvers, reviewers and story owners. The fix counts PR activity inside the
window, counts a story only when its work intersects the window, drops rows with
no activity at all, and says how many people that hides.

Each test pins the filter rule it exercises:

* an actor active only **outside** the window is dropped, an active one stays;
* an all-time author/approver with no window activity is dropped;
* a story owned only outside the window does not own a row (two stories, one
  row: ``stories_owned`` is the windowed count, never the store total);
* ``hidden`` is the number of people the window hid, and the note says it;
* both API call sites pass their window through.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from analysis import ballcourt, jira
from ingest.store import connect
from server import app as app_module
from tests.test_review_model import AS_OF, Store

WINDOW = (datetime(2026, 2, 1), AS_OF)


def person(store: Store, pseudonym: str, kind: str = "human") -> Store:
    """A PERSON object; ``actor_kind = bot`` is what makes it automation."""
    store.objects.append({
        "object_type": "PERSON",
        "object_id": f"PERSON:{pseudonym}",
        "repo": None,
        "pr_id": None,
        "ticket_keys": [],
        "actor": pseudonym,
        "state": None,
        "created_at": None,
        "closed_at": None,
        "head_commit": None,
        "message_class": None,
        "attributes": {"actor_kind": kind},
    })
    return store


def story(store: Store, key: str, actor: str, created: datetime,
          done: datetime | None = None, state: str = "done") -> Store:
    """A STORY object with its two window-relevant stamps (created, done)."""
    store.objects.append({
        "object_type": "STORY",
        "object_id": f"STORY:{key}",
        "repo": None,
        "pr_id": None,
        "ticket_keys": [key],
        "actor": actor,
        "state": state,
        "created_at": created,
        "closed_at": done,
        "head_commit": None,
        "message_class": None,
        "attributes": {},
    })
    return store


def build_store() -> Store:
    """Two PRs (one before the window, one inside) and four stories.

    Window: 1 Feb → 1 Mar 2026 (the prd/67 default 28 days on a 1 Mar
    snapshot). AlOl and TtTt act only in January; OoOo owns a story that
    finished in January; PpPp owns one old finished story and one that
    finished inside the window.
    """
    store = Store()
    store.pr(1, "AlOl", opened=datetime(2026, 1, 10, 9, 0),
             state="merged", closed=datetime(2026, 1, 20, 9, 0))
    store.event(1, "pr_approved", "TtTt", 5 * 24)      # 15 Jan: outside
    store.pr(2, "BbBb", opened=datetime(2026, 2, 5, 9, 0))
    store.event(2, "pr_approved", "SsSs", 5 * 24)      # 10 Feb: inside
    # a bot that acted in the window keeps its chip; one that never acted does
    # not get a row (the window drops 0/0/0/0 roles, bots included)
    person(store, "BiBi", kind="bot")
    person(store, "ZzZz", kind="bot")
    store.event(2, "comment_added", "BiBi", 7 * 24, severity="suggestion")
    story(store, "ENG-901", "OoOo",
          datetime(2026, 1, 5, 9, 0), datetime(2026, 1, 20, 9, 0))
    story(store, "ENG-902", "PpPp",
          datetime(2025, 12, 1, 9, 0), datetime(2025, 12, 10, 9, 0))
    story(store, "ENG-903", "PpPp",
          datetime(2026, 1, 10, 9, 0), datetime(2026, 2, 20, 9, 0))
    story(store, "ENG-904", "QqQq",
          datetime(2026, 2, 15, 9, 0), None, state="in progress")
    return store


def build_jira_only() -> Store:
    """The live symptom's shape: stories and people, no PRs at all.

    ``window_from_events`` has no interval to start from on such a store, so the
    Queue legend must not be filtered by a zero-length point window (the whole
    roster would be "hidden" while the answer is a real timeframe).
    """
    store = Store()
    person(store, "OoOo")
    person(store, "PpPp")
    person(store, "QqQq")
    story(store, "ENG-901", "OoOo",
          datetime(2026, 1, 5, 9, 0), datetime(2026, 1, 20, 9, 0))
    story(store, "ENG-902", "PpPp",
          datetime(2025, 12, 1, 9, 0), datetime(2025, 12, 10, 9, 0))
    story(store, "ENG-903", "PpPp",
          datetime(2026, 1, 10, 9, 0), datetime(2026, 2, 20, 9, 0))
    story(store, "ENG-904", "QqQq",
          datetime(2026, 2, 15, 9, 0), None, state="in progress")
    return store


class LegendCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self) -> Store:
        return build_store()

    def load(self):
        store = self.fixture()
        con = connect(store.write(self.path), read_only=True)
        try:
            court = ballcourt.load(con)
            data = jira.load(con, court)
        finally:
            con.close()
        # SsSs is the sentinel; SeSe is a sentinel with no activity at all, so
        # the role alone may not put a chip in a windowed legend.
        court.sentinels = {"SsSs", "SeSe"}
        return court, data

    def legend(self, window=WINDOW) -> dict:
        court, data = self.load()
        return ballcourt.person_legend(court, data.stories, window=window)

    @staticmethod
    def rows(legend: dict) -> dict[str, dict]:
        return {row["person"]: row for row in legend["people"]}


class WindowedLegendTests(LegendCase):
    def test_only_people_who_acted_in_the_window_are_listed(self):
        legend = self.legend()
        rows = self.rows(legend)
        self.assertEqual(set(rows), {"BbBb", "SsSs", "PpPp", "QqQq", "BiBi"})
        self.assertEqual(rows["BiBi"]["role"], "bot")
        self.assertEqual(rows["BiBi"]["review_responses"], 0)
        # the inside author and reviewer keep their measured counts
        self.assertEqual(rows["BbBb"]["prs_authored"], 1)
        self.assertEqual(rows["SsSs"]["approvals"], 1)
        self.assertEqual(rows["SsSs"]["review_responses"], 1)
        self.assertEqual(rows["SsSs"]["role"], "sentinel")

    def test_an_all_time_author_or_approver_with_no_window_activity_is_dropped(self):
        rows = self.rows(self.legend())
        # AlOl opened PR #1 on 10 Jan, TtTt approved it on 15 Jan: both are
        # all-time authors/approvers, both are outside the window.
        self.assertNotIn("AlOl", rows)
        self.assertNotIn("TtTt", rows)
        self.assertNotIn("OoOo", rows)   # their story finished before the window
        self.assertNotIn("SeSe", rows)   # a role without activity is not a row
        self.assertNotIn("ZzZz", rows)   # nor is an idle bot

    def test_a_story_counts_only_when_its_work_intersects_the_window(self):
        rows = self.rows(self.legend())
        # PpPp owns two stories; only ENG-903 intersects the window.
        self.assertEqual(rows["PpPp"]["stories_owned"], 1)
        self.assertEqual(rows["PpPp"]["role"], "story owner")
        # an open story intersects a window that starts after its creation
        self.assertEqual(rows["QqQq"]["stories_owned"], 1)

    def test_hidden_and_note_say_how_many_people_the_window_hides(self):
        legend = self.legend()
        # AlOl, TtTt, OoOo and the idle bot ZzZz: dropped, and counted in the
        # honest note.
        self.assertEqual(legend["hidden"], 4)
        self.assertIn("4 people with no activity in this window are hidden",
                      legend["note"])
        self.assertIn("pseudonyms only", legend["note"])
        self.assertEqual([row["person"] for row in legend["people"]],
                         ["SsSs", "BiBi", "PpPp", "QqQq", "BbBb"])

    def test_an_empty_window_returns_an_empty_legend_with_the_count(self):
        legend = self.legend(window=(datetime(2025, 1, 1),
                                     datetime(2025, 1, 31)))
        self.assertEqual(legend["people"], [])
        self.assertEqual(legend["hidden"], 9)
        self.assertIn("9 people with no activity in this window are hidden",
                      legend["note"])

    def test_without_a_window_the_all_time_legend_is_unchanged(self):
        legend = self.legend(window=None)
        rows = self.rows(legend)
        self.assertEqual(set(rows), {"AlOl", "BbBb", "TtTt", "SsSs",
                                     "OoOo", "PpPp", "QqQq", "BiBi", "ZzZz"})
        self.assertEqual(legend["hidden"], 0)
        self.assertEqual(rows["PpPp"]["stories_owned"], 2)
        self.assertNotIn("no activity in this window", legend["note"])


# --------------------------------------------------------------------------- #
# the two call sites pass the window they already computed
# --------------------------------------------------------------------------- #
class Reply:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode())


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
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return Reply(start["status"], headers, body)


def get(path: str, **query: object) -> Reply:
    return asyncio.run(_call_asgi(app_module.app, path, urlencode(query)))


class LegendApiTests(LegendCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_dir = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp_dir.name).resolve()
        cls.store_path = build_store().write(cls.dir / "del.duckdb")
        cls.jira_path = build_jira_only().write(cls.dir / "jiraonly.duckdb")
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "LOG", False),
        ]
        for patch in cls.env:
            patch.start()

    @classmethod
    def tearDownClass(cls):
        for patch in reversed(cls.env):
            patch.stop()
        cls.tmp_dir.cleanup()

    def setUp(self):
        super().setUp()
        app_module._analysis_cache = None
        app_module._advice_cache.clear()

    def tearDown(self):
        app_module._analysis_cache = None
        app_module._advice_cache.clear()
        super().tearDown()

    def assert_windowed(self, payload: dict):
        legend = payload["legend"]
        people = {row["person"] for row in legend["people"]}
        self.assertIn("BbBb", people)
        self.assertIn("SsSs", people)
        self.assertNotIn("AlOl", people)
        self.assertNotIn("TtTt", people)
        self.assertEqual(legend["hidden"], 4)
        self.assertIn("no activity in this window are hidden", legend["note"])

    def test_queues_endpoint_passes_its_window_to_the_legend(self):
        reply = get("/api/queues", days=28, tenant="del")
        self.assertEqual(reply.status, 200)
        self.assert_windowed(reply.json())

    def test_a_jira_only_store_filters_by_the_story_window_not_a_point(self):
        reply = get("/api/queues", days=28, tenant="jiraonly")
        self.assertEqual(reply.status, 200)
        legend = reply.json()["legend"]
        self.assertEqual({row["person"] for row in legend["people"]},
                         {"PpPp", "QqQq"})
        self.assertEqual(legend["hidden"], 1)
        self.assertIn("1 person with no activity in this window is hidden",
                      legend["note"])

    def test_insights_endpoint_passes_its_window_to_the_legend(self):
        reply = get("/api/insights", days=28, tenant="del")
        self.assertEqual(reply.status, 200)
        self.assert_windowed(reply.json())

"""The sprint lens (prd/26 P6): bands, scope and carry-over.

Every assertion runs against a **synthetic store with hand-computed
timestamps** — no fixture generator, no randomness — so a moved band edge, a
changed scope rule or a carry-over that stops being marked fails with the exact
number that moved.

The store holds three sprints and six stories:

* ``ENG-1`` — committed to sprint 1, done before its close: done by end;
* ``ENG-2`` — committed to sprint 1 (done after its close: **late**) and then
  sprint 2 (done by its close): the carry-over with a measured slip;
* ``ENG-3`` — added to sprint 2 and removed again, never done: **out of scope**
  now, reported as ``removed_to_backlog``, and carried out of sprint 2;
* ``ENG-4`` — never in a sprint: the story the plan never included;
* ``ENG-5`` — removed from sprint 1 and re-added to **the same** sprint, never
  done: not an explicit re-add, but still carried (it missed the close);
* ``ENG-6`` — created later, committed to the active sprint 3: the window
  filter's cohort; an active sprint has no late stories yet.

The API half drives the real ASGI app against a temp tenant directory, so the
endpoint contract (tenant scoping, 400/404, a store without the Jira layer) is
pinned as well.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from analysis import ballcourt, jira
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from server import app as app_module

REPO = "DEL/web"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = datetime(2026, 2, 20, 12, 0)


# --------------------------------------------------------------------------- #
# the synthetic store
# --------------------------------------------------------------------------- #
class Fixture:
    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self._count = 0

    def _object(self, object_type: str, object_id: str, **kwargs) -> None:
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

    def sprint(self, sid: int, start: datetime, planned_end: str | None = None,
               state: str = "closed", closed: datetime | None = None) -> str:
        object_id = f"SPRINT:DEL#{sid}"
        self._object(
            "SPRINT", object_id, state=state, created_at=start, closed_at=closed,
            attributes={
                "goal_class": "feature",
                **({"end_date": planned_end} if planned_end else {}),
            },
        )
        return object_id

    def story(self, key: str, created: datetime, points: int | None = 3,
              state: str = "open", closed: datetime | None = None,
              assignee: str = "AaAa") -> str:
        object_id = f"STORY:{key}"
        self._object(
            "STORY", object_id, ticket_keys=[key], actor=assignee, state=state,
            created_at=created, closed_at=closed,
            attributes={
                "summary_class": "feature",
                "epic_key": "DEL-1",
                **({"points": points} if points is not None else {}),
            },
        )
        self.event(key, created, "created", actor=assignee)
        return object_id

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

    def pointed(self, key: str, ts: datetime, points: int) -> None:
        self.event(key, ts, "pointed", points=points)

    def status(self, key: str, ts: datetime, to_status: str,
               from_status: str = "backlog") -> None:
        self.event(key, ts, "status_changed",
                   **{"from_status": from_status, "to_status": to_status})

    def add_to_sprint(self, key: str, ts: datetime, sid: int,
                      at_start: bool = False) -> None:
        self.event(
            key, ts, "sprint_added",
            objects=[f"STORY:{key}", f"SPRINT:DEL#{sid}"],
            sprint_id=sid, **({"at_start": True} if at_start else {}),
        )

    def remove_from_sprint(self, key: str, ts: datetime, sid: int) -> None:
        self.event(
            key, ts, "sprint_removed",
            objects=[f"STORY:{key}", f"SPRINT:DEL#{sid}"],
            sprint_id=sid,
        )

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-sprint-view",
                "source": "fixture",
                "as_of": self.as_of,
                "source_watermark": self.as_of,
                "classifier_version": "deterministic-v1",
                "prs_seen": 0,
                "prs_kept": 0,
                "prs_dropped": 0,
                "events_written": len(self.events),
                "events_skipped": 0,
                "issues_seen": sum(
                    1 for obj in self.objects if obj["object_type"] == "STORY"
                ),
                "issues_kept": sum(
                    1 for obj in self.objects if obj["object_type"] == "STORY"
                ),
                "issues_skipped": 0,
            })
        finally:
            con.close()
        return path


def fixture() -> Fixture:
    """Three sprints, six stories: the ordinary, the carried, the removed."""
    fx = Fixture()
    # sprint 1: Jan 5 09:00 → Jan 19 16:00 (planned end the same day)
    fx.sprint(1, T0, planned_end="2026-01-19",
              closed=datetime(2026, 1, 19, 16, 0))
    # sprint 2: Jan 20 09:00 → Feb 2 16:00
    fx.sprint(2, datetime(2026, 1, 20, 9, 0), planned_end="2026-02-02",
              closed=datetime(2026, 2, 2, 16, 0))
    # sprint 3: active, started Feb 3 09:00, planned to Feb 16
    fx.sprint(3, datetime(2026, 2, 3, 9, 0), planned_end="2026-02-16",
              state="active")

    # ENG-1: sprint 1, done
    fx.story("ENG-1", T0, points=5, state="done",
             closed=datetime(2026, 1, 15, 17, 0))
    fx.pointed("ENG-1", T0 + timedelta(hours=1), 5)
    fx.add_to_sprint("ENG-1", T0 + timedelta(hours=2), 1, at_start=True)
    fx.status("ENG-1", T0 + timedelta(hours=5), "in_progress")
    fx.status("ENG-1", datetime(2026, 1, 15, 17, 0), "done",
              from_status="in_progress")

    # ENG-2: sprint 1 → sprint 2: the carry-over
    fx.story("ENG-2", T0, points=3, state="done",
             closed=datetime(2026, 2, 1, 16, 0))
    fx.pointed("ENG-2", T0 + timedelta(hours=1), 3)
    fx.add_to_sprint("ENG-2", T0 + timedelta(hours=2), 1, at_start=True)
    fx.add_to_sprint("ENG-2", datetime(2026, 1, 21, 10, 0), 2)
    fx.status("ENG-2", datetime(2026, 1, 22, 9, 0), "in_progress")
    fx.status("ENG-2", datetime(2026, 2, 1, 16, 0), "done",
              from_status="in_progress")

    # ENG-3: added to sprint 2, removed again — out of scope now
    fx.story("ENG-3", T0, points=2)
    fx.pointed("ENG-3", T0 + timedelta(hours=1), 2)
    fx.add_to_sprint("ENG-3", datetime(2026, 1, 21, 11, 0), 2)
    fx.remove_from_sprint("ENG-3", datetime(2026, 1, 25, 15, 0), 2)

    # ENG-4: never in a sprint
    fx.story("ENG-4", T0, points=1)
    fx.pointed("ENG-4", T0 + timedelta(hours=1), 1)

    # ENG-5: removed from sprint 1 and re-added to the *same* sprint
    fx.story("ENG-5", T0, points=1)
    fx.pointed("ENG-5", T0 + timedelta(hours=1), 1)
    fx.add_to_sprint("ENG-5", T0 + timedelta(hours=2), 1)
    fx.remove_from_sprint("ENG-5", T0 + timedelta(days=2), 1)
    fx.add_to_sprint("ENG-5", T0 + timedelta(days=3), 1)

    # ENG-6: created later, committed to the active sprint 3
    fx.story("ENG-6", datetime(2026, 2, 10, 9, 0), points=2)
    fx.pointed("ENG-6", datetime(2026, 2, 10, 10, 0), 2)
    fx.add_to_sprint("ENG-6", datetime(2026, 2, 10, 11, 0), 3)
    return fx


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fx: Fixture | None = None):
        (fx or fixture()).write(self.path)
        con = connect(self.path, read_only=True)
        court = ballcourt.load(con)          # wall-clock: exact hours
        data = jira.load(con, court)
        return con, court, data

    def sprint_view(self, fx: Fixture | None = None, days: float | None = None):
        con, court, data = self.load(fx)
        try:
            end = court.snapshot
            start = (
                min(story.start for story in data.stories)
                if not days else end - timedelta(days=days)
            )
            return jira.sprints(court, data, (start, end))
        finally:
            con.close()


def by_sprint(payload: dict, sid: int) -> dict:
    return next(row for row in payload["sprints"] if row["sprint_id"] == sid)


# --------------------------------------------------------------------------- #
# bands
# --------------------------------------------------------------------------- #
class SprintBandTests(StoreCase):
    def test_a_band_spans_its_start_to_its_completion(self):
        payload = self.sprint_view()
        first = by_sprint(payload, 1)
        self.assertEqual(first["band_start"], "2026-01-05T09:00:00Z")
        self.assertEqual(first["band_end"], "2026-01-19T16:00:00Z")
        self.assertEqual(first["planned_end"], "2026-01-19")
        self.assertFalse(first["open"])
        # 14 days and 7 hours
        self.assertEqual(first["days"], 14.3)

    def test_an_active_sprint_ends_at_the_snapshot_not_the_plan(self):
        payload = self.sprint_view()
        active = by_sprint(payload, 3)
        self.assertTrue(active["open"])
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["planned_end"], "2026-02-16")
        # the band is measured: start → snapshot, never start → planned end
        self.assertEqual(active["band_end"], "2026-02-20T12:00:00Z")
        self.assertNotEqual(active["band_end"], "2026-02-16T00:00:00Z")
        self.assertEqual(payload["active"], 3)
        self.assertEqual(payload["count"], 3)

    def test_bands_are_ordered_by_start(self):
        payload = self.sprint_view()
        self.assertEqual([row["sprint_id"] for row in payload["sprints"]], [1, 2, 3])
        self.assertEqual(
            [row["band_start"] for row in payload["sprints"]],
            sorted(row["band_start"] for row in payload["sprints"]),
        )

    def test_a_band_reports_the_velocity_of_its_own_window(self):
        """prd/63 V-A9/V-C3: stories finished inside the band's dates."""
        payload = self.sprint_view()
        two = by_sprint(payload, 2)
        # ENG-2 finished inside sprint 2's dates and was committed to it
        self.assertEqual(two["window_done"], 1)
        self.assertEqual(two["window_own"], 1)
        self.assertEqual(two["window_carry_in"], 0)
        self.assertEqual(two["window_points"], 3)

    def test_a_closed_sprint_without_a_completion_still_stops_at_the_snapshot(self):
        fx = fixture()
        # sprint 4 is closed in the store but has no completion timestamp: its
        # band has to stop at the snapshot, and it is *not* the active sprint
        fx.sprint(4, datetime(2026, 1, 5, 9, 0), planned_end="2026-01-19",
                  state="closed")
        payload = self.sprint_view(fx)
        ghost = by_sprint(payload, 4)
        # the band has no measured end, so it stops at the snapshot ...
        self.assertTrue(ghost["open"])
        self.assertEqual(ghost["band_end"], "2026-02-20T12:00:00Z")
        # ... but the store's state still says closed, so it is not "active"
        self.assertEqual(ghost["state"], "closed")
        self.assertEqual(payload["active"], 3)

    def test_a_long_sprint_is_flagged_as_a_length_outlier(self):
        """prd/55 §5: a 40-day sprint is not comparable with 14-day siblings."""
        fx = fixture()
        fx.sprint(4, datetime(2026, 2, 6, 9, 0), planned_end="2026-03-18",
                  state="active")
        fx.story("ENG-8", datetime(2026, 2, 6, 9, 0), points=2)
        fx.add_to_sprint("ENG-8", datetime(2026, 2, 6, 10, 0), 4)
        payload = self.sprint_view(fx)
        long_sprint = by_sprint(payload, 4)
        self.assertEqual(long_sprint["planned_days"], 40.3)
        self.assertTrue(long_sprint["length_outlier"])
        self.assertIn("not comparable", long_sprint["length_note"])
        # the ordinary sprints are not flagged
        for sid in (1, 2, 3):
            self.assertFalse(by_sprint(payload, sid)["length_outlier"])

    def test_a_sprint_without_an_object_is_reported_without_a_band(self):
        fx = fixture()
        fx.story("ENG-7", T0, points=1)
        fx.add_to_sprint("ENG-7", T0 + timedelta(hours=2), 99)
        payload = self.sprint_view(fx)
        ghost = by_sprint(payload, 99)
        self.assertIsNone(ghost["band_start"])
        self.assertIsNone(ghost["band_end"])
        self.assertIn("no SPRINT object", ghost["note"])
        self.assertEqual(ghost["stories"], ["ENG-7"])


# --------------------------------------------------------------------------- #
# scope
# --------------------------------------------------------------------------- #
class SprintScopeTests(StoreCase):
    def test_stories_in_and_out_of_a_sprint_are_counted(self):
        payload = self.sprint_view()
        scope = payload["scope"]
        self.assertEqual(scope["stories"], 6)
        # ENG-1, ENG-2, ENG-5, ENG-6 are currently committed
        self.assertEqual(scope["in_sprint"], 4)
        self.assertEqual(scope["out_of_sprint"], 2)
        self.assertEqual(scope["in_share"], 0.6667)
        # the two reasons are never folded together
        self.assertEqual(scope["never_in_sprint"], 1)
        self.assertEqual(scope["removed_to_backlog"], 1)
        self.assertEqual(scope["unplanned"], ["ENG-3", "ENG-4"])

    def test_each_sprint_reports_its_own_scope(self):
        payload = self.sprint_view()
        first = by_sprint(payload, 1)
        self.assertEqual(first["stories"], ["ENG-1", "ENG-2", "ENG-5"])
        self.assertEqual(first["current_stories"], ["ENG-1", "ENG-5"])
        self.assertEqual(first["points"], 9)          # 5 + 3 + 1
        # prd/37: "done" is done by the sprint's close. ENG-1 made it; ENG-2
        # finished on Feb 1, after sprint 1 closed (Jan 19) → late.
        self.assertEqual(first["done"], 1)
        self.assertEqual(first["done_by_end"], 1)
        self.assertEqual(first["done_late"], 1)
        self.assertEqual(first["never_done"], 1)      # ENG-5
        self.assertEqual(first["done_points"], 5)
        self.assertEqual(first["late_points"], 3)
        self.assertEqual(first["open_stories"], 1)
        second = by_sprint(payload, 2)
        self.assertEqual(second["stories"], ["ENG-2", "ENG-3"])
        self.assertEqual(second["points"], 5)
        # ENG-2 was done by sprint 2's close; ENG-3 never was
        self.assertEqual(second["done"], 1)
        self.assertEqual(second["done_by_end"], 1)
        self.assertEqual(second["never_done"], 1)
        self.assertEqual(by_sprint(payload, 3)["stories"], ["ENG-6"])

    def test_the_done_breakdown_is_measured_at_the_close(self):
        payload = self.sprint_view()
        first = by_sprint(payload, 1)
        # 1 done by end, 1 late, 1 never — never "2 done" because today is later
        self.assertEqual(
            (first["done_by_end"], first["done_late"], first["never_done"]),
            (1, 1, 1),
        )
        self.assertEqual(first["done_share"], round(1 / 3, 4))
        self.assertFalse(first["on_time"])
        delivery = payload["delivery"]
        self.assertEqual(delivery["closed_sprints"], 2)
        self.assertEqual(delivery["done_by_end"], 2)   # ENG-1 + ENG-2 in sprint 2
        self.assertEqual(delivery["done_late"], 1)     # ENG-2 in sprint 1
        self.assertEqual(delivery["never_done"], 2)    # ENG-3, ENG-5
        self.assertEqual(delivery["worst"]["sprint_id"], 1)

    def test_the_window_limits_the_cohort_but_not_a_sprints_scope(self):
        # a 30-day window from Feb 20 keeps ENG-6 only (created Feb 10)
        payload = self.sprint_view(days=30)
        self.assertEqual(payload["scope"]["stories"], 1)
        self.assertEqual(payload["scope"]["in_sprint"], 1)
        self.assertEqual(payload["scope"]["unplanned"], [])
        # sprint 1's own scope is the store's, not the window's
        self.assertEqual(by_sprint(payload, 1)["story_count"], 3)
        self.assertEqual(payload["count"], 3)


# --------------------------------------------------------------------------- #
# carry-over
# --------------------------------------------------------------------------- #
class SprintCarryOverTests(StoreCase):
    def test_carry_over_is_committed_but_not_done_by_the_close(self):
        payload = self.sprint_view()
        carry = payload["carry_over"]
        # prd/37: ENG-2 finished late, ENG-3 and ENG-5 never finished — all
        # committed to a closed sprint that ended first. The old count was 0
        # here (none was explicitly re-added to a second sprint).
        self.assertEqual(carry["count"], 3)
        self.assertEqual(carry["stories"], ["ENG-2", "ENG-3", "ENG-5"])
        self.assertEqual(carry["late"], 1)
        self.assertEqual(carry["never"], 2)
        self.assertEqual(carry["share"], 0.6)         # 3 of 5 ever committed
        self.assertEqual(carry["sprints_involved"], 2)
        # worst slip first: the never-done stories have run since their close
        self.assertEqual([row["key"] for row in carry["rows"]],
                         ["ENG-5", "ENG-3", "ENG-2"])
        late = next(row for row in carry["rows"] if row["key"] == "ENG-2")
        self.assertEqual(late["status"], "late")
        self.assertEqual(late["missed_sprints"], [1])
        self.assertEqual(late["sprints"], [1, 2])
        # the per-story rows carry the same mark, so a lane can draw it
        story_rows = {row["key"]: row for row in payload["rows"]}
        self.assertTrue(story_rows["ENG-2"]["carried_over"])
        self.assertEqual(story_rows["ENG-2"]["sprint_outcomes"],
                         {"1": "late", "2": "by_end"})
        self.assertFalse(story_rows["ENG-1"]["carried_over"])

    def test_the_sprints_name_what_came_in_and_went_out(self):
        payload = self.sprint_view()
        self.assertEqual(by_sprint(payload, 1)["carried_out"], ["ENG-2", "ENG-5"])
        self.assertEqual(by_sprint(payload, 2)["carried_in"], ["ENG-2"])
        self.assertEqual(by_sprint(payload, 2)["carried_out"], ["ENG-3"])
        self.assertEqual(by_sprint(payload, 1)["carried_in"], [])
        # an active sprint has not closed, so nothing in it is carried out yet
        self.assertEqual(by_sprint(payload, 3)["carried_out"], [])

    def test_a_re_add_to_the_same_sprint_is_not_an_explicit_readd(self):
        payload = self.sprint_view()
        story_rows = {row["key"]: row for row in payload["rows"]}
        self.assertEqual(story_rows["ENG-5"]["sprints"], [1])
        # the explicit multi-sprint signal stays separate from carry-over
        self.assertFalse(story_rows["ENG-5"]["explicit_readd"])
        # ... but ENG-5 was never done by sprint 1's close, so it *is* carried
        self.assertTrue(story_rows["ENG-5"]["carried_over"])
        self.assertEqual(story_rows["ENG-5"]["adds"], 2)

    def test_a_removed_story_reports_no_current_sprint(self):
        payload = self.sprint_view()
        story_rows = {row["key"]: row for row in payload["rows"]}
        self.assertIsNone(story_rows["ENG-3"]["current_sprint"])
        self.assertEqual(story_rows["ENG-3"]["sprints"], [2])
        self.assertIsNotNone(story_rows["ENG-3"]["removed"])
        self.assertFalse(story_rows["ENG-3"]["in_sprint"])


class SprintScopeCreepTests(StoreCase):
    """prd/42 M5: scope added after the start, and where carry-over went."""

    def test_added_after_start_is_counted_per_sprint(self):
        payload = self.sprint_view()
        # ENG-2 and ENG-3 were added to sprint 2 a day after it opened; ENG-1
        # was added at the start, so it is not scope creep.
        s2 = by_sprint(payload, 2)
        self.assertEqual(sorted(s2["added_after_start_stories"]), ["ENG-2", "ENG-3"])
        self.assertEqual(s2["added_after_start"], 2)
        self.assertEqual(s2["added_after_start_share"], 1.0)   # 2 of 2
        self.assertEqual(by_sprint(payload, 1)["added_after_start_stories"], ["ENG-5"])
        self.assertEqual(by_sprint(payload, 3)["added_after_start_stories"], ["ENG-6"])

    def test_carry_over_says_where_the_story_went(self):
        payload = self.sprint_view()
        carry = payload["carry_over"]
        rows = {row["key"]: row for row in carry["rows"]}
        # ENG-2 moved into sprint 2: the re-commit is the destination.
        self.assertEqual(rows["ENG-2"]["destination_sprint"], 2)
        self.assertTrue(rows["ENG-2"]["reappears"])
        # ENG-3 missed sprint 2 and never appeared in sprint 3.
        self.assertEqual(rows["ENG-3"]["destination_sprint"], 3)
        self.assertFalse(rows["ENG-3"]["reappears"])
        # ENG-5 missed sprint 1 and never appeared in sprint 2.
        self.assertEqual(rows["ENG-5"]["destination_sprint"], 2)
        self.assertFalse(rows["ENG-5"]["reappears"])
        self.assertEqual(carry["reappeared"], 1)
        self.assertEqual(carry["never_reappeared"], 2)
        self.assertEqual(carry["no_next_sprint"], 0)


# --------------------------------------------------------------------------- #
# a store without the ticket layer
# --------------------------------------------------------------------------- #
class NoJiraTests(StoreCase):
    def test_a_pr_only_store_reports_unavailable_with_a_reason(self):
        fx = Fixture()
        fx._object("PR", f"PR:{REPO}#1", repo=REPO, pr_id=1,
                   ticket_keys=["DEL-1"], actor="AaAa", state="merged",
                   created_at=T0, closed_at=T0 + timedelta(hours=5))
        fx.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            data = jira.load(con, court)
            payload = jira.sprints(court, data, (T0, court.snapshot))
        finally:
            con.close()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["sprints"], [])
        self.assertEqual(payload["scope"]["stories"], 0)
        self.assertIn("sprint lens needs the Jira ingest", payload["note"])


# --------------------------------------------------------------------------- #
# the API (real ASGI app, temp tenant directory)
# --------------------------------------------------------------------------- #
class Reply:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
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
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return Reply(start["status"], headers, body)


def get(path: str, **query: object) -> Reply:
    return asyncio.run(_call_asgi(app_module.app, path, urlencode(query)))


class SprintApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        fixture().write(cls.dir / "del.duckdb")
        # a second dataset with no Jira layer: tenant isolation + availability
        pr_only = Fixture()
        pr_only._object("PR", f"PR:{REPO}#1", repo=REPO, pr_id=1,
                        ticket_keys=["OPS-1"], actor="AaAa", state="merged",
                        created_at=T0, closed_at=T0 + timedelta(hours=5))
        pr_only.write(cls.dir / "ops.duckdb")
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
        cls.tmp.cleanup()

    def setUp(self):
        app_module._analysis_cache = None

    def test_the_endpoint_returns_bands_scope_and_carry_over(self):
        reply = get("/api/sprints", tenant="del", days=3650)
        self.assertEqual(reply.status, 200)
        body = reply.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tenant"], "del")
        self.assertEqual(reply.headers.get("x-tenant"), "del")
        self.assertTrue(body["available"])
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["active"], 3)
        self.assertEqual([row["sprint_id"] for row in body["sprints"]], [1, 2, 3])
        first = body["sprints"][0]
        self.assertEqual(first["band_start"], "2026-01-05T09:00:00Z")
        self.assertEqual(first["band_end"], "2026-01-19T16:00:00Z")
        self.assertEqual(body["scope"]["in_sprint"], 4)
        self.assertEqual(body["scope"]["out_of_sprint"], 2)
        self.assertEqual(body["scope"]["unplanned"], ["ENG-3", "ENG-4"])
        self.assertEqual(body["carry_over"]["count"], 3)
        self.assertEqual(body["carry_over"]["stories"], ["ENG-2", "ENG-3", "ENG-5"])
        self.assertEqual(first["carried_out"], ["ENG-2", "ENG-5"])
        self.assertEqual(body["sprints"][1]["carried_in"], ["ENG-2"])
        # prd/37: the chip counts done-by-end, not done-by-now
        self.assertEqual(first["done_by_end"], 1)
        self.assertEqual(first["done_late"], 1)
        self.assertEqual(first["never_done"], 1)
        # the story rows are the drill-down, with the mark on each
        rows = {row["key"]: row for row in body["rows"]}
        self.assertTrue(rows["ENG-2"]["carried_over"])
        self.assertFalse(rows["ENG-1"]["carried_over"])
        self.assertIn("window", body)
        self.assertEqual(body["window"]["days"], body["window"]["days"])

    def test_days_filters_the_cohort(self):
        body = get("/api/sprints", tenant="del", days=30).json()
        self.assertEqual(body["scope"]["stories"], 1)
        self.assertEqual(body["scope"]["in_sprint"], 1)
        self.assertEqual(body["carry_over"]["count"], 0)

    def test_a_store_without_the_jira_layer_says_so(self):
        body = get("/api/sprints", tenant="ops").json()
        self.assertFalse(body["available"])
        self.assertEqual(body["sprints"], [])
        self.assertIn("sprint lens needs the Jira ingest", body["note"])

    def test_an_unknown_tenant_is_a_404_not_a_fallback(self):
        reply = get("/api/sprints", tenant="nope")
        self.assertEqual(reply.status, 404)
        self.assertEqual(reply.json()["valid"], ["del", "ops"])

    def test_an_unknown_parameter_is_rejected(self):
        reply = get("/api/sprints", tenant="del", level="team")
        self.assertEqual(reply.status, 400)
        self.assertIn("level", reply.json()["error"])
        self.assertIn("days", reply.json()["valid"])


if __name__ == "__main__":
    unittest.main()

"""The ticket-centric view (prd/25): ``/api/ticket/{key}`` and the ticket page's data.

Every assertion runs against a **synthetic store with hand-computed timestamps**
— no fixture generator, no randomness — so a moved boundary or a changed join
fails with the exact number that moved. The store holds two stories:

* ``DEL-167`` — eight measurable boundaries, two PRs (one merged, one still
  open), a sprint membership and a worklog: the full cycle;
* ``DEL-176`` — no ``pointed`` event and no PR: the ticket whose prose answer
  must name the missing boundary instead of inventing a number.

The API half drives the real ASGI app against a temp tenant directory, which is
also where the ``?ticket=`` filter is pinned: a ticket filter has to match the
story's own events, not only the PR-side ``TICKET:`` links.

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

from analysis import ballcourt, jira, tickets
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from server import app as app_module

REPO = "DEL/web"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = datetime(2026, 2, 1, 12, 0)


# --------------------------------------------------------------------------- #
# the synthetic store
# --------------------------------------------------------------------------- #
class Fixture:
    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
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

    def sprint(self, sid: int, start: datetime, end: datetime | None = None,
               state: str = "closed", closed: datetime | None = None) -> str:
        object_id = f"SPRINT:DEL#{sid}"
        self._object(
            "SPRINT", object_id, state=state, created_at=start, closed_at=closed,
            attributes={
                "goal_class": "feature",
                **({"end_date": end.strftime("%Y-%m-%d")} if end else {}),
            },
        )
        return object_id

    def story(self, key: str, created: datetime, assignee: str = "AaAa",
              points: int | None = 3, state: str = "open",
              closed: datetime | None = None) -> str:
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

    def add_to_sprint(self, key: str, ts: datetime, sid: int) -> None:
        self.event(
            key, ts, "sprint_added", objects=[f"STORY:{key}", f"SPRINT:DEL#{sid}"],
            sprint_id=sid,
        )

    def worklog(self, key: str, created: datetime, hours: float,
                delay_hours: float = 0.0, bucket: str = "same_day",
                actor: str = "AaAa") -> None:
        self.event(
            key, created, "worklogged", actor=actor,
            time_spent_seconds=int(hours * 3600),
            log_delay_seconds=int(delay_hours * 3600),
            log_delay_bucket=bucket,
        )

    def pr(self, number: int, author: str, opened: datetime,
           tickets: tuple[str, ...] = ("DEL-1",), state: str = "merged",
           closed: datetime | None = None) -> "Fixture":
        self.opened[number] = opened
        self._object(
            "PR", f"PR:{REPO}#{number}", repo=REPO, pr_id=number,
            ticket_keys=list(tickets), actor=author, state=state,
            created_at=opened, closed_at=closed, head_commit="a" * 40,
        )
        self.pr_event(number, "pr_opened", author, 0, tickets=tickets)
        return self

    def pr_event(self, number: int, activity: str, actor: str, hours: float,
                 tickets: tuple[str, ...] = (), people: tuple[str, ...] = (),
                 **attributes) -> "Fixture":
        self._count += 1
        event_key = f"{REPO}#{number}:{self._count}"
        self.events.append({
            "event_key": event_key,
            "ts": self.opened[number] + timedelta(hours=hours),
            "activity": activity,
            "actor": actor,
            "repo": REPO,
            "pr_id": number,
            "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(event_key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        self.relations.append((event_key, "PR", f"PR:{REPO}#{number}"))
        # the ingest links a PR event to the TICKET objects it names, so the
        # ticket filter has to see these too (not only STORY-linked events)
        for ticket in tickets:
            self.relations.append((event_key, "TICKET", f"TICKET:{ticket}"))
        for person in people:
            self.relations.append((event_key, "PERSON", f"PERSON:{person}"))
        return self

    def thread(self, number: int, index: int, *, state: str = "open",
               closed: datetime | None = None) -> "Fixture":
        self._object(
            "THREAD", f"THREAD:{REPO}#{number}:{index}", repo=REPO, pr_id=number,
            state=state, created_at=self.opened[number], closed_at=closed,
            attributes={"severity": "suggestion"},
        )
        return self

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-ticket-view",
                "source": "fixture",
                "as_of": self.as_of,
                "source_watermark": self.as_of,
                "classifier_version": "deterministic-v1",
                "prs_seen": len(self.opened),
                "prs_kept": len(self.opened),
                "prs_dropped": 0,
                "events_written": len(self.events),
                "events_skipped": 0,
                "issues_seen": sum(1 for o in self.objects if o["object_type"] == "STORY"),
                "issues_kept": sum(1 for o in self.objects if o["object_type"] == "STORY"),
                "issues_skipped": 0,
            })
        finally:
            con.close()
        return path


def fixture() -> Fixture:
    """DEL-167's eight boundaries 10 h apart, two joined PRs, one sprint."""
    fx = Fixture()
    fx.sprint(1, T0, end=T0 + timedelta(days=14), closed=T0 + timedelta(days=14))
    fx.story("DEL-167", T0, points=8, state="done",
             closed=T0 + timedelta(hours=90))
    fx.pointed("DEL-167", T0 + timedelta(hours=10), 8)
    fx.add_to_sprint("DEL-167", T0 + timedelta(hours=20), 1)
    fx.status("DEL-167", T0 + timedelta(hours=30), "in_progress")
    fx.status("DEL-167", T0 + timedelta(hours=90), "done", from_status="in_progress")
    # 4 h of work inside sprint → in-progress (started +30; logged at +25)
    fx.worklog("DEL-167", T0 + timedelta(hours=25), 4.0, delay_hours=1.0)
    # the merged PR: open +40, first review +50, push +60, approval +70, merge +80
    fx.pr(52, "AaAa", T0 + timedelta(hours=40), tickets=("DEL-1", "DEL-167"),
          closed=T0 + timedelta(hours=80))
    fx.pr_event(52, "comment_added", "RrRr", 10, severity="suggestion",
                tickets=("DEL-1", "DEL-167"))
    fx.pr_event(52, "pr_pushed", "AaAa", 20, push_class="content",
                tickets=("DEL-1", "DEL-167"))
    fx.pr_event(52, "pr_approved", "RrRr", 30, tickets=("DEL-1", "DEL-167"))
    fx.pr_event(52, "pr_merged", "RrRr", 40, tickets=("DEL-1", "DEL-167"))
    # the still-open PR: open +45, first review +55, a content push +65
    fx.pr(53, "AlNg", T0 + timedelta(hours=45), tickets=("DEL-1", "DEL-167"),
          state="open", closed=None)
    fx.pr_event(53, "comment_added", "RrRr", 10, severity="mandatory",
                tickets=("DEL-1", "DEL-167"))
    fx.pr_event(53, "pr_pushed", "AlNg", 20, push_class="content",
                tickets=("DEL-1", "DEL-167"))

    # DEL-176: no pointed event, no PR — the missing-boundary ticket
    fx.story("DEL-176", T0, points=None, state="done",
             closed=T0 + timedelta(hours=90))
    fx.add_to_sprint("DEL-176", T0 + timedelta(hours=20), 1)
    fx.status("DEL-176", T0 + timedelta(hours=30), "in_progress")
    fx.status("DEL-176", T0 + timedelta(hours=90), "done", from_status="in_progress")

    # a PR whose ticket is not in the store: the "PR without a story" case
    fx.pr(90, "AaAa", T0 + timedelta(hours=5), tickets=("DEL-999",),
          closed=T0 + timedelta(hours=15))
    fx.pr_event(90, "pr_merged", "RrRr", 10)
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


# --------------------------------------------------------------------------- #
# analysis/tickets.py
# --------------------------------------------------------------------------- #
class TicketCycleTests(StoreCase):
    def test_the_eight_segments_and_the_boundary_timestamps(self):
        con, court, data = self.load()
        try:
            payload = tickets.ticket(court, data, "DEL-167")
        finally:
            con.close()
        self.assertIsNotNone(payload)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["key"], "DEL-167")
        self.assertEqual(payload["story"]["points"], 8)
        self.assertEqual(payload["story"]["state"], "done")
        self.assertEqual(payload["story"]["epic_key"], "DEL-1")
        self.assertEqual(payload["story"]["cycle_hours"], 90.0)
        segments = payload["segments"]
        self.assertEqual(segments["order"], list(jira.SEGMENT_KEYS))
        self.assertEqual(segments["measured"], 8)
        self.assertEqual(segments["total_hours"], 90.0)
        self.assertEqual(
            {row["key"]: row["hours"] for row in segments["rows"]},
            {
                "created_ready": 10.0,
                "ready_sprint": 10.0,
                "sprint_in_progress": 10.0,
                "in_progress_pr_open": 10.0,
                "pr_open_first_review": 10.0,
                "review_rounds": 20.0,       # first review +50 → approval +70
                "final_approval_merge": 10.0,
                "merge_done": 10.0,
            },
        )
        self.assertEqual(segments["largest"], "review_rounds")
        self.assertEqual(segments["largest_share"], round(20 / 90, 4))
        # the boundary timestamps, in order, each with an availability flag.
        # prd/29 adds the three workflow-stage boundaries; without a stage map
        # (this store) the review/QA ones are honestly unavailable.
        self.assertEqual(
            list(segments["boundaries"]),
            ["created", "refined", "ready", "sprint", "started", "in_review",
             "in_verification", "pr_open", "first_review", "approval", "merged",
             "done"],
        )
        self.assertEqual(
            sorted(name for name, entry in segments["boundaries"].items()
                   if not entry["available"]),
            ["in_review", "in_verification"],
        )
        self.assertEqual(segments["boundaries"]["refined"]["ts"],
                         "2026-01-05T19:00:00Z")
        # flow efficiency is per segment, and the worklog lands in sprint→started
        self.assertEqual(segments["rows"][2]["active_hours"], 4.0)
        self.assertEqual(segments["rows"][2]["efficiency"], 0.4)

    def test_a_sprint_chip_measures_done_by_the_close_not_by_now(self):
        # prd/37: the ticket's sprint chip and the sprint lens share one
        # definition — a story done after its sprint closed is late, not done.
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=1), closed=T0 + timedelta(days=1))
        fx.story("DEL-167", T0, points=8, state="done",
                 closed=T0 + timedelta(hours=90))
        fx.add_to_sprint("DEL-167", T0 + timedelta(hours=20), 1)
        fx.status("DEL-167", T0 + timedelta(hours=30), "in_progress")
        fx.status("DEL-167", T0 + timedelta(hours=90), "done",
                  from_status="in_progress")
        con, court, data = self.load(fx)
        try:
            payload = tickets.ticket(court, data, "DEL-167")
        finally:
            con.close()
        sprints = payload["sprints"]
        self.assertTrue(sprints["carried_over"])
        self.assertEqual(sprints["late"], 1)
        self.assertEqual(sprints["never"], 0)
        membership = sprints["memberships"][0]
        self.assertEqual(membership["outcome"], "late")
        self.assertFalse(membership["done_by_end"])
        self.assertTrue(membership["closed"])
        self.assertGreater(membership["slip_hours"], 0)

    def test_the_prs_are_joined_by_the_story_key(self):
        con, court, data = self.load()
        try:
            payload = tickets.ticket(court, data, "DEL-167")
        finally:
            con.close()
        prs = {pr["number"]: pr for pr in payload["prs"]}
        self.assertEqual(sorted(prs), [52, 53])
        merged = prs[52]
        self.assertEqual(merged["state"], "merged")
        self.assertFalse(merged["censored"])
        self.assertEqual(merged["review_rounds"], 1)
        self.assertEqual(merged["push_classes"], {"content": 1})
        self.assertEqual(merged["opened"], "2026-01-07T01:00:00Z")
        self.assertEqual(merged["closed"], "2026-01-08T17:00:00Z")
        # the phases are the PR gantt's, not a second vocabulary
        self.assertTrue(merged["intervals"])
        self.assertTrue(all("phase" in interval for interval in merged["intervals"]))
        open_pr = prs[53]
        self.assertTrue(open_pr["censored"])
        self.assertEqual(open_pr["censored_at"], "2026-02-01T12:00:00Z")
        self.assertIsNone(open_pr["merged"])
        self.assertEqual(open_pr["review_rounds"], 1)

    def test_sprints_worklogs_and_events_travel_with_the_ticket(self):
        con, court, data = self.load()
        try:
            payload = tickets.ticket(court, data, "DEL-167")
        finally:
            con.close()
        self.assertFalse(payload["sprints"]["carried_over"])
        membership = payload["sprints"]["memberships"][0]
        self.assertEqual(membership["sprint_id"], 1)
        self.assertEqual(membership["state"], "closed")
        self.assertEqual(membership["added"], "2026-01-06T05:00:00Z")
        self.assertIsNotNone(membership["start"])

        self.assertEqual(payload["worklog"]["worklogs"], 1)
        self.assertEqual(payload["worklog"]["logged_hours"], 4.0)
        self.assertEqual(payload["worklogs"][0]["bucket"], "same_day")
        self.assertEqual(payload["worklogs"][0]["log_delay_hours"], 1.0)
        # active PR time is measured, not inferred
        self.assertGreater(payload["worklog"]["active_hours"], 0)
        self.assertIsNotNone(payload["worklog"]["logged_to_active_ratio"])

        activities = [event["activity"] for event in payload["events"]]
        self.assertIn("created", activities)
        self.assertIn("pointed", activities)
        self.assertIn("sprint_added", activities)
        self.assertIn("status_changed", activities)
        self.assertEqual(payload["event_count"], len(payload["events"]))

    def test_the_answer_says_where_the_time_went(self):
        con, court, data = self.load()
        try:
            payload = tickets.ticket(court, data, "DEL-167")
        finally:
            con.close()
        self.assertEqual(payload["answer_kind"], "measured")
        self.assertIn("DEL-167 took 90 h from created to done", payload["answer"])
        self.assertIn("review rounds", payload["answer"])
        self.assertIn("20 h", payload["answer"])
        self.assertNotIn("cannot say", payload["answer"])

    def test_the_answer_names_the_missing_boundary(self):
        con, court, data = self.load()
        try:
            payload = tickets.ticket(court, data, "DEL-176")
        finally:
            con.close()
        self.assertEqual(payload["answer_kind"], "partial")
        self.assertIn("the store cannot say how long the created → ready segment took",
                      payload["answer"])
        self.assertIn("because the ready boundary is missing", payload["answer"])
        self.assertIn("no 'pointed' event", payload["answer"])
        self.assertIn("refined", payload["segments"]["missing"])
        refined = next(row for row in payload["segments"]["rows"]
                       if row["key"] == "created_ready")
        self.assertFalse(refined["available"])
        self.assertEqual(refined["missing"], ["ready"])
        self.assertEqual(refined["bounds"], ["created", "ready"])
        self.assertIsNone(refined["hours"])
        # the measured segment is still reported, and never zero-filled
        started = next(row for row in payload["segments"]["rows"]
                       if row["key"] == "sprint_in_progress")
        self.assertEqual(started["hours"], 10.0)

    def test_a_pr_without_a_story_is_partial_but_answerable(self):
        con, court, data = self.load()
        try:
            payload = tickets.ticket(court, data, "DEL-999")
        finally:
            con.close()
        self.assertIsNotNone(payload)
        self.assertFalse(payload["available"])
        self.assertIsNone(payload["story"])
        self.assertEqual(payload["pr_count"], 1)
        self.assertEqual(payload["answer_kind"], "story_missing")
        self.assertIn("no STORY object for DEL-999", payload["answer"])
        self.assertIn("no STORY object", payload["note"])
        self.assertEqual(payload["segments"]["measured"], 0)

    def test_an_unknown_key_is_not_a_ticket(self):
        con, court, data = self.load()
        try:
            self.assertIsNone(tickets.ticket(court, data, "DEL-404"))
            self.assertIsNone(tickets.ticket(court, data, ""))
        finally:
            con.close()

    def test_the_prefix_forms_resolve_the_same_ticket(self):
        con, court, data = self.load()
        try:
            for key in ("DEL-167", "STORY:DEL-167", "TICKET:DEL-167"):
                with self.subTest(key=key):
                    payload = tickets.ticket(court, data, key)
                    self.assertEqual(payload["key"], "DEL-167")
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# the small adjacent fix: a note only explains an unavailable metric
# --------------------------------------------------------------------------- #
class BenchmarkTests(StoreCase):
    """prd/42 M2: "73 h" is read against its cohort."""

    def fixture(self) -> Fixture:
        fx = Fixture()
        # four stories, cycles 10 / 20 / 30 / 40 h, all 3 points
        for index, (key, hours) in enumerate(
            (("DEL-201", 10), ("DEL-202", 20), ("DEL-203", 30), ("DEL-204", 40))
        ):
            created = T0 + timedelta(days=index)
            fx.story(key, created, points=3, state="done",
                     closed=created + timedelta(hours=hours))
            fx.pointed(key, created + timedelta(hours=1), 3)
            fx.status(key, created + timedelta(hours=2), "in_progress")
            fx.status(key, created + timedelta(hours=hours), "done",
                      from_status="in_progress")
        return fx

    def test_the_cycle_is_ranked_against_the_cohort(self):
        con, court, data = self.load(self.fixture())
        try:
            body = tickets.ticket(court, data, "DEL-203")
        finally:
            con.close()
        benchmark = body["benchmark"]
        self.assertTrue(benchmark["available"])
        self.assertEqual(benchmark["rank"], 3)
        self.assertEqual(benchmark["n"], 4)
        self.assertEqual(benchmark["faster"], 2)
        self.assertEqual(benchmark["median_hours"], 25.0)
        self.assertEqual(benchmark["points"], 3)
        self.assertEqual(benchmark["points_n"], 4)
        self.assertEqual(benchmark["points_median_hours"], 25.0)
        self.assertFalse(benchmark["censored"])

    def test_a_faster_story_is_ranked_first(self):
        con, court, data = self.load(self.fixture())
        try:
            body = tickets.ticket(court, data, "DEL-201")
        finally:
            con.close()
        self.assertEqual(body["benchmark"]["rank"], 1)
        self.assertEqual(body["benchmark"]["percentile"], 0.25)

    def test_an_open_story_has_no_completed_cohort_rank(self):
        fx = self.fixture()
        fx.story("DEL-205", T0, points=3, state="open")
        con, court, data = self.load(fx)
        try:
            body = tickets.ticket(court, data, "DEL-205")
        finally:
            con.close()
        self.assertFalse(body["benchmark"]["available"])
        self.assertIn("censored", body["benchmark"]["note"])


class DoneButOpenTests(StoreCase):
    """prd/42 M4: Done in Jira, PR open — with the opened-after-close case."""

    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.story("DEL-183", T0, points=3, state="done",
                 closed=T0 + timedelta(hours=10))
        fx.status("DEL-183", T0 + timedelta(hours=10), "done")
        # the PR opened nine days after its ticket was closed
        fx.pr(66, "BbBb", T0 + timedelta(days=9), tickets=("DEL-183",),
              state="open", closed=None)
        fx.pr_event(66, "reviewer_changed", "BbBb", 1, change="added",
                    people=("RrRr",), tickets=("DEL-183",))
        return fx

    def test_the_list_names_the_ticket_who_owes_and_the_after_close_case(self):
        con, court, data = self.load(self.fixture())
        try:
            delivery = ballcourt.delivery_list(
                court, ballcourt.window_from_events(court, None),
                done_tickets=jira.done_keys(data),
            )
            body = tickets.done_but_open(court, data, delivery)
        finally:
            con.close()
        self.assertTrue(body["available"])
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["opened_after_close"], 1)
        row = body["rows"][0]
        self.assertEqual(row["key"], "DEL-183")
        self.assertEqual(row["number"], 66)
        self.assertTrue(row["opened_after_close"])
        self.assertGreater(row["opened_after_close_hours"], 24 * 8)
        self.assertEqual(row["waiting_on"], ["RrRr"])


class PrDrawerFactsTests(StoreCase):
    """prd/42 M6: the ticket payload carries the drawer's review facts."""

    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.story("DEL-210", T0, points=5, state="open")
        fx.pr(210, "AaAa", T0 + timedelta(hours=1), tickets=("DEL-210",),
              state="open", closed=None)
        fx.pr_event(210, "reviewer_changed", "AaAa", 2, change="added",
                    people=("RrRr", "SsSs"), tickets=("DEL-210",))
        fx.pr_event(210, "pr_approved", "RrRr", 4, tickets=("DEL-210",))
        fx.pr_event(210, "comment_added", "RrRr", 5, severity="suggestion",
                    tickets=("DEL-210",))
        fx.thread(210, 1, state="open")
        fx.thread(210, 2, state="resolved", closed=T0 + timedelta(hours=6))
        return fx

    def test_the_pr_row_names_reviewers_approvals_threads_and_last_event(self):
        con, court, data = self.load(self.fixture())
        try:
            threads = [
                {"repo": REPO, "pr_id": 210, "closed_at": None},
                {"repo": REPO, "pr_id": 210, "closed_at": T0},
            ]
            body = tickets.ticket(court, data, "DEL-210", threads=threads)
        finally:
            con.close()
        pr = body["prs"][0]
        self.assertEqual(pr["reviewers_total"], 2)
        self.assertEqual(pr["reviewers_approved"], 1)
        self.assertEqual(pr["reviewers_pending"], 1)
        self.assertEqual(pr["approved"], ["RrRr"])
        self.assertEqual(pr["waiting_on"], ["SsSs"])
        self.assertEqual(pr["open_threads"], 1)
        self.assertEqual(pr["last_event_activity"], "comment_added")
        self.assertIsNotNone(pr["last_event"])


class EpicTests(StoreCase):
    """prd/42 P9: an epic says it is not a story, and lists its children."""

    def fixture(self) -> Fixture:
        fx = Fixture()
        fx._object("EPIC", "EPIC:DEL-1", ticket_keys=["DEL-1"], state="open",
                   created_at=T0, closed_at=None)
        fx.story("DEL-220", T0, points=3, state="done",
                 closed=T0 + timedelta(hours=20))
        fx.status("DEL-220", T0 + timedelta(hours=20), "done")
        # every PR carries its epic key, so DEL-1 has PRs even though it is not
        # a story — the epic branch must win over "PR without a story".
        fx.pr(1, "AaAa", T0, tickets=("DEL-1",), state="open", closed=None)
        return fx

    def test_the_epic_page_states_it_is_not_a_story(self):
        con, court, data = self.load(self.fixture())
        try:
            body = tickets.ticket(court, data, "DEL-1")
        finally:
            con.close()
        self.assertEqual(body["kind"], "epic")
        self.assertFalse(body["available"])
        self.assertIn("is an epic", body["answer"])
        self.assertEqual(body["epic"]["story_count"], 1)
        self.assertEqual(body["epic"]["stories"][0]["key"], "DEL-220")
        self.assertEqual(body["segments"]["rows"], [])


class NoteTests(StoreCase):
    def test_an_available_metric_carries_no_no_event_note(self):
        con, court, data = self.load()
        try:
            metrics = jira.metrics(court, data, (T0, AS_OF))
        finally:
            con.close()
        self.assertIsNone(metrics["waiting_time_decomposition"]["note"])
        self.assertIsNone(metrics["carry_over"]["note"])
        self.assertIsNone(metrics["point_churn"]["note"])
        self.assertIsNone(metrics["worklog_cross_check"]["note"])
        self.assertIsNone(metrics["jira_conformance"]["note"])

    def test_the_cohort_decomposition_names_example_tickets(self):
        con, court, data = self.load()
        try:
            metric = jira.waiting_time_decomposition(court, data, (T0, AS_OF))
        finally:
            con.close()
        self.assertEqual(metric["largest"], "sprint_in_progress")
        # prd/25: rule evidence can link the tickets behind the largest segment
        self.assertEqual(metric["largest_examples"], ["DEL-167", "DEL-176"])

    def test_an_unavailable_metric_still_explains_itself(self):
        fx = Fixture()
        fx.story("DEL-200", T0, state="done", closed=T0 + timedelta(hours=10))
        fx.status("DEL-200", T0 + timedelta(hours=5), "in_progress")
        con, court, data = self.load(fx)
        try:
            metrics = jira.metrics(court, data, (T0, AS_OF))
        finally:
            con.close()
        self.assertFalse(metrics["worklog_cross_check"]["available"])
        self.assertIn("no 'worklogged' event", metrics["worklog_cross_check"]["note"])
        self.assertFalse(metrics["point_churn"]["available"])
        self.assertIn("no 'pointed' event", metrics["point_churn"]["note"])
        self.assertFalse(metrics["carry_over"]["available"])
        self.assertIn("no 'sprint_added' event", metrics["carry_over"]["note"])


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


class TicketApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        fixture().write(cls.dir / "del.duckdb")
        # a second dataset without this ticket: tenant isolation must not fall back
        other = Fixture()
        other.pr(7, "AaAa", T0, tickets=("OPS-7",), closed=T0 + timedelta(hours=5))
        other.pr_event(7, "pr_merged", "RrRr", 5)
        other.write(cls.dir / "ops.duckdb")
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

    def test_the_ticket_endpoint_returns_the_whole_cycle(self):
        reply = get("/api/ticket/DEL-167", tenant="del")
        self.assertEqual(reply.status, 200, reply.body)
        body = reply.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tenant"], "del")
        self.assertEqual(reply.headers.get("x-tenant"), "del")
        self.assertEqual(body["key"], "DEL-167")
        self.assertEqual(body["story"]["points"], 8)
        self.assertEqual(len(body["segments"]["rows"]), 8)
        self.assertEqual(len(body["segments"]["boundaries"]), 12)
        self.assertEqual(body["pr_count"], 2)
        self.assertEqual(body["worklog"]["worklogs"], 1)
        self.assertEqual(body["sprints"]["memberships"][0]["sprint_id"], 1)
        self.assertTrue(body["events"])
        self.assertIn("DEL-167 took", body["answer"])
        # the server attaches the calendar, so hours are business hours
        self.assertEqual(body["hours_mode"], "business")
        self.assertGreater(body["segments"]["total_hours"], 0)
        self.assertLess(body["segments"]["total_hours"], 90)
        self.assertTrue(body["answer_kind"] in ("measured", "partial"))

    def test_a_missing_boundary_is_named_by_the_api(self):
        body = get("/api/ticket/DEL-176", tenant="del").json()
        self.assertEqual(body["answer_kind"], "partial")
        self.assertIn("the ready boundary is missing", body["answer"])
        self.assertIn("refined", body["segments"]["missing"])

    def test_a_pr_without_a_story_is_still_a_ticket(self):
        body = get("/api/ticket/DEL-999", tenant="del").json()
        self.assertFalse(body["available"])
        self.assertIsNone(body["story"])
        self.assertEqual(body["pr_count"], 1)
        self.assertEqual(body["answer_kind"], "story_missing")

    def test_the_delivery_list_flags_done_in_jira_but_pr_open(self):
        """prd/34 §5: the server passes the ticket layer's Done set in."""
        body = get("/api/delivery", tenant="del").json()
        row = next(row for row in body["rows"] if row["number"] == 53)
        self.assertTrue(row["stale"])
        self.assertIn("Done in Jira", " ".join(row["stale_reasons"]))
        self.assertEqual(body["counts"]["stale"], 1)
        # prd/42 M4: the same fact is a first-class list, not only a stale pill.
        self.assertIn("done_open", body)
        self.assertGreaterEqual(body["done_open"]["count"], 1)
        self.assertIn("DEL-167", [item["key"] for item in body["done_open"]["rows"]])
        # prd/42 M6: the drawer's review facts travel on every delivery row.
        self.assertIn("reviewers_total", row)
        self.assertIn("waiting_on", row)
        self.assertIn("last_event", row)
        self.assertIn("open_threads", row)
        # prd/34 §2: the owner is named, never "review queue"
        self.assertEqual(row["waiting_on"], ["RrRr"])
        self.assertEqual(row["ball_label"], "RrRr")
        self.assertEqual(row["waiting_roles"], {"RrRr": "review"})

    def test_an_unknown_ticket_is_a_404(self):
        reply = get("/api/ticket/DEL-404", tenant="del")
        self.assertEqual(reply.status, 404)
        body = reply.json()
        self.assertFalse(body["ok"])
        self.assertIn("DEL-404", body["error"])
        self.assertEqual(body["tenant"], "del")

    def test_a_malformed_key_is_a_400(self):
        for key in ("bad", "DEL", "DEL-"):
            with self.subTest(key=key):
                reply = get(f"/api/ticket/{key}", tenant="del")
                self.assertEqual(reply.status, 400, reply.body)
                self.assertIn("not shaped like PROJ-123", reply.json()["error"])

    def test_an_unknown_query_parameter_is_a_400(self):
        reply = get("/api/ticket/DEL-167", tenant="del", bogus="1")
        self.assertEqual(reply.status, 400)
        self.assertIn("bogus", reply.json()["error"])
        self.assertIn("tenant", reply.json()["valid"])

    def test_an_unknown_tenant_is_a_404_and_not_a_fallback(self):
        reply = get("/api/ticket/DEL-167", tenant="nope")
        self.assertEqual(reply.status, 404)
        self.assertIn("nope", reply.json()["error"])
        self.assertEqual(sorted(reply.json()["valid"]), ["del", "ops"])

    def test_the_ticket_is_not_found_in_another_dataset(self):
        reply = get("/api/ticket/DEL-167", tenant="ops")
        self.assertEqual(reply.status, 404)

    def test_the_event_filter_matches_the_storys_own_events(self):
        reply = get("/api/events", tenant="del", ticket="DEL-167", limit=200)
        self.assertEqual(reply.status, 200)
        rows = reply.json()
        activities = {row["activity"] for row in rows}
        # the story side: created / pointed / sprint / status / worklog
        self.assertIn("created", activities)
        self.assertIn("pointed", activities)
        self.assertIn("sprint_added", activities)
        self.assertIn("status_changed", activities)
        # and the PR events that name the key through their TICKET links still match
        self.assertIn("pr_opened", activities)
        # a different story's events never leak in
        self.assertNotIn("DEL-176:e1", {row["key"] for row in rows})
        # the object-id form resolves to the same rows
        other = get("/api/events", tenant="del", ticket="STORY:DEL-167", limit=200)
        self.assertEqual(
            [row["key"] for row in other.json()],
            [row["key"] for row in rows],
        )

    def test_the_object_filter_returns_the_story_and_its_prs(self):
        reply = get("/api/objects", tenant="del", ticket="DEL-167")
        self.assertEqual(reply.status, 200)
        ids = {row["object_id"] for row in reply.json()}
        self.assertIn("STORY:DEL-167", ids)
        self.assertIn("PR:DEL/web#52", ids)
        self.assertIn("PR:DEL/web#53", ids)
        self.assertNotIn("STORY:DEL-176", ids)


if __name__ == "__main__":
    unittest.main()

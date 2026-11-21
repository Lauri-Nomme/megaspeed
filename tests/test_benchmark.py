"""Benchmark contrast (prd/57): one per-item duration next to its cohort median.

The fixture is hand-built so every median is computable in one's head:

* ten baseline stories with eight 10 h segments and an 80 h cycle;
* one target story whose ``review rounds`` is 20 h and cycle is 90 h, so the
  target's multiples are 2.0× and 90/80× the completed-story medians;
* eleven merged PRs (30 h cycles, 30 h review phase; the target's 40 h / 35 h);
* ten open PRs waiting on a reviewer (the delivery-list wait cohort) and ten
  reviewer answers (the response-time cohort).

A second, small fixture (three stories, three open PRs) pins the N < 10
suppression. The API half drives the real ASGI app so the payload shape the UI
reads is pinned, not only the library call.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from analysis import ballcourt, benchmark, jira, tickets
from analysis.benchmark import BENCHMARK_MIN_N
from ingest.store import connect
from server import app as app_module
from tests.test_jira_analysis import Fixture, T0, AS_OF

REPO = "ENG/platform"


class BenchFixture(Fixture):
    """The shared Jira/PR fixture plus the PERSON links a roster needs."""

    def respond(self, number: int, activity: str, actor: str, hours: float,
                people: tuple[str, ...] = (), **attributes) -> None:
        self.pr_event(number, activity, actor, hours, **attributes)
        key = self.events[-1]["event_key"]
        for person in people:
            self.relations.append((key, "PERSON", f"PERSON:{person}"))

    def open_pr(self, number: int, holder: str, opened: datetime,
                answered_hours: float | None = None) -> None:
        """An open PR waiting on ``holder`` (optionally already answered)."""
        self.pr(number, "AaAa", opened, tickets=("OPS-2",), state="open",
                closed=None)
        self.respond(number, "reviewer_changed", "AaAa", 1, people=(holder,),
                     change="added")
        if answered_hours is not None:
            self.respond(number, "comment_added", holder, answered_hours)


def contrast_fixture(stories: int = 10, open_prs: int = 10) -> BenchFixture:
    """``stories`` baselines + one target; ``open_prs`` open PRs for the waits."""
    fx = BenchFixture()
    fx.sprint(1, T0, end=T0 + timedelta(days=14), closed=T0 + timedelta(days=14))
    for index in range(stories):
        key = f"ENG-{index + 1}"
        base = T0 + timedelta(days=index)
        fx.story(key, base, state="done", closed=base + timedelta(hours=80))
        for offset, points in ((10, 3),):
            fx.pointed(key, base + timedelta(hours=offset), points)
        fx.add_to_sprint(key, base + timedelta(hours=20), 1)
        fx.status(key, base + timedelta(hours=30), "in_progress")
        fx.status(key, base + timedelta(hours=80), "done",
                  from_status="in_progress")
        number = 100 + index
        fx.pr(number, "AaAa", base + timedelta(hours=40), tickets=(key,),
              closed=base + timedelta(hours=70))
        fx.respond(number, "reviewer_changed", "AaAa", 1, people=("RrRr",),
                   change="added")
        fx.respond(number, "comment_added", "RrRr", 10)
        fx.pr_event(number, "pr_approved", "RrRr", 20)
        fx.pr_event(number, "pr_merged", "RrRr", 30)

    # the target: the same eight boundaries, but review rounds takes 20 h and
    # the PR merges 10 h later (a 90 h cycle, a 40 h PR cycle)
    base = T0
    fx.story("ENG-99", base, state="done", closed=base + timedelta(hours=90))
    fx.pointed("ENG-99", base + timedelta(hours=10), 8)
    fx.add_to_sprint("ENG-99", base + timedelta(hours=20), 1)
    fx.status("ENG-99", base + timedelta(hours=30), "in_progress")
    fx.status("ENG-99", base + timedelta(hours=90), "done",
              from_status="in_progress")
    fx.pr(50, "AaAa", base + timedelta(hours=40), tickets=("ENG-99",),
          closed=base + timedelta(hours=80))
    fx.respond(50, "reviewer_changed", "AaAa", 1, people=("RrRr",),
               change="added")
    fx.respond(50, "comment_added", "RrRr", 10)
    fx.pr_event(50, "pr_approved", "RrRr", 30)
    fx.pr_event(50, "pr_merged", "RrRr", 40)

    for index in range(open_prs):
        fx.open_pr(200 + index, "FrMo", T0 + timedelta(days=index + 1))
    return fx


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fx: BenchFixture | None = None):
        (fx or contrast_fixture()).write(self.path)
        con = connect(self.path, read_only=True)
        court = ballcourt.load(con)          # wall-clock: exact hours
        data = jira.load(con, court)
        return con, court, data

    def ticket(self, key: str = "ENG-99", fx: BenchFixture | None = None):
        con, court, data = self.load(fx)
        try:
            return court, data, tickets.ticket(court, data, key)
        finally:
            con.close()


def _median(values: list[float]) -> float:
    ordered = sorted(float(value) for value in values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


# --------------------------------------------------------------------------- #
# 1. the helper itself
# --------------------------------------------------------------------------- #
class ContrastHelperTests(unittest.TestCase):
    def test_a_cohort_of_ten_produces_a_median_and_a_multiple(self):
        block = benchmark.contrast(
            20, [10] * 9 + [20], basis="median of a test cohort",
        )
        self.assertTrue(block["available"])
        self.assertEqual(block["median"], 10.0)
        self.assertEqual(block["multiple"], 2.0)
        self.assertEqual(block["n"], 10)
        self.assertIn("test cohort", block["basis"])

    def test_a_cohort_below_ten_is_suppressed_with_the_reason(self):
        block = benchmark.contrast(
            20, [10] * 9, basis="median of a test cohort",
        )
        self.assertFalse(block["available"])
        self.assertIsNone(block["multiple"])
        self.assertIn(f"N=9 < {BENCHMARK_MIN_N}", block["note"])

    def test_no_value_or_no_cohort_is_a_stated_result_not_a_zero(self):
        self.assertIn("no measured value", benchmark.contrast(
            None, [10] * 10, basis="cohort")["note"])
        self.assertFalse(benchmark.contrast(5, [], basis="cohort")["available"])

    def test_stats_is_the_cohort_half_and_contrast_reuses_it(self):
        block = benchmark.stats([1, 2, 3] * 4, basis="cohort")
        self.assertTrue(block["available"])
        self.assertEqual(block["median"], 2.0)
        self.assertEqual(block["n"], 12)
        self.assertNotIn("multiple", block)


# --------------------------------------------------------------------------- #
# 2. the ticket: segments, cycle and PR phases
# --------------------------------------------------------------------------- #
class TicketBenchmarkTests(StoreCase):
    def test_a_segment_carries_its_cohort_median_and_multiple(self):
        court, data, payload = self.ticket()
        segments = {row["key"]: row for row in payload["segments"]["rows"]}
        review = segments["review_rounds"]
        self.assertEqual(review["hours"], 20.0)
        self.assertTrue(review["benchmark"]["available"])
        self.assertEqual(review["benchmark"]["median"], 10.0)
        self.assertEqual(review["benchmark"]["n"], 11)
        self.assertEqual(review["benchmark"]["multiple"], 2.0)
        # an unchanged segment is 1.0× the median
        self.assertEqual(segments["created_ready"]["benchmark"]["multiple"], 1.0)
        # the multiple is labelled with what it is a multiple of
        self.assertIn("completed stories", review["benchmark"]["basis"])

    def test_the_segment_median_matches_the_segment_cohort_recompute(self):
        con, court, data = self.load()
        try:
            payload = tickets.ticket(court, data, "ENG-99")
            cohort = jira.segment_cohort(court, data)
        finally:
            con.close()
        for row in payload["segments"]["rows"]:
            expected = _median(cohort[row["key"]])
            self.assertEqual(row["benchmark"]["median"], expected, row["key"])
            self.assertEqual(
                row["benchmark"]["multiple"],
                round(row["hours"] / expected, 2),
                row["key"],
            )

    def test_the_cycle_is_read_against_completed_stories(self):
        court, data, payload = self.ticket()
        block = payload["benchmark"]
        self.assertEqual(payload["segments"]["cycle_hours"], 90.0)
        self.assertEqual(block["median_hours"], 80.0)
        self.assertEqual(block["n"], 11)
        contrast = block["contrast"]
        self.assertTrue(contrast["available"])
        self.assertEqual(contrast["median"], 80.0)
        self.assertEqual(contrast["multiple"], round(90 / 80, 2))

    def test_pr_phases_and_the_pr_cycle_have_their_own_cohorts(self):
        con, court, data = self.load()
        try:
            payload = tickets.ticket(court, data, "ENG-99")
            cohort = ballcourt.phase_cohort(court)
            cycles = ballcourt.pr_cycle_cohort(court)
            # the merged cohort: ten baselines at 30 h, the target at 40 h
            self.assertEqual(_median(cycles), 30.0)
            self.assertEqual(len(cycles), 11)
        finally:
            con.close()
        target = next(pr for pr in payload["prs"] if pr["number"] == 50)
        review = target["phase_benchmarks"]["review"]
        self.assertEqual(review["median"], _median(cohort["review"]))
        self.assertEqual(
            review["multiple"], round(target["phase_hours"]["review"] / review["median"], 2)
        )
        # a 40 h PR cycle against the 30 h merged-PR median
        self.assertEqual(target["cycle_benchmark"]["median"], 30.0)
        self.assertEqual(target["cycle_benchmark"]["multiple"], round(40 / 30, 2))

    def test_a_declined_pr_is_not_benchmarked_against_merged_prs(self):
        """prd/63 B10: a bot-declined zombie is not delivered work."""
        fx = contrast_fixture()
        for record in fx.objects:
            if record["object_id"] == "PR:ENG/platform#50":
                record["state"] = "declined"
        con, court, data = self.load(fx)
        try:
            payload = tickets.ticket(court, data, "ENG-99")
        finally:
            con.close()
        target = next(pr for pr in payload["prs"] if pr["number"] == 50)
        block = target["cycle_benchmark"]
        self.assertTrue(block["declined"])
        self.assertFalse(block["available"])
        self.assertIsNone(block["multiple"])
        self.assertIn("declined", block["note"])
        # phases are not compared with the merged-PR cohort either
        for phase in target["phase_benchmarks"].values():
            self.assertTrue(phase["declined"])
            self.assertIsNone(phase["multiple"])

    def test_a_small_story_cohort_suppresses_every_ticket_contrast(self):
        court, data, payload = self.ticket(fx=contrast_fixture(stories=2))
        for row in payload["segments"]["rows"]:
            self.assertFalse(row["benchmark"]["available"], row["key"])
            self.assertIsNone(row["benchmark"]["multiple"])
            self.assertIn("cohort too small", row["benchmark"]["note"])
        # the cycle rank/median still exists (3 completed stories), the
        # *contrast* is suppressed — a rank is not a multiple
        self.assertFalse(payload["benchmark"]["contrast"]["available"])
        self.assertIn("cohort too small", payload["benchmark"]["contrast"]["note"])

    def test_an_open_story_has_no_cycle_contrast(self):
        con, court, data = self.load(contrast_fixture(open_prs=0))
        try:
            # make the target open by giving it no done status
            story = jira.story_for(data, "ENG-99")
            story.events = [
                event for event in story.events
                if not (event["activity"] == "status_changed"
                        and (event.get("attributes") or {}).get("to_status") == "done")
            ]
            story.closed = None
            story.state = "open"
            story._cycle_cache = None
            payload = tickets.ticket(court, data, "ENG-99")
        finally:
            con.close()
        self.assertFalse(payload["benchmark"]["available"])
        self.assertIn("censored", payload["benchmark"]["note"])


# --------------------------------------------------------------------------- #
# 3. the delivery list and response times
# --------------------------------------------------------------------------- #
class DeliveryBenchmarkTests(StoreCase):
    def test_the_wait_benchmark_matches_the_window_cohort(self):
        con, court, data = self.load()
        try:
            payload = ballcourt.delivery_list(
                court, (T0, court.snapshot),
            )
        finally:
            con.close()
        rows = payload["rows"]
        self.assertGreaterEqual(len(rows), 10)
        cohort = [row["waiting_hours"] for row in rows if row["in_window"]]
        block = payload["wait_benchmark"]
        self.assertTrue(block["available"])
        self.assertEqual(block["median"], _median(cohort))
        self.assertEqual(block["n"], len(cohort))
        for row in rows:
            self.assertEqual(
                row["wait_benchmark"]["multiple"],
                round(row["waiting_hours"] / block["median"], 2),
            )

    def test_a_small_delivery_cohort_suppresses_the_wait_contrast(self):
        con, court, data = self.load(contrast_fixture(stories=2, open_prs=3))
        try:
            payload = ballcourt.delivery_list(court, (T0, court.snapshot))
        finally:
            con.close()
        self.assertFalse(payload["wait_benchmark"]["available"])
        for row in payload["rows"]:
            self.assertFalse(row["wait_benchmark"]["available"])
            self.assertIn("N=3", row["wait_benchmark"]["note"])

    def test_the_phase_benchmarks_are_the_merged_pr_cohort(self):
        con, court, data = self.load()
        try:
            payload = ballcourt.delivery_list(court, (T0, court.snapshot))
            cohort = ballcourt.phase_cohort(court)
        finally:
            con.close()
        self.assertIn("review", payload["phase_benchmarks"])
        self.assertEqual(
            payload["phase_benchmarks"]["review"]["median"],
            _median(cohort["review"]),
        )
        self.assertEqual(payload["phase_benchmarks"]["review"]["n"], 11)

    def test_response_times_compare_each_person_to_the_answer_cohort(self):
        con, court, data = self.load()
        try:
            payload = ballcourt.response_times(court, (T0, court.snapshot))
        finally:
            con.close()
        block = payload["response_benchmark"]
        self.assertTrue(block["available"])
        self.assertGreaterEqual(block["n"], 10)
        self.assertEqual(block["median"], 9.0)
        person = next(row for row in payload["people"] if row["person"] == "RrRr")
        contrast = person["response_benchmark"]
        self.assertTrue(contrast["available"])
        self.assertEqual(contrast["median"], 9.0)
        self.assertEqual(contrast["multiple"], 1.0)


# --------------------------------------------------------------------------- #
# 4. the API attaches the benchmarks (real ASGI app)
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
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return Reply(start["status"], headers, body)


def get(path: str, **query: object) -> Reply:
    return asyncio.run(_call_asgi(app_module.app, path, urlencode(query)))


class BenchmarkApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        contrast_fixture().write(cls.dir / "bench.duckdb")
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "LOG", False),
            # wall-clock, so the hand-computed 10 h / 20 h medians hold
            mock.patch.object(
                app_module, "CALENDAR_CONFIG", cls.dir / "no-calendar.yaml",
            ),
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
        app_module._ticket_cache.clear()

    def test_the_ticket_endpoint_carries_segment_and_cycle_benchmarks(self):
        reply = get("/api/ticket/ENG-99", tenant="bench")
        self.assertEqual(reply.status, 200, reply.body)
        body = reply.json()
        segments = {row["key"]: row for row in body["segments"]["rows"]}
        self.assertEqual(segments["review_rounds"]["benchmark"]["multiple"], 2.0)
        self.assertEqual(body["benchmark"]["contrast"]["multiple"],
                         round(90 / 80, 2))
        self.assertEqual(body["prs"][0]["cycle_benchmark"]["median"], 30.0)

    def test_the_delivery_endpoint_carries_the_wait_and_phase_benchmarks(self):
        reply = get("/api/delivery", tenant="bench")
        self.assertEqual(reply.status, 200, reply.body)
        body = reply.json()
        self.assertTrue(body["wait_benchmark"]["available"])
        self.assertIn("review", body["phase_benchmarks"])
        self.assertIn("wait_benchmark", body["rows"][0])

    def test_the_second_ticket_reuses_the_cached_cohorts(self):
        """One benchmark build per store version, not one per ticket key."""
        first = get("/api/ticket/ENG-99", tenant="bench")
        second = get("/api/ticket/ENG-1", tenant="bench")
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        # the store has > 1 completed story; both answers quote the same cohort
        self.assertEqual(first.json()["benchmark"]["contrast"]["n"], 11)
        self.assertEqual(second.json()["benchmark"]["contrast"]["n"], 11)

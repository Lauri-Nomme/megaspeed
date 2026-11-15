"""Business-hours calendar (prd/19): config, computation, analysis, API.

Everything here is **deterministic**: a fixed calendar file and fixed intervals
give exact business-hour counts, so a change in the day walk, the half-day rule,
the vacation handling or the timezone conversion fails with the number that
moved. The intervals are written in Tallinn local time and converted to the
naive-UTC form the store holds, so the tests read like the calendar they check.

Cases pinned (prd/19):

* a normal working day,
* a weekend (and a night) excluded,
* a half-day (24 Dec, 31 Dec) counting half,
* a full public holiday,
* a vacation,
* a per-person override (`LaNo: "12:00–20:00"`),
* no calendar at all falling back to wall-clock,
* the vacation-blocking signal, with and without delegation,
* the API: ``hours_mode`` on every analysis view and ``GET /api/calendar``.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from analysis import ballcourt, calendar as workcal, rules
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from server import app as app_module

TZ = ZoneInfo("Europe/Tallinn")
REPO = "ENG/platform"

DEFAULT_YAML = """
timezone: Europe/Tallinn
weekdays: [1, 2, 3, 4, 5]
working_hours:
  default: "09:00–17:00"
easter: [2026]
holidays:
  - {month_day: "01-01", type: full, name: uusaasta}
  - {month_day: "12-24", type: half, name: jõululaupäev}
  - {month_day: "12-31", type: half, name: aastavahetus}
overrides:
  LaNo: "12:00–20:00"
vacations:
  LaNo:
    - {start: 2026-07-06, end: 2026-07-17}
"""


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A Tallinn local wall-clock time, as the naive UTC the store holds."""
    return (
        datetime(year, month, day, hour, minute, tzinfo=TZ)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def write_calendar(root: Path, text: str = DEFAULT_YAML, name: str = "calendar.yaml") -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 1. the committed default config
# --------------------------------------------------------------------------- #
class DefaultConfigTests(unittest.TestCase):
    """The committed file is part of the feature: it must load and be Estonian."""

    def setUp(self):
        self.cal = workcal.load()

    def test_the_committed_default_loads(self):
        self.assertIsNotNone(self.cal)
        self.assertEqual(self.cal.hours_mode, "business")
        self.assertEqual(self.cal.timezone, "Europe/Tallinn")
        self.assertEqual(self.cal.default_window, (9 * 60, 17 * 60))
        self.assertEqual(sorted(self.cal.weekdays), [1, 2, 3, 4, 5])
        self.assertEqual(self.cal.vacations, {})

    def test_estonian_public_holidays_are_present(self):
        full = {
            (1, 1), (2, 24), (5, 1), (6, 23), (6, 24), (8, 20), (12, 25), (12, 26),
        }
        for month_day in full:
            holiday = self.cal.holiday_for(date(2026, *month_day))
            self.assertIsNotNone(holiday, month_day)
            self.assertEqual(holiday.kind, "full", month_day)
        # 24 Dec and 31 Dec are shortened working days, not days off
        self.assertEqual(self.cal.holiday_for(date(2026, 12, 24)).kind, "half")
        self.assertEqual(self.cal.holiday_for(date(2026, 12, 31)).kind, "half")

    def test_easter_and_good_friday_are_computed(self):
        self.assertEqual(workcal.easter_sunday(2026), date(2026, 4, 5))
        self.assertEqual(workcal.easter_sunday(2027), date(2027, 3, 28))
        self.assertEqual(self.cal.holiday_for(date(2026, 4, 5)).kind, "full")
        self.assertEqual(self.cal.holiday_for(date(2026, 4, 3)).kind, "full")

    def test_a_normal_day_and_a_weekend(self):
        self.assertEqual(self.cal.business_hours(None, at(2026, 3, 2, 9), at(2026, 3, 2, 17)), 8.0)
        # Friday 17:00 -> Monday 09:00 is 64 wall-clock hours and zero working hours
        self.assertEqual(self.cal.business_hours(None, at(2026, 3, 6, 17), at(2026, 3, 9, 9)), 0.0)

    def test_a_half_day_keeps_the_morning(self):
        self.assertEqual(self.cal.business_hours(None, at(2026, 12, 24, 9), at(2026, 12, 24, 17)), 4.0)
        self.assertEqual(self.cal.business_hours(None, at(2026, 12, 24, 13), at(2026, 12, 24, 17)), 0.0)

    def test_a_full_holiday_counts_nothing(self):
        self.assertEqual(self.cal.business_hours(None, at(2026, 1, 1, 9), at(2026, 1, 1, 17)), 0.0)
        self.assertEqual(self.cal.business_hours(None, at(2026, 2, 24, 9), at(2026, 2, 24, 17)), 0.0)

    def test_a_tenant_file_extends_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "del").mkdir()
            write_calendar(root / "del", """
holidays:
  - {date: 2026-12-24, type: full, name: company closes}
overrides:
  ToBr: "08:00–16:00"
vacations:
  ToBr:
    - {start: 2026-08-03, end: 2026-08-07}
""")
            merged = workcal.load(tenant="del", tenants_dir=root)
        # the tenant overrides the half-day to a full day off
        self.assertEqual(merged.holiday_for(date(2026, 12, 24)).kind, "full")
        # and keeps the committed holidays it did not mention
        self.assertEqual(merged.holiday_for(date(2026, 1, 1)).kind, "full")
        self.assertEqual(merged.overrides["ToBr"], (8 * 60, 16 * 60))
        self.assertEqual(len(merged.vacations["ToBr"]), 1)
        self.assertEqual(len(merged.sources), 2)


# --------------------------------------------------------------------------- #
# 2. the computation itself
# --------------------------------------------------------------------------- #
class BusinessHoursTests(unittest.TestCase):
    """Exact counts over a fixed calendar; each case is one PRD requirement."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = write_calendar(self.root)
        self.cal = workcal.load(config_path=self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def hours(self, person, t1, t2):
        return self.cal.business_hours(person, t1, t2)

    def test_a_normal_day_is_the_window(self):
        self.assertEqual(self.hours(None, at(2026, 3, 2, 9), at(2026, 3, 2, 17)), 8.0)

    def test_partial_days_are_clipped_to_the_window(self):
        self.assertEqual(self.hours(None, at(2026, 3, 2, 7), at(2026, 3, 2, 9)), 0.0)
        self.assertEqual(self.hours(None, at(2026, 3, 2, 16), at(2026, 3, 2, 20)), 1.0)
        self.assertEqual(self.hours(None, at(2026, 3, 2, 16), at(2026, 3, 3, 10)), 2.0)

    def test_weekends_and_nights_are_excluded(self):
        # Friday 16:00 -> Monday 10:00: 66 wall-clock hours, 2 working hours
        start, end = at(2026, 3, 6, 16), at(2026, 3, 9, 10)
        self.assertEqual((end - start).total_seconds() / 3600.0, 66.0)
        self.assertEqual(self.hours(None, start, end), 2.0)

    def test_a_full_holiday_counts_nothing(self):
        self.assertEqual(self.hours(None, at(2026, 1, 1, 9), at(2026, 1, 1, 17)), 0.0)

    def test_a_half_day_counts_half(self):
        self.assertEqual(self.hours(None, at(2026, 12, 24, 9), at(2026, 12, 24, 17)), 4.0)
        self.assertEqual(self.hours(None, at(2026, 12, 31, 9), at(2026, 12, 31, 12)), 3.0)

    def test_a_vacation_counts_nothing(self):
        # LaNo is away Mon 6 Jul - Fri 17 Jul 2026: two full working weeks
        self.assertEqual(self.hours("LaNo", at(2026, 7, 6, 9), at(2026, 7, 17, 17)), 0.0)
        # the Monday before the vacation still counts
        self.assertEqual(self.hours("LaNo", at(2026, 6, 29, 12), at(2026, 6, 29, 20)), 8.0)
        # and a colleague without a vacation works those days
        self.assertEqual(self.hours("ToBr", at(2026, 7, 6, 9), at(2026, 7, 6, 17)), 8.0)

    def test_a_per_person_override_replaces_the_window(self):
        # LaNo works 12:00-20:00, so 09:00-17:00 is five hours for them
        self.assertEqual(self.hours("LaNo", at(2026, 3, 2, 9), at(2026, 3, 2, 17)), 5.0)
        # and the evening hours count, which the default window would drop
        self.assertEqual(self.hours("LaNo", at(2026, 3, 2, 17), at(2026, 3, 2, 20)), 3.0)
        self.assertEqual(self.hours(None, at(2026, 3, 2, 17), at(2026, 3, 2, 20)), 0.0)
        self.assertEqual(self.hours("ToBr", at(2026, 3, 2, 9), at(2026, 3, 2, 17)), 8.0)

    def test_ignore_vacations_prices_the_time_the_person_would_have_worked(self):
        start, end = self.cal.day_bounds(date(2026, 7, 6), date(2026, 7, 17))
        self.assertEqual(self.hours("LaNo", start, end), 0.0)
        self.assertEqual(
            self.cal.business_hours("LaNo", start, end, ignore_vacations=True), 80.0
        )

    def test_business_days_and_vacation_list(self):
        self.assertEqual(self.cal.working_days("LaNo", date(2026, 7, 6), date(2026, 7, 17)), 0)
        self.assertEqual(
            self.cal.working_days("LaNo", date(2026, 7, 6), date(2026, 7, 17), ignore_vacations=True),
            10,
        )
        vacations = self.cal.vacation_list()
        self.assertEqual(len(vacations), 1)
        self.assertEqual(vacations[0]["person"], "LaNo")
        self.assertEqual(vacations[0]["start"], "2026-07-06")
        self.assertEqual(vacations[0]["end"], "2026-07-17")
        self.assertEqual(vacations[0]["days"], 12)
        self.assertEqual(vacations[0]["business_days"], 10)

    def test_summary_reports_the_effective_calendar(self):
        summary = self.cal.summary()
        self.assertEqual(summary["hours_mode"], "business")
        self.assertEqual(summary["timezone"], "Europe/Tallinn")
        self.assertEqual(summary["working_hours"]["default"], "09:00-17:00")
        self.assertEqual(summary["overrides"], {"LaNo": "12:00-20:00"})
        self.assertEqual(summary["holidays"]["count"], 5)   # 3 recurring + 2 easter
        self.assertEqual(summary["vacations"][0]["person"], "LaNo")

    def test_a_broken_config_names_the_problem(self):
        for text, needle in (
            ('working_hours: {default: "9 to 5"}', "working window"),
            ('timezone: Mars/Olympus', "timezone"),
            ('holidays: [{month_day: "02-30"}]', "calendar day"),
            ('holidays: [{month_day: "01-01", type: "mostly"}]', "type"),
            ('vacations: {LaNo: [{start: 2026-07-17, end: 2026-07-06}]}', "before it starts"),
        ):
            path = write_calendar(self.root, text, name="broken.yaml")
            with self.subTest(text=text):
                with self.assertRaises(workcal.CalendarError) as caught:
                    workcal.load(config_path=path)
                self.assertIn(needle, str(caught.exception))

    def test_window_parsing_accepts_the_prd_dash(self):
        self.assertEqual(workcal.parse_window("09:00–17:00"), (540, 1020))
        self.assertEqual(workcal.parse_window("09:00-17:00"), (540, 1020))
        self.assertEqual(workcal.parse_window("9-17"), (540, 1020))
        self.assertEqual(workcal.parse_window("12:00 — 20:00"), (720, 1200))
        self.assertIsNone(workcal.parse_window("off"))

    def test_no_calendar_falls_back_to_wall_clock(self):
        start, end = at(2026, 3, 6, 16), at(2026, 3, 9, 10)
        self.assertEqual(workcal.business_hours(None, None, start, end), 66.0)


# --------------------------------------------------------------------------- #
# 3. the analysis: ball-in-court, delivery, rules
# --------------------------------------------------------------------------- #
class Fixture:
    """A tiny store built from explicit events (no generator, no randomness)."""

    def __init__(self, as_of: datetime):
        self.as_of = as_of
        self.events: list[dict] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
        self._count = 0

    def pr(self, number: int, author: str, opened: datetime, state: str = "open",
           closed: datetime | None = None) -> "Fixture":
        self.opened[number] = opened
        self.objects.append({
            "object_type": "PR", "object_id": f"PR:{REPO}#{number}", "repo": REPO,
            "pr_id": number, "ticket_keys": ["ENG-1"], "actor": author,
            "state": state, "created_at": opened, "closed_at": closed,
            "head_commit": "a" * 40, "message_class": None, "attributes": {},
        })
        self.event(number, "pr_opened", author, opened)
        return self

    def event(self, number: int, activity: str, actor: str, ts: datetime,
              **attributes) -> "Fixture":
        self._count += 1
        key = f"{REPO}#{number}:{self._count}"
        self.events.append({
            "event_key": key, "ts": ts, "activity": activity, "actor": actor,
            "repo": REPO, "pr_id": number, "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        return self

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, [])
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-calendar", "source": "fixture", "as_of": self.as_of,
                "source_watermark": self.as_of, "classifier_version": "deterministic-v1",
                "prs_seen": len(self.opened), "prs_kept": len(self.opened),
                "prs_dropped": 0, "events_written": len(self.events),
                "events_skipped": 0,
            })
        finally:
            con.close()
        return path


class AnalysisCase(unittest.TestCase):
    """One temp store + one fixed calendar per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "store.duckdb"
        self.cal = workcal.load(config_path=write_calendar(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def court(self, fixture: Fixture, calendar=True):
        fixture.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            return ballcourt.load(con, calendar=self.cal if calendar else None)
        finally:
            con.close()

    def weekend_fixture(self) -> Fixture:
        """A review wait from Friday 16:00 to Monday 10:00 (66 wall-clock hours)."""
        fx = Fixture(as_of=at(2026, 3, 9, 12))
        fx.pr(1, "AaAa", at(2026, 3, 6, 16))
        fx.event(1, "comment_added", "RrRr", at(2026, 3, 9, 10), severity="suggestion")
        return fx


class BallCourtHoursTests(AnalysisCase):
    def test_intervals_switch_to_business_hours(self):
        court = self.court(self.weekend_fixture())
        self.assertEqual(court.hours_mode, "business")
        self.assertIn("business hours", court.assumption)
        review = [i for i in court.intervals if i.phase == "review"]
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0].seconds, 66 * 3600)      # wall clock is unchanged
        self.assertEqual(review[0].hours, 2.0)              # business hours

    def test_without_a_calendar_the_court_stays_wall_clock(self):
        court = self.court(self.weekend_fixture(), calendar=False)
        self.assertEqual(court.hours_mode, "wall-clock")
        review = [i for i in court.intervals if i.phase == "review"]
        self.assertEqual(review[0].hours, 66.0)
        self.assertEqual(court.business_hours(None, review[0].start, review[0].end), 66.0)

    def test_blocking_graph_and_queue_report_the_mode(self):
        court = self.court(self.weekend_fixture())
        graph = ballcourt.blocking_graph(court, level="person")
        self.assertEqual(graph["hours_mode"], "business")
        edge = next(e for e in graph["edges"] if e["to"] == "RrRr")
        self.assertEqual(edge["hours"], 2.0)
        self.assertIn("business hours", graph["assumption"])

        queues = ballcourt.queue_series(court, level="person")
        self.assertEqual(queues["hours_mode"], "business")
        self.assertIn("service_hours", queues["window"])
        rr = next(s for s in queues["streams"] if s["id"] == "RrRr")
        self.assertEqual(rr["summary"]["hours"], 2.0)

    def test_the_delivery_list_counts_business_hours(self):
        # an open PR that has only been opened: its wait is the whole age
        fx = Fixture(as_of=at(2026, 3, 9, 12))
        fx.pr(1, "AaAa", at(2026, 3, 6, 16))
        court = self.court(fx)
        delivery = ballcourt.delivery_list(court)
        self.assertEqual(delivery["hours_mode"], "business")
        row = delivery["rows"][0]
        # Friday 16:00 -> Monday 12:00 is 68 wall-clock hours, 4 working hours
        wall = (court.snapshot - at(2026, 3, 6, 16)).total_seconds() / 3600.0
        self.assertEqual(wall, 68.0)
        self.assertEqual(row["waiting_hours"], 4.0)
        self.assertEqual(row["age_hours"], 4.0)


class RulesHoursTests(AnalysisCase):
    def test_time_to_first_review_uses_business_hours(self):
        court = self.court(self.weekend_fixture())
        window = rules.window_for(court)
        metric = rules.time_to_first_review(court, window)
        self.assertEqual(metric["median_hours"], 2.0)     # 66 wall-clock hours
        self.assertEqual(metric["over_target"], 0)

    def test_insights_report_the_mode(self):
        court = self.court(self.weekend_fixture())
        window = rules.window_for(court)
        payload = rules.insights(court, [], window)
        self.assertEqual(payload["hours_mode"], "business")
        self.assertIn("business hours", payload["assumption"])
        self.assertIn("business", payload["note"])

    def test_without_a_calendar_the_insights_stay_wall_clock(self):
        court = self.court(self.weekend_fixture(), calendar=False)
        window = rules.window_for(court)
        payload = rules.insights(court, [], window)
        self.assertEqual(payload["hours_mode"], "wall-clock")
        self.assertEqual(payload["metrics"]["time_to_first_review"]["median_hours"], 66.0)
        self.assertFalse(payload["metrics"]["vacation_blocking"]["available"])


class VacationBlockingTests(AnalysisCase):
    """LaNo's PR waits on reviewer RrRr, who is away 6-17 July 2026."""

    def vacation_fixture(self, delegated: bool = False) -> Fixture:
        fx = Fixture(as_of=at(2026, 7, 20, 12))
        fx.pr(1, "AaAa", at(2026, 7, 3, 9))
        fx.event(1, "pr_pushed", "AaAa", at(2026, 7, 3, 16), push_class="content")
        if delegated:
            # YyYy picks the PR up during the vacation: the ball moves
            fx.event(1, "comment_added", "YyYy", at(2026, 7, 8, 10),
                     severity="suggestion")
            fx.event(1, "pr_approved", "RrRr", at(2026, 7, 20, 10))
        else:
            fx.event(1, "pr_approved", "RrRr", at(2026, 7, 20, 10))
        return fx

    def test_a_vacation_blocks_an_item_with_no_delegation(self):
        # RrRr's vacation comes from a tenant-style override in the calendar
        self.cal = workcal.load(config_path=write_calendar(self.root, DEFAULT_YAML.replace(
            "LaNo:\n    - {start: 2026-07-06, end: 2026-07-17}",
            "RrRr:\n    - {start: 2026-07-06, end: 2026-07-17}",
        )))
        court = self.court(self.vacation_fixture())
        metric = rules.vacation_blocking(court, rules.window_for(court))
        self.assertTrue(metric["available"])
        self.assertEqual(metric["n"], 1)
        self.assertEqual(metric["blocked_items"], 1)
        self.assertEqual(metric["delegated_items"], 0)
        # the ten working days of the vacation, at 8 h each
        self.assertEqual(metric["blocked_hours"], 80.0)
        vacation = metric["vacations"][0]
        self.assertEqual(vacation["person"], "RrRr")
        self.assertEqual(vacation["business_days"], 10)

    def test_delegation_clears_the_block(self):
        self.cal = workcal.load(config_path=write_calendar(self.root, DEFAULT_YAML.replace(
            "LaNo:\n    - {start: 2026-07-06, end: 2026-07-17}",
            "RrRr:\n    - {start: 2026-07-06, end: 2026-07-17}",
        )))
        court = self.court(self.vacation_fixture(delegated=True))
        metric = rules.vacation_blocking(court, rules.window_for(court))
        self.assertEqual(metric["delegated_items"], 1)
        self.assertEqual(metric["blocked_items"], 0)
        self.assertEqual(metric["blocked_hours"], 0.0)
        payload = rules.insights(court, [], rules.window_for(court))
        self.assertIsNone(next(
            (s for s in payload["suggestions"] if s["metric"] == "vacation_blocking"), None
        ))

    def test_the_insight_names_the_period_and_the_cost(self):
        self.cal = workcal.load(config_path=write_calendar(self.root, DEFAULT_YAML.replace(
            "LaNo:\n    - {start: 2026-07-06, end: 2026-07-17}",
            "RrRr:\n    - {start: 2026-07-06, end: 2026-07-17}",
        )))
        court = self.court(self.vacation_fixture())
        window = rules.window_for(court)
        team = rules.insights(court, [], window, level="team")
        suggestion = next(s for s in team["suggestions"] if s["id"] == "vacation-blocking")
        self.assertEqual(suggestion["metric"], "vacation_blocking")
        self.assertEqual(suggestion["severity"], "critical")     # 80 h >= 40 h
        self.assertEqual(suggestion["impact_hours"], 80.0)
        self.assertEqual(suggestion["evidence"]["blocked_items"], 1)
        self.assertEqual(suggestion["evidence"]["vacations"], 1)
        # team level does not name the person
        self.assertNotIn("RrRr", json.dumps(suggestion))
        self.assertIn("delegat", suggestion["suggestion"])

        person = rules.insights(court, [], window, level="person")
        named = next(
            s for s in person["suggestions"] if s["metric"] == "vacation_blocking"
        )
        self.assertEqual(named["id"], "vacation-RrRr-2026-07-06")
        self.assertEqual(named["scope"], {"kind": "person", "person": "RrRr"})
        self.assertIn("RrRr on vacation 2026-07-06 → 2026-07-17", named["title"])
        self.assertIn("1 item waited", named["title"])


# --------------------------------------------------------------------------- #
# 4. the API (real ASGI app, temp tenant directory)
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
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": query.encode(), "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
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


class CalendarApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        fixture = Fixture(as_of=at(2026, 3, 9, 12))
        fixture.pr(1, "AaAa", at(2026, 3, 6, 16))
        fixture.event(1, "comment_added", "RrRr", at(2026, 3, 9, 10), severity="suggestion")
        fixture.write(cls.dir / "del.duckdb")
        # a per-tenant calendar: an override and a vacation
        (cls.dir / "del").mkdir()
        write_calendar(cls.dir / "del", """
overrides:
  RrRr: "12:00–20:00"
vacations:
  RrRr:
    - {start: 2026-02-23, end: 2026-02-27}
""")
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
        app_module._calendar_cache.clear()

    def test_the_effective_calendar_is_inspectable(self):
        body = get("/api/calendar", tenant="del").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tenant"], "del")
        self.assertEqual(body["hours_mode"], "business")
        self.assertEqual(body["timezone"], "Europe/Tallinn")
        self.assertEqual(body["overrides"]["RrRr"], "12:00-20:00")
        self.assertEqual(body["vacations"][0]["person"], "RrRr")
        self.assertIn("uusaasta", json.dumps(body["holidays"]["recurring"]))
        self.assertEqual(len(body["sources"]), 2)

    def test_health_reports_the_mode(self):
        body = get("/api/health", tenant="del").json()
        self.assertEqual(body["calendar"]["hours_mode"], "business")
        self.assertEqual(body["calendar"]["timezone"], "Europe/Tallinn")

    def test_every_analysis_view_says_business(self):
        for path in ("/api/blocking", "/api/queues", "/api/delivery", "/api/insights"):
            with self.subTest(path=path):
                body = get(path, tenant="del", days=3650).json()
                self.assertEqual(body["hours_mode"], "business")
                self.assertIn("business hours", body["assumption"])
                self.assertNotIn("calendar_error", body)

    def test_the_per_person_override_changes_the_wait(self):
        # RrRr works 12:00-20:00: Friday 16:00-20:00 is four working hours (the
        # default 09:00-17:00 window would count one) and Monday 09:00-10:00 is
        # outside their window, so the Friday-to-Monday review wait is 4 h, not 2.
        body = get("/api/insights", tenant="del", days=3650).json()
        self.assertEqual(body["metrics"]["time_to_first_review"]["median_hours"], 4.0)

    def test_no_calendar_config_falls_back_to_wall_clock(self):
        with mock.patch.object(app_module, "CALENDAR_CONFIG", self.dir / "missing.yaml"):
            app_module._analysis_cache = None
            app_module._calendar_cache.clear()
            calendar_reply = get("/api/calendar", tenant="del").json()
            insights = get("/api/insights", tenant="del", days=3650).json()
        self.assertFalse(calendar_reply["ok"])
        self.assertEqual(calendar_reply["hours_mode"], "wall-clock")
        self.assertEqual(insights["hours_mode"], "wall-clock")
        self.assertEqual(
            insights["metrics"]["time_to_first_review"]["median_hours"], 66.0
        )

    def test_a_broken_tenant_calendar_is_reported_not_fatal(self):
        broken = write_calendar(self.dir / "del", "working_hours: {default: nope}")
        try:
            app_module._analysis_cache = None
            app_module._calendar_cache.clear()
            reply = get("/api/calendar", tenant="del").json()
            insights = get("/api/insights", tenant="del", days=3650).json()
        finally:
            broken.unlink()
            write_calendar(self.dir / "del", """
overrides:
  RrRr: "12:00–20:00"
vacations:
  RrRr:
    - {start: 2026-02-23, end: 2026-02-27}
""")
            app_module._analysis_cache = None
            app_module._calendar_cache.clear()
        self.assertFalse(reply["ok"])
        self.assertIn("working window", reply["error"])
        # the analysis still answers, honestly labelled, and carries the reason
        self.assertEqual(insights["hours_mode"], "wall-clock")
        self.assertIn("working window", insights["calendar_error"])

    def test_an_unknown_tenant_is_a_clean_404(self):
        reply = get("/api/calendar", tenant="nope")
        self.assertEqual(reply.status, 404)
        self.assertEqual(reply.json()["valid"], ["del"])

    def test_an_unknown_parameter_is_rejected(self):
        reply = get("/api/calendar", tenant="del", tz="UTC")
        self.assertEqual(reply.status, 400)
        self.assertIn("tz", reply.json()["error"])


if __name__ == "__main__":
    unittest.main()

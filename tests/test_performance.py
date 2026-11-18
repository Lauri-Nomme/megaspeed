"""Performance regressions from the adversarial review (prd/33).

Each fix in the review's ranked list is pinned by the *behaviour* it must keep,
not only by a speed number, so a future rewrite can move the milliseconds again
without changing an answer:

* **#2** ``queue_series`` WIP sampling — the difference-array rewrite must
  reproduce the naive per-interval/per-bucket series exactly, and a large
  bucket count must no longer cost O(intervals × buckets) wall time.
* **#4** ``business_hours`` and ``_cycle`` memoisation — a memoised answer must
  equal a freshly computed one, the memo must actually be consulted, and the
  memo key must keep ``ignore_vacations`` apart. A store reload (new ``Story``
  objects) and a calendar-version change (a new ``Calendar``, see
  ``test_server``) must recompute rather than answer from the old cache.
* **#9** the bulk ``/api/events`` relation lookup — the single ``JOIN`` must
  return the chunked ``IN`` result set it replaced, for every key, in the same
  order, including with several chunks and unknown keys.

The stores here are synthetic and small: the point is equivalence, and the one
timing assertion is a generous "does not blow up" guard, not a benchmark.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from analysis import ballcourt, calendar as workcal, jira
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from server import query

REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = datetime(2026, 3, 1, 12, 0)


# --------------------------------------------------------------------------- #
# a synthetic store: PRs with review waits, plus arbitrary event → object links
# --------------------------------------------------------------------------- #
class Bench:
    """A small explicit store, written through the real schema/write path."""

    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
        self._count = 0

    def pr(self, number: int, author: str = "AaAa", opened: datetime = T0,
           state: str = "open", closed: datetime | None = None,
           tickets: tuple[str, ...] = ("ENG-1",)) -> "Bench":
        self.opened[number] = opened
        self.objects.append({
            "object_type": "PR",
            "object_id": f"PR:{REPO}#{number}",
            "repo": REPO,
            "pr_id": number,
            "ticket_keys": list(tickets),
            "actor": author,
            "state": state,
            "created_at": opened,
            "closed_at": closed,
            "head_commit": "a" * 40,
            "message_class": None,
            "attributes": {},
        })
        return self.event(number, "pr_opened", author, 0)

    def event(self, number: int, activity: str, actor: str, hours: float,
              links: tuple[tuple[str, str], ...] = (), **attributes) -> "Bench":
        self._count += 1
        ts = self.opened[number] + timedelta(hours=hours)
        key = f"{REPO}#{number}:{self._count}"
        self.events.append({
            "event_key": key,
            "ts": ts,
            "activity": activity,
            "actor": actor,
            "repo": REPO,
            "pr_id": number,
            "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        for object_type, object_id in links:
            self.relations.append((key, object_type, object_id))
        return self

    def review(self, number: int, reviewer: str, hours: float) -> "Bench":
        """A reviewer-roster addition, linked to the reviewer's person object."""
        return self.event(
            number, "reviewer_changed", "AaAa", hours, change="added",
            links=(("PERSON", f"PERSON:{reviewer}"),),
        )

    def approve(self, number: int, reviewer: str, hours: float) -> "Bench":
        return self.event(
            number, "pr_approved", reviewer, hours,
            links=(("PERSON", f"PERSON:{reviewer}"),),
        )

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-performance",
                "source": "fixture",
                "as_of": self.as_of,
                "source_watermark": self.as_of,
                "classifier_version": "deterministic-v1",
                "prs_seen": len(self.opened),
                "prs_kept": len(self.opened),
                "prs_dropped": 0,
                "events_written": len(self.events),
                "events_skipped": 0,
            })
        finally:
            con.close()
        return path


def review_store(prs: int = 40) -> Bench:
    """``prs`` PRs, each waiting on two reviewers before an approval and merge."""
    store = Bench()
    for number in range(1, prs + 1):
        opened = T0 + timedelta(hours=6 * number)
        store.pr(number, "AaAa", opened)
        store.review(number, "RrRr", 1)
        store.review(number, "SsSs", 2)
        store.approve(number, "RrRr", 5 + number % 7)
        store.event(number, "pr_merged", "RrRr", 20 + number % 11)
        store.objects[number - 1]["state"] = "merged"
        store.objects[number - 1]["closed_at"] = opened + timedelta(hours=20)
    return store


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def court(self, store: Bench, calendar=None) -> ballcourt.BallCourt:
        path = store.write(self.path)
        con = connect(path, read_only=True)
        try:
            return ballcourt.load(con, calendar=calendar)
        finally:
            con.close()

    def connect(self, store: Bench):
        path = store.write(self.path)
        return connect(path, read_only=True)


# --------------------------------------------------------------------------- #
# #2 queue_series: difference array, byte-identical series
# --------------------------------------------------------------------------- #
def naive_wip(court: ballcourt.BallCourt, level: str, start: datetime,
              end: datetime, bucket_hours: float, buckets: int) -> dict[str, list[int]]:
    """The pre-prd/33 sampling loop: every interval against every bucket."""
    prs = {state.pr["object_id"]: state.pr for state in court.prs}
    series: dict[str, list[int]] = {}
    for interval in court.intervals:
        if not (interval.measured and interval.start < end and interval.end > start):
            continue
        if level == "person":
            people = interval.holders
        else:
            people = (interval.waited_on,) if interval.waited_on else ()
        for person in people:
            if not person:
                continue
            if level == "person":
                name = person
            else:
                name = ballcourt._queue_of(
                    court, person, prs.get(interval.object_id) or {}, "ball"
                )
            wip = series.setdefault(name, [0] * buckets)
            for b in range(buckets):
                at = start + timedelta(hours=(b + 0.5) * bucket_hours)
                if interval.start <= at < interval.end:
                    wip[b] += 1
    return series


class QueueSeriesEquivalenceTests(StoreCase):
    """The difference array must be invisible in the payload."""

    def setUp(self):
        super().setUp()
        self.store = review_store(prs=40)
        self.bc = self.court(self.store)

    def assert_same_wip(self, level: str, bucket_hours: float) -> dict:
        start = self.bc.intervals[0].start - timedelta(hours=3)
        end = self.bc.snapshot
        payload = ballcourt.queue_series(
            self.bc, level=level, window=(start, end), bucket_hours=bucket_hours
        )
        buckets = payload["buckets"]
        got = {stream["id"]: stream["wip"] for stream in payload["streams"]}
        want = naive_wip(self.bc, level, start, end, bucket_hours, buckets)
        self.assertTrue(want, "the fixture has no measured interval")
        for name in set(got) | set(want):
            self.assertEqual(
                got.get(name, [0] * buckets),
                want.get(name, [0] * buckets),
                f"{level} @ {bucket_hours}h stream {name}",
            )
        # non-vacuous: some bucket really holds a queued item
        self.assertGreater(max(max(series, default=0) for series in want.values()), 0)
        return payload

    def test_the_series_matches_the_naive_loop_at_every_bucket_width(self):
        for bucket_hours in (1.0, 7.0, 24.0, 100.0):
            for level in ("person", "queue"):
                with self.subTest(level=level, bucket_hours=bucket_hours):
                    self.assert_same_wip(level, bucket_hours)

    def test_the_default_window_matches_too(self):
        for level in ("person", "queue"):
            with self.subTest(level=level):
                payload = ballcourt.queue_series(self.bc, level=level)
                start = self.bc.intervals[0].start
                end = self.bc.snapshot
                buckets = payload["buckets"]
                got = {stream["id"]: stream["wip"] for stream in payload["streams"]}
                want = naive_wip(
                    self.bc, level, start, end, payload["bucket_hours"], buckets
                )
                for name in set(got) | set(want):
                    self.assertEqual(
                        got.get(name, [0] * buckets), want.get(name, [0] * buckets), name
                    )

    def test_the_window_edges_still_clip_the_series(self):
        # a window that starts and ends inside the measured intervals: only the
        # midpoints inside [start, end) count, exactly as the naive loop decides
        interval = self.bc.intervals[0]
        start = interval.start + timedelta(minutes=30)
        end = start + timedelta(hours=5)
        payload = ballcourt.queue_series(
            self.bc, level="person", window=(start, end), bucket_hours=1.0
        )
        buckets = payload["buckets"]
        got = {stream["id"]: stream["wip"] for stream in payload["streams"]}
        want = naive_wip(self.bc, "person", start, end, 1.0, buckets)
        for name in set(got) | set(want):
            self.assertEqual(got.get(name, [0] * buckets), want.get(name, [0] * buckets), name)


class QueueSeriesScaleTests(unittest.TestCase):
    """A large bucket count must not multiply by the interval count."""

    @staticmethod
    def big_court(intervals: int = 400, people: int = 8) -> ballcourt.BallCourt:
        """``intervals`` measured review waits over ``people`` reviewers."""
        start = T0
        rows: list[ballcourt.Interval] = []
        states: list[ballcourt.PRState] = []
        for index in range(intervals):
            begin = start + timedelta(hours=index * 7)
            end = begin + timedelta(hours=5)
            object_id = f"PR:{REPO}#{index + 1}"
            pr = {
                "object_id": object_id,
                "repo": REPO,
                "pr_id": index + 1,
                "ticket_keys": ["ENG-1"],
                "actor": "AaAa",
                "state": "open",
                "created_ts": begin,
                "closed_ts": None,
            }
            rows.append(ballcourt.Interval(
                object_id=object_id, repo=REPO, number=index + 1,
                tickets=("ENG-1",), author="AaAa", start=begin, end=end,
                phase="review", ball="review",
                ball_person=f"R{index % people:02d}", by="AaAa",
                kind="review_requested", evidence="none", commits=0, open=True,
                ball_people=(f"R{index % people:02d}",),
            ))
            states.append(ballcourt.PRState(pr=pr, events=[], commits=[], intervals=[rows[-1]]))
        return ballcourt.BallCourt(
            intervals=rows, prs=states,
            snapshot=start + timedelta(days=365 * 3), sentinels=set(), events=intervals,
        )

    def test_ten_years_of_hourly_buckets_finish_quickly(self):
        court = self.big_court()
        start = court.intervals[0].start
        end = start + timedelta(days=3650)
        began = time.perf_counter()
        payload = ballcourt.queue_series(court, "person", (start, end), 1.0)
        elapsed = time.perf_counter() - began
        # 400 intervals × 87,601 buckets was ~35M Python iterations before the
        # rewrite (tens of seconds); the difference array is O(intervals +
        # buckets). 5 s is a generous ceiling for a loaded CI box, and still
        # fails loudly if the product comes back.
        self.assertGreaterEqual(payload["buckets"], 87_000)
        self.assertLess(elapsed, 5.0, f"queue_series took {elapsed:.1f}s")
        # and it still answers: every 5-hour interval covers five hourly buckets
        totals = {stream["id"]: sum(stream["wip"]) for stream in payload["streams"]}
        self.assertEqual(sum(totals.values()), 5 * 400)

    def test_the_series_is_still_the_naive_one_at_scale(self):
        court = self.big_court(intervals=40)
        start = court.intervals[0].start
        end = start + timedelta(days=400)
        payload = ballcourt.queue_series(court, "person", (start, end), 6.0)
        buckets = payload["buckets"]
        got = {stream["id"]: stream["wip"] for stream in payload["streams"]}
        want = naive_wip(court, "person", start, end, 6.0, buckets)
        for name in set(got) | set(want):
            self.assertEqual(got.get(name, [0] * buckets), want.get(name, [0] * buckets), name)


# --------------------------------------------------------------------------- #
# #4 memoisation: business_hours
# --------------------------------------------------------------------------- #
CALENDAR_YAML = """
timezone: UTC
weekdays: [1, 2, 3, 4, 5]
working_hours:
  default: "09:00-17:00"
vacations:
  AaAa:
    - {start: 2026-03-03, end: 2026-03-03}
"""


class BusinessHoursMemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "default.yaml").write_text(CALENDAR_YAML, encoding="utf-8")
        tenant = self.dir / "t"
        tenant.mkdir()
        (tenant / workcal.TENANT_FILE).write_text(
            "working_hours:\n  default: \"10:00-14:00\"\n", encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def calendar(self, tenant: str | None = None) -> workcal.Calendar:
        cal = workcal.load(tenant=tenant, tenants_dir=self.dir,
                           config_path=self.dir / "default.yaml")
        self.assertIsNotNone(cal)
        return cal

    def test_a_repeated_question_is_answered_from_the_memo(self):
        cal = self.calendar()
        fresh = self.calendar()  # same config, no memo
        t1 = datetime(2026, 3, 2, 9)          # Monday
        t2 = datetime(2026, 3, 6, 17)         # Friday
        with mock.patch.object(cal, "_local", side_effect=cal._local) as spy:
            first = cal.business_hours("AaAa", t1, t2)
            second = cal.business_hours("AaAa", t1, t2)
        self.assertEqual(first, second)
        self.assertEqual(first, fresh.business_hours("AaAa", t1, t2))
        # one computation converts its two bounds; a second walk would add two
        self.assertEqual(spy.call_count, 2)

    def test_the_memo_key_keeps_ignore_vacations_apart(self):
        cal = self.calendar()
        t1 = datetime(2026, 3, 2, 9)          # Monday
        t2 = datetime(2026, 3, 4, 17)         # Wednesday, Tuesday is a vacation
        with_vacation = cal.business_hours("AaAa", t1, t2)
        without = cal.business_hours("AaAa", t1, t2, ignore_vacations=True)
        self.assertEqual(with_vacation, 16.0)
        self.assertEqual(without, 24.0)
        # and both are memoised separately
        self.assertEqual(cal.business_hours("AaAa", t1, t2), 16.0)
        self.assertEqual(cal.business_hours("AaAa", t1, t2, ignore_vacations=True), 24.0)

    def test_degenerate_ranges_answer_zero(self):
        cal = self.calendar()
        monday = datetime(2026, 3, 2, 9)
        self.assertEqual(cal.business_hours("AaAa", monday, monday), 0.0)
        self.assertEqual(cal.business_hours("AaAa", monday, monday - timedelta(hours=1)), 0.0)
        self.assertEqual(cal.business_hours(None, monday, monday + timedelta(hours=8)), 8.0)

    def test_a_legitimate_zero_is_a_value_and_is_reused(self):
        # 0.0 must not read as "not cached": the weekend answer is memoised too
        cal = self.calendar()
        saturday = datetime(2026, 3, 7, 9)
        sunday = datetime(2026, 3, 8, 17)
        self.assertEqual(cal.business_hours("AaAa", saturday, sunday), 0.0)
        with mock.patch.object(cal, "_local", side_effect=cal._local) as spy:
            self.assertEqual(cal.business_hours("AaAa", saturday, sunday), 0.0)
        self.assertEqual(spy.call_count, 0)

    def test_a_new_calendar_version_starts_with_an_empty_memo(self):
        before = self.calendar()
        monday = datetime(2026, 3, 2, 9)
        self.assertEqual(before.business_hours(None, monday, monday + timedelta(hours=8)), 8.0)
        self.assertTrue(before._hours_cache)
        after = self.calendar(tenant="t")     # the tenant window wins: 4 h/day
        self.assertFalse(after._hours_cache)
        self.assertEqual(after.business_hours(None, monday, monday + timedelta(hours=8)), 4.0)


# --------------------------------------------------------------------------- #
# #4 memoisation: the per-story cycle
# --------------------------------------------------------------------------- #
def make_story() -> jira.Story:
    """One story with the prd/21 boundaries, built the way ``jira.load`` builds it."""
    story = jira.Story(
        object_id="STORY:ENG-1", key="ENG-1", actor="AaAa", state="done",
        created=T0, closed=T0 + timedelta(hours=90), points=3,
        epic_key="ENG-1", summary_class="feature",
    )
    story.events = [
        {"key": "e1", "ts": T0, "_ts": T0, "activity": "created",
         "actor": "AaAa", "attributes": {}},
        {"key": "e2", "ts": T0 + timedelta(hours=10), "_ts": T0 + timedelta(hours=10),
         "activity": "pointed", "actor": "AaAa", "attributes": {"points": 3}},
        {"key": "e3", "ts": T0 + timedelta(hours=20), "_ts": T0 + timedelta(hours=20),
         "activity": "sprint_added", "actor": "AaAa", "attributes": {"sprint_id": 1}},
        {"key": "e4", "ts": T0 + timedelta(hours=30), "_ts": T0 + timedelta(hours=30),
         "activity": "status_changed", "actor": "AaAa",
         "attributes": {"from_status": "backlog", "to_status": "in_progress"}},
        {"key": "e5", "ts": T0 + timedelta(hours=25), "_ts": T0 + timedelta(hours=25),
         "activity": "worklogged", "actor": "AaAa",
         "attributes": {"time_spent_seconds": 4 * 3600, "log_delay_seconds": 3600}},
        {"key": "e6", "ts": T0 + timedelta(hours=90), "_ts": T0 + timedelta(hours=90),
         "activity": "status_changed", "actor": "AaAa",
         "attributes": {"from_status": "in_progress", "to_status": "done"}},
    ]
    return story


class CycleMemoTests(unittest.TestCase):
    def test_the_cycle_is_derived_once_and_reused(self):
        story = make_story()
        first = jira.cycle_for(story)
        self.assertIs(story._cycle_cache, first)
        # a second request does no derivation at all, not merely an equal answer
        with mock.patch.object(
            jira, "_first_event_at", side_effect=AssertionError("cycle recomputed")
        ), mock.patch.object(
            jira, "_first_status", side_effect=AssertionError("cycle recomputed")
        ):
            self.assertIs(jira.cycle_for(story), first)

    def test_a_cached_cycle_equals_a_fresh_one(self):
        cached = jira.cycle_for(make_story())
        fresh = jira.cycle_for(make_story())   # a reloaded store: new Story object
        self.assertIsNot(cached, fresh)
        self.assertEqual(cached.person, fresh.person)
        self.assertEqual(cached.boundaries, fresh.boundaries)
        self.assertEqual(cached.spans, fresh.spans)
        self.assertEqual(cached.touches, fresh.touches)
        self.assertEqual(cached.review_rounds, fresh.review_rounds)
        self.assertEqual(cached.missing, fresh.missing)

    def test_the_cache_is_not_part_of_a_storys_identity(self):
        one, two = make_story(), make_story()
        self.assertEqual(one, two)
        jira.cycle_for(one)
        self.assertEqual(one, two)
        self.assertIsNot(one._cycle_cache, None)

    def test_a_store_version_change_recomputes(self):
        story = make_story()
        before = jira.cycle_for(story)
        # what ``jira.load`` returns after the store gained a review status
        reloaded = make_story()
        reloaded.events = story.events + [{
            "key": "e7", "ts": T0 + timedelta(hours=60), "_ts": T0 + timedelta(hours=60),
            "activity": "status_changed", "actor": "AaAa",
            "attributes": {"from_status": "in_progress", "to_status": "in_review"},
        }]
        after = jira.cycle_for(reloaded)
        self.assertIsNot(after, before)
        self.assertIsNone(before.boundaries["in_review"])
        self.assertEqual(after.boundaries["in_review"], T0 + timedelta(hours=60))
        # the old version keeps its own answer: no cross-version bleed
        self.assertIs(jira.cycle_for(story), before)
        self.assertIsNone(before.boundaries["in_review"])


# --------------------------------------------------------------------------- #
# #9 bulk relation lookup: one JOIN, the same result set
# --------------------------------------------------------------------------- #
def relations_chunked(con, keys: list[str], chunk: int = 500) -> dict[str, list[str]]:
    """The pre-prd/33 chunked ``IN`` lookup, kept as the reference."""
    rel: dict[str, list[str]] = {key: [] for key in keys}
    if not keys:
        return rel
    for start in range(0, len(keys), chunk):
        part = keys[start : start + chunk]
        placeholders = ",".join("?" * len(part))
        for key, _otype, oid in con.execute(
            "SELECT event_key, object_type, object_id FROM event_objects"
            f" WHERE event_key IN ({placeholders}) ORDER BY object_type, object_id",
            part,
        ).fetchall():
            rel[key].append(oid)
    return rel


class RelationLookupTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.store = review_store(prs=40)
        # a PR whose event links several objects, so a list has > 1 entry
        self.store.event(
            1, "comment_added", "RrRr", 3,
            links=(("PERSON", "PERSON:RrRr"), ("THREAD", "THREAD:abc"),
                   ("PERSON", "PERSON:AaAa")),
        )
        self.con = self.connect(self.store)
        self.keys = [
            row[0] for row in self.con.execute(
                "SELECT event_key FROM events ORDER BY ts, event_key"
            ).fetchall()
        ]

    def tearDown(self):
        self.con.close()
        super().tearDown()

    def test_the_join_returns_the_chunked_in_result_set(self):
        wanted = self.keys + ["no-such-key"]
        want = relations_chunked(self.con, wanted)
        got = query._relations(self.con, wanted)
        self.assertEqual(got, want)
        self.assertTrue(any(len(ids) > 1 for ids in want.values()), "no multi-link row")

    def test_a_repeated_key_is_not_counted_twice(self):
        # ``list_events`` passes unique keys; the JOIN dedupes its values
        # relation, so a caller that repeats one still gets one list.
        one = query._relations(self.con, [self.keys[0]])
        many = query._relations(self.con, [self.keys[0]] * 3)
        self.assertEqual(many, one)

    def test_several_chunks_agree_with_one(self):
        want = relations_chunked(self.con, self.keys)
        with mock.patch.object(query, "RELATION_CHUNK", 3):
            self.assertEqual(query._relations(self.con, self.keys), want)
        with mock.patch.object(query, "RELATION_CHUNK", 1):
            self.assertEqual(query._relations(self.con, self.keys), want)

    def test_no_keys_is_an_empty_answer(self):
        self.assertEqual(query._relations(self.con, []), {})

    def test_list_events_keeps_every_rows_object_list(self):
        rows, total = query.list_events(self.con, None, None, None, 0, "asc")
        self.assertEqual(len(rows), total)
        want = relations_chunked(self.con, [row["key"] for row in rows])
        for row in rows:
            self.assertEqual(row["objects"], want[row["key"]], row["key"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

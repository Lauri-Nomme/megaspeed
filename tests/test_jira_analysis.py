"""Jira-layer analysis (prd/21): segments, carry-over, churn, worklogs, conformance.

Every metric is pinned against a **synthetic store with hand-computed
timestamps** — no fixture generator, no randomness — so a change in a boundary,
a share or an accounting rule fails with the exact number that moved. The
fixture below is deliberately small: one story with eight known boundaries is
enough to assert all eight segments, because the decomposition is additive.

The last class runs the *real* pipelines (Bitbucket fixture + Jira fixture) into
one store, which is the only way to prove the ticket→PR join: the story key on
the PR object is the join key, and both sides have to resolve it to the same
ticket.

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

from analysis import ballcourt, calendar, jira, rules
from ingest.store import connect, ensure_schema, verify, write_events, write_objects, write_run
from server import app as app_module

REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)
AS_OF = datetime(2026, 3, 1, 12, 0)


# --------------------------------------------------------------------------- #
# a synthetic store: explicit stories, sprints, worklogs and PRs
# --------------------------------------------------------------------------- #
class Fixture:
    """Build a store from explicit Jira objects/events plus optional PRs."""

    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
        self._count = 0

    # -- objects ------------------------------------------------------------ #
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
        object_id = f"SPRINT:ENG#{sid}"
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
                "epic_key": "ENG-1",
                **({"points": points} if points is not None else {}),
            },
        )
        self.event(key, created, "created", actor=assignee)
        return object_id

    # -- Jira events -------------------------------------------------------- #
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
               from_status: str = "todo") -> None:
        self.event(key, ts, "status_changed",
                   **{"from_status": from_status, "to_status": to_status})

    def add_to_sprint(self, key: str, ts: datetime, sid: int,
                      at_start: bool = False) -> None:
        self.event(
            key, ts, "sprint_added", objects=[f"STORY:{key}", f"SPRINT:ENG#{sid}"],
            sprint_id=sid, **({"at_start": True} if at_start else {}),
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

    # -- PRs ---------------------------------------------------------------- #
    def pr(self, number: int, author: str, opened: datetime,
           tickets: tuple[str, ...] = ("ENG-1",), state: str = "merged",
           closed: datetime | None = None) -> "Fixture":
        self.opened[number] = opened
        self._object(
            "PR", f"PR:{REPO}#{number}", repo=REPO, pr_id=number,
            ticket_keys=list(tickets), actor=author, state=state,
            created_at=opened, closed_at=closed, head_commit="a" * 40,
        )
        self.pr_event(number, "pr_opened", author, 0)
        return self

    def pr_event(self, number: int, activity: str, actor: str, hours: float,
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
        return self

    # -- materialise -------------------------------------------------------- #
    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-jira-analysis",
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


def balanced_fixture() -> Fixture:
    """One story, eight boundaries 10 h apart, and a merged PR in the middle.

    created +0 · pointed +10 · sprint_added +20 · in progress +30 · PR open +40 ·
    first review +50 · push +60 · approval +70 · merge +80 · done +90
    """
    fx = Fixture()
    fx.sprint(1, T0, end=T0 + timedelta(days=14), closed=T0 + timedelta(days=14))
    fx.story("ENG-1", T0, points=3, state="done", closed=T0 + timedelta(hours=90))
    fx.pointed("ENG-1", T0 + timedelta(hours=10), 3)
    fx.add_to_sprint("ENG-1", T0 + timedelta(hours=20), 1)
    fx.status("ENG-1", T0 + timedelta(hours=30), "in_progress")
    fx.status("ENG-1", T0 + timedelta(hours=90), "done", from_status="in_progress")
    # 4 h of work logged inside the sprint→in-progress segment (started +24)
    fx.worklog("ENG-1", T0 + timedelta(hours=25), 4.0, delay_hours=1.0,
               bucket="same_day")
    fx.pr(1, "AaAa", T0 + timedelta(hours=40), closed=T0 + timedelta(hours=80))
    fx.pr_event(1, "comment_added", "RrRr", 10, severity="suggestion")
    fx.pr_event(1, "pr_pushed", "AaAa", 20, push_class="content")
    fx.pr_event(1, "pr_approved", "RrRr", 30)
    fx.pr_event(1, "pr_merged", "RrRr", 40)
    return fx


class StoreCase(unittest.TestCase):
    """One temp store per test; load it the way the API does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fixture: Fixture, days: float | None = None):
        fixture.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            data = jira.load(con, court)
            window = rules.window_for(court, days, data)
            threads = rules.load_threads(con)
            bots = rules.load_bots(con)
            sizes = rules.load_pr_sizes(con)
            return court, data, window, threads, bots, sizes
        finally:
            con.close()

    def metric(self, fixture: Fixture, name: str, days: float | None = None):
        court, data, window, threads, _bots, sizes = self.load(fixture, days)
        return rules.metrics(court, threads, window, sizes, data)[name]

    def payload(self, fixture: Fixture, level: str = "team", days: float | None = None):
        court, data, window, threads, bots, sizes = self.load(fixture, days)
        return rules.insights(
            court, threads, window, level=level, bots=bots, sizes=sizes, jira=data
        )


def by_id(payload: dict, suggestion_id: str) -> dict | None:
    return next((s for s in payload["suggestions"] if s["id"] == suggestion_id), None)


# --------------------------------------------------------------------------- #
# 1. waiting-time decomposition (prd/21 §1)
# --------------------------------------------------------------------------- #
class WaitingTimeTests(StoreCase):
    def test_the_eight_segments_are_measured_exactly(self):
        metric = self.metric(balanced_fixture(), "waiting_time_decomposition")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["stories"], 1)
        self.assertEqual(metric["stories_decomposed"], 1)
        hours = {key: entry["hours"] for key, entry in metric["segments"].items()}
        self.assertEqual(hours, {
            "ready_refined": 10.0,
            "refined_sprint": 10.0,
            "sprint_in_progress": 10.0,
            "in_progress_pr_open": 10.0,
            "pr_open_first_review": 10.0,
            "review_rounds": 20.0,          # first review +50 → approval +70
            "final_approval_merge": 10.0,
            "merge_done": 10.0,
        })
        self.assertEqual(metric["total_hours"], 90.0)
        self.assertEqual(metric["cycle_hours"], 90.0)
        self.assertEqual(metric["explained_share"], 1.0)
        self.assertEqual(metric["largest"], "review_rounds")
        self.assertEqual(metric["largest_hours"], 20.0)
        self.assertEqual(metric["largest_share"], round(20 / 90, 4))
        self.assertEqual(metric["segments"]["review_rounds"]["share"],
                         round(20 / 90, 4))
        self.assertEqual(metric["segments"]["ready_refined"]["share"],
                         round(10 / 90, 4))
        self.assertEqual(metric["missing_boundaries"], {})
        self.assertEqual(metric["order"], list(jira.SEGMENT_KEYS))

    def test_flow_efficiency_is_per_segment(self):
        metric = self.metric(balanced_fixture(), "waiting_time_decomposition")
        segments = metric["segments"]
        # 4 h of logged work inside a 10 h sprint→in-progress wait
        self.assertEqual(segments["sprint_in_progress"]["active_hours"], 4.0)
        self.assertEqual(segments["sprint_in_progress"]["efficiency"], 0.4)
        # the push at +60 fills the first half of the review round: 10 of 20 h
        self.assertEqual(segments["review_rounds"]["active_hours"], 10.0)
        self.assertEqual(segments["review_rounds"]["efficiency"], 0.5)
        # a segment with no evidence of work is 0% — not "unknown"
        self.assertEqual(segments["ready_refined"]["active_hours"], 0.0)
        self.assertEqual(segments["ready_refined"]["efficiency"], 0.0)
        self.assertEqual(metric["clamped"], 0)

    def test_a_dominant_segment_emits_the_plain_language_template(self):
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=14))
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=80))
        fx.pointed("ENG-1", T0 + timedelta(hours=5), 3)
        fx.add_to_sprint("ENG-1", T0 + timedelta(hours=5), 1)
        fx.status("ENG-1", T0 + timedelta(hours=55), "in_progress")   # 50 h wait
        fx.status("ENG-1", T0 + timedelta(hours=80), "done", from_status="in_progress")
        fx.pr(1, "AaAa", T0 + timedelta(hours=60), closed=T0 + timedelta(hours=75))
        fx.pr_event(1, "pr_approved", "RrRr", 5)
        fx.pr_event(1, "pr_merged", "RrRr", 15)
        payload = self.payload(fx)
        metric = payload["metrics"]["waiting_time_decomposition"]
        self.assertEqual(metric["largest"], "sprint_in_progress")
        self.assertEqual(metric["largest_hours"], 50.0)
        self.assertEqual(metric["largest_share"], round(50 / 80, 4))
        suggestion = by_id(payload, "jira-waiting-time")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["metric"], "waiting_time_decomposition")
        self.assertEqual(suggestion["severity"], "critical")   # 62% ≥ 40%
        self.assertEqual(
            suggestion["title"],
            "The team spends 62% of ticket cycle time in the sprint → in progress segment",
        )
        self.assertEqual(suggestion["impact_hours"], 25.0)     # half the 50 h wait
        self.assertEqual(suggestion["evidence"]["segment"], "sprint → in progress")
        self.assertEqual(suggestion["evidence"]["stories"], 1)
        self.assertIn("Start committed work on day one", suggestion["suggestion"])

    def test_a_balanced_cycle_emits_no_waiting_rule(self):
        # the largest segment is 22% of the cycle: below the 25% target
        payload = self.payload(balanced_fixture())
        self.assertIsNone(by_id(payload, "jira-waiting-time"))

    def test_missing_boundaries_are_counted_not_guessed(self):
        fx = Fixture()
        # no pointed event and no PR: four segments have no boundary
        fx.sprint(1, T0, end=T0 + timedelta(days=14))
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=40))
        fx.add_to_sprint("ENG-1", T0 + timedelta(hours=10), 1)
        fx.status("ENG-1", T0 + timedelta(hours=20), "in_progress")
        fx.status("ENG-1", T0 + timedelta(hours=40), "done", from_status="in_progress")
        metric = self.metric(fx, "waiting_time_decomposition")
        self.assertTrue(metric["available"])                 # 2 of 8 are measured
        self.assertEqual(metric["segments"]["sprint_in_progress"]["hours"], 10.0)
        self.assertEqual(metric["segments"]["merge_done"]["n"], 0)
        self.assertEqual(metric["segments"]["ready_refined"]["missing"], 1)
        self.assertEqual(metric["missing_boundaries"]["refined"], 1)
        self.assertEqual(metric["missing_boundaries"]["pr_open"], 1)
        self.assertEqual(metric["explained_share"], round(10 / 40, 4))

    def test_a_jira_only_store_still_has_a_window(self):
        # no PRs at all: "all time" must not collapse to the snapshot
        fx = balanced_fixture()
        fx.objects = [o for o in fx.objects if o["object_type"] != "PR"]
        fx.events = [e for e in fx.events if e["repo"] is None]
        court, data, window, _threads, _bots, _sizes = self.load(fx)
        self.assertEqual(window[0], T0)
        metric = jira.waiting_time_decomposition(court, data, window)
        self.assertTrue(metric["available"])
        self.assertEqual(metric["stories"], 1)

    def test_a_pr_opened_before_the_ticket_started_is_inverted_not_negative(self):
        fx = balanced_fixture()
        # move the PR open to +25, i.e. before the story moved to in progress
        for event in fx.events:
            if event["repo"] is not None:
                event["ts"] -= timedelta(hours=15)
        for obj in fx.objects:
            if obj["object_type"] == "PR":
                obj["created_at"] -= timedelta(hours=15)
                obj["closed_at"] -= timedelta(hours=15)
        metric = self.metric(fx, "waiting_time_decomposition")
        segment = metric["segments"]["in_progress_pr_open"]
        self.assertEqual(segment["inverted"], 1)
        self.assertEqual(segment["hours"], 0.0)
        self.assertEqual(metric["segments"]["sprint_in_progress"]["inverted"], 0)
        conformance = self.metric(fx, "jira_conformance")
        self.assertEqual(conformance["pr_opened_before_start"], 1)

    def test_days_filters_the_story_cohort(self):
        fx = balanced_fixture()
        fx.story("ENG-2", AS_OF - timedelta(days=2), state="done",
                 closed=AS_OF - timedelta(days=1))
        fx.pointed("ENG-2", AS_OF - timedelta(days=2, hours=-10), 2)
        full = self.metric(fx, "carry_over", days=None)
        week = self.metric(fx, "carry_over", days=7)
        self.assertEqual(full["stories"], 2)
        self.assertEqual(week["stories"], 1)


# --------------------------------------------------------------------------- #
# 2. carry-over (prd/21 §2)
# --------------------------------------------------------------------------- #
class CarryOverTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=14), closed=T0 + timedelta(days=14))
        fx.sprint(2, T0 + timedelta(days=14), end=T0 + timedelta(days=28))
        # ENG-1 was in sprint 1 from the start, then moved to sprint 2
        fx.story("ENG-1", T0)
        fx.add_to_sprint("ENG-1", T0, 1, at_start=True)
        fx.add_to_sprint("ENG-1", T0 + timedelta(days=14), 2)
        fx.status("ENG-1", T0 + timedelta(days=15), "in_progress")
        # ENG-2 was only ever in sprint 1
        fx.story("ENG-2", T0)
        fx.add_to_sprint("ENG-2", T0, 1, at_start=True)
        fx.status("ENG-2", T0 + timedelta(days=1), "in_progress")
        # ENG-3 was removed from sprint 1 and re-added to the same sprint
        fx.story("ENG-3", T0)
        fx.add_to_sprint("ENG-3", T0, 1, at_start=True)
        fx.add_to_sprint("ENG-3", T0 + timedelta(days=3), 1)
        # ENG-4 never entered a sprint
        fx.story("ENG-4", T0)
        return fx

    def test_carry_over_counts_distinct_sprints(self):
        metric = self.metric(self.fixture(), "carry_over")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["stories"], 4)
        self.assertEqual(metric["stories_in_a_sprint"], 3)
        self.assertEqual(metric["carried_over"], 1)
        self.assertEqual([row["key"] for row in metric["rows"]], ["ENG-1"])
        self.assertEqual(metric["rows"][0]["sprints"], [1, 2])
        self.assertEqual(metric["share"], round(1 / 3, 4))
        self.assertEqual(metric["readded_same_sprint"], 1)
        self.assertEqual(metric["sprints_involved"], 2)
        # first sprint_added (T0) → first in-progress (T0 + 15 d) = 360 h
        self.assertEqual(metric["rows"][0]["wait_hours"], 360.0)

    def test_emits_the_carry_over_template(self):
        payload = self.payload(self.fixture())
        suggestion = by_id(payload, "jira-carry-over")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["title"], "1 story carried over between sprints")
        self.assertEqual(suggestion["severity"], "critical")   # 1 of 3 stories ≥ 25%
        self.assertEqual(suggestion["impact_hours"], 360.0)
        self.assertEqual(suggestion["evidence"]["carried_over"], 1)
        self.assertEqual(suggestion["evidence"]["sprints"], 2)

    def test_no_carry_over_means_no_rule(self):
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=14))
        fx.story("ENG-1", T0)
        fx.add_to_sprint("ENG-1", T0, 1, at_start=True)
        payload = self.payload(fx)
        metric = payload["metrics"]["carry_over"]
        self.assertTrue(metric["available"])
        self.assertEqual(metric["carried_over"], 0)
        self.assertIsNone(by_id(payload, "jira-carry-over"))


# --------------------------------------------------------------------------- #
# 3. point churn (prd/21 §3)
# --------------------------------------------------------------------------- #
class PointChurnTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=14))
        # ENG-1: pointed at refinement (+10), then re-pointed after start (+50)
        fx.story("ENG-1", T0)
        fx.pointed("ENG-1", T0 + timedelta(hours=10), 3)
        fx.status("ENG-1", T0 + timedelta(hours=30), "in_progress")
        fx.pointed("ENG-1", T0 + timedelta(hours=50), 8)
        # ENG-2: the estimate is set before the start and never moves
        fx.story("ENG-2", T0)
        fx.pointed("ENG-2", T0 + timedelta(hours=5), 2)
        fx.status("ENG-2", T0 + timedelta(hours=20), "in_progress")
        return fx

    def test_a_point_change_after_start_is_churn(self):
        metric = self.metric(self.fixture(), "point_churn")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["stories"], 2)
        self.assertEqual(metric["stories_pointed"], 2)
        self.assertEqual(metric["churned"], 1)
        self.assertEqual(metric["churned_events"], 1)
        self.assertEqual([row["key"] for row in metric["rows"]], ["ENG-1"])
        self.assertEqual(metric["rows"][0]["points"], 8)
        self.assertEqual(metric["share"], 0.5)

    def test_emits_the_point_churn_template(self):
        payload = self.payload(self.fixture())
        suggestion = by_id(payload, "jira-point-churn")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["title"], "1 story re-pointed after work started")
        self.assertEqual(suggestion["severity"], "critical")   # 1 of 2 stories ≥ 25%
        # the impact basis is the measured refinement cost: median(10 h, 5 h)
        self.assertEqual(suggestion["evidence"]["hours_per_refinement"], 7.5)
        self.assertEqual(suggestion["impact_hours"], 7.5)

    def test_points_set_before_start_are_not_churn(self):
        fx = Fixture()
        fx.story("ENG-1", T0)
        fx.pointed("ENG-1", T0 + timedelta(hours=5), 3)
        fx.status("ENG-1", T0 + timedelta(hours=20), "in_progress")
        payload = self.payload(fx)
        self.assertEqual(payload["metrics"]["point_churn"]["churned"], 0)
        self.assertIsNone(by_id(payload, "jira-point-churn"))


# --------------------------------------------------------------------------- #
# 4. worklog cross-check (prd/21 §4)
# --------------------------------------------------------------------------- #
class WorklogTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=14))
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=90))
        fx.pointed("ENG-1", T0 + timedelta(hours=10), 3)
        fx.status("ENG-1", T0 + timedelta(hours=30), "in_progress")
        fx.status("ENG-1", T0 + timedelta(hours=90), "done", from_status="in_progress")
        # one worklog per log_delay_bucket
        fx.worklog("ENG-1", T0 + timedelta(hours=40), 1.0, delay_hours=1.0,
                   bucket="same_day")
        fx.worklog("ENG-1", T0 + timedelta(hours=41), 1.0, delay_hours=26.0,
                   bucket="next_day")
        fx.worklog("ENG-1", T0 + timedelta(hours=42), 1.0, delay_hours=120.0,
                   bucket="within_week")
        fx.worklog("ENG-1", T0 + timedelta(hours=43), 1.0, delay_hours=61 * 24.0,
                   bucket="late")
        fx.pr(1, "AaAa", T0 + timedelta(hours=40), closed=T0 + timedelta(hours=80))
        fx.pr_event(1, "comment_added", "RrRr", 10, severity="suggestion")
        fx.pr_event(1, "pr_pushed", "AaAa", 20, push_class="content")
        fx.pr_event(1, "pr_approved", "RrRr", 30)
        fx.pr_event(1, "pr_merged", "RrRr", 40)
        return fx

    def test_bucket_distribution_and_delay_statistics(self):
        metric = self.metric(self.fixture(), "worklog_cross_check")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["worklogs"], 4)
        self.assertEqual(metric["logged_hours"], 4.0)
        self.assertEqual(metric["buckets"], {
            "same_day": 1, "next_day": 1, "within_week": 1, "late": 1,
        })
        self.assertEqual(metric["bucket_shares"]["late"], 0.25)
        self.assertEqual(metric["late_share"], 0.5)
        self.assertEqual(metric["untrusted_hours"], 2.0)
        # (1 + 26 + 120 + 1464) h / 4 / 24
        self.assertEqual(metric["mean_delay_days"], 16.78)
        self.assertEqual(metric["median_delay_days"], 3.04)   # (26 + 120) / 2 h
        self.assertEqual(metric["p90_delay_days"], 61.0)

    def test_logged_hours_are_cross_checked_against_pr_activity(self):
        metric = self.metric(self.fixture(), "worklog_cross_check")
        # the only active PR interval is the push at +60 → [50, 60)
        self.assertEqual(metric["active_hours"], 10.0)
        self.assertEqual(metric["logged_to_active_ratio"], 0.4)
        self.assertEqual(metric["logged_to_cycle_ratio"], round(4 / 90, 2))

    def test_emits_the_late_logging_template(self):
        payload = self.payload(self.fixture())
        suggestion = by_id(payload, "jira-worklog-noise")
        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["title"],
            "Logging is 16.8 days late on average — don't trust the logged hours",
        )
        self.assertEqual(suggestion["severity"], "critical")   # ≥ 7 days
        self.assertEqual(suggestion["impact_hours"], 0.0)      # measurement, not wait
        self.assertEqual(suggestion["evidence"]["untrusted_hours"], 2.0)
        self.assertEqual(suggestion["evidence"]["late_share"], 0.5)

    def test_a_large_mismatch_emits_the_ratio_template(self):
        payload = self.payload(self.fixture())
        suggestion = by_id(payload, "jira-worklog-mismatch")
        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["title"], "Logged hours differ from active PR time by 0.4×"
        )
        self.assertEqual(suggestion["severity"], "warning")
        self.assertEqual(suggestion["evidence"]["ratio"], 0.4)

    def test_same_day_logging_with_matching_hours_is_silent(self):
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=14))
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=90))
        fx.pointed("ENG-1", T0 + timedelta(hours=10), 3)
        fx.status("ENG-1", T0 + timedelta(hours=30), "in_progress")
        fx.status("ENG-1", T0 + timedelta(hours=90), "done", from_status="in_progress")
        fx.worklog("ENG-1", T0 + timedelta(hours=40), 20.0, delay_hours=1.0,
                   bucket="same_day")
        fx.pr(1, "AaAa", T0 + timedelta(hours=40), closed=T0 + timedelta(hours=80))
        fx.pr_event(1, "pr_pushed", "AaAa", 20, push_class="content")
        fx.pr_event(1, "pr_merged", "RrRr", 40)
        payload = self.payload(fx)
        metric = payload["metrics"]["worklog_cross_check"]
        self.assertEqual(metric["late_share"], 0.0)
        self.assertEqual(metric["logged_to_active_ratio"], 1.0)
        self.assertIsNone(by_id(payload, "jira-worklog-noise"))
        self.assertIsNone(by_id(payload, "jira-worklog-mismatch"))


# --------------------------------------------------------------------------- #
# 5. conformance, Jira side (prd/21 §5)
# --------------------------------------------------------------------------- #
class JiraConformanceTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=14))
        # ENG-1: started (+30) before the estimate was set (+40)
        fx.story("ENG-1", T0)
        fx.status("ENG-1", T0 + timedelta(hours=30), "in_progress")
        fx.pointed("ENG-1", T0 + timedelta(hours=40), 5)
        # ENG-2: no pointed event at all — unknown, not a violation
        fx.story("ENG-2", T0)
        fx.status("ENG-2", T0 + timedelta(hours=20), "in_progress")
        # ENG-3: refined first, then started — conforming
        fx.story("ENG-3", T0)
        fx.pointed("ENG-3", T0 + timedelta(hours=5), 2)
        fx.status("ENG-3", T0 + timedelta(hours=20), "in_progress")
        return fx

    def test_counts_the_violations_it_can_prove(self):
        metric = self.metric(self.fixture(), "jira_conformance")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["stories"], 3)
        self.assertEqual(metric["stories_started"], 3)
        self.assertEqual(metric["started_before_refined"], 1)
        self.assertEqual(metric["started_unrefined"], 1)   # not a violation
        self.assertEqual(metric["started_before_refined_share"], round(1 / 3, 4))
        self.assertEqual(metric["examples"], ["ENG-1"])
        self.assertEqual(metric["unrefined_examples"], ["ENG-2"])
        # ENG-1 was pointed only after it started, so it is churn as well
        self.assertEqual(metric["points_changed_after_start"], 1)
        self.assertEqual(metric["carried_over"], 0)

    def test_emits_the_conformance_template(self):
        payload = self.payload(self.fixture())
        suggestion = by_id(payload, "jira-conformance")
        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["title"],
            "1 story started before refinement; 1 re-pointed after start",
        )
        self.assertEqual(suggestion["severity"], "critical")   # 33% unrefined starts
        self.assertEqual(suggestion["evidence"]["started_unrefined"], 1)
        # the median ready→refined segment here is 22.5 h (40 h and 5 h)
        self.assertEqual(suggestion["evidence"]["hours_per_refinement"], 22.5)
        self.assertEqual(suggestion["impact_hours"], 45.0)    # 2 × refinement cost

    def test_a_conforming_store_is_silent(self):
        fx = Fixture()
        fx.story("ENG-1", T0)
        fx.pointed("ENG-1", T0 + timedelta(hours=5), 2)
        fx.status("ENG-1", T0 + timedelta(hours=20), "in_progress")
        payload = self.payload(fx)
        self.assertIsNone(by_id(payload, "jira-conformance"))


# --------------------------------------------------------------------------- #
# no Jira data: a stated result, not zeroes
# --------------------------------------------------------------------------- #
class NoJiraTests(StoreCase):
    def pr_only(self) -> Fixture:
        fx = Fixture()
        fx.pr(1, "AaAa", T0, closed=T0 + timedelta(hours=60))
        fx.pr_event(1, "pr_approved", "RrRr", 40)
        fx.pr_event(1, "pr_merged", "RrRr", 60)
        return fx

    def test_every_jira_metric_reports_unavailable(self):
        payload = self.payload(self.pr_only())
        self.assertFalse(payload["jira"]["available"])
        self.assertIn("no Jira data", payload["jira"]["note"])
        for name in ("waiting_time_decomposition", "carry_over", "point_churn",
                     "worklog_cross_check", "jira_conformance"):
            with self.subTest(metric=name):
                metric = payload["metrics"][name]
                self.assertFalse(metric["available"])
                self.assertIn("no Jira data", metric["note"])
        self.assertEqual(payload["sample"]["stories"], 0)
        self.assertFalse(payload["sample"]["jira_available"])

    def test_no_jira_suggestion_is_emitted(self):
        payload = self.payload(self.pr_only())
        ids = {suggestion["id"] for suggestion in payload["suggestions"]}
        self.assertFalse({i for i in ids if i.startswith("jira-")}, ids)
        # the PR-side metrics still work
        self.assertEqual(payload["metrics"]["cycle_time"]["n"], 1)
        self.assertIsNotNone(by_id(payload, "review-sla") or by_id(payload, "cycle-time"))

    def test_the_contract_still_holds_with_jira_objects(self):
        fx = balanced_fixture()
        fx.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            report = verify(con, ticket_projects=("ENG",))
        finally:
            con.close()
        self.assertTrue(report["ok"], report["problems"])


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


class JiraInsightsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        cls.store = balanced_fixture().write(cls.dir / "del.duckdb")
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

    def test_insights_expose_the_ticket_layer(self):
        reply = get("/api/insights", tenant="del", level="team", days=3650)
        self.assertEqual(reply.status, 200)
        body = reply.json()
        self.assertTrue(body["jira"]["available"])
        self.assertEqual(body["jira"]["stories"], 1)
        self.assertEqual(body["sample"]["stories"], 1)
        self.assertEqual(body["sample"]["stories_decomposed"], 1)
        decomposition = body["metrics"]["waiting_time_decomposition"]
        # the calendar is attached by the server, so the segments are business
        # hours: strictly less than the 90 wall-clock hours the fixture spans
        self.assertEqual(body["hours_mode"], "business")
        self.assertGreater(decomposition["total_hours"], 0.0)
        self.assertLess(decomposition["total_hours"], 90.0)
        self.assertEqual(
            round(sum(entry["hours"] for entry in decomposition["segments"].values()), 2),
            decomposition["total_hours"],
        )
        self.assertGreater(decomposition["segments"]["review_rounds"]["share"], 0.0)
        # the ticket-layer rule is wired through the API, and its evidence names
        # a real segment (business hours compress the waits, so which one
        # dominates is a property of the calendar, not of the fixture)
        suggestion = next(
            (s for s in body["suggestions"] if s["id"] == "jira-waiting-time"), None
        )
        if suggestion is not None:
            self.assertEqual(suggestion["metric"], "waiting_time_decomposition")
            self.assertIn(suggestion["evidence"]["segment"],
                          [label for _key, label in jira.SEGMENTS])


# --------------------------------------------------------------------------- #
# the real pipelines: Bitbucket + Jira in one store (the join, end to end)
# --------------------------------------------------------------------------- #
class CombinedFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ingest import fixtures, jira_fixtures
        from ingest.config import Config
        from ingest.importer import import_bundle
        from ingest.pipeline import run
        from ingest.scrape_jira import scrape
        from ingest.transport import FixtureJiraTransport, FixtureTransport

        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.cfg = Config(
            db_path=root / "combined.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform"), ("ENG", "mobile")],
            ticket_projects=("PROJ", "OPS", "ARCH"),
            jira_projects=("PROJ",),
        )
        # 1. the Bitbucket half: PRs, commits, review events
        run(cls.cfg, FixtureTransport(fixtures.generate(n=40, seed=20261115)),
            source="fixture")
        # 2. the Jira half: epic, stories, sprints, worklogs
        bundle = root / "jira.tgz"
        scrape(
            cls.cfg, FixtureJiraTransport(jira_fixtures.generate()), "jira-fixture",
            out=bundle, state_dir=root / "jira.state",
        )
        import_bundle(cls.cfg, bundle)
        cls.con = connect(cls.cfg.db_path, read_only=True)
        # the committed calendar (prd/19): the same one the server attaches
        cls.calendar = calendar.load()
        cls.court = ballcourt.load(cls.con, calendar=cls.calendar)
        cls.data = jira.load(cls.con, cls.court)
        cls.window = rules.window_for(cls.court, None, cls.data)
        cls.payload = rules.insights(
            cls.court, rules.load_threads(cls.con), cls.window,
            bots=rules.load_bots(cls.con), sizes=rules.load_pr_sizes(cls.con),
            jira=cls.data,
        )

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def test_both_halves_are_in_one_store(self):
        self.assertTrue(self.data.available)
        self.assertEqual(len(self.data.stories), 12)
        self.assertEqual(self.data.epics, 1)
        self.assertEqual(len(self.data.sprints), 3)
        self.assertTrue(self.court.prs)

    def test_the_ticket_to_pr_join_resolves(self):
        joined = [story for story in self.data.stories if story.prs]
        self.assertTrue(joined, "no story resolved to a PR: the key join is broken")
        # every joined PR names the story's key
        for story in joined:
            for state in story.prs:
                self.assertIn(story.key, state.pr["ticket_keys"])
        decomposition = self.payload["metrics"]["waiting_time_decomposition"]
        # the PR-side segments are measurable only because of the join
        self.assertGreater(decomposition["segments"]["in_progress_pr_open"]["n"], 0)
        self.assertGreater(decomposition["segments"]["pr_open_first_review"]["n"], 0)

    def test_the_jira_metrics_are_populated(self):
        metrics = self.payload["metrics"]
        self.assertTrue(metrics["waiting_time_decomposition"]["available"])
        self.assertGreater(metrics["waiting_time_decomposition"]["total_hours"], 0)
        # PROJ-105 is carried from sprint 1 to sprint 2 by the fixture
        self.assertGreaterEqual(metrics["carry_over"]["carried_over"], 1)
        self.assertIn("PROJ-105", [row["key"] for row in metrics["carry_over"]["rows"]])
        # the fixture covers all four worklog buckets by construction
        self.assertEqual(
            {name for name, count in metrics["worklog_cross_check"]["buckets"].items()
             if count},
            {"same_day", "next_day", "within_week", "late"},
        )
        self.assertEqual(self.payload["hours_mode"], "business")


if __name__ == "__main__":
    unittest.main()

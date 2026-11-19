"""Flow efficiency and survival analysis (prd/05, goals 1 + 7).

Two questions the waiting-time decomposition has to answer honestly:

* **flow efficiency** — of a PR's lifetime, how much was work and how much was
  queue? The interval model (``analysis.ballcourt``) already labels every
  stretch; this module's metric only classifies active vs waiting, so the tests
  build intervals by hand and pin the arithmetic: 10 h of active work in a 50 h
  PR is 20%, and the process number is Σ working ÷ Σ elapsed.
* **time to merge** — what is the cycle time when the PRs that never merged are
  counted too? Open PRs are right-censored at the snapshot and declined PRs at
  their close, so the Kaplan–Meier median is not the merged-only median. The
  hand-computed fixtures below pin the metric and the two rules it feeds; the
  estimator's arithmetic is pinned in ``test_survival.py``.

Everything is deterministic: explicit events, explicit commits, no generator and
no randomness, so a change in the heuristic, a threshold or the estimator fails
with the exact number that moved.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from analysis import ballcourt, calendar as workcal, rules
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run

REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)
TZ = ZoneInfo("Europe/Tallinn")

CALENDAR_YAML = """
timezone: Europe/Tallinn
weekdays: [1, 2, 3, 4, 5]
working_hours:
  default: "09:00–17:00"
"""


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A Tallinn local wall-clock time, as the naive UTC the store holds."""
    return (
        datetime(year, month, day, hour, minute, tzinfo=TZ)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )


def write_calendar(root: Path) -> Path:
    path = root / "calendar.yaml"
    path.write_text(CALENDAR_YAML, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# a synthetic store with hand-written events, commits and objects
# --------------------------------------------------------------------------- #
class Fixture:
    """Build a store from explicit PRs, events and commits (no randomness)."""

    def __init__(self, as_of: datetime):
        self.as_of = as_of
        self.events: list[dict] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
        self._count = 0

    def pr(self, number: int, author: str = "AaAa", opened: datetime = T0,
           state: str = "open", closed: datetime | None = None,
           tickets: tuple[str, ...] = ("ENG-1",)) -> "Fixture":
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
        self.event(number, "pr_opened", author, 0)
        return self

    def event(self, number: int, activity: str, actor: str, hours: float,
              **attributes) -> "Fixture":
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
        return self

    def commit(self, number: int, hours: float) -> "Fixture":
        """A COMMIT object on the PR at ``opened + hours`` (the work evidence)."""
        self._count += 1
        sha = hashlib.sha1(f"{REPO}#{number}:{self._count}".encode()).hexdigest()
        self.objects.append({
            "object_type": "COMMIT",
            "object_id": f"COMMIT:{sha}",
            "repo": REPO,
            "pr_id": number,
            "ticket_keys": [],
            "actor": None,
            "state": None,
            "created_at": self.opened[number] + timedelta(hours=hours),
            "closed_at": None,
            "head_commit": sha,
            "message_class": "feature",
            "attributes": {},
        })
        return self

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, [])
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-flow-survival",
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


class StoreCase(unittest.TestCase):
    """One temp store per test; the court loads the way the API loads it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def court(self, fixture: Fixture, calendar=None) -> ballcourt.BallCourt:
        fixture.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            return ballcourt.load(con, calendar=calendar)
        finally:
            con.close()

    def metric(self, fixture: Fixture, name: str, calendar=None):
        court = self.court(fixture, calendar)
        window = rules.window_for(court)
        return rules.metrics(court, [], window)[name]

    def insights(self, fixture: Fixture, calendar=None, level: str = "team"):
        court = self.court(fixture, calendar)
        window = rules.window_for(court)
        return rules.insights(court, [], window, level=level)


def by_id(payload: dict, suggestion_id: str) -> dict | None:
    return next((s for s in payload["suggestions"] if s["id"] == suggestion_id), None)


# --------------------------------------------------------------------------- #
# 2. flow efficiency
# --------------------------------------------------------------------------- #
def active_fixture(as_of: datetime = T0 + timedelta(hours=200)) -> Fixture:
    """One PR that is exactly 10 h active in 50 h elapsed (20%).

    opened ──10 h── needs_work ──10 h (1 commit)── push ──30 h── merged
             review              active              review
    """
    fx = Fixture(as_of=as_of)
    fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=50))
    fx.event(1, "pr_needs_work", "RrRr", 10)
    fx.commit(1, 15)
    fx.event(1, "pr_pushed", "AaAa", 20, push_class="content")
    fx.event(1, "pr_merged", "RrRr", 50)
    return fx


class FlowEfficiencyTests(StoreCase):
    def test_active_over_elapsed_is_exact(self):
        metric = self.metric(active_fixture(), "flow_efficiency")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["n"], 1)
        self.assertEqual(metric["elapsed_hours"], 50.0)
        self.assertEqual(metric["active_hours"], 10.0)
        self.assertEqual(metric["waiting_hours"], 40.0)
        self.assertEqual(metric["touch_hours"], 0.0)
        self.assertEqual(metric["median_efficiency"], 0.2)     # 10 / 50
        self.assertEqual(metric["aggregate_efficiency"], 0.2)
        row = metric["prs"][0]
        self.assertEqual(row["id"], "platform#1")
        self.assertEqual(row["elapsed_hours"], 50.0)
        self.assertEqual(row["active_hours"], 10.0)
        self.assertEqual(row["efficiency"], 0.2)
        self.assertEqual(row["measured_efficiency"], 0.2)
        self.assertEqual(row["commits"], 1)
        self.assertEqual(row["state"], "merged")
        # the waiting split: this PR waits only in review
        self.assertEqual(metric["waiting_breakdown"]["review"]["hours"], 40.0)
        self.assertEqual(metric["waiting_breakdown"]["review"]["share"], 1.0)

    def test_reviewer_touches_are_credited_at_a_stated_cost(self):
        fx = active_fixture()
        # a second PR: no commits at all, one review comment, merged after 20 h
        fx.pr(2, "AaAa", state="merged", closed=T0 + timedelta(hours=20))
        fx.event(2, "comment_added", "RrRr", 10, severity="suggestion")
        fx.event(2, "pr_merged", "RrRr", 20)
        metric = self.metric(fx, "flow_efficiency")
        self.assertEqual(metric["touch_hours_per_touch"], 0.5)
        self.assertEqual(metric["touches"], 3)          # needs_work + push + comment
        self.assertEqual(metric["credited_touches"], 1)  # the push/needs_work sit in active time
        self.assertEqual(metric["touch_hours"], 0.5)
        row = next(r for r in metric["prs"] if r["number"] == 2)
        self.assertEqual(row["active_hours"], 0.0)
        self.assertEqual(row["touch_hours"], 0.5)
        self.assertEqual(row["efficiency"], 0.025)       # 0.5 / 20
        self.assertEqual(row["measured_efficiency"], 0.0)
        # process number: (10 + 0.5) / (50 + 20)
        self.assertEqual(metric["aggregate_efficiency"], 0.15)
        self.assertEqual(metric["median_efficiency"], 0.1125)

    def test_median_and_aggregate_differ_when_long_prs_wait_more(self):
        fx = active_fixture()
        # 10 h active in 25 h elapsed = 40%
        fx.pr(3, "AaAa", state="merged", closed=T0 + timedelta(hours=25))
        fx.event(3, "pr_needs_work", "RrRr", 5)
        fx.commit(3, 6)
        fx.event(3, "pr_pushed", "AaAa", 15, push_class="content")
        fx.event(3, "pr_merged", "RrRr", 25)
        # 6 h active in 10 h elapsed = 60%
        fx.pr(4, "AaAa", state="merged", closed=T0 + timedelta(hours=10))
        fx.event(4, "pr_needs_work", "RrRr", 1)
        fx.event(4, "pr_pushed", "AaAa", 7, push_class="content")
        fx.event(4, "pr_merged", "RrRr", 10)
        metric = self.metric(fx, "flow_efficiency")
        self.assertEqual(metric["n"], 3)
        self.assertEqual(metric["median_efficiency"], 0.4)
        # Σ working / Σ elapsed = (10 + 10 + 6) / (50 + 25 + 10)
        self.assertEqual(metric["aggregate_efficiency"], 0.3059)
        self.assertEqual(
            [row["efficiency"] for row in metric["prs"]], [0.2, 0.4, 0.6]
        )
        self.assertEqual(metric["by_state"]["merged"]["n"], 3)
        self.assertEqual(metric["by_state"]["merged"]["median_efficiency"], 0.4)

    def test_without_any_active_evidence_the_metric_is_unavailable(self):
        # a review-only PR: the store has no commit evidence, so "waiting" cannot
        # be distinguished from unobserved work — the metric says so instead of
        # reporting a 2.5% efficiency as fact
        fx = Fixture(as_of=T0 + timedelta(hours=200))
        fx.pr(2, "AaAa", state="merged", closed=T0 + timedelta(hours=20))
        fx.event(2, "comment_added", "RrRr", 10, severity="suggestion")
        fx.event(2, "pr_merged", "RrRr", 20)
        metric = self.metric(fx, "flow_efficiency")
        self.assertFalse(metric["available"])
        self.assertEqual(metric["n"], 1)
        self.assertEqual(metric["median_efficiency"], 0.025)
        self.assertIsNone(by_id(self.insights(fx), "flow-efficiency"))

    def test_a_long_pr_pulls_the_process_number_below_the_median_pr(self):
        # two efficient PRs (40 of 50 h = 80%) and one long, mostly-waiting PR
        # (10 of 500 h = 2%): the median PR is healthy, the process is not
        fx = Fixture(as_of=T0 + timedelta(hours=600))
        for number in (1, 2):
            fx.pr(number, "AaAa", state="merged", closed=T0 + timedelta(hours=50))
            fx.event(number, "pr_needs_work", "RrRr", 5)
            fx.commit(number, 6)
            fx.event(number, "pr_pushed", "AaAa", 45, push_class="content")
            fx.event(number, "pr_merged", "RrRr", 50)
        fx.pr(3, "AaAa", state="merged", closed=T0 + timedelta(hours=500))
        fx.event(3, "pr_needs_work", "RrRr", 5)
        fx.commit(3, 6)
        fx.event(3, "pr_pushed", "AaAa", 15, push_class="content")
        fx.event(3, "pr_merged", "RrRr", 500)
        metric = self.metric(fx, "flow_efficiency")
        self.assertEqual(metric["median_efficiency"], 0.8)
        self.assertEqual(metric["aggregate_efficiency"], 0.15)   # 90 / 600
        suggestion = by_id(self.insights(fx), "flow-efficiency")
        self.assertIsNotNone(suggestion)
        self.assertEqual(
            suggestion["title"],
            "Flow efficiency is 15% — the process spends 85% of cycle time waiting",
        )
        # the median PR is at target, so the rule says where the gap is
        self.assertIn("median PR is at 80%", suggestion["suggestion"])
        self.assertIn("long PRs", suggestion["suggestion"])

    def test_the_rule_says_the_process_spends_most_of_its_time_waiting(self):
        payload = self.insights(active_fixture())
        suggestion = by_id(payload, "flow-efficiency")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["metric"], "flow_efficiency")
        self.assertEqual(suggestion["severity"], "warning")    # 20% is under 25%, above 10%
        self.assertEqual(
            suggestion["title"],
            "Flow efficiency is 20% — the process spends 80% of cycle time waiting",
        )
        self.assertEqual(suggestion["evidence"]["aggregate_efficiency"], 0.2)
        self.assertEqual(suggestion["evidence"]["median_efficiency"], 0.2)
        self.assertEqual(suggestion["evidence"]["largest_waiting_bucket"], "the review queue")
        self.assertEqual(suggestion["evidence"]["touch_hours_per_touch"], 0.5)
        self.assertIn("80%", suggestion["suggestion"])
        # waiting above a healthy 75% of elapsed: 40 − 50 × 0.75 = 2.5 h
        self.assertEqual(suggestion["impact_hours"], 2.5)

    def test_business_hours_are_the_denominator(self):
        # Friday 16:00 -> Monday 10:00: 66 wall-clock hours, 2 working hours.
        # The author works 30 min on Friday and 30 min on Monday (1.5 h active).
        fx = Fixture(as_of=at(2026, 3, 9, 12))
        fx.pr(1, "AaAa", opened=at(2026, 3, 6, 16), state="merged",
              closed=at(2026, 3, 9, 10))
        fx.event(1, "pr_needs_work", "RrRr", 0)
        fx.commit(1, 0.5)
        fx.event(1, "pr_pushed", "AaAa", 65.5, push_class="content")
        fx.event(1, "pr_merged", "RrRr", 66)
        calendar = workcal.load(config_path=write_calendar(self.root))
        metric = self.metric(fx, "flow_efficiency", calendar=calendar)
        self.assertEqual(metric["elapsed_hours"], 2.0)
        self.assertEqual(metric["active_hours"], 1.5)
        self.assertEqual(metric["median_efficiency"], 0.75)
        # the same store without a calendar is wall-clock: 65.5 / 66 ≈ 99%
        wall = self.metric(fx, "flow_efficiency")
        self.assertEqual(wall["elapsed_hours"], 66.0)
        self.assertEqual(wall["active_hours"], 65.5)
        self.assertEqual(wall["median_efficiency"], 0.9924)


# --------------------------------------------------------------------------- #
# 3. survival / time to merge
# --------------------------------------------------------------------------- #
def survival_fixture(as_of: datetime) -> Fixture:
    """Merged at 10/20/30/40 h, one open PR 15 h old, one declined at 25 h."""
    fx = Fixture(as_of=as_of)
    for number, hours in enumerate((10, 20, 30, 40), start=1):
        fx.pr(number, "AaAa", state="merged", closed=T0 + timedelta(hours=hours))
        fx.event(number, "pr_merged", "RrRr", hours)
    fx.pr(5, "AaAa", opened=as_of - timedelta(hours=15))     # still open, age 15 h
    fx.pr(6, "AaAa", state="declined", closed=T0 + timedelta(hours=25))
    fx.event(6, "pr_declined", "RrRr", 25)
    return fx


class SurvivalTests(StoreCase):
    def test_the_median_counts_censored_prs_instead_of_dropping_them(self):
        metric = self.metric(survival_fixture(T0 + timedelta(hours=100)), "survival")
        self.assertEqual(metric["estimator"], "Kaplan–Meier")
        self.assertEqual(metric["n"], 6)
        self.assertEqual(metric["events"], 4)
        self.assertEqual(metric["cohort"], 6)
        self.assertTrue(metric["median_reached"])
        # KM: 5/6, then 3/4 of that, then 1/2 of that at 30 h -> 0.3125
        self.assertEqual(metric["median_hours"], 30.0)
        self.assertEqual(metric["quantiles"], {"p25": 20.0, "p50": 30.0, "p75": 40.0})
        self.assertEqual(
            [step["hours"] for step in metric["curve"]], [10.0, 20.0, 30.0, 40.0]
        )
        self.assertEqual(
            [step["at_risk"] for step in metric["curve"]], [6, 4, 2, 1]
        )
        # the naive number, reported next to it, is the merged-only median
        self.assertEqual(metric["merged_only_median_hours"], 25.0)
        self.assertEqual(metric["understatement_hours"], 5.0)
        self.assertEqual(metric["understatement_share"], 0.2)
        self.assertEqual(metric["censored"]["open"], 1)
        self.assertEqual(metric["censored"]["declined"], 1)
        self.assertEqual(metric["censored"]["total"], 2)
        self.assertEqual(metric["censored"]["open_median_age_hours"], 15.0)

    def test_cycle_time_carries_the_survival_estimate(self):
        court = self.court(survival_fixture(T0 + timedelta(hours=100)))
        window = rules.window_for(court)
        cycle = rules.cycle_time(court, window)
        fit = rules.time_to_merge(court, window)
        self.assertEqual(cycle["median_hours"], 25.0)
        self.assertEqual(cycle["survival_median_hours"], fit["median_hours"])
        self.assertIn("censored", cycle["censored"]["note"])
        self.assertIn("survival", cycle["censored"]["note"])

    def test_a_median_that_is_not_reached_emits_no_rule(self):
        fx = Fixture(as_of=T0 + timedelta(hours=100))
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=10))
        fx.event(1, "pr_merged", "RrRr", 10)
        for number, age in ((2, 20), (3, 30), (4, 40)):
            fx.pr(number, "AaAa", opened=T0 + timedelta(hours=100 - age))
        metric = self.metric(fx, "survival")
        self.assertIsNone(metric["median_hours"])
        self.assertFalse(metric["median_reached"])
        self.assertIsNone(by_id(self.insights(fx), "time-to-merge"))

    def test_the_rule_uses_the_full_population_median(self):
        fx = Fixture(as_of=T0 + timedelta(hours=1000))
        for number, hours in enumerate((30, 60, 100, 240), start=1):
            fx.pr(number, "AaAa", state="merged", closed=T0 + timedelta(hours=hours))
            fx.event(number, "pr_merged", "RrRr", hours)
        fx.pr(5, "AaAa", state="declined", closed=T0 + timedelta(hours=48))
        fx.event(5, "pr_declined", "RrRr", 48)
        fx.pr(6, "AaAa", opened=T0 + timedelta(hours=900))     # open, age 100 h
        payload = self.insights(fx)
        metric = payload["metrics"]["survival"]
        self.assertEqual(metric["median_hours"], 100.0)        # event at 100 precedes the censoring
        self.assertEqual(metric["merged_only_median_hours"], 80.0)
        suggestion = by_id(payload, "time-to-merge")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["metric"], "survival")
        self.assertEqual(suggestion["severity"], "warning")
        self.assertEqual(suggestion["evidence"]["survival_median_hours"], 100.0)
        self.assertEqual(suggestion["evidence"]["merged_only_median_hours"], 80.0)
        self.assertEqual(suggestion["evidence"]["open_censored_prs"], 1)
        self.assertEqual(suggestion["evidence"]["declined_censored_prs"], 1)
        self.assertEqual(suggestion["evidence"]["estimator"], "Kaplan–Meier")
        self.assertEqual(suggestion["impact_hours"], 168.0)     # (100 − 72) × 6

    def test_a_healthy_merged_median_with_a_stale_open_tail_is_flagged(self):
        # the merged PRs are fast (median 20 h < 72 h), but an open PR is 80 h old:
        # the merged-only median cannot see it, which is product review #5
        fx = Fixture(as_of=T0 + timedelta(hours=100))
        for number, hours in enumerate((10, 20, 30), start=1):
            fx.pr(number, "AaAa", state="merged", closed=T0 + timedelta(hours=hours))
            fx.event(number, "pr_merged", "RrRr", hours)
        fx.pr(4, "AaAa", opened=T0 + timedelta(hours=20))      # open, age 80 h
        payload = self.insights(fx)
        metric = payload["metrics"]["survival"]
        self.assertEqual(metric["merged_only_median_hours"], 20.0)
        self.assertEqual(metric["censored"]["open_over_target"], 1)
        self.assertEqual(metric["censored"]["oldest_open_age_hours"], 80.0)
        self.assertEqual(metric["censored"]["open_excess_hours"], 8.0)
        self.assertIsNone(by_id(payload, "time-to-merge"))     # the KM median is 20 h
        suggestion = by_id(payload, "cycle-time-censoring")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["severity"], "warning")
        self.assertEqual(suggestion["impact_hours"], 8.0)      # 80 − 72
        # prd/35: a day is 8 business hours, so the 72 h target reads "9 d"
        self.assertIn("meets 9 d", suggestion["title"])
        self.assertIn("1 open PR", suggestion["title"])
        self.assertEqual(suggestion["evidence"]["open_over_target"], 1)

    def test_business_hours_apply_to_the_survival_estimate(self):
        # Friday 16:00 -> Monday 10:00 is 2 business hours, 66 wall-clock hours
        fx = Fixture(as_of=at(2026, 3, 9, 12))
        fx.pr(1, "AaAa", opened=at(2026, 3, 6, 16), state="merged",
              closed=at(2026, 3, 9, 10))
        fx.event(1, "pr_merged", "RrRr", 66)
        calendar = workcal.load(config_path=write_calendar(self.root))
        business = self.metric(fx, "survival", calendar=calendar)
        self.assertEqual(business["median_hours"], 2.0)
        self.assertEqual(business["merged_only_median_hours"], 2.0)
        wall = self.metric(fx, "survival")
        self.assertEqual(wall["median_hours"], 66.0)

    def test_the_metric_is_exposed_in_the_insights_payload(self):
        payload = self.insights(survival_fixture(T0 + timedelta(hours=100)))
        metrics = payload["metrics"]
        self.assertIn("flow_efficiency", metrics)
        self.assertIn("survival", metrics)
        self.assertEqual(metrics["survival"]["estimator"], "Kaplan–Meier")
        self.assertEqual(payload["sample"]["survival"], 6)
        self.assertEqual(payload["sample"]["flow_efficiency"], 6)
        self.assertFalse(metrics["flow_efficiency"]["available"])
        self.assertEqual(payload["targets"]["flow_efficiency"], 0.25)
        self.assertEqual(payload["targets"]["flow_efficiency_touch_hours"], 0.5)


if __name__ == "__main__":
    unittest.main()

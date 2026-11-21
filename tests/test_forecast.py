"""Probabilistic throughput forecast (prd/60): counts, bootstrap, refusals.

The simulation and the refusals are pure and pinned here: a hand-computed
5-story fixture for the quantiles, the 85 % edge convention, the exact refusal
copy, and the risk adjustment. The store-facing tests build a synthetic
thirteen-week series (known counts, one in-progress week, one unmerged story)
so a boundary or a population change fails with the exact number that moved.
The del-combined pin runs when that store is present.

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

from analysis import ballcourt, forecast, jira, rules, stability
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from server import app as app_module
from tests.test_yield import _del_combined_path

REPO = "DEL/api"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
#: Thirteen full weeks from T0, then a partial week containing the snapshot.
AS_OF = T0 + timedelta(days=7 * 13, hours=12)   # Monday 6 Apr 2026, 12:00


# --------------------------------------------------------------------------- #
# a synthetic store: explicit weekly deliveries
# --------------------------------------------------------------------------- #
class Fixture:
    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self.opened: dict[tuple[str, int], datetime] = {}
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

    def sprint(self, sid: int, start: datetime, planned_end: str,
               state: str = "active", end: datetime | None = None) -> None:
        self._object(
            "SPRINT", f"SPRINT:DEL#{sid}", state=state, created_at=start,
            closed_at=end,
            attributes={"goal_class": "feature", "end_date": planned_end},
        )

    def story(self, key: str, created: datetime, done: datetime | None = None,
              state: str = "done") -> None:
        self._object(
            "STORY", f"STORY:{key}", ticket_keys=[key], actor="AaAa",
            state=state if done else "open", created_at=created,
            closed_at=done,
            attributes={"summary_class": "feature", "epic_key": "DEL-1",
                        "points": 3},
        )
        self.event(key, created, "created")

    def event(self, key: str, ts: datetime, activity: str, **attributes) -> None:
        self._count += 1
        event_key = f"{key}:e{self._count}"
        self.events.append({
            "event_key": event_key,
            "ts": ts,
            "activity": activity,
            "actor": "AaAa",
            "repo": None,
            "pr_id": None,
            "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(event_key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        self.relations.append((event_key, "STORY", f"STORY:{key}"))

    def add_to_sprint(self, key: str, ts: datetime, sid: int) -> None:
        self._count += 1
        event_key = f"{key}:s{self._count}"
        self.events.append({
            "event_key": event_key,
            "ts": ts,
            "activity": "sprint_added",
            "actor": "AaAa",
            "repo": None,
            "pr_id": None,
            "attributes": {"sprint_id": sid, "at_start": True},
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(event_key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        self.relations.append((event_key, "STORY", f"STORY:{key}"))
        self.relations.append((event_key, "SPRINT", f"SPRINT:DEL#{sid}"))

    def pr(self, number: int, tickets: tuple[str, ...],
           opened: datetime, state: str = "open") -> None:
        self.opened[(REPO, number)] = opened
        self._object(
            "PR", f"PR:{REPO}#{number}", repo=REPO, pr_id=number,
            ticket_keys=list(tickets), actor="AaAa", state=state,
            created_at=opened, closed_at=None, head_commit="a" * 40,
        )
        self._count += 1
        event_key = f"{REPO}#{number}:p{self._count}"
        self.events.append({
            "event_key": event_key,
            "ts": opened,
            "activity": "pr_opened",
            "actor": "AaAa",
            "repo": REPO,
            "pr_id": number,
            "attributes": {},
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(event_key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        self.relations.append((event_key, "PR", f"PR:{REPO}#{number}"))

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-forecast",
                "source": "fixture",
                "as_of": self.as_of,
                "source_watermark": self.as_of,
                "classifier_version": "deterministic-v1",
                "prs_seen": len(self.opened),
                "prs_kept": len(self.opened),
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


#: The PRD's del-combined shape, delivered (unmerged excluded).
WEEKLY = [2, 3, 7, 8, 6, 7, 5, 7, 10, 6, 8, 6, 12]


def series_fixture(counts: list[int] = WEEKLY, *,
                   unmerged: int = 0) -> Fixture:
    """One story per delivery, one week apart; the last week is in progress."""
    fx = Fixture()
    first = T0
    # the snapshot is the Monday after the counted weeks, so the partial week is
    # the bucket that contains it (and the counted weeks are the whole sample)
    fx.as_of = first + timedelta(days=7 * len(counts), hours=12)
    fx.sprint(3, first, "2026-04-24", state="active")
    number = 0
    for week, count in enumerate(counts):
        week_start = first + timedelta(days=7 * week)
        for offset in range(count):
            number += 1
            key = f"DEL-{number}"
            midnight = week_start + timedelta(hours=1)
            fx.story(key, midnight)
            fx.add_to_sprint(key, midnight, 3)
            # Monday belongs to its own bucket; the done boundary is Wednesday
            done = midnight + timedelta(days=2)
            fx.event(key, done, "status_changed",
                     **{"from_status": "in_progress", "to_status": "done"})
            if number <= unmerged:
                # Jira done while a named PR is still open: unmerged, not delivered
                fx.pr(number, (key,), done - timedelta(days=1))
    # the last counted week is real; add one *partial* week on top (in progress)
    partial = first + timedelta(days=7 * len(counts), hours=10)
    fx.story("DEL-PARTIAL", partial)
    fx.event("DEL-PARTIAL", partial, "status_changed",
             **{"from_status": "in_progress", "to_status": "done"})
    return fx


def closed_sprint_fixture() -> Fixture:
    """Four weeks of history, then a closed sprint (Feb 2 → Feb 16) of five.

    The forecast for that sprint is a **backtest**: the sample must stop at
    Feb 2, so it sees four weeks — fewer than the eight a forecast needs.
    """
    fx = Fixture()
    first = T0
    fx.as_of = first + timedelta(days=7 * 14, hours=12)      # Mon 13 Apr
    fx.sprint(2, first + timedelta(days=28), "2026-02-16", state="closed",
              end=datetime(2026, 2, 16, 16))
    number = 0

    def deliver(week_start: datetime, count: int, sid: int | None) -> None:
        nonlocal number
        for _ in range(count):
            number += 1
            key = f"DEL-{number}"
            moment = week_start + timedelta(hours=1)
            fx.story(key, moment)
            if sid is not None:
                fx.add_to_sprint(key, moment, sid)
            done = moment + timedelta(days=2)
            fx.event(key, done, "status_changed",
                     **{"from_status": "in_progress", "to_status": "done"})

    for week in range(4):                                    # Jan 5 → Feb 2
        deliver(first + timedelta(days=7 * week), 2, None)
    for week in range(4, 6):                                 # Feb 2 → Feb 16
        deliver(first + timedelta(days=7 * week), 2 + (week - 4), 2)
    return fx


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fx: Fixture):
        fx.write(self.path)
        con = connect(self.path, read_only=True)
        court = ballcourt.load(con)
        data = jira.load(con, court)
        window = rules.window_for(court, None, data)
        return con, court, data, window


# --------------------------------------------------------------------------- #
# 1. the simulation contract
# --------------------------------------------------------------------------- #
class QuantileTests(unittest.TestCase):
    """series [0,1,2,3], 3 periods, 10 trials, seed 42 — draws listed by hand:

    002 111 003 000 110 131 320 132 211 200 → sums 2,3,3,0,2,5,5,6,4,2
    """

    SERIES = {
        "values": [0, 1, 2, 3], "unit": "stories", "mean": 1.5,
        "sd": 1.12, "cv": 0.75, "n": 4,
    }

    def block(self):
        block = forecast.forecast(
            self.SERIES, 5, 3, 42, trials=10, start="2026-11-30",
        )
        block.pop("_sums")
        return block

    def test_quantiles_are_the_hand_computed_order_statistics(self):
        block = self.block()
        # capped sums: 2,3,3,0,2,5,5,5,4,2 → P15=2, P50=3, P85=5
        self.assertEqual(block["how_many"], {"p15": 2, "p50": 3, "p85": 5})
        self.assertEqual(block["uncapped"], {"p15": 2, "p50": 3, "p85": 5})

    def test_p_reach_is_the_uncapped_share(self):
        # three of ten sums reach 5 (5, 5, 6)
        self.assertEqual(self.block()["p_reach_target"], 0.3)

    def test_when_is_counted_from_the_period_index(self):
        when = self.block()["when"]
        self.assertEqual(when["p50_weeks"], 3)
        self.assertEqual(when["p85_weeks"], 5)
        self.assertEqual(when["p95_weeks"], 6)
        self.assertEqual(when["p50_date"], "2026-12-20")
        self.assertEqual(when["p85_date"], "2027-01-03")
        self.assertEqual(when["unfinished_share"], 0.0)

    def test_a_date_is_the_last_day_of_the_period(self):
        # the PRD's own example: 5 weeks from 30 Nov is 3 Jan, not 4 Jan
        self.assertEqual(forecast.periods_to_date(5, "2026-11-30"), "2027-01-03")
        self.assertEqual(forecast.periods_to_date(1, "2026-11-30"), "2026-12-06")
        self.assertIsNone(forecast.periods_to_date(0, "2026-11-30"))

    def test_the_same_seed_is_the_identical_interval(self):
        first = self.block()
        second = self.block()
        self.assertEqual(first["how_many"], second["how_many"])
        self.assertEqual(first["seed"], second["seed"])

    def test_quantiles_are_monotone_and_capped_at_the_target(self):
        block = forecast.forecast(
            {"values": [0, 2, 4], "n": 3}, 6, 4, 7, trials=2000,
        )
        block.pop("_sums")
        self.assertLessEqual(block["how_many"]["p15"], block["how_many"]["p50"])
        self.assertLessEqual(block["how_many"]["p50"], block["how_many"]["p85"])
        self.assertLessEqual(block["how_many"]["p85"], 6)

    def test_p_reach_is_monotone_in_periods(self):
        values = [0, 2, 4]
        reached = [
            forecast.forecast({"values": values, "n": 3}, 6, periods, 1,
                              trials=5000)["p_reach_target"]
            for periods in (1, 2, 4, 8)
        ]
        self.assertEqual(reached, sorted(reached))
        self.assertEqual(reached[0], 0.0)

    def test_p_reach_curve_is_exact_and_monotone(self):
        block = forecast.forecast(
            {"values": [0, 1, 2, 3], "n": 4}, 5, 3, 42, trials=10,
        )
        curve = forecast.p_reach_curve(block["_sums"], maximum=8)
        p = {row["n"]: row["p"] for row in curve}
        self.assertEqual(p[5], block["p_reach_target"])
        self.assertEqual(p[0], 1.0)
        self.assertEqual([p[n] for n in sorted(p)], sorted(p.values(), reverse=True))

    def test_capping_is_min_of_the_uncapped_quantile(self):
        block = forecast.forecast(
            {"values": [0, 1, 2, 3], "n": 4}, 5, 3, 42, trials=10,
        )
        block.pop("_sums")
        for key in ("p15", "p50", "p85"):
            self.assertEqual(
                min(block["uncapped"][key], 4), min(block["how_many"][key], 4)
            )


# --------------------------------------------------------------------------- #
# 2. the series
# --------------------------------------------------------------------------- #
class ThroughputTests(StoreCase):
    def test_the_weekly_counts_are_the_hand_recount(self):
        con, court, data, window = self.load(series_fixture())
        try:
            series = forecast.throughput(court, data, window)
        finally:
            con.close()
        self.assertEqual(series["values"], WEEKLY)
        self.assertEqual(series["n"], 13)
        self.assertEqual(series["basis"], "stories delivered / week")
        self.assertTrue(series["buckets"][-1]["in_progress"])

    def test_the_in_progress_bucket_is_excluded(self):
        con, court, data, window = self.load(series_fixture())
        try:
            series = forecast.throughput(court, data, window)
        finally:
            con.close()
        partial = [row for row in series["buckets"] if row["in_progress"]]
        self.assertEqual(len(partial), 1)
        # DEL-PARTIAL is done in the partial week; it must not enter the sample
        self.assertEqual(sum(series["values"]), sum(WEEKLY))

    def test_an_unmerged_story_is_not_delivered(self):
        con, court, data, window = self.load(series_fixture(unmerged=1))
        try:
            series = forecast.throughput(court, data, window)
        finally:
            con.close()
        self.assertEqual(series["values"], [1] + WEEKLY[1:])
        self.assertEqual(series["n"], 13)

    def test_mean_sd_and_cv_are_the_observed_weeks(self):
        con, court, data, window = self.load(series_fixture())
        try:
            series = forecast.throughput(court, data, window)
        finally:
            con.close()
        expected_mean = sum(WEEKLY) / len(WEEKLY)
        expected_sd = (sum((v - expected_mean) ** 2 for v in WEEKLY) / len(WEEKLY)) ** 0.5
        self.assertAlmostEqual(series["mean"], round(expected_mean, 2))
        self.assertAlmostEqual(series["sd"], round(expected_sd, 2))
        self.assertAlmostEqual(series["cv"], round(expected_sd / expected_mean, 2))

    def test_a_closed_sprint_is_a_backtest(self):
        con, court, data, window = self.load(closed_sprint_fixture())
        try:
            resolved = forecast.resolve_request(
                court, data, target="sprint:2", n=None, by="sprint",
                as_of=court.snapshot,
            )
            payload = forecast.payload(
                court, data,
                (window[0], forecast.parse_moment(resolved["cutoff"])),
                n=resolved["n"], periods=resolved["periods"],
                target="sprint:2", by=resolved["by"], tenant="t",
                as_of=court.snapshot.isoformat(), sprint=resolved["sprint"],
                delivered_now=resolved["delivered_now"],
                start=resolved["start"], backtest=True,
            )
        finally:
            con.close()
        self.assertTrue(resolved["backtest"])
        self.assertEqual(resolved["cutoff"], "2026-02-02T09:00:00")
        self.assertEqual(resolved["start"], datetime(2026, 2, 2, 9, 0))
        self.assertEqual(resolved["delivered_now"], 0)
        self.assertEqual(resolved["by"], "2026-02-16")
        self.assertEqual(resolved["periods"], 2)
        # four weeks of history at the sprint's start: not a forecast
        self.assertFalse(payload["available"])
        self.assertIn("sparse", [row["code"] for row in payload["assess"]["reasons"]])
        self.assertIsNone(payload["forecast"])
        self.assertTrue(payload["backtest"])

    def test_pr_basis_fallback_changes_the_unit(self):
        fx = Fixture()
        for number in range(1, 4):
            opened = T0 + timedelta(days=7 * number)
            fx.pr(number, ("DEL-1",), opened, state="merged")
        con, court, data, window = self.load(fx)
        try:
            series = forecast.throughput(court, data, window)
        finally:
            con.close()
        self.assertEqual(series["source"], "merged_prs")
        self.assertEqual(series["unit"], "PRs")
        self.assertEqual(series["basis"], "PRs merged / week")
        assessment = forecast.assess(series)
        self.assertIn("pr_basis", [row["code"] for row in assessment["warnings"]])


# --------------------------------------------------------------------------- #
# 3. the refusals — exact copy, no numbers
# --------------------------------------------------------------------------- #
GOOD = {"values": WEEKLY, "n": 13, "mean": 6.69, "sd": 2.52, "cv": 0.38,
        "unit": "stories"}


class RefusalTests(unittest.TestCase):
    def test_sparse_history_refuses_with_the_count_and_the_minimum(self):
        series = {"values": [2, 3, 4], "n": 3, "mean": 3.0, "sd": 1.0, "cv": 0.33}
        assessment = forecast.assess(series, None)
        self.assertFalse(assessment["available"])
        reason = assessment["reasons"][0]
        self.assertEqual(reason["code"], "sparse")
        self.assertEqual(reason["reason"], "insufficient_history")
        self.assertEqual(
            reason["sentence"],
            "Not enough history to forecast: 3 weeks observed, 8 needed.",
        )
        self.assertEqual(reason["minimum"], 8)

    def test_one_week_is_singular_and_not_called_erratic(self):
        series = {"values": [3], "n": 1, "mean": 3.0, "sd": 0.0, "cv": None}
        assessment = forecast.assess(series)
        self.assertEqual([row["code"] for row in assessment["reasons"]], ["sparse"])
        self.assertEqual(
            assessment["reasons"][0]["sentence"],
            "Not enough history to forecast: 1 week observed, 8 needed.",
        )

    def test_erratic_throughput_refuses_with_its_cv(self):
        values = [0, 9, 1, 8, 0, 10, 1, 9]
        series = {"values": values, "n": len(values), "mean": 4.75,
                  "sd": 4.3, "cv": 0.9}
        assessment = forecast.assess(series, None)
        self.assertFalse(assessment["available"])
        self.assertEqual(assessment["reasons"][0]["code"], "erratic")
        self.assertEqual(
            assessment["reasons"][0]["sentence"],
            "Throughput is erratic (CV 0.90): the range would be so wide it "
            "would look like a forecast and behave like a guess.",
        )

    def test_a_flat_zero_series_is_erratic(self):
        series = {"values": [0] * 9, "n": 9, "mean": 0.0, "sd": 0.0, "cv": None}
        self.assertIn("erratic", [row["code"] for row in forecast.assess(series)["reasons"]])

    def test_prd61_overload_refuses_with_the_exact_hand_off(self):
        # the wiring is prd/61's own classifier and its refuses_forecast flag
        flag = stability.classify(1.0, 0.0)[0]
        self.assertEqual(flag, "overload")
        wip = {
            "flag": flag,
            "refuses_forecast": stability.refuses_forecast(flag),
            "wip": {"from": 6, "to": 17, "slope_per_week": 1.0},
            "throughput": {"slope_per_week": 0.0},
        }
        assessment = forecast.assess(GOOD, wip)
        self.assertFalse(assessment["available"])
        reason = assessment["reasons"][0]
        self.assertEqual(reason["code"], "overload")
        self.assertEqual(reason["reason"], "wip_overload")
        self.assertEqual(
            reason["sentence"],
            "WIP rose 6 → 17 while throughput did not follow — Little's Law "
            "says cycle time will grow, so a throughput forecast would be "
            "optimistic. Fix the queue first (prd/61).",
        )

    def test_filling_warns_and_does_not_refuse(self):
        flag = stability.classify(1.0, 0.29)[0]
        self.assertEqual(flag, "filling")
        wip = {
            "flag": flag,
            "refuses_forecast": stability.refuses_forecast(flag),
            "wip": {"from": 6, "to": 17, "slope_per_week": 1.0},
            "throughput": {"slope_per_week": 0.29},
        }
        assessment = forecast.assess(GOOD, wip)
        self.assertTrue(assessment["available"])
        self.assertEqual(assessment["reasons"], [])
        self.assertEqual(assessment["warnings"][0]["code"], "wip_filling")
        self.assertIn("the interval is optimistic; plan at P85",
                      assessment["warnings"][0]["sentence"])

    def test_a_refused_payload_carries_no_forecast_numbers(self):
        fx = series_fixture(counts=[1, 2, 1])          # three weeks
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.duckdb"
            fx.write(path)
            con = connect(path, read_only=True)
            try:
                court = ballcourt.load(con)
                data = jira.load(con, court)
                window = rules.window_for(court, None, data)
                payload = forecast.payload(
                    court, data, window, n=10, periods=2,
                    target="open", by="2026-04-24", tenant="t",
                    as_of="2026-04-06T12:00:00",
                )
            finally:
                con.close()
        self.assertFalse(payload["available"])
        self.assertIsNone(payload["forecast"])
        self.assertIsNone(payload["adjust"])
        self.assertEqual(payload["note"], payload["assess"]["reasons"][0]["sentence"])


# --------------------------------------------------------------------------- #
# 4. the risk adjustment
# --------------------------------------------------------------------------- #
class AdjustTests(unittest.TestCase):
    def test_the_two_counts_are_never_blended(self):
        sample = {"values": WEEKLY, "n": 13, "mean": 6.69, "sd": 2.52,
                  "cv": 0.38, "unit": "stories"}
        block = forecast.forecast(sample, 30, 4, 123, trials=2000)
        block.pop("_sums")
        risk = {"rows": [
            {"key": "DEL-167", "reason": "done-but-unmerged", "who": ["FrMo"],
             "waiting_hours": 144.0, "detail": {"prs": [53]},
             "label": "Done in Jira, PR unmerged"},
            {"key": "DEL-176", "reason": "stuck-on", "who": ["FrMo"],
             "waiting_hours": 131.6, "detail": {"prs": [60]},
             "label": "stuck on a person"},
        ]}
        adjustment = forecast.adjust(block, risk, 30)
        self.assertEqual(adjustment["at_risk_count"], 2)
        self.assertEqual(adjustment["doubtful"], 1)
        self.assertEqual(adjustment["ceiling_if_they_do_not"], 29)
        self.assertEqual(
            adjustment["how_many_if_the_at_risk_merge"], block["how_many"]
        )
        self.assertLessEqual(
            adjustment["how_many_if_they_do_not"]["p85"],
            adjustment["ceiling_if_they_do_not"],
        )
        # only the doubtful row lowers the ceiling; the stuck-on row stays
        self.assertEqual(adjustment["delivered_if_the_blocked_items_hold"], 29)


# --------------------------------------------------------------------------- #
# 5. the del-combined evidence, when present
# --------------------------------------------------------------------------- #
@unittest.skipUnless(_del_combined_path() is not None,
                     "the del-combined store is not present")
class DelCombinedForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = _del_combined_path()
        con = connect(path, read_only=True)
        try:
            cls.court = ballcourt.load(con)
            cls.data = jira.load(con, cls.court)
        finally:
            con.close()
        cls.window = rules.window_for(cls.court, None, cls.data)

    def test_the_series_recounts_the_store(self):
        series = forecast.throughput(self.court, self.data, self.window)
        # 13 settled weeks, the partial week excluded; DEL-167/168/183 are
        # unmerged, so they are not delivered (the sprint lens agrees).
        self.assertEqual(series["n"], 13)
        self.assertEqual(
            series["values"],
            [2, 3, 7, 8, 6, 7, 5, 7, 10, 4, 8, 5, 12],
        )
        self.assertAlmostEqual(series["mean"], 6.46, places=2)
        self.assertAlmostEqual(series["cv"], 0.41, places=2)

    def test_filling_warns_and_the_forecast_is_available(self):
        payload = forecast.payload(
            self.court, self.data, self.window,
            n=30, periods=4, target="open", by="2026-12-15",
            tenant="del-combined",
            as_of=self.court.snapshot.isoformat(),
        )
        self.assertTrue(payload["available"])
        self.assertEqual(payload["wip_flag"], "filling")
        self.assertEqual(
            [row["code"] for row in payload["assess"]["warnings"]],
            ["wip_filling"],
        )
        self.assertIsNotNone(payload["forecast"])

    def test_the_sprint_target_is_the_committed_scope(self):
        resolved = forecast.resolve_request(
            self.court, self.data, target="sprint:3", n=None, by="sprint",
            as_of=self.court.snapshot,
        )
        self.assertEqual(resolved["n"], 30)
        self.assertEqual(resolved["delivered_now"], 25)
        self.assertEqual(resolved["periods"], 2)
        self.assertEqual(resolved["by"], "2026-11-27")


# --------------------------------------------------------------------------- #
# 6. the API: payload, cache, tenant scoping
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


class ForecastApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        series_fixture().write(cls.dir / "del.duckdb")
        series_fixture().write(cls.dir / "other.duckdb")
        series_fixture(counts=[2, 1, 1]).write(cls.dir / "small.duckdb")
        closed_sprint_fixture().write(cls.dir / "backtest.duckdb")
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(
                app_module, "CALENDAR_CONFIG", cls.dir / "no-calendar.yaml"
            ),
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
        with app_module._forecast_lock:
            app_module._forecast_cache.clear()
            app_module._forecast_series_cache.clear()

    def test_the_payload_is_an_interval_with_its_provenance(self):
        body = get("/api/forecast", tenant="del", n=30, days=3650).json()
        self.assertEqual(body["ok"], True)
        self.assertTrue(body["available"])
        block = body["forecast"]
        self.assertLess(block["how_many"]["p15"], block["how_many"]["p85"])
        self.assertEqual(block["trials"], 10000)
        self.assertIn("Method: 10,000 resamples of 13 weeks", body["method"])
        self.assertIn("r ≈ −0.05", body["method"])
        self.assertEqual(block["p_reach_curve"][block["capped_at"]]["p"],
                         block["p_reach_target"])
        self.assertIn("–", block["headline"])
        self.assertEqual(body["request"]["confidence"], "P15–P85")

    def test_two_fetches_are_the_same_interval_and_hit_the_cache(self):
        first = get("/api/forecast", tenant="del", n=30, days=3650).json()
        second = get("/api/forecast", tenant="del", n=30, days=3650).json()
        self.assertEqual(first["forecast"]["seed"], second["forecast"]["seed"])
        self.assertEqual(first["forecast"]["how_many"], second["forecast"]["how_many"])
        self.assertEqual(first, second)
        self.assertEqual(len(app_module._forecast_cache), 1)

    def test_a_refusal_body_has_no_numbers(self):
        body = get("/api/forecast", tenant="small", n=10, days=3650).json()
        self.assertFalse(body["available"])
        self.assertIsNone(body["forecast"])
        self.assertEqual(body["assess"]["reasons"][0]["code"], "sparse")
        self.assertIn("Not enough history to forecast", body["note"])

    def test_an_empty_target_says_nothing_to_forecast(self):
        body = get("/api/forecast", tenant="small", days=3650).json()
        self.assertEqual(body["delegate"]["reason"], "nothing to forecast")
        self.assertIsNone(body["forecast"])

    def test_the_sprint_parameter_is_the_same_target(self):
        one = get("/api/forecast", tenant="del", target="sprint:3", days=3650).json()
        two = get("/api/forecast", tenant="del", sprint=3, days=3650).json()
        self.assertEqual(one["request"]["target"], "sprint:3")
        self.assertEqual(two["request"]["target"], "sprint:3")
        self.assertEqual(one["forecast"], two["forecast"])

    def test_a_small_sprint_delegates_to_prd52(self):
        body = get("/api/forecast", tenant="small", target="sprint:3",
                   days=3650).json()
        self.assertIsNotNone(body["delegate"])
        self.assertEqual(body["delegate"]["to"], "prd/52")
        self.assertIsNone(body["forecast"])

    def test_a_closed_sprint_forecast_is_a_backtest(self):
        body = get("/api/forecast", tenant="backtest", target="sprint:2",
                   days=3650).json()
        self.assertTrue(body["backtest"])
        self.assertFalse(body["available"])
        self.assertIsNone(body["forecast"])
        self.assertIn("sparse", [row["code"] for row in body["assess"]["reasons"]])
        self.assertLess(body["throughput"]["n"], 8)

    def test_tenant_scoping_keeps_the_seeds_apart(self):
        reply = get("/api/forecast", tenant="del", n=30, days=3650)
        one = reply.json()
        two = get("/api/forecast", tenant="other", n=30, days=3650).json()
        self.assertEqual(reply.headers.get("x-tenant"), "del")
        self.assertEqual(one["tenant"], "del")
        self.assertEqual(two["tenant"], "other")
        # the seed is crc32(tenant|…): the same request in another tenant is a
        # different interval, and its cache entry never answers for this one
        self.assertNotEqual(one["forecast"]["seed"], two["forecast"]["seed"])
        self.assertEqual(one["forecast"]["how_many"], two["forecast"]["how_many"])
        self.assertEqual(one["forecast"]["trials"], two["forecast"]["trials"])

    def test_bad_n_and_bad_by_are_400s(self):
        self.assertEqual(get("/api/forecast", tenant="del", n=-1).status, 400)
        self.assertEqual(get("/api/forecast", tenant="del", by="nope").status, 400)
        self.assertEqual(
            get("/api/forecast", tenant="del", confidence="p50").status, 400
        )

    def test_sprints_links_each_band_to_the_one_forecast_endpoint(self):
        body = get("/api/sprints", tenant="del", days=3650).json()
        for band in body["sprints"]:
            self.assertEqual(
                band["forecast_url"], f"/api/forecast?target=sprint:{band['sprint_id']}"
            )


if __name__ == "__main__":
    unittest.main()

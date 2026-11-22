"""prd/59 — aging WIP: in-flight age against the team's own percentiles.

Every number here is pinned against a **synthetic store with hand-written
events** (no fixture generator, no randomness, no calendar — so a business hour
is one elapsed hour and the recompute is exact). The tests cover:

* the percentiles equal a hand-sorted manual recompute (nearest-rank,
  ``index = round(q × (n − 1))``) and the p75 time in review;
* the band boundaries at exactly p50/p85/p95 (p85 is ``aging``, p95 ``very
  old``), with ties counted as ``≤`` in ``rank_pct``;
* ``chase_order`` = live rows past p85, ranked by ``age − p85``, and ``dead``
  rows excluded (listed under ``replan``);
* the story clock (created → snapshot, and entered_stage → snapshot) against
  ``story_cycle`` and the per-stage cohorts;
* ``rule_thresholds`` moving ``stale-prs``/``sentinel-tail`` off 5 bd / 24 h,
  quoting the source, and ``MEGASPEED_AGING_THRESHOLDS=fixed`` restoring them;
* a cohort below ``MIN_N`` → ``available: False``, no percentile and no chase.

The API half (payload shape, cache, tenant scoping, 400 on an unknown stage)
lives in ``tests/test_server.py`` (``AgingEndpointTests``), like the other
endpoints.

    .venv/bin/python -m unittest tests.test_aging -v
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from analysis import aging, ballcourt, jira
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run

REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)
AS_OF = datetime(2026, 3, 1, 12, 0)


# --------------------------------------------------------------------------- #
# a synthetic store with hand-written PRs, stories and events
# --------------------------------------------------------------------------- #
class Fixture:
    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.objects: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.opened: dict[int, datetime] = {}
        self._count = 0

    # -- PRs ---------------------------------------------------------------- #
    def pr(self, number: int, opened: datetime, *, state: str = "open",
           closed: datetime | None = None, tickets: tuple[str, ...] = ()) -> "Fixture":
        self.opened[number] = opened
        self.objects.append({
            "object_type": "PR",
            "object_id": f"PR:{REPO}#{number}",
            "repo": REPO,
            "pr_id": number,
            "ticket_keys": list(tickets),
            "actor": "AaAa",
            "state": state,
            "created_at": opened,
            "closed_at": closed,
            "head_commit": "a" * 40,
            "message_class": None,
            "attributes": {},
        })
        self.event(number, "pr_opened", "AaAa", 0)
        return self

    def event(self, number: int, activity: str, actor: str, hours: float,
              people: tuple[str, ...] = (), **attributes) -> "Fixture":
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
        for person in people:
            self.relations.append((key, "PERSON", f"PERSON:{person}"))
        return self

    def reviewed(self, number: int, merged_after: float | None = None) -> "Fixture":
        """One reviewer is asked at opening; optionally the PR merges later."""
        self.event(number, "reviewer_changed", "AaAa", 0,
                   people=("ReVw",), change="added")
        if merged_after is not None:
            self.event(number, "pr_merged", "AaAa", merged_after)
        return self

    # -- stories ------------------------------------------------------------ #
    def story(self, key: str, created: datetime, *, done: datetime | None = None,
              in_review: datetime | None = None, points: int = 3,
              events: list[tuple[datetime, str, str]] | None = None,
              actor: str = "AaAa") -> "Fixture":
        self.objects.append({
            "object_type": "STORY",
            "object_id": f"STORY:{key}",
            "repo": None,
            "pr_id": None,
            "ticket_keys": [key],
            "actor": actor,
            "state": "done" if done else "open",
            "created_at": created,
            "closed_at": done,
            "head_commit": None,
            "message_class": None,
            "attributes": {"points": points},
        })
        rows = [(created, "created", None, "LaAd")]
        if in_review:
            rows.append((in_review, "status_changed", "in_review", "AaAa"))
        if done:
            rows.append((done, "status_changed", "done", "AaAa"))
        rows.extend(events or [])
        for stamp, activity, stage, actor in rows:
            self._count += 1
            attributes = {"to_status": stage} if stage else {}
            self.events.append({
                "event_key": f"STORY:{key}:{self._count}",
                "ts": stamp,
                "activity": activity,
                "actor": actor,
                "repo": None,
                "pr_id": None,
                "attributes": attributes,
                "classifier_version": "deterministic-v1",
                "content_hash": hashlib.blake2b(
                    f"{key}:{self._count}".encode(), digest_size=8,
                ).hexdigest(),
                "as_of": self.as_of,
            })
            self.relations.append(
                (f"STORY:{key}:{self._count}", "STORY", f"STORY:{key}")
            )
        return self

    # -- materialise -------------------------------------------------------- #
    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-aging",
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
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fixture: Fixture):
        fixture.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con, calendar=None)
            data = jira.load(con, court)
        finally:
            con.close()
        return fixture, court, data


def review_store() -> Fixture:
    """20 merged PRs whose review times are 1..20 h, plus open PRs at known ages."""
    fx = Fixture()
    for hours in range(1, 21):
        number = 100 + hours
        opened = T0 + timedelta(hours=10 * hours)
        fx.pr(number, opened, state="merged", closed=opened + timedelta(hours=hours))
        fx.reviewed(number, merged_after=hours)
    # open items in review, created `hours` before the snapshot
    for number, hours in (
        (1, 5), (2, 12), (3, 17), (4, 19), (5, 19), (6, 40),
    ):
        opened = AS_OF - timedelta(hours=hours)
        fx.pr(number, opened, state="open", tickets=(f"ENG-{number}",))
        fx.reviewed(number)
    # one PR with author work before review: 10 h in review, 30 h of cycle
    cycle_opened = AS_OF - timedelta(hours=30)
    fx.pr(7, cycle_opened, state="open", tickets=("ENG-7",))
    # the author works for 20 h, then asks for review: 10 h in review, 30 h cycle
    fx.event(7, "pr_pushed", "AaAa", 20)
    fx.event(7, "reviewer_changed", "AaAa", 20, people=("ReVw",), change="added")
    return fx


# --------------------------------------------------------------------------- #
# 1. percentiles match a manual recompute
# --------------------------------------------------------------------------- #
class PercentileTests(StoreCase):
    def test_review_percentiles_equal_a_hand_sorted_recompute(self):
        fixture, court, data = self.load(review_store())
        block = aging.review_thresholds(court)
        assert block["n"] == 20 and block["available"] is True
        # the manual recompute: sorted 1..20, index = round(q × (n − 1))
        values = sorted(aging.review_hours(state) for state in court.prs
                        if state.pr["state"] == "merged")
        self.assertEqual(values, [float(hour) for hour in range(1, 21)])

        def manual(q):
            return values[round(q * (len(values) - 1))]

        self.assertEqual(block["p50"], manual(0.50))
        self.assertEqual(block["p75"], manual(0.75))
        self.assertEqual(block["p85"], manual(0.85))
        self.assertEqual(block["p95"], manual(0.95))
        # pinned literals so a formula change fails loudly, not silently
        self.assertEqual((block["p50"], block["p75"], block["p85"], block["p95"]),
                         (11.0, 15.0, 17.0, 19.0))
        self.assertIn("merged PRs", block["basis"])
        self.assertEqual(block["min_n"], aging.MIN_N)

    def test_review_hours_is_a_manual_sum_of_the_review_intervals(self):
        fixture, court, data = self.load(review_store())
        state = next(s for s in court.prs if s.pr["pr_id"] == 105)
        intervals = [iv.hours for iv in state.intervals if iv.phase == "review"]
        self.assertEqual(aging.review_hours(state), round(sum(intervals), 2))
        self.assertEqual(aging.review_hours(state), 5.0)
        # the open #3 has been in review exactly 17 h (its whole life)
        open_state = next(s for s in court.prs if s.pr["pr_id"] == 3)
        self.assertEqual(aging.review_hours(open_state), 17.0)

    def test_rank_pct_counts_ties_as_less_or_equal(self):
        self.assertEqual(aging.rank_pct(17, [1, 17, 17, 20]), 0.75)
        self.assertEqual(aging.rank_pct(17, [1, 2, 3]), 1.0)
        self.assertEqual(aging.rank_pct(0, [1, 2, 3]), 0.0)
        self.assertIsNone(aging.rank_pct(None, [1, 2]))


# --------------------------------------------------------------------------- #
# 2. bands, at exactly p50 / p85 / p95
# --------------------------------------------------------------------------- #
class BandTests(StoreCase):
    def test_band_boundaries_are_half_open_and_exactly_p85_is_aging(self):
        block = {"available": True, "p50": 11.0, "p85": 17.0, "p95": 19.0}
        self.assertEqual(aging.band_for(10.99, block), "normal")
        self.assertEqual(aging.band_for(11.0, block), "watch")     # exactly p50
        self.assertEqual(aging.band_for(16.99, block), "watch")
        self.assertEqual(aging.band_for(17.0, block), "aging")     # exactly p85
        self.assertEqual(aging.band_for(18.99, block), "aging")
        self.assertEqual(aging.band_for(19.0, block), "very_old")  # exactly p95
        self.assertEqual(aging.band_for(500, block), "very_old")
        self.assertEqual(aging.band_for(None, block), "insufficient")
        self.assertEqual(
            aging.band_for(500, {"available": False, "p50": None}),
            "insufficient",
        )
        # the vocabulary is closed and "stale" is not one of its words
        self.assertEqual(aging.BANDS,
                         ("normal", "watch", "aging", "very_old", "insufficient"))
        self.assertNotIn("stale", aging.BANDS)

    def test_rows_carry_the_band_rank_and_multiple(self):
        fixture, court, data = self.load(review_store())
        rows = {row["pr"]: row for row in aging.in_flight(court, data, "pr")}
        self.assertEqual(rows["#3"]["age_hours"], 17.0)
        self.assertEqual(rows["#3"]["band"], "aging")
        self.assertEqual(rows["#3"]["rank_pct"], 0.85)   # 17 of 20 ≤ 17
        self.assertEqual(rows["#3"]["cohort_n"], 20)
        self.assertEqual(rows["#3"]["multiple"], 1.0)     # exactly p85
        self.assertEqual(rows["#4"]["band"], "very_old")  # exactly p95
        self.assertEqual(rows["#1"]["band"], "normal")
        self.assertEqual(rows["#2"]["band"], "watch")
        self.assertEqual(rows["#6"]["hours_at_risk"], 23.0)  # 40 − 17
        # every row states its clock; the story and PR clocks are never mixed
        self.assertTrue(all(row["basis"] == aging.BASIS_REVIEW for row in rows.values()))


# --------------------------------------------------------------------------- #
# 3. the chase list
# --------------------------------------------------------------------------- #
class ChaseTests(StoreCase):
    def test_chase_order_is_hours_at_risk_desc_and_excludes_dead(self):
        fixture, court, data = self.load(review_store())
        rows = aging.in_flight(court, data, "pr")
        chase = aging.chase_order(rows)
        self.assertEqual([row["pr"] for row in chase], ["#6", "#4", "#5", "#3"])
        risks = [row["hours_at_risk"] for row in chase]
        self.assertEqual(risks, sorted(risks, reverse=True))
        self.assertEqual(risks, [23.0, 2.0, 2.0, 0.0])

        # a dead row (no live PR) is never chased; it is listed for re-planning
        dead = dict(rows[0], pr="#dead", object_id="STORY:ENG-9", kind="story",
                    live="dead", band="very_old", hours_at_risk=900.0,
                    open_prs=[], age_hours=1000.0)
        self.assertEqual(
            [row["pr"] for row in aging.chase_order(rows + [dead])],
            ["#6", "#4", "#5", "#3"],
        )
        self.assertEqual([row["object_id"] for row in aging.replan(rows + [dead])],
                         ["STORY:ENG-9"])

    def test_the_feed_is_the_same_ranked_list(self):
        fixture, court, data = self.load(review_store())
        payload = aging.payload(court, data, kind="pr")
        self.assertEqual(
            payload["feed"]["chase"],
            ["PR:ENG/platform#6", "PR:ENG/platform#4",
             "PR:ENG/platform#5", "PR:ENG/platform#3"],
        )
        self.assertEqual(payload["feed"]["hours_at_risk"], 27.0)
        self.assertEqual(payload["feed"]["threshold_hours"], 17.0)
        self.assertIn("p85 of 20 merged PRs (17 h)", payload["feed"]["threshold_source"])
        self.assertEqual(
            [item["pr"] for item in payload["feed"]["items"]],
            ["#6", "#4", "#5", "#3"],
        )
        self.assertEqual(payload["counts"]["chase"], 4)


# --------------------------------------------------------------------------- #
# 4. insufficient history
# --------------------------------------------------------------------------- #
class InsufficientTests(StoreCase):
    def small_store(self) -> Fixture:
        fx = Fixture()
        for hours in range(1, 6):                 # five merged PRs, not twenty
            number = 200 + hours
            opened = T0 + timedelta(hours=10 * hours)
            fx.pr(number, opened, state="merged",
                  closed=opened + timedelta(hours=hours))
            fx.reviewed(number, merged_after=hours)
        opened = AS_OF - timedelta(hours=30)
        fx.pr(1, opened, state="open")
        fx.reviewed(1)
        return fx

    def test_a_small_cohort_has_no_percentile_and_no_chase(self):
        fixture, court, data = self.load(self.small_store())
        block = aging.review_thresholds(court)
        self.assertEqual(block["n"], 5)
        self.assertFalse(block["available"])
        self.assertIsNone(block["p50"])
        self.assertIsNone(block["p85"])
        self.assertIsNone(block["p75"])
        self.assertIn("insufficient history", block["note"])
        payload = aging.payload(court, data, kind="pr")
        row = payload["rows"][0]
        self.assertEqual(row["band"], "insufficient")
        self.assertIsNone(row["multiple"])
        self.assertEqual(row["hours_at_risk"], 0.0)
        self.assertEqual(payload["feed"]["chase"], [])
        self.assertEqual(payload["counts"]["insufficient"], 1)
        self.assertEqual(payload["thresholds"][0]["available"], False)

    def test_rule_thresholds_fall_back_and_say_so(self):
        fixture, court, data = self.load(self.small_store())
        thresholds = aging.rule_thresholds(court, data)
        self.assertFalse(thresholds["stale_prs"]["available"])
        self.assertEqual(thresholds["stale_prs"]["threshold_hours"], 40.0)
        self.assertIn("insufficient history", thresholds["stale_prs"]["source"])
        self.assertIn("fixed", thresholds["stale_prs"]["source"])


# --------------------------------------------------------------------------- #
# 5. the rules' constants move (and the fixed mode restores them)
# --------------------------------------------------------------------------- #
class RuleThresholdTests(StoreCase):
    def test_stale_prs_uses_the_p85_of_merged_prs_and_quotes_it(self):
        fixture, court, data = self.load(review_store())
        thresholds = aging.rule_thresholds(court, data)
        stale = thresholds["stale_prs"]
        self.assertTrue(stale["available"])
        self.assertEqual(stale["threshold_hours"], 17.0)
        self.assertEqual(stale["threshold_days"], 2.12)
        self.assertEqual(stale["source"], "p85 of 20 merged PRs (17 h)")
        self.assertEqual(thresholds["mode"], "self")

    def test_the_sentinel_tail_uses_the_story_review_segment(self):
        fx = Fixture()
        # completed stories with a measured review segment (first review → approval)
        for index in range(20):
            key = f"ENG-{100 + index}"
            created = T0 + timedelta(hours=index)
            done = created + timedelta(hours=60 + index)
            fx.story(key, created, done=done)
            # a PR that gives the story its first_review/approval boundaries
            number = 300 + index
            fx.pr(number, created, state="merged", closed=done, tickets=(key,))
            fx.event(number, "reviewer_changed", "AaAa", 1,
                     people=("ReVw",), change="added")
            fx.event(number, "comment_added", "ReVw", 2)
            fx.event(number, "pr_approved", "ReVw", 10 + index)
            fx.event(number, "pr_merged", "AaAa", 60 + index)
        fixture, court, data = self.load(fx)
        segments = jira.segment_cohort(court, data)
        self.assertTrue(segments.get("review_rounds"))
        expected = aging.percentile(segments["review_rounds"], 0.85)
        thresholds = aging.rule_thresholds(court, data)
        self.assertEqual(thresholds["sentinel_tail"]["threshold_hours"], expected)
        self.assertIn("completed stories' review segment",
                      thresholds["sentinel_tail"]["source"])
        # it moved off the old 24 h constant (or says why it could not)
        if thresholds["sentinel_tail"]["available"] and expected != 24.0:
            self.assertNotEqual(thresholds["sentinel_tail"]["threshold_hours"], 24.0)

    def test_fixed_mode_restores_the_old_constants(self):
        fixture, court, data = self.load(review_store())
        with mock.patch.dict(os.environ, {aging.FIXED_MODE_ENV: "fixed"}):
            thresholds = aging.rule_thresholds(court, data)
        self.assertEqual(thresholds["mode"], "fixed")
        self.assertEqual(thresholds["stale_prs"]["threshold_hours"], 40.0)
        self.assertIn("fixed (5 business days)", thresholds["stale_prs"]["source"])
        self.assertEqual(thresholds["sentinel_tail"]["threshold_hours"], 24.0)
        self.assertIn("fixed (24 h)", thresholds["sentinel_tail"]["source"])
        # the self-calibrated value still travels, labelled
        self.assertEqual(thresholds["stale_prs"]["self_calibrated"], 17.0)

    def test_insights_quote_the_calibrated_threshold_in_the_evidence(self):
        from analysis import rules
        fixture, court, data = self.load(review_store())
        window = rules.window_for(court, None, data)
        computed = rules.metrics(court, [], window, jira=data)
        self.assertEqual(computed["aging"]["stale_prs"]["threshold_hours"], 17.0)
        self.assertEqual(computed["stale_prs"]["limit_hours"], 17.0)
        self.assertEqual(computed["stale_prs"]["threshold_source"],
                         "p85 of 20 merged PRs (17 h)")


# --------------------------------------------------------------------------- #
# 6. the story clock: cycle, stage and liveness
# --------------------------------------------------------------------------- #
class StoryTests(StoreCase):
    def story_store(self) -> Fixture:
        fx = Fixture()
        # 20 completed stories: cycles 10..29 h, in review from +2 h
        for index in range(20):
            key = f"DEL-{200 + index}"
            created = T0 + timedelta(hours=index)
            hours = 10 + index
            fx.story(
                key, created,
                in_review=created + timedelta(hours=2),
                done=created + timedelta(hours=hours),
            )
        # a live story: open, has an open PR, 40 h old (very old: p85 = 27)
        live_created = AS_OF - timedelta(hours=40)
        fx.story("DEL-300", live_created, in_review=AS_OF - timedelta(hours=40))
        fx.pr(500, live_created + timedelta(hours=1), state="open",
              tickets=("DEL-300",))
        # dead: no PR, no human event for 500 h
        fx.story("DEL-301", AS_OF - timedelta(hours=1000),
                 events=[(AS_OF - timedelta(hours=500), "comment_added", None, "AaAa")])
        # unobserved: no PR, a human event an hour ago
        fx.story("DEL-302", AS_OF - timedelta(hours=1000),
                 events=[(AS_OF - timedelta(hours=1), "comment_added", None, "AaAa")])
        return fx

    def test_story_cycle_thresholds_and_stage_cohort(self):
        fixture, court, data = self.load(self.story_store())
        blocks = aging.story_thresholds(court, data)
        self.assertTrue(blocks["story_cycle"]["available"])
        self.assertEqual(blocks["story_cycle"]["n"], 20)
        # cycles 10..29 → p50 at round(.5×19)=10 → 20
        self.assertEqual(blocks["story_cycle"]["p50"], 20.0)
        # cycles 10..29 → p85 at round(.85×19) = 16 → 26
        self.assertEqual(blocks["story_cycle"]["p85"], 26.0)
        self.assertEqual(blocks["story_cycle"]["p95"], 28.0)
        # the in-review dwell is cycle − 2 h, per completed story
        in_review = blocks["stage:in_review"]
        self.assertTrue(in_review["available"])
        self.assertEqual(in_review["p50"], 18.0)
        self.assertEqual(in_review["p85"], 24.0)
        self.assertEqual(in_review["min_n"], aging.MIN_N)
        # a stage the store never completed is absent from the selector
        self.assertEqual(aging.stages_for("story", court, data), ["cycle", "in_review"])

    def test_story_rows_use_the_story_clock_and_the_stage_clock(self):
        fixture, court, data = self.load(self.story_store())
        cycle_rows = {row["ticket"]: row for row in aging.in_flight(court, data, "story")}
        self.assertEqual(set(cycle_rows), {"DEL-300", "DEL-301", "DEL-302"})
        self.assertEqual(cycle_rows["DEL-300"]["age_hours"], 40.0)
        self.assertEqual(cycle_rows["DEL-300"]["band"], "very_old")
        self.assertEqual(cycle_rows["DEL-300"]["live"], "live")
        self.assertEqual(cycle_rows["DEL-301"]["live"], "dead")
        self.assertEqual(cycle_rows["DEL-302"]["live"], "unobserved")
        # prd/68 F-03: a `dead` story still names its owner — the holder is who
        # the row waits on, and on a Jira-only store that is the assignee.
        self.assertEqual(cycle_rows["DEL-301"]["holder"],
                         [{"person": "AaAa", "role": "owner",
                           "hours": cycle_rows["DEL-301"]["age_hours"]}])
        self.assertNotEqual(cycle_rows["DEL-301"]["holder"], [])
        self.assertEqual(cycle_rows["DEL-300"]["basis"], aging.BASIS_STORY_CYCLE)

        # only the live story is *currently* in review: a story with no status
        # transition has no stage clock, so it is absent rather than an empty 0
        stage_rows = {
            row["ticket"]: row
            for row in aging.in_flight(court, data, "story", "in_review")
        }
        self.assertEqual(set(stage_rows), {"DEL-300"})
        row = stage_rows["DEL-300"]
        self.assertEqual(row["age_hours"], 40.0)   # entered in_review at creation
        self.assertEqual(row["stage"], "in_review")
        self.assertEqual(row["basis"], aging.BASIS_STAGE)
        self.assertEqual(stage_rows["DEL-300"]["band"], "very_old")  # p85 = 25

    def test_chase_keeps_the_live_story_and_replans_the_rest(self):
        fixture, court, data = self.load(self.story_store())
        payload = aging.payload(court, data, kind="story")
        self.assertEqual(payload["feed"]["chase"], ["STORY:DEL-300"])
        self.assertEqual(payload["counts"]["dead"], 1)
        self.assertEqual(
            [item["ticket"] for item in payload["feed"]["replan"]],
            ["DEL-301", "DEL-302"],   # equal age → stable object_id order
        )
        # prd/68 F-06: `only=live` hides the abandoned rows from the *table* but
        # not from the re-plan feed — the hidden note says "see re-plan", so the
        # section has to be there. The feed is window-scoped, not `only`-scoped.
        live_only = aging.payload(court, data, kind="story", only="live")
        self.assertEqual([row["ticket"] for row in live_only["rows"]], ["DEL-300"])
        self.assertEqual([item["ticket"] for item in live_only["feed"]["replan"]],
                         ["DEL-301", "DEL-302"])
        # prd/68 F-08: "no live PR" is the re-plan population (dead+unobserved),
        # which the chip reads; `dead` keeps the abandoned-only meaning.
        self.assertEqual(live_only["counts"]["no_live_pr"], 2)
        self.assertEqual(live_only["counts"]["dead"], 0)
        self.assertEqual([row["ticket"] for row in payload["rows"]],
                         ["DEL-300", "DEL-301", "DEL-302"])


# --------------------------------------------------------------------------- #
# 7. the payload contract
# --------------------------------------------------------------------------- #
class PayloadTests(StoreCase):
    def test_payload_shape_and_counts(self):
        fixture, court, data = self.load(review_store())
        payload = aging.payload(court, data, kind="pr")
        for key in ("ok", "as_of", "hours_mode", "window", "thresholds", "rows",
                    "counts", "review_p75", "feed", "note", "bands", "min_n",
                    "stages", "active_threshold"):
            self.assertIn(key, payload)
        self.assertEqual(payload["hours_mode"], "wall-clock")   # no calendar here
        self.assertEqual(payload["counts"]["open"], 7)
        self.assertEqual(payload["counts"]["over_p85"], 4)      # #6, #4, #5, #3
        self.assertEqual(payload["review_p75"]["value"], 15.0)
        self.assertEqual(payload["review_p75"]["n"], 20)
        self.assertEqual(payload["bands"], list(aging.BANDS))
        self.assertEqual(payload["feed"]["replan"], [])
        row = payload["rows"][0]
        for key in ("kind", "object_id", "ticket", "stage", "entered",
                    "age_hours", "calendar_days", "band", "rank_pct",
                    "cohort_n", "multiple", "holder", "live", "reason",
                    "dependents", "href", "basis", "hours_at_risk"):
            self.assertIn(key, row)
        self.assertTrue(row["href"].startswith("#ticket/"))

    def test_an_unknown_stage_is_a_stated_error_listing_the_stages(self):
        fixture, court, data = self.load(review_store())
        with self.assertRaises(ValueError) as caught:
            aging.payload(court, data, kind="pr", stage="nope")
        self.assertIn("review", str(caught.exception))
        with self.assertRaises(ValueError):
            aging.payload(court, data, kind="bogus")

    def test_the_pr_cycle_is_a_second_clock_not_a_sum(self):
        fixture, court, data = self.load(review_store())
        review = aging.payload(court, data, kind="pr", stage="review")
        cycle = aging.payload(court, data, kind="pr", stage="cycle")
        self.assertEqual(review["active_threshold"], "review_time")
        self.assertEqual(cycle["active_threshold"], "pr_cycle")
        review_row = next(row for row in review["rows"] if row["pr"] == "#7")
        cycle_row = next(row for row in cycle["rows"] if row["pr"] == "#7")
        self.assertEqual(review_row["age_hours"], 10.0)
        self.assertEqual(cycle_row["age_hours"], 30.0)
        self.assertTrue(all(row["basis"] == aging.BASIS_PR_CYCLE
                            for row in cycle["rows"]))

    def test_limit_truncates_the_table_but_not_the_counts(self):
        fixture, court, data = self.load(review_store())
        payload = aging.payload(court, data, kind="pr", limit=2)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["counts"]["open"], 7)
        self.assertEqual(len(payload["feed"]["items"]), 4)


if __name__ == "__main__":
    unittest.main()

"""Analysis → suggested rules (prd/17): metrics, templates, ranking, API.

Every metric is pinned against a **synthetic store with hand-computed events**
(no fixture generator, no randomness), so a change in a threshold, a median or
an attribution rule fails with the exact number that moved. Each metric test
also asserts the suggestion the metric emits: a metric nobody can turn into a
"change this" rule is not the product.

The API half runs the real ASGI app against a temp tenant directory, like
``test_tenants``: no running server, no ``data/``.

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

from analysis import advice, ballcourt, rules
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from server import app as app_module

REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)
AS_OF = datetime(2026, 3, 1, 12, 0)


# --------------------------------------------------------------------------- #
# a synthetic store with hand-written events/objects
# --------------------------------------------------------------------------- #
class Fixture:
    """Build a store from explicit PRs, events, threads and people."""

    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.objects: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.opened: dict[int, datetime] = {}
        self._count = 0

    # -- PRs and events ----------------------------------------------------- #
    def pr(self, number: int, author: str, opened: datetime = T0,
           state: str = "open", closed: datetime | None = None,
           tickets: tuple[str, ...] = ("ENG-1",),
           attributes: dict | None = None) -> "Fixture":
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
            "attributes": dict(attributes or {}),
        })
        self.event(number, "pr_opened", author, 0)
        return self

    def commit(self, number: int) -> "Fixture":
        """One COMMIT object on the PR (the store's size signal for commits)."""
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
            "created_at": self.opened[number],
            "closed_at": None,
            "head_commit": sha,
            "message_class": "feature",
            "attributes": {},
        })
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

    # -- objects ------------------------------------------------------------ #
    def thread(self, number: int, index: int, comment_type: str | None = None,
               outcome: str | None = None, created: datetime | None = None,
               state: str = "resolved") -> "Fixture":
        attributes = {"severity": "suggestion", "anchor_class": "general"}
        if comment_type:
            attributes["comment_type"] = comment_type
            attributes["thread_outcome"] = outcome or "unresolved"
        self.objects.append({
            "object_type": "THREAD",
            "object_id": f"THREAD:{REPO}#{number}:{index}",
            "repo": REPO,
            "pr_id": number,
            "ticket_keys": [],
            "actor": None,
            "state": state,
            "created_at": created or self.opened[number],
            "closed_at": None,
            "head_commit": None,
            "message_class": None,
            "attributes": attributes,
        })
        return self

    def person(self, pseudonym: str, kind: str = "human") -> "Fixture":
        self.objects.append({
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
        return self

    # -- materialise -------------------------------------------------------- #
    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-rules",
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

    def court(self, path: Path) -> ballcourt.BallCourt:
        con = connect(path, read_only=True)
        try:
            return ballcourt.load(con)
        finally:
            con.close()

    def threads(self, path: Path) -> list[dict]:
        con = connect(path, read_only=True)
        try:
            return rules.load_threads(con)
        finally:
            con.close()


class StoreCase(unittest.TestCase):
    """One temp store per test; helpers to load it the way the API does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def metric(self, fixture: Fixture, name: str, days: float | None = None,
               caps: dict | None = None):
        court, threads, sizes, _bots = self.load(fixture)
        window = rules.window_for(court, days)
        return rules.metrics(court, threads, window, sizes, caps=caps)[name]

    def insights(self, fixture: Fixture, level: str = "team", days: float | None = None,
                 caps: dict | None = None, policy_lab: dict | None = None):
        court, threads, sizes, bots = self.load(fixture)
        window = rules.window_for(court, days)
        return rules.insights(
            court, threads, window, level=level, bots=bots, sizes=sizes, caps=caps,
            policy_lab=policy_lab,
        )

    def load(self, fixture: Fixture):
        fixture.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            return (
                ballcourt.load(con),
                rules.load_threads(con),
                rules.load_pr_sizes(con),
                rules.load_bots(con),
            )
        finally:
            con.close()


def by_id(payload: dict, suggestion_id: str) -> dict | None:
    return next((s for s in payload["suggestions"] if s["id"] == suggestion_id), None)


# --------------------------------------------------------------------------- #
# 1. time_to_first_review
# --------------------------------------------------------------------------- #
class TimeToFirstReviewTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.pr(1, "AaAa")
        fx.event(1, "comment_added", "RrRr", 10, severity="suggestion")
        fx.pr(2, "AaAa")
        fx.event(2, "pr_approved", "RrRr", 40)
        fx.pr(3, "AaAa")
        # the author's own comment is not a review action
        fx.event(3, "comment_added", "AaAa", 5, severity="suggestion")
        fx.event(3, "comment_added", "SsSs", 100, severity="suggestion")
        fx.pr(4, "AaAa")
        fx.event(4, "pr_pushed", "AaAa", 12, push_class="content")
        return fx

    def test_median_p90_and_missing_are_measured(self):
        metric = self.metric(self.fixture(), "time_to_first_review")
        self.assertEqual(metric["n"], 3)
        self.assertEqual(metric["missing"], 1)
        self.assertEqual(metric["median_hours"], 40.0)
        self.assertEqual(metric["p90_hours"], 100.0)
        self.assertEqual(metric["over_target"], 2)
        self.assertEqual(metric["by_reviewer"]["RrRr"]["median_hours"], 25.0)
        self.assertEqual(metric["by_reviewer"]["SsSs"]["n"], 1)

    def test_emits_a_review_sla_rule_with_impact(self):
        payload = self.insights(self.fixture())
        suggestion = by_id(payload, "review-sla")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["metric"], "time_to_first_review")
        self.assertEqual(suggestion["severity"], "warning")
        self.assertEqual(suggestion["impact_hours"], 48.0)   # (40 − 24) × 3
        self.assertEqual(suggestion["evidence"]["prs"], 3)
        self.assertEqual(suggestion["evidence"]["no_review_yet"], 1)
        self.assertIn("24 h", suggestion["suggestion"])

    def test_a_healthy_median_with_a_long_tail_emits_an_info_rule(self):
        fx = Fixture()
        for number, first_review in enumerate((2, 4, 6, 10, 60), start=1):
            fx.pr(number, "AaAa")
            fx.event(number, "comment_added", "RrRr", first_review, severity="suggestion")
        payload = self.insights(fx)
        self.assertIsNone(by_id(payload, "review-sla"))
        suggestion = by_id(payload, "review-tail")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["severity"], "info")
        self.assertEqual(suggestion["impact_hours"], 18.0)   # (60 − 24) × 1 × 0.5


# --------------------------------------------------------------------------- #
# 2. cycle_time / time_to_merge
# --------------------------------------------------------------------------- #
class CycleTimeTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        for number, hours in enumerate((30, 60, 100, 240), start=1):
            fx.pr(number, "AaAa", state="merged",
                  closed=T0 + timedelta(hours=hours))
            fx.event(number, "pr_approved", "RrRr", hours - 2)
            fx.event(number, "pr_merged", "RrRr", hours)
        return fx

    def test_median_and_p90_use_merged_prs_only(self):
        fx = self.fixture()
        fx.pr(99, "AaAa", state="open")          # never merged: not a cycle
        metric = self.metric(fx, "cycle_time")
        self.assertEqual(metric["n"], 4)
        self.assertEqual(metric["median_hours"], 80.0)
        self.assertEqual(metric["p90_hours"], 240.0)
        self.assertEqual(metric["over_target"], 2)

    def test_emits_a_cycle_time_rule(self):
        suggestion = by_id(self.insights(self.fixture()), "cycle-time")
        self.assertIsNotNone(suggestion)
        # concepts.md §1: the "week" gate is 40 business hours (5 d), not 168 h
        # (7 × 24). An 80 h median is two working weeks, so it is critical.
        self.assertEqual(suggestion["severity"], "critical")
        self.assertEqual(suggestion["impact_hours"], 32.0)   # (80 − 72) × 4
        self.assertEqual(suggestion["evidence"]["merged_prs"], 4)


# --------------------------------------------------------------------------- #
# 3. reviewer_responsiveness
# --------------------------------------------------------------------------- #
class ResponseTimeTests(StoreCase):
    """prd/42 M1: added as reviewer → first response, and the sentinel gate."""

    def fixture(self) -> Fixture:
        fx = Fixture()
        # PR 1: two reviewers added; CaOs answers after 10 h, FrMo (sentinel)
        # after 40 h. Every PR waits ~2.5 h for a peer and ~20 h for the gate.
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=60))
        fx.event(1, "reviewer_changed", "AaAa", 0, change="added",
                 people=("FrMo", "CaOs"))
        fx.event(1, "comment_added", "CaOs", 8, severity="suggestion")
        fx.event(1, "pr_approved", "CaOs", 10)
        fx.event(1, "pr_approved", "FrMo", 40, sentinel=True)
        fx.event(1, "pr_merged", "FrMo", 60)
        # PR 2: the sentinel is asked and never answers; a peer answers in 4 h.
        # The PR is still open: a merged/declined PR was never the sentinel's
        # turn (prd/46 §2), so it must not inflate "never answered".
        fx.pr(2, "BbBb", state="open")
        fx.event(2, "reviewer_changed", "BbBb", 0, change="added",
                 people=("FrMo", "XxXx"))
        fx.event(2, "pr_approved", "XxXx", 4)
        return fx

    def test_response_medians_and_the_never_answered_count(self):
        court = self.load(self.fixture())[0]
        body = ballcourt.response_times(court)
        people = {row["person"]: row for row in body["people"]}
        self.assertEqual(people["CaOs"]["median_hours"], 8.0)
        self.assertEqual(people["CaOs"]["n"], 1)
        self.assertEqual(people["CaOs"]["never"], 0)
        self.assertEqual(people["XxXx"]["median_hours"], 4.0)
        # prd/43 §2: the sentinel's clock opens at the last peer approval (10 h),
        # not at "added as reviewer" (0 h): 40 − 10 = 30 h, not 40 h.
        self.assertEqual(people["FrMo"]["median_hours"], 30.0)
        # asked twice, answered once: "slow" and "asleep" are different facts
        self.assertEqual(people["FrMo"]["never"], 1)
        self.assertEqual(people["FrMo"]["role"], "sentinel")
        # the peer baseline is the median of the non-sentinel medians
        self.assertEqual(body["reviewer_median_hours"], 6.0)
        self.assertEqual(body["sentinels"], ["FrMo"])

    def test_never_only_counts_requests_that_were_actually_owed(self):
        """prd/46 §2: the old 24 was 16 closed PRs + 8 gates that never opened."""
        fx = Fixture()
        # merged without the sentinel answering: ex post not required
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=20))
        fx.event(1, "reviewer_changed", "AaAa", 0, change="added",
                 people=("FrMo", "CaOs"))
        fx.event(1, "pr_approved", "CaOs", 4)
        fx.event(1, "pr_merged", "CaOs", 20)
        # open, but the peer never answered, so the sentinel's gate never opened
        fx.pr(2, "BbBb", state="open")
        fx.event(2, "reviewer_changed", "BbBb", 0, change="added",
                 people=("FrMo", "DaKo"))
        # open and the gate opened: this one is a real never-answered
        fx.pr(3, "CcCc", state="open")
        fx.event(3, "reviewer_changed", "CcCc", 0, change="added",
                 people=("FrMo", "ErLi"))
        fx.event(3, "pr_approved", "ErLi", 3)
        # declare the sentinel role (the store records it on the sentinel's own
        # event; a global role, so one declaration is enough)
        fx.pr(4, "DdDd", state="open")
        fx.event(4, "reviewer_changed", "FrMo", 0, change="removed",
                 sentinel=True)
        body = ballcourt.response_times(self.load(fx)[0])
        fr = next(row for row in body["people"] if row["person"] == "FrMo")
        self.assertEqual(fr["never"], 1)
        self.assertEqual(body["never_excluded"], {
            "closed_prs": 1, "gate_not_open": 1,
        })
        self.assertGreater(fr["never_oldest_hours"], 0)
        # one canonical number, one definition, stated in the payload
        canonical = body["sentinel_wait"]
        self.assertEqual(canonical["definition"], ballcourt.SENTINEL_WAIT_DEFINITION)
        self.assertIn("last peer answer", canonical["definition"])
        self.assertIsNone(canonical["median_hours"])   # no sentinel response yet
        self.assertEqual(canonical["never"], 1)

    def test_the_sentinel_lag_is_peer_approval_to_sentinel_approval(self):
        court = self.load(self.fixture())[0]
        body = ballcourt.response_times(court)
        fr = next(row for row in body["people"] if row["person"] == "FrMo")
        self.assertEqual(fr["sentinel_lag_n"], 1)
        self.assertEqual(fr["sentinel_lag_median_hours"], 30.0)   # 10 → 40
        self.assertIn("peer approval", body["sentinel_lag_basis"])

    def test_the_sentinel_response_clock_is_gate_aware(self):
        """prd/43 §2: ~6 h, not the ~20 h "added as reviewer" clock.

        FrMo is added at open, every peer approves by 14 h and he answers at
        20 h. The old clock priced the whole 20 h; the precedence gate opened
        at 14 h, so his own lag is 6 h and the "×N slower" label is computed
        from that, not from the peer phase.
        """
        fx = Fixture()
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=26))
        fx.event(1, "reviewer_changed", "AaAa", 0, change="added",
                 people=("FrMo", "CaOs", "DaKo"))
        fx.event(1, "pr_approved", "CaOs", 8)
        fx.event(1, "pr_approved", "DaKo", 14)          # the gate opens here
        fx.event(1, "pr_approved", "FrMo", 20, sentinel=True)
        fx.event(1, "pr_merged", "FrMo", 26)
        court = self.load(fx)[0]
        body = ballcourt.response_times(court)
        fr = next(row for row in body["people"] if row["person"] == "FrMo")
        self.assertEqual(fr["role"], "sentinel")
        self.assertEqual(fr["median_hours"], 6.0)
        # the separate peer → sentinel column agrees: one fact, one number
        self.assertEqual(fr["sentinel_lag_median_hours"], 6.0)
        self.assertEqual(body["gate_aware_n"], 1)
        self.assertTrue(body["sentinel_after_peers"])
        self.assertIn("last peer", body["basis"])
        # peer clocks are unchanged: CaOs 0 → 8
        caos = next(row for row in body["people"] if row["person"] == "CaOs")
        self.assertEqual(caos["median_hours"], 8.0)

    def test_the_gate_opens_at_the_last_peer_answer_not_only_an_approval(self):
        """concepts.md §2: "answered" includes a comment or needs-work."""
        fx = Fixture()
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=26))
        fx.event(1, "reviewer_changed", "AaAa", 0, change="added",
                 people=("FrMo", "CaOs", "DaKo"))
        fx.event(1, "pr_approved", "CaOs", 8)
        fx.event(1, "pr_approved", "DaKo", 10)
        fx.event(1, "comment_added", "DaKo", 16, severity="suggestion")
        fx.event(1, "pr_approved", "FrMo", 20, sentinel=True)
        fx.event(1, "pr_merged", "FrMo", 26)
        court = self.load(fx)[0]
        fr = next(
            row for row in ballcourt.response_times(court)["people"]
            if row["person"] == "FrMo"
        )
        # 20 − 16 (the last peer answer), not 20 − 10 (the last approval)
        self.assertEqual(fr["median_hours"], 4.0)

    def test_the_forgotten_sentinel_requests_stay_surfaced(self):
        """prd/43 §2: the orphans (#55/#60) are a quantity, not a lost count."""
        fx = Fixture()
        fx.pr(1, "AaAa")                     # open, the ticket is waiting
        fx.event(1, "reviewer_changed", "AaAa", 0, change="added",
                 people=("FrMo", "CaOs"))
        fx.event(1, "pr_approved", "CaOs", 4)   # the gate opened 4 h in; no answer
        court = self.load(fx)[0]
        body = ballcourt.response_times(court)
        fr = next(row for row in body["people"] if row["person"] == "FrMo")
        self.assertEqual(fr["never"], 1)
        self.assertIsNotNone(fr["never_oldest_hours"])
        self.assertGreater(fr["never_oldest_hours"], 0)

    def test_the_queue_payload_carries_the_comparison(self):
        court = self.load(self.fixture())[0]
        body = ballcourt.queue_series(court, "person", rules.window_for(court, None))
        self.assertIn("response", body)
        self.assertEqual(body["response"]["reviewer_median_hours"], 6.0)
        # the legend names the sentinel role without a real name (prd/02)
        legend = ballcourt.person_legend(court)
        roles = {row["person"]: row["role"] for row in legend["people"]}
        self.assertEqual(roles["FrMo"], "sentinel")
        self.assertIn("pseudonyms only", legend["note"])


class ResponsivenessTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        # XxXx: 100 h review ball, 5 h author ball -> flagged
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=120))
        fx.event(1, "pr_needs_work", "XxXx", 10)
        fx.event(1, "pr_pushed", "AaAa", 15, push_class="content")
        fx.event(1, "pr_approved", "XxXx", 115)
        fx.event(1, "pr_merged", "XxXx", 120)
        # YyYy: 10 h review ball, 10 h author ball -> not flagged
        fx.pr(2, "BbBb")
        fx.event(2, "pr_needs_work", "YyYy", 10)
        fx.event(2, "pr_pushed", "BbBb", 20, push_class="content")
        fx.event(2, "pr_approved", "YyYy", 30)
        # ZzZz: 35 h median review ball, 10 h author ball -> flagged
        fx.pr(3, "CcCc")
        fx.event(3, "pr_needs_work", "ZzZz", 10)
        fx.event(3, "pr_pushed", "CcCc", 20, push_class="content")
        fx.event(3, "pr_approved", "ZzZz", 80)
        # XxXx and ZzZz author their own PRs: the contrast needs both sides
        fx.pr(4, "XxXx")
        fx.event(4, "pr_needs_work", "YyYy", 10)
        fx.event(4, "pr_pushed", "XxXx", 15, push_class="content")
        fx.event(4, "pr_approved", "YyYy", 20)
        fx.pr(5, "ZzZz")
        fx.event(5, "pr_needs_work", "YyYy", 10)
        fx.event(5, "pr_pushed", "ZzZz", 20, push_class="content")
        fx.event(5, "pr_approved", "YyYy", 30)
        return fx

    def test_review_ball_vs_author_ball_per_person(self):
        metric = self.metric(self.fixture(), "reviewer_responsiveness")
        people = {p["person"]: p for p in metric["people"]}
        self.assertEqual(people["XxXx"]["review_median_hours"], 55.0)
        self.assertEqual(people["XxXx"]["author_median_hours"], 5.0)
        self.assertTrue(people["XxXx"]["flagged"])
        self.assertFalse(people["YyYy"]["flagged"])
        self.assertEqual(people["ZzZz"]["review_median_hours"], 35.0)
        self.assertEqual(metric["flagged"], ["XxXx", "ZzZz"])
        self.assertEqual(metric["spread_hours"], 45.0)      # 55 − 10

    def test_person_level_rules_name_pseudonyms_only(self):
        payload = self.insights(self.fixture(), level="person")
        xx = by_id(payload, "reviewer-XxXx")
        self.assertIsNotNone(xx)
        self.assertEqual(xx["severity"], "warning")
        self.assertEqual(xx["impact_hours"], 62.0)          # (55 − 24) × 2
        self.assertEqual(xx["scope"], {"kind": "person", "person": "XxXx"})
        self.assertEqual(by_id(payload, "reviewer-ZzZz")["impact_hours"], 22.0)
        # team level must not name a person
        team = self.insights(self.fixture(), level="team")
        self.assertIsNone(by_id(team, "reviewer-XxXx"))

    def test_team_level_reports_the_spread_without_names(self):
        suggestion = by_id(self.insights(self.fixture(), level="team"), "reviewer-spread")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["evidence"]["flagged_reviewers"], 2)
        self.assertEqual(suggestion["evidence"]["worst_median_hours"], 55.0)
        self.assertEqual(suggestion["evidence"]["fastest_median_hours"], 10.0)
        self.assertEqual(suggestion["impact_hours"], 84.0)  # 62 + 22
        self.assertNotIn("XxXx", json.dumps(suggestion["evidence"]))
        self.assertNotIn("XxXx", suggestion["title"])

    def test_bots_are_never_coached(self):
        fx = self.fixture()
        fx.person("XxXx", kind="bot")
        payload = self.insights(fx, level="person")
        self.assertIsNone(by_id(payload, "reviewer-XxXx"))
        self.assertIsNotNone(by_id(payload, "reviewer-ZzZz"))


# --------------------------------------------------------------------------- #
# 4. rework_loops
# --------------------------------------------------------------------------- #
class ReworkLoopTests(StoreCase):
    """prd/40 + concepts.md §6: one definition of a review round.

    Every PR has a first review first, because a push before anyone has looked
    is setup, not rework. The three old over-counts are pinned here: the
    pre-first-review push, the pure rebase that lost nothing, and the
    push-induced unapproval (the same round as its push).
    """

    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=100))
        fx.event(1, "comment_added", "RrRr", 5, severity="suggestion")  # first review
        fx.event(1, "pr_pushed", "AaAa", 10, push_class="content")
        fx.event(1, "pr_pushed", "AaAa", 20, push_class="content")
        fx.event(1, "pr_unapproved", "RrRr", 30, cause="push")   # not a second round
        fx.event(1, "pr_approved", "RrRr", 50)
        fx.event(1, "pr_merged", "RrRr", 100)
        fx.pr(2, "AaAa")
        fx.event(2, "comment_added", "RrRr", 5, severity="suggestion")
        fx.event(2, "pr_pushed", "AaAa", 10, push_class="pure_rebase")  # head move
        fx.event(2, "pr_pushed", "AaAa", 20, push_class="rebase_conflict")
        fx.pr(3, "AaAa")
        fx.pr(4, "AaAa")
        fx.event(4, "comment_added", "RrRr", 5, severity="suggestion")
        for hours in (10, 20, 30, 40):
            fx.event(4, "pr_pushed", "AaAa", hours, push_class="content")
        return fx

    def test_rounds_are_split_by_push_class(self):
        metric = self.metric(self.fixture(), "rework_loops")
        self.assertEqual(metric["n"], 4)
        # PR1: 2 content pushes (the cause=push unapproval is the same round)
        # PR2: 1 rebase_conflict (the pure rebase lost no approval)
        # PR3: 0, PR4: 4 content pushes
        self.assertEqual(metric["total_rounds"], 7)
        self.assertEqual(metric["median_rounds"], 1.5)
        self.assertEqual(metric["p90_rounds"], 4.0)
        # prd/46 §3: the target is the lab's best-scenario cap (2); the
        # conformance threshold (3) is still reported as `max_rounds`.
        self.assertEqual(metric["target_rounds"], 2)
        self.assertEqual(metric["max_rounds"], 3)
        self.assertEqual(metric["prs_over_target"], 1)
        self.assertEqual(metric["excess_rounds"], 2)   # PR4: 4 rounds − cap 2
        self.assertEqual(metric["by_push_class"]["content"], {"pushes": 6, "prs": 2})
        self.assertEqual(metric["by_push_class"]["rebase_conflict"], {"pushes": 1, "prs": 1})
        # the exclusions are reported, not silently dropped
        self.assertNotIn("pure_rebase", metric["by_push_class"])
        self.assertEqual(metric["excluded_pure_rebase"], 1)
        self.assertEqual(metric["excluded_push_unapprovals"], 1)
        self.assertEqual(metric["excluded_pre_first_review"], 0)

    def test_a_push_before_the_first_review_is_not_a_round(self):
        fx = Fixture()
        fx.pr(1, "AaAa")
        fx.event(1, "pr_pushed", "AaAa", 10, push_class="content")
        fx.event(1, "pr_approved", "RrRr", 20)
        metric = self.metric(fx, "rework_loops")
        self.assertEqual(metric["total_rounds"], 0)
        self.assertEqual(metric["excluded_pre_first_review"], 1)

    def test_emits_a_round_cap_rule(self):
        suggestion = by_id(self.insights(self.fixture()), "round-cap")
        self.assertIsNotNone(suggestion)
        # one closed PR: 100 h cycle ÷ 2 rounds = 50 h per round; the excess is
        # measured against the lab's cap (2), so PR4's 4 rounds give 2 excess.
        self.assertAlmostEqual(suggestion["impact_hours"], 100.0, places=1)
        self.assertEqual(suggestion["evidence"]["excess_rounds"], 2)
        self.assertEqual(suggestion["evidence"]["prs_over_target"], 1)
        # prd/46 §3: the rule, the metric and the lab all quote the cap 2; the
        # conformance threshold stays 3 and is not presented as the target.
        self.assertEqual(suggestion["evidence"]["round_cap"], 2)
        self.assertIn("target 2 max", suggestion["title"])
        self.assertNotIn("cap them at 3", suggestion["title"])
        self.assertIn("Cap review rounds at 2", suggestion["suggestion"])

    def test_the_round_cap_rule_carries_the_correlation_and_the_lab(self):
        """prd/43 §1: r ≈ 0.6, and the lab's round-cap is the best scenario.

        Six merged PRs with rounds 0-3 and a rising cycle: the metric reports
        the correlation and the marginal hours per round, the rule ranks on
        them, and the policy-lab note (supplied by the server) says the model
        supports it. This is the lever the QA said was not a Rules item at all.
        """
        fx = Fixture()
        for number, (rounds, cycle) in enumerate(
            ((0, 20), (1, 30), (1, 34), (2, 45), (2, 52), (3, 70)), start=1
        ):
            fx.pr(number, "AaAa", state="merged", closed=T0 + timedelta(hours=cycle))
            fx.event(number, "comment_added", "RrRr", 1, severity="suggestion")
            for index in range(rounds):
                fx.event(number, "pr_pushed", "AaAa", 2 + index * 3,
                         push_class="content")
            fx.event(number, "pr_approved", "RrRr", cycle - 1)
            fx.event(number, "pr_merged", "RrRr", cycle)
        metric = self.metric(fx, "rework_loops")
        self.assertEqual(metric["rounds_cycle_n"], 6)
        self.assertGreater(metric["rounds_cycle_correlation"], 0.9)
        self.assertGreater(metric["rounds_cycle_slope_hours"], 0)
        self.assertEqual(metric["median_cycle_by_rounds"]["0"], 20.0)
        report = advice.report({
            "roundcap2": {
                "cycle_median_hours": {"percent": -13.0, "direction": "down",
                                       "better": True},
                "throughput_work_per_day": {"percent": 4.7, "direction": "up",
                                            "better": True},
                "within_noise": False,
                "verdict": "cycle 13.0 % shorter, delivered work 4.7 % more",
                "verdict_tone": "good",
            },
        })
        payload = self.insights(fx, policy_lab=report)
        suggestion = by_id(payload, "round-cap")
        self.assertIsNotNone(suggestion)
        self.assertIn("r = ", suggestion["title"])
        self.assertEqual(suggestion["evidence"]["rounds_cycle_correlation"],
                         metric["rounds_cycle_correlation"])
        self.assertTrue(suggestion["policy_lab"]["supports"])
        self.assertIn("−13 % cycle", suggestion["policy_lab"]["note"])
        # the one-line cross-reference the Rules page shows
        self.assertEqual(
            payload["policy_lab"]["best"]["label"], "cap review at 2 rounds"
        )
        self.assertIn("the lab's best scenario is cap review at 2 rounds",
                      payload["policy_lab"]["note"])


# --------------------------------------------------------------------------- #
# 5. rebase_waste
# --------------------------------------------------------------------------- #
class RebaseWasteTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.pr(1, "AaAa")
        fx.event(1, "pr_pushed", "AaAa", 10, push_class="pure_rebase")
        fx.event(1, "pr_unapproved", "RrRr", 20, cause="push")
        fx.pr(2, "AaAa")
        fx.event(2, "pr_pushed", "AaAa", 10, push_class="content")
        fx.event(2, "pr_unapproved", "RrRr", 20, cause="push")
        fx.pr(3, "AaAa")
        fx.event(3, "pr_pushed", "AaAa", 10, push_class="pure_rebase")
        fx.event(3, "pr_unapproved", "RrRr", 20, cause="manual")
        return fx

    def test_attribution_follows_the_causing_push(self):
        metric = self.metric(self.fixture(), "rebase_waste")
        self.assertEqual(metric["n"], 3)
        self.assertEqual(metric["lost_on_pure_rebase"], 1)
        self.assertEqual(metric["lost_on_content"], 1)
        self.assertEqual(metric["lost_manual"], 1)
        self.assertEqual(metric["prs_affected"], 3)
        self.assertEqual(metric["share_pure_rebase"], 0.33)

    def test_emits_a_no_rebase_rule(self):
        suggestion = by_id(self.insights(self.fixture()), "rebase-waste")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["severity"], "warning")
        # no closed PR: one round falls back to the median review-ball wait (10 h)
        self.assertEqual(suggestion["impact_hours"], 10.0)
        self.assertEqual(suggestion["evidence"]["approvals_lost"], 1)
        self.assertIn("patch-id", suggestion["suggestion"])


# --------------------------------------------------------------------------- #
# 6. comment_tax
# --------------------------------------------------------------------------- #
class CommentTaxTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=60))
        fx.event(1, "comment_added", "RrRr", 10, severity="suggestion")
        fx.event(1, "pr_pushed", "AaAa", 30, push_class="content")
        fx.event(1, "pr_approved", "RrRr", 50)
        fx.event(1, "pr_merged", "RrRr", 60)
        for index in range(2):
            fx.thread(1, index, "nit", "rejected")
        fx.thread(1, 2, "out_of_scope", "walked_back")
        for index in range(3, 6):
            fx.thread(1, index, "nit", "acted_on")
        for index in range(6, 10):
            fx.thread(1, index, "question", "unresolved")
        return fx

    def test_threads_are_grouped_by_type_and_outcome(self):
        metric = self.metric(self.fixture(), "comment_tax")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["classified"], 10)
        self.assertEqual(metric["wasted"], 3)
        self.assertEqual(metric["wasted_share"], 0.3)
        self.assertEqual(metric["rejected"], 2)
        self.assertEqual(metric["walked_back"], 1)
        self.assertEqual(metric["matrix"]["nit"], {"acted_on": 3, "rejected": 2})
        self.assertEqual(metric["by_type"]["out_of_scope"], {"threads": 1, "wasted": 1})

    def test_emits_a_move_nits_to_the_ticket_rule(self):
        suggestion = by_id(self.insights(self.fixture()), "comment-tax")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["severity"], "warning")
        self.assertEqual(suggestion["impact_hours"], 180.0)   # 3 × 60 h per round
        self.assertEqual(suggestion["evidence"]["top_type"], "nit")
        self.assertEqual(suggestion["evidence"]["share"], 0.3)

    def test_an_unclassified_store_says_so_instead_of_guessing(self):
        fx = Fixture()
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=30))
        fx.event(1, "pr_approved", "RrRr", 20)
        fx.event(1, "pr_merged", "RrRr", 30)
        for index in range(4):
            fx.thread(1, index)          # pre-classifier threads: no labels
        payload = self.insights(fx)
        metric = payload["metrics"]["comment_tax"]
        self.assertFalse(metric["available"])
        self.assertEqual(metric["classified"], 0)
        self.assertEqual(metric["threads"], 4)
        self.assertIsNone(by_id(payload, "comment-tax"))


# --------------------------------------------------------------------------- #
# 7. conformance
# --------------------------------------------------------------------------- #
class ConformanceTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        # merged with no approval at all
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=48))
        fx.event(1, "pr_merged", "AaAa", 48)
        # four rounds, then approved (a round needs a first review)
        fx.pr(2, "AaAa", state="merged", closed=T0 + timedelta(hours=200))
        fx.event(2, "comment_added", "RrRr", 5, severity="suggestion")
        for hours in (10, 20, 30, 40):
            fx.event(2, "pr_pushed", "AaAa", hours, push_class="content")
        fx.event(2, "pr_approved", "RrRr", 50)
        fx.event(2, "pr_merged", "RrRr", 200)
        return fx

    def test_counts_unapproved_merges_and_long_loops(self):
        metric = self.metric(self.fixture(), "conformance")
        self.assertEqual(metric["n"], 2)
        self.assertEqual(metric["merged"], 2)
        self.assertEqual(metric["merged_without_approval"], 1)
        self.assertEqual(metric["examples"], [f"PR:{REPO}#1"])
        self.assertEqual(metric["prs_over_max_rounds"], 1)
        self.assertEqual(metric["excess_rounds"], 1)

    def test_emits_both_conformance_rules(self):
        payload = self.insights(self.fixture())
        unapproved = by_id(payload, "conformance-unapproved")
        rounds = by_id(payload, "conformance-rounds")
        self.assertIsNotNone(unapproved)
        self.assertIsNotNone(rounds)
        self.assertEqual(unapproved["severity"], "critical")
        self.assertEqual(rounds["severity"], "warning")
        # titles are sentences, so a single item is singular (QA round 2 B19)
        self.assertEqual(unapproved["title"], "1 PR merged with no approval")
        self.assertEqual(rounds["title"], "1 PR ran past 3 review rounds")
        # the one closed PR with rounds: 200 h ÷ 4 = 50 h per round
        self.assertEqual(unapproved["impact_hours"], 50.0)
        self.assertEqual(rounds["impact_hours"], 50.0)


# --------------------------------------------------------------------------- #
# 8. sentinel_queue
# --------------------------------------------------------------------------- #
class SentinelQueueTests(StoreCase):
    def fixture(self, sentinel: bool = True) -> Fixture:
        fx = Fixture()
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=102))
        fx.event(1, "comment_added", "RrRr", 100, severity="suggestion",
                 **({"sentinel": True} if sentinel else {}))
        fx.event(1, "pr_approved", "RrRr", 101,
                 **({"sentinel": True} if sentinel else {}))
        fx.event(1, "pr_merged", "RrRr", 102)
        return fx

    def test_median_wait_on_the_mandatory_approval(self):
        """prd/46 §2: one clock — last peer answer → first response."""
        metric = self.metric(self.fixture(), "sentinel_queue")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["sentinels"], ["RrRr"])
        self.assertEqual(metric["n"], 1)
        self.assertEqual(metric["median_hours"], 100.0)   # PR open → first response
        self.assertTrue(metric["sentinel_after_peers"])
        self.assertIn("last peer answer", metric["definition"])

    def test_the_rules_card_quotes_the_same_number_as_the_response_table(self):
        """prd/46 §2: one canonical clock; the two surfaces cannot drift."""
        court = self.load(self.fixture())[0]
        response = ballcourt.response_times(court)
        metric = rules.sentinel_queue(court, rules.window_for(court))
        self.assertEqual(metric["median_hours"],
                         response["sentinel_wait"]["median_hours"])
        self.assertEqual(metric["n"], response["sentinel_wait"]["n"])
        self.assertEqual(metric["definition"],
                         ballcourt.SENTINEL_WAIT_DEFINITION)

    def test_emits_a_rotation_rule(self):
        suggestion = by_id(self.insights(self.fixture()), "sentinel-queue")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["severity"], "critical")
        self.assertEqual(suggestion["impact_hours"], 76.0)   # (100 − 24) × 1
        self.assertEqual(suggestion["evidence"]["sentinels"], 1)

    def test_no_role_data_means_no_rule(self):
        payload = self.insights(self.fixture(sentinel=False))
        self.assertFalse(payload["metrics"]["sentinel_queue"]["available"])
        self.assertIsNone(by_id(payload, "sentinel-queue"))


# --------------------------------------------------------------------------- #
# 8b. the prd/37 rules: stale PRs and the sentinel tail
# --------------------------------------------------------------------------- #
class StaleAndSentinelTailTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        # PR 1: open since T0 (55 calendar days) — far past 5 business days
        fx.pr(1, "AaAa", tickets=("ENG-1",))
        # PR 2: open for 10 h — healthy, not stale
        fx.pr(2, "AaAa", opened=AS_OF - timedelta(hours=10), tickets=("ENG-2",))
        # PR 3: the peer answered, then the sentinel was the only holder for
        # ~98 h — the sole-holder tail the rule names
        fx.pr(3, "AaAa", opened=AS_OF - timedelta(hours=100), tickets=("ENG-3",))
        # the sentinel asked for a change, the author answered it, and the
        # sentinel has not re-reviewed since: the sentinel is the only holder
        fx.event(3, "pr_needs_work", "FrMo", 1, sentinel=True)
        fx.event(3, "comment_added", "AaAa", 2, severity="suggestion")
        return fx

    def test_stale_prs_are_flagged_and_priced(self):
        metric = self.metric(self.fixture(), "stale_prs")
        self.assertEqual(metric["stale_prs"], 2)          # #1 and #3
        self.assertEqual(metric["open_prs"], 3)
        self.assertEqual(metric["limit_days"], 5.0)
        self.assertEqual(metric["over_age"], 2)
        # the worst (oldest) item is first: prd/37 §3
        self.assertEqual(metric["rows"][0]["id"], "platform#1")
        self.assertTrue(metric["rows"][0]["affected"])
        self.assertGreater(metric["hours"], 0)
        suggestion = by_id(self.insights(self.fixture()), "stale-prs")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["metric"], "stale_prs")
        self.assertEqual(suggestion["severity"], "critical")
        self.assertIn("platform#1", suggestion["evidence"]["examples"])
        # prd/42 P8: the example carries the ticket key, so the view can link it
        self.assertIn("ENG-1", suggestion["evidence"]["examples"])

    def test_a_done_ticket_with_an_open_pr_is_stale(self):
        fx = self.fixture()
        fx.write(self.path)
        court = fx.court(self.path)
        window = rules.window_for(court, None)
        metric = rules.stale_prs(court, window, done_tickets={"ENG-2"})
        row = next(row for row in metric["rows"] if row["id"] == "platform#2")
        # the ticket is Done but the PR is open: the whole current wait is
        # finished work that was never delivered
        self.assertTrue(row["ticket_done"])
        self.assertTrue(row["affected"])
        self.assertGreater(row["hours"], 0)
        self.assertIn("Done in Jira", " ".join(row["reasons"]))

    def test_the_sentinel_tail_is_the_sole_holder_past_the_threshold(self):
        metric = self.metric(self.fixture(), "sentinel_tail")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["sentinels"], ["FrMo"])
        self.assertEqual(metric["tail_items"], 1)
        self.assertEqual(metric["rows"][0]["id"], "platform#3")
        self.assertEqual(metric["rows"][0]["person"], "FrMo")
        self.assertGreater(metric["rows"][0]["hours"], rules.SENTINEL_TAIL_HOURS)
        suggestion = by_id(self.insights(self.fixture()), "sentinel-tail")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["metric"], "sentinel_tail")
        self.assertIn("waiting only on FrMo", suggestion["title"])
        self.assertEqual(suggestion["severity"], "critical")   # 98 h ≥ 3 × 24


# --------------------------------------------------------------------------- #
# 8c. at-stake additivity and impact-sorted drill-downs (prd/37 §2/§3)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# prd/43 §5: one PR, one set of numbers
# --------------------------------------------------------------------------- #
class OnePrOneSetOfNumbersTests(StoreCase):
    """The list inspector and the drawer must not disagree about one PR."""

    def fixture(self) -> Fixture:
        fx = Fixture()
        # PR 1: three commits, all dated at the open instant — the old
        # per-interval sum counted none of them (`start < c <= end`), so the
        # list said "commits in log 0" while the ticket drawer said 3.
        fx.pr(1, "AaAa")
        for _ in range(3):
            fx.commit(1)
        fx.event(1, "reviewer_changed", "AaAa", 0, change="added",
                 people=("RrRr", "FrMo"))
        fx.event(1, "comment_added", "RrRr", 5, severity="suggestion")
        fx.event(1, "pr_approved", "RrRr", 20)
        return fx

    def test_the_delivery_row_counts_the_pr_commit_objects(self):
        court, threads, sizes, _bots = self.load(self.fixture())
        window = rules.window_for(court, None)
        rows = ballcourt.delivery_list(court, window)["rows"]
        self.assertEqual(rows[0]["commits"], 3)
        state = court.prs[0]
        self.assertEqual(len(state.commits), 3)
        # the size rule reads the same canonical count
        pr_size = rules.metrics(court, threads, window, sizes)["pr_size"]
        self.assertEqual(pr_size["rows"][0]["commits"], 3)
        # and flow efficiency no longer sums the intervals either
        flow = rules.metrics(court, threads, window, sizes)["flow_efficiency"]
        self.assertEqual(flow["prs"][0]["commits"], 3)

    def test_the_delivery_row_carries_the_business_hour_phase_spans(self):
        court, _threads, _sizes, _bots = self.load(self.fixture())
        window = rules.window_for(court, None)
        row = ballcourt.delivery_list(court, window)["rows"][0]
        state = court.prs[0]
        expected = {}
        for interval in state.intervals:
            if interval.measured and interval.hours > 0:
                expected[interval.phase] = round(
                    expected.get(interval.phase, 0.0) + round(interval.hours, 2), 2
                )
        self.assertEqual(row["phase_hours"], expected)
        self.assertGreater(row["phase_hours"].get("review", 0), 0)
        # the raw last event, not the derived `kind` — the ticket drawer labels
        # the same event `comment_added`, and the list must not say
        # `author_reply` for it.
        self.assertEqual(row["last_event_activity"], "pr_approved")


# --------------------------------------------------------------------------- #
# prd/43 §1: Rules ↔ Policy-lab cross-check
# --------------------------------------------------------------------------- #
def _compare(percent, *, better, delivered=None, delivered_better=None,
             within_noise=False, verdict_tone=None):
    return {
        "cycle_median_hours": {
            "percent": percent,
            "direction": "down" if percent < 0 else "up",
            "better": better,
        },
        "throughput_work_per_day": {
            "percent": delivered,
            "direction": "up" if (delivered or 0) > 0 else "down",
            "better": delivered_better,
        },
        "within_noise": within_noise,
        "verdict": "a verdict",
        "verdict_tone": verdict_tone,
    }


class PolicyLabCrossCheckTests(unittest.TestCase):
    """The two advice surfaces must agree, in both directions (prd/43 §1)."""

    def report(self):
        return advice.report({
            "refine": _compare(82.0, better=False),
            "sentinel2": _compare(-3.0, better=True, within_noise=True),
            "size3": _compare(289.0, better=False),
            "sla4": _compare(-9.0, better=True, delivered=-8.4,
                             delivered_better=False),
            "roundcap2": _compare(-13.0, better=True, delivered=4.7,
                                  delivered_better=True),
        })

    def test_a_contradicted_rule_says_not_a_measured_win(self):
        report = self.report()
        suggestions = [
            {"id": "jira-waiting-time",   # refine before start
             "evidence": {"segment_key": "created_ready"}},
            {"id": "sentinel-tail"},       # a second sentinel
            {"id": "pr-size"},             # smaller PRs
            {"id": "round-cap"},           # the cap the lab supports
        ]
        advice.attach(suggestions, report)
        refine = suggestions[0]["policy_lab"]
        self.assertEqual(refine["label"], "refine before start")
        self.assertTrue(refine["contradiction"])
        self.assertFalse(refine["supports"])
        # the PRD's exact phrasing, with the caveat that explains the gap
        self.assertTrue(refine["note"].startswith(
            "the policy lab models this at +82 % cycle — not a measured win"
        ))
        self.assertIn("cannot see the ticket-level wait", refine["note"])
        sentinel = suggestions[1]["policy_lab"]
        self.assertIn("no clear effect", sentinel["note"])
        self.assertIn("−3 % cycle", sentinel["note"])
        self.assertTrue(sentinel["contradiction"])   # no effect ≠ a measured win
        size = suggestions[2]["policy_lab"]
        self.assertIn("+289 % cycle", size["note"])
        self.assertIn("not a measured win", size["note"])
        capped = suggestions[3]["policy_lab"]
        self.assertTrue(capped["supports"])
        self.assertFalse(capped["contradiction"])
        self.assertIn("−13 % cycle", capped["note"])

    def test_the_refinement_note_does_not_ride_a_review_rounds_card(self):
        """prd/46 §3: the same rule id measures whichever segment is largest."""
        suggestions = [
            {"id": "jira-waiting-time",
             "evidence": {"segment_key": "review_rounds"}},
            {"id": "jira-waiting-time",
             "evidence": {"segment_key": "created_ready"}},
        ]
        advice.attach(suggestions, self.report())
        self.assertNotIn("policy_lab", suggestions[0])
        self.assertTrue(suggestions[1]["policy_lab"]["contradiction"])
        self.assertIn("+82 % cycle", suggestions[1]["policy_lab"]["note"])

    def test_a_contradicted_rule_is_ranked_below_the_supported_lever(self):
        """prd/46 §3: the #1 must agree with the banner's own best scenario."""
        suggestions = [
            {"id": "jira-waiting-time", "impact_score": 100.0,
             "impact_hours": 100.0, "severity": "critical", "sample": 10,
             "evidence": {"segment_key": "created_ready"}},
            {"id": "sentinel-tail", "impact_score": 90.0,
             "impact_hours": 90.0, "severity": "critical", "sample": 10},
            {"id": "round-cap", "impact_score": 50.0,
             "impact_hours": 50.0, "severity": "warning", "sample": 10},
        ]
        advice.attach(suggestions, self.report())
        ordered = rules.rank(suggestions, lab_aware=True)
        self.assertEqual(ordered[0]["id"], "round-cap")
        self.assertTrue(ordered[0]["policy_lab"]["best"])
        self.assertIn("−13 % cycle", ordered[0]["policy_lab"]["note"])
        self.assertEqual([row["rank"] for row in ordered], [1, 2, 3])
        # the demoted rules keep their measured impact; only the position moved
        jira = next(row for row in ordered if row["id"] == "jira-waiting-time")
        self.assertEqual(jira["impact_score"], 100.0)
        self.assertGreater(jira["rank"], ordered[0]["rank"])
        self.assertTrue(jira["policy_lab"]["contradiction"])

    def test_the_best_scenario_is_the_one_the_lab_supports(self):
        report = self.report()
        self.assertTrue(report["available"])
        self.assertEqual(report["best"]["id"], "roundcap2")
        self.assertEqual(report["best"]["label"], "cap review at 2 rounds")
        self.assertEqual(
            report["note"],
            "the lab's best scenario is cap review at 2 rounds — −13 % cycle",
        )

    def test_a_mixed_sla_result_quotes_delivered_work(self):
        report = self.report()
        suggestion = {"id": "review-sla"}
        advice.attach([suggestion], report)
        note = suggestion["policy_lab"]["note"]
        self.assertIn("−9 % cycle", note)
        self.assertIn("−8 % delivered work", note)
        self.assertIn("mixed", note)

    def test_no_lab_result_is_stated_not_invented(self):
        report = advice.report({"refine": None})
        self.assertFalse(report["available"])
        self.assertIsNone(report["best"])
        self.assertIn("no validated result", report["note"])
        suggestion = {"id": "jira-waiting-time"}
        advice.attach([suggestion], report)
        self.assertNotIn("policy_lab", suggestion)


class AtStakeTests(unittest.TestCase):
    def suggestion(self, sid, impact, sample, scope=None, metric=None):
        return {
            "id": sid,
            "metric": metric or sid.replace("-", "_"),
            "impact_hours": impact,
            "sample": sample,
            "scope": scope or {"kind": "team"},
            "rank": 1,
        }

    def test_a_subset_rule_is_not_added_to_its_parent(self):
        suggestions = [
            self.suggestion("rework-loops", 100.0, 10),
            self.suggestion("rebase-waste", 40.0, 3),      # ⊂ rework-loops
            self.suggestion("flow-efficiency", 160.0, 10),
            self.suggestion("jira-carry-over", 70.0, 20),
            self.suggestion("sprint-on-time", 70.0, 20),
        ]
        at_stake = rules.attribute_at_stake(suggestions)
        # the PR-wait pool is counted at its broadest estimate (160), not
        # 100 + 40 + 160; the sprint pool once (70), not 140
        self.assertEqual(at_stake["hours"], 230.0)
        attributed = {
            s["id"]: s["at_stake"]["attributed_hours"] for s in suggestions
        }
        self.assertEqual(sum(attributed.values()), at_stake["hours"])
        self.assertEqual(attributed["rebase-waste"], 40.0)      # most specific
        # the pool is filled to its broadest estimate (160), most specific first
        self.assertEqual(attributed["rework-loops"], 100.0)
        self.assertEqual(attributed["flow-efficiency"], 20.0)
        self.assertEqual(attributed["sprint-on-time"], 0.0)
        self.assertEqual(attributed["jira-carry-over"], 70.0)
        # a fully-absorbed rule points at the rule that owns its pool
        absorbed = next(s for s in suggestions if s["id"] == "sprint-on-time")
        self.assertEqual(absorbed["at_stake"]["attributed_to"], "jira-carry-over")
        self.assertGreater(at_stake["overlap_hours"], 0)

    def test_person_rules_are_counted_before_the_team_rule(self):
        suggestions = [
            self.suggestion("reviewer-AaAa", 30.0, 5,
                            {"kind": "person", "person": "AaAa"}),
            self.suggestion("reviewer-BbBb", 20.0, 5,
                            {"kind": "person", "person": "BbBb"}),
            self.suggestion("reviewer-spread", 100.0, 20),
        ]
        at_stake = rules.attribute_at_stake(suggestions)
        attributed = {s["id"]: s["at_stake"]["attributed_hours"] for s in suggestions}
        self.assertEqual(attributed["reviewer-AaAa"], 30.0)
        self.assertEqual(attributed["reviewer-BbBb"], 20.0)
        self.assertEqual(attributed["reviewer-spread"], 50.0)   # the remainder
        self.assertEqual(at_stake["hours"], 100.0)

    def test_the_drill_down_is_impact_sorted(self):
        rows = [
            {"id": "platform#1", "hours": 3.0, "affected": False},
            {"id": "platform#2", "hours": 90.0, "affected": True},
            {"id": "platform#3", "hours": 30.0, "affected": True},
        ]
        ordered = rules._drill_order(rows)
        # worst wait first, never by key — prd/37 §3
        self.assertEqual([row["id"] for row in ordered],
                         ["platform#2", "platform#3", "platform#1"])


# --------------------------------------------------------------------------- #
# 9. pr_size
# --------------------------------------------------------------------------- #
class PrSizeTests(StoreCase):
    def commits_fixture(self) -> Fixture:
        """Diff size absent: the metric falls back to commit count."""
        fx = Fixture()
        for number, commits, first_review in (
            (1, 12, 60), (2, 12, 50),      # large: slow first review
            (3, 2, 5), (4, 3, 8), (5, 1, 10),
        ):
            fx.pr(number, "AaAa")
            for _ in range(commits):
                fx.commit(number)
            fx.event(number, "comment_added", "RrRr", first_review,
                     severity="suggestion")
        return fx

    def lines_fixture(self) -> Fixture:
        """Diff size present on the PR object: the line cap drives the split."""
        fx = Fixture()
        for number, (added, removed), first_review in (
            (1, (300, 200), 40), (2, (450, 0), 50),   # 500 and 450 lines: large
            (3, (50, 0), 5), (4, (100, 0), 10),
        ):
            fx.pr(number, "AaAa", attributes={
                "lines_added": added, "lines_removed": removed,
            })
            fx.commit(number)
            fx.event(number, "comment_added", "RrRr", first_review,
                     severity="suggestion")
        return fx

    def test_commits_are_counted_and_diff_size_is_marked_pending(self):
        metric = self.metric(self.commits_fixture(), "pr_size")
        self.assertEqual(metric["n"], 5)
        self.assertEqual(metric["commits"]["median"], 3.0)
        self.assertEqual(metric["commits"]["p90"], 12.0)
        self.assertEqual(metric["commits"]["max"], 12)
        self.assertEqual(metric["commits"]["total"], 30)
        self.assertFalse(metric["diff_size"]["available"])
        self.assertTrue(metric["diff_size"]["pending"])
        self.assertEqual(metric["over_commits_cap"], 2)
        self.assertEqual(metric["large_prs"], 2)
        self.assertEqual(metric["small_prs"], 3)
        self.assertEqual(metric["large_median_first_review_hours"], 55.0)
        self.assertEqual(metric["small_median_first_review_hours"], 8.0)

    def test_emits_a_size_cap_rule_from_commits_alone(self):
        suggestion = by_id(self.insights(self.commits_fixture()), "pr-size")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["severity"], "warning")
        self.assertEqual(suggestion["impact_hours"], 94.0)   # (55 − 8) × 2
        self.assertIn("10 commits", suggestion["title"])
        self.assertIn("size cap", suggestion["suggestion"])
        self.assertIn("Diff size is not captured yet", suggestion["suggestion"])
        self.assertTrue(suggestion["evidence"]["diff_size_pending"])
        self.assertEqual(suggestion["evidence"]["large_prs"], 2)

    def test_diff_size_uses_lines_when_the_store_has_them(self):
        metric = self.metric(self.lines_fixture(), "pr_size")
        self.assertTrue(metric["diff_size"]["available"])
        self.assertFalse(metric["diff_size"]["pending"])
        self.assertEqual(metric["diff_size"]["median"], 275.0)   # (100 + 450) / 2
        self.assertEqual(metric["diff_size"]["p90"], 500.0)
        self.assertEqual(metric["over_lines_cap"], 2)
        self.assertEqual(metric["large_prs"], 2)

        suggestion = by_id(self.insights(self.lines_fixture()), "pr-size")
        self.assertIsNotNone(suggestion)
        self.assertIn("400 changed lines", suggestion["title"])
        self.assertIn("400 changed lines", suggestion["suggestion"])
        self.assertNotIn("not captured yet", suggestion["suggestion"])
        self.assertFalse(suggestion["evidence"]["diff_size_pending"])
        self.assertEqual(suggestion["impact_hours"], 75.0)   # (45 − 7.5) × 2

    def test_no_size_effect_means_no_rule(self):
        fx = Fixture()
        # large PRs get *faster* first reviews: the cap is not the bottleneck
        for number, commits, first_review in (
            (1, 12, 5), (2, 12, 6), (3, 2, 20), (4, 3, 30),
        ):
            fx.pr(number, "AaAa")
            for _ in range(commits):
                fx.commit(number)
            fx.event(number, "comment_added", "RrRr", first_review,
                     severity="suggestion")
        self.assertIsNone(by_id(self.insights(fx), "pr-size"))

    def test_one_large_pr_is_not_a_sample(self):
        fx = Fixture()
        for number, commits, first_review in ((1, 12, 60), (2, 1, 5), (3, 2, 8)):
            fx.pr(number, "AaAa")
            for _ in range(commits):
                fx.commit(number)
            fx.event(number, "comment_added", "RrRr", first_review,
                     severity="suggestion")
        self.assertIsNone(by_id(self.insights(fx), "pr-size"))

    def test_no_review_latency_means_no_rule(self):
        fx = Fixture()
        # commits and pushes but no reviewer action yet: no latency to compare
        for number, commits, pushes in ((1, 12, 2), (2, 12, 2), (3, 1, 0), (4, 2, 0)):
            fx.pr(number, "AaAa")
            for _ in range(commits):
                fx.commit(number)
            for index in range(pushes):
                fx.event(number, "pr_pushed", "AaAa", 10 + index,
                         push_class="content")
        metric = self.metric(fx, "pr_size")
        self.assertEqual(metric["large_prs"], 2)
        self.assertIsNone(metric["large_median_first_review_hours"])
        self.assertIsNone(by_id(self.insights(fx), "pr-size"))

    # -- prd/39: the caps are the tenant's calibration -------------------- #
    def test_caps_rebucket_the_metric_and_the_rule(self):
        fx = self.commits_fixture()          # commits 12, 12, 2, 3, 1
        default = self.metric(fx, "pr_size")
        self.assertEqual(default["caps"], {"commits": 10, "lines": 400})
        self.assertEqual(default["large_prs"], 2)
        # the same store at a tighter commit cap: 12, 12, 3 are over
        tighter = self.metric(fx, "pr_size", caps={"commits": 2, "lines": 400})
        self.assertEqual(tighter["commits_cap"], 2)
        self.assertEqual(tighter["large_prs"], 3)
        self.assertEqual(tighter["over_commits_cap"], 3)
        # the rule (and its evidence) re-evaluate against the chosen caps
        payload = self.insights(fx, caps={"commits": 2, "lines": 400})
        self.assertEqual(payload["targets"]["size_commit_cap"], 2)
        self.assertEqual(payload["metrics"]["pr_size"]["large_prs"], 3)
        suggestion = by_id(payload, "pr-size")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["evidence"]["commits_cap"], 2)
        self.assertIn("2 commits", suggestion["title"])

    def test_size_caps_are_clamped_and_defaulted(self):
        self.assertEqual(
            rules.size_caps({"commits": 0, "lines": 10_000_000}),
            {"commits": 1, "lines": 10_000},
        )
        self.assertEqual(
            rules.size_caps({"commits": "nope"}),
            {"commits": 10, "lines": 400},
        )
        # a bool is not a cap (True would otherwise read as 1)
        self.assertEqual(rules.size_caps({"commits": True})["commits"], 10)

    def test_the_distribution_matches_the_objects(self):
        fx = self.commits_fixture()          # commit counts: 12, 12, 2, 3, 1
        court, _threads, sizes, _bots = self.load(fx)
        window = rules.window_for(court, None)
        body = rules.pr_size_distribution(court, window, sizes)
        self.assertEqual(body["n"], 5)
        self.assertEqual(body["commits"]["values"], [1, 2, 3, 12, 12])
        self.assertEqual(body["commits"]["max"], 12)
        self.assertEqual(sum(body["commits"]["counts"]), 5)
        # the cap is an edge, so the marker lands on a boundary
        self.assertIn(10, body["commits"]["edges"])
        self.assertFalse(body["lines"]["available"])
        self.assertEqual(body["large_prs"], 2)
        self.assertEqual(
            {row["number"]: row["commits"] for row in body["rows"]},
            {1: 12, 2: 12, 3: 2, 4: 3, 5: 1},
        )

    def test_size_vs_cycle_correlation_is_reported(self):
        """prd/42 M8: the sliders' missing evidence — size vs cycle."""
        fx = Fixture()
        for number, commits, hours in ((1, 1, 10), (2, 2, 20), (3, 3, 30), (4, 4, 40)):
            fx.pr(number, "AaAa", state="merged",
                  closed=T0 + timedelta(hours=hours))
            for _ in range(commits):
                fx.commit(number)
        court, _threads, sizes, _bots = self.load(fx)
        body = rules.pr_size_distribution(
            court, rules.window_for(court, None), sizes,
        )
        correlations = body["correlations"]
        self.assertEqual(correlations["n"], 4)
        self.assertEqual(correlations["commits"], 1.0)
        self.assertIsNone(correlations["lines"])     # diff size not captured
        self.assertIn("Pearson r", correlations["basis"])
        # every closed row carries its canonical cycle (business hours)
        self.assertEqual(
            {row["number"]: row["cycle_hours"] for row in body["rows"]},
            {1: 10.0, 2: 20.0, 3: 30.0, 4: 40.0},
        )

    def test_the_line_distribution_matches_the_pr_attributes(self):
        fx = self.lines_fixture()            # changed lines: 500, 450, 50, 100
        court, _threads, sizes, _bots = self.load(fx)
        window = rules.window_for(court, None)
        body = rules.pr_size_distribution(court, window, sizes)
        self.assertTrue(body["lines"]["available"])
        self.assertEqual(body["lines"]["values"], [50, 100, 450, 500])
        self.assertEqual(sum(body["lines"]["counts"]), 4)
        self.assertIn(400, body["lines"]["edges"])
        self.assertEqual(body["large_prs"], 2)
        # moving only the line cap re-buckets the "large" count exactly
        tighter = rules.pr_size_distribution(
            court, window, sizes, caps={"commits": 10, "lines": 50}
        )
        self.assertEqual(tighter["large_prs"], 3)      # 100, 450, 500
        looser = rules.pr_size_distribution(
            court, window, sizes, caps={"commits": 10, "lines": 500}
        )
        self.assertEqual(looser["large_prs"], 0)


# --------------------------------------------------------------------------- #
# the evidence drill-down (prd/26 P5)
# --------------------------------------------------------------------------- #
class DrillDownTests(StoreCase):
    """Each suggestion must open into the *items* behind its number.

    A metric that ranks "16 PRs past 3 rounds" but cannot say which 16 leaves the
    reader at a dead end; these tests pin the per-item rows the Rules page
    expands, the flags that mark the affected ones, and the ticket keys each row
    links.
    """

    def fixture(self) -> Fixture:
        fx = Fixture()
        # PR 1: first review in 10 h, merged at 20 h — healthy
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=20),
              tickets=("ENG-1",))
        fx.event(1, "comment_added", "RrRr", 10, severity="suggestion")
        fx.event(1, "pr_approved", "RrRr", 15)
        fx.event(1, "pr_merged", "RrRr", 20)
        # PR 2: first review in 100 h, merged at 120 h — over target
        fx.pr(2, "AaAa", state="merged", closed=T0 + timedelta(hours=120),
              tickets=("ENG-2",))
        fx.event(2, "comment_added", "RrRr", 100, severity="suggestion")
        fx.event(2, "pr_approved", "RrRr", 110)
        fx.event(2, "pr_merged", "RrRr", 120)
        # PR 3: open, never reviewed — the missing-review row
        fx.pr(3, "AaAa", tickets=("ENG-3",))
        return fx

    def test_first_review_rows_are_the_prs_with_their_links(self):
        metric = self.metric(self.fixture(), "time_to_first_review")
        rows = metric["rows"]
        # affected first, then by hours; the never-reviewed PR is last
        self.assertEqual([row["id"] for row in rows],
                         ["platform#2", "platform#1", "platform#3"])
        self.assertTrue(rows[0]["over_target"])
        self.assertEqual(rows[0]["tickets"], ["ENG-2"])
        self.assertEqual(rows[0]["object_id"], f"PR:{REPO}#2")
        self.assertEqual(rows[0]["reviewer"], "RrRr")
        self.assertEqual(rows[0]["first_review_hours"], 100.0)
        self.assertFalse(rows[1]["over_target"])
        self.assertTrue(rows[2]["missing_review"])
        self.assertIsNone(rows[2]["first_review_hours"])
        self.assertEqual(rows[2]["tickets"], ["ENG-3"])

    def test_the_suggestion_and_its_metric_agree_on_the_items(self):
        payload = self.insights(self.fixture())
        suggestion = by_id(payload, "review-sla")
        self.assertIsNotNone(suggestion)
        rows = payload["metrics"][suggestion["metric"]]["rows"]
        self.assertEqual(len(rows), 3)
        # the count in the evidence is the number of rows that are over target
        self.assertEqual(
            suggestion["evidence"]["over_target"],
            sum(1 for row in rows if row["over_target"]),
        )

    def test_cycle_time_rows_include_the_censored_population(self):
        fx = self.fixture()
        fx.pr(4, "AaAa", tickets=("ENG-4",))          # still open
        rows = self.metric(fx, "cycle_time")["rows"]
        by_number = {row["number"]: row for row in rows}
        self.assertEqual(set(by_number), {1, 2, 3, 4})
        self.assertTrue(by_number[2]["over_target"])
        self.assertFalse(by_number[1]["over_target"])
        # an open PR is evidence too, marked censored rather than dropped
        self.assertTrue(by_number[4]["censored"])
        self.assertFalse(by_number[4]["merged"])
        self.assertTrue(by_number[1]["merged"])

    def test_rework_rows_carry_the_round_count_per_pr(self):
        fx = Fixture()
        fx.pr(1, "AaAa")
        fx.event(1, "comment_added", "RrRr", 5, severity="suggestion")
        for index in range(4):
            fx.event(1, "pr_pushed", "AaAa", 10 + index, push_class="content")
        fx.pr(2, "AaAa")
        fx.event(2, "comment_added", "RrRr", 5, severity="suggestion")
        fx.event(2, "pr_pushed", "AaAa", 10, push_class="content")
        rows = self.metric(fx, "rework_loops")["rows"]
        by_number = {row["number"]: row for row in rows}
        self.assertEqual(by_number[1]["rounds"], 4)
        self.assertTrue(by_number[1]["over_max_rounds"])
        self.assertTrue(by_number[1]["affected"])
        self.assertEqual(by_number[2]["rounds"], 1)
        self.assertFalse(by_number[2]["affected"])

    def test_conformance_rows_are_the_violating_prs_only(self):
        fx = Fixture()
        # PR 1: merged with no approval
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=10),
              tickets=("ENG-1",))
        fx.event(1, "pr_merged", "AaAa", 10)
        # PR 2: merged with an approval
        fx.pr(2, "AaAa", state="merged", closed=T0 + timedelta(hours=20),
              tickets=("ENG-2",))
        fx.event(2, "pr_approved", "RrRr", 15)
        fx.event(2, "pr_merged", "RrRr", 20)
        rows = self.metric(fx, "conformance")["rows"]
        self.assertEqual([row["number"] for row in rows], [1])
        self.assertTrue(rows[0]["merged_without_approval"])
        self.assertEqual(rows[0]["tickets"], ["ENG-1"])

    def test_comment_tax_rows_name_the_thread_and_its_pr(self):
        fx = Fixture()
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=10),
              tickets=("ENG-7",))
        fx.thread(1, 0, "nit", "rejected")
        fx.thread(1, 1, "nit", "acted_on")
        rows = self.metric(fx, "comment_tax")["rows"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["wasted"])
        self.assertEqual(rows[0]["outcome"], "rejected")
        self.assertEqual(rows[0]["tickets"], ["ENG-7"])
        self.assertEqual(rows[0]["object_id"], f"THREAD:{REPO}#1:0")
        self.assertFalse(rows[1]["wasted"])

    def test_pr_size_rows_keep_the_item_identity(self):
        fx = Fixture()
        fx.pr(1, "AaAa", tickets=("ENG-1",))
        for _ in range(12):
            fx.commit(1)
        rows = self.metric(fx, "pr_size")["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "platform#1")
        self.assertEqual(rows[0]["tickets"], ["ENG-1"])
        self.assertTrue(rows[0]["large"])
        self.assertTrue(rows[0]["affected"])

    def test_rebase_rows_are_the_prs_that_lost_an_approval(self):
        fx = Fixture()
        fx.pr(1, "AaAa", tickets=("ENG-1",))
        fx.event(1, "pr_pushed", "AaAa", 10, push_class="pure_rebase")
        fx.event(1, "pr_unapproved", "RrRr", 20, cause="push")
        fx.pr(2, "AaAa", tickets=("ENG-2",))        # untouched: not in the rows
        rows = self.metric(fx, "rebase_waste")["rows"]
        self.assertEqual([row["number"] for row in rows], [1])
        self.assertEqual(rows[0]["lost_on_pure_rebase"], 1)
        self.assertEqual(rows[0]["lost"], {"pure_rebase": 1})
        self.assertEqual(rows[0]["tickets"], ["ENG-1"])

    def test_survival_rows_mark_the_censored_observations(self):
        fx = self.fixture()
        fx.pr(4, "AaAa", tickets=("ENG-4",))
        rows = self.metric(fx, "survival")["rows"]
        by_number = {row["number"]: row for row in rows}
        self.assertTrue(by_number[1]["event"])
        self.assertFalse(by_number[1]["censored"])
        self.assertTrue(by_number[4]["censored"])
        self.assertFalse(by_number[4]["event"])

    def test_reviewer_rows_aggregate_the_waits_per_person_and_pr(self):
        fx = Fixture()
        fx.pr(1, "AaAa")
        fx.event(1, "pr_needs_work", "XxXx", 10)
        fx.event(1, "pr_pushed", "AaAa", 15, push_class="content")
        fx.event(1, "pr_needs_work", "XxXx", 20)
        fx.event(1, "pr_pushed", "AaAa", 25, push_class="content")
        metric = self.metric(fx, "reviewer_responsiveness")
        rows = [row for row in metric["rows"] if row["person"] == "XxXx"]
        self.assertEqual(len(rows), 1)               # one row per (person, PR)
        self.assertEqual(rows[0]["number"], 1)
        self.assertGreaterEqual(rows[0]["waits"], 1)
        self.assertEqual(rows[0]["id"], "platform#1")


# --------------------------------------------------------------------------- #
# the ranked list itself
# --------------------------------------------------------------------------- #
class RankingTests(StoreCase):
    def fixture(self) -> Fixture:
        fx = Fixture()
        # a slow reviewer with fast own PRs
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=300))
        fx.event(1, "pr_needs_work", "XxXx", 10)
        fx.event(1, "pr_pushed", "AaAa", 20, push_class="content")
        fx.event(1, "pr_approved", "XxXx", 280)
        fx.event(1, "pr_merged", "XxXx", 300)
        # a rebase that lost an approval
        fx.pr(2, "AaAa")
        fx.event(2, "pr_pushed", "AaAa", 10, push_class="pure_rebase")
        fx.event(2, "pr_unapproved", "RrRr", 20, cause="push")
        # an unapproved merge
        fx.pr(3, "AaAa", state="merged", closed=T0 + timedelta(hours=50))
        fx.event(3, "pr_merged", "AaAa", 50)
        return fx

    def test_payload_shape_and_ranking(self):
        payload = self.insights(self.fixture(), level="person")
        for key in ("generated_at", "as_of", "hours_mode", "sample",
                    "suggestions", "metrics", "window", "targets"):
            self.assertIn(key, payload)
        self.assertEqual(payload["hours_mode"], "wall-clock")
        self.assertEqual(payload["sample"]["prs"], 3)
        self.assertEqual(payload["sample"]["days"], payload["window"]["days"])

        suggestions = payload["suggestions"]
        self.assertGreaterEqual(len(suggestions), 4)
        scores = [s["impact_score"] for s in suggestions]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual([s["rank"] for s in suggestions], list(range(1, len(suggestions) + 1)))
        for suggestion in suggestions:
            self.assertAlmostEqual(
                suggestion["impact_score"],
                round(suggestion["impact_hours"] * suggestion["confidence"], 1),
                places=1,
            )
            for key in ("id", "metric", "title", "evidence", "suggestion",
                        "severity", "impact_hours"):
                self.assertIn(key, suggestion)
            self.assertIn(suggestion["severity"], ("critical", "warning", "info"))

    def test_severity_breaks_ties_in_the_rank(self):
        """prd/42 P2: severity ≠ rank, but equal impact is ordered by severity."""
        ordered = rules.rank([
            {"id": "warn", "severity": "warning", "impact_score": 10.0,
             "impact_hours": 10.0, "sample": 1},
            {"id": "crit", "severity": "critical", "impact_score": 10.0,
             "impact_hours": 10.0, "sample": 1},
        ])
        self.assertEqual([row["id"] for row in ordered], ["crit", "warn"])
        self.assertEqual([row["rank"] for row in ordered], [1, 2])

    def test_confidence_ramps_with_sample_size(self):
        payload = self.insights(self.fixture())
        conformance = by_id(payload, "conformance-unapproved")
        # the sample is every merged PR inspected (2 here), not the violation count
        self.assertEqual(conformance["sample"], 2)
        self.assertEqual(conformance["confidence"], 0.1)

    def test_days_filters_the_pr_cohort(self):
        fx = self.fixture()
        # a recent PR plus the Jan cohort: a 7-day window keeps only the recent one
        fx.pr(4, "AaAa", opened=AS_OF - timedelta(days=2))
        full = self.insights(fx, days=None)
        week = self.insights(fx, days=7)
        self.assertEqual(full["sample"]["prs"], 4)
        self.assertEqual(week["sample"]["prs"], 1)

    def test_a_clean_store_emits_no_rules(self):
        fx = Fixture()
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=20))
        fx.event(1, "comment_added", "RrRr", 2, severity="suggestion")
        fx.event(1, "pr_approved", "RrRr", 10)
        fx.event(1, "pr_merged", "RrRr", 20)
        payload = self.insights(fx)
        self.assertEqual(payload["suggestions"], [])


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


class InsightsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        cls.store = cls._fixture().write(cls.dir / "del.duckdb")
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

    @staticmethod
    def _fixture() -> Fixture:
        fx = Fixture()
        # slow reviewer (person rule), long cycle, and a rebase waste
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=300),
              attributes={"lines_added": 10, "lines_removed": 5})
        fx.event(1, "pr_needs_work", "XxXx", 10)
        fx.event(1, "pr_pushed", "AaAa", 20, push_class="content")
        fx.event(1, "pr_approved", "XxXx", 280)
        fx.event(1, "pr_merged", "XxXx", 300)
        fx.pr(2, "AaAa")
        fx.event(2, "pr_pushed", "AaAa", 10, push_class="pure_rebase")
        fx.event(2, "pr_unapproved", "RrRr", 20, cause="push")
        fx.thread(2, 1, "nit", "rejected")
        fx.thread(2, 2, "nit", "rejected")
        fx.thread(2, 3, "nit", "rejected")
        fx.thread(2, 4, "nit", "acted_on")
        # XxXx authors a fast PR of their own: the responsiveness contrast needs it
        fx.pr(3, "XxXx")
        fx.event(3, "pr_needs_work", "YyYy", 10)
        fx.event(3, "pr_pushed", "XxXx", 15, push_class="content")
        fx.event(3, "pr_approved", "YyYy", 20)
        fx.person("XxXx")
        # prd/39: known commit counts for the PR-size distribution endpoint
        for _ in range(12):
            fx.commit(1)
        for _ in range(2):
            fx.commit(2)
        fx.commit(3)
        return fx

    def setUp(self):
        app_module._analysis_cache = None
        app_module._pr_size_cache.clear()
        app_module._advice_cache.clear()

    def test_insights_carries_the_policy_lab_cross_check(self):
        """prd/43 §1: the Rules payload names the lab's verdict and its best."""
        report = advice.report({
            "size3": _compare(289.0, better=False),
            "roundcap2": _compare(-13.0, better=True),
        })
        with mock.patch.object(
            app_module, "_advice_payload", return_value=report,
        ):
            body = get("/api/insights", tenant="del", level="team", days=3650).json()
        self.assertEqual(
            body["policy_lab"]["note"],
            "the lab's best scenario is cap review at 2 rounds — −13 % cycle",
        )
        flow = by_id(body, "flow-efficiency")
        self.assertIsNotNone(flow)
        self.assertTrue(flow["policy_lab"]["contradiction"])
        self.assertIn("+289 % cycle — not a measured win",
                      flow["policy_lab"]["note"])

    def test_the_api_runs_the_real_lab_or_states_why_it_did_not(self):
        body = get("/api/insights", tenant="del", level="team", days=3650).json()
        lab = body["policy_lab"]
        self.assertIn("available", lab)
        self.assertIn("note", lab)
        self.assertIn("basis", lab)
        if lab["available"]:
            self.assertGreaterEqual(len(lab["policies"]), 1)
        # every rule's note is one of the lab's own lines, never a guess
        for suggestion in body["suggestions"]:
            if "policy_lab" in suggestion:
                self.assertTrue(suggestion["policy_lab"]["note"])

    def test_insights_returns_a_ranked_tenant_scoped_list(self):
        reply = get("/api/insights", tenant="del", level="team", days=3650)
        self.assertEqual(reply.status, 200)
        body = reply.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tenant"], "del")
        # the server attaches the committed calendar (prd/19), so durations are
        # business hours; a court loaded without one stays wall-clock
        self.assertEqual(body["hours_mode"], "business")
        self.assertIn("business hours", body["assumption"])
        self.assertEqual(body["level"], "team")
        self.assertEqual(body["sample"]["prs"], 3)
        self.assertIn("generated_at", body)
        self.assertIn("as_of", body)
        self.assertTrue(body["suggestions"])
        scores = [s["impact_score"] for s in body["suggestions"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual([s["rank"] for s in body["suggestions"]],
                         list(range(1, len(body["suggestions"]) + 1)))
        ids = {s["id"] for s in body["suggestions"]}
        self.assertIn("rebase-waste", ids)
        self.assertIn("comment-tax", ids)
        # team level never names a person
        self.assertNotIn("reviewer-XxXx", ids)
        # metric 9: commits always, diff size when the PR object carries it
        pr_size = body["metrics"]["pr_size"]
        self.assertEqual(pr_size["n"], 3)
        self.assertTrue(pr_size["diff_size"]["available"])
        self.assertEqual(pr_size["diff_size"]["median"], 15.0)

    def test_the_new_flow_and_survival_metrics_are_exposed(self):
        body = get("/api/insights", tenant="del", level="team", days=3650).json()
        flow = body["metrics"]["flow_efficiency"]
        # PR 1 is 10 h active in 300 h; PR 3 is 5 h active; PR 2 has no work
        self.assertTrue(flow["available"])
        self.assertEqual(flow["n"], 3)
        self.assertIn("aggregate_efficiency", flow)
        # the touch credit can only raise efficiency, never lower it
        self.assertGreaterEqual(flow["median_efficiency"], flow["measured_median_efficiency"])
        self.assertGreater(flow["aggregate_efficiency"], 0.0)
        self.assertEqual(flow["touch_hours_per_touch"], 0.5)
        self.assertIn("review", flow["waiting_breakdown"])
        self.assertEqual(body["targets"]["flow_efficiency"], 0.25)
        # survival: one merged PR, two open ones censored at the snapshot
        fit = body["metrics"]["survival"]
        self.assertEqual(fit["estimator"], "Kaplan–Meier")
        self.assertEqual(fit["n"], 3)
        self.assertEqual(fit["events"], 1)
        self.assertEqual(fit["censored"]["open"], 2)
        # business hours (prd/19): the one merged PR spans 78 working hours
        self.assertEqual(fit["merged_only_median_hours"], 78.0)
        self.assertEqual(body["metrics"]["cycle_time"]["median_hours"], 78.0)
        # one event in three leaves S at 2/3: the median is not reached
        self.assertIsNone(fit["median_hours"])
        self.assertEqual(body["metrics"]["cycle_time"]["survival_median_hours"],
                         fit["median_hours"])
        ids = {s["id"] for s in body["suggestions"]}
        self.assertIn("flow-efficiency", ids)

    def test_person_level_adds_pseudonym_scoped_rules(self):
        body = get("/api/insights", tenant="del", level="person", days=3650).json()
        suggestion = by_id(body, "reviewer-XxXx")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["scope"], {"kind": "person", "person": "XxXx"})
        self.assertEqual(suggestion["metric"], "reviewer_responsiveness")

    def test_days_filters_the_cohort(self):
        body = get("/api/insights", tenant="del", days=1).json()
        self.assertEqual(body["sample"]["prs"], 0)
        # prd/37: the stale-PR rule is a *snapshot* fact (what is open now),
        # not a cohort metric, so it survives a one-day window; every cohort
        # rule is gone.
        ids = {suggestion["id"] for suggestion in body["suggestions"]}
        self.assertEqual(ids - {"stale-prs"}, set())

    def test_a_suggestion_expands_into_its_linked_items(self):
        """prd/26 P5: the payload carries the rows the Rules page expands."""
        body = get("/api/insights", tenant="del", level="team", days=3650).json()
        self.assertTrue(body["suggestions"])
        for suggestion in body["suggestions"]:
            metric = body["metrics"][suggestion["metric"]]
            rows = metric.get("rows") or metric.get("prs") or metric.get("people") or []
            self.assertTrue(rows, suggestion["id"])
            for row in rows[:3]:
                if row.get("kind") in ("pr", "thread", "story"):
                    # an item the UI can render and link: identity + ticket keys
                    self.assertTrue(row.get("object_id"), suggestion["id"])
                    self.assertIn("tickets", row, suggestion["id"])
        # the rebase rows are the PRs that lost an approval, with the cause
        rebase = body["metrics"]["rebase_waste"]["rows"]
        self.assertEqual(rebase[0]["tickets"], ["ENG-1"])
        self.assertEqual(rebase[0]["lost_on_pure_rebase"], 1)

    # -- prd/39: the PR-size distributions + caps --------------------------- #
    def test_pr_size_endpoint_returns_the_store_distribution(self):
        reply = get("/api/pr-size", tenant="del")
        self.assertEqual(reply.status, 200, reply.body.decode())
        self.assertEqual(reply.headers.get("x-tenant"), "del")
        body = reply.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["tenant"], "del")
        self.assertEqual(body["n"], 3)
        # commits per PR match the COMMIT objects in the store
        rows = connect(self.store, read_only=True).execute(
            "SELECT pr_id, count(*) FROM objects WHERE object_type = 'COMMIT'"
            " GROUP BY pr_id ORDER BY pr_id"
        ).fetchall()
        expected = sorted(int(count) for _pr_id, count in rows)
        self.assertEqual(body["commits"]["values"], expected)
        self.assertEqual(sum(body["commits"]["counts"]), body["n"])
        self.assertIn(body["caps"]["commits"], body["commits"]["edges"])
        # changed lines come from the PR object's lines_added + lines_removed
        self.assertTrue(body["lines"]["available"])
        self.assertEqual(body["lines"]["values"], [15])
        self.assertEqual(body["large_prs"], 1)          # #1: 12 commits > 10
        self.assertEqual(body["over_commits_cap"], 1)
        self.assertEqual(body["defaults"], {"commits": 10, "lines": 400})

    def test_pr_size_caps_rebucket_the_count_without_a_reload(self):
        default = get("/api/pr-size", tenant="del").json()
        self.assertEqual(default["large_prs"], 1)
        tighter = get("/api/pr-size", tenant="del", size_commits=1).json()
        self.assertEqual(tighter["caps"]["commits"], 1)
        self.assertEqual(tighter["large_prs"], 2)       # 12 and 2 commits
        looser = get("/api/pr-size", tenant="del", size_commits=50).json()
        self.assertEqual(looser["large_prs"], 0)
        # the line slider is independent of the commit slider
        lines = get("/api/pr-size", tenant="del", size_commits=50, size_lines=20).json()
        self.assertEqual(lines["caps"]["lines"], 20)
        self.assertEqual(lines["large_prs"], 0)         # 15 changed lines
        self.assertEqual(
            get("/api/pr-size", tenant="del", size_commits=1, size_lines=20).json()["large_prs"],
            2,
        )

    def test_the_size_rule_uses_the_requested_caps(self):
        body = get("/api/insights", tenant="del", days=3650, size_commits=1).json()
        self.assertEqual(body["targets"]["size_commit_cap"], 1)
        self.assertEqual(body["targets"]["size_default_commit_cap"], 10)
        self.assertEqual(body["metrics"]["pr_size"]["commits_cap"], 1)
        self.assertEqual(body["metrics"]["pr_size"]["caps"]["commits"], 1)
        self.assertEqual(body["metrics"]["pr_size"]["large_prs"], 2)

    def test_pr_size_rejects_bad_caps_and_unknown_params(self):
        for query in ({"size_commits": 0}, {"size_lines": -5},
                      {"size_commits": "many"}, {"metric": "pr_size"}):
            with self.subTest(query=query):
                reply = get("/api/pr-size", tenant="del", **query)
                self.assertEqual(reply.status, 400, reply.body.decode())
                self.assertIn("valid", reply.json())

    def test_pr_size_is_cached_per_store_and_caps(self):
        real = app_module.insights_rules.pr_size_distribution
        with mock.patch.object(
            app_module.insights_rules, "pr_size_distribution", side_effect=real
        ) as distribution:
            first = get("/api/pr-size", tenant="del")
            second = get("/api/pr-size", tenant="del")
            self.assertEqual(first.body, second.body)
            self.assertEqual(distribution.call_count, 1)      # one build, not two
            # a different calibration is a different answer, not a cache hit
            third = get("/api/pr-size", tenant="del", size_commits=5)
            self.assertEqual(third.status, 200)
            self.assertEqual(distribution.call_count, 2)

    def test_pr_size_is_tenant_scoped(self):
        reply = get("/api/pr-size", tenant="nope")
        self.assertEqual(reply.status, 404)
        self.assertEqual(reply.json()["valid"], ["del"])

    def test_an_unknown_tenant_is_a_clean_404(self):
        reply = get("/api/insights", tenant="nope")
        self.assertEqual(reply.status, 404)
        self.assertEqual(reply.json()["valid"], ["del"])

    def test_a_bad_level_lists_the_valid_ones(self):
        reply = get("/api/insights", tenant="del", level="queue")
        self.assertEqual(reply.status, 400)
        self.assertIn("team", reply.json()["error"])
        self.assertIn("level", reply.json()["valid"])

    def test_an_unknown_parameter_is_rejected(self):
        reply = get("/api/insights", tenant="del", metric="rebase")
        self.assertEqual(reply.status, 400)
        self.assertIn("metric", reply.json()["error"])


if __name__ == "__main__":
    unittest.main()

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

from analysis import ballcourt, rules
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
            write_events(con, self.events, [])
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

    def metric(self, fixture: Fixture, name: str, days: float | None = None):
        court, threads, sizes, _bots = self.load(fixture)
        window = rules.window_for(court, days)
        return rules.metrics(court, threads, window, sizes)[name]

    def insights(self, fixture: Fixture, level: str = "team", days: float | None = None):
        court, threads, sizes, bots = self.load(fixture)
        window = rules.window_for(court, days)
        return rules.insights(
            court, threads, window, level=level, bots=bots, sizes=sizes
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
        self.assertEqual(suggestion["severity"], "warning")
        self.assertEqual(suggestion["impact_hours"], 32.0)   # (80 − 72) × 4
        self.assertEqual(suggestion["evidence"]["merged_prs"], 4)


# --------------------------------------------------------------------------- #
# 3. reviewer_responsiveness
# --------------------------------------------------------------------------- #
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
    def fixture(self) -> Fixture:
        fx = Fixture()
        fx.pr(1, "AaAa", state="merged", closed=T0 + timedelta(hours=100))
        fx.event(1, "pr_pushed", "AaAa", 10, push_class="content")
        fx.event(1, "pr_pushed", "AaAa", 20, push_class="content")
        fx.event(1, "pr_unapproved", "RrRr", 30, cause="push")
        fx.event(1, "pr_approved", "RrRr", 50)
        fx.event(1, "pr_merged", "RrRr", 100)
        fx.pr(2, "AaAa")
        fx.event(2, "pr_pushed", "AaAa", 10, push_class="pure_rebase")
        fx.event(2, "pr_pushed", "AaAa", 20, push_class="rebase_conflict")
        fx.pr(3, "AaAa")
        return fx

    def test_rounds_are_split_by_push_class(self):
        metric = self.metric(self.fixture(), "rework_loops")
        self.assertEqual(metric["n"], 3)
        self.assertEqual(metric["total_rounds"], 5)
        self.assertEqual(metric["median_rounds"], 2.0)
        self.assertEqual(metric["p90_rounds"], 3.0)
        self.assertEqual(metric["prs_over_2"], 1)
        self.assertEqual(metric["excess_rounds"], 1)
        self.assertEqual(metric["by_push_class"]["content"], {"pushes": 2, "prs": 1})
        self.assertEqual(metric["by_push_class"]["pure_rebase"], {"pushes": 1, "prs": 1})
        self.assertEqual(metric["by_push_class"]["rebase_conflict"], {"pushes": 1, "prs": 1})

    def test_emits_a_round_cap_rule(self):
        suggestion = by_id(self.insights(self.fixture()), "rework-loops")
        self.assertIsNotNone(suggestion)
        # one closed PR: 100 h cycle ÷ 3 rounds = 33.33 h per round
        self.assertAlmostEqual(suggestion["impact_hours"], 33.3, places=1)
        self.assertEqual(suggestion["evidence"]["excess_rounds"], 1)
        self.assertEqual(suggestion["evidence"]["prs_over_2"], 1)


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
        # four rounds, then approved
        fx.pr(2, "AaAa", state="merged", closed=T0 + timedelta(hours=200))
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
        metric = self.metric(self.fixture(), "sentinel_queue")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["sentinels"], ["RrRr"])
        self.assertEqual(metric["n"], 2)                 # 100 h + 1 h review ball
        self.assertEqual(metric["median_hours"], 50.5)

    def test_emits_a_rotation_rule(self):
        suggestion = by_id(self.insights(self.fixture()), "sentinel-queue")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["severity"], "warning")
        self.assertEqual(suggestion["impact_hours"], 53.0)   # (50.5 − 24) × 2
        self.assertEqual(suggestion["evidence"]["sentinels"], 1)

    def test_no_role_data_means_no_rule(self):
        payload = self.insights(self.fixture(sentinel=False))
        self.assertFalse(payload["metrics"]["sentinel_queue"]["available"])
        self.assertIsNone(by_id(payload, "sentinel-queue"))


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
        for index in range(4):
            fx.event(1, "pr_pushed", "AaAa", 10 + index, push_class="content")
        fx.pr(2, "AaAa")
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
        return fx

    def setUp(self):
        app_module._analysis_cache = None

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
        self.assertEqual(body["suggestions"], [])

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

"""Stacked-PR cascade (prd/22): the stack DAG, the cascade cost, the rule.

The synthetic store is a three-PR stack -- C on B on A -- with hand-written
events and hand-computed hours, so a change in the edge rule, the depth, the
round pricing or the suggestion fails with the exact number that moved:

    A #1  feature/ENG-1-base  -> main            merged at +24 h
    B #2  feature/ENG-1-b     -> feature/ENG-1-base
    C #3  feature/ENG-1-c     -> feature/ENG-1-b

A merges, and the two PRs above it rebase (``pure_rebase``) and re-enter
review: B 10 h, C 4 h. So one bottom-of-stack merge cost 2 rebases and 14 h of
re-review, and the critical path is the whole chain.

The honest negatives are pinned too: a store whose PRs carry no ref digests
(ingested before prd/22) says ``available: false`` rather than "0 stacks", and a
corpus where every PR targets main says ``stacks: 0`` -- both emit no rule.

The ingest half (refs persisted as digests, ``--verify`` rejecting a branch
name) is pinned in ``tests/test_ref_digest.py``.

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

from analysis import ballcourt, rules, stack
from ingest.pipeline import ref_digest
from ingest.store import (
    connect, ensure_schema, write_events, write_objects, write_run,
)
from server import app as app_module

REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = T0 + timedelta(days=7)
#: The per-tenant pepper the ingest would hold in the raw zone (SEC-1).
PEPPER = bytes.fromhex("5e" * 32)


def digest(branch: str, repo: str = REPO) -> str:
    return ref_digest(repo, {"displayId": branch}, PEPPER)


# --------------------------------------------------------------------------- #
# a minimal store: PR objects + events, nothing else needed for the stack
# --------------------------------------------------------------------------- #
class Fixture:
    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
        self._count = 0

    def pr(self, number: int, source: str | None, target: str | None,
           opened: datetime = T0, state: str = "open",
           closed: datetime | None = None, author: str = "AaAa",
           tickets: tuple[str, ...] = ("ENG-1",),
           extra_attributes: dict | None = None) -> "Fixture":
        """One PR. ``source``/``target`` are branch names *only* to build the
        digests here: the store gets the digests, never the names."""
        self.opened[number] = opened
        attributes = dict(extra_attributes or {})
        if source:
            attributes["source_ref"] = digest(source)
        if target:
            attributes["target_ref"] = digest(target)
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
            "attributes": attributes,
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

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, [])
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-stack",
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


def stack_fixture(with_refs: bool = True, partial_refs: bool = False) -> Fixture:
    """C on B on A; A merges, B and C rebase and are re-reviewed.

    ``with_refs=False`` builds the same events with no ref digests at all (a
    store ingested before prd/22); ``partial_refs=True`` writes one side only,
    which must still place the PR in the DAG.
    """
    fx = Fixture()
    if partial_refs:
        refs = [("feature/ENG-1-base", None), (None, "feature/ENG-1-base"), (None, None)]
    elif with_refs:
        refs = [
            ("feature/ENG-1-base", "main"),
            ("feature/ENG-1-b", "feature/ENG-1-base"),
            ("feature/ENG-1-c", "feature/ENG-1-b"),
        ]
    else:
        refs = [(None, None)] * 3
    fx.pr(1, *refs[0], opened=T0, state="merged",
          closed=T0 + timedelta(hours=24))
    fx.pr(2, *refs[1], opened=T0 + timedelta(hours=1))
    fx.pr(3, *refs[2], opened=T0 + timedelta(hours=2))

    # A lands: the trigger. Its own review round is not part of the cascade.
    fx.event(1, "pr_approved", "RrRr", 23)
    fx.event(1, "pr_merged", "RrRr", 24)

    # B rebases 2 h after the merge and waits 10 h for the re-review to end.
    fx.event(2, "pr_pushed", "AaAa", 25, push_class="pure_rebase")   # +24 h from T0
    fx.event(2, "pr_approved", "RrRr", 35)

    # C rebases 3 h after the merge and waits 4 h.
    fx.event(3, "pr_pushed", "AaAa", 25, push_class="pure_rebase")   # +23 h from T0
    fx.event(3, "pr_approved", "RrRr", 29)
    return fx


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def court(self, fixture: Fixture) -> ballcourt.BallCourt:
        fixture.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            return ballcourt.load(con)
        finally:
            con.close()

    def payload(self, fixture: Fixture) -> dict:
        court = self.court(fixture)
        con = connect(self.path, read_only=True)
        try:
            threads = rules.load_threads(con)
            sizes = rules.load_pr_sizes(con)
            bots = rules.load_bots(con)
        finally:
            con.close()
        window = rules.window_for(court, None)
        return rules.insights(court, threads, window, bots=bots, sizes=sizes)


def by_id(payload: dict, suggestion_id: str) -> dict | None:
    return next((s for s in payload["suggestions"] if s["id"] == suggestion_id), None)


# --------------------------------------------------------------------------- #
# 1. the stack DAG
# --------------------------------------------------------------------------- #
class StackDagTests(StoreCase):
    def test_edges_follow_target_equals_source(self):
        graph = stack.build(self.court(stack_fixture()))
        self.assertEqual(
            graph.edges,
            [(f"PR:{REPO}#1", f"PR:{REPO}#2"), (f"PR:{REPO}#2", f"PR:{REPO}#3")],
        )
        self.assertEqual(graph.above(f"PR:{REPO}#1"), [f"PR:{REPO}#2", f"PR:{REPO}#3"])
        self.assertEqual(graph.above(f"PR:{REPO}#3"), [])

    def test_one_sided_refs_still_place_a_pr_in_the_dag(self):
        """A parent only needs its source, a child only its target."""
        graph = stack.build(self.court(stack_fixture(partial_refs=True)))
        self.assertEqual(graph.edges, [(f"PR:{REPO}#1", f"PR:{REPO}#2")])
        metric = rules.metrics(*self.metric_args(stack_fixture(partial_refs=True)))["stacked_cascade"]
        self.assertTrue(metric["available"])
        self.assertEqual(metric["prs_with_refs"], 2)

    def test_the_counts_are_cohort_consistent(self):
        """A window holding only the base must not report edges without stacks."""
        court = self.court(stack_fixture())
        window = (T0, T0 + timedelta(minutes=30))     # only PR #1 is in the cohort
        metric = rules.metrics(court, [], window, {})["stacked_cascade"]
        self.assertTrue(metric["available"])          # the store can derive stacks
        self.assertEqual(metric["stacks"], 0)         # but none is in this window
        self.assertEqual(metric["edges"], 0)
        self.assertEqual(metric["stacked_prs"], 0)
        self.assertEqual(metric["depth"]["median"], None)

    def test_depth_is_the_longest_chain_below_a_pr(self):
        graph = stack.build(self.court(stack_fixture()))
        self.assertEqual(
            graph.depth,
            {f"PR:{REPO}#1": 0, f"PR:{REPO}#2": 1, f"PR:{REPO}#3": 2},
        )

    def test_depth_distribution_is_median_and_max_over_stacked_prs(self):
        metric = rules.metrics(*self.metric_args(stack_fixture()))["stacked_cascade"]
        self.assertTrue(metric["available"])
        self.assertEqual(metric["stacks"], 1)
        self.assertEqual(metric["stacked_prs"], 2)          # B and C, not the base
        self.assertEqual(metric["edges"], 2)
        self.assertEqual(metric["depth"]["median"], 1.5)     # (1 + 2) / 2
        self.assertEqual(metric["depth"]["max"], 2)
        self.assertEqual(metric["depth"]["histogram"], {"1": 1, "2": 1})
        self.assertEqual(metric["depth"]["deep_prs"], 0)     # nothing at depth 3
        self.assertEqual(metric["stacks_detail"][0]["prs"], [1, 2, 3])
        self.assertEqual(metric["stacks_detail"][0]["depth"], 2)

    def metric_args(self, fixture: Fixture):
        court = self.court(fixture)
        return court, [], rules.window_for(court, None), {}


# --------------------------------------------------------------------------- #
# 2. the cascade cost
# --------------------------------------------------------------------------- #
class CascadeCostTests(StoreCase):
    def metric(self, fixture: Fixture) -> dict:
        court = self.court(fixture)
        return rules.metrics(court, [], rules.window_for(court, None), {})["stacked_cascade"]

    def test_one_bottom_merge_prices_every_rebase_above_it(self):
        metric = self.metric(stack_fixture())
        self.assertEqual(metric["triggers"], 1)              # A's merge
        self.assertEqual(metric["cascaded_rebases"], 2)      # B and C
        self.assertEqual(metric["re_review_hours"], 14.0)    # 10 h + 4 h
        self.assertEqual(metric["unfinished_rounds"], 0)
        self.assertEqual(
            metric["critical_path"],
            {"repo": REPO, "chain": [1, 2, 3], "hours": 14.0, "rebases": 2},
        )

    def test_a_content_push_is_not_a_rebase(self):
        fx = stack_fixture()
        # B answers the merge with a content push, not a rebase: no cascade there
        fx.events = [
            event for event in fx.events
            if not (event["pr_id"] == 2 and event["activity"] == "pr_pushed")
        ]
        fx.event(2, "pr_pushed", "AaAa", 25, push_class="content")
        metric = self.metric(fx)
        self.assertEqual(metric["cascaded_rebases"], 1)
        self.assertEqual(metric["re_review_hours"], 4.0)

    def test_a_rebase_without_a_finished_round_is_reported_not_priced(self):
        fx = stack_fixture()
        fx.events = [
            event for event in fx.events
            if not (event["pr_id"] == 2 and event["activity"] == "pr_approved")
        ]
        metric = self.metric(fx)
        self.assertEqual(metric["cascaded_rebases"], 2)
        self.assertEqual(metric["unfinished_rounds"], 1)     # B never re-approved
        self.assertEqual(metric["re_review_hours"], 4.0)     # C only

    def test_a_pr_above_that_already_landed_is_not_charged(self):
        fx = stack_fixture()
        # C lands before A merges, so A's merge cannot force anything on C
        fx.objects = [
            obj if not (obj["object_id"] == f"PR:{REPO}#3") else
            {**obj, "state": "merged", "closed_at": T0 + timedelta(hours=20)}
            for obj in fx.objects
        ]
        metric = self.metric(fx)
        self.assertEqual(metric["cascaded_rebases"], 1)      # B only
        self.assertEqual(metric["re_review_hours"], 10.0)

    def test_a_lower_rebase_push_is_a_trigger_too(self):
        # A is rebased (not merged); B and C above it still rebase and re-review
        fx = stack_fixture()
        fx.events = [
            event for event in fx.events
            if not (event["pr_id"] == 1 and event["activity"] == "pr_merged")
        ]
        fx.event(1, "pr_pushed", "AaAa", 24, push_class="pure_rebase")
        metric = self.metric(fx)
        self.assertEqual(metric["triggers"], 1)              # A's rebase
        self.assertEqual(metric["cascaded_rebases"], 2)      # B and C
        self.assertEqual(metric["re_review_hours"], 14.0)

    def test_one_rebase_is_never_charged_to_two_triggers(self):
        # A merges, then B rebases: both move the branch under C, but C pushes
        # once -- the pair is attributed to the earliest trigger only
        metric = self.metric(stack_fixture())
        self.assertEqual(metric["triggers"], 1)
        self.assertEqual(metric["cascaded_rebases"], 2)

    def test_equal_stacks_pick_a_chain_without_comparing_ids_to_numbers(self):
        """Regression: the tie-break compared PR numbers with object ids."""
        fx = Fixture()
        for base in (1, 11):
            fx.pr(base, f"feature/ENG-{base}", "main",
                  state="merged", closed=T0 + timedelta(hours=24))
            fx.pr(base + 1, f"feature/ENG-{base}-b", f"feature/ENG-{base}",
                  opened=T0 + timedelta(hours=1))
            fx.event(base, "pr_merged", "RrRr", 24)
            fx.event(base + 1, "pr_pushed", "AaAa", 25, push_class="pure_rebase")
            fx.event(base + 1, "pr_approved", "RrRr", 27)
        metric = self.metric(fx)
        self.assertEqual(metric["stacks"], 2)
        self.assertEqual(metric["cascaded_rebases"], 2)
        # both stacks cost the same: the pick is deterministic (lowest ids)
        self.assertEqual(metric["critical_path"]["chain"], [1, 2])
        self.assertEqual(metric["critical_path"]["hours"], 2.0)


# --------------------------------------------------------------------------- #
# 3. the suggestion
# --------------------------------------------------------------------------- #
class StackedCascadeRuleTests(StoreCase):
    def test_the_rule_names_the_rebases_the_hours_and_the_depth(self):
        payload = self.payload(stack_fixture())
        suggestion = by_id(payload, "stacked-cascade")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["metric"], "stacked_cascade")
        self.assertEqual(suggestion["impact_hours"], 14.0)
        self.assertIn("2 stacked PRs", suggestion["title"])
        self.assertIn("2 rebases", suggestion["title"])
        self.assertIn("14 h", suggestion["title"])
        self.assertIn("bottom-up", suggestion["suggestion"])
        self.assertIn("depth", suggestion["suggestion"])
        evidence = suggestion["evidence"]
        self.assertEqual(evidence["stacked_prs"], 2)
        self.assertEqual(evidence["rebases"], 2)
        self.assertEqual(evidence["re_review_hours"], 14.0)
        self.assertEqual(evidence["depth_median"], 1.5)
        self.assertEqual(evidence["depth_max"], 2)
        self.assertEqual(evidence["critical_path_hours"], 14.0)
        self.assertEqual(evidence["triggers"], 1)
        # severity is a multiple of the measured round cost, not a fixed hour
        round_hours = payload["targets"]["hours_per_round"]
        expected = (
            "critical" if 14.0 >= 3 * round_hours
            else ("warning" if 14.0 >= round_hours else "info")
        )
        self.assertEqual(suggestion["severity"], expected)

    def test_the_api_payload_carries_the_metric_and_the_sample(self):
        payload = self.payload(stack_fixture())
        self.assertTrue(payload["metrics"]["stacked_cascade"]["available"])
        self.assertEqual(payload["sample"]["stacked_prs"], 2)
        self.assertEqual(payload["targets"]["max_stack_depth"], 2)
        self.assertIn("stacked-cascade", {s["id"] for s in payload["suggestions"]})

    def test_no_refs_is_an_honest_negative_not_a_zero(self):
        payload = self.payload(stack_fixture(with_refs=False))
        metric = payload["metrics"]["stacked_cascade"]
        self.assertFalse(metric["available"])
        self.assertEqual(metric["prs_with_refs"], 0)
        self.assertEqual(metric["stacked_prs"], 0)
        self.assertIn("predates", metric["note"])
        self.assertIsNone(by_id(payload, "stacked-cascade"))

    def test_no_stacks_is_an_honest_negative_not_a_missing_measurement(self):
        fx = Fixture()
        for number in (1, 2, 3):
            fx.pr(number, f"feature/ENG-{number}", "main")
        payload = self.payload(fx)
        metric = payload["metrics"]["stacked_cascade"]
        self.assertTrue(metric["available"])
        self.assertEqual(metric["stacks"], 0)
        self.assertEqual(metric["stacked_prs"], 0)
        self.assertEqual(metric["edges"], 0)
        self.assertIn("no stacked PRs", metric["note"])
        self.assertIsNone(by_id(payload, "stacked-cascade"))


# --------------------------------------------------------------------------- #
# 4. the API: /api/insights carries the metric and the rule
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
    return Reply(start["status"], dict(start.get("headers") or []), body)


def get(path: str, **query: object) -> Reply:
    return asyncio.run(_call_asgi(app_module.app, path, urlencode(query)))


class InsightsApiTests(unittest.TestCase):
    """The real ASGI app against a temp tenant directory, like test_rules."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        stack_fixture().write(cls.dir / "del.duckdb")
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

    def test_the_payload_carries_the_stack_metric_and_rule(self):
        reply = get("/api/insights", tenant="del", level="team", days=3650)
        self.assertEqual(reply.status, 200)
        body = reply.json()
        self.assertTrue(body["ok"])
        metric = body["metrics"]["stacked_cascade"]
        self.assertTrue(metric["available"])
        self.assertEqual(metric["stacked_prs"], 2)
        self.assertEqual(metric["depth"]["max"], 2)
        self.assertEqual(metric["cascaded_rebases"], 2)
        self.assertGreater(metric["re_review_hours"], 0)
        self.assertEqual(body["sample"]["stacked_prs"], 2)
        self.assertEqual(body["targets"]["max_stack_depth"], 2)
        suggestion = by_id(body, "stacked-cascade")
        self.assertIsNotNone(suggestion)
        self.assertEqual(suggestion["metric"], "stacked_cascade")
        # ranking metadata is filled in like every other rule
        self.assertGreaterEqual(suggestion["rank"], 1)
        self.assertIn("stacked", suggestion["title"])


if __name__ == "__main__":
    unittest.main()

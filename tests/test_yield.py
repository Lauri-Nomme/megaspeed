"""First-pass yield and stability (prd/62).

The Rules page recommends speed levers and never showed what they cost. These
tests pin the cost side against hand-built stories whose loop-backs are known to
the minute:

* per-step %C&A and the product — VSM's **rolled %C&A** — plus the joint clean
  count next to it;
* the stability rates (reopen / failed verification / review bounce), the
  labelled rework proxy and the *unavailable* bug share (the store carries no
  issue type; ``summary_class`` is never substituted);
* the edge cases: <10 deliveries (counts, no rate), a step whose falsifying
  transitions are not in the store (unfalsifiable, never a perfect score), a
  story with several PRs and a story with none.

The del-combined pin runs only when that store is present (``MEGASPEED_DB`` or
``data/del-combined.duckdb``); it records the 66/87 · 11/87 · 78/87 · rolled
0.086 · joint 8 evidence from the PRD.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from analysis import ballcourt, jira, rules
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from server import app as app_module

REPO = "DEL/web"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = datetime(2026, 3, 2, 9, 0)


class Fixture:
    """Stories with explicit status histories and one merged PR each by default."""

    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
        self._count = 0
        self._pr = 0

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

    def story(self, key: str, *, summary_class: str = "feature") -> None:
        self._object(
            "STORY", f"STORY:{key}", ticket_keys=[key], actor="AaAa",
            state="done", created_at=T0, closed_at=T0 + timedelta(hours=30),
            attributes={
                "summary_class": summary_class, "epic_key": "DEL-1", "points": 3,
            },
        )
        self.event(key, T0, "created")

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

    def status(self, key: str, hours: float, to_status: str, from_status: str) -> None:
        self.event(key, T0 + timedelta(hours=hours), "status_changed",
                   from_status=from_status, to_status=to_status)

    def lifecycle(
        self, key: str, *,
        refine_bounce: bool = False,
        point_churn: bool = False,
        develop_bounce: bool = False,
        failed_verification: bool = False,
        reopened: bool = False,
        review_rounds: int = 0,
        in_review_stage: bool = True,
        prs: int = 1,
    ) -> None:
        """One completed story, with exactly the requested loop-backs."""
        self.story(key)
        self.status(key, 1, "ready", "backlog")
        if refine_bounce:
            # ready → backlog: the workflow sent the story back to refinement
            self.status(key, 1.5, "backlog", "ready")
            self.status(key, 2, "in_progress", "backlog")
        else:
            self.status(key, 2, "in_progress", "ready")
        if point_churn:
            # re-pointed after work started: a refinement miss
            self.event(key, T0 + timedelta(hours=12), "pointed", points=5)
        if in_review_stage:
            self.status(key, 10, "in_review", "in_progress")
            if develop_bounce:
                self.status(key, 12, "in_progress", "in_review")
                self.status(key, 14, "in_review", "in_progress")
            self.status(key, 18, "in_verification", "in_review")
        else:
            self.status(key, 18, "in_verification", "in_progress")
        if failed_verification:
            self.status(key, 20, "in_progress", "in_verification")
            self.status(key, 22, "in_verification", "in_progress")
        if reopened:
            self.status(key, 24, "done", "in_verification")
            self.status(key, 25, "ready", "done")
            self.status(key, 26, "in_progress", "ready")
            self.status(key, 28, "done", "in_progress")
        else:
            self.status(key, 28, "done", "in_verification")
        for index in range(prs):
            self.pr(key, review_rounds=review_rounds, index=index)

    def pr(self, key: str, *, review_rounds: int = 0, index: int = 0) -> None:
        self._pr += 1
        number = self._pr
        opened = T0 + timedelta(hours=11 + index)
        self.opened[number] = opened
        self._object(
            "PR", f"PR:{REPO}#{number}", repo=REPO, pr_id=number,
            ticket_keys=[key], actor="AaAa", state="merged",
            created_at=opened, closed_at=opened + timedelta(hours=7),
            head_commit="a" * 40,
        )
        self.pr_event(number, opened, "pr_opened", "AaAa")
        self.pr_event(number, opened, "reviewer_changed", "AaAa", change="added")
        # the approval is the first review; a round is a push after it
        self.pr_event(number, opened + timedelta(hours=2), "pr_approved", "RrRr")
        for round_index in range(review_rounds):
            self.pr_event(
                number,
                opened + timedelta(hours=3 + round_index),
                "pr_pushed", "AaAa", push_class="content",
            )
            self.pr_event(
                number,
                opened + timedelta(hours=4 + round_index),
                "pr_approved", "RrRr",
            )
        self.pr_event(number, opened + timedelta(hours=7), "pr_merged", "AaAa")

    def pr_event(self, number: int, ts: datetime, activity: str, actor: str,
                 **attributes) -> None:
        self._count += 1
        event_key = f"{REPO}#{number}:{self._count}"
        self.events.append({
            "event_key": event_key,
            "ts": ts,
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

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-yield",
                "source": "fixture",
                "as_of": self.as_of,
                "source_watermark": self.as_of,
                "classifier_version": "deterministic-v1",
                "prs_seen": self._pr,
                "prs_kept": self._pr,
                "prs_dropped": 0,
                "events_written": len(self.events),
                "events_skipped": 0,
                "issues_seen": 0,
                "issues_kept": 0,
                "issues_skipped": 0,
            })
        finally:
            con.close()
        return path


def twelve_stories() -> Fixture:
    """12 deliveries, one distinct loop-back per story — every count is exact.

    refine dirty: S1 (ready → backlog), S2 (re-pointed after started)  → 10/12
    develop dirty: S3 (in_review → in_progress)                       → 11/12
    review dirty: S4, S5, S6 (one round on the merged PR)             → 9/12
    verify dirty: S7 (failed verification), S8 (reopened)             → 10/12
    clean everywhere: S9–S12                                          → joint 4
    """
    fx = Fixture()
    fx.lifecycle("S1", refine_bounce=True)
    fx.lifecycle("S2", point_churn=True)
    fx.lifecycle("S3", develop_bounce=True)
    fx.lifecycle("S4", review_rounds=1)
    fx.lifecycle("S5", review_rounds=1)
    fx.lifecycle("S6", review_rounds=1)
    fx.lifecycle("S7", failed_verification=True)
    fx.lifecycle("S8", reopened=True)
    for key in ("S9", "S10", "S11", "S12"):
        fx.lifecycle(key)
    return fx


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fx: Fixture | None = None, path: Path | None = None):
        target = path or self.path
        (fx or twelve_stories()).write(target)
        con = connect(target, read_only=True)
        try:
            court = ballcourt.load(con)
            data = jira.load(con, court)
            return con, court, data
        finally:
            con.close()

    def payload(self, fx: Fixture | None = None) -> dict:
        con, court, data = self.load(fx)
        window = rules.window_for(court, None, data)
        return jira.first_pass_yield(court, data, window)


class YieldTests(StoreCase):
    def test_each_step_counts_the_stories_that_looped(self):
        payload = self.payload()
        steps = {step["id"]: step for step in payload["steps"]}
        self.assertEqual([step["id"] for step in payload["steps"]],
                         ["refine", "develop", "review", "verify"])
        self.assertEqual((steps["refine"]["clean_n"], steps["refine"]["n"]), (10, 12))
        self.assertEqual((steps["develop"]["clean_n"], steps["develop"]["n"]), (11, 12))
        self.assertEqual((steps["review"]["clean_n"], steps["review"]["n"]), (9, 12))
        self.assertEqual((steps["verify"]["clean_n"], steps["verify"]["n"]), (10, 12))
        self.assertEqual(steps["refine"]["rate"], 0.833)
        self.assertEqual(steps["develop"]["rate"], 0.917)
        self.assertEqual(steps["review"]["rate"], 0.75)
        self.assertEqual(steps["verify"]["rate"], 0.833)
        self.assertEqual(steps["review"]["items"], ["S4", "S5", "S6"])
        self.assertEqual(steps["develop"]["items"], ["S3"])
        self.assertEqual(steps["refine"]["items"], ["S1", "S2"])
        self.assertEqual(steps["verify"]["items"], ["S7", "S8"])

    def test_rolled_is_the_product_and_the_joint_count_is_printed(self):
        payload = self.payload()
        self.assertEqual(payload["n"], 12)
        self.assertEqual(payload["rolled"], 0.477)      # 10/12 · 11/12 · 9/12 · 10/12
        self.assertAlmostEqual(
            payload["rolled"],
            (10 / 12) * (11 / 12) * (9 / 12) * (10 / 12), places=3,
        )
        self.assertEqual(payload["clean_joint"], 4)
        self.assertEqual(payload["joint_rate"], 0.333)
        # the joint count is not the product: both are stated, and the basis
        # explains why they can differ
        self.assertIn("product of the four steps", payload["basis"])
        self.assertIn("joint count", payload["basis"])

    def test_the_drill_down_names_the_story_that_looped(self):
        payload = self.payload()
        steps = {step["id"]: step for step in payload["steps"]}
        self.assertEqual(steps["develop"]["items"], ["S3"])
        self.assertEqual(steps["review"]["items"], ["S4", "S5", "S6"])

    def test_a_step_with_no_falsifying_signal_is_unfalsifiable(self):
        fx = Fixture()
        for key in ("U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8", "U9", "U10", "U11"):
            fx.lifecycle(key, in_review_stage=False)
        payload = self.payload(fx)
        steps = {step["id"]: step for step in payload["steps"]}
        develop = steps["develop"]
        self.assertFalse(develop["available"])
        self.assertTrue(develop["unfalsifiable"])
        self.assertIsNone(develop["rate"])
        self.assertIsNone(develop["clean_n"])
        # and the roll-up cannot claim a perfect score from a missing signal
        self.assertIsNone(payload["rolled"])

    def test_below_ten_deliveries_counts_only(self):
        fx = Fixture()
        for key in ("F1", "F2", "F3", "F4", "F5"):
            fx.lifecycle(key)
        payload = self.payload(fx)
        self.assertEqual(payload["n"], 5)
        self.assertIsNone(payload["rolled"])
        self.assertEqual(payload["clean_joint"], 5)
        for step in payload["steps"]:
            self.assertIsNone(step["rate"])
            self.assertTrue(step["rate_hidden"])
            self.assertEqual(step["clean_n"], 5)
            self.assertIn("too few for a rate", step["note"])

    def test_a_story_with_two_prs_must_pass_review_twice(self):
        fx = Fixture()
        fx.lifecycle("M1", prs=2)
        con, court, data = self.load(fx)
        story = jira.story_for(data, "M1")
        self.assertEqual(len(jira._merged_prs(story)), 2)
        self.assertTrue(jira.story_yield(court, story)["steps"][2]["clean"])
        # a round on *either* PR makes the story unclean at review
        fx2 = Fixture()
        fx2.lifecycle("M2", prs=1)
        fx2.pr("M2", review_rounds=1, index=1)
        second = Path(self.tmp.name) / "second.duckdb"
        con2, court2, data2 = self.load(fx2, path=second)
        story2 = jira.story_for(data2, "M2")
        self.assertEqual(len(jira._merged_prs(story2)), 2)
        self.assertFalse(jira.story_yield(court2, story2)["steps"][2]["clean"])

    def test_a_story_with_no_pr_is_excluded_from_the_review_denominator(self):
        fx = Fixture()
        for key in ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10"):
            fx.lifecycle(key)
        # a Jira-only completed story: no PR at all
        fx.story("P11")
        fx.status("P11", 1, "ready", "backlog")
        fx.status("P11", 2, "in_progress", "ready")
        fx.status("P11", 10, "in_review", "in_progress")
        fx.status("P11", 28, "done", "in_review")
        con, court, data = self.load(fx)
        window = rules.window_for(court, None, data)
        payload = jira.first_pass_yield(court, data, window)
        self.assertEqual(payload["n"], 11)
        self.assertEqual(payload["no_pr"], 1)
        review = next(step for step in payload["steps"] if step["id"] == "review")
        self.assertEqual(review["n"], 10)
        # the story's own pass says the review step is not measurable, not that
        # it bounced
        story = jira.story_for(data, "P11")
        passline = jira.story_yield(court, story)
        review_step = next(
            step for step in passline["steps"] if step["id"] == "review"
        )
        self.assertFalse(review_step["available"])
        self.assertIn("no merged PR", review_step["note"])


class StabilityTests(StoreCase):
    def stability(self, fx: Fixture | None = None) -> dict:
        con, court, data = self.load(fx)
        window = rules.window_for(court, None, data)
        return jira.stability(court, data, window)

    def test_the_three_direct_rates_are_flip_counts_over_deliveries(self):
        payload = self.stability()
        self.assertEqual(payload["n"], 12)
        self.assertEqual(payload["reopen"]["count"], 1)
        self.assertEqual(payload["reopen"]["rate"], 0.083)
        self.assertEqual(payload["reopen"]["items"], ["S8"])
        self.assertEqual(payload["failed_verification"]["count"], 1)
        self.assertEqual(payload["failed_verification"]["rate"], 0.083)
        self.assertEqual(payload["failed_verification"]["items"], ["S7"])
        self.assertEqual(payload["review_bounce"]["count"], 1)
        self.assertEqual(payload["review_bounce"]["rate"], 0.083)
        self.assertEqual(payload["zero_approval_merges"]["count"], 0)

    def test_the_rework_proxy_is_labelled_and_adds_the_two_quality_signals(self):
        payload = self.stability()
        proxy = payload["rework_proxy"]
        self.assertTrue(proxy["proxy"])
        self.assertEqual(proxy["count"], 2)               # reopen + failed verification
        self.assertEqual(proxy["rate"], 0.167)
        self.assertIn("reopened or failed verification", proxy["basis"])
        self.assertIn("not DORA's rework rate", proxy["note"])

    def test_bug_share_is_unavailable_with_its_reason(self):
        payload = self.stability()
        bug = payload["bug_share"]
        self.assertFalse(bug["available"])
        self.assertIsNone(bug["rate"])
        self.assertIn("no issue type", bug["reason"])

    def test_summary_class_is_never_used_for_the_bug_share(self):
        # a store full of summary_class=bug must still refuse: summary_class
        # classifies the *summary text*, not the issue type.
        fx = twelve_stories()
        for obj in fx.objects:
            if obj["object_type"] == "STORY":
                obj["attributes"]["summary_class"] = "bug"
        payload = self.stability(fx)
        self.assertFalse(payload["bug_share"]["available"])
        source = Path(jira.__file__).read_text()
        block = source[source.index("def stability("):]
        block = block[:block.index("\ndef ", block.index("def story_yield"))]
        # summary_class classifies the summary text; the bug share reads only
        # the issue type, so the attribute is never touched here
        self.assertNotIn("story.summary_class", block)

    def test_an_issue_type_when_the_store_has_one(self):
        fx = twelve_stories()
        for index, obj in enumerate(
            (row for row in fx.objects if row["object_type"] == "STORY")
        ):
            obj["attributes"]["issue_type"] = "bug" if index < 3 else "story"
        payload = self.stability(fx)
        self.assertTrue(payload["bug_share"]["available"])
        self.assertEqual(payload["bug_share"]["count"], 3)
        self.assertEqual(payload["bug_share"]["rate"], 0.25)
        self.assertTrue(payload["bug_share"]["proxy"])


class NoJiraTests(StoreCase):
    def test_a_store_without_the_ticket_layer_refuses_with_a_reason(self):
        fx = Fixture()                 # no STORY objects at all
        fx.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            data = jira.load(con, court)
            window = rules.window_for(court, None, data)
            yield_payload = jira.first_pass_yield(court, data, window)
            stability = jira.stability(court, data, window)
        finally:
            con.close()
        self.assertFalse(yield_payload["available"])
        self.assertEqual(yield_payload["n"], 0)
        self.assertIn("no STORY objects", yield_payload["note"])
        self.assertFalse(stability["available"])
        self.assertIn("no STORY objects", stability["note"])


class TicketYieldTests(StoreCase):
    def test_the_ticket_payload_has_the_four_step_pass(self):
        con, court, data = self.load()
        story = jira.story_for(data, "S9")
        passline = jira.story_yield(court, story)
        self.assertEqual([step["id"] for step in passline["steps"]],
                         ["refine", "develop", "review", "verify"])
        self.assertTrue(passline["rolled_clean"])
        story = jira.story_for(data, "S3")
        self.assertFalse(jira.story_yield(court, story)["rolled_clean"])
        self.assertFalse(jira.story_yield(court, story)["steps"][1]["clean"])


def _del_combined_path() -> Path | None:
    """The PRD's evidence store, when it is present (never a different tenant)."""
    candidates = []
    env = os.environ.get("MEGASPEED_DB")
    if env:
        candidates.append(Path(env))
    candidates.append(
        Path(__file__).resolve().parent.parent / "data" / "del-combined.duckdb"
    )
    for path in candidates:
        if path.name == "del-combined.duckdb" and path.exists():
            return path
    return None


@unittest.skipUnless(
    _del_combined_path() is not None,
    "the del-combined store is not present",
)
class DelCombinedEvidenceTests(unittest.TestCase):
    """The PRD's own evidence: 66/87 · 11/87 · 78/87 · rolled 0.086 · joint 8."""

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

    def test_the_yield_matches_the_research(self):
        payload = jira.first_pass_yield(self.court, self.data, self.window)
        steps = {step["id"]: step for step in payload["steps"]}
        self.assertEqual(payload["n"], 87)
        self.assertEqual((steps["refine"]["clean_n"], steps["refine"]["n"]), (87, 87))
        self.assertEqual((steps["develop"]["clean_n"], steps["develop"]["n"]), (66, 87))
        # prd/81: the review step is anchored on the first *human review
        # remark*, not the first comment. This store predates the prd/75 thread
        # taxonomy (no ``comment_kind``), so only approvals are reviews — the
        # old 11/87 counted the first comment, whatever it was, as a review.
        self.assertEqual((steps["review"]["clean_n"], steps["review"]["n"]), (49, 87))
        self.assertEqual((steps["verify"]["clean_n"], steps["verify"]["n"]), (78, 87))
        # prd/81 retires the old 0.086 / 8: the all-four-steps pin is
        # recomputed against the human-review boundary, not the old one.
        self.assertEqual(payload["rolled"], 0.383)
        self.assertEqual(payload["clean_joint"], 33)

    def test_the_stability_rates_match_the_research(self):
        payload = jira.stability(self.court, self.data, self.window)
        self.assertEqual(payload["n"], 87)
        self.assertEqual(payload["reopen"]["count"], 5)
        self.assertEqual(payload["reopen"]["rate"], 0.057)
        self.assertEqual(payload["failed_verification"]["count"], 4)
        self.assertEqual(payload["failed_verification"]["rate"], 0.046)
        self.assertEqual(payload["review_bounce"]["count"], 21)
        self.assertEqual(payload["review_bounce"]["rate"], 0.241)
        self.assertFalse(payload["bug_share"]["available"])
        self.assertEqual(payload["rework_proxy"]["rate"], 0.103)


# --------------------------------------------------------------------------- #
# the API: both payloads ride /api/insights, and the ticket carries its pass
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


class YieldApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        twelve_stories().write(cls.dir / "del.duckdb")
        ticket_fx = Fixture()
        ticket_fx.lifecycle("DEL-3", develop_bounce=True)
        ticket_fx.write(cls.dir / "ticket.duckdb")
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
        app_module._advice_cache.clear()

    def test_insights_carry_the_yield_and_stability_panels(self):
        payload = get("/api/insights", tenant="del", days=3650).json()
        metrics = payload["metrics"]
        self.assertEqual(metrics["yield"]["rolled"], 0.477)
        self.assertEqual(metrics["yield"]["clean_joint"], 4)
        self.assertEqual(metrics["stability"]["reopen"]["rate"], 0.083)
        self.assertFalse(metrics["stability"]["bug_share"]["available"])
        # the measured %C&A never comes from the model
        self.assertEqual(metrics["yield"]["steps"][2]["clean_n"], 9)

    def test_the_ticket_payload_has_its_own_pass(self):
        payload = get("/api/ticket/DEL-3", tenant="ticket").json()
        self.assertEqual(payload["yield"]["steps"][1]["id"], "develop")
        self.assertFalse(payload["yield"]["steps"][1]["clean"])
        self.assertFalse(payload["yield"]["rolled_clean"])


class GuardrailTests(unittest.TestCase):
    """prd/62: a guardrail beside every lever — modelled or explicitly held."""

    STABILITY = {
        "available": True,
        "n": 87,
        "reopen": {"count": 5, "rate": 0.057},
        "failed_verification": {"count": 4, "rate": 0.046},
        "zero_approval_merges": {"count": 0, "n": 106},
        "bug_share": {"available": False, "reason": "no issue type"},
    }

    def compare(self, policy_id: str) -> dict:
        baseline = {
            "cycle_hours": {"n": 100, "median": 40.0, "p90": 100.0},
            "lead_hours": {"n": 100, "median": 50.0, "p90": 120.0},
            "flow_efficiency": {"aggregate": 0.30},
            "throughput_work_per_day": 5.0,
            "throughput_per_day": 1.0,
            "backlog_max": 0,
            "rounds": {"mean": 1.6, "p85": 3.0},
            "prs_per_100_commits": 16.2,
            "escalated_share": 0.0,
            "declined_per_100": 15.0,
        }
        scenario = {
            "cycle_hours": {"n": 100, "median": 34.0, "p90": 90.0},
            "lead_hours": {"n": 100, "median": 54.0, "p90": 126.0},
            "flow_efficiency": {"aggregate": 0.34},
            "throughput_work_per_day": 5.2,
            "throughput_per_day": 1.3,
            "backlog_max": 0,
            "rounds": {"mean": 0.8, "p85": 1.0},
            "prs_per_100_commits": 26.4,
            "escalated_share": 0.18,
            "declined_per_100": 15.0,
        }
        return simulation_compare(policy_id, baseline, scenario, self.STABILITY)

    def test_the_round_cap_says_modelled_and_what_it_holds(self):
        from analysis import advice
        report = advice.report({"roundcap2": self.compare("roundcap2")})
        entry = next(e for e in report["policies"] if e["id"] == "roundcap2")
        guard = entry["guardrail"]
        self.assertTrue(guard["modelled"])
        self.assertIn("rounds 1.6 → 0.8 (model)", guard["text"])
        self.assertIn("reopen 5.7 %", guard["text"])
        self.assertIn("held constant", guard["text"])
        self.assertIn("does not rework the code", guard["text"])

    def test_the_size_cap_shows_prs_per_100_commits_rising(self):
        from analysis import advice
        report = advice.report({"size3": self.compare("size3")})
        entry = next(e for e in report["policies"] if e["id"] == "size3")
        guard = entry["guardrail"]
        self.assertTrue(guard["modelled"])
        self.assertEqual(guard["label"], "PRs per 100 commits")
        self.assertIn("16.2 → 26.4", guard["text"])
        self.assertTrue(guard["worsens"])       # more PRs is the cost, tinted

    def test_held_constant_is_stated_when_the_model_has_no_modelled_fact(self):
        from analysis import advice
        held_only = {
            "modelled": [],
            "held_constant": [
                {"id": "reopen", "label": "reopened", "baseline": 0.057},
                {"id": "failed_verification", "label": "failed verification",
                 "baseline": 0.046},
            ],
            "not_modelled": [],
        }
        guard = advice._guardrail(
            {"id": "sentinel2"}, {"guardrails": held_only}
        )
        self.assertFalse(guard["modelled"])
        self.assertTrue(guard["text"].startswith("held constant:"))
        self.assertIn("reopen 5.7 %", guard["text"])

    def test_a_missing_lab_result_omits_the_guardrail(self):
        from analysis import advice
        report = advice.report({"roundcap2": None})
        suggestion = {"id": "round-cap"}
        advice.attach([suggestion], report)
        self.assertNotIn("policy_lab", suggestion)

    def test_the_guardrail_rides_the_rule(self):
        from analysis import advice
        suggestions = [{"id": "round-cap"}]
        advice.attach(suggestions, advice.report({"roundcap2": self.compare("roundcap2")}))
        guard = suggestions[0]["policy_lab"]["guardrail"]
        self.assertTrue(guard["modelled"])
        self.assertIn("reopen 5.7 %", guard["text"])


def simulation_compare(policy_id: str, baseline: dict, scenario: dict,
                       stability: dict) -> dict:
    """A compare block shaped exactly as ``simulation.compare`` produces."""
    from analysis import simulation
    left = {"predicted": baseline, "bottleneck": None}
    right = {"predicted": scenario, "bottleneck": None,
             "scenario": {"review_round_cap": 2}}
    return simulation.compare(left, right, held_constant=stability)


if __name__ == "__main__":
    unittest.main()

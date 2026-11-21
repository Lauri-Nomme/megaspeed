"""prd/64 G2 — no-Jira honesty (F-03) and the sprint "still open" miss (F-06b).

F-03: a store with no Jira layer has no ``created → ready`` segment, so
``refinement_cost`` reports ``hours: None``. The Policy lab must not model
``refine before start`` at a default, Rules must not rank it as the best
scenario, and the API must expose the unavailable state instead of a
fabricated delta. The ``del-combined`` result (``+82 % — not a measured win``)
is unchanged.

F-06b: a story still open at a sprint's close missed that sprint exactly like a
late one, so its drill-down row is ``affected: True``. The ``status`` stays
``never`` so the page can still say *why* it missed ("still open" vs "late");
the stories delivered by the end are not affected.

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

from analysis import advice, ballcourt, jira, rules, simulation
from ingest.store import (
    connect, ensure_schema, write_events, write_objects, write_run,
)
from server import app as app_module

REPO = "NOJ/only"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday, no calendar attached
AUTHORS = ("AaAa", "BbBb", "CcCc", "DdDd")
SENTINEL = "SsSs"


# --------------------------------------------------------------------------- #
# 1. a PR-only store (no Jira objects or events at all)
# --------------------------------------------------------------------------- #
class PrOnlyFixture:
    """N PRs with fixed offsets; the same shape as tests/test_simulation.py.

    The store has no STORY, SPRINT or Jira event, so the ticket layer is
    ``available: False`` and ``refinement_cost`` has nothing to measure.
    """

    def __init__(self, *, n_prs: int = 80, days: float = 20,
                 peer_first: float = 2.0, peer_second: float = 4.0,
                 gate: float = 6.0, merge: float = 7.0):
        self.as_of = T0 + timedelta(days=days)
        self.events: list[dict] = []
        self.objects: list[dict] = []
        self.n_prs = n_prs
        self.spacing = days * 24.0 / max(1, n_prs)
        self.peer_first = peer_first
        self.peer_second = peer_second
        self.gate = gate
        self.merge = merge
        self._count = 0
        for number in range(1, n_prs + 1):
            self._pr(number)

    def _pr(self, number: int) -> None:
        opened = T0 + timedelta(hours=(number - 1) * self.spacing)
        author = AUTHORS[(number - 1) % len(AUTHORS)]
        peer = AUTHORS[number % len(AUTHORS)]
        self.objects.append({
            "object_type": "PR",
            "object_id": f"PR:{REPO}#{number}",
            "repo": REPO,
            "pr_id": number,
            "ticket_keys": [],
            "actor": author,
            "state": "merged",
            "created_at": opened,
            "closed_at": opened + timedelta(hours=self.merge),
            "head_commit": "a" * 40,
            "message_class": None,
            "attributes": {"lines_added": 10, "lines_removed": 2},
        })
        self._event(number, "pr_opened", author, 0.0)
        self._event(number, "reviewer_changed", author, 0.0, change="added",
                    people=(peer,))
        self._event(number, "reviewer_changed", author, 0.0, change="added",
                    people=(SENTINEL,))
        self._event(number, "pr_pushed", author, 0.0, push_class="content",
                    push_index=1, commits_added=2, commits_removed=0)
        self._event(number, "comment_added", peer, self.peer_first,
                    severity="suggestion", thread_state="open")
        self._event(number, "pr_approved", peer, self.peer_second, sentinel=False)
        self._event(number, "pr_approved", SENTINEL, self.gate, sentinel=True)
        self._event(number, "pr_merged", author, self.merge, review_rounds=1)

    def _event(self, number: int, activity: str, actor: str, hours: float,
               **attributes) -> None:
        self._count += 1
        ts = T0 + timedelta(hours=(number - 1) * self.spacing + hours)
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

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, [])
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-prd64-g2",
                "source": "fixture",
                "as_of": self.as_of,
                "source_watermark": self.as_of,
                "classifier_version": "deterministic-v1",
                "prs_seen": self.n_prs,
                "prs_kept": self.n_prs,
                "prs_dropped": 0,
                "events_written": len(self.events),
                "events_skipped": 0,
            })
        finally:
            con.close()
        return path


class PrOnlyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "pr-only.duckdb"
        PrOnlyFixture().write(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def load(self):
        con = connect(self.path, read_only=True)
        court = ballcourt.load(con)
        data = jira.load(con, court)
        window = rules.window_for(court, None, data)
        return con, court, data, window


class RefinementUnmeasuredTests(PrOnlyCase):
    """F-03: no measured segment means None, not a default 1 h."""

    def test_refinement_cost_has_no_fallback(self):
        con, court, data, window = self.load()
        try:
            self.assertFalse(data.available)
            cost = jira.refinement_cost(court, data, window)
            self.assertIsNone(cost["hours"])
            self.assertEqual(cost["n"], 0)
            self.assertIn("no measured created → ready segment", cost["basis"])
            # the constant is gone: no module-level default can leak back in
            self.assertFalse(hasattr(jira, "DEFAULT_REFINEMENT_HOURS"))
        finally:
            con.close()

    def test_a_scenario_without_its_input_is_unavailable(self):
        con, court, data, window = self.load()
        try:
            cost = jira.refinement_cost(court, data, window)
            calibration = simulation.calibrate(
                court, window, conn=con, refinement=cost,
            )
            self.assertTrue(calibration["ok"])
            self.assertIsNone(calibration["refinement"]["hours"])

            result = simulation.run(
                calibration, simulation.Scenario(refine_before_start=True),
            )
            self.assertFalse(result["available"])
            self.assertEqual(result["reason"], "no measured refinement segments")
            self.assertNotIn("predicted", result)
            self.assertEqual(result["scenario"]["refine_before_start"], True)

            payload = simulation.simulate(
                calibration, simulation.Scenario(refine_before_start=True),
            )
            self.assertTrue(payload["validation"]["ok"])   # gate still passes
            self.assertIsNone(payload["scenario"])
            self.assertIsNone(payload["compare"])
            self.assertIs(payload["scenario_available"], False)
            self.assertEqual(
                payload["scenario_unavailable_reason"],
                "no measured refinement segments",
            )
            self.assertIn("not run", payload["note"])
            self.assertIsNone(payload["calibration"]["refinement"]["hours"])
        finally:
            con.close()

    def test_advice_best_scenario_excludes_the_unavailable_refinement(self):
        con, court, data, window = self.load()
        try:
            cost = jira.refinement_cost(court, data, window)
            calibration = simulation.calibrate(
                court, window, conn=con, refinement=cost,
            )
            comparisons: dict[str, dict | None] = {}
            for policy in advice.POLICIES:
                result = simulation.simulate(
                    calibration, simulation.Scenario.from_dict(policy["scenario"]),
                )
                comparison = result.get("compare")
                if comparison is None and result.get("scenario_available") is False:
                    comparison = {
                        "available": False,
                        "reason": result.get("scenario_unavailable_reason"),
                    }
                comparisons[policy["id"]] = comparison
            report = advice.report(comparisons)
            refine = next(
                entry for entry in report["policies"] if entry["id"] == "refine"
            )
            self.assertFalse(refine["available"])
            self.assertEqual(
                refine["unavailable_reason"], "no measured refinement segments",
            )
            self.assertIn("cannot model this policy", refine["note"])
            self.assertNotEqual((report["best"] or {}).get("id"), "refine")
            self.assertNotIn("refine", report["note"].lower())
            # the banner's own sentence never claims a refinement win
            self.assertNotIn("refine before start", report["note"])
        finally:
            con.close()

    def test_api_exposes_the_unavailable_state(self):
        con, court, data, window = self.load()
        saved = []
        try:
            class Reply:
                def __init__(self, status, body):
                    self.status = status
                    self._body = body

                def json(self):
                    return json.loads(self._body)

            async def call(path: str, **query: object):
                sent: list[dict] = []

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message):
                    sent.append(message)

                scope = {
                    "type": "http", "asgi": {"version": "3.0"},
                    "http_version": "1.1", "method": "GET", "path": path,
                    "query_string": urlencode(query).encode(), "root_path": "",
                    "headers": [(b"host", b"testserver")],
                    "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
                }
                await app_module.app(scope, receive, send)
                start = next(m for m in sent if m["type"] == "http.response.start")
                body = b"".join(
                    m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body"
                )
                return Reply(start["status"], body)

            patches = [
                mock.patch.object(app_module, "TENANTS_DIR", self.root),
                mock.patch.object(app_module, "DEFAULT_TENANT", ""),
                mock.patch.object(app_module, "LOG", False),
                mock.patch.object(
                    app_module, "CALENDAR_CONFIG", self.root / "no-calendar.yaml",
                ),
            ]
            for patch in patches:
                saved.append(patch)
                patch.start()
            app_module._analysis_cache = None
            app_module._sim_cache.clear()
            app_module._advice_cache.clear()

            scenario = json.dumps({"refine_before_start": True})
            reply = asyncio.run(call(
                "/api/simulate", tenant="pr-only", days=3650, scenario=scenario,
            ))
            self.assertEqual(reply.status, 200)
            body = reply.json()
            self.assertIsNone(body["calibration"]["refinement"]["hours"])
            self.assertIsNone(body["scenario"])
            self.assertIsNone(body["compare"])
            self.assertIs(body["scenario_available"], False)
            self.assertEqual(
                body["scenario_unavailable_reason"],
                "no measured refinement segments",
            )
            self.assertIn("no measured refinement segments", body["note"])

            # the Rules cross-check carries the same unavailable state, and its
            # banner does not name refinement.
            reply = asyncio.run(call("/api/insights", tenant="pr-only", days=3650))
            self.assertEqual(reply.status, 200)
            insights = reply.json()
            lab = insights["policy_lab"]
            self.assertEqual(
                lab["calibration_inputs"]["refinement"]["hours"], None,
            )
            self.assertFalse(
                lab["calibration_inputs"]["refinement"]["available"]
            )
            self.assertNotEqual((lab["best"] or {}).get("id"), "refine")
            self.assertNotIn("refine before start", lab["note"])
            refine = next(
                entry for entry in lab["policies"] if entry["id"] == "refine"
            )
            self.assertFalse(refine["available"])
            self.assertIn("no measured refinement segments", refine["note"])
        finally:
            for patch in reversed(saved):
                patch.stop()
            con.close()


# --------------------------------------------------------------------------- #
# 2. sprint close outcomes (F-06b)
# --------------------------------------------------------------------------- #
class SprintFixture:
    """One closed sprint with a by-end, a late and a still-open story."""

    def __init__(self):
        self.as_of = datetime(2026, 2, 20, 12, 0)
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self._count = 0

    def _object(self, object_type: str, object_id: str, **kwargs) -> None:
        record = {
            "object_type": object_type, "object_id": object_id, "repo": None,
            "pr_id": None, "ticket_keys": [], "actor": None, "state": None,
            "created_at": None, "closed_at": None, "head_commit": None,
            "message_class": None, "attributes": {},
        }
        record.update(kwargs)
        self.objects.append(record)

    def event(self, key: str, ts: datetime, activity: str,
              actor: str = "AaAa", objects: list[str] | None = None,
              **attributes) -> None:
        self._count += 1
        event_key = f"{key}:e{self._count}"
        self.events.append({
            "event_key": event_key, "ts": ts, "activity": activity,
            "actor": actor, "repo": None, "pr_id": None,
            "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(
                event_key.encode(), digest_size=8,
            ).hexdigest(),
            "as_of": self.as_of,
        })
        for object_id in objects or [f"STORY:{key}"]:
            self.relations.append(
                (event_key, object_id.split(":", 1)[0], object_id)
            )

    def sprint(self, sid: int, start: datetime, end: datetime) -> None:
        self._object(
            "SPRINT", f"SPRINT:DEL#{sid}", state="closed",
            created_at=start, closed_at=end,
            attributes={
                "goal_class": "feature", "end_date": end.strftime("%Y-%m-%d"),
            },
        )

    def story(self, key: str, created: datetime, points: int = 3,
              state: str = "open", closed: datetime | None = None) -> None:
        self._object(
            "STORY", f"STORY:{key}", ticket_keys=[key], actor="AaAa",
            state=state, created_at=created, closed_at=closed,
            attributes={
                "summary_class": "feature", "epic_key": "DEL-1", "points": points,
            },
        )
        self.event(key, created, "created")

    def added(self, key: str, ts: datetime, sid: int) -> None:
        self.event(
            key, ts, "sprint_added",
            objects=[f"STORY:{key}", f"SPRINT:DEL#{sid}"], sprint_id=sid,
        )

    def status(self, key: str, ts: datetime, to_status: str,
               from_status: str = "backlog") -> None:
        self.event(
            key, ts, "status_changed",
            **{"from_status": from_status, "to_status": to_status},
        )

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-prd64-g2-sprint", "source": "fixture",
                "as_of": self.as_of, "source_watermark": self.as_of,
                "classifier_version": "deterministic-v1",
                "prs_seen": 0, "prs_kept": 0, "prs_dropped": 0,
                "events_written": len(self.events), "events_skipped": 0,
                "issues_seen": 3, "issues_kept": 3, "issues_skipped": 0,
            })
        finally:
            con.close()
        return path


def sprint_fixture() -> SprintFixture:
    """S1 closes Jan 19 16:00: one done by then, one late, one still open."""
    fx = SprintFixture()
    close = datetime(2026, 1, 19, 16, 0)
    fx.sprint(1, T0, close)
    # by end: done Jan 15
    fx.story("ENG-1", T0, state="done", closed=datetime(2026, 1, 15, 17, 0))
    fx.added("ENG-1", T0 + timedelta(hours=2), 1)
    fx.status("ENG-1", T0 + timedelta(hours=5), "in_progress")
    fx.status("ENG-1", datetime(2026, 1, 15, 17, 0), "done",
              from_status="in_progress")
    # late: done Feb 1, after the close
    fx.story("ENG-2", T0, state="done", closed=datetime(2026, 2, 1, 16, 0))
    fx.added("ENG-2", T0 + timedelta(hours=3), 1)
    fx.status("ENG-2", T0 + timedelta(days=1), "in_progress")
    fx.status("ENG-2", datetime(2026, 2, 1, 16, 0), "done",
              from_status="in_progress")
    # still open at the close and at the snapshot
    fx.story("ENG-3", T0)
    fx.added("ENG-3", T0 + timedelta(hours=4), 1)
    fx.status("ENG-3", T0 + timedelta(days=2), "in_progress")
    return fx


class SprintOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sprint.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self):
        sprint_fixture().write(self.path)
        con = connect(self.path, read_only=True)
        court = ballcourt.load(con)
        data = jira.load(con, court)
        return con, court, data

    def test_still_open_counts_as_affected_and_keeps_its_label(self):
        con, court, data = self.load()
        try:
            metric = jira.sprint_delivery(court, data, (T0, court.snapshot))
            rows = {row["key"]: row for row in metric["rows"]}
            self.assertEqual(set(rows), {"ENG-2", "ENG-3"})
            # both misses are affected; the label says which kind of miss
            self.assertTrue(rows["ENG-2"]["affected"])
            self.assertTrue(rows["ENG-3"]["affected"])
            self.assertEqual(rows["ENG-2"]["status"], "late")
            self.assertEqual(rows["ENG-3"]["status"], "never")
            # the story delivered by the close has no drill row to affect
            self.assertNotIn("ENG-1", rows)
            self.assertEqual(metric["done_by_end"], 1)
            self.assertEqual(metric["done_late"], 1)
            self.assertEqual(metric["never_done"], 1)
        finally:
            con.close()

    def test_the_drill_marks_one_affected_row_per_miss(self):
        con, court, data = self.load()
        try:
            window = rules.window_for(court, None, data)
            metric = jira.sprint_delivery(court, data, window)
            affected = [row for row in metric["rows"] if row["affected"]]
            self.assertEqual(
                sorted(row["status"] for row in affected), ["late", "never"],
            )
            self.assertEqual(
                {row["key"] for row in affected}, {"ENG-2", "ENG-3"},
            )
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# 3. the del-combined evidence (only when the store is present)
# --------------------------------------------------------------------------- #
def _del_combined_path() -> Path | None:
    import os

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
class DelCombinedRefinementTests(unittest.TestCase):
    """The PRD's control: ``+82 % — not a measured win`` stays the verdict."""

    @classmethod
    def setUpClass(cls):
        from analysis import calendar as workcal

        cls.path = _del_combined_path()
        con = connect(cls.path, read_only=True)
        try:
            calendar = workcal.load(
                tenant=cls.path.stem, tenants_dir=cls.path.parent,
                config_path=(
                    Path(__file__).resolve().parent.parent
                    / "config" / "calendar.default.yaml"
                ),
            )
            cls.court = ballcourt.load(con, calendar=calendar)
            cls.data = jira.load(con, cls.court)
        finally:
            con.close()
        cls.window = rules.window_for(cls.court, None, cls.data)
        cls.cost = jira.refinement_cost(cls.court, cls.data, cls.window)
        con = connect(cls.path, read_only=True)
        try:
            cls.calibration = simulation.calibrate(
                cls.court, cls.window, conn=con, refinement=cls.cost,
            )
        finally:
            con.close()

    def test_del_combined_measures_a_refinement_segment(self):
        self.assertIsNotNone(self.cost["hours"])
        self.assertGreater(self.cost["n"], 0)
        self.assertIsNotNone(self.calibration["refinement"]["hours"])

    def test_the_refinement_scenario_is_still_not_a_measured_win(self):
        result = simulation.simulate(
            self.calibration,
            simulation.Scenario(refine_before_start=True),
        )
        self.assertTrue(result["validation"]["ok"])
        comparison = result["compare"]["cycle_median_hours"]
        self.assertFalse(comparison["better"])
        self.assertGreater(comparison["percent"], 0)
        entry = next(
            item for item in advice.report({
                policy["id"]: result["compare"]
                if policy["id"] == "refine" else None
                for policy in advice.POLICIES
            })["policies"]
            if item["id"] == "refine"
        )
        self.assertIn("not a measured win", entry["note"])


if __name__ == "__main__":
    unittest.main()

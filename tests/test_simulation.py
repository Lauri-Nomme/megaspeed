"""Policy lab (prd/23): calibration, the SimPy model, the validation gate.

Three things are pinned here, in order of importance:

1. **The gate.** The baseline must reproduce the store's own cycle-time median
   within the stated tolerance, and a deliberately mis-calibrated model must be
   rejected — otherwise the gate is decoration. When the baseline fails, no
   scenario number may be shown at all.
2. **The model is arithmetic, not vibes.** A synthetic store with a *known*
   arrival rate and *known* handover times has a hand-computable cycle time; the
   model must predict it (that is the "predicted median matches an analytic
   expectation" test).
3. **The knobs move the prediction in the direction the mechanism implies.** A
   WIP limit trades lead time for cycle time; an SLA shortens the cycle; a
   scenario with no stacks does nothing.

Everything is deterministic: fixed seed, no wall clock, no real store.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from analysis import ballcourt, simulation
from ingest.store import (
    connect, ensure_schema, write_events, write_objects, write_run,
)
from server import app as app_module

REPO = "SIM/lab"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday, no calendar attached
AUTHORS = ("AaAa", "BbBb", "CcCc", "DdDd")
SENTINEL = "SsSs"


# --------------------------------------------------------------------------- #
# a synthetic store with hand-known handover times
# --------------------------------------------------------------------------- #
class Fixture:
    """N PRs, evenly spaced, with fixed peer/approval/merge offsets in hours.

    Every PR has the same shape, so the cycle time is a constant that the test
    can compute by hand:

        open --peer_first--> comment --peer_second--> approval
             --gate--> sentinel approval --merge--> merged
    """

    def __init__(self, *, n_prs: int, days: float,
                 peer_first: float = 2.0, peer_second: float = 4.0,
                 gate: float = 6.0, merge: float = 7.0,
                 push_class: str = "content"):
        self.as_of = T0 + timedelta(days=days)
        self.events: list[dict] = []
        self.objects: list[dict] = []
        self.n_prs = n_prs
        self.spacing = days * 24.0 / max(1, n_prs)
        self.peer_first = peer_first
        self.peer_second = peer_second
        self.gate = gate
        self.merge = merge
        self.push_class = push_class
        self._count = 0
        for number in range(1, n_prs + 1):
            self._pr(number)

    @property
    def cycle_hours(self) -> float:
        return self.merge

    def _pr(self, number: int) -> None:
        opened = T0 + timedelta(hours=(number - 1) * self.spacing)
        author = AUTHORS[(number - 1) % len(AUTHORS)]
        peer = AUTHORS[number % len(AUTHORS)]
        self.objects.append({
            "object_type": "PR",
            "object_id": f"PR:{REPO}#{number}",
            "repo": REPO,
            "pr_id": number,
            "ticket_keys": [f"SIM-{number}"],
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
        self._event(number, "pr_pushed", author, 0.0, push_class=self.push_class,
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
                "run_id": "fixture-simulation",
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


class StoreCase(unittest.TestCase):
    """A temp store + the calibration it produces."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def calibrate(self, fixture: Fixture) -> dict:
        fixture.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            return simulation.calibrate(court, conn=con)
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# 1. calibration
# --------------------------------------------------------------------------- #
class CalibrationTests(StoreCase):
    def test_arrival_service_and_rework_come_from_the_store(self):
        fixture = Fixture(n_prs=80, days=20)
        calibration = self.calibrate(fixture)
        self.assertTrue(calibration["ok"])
        # 80 PRs over 20 days = 4 per day; without a calendar a day is 24 h
        self.assertAlmostEqual(calibration["arrival"]["per_business_day"], 4.0, places=2)
        self.assertAlmostEqual(calibration["arrival"]["mean_interarrival_hours"], 6.0, places=1)
        # every reviewer answers in 2 h (two peer handovers, then the gate); the
        # modelled *service* time is the calibrated share of that response
        self.assertAlmostEqual(calibration["review_service"]["pooled"]["mean"], 2.0, places=2)
        peer = calibration["review_service"]["by_reviewer"]
        self.assertEqual(sorted(peer), sorted(AUTHORS))
        for person, fit in peer.items():
            self.assertAlmostEqual(fit["mean"], 2.0 * simulation.SERVICE_SHARE,
                                   places=2, msg=person)
        observed = {row["person"]: row["review"]["observed"]["mean_hours"]
                    for row in calibration["reviewers"]}
        for person in AUTHORS:
            self.assertAlmostEqual(observed[person], 2.0, places=2, msg=person)
        self.assertAlmostEqual(calibration["sentinel"]["observed"]["mean_hours"], 2.0, places=2)
        self.assertAlmostEqual(calibration["merge_delay"]["mean"], 1.0, places=2)
        # no rework in this fixture, one content push per PR
        self.assertEqual(calibration["rework"]["probability"], 0.0)
        self.assertEqual(calibration["rework"]["push_class"]["content"], 1.0)
        self.assertEqual(calibration["pool"]["sentinels"], [SENTINEL])
        self.assertEqual(calibration["pr_size"]["commits"]["median"], 0.0)
        self.assertEqual(calibration["pr_size"]["lines"]["median"], 12.0)
        self.assertIn("calibrated from 80 PRs", calibration["provenance"])

    def test_the_components_add_up_to_the_measured_cycle(self):
        """The unmeasured residue closes the gap, so the parts equal the whole."""
        calibration = self.calibrate(Fixture(n_prs=80, days=20))
        self.assertAlmostEqual(calibration["unmeasured"]["mean_hours"], 0.0, places=2)
        self.assertAlmostEqual(
            calibration["historical"]["cycle_median_hours"], 7.0, places=2
        )

    def test_an_empty_store_is_unavailable_not_an_error(self):
        fixture = Fixture(n_prs=0, days=20)
        calibration = self.calibrate(fixture)
        self.assertFalse(calibration["ok"])
        self.assertFalse(calibration["available"])
        self.assertIn("nothing to calibrate", calibration["note"])
        # and the model refuses to run rather than inventing numbers
        result = simulation.run(calibration)
        self.assertFalse(result["ok"])


# --------------------------------------------------------------------------- #
# 2. the model against a hand-computed cycle time
# --------------------------------------------------------------------------- #
class ModelTests(StoreCase):
    def test_predicted_median_matches_the_hand_computed_cycle(self):
        """7 h is arithmetic: 2 h peer + 2 h peer + 2 h gate + 1 h merge."""
        fixture = Fixture(n_prs=80, days=20)
        calibration = self.calibrate(fixture)
        result = simulation.run(calibration, simulation.Scenario(), seed=7)
        self.assertTrue(result["ok"])
        median = result["predicted"]["cycle_hours"]["median"]
        self.assertAlmostEqual(median, fixture.cycle_hours, delta=0.7)
        # a light load must not turn the gate into a queue
        self.assertLess(result["bottleneck"]["utilisation"], 0.8)

    def test_the_model_is_deterministic_for_a_seed(self):
        calibration = self.calibrate(Fixture(n_prs=40, days=20))
        first = simulation.run(calibration, simulation.Scenario(), seed=11)
        second = simulation.run(calibration, simulation.Scenario(), seed=11)
        self.assertEqual(first["predicted"]["cycle_hours"], second["predicted"]["cycle_hours"])
        other = simulation.run(calibration, simulation.Scenario(), seed=12)
        self.assertNotEqual(first["predicted"]["cycle_hours"], other["predicted"]["cycle_hours"])

    def test_flow_efficiency_and_throughput_are_reported(self):
        calibration = self.calibrate(Fixture(n_prs=80, days=20))
        result = simulation.run(calibration, simulation.Scenario(), seed=3)
        efficiency = result["predicted"]["flow_efficiency"]
        self.assertGreater(efficiency["aggregate"], 0.0)
        self.assertLess(efficiency["aggregate"], 1.0)
        self.assertGreater(result["predicted"]["throughput_per_day"], 3.0)
        self.assertEqual(result["predicted"]["censored"], 0)
        # every person is a capacity-1 server, and the mandatory gate is a slot
        self.assertEqual(
            sorted(row["person"] for row in result["resources"]),
            sorted(list(AUTHORS) + [SENTINEL, "sentinel:1"]),
        )


# --------------------------------------------------------------------------- #
# 3. the validation gate
# --------------------------------------------------------------------------- #
class ValidationTests(StoreCase):
    def test_the_baseline_validates_on_a_store_it_was_fitted_to(self):
        calibration = self.calibrate(Fixture(n_prs=80, days=20))
        gate = simulation.validate(calibration, simulation.run(calibration, seed=7))
        self.assertTrue(gate["ok"], gate)
        self.assertEqual(gate["status"], "validated")
        self.assertLessEqual(abs(gate["percent_off"]), 20.0)

    def test_a_mis_calibrated_model_is_rejected(self):
        """Double every latency and the gate must refuse the model."""
        calibration = self.calibrate(Fixture(n_prs=80, days=20))
        bad = copy.deepcopy(calibration)
        for reviewer in bad["reviewers"]:
            for kind in ("review", "author"):
                for key in ("service", "attention"):
                    fit = reviewer[kind].get(key)
                    if fit and fit.get("mu") is not None:
                        fit["mu"] += math.log(2.0)
                        for field in ("mean", "median", "p90"):
                            fit[field] = round(fit[field] * 2.0, 2)
        for key in ("service", "attention"):
            fit = bad["sentinel"].get(key)
            if fit and fit.get("mu") is not None:
                fit["mu"] += math.log(2.0)
                for field in ("mean", "median", "p90"):
                    fit[field] = round(fit[field] * 2.0, 2)
        gate = simulation.validate(bad, simulation.run(bad, seed=7))
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["status"], "failed")
        self.assertGreater(gate["percent_off"], 20.0)

    def test_a_failed_baseline_withholds_the_scenario(self):
        calibration = self.calibrate(Fixture(n_prs=80, days=20))
        bad = copy.deepcopy(calibration)
        for reviewer in bad["reviewers"]:
            for kind in ("review", "author"):
                for key in ("service", "attention"):
                    fit = reviewer[kind].get(key)
                    if fit and fit.get("mu") is not None:
                        fit["mu"] += math.log(2.0)
                        for field in ("mean", "median", "p90"):
                            fit[field] = round(fit[field] * 2.0, 2)
        payload = simulation.simulate(bad, simulation.Scenario(sentinels=2), seed=7)
        self.assertFalse(payload["validation"]["ok"])
        self.assertIsNone(payload["scenario"])
        self.assertIsNone(payload["compare"])
        self.assertIn("baseline did not validate", payload["blocked"])

    def test_a_passing_gate_runs_the_scenario_and_reports_the_delta(self):
        calibration = self.calibrate(Fixture(n_prs=80, days=20))
        payload = simulation.simulate(
            calibration, simulation.Scenario(review_sla_hours=1.0), seed=7
        )
        self.assertTrue(payload["validation"]["ok"])
        self.assertIsNotNone(payload["scenario"])
        compare = payload["compare"]
        self.assertIsNotNone(compare["cycle_median_hours"]["percent"])
        self.assertIn(compare["cycle_median_hours"]["direction"], ("down", "up", "same"))
        self.assertIn("better", compare["cycle_median_hours"])
        self.assertIn("within_noise", compare)


# --------------------------------------------------------------------------- #
# 4. the policy knobs
# --------------------------------------------------------------------------- #
class ScenarioTests(StoreCase):
    def loaded(self) -> dict:
        """A store whose mandatory gate is genuinely loaded (ρ ≈ 0.75)."""
        return self.calibrate(Fixture(n_prs=320, days=20))

    def test_a_wip_limit_trades_lead_time_for_a_shorter_cycle(self):
        calibration = self.loaded()
        baseline = simulation.run(calibration, simulation.Scenario(), seed=5)
        limited = simulation.run(calibration, simulation.Scenario(wip_limit=1), seed=5)
        cycle = simulation.compare(baseline, limited)["cycle_median_hours"]
        self.assertLessEqual(cycle["percent"], 0.0, "a WIP limit must not lengthen the cycle")
        # the cost is visible, not hidden: work waits in the author's backlog
        self.assertGreater(
            limited["predicted"]["lead_hours"]["median"],
            baseline["predicted"]["lead_hours"]["median"] * 2,
        )
        self.assertGreater(limited["predicted"]["backlog_max"], 0)

    def test_a_review_sla_shortens_the_cycle(self):
        calibration = self.loaded()
        baseline = simulation.run(calibration, simulation.Scenario(), seed=5)
        sla = simulation.run(calibration, simulation.Scenario(review_sla_hours=1.0), seed=5)
        self.assertLess(
            simulation.compare(baseline, sla)["cycle_median_hours"]["percent"], 0.0
        )
        self.assertGreater(sla["queues"]["escalated"], 0)

    def test_more_sentinels_never_lengthen_the_gate(self):
        calibration = self.loaded()
        one = simulation.run(calibration, simulation.Scenario(sentinels=1), seed=5)
        two = simulation.run(calibration, simulation.Scenario(sentinels=2), seed=5)
        self.assertLessEqual(
            two["predicted"]["cycle_hours"]["median"],
            one["predicted"]["cycle_hours"]["median"],
        )
        gate = next(row for row in two["resources"] if row["person"].startswith("sentinel"))
        self.assertLess(gate["utilisation"], 0.75)

    def test_a_stack_cap_is_inert_without_ref_digests(self):
        calibration = self.loaded()
        self.assertFalse(calibration["stack_depth"]["available"])
        baseline = simulation.run(calibration, simulation.Scenario(), seed=5)
        capped = simulation.run(calibration, simulation.Scenario(stack_depth_cap=1), seed=5)
        self.assertEqual(
            baseline["predicted"]["cycle_hours"], capped["predicted"]["cycle_hours"]
        )

    def test_a_scenario_rejects_an_unknown_knob(self):
        with self.assertRaises(simulation.SimulationError):
            simulation.Scenario.from_dict({"wip": 2})
        with self.assertRaises(simulation.SimulationError):
            simulation.Scenario.from_dict({"sentinels": 0})
        with self.assertRaises(simulation.SimulationError):
            simulation.Scenario.from_dict({"review_first": "yes"})
        scenario = simulation.Scenario.from_dict({"sentinels": "2", "wip_limit": "1"})
        self.assertEqual(scenario.sentinels, 2)
        self.assertEqual(scenario.wip_limit, 1)
        self.assertTrue(simulation.Scenario.from_dict(None).is_baseline)


# --------------------------------------------------------------------------- #
# 5. the API
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


class SimulateApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        Fixture(n_prs=80, days=20).write(cls.dir / "del.duckdb")
        cls.env = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "LOG", False),
            # no calendar file: the fixture's handover times are wall-clock hours,
            # so the API must price them the same way the fixture does
            mock.patch.object(
                app_module, "CALENDAR_CONFIG", cls.dir / "no-calendar.yaml"
            ),
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
        app_module._sim_cache.clear()

    def test_baseline_payload_carries_the_gate_and_the_calibration(self):
        reply = get("/api/simulate", tenant="del", days=3650)
        self.assertEqual(reply.status, 200)
        body = reply.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["validation"]["ok"])
        self.assertEqual(body["validation"]["status"], "validated")
        self.assertIn("p90_check", body["validation"])
        self.assertEqual(body["calibration"]["prs"]["n"], 80)
        self.assertIn("calibrated from 80 PRs", body["provenance"])
        self.assertAlmostEqual(body["calibration"]["arrival"]["per_business_day"], 4.0, places=2)
        self.assertIsNone(body["scenario"])
        self.assertEqual(body["tenant"], "del")

    def test_a_scenario_returns_the_ab_compare(self):
        scenario = json.dumps({"review_sla_hours": 1.0})
        reply = get("/api/simulate", tenant="del", days=3650, scenario=scenario)
        self.assertEqual(reply.status, 200)
        body = reply.json()
        self.assertIsNotNone(body["scenario"])
        self.assertIsNotNone(body["compare"])
        self.assertEqual(body["params"]["scenario"]["review_sla_hours"], 1.0)
        self.assertIn("cycle_median_hours", body["compare"])

    def test_the_cache_answers_the_same_request_with_the_same_object(self):
        first = get("/api/simulate", tenant="del", days=3650).json()
        second = get("/api/simulate", tenant="del", days=3650).json()
        self.assertEqual(
            first["baseline"]["predicted"]["cycle_hours"],
            second["baseline"]["predicted"]["cycle_hours"],
        )

    def test_a_bad_scenario_is_a_400_that_lists_the_knobs(self):
        reply = get("/api/simulate", tenant="del", scenario=json.dumps({"wip": 2}))
        self.assertEqual(reply.status, 400)
        body = reply.json()
        self.assertIn("unknown scenario knob", body["error"])
        reply = get("/api/simulate", tenant="del", scenario="{not json")
        self.assertEqual(reply.status, 400)
        self.assertIn("not valid JSON", reply.json()["error"])

    def test_unknown_query_parameters_are_rejected(self):
        reply = get("/api/simulate", tenant="del", wip=2)
        self.assertEqual(reply.status, 400)
        self.assertIn("unknown query parameter", reply.json()["error"])

    def test_an_unknown_tenant_is_a_404(self):
        reply = get("/api/simulate", tenant="nope")
        self.assertEqual(reply.status, 404)


if __name__ == "__main__":
    unittest.main()

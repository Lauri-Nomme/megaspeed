"""prd/44 DES-2 — `build_status` has a producer, and CI reaches the model.

The closed vocabulary always contained ``build_status`` and prd/40 gave it a
``ci`` flag, but nothing emitted it, so CI wait was unrepresentable in both
corpora. This pins the mapping end to end:

* a commit build-status record (DC ``/rest/build-status/1.0``, or an inline
  ``BUILD_STATUS`` activity) becomes a ``build_status`` event with a closed
  ``result`` label and the synthetic ``CI`` actor;
* ``ballcourt`` turns the pair (started → finished) into a real ``ci``-phase
  interval;
* the blocking graph carries a ``CI`` node with the wait-hours, so the "CI is
  a hidden bottleneck" answer is no longer invisible.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis import ballcourt
from ingest.bitbucket import PullRequest
from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.pipeline import CI_ACTOR, KeyFilter, Reducer, Stats
from ingest.pseudonym import IdentityTable
from ingest.scrape import scrape
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from ingest.taxonomy import PR_ACTIVITY_MAP
from ingest.transport import FixtureTransport

T0 = 1_790_000_000_000                  # epoch ms (a Monday)
AS_OF = datetime(2026, 11, 15, 12, 0)
HASH = "a" * 40
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}


def _pr(*, build_statuses: list[dict] | None = None,
        activities: list[dict] | None = None) -> PullRequest:
    pull = PullRequest("ENG", "platform", {
        "id": 1,
        "state": "OPEN",
        "createdDate": T0,
        "fromRef": {"latestCommit": HASH, "displayId": "feature/PROJ-1-thing"},
        "author": {"user": ADA},
    }, commits=[{
        "id": HASH, "author": ADA, "authorTimestamp": T0, "message": "add the thing",
    }])
    pull.activities = [
        {"id": 1, "createdDate": T0, "action": "OPENED", "user": ADA},
        *(activities or []),
    ]
    pull.build_statuses = build_statuses or []
    return pull


class BuildStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform")],
        )
        self.reducer = Reducer(
            self.cfg,
            IdentityTable(self.cfg.identity_path),
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape,
                            self.cfg.ticket_projects),
            AS_OF,
        )
        self.stats = Stats()

    def tearDown(self):
        self.tmp.cleanup()

    def reduce(self, pr: PullRequest) -> list[dict]:
        self.reducer.reduce(pr, ["PROJ-1"], pr.activities, self.stats)
        return self.stats.events

    def test_the_activity_map_knows_the_build_status_action(self):
        self.assertEqual(PR_ACTIVITY_MAP["BUILD_STATUS"], "build_status")

    def test_a_build_status_stream_emits_closed_result_events(self):
        events = self.reduce(_pr(build_statuses=[
            {"state": "INPROGRESS", "dateAdded": T0 + 60 * 60_000, "commitId": HASH},
            {"state": "SUCCESSFUL", "dateAdded": T0 + 120 * 60_000, "commitId": HASH},
        ]))
        builds = [event for event in events if event["activity"] == "build_status"]
        self.assertEqual([event["attributes"]["result"] for event in builds],
                         ["in_progress", "success"])
        self.assertEqual({event["actor"] for event in builds}, {CI_ACTOR})
        self.assertEqual({event["attributes"]["actor_kind"] for event in builds}, {"bot"})
        # the COMMIT object is linked when (and only when) the store has it
        self.assertIn(f"COMMIT:{HASH}", {
            oid for key, _kind, oid in self.stats.relations
            if key.endswith(":build:1") or key.endswith(":build:2")
        })

    def test_an_inline_build_status_activity_is_mapped_too(self):
        events = self.reduce(_pr(activities=[{
            "id": 5, "createdDate": T0 + 30 * 60_000, "action": "BUILD_STATUS",
            "commit": {"id": HASH},
            "buildStatus": {"state": "FAILED"},
        }]))
        builds = [event for event in events if event["activity"] == "build_status"]
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["attributes"]["result"], "failed")

    def test_a_build_produces_a_ci_interval_and_a_ci_node(self):
        self.reduce(_pr(build_statuses=[
            {"state": "INPROGRESS", "dateAdded": T0 + 60 * 60_000},
            {"state": "SUCCESSFUL", "dateAdded": T0 + 120 * 60_000},
        ]))
        con = connect(self.cfg.db_path)
        try:
            ensure_schema(con)
            write_events(con, self.stats.events, self.stats.relations)
            write_objects(con, self.stats.objects)
            write_run(con, {
                "run_id": "fixture-build-status",
                "source": "fixture",
                "as_of": AS_OF,
                "source_watermark": AS_OF,
                "classifier_version": "deterministic-v1",
                "prs_seen": 1, "prs_kept": 1, "prs_dropped": 0,
                "events_written": len(self.stats.events), "events_skipped": 0,
            })
            court = ballcourt.load(con)
        finally:
            con.close()

        ci = [interval for interval in court.intervals if interval.phase == "ci"]
        self.assertEqual(len(ci), 1)
        self.assertTrue(ci[0].ci)
        self.assertAlmostEqual(ci[0].hours, 1.0)

        graph = ballcourt.blocking_graph(court, level="queue")
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertIn("CI", nodes)
        self.assertAlmostEqual(nodes["CI"]["hours"], 1.0)
        self.assertIn("CI", {edge["to"] for edge in graph["edges"]})


class ScrapeBuildStatusTests(unittest.TestCase):
    """The scrape reads DC's build-status resource, not only the activity map."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform")],
            ticket_projects=("PROJ",),
        )
        self.out = root / "export" / "bb.tgz"
        self.state_dir = root / "export" / "bb.state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_scrape_turns_a_build_status_record_into_an_event(self):
        bundle = {
            "schema": "megaspeed.fixture/1",
            "projects": [{"key": "ENG", "repos": [{
                "slug": "platform",
                "pull_requests": [{
                    "pull_request": {
                        "id": 1, "title": "PROJ-1 add the thing", "state": "OPEN",
                        "createdDate": T0,
                        "fromRef": {"latestCommit": HASH, "displayId": "feature/PROJ-1"},
                        "toRef": {"latestCommit": "b" * 40, "displayId": "main"},
                        "author": {"user": ADA},
                    },
                    "activities": [{
                        "id": 1, "createdDate": T0, "action": "OPENED", "user": ADA,
                    }],
                    "commits": [{
                        "id": HASH, "author": ADA, "authorTimestamp": T0,
                        "message": "add the thing",
                    }],
                }],
            }]}],
            "build_statuses": {HASH: [
                {"state": "INPROGRESS", "dateAdded": T0 + 30 * 60_000},
                {"state": "SUCCESSFUL", "dateAdded": T0 + 45 * 60_000},
            ]},
        }
        report = scrape(self.cfg, FixtureTransport(bundle), "fixture",
                        out=self.out, state_dir=self.state_dir)
        self.assertEqual(report["requests_by_kind"].get("build-status"), 1)
        builds = [event for event in read_bundle(self.out).events
                  if event["activity"] == "build_status"]
        self.assertEqual([event["attributes"]["result"] for event in builds],
                         ["in_progress", "success"])
        self.assertEqual({event["actor"] for event in builds}, {CI_ACTOR})


if __name__ == "__main__":
    unittest.main()
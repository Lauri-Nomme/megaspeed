"""Configurable workflow stages (prd/29): projection, config, lifecycle, UI.

The install's real workflow is seven statuses, not three categories:

    Story: OPEN → READY FOR REFINEMENT → REFINED → IN PROGRESS → IN REVIEW →
           IN VERIFICATION → CLOSED
    Bug:   NEW → TRIAGED → IN PROGRESS → IN REVIEW → IN VERIFICATION → RESOLVED

A 3-bucket projection folds review and QA into "in progress", so "time in QA"
was never measurable. prd/29 adds a closed **stage** vocabulary, a per-workflow
status map, and three status-driven lifecycle boundaries (``ready``,
``in_review``, ``in_verification``); the last segment splits into
``merge→verification`` + ``verification→done`` when the store has a QA stage.

The backward-compatibility half of this module is as important as the new
behaviour: a store scraped before prd/29 (``todo`` labels, no stage map) must
still produce today's eight segments, byte for byte.

    python3 -m unittest tests.test_jira_stages -v
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from analysis import ballcourt, jira, rules, tickets
from ingest import jira_fixtures, taxonomy
from ingest.bundle import read_bundle
from ingest.config import Config, load_config, parse_status_map
from ingest.progress import ProgressReporter
from ingest.scrape_jira import scrape
from ingest.store import (
    connect, ensure_schema, write_events, write_objects, write_run,
)
from ingest.transport import FixtureJiraTransport

REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday, 09:00 — wall-clock friendly
AS_OF = datetime(2026, 3, 1, 12, 0)
REGEX = r"\b[A-Z]+-\d+\b"
SHAPE = r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$"

#: ``[jira.stages]`` for the two workflows above (prd/29 §2).
STAGE_MAP = {
    "open": "backlog",
    "ready for refinement": "backlog",
    "refined": "ready",
    "in progress": "in_progress",
    "in review": "in_review",
    "in verification": "in_verification",
    "closed": "done",
    "new": "backlog",
    "triaged": "ready",
    "resolved": "done",
}


# --------------------------------------------------------------------------- #
# a synthetic store with the install's workflow
# --------------------------------------------------------------------------- #
class Fixture:
    """Objects + events written straight to a store (no raw payloads)."""

    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
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

    def story(self, key: str, created: datetime, *, closed: datetime | None = None,
              state: str = "done", assignee: str = "AaAa") -> None:
        self._object(
            "STORY", f"STORY:{key}", ticket_keys=[key], actor=assignee, state=state,
            created_at=created, closed_at=closed,
            attributes={"summary_class": "feature", "epic_key": "ENG-1", "points": 3},
        )
        self.event(key, created, "created", actor=assignee)

    def event(self, key: str, ts: datetime, activity: str, actor: str = "AaAa",
              objects: list[str] | None = None, **attributes) -> None:
        self._count += 1
        event_key = f"{key}:e{self._count}"
        self.events.append({
            "event_key": event_key,
            "ts": ts,
            "activity": activity,
            "actor": actor,
            "repo": None,
            "pr_id": None,
            "attributes": attributes,
            "classifier_version": taxonomy.CLASSIFIER_VERSION,
            "content_hash": hashlib.blake2b(event_key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        for object_id in objects or [f"STORY:{key}"]:
            self.relations.append((event_key, object_id.split(":", 1)[0], object_id))

    def status(self, key: str, ts: datetime, to_status: str,
               from_status: str) -> None:
        self.event(key, ts, "status_changed",
                   **{"from_status": from_status, "to_status": to_status})

    def pointed(self, key: str, ts: datetime, points: int) -> None:
        self.event(key, ts, "pointed", points=points)

    def pr(self, number: int, author: str, opened: datetime, merged: datetime,
           tickets: tuple[str, ...] = ("ENG-1",)) -> None:
        self.opened[number] = opened
        object_id = f"PR:{REPO}#{number}"
        self._object(
            "PR", object_id, repo=REPO, pr_id=number, ticket_keys=list(tickets),
            actor=author, state="merged", created_at=opened, closed_at=merged,
            head_commit="a" * 40,
        )
        for activity, actor_name, hours, attributes in (
            ("pr_opened", author, 0, {}),
            ("comment_added", "RrRr", 5, {"severity": "suggestion"}),
            ("pr_approved", "RrRr", 10, {}),
            ("pr_merged", "RrRr", 20, {}),
        ):
            self._count += 1
            event_key = f"{REPO}#{number}:e{self._count}"
            self.events.append({
                "event_key": event_key,
                "ts": opened + timedelta(hours=hours),
                "activity": activity,
                "actor": actor_name,
                "repo": REPO,
                "pr_id": number,
                "attributes": attributes,
                "classifier_version": taxonomy.CLASSIFIER_VERSION,
                "content_hash": hashlib.blake2b(
                    event_key.encode(), digest_size=8
                ).hexdigest(),
                "as_of": self.as_of,
            })
            self.relations.append((event_key, "PR", object_id))

    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-jira-stages",
                "source": "jira-fixture",
                "as_of": self.as_of,
                "source_watermark": self.as_of,
                "classifier_version": taxonomy.CLASSIFIER_VERSION,
                "prs_seen": len(self.opened),
                "prs_kept": len(self.opened),
                "prs_dropped": 0,
                "events_written": len(self.events),
                "events_skipped": 0,
                "issues_seen": sum(
                    1 for o in self.objects if o["object_type"] == "STORY"
                ),
                "issues_kept": sum(
                    1 for o in self.objects if o["object_type"] == "STORY"
                ),
                "issues_skipped": 0,
            })
        finally:
            con.close()
        return path


def workflow_fixture() -> Fixture:
    """One story through the install's workflow, merged at +50, closed at +90.

    created +0 · REFINED (+ready) +10 · IN PROGRESS +20 · PR open +30 ·
    first review +35 · IN REVIEW +45 · approval +40 · merge +50 ·
    IN VERIFICATION +60 · CLOSED +90
    """
    fx = Fixture()
    fx.story("ENG-1", T0, closed=T0 + timedelta(hours=90))
    fx.pointed("ENG-1", T0 + timedelta(hours=10), 3)
    fx.status("ENG-1", T0 + timedelta(hours=10), "ready", "backlog")
    fx.status("ENG-1", T0 + timedelta(hours=20), "in_progress", "ready")
    fx.pr(1, "AaAa", T0 + timedelta(hours=30), T0 + timedelta(hours=50))
    fx.status("ENG-1", T0 + timedelta(hours=45), "in_review", "in_progress")
    fx.status("ENG-1", T0 + timedelta(hours=60), "in_verification", "in_review")
    fx.status("ENG-1", T0 + timedelta(hours=90), "done", "in_verification")
    return fx


def legacy_fixture() -> Fixture:
    """The same story as a *pre-prd/29* store: ``todo`` and no stage map.

    The reopen at +5 writes a legacy ``todo`` *to_status* on purpose: it is what
    an old store holds, and the analysis must read it as ``backlog``.
    """
    fx = Fixture()
    fx.story("ENG-9", T0, closed=T0 + timedelta(hours=90))
    fx.status("ENG-9", T0 + timedelta(hours=5), "todo", "in_progress")
    fx.pointed("ENG-9", T0 + timedelta(hours=10), 3)
    fx.status("ENG-9", T0 + timedelta(hours=30), "in_progress", "todo")
    fx.pr(1, "AaAa", T0 + timedelta(hours=40), T0 + timedelta(hours=80),
          tickets=("ENG-9",))
    fx.status("ENG-9", T0 + timedelta(hours=90), "done", "in_progress")
    return fx


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fixture: Fixture):
        fixture.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            data = jira.load(con, court)
            window = rules.window_for(court, None, data)
            return con, court, data, window
        finally:
            con.close()

    def metric(self, fixture: Fixture, name: str):
        _con, court, data, window = self.load(fixture)
        return rules.metrics(court, [], window, {}, data)[name]


# --------------------------------------------------------------------------- #
# 1. config: the status map (TOML + env)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _env(**overrides: str):
    clean = {
        key: value for key, value in os.environ.items()
        if not key.startswith("MEGASPEED_")
    }
    clean.update(overrides)
    with mock.patch.dict(os.environ, clean, clear=True):
        yield


class StatusMapConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.missing = self.root / "no-config.toml"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, **env: str) -> Config:
        with _env(MEGASPEED_CONFIG=str(self.missing), MEGASPEED_REPOS="DEL/huge",
                  **env):
            return load_config()

    def test_no_map_is_the_default(self):
        self.assertEqual(self.load().jira_status_map, {})

    def test_the_toml_table_carries_the_map(self):
        path = self.root / "config.toml"
        path.write_text(
            "[jira.stages]\n"
            'open = "backlog"\n'
            '"ready for refinement" = "backlog"\n'
            'refined = "ready"\n'
            '"in review" = "in_review"\n'
            '"in verification" = "in_verification"\n'
            'closed = "done"\n'
        )
        with _env(MEGASPEED_CONFIG=str(path), MEGASPEED_REPOS="DEL/huge"):
            cfg = load_config()
        self.assertEqual(cfg.jira_status_map, {
            "open": "backlog",
            "ready for refinement": "backlog",
            "refined": "ready",
            "in review": "in_review",
            "in verification": "in_verification",
            "closed": "done",
        })

    def test_the_env_string_is_normalised(self):
        cfg = self.load(
            MEGASPEED_JIRA_STAGES="In Review=in_review, in verification = in_verification ,"
        )
        self.assertEqual(cfg.jira_status_map, {
            "in review": "in_review",
            "in verification": "in_verification",
        })

    def test_env_merges_over_toml_per_name(self):
        path = self.root / "config.toml"
        path.write_text(
            "[jira.stages]\n"
            '"in review" = "in_review"\n'
            '"in verification" = "in_verification"\n'
        )
        with _env(MEGASPEED_CONFIG=str(path), MEGASPEED_REPOS="DEL/huge",
                  MEGASPEED_JIRA_STAGES="in review=in_progress"):
            cfg = load_config()
        self.assertEqual(cfg.jira_status_map["in review"], "in_progress")
        self.assertEqual(cfg.jira_status_map["in verification"], "in_verification")

    def test_a_stage_outside_the_closed_set_is_an_error(self):
        # never a silent ``other``: a typo would fold the QA wait back into
        # "in progress", the exact measurement this config exists to make.
        with self.assertRaises(SystemExit) as caught:
            self.load(MEGASPEED_JIRA_STAGES="in review=reviewing")
        self.assertIn("reviewing", str(caught.exception.code))
        self.assertIn("MEGASPEED_JIRA_STAGES", str(caught.exception.code))

    def test_a_toml_stage_outside_the_closed_set_is_an_error(self):
        path = self.root / "config.toml"
        path.write_text('[jira.stages]\n"in review" = "reviews"\n')
        with self.assertRaises(SystemExit) as caught:
            with _env(MEGASPEED_CONFIG=str(path), MEGASPEED_REPOS="DEL/huge"):
                load_config()
        self.assertIn("reviews", str(caught.exception.code))

    def test_a_malformed_env_entry_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            self.load(MEGASPEED_JIRA_STAGES="in review")
        self.assertIn("NAME=STAGE", str(caught.exception.code))

    def test_the_parse_helper_accepts_both_shapes(self):
        self.assertEqual(parse_status_map({"in review": "in_review"}),
                         {"in review": "in_review"})
        self.assertEqual(parse_status_map("in review=in_review"),
                         {"in review": "in_review"})
        self.assertEqual(parse_status_map(""), {})
        self.assertEqual(parse_status_map(None), {})
        # a legacy ``todo`` is not a stage: the map is a stage map
        with self.assertRaises(SystemExit):
            parse_status_map("to do=todo")


# --------------------------------------------------------------------------- #
# 2. the lifecycle with the workflow configured
# --------------------------------------------------------------------------- #
class WorkflowLifecycleTests(StoreCase):
    def test_the_stage_boundaries_are_distinct_and_ordered(self):
        con, _court, data, _window = self.load(workflow_fixture())
        try:
            story = jira.story_for(data, "ENG-1")
            cycle = jira.cycle_for(story)
        finally:
            con.close()
        bounds = cycle.boundaries
        self.assertEqual(bounds["ready"], T0 + timedelta(hours=10))
        self.assertEqual(bounds["refined"], T0 + timedelta(hours=10))
        self.assertEqual(bounds["in_review"], T0 + timedelta(hours=45))
        self.assertEqual(bounds["in_verification"], T0 + timedelta(hours=60))
        self.assertLess(bounds["started"], bounds["in_review"])
        self.assertLess(bounds["in_review"], bounds["in_verification"])
        self.assertLess(bounds["in_verification"], bounds["done"])
        self.assertTrue(jira.uses_verification(data))

    def test_the_qa_segment_is_measured_separately(self):
        metric = self.metric(workflow_fixture(), "waiting_time_decomposition")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["stories_decomposed"], 1)
        # the eight segments stay, with the last one split in two
        self.assertEqual(metric["order"], [
            "ready_refined", "refined_sprint", "sprint_in_progress",
            "in_progress_pr_open", "pr_open_first_review", "review_rounds",
            "final_approval_merge", "merge_verification", "verification_done",
        ])
        self.assertEqual(
            {key: entry["hours"] for key, entry in metric["segments"].items()},
            {
                "ready_refined": 10.0,          # created +0 → refined +10
                "refined_sprint": 0.0,          # no sprint membership
                "sprint_in_progress": 0.0,
                "in_progress_pr_open": 10.0,    # started +20 → PR open +30
                "pr_open_first_review": 5.0,    # +30 → first review +35
                "review_rounds": 5.0,           # +35 → approval +40
                "final_approval_merge": 10.0,   # +40 → merge +50
                "merge_verification": 10.0,     # merge +50 → QA +60
                "verification_done": 30.0,      # QA +60 → closed +90
            },
        )
        qa = metric["segments"]["verification_done"]
        self.assertEqual(qa["n"], 1)
        self.assertEqual(qa["share"], round(30 / 80, 4))
        self.assertEqual(qa["bounds"], ["in_verification", "done"])
        self.assertEqual(metric["largest"], "verification_done")
        # the QA boundary exists, so it is not reported missing
        self.assertNotIn("in_verification", metric["missing_boundaries"])
        self.assertNotIn("in_review", metric["missing_boundaries"])
        # the segment still has its own advice (prd/17: not "wait less")
        for key in ("merge_verification", "verification_done"):
            self.assertIn(key, rules.SEGMENT_ADVICE)

    def test_the_ticket_detail_renders_the_verification_segment(self):
        con, court, data, _window = self.load(workflow_fixture())
        try:
            payload = tickets.ticket(court, data, "ENG-1")
        finally:
            con.close()
        rows = {row["key"]: row for row in payload["segments"]["rows"]}
        self.assertEqual(len(rows), 9)
        self.assertNotIn("merge_done", rows)
        self.assertTrue(rows["verification_done"]["available"])
        self.assertEqual(rows["verification_done"]["hours"], 30.0)
        self.assertEqual(rows["verification_done"]["label"], "verification → done")
        self.assertTrue(payload["segments"]["boundaries"]["in_verification"]["available"])
        self.assertEqual(
            payload["segments"]["boundaries"]["in_verification"]["ts"],
            "2026-01-07T21:00:00Z",
        )

    def test_a_ticket_that_never_reached_qa_names_the_missing_boundary(self):
        """A configured store, a story that closed before verification."""
        fx = Fixture()
        fx.story("ENG-2", T0, closed=T0 + timedelta(hours=60))
        fx.pointed("ENG-2", T0 + timedelta(hours=10), 3)
        fx.status("ENG-2", T0 + timedelta(hours=10), "ready", "backlog")
        fx.status("ENG-2", T0 + timedelta(hours=20), "in_progress", "ready")
        fx.status("ENG-2", T0 + timedelta(hours=30), "in_review", "in_progress")
        # and a sibling that *does* reach QA, so the store's workflow has it
        fx.story("ENG-3", T0, closed=T0 + timedelta(hours=90))
        fx.status("ENG-3", T0 + timedelta(hours=20), "in_progress", "backlog")
        fx.status("ENG-3", T0 + timedelta(hours=60), "in_verification", "in_progress")
        fx.status("ENG-3", T0 + timedelta(hours=90), "done", "in_verification")
        con, court, data, _window = self.load(fx)
        try:
            payload = tickets.ticket(court, data, "ENG-2")
        finally:
            con.close()
        rows = {row["key"]: row for row in payload["segments"]["rows"]}
        self.assertIn("verification_done", rows)
        self.assertFalse(rows["verification_done"]["available"])
        self.assertEqual(rows["verification_done"]["missing"], ["in_verification"])
        self.assertIn("no status change to in verification",
                      rows["verification_done"]["reason"])

    def test_in_review_without_a_pr_is_reported(self):
        fx = Fixture()
        fx.story("ENG-4", T0, closed=T0 + timedelta(hours=60))
        fx.pointed("ENG-4", T0 + timedelta(hours=10), 3)
        fx.status("ENG-4", T0 + timedelta(hours=20), "in_progress", "backlog")
        fx.status("ENG-4", T0 + timedelta(hours=30), "in_review", "in_progress")
        fx.status("ENG-4", T0 + timedelta(hours=60), "done", "in_review")
        con, court, data, window = self.load(fx)
        try:
            metric = jira.conformance(court, data, window)
        finally:
            con.close()
        self.assertEqual(metric["in_review_without_pr"], 1)
        self.assertEqual(metric["in_review_examples"], ["ENG-4"])
        row = next(r for r in metric["rows"] if r["key"] == "ENG-4")
        self.assertIn("in_review_without_pr", row["violations"])


# --------------------------------------------------------------------------- #
# 3. backward compatibility: no map, no QA stage, today's numbers
# --------------------------------------------------------------------------- #
class BackwardCompatTests(StoreCase):
    def test_a_legacy_todo_store_reads_as_backlog(self):
        con, _court, data, _window = self.load(legacy_fixture())
        try:
            story = jira.story_for(data, "ENG-9")
            # the store holds ``todo``; the stage read is ``backlog``
            to_statuses = {
                jira.attributes(event)["to_status"]
                for event in story.of("status_changed")
            }
            self.assertIn("todo", to_statuses)
            self.assertEqual(
                jira._first_status(story, "backlog"), T0 + timedelta(hours=5)
            )
            self.assertEqual(
                jira._first_status(story, "in_progress"), T0 + timedelta(hours=30)
            )
        finally:
            con.close()

    def test_an_unconfigured_store_keeps_the_eight_segments(self):
        metric = self.metric(legacy_fixture(), "waiting_time_decomposition")
        self.assertTrue(metric["available"])
        self.assertEqual(metric["order"], list(jira.SEGMENT_KEYS))
        self.assertNotIn("merge_verification", metric["segments"])
        self.assertNotIn("verification_done", metric["segments"])
        # exactly today's numbers: eight segments, merge→done at the end
        self.assertEqual(
            {key: entry["hours"] for key, entry in metric["segments"].items()},
            {
                "ready_refined": 10.0,          # created +0 → refined +10
                "refined_sprint": 0.0,          # no sprint membership
                "sprint_in_progress": 0.0,
                "in_progress_pr_open": 10.0,    # started +30 → PR open +40
                "pr_open_first_review": 5.0,    # +40 → first review +45
                "review_rounds": 5.0,           # +45 → approval +50
                "final_approval_merge": 30.0,   # approval +50 → merge +80
                "merge_done": 10.0,             # merge +80 → done +90
            },
        )
        self.assertEqual(metric["largest"], "final_approval_merge")
        # review/QA stages do not exist without a map, and the payload says so
        # (the fixture also has no sprint membership, hence ``sprint``).
        self.assertEqual(
            metric["missing_boundaries"],
            {"in_review": 1, "in_verification": 1, "sprint": 1},
        )

    def test_the_unconfigured_ticket_view_has_no_qa_row(self):
        con, court, data, _window = self.load(legacy_fixture())
        try:
            payload = tickets.ticket(court, data, "ENG-9")
        finally:
            con.close()
        keys = [row["key"] for row in payload["segments"]["rows"]]
        self.assertEqual(keys, list(jira.SEGMENT_KEYS))
        self.assertIn("in_verification", payload["segments"]["boundaries"])
        self.assertFalse(
            payload["segments"]["boundaries"]["in_verification"]["available"]
        )
        self.assertIn("no status change to in verification",
                      payload["segments"]["boundaries"]["in_verification"]["reason"])


# --------------------------------------------------------------------------- #
# 4. the projection is reproducible: the map rides in the manifest/report
# --------------------------------------------------------------------------- #
class ManifestEchoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_scrape_manifest_echoes_the_stage_map(self):
        # The fixture's statuses are "To Do" / "In Progress" / "Done"; mapping
        # "to do" onto ``ready`` is not what an operator would configure, but it
        # proves the map reaches the reducer (the labels below would otherwise
        # be the category projection, ``backlog``).
        stage_map = {**STAGE_MAP, "to do": "ready"}
        cfg = Config(
            db_path=self.root / "store.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=REGEX,
            key_shape=SHAPE,
            repos=[],
            ticket_projects=("PROJ",),
            jira_projects=("PROJ",),
            jira_status_map=stage_map,
        )
        out = self.root / "jira.tgz"
        report = scrape(
            cfg,
            FixtureJiraTransport(jira_fixtures.generate()),
            "jira-fixture",
            out=out,
            state_dir=self.root / "jira.state",
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
        )
        self.assertEqual(report["jira_status_map"], dict(sorted(stage_map.items())))
        bundle = read_bundle(out)
        self.assertEqual(
            bundle.manifest["jira"]["status_map"], dict(sorted(stage_map.items()))
        )
        # every status_changed row in the bundle is a stage label, and the map
        # was applied (To Do -> ready, not the category's backlog)
        statuses = [
            event["attributes"] for event in bundle.events
            if event["activity"] == "status_changed"
        ]
        self.assertTrue(statuses)
        for attributes in statuses:
            for name in ("from_status", "to_status"):
                self.assertIn(attributes[name], taxonomy.STATUS_STAGE)
            taxonomy.check_attributes(attributes)
        self.assertIn("ready", {a["from_status"] for a in statuses})
        self.assertNotIn("backlog", {a["from_status"] for a in statuses})

    def test_an_unconfigured_scrape_echoes_an_empty_map(self):
        cfg = Config(
            db_path=self.root / "store.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=REGEX,
            key_shape=SHAPE,
            repos=[],
            ticket_projects=("PROJ",),
            jira_projects=("PROJ",),
        )
        out = self.root / "jira.tgz"
        report = scrape(
            cfg,
            FixtureJiraTransport(jira_fixtures.generate()),
            "jira-fixture",
            out=out,
            state_dir=self.root / "jira.state",
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
        )
        self.assertEqual(report["jira_status_map"], {})
        bundle = read_bundle(out)
        self.assertEqual(bundle.manifest["jira"]["status_map"], {})
        # no map => the category projection: "To Do" is backlog
        statuses = [
            event["attributes"] for event in bundle.events
            if event["activity"] == "status_changed"
        ]
        self.assertIn("backlog", {a["from_status"] for a in statuses})


if __name__ == "__main__":
    unittest.main()

"""The DEL Jira corpus on the install's *actual* workflow (prd/29).

``ingest/jira_fixtures.generate_del`` is the deterministic replacement for a
live scrape of a jira-lab that only has Atlassian's default workflow: the same
100 DEL tickets, backdated onto their Bitbucket PRs, but moving through the
installation's real Story (OPEN → … → CLOSED) and Bug (NEW → … → RESOLVED)
workflows. These tests pin the three things the rebuild depends on:

1. the fixture really emits both workflows (and the rework / reopen
   back-transitions), with every status inside the configured stage map;
2. the *scrape* projects those names to stages, so ``in_review`` and
   ``in_verification`` survive instead of collapsing into ``in_progress``;
3. a combined store built from a Bitbucket PR half + this Jira half has the
   ``in_review`` / ``in_verification`` boundaries and a measurable
   ``verification → done`` (QA) segment -- under one identity map, so the Jira
   assignee and the git author are the same pseudonym.

    python3 -m unittest tests.test_jira_del_workflow -v
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from analysis import ballcourt, jira, tickets
from ingest import jira_fixtures, taxonomy
from ingest.bundle import read_bundle
from ingest.config import Config, load_config
from ingest.importer import import_bundle
from ingest.jira_pipeline import status_stage
from ingest.progress import ProgressReporter
from ingest.pseudonym import IdentityTable
from ingest.scrape_jira import scrape
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run
from ingest.transport import FixtureJiraTransport

REPO = Path(__file__).resolve().parent.parent
REGEX = r"\b[A-Z]+-\d+\b"
SHAPE = r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$"
#: The two halves of the workflow, as the install defines them.
STORY_STAGES = ("backlog", "ready", "in_progress", "in_review",
                "in_verification", "done")
BUG_STAGES = ("backlog", "ready", "in_progress", "in_review",
              "in_verification", "done")


def _del_timeline() -> dict[str, dict]:
    """A deterministic PR timeline for the one ticket the store test uses."""
    return {
        "DEL-100": {
            "open": datetime(2026, 8, 17, 10, 0),
            "merged": datetime(2026, 8, 20, 12, 0),
            "prs": 1,
            "lines": 120,
        }
    }


def _config(root: Path, *, stage_map: dict[str, str] | None = None) -> Config:
    return Config(
        db_path=root / "store.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=REGEX,
        key_shape=SHAPE,
        repos=[],
        ticket_projects=("DEL",),
        jira_projects=("DEL",),
        jira_status_map=dict(stage_map or {}),
        jira_min_interval=0.0,
    )


def _scrape(root: Path, bundle: dict, *, stage_map: dict[str, str] | None = None,
            identity: Path | None = None) -> Path:
    cfg = _config(root, stage_map=stage_map)
    out = root / "jira.tgz"
    scrape(
        cfg,
        FixtureJiraTransport(bundle),
        "jira-fixture",
        out=out,
        state_dir=root / "state",
        full=True,
        identity_path=identity or (root / "identity.json"),
        progress=ProgressReporter(stream=io.StringIO(), interval=0),
    )
    return out


def _status_changes(bundle_path: Path) -> list[dict]:
    bundle = read_bundle(bundle_path)
    return [
        event["attributes"] for event in bundle.events
        if event["activity"] == "status_changed"
    ]


class DelWorkflowFixtureTests(unittest.TestCase):
    """The generator mirrors jira-lab/seed.py's data on the real workflows."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = jira_fixtures.generate_del()
        cls.issues = [i for i in cls.bundle["issues"] if i["key"] != "DEL-1"]
        cls.epic = next(i for i in cls.bundle["issues"] if i["key"] == "DEL-1")

    def test_the_corpus_mirrors_the_lab(self):
        self.assertEqual(len(self.issues), 100)
        self.assertEqual({i["key"] for i in self.issues},
                         {f"DEL-{n}" for n in range(100, 200)})
        self.assertEqual(len(self.bundle["sprints"]), 3)
        self.assertEqual(self.epic["fields"]["summary"].startswith(
            "Delivery platform epic"), True)
        self.assertEqual(
            sum(len(i["fields"]["worklog"]["worklogs"]) for i in self.issues), 199
        )
        # the same 5 engineers own the work; admin creates every issue
        assignees = {i["fields"]["assignee"]["name"] for i in self.issues}
        self.assertEqual(assignees, {"alice", "bob", "carol", "dave", "erin"})
        self.assertEqual({i["fields"]["reporter"]["name"] for i in self.issues},
                         {"admin"})

    def test_both_workflows_are_exercised(self):
        types = {i["fields"]["issuetype"]["name"] for i in self.issues}
        self.assertEqual(types, {"Story", "Bug"})
        self.assertEqual(self.bundle["workflow"]["story"],
                         list(jira_fixtures.STORY_WORKFLOW))
        self.assertEqual(self.bundle["workflow"]["bug"],
                         list(jira_fixtures.BUG_WORKFLOW))

        statuses = set()
        transitions = set()
        for issue in self.issues:
            previous = None
            for history in issue["changelog"]["histories"]:
                for item in history["items"]:
                    if item["field"] != "status":
                        continue
                    statuses.update((item["fromString"], item["toString"]))
                    transitions.add((item["fromString"], item["toString"]))
                    if previous is not None:
                        self.assertEqual(item["fromString"], previous)
                    previous = item["toString"]
            self.assertEqual(previous, issue["fields"]["status"]["name"])
        self.assertTrue(set(jira_fixtures.STORY_WORKFLOW) <= statuses)
        self.assertTrue(set(jira_fixtures.BUG_WORKFLOW) <= statuses)
        # the back-transitions the install allows are really in the data
        self.assertIn(("IN REVIEW", "IN PROGRESS"), transitions)
        self.assertIn(("IN VERIFICATION", "IN PROGRESS"), transitions)
        self.assertIn(("RESOLVED", "TRIAGED"), transitions)

    def test_every_issue_is_backdated_inside_the_window(self):
        for issue in self.issues:
            fields = issue["fields"]
            created = datetime.fromisoformat(
                fields["created"].replace("+0000", "+00:00"))
            updated = datetime.fromisoformat(
                fields["updated"].replace("+0000", "+00:00"))
            self.assertGreaterEqual(created, datetime(2026, 8, 3, tzinfo=created.tzinfo))
            self.assertLessEqual(updated, jira_fixtures.DEL_HORIZON)
            self.assertGreaterEqual(updated, created)
            # the estimate is recorded after the status moved to ready
            pointed = [h for h in issue["changelog"]["histories"]
                       if any(i["field"] == "Story Points" for i in h["items"])]
            self.assertEqual(len(pointed), 1)

    def test_every_human_field_is_planted_with_a_canary(self):
        for issue in self.issues:
            self.assertIn("zzcanary", issue["fields"]["summary"])
            self.assertIn("zzcanary", issue["fields"]["description"])
            for worklog in issue["fields"]["worklog"]["worklogs"]:
                self.assertIn("zzcanary", worklog["comment"])


class DelWorkflowProjectionTests(unittest.TestCase):
    """The status map turns the workflow names into stages (prd/29)."""

    def test_the_stage_map_covers_both_workflows(self):
        categories = {
            status["id"]: status["statusCategory"]["key"]
            for status in jira_fixtures.WORKFLOW_STATUSES
        }
        for name, stage in jira_fixtures.DEL_STAGE_MAP.items():
            status = next(s for s in jira_fixtures.WORKFLOW_STATUSES
                          if s["name"].lower() == name)
            self.assertEqual(
                status_stage(status["id"], status["name"], categories,
                             jira_fixtures.DEL_STAGE_MAP),
                stage,
            )
        # ... and without the map, review and QA are the coarse category
        self.assertEqual(
            status_stage("20004", "IN REVIEW", categories, {}), "in_progress")
        self.assertEqual(
            status_stage("20005", "IN VERIFICATION", categories, {}), "in_progress")

    def test_the_committed_config_matches_the_fixture(self):
        raw = tomllib.loads((REPO / "ingest" / "config.toml").read_text())
        self.assertEqual(raw["jira"]["stages"], jira_fixtures.WORKFLOW_STAGE_MAP)
        with mock.patch.dict(os.environ, {
            key: value for key, value in os.environ.items()
            if not key.startswith("MEGASPEED_")
        } | {"MEGASPEED_CONFIG": str(REPO / "ingest" / "config.toml"),
             "MEGASPEED_REPOS": "DEL/delivery-api"}, clear=True):
            cfg = load_config()
        self.assertEqual(cfg.jira_status_map, jira_fixtures.WORKFLOW_STAGE_MAP)

    def test_the_scrape_emits_review_and_qa_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _scrape(root, jira_fixtures.generate_del(),
                           stage_map=jira_fixtures.DEL_STAGE_MAP)
            statuses = _status_changes(path)
            bundle = read_bundle(path)
        self.assertEqual(bundle.manifest["jira"]["status_map"],
                         dict(sorted(jira_fixtures.DEL_STAGE_MAP.items())))
        to_status = {attributes["to_status"] for attributes in statuses}
        # backlog is the creation status: it only appears as a from_status
        self.assertTrue(set(STORY_STAGES[1:]) <= to_status)
        self.assertIn("in_review", to_status)
        self.assertIn("in_verification", to_status)
        for attributes in statuses:
            for name in ("from_status", "to_status"):
                self.assertIn(attributes[name], taxonomy.STATUS_STAGE)
            self.assertNotEqual(attributes["from_status"], attributes["to_status"])
            taxonomy.check_attributes(attributes)

    def test_the_scrape_keeps_one_identity_per_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The Bitbucket scrape already saw alice; the Jira scrape must link
            # to that entry instead of minting a second pseudonym.
            table = IdentityTable(root / "identity.json")
            bitbucket_alice = table.pseudonym({
                "name": "alice",
                "displayName": "Alice Nguyen",
                "emailAddress": "alice.nguyen@delivery.example",
            })
            table.save()
            path = _scrape(root, jira_fixtures.generate_del(),
                           stage_map=jira_fixtures.DEL_STAGE_MAP)
            bundle = read_bundle(path)
            table = IdentityTable(root / "identity.json")
        self.assertEqual(bitbucket_alice, "AlNg")
        self.assertEqual(table.entries["jira:alice"]["pseudonym"], bitbucket_alice)
        self.assertEqual(table.entries["jira:alice"]["linked_to"], "bb:alice")
        jira_actors = {event["actor"] for event in bundle.events
                       if event["event_key"].startswith("DEL-")}
        self.assertIn(bitbucket_alice, jira_actors)
        self.assertNotIn("jira:alice", jira_actors)  # a raw identity never crosses


class CombinedStoreQaTests(unittest.TestCase):
    """Bitbucket PR half + the workflow Jira half, in one store."""

    def _write_bitbucket_half(self, store: Path, opened: datetime,
                              merged: datetime) -> None:
        """The rows the Bitbucket importer writes for one merged PR."""
        object_id = "PR:DEL/delivery-api#1"
        events = []
        for index, (activity, when, actor, attributes) in enumerate((
            ("pr_opened", opened, "AlNg", {}),
            ("comment_added", opened + timedelta(hours=3), "BoFe",
             {"severity": "suggestion"}),
            ("pr_approved", opened + timedelta(hours=5), "BoFe", {}),
            ("pr_merged", merged, "AlNg", {}),
        ), start=1):
            key = f"DEL/delivery-api#1:act:{index}"
            events.append({
                "event_key": key,
                "ts": when,
                "activity": activity,
                "actor": actor,
                "repo": "DEL/delivery-api",
                "pr_id": 1,
                "attributes": attributes,
                "classifier_version": taxonomy.CLASSIFIER_VERSION,
                "content_hash": hashlib.blake2b(key.encode(), digest_size=8).hexdigest(),
                "as_of": merged + timedelta(days=1),
            })
        con = connect(store)
        try:
            ensure_schema(con)
            write_objects(con, [{
                "object_type": "PR",
                "object_id": object_id,
                "repo": "DEL/delivery-api",
                "pr_id": 1,
                "ticket_keys": ["DEL-100"],
                "actor": "AlNg",
                "state": "merged",
                "created_at": opened,
                "closed_at": merged,
                "head_commit": "a" * 40,
                "message_class": "feature",
                "attributes": {"lines_added": 100, "lines_removed": 20},
            }])
            write_events(con, events,
                         [(event["event_key"], "PR", object_id) for event in events])
            write_run(con, {
                "run_id": "bitbucket-dc-test",
                "source": "bitbucket-dc",
                "as_of": merged + timedelta(days=1),
                "source_watermark": merged,
                "classifier_version": taxonomy.CLASSIFIER_VERSION,
                "prs_seen": 1,
                "prs_kept": 1,
                "prs_dropped": 0,
                "events_written": len(events),
                "events_skipped": 0,
                "issues_seen": 0,
                "issues_kept": 0,
                "issues_skipped": 0,
            })
        finally:
            con.close()

    def test_the_combined_store_has_a_measurable_qa_segment(self):
        timeline = _del_timeline()
        opened = timeline["DEL-100"]["open"]
        merged = timeline["DEL-100"]["merged"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "combined.duckdb"
            self._write_bitbucket_half(store, opened, merged)

            path = _scrape(root, jira_fixtures.generate_del(timeline),
                           stage_map=jira_fixtures.DEL_STAGE_MAP)
            cfg = _config(root, stage_map=jira_fixtures.DEL_STAGE_MAP)
            cfg.db_path = store
            imported = import_bundle(cfg, path)
            self.assertNotEqual(imported.get("verified"), False)

            con = connect(store, read_only=True)
            try:
                court = ballcourt.load(con)
                data = jira.load(con, court)
                payload = tickets.ticket(court, data, "DEL-100")
                sources = {row[0] for row in con.execute(
                    "SELECT DISTINCT source FROM ingest_runs").fetchall()}
            finally:
                con.close()

        self.assertEqual(sources, {"bitbucket-dc", "jira-fixture"})
        self.assertTrue(jira.uses_verification(data))
        boundaries = payload["segments"]["boundaries"]
        for name in ("in_review", "in_verification", "merged", "done"):
            self.assertTrue(boundaries[name]["available"], name)
        rows = {row["key"]: row for row in payload["segments"]["rows"]}
        self.assertIn("merge_verification", rows)
        self.assertIn("verification_done", rows)
        self.assertGreater(rows["verification_done"]["hours"], 0)
        self.assertGreaterEqual(
            boundaries["in_verification"]["ts"], boundaries["merged"]["ts"])
        # DEL-100's assignee is carol; the PR author above is alice. Both keep
        # the Bitbucket pseudonym, because the Jira scrape linked to that map.
        self.assertEqual(payload["story"]["assignee"], "CaOs")


if __name__ == "__main__":
    unittest.main()

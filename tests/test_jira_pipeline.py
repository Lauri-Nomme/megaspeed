"""The Jira reduction: raw payload -> closed labels, keys and pseudonyms.

These are the unit-level guarantees the fixture end-to-end test builds on: the
status *category* (never the workflow's status name), the resolution class, the
summary projection, the worklog-noise bucket, and the changelog walker that
turns status / assignee / sprint / points items into the closed activity set
while dropping everything else.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ingest import taxonomy
from ingest.config import Config
from ingest.jira_pipeline import (
    JiraReducer,
    JiraStats,
    classify_summary,
    log_delay_bucket,
    points_value,
    resolution_class,
    status_stage,
)
from ingest.pseudonym import IdentityTable

UTC = timezone.utc
T0 = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

CATEGORIES = {"10000": "new", "10001": "indeterminate", "10002": "done"}

# The install's real workflows (prd/29 §1): status name, status id, Jira
# category. The category alone cannot tell IN REVIEW from IN PROGRESS, which is
# why the stage map exists.
STORY_WORKFLOW = (
    ("OPEN", "10010", "new"),
    ("READY FOR REFINEMENT", "10011", "new"),
    ("REFINED", "10012", "indeterminate"),
    ("IN PROGRESS", "10013", "indeterminate"),
    ("IN REVIEW", "10014", "indeterminate"),
    ("IN VERIFICATION", "10015", "indeterminate"),
    ("CLOSED", "10016", "done"),
)
BUG_WORKFLOW = (
    ("NEW", "10020", "new"),
    ("TRIAGED", "10021", "indeterminate"),
    ("IN PROGRESS", "10022", "indeterminate"),
    ("IN REVIEW", "10023", "indeterminate"),
    ("IN VERIFICATION", "10024", "indeterminate"),
    ("RESOLVED", "10025", "done"),
)
WORKFLOW_CATEGORIES = {
    status_id: category for _, status_id, category in STORY_WORKFLOW + BUG_WORKFLOW
}
#: ``[jira.stages]`` for the workflows above (prd/29 §2).
WORKFLOW_STAGE_MAP = {
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


def _walk(workflow) -> list:
    """One changelog history per step, with the exact status names and ids."""
    histories = []
    for index, ((name, sid, _c), (next_name, next_id, _n)) in enumerate(
        zip(workflow, workflow[1:]), start=1
    ):
        histories.append(_history(index, T0 + timedelta(days=index), [
            _item("status", name, next_name, sid, next_id),
        ]))
    return histories
REPORTER = {"name": "lauri.nomme", "displayName": "Lauri Nomme", "emailAddress": "lauri.nomme@example.invalid"}
DEV = {"name": "mari.tamm", "displayName": "Mari Tamm", "emailAddress": "mari.tamm@example.invalid"}


def _ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _item(field, from_string=None, to_string=None, from_id=None, to_id=None) -> dict:
    return {
        "field": field,
        "from": from_id,
        "fromString": from_string,
        "to": to_id,
        "toString": to_string,
    }


def _issue(*, key="DEL-100", histories=None, worklogs=None, itype="Story",
           summary="Add login form validation", points=3, epic="DEL-1",
           sprint=None, status=None, resolution=None, resolutiondate=None) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": "zzcanary description",
            "issuetype": {"name": itype},
            "status": status or {"id": "10000", "name": "To Do"},
            "resolution": resolution,
            "resolutiondate": resolutiondate,
            "assignee": DEV,
            "reporter": REPORTER,
            "creator": REPORTER,
            "created": _ts(T0),
            "updated": _ts(T0),
            "customfield_10100": epic,
            "customfield_10106": points,
            "customfield_10104": sprint,
            "worklog": {"total": len(worklogs or []), "worklogs": worklogs or []},
        },
        "changelog": {"histories": histories or []},
    }


def _history(hid, when, items, author=REPORTER) -> dict:
    return {"id": str(hid), "created": _ts(when), "author": author, "items": items}


class PureFunctionTests(unittest.TestCase):
    def test_status_stage_uses_the_category_not_the_name(self):
        # prd/29: the category projection is the deterministic default, and it
        # is why an unconfigured store keeps the pre-prd/29 numbers.
        self.assertEqual(status_stage("10000", "To Do", CATEGORIES), "backlog")
        self.assertEqual(status_stage("10001", "In Progress", CATEGORIES), "in_progress")
        self.assertEqual(status_stage("10002", "Done", CATEGORIES), "done")
        # a review status is category ``indeterminate`` -> in_progress without a
        # map: the collapse prd/29 exists to fix.
        self.assertEqual(
            status_stage("10003", "In Review", {"10003": "indeterminate"}), "in_progress"
        )
        # an unknown id falls back to keyword rules over the name ...
        self.assertEqual(status_stage("99999", "In Review", {}), "in_review")
        self.assertEqual(status_stage("99999", "In Verification", {}), "in_verification")
        self.assertEqual(status_stage("99999", "Refined", {}), "ready")
        self.assertEqual(status_stage("99999", "Ready for Refinement", {}), "backlog")
        # ... and an unknown name is ``other``, never the name itself
        self.assertEqual(status_stage("99999", "Waiting for the vendor", {}), "other")
        self.assertNotIn("Waiting for the vendor", taxonomy.STATUS_STAGE)

    def test_the_stage_map_beats_the_category_beats_the_name(self):
        stage_map = {"in review": "in_review", "in verification": "in_verification"}
        # 1. the config map wins, even over a category that says otherwise
        self.assertEqual(
            status_stage("10003", "In Review", {"10003": "indeterminate"}, stage_map),
            "in_review",
        )
        self.assertEqual(
            status_stage("10004", "IN VERIFICATION", {"10004": "new"}, stage_map),
            "in_verification",
        )
        # 2. the category wins over the name rules ("Refined" would be ready)
        self.assertEqual(
            status_stage("10005", "Refined", {"10005": "indeterminate"}, stage_map),
            "in_progress",
        )
        # 3. name rules only for an id the categories map does not know
        self.assertEqual(status_stage("99999", "Triaged", {}, stage_map), "ready")
        # 4. every answer is a stage, and a name never crosses
        for value in ("backlog", "ready", "in_progress", "in_review", "in_verification"):
            self.assertIn(value, taxonomy.STATUS_STAGE)
        self.assertEqual(status_stage("99999", "Escalated", {}, stage_map), "other")

    def test_resolution_class(self):
        self.assertIsNone(resolution_class(None))
        self.assertIsNone(resolution_class(""))
        self.assertEqual(resolution_class("Done"), "done")
        self.assertEqual(resolution_class("Fixed"), "done")
        self.assertEqual(resolution_class("Won't Do"), "wont_do")
        self.assertEqual(resolution_class("Duplicate"), "duplicate")
        self.assertEqual(resolution_class("Cannot Reproduce"), "wont_do")
        self.assertEqual(resolution_class("Escalated to legal"), "other")
        self.assertIn(resolution_class("Escalated to legal"), taxonomy.RESOLUTION_CLASS)

    def test_summary_class_is_a_label_not_a_summary(self):
        self.assertEqual(classify_summary("Fix the shard rebalancing race"), "fix")
        self.assertEqual(classify_summary("Add login form validation"), "feature")
        self.assertEqual(classify_summary("Document the rollback runbook"), "docs")
        self.assertEqual(classify_summary("Rotate the webhook secret"), "chore")
        self.assertEqual(classify_summary("Spike: how should we shard?"), "spike")
        self.assertEqual(classify_summary("Delivery platform epic"), "other")
        for text in ("Fix the shard race", "Add a widget", ""):
            self.assertIn(classify_summary(text), taxonomy.SUMMARY_CLASS)

    def test_points_are_whole_and_rounding_is_reported(self):
        self.assertEqual(points_value(5), (5, False))
        self.assertEqual(points_value("8"), (8, False))
        self.assertEqual(points_value(2.5), (3, True))
        self.assertEqual(points_value(2.4), (2, True))
        self.assertEqual(points_value(None), (None, False))
        self.assertEqual(points_value(""), (None, False))
        self.assertEqual(points_value("not a number"), (None, False))

    def test_worklog_noise_buckets(self):
        started = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        same_day = started + timedelta(hours=6)
        next_day = started + timedelta(hours=30)
        within_week = started + timedelta(days=5)
        late = started + timedelta(days=40)
        self.assertEqual(log_delay_bucket(6 * 3600, started, same_day), "same_day")
        self.assertEqual(log_delay_bucket(30 * 3600, started, next_day), "next_day")
        self.assertEqual(log_delay_bucket(5 * 86400, started, within_week), "within_week")
        self.assertEqual(log_delay_bucket(40 * 86400, started, late), "late")
        for delay, created in ((0, started), (30 * 3600, next_day), (40 * 86400, late)):
            self.assertIn(log_delay_bucket(delay, started, created), taxonomy.LOG_DELAY_BUCKET)


class ReducerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[],
            ticket_projects=("DEL",),
        )
        self.ident = IdentityTable(root / "identity.json")
        self.reducer = JiraReducer(
            self.cfg,
            self.ident,
            T0.replace(year=2026, month=11, day=16),
            epic_link_field="customfield_10100",
            sprint_field="customfield_10104",
            story_points_field="customfield_10106",
            status_categories=CATEGORIES,
        )
        self.stats = JiraStats()

    def tearDown(self):
        self.tmp.cleanup()

    def _reduce(self, issue, sprints=(), memberships=None):
        self.reducer.prime([issue], list(sprints))
        for project, sprint in sprints:
            self.reducer.reduce_sprint(sprint, project, self.stats)
        self.reducer.reduce_issue(
            issue,
            project="DEL",
            memberships=memberships or {},
            stats=self.stats,
        )
        return self.stats

    def test_story_becomes_an_object_with_closed_attributes(self):
        stats = self._reduce(_issue())
        story = next(o for o in stats.objects if o["object_type"] == "STORY")
        self.assertEqual(story["object_id"], "STORY:DEL-100")
        self.assertEqual(story["ticket_keys"], ["DEL-100"])
        self.assertEqual(story["state"], "open")
        self.assertEqual(story["attributes"]["points"], 3)
        self.assertEqual(story["attributes"]["epic_key"], "DEL-1")
        self.assertEqual(story["attributes"]["summary_class"], "feature")
        taxonomy.check_object_attributes(story["attributes"])

    def test_created_event_names_the_reporter_and_the_epic(self):
        epic = _issue(key="DEL-1", itype="Epic", points=None, epic=None)
        story = _issue()
        # Both issues are in the bundle, so the epic link is a real reference.
        self.reducer.prime([epic, story], [])
        self.reducer.reduce_issue(
            story, project="DEL", memberships={}, stats=self.stats
        )
        stats = self.stats
        created = next(e for e in stats.events if e["activity"] == "created")
        self.assertEqual(created["event_key"], "DEL-100:created")
        self.assertEqual(created["actor"], "LaNo")
        self.assertIsNone(created["repo"])
        self.assertIsNone(created["pr_id"])
        objects = [oid for key, _type, oid in stats.relations if key == created["event_key"]]
        self.assertIn("STORY:DEL-100", objects)
        self.assertIn("EPIC:DEL-1", objects)

    def test_a_status_change_is_a_category_transition(self):
        histories = [
            _history(1, T0 + timedelta(days=2), [
                _item("status", "To Do", "In Progress", "10000", "10001"),
            ]),
            _history(2, T0 + timedelta(days=4), [
                _item("resolution", None, "Done", None, "10000"),
                _item("status", "In Progress", "Done", "10001", "10002"),
            ]),
        ]
        stats = self._reduce(_issue(histories=histories))
        changes = [e for e in stats.events if e["activity"] == "status_changed"]
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[0]["attributes"]["from_status"], "backlog")
        self.assertEqual(changes[0]["attributes"]["to_status"], "in_progress")
        self.assertEqual(changes[1]["attributes"]["to_status"], "done")
        self.assertEqual(stats.transitions, 2)
        # a no-op status write is not a transition
        stats = JiraStats()
        self.stats = stats
        self.reducer.actor_kinds = {"system": "bot"}
        histories = [_history(3, T0, [_item("status", "Done", "Done", "10002", "10002")])]
        self._reduce(_issue(histories=histories))
        self.assertEqual([e for e in stats.events if e["activity"] == "status_changed"], [])
        self.assertEqual(stats.changelog_ignored, 1)

    def test_the_install_workflows_project_onto_stages(self):
        """Story and Bug changelogs -> the exact stage transitions (prd/29).

        OPEN -> READY FOR REFINEMENT is two *backlog* statuses, so it is not a
        stage transition and emits nothing; every other step does, and each row
        passes the closed-vocabulary gate.
        """
        story = _issue(
            key="DEL-200",
            status={"id": "10010", "name": "OPEN"},
            histories=_walk(STORY_WORKFLOW),
        )
        bug = _issue(
            key="DEL-201", itype="Bug",
            status={"id": "10020", "name": "NEW"},
            histories=_walk(BUG_WORKFLOW),
        )
        self.reducer.stage_map = dict(WORKFLOW_STAGE_MAP)
        self.reducer.status_categories = dict(WORKFLOW_CATEGORIES)
        self.reducer.prime([story, bug], [])
        self.reducer.reduce_issue(story, project="DEL", memberships={}, stats=self.stats)
        self.reducer.reduce_issue(bug, project="DEL", memberships={}, stats=self.stats)
        stats = self.stats
        changes = [e for e in stats.events if e["activity"] == "status_changed"]

        def pairs(key):
            return [
                (e["attributes"]["from_status"], e["attributes"]["to_status"])
                for e in changes if e["event_key"].startswith(f"{key}:")
            ]

        self.assertEqual(pairs("DEL-200"), [
            ("backlog", "ready"),
            ("ready", "in_progress"),
            ("in_progress", "in_review"),
            ("in_review", "in_verification"),
            ("in_verification", "done"),
        ])
        self.assertEqual(pairs("DEL-201"), [
            ("backlog", "ready"),
            ("ready", "in_progress"),
            ("in_progress", "in_review"),
            ("in_review", "in_verification"),
            ("in_verification", "done"),
        ])
        # the closed-vocabulary gate accepts every emitted row
        for event in changes:
            taxonomy.check_attributes(event["attributes"])
            self.assertIn(event["attributes"]["to_status"], taxonomy.STATUS_STAGE)
        # OPEN -> READY FOR REFINEMENT (both backlog) was counted, not stored
        self.assertGreaterEqual(stats.changelog_ignored, 1)

    def test_without_a_stage_map_the_same_workflow_is_the_coarse_projection(self):
        """No config => today's numbers: review/QA fold into in_progress."""
        story = _issue(
            key="DEL-202",
            status={"id": "10010", "name": "OPEN"},
            histories=_walk(STORY_WORKFLOW),
        )
        self.reducer.status_categories = dict(WORKFLOW_CATEGORIES)
        stats = self._reduce(story)
        changes = [e for e in stats.events if e["activity"] == "status_changed"]
        self.assertEqual(
            [(e["attributes"]["from_status"], e["attributes"]["to_status"])
             for e in changes],
            [("backlog", "in_progress"), ("in_progress", "done")],
        )

    def test_points_assignee_and_sprint_changes(self):
        histories = [
            _history(1, T0 + timedelta(days=1), [
                _item("Story Points", None, "2.5", None, "2.5"),
            ]),
            _history(2, T0 + timedelta(days=2), [
                _item("assignee", "Mari Tamm", "Liisa Rand", "mari.tamm", "liisa.rand"),
            ]),
            _history(3, T0 + timedelta(days=3), [
                _item("Sprint", None, "DEL Sprint 1", None, "1"),
            ]),
            _history(4, T0 + timedelta(days=30), [
                _item("Sprint", "DEL Sprint 1", "DEL Sprint 2", "1", "2"),
            ]),
        ]
        sprint1 = {"id": 1, "name": "DEL Sprint 1", "state": "closed",
                   "startDate": "2026-08-17T09:00:00.000Z", "endDate": "2026-09-11T17:00:00.000Z"}
        sprint2 = {"id": 2, "name": "DEL Sprint 2", "state": "active",
                   "startDate": "2026-09-14T09:00:00.000Z", "endDate": "2026-10-09T17:00:00.000Z"}
        stats = self._reduce(
            _issue(histories=histories), sprints=[("DEL", sprint1), ("DEL", sprint2)]
        )
        pointed = [e for e in stats.events if e["activity"] == "pointed"]
        self.assertEqual(pointed[0]["attributes"]["points"], 3)
        self.assertEqual(stats.points_rounded, 1)

        assigned = [e for e in stats.events if e["activity"] == "assigned"]
        self.assertEqual(assigned[0]["attributes"]["change"], "added")
        self.assertIn("PERSON:LiRa", [
            oid for key, _t, oid in stats.relations if key == assigned[0]["event_key"]
        ])

        added = [e for e in stats.events if e["activity"] == "sprint_added"]
        removed = [e for e in stats.events if e["activity"] == "sprint_removed"]
        self.assertEqual([e["attributes"]["sprint_id"] for e in added], [1, 2])
        self.assertEqual([e["attributes"]["sprint_id"] for e in removed], [1])
        self.assertFalse(added[0]["attributes"].get("at_start"))
        sprint_objects = [
            oid for key, _t, oid in stats.relations if key == added[0]["event_key"]
        ]
        self.assertIn("SPRINT:DEL#1", sprint_objects)

    def test_membership_without_a_changelog_item_is_at_start(self):
        sprint1 = {"id": 1, "name": "DEL Sprint 1", "state": "closed",
                   "startDate": "2026-08-17T09:00:00.000Z", "endDate": "2026-09-11T17:00:00.000Z"}
        stats = self._reduce(
            _issue(histories=[]),
            sprints=[("DEL", sprint1)],
            memberships={"DEL-100": {1}},
        )
        added = [e for e in stats.events if e["activity"] == "sprint_added"]
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0]["attributes"]["at_start"])
        self.assertEqual(added[0]["actor"], "system")
        self.assertEqual(added[0]["ts"], datetime(2026, 8, 17, 9, 0))
        self.assertEqual(stats.sprint_at_start, 1)

    def test_worklog_carries_the_created_started_gap(self):
        started = T0 + timedelta(days=1)
        created = started + timedelta(days=3)  # logged three days later
        worklogs = [{
            "id": "77",
            "author": DEV,
            "started": _ts(started),
            "created": _ts(created),
            "timeSpentSeconds": 5400,
            "comment": "zzcanary worklog comment",
        }]
        stats = self._reduce(_issue(worklogs=worklogs))
        logged = [e for e in stats.events if e["activity"] == "worklogged"]
        self.assertEqual(len(logged), 1)
        attributes = logged[0]["attributes"]
        self.assertEqual(attributes["time_spent_seconds"], 5400)
        self.assertEqual(attributes["log_delay_seconds"], 3 * 86400)
        self.assertEqual(attributes["log_delay_bucket"], "within_week")
        self.assertEqual(logged[0]["ts"], created.replace(tzinfo=None))
        self.assertEqual(logged[0]["event_key"], "DEL-100:worklog:77")
        self.assertEqual(stats.log_delays, {"within_week": 1})

    def test_unknown_changelog_fields_are_dropped_not_stored(self):
        histories = [
            _history(1, T0, [
                _item("summary", None, "Add login form validation zzcanary"),
                _item("description", None, "zzcanary body"),
                _item("Epic Child", None, "DEL-101"),
                _item("labels", None, "auth"),
                _item("Rank", None, "0|i00007:"),
            ]),
        ]
        stats = self._reduce(_issue(histories=histories))
        self.assertEqual(len(stats.events), 1)  # only ``created``
        self.assertEqual(stats.changelog_ignored, 5)
        payload = repr(stats.events).lower()
        self.assertNotIn("zzcanary", payload)
        self.assertNotIn("epic child", payload)

    def test_an_epic_becomes_an_epic_object(self):
        stats = self._reduce(_issue(key="DEL-1", itype="Epic", points=None, epic=None))
        epic = next(o for o in stats.objects if o["object_type"] == "EPIC")
        self.assertEqual(epic["object_id"], "EPIC:DEL-1")
        self.assertEqual(epic["ticket_keys"], ["DEL-1"])
        self.assertEqual(epic["attributes"]["summary_class"], "feature")
        self.assertEqual(stats.epics, 1)

    def test_an_unknown_issue_type_is_skipped(self):
        stats = self._reduce(_issue(itype="Sub-task"))
        self.assertEqual(stats.events, [])
        self.assertEqual(stats.issues_skipped_type, 1)

    def test_a_story_never_references_an_epic_outside_the_bundle(self):
        # The epic was not scraped (e.g. the run was scoped to one key): the
        # Story object keeps the epic *key*, but no event points at a dangling
        # EPIC object (that would fail the contract on import).
        stats = self._reduce(_issue())
        self.assertEqual(stats.objects[0]["attributes"]["epic_key"], "DEL-1")
        referenced = {oid for _k, _t, oid in stats.relations}
        self.assertNotIn("EPIC:DEL-1", referenced)


if __name__ == "__main__":
    unittest.main()

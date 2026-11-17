"""The closed vocabulary and the store shapes for the Jira side (prd/02, prd/03).

A Jira event has no repo/PR, so its natural key is ``KEY:natural``; an Epic /
Story object id carries a ticket key and a Sprint id is ``PROJECT#id``. These are
the shapes ``--verify`` and the import gate must accept -- and the shapes they
must still reject.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import re
import unittest

from ingest import taxonomy
from ingest.store import EVENT_KEY_RE, SOURCES, object_shape_problems

KEY_SHAPE = r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$"


class VocabularyTests(unittest.TestCase):
    def test_jira_activities_are_in_the_closed_set(self):
        for activity in (
            "created",
            "status_changed",
            "assigned",
            "pointed",
            "sprint_added",
            "sprint_removed",
            "worklogged",
        ):
            self.assertIn(activity, taxonomy.ACTIVITIES)

    def test_jira_object_types_are_declared(self):
        for object_type in ("EPIC", "STORY", "SPRINT"):
            self.assertIn(object_type, taxonomy.OBJECT_TYPES)

    def test_issue_and_sprint_states_are_closed(self):
        self.assertEqual(taxonomy.ISSUE_STATE, {"open", "done", "wont_do", "duplicate", "other"})
        self.assertEqual(taxonomy.SPRINT_STATE, {"future", "active", "closed"})

    def test_status_stages_are_a_closed_set(self):
        # prd/29: the fixed stage vocabulary. ``STATUS_CLASS`` is the pre-prd/29
        # name for the same closed set (an alias, not a second vocabulary).
        self.assertEqual(
            taxonomy.STATUS_STAGE,
            {"backlog", "ready", "in_progress", "in_review", "in_verification",
             "done", "other"},
        )
        self.assertIs(taxonomy.STATUS_CLASS, taxonomy.STATUS_STAGE)

    def test_normalize_stage_reads_every_store_generation(self):
        # a legacy store wrote ``todo`` where the stage vocabulary writes backlog
        self.assertEqual(taxonomy.normalize_stage("todo"), "backlog")
        # stages pass through, including the coarse ones old stores already had
        for stage in ("backlog", "ready", "in_progress", "in_review",
                      "in_verification", "done", "other"):
            self.assertEqual(taxonomy.normalize_stage(stage), stage)
        # anything else is ``other``, never the value itself
        self.assertEqual(taxonomy.normalize_stage("In Review"), "other")
        self.assertEqual(taxonomy.normalize_stage(None), "other")
        self.assertEqual(taxonomy.normalize_stage(""), "other")

    def test_worklog_noise_buckets_are_closed(self):
        self.assertEqual(
            taxonomy.LOG_DELAY_BUCKET, {"same_day", "next_day", "within_week", "late"}
        )

    def test_jira_event_attributes_validate(self):
        ok = {
            "from_status": "backlog",
            "to_status": "done",
            "sprint_id": 3,
            "at_start": True,
            "points": 5,
            "time_spent_seconds": 3600,
            "log_delay_seconds": 86_400,
            "log_delay_bucket": "next_day",
        }
        self.assertEqual(taxonomy.check_attributes(ok), ok)

    def test_a_raw_status_name_cannot_be_stored_as_an_attribute(self):
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.check_attributes({"to_status": "In Review"})
        # ... and neither can the pre-prd/29 label: the gate accepts stages only
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.check_attributes({"from_status": "todo"})
        # every stage does validate, both ends
        for stage in taxonomy.STATUS_STAGE:
            taxonomy.check_attributes({"from_status": stage, "to_status": stage})
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.check_attributes({"points": -1})
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.check_attributes({"log_delay_bucket": "eventually"})

    def test_jira_object_attributes_validate(self):
        ok = {
            "summary_class": "feature",
            "goal_class": "other",
            "epic_key": "DEL-1",
            "points": 3,
            "end_date": "2026-09-11",
        }
        self.assertEqual(taxonomy.check_object_attributes(ok), ok)

    def test_a_summary_cannot_be_stored_as_an_object_attribute(self):
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.check_object_attributes({"summary_class": "add login form validation"})
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.check_object_attributes({"epic_key": "Delivery platform epic"})
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.check_object_attributes({"end_date": "11 September"})


class EventKeyShapeTests(unittest.TestCase):
    def test_bitbucket_keys_still_match(self):
        self.assertRegex("DEL/delivery-api#42:act:387", EVENT_KEY_RE)

    def test_jira_keys_match(self):
        for key in (
            "DEL-100:created",
            "DEL-100:changelog:10190:status:0",
            "DEL-100:changelog:10111:sprint:added",
            "DEL-100:worklog:10000",
            "DEL-1:created",
        ):
            self.assertRegex(key, EVENT_KEY_RE)

    def test_prose_and_bare_keys_do_not_match(self):
        for key in (
            "DEL-100",
            "DEL-100:Add login form validation",
            "feature/secret-q4",
            "DEL-100:created: ",
        ):
            self.assertIsNone(re.fullmatch(EVENT_KEY_RE, key), key)


class ObjectShapeTests(unittest.TestCase):
    def test_epic_and_story_ids_carry_their_key(self):
        self.assertEqual(object_shape_problems("EPIC", "EPIC:DEL-1", "open", KEY_SHAPE), [])
        self.assertEqual(object_shape_problems("STORY", "STORY:DEL-100", "done", KEY_SHAPE), [])
        self.assertEqual(
            object_shape_problems("STORY", "STORY:DEL-100", "open", KEY_SHAPE, ("DEL-100",)), []
        )

    def test_a_story_outside_the_configured_key_set_is_rejected(self):
        problems = object_shape_problems("STORY", "STORY:DEL-101", "open", KEY_SHAPE, ("DEL-100",))
        self.assertTrue(any("configured ticket keys" in p for p in problems), problems)

    def test_a_story_id_that_is_not_a_key_is_rejected(self):
        problems = object_shape_problems(
            "STORY", "STORY:add-login-form-validation", "open", KEY_SHAPE
        )
        self.assertTrue(any("not a key" in p for p in problems), problems)

    def test_sprint_ids(self):
        self.assertEqual(object_shape_problems("SPRINT", "SPRINT:DEL#3", "active"), [])
        for bad in ("SPRINT:3", "SPRINT:DEL#three", "SPRINT:del#3"):
            problems = object_shape_problems("SPRINT", bad, "active")
            self.assertTrue(problems, bad)

    def test_states_are_per_type(self):
        problems = object_shape_problems("SPRINT", "SPRINT:DEL#3", "running")
        self.assertTrue(any("state" in p for p in problems), problems)
        problems = object_shape_problems("STORY", "STORY:DEL-100", "merged", KEY_SHAPE)
        self.assertTrue(any("state" in p for p in problems), problems)
        # a PR must not be able to borrow a Jira state
        problems = object_shape_problems("PR", "PR:DEL/delivery-api#7", "done")
        self.assertTrue(any("state" in p for p in problems), problems)


class SourceTests(unittest.TestCase):
    def test_both_source_families_are_known(self):
        self.assertEqual(
            set(SOURCES),
            {"fixture", "bitbucket-dc", "jira-fixture", "jira-dc", "jira-cloud"},
        )

    def test_the_cloud_source_is_the_jira_family(self):
        # prd/24: the same ticket layer, a different API surface.
        self.assertIn("jira-cloud", SOURCES)
        self.assertIn("jira-dc", SOURCES)


if __name__ == "__main__":
    unittest.main()

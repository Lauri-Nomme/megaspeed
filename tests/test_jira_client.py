"""The Jira REST client, against the fixture transport (no network).

What the scrape relies on and what these tests pin: custom-field discovery (the
ids are installation-specific), the status -> category map, Jira/Agile paging,
sprint membership, the Greenhopper Sprint field's many shapes, and the
"inline worklog page is truncated -> fetch the worklog endpoint" path.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from ingest import jira_fixtures
from ingest.jira import (
    FIELD_EPIC_LINK,
    FIELD_SPRINT,
    FIELD_STORY_POINTS,
    JiraClient,
    JiraError,
    request_kind,
    sprint_id,
    sprint_state,
)
from ingest.transport import FixtureJiraTransport


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fixture = jira_fixtures.generate()
        self.transport = FixtureJiraTransport(self.fixture)

    def test_field_discovery_and_status_categories(self):
        client = JiraClient(self.transport, page_limit=5, min_interval=0)
        fields = client.field_ids()
        self.assertEqual(fields[FIELD_EPIC_LINK], "customfield_10100")
        self.assertEqual(fields[FIELD_SPRINT], "customfield_10104")
        self.assertEqual(fields[FIELD_STORY_POINTS], "customfield_10106")
        self.assertEqual(client.status_categories()["10002"], "done")
        self.assertEqual(client.requests_by_kind["fields"], 1)
        self.assertEqual(client.requests_by_kind["statuses"], 1)

    def test_paging_walks_every_page(self):
        client = JiraClient(self.transport, page_limit=2, min_interval=0, list_page_limit=2)
        rows = list(client.search("project = PROJ"))
        self.assertEqual(len(rows), len(self.fixture["issues"]))
        self.assertGreater(client.requests_by_kind["search"], 1)

    def test_search_projects_only_the_requested_fields(self):
        client = JiraClient(self.transport, page_limit=50, min_interval=0)
        rows = list(client.search("project = PROJ", fields="key,updated"))
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row["fields"]), {"updated"})

    def test_jql_scope_is_honoured_by_the_fixture(self):
        client = JiraClient(self.transport, page_limit=50, min_interval=0)
        keys = [row["key"] for row in client.search("key in (PROJ-101,PROJ-102)")]
        self.assertEqual(sorted(keys), ["PROJ-101", "PROJ-102"])
        keys = [row["key"] for row in client.search("project = NOPE")]
        self.assertEqual(keys, [])

    def test_sprint_membership_and_board(self):
        client = JiraClient(self.transport, page_limit=50, min_interval=0)
        board = client.scrum_board("PROJ")
        self.assertEqual(board, 1)
        sprints = {int(s["id"]): s for s in client.sprints(board)}
        self.assertEqual(sorted(sprints), [1, 2, 3])
        self.assertEqual(sprints[3]["state"], "active")
        self.assertEqual(
            sorted(client.sprint_issue_keys(1)), ["PROJ-101", "PROJ-102", "PROJ-103", "PROJ-104"]
        )

    def test_a_truncated_inline_worklog_page_is_completed(self):
        class Truncating:
            def __init__(self, full):
                self.full = full
                self.calls: list[str] = []

            def get(self, path, params=None):
                self.calls.append(path)
                if path.endswith("/worklog"):
                    return {
                        "startAt": 0,
                        "maxResults": 100,
                        "total": len(self.full),
                        "worklogs": self.full,
                    }
                raise AssertionError(path)

        full = [{"id": str(n)} for n in range(25)]
        transport = Truncating(full)
        client = JiraClient(transport, min_interval=0)
        issue = {"key": "DEL-1", "fields": {"worklog": {"total": 25, "worklogs": full[:20]}}}
        self.assertEqual(len(client.complete_worklogs(issue)), 25)
        self.assertEqual(transport.calls, ["/rest/api/2/issue/DEL-1/worklog"])

    def test_a_complete_inline_page_costs_no_extra_request(self):
        client = JiraClient(self.transport, page_limit=50, min_interval=0)
        issue = client.issue("PROJ-101")
        before = client.requests
        logs = client.complete_worklogs(issue)
        self.assertEqual(client.requests, before)
        self.assertEqual(len(logs), 2)

    def test_an_unknown_issue_is_an_error_not_an_empty_issue(self):
        client = JiraClient(self.transport, page_limit=50, min_interval=0)
        with self.assertRaises(JiraError):
            client.issue("PROJ-999")

    def test_request_kinds(self):
        self.assertEqual(request_kind("/rest/api/2/field"), "fields")
        self.assertEqual(request_kind("/rest/api/2/status"), "statuses")
        self.assertEqual(request_kind("/rest/api/2/search"), "search")
        self.assertEqual(request_kind("/rest/api/2/issue/DEL-1"), "issue")
        self.assertEqual(request_kind("/rest/api/2/issue/DEL-1/worklog"), "worklog")
        self.assertEqual(request_kind("/rest/agile/1.0/board"), "boards")
        self.assertEqual(request_kind("/rest/agile/1.0/board/1/sprint"), "sprints")
        self.assertEqual(request_kind("/rest/agile/1.0/sprint/1/issue"), "sprint-issues")


class SprintFieldTests(unittest.TestCase):
    def test_the_sprint_field_is_parsed_in_every_shape(self):
        blob = (
            "com.atlassian.greenhopper.service.sprint.Sprint@1[id=7,"
            "name=DEL Sprint 7,state=ACTIVE]"
        )
        self.assertEqual(sprint_id([blob]), 7)
        self.assertEqual(sprint_id({"id": 8}), 8)
        self.assertEqual(sprint_id(9), 9)
        self.assertEqual(sprint_id("10"), 10)
        self.assertIsNone(sprint_id(None))
        self.assertIsNone(sprint_id("DEL Sprint 1"))
        self.assertIsNone(sprint_id(True))

    def test_sprint_state_is_closed(self):
        self.assertEqual(sprint_state("ACTIVE"), "active")
        self.assertEqual(sprint_state("closed"), "closed")
        self.assertEqual(sprint_state("future"), "future")
        self.assertEqual(sprint_state("running"), "future")


if __name__ == "__main__":
    unittest.main()

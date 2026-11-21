"""The Jira probe must speak while it works, and never walk a whole project.

On a real Cloud project the old probe performed ``list(client.search(...))`` just
to print an issue count: minutes of silence on a big project, and no output at
all until the end. It now:

* prints each step as it completes (``flush=True``);
* asks the API for a count (exact on DC, approximate on Cloud) instead of paging;
* samples at most :data:`PROBE_SAMPLE` issues for the issue-type breakdown.

    python3 -m unittest tests.test_jira_probe -v
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from collections import Counter
from pathlib import Path

from ingest.config import Config
from ingest.jira import JiraClient
from ingest.jira_cloud import CloudJiraClient
from ingest.scrape_cli import PROBE_SAMPLE, _jira_probe

REGEX = r"\b[A-Z]+-\d+\b"
SHAPE = r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$"


class _ProbeClient:
    """A client with ``total`` issues but a lazy search generator."""

    requests = 0
    requests_by_kind = Counter()

    def __init__(self, total: int = 12345, rows: int = 1000):
        self.total = total
        self.rows = rows
        self.consumed = 0
        self.counted_jql = ""

    def check(self):
        return {"version": "9.17.5"}

    def field_ids(self):
        return {"Epic Link": "customfield_10100", "Team": "customfield_10111"}

    def scrum_board(self, project):
        return 7

    def sprints(self, board):
        return [{"id": 1, "state": "closed", "name": "S1"}]

    def count(self, jql):
        self.counted_jql = jql
        return self.total

    def search(self, jql, fields="key,updated"):
        for index in range(self.rows):
            self.consumed += 1
            yield {
                "key": f"PROJ-{index}",
                "fields": {"issuetype": {"name": "Bug" if index % 2 else "Story"}},
            }


def _cfg() -> Config:
    return Config(
        db_path=Path("/tmp/x.duckdb"),
        identity_path=Path("/tmp/x.json"),
        ticket_regex=REGEX,
        key_shape=SHAPE,
        repos=[],
        ticket_projects=("PROJ",),
        jira_projects=("PROJ",),
    )


def _run(client, *, as_json: bool = False,
         projects: tuple[str, ...] | None = None) -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = _jira_probe(
            _cfg(), transport=None, as_json=as_json, client=client,
            projects=projects,
        )
    if as_json:
        assert code == 0
    return out.getvalue(), err.getvalue()


class ProbeTests(unittest.TestCase):
    def test_it_prints_before_and_after_the_issue_work(self):
        client = _ProbeClient()
        out, _err = _run(client)
        self.assertIn("jira 9.17.5", out)
        self.assertIn("PROJ: reading board and sprints", out)
        self.assertIn("board          PROJ: 7 · 1 sprint(s)", out)
        self.assertIn("issues         PROJ: 12,345", out)
        self.assertIn("first 200", out)

    def test_it_samples_instead_of_walking_every_issue(self):
        client = _ProbeClient(total=12345, rows=5000)
        out, _err = _run(client)
        # one extra row tells us whether the sample covered the whole project
        self.assertEqual(client.consumed, PROBE_SAMPLE + 1)
        self.assertIn(f"first {PROBE_SAMPLE}", out)

    def test_an_unknown_total_reports_at_least_the_sample(self):
        client = _ProbeClient(total=None, rows=5000)
        out, _err = _run(client)
        self.assertIn(f"≥{PROBE_SAMPLE}", out)

    def test_json_keeps_stdout_machine_readable(self):
        client = _ProbeClient()
        out, err = _run(client, as_json=True)
        report = json.loads(out)          # no progress text in the JSON
        self.assertEqual(report["issues"]["PROJ"]["total"], 12345)
        self.assertEqual(report["issues"]["PROJ"]["sample"], PROBE_SAMPLE)
        self.assertIn("reading board", err)   # progress goes to stderr

    def test_the_cli_project_overrides_the_configured_list(self):
        # `--project PROJ --probe` must probe PROJ, not whatever the env says.
        client = _ProbeClient()
        out, _err = _run(client, projects=("PROJ",))
        self.assertIn("projects       PROJ", out)
        self.assertIn("issues         PROJ:", out)


class _CountTransport:
    def __init__(self, get_payload=None, post_payload=None):
        self.get_payload = get_payload or {}
        self.post_payload = post_payload or {}
        self.posted: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        return self.get_payload

    def post(self, path, body=None):
        self.posted.append((path, body or {}))
        return self.post_payload


class CountTests(unittest.TestCase):
    def test_dc_uses_the_search_total(self):
        client = JiraClient(_CountTransport(get_payload={"total": 123}))
        self.assertEqual(client.count("issuetype in (Story)"), 123)

    def test_cloud_uses_approximate_count(self):
        transport = _CountTransport(post_payload={"count": 99})
        client = CloudJiraClient(transport)
        self.assertEqual(client.count("issuetype in (Story)"), 99)
        self.assertEqual(transport.posted[0][0], "/rest/api/3/search/approximate-count")
        self.assertEqual(transport.posted[0][1]["jql"], "issuetype in (Story)")
        self.assertEqual(client.requests, 1)

    def test_cloud_falls_back_to_the_legacy_total(self):
        transport = _CountTransport(get_payload={"total": 42})
        client = CloudJiraClient(transport)
        # approximate-count returns {} -> the legacy search's total is used
        self.assertEqual(client.count("x"), 42)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""prd/49 — `repo` as a first-class scope dimension.

The store has always carried each PR's repository; the scope control, Queue,
Blocking, Delivery, Rules and the policy lab did not accept it. These tests pin
the two halves of the fix:

* the analysis slice (``ballcourt.scope_to_repo`` / ``jira.scope_to_repo``) is
  exactly the repository's cohort — PRs, intervals and the stories joined to its
  PRs — while the sentinel/bot *roles* stay store-wide (concepts.md §2);
* the API endpoints accept ``repo=``, echo it, filter their rows and recompute
  the rule cohort, and reject an unknown repo with a 400 that lists the real
  ones (a typo must not read as "0 open items").

The frontend half (the scope option, ``matchesScope`` and the URL round-trip)
lives in ``app/test/prd49.test.mjs``.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from analysis import ballcourt, jira, rules
from ingest.store import connect
from server import app as app_module
from server.harness import BrowserBridge
from tests.test_jira_analysis import Fixture, T0
from tests.test_server import get, make_store

REPO_A = "ENG/platform"
REPO_B = "ENG/mobile"


class RepoScopeCohortTests(unittest.TestCase):
    """The analysis slice: a repo cohort, with store-wide roles."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def fixture(self) -> Fixture:
        """Two stories, each joined to one PR — one per repository."""
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=14))
        # repo A: a 3-point story, 4 h logged, one merged PR
        fx.story("ENG-1", T0, points=3, state="done", closed=T0 + timedelta(hours=90))
        fx.status("ENG-1", T0 + timedelta(hours=10), "in_progress")
        fx.status("ENG-1", T0 + timedelta(hours=90), "done",
                  from_status="in_progress")
        fx.worklog("ENG-1", T0 + timedelta(hours=20), 4.0)
        fx.pr(1, "AaAa", T0 + timedelta(hours=40), tickets=("ENG-1",),
              closed=T0 + timedelta(hours=80), repo=REPO_A)
        fx.pr_event(1, "pr_approved", "RrRr", 30, repo=REPO_A)
        fx.pr_event(1, "pr_merged", "RrRr", 40, repo=REPO_A)
        # repo B: an 8-point story, 2 h logged, one merged PR whose sentinel
        # declaration is the only occurrence of the role in the store
        fx.story("ENG-2", T0, points=8, state="done", closed=T0 + timedelta(hours=90))
        fx.status("ENG-2", T0 + timedelta(hours=10), "in_progress")
        fx.status("ENG-2", T0 + timedelta(hours=90), "done",
                  from_status="in_progress")
        fx.worklog("ENG-2", T0 + timedelta(hours=20), 2.0)
        fx.pr(2, "BbBb", T0 + timedelta(hours=40), tickets=("ENG-2",),
              closed=T0 + timedelta(hours=80), repo=REPO_B)
        fx.pr_event(2, "pr_approved", "FrMo", 30, repo=REPO_B, sentinel=True)
        fx.pr_event(2, "pr_merged", "FrMo", 40, repo=REPO_B)
        return fx

    def analyse(self):
        self.fixture().write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            data = jira.load(con, court)
            window = rules.window_for(court, None, data)
            threads = rules.load_threads(con)
            bots = rules.load_bots(con)
            sizes = rules.load_pr_sizes(con)
        finally:
            con.close()
        return court, data, window, threads, bots, sizes

    def test_the_slice_is_the_repo_cohort_and_roles_stay_store_wide(self):
        court, data, *_ = self.analyse()
        self.assertEqual(court.repos, [REPO_B, REPO_A])
        scoped = ballcourt.scope_to_repo(court, REPO_A)
        self.assertEqual([s.pr["object_id"] for s in scoped.prs], [f"PR:{REPO_A}#1"])
        self.assertTrue(scoped.intervals)
        self.assertTrue(all(i.repo == REPO_A for i in scoped.intervals))
        # the vertical dimensions do not narrow: a sentinel declared on a repo-B
        # event is still the sentinel for the repo-A slice
        self.assertEqual(scoped.sentinels, {"FrMo"})
        self.assertEqual(scoped.bots, court.bots)

        stories = jira.scope_to_repo(data, REPO_A)
        self.assertEqual([story.key for story in stories.stories], ["ENG-1"])
        self.assertEqual(
            [state.pr["repo"] for story in stories.stories for state in story.prs],
            [REPO_A],
        )
        # the story's worklog half is measured on the scoped cohort, not the
        # whole store's stories
        self.assertEqual(stories.events, len(stories.stories[0].events))

    def test_a_repo_scoped_rule_uses_the_repo_cohort(self):
        court, data, window, threads, bots, sizes = self.analyse()
        whole = rules.insights(
            court, threads, window, bots=bots, sizes=sizes, jira=data,
        )
        scoped_court = ballcourt.scope_to_repo(court, REPO_B)
        scoped_jira = jira.scope_to_repo(data, REPO_B)
        scoped = rules.insights(
            scoped_court, threads, window, bots=bots, sizes=sizes, jira=scoped_jira,
        )
        # the cohort, not only the rows: whole store 2 PRs, repo B 1 PR
        self.assertEqual(whole["dataset"]["prs"], 2)
        self.assertEqual(scoped["dataset"]["prs"], 1)
        self.assertEqual(scoped["sample"]["prs"], 1)
        self.assertEqual(scoped["metrics"]["pr_size"]["n"], 1)
        # the Jira half narrows too: one story, one worklog — the 2 h of repo B,
        # never repo A's 4 h
        worklog = scoped["metrics"]["worklog_cross_check"]
        self.assertTrue(worklog["available"])
        self.assertEqual(worklog["stories_with_worklogs"], 1)
        self.assertEqual(worklog["logged_hours"], 2.0)
        self.assertEqual([row["key"] for row in worklog["rows"]], ["ENG-2"])
        # every drill-down row names only this repo's PRs
        for name, metric in scoped["metrics"].items():
            for row in metric.get("rows") or ():
                if row.get("repo"):
                    self.assertEqual(row["repo"], REPO_B, f"{name} leaked {row['repo']}")


class RepoScopeApiTests(unittest.TestCase):
    """The endpoints: ``?repo=`` filters, echoes and validates."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name).resolve()
        cls.db = make_store(cls.dir, "alpha", n=40, seed=101)
        cls.patches = [
            mock.patch.object(app_module, "TENANTS_DIR", cls.dir),
            mock.patch.object(app_module, "DEFAULT_TENANT", ""),
            mock.patch.object(app_module, "bridge", BrowserBridge()),
            mock.patch.object(app_module, "LOG", False),
            mock.patch.dict(os.environ, {app_module.HARNESS_TOKEN_ENV: "t"}),
        ]
        for patch in cls.patches:
            patch.start()
        cls.con = connect(cls.db, read_only=True)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        for patch in reversed(cls.patches):
            patch.stop()
        cls.tmp.cleanup()

    def pr_count(self, repo: str) -> int:
        return int(self.con.execute(
            "SELECT count(*) FROM objects WHERE object_type = 'PR' AND repo = ?",
            [repo],
        ).fetchone()[0])

    def test_delivery_queue_and_blocking_are_the_repo_slice(self):
        self.assertTrue(self.pr_count(REPO_A))
        whole = get("/api/delivery")
        self.assertEqual(whole.status, 200, whole.text)
        scoped = get("/api/delivery", repo=REPO_A)
        self.assertEqual(scoped.status, 200, scoped.text)
        body = scoped.json()
        self.assertEqual(body["repo"], REPO_A)
        self.assertTrue(body["rows"])
        self.assertTrue(all(row["repo"] == REPO_A for row in body["rows"]))
        self.assertEqual(body["counts"]["open"], len(body["rows"]))
        self.assertLess(body["counts"]["open"], whole.json()["counts"]["open"])
        self.assertTrue(all(row["repo"] == REPO_A for row in body["done_open"]["rows"]))

        other = get("/api/delivery", repo=REPO_B).json()
        self.assertTrue(
            {row["object_id"] for row in body["rows"]}.isdisjoint(
                {row["object_id"] for row in other["rows"]}
            )
        )

        for path in ("/api/queues", "/api/blocking"):
            with self.subTest(path=path):
                reply = get(path, repo=REPO_A)
                self.assertEqual(reply.status, 200, reply.text)
                self.assertEqual(reply.json()["repo"], REPO_A)

    def test_rules_recompute_on_the_repo_cohort(self):
        whole = get("/api/insights").json()
        scoped = get("/api/insights", repo=REPO_A).json()
        self.assertEqual(scoped["repo"], REPO_A)
        # the repo's PRs are the cohort every rule measures
        self.assertEqual(scoped["dataset"]["prs"], self.pr_count(REPO_A))
        self.assertEqual(scoped["sample"]["prs"], self.pr_count(REPO_A))
        self.assertEqual(whole["dataset"]["prs"], sum(
            self.pr_count(repo) for repo in (REPO_A, REPO_B)
        ))
        self.assertLess(scoped["dataset"]["prs"], whole["dataset"]["prs"])
        for row in scoped["metrics"]["pr_size"]["rows"]:
            self.assertEqual(row["repo"], REPO_A)

    def test_an_unknown_repo_is_a_400_listing_the_real_ones(self):
        reply = get("/api/delivery", repo="ENG/nope")
        self.assertEqual(reply.status, 400, reply.text)
        body = reply.json()
        self.assertFalse(body["ok"])
        self.assertIn("ENG/nope", body["error"])
        self.assertIn(REPO_A, body["error"])
        self.assertIn(REPO_B, body["error"])


if __name__ == "__main__":
    unittest.main()

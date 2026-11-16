"""prd/22 ingest slice: a PR's branch refs are persisted as one-way digests.

The stack DAG only ever asks "is this the same branch?" (``A.toRef ==
B.fromRef``), so equality is all the store needs. The branch *name* is the one
thing prd/02 forbids: a DC branch is the ticket summary in slug form
(``DEL-101-add-login-form-validation``), ``schema.sql`` has no branch column and
``--verify`` rejects prose anywhere. A digest answers the question without the
name, and the repo is part of it so two repos' ``main`` cannot look like one
branch.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ingest import taxonomy
from ingest.bitbucket import PullRequest
from ingest.config import Config
from ingest.pipeline import KeyFilter, Reducer, Stats, ref_digest
from ingest.pseudonym import IdentityTable
from ingest.store import connect, ensure_schema, verify, write_objects, write_run

AS_OF = datetime(2026, 11, 15, 12, 0, 0)
T0 = 1_790_000_000_000  # epoch ms
REPO = "ENG/platform"
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}


def digest(branch: str, repo: str = REPO) -> str:
    return ref_digest(repo, {"displayId": branch})


class RefDigestTests(unittest.TestCase):
    def test_the_same_branch_is_the_same_digest_and_the_name_is_gone(self):
        a = ref_digest(REPO, {"displayId": "feature/ENG-1-base"})
        b = ref_digest(REPO, {"displayId": "feature/ENG-1-base"})
        self.assertEqual(a, b)
        self.assertRegex(a, taxonomy.REF_DIGEST)
        self.assertNotIn("ENG-1-base", a)
        self.assertNotEqual(a, ref_digest(REPO, {"displayId": "main"}))

    def test_a_branch_ref_is_repo_scoped(self):
        # two repos' main must not look like the same branch: that would invent
        # a cross-repo stack edge
        self.assertNotEqual(
            ref_digest("ENG/platform", {"displayId": "main"}),
            ref_digest("ENG/mobile", {"displayId": "main"}),
        )

    def test_a_missing_ref_is_none_not_empty(self):
        self.assertIsNone(ref_digest(REPO, None))
        self.assertIsNone(ref_digest(REPO, {}))
        self.assertIsNone(ref_digest(REPO, {"displayId": "  "}))

    def test_the_object_attribute_spec_accepts_a_digest_and_rejects_a_name(self):
        payload = {"source_ref": digest("feature/ENG-1-b"), "target_ref": digest("main")}
        self.assertEqual(taxonomy.check_object_attributes(payload), payload)
        for bad in ({"source_ref": "feature/ENG-1-b"}, {"target_ref": "DEL-101-add-login"}):
            with self.assertRaises(taxonomy.TaxonomyError):
                taxonomy.check_object_attributes(bad)


class ReducerRefTests(unittest.TestCase):
    """The reducer is where the raw ``fromRef``/``toRef`` are read and dropped."""

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
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape, self.cfg.ticket_projects),
            AS_OF,
        )
        self.stats = Stats()

    def tearDown(self):
        self.tmp.cleanup()

    def reduce(self, pr_id: int, source: str, target: str) -> dict:
        pr = {
            "id": pr_id,
            "state": "OPEN",
            "createdDate": T0,
            "fromRef": {"displayId": source, "latestCommit": "a" * 40},
            "toRef": {"displayId": target, "latestCommit": "b" * 40},
            "author": {"user": ADA},
        }
        pull = PullRequest("ENG", "platform", pr, [])
        pull.activities = [{
            "id": 1, "action": "OPENED", "createdDate": T0, "user": ADA,
        }]
        self.reducer.reduce(pull, ["ENG-1"], pull.activities, self.stats)
        record = next(o for o in self.stats.objects if o["object_type"] == "PR")
        self.stats = Stats()
        return record

    def test_a_stacked_child_matches_its_parent_by_digest(self):
        parent = self.reduce(1, "feature/ENG-1-base", "main")
        child = self.reduce(2, "feature/ENG-1-b", "feature/ENG-1-base")
        self.assertEqual(parent["attributes"]["source_ref"], digest("feature/ENG-1-base"))
        self.assertEqual(parent["attributes"]["target_ref"], digest("main"))
        # the edge: the parent's source branch is the child's target branch
        self.assertEqual(parent["attributes"]["source_ref"], child["attributes"]["target_ref"])
        self.assertEqual(child["attributes"]["source_ref"], digest("feature/ENG-1-b"))

    def test_a_pr_without_refs_carries_no_ref_attributes(self):
        pr = {
            "id": 3,
            "state": "OPEN",
            "createdDate": T0,
            "author": {"user": ADA},
        }
        pull = PullRequest("ENG", "platform", pr, [])
        pull.activities = [{"id": 1, "action": "OPENED", "createdDate": T0, "user": ADA}]
        self.reducer.reduce(pull, ["ENG-1"], pull.activities, self.stats)
        record = next(o for o in self.stats.objects if o["object_type"] == "PR")
        self.assertNotIn("source_ref", record["attributes"])
        self.assertNotIn("target_ref", record["attributes"])

    def test_the_branch_name_never_reaches_the_object(self):
        record = self.reduce(1, "feature/ENG-1-base", "main")
        serialised = json.dumps(record, default=str)
        self.assertNotIn("feature/ENG-1-base", serialised)
        self.assertNotIn("refs/heads", serialised)


class VerifyRefTests(unittest.TestCase):
    def test_a_branch_name_in_a_ref_fails_verify(self):
        """The contract is re-asserted by --verify, not just by the reducer."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.duckdb"
            con = connect(path)
            try:
                ensure_schema(con)
                write_objects(con, [{
                    "object_type": "PR",
                    "object_id": "PR:ENG/platform#1",
                    "repo": "ENG/platform",
                    "pr_id": 1,
                    "ticket_keys": ["ENG-1"],
                    "actor": "AdLo",
                    "state": "open",
                    "created_at": AS_OF,
                    "closed_at": None,
                    "head_commit": "a" * 40,
                    "message_class": None,
                    "attributes": {
                        "source_ref": digest("feature/ENG-1-base"),
                        "target_ref": "DEL-101-add-login",
                    },
                }])
                write_run(con, {
                    "run_id": "fixture-verify-ref",
                    "source": "fixture",
                    "as_of": AS_OF,
                    "source_watermark": AS_OF,
                    "classifier_version": "deterministic-v1",
                    "prs_seen": 1,
                    "prs_kept": 1,
                    "prs_dropped": 0,
                    "events_written": 0,
                    "events_skipped": 0,
                })
                report = verify(con)
            finally:
                con.close()
            self.assertFalse(report["ok"])
            self.assertTrue(
                any("target_ref" in problem for problem in report["problems"]),
                report["problems"],
            )


if __name__ == "__main__":
    unittest.main()

"""Reducer + fixture shape tests against real Bitbucket DC 9.x payloads.

The QA report found the suite green while the reviewer dimension was empty in
production, because the fixtures invented an action name (``REVIEWER_UPDATED``)
that DC never sends. These tests pin the *real* shapes, taken from a DC 9.4
instance:

* reviewer roster changes: ``action=UPDATED`` + ``addedReviewers`` /
  ``removedReviewers``;
* a rescope: ``RESCOPED`` + ``fromHash`` / ``toHash`` / ``previousFromHash`` /
  ``previousToHash``;
* a completed review: ``REVIEWED``;
* a merge: ``MERGED`` + ``commit``;
* a comment thread: anchor on the *activity* (``commentAnchor``), resolution on
  the comment (``threadResolved`` / ``resolvedDate`` / ``resolver``).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ingest import classify, fixtures
from ingest.bitbucket import PullRequest
from ingest.config import Config
from ingest.pipeline import KeyFilter, Reducer, Stats
from ingest.pseudonym import IdentityTable
from ingest.taxonomy import PR_ACTIVITY_MAP

AS_OF = datetime(2026, 11, 15, 12, 0, 0)
T0 = 1_790_000_000_000  # epoch ms
HASH_A = "a" * 40
HASH_B = "b" * 40
HASH_C = "c" * 40

GRACE = {"name": "grace", "displayName": "Grace Hopper", "type": "NORMAL"}
ALAN = {"name": "alan", "displayName": "Alan Turing", "type": "NORMAL"}
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}
#: A service-shaped account name: ``classify_actor`` reads it as a bot without
#: any configuration (the DC account type is NORMAL).
_CI_BOT = {"name": "ci-bot", "displayName": "CI", "type": "NORMAL"}


def _activity(activity_id: int, action: str, offset: int, user: dict | None = None, **extra) -> dict:
    payload = {"id": activity_id, "createdDate": T0 + offset * 60_000, "action": action, **extra}
    if user is not None:
        payload["user"] = user
    return payload


def _pr(activities: list[dict], commits: list[dict] | None = None) -> PullRequest:
    pr = {
        "id": 1,
        "state": "OPEN",
        "createdDate": T0,
        "fromRef": {"latestCommit": HASH_A, "displayId": "feature/PROJ-1-thing"},
        "author": {"user": ADA},
    }
    pull = PullRequest("ENG", "platform", pr, commits or [])
    pull.activities = activities
    return pull


class ReducerShapeTests(unittest.TestCase):
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

    def reduce(self, pr: PullRequest) -> list[dict]:
        self.reducer.reduce(pr, ["PROJ-1"], pr.activities, self.stats)
        return self.stats.events

    def test_updated_action_produces_reviewer_changes(self):
        pr = _pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "UPDATED", 5, ADA, addedReviewers=[GRACE], removedReviewers=[]),
            _activity(3, "UPDATED", 10, ADA, addedReviewers=[], removedReviewers=[GRACE]),
        ])
        events = self.reduce(pr)
        reviewer_events = [e for e in events if e["activity"] == "reviewer_changed"]
        self.assertEqual(
            sorted(e["attributes"]["change"] for e in reviewer_events),
            ["added", "removed"],
        )
        # the touched reviewer is a first-class object on the event
        objects = {
            oid
            for key, _type, oid in self.stats.relations
            if key == reviewer_events[0]["event_key"]
        }
        self.assertTrue(any(o.startswith("PERSON:") for o in objects), objects)

    def test_updated_with_hashes_is_a_push(self):
        pr = _pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(
                2, "UPDATED", 5, ADA,
                fromHash=HASH_B, toHash=HASH_A,
                previousFromHash=HASH_A, previousToHash=HASH_A,
            ),        ])
        events = self.reduce(pr)
        pushes = [e for e in events if e["activity"] == "pr_pushed"]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]["attributes"]["commits_added"], 0)

    def test_reviewed_is_a_reviewer_state_change(self):
        pr = _pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "REVIEWED", 5, GRACE),
        ])
        events = self.reduce(pr)
        reviewed = [e for e in events if e["attributes"].get("change") == "reviewed"]
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(reviewed[0]["activity"], "reviewer_changed")

    def test_merged_activity_records_the_merge_commit(self):
        merge_commit = {"id": HASH_C, "message": "Merge pull request #1 in ENG/platform",
                        "authorTimestamp": T0 + 60_000}
        pr = _pr([_activity(1, "OPENED", 0, ADA), _activity(2, "MERGED", 1, GRACE, commit=merge_commit)])
        events = self.reduce(pr)
        merged = [e for e in events if e["activity"] == "pr_merged"][0]
        self.assertTrue(merged["attributes"]["review_rounds"] >= 0)
        self.assertIn(f"COMMIT:{HASH_C}", [oid for key, _t, oid in self.stats.relations if key == merged["event_key"]])
        commit_objects = [o for o in self.stats.objects if o["object_id"] == f"COMMIT:{HASH_C}"]
        self.assertEqual(len(commit_objects), 1)
        self.assertEqual(commit_objects[0]["message_class"], "merge")

    def test_comment_anchor_lives_on_the_activity(self):
        comment = {
            "id": 101,
            "text": "zzcanary-deadbeef please look",
            "author": GRACE,
            "severity": "BLOCKER",
            "state": "RESOLVED",
            "threadResolved": True,
            "comments": [{"id": 102, "text": "zzcanary-reply", "author": ADA}],
        }
        pr = _pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(
                2, "COMMENTED", 5, GRACE,
                comment=comment,
                commentAction="ADDED",
                commentAnchor={"path": "src/main.py", "line": 7},
            ),
        ])
        events = self.reduce(pr)
        comment_events = [e for e in events if e["activity"] == "comment_added"]
        # prd/76: the opener plus one event for the nested reply.
        self.assertEqual(len(comment_events), 2)
        opener = [e for e in comment_events if not e["attributes"].get("reply")]
        reply_events = [e for e in comment_events if e["attributes"].get("reply")]
        self.assertEqual(len(opener), 1)
        self.assertEqual(len(reply_events), 1)
        attributes = opener[0]["attributes"]
        self.assertEqual(attributes["anchor_class"], "file")
        self.assertEqual(attributes["thread_state"], "resolved")
        self.assertEqual(attributes["replies"], 1)
        self.assertEqual(reply_events[0]["attributes"]["reply_index"], 1)
        self.assertEqual(reply_events[0]["attributes"]["parent_comment_id"], 101)
        # raw comment text is never stored, not even hashed into attributes
        for event in comment_events:
            self.assertNotIn("zzcanary", json.dumps(event["attributes"]))


class FixtureShapeTests(unittest.TestCase):
    """The committed/generated bundle must speak DC 9.x, not a private dialect."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = fixtures.generate(n=24, seed=20261115)
        cls.acts = [
            activity
            for project in cls.bundle["projects"]
            for repo in project["repos"]
            for record in repo["pull_requests"]
            for activity in record["activities"]
        ]

    def test_only_real_dc_actions(self):
        actions = {a["action"] for a in self.acts}
        self.assertTrue(actions.issubset(set(PR_ACTIVITY_MAP)), sorted(actions - set(PR_ACTIVITY_MAP)))
        self.assertNotIn("REVIEWER_UPDATED", actions)

    def test_rescope_carries_real_hash_fields(self):
        rescopes = [a for a in self.acts if a["action"] == "RESCOPED"]
        self.assertTrue(rescopes)
        for activity in rescopes:
            for field in ("fromHash", "toHash", "previousFromHash", "previousToHash"):
                self.assertRegex(activity[field], r"^[0-9a-f]{40}$", field)
            self.assertIn("commits", activity["added"])

    def test_merge_carries_commit_and_review_carries_no_invention(self):
        merges = [a for a in self.acts if a["action"] == "MERGED"]
        self.assertTrue(merges)
        self.assertTrue(all((a.get("commit") or {}).get("id") for a in merges))
        updates = [a for a in self.acts if a["action"] == "UPDATED"]
        self.assertTrue(updates)
        for activity in updates:
            self.assertTrue("addedReviewers" in activity and "removedReviewers" in activity)

    def test_comment_threads_are_resolvable(self):
        comments = [a["comment"] for a in self.acts if a["action"] == "COMMENTED"]
        self.assertTrue(comments)
        resolved = [c for c in comments if c["threadResolved"]]
        self.assertTrue(resolved, "fixtures must contain resolved threads")
        for comment in resolved:
            self.assertEqual(comment["state"], "RESOLVED")
            self.assertIsNotNone(comment["resolvedDate"])
            self.assertIsNotNone(comment["resolver"])


class CommentKindTests(unittest.TestCase):
    """prd/75: review / CI / bot / admin, decided before the model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform")],
            bot_aliases=("ape",),
        )
        self.classifier = _Forced("nit")
        self.reducer = Reducer(
            self.cfg,
            IdentityTable(self.cfg.identity_path),
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape, self.cfg.ticket_projects),
            AS_OF,
            self.classifier,
        )
        self.stats = Stats()

    def tearDown(self):
        self.tmp.cleanup()

    def reduce(self, pr: PullRequest) -> dict:
        self.reducer.reduce(pr, ["PROJ-1"], pr.activities, self.stats)
        thread = next(
            o for o in self.stats.objects
            if o["object_type"] == "THREAD" and o["object_id"].endswith(":101")
        )
        return json.loads(thread["attributes"]) if isinstance(thread["attributes"], str) else thread["attributes"]

    def _comment(self, text, author, replies=()):
        return {
            "id": 101,
            "text": text,
            "author": author,
            "state": "OPEN",
            "comments": list(replies),
        }

    def test_ci_coverage_is_a_notice_and_never_calls_the_model(self):
        attrs = self.reduce(_pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(
                2, "COMMENTED", 5, _CI_BOT,
                comment=self._comment(
                    "Partial code coverage [report](http://x).\n"
                    "Coverage info for the first build:\n\ninstruction coverage is 0%",
                    _CI_BOT,
                ),
                commentAction="ADDED",
                commentAnchor={"path": "a.py", "line": 1},
            ),
        ]))
        self.assertEqual(attrs["comment_kind"], "ci_notice")
        self.assertEqual(attrs["ci_template"], "coverage")
        self.assertEqual(attrs["coverage_min"], 0)
        self.assertTrue(attrs["coverage_zero"])
        self.assertNotIn("comment_type", attrs)
        self.assertEqual(self.classifier.seen, [])

    def test_qodana_finding_carries_the_count(self):
        attrs = self.reduce(_pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(
                2, "COMMENTED", 5, _CI_BOT,
                comment=self._comment(
                    "#### ⚠️ Qodana detected 1021 new issue(s)\n\n"
                    "Please do not merge without solving/suppressing",
                    _CI_BOT,
                ),
                commentAction="ADDED",
            ),
        ]))
        self.assertEqual(attrs["comment_kind"], "ci_finding")
        self.assertEqual(attrs["ci_template"], "qodana_issues")
        self.assertEqual(attrs["ci_issues"], 1021)
        self.assertNotIn("comment_type", attrs)

    def test_admin_note_is_not_sent_to_the_model(self):
        attrs = self.reduce(_pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(
                2, "COMMENTED", 5, GRACE,
                comment=self._comment("Rebased https://x/commits/abc", GRACE),
                commentAction="ADDED",
            ),
        ]))
        self.assertEqual(attrs["comment_kind"], "admin")
        self.assertNotIn("comment_type", attrs)
        self.assertEqual(self.classifier.seen, [])

    def test_review_remark_is_labeled_from_the_opener(self):
        attrs = self.reduce(_pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(
                2, "COMMENTED", 5, GRACE,
                comment=self._comment("please rename this", GRACE),
                commentAction="ADDED",
                commentAnchor={"path": "a.py", "line": 1},
            ),
        ]))
        self.assertEqual(attrs["comment_kind"], "review")
        self.assertEqual(attrs["label_source"], "opener")
        self.assertEqual(attrs["comment_type"], "nit")
        self.assertEqual(attrs["classifier_version"], "forced-v1")

    def test_bot_thread_labels_the_first_human_reply(self):
        attrs = self.reduce(_pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(
                2, "COMMENTED", 5, _CI_BOT,
                comment=self._comment(
                    "Partial code coverage [report](http://x).\n"
                    "Coverage info for the first build:",
                    _CI_BOT,
                    replies=[{"id": 102, "text": "the report is stale", "author": GRACE}],
                ),
                commentAction="ADDED",
            ),
        ]))
        self.assertEqual(attrs["comment_kind"], "ci_notice")
        self.assertEqual(attrs["label_source"], "reply")
        self.assertEqual(attrs["comment_type"], "nit")
        self.assertEqual(len(self.classifier.seen), 1)
        self.assertEqual(self.classifier.seen[0][0], "the report is stale")

    # -- prd/78: the deterministic no-fit filter (openers only) -------------

    def _reduce_fresh(self, pr: PullRequest) -> tuple[dict, Stats]:
        stats = Stats()
        self.reducer.reduce(pr, ["PROJ-1"], pr.activities, stats)
        thread = next(
            o for o in stats.objects
            if o["object_type"] == "THREAD" and o["object_id"].endswith(":101")
        )
        attrs = thread["attributes"]
        if isinstance(attrs, str):
            attrs = json.loads(attrs)
        return attrs, stats

    def test_notice_opener_is_admin_and_never_calls_the_model(self):
        for text in (
            "please review",
            "LGTM",
            "+1",
            "closing this",
            "fixed in abc1234",
            "will add the fix",
            "merged master",
            "fyi",
            "https://x/overview?commentId=1045138",
        ):
            with self.subTest(text=text):
                self.classifier.seen.clear()
                attrs, stats = self._reduce_fresh(_pr([
                    _activity(1, "OPENED", 0, ADA),
                    _activity(
                        2, "COMMENTED", 5, GRACE,
                        comment=self._comment(text, GRACE),
                        commentAction="ADDED",
                        commentAnchor={"path": "a.py", "line": 1},
                    ),
                ]))
                self.assertEqual(attrs["comment_kind"], "admin", text)
                self.assertNotIn("comment_type", attrs, text)
                self.assertEqual(self.classifier.seen, [], text)
                # the dialogue event stays in the log; only the label is withheld
                events = [e for e in stats.events if e["activity"] == "comment_added"]
                self.assertEqual(len(events), 1, text)

    def test_a_suggestion_that_looks_like_a_request_stays_a_review_remark(self):
        attrs, _stats = self._reduce_fresh(_pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(
                2, "COMMENTED", 5, GRACE,
                comment=self._comment(
                    "can we also show the count in the message?", GRACE
                ),
                commentAction="ADDED",
                commentAnchor={"path": "a.py", "line": 1},
            ),
        ]))
        self.assertEqual(attrs["comment_kind"], "review")
        self.assertEqual(attrs["comment_type"], "nit")

    def test_a_status_note_in_a_reply_does_not_admin_the_opener(self):
        # prd/78 "replies are exempt": "fixed in the next push" is the outcome
        # evidence for the thread, not a no-fit opener.
        attrs, stats = self._reduce_fresh(_pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(
                2, "COMMENTED", 5, GRACE,
                comment=self._comment(
                    "the cache key is missing the model name",
                    GRACE,
                    replies=[{
                        "id": 102,
                        "text": "fixed in the next push",
                        "author": ADA,
                    }],
                ),
                commentAction="ADDED",
                commentAnchor={"path": "a.py", "line": 1},
            ),
        ]))
        self.assertEqual(attrs["comment_kind"], "review")
        self.assertEqual(attrs["label_source"], "opener")
        self.assertEqual(attrs["comment_type"], "nit")
        self.assertEqual(
            [text for text, *_ in self.classifier.seen],
            ["the cache key is missing the model name"],
        )
        replies = [
            e for e in stats.events
            if e["activity"] == "comment_added" and e["attributes"].get("reply")
        ]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["attributes"]["parent_comment_id"], 101)

    def test_a_status_note_reply_on_a_bot_thread_is_still_classified(self):
        # The bot-thread review-label skip stays is_admin-only: a status note
        # reply must still reach the model (prd/78 / prd/75 intent).
        attrs, _stats = self._reduce_fresh(_pr([
            _activity(1, "OPENED", 0, ADA),
            _activity(
                2, "COMMENTED", 5, _CI_BOT,
                comment=self._comment(
                    "Partial code coverage [report](http://x).\n"
                    "Coverage info for the first build:",
                    _CI_BOT,
                    replies=[{
                        "id": 102,
                        "text": "fixed in the next push",
                        "author": GRACE,
                    }],
                ),
                commentAction="ADDED",
            ),
        ]))
        self.assertEqual(attrs["comment_kind"], "ci_notice")
        self.assertEqual(attrs["label_source"], "reply")
        self.assertEqual(attrs["comment_type"], "nit")
        self.assertEqual(
            [text for text, *_ in self.classifier.seen],
            ["fixed in the next push"],
        )


class _Forced(classify.MockClassifier):
    backend = "forced"
    version = "forced-v1"

    def __init__(self, comment_type: str):
        super().__init__()
        self.forced = comment_type
        self.seen: list[tuple] = []

    def classify(self, text, kind, context=None):
        self.seen.append((text, kind, context))
        return {**super().classify(text, kind, context), "comment_type": self.forced}


if __name__ == "__main__":
    unittest.main()

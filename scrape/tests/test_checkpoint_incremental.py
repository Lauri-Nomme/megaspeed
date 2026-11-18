"""Incremental scrape checkpoints (prd/33 §1): O(delta), same crash guarantee.

A checkpoint used to re-serialise the **whole** bundle (gzip+tar of everything so
far) every ``--checkpoint-every`` PRs -- 4,000 PRs took 45 s with checkpoints vs
2.96 s without, the signature of O(N²). It now stages only the rows added since
the last checkpoint in ``<out>.parts/`` and the tgz is written once, at the end.

These tests pin the three things that matter:

1. **The write is O(delta)**: with a checkpoint after *every* PR, the total staged
   bytes stay within one full serialisation of the bundle (the old code would
   have written it N times), and no single checkpoint grows with the prefix.
2. **The crash-resume guarantee is unchanged**: a crash mid-run still leaves a
   readable checkpoint, and the next run resumes from it, re-reading nothing it
   already paid for, and the final bundle is the union.
3. **The artifact is unchanged**: the tgz a checkpointed run produces holds the
   same rows as the same run without checkpoints.

    python3 -m unittest discover -s scrape/tests -v
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ingest import bundle as bundle_mod
from ingest import jira_fixtures
from ingest.bundle import read_bundle, read_inflight, rows_for_events, rows_for_objects
from ingest.config import Config
from ingest.progress import ProgressReporter
from ingest.scrape import scrape
from ingest.scrape_jira import scrape as scrape_jira
from ingest.transport import FixtureJiraTransport, FixtureTransport

T0 = 1_790_000_000_000  # epoch ms
MINUTE = 60_000
HASH = "a" * 40
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}
NO_CUTOFF = 24 * 365  # wide enough to keep every re-scraped event
PR_COUNT = 40


def _record(pr_id: int) -> dict:
    """One MERGED fixture PR shaped like the DC REST payloads."""
    stamp = T0 + pr_id * MINUTE
    key = f"DEL-{pr_id}"
    activities = [
        {"id": pr_id * 100 + 1, "createdDate": stamp, "action": "OPENED", "user": ADA},
        {"id": pr_id * 100 + 2, "createdDate": stamp + MINUTE, "action": "APPROVED",
         "user": ADA},
        {"id": pr_id * 100 + 3, "createdDate": stamp + 2 * MINUTE, "action": "MERGED",
         "user": ADA,
         "commit": {"id": f"{pr_id:040d}", "author": ADA, "committer": ADA,
                    "authorTimestamp": stamp, "committerTimestamp": stamp,
                    "message": f"{key} merge the thing"}},
    ]
    return {
        "pull_request": {
            "id": pr_id,
            "version": 1,
            "title": f"{key}: thing {pr_id}",
            "description": "zzcanary description",
            "state": "MERGED",
            "open": False,
            "closed": True,
            "draft": False,
            "createdDate": stamp,
            "updatedDate": stamp + 2 * MINUTE,
            "closedDate": stamp + 2 * MINUTE,
            "fromRef": {"id": f"refs/heads/feature/{key}", "displayId": f"feature/{key}",
                        "type": "BRANCH", "latestCommit": HASH},
            "toRef": {"id": "refs/heads/main", "displayId": "main", "type": "BRANCH",
                      "latestCommit": HASH},
            "author": {"user": ADA, "role": "AUTHOR", "approved": False},
            "reviewers": [],
            "participants": [],
            "properties": {},
        },
        "activities": activities,
        "commits": [{
            "id": f"{pr_id:040d}", "displayId": f"{pr_id:040d}"[:7], "author": ADA,
            "authorTimestamp": stamp, "committer": ADA, "committerTimestamp": stamp,
            "message": f"{key} add the thing",
        }],
    }


def _bundle(count: int = PR_COUNT) -> dict:
    records = [_record(pr_id) for pr_id in range(1, count + 1)]
    records.sort(key=lambda record: record["pull_request"]["updatedDate"], reverse=True)
    return {
        "schema": "megaspeed.fixture/1",
        "projects": [{"key": "DEL", "repos": [{"slug": "huge", "pull_requests": records}]}],
    }


class Harness:
    """Forwards to the fixture transport and can die on one PR's commits."""

    def __init__(self, bundle: dict, *, crash_on_commits: int | None = None):
        self.inner = FixtureTransport(bundle)
        self.crash_on_commits = crash_on_commits
        self.commits: set[int] = set()

    def get(self, path: str, params: dict | None = None) -> dict:
        if path.endswith("/commits"):
            pr_id = int(path.split("/")[-2])
            self.commits.add(pr_id)
            if pr_id == self.crash_on_commits:
                raise RuntimeError("simulated crash (kill -9) mid-scrape")
        return self.inner.get(path, params)


def _cfg(root: Path) -> Config:
    return Config(
        db_path=root / "unused.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[("DEL", "huge")],
        ticket_projects=("DEL",),
    )


def _raw_size(bundle) -> int:
    """Bytes of one full serialisation of the bundle's rows (the O(1) baseline)."""
    return len(bundle_mod._jsonl(rows_for_events(bundle.events, bundle.relations))) + len(
        bundle_mod._jsonl(rows_for_objects(bundle.objects))
    )


class IncrementalCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _cfg(self.root)
        self.out = self.root / "export" / "huge.tgz"
        self.state_dir = self.root / "export" / "huge.state"
        self.bundle = _bundle()
        self.staged: list[dict] = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_scrape(self, transport=None, *, out: Path | None = None,
                   state_dir: Path | None = None, **kwargs) -> dict:
        return scrape(
            self.cfg,
            transport or Harness(self.bundle),
            "fixture",
            out=out or self.out,
            state_dir=state_dir or self.state_dir,
            overlap_hours=NO_CUTOFF,
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
            **kwargs,
        )

    def record_staged(self):
        """Record what each checkpoint staged (the delta, per call)."""
        original = bundle_mod.BundleSpool.append

        def counting(spool, **kwargs):
            written = original(spool, **kwargs)
            self.staged.append(written)
            return written

        return mock.patch.object(bundle_mod.BundleSpool, "append", counting)

    # -- 1. the checkpoint write is O(delta), not O(prefix) -----------------
    def test_a_checkpoint_after_every_pr_stages_only_that_pr(self):
        with self.record_staged():
            report = self.run_scrape(checkpoint_every=1)

        self.assertEqual(report["prs_kept"], PR_COUNT)
        # One checkpoint per PR, plus the repo-boundary one (which stages no
        # rows: its ``bytes`` is 0).
        self.assertIn(len(self.staged), (PR_COUNT, PR_COUNT + 1))
        self.assertEqual(self.staged[-1]["bytes"], 0)
        final = read_bundle(self.out)
        raw = _raw_size(final)
        staged = sum(entry["bytes"] for entry in self.staged)
        largest = max(entry["bytes"] for entry in self.staged)
        # Every row is staged exactly once: the whole run writes about one
        # serialisation, not one *per checkpoint* (the old O(N²) shape).
        self.assertLessEqual(staged, raw * 1.25, f"staged {staged} vs one copy {raw}")
        self.assertGreater(staged, raw * 0.5, "the rows were not staged at all")
        # No single checkpoint re-writes the prefix: the biggest one is a couple
        # of PRs' worth, never the bundle so far.
        self.assertLess(largest, raw / 4, f"largest checkpoint {largest} of {raw}")

    def test_the_checkpoint_cost_does_not_grow_with_the_prefix(self):
        with self.record_staged():
            self.run_scrape(checkpoint_every=2)
        staged = [entry["bytes"] for entry in self.staged]
        self.assertGreater(len(staged), 10)
        first, last = staged[0], staged[-1]
        # Same fixture per PR, so the last checkpoint costs what the first one
        # did -- the whole point of the fix (it used to be ~N times bigger).
        self.assertLess(last, first * 1.5, staged)
        self.assertLess(max(staged), sum(staged) / 4, staged)

    # -- 2. the crash-resume guarantee is unchanged -------------------------
    def test_a_crash_leaves_a_readable_checkpoint_and_the_resume_carries_it(self):
        # NEWEST-first, so PRs 40, 39 are read first and the crash lands on 38's
        # commits -- two PRs after the last checkpoint (checkpoint_every=2).
        with self.assertRaises(RuntimeError):
            self.run_scrape(Harness(self.bundle, crash_on_commits=38), checkpoint_every=2)

        # No tgz yet: the in-flight artifact is the incremental spool.
        self.assertFalse(self.out.exists())
        checkpoint = read_inflight(self.out)
        self.assertEqual({event["pr_id"] for event in checkpoint.events}, {40, 39})
        self.assertTrue(checkpoint.manifest["run"]["checkpoint"])
        state = json.loads((self.state_dir / "watermark.json").read_text())
        self.assertIs(state["sources"]["fixture"]["complete"], False)
        cursor = json.loads((self.state_dir / "prs.json").read_text())["sources"]["fixture"]
        self.assertEqual(set(cursor), {"DEL/huge#39", "DEL/huge#40"})

        resume = Harness(self.bundle)
        report = self.run_scrape(resume, checkpoint_every=2)

        self.assertTrue(report["resumed"])
        self.assertEqual(report["prs_skipped_resumed"], 2)
        self.assertEqual(report["events_carried"], len(checkpoint.events))
        self.assertFalse({39, 40} & resume.commits)
        final = read_bundle(self.out)
        self.assertEqual(
            {event["pr_id"] for event in final.events}, set(range(1, PR_COUNT + 1))
        )
        # The staged parts were consumed: the completed run leaves no spool.
        self.assertFalse(Path(report["checkpoint_spool"]).exists())

    def test_a_second_crash_keeps_every_checkpoint_of_the_first(self):
        """A resume must continue the part numbering, not overwrite the spool."""
        with self.assertRaises(RuntimeError):
            self.run_scrape(Harness(self.bundle, crash_on_commits=38), checkpoint_every=2)
        first = read_inflight(self.out)
        with self.assertRaises(RuntimeError):
            # Resume, then die again on a PR that was not checkpointed yet.
            self.run_scrape(Harness(self.bundle, crash_on_commits=30), checkpoint_every=2)
        second = read_inflight(self.out)
        first_keys = {event["event_key"] for event in first.events}
        second_keys = {event["event_key"] for event in second.events}
        self.assertLessEqual(first_keys, second_keys)
        self.assertGreater(len(second_keys), len(first_keys))

    # -- 3. the artifact is unchanged ---------------------------------------
    def test_the_bundle_matches_a_run_without_checkpoints(self):
        self.run_scrape(checkpoint_every=1)
        with_checkpoints = read_bundle(self.out)
        without = self.run_scrape(
            out=self.root / "export" / "plain.tgz",
            state_dir=self.root / "export" / "plain.state",
            checkpoint_every=0,
        )
        plain = read_bundle(self.root / "export" / "plain.tgz")
        self.assertEqual(without["events"], len(plain.events))
        self.assertEqual(
            [(e["event_key"], e["ts"], e["activity"], e["objects"]) for e in plain.events],
            [(e["event_key"], e["ts"], e["activity"], e["objects"])
             for e in with_checkpoints.events],
        )
        self.assertEqual(
            sorted(o["object_id"] for o in plain.objects),
            sorted(o["object_id"] for o in with_checkpoints.objects),
        )
        self.assertEqual(plain.manifest["counts"], with_checkpoints.manifest["counts"])

    def test_a_stale_spool_never_leaks_into_a_fresh_run(self):
        """A dead run's staged rows are discarded when the next run starts over."""
        with self.assertRaises(RuntimeError):
            self.run_scrape(Harness(self.bundle, crash_on_commits=38), checkpoint_every=2)
        spool = bundle_mod.spool_dir(self.out)
        self.assertTrue(spool.is_dir())
        report = self.run_scrape(full=True, checkpoint_every=2)
        self.assertFalse(report["resumed"])
        self.assertFalse(spool.exists())
        final = read_bundle(self.out)
        self.assertEqual(len(final.events), len({e["event_key"] for e in final.events}))
        self.assertEqual(
            {event["pr_id"] for event in final.events}, set(range(1, PR_COUNT + 1))
        )

    def test_a_partial_tgz_from_an_older_build_still_resumes(self):
        """Backwards compatibility: a pre-spool checkpoint is still readable."""
        checkpoint = read_inflight  # the reader the resume uses
        events = [_record(pr_id) for pr_id in (40, 39)]
        bundle = _bundle()
        # Build a partial tgz exactly the way the old code did: two PRs reduced.
        partial_out = self.out
        self.run_scrape(
            Harness({"schema": "megaspeed.fixture/1", "projects": [
                {"key": "DEL", "repos": [{"slug": "huge", "pull_requests": events}]}
            ]}),
            out=partial_out,
        )
        self.assertTrue(partial_out.is_file())
        # Rewind the cursors to "in flight" and drop the spool: that is what a
        # state dir written by the previous build looks like.
        state_path = self.state_dir / "watermark.json"
        state = json.loads(state_path.read_text())
        entry = state["sources"]["fixture"]
        entry["complete"] = False
        entry["runs"] = 0
        state_path.write_text(json.dumps(state))
        self.assertEqual(checkpoint(partial_out).path, partial_out)

        report = self.run_scrape(checkpoint_every=2)
        self.assertTrue(report["resumed"])
        self.assertEqual(report["resumed_from"], str(partial_out))
        self.assertGreater(report["events_carried"], 0)
        final = read_bundle(self.out)
        self.assertEqual(
            {event["pr_id"] for event in final.events}, set(range(1, PR_COUNT + 1))
        )


class JiraIncrementalCheckpointTests(unittest.TestCase):
    """The Jira walk has the identical checkpoint shape (prd/33 §1)."""

    SOURCE = "jira-fixture"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = jira_fixtures.generate()
        self.cfg = Config(
            db_path=self.root / "unused.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[],
            jira_projects=("PROJ",),
            ticket_projects=("PROJ",),
        )
        self.out = self.root / "export" / "jira.tgz"
        self.state_dir = self.root / "export" / "jira.state"

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, *, out: Path | None = None, state_dir: Path | None = None,
               **kwargs) -> dict:
        return scrape_jira(
            self.cfg,
            FixtureJiraTransport(self.fixture),
            self.SOURCE,
            out=out or self.out,
            state_dir=state_dir or self.state_dir,
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
            **kwargs,
        )

    def test_a_jira_checkpoint_stages_only_the_delta(self):
        staged: list[int] = []
        original = bundle_mod.BundleSpool.append

        def counting(spool, **kwargs):
            written = original(spool, **kwargs)
            staged.append(written["bytes"])
            return written

        with mock.patch.object(bundle_mod.BundleSpool, "append", counting):
            report = self.ingest(checkpoint_every=1)
        self.assertGreater(report["issues_kept"], 5)
        # One per issue, plus the project-boundary checkpoint (0 row bytes).
        self.assertIn(len(staged), (report["issues_kept"], report["issues_kept"] + 1))
        self.assertEqual(staged[-1], 0)
        raw = _raw_size(read_bundle(self.out))
        self.assertLessEqual(sum(staged), raw * 1.25, f"staged {sum(staged)} of {raw}")
        self.assertLess(max(staged), raw / 2)

    def test_the_jira_bundle_matches_a_run_without_checkpoints(self):
        self.ingest(checkpoint_every=1)
        with_checkpoints = read_bundle(self.out)
        self.ingest(out=self.root / "export" / "plain.tgz",
                    state_dir=self.root / "export" / "plain.state",
                    checkpoint_every=0)
        plain = read_bundle(self.root / "export" / "plain.tgz")
        self.assertEqual(
            [(e["event_key"], e["ts"], e["activity"]) for e in plain.events],
            [(e["event_key"], e["ts"], e["activity"]) for e in with_checkpoints.events],
        )
        self.assertEqual(
            sorted(o["object_id"] for o in plain.objects),
            sorted(o["object_id"] for o in with_checkpoints.objects),
        )


if __name__ == "__main__":
    unittest.main()

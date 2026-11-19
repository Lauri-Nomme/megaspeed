"""Scrape-reliability regressions (the DEL scrape that died three times, 2 h).

Five bugs, five tests -- each one fails without its fix:

1. ``transport.py`` did not retry ``ConnectionResetError``/``BrokenPipeError``
   (a reset from ``http.client.getresponse()`` is a plain ``OSError``), so a
   single reset escaped and killed the run.
2. A crash lost everything: the bundle and the cursors were only written at the
   end, so the next run re-scraped from scratch. Now the run checkpoints and a
   later run resumes from the checkpoint.
3. A 404 on ``/pull-requests/{id}/commits`` raised ``BitbucketError`` out of the
   walk (exit 2). Now one bad PR is skipped and counted, never aborting the run.
4. A run that saw only a handful of PRs still wrote a "successful" bundle and
   exited 0. Now it warns and exits non-zero (``--force``/``--allow-partial``
   overrides).
5. A scrape could race an in-flight corpus rebuild. The quiescence precondition
   is documented and a cheap guard notices a repo that moved mid-walk.

    python3 -m unittest discover -s scrape/tests -v
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from ingest import scrape as scrape_module
from ingest.bundle import read_bundle, read_inflight
from ingest.config import Config
from ingest.progress import ProgressReporter
from ingest.scrape import scrape
from ingest.scrape_cli import main
from ingest.transport import BitbucketError, FixtureTransport, HttpTransport

T0 = 1_790_000_000_000  # epoch ms
MINUTE = 60_000
HASH = "a" * 40
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}
NO_CUTOFF = 24 * 365  # wide enough to keep every re-scraped event

_PR_PATH = re.compile(r"/pull-requests(?:/(\d+)(?:/(activities|commits))?)?$")


def _kind(path: str) -> str:
    if path.startswith("/rest/build-status/"):
        return "build-status"
    match = _PR_PATH.search(path)
    assert match, f"unexpected path {path}"
    if match.group(1) is None:
        return "list"
    return match.group(2) or "detail"


def _pr_id(path: str) -> int | None:
    match = _PR_PATH.search(path)
    return int(match.group(1)) if match and match.group(1) else None


def _record(pr_id: int, *, key: str | None = None, minute: int | None = None) -> dict:
    """One MERGED fixture PR shaped like the DC REST payloads."""
    key = f"DEL-{pr_id}" if key is None else key
    stamp = T0 + (pr_id if minute is None else minute) * MINUTE
    activities = [
        {"id": pr_id * 100 + 1, "createdDate": stamp, "action": "OPENED", "user": ADA},
        {"id": pr_id * 100 + 2, "createdDate": stamp + MINUTE, "action": "MERGED",
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
            "updatedDate": stamp + MINUTE,
            "closedDate": stamp + MINUTE,
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


def _bundle(ids: list[int], *, project: str = "DEL", slug: str = "huge",
            minute=None) -> dict:
    """A repo whose PRs are ordered ``updatedDate`` descending (``order=NEWEST``)."""
    records = [
        _record(pr_id, minute=(minute(pr_id) if minute else None)) for pr_id in ids
    ]
    records.sort(key=lambda record: record["pull_request"]["updatedDate"], reverse=True)
    return {
        "schema": "megaspeed.fixture/1",
        "projects": [{"key": project, "repos": [{"slug": slug, "pull_requests": records}]}],
    }


class Harness:
    """Forwards to the fixture transport, records requests, injects failures."""

    def __init__(self, bundle: dict, *, fail_commits_for=(), crash_on_commits=None,
                 on_request=None):
        self.inner = FixtureTransport(bundle)
        self.fail_commits_for = set(fail_commits_for)
        self.crash_on_commits = crash_on_commits
        self.on_request = on_request
        self.paths: list[str] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.paths.append(path)
        pr_id, kind = _pr_id(path), _kind(path)
        if self.on_request is not None:
            self.on_request(path, kind, pr_id)
        if kind == "commits" and pr_id in self.fail_commits_for:
            # Exactly what HttpTransport raises for a 404 (BitbucketError).
            raise BitbucketError(f"404 Not Found for {path}")
        if kind == "commits" and self.crash_on_commits == pr_id:
            raise RuntimeError("simulated crash (kill -9) mid-scrape")
        return self.inner.get(path, params)

    # -- inspection helpers ------------------------------------------------
    def touched_prs(self, kind: str | None = None) -> set[int]:
        return {
            pr_id for path in self.paths
            if (pr_id := _pr_id(path)) is not None
            and (kind is None or _kind(path) == kind)
        }


def _cfg(root: Path, *, repos=(("DEL", "huge"),), projects=("DEL",)) -> Config:
    return Config(
        db_path=root / "unused.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=list(repos),
        ticket_projects=projects,
    )


# --------------------------------------------------------------------------- #
# 1. the transport retries a reset/abort instead of dying
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self.payload


class TransportRetryTests(unittest.TestCase):
    """``ConnectionResetError`` is an ``OSError``, not a ``URLError``."""

    def setUp(self):
        self.transport = HttpTransport("http://bb.example", token="t", max_retries=3)
        self.path = "/rest/api/1.0/projects/DEL/repos/huge/pull-requests"

    def _run(self, errors: list[BaseException]) -> tuple[dict, list[float]]:
        """Serve ``errors`` in order, then a good response; return the result."""
        calls: list[str] = []
        sleeps: list[float] = []

        def fake_urlopen(request, timeout=None, context=None):
            calls.append(request.full_url)
            if len(calls) <= len(errors):
                raise errors[len(calls) - 1]
            return _FakeResponse(b'{"values": [], "isLastPage": true}')

        with mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch("ingest.transport.time.sleep", sleeps.append):
            return self.transport.get(self.path), sleeps

    def test_connection_reset_is_retried_then_succeeds(self):
        page, sleeps = self._run([
            ConnectionResetError(104, "Connection reset by peer"),
            ConnectionResetError(104, "Connection reset by peer"),
        ])
        self.assertEqual(page, {"values": [], "isLastPage": True})
        self.assertEqual(sleeps, [1.0, 1.5])  # the same exponential backoff

    def test_broken_pipe_is_retried_then_succeeds(self):
        page, sleeps = self._run([BrokenPipeError(32, "Broken pipe")])
        self.assertEqual(page, {"values": [], "isLastPage": True})
        self.assertEqual(sleeps, [1.0])

    def test_a_reset_that_outlives_the_retries_still_raises(self):
        with mock.patch("ingest.transport.time.sleep", lambda _s: None):
            with self.assertRaises(BitbucketError) as ctx:
                self._run([
                    ConnectionResetError(104, "reset"),
                    ConnectionResetError(104, "reset"),
                    ConnectionResetError(104, "reset"),
                ])
        self.assertIn("failed after 3 attempts", str(ctx.exception))

    def test_a_404_is_not_retried(self):
        error = urllib.error.HTTPError(
            "http://bb.example" + self.path, 404, "Not Found", {}, None
        )
        with mock.patch("ingest.transport.time.sleep", lambda _s: None):
            with self.assertRaises(BitbucketError) as ctx:
                self._run([error])
        self.assertIn("404", str(ctx.exception))


# --------------------------------------------------------------------------- #
# 2. a crash checkpoints, and the next run resumes instead of restarting
# --------------------------------------------------------------------------- #
class CheckpointResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _cfg(self.root)
        self.out = self.root / "export" / "huge.tgz"
        self.state_dir = self.root / "export" / "huge.state"
        self.bundle = _bundle([1, 2, 3, 4, 5, 6])

    def tearDown(self):
        self.tmp.cleanup()

    def run_scrape(self, transport, **kwargs) -> dict:
        return scrape(
            self.cfg, transport, "fixture",
            out=self.out, state_dir=self.state_dir,
            overlap_hours=NO_CUTOFF,
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
            **kwargs,
        )

    def test_a_crash_mid_scrape_resumes_without_re_reading_checkpointed_prs(self):
        # NEWEST-first: PRs 6 and 5 are read first, the crash lands on PR 4's
        # commits -- two PRs after the last checkpoint (checkpoint_every=2).
        with self.assertRaises(RuntimeError):
            self.run_scrape(
                Harness(self.bundle, crash_on_commits=4), checkpoint_every=2
            )

        # The crash left a *readable* in-flight checkpoint (the incremental
        # spool: no tgz is written until the run finishes) and an explicit
        # "unfinished" marker.
        self.assertFalse(self.out.exists())
        checkpoint = read_inflight(self.out)
        self.assertEqual(
            {event["pr_id"] for event in checkpoint.events}, {5, 6}
        )
        state = json.loads((self.state_dir / "watermark.json").read_text())
        self.assertIs(state["sources"]["fixture"]["complete"], False)
        self.assertEqual(state["sources"]["fixture"]["runs"], 0)  # a checkpoint is not a run
        interrupted = state["sources"]["fixture"]["last_run_id"]
        prs = json.loads((self.state_dir / "prs.json").read_text())["sources"]["fixture"]
        self.assertEqual(set(prs), {"DEL/huge#5", "DEL/huge#6"})
        self.assertEqual({entry["run_id"] for entry in prs.values()}, {interrupted})

        # The resume: no commits/activities for 5 and 6 (already checkpointed).
        resume = Harness(self.bundle)
        report = self.run_scrape(resume, checkpoint_every=2)

        self.assertTrue(report["resumed"])
        self.assertEqual(report["resumed_from"], str(self.out))
        self.assertEqual(report["prs_skipped_resumed"], 2)
        self.assertEqual(report["events_carried"], len(checkpoint.events))
        self.assertFalse({5, 6} & resume.touched_prs("commits"))
        self.assertFalse({5, 6} & resume.touched_prs("activities"))
        self.assertEqual(resume.touched_prs("commits"), {1, 2, 3, 4})

        # ... and the final bundle is the union: nothing the crash scraped is lost.
        final = read_bundle(self.out)
        self.assertEqual({event["pr_id"] for event in final.events}, {1, 2, 3, 4, 5, 6})
        self.assertFalse(final.manifest["run"]["checkpoint"])
        self.assertTrue(final.manifest["run"]["resumed"])
        state = json.loads((self.state_dir / "watermark.json").read_text())
        self.assertIs(state["sources"]["fixture"]["complete"], True)
        self.assertEqual(state["sources"]["fixture"]["runs"], 1)  # one run, not N checkpoints
        # The in-flight spool is gone once the tgz is durable.
        self.assertFalse(Path(report["checkpoint_spool"]).exists())

    def test_checkpointing_can_be_disabled(self):
        report = self.run_scrape(Harness(self.bundle), checkpoint_every=0)
        self.assertEqual(report["prs_kept"], 6)
        state = json.loads((self.state_dir / "watermark.json").read_text())
        self.assertIs(state["sources"]["fixture"]["complete"], True)


# --------------------------------------------------------------------------- #
# 3. a 404 on one PR's commits skips that PR, never the run
# --------------------------------------------------------------------------- #
class PerPullRequestFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _cfg(self.root)
        self.out = self.root / "export" / "huge.tgz"
        self.state_dir = self.root / "export" / "huge.state"
        self.bundle = _bundle([1, 2, 3, 4, 5, 6])

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_404_on_one_pr_is_skipped_while_the_others_succeed(self):
        log = io.StringIO()
        report = scrape(
            self.cfg, Harness(self.bundle, fail_commits_for={3}), "fixture",
            out=self.out, state_dir=self.state_dir, overlap_hours=NO_CUTOFF,
            progress=ProgressReporter(stream=log, interval=0),
        )

        self.assertEqual(report["prs_kept"], 5)
        self.assertEqual(report["prs_failed"], 1)
        self.assertEqual(
            [(f["repo"], f["pr_id"]) for f in report["failures"]], [("DEL/huge", 3)]
        )
        self.assertIn("404", report["failures"][0]["error"])
        # Logged (stderr), counted (report/manifest), and not fatal.
        self.assertIn("skipped DEL/huge#3", log.getvalue())
        self.assertEqual(read_bundle(self.out).manifest["counts"]["prs_failed"], 1)
        self.assertEqual(
            {event["pr_id"] for event in read_bundle(self.out).events}, {1, 2, 4, 5, 6}
        )
        # Not marked done: the next run pays for it again (and can heal the gap).
        prs = json.loads((self.state_dir / "prs.json").read_text())["sources"]["fixture"]
        self.assertNotIn("DEL/huge#3", prs)
        state = json.loads((self.state_dir / "watermark.json").read_text())
        self.assertIs(state["sources"]["fixture"]["complete"], True)

        # A second run retries exactly the PR that failed before.
        retry = Harness(self.bundle)
        again = scrape(
            self.cfg, retry, "fixture", out=self.out, state_dir=self.state_dir,
            overlap_hours=NO_CUTOFF,
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
        )
        self.assertEqual(again["prs_failed"], 0)
        self.assertIn(3, retry.touched_prs("commits"))
        # The incremental run emits the delta: the retried PR's events are in
        # the new bundle (plus the watermark-boundary tie, which is re-read).
        healed = {event["pr_id"] for event in read_bundle(self.out).events}
        self.assertIn(3, healed)
        self.assertLessEqual(healed, {1, 2, 3, 4, 5, 6})


# --------------------------------------------------------------------------- #
# 4. a suspiciously tiny bundle warns and exits non-zero (--force overrides)
# --------------------------------------------------------------------------- #
class PartialBundleGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "fixture.json"
        self.out = self.root / "export" / "tiny.tgz"
        self.state_dir = self.root / "export" / "tiny.state"
        # 30 PRs of a repo that "used to be big", ticket keys of the default
        # project set (PROJ) so no environment is needed.
        self.fixture.write_text(json.dumps(_bundle(
            list(range(1, 31)), project="ENG", slug="platform"
        )))
        self.argv = [
            "--fixture-file", str(self.fixture),
            "--repo", "ENG/platform",
            "--out", str(self.out), "--state-dir", str(self.state_dir),
            "--overlap-hours", str(NO_CUTOFF), "--quiet",
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = main([*self.argv, *extra])
        return code, stderr.getvalue()

    def test_a_tiny_run_warns_and_exits_non_zero_and_force_overrides(self):
        code, _err = self.run_cli()
        self.assertEqual(code, 0)
        state = json.loads((self.state_dir / "watermark.json").read_text())
        self.assertEqual(state["sources"]["fixture"]["repos"]["ENG/platform"]["prs_seen"], 30)
        baseline = (self.state_dir / "watermark.json").read_text()

        # The source now only exposes 5 of those 30 PRs: a bundle of 5 must not
        # look like a success (that is exactly how the DEL gap went unnoticed).
        self.fixture.write_text(json.dumps(_bundle(
            [1, 2, 3, 4, 5], project="ENG", slug="platform"
        )))
        code, err = self.run_cli()
        self.assertEqual(code, 3)
        self.assertIn("warning: ENG/platform: too few PRs listed", err)
        self.assertIn("this run looks partial", err)
        self.assertTrue(self.out.is_file())  # the bundle is still written

        # Put the 30-PR baseline back so the override runs see the same "tiny".
        (self.state_dir / "watermark.json").write_text(baseline)
        code, err = self.run_cli("--force")
        self.assertEqual(code, 0)
        self.assertIn("warning: ENG/platform: too few PRs listed", err)

        (self.state_dir / "watermark.json").write_text(baseline)
        code, _err = self.run_cli("--allow-partial")
        self.assertEqual(code, 0)


# --------------------------------------------------------------------------- #
# 5. the quiescence precondition is documented, and a moved source is noticed
# --------------------------------------------------------------------------- #
class QuiescenceGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _cfg(self.root)
        self.out = self.root / "export" / "huge.tgz"
        self.state_dir = self.root / "export" / "huge.state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_precondition_is_documented(self):
        doc = inspect.getdoc(scrape_module) or ""
        self.assertIn("quiescent", doc.lower())
        self.assertIn("corpus", doc.lower())
        self.assertIn("rebuild", doc.lower())
        self.assertIn("quiescen", (inspect.getdoc(scrape) or "").lower())

    def test_a_source_that_moves_mid_walk_is_reported(self):
        bundle = _bundle([1, 2, 3, 4])
        moved = {"done": False}

        def mutate(path: str, kind: str, pr_id: int | None) -> None:
            # A rebuild lands mid-walk and a brand-new PR appears at the top of
            # the repo: the newest updatedDate jumps past the one we opened with.
            if kind == "commits" and pr_id == 3 and not moved["done"]:
                moved["done"] = True
                bundle["projects"][0]["repos"][0]["pull_requests"].insert(
                    0, _record(99, minute=9_000)
                )

        buffer = io.StringIO()
        report = scrape(
            self.cfg, Harness(bundle, on_request=mutate), "fixture",
            out=self.out, state_dir=self.state_dir, overlap_hours=NO_CUTOFF,
            progress=ProgressReporter(stream=buffer, interval=0),
            verify_quiet=True,
        )
        self.assertTrue(moved["done"])
        self.assertEqual(report["source_moved"], ["DEL/huge"])
        self.assertIn("not quiescent", buffer.getvalue())
        self.assertEqual(read_bundle(self.out).manifest["run"]["source_moved"], ["DEL/huge"])

    def test_a_quiet_source_is_not_flagged(self):
        report = scrape(
            self.cfg, Harness(_bundle([1, 2, 3])), "fixture",
            out=self.out, state_dir=self.state_dir, overlap_hours=NO_CUTOFF,
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
            verify_quiet=True,
        )
        self.assertEqual(report["source_moved"], [])


if __name__ == "__main__":
    unittest.main()

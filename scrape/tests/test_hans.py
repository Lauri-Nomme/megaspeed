"""A hans scrape imports with the stdlib-only scraper artifact (prd/31).

The scrape side must be able to read a capture **without** DuckDB, without
credentials and without a server: the transport reads the REST dumps, the git
mirror answers the commit/diff questions, and the bundle comes out exactly as a
live scrape's would. These tests are the artifact's own (no store, no network).

    python3 -m unittest discover -s scrape/tests -v
"""

from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.hans import HansTransport
from ingest.scrape import scrape
from ingest.transport import API

T0 = 1_790_000_000_000
MINUTE = 60_000
ADA = {"name": "ada", "displayName": "Ada Lovelace", "emailAddress": "ada@example.com"}
GRACE = {"name": "grace", "displayName": "Grace Hopper", "emailAddress": "grace@example.com"}
CANARY = "zzcanary"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "Ada Lovelace",
            "GIT_AUTHOR_EMAIL": "ada@example.com",
            "GIT_COMMITTER_NAME": "Ada Lovelace",
            "GIT_COMMITTER_EMAIL": "ada@example.com",
            "GIT_AUTHOR_DATE": "2026-11-01T10:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-11-01T10:00:00+00:00",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return result.stdout.decode().strip()


def _commit(work: Path, message: str, files: dict[str, str]) -> str:
    for name, text in files.items():
        (work / name).write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD")


def build_scrape(root: Path) -> Path:
    """One open PR with a key, one merged PR without, and a real mirror."""
    work = root / "work"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    base = _commit(work, "chore: init", {"README.md": "demo\n"})
    _git(work, "checkout", "-q", "-b", "feature/one")
    head = _commit(work, "feat: widget", {"widget.py": "one\ntwo\n"})
    _git(work, "checkout", "-q", "main")
    _git(work, "checkout", "-q", "-b", "feature/two")
    docs = _commit(work, "chore: docs", {"docs.md": "a\n"})
    _git(work, "checkout", "-q", "main")
    _git(work, "merge", "-q", "--no-ff", "-m", "Merge pull request #2", "feature/two")
    merged = _git(work, "rev-parse", "HEAD")

    mirror = root / "git"
    _git(work, "init", "-q", "--bare", str(mirror))
    _git(work, "push", "-q", str(mirror), "refs/heads/*:refs/heads/*")
    _git(work, "push", "-q", str(mirror), f"{head}:refs/pull-requests/1/from")

    def pr(pr_id: int, title: str, state: str, source: str, target: str) -> dict:
        return {
            "id": pr_id,
            "title": title,
            "description": f"{CANARY} body",
            "state": state,
            "open": state == "OPEN",
            "closed": state != "OPEN",
            "createdDate": T0 + pr_id * MINUTE,
            "updatedDate": T0 + pr_id * MINUTE,
            "closedDate": T0 + pr_id * MINUTE if state != "OPEN" else None,
            "fromRef": {"displayId": source, "latestCommit": head if pr_id == 1 else docs},
            "toRef": {"displayId": "main", "latestCommit": base if pr_id == 1 else merged},
            "author": {"user": ADA},
            "reviewers": [],
            "properties": {},
        }

    one = pr(1, "DEMO-1: widget", "OPEN", "feature/one", "main")
    two = pr(2, "docs only", "MERGED", "feature/two", "main")
    rest = root / "rest"
    rest.mkdir()
    (rest / "users.json").write_text(json.dumps([ADA, GRACE]))
    (rest / "pull-requests_DEMO_widget.json").write_text(json.dumps([two, one]))
    for record, activities in (
        (one, [
            {"id": 11, "createdDate": T0, "action": "OPENED", "user": ADA},
            {"id": 12, "createdDate": T0 + MINUTE, "action": "COMMENTED", "user": GRACE,
             "comment": {"id": 100, "text": f"{CANARY} note", "severity": "NORMAL"}},
        ]),
        (two, [
            {"id": 21, "createdDate": T0 + 2 * MINUTE, "action": "OPENED", "user": ADA},
            {"id": 22, "createdDate": T0 + 3 * MINUTE, "action": "MERGED", "user": ADA,
             "commit": {"id": merged, "message": "Merge pull request #2",
                        "author": ADA, "authorTimestamp": T0 + 3 * MINUTE}},
        ]),
    ):
        (rest / f"pr_{record['id']}.json").write_text(json.dumps(record))
        (rest / f"pr_{record['id']}_activities.json").write_text(json.dumps(activities))
    (root / "index.json").write_text(json.dumps({
        "project": "DEMO", "repo": "widget", "prs": [2, 1],
        "entities": [
            {"file": "rest/pull-requests_DEMO_widget.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests?state=ALL"},
            {"file": "rest/pr_1.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests/1"},
            {"file": "rest/pr_2.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests/2"},
        ],
    }))
    return root


class HansScrapeArtifactTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.scrape_dir = build_scrape(self.root / "capture")
        self.cfg = Config(
            db_path=self.root / "unused.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("DEMO", "widget")],
            ticket_projects=("DEMO",),
            text_backend="mock",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_transport_reads_the_capture_without_a_store(self):
        transport = HansTransport(self.scrape_dir)
        rows = transport.get(f"{API}/projects/DEMO/repos/widget/pull-requests", {"state": "ALL"})
        self.assertEqual([row["id"] for row in rows["values"]], [2, 1])
        self.assertTrue(rows["isLastPage"])
        commits = transport.get(f"{API}/projects/DEMO/repos/widget/pull-requests/2/commits")
        self.assertEqual([c["message"] for c in commits["values"]], ["chore: docs"])

    def test_a_capture_scrapes_to_a_valid_bundle(self):
        transport = HansTransport(self.scrape_dir)
        report = scrape(
            self.cfg,
            transport,
            "hans",
            out=self.root / "export" / "hans.tgz",
            state_dir=self.root / "export" / "hans.state",
            text_classifier=None,
            verify_quiet=False,
        )
        self.assertEqual(report["prs_kept"], 2)   # the keyless PR imports too
        # opened + comment (PR 1) and opened + merged (PR 2); no push_class
        # without a classifier, so no pr_pushed.
        self.assertEqual(report["events"], 4)
        self.assertEqual(report["source"], "hans")
        bundle = read_bundle(self.root / "export" / "hans.tgz")
        self.assertEqual(bundle.source, "hans")
        self.assertEqual(
            {o["object_type"] for o in bundle.objects},
            {"PR", "COMMIT", "PERSON", "TICKET", "THREAD"},
        )
        with tarfile.open(self.root / "export" / "hans.tgz") as tar:
            data = b"".join(
                handle.read() for handle in (
                    tar.extractfile(m) for m in tar.getmembers()
                ) if handle is not None
            )
        self.assertNotIn(CANARY.encode(), data)


if __name__ == "__main__":
    unittest.main()

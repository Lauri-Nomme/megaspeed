"""Import Bitbucket data from a hans scrape directory (prd/31).

Two corpora:

* a **synthetic** scrape (a real git mirror + hand-written REST dumps) that pins
  the transport mapping, the commit/diff derivation from the mirror, the
  pseudonymisation contract and the CLI/env plumbing -- it needs no external
  data;
* the **golden10** capture (``~/dev/hans/scrape/golden10``, 10 PRs of
  FIX/golden) when it is present, which is the real acceptance corpus: the PR
  list has 10 PRs, none of which references a ticket key, so it also proves a
  repo-scoped capture imports its PR/commit/review history anyway.

The commit derivation is additionally diffed against the paired **live REST**
capture in ``~/dev/hans/corpus`` (the ``pr_<id>_commits.json`` dumps), so
"commits come from the mirror" is checked against what DC itself returned, not
against our own idea of it.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ingest import gitclass, taxonomy
from ingest.bitbucket import BitbucketClient, request_kind
from ingest.config import Config
from ingest.hans import HansTransport
from ingest.importer import ImportRejected, import_bundle
from ingest.scrape import scrape
from ingest.store import connect, verify
from ingest.transport import API, BitbucketError

T0 = 1_790_000_000_000
MINUTE = 60_000
GOLDEN10 = Path.home() / "dev" / "hans" / "scrape" / "golden10"
LIVE_CORPUS = Path.home() / "dev" / "hans" / "corpus" / "rest"

ADA = {"name": "ada", "displayName": "Ada Lovelace", "emailAddress": "ada@example.com", "type": "NORMAL"}
GRACE = {"name": "grace", "displayName": "Grace Hopper", "emailAddress": "grace@example.com", "type": "NORMAL"}
ALAN = {"name": "alan", "displayName": "Alan Turing", "emailAddress": "alan@example.com", "type": "NORMAL"}
#: Never stored anywhere: every assertion that the raw zone stayed raw looks for it.
CANARY = "zzcanary"


# --------------------------------------------------------------------------- #
# synthetic scrape: a real git mirror + the REST dumps a hans scrape writes
# --------------------------------------------------------------------------- #
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
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD")


def _git_user(name: str, email: str) -> dict:
    return {"name": name.split()[0].lower(), "displayName": name, "emailAddress": email}


def _commit_row(sha: str, message: str, ts: int) -> dict:
    return {
        "id": sha,
        "displayId": sha[:11],
        "author": _git_user("Ada Lovelace", "ada@example.com"),
        "authorTimestamp": ts,
        "committer": _git_user("Ada Lovelace", "ada@example.com"),
        "committerTimestamp": ts,
        "message": message,
    }


def _activity(activity_id: int, action: str, minute: int, user: dict, **extra) -> dict:
    return {
        "id": activity_id,
        "createdDate": T0 + minute * MINUTE,
        "action": action,
        "user": user,
        **extra,
    }


def build_scrape(root: Path) -> Path:
    """Write a minimal but real hans scrape into ``root`` and return it."""
    work = root / "work"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    base = _commit(work, "chore: init", {"README.md": "demo\n"})

    _git(work, "checkout", "-q", "-b", "feature/one")
    first = _commit(work, "feat: widget list", {"widget.py": "one\n"})
    second = _commit(work, "feat: widget list polish", {"widget.py": "one\ntwo\n"})

    _git(work, "checkout", "-q", "main")
    _git(work, "checkout", "-q", "-b", "feature/two")
    docs = _commit(work, "chore: docs only", {"docs.md": "a\nb\n"})

    _git(work, "checkout", "-q", "main")
    _git(work, "merge", "-q", "--no-ff", "-m", "Merge pull request #2", "feature/two")
    merged = _git(work, "rev-parse", "HEAD")

    mirror = root / "git"
    _git(work, "init", "-q", "--bare", str(mirror))
    _git(work, "push", "-q", str(mirror), "refs/heads/*:refs/heads/*")
    # The refs a hans mirror carries: the PR refs and the pinned old heads.
    _git(work, "push", "-q", str(mirror), f"{second}:refs/pull-requests/1/from")
    _git(work, "push", "-q", str(mirror), f"{second}:refs/stash-refs/pull-requests/1/from")
    _git(work, "push", "-q", str(mirror), f"{docs}:refs/stash-refs/pull-requests/2/from")
    for sha in (first, second):
        _git(work, "push", "-q", str(mirror), f"{sha}:refs/keep/{sha}")

    rest = root / "rest"
    rest.mkdir()

    one = {
        "id": 1,
        "version": 3,
        "title": "DEMO-1: widget list",
        "description": f"{CANARY} description text",
        "state": "OPEN",
        "open": True,
        "closed": False,
        "draft": False,
        "createdDate": T0,
        "updatedDate": T0 + 4 * MINUTE,
        "closedDate": None,
        "fromRef": {"id": "refs/heads/feature/one", "displayId": "feature/one",
                    "latestCommit": second, "type": "BRANCH"},
        "toRef": {"id": "refs/heads/main", "displayId": "main",
                  "latestCommit": base, "type": "BRANCH"},
        "author": {"user": ADA, "role": "AUTHOR", "approved": False},
        "reviewers": [{"user": GRACE, "role": "REVIEWER", "approved": True, "status": "APPROVED"}],
        "participants": [],
        "properties": {},
        "locked": False,
    }
    two = {
        "id": 2,
        "version": 2,
        "title": "docs only",
        "description": f"{CANARY} second description",
        "state": "MERGED",
        "open": False,
        "closed": True,
        "draft": False,
        "createdDate": T0 + 5 * MINUTE,
        "updatedDate": T0 + 8 * MINUTE,
        "closedDate": T0 + 8 * MINUTE,
        "fromRef": {"id": "refs/heads/feature/two", "displayId": "feature/two",
                    "latestCommit": docs, "type": "BRANCH"},
        "toRef": {"id": "refs/heads/main", "displayId": "main",
                  "latestCommit": merged, "type": "BRANCH"},
        "author": {"user": ADA, "role": "AUTHOR", "approved": False},
        "reviewers": [],
        "participants": [],
        "properties": {},
        "locked": False,
    }
    activities = {
        1: [
            _activity(11, "OPENED", 0, ADA),
            _activity(
                12, "COMMENTED", 1, GRACE,
                comment={"id": 100, "text": f"{CANARY} inline note", "severity": "NORMAL",
                         "state": "OPEN", "threadResolved": False},
                commentAnchor={"path": "widget.py", "line": 2},
            ),
            _activity(13, "APPROVED", 2, GRACE),
            _activity(
                14, "RESCOPED", 3, ADA,
                fromHash=second, previousFromHash=first,
                toHash=base, previousToHash=base,
                added={"commits": [_commit_row(second, "feat: widget list polish", T0 + 3 * MINUTE)]},
                removed={"commits": []},
            ),
        ],
        2: [
            _activity(21, "OPENED", 5, ADA),
            _activity(
                22, "COMMENTED", 6, GRACE,
                comment={"id": 200, "text": f"{CANARY} general note", "severity": "NORMAL",
                         "state": "OPEN", "threadResolved": False},
            ),
            _activity(
                23, "MERGED", 8, ADA,
                commit={
                    "id": merged,
                    "displayId": merged[:11],
                    "message": "Merge pull request #2",
                    "author": _git_user("Ada Lovelace", "ada@example.com"),
                    "authorTimestamp": T0 + 8 * MINUTE,
                    "committer": _git_user("Ada Lovelace", "ada@example.com"),
                    "committerTimestamp": T0 + 8 * MINUTE,
                    "parents": [{"id": base}, {"id": docs}],
                },
            ),
        ],
    }

    (rest / "users.json").write_text(json.dumps([ADA, GRACE, ALAN]))
    (rest / "pull-requests_DEMO_widget.json").write_text(json.dumps([two, one]))
    for pr in (one, two):
        (rest / f"pr_{pr['id']}.json").write_text(json.dumps(pr))
        (rest / f"pr_{pr['id']}_activities.json").write_text(json.dumps(activities[pr["id"]]))
    (rest / "project_DEMO.json").write_text(json.dumps({"key": "DEMO", "name": "Demo"}))
    (rest / "repo_DEMO_widget.json").write_text(json.dumps({"slug": "widget", "name": "widget"}))
    (rest / "branches_DEMO_widget.json").write_text(json.dumps({"values": []}))
    (rest / "tags_DEMO_widget.json").write_text(json.dumps({"values": []}))

    index = {
        "scraped_at": "2026-11-01T10:00:00Z",
        "base": "http://localhost:7990",
        "project": "DEMO",
        "repo": "widget",
        "prs": [2, 1],
        "git": {},
        "entities": [
            {"file": "rest/project_DEMO.json", "endpoint": f"{API}/projects/DEMO"},
            {"file": "rest/repo_DEMO_widget.json", "endpoint": f"{API}/projects/DEMO/repos/widget"},
            {"file": "rest/users.json", "endpoint": f"{API}/users"},
            {"file": "rest/branches_DEMO_widget.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/branches"},
            {"file": "rest/tags_DEMO_widget.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/tags"},
            {"file": "rest/pull-requests_DEMO_widget.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests?state=ALL"},
            {"file": "rest/pr_1.json", "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests/1"},
            {"file": "rest/pr_1_activities.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests/1/activities"},
            {"file": "rest/pr_2.json", "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests/2"},
            {"file": "rest/pr_2_activities.json",
             "endpoint": f"{API}/projects/DEMO/repos/widget/pull-requests/2/activities"},
        ],
    }
    (root / "index.json").write_text(json.dumps(index))
    return root


def _config(root: Path) -> Config:
    return Config(
        db_path=root / "store.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[("DEMO", "widget")],
        ticket_projects=("DEMO",),
        text_backend="mock",
    )


def _path(suffix: str = "") -> str:
    return f"{API}/projects/DEMO/repos/widget{suffix}"


class SyntheticScrape:
    """A built scrape directory plus its config (one per test class)."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.scrape_dir = build_scrape(self.root / "capture")
        self.cfg = _config(self.root)

    def cleanup(self) -> None:
        self._tmp.cleanup()


# --------------------------------------------------------------------------- #
# the transport: path -> file mapping, paging, and the mirror-derived commits
# --------------------------------------------------------------------------- #
class HansTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = SyntheticScrape()
        cls.transport = HansTransport(cls.corpus.scrape_dir)

    @classmethod
    def tearDownClass(cls):
        cls.corpus.cleanup()

    def path(self, suffix: str = "") -> str:
        return _path(suffix)

    def test_the_capture_names_its_own_project_and_repo(self):
        self.assertEqual(self.transport.project, "DEMO")
        self.assertEqual(self.transport.repo, "widget")
        self.assertTrue(self.transport.mirror_available)
        self.assertEqual([u["name"] for u in self.transport.users], ["ada", "grace", "alan"])

    def test_the_list_is_paged_the_way_iter_pull_request_pages_expects(self):
        client = BitbucketClient(self.transport, list_page_limit=1)
        pages = list(client.iter_pull_request_pages("DEMO", "widget"))
        self.assertEqual([len(page) for page in pages], [1, 1])
        self.assertEqual([row["id"] for page in pages for row in page], [2, 1])
        self.assertEqual(client.requests_by_kind["list"], 2)

    def test_the_list_is_newest_first_and_filtered_by_state(self):
        newest = self.transport.get(self.path("/pull-requests"), {"state": "ALL", "order": "NEWEST"})
        self.assertEqual([row["id"] for row in newest["values"]], [2, 1])
        merged = self.transport.get(self.path("/pull-requests"), {"state": "MERGED"})
        self.assertEqual([row["id"] for row in merged["values"]], [2])

    def test_every_path_the_client_builds_is_served_from_a_dump(self):
        client = BitbucketClient(self.transport)
        detail = client.pull_request("DEMO", "widget", 1)
        self.assertEqual(detail["title"], "DEMO-1: widget list")
        activities = list(client.iter_activities("DEMO", "widget", 1))
        self.assertEqual([a["action"] for a in activities], ["OPENED", "COMMENTED", "APPROVED", "RESCOPED"])
        for path in (
            self.path("/pull-requests"),
            self.path("/pull-requests/1"),
            self.path("/pull-requests/1/activities"),
            self.path("/pull-requests/1/commits"),
        ):
            self.assertIn(request_kind(path), ("list", "detail", "activities", "commits"))

    def test_the_project_repo_users_and_collections_come_from_the_manifest(self):
        self.assertEqual(self.transport.get(f"{API}/projects/DEMO")["key"], "DEMO")
        self.assertEqual(self.transport.get(f"{API}/projects/DEMO/repos/widget")["slug"], "widget")
        self.assertEqual(self.transport.get(f"{API}/projects/DEMO/repos/widget/branches")["values"], [])
        self.assertEqual(len(self.transport.get(f"{API}/users")["values"]), 3)

    def test_an_unrelated_repo_is_an_error_not_an_empty_page(self):
        with self.assertRaises(BitbucketError):
            self.transport.get(f"{API}/projects/OTHER/repos/widget/pull-requests")
        with self.assertRaises(BitbucketError):
            self.transport.get(f"{API}/projects/DEMO/repos/widget/unknown")

    def test_commits_are_derived_from_the_mirror_for_an_open_pr(self):
        commits = list(BitbucketClient(self.transport).iter_commits("DEMO", "widget", 1))
        self.assertEqual([c["message"] for c in commits],
                         ["feat: widget list", "feat: widget list polish"])
        # The DC row shape the reducer reads, with the *slug* (not the git name)
        # so a commit author and a PR author are the same person.
        self.assertEqual(commits[0]["author"]["name"], "ada")
        self.assertEqual(commits[0]["displayId"], commits[0]["id"][:11])
        self.assertEqual(commits[0]["authorTimestamp"] % 1000, 0)

    def test_commits_are_derived_from_the_merge_parent_for_a_merged_pr(self):
        """``main`` has moved past the branch: the merge commit's first parent is the base."""
        commits = list(BitbucketClient(self.transport).iter_commits("DEMO", "widget", 2))
        self.assertEqual([c["message"] for c in commits], ["chore: docs only"])

    def test_the_merge_parent_lookup_does_not_walk_the_whole_history(self):
        """One ancestry-path query (+ its parent), not one git per chain commit."""
        transport = self.transport
        record = transport._raw_detail(2)
        target = transport._target(record)
        head = transport._head(record, 2)
        calls: list[str] = []
        original = transport._git

        def counting(*args, **kwargs):
            calls.append(args[0])
            return original(*args, **kwargs)

        transport._git = counting
        base = transport._merge_parent(target, head)
        self.assertEqual(len(base), 40)
        self.assertLessEqual(len(calls), 4, calls)

    def test_the_diff_base_is_stamped_on_the_detail_for_a_merged_pr(self):
        detail = self.transport.get(self.path("/pull-requests/2"))
        base = detail["megaspeedDiffBase"]
        self.assertEqual(len(base), 40)
        self.assertNotEqual(base, detail["toRef"]["latestCommit"])

    def test_a_missing_index_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BitbucketError):
                HansTransport(tmp)


# --------------------------------------------------------------------------- #
# end to end: scrape -> bundle -> store, with the pseudonymisation contract
# --------------------------------------------------------------------------- #
class SyntheticImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = SyntheticScrape()
        cls.transport = HansTransport(cls.corpus.scrape_dir)
        cls.report = scrape(
            cls.corpus.cfg,
            cls.transport,
            "hans",
            out=cls.corpus.root / "export" / "demo.tgz",
            state_dir=cls.corpus.root / "export" / "demo.state",
            push_classifier=gitclass.PushClassifier.for_mirror(cls.transport.mirror),
            text_classifier=None,
            verify_quiet=False,
        )
        cls.imported = import_bundle(cls.corpus.cfg, cls.corpus.root / "export" / "demo.tgz")

    @classmethod
    def tearDownClass(cls):
        cls.corpus.cleanup()

    def bundle_bytes(self) -> bytes:
        data = b""
        with tarfile.open(self.corpus.root / "export" / "demo.tgz") as tar:
            for member in tar.getmembers():
                handle = tar.extractfile(member)
                if handle is not None:
                    data += handle.read()
        return data

    def test_the_capture_is_its_own_scope_so_a_keyless_pr_still_imports(self):
        self.assertEqual(self.report["prs_seen"], 2)
        self.assertEqual(self.report["prs_kept"], 2)
        self.assertEqual(self.report["prs_dropped"], 0)
        # ... and the key that *is* there is still attached.
        con = connect(self.corpus.cfg.db_path, read_only=True)
        try:
            keys = con.execute(
                "SELECT pr_id, ticket_keys FROM objects WHERE object_type = 'PR' ORDER BY pr_id"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(keys, [(1, ["DEMO-1"]), (2, [])])

    def test_object_and_event_counts(self):
        self.assertEqual(self.report["counts"]["prs"], 2)
        con = connect(self.corpus.cfg.db_path, read_only=True)
        try:
            objects = dict(con.execute(
                "SELECT object_type, count(*) FROM objects GROUP BY 1"
            ).fetchall())
            events = dict(con.execute(
                "SELECT activity, count(*) FROM events GROUP BY 1"
            ).fetchall())
            sources = con.execute("SELECT DISTINCT source FROM ingest_runs").fetchall()
        finally:
            con.close()
        # PR 2, COMMIT 4 (two branch commits + the docs commit + the merge),
        # PRREV 1 (the one rescope), THREAD 2, PERSON 2 (alan only seeds the map),
        # TICKET 1 (the one key the filter did find).
        self.assertEqual(objects, {
            "PR": 2, "COMMIT": 4, "PRREV": 1, "THREAD": 2, "PERSON": 2, "TICKET": 1,
        })
        self.assertEqual(events, {
            "pr_opened": 2, "comment_added": 2, "pr_approved": 1,
            "pr_pushed": 1, "pr_merged": 1,
        })
        self.assertEqual(sources, [("hans",)])

    def test_push_class_and_diff_size_come_from_the_mirror(self):
        push = self.report["push_class"]
        self.assertTrue(push["enabled"], push)
        self.assertEqual(push["counts"], {"content": 1})
        self.assertEqual(push["diff_size"]["measured"], 2)
        self.assertEqual(push["reasons"], {"commits_changed": 1})

        con = connect(self.corpus.cfg.db_path, read_only=True)
        try:
            attrs = dict(con.execute(
                "SELECT pr_id, CAST(attributes AS VARCHAR) FROM objects WHERE object_type = 'PR'"
            ).fetchall())
            push_class = con.execute(
                "SELECT CAST(attributes AS VARCHAR) FROM objects WHERE object_type = 'PRREV'"
            ).fetchone()[0]
        finally:
            con.close()
        one, two = json.loads(attrs[1]), json.loads(attrs[2])
        # PR 1: two lines added on widget.py against the fork point.
        self.assertEqual((one["lines_added"], one["lines_removed"]), (2, 0))
        # PR 2 is merged: the size is measured against the *merge* base, not the
        # moved target (which would have measured an empty diff).
        self.assertEqual((two["lines_added"], two["lines_removed"]), (2, 0))
        self.assertEqual(json.loads(push_class)["push_class"], "content")

    def test_comments_are_classified_then_discarded(self):
        con = connect(self.corpus.cfg.db_path, read_only=True)
        try:
            rows = con.execute(
                "SELECT object_id, CAST(attributes AS VARCHAR) FROM objects"
                " WHERE object_type = 'THREAD' ORDER BY object_id"
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 2)
        for oid, attrs in rows:
            payload = json.loads(attrs)
            self.assertIn(payload["comment_type"], taxonomy.COMMENT_TYPE)
            self.assertIn(payload["thread_outcome"], taxonomy.THREAD_OUTCOME)
            self.assertEqual(payload["anchor_class"], "file" if oid.endswith(":100") else "general")
        self.assertNotIn(CANARY.encode(), self.bundle_bytes())

    def test_the_pseudonym_map_is_seeded_from_users_json(self):
        path = self.corpus.root / "export" / "demo.state" / "identity.json"
        identity = json.loads(path.read_text())["actors"]
        self.assertEqual(
            sorted(identity), ["bb:ada", "bb:alan", "bb:grace"]
        )
        self.assertEqual(sorted(entry["pseudonym"] for entry in identity.values()),
                         ["AdLo", "AlTu", "GrHo"])

    def test_verify_passes(self):
        con = connect(self.corpus.cfg.db_path, read_only=True)
        try:
            report = verify(con, self.corpus.cfg.key_shape, self.corpus.cfg.ticket_projects)
        finally:
            con.close()
        self.assertTrue(report["ok"], report["problems"])

    def test_a_second_run_is_incremental_and_import_is_idempotent(self):
        again = scrape(
            self.corpus.cfg,
            self.transport,
            "hans",
            out=self.corpus.root / "export" / "demo.tgz",
            state_dir=self.corpus.root / "export" / "demo.state",
            push_classifier=gitclass.PushClassifier.for_mirror(self.transport.mirror),
            verify_quiet=False,
        )
        # The older PR is below the cursor; the newest one ties it and is read
        # again (a tie is ambiguous, and re-reading is idempotent -- prd/14).
        self.assertEqual(again["prs_skipped_watermark"], 1)
        self.assertEqual(again["prs_kept"], 1)
        second = import_bundle(self.corpus.cfg, self.corpus.root / "export" / "demo.tgz")
        self.assertEqual(second["events_new"], 0)
        self.assertEqual(second["events_rewritten"], 0)


class MixedStoreTests(unittest.TestCase):
    """A capture and a ticket-scoped scrape can share one store and still verify."""

    def test_a_later_ticket_scoped_import_still_verifies(self):
        from ingest import fixtures

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scrape_dir = build_scrape(root / "capture")
            cfg = _config(root)
            transport = HansTransport(scrape_dir)
            scrape(
                cfg,
                transport,
                "hans",
                out=root / "hans.tgz",
                state_dir=root / "hans.state",
                text_classifier=None,
                verify_quiet=False,
            )
            import_bundle(cfg, root / "hans.tgz")

            # A synthetic (ticket-scoped) bundle into the same store: its keys
            # are the fixture projects, so the scope is widened for both.
            cfg.ticket_projects = ("DEMO", "PROJ", "OPS", "ARCH")
            bundle_path = fixtures.write_bundle(root / "fixture.json", n=2, seed=1)
            corpus = json.loads(Path(bundle_path).read_text())
            cfg.repos = [
                (project["key"], repo["slug"])
                for project in corpus.get("projects", [])
                for repo in project.get("repos", [])
            ]
            fixture_tgz = root / "fixture.tgz"
            # write_bundle writes the JSON corpus; the scrape turns it into a tgz.
            from ingest.transport import FixtureTransport

            scrape(
                cfg,
                FixtureTransport(corpus),
                "fixture",
                out=fixture_tgz,
                state_dir=root / "fixture.state",
                text_classifier=None,
                verify_quiet=False,
            )
            imported = import_bundle(cfg, fixture_tgz)
            self.assertTrue(imported["verified"], imported["problems"])

            con = connect(cfg.db_path, read_only=True)
            try:
                sources = sorted(row[0] for row in con.execute(
                    "SELECT DISTINCT source FROM ingest_runs"
                ).fetchall())
                report = verify(con, cfg.key_shape, cfg.ticket_projects)
            finally:
                con.close()
            self.assertEqual(sources, ["fixture", "hans"])
            self.assertTrue(report["ok"], report["problems"])


class RepoScopeGuardTests(unittest.TestCase):
    """Only a repo-scoped capture may import PRs without a ticket key (prd/31)."""
    def test_a_fixture_bundle_still_rejects_a_keyless_pr(self):
        from ingest.bundle import write_bundle

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = _config(root)
            bundle = root / "keyless.tgz"
            write_bundle(
                bundle,
                manifest={
                    "schema_version": 1,
                    "source": "fixture",
                    "sources": ["fixture"],
                    "classifier_version": taxonomy.CLASSIFIER_VERSION,
                    "watermark_ts": "2026-11-01T00:00:00",
                    "counts": {},
                    "run": {"run_id": "fixture-test-1"},
                },
                events=[],
                objects=[{
                    "object_type": "PR", "object_id": "PR:DEMO/widget#1",
                    "repo": "DEMO/widget", "pr_id": 1, "ticket_keys": [],
                    "actor": "AdLo", "state": "open", "attributes": {},
                }],
                relations=[],
            )
            with self.assertRaises(ImportRejected) as caught:
                import_bundle(cfg, bundle)
            self.assertIn("no matched ticket key", str(caught.exception))


# --------------------------------------------------------------------------- #
# the CLI: --hans-scrape and MEGASPEED_HANS_SCRAPE
# --------------------------------------------------------------------------- #
class HansCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = SyntheticScrape()

    @classmethod
    def tearDownClass(cls):
        cls.corpus.cleanup()

    def run_cli(self, *argv: str, env: dict | None = None) -> tuple[int, dict, str]:
        from ingest.scrape_cli import main

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(list(argv))
        payload = json.loads(out.getvalue()) if out.getvalue().strip() else {}
        return code, payload, err.getvalue()

    def test_the_flag_runs_the_bitbucket_half_offline(self):
        out = self.corpus.root / "cli" / "flag.tgz"
        code, report, err = self.run_cli(
            "--hans-scrape", str(self.corpus.scrape_dir),
            "--out", str(out), "--json", "--quiet", "--text-backend", "mock",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(report["source"], "hans")
        self.assertEqual(report["prs_kept"], 2)
        self.assertEqual(report["hans_scrape"]["project"], "DEMO")
        self.assertTrue(out.is_file())

    def test_the_environment_variable_selects_the_same_source(self):
        out = self.corpus.root / "cli" / "env.tgz"
        code, report, err = self.run_cli(
            "--out", str(out), "--json", "--quiet", "--text-backend", "mock",
            env={"MEGASPEED_HANS_SCRAPE": str(self.corpus.scrape_dir),
                 "MEGASPEED_REPOS": ""},
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(report["hans_scrape"]["scrape"], str(self.corpus.scrape_dir))
        self.assertEqual(report["prs_seen"], 2)

    def test_no_source_is_still_an_error(self):
        with mock.patch.dict(os.environ, {"MEGASPEED_HANS_SCRAPE": ""}, clear=False):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as caught:
                    from ingest.scrape_cli import main

                    main([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--hans-scrape", err.getvalue())


# --------------------------------------------------------------------------- #
# the real capture
# --------------------------------------------------------------------------- #
@unittest.skipUnless(GOLDEN10.is_dir(), f"no hans capture at {GOLDEN10}")
class Golden10Tests(unittest.TestCase):
    """FIX/golden: 10 PRs, none of which references a ticket key."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.transport = HansTransport(GOLDEN10)
        cls.cfg = Config(
            db_path=cls.root / "store.duckdb",
            identity_path=cls.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[(cls.transport.project, cls.transport.repo)],
            # The capture's own project, as an operator would scope it.
            ticket_projects=(cls.transport.project,),
            text_backend="mock",
        )
        cls.report = scrape(
            cls.cfg,
            cls.transport,
            "hans",
            out=cls.root / "golden10.tgz",
            state_dir=cls.root / "golden10.state",
            push_classifier=gitclass.PushClassifier.for_mirror(cls.transport.mirror),
            verify_quiet=False,
        )
        cls.imported = import_bundle(cls.cfg, cls.root / "golden10.tgz")
        cls.con = connect(cls.cfg.db_path, read_only=True)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls._tmp.cleanup()

    def test_the_whole_capture_imports(self):
        self.assertEqual(self.report["prs_seen"], 10)
        self.assertEqual(self.report["prs_kept"], 10)
        self.assertEqual(self.report["prs_dropped"], 0)
        self.assertEqual(self.report["counts"]["prs"], 10)
        self.assertEqual(self.report["requests_by_kind"],
                         {"activities": 10, "build-status": 10, "commits": 10, "list": 1})

    def test_object_counts(self):
        counts = dict(self.con.execute(
            "SELECT object_type, count(*) FROM objects GROUP BY 1"
        ).fetchall())
        # 10 PRs, 19 commits (18 PR commits + 1 force-pushed-away head that a
        # rescope reports as removed) plus the 4 merge commits, 4 revisions
        # (PR 6, 9 x2, 10), 19 comment threads, 4 people from users.json.
        self.assertEqual(counts, {
            "PR": 10, "COMMIT": 22, "PRREV": 4, "THREAD": 19, "PERSON": 4,
        })

    def test_event_counts(self):
        events = dict(self.con.execute(
            "SELECT activity, count(*) FROM events GROUP BY 1"
        ).fetchall())
        self.assertEqual(events, {
            "pr_opened": 10,
            "comment_added": 19,
            "reviewer_changed": 9,
            "pr_approved": 7,
            "pr_merged": 5,
            "pr_pushed": 4,
            "pr_unapproved": 2,
            "question_raised": 2,
            "pr_declined": 1,
        })
        self.assertEqual(sum(events.values()), self.report["events"])

    def test_every_pr_has_its_commits_and_its_diff_size(self):
        rows = self.con.execute(
            "SELECT pr_id, CAST(attributes AS VARCHAR) FROM objects WHERE object_type = 'PR'"
        ).fetchall()
        self.assertEqual(len(rows), 10)
        for pr_id, attrs in rows:
            payload = json.loads(attrs)
            self.assertIn("lines_added", payload, f"PR {pr_id} has no diff size")
            self.assertIn("lines_removed", payload, f"PR {pr_id} has no diff size")
            self.assertGreater(payload["lines_added"], 0, f"PR {pr_id}")
        # A merged PR is measured against its merge base, not the moved target.
        merged = dict(rows)[1]
        self.assertEqual(json.loads(merged)["lines_added"], 9)
        # The commits endpoint was synthesized for every PR.
        self.assertEqual(self.report["requests_by_kind"]["commits"], 10)
        per_pr = dict(self.con.execute(
            "SELECT pr_id, count(*) FROM objects WHERE object_type = 'COMMIT' GROUP BY 1"
        ).fetchall())
        self.assertEqual(per_pr[1], 3)   # 2 branch commits + the merge commit
        self.assertEqual(per_pr[5], 4)   # stacked: 3 + the merge commit
        # PR 4's own commit is shared with PR 5 (the stacked pair), so the row is
        # owned by whichever PR wrote it last; the object itself must be there.
        base_commit = "COMMIT:5a41da9652f47cc591682ce0cb514e694585e180"
        self.assertEqual(
            self.con.execute(
                "SELECT count(*) FROM objects WHERE object_id = ?", [base_commit]
            ).fetchone()[0],
            1,
        )

    def test_push_class_is_populated_from_the_mirror(self):
        push = self.report["push_class"]
        self.assertTrue(push["enabled"], push)
        self.assertEqual(push["mirror"], str(self.transport.mirror))
        self.assertEqual(push["fetches"], 0)
        self.assertEqual(push["counts"], {"content": 3, "rebase_conflict": 1})
        self.assertEqual(push["diff_size"]["measured"], 10)
        classes = {row[0] for row in self.con.execute(
            "SELECT DISTINCT CAST(attributes->>'push_class' AS VARCHAR) FROM objects"
            " WHERE object_type = 'PRREV'"
        ).fetchall()}
        self.assertEqual(classes, {"content", "rebase_conflict"})

    def test_comment_classification_works_from_the_activities(self):
        rows = self.con.execute(
            "SELECT CAST(attributes AS VARCHAR) FROM objects WHERE object_type = 'THREAD'"
        ).fetchall()
        self.assertEqual(len(rows), 19)
        kinds = set()
        for (attrs,) in rows:
            payload = json.loads(attrs)
            self.assertIn(payload["comment_type"], taxonomy.COMMENT_TYPE)
            self.assertIn(payload["thread_outcome"], taxonomy.THREAD_OUTCOME)
            kinds.add(payload["comment_type"])
        self.assertGreaterEqual(len(kinds), 2)

    def test_verify_passes(self):
        report = verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)
        self.assertTrue(report["ok"], report["problems"])
        self.assertTrue(self.imported["verified"])

    def test_the_bundle_holds_no_raw_comment_text(self):
        canaries = ("add unit tests", "Looks good to me", "orphaned", "verify orphaned-anchor")
        data = b""
        with tarfile.open(self.root / "golden10.tgz") as tar:
            for member in tar.getmembers():
                handle = tar.extractfile(member)
                if handle is not None:
                    data += handle.read()
        for canary in canaries:
            self.assertNotIn(canary.encode(), data)

    @unittest.skipUnless(LIVE_CORPUS.is_dir(), f"no live REST capture at {LIVE_CORPUS}")
    def test_derived_commits_match_the_paired_live_rest_capture(self):
        """The mirror derivation must equal what DC's ``/commits`` returned."""
        checked = 0
        for detail_path in sorted(GOLDEN10.glob("rest/pr_*.json")):
            if detail_path.name.endswith("_activities.json"):
                continue
            pr_id = int(detail_path.stem.split("_")[1])
            live = LIVE_CORPUS / f"pr_{pr_id}_commits.json"
            if not live.is_file():
                continue
            payload = json.loads(live.read_text())
            expected = [c["id"] for c in (
                payload.get("values", payload) if isinstance(payload, dict) else payload
            )]
            derived = [c["id"] for c in self.transport.get(
                f"{API}/projects/FIX/repos/golden/pull-requests/{pr_id}/commits"
            )["values"]]
            self.assertEqual(sorted(derived), sorted(expected), f"PR {pr_id}")
            checked += 1
        self.assertGreaterEqual(checked, 8)


if __name__ == "__main__":
    unittest.main()

"""``push_class``: deterministic content / pure_rebase / rebase_conflict (prd/16).

GAP-01 was that every live ``pr_pushed`` carried ``push_class="other"``. These
tests build a **real git repository** and let the scrape's classifier fetch it
through the real (shallow, partial) fetch path, so ``git patch-id`` and
``git range-diff`` are exercised for real instead of being mocked:

* ``pure``     -- rebased onto a moved ``main``, identical patch;
* ``content``  -- rebased *and* given a new commit (real rework);
* ``conflict`` -- rebased onto a conflicting ``main``, resolution only.

The old heads are deliberately left unreachable from the served branches (that
is what a force-push leaves behind), so the fetch-by-SHA path is covered too.
They are kept as ``refs/keep/*`` in the served repo because a Bitbucket server
keeps every pushed object; the served repo enables the same "wants by SHA"
options, which is the server-side half of the contract.

Also covered: an old head the server cannot serve degrades to ``other`` (no
exception, no crash), the ``megaspeedPatchId`` hand-over into the reducer, the
label crossing the tgz, and ``--verify`` accepting all three values.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ingest import taxonomy
from ingest.gitclass import PushClassifier, parse_range_diff, rescope_hashes
from ingest.importer import import_bundle
from ingest.scrape import scrape
from ingest.store import connect, verify
from ingest.transport import FixtureTransport

PROJECT = "DEMO"
REPO = "demo"
TARGETS: dict[str, tuple[str, str, str]] = {}


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "Dev",
            "GIT_AUTHOR_EMAIL": "dev@example.invalid",
            "GIT_COMMITTER_NAME": "Dev",
            "GIT_COMMITTER_EMAIL": "dev@example.invalid",
            "GIT_AUTHOR_DATE": "2026-11-01T10:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-11-01T10:00:00+00:00",
            "GIT_TERMINAL_PROMPT": "0",
        },
    )
    return result.stdout.decode().strip()


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text)


def _commit(root: Path, message: str, files: dict[str, str]) -> str:
    for name, text in files.items():
        _write(root, name, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


class ScenarioRepo:
    """A work repo with three push scenarios, plus a served bare clone of it."""

    def __init__(self, root: Path):
        self.root = root
        self.work = root / "work"
        self.served = root / "served" / "scm" / PROJECT / f"{REPO}.git"
        self.cases: dict[str, tuple[str, str, str]] = {}

    def build(self) -> None:
        work = self.work
        work.mkdir(parents=True)
        _git(work, "init", "-q", "-b", "main")
        _write(work, "f.txt", "one\ntwo\nthree\n")
        _write(work, "notes.txt", "notes\n")
        base = _commit(work, "base", {"f.txt": "one\ntwo\nthree\n", "notes.txt": "notes\n"})

        # -- pure_rebase: main moves an unrelated file, branch keeps its patch --
        target_pure = _commit(work, "main moves", {"notes.txt": "notes\nmore\n"})
        _git(work, "checkout", "-q", "-b", "pure", base)
        old_pure = _commit(work, "pure feature", {"f.txt": "one\nPURE\nthree\n"})
        _git(work, "rebase", "-q", "main")
        new_pure = _git(work, "rev-parse", "HEAD")
        self.cases["pure"] = (target_pure, old_pure, new_pure)

        # -- content: rebased *and* a new commit (real rework) -----------------
        target_content = _commit(work, "main moves again", {"notes.txt": "notes\nmore\nagain\n"})
        _git(work, "checkout", "-q", "-b", "content", base)
        _commit(work, "content feature", {"f.txt": "one\nCONTENT\nthree\n"})
        old_content = _git(work, "rev-parse", "HEAD")
        _git(work, "rebase", "-q", "main")
        new_content = _commit(work, "address review", {"f.txt": "one\nCONTENT\nthree\nfour\n"})
        self.cases["content"] = (target_content, old_content, new_content)

        # -- rebase_conflict: main touches the same line, resolution is a hunk --
        _git(work, "checkout", "-q", "main")
        target_conflict = _commit(work, "main edits the same line", {"f.txt": "one\nMAIN\nthree\n"})
        _git(work, "checkout", "-q", "-b", "conflict", base)
        old_conflict = _commit(work, "conflict feature", {"f.txt": "one\nCONFLICT\nthree\n"})
        _git(work, "checkout", "-q", "conflict")
        # A conflicting rebase: resolve by keeping both intents on that line.
        rebase = subprocess.run(
            ["git", "-C", str(work), "rebase", "main"],
            capture_output=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "HOME": str(work),
                "GIT_AUTHOR_NAME": "Dev",
                "GIT_AUTHOR_EMAIL": "dev@example.invalid",
                "GIT_COMMITTER_NAME": "Dev",
                "GIT_COMMITTER_EMAIL": "dev@example.invalid",
                "GIT_AUTHOR_DATE": "2026-11-01T10:00:00+00:00",
                "GIT_COMMITTER_DATE": "2026-11-01T10:00:00+00:00",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        assert rebase.returncode != 0, "the scenario must actually conflict"
        _write(work, "f.txt", "one\nMAIN + CONFLICT\nthree\n")
        _git(work, "add", "f.txt")
        _git(work, "-c", "core.editor=true", "rebase", "--continue")
        new_conflict = _git(work, "rev-parse", "HEAD")
        self.cases["conflict"] = (target_conflict, old_conflict, new_conflict)

        # -- rewrite: same commit count, but a substantial real change ---------
        _git(work, "checkout", "-q", "-b", "rewrite", base)
        block = "".join(f"line {index}\n" for index in range(12))
        old_rewrite = _commit(work, "rewrite feature", {"block.txt": block})
        _git(work, "rebase", "-q", "main")
        rewritten = "".join(f"changed {index}\n" for index in range(12))
        _write(work, "block.txt", rewritten)
        _git(work, "add", "block.txt")
        _git(work, "commit", "-q", "--amend", "-m", "rewrite feature")
        new_rewrite = _git(work, "rev-parse", "HEAD")
        self.cases["rewrite"] = (target_conflict, old_rewrite, new_rewrite)

        self._serve(base)

    def _serve(self, base: str) -> None:
        """Bare clone whose objects include the pre-rebase heads (like DC)."""
        self.served.parent.mkdir(parents=True, exist_ok=True)
        _git(self.work, "clone", "-q", "--bare", str(self.work), str(self.served))
        _git(self.served, "config", "uploadpack.allowReachableSHA1InWant", "true")
        _git(self.served, "config", "uploadpack.allowAnySHA1InWant", "true")
        # Keep the pre-rebase heads as objects the server holds but no branch
        # points at: that is a force-push on DC, and it is what makes the
        # fetch-by-SHA path (rather than a branch fetch) matter.
        for name, (_target, old, _new) in self.cases.items():
            _git(self.work, "update-ref", f"refs/keep/{name}", old)
        _git(self.work, "push", "-q", str(self.served),
             "refs/heads/*:refs/heads/*", "refs/keep/*:refs/keep/*")
        _git(self.served, "update-ref", "-d", "refs/heads/pure")
        _git(self.served, "update-ref", "-d", "refs/heads/content")
        _git(self.served, "update-ref", "-d", "refs/heads/conflict")
        _ = base


def _classifier(tmp: Path, **kwargs) -> PushClassifier:
    return PushClassifier(
        tmp / "cache",
        f"file://{tmp / 'served'}",
        url_template="{base}/scm/{project}/{repo}.git",
        **kwargs,
    )


class PushClassScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.repo = ScenarioRepo(cls.root)
        cls.repo.build()
        cls.classifier = _classifier(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_pure_rebase_keeps_the_patch_id(self):
        target, old, new = self.repo.cases["pure"]
        verdict = self.classifier.classify(PROJECT, REPO, target, old, new)
        self.assertEqual(verdict.push_class, "pure_rebase", verdict)
        self.assertEqual(verdict.detail, "same_patch_id")

    def test_a_new_commit_is_content(self):
        target, old, new = self.repo.cases["content"]
        verdict = self.classifier.classify(PROJECT, REPO, target, old, new)
        self.assertEqual(verdict.push_class, "content", verdict)

    def test_a_resolved_conflict_is_a_rebase_conflict(self):
        target, old, new = self.repo.cases["conflict"]
        verdict = self.classifier.classify(PROJECT, REPO, target, old, new)
        self.assertEqual(verdict.push_class, "rebase_conflict", verdict)
        self.assertEqual(verdict.detail, "conflict_hunks")

    def test_a_substantial_rewrite_with_the_same_commit_count_is_content(self):
        """Small hunks are a conflict; a real edit is content (the threshold)."""
        target, old, new = self.repo.cases["rewrite"]
        verdict = self.classifier.classify(PROJECT, REPO, target, old, new)
        self.assertEqual(verdict.push_class, "content", verdict)
        self.assertEqual(verdict.detail, "patch_changed")

    def test_the_cache_is_a_shallow_partial_repo_not_a_mirror(self):
        target, old, new = self.repo.cases["pure"]
        self.classifier.classify(PROJECT, REPO, target, old, new)
        cache = self.classifier.root / f"{PROJECT}-{REPO}.git"
        self.assertTrue(cache.is_dir(), cache)
        # Only heads the classification touched are pinned: no branch mirror.
        refs = _git(cache, "for-each-ref", "--format=%(refname)").splitlines()
        self.assertTrue([r for r in refs if r.startswith("refs/megaspeed/seen/")], refs)
        self.assertEqual([r for r in refs if r.startswith("refs/heads/")], [])

    def test_a_deep_history_stays_shallow_and_falls_back_instead_of_guessing(self):
        """The cache is depth-limited, and a merge base beyond it means ``other``.

        A branch cut from a commit older than the shallow window cannot be
        diffed against the target without deepening; with the ceiling reached,
        the honest answer is ``other`` -- never a guessed label.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            _git(work, "init", "-q", "-b", "main")
            _write(work, "f.txt", "0\n")
            root_commit = _commit(work, "root", {"f.txt": "0\n"})
            for index in range(40):
                _commit(work, f"filler {index}", {"f.txt": f"filler {index}\n"})
            target = _git(work, "rev-parse", "HEAD")
            _git(work, "checkout", "-q", "-b", "topic", root_commit)
            old = _commit(work, "topic work", {"other.txt": "topic\n"})
            _git(work, "checkout", "-q", "topic")
            _git(work, "rebase", "-q", "main")
            new = _git(work, "rev-parse", "HEAD")

            served = root / "served" / "scm" / PROJECT / f"{REPO}.git"
            served.parent.mkdir(parents=True)
            _git(work, "clone", "-q", "--bare", str(work), str(served))
            _git(served, "config", "uploadpack.allowAnySHA1InWant", "true")
            classifier = _classifier(root, depth=4, max_depth=8)
            verdict = classifier.classify(PROJECT, REPO, target, old, new)
            self.assertEqual(verdict.push_class, "other")
            self.assertEqual(verdict.detail, "no_merge_base")
            cache = classifier.root / f"{PROJECT}-{REPO}.git"
            self.assertEqual(_git(cache, "rev-parse", "--is-shallow-repository"), "true")

    def test_an_unreachable_old_head_degrades_to_other(self):
        _target, _old, new = self.repo.cases["pure"]
        missing = "f" * 40
        verdict = self.classifier.classify(PROJECT, REPO, new, missing, new)
        self.assertEqual(verdict.push_class, "other")
        self.assertEqual(verdict.detail, "missing_old_head")

    def test_annotate_stamps_the_activity_and_never_overwrites_a_label(self):
        target, old, new = self.repo.cases["content"]
        activities = [
            {
                "id": 1,
                "action": "RESCOPED",
                "fromHash": new,
                "previousFromHash": old,
                "toHash": target,
                "previousToHash": target,
            },
            {
                # A fixture bundle already carries the label: leave it alone.
                "id": 2,
                "action": "RESCOPED",
                "fromHash": new,
                "previousFromHash": old,
                "toHash": target,
                "megaspeedPatchId": {"push_class": "pure_rebase"},
            },
            {"id": 3, "action": "APPROVED"},
        ]
        self.classifier.annotate(PROJECT, REPO, {}, activities)
        self.assertEqual(activities[0]["megaspeedPatchId"], {"push_class": "content"})
        self.assertEqual(activities[1]["megaspeedPatchId"], {"push_class": "pure_rebase"})
        self.assertNotIn("megaspeedPatchId", activities[2])


class HelpersTests(unittest.TestCase):
    def test_rescope_hashes_uses_the_push_time_target(self):
        activity = {
            "previousFromHash": "a" * 40,
            "fromHash": "b" * 40,
            "previousToHash": "c" * 40,
            "toHash": "d" * 40,
        }
        self.assertEqual(rescope_hashes(activity), ("d" * 40, "a" * 40, "b" * 40))
        # A missing old head is not guessed at: there is nothing to diff.
        self.assertIsNone(rescope_hashes({"fromHash": "b" * 40, "toHash": "d" * 40}))

    def test_parse_range_diff_counts_changed_lines_not_context(self):
        output = (
            "1:  aaaaaaa = 1:  aaaaaaa base commit\n"
            "2:  bbbbbbb ! 2:  bbbbbbb feature commit\n"
            "    @@ -1,4 +1,4 @@\n"
            "     context line\n"
            "    -old line\n"
            "    +new line\n"
            "-:  ------- > 3:  ccccccc extra commit\n"
        )
        ops, changed = parse_range_diff(output)
        self.assertEqual(ops, ["=", "!", ">"])
        self.assertEqual(changed, 2)

    def test_the_vocabulary_accepts_all_three_and_rejects_prose(self):
        for value in ("content", "pure_rebase", "rebase_conflict"):
            self.assertEqual(taxonomy.check_attributes({"push_class": value}),
                             {"push_class": value})
            self.assertIn(value, taxonomy.PUSH_CLASS)
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.check_attributes({"push_class": "looks like a rebase to me"})


class ForScrapeTests(unittest.TestCase):
    def test_disabled_for_fixture_and_without_a_base_url(self):
        from ingest.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(
                db_path=root / "s.duckdb",
                identity_path=root / "id.json",
                ticket_regex=r"\b[A-Z]+-\d+\b",
                key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
                repos=[("DEMO", "demo")],
                base_url="http://bb.example.invalid:7990",
            )
            self.assertIsNotNone(PushClassifier.for_scrape(cfg, "bitbucket-dc", root))
            self.assertIsNone(PushClassifier.for_scrape(cfg, "fixture", root))
            cfg.base_url = ""
            self.assertIsNone(PushClassifier.for_scrape(cfg, "bitbucket-dc", root))
            cfg.base_url = "http://bb.example.invalid:7990"
            cfg.push_class = False
            self.assertIsNone(PushClassifier.for_scrape(cfg, "bitbucket-dc", root))

    def test_url_is_the_dc_scm_slug(self):
        classifier = PushClassifier("/tmp/unused", "http://bb.example:7990/")
        self.assertEqual(
            classifier.repo_url("DEL", "delivery-api"),
            "http://bb.example:7990/scm/DEL/delivery-api.git",
        )

    def test_the_summary_says_why_push_class_is_off(self):
        import contextlib
        import io

        from ingest.scrape_cli import _print_scrape

        report = {
            "source": "fixture",
            "run_id": "r",
            "bundle": "b.tgz",
            "bundle_bytes": 0,
            "bundle_sha256": "",
            "state_dir": "s",
            "identity_map": "i.json",
            "ticket_regex": "x",
            "ticket_projects": [],
            "prs_seen": 0,
            "prs_kept": 0,
            "prs_dropped": 0,
            "prs_skipped_settled": 0,
            "prs_settled": 0,
            "prs_settled_total": 0,
            "prs_skipped_watermark": 0,
            "events": 0,
            "events_collected": 0,
            "objects": 0,
            "requests": 0,
            "requests_by_kind": {},
            "previous_watermark": None,
            "watermark": None,
            "overlap_hours": 72,
            "full": False,
            "repo_watermarks": {},
            "early_stopped": [],
            "keys_by_placement": {},
            "push_class": {"enabled": False, "counts": {}, "reasons": {}, "fetches": 0, "repos": []},
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _print_scrape(report)
        self.assertIn("push class     off (no local git cache)", out.getvalue())
        report["push_class"] = {"enabled": False, "git": "", "counts": {}, "reasons": {},
                                "fetches": 0, "repos": []}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _print_scrape(report)
        self.assertIn("git not installed", out.getvalue())


class ScrapeWiringTests(unittest.TestCase):
    """The label must reach the reducer, the bundle, and pass ``--verify``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = ScenarioRepo(self.root)
        self.repo.build()
        self.classifier = _classifier(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_push_class_crosses_the_bundle_and_verifies(self):
        from ingest.config import Config

        target, old, new = self.repo.cases["content"]
        commit = {
            "id": new,
            "displayId": new[:7],
            "author": {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"},
            "authorTimestamp": 1_790_000_000_000,
            "committer": {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"},
            "committerTimestamp": 1_790_000_000_000,
            "message": "DEMO-1 retry the parser",
        }
        activity = {
            "id": 2,
            "createdDate": 1_790_000_100_000,
            "action": "RESCOPED",
            "user": {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"},
            "fromHash": new,
            "previousFromHash": old,
            "toHash": target,
            "previousToHash": target,
            "added": {"commits": [commit], "total": 1},
            "removed": {"commits": [], "total": 0},
        }
        pr = {
            "id": 1,
            "version": 2,
            "title": "DEMO-1: retry the parser",
            "description": "Epic DEMO-1",
            "state": "OPEN",
            "open": True,
            "closed": False,
            "createdDate": 1_790_000_000_000,
            "updatedDate": 1_790_000_100_000,
            "fromRef": {"latestCommit": new, "displayId": "DEMO-1-retry"},
            "toRef": {"latestCommit": target, "displayId": "main"},
            "author": {"user": {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}},
            "reviewers": [],
            "properties": {},
        }
        bundle = {
            "schema": "megaspeed.fixture/1",
            "projects": [{
                "key": PROJECT,
                "repos": [{
                    "slug": REPO,
                    "pull_requests": [{"pull_request": pr, "activities": [activity], "commits": [commit]}],
                }],
            }],
        }
        cfg = Config(
            db_path=self.root / "store.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[(PROJECT, REPO)],
            ticket_projects=("DEMO",),
            base_url="",
        )
        out = self.root / "export" / "demo.tgz"
        report = scrape(
            cfg,
            FixtureTransport(bundle),
            "bitbucket-dc",
            out=out,
            state_dir=self.root / "export" / "demo.state",
            push_classifier=self.classifier,
        )
        self.assertEqual(report["push_class"]["counts"], {"content": 1})

        from ingest.bundle import read_bundle

        written = read_bundle(out)
        pushes = [e for e in written.events if e["activity"] == "pr_pushed"]
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]["attributes"]["push_class"], "content")

        import_bundle(cfg, out)
        con = connect(cfg.db_path, read_only=True)
        try:
            payload = verify(con, cfg.key_shape, cfg.ticket_projects)
            self.assertTrue(payload["ok"], payload["problems"])
            stored = json.loads(con.execute(
                "SELECT attributes FROM events WHERE activity = 'pr_pushed'"
            ).fetchone()[0])
            self.assertEqual(stored["push_class"], "content")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()

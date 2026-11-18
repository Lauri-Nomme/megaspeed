"""prd/32: the app's build identity (tag + short SHA) and its fallbacks.

``GET /api/meta`` is what the UI renders ("v0.9.0 · 88f72bb"), so it is pinned
against the real ASGI app, and the resolution order is pinned with real git
repositories in a temp dir:

* a checkout resolves ``git describe --tags --match 'v[0-9]*'`` — a
  ``scraper-<sha>`` tag must never masquerade as an app release;
* a directory with no ``.git`` falls back to the baked ``VERSION`` file;
* with neither, the hardcoded ``__version__`` still answers.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from server import app as app_module
from server import version as version_module

HAS_GIT = shutil.which("git") is not None


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _head(repo: Path) -> str:
    return _run(repo, "rev-parse", "--short=8", "HEAD")


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True,
                   capture_output=True)
    _run(root, "config", "user.email", "test@example.invalid")
    _run(root, "config", "user.name", "Test")
    (root / "file.txt").write_text("one\n", encoding="utf-8")
    _run(root, "add", "file.txt")
    _run(root, "commit", "-q", "-m", "one")


def _call_asgi(path: str, query: str = "") -> tuple[int, dict]:
    """One GET through the real app, without httpx (see tests/test_server.py)."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:8766")],
        "client": ("127.0.0.1", 4321),
        "server": ("127.0.0.1", 8766),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app_module.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body.decode())


class MetaEndpointTests(unittest.TestCase):
    """`/api/meta` answers without a store: a version is not tenant data."""

    def test_meta_reports_version_tag_and_sha(self):
        status, body = _call_asgi("/api/meta")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        # The repo carries the v0.9.0 release tag (prd/32 §4), so a checkout
        # resolves all three. A SHA-only answer is valid for a pre-tag build.
        self.assertTrue(body["version"], body)
        self.assertTrue(body["tag"], body)
        self.assertTrue(body["sha"], body)
        self.assertIn("build_time", body)

    def test_meta_matches_the_startup_resolution(self):
        _status, body = _call_asgi("/api/meta")
        for key in ("version", "tag", "sha", "build_time"):
            self.assertEqual(body[key], version_module.VERSION_INFO[key], key)

    def test_meta_rejects_unknown_parameters(self):
        status, body = _call_asgi("/api/meta", query="tenant=alpha")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_the_app_version_is_the_resolved_string(self):
        self.assertEqual(app_module.app.version, version_module.VERSION_INFO["version"])


@unittest.skipUnless(HAS_GIT, "git is not installed")
class GitResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        _init_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_exact_tag_describes_the_build(self):
        _run(self.root, "tag", "-a", "v1.2.3", "-m", "release")
        info = version_module.resolve_version(self.root)
        self.assertEqual(info["version"], "v1.2.3")
        self.assertEqual(info["tag"], "v1.2.3")
        self.assertEqual(info["sha"], _head(self.root))
        self.assertEqual(info["source"], "git")
        self.assertTrue(info["build_time"], info)

    def test_commits_after_the_tag_keep_the_tag_and_add_the_distance(self):
        _run(self.root, "tag", "-a", "v1.2.3", "-m", "release")
        (self.root / "file.txt").write_text("two\n", encoding="utf-8")
        _run(self.root, "commit", "-q", "-am", "two")
        info = version_module.resolve_version(self.root)
        self.assertEqual(info["tag"], "v1.2.3")
        self.assertRegex(info["version"], r"^v1\.2\.3-1-g[0-9a-f]{7,8}$")
        self.assertEqual(info["sha"], _head(self.root))

    def test_a_scraper_tag_is_not_an_app_release(self):
        _run(self.root, "tag", "scraper-0123456789abcdef0123456789abcdef01234567")
        info = version_module.resolve_version(self.root)
        # no semver tag: the SHA alone is the honest answer
        self.assertEqual(info["tag"], "")
        self.assertEqual(info["sha"], _head(self.root))
        self.assertEqual(info["version"], info["sha"])
        self.assertEqual(info["source"], "git")

    def test_the_version_file_wins_when_git_metadata_is_gone(self):
        # The airgapped tgz: no `.git`, a baked VERSION line, still an identity.
        (self.root / ".git").rename(self.root / ".git-away")
        (self.root / "VERSION").write_text("v1.2.3-0-gdeadbeef\n", encoding="utf-8")
        info = version_module.resolve_version(self.root)
        self.assertEqual(info["version"], "v1.2.3-0-gdeadbeef")
        self.assertEqual(info["tag"], "v1.2.3")
        self.assertEqual(info["sha"], "deadbeef")
        self.assertEqual(info["source"], "version-file")
        self.assertTrue(info["build_time"], info)


class VersionFileFallbackTests(unittest.TestCase):
    """No ``.git`` at all — the packaged build's case."""

    def test_a_version_file_yields_a_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "VERSION").write_text("v0.9.0\n", encoding="utf-8")
            info = version_module.resolve_version(root)
            self.assertEqual(info["version"], "v0.9.0")
            self.assertEqual(info["tag"], "v0.9.0")
            self.assertEqual(info["sha"], "")
            self.assertEqual(info["source"], "version-file")

    def test_a_missing_version_file_falls_back_to_the_hardcoded_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = version_module.resolve_version(Path(tmp).resolve())
            self.assertEqual(info["version"], version_module.__version__)
            self.assertTrue(info["version"], info)
            self.assertEqual(info["tag"], "")
            self.assertEqual(info["sha"], "")
            self.assertEqual(info["source"], "default")

    def test_a_blank_version_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "VERSION").write_text("\n", encoding="utf-8")
            info = version_module.resolve_version(root)
            self.assertEqual(info["source"], "default")


class ParseIdentityTests(unittest.TestCase):
    def test_describe_strings_split_into_tag_and_sha(self):
        self.assertEqual(version_module.parse_identity("v0.9.0-3-g88f72bb"), ("v0.9.0", "88f72bb"))
        self.assertEqual(version_module.parse_identity("v0.9.0-0-gdeadbeef"), ("v0.9.0", "deadbeef"))
        # a pre-release tag may itself contain a dash
        self.assertEqual(version_module.parse_identity("v1.0.0-rc1-2-gabc1234"), ("v1.0.0-rc1", "abc1234"))
        self.assertEqual(version_module.parse_identity("v0.9.0", "88f72bb"), ("v0.9.0", "88f72bb"))
        self.assertEqual(version_module.parse_identity("88f72bb"), ("", "88f72bb"))
        self.assertEqual(version_module.parse_identity("0.9.0"), ("", ""))


if __name__ == "__main__":
    unittest.main()

"""The app's build identity (prd/32): tag + short SHA, resolved once at startup.

The UI shows ``v0.9.0 · 88f72bb`` in its meta strip and ``GET /api/meta``
returns the same three strings, so an operator looking at a screenshot can say
which build produced it. This is a *code* identity, never tenant data: nothing
here is derived from a store (prd/02).

Resolution order, first hit wins:

1. **git checkout** — ``git describe --tags --match 'v[0-9]*' --always
   --abbrev=8``. The ``--match`` is deliberate: the repo also carries
   ``scraper-<sha>`` tags for the ingest pipeline, and a scraper tag must never
   masquerade as an app release. A bare SHA (no reachable semver tag) is a
   perfectly good answer — ``tag`` is then ``""`` and the UI shows the SHA.
2. **``VERSION`` file** at the repo root — the airgapped ``.tgz`` ships no
   ``.git``, so the release bake writes one line of ``git describe`` output
   there (``v0.9.0`` or ``v0.9.0-0-gdeadbeef``).
3. **the hardcoded :data:`__version__`** — so a source tree with neither still
   answers honestly instead of 500ing.

``build_time`` is the HEAD commit time for a checkout and the ``VERSION``
file's mtime for a baked release; it is the build's time, not the process's.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The hardcoded last resort (prd/32). Bumped by hand only if a build has neither
# git metadata nor a VERSION file.
__version__ = "0.14.2"

VERSION_FILE = "VERSION"

# A release tag: v1.2.3, optionally with a pre-release/build suffix.
_SEMVER = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
# git describe: <tag>-<commits-since>-g<sha> (the tag may itself contain '-').
_DESCRIBE = re.compile(r"^(?P<tag>.+)-(?P<distance>\d+)-g(?P<sha>[0-9a-f]{7,40})$")
# A bare (possibly 'g'-prefixed) commit hash.
_SHA_ONLY = re.compile(r"^g?(?P<sha>[0-9a-f]{7,40})$")


def _git(repo: Path, *args: str) -> str:
    """Run one read-only git command; '' when git or the repo is unavailable."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # no git binary, no exec, timeout
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def parse_identity(raw: str, sha: str = "") -> tuple[str, str]:
    """Split a version string into ``(tag, sha)``.

    ``v0.9.0-3-g88f72bb`` → ``('v0.9.0', '88f72bb')``; ``v0.9.0`` →
    ``('v0.9.0', sha)``; a bare hash → ``('', hash)``; anything else is kept as
    the version with no tag (``tag`` is only a *semver* tag).
    """
    text = (raw or "").strip()
    if not text:
        return "", sha
    match = _DESCRIBE.match(text)
    if match:
        return match.group("tag"), match.group("sha")
    if _SEMVER.match(text):
        return text, sha
    hash_only = _SHA_ONLY.match(text)
    if hash_only:
        return "", hash_only.group("sha")
    return "", sha


def resolve_version(repo_root: Path | str | None = None) -> dict[str, str]:
    """Resolve ``{version, tag, sha, build_time, source}`` for a checkout or a
    baked release directory.

    ``repo_root`` is a parameter (not just :data:`REPO_ROOT`) so the VERSION-file
    fallback can be tested against a temp directory that has no ``.git``.
    """
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    version = tag = sha = build_time = ""
    source = "default"

    if (root / ".git").exists():  # a dir in a clone, a file in a worktree
        sha = _git(root, "rev-parse", "--short=8", "HEAD")
        described = _git(
            root, "describe", "--tags", "--match", "v[0-9]*", "--always", "--abbrev=8"
        )
        if described:
            tag, described_sha = parse_identity(described, sha)
            sha = described_sha or sha
            version = described
            source = "git"
            build_time = _git(root, "log", "-1", "--format=%cI", "HEAD")

    if not version:
        # The tgz fallback: one line of `git describe` output, baked at release.
        path = root / VERSION_FILE
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            baked = lines[0].strip() if lines else ""
        except OSError:
            baked = ""
        if baked:
            tag, baked_sha = parse_identity(baked)
            sha = baked_sha
            version = baked
            source = "version-file"
            try:
                build_time = datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ).isoformat(timespec="seconds")
            except OSError:
                build_time = ""

    if not version:
        version = __version__
        tag, sha = "", ""
        source = "default"

    return {
        "version": version,
        "tag": tag,
        "sha": sha,
        "build_time": build_time,
        "source": source,
    }


# Resolved once, at import (= server startup). A build identity does not change
# while the process runs, and the UI asks for it on every load.
VERSION_INFO = resolve_version()

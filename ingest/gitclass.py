"""Deterministic ``push_class`` from a local, shallow, partial git repo (prd/16).

GAP-01 was that a live scrape emitted ``push_class="other"`` on every
``pr_pushed``: the classifier is a *git* diff (prd/02, prd/04), and the scrape
side had no repository to diff. This module gives it one -- a **shallow,
partial** bare repo per ``PROJECT/repo`` under ``<state-dir>/git/``:

* **Nothing is mirrored.** Only the refs and commits the matching PRs mention
  are fetched (``--depth`` for the commit graph, ``--filter=blob:none`` so the
  pack stays tiny); a blob is lazily pulled only when a diff actually needs it.
* **It never crosses.** The repo is local state, like the watermark and the
  identity map; only the resulting label enters the bundle (prd/16).
* **Incremental by construction.** The cache survives runs, so the head a PR
  had on the previous scrape is still there when the next rescope reports it as
  ``previousFromHash``. Every head we see is also pinned under
  ``refs/megaspeed/seen/<sha>`` so a repack cannot drop it.

The classification is the one the PRD prescribes, and it is deterministic:

1. ``git patch-id --stable`` of ``target...old`` vs ``target...new``.
   Same patch id -> ``pure_rebase`` (the push only moved the commits).
2. A different patch id is read through ``git range-diff``:
   * commits added or removed, or a change bigger than
     :data:`DEFAULT_CONFLICT_LINES` lines -> ``content`` (real rework);
   * the same commits, only a handful of changed lines -> ``rebase_conflict``
     (the rebase hit a conflict and the resolution is the only difference).

Failure is never fatal (an airgapped scrape must not die on one old head):

* the server no longer serves ``previousFromHash`` (a force-push from another
  machine before our first scrape),
* no merge base inside the shallow window after deepening to the ceiling,
* ``git`` missing, the remote refusing the fetch, or a diff failing.

Each of those degrades that one push to ``other`` -- the vocabulary's explicit
escape hatch (prd/02) -- and is counted under :attr:`PushClassifier.reasons` for
the scrape report. Nothing is retried across runs beyond the incremental cache.

The label is handed to :mod:`ingest.pipeline` through the non-DC
``megaspeedPatchId`` block it already reads, so the reducer stays a pure
raw-payload -> label transform.

The same cache also answers prd/16's **PR diff size**: ``git diff --numstat
<target>...<head>`` totals for the PR's final head, handed over as the non-DC
``megaspeedDiffSize`` block on the PR record. A diff that cannot be computed
(head gone, no merge base) leaves the block off entirely -- the analysis layer
reads an absent size as "pending" and never as zero.

A **hans scrape** (prd/31) already ships the repository as a bare mirror, so the
classifier can read that in place instead of fetching (``for_mirror``): same
labels, same diff sizes, no server and no credentials.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

#: Local directory (inside the scrape state dir) holding the bare repo caches.
GIT_STATE_DIR = "git"
#: Commits per fetched head on the first attempt. Deep enough for a typical PR
#: branch, cheap enough for a huge project (prd/16: ``--depth`` as needed).
DEFAULT_DEPTH = 128
#: Deepening ceiling when the merge base is outside the shallow window.
DEFAULT_MAX_DEPTH = 1024
#: Changed lines in the range-diff at or below which a changed patch counts as a
#: resolved conflict ("only small conflict hunks", prd/16).
DEFAULT_CONFLICT_LINES = 20
#: ``git range-diff --creation-factor``. The default (60) refuses to pair two
#: commits whose patches differ too much and shows them as one removed plus one
#: added -- which is exactly what a resolved conflict looks like on a small
#: commit (its two or three lines are "all new"). Forcing the pairing lets the
#: changed-line count decide instead of the matching heuristic.
CREATION_FACTOR = 999
#: Per git invocation. A hung fetch must not hang the whole scrape.
GIT_TIMEOUT = 300.0
#: Local refs the cache writes. ``target``/``source`` are the PR's branches;
#: ``seen/<sha>`` pins heads that may become unreachable after a force-push.
TARGET_REF = "refs/megaspeed/target"
SOURCE_REF = "refs/megaspeed/source"
SEEN_PREFIX = "refs/megaspeed/seen"
DEFAULT_URL_TEMPLATE = "{base}/scm/{project}/{repo}.git"

_HASH = re.compile(r"^[0-9a-f]{7,40}$")
# ``1:  abcdefg = 1:  abcdefg subject`` / ``-:  ------- > 2:  abcdefg subject``
_COMMIT_LINE = re.compile(
    r"^(?P<left>-|\d+):\s+(?P<lhash>-+|[0-9a-f]+)\s+(?P<op>[=<>!])\s+"
    r"(?P<right>-|\d+):\s+(?P<rhash>-+|[0-9a-f]+)"
)
# A changed line of an interdiff hunk: exactly four spaces, then +/-.
_DIFF_LINE = re.compile(r"^ {4}(?P<sign>[+-])")


class GitError(RuntimeError):
    """A git invocation failed (or git is not installed)."""


@dataclass(frozen=True)
class Verdict:
    """One push's label plus the machine-readable reason (report only)."""

    push_class: str
    detail: str


@dataclass(frozen=True)
class DiffSize:
    """A PR's final diff size in lines (prd/16 "PR diff size")."""

    lines_added: int
    lines_removed: int


@dataclass(frozen=True)
class _Proc:
    code: int
    out: bytes
    err: bytes

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", "replace")


def _auth_header(*, token: str, user: str, password: str) -> str:
    """``Authorization`` for smart HTTP, from the same creds the REST client uses.

    Passed as ``-c http.extraHeader=...`` on each invocation, never written to
    the repo config: the scrape's git cache must not persist a credential.
    """
    if token:
        return f"Authorization: Bearer {token}"
    if user:
        raw = f"{user}:{password}".encode()
        return "Authorization: Basic " + base64.b64encode(raw).decode()
    return ""


def rescope_hashes(activity: dict) -> tuple[str, str, str] | None:
    """``(target, old, new)`` for a rescope activity, or ``None``.

    DC's RESCOPED shape is ``previousFromHash -> fromHash`` (source) and
    ``previousToHash -> toHash`` (target). The activity's own ``toHash`` is the
    target **at push time**, which is the correct common base for a before/after
    comparison: using the PR's current target would let a target move alone
    flip the label.

    A rescope that only moved the target (``previousFromHash == fromHash``) is
    handled naturally: both diffs are identical, i.e. a pure rebase.
    """
    old = str(activity.get("previousFromHash") or "")
    new = str(activity.get("fromHash") or "")
    target = str(activity.get("toHash") or activity.get("previousToHash") or "")
    if not (old and new and target):
        return None
    if not (_HASH.fullmatch(old) and _HASH.fullmatch(new) and _HASH.fullmatch(target)):
        return None
    return target, old, new


def parse_range_diff(output: str) -> tuple[list[str], int]:
    """``(commit ops, changed lines)`` from ``git range-diff`` output.

    ``ops`` is one letter per commit pair: ``=`` unchanged, ``!`` changed,
    ``<`` only in the old range, ``>`` only in the new range. ``changed`` counts
    the interdiff's added/removed lines -- context and hunk headers are not
    counted, so a conflict whose resolution is "adapt these two lines" scores 4,
    while a new commit inside an existing one scores its full diff.
    """
    ops: list[str] = []
    changed = 0
    for line in output.splitlines():
        match = _COMMIT_LINE.match(line)
        if match:
            ops.append(match["op"])
            continue
        if _DIFF_LINE.match(line):
            changed += 1
    return ops, changed


def parse_numstat(output: str) -> tuple[int, int]:
    """``(lines added, lines removed)`` summed from ``git diff --numstat``.

    One line per changed path: ``<added>\\t<removed>\\t<path>``. A binary path
    reports ``-`` for both counts and contributes nothing (git has no line
    count for it); a rename with no edits reports ``0\\t0`` and is kept as such.
    """
    added = removed = 0
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        if fields[0].isdigit():
            added += int(fields[0])
        if fields[1].isdigit():
            removed += int(fields[1])
    return added, removed


class PushClassifier:
    """Classify rescopes of one repo against a cached shallow bare repo."""

    def __init__(
        self,
        root: str | Path,
        base_url: str,
        *,
        user: str = "",
        password: str = "",
        token: str = "",
        url_template: str = DEFAULT_URL_TEMPLATE,
        depth: int = DEFAULT_DEPTH,
        max_depth: int = DEFAULT_MAX_DEPTH,
        conflict_lines: int = DEFAULT_CONFLICT_LINES,
        timeout: float = GIT_TIMEOUT,
        filter_blobs: bool = True,
        mirror: str | Path | None = None,
    ):
        self.root = Path(root)
        self.base_url = str(base_url or "").rstrip("/")
        self.url_template = url_template
        self.depth = max(1, int(depth))
        self.max_depth = max(self.depth, int(max_depth))
        self.conflict_lines = max(0, int(conflict_lines))
        self.timeout = float(timeout)
        self.filter_blobs = bool(filter_blobs)
        #: A **local bare mirror** to read instead of fetching (prd/31). Nothing
        #: is ever written to it: it is the hans scrape's ``git/`` directory.
        self.mirror = Path(mirror) if mirror is not None else None
        self.git = shutil.which("git") or ""
        self.header = _auth_header(token=token, user=user, password=password)
        #: push_class -> count, and reason -> count (both for the scrape report).
        self.counts: Counter[str] = Counter()
        self.reasons: Counter[str] = Counter()
        #: did the diff move at a comment anchor? (prd/16 Q4 thread outcomes)
        self.thread_counts: Counter[str] = Counter()
        #: why a PR's diff size could not be measured (report only)
        self.size_reasons: Counter[str] = Counter()
        #: PRs whose diff size this run measured (fixture-provided ones excluded)
        self.sizes = 0
        #: git fetch invocations, so the report can show what the cache cost.
        self.fetches = 0
        #: git subprocess invocations (every ``_run``), so the report -- and the
        #: "a warm cache costs nothing" regression test -- can show the per-PR
        #: cost instead of guessing it.
        self.runs = 0
        self._repos: dict[str, Path] = {}
        self._complete_cache: dict[Path, bool] = {}
        # -- memos (the review's 26 subprocesses/PR) ----------------------
        #: ``(repo, target, old, new) -> Verdict`` (a whole transition).
        self._memo: dict[tuple[str, str, str, str], Verdict] = {}
        #: ``(repo, sha) -> present?``. ``True`` is permanent (git objects are
        #: immutable); a ``False`` is only valid until the next fetch of that
        #: repo, which is what :attr:`_gen` tracks.
        self._present_cache: dict[tuple[str, str], bool] = {}
        self._present_gen: dict[tuple[str, str], int] = {}
        #: ``(repo, a, b) -> (generation, merge base)`` -- a merge base can only
        #: *appear* after a fetch (deepening), so it is generation-scoped too.
        self._merge_base_cache: dict[tuple[str, str, str], tuple[int, str]] = {}
        #: ``(repo, target, head) -> patch id`` (immutable commit graph).
        self._patch_cache: dict[tuple[str, str, str], str | None] = {}
        #: ``(repo, target, head) -> (DiffSize, reason)`` (immutable graph).
        self._size_cache: dict[tuple[str, str, str], tuple[DiffSize | None, str]] = {}
        #: ``(repo, old, new, path) -> changed?`` (immutable graph).
        self._changed_cache: dict[tuple[str, str, str, str], bool | None] = {}
        #: heads already pinned under ``refs/megaspeed/seen/`` by this run.
        self._kept: set[tuple[str, str]] = set()
        #: ``repo -> fetch generation``: bumped by every successful fetch.
        self._gen: dict[str, int] = {}

    # -- construction ----------------------------------------------------
    @classmethod
    def for_scrape(cls, cfg, source: str, state_dir: str | Path) -> "PushClassifier | None":
        """The classifier a scrape should use, or ``None`` when there is none.

        Fixture mode already carries labels (``megaspeedPatchId`` in the
        generated bundle) and has no server to fetch from; a run without a base
        URL has nothing to fetch either. ``MEGASPEED_PUSH_CLASS=0`` turns the
        whole thing off for a source whose git endpoint is not reachable.
        """
        if not getattr(cfg, "push_class", True):
            return None
        if source == "fixture" or not getattr(cfg, "base_url", ""):
            return None
        # No early return when git is missing: the classifier reports that as a
        # reason instead of silently labelling every push ``other``.
        return cls(
            Path(state_dir) / GIT_STATE_DIR,
            cfg.base_url,
            user=cfg.auth_user,
            password=cfg.auth_password,
            token=cfg.auth_token,
            depth=cfg.git_depth,
            max_depth=cfg.git_max_depth,
            conflict_lines=cfg.push_conflict_lines,
        )

    @classmethod
    def for_mirror(cls, mirror: str | Path, **kwargs) -> "PushClassifier":
        """Classify against a **local bare mirror** instead of a remote (prd/31).

        A hans scrape ships the repository as a bare mirror holding every ref it
        saw (branches, tags, ``refs/pull-requests/*``, ``refs/stash-refs/*`` and
        the heads pinned under ``refs/keep/*``). That is exactly the object set
        the live path's shallow cache would have fetched, so the classifier reads
        it **in place**: no fetch, no cache, no credentials, and nothing is ever
        written to the mirror (every command is a read: ``cat-file``, ``diff``,
        ``patch-id``, ``range-diff``, ``merge-base``).

        ``root``/``base_url`` are unused in this mode; ``report()`` names the
        mirror so the scrape report still says where the labels came from.
        """
        path = Path(mirror)
        return cls(path, "", mirror=path, **kwargs)

    @property
    def available(self) -> bool:
        return bool(self.git)

    def repo_url(self, project: str, repo: str) -> str:
        """Smart-HTTP clone URL for a DC repo (``http://host:7990/scm/PROJ/repo.git``)."""
        return self.url_template.format(base=self.base_url, project=project, repo=repo)

    def report(self) -> dict:
        return {
            "enabled": self.available,
            "git": self.git,
            "state": str(self.root),
            "mirror": str(self.mirror) if self.mirror is not None else None,
            "counts": dict(sorted(self.counts.items())),
            "reasons": dict(sorted(self.reasons.items())),
            "thread_changes": dict(sorted(self.thread_counts.items())),
            "diff_size": {
                "measured": self.sizes,
                "reasons": dict(sorted(self.size_reasons.items())),
            },
            "fetches": self.fetches,
            "git_runs": self.runs,
            "repos": sorted(self._repos),
        }

    # -- public ----------------------------------------------------------
    def annotate(self, project: str, repo: str, pr_record: dict, activities: list[dict]) -> None:
        """Stamp ``megaspeedPatchId`` on every rescope activity of one PR.

        Activities that already carry the block (a fixture bundle, or a second
        pass) are left alone. A failure degrades only the affected pushes and
        never raises into the scrape loop.
        """
        cases: list[tuple[dict, tuple[str, str, str]]] = []
        for activity in activities:
            if "megaspeedPatchId" in activity:
                continue  # a fixture bundle (or a second pass) already has it
            hashes = rescope_hashes(activity)
            if hashes is not None:
                cases.append((activity, hashes))
        if not cases:
            return
        if not self.available:
            self.reasons["git_unavailable"] += len(cases)
            return

        try:
            path = self._repo(project, repo)
            wants = sorted({sha for _activity, hashes in cases for sha in hashes})
            self._ensure_heads(
                path, self._refspecs(pr_record), [*wants, self._pr_target(pr_record)]
            )
        except GitError:
            self.reasons["repo_unavailable"] += len(cases)
            return

        for activity, (target, old, new) in cases:
            verdict = self._classify(path, target, old, new)
            activity["megaspeedPatchId"] = {"push_class": verdict.push_class}
            self.counts[verdict.push_class] += 1
            self.reasons[verdict.detail] += 1

    def classify(self, project: str, repo: str, target: str, old: str, new: str) -> Verdict:
        """Low-level entry point (tests, diagnostics): classify one transition.

        ``target``/``old``/``new`` must be commits of the project/repo the cache
        is for; the heads are fetched on demand, exactly like :meth:`annotate`.
        """
        if not self.available:
            return Verdict("other", "git_unavailable")
        path = self._repo(project, repo)
        self._ensure_heads(path, [], [target, old, new])
        return self._classify(path, target, old, new)

    # -- prd/16: PR diff size (lines added/removed) -----------------------
    def annotate_diff_size(
        self,
        project: str,
        repo: str,
        pr_record: dict,
        commits: list[dict] | tuple[dict, ...] = (),
    ) -> None:
        """Stamp ``megaspeedDiffSize`` on the PR record (prd/16 "PR diff size").

        ``git diff --numstat <target>...<head>`` totals for the PR's **final**
        head, against the same shallow cache ``push_class`` uses. The block is
        non-DC: the reducer copies the two integers onto the PR object's
        attributes, which is what ``analysis/rules.py``'s ``pr_size`` reads.

        Nothing is stamped when the size is unknown -- a head the server no
        longer serves, a shallow window without a merge base, git missing or a
        failing diff. The analysis layer treats an absent size as "pending", so
        a guess (or a zero) here would be worse than silence. Never raises.
        """
        if "megaspeedDiffSize" in pr_record:
            return  # a fixture bundle (or a second pass) already has it
        # prd/31: a source that knows the PR's real base (a hans scrape's mirror
        # does) states it, because ``toRef.latestCommit`` has moved past a merged
        # branch and would measure an empty diff.
        target = str(pr_record.get("megaspeedDiffBase") or "") or self._pr_target(pr_record)
        head = self._pr_head(pr_record) or (
            str((commits[-1] or {}).get("id") or "") if commits else ""
        )
        if not self.available:
            self.size_reasons["git_unavailable"] += 1
            return
        try:
            path = self._repo(project, repo)
            # The cache is shared with push_class/threads: only pay for a fetch
            # when a commit this PR needs is not there yet.
            if not (self._present(path, target) and self._present(path, head)):
                self._ensure_heads(path, self._refspecs(pr_record), [target, head])
        except GitError:
            self.size_reasons["repo_unavailable"] += 1
            return
        size = self._diff_size(path, target, head)
        if size is None:
            return
        pr_record["megaspeedDiffSize"] = {
            "lines_added": size.lines_added,
            "lines_removed": size.lines_removed,
        }
        self.sizes += 1

    # -- prd/16 Q4: did the diff move at a comment anchor? ----------------
    def annotate_threads(
        self,
        project: str,
        repo: str,
        pr_record: dict,
        activities: list[dict],
        commits: list[dict] | tuple[dict, ...] = (),
    ) -> None:
        """Stamp ``megaspeedThreadChange`` on comment activities (prd/15 Q4).

        The thread-outcome classifier needs to know whether the code the
        reviewer pointed at actually changed. The REST payload has no diff, so
        this compares the **anchor path** between the PR head at comment time
        and the head after the next push -- one local ``git diff`` per thread
        against the same shallow cache ``push_class`` already uses.

        Only a decided answer is stamped: a missing anchor path, a head the
        server no longer serves, or no git cache leaves the activity untouched
        and the reducer falls back to the classifier's read of the text.
        """
        cases: list[tuple[dict, str, int]] = []
        for activity in activities:
            if str(activity.get("action") or "") != "COMMENTED":
                continue
            if "megaspeedThreadChange" in activity:
                continue  # already decided (fixture bundle or a second pass)
            comment = activity.get("comment") or {}
            anchor = activity.get("commentAnchor") or comment.get("anchor") or {}
            anchor_path = str(anchor.get("path") or "")
            if not anchor_path:
                self.thread_counts["no_anchor"] += 1
                continue
            cases.append((activity, anchor_path, int(activity.get("createdDate") or 0)))
        if not cases:
            return
        if not self.available:
            self.thread_counts["git_unavailable"] += len(cases)
            return

        timeline = self._head_timeline(pr_record, activities, commits)
        try:
            path = self._repo(project, repo)
            wants = [sha for _ts, sha in timeline]
            head = self._pr_head(pr_record)
            self._ensure_heads(path, self._refspecs(pr_record), [*wants, head])
        except GitError:
            self.thread_counts["repo_unavailable"] += len(cases)
            return

        for activity, anchor_path, ts in cases:
            old = self._head_at(timeline, ts)
            new = self._head_after(timeline, ts) or self._pr_head(pr_record)
            changed = self._changed_at(path, old, new, anchor_path)
            if changed is None:
                self.thread_counts["unknown"] += 1
                continue
            activity["megaspeedThreadChange"] = {"changed": changed}
            self.thread_counts["changed" if changed else "unchanged"] += 1

    @staticmethod
    def _head_timeline(
        pr_record: dict, activities: list[dict], commits: list[dict] | tuple[dict, ...]
    ) -> list[tuple[int, str]]:
        """``(epoch_ms, head_sha)`` for every moment the PR head is known.

        Commit timestamps and rescope activities both mark a head; sorted, they
        let a comment dated anywhere in the PR's life be pinned to the code that
        was on the branch when it was written.
        """
        points: list[tuple[int, str]] = []
        for commit in commits:
            ts, sha = commit.get("authorTimestamp"), commit.get("id")
            if ts and sha:
                points.append((int(ts), str(sha)))
        for activity in activities:
            if str(activity.get("action") or "") != "RESCOPED":
                continue
            ts, sha = activity.get("createdDate"), activity.get("fromHash")
            if ts and sha:
                points.append((int(ts), str(sha)))
        points.sort()
        return points

    @staticmethod
    def _head_at(timeline: list[tuple[int, str]], ts: int) -> str:
        """The head as of ``ts`` (the newest known head at or before it)."""
        head = ""
        for point_ts, sha in timeline:
            if point_ts > ts:
                break
            head = sha
        return head or (timeline[0][1] if timeline else "")

    @staticmethod
    def _head_after(timeline: list[tuple[int, str]], ts: int) -> str:
        """The first head *after* ``ts`` (the push that answered the comment)."""
        for point_ts, sha in timeline:
            if point_ts > ts:
                return sha
        return ""

    @staticmethod
    def _pr_head(pr_record: dict) -> str:
        return str((pr_record.get("fromRef") or {}).get("latestCommit") or "")

    def _changed_at(self, path: Path, old: str, new: str, anchor_path: str) -> bool | None:
        """Did the range ``old..new`` touch ``anchor_path``? ``None`` = unknown."""
        if not (old and new and anchor_path):
            return None
        if old == new:
            return False
        key = (str(path), old, new, anchor_path)
        if key in self._changed_cache:
            return self._changed_cache[key]
        if not (self._present(path, old) and self._present(path, new)):
            return None
        result = self._run(
            path, ["diff", "--numstat", "--no-renames", old, new, "--", anchor_path], check=False
        )
        if result.code != 0:
            return None
        changed = bool(result.text.strip())
        self._changed_cache[key] = changed
        return changed

    def _diff_size(self, path: Path, target: str, head: str) -> DiffSize | None:
        """``git diff --numstat target...head`` totals, or ``None`` when unknown.

        The three-dot form diffs against the merge base, so a target that moved
        after the branch was cut does not inflate the PR's own size. Renames are
        left to git's default detection: a file moved without edits is a 0-line
        change, not a delete plus an add of the whole file.

        Memoised per (repo, target, head): the commit graph is immutable, so a
        re-scrape of an unchanged PR costs no subprocess. The failure reason is
        re-counted on a memo hit, so ``report()`` still explains every PR.
        """
        key = (str(path), target, head)
        cached = self._size_cache.get(key)
        if cached is None:
            cached = self._measure_size(path, target, head)
            self._size_cache[key] = cached
        size, reason = cached
        if reason:
            self.size_reasons[reason] += 1
        return size

    def _measure_size(self, path: Path, target: str, head: str) -> tuple[DiffSize | None, str]:
        """The (size, failure reason) pair :meth:`_diff_size` memoises."""
        if not (target and head):
            return None, "no_head"
        for label, sha in (("target", target), ("head", head)):
            if not self._present(path, sha):
                return None, f"missing_{label}"
        if not self._ensure_merge_base(path, target, head):
            return None, "no_merge_base"
        result = self._run(
            path,
            ["diff", "--numstat", "--no-ext-diff", "--no-textconv", "--find-renames",
             f"{target}...{head}"],
            check=False,
        )
        if result.code != 0:
            return None, "diff_failed"
        added, removed = parse_numstat(result.text)
        return DiffSize(added, removed), ""

    # -- cache management ------------------------------------------------
    def _repo(self, project: str, repo: str) -> Path:
        key = f"{project}/{repo}"
        cached = self._repos.get(key)
        if cached is not None:
            return cached
        if self.mirror is not None:
            # prd/31: read the scrape's bare mirror in place. No init, no
            # remote, no fetch -- the mirror is not ours to write to.
            if not (self.mirror / "HEAD").exists():
                raise GitError(f"no git mirror at {self.mirror}")
            self._repos[key] = self.mirror
            return self.mirror
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-")
        path = self.root / f"{slug}.git"
        if not (path / "HEAD").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._run(path.parent, ["init", "-q", "--bare", str(path)])
            self._run(path, ["config", "gc.auto", "0"])
            self._run(path, ["config", "remote.origin.url", self.repo_url(project, repo)])
            # No default fetch refspec on purpose: a bare `git fetch origin` would
            # mirror every branch (prd/16 forbids a mirror). Each fetch names the
            # one refspec or commit it wants.
            self._run(path, ["config", "remote.origin.fetch", ""])
        self._repos[key] = path
        return path

    def _refspecs(self, pr_record: dict) -> list[str]:
        """The PR's branches, fetched so their tips are local and stay reachable."""
        specs: list[str] = []
        target = (pr_record.get("toRef") or {}).get("displayId")
        source = (pr_record.get("fromRef") or {}).get("displayId")
        if target:
            specs.append(f"+refs/heads/{target}:{TARGET_REF}")
        if source:
            specs.append(f"+refs/heads/{source}:{SOURCE_REF}")
        return specs

    @staticmethod
    def _pr_target(pr_record: dict) -> str:
        return str((pr_record.get("toRef") or {}).get("latestCommit") or "")

    def _ensure_heads(self, path: Path, refspecs: list[str], wants: list[str]) -> None:
        """Fetch (best effort) the branches and commits a PR needs.

        Branch refs are fetched together -- one of them may be gone after a
        merge, so a failure there is not fatal. The commits are fetched as one
        batch and, if the batch is refused, one by one: a single unreachable
        old head must not cost the others.

        A branch fetch is **skipped when the cache already holds every commit
        the PR names**: the branch tip cannot add anything the classification
        reads, so a warm cache costs no network round trip at all (the review
        measured one ``fetch`` per PR, cold *and* warm).

        In mirror mode there is nothing to fetch: the scrape's bare mirror
        already holds every ref hans captured (prd/31).
        """
        if self.mirror is not None:
            return
        wanted = [sha for sha in dict.fromkeys(wants) if _HASH.fullmatch(sha or "")]
        # Once a cache has its full history (a small repo that deepening
        # completed), asking for ``--depth`` again would re-shallow it and force
        # the merge base to be re-deepened on every run.
        depth = None if self._complete(path) else self.depth
        missing = [sha for sha in wanted if not self._present(path, sha)]
        if refspecs and missing:
            self._fetch(path, refspecs, depth=depth)
            missing = [sha for sha in missing if not self._present(path, sha)]
        if missing:
            if not self._fetch(path, missing, depth=depth):
                for sha in missing:
                    if not self._present(path, sha):
                        self._fetch(path, [sha], depth=depth)
        self._keep_many(path, [sha for sha in wanted if self._present(path, sha)])

    def _complete(self, path: Path) -> bool:
        """True when the cache has the full history (not shallow any more).

        An empty cache also answers "not shallow", so a ref is required too:
        otherwise the very first fetch would skip ``--depth`` and pull a whole
        project's history.
        """
        cached = self._complete_cache.get(path)
        if cached is None:
            shallow = self._run(path, ["rev-parse", "--is-shallow-repository"], check=False)
            refs = self._run(path, ["for-each-ref", "--count=1", "--format=%(refname)"],
                             check=False)
            cached = (
                shallow.code == 0
                and shallow.text.strip() == "false"
                and refs.code == 0
                and bool(refs.text.strip())
            )
            self._complete_cache[path] = cached
        return cached

    def _keep_many(self, path: Path, shas: list[str]) -> None:
        """Pin several heads in **one** ``git update-ref --stdin`` process.

        The review counted three ``update-ref`` runs per PR; the pins are
        idempotent per (repo, sha), so a re-scrape of the same PRs pays none.
        """
        fresh = [
            sha for sha in dict.fromkeys(shas) if (str(path), sha) not in self._kept
        ]
        if not fresh:
            return
        stdin = b"".join(f"update {SEEN_PREFIX}/{sha} {sha}\n".encode() for sha in fresh)
        result = self._run(path, ["update-ref", "--stdin"], stdin=stdin, check=False)
        if result.code != 0:
            # An old git without ``--stdin`` support: fall back to one call each.
            for sha in fresh:
                self._run(path, ["update-ref", f"{SEEN_PREFIX}/{sha}", sha], check=False)
        self._kept.update((str(path), sha) for sha in fresh)

    def _fetch(
        self, path: Path, specs: list[str], *, depth: int | None = None, deepen: int | None = None
    ) -> bool:
        if self.mirror is not None:
            # prd/31: a mirror is read-only local state; there is no remote.
            return False
        args = ["fetch", "--no-tags", "--quiet"]
        if deepen:
            args.append(f"--deepen={int(deepen)}")
        elif depth:
            args += ["--depth", str(int(depth))]
        if self.filter_blobs:
            # Commit graph only; a diff pulls the few blobs it needs on demand.
            args.append("--filter=blob:none")
        self.fetches += 1
        code = self._run(path, args + ["origin", *specs], check=False).code
        if code == 0:
            # The object graph just grew: a cached "not present" or "no merge
            # base" answer is stale from here on.
            self._bump(path)
        return code == 0

    # -- memo plumbing -----------------------------------------------------
    def _generation(self, path: Path) -> int:
        return self._gen.get(str(path), 0)

    def _bump(self, path: Path) -> None:
        self._gen[str(path)] = self._generation(path) + 1

    # -- classification ---------------------------------------------------
    def _classify(self, path: Path, target: str, old: str, new: str) -> Verdict:
        key = (str(path), target, old, new)
        verdict = self._memo.get(key)
        if verdict is None:
            verdict = self._classify_uncached(path, target, old, new)
            self._memo[key] = verdict
        return verdict

    def _classify_uncached(self, path: Path, target: str, old: str, new: str) -> Verdict:
        for label, sha in (("target", target), ("old_head", old), ("new_head", new)):
            if not self._present(path, sha):
                # A force-pushed head the server no longer serves, or a branch
                # that is gone: nothing to diff against (prd/16 fallback).
                return Verdict("other", f"missing_{label}")
        if not (self._ensure_merge_base(path, target, old)
                and self._ensure_merge_base(path, target, new)):
            return Verdict("other", "no_merge_base")

        patch_old = self._patch_id(path, target, old)
        patch_new = self._patch_id(path, target, new)
        if patch_old is None or patch_new is None:
            return Verdict("other", "patch_id_failed")
        if patch_old == patch_new:
            return Verdict("pure_rebase", "same_patch_id")
        return self._changed(path, target, old, new)

    def _changed(self, path: Path, target: str, old: str, new: str) -> Verdict:
        """Different patch ids: conflict resolution, or real rework?

        ``target..old`` / ``target..new`` (two-dot) compare the *branch's own*
        commits; the three-dot form would put the target's own side in the left
        range only, turning every rebase into "commits changed". The eager
        ``--creation-factor`` makes a rewritten commit pair with its original so
        the changed-line count, not the pairing heuristic, decides (a resolved
        conflict on a three-line commit would otherwise look like a full
        replacement).
        """
        result = self._run(
            path,
            ["range-diff", f"--creation-factor={CREATION_FACTOR}", "--no-color", "--no-notes",
             f"{target}..{old}", f"{target}..{new}"],
            check=False,
        )
        if result.code != 0:
            # Cannot prove it is only a conflict: the honest label is rework.
            return Verdict("content", "range_diff_failed")
        ops, changed = parse_range_diff(result.text)
        if not ops:
            return Verdict("content", "range_diff_empty")
        if any(op in "<>" for op in ops):
            # Commits were added, dropped or squashed: real rework by definition.
            return Verdict("content", "commits_changed")
        if changed == 0:
            return Verdict("pure_rebase", "range_diff_identical")
        if changed <= self.conflict_lines:
            return Verdict("rebase_conflict", "conflict_hunks")
        return Verdict("content", "patch_changed")

    def _patch_id(self, path: Path, target: str, head: str) -> str | None:
        """``git patch-id --stable`` of the PR diff (``target...head``).

        ``--no-renames`` keeps the diff content-only: a base that renamed a file
        must not change the patch id of an unchanged branch. Memoised per
        (repo, target, head): two diffs of the same immutable commits cannot
        differ, so a re-scrape pays nothing.
        """
        key = (str(path), target, head)
        if key in self._patch_cache:
            return self._patch_cache[key]
        diff = self._run(
            path,
            ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
             f"{target}...{head}"],
            check=False,
        )
        if diff.code != 0:
            return None
        ids = self._run(path, ["patch-id", "--stable"], stdin=diff.out, check=False)
        if ids.code != 0:
            return None
        value = ""
        for line in ids.text.splitlines():
            fields = line.split()
            if fields and _HASH.fullmatch(fields[0]):
                value = fields[0].lower()
                break
        self._patch_cache[key] = value
        return value

    def _ensure_merge_base(self, path: Path, target: str, head: str) -> bool:
        """True once the shallow window contains a merge base.

        A first shallow fetch often cuts the ancestry before the branch point,
        so no common commit is visible and ``target...head`` cannot be diffed.
        Deepening doubles the window up to the ceiling; past that the push is
        unclassifiable rather than misclassified.
        """
        if self._merge_base(path, target, head):
            return True
        depth = self.depth
        while depth < self.max_depth:
            depth = min(depth * 2, self.max_depth)
            if not self._fetch(path, [target, head], deepen=depth):
                return False
            self._complete_cache.pop(path, None)  # the boundary just moved
            if self._merge_base(path, target, head):
                return True
        return False

    def _merge_base(self, path: Path, a: str, b: str) -> str:
        """``git merge-base a b`` (``""`` = none in the shallow window).

        Memoised per (repo, a, b) **per fetch generation**: deepening is what can
        create a merge base, so a negative answer is only trusted until the next
        fetch of this repo.
        """
        key = (str(path), a, b)
        cached = self._merge_base_cache.get(key)
        if cached is not None and cached[0] == self._generation(path):
            return cached[1]
        result = self._run(path, ["merge-base", a, b], check=False)
        value = result.text.strip() if result.code == 0 else ""
        self._merge_base_cache[key] = (self._generation(path), value)
        return value

    def _present(self, path: Path, sha: str) -> bool:
        """Is ``sha`` a commit in the cache? Memoised (the review's 13/PR).

        ``True`` is permanent -- git objects are immutable and a repack cannot
        remove a commit that a ref (or our own ``seen/`` pin) keeps reachable. A
        ``False`` is only valid until the next fetch, which is what the fetch
        generation guards.
        """
        if not sha or not _HASH.fullmatch(sha):
            return False
        key = (str(path), sha)
        cached = self._present_cache.get(key)
        if cached is True:
            return True
        if cached is False and self._present_gen.get(key) == self._generation(path):
            return False
        value = self._run(path, ["cat-file", "-e", f"{sha}^{{commit}}"], check=False).code == 0
        self._present_cache[key] = value
        self._present_gen[key] = self._generation(path)
        return value

    # -- process plumbing -------------------------------------------------
    def _run(
        self,
        path: Path,
        args: list[str],
        *,
        stdin: bytes | None = None,
        check: bool = True,
    ) -> _Proc:
        if not self.git:
            raise GitError("git is not installed")
        command = [self.git, "-C", str(path)]
        if self.header:
            command += ["-c", f"http.extraHeader={self.header}"]
        command += args
        self.runs += 1
        try:
            proc = subprocess.run(
                command,
                input=stdin,
                capture_output=True,
                timeout=self.timeout,
                env=self._env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if check:
                raise GitError(f"git {args[0]} could not run: {exc}") from exc
            return _Proc(1, b"", str(exc).encode())
        result = _Proc(proc.returncode, proc.stdout, proc.stderr)
        if check and result.code != 0:
            raise GitError(
                f"git {args[0]} failed ({result.code}): "
                f"{result.err.decode('utf-8', 'replace').strip()[:300]}"
            )
        return result

    @staticmethod
    def _env() -> dict[str, str]:
        env = dict(os.environ)
        # Never block on a credential prompt in an unattended scrape, and keep
        # git's messages parseable regardless of the operator's locale.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C"
        return env

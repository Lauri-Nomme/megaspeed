"""Import Bitbucket data from a **hans scrape directory** (prd/31).

``~/dev/hans``'s ``bb-archiver scrape`` captured the REST payloads and a bare git
mirror of a repository during the migration. That capture is a read-only
snapshot of the same Bitbucket Data Center API :class:`ingest.transport.
HttpTransport` calls, so the reducer does not change at all -- only the
transport does:

```
rest/pull-requests_<PROJ>_<REPO>.json   /rest/api/1.0/projects/<P>/repos/<R>/pull-requests
rest/pr_<id>.json                       /rest/api/1.0/projects/<P>/repos/<R>/pull-requests/<id>
rest/pr_<id>_activities.json            /rest/api/1.0/projects/<P>/repos/<R>/pull-requests/<id>/activities
rest/users.json                         /rest/api/1.0/users
rest/project_<P>.json, rest/repo_<P>_<R>.json, rest/branches_*, rest/tags_*
index.json                              the manifest (project, repo, prs, entities)
git/                                    bare mirror: refs/heads, refs/tags,
                                        refs/pull-requests/*, refs/stash-refs/*, refs/keep/*
```

The dump has **no** per-PR ``/commits``, ``/comments`` or ``/diff`` REST file, so
those are derived:

* **commits** come from the ``git/`` mirror: the PR's base is the merge base
  against ``toRef.latestCommit``, or -- for a PR that is already merged -- the
  first parent of the merge commit that brought its head into the target. That
  reproduces DC's ``/commits`` payload exactly (verified against the paired live
  capture in ``~/dev/hans/corpus``), including the stacked-PR cases where a head
  is an ancestor of the target.
* **diff size / push_class** come from the same mirror, read directly by
  :class:`ingest.gitclass.PushClassifier` (see ``for_mirror``): no fetch, no
  cache, no credentials.
* **comments** are the ``COMMENTED`` activities, which carry the comment body --
  they go through the normal reducer, so they are classified and discarded
  exactly like a live comment (prd/02, prd/16). No raw text leaves this module.

The scrape directory **is** the scope (``unscoped``): it holds exactly the PRs
hans was told to capture, so a PR without a ticket key is still imported (its
PR/commit/review history is real data). The ticket filter still runs and still
attaches keys where a PR does carry one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .transport import API, BitbucketError, _page

#: ``/projects/<P>/repos/<R>`` and whatever follows it: the pull-request
#: sub-resources, the branch/tag collections, or nothing (the repo document).
_REPO_ROUTE = re.compile(
    r"^" + API + r"/projects/(?P<proj>[^/]+)/repos/(?P<repo>[^/]+)(?P<rest>/.*)?$"
)
#: The pull-request collection and its two per-PR sub-resources.
_PR_ROUTE = re.compile(
    r"^/pull-requests(?:/(?P<pr>\d+)(?:/(?P<sub>activities|commits))?)?$"
)
_USERS_PATH = API + "/users"
#: Git record separator: ``git log -z`` ends every commit with NUL, and the
#: fields are joined with US (a commit message cannot contain either).
_RECORD, _FIELD = "\x00", "\x1f"
_LOG_FORMAT = _FIELD.join(("%H", "%an", "%ae", "%at", "%cn", "%ce", "%ct", "%B"))


def _slug(name: str, email: str) -> str:
    """DC's user slug for a git author: the e-mail local part, else the name.

    The scrape's identity table keys a Bitbucket actor by slug (``bb:ada``), so a
    commit author must resolve to the *same* actor as the PR author who pushed
    it -- otherwise one human would get two pseudonyms and the store would show
    two people.
    """
    local = email.split("@", 1)[0].strip().lower()
    return local or name.strip()


def _commit_user(name: str, email: str) -> dict:
    """A git author/committer as the DC user shape the reducer reads."""
    name, email = name.strip(), email.strip()
    return {
        "name": _slug(name, email),
        "displayName": name,
        "emailAddress": email,
        "type": "NORMAL",
    }


class HansTransport:
    """Serve the DC REST paths from a hans scrape directory.

    The interface is the one :class:`ingest.bitbucket.BitbucketClient` uses
    (``get(path, params)`` returning DC JSON, paging included), so the scrape,
    the reducer and the bundle are unchanged.
    """

    #: The scrape directory is the scope (see the module docstring): a PR with no
    #: ticket key is imported rather than dropped. ``ingest.scrape.collect``
    #: reads this attribute.
    unscoped = True

    def __init__(self, path: str | Path, *, git: str | None = None, timeout: float = 120.0):
        self.root = Path(path).expanduser()
        index_path = self.root / "index.json"
        if not index_path.is_file():
            raise BitbucketError(
                f"not a hans scrape directory (no index.json): {self.root}"
            )
        try:
            self.index = json.loads(index_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BitbucketError(f"cannot read {index_path}: {exc}") from exc
        self.project = str(self.index.get("project") or "")
        self.repo = str(self.index.get("repo") or "")
        self.mirror = self.root / "git"
        self.git = git if git is not None else (shutil.which("git") or "")
        self.timeout = float(timeout)
        self._routes = self._build_routes()
        if not (self.project and self.repo):
            self.project, self.repo = self._infer_repo()
        self._pr_ids = self._read_pr_ids()
        self._details: dict[int, dict] = {}
        self._activities: dict[int, list[dict]] = {}
        self._commits: dict[int, list[dict]] = {}
        self._bases: dict[int, str] = {}
        #: target sha -> its first-parent chain (newest → oldest), shared by all
        #: merged PRs against that target. The chain is large; recomputing it for
        #: every PR was one of the two O(history) costs here.
        self._chains: dict[str, list[str]] = {}
        self._list: list[dict] | None = None
        self.users = self._load_users()
        #: Requests served, for diagnostics (BitbucketClient counts its own).
        self.requests = 0

    # -- construction ----------------------------------------------------
    def _build_routes(self) -> dict[str, Path]:
        """``endpoint path -> file`` from the manifest's entity table.

        This is the scrape's own statement of what it captured, so a differently
        named dump (hans has changed file naming before) still routes correctly;
        the conventional names below are only a fallback.
        """
        routes: dict[str, Path] = {}
        for entity in self.index.get("entities") or []:
            endpoint = str(entity.get("endpoint") or "")
            name = str(entity.get("file") or "")
            if not (endpoint and name):
                continue
            target = self.root / name
            if target.is_file():
                routes[endpoint.split("?", 1)[0].rstrip("/")] = target
        return routes

    def _infer_repo(self) -> tuple[str, str]:
        """Project/repo for a manifest that omits them.

        The entity table names them (``/projects/<P>/repos/<R>``), and the PR
        list dump does too (``rest/pull-requests_<P>_<R>.json``); a capture that
        carries neither cannot be routed, so that is an error.
        """
        for endpoint in self._routes:
            match = re.match(
                r"^" + API + r"/projects/([^/]+)/repos/([^/]+)", endpoint
            )
            if match:
                return match.group(1), match.group(2)
        for path in sorted((self.root / "rest").glob("pull-requests_*.json")):
            project, _, repo = path.stem[len("pull-requests_"):].partition("_")
            if project and repo:
                return project, repo
        raise BitbucketError(
            f"cannot tell which project/repo {self.root} captures: index.json has"
            " no project/repo and no pull-requests dump names one"
        )

    def _read_pr_ids(self) -> list[int]:
        ids: list[int] = []
        for value in self.index.get("prs") or []:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        if ids:
            return ids
        # No ``prs`` list: the detail dumps are the enumeration.
        for path in sorted((self.root / "rest").glob("pr_*.json")):
            match = re.fullmatch(r"pr_(\d+)\.json", path.name)
            if match:
                ids.append(int(match["1"]))
        return sorted(set(ids))

    def _load_users(self) -> list[dict]:
        """``users.json``: the instance's users, used to seed pseudonyms."""
        path = self._routes.get(_USERS_PATH)
        if path is None:
            path = self.root / "rest" / "users.json"
        if not path.is_file():
            return []
        payload = self._load(path)
        if isinstance(payload, dict):
            payload = payload.get("values") or []
        return [user for user in payload if isinstance(user, dict) and user]

    # -- transport interface ---------------------------------------------
    @property
    def mirror_available(self) -> bool:
        return bool(self.git) and (self.mirror / "HEAD").exists()

    def get(self, path: str, params: dict | None = None) -> dict:
        """One DC REST response, from the dump (or the mirror, for commits)."""
        params = params or {}
        self.requests += 1
        repo_match = _REPO_ROUTE.match(path)
        if repo_match:
            self._check_repo(repo_match["proj"], repo_match["repo"])
            rest = repo_match["rest"] or ""
            pr_match = _PR_ROUTE.match(rest)
            if pr_match:
                pr_id, sub = pr_match["pr"], pr_match["sub"]
                if pr_id is None:
                    return _page(self._list_values(params), _start(params), _limit(params))
                pr_id = int(pr_id)
                if sub is None:
                    return self._detail(pr_id)
                if sub == "activities":
                    return _page(self._activity_values(pr_id), _start(params), _limit(params))
                return _page(self._commit_values(pr_id), _start(params), _limit(params))
            # branches, tags, or the repo document itself.
            return self._served(path, params)
        if path.rstrip("/") == _USERS_PATH:
            return _page(self.users, _start(params), _limit(params))
        return self._served(path, params)

    def _check_repo(self, project: str, repo: str) -> None:
        if (project.upper(), repo.lower()) != (self.project.upper(), self.repo.lower()):
            raise BitbucketError(
                f"hans scrape {self.root} holds {self.project}/{self.repo},"
                f" not {project}/{repo}"
            )

    def _served(self, path: str, params: dict) -> dict:
        """A document the manifest maps verbatim (project, repo, branches, tags)."""
        target = self._routes.get(path.rstrip("/"))
        if target is None:
            raise BitbucketError(f"hans scrape {self.root} has no dump for {path}")
        payload = self._load(target)
        if isinstance(payload, list):
            return _page(payload, _start(params), _limit(params))
        if isinstance(payload, dict) and isinstance(payload.get("values"), list):
            return _page(payload["values"], _start(params), _limit(params))
        return payload

    # -- collections -----------------------------------------------------
    def _list_values(self, params: dict) -> list[dict]:
        """The PR list, filtered by ``state`` and sorted the way DC sorts it.

        ``order=NEWEST`` means ``updatedDate`` descending (DC 9.4.18), which is
        what the scrape's watermark/early-termination logic relies on -- the dump
        itself is in scrape order, not necessarily DC's.
        """
        if self._list is None:
            target = self._routes.get(
                f"{API}/projects/{self.project}/repos/{self.repo}/pull-requests"
            )
            if target is None:
                target = self.root / "rest" / f"pull-requests_{self.project}_{self.repo}.json"
            if target.is_file():
                payload = self._load(target)
                values = payload.get("values") if isinstance(payload, dict) else payload
                self._list = [row for row in (values or []) if isinstance(row, dict)]
            else:
                # REST-only scrape without the list dump: the details are enough
                # to walk (they carry the same cheap fields).
                self._list = [self._detail(pr_id) for pr_id in self._pr_ids]
        values = list(self._list)
        state = str(params.get("state") or "ALL").upper()
        if state != "ALL":
            values = [row for row in values if str(row.get("state") or "OPEN").upper() == state]
        if str(params.get("order") or "NEWEST").upper() == "NEWEST":
            values.sort(key=lambda row: int(row.get("updatedDate") or 0), reverse=True)
        # The scrape reduces the *list* rows (DC's list carries a full record),
        # so the derived diff base has to ride along with them, not only with the
        # detail document.
        out = []
        for row in values:
            pr_id = row.get("id")
            base = self._diff_base(int(pr_id)) if pr_id is not None else ""
            out.append({**row, "megaspeedDiffBase": base} if base else row)
        return out

    def _detail(self, pr_id: int) -> dict:
        """``rest/pr_<id>.json``, plus the base the mirror says the PR forked from.

        ``megaspeedDiffBase`` is the non-DC block the diff-size classifier reads
        (``ingest.gitclass``): for a merged PR the target has moved past the
        branch, so diffing against ``toRef.latestCommit`` would report 0 lines.
        The base is derived from the mirror and never enters the bundle.
        """
        record = self._raw_detail(pr_id)
        out = dict(record)
        base = self._diff_base(pr_id)
        if base:
            out["megaspeedDiffBase"] = base
        return out

    def _raw_detail(self, pr_id: int) -> dict:
        cached = self._details.get(pr_id)
        if cached is not None:
            return cached
        target = self._routes.get(
            f"{API}/projects/{self.project}/repos/{self.repo}/pull-requests/{pr_id}"
        )
        if target is None:
            target = self.root / "rest" / f"pr_{pr_id}.json"
        if not target.is_file():
            raise BitbucketError(f"hans scrape {self.root} has no PR {pr_id}")
        payload = self._load(target)
        if not isinstance(payload, dict):
            raise BitbucketError(f"{target} is not a PR detail document")
        self._details[pr_id] = payload
        return payload

    def _activity_values(self, pr_id: int) -> list[dict]:
        cached = self._activities.get(pr_id)
        if cached is not None:
            return cached
        target = self._routes.get(
            f"{API}/projects/{self.project}/repos/{self.repo}"
            f"/pull-requests/{pr_id}/activities"
        )
        if target is None:
            target = self.root / "rest" / f"pr_{pr_id}_activities.json"
        if not target.is_file():
            values: list[dict] = []
        else:
            payload = self._load(target)
            values = payload.get("values") if isinstance(payload, dict) else payload
            values = [row for row in (values or []) if isinstance(row, dict)]
        self._activities[pr_id] = values
        return values

    def _commit_values(self, pr_id: int) -> list[dict]:
        """Synthesize DC's ``/commits`` payload from the git mirror.

        ``git log --reverse <base>..<head>`` over the PR's own commits, shaped
        like DC's rows (``id``/``displayId``/``author``/``authorTimestamp``/
        ``message``). The reducer reads exactly those fields.
        """
        cached = self._commits.get(pr_id)
        if cached is not None:
            return cached
        record = self._raw_detail(pr_id)
        base = self._diff_base(pr_id)
        head = self._head(record, pr_id)
        commits: list[dict] = []
        if base and head and self.mirror_available:
            output = self._git(
                "log", "--reverse", "-z", f"--format={_LOG_FORMAT}", f"{base}..{head}"
            )
            for row in output.split(_RECORD):
                fields = row.split(_FIELD, 7)
                if len(fields) < 8 or not fields[0].strip():
                    continue
                sha, an, ae, at, cn, ce, ct, message = fields
                sha = sha.strip()
                commits.append(
                    {
                        "id": sha,
                        "displayId": sha[:11],
                        "author": _commit_user(an, ae),
                        "authorTimestamp": int(at or 0) * 1000,
                        "committer": _commit_user(cn, ce),
                        "committerTimestamp": int(ct or 0) * 1000,
                        # DC's ``message`` carries no trailing newline.
                        "message": message.rstrip("\n"),
                    }
                )
        self._commits[pr_id] = commits
        return commits

    # -- the mirror ------------------------------------------------------
    def _diff_base(self, pr_id: int) -> str:
        """The commit the PR's diff is measured against, or ``""``.

        Three cases, in order:

        1. the head is **not** in the target (an open/unmerged PR): the merge
           base is the fork point, exactly what ``target...head`` means;
        2. the head **is** in the target (merged): walk the target's
           first-parent chain to the merge that brought the head in and use its
           first parent -- the target at merge time;
        3. nothing derivable (a trimmed mirror): ``""``, and the callers fall
           back to "unknown" instead of guessing.
        """
        cached = self._bases.get(pr_id)
        if cached is not None:
            return cached
        base = ""
        try:
            record = self._raw_detail(pr_id)
        except BitbucketError:
            # A list-only capture: nothing to derive a base from.
            self._bases[pr_id] = ""
            return ""
        target = self._target(record)
        head = self._head(record, pr_id)
        if target and head and self.mirror_available:
            if head == target:
                base = ""
            elif self._is_ancestor(target, head):
                base = self._merge_parent(target, head)
            else:
                base = self._git("merge-base", target, head).strip()
        self._bases[pr_id] = base
        return base

    def _target(self, record: dict) -> str:
        target = str((record.get("toRef") or {}).get("latestCommit") or "")
        if target:
            return target
        return self._branch_tip((record.get("toRef") or {}).get("displayId") or "")

    def _head(self, record: dict, pr_id: int) -> str:
        """The PR's head: ``fromRef``, then the PR refs the mirror carries."""
        head = str((record.get("fromRef") or {}).get("latestCommit") or "")
        if head:
            return head
        for ref in (
            f"refs/stash-refs/pull-requests/{pr_id}/from",
            f"refs/pull-requests/{pr_id}/from",
        ):
            sha = self._rev_parse(ref)
            if sha:
                return sha
        return self._branch_tip((record.get("fromRef") or {}).get("displayId") or "")

    def _merge_parent(self, target: str, head: str) -> str:
        """First parent of the merge that brought ``head`` into ``target``.

        The merge is the **oldest** commit on the target's first-parent chain
        that still contains the head; its first parent is the target at merge
        time. ``git rev-list --first-parent --ancestry-path <head>..<target>``
        answers that in **one** command (the last entry). The previous code
        spawned ``git merge-base --is-ancestor`` for every commit in the chain,
        which is O(history) git processes per merged PR -- hours on a big
        mirror. The chain lookup fallback keeps the exact old semantics for a
        git that cannot run ``--ancestry-path``.
        """
        try:
            path = self._git(
                "rev-list", "--first-parent", "--ancestry-path", f"{head}..{target}"
            ).split()
        except BitbucketError:
            path = []
        if path:
            parent = self._git("rev-parse", f"{path[-1]}^1", check=False).strip()
            return parent if re.fullmatch(r"[0-9a-f]{40}", parent) else ""
        chain = self._chain(target)
        if not chain:
            return ""
        # Recent → oldest is true → false for "head is an ancestor"; binary
        # search the boundary instead of walking every commit.
        low, high, found = 0, len(chain) - 1, None
        while low <= high:
            mid = (low + high) // 2
            if self._is_ancestor(chain[mid], head):
                found = mid
                high = mid - 1
            else:
                low = mid + 1
        if found is None:
            return ""
        return chain[found + 1] if found + 1 < len(chain) else ""

    def _chain(self, target: str) -> list[str]:
        """The target's first-parent chain, cached across every merged PR."""
        if target not in self._chains:
            self._chains[target] = self._git(
                "rev-list", "--first-parent", target
            ).split()
        return self._chains[target]

    def _branch_tip(self, display_id: str) -> str:
        return self._rev_parse(f"refs/heads/{display_id}") if display_id else ""

    def _rev_parse(self, ref: str) -> str:
        if not self.mirror_available:
            return ""
        result = self._git("rev-parse", "--verify", "--quiet", ref, check=False)
        return result.strip() if re.fullmatch(r"[0-9a-f]{40}", result.strip()) else ""

    def _is_ancestor(self, commit: str, other: str) -> bool:
        if not (commit and other and self.mirror_available):
            return False
        self._git("merge-base", "--is-ancestor", other, commit, check=False)
        return self._last_code == 0

    # -- plumbing --------------------------------------------------------
    def _git(self, *args: str, check: bool = True) -> str:
        """Run one read-only git command against the mirror.

        ``MEGASPEED_GIT_TRACE=1`` prints the command, its duration and its
        output size to stderr: on a large capture the only way to see which
        git call is slow (the transport is otherwise a black box between PRs).
        """
        if not self.git:
            raise BitbucketError("git is not installed: cannot derive commits from the mirror")
        command = [self.git, "-C", str(self.mirror), *args]
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C"
        trace = bool(os.environ.get("MEGASPEED_GIT_TRACE"))
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                timeout=self.timeout,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if trace:
                print(
                    f"[hans-git] {' '.join(args)[:160]} · "
                    f"{time.monotonic() - started:.2f}s · FAILED {exc}",
                    file=sys.stderr, flush=True,
                )
            raise BitbucketError(f"git {args[0]} could not run: {exc}") from exc
        if trace:
            print(
                f"[hans-git] {' '.join(args)[:160]} · "
                f"{time.monotonic() - started:.2f}s · {len(proc.stdout)} bytes"
                f" · rc={proc.returncode}",
                file=sys.stderr, flush=True,
            )
        self._last_code = proc.returncode
        if check and proc.returncode != 0:
            raise BitbucketError(
                f"git {args[0]} failed ({proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace').strip()[:300]}"
            )
        return proc.stdout.decode("utf-8", "replace")

    def _load(self, path: Path):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BitbucketError(f"cannot read {path}: {exc}") from exc

    def report(self) -> dict:
        """What this transport served, for the scrape report (never raw text)."""
        return {
            "scrape": str(self.root),
            "project": self.project,
            "repo": self.repo,
            "prs": len(self._pr_ids),
            "mirror": str(self.mirror) if self.mirror_available else None,
            "users": len(self.users),
            "requests": self.requests,
        }

    _last_code = 0


def _start(params: dict) -> int:
    return int(params.get("start", 0) or 0)


def _limit(params: dict) -> int:
    return int(params.get("limit", 25) or 25)


__all__ = ["HansTransport"]

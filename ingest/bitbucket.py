"""Bitbucket Data Center REST client (PRs, activities, commits, paging).

Written against the real DC paths listed in ``prd/04-exports.md``:

    /rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests
    /rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests/{id}/activities
    /rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests/{id}/commits

The client is transport-agnostic, so fixture mode and live mode share it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterator

from .transport import API


@dataclass
class PullRequest:
    project: str
    repo: str
    pr: dict
    commits: list[dict] = field(default_factory=list)
    activities: list[dict] = field(default_factory=list)

    @property
    def id(self) -> int:
        return int(self.pr["id"])

    @property
    def updated_ms(self) -> int:
        return int(self.pr.get("updatedDate") or self.pr.get("createdDate") or 0)


class BitbucketClient:
    def __init__(self, transport, page_limit: int = 25, min_interval: float = 0.0):
        self.transport = transport
        self.page_limit = page_limit
        self.min_interval = min_interval  # politeness delay between requests
        self._last_request = 0.0
        self.requests = 0

    # -- low level -------------------------------------------------------
    def _get(self, path: str, params: dict | None = None) -> dict:
        if self.min_interval:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._last_request = time.monotonic()
        self.requests += 1
        return self.transport.get(path, params)

    def _pages(self, path: str, params: dict | None = None) -> Iterator[dict]:
        start = 0
        while True:
            page = self._get(path, {**(params or {}), "start": start, "limit": self.page_limit})
            yield from page.get("values", [])
            if page.get("isLastPage", True):
                return
            start = page.get("nextPageStart", start + self.page_limit)

    # -- collections -----------------------------------------------------
    def iter_pull_requests(self, project: str, repo: str, state: str = "ALL") -> Iterator[dict]:
        path = f"{API}/projects/{project}/repos/{repo}/pull-requests"
        yield from self._pages(path, {"state": state})

    def iter_activities(self, project: str, repo: str, pr_id: int) -> Iterator[dict]:
        path = f"{API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/activities"
        yield from self._pages(path)

    def iter_commits(self, project: str, repo: str, pr_id: int) -> Iterator[dict]:
        path = f"{API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/commits"
        yield from self._pages(path)

    # -- composite -------------------------------------------------------
    def pull_requests(self, project: str, repo: str, state: str = "ALL") -> Iterator[PullRequest]:
        """Metadata + commits. Activities are fetched only for kept PRs.

        Commits come first on purpose: the ticket-key filter reads commit
        messages, so a PR that matches nothing never costs an activities
        request against the real server.
        """
        for pr in self.iter_pull_requests(project, repo, state=state):
            commits = list(self.iter_commits(project, repo, int(pr["id"])))
            yield PullRequest(project, repo, pr, commits)

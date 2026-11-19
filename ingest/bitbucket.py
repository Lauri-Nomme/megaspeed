"""Bitbucket Data Center REST client (PRs, activities, commits, paging).

Written against the real DC paths listed in ``prd/04-exports.md``:

    /rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests
    /rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests/{id}
    /rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests/{id}/activities
    /rest/api/1.0/projects/{proj}/repos/{repo}/pull-requests/{id}/commits

The client is transport-agnostic, so fixture mode and live mode share it.

Two things make a huge project affordable (prd/14):

* the **list** is read with ``limit=100`` and ``order=NEWEST``, so one request
  covers 100 PR summaries and the newest activity is always seen first;
* the **detail / activities / commits** endpoints are only touched by callers
  that decided a PR is worth it. ``requests_by_kind`` counts each kind, which is
  what the scrape tests assert on.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterator

from .transport import API

# The PR list is cheap per row, so ask for the largest page DC allows (prd/14 §1)
# independently of the (politeness) page size used for activities/commits.
LIST_PAGE_LIMIT = 100

_SUBRESOURCE = re.compile(
    r"/pull-requests(?:/(?P<pr>\d+)(?:/(?P<sub>activities|commits))?)?$"
)

CLOSED_STATES = frozenset({"MERGED", "DECLINED"})

#: DC's commit build-status resource (prd/44 DES-2). It is *not* part of the PR
#: activity stream; the scrape reads it for the PR's head commit so the prd/40
#: ``ci`` flag has a producer. A server without the module 404s, which the
#: scrape treats as "no CI data", never as a failed PR.
BUILD_STATUS_API = "/rest/build-status/1.0"


@dataclass
class PullRequest:
    project: str
    repo: str
    pr: dict
    commits: list[dict] = field(default_factory=list)
    activities: list[dict] = field(default_factory=list)
    #: Raw commit build-status records (DC ``/rest/build-status/1.0``), each
    #: annotated with ``commitId``. Empty = no CI data (not "no CI ran").
    build_statuses: list[dict] = field(default_factory=list)

    @property
    def id(self) -> int:
        return int(self.pr["id"])

    @property
    def state(self) -> str:
        return str(self.pr.get("state") or "OPEN").upper()

    @property
    def closed(self) -> bool:
        return self.state in CLOSED_STATES

    @property
    def label(self) -> str:
        """``DEL/delivery-api#42`` — the key the scrape state files use."""
        return f"{self.project}/{self.repo}#{self.id}"

    @property
    def updated_ms(self) -> int:
        return int(self.pr.get("updatedDate") or self.pr.get("createdDate") or 0)


def request_kind(path: str) -> str:
    """Classify a request path: ``list`` | ``detail`` | ``activities`` | ``commits``."""
    match = _SUBRESOURCE.search(path)
    if not match:
        return "other"
    if match["pr"] is None:
        return "list"
    return match["sub"] or "detail"


class BitbucketClient:
    def __init__(
        self,
        transport,
        page_limit: int = 25,
        min_interval: float = 0.0,
        list_page_limit: int = LIST_PAGE_LIMIT,
    ):
        self.transport = transport
        self.page_limit = page_limit
        self.list_page_limit = list_page_limit
        self.min_interval = min_interval  # politeness delay between requests
        self._last_request = 0.0
        self.requests = 0
        # What the scrape actually cost, by endpoint kind (prd/14): the tests
        # assert that a non-matching PR costs no detail/activities/commits.
        self.requests_by_kind: Counter[str] = Counter()

    # -- low level -------------------------------------------------------
    def _get(self, path: str, params: dict | None = None, kind: str | None = None) -> dict:
        self.requests_by_kind[kind or request_kind(path)] += 1
        if self.min_interval:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._last_request = time.monotonic()
        self.requests += 1
        return self.transport.get(path, params)

    def _pages(
        self,
        path: str,
        params: dict | None = None,
        *,
        kind: str | None = None,
        page_limit: int | None = None,
    ) -> Iterator[dict]:
        limit = self.page_limit if page_limit is None else page_limit
        start = 0
        while True:
            page = self._get(path, {**(params or {}), "start": start, "limit": limit}, kind=kind)
            yield from page.get("values", [])
            if page.get("isLastPage", True):
                return
            start = page.get("nextPageStart", start + limit)

    # -- collections -----------------------------------------------------
    def iter_pull_request_pages(
        self, project: str, repo: str, state: str = "ALL", order: str = "NEWEST"
    ) -> Iterator[list[dict]]:
        """Cheap PR summaries, newest first, one 100-row page at a time.

        Pages (not rows) because prd/14 §4 stops paginating as soon as a *full*
        page sits entirely at/below the watermark: DC 9.4.18 sorts
        ``order=NEWEST`` by ``updatedDate`` descending, so every older page is
        older still and nothing further can need scraping.
        """
        path = f"{API}/projects/{project}/repos/{repo}/pull-requests"
        start = 0
        while True:
            page = self._get(
                path,
                {"state": state, "order": order, "start": start, "limit": self.list_page_limit},
                kind="list",
            )
            values = list(page.get("values", []))
            yield values
            if page.get("isLastPage", True):
                return
            start = page.get("nextPageStart", start + self.list_page_limit)

    def iter_pull_requests(
        self, project: str, repo: str, state: str = "ALL", order: str = "NEWEST"
    ) -> Iterator[dict]:
        for page in self.iter_pull_request_pages(project, repo, state=state, order=order):
            yield from page

    def pull_request(self, project: str, repo: str, pr_id: int) -> dict:
        """One PR's full record. Only for PRs that survived the ticket pre-filter."""
        path = f"{API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}"
        return self._get(path, kind="detail")

    def newest_pull_request(self, project: str, repo: str) -> dict | None:
        """The single newest list row (``limit=1``) -- one cheap request.

        The quiescence guard (prd/14, ``ingest/scrape.py``) reads this before and
        after a repo so a corpus rebuild that lands mid-scrape is *noticed*
        instead of silently producing a torn snapshot.
        """
        path = f"{API}/projects/{project}/repos/{repo}/pull-requests"
        page = self._get(
            path, {"state": "ALL", "order": "NEWEST", "start": 0, "limit": 1}, kind="list"
        )
        values = page.get("values") or []
        return dict(values[0]) if values else None

    def iter_activities(self, project: str, repo: str, pr_id: int) -> Iterator[dict]:
        path = f"{API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/activities"
        yield from self._pages(path, kind="activities")

    def iter_commits(self, project: str, repo: str, pr_id: int) -> Iterator[dict]:
        path = f"{API}/projects/{project}/repos/{repo}/pull-requests/{pr_id}/commits"
        yield from self._pages(path, kind="commits")

    def build_statuses(self, commit_id: str) -> list[dict]:
        """The build statuses DC recorded for one commit (prd/44 DES-2).

        States are normalised by the reducer (``SUCCESSFUL``/``FAILED``/
        ``INPROGRESS`` -> ``taxonomy.BUILD_RESULT``); each record carries
        ``dateAdded``, so a started/finished pair becomes two events.
        """
        path = f"{BUILD_STATUS_API}/commits/{commit_id}"
        return list(self._pages(path, kind="build-status"))

    # -- composite -------------------------------------------------------
    def pull_requests(
        self,
        project: str,
        repo: str,
        state: str = "ALL",
        order: str = "NEWEST",
        *,
        with_commits: bool = True,
    ) -> Iterator[PullRequest]:
        """List rows (+ commits). Used by ``--probe``, which maps *every* PR.

        The scrape does not use this: it pre-filters on the cheap list fields
        before it pays for a single commit/activity request (prd/14 §1).
        """
        for pr in self.iter_pull_requests(project, repo, state=state, order=order):
            commits = list(self.iter_commits(project, repo, int(pr["id"]))) if with_commits else []
            yield PullRequest(project, repo, pr, commits)

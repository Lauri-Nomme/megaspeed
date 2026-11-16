"""Jira Data Center REST client (issues, changelog, worklogs, sprints, fields).

Written against the real DC 9.17 paths listed in ``prd/04-exports.md``:

    /rest/api/2/field                                   # custom-field discovery
    /rest/api/2/status                                  # status -> category
    /rest/api/2/search?jql=...                          # issue list (paged)
    /rest/api/2/issue/{key}?expand=changelog            # fields + history
    /rest/api/2/issue/{key}/worklog                     # native worklogs
    /rest/agile/1.0/board?projectKeyOrId=PROJ           # scrum board
    /rest/agile/1.0/board/{id}/sprint                   # sprint list
    /rest/agile/1.0/sprint/{id}/issue                   # sprint membership

The client is transport-agnostic: fixture mode (:class:`FixtureJiraTransport`)
and live mode (:class:`ingest.transport.HttpTransport`) share it, so a fixture
run exercises the paging, URL building and parsing rather than bypassing them.

Two shape facts the rest of the ingest relies on:

* **Jira DC has no stable custom-field ids across installations.** "Epic Link",
  "Sprint" and "Story Points" are ``customfield_10100`` / ``_10104`` / ``_10106``
  on the lab and something else elsewhere, so they are *discovered* by name
  (:meth:`JiraClient.field_ids`) and can be pinned by config.
* **The Sprint field is a serialized Greenhopper blob**, not a plain id
  (``com.atlassian.greenhopper.service.sprint.Sprint@…[id=3,name=…]``). The id
  is parsed out of it; newer builds return a list of objects instead.

Nothing here writes anything: raw payloads stay in memory and are reduced to
keys / pseudonyms / closed labels by :mod:`ingest.jira_pipeline`.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterator

API2 = "/rest/api/2"
AGILE = "/rest/agile/1.0"
#: Issue list page size. The list is cheap per row (key + updated), so ask for
#: the largest page DC allows.
LIST_PAGE_LIMIT = 100
#: Custom fields the ingest needs, by their Jira *name*. Ids are discovered.
FIELD_EPIC_LINK = "Epic Link"
FIELD_SPRINT = "Sprint"
FIELD_STORY_POINTS = "Story Points"

_SPRINT_ID = re.compile(r"\bid=(\d+)\b")


class JiraError(RuntimeError):
    pass


def request_kind(path: str) -> str:
    """Classify a request path for the scrape cost report (prd/14)."""
    if path.endswith("/field"):
        return "fields"
    if path.endswith("/status"):
        return "statuses"
    if path.endswith("/serverInfo"):
        return "serverinfo"
    if "/worklog" in path:
        return "worklog"
    if path.endswith("/search"):
        return "search"
    if re.search(r"/issue/[^/]+$", path):
        return "issue"
    if re.search(r"/sprint/\d+/issue$", path):
        return "sprint-issues"
    if path.endswith("/sprint"):
        return "sprints"
    if path.endswith("/board"):
        return "boards"
    return "other"


def sprint_id(value) -> int | None:
    """The numeric id out of a Jira Sprint field value, in any of its shapes."""
    if value in (None, "", [], {}):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        raw = value.get("id")
        return int(raw) if str(raw or "").isdigit() else None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = sprint_id(item)
            if found is not None:
                return found
        return None
    text = str(value)
    if text.isdigit():
        return int(text)
    match = _SPRINT_ID.search(text)
    return int(match.group(1)) if match else None


def user_of(record: dict | None) -> dict:
    """Normalise a Jira user record to the shape the identity table expects."""
    if not record:
        return {}
    return {
        "name": record.get("name") or record.get("accountId") or "",
        "displayName": record.get("displayName") or "",
        "emailAddress": record.get("emailAddress") or "",
        "accountType": record.get("accountType") or "",
        "active": record.get("active", True),
    }


@dataclass
class JiraClient:
    transport: object
    page_limit: int = 50
    min_interval: float = 0.0  # politeness delay between requests
    list_page_limit: int = LIST_PAGE_LIMIT
    requests: int = 0
    requests_by_kind: Counter = field(default_factory=Counter)
    _last_request: float = 0.0

    # -- low level -------------------------------------------------------
    def _get(self, path: str, params: dict | None = None, kind: str | None = None) -> dict:
        self.requests_by_kind[kind or request_kind(path)] += 1
        if self.min_interval:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        self._last_request = time.monotonic()
        self.requests += 1
        try:
            return self.transport.get(path, params)
        except JiraError:
            raise
        except Exception as exc:  # transport errors are all "the source said no"
            raise JiraError(f"GET {path} failed: {exc}") from exc

    def _pages(
        self,
        path: str,
        params: dict | None = None,
        *,
        kind: str | None = None,
        page_limit: int | None = None,
    ) -> Iterator[dict]:
        """Walk a Jira paged collection (``search``: ``issues``; agile: ``values``)."""
        limit = self.page_limit if page_limit is None else page_limit
        start = 0
        while True:
            page = self._get(path, {**(params or {}), "startAt": start, "maxResults": limit}, kind=kind)
            values = None
            for key in ("issues", "values", "worklogs"):
                if key in page:
                    values = page.get(key)
                    break
            values = list(values or [])
            yield from values
            start += len(values)
            if page.get("isLast") is True:
                return
            total = page.get("total")
            if total is not None and start >= int(total):
                return
            if not values:
                return

    # -- metadata --------------------------------------------------------
    def check(self) -> dict:
        """Cheap connectivity probe; never called by fixture mode."""
        return self._get(f"{API2}/serverInfo", kind="serverinfo")

    def fields(self) -> list[dict]:
        return list(self._get(f"{API2}/field", kind="fields") or [])

    def field_ids(self) -> dict[str, str]:
        """Jira field *name* -> field id (``Story Points`` -> ``customfield_10106``)."""
        return {
            str(entry.get("name") or ""): str(entry.get("id") or "")
            for entry in self.fields()
            if entry.get("name") and entry.get("id")
        }

    def status_categories(self) -> dict[str, str]:
        """Status id -> status *category* key (``new``/``indeterminate``/``done``).

        The category is what crosses into the store; the workflow's own status
        names never do (prd/02 "Status / assignee / sprint / points").
        """
        out: dict[str, str] = {}
        for entry in self._get(f"{API2}/status", kind="statuses") or []:
            category = (entry.get("statusCategory") or {}).get("key")
            if entry.get("id") and category:
                out[str(entry["id"])] = str(category)
        return out

    # -- agile -----------------------------------------------------------
    def boards(self, project: str) -> list[dict]:
        path = f"{AGILE}/board"
        return list(self._pages(path, {"projectKeyOrId": project}, kind="boards"))

    def scrum_board(self, project: str) -> int | None:
        """The project's scrum board id (sprints only exist on scrum boards)."""
        boards = self.boards(project)
        for board in boards:
            if str(board.get("type") or "").lower() == "scrum":
                return int(board["id"])
        return int(boards[0]["id"]) if boards else None

    def sprints(self, board_id: int) -> list[dict]:
        path = f"{AGILE}/board/{board_id}/sprint"
        return list(
            self._pages(path, {"state": "future,active,closed"}, kind="sprints")
        )

    def sprint_issue_keys(self, sprint_id: int) -> list[str]:
        path = f"{AGILE}/sprint/{sprint_id}/issue"
        return [
            str(issue.get("key"))
            for issue in self._pages(path, {"fields": "key"}, kind="sprint-issues")
            if issue.get("key")
        ]

    # -- issues ----------------------------------------------------------
    def search(self, jql: str, fields: str = "key,updated") -> Iterator[dict]:
        """Issue rows matching ``jql`` (``key`` + ``updated`` by default)."""
        path = f"{API2}/search"
        yield from self._pages(
            path,
            {"jql": jql, "fields": fields, "validateQuery": "false"},
            kind="search",
            page_limit=self.list_page_limit,
        )

    def issue(self, key: str, *, changelog: bool = True) -> dict:
        """One issue with its full history (``expand=changelog``)."""
        path = f"{API2}/issue/{key}"
        params = {"expand": "changelog"} if changelog else None
        return self._get(path, params, kind="issue")

    def worklogs(self, key: str) -> list[dict]:
        path = f"{API2}/issue/{key}/worklog"
        return list(self._pages(path, kind="worklog"))

    def inline_worklogs(self, issue: dict) -> list[dict]:
        """Worklogs already inside an issue's ``fields`` (search projection)."""
        block = (issue.get("fields") or {}).get("worklog") or {}
        return list(block.get("worklogs") or [])

    def complete_worklogs(self, issue: dict) -> list[dict]:
        """Every worklog of ``issue``: the inline page when it is complete.

        ``fields=worklog`` returns at most one page (20) of logs. Only when that
        page is truncated does the scrape pay for the worklog endpoint -- the
        common case is a single request per issue either way.
        """
        inline = self.inline_worklogs(issue)
        block = (issue.get("fields") or {}).get("worklog") or {}
        total = block.get("total")
        if total is None or len(inline) >= int(total):
            return inline
        return self.worklogs(str(issue.get("key")))


def sprint_state(value: str | None) -> str:
    """Jira Agile sprint state -> the closed ``SPRINT_STATE`` label."""
    state = str(value or "").strip().lower()
    return state if state in ("future", "active", "closed") else "future"


__all__ = [
    "AGILE",
    "API2",
    "FIELD_EPIC_LINK",
    "FIELD_SPRINT",
    "FIELD_STORY_POINTS",
    "JiraClient",
    "JiraError",
    "LIST_PAGE_LIMIT",
    "request_kind",
    "sprint_id",
    "sprint_state",
    "user_of",
]

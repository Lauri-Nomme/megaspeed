"""Transports for the Bitbucket Data Center client.

Two implementations, one interface:

* :class:`HttpTransport` — the real REST API (used only with credentials).
* :class:`FixtureTransport` — serves a synthetic bundle from the exact same
  paths, including paging envelopes, so the fixture run exercises the client's
  paging, URL building and parsing code rather than bypassing it.

Nothing here writes raw content to disk: responses stay in memory and are
reduced to labels by the pipeline.
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urlencode

API = "/rest/api/1.0"
_PR_PATH = re.compile(
    r"^" + API + r"/projects/(?P<proj>[^/]+)/repos/(?P<repo>[^/]+)/pull-requests"
    r"(?:/(?P<pr>\d+))?(?:/(?P<sub>activities|commits))?$"
)
_BUILD_STATUS_PATH = re.compile(
    r"^/rest/build-status/1\.0/commits/(?P<sha>[0-9a-f]{7,40})$"
)


class BitbucketError(RuntimeError):
    pass


#: Failures that are worth another attempt, with the same exponential backoff.
#:
#: ``ConnectionResetError`` and ``BrokenPipeError`` are the important ones: DC
#: under load resets the connection from inside ``http.client.getresponse()``
#: (and a reset mid-body surfaces while ``response.read()`` runs), and both are
#: plain ``OSError``s -- *not* ``urllib.error.URLError``. Without them a single
#: reset escaped :meth:`HttpTransport.get` and killed a two-hour scrape.
#: ``http.client.RemoteDisconnected`` is a ``ConnectionResetError`` subclass, so
#: a server that drops the connection without a response is covered too.
RETRYABLE = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    ConnectionResetError,
    BrokenPipeError,
)


class FixtureTransport:
    """An in-memory stand-in for the DC REST API."""

    def __init__(self, bundle: dict):
        self.bundle = bundle
        self._index = {
            (proj["key"], repo["slug"]): repo["pull_requests"]
            for proj in bundle.get("projects", [])
            for repo in proj.get("repos", [])
        }

    def get(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        # prd/44 DES-2: commit build statuses are a separate DC resource. The
        # fixture bundle may carry ``build_statuses`` keyed by commit sha; an
        # absent key is an empty page (the corpora have no CI data).
        match = _BUILD_STATUS_PATH.match(path)
        if match:
            values = (self.bundle.get("build_statuses") or {}).get(match["sha"], [])
            return _page(list(values), int(params.get("start", 0)),
                         int(params.get("limit", 25)))
        match = _PR_PATH.match(path)
        if not match:
            raise BitbucketError(f"fixture has no route for {path}")
        key = (match["proj"], match["repo"])
        if key not in self._index:
            raise BitbucketError(f"fixture has no repo {key[0]}/{key[1]}")
        prs = self._index[key]
        pr_id, sub = match["pr"], match["sub"]
        limit = int(params.get("limit", 25))
        start = int(params.get("start", 0))

        if pr_id is None:
            if sub:
                raise BitbucketError(f"bad fixture path {path}")
            return _page([p["pull_request"] for p in prs], start, limit)
        record = next((p for p in prs if int(p["pull_request"]["id"]) == int(pr_id)), None)
        if record is None:
            raise BitbucketError(f"fixture has no PR {key[0]}/{key[1]}#{pr_id}")
        if sub is None:
            return record["pull_request"]
        return _page(record[sub], start, limit)


def _page(values: list[Any], start: int, limit: int) -> dict:
    window = values[start : start + limit]
    is_last = start + limit >= len(values)
    page = {"size": len(window), "limit": limit, "isLastPage": is_last, "values": window, "start": start}
    if not is_last:
        page["nextPageStart"] = start + limit
    return page


@dataclass
class HttpTransport:
    """Thin, rate-limit-friendly DC REST transport (bearer or basic auth).

    Stdlib only (``urllib``): live mode must not add a dependency that fixture
    mode never uses, and ``requests`` buys us nothing at this size.
    """

    base_url: str
    token: str = ""
    user: str = ""
    password: str = ""
    verify_tls: bool = True
    max_retries: int = 3
    timeout: float = 30.0

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.user:
            raw = f"{self.user}:{self.password}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        return headers

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict | None = None) -> dict:
        """One JSON POST (Cloud's approximate-count is the only caller today)."""
        return self._request("POST", path, body=body)

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None) -> dict:
        url = self.base_url + path + (("?" + urlencode(params)) if params else "")
        payload = json.dumps(body).encode() if body is not None else None
        headers = self._headers()
        if payload is not None:
            headers["Content-Type"] = "application/json"
        last: Exception | None = None
        for attempt in range(self.max_retries):
            request = urllib.request.Request(
                url, data=payload, headers=headers, method=method
            )
            context = None
            if url.startswith("https") and not self.verify_tls:
                context = ssl._create_unverified_context()  # noqa: SLF001 - explicit opt-in
            try:
                with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                    return json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504):
                    last = exc
                    time.sleep(1.5 ** attempt)
                    continue
                raise BitbucketError(f"{exc.code} {exc.reason} for {path}") from exc
            except RETRYABLE as exc:
                # A reset/abort from getresponse()/read() lands here (it is an
                # OSError, not a URLError): retry it instead of killing the run.
                last = exc
                time.sleep(1.5 ** attempt)
        raise BitbucketError(f"{method} {path} failed after {self.max_retries} attempts: {last}")

    def check(self) -> dict:
        """Cheap connectivity probe — deliberately never called by fixture mode."""
        return self.get(API + "/application-properties")


def iter_pages(transport, path: str, params: dict | None = None, page_limit: int = 25) -> Iterator[dict]:
    """Yield individual values from a DC paged collection."""
    start = 0
    while True:
        page = transport.get(path, {**(params or {}), "start": start, "limit": page_limit})
        yield from page.get("values", [])
        if page.get("isLastPage", True):
            return
        start = page.get("nextPageStart", start + page_limit)


# --------------------------------------------------------------------------- #
# Jira fixture transport
# --------------------------------------------------------------------------- #
_JIRA_ISSUE = re.compile(r"^/rest/api/2/issue/(?P<key>[^/]+?)(?:/(?P<sub>worklog))?$")
_JIRA_SPRINT_ISSUES = re.compile(r"^/rest/agile/1\.0/sprint/(?P<id>\d+)/issue$")
_JIRA_BOARD_SPRINTS = re.compile(r"^/rest/agile/1\.0/board/(?P<id>\d+)/sprint$")
_JQL_KEY_IN = re.compile(r"key\s+in\s*\(([^)]*)\)", re.I)
_JQL_PROJECT = re.compile(r"project\s*(?:=\s*|in\s*\()\s*\"?([A-Za-z0-9_]+)\"?", re.I)
#: prd/66: ``project in ("A","B")`` -- the team search AND-ed with a project list.
_JQL_PROJECT_LIST = re.compile(r"project\s+in\s*\(([^)]*)\)", re.I)
_JQL_UPDATED = re.compile(r"updated\s*>=\s*\"?([^\"]+)\"?", re.I)
#: prd/27: the one search that resolves an epic set to its children.
_JQL_EPIC_LINK = re.compile(r"\"?\s*Epic Link\s*\"?\s+in\s*\(([^)]*)\)", re.I)
#: prd/66: a custom-field ``in`` clause, by display name or ``cf[<id>]``.
_JQL_CUSTOM_IN = re.compile(r"(?:\"([^\"]+)\"|cf\[(\d+)\])\s+in\s*\(([^)]*)\)", re.I)


def _jql_time(value: str) -> datetime:
    text = value.strip().replace("Z", "")
    for pattern in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M", "%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _jql_filter(issues: list[dict], jql: str, epic_field: str = "customfield_10100",
                fields: list[dict] | None = None) -> list[dict]:
    """The tiny subset of JQL this fixture honours (keys, project, updated, epic, custom)."""
    selected = list(issues)
    match = _JQL_KEY_IN.search(jql or "")
    if match:
        wanted = {
            token.strip().strip('"').upper() for token in match.group(1).split(",") if token.strip()
        }
        selected = [i for i in selected if str(i.get("key", "")).upper() in wanted]
    else:
        match = _JQL_PROJECT_LIST.search(jql or "")
        if match:
            wanted = {
                _jql_unquote(token.strip()).upper()
                for token in _split_jql_values(match.group(1))
                if token.strip()
            }
            selected = [
                i
                for i in selected
                if str(i.get("key", "")).partition("-")[0].upper() in wanted
            ]
        else:
            match = _JQL_PROJECT.search(jql or "")
            if match:
                project = match.group(1).strip().upper()
                selected = [
                    i
                    for i in selected
                    if str(i.get("key", "")).upper().startswith(project + "-")
                ]
    # prd/27: ``"Epic Link" in (PROJ-1)`` -- the epic set resolved to its children.
    match = _JQL_EPIC_LINK.search(jql or "")
    if match:
        wanted = {
            token.strip().strip('"').upper() for token in match.group(1).split(",") if token.strip()
        }
        selected = [
            i
            for i in selected
            if str((i.get("fields") or {}).get(epic_field) or "").upper() in wanted
        ]
    match = _JQL_UPDATED.search(jql or "")
    if match:
        cutoff = _jql_time(match.group(1))
        selected = [
            i
            for i in selected
            if _jql_time(str((i.get("fields") or {}).get("updated") or "1970-01-01")) >= cutoff
        ]
    # prd/66: a custom-field clause (``cf[10111] in ("Platform")`` or
    # ``"Team" in ("Platform")``), matched by the field's *value* (a string, or
    # an object/list whose ``name`` carries it). This is what lets a team-scoped
    # fixture run resolve its key set exactly like a real installation.
    by_name = {
        str(entry.get("name") or "").strip().lower(): str(entry.get("id") or "")
        for entry in (fields or ())
        if entry.get("name") and entry.get("id")
    }
    for clause in _JQL_CUSTOM_IN.finditer(jql or ""):
        name, cf_id, values = clause.group(1), clause.group(2), clause.group(3)
        if cf_id:
            field_id = f"customfield_{cf_id}"
        else:
            field_id = by_name.get(str(name or "").strip().lower(), "")
        if not field_id or field_id == epic_field:
            continue    # key/project/issuetype, or the epic clause handled above
        wanted = {
            _jql_unquote(token.strip()).lower()
            for token in _split_jql_values(values)
            if token.strip()
        }
        selected = [
            i for i in selected
            if _field_names((i.get("fields") or {}).get(field_id)) & wanted
        ]
    return selected


def _jql_unquote(value: str) -> str:
    """A JQL literal without its quotes/escapes, for case-folded comparison."""
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.replace('\\"', '"').replace("\\\\", "\\")


def _split_jql_values(value: str) -> list[str]:
    """Split ``"A","B, C"`` into its literals (a comma inside quotes is data)."""
    out: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == '"':
            current.append(char)
            quoted = not quoted
        elif char == "," and not quoted:
            out.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        out.append("".join(current))
    return out


def _field_names(value) -> set[str]:
    """Case-folded names a field value can be compared by (object/list/string)."""
    if isinstance(value, list):
        names: set[str] = set()
        for item in value:
            names |= _field_names(item)
        return names
    if isinstance(value, dict):
        for key in ("name", "value"):
            if value.get(key):
                return {str(value[key]).strip().lower()}
        return set()
    if value in (None, ""):
        return set()
    return {str(value).strip().lower()}


class FixtureJiraTransport:
    """An in-memory stand-in for the Jira DC + Agile REST API.

    Serves the exact paths :class:`ingest.jira.JiraClient` builds, including the
    paging envelopes (``startAt``/``maxResults``/``total``) and the Greenhopper
    Sprint field blob, so fixture mode exercises the real parsing path.
    """

    def __init__(self, bundle: dict):
        self.bundle = bundle
        self._issues = {str(i["key"]): i for i in bundle.get("issues", [])}
        self._sprints = {int(s["id"]): s for s in bundle.get("sprints", [])}
        self._sprint_issues = {
            int(sid): [str(k) for k in keys]
            for sid, keys in (bundle.get("sprint_issues") or {}).items()
        }
        # prd/27: the fixture resolves "Epic Link" by *name*, exactly like a real
        # installation discovers the field id (it is not portable).
        self._epic_field = next(
            (
                str(field["id"])
                for field in bundle.get("fields", [])
                if str(field.get("name") or "") == "Epic Link" and field.get("id")
            ),
            "customfield_10100",
        )

    def get(self, path: str, params: dict | None = None) -> dict:
        params = params or {}
        if path == "/rest/api/2/serverInfo":
            return {"version": "9.17.5-fixture", "versionNumbers": [9, 17, 5]}
        if path == "/rest/api/2/field":
            return list(self.bundle.get("fields", []))
        if path == "/rest/api/2/status":
            return list(self.bundle.get("statuses", []))
        if path == "/rest/api/2/search":
            rows = _jql_filter(
                list(self._issues.values()), str(params.get("jql") or ""), self._epic_field,
                self.bundle.get("fields", []),
            )
            rows.sort(key=lambda i: (str((i.get("fields") or {}).get("updated") or ""), i["key"]))
            fields = [f.strip() for f in str(params.get("fields") or "key").split(",")]
            projected = [_project_issue(i, fields) for i in rows]
            return _jira_page(projected, params, "issues")
        if path == "/rest/agile/1.0/board":
            project = str(params.get("projectKeyOrId") or "").upper()
            boards = [
                b
                for b in self.bundle.get("boards", [])
                if not project or str(b.get("name", "")).upper().startswith(project)
            ]
            return _jira_page(boards, params, "values")
        match = _JIRA_BOARD_SPRINTS.match(path)
        if match:
            board_id = int(match["id"])
            sprints = [
                s for s in self._sprints.values() if int(s.get("originBoardId") or 0) == board_id
            ]
            return _jira_page(sorted(sprints, key=lambda s: int(s["id"])), params, "values")
        match = _JIRA_SPRINT_ISSUES.match(path)
        if match:
            keys = self._sprint_issues.get(int(match["id"]), [])
            rows = [_project_issue(self._issues[k], ["key"]) for k in keys if k in self._issues]
            return _jira_page(rows, params, "issues")
        match = _JIRA_ISSUE.match(path)
        if match:
            key = match["key"]
            issue = self._issues.get(key)
            if issue is None:
                raise BitbucketError(f"fixture has no issue {key}")
            if match["sub"] == "worklog":
                worklogs = ((issue.get("fields") or {}).get("worklog") or {}).get("worklogs") or []
                return _jira_page(list(worklogs), params, "worklogs")
            payload = json.loads(json.dumps(issue))
            if "changelog" not in str(params.get("expand") or ""):
                payload.pop("changelog", None)
            return payload
        raise BitbucketError(f"fixture has no Jira route for {path}")


def _project_issue(issue: dict, fields: list[str]) -> dict:
    """A search row carries only the requested fields (plus the key)."""
    row = {"id": issue.get("id"), "key": issue["key"], "self": issue.get("self")}
    source = issue.get("fields") or {}
    row["fields"] = {name: source.get(name) for name in fields if name in source}
    return row


def _jira_page(values: list[Any], params: dict, key: str) -> dict:
    start = int(params.get("startAt", 0))
    limit = int(params.get("maxResults", 50))
    window = values[start : start + limit]
    page = {"startAt": start, "maxResults": limit, "total": len(values)}
    page[key] = window
    if key != "issues":
        page["isLast"] = start + limit >= len(values)
    return page

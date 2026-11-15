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
from typing import Any, Iterator
from urllib.parse import urlencode

API = "/rest/api/1.0"
_PR_PATH = re.compile(
    r"^" + API + r"/projects/(?P<proj>[^/]+)/repos/(?P<repo>[^/]+)/pull-requests"
    r"(?:/(?P<pr>\d+))?(?:/(?P<sub>activities|commits))?$"
)


class BitbucketError(RuntimeError):
    pass


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
        url = self.base_url + path + (("?" + urlencode(params)) if params else "")
        last: Exception | None = None
        for attempt in range(self.max_retries):
            request = urllib.request.Request(url, headers=self._headers(), method="GET")
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
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                time.sleep(1.5 ** attempt)
        raise BitbucketError(f"GET {path} failed after {self.max_retries} attempts: {last}")

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

"""Jira **Cloud** REST client (v3, Bearer PAT, cursor pagination).

The Cloud sibling of :mod:`ingest.jira`. It is a *shape* adapter, not a second
pipeline: it builds the same requests :class:`ingest.jira.JiraClient` builds,
against Cloud's surface, and returns the same payloads, so
:mod:`ingest.jira_pipeline` reduces both without a branch.

What is different (prd/24):

* **Base path** -- ``/rest/api/3`` instead of ``/rest/api/2``.
* **Auth** -- a Bearer PAT (``MEGASPEED_JIRA_CLOUD_TOKEN``), never Basic.
* **Search** -- ``/rest/api/3/search/jql`` is *cursor* paged
  (``nextPageToken`` / ``isLast``), where DC pages with ``startAt``.
* **Custom-field ids** -- Cloud's documented ids (``customfield_10014`` Epic
  Link, ``customfield_10020`` Sprint, ``customfield_10016`` Story Points) are
  still *discovered* from ``/rest/api/3/field``, and the documented ids are only
  a fallback for a site whose field list hides one of them.
* **Users** -- ``accountId`` rather than ``name``; :func:`ingest.jira.user_of`
  already normalises both.
* **Worklogs** -- native Cloud worklogs, or the Tempo Cloud add-on
  (``/rest/tempo-timesheets/4/worklogs/{key}``) when ``tempo=True``.

Agile paths (board / sprint / sprint issues) are identical on both deployments,
so they are inherited unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .jira import JiraClient

#: Cloud's REST namespace. DC is ``/rest/api/2`` (prd/24).
CLOUD_API = "/rest/api/3"
#: The modern Cloud search: cursor paged, no ``startAt``/``total``.
CLOUD_SEARCH = "/rest/api/3/search/jql"
#: Tempo Cloud's worklog API (an add-on; the DC lab has no Tempo).
CLOUD_TEMPO = "/rest/tempo-timesheets/4"

#: Cloud's *documented* custom-field ids, used only when ``/field`` does not
#: report the field under that name. The real mapping is always discovered.
CLOUD_FIELD_IDS: dict[str, str] = {
    "Epic Link": "customfield_10014",
    "Sprint": "customfield_10020",
    "Story Points": "customfield_10016",
}


@dataclass
class CloudJiraClient(JiraClient):
    """Cloud v3 client: same interface as :class:`JiraClient`, Cloud shapes."""

    api: str = CLOUD_API
    search_path: str = CLOUD_SEARCH
    #: Read worklogs through the Tempo Cloud stub instead of native Cloud worklogs.
    tempo: bool = False

    # -- metadata --------------------------------------------------------
    def check(self) -> dict:
        return self._get(f"{self.api}/serverInfo", kind="serverinfo")

    def fields(self) -> list[dict]:
        return list(self._get(f"{self.api}/field", kind="fields") or [])

    def field_ids(self) -> dict[str, str]:
        """Cloud field *name* -> Cloud field id, discovered (documented fallback)."""
        discovered = super().field_ids()
        out = dict(discovered)
        for name, cloud_id in CLOUD_FIELD_IDS.items():
            out.setdefault(name, cloud_id)
        return out

    def status_categories(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for entry in self._get(f"{self.api}/status", kind="statuses") or []:
            category = (entry.get("statusCategory") or {}).get("key")
            if entry.get("id") and category:
                out[str(entry["id"])] = str(category)
        return out

    # -- search (cursor paged) -------------------------------------------
    def _cursor_pages(
        self,
        path: str,
        params: dict | None = None,
        *,
        kind: str | None = None,
        page_limit: int | None = None,
    ) -> Iterator[dict]:
        """Walk a Cloud cursor collection (``issues`` + ``nextPageToken``)."""
        limit = self.page_limit if page_limit is None else page_limit
        token: str | None = None
        while True:
            query = {**(params or {}), "maxResults": limit}
            if token:
                query["nextPageToken"] = token
            page = self._get(path, query, kind=kind)
            values = list(page.get("issues") or [])
            yield from values
            token = page.get("nextPageToken")
            if page.get("isLast") is True or not token or not values:
                return

    def search(self, jql: str, fields: str = "key,updated") -> Iterator[dict]:
        """Issue rows matching ``jql`` (Cloud cursor pagination)."""
        yield from self._cursor_pages(
            self.search_path,
            {"jql": jql, "fields": fields},
            kind="search",
            page_limit=self.list_page_limit,
        )

    # -- issues ----------------------------------------------------------
    def issue(self, key: str, *, changelog: bool = True) -> dict:
        path = f"{self.api}/issue/{key}"
        params = {"expand": "changelog"} if changelog else None
        return self._get(path, params, kind="issue")

    def worklogs(self, key: str) -> list[dict]:
        if self.tempo:
            return self.tempo_worklogs(key)
        return list(self._pages(f"{self.api}/issue/{key}/worklog", kind="worklog"))

    def tempo_worklogs(self, key: str) -> list[dict]:
        """Tempo Cloud worklogs for one issue, in the native worklog shape.

        Tempo returns a bare JSON list keyed by ``worklogId``; the reducer reads
        ``id``, so the two are reconciled here rather than in the pipeline.
        """
        rows = self._get(f"{CLOUD_TEMPO}/worklogs/{key}", kind="worklog") or []
        out: list[dict] = []
        for row in rows:
            worklog = dict(row)
            if not worklog.get("id") and worklog.get("worklogId") is not None:
                worklog["id"] = str(worklog["worklogId"])
            out.append(worklog)
        return out

    # Agile paths (board / sprint / sprint issues) are byte-identical on Cloud
    # and DC, so ``boards``/``sprints``/``sprint_issue_keys`` are inherited.


__all__ = ["CLOUD_API", "CLOUD_FIELD_IDS", "CLOUD_SEARCH", "CLOUD_TEMPO", "CloudJiraClient"]

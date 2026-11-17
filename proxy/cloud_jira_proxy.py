"""A Cloud-shaped Jira API in front of a Jira **Data Center** lab (prd/24).

Atlassian Cloud is SaaS-only, so there is no local Cloud install to scrape
against. This module emulates the slice of the Cloud API surface the ingest
actually uses, in front of the DC lab, so the ``--jira-cloud`` scrape can be
developed and tested locally against real-shaped responses:

```
python3 -m proxy.cloud_jira_proxy --upstream http://127.0.0.1:8090 \\
        --port 8091 --pat cloud-lab-pat \\
        --user admin --password admin-lab-pw
```

What it translates (and why it has to):

| Cloud request | DC request | Why it differs |
| --- | --- | --- |
| ``Authorization: Bearer <PAT>`` | ``Basic admin:...`` | Cloud uses PATs, DC uses Basic |
| ``/rest/api/3/...`` | ``/rest/api/2/...`` | Cloud is v3, DC is v2 |
| ``/rest/api/3/search/jql?nextPageToken=`` | ``/rest/api/2/search?startAt=`` | Cloud's modern search is cursor-paged |
| ``customfield_10014/10020/10016`` | discovered DC ids | field ids are per-installation |
| ``/rest/tempo-timesheets/4/worklogs/{key}`` | ``/rest/api/2/issue/{key}/worklog`` | Tempo is an add-on, absent from the lab |

The **field mapping is discovered**, never hardcoded: Cloud's documented ids
(``customfield_10014`` Epic Link, ``customfield_10020`` Sprint,
``customfield_10016`` Story Points, ``customfield_10011`` Epic Name) are bound to
whatever id the DC lab reports for that *name* on ``/rest/api/2/field``. A
different lab, or a re-seeded one, therefore needs no code change.

Shape differences this proxy papers over, on top of the table above:

* **Users**: Cloud identifies people by ``accountId`` and hides ``emailAddress``;
  DC returns ``name`` + ``key`` + the address. The proxy rewrites the record to
  the Cloud shape (``accountId`` = the DC username, which keeps the identity
  key stable, and no e-mail). The ingest already reads both shapes.
* **The Sprint field**: DC returns a serialized Greenhopper blob
  (``com.atlassian.greenhopper...Sprint@…[id=3,name=…]``), Cloud a list of
  sprint objects. The proxy converts it (the ingest only needs ``id``; sprint
  membership comes from the changelog and the agile API).
* **Changelog items**: Cloud includes ``fieldId`` next to ``field``; DC 9.17
  does not. The proxy adds it for the mapped custom fields.
* **``self`` URLs**: rewritten from the DC base to the proxy's own base, so a
  response looks like it came from the Cloud site it claims to be.
* **``serverInfo``**: ``deploymentType`` is ``Cloud`` (Cloud has no
  ``deploymentType`` field at all; the ingest only reads ``version``).
* **Tempo**: a worklog stub over the native worklogs. Tempo's ``comment`` is
  deliberately *not* mapped: the ingest never reads it and it is raw text, so
  the stub must not be a raw-text path.

The proxy is a **development tool in the raw zone**: it sees raw Jira payloads
and must never be deployed next to the store. It is stdlib-only so it runs
anywhere the scraper does.
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CLOUD_API = "/rest/api/3"
DC_API = "/rest/api/2"
AGILE = "/rest/agile/1.0"
TEMPO = "/rest/tempo-timesheets/4"

#: Cloud's *documented* custom-field ids. These are the ids a Cloud site's
#: ``/rest/api/3/field`` reports for the classic project fields the ingest
#: needs; the DC side is discovered by name at start-up.
CLOUD_FIELDS: dict[str, str] = {
    "Epic Link": "customfield_10014",
    "Sprint": "customfield_10020",
    "Story Points": "customfield_10016",
    "Epic Name": "customfield_10011",
}

_ISSUE_PATH = re.compile(r"^" + CLOUD_API + r"/issue/(?P<key>[^/]+?)(?:/worklog)?$")
_TEMPO_WORKLOGS = re.compile(r"^" + TEMPO + r"/worklogs/(?P<key>[^/]+)$")
_FIELD_ID = re.compile(r"^customfield_\d+$")
#: The DC Sprint field is a serialized Greenhopper blob; Cloud returns a list of
#: sprint objects. Only the keys the ingest can ever see are extracted.
_SPRINT_PAIR = re.compile(r"(\w+)=([^,\]]*)")


class UpstreamError(RuntimeError):
    """The DC lab answered with an error (or not at all)."""


class RouteError(UpstreamError):
    """The request is not part of the Cloud surface this proxy emulates."""


class FieldMap:
    """Cloud documented field id <-> the DC lab's discovered id, by field *name*.

    ``bind`` is what makes this an emulation rather than a guess: the DC
    installation reports ``{"name": "Story Points", "id": "customfield_10106"}``
    and we attach the Cloud id for that name to it. Nothing here assumes a DC id.
    """

    def __init__(self, dc_by_name: dict[str, str] | None = None):
        self.by_name: dict[str, str] = dict(CLOUD_FIELDS)
        self.cloud_to_dc: dict[str, str] = {}
        self.dc_to_cloud: dict[str, str] = {}
        self.dc_by_name: dict[str, str] = {}
        if dc_by_name:
            self.bind(dc_by_name)

    def bind(self, dc_by_name: dict[str, str]) -> "FieldMap":
        self.dc_by_name = dict(dc_by_name)
        self.cloud_to_dc.clear()
        self.dc_to_cloud.clear()
        for name, cloud_id in self.by_name.items():
            dc_id = self.dc_by_name.get(name)
            if not dc_id:
                continue
            self.cloud_to_dc[cloud_id] = dc_id
            self.dc_to_cloud[dc_id] = cloud_id
        return self

    # -- ids -------------------------------------------------------------
    def to_dc(self, field_id: Any) -> Any:
        return self.cloud_to_dc.get(str(field_id), field_id)

    def to_cloud(self, field_id: Any) -> Any:
        return self.dc_to_cloud.get(str(field_id), field_id)

    def cloud_id_for_name(self, name: Any) -> str | None:
        cloud_id = self.by_name.get(str(name or "").strip())
        # Only hand out an id the DC side actually has: a changelog ``fieldId``
        # for a field the site does not expose would be a lie.
        return cloud_id if cloud_id in self.cloud_to_dc else None

    # -- request translation ---------------------------------------------
    def fields_param(self, value: Any) -> Any:
        """``customfield_10016,key`` -> ``customfield_10106,key`` (Cloud -> DC)."""
        if not value:
            return value
        parts = [str(value)] if isinstance(value, (list, tuple)) else str(value).split(",")
        mapped = [str(self.to_dc(part.strip())) for part in parts if str(part).strip()]
        return ",".join(mapped)

    def jql(self, text: Any) -> str:
        """Rewrite Cloud ``cf[10016]`` / ``customfield_10016`` references to DC ids."""
        out = str(text or "")
        for cloud_id, dc_id in self.cloud_to_dc.items():
            out = out.replace(f"cf[{cloud_id.partition('_')[2]}]", f"cf[{dc_id.partition('_')[2]}]")
            out = out.replace(cloud_id, dc_id)
        return out

    def as_dict(self) -> dict[str, dict[str, str]]:
        """Diagnostics: the bound map plus what the DC side reported, by name."""
        return {
            "cloud_to_dc": dict(sorted(self.cloud_to_dc.items())),
            "dc_by_name": dict(sorted(self.dc_by_name.items())),
        }


def _is_user(record: dict) -> bool:
    """A Jira user record: DC has ``name``+``displayName``, Cloud ``accountId``."""
    return "displayName" in record and ("name" in record or "accountId" in record or "key" in record)


def _flat(params: dict[str, list[str]] | None) -> dict[str, str]:
    """``parse_qs`` output -> a flat param dict (``fields`` may repeat)."""
    out: dict[str, str] = {}
    for key, values in (params or {}).items():
        if not values:
            continue
        if key == "fields":
            parts = [part.strip() for value in values for part in str(value).split(",") if part.strip()]
            if parts:
                out[key] = ",".join(parts)
        else:
            out[key] = str(values[-1])
    return out


@dataclass
class CloudJiraProxy:
    """Cloud request/response translation in front of a DC REST base URL."""

    upstream: str
    pat: str
    user: str = "admin"
    password: str = ""
    timeout: float = 15.0
    fields: FieldMap = field(default_factory=FieldMap)
    requests: int = 0
    #: Diagnostics: what the last discovery found, per field name.
    discovered: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ready: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.upstream = str(self.upstream).rstrip("/")

    # -- upstream --------------------------------------------------------
    def _authorisation(self) -> str:
        raw = f"{self.user}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def upstream_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.upstream + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "Authorization": self._authorisation()},
            method="GET",
        )
        self.requests += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:400]
            raise UpstreamError(f"{exc.code} {exc.reason} for {path}: {body}") from exc
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise UpstreamError(f"GET {path} failed: {exc}") from exc

    # -- discovery -------------------------------------------------------
    def discover(self) -> dict[str, str]:
        """Bind the Cloud documented ids to the DC lab's ids, by field name."""
        payload = self.upstream_get(f"{DC_API}/field")
        by_name = {
            str(entry.get("name") or ""): str(entry.get("id") or "")
            for entry in (payload or [])
            if entry.get("name") and entry.get("id")
        }
        self.fields.bind(by_name)
        self.discovered = {
            name: self.fields.cloud_to_dc[cloud_id]
            for name, cloud_id in self.fields.by_name.items()
            if cloud_id in self.fields.cloud_to_dc
        }
        self._ready = True
        return dict(self.discovered)

    def ensure_discovered(self) -> None:
        """Discover once; a failed discovery retries on the next request."""
        if self._ready:
            return
        with self._lock:
            if not self._ready:
                self.discover()

    # -- auth ------------------------------------------------------------
    def authorised(self, header: str | None) -> bool:
        """Bearer PAT only, exactly like Cloud (Basic is rejected)."""
        if not header:
            return False
        scheme, _, value = header.partition(" ")
        if scheme.strip().lower() != "bearer":
            return False
        return bool(self.pat) and hmac.compare_digest(value.strip(), self.pat)

    # -- response translation -------------------------------------------
    def _cloud_user(self, record: dict) -> dict:
        out = {k: v for k, v in record.items() if k not in ("name", "key", "emailAddress")}
        out["accountId"] = str(record.get("accountId") or record.get("name") or record.get("key") or "")
        out.setdefault("accountType", "atlassian")
        return out

    def _sprint_objects(self, value: Any) -> Any:
        """DC Greenhopper Sprint blob -> the Cloud list of sprint objects.

        The ingest only ever needs the ``id`` (membership comes from the
        changelog and the agile API), but the shape is what makes a response
        look like Cloud's, and a comma inside a goal would truncate the rest of
        the blob -- harmless here, and noted so nobody relies on ``goal``.
        """
        items = value if isinstance(value, list) else [value]
        out: list[dict] = []
        for item in items:
            if item in (None, "", {}):
                continue
            if isinstance(item, dict):
                out.append(item)
                continue
            pairs = dict(_SPRINT_PAIR.findall(str(item)))
            if not pairs.get("id", "").isdigit():
                continue
            sprint: dict[str, Any] = {
                "id": int(pairs["id"]),
                "name": pairs.get("name") or "",
                "state": str(pairs.get("state") or "").lower(),
            }
            for key, target in (("rapidViewId", "boardId"), ("startDate", "startDate"),
                                ("endDate", "endDate"), ("completeDate", "completeDate")):
                if pairs.get(key) and pairs[key] != "<null>":
                    sprint[target] = int(pairs[key]) if pairs[key].isdigit() else pairs[key]
            out.append(sprint)
        if not out and value in (None, "", [], {}):
            return None
        return out

    def translate(self, value: Any, public_base: str) -> Any:
        """Rewrite a DC payload into the Cloud shape (fields, users, URLs)."""
        if isinstance(value, dict):
            record = self._cloud_user(value) if _is_user(value) else value
            sprint_field = self.fields.cloud_id_for_name("Sprint")
            out: dict[str, Any] = {}
            for key, item in record.items():
                name = str(key)
                if _FIELD_ID.match(name):
                    # A DC-only custom field has no Cloud id here; a Cloud site
                    # would not report it under this id at all.
                    cloud_id = self.fields.dc_to_cloud.get(name)
                    if not cloud_id:
                        continue
                    name = cloud_id
                if name == sprint_field:
                    out[name] = self._sprint_objects(item)
                    continue
                out[name] = self.translate(item, public_base)
            return out
        if isinstance(value, list):
            return [self.translate(item, public_base) for item in value]
        if isinstance(value, str) and self.upstream:
            value = value.replace(self.upstream + DC_API, public_base + CLOUD_API)
            if self.upstream in value:
                value = value.replace(self.upstream, public_base)
            return value
        return value

    def translate_issue(self, issue: dict, public_base: str) -> dict:
        payload = self.translate(issue, public_base)
        for history in (payload.get("changelog") or {}).get("histories") or []:
            for item in history.get("items") or []:
                if item.get("fieldId"):
                    continue
                field_id = self.fields.cloud_id_for_name(item.get("field"))
                if field_id:
                    item["fieldId"] = field_id
        return payload

    def translate_fields(self, payload: Any) -> list[dict]:
        """The field list as a Cloud site would report it: Cloud ids, no DC-only customs."""
        out: list[dict] = []
        for entry in payload or []:
            field_id = str(entry.get("id") or "")
            if _FIELD_ID.match(field_id):
                cloud_id = self.fields.dc_to_cloud.get(field_id)
                if not cloud_id:
                    # A DC-only custom field has no Cloud counterpart here; the
                    # Cloud site would not have it under this id.
                    continue
                entry = {**entry, "id": cloud_id}
            out.append(entry)
        return out

    def translate_worklogs(self, payload: dict, public_base: str) -> dict:
        page = self.translate(payload, public_base)
        page.setdefault("startAt", 0)
        page.setdefault("maxResults", len(page.get("worklogs") or []))
        page.setdefault("total", len(page.get("worklogs") or []))
        return page

    def tempo_worklogs(self, key: str) -> list[dict]:
        """Tempo Cloud's ``GET /rest/tempo-timesheets/4/worklogs/{key}`` shape."""
        page = self.upstream_get(f"{DC_API}/issue/{key}/worklog")
        rows: list[dict] = []
        for worklog in (page or {}).get("worklogs") or []:
            author = self._cloud_user(worklog.get("author") or {}) if worklog.get("author") else {}
            rows.append(
                {
                    "worklogId": _int_or_raw(worklog.get("id")),
                    "issueId": _int_or_raw(worklog.get("issueId")),
                    "issue": {"key": key, "id": _int_or_raw(worklog.get("issueId"))},
                    "author": author,
                    "started": worklog.get("started"),
                    "created": worklog.get("created"),
                    "updated": worklog.get("updated"),
                    "timeSpentSeconds": worklog.get("timeSpentSeconds"),
                    # ``comment`` is raw text and the ingest never reads it, so
                    # the stub does not carry it (the proxy is raw-zone, but a
                    # needless raw-text path is a needless risk).
                }
            )
        return rows

    # -- request dispatch -------------------------------------------------
    def handle(self, path: str, params: dict[str, list[str]] | None, public_base: str) -> tuple[int, Any]:
        """Translate one Cloud request. Returns ``(status, json_payload)``."""
        self.ensure_discovered()
        query = _flat(params)

        if path == f"{CLOUD_API}/serverInfo":
            return 200, self._server_info()

        if path == f"{CLOUD_API}/field":
            return 200, self.translate_fields(self.upstream_get(f"{DC_API}/field"))

        if path == f"{CLOUD_API}/status":
            return 200, self.translate(self.upstream_get(f"{DC_API}/status"), public_base)

        if path in (f"{CLOUD_API}/search", f"{CLOUD_API}/search/jql"):
            return 200, self._search(path.endswith("/jql"), query, public_base)

        match = _ISSUE_PATH.match(path)
        if match:
            return 200, self._issue(path, match["key"], query, public_base)

        match = _TEMPO_WORKLOGS.match(path)
        if match:
            return 200, self.translate(self.tempo_worklogs(match["key"]), public_base)

        if path.startswith(AGILE):
            return 200, self.translate(self.upstream_get(path, query or None), public_base)

        if path.startswith(CLOUD_API):
            return 200, self.translate(self.upstream_get(self._dc_path(path), query or None), public_base)

        raise RouteError(f"the proxy has no route for {path}")

    def _dc_path(self, path: str) -> str:
        """``/rest/api/3/...`` -> ``/rest/api/2/...`` (everything else unchanged)."""
        return DC_API + path[len(CLOUD_API):] if path.startswith(CLOUD_API) else path

    def _server_info(self) -> dict:
        payload = dict(self.upstream_get(f"{DC_API}/serverInfo") or {})
        version = str(payload.get("version") or "unknown")
        payload["version"] = f"{version}-cloud-proxy"
        payload["deploymentType"] = "Cloud"
        return payload

    def _search(self, cursor: bool, query: dict[str, str], public_base: str) -> dict:
        dc = {key: value for key, value in query.items() if key != "nextPageToken"}
        if "fields" in dc:
            dc["fields"] = self.fields.fields_param(dc["fields"])
        if "jql" in dc:
            dc["jql"] = self.fields.jql(dc["jql"])
        dc["validateQuery"] = "false"
        if cursor:
            # Cloud's cursor search: nextPageToken -> DC startAt.
            dc["startAt"] = str(_token_offset(query.get("nextPageToken")))
        page = self.upstream_get(f"{DC_API}/search", dc)
        issues = [self.translate_issue(issue, public_base) for issue in page.get("issues") or []]
        if not cursor:
            # The legacy Cloud search envelope is the DC one (startAt/maxResults/
            # total/issues); only the field ids and users change.
            return {**page, "issues": issues}
        start = int(dc.get("startAt", 0))
        following = start + len(issues)
        total = int(page.get("total") or 0)
        is_last = not issues or following >= total
        out: dict[str, Any] = {"issues": issues, "isLast": is_last}
        if not is_last:
            out["nextPageToken"] = str(following)
        return out

    def _issue(self, path: str, key: str, query: dict[str, str], public_base: str) -> dict:
        dc_params = dict(query)
        if "fields" in dc_params:
            dc_params["fields"] = self.fields.fields_param(dc_params["fields"])
        if path.endswith("/worklog"):
            page = self.upstream_get(f"{DC_API}/issue/{key}/worklog", dc_params or None)
            return self.translate_worklogs(page or {}, public_base)
        issue = self.upstream_get(f"{DC_API}/issue/{key}", dc_params or None)
        return self.translate_issue(issue or {}, public_base)


def _int_or_raw(value: Any) -> Any:
    text = str(value if value is not None else "")
    return int(text) if text.isdigit() else value


def _token_offset(token: str | None) -> int:
    """The proxy's cursor is the DC ``startAt`` it stands for (opaque to clients)."""
    try:
        return max(0, int(str(token or "0")))
    except ValueError as exc:
        raise UpstreamError(f"bad nextPageToken {token!r}") from exc


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    server_version = "cloud-jira-proxy/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------
    @property
    def proxy(self) -> CloudJiraProxy:
        return self.server.proxy  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib hook
        if getattr(self.server, "verbose", False):
            sys.stderr.write("proxy: " + (fmt % args) + "\n")

    def _public_base(self) -> str:
        host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_address[1]}"
        return f"http://{host}"

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        parsed = urllib.parse.urlsplit(self.path)
        if not self.proxy.authorised(self.headers.get("Authorization")):
            # Cloud's own 401 body shape.
            self._send(401, {"errorMessages": ["You are not authenticated."], "errors": {}})
            return
        try:
            status, payload = self.proxy.handle(
                parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True), self._public_base()
            )
        except RouteError as exc:
            self._send(404, {"errorMessages": [str(exc)], "errors": {}})
            return
        except UpstreamError as exc:
            self._send(502, {"errorMessages": [str(exc)], "errors": {}})
            return
        except Exception as exc:  # noqa: BLE001 - a dev proxy must answer, not crash
            self._send(500, {"errorMessages": [f"proxy error: {exc}"], "errors": {}})
            return
        self._send(status, payload)


def make_server(proxy: CloudJiraProxy, host: str = "127.0.0.1", port: int = 0,
                verbose: bool = False) -> ThreadingHTTPServer:
    """A running (but not serving) proxy server; ``port=0`` picks a free one."""
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.proxy = proxy  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def base_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cloud-jira-proxy",
        description="Emulate the Cloud Jira API in front of a Jira DC lab (prd/24).",
    )
    ap.add_argument("--upstream", default=os.environ.get("MEGASPEED_JIRA_BASE_URL", "http://127.0.0.1:8090"),
                    help="DC lab base URL (default MEGASPEED_JIRA_BASE_URL or http://127.0.0.1:8090)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MEGASPEED_JIRA_CLOUD_PROXY_PORT", 8091)))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--pat", default=os.environ.get("MEGASPEED_JIRA_CLOUD_PROXY_PAT", ""),
                    help="Bearer PAT the proxy accepts (default MEGASPEED_JIRA_CLOUD_PROXY_PAT)")
    ap.add_argument("--user", default=os.environ.get("MEGASPEED_JIRA_USER", "admin"),
                    help="DC upstream username (default MEGASPEED_JIRA_USER or admin)")
    ap.add_argument("--password", default=os.environ.get("MEGASPEED_JIRA_PASSWORD", ""),
                    help="DC upstream password (default MEGASPEED_JIRA_PASSWORD)")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    args = ap.parse_args(argv)

    if not args.pat:
        ap.error("--pat (or MEGASPEED_JIRA_CLOUD_PROXY_PAT) is required: the proxy accepts Bearer PATs only")
    if not args.password:
        ap.error("--password (or MEGASPEED_JIRA_PASSWORD) is required for the DC upstream")

    proxy = CloudJiraProxy(args.upstream, pat=args.pat, user=args.user, password=args.password)
    try:
        discovered = proxy.discover()
    except UpstreamError as exc:
        print(f"warning: field discovery failed ({exc}); it will retry on the first request", file=sys.stderr)
        discovered = {}
    server = make_server(proxy, host=args.host, port=args.port, verbose=args.verbose)
    print(f"cloud-jira-proxy: {base_url(server)} -> {proxy.upstream}")
    print("field mapping (cloud -> dc): " + (", ".join(
        f"{cloud}={dc}" for cloud, dc in sorted(proxy.fields.cloud_to_dc.items())
    ) or "none discovered"))
    for name, dc_id in sorted(discovered.items()):
        print(f"  {name:14} {proxy.fields.by_name[name]} -> {dc_id}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

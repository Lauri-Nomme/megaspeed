"""Identify a Jira field (for example the team field) from one ticket.

    # what fields does this site have, and which one is "Team"?
    python3 -m ingest.jira_fields --list

    # what does one ticket actually carry? (raw zone; nothing is written)
    python3 -m ingest.jira_fields --issue DEL-100

    # the Cloud shape (v3 + cursor search), same output
    python3 -m ingest.jira_fields --cloud --issue DEL-100

Why this exists: **"Team" is not a standard Jira field.** It is the Atlassian
Teams / Advanced Roadmaps custom field (schema
``com.atlassian.teams:rm-teams-custom-field-team``); a site may rename it, and
its id (``customfield_10111`` on the lab) is per-installation. Guessing the id
is how a team-scoped scrape silently reads nothing, so the scraper discovers it
by name and this command shows what the site actually has.

The command is read-only and runs on the **raw side** (next to Jira): its output
may contain ticket text, so it is never part of a bundle and never reaches the
analysis zone. ``--json`` is for scripting; the default text output is aligned
for a human.

Configuration comes from the same environment as the scraper
(``MEGASPEED_JIRA_BASE_URL`` + credentials, or
``MEGASPEED_JIRA_CLOUD_BASE_URL`` + a Bearer PAT, or + the account e-mail and
API token), with
``--config`` for a TOML file.
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import Config, load_config
from .scrape_jira import (
    CLOUD_TEAM_SCHEMAS,
    _field_ref,
    _jql_string,
    jql_for_teams,
)

#: A value longer than this is truncated in the text output (the JSON keeps it).
VALUE_LIMIT = 200


def client_from_config(cfg: Config, *, cloud: bool):
    """Build the read-only client the scraper would use (no request yet)."""
    from .transport import HttpTransport

    if cloud:
        if not cfg.jira_cloud_base_url:
            raise SystemExit(
                "jira cloud mode needs MEGASPEED_JIRA_CLOUD_BASE_URL (and"
                " MEGASPEED_JIRA_CLOUD_TOKEN, or MEGASPEED_JIRA_CLOUD_USER +"
                " MEGASPEED_JIRA_CLOUD_API_TOKEN); in dev that is the proxy URL"
            )
        from .jira_cloud import CloudJiraClient

        transport = HttpTransport(
            cfg.jira_cloud_base_url,
            token=cfg.jira_cloud_token,
            user=cfg.jira_cloud_user,
            password=cfg.jira_cloud_api_token,
        )
        client = CloudJiraClient(
            transport, page_limit=cfg.jira_page_limit, tempo=cfg.jira_tempo
        )
    else:
        if not cfg.jira_base_url:
            raise SystemExit(
                "jira mode needs MEGASPEED_JIRA_BASE_URL (and"
                " MEGASPEED_JIRA_USER/MEGASPEED_JIRA_PASSWORD or MEGASPEED_JIRA_TOKEN)"
            )
        from .jira import JiraClient

        transport = HttpTransport(
            cfg.jira_base_url,
            token=cfg.jira_auth_token,
            user=cfg.jira_auth_user,
            password=cfg.jira_auth_password,
        )
        client = JiraClient(
            transport, page_limit=cfg.jira_page_limit, min_interval=0.05,
            tempo=cfg.jira_tempo,
        )
    return client


def _auth_method(cfg: Config, cloud: bool) -> str:
    """Which credentials the client will send, named but never printed."""
    if cloud:
        if cfg.jira_cloud_token:
            return "Bearer PAT (MEGASPEED_JIRA_CLOUD_TOKEN)"
        if cfg.jira_cloud_user:
            return (
                f"Basic {cfg.jira_cloud_user} "
                "(MEGASPEED_JIRA_CLOUD_API_TOKEN)"
            )
        return "none — no Cloud credentials are set"
    if cfg.jira_auth_token:
        return "Bearer PAT (MEGASPEED_JIRA_TOKEN)"
    if cfg.jira_auth_user:
        return f"Basic {cfg.jira_auth_user} (MEGASPEED_JIRA_PASSWORD)"
    return "none — no Jira credentials are set"


def auth_help(cfg: Config, *, cloud: bool, errors: list[str]) -> str:
    """The 403/401 diagnostic: what was sent, what to try, no secrets."""
    base = cfg.jira_cloud_base_url if cloud else cfg.jira_base_url
    method = _auth_method(cfg, cloud)
    lines = [
        f"cannot authenticate to {base}",
        *[f"  {error}" for error in errors],
        f"  auth sent: {method}",
        "next:",
    ]
    if cloud:
        lines += [
            "  1. Cloud API token (the usual method): set",
            "     MEGASPEED_JIRA_CLOUD_USER=<your account e-mail>",
            "     MEGASPEED_JIRA_CLOUD_API_TOKEN=<token from id.atlassian.com>",
            "  2. Cloud Personal Access Token: set MEGASPEED_JIRA_CLOUD_TOKEN",
            "     (Bearer); it must be enabled for the site and the account",
            "  3. Against the local emulation proxy, the PAT is the proxy's --pat,",
            "     not a Cloud token",
            "  4. A 403 on every path can also be an Atlassian IP allowlist / SSO",
            "     policy or a reverse proxy rejecting the request",
        ]
    else:
        lines += [
            "  1. MEGASPEED_JIRA_TOKEN (Bearer PAT), or",
            "     MEGASPEED_JIRA_USER + MEGASPEED_JIRA_PASSWORD (Basic)",
            "  2. a 403 on every path can be a reverse proxy or an IP allowlist",
        ]
    return "\n".join(lines)


def probe_auth(client, *, cloud: bool, cfg: Config) -> dict:
    """Prove the credentials work; fall back to ``/myself`` when ``serverInfo`` fails.

    ``/serverInfo`` is the cheap probe, but some Cloud sites and proxies block
    it for token users while ``/myself`` still works. A failed ``serverInfo``
    therefore is not fatal: identity + field reads are the real test. Only when
    both paths fail do we stop, with an actionable 401/403 diagnostic.
    """
    base = "/rest/api/3" if cloud else "/rest/api/2"
    errors: list[str] = []
    try:
        info = client.check() or {}
        return {"ok": True, "version": info.get("version"),
                "via": f"{base}/serverInfo", "warning": ""}
    except Exception as exc:  # transport/HTTP error, keep the detail
        errors.append(f"{base}/serverInfo: {exc}")
    try:
        me = client._get(f"{base}/myself", kind="myself") or {}   # noqa: SLF001
        who = (me.get("displayName") or me.get("emailAddress")
               or me.get("name") or me.get("accountId") or "unknown")
        return {
            "ok": True,
            "version": None,
            "via": f"{base}/myself",
            "account": who,
            "warning": (
                f"{errors[0]} — continuing via {base}/myself as {who}"
            ),
        }
    except Exception as exc:
        errors.append(f"{base}/myself: {exc}")
    raise SystemExit(auth_help(cfg, cloud=cloud, errors=errors))


def team_candidates(fields: list[dict]) -> list[dict]:
    """Fields that plausibly *are* the team field (name or schema says "team").

    The Atlassian Teams field is the one with
    ``schema.custom == "com.atlassian.teams:rm-teams-custom-field-team"``; a
    renamed field keeps that schema, and a custom team field of another type
    usually keeps "team" in its name.
    """
    out = []
    for entry in fields:
        name = str(entry.get("name") or "")
        schema = entry.get("schema") or {}
        haystack = f"{name} {schema.get('custom') or ''} {schema.get('type') or ''}".lower()
        if "team" in haystack:
            out.append({
                "id": str(entry.get("id") or ""),
                "name": name,
                "custom": bool(entry.get("custom")),
                "schema": dict(schema),
            })
    return out


def _team_value(value) -> tuple[str, str]:
    """(display name, team id) out of a Teams / select / multicheckbox value.

    The Cloud Teams field returns ``{id, name, avatarUrl, ...}``; a select
    returns ``{value, id}``; a multicheckbox returns a list of those. Showing the
    *name* first is what makes the field identifiable -- the raw JSON truncates
    before ``name`` and looks like an opaque UUID.
    """
    if isinstance(value, list):
        names: list[str] = []
        ids: list[str] = []
        for item in value:
            name, ident = _team_value(item)
            if name:
                names.append(name)
            if ident:
                ids.append(ident)
        return ", ".join(names), ", ".join(ids)
    if isinstance(value, dict):
        name = value.get("name") or value.get("value") or value.get("title")
        ident = value.get("id")
        return (str(name) if name else "", str(ident) if ident else "")
    if isinstance(value, str):
        return value, ""
    return "", ""


def _compact(value, limit: int = VALUE_LIMIT) -> str:
    """One field value, short enough for a table row (JSON keeps the full one)."""
    if value is None or value == "":
        return "— (null)"
    name, ident = _team_value(value)
    if name or (ident and not isinstance(value, (dict, list))):
        text = name or ident
        if ident and ident not in text:
            text = f"{text}  · id={ident}"
        return text if len(text) <= limit else text[: limit - 1] + "…"
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True) \
            if isinstance(value, (dict, list)) else str(value)
    except (TypeError, ValueError):
        text = repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _issue_type(issue: dict) -> str:
    value = (issue.get("fields") or {}).get("issuetype")
    if isinstance(value, dict):
        return str(value.get("name") or "?")
    return str(value or "?")


def inspect(client, issue_key: str | None = None, *, catalogue: bool = False,
            version=None, auth: dict | None = None) -> dict:
    """The field catalogue and (optionally) one ticket's field values.

    ``catalogue=True`` lists every field; an issue additionally reports the
    fields on that ticket's screen with their values. The team candidates are
    reported both ways, so the command still identifies the field when the
    ticket's value is null. ``version``/``auth`` come from :func:`probe_auth`
    so the report says which probe path proved the credentials.
    """
    fields = list(client.fields() or [])
    by_id = {str(entry.get("id") or ""): entry for entry in fields if entry.get("id")}
    report: dict = {
        "version": version,
        "auth": auth or {},
        "requests": client.requests,
        "scope": "cloud" if client.__class__.__name__.startswith("Cloud") else "dc",
        "fields": [],
        "team_candidates": team_candidates(fields),
        "issue": None,
        "hint": {},
    }
    if issue_key:
        issue = client.issue(issue_key, changelog=False)
        values = (issue.get("fields") or {})
        rows = []
        for field_id, value in values.items():
            meta = by_id.get(str(field_id)) or {}
            rows.append({
                "id": str(field_id),
                "name": str(meta.get("name") or ""),
                "value": value,
                "schema": dict(meta.get("schema") or {}),
                "null": value is None or value == "",
            })
        rows.sort(key=lambda row: (row["null"], row["name"].lower(), row["id"]))
        report["fields"] = rows
        report["issue"] = {
            "key": str(issue.get("key") or issue_key),
            "type": _issue_type(issue),
            "field_count": len(rows),
            "non_null": sum(1 for row in rows if not row["null"]),
        }
        for candidate in report["team_candidates"]:
            row = next((r for r in rows if r["id"] == candidate["id"]), None)
            candidate["value"] = row["value"] if row else None
            candidate["on_screen"] = row is not None
            candidate["null"] = bool(row["null"]) if row else True
            name, ident = _team_value(candidate["value"])
            candidate["value_name"] = name
            candidate["value_id"] = ident
    elif catalogue:
        report["fields"] = [
            {
                "id": str(entry.get("id") or ""),
                "name": str(entry.get("name") or ""),
                "value": None,
                "schema": dict(entry.get("schema") or {}),
                "null": True,
            }
            for entry in sorted(fields, key=lambda e: (str(e.get("name") or "").lower(), str(e.get("id") or "")))
        ]
        for candidate in report["team_candidates"]:
            candidate.setdefault("value", None)
            candidate["value_name"] = ""
            candidate["value_id"] = ""
            candidate["on_screen"] = False
            candidate["null"] = True
    report["team_candidates"] = sorted(
        report["team_candidates"], key=_candidate_rank
    )
    if report["team_candidates"]:
        best = report["team_candidates"][0]
        field_id = best["id"]
        name, ident = best.get("value_name") or "", best.get("value_id") or ""
        scope = report.get("scope") or "dc"
        cloud_team = (
            scope == "cloud"
            and (best.get("schema") or {}).get("custom") in CLOUD_TEAM_SCHEMAS
        )
        ref = _jql_string(f"{best['name']}[Team]") if cloud_team else _field_ref(field_id)
        team_value = ident or name
        jql = jql_for_teams(
            ref, (team_value or "<team>",),
            cloud_team=cloud_team and bool(ident),
        )
        report["hint"] = {
            "field": field_id,
            "field_name": best["name"],
            "cloud_team": cloud_team,
            "env": f"MEGASPEED_JIRA_TEAM_FIELD={field_id}",
            "team_id": ident,
            "team_name": name,
            "teams": (
                f'MEGASPEED_JIRA_TEAMS="{ident}"'
                if ident else f'MEGASPEED_JIRA_TEAMS="{name or "<team name>"}"'
            ),
            "name_alt": (
                f"team name: {name} — JQL compares the id; names go through"
                " JQL autocomplete where the site supports it"
                if cloud_team and ident and name else ""
            ),
            "jql": jql,
            "inspect": f"python3 -m ingest.jira_fields --issue {issue_key or 'KEY'}",
        }
    report["requests"] = client.requests
    return report


def _candidate_rank(candidate: dict) -> tuple:
    """Most likely field first: populated beats present, Teams schema beats names.

    "Test Team" (null) must not outrank the `atlassian-team` field that actually
    carries ``[ACME] Platform Team Alpha`` just because it sorts first by id.
    """
    schema = str((candidate.get("schema") or {}).get("custom") or "")
    populated = bool(candidate.get("on_screen")) and not candidate.get("null")
    return (
        0 if populated else 1,
        0 if schema in CLOUD_TEAM_SCHEMAS else 1,
        0 if str(candidate.get("name") or "").strip().lower() == "team" else 1,
        str(candidate.get("name") or "").lower(),
    )


def render(report: dict) -> str:
    """The aligned text output (the JSON form is for scripts)."""
    lines: list[str] = []
    scope = report.get("scope") or "dc"
    lines.append(
        f"jira {report.get('version') or '?'} · {scope.upper()} · "
        f"{report.get('requests', 0)} requests · read-only"
    )
    auth = report.get("auth") or {}
    if auth.get("warning"):
        lines.append(f"auth           {auth['warning']}")
    elif auth.get("via"):
        who = f" as {auth['account']}" if auth.get("account") else ""
        lines.append(f"auth           ok via {auth['via']}{who}")
    issue = report.get("issue")
    if issue:
        lines.append(
            f"issue {issue['key']} · {issue['type']} · "
            f"{issue['non_null']} non-null of {issue['field_count']} fields on screen"
        )
    candidates = report.get("team_candidates") or []
    if candidates:
        lines.append("")
        lines.append("TEAM CANDIDATE(S)")
        for index, entry in enumerate(candidates):
            schema = entry.get("schema") or {}
            detail = schema.get("custom") or schema.get("type") or ""
            marker = "   <- likely" if index == 0 else ""
            lines.append(f"  {entry['id']:<22} {entry['name']:<26} {detail}{marker}")
            value_name = entry.get("value_name") or ""
            value_id = entry.get("value_id") or ""
            if value_name or value_id:
                if value_name:
                    lines.append(f"    team:  {value_name}")
                if value_id:
                    lines.append(f"    id:    {value_id}")
            elif "value" in entry:
                where = "on screen" if entry.get("on_screen") else "not on this ticket's screen"
                lines.append(f"    value: {_compact(entry.get('value'))}  ({where})")
    else:
        lines.append("")
        lines.append(
            "No field name or schema contains 'team' — this site may not have"
            " Atlassian Teams. A team-like custom field is listed below by value."
        )
    rows = report.get("fields") or []
    if rows:
        shown = [row for row in rows if not row.get("null")] or rows
        lines.append("")
        lines.append(f"FIELDS ({len(shown)} shown)" if not issue else "FIELD VALUES")
        for row in shown:
            name = row.get("name") or "(unknown name)"
            lines.append(f"  {row['id']:<22} {name:<26} {_compact(row.get('value'))}")
    hint = report.get("hint") or {}
    if hint:
        lines.append("")
        lines.append("NEXT")
        lines.append(f"  {hint['env']}")
        lines.append(f"  {hint['teams']}")
        if hint.get("name_alt"):
            lines.append(f"  # {hint['name_alt']}")
        lines.append(f"  jql: {hint['jql']}")
        lines.append(
            "  then: MEGASPEED_JIRA_TEAMS=\"…\" python3 -m ingest scrape --jira --live"
            " --team \"…\" --out export/team-jira.tgz"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python3 -m ingest.jira_fields",
        description="Identify a Jira field (e.g. Team) on this site and its value on one ticket.",
    )
    ap.add_argument("--issue", metavar="KEY", default=None,
                    help="inspect one ticket (e.g. DEL-100); prints the fields on its screen")
    ap.add_argument("--list", action="store_true",
                    help="list the site's whole field catalogue (default when --issue is absent)")
    ap.add_argument("--cloud", action="store_true",
                    help="use the Jira Cloud client (v3 + cursor search)")
    ap.add_argument("--config", default=None, help="runtime config TOML (non-secret)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def _utf8_console() -> None:
    """Windows consoles default to cp1252; this output has ``·``, ``—`` and ``…``."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # a captured/StringIO stream
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        client = client_from_config(cfg, cloud=args.cloud)
        auth = probe_auth(client, cloud=args.cloud, cfg=cfg)
        report = inspect(
            client, args.issue, catalogue=args.list or not args.issue,
            version=auth.get("version"), auth=auth,
        )
    except SystemExit as exc:
        print(str(exc.code) if exc.code not in (0, None) else "bad configuration", file=sys.stderr)
        return 2
    except Exception as exc:  # transport/HTTP errors are operator-facing
        print(f"jira fields: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, default=str, ensure_ascii=False))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - the module entry point
    raise SystemExit(main())

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
``MEGASPEED_JIRA_CLOUD_BASE_URL`` + ``MEGASPEED_JIRA_CLOUD_TOKEN``), with
``--config`` for a TOML file.
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import Config, load_config

#: A value longer than this is truncated in the text output (the JSON keeps it).
VALUE_LIMIT = 200


def client_from_config(cfg: Config, *, cloud: bool):
    """Build the read-only client the scraper would use (and probe it once)."""
    from .transport import HttpTransport

    if cloud:
        if not cfg.jira_cloud_base_url:
            raise SystemExit(
                "jira cloud mode needs MEGASPEED_JIRA_CLOUD_BASE_URL (and"
                " MEGASPEED_JIRA_CLOUD_TOKEN); in dev that is the proxy URL"
            )
        from .jira_cloud import CloudJiraClient

        transport = HttpTransport(cfg.jira_cloud_base_url, token=cfg.jira_cloud_token)
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
    client.check()
    return client


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


def _compact(value, limit: int = VALUE_LIMIT) -> str:
    """One field value, short enough for a table row (JSON keeps the full one)."""
    if value is None or value == "":
        return "— (null)"
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


def inspect(client, issue_key: str | None = None, *, catalogue: bool = False) -> dict:
    """The field catalogue and (optionally) one ticket's field values.

    ``catalogue=True`` lists every field; an issue additionally reports the
    fields on that ticket's screen with their values. The team candidates are
    reported both ways, so the command still identifies the field when the
    ticket's value is null.
    """
    fields = list(client.fields() or [])
    by_id = {str(entry.get("id") or ""): entry for entry in fields if entry.get("id")}
    report: dict = {
        "version": (client.check() or {}).get("version"),
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
    if report["team_candidates"]:
        field_id = report["team_candidates"][0]["id"]
        report["hint"] = {
            "field": field_id,
            "env": f"MEGASPEED_JIRA_TEAM_FIELD={field_id}",
            "teams": "MEGASPEED_JIRA_TEAMS=\"<team name>[,<team name>]\"",
            "inspect": f"python3 -m ingest.jira_fields --issue {issue_key or 'KEY'}",
        }
    report["requests"] = client.requests
    return report


def render(report: dict) -> str:
    """The aligned text output (the JSON form is for scripts)."""
    lines: list[str] = []
    scope = report.get("scope") or "dc"
    lines.append(
        f"jira {report.get('version') or '?'} · {scope.upper()} · "
        f"{report.get('requests', 0)} requests · read-only"
    )
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
        for entry in candidates:
            schema = entry.get("schema") or {}
            detail = schema.get("custom") or schema.get("type") or ""
            lines.append(f"  {entry['id']:<22} {entry['name']:<26} {detail}")
            if "value" in entry:
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        client = client_from_config(cfg, cloud=args.cloud)
        report = inspect(client, args.issue, catalogue=args.list or not args.issue)
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

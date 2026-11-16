"""Scrape side of the airgap, Jira half: source -> classify -> pseudonymise -> tgz.

The Jira sibling of :mod:`ingest.scrape`. It runs **next to the source**
(prd/11 §1), reads the Jira DC REST API, reduces it to keys / pseudonyms /
closed labels with :mod:`ingest.jira_pipeline`, and writes the same transfer
bundle (:mod:`ingest.bundle`) the Bitbucket scrape writes. It never opens the
DuckDB store.

```
python3 -m ingest scrape --jira --live --project DEL --out export/del-jira.tgz
python3 -m ingest scrape --jira --fixture --out /tmp/jira-fixture.tgz
```

What is read, and why (prd/04):

| Endpoint | Becomes |
| --- | --- |
| ``/rest/api/2/search`` | the issue list: keys + ``updated`` (the watermark) |
| ``/rest/api/2/issue/{key}?expand=changelog`` | the issue snapshot + every status / assignee / sprint / points change |
| ``/rest/api/2/issue/{key}/worklog`` | native worklogs (only when the inline page is truncated) |
| ``/rest/agile/1.0/board/{id}/sprint`` | Sprint objects (start / end / complete / state) |
| ``/rest/agile/1.0/sprint/{id}/issue`` | membership-at-start vs added-later |
| ``/rest/api/2/field`` + ``/status`` | custom-field discovery, status -> category |

Local state, kept next to the output and never inside the tgz (prd/11 §3,§5):

```
<state-dir>/
  watermark.json    # per-source event cursor (shared with the Bitbucket scrape)
  jira_issues.json  # per-issue cursor: {updated, done} (settled = resolved)
  identity.json     # RAW ZONE: Jira + Bitbucket identities -> pseudonym
```

Incremental semantics are the same shape as the Bitbucket side: the watermark
is the max issue ``updated`` seen, the JQL asks for ``updated >= watermark -
overlap``, and an issue that is already recorded and sits below that cutoff is
not fetched again. An issue this scraper has *never* seen is always read in full,
no matter how old it is (the FR-1 rule) -- its history is what we are missing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import bundle as bundle_mod
from . import taxonomy
from .config import Config
from .jira import (
    FIELD_EPIC_LINK,
    FIELD_SPRINT,
    FIELD_STORY_POINTS,
    JiraClient,
)
from .jira_pipeline import JiraReducer, JiraStats
from .pipeline import KeyFilter, _object
from .progress import DEFAULT_INTERVAL as DEFAULT_PROGRESS_INTERVAL
from .progress import ProgressReporter
from .pseudonym import IdentityTable
from .scrape import IDENTITY_FILE, STATE_FILE, ScrapeState, default_state_dir
from .timeutil import iso, iso_precise, to_naive_utc, utcnow

ISSUE_STATE_FILE = "jira_issues.json"
ISSUE_STATE_SCHEMA_VERSION = 1
#: The Jira issue types that become objects. Sub-tasks are dropped: their parent
#: carries the estimate and the status that matter.
ISSUE_TYPES = ("Epic", "Story", "Bug", "Task")


# --------------------------------------------------------------------------- #
# local state
# --------------------------------------------------------------------------- #
class JiraIssueState:
    """Per-issue scrape cursor (``jira_issues.json``), the Jira analogue of ``prs.json``.

    ``done`` means "this issue is resolved and we have everything it will ever
    emit": it is skipped with **zero** issue requests while it sits strictly
    below the watermark, and comes back as soon as its ``updated`` moves.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.sources: dict[str, dict[str, dict]] = {}
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                payload = {}
            if payload.get("schema_version") in (None, ISSUE_STATE_SCHEMA_VERSION):
                self.sources = {
                    str(source): dict(entries or {})
                    for source, entries in (payload.get("sources") or {}).items()
                }

    def get(self, source: str, key: str) -> dict | None:
        return (self.sources.get(source) or {}).get(key)

    def entries(self, source: str) -> dict[str, dict]:
        return dict(self.sources.get(source) or {})

    def done_count(self, source: str) -> int:
        return sum(1 for entry in self.entries(source).values() if entry.get("done"))

    def record(self, source: str, key: str, *, updated: datetime | None, done: bool) -> None:
        self.sources.setdefault(source, {})[key] = {
            "updated": iso_precise(updated),
            "done": bool(done),
            "scraped_at": iso(utcnow()),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "_comment": (
                        "Scraper-side per-issue cursor: resolved issues are never"
                        " re-fetched unless their 'updated' moves. Local state,"
                        " never transferred."
                    ),
                    "schema_version": ISSUE_STATE_SCHEMA_VERSION,
                    "sources": self.sources,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


# --------------------------------------------------------------------------- #
# reduction
# --------------------------------------------------------------------------- #
@dataclass
class JiraCollected:
    source: str
    as_of: datetime
    run_id: str
    projects: tuple[str, ...] = ()
    events: list[dict] = field(default_factory=list)
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    stats: JiraStats = field(default_factory=JiraStats)
    requests: int = 0
    requests_by_kind: dict[str, int] = field(default_factory=dict)
    ticket_regex: str = ""
    ticket_projects: tuple[str, ...] = ()
    ticket_keys: tuple[str, ...] = ()
    identity: IdentityTable | None = None
    watermark: datetime | None = None
    boards: dict[str, int] = field(default_factory=dict)
    sprint_count: int = 0
    fields: dict[str, str] = field(default_factory=dict)
    statuses: int = 0

    def counts(self) -> dict:
        return self.stats.counts()


def jql_for(project: str, *, since: datetime | None = None, keys: tuple[str, ...] = ()) -> str:
    """The issue query: one project (or an explicit key set), Epics + Stories.

    ``since`` is the watermark overlap cursor; Jira's own ``yyyy/MM/dd HH:mm``
    form is used so the query is portable across DC versions. An explicit key
    set (prd/18) replaces the project clause entirely: on a shared project a
    prefix would match everyone's tickets.
    """
    if keys:
        scope = "key in (" + ",".join(keys) + ")"
    else:
        scope = f'project = "{project}"'
    types = "issuetype in (" + ", ".join(ISSUE_TYPES) + ")"
    clauses = [scope, types]
    if since is not None:
        clauses.append(f'updated >= "{since.strftime("%Y/%m/%d %H:%M")}"')
    return " AND ".join(clauses) + " ORDER BY updated ASC"


def collect(
    cfg: Config,
    transport,
    source: str,
    limit: int | None = None,
    *,
    identity: IdentityTable | None = None,
    state: ScrapeState | None = None,
    issue_state: JiraIssueState | None = None,
    full: bool = False,
    progress: ProgressReporter | None = None,
    projects: tuple[str, ...] | None = None,
    overlap_hours: int = 72,
) -> JiraCollected:
    """Read the source and reduce it in memory. Nothing is written here."""
    as_of = utcnow()
    run_id = f"{source}-{as_of.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ident = identity or IdentityTable(cfg.identity_path)
    key_filter = KeyFilter.build(
        cfg.ticket_regex, cfg.key_shape, cfg.ticket_projects, cfg.ticket_keys
    )
    client = JiraClient(
        transport,
        page_limit=cfg.jira_page_limit,
        min_interval=(
            0.0 if source.startswith(("fixture", "jira-fixture")) else cfg.jira_min_interval
        ),
    )
    project_list = tuple(projects or cfg.jira_projects or cfg.ticket_projects)
    out = JiraCollected(
        source=source,
        as_of=as_of,
        run_id=run_id,
        projects=project_list,
        ticket_regex=cfg.ticket_regex,
        ticket_projects=cfg.ticket_projects,
        ticket_keys=cfg.ticket_keys,
        identity=ident,
    )
    stats = out.stats

    # -- metadata: field ids + status categories (2 requests) -------------
    fields = client.field_ids()
    epic_link = cfg.jira_epic_link_field or fields.get(FIELD_EPIC_LINK, "")
    sprint_field = cfg.jira_sprint_field or fields.get(FIELD_SPRINT, "")
    points_field = cfg.jira_story_points_field or fields.get(FIELD_STORY_POINTS, "")
    categories = client.status_categories()
    out.fields = {
        name: field_id
        for name, field_id in (
            ("epic_link", epic_link),
            ("sprint", sprint_field),
            ("story_points", points_field),
        )
        if field_id
    }
    out.statuses = len(categories)

    # -- sprints + membership (agile API) --------------------------------
    sprints: list[tuple[str, dict]] = []
    memberships: dict[str, set[int]] = {}
    for project in project_list:
        board = cfg.jira_board or client.scrum_board(project)
        if board is None:
            continue
        out.boards[project] = board
        for sprint in client.sprints(board):
            sprints.append((project, sprint))
            sid = int(sprint["id"])
            for key in client.sprint_issue_keys(sid):
                memberships.setdefault(key, set()).add(sid)
    out.sprint_count = len(sprints)

    reducer = JiraReducer(
        cfg,
        ident,
        as_of,
        epic_link_field=epic_link,
        sprint_field=sprint_field,
        story_points_field=points_field,
        status_categories=categories,
    )
    for project, sprint in sprints:
        reducer.reduce_sprint(sprint, project, stats)

    # -- issues ----------------------------------------------------------
    previous = None if full else (state.watermark(source) if state else None)
    cutoff = previous - timedelta(hours=overlap_hours) if previous else None
    prior = {} if full else (issue_state.entries(source) if issue_state else {})
    # An explicit key set (prd/18) is one query across projects: `key in (...)`
    # replaces the project clause, so only the first project is walked.
    key_scoped = bool(cfg.ticket_keys)
    fetched: list[dict] = []
    seen: set[str] = set()
    stop = False
    for index, project in enumerate(project_list):
        if stop or (key_scoped and index > 0):
            break
        jql = jql_for(
            project,
            since=cutoff,
            keys=cfg.ticket_keys if key_scoped else (),
        )
        for row in client.search(jql):
            key = str(row.get("key") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            stats.issues_seen += 1
            updated = _updated(row)
            if updated is not None:
                stats.watermark = (
                    updated if stats.watermark is None else max(stats.watermark, updated)
                )
            if not key_filter.accepts(key):
                stats.issues_skipped += 1
                continue
            if limit is not None and stats.issues_kept >= limit:
                stats.issues_skipped += 1
                stop = True
                continue
            if _unchanged(key, updated, prior, cutoff):
                stats.issues_skipped += 1
                continue
            issue = client.issue(key)
            _complete_worklogs(client, issue)
            fetched.append(issue)
            stats.issues_kept += 1
            if issue_state is not None:
                resolved = bool((issue.get("fields") or {}).get("resolution"))
                issue_state.record(
                    source,
                    key,
                    updated=_updated({"fields": issue.get("fields")}) or updated,
                    done=resolved,
                )
            if progress is not None:
                progress.tick(
                    prs_seen=stats.issues_seen,
                    prs_kept=stats.issues_kept,
                    prs_skipped=stats.issues_skipped,
                    requests=client.requests,
                    events=len(stats.events),
                )

    # -- reduction (two passes: object ids first, then events) ------------
    reducer.prime(fetched, sprints)
    for issue in fetched:
        reducer.reduce_issue(
            issue,
            project=_project_of(issue, project_list),
            memberships=memberships,
            stats=stats,
        )

    for pseudonym in sorted(stats.people):
        stats.objects.append(
            _object(
                "PERSON",
                f"PERSON:{pseudonym}",
                actor=pseudonym,
                attributes={"actor_kind": reducer.actor_kind(pseudonym)},
            )
        )

    out.events = stats.events
    out.relations = stats.relations
    out.objects = stats.objects
    out.requests = client.requests
    out.requests_by_kind = dict(sorted(client.requests_by_kind.items()))
    out.watermark = stats.watermark
    return out


def _updated(row: dict) -> datetime | None:
    value = (row.get("fields") or {}).get("updated") or row.get("updated")
    return to_naive_utc(value) if value else None


def _project_of(issue: dict, projects: tuple[str, ...]) -> str:
    """The issue's project key, from its key (``DEL-100`` -> ``DEL``)."""
    prefix = str(issue.get("key") or "").partition("-")[0]
    return prefix if prefix else (projects[0] if projects else "")


def _unchanged(
    key: str, updated: datetime | None, prior: dict[str, dict], cutoff: datetime | None
) -> bool:
    """True when this issue is already recorded and nothing moved since.

    Strictly-below-the-cutoff only: an issue *at* the cursor is ambiguous (ties
    are common) and is re-read, which is harmless because import is idempotent
    while dropping it could lose an event. An unrecorded issue is never skipped
    (FR-1: its whole history is missing).
    """
    if cutoff is None or updated is None:
        return False
    if updated >= cutoff:
        return False
    entry = prior.get(key)
    if not entry:
        return False
    recorded = entry.get("updated")
    return bool(recorded) and updated <= to_naive_utc(recorded)


def _complete_worklogs(client: JiraClient, issue: dict) -> None:
    """Replace the inline (capped) worklog page with the complete list if needed."""
    block = (issue.get("fields") or {}).get("worklog") or {}
    inline = list(block.get("worklogs") or [])
    total = block.get("total")
    if total is None or len(inline) >= int(total):
        return
    complete = client.worklogs(str(issue.get("key")))
    issue["fields"]["worklog"] = {
        **block,
        "startAt": 0,
        "maxResults": len(complete),
        "total": len(complete),
        "worklogs": complete,
    }


# --------------------------------------------------------------------------- #
# scrape (reduction -> tgz)
# --------------------------------------------------------------------------- #
def build_manifest(collected: JiraCollected, *, previous: datetime | None,
                   watermark: datetime | None, overlap_hours: int, full: bool) -> dict:
    """The bundle's manifest: provenance + counts, and nothing raw (prd/11 §2)."""
    counts = collected.counts()
    return {
        "schema_version": bundle_mod.SCHEMA_VERSION,
        "generator": {"name": "megaspeed-scrape", "version": _version()},
        "created_at": iso(collected.as_of),
        "source": collected.source,
        "sources": [collected.source],
        "classifier_version": taxonomy.CLASSIFIER_VERSION,
        "ticket_projects": list(collected.ticket_projects),
        "ticket_keys": list(collected.ticket_keys),
        "ticket_regex": collected.ticket_regex,
        "projects": list(collected.projects),
        "repos": [],
        "watermark_ts": iso(watermark),
        "watermarks": {collected.source: iso(watermark)},
        "previous_watermark_ts": iso(previous),
        "overlap_hours": overlap_hours,
        "full": bool(full),
        "counts": counts,
        "jira": {
            "boards": dict(sorted(collected.boards.items())),
            "sprints": collected.sprint_count,
            "fields": dict(sorted(collected.fields.items())),
            "statuses": collected.statuses,
            "worklogs": collected.stats.worklogs,
            "log_delays": dict(sorted(collected.stats.log_delays.items())),
        },
        "run": {
            "run_id": collected.run_id,
            "as_of": iso_precise(collected.as_of),
            "source": collected.source,
            "classifier_version": taxonomy.CLASSIFIER_VERSION,
            "issues_seen": collected.stats.issues_seen,
            "issues_kept": collected.stats.issues_kept,
            "issues_skipped": collected.stats.issues_skipped,
            "issues_skipped_type": collected.stats.issues_skipped_type,
            "transitions": collected.stats.transitions,
            "sprint_changes": collected.stats.sprint_changes,
            "sprint_at_start": collected.stats.sprint_at_start,
            "points_changes": collected.stats.points_changes,
            "points_rounded": collected.stats.points_rounded,
            "assigned_changes": collected.stats.assigned_changes,
            "changelog_items": collected.stats.changelog_items,
            "changelog_ignored": collected.stats.changelog_ignored,
        },
        "requests": collected.requests,
        "requests_by_kind": dict(collected.requests_by_kind),
        "activities_by_activity": dict(sorted(collected.stats.activities.items())),
    }


def _version() -> str:
    from . import __version__

    return __version__


def scrape(
    cfg: Config,
    transport,
    source: str,
    *,
    out: str | Path,
    state_dir: str | Path | None = None,
    limit: int | None = None,
    overlap_hours: int = 72,
    full: bool = False,
    identity_path: str | Path | None = None,
    progress: ProgressReporter | None = None,
    projects: tuple[str, ...] | None = None,
) -> dict:
    """Scrape Jira into ``out`` (a tgz) and update local state.

    ``full`` ignores the watermark and the per-issue cursor and re-emits every
    issue; otherwise only issues whose ``updated`` is at/after
    ``watermark - overlap_hours`` are fetched (the overlap keeps a backdated
    edit from being lost). The new watermark never moves backwards.
    """
    out = Path(out).expanduser()
    state_dir = Path(state_dir).expanduser() if state_dir else default_state_dir(out)
    state = ScrapeState(state_dir / STATE_FILE)
    issue_state = JiraIssueState(state_dir / ISSUE_STATE_FILE)
    ident_path = Path(identity_path).expanduser() if identity_path else state_dir / IDENTITY_FILE
    reporter = progress if progress is not None else ProgressReporter(
        interval=DEFAULT_PROGRESS_INTERVAL
    )

    previous = None if full else state.watermark(source)
    collected = collect(
        cfg,
        transport,
        source,
        limit,
        identity=IdentityTable(ident_path),
        state=state,
        issue_state=issue_state,
        full=full,
        progress=reporter,
        projects=projects,
        overlap_hours=overlap_hours,
    )

    # The Jira cursor is the max issue ``updated`` (the column the JQL filters
    # on), not the newest event timestamp: a worklog's ``created`` can be older
    # than the issue's ``updated`` and the two must not drift apart.
    watermark = max((w for w in (previous, collected.watermark) if w is not None), default=None)

    manifest = build_manifest(
        collected,
        previous=previous,
        watermark=watermark,
        overlap_hours=overlap_hours,
        full=full,
    )
    written = bundle_mod.write_bundle(
        out, manifest=manifest, events=collected.events, objects=collected.objects,
        relations=collected.relations,
    )

    state.record(source, watermark, collected.run_id, out, repos={})
    state.save()
    issue_state.save()
    if collected.identity is not None:
        collected.identity.save()

    return {
        "mode": "scrape",
        "source": source,
        "run_id": collected.run_id,
        "as_of": collected.as_of,
        "bundle": str(out),
        "bundle_bytes": written["bytes"],
        "bundle_sha256": written["sha256"],
        "state_dir": str(state_dir),
        "issue_state": str(state_dir / ISSUE_STATE_FILE),
        "identity_map": str(ident_path),
        "watermark": iso(watermark),
        "previous_watermark": iso(previous),
        "overlap_hours": overlap_hours,
        "full": bool(full),
        "projects": list(collected.projects),
        "boards": dict(sorted(collected.boards.items())),
        "fields": dict(sorted(collected.fields.items())),
        "issues_seen": collected.stats.issues_seen,
        "issues_kept": collected.stats.issues_kept,
        "issues_skipped": collected.stats.issues_skipped,
        "issues_skipped_type": collected.stats.issues_skipped_type,
        "issues_resolved_total": issue_state.done_count(source),
        "events": len(collected.events),
        "objects": len(collected.objects),
        "counts": manifest["counts"],
        "requests": collected.requests,
        "requests_by_kind": dict(collected.requests_by_kind),
        "activities_by_activity": dict(sorted(collected.stats.activities.items())),
        "worklogs": collected.stats.worklogs,
        "log_delays": dict(sorted(collected.stats.log_delays.items())),
        "transitions": collected.stats.transitions,
        "points_changes": collected.stats.points_changes,
        "points_rounded": collected.stats.points_rounded,
        "sprint_changes": collected.stats.sprint_changes,
        "sprint_at_start": collected.stats.sprint_at_start,
        "assigned_changes": collected.stats.assigned_changes,
        "changelog_items": collected.stats.changelog_items,
        "changelog_ignored": collected.stats.changelog_ignored,
        "classifier_version": taxonomy.CLASSIFIER_VERSION,
        "ticket_regex": cfg.ticket_regex,
        "ticket_projects": list(cfg.ticket_projects),
        "ticket_keys": list(cfg.ticket_keys),
    }


__all__ = [
    "ISSUE_STATE_FILE",
    "JiraCollected",
    "JiraIssueState",
    "collect",
    "jql_for",
    "scrape",
]

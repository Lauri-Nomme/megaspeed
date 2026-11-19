"""Scrape side of the airgap, Jira half: source -> classify -> pseudonymise -> tgz.

The Jira sibling of :mod:`ingest.scrape`. It runs **next to the source**
(prd/11 §1), reads the Jira DC REST API, reduces it to keys / pseudonyms /
closed labels with :mod:`ingest.jira_pipeline`, and writes the same transfer
bundle (:mod:`ingest.bundle`) the Bitbucket scrape writes. It never opens the
DuckDB store.

```
python3 -m ingest scrape --jira --live --project DEL --out export/del-jira.tgz
python3 -m ingest scrape --jira-cloud --live --project DEL --out export/del-jira-cloud.tgz
python3 -m ingest scrape --jira --fixture --out /tmp/jira-fixture.tgz
```

The scope is chosen by the most specific setting available (prd/18, prd/27):
an explicit ticket set (``MEGASPEED_TICKET_KEYS``) beats an epic set
(``MEGASPEED_JIRA_EPICS``), which beats the project list
(``MEGASPEED_JIRA_PROJECTS``). An epic set is resolved once, at the start of the
run, with one extra search (``"Epic Link" in (DEL-1, DEL-2)``); from then on the
walk is an ordinary explicit-key-set walk, and the resolved keys ride in the
bundle manifest so import and ``--verify`` scope the store the same way.

The Cloud variant (prd/24) is the *same* walk and the same reducer with a
different client (:class:`ingest.jira_cloud.CloudJiraClient`): v3 paths, Bearer
PAT, cursor pagination and Cloud's field ids, so both sources emit the same
objects and events from the same data. Development points it at the local
emulation proxy (``proxy/cloud_jira_proxy.py``); production at a real Cloud URL.

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
  jira_issues.json  # per-issue cursor: {updated, done, run_id} (settled = resolved)
  identity.json     # RAW ZONE: Jira + Bitbucket identities -> pseudonym
```

Incremental semantics are the same shape as the Bitbucket side: the watermark
is the max issue ``updated`` seen, the JQL asks for ``updated >= watermark -
overlap``, and an issue that is already recorded and sits below that cutoff is
not fetched again. An issue this scraper has *never* seen is always read in full,
no matter how old it is (the FR-1 rule) -- its history is what we are missing.

Two rules the Jira side needs because its walk is *not* the Bitbucket walk:

* **A capped run does not consume the cursor.** The search is ordered by
  ``updated ASC``, so ``--limit`` reads the *oldest* issues first; a watermark
  taken from every row the page returned would put the unread ones below the
  next run's cutoff and delete them silently. The cap therefore stops the walk
  (no row past it is even read) and the cursor is left where it was, with
  ``truncated: true`` in the manifest.
* **One bad issue is skipped, not fatal.** A 404 on a moved/deleted issue, a
  permission error or a 5xx that outlived the transport retries is counted in
  ``failures`` and skipped; it is not recorded in the per-issue cursor, so a
  later run inside the overlap window retries it.

Crash safety is the Bitbucket contract, adapted to a walk that reduces as it
goes: identity -> the staged rows -> ``jira_issues.json`` -> ``watermark.json``
are checkpointed every ``--checkpoint-every`` issues and at every project
boundary, the next run resumes from that checkpoint, and the issues the
interrupted run already checkpointed are skipped by their ``run_id`` (their
events ride in the carried rows). A checkpoint is incremental -- it stages only
the rows added since the last one in ``<out>.parts/``
(:class:`ingest.bundle.BundleSpool`) and the tgz is written once at the end, so
it costs O(new issues) rather than O(bundle so far). A checkpoint never advances
the global cursor -- the walk is per project, so a mid-run cursor would strand a
later project's older issues.
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
    JiraError,
)
from .jira_pipeline import JiraReducer, JiraStats
from .pipeline import KeyFilter, _object
from .progress import DEFAULT_INTERVAL as DEFAULT_PROGRESS_INTERVAL
from .progress import ProgressReporter
from .pseudonym import IdentityTable
from .scrape import (
    DEFAULT_CHECKPOINT_EVERY,
    IDENTITY_FILE,
    STATE_FILE,
    ScrapeState,
    _merge_events,
    _merge_objects,
    _write_bundle_atomic,
    default_state_dir,
)
from .timeutil import iso, iso_precise, to_naive_utc, utcnow

ISSUE_STATE_FILE = "jira_issues.json"
ISSUE_STATE_SCHEMA_VERSION = 1
SPRINT_STATE_FILE = "jira_sprints.json"
SPRINT_STATE_SCHEMA_VERSION = 1
#: How far back from the issue watermark a closed sprint is still re-checked,
#: and how many recent closed sprints are always kept in the walk (prd/44
#: NFR-2). Membership of an older, closed sprint cannot change once its
#: ``completeDate`` stops moving.
SPRINT_WATERMARK_DAYS = 30
SPRINT_CLOSED_KEEP = 6
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

    def record(self, source: str, key: str, *, updated: datetime | None, done: bool,
               run_id: str = "") -> None:
        self.sources.setdefault(source, {})[key] = {
            "updated": iso_precise(updated),
            "done": bool(done),
            "scraped_at": iso(utcnow()),
            # The run that wrote this entry: a resume skips the issues its own
            # interrupted run checkpointed (their events are in the partial
            # bundle), instead of re-fetching them (mirrors ``prs.json``).
            "run_id": str(run_id or ""),
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
class JiraSprintState:
    """Per-sprint membership cursor (``jira_sprints.json``, prd/44 NFR-2).

    The Jira side used to walk **every** sprint's membership on **every** run:
    one ``/sprint/{id}/issue`` request per sprint, forever, even though a closed
    sprint's membership cannot change once its ``completeDate`` is final. This
    cache stores, per sprint id: the metadata that decides freshness
    (``state`` / ``start_date`` / ``complete_date``) and the issue keys the walk
    read. A later run re-reads membership only for an open sprint or one whose
    ``completeDate`` moved; everything else is replayed from here.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.sources: dict[str, dict[str, dict]] = {}
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                payload = {}
            if payload.get("schema_version") in (None, SPRINT_STATE_SCHEMA_VERSION):
                self.sources = {
                    str(source): dict(entries or {})
                    for source, entries in (payload.get("sources") or {}).items()
                }

    def get(self, source: str, sprint_id: int) -> dict | None:
        return (self.sources.get(source) or {}).get(str(int(sprint_id)))

    def entries(self, source: str) -> dict[str, dict]:
        return dict(self.sources.get(source) or {})

    def record(self, source: str, sprint: dict, keys: list[str]) -> None:
        self.sources.setdefault(source, {})[str(int(sprint["id"]))] = {
            "state": str(sprint.get("state") or "").strip().lower(),
            "start_date": sprint.get("startDate"),
            "complete_date": sprint.get("completeDate"),
            "membership": sorted({str(key) for key in keys if key}),
            "scraped_at": iso(utcnow()),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "_comment": (
                        "Scraper-side per-sprint membership cursor: a closed"
                        " sprint is re-read only when its completeDate moves."
                        " Local state, never transferred."
                    ),
                    "schema_version": SPRINT_STATE_SCHEMA_VERSION,
                    "sources": self.sources,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


def _sprint_membership_candidates(
    sprints: list[dict], cached: dict[str, dict], *,
    watermark: datetime | None, bootstrap: bool,
) -> list[dict]:
    """The sprints whose membership this run must actually read (prd/44 NFR-2).

    Rules, in order:
    * a sprint never seen before is read on the **bootstrap** run (the first
      run has nothing to replay); on a later run an old closed sprint outside
      the watermark window and the last K is not walked;
    * an active/future sprint is read (its membership is still moving);
    * a closed sprint is read when its ``completeDate`` moved;
    * otherwise it is replayed from the cache — the fix for the O(all sprints)
      walk that even an incremental run paid before this cursor.

    ``watermark`` is the issue cursor; ``watermark - SPRINT_WATERMARK_DAYS`` and
    the last ``SPRINT_CLOSED_KEEP`` closed sprints bound the walk from the
    sprint side too.
    """
    closed = sorted(
        (sprint for sprint in sprints
         if str(sprint.get("state") or "").lower() == "closed"),
        key=lambda sprint: str(sprint.get("startDate") or ""),
    )
    recent_closed = {str(int(sprint["id"])) for sprint in closed[-SPRINT_CLOSED_KEEP:]}
    cutoff = (
        (watermark - timedelta(days=SPRINT_WATERMARK_DAYS)).isoformat()
        if watermark is not None else None
    )
    out: list[dict] = []
    for sprint in sprints:
        sid = str(int(sprint["id"]))
        entry = cached.get(sid)
        state = str(sprint.get("state") or "").strip().lower()
        if bootstrap:
            out.append(sprint)
            continue
        if entry is None:
            if (state in ("active", "future") or sid in recent_closed
                    or cutoff is None
                    or str(sprint.get("startDate") or "") >= cutoff):
                out.append(sprint)
            continue
        if state in ("active", "future"):
            out.append(sprint)
            continue
        if sprint.get("completeDate") != entry.get("complete_date"):
            out.append(sprint)
    return out


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
    #: The explicit key set the walk was scoped to: ``MEGASPEED_TICKET_KEYS``
    #: verbatim (prd/18), or the epic set *resolved* to epics + children
    #: (prd/27). It rides in the manifest, so import and ``--verify`` scope the
    #: store exactly the way the scrape did.
    ticket_keys: tuple[str, ...] = ()
    #: The epic keys the operator asked for (prd/27); empty unless epic-scoped.
    epic_keys: tuple[str, ...] = ()
    identity: IdentityTable | None = None
    watermark: datetime | None = None
    boards: dict[str, int] = field(default_factory=dict)
    sprint_count: int = 0
    fields: dict[str, str] = field(default_factory=dict)
    statuses: int = 0
    #: prd/29: the workflow status -> stage map this run projected with. Echoed
    #: in the manifest so a store's stage labels are reproducible.
    status_map: dict[str, str] = field(default_factory=dict)
    #: Issues that failed to fetch: ``[{key, error}]``. Counted, not fatal, and
    #: not recorded in the per-issue cursor, so a later run retries them (FR-2)
    #: -- within the watermark overlap window, exactly like the Bitbucket side.
    failures: list[dict] = field(default_factory=list)
    #: The run hit ``--limit``: issues it never read must not advance the cursor
    #: (FR-1). Mirrors the Bitbucket ``truncated`` per-repo flag.
    truncated: bool = False
    #: Crash-safety provenance (mirrors the Bitbucket scrape).
    resumed: bool = False
    carried_events: list[dict] = field(default_factory=list)
    carried_objects: list[dict] = field(default_factory=list)
    carried_relations: list[tuple[str, str, str]] = field(default_factory=list)

    def counts(self) -> dict:
        return self.stats.counts()


def jql_for(project: str, *, since: datetime | None = None, keys: tuple[str, ...] = ()) -> str:
    """The issue query: one project (or an explicit key set), Epics + Stories.

    ``since`` is the watermark overlap cursor; Jira's own ``yyyy/MM/dd HH:mm``
    form is used so the query is portable across DC versions. An explicit key
    set (prd/18) replaces the project clause entirely: on a shared project a
    prefix would match everyone's tickets. The key set an epic scope resolves to
    (prd/27) rides through here too, so the walk is the same either way.
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


def jql_for_epics(epics: tuple[str, ...]) -> str:
    """The one extra search that resolves an epic set to its children (prd/27)."""
    return '"Epic Link" in (' + ",".join(epics) + ")"


def resolve_epic_keys(
    client: JiraClient,
    epics: tuple[str, ...],
    *,
    progress: ProgressReporter | None = None,
) -> tuple[str, ...]:
    """An epic set -> the epics themselves **plus** their child issues (prd/27).

    One search over the *keys* the scope is made of, so an epic-scoped run costs
    exactly one request more than an explicit key set -- the walk, the manifest
    and import then cannot tell the two apart. The epic keys come first (they
    are what the operator asked for) and a child is de-duplicated, so the result
    is a stable order regardless of the search's own ordering.
    """
    keys: list[str] = [str(key) for key in epics if str(key)]
    for row in client.search(jql_for_epics(tuple(keys)), fields="key"):
        key = str(row.get("key") or "")
        if key and key not in keys:
            keys.append(key)
    if progress is not None:
        progress.emit(f"[scrape] epics {', '.join(epics)} -> {len(keys)} key(s)")
    return tuple(keys)


def _projects_of(keys: tuple[str, ...]) -> tuple[str, ...]:
    """The project prefixes of a key set, in first-seen order (``DEL-1`` -> DEL)."""
    return tuple(dict.fromkeys(key.partition("-")[0] for key in keys if "-" in key))


def collect(
    cfg: Config,
    transport,
    source: str,
    limit: int | None = None,
    *,
    client: JiraClient | None = None,
    identity: IdentityTable | None = None,
    state: ScrapeState | None = None,
    issue_state: JiraIssueState | None = None,
    sprint_state: JiraSprintState | None = None,
    full: bool = False,
    progress: ProgressReporter | None = None,
    projects: tuple[str, ...] | None = None,
    overlap_hours: int = 72,
    resume_run_id: str | None = None,
    carry_events: list[dict] | None = None,
    carry_objects: list[dict] | None = None,
    carry_relations: list[tuple[str, str, str]] | None = None,
    checkpoint=None,
    checkpoint_every: int = 0,
) -> JiraCollected:
    """Read the source and reduce it in memory. Nothing is written here.

    ``client`` lets the caller supply a differently-shaped client -- the Cloud
    one (:class:`ingest.jira_cloud.CloudJiraClient`) -- so this walk, the
    reduction and the object/event model are shared verbatim between DC and
    Cloud (prd/24). Without it a DC client is built over ``transport``.

    Crash safety mirrors :func:`ingest.scrape.collect`:

    * ``checkpoint(collected, project)`` is called every ``checkpoint_every``
      kept issues and at every project boundary, so the caller can persist a
      partial bundle + both cursors without waiting for the end of the run. The
      reduction therefore happens **as each issue arrives**, not in a final pass
      -- a cursor without the events it covers would skip those issues forever.
    * ``resume_run_id`` / ``carry_events`` / ``carry_objects`` resume an
      interrupted run: issues its own run already checkpointed are skipped (the
      carried bundle holds their events), and the rows are carried into this
      run's bundle.

    A per-issue fetch failure (a 404 on a moved/deleted issue, a permission
    error, a 5xx that outlived the transport retries) is **skipped and
    counted** in ``failures`` -- one bad issue never aborts the run, and it is
    not recorded in the per-issue cursor, so a later run retries it (within the
    watermark overlap window, the same bound the Bitbucket walk has).
    """
    as_of = utcnow()
    run_id = f"{source}-{as_of.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    ident = identity or IdentityTable(cfg.identity_path)
    client = client or JiraClient(
        transport,
        page_limit=cfg.jira_page_limit,
        min_interval=(
            0.0 if source.startswith(("fixture", "jira-fixture")) else cfg.jira_min_interval
        ),
        # prd/30: a caller that builds the client from config (the dev shortcut
        # in ``ingest.live``) still reads Tempo when it is configured.
        tempo=cfg.jira_tempo,
    )
    # -- scope: ticket keys > epics > projects (prd/18, prd/27) -----------
    # The most specific scope wins. An epic set is resolved here, once, to the
    # epics plus their children, and from then on the run is an ordinary
    # explicit-key-set run: same ``key in (...)`` query, same manifest, so
    # import and ``--verify`` cannot tell the two paths apart.
    epic_keys = () if cfg.ticket_keys else tuple(cfg.jira_epics)
    scope_keys = tuple(cfg.ticket_keys)
    if epic_keys:
        scope_keys = resolve_epic_keys(client, epic_keys, progress=progress)
    key_filter = KeyFilter.build(
        cfg.ticket_regex, cfg.key_shape, cfg.ticket_projects, scope_keys
    )
    # The epics name the projects to read sprints from, so an epic-scoped run
    # does not need (or inherit) an unrelated project list.
    project_list = tuple(
        projects
        or (_projects_of(epic_keys) if epic_keys else ())
        or cfg.jira_projects
        or cfg.ticket_projects
    )
    out = JiraCollected(
        source=source,
        as_of=as_of,
        run_id=run_id,
        projects=project_list,
        ticket_regex=cfg.ticket_regex,
        ticket_projects=cfg.ticket_projects,
        ticket_keys=scope_keys,
        epic_keys=epic_keys,
        identity=ident,
        resumed=bool(carry_events or carry_objects),
        carried_events=list(carry_events or []),
        carried_objects=list(carry_objects or []),
        carried_relations=list(carry_relations or []),
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
    out.status_map = dict(sorted(cfg.jira_status_map.items()))

    # -- sprints + membership (agile API) --------------------------------
    # prd/44 NFR-2: a closed sprint's membership cannot change once its
    # completeDate is final, so this cursor is what keeps an incremental run
    # from paying one membership request per sprint, forever.
    sprints: list[tuple[str, dict]] = []
    memberships: dict[str, set[int]] = {}
    sprints_previous = None if full else (state.watermark(source) if state else None)
    sprints_cached = {} if sprint_state is None else sprint_state.entries(source)
    sprints_bootstrap = bool(full or not sprints_cached)
    for project in project_list:
        board = cfg.jira_board or client.scrum_board(project)
        if board is None:
            continue
        out.boards[project] = board
        board_sprints = list(client.sprints(board))
        for sprint in board_sprints:
            sprints.append((project, sprint))
        walk = {
            int(sprint["id"]) for sprint in _sprint_membership_candidates(
                board_sprints, sprints_cached,
                watermark=sprints_previous, bootstrap=sprints_bootstrap,
            )
        }
        for sprint in board_sprints:
            sid = int(sprint["id"])
            cached_entry = sprints_cached.get(str(sid)) or {}
            if sprint_state is not None and sid not in walk:
                keys = list(cached_entry.get("membership") or [])
            else:
                keys = list(client.sprint_issue_keys(sid))
                if sprint_state is not None:
                    sprint_state.record(source, sprint, keys)
            for key in keys:
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
        # prd/29: the operator's workflow map, echoed in the manifest so the
        # store's projection is reproducible.
        stage_map=cfg.jira_status_map,
    )
    for project, sprint in sprints:
        reducer.reduce_sprint(sprint, project, stats)
    # Register the sprint ids/starts before the walk; each issue registers
    # itself as it is fetched (the reduction is incremental, see below).
    reducer.prime([], sprints)

    # -- issues ----------------------------------------------------------
    previous = sprints_previous
    cutoff = previous - timedelta(hours=overlap_hours) if previous else None
    prior = {} if full else (issue_state.entries(source) if issue_state else {})
    # An explicit key set -- from MEGASPEED_TICKET_KEYS (prd/18) or resolved from
    # an epic set (prd/27) -- is one query across projects: `key in (...)` replaces
    # the project clause, so only the first project is walked.
    key_scoped = bool(scope_keys)
    seen: set[str] = set()
    stop = False
    truncated = False
    pending = 0  # kept issues since the last checkpoint

    def _refresh() -> None:
        """Fill the row views from the in-flight accumulator.

        ``stats`` is the accumulator during the walk (``out.events`` is only
        filled by a refresh), so a mid-run checkpoint must ask for one first --
        otherwise it would write an empty (or stale) bundle.
        """
        fresh_events = list(stats.events)
        out.events = _merge_events(out.carried_events, fresh_events)
        out.objects = _merge_objects(
            out.carried_objects, [*stats.objects, *_people_objects(stats, reducer)]
        )
        out.relations = _finalize_relations(
            out.events,
            out.objects,
            _merge_relations(out.carried_relations, fresh_events, list(stats.relations)),
        )
        out.watermark = stats.watermark

    def _checkpoint(project: str | None) -> None:
        _refresh()
        checkpoint(out, project)

    for index, project in enumerate(project_list):
        if stop or (key_scoped and index > 0):
            break
        jql = jql_for(
            project,
            since=cutoff,
            keys=scope_keys if key_scoped else (),
        )
        for row in client.search(jql):
            key = str(row.get("key") or "")
            if not key or key in seen:
                continue
            # FR-1: the cap is a *stop*, not a skip. A row beyond the limit was
            # never read, so it must not advance the cursor (mirrors the
            # Bitbucket walk, scrape.py ``if limit ... truncated = True``): the
            # search is ordered by ``updated ASC``, so everything unread is at
            # or after the last read row and the next run asks for it again.
            if limit is not None and stats.issues_kept >= limit:
                truncated = True
                out.truncated = True
                stop = True
                break
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
            reason = _unchanged(key, updated, prior, cutoff, resume_run_id)
            if reason is not None:
                stats.issues_skipped += 1
                if reason == "resumed":
                    stats.issues_skipped_resumed += 1
                continue
            # FR-2: a per-issue fetch failure (a 404 on a moved/deleted issue, a
            # permission error, a 5xx that outlived the transport retries) skips
            # *this* issue: one bad issue must never abort a two-hour walk.
            # Nothing is recorded for it, so the next run retries it (within the
            # watermark overlap, like a failed PR on the Bitbucket side).
            try:
                issue = client.issue(key)
                _complete_worklogs(client, issue)
            except JiraError as exc:
                stats.issues_failed += 1
                out.failures.append({"key": key, "error": str(exc)})
                if progress is not None:
                    progress.emit(f"[scrape] skipped {key}: {exc}")
                continue
            stats.issues_kept += 1
            if issue_state is not None:
                resolved = bool((issue.get("fields") or {}).get("resolution"))
                issue_state.record(
                    source,
                    key,
                    updated=_updated({"fields": issue.get("fields")}) or updated,
                    done=resolved,
                    run_id=run_id,
                )
            # Reduce now, not in a final pass: a checkpoint has to carry this
            # issue's events with its cursor (see the docstring).
            reducer.prime([issue], [])
            reducer.reduce_issue(
                issue,
                project=_project_of(issue, project_list),
                memberships=memberships,
                stats=stats,
            )
            if progress is not None:
                progress.tick(
                    prs_seen=stats.issues_seen,
                    prs_kept=stats.issues_kept,
                    prs_skipped=stats.issues_skipped,
                    requests=client.requests,
                    events=len(stats.events),
                )
            if checkpoint is not None and checkpoint_every > 0:
                pending += 1
                if pending >= checkpoint_every:
                    pending = 0
                    _checkpoint(project)
        if stop:
            break
        # A project boundary is a free checkpoint: nothing is half-read.
        if checkpoint is not None and checkpoint_every > 0:
            pending = 0
            _checkpoint(None)

    _refresh()
    out.truncated = truncated or out.truncated
    out.requests = client.requests
    out.requests_by_kind = dict(sorted(client.requests_by_kind.items()))
    return out


def _people_objects(stats: JiraStats, reducer: JiraReducer) -> list[dict]:
    """PERSON rows for every actor seen so far (idempotent; derived, not stored)."""
    return [
        _object(
            "PERSON",
            f"PERSON:{pseudonym}",
            actor=pseudonym,
            attributes={"actor_kind": reducer.actor_kind(pseudonym)},
        )
        for pseudonym in sorted(stats.people)
    ]


def _merge_relations(
    carried: list[tuple[str, str, str]],
    fresh_events: list[dict],
    fresh: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """Carried relations minus the events this run re-reduced, then this run's."""
    replaced = {str(event["event_key"]) for event in fresh_events}
    return [row for row in carried if row[0] not in replaced] + list(fresh)


def _finalize_relations(
    events: list[dict],
    objects: list[dict],
    relations: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """Drop relations to objects this bundle does not emit; (re)add epic links.

    A dangling relation is a contract violation (``store.verify``), not a
    tolerable edge, so the filter is the last word. The epic link is derived from
    the STORY object's ``epic_key`` instead of the reducer's ``known`` set: that
    makes it independent of fetch order and idempotent across a checkpoint/
    resume, where the carried STORY object is the only record left.
    """
    emitted = {str(obj.get("object_id")) for obj in objects}
    present = {str(event.get("event_key")) for event in events}
    kept = [row for row in relations if row[2] in emitted and row[0] in present]
    have = {(row[0], row[2]) for row in kept}
    for obj in objects:
        if obj.get("object_type") != "STORY":
            continue
        epic_key = (obj.get("attributes") or {}).get("epic_key")
        if not epic_key:
            continue
        epic_id = f"EPIC:{epic_key}"
        if epic_id not in emitted:
            continue
        story_key = str(obj.get("object_id") or "").partition(":")[2]
        if not story_key:
            continue
        event_key = f"{story_key}:created"
        if event_key not in present or (event_key, epic_id) in have:
            continue
        kept.append((event_key, "EPIC", epic_id))
        have.add((event_key, epic_id))
    return kept


def _cursor(a: datetime | None, b: datetime | None) -> datetime | None:
    """The *earlier* of two optional cursors (``None`` is "no constraint")."""
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _updated(row: dict) -> datetime | None:
    value = (row.get("fields") or {}).get("updated") or row.get("updated")
    return to_naive_utc(value) if value else None


def _project_of(issue: dict, projects: tuple[str, ...]) -> str:
    """The issue's project key, from its key (``DEL-100`` -> ``DEL``)."""
    prefix = str(issue.get("key") or "").partition("-")[0]
    return prefix if prefix else (projects[0] if projects else "")


def _unchanged(
    key: str,
    updated: datetime | None,
    prior: dict[str, dict],
    cutoff: datetime | None,
    resume_run_id: str | None = None,
) -> str | None:
    """Why this issue needs no fetch (``"resumed"`` / ``"watermark"``), or ``None``.

    Strictly-below-the-cutoff only: an issue *at* the cursor is ambiguous (ties
    are common) and is re-read, which is harmless because import is idempotent
    while dropping it could lose an event. An unrecorded issue is never skipped
    (FR-1: its whole history is missing).

    ``"resumed"`` -- this issue was already read and checkpointed by the run we
    are resuming (its entry carries that run's ``run_id`` and the same
    ``updated``). Its events are in the carried bundle, so paying for it again
    would only re-do work the crash already paid for. This check comes *before*
    the cutoff, because a checkpointed issue can sit at or above it.

    ``"watermark"`` -- recorded and below the cutoff: nothing new in it.
    """
    entry = prior.get(key)
    if not entry:
        return None
    if resume_run_id and entry.get("run_id") == resume_run_id:
        if updated is not None and entry.get("updated") == iso_precise(updated):
            return "resumed"
    if cutoff is None or updated is None:
        return None
    if updated >= cutoff:
        return None
    recorded = entry.get("updated")
    return "watermark" if recorded and updated <= to_naive_utc(recorded) else None


def _complete_worklogs(client: JiraClient, issue: dict) -> None:
    """Replace the inline (capped) worklog page with the complete list if needed.

    With ``tempo`` the add-on is the source of truth, so its worklog endpoint is
    read for every issue -- not only when Jira's inline page happens to be
    truncated (prd/30).
    """
    block = (issue.get("fields") or {}).get("worklog") or {}
    inline = list(block.get("worklogs") or [])
    total = block.get("total")
    if not getattr(client, "tempo", False) and (
        total is None or len(inline) >= int(total)
    ):
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
                   watermark: datetime | None, overlap_hours: int, full: bool,
                   checkpoint: bool = False, truncated: bool = False) -> dict:
    """The bundle's manifest: provenance + counts, and nothing raw (prd/11 §2)."""
    counts = _bundle_counts(collected)
    return {
        "schema_version": bundle_mod.SCHEMA_VERSION,
        "generator": {"name": "megaspeed-scrape", "version": _version()},
        "created_at": iso(collected.as_of),
        "source": collected.source,
        "sources": [collected.source],
        "classifier_version": taxonomy.CLASSIFIER_VERSION,
        "ticket_projects": list(collected.ticket_projects),
        "ticket_keys": list(collected.ticket_keys),
        # prd/27: the epic keys the operator asked for. ``ticket_keys`` above is
        # the *resolved* set (epics + children), which is what import and
        # ``--verify`` enforce; this is provenance for the request.
        "jira_epics": list(collected.epic_keys),
        "ticket_regex": collected.ticket_regex,
        "projects": list(collected.projects),
        "repos": [],
        "watermark_ts": iso(watermark),
        "watermarks": {collected.source: iso(watermark)},
        "previous_watermark_ts": iso(previous),
        "overlap_hours": overlap_hours,
        "full": bool(full),
        # FR-1: this run hit --limit, so the cursor was deliberately not
        # advanced past the issues it never read. Recorded so an operator (or a
        # resume) can tell a capped run from a complete one.
        "truncated": bool(truncated),
        "counts": counts,
        "jira": {
            "boards": dict(sorted(collected.boards.items())),
            "sprints": collected.sprint_count,
            "fields": dict(sorted(collected.fields.items())),
            "statuses": collected.statuses,
            # prd/29: status name -> stage. Empty means the Jira category
            # projection was used (3 coarse stages).
            "status_map": dict(sorted(collected.status_map.items())),
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
            "issues_failed": collected.stats.issues_failed,
            "issues_skipped_resumed": collected.stats.issues_skipped_resumed,
            "truncated": bool(truncated),
            # Provenance for the crash-safety machinery (mirrors scrape.py).
            "checkpoint": bool(checkpoint),
            "resumed": bool(collected.resumed),
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


def _bundle_counts(collected: JiraCollected) -> dict:
    """Counts of what the *bundle* holds, not just what this run read.

    A resumed run carries the interrupted run's events and objects, so the
    reducer's own tallies (this run only) would undercount the tgz. The run
    tallies stay in ``manifest["run"]``; these describe the file.
    """
    counts = collected.counts()
    counts["events"] = len(collected.events)
    counts["objects"] = len(collected.objects)
    for label, object_type in (("epics", "EPIC"), ("stories", "STORY"),
                               ("sprints", "SPRINT"), ("people", "PERSON")):
        counts[label] = sum(
            1 for obj in collected.objects if obj.get("object_type") == object_type
        )
    counts["worklogs"] = sum(
        1 for event in collected.events if event.get("activity") == "worklogged"
    )
    return counts


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
    checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
    client: JiraClient | None = None,
) -> dict:
    """Scrape Jira into ``out`` (a tgz) and update local state.

    ``client`` is the shape adapter (DC by default, Cloud via
    :class:`ingest.jira_cloud.CloudJiraClient`): everything below -- the walk,
    the reduction, the bundle -- is shared, so ``--jira`` and ``--jira-cloud``
    produce the same objects and events from the same data (prd/24).

    ``full`` ignores the watermark and the per-issue cursor and re-emits every
    issue; otherwise only issues whose ``updated`` is at/after
    ``watermark - overlap_hours`` are fetched (the overlap keeps a backdated
    edit from being lost). The new watermark never moves backwards, and a
    **capped** run (``limit``) does not move it at all: the issues it never read
    are not consumed (FR-1).

    Crash safety mirrors :func:`ingest.scrape.scrape`: the identity map, the rows
    added since the last checkpoint, ``jira_issues.json`` and ``watermark.json``
    are checkpointed every ``checkpoint_every`` issues and at every project
    boundary, and the next run resumes from that checkpoint instead of starting
    over (FR-2). A checkpoint is incremental (``<out>.parts/``, the tgz is
    written once at the end), so its cost does not grow with the bundle.
    """
    out = Path(out).expanduser()
    state_dir = Path(state_dir).expanduser() if state_dir else default_state_dir(out)
    state = ScrapeState(state_dir / STATE_FILE)
    issue_state = JiraIssueState(state_dir / ISSUE_STATE_FILE)
    sprint_state = JiraSprintState(state_dir / SPRINT_STATE_FILE)
    ident_path = Path(identity_path).expanduser() if identity_path else state_dir / IDENTITY_FILE
    reporter = progress if progress is not None else ProgressReporter(
        interval=DEFAULT_PROGRESS_INTERVAL
    )

    previous = None if full else state.watermark(source)
    # Crash safety is incremental (see ``checkpoint``): the in-flight rows are
    # staged in ``<out>.parts/`` and the tgz is written once, at the end.
    spool = bundle_mod.BundleSpool(bundle_mod.spool_dir(out))

    # -- resume an interrupted run (module docstring "crash safety") --------
    resume_run_id: str | None = None
    resume_bundle: Path | None = None
    carried_bundle: bundle_mod.Bundle | None = None
    carry_events: list[dict] = []
    carry_objects: list[dict] = []
    carry_relations: list[tuple[str, str, str]] = []
    if not full and state.incomplete(source):
        resume_run_id = state.last_run_id(source)
        resume_bundle = state.last_bundle_path(source) or out
        if bundle_mod.inflight_path(resume_bundle) is not None:
            try:
                carried = bundle_mod.read_inflight(resume_bundle)
            except bundle_mod.BundleError as exc:
                reporter.emit(f"[scrape] cannot resume from {resume_bundle}: {exc}")
                resume_run_id, resume_bundle = None, None
            else:
                if carried.source != source:
                    reporter.emit(
                        f"[scrape] not resuming {resume_bundle}: it holds source"
                        f" {carried.source!r}, not {source!r}"
                    )
                    resume_run_id, resume_bundle = None, None
                else:
                    carried_bundle = carried
                    carry_objects, carry_relations = carried.objects, carried.relations
                    carry_events = carried.events
                    reporter.emit(
                        f"[scrape] resuming interrupted run {resume_run_id}"
                        f" from {resume_bundle}: carrying {len(carry_events)} event(s),"
                        f" {len(carry_objects)} object(s)"
                    )
    if carried_bundle is None:
        # A fresh run: never let a dead run's staged rows leak into this bundle.
        spool.reset()
    else:
        # Adopt the carried rows *before* they are stripped below (the spool may
        # have to stage them once, and a row without its objects is invalid).
        spool.resume(carried_bundle)
        # Drop the per-event object lists: the merged relations are the
        # authority, so a dangling edge from a partial bundle cannot survive the
        # resume. In place, so the spool still recognises these rows.
        for event in carry_events:
            event["objects"] = []

    def checkpoint(collected_now: JiraCollected, project: str | None) -> None:
        """Persist identity + the new rows + both cursors, mid-run.

        Incremental on purpose: only the rows added since the last checkpoint are
        staged (``<out>.parts/``) and the tgz is written once at the end, so the
        cost is O(new issues) instead of a full re-serialisation of the bundle so
        far (:class:`ingest.bundle.BundleSpool`). The crash-resume contract is
        unchanged.
        """
        # The cursor is **not** advanced here. The Jira watermark is one global
        # value but the walk is per project: after project A, a cursor at A's
        # newest ``updated`` would put project B's older issues outside the next
        # run's JQL window -- and those were never read. So a checkpoint keeps
        # the previous cursor (a resume re-walks it and skips the issues this run
        # already recorded by ``run_id``); only a completed run advances it.
        # FR-1: likewise, a capped run never advances it (mirrors scrape.py's
        # ``if not truncated``).
        watermark_now = previous
        manifest = build_manifest(
            collected_now,
            previous=previous,
            watermark=watermark_now,
            overlap_hours=overlap_hours,
            full=full,
            checkpoint=True,
            truncated=collected_now.truncated,
        )
        # Identity first: the bundle names these pseudonyms, so the map must
        # never lag behind a bundle that mentions them.
        if collected_now.identity is not None:
            collected_now.identity.save()
        spool.append(
            manifest=manifest,
            events=collected_now.events,
            objects=collected_now.objects,
            relations=collected_now.relations,
        )
        issue_state.save()
        sprint_state.save()
        state.record(
            source, watermark_now, collected_now.run_id, out, repos={},
            complete=False, resumed=collected_now.resumed, count_run=False,
        )
        state.save()

    collected = collect(
        cfg,
        transport,
        source,
        limit,
        client=client,
        identity=IdentityTable(ident_path),
        state=state,
        issue_state=issue_state,
        sprint_state=sprint_state,
        full=full,
        progress=reporter,
        projects=projects,
        overlap_hours=overlap_hours,
        resume_run_id=resume_run_id,
        carry_events=carry_events,
        carry_objects=carry_objects,
        carry_relations=carry_relations,
        checkpoint=checkpoint,
        checkpoint_every=max(0, int(checkpoint_every)),
    )

    # The Jira cursor is the max issue ``updated`` (the column the JQL filters
    # on), not the newest event timestamp: a worklog's ``created`` can be older
    # than the issue's ``updated`` and the two must not drift apart.
    # FR-1: a capped run keeps the old cursor -- the issues it never read are
    # below the watermark it would otherwise write, and would never be asked for
    # again.
    watermark = previous if collected.truncated else _cursor(previous, collected.watermark)

    manifest = build_manifest(
        collected,
        previous=previous,
        watermark=watermark,
        overlap_hours=overlap_hours,
        full=full,
        truncated=collected.truncated,
    )
    if collected.identity is not None:
        collected.identity.save()
    written = _write_bundle_atomic(
        out, manifest=manifest, events=collected.events, objects=collected.objects,
        relations=collected.relations,
    )

    issue_state.save()
    sprint_state.save()
    state.record(source, watermark, collected.run_id, out, repos={},
                 complete=True, resumed=collected.resumed)
    state.save()
    # The tgz is durable and the state file says the run is complete: the
    # in-flight rows have done their job.
    spool.finish()

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
        "sprint_state": str(state_dir / SPRINT_STATE_FILE),
        "identity_map": str(ident_path),
        "checkpoint_spool": str(spool.directory),
        "watermark": iso(watermark),
        "previous_watermark": iso(previous),
        "overlap_hours": overlap_hours,
        "full": bool(full),
        "truncated": bool(collected.truncated),
        "resumed": bool(collected.resumed),
        "resumed_from": str(resume_bundle) if resume_bundle else None,
        "events_carried": len(carry_events),
        "checkpoint_every": max(0, int(checkpoint_every)),
        "failures": list(collected.failures),
        "projects": list(collected.projects),
        "boards": dict(sorted(collected.boards.items())),
        "fields": dict(sorted(collected.fields.items())),
        "issues_seen": collected.stats.issues_seen,
        "issues_kept": collected.stats.issues_kept,
        "issues_skipped": collected.stats.issues_skipped,
        "issues_skipped_type": collected.stats.issues_skipped_type,
        "issues_failed": collected.stats.issues_failed,
        "issues_skipped_resumed": collected.stats.issues_skipped_resumed,
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
        # The scope the run actually used: the explicit set, or the epic set
        # resolved to epics + children (prd/27). Import/--verify use this.
        "ticket_keys": list(collected.ticket_keys),
        "jira_epics": list(collected.epic_keys),
        "jira_status_map": dict(sorted(collected.status_map.items())),
    }


__all__ = [
    "ISSUE_STATE_FILE",
    "JiraCollected",
    "JiraIssueState",
    "collect",
    "jql_for",
    "jql_for_epics",
    "resolve_epic_keys",
    "scrape",
]

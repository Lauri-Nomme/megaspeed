"""Scrape side of the airgap: source -> classify -> pseudonymise -> tgz.

This is the half that runs **next to the source** (prd/11 §1). It reads raw
Bitbucket DC payloads, reduces them to keys / pseudonyms / closed labels with
:mod:`ingest.pipeline`, and writes a transfer bundle (:mod:`ingest.bundle`).
It never opens the DuckDB store: the scraper artifact is stdlib-only, and the
pseudonymised zone is the *import* side's business.

Local state, kept **next to the output** and never inside the tgz (prd/11 §3,§5):

```
<state-dir>/
  watermark.json   # per-source event cursor + per-repo updatedDate cursor
  prs.json         # per-PR cursor: {state, updated_date, done} (settled)
  identity.json    # RAW ZONE: slug/email -> pseudonym. Back this up.
  git/             # RAW ZONE: shallow/partial bare repo per PROJECT/repo,
                   #   used only for the deterministic push_class (prd/16)
```

The identity map is the one irreplaceable piece of scrape-side state: losing it
breaks pseudonym continuity across incremental transfers. The git cache is
disposable -- losing it only costs a re-fetch (and an old force-pushed head
becomes unclassifiable, see :mod:`ingest.gitclass`).

A huge Bitbucket project is made affordable by four layers (prd/14):

1. **Ticket-key pre-filter on the PR list.** The list endpoint returns cheap
   summaries (``title``, ``fromRef.displayId``, ``description``, ``state``,
   ``updatedDate``); a PR that carries no target ticket key on any of them never
   costs a detail, commit or activity request. The key matcher is the ingest's
   own (:class:`ingest.pipeline.KeyFilter`), so scrape and store agree on what a
   ticket is.
2. **Per-repo ``updatedDate`` watermark** in ``watermark.json``: a PR that has
   not moved since the last run has nothing new to tell us.
3. **Settled finality** in ``prs.json``: a MERGED/DECLINED PR that was scraped
   once and has not been updated since is *done* -- it is never fetched again
   unless its ``updatedDate`` moves.
4. **NEWEST-first listing with early termination**: DC 9.4.18 sorts
   ``order=NEWEST`` by ``updatedDate`` descending, so pagination stops at the
   first full page that is entirely at/below the watermark. A huge project is
   walked back only to the cursor, not to the beginning of time.

``--full`` ignores all of the cursors and re-scrapes everything (a rebuild of
the main side's store needs that); the pre-filter still applies, because a
non-matching PR has nothing we would ever store.

The push classifier (prd/16) runs here, between the activity fetch and the
reduction: every RESCOPED activity is stamped with the deterministic
``push_class`` before :class:`ingest.pipeline.Reducer` turns it into a
``pr_pushed`` event. It needs a local shallow git cache, so it is on for live
runs only (``--no-push-class`` / ``MEGASPEED_PUSH_CLASS=0`` turns it off).

The **text classifier** (prd/16) runs in the same window, on the same PR: the
raw comment bodies and the PR description are read, projected onto the closed
vocabulary by :mod:`ingest.classify` (local Ollama on a live run, deterministic
mock for a fixture), and dropped. Only ``comment_type`` / ``thread_outcome`` on
the THREAD objects and the ``question_raised`` / ``question_answered`` events
leave the raw zone. The git cache also answers "did the diff move at this
comment's anchor?" -- the deterministic half of ``thread_outcome``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import bundle as bundle_mod
from . import classify, gitclass, taxonomy
from .bitbucket import BitbucketClient, PullRequest
from .config import Config
from .pipeline import KeyFilter, Reducer, Stats, _object
from .progress import DEFAULT_INTERVAL as DEFAULT_PROGRESS_INTERVAL
from .progress import ProgressReporter
from .pseudonym import IdentityTable
from .timeutil import iso, iso_precise, to_naive_utc, utcnow

STATE_SCHEMA_VERSION = 2
#: ``watermark.json`` written by earlier versions is still readable (the per-repo
#: cursor was added in v2; a v1 file simply has no repo watermarks yet).
SUPPORTED_STATE_VERSIONS = frozenset({1, STATE_SCHEMA_VERSION})
PR_STATE_SCHEMA_VERSION = 1
STATE_FILE = "watermark.json"
PR_STATE_FILE = "prs.json"
IDENTITY_FILE = "identity.json"
# How far back to re-emit before the watermark, so a backdated/late activity is
# not lost between transfers (prd/11 §3/§8: default 1-7 days).
DEFAULT_OVERLAP_HOURS = 72

# Fields the reducer needs from a PR record. DC's list rows carry them all, so a
# detail request is only made when a source returns a trimmed summary row.
_DETAIL_FIELDS = ("createdDate", "state", "author", "fromRef", "toRef", "properties")


# --------------------------------------------------------------------------- #
# local state
# --------------------------------------------------------------------------- #
class ScrapeState:
    """Per-source watermark cursor, persisted next to the output bundle.

    Two cursors live here, with different jobs:

    * ``watermark_ts`` (per source) -- the max **event** ts emitted; it is what
      the tgz manifest carries and what import checks for monotonicity.
    * ``repos[label].updated_ts`` (per repo) -- the max **PR updatedDate** seen;
      it is the scrape-time cursor from prd/14 §2 and never moves backwards.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.sources: dict[str, dict] = {}
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                payload = {}
            if payload.get("schema_version") in (None, *SUPPORTED_STATE_VERSIONS):
                self.sources = dict(payload.get("sources") or {})

    def watermark(self, source: str) -> datetime | None:
        entry = self.sources.get(source) or {}
        value = entry.get("watermark_ts")
        return to_naive_utc(value) if value else None

    def runs(self, source: str) -> int:
        return int((self.sources.get(source) or {}).get("runs") or 0)

    def repo_watermarks(self, source: str) -> dict[str, datetime]:
        entry = self.sources.get(source) or {}
        out: dict[str, datetime] = {}
        for label, cursor in (entry.get("repos") or {}).items():
            value = (cursor or {}).get("updated_ts")
            if value:
                out[label] = to_naive_utc(value)
        return out

    def repo_watermark(self, source: str, repo_label: str) -> datetime | None:
        return self.repo_watermarks(source).get(repo_label)

    def record(
        self,
        source: str,
        watermark: datetime | None,
        run_id: str,
        bundle: Path,
        *,
        repos: dict[str, dict] | None = None,
    ) -> None:
        entry = dict(self.sources.get(source) or {})
        stored = dict(entry.get("repos") or {})
        if repos is not None:
            stored = repos
        self.sources[source] = {
            "watermark_ts": iso(watermark),
            "runs": self.runs(source) + 1,
            "last_run_id": run_id,
            "last_bundle": bundle.name,
            "updated_at": iso(utcnow()),
            "repos": stored,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "_comment": (
                        "Scraper-side watermark cursor. Local state, never transferred."
                    ),
                    "schema_version": STATE_SCHEMA_VERSION,
                    "sources": self.sources,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


class PullRequestState:
    """Per-PR scrape cursor (``prs.json``), the settled-PR set of prd/14 §3.

    ``done`` means "this PR is closed and we have everything it will ever emit":
    a MERGED/DECLINED PR that has been fully scraped. Such a PR is skipped with
    **zero** requests while it sits strictly below the repo watermark, and comes
    back as soon as its ``updatedDate`` moves.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.sources: dict[str, dict[str, dict]] = {}
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                payload = {}
            if payload.get("schema_version") in (None, PR_STATE_SCHEMA_VERSION):
                self.sources = {
                    str(source): dict(entries or {})
                    for source, entries in (payload.get("sources") or {}).items()
                }

    def get(self, source: str, pr_key: str) -> dict | None:
        return (self.sources.get(source) or {}).get(pr_key)

    def entries(self, source: str) -> dict[str, dict]:
        return dict(self.sources.get(source) or {})

    def done_count(self, source: str) -> int:
        return sum(1 for entry in self.entries(source).values() if entry.get("done"))

    def record(
        self,
        source: str,
        pr_key: str,
        *,
        state: str,
        updated: datetime | None,
        done: bool,
        ticket_keys: list[str],
    ) -> None:
        self.sources.setdefault(source, {})[pr_key] = {
            "state": str(state or "OPEN").upper(),
            # Precise on purpose: DC's updatedDate is epoch **milliseconds**, and
            # a second-truncated cursor would read as "moved" on every run.
            "updated_date": iso_precise(updated),
            "done": bool(done),
            "ticket_keys": sorted(ticket_keys),
            "scraped_at": iso(utcnow()),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "_comment": (
                        "Scraper-side per-PR cursor: settled (MERGED/DECLINED) PRs"
                        " are never re-fetched unless updatedDate moves. Local"
                        " state, never transferred."
                    ),
                    "schema_version": PR_STATE_SCHEMA_VERSION,
                    "sources": self.sources,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


# --------------------------------------------------------------------------- #
# reduction (source -> in-memory pseudonymised rows)
# --------------------------------------------------------------------------- #
@dataclass
class Collected:
    source: str
    as_of: datetime
    run_id: str
    events: list[dict] = field(default_factory=list)
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    requests: int = 0
    requests_by_kind: dict[str, int] = field(default_factory=dict)
    keys_by_placement: dict[str, int] = field(default_factory=dict)
    ticket_regex: str = ""
    ticket_projects: tuple[str, ...] = ()
    identity: IdentityTable | None = None
    #: repo label -> the PR-updatedDate cursor after this run (None = unknown)
    repo_watermarks: dict[str, datetime | None] = field(default_factory=dict)
    #: repo label -> per-repo counters for the report / state file
    repos: dict[str, dict] = field(default_factory=dict)
    #: the push classifier that ran (None when this source needs none)
    push_classifier: gitclass.PushClassifier | None = None
    #: the prd/16 text classifier (None when text classification is off)
    text_classifier: classify.Classifier | None = None

    def counts(self) -> dict:
        stats = self.stats
        by_type: dict[str, int] = {}
        for record in self.objects:
            by_type[record["object_type"]] = by_type.get(record["object_type"], 0) + 1
        return {
            "events": len(self.events),
            "objects": len(self.objects),
            "prs": by_type.get("PR", 0),
            "commits": by_type.get("COMMIT", 0),
            "tickets": by_type.get("TICKET", 0),
            "people": by_type.get("PERSON", 0),
            "prs_seen": stats.prs_seen,
            "prs_kept": stats.prs_kept,
            "prs_dropped": stats.prs_dropped,
            "prs_settled": stats.prs_settled,
            "prs_skipped_settled": stats.prs_skipped_settled,
            "prs_skipped_watermark": stats.prs_skipped_watermark,
        }


def collect(
    cfg: Config,
    transport,
    source: str,
    limit: int | None = None,
    *,
    identity: IdentityTable | None = None,
    state: ScrapeState | None = None,
    pr_state: PullRequestState | None = None,
    full: bool = False,
    progress: ProgressReporter | None = None,
    push_classifier: gitclass.PushClassifier | None = None,
    text_classifier: classify.Classifier | None = None,
) -> Collected:
    """Read the source and reduce it in memory. Nothing is written here.

    ``state`` / ``pr_state`` enable the incremental cursors; without them every
    PR that matches the filter is scraped (a plain full read). ``progress``
    emits interval-gated status lines to stderr (prd/14); nothing there can
    reach the bundle. ``push_classifier`` (prd/16) may be ``None``: a fixture
    bundle already carries its labels and needs no git cache.

    ``text_classifier`` (prd/16) reads the raw comment bodies and PR
    descriptions **before they are discarded** and hands back only closed
    labels; ``None`` disables text classification entirely (no text is read).
    """
    as_of = utcnow()
    run_id = f"{source}-{as_of.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    key_filter = KeyFilter.build(cfg.ticket_regex, cfg.key_shape, cfg.ticket_projects)
    ident = identity or IdentityTable(cfg.identity_path)
    client = BitbucketClient(
        transport, page_limit=cfg.page_limit, min_interval=0.0 if source == "fixture" else 0.2
    )
    reducer = Reducer(cfg, ident, key_filter, as_of, classifier=text_classifier)
    stats = Stats()
    out = Collected(
        source=source,
        as_of=as_of,
        run_id=run_id,
        stats=stats,
        ticket_regex=cfg.ticket_regex,
        ticket_projects=cfg.ticket_projects,
        push_classifier=push_classifier,
        text_classifier=text_classifier,
    )
    prefilter = getattr(cfg, "prefilter", True)

    def tick() -> None:
        """Interval-gated progress line (prd/14): a no-op most of the time."""
        if progress is None:
            return
        progress.tick(
            prs_seen=stats.prs_seen,
            prs_kept=stats.prs_kept,
            prs_skipped=(
                stats.prs_dropped + stats.prs_skipped_settled + stats.prs_skipped_watermark
            ),
            requests=client.requests,
            events=len(stats.events),
        )

    stop = False
    for project, repo in cfg.repos:
        label = f"{project}/{repo}"
        previous = None if full else (state.repo_watermark(source, label) if state else None)
        cursor = previous
        newest: datetime | None = None
        truncated = False
        early_stopped = False
        repo_seen = repo_kept = repo_settled = 0

        # NEWEST-first (DC 9.4.18 sorts by updatedDate desc), so pagination can
        # stop at the first full page that is entirely at/below the watermark.
        for page in client.iter_pull_request_pages(project, repo):
            for row in page:
                if limit is not None and stats.prs_seen >= limit:
                    truncated = True
                    stop = True
                    break
                stats.prs_seen += 1
                repo_seen += 1
                tick()
                pr = PullRequest(project, repo, dict(row))
                stamp = _updated_stamp(pr.pr)
                if stamp is not None:
                    newest = stamp if newest is None else max(newest, stamp)
                    stats.watermark = (
                        stamp if stats.watermark is None else max(stats.watermark, stamp)
                    )

                reason = _unchanged(pr, stamp, pr_state, source, previous, full)
                if reason is not None:
                    if reason == "settled":
                        stats.prs_skipped_settled += 1
                        repo_settled += 1
                    else:
                        stats.prs_skipped_watermark += 1
                    continue

                # -- layer 1: the cheap ticket-key pre-filter ------------------
                cheap_keys = _list_keys(key_filter, pr.pr) if prefilter else []
                if prefilter and not cheap_keys:
                    stats.prs_dropped += 1
                    continue

                # -- expensive: detail (only if the list row is trimmed) ----
                if _needs_detail(pr.pr):
                    pr.pr = _with_detail(pr.pr, client.pull_request(project, repo, pr.id))
                pr.commits = list(client.iter_commits(project, repo, pr.id))
                keys = key_filter.keys(
                    (pr.pr.get("fromRef") or {}).get("displayId"),
                    pr.pr.get("title"),
                    pr.pr.get("description"),
                    *[c.get("message") for c in pr.commits],
                )
                if not keys:
                    stats.prs_dropped += 1
                    # Nothing to store now or ever: remember it so a non-prefilter
                    # run does not pay for its commits again.
                    if pr_state is not None:
                        pr_state.record(
                            source, pr.label, state=pr.state, updated=stamp,
                            done=pr.closed, ticket_keys=[],
                        )
                    continue

                stats.prs_kept += 1
                repo_kept += 1
                _note_placements(reducer.key_filter, pr, stats)
                # activities are fetched only for PRs that survived the filter
                pr.activities = list(client.iter_activities(project, repo, pr.id))
                if push_classifier is not None:
                    # prd/16: classify the rescopes from the local git cache
                    # before the reduction turns them into pr_pushed events.
                    push_classifier.annotate(project, repo, pr.pr, pr.activities)
                    for name, value in push_classifier.counts.items():
                        stats.push_classes[name] = value
                    if text_classifier is not None:
                        # ... and whether the diff moved at each comment anchor,
                        # which is the deterministic half of thread_outcome.
                        push_classifier.annotate_threads(
                            project, repo, pr.pr, pr.activities, pr.commits
                        )
                tick()
                reducer.reduce(pr, keys, pr.activities, stats)
                if pr_state is not None:
                    pr_state.record(
                        source, pr.label, state=pr.state, updated=stamp,
                        done=pr.closed, ticket_keys=keys,
                    )
                    if pr.closed:
                        stats.prs_settled += 1

            if stop:
                break
            # -- layer 4: early termination (prd/14 §4) --------------------
            # A full page strictly below the watermark means every *older* page
            # is too (DC sorts NEWEST by updatedDate desc): stop paginating.
            # Strict `<` on purpose: a page tying the watermark is ambiguous and
            # is read through, which is safer and idempotent.
            if previous is not None and len(page) >= client.list_page_limit:
                stamps = [_updated_stamp(row) for row in page]
                if all(s is not None and s < previous for s in stamps):
                    early_stopped = True
                    break

        if not truncated and newest is not None:
            cursor = newest if cursor is None else max(cursor, newest)
        out.repo_watermarks[label] = cursor
        tick()
        out.repos[label] = {
            "prs_seen": repo_seen,
            "prs_kept": repo_kept,
            "prs_settled": repo_settled,
            "truncated": truncated,
            "early_stopped": early_stopped,
        }
        if stop:
            break

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
    out.keys_by_placement = dict(sorted(stats.keys_by_placement.items()))
    out.identity = ident
    return out


def _updated_stamp(row: dict) -> datetime | None:
    """The row's ``updatedDate`` (DC epochs are ms); ``None`` when it has none."""
    value = row.get("updatedDate") or row.get("createdDate") or 0
    return to_naive_utc(value) if value else None


def _list_keys(key_filter: KeyFilter, row: dict) -> list[str]:
    """The keys visible on the *cheap* list row (prd/14 §1).

    Title, source branch **and description** all arrive with the list page, so
    the pre-filter never needs a per-PR request to decide.
    """
    return key_filter.keys(
        (row.get("fromRef") or {}).get("displayId"),
        row.get("title"),
        row.get("description"),
    )


def _unchanged(
    pr: PullRequest,
    stamp: datetime | None,
    pr_state: PullRequestState | None,
    source: str,
    repo_watermark: datetime | None,
    full: bool,
) -> str | None:
    """Why this PR needs no per-PR request, or ``None`` when it must be read.

    ``"settled"`` -- a MERGED/DECLINED PR we already scraped below the
    watermark: its history is final (prd/14 §3).

    ``"watermark"`` -- any other PR already seen below the watermark: there is
    nothing new in it (prd/14 §2).

    The comparisons are **strict**: ``updatedDate >= watermark`` means "needs
    processing". ``updatedDate`` ties are common, so a row exactly *at* the
    cursor is ambiguous and is re-read -- import is idempotent, so that is
    harmless, while dropping it could lose an event. The per-PR cursor catches
    the other half of the ambiguity: a PR below the watermark but newer than the
    ``updatedDate`` we recorded has moved since we read it and is fetched too.
    An untracked PR is never skipped here -- the free list filter decides.
    """
    if full or pr_state is None or stamp is None:
        return None
    if repo_watermark is None or stamp >= repo_watermark:
        return None
    entry = pr_state.get(source, pr.label)
    if not entry:
        return None
    recorded = entry.get("updated_date")
    if recorded and stamp > to_naive_utc(recorded):
        return None
    return "settled" if (pr.closed and entry.get("done")) else "watermark"


def _needs_detail(row: dict) -> bool:
    """Does the list row lack something the reduction reads?

    DC 9.x's PR list returns full PR records, so this is normally ``False`` and a
    matching PR costs three requests (commits, activities, detail only if the
    summary was trimmed). The check keeps the scrape correct against a source
    that returns a leaner summary without making every run pay for it.
    """
    return any(field_name not in row for field_name in _DETAIL_FIELDS)


def _with_detail(row: dict, detail: dict) -> dict:
    """Merge a detail fetch *under* the list row (the summary stays authoritative).

    Empty values from the summary never overwrite a real value from the detail
    record: a list projection that carries ``properties: {}`` must not hide the
    merge commit the detail endpoint returned.
    """
    merged = dict(detail)
    for key, value in row.items():
        if value not in (None, "", [], {}):
            merged[key] = value
        else:
            merged.setdefault(key, value)
    return merged


def _note_placements(key_filter: KeyFilter, pr: PullRequest, stats: Stats) -> None:
    placements = {
        "branch": (pr.pr.get("fromRef") or {}).get("displayId") or "",
        "title": pr.pr.get("title") or "",
        "description": pr.pr.get("description") or "",
        "commit": "\n".join(c.get("message") or "" for c in pr.commits),
    }
    for name, text in placements.items():
        if key_filter.keys(text):
            stats.keys_by_placement[name] = stats.keys_by_placement.get(name, 0) + 1


# --------------------------------------------------------------------------- #
# scrape (reduction -> tgz)
# --------------------------------------------------------------------------- #
def default_state_dir(out: Path) -> Path:
    """``export/del.tgz`` -> ``export/del.state/`` (state stays next to output)."""
    out = Path(out)
    return out.parent / f"{out.stem}.state"


def build_manifest(collected: Collected, *, previous: datetime | None, watermark: datetime | None,
                   overlap_hours: int, full: bool, events_emitted: int) -> dict:
    """The bundle's manifest: provenance + counts, and nothing raw (prd/11 §2)."""
    counts = collected.counts()
    counts["events"] = events_emitted
    repos = sorted({str(e["repo"]) for e in collected.events if e.get("repo")})
    return {
        "schema_version": bundle_mod.SCHEMA_VERSION,
        "generator": {"name": "megaspeed-scrape", "version": _version()},
        "created_at": iso(collected.as_of),
        "source": collected.source,
        "sources": [collected.source],
        "classifier_version": taxonomy.CLASSIFIER_VERSION,
        "ticket_projects": list(collected.ticket_projects),
        "ticket_regex": collected.ticket_regex,
        "projects": sorted({repo.partition("/")[0] for repo in repos}),
        "repos": repos,
        "watermark_ts": iso(watermark),
        "watermarks": {collected.source: iso(watermark)},
        "previous_watermark_ts": iso(previous),
        "overlap_hours": overlap_hours,
        "full": bool(full),
        "counts": counts,
        "text_classifier": (
            collected.text_classifier.report()
            if collected.text_classifier is not None
            else {"enabled": False}
        ),
        "run": {
            "run_id": collected.run_id,
            "as_of": iso_precise(collected.as_of),
            "source": collected.source,
            "classifier_version": taxonomy.CLASSIFIER_VERSION,
            "prs_seen": collected.stats.prs_seen,
            "prs_kept": collected.stats.prs_kept,
            "prs_dropped": collected.stats.prs_dropped,
            "prs_settled": collected.stats.prs_settled,
            "prs_skipped_settled": collected.stats.prs_skipped_settled,
            "prs_skipped_watermark": collected.stats.prs_skipped_watermark,
            "early_stopped": sorted(
                label for label, info in collected.repos.items() if info.get("early_stopped")
            ),        },
        "requests": collected.requests,
        "requests_by_kind": dict(collected.requests_by_kind),
        "keys_by_placement": collected.keys_by_placement,
        "activities_by_action": dict(sorted(collected.stats.activities_by_action.items())),
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
    overlap_hours: int = DEFAULT_OVERLAP_HOURS,
    full: bool = False,
    identity_path: str | Path | None = None,
    progress: ProgressReporter | None = None,
    push_classifier: gitclass.PushClassifier | None = None,
    text_classifier: classify.Classifier | None = None,
) -> dict:
    """Scrape one source into ``out`` (a tgz) and update local state.

    ``full`` ignores the watermark and the settled-PR set and re-emits
    everything; otherwise only events newer than ``watermark - overlap_hours``
    are emitted (the overlap keeps backdated activities from being lost). The
    new watermark never moves backwards.

    ``progress`` defaults to an stderr reporter every
    :data:`ingest.progress.DEFAULT_INTERVAL` seconds; pass
    ``ProgressReporter(interval=0)`` to silence it. It never writes to stdout or
    into the bundle -- the tgz contract is unaffected.

    ``push_classifier`` defaults to the one the config/source imply (a live run
    gets a local shallow git cache under ``<state-dir>/git/``, prd/16); pass one
    explicitly to point it somewhere else, or ``None`` after disabling
    ``cfg.push_class``.

    ``text_classifier`` is the local-LLM classifier (prd/16) that labels
    comment bodies and PR descriptions before they are discarded. It defaults to
    what the config/source imply: the deterministic mock for ``--fixture``, the
    local Ollama box for a live run, ``None`` when ``cfg.text_classify`` is off.
    Raw text never crosses into the bundle -- only the labels do.
    """
    out = Path(out).expanduser()
    state_dir = Path(state_dir).expanduser() if state_dir else default_state_dir(out)
    state = ScrapeState(state_dir / STATE_FILE)
    pr_state = PullRequestState(state_dir / PR_STATE_FILE)
    ident_path = Path(identity_path).expanduser() if identity_path else state_dir / IDENTITY_FILE
    reporter = progress if progress is not None else ProgressReporter(
        interval=DEFAULT_PROGRESS_INTERVAL
    )
    classifier = (
        push_classifier
        if push_classifier is not None
        else gitclass.PushClassifier.for_scrape(cfg, source, state_dir)
    )
    text = (
        text_classifier
        if text_classifier is not None
        else classify.for_scrape(cfg, source)
    )

    previous = None if full else state.watermark(source)
    collected = collect(
        cfg,
        transport,
        source,
        limit,
        identity=IdentityTable(ident_path),
        state=state,
        pr_state=pr_state,
        full=full,
        progress=reporter,
        push_classifier=classifier,
        text_classifier=text,
    )

    cutoff = previous - timedelta(hours=overlap_hours) if previous else None
    events = collected.events if cutoff is None else [e for e in collected.events if e["ts"] >= cutoff]
    emitted = max((e["ts"] for e in events), default=None)
    watermark = max((w for w in (previous, emitted) if w is not None), default=None)

    manifest = build_manifest(
        collected,
        previous=previous,
        watermark=watermark,
        overlap_hours=overlap_hours,
        full=full,
        events_emitted=len(events),
    )
    written = bundle_mod.write_bundle(
        out, manifest=manifest, events=events, objects=collected.objects,
        relations=collected.relations,
    )

    repo_entries = {
        label: {
            # Milliseconds matter: the cursor is compared to DC's updatedDate.
            "updated_ts": iso_precise(collected.repo_watermarks.get(label)),
            **info,
        }
        for label, info in collected.repos.items()
    }
    state.record(source, watermark, collected.run_id, out, repos=repo_entries)
    state.save()
    pr_state.save()
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
        "pr_state": str(state_dir / PR_STATE_FILE),
        "identity_map": str(ident_path),
        "watermark": iso(watermark),
        "previous_watermark": iso(previous),
        "repo_watermarks": {
            label: iso(stamp) for label, stamp in sorted(collected.repo_watermarks.items())
        },
        "overlap_hours": overlap_hours,
        "full": bool(full),
        "requests": collected.requests,
        "requests_by_kind": dict(collected.requests_by_kind),
        "prs_seen": collected.stats.prs_seen,
        "prs_kept": collected.stats.prs_kept,
        "prs_dropped": collected.stats.prs_dropped,
        "prs_settled": collected.stats.prs_settled,
        "prs_skipped_settled": collected.stats.prs_skipped_settled,
        "prs_skipped_watermark": collected.stats.prs_skipped_watermark,
        "early_stopped": sorted(
            label for label, info in collected.repos.items() if info.get("early_stopped")
        ),
        "prs_settled_total": pr_state.done_count(source),
        "events": len(events),
        "events_collected": len(collected.events),
        "objects": len(collected.objects),
        "counts": manifest["counts"],
        "classifier_version": taxonomy.CLASSIFIER_VERSION,
        "ticket_regex": cfg.ticket_regex,
        "ticket_projects": list(cfg.ticket_projects),
        "keys_by_placement": collected.keys_by_placement,
        "activities_by_action": dict(sorted(collected.stats.activities_by_action.items())),
        "push_class": (
            collected.push_classifier.report()
            if collected.push_classifier is not None
            else {"enabled": False, "counts": {}, "reasons": {}, "fetches": 0, "repos": []}
        ),
        "text_classify": (
            {
                **collected.text_classifier.report(),
                "comment_types": dict(sorted(collected.stats.comment_types.items())),
                "thread_outcomes": dict(sorted(collected.stats.thread_outcomes.items())),
                "questions_raised": collected.stats.questions_raised,
                "questions_answered": collected.stats.questions_answered,
                "bot_comments_skipped": collected.stats.bot_comments_skipped,
            }
            if collected.text_classifier is not None
            else {"enabled": False}
        ),
    }

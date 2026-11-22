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
  pepper.json      # RAW ZONE: HMAC pepper for the prd/22 ref digests (0600).
                   #   Back it up: a new pepper cannot match old digests.
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

The source can also be a **hans scrape directory** (prd/31): a read-only
capture of the same REST payloads plus a bare git mirror, produced during a
migration. :class:`ingest.hans.HansTransport` serves those paths, the mirror
answers the push/diff questions, and everything below is unchanged. Such a
capture *is* its own scope, so a PR without a ticket key is imported rather than
dropped (``HansTransport.unscoped``).

Preconditions and crash safety
------------------------------

**The source must be quiescent.** A scrape is not a snapshot transaction: it
walks a repo page by page while people keep pushing. A corpus *rebuild* (e.g.
``corpus/generate.py`` re-seeding the lab, or a restore that rewrites PR ids)
running concurrently is worse than a normal push -- it can renumber or delete
rows mid-walk, so the run may see a PR twice, miss one, or record a cursor that
never existed. Keep rebuilds and scrapes mutually exclusive: do not start a
scrape while a rebuild/restore is in flight, and do not start a rebuild while a
scrape runs. Two guards help, neither is a substitute for that discipline:

* a **quiescence check** (live runs; ``verify_quiet``) re-reads the repo's
  newest row before and after the walk. A ``updatedDate`` that moved means the
  source changed under us: the run is reported as ``source_moved`` (and the
  operator is warned), so a torn snapshot is never mistaken for a clean one.
* an **in-flight state dir lock** is *not* taken; two scrapes sharing one
  ``--state-dir`` will still race on ``watermark.json``/``prs.json``. Use one
  state dir per source and one scrape at a time.

**A crash must not cost the whole run.** The bundle rows and the cursors are
checkpointed as the walk progresses (``checkpoint_every`` PRs and at every repo
boundary, :func:`scrape`), not only at the end. A checkpoint is **incremental**:
it stages only the rows added since the last one in ``<out>.parts/`` (an
append-only spool, :class:`ingest.bundle.BundleSpool`), so its cost is
proportional to the new work rather than to the bundle so far, and the tgz is
built once when the run finishes. Order is deliberate: identity map, then the
staged rows, then ``prs.json``, then ``watermark.json`` -- the last one is the
commit marker (``complete: false``). A run that dies leaves a readable spool plus
``complete: false``; the next run loads that checkpoint (``resumed: true``),
skips the PRs the interrupted run already checkpointed, and finishes the walk
instead of starting over.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import bundle as bundle_mod
from . import classify, gitclass, pepper as pepper_mod, taxonomy
from .bitbucket import BitbucketClient, PullRequest
from .config import Config
from .pipeline import KeyFilter, Reducer, Stats, _object
from .progress import DEFAULT_INTERVAL as DEFAULT_PROGRESS_INTERVAL
from .progress import ProgressReporter
from .pseudonym import IdentityTable
from .timeutil import iso, iso_precise, to_naive_utc, utcnow
from .transport import BitbucketError

STATE_SCHEMA_VERSION = 2
#: ``watermark.json`` written by earlier versions is still readable (the per-repo
#: cursor was added in v2; a v1 file simply has no repo watermarks yet).
SUPPORTED_STATE_VERSIONS = frozenset({1, STATE_SCHEMA_VERSION})
PR_STATE_SCHEMA_VERSION = 1
STATE_FILE = "watermark.json"
PR_STATE_FILE = "prs.json"
IDENTITY_FILE = "identity.json"
#: RAW ZONE: the per-tenant HMAC pepper for the prd/22 ref digests
#: (:mod:`ingest.pepper`). Never enters the bundle or the store.
PEPPER_FILE = pepper_mod.PEPPER_FILE
# How far back to re-emit before the watermark, so a backdated/late activity is
# not lost between transfers (prd/11 §3/§8: default 1-7 days).
DEFAULT_OVERLAP_HOURS = 72
#: Persist the bundle + cursors this often (kept PRs), plus at every repo
#: boundary. 0 disables checkpointing (the pre-crash-safety behaviour).
DEFAULT_CHECKPOINT_EVERY = 50

# Fields the reducer needs from a PR record. DC's list rows carry them all, so a
# detail request is only made when a source returns a trimmed summary row.
# ``description`` is here on purpose (FR-2): it is a *filter* input, and a list
# projection that omits it cannot decide whether a description-only key matches.
_DETAIL_FIELDS = ("createdDate", "state", "author", "fromRef", "toRef", "properties", "description")


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

    Plus the crash marker: ``complete`` is ``False`` while a run is in flight
    (each checkpoint rewrites the file) and ``True`` once the final bundle is
    written. ``last_bundle_path`` points at that in-flight bundle so a later run
    can resume from it (:func:`scrape`).
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

    def last_run_id(self, source: str) -> str | None:
        value = (self.sources.get(source) or {}).get("last_run_id")
        return str(value) if value else None

    def incomplete(self, source: str) -> bool:
        """Was the last run for this source interrupted (checkpointed, unfinished)?

        A state file written by an older build has no ``complete`` key at all;
        that is read as "complete" so a pre-checkpoint run is never resumed.
        """
        entry = self.sources.get(source) or {}
        return bool(entry) and entry.get("complete") is False

    def last_bundle_path(self, source: str) -> Path | None:
        entry = self.sources.get(source) or {}
        value = entry.get("last_bundle_path")
        return Path(value) if value else None

    def repo_entries(self, source: str) -> dict[str, dict]:
        return dict((self.sources.get(source) or {}).get("repos") or {})

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
        complete: bool = True,
        resumed: bool = False,
        count_run: bool = True,
        ref_digest_key_id: str | None = None,
    ) -> None:
        entry = dict(self.sources.get(source) or {})
        stored = dict(entry.get("repos") or {})
        if repos is not None:
            stored = repos
        # The ref-digest key id travels with the cursor: a changed pepper makes
        # every new digest incomparable with the rows already in the store, so
        # the next run must notice (see scrape()).
        key_id = ref_digest_key_id or entry.get("ref_digest_key_id")
        self.sources[source] = {
            "watermark_ts": iso(watermark),
            # A checkpoint is not a run: only the final write counts.
            "runs": self.runs(source) + (1 if count_run else 0),
            "last_run_id": run_id,
            "last_bundle": bundle.name,
            # Absolute on purpose: a resume may be asked for a different --out.
            "last_bundle_path": str(Path(bundle).resolve()),
            # False while a run is in flight; the resume contract (see scrape()).
            "complete": bool(complete),
            "resumed": bool(resumed),
            "ref_digest_key_id": key_id,
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
        run_id: str = "",
    ) -> None:
        self.sources.setdefault(source, {})[pr_key] = {
            "state": str(state or "OPEN").upper(),
            # Precise on purpose: DC's updatedDate is epoch **milliseconds**, and
            # a second-truncated cursor would read as "moved" on every run.
            "updated_date": iso_precise(updated),
            "done": bool(done),
            "ticket_keys": sorted(ticket_keys),
            # Which run wrote this entry. A resumed run skips exactly the entries
            # of the interrupted run it is resuming (see ``_unchanged``).
            "run_id": str(run_id or ""),
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
    #: Explicit ticket-key set the run was scoped to (prd/18); empty = prefixes.
    ticket_keys: tuple[str, ...] = ()
    identity: IdentityTable | None = None
    #: repo label -> the PR-updatedDate cursor after this run (None = unknown)
    repo_watermarks: dict[str, datetime | None] = field(default_factory=dict)
    #: repo label -> per-repo counters for the report / state file
    repos: dict[str, dict] = field(default_factory=dict)
    #: the push classifier that ran (None when this source needs none)
    push_classifier: gitclass.PushClassifier | None = None
    #: the prd/16 text classifier (None when text classification is off)
    text_classifier: classify.Classifier | None = None
    #: PRs whose per-PR fetch failed (404, reset, ...) and were skipped, so one
    #: bad PR can never abort the run: ``{repo, pr_id, error}`` per row.
    failures: list[dict] = field(default_factory=list)
    #: Events/objects loaded from the bundle of an interrupted run (resume).
    carried_events: list[dict] = field(default_factory=list)
    #: True when this run resumed an interrupted one (see ``scrape``).
    resumed: bool = False
    #: Repos whose newest PR moved *while* the walk was running (quiescence).
    source_moved: list[str] = field(default_factory=list)

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
            "prs_failed": len(self.failures),
            "prs_settled": stats.prs_settled,
            "prs_skipped_settled": stats.prs_skipped_settled,
            "prs_skipped_watermark": stats.prs_skipped_watermark,
            "prs_skipped_resumed": stats.prs_skipped_resumed,
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
    resume_run_id: str | None = None,
    carry_events: list[dict] | None = None,
    carry_objects: list[dict] | None = None,
    checkpoint=None,
    checkpoint_every: int = 0,
    verify_quiet: bool = False,
    pepper: bytes | None = None,
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

    Crash safety (see the module docstring):

    * ``checkpoint(collected, completed, current)`` is called every
      ``checkpoint_every`` kept PRs and at every repo boundary, so the caller can
      persist a partial bundle + cursor without waiting for the end of the run.
    * ``resume_run_id`` / ``carry_events`` / ``carry_objects`` resume an
      interrupted run: the PRs it already checkpointed (same ``run_id``, same
      ``updatedDate``) are skipped, and the rows it wrote are carried into this
      run's bundle so nothing is lost.
    * ``verify_quiet`` re-reads each repo's newest row after the walk and records
      a repo that moved under us in ``source_moved`` (the quiescence guard).

    A per-PR fetch failure (a 404 on one PR's commits, a reset that outlived the
    transport retries) is **skipped and counted** in ``failures`` -- one bad PR
    never aborts the run, and it is not recorded as done, so the next run retries
    it.
    """
    as_of = utcnow()
    run_id = f"{source}-{as_of.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    key_filter = KeyFilter.build(
        cfg.ticket_regex, cfg.key_shape, cfg.ticket_projects, cfg.ticket_keys
    )
    ident = identity or IdentityTable(cfg.identity_path)
    # prd/31: a hans scrape ships the instance's user list. Seeding it makes the
    # pseudonyms independent of PR order -- someone who only ever commented (or
    # was mentioned) gets the same pseudonym as someone who authored a PR.
    for user in getattr(transport, "users", None) or ():
        if isinstance(user, dict) and user:
            ident.pseudonym(user)
    # prd/31: a hans scrape *is* the scope: it holds exactly the PRs that were
    # captured, so a PR with no ticket key is still imported (its PR/commit/
    # review history is real data). The filter still attaches keys where present.
    unscoped = bool(getattr(transport, "unscoped", False))
    client = BitbucketClient(
        transport,
        page_limit=cfg.page_limit,
        min_interval=0.0 if source in ("fixture", "hans") else 0.2,
    )
    reducer = Reducer(cfg, ident, key_filter, as_of, classifier=text_classifier, pepper=pepper)
    stats = Stats()
    out = Collected(
        source=source,
        as_of=as_of,
        run_id=run_id,
        stats=stats,
        ticket_regex=cfg.ticket_regex,
        ticket_projects=cfg.ticket_projects,
        ticket_keys=cfg.ticket_keys,
        push_classifier=push_classifier,
        text_classifier=text_classifier,
        resumed=bool(carry_events or carry_objects),
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

    def _refresh(collected_now: Collected) -> None:
        """Refresh the row views from the in-flight accumulator.

        ``stats`` is the accumulator during the walk (``collected.events`` is
        only filled at the end), so a mid-run checkpoint must ask for a refresh
        first -- otherwise it would write an empty bundle.
        """
        collected_now.events = list(stats.events)
        collected_now.relations = list(stats.relations)
        collected_now.objects = _merge_objects(
            carry_objects or [], [*stats.objects, *_people_objects(stats, reducer)]
        )
        collected_now.carried_events = list(carry_events or [])

    stop = False
    completed: list[str] = []
    pending = 0  # kept PRs since the last checkpoint
    for project, repo in cfg.repos:
        label = f"{project}/{repo}"
        previous = None if full else (state.repo_watermark(source, label) if state else None)
        cursor = previous
        newest: datetime | None = None
        truncated = False
        early_stopped = False
        repo_seen = repo_kept = repo_settled = repo_skipped = repo_failed = 0
        # Quiescence guard, half 1: the newest row before the walk.
        opened_at = _newest_stamp(client, project, repo) if verify_quiet else None

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

                reason = _unchanged(pr, stamp, pr_state, source, previous, full, resume_run_id)
                if reason is not None:
                    repo_skipped += 1
                    if reason == "settled":
                        stats.prs_skipped_settled += 1
                        repo_settled += 1
                    elif reason == "resumed":
                        stats.prs_skipped_resumed += 1
                    else:
                        stats.prs_skipped_watermark += 1
                    continue

                # A per-PR fetch failure (a 404 on one PR's commits, a reset that
                # outlived the transport retries) skips *this* PR: one bad row
                # must never abort a two-hour walk. Nothing is recorded for it,
                # so the next run retries it (and the partial guard counts it).
                try:
                    # -- layer 1: the cheap ticket-key pre-filter --------------
                    cheap_keys = _list_keys(key_filter, pr.pr) if prefilter else []
                    detail_fetched = False
                    if prefilter and not cheap_keys and not unscoped:
                        if "description" in pr.pr:
                            stats.prs_dropped += 1
                            continue
                        # The list projection omitted ``description``. If it is
                        # otherwise a complete PR record, the only key may be in
                        # the body, so one detail request decides it (FR-2). A
                        # wholly trimmed projection is different: fetching detail
                        # for every PR would defeat the pre-filter, so its cheap
                        # fields are treated as authoritative (the detail is
                        # still fetched for every PR that *does* match).
                        if not all(
                            field in pr.pr for field in _DETAIL_FIELDS if field != "description"
                        ):
                            stats.prs_dropped += 1
                            continue
                        pr.pr = _with_detail(pr.pr, client.pull_request(project, repo, pr.id))
                        detail_fetched = True
                        cheap_keys = _list_keys(key_filter, pr.pr)
                        if not cheap_keys:
                            stats.prs_dropped += 1
                            continue

                    # -- expensive: detail (only if the list row is trimmed) ----
                    if not detail_fetched and _needs_detail(pr.pr):
                        pr.pr = _with_detail(pr.pr, client.pull_request(project, repo, pr.id))
                    pr.commits = list(client.iter_commits(project, repo, pr.id))
                    # hans only: stamp the mirror-derived base on the kept PR's
                    # record for the diff-size classifier. iter_commits already
                    # computed and cached it, so this is a dictionary lookup.
                    if push_classifier is not None and "megaspeedDiffBase" not in pr.pr:
                        base = client.diff_base(project, repo, pr.id)
                        if base:
                            pr.pr["megaspeedDiffBase"] = base
                    keys = key_filter.keys(
                        (pr.pr.get("fromRef") or {}).get("displayId"),
                        pr.pr.get("title"),
                        pr.pr.get("description"),
                        *[c.get("message") for c in pr.commits],
                    )
                    if not keys and not unscoped:
                        stats.prs_dropped += 1
                        # Nothing to store now or ever: remember it so a
                        # non-prefilter run does not pay for its commits again.
                        if pr_state is not None:
                            pr_state.record(
                                source, pr.label, state=pr.state, updated=stamp,
                                done=pr.closed, ticket_keys=[], run_id=run_id,
                            )
                        continue

                    stats.prs_kept += 1
                    repo_kept += 1
                    _note_placements(reducer.key_filter, pr, stats)
                    # activities are fetched only for PRs that survived the filter
                    pr.activities = list(client.iter_activities(project, repo, pr.id))
                    # prd/44 DES-2: the commit's CI build is a separate DC
                    # resource. One request for the head commit; a forge
                    # without the module (404) is "no CI data", never a
                    # skipped PR.
                    head_sha = (pr.pr.get("fromRef") or {}).get("latestCommit")
                    if head_sha:
                        try:
                            pr.build_statuses = [
                                {**record, "commitId": head_sha}
                                for record in client.build_statuses(head_sha)
                                if isinstance(record, dict)
                            ]
                        except BitbucketError:
                            pr.build_statuses = []
                    if push_classifier is not None:
                        # prd/16: classify the rescopes from the local git cache
                        # before the reduction turns them into pr_pushed events.
                        push_classifier.annotate(project, repo, pr.pr, pr.activities)
                        for name, value in push_classifier.counts.items():
                            stats.push_classes[name] = value
                        # ... and measure the PR's final diff size (numstat totals)
                        # from the same cache, for the analysis-side pr_size metric.
                        push_classifier.annotate_diff_size(project, repo, pr.pr, pr.commits)
                        if text_classifier is not None:
                            # ... and whether the diff moved at each comment
                            # anchor, the deterministic half of thread_outcome.
                            push_classifier.annotate_threads(
                                project, repo, pr.pr, pr.activities, pr.commits
                            )
                    tick()
                    if (source != "fixture" and text_classifier is not None
                            and progress is not None and progress.enabled
                            and stats.prs_kept <= 3):
                        progress.emit(
                            f"[scrape] {pr.label}: classifying text with {text_classifier.version}…"
                        )
                    reducer.reduce(pr, keys, pr.activities, stats)
                    if pr_state is not None:
                        pr_state.record(
                            source, pr.label, state=pr.state, updated=stamp,
                            done=pr.closed, ticket_keys=keys, run_id=run_id,
                        )
                        if pr.closed:
                            stats.prs_settled += 1
                except BitbucketError as exc:
                    repo_failed += 1
                    out.failures.append({"repo": label, "pr_id": pr.id, "error": str(exc)})
                    if progress is not None:
                        progress.emit(f"[scrape] skipped {pr.label}: {exc}")
                    continue

                # Checkpoint the bundle + cursors as we go (see scrape()): a crash
                # costs the PRs since the last checkpoint, not the whole run.
                if checkpoint is not None and checkpoint_every > 0:
                    pending += 1
                    if pending >= checkpoint_every:
                        pending = 0
                        _refresh(out)
                        checkpoint(out, completed, label)

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

        # Quiescence guard, half 2: did the newest row move under the walk?
        if verify_quiet:
            closed_at = _newest_stamp(client, project, repo)
            if opened_at is not None and closed_at is not None and closed_at > opened_at:
                out.source_moved.append(label)
                if progress is not None:
                    progress.emit(
                        f"[scrape] warning: {label} changed during the walk"
                        f" (newest PR {iso(opened_at)} -> {iso(closed_at)});"
                        " the source was not quiescent"
                    )

        if not truncated and newest is not None:
            cursor = newest if cursor is None else max(cursor, newest)
        out.repo_watermarks[label] = cursor
        tick()
        out.repos[label] = {
            "prs_seen": repo_seen,
            "prs_kept": repo_kept,
            "prs_settled": repo_settled,
            "prs_skipped": repo_skipped,
            "prs_failed": repo_failed,
            "truncated": truncated,
            "early_stopped": early_stopped,
        }
        if stop:
            break
        completed.append(label)
        if checkpoint is not None and checkpoint_every > 0:
            pending = 0
            _refresh(out)
            checkpoint(out, completed, None)

    _refresh(out)
    out.requests = client.requests
    out.requests_by_kind = dict(sorted(client.requests_by_kind.items()))
    out.keys_by_placement = dict(sorted(stats.keys_by_placement.items()))
    out.identity = ident
    return out


def _people_objects(stats: Stats, reducer: Reducer) -> list[dict]:
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


def _newest_stamp(client, project: str, repo: str) -> datetime | None:
    """The repo's newest PR ``updatedDate`` (one cheap ``limit=1`` request).

    ``None`` when the probe itself fails: a guard must never be the reason a
    scrape dies.
    """
    try:
        row = client.newest_pull_request(project, repo)
    except BitbucketError:
        return None
    return _updated_stamp(row) if row else None


def _merge_objects(carried: list[dict], fresh: list[dict]) -> list[dict]:
    """Resume merge: the rows of the interrupted bundle, then this run's rows.

    This run's row wins over a carried one for a shared ``object_id`` (it is the
    newer read of the same object), so the merged set is exactly the union the
    final bundle needs.

    The de-duplication runs even without a carry-over: the reducer can emit the
    same id twice in one run (a stacked PR reports the commit its child branch
    also reports, so the two rows disagree on ``pr_id``), and the store keeps the
    **first** row it is handed for a natural key. Keeping one row per id -- first
    wins inside this run -- therefore leaves the store's contents exactly as they
    were, and it is what lets an incremental checkpoint stage each object once.
    """
    merged: dict[str, dict] = {}
    for row in fresh:
        merged.setdefault(str(row["object_id"]), row)
    if not carried:
        return list(merged.values())
    union = {str(row["object_id"]): row for row in carried}
    union.update(merged)  # this run's row wins over the carried one
    return list(union.values())


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
    resume_run_id: str | None = None,
) -> str | None:
    """Why this PR needs no per-PR request, or ``None`` when it must be read.

    ``"settled"`` -- a MERGED/DECLINED PR we already scraped below the
    watermark: its history is final (prd/14 §3).

    ``"watermark"`` -- any other PR already seen below the watermark: there is
    nothing new in it (prd/14 §2).

    ``"resumed"`` -- this PR was already read and checkpointed by the run we are
    resuming (its entry carries that run's ``run_id`` and the same
    ``updatedDate``). The events live in the carried-over bundle, so paying for
    the PR again would only re-do work a crash already paid for.

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
    entry = pr_state.get(source, pr.label)
    if resume_run_id and entry and entry.get("run_id") == resume_run_id:
        recorded = entry.get("updated_date")
        if not recorded or stamp <= to_naive_utc(recorded):
            return "resumed"
    if repo_watermark is None or stamp >= repo_watermark:
        return None
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
                   overlap_hours: int, full: bool, events_emitted: int,
                   checkpoint: bool = False, ref_digest_key_id: str | None = None) -> dict:
    """The bundle's manifest: provenance + counts, and nothing raw (prd/11 §2)."""
    counts = collected.counts()
    counts["events"] = events_emitted
    repos = sorted({
        str(e["repo"])
        for e in [*collected.carried_events, *collected.events]
        if e.get("repo")
    })
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
        "projects": sorted({repo.partition("/")[0] for repo in repos}),
        "repos": repos,
        "watermark_ts": iso(watermark),
        "watermarks": {collected.source: iso(watermark)},
        "previous_watermark_ts": iso(previous),
        "overlap_hours": overlap_hours,
        "full": bool(full),
        "counts": counts,
        # prd/22 + SEC-1: *which* per-tenant pepper produced this bundle's ref
        # digests. A non-reversible label (never the key): two bundles with the
        # same id produce comparable digests, and a changed id means the store's
        # older ref rows can no longer be matched.
        "ref_digest_key_id": ref_digest_key_id,
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
            "prs_failed": len(collected.failures),
            "prs_settled": collected.stats.prs_settled,
            "prs_skipped_settled": collected.stats.prs_skipped_settled,
            "prs_skipped_watermark": collected.stats.prs_skipped_watermark,
            "prs_skipped_resumed": collected.stats.prs_skipped_resumed,
            # Provenance for the crash-safety machinery: this tgz is an
            # in-flight checkpoint, and/or continues an interrupted run.
            "checkpoint": bool(checkpoint),
            "resumed": bool(collected.resumed),
            "source_moved": list(collected.source_moved),
            "early_stopped": sorted(
                label for label, info in collected.repos.items() if info.get("early_stopped")
            ),
        },
        "requests": collected.requests,
        "requests_by_kind": dict(collected.requests_by_kind),
        "keys_by_placement": collected.keys_by_placement,
        "activities_by_action": dict(sorted(collected.stats.activities_by_action.items())),
    }


def _version() -> str:
    from . import __version__

    return __version__


#: A run that *lists* fewer than this share of the previous run's PRs is
#: suspicious (the bundle would be silently partial), unless it legitimately
#: stopped early at the watermark or was capped with ``--n``.
PARTIAL_RATIO = 0.5
#: ... and only once the previous run was big enough for a ratio to mean
#: anything: a 3-PR repo shrinking to 2 is not news.
PARTIAL_MIN_BASELINE = 20


def partial_report(collected: Collected, prior_repos: dict[str, dict]) -> dict:
    """Is this bundle implausibly small compared to the previous run?

    The failure this guards against is the worst kind: a run that saw a handful
    of PRs (a half-restored source, a repo whose listing silently ended, a
    broken filter) still wrote a *valid* bundle, reported success and exited 0.
    The next incremental run then treated that tiny snapshot as the new baseline
    and the gap was invisible.

    The baseline is the previous run's per-repo counters in ``watermark.json``.
    A legitimately smaller run is excluded on purpose: one that stopped at the
    watermark (``early_stopped``) or was capped by ``--n`` (``truncated``) *must*
    see fewer rows. A run whose PRs were all skipped as settled is explained by
    ``prs_skipped``, not by a shrinking source.

    The caller decides what to do with it (:mod:`ingest.scrape_cli` warns and
    exits non-zero unless ``--allow-partial``/``--force``).
    """
    reasons: list[dict] = []
    for label, info in sorted(collected.repos.items()):
        if info.get("early_stopped") or info.get("truncated"):
            continue
        prior = prior_repos.get(label) or {}
        seen = int(info.get("prs_seen") or 0)
        kept = int(info.get("prs_kept") or 0)
        skipped = int(info.get("prs_skipped") or 0)
        prior_seen = int(prior.get("prs_seen") or 0)
        prior_kept = int(prior.get("prs_kept") or 0)
        if prior_seen >= PARTIAL_MIN_BASELINE and seen < prior_seen * PARTIAL_RATIO:
            reasons.append({
                "repo": label,
                "reason": "too few PRs listed",
                "detail": f"saw {seen} PR(s); the previous run saw {prior_seen}",
            })
        elif (
            prior_kept >= PARTIAL_MIN_BASELINE
            and kept == 0
            and seen > 0
            and skipped == 0
        ):
            reasons.append({
                "repo": label,
                "reason": "no PR kept",
                "detail": (
                    f"kept 0 of {seen} PR(s); the previous run kept {prior_kept}"
                    " (ticket filter or source?)"
                ),
            })
    return {
        "suspicious": bool(reasons),
        "reasons": reasons,
        "failures": list(collected.failures),
        "baseline": {
            label: {"prs_seen": int(entry.get("prs_seen") or 0),
                    "prs_kept": int(entry.get("prs_kept") or 0)}
            for label, entry in sorted(prior_repos.items())
        },
    }


def _merge_events(carried: list[dict], fresh: list[dict]) -> list[dict]:
    """Resume merge: the interrupted run's events, then this run's.

    This run's row wins for a shared ``event_key`` (identical content in
    practice); the union is what the final bundle must carry, because the
    checkpointed bundle was never imported. One row per key -- also the shape an
    incremental checkpoint needs to stage each event exactly once.
    """
    merged = {str(event["event_key"]): event for event in carried}
    merged.update({str(event["event_key"]): event for event in fresh})
    return list(merged.values())


def _write_bundle_atomic(
    path: Path,
    *,
    manifest: dict,
    events: list[dict],
    objects: list[dict],
    relations: list[tuple[str, str, str]],
) -> dict:
    """Write the tgz through a temp file + rename.

    A checkpoint runs after every N PRs, so a kill *during* a write must not
    leave a truncated tgz where the resume expects a readable one: the old file
    (or nothing) stays until the new one is complete.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    os.close(handle)
    temp = Path(temp_name)
    try:
        written = bundle_mod.write_bundle(
            temp, manifest=manifest, events=events, objects=objects, relations=relations
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    written["path"] = str(path)
    return written


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
    checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
    verify_quiet: bool | None = None,
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

    **Crash safety.** The identity map, the rows added since the last checkpoint
    and both cursors are checkpointed every ``checkpoint_every`` kept PRs and at
    every repo boundary (``0`` disables, giving the old end-of-run-only
    behaviour). A checkpoint stages only the **delta** in ``<out>.parts/`` and
    the tgz is written once at the end, so checkpoints stay cheap however large
    the bundle grows (:class:`ingest.bundle.BundleSpool`). The write order is
    identity -> staged rows -> ``prs.json`` -> ``watermark.json``, and
    ``watermark.json`` is the commit marker (``complete: false`` until the final
    bundle lands). If the run dies, the next call with the same ``--state-dir``
    resumes: it reads the checkpointed rows back, carries them into the new
    bundle, and skips the PRs the interrupted run already read (matched by
    ``run_id`` + ``updatedDate``). The report says so via ``resumed``.

    **Quiescence.** ``verify_quiet`` (default: on for a live source, off for a
    fixture) re-reads each repo's newest PR before and after the walk; a repo
    that moved is listed in ``source_moved`` and warned about, so a corpus
    rebuild that raced the scrape is visible instead of silent. It costs one
    ``limit=1`` request per repo. It cannot make a scrape transactional -- see
    the module docstring for the precondition.
    """
    out = Path(out).expanduser()
    state_dir = Path(state_dir).expanduser() if state_dir else default_state_dir(out)
    state = ScrapeState(state_dir / STATE_FILE)
    pr_state = PullRequestState(state_dir / PR_STATE_FILE)
    ident_path = Path(identity_path).expanduser() if identity_path else state_dir / IDENTITY_FILE
    reporter = progress if progress is not None else ProgressReporter(
        interval=DEFAULT_PROGRESS_INTERVAL
    )
    # SEC-1: the per-tenant ref-digest pepper lives in the raw zone (the state
    # dir), never in the bundle. Loading it creates it on first use (0600).
    pepper_path = state_dir / PEPPER_FILE
    pepper = pepper_mod.load_pepper(pepper_path)
    ref_digest_key_id = pepper_mod.key_id(pepper)
    prior_key_id = (state.sources.get(source) or {}).get("ref_digest_key_id")
    if prior_key_id and prior_key_id != ref_digest_key_id:
        raise SystemExit(
            f"ref-digest pepper changed under {state_dir} (was {prior_key_id}, now"
            f" {ref_digest_key_id}): the new digests cannot be matched against the"
            " rows already in the store, so the stack DAG would silently split."
            " Restore the original pepper.json, or re-scrape with --full and"
            " re-import into a fresh store."
        )
    if not prior_key_id and state.watermark(source) is not None:
        reporter.emit(
            f"[scrape] warning: {state_dir} has no recorded ref-digest key id:"
            " refs scraped before this build were unkeyed (16 hex) and will not"
            " match this run's digests -- re-scrape with --full to re-key them"
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
    if reporter.enabled and source != "fixture":
        # Say what is about to happen *before* the first request: a text
        # classifier's first batch can take minutes on a CPU-only box, and the
        # interval tick does not fire until the first PR completes. Fixture runs
        # stay silent (their tests pin an empty stderr; they finish instantly).
        repos_label = ", ".join(f"{project}/{repo}" for project, repo in cfg.repos) or "—"
        key_scope = (
            f" · key-scoped ({len(cfg.ticket_keys)} key(s))"
            if cfg.ticket_keys and not getattr(transport, "unscoped", False) else ""
        )
        if text is None:
            text_label = "text off"
        else:
            model = getattr(text, "model", "") or text.version
            workers = getattr(text, "workers", 1)
            url = getattr(text, "url", "")
            text_label = (
                f"text {text.backend}:{model}{('@' + url) if url else ''} ({workers}w)"
            )
        reporter.emit(
            f"[scrape] {source} · {repos_label} · writing {out}"
            f"{key_scope} · {text_label}"
        )
        if text is not None:
            labeled = {"n": 0}

            def _text_progress(done: int, total: int) -> None:
                labeled["n"] += 1
                if labeled["n"] == 1 or labeled["n"] % 10 == 0:
                    reporter.emit(
                        f"[scrape] text labels: {labeled['n']} classified"
                        f" · {text.version} ({done}/{total} in this batch)"
                    )

            text.on_progress = _text_progress

            def _text_failed(exc: Exception) -> None:
                fallback = getattr(getattr(text, "fallback", None), "version", "mock")
                reporter.emit(
                    f"[scrape] text classifier failed: {exc} — falling back to"
                    f" {fallback} (later failures are silent)"
                )

            text.on_fallback = _text_failed
    quiet_check = (source not in ("fixture", "hans")) if verify_quiet is None else bool(verify_quiet)
    prior_repo_entries = state.repo_entries(source)
    # Crash safety is incremental (see ``checkpoint``): the in-flight rows are
    # staged in ``<out>.parts/`` and the tgz is written once, at the end.
    spool = bundle_mod.BundleSpool(bundle_mod.spool_dir(out))

    # -- resume an interrupted run (module docstring "crash safety") --------
    resume_run_id: str | None = None
    resume_bundle: Path | None = None
    carried_bundle: bundle_mod.Bundle | None = None
    carry_events: list[dict] = []
    carry_objects: list[dict] = []
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
                    carry_events, carry_objects = carried.events, carried.objects
                    reporter.emit(
                        f"[scrape] resuming interrupted run {resume_run_id}"
                        f" from {resume_bundle}: carrying {len(carry_events)} event(s),"
                        f" {len(carry_objects)} object(s)"
                    )
    if carried_bundle is None:
        # A fresh run: never let a dead run's staged rows leak into this bundle.
        spool.reset()
    else:
        spool.resume(carried_bundle)

    previous = None if full else state.watermark(source)
    # FR-1: the overlap cutoff is for PRs whose history is already in the store.
    # A PR this scraper has never kept (no ``prs.json`` entry, or one recorded
    # without ticket keys) must be emitted in full regardless of age, otherwise
    # its entire history is dropped and it is marked settled forever.
    prior_prs = {} if full else pr_state.entries(source)
    cutoff = previous - timedelta(hours=overlap_hours) if previous else None

    def _fresh_events(collected_now: Collected) -> list[dict]:
        if cutoff is None:
            return list(collected_now.events)

        def _already_kept(event: dict) -> bool:
            entry = prior_prs.get(f"{event.get('repo')}#{event.get('pr_id')}")
            return bool(entry and entry.get("ticket_keys"))

        return [e for e in collected_now.events if e["ts"] >= cutoff or not _already_kept(e)]

    def _emitted_events(collected_now: Collected) -> list[dict]:
        # The carried rows are unioned in *after* the cutoff on purpose: the
        # checkpointed bundle was never imported, so the overlap window (which
        # assumes the store already has the older history) must not drop them.
        return _merge_events(collected_now.carried_events, _fresh_events(collected_now))

    def _repo_entries(
        collected_now: Collected, current: str | None, *, final: bool
    ) -> dict[str, dict]:
        entries = {label: dict(entry) for label, entry in state.repo_entries(source).items()}
        for label, info in collected_now.repos.items():
            entries[label] = {
                # Milliseconds matter: the cursor is compared to DC's updatedDate.
                "updated_ts": iso_precise(collected_now.repo_watermarks.get(label)),
                **info,
                "complete": True,
            }
        if not final and current and current not in collected_now.repos:
            # Mid-repo: never advance that repo's cursor past PRs we have not
            # read (the resume re-walks from the previous cursor and skips the
            # per-PR entries this run checkpointed).
            entry = dict(entries.get(current) or {})
            entry.setdefault("updated_ts", None)
            entry["in_progress"] = True
            entries[current] = entry
        return entries

    def checkpoint(collected_now: Collected, completed: list[str], current: str | None) -> None:
        """Persist identity + the new rows + both cursors, mid-run.

        Incremental on purpose: only the rows added since the last checkpoint are
        staged (``<out>.parts/``), never the whole bundle re-serialised, and the
        tgz is written once when the run finishes (see
        :class:`ingest.bundle.BundleSpool`). The crash-resume contract is
        unchanged -- a run that dies still leaves a readable checkpoint that the
        next run resumes from.
        """
        events_now = _emitted_events(collected_now)
        emitted = max((e["ts"] for e in events_now), default=None)
        watermark_now = max((w for w in (previous, emitted) if w is not None), default=None)
        manifest = build_manifest(
            collected_now,
            previous=previous,
            watermark=watermark_now,
            overlap_hours=overlap_hours,
            full=full,
            events_emitted=len(events_now),
            checkpoint=True,
            ref_digest_key_id=ref_digest_key_id,
        )
        # Identity first: the bundle names these pseudonyms, so the map must
        # never lag behind a bundle that mentions them.
        if collected_now.identity is not None:
            collected_now.identity.save()
        spool.append(
            manifest=manifest,
            events=events_now,
            objects=collected_now.objects,
            relations=collected_now.relations,
        )
        pr_state.save()
        state.record(
            source, watermark_now, collected_now.run_id, out,
            repos=_repo_entries(collected_now, current, final=False),
            complete=False, resumed=collected_now.resumed, count_run=False,
            ref_digest_key_id=ref_digest_key_id,
        )
        state.save()

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
        resume_run_id=resume_run_id,
        carry_events=carry_events,
        carry_objects=carry_objects,
        checkpoint=checkpoint,
        checkpoint_every=max(0, int(checkpoint_every)),
        verify_quiet=quiet_check,
        pepper=pepper,
    )

    events = _emitted_events(collected)
    emitted = max((e["ts"] for e in events), default=None)
    watermark = max((w for w in (previous, emitted) if w is not None), default=None)

    manifest = build_manifest(
        collected,
        previous=previous,
        watermark=watermark,
        overlap_hours=overlap_hours,
        full=full,
        events_emitted=len(events),
        ref_digest_key_id=ref_digest_key_id,
    )
    if collected.identity is not None:
        collected.identity.save()
    written = _write_bundle_atomic(
        out, manifest=manifest, events=events, objects=collected.objects,
        relations=collected.relations,
    )
    pr_state.save()
    state.record(
        source, watermark, collected.run_id, out,
        repos=_repo_entries(collected, None, final=True),
        complete=True, resumed=collected.resumed,
        ref_digest_key_id=ref_digest_key_id,
    )
    state.save()
    # The tgz is durable and the state file says the run is complete: the
    # in-flight rows have done their job.
    spool.finish()
    partial = partial_report(collected, prior_repo_entries)

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
        # Where the incremental checkpoints were staged (removed on success).
        "checkpoint_spool": str(spool.directory),
        # SEC-1: the raw-zone pepper path + a non-reversible id for it. Neither
        # the pepper nor anything derived from it beyond the id is reported.
        "ref_digest_pepper": str(pepper_path),
        "ref_digest_key_id": ref_digest_key_id,
        "watermark": iso(watermark),
        "previous_watermark": iso(previous),
        "repo_watermarks": {
            label: iso(stamp) for label, stamp in sorted(collected.repo_watermarks.items())
        },
        "overlap_hours": overlap_hours,
        "full": bool(full),
        "resumed": bool(collected.resumed),
        "resumed_from": str(resume_bundle) if resume_bundle else None,
        "checkpoint_every": max(0, int(checkpoint_every)),
        "source_moved": list(collected.source_moved),
        "partial": partial,
        "events_collected": len(collected.events),
        "events_carried": len(collected.carried_events),
        "events_dropped_watermark": len(collected.events) - len(_fresh_events(collected)),
        "requests": collected.requests,
        "requests_by_kind": dict(collected.requests_by_kind),
        "prs_seen": collected.stats.prs_seen,
        "prs_kept": collected.stats.prs_kept,
        "prs_dropped": collected.stats.prs_dropped,
        "prs_failed": len(collected.failures),
        "failures": list(collected.failures),
        "prs_settled": collected.stats.prs_settled,
        "prs_skipped_settled": collected.stats.prs_skipped_settled,
        "prs_skipped_watermark": collected.stats.prs_skipped_watermark,
        "prs_skipped_resumed": collected.stats.prs_skipped_resumed,
        "early_stopped": sorted(
            label for label, info in collected.repos.items() if info.get("early_stopped")
        ),
        "prs_settled_total": pr_state.done_count(source),
        "events": len(events),
        "objects": len(collected.objects),
        "counts": manifest["counts"],
        "classifier_version": taxonomy.CLASSIFIER_VERSION,
        "ticket_regex": cfg.ticket_regex,
        "ticket_projects": list(cfg.ticket_projects),
        "ticket_keys": list(cfg.ticket_keys),
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

"""Scrape side of the airgap: source -> classify -> pseudonymise -> tgz.

This is the half that runs **next to the source** (prd/11 §1). It reads raw
Bitbucket DC payloads, reduces them to keys / pseudonyms / closed labels with
:mod:`ingest.pipeline`, and writes a transfer bundle (:mod:`ingest.bundle`).
It never opens the DuckDB store: the scraper artifact is stdlib-only, and the
pseudonymised zone is the *import* side's business.

Local state, kept **next to the output** and never inside the tgz (prd/11 §3,§5):

```
<state-dir>/
  watermark.json   # per-source cursor: max event ts emitted so far
  identity.json    # RAW ZONE: slug/email -> pseudonym. Back this up.
```

The identity map is the one irreplaceable piece of scrape-side state: losing it
breaks pseudonym continuity across incremental transfers.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import bundle as bundle_mod
from . import taxonomy
from .bitbucket import BitbucketClient, PullRequest
from .config import Config
from .pipeline import KeyFilter, Reducer, Stats, _object
from .pseudonym import IdentityTable
from .timeutil import iso, iso_precise, to_naive_utc, utcnow

STATE_SCHEMA_VERSION = 1
STATE_FILE = "watermark.json"
IDENTITY_FILE = "identity.json"
# How far back to re-emit before the watermark, so a backdated/late activity is
# not lost between transfers (prd/11 §3/§8: default 1-7 days).
DEFAULT_OVERLAP_HOURS = 72


# --------------------------------------------------------------------------- #
# local state
# --------------------------------------------------------------------------- #
class ScrapeState:
    """Per-source watermark cursor, persisted next to the output bundle."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.sources: dict[str, dict] = {}
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                payload = {}
            if payload.get("schema_version") in (None, STATE_SCHEMA_VERSION):
                self.sources = dict(payload.get("sources") or {})

    def watermark(self, source: str) -> datetime | None:
        entry = self.sources.get(source) or {}
        value = entry.get("watermark_ts")
        return to_naive_utc(value) if value else None

    def runs(self, source: str) -> int:
        return int((self.sources.get(source) or {}).get("runs") or 0)

    def record(self, source: str, watermark: datetime | None, run_id: str, bundle: Path) -> None:
        self.sources[source] = {
            "watermark_ts": iso(watermark),
            "runs": self.runs(source) + 1,
            "last_run_id": run_id,
            "last_bundle": bundle.name,
            "updated_at": iso(utcnow()),
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
    keys_by_placement: dict[str, int] = field(default_factory=dict)
    ticket_regex: str = ""
    ticket_projects: tuple[str, ...] = ()
    identity: IdentityTable | None = None

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
        }


def collect(
    cfg: Config,
    transport,
    source: str,
    limit: int | None = None,
    *,
    identity: IdentityTable | None = None,
) -> Collected:
    """Read the source and reduce it in memory. Nothing is written here."""
    as_of = utcnow()
    run_id = f"{source}-{as_of.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    key_filter = KeyFilter.build(cfg.ticket_regex, cfg.key_shape, cfg.ticket_projects)
    ident = identity or IdentityTable(cfg.identity_path)
    client = BitbucketClient(
        transport, page_limit=cfg.page_limit, min_interval=0.0 if source == "fixture" else 0.2
    )
    reducer = Reducer(cfg, ident, key_filter, as_of)
    stats = Stats()
    out = Collected(
        source=source,
        as_of=as_of,
        run_id=run_id,
        stats=stats,
        ticket_regex=cfg.ticket_regex,
        ticket_projects=cfg.ticket_projects,
    )

    stop = False
    for project, repo in cfg.repos:
        for pr in client.pull_requests(project, repo):
            if limit is not None and stats.prs_seen >= limit:
                stop = True
                break
            stats.prs_seen += 1
            if pr.updated_ms:
                stamp = to_naive_utc(pr.updated_ms)
                stats.watermark = stamp if stats.watermark is None else max(stats.watermark, stamp)
            keys = reducer.key_filter.keys(
                (pr.pr.get("fromRef") or {}).get("displayId"),
                pr.pr.get("title"),
                pr.pr.get("description"),
                *[c.get("message") for c in pr.commits],
            )
            if not keys:
                stats.prs_dropped += 1
                continue
            stats.prs_kept += 1
            _note_placements(reducer.key_filter, pr, stats)
            # activities are fetched only for PRs that survived the filter
            pr.activities = list(client.iter_activities(project, repo, pr.id))
            reducer.reduce(pr, keys, pr.activities, stats)
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
    out.keys_by_placement = dict(sorted(stats.keys_by_placement.items()))
    out.identity = ident
    return out


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
        "run": {
            "run_id": collected.run_id,
            "as_of": iso_precise(collected.as_of),
            "source": collected.source,
            "classifier_version": taxonomy.CLASSIFIER_VERSION,
            "prs_seen": collected.stats.prs_seen,
            "prs_kept": collected.stats.prs_kept,
            "prs_dropped": collected.stats.prs_dropped,
        },
        "requests": collected.requests,
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
) -> dict:
    """Scrape one source into ``out`` (a tgz) and update local state.

    ``full`` ignores the watermark and re-emits everything; otherwise only
    events newer than ``watermark - overlap_hours`` are emitted (the overlap
    keeps backdated activities from being lost). The new watermark never moves
    backwards.
    """
    out = Path(out).expanduser()
    state_dir = Path(state_dir).expanduser() if state_dir else default_state_dir(out)
    state = ScrapeState(state_dir / STATE_FILE)
    ident_path = Path(identity_path).expanduser() if identity_path else state_dir / IDENTITY_FILE

    previous = None if full else state.watermark(source)
    collected = collect(cfg, transport, source, limit, identity=IdentityTable(ident_path))

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

    state.record(source, watermark, collected.run_id, out)
    state.save()
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
        "identity_map": str(ident_path),
        "watermark": iso(watermark),
        "previous_watermark": iso(previous),
        "overlap_hours": overlap_hours,
        "full": bool(full),
        "requests": collected.requests,
        "prs_seen": collected.stats.prs_seen,
        "prs_kept": collected.stats.prs_kept,
        "prs_dropped": collected.stats.prs_dropped,
        "events": len(events),
        "events_collected": len(collected.events),
        "objects": len(collected.objects),
        "counts": manifest["counts"],
        "classifier_version": taxonomy.CLASSIFIER_VERSION,
        "ticket_regex": cfg.ticket_regex,
        "ticket_projects": list(cfg.ticket_projects),
        "keys_by_placement": collected.keys_by_placement,
        "activities_by_action": dict(sorted(collected.stats.activities_by_action.items())),
    }

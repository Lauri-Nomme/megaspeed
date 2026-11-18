"""Import side of the airgap: tgz -> DuckDB, via the existing store writer.

This is the half that runs on the **main** side (prd/11 §1). It has no source
credentials and never sees raw content: it reads a bundle
(:mod:`ingest.bundle`), checks it, and loads the rows with
:func:`ingest.store.write_events` -- idempotent by natural key, so re-importing
a bundle (or an overlapping incremental one) is safe.

Order of operations is the contract (prd/11 §6): checksum + schema + classifier
+ watermark monotonicity are checked *before* the store is opened for writing,
and a bundle that fails is rejected, not merged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import taxonomy
from .bundle import Bundle, BundleError, read_bundle
from .config import Config, load_config
from .pipeline import fill_derived, recompute_derived
from .store import (
    ACTOR_RE,
    CONTENT_HASH_RE,
    EVENT_KEY_RE,
    HEAD_COMMIT_RE,
    REPO_RE,
    REPO_SCOPED_SOURCES,
    RUN_ID_RE,
    SOURCES,
    connect,
    ensure_schema,
    object_shape_problems,
    summary,
    utcnow,
    verify,
    write_events,
    write_objects,
    write_run,
)

#: Sources this build knows how to name in ``ingest_runs`` (store.verify enforces
#: the same set, but the gate must reject before a single row is written).
ALLOWED_SOURCES = SOURCES


class ImportRejected(RuntimeError):
    """The bundle is structurally fine but may not be merged into this store."""


def _matches(pattern: str, value) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def validate_bundle(
    bundle: Bundle,
    *,
    key_shape: str,
    ticket_projects: tuple[str, ...],
    ticket_keys: tuple[str, ...],
) -> None:
    """The pre-write half of the pseudonymisation gate (SEC-1).

    ``read_bundle`` checks the container (checksums, schema, closed
    vocabularies); this checks the **content class** of every row the scraper
    sent: actors must be pseudonyms, hashes must be hashes, event keys / run ids
    / object ids must be identifiers, ticket keys must match the configured
    scope, and attribute bags must be closed. A violation raises
    :class:`ImportRejected` *before* the store is opened for writing, so a
    tampered tgz cannot leave even one row behind.

    It deliberately re-implements nothing from ``verify`` that can be shared:
    object-id/state shapes come from :func:`store.object_shape_problems`, so the
    gate and the audit agree by construction.
    """
    problems: list[str] = []
    key_set = tuple(sorted({str(k).strip().upper() for k in ticket_keys or () if str(k).strip()}))
    projects = tuple(str(p).upper() for p in ticket_projects or ())
    # prd/31: a repo-scoped capture (a hans scrape) imports PRs that reference no
    # ticket key -- the capture is the scope. Every other source keeps the guard.
    repo_scoped = bundle.source in REPO_SCOPED_SOURCES

    def key_ok(value) -> bool:
        key = str(value)
        if not re.fullmatch(key_shape, key):
            return False
        if key_set:
            return key.upper() in key_set
        return key.partition("-")[0] in projects

    if bundle.source not in ALLOWED_SOURCES:
        problems.append(f"source {bundle.source!r} is not one of {ALLOWED_SOURCES}")
    run_info = bundle.manifest.get("run") or {}
    run_id = run_info.get("run_id")
    if run_id is not None and not _matches(RUN_ID_RE, str(run_id)):
        problems.append(f"run_id {run_id!r} is not an identifier")

    object_ids: set[tuple[str, str]] = set()
    for obj in bundle.objects:
        otype = obj["object_type"]
        oid = obj["object_id"]
        object_ids.add((otype, oid))
        problems.extend(object_shape_problems(otype, oid, obj.get("state"), key_shape, key_set))
        actor = obj.get("actor")
        if actor not in (None, "") and not _matches(ACTOR_RE, str(actor)):
            problems.append(f"objects.actor {actor!r} is not a pseudonym shape ({oid})")
        head = obj.get("head_commit")
        if head not in (None, "") and not _matches(HEAD_COMMIT_RE, str(head)):
            problems.append(f"objects.head_commit {head!r} is not a commit hash ({oid})")
        repo = obj.get("repo")
        if repo not in (None, "") and not _matches(REPO_RE, str(repo)):
            problems.append(f"objects.repo {repo!r} is not a repo slug ({oid})")
        message_class = obj.get("message_class")
        if message_class not in (None, "") and message_class not in taxonomy.MESSAGE_CLASS:
            problems.append(f"objects.message_class {message_class!r} is outside the vocabulary ({oid})")
        for key in obj.get("ticket_keys") or []:
            if not key_ok(key):
                problems.append(f"objects.ticket_keys {key!r} is outside the configured scope ({oid})")
        if otype == "PR" and not obj.get("ticket_keys") and not repo_scoped:
            problems.append(f"PR object {oid} carries no matched ticket key")
        if otype in ("STORY", "EPIC") and not obj.get("ticket_keys"):
            problems.append(f"{otype} object {oid} carries no ticket key")
        try:
            taxonomy.check_object_attributes(obj.get("attributes") or {})
        except taxonomy.TaxonomyError as exc:
            problems.append(f"objects.attributes for {oid}: {exc}")

    for event in bundle.events:
        key = event["event_key"]
        if not _matches(EVENT_KEY_RE, key):
            problems.append(f"events.event_key {key!r} is not an identifier")
        if not _matches(ACTOR_RE, str(event["actor"])):
            problems.append(f"events.actor {event['actor']!r} is not a pseudonym shape ({key})")
        content_hash = event.get("content_hash")
        if content_hash not in (None, "") and not _matches(CONTENT_HASH_RE, str(content_hash)):
            problems.append(f"events.content_hash {content_hash!r} is not a digest ({key})")
        repo = event.get("repo")
        if repo not in (None, "") and not _matches(REPO_RE, str(repo)):
            problems.append(f"events.repo {repo!r} is not a repo slug ({key})")
        if event.get("classifier_version") not in taxonomy.CLASSIFIER_VERSIONS:
            problems.append(
                f"events.classifier_version {event.get('classifier_version')!r}"
                f" is not known ({key})"
            )
        try:
            taxonomy.check_attributes(event.get("attributes") or {})
        except taxonomy.TaxonomyError as exc:
            problems.append(f"events.attributes for {key}: {exc}")
        for object_id in event.get("objects") or []:
            otype = str(object_id).partition(":")[0]
            if (otype, object_id) not in object_ids:
                problems.append(f"event {key} references an object not in the bundle: {object_id}")

    if problems:
        shown = "\n  - ".join(problems[:50])
        more = f"\n  … and {len(problems) - 50} more" if len(problems) > 50 else ""
        raise ImportRejected(
            f"bundle {bundle.path} violates the pseudonymisation contract;"
            f" nothing was written:\n  - {shown}{more}"
        )


def _previous_watermark(con, source: str):
    row = con.execute(
        "SELECT max(source_watermark) FROM ingest_runs WHERE source = ?", [source]
    ).fetchone()
    return row[0] if row else None


def check_watermark(con, bundle: Bundle) -> None:
    """New watermark must not go backwards for a source (prd/11 §6)."""
    previous = _previous_watermark(con, bundle.source)
    incoming = bundle.watermark
    if previous is not None and incoming is not None and incoming < previous:
        raise ImportRejected(
            f"watermark goes backwards for {bundle.source}:"
            f" bundle {incoming} < store {previous}"
        )


def import_bundle(cfg: Config, path: str | Path, *, verify_contract: bool = True) -> dict:
    """Validate ``path`` and load it into ``cfg.db_path``.

    The order is the gate (SEC-1, prd/11 §6): the full bundle is shape-checked
    **before** the store is opened, then the merge runs in one transaction and
    ``verify`` is the final check before ``COMMIT``. A bundle that fails either
    check leaves the store exactly as it was.
    """
    bundle = read_bundle(path)
    imported_at = utcnow()
    run_info = dict(bundle.manifest.get("run") or {})
    source = bundle.source
    run_id = str(run_info.get("run_id") or f"import-{imported_at.strftime('%Y%m%dT%H%M%S')}")
    as_of = run_info.get("as_of") or bundle.manifest.get("created_at") or imported_at
    projects = bundle.ticket_projects or cfg.ticket_projects
    keys = bundle.ticket_keys or cfg.ticket_keys

    # SEC-1: nothing below this line may write a row unless every actor, hash,
    # key and identifier in the bundle is of the expected shape.
    validate_bundle(
        bundle, key_shape=cfg.key_shape, ticket_projects=projects, ticket_keys=keys
    )

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(cfg.db_path)
    try:
        ensure_schema(con)
        check_watermark(con, bundle)
        before = con.execute("SELECT count(*) FROM events").fetchone()[0]
        report = None
        con.execute("BEGIN")
        try:
            # Derived attributes are a function of the *whole* stored log, so
            # union the bundle with what the store already knows before writing.
            fill_derived(con, bundle.events)
            written, rewritten = write_events(con, bundle.events, bundle.relations)
            write_objects(con, bundle.objects)
            derived = recompute_derived(
                con, {(e["repo"], e["pr_id"]) for e in bundle.events if e.get("repo")}
            )
            write_run(
                con,
                {
                    "run_id": run_id,
                    "source": source,
                    "as_of": as_of,
                    "source_watermark": bundle.watermark,
                    "classifier_version": bundle.classifier_version,
                    # prd/27: the resolved scope the bundle was scraped with, so
                    # ``--verify`` can enforce the same set on the store.
                    "ticket_keys": list(keys),
                    "prs_seen": int(run_info.get("prs_seen") or 0),
                    "prs_kept": int(run_info.get("prs_kept") or 0),
                    "prs_dropped": int(run_info.get("prs_dropped") or 0),
                    "issues_seen": int(run_info.get("issues_seen") or 0),
                    "issues_kept": int(run_info.get("issues_kept") or 0),
                    "issues_skipped": int(run_info.get("issues_skipped") or 0),
                    "events_written": written,
                    "events_skipped": len(bundle.events) - written,
                },
            )
            report = verify(con, cfg.key_shape, projects, keys) if verify_contract else None
            if report is not None and not report["ok"]:
                raise ImportRejected(
                    "bundle merged but the store failed the pseudonymisation"
                    " contract; rolled back:\n  - "
                    + "\n  - ".join(report["problems"])
                )
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        after = con.execute("SELECT count(*) FROM events").fetchone()[0]
        totals = summary(con)
    finally:
        con.close()

    return {
        "mode": "import",
        "bundle": str(bundle.path),
        "bundle_sha256": bundle.sha256,
        "source": source,
        "run_id": run_id,
        "as_of": as_of,
        "db": str(cfg.db_path),
        "classifier_version": bundle.classifier_version,
        "schema_version": bundle.manifest.get("schema_version"),
        "ticket_projects": list(projects),
        "ticket_keys": list(keys),
        "watermark": bundle.manifest.get("watermark_ts"),
        "events_loaded": len(bundle.events),
        "events_new": int(after - before),
        "events_rewritten": rewritten,
        "events_derived": derived,
        "objects": len(bundle.objects),
        "verified": bool(report["ok"]) if report else None,
        "problems": list(report["problems"]) if report else [],
        "summary": totals,
    }


def _print_import(r: dict) -> None:
    print(f"bundle         {r['bundle']}  sha256 {r['bundle_sha256'][:16]}…")
    print(f"source         {r['source']} (run {r['run_id']})")
    print(f"schema         v{r['schema_version']} · classifier {r['classifier_version']}"
          f" · watermark {r['watermark']}")
    print(f"store          {r['db']}")
    print(
        f"events         {r['events_loaded']} loaded, {r['events_new']} new, "
        f"{r['events_rewritten']} rewritten, {r['events_derived']} re-derived"
    )
    print(f"objects        {r['objects']}")
    if r.get("ticket_keys"):
        # prd/27: the scope the bundle was scraped with (an epic set resolved to
        # epics + children looks exactly like an explicit key set here).
        print(f"scope          {len(r['ticket_keys'])} explicit ticket key(s)")
    s = r["summary"]
    print(f"store now      {s['events']} events, {s['prs']} PRs, {s['commits']} commits, as_of {s['as_of']}")
    if s.get("stories") or s.get("epics"):
        print(
            f"ticket layer   {s.get('epics', 0)} epics, {s.get('stories', 0)} stories,"
            f" {s.get('sprints', 0)} sprints"
        )
    if r["verified"] is None:
        print("contract       skipped (--no-verify)")
    else:
        print(f"contract       {'OK — no free text, no canaries' if r['verified'] else 'VIOLATED'}")


def build_parser(prog: str = "python3 -m ingest import") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Import a scrape bundle (.tgz) into the pseudonymised DuckDB store.",
    )
    ap.add_argument("bundle", help="path to the .tgz written by the scrape side")
    ap.add_argument("--db", default=None, help="override store path (or MEGASPEED_DB)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the pseudonymisation contract check (debug only)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.db:
        cfg.db_path = Path(args.db).expanduser().resolve()

    try:
        report = import_bundle(cfg, args.bundle, verify_contract=not args.no_verify)
    except BundleError as exc:
        print(f"bundle rejected: {exc}", file=sys.stderr)
        return 2
    except ImportRejected as exc:
        print(f"import rejected: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_import(report)
    if report["verified"] is False:
        print("\nPSEUDONYMISATION CONTRACT VIOLATED:", file=sys.stderr)
        for problem in report["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


__all__ = ["BundleError", "ImportRejected", "bundles_supported", "import_bundle", "main"]


def bundles_supported() -> tuple[int, ...]:
    """Schema versions this build can import (for the CLI/README)."""
    from .bundle import SUPPORTED_SCHEMA_VERSIONS

    return tuple(sorted(SUPPORTED_SCHEMA_VERSIONS))

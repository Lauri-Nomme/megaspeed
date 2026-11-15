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
import sys
from pathlib import Path

from .bundle import Bundle, BundleError, read_bundle
from .config import Config, load_config
from .pipeline import fill_derived, recompute_derived
from .store import connect, ensure_schema, summary, utcnow, verify, write_events, write_objects, write_run


class ImportRejected(RuntimeError):
    """The bundle is structurally fine but may not be merged into this store."""


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
    """Validate ``path`` and load it into ``cfg.db_path``."""
    bundle = read_bundle(path)
    imported_at = utcnow()
    run_info = dict(bundle.manifest.get("run") or {})
    source = bundle.source
    run_id = str(run_info.get("run_id") or f"import-{imported_at.strftime('%Y%m%dT%H%M%S')}")
    as_of = run_info.get("as_of") or bundle.manifest.get("created_at") or imported_at
    projects = bundle.ticket_projects or cfg.ticket_projects
    keys = bundle.ticket_keys or cfg.ticket_keys

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(cfg.db_path)
    try:
        ensure_schema(con)
        check_watermark(con, bundle)
        before = con.execute("SELECT count(*) FROM events").fetchone()[0]
        # Derived attributes are a function of the *whole* stored log, so union
        # the bundle with what the store already knows before writing.
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
                "prs_seen": int(run_info.get("prs_seen") or 0),
                "prs_kept": int(run_info.get("prs_kept") or 0),
                "prs_dropped": int(run_info.get("prs_dropped") or 0),
                "events_written": written,
                "events_skipped": len(bundle.events) - written,
            },
        )
        after = con.execute("SELECT count(*) FROM events").fetchone()[0]
        report = verify(con, cfg.key_shape, projects, keys) if verify_contract else None
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
    s = r["summary"]
    print(f"store now      {s['events']} events, {s['prs']} PRs, {s['commits']} commits, as_of {s['as_of']}")
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

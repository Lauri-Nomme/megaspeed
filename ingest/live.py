"""``--live`` dev shortcut: scrape -> import in one process (no real airgap).

Production is always **scrape next to the source -> transfer the tgz -> import**.
This module exists so a developer (and the test suite) can do both halves in one
command without pretending the boundary is gone: it writes a real bundle to a
file first and then imports *that file*, so the tgz contract is exercised on
every dev run.

    python3 -m ingest --live --repo DEL/delivery-api ...
    python3 -m ingest --live --bundle export/del.tgz --state-dir export/del.state

Without ``--bundle``/``--state-dir`` the bundle and the watermark live in a
per-run temporary directory: the run is then always a full scrape, which is what
a dev loop wants. Point both at real paths to get incremental behaviour.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .config import Config
from .importer import import_bundle
from .scrape import DEFAULT_OVERLAP_HOURS, scrape


def run(
    cfg: Config,
    transport,
    source: str,
    limit: int | None = None,
    *,
    out: str | Path | None = None,
    state_dir: str | Path | None = None,
    overlap_hours: int = DEFAULT_OVERLAP_HOURS,
    full: bool = False,
) -> dict:
    """Scrape, write a bundle, import it, and return the combined report."""
    with tempfile.TemporaryDirectory(prefix="megaspeed-scrape-") as scratch:
        bundle_path = Path(out).expanduser() if out else Path(scratch) / f"{source}.tgz"
        scrape_report = scrape(
            cfg,
            transport,
            source,
            out=bundle_path,
            # No explicit state dir -> no watermark continuity inside the dev
            # loop: every run is a full scrape of the source.
            state_dir=Path(state_dir).expanduser() if state_dir else Path(scratch) / "state",
            limit=limit,
            overlap_hours=overlap_hours,
            full=full or state_dir is None,
            identity_path=cfg.identity_path,
        )
        import_report = import_bundle(cfg, bundle_path)

    return {
        "run_id": scrape_report["run_id"],
        "source": source,
        "as_of": scrape_report["as_of"],
        "db": import_report["db"],
        "bundle": scrape_report["bundle"],
        "bundle_bytes": scrape_report["bundle_bytes"],
        "bundle_sha256": scrape_report["bundle_sha256"],
        "state_dir": scrape_report["state_dir"],
        "identity_map": scrape_report["identity_map"],
        "requests": scrape_report["requests"],
        "requests_by_kind": scrape_report["requests_by_kind"],
        "prs_seen": scrape_report["prs_seen"],
        "prs_kept": scrape_report["prs_kept"],
        "prs_dropped": scrape_report["prs_dropped"],
        "prs_settled": scrape_report["prs_settled"],
        "prs_skipped_settled": scrape_report["prs_skipped_settled"],
        "prs_skipped_watermark": scrape_report["prs_skipped_watermark"],
        "early_stopped": scrape_report["early_stopped"],
        "events_emitted": scrape_report["events"],
        "events_new": import_report["events_new"],
        "events_rewritten": import_report["events_rewritten"],
        "events_derived": import_report["events_derived"],
        "objects": scrape_report["objects"],
        "watermark": scrape_report["watermark"],
        "activities_by_action": scrape_report["activities_by_action"],
        "keys_by_placement": scrape_report["keys_by_placement"],
        "verified": import_report["verified"],
        "problems": import_report["problems"],
        "summary": import_report["summary"],
        "ticket_regex": scrape_report["ticket_regex"],
        "ticket_projects": scrape_report["ticket_projects"],
        "ticket_keys": scrape_report["ticket_keys"],
    }

#!/usr/bin/env python3
"""Rebuild ``data/del-combined.duckdb`` on the install's *actual* workflow.

The ``jira-lab`` container runs Atlassian's default workflow (To Do / In
Progress / Done), so a live scrape of it cannot carry ``in_review`` or
``in_verification``. Installing the custom workflow into Jira DC 9.17 is fragile
(no public REST, a finicky XML import, and a healthy lab to lose). This script
takes the deterministic path instead, and it is reproducible:

1. read each ticket's first PR open / last merged close from the **Bitbucket**
   store (``data/del3.duckdb``, the 131-PR DEL corpus -- unchanged);
2. generate the DEL Jira bundle on the real Story + Bug workflows
   (:func:`ingest.jira_fixtures.generate_del`), backdated onto those PRs;
3. scrape that bundle through the real fixture transport with the workflow
   status -> stage map configured (``[jira.stages]`` / ``MEGASPEED_JIRA_STAGES``),
   so the bundle (and its manifest) carries stage labels, not Jira categories;
4. import it into a **copy** of the Bitbucket store under the same raw-zone
   identity map (``data/identity.json``), so one human keeps one pseudonym
   across both sources;
5. verify the pseudonymisation contract and the stages, and only then replace
   the live store -- the previous ``del-combined`` keeps working until the new
   one is proven.

The Jira side is a fixture because the lab cannot run the workflow; everything
downstream of the transport is the production path (the same reducer, bundle,
import gate and store the live scrape uses).

Usage
-----
    python3 jira-lab/rebuild_combined.py
    python3 jira-lab/rebuild_combined.py --check-only
    python3 jira-lab/rebuild_combined.py --bitbucket-store data/del3.duckdb \\
        --out data/del-combined.duckdb --identity data/identity.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import backdate  # noqa: E402  (jira-lab/backdate.py: the PR-timeline reader)

from ingest import jira_fixtures  # noqa: E402
from ingest.config import load_config  # noqa: E402
from ingest.importer import import_bundle  # noqa: E402
from ingest.progress import ProgressReporter  # noqa: E402
from ingest.scrape_jira import scrape  # noqa: E402
from ingest.store import connect, verify  # noqa: E402
from ingest.transport import FixtureJiraTransport  # noqa: E402


def log(message: str) -> None:
    print(f"[combined] {message}", flush=True)


def _stage_map_env(stage_map: dict[str, str]) -> str:
    return ",".join(f"{name}={stage}" for name, stage in stage_map.items())


def configure(stage_map: dict[str, str], identity: Path, store: Path) -> None:
    """Deployment settings for this rebuild (env wins over ingest/config.toml)."""
    os.environ["MEGASPEED_TICKET_PROJECTS"] = "DEL"
    os.environ["MEGASPEED_JIRA_PROJECTS"] = "DEL"
    os.environ["MEGASPEED_JIRA_STAGES"] = _stage_map_env(stage_map)
    os.environ["MEGASPEED_JIRA_MIN_INTERVAL"] = "0"
    os.environ["MEGASPEED_IDENTITY"] = str(identity)
    os.environ["MEGASPEED_DB"] = str(store)


def _require_bitbucket_only(store: Path) -> None:
    """The base must be the PR half only: two Jira projections must never mix."""
    con = connect(store, read_only=True)
    try:
        stories = con.execute(
            "SELECT count(*) FROM objects WHERE object_type IN ('STORY', 'EPIC')"
        ).fetchone()[0]
        runs = [row[0] for row in con.execute(
            "SELECT DISTINCT source FROM ingest_runs").fetchall()]
    finally:
        con.close()
    if stories:
        raise SystemExit(
            f"{store} already holds {stories} Jira object(s): start from the "
            "Bitbucket-only store (data/del3.duckdb), not a combined one"
        )
    if "bitbucket-dc" not in runs:
        raise SystemExit(f"{store} has no bitbucket-dc run: it is not the DEL corpus")


def _stage_counts(con) -> dict[str, int]:
    rows = con.execute(
        "SELECT json_extract_string(attributes, '$.to_status') AS stage, count(*)"
        " FROM events WHERE activity = 'status_changed' GROUP BY 1 ORDER BY 1"
    ).fetchall()
    return {str(stage): int(count) for stage, count in rows if stage}


def _report(store: Path) -> dict:
    """The same read the UI does: stages, boundaries and the QA segment."""
    from analysis import ballcourt, calendar as workcal, jira, rules, tickets

    calendar = workcal.load(
        tenant="del",
        tenants_dir=REPO / "data",
        config_path=REPO / "config" / "calendar.default.yaml",
    )
    con = connect(store, read_only=True)
    try:
        court = ballcourt.load(con, calendar=calendar)
        data = jira.load(con, court)
        window = rules.window_for(court, None, data)
        metric = rules.metrics(court, [], window, {}, data)["waiting_time_decomposition"]
        if not jira.uses_verification(data):
            raise SystemExit("the rebuilt store has no in_verification stage")
        if "verification_done" not in metric["segments"]:
            raise SystemExit("the rebuilt store has no verification->done segment")
        qa = metric["segments"]["verification_done"]["hours"]
        if not qa or qa <= 0:
            raise SystemExit("the rebuilt store's verification->done segment is empty")

        # A closed story with a real QA wait (the docs' DEL-167 example),
        # a bug, and a still-open story.
        sample = ["DEL-167", "DEL-105", "DEL-199"]
        detail = {}
        for key in sample:
            payload = tickets.ticket(court, data, key)
            if payload is None:
                continue
            boundaries = payload["segments"]["boundaries"]
            rows = {row["key"]: row for row in payload["segments"]["rows"]}
            detail[key] = {
                "in_review": (boundaries.get("in_review") or {}).get("ts"),
                "in_verification": (boundaries.get("in_verification") or {}).get("ts"),
                "merged": (boundaries.get("merged") or {}).get("ts"),
                "done": (boundaries.get("done") or {}).get("ts"),
                "verification_done_hours": rows.get("verification_done", {}).get("hours"),
                "state": payload["story"]["state"] if payload.get("story") else None,
            }
        return {
            "segments": {key: entry["hours"] for key, entry in metric["segments"].items()},
            "order": metric["order"],
            "tickets": detail,
        }
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bitbucket-store", default=str(REPO / "data" / "del3.duckdb"),
                    help="Bitbucket-only DuckDB store (the 131-PR DEL corpus)")
    ap.add_argument("--out", default=str(REPO / "data" / "del-combined.duckdb"))
    ap.add_argument("--identity", default=str(REPO / "data" / "identity.json"),
                    help="raw-zone identity map shared with the Bitbucket scrape")
    ap.add_argument("--bundle", default=str(HERE / "build" / "del-jira-workflow.tgz"))
    ap.add_argument("--seed", type=int, default=jira_fixtures.DEL_SEED)
    ap.add_argument("--check-only", action="store_true",
                    help="verify an existing --out store, write nothing")
    args = ap.parse_args()

    store = Path(args.out).expanduser().resolve()
    if args.check_only:
        if not store.exists():
            raise SystemExit(f"no store at {store}")
        log(json.dumps(_report(store), indent=1))
        return 0

    base = Path(args.bitbucket_store).expanduser().resolve()
    identity = Path(args.identity).expanduser().resolve()
    bundle_path = Path(args.bundle).expanduser().resolve()
    if not base.exists():
        raise SystemExit(f"no Bitbucket store at {base}")
    if not identity.exists():
        raise SystemExit(f"no identity map at {identity} (run the Bitbucket scrape first)")
    _require_bitbucket_only(base)

    configure(jira_fixtures.DEL_STAGE_MAP, identity, store)
    cfg = load_config()
    cfg.db_path = store
    log(f"bitbucket half  {base}")
    log(f"identity map    {identity}")
    log(f"stage map       {cfg.jira_status_map}")

    timeline = backdate.read_pr_timeline(base)
    if len(timeline) < 100:
        raise SystemExit(f"only {len(timeline)} ticket timelines in {base}; expected 100")
    bundle = jira_fixtures.generate_del(timeline, seed=args.seed)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_text(json.dumps(bundle, indent=1) + "\n")
    log(f"jira bundle     {bundle_path} ({len(bundle['issues'])} issues,"
        f" {len(bundle['sprints'])} sprints)")

    # The scrape runs through the production reducer with the stage map; the
    # state directory is throwaway (the identity map is the persistent one).
    tgz = bundle_path.with_suffix(".tgz")
    with tempfile.TemporaryDirectory(prefix="del-jira-state-") as state_dir:
        report = scrape(
            cfg,
            FixtureJiraTransport(bundle),
            "jira-fixture",
            out=tgz,
            state_dir=state_dir,
            full=True,
            identity_path=identity,
            progress=ProgressReporter(stream=io.StringIO(), interval=0),
        )
    if report["jira_status_map"] != dict(sorted(cfg.jira_status_map.items())):
        raise SystemExit("the scrape did not echo the stage map in its manifest")
    log(f"scraped         {report['issues_kept']} issues -> {report['counts']['events']}"
        f" events, {report['counts']['objects']} objects, {report['worklogs']} worklogs")
    log(f"transitions     {report['transitions']} status changes projected to stages")
    log(f"log delays      {report['log_delays']}")

    # Build on a copy so the live store is untouched until the new one passes
    # every check (prd/11's gate: verify, then commit).
    staged = store.with_name(store.stem + ".new" + store.suffix)
    staged.unlink(missing_ok=True)
    shutil.copy2(base, staged)
    cfg.db_path = staged
    imported = import_bundle(cfg, tgz)
    if imported.get("verified") is False:
        raise SystemExit("the import gate rejected the Jira bundle")

    con = connect(staged, read_only=True)
    try:
        contract = verify(con, cfg.key_shape, cfg.ticket_projects, cfg.ticket_keys)
        if not contract["ok"]:
            raise SystemExit("pseudonymisation contract violated:\n  - "
                             + "\n  - ".join(contract["problems"]))
        stages = _stage_counts(con)
        if stages.get("in_review", 0) == 0 or stages.get("in_verification", 0) == 0:
            raise SystemExit(f"the rebuilt store has no review/QA stages: {stages}")
        runs = con.execute("SELECT source, issues_kept FROM ingest_runs"
                           " ORDER BY source").fetchall()
        log(f"stages          {stages}")
        log(f"runs            {runs}")
    finally:
        con.close()

    detail = _report(staged)
    log("qa segments     "
        + ", ".join(f"{key}={hours:.1f}h" for key, hours in detail["segments"].items()))

    if store.exists():
        backup = HERE / "build" / "del-combined.before.duckdb"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(store, backup)
        log(f"backup          {backup}")
    os.replace(staged, store)
    log(f"live store      {store}")
    log(json.dumps(detail, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

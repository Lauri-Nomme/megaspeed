"""CLI entrypoint: ``python3 -m ingest``.

The ingest is split into the two sides of the airgap (prd/11 §7):

    # scrape side (next to the source): -> a tgz of pseudonymised rows
    python3 -m ingest scrape --live --repo DEL/delivery-api --out export/del.tgz

    # import side (this env): tgz -> DuckDB store
    python3 -m ingest import export/del.tgz --db data/del.duckdb

The older one-shot form is still supported and is the dev shortcut
(``--live`` = scrape + import back-to-back, through a real bundle file):

    python3 -m ingest --fixture --n 50
    python3 -m ingest --fixture --n 50 --write-fixtures   # (re)generate fixtures/
    python3 -m ingest --fixture-file fixtures/bitbucket-dc-fixture.json
    python3 -m ingest --verify
    python3 -m ingest --live            # requires MEGASPEED_BB_BASE_URL + token
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import fixtures as fixtures_mod
from .config import Config, REPO_ROOT, filter_scope, load_config
from .scrape import DEFAULT_OVERLAP_HOURS
from .transport import BitbucketError, FixtureTransport, HttpTransport


def _fixture_bundle(path: Path | None, n: int, seed: int) -> dict:
    if path and path.exists():
        return json.loads(path.read_text())
    return fixtures_mod.generate(n=n, seed=seed)


def _bundle_pr_count(bundle: dict) -> int:
    return sum(
        len(repo.get("pull_requests") or [])
        for project in bundle.get("projects", [])
        for repo in project.get("repos", [])
    )


def _print_probe(report: dict) -> None:
    print(f"probe          {report['as_of']} · {report['requests']} requests · read-only, nothing written")
    print(
        f"ticket filter  {report['ticket_regex']}   (shape {report['key_shape']}"
        f", {filter_scope(tuple(report['ticket_projects']), tuple(report.get('ticket_keys') or ()))})"
    )
    for repo in report["repos"]:
        states = ", ".join(f"{k}={v}" for k, v in sorted(repo["by_state"].items()))
        actions = ", ".join(f"{k}={v}" for k, v in sorted(repo["actions"].items()))
        print(f"\nrepo           {repo['repo']}: {repo['prs']} PRs ({states}), {repo['commits']} commits")
        print(f"activities     {actions}")
        if repo["ignored_actions"]:
            ignored = ", ".join(f"{k}={v}" for k, v in sorted(repo["ignored_actions"].items()))
            print(f"ignored        {ignored}  (outside the closed vocabulary)")
        print(f"filter         {len(repo['matched_prs'])} matched {repo['keys'] or ''}"
              f" · {len(repo['unmatched_prs'])} dropped")
        if repo["unmatched_prs"]:
            print(f"               no ticket key on PRs {repo['unmatched_prs']}")
    if report["bypass_filter"]:
        print(f"\ndry run        {report['dry_run_events']} events in memory (filter bypassed for mechanics only)")
        print(f"by activity    {report['dry_run_event_activities']}")
        print(f"pseudonyms     {', '.join(report['dry_run_pseudonyms'])}")
        for row in report["dry_run_sample"]:
            print(f"  {row['ts']}  {row['activity']:16} {row['actor']:5} {row['attributes']}")
    else:
        print("\nhint           --probe-bypass-filter to map the reducer over this corpus in memory")


def _legacy_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python3 -m ingest", description="Bitbucket DC -> pseudonymised store")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--fixture", action="store_true", help="ingest synthetic data (default)")
    mode.add_argument("--fixture-file", metavar="PATH", help="ingest a committed fixture bundle")
    mode.add_argument("--live", action="store_true",
                      help="scrape a real Bitbucket DC + import it (dev shortcut for scrape+import)")
    mode.add_argument("--import", dest="import_bundle", metavar="PATH.tgz",
                      help="import a scrape bundle into the store (same as the 'import' subcommand)")
    mode.add_argument("--verify", action="store_true", help="check the pseudonymisation contract of an existing store")
    mode.add_argument("--write-fixtures", action="store_true", help="write the synthetic bundle to fixtures/ and exit")
    ap.add_argument("--jira", action="store_true",
                    help="ingest Jira (Epic/Story/Sprint + worklogs) instead of Bitbucket")
    ap.add_argument("--jira-cloud", action="store_true",
                    help="ingest Jira **Cloud** (v3 + Bearer PAT, prd/24) instead of DC;"
                         " implies --jira and reads MEGASPEED_JIRA_CLOUD_BASE_URL"
                         " + MEGASPEED_JIRA_CLOUD_TOKEN, or MEGASPEED_JIRA_CLOUD_USER"
                         " + MEGASPEED_JIRA_CLOUD_API_TOKEN (Basic)")
    ap.add_argument("--project", action="append", default=None, metavar="KEY",
                    help="--jira: override the configured Jira projects (repeatable)")
    ap.add_argument("--team", action="append", default=None, metavar="NAME",
                    help="--jira: scrape only the issues carrying one of these team values"
                         " (repeatable; prd/66). See `python3 -m ingest.jira_fields`")
    ap.add_argument("--team-field", default=None, metavar="NAME|ID",
                    help="--jira: the team field's name or id (default: discover 'Team')")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="--jira: read at most N issues")
    ap.add_argument("--n", type=int, default=None,
                    help="fixture PRs to generate (default 50); with --fixture-file, cap how many "
                         "PRs are read (default: all of them)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--db", default=None, help="override store path")
    ap.add_argument("--config", default=None)
    ap.add_argument("--repo", action="append", default=None, metavar="PROJECT/REPO",
                    help="override configured repos (repeatable)")
    ap.add_argument("--probe", action="store_true",
                    help="live mode: read-only diagnostics, no store writes")
    ap.add_argument("--probe-bypass-filter", action="store_true",
                    help="probe only: run the reducer in memory over unfiltered PRs")
    ap.add_argument("--out", default=str(REPO_ROOT / "fixtures" / "bitbucket-dc-fixture.json"))
    # Dev-shortcut controls: keep the bundle / its state around instead of
    # writing them to a throwaway temp dir.
    ap.add_argument("--bundle", default=None, metavar="PATH.tgz",
                    help="--live/--fixture: also keep the transfer bundle at this path")
    ap.add_argument("--state-dir", default=None, metavar="DIR",
                    help="--live/--fixture: watermark + identity state (enables incremental scrapes)")
    ap.add_argument("--overlap-hours", type=int, default=DEFAULT_OVERLAP_HOURS, metavar="N",
                    help=f"re-emit this far before the watermark (default {DEFAULT_OVERLAP_HOURS})")
    ap.add_argument("--full", action="store_true", help="ignore the watermark and re-scrape everything")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def main(argv: list[str] | None = None) -> int:
    # Windows consoles/pipes: the reports below use '·', '—', '≥', '…'.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    # Two halves, two CLIs: `python3 -m ingest scrape|import ...`. The scrape
    # side is also its own artifact (scrape/build.sh -> megaspeed-scrape).
    if argv and argv[0] in ("scrape", "import"):
        command, rest = argv[0], argv[1:]
        if command == "scrape":
            from .scrape_cli import main as scrape_main

            return scrape_main(rest)
        from .importer import main as import_main

        return import_main(rest)

    ap = _legacy_parser()
    args = ap.parse_args(argv)

    if args.n is not None and args.n < 1:
        print(f"--n must be at least 1 (got {args.n}): an empty ingest is not a green run", file=sys.stderr)
        return 2

    if args.jira_cloud:
        # prd/24: one source, two shapes. Promote it here exactly like the
        # scrape CLI, so every --jira flag keeps its meaning on this shortcut.
        args.jira = True

    cfg: Config = load_config(args.config)
    if args.db:
        cfg.db_path = Path(args.db).expanduser().resolve()
    # prd/66: one-run team scope override (the env/config value still wins when
    # no flag is given).
    if getattr(args, "team", None):
        from .config import parse_teams

        try:
            cfg.jira_teams = parse_teams(args.team)
        except SystemExit as exc:
            print(str(exc.code) if exc.code not in (0, None) else "bad --team", file=sys.stderr)
            return 2
    if getattr(args, "team_field", None):
        cfg.jira_team_field = str(args.team_field).strip()
    if args.repo:
        repos = []
        for item in args.repo:
            project, _, repo = item.partition("/")
            if not project or not repo:
                print(f"bad --repo {item!r}, expected PROJECT/REPO", file=sys.stderr)
                return 2
            repos.append((project, repo))
        cfg.repos = repos
    seed = args.seed if args.seed is not None else cfg.seed
    generate_n = args.n if args.n is not None else 50

    if args.write_fixtures:
        path = fixtures_mod.write_bundle(Path(args.out), n=generate_n, seed=seed)
        print(path)
        return 0

    if args.verify:
        # Import side: DuckDB is needed here, not on the scrape side, so the
        # scrape subcommand above must stay importable without it.
        from .store import connect, imported_scope, verify

        if not cfg.db_path.exists():
            print(f"no store at {cfg.db_path} — run: python3 -m ingest --fixture --n 50", file=sys.stderr)
            return 2
        con = connect(cfg.db_path, read_only=True)
        try:
            # prd/27: an epic-scoped (or key-scoped) store carries its scope in
            # ingest_runs, so --verify enforces the same set the import did even
            # though the operator only configured MEGASPEED_JIRA_EPICS.
            keys = cfg.ticket_keys or imported_scope(con)
            report = verify(con, cfg.key_shape, cfg.ticket_projects, keys)
        finally:
            con.close()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_verify(report, cfg.db_path)
        return 0 if report["ok"] else 1

    if args.import_bundle:
        from .importer import import_bundle as do_import
        from .importer import _print_import
        from .bundle import BundleError
        from .importer import ImportRejected

        try:
            imported = do_import(cfg, args.import_bundle)
        except (BundleError, ImportRejected) as exc:
            print(f"bundle rejected: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(imported, indent=2, default=str))
        else:
            _print_import(imported)
        return 0 if imported["verified"] is not False else 1

    if args.probe and not args.live:
        print("--probe only makes sense with --live", file=sys.stderr)
        return 2

    if args.jira:
        return _run_jira(cfg, args)

    if not cfg.repos:
        print(
            "no repos configured: set MEGASPEED_REPOS=PROJECT/REPO,PROJECT/REPO"
            " (or --repo) for the Bitbucket ingest",
            file=sys.stderr,
        )
        return 2

    limit = None
    try:
        if args.live:
            if not cfg.base_url:
                print("live mode needs MEGASPEED_BB_BASE_URL (and MEGASPEED_BB_TOKEN)", file=sys.stderr)
                return 2
            transport = HttpTransport(
                cfg.base_url,
                token=cfg.auth_token,
                user=cfg.auth_user,
                password=cfg.auth_password,
            )
            transport.check()
            source = "bitbucket-dc"
            if args.probe:
                from .bitbucket import BitbucketClient
                from .probe import probe as run_probe

                client = BitbucketClient(transport, page_limit=cfg.page_limit, min_interval=0.05)
                report = run_probe(cfg, client, bypass_filter=args.probe_bypass_filter)
                if args.json:
                    print(json.dumps(report, indent=2, default=str))
                else:
                    _print_probe(report)
                return 0
        else:
            bundle = _fixture_bundle(Path(args.fixture_file) if args.fixture_file else cfg.fixture_path,
                                     generate_n, seed)
            transport = FixtureTransport(bundle)
            source = "fixture"
            # A replay of a bundle is bounded by the bundle, not by the generator
            # default -- capping a 60-PR file at 50 silently dropped 10 PRs, and
            # the limit used to fire after the counter incremented (51 seen, 50
            # read).
            if args.fixture_file and args.n is not None:
                available = _bundle_pr_count(bundle)
                if args.n > available:
                    print(
                        f"warning: --n {args.n} exceeds the {available} PR(s) in {args.fixture_file};"
                        f" reading all {available}",
                        file=sys.stderr,
                    )
                limit = args.n

        # The one-shot dev shortcut only: scrape + import in one process, which
        # is why the import-side dependency is loaded here and nowhere else.
        from .live import run

        report = run(
            cfg,
            transport,
            source=source,
            limit=limit,
            out=args.bundle,
            state_dir=args.state_dir,
            overlap_hours=args.overlap_hours,
            full=args.full,
        )
    except BitbucketError as exc:
        # The likely operator mistakes (typo'd repo, bad password, DNS/port, a
        # bundle whose repos differ from config) deserve a message, not a stack.
        print(f"bitbucket error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_run(report)
    if not report["verified"]:
        print("\nPSEUDONYMISATION CONTRACT VIOLATED:", file=sys.stderr)
        for problem in report["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


def _run_jira(cfg: Config, args) -> int:
    """``python3 -m ingest --jira ...``: the Jira dev shortcut (scrape + import)."""
    from .jira import JiraError
    from .live import run_jira
    from .scrape_cli import _jira_transport

    if args.probe:
        from .scrape_cli import _jira_probe

        try:
            # the helper returns (transport, source, limit, client): the client
            # is the Cloud one in --jira-cloud mode, and the probe must use it
            # (a DC client would talk the wrong API shape to a Cloud site).
            transport, _source, _limit, client = _jira_transport(cfg, args)
        except SystemExit as exc:
            print(str(exc.code) if exc.code not in (0, None) else "bad configuration", file=sys.stderr)
            return 2
        return _jira_probe(
            cfg, transport, args.json, client=client,
            projects=tuple(args.project) if args.project else None,
        )

    projects = None
    if args.project:
        projects = tuple(str(p).strip().upper() for p in args.project if str(p).strip())
    try:
        transport, source, limit, _client = _jira_transport(cfg, args)
        report = run_jira(
            cfg,
            transport,
            source,
            limit if limit is not None else args.limit,
            out=args.bundle,
            state_dir=args.state_dir,
            overlap_hours=args.overlap_hours,
            full=args.full,
            projects=projects,
        )
    except (JiraError, BitbucketError) as exc:
        print(f"jira error: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        print(str(exc.code) if exc.code not in (0, None) else "bad configuration", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"source         {report['source']} (run {report['run_id']})")
        print(f"bundle         {report['bundle']}  ({report['bundle_bytes']} bytes)")
        print(f"store          {report['db']}")
        print(f"identity map   {report['identity_map']}  (raw zone, never served)")
        print(f"projects       {', '.join(report['projects'])}")
        print(
            f"issues         {report['issues_seen']} seen -> {report['issues_kept']} kept,"
            f" {report['issues_skipped']} skipped"
        )
        print(
            f"events         {report['events_emitted']} emitted, {report['events_new']} new,"
            f" {report['events_rewritten']} rewritten"
        )
        counts = report["counts"]
        print(
            f"objects        {report['objects']}  (epics={counts['epics']},"
            f" stories={counts['stories']}, sprints={counts['sprints']})"
        )
        print(f"worklogs       {report['worklogs']}  ({report['log_delays']})")
        print(f"requests       {report['requests']}")
        s = report["summary"]
        print(
            f"store now      {s['events']} events, {s.get('epics', 0)} epics,"
            f" {s.get('stories', 0)} stories, {s.get('sprints', 0)} sprints, as_of {s['as_of']}"
        )
        print(f"contract       {'OK — no free text, no canaries' if report['verified'] else 'VIOLATED'}")
    if not report["verified"]:
        print("\nPSEUDONYMISATION CONTRACT VIOLATED:", file=sys.stderr)
        for problem in report["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


def _print_run(r: dict) -> None:
    print(f"source         {r['source']} (run {r['run_id']})")
    print(f"bundle         {r['bundle']}  ({r['bundle_bytes']} bytes)")
    print(f"store          {r['db']}")
    print(f"identity map   {r['identity_map']}  (raw zone, never served)")
    scope = filter_scope(tuple(r["ticket_projects"]), tuple(r.get("ticket_keys") or ()))
    print(f"ticket filter  {r['ticket_regex']}   ({scope})")
    print(
        f"PRs            {r['prs_seen']} seen -> {r['prs_kept']} kept, {r['prs_dropped']} dropped "
        f"(no ticket key)"
    )
    print(
        f"events         {r['events_emitted']} emitted, {r['events_new']} new, "
        f"{r['events_rewritten']} rewritten, {r['events_derived']} re-derived"
    )
    print(f"objects        {r['objects']}")
    print(f"requests       {r['requests']}")
    placements = ", ".join(f"{k}={v}" for k, v in r["keys_by_placement"].items())
    print(f"key found in   {placements}")
    s = r["summary"]
    print(f"store now      {s['events']} events, {s['prs']} PRs, {s['commits']} commits, as_of {s['as_of']}")
    print(f"contract       {'OK — no free text, no canaries' if r['verified'] else 'VIOLATED'}")


def _print_verify(report: dict, db: Path) -> None:
    s = report["summary"]
    print(f"store          {db}")
    print(f"events         {s['events']}   PRs {s['prs']}   commits {s['commits']}   as_of {s['as_of']}")
    keys = report.get("ticket_keys") or []
    if keys:
        shown = ", ".join(keys[:4]) + (" …" if len(keys) > 4 else "")
        print(f"scope          {len(keys)} explicit ticket key(s): {shown}")
    print("tables:")
    for table, columns in report["tables"].items():
        print(f"  {table:14} {', '.join(columns)}")
    if report["ok"]:
        print(
            "contract       OK — columns allowlisted, identifier shapes checked, "
            "closed vocabularies hold, no canaries"
        )
    else:
        print("contract       VIOLATED")
        for problem in report["problems"]:
            print(f"  - {problem}")


if __name__ == "__main__":
    raise SystemExit(main())

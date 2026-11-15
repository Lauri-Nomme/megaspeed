"""CLI entrypoint: ``python3 -m ingest``.

Fixture mode is the default and needs no credentials:

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
from .config import Config, REPO_ROOT, load_config
from .pipeline import run
from .store import connect, verify
from .transport import FixtureTransport, HttpTransport


def _fixture_bundle(path: Path | None, n: int, seed: int) -> dict:
    if path and path.exists():
        return json.loads(path.read_text())
    return fixtures_mod.generate(n=n, seed=seed)


def _print_probe(report: dict) -> None:
    print(f"probe          {report['as_of']} · {report['requests']} requests · read-only, nothing written")
    print(f"ticket filter  {report['ticket_regex']}   (shape {report['key_shape']})")
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m ingest", description="Bitbucket DC -> pseudonymised store")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--fixture", action="store_true", help="ingest synthetic data (default)")
    mode.add_argument("--fixture-file", metavar="PATH", help="ingest a committed fixture bundle")
    mode.add_argument("--live", action="store_true", help="call the real Bitbucket DC REST API")
    mode.add_argument("--verify", action="store_true", help="check the pseudonymisation contract of an existing store")
    mode.add_argument("--write-fixtures", action="store_true", help="write the synthetic bundle to fixtures/ and exit")
    ap.add_argument("--n", type=int, default=50, help="fixture PRs to generate (default 50)")
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
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    cfg: Config = load_config(args.config)
    if args.db:
        cfg.db_path = Path(args.db).expanduser().resolve()
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

    if args.write_fixtures:
        path = fixtures_mod.write_bundle(Path(args.out), n=args.n, seed=seed)
        print(path)
        return 0

    if args.verify:
        if not cfg.db_path.exists():
            print(f"no store at {cfg.db_path} — run: python3 -m ingest --fixture --n 50", file=sys.stderr)
            return 2
        con = connect(cfg.db_path, read_only=True)
        try:
            report = verify(con)
        finally:
            con.close()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_verify(report, cfg.db_path)
        return 0 if report["ok"] else 1

    if args.probe and not args.live:
        print("--probe only makes sense with --live", file=sys.stderr)
        return 2

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
        bundle = _fixture_bundle(Path(args.fixture_file) if args.fixture_file else cfg.fixture_path, args.n, seed)
        transport = FixtureTransport(bundle)
        source = "fixture"

    report = run(cfg, transport, source=source, limit=args.n if source == "fixture" else None)
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


def _print_run(r: dict) -> None:
    print(f"source         {r['source']} (run {r['run_id']})")
    print(f"store          {r['db']}")
    print(f"identity map   {r['identity_map']}  (raw zone, never served)")
    print(f"ticket filter  {r['ticket_regex']}")
    print(
        f"PRs            {r['prs_seen']} seen -> {r['prs_kept']} kept, {r['prs_dropped']} dropped "
        f"(no ticket key)"
    )
    print(f"events         {r['events_emitted']} emitted, {r['events_new']} new (idempotent by natural key)")
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
    print("tables:")
    for table, columns in report["tables"].items():
        print(f"  {table:14} {', '.join(columns)}")
    if report["ok"]:
        print("contract       OK — columns allowlisted, no whitespace-bearing strings, no canaries")
    else:
        print("contract       VIOLATED")
        for problem in report["problems"]:
            print(f"  - {problem}")


if __name__ == "__main__":
    raise SystemExit(main())

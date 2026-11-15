"""``megaspeed-scrape`` — the scrape-side CLI (airgapped, next to the source).

This module is the entry point of the self-contained artifact built by
``scrape/build.sh``. It imports **nothing that needs DuckDB**: the scrape side is
source -> classify -> pseudonymise -> tgz (prd/11 §7, prd/13).

```
megaspeed-scrape --live --repo DEL/delivery-api --repo DEL/delivery-web \\
                 --out export/del.tgz --state-dir export/del.state

# no source credentials needed: replay/produce a synthetic bundle
megaspeed-scrape --fixture --n 50 --out /tmp/fixture.tgz
```

Credentials are **never baked into the artifact**: they come from the
environment (``MEGASPEED_BB_BASE_URL`` + ``MEGASPEED_BB_TOKEN`` or
``MEGASPEED_BB_USER``/``MEGASPEED_BB_PASSWORD``), and ticket projects / bot
slugs / repos from the environment or a runtime ``--config`` TOML file.

Also available as ``python3 -m ingest scrape ...``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import REPO_ROOT, Config, load_config
from .progress import DEFAULT_INTERVAL as DEFAULT_PROGRESS_INTERVAL
from .progress import ProgressReporter
from .scrape import DEFAULT_OVERLAP_HOURS, default_state_dir, scrape
from .transport import BitbucketError, FixtureTransport, HttpTransport

DEFAULT_OUT = "export/megaspeed-scrape.tgz"


def _probe(cfg: Config, as_json: bool, bypass_filter: bool) -> int:
    from .bitbucket import BitbucketClient
    from .probe import probe as run_probe

    transport = HttpTransport(
        cfg.base_url, token=cfg.auth_token, user=cfg.auth_user, password=cfg.auth_password
    )
    transport.check()
    client = BitbucketClient(transport, page_limit=cfg.page_limit, min_interval=0.05)
    report = run_probe(cfg, client, bypass_filter=bypass_filter)
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    print(f"probe          {report['as_of']} · {report['requests']} requests · read-only, nothing written")
    for repo in report["repos"]:
        states = ", ".join(f"{k}={v}" for k, v in sorted(repo["by_state"].items()))
        print(f"repo           {repo['repo']}: {repo['prs']} PRs ({states}), {repo['commits']} commits")
        print(f"filter         {len(repo['matched_prs'])} matched · {len(repo['unmatched_prs'])} dropped")
    return 0


def _print_scrape(r: dict) -> None:
    print(f"source         {r['source']} (run {r['run_id']})")
    print(f"bundle         {r['bundle']}  ({r['bundle_bytes']} bytes)")
    print(f"sha256         {r['bundle_sha256']}")
    print(f"state          {r['state_dir']}  (watermark + prs + identity, raw zone, never transferred)")
    print(f"identity map   {r['identity_map']}")
    print(f"ticket filter  {r['ticket_regex']}   (projects {','.join(r['ticket_projects'])})")
    print(
        f"PRs            {r['prs_seen']} seen -> {r['prs_kept']} kept, {r['prs_dropped']} dropped "
        f"(no ticket key)"
    )
    print(
        f"settled        {r['prs_skipped_settled']} skipped (closed, unchanged)"
        f" · {r['prs_settled']} marked done · {r['prs_settled_total']} in prs.json"
    )
    print(f"unchanged      {r['prs_skipped_watermark']} skipped (open/recent, at the watermark)")
    print(
        f"events         {r['events']} emitted"
        + (f" (of {r['events_collected']} collected; watermark overlap)" if r["events"] != r["events_collected"] else "")
    )
    print(f"objects        {r['objects']}")
    by_kind = ", ".join(f"{k}={v}" for k, v in r["requests_by_kind"].items())
    print(f"requests       {r['requests']}" + (f"  ({by_kind})" if by_kind else ""))
    print(f"watermark      {r['previous_watermark'] or '—'} -> {r['watermark'] or '—'}"
          f"   (overlap {r['overlap_hours']}h{', full' if r['full'] else ''})")
    cursors = ", ".join(f"{k}={v or '—'}" for k, v in r["repo_watermarks"].items())
    print(f"repo cursors   {cursors}")
    if r["early_stopped"]:
        print(f"early stop     {', '.join(r['early_stopped'])} (full page at/below the watermark)")
    placements = ", ".join(f"{k}={v}" for k, v in r["keys_by_placement"].items())
    print(f"key found in   {placements}")


def build_parser(prog: str = "megaspeed-scrape") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Scrape a source, classify + pseudonymise it, and write a transfer bundle (.tgz).",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="call the real Bitbucket DC REST API")
    mode.add_argument("--fixture", action="store_true", help="scrape synthetic data (no credentials)")
    mode.add_argument("--fixture-file", metavar="PATH", help="scrape a committed fixture bundle")
    ap.add_argument("--out", default=DEFAULT_OUT, metavar="PATH.tgz",
                    help=f"bundle to write (default {DEFAULT_OUT})")
    ap.add_argument("--state-dir", default=None, metavar="DIR",
                    help="watermark cursor + identity map (default: next to --out, <out>.state/)")
    ap.add_argument("--identity", default=None, metavar="PATH",
                    help="raw-zone identity map to use instead of <state-dir>/identity.json")
    ap.add_argument("--overlap-hours", type=int, default=DEFAULT_OVERLAP_HOURS, metavar="N",
                    help=f"re-emit this far before the watermark (default {DEFAULT_OVERLAP_HOURS})")
    ap.add_argument("--full", action="store_true", help="ignore the watermark and re-scrape everything")
    ap.add_argument("--no-prefilter", action="store_true",
                    help="fetch every PR's commits before filtering (old, slower path; prd/14)")
    ap.add_argument("--progress-interval", type=float, default=None, metavar="SECONDS",
                    help="progress line on stderr every N seconds; 0 disables"
                         f" (default {DEFAULT_PROGRESS_INTERVAL:g})")
    ap.add_argument("--quiet", action="store_true",
                    help="no progress lines on stderr (the final summary still prints)")
    ap.add_argument("--n", type=int, default=None,
                    help="fixture PRs to generate (default 50); with --fixture-file, cap how many are read")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--config", default=None, help="runtime config TOML (non-secret)")
    ap.add_argument("--repo", action="append", default=None, metavar="PROJECT/REPO",
                    help="override configured repos (repeatable)")
    ap.add_argument("--probe", action="store_true",
                    help="--live only: read-only diagnostics, write no bundle")
    ap.add_argument("--probe-bypass-filter", action="store_true", help="probe only: map the reducer in memory")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="version", version=f"megaspeed-scrape {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if not (args.live or args.fixture or args.fixture_file or args.probe):
        # No default: a scraper that silently generated synthetic data would be
        # a nasty surprise in an airgapped run.
        ap.error("choose a source: --live, --fixture or --fixture-file PATH")
    if args.n is not None and args.n < 1:
        print(f"--n must be at least 1 (got {args.n})", file=sys.stderr)
        return 2
    if args.overlap_hours < 0:
        print("--overlap-hours must be >= 0", file=sys.stderr)
        return 2
    if args.progress_interval is not None and args.progress_interval < 0:
        print("--progress-interval must be >= 0 (0 disables)", file=sys.stderr)
        return 2

    try:
        cfg = load_config(args.config)
    except SystemExit as exc:  # config.py reports operator errors this way
        print(str(exc.code) if exc.code not in (0, None) else "bad configuration", file=sys.stderr)
        return 2
    if args.repo:
        repos = []
        for item in args.repo:
            project, _, repo = item.partition("/")
            if not project or not repo:
                print(f"bad --repo {item!r}, expected PROJECT/REPO", file=sys.stderr)
                return 2
            repos.append((project, repo))
        cfg.repos = repos
    if args.no_prefilter:
        cfg.prefilter = False

    seed = args.seed if args.seed is not None else cfg.seed
    generate_n = args.n if args.n is not None else 50
    out = Path(args.out).expanduser()
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir(out)

    # Progress goes to stderr only, so a piped --json report stays machine
    # readable (prd/14 "Progress reporting").
    interval = 0.0 if args.quiet else (
        args.progress_interval
        if args.progress_interval is not None
        else cfg.progress_interval
    )
    progress = ProgressReporter(interval=interval)

    try:
        if args.live or args.probe:
            if not cfg.base_url:
                print("live mode needs MEGASPEED_BB_BASE_URL (and MEGASPEED_BB_TOKEN)", file=sys.stderr)
                return 2
            if args.probe:
                return _probe(cfg, args.json, args.probe_bypass_filter)
            transport = HttpTransport(
                cfg.base_url, token=cfg.auth_token, user=cfg.auth_user, password=cfg.auth_password
            )
            transport.check()
            source = "bitbucket-dc"
        else:
            from . import fixtures as fixtures_mod

            path = Path(args.fixture_file).expanduser() if args.fixture_file else cfg.fixture_path
            bundle = json.loads(path.read_text()) if path and path.exists() else fixtures_mod.generate(
                n=generate_n, seed=seed
            )
            transport = FixtureTransport(bundle)
            source = "fixture"
            if args.fixture_file and args.n is not None:
                available = sum(
                    len(repo.get("pull_requests") or [])
                    for project in bundle.get("projects", [])
                    for repo in project.get("repos", [])
                )
                if args.n > available:
                    print(
                        f"warning: --n {args.n} exceeds the {available} PR(s) in {args.fixture_file};"
                        f" reading all {available}",
                        file=sys.stderr,
                    )

        report = scrape(
            cfg,
            transport,
            source,
            out=out,
            state_dir=state_dir,
            limit=args.n if (args.fixture_file and args.n is not None) else None,
            overlap_hours=args.overlap_hours,
            full=args.full,
            identity_path=args.identity,
            progress=progress,
        )
    except BitbucketError as exc:
        print(f"bitbucket error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_scrape(report)
        print(f"\nnext           python3 -m ingest import {report['bundle']}")
    return 0


__all__ = ["DEFAULT_OUT", "build_parser", "main", "REPO_ROOT"]

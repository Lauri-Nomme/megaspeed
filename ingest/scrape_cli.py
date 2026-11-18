"""``megaspeed-scrape`` — the scrape-side CLI (airgapped, next to the source).

This module is the entry point of the self-contained artifact built by
``scrape/build.sh``. It imports **nothing that needs DuckDB**: the scrape side is
source -> classify -> pseudonymise -> tgz (prd/11 §7, prd/13).

```
megaspeed-scrape --live --repo DEL/delivery-api --repo DEL/delivery-web \\
                 --out export/del.tgz --state-dir export/del.state

# the Jira half: the ticket layer (Epic/Story/Sprint + worklogs)
megaspeed-scrape --jira --live --project DEL \\
                 --out export/del-jira.tgz --state-dir export/del.state

# no source credentials needed: replay/produce a synthetic bundle
megaspeed-scrape --fixture --n 50 --out /tmp/fixture.tgz
megaspeed-scrape --jira --fixture --out /tmp/jira-fixture.tgz
```

Credentials are **never baked into the artifact**: they come from the
environment (``MEGASPEED_BB_BASE_URL`` + ``MEGASPEED_BB_TOKEN`` or
``MEGASPEED_BB_USER``/``MEGASPEED_BB_PASSWORD`` for Bitbucket;
``MEGASPEED_JIRA_BASE_URL`` + ``MEGASPEED_JIRA_USER``/``MEGASPEED_JIRA_PASSWORD``
or ``MEGASPEED_JIRA_TOKEN`` for Jira), and ticket keys/projects / bot slugs /
repos from the environment or a runtime ``--config`` TOML file.

Also available as ``python3 -m ingest scrape ...``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import __version__, classify
from .config import REPO_ROOT, Config, filter_scope, load_config
from .progress import DEFAULT_INTERVAL as DEFAULT_PROGRESS_INTERVAL
from .progress import ProgressReporter
from .scrape import (
    DEFAULT_CHECKPOINT_EVERY,
    DEFAULT_OVERLAP_HOURS,
    default_state_dir,
    scrape,
)
from .transport import BitbucketError, FixtureTransport, HttpTransport

DEFAULT_OUT = "export/megaspeed-scrape.tgz"
DEFAULT_JIRA_OUT = "export/megaspeed-jira.tgz"


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


def _tempo(cfg: Config, args) -> bool:
    """prd/30: ``--tempo`` overrides, else ``MEGASPEED_JIRA_TEMPO`` / ``[jira] tempo``."""
    return bool(getattr(args, "tempo", False) or cfg.jira_tempo)


def _jira_transport(cfg: Config, args) -> tuple[object, str, int | None, object | None]:
    """Build the Jira transport + source name (+ the Cloud client) for the mode."""
    from .jira_fixtures import generate
    from .transport import FixtureJiraTransport

    tempo = _tempo(cfg, args)
    if args.live or args.probe:
        if args.jira_cloud:
            # prd/24: the Cloud shape -- v3, Bearer PAT, cursor search, Cloud
            # field ids. Point the base URL at a real site in production, or at
            # the local emulation proxy (proxy/cloud_jira_proxy.py) in dev.
            if not cfg.jira_cloud_base_url:
                raise SystemExit(
                    "jira cloud mode needs MEGASPEED_JIRA_CLOUD_BASE_URL (and"
                    " MEGASPEED_JIRA_CLOUD_TOKEN); in dev that is the proxy URL"
                )
            from .jira_cloud import CloudJiraClient

            transport = HttpTransport(cfg.jira_cloud_base_url, token=cfg.jira_cloud_token)
            client = CloudJiraClient(transport, page_limit=cfg.jira_page_limit, tempo=tempo)
            client.check()
            return transport, "jira-cloud", args.limit, client
        if not cfg.jira_base_url:
            raise SystemExit(
                "jira live mode needs MEGASPEED_JIRA_BASE_URL (and"
                " MEGASPEED_JIRA_USER/MEGASPEED_JIRA_PASSWORD or MEGASPEED_JIRA_TOKEN)"
            )
        from .jira import JiraClient

        transport = HttpTransport(
            cfg.jira_base_url,
            token=cfg.jira_auth_token,
            user=cfg.jira_auth_user,
            password=cfg.jira_auth_password,
        )
        client = JiraClient(
            transport,
            page_limit=cfg.jira_page_limit,
            # a probe is a handful of reads: keep its old 0.05 s cadence.
            min_interval=0.05 if getattr(args, "probe", False) else cfg.jira_min_interval,
            tempo=tempo,
        )
        client.check()
        return transport, "jira-dc", args.limit, client
    if args.jira_cloud:
        # Cloud has no local install and no fixture corpus of its own: the
        # emulation proxy (proxy/cloud_jira_proxy.py) *is* the local Cloud, and
        # it is reached over HTTP like the real thing.
        raise SystemExit(
            "jira cloud mode needs --live: point MEGASPEED_JIRA_CLOUD_BASE_URL at a"
            " real Cloud site or at the local emulation proxy"
            " (python3 -m proxy.cloud_jira_proxy)"
        )
    path = Path(args.fixture_file).expanduser() if args.fixture_file else cfg.fixture_path
    bundle = (
        json.loads(path.read_text())
        if path and path.exists() and args.fixture_file
        else generate(n=args.n, seed=args.seed if args.seed is not None else cfg.seed)
    )
    # FR-8: --limit means the same thing in every mode. The fixture path used to
    # return None, so `--jira --fixture --limit 3` read all 13 issues.
    if tempo:
        # prd/30: the lab cannot run Tempo, so the fixture serves its shape over
        # the same worklog records; `--jira --fixture --tempo` exercises it.
        from .jira import JiraClient
        from .jira_fixtures import TempoFixtureTransport

        transport = TempoFixtureTransport(bundle)
        client = JiraClient(
            transport, page_limit=cfg.jira_page_limit, min_interval=0.0, tempo=True
        )
        return transport, "jira-fixture", args.limit, client
    return FixtureJiraTransport(bundle), "jira-fixture", args.limit, None


def _issue_type_name(row: dict) -> str:
    """The issue-type *name* out of a search row (Jira returns an object)."""
    value = (row.get("fields") or {}).get("issuetype")
    if isinstance(value, dict):
        return str(value.get("name") or "?")
    return str(value or "?")


def _jira_probe(cfg: Config, transport, as_json: bool, client=None) -> int:
    """Read-only Jira diagnostics: version, board, sprints, issue counts."""
    from .jira import FIELD_EPIC_LINK, FIELD_SPRINT, FIELD_STORY_POINTS, JiraClient
    from .scrape_jira import ISSUE_TYPES, jql_for

    client = client or JiraClient(transport, page_limit=cfg.jira_page_limit, min_interval=0.05)
    version = (client.check() or {}).get("version")
    fields = client.field_ids()
    projects = tuple(cfg.jira_projects or cfg.ticket_projects)
    report: dict = {
        "version": version,
        "projects": list(projects),
        "requests": client.requests,
        "fields": {
            "epic_link": cfg.jira_epic_link_field or fields.get(FIELD_EPIC_LINK),
            "sprint": cfg.jira_sprint_field or fields.get(FIELD_SPRINT),
            "story_points": cfg.jira_story_points_field or fields.get(FIELD_STORY_POINTS),
        },
        "boards": {},
        "sprints": {},
        "issues": {},
    }
    for project in projects:
        board = cfg.jira_board or client.scrum_board(project)
        report["boards"][project] = board
        if board:
            sprints = client.sprints(board)
            report["sprints"][project] = [
                {"id": int(s["id"]), "state": s.get("state"), "name": s.get("name")}
                for s in sprints
            ]
        rows = list(client.search(jql_for(project), fields="key,updated,issuetype"))
        types = Counter(_issue_type_name(row) for row in rows)
        report["issues"][project] = {"total": len(rows), "types": dict(sorted(types.items()))}
    report["requests"] = client.requests
    report["requests_by_kind"] = dict(sorted(client.requests_by_kind.items()))
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return 0
    print(f"probe          jira {report['version']} · {report['requests']} requests · read-only")
    print(f"projects       {', '.join(report['projects'])}  (issue types: {', '.join(ISSUE_TYPES)})")
    for name, field_id in report["fields"].items():
        print(f"field          {name:14} {field_id or '— (not discovered)'}")
    for project, board in report["boards"].items():
        print(f"board          {project}: {board}")
        for sprint in report["sprints"].get(project, []):
            print(f"  sprint {sprint['id']:>4}  {sprint['state']:<7} {sprint['name']}")
        issues = report["issues"].get(project, {})
        types = ", ".join(f"{k}={v}" for k, v in (issues.get("types") or {}).items())
        print(f"issues         {project}: {issues.get('total', 0)} ({types})")
    return 0


def _print_jira_scrape(r: dict) -> None:
    print(f"source         {r['source']} (run {r['run_id']})")
    print(f"bundle         {r['bundle']}  ({r['bundle_bytes']} bytes)")
    print(f"sha256         {r['bundle_sha256']}")
    print(f"state          {r['state_dir']}  (watermark + issues + identity, raw zone, never transferred)")
    print(f"identity map   {r['identity_map']}")
    print(f"projects       {', '.join(r['projects'])}")
    epics = tuple(r.get("jira_epics") or ())
    keys = tuple(r.get("ticket_keys") or ())
    scope = (
        # prd/27: the epic set resolved to epics + children; listing 100 keys
        # would bury the line, so name the epics and count the union.
        f"epics {','.join(epics)} -> {len(keys)} key(s)"
        if epics
        else filter_scope(tuple(r['ticket_projects']), keys)
    )
    print(f"ticket filter  {r['ticket_regex']}   ({scope})")
    fields = ", ".join(f"{k}={v}" for k, v in r["fields"].items())
    print(f"fields         {fields or '—'}")
    print(
        f"issues         {r['issues_seen']} seen -> {r['issues_kept']} kept,"
        f" {r['issues_skipped']} skipped"
        + (
            f" ({r['issues_skipped_type']} of the kept ones are an unsupported issue type)"
            if r['issues_skipped_type']
            else ""
        )
    )
    if r.get("issues_failed"):
        print(
            f"issues failed  {r['issues_failed']} skipped after a fetch error"
            " (not marked done; a later run inside the overlap window retries them)"
        )
        for failure in r.get("failures") or []:
            print(f"  {failure.get('key')}: {failure.get('error')}")
    if r.get("truncated"):
        print(
            f"truncated      --limit reached: the watermark was NOT advanced, so the"
            f" issues not read ({r['issues_seen']} seen) stay queued for the next run"
        )
    if r.get("resumed"):
        print(
            f"resumed        from {r.get('resumed_from') or '—'}"
            f" · {r.get('events_carried', 0)} event(s) carried"
            f" · {r.get('issues_skipped_resumed', 0)} issue(s) already checkpointed"
        )
    counts = r["counts"]
    print(
        f"objects        {r['objects']}  (epics={counts['epics']}, stories={counts['stories']},"
        f" sprints={counts['sprints']}, people={counts['people']})"
    )
    print(f"events         {r['events']}")
    acts = ", ".join(f"{k}={v}" for k, v in r["activities_by_activity"].items())
    print(f"activities     {acts}")
    print(
        f"changelog      {r['changelog_items']} item(s) -> {r['transitions']} status,"
        f" {r['points_changes']} points, {r['sprint_changes']} sprint,"
        f" {r['assigned_changes']} assignee · {r['changelog_ignored']} dropped"
    )
    if r["points_rounded"]:
        print(f"points         {r['points_rounded']} fractional value(s) rounded to whole points")
    print(
        f"worklogs       {r['worklogs']}  (delay: "
        + ", ".join(f"{k}={v}" for k, v in r["log_delays"].items())
        + ")"
    )
    if r["sprint_at_start"]:
        print(f"sprint start   {r['sprint_at_start']} membership-at-start event(s) synthesised")
    by_kind = ", ".join(f"{k}={v}" for k, v in r["requests_by_kind"].items())
    print(f"requests       {r['requests']}" + (f"  ({by_kind})" if by_kind else ""))
    print(f"watermark      {r['previous_watermark'] or '—'} -> {r['watermark'] or '—'}"
          f"   (overlap {r['overlap_hours']}h{', full' if r['full'] else ''})")


def _print_scrape(r: dict) -> None:
    print(f"source         {r['source']} (run {r['run_id']})")
    print(f"bundle         {r['bundle']}  ({r['bundle_bytes']} bytes)")
    print(f"sha256         {r['bundle_sha256']}")
    print(f"state          {r['state_dir']}  (watermark + prs + identity, raw zone, never transferred)")
    print(f"identity map   {r['identity_map']}")
    scope = filter_scope(tuple(r['ticket_projects']), tuple(r.get('ticket_keys') or ()))
    print(f"ticket filter  {r['ticket_regex']}   ({scope})")
    print(
        f"PRs            {r['prs_seen']} seen -> {r['prs_kept']} kept, {r['prs_dropped']} dropped "
        f"(no ticket key)"
    )
    if r.get("prs_failed"):
        print(f"PRs failed     {r['prs_failed']} skipped after a fetch error (retried next run)")
    if r.get("resumed"):
        print(
            f"resumed        from {r.get('resumed_from') or '—'}"
            f" · {r.get('events_carried', 0)} event(s) carried"
            f" · {r.get('prs_skipped_resumed', 0)} PR(s) already checkpointed"
        )
    if r.get("source_moved"):
        print(f"source moved   {', '.join(r['source_moved'])} (not quiescent during the walk)")
    partial = r.get("partial") or {}
    for reason in partial.get("reasons") or []:
        print(f"partial        {reason.get('repo')}: {reason.get('reason')} — {reason.get('detail')}")
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
    push = r.get("push_class") or {}
    if push.get("enabled"):
        counts = ", ".join(f"{k}={v}" for k, v in (push.get("counts") or {}).items()) or "none"
        reasons = ", ".join(f"{k}={v}" for k, v in (push.get("reasons") or {}).items())
        print(f"push class     {counts}")
        if reasons:
            print(f"push reasons   {reasons}")
        size = push.get("diff_size") or {}
        unknown = ", ".join(
            f"{k}={v}" for k, v in (size.get("reasons") or {}).items()
        )
        print(f"diff size      {size.get('measured', 0)} PR(s) measured"
              + (f" · unknown: {unknown}" if unknown else ""))
    elif push:
        why = "git not installed" if "git" in push and not push.get("git") else "off"
        print(f"push class     {why} (no local git cache)")
    text = r.get("text_classify") or {}
    if text.get("enabled"):
        detail = " ".join(
            str(part) for part in (text.get("backend"), text.get("model") or text.get("version"))
            if part
        )
        print(
            f"text classify  {detail}"
            + f" · {text.get('calls', 0)} calls"
            + (f", {text['fallbacks']} fallback(s)" if text.get("fallbacks") else "")
        )
        kinds = ", ".join(f"{k}={v}" for k, v in (text.get("comment_types") or {}).items())
        if kinds:
            print(f"comment types  {kinds}")
        outcomes = ", ".join(f"{k}={v}" for k, v in (text.get("thread_outcomes") or {}).items())
        if outcomes:
            print(f"thread outcome {outcomes}")
        print(
            f"questions      {text.get('questions_raised', 0)} raised · "
            f"{text.get('questions_answered', 0)} answered"
        )
        if text.get("bot_comments_skipped"):
            print(f"bot notices    {text['bot_comments_skipped']} skipped (not dialogue)")
    elif text:
        print("text classify  off (no comment/description labels)")


def build_parser(prog: str = "megaspeed-scrape") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Scrape a source, classify + pseudonymise it, and write a transfer bundle (.tgz).",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="call the real Bitbucket DC REST API")
    mode.add_argument("--fixture", action="store_true", help="scrape synthetic data (no credentials)")
    mode.add_argument("--fixture-file", metavar="PATH", help="scrape a committed fixture bundle")
    ap.add_argument("--jira", action="store_true",
                    help="scrape Jira (Epic/Story/Sprint + worklogs) instead of Bitbucket")
    ap.add_argument("--jira-cloud", action="store_true",
                    help="scrape Jira **Cloud** (v3 + Bearer PAT, prd/24) instead of DC;"
                         " implies --jira and reads MEGASPEED_JIRA_CLOUD_BASE_URL"
                         " + MEGASPEED_JIRA_CLOUD_TOKEN")
    ap.add_argument("--tempo", action="store_true",
                    help="read worklogs through the Tempo add-on instead of Jira's native"
                         " worklog (prd/30); implies --jira, and applies to --jira-cloud too")
    ap.add_argument("--project", action="append", default=None, metavar="KEY",
                    help="--jira: override the configured Jira projects (repeatable)")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="--jira: read at most N issues (dev/demo)")
    ap.add_argument("--out", default=None, metavar="PATH.tgz",
                    help=f"bundle to write (default {DEFAULT_OUT}, or {DEFAULT_JIRA_OUT} for --jira)")
    ap.add_argument("--state-dir", default=None, metavar="DIR",
                    help="watermark cursor + identity map (default: next to --out, <out>.state/)")
    ap.add_argument("--identity", default=None, metavar="PATH",
                    help="raw-zone identity map to use instead of <state-dir>/identity.json")
    ap.add_argument("--overlap-hours", type=int, default=DEFAULT_OVERLAP_HOURS, metavar="N",
                    help=f"re-emit this far before the watermark (default {DEFAULT_OVERLAP_HOURS})")
    ap.add_argument("--checkpoint-every", type=int, default=DEFAULT_CHECKPOINT_EVERY, metavar="N",
                    help="checkpoint the bundle + cursors every N kept PRs, and at every repo"
                         f" boundary (default {DEFAULT_CHECKPOINT_EVERY}; 0 disables)")
    ap.add_argument("--allow-partial", "--force", dest="allow_partial", action="store_true",
                    help="write the bundle even when the run looks implausibly partial"
                         " (few PRs vs the previous run); without it a suspicious run exits 3")
    ap.add_argument("--no-source-check", action="store_true",
                    help="skip the quiescence guard (the per-repo newest-PR re-read that"
                         " notices a source changing mid-scrape)")
    ap.add_argument("--full", action="store_true", help="ignore the watermark and re-scrape everything")
    ap.add_argument("--no-prefilter", action="store_true",
                    help="fetch every PR's commits before filtering (old, slower path; prd/14)")
    ap.add_argument("--no-push-class", action="store_true",
                    help="skip the deterministic push_class (no local git cache; prd/16)")
    ap.add_argument("--no-text-classify", action="store_true",
                    help="skip comment/description classification (no local LLM; prd/16)")
    ap.add_argument("--text-backend", choices=("ollama", "mock"), default=None,
                    help="classifier for comment text (default: mock for --fixture, ollama for --live)")
    ap.add_argument("--llm-url", default=None, metavar="URL",
                    help="local Ollama base URL (default MEGASPEED_LLM_URL, "
                         f"{classify.DEFAULT_URL})")
    ap.add_argument("--llm-model", default=None, metavar="MODEL",
                    help=f"local Ollama model (default MEGASPEED_LLM_MODEL, {classify.DEFAULT_MODEL})")
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
    if args.limit is not None and args.limit < 1:
        print(f"--limit must be at least 1 (got {args.limit})", file=sys.stderr)
        return 2
    if args.progress_interval is not None and args.progress_interval < 0:
        print("--progress-interval must be >= 0 (0 disables)", file=sys.stderr)
        return 2
    if args.checkpoint_every < 0:
        print("--checkpoint-every must be >= 0 (0 disables)", file=sys.stderr)
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
    if args.no_push_class:
        cfg.push_class = False
    if args.no_text_classify:
        cfg.text_classify = False
    if args.text_backend:
        cfg.text_backend = args.text_backend
    if args.llm_url:
        cfg.llm_url = args.llm_url
    if args.llm_model:
        cfg.llm_model = args.llm_model

    if args.jira_cloud:
        # prd/24: one source, two shapes. ``--jira-cloud`` is a Jira scrape with
        # the Cloud adapter, so every downstream flag (--project, --limit, ...)
        # means the same thing.
        args.jira = True
    if args.tempo:
        # prd/30: Tempo is a Jira add-on, so ``--tempo`` implies a Jira scrape.
        # With ``--jira-cloud`` it reads Tempo Cloud instead of Tempo DC.
        args.jira = True

    seed = args.seed if args.seed is not None else cfg.seed
    generate_n = args.n if args.n is not None else 50
    out = Path(args.out).expanduser() if args.out else Path(
        DEFAULT_JIRA_OUT if args.jira else DEFAULT_OUT
    )
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir(out)

    # Progress goes to stderr only, so a piped --json report stays machine
    # readable (prd/14 "Progress reporting").
    interval = 0.0 if args.quiet else (
        args.progress_interval
        if args.progress_interval is not None
        else cfg.progress_interval
    )
    progress = ProgressReporter(interval=interval)

    if args.jira:
        from .jira import JiraError
        from .scrape_jira import scrape as scrape_jira

        projects = None
        if args.project:
            projects = tuple(str(p).strip().upper() for p in args.project if str(p).strip())
        try:
            if args.probe:
                if args.jira_cloud and not cfg.jira_cloud_base_url:
                    print("--jira-cloud --probe needs MEGASPEED_JIRA_CLOUD_BASE_URL", file=sys.stderr)
                    return 2
                if not args.jira_cloud and not cfg.jira_base_url:
                    print("--jira --probe needs MEGASPEED_JIRA_BASE_URL", file=sys.stderr)
                    return 2
                transport, _source, _limit, client = _jira_transport(cfg, args)
                return _jira_probe(cfg, transport, args.json, client=client)
            transport, source, limit, client = _jira_transport(cfg, args)
            report = scrape_jira(
                cfg,
                transport,
                source,
                out=out,
                state_dir=state_dir,
                limit=limit,
                overlap_hours=args.overlap_hours,
                full=args.full,
                identity_path=args.identity,
                progress=progress,
                projects=projects,
                checkpoint_every=args.checkpoint_every,
                client=client,
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
            _print_jira_scrape(report)
            print(f"\nnext           python3 -m ingest import {report['bundle']}")
        return 0

    if not cfg.repos:
        print(
            "no repos configured: set MEGASPEED_REPOS=PROJECT/REPO,PROJECT/REPO"
            " (or --repo) for the Bitbucket scrape",
            file=sys.stderr,
        )
        return 2

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
            checkpoint_every=args.checkpoint_every,
            verify_quiet=False if args.no_source_check else None,
        )
    except BitbucketError as exc:
        print(f"bitbucket error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_scrape(report)
        print(f"\nnext           python3 -m ingest import {report['bundle']}")
    return _partial_exit(report, allow_partial=args.allow_partial)


def _partial_exit(report: dict, *, allow_partial: bool) -> int:
    """Warn about a partial-looking run; exit non-zero unless overridden.

    A bundle that is *valid* but implausibly small used to exit 0, so a run that
    saw 5 PRs where the previous one saw 400 looked like success and became the
    new baseline. Failures (PRs skipped after a 404/reset) are always warned
    about; the size heuristic decides the exit code.
    """
    partial = report.get("partial") or {}
    for failure in partial.get("failures") or []:
        print(
            f"warning: skipped {failure.get('repo')}#{failure.get('pr_id')}:"
            f" {failure.get('error')}",
            file=sys.stderr,
        )
    if report.get("prs_failed"):
        print(
            f"warning: {report['prs_failed']} PR(s) could not be fetched and were skipped"
            " (they are not marked done; the next run retries them)",
            file=sys.stderr,
        )
    for reason in partial.get("reasons") or []:
        print(
            f"warning: {reason.get('repo')}: {reason.get('reason')}"
            f" -- {reason.get('detail')}",
            file=sys.stderr,
        )
    if report.get("source_moved"):
        print(
            "warning: the source changed during the scrape: "
            + ", ".join(report["source_moved"])
            + " (a corpus rebuild must not run concurrently with a scrape)",
            file=sys.stderr,
        )
    if partial.get("suspicious") and not allow_partial:
        print(
            "error: this run looks partial (see the warnings above), so the bundle is"
            " probably missing PRs; the state cursors were advanced anyway.\n"
            "       re-run with --full to re-read everything, or pass"
            " --allow-partial/--force to accept it",
            file=sys.stderr,
        )
        return 3
    return 0


__all__ = ["DEFAULT_JIRA_OUT", "DEFAULT_OUT", "build_parser", "main", "REPO_ROOT"]

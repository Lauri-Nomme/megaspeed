"""Emit the missing nested-reply events from the capture (prd/76).

A store ingested before prd/76 has one ``comment_added`` event per **thread**;
the replies inside that comment were dropped with the raw payload. The capture
is the only source that still has them, so this command re-reads it for the PRs
the store already holds and writes one ``comment_added`` event per reply -- no
git walk, no model, no raw text. The rows are built by the same
:func:`ingest.pipeline.reply_events` helper the reducer uses, so a backfilled
store is indistinguishable from a freshly ingested one.

    python3 -m ingest backfill-replies \\
        --hans-scrape ~/dev/hans/scrape/demo \\
        --db data/demo.duckdb \\
        --identity ~/dev/hans/scrape/demo/identity.json

Dry run by default (counts only). ``--apply`` backs the store up first, writes,
saves any pseudonyms it had to allocate into the identity map, and re-verifies
the contract. It is idempotent: an ``event_key`` already in the store is
skipped, so a second ``--apply`` inserts 0.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bitbucket import BitbucketClient
from .config import Config, load_config
from .hans import HansTransport
from .pipeline import SYSTEM_ACTOR, _ts, classify_actor, reply_events
from .pseudonym import IdentityTable, actor_key
from .store import connect, imported_scope, utcnow, verify, write_events


class BackfillError(RuntimeError):
    pass


def _pr_rows(con, repo: str) -> list[tuple[int, list[str], Any]]:
    return [
        (int(pr_id), list(keys or []), created)
        for pr_id, keys, created in con.execute(
            "SELECT pr_id, ticket_keys, created_at FROM objects"
            " WHERE object_type = 'PR' AND repo = ? ORDER BY pr_id",
            [repo],
        ).fetchall()
    ]


def _stored(con, repo: str) -> tuple[set[str], set[str]]:
    """Event keys already in the store and thread ids it holds, for ``repo``."""
    events = {
        row[0]
        for row in con.execute(
            "SELECT event_key FROM events WHERE repo = ?", [repo]
        ).fetchall()
    }
    threads = {
        row[0]
        for row in con.execute(
            "SELECT object_id FROM objects WHERE object_type = 'THREAD' AND repo = ?",
            [repo],
        ).fetchall()
    }
    return events, threads


def backfill_replies(
    cfg: Config,
    *,
    hans_scrape: str | Path,
    identity_path: str | Path | None = None,
    apply: bool = False,
    backup: bool = True,
) -> dict:
    """Read the capture for the stored PRs and emit their reply events."""
    transport = HansTransport(hans_scrape)
    repo = f"{transport.project}/{transport.repo}"
    identity = IdentityTable(Path(identity_path or cfg.identity_path).expanduser())
    known_authors = set(identity.entries)
    unknown_authors: set[str] = set()
    actor_kinds: dict[str, str] = {}
    as_of = utcnow()
    report: dict[str, Any] = {
        "mode": "backfill-replies",
        "db": str(cfg.db_path),
        "capture": str(transport.root),
        "repo": repo,
        "applied": False,
    }
    if not Path(cfg.db_path).is_file():
        raise BackfillError(f"no store at {cfg.db_path}")

    def resolve(user: dict | None) -> str:
        if not user:
            # The reducer's ``_actor`` reads a missing author as the synthetic
            # system actor, and ``classify_actor(None)`` as a bot.
            actor_kinds.setdefault(SYSTEM_ACTOR, "bot")
            return SYSTEM_ACTOR
        who = identity.pseudonym(user)
        # The raw record is in hand here (and only here), so bot-vs-human is
        # decided the way the scrape decided it -- the identity entry keeps the
        # pseudonym, not the account's SERVICE marker.
        actor_kinds.setdefault(who, classify_actor(user, cfg.bot_slugs))
        return who

    con = connect(cfg.db_path)
    try:
        prs = _pr_rows(con, repo)
        existing, threads = _stored(con, repo)
        client = BitbucketClient(transport, page_limit=1000, min_interval=0.0)
        events: list[dict] = []
        relations: list[tuple[str, str, str]] = []
        found = inserted = skipped = orphaned = 0
        for pr_id, ticket_keys, created_at in prs:
            try:
                activities = sorted(
                    client.iter_activities(transport.project, transport.repo, pr_id),
                    key=lambda a: (a.get("createdDate") or 0, a.get("id") or 0),
                )
            except Exception:  # noqa: BLE001 - a torn capture row is not fatal
                continue
            fallback_ts = created_at or as_of
            ticket_objects = [f"TICKET:{key}" for key in ticket_keys]
            for index, activity in enumerate(activities):
                if str(activity.get("action") or "") != "COMMENTED":
                    continue
                if activity.get("commentAction") not in (None, "ADDED"):
                    continue
                comment = activity.get("comment") or {}
                if comment.get("id") is None:
                    continue
                parent_id = int(comment["id"])
                thread_id = f"THREAD:{repo}#{pr_id}:{parent_id}"
                replies = list(enumerate(comment.get("comments") or [], start=1))
                found += len(replies)
                if thread_id not in threads:
                    # The opener was never ingested (a filtered/partial store):
                    # a reply alone would dangle off a missing THREAD.
                    orphaned += len(replies)
                    continue
                natural = f"act:{activity.get('id') or index}"
                new: list[tuple[int, dict]] = []
                for position, reply in replies:
                    key = f"{repo}#{pr_id}:{natural}:reply{position}"
                    if key in existing:
                        skipped += 1
                        continue
                    existing.add(key)
                    new.append((position, reply))
                    raw = (reply or {}).get("author") or {}
                    if raw:
                        actor = actor_key(raw)
                        if actor not in known_authors:
                            unknown_authors.add(actor)
                if not new:
                    continue
                objects = [f"PR:{repo}#{pr_id}", *ticket_objects, thread_id]
                for event in reply_events(
                    repo=repo,
                    pr_id=pr_id,
                    parent_natural=natural,
                    opener_ts=_ts(activity.get("createdDate"), fallback_ts),
                    parent_comment_id=parent_id,
                    replies=new,
                    actor_for=resolve,
                    actor_kind_for=lambda actor: actor_kinds.get(actor, "human"),
                    as_of=as_of,
                ):
                    events.append(event)
                    for oid in objects:
                        relations.append(
                            (event["event_key"], oid.split(":", 1)[0], oid)
                        )

        inserted = len(events)
        report.update(
            {
                "prs_read": len(prs),
                "replies_found": found,
                "replies_inserted": inserted,
                "replies_skipped": skipped,
                "unmatched_threads": orphaned,
                "unknown_authors": len(unknown_authors),
            }
        )
        if not apply:
            report["dry_run"] = True
            return report
        if backup:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            backup_path = Path(str(cfg.db_path) + f".pre-backfill-{stamp}.duckdb")
            shutil.copy2(cfg.db_path, backup_path)
            report["backup"] = str(backup_path)
        con.execute("BEGIN")
        try:
            write_events(con, events, relations)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        identity.save()
        result = verify(con, cfg.key_shape, cfg.ticket_projects, imported_scope(con))
        report["verified"] = bool(result["ok"])
        report["problems"] = list(result["problems"])[:5]
        report["applied"] = True
        return report
    finally:
        con.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m ingest backfill-replies",
        description="Emit nested-reply comment_added events from a hans capture (prd/76).",
    )
    ap.add_argument("--hans-scrape", required=True, metavar="DIR",
                    help="the capture the store was ingested from")
    ap.add_argument("--db", default=None, metavar="PATH")
    ap.add_argument("--identity", default=None, metavar="PATH",
                    help="the capture's raw-zone identity map (default: config)")
    ap.add_argument("--apply", action="store_true",
                    help="write the store (default: dry run, counts only)")
    ap.add_argument("--no-backup", action="store_true",
                    help="do not copy the store before --apply")
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.db:
        cfg.db_path = Path(args.db).expanduser().resolve()
    try:
        report = backfill_replies(
            cfg,
            hans_scrape=args.hans_scrape,
            identity_path=args.identity,
            apply=bool(args.apply),
            backup=not args.no_backup,
        )
    except (BackfillError, OSError) as exc:
        print(f"backfill-replies failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report.get("applied") or report.get("dry_run") else 1
    print(f"db       {report['db']}")
    print(f"capture  {report['capture']}  ({report['repo']})")
    print(f"PRs      {report['prs_read']} read")
    print(
        f"replies  {report['replies_found']} found · "
        f"{report['replies_inserted']} inserted · {report['replies_skipped']} skipped"
    )
    if report["unmatched_threads"]:
        print(f"orphans  {report['unmatched_threads']} reply(ies) with no stored THREAD — skipped")
    print(f"authors  {report['unknown_authors']} new pseudonym(s)")
    if report.get("applied"):
        print(f"backup   {report.get('backup', '(none)')}")
        print(f"verify   {'OK' if report.get('verified') else report.get('problems')}")
    else:
        print("dry run  — re-run with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

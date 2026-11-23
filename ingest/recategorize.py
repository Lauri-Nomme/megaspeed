"""Re-derive ``comment_kind`` for an already-imported store (prd/75).

The first scrape had no CI/admin vocabulary: a coverage bot's report was sent to
the model and got ``nit``/``optimization``, an administrative "rebased <url>"
got ``suggestion``, and the bot's own ``comment_added`` events still say
``actor_kind: human`` because the account was not configured as a bot at scrape
time (the PERSON row was corrected later).

This command is deterministic and idempotent. It reads the texts from a
``classify`` trace (the same raw-zone file a relabel uses), classifies each
thread's opener with :mod:`ingest.citext`, and writes only closed labels:

* ``comment_kind`` on every THREAD (review / ci_notice / ci_finding / bot /
  bot_request / admin) plus the CI facts (``ci_template``, ``ci_issues``,
  ``coverage_min``, ``coverage_zero``);
* removes ``comment_type`` / ``classifier_version`` / ``label_source`` from a
  thread that is **not** a human review remark -- a bot's report is not a
  review label, and calling it one corrupts the comment-tax denominator;
* repairs the stale ``actor_kind`` on events to match the PERSON row.

``thread_outcome`` is deliberately left alone: it is derived from the diff and
the resolver, not from the comment text, so it stays truthful for any thread.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import citext, taxonomy
from .config import Config, load_config
from .llmtrace import read_trace
from .store import connect, imported_scope, verify


class RecategorizeError(RuntimeError):
    pass


def _person_kinds(con) -> dict[str, str]:
    kinds: dict[str, str] = {}
    for oid, attrs in con.execute(
        "SELECT object_id, CAST(attributes AS VARCHAR) FROM objects WHERE object_type='PERSON'"
    ).fetchall():
        kind = str((json.loads(attrs or "{}") or {}).get("actor_kind") or "human")
        kinds[oid.partition(":")[2]] = kind
    return kinds


def _openers(con) -> dict[str, str]:
    """thread id -> opener actor (each thread has exactly one comment_added)."""
    return {
        tid: actor
        for tid, actor in con.execute(
            """
            SELECT r.object_id, e.actor
            FROM events e
            JOIN event_objects r ON e.event_key = r.event_key
            WHERE e.activity='comment_added' AND r.object_type='THREAD'
            """
        ).fetchall()
    }


def _trace_texts(trace_path: Path) -> dict[str, str]:
    records, _bad = read_trace(trace_path)
    texts: dict[str, str] = {}
    for record in records:
        context = record.get("context") or {}
        if context.get("role") != "opener" or not context.get("thread_id"):
            continue
        texts[str(context["thread_id"])] = str(record.get("text") or "")
    return texts


def _kind_for(text: str, opener_kind: str, aliases: tuple[str, ...]) -> tuple[str, dict[str, Any]]:
    """``(comment_kind, extra attributes)`` from the opener text and actor kind."""
    if opener_kind == "bot":
        template, issues, low = citext.ci_kind(text)
        if template == "unknown":
            return "bot", {"ci_template": "unknown"}
        extra: dict[str, Any] = {"ci_template": template}
        if issues is not None:
            extra["ci_issues"] = int(issues)
        if low is not None:
            extra["coverage_min"] = max(0, min(100, int(round(low))))
            extra["coverage_zero"] = low <= 0
        return ("ci_finding" if template == "qodana_issues" else "ci_notice"), extra
    if text and citext.is_bot_request(text, aliases):
        return "bot_request", {}
    if text and citext.is_admin(text):
        return "admin", {}
    return "review", {}


def recategorize(
    cfg: Config,
    *,
    trace_path: str | Path,
    apply: bool = False,
    backup: bool = True,
    progress=None,
) -> dict:
    trace_path = Path(trace_path).expanduser()
    if not trace_path.is_file():
        raise RecategorizeError(f"no trace at {trace_path}")
    con = connect(cfg.db_path)
    report: dict[str, Any] = {
        "mode": "recategorize",
        "db": str(cfg.db_path),
        "trace": str(trace_path),
        "applied": False,
    }
    try:
        person_kind = _person_kinds(con)
        openers = _openers(con)
        texts = _trace_texts(trace_path)
        rows = con.execute(
            "SELECT object_id, CAST(attributes AS VARCHAR) FROM objects WHERE object_type='THREAD'"
        ).fetchall()
        kinds = Counter()
        removed_types = Counter()
        no_text = Counter()
        changed = 0
        updates: list[tuple[str, str]] = []
        for thread_id, raw in rows:
            attributes = json.loads(raw or "{}") or {}
            actor = openers.get(thread_id, "")
            opener_kind = person_kind.get(actor, "human")
            text = texts.get(thread_id, "")
            if not text:
                no_text[opener_kind] += 1
                # Without text a bot is still a bot; a human stays a review
                # remark and keeps its label (the safe default).
                kind, extra = ("bot", {}) if opener_kind == "bot" else ("review", {})
            else:
                kind, extra = _kind_for(text, opener_kind, cfg.bot_aliases)
            kinds[kind] += 1
            new = dict(attributes)
            new["comment_kind"] = kind
            new.update(extra)
            if kind == "review":
                if new.get("comment_type") and not new.get("label_source"):
                    new["label_source"] = "opener"
            else:
                for key in ("comment_type", "classifier_version", "label_source"):
                    if key in new:
                        removed_types[f"{kind}:{key}"] += 1
                        del new[key]
            new = taxonomy.check_object_attributes(new)
            if new != attributes:
                changed += 1
                updates.append(
                    (thread_id, json.dumps(new, sort_keys=True, separators=(",", ":")))
                )
        # A stale ``actor_kind`` on the events is a data defect of the first
        # scrape, not a label choice: a bot's events must say bot.
        stale = con.execute(
            """
            SELECT event_key, actor, CAST(attributes AS VARCHAR)
            FROM events
            WHERE json_extract_string(attributes, '$.actor_kind') <> 'bot'
            """
        ).fetchall()
        actor_updates: list[tuple[str, str]] = []
        for event_key, actor, raw in stale:
            if person_kind.get(actor) != "bot":
                continue
            attributes = json.loads(raw or "{}") or {}
            attributes["actor_kind"] = "bot"
            actor_updates.append(
                (event_key, json.dumps(attributes, sort_keys=True, separators=(",", ":")))
            )

        report.update(
            {
                "threads": len(rows),
                "threads_changed": changed,
                "kinds": dict(kinds),
                "removed_labels": dict(removed_types),
                "no_text": dict(no_text),
                "actor_kind_repaired": len(actor_updates),
            }
        )
        if not apply:
            report["dry_run"] = True
            return report
        if backup:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            backup_path = Path(str(cfg.db_path) + f".pre-recategorize-{stamp}.duckdb")
            shutil.copy2(cfg.db_path, backup_path)
            report["backup"] = str(backup_path)
        con.execute("BEGIN")
        try:
            for thread_id, payload in updates:
                con.execute(
                    "UPDATE objects SET attributes = CAST(? AS JSON) WHERE object_id = ?",
                    [payload, thread_id],
                )
            for event_key, payload in actor_updates:
                con.execute(
                    "UPDATE events SET attributes = CAST(? AS JSON) WHERE event_key = ?",
                    [payload, event_key],
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        result = verify(con, cfg.key_shape, cfg.ticket_projects, imported_scope(con))
        report["verified"] = bool(result["ok"])
        report["problems"] = list(result["problems"])[:5]
        report["applied"] = True
        return report
    finally:
        con.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m ingest recategorize",
        description="Re-derive comment_kind for an imported store (prd/75).",
    )
    ap.add_argument("--trace", required=True, metavar="PATH",
                    help="the classify trace whose opener texts are the source")
    ap.add_argument("--db", default=None, metavar="PATH")
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
        report = recategorize(
            cfg,
            trace_path=args.trace,
            apply=bool(args.apply),
            backup=not args.no_backup,
        )
    except (RecategorizeError, OSError) as exc:
        print(f"recategorize failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report.get("applied") or report.get("dry_run") else 1
    print(f"db       {report['db']}")
    print(f"trace    {report['trace']}")
    print(f"threads  {report['threads']} · changed {report['threads_changed']}")
    print(f"kinds    {report['kinds']}")
    if report["removed_labels"]:
        print(f"removed  {report['removed_labels']}")
    if report["no_text"]:
        print(f"no text  {report['no_text']} (bot -> bot, human -> review)")
    print(f"events   actor_kind repaired: {report['actor_kind_repaired']}")
    if report.get("applied"):
        print(f"backup   {report.get('backup', '(none)')}")
        print(f"verify   {'OK' if report.get('verified') else report.get('problems')}")
    else:
        print("dry run  — re-run with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

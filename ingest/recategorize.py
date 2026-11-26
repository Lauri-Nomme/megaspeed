"""Re-derive ``comment_kind`` for an already-imported store (prd/75).

The first scrape had no CI/admin vocabulary: a coverage bot's report was sent to
the model and got ``nit``/``optimization``, an administrative "rebased <url>"
got ``suggestion``, and the bot's own ``comment_added`` events still say
``actor_kind: human`` because the account was not configured as a bot at scrape
time (the PERSON row was corrected later).

This command is deterministic and idempotent. It reads the texts from a
``classify`` trace (the same raw-zone file a relabel uses), classifies each
thread's **opener** with :mod:`ingest.citext`, and writes only closed labels:

* ``comment_kind`` on every THREAD (review / ci_notice / ci_finding / bot /
  bot_request / admin) plus the CI facts (``ci_template``, ``ci_issues``,
  ``coverage_min``, ``coverage_zero``);
* removes ``comment_type`` / ``classifier_version`` / ``label_source`` from a
  thread that is **not** a human review remark -- a bot's report is not a
  review label, and calling it one corrupts the comment-tax denominator;
* repairs the stale ``actor_kind`` on events to match the PERSON row.

``thread_outcome`` is deliberately left alone: it is derived from the diff and
the resolver, not from the comment text, so it stays truthful for any thread.

prd/79: a human opener written by the PR author is ``admin`` (the author's own
note is not a review remark) unless it asks the reviewers something. The author
comes from the PR object's ``actor`` (its documented role), joined through the
thread id; a thread with no text stays ``review``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from . import citext, taxonomy
from .config import Config, load_config
from .llmtrace import read_trace
from .store import backup_db, connect, imported_scope, verify


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
    """thread id -> opener actor (each thread has exactly one opener).

    prd/76: a thread now also has one ``comment_added`` event per nested reply.
    They carry ``reply=true``; without the filter a reply (which sorts after the
    opener in the store but could be any actor) would be read as the opener.
    """
    return {
        tid: actor
        for tid, actor in con.execute(
            """
            SELECT r.object_id, e.actor
            FROM events e
            JOIN event_objects r ON e.event_key = r.event_key
            WHERE e.activity='comment_added' AND r.object_type='THREAD'
              AND coalesce(json_extract_string(e.attributes, '$.reply'), 'false') <> 'true'
            """
        ).fetchall()
    }


def _pr_authors(con) -> dict[str, str]:
    """PR object id -> author pseudonym (``objects.actor``, prd/44 DES-6).

    The column's role for a PR *is* the author (``store.ACTOR_ROLE_BY_TYPE``),
    so this is the same deterministic pseudonym the reducer wrote in
    :meth:`ingest.pipeline.Reducer.reduce`. An old or hand-built store may have
    no ``actor`` on its PR rows; the map is then empty and the prd/79 rule
    simply never fires (never guessed).
    """
    return {
        object_id: actor
        for object_id, actor in con.execute(
            "SELECT object_id, actor FROM objects"
            " WHERE object_type='PR' AND actor IS NOT NULL"
        ).fetchall()
        if actor
    }


def _thread_pr(thread_id: str) -> str:
    """``THREAD:<repo>#<pr>:<comment>`` -> ``PR:<repo>#<pr>`` (else ``""``)."""
    body = thread_id.partition("THREAD:")[2]
    repo_pr, _sep, comment = body.rpartition(":")
    if not repo_pr or not comment:
        return ""
    return f"PR:{repo_pr}"


def _trace_texts(trace_path: Path) -> dict[str, str]:
    """thread id -> **opener** text (prd/78: replies are never read here).

    A reply carries ``role=reply`` in the trace; it is skipped, so a status note
    there can never re-categorise its thread. Only the opener decides the kind.
    """
    records, _bad = read_trace(trace_path)
    texts: dict[str, str] = {}
    for record in records:
        context = record.get("context") or {}
        if context.get("role") != "opener" or not context.get("thread_id"):
            continue
        texts[str(context["thread_id"])] = str(record.get("text") or "")
    return texts


def _is_author_note(text: str, opener_kind: str, opener_actor: str, author: str) -> bool:
    """prd/79: a human opener written by the PR author that is not a question.

    An empty (or whitespace) body is not an author note: without text the safe
    default is ``review``, never a guess.
    """
    return bool(
        (text or "").strip()
        and author
        and opener_kind != "bot"
        and opener_actor == author
        and not citext.looks_like_question(text)
    )


def _kind_for(
    text: str,
    opener_kind: str,
    aliases: tuple[str, ...],
    *,
    opener_actor: str = "",
    author: str = "",
) -> tuple[str, dict[str, Any]]:
    """``(comment_kind, extra attributes)`` from the opener text and actor kind.

    prd/79: a human opener written *by the PR author* is an administrative note,
    not a review remark -- unless it asks the reviewers something. The check
    runs after the bot paths (a bot is never an author note) and before
    ``is_admin``/``is_notice`` (which it mostly subsumes for author text). With
    no text there is no author-note path: the caller keeps ``review``.
    """
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
    if _is_author_note(text, opener_kind, opener_actor, author):
        # prd/79: the author explaining or justifying their own change. The
        # existing ``admin`` kind, no new attribute (no schema change).
        return "admin", {}
    if text and citext.is_notice(text):
        # prd/78: rebase/merge, review requests, author status notes, bare
        # links, fyi -- a status note, not a review remark.
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
    # Copy the store before opening it: on Windows an open DuckDB file cannot be
    # copied (WinError 32), and an --apply must always have its backup.
    backup_path = (
        backup_db(cfg.db_path, "recategorize") if apply and backup else None
    )
    con = connect(cfg.db_path)
    report: dict[str, Any] = {
        "mode": "recategorize",
        "db": str(cfg.db_path),
        "trace": str(trace_path),
        "applied": False,
    }
    try:
        person_kind = _person_kinds(con)
        pr_author = _pr_authors(con)
        openers = _openers(con)
        texts = _trace_texts(trace_path)
        rows = con.execute(
            "SELECT object_id, CAST(attributes AS VARCHAR) FROM objects WHERE object_type='THREAD'"
        ).fetchall()
        kinds = Counter()
        removed_types = Counter()
        no_text = Counter()
        author_notes = 0
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
                author = pr_author.get(_thread_pr(thread_id), "")
                if _is_author_note(text, opener_kind, actor, author):
                    # prd/79: reported on its own line so the new admin count can
                    # be checked against the author-opener count (minus questions).
                    author_notes += 1
                kind, extra = _kind_for(
                    text, opener_kind, cfg.bot_aliases,
                    opener_actor=actor, author=author,
                )
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
                "author_notes": author_notes,
                "removed_labels": dict(removed_types),
                "no_text": dict(no_text),
                "actor_kind_repaired": len(actor_updates),
            }
        )
        if not apply:
            report["dry_run"] = True
            return report
        if backup_path is not None:
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
    print(f"notes    {report['author_notes']} author openers -> admin (prd/79)")
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

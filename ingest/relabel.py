"""Re-determine the text labels from the raw trace; emit a labels-only patch.

The trace (:mod:`ingest.llmtrace`) holds every classified text with the ids it
came from. This tool replays those texts through a (possibly different) model,
recomputes the thread labels exactly as the reducer did, and writes a
:mod:`ingest.labelpatch` tgz that corrects **only the labels** -- no git walk, no
REST calls, no new events.

    python3 -m ingest relabel \\
        --trace export/hans.state/classify-trace.jsonl \\
        --bundle export/hans.tgz \\
        --out export/hans-labels.tgz \\
        --llm-url http://127.0.0.1:11434 --llm-model qwen3:8b

Then on the import side::

    python3 -m ingest import export/hans-labels.tgz --db data/demo.duckdb

What is replayed: the **opening** comment of each thread and each PR
description -- the two texts that became labels on a THREAD object / a
``question_raised`` event. Replies only affect whether a question got answered,
which is a structural event, not a label; they stay in the trace for later.

The interesting property: the *text* never leaves the raw zone. The patch is a
handful of closed labels, so the airgap contract is untouched.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import classify, labelpatch, llmtrace, taxonomy
from .bundle import Bundle, BundleError, read_bundle
from .config import load_config
from .logs import iso
from .progress import ProgressReporter

#: Roles whose label lands on a stored row. ``reply`` is traced but not patched.
REPLAY_ROLES = ("opener", "description")


class RelabelError(RuntimeError):
    """The trace/bundle pairing cannot produce a meaningful patch."""


def _index(bundle: Bundle) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """THREAD objects by id, and ``question_raised`` events by linked object id.

    A description question is keyed by its **PR** (``…:desc:q``); a thread
    question is keyed by its **THREAD**. The PR key deliberately holds only the
    description event: a thread event is also related to the PR, and patching
    "the PR's question" must not clobber every thread's phase.
    """
    threads = {
        obj["object_id"]: obj for obj in bundle.objects if obj.get("object_type") == "THREAD"
    }
    questions: dict[str, list[dict]] = {}
    for event in bundle.events:
        if event.get("activity") != "question_raised":
            continue
        key = str(event.get("event_key") or "")
        if key.endswith(":desc:q"):
            questions.setdefault(
                f"PR:{event.get('repo')}#{event.get('pr_id')}", []
            ).append(event)
            continue
        for object_id in event.get("objects") or []:
            if str(object_id).startswith("THREAD:"):
                questions.setdefault(str(object_id), []).append(event)
    return threads, questions


def _replay_items(records: list[dict]) -> list[dict]:
    """The trace records whose labels land on a stored row, one per target.

    A resumed scrape appends to the same trace, so the same thread can appear
    twice; the **last** record for a target wins (it is the most recent call).
    """
    items: dict[tuple, dict] = {}
    for record in records:
        context = record.get("context") or {}
        if context.get("role") not in REPLAY_ROLES:
            continue
        if record.get("kind") not in classify.KINDS:
            continue
        text = str(record.get("text") or "")
        if not text.strip():
            continue
        if context.get("role") == "opener":
            if not context.get("thread_id"):
                continue
            key = ("thread", context["thread_id"], record.get("kind"))
        else:
            key = ("pr", context.get("repo"), context.get("pr_id"), record.get("kind"))
        items[key] = record
    return list(items.values())


def _outcome(thread: dict, intent: str) -> str:
    attributes = thread.get("attributes") or {}
    return classify.thread_outcome(
        resolved=str(thread.get("state") or "") == "resolved",
        anchor_changed=attributes.get("anchor_changed"),
        resolver_is_commenter=bool(attributes.get("resolver_is_commenter", False)),
        thread_intent=intent,
    )


def _thread_pr_id(object_id: str) -> int | None:
    """``THREAD:DEMO/widget#11096:123`` -> ``11096`` (``None`` on a bad shape)."""
    try:
        return int(str(object_id).split("#", 1)[1].split(":", 1)[0])
    except (IndexError, ValueError):
        return None


def _records_from_hans(
    cfg, transport, bundle: Bundle, progress: ProgressReporter | None = None
) -> list[dict]:
    """Re-read the texts from the **capture** instead of a trace.

    A scrape that ran before the trace existed (or whose trace was lost) still
    has its source: the hans capture on disk. This mode reads only the PRs the
    bundle holds, re-applies the reducer's deterministic gating (human author,
    ``ADDED`` comment, non-empty body), and produces the same record shape the
    trace does -- so the patch builder is shared. It touches no git.
    """
    from .bitbucket import BitbucketClient
    from .pipeline import classify_actor

    client = BitbucketClient(transport, page_limit=1000, min_interval=0.0)
    project, repo = transport.project, transport.repo
    repo_label = f"{project}/{repo}"
    threads = {
        obj["object_id"] for obj in bundle.objects if obj.get("object_type") == "THREAD"
    }
    pr_objects = {
        obj["pr_id"]: obj for obj in bundle.objects if obj.get("object_type") == "PR"
    }
    commit_ids: dict[int, list[str]] = {}
    for obj in bundle.objects:
        if obj.get("object_type") == "COMMIT" and obj.get("pr_id") is not None:
            commit_ids.setdefault(int(obj["pr_id"]), []).append(
                str(obj["object_id"]).split(":", 1)[1]
            )
    wanted = {pr_id for pr_id in pr_objects if pr_id is not None}
    wanted.update(
        pr_id for pr_id in (_thread_pr_id(oid) for oid in threads) if pr_id is not None
    )
    records: list[dict] = []
    for ordinal, pr_id in enumerate(sorted(wanted), start=1):
        if progress is not None and progress.enabled and ordinal % 250 == 0:
            progress.emit(f"[relabel] {ordinal}/{len(wanted)} PR(s) read from the capture")
        try:
            detail = client.pull_request(project, repo, pr_id)
            activities = sorted(
                client.iter_activities(project, repo, pr_id),
                key=lambda a: (a.get("createdDate") or 0, a.get("id") or 0),
            )
        except Exception:  # noqa: BLE001 - a torn capture row is not fatal
            continue
        soup = pr_objects.get(pr_id) or {}
        common = {
            "repo": repo_label,
            "pr_id": pr_id,
            "ticket_keys": list(soup.get("ticket_keys") or []),
            "commit_ids": sorted(commit_ids.get(pr_id) or []),
        }
        description = str(detail.get("description") or "").strip()
        if description:
            records.append({
                "kind": classify.KIND_DESCRIPTION,
                "text": description,
                "context": {
                    **common,
                    "role": "description",
                    "thread_id": None,
                    "comment_id": None,
                    "activity_id": None,
                    "ts": None,
                },
            })
        for activity in activities:
            if str(activity.get("action") or "") != "COMMENTED":
                continue
            if activity.get("commentAction") not in (None, "ADDED"):
                continue
            comment = activity.get("comment") or {}
            comment_id = comment.get("id")
            if comment_id is None:
                continue
            thread_id = f"THREAD:{repo_label}#{pr_id}:{comment_id}"
            if thread_id not in threads:
                continue
            if classify_actor(activity.get("user") or {}, cfg.bot_slugs) == "bot":
                continue
            body = str(comment.get("text") or "").strip()
            if not body:
                continue
            records.append({
                "kind": classify.KIND_COMMENT,
                "text": body,
                "context": {
                    **common,
                    "role": "opener",
                    "thread_id": thread_id,
                    "comment_id": comment_id,
                    "activity_id": activity.get("id"),
                    "ts": None,
                },
            })
    return records


def relabel(
    cfg,
    *,
    trace_path: str | Path | None = None,
    hans_scrape: str | Path | None = None,
    bundle_path: str | Path,
    out: str | Path,
    classifier: classify.Classifier | None = None,
    limit: int | None = None,
    progress: ProgressReporter | None = None,
) -> dict:
    """Replay the trace (or the capture), compare with ``bundle``, write a patch."""
    bundle_path = Path(bundle_path).expanduser()
    out = Path(out).expanduser()
    reporter = progress if progress is not None else ProgressReporter(interval=30)
    records: list[dict] = []
    bad = 0
    source_label = ""
    if hans_scrape is not None:
        from .hans import HansTransport

        transport = HansTransport(hans_scrape)
        source_label = f"capture {transport.root}"
        bundle_for_records = read_bundle(bundle_path)
        records = _records_from_hans(cfg, transport, bundle_for_records, reporter)
        texts_seen = len(records)
    else:
        if trace_path is None:
            raise RelabelError("one of --trace / --hans-scrape is required")
        trace_path = Path(trace_path).expanduser()
        source_label = f"trace {trace_path}"
        if not trace_path.is_file():
            raise RelabelError(
                f"no trace at {trace_path} (did the scrape write <state-dir>/"
                f"{llmtrace.TRACE_FILE}?)"
            )
        records, bad = llmtrace.read_trace(trace_path)
        texts_seen = len(records)
    items = _replay_items(records)
    if limit is not None:
        items = items[: max(0, int(limit))]
    bundle = read_bundle(bundle_path)
    threads, questions = _index(bundle)
    text = classifier if classifier is not None else classify.for_scrape(cfg, "hans")
    if text is None:
        raise RelabelError("text classification is off (MEGASPEED_TEXT_CLASSIFY=0)")
    if reporter.enabled and getattr(text, "on_call", None) is None:
        text.on_call = lambda record: reporter.emit(llmtrace.call_line(record))

    # The same text in ten threads is one model call: cache by (kind, text) and
    # reuse the answer. The trace still has one record per thread, so one line
    # per call (not per row) is honest.
    answers: dict[tuple[str, str], dict] = {}
    replayed = 0
    reused = 0
    rows: list[dict] = []
    flips = 0
    missing = 0
    changed_types = 0
    changed_outcomes = 0
    changed_phases = 0
    for ordinal, record in enumerate(items, start=1):
        context = record.get("context") or {}
        kind = str(record["kind"])
        body = str(record["text"])
        marker = (kind, body)
        labels = answers.get(marker)
        if labels is None:
            labels = text.classify(body, kind)
            answers[marker] = labels
            replayed += 1
        else:
            reused += 1
        if reporter.enabled and ordinal % 500 == 0:
            reporter.emit(
                f"[relabel] {ordinal}/{len(items)} text(s) · {replayed} call(s),"
                f" {reused} reused · {len(rows)} row(s) so far"
            )

        if context.get("role") == "description":
            pr_id = f"PR:{context.get('repo')}#{context.get('pr_id')}"
            events = questions.get(pr_id) or []
            if bool(labels.get("question")) != bool(events):
                flips += 1
            elif events and labels.get("phase"):
                phase = labels["phase"]
                if any((event.get("attributes") or {}).get("phase") != phase for event in events):
                    rows.append(
                        {
                            "target": "question",
                            "object_id": pr_id,
                            "labels": {"phase": phase},
                            "classifier_version": text.version,
                            "previous": {
                                "phase": (events[0].get("attributes") or {}).get("phase")
                            },
                        }
                    )
                    changed_phases += 1
            continue

        thread_id = str(context.get("thread_id"))
        thread = threads.get(thread_id)
        if thread is None:
            # The patch is built against this bundle: a thread the bundle does
            # not hold means a mismatched trace/bundle pair, which the caller
            # must see rather than silently importing a partial patch.
            missing += 1
            continue
        attributes = thread.get("attributes") or {}
        comment_type = labels.get("comment_type") or attributes.get("comment_type")
        intent = labels.get("thread_intent") or "unclear"
        outcome = _outcome(thread, intent)
        new = {
            "comment_type": comment_type,
            "thread_outcome": outcome,
            "classifier_version": text.version,
        }
        previous = {
            "comment_type": attributes.get("comment_type"),
            "thread_outcome": attributes.get("thread_outcome"),
            "classifier_version": attributes.get("classifier_version"),
        }
        if new != previous:
            rows.append(
                {
                    "target": "thread",
                    "object_id": thread_id,
                    "labels": new,
                    "previous": previous,
                }
            )
            if new["comment_type"] != previous["comment_type"]:
                changed_types += 1
            if new["thread_outcome"] != previous["thread_outcome"]:
                changed_outcomes += 1

        events = questions.get(thread_id) or []
        if bool(labels.get("question")) != bool(events):
            flips += 1
        elif events and labels.get("phase"):
            phase = labels["phase"]
            if any((event.get("attributes") or {}).get("phase") != phase for event in events):
                rows.append(
                    {
                        "target": "question",
                        "object_id": thread_id,
                        "labels": {"phase": phase},
                        "classifier_version": text.version,
                        "previous": {"phase": (events[0].get("attributes") or {}).get("phase")},
                    }
                )
                changed_phases += 1

    classifier_report = text.report()
    calls = int(classifier_report.get("calls") or 0)
    fallbacks = int(classifier_report.get("fallbacks") or 0)
    if calls and fallbacks >= calls:
        # Every call degraded to the deterministic mock: the "new" labels would
        # be the ones already in the bundle, or worse, a half-model mix. Writing
        # that patch would look like a successful re-classification.
        raise RelabelError(
            f"every one of {calls} model call(s) fell back to"
            f" {classifier_report.get('version')}: {classifier_report.get('last_error') or 'no model answer'}"
            " -- fix the endpoint/model and re-run (nothing was written)"
        )

    manifest = {
        "kind": labelpatch.PATCH_KIND,
        "created_at": iso(),
        "source": bundle.source,
        "classifier_version": taxonomy.CLASSIFIER_VERSION,
        "ticket_projects": list(bundle.ticket_projects),
        "ticket_keys": list(bundle.ticket_keys),
        "applies_to": {
            "bundle": bundle_path.name,
            "bundle_sha256": bundle.sha256,
            "input": source_label,
            "run_id": (bundle.manifest.get("run") or {}).get("run_id"),
        },
        "input": {
            "source": "capture" if hans_scrape is not None else "trace",
            "records": len(records),
            "unreadable": bad,
            "replayable": len(items),
            "texts": len(answers),
            "reused": reused,
        },
        "text_classifier": text.report(),
        "counts": {
            "question_flips": flips,
            "missing_threads": missing,
            "changed_comment_type": changed_types,
            "changed_thread_outcome": changed_outcomes,
            "changed_phase": changed_phases,
        },
    }
    written = labelpatch.write_patch(out, manifest=manifest, rows=rows)
    report = {
        "mode": "relabel",
        "input": source_label,
        "input_kind": "capture" if hans_scrape is not None else "trace",
        "records": len(records),
        "unreadable": bad,
        "replayable": len(items),
        "texts_classified": replayed,
        "texts_reused": reused,
        "bundle": str(bundle_path),
        "bundle_sha256": bundle.sha256,
        "patch": str(out),
        "patch_bytes": written["bytes"],
        "patch_sha256": written["sha256"],
        "rows": len(rows),
        "threads": sum(1 for row in rows if row["target"] == "thread"),
        "questions": sum(1 for row in rows if row["target"] == "question"),
        "question_flips": flips,
        "missing_threads": missing,
        "changed_comment_type": changed_types,
        "changed_thread_outcome": changed_outcomes,
        "changed_phase": changed_phases,
        "classifier": text.report(),
    }
    return report


def _print(report: dict) -> None:
    print(f"input          {report['input']}")
    print(
        f"               {report['records']} record(s),"
        f" {report['unreadable']} unreadable · {report['replayable']} replayable"
    )
    print(f"bundle         {report['bundle']}  sha256 {report['bundle_sha256'][:16]}…")
    classifier = report.get("classifier") or {}
    detail = " ".join(
        str(part)
        for part in (classifier.get("backend"), classifier.get("model") or classifier.get("version"))
        if part
    )
    print(
        f"classifier     {detail} · {classifier.get('calls', 0)} call(s),"
        f" {classifier.get('fallbacks', 0)} fallback(s)"
        + (f" · {report['texts_reused']} text(s) reused" if report["texts_reused"] else "")
    )
    print(
        f"patch          {report['patch']}  sha256 {report['patch_sha256'][:16]}…"
        f"  ({report['rows']} row(s))"
    )
    print(
        f"labels         {report['threads']} thread · {report['questions']} question phase"
        f" · comment_type {report['changed_comment_type']} ·"
        f" outcome {report['changed_thread_outcome']}"
    )
    if report["question_flips"]:
        print(
            f"question flips {report['question_flips']} (not applied: a label patch"
            " cannot add or remove a question_raised event)"
        )
    if report.get("missing_threads"):
        print(
            f"not found      {report['missing_threads']} thread(s) from the trace are"
            " not in this bundle (mismatched trace/bundle pair?)"
        )
    print(f"next           python3 -m ingest import {report['patch']}")


def build_parser(prog: str = "python3 -m ingest relabel") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Replay <state-dir>/classify-trace.jsonl through a model and write"
                    " a labels-only patch tgz.",
    )
    ap.add_argument("--trace", metavar="PATH",
                    help="the classifier trace the scrape wrote (state dir)")
    ap.add_argument("--hans-scrape", metavar="DIR",
                    help="read the texts from a hans capture instead of a trace (for a run"
                         " whose bundle predates the trace); reads no git")
    ap.add_argument("--bundle", required=True, metavar="PATH.tgz",
                    help="the bundle that run wrote (the patch is built against it)")
    ap.add_argument("--out", default=None, metavar="PATH.tgz",
                    help="patch to write (default: <bundle>-labels.tgz)")
    ap.add_argument("--text-backend", choices=("ollama", "mock"), default=None,
                    help="classifier to replay with (default: config / ollama)")
    ap.add_argument("--llm-url", default=None, metavar="URL")
    ap.add_argument("--llm-model", default=None, metavar="MODEL")
    ap.add_argument("--llm-workers", type=int, default=None, metavar="N")
    ap.add_argument("--llm-think", dest="llm_think", action="store_true", default=None)
    ap.add_argument("--no-llm-think", dest="llm_think", action="store_false")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help="replay at most N text(s) (a quick check)")
    ap.add_argument("--progress-interval", type=float, default=None, metavar="SECONDS")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--config", default=None)
    ap.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if bool(args.trace) == bool(args.hans_scrape):
        print("choose exactly one input: --trace PATH or --hans-scrape DIR", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    if getattr(args, "text_backend", None):
        cfg.text_backend = args.text_backend
    if args.llm_url:
        cfg.llm_url = args.llm_url
    if args.llm_model:
        cfg.llm_model = args.llm_model
    if args.llm_workers is not None:
        cfg.llm_workers = max(1, int(args.llm_workers))
    if args.llm_think is not None:
        cfg.llm_think = bool(args.llm_think)
    interval = 0.0 if args.quiet else (
        args.progress_interval if args.progress_interval is not None else 30.0
    )
    out = Path(args.out).expanduser() if args.out else Path(
        str(Path(args.bundle).expanduser().with_suffix("")) + "-labels.tgz"
    )
    try:
        report = relabel(
            cfg,
            trace_path=args.trace or None,
            hans_scrape=args.hans_scrape,
            bundle_path=args.bundle,
            out=out,
            limit=args.limit,
            progress=ProgressReporter(interval=interval),
        )
    except (RelabelError, BundleError, classify.ClassifyError) as exc:
        print(f"relabel failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print(report)
    return 0


__all__ = ["RelabelError", "build_parser", "main", "relabel"]

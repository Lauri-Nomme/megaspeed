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
import time
from datetime import datetime, timezone
from pathlib import Path

from . import classify, labelpatch, llmtrace, taxonomy
from .bundle import Bundle, BundleError, read_bundle
from .config import load_config
from .logs import iso
from .progress import ProgressReporter, format_elapsed
from .pseudonym import IdentityTable
from .timeutil import iso as ts_iso

#: Roles whose label lands on a stored row. ``reply`` is traced but not patched.
REPLAY_ROLES = ("opener", "description")


class RelabelError(RuntimeError):
    """The trace/bundle pairing cannot produce a meaningful patch."""


def _index(bundle: Bundle) -> tuple[dict[str, dict], dict[str, list[dict]], dict[str, dict]]:
    """THREAD objects by id, question events by object id, and PR objects by id.

    A description question is keyed by its **PR** (``…:desc:q``); a thread
    question is keyed by its **THREAD**. The PR key deliberately holds only the
    description event: a thread event is also related to the PR, and patching
    "the PR's question" must not clobber every thread's phase.
    """
    threads = {
        obj["object_id"]: obj for obj in bundle.objects if obj.get("object_type") == "THREAD"
    }
    prs = {
        obj["object_id"]: obj for obj in bundle.objects if obj.get("object_type") == "PR"
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
    return threads, questions, prs


def _replay_items(records: list[dict]) -> list[dict]:
    """The trace records whose labels land on a stored row, one per target.

    A resumed scrape appends to the same trace, so the same thread can appear
    twice; the **last** record for a target wins (it is the most recent call).
    """
    items: dict[tuple, dict] = {}
    for record in records:
        if record.get("batch"):
            # A relabel trace can hold batched call records (diagnostics only);
            # only per-text records know which row they came from.
            continue
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


def _add_question_events(
    record: dict, thread: dict | None, pr: dict | None, phase: str
) -> list[dict] | None:
    """The derived event(s) a re-label that *adds* a question must create.

    Mirrors the reducer: ``question_raised`` at the comment's (or description's)
    timestamp by the pseudonym that wrote it, plus a ``question_answered`` with
    ``answer_source: resolution`` when the thread was closed by someone other
    than the asker. ``None`` means the trace lacks the facts to build it (a
    pre-v0.16 trace), and the flip is reported as unappliable, never guessed.
    """
    context = record.get("context") or {}
    repo = str(context.get("repo") or "")
    pr_id = context.get("pr_id")
    if not repo or not isinstance(pr_id, int):
        return None
    actor = str(
        context.get("actor")
        or (pr or {}).get("actor")
        or (thread or {}).get("actor")
        or ""
    )
    if not actor:
        return None
    stamp = str(context.get("ts") or iso())
    tickets = [f"TICKET:{key}" for key in context.get("ticket_keys") or []]
    common = {"repo": repo, "pr_id": pr_id, "as_of": stamp}
    events: list[dict] = []
    if context.get("role") == "description":
        events.append({
            **common,
            "event_key": f"{repo}#{pr_id}:desc:q",
            "ts": stamp,
            "actor": actor,
            "objects": [f"PR:{repo}#{pr_id}", *tickets],
            "attributes": {"phase": phase},
        })
        return events
    activity_id = context.get("activity_id")
    thread_id = str(context.get("thread_id") or "")
    if activity_id is None or not thread_id:
        return None
    events.append({
        **common,
        "event_key": f"{repo}#{pr_id}:act:{activity_id}:q",
        "ts": stamp,
        "actor": actor,
        "objects": [f"PR:{repo}#{pr_id}", *tickets, thread_id],
        "attributes": {"phase": phase},
    })
    attributes = (thread or {}).get("attributes") or {}
    if (
        thread is not None
        and thread.get("state") == "resolved"
        and not attributes.get("resolver_is_commenter")
        and thread.get("actor")
        and thread.get("closed_at")
    ):
        events.append({
            **common,
            "event_key": f"{repo}#{pr_id}:t{context.get('comment_id')}:a",
            "ts": ts_iso(thread["closed_at"]),
            "as_of": ts_iso(thread["closed_at"]),
            "actor": thread["actor"],
            "objects": [f"PR:{repo}#{pr_id}", *tickets, thread_id],
            "attributes": {"phase": phase, "answer_source": "resolution"},
        })
    return events


def _thread_pr_id(object_id: str) -> int | None:
    """``THREAD:DEMO/widget#11096:123`` -> ``11096`` (``None`` on a bad shape)."""
    try:
        return int(str(object_id).split("#", 1)[1].split(":", 1)[0])
    except (IndexError, ValueError):
        return None


def _ms_iso(value) -> str | None:
    """Epoch milliseconds (the DC payload) -> the event's ISO stamp."""
    try:
        return ts_iso(datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc))
    except (TypeError, ValueError, OSError):
        return None


def _records_from_hans(
    cfg,
    transport,
    bundle: Bundle,
    progress: ProgressReporter | None = None,
    identity=None,
) -> list[dict]:
    """Re-read the texts from the **capture** instead of a trace.

    A scrape that ran before the trace existed (or whose trace was lost) still
    has its source: the hans capture on disk. This mode reads only the PRs the
    bundle holds, re-applies the reducer's deterministic gating (human author,
    ``ADDED`` comment, non-empty body), and produces the same record shape the
    trace does -- so the patch builder is shared. It touches no git.

    ``identity`` is the scrape's raw-zone identity map: with it, a question a
    re-label adds can carry the **asker's** pseudonym (not the PR author's).
    """
    from .bitbucket import BitbucketClient
    from .pipeline import classify_actor

    def pseudonym(user: dict | None) -> str | None:
        if identity is None or not user:
            return None
        return identity.pseudonym(user)

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
                    "ts": _ms_iso(detail.get("createdDate")),
                    "actor": pseudonym(detail.get("author")),
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
                    "ts": _ms_iso(activity.get("createdDate")),
                    "actor": pseudonym(activity.get("user") or comment.get("author")),
                },
            })
    return records


class LabelCache:
    """Append-only, raw-text-free label cache: the resume checkpoint.

    One JSON line per classified text::

        {"kind": "comment", "digest": "…", "labels": {…},
         "model": "qwen3:30b-a3b", "think": true, "version": "ollama-v1"}

    The digest is :func:`ingest.classify.digest` (a blake2b of the text), so the
    cache carries **no prose** -- only labels and the model identity that
    produced them. A resumed run (or a re-run after a model change) reuses an
    entry only when model, think flag and classifier version all match; anything
    else is re-classified and appended.

    A torn last line is skipped, exactly like the trace reader.
    """

    def __init__(self, path: str | Path, *, model: str, think: bool, version: str):
        self.path = Path(path)
        self.model = str(model or "")
        self.think = bool(think)
        self.version = str(version or "")
        self.labels: dict[tuple[str, str], dict] = {}
        self.read = 0
        self.bad = 0
        self._handle = None
        if self.path.is_file():
            self._load()

    def _key(self, kind: str, text: str) -> tuple[str, str]:
        return str(kind), classify.digest(str(text)).hex()

    @staticmethod
    def _project(kind: str, labels: dict) -> dict:
        """Only the keys that kind actually has (a description has no comment_type)."""
        return {
            key: labels[key] for key in classify.labels_for(kind) if key in labels
        }

    def _matches(self, entry: dict) -> bool:
        return (
            str(entry.get("model") or "") == self.model
            and bool(entry.get("think", False)) == self.think
            and str(entry.get("version") or "") == self.version
        )

    def _load(self) -> None:
        with self.path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    self.bad += 1
                    continue
                if not isinstance(entry, dict) or not isinstance(entry.get("labels"), dict):
                    self.bad += 1
                    continue
                kind = str(entry.get("kind") or "")
                digest = str(entry.get("digest") or "")
                if not kind or not digest or not self._matches(entry):
                    continue
                try:
                    labels = classify.parse_labels(
                        kind, self._project(kind, entry["labels"])
                    )
                except (classify.ClassifyError, ValueError):
                    # A hand-edited or truncated label bag must never become a
                    # patch row: re-classify instead.
                    self.bad += 1
                    continue
                self.labels[(kind, digest)] = labels
                self.read += 1

    def get(self, kind: str, text: str) -> dict | None:
        return self.labels.get(self._key(kind, text))

    def put(self, kind: str, text: str, labels: dict) -> None:
        key = self._key(kind, text)
        if key in self.labels:
            return
        labels = self._project(kind, labels)
        self.labels[key] = labels
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
        entry = {
            "kind": key[0],
            "digest": key[1],
            "labels": labels,
            "model": self.model,
            "think": self.think,
            "version": self.version,
        }
        self._handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __del__(self):  # pragma: no cover - a safety net for an error path
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass

    def __len__(self) -> int:
        return len(self.labels)


def _stop_requested(path: Path) -> bool:
    """A stop file is the polite Ctrl-C: finish the batch, keep the cache, exit."""
    return path.is_file()


def _consume_stop_file(path: Path) -> None:
    """Move the stop file aside so the resumed run does not stop immediately."""
    try:
        path.replace(path.with_name(path.name + ".used"))
    except OSError:  # pragma: no cover - a locked file just stays
        pass


def relabel(
    cfg,
    *,
    trace_path: str | Path | None = None,
    hans_scrape: str | Path | None = None,
    bundle_path: str | Path,
    out: str | Path,
    classifier: classify.Classifier | None = None,
    identity_path: str | Path | None = None,
    batch_size: int = 1,
    labels_cache: str | Path | None = None,
    stop_file: str | Path | None = None,
    call_trace_path: str | Path | None = None,
    limit: int | None = None,
    progress: ProgressReporter | None = None,
) -> dict:
    """Replay the trace (or the capture), compare with ``bundle``, write a patch."""
    bundle_path = Path(bundle_path).expanduser()
    out = Path(out).expanduser()
    reporter = progress if progress is not None else ProgressReporter(interval=30)
    bundle = read_bundle(bundle_path)
    records: list[dict] = []
    bad = 0
    source_label = ""
    if hans_scrape is not None:
        from .hans import HansTransport

        transport = HansTransport(hans_scrape)
        source_label = f"capture {transport.root}"
        identity = IdentityTable(Path(identity_path).expanduser()) if identity_path else None
        # One read: the same parsed bundle scopes the replay and is patched.
        records = _records_from_hans(cfg, transport, bundle, reporter, identity=identity)
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
    threads, questions, prs = _index(bundle)
    text = classifier if classifier is not None else classify.for_scrape(cfg, "hans")
    if text is None:
        raise RelabelError("text classification is off (MEGASPEED_TEXT_CLASSIFY=0)")
    trace_writer = (
        llmtrace.TraceWriter(Path(call_trace_path).expanduser())
        if call_trace_path
        else None
    )
    if trace_writer is not None:
        trace_writer.on_error = reporter.emit

    def _emit_call(record: dict) -> None:
        if trace_writer is not None:
            trace_writer(record)
        if reporter.enabled:
            reporter.emit(llmtrace.call_line(record))

    def _emit_batch(record: dict) -> None:
        if trace_writer is not None:
            trace_writer(record)
        if reporter.enabled:
            reporter.emit(llmtrace.batch_line(record))

    if trace_writer is not None or reporter.enabled:
        text.on_call = _emit_call
        if hasattr(text, "on_batch"):
            text.on_batch = _emit_batch

    # The same text in ten threads is one model call. The cache is the resume
    # checkpoint: it is flushed after every batch, so Ctrl-C / a stop file / a
    # reboot loses at most the batch in flight.
    cache_path = Path(labels_cache).expanduser() if labels_cache else Path(
        str(out) + ".labels.jsonl"
    )
    stop_path = Path(stop_file).expanduser() if stop_file else Path(str(out) + ".stop")
    cache = LabelCache(
        cache_path,
        model=getattr(text, "model", None) or "",
        think=bool(getattr(text, "think", False)),
        version=text.version,
    )
    started = time.monotonic()
    if reporter.enabled:
        reporter.emit(
            f"[relabel] {len(items)} text(s) to classify · {len(threads)} thread(s)"
            f" · {len(prs)} PR(s) in the bundle · batch {max(1, int(batch_size))}"
            f" · cache {cache_path}"
            f" · trace {trace_writer.path if trace_writer is not None else 'off'}"
        )

    markers: list[tuple[str, str]] = []
    contexts: dict[tuple[str, str], dict] = {}
    seen: set[tuple[str, str]] = set()
    for record in items:
        marker = (str(record["kind"]), str(record["text"]))
        if marker in seen:
            continue
        seen.add(marker)
        markers.append(marker)
        contexts[marker] = record.get("context") or {}

    answers: dict[tuple[str, str], dict] = {}
    pending: list[tuple[str, str]] = []
    for marker in markers:
        labels = cache.get(*marker)
        if labels is not None:
            answers[marker] = labels
        else:
            pending.append(marker)
    cache_hits = len(markers) - len(pending)

    # -- classification pass: batches of one kind, cache flushed per batch ----
    model_label = (
        f"{getattr(text, 'backend', '?')}:{getattr(text, 'model', None) or text.version}"
    )
    batch_size = max(1, int(batch_size))
    classified = 0
    batches = 0
    stopped = ""
    next_log = 500
    index = 0
    try:
        while index < len(pending):
            if _stop_requested(stop_path):
                stopped = f"stop file {stop_path}"
                _consume_stop_file(stop_path)
                break
            kind = pending[index][0]
            group: list[tuple[str, str]] = []
            while (
                index < len(pending)
                and pending[index][0] == kind
                and len(group) < batch_size
            ):
                group.append(pending[index])
                index += 1
            bodies = [body for _kind, body in group]
            batch_started = time.monotonic()
            try:
                if len(group) > 1 and hasattr(text, "classify_batch"):
                    results = text.classify_batch([
                        (body, kind, contexts.get((kind, body))) for body in bodies
                    ])
                else:
                    results = [
                        (
                            text.classify(body, kind, contexts.get((kind, body))),
                            getattr(text, "last_version", text.version),
                        )
                        for body in bodies
                    ]
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad batch is retried per text
                reporter.emit(
                    f"[relabel] batch of {len(group)} failed"
                    f" ({type(exc).__name__}: {exc}); retrying one by one"
                )
                results = [
                    (
                        text.classify(body, kind, contexts.get((kind, body))),
                        getattr(text, "last_version", text.version),
                    )
                    for body in bodies
                ]
            for (mkind, body), (labels, _version) in zip(group, results):
                answers[(mkind, body)] = labels
                cache.put(mkind, body, labels)
                classified += 1
            cache.flush()
            batches += 1
            if reporter.enabled and len(group) > 1:
                reporter.emit(
                    f"[relabel] batch {len(group)} ·"
                    f" {time.monotonic() - batch_started:.1f}s · {kind} · {model_label}"
                )
            if reporter.enabled and classified >= next_log:
                elapsed = max(1e-6, time.monotonic() - started)
                rate = classified / elapsed
                eta = (len(pending) - classified) / rate if rate > 0 else 0
                reporter.emit(
                    f"[relabel] {classified}/{len(pending)} text(s) ·"
                    f" {cache_hits} cached · {batch_size <= 1 and '1 call' or str(batches) + ' batch(es)'}"
                    f" · {rate:.2f} text/s · eta {format_elapsed(eta)}"
                )
                next_log += 500
    except KeyboardInterrupt:
        stopped = "interrupted (Ctrl-C)"

    if stopped:
        cache.close()
        if trace_writer is not None:
            trace_writer.close()
        return {
            "mode": "relabel",
            "stopped": True,
            "reason": stopped,
            "input": source_label,
            "patch": None,
            "labels_cache": str(cache_path),
            "cached_labels": len(cache),
            "classified": classified,
            "remaining": len(pending) - classified,
            "texts_total": len(markers),
            "batches": batches,
            "trace": str(trace_writer.path) if trace_writer is not None else None,
            "trace_records": trace_writer.count if trace_writer is not None else 0,
            "classifier": text.report(),
        }

    rows: list[dict] = []
    flips = 0
    added = 0
    removed = 0
    unappliable = 0
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
            raise RelabelError(
                f"no label for {kind} text {classify.digest(body).hex()[:12]}"
                " (the classification pass missed it)"
            )
        if reporter.enabled and ordinal % 500 == 0:
            reporter.emit(f"[relabel] {ordinal}/{len(items)} text(s) mapped")

        if context.get("role") == "description":
            pr_id = f"PR:{context.get('repo')}#{context.get('pr_id')}"
            events = questions.get(pr_id) or []
            phase = str(labels.get("phase") or "implementation")
            if bool(labels.get("question")) != bool(events):
                flips += 1
                if labels.get("question"):
                    built = _add_question_events(record, None, prs.get(pr_id), phase)
                    if built:
                        rows.append({
                            "target": "question",
                            "object_id": pr_id,
                            "action": "add",
                            "labels": {"phase": phase},
                            "events": built,
                            "previous": {"phase": None},
                        })
                        added += 1
                    else:
                        unappliable += 1
                else:
                    rows.append({
                        "target": "question",
                        "object_id": pr_id,
                        "action": "delete",
                        "labels": {"phase": phase},
                        "previous": {
                            "phase": (events[0].get("attributes") or {}).get("phase")
                        },
                    })
                    removed += 1
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
        phase = str(labels.get("phase") or attributes.get("phase") or "review")
        if bool(labels.get("question")) != bool(events):
            flips += 1
            if labels.get("question"):
                built = _add_question_events(record, thread, prs.get(f"PR:{context.get('repo')}#{context.get('pr_id')}"), phase)
                if built:
                    rows.append({
                        "target": "question",
                        "object_id": thread_id,
                        "action": "add",
                        "labels": {"phase": phase},
                        "events": built,
                        "previous": {"phase": None},
                    })
                    added += 1
                else:
                    unappliable += 1
            else:
                rows.append({
                    "target": "question",
                    "object_id": thread_id,
                    "action": "delete",
                    "labels": {"phase": phase},
                    "previous": {"phase": (events[0].get("attributes") or {}).get("phase")},
                })
                removed += 1
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
        cache.close()
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
            "texts": len(markers),
            "cached": cache_hits,
            "classified": classified,
            "batches": batches,
            "batch_size": batch_size,
            "labels_cache": cache_path.name,
            "trace": (
                {"file": trace_writer.path.name, "records": trace_writer.count}
                if trace_writer is not None
                else {"enabled": False}
            ),
        },
        "text_classifier": text.report(),
        "counts": {
            "question_flips": flips,
            "question_added": added,
            "question_removed": removed,
            "question_unappliable": unappliable,
            "missing_threads": missing,
            "changed_comment_type": changed_types,
            "changed_thread_outcome": changed_outcomes,
            "changed_phase": changed_phases,
        },
    }
    cache.close()
    if trace_writer is not None:
        trace_writer.close()
    written = labelpatch.write_patch(out, manifest=manifest, rows=rows)
    report = {
        "mode": "relabel",
        "input": source_label,
        "input_kind": "capture" if hans_scrape is not None else "trace",
        "records": len(records),
        "unreadable": bad,
        "replayable": len(items),
        "texts_classified": classified,
        "texts_cached": cache_hits,
        "texts_total": len(markers),
        "batches": batches,
        "batch_size": batch_size,
        "labels_cache": str(cache_path),
        "trace": str(trace_writer.path) if trace_writer is not None else None,
        "trace_records": trace_writer.count if trace_writer is not None else 0,
        "bundle": str(bundle_path),
        "bundle_sha256": bundle.sha256,
        "patch": str(out),
        "patch_bytes": written["bytes"],
        "patch_sha256": written["sha256"],
        "rows": len(rows),
        "threads": sum(1 for row in rows if row["target"] == "thread"),
        "questions": sum(1 for row in rows if row["target"] == "question"),
        "question_flips": flips,
        "question_added": added,
        "question_removed": removed,
        "question_unappliable": unappliable,
        "missing_threads": missing,
        "changed_comment_type": changed_types,
        "changed_thread_outcome": changed_outcomes,
        "changed_phase": changed_phases,
        "classifier": text.report(),
    }
    return report


def _print(report: dict) -> None:
    if report.get("stopped"):
        print(f"input          {report['input']}")
        print(f"stopped        {report['reason']}")
        print(
            f"cache          {report['labels_cache']} · {report['cached_labels']}"
            f" label(s) saved · {report['classified']}/{report['texts_total']} text(s)"
            f" in this run, {report['remaining']} left"
        )
        if report.get("trace"):
            print(f"call trace     {report['trace']} · {report.get('trace_records', 0)} record(s)")
        print("no patch       re-run the same command to resume")
        return
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
        f"classifier     {detail} · {classifier.get('calls', 0)} label(s),"
        f" {classifier.get('batches', 0)} batch(es),"
        f" {classifier.get('retries', 0)} retry(ies),"
        f" {classifier.get('fallbacks', 0)} fallback(s)"
        + (f" · {report['texts_cached']} cached" if report.get("texts_cached") else "")
    )
    if report.get("trace"):
        print(f"call trace     {report['trace']} · {report.get('trace_records', 0)} record(s)")
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
            f"question flips {report['question_flips']} applied ·"
            f" {report['question_added']} event(s) added,"
            f" {report['question_removed']} removed"
            + (
                f" · {report['question_unappliable']} not buildable (pre-v0.16 trace:"
                " no activity id / author)"
                if report["question_unappliable"]
                else ""
            )
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
    ap.add_argument("--identity", metavar="PATH",
                    help="the scrape's raw-zone identity map (with --hans-scrape this lets"
                         " an added question carry the asker's pseudonym)")
    ap.add_argument("--batch-size", type=int, default=1, metavar="N",
                    help="label N same-kind texts per model request (default 1). Each"
                         " batch is validated item by item; a bad batch is retried per text")
    ap.add_argument("--labels-cache", default=None, metavar="PATH",
                    help="resume checkpoint (default: <out>.labels.jsonl). Labels plus"
                         " model/think/version; no text. Re-run the same command to resume")
    ap.add_argument("--stop-file", default=None, metavar="PATH",
                    help="touch this file to stop after the current batch (default:"
                         " <out>.stop); it is moved to .used and no patch is written")
    ap.add_argument("--call-trace", dest="call_trace", default=None, metavar="PATH",
                    help="write every model call here -- full request, response and"
                         " thinking (default <out>.trace.jsonl; raw zone, never imported)")
    ap.add_argument("--no-call-trace", action="store_true",
                    help="do not write the model-call trace")
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
    if args.batch_size < 1:
        print("--batch-size must be at least 1", file=sys.stderr)
        return 2
    interval = 0.0 if args.quiet else (
        args.progress_interval if args.progress_interval is not None else 30.0
    )
    out = Path(args.out).expanduser() if args.out else Path(
        str(Path(args.bundle).expanduser().with_suffix("")) + "-labels.tgz"
    )
    call_trace_path = (
        None
        if args.no_call_trace
        else (
            Path(args.call_trace).expanduser()
            if args.call_trace
            else Path(str(out) + ".trace.jsonl")
        )
    )
    try:
        report = relabel(
            cfg,
            trace_path=args.trace or None,
            hans_scrape=args.hans_scrape,
            bundle_path=args.bundle,
            out=out,
            identity_path=args.identity,
            batch_size=args.batch_size,
            labels_cache=args.labels_cache,
            stop_file=args.stop_file,
            call_trace_path=call_trace_path,
            limit=args.limit,
            progress=ProgressReporter(interval=interval),
        )
    except KeyboardInterrupt:
        print(
            "relabel interrupted — saved labels are in the cache;"
            " re-run the same command to resume",
            file=sys.stderr,
        )
        return 130
    except (RelabelError, BundleError, classify.ClassifyError) as exc:
        print(f"relabel failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print(report)
    return 0


__all__ = ["RelabelError", "build_parser", "main", "relabel"]

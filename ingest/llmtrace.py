"""The **raw-zone** model trace: every classifier call, input and output.

The pseudonymisation contract (prd/02) says a raw comment body is read once and
dropped. It also says a label must be reproducible and tunable. Those two pull
in opposite directions, so the trace lives where raw text is already allowed:
the scrape's ``--state-dir`` (the raw zone), **never** the bundle or the store.

Each call appends one JSON line to ``<state-dir>/classify-trace.jsonl``:

    {
      "ts": "2026-11-22T07:12:03.421Z",     # when the call finished
      "call": 1207,                          # monotonic within the run
      "backend": "ollama", "version": "ollama-v1",
      "model": "qwen3:30b-a3b", "url": "http://127.0.0.1:11434",
      "kind": "comment",
      "context": {"repo": "DEMO/widget", "pr_id": 11096,
                  "thread_id": "THREAD:DEMO/widget#11096:123", "comment_id": 123,
                  "commit_ids": ["…"], "ticket_keys": ["DEMO-1"],
                  "role": "opener"},         # opener | reply | description
      "text": "nit: rename this variable",   # the exact input -- raw zone only
      "text_digest": "…", "text_chars": 27,
      "request": { … the exact /api/chat body (think, format, options) … },
      "response": { … the full Ollama answer, timings included … },
      "content": "{\"comment_type\":\"nit\",…}",   # message.content
      "thinking": "…",                        # message.thinking, when enabled
      "timings": {"wall_ms": 2310.4, "total_duration_ns": 2290000000, …},
      "labels": {"comment_type": "nit", …},   # what was written to the store
      "label_version": "ollama-v1",
      "fallback": false,
      "error": "", "sent": true
    }

Why this is not a contract violation: the trace is first-party *diagnostic*
state next to the source, exactly like the identity map and the pepper. Nothing
in it is ever transferred by :mod:`ingest.bundle`; the label patch
(:mod:`ingest.labelpatch`) that crosses the airgap carries labels only.

The two consumers:

* :class:`TraceWriter` -- what the scrape feeds every call record to;
* :func:`read_trace` + :mod:`ingest.relabel` -- re-classify the same texts with a
  different (or newly available) model, without re-walking git or the REST API.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Callable, Iterator

from . import logs

#: File name inside ``--state-dir``.
TRACE_FILE = "classify-trace.jsonl"

#: ``classify-trace.jsonl`` is append-across-resumes: a resumed run keeps the
#: earlier call records, so the file describes every text the corpus ever had
#: classified under this state dir.
MAX_LINE_BYTES = 1 << 20  # a pathological model answer must not fill the disk silently


class TraceWriter:
    """Append the classifier's call records to one JSONL file.

    Thread-safe: the Ollama backend calls this from its worker pool. One open
    handle, flushed per record, so a killed scrape keeps every completed call.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.count = 0
        self.bytes = 0
        self.skipped = 0
        self._lock = threading.Lock()
        self._handle = None
        #: The first write failure ("IsADirectoryError", "Permission denied");
        #: reported once, because a silently empty trace is undebuggable.
        self.last_error = ""
        #: Optional ``(message)`` sink (the progress reporter adds a timestamp).
        self.on_error: Callable[[str], None] | None = None

    @property
    def enabled(self) -> bool:
        return True

    def __call__(self, record: dict) -> None:
        try:
            line = json.dumps(record, sort_keys=True, default=str) + "\n"
        except (TypeError, ValueError):  # pragma: no cover - default=str covers it
            self.skipped += 1
            return
        if len(line) > MAX_LINE_BYTES:
            # Keep the call, drop the bulk: the labels and the error survive,
            # the (huge) prose is replaced by a marker.
            record = dict(record)
            for key in ("text", "thinking", "content"):
                value = record.get(key)
                if isinstance(value, str) and len(value) > 8192:
                    record[key] = value[:8192] + f"…[truncated {len(value) - 8192} chars]"
            line = json.dumps(record, sort_keys=True, default=str) + "\n"
        try:
            with self._lock:
                if self._handle is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._handle = self.path.open("a", encoding="utf-8")
                self._handle.write(line)
                self._handle.flush()
                self.count += 1
                self.bytes += len(line)
        except OSError as exc:
            # A trace that must not abort the run also must not fail silently.
            message = f"[trace] cannot write {self.path}: {type(exc).__name__}: {exc}"
            with self._lock:
                self.skipped += 1
                first = not self.last_error
                if first:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self._handle = None
            if first:
                if self.on_error is not None:
                    self.on_error(message)
                else:
                    print(f"[{logs.iso()}] {message}", file=sys.stderr, flush=True)

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def __del__(self):  # pragma: no cover - a safety net for an error path
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass

    def report(self) -> dict:
        return {
            "enabled": True,
            "file": TRACE_FILE,
            "records": self.count,
            "bytes": self.bytes,
            "skipped": self.skipped,
            "last_error": self.last_error,
        }

    def __enter__(self) -> "TraceWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_trace(path: str | Path) -> tuple[list[dict], int]:
    """Every readable record, plus the number of malformed lines skipped.

    A killed scrape can leave a half-written last line; skipping it (and
    reporting the count) keeps a relabel run usable instead of failing on a
    torn write.
    """
    records: list[dict] = []
    bad = 0
    with Path(path).open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                bad += 1
    return records, bad


def _where(context: dict) -> str:
    parts = [str(context.get("repo") or "?")]
    if context.get("pr_id") is not None:
        parts[0] = f"{parts[0]}#{context['pr_id']}"
    if context.get("thread_id"):
        parts.append(f"thread={context['thread_id'].rsplit(':', 1)[-1]}")
    if context.get("comment_id") is not None:
        parts.append(f"comment={context['comment_id']}")
    if context.get("role"):
        parts.append(str(context["role"]))
    return " · ".join(parts)


def _labels(labels: dict) -> str:
    order = ("comment_type", "thread_intent", "question", "answered", "phase")
    pieces = []
    for key in order:
        if key not in labels:
            continue
        value = labels[key]
        if isinstance(value, bool):
            value = "yes" if value else "no"
        pieces.append(f"{key}={value}")
    return " ".join(pieces)


def call_line(record: dict) -> str:
    """One operator line for one classifier call (the timestamp is added by the
    reporter, so this stays pure and testable)."""
    context = record.get("context") or {}
    kind = record.get("kind") or "?"
    duration = (record.get("timings") or {}).get("wall_ms")
    took = f"{float(duration) / 1000:.2f}s" if duration is not None else "—"
    backend = record.get("backend") or "?"
    model = record.get("model") or record.get("label_version") or "?"
    where = _where(context)
    digest = str(record.get("text_digest") or "")[:8]
    chars = record.get("text_chars")
    size = f" n={chars}" if chars is not None else ""
    head = (
        f"[classify] {where} · {kind} · {backend}:{model} · {took}"
        f" · text={digest}{size}"
    )
    error = str(record.get("error") or "")
    if error:
        return f"{head} · FAIL {record.get('label_version') or backend} · {error}"
    if record.get("sent") is False and backend != "mock":
        return f"{head} · SKIPPED · {record.get('label_version') or backend}"
    return f"{head} · ok · {_labels(record.get('labels') or {})}"


def batch_line(record: dict) -> str:
    """One operator line for a **batched** call (N texts, one request)."""
    texts = record.get("texts") or []
    first_context = (texts[0].get("context") if texts else None) or {}
    took_ms = (record.get("timings") or {}).get("wall_ms")
    took = f"{float(took_ms) / 1000:.2f}s" if took_ms is not None else "—"
    backend = record.get("backend") or "?"
    model = record.get("model") or record.get("label_version") or "?"
    count = record.get("count") or len(texts)
    where = _where(first_context)
    head = (
        f"[classify] batch {count} · {where} · {record.get('kind') or '?'}"
        f" · {backend}:{model} · {took}"
    )
    error = str(record.get("error") or "")
    if error:
        return f"{head} · FAIL {record.get('label_version') or backend} · {error}"
    labels = record.get("labels") or []
    summary = ", ".join(
        str(label.get("comment_type") or label.get("phase") or "?") for label in labels
    )
    return f"{head} · ok · {summary}"


def iter_trace(path: str | Path) -> Iterator[dict]:
    records, _bad = read_trace(path)
    yield from records


__all__ = [
    "TRACE_FILE",
    "TraceWriter",
    "batch_line",
    "call_line",
    "iter_trace",
    "read_trace",
]

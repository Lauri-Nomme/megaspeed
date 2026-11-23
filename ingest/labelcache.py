"""Digest-keyed, raw-text-free label cache shared by the scrape and relabel (prd/74).

A classifier reads a text once and answers with closed labels; the same text can
show up again in the next run (a re-scrape of an unchanged capture, a relabel of
the same trace) and its label cannot have changed while the model identity did
not. :class:`LabelCache` is the checkpoint that makes that second read free, and
:class:`CachedClassifier` is the wrapper that puts it in front of any
:class:`ingest.classify.Classifier`.

The file holds **no prose** -- only the text digest and the labels::

    {"kind": "comment", "digest": "…", "labels": {…},
     "model": "qwen3:30b-a3b", "think": true, "version": "ollama-v2"}

so it is safe to keep next to the source (raw zone) and safe to hand to the
relabel, exactly like the raw call trace (:mod:`ingest.llmtrace`). A cache hit is
still written to the trace with ``cached: true``: the label is auditable, it just
cost no request. A different model, think flag or classifier version is a miss,
never a silent mix of two classifiers' labels.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Sequence

from . import classify
from .logs import iso as _iso

#: File name inside ``--state-dir``; the relabel keeps its own default (an
#: explicit ``--labels-cache`` can point both tools at the same file).
CACHE_FILE = "classify-cache.jsonl"


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


class CachedClassifier(classify.Classifier):
    """A classifier that answers from :class:`LabelCache` before asking the model.

    ``inner`` is the real backend (the scrape's Ollama box, the relabel's chosen
    model); ``cache`` is built with the **inner's** model / think / version, so an
    entry is reused only by the classifier identity that produced it. Everything
    else is forwarded: ``on_call`` / ``on_batch`` / ``on_progress`` /
    ``on_fallback`` are properties on the inner classifier, and ``backend`` /
    ``version`` / ``model`` / ``url`` / ``workers`` / ``think`` read through, so a
    caller (the scrape's manifest, its startup line, a progress callback) sees
    the backend that actually answers.

    A hit is traced with ``cached: true`` and ``sent: false`` (no request), in
    the same record shape as a real call, so ``classify-trace.jsonl`` remains a
    complete audit trail. The counters in :meth:`report` keep the inner's call
    count and add the cache's read/hit/miss counts beside it.
    """

    def __init__(self, inner: classify.Classifier, cache: LabelCache):
        # The callbacks live on the inner classifier: capture them before the
        # base initialiser clears them through the forwarding properties.
        callbacks = {
            name: getattr(inner, name, None)
            for name in ("on_call", "on_batch", "on_progress", "on_fallback")
        }
        self.inner = inner
        self.cache = cache
        # The scrape asks serially today, but a batch's inner workers can finish
        # concurrently; one lock keeps the file handle and the hit/miss counters
        # safe without reaching into the inner classifier's bookkeeping.
        self._cache_lock = threading.Lock()
        super().__init__()
        for name, value in callbacks.items():
            setattr(self, name, value)
        self.hits = 0
        self.misses = 0

    # -- forwarded identity / callbacks ----------------------------------
    @property
    def backend(self) -> str:  # type: ignore[override]
        return self.inner.backend

    @property
    def version(self) -> str:  # type: ignore[override]
        return self.inner.version

    @property
    def model(self):
        return getattr(self.inner, "model", None)

    @property
    def url(self):
        return getattr(self.inner, "url", None)

    @property
    def workers(self) -> int:
        return int(getattr(self.inner, "workers", 1))

    @property
    def think(self) -> bool:
        return bool(getattr(self.inner, "think", False))

    @property
    def fallback(self):
        return getattr(self.inner, "fallback", None)

    @property
    def on_call(self) -> Callable[[dict], None] | None:
        return getattr(self.inner, "on_call", None)

    @on_call.setter
    def on_call(self, value: Callable[[dict], None] | None) -> None:
        self.inner.on_call = value

    @property
    def on_batch(self) -> Callable[[dict], None] | None:
        return getattr(self.inner, "on_batch", None)

    @on_batch.setter
    def on_batch(self, value: Callable[[dict], None] | None) -> None:
        self.inner.on_batch = value

    @property
    def on_progress(self) -> Callable[[int, int], None] | None:
        return getattr(self.inner, "on_progress", None)

    @on_progress.setter
    def on_progress(self, value: Callable[[int, int], None] | None) -> None:
        self.inner.on_progress = value

    @property
    def on_fallback(self) -> Callable[[Exception], None] | None:
        return getattr(self.inner, "on_fallback", None)

    @on_fallback.setter
    def on_fallback(self, value: Callable[[Exception], None] | None) -> None:
        self.inner.on_fallback = value

    # -- classification --------------------------------------------------
    def classify(self, text: str, kind: str, context: dict | None = None) -> dict:
        labels = self._lookup(kind, text)
        if labels is not None:
            self._hit(text, kind, context, labels)
            return labels
        with self._lock:
            self.misses += 1
        if context is None:
            labels = self.inner.classify(text, kind)
        else:
            labels = self.inner.classify(text, kind, context)
        self._store(kind, text, labels)
        self.last_version = getattr(self.inner, "last_version", None) or self.version
        return labels

    def classify_many(
        self, items: Sequence[tuple]
    ) -> list[tuple[dict, str]]:
        """``(labels, version)`` per item, in input order, from cache or model.

        Every text is looked up first, so the inner backend is asked only about
        the misses -- in **one** call, which keeps its worker pool in play (the
        scrape's throughput comes from there). The hits are traced as they are
        served; the misses are cached after the inner answers them.
        """
        prepared: list[tuple[str, str, dict | None]] = []
        for item in items:
            text, kind = item[0], item[1]
            context = item[2] if len(item) > 2 else None
            prepared.append(
                (str(text), str(kind), dict(context) if context else None)
            )
        results: list[tuple[dict, str] | None] = [None] * len(prepared)
        pending: list[int] = []
        for index, (text, kind, context) in enumerate(prepared):
            labels = self._lookup(kind, text)
            if labels is None:
                pending.append(index)
                continue
            self._hit(text, kind, context, labels)
            results[index] = (labels, self.version)
        if pending:
            with self._lock:
                self.misses += len(pending)
            answers = self.inner.classify_many([prepared[index] for index in pending])
            if len(answers) != len(pending):
                raise classify.ClassifyError(
                    f"backend returned {len(answers)} answer(s) for {len(pending)}"
                    " uncached text(s)"
                )
            for index, (labels, version) in zip(pending, answers):
                text, kind, _context = prepared[index]
                self._store(kind, text, labels)
                results[index] = (labels, version)
            self.last_version = (
                getattr(self.inner, "last_version", None) or self.version
            )
        out: list[tuple[dict, str]] = []
        for answer in results:
            if answer is None:  # pragma: no cover - a backend that lost an answer
                raise classify.ClassifyError(
                    "the label pass missed a text; refusing to return a short batch"
                )
            out.append(answer)
        return out

    # -- cache helpers ---------------------------------------------------
    def _lookup(self, kind: str, text: str) -> dict | None:
        with self._cache_lock:
            labels = self.cache.get(kind, text)
        if labels is None:
            return None
        with self._lock:
            self.hits += 1
            self.last_version = self.version
        return labels

    def _store(self, kind: str, text: str, labels: dict) -> None:
        with self._cache_lock:
            self.cache.put(kind, text, labels)
            self.cache.flush()

    def _hit(self, text: str, kind: str, context: dict | None, labels: dict) -> None:
        """Trace a cache hit in the shape a real call record has (prd/74)."""
        body = str(text)
        record = {
            "ts": _iso(),
            "call": self._next_call(),
            "backend": self.backend,
            "label_version": self.version,
            "model": self.model,
            "url": self.url,
            "kind": kind,
            "context": dict(context or {}),
            "text": body,
            "text_digest": classify.digest(body).hex(),
            "text_chars": len(body),
            "sent": False,
            "labels": dict(labels),
            "ok": True,
            "cached": True,
            "timings": {"wall_ms": 0.0},
        }
        callback = self.on_call
        if callback is not None:
            try:
                callback(record)
            except Exception:  # noqa: BLE001 - tracing must never abort a run
                pass

    def _next_call(self) -> int:
        """The next trace call number, shared with the inner's own counter."""
        inner = self.inner
        lock = getattr(inner, "_lock", None)
        try:
            if lock is not None:
                with lock:
                    inner.attempts = int(getattr(inner, "attempts", 0)) + 1
                    return inner.attempts
            inner.attempts = int(getattr(inner, "attempts", 0)) + 1
            return inner.attempts
        except Exception:  # noqa: BLE001 - a test double without counters
            with self._lock:
                self.attempts += 1
                return self.attempts

    def report(self) -> dict:
        inner_report = (
            self.inner.report() if hasattr(self.inner, "report") else super().report()
        )
        return {
            **inner_report,
            "cache": {
                "path": str(self.cache.path),
                "read": self.cache.read,
                "hits": self.hits,
                "misses": self.misses,
            },
        }


__all__ = ["CACHE_FILE", "CachedClassifier", "LabelCache"]

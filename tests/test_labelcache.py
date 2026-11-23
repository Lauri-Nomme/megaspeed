"""The shared, digest-keyed label cache (prd/74).

One cache, two consumers: ``ingest relabel`` uses it as a resume checkpoint and
the scrape now consults it before asking the model. What these tests pin down:

* a repeat text costs no model call, and the file holds a digest + labels but no
  prose;
* a batch consults the cache per text first, so the inner backend -- and its
  worker pool -- sees only the misses, in input order;
* a different model / think flag / classifier version is a miss, never a mix of
  two classifiers' labels;
* a hit is still an auditable trace record (``cached: true``) that the ordinary
  trace writer and printer can handle;
* a second scrape of an unchanged fixture makes **zero** model calls and emits
  the same labels.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ingest import classify, fixtures, labelcache, llmtrace
from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.progress import ProgressReporter
from ingest.scrape import scrape
from ingest.transport import FixtureTransport

QUIET = ProgressReporter(interval=0)

#: A unique, prose-shaped string: it must never appear in the cache file.
CANARY = "zzcanary this breaks the retry loop on a null payload"


class Counting(classify.MockClassifier):
    """A deterministic classifier that records every real (non-cached) call."""

    backend = "counting"
    version = "counting-v1"
    model = "fake-model"
    think = False

    def __init__(self, *, comment_type: str | None = None):
        super().__init__()
        self.seen: list[str] = []
        self.forced = comment_type

    def _labels_only(self, text, kind):
        labels = super()._labels_only(text, kind)
        if self.forced is not None:
            labels = {**labels, "comment_type": self.forced}
        return labels

    def classify(self, text, kind, context=None):
        self.seen.append(text)
        return super().classify(text, kind, context)


class Fallback(Counting):
    """The inner backend falls back to the mock for one text."""

    def __init__(self):
        super().__init__()
        self.fail = True

    def classify(self, text, kind, context=None):
        labels = super().classify(text, kind, context)
        self.last_version = "mock-v1" if self.fail else self.version
        return labels


class FallbackNotCachedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / labelcache.CACHE_FILE
        self.cache = labelcache.LabelCache(
            self.path, model="fake-model", think=False, version="counting-v1"
        )
        self.inner = Fallback()
        self.wrapper = labelcache.CachedClassifier(self.inner, self.cache)

    def tearDown(self):
        self.cache.close()
        self.tmp.cleanup()

    def test_a_fallback_label_never_enters_the_cache(self):
        self.wrapper.classify(CANARY, classify.KIND_COMMENT)
        self.assertIsNone(self.cache.get(classify.KIND_COMMENT, CANARY))
        self.assertEqual(len(self.cache), 0)

        # Once the backend answers for real, the label is cached and reused.
        self.inner.fail = False
        self.wrapper.classify(CANARY, classify.KIND_COMMENT)
        self.assertIsNotNone(self.cache.get(classify.KIND_COMMENT, CANARY))
        self.wrapper.classify(CANARY, classify.KIND_COMMENT)
        self.assertEqual(self.wrapper.hits, 1)

    def test_a_fallback_batch_answer_is_not_cached(self):
        class BatchFallback(Counting):
            def classify_many(self, items):
                return [
                    (self._labels_only(text, kind), "mock-v1")
                    for text, kind, *_rest in items
                ]

        wrapper = labelcache.CachedClassifier(
            BatchFallback(),
            labelcache.LabelCache(
                self.path, model="fake-model", think=False, version="counting-v1"
            ),
        )
        wrapper.classify_many([(CANARY, classify.KIND_COMMENT)])
        self.assertEqual(len(wrapper.cache), 0)


class LabelCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "state" / labelcache.CACHE_FILE

    def tearDown(self):
        self.tmp.cleanup()

    def _wrapper(self, *, model="fake-model", think=False, version="counting-v1"):
        cache = labelcache.LabelCache(
            self.path, model=model, think=think, version=version
        )
        inner = Counting()
        return labelcache.CachedClassifier(inner, cache), inner, cache

    def test_a_repeat_text_is_one_model_call_and_the_file_has_no_prose(self):
        wrapper, inner, cache = self._wrapper()
        first = wrapper.classify(CANARY, classify.KIND_COMMENT)
        second = wrapper.classify(CANARY, classify.KIND_COMMENT)
        cache.close()

        self.assertEqual(inner.calls, 1)
        self.assertEqual(wrapper.hits, 1)
        self.assertEqual(wrapper.misses, 1)
        self.assertEqual(first, second)

        data = self.path.read_text(encoding="utf-8")
        self.assertIn('"labels"', data)
        self.assertIn(classify.digest(CANARY).hex(), data)
        self.assertNotIn(CANARY, data)
        self.assertNotIn("retry loop", data)

    def test_a_batch_asks_only_about_the_misses_in_input_order(self):
        wrapper, inner, _cache = self._wrapper()
        cached = "this breaks on a null payload"
        cached_labels = wrapper.classify(cached, classify.KIND_COMMENT)
        inner.seen.clear()
        texts = [cached, "why do we retry twice?", "nit: rename this variable"]

        answers = wrapper.classify_many(
            [(text, classify.KIND_COMMENT) for text in texts]
        )

        # The inner backend saw only the two new texts, in input order.
        self.assertEqual(inner.seen, texts[1:])
        self.assertEqual([answer[1] for answer in answers], ["counting-v1"] * 3)
        self.assertEqual(answers[0][0], cached_labels)
        self.assertEqual(
            [answer[0]["comment_type"] for answer in answers],
            ["blocker", "question", "nit"],
        )
        self.assertEqual(wrapper.hits, 1)
        self.assertEqual(wrapper.misses, 3)

    def test_a_changed_identity_is_a_miss_never_a_silent_mix(self):
        first, inner, cache = self._wrapper()
        first.classify(CANARY, classify.KIND_COMMENT)
        cache.close()
        self.assertEqual(inner.calls, 1)

        for overrides in (
            {"model": "other-model"},
            {"think": True},
            {"version": "counting-v2"},
        ):
            with self.subTest(**overrides):
                cache = labelcache.LabelCache(self.path, **{
                    "model": "fake-model", "think": False,
                    "version": "counting-v1", **overrides,
                })
                # The stored entry belongs to another identity: it is ignored.
                self.assertEqual(len(cache), 0)
                inner = Counting(comment_type="nit")
                wrapper = labelcache.CachedClassifier(inner, cache)
                labels = wrapper.classify(CANARY, classify.KIND_COMMENT)
                cache.close()
                self.assertEqual(inner.calls, 1)
                self.assertEqual(labels["comment_type"], "nit")

    def test_a_hit_is_traced_as_cached_and_the_printer_says_so(self):
        wrapper, _inner, cache = self._wrapper()
        records: list[dict] = []
        wrapper.on_call = records.append

        first = wrapper.classify(CANARY, classify.KIND_COMMENT)
        wrapper.classify(CANARY, classify.KIND_COMMENT)
        cache.close()

        self.assertEqual(len(records), 2)
        hit = records[1]
        for key in (
            "ts", "call", "backend", "label_version", "model", "url", "kind",
            "context", "text", "text_digest", "text_chars", "sent", "labels",
            "ok", "cached", "timings",
        ):
            self.assertIn(key, hit)
        self.assertTrue(hit["cached"])
        self.assertTrue(hit["ok"])
        self.assertFalse(hit["sent"])
        self.assertEqual(hit["labels"], first)
        self.assertEqual(hit["text_digest"], classify.digest(CANARY).hex())
        self.assertEqual(hit["timings"], {"wall_ms": 0.0})
        # The call numbers are one monotonic sequence across real and cached.
        self.assertGreater(hit["call"], records[0]["call"])

        line = llmtrace.call_line(hit)
        self.assertIn("cached", line)
        self.assertIn("comment_type=blocker", line)

        # The ordinary trace writer accepts and round-trips the record.
        trace = self.root / "trace.jsonl"
        writer = llmtrace.TraceWriter(trace)
        for record in records:
            writer(record)
        writer.close()
        stored, bad = llmtrace.read_trace(trace)
        self.assertEqual(bad, 0)
        self.assertTrue(stored[1]["cached"])


class ScrapeCacheTests(unittest.TestCase):
    """The acceptance test from prd/74: a second unchanged scrape is free."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=self.root / "store.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform"), ("ENG", "mobile")],
            ticket_projects=("PROJ", "OPS", "ARCH"),
        )
        self.state_dir = self.root / "export" / "cache.state"

    def tearDown(self):
        self.tmp.cleanup()

    def _scrape(self, name: str, source: dict, *, full: bool = False) -> Counting:
        classifier = Counting()
        scrape(
            self.cfg,
            FixtureTransport(source),
            "bitbucket-dc",
            out=self.root / "export" / f"{name}.tgz",
            state_dir=self.state_dir,
            full=full,
            text_classifier=classifier,
            progress=QUIET,
        )
        return classifier

    @staticmethod
    def _labels(path: Path) -> tuple[dict, list]:
        bundle = read_bundle(path)
        threads = {
            obj["object_id"]: {
                key: (obj.get("attributes") or {}).get(key)
                for key in ("comment_type", "thread_outcome", "classifier_version")
            }
            for obj in bundle.objects
            if obj.get("object_type") == "THREAD"
        }
        questions = sorted(
            (event["event_key"], (event.get("attributes") or {}).get("phase"))
            for event in bundle.events
            if event.get("activity") == "question_raised"
        )
        return threads, questions

    def test_a_second_full_scrape_makes_zero_model_calls_and_same_labels(self):
        source = fixtures.generate(n=4, seed=5)
        first = self._scrape("first", source)
        self.assertGreater(first.calls, 0)
        cache_file = self.state_dir / labelcache.CACHE_FILE
        self.assertTrue(cache_file.is_file())
        self.assertNotIn("zzcanary", cache_file.read_text(encoding="utf-8").lower())

        second = self._scrape("second", source, full=True)
        self.assertEqual(second.calls, 0)
        self.assertEqual(second.seen, [])

        first_labels = self._labels(self.root / "export" / "first.tgz")
        second_labels = self._labels(self.root / "export" / "second.tgz")
        self.assertTrue(first_labels[0])
        self.assertEqual(first_labels, second_labels)


if __name__ == "__main__":
    unittest.main()

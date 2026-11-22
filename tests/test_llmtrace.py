"""The raw-zone model trace and its operator lines.

Every classifier call must leave a record that can be replayed (the full input
and output, thinking included) and one progress line that says what was tried
and what happened -- including the full error when it failed.

    python3 -m unittest tests.test_llmtrace -v
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ingest import classify, llmtrace, logs
from ingest.config import Config
from ingest.progress import ProgressReporter


class StampTests(unittest.TestCase):
    def test_the_stamp_is_sortable_utc_with_milliseconds(self):
        moment = datetime(2026, 11, 22, 7, 12, 3, 421900, tzinfo=timezone.utc)
        self.assertEqual(logs.iso(lambda: moment), "2026-11-22T07:12:03.421Z")

    def test_a_naive_time_is_read_as_utc(self):
        moment = datetime(2026, 1, 2, 3, 4, 5, 6000)
        self.assertEqual(logs.iso(lambda: moment), "2026-01-02T03:04:05.006Z")


class ProgressStampTests(unittest.TestCase):
    def test_every_emitted_line_is_stamped(self):
        moment = datetime(2026, 11, 22, 7, 12, 3, tzinfo=timezone.utc)
        stream = io.StringIO()
        reporter = ProgressReporter(stream=stream, interval=0, wall=lambda: moment)
        reporter.emit("[scrape] hello")
        self.assertEqual(stream.getvalue(), "[2026-11-22T07:12:03.000Z] [scrape] hello\n")


class TraceWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / llmtrace.TRACE_FILE

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_json_line_per_record_with_counts(self):
        writer = llmtrace.TraceWriter(self.path)
        writer({"call": 1, "text": "nit: rename this", "labels": {"comment_type": "nit"}})
        writer.close()
        writer({"call": 2, "text": "why two loops?"})
        writer.close()

        records, bad = llmtrace.read_trace(self.path)
        self.assertEqual(bad, 0)
        self.assertEqual([r["call"] for r in records], [1, 2])
        self.assertEqual(records[0]["labels"]["comment_type"], "nit")

    def test_a_torn_last_line_is_skipped_and_counted(self):
        writer = llmtrace.TraceWriter(self.path)
        writer({"call": 1})
        writer.close()
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write('{"call": 2, "text": "half')
        records, bad = llmtrace.read_trace(self.path)
        self.assertEqual([r["call"] for r in records], [1])
        self.assertEqual(bad, 1)

    def test_a_write_failure_is_reported_once_and_never_aborts_the_run(self):
        target = Path(self.tmp.name) / "a-directory"
        target.mkdir()
        writer = llmtrace.TraceWriter(target)          # a directory, not a file
        seen: list[str] = []
        writer.on_error = seen.append
        writer({"call": 1})
        writer({"call": 2})
        self.assertEqual(len(seen), 1)
        self.assertEqual(writer.count, 0)
        self.assertGreaterEqual(writer.skipped, 1)
        self.assertIn("IsADirectoryError", writer.report()["last_error"])

    def test_an_enormous_text_is_truncated_but_the_call_survives(self):
        writer = llmtrace.TraceWriter(self.path)
        writer({
            "call": 1,
            "text": "x" * (llmtrace.MAX_LINE_BYTES + 100),
            "labels": {"comment_type": "nit"},
        })
        writer.close()
        record = llmtrace.read_trace(self.path)[0][0]
        self.assertLess(len(record["text"]), llmtrace.MAX_LINE_BYTES)
        self.assertIn("truncated", record["text"])
        self.assertEqual(record["labels"]["comment_type"], "nit")


class CallLineTests(unittest.TestCase):
    def record(self, **overrides) -> dict:
        base = {
            "backend": "ollama",
            "model": "qwen3:8b",
            "kind": "comment",
            "context": {
                "repo": "DEMO/widget", "pr_id": 11096,
                "thread_id": "THREAD:DEMO/widget#11096:123", "comment_id": 123,
                "role": "opener",
            },
            "text_digest": "abcdef0123456789",
            "text_chars": 27,
            "timings": {"wall_ms": 2310.0},
            "labels": {"comment_type": "nit", "question": False, "phase": "review"},
            "label_version": "ollama-v1",
            "sent": True,
            "error": "",
        }
        base.update(overrides)
        return base

    def test_a_good_call_names_the_label_and_the_place(self):
        line = llmtrace.call_line(self.record())
        self.assertIn("[classify] DEMO/widget#11096", line)
        self.assertIn("thread=123", line)
        self.assertIn("comment=123", line)
        self.assertIn("ollama:qwen3:8b", line)
        self.assertIn("2.31s", line)
        self.assertIn("text=abcdef01 n=27", line)
        self.assertIn("comment_type=nit", line)
        self.assertIn("ok", line)

    def test_a_failure_carries_the_full_error(self):
        detail = "urllib.error.URLError: <urlopen error [Errno 111] Connection refused>"
        line = llmtrace.call_line(
            self.record(error=detail, label_version="mock-v1", labels={"comment_type": "nit"})
        )
        self.assertIn("FAIL mock-v1", line)
        self.assertIn(detail, line)

    def test_a_breaker_skip_is_not_reported_as_success(self):
        line = llmtrace.call_line(
            self.record(
                sent=False,
                error="circuit breaker open after 5 consecutive failure(s)",
                label_version="mock-v1",
            )
        )
        self.assertIn("FAIL mock-v1", line)
        self.assertIn("circuit breaker open", line)

    def test_a_mock_answer_is_not_reported_as_skipped(self):
        line = llmtrace.call_line(
            self.record(backend="mock", label_version="mock-v1", model=None, sent=False)
        )
        self.assertNotIn("SKIPPED", line)
        self.assertIn("ok", line)


class ScrapeTraceWiringTests(unittest.TestCase):
    """What a live-shaped scrape writes: one trace file, one line per call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=self.root / "store.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform")],
            ticket_projects=("PROJ", "OPS", "ARCH"),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _scrape(self, source: str, stream: io.StringIO | None = None) -> dict:
        from ingest import fixtures
        from ingest.scrape import scrape
        from ingest.transport import FixtureTransport

        return scrape(
            self.cfg,
            FixtureTransport(fixtures.generate(n=3, seed=5)),
            source,
            out=self.root / "export" / f"{source}.tgz",
            state_dir=self.root / f"{source}.state",
            text_classifier=classify.MockClassifier(),
            progress=ProgressReporter(stream=stream or io.StringIO(), interval=1),
        )

    def test_a_live_shaped_scrape_writes_the_trace_and_names_it_in_the_manifest(self):
        from ingest.bundle import read_bundle

        report = self._scrape("bitbucket-dc")
        trace = self.root / "bitbucket-dc.state" / llmtrace.TRACE_FILE
        self.assertTrue(trace.is_file())
        records, bad = llmtrace.read_trace(trace)
        self.assertEqual(bad, 0)
        self.assertGreater(len(records), 0)
        self.assertEqual(report["text_trace"]["records"], len(records))
        # The bundle records that the trace exists, never a path or its content.
        manifest = read_bundle(self.root / "export" / "bitbucket-dc.tgz").manifest
        self.assertEqual(manifest["text_trace"]["records"], len(records))
        self.assertEqual(manifest["text_trace"]["file"], llmtrace.TRACE_FILE)
        self.assertNotIn(str(self.root), json.dumps(manifest["text_trace"]))

    def test_every_call_prints_a_timestamped_line_with_ids_and_outcome(self):
        stream = io.StringIO()
        self._scrape("bitbucket-dc", stream)
        lines = [line for line in stream.getvalue().splitlines() if "[classify]" in line]
        self.assertGreater(len(lines), 0)
        self.assertTrue(all(line.startswith("[20") and "Z] [classify]" in line for line in lines))
        self.assertIn("ENG/platform#", lines[0])
        openers = [line for line in lines if "thread=" in line]
        self.assertTrue(openers, "an opener call must name its thread")
        self.assertIn("comment=", openers[0])
        self.assertIn("text=", openers[0])
        self.assertIn("ok", openers[0])

    def test_a_fixture_scrape_writes_no_trace(self):
        self._scrape("fixture")
        self.assertFalse((self.root / "fixture.state" / llmtrace.TRACE_FILE).exists())


class ClassifierTraceTests(unittest.TestCase):
    """The classifier builds the record: request, response, thinking, error."""

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def close(self):
            pass

    def test_an_empty_answer_from_a_thinking_model_is_retried_without_thinking(self):
        good = json.dumps({
            "comment_type": "blocker", "thread_intent": "unclear",
            "question": False, "answered": False, "phase": "review",
        })
        payloads = [
            {"message": {"content": "", "thinking": "deliberation used the budget"},
             "done_reason": "length"},
            {"message": {"content": good}, "done_reason": "stop"},
        ]
        calls: list[dict] = []

        def opener(request, timeout):
            calls.append(json.loads(request.data))
            return self.Response(payloads.pop(0))

        classifier = classify.OllamaClassifier(
            "http://llm.invalid", "qwen3:30b-a3b", opener=opener
        )
        labels = classifier.classify("this drops the tenant filter", classify.KIND_COMMENT)
        self.assertEqual(labels["comment_type"], "blocker")
        self.assertEqual(classifier.fallbacks, 0)
        self.assertEqual(classifier.retries, 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["think"])
        self.assertFalse(calls[1]["think"])

    def test_a_batch_request_labels_n_texts_in_one_call(self):
        answers = [
            {"comment_type": "nit", "thread_intent": "unclear",
             "question": False, "answered": False, "phase": "review"},
            {"comment_type": "blocker", "thread_intent": "acted",
             "question": False, "answered": False, "phase": "review"},
        ]
        calls: list[dict] = []

        def opener(request, timeout):
            calls.append(json.loads(request.data))
            return self.Response({
                "message": {"content": json.dumps(answers),
                            "thinking": "two remarks"},
                "done_reason": "stop",
            })

        classifier = classify.OllamaClassifier(
            "http://llm.invalid", "qwen3:8b", opener=opener
        )
        traced: list[dict] = []
        classifier.on_batch = traced.append
        results = classifier.classify_batch([
            ("nit: rename this", classify.KIND_COMMENT, {"pr_id": 1}),
            ("this crashes on null", classify.KIND_COMMENT, {"pr_id": 2}),
        ])
        self.assertEqual([labels["comment_type"] for labels, _v in results], ["nit", "blocker"])
        self.assertEqual(len(calls), 1)
        body = calls[0]
        self.assertEqual(body["format"]["type"], "array")
        self.assertEqual(body["format"]["minItems"], 2)
        self.assertEqual(body["format"]["maxItems"], 2)
        self.assertIn("JSON array", body["messages"][0]["content"])
        self.assertEqual(classifier.report()["batches"], 1)
        self.assertEqual(classifier.report()["calls"], 2)
        self.assertEqual(len(traced), 1)
        self.assertTrue(traced[0]["batch"])
        self.assertEqual(traced[0]["thinking"], "two remarks")
        self.assertEqual(
            [item["context"] for item in traced[0]["texts"]],
            [{"pr_id": 1}, {"pr_id": 2}],
        )
        self.assertEqual(len(traced[0]["labels"]), 2)

    def test_a_short_batch_answer_is_rejected_for_the_per_text_fallback(self):
        def opener(request, timeout):
            return self.Response({
                "message": {"content": json.dumps([{
                    "comment_type": "nit", "thread_intent": "unclear",
                    "question": False, "answered": False, "phase": "review",
                }])},
                "done_reason": "stop",
            })

        classifier = classify.OllamaClassifier("http://llm.invalid", opener=opener)
        with self.assertRaises(classify.ClassifyError):
            classifier.classify_batch([
                ("a", classify.KIND_COMMENT), ("b", classify.KIND_COMMENT),
            ])

    def test_a_batch_of_mixed_kinds_is_refused_before_a_request(self):
        classifier = classify.OllamaClassifier("http://llm.invalid")
        with self.assertRaises(classify.ClassifyError):
            classifier.classify_batch([
                ("a", classify.KIND_COMMENT), ("b", classify.KIND_DESCRIPTION),
            ])

    def test_a_good_answer_traces_the_exact_request_response_and_thinking(self):
        payload = {
            "message": {
                "content": json.dumps({
                    "comment_type": "nit", "thread_intent": "acted",
                    "question": False, "answered": False, "phase": "review",
                }),
                "thinking": "the word nit is a style remark",
            },
            "total_duration": 2_300_000_000,
            "eval_count": 31,
            "done_reason": "stop",
        }

        class Response:
            def read(self):
                return json.dumps(payload).encode()

            def close(self):
                pass

        classifier = classify.OllamaClassifier(
            "http://llm.invalid", "qwen3:8b", opener=lambda request, timeout: Response()
        )
        seen: list[dict] = []
        classifier.on_call = seen.append
        context = {"repo": "DEMO/widget", "pr_id": 7, "thread_id": "THREAD:DEMO/widget#7:1"}
        labels = classifier.classify("nit: rename this", classify.KIND_COMMENT, context)

        self.assertEqual(labels["comment_type"], "nit")
        self.assertEqual(len(seen), 1)
        record = seen[0]
        self.assertEqual(record["context"], context)
        self.assertEqual(record["text"], "nit: rename this")
        self.assertTrue(record["ok"])
        self.assertEqual(record["labels"], labels)
        self.assertEqual(record["label_version"], "ollama-v1")
        self.assertTrue(record["request"]["think"])
        self.assertEqual(
            record["request"]["format"], classify.schema_for(classify.KIND_COMMENT)
        )
        self.assertEqual(record["thinking"], "the word nit is a style remark")
        self.assertEqual(json.loads(record["content"])["comment_type"], "nit")
        self.assertEqual(record["timings"]["eval_count"], 31)
        self.assertIsNotNone(record["timings"]["wall_ms"])
        self.assertFalse(record["fallback"])

    def test_a_failure_traces_the_request_and_the_full_error(self):
        def opener(request, timeout):
            raise OSError("[Errno 111] Connection refused")

        classifier = classify.OllamaClassifier(
            "http://llm.invalid", "qwen3:8b", opener=opener, max_failures=1
        )
        seen: list[dict] = []
        classifier.on_call = seen.append
        labels = classifier.classify("nit: rename this", classify.KIND_COMMENT)

        self.assertEqual(labels["comment_type"], "nit")  # deterministic fallback
        record = seen[0]
        self.assertFalse(record["ok"])
        self.assertIn("OSError", record["error"])
        self.assertIn("Connection refused", record["error"])
        self.assertTrue(record["fallback"])
        self.assertEqual(record["label_version"], "mock-v1")
        self.assertTrue(record["sent"])
        self.assertEqual(record["request"]["model"], "qwen3:8b")

    def test_a_breaker_skip_is_traced_without_a_request(self):
        def opener(request, timeout):
            raise OSError("down")

        classifier = classify.OllamaClassifier(
            "http://llm.invalid", opener=opener, max_failures=1
        )
        seen: list[dict] = []
        classifier.on_call = seen.append
        classifier.classify("first", classify.KIND_COMMENT)   # opens the breaker
        classifier.classify("second", classify.KIND_COMMENT)  # never sent
        self.assertEqual(len(seen), 2)
        skipped = seen[1]
        self.assertFalse(skipped["sent"])
        self.assertTrue(skipped["fallback"])
        self.assertIn("circuit breaker open", skipped["error"])
        self.assertIsNone(skipped["request"])

    def test_an_explicitly_disabled_think_is_pinned(self):
        classifier = classify.OllamaClassifier("http://llm.invalid", think=False)
        self.assertFalse(classifier.request_body("x", classify.KIND_COMMENT)["think"])
        self.assertFalse(classifier.report()["think"])

    def test_classify_many_passes_each_item_context(self):
        class Recording(classify.MockClassifier):
            def __init__(self):
                super().__init__()
                self.contexts = []

            def classify(self, text, kind, context=None):
                self.contexts.append(context)
                return super().classify(text, kind, context)

        classifier = Recording()
        classifier.classify_many([
            ("a", classify.KIND_COMMENT, {"pr_id": 1}),
            ("b", classify.KIND_COMMENT),
        ])
        self.assertEqual(classifier.contexts, [{"pr_id": 1}, None])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

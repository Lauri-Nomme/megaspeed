"""Local-LLM classification: closed labels, mock-first, no live model (prd/16).

The feature is: read a raw comment body / PR description **scrape-side**, project
it onto a closed vocabulary, and emit only the labels. These tests pin all of it
without a live LLM:

* the **mock** backend is deterministic and covers every ``comment_type`` /
  ``thread_outcome`` / question case;
* a **contract test** asserts both backends share one schema (the Ollama backend
  literally sends :func:`ingest.classify.schema_for` as its ``format``), and that
  an answer outside the vocabulary is refused by the parser;
* the Ollama backend is exercised with an injected opener (canned response,
  garbage, connection refused) so its request shape and its fallback path are
  covered offline;
* ``--verify`` accepts the new vocabulary and rejects a tampered label;
* the reducer emits ``comment_type`` + ``thread_outcome`` on THREAD objects and
  ``question_raised`` / ``question_answered`` events with a ``phase``;
* the label crosses the tgz and no raw text ever does;
* the git cache answers "did the diff move at the comment anchor?".

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from ingest import classify, fixtures, taxonomy
from ingest.bitbucket import PullRequest
from ingest.config import Config, load_config
from ingest.gitclass import PushClassifier
from ingest.importer import import_bundle
from ingest.pipeline import KeyFilter, Reducer, Stats, run
from ingest.progress import ProgressReporter
from ingest.pseudonym import IdentityTable
from ingest.scrape import scrape
from ingest.store import connect, verify
from ingest.transport import FixtureTransport

AS_OF = datetime(2026, 11, 15, 12, 0, 0)
T0 = 1_790_000_000_000  # epoch ms

ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}
GRACE = {"name": "grace", "displayName": "Grace Hopper", "type": "NORMAL"}


def _config(root: Path) -> Config:
    return Config(
        db_path=root / "store.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[("ENG", "platform")],
    )


def _activity(activity_id: int, action: str, offset: int, user: dict | None = None, **extra) -> dict:
    payload = {"id": activity_id, "createdDate": T0 + offset * 60_000, "action": action, **extra}
    if user is not None:
        payload["user"] = user
    return payload


def _pr(description: str = "PROJ-1: split the retry budget.", activities: list[dict] | None = None,
        commits: list[dict] | None = None) -> PullRequest:
    pr = {
        "id": 1,
        "title": "PROJ-1: retry budget",
        "description": description,
        "state": "OPEN",
        "createdDate": T0,
        "updatedDate": T0 + 600_000,
        "fromRef": {"latestCommit": "a" * 40, "displayId": "feature/PROJ-1-retry"},
        "toRef": {"latestCommit": "b" * 40, "displayId": "main"},
        "author": {"user": ADA},
        "properties": {},
    }
    pull = PullRequest("ENG", "platform", pr, commits or [])
    pull.activities = activities or []
    return pull


def _comment(comment_id: int, text: str, author: dict = GRACE, **extra) -> dict:
    return {
        "id": comment_id,
        "text": text,
        "author": author,
        "severity": "NORMAL",
        "state": "OPEN",
        "threadResolved": False,
        "comments": [],
        **extra,
    }


# --------------------------------------------------------------------------- #
# the mock backend: deterministic labels for every case
# --------------------------------------------------------------------------- #
COMMENT_CASES = [
    ("blocker", "this crashes on a null payload -- please fix before merge"),
    ("blocker", "the migration is not backwards compatible, broken for old rows"),
    ("suggestion", "maybe extract this into a small helper"),
    ("question", "why do we retry twice here?"),
    ("question", "should we keep the old endpoint around?"),
    ("nit", "nit: rename this variable"),
    ("nit", "typo in the docstring, trailing whitespace on line 4"),
    ("out_of_scope", "unrelated cleanup, belongs in another ticket"),
    ("out_of_scope", "not part of this PR, follow-up ticket please"),
    ("optimization", "this does a linear scan per row, consider caching"),
    ("optimization", "memory usage grows with the batch size"),
]


class MockCommentTypeTests(unittest.TestCase):
    def setUp(self):
        self.mock = classify.MockClassifier()

    def test_every_comment_type_case_is_deterministic(self):
        for expected, text in COMMENT_CASES:
            with self.subTest(expected=expected, text=text):
                labels = self.mock.classify(text, classify.KIND_COMMENT)
                self.assertEqual(labels["comment_type"], expected)
                self.assertIn(labels["comment_type"], taxonomy.COMMENT_TYPE)
                # and it is a projection, not a paraphrase: only closed values.
                self.assertEqual(set(labels), set(classify.labels_for(classify.KIND_COMMENT)))

    def test_the_same_text_always_yields_the_same_labels(self):
        text = "this will break the parser -- must not merge"
        first = classify.MockClassifier().classify(text, classify.KIND_COMMENT)
        second = classify.MockClassifier().classify(text, classify.KIND_COMMENT)
        self.assertEqual(first, second)

    def test_a_bare_comment_is_the_residual_suggestion(self):
        labels = self.mock.classify("I moved the constant up a little", classify.KIND_COMMENT)
        self.assertEqual(labels["comment_type"], "suggestion")

    def test_only_bitbucket_realistic_phases_are_emitted(self):
        comment = self.mock.classify("why?", classify.KIND_COMMENT)
        description = self.mock.classify("PROJ-1: why does the queue back off?", classify.KIND_DESCRIPTION)
        self.assertEqual(comment["phase"], "review")
        self.assertEqual(description["phase"], "implementation")
        for labels in (comment, description):
            self.assertIn(labels["phase"], classify.PHASES_BY_KIND[classify.KIND_COMMENT])

    def test_a_question_raises_and_an_answer_answers(self):
        question = self.mock.classify("why is the retry budget split?", classify.KIND_COMMENT)
        answer = self.mock.classify("good catch, fixed in the next push", classify.KIND_COMMENT)
        self.assertTrue(question["question"])
        self.assertFalse(question["answered"])
        self.assertFalse(answer["question"])
        self.assertTrue(answer["answered"])

    def test_thread_intent_reads_wont_fix_withdrawn_and_fixed(self):
        cases = [
            ("won't fix, this is by design", "rejected"),
            ("nevermind, my bad", "withdrawn"),
            ("fixed and pushed", "acted"),
            ("could you reorder these imports", "unclear"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    self.mock.classify(text, classify.KIND_COMMENT)["thread_intent"], expected
                )

    def test_a_description_never_carries_a_comment_type(self):
        labels = self.mock.classify("PROJ-1 plan, test plan included.", classify.KIND_DESCRIPTION)
        self.assertEqual(set(labels), set(classify.labels_for(classify.KIND_DESCRIPTION)))
        self.assertNotIn("comment_type", labels)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(classify.ClassifyError):
            self.mock.classify("hello", "pull_request_title")


class ClassifierProgressTests(unittest.TestCase):
    """A long model batch must report progress inside one PR (not only per PR)."""

    def test_the_mock_reports_each_text(self):
        seen: list[tuple[int, int]] = []
        classifier = classify.MockClassifier()
        classifier.on_progress = lambda done, total: seen.append((done, total))
        classifier.classify_many([("nit: rename", classify.KIND_COMMENT),
                                  ("why?", classify.KIND_COMMENT)])
        self.assertEqual(seen, [(1, 2), (2, 2)])

    def test_a_broken_progress_callback_never_aborts_the_batch(self):
        classifier = classify.MockClassifier()

        def boom(done, total):
            raise RuntimeError("reporter failed")

        classifier.on_progress = boom
        self.assertEqual(len(classifier.classify_many([("nit", classify.KIND_COMMENT)])), 1)


class ThreadOutcomeTests(unittest.TestCase):
    def test_an_open_thread_is_unresolved(self):
        self.assertEqual(
            classify.thread_outcome(resolved=False, anchor_changed=True), "unresolved"
        )

    def test_a_moved_diff_is_acted_on(self):
        self.assertEqual(
            classify.thread_outcome(resolved=True, anchor_changed=True), "acted_on"
        )

    def test_the_commenter_resolving_their_own_comment_is_a_walk_back(self):
        self.assertEqual(
            classify.thread_outcome(
                resolved=True, anchor_changed=False, resolver_is_commenter=True
            ),
            "walked_back",
        )

    def test_resolved_without_a_change_is_rejected(self):
        self.assertEqual(
            classify.thread_outcome(resolved=True, anchor_changed=False), "rejected"
        )

    def test_an_unknown_diff_falls_back_to_the_classifier_intent(self):
        # No git cache / no anchor path: the only remaining signal is the text.
        self.assertEqual(classify.thread_outcome(resolved=True, thread_intent="rejected"), "rejected")
        self.assertEqual(classify.thread_outcome(resolved=True, thread_intent="withdrawn"), "walked_back")
        self.assertEqual(classify.thread_outcome(resolved=True, thread_intent="acted"), "acted_on")
        # DES-3: an unknown outcome is unknown. Booking it as acted_on would
        # under-count the comment tax (the flattering direction).
        self.assertEqual(classify.thread_outcome(resolved=True), "unresolved")
        self.assertEqual(
            classify.thread_outcome(resolved=True, thread_intent="unclear"), "unresolved"
        )
        # a value the parser never agreed to cannot leak into a label
        self.assertIn(
            classify.thread_outcome(resolved=True, thread_intent="looks fixed to me"),
            taxonomy.THREAD_OUTCOME,
        )

    def test_every_answer_is_in_the_closed_set(self):
        for resolved in (True, False):
            for changed in (True, False, None):
                for commenter in (True, False):
                    label = classify.thread_outcome(
                        resolved=resolved, anchor_changed=changed, resolver_is_commenter=commenter
                    )
                    self.assertIn(label, taxonomy.THREAD_OUTCOME)


# --------------------------------------------------------------------------- #
# backends share one schema (the contract)
# --------------------------------------------------------------------------- #
class SchemaContractTests(unittest.TestCase):
    def setUp(self):
        self.mock = classify.MockClassifier()
        self.ollama = classify.OllamaClassifier()

    def test_both_backends_speak_the_same_schema(self):
        texts = {
            classify.KIND_COMMENT: ["nit: rename this", "why?", "fixed, thanks"],
            classify.KIND_DESCRIPTION: ["PROJ-1 plan. Should we split the budget?"],
        }
        for kind, samples in texts.items():
            schema = classify.schema_for(kind)
            self.assertEqual(set(schema["required"]), set(classify.labels_for(kind)))
            for backend in (self.mock, self.ollama):
                for text in samples:
                    labels = backend.classify(text, kind) if backend is self.mock else None
                    if labels is None:  # no network in tests: check the schema side
                        self.assertEqual(
                            set(self.ollama.request_body(text, kind)["format"]["required"]),
                            set(schema["required"]),
                        )
                        continue
                    self.assertEqual(set(labels), set(schema["required"]))
                    for key, value in labels.items():
                        spec = schema["properties"][key]
                        if spec["type"] == "boolean":
                            self.assertIsInstance(value, bool)
                        else:
                            self.assertIn(value, spec["enum"], (kind, key, value))

    def test_the_ollama_request_is_constrained_and_cold(self):
        body = self.ollama.request_body("nit: rename this", classify.KIND_COMMENT)
        self.assertEqual(body["format"], classify.schema_for(classify.KIND_COMMENT))
        self.assertEqual(body["options"]["temperature"], 0)
        self.assertFalse(body["stream"])
        self.assertFalse(body["think"])
        self.assertEqual(body["model"], classify.DEFAULT_MODEL)
        self.assertEqual(
            [message["role"] for message in body["messages"]], ["system", "user"]
        )

    def test_the_prompt_names_every_label_of_the_closed_set(self):
        prompt = classify.system_prompt(classify.KIND_COMMENT)
        for label in taxonomy.COMMENT_TYPE:
            self.assertIn(label, prompt)
        for intent in taxonomy.THREAD_INTENT:
            self.assertIn(intent, prompt)

    def test_the_parser_refuses_anything_outside_the_vocabulary(self):
        good = {
            "comment_type": "nit",
            "thread_intent": "acted",
            "question": False,
            "answered": False,
            "phase": "review",
        }
        self.assertEqual(classify.parse_labels(classify.KIND_COMMENT, good)["comment_type"], "nit")
        for bad, value in [
            ("comment_type", "nitpick"),
            ("thread_intent", "maybe later"),
            ("phase", "standup"),
            ("question", "yes please"),
        ]:
            with self.subTest(field=bad):
                with self.assertRaises(classify.ClassifyError):
                    classify.parse_labels(classify.KIND_COMMENT, {**good, bad: value})

    def test_a_phase_the_kind_cannot_know_is_clamped_not_stored(self):
        # ``sprint`` is in the closed vocabulary but a Bitbucket comment cannot
        # prove it: the honest answer is the kind's default, never a guess.
        labels = classify.parse_labels(
            classify.KIND_COMMENT,
            {
                "comment_type": "question",
                "thread_intent": "unclear",
                "question": True,
                "answered": False,
                "phase": "sprint",
            },
        )
        self.assertEqual(labels["phase"], "review")

    def test_the_parser_refuses_prose_and_missing_keys(self):
        with self.assertRaises(classify.ClassifyError):
            classify.parse_labels(classify.KIND_COMMENT, "this looks like a nice change")
        with self.assertRaises(classify.ClassifyError):
            classify.parse_labels(classify.KIND_COMMENT, {"comment_type": "nit"})
        with self.assertRaises(classify.ClassifyError):
            classify.parse_labels(
                classify.KIND_COMMENT,
                {
                    "comment_type": "nit",
                    "thread_intent": "acted",
                    "question": False,
                    "answered": False,
                    "phase": "review",
                    "summary": "the reviewer wants a rename",
                },
            )


class OllamaOfflineTests(unittest.TestCase):
    """The real backend, without a real model: request shape + fallback path."""

    def _classifier(self, payload, *, error=None):
        def opener(request, timeout):
            self.request = request
            self.timeout = timeout
            if error is not None:
                raise error

            class Response:
                def read(self):
                    return payload if isinstance(payload, bytes) else json.dumps(payload).encode()

                def close(self):
                    pass

            return Response()

        return classify.OllamaClassifier(
            "http://llm.example.invalid:11434", "test-model", timeout=5, opener=opener
        )

    def test_a_parallel_batch_reports_each_completion(self):
        classifier = self._classifier({
            "message": {"content": json.dumps({
                "comment_type": "blocker", "thread_intent": "acted",
                "question": False, "answered": False, "phase": "review",
            })}
        })
        seen: list[tuple[int, int]] = []
        classifier.on_progress = lambda done, total: seen.append((done, total))
        items = [("one", classify.KIND_COMMENT), ("two", classify.KIND_COMMENT),
                 ("three", classify.KIND_COMMENT)]
        self.assertEqual(len(classifier.classify_many(items)), 3)
        self.assertEqual(sorted(done for done, _total in seen), [1, 2, 3])
        self.assertTrue(all(total == 3 for _done, total in seen))

    def test_a_valid_answer_is_parsed_into_labels(self):
        classifier = self._classifier(
            {
                "message": {
                    "content": json.dumps(
                        {
                            "comment_type": "blocker",
                            "thread_intent": "acted",
                            "question": False,
                            "answered": False,
                            "phase": "review",
                        }
                    )
                }
            }
        )
        labels = classifier.classify("this drops the tenant filter", classify.KIND_COMMENT)
        self.assertEqual(labels["comment_type"], "blocker")
        self.assertEqual(classifier.fallbacks, 0)
        self.assertEqual(self.request.full_url, "http://llm.example.invalid:11434/api/chat")
        self.assertEqual(self.timeout, 5)
        body = json.loads(self.request.data)
        self.assertEqual(body["format"], classify.schema_for(classify.KIND_COMMENT))
        self.assertFalse(body["think"])

    def test_garbage_degrades_to_the_mock_and_is_counted(self):
        classifier = self._classifier({"message": {"content": "I think this is a nit."}})
        labels = classifier.classify("nit: rename this", classify.KIND_COMMENT)
        self.assertEqual(labels["comment_type"], "nit")  # from the mock fallback
        self.assertEqual(classifier.fallbacks, 1)

    def test_a_dead_endpoint_never_raises_into_the_scrape(self):
        classifier = self._classifier(b"", error=OSError("connection refused"))
        labels = classifier.classify("why twice?", classify.KIND_COMMENT)
        self.assertEqual(labels["comment_type"], "question")
        self.assertEqual(classifier.fallbacks, 1)
        self.assertEqual(labels["phase"], "review")

    def test_a_dead_endpoint_stops_being_asked_after_a_few_failures(self):
        """A hanging box must not cost one timeout per comment for hours."""
        calls = []

        def opener(request, timeout):
            calls.append(request)
            raise OSError("connection refused")

        classifier = classify.OllamaClassifier("http://llm.invalid", opener=opener, max_failures=3)
        seen: list[str] = []
        classifier.on_fallback = lambda exc: seen.append(str(exc))
        for _ in range(10):
            labels = classifier.classify("nit: rename", classify.KIND_COMMENT)
            self.assertEqual(labels["comment_type"], "nit")
        self.assertEqual(len(calls), 3, "the breaker must stop the requests")
        self.assertTrue(classifier.down)
        self.assertEqual(classifier.fallbacks, 10)
        # The first failure is surfaced once and recorded: a 100%-fallback run
        # can no longer look like a working one at the end.
        self.assertEqual(len(seen), 1)
        self.assertIn("connection refused", classifier.last_error)
        report = classifier.report()
        self.assertTrue(report["down"])
        self.assertEqual(report["url"], "http://llm.invalid")
        self.assertIn("connection refused", report["last_error"])

    def test_a_good_answer_leaves_no_error(self):
        classifier = self._classifier({"message": {"content": json.dumps({
            "comment_type": "nit", "thread_intent": "acted",
            "question": False, "answered": False, "phase": "review",
        })}})
        seen: list[str] = []
        classifier.on_fallback = lambda exc: seen.append(str(exc))
        labels = classifier.classify("nit: rename", classify.KIND_COMMENT)
        self.assertEqual(labels["comment_type"], "nit")
        self.assertEqual(classifier.fallbacks, 0)
        self.assertEqual(classifier.last_error, "")
        self.assertEqual(seen, [])

    def test_the_report_records_backend_model_and_counts(self):
        classifier = self._classifier({"message": {"content": "{}"}})
        classifier.classify("anything", classify.KIND_COMMENT)
        report = classifier.report()
        self.assertEqual(report["backend"], "ollama")
        self.assertEqual(report["version"], "ollama-v1")
        self.assertEqual(report["model"], "test-model")
        self.assertEqual(report["fallbacks"], 1)
        self.assertEqual(report["calls"], 1)


class ClassificationConcurrencyTests(unittest.TestCase):
    """prd/33 §7: classification used to be strictly serial (one in flight).

    The Ollama client is the measured bottleneck (4.4 h for 8k comments at 2 s),
    so a PR's texts are classified by a **bounded** pool. These tests pin the
    three properties that make that safe: the bound is the configured worker
    count, the labels are identical to a serial run (order included), and the
    deterministic mock path is untouched (synchronous, in the caller's thread).
    """

    def _server(self, *, answer=None):
        """A local stub /api/chat that records concurrency and honours a delay."""
        import threading
        import time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        payload = json.dumps({"message": {"content": json.dumps(answer or {
            "comment_type": "suggestion", "thread_intent": "acted",
            "question": False, "answered": False, "phase": "review",
        })}}).encode()
        state = {"open": 0, "max": 0, "requests": 0}
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                with lock:
                    state["open"] += 1
                    state["max"] = max(state["max"], state["open"])
                    state["requests"] += 1
                self.rfile.read(int(self.headers.get("content-length") or 0))
                time.sleep(0.02)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                with lock:
                    state["open"] -= 1

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", state

    @staticmethod
    def _items(count: int) -> list[tuple[str, str]]:
        return [(f"comment {index}: could you rename this?", classify.KIND_COMMENT)
                for index in range(count)]

    def test_the_in_flight_requests_are_bounded_by_the_worker_count(self):
        url, state = self._server()
        classifier = classify.OllamaClassifier(url, "stub", timeout=10, workers=3)
        classifier.classify_many(self._items(12))
        self.assertGreater(state["max"], 1, "the batch was serial")
        self.assertLessEqual(state["max"], 3, state)
        self.assertEqual(state["requests"], 12)

    def test_a_single_worker_restores_the_serial_client(self):
        url, state = self._server()
        classifier = classify.OllamaClassifier(url, "stub", timeout=10, workers=1)
        classifier.classify_many(self._items(6))
        self.assertEqual(state["max"], 1)
        self.assertEqual(state["requests"], 6)

    def test_the_batch_output_is_identical_to_a_serial_run(self):
        url, _state = self._server()
        items = self._items(9) + [
            ("PROJ-1: does this change the retry budget?", classify.KIND_DESCRIPTION),
        ]
        serial = classify.OllamaClassifier(url, "stub", timeout=10, workers=1)
        pooled = classify.OllamaClassifier(url, "stub", timeout=10, workers=4)
        self.assertEqual(serial.classify_many(items), pooled.classify_many(items))
        # ... and the per-item answers are the ones a plain ``classify`` gives.
        self.assertEqual(
            [labels for labels, _version in pooled.classify_many(items)],
            [serial.classify(text, kind) for text, kind in items],
        )
        self.assertEqual(pooled.calls, len(items) * 2)

    def test_a_failing_backend_still_degrades_to_the_mock(self):
        def opener(request, timeout):
            raise OSError("connection refused")

        classifier = classify.OllamaClassifier(
            "http://llm.invalid:11434", opener=opener, workers=4, max_failures=3
        )
        answers = classifier.classify_many(self._items(6))
        self.assertEqual(len(answers), 6)
        for labels, version in answers:
            self.assertEqual(version, "mock-v1")
            self.assertEqual(labels["phase"], "review")
        self.assertTrue(classifier.down)
        self.assertEqual(classifier.fallbacks, 6)

    def test_the_mock_backend_stays_synchronous_in_the_calling_thread(self):
        import threading

        mock_classifier = classify.MockClassifier()
        threads: list[int] = []
        original = mock_classifier.classify

        def recording(text, kind):
            threads.append(threading.get_ident())
            return original(text, kind)

        mock_classifier.classify = recording
        items = self._items(8)
        answers = mock_classifier.classify_many(items)
        self.assertEqual(len(answers), len(items))
        self.assertEqual(set(threads), {threading.get_ident()})
        self.assertEqual(
            [labels for labels, _version in answers],
            [classify.MockClassifier().classify(text, kind) for text, kind in items],
        )

    def test_the_worker_count_is_configurable(self):
        with mock.patch.dict(os.environ, {"MEGASPEED_LLM_WORKERS": "2"}):
            cfg = load_config(None)
        self.assertEqual(cfg.llm_workers, 2)
        self.assertEqual(
            classify.for_scrape(cfg, "bitbucket-dc").workers, 2
        )
        cfg.llm_workers = 1
        self.assertEqual(classify.for_scrape(cfg, "bitbucket-dc").workers, 1)
        # The deterministic backends have no pool at all.
        self.assertIsInstance(classify.for_scrape(cfg, "fixture"), classify.MockClassifier)
        # The operator can set it per run as well.
        from ingest.scrape_cli import build_parser
        self.assertEqual(build_parser().parse_args(["--llm-workers", "3"]).llm_workers, 3)


class ReducerBatchTests(unittest.TestCase):
    """The reducer asks for one PR's texts in **one** batch, in source order."""

    class Recording(classify.MockClassifier):
        backend = "recording-mock"

        def __init__(self):
            super().__init__()
            self.batches: list[list[tuple[str, str]]] = []

        def classify_many(self, items):
            self.batches.append(list(items))
            return super().classify_many(items)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _config(self.root)
        self.classifier = self.Recording()
        self.reducer = Reducer(
            self.cfg,
            IdentityTable(self.cfg.identity_path),
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape, self.cfg.ticket_projects),
            AS_OF,
            classifier=self.classifier,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _reduce(self, pr: PullRequest) -> Stats:
        stats = Stats()
        self.reducer.reduce(pr, ["PROJ-1"], pr.activities, stats)
        return stats

    def test_one_pr_is_one_batch_holding_the_description_and_its_comments(self):
        pr = _pr(
            description="PROJ-1: why two retries?",
            activities=[
                _activity(1, "OPENED", 0, ADA),
                _activity(2, "COMMENTED", 5, GRACE,
                          comment=_comment(101, "nit: rename this variable"),
                          commentAction="ADDED"),
                _activity(3, "COMMENTED", 6, GRACE,
                          comment=_comment(102, "this crashes on a null payload"),
                          commentAction="ADDED"),
            ],
        )
        self._reduce(pr)
        self.assertEqual(len(self.classifier.batches), 1)
        self.assertEqual(
            [kind for _text, kind in self.classifier.batches[0]],
            [classify.KIND_DESCRIPTION, classify.KIND_COMMENT, classify.KIND_COMMENT],
        )
        self.assertEqual(
            [text for text, _kind in self.classifier.batches[0]],
            ["PROJ-1: why two retries?", "nit: rename this variable",
             "this crashes on a null payload"],
        )

    def test_a_batched_label_is_the_one_the_thread_gets(self):
        pr = _pr(activities=[
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE,
                      comment=_comment(101, "nit: rename this variable"),
                      commentAction="ADDED"),
        ])
        stats = self._reduce(pr)
        thread = next(obj for obj in stats.objects if obj["object_type"] == "THREAD")
        self.assertEqual(thread["attributes"]["comment_type"], "nit")
        self.assertEqual(thread["attributes"]["classifier_version"], "mock-v1")

    def test_bot_notices_and_edits_are_never_sent_to_the_model(self):
        bot = {"name": "ci-bot", "displayName": "CI", "type": "SERVICE"}
        pr = _pr(description="", activities=[
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, bot,
                      comment=_comment(101, "build failed"), commentAction="ADDED"),
            _activity(3, "COMMENTED", 6, GRACE,
                      comment=_comment(102, "nit: rename this"), commentAction="UPDATED"),
            _activity(4, "COMMENTED", 7, GRACE,
                      comment=_comment(103, "typo in the docstring"),
                      commentAction="ADDED"),
        ])
        stats = self._reduce(pr)
        texts = [text for batch in self.classifier.batches for text, _kind in batch]
        self.assertEqual(texts, ["typo in the docstring"])
        self.assertEqual(stats.bot_comments_skipped, 1)

    def test_a_text_repeated_inside_one_pr_costs_one_request(self):
        pr = _pr(description="", activities=[
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE,
                      comment=_comment(101, "nit: rename this"), commentAction="ADDED"),
            _activity(3, "COMMENTED", 6, ADA,
                      comment=_comment(102, "nit: rename this"), commentAction="ADDED"),
        ])
        self._reduce(pr)
        self.assertEqual(len(self.classifier.batches[0]), 1)

    def test_the_batch_is_dropped_after_the_pr(self):
        pr = _pr(activities=[_activity(1, "OPENED", 0, ADA)])
        self._reduce(pr)
        self.assertEqual(self.reducer._labels, {})


class ConstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _config(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fixture_uses_the_mock_and_disabled_uses_nothing(self):
        self.assertIsInstance(classify.for_scrape(self.cfg, "fixture"), classify.MockClassifier)
        self.cfg.text_classify = False
        self.assertIsNone(classify.for_scrape(self.cfg, "fixture"))
        self.assertIsNone(classify.for_scrape(self.cfg, "bitbucket-dc"))

    def test_a_live_run_uses_ollama_with_the_configured_endpoint(self):
        self.cfg.llm_url = "http://llm.example.invalid:11434/"
        self.cfg.llm_model = "some-model"
        classifier = classify.for_scrape(self.cfg, "bitbucket-dc")
        self.assertIsInstance(classifier, classify.OllamaClassifier)
        self.assertEqual(classifier.url, "http://llm.example.invalid:11434")
        self.assertEqual(classifier.model, "some-model")

    def test_the_backend_can_be_forced_to_the_mock(self):
        self.cfg.text_backend = "mock"
        self.assertIsInstance(classify.for_scrape(self.cfg, "bitbucket-dc"), classify.MockClassifier)


class LLMUrlValidationTests(unittest.TestCase):
    """SEC-3: a mistyped endpoint must fail at startup, not ship prose off-box."""

    def test_valid_http_and_https_urls_pass(self):
        for url in (
            "http://127.0.0.1:11434",
            "http://10.43.36.10:11434",
            "https://llm.example.invalid:11434",
        ):
            with self.subTest(url=url):
                self.assertEqual(classify.validate_llm_url(url), url)

    def test_malformed_urls_are_rejected(self):
        for url in (
            "",
            "not-a-url",
            "ftp://llm.example.invalid",
            "file:///etc/passwd",
            "http://",
            "http://user:secret@llm.example.invalid:11434",
            "http://llm.example.invalid:notaport",
            "http://llm.example.invalid:11434?exfiltrate=1",
        ):
            with self.subTest(url=url):
                with self.assertRaises(classify.ClassifyError):
                    classify.validate_llm_url(url)

    def test_the_ollama_backend_refuses_a_malformed_url(self):
        with self.assertRaises(classify.ClassifyError):
            classify.OllamaClassifier("ftp://llm.example.invalid")

    def test_load_config_rejects_a_malformed_env_url(self):
        with mock.patch.dict(os.environ, {"MEGASPEED_LLM_URL": "http://user@evil.invalid:11434"}):
            with self.assertRaises(SystemExit):
                load_config()


# --------------------------------------------------------------------------- #
# vocabulary + --verify
# --------------------------------------------------------------------------- #
class VocabularyTests(unittest.TestCase):
    def test_the_vocabularies_are_closed_sets(self):
        self.assertEqual(
            taxonomy.COMMENT_TYPE,
            frozenset({"blocker", "suggestion", "question", "nit", "out_of_scope", "optimization"}),
        )
        self.assertEqual(
            taxonomy.THREAD_OUTCOME,
            frozenset({"acted_on", "rejected", "walked_back", "unresolved"}),
        )
        self.assertEqual(
            taxonomy.QUESTION_PHASE,
            frozenset({"breakdown", "refinement", "sprint", "implementation", "review"}),
        )

    def test_object_attributes_accept_the_labels_and_reject_prose(self):
        payload = {
            "comment_type": "out_of_scope",
            "thread_outcome": "walked_back",
            "classifier_version": "ollama-v1",
        }
        self.assertEqual(taxonomy.check_object_attributes(payload), payload)
        for bad in (
            {"comment_type": "nitpick"},
            {"thread_outcome": "fixed"},
            {"classifier_version": "qwen said this was a nit"},
        ):
            with self.assertRaises(taxonomy.TaxonomyError):
                taxonomy.check_object_attributes(bad)

    def test_question_events_accept_the_phase_and_answer_source(self):
        payload = {"phase": "refinement", "answer_source": "resolution"}
        self.assertEqual(taxonomy.check_attributes(payload), payload)
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.check_attributes({"answer_source": "by email"})


class VerifyVocabularyTests(unittest.TestCase):
    """``--verify`` must accept the new vocabulary and catch a tampered label."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.cfg = _config(root)
        cls.report = run(cls.cfg, FixtureTransport(fixtures.generate(n=12, seed=11)), source="fixture")
        assert cls.report["verified"], cls.report["problems"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.con = connect(self.cfg.db_path)
        self.con.execute("BEGIN")

    def tearDown(self):
        self.con.execute("ROLLBACK")
        self.con.close()

    def _thread(self):
        row = self.con.execute(
            "SELECT object_id FROM objects WHERE object_type = 'THREAD'"
            " AND attributes->>'comment_type' IS NOT NULL LIMIT 1"
        ).fetchone()
        if row is None:
            self.skipTest("fixture produced no labelled thread")
        return row[0]

    def test_a_labelled_store_passes(self):
        self.assertTrue(verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)["ok"])

    def test_every_comment_type_is_accepted(self):
        thread = self._thread()
        for value in sorted(taxonomy.COMMENT_TYPE):
            self.con.execute(
                "UPDATE objects SET attributes = json_merge_patch(attributes, ?) WHERE object_id = ?",
                [json.dumps({"comment_type": value}), thread],
            )
            report = verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)
            self.assertTrue(report["ok"], (value, report["problems"]))

    def test_every_thread_outcome_is_accepted(self):
        thread = self._thread()
        for value in sorted(taxonomy.THREAD_OUTCOME):
            self.con.execute(
                "UPDATE objects SET attributes = json_merge_patch(attributes, ?) WHERE object_id = ?",
                [json.dumps({"thread_outcome": value}), thread],
            )
            self.assertTrue(verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)["ok"])

    def test_a_tampered_comment_type_is_rejected(self):
        thread = self._thread()
        self.con.execute(
            "UPDATE objects SET attributes = json_merge_patch(attributes, ?) WHERE object_id = ?",
            [json.dumps({"comment_type": "this is basically a nit"}), thread],
        )
        report = verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)
        self.assertFalse(report["ok"])
        self.assertTrue(any("comment_type" in p or "whitespace" in p for p in report["problems"]),
                        report["problems"])

    def test_a_tampered_thread_outcome_is_rejected(self):
        thread = self._thread()
        self.con.execute(
            "UPDATE objects SET attributes = json_merge_patch(attributes, ?) WHERE object_id = ?",
            [json.dumps({"thread_outcome": "addressed eventually"}), thread],
        )
        report = verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)
        self.assertFalse(report["ok"])
        self.assertTrue(any("thread_outcome" in p or "whitespace" in p for p in report["problems"]),
                        report["problems"])

    def test_a_prose_classifier_version_is_rejected(self):
        thread = self._thread()
        self.con.execute(
            "UPDATE objects SET attributes = json_merge_patch(attributes, ?) WHERE object_id = ?",
            [json.dumps({"classifier_version": "qwen says nit"}), thread],
        )
        self.assertFalse(verify(self.con, self.cfg.key_shape, self.cfg.ticket_projects)["ok"])


# --------------------------------------------------------------------------- #
# the reducer: labels + question events
# --------------------------------------------------------------------------- #
class ReducerLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _config(self.root)
        self.reducer = Reducer(
            self.cfg,
            IdentityTable(self.cfg.identity_path),
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape, self.cfg.ticket_projects),
            AS_OF,
            classifier=classify.MockClassifier(),
        )
        self.stats = Stats()

    def tearDown(self):
        self.tmp.cleanup()

    def reduce(self, pr: PullRequest) -> list[dict]:
        self.reducer.reduce(pr, ["PROJ-1"], pr.activities, self.stats)
        return self.stats.events

    def thread(self, comment_id: int) -> dict:
        return next(
            o for o in self.stats.objects
            if o["object_type"] == "THREAD" and o["object_id"].endswith(f":{comment_id}")
        )

    def test_a_thread_carries_comment_type_and_outcome(self):
        pr = _pr(activities=[
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE,
                      comment=_comment(101, "nit: rename this variable"),
                      commentAction="ADDED", commentAnchor={"path": "src/x.py", "line": 3}),
        ])
        self.reduce(pr)
        thread = self.thread(101)
        self.assertEqual(thread["attributes"]["comment_type"], "nit")
        self.assertEqual(thread["attributes"]["thread_outcome"], "unresolved")
        self.assertEqual(thread["attributes"]["classifier_version"], "mock-v1")
        self.assertEqual(self.stats.comment_types, {"nit": 1})
        self.assertEqual(self.stats.thread_outcomes, {"unresolved": 1})

    def test_a_mock_fallback_is_stamped_mock_v1_not_ollama_v1(self):
        """FR-3: a per-call degrade must not claim the model produced the label."""
        def opener(request, timeout):
            raise OSError("connection refused")

        classifier = classify.OllamaClassifier(
            "http://llm.invalid:11434", opener=opener, max_failures=1
        )
        reducer = Reducer(
            self.cfg,
            IdentityTable(self.cfg.identity_path),
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape, self.cfg.ticket_projects),
            AS_OF,
            classifier=classifier,
        )
        pr = _pr(activities=[
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE,
                      comment=_comment(101, "nit: rename this variable"),
                      commentAction="ADDED"),
        ])
        stats = Stats()
        reducer.reduce(pr, ["PROJ-1"], pr.activities, stats)
        thread = next(
            obj for obj in stats.objects
            if obj["object_type"] == "THREAD" and obj["object_id"].endswith(":101")
        )
        self.assertEqual(thread["attributes"]["classifier_version"], "mock-v1")
        self.assertEqual(classifier.fallbacks, classifier.calls)

    def test_dc_blocker_severity_outranks_the_text_label(self):
        # The prose is a suggestion; DC's own BLOCKER marker wins (deterministic
        # where the source provides it, prd/02).
        pr = _pr(activities=[
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE,
                      comment=_comment(101, "maybe move this", severity="BLOCKER"),
                      commentAction="ADDED"),
        ])
        self.reduce(pr)
        self.assertEqual(self.thread(101)["attributes"]["comment_type"], "blocker")

    def test_the_anchor_diff_decides_the_outcome(self):
        for changed, expected in ((True, "acted_on"), (False, "rejected")):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = _config(root)
                stats = Stats()
                reducer = Reducer(
                    cfg,
                    IdentityTable(cfg.identity_path),
                    KeyFilter.build(cfg.ticket_regex, cfg.key_shape, cfg.ticket_projects),
                    AS_OF,
                    classifier=classify.MockClassifier(),
                )
                activity = _activity(
                    2, "COMMENTED", 5, GRACE,
                    comment=_comment(101, "please rename this", state="RESOLVED",
                                     threadResolved=True, resolvedDate=T0 + 30_000,
                                     resolver=ADA),
                    commentAction="ADDED", commentAnchor={"path": "src/x.py", "line": 3},
                )
                activity["megaspeedThreadChange"] = {"changed": changed}
                pull = _pr(activities=[_activity(1, "OPENED", 0, ADA), activity])
                reducer.reduce(pull, ["PROJ-1"], pull.activities, stats)
                thread = next(o for o in stats.objects if o["object_type"] == "THREAD")
                self.assertEqual(thread["attributes"]["thread_outcome"], expected)

    def test_a_thread_the_commenter_withdrew_is_a_walk_back(self):
        activity = _activity(
            2, "COMMENTED", 5, GRACE,
            comment=_comment(101, "nit: rename this", state="RESOLVED", threadResolved=True,
                             resolvedDate=T0 + 30_000, resolver=GRACE),
            commentAction="ADDED", commentAnchor={"path": "src/x.py", "line": 3},
        )
        activity["megaspeedThreadChange"] = {"changed": False}
        self.reduce(_pr(activities=[_activity(1, "OPENED", 0, ADA), activity]))
        self.assertEqual(self.thread(101)["attributes"]["thread_outcome"], "walked_back")

    def test_a_question_thread_raises_and_gets_answered_by_its_reply(self):
        comment = _comment(101, "why do we retry twice here?", comments=[
            {"id": 102, "text": "good catch, fixed in the next push", "author": ADA,
             "createdDate": T0 + 6 * 60_000},
        ])
        pr = _pr(activities=[
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE, comment=comment, commentAction="ADDED"),
        ])
        events = self.reduce(pr)
        raised = [e for e in events if e["activity"] == "question_raised"]
        answered = [e for e in events if e["activity"] == "question_answered"]
        self.assertEqual(len(raised), 1)
        self.assertEqual(len(answered), 1)
        self.assertEqual(raised[0]["attributes"]["phase"], "review")
        self.assertEqual(answered[0]["attributes"]["phase"], "review")
        self.assertEqual(answered[0]["attributes"]["answer_source"], "reply")
        self.assertEqual(answered[0]["actor"], "AdLo")
        self.assertEqual(self.stats.questions_raised, 1)
        self.assertEqual(self.stats.questions_answered, 1)
        self.assertEqual(self.thread(101)["attributes"]["comment_type"], "question")

    def test_a_resolved_question_is_answered_by_the_resolution(self):
        comment = _comment(101, "why do we retry twice here?", state="RESOLVED",
                           threadResolved=True, resolvedDate=T0 + 30 * 60_000, resolver=ADA)
        pr = _pr(activities=[
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE, comment=comment, commentAction="ADDED"),
        ])
        events = self.reduce(pr)
        answered = [e for e in events if e["activity"] == "question_answered"]
        self.assertEqual(len(answered), 1)
        self.assertEqual(answered[0]["attributes"]["answer_source"], "resolution")
        self.assertEqual(answered[0]["actor"], "AdLo")

    def test_a_description_question_is_raised_in_implementation(self):
        events = self.reduce(_pr(
            description="PROJ-1: split the retry budget. Should it be per queue?",
            activities=[_activity(1, "OPENED", 0, ADA)],
        ))
        raised = [e for e in events if e["activity"] == "question_raised"]
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0]["attributes"]["phase"], "implementation")
        self.assertEqual(raised[0]["actor"], "AdLo")
        self.assertEqual(self.stats.questions_raised, 1)

    def test_no_classifier_means_no_text_labels_and_no_question_events(self):
        reducer = Reducer(
            self.cfg,
            IdentityTable(self.cfg.identity_path),
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape, self.cfg.ticket_projects),
            AS_OF,
        )
        stats = Stats()
        pr = _pr(description="Should we split it?", activities=[
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, GRACE,
                      comment=_comment(101, "why?"), commentAction="ADDED"),
        ])
        reducer.reduce(pr, ["PROJ-1"], pr.activities, stats)
        self.assertFalse([e for e in stats.events if e["activity"].startswith("question_")])
        thread = next(o for o in stats.objects if o["object_type"] == "THREAD")
        self.assertNotIn("comment_type", thread["attributes"])
        self.assertEqual(stats.questions_raised, 0)

    def test_a_bot_notice_is_never_labelled(self):
        """A stale-PR auto-decline is not a review remark (deterministic, prd/02)."""
        bot = {"name": "bitbucket.system-user", "displayName": "Bitbucket", "type": "SERVICE"}
        pr = _pr(activities=[
            _activity(1, "OPENED", 0, ADA),
            _activity(2, "COMMENTED", 5, bot,
                      comment=_comment(101, "inactive for 5 weeks, declined", author=bot),
                      commentAction="ADDED"),
        ])
        self.reduce(pr)
        self.assertNotIn("comment_type", self.thread(101)["attributes"])
        self.assertEqual(self.stats.bot_comments_skipped, 1)
        self.assertEqual(self.stats.comment_types, {})

    def test_the_raw_comment_body_never_reaches_an_event_or_object(self):
        pr = _pr(description="PROJ-1: zzcanary description text",
                 activities=[
                     _activity(1, "OPENED", 0, ADA),
                     _activity(2, "COMMENTED", 5, GRACE,
                               comment=_comment(101, "zzcanary why are we doing this?"),
                               commentAction="ADDED"),
                 ])
        self.reduce(pr)
        payload = json.dumps({"events": self.stats.events, "objects": self.stats.objects}, default=str)
        self.assertNotIn("zzcanary", payload.lower())


# --------------------------------------------------------------------------- #
# the anchor diff (git cache)
# --------------------------------------------------------------------------- #
def _git(cwd: Path, *args: str) -> str:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cwd),
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": "dev@example.invalid",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": "dev@example.invalid",
        "GIT_AUTHOR_DATE": "2026-11-01T10:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-11-01T10:00:00+00:00",
        "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env
    )
    return result.stdout.decode().strip()


class AnchorDiffTests(unittest.TestCase):
    """One local ``git diff`` per thread: did the code at the anchor change?"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        work = cls.root / "work"
        work.mkdir()
        _git(work, "init", "-q", "-b", "main")
        (work / "f.txt").write_text("one\ntwo\n")
        (work / "other.txt").write_text("notes\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "base")
        cls.base = _git(work, "rev-parse", "HEAD")
        _git(work, "checkout", "-q", "-b", "feature")
        (work / "f.txt").write_text("one\nTWO\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "feature")
        cls.old = _git(work, "rev-parse", "HEAD")
        (work / "f.txt").write_text("one\nTWO\nthree\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "address review")
        cls.new = _git(work, "rev-parse", "HEAD")
        # the pre-push head has to stay reachable on the server (like DC)
        _git(work, "update-ref", "refs/keep/old", cls.old)
        cls.served = cls.root / "served" / "scm" / "ENG" / "platform.git"
        cls.served.parent.mkdir(parents=True)
        _git(work, "clone", "-q", "--bare", str(work), str(cls.served))
        _git(cls.served, "config", "uploadpack.allowAnySHA1InWant", "true")
        cls.classifier = PushClassifier(
            cls.root / "cache",
            f"file://{cls.root / 'served'}",
            url_template="{base}/scm/{project}/{repo}.git",
        )
        cls.pr_record = {
            "fromRef": {"latestCommit": cls.new, "displayId": "feature"},
            "toRef": {"latestCommit": cls.base, "displayId": "main"},
        }
        cls.commits = [
            {"id": cls.old, "authorTimestamp": 1_790_000_000_000},
            {"id": cls.new, "authorTimestamp": 1_790_000_600_000},
        ]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def annotate(self, anchor_path: str, ts: int = 1_790_000_300_000) -> dict:
        activity = {
            "id": 2,
            "action": "COMMENTED",
            "createdDate": ts,
            "comment": {"id": 101},
            "commentAnchor": {"path": anchor_path, "line": 1},
        }
        self.classifier.annotate_threads(
            "ENG", "platform", self.pr_record, [activity], self.commits
        )
        return activity

    def test_a_change_on_the_anchor_path_is_stamped_true(self):
        activity = self.annotate("f.txt")
        self.assertEqual(activity["megaspeedThreadChange"], {"changed": True})

    def test_an_untouched_path_is_stamped_false(self):
        activity = self.annotate("other.txt")
        self.assertEqual(activity["megaspeedThreadChange"], {"changed": False})

    def test_no_anchor_path_is_not_guessed(self):
        activity = {
            "id": 3, "action": "COMMENTED", "createdDate": 1_790_000_300_000,
            "comment": {"id": 102},
        }
        self.classifier.annotate_threads(
            "ENG", "platform", self.pr_record, [activity], self.commits
        )
        self.assertNotIn("megaspeedThreadChange", activity)

    def test_an_existing_stamp_is_never_overwritten(self):
        activity = {
            "id": 4, "action": "COMMENTED", "createdDate": 1_790_000_300_000,
            "comment": {"id": 103},
            "commentAnchor": {"path": "other.txt"},
            "megaspeedThreadChange": {"changed": True},
        }
        self.classifier.annotate_threads(
            "ENG", "platform", self.pr_record, [activity], self.commits
        )
        self.assertEqual(activity["megaspeedThreadChange"], {"changed": True})

    def test_the_head_timeline_pins_a_comment_to_the_code_of_its_moment(self):
        timeline = PushClassifier._head_timeline(self.pr_record, [], self.commits)
        self.assertEqual(PushClassifier._head_at(timeline, 1_790_000_300_000), self.old)
        self.assertEqual(PushClassifier._head_after(timeline, 1_790_000_300_000), self.new)

    def test_no_git_cache_degrades_without_raising(self):
        classifier = PushClassifier(self.root / "unused", "")
        classifier.git = ""
        activity = {"id": 5, "action": "COMMENTED", "createdDate": 1_790_000_300_000,
                    "comment": {"id": 104}, "commentAnchor": {"path": "f.txt"}}
        classifier.annotate_threads("ENG", "platform", self.pr_record, [activity], self.commits)
        self.assertNotIn("megaspeedThreadChange", activity)
        self.assertEqual(classifier.thread_counts["git_unavailable"], 1)


# --------------------------------------------------------------------------- #
# the scrape: labels cross, text does not
# --------------------------------------------------------------------------- #
class ScrapeBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _config(self.root)
        self.out = self.root / "export" / "fixture.tgz"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_run_says_what_it_is_doing_before_the_first_request(self):
        # prd/68 follow-up: a 30B text classifier can spend minutes on the first
        # PR; the start line names the source, scope and model immediately.
        # Fixture runs stay silent (tests pin an empty stderr); use the hans
        # source with the deterministic classifier.
        stream = io.StringIO()
        scrape(
            self.cfg,
            FixtureTransport(fixtures.generate(n=3, seed=11)),
            "hans",
            out=self.out,
            state_dir=self.root / "export" / "state-line",
            progress=ProgressReporter(stream=stream, interval=30),
            text_classifier=classify.MockClassifier(),
        )
        text = stream.getvalue()
        self.assertIn("[scrape] hans", text)
        self.assertIn("text mock:mock-v1", text)

    def test_labels_cross_the_bundle_and_the_store_verifies(self):
        report = scrape(
            self.cfg,
            FixtureTransport(fixtures.generate(n=12, seed=11)),
            "fixture",
            out=self.out,
            state_dir=self.root / "export" / "state",
        )
        text = report["text_classify"]
        self.assertTrue(text["enabled"])
        self.assertEqual(text["backend"], "mock")
        self.assertEqual(text["version"], "mock-v1")
        self.assertGreater(text["calls"], 0)

        from ingest.bundle import read_bundle

        bundle = read_bundle(self.out)
        self.assertEqual(bundle.manifest["text_classifier"]["backend"], "mock")
        threads = [o for o in bundle.objects if o["object_type"] == "THREAD"]
        self.assertTrue(threads)
        for thread in threads:
            self.assertIn(thread["attributes"]["comment_type"], taxonomy.COMMENT_TYPE)
            self.assertIn(thread["attributes"]["thread_outcome"], taxonomy.THREAD_OUTCOME)
            self.assertEqual(thread["attributes"]["classifier_version"], "mock-v1")

        import_bundle(self.cfg, self.out)
        con = connect(self.cfg.db_path, read_only=True)
        try:
            payload = verify(con, self.cfg.key_shape, self.cfg.ticket_projects)
            self.assertTrue(payload["ok"], payload["problems"])
            stored = con.execute(
                "SELECT count(*) FROM objects WHERE object_type = 'THREAD'"
                " AND attributes->>'comment_type' IS NOT NULL"
            ).fetchone()[0]
            self.assertGreater(stored, 0)
        finally:
            con.close()

        # no raw text anywhere in the tgz or the store
        self.assertNotIn("zzcanary", self.out.read_bytes().decode("latin-1").lower())

    def test_a_scrape_without_a_classifier_emits_no_text_labels(self):
        self.cfg.text_classify = False
        report = scrape(
            self.cfg,
            FixtureTransport(fixtures.generate(n=6, seed=11)),
            "fixture",
            out=self.out,
            state_dir=self.root / "export" / "state",
        )
        self.assertEqual(report["text_classify"], {"enabled": False})
        from ingest.bundle import read_bundle

        bundle = read_bundle(self.out)
        self.assertEqual(bundle.manifest["text_classifier"], {"enabled": False})
        for thread in (o for o in bundle.objects if o["object_type"] == "THREAD"):
            self.assertNotIn("comment_type", thread["attributes"])

    def test_an_explicit_classifier_is_used_for_a_live_shaped_scrape(self):
        class Recording(classify.MockClassifier):
            texts = 0

            def classify(self, text, kind):
                Recording.texts += 1
                return super().classify(text, kind)

        report = scrape(
            self.cfg,
            FixtureTransport(fixtures.generate(n=4, seed=3)),
            "bitbucket-dc",
            out=self.out,
            state_dir=self.root / "export" / "state",
            text_classifier=Recording(),
        )
        self.assertGreater(Recording.texts, 0)
        self.assertEqual(report["text_classify"]["calls"], Recording.texts)


if __name__ == "__main__":
    unittest.main()

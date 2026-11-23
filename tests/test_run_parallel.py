"""The parallel runner's scheduler (stdlib only, no worker processes here)."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "run_parallel", Path(__file__).with_name("run_parallel.py")
)
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


class PlanTests(unittest.TestCase):
    def test_round_robin_covers_every_test_exactly_once(self):
        ids = [f"m.C.test_{index}" for index in range(17)]
        chunks = runner._plan(ids, 4, None, "test")
        self.assertEqual(len(chunks), 4)
        flat = [name for chunk in chunks for name in chunk]
        self.assertEqual(sorted(flat), sorted(ids))
        self.assertEqual(len(flat), len(set(flat)))

    def test_round_robin_never_returns_an_empty_bucket(self):
        ids = [f"t{index}" for index in range(3)]
        chunks = runner._plan(ids, 8, None, "test")
        self.assertEqual(len(chunks), 3)

    def test_timings_pack_the_slowest_tests_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "times.json"
            path.write_text(
                json.dumps([[9.0, "a.B"], [1.0, "b.B"], [1.0, "c.B"]]), encoding="utf-8"
            )
            chunks = runner._plan(["a.B", "b.B", "c.B"], 2, str(path), "module")
        self.assertIn(["a.B"], chunks)
        self.assertIn(["b.B", "c.B"], chunks)

    def test_a_broken_times_file_falls_back_to_round_robin(self):
        ids = [f"t{index}" for index in range(5)]
        chunks = runner._plan(ids, 2, "/nonexistent/times.json", "test")
        self.assertEqual(sorted(name for c in chunks for name in c), sorted(ids))


if __name__ == "__main__":
    unittest.main()

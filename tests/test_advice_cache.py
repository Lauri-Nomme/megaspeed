"""The policy-lab disk cache (perf: a restart must not re-simulate)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import app as app_module


class AdviceDiskCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = self.root / "t.duckdb"
        self.store.write_bytes(b"store")
        self.old_dir = app_module.TENANTS_DIR
        app_module.TENANTS_DIR = self.root
        app_module._advice_cache.clear()
        self.computes = 0

        def fake(*args, **kwargs):
            self.computes += 1
            return {"available": True, "scenarios": {"s": {"x": 1}}}

        self.patcher = mock.patch.object(
            app_module, "_compute_advice", side_effect=fake
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        app_module.TENANTS_DIR = self.old_dir
        app_module._advice_cache.clear()
        self.tmp.cleanup()

    def call(self, window=(0, 1)):
        return app_module._advice_payload(
            None, "t", self.store, None, None, window, None
        )

    def files(self):
        return sorted((self.root / ".cache" / "advice").glob("*.json"))

    def test_a_restart_reads_the_lab_from_disk(self):
        first = self.call()
        self.assertEqual(self.computes, 1)
        self.assertEqual(len(self.files()), 1)

        # A new process: the memory cache is empty, the file is not.
        app_module._advice_cache.clear()
        second = self.call()
        self.assertEqual(self.computes, 1, "the second process re-simulated")
        self.assertEqual(first, second)

    def test_a_different_window_is_a_different_file(self):
        self.call((0, 1))
        app_module._advice_cache.clear()
        self.call((0, 2))
        self.assertEqual(self.computes, 2)
        self.assertEqual(len(self.files()), 2)

    def test_a_changed_store_is_a_new_key(self):
        self.call()
        app_module._advice_cache.clear()
        self.store.write_bytes(b"store v2")
        self.call()
        self.assertEqual(self.computes, 2)

    def test_a_corrupt_file_is_ignored(self):
        self.call()
        for path in self.files():
            path.write_text("{ not json", encoding="utf-8")
        app_module._advice_cache.clear()
        self.call()
        self.assertEqual(self.computes, 2)
        self.assertEqual(json.loads(self.files()[0].read_text()), {
            "available": True, "scenarios": {"s": {"x": 1}}
        })

    def test_a_stated_no_result_is_not_persisted(self):
        self.patcher.stop()
        self.patcher = mock.patch.object(
            app_module, "_compute_advice",
            side_effect=lambda *a, **k: {"available": False, "note": "no model"},
        )
        self.patcher.start()
        self.call()
        app_module._advice_cache.clear()
        self.call()
        self.assertEqual(self.files(), [])


if __name__ == "__main__":
    unittest.main()

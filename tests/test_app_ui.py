"""UI-level regression tests for the browser app (``app/*.js``).

The app is vanilla ES modules with no build step and no framework, so the parts
that decide what a label says (ticket keys), what the scope banner admits
(per-view filtering) and how a queue row is classified (role vs person) are
pure functions in ``app/tickets.js`` and ``app/scope.js``. Node's test runner
exercises them directly, plus a few source-level guards for the wiring that
only exists in the page (the honesty-line guard, the level selects, the
newest-first event paging and the "load the full range" affordance).

Node is optional in the deployment image, so the test skips when it is absent
rather than failing the suite:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_TESTS = ROOT / "app" / "test"


class AppUiTests(unittest.TestCase):
    def test_app_unit_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed; app/*.js unit tests need it")
        files = sorted(str(path) for path in APP_TESTS.glob("*.test.mjs"))
        self.assertTrue(files, f"no app tests found in {APP_TESTS}")
        proc = subprocess.run(
            [node, "--disable-warning=MODULE_TYPELESS_PACKAGE_JSON", "--test", *files],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # A silently skipped suite would also exit 0; require the pass count.
        self.assertIn("fail 0", proc.stdout)
        self.assertNotIn("pass 0", proc.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

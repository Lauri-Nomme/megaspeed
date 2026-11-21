"""The scrape side must run **without the import-side dependency** (prd/11 §7).

The airgapped scrape box has no DuckDB: the store is an import-side concept.
``python -m ingest`` used to import ``.live`` -> ``.importer`` -> ``.store`` at
module import time, so even ``--help`` crashed with ``ModuleNotFoundError:
duckdb`` on that box. The two subcommands are dispatched before the legacy
one-shot path; this test blocks ``duckdb`` in a subprocess and proves both the
parser and a real fixture scrape still run.

    python3 -m unittest tests.test_airgap_cli -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Run ``python -m ingest`` with ``duckdb`` unimportable, exactly like the
#: airgapped artifact. ``argv`` is the full ``sys.argv`` (``argv[0]`` included).
BLOCKER = """
import runpy, sys

class _NoDuckDB:
    def find_spec(self, name, path=None, target=None):
        if name == "duckdb" or name.startswith("duckdb."):
            raise ImportError("the scrape side must not import duckdb")
        return None

sys.meta_path.insert(0, _NoDuckDB())
sys.argv = {argv!r}
runpy.run_module("ingest", run_name="__main__")
"""


def _run_without_duckdb(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", BLOCKER.format(argv=argv)],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


class AirgapCliTests(unittest.TestCase):
    def test_the_scrape_parser_does_not_import_duckdb(self):
        proc = _run_without_duckdb(["ingest", "scrape", "--help"])
        self.assertEqual(
            proc.returncode, 0,
            f"scrape --help failed without duckdb:\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertNotIn("ModuleNotFoundError", proc.stderr)

    def test_a_jira_fixture_scrape_runs_without_duckdb(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "jira.tgz"
            state = Path(tmp) / "state"
            proc = _run_without_duckdb([
                "ingest", "scrape", "--jira", "--fixture",
                "--out", str(out), "--state-dir", str(state), "--quiet",
            ])
            self.assertEqual(
                proc.returncode, 0,
                f"jira fixture scrape failed without duckdb:\n{proc.stdout}\n{proc.stderr}",
            )
            self.assertTrue(out.is_file(), "the scrape wrote no bundle")
            self.assertNotIn("ModuleNotFoundError", proc.stderr)

    def test_an_import_subcommand_still_uses_duckdb(self):
        """The lazy import is a boundary, not a removal: import still needs it."""
        with tempfile.TemporaryDirectory() as tmp:
            proc = _run_without_duckdb([
                "ingest", "import", str(Path(tmp) / "missing.tgz"),
                "--db", str(Path(tmp) / "store.duckdb"),
            ])
            # The import side may fail for the missing bundle; the point is that
            # it reached the import path (duckdb is genuinely required there).
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("duckdb", (proc.stdout + proc.stderr).lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

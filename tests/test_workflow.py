"""CI supply chain: pinned actions, least privilege (SEC-5).

A moved tag on a third-party action executes arbitrary code in a job that
publishes a binary other machines will run. This test pins the workflow to full
commit SHAs and asserts that only the release job can write to the repo.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build-scraper.yml"
SHA_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


class WorkflowPinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = yaml.safe_load(WORKFLOW.read_text())

    def test_every_action_is_pinned_to_a_full_commit_sha(self):
        for name, job in self.data["jobs"].items():
            for step in job.get("steps") or []:
                uses = step.get("uses")
                if uses:
                    with self.subTest(job=name, uses=uses):
                        self.assertRegex(uses, SHA_PIN)

    def test_the_release_action_is_pinned_to_softprops_v2(self):
        release = self.data["jobs"]["release"]
        uses = [step["uses"] for step in release["steps"] if step.get("uses")]
        softprops = [use for use in uses if use.startswith("softprops/action-gh-release@")]
        self.assertEqual(len(softprops), 1, uses)
        self.assertRegex(softprops[0], SHA_PIN)
        self.assertFalse(softprops[0].endswith("@v2"))

    def test_write_permission_is_scoped_to_the_release_job(self):
        self.assertEqual(self.data["permissions"], {"contents": "read"})
        build_permissions = self.data["jobs"]["build"].get("permissions") or {}
        self.assertNotIn("write", str(build_permissions))
        self.assertEqual(self.data["jobs"]["release"]["permissions"], {"contents": "write"})

    def test_the_release_runs_only_after_a_main_push_build(self):
        release = self.data["jobs"]["release"]
        self.assertEqual(release["needs"], "build")
        self.assertIn("github.event_name == 'push'", release["if"])


if __name__ == "__main__":
    unittest.main()

"""prd/45 — concepts conformance.

``docs/concepts.md`` is the single source of truth for the units and labels the
product renders. This module is the *enforcement* the doc points at:

* a business duration never divides by 24 — a day is ``BUSINESS_HOURS_PER_DAY``
  (8 business hours, concepts.md §1);
* duration *strings* are built only by the shared formatters
  (``app/format.js`` ``fmtDuration``/``fmtCalendarDuration`` and Python
  ``analysis.rules._human_hours``), never by an ad-hoc ``hours / 24`` or
  ``hours / 8``;
* the queue diagnosis's ``per_day`` is a business-day rate
  (``analysis/ballcourt.py``), pinned to the value the old ``/24`` got wrong.

The scans are deliberately source-level: a reintroduced literal fails here even
if no behavioural test happens to exercise that line.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis import ballcourt
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = datetime(2026, 3, 1, 12, 0)

#: ``[*/] 24`` / ``[*/] 24.0`` — the d=24h leftover. A fixture that generates
#: *wall-clock minutes* (``5 * 24 * 60``) is not a business duration.
_DIVIDE_BY_24 = re.compile(r"[*/]\s+24(?:\.0)?\b")
_24_ALLOWED = {
    # synthetic Jira fixture data: wall-clock minute offsets, not durations
    "ingest/jira_fixtures.py",
}
#: A duration string built from a division: ``${hours / 24} d`` / ``f"{h / 8} d"``.
_DURATION_FROM_DIVISION = re.compile(
    r"(?:\$\{[^}]*[*/]\s*(?:24|8)(?:\.0)?\b[^}]*\}|f\".*?[*/]\s*(?:24|8)(?:\.0)?\b.*?\")"
)
#: `${n} d` / `f"{n} d"`: legal only in the shared formatter (and the one
#: threshold label that reads its two numbers from the payload).
_DAY_LABEL = re.compile(r"(?:\$\{[^}]*\}\s*d\b|\}\s*d\")")
_DAY_LABEL_ALLOWED = {
    "app/format.js",   # fmtDuration — the formatter itself
    "app/tables.js",   # weekLabel: the payload's stated "5 d = 40 h" threshold
}


class QueueRateTests(unittest.TestCase):
    """The queue diagnosis's ``per_day`` is business days, not 24-hour units."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def test_per_day_is_a_business_day_rate(self):
        events = [
            self.event(1, "pr_opened", "AuAu", 0.0),
            self.event(1, "reviewer_changed", "AuAu", 0.0,
                       people=("RArA",), change="added"),
            self.event(1, "pr_approved", "RArA", 4.0),
            self.event(1, "pr_merged", "RArA", 16.0),
        ]
        con = connect(self.path)
        try:
            ensure_schema(con)
            write_events(con, events, [])
            write_objects(con, [{
                "object_type": "PR",
                "object_id": f"PR:{REPO}#1",
                "repo": REPO,
                "pr_id": 1,
                "ticket_keys": ["ENG-1"],
                "actor": "AuAu",
                "state": "merged",
                "created_at": T0,
                "closed_at": T0 + timedelta(hours=16),
                "head_commit": "a" * 40,
                "message_class": None,
                "attributes": {},
            }])
            write_run(con, {
                "run_id": "fixture-conformance",
                "source": "fixture",
                "as_of": AS_OF,
                "source_watermark": AS_OF,
                "classifier_version": "deterministic-v1",
                "prs_seen": 1, "prs_kept": 1, "prs_dropped": 0,
                "events_written": len(events), "events_skipped": 0,
            })
        finally:
            con.close()

        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
        finally:
            con.close()

        # a two-calendar-day window is 48 / 8 = 6 business days; the one merged
        # PR therefore settles at 1/6 per business day. The old `span_hours/24`
        # read 1/2 — the number this test forbids returning.
        window = (T0, T0 + timedelta(hours=48))
        payload = ballcourt.queue_series(court, level="queue", window=window)
        streams = {row["id"]: row for row in payload["streams"]}
        merged = streams[ballcourt.REVIEW]
        self.assertEqual(merged["summary"]["throughput_per_day"], 0.17)
        self.assertNotEqual(merged["summary"]["throughput_per_day"], 0.5)

    def event(self, number: int, activity: str, actor: str, hours: float,
              **extra) -> dict:
        key = f"{REPO}#{number}:{activity}:{hours}"
        attributes = {k: v for k, v in extra.items() if k != "people"}
        event = {
            "event_key": key,
            "ts": T0 + timedelta(hours=hours),
            "activity": activity,
            "actor": actor,
            "repo": REPO,
            "pr_id": number,
            "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(key.encode(), digest_size=8).hexdigest(),
            "as_of": AS_OF,
        }
        return event


class SourceConformanceTests(unittest.TestCase):
    """The grep the PRD asks for, made permanent."""

    @staticmethod
    def python_sources() -> list[Path]:
        return sorted(
            path
            for folder in ("analysis", "ingest", "server")
            for path in (REPO_ROOT / folder).glob("*.py")
        )

    @staticmethod
    def js_sources() -> list[Path]:
        return sorted((REPO_ROOT / "app").glob("*.js"))

    @staticmethod
    def relative(path: Path) -> str:
        return str(path.relative_to(REPO_ROOT))

    def test_no_business_duration_divides_by_24(self):
        offenders = []
        for path in self.python_sources():
            name = self.relative(path)
            if name in _24_ALLOWED:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if _DIVIDE_BY_24.search(line):
                    offenders.append(f"{name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "a business duration divides by 24: " + "\n".join(offenders))

    def test_python_duration_strings_come_from_human_hours(self):
        """The only ``… d`` string in Python is ``rules._human_hours``."""
        offenders = []
        for path in self.python_sources():
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if not _DAY_LABEL.search(line):
                    continue
                # the one legal builder: _human_hours divides by the constant
                if (self.relative(path) == "analysis/rules.py"
                        and "round(hours / BUSINESS_HOURS_PER_DAY" in line):
                    continue
                offenders.append(f"{self.relative(path)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "an ad-hoc day label in Python: " + "\n".join(offenders))
        rules_text = (REPO_ROOT / "analysis" / "rules.py").read_text()
        self.assertIn("def _human_hours", rules_text)

    def test_js_divides_for_a_day_only_in_the_shared_formatter(self):
        pattern = re.compile(r"[*/]\s+(?:24|8)(?:\.0)?\b(?!-)")
        offenders = []
        for path in self.js_sources():
            if path.name == "format.js":
                continue    # the shared formatter is the one legal place
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{self.relative(path)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "an ad-hoc /8 or /24 in the UI: " + "\n".join(offenders))

    def test_js_day_labels_come_from_the_shared_formatter(self):
        offenders = []
        for path in self.js_sources():
            name = self.relative(path)
            if name in _DAY_LABEL_ALLOWED:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if _DAY_LABEL.search(line) or _DURATION_FROM_DIVISION.search(line):
                    offenders.append(f"{name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "an ad-hoc duration string in the UI: " + "\n".join(offenders))

    def test_a_self_calibrated_threshold_is_never_rendered_without_its_source(self):
        """prd/59: a threshold is a claim about a cohort; its n travels with it.

        The rules price from the metric's calibrated threshold (never a bare
        module constant) and quote the source, and the aging view prints the
        source + n next to every tick.
        """
        rules_text = (REPO_ROOT / "analysis" / "rules.py").read_text()
        self.assertIn('2 * sp["limit_hours"]', rules_text)
        self.assertIn('3 * st["threshold_hours"]', rules_text)
        self.assertIn('"limit_hours": sp["limit_hours"]', rules_text)
        self.assertIn('"threshold_source": sp.get("threshold_source")', rules_text)
        self.assertIn('"threshold_source": st.get("threshold_source")', rules_text)
        aging_text = (REPO_ROOT / "analysis" / "aging.py").read_text()
        self.assertIn("MIN_N = 20", aging_text)
        self.assertIn('"insufficient history', aging_text)
        # the band vocabulary is closed and never reuses "stale" (prd/34 owns it)
        self.assertNotIn("stale", aging_text.split("BANDS = (")[1].split(")")[0])
        ui = (REPO_ROOT / "app" / "aging.js").read_text()
        self.assertIn("block.source", ui)
        self.assertIn("n = ${block.n}", ui)
        self.assertIn("insufficient history", ui.lower())

    def test_the_stale_segment_label_is_gone(self):
        """concepts.md §5: the first segment is `created → ready`, not `ready → refined`."""
        offenders = []
        for path in (*self.python_sources(), *self.js_sources()):
            text = path.read_text()
            if "ready → refined" in text or "ready->refined" in text:
                offenders.append(self.relative(path))
        self.assertEqual(offenders, [])


class DocsEnforcementTests(unittest.TestCase):
    """concepts.md names this module as the enforcement of its units."""

    def test_concepts_points_at_the_conformance_test(self):
        text = (REPO_ROOT / "docs" / "concepts.md").read_text()
        self.assertIn("test_conformance.py", text)
        self.assertIn("How to use this file", text)
        self.assertIn("BUSINESS_HOURS_PER_DAY", text)

    def test_every_object_type_documents_what_actor_means(self):
        """prd/44 DES-6: one column, several roles — no silent type."""
        from ingest import store, taxonomy

        self.assertEqual(set(store.ACTOR_ROLE_BY_TYPE), set(taxonomy.OBJECT_TYPES))
        concepts = (REPO_ROOT / "docs" / "concepts.md").read_text()
        for role in ("author", "resolver", "self", "pusher"):
            self.assertIn(role, concepts)
        self.assertIn("?actor=", concepts)


if __name__ == "__main__":
    unittest.main()

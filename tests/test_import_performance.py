"""Import throughput is batched, not per-row (NFR-1).

The old path staged every event with ``executemany`` (one statement per row,
~9 ms/event) and re-derived attributes with one full scan per PR. This test
imports a synthetic multi-thousand-event bundle and pins a per-event budget
that the per-row implementation cannot meet, while still asserting the store is
complete and verifies.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from ingest.bundle import write_bundle
from ingest.config import Config
from ingest.importer import import_bundle
from ingest.store import connect

EVENTS = 4_000
PRS = 40
T0 = datetime(2026, 1, 1, 8, 0, 0)
HASH = "a" * 40


def _bundle() -> tuple[list[dict], list[dict], dict]:
    events: list[dict] = []
    objects: list[dict] = []
    per_pr = EVENTS // PRS
    for pr_id in range(1, PRS + 1):
        repo = "ENG/platform"
        label = f"{repo}#{pr_id}"
        objects.append(
            {
                "object_type": "PR",
                "object_id": f"PR:{label}",
                "repo": repo,
                "pr_id": pr_id,
                "ticket_keys": ["PROJ-1"],
                "actor": "AbCd",
                "state": "merged",
                "created_at": T0,
                "closed_at": T0 + timedelta(days=1),
                "head_commit": None,
                "message_class": None,
                "attributes": {"actor_kind": "human"},
            }
        )
        objects.append(
            {
                "object_type": "TICKET",
                "object_id": "TICKET:PROJ-1",
                "repo": None,
                "pr_id": None,
                "ticket_keys": ["PROJ-1"],
                "actor": None,
                "state": None,
                "created_at": None,
                "closed_at": None,
                "head_commit": None,
                "message_class": None,
                "attributes": {},
            }
        )
        for index in range(per_pr):
            ts = T0 + timedelta(minutes=index)
            events.append(
                {
                    "event_key": f"{label}:act:{index}",
                    "ts": ts,
                    "activity": "pr_pushed" if index else "pr_opened",
                    "actor": "AbCd",
                    "repo": repo,
                    "pr_id": pr_id,
                    "objects": [f"PR:{label}", "TICKET:PROJ-1"],
                    "attributes": {"actor_kind": "human"},
                    "classifier_version": "deterministic-v1",
                    "content_hash": f"{pr_id:04x}{index:012x}"[:16],
                    "as_of": T0 + timedelta(days=2),
                }
            )
    manifest = {
        "schema_version": 1,
        "source": "fixture",
        "classifier_version": "deterministic-v1",
        "ticket_projects": ["PROJ"],
        "ticket_keys": [],
        "watermark_ts": (T0 + timedelta(days=2)).isoformat(),
        "created_at": (T0 + timedelta(days=2)).isoformat(),
        "run": {
            "run_id": "fixture-performance",
            "as_of": (T0 + timedelta(days=2)).isoformat(),
            "source": "fixture",
            "prs_seen": PRS,
            "prs_kept": PRS,
            "prs_dropped": 0,
        },
    }
    return events, objects, manifest


class ImportPerformanceTests(unittest.TestCase):
    def test_a_multi_thousand_event_bundle_imports_in_a_batch_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events, objects, manifest = _bundle()
            bundle = root / "perf.tgz"
            write_bundle(bundle, manifest=manifest, events=events, objects=objects)
            cfg = Config(
                db_path=root / "store.duckdb",
                identity_path=root / "identity.json",
                ticket_regex=r"\b[A-Z]+-\d+\b",
                key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
                repos=[("ENG", "platform")],
                ticket_projects=("PROJ",),
            )
            started = time.monotonic()
            report = import_bundle(cfg, bundle)
            elapsed = time.monotonic() - started

            self.assertTrue(report["verified"], report["problems"])
            self.assertEqual(report["events_new"], EVENTS)
            per_event_ms = elapsed * 1000.0 / EVENTS
            self.assertLess(
                per_event_ms,
                2.0,
                f"import took {elapsed:.2f}s ({per_event_ms:.2f} ms/event) for {EVENTS} events",
            )
            con = connect(cfg.db_path, read_only=True)
            try:
                self.assertEqual(con.execute("SELECT count(*) FROM events").fetchone()[0], EVENTS)
                self.assertEqual(
                    con.execute("SELECT count(*) FROM event_objects").fetchone()[0], EVENTS * 2
                )
            finally:
                con.close()

            # Re-import is still a cheap no-op.
            second = import_bundle(cfg, bundle)
            self.assertEqual(second["events_new"], 0)
            self.assertEqual(second["events_rewritten"], 0)


if __name__ == "__main__":
    unittest.main()

"""Scrape-side contract tests: tgz roundtrip, integrity, watermark (stdlib only).

These run without DuckDB and without a network: they are the tests the scraper
artifact's own CI job can afford (prd/13). The import half is covered by
``tests/`` (which needs the store) and by the end-to-end run in the README.

    python3 -m unittest discover -s scrape/tests -t .
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ingest import fixtures
from ingest.bundle import (
    BUNDLE_DIR,
    EVENTS,
    MANIFEST,
    OBJECTS,
    SUMS,
    BundleError,
    BundleSpool,
    inflight_path,
    read_bundle,
    read_inflight,
    read_spool,
    spool_dir,
    write_bundle,
)
from ingest.config import Config
from ingest.scrape import ScrapeState, scrape
from ingest.timeutil import to_naive_utc
from ingest.transport import FixtureTransport

T0 = datetime(2026, 10, 1, 9, 0, 0)


def _event(key: str, activity: str = "pr_opened", minute: int = 0) -> dict:
    return {
        "event_key": key,
        "ts": T0.replace(minute=minute),
        "activity": activity,
        "actor": "LaNo",
        "repo": "DEL/delivery-api",
        "pr_id": 7,
        "attributes": {"actor_kind": "human"},
        "classifier_version": "deterministic-v1",
        "content_hash": "0123456789abcdef",
        "as_of": T0,
    }


def _object(object_type: str, object_id: str) -> dict:
    return {
        "object_type": object_type,
        "object_id": object_id,
        "repo": "DEL/delivery-api",
        "pr_id": 7,
        "ticket_keys": ["DEL-1"],
        "actor": "LaNo",
        "state": "open" if object_type == "PR" else None,
        "created_at": T0,
        "closed_at": None,
        "head_commit": None,
        "message_class": None,
        "attributes": {},
    }


def _manifest(**overrides) -> dict:
    manifest = {
        "schema_version": 1,
        "created_at": "2026-10-01T10:00:00Z",
        "source": "fixture",
        "sources": ["fixture"],
        "classifier_version": "deterministic-v1",
        "ticket_projects": ["DEL"],
        "repos": ["DEL/delivery-api"],
        "watermark_ts": "2026-10-01T09:05:00Z",
        "counts": {},
    }
    manifest.update(overrides)
    return manifest


def _payload() -> tuple[list[dict], list[dict], list[tuple[str, str, str]]]:
    events = [_event("DEL/delivery-api#7:act:1"), _event("DEL/delivery-api#7:act:2", "pr_merged", 5)]
    objects = [_object("PR", "PR:DEL/delivery-api#7"), _object("TICKET", "TICKET:DEL-1")]
    relations = [
        ("DEL/delivery-api#7:act:1", "PR", "PR:DEL/delivery-api#7"),
        ("DEL/delivery-api#7:act:1", "TICKET", "TICKET:DEL-1"),
        ("DEL/delivery-api#7:act:2", "PR", "PR:DEL/delivery-api#7"),
    ]
    return events, objects, relations


def _rewrite(bundle: Path, name: str, payload: bytes) -> None:
    """Replace one member and keep the rest, so tests can build bad bundles."""
    members: dict[str, bytes] = {}
    with tarfile.open(bundle, "r:gz") as tar:
        for member in tar.getmembers():
            members[Path(member.name).name] = tar.extractfile(member).read()
    members[name] = payload
    sums = "".join(
        f"{hashlib.sha256(members[n]).hexdigest()}  {n}\n" for n in (MANIFEST, EVENTS, OBJECTS)
    ).encode()
    members[SUMS] = sums
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            for n in (MANIFEST, EVENTS, OBJECTS, SUMS):
                info = tarfile.TarInfo(name=f"{BUNDLE_DIR}/{n}")
                info.size = len(members[n])
                info.mtime = 0
                tar.addfile(info, io.BytesIO(members[n]))
    bundle.write_bytes(raw.getvalue())


class BundleRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "export.tgz"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, **manifest) -> Path:
        events, objects, relations = _payload()
        write_bundle(
            self.path,
            manifest=_manifest(**manifest),
            events=events,
            objects=objects,
            relations=relations,
        )
        return self.path

    def test_members_are_exactly_the_contract(self):
        self.write()
        with tarfile.open(self.path, "r:gz") as tar:
            self.assertEqual(
                sorted(tar.getnames()),
                sorted(f"{BUNDLE_DIR}/{n}" for n in (MANIFEST, EVENTS, OBJECTS, SUMS)),
            )

    def test_roundtrip_parses_rows_relations_and_watermark(self):
        bundle = read_bundle(self.write())
        self.assertEqual(len(bundle.events), 2)
        self.assertEqual(len(bundle.objects), 2)
        self.assertEqual(bundle.events[0]["ts"], datetime(2026, 10, 1, 9, 0))
        self.assertEqual(bundle.watermark, to_naive_utc("2026-10-01T09:05:00Z"))
        self.assertEqual(bundle.ticket_projects, ("DEL",))
        self.assertIn(("DEL/delivery-api#7:act:1", "PR", "PR:DEL/delivery-api#7"), bundle.relations)
        self.assertEqual(bundle.events[1]["objects"], ["PR:DEL/delivery-api#7"])

    def test_bundle_is_deterministic(self):
        first = read_bundle(self.write()).sha256
        second = read_bundle(self.write()).sha256
        self.assertEqual(first, second)

    def test_no_identity_map_crosses_the_boundary(self):
        self.write()
        with tarfile.open(self.path, "r:gz") as tar:
            for member in tar.getmembers():
                self.assertNotIn("identity", member.name)
        raw = self.path.read_bytes()
        self.assertNotIn(b"LaNo\":", raw)  # only pseudonyms, no raw->pseudonym table

    def test_tampered_payload_is_rejected(self):
        path = self.write()
        with tarfile.open(path, "r:gz") as tar:
            bad = tar.extractfile(f"{BUNDLE_DIR}/{EVENTS}").read() + b'{"event_key":"x"}\n'
        # rewrite without fixing sums -> checksum mismatch
        members = {}
        with tarfile.open(path, "r:gz") as tar:
            for member in tar.getmembers():
                members[Path(member.name).name] = tar.extractfile(member).read()
        members[EVENTS] = bad
        raw = io.BytesIO()
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as archive:
                for n, payload in members.items():
                    info = tarfile.TarInfo(name=f"{BUNDLE_DIR}/{n}")
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
        path.write_bytes(raw.getvalue())
        with self.assertRaises(BundleError) as ctx:
            read_bundle(path)
        self.assertIn("checksum", str(ctx.exception))

    def test_counts_must_match_the_rows(self):
        path = self.write()
        manifest = dict(_manifest(counts={"events": 99, "objects": 2}))
        _rewrite(path, MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode())
        with self.assertRaises(BundleError) as ctx:
            read_bundle(path)
        self.assertIn("99 event(s)", str(ctx.exception))

    def test_unknown_schema_version_is_rejected(self):
        path = self.write(schema_version=99)
        with self.assertRaises(BundleError) as ctx:
            read_bundle(path)
        self.assertIn("schema_version", str(ctx.exception))

    def test_unknown_classifier_version_is_rejected(self):
        path = self.write(classifier_version="future-v9")
        with self.assertRaises(BundleError) as ctx:
            read_bundle(path)
        self.assertIn("classifier_version", str(ctx.exception))

    def test_activity_outside_the_vocabulary_is_rejected(self):
        path = self.write()
        events, objects, _relations = _payload()
        events[0]["activity"] = "summarised_the_ticket"
        from ingest.bundle import serialize_event

        rows = [serialize_event(e, ["PR:DEL/delivery-api#7"]) for e in events]
        _rewrite(path, EVENTS, "".join(json.dumps(r) + "\n" for r in rows).encode())
        with self.assertRaises(BundleError) as ctx:
            read_bundle(path)
        self.assertIn("vocabulary", str(ctx.exception))

    def test_event_without_objects_is_rejected(self):
        path = self.write()
        events, _objects, _relations = _payload()
        from ingest.bundle import serialize_event

        rows = [serialize_event(e, []) for e in events]
        _rewrite(path, EVENTS, "".join(json.dumps(r) + "\n" for r in rows).encode())
        with self.assertRaises(BundleError) as ctx:
            read_bundle(path)
        self.assertIn("references no object", str(ctx.exception))

    def test_missing_bundle_is_a_clean_error(self):
        with self.assertRaises(BundleError) as ctx:
            read_bundle(self.root / "nope.tgz")
        self.assertIn("no bundle", str(ctx.exception))


class ScrapeStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_watermark_persists_per_source(self):
        state = ScrapeState(self.root / "watermark.json")
        self.assertIsNone(state.watermark("bitbucket-dc"))
        stamp = datetime(2026, 10, 5, 12, 0)
        state.record("bitbucket-dc", stamp, "run-1", self.root / "a.tgz")
        state.save()

        reloaded = ScrapeState(self.root / "watermark.json")
        self.assertEqual(reloaded.watermark("bitbucket-dc"), stamp)
        self.assertIsNone(reloaded.watermark("jira"))
        self.assertEqual(reloaded.runs("bitbucket-dc"), 1)
        reloaded.record("bitbucket-dc", stamp, "run-2", self.root / "b.tgz")
        self.assertEqual(reloaded.runs("bitbucket-dc"), 2)


class FixtureScrapeTests(unittest.TestCase):
    """Scrape a synthetic source through the real code path, then read it back."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=self.root / "unused.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform"), ("ENG", "mobile")],
            ticket_projects=("PROJ", "OPS", "ARCH"),
        )
        self.bundle = fixtures.generate(n=12, seed=11)
        self.out = self.root / "export" / "fixture.tgz"

    def tearDown(self):
        self.tmp.cleanup()

    def test_scrape_writes_a_valid_bundle_and_local_state(self):
        report = scrape(self.cfg, FixtureTransport(self.bundle), "fixture", out=self.out)
        self.assertGreater(report["events"], 0)
        self.assertEqual(report["prs_kept"] + report["prs_dropped"], report["prs_seen"])

        bundle = read_bundle(self.out)
        self.assertEqual(bundle.manifest["source"], "fixture")
        self.assertEqual(len(bundle.events), report["events"])
        self.assertIsNotNone(bundle.watermark)

        state_dir = Path(report["state_dir"])
        self.assertTrue((state_dir / "watermark.json").is_file())
        # raw-zone identity map, next to the output, never inside the tgz
        self.assertTrue((state_dir / "identity.json").is_file())
        self.assertIn("actors", json.loads((state_dir / "identity.json").read_text()))

    def test_second_scrape_with_a_state_dir_is_incremental(self):
        first = scrape(self.cfg, FixtureTransport(self.bundle), "fixture", out=self.out)
        watermark = to_naive_utc(first["watermark"])
        second = scrape(
            self.cfg, FixtureTransport(self.bundle), "fixture",
            out=self.out, state_dir=first["state_dir"], overlap_hours=0,
        )
        self.assertLessEqual(second["events"], first["events"])
        self.assertIsNotNone(second["previous_watermark"])
        for event in read_bundle(self.out).events:
            self.assertGreaterEqual(event["ts"], watermark)
        # the watermark never goes backwards
        self.assertGreaterEqual(to_naive_utc(second["watermark"]), watermark)

    def test_full_rescrape_ignores_the_watermark(self):
        first = scrape(self.cfg, FixtureTransport(self.bundle), "fixture", out=self.out)
        second = scrape(
            self.cfg, FixtureTransport(self.bundle), "fixture",
            out=self.out, state_dir=first["state_dir"], overlap_hours=0, full=True,
        )
        self.assertEqual(second["events"], first["events"])
        self.assertIsNone(second["previous_watermark"])

    def test_pseudonyms_are_stable_across_scrapes(self):
        scrape(self.cfg, FixtureTransport(self.bundle), "fixture", out=self.out)
        people_first = {o["object_id"] for o in read_bundle(self.out).objects if o["object_type"] == "PERSON"}
        # A *full* re-scrape: an incremental one only re-emits the PRs that can
        # still change (open / recently updated -- prd/14 §3), so it would not
        # mention every person again. Pseudonym continuity is about the same
        # person keeping the same pseudonym, so compare like with like.
        scrape(
            self.cfg, FixtureTransport(self.bundle), "fixture",
            out=self.out, state_dir=self.out.parent / f"{self.out.stem}.state", full=True,
        )
        people_second = {o["object_id"] for o in read_bundle(self.out).objects if o["object_type"] == "PERSON"}
        self.assertEqual(people_first, people_second)


class SpoolTests(unittest.TestCase):
    """The in-flight staging area an incremental checkpoint writes (prd/33 §1).

    A scrape stages only the rows added since the last checkpoint here and writes
    the tgz once at the end, so the spool must be a *complete* and *readable*
    record of the run so far -- it is what the resume path reads.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "export" / "del.tgz"
        self.spool = BundleSpool(spool_dir(self.path))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_spool_lives_next_to_the_output_and_is_not_a_bundle(self):
        self.assertEqual(spool_dir(self.path), self.root / "export" / "del.tgz.parts")
        self.assertEqual(spool_dir("del.tgz"), Path("del.tgz.parts"))

    def test_a_checkpoint_stages_only_the_rows_that_are_new(self):
        events, objects, relations = _payload()
        first = self.spool.append(
            manifest=_manifest(), events=events[:1], objects=objects[:1],
            relations=relations[:2],
        )
        self.assertEqual(first["events"], 1)
        self.assertEqual(first["objects"], 1)
        # The same lists again (a repo-boundary checkpoint with no new work):
        # nothing is staged, so the write is O(delta), not O(prefix).
        again = self.spool.append(
            manifest=_manifest(), events=events[:1], objects=objects[:1],
            relations=relations[:2],
        )
        self.assertEqual(again["bytes"], 0)
        self.assertEqual(again["events"], 0)
        # One more row: only that row is written.
        third = self.spool.append(
            manifest=_manifest(counts={"events": 2}), events=events, objects=objects,
            relations=relations,
        )
        self.assertEqual(third["events"], 1)
        self.assertEqual(third["objects"], 1)

    def test_the_spool_reads_back_as_the_union_of_every_part(self):
        events, objects, relations = _payload()
        self.spool.append(manifest=_manifest(), events=events[:1], objects=objects,
                          relations=relations[:2])
        self.spool.append(manifest=_manifest(), events=events, objects=objects,
                          relations=relations)
        carried = read_spool(self.spool.directory)
        self.assertEqual(
            [event["event_key"] for event in carried.events],
            [event["event_key"] for event in events],
        )
        self.assertEqual(
            sorted(obj["object_id"] for obj in carried.objects),
            sorted(obj["object_id"] for obj in objects),
        )
        # Every event keeps its objects: that is what the resume carries.
        for event in carried.events:
            self.assertTrue(event["objects"], event["event_key"])
        self.assertEqual(len(carried.relations), 3)
        self.assertEqual(carried.source, "fixture")

    def test_a_later_row_for_the_same_key_wins(self):
        events, objects, _relations = _payload()
        self.spool.append(manifest=_manifest(), events=events[:1], objects=objects,
                          relations=[(events[0]["event_key"], "PR", "PR:DEL/delivery-api#7")])
        changed = {**events[0], "activity": "pr_merged"}
        self.spool.append(manifest=_manifest(), events=[changed], objects=objects,
                          relations=[(events[0]["event_key"], "PR", "PR:DEL/delivery-api#7")])
        carried = read_spool(self.spool.directory)
        self.assertEqual(len(carried.events), 1)
        self.assertEqual(carried.events[0]["activity"], "pr_merged")

    def test_read_inflight_prefers_the_spool_over_a_partial_tgz(self):
        events, objects, relations = _payload()
        write_bundle(self.path, manifest=_manifest(), events=events, objects=objects,
                     relations=relations)
        self.assertIsNotNone(inflight_path(self.path))
        self.assertFalse(inflight_path(self.path).is_dir())
        self.spool.append(manifest=_manifest(), events=events[:1], objects=objects,
                          relations=relations[:2])
        self.assertEqual(inflight_path(self.path), self.spool.directory)
        self.assertEqual(len(read_inflight(self.path).events), 1)

    def test_a_corrupt_part_is_refused_rather_than_resumed(self):
        events, objects, relations = _payload()
        self.spool.append(manifest=_manifest(), events=events, objects=objects,
                          relations=relations)
        part = next(self.spool.directory.glob("part-*.events.jsonl"))
        part.write_bytes(b"not json\n")
        with self.assertRaises(BundleError):
            read_spool(self.spool.directory)

    def test_a_missing_spool_is_an_error_not_an_empty_run(self):
        with self.assertRaises(BundleError):
            read_spool(self.spool.directory)
        with self.assertRaises(BundleError):
            read_inflight(self.path)

    def test_finish_removes_the_in_flight_rows(self):
        events, objects, relations = _payload()
        self.spool.append(manifest=_manifest(), events=events, objects=objects,
                          relations=relations)
        self.assertTrue(self.spool.directory.is_dir())
        self.spool.finish()
        self.assertFalse(self.spool.directory.exists())
        self.assertIsNone(inflight_path(self.path))

    def test_reset_discards_a_dead_run_s_rows(self):
        events, objects, relations = _payload()
        self.spool.append(manifest=_manifest(), events=events, objects=objects,
                          relations=relations)
        self.spool.reset()
        self.assertFalse(self.spool.exists)
        self.assertEqual(self.spool.append(
            manifest=_manifest(), events=events[:1], objects=objects[:1],
            relations=relations[:2],
        )["events"], 1)

    def test_resume_adopts_the_carried_rows_instead_of_staging_them_again(self):
        events, objects, relations = _payload()
        self.spool.append(manifest=_manifest(), events=events, objects=objects,
                          relations=relations)
        carried = read_spool(self.spool.directory)
        resumed = BundleSpool(self.spool.directory)
        resumed.resume(carried)
        self.assertEqual(resumed.parts, self.spool.parts)  # nothing re-staged
        again = resumed.append(manifest=_manifest(), events=carried.events,
                               objects=carried.objects, relations=carried.relations)
        self.assertEqual(again["bytes"], 0)

    def test_resuming_into_a_different_output_stages_the_carried_rows_once(self):
        events, objects, relations = _payload()
        self.spool.append(manifest=_manifest(), events=events, objects=objects,
                          relations=relations)
        carried = read_spool(self.spool.directory)
        other = BundleSpool(spool_dir(self.root / "export" / "other.tgz"))
        other.resume(carried)
        self.assertEqual(other.parts, 1)
        self.assertEqual(other.events_written, len(events))
        self.assertEqual(
            sorted(e["event_key"] for e in read_spool(other.directory).events),
            sorted(e["event_key"] for e in events),
        )

    def test_a_resumed_spool_continues_after_the_parts_a_dead_run_left(self):
        """A new part index would overwrite the checkpoint that is being resumed."""
        events, objects, relations = _payload()
        self.spool.append(manifest=_manifest(), events=events[:1], objects=objects,
                          relations=relations[:2])
        first_part = next(self.spool.directory.glob("part-00001.events.jsonl"))
        before = first_part.read_bytes()

        resumed = BundleSpool(self.spool.directory)
        self.assertEqual(resumed.parts, 1)
        resumed.append(manifest=_manifest(), events=events, objects=objects,
                       relations=relations)
        self.assertEqual(resumed.parts, 2)
        self.assertEqual(first_part.read_bytes(), before)  # the checkpoint survives
        self.assertEqual(len(read_spool(self.spool.directory).events), len(events))


if __name__ == "__main__":
    unittest.main()

"""prd/71: one ``rekey`` command for identity maintenance.

The 2026-11-23 repair was hand SQL (surname-first pseudonyms swapped, then every
numeric-suffix variant merged into its base). These tests pin the command that
replaces it: a dry run that writes nothing, an apply that merges the store in one
transaction, a missing base created, the identity map updated (duplicates
reported), a label patch rewritten to a new tgz, a cycle refused, and the
backup/no-backup pair.

Everything here is synthetic or fixture data: ``DEMO``/``demo-tlla`` only.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ingest import fixtures, labelpatch, rekey as rekey_mod, taxonomy
from ingest.__main__ import main as ingest_main
from ingest.config import Config
from ingest.pipeline import run
from ingest.store import connect, verify
from ingest.transport import FixtureTransport

NOW = datetime(2026, 11, 2, 9, 0, 0)


def _config(root: Path) -> Config:
    return Config(
        db_path=root / "store.duckdb",
        identity_path=root / "identity.json",
        ticket_regex=r"\b[A-Z]+-\d+\b",
        key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
        repos=[("ENG", "platform")],
        ticket_projects=("PROJ", "OPS", "ARCH", "DEMO"),
    )


def _object(con, otype, oid, *, repo=None, pr_id=None, keys=None, actor=None,
            state=None, attributes="{}"):
    con.execute(
        "INSERT INTO objects (object_type, object_id, repo, pr_id, ticket_keys,"
        " actor, state, created_at, attributes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, CAST(? AS JSON))",
        [otype, oid, repo, pr_id, keys, actor, state, NOW, attributes],
    )


def _event(con, key, activity, actor, *, repo=None, pr_id=None, objects=()):
    con.execute(
        "INSERT INTO events (event_key, ts, activity, actor, repo, pr_id,"
        " attributes, classifier_version, content_hash, as_of)"
        " VALUES (?, ?, ?, ?, ?, ?, CAST('{}' AS JSON), ?, NULL, ?)",
        [key, NOW, activity, actor, repo, pr_id, taxonomy.CLASSIFIER_VERSION, NOW],
    )
    for object_id in objects:
        con.execute(
            "INSERT INTO event_objects (event_key, object_type, object_id)"
            " VALUES (?, ?, ?)",
            [key, object_id.split(":", 1)[0], object_id],
        )


def _inject_suffix_actors(con) -> None:
    """A few suffix actors on top of the fixture import (BB *and* Jira sides)."""
    con.execute("BEGIN")
    # ``MaSe2`` (Bitbucket) and ``MaSe3`` (Jira) both merge into the existing
    # ``MaSe`` person; ``PaKo2`` has no base row yet (it must be created).
    _object(con, "PR", "PR:ENG/platform#9001", repo="ENG/platform", pr_id=9001,
            keys=["DEMO-9001"], actor="MaSe2", state="merged")
    _object(con, "STORY", "STORY:DEMO-9001", keys=["DEMO-9001"], actor="MaSe",
            state="open")
    for person in ("MaSe2", "MaSe3", "PaKo2"):
        _object(con, "PERSON", f"PERSON:{person}", actor=person,
                attributes='{"actor_kind":"human"}')
    # A dead PERSON row: its pseudonym appears in no event, object actor or
    # link. The sweep removes it; the roster rows above stay.
    _object(con, "PERSON", "PERSON:ZzZz", actor="ZzZz",
            attributes='{"actor_kind":"human"}')
    # An event linked to *both* the suffix actor and its existing base: the
    # merge has to drop the duplicate relation instead of violating the key.
    _event(con, "ENG/platform#9001:pr_merged", "pr_merged", "MaSe2",
           repo="ENG/platform", pr_id=9001,
           objects=["PR:ENG/platform#9001", "PERSON:MaSe2", "PERSON:MaSe"])
    _event(con, "ENG/platform#9001:pr_approved", "pr_approved", "MaSe2",
           repo="ENG/platform", pr_id=9001,
           objects=["PR:ENG/platform#9001", "PERSON:MaSe2"])
    _event(con, "DEMO-9001:assigned:1:0", "assigned", "MaSe3",
           objects=["STORY:DEMO-9001", "PERSON:MaSe3"])
    _event(con, "ENG/platform#9001:pr_pushed", "pr_pushed", "PaKo2",
           repo="ENG/platform", pr_id=9001,
           objects=["PR:ENG/platform#9001", "PERSON:PaKo2"])
    con.execute("COMMIT")


def _identity(path: Path, entries: dict[str, str]) -> None:
    path.write_text(
        json.dumps(
            {
                "_comment": "RAW ZONE ONLY. test fixture.",
                "actors": {
                    key: {"pseudonym": pseudonym, "given": "Test", "surname": "User"}
                    for key, pseudonym in entries.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _counts(db: Path) -> tuple[int, int, int]:
    con = connect(db, read_only=True)
    try:
        return tuple(
            int(con.execute(sql).fetchone()[0])
            for sql in (
                "SELECT count(*) FROM events",
                "SELECT count(*) FROM objects",
                "SELECT count(*) FROM event_objects",
            )
        )
    finally:
        con.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _person_count(db: Path) -> int:
    con = connect(db, read_only=True)
    try:
        return int(
            con.execute(
                "SELECT count(*) FROM objects WHERE object_type = 'PERSON'"
            ).fetchone()[0]
        )
    finally:
        con.close()


def _patch(path: Path) -> Path:
    """A valid label patch whose ``add`` question carries the old actor."""
    labelpatch.write_patch(
        path,
        manifest={
            "created_at": "2026-11-02T00:00:00Z",
            "source": "hans",
            "classifier_version": taxonomy.CLASSIFIER_VERSION,
        },
        rows=[
            {
                "target": "thread",
                "object_id": "THREAD:ENG/platform#9001:10",
                "labels": {"comment_type": "nit", "thread_outcome": "unresolved"},
            },
            {
                "target": "question",
                "object_id": "PR:ENG/platform#9001",
                "action": "add",
                "labels": {"phase": "review"},
                "events": [
                    {
                        "event_key": "ENG/platform#9001:added:q",
                        "ts": "2026-11-02T09:00:00Z",
                        "actor": "MaSe2",
                        "repo": "ENG/platform",
                        "pr_id": 9001,
                        "objects": ["PR:ENG/platform#9001"],
                        "attributes": {"phase": "review"},
                    }
                ],
            },
        ],
    )
    return path


class RekeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = _config(self.root)
        report = run(
            self.cfg,
            FixtureTransport(fixtures.generate(n=8, seed=7)),
            source="fixture",
        )
        self.assertTrue(report["verified"], report["problems"])
        con = connect(self.cfg.db_path)
        try:
            _inject_suffix_actors(con)
            check = verify(con, self.cfg.key_shape, self.cfg.ticket_projects)
            self.assertTrue(check["ok"], check["problems"])
        finally:
            con.close()

    def tearDown(self):
        self.tmp.cleanup()

    # -- dry run ---------------------------------------------------------
    def test_dry_run_writes_nothing_and_prints_the_evidence(self):
        identity = self.root / "identity.json"
        _identity(identity, {"bb:someone": "MaSe2", "jira:someone": "MaSe"})
        before = _counts(self.cfg.db_path)
        db_digest = _digest(self.cfg.db_path)
        identity_digest = _digest(identity)

        report = rekey_mod.rekey(
            self.cfg,
            db_path=self.cfg.db_path,
            identity_path=identity,
            merge_suffixes=True,
        )

        self.assertTrue(report["dry_run"])
        self.assertTrue(report["changed"])
        self.assertIsNone(report["verified"])
        self.assertEqual(_counts(self.cfg.db_path), before)
        self.assertEqual(_digest(self.cfg.db_path), db_digest)
        self.assertEqual(_digest(identity), identity_digest)
        self.assertEqual(list(self.root.glob("*pre-rekey*")), [])
        by_old = {row["old"]: row for row in report["mappings"]}
        self.assertEqual(by_old["MaSe2"]["new"], "MaSe")
        self.assertEqual(by_old["MaSe2"]["bb_events"], 2)
        self.assertEqual(by_old["MaSe2"]["jira_events"], 0)
        self.assertEqual(by_old["MaSe3"]["jira_events"], 1)
        self.assertEqual(by_old["MaSe2"]["person_links"], 2)
        self.assertEqual(by_old["PaKo2"]["new"], "PaKo")
        self.assertEqual(by_old["MaSe2"]["shared_ticket_keys"], ["DEMO-9001"])
        self.assertEqual(by_old["MaSe2"]["prs_old"], 1)
        # The identity map would change but the dry run leaves it alone.
        self.assertEqual(report["identity"]["updated"], 1)
        self.assertFalse(report["identity"].get("written"))

    # -- apply + idempotence ---------------------------------------------
    def test_apply_merges_everything_and_a_second_run_is_unchanged(self):
        before = _counts(self.cfg.db_path)
        before_persons = _person_count(self.cfg.db_path)
        report = rekey_mod.rekey(
            self.cfg,
            db_path=self.cfg.db_path,
            merge_suffixes=True,
            apply=True,
        )
        self.assertFalse(report["dry_run"])
        self.assertTrue(report["verified"], report["problems"])
        self.assertTrue(report["backup"])
        self.assertTrue(Path(report["backup"]).is_file())
        # No event, object or relation is invented or lost by a merge beyond
        # the merged rows themselves; one dead PERSON row is swept.
        after = _counts(self.cfg.db_path)
        self.assertEqual(after[0], before[0])  # events
        self.assertEqual(after[1], before[1] - 3)  # 2 merged + 1 dead
        self.assertLessEqual(after[2], before[2])
        self.assertEqual(report["orphan_persons"], 1)
        self.assertEqual(_person_count(self.cfg.db_path), before_persons - 3)

        con = connect(self.cfg.db_path, read_only=True)
        try:
            actors = {row[0] for row in con.execute(
                "SELECT DISTINCT actor FROM events"
            ).fetchall()}
            self.assertFalse({"MaSe2", "MaSe3", "PaKo2"} & actors)
            self.assertIn("MaSe", actors)
            self.assertIn("PaKo", actors)
            persons = {row[0] for row in con.execute(
                "SELECT object_id FROM objects WHERE object_type = 'PERSON'"
            ).fetchall()}
            self.assertNotIn("PERSON:MaSe2", persons)
            self.assertNotIn("PERSON:MaSe3", persons)
            self.assertNotIn("PERSON:ZzZz", persons)  # dead row swept
            self.assertIn("PERSON:MaSe", persons)
            self.assertIn("PERSON:PaKo", persons)  # missing base created
            person = con.execute(
                "SELECT actor, CAST(attributes AS VARCHAR) FROM objects"
                " WHERE object_type = 'PERSON' AND object_id = 'PERSON:PaKo'"
            ).fetchone()
            self.assertEqual(person[0], "PaKo")
            self.assertEqual(json.loads(person[1])["actor_kind"], "human")
            # No relation to an old person survives, and none is duplicated.
            stale = con.execute(
                "SELECT count(*) FROM event_objects WHERE object_type = 'PERSON'"
                " AND object_id IN ('PERSON:MaSe2', 'PERSON:MaSe3', 'PERSON:PaKo2')"
            ).fetchone()[0]
            self.assertEqual(stale, 0)
            duplicated = con.execute(
                "SELECT count(*) FROM (SELECT event_key, object_type, object_id,"
                " count(*) c FROM event_objects GROUP BY 1, 2, 3 HAVING c > 1)"
            ).fetchone()[0]
            self.assertEqual(duplicated, 0)
            check = verify(con, self.cfg.key_shape, self.cfg.ticket_projects)
            self.assertTrue(check["ok"], check["problems"])
        finally:
            con.close()

        # Second --apply: all old names are gone, so nothing is written.
        digest = _digest(self.cfg.db_path)
        backups = sorted(self.root.glob("*pre-rekey*"))
        again = rekey_mod.rekey(
            self.cfg,
            db_path=self.cfg.db_path,
            merge_suffixes=True,
        )
        self.assertTrue(again["unchanged"])
        self.assertFalse(again["changed"])
        self.assertIsNone(again["backup"])
        self.assertEqual(_digest(self.cfg.db_path), digest)
        self.assertEqual(sorted(self.root.glob("*pre-rekey*")), backups)
        self.assertEqual(_counts(self.cfg.db_path), after)

    # -- explicit map ----------------------------------------------------
    def test_explicit_map_merges_a_person_and_updates_the_identity_map(self):
        identity = self.root / "identity.json"
        _identity(
            identity,
            {
                "bb:old": "MaSe2",
                "jira:old": "MaSe",
                "bb:other": "KrSa",
            },
        )
        report = rekey_mod.rekey(
            self.cfg,
            db_path=self.cfg.db_path,
            identity_path=identity,
            explicit={"MaSe2": "MaSe"},
            apply=True,
        )
        self.assertTrue(report["verified"], report["problems"])
        self.assertEqual(report["identity"]["updated"], 1)
        self.assertTrue(report["identity"]["written"])
        duplicates = {
            row["pseudonym"]: row["keys"] for row in report["identity"]["duplicates"]
        }
        self.assertEqual(duplicates["MaSe"], ["bb:old", "jira:old"])
        stored = json.loads(identity.read_text())
        self.assertEqual(stored["actors"]["bb:old"]["pseudonym"], "MaSe")
        self.assertEqual(stored["actors"]["jira:old"]["pseudonym"], "MaSe")
        self.assertEqual(stored["actors"]["bb:other"]["pseudonym"], "KrSa")

        con = connect(self.cfg.db_path, read_only=True)
        try:
            self.assertEqual(
                con.execute(
                    "SELECT count(*) FROM events WHERE actor = 'MaSe2'"
                ).fetchone()[0],
                0,
            )
            self.assertIsNotNone(
                con.execute(
                    "SELECT 1 FROM objects WHERE object_type = 'PERSON'"
                    " AND object_id = 'PERSON:MaSe'"
                ).fetchone()
            )
        finally:
            con.close()

    # -- patch -----------------------------------------------------------
    def test_label_patch_gets_the_new_actor_and_the_input_is_untouched(self):
        source = _patch(self.root / "labels.tgz")
        out = self.root / "labels.rekeyed.tgz"
        before = _digest(source)

        dry = rekey_mod.rekey(
            self.cfg,
            db_path=self.cfg.db_path,
            explicit={"MaSe2": "MaSe"},
            patch_path=source,
            out_path=out,
        )
        self.assertEqual(dry["patch"]["events_remapped"], 1)
        self.assertFalse(out.exists())
        self.assertEqual(_digest(source), before)

        report = rekey_mod.rekey(
            self.cfg,
            db_path=self.cfg.db_path,
            explicit={"MaSe2": "MaSe"},
            patch_path=source,
            out_path=out,
            apply=True,
        )
        self.assertTrue(report["verified"], report["problems"])
        self.assertTrue(report["patch"]["validated"])
        self.assertTrue(out.is_file())
        self.assertEqual(_digest(source), before)  # never rewritten in place
        self.assertNotEqual(out.resolve(), source.resolve())
        rewritten = labelpatch.read_patch(out)
        actors = [
            event["actor"]
            for row in rewritten.rows
            for event in row.get("events") or ()
        ]
        self.assertIn("MaSe", actors)
        self.assertNotIn("MaSe2", actors)
        self.assertEqual(rewritten.manifest["rekey"]["events_remapped"], 1)
        self.assertEqual(len(rewritten.rows), 2)

        # A second identical --apply sees the store, the identity map and the
        # already-rekeyed output as done: nothing is written, not even a backup.
        digest = _digest(self.cfg.db_path)
        out_digest = _digest(out)
        backups = sorted(self.root.glob("*pre-rekey*"))
        again = rekey_mod.rekey(
            self.cfg,
            db_path=self.cfg.db_path,
            explicit={"MaSe2": "MaSe"},
            patch_path=source,
            out_path=out,
            apply=True,
        )
        self.assertTrue(again["unchanged"])
        self.assertTrue(again["patch"]["already_rekeyed"])
        self.assertEqual(_digest(self.cfg.db_path), digest)
        self.assertEqual(_digest(out), out_digest)
        self.assertEqual(sorted(self.root.glob("*pre-rekey*")), backups)

    # -- cycle -----------------------------------------------------------
    def test_a_cycle_is_refused_and_writes_nothing(self):
        before = _counts(self.cfg.db_path)
        with self.assertRaises(rekey_mod.RekeyError) as caught:
            rekey_mod.rekey(
                self.cfg,
                db_path=self.cfg.db_path,
                explicit={"MaSe": "KrSa", "KrSa": "MaSe"},
                apply=True,
            )
        self.assertIn("cycle", str(caught.exception))
        self.assertEqual(_counts(self.cfg.db_path), before)
        self.assertEqual(list(self.root.glob("*pre-rekey*")), [])

    def test_a_chain_resolves_transitively(self):
        resolved, notes = rekey_mod.resolve_mapping(
            {"MaSe2": "MaSe3", "MaSe3": "MaSe"}
        )
        self.assertEqual(resolved, {"MaSe2": "MaSe", "MaSe3": "MaSe"})
        self.assertTrue(notes)

    # -- backup ----------------------------------------------------------
    def test_backup_is_a_copy_and_no_backup_skips_it(self):
        before = _counts(self.cfg.db_path)
        report = rekey_mod.rekey(
            self.cfg,
            db_path=self.cfg.db_path,
            explicit={"PaKo2": "PaKo"},
            apply=True,
        )
        backup = Path(report["backup"])
        self.assertTrue(backup.is_file())
        self.assertIn(".pre-rekey-", backup.name)
        self.assertEqual(_counts(backup), before)  # a real, openable copy
        copy = connect(backup, read_only=True)
        try:
            check = verify(copy, self.cfg.key_shape, self.cfg.ticket_projects)
        finally:
            copy.close()
        self.assertTrue(check["ok"], check["problems"])

        # The pre-rekey copy still holds PaKo2, so a --no-backup apply on it is
        # a real change that must create no backup at all.
        second = self.root / "no-backup.duckdb"
        second.write_bytes(backup.read_bytes())
        skipped = rekey_mod.rekey(
            self.cfg,
            db_path=second,
            explicit={"PaKo2": "PaKo"},
            apply=True,
            backup=False,
        )
        self.assertTrue(skipped["changed"])
        self.assertTrue(skipped["verified"], skipped["problems"])
        self.assertIsNone(skipped["backup"])
        self.assertEqual(list(self.root.glob("no-backup.duckdb*pre-rekey*")), [])

    # -- CLI -------------------------------------------------------------
    def test_cli_routes_rekey_and_defaults_to_dry_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ingest_main(
                [
                    "rekey",
                    "--db", str(self.cfg.db_path),
                    "--map", "PaKo2=PaKo",
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("PaKo2 → PaKo", text)
        self.assertIn("nothing written", text)
        self.assertEqual(list(self.root.glob("*pre-rekey*")), [])


if __name__ == "__main__":
    unittest.main()

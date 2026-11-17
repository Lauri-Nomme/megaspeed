"""prd/22 ingest slice: a PR's branch refs are persisted as keyed digests.

The stack DAG only ever asks "is this the same branch?" (``A.toRef ==
B.fromRef``), so equality is all the store needs. The branch *name* is the one
thing prd/02 forbids: a DC branch is the ticket summary in slug form
(``DEL-101-add-login-form-validation``), ``schema.sql`` has no branch column and
``--verify`` rejects prose anywhere. A digest answers the question without the
name, and the repo is part of it so two repos' ``main`` cannot look like one
branch.

SEC-1: the digest is an HMAC over a per-tenant pepper from the raw zone
(:mod:`ingest.pepper`), 128 bits wide. A plain hash would be reversible: both
inputs are in the clear in the store and the branch is a slug of the summary.
The last classes here pin that before/after, and that no unkeyed mode exists.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import gzip
import hashlib
import itertools
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ingest import fixtures, pepper as pepper_mod
from ingest import taxonomy
from ingest.bitbucket import PullRequest
from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.pipeline import KeyFilter, Reducer, Stats, ref_digest
from ingest.pseudonym import IdentityTable
from ingest.scrape import scrape
from ingest.store import connect, ensure_schema, verify, write_objects, write_run
from ingest.transport import FixtureTransport

AS_OF = datetime(2026, 11, 15, 12, 0, 0)
T0 = 1_790_000_000_000  # epoch ms
REPO = "ENG/platform"
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}
#: A fixed per-tenant pepper for the tests (the real one is random per state dir).
PEPPER = bytes.fromhex("5e" * 32)
OTHER_PEPPER = bytes.fromhex("a1" * 32)


def digest(branch: str, repo: str = REPO, pepper: bytes = PEPPER) -> str:
    return ref_digest(repo, {"displayId": branch}, pepper)


class RefDigestTests(unittest.TestCase):
    def test_the_same_branch_is_the_same_digest_and_the_name_is_gone(self):
        a = digest("feature/ENG-1-base")
        b = digest("feature/ENG-1-base")
        self.assertEqual(a, b)
        self.assertRegex(a, taxonomy.REF_DIGEST)
        self.assertNotIn("ENG-1-base", a)
        self.assertNotEqual(a, digest("main"))

    def test_the_digest_is_128_bits_of_hex(self):
        # SEC-1: >= 128 bits, and the *key* is what stops dictionary recovery.
        self.assertEqual(len(digest("feature/ENG-1-base")), 32)

    def test_a_different_pepper_gives_a_different_digest(self):
        """The whole point: no pepper, no reproducibility (and no cross-tenant link)."""
        self.assertNotEqual(
            digest("feature/ENG-1-base", pepper=PEPPER),
            digest("feature/ENG-1-base", pepper=OTHER_PEPPER),
        )
        # ... including across "tenants" that share a repo name.
        self.assertNotEqual(
            ref_digest(REPO, {"displayId": "main"}, PEPPER),
            ref_digest(REPO, {"displayId": "main"}, OTHER_PEPPER),
        )

    def test_a_branch_ref_is_repo_scoped(self):
        # two repos' main must not look like the same branch: that would invent
        # a cross-repo stack edge
        self.assertNotEqual(
            digest("main", repo="ENG/platform"),
            digest("main", repo="ENG/mobile"),
        )

    def test_a_missing_ref_is_none_not_empty(self):
        self.assertIsNone(ref_digest(REPO, None, PEPPER))
        self.assertIsNone(ref_digest(REPO, {}, PEPPER))
        self.assertIsNone(ref_digest(REPO, {"displayId": "  "}, PEPPER))

    def test_there_is_no_unkeyed_mode(self):
        """An empty pepper must raise, not fall back to a reversible hash."""
        for bad in (b"", "", None):
            with self.assertRaises(ValueError):
                ref_digest(REPO, {"displayId": "main"}, bad)

    def test_the_object_attribute_spec_accepts_a_digest_and_rejects_a_name(self):
        payload = {"source_ref": digest("feature/ENG-1-b"), "target_ref": digest("main")}
        self.assertEqual(taxonomy.check_object_attributes(payload), payload)
        for bad in ({"source_ref": "feature/ENG-1-b"}, {"target_ref": "DEL-101-add-login"}):
            with self.assertRaises(taxonomy.TaxonomyError):
                taxonomy.check_object_attributes(bad)

    def test_the_spec_rejects_the_old_unkeyed_16_hex_shape(self):
        old = hashlib.blake2b(f"{REPO}|main".encode(), digest_size=8).hexdigest()
        self.assertRegex(old, r"^[0-9a-f]{16}$")
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.check_object_attributes({"source_ref": old})


class DictionaryRecoveryTests(unittest.TestCase):
    """SEC-1 before/after: the old digest is recoverable, the keyed one is not.

    The review's repro: repo + ticket key are in the clear in the store, and a
    branch name is ``KEY-<slug of the summary>``, so a few thousand permutations
    of common words recover it in milliseconds.
    """

    WORDS = ("add", "login", "form", "validation", "fix", "api", "cache",
             "retry", "ui", "db", "auth")
    TARGET = "DEL-101-add-login-form-validation"

    def candidates(self):
        for size in range(1, 5):
            for combo in itertools.permutations(self.WORDS, size):
                yield "DEL-101-" + "-".join(combo)

    @staticmethod
    def unkeyed(branch: str) -> str:
        return hashlib.blake2b(f"DEL/web|{branch}".encode(), digest_size=8).hexdigest()

    def test_the_old_unkeyed_digest_is_recoverable_from_the_store_alone(self):
        target = self.unkeyed(self.TARGET)
        recovered = [c for c in self.candidates() if self.unkeyed(c) == target]
        self.assertEqual(recovered, [self.TARGET])

    def test_the_keyed_digest_cannot_be_recovered_without_the_pepper(self):
        target = ref_digest("DEL/web", {"displayId": self.TARGET}, PEPPER)
        # Neither the right algorithm with the wrong key, nor the old unkeyed
        # hash of any candidate, ever lands on the stored digest.
        wrong = {digest(c, repo="DEL/web", pepper=OTHER_PEPPER) for c in self.candidates()}
        unkeyed = {self.unkeyed(c) for c in self.candidates()}
        self.assertNotIn(target, wrong)
        self.assertNotIn(target, unkeyed)
        # ... and the candidate that *is* the branch is the only one the holder
        # of the pepper can confirm (equality is all the DAG needs).
        self.assertIn(target, {digest(c, repo="DEL/web") for c in self.candidates()})


class PepperFileTests(unittest.TestCase):
    def test_the_pepper_is_created_0600_once_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / pepper_mod.PEPPER_FILE
            first = pepper_mod.load_pepper(path)
            self.assertEqual(len(first), pepper_mod.PEPPER_BYTES)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(pepper_mod.load_pepper(path), first)
            # the key id is stable and one-way (it must not contain the key)
            key_id = pepper_mod.key_id(first)
            self.assertEqual(key_id, pepper_mod.key_id(first))
            self.assertNotIn(first.hex()[:16], key_id)

    def test_two_state_dirs_get_unrelated_peppers(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = pepper_mod.load_pepper(Path(tmp) / "a" / pepper_mod.PEPPER_FILE)
            b = pepper_mod.load_pepper(Path(tmp) / "b" / pepper_mod.PEPPER_FILE)
            self.assertNotEqual(a, b)

    def test_a_corrupt_or_short_pepper_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / pepper_mod.PEPPER_FILE
            for payload in ("not json", json.dumps({"pepper": "zz"}),
                            json.dumps({"pepper": "00" * 4})):
                path.write_text(payload)
                with self.assertRaises(pepper_mod.PepperError):
                    pepper_mod.load_pepper(path)


class ReducerRefTests(unittest.TestCase):
    """The reducer is where the raw ``fromRef``/``toRef`` are read and dropped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform")],
        )
        self.reducer = Reducer(
            self.cfg,
            IdentityTable(self.cfg.identity_path),
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape, self.cfg.ticket_projects),
            AS_OF,
            pepper=PEPPER,
        )
        self.stats = Stats()

    def test_a_reducer_without_a_pepper_emits_no_refs_at_all(self):
        """Never an unkeyed digest: no raw zone, no ref attributes."""
        bare = Reducer(
            self.cfg,
            IdentityTable(self.cfg.identity_path),
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape, self.cfg.ticket_projects),
            AS_OF,
        )
        pr = {
            "id": 9,
            "state": "OPEN",
            "createdDate": T0,
            "fromRef": {"displayId": "feature/ENG-1-base", "latestCommit": "a" * 40},
            "toRef": {"displayId": "main", "latestCommit": "b" * 40},
            "author": {"user": ADA},
        }
        pull = PullRequest("ENG", "platform", pr, [])
        pull.activities = [{"id": 1, "action": "OPENED", "createdDate": T0, "user": ADA}]
        bare.reduce(pull, ["ENG-1"], pull.activities, self.stats)
        record = next(o for o in self.stats.objects if o["object_type"] == "PR")
        self.assertNotIn("source_ref", record["attributes"])
        self.assertNotIn("target_ref", record["attributes"])

    def tearDown(self):
        self.tmp.cleanup()

    def reduce(self, pr_id: int, source: str, target: str) -> dict:
        pr = {
            "id": pr_id,
            "state": "OPEN",
            "createdDate": T0,
            "fromRef": {"displayId": source, "latestCommit": "a" * 40},
            "toRef": {"displayId": target, "latestCommit": "b" * 40},
            "author": {"user": ADA},
        }
        pull = PullRequest("ENG", "platform", pr, [])
        pull.activities = [{
            "id": 1, "action": "OPENED", "createdDate": T0, "user": ADA,
        }]
        self.reducer.reduce(pull, ["ENG-1"], pull.activities, self.stats)
        record = next(o for o in self.stats.objects if o["object_type"] == "PR")
        self.stats = Stats()
        return record

    def test_a_stacked_child_matches_its_parent_by_digest(self):
        parent = self.reduce(1, "feature/ENG-1-base", "main")
        child = self.reduce(2, "feature/ENG-1-b", "feature/ENG-1-base")
        self.assertEqual(parent["attributes"]["source_ref"], digest("feature/ENG-1-base"))
        self.assertEqual(parent["attributes"]["target_ref"], digest("main"))
        # the edge: the parent's source branch is the child's target branch
        self.assertEqual(parent["attributes"]["source_ref"], child["attributes"]["target_ref"])
        self.assertEqual(child["attributes"]["source_ref"], digest("feature/ENG-1-b"))

    def test_a_pr_without_refs_carries_no_ref_attributes(self):
        pr = {
            "id": 3,
            "state": "OPEN",
            "createdDate": T0,
            "author": {"user": ADA},
        }
        pull = PullRequest("ENG", "platform", pr, [])
        pull.activities = [{"id": 1, "action": "OPENED", "createdDate": T0, "user": ADA}]
        self.reducer.reduce(pull, ["ENG-1"], pull.activities, self.stats)
        record = next(o for o in self.stats.objects if o["object_type"] == "PR")
        self.assertNotIn("source_ref", record["attributes"])
        self.assertNotIn("target_ref", record["attributes"])

    def test_the_branch_name_never_reaches_the_object(self):
        record = self.reduce(1, "feature/ENG-1-base", "main")
        serialised = json.dumps(record, default=str)
        self.assertNotIn("feature/ENG-1-base", serialised)
        self.assertNotIn("refs/heads", serialised)


class VerifyRefTests(unittest.TestCase):
    def _verify_with(self, attributes: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.duckdb"
            con = connect(path)
            try:
                ensure_schema(con)
                write_objects(con, [{
                    "object_type": "PR",
                    "object_id": "PR:ENG/platform#1",
                    "repo": "ENG/platform",
                    "pr_id": 1,
                    "ticket_keys": ["PROJ-1"],
                    "actor": "AdLo",
                    "state": "open",
                    "created_at": AS_OF,
                    "closed_at": None,
                    "head_commit": "a" * 40,
                    "message_class": None,
                    "attributes": attributes,
                }])
                write_run(con, {
                    "run_id": "fixture-verify-ref",
                    "source": "fixture",
                    "as_of": AS_OF,
                    "source_watermark": AS_OF,
                    "classifier_version": "deterministic-v1",
                    "prs_seen": 1,
                    "prs_kept": 1,
                    "prs_dropped": 0,
                    "events_written": 0,
                    "events_skipped": 0,
                })
                return verify(con)
            finally:
                con.close()

    def test_a_branch_name_in_a_ref_fails_verify(self):
        """The contract is re-asserted by --verify, not just by the reducer."""
        report = self._verify_with({
            "source_ref": digest("feature/ENG-1-base"),
            "target_ref": "DEL-101-add-login",
        })
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("target_ref" in problem for problem in report["problems"]),
            report["problems"],
        )

    def test_the_old_unkeyed_16_hex_digest_fails_verify(self):
        """SEC-1: the reversible shape is no longer a valid ref attribute."""
        old = hashlib.blake2b(f"{REPO}|main".encode(), digest_size=8).hexdigest()
        report = self._verify_with({"source_ref": old})
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("source_ref" in problem for problem in report["problems"]),
            report["problems"],
        )

    def test_a_keyed_digest_passes_verify(self):
        report = self._verify_with({
            "source_ref": digest("feature/ENG-1-base"),
            "target_ref": digest("main"),
        })
        self.assertTrue(report["ok"], report["problems"])


class PepperWiringTests(unittest.TestCase):
    """SEC-1 end to end: the scrape keys the digests from the raw zone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=self.root / "unused.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform")],
            ticket_projects=("ARCH",),
        )
        self.out = self.root / "export" / "bb.tgz"
        self.state_dir = self.root / "export" / "bb.state"

    def tearDown(self):
        self.tmp.cleanup()

    def run_scrape(self) -> dict:
        return scrape(
            self.cfg,
            FixtureTransport(fixtures.generate(n=4, seed=5)),
            "fixture",
            out=self.out,
            state_dir=self.state_dir,
            checkpoint_every=0,
        )

    def test_the_pepper_stays_in_the_raw_zone_and_only_its_id_travels(self):
        report = self.run_scrape()
        pepper_path = self.state_dir / pepper_mod.PEPPER_FILE
        self.assertTrue(pepper_path.is_file())
        self.assertEqual(pepper_path.stat().st_mode & 0o777, 0o600)
        pepper_hex = json.loads(pepper_path.read_text())["pepper"]

        bundle = read_bundle(self.out)
        self.assertEqual(bundle.manifest["ref_digest_key_id"], report["ref_digest_key_id"])
        self.assertEqual(report["ref_digest_pepper"], str(pepper_path))
        # Not the key, nor any prefix of it, in any byte of the tgz.
        raw = gzip.decompress(self.out.read_bytes())
        self.assertNotIn(pepper_hex.encode(), raw)
        self.assertNotIn(pepper_hex[:16].encode(), raw)

        # The refs are 128-bit keyed digests in the new shape.
        prs = [obj for obj in bundle.objects if obj["object_type"] == "PR"]
        self.assertTrue(prs)
        digests = [
            pr["attributes"][attribute]
            for pr in prs
            for attribute in ("source_ref", "target_ref")
            if attribute in pr["attributes"]
        ]
        self.assertTrue(digests)
        for value in digests:
            self.assertRegex(value, taxonomy.REF_DIGEST)
            self.assertEqual(len(value), 32)

    def test_a_replaced_pepper_is_refused_instead_of_silently_rekeying(self):
        self.run_scrape()
        pepper_path = self.state_dir / pepper_mod.PEPPER_FILE
        pepper_path.write_text(json.dumps({"pepper": (b"\x01" * 32).hex()}))
        with self.assertRaises(SystemExit) as ctx:
            self.run_scrape()
        self.assertIn("pepper changed", str(ctx.exception))

    def test_the_same_branch_in_two_state_dirs_gets_unrelated_digests(self):
        first = self.run_scrape()
        report = scrape(
            self.cfg,
            FixtureTransport(fixtures.generate(n=4, seed=5)),
            "fixture",
            out=self.root / "export" / "other.tgz",
            state_dir=self.root / "export" / "other.state",
            checkpoint_every=0,
        )
        self.assertNotEqual(first["ref_digest_key_id"], report["ref_digest_key_id"])
        refs_a = _refs(read_bundle(self.out))
        refs_b = _refs(read_bundle(self.root / "export" / "other.tgz"))
        self.assertTrue(refs_a)
        self.assertFalse(refs_a & refs_b)


def _refs(bundle) -> set[str]:
    return {
        obj["attributes"][attribute]
        for obj in bundle.objects
        if obj["object_type"] == "PR"
        for attribute in ("source_ref", "target_ref")
        if attribute in obj["attributes"]
    }


if __name__ == "__main__":
    unittest.main()

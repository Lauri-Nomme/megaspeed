"""One identity map for both sources (prd/03 "Identity resolution").

A Jira assignee and a git author are the same human, and downstream must see one
pseudonym for them (prd/02). The table keys Bitbucket actors by ``bb:<slug>`` and
Jira actors by ``jira:<username>``; the first Jira sighting of a person links to
the existing Bitbucket entry when the records obviously describe the same human,
and an ambiguous match allocates a fresh pseudonym rather than merging two
people.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ingest.pseudonym import (
    BITBUCKET,
    JIRA,
    IdentityTable,
    actor_key,
    base_pseudonym,
    jira_actor_key,
)

LAURI_BB = {
    "name": "lauri.nomme",
    "displayName": "Lauri Nomme",
    "emailAddress": "lauri.nomme@example.invalid",
}
LAURI_JIRA = {
    "name": "lauri.nomme",
    "key": "JIRAUSER10102",
    "displayName": "Lauri Nomme",
    "emailAddress": "lauri.nomme@example.invalid",
}
CAROL_JIRA = {
    "name": "carol",
    "key": "JIRAUSER10103",
    "displayName": "Carol Osei",
    "emailAddress": "carol.osei@delivery.example",
}


class KeyTests(unittest.TestCase):
    def test_keys_are_namespaced_per_source(self):
        self.assertEqual(actor_key(LAURI_BB), "bb:lauri.nomme")
        self.assertEqual(jira_actor_key(LAURI_JIRA), "jira:lauri.nomme")
        # the installation-local numeric key is never used: it would break
        # pseudonym continuity across a re-seeded lab
        self.assertNotIn("JIRAUSER", jira_actor_key(LAURI_JIRA))

    def test_email_local_part_is_the_fallback(self):
        self.assertEqual(actor_key({"emailAddress": "a.b@corp.invalid"}), "bb:a.b")
        self.assertEqual(jira_actor_key({"emailAddress": "a.b@corp.invalid"}), "jira:a.b")


class LinkingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "identity.json"
        self.table = IdentityTable(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_base_scheme_is_unchanged(self):
        self.assertEqual(base_pseudonym("lauri", "nomme"), "LaNo")
        self.assertEqual(self.table.pseudonym(LAURI_BB), "LaNo")

    def test_jira_actor_links_to_the_bitbucket_identity(self):
        bitbucket = self.table.pseudonym(LAURI_BB)
        jira = self.table.pseudonym(LAURI_JIRA, source=JIRA)
        self.assertEqual(jira, bitbucket)
        self.assertEqual(self.table.pseudonym_jira(LAURI_JIRA), "LaNo")
        entry = self.table.entries["jira:lauri.nomme"]
        self.assertEqual(entry["linked_to"], "bb:lauri.nomme")
        self.assertEqual(entry["pseudonym"], "LaNo")

    def test_link_survives_a_reload(self):
        self.table.pseudonym(LAURI_BB)
        self.table.pseudonym(LAURI_JIRA, source=JIRA)
        self.table.save()
        reloaded = IdentityTable(self.path)
        self.assertEqual(reloaded.pseudonym_jira(LAURI_JIRA), "LaNo")
        self.assertEqual(reloaded.pseudonym(LAURI_BB), "LaNo")
        # and saving again is a no-op: the link was already recorded
        reloaded.save()
        self.assertFalse(reloaded.dirty)

    def test_link_by_email_local_part(self):
        self.table.pseudonym(LAURI_BB)
        jira = {
            "name": "lnomme",  # a different username, same e-mail
            "displayName": "Lauri N",
            "emailAddress": "lauri.nomme@example.invalid",
        }
        self.assertEqual(self.table.pseudonym(jira, source=JIRA), "LaNo")

    def test_link_by_display_name(self):
        self.table.pseudonym(LAURI_BB)
        jira = {"name": "ln", "displayName": "Lauri Nomme", "emailAddress": ""}
        self.assertEqual(self.table.pseudonym(jira, source=JIRA), "LaNo")

    def test_an_unknown_jira_actor_gets_its_own_pseudonym(self):
        self.table.pseudonym(LAURI_BB)
        self.assertEqual(self.table.pseudonym(CAROL_JIRA, source=JIRA), "CaOs")
        # ... and is not a Bitbucket entry
        self.assertNotIn("bb:carol", self.table.entries)
        self.assertEqual(
            {key for key in self.table.entries if key.startswith("jira:")},
            {"jira:carol"},
        )

    def test_an_ambiguous_match_allocates_instead_of_merging(self):
        # Two Bitbucket people share a display name; a Jira account that only
        # matches on that name must not be silently attached to either.
        self.table.pseudonym({"name": "a.one", "displayName": "Alex Kim"})
        self.table.pseudonym({"name": "b.two", "displayName": "Alex Kim"})
        pseudonym = self.table.pseudonym(
            {"name": "alex.kim", "displayName": "Alex Kim"}, source=JIRA
        )
        self.assertEqual(pseudonym, "AlKi3")
        self.assertNotIn("linked_to", self.table.entries["jira:alex.kim"])

    def test_jira_first_then_bitbucket_still_links(self):
        # The order of the two scrapes must not matter for the result.
        jira_first = self.table.pseudonym(CAROL_JIRA, source=JIRA)
        self.assertEqual(jira_first, "CaOs")
        bitbucket = self.table.pseudonym(
            {"name": "carol", "displayName": "Carol Osei", "emailAddress": ""}
        )
        self.assertEqual(bitbucket, "CaOs")

    def test_identity_file_never_contains_anything_but_the_mapping(self):
        self.table.pseudonym(LAURI_BB)
        self.table.pseudonym(CAROL_JIRA, source=JIRA)
        self.table.save()
        payload = json.loads(self.path.read_text())
        self.assertEqual(set(payload), {"_comment", "actors"})
        self.assertIn("RAW ZONE ONLY", payload["_comment"])


if __name__ == "__main__":
    unittest.main()

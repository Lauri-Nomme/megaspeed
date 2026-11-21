"""prd/64 G3 — scope correctness: who a row actually waits on (F-07).

The delivery row's ``waiting_for`` must name the holder of the ball *now*. With
several holders it is the role label; ``waiting_on`` and ``waiting_roles`` carry
the real people. The bug leaked an **ended** interval's owner (CaOs) into
``waiting_for`` whenever the unobserved tail had two holders (#66 waits on
BoFe + FrMo), so ``person:CaOs`` listed a PR CaOs does not hold.

The client half of the contract (``matchesScope`` / ``doneOpenMarkup``) is
pinned in ``app/test/prd64scope.test.mjs``; the same assertions run against the
PRD evidence store when it is present (``MEGASPEED_DB`` or
``data/del-combined.duckdb``).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from analysis import ballcourt, calendar, jira, tickets
from ingest.store import connect
from tests.test_review_model import Store
from tests.test_yield import _del_combined_path

ROLE_LABELS = {"author", "review", "ready", "sentinel", "unknown"}


def holders(row: dict) -> list[str]:
    """The scope's holder set — the client's rule, in Python for the pin."""
    waiting_on = row.get("waiting_on") or []
    if waiting_on:
        return list(waiting_on)
    return [row["waiting_for"]] if row.get("waiting_for") else []


class MultiHolderTailTests(unittest.TestCase):
    """A measured interval owned by CaOs, then a two-holder unobserved tail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def court(self, store: Store) -> ballcourt.BallCourt:
        con = connect(store.write(self.path), read_only=True)
        try:
            return ballcourt.load(con)
        finally:
            con.close()

    def fixture(self) -> Store:
        # CaOs is the peer reviewer; FrMo the sentinel. CaOs asks for changes
        # at t13 — the ball is back with the author (AuAu) while FrMo still
        # owes a verdict: two holders, and the wait now belongs to nobody by
        # name. The interval that just ended was CaOs's alone.
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "CaOs")
        store.sentinel(1, "FrMo")
        store.event(1, "pr_needs_work", "CaOs", 13)
        return store

    def test_the_multi_holder_tail_keeps_the_role_label_not_the_ended_owner(self):
        court = self.court(self.fixture())
        ended = [
            interval for interval in court.intervals
            if interval.measured and interval.ball != "ready"
        ]
        self.assertTrue(any(interval.owner == "CaOs" for interval in ended),
                        "the fixture needs a measured interval CaOs owned")
        row = ballcourt.delivery_list(court)["rows"][0]
        self.assertEqual(row["ball"], "author")
        self.assertEqual(row["waiting_on"], ["AuAu", "FrMo"])
        self.assertEqual(row["waiting_roles"],
                         {"AuAu": "author", "FrMo": "sentinel"})
        # the fix: never the ended interval's owner; the label names the set
        self.assertEqual(row["waiting_for"], "author")
        self.assertIn(row["waiting_for"], ROLE_LABELS)
        self.assertNotIn("CaOs", holders(row))
        self.assertEqual(holders(row), ["AuAu", "FrMo"])

    def test_a_row_the_ended_owner_holds_is_still_theirs(self):
        # CaOs is the only holder, so their scope keeps the row.
        store = Store()
        store.pr(2, "AuAu")
        store.roster(2, "CaOs")
        row = ballcourt.delivery_list(self.court(store))["rows"][0]
        self.assertEqual(row["waiting_on"], ["CaOs"])
        self.assertEqual(row["waiting_for"], "CaOs")
        self.assertIn("CaOs", holders(row))

    def test_a_single_holder_tail_still_names_the_person(self):
        # the guard on the fix: only the *multi*-holder case loses the name
        store = Store()
        store.pr(3, "AuAu")
        store.roster(3, "RArA")
        store.sentinel(3, "FrMo")
        store.event(3, "pr_approved", "RArA", 2)
        row = ballcourt.delivery_list(self.court(store))["rows"][0]
        self.assertEqual(row["waiting_on"], ["FrMo"])
        self.assertEqual(row["waiting_for"], "FrMo")
        self.assertEqual(row["waiting_roles"], {"FrMo": "sentinel"})


@unittest.skipUnless(_del_combined_path() is not None,
                     "the del-combined store is not present")
class DelCombinedScopeTests(unittest.TestCase):
    """The QA evidence: #66 waits on BoFe + FrMo, never on CaOs."""

    @classmethod
    def setUpClass(cls):
        path = _del_combined_path()
        cls.calendar = calendar.load(
            config_path=Path(__file__).resolve().parent.parent / "config"
            / "calendar.default.yaml"
        )
        con = connect(path, read_only=True)
        try:
            cls.court = ballcourt.load(con, calendar=cls.calendar)
            cls.data = jira.load(con, cls.court)
        finally:
            con.close()
        cls.delivery = ballcourt.delivery_list(cls.court)
        cls.rows = cls.delivery["rows"]

    def row(self, number: int) -> dict:
        matches = [row for row in self.rows if row["number"] == number]
        self.assertEqual(len(matches), 1, f"PR #{number} is not in the list")
        return matches[0]

    def test_pr_66_is_not_waiting_on_caos(self):
        row = self.row(66)
        self.assertNotIn("CaOs", holders(row))
        self.assertIn(row["waiting_for"], ROLE_LABELS)
        self.assertIn("BoFe", row["waiting_on"])
        self.assertIn("FrMo", row["waiting_on"])
        self.assertEqual(row["waiting_roles"],
                         {"BoFe": "author", "FrMo": "sentinel"})

    def test_a_row_caos_holds_is_in_their_scope(self):
        caos_rows = [row for row in self.rows if "CaOs" in holders(row)]
        self.assertTrue(caos_rows, "the store has no row waiting on CaOs")
        self.assertNotIn(66, [row["number"] for row in caos_rows])

    def test_the_three_done_open_prs_all_carry_a_sentinel_role(self):
        done = tickets.done_but_open(self.court, self.data, self.delivery)
        self.assertEqual(done["count"], 3)
        numbers = sorted(row["number"] for row in done["rows"])
        self.assertEqual(numbers, [53, 55, 66])
        by_number = {row["number"]: row for row in self.rows}
        for row in done["rows"]:
            court_row = by_number[row["number"]]
            roles = court_row["waiting_roles"]
            self.assertIn("sentinel", roles.values(),
                          f"#{row['number']} has no sentinel holder")

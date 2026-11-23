"""prd/70 U4 — the verdict card's ticket (F-03), ranking (F-05) and holder clock (F-06).

Three QA findings touch ``#verdict``:

* **F-03** — ``analysis/verdict.py`` picked ``ticket_keys[-1]``, so the card
  labelled ``#11096`` with a story closed in 2023 while ``constraint.items``,
  ``holders.current`` and ``adjacent.unmerged`` of the *same* response could
  disagree. The verdict now calls ``ballcourt.primary_ticket`` with the store's
  own ``ticket_maps``, the helper Aging and the ticket page already use.
* **F-05** — the "Next:" footer prints both measures now (hours owed now, then
  the reference removable pool) and says so, so it can no longer show
  ``verification 634 h`` above ``refinement ≈2.3k h`` under a caption claiming
  one ranking. The client half is pinned in
  ``app/test/prd70-u4-verdict.test.mjs``.
* **F-06** — ``holders()`` summed every interval the store ever held, so a
  ``1w``/``4w``/``1q`` card printed a five-year total. The person hours are now
  clipped to the card's own window (``All`` keeps the store-wide figure).

Every number here is exact: the store is hand-written, there is no calendar, so
one elapsed hour is one business hour.

    .venv/bin/python -m unittest tests.test_prd70_u4_verdict -v
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from analysis import verdict
from tests.test_aging import AS_OF, Fixture, StoreCase
from tests.test_prd70_u1_attribution import add_epic

DAY = timedelta(days=1)


# --------------------------------------------------------------------------- #
# F-03 — one canonical ticket, and every verdict surface quotes it
# --------------------------------------------------------------------------- #
def canonical_store() -> Fixture:
    """The three multi-key shapes the QA named, plus the canonical helper's data.

    ``#30`` = EPIC + STORY (the QA's ``DEMO-56715``/``DEMO-57507``);
    ``#96`` = open STORY + a 2023-done STORY (``DEMO-60130``/``DEMO-9714``);
    ``#77`` = open STORY + the EPIC listed *last* (``keys[-1]`` would take the
    epic). All three sit in review at the snapshot.
    """
    fx = Fixture()
    add_epic(fx, "ENG-56715", AS_OF - 60 * DAY)
    # #30: the epic key is listed first, the story second
    fx.story("ENG-57507", AS_OF - 30 * DAY, done=AS_OF - 2 * DAY)
    fx.pr(30, AS_OF - 5 * DAY, tickets=("ENG-56715", "ENG-57507"))
    fx.reviewed(30)
    # #96: an open story next to one closed in 2023 — keys[-1] took the old one
    fx.story("ENG-60130", AS_OF - 3 * DAY)
    fx.story("ENG-9714", AS_OF - 900 * DAY, done=AS_OF - 880 * DAY)
    fx.pr(96, AS_OF - 3 * DAY, tickets=("ENG-60130", "ENG-9714"))
    fx.reviewed(96)
    # #77: the epic is listed last — keys[-1] took the epic
    fx.story("ENG-57508", AS_OF - 20 * DAY)
    fx.pr(77, AS_OF - 1 * DAY, tickets=("ENG-57508", "ENG-56715"))
    fx.reviewed(77)
    return fx


class VerdictTicketTests(StoreCase):
    """F-03: the verdict's item, holder row and Done-open row agree."""

    def setUp(self):
        super().setUp()
        _fixture, self.court, self.data = self.load(canonical_store())
        self.payload = verdict.verdict(
            self.court, self.data, (AS_OF - 90 * DAY, AS_OF),
        )
        self.items = {item["pr"]: item for item in self.payload["exploit"]["items"]}
        self.current = {
            row["pr"]: row for row in self.payload["holders"]["current"]
        }

    def test_a_story_key_beats_the_epic_key_next_to_it(self):
        self.assertEqual(self.items["#30"]["ticket"], "ENG-57507")
        self.assertEqual(self.items["#30"]["href"], "#ticket/ENG-57507")
        # the epic listed *last* is still not the label
        self.assertEqual(self.items["#77"]["ticket"], "ENG-57508")
        self.assertEqual(self.items["#77"]["href"], "#ticket/ENG-57508")

    def test_the_open_story_beats_the_one_closed_in_2023(self):
        # keys[-1] here is ENG-9714 (the 2023-done story)
        self.assertEqual(self.items["#96"]["ticket"], "ENG-60130")

    def test_the_chase_list_and_the_holder_rows_never_differ(self):
        for pr, item in self.items.items():
            self.assertIn(pr, self.current)
            self.assertEqual(self.current[pr]["ticket"], item["ticket"], pr)
            self.assertEqual(item["href"], f"#ticket/{item['ticket']}", pr)

    def test_the_done_open_row_uses_the_canonical_ticket(self):
        unmerged = next(row for row in self.payload["adjacent"]
                        if row["kind"] == "unmerged")
        # only #30's canonical story is Done while its PR is open
        self.assertEqual([item["key"] for item in unmerged["items"]],
                         ["ENG-57507"])
        self.assertEqual(unmerged["items"][0]["href"], "#ticket/ENG-57507")
        # ... and the liveness count is the same cohort, not a key count
        self.assertEqual(self.payload["liveness"]["unmerged"], unmerged["n"])

    def test_the_maps_are_the_stores_own_rows(self):
        # an epic-only key still resolves: the helper never drops evidence
        self.assertEqual(self.items["#30"]["ticket"], "ENG-57507")
        self.assertNotEqual(self.items["#30"]["ticket"], "ENG-56715")


# --------------------------------------------------------------------------- #
# F-06 — the holder row is the card's window, not the store's five-year total
# --------------------------------------------------------------------------- #
def history_store() -> Fixture:
    """Two long-finished reviews, one last week, one live across the window edge."""
    fx = Fixture()
    # a 672 h review in 2025 (400 d before the snapshot), far outside 7 d
    fx.pr(40, AS_OF - 400 * DAY, state="merged", closed=AS_OF - 372 * DAY)
    fx.reviewed(40, merged_after=672)
    # a 24 h review ten days ago — also outside a 7 d window
    fx.pr(41, AS_OF - 10 * DAY, state="merged", closed=AS_OF - 9 * DAY)
    fx.reviewed(41, merged_after=24)
    # an observed 600 h review that straddles the window edge: opened 30 d ago,
    # the reviewer answered 5 d ago, so the 7 d window holds only its last 48 h
    fx.pr(42, AS_OF - 30 * DAY)
    fx.event(42, "reviewer_changed", "AaAa", 0, people=("ReVw",), change="added")
    fx.event(42, "comment_added", "ReVw", 600, people=("ReVw",))
    return fx


class HolderWindowTests(StoreCase):
    """F-06: ``holders(..., window=None)`` is All; a window clips the hours."""

    def setUp(self):
        super().setUp()
        _fixture, self.court, self.data = self.load(history_store())

    def hours(self, window):
        block = verdict.holders(self.court, [], window)
        return {person["person"]: person["hours"] for person in block["people"]}

    def test_all_keeps_the_store_wide_total(self):
        # 672 h (2025) + 24 h (10 d ago) + 600 h (the review across the edge)
        self.assertEqual(self.hours(None)["ReVw"], 1296.0)

    def test_a_window_clips_the_person_hours_to_the_card(self):
        # only the last 48 h of the 600 h review fall inside 7 d
        self.assertEqual(self.hours((AS_OF - 7 * DAY, AS_OF))["ReVw"], 48.0)

    def test_the_payload_uses_the_cards_window(self):
        short = verdict.verdict(self.court, self.data, (AS_OF - 7 * DAY, AS_OF))
        full = verdict.verdict(self.court, self.data, (AS_OF - 400 * DAY, AS_OF))
        short_hours = {p["person"]: p["hours"] for p in short["holders"]["people"]}
        full_hours = {p["person"]: p["hours"] for p in full["holders"]["people"]}
        self.assertEqual(short_hours["ReVw"], 48.0)
        self.assertEqual(full_hours["ReVw"], 1296.0)
        self.assertTrue(short["holders"]["windowed"])
        self.assertIn("inside this window", short["holders"]["note"])

    def test_the_peer_range_is_windowed_too(self):
        block = verdict.holders(self.court, [], (AS_OF - 7 * DAY, AS_OF))
        self.assertEqual(block["peer_range"], [48.0, 48.0])
        self.assertEqual(block["sentinel_hours"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

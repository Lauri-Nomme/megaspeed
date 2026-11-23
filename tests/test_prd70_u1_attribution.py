"""prd/70 U1 — one canonical ticket per PR (F-03) and one holder story (F-09).

The combined corpus exposed two contradictions that are really one bug each:

* **F-03** — a PR can name several ticket keys. ``#11030`` names an EPIC
  (``DEMO-56715``) next to its STORY (``DEMO-57507``) and ``#11096`` names an
  open STORY (``DEMO-60130``) next to one closed in 2023 (``DEMO-9714``). The
  aging table took ``story_tickets[0]`` (the epic / the open story), the client
  took ``keys[-1]`` (the story / the 2023 story), and the epic link opened a
  page with 0 PRs. One helper — ``analysis.ballcourt.primary_ticket`` — now
  answers for every surface.
* **F-09** — an open PR with no *named* holder printed ``HOLDER = —`` on Aging
  and ``waiting 5 d 0 h on nobody`` on the ticket page while Delivery said
  ``review queue`` / ``ready to merge``. One attribution —
  ``analysis.ballcourt.holder_attribution`` — now answers with the live queue.

The store below is hand-written (the ``tests/test_aging.py`` fixture) so every
number is exact. ``ENG-56715`` is the epic; ``ENG-57507`` its done story;
``ENG-60130`` the open story; ``ENG-9714`` the 2023-done story; ``#93`` is
already reviewed and ready; ``#88``/``#95`` wait on nobody in particular.

    .venv/bin/python -m unittest tests.test_prd70_u1_attribution -v
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from analysis import aging, ballcourt, tickets
from tests.test_aging import AS_OF, StoreCase, Fixture


def add_epic(fx: Fixture, key: str, created) -> Fixture:
    """An EPIC object — the fixture's ``story`` only writes STORY rows."""
    fx.objects.append({
        "object_type": "EPIC",
        "object_id": f"EPIC:{key}",
        "repo": None,
        "pr_id": None,
        "ticket_keys": [key],
        "actor": "AaAa",
        "state": "open",
        "created_at": created,
        "closed_at": None,
        "head_commit": None,
        "message_class": None,
        "attributes": {},
    })
    return fx


def attribution_store() -> Fixture:
    """Four open PRs and the four ticket shapes the QA run named."""
    fx = Fixture()
    add_epic(fx, "ENG-56715", AS_OF - timedelta(days=60))
    # #30 — the QA's DEMO-56715/DEMO-57507: epic + STORY (done) at a time.
    fx.story("ENG-57507", AS_OF - timedelta(days=30), done=AS_OF - timedelta(days=2))
    fx.pr(30, AS_OF - timedelta(days=5), state="open",
          tickets=("ENG-56715", "ENG-57507"))
    # #96 — the QA's DEMO-60130/DEMO-9714: open STORY + a 2023-done STORY.
    fx.story("ENG-60130", AS_OF - timedelta(days=3))
    fx.story("ENG-9714", AS_OF - timedelta(days=900),
             done=AS_OF - timedelta(days=880))
    fx.pr(96, AS_OF - timedelta(days=3), state="open",
          tickets=("ENG-60130", "ENG-9714"))
    # #88 / #95 — no reviewer was ever added: nobody at person resolution.
    fx.story("ENG-57777", AS_OF - timedelta(days=20))
    fx.pr(88, AS_OF - timedelta(days=4), state="open", tickets=("ENG-57777",))
    fx.story("ENG-57778", AS_OF - timedelta(days=20))
    fx.pr(95, AS_OF - timedelta(days=2), state="open", tickets=("ENG-57778",))
    # #93 — reviewed and approved: every reviewer answered, nothing merged.
    fx.story("ENG-58888", AS_OF - timedelta(days=10))
    fx.pr(93, AS_OF - timedelta(days=2), state="open", tickets=("ENG-58888",))
    fx.event(93, "reviewer_changed", "AaAa", 0, people=("ReVw",), change="added")
    fx.event(93, "pr_approved", "ReVw", 1, people=("ReVw",))
    # #65 — names ONLY the epic: no story page, so the epic page must list it.
    fx.pr(65, AS_OF - timedelta(days=7), state="open", tickets=("ENG-56715",))
    return fx


class PrimaryTicketTests(unittest.TestCase):
    """The helper itself: the three stated preferences and the honest None."""

    KINDS = {
        "ENG-56715": "EPIC", "ENG-57507": "STORY",
        "ENG-60130": "STORY", "ENG-9714": "STORY",
    }
    STATES = {
        "ENG-56715": "open", "ENG-57507": "done",
        "ENG-60130": "open", "ENG-9714": "done",
    }

    def test_a_story_key_beats_the_epic_key_next_to_it(self):
        self.assertEqual(
            ballcourt.primary_ticket(
                ["ENG-56715", "ENG-57507"],
                kind_by_key=self.KINDS, state_by_key=self.STATES,
            ),
            "ENG-57507",
        )

    def test_the_open_story_beats_the_story_closed_years_ago(self):
        self.assertEqual(
            ballcourt.primary_ticket(
                ["ENG-60130", "ENG-9714"],
                kind_by_key=self.KINDS, state_by_key=self.STATES,
            ),
            "ENG-60130",
        )

    def test_a_tie_picks_the_most_recent_story(self):
        # two live stories of the same kind: the higher ticket number is newer
        self.assertEqual(
            ballcourt.primary_ticket(
                ["ENG-40000", "ENG-60130"],
                kind_by_key={"ENG-40000": "STORY", "ENG-60130": "STORY"},
                state_by_key={"ENG-40000": "open", "ENG-60130": "open"},
            ),
            "ENG-60130",
        )

    def test_no_keys_is_none_and_an_unknown_key_is_still_usable(self):
        self.assertIsNone(ballcourt.primary_ticket([]))
        self.assertIsNone(ballcourt.primary_ticket([None, "  "]))
        # the store does not know the key: the PR's own evidence is not dropped
        self.assertEqual(ballcourt.primary_ticket(["ENG-99999"]), "ENG-99999")


class AttributionSurfacesTests(StoreCase):
    """F-03 + F-09: the three surfaces answer with the same ticket and holder."""

    def setUp(self):
        super().setUp()
        self.fixture, self.court, self.data = self.load(attribution_store())
        self.delivery_payload = ballcourt.delivery_list(self.court)
        self.delivery = {
            row["number"]: row for row in self.delivery_payload["rows"]
        }
        self.aging = {
            row["pr"]: row for row in aging.payload(
                self.court, self.data, kind="pr", stage="review",
            )["rows"]
        }

    # -- F-03 ------------------------------------------------------------- #
    def test_the_maps_come_from_the_jira_layer(self):
        # the maps are built from the store's own STORY/EPIC rows, no extra query
        kinds, states = ballcourt.ticket_maps(self.data)
        self.assertEqual(kinds.get("ENG-56715"), "EPIC")
        self.assertEqual(kinds.get("ENG-57507"), "STORY")
        self.assertEqual(states.get("ENG-60130"), "open")
        self.assertEqual(states.get("ENG-57507"), "done")

    def test_the_epic_and_the_story_resolve_to_the_story_everywhere(self):
        row = self.aging["#30"]
        self.assertEqual(row["ticket"], "ENG-57507")
        self.assertEqual(row["href"], "#ticket/ENG-57507")
        # the story is Done while the PR is open — the row says so
        self.assertEqual(row["live"], "unmerged")

    def test_two_stories_resolve_to_the_open_one(self):
        self.assertEqual(self.aging["#96"]["ticket"], "ENG-60130")
        self.assertEqual(self.aging["#96"]["live"], "live")

    def test_a_pr_that_names_only_the_epic_links_to_the_epic_page(self):
        row = self.aging["#65"]
        self.assertEqual(row["ticket"], "ENG-56715")
        self.assertEqual(row["href"], "#ticket/ENG-56715")

    def test_the_ticket_page_lists_the_pr_its_row_links_to(self):
        payload = tickets.ticket(self.court, self.data, "ENG-57507")
        self.assertEqual([pr["number"] for pr in payload["prs"]], [30])
        # and the other key is still the store's own join, never a second label
        self.assertEqual(self.aging["#30"]["ticket"], payload["key"])

    def test_the_epic_page_lists_the_prs_that_name_it(self):
        payload = tickets.ticket(self.court, self.data, "ENG-56715")
        self.assertEqual(payload["kind"], "epic")
        numbers = [pr["number"] for pr in payload["prs"]]
        self.assertIn(30, numbers)   # epic next to its story
        self.assertIn(65, numbers)   # epic-only PR: this page is its only link
        self.assertEqual(payload["pr_count"], len(numbers))
        self.assertGreaterEqual(payload["pr_count"], 2)

    def test_the_done_open_panel_uses_the_canonical_ticket(self):
        panel = tickets.done_but_open(
            self.court, self.data, self.delivery_payload,
        )
        keys = [row["key"] for row in panel["rows"]]
        # #30's canonical story is Done while its PR is open
        self.assertIn("ENG-57507", keys)
        # #96's canonical story is the *open* one; the 2023 ticket is not this
        # PR's Done-open fact, so the panel does not claim it
        self.assertNotIn("ENG-9714", keys)
        row = next(row for row in panel["rows"] if row["key"] == "ENG-57507")
        self.assertEqual(row["holder_label"], "review queue")

    # -- F-09 ------------------------------------------------------------- #
    def test_an_unassigned_pr_waits_on_the_review_queue_on_every_surface(self):
        for number in ("#30", "#88", "#95", "#96"):
            row = self.aging[number]
            self.assertEqual(
                [(holder["person"], holder["role"]) for holder in row["holder"]],
                [("review queue", "")],
                number,
            )
            self.assertEqual(self.delivery[int(number[1:])]["holder_label"],
                             "review queue", number)

    def test_a_reviewed_pr_waits_on_the_merge_on_every_surface(self):
        row = self.aging["#93"]
        self.assertEqual([holder["person"] for holder in row["holder"]],
                         ["ready to merge"])
        self.assertEqual(self.delivery[93]["holder_label"], "ready to merge")

    def test_the_ticket_page_says_the_queue_not_nobody(self):
        payload = tickets.ticket(self.court, self.data, "ENG-57507")
        pr = payload["open_prs"][0]
        self.assertEqual(pr["number"], 30)
        self.assertEqual(pr["waiting_on"], ["review queue"])
        self.assertEqual(pr["holder_label"], "review queue")
        self.assertIn("on review queue", payload["answer"])
        self.assertNotIn("nobody", payload["answer"])
        self.assertNotIn("nobody", str(payload["open_prs"]))

    def test_the_ready_pr_on_the_ticket_page_names_the_merge(self):
        payload = tickets.ticket(self.court, self.data, "ENG-58888")
        self.assertEqual(payload["open_prs"][0]["waiting_on"], ["ready to merge"])
        self.assertIn("on ready to merge", payload["answer"])
        self.assertNotIn("nobody", payload["answer"])

    def test_a_named_reviewer_is_still_named(self):
        # prd/40: a single holder is named, never replaced by a queue label
        fixture = Fixture()
        fixture.story("ENG-70000", AS_OF - timedelta(days=10))
        fixture.pr(70, AS_OF - timedelta(days=2), state="open",
                   tickets=("ENG-70000",))
        fixture.event(70, "reviewer_changed", "AaAa", 0,
                      people=("ReVw",), change="added")
        _, court, data = self.load(fixture)
        row = next(
            row for row in aging.payload(court, data, kind="pr", stage="review")["rows"]
            if row["pr"] == "#70"
        )
        self.assertEqual([holder["person"] for holder in row["holder"]], ["ReVw"])
        payload = tickets.ticket(court, data, "ENG-70000")
        self.assertEqual(payload["open_prs"][0]["waiting_on"], ["ReVw"])


if __name__ == "__main__":
    unittest.main()

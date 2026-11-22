"""prd/68 G1 — aging correctness on a store whose stories have **no PRs**.

The live Jira-only corpus (`demo-tlla`, 2,320 stories, 0 PRs) exposed three
aging bugs that a PR-carrying fixture cannot see: every open story is `dead` or
`unobserved`, so the PR-based holder fallback dropped the story's own assignee,
the default `only=live` tab emptied the re-plan feed the hidden note points at,
and the chip labelled the abandoned-only `dead` count as "no live PR".

The fixture here is deliberately PR-free and hand-written (no calendar, so one
business hour is one elapsed hour):

* ``DEL-700`` — created two years ago, no event since: **out** of a 30-day
  window, still owned;
* ``DEL-701`` — created two years ago, a human comment five days ago:
  in-window, `unobserved`, owned by ``LaNo``;
* ``DEL-702`` — created 400 days ago, a human comment 20 days ago: in-window,
  `dead` (past the 10-business-day line), owned by ``MiMe``;
* ``DEL-703`` — created two days ago, no PR, owned by ``LaNo``: in-window,
  `unobserved`.

``only=live`` under a 30-day window keeps **0 rows** and yet must keep the
re-plan feed (701/702/703) and say "103"-style `no_live_pr`, never the
abandoned-only ``dead`` count. Each test fails on `fb046ec`:

    .venv/bin/python -m unittest tests.test_prd68_g1 -v
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from analysis import aging
from tests.test_aging import AS_OF, Fixture, StoreCase


def prless_store() -> Fixture:
    """20 finished stories (a real p85 cohort) and four PR-free open stories."""
    fx = Fixture()
    for index in range(20):
        created = AS_OF - timedelta(days=20) + timedelta(hours=index)
        fx.story(
            f"DEL-6{index:02d}", created,
            in_review=created + timedelta(hours=2),
            done=created + timedelta(hours=10 + index),
        )
    # out of the 30-day window: nothing has moved since creation, two years ago
    fx.story("DEL-700", AS_OF - timedelta(days=730), actor="LaNo")
    # in-window and unobserved: a human commented one day ago
    fx.story(
        "DEL-701", AS_OF - timedelta(days=730), actor="LaNo",
        events=[(AS_OF - timedelta(days=1), "comment_added", None, "LaNo")],
    )
    # in-window and dead: the last human event is 20 calendar days old (far past
    # the 10-business-day line), so the work is abandoned while in the window
    fx.story(
        "DEL-702", AS_OF - timedelta(days=400), actor="MiMe",
        events=[(AS_OF - timedelta(days=20), "comment_added", None, "MiMe")],
    )
    # in-window by creation alone, unobserved, the same owner as 701
    fx.story("DEL-703", AS_OF - timedelta(days=2), actor="LaNo")
    return fx


def holders_of(rows, person: str):
    """The rows whose holder set names ``person`` — the page's person scope."""
    return [
        row for row in rows
        if any(holder.get("person") == person for holder in row.get("holder") or ())
    ]


class HolderTests(StoreCase):
    """prd/68 F-03: a story with no PR is held by its Jira assignee."""

    def test_a_no_pr_story_row_names_its_assignee_as_the_owner(self):
        fixture, court, data = self.load(prless_store())
        rows = {row["ticket"]: row for row in aging.in_flight(court, data, "story")}
        for ticket, person in (("DEL-700", "LaNo"), ("DEL-701", "LaNo"),
                               ("DEL-702", "MiMe"), ("DEL-703", "LaNo")):
            with self.subTest(ticket=ticket):
                row = rows[ticket]
                self.assertIn(row["live"], ("dead", "unobserved"))
                self.assertEqual(row["open_prs"], [])
                self.assertEqual(row["holder"], [{
                    "person": person, "role": "owner",
                    "hours": row["age_hours"],
                }])
        # the whole store: no row is anonymous any more
        self.assertFalse([row for row in rows.values() if not row["holder"]])

    def test_a_person_scope_includes_the_no_pr_stories(self):
        fixture, court, data = self.load(prless_store())
        rows = aging.in_flight(court, data, "story")
        self.assertEqual(
            sorted(row["ticket"] for row in holders_of(rows, "LaNo")),
            ["DEL-700", "DEL-701", "DEL-703"],
        )
        self.assertEqual([row["ticket"] for row in holders_of(rows, "MiMe")],
                         ["DEL-702"])
        self.assertEqual(holders_of(rows, "NoOn"), [])

    def test_a_story_with_an_open_pr_keeps_the_pr_holder_not_a_second_owner(self):
        # the fallback never adds the assignee on top of a measured holder
        fixture = prless_store()
        fixture.story("DEL-704", AS_OF - timedelta(days=730), actor="LaNo")
        fixture.pr(970, AS_OF - timedelta(days=30), state="open",
                   tickets=("DEL-704",))
        fixture.event(970, "reviewer_changed", "ReVw", 1, people=("ReVw",),
                      change="added")
        fixture, court, data = self.load(fixture)
        row = next(
            row for row in aging.in_flight(court, data, "story")
            if row["ticket"] == "DEL-704"
        )
        self.assertEqual([holder["person"] for holder in row["holder"]], ["ReVw"])
        self.assertNotIn("LaNo", [holder["person"] for holder in row["holder"]])


class ReplanTests(StoreCase):
    """prd/68 F-06/F-08: the re-plan feed and the "no live PR" count agree."""

    def window(self, court, data):
        return aging.window_for(court, data, 30)

    def test_only_live_keeps_the_window_scoped_replan_feed(self):
        fixture, court, data = self.load(prless_store())
        live = aging.payload(court, data, kind="story", only="live",
                             window=self.window(court, data))
        self.assertEqual(live["rows"], [])
        # the feed is window-scoped: DEL-700 (outside) is not an item here
        self.assertEqual([item["ticket"] for item in live["feed"]["replan"]],
                         ["DEL-701", "DEL-702", "DEL-703"])
        # the hidden note's "— see re-plan" now names a section that renders
        self.assertIn("see re-plan", live["hidden"]["note"])
        self.assertEqual(live["counts"]["no_live_pr"],
                         len(live["feed"]["replan"]))

    def test_the_replan_items_carry_the_holder_the_rows_do(self):
        fixture, court, data = self.load(prless_store())
        live = aging.payload(court, data, kind="story", only="live",
                             window=self.window(court, data))
        by_ticket = {item["ticket"]: item for item in live["feed"]["replan"]}
        self.assertEqual(by_ticket["DEL-701"]["holder"], [{
            "person": "LaNo", "role": "owner",
            "hours": by_ticket["DEL-701"]["age_hours"],
        }])
        self.assertEqual(by_ticket["DEL-702"]["kind"], "story")
        self.assertEqual(by_ticket["DEL-702"]["live"], "dead")

    def test_no_live_pr_counts_dead_and_unobserved_while_dead_stays_abandoned(self):
        fixture, court, data = self.load(prless_store())
        window = self.window(court, data)
        for only in ("all", "live"):
            with self.subTest(only=only):
                payload = aging.payload(court, data, kind="story", only=only,
                                        window=window)
                counts = payload["counts"]
                on_screen = payload["rows"]
                # dead + unobserved: the re-plan feed's own population
                self.assertEqual(counts["no_live_pr"], 3)
                self.assertEqual(counts["no_live_pr"],
                                 len(payload["feed"]["replan"]))
                # `dead` is the abandoned-only number, never "no live PR"
                self.assertEqual(
                    counts["dead"],
                    len([row for row in on_screen if row["live"] == "dead"]),
                )
        everything = aging.payload(court, data, kind="story", only="all",
                                   window=window)
        self.assertEqual(everything["counts"]["dead"], 1)          # DEL-702
        self.assertEqual(
            everything["counts"]["no_live_pr"],
            len([row for row in everything["rows"]
                 if row["live"] in ("dead", "unobserved")]),       # 1 + 2
        )

    def test_only_all_is_unchanged_by_the_feed_fix(self):
        fixture, court, data = self.load(prless_store())
        window = self.window(court, data)
        payload = aging.payload(court, data, kind="story", only="all",
                                window=window)
        self.assertEqual([row["ticket"] for row in payload["rows"]],
                         ["DEL-701", "DEL-702", "DEL-703"])
        self.assertEqual([item["ticket"] for item in payload["feed"]["replan"]],
                         ["DEL-701", "DEL-702", "DEL-703"])
        self.assertEqual(payload["counts"]["open"], 3)
        self.assertEqual(payload["counts"]["dead"], 1)
        self.assertEqual(payload["hidden"]["no_live_pr"], 0)


if __name__ == "__main__":
    unittest.main()

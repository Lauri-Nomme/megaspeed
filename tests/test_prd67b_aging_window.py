"""prd/67 W-03 — the aging rows are window-scoped, not all-time.

The live Jira-only corpus returned **239 rows** in a 90-day view (all
``dead``/``unobserved``, ages up to 1,243 calendar days) because
``_story_rows`` returned every open story and the window only shaped the
percentile cohort. The regression here is a two-speed fixture, hand-written:

* ``DEL-400`` — open since two years ago but with an open PR and a human event
  inside the last 30 days: in the window, and chasable;
* ``DEL-401`` — open since two years ago with no human event since: out of the
  window, and out of the re-plan feed;
* ``DEL-402`` — old and dead (no PR, last human 5 days ago) but *active* in the
  window: still shown, still under re-plan (the feed is windowed, not empty);
* ``DEL-403`` — created three days ago: in the window by creation alone.

``days=30`` keeps 400/402/403, hides 401 and says so; ``days=all`` (no window)
keeps all four; ``only=live`` keeps the live row only and counts the rest as
hidden. The percentile cohort is untouched (it already used the window).

    .venv/bin/python -m unittest tests.test_prd67b_aging_window -v
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from analysis import aging
from tests.test_aging import AS_OF, StoreCase, T0, Fixture


def window_store() -> Fixture:
    """20 finished stories inside 30 days, plus one row per window edge case."""
    fx = Fixture()
    # the 30-day cohort: cycles 10..29 h, done 20..1 days before the snapshot
    for index in range(20):
        created = AS_OF - timedelta(days=20) + timedelta(hours=index)
        fx.story(
            f"DEL-5{index:02d}", created,
            in_review=created + timedelta(hours=2),
            done=created + timedelta(hours=10 + index),
        )
    # live: created long ago, open PR, human work five days ago
    fx.story(
        "DEL-400", AS_OF - timedelta(days=730),
        events=[(AS_OF - timedelta(days=5), "comment_added", None, "AaAa")],
    )
    fx.pr(900, AS_OF - timedelta(days=6), state="open", tickets=("DEL-400",))
    # dead and stale: no PR, no human event since creation two years ago
    fx.story("DEL-401", AS_OF - timedelta(days=730))
    # dead but active: no PR, last human five days ago (inside the window)
    fx.story(
        "DEL-402", AS_OF - timedelta(days=730),
        events=[(AS_OF - timedelta(days=5), "comment_added", None, "AaAa")],
    )
    # created inside the window, no PR: in by creation alone
    fx.story("DEL-403", AS_OF - timedelta(days=3))
    return fx


class WindowScopeTests(StoreCase):
    def test_a_thirty_day_view_drops_the_story_that_did_not_move_in_it(self):
        fixture, court, data = self.load(window_store())
        window = aging.window_for(court, data, 30)
        rows = aging.in_flight(court, data, "story", window=window)
        self.assertEqual(
            [row["ticket"] for row in rows], ["DEL-400", "DEL-402", "DEL-403"],
        )
        # the age is still the censored age to the snapshot, not to the window
        self.assertGreater(rows[0]["calendar_days"], 700)
        self.assertEqual(rows[0]["live"], "live")
        self.assertEqual(rows[1]["live"], "dead")
        self.assertEqual(rows[2]["live"], "unobserved")

        # the percentile cohort is the same windowed cohort as before (unchanged)
        self.assertEqual(aging.story_thresholds(court, data, window)["story_cycle"]["p85"],
                         26.0)

    def test_the_payload_counts_and_the_replan_feed_follow_the_window(self):
        fixture, court, data = self.load(window_store())
        window = aging.window_for(court, data, 30)
        payload = aging.payload(court, data, kind="story", window=window)
        self.assertEqual([row["ticket"] for row in payload["rows"]],
                         ["DEL-400", "DEL-402", "DEL-403"])
        self.assertEqual(payload["counts"]["open"], 3)
        self.assertEqual(payload["counts"]["live"], 1)
        self.assertEqual(payload["counts"]["dead"], 1)
        # the re-plan feed is windowed too: DEL-401 is not an all-time item here
        self.assertEqual([item["ticket"] for item in payload["feed"]["replan"]],
                         ["DEL-402", "DEL-403"])
        self.assertEqual([item["ticket"] for item in payload["feed"]["items"]],
                         ["DEL-400"])
        # and the view says how many rows it hid
        self.assertEqual(payload["hidden"]["outside_window"], 1)
        self.assertEqual(payload["hidden"]["no_live_pr"], 0)
        self.assertEqual(payload["hidden"]["total"], 1)
        self.assertEqual(payload["hidden"]["note"],
                         "1 open item is outside this window — see re-plan")

    def test_all_or_zero_days_is_the_whole_store(self):
        fixture, court, data = self.load(window_store())
        for days in (None, 0):
            with self.subTest(days=days):
                payload = aging.payload(
                    court, data, kind="story", window=aging.window_for(court, data, days),
                )
                self.assertEqual([row["ticket"] for row in payload["rows"]],
                                 ["DEL-400", "DEL-401", "DEL-402", "DEL-403"])
                self.assertEqual(payload["counts"]["open"], 4)
                self.assertEqual(payload["hidden"]["total"], 0)
                self.assertIsNone(payload["hidden"]["note"])
                self.assertEqual(
                    [item["ticket"] for item in payload["feed"]["replan"]],
                    ["DEL-401", "DEL-402", "DEL-403"],
                )

    def test_only_live_filters_after_the_window_and_counts_what_it_hides(self):
        fixture, court, data = self.load(window_store())
        window = aging.window_for(court, data, 30)
        live = aging.payload(court, data, kind="story", only="live", window=window)
        self.assertEqual([row["ticket"] for row in live["rows"]], ["DEL-400"])
        self.assertEqual(live["counts"]["open"], 1)
        self.assertEqual(live["counts"]["dead"], 0)
        # prd/68 F-06: the re-plan feed survives `only=live` (window-scoped):
        # the default tab's "see re-plan" now names rows that render.
        self.assertEqual(
            [item["ticket"] for item in live["feed"]["replan"]],
            ["DEL-402", "DEL-403"],
        )
        self.assertEqual(live["counts"]["no_live_pr"], 2)
        self.assertEqual(live["hidden"]["outside_window"], 1)
        self.assertEqual(live["hidden"]["no_live_pr"], 2)
        self.assertEqual(live["hidden"]["total"], 3)
        self.assertEqual(
            live["hidden"]["note"],
            "3 open items are outside this window or have no live PR — see re-plan",
        )

    def test_no_window_means_no_filter_for_other_callers(self):
        fixture, court, data = self.load(window_store())
        rows = aging.in_flight(court, data, "story")
        self.assertEqual(len(rows), 4)


class HiddenNoteTests(unittest.TestCase):
    def test_the_sentence_is_grammatical_and_absent_when_nothing_is_hidden(self):
        self.assertIsNone(aging.hidden_note(0, 0))
        self.assertEqual(aging.hidden_note(1, 0),
                         "1 open item is outside this window — see re-plan")
        self.assertEqual(aging.hidden_note(0, 1),
                         "1 open item has no live PR — see re-plan")
        self.assertEqual(aging.hidden_note(0, 2),
                         "2 open items have no live PR — see re-plan")
        self.assertEqual(aging.hidden_note(2, 0),
                         "2 open items are outside this window — see re-plan")


if __name__ == "__main__":
    unittest.main()

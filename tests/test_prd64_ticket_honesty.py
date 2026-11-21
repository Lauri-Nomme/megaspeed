"""prd/64 G6 — ticket detail honesty (F-14, F-15) and polish data.

The ticket page must carry the same findings as the views that link to it:

* **F-14** — a PR opened *after* the ticket closed says so on the ticket page
  (``opened 6 d 4 h after the ticket closed — the work cannot have been part of
  the delivered story``), priced on the same business clock the Overview /
  Delivery ``Done in Jira, PR open`` panel uses. A PR opened before the close
  carries nothing.
* **F-15** — a PR approved by *every* reviewer and then closed unmerged is
  stated with its approvers, its closer (the bot when the store names one, and
  no invented bot when it does not) and the hours lost from the final approval.

The fixtures are hand-built timestamps, so every number is exact. The same
assertions run against the PRD evidence store when it is present
(``MEGASPEED_DB`` or ``data/del-combined.duckdb``).

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis import ballcourt, calendar, jira, tickets
from ingest.store import connect
from tests.test_ticket_view import Fixture, StoreCase

T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = datetime(2026, 2, 1, 12, 0)


def findings_fixture() -> Fixture:
    """Four tickets: one PR after the close, two approved-unmerged, one normal."""
    fx = Fixture(as_of=AS_OF)

    # DEL-183: Jira done at +90 h; #65 merged before the close; #66 opened 52 h
    # (6 d 4 h on the 8 h clock) *after* the close and still open.
    fx.story("DEL-183", T0, points=5, state="done",
             closed=T0 + timedelta(hours=90))
    fx.pointed("DEL-183", T0 + timedelta(hours=10), 5)
    fx.status("DEL-183", T0 + timedelta(hours=30), "in_progress")
    fx.status("DEL-183", T0 + timedelta(hours=90), "done",
              from_status="in_progress")
    fx.pr(65, "DaKo", T0 + timedelta(hours=40),
          tickets=("DEL-1", "DEL-183"), closed=T0 + timedelta(hours=80))
    fx.pr_event(65, "pr_merged", "CaOs", 40, tickets=("DEL-1", "DEL-183"))
    fx.pr(66, "BoFe", T0 + timedelta(hours=142),
          tickets=("DEL-1", "DEL-183"), state="open", closed=None)
    fx.pr_event(66, "comment_added", "CaOs", 2, severity="mandatory",
                tickets=("DEL-1", "DEL-183"))

    # DEL-161: open story, #34 approved by DaKo + FrMo and declined by the bot
    # BiBi 187 h (23 d 3 h) after the final approval.
    fx.story("DEL-161", T0, points=2, state="open")
    fx.pointed("DEL-161", T0 + timedelta(hours=5), 2)
    fx.status("DEL-161", T0 + timedelta(hours=10), "in_progress")
    fx._object("PERSON", "PERSON:BiBi", attributes={"actor_kind": "bot"})
    fx.pr(34, "ErLi", T0 + timedelta(hours=20),
          tickets=("DEL-1", "DEL-161"), state="declined",
          closed=T0 + timedelta(hours=20 + 30 + 187))
    fx.pr_event(34, "reviewer_changed", "ErLi", 1, change="added",
                tickets=("DEL-1", "DEL-161"), people=("DaKo",))
    fx.pr_event(34, "reviewer_changed", "ErLi", 1, change="added",
                tickets=("DEL-1", "DEL-161"), people=("FrMo",))
    fx.pr_event(34, "pr_approved", "DaKo", 20, tickets=("DEL-1", "DEL-161"),
                people=("DaKo",))
    fx.pr_event(34, "pr_approved", "FrMo", 30, tickets=("DEL-1", "DEL-161"),
                people=("FrMo",))
    fx.pr_event(34, "pr_declined", "BiBi", 30 + 187,
                tickets=("DEL-1", "DEL-161"), people=("BiBi",))

    # DEL-170: approved by every reviewer, closed without the store naming a
    # closer — the page says "closed unmerged after approval", never a bot.
    fx.story("DEL-170", T0, points=3, state="open")
    fx.pointed("DEL-170", T0 + timedelta(hours=5), 3)
    fx.status("DEL-170", T0 + timedelta(hours=10), "in_progress")
    fx.pr(35, "AlNg", T0 + timedelta(hours=20),
          tickets=("DEL-1", "DEL-170"), state="declined",
          closed=T0 + timedelta(hours=130))
    fx.pr_event(35, "reviewer_changed", "AlNg", 1, change="added",
                tickets=("DEL-1", "DEL-170"), people=("DaKo",))
    fx.pr_event(35, "reviewer_changed", "AlNg", 1, change="added",
                tickets=("DEL-1", "DEL-170"), people=("FrMo",))
    fx.pr_event(35, "pr_approved", "DaKo", 20, tickets=("DEL-1", "DEL-170"))
    fx.pr_event(35, "pr_approved", "FrMo", 30, tickets=("DEL-1", "DEL-170"))
    return fx


class FindingsStoreCase(StoreCase):
    def payload(self, key: str) -> dict:
        (findings_fixture()).write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)          # wall-clock: exact hours
            data = jira.load(con, court)
            payload = tickets.ticket(court, data, key)
        finally:
            con.close()
        self.assertIsNotNone(payload)
        return payload


class OpenedAfterCloseTests(FindingsStoreCase):
    def test_del_183_payload_flags_the_pr_opened_after_the_close(self):
        payload = self.payload("DEL-183")
        rows = {pr["number"]: pr for pr in payload["prs"]}
        self.assertTrue(rows[66]["opened_after_close"])
        self.assertEqual(rows[66]["opened_after_close_hours"], 52.0)
        # the PR opened before the close carries nothing
        self.assertFalse(rows[65]["opened_after_close"])
        self.assertEqual(rows[65]["opened_after_close_hours"], 0.0)

    def test_the_narrative_states_the_after_close_finding_with_its_duration(self):
        payload = self.payload("DEL-183")
        self.assertIn(
            "PR #66 opened 6 d 4 h after the ticket closed — the work cannot "
            "have been part of the delivered story.",
            payload["answer"],
        )
        self.assertNotIn("PR #65 opened", payload["answer"])

    def test_a_normal_merged_pr_has_no_after_close_finding(self):
        payload = self.payload("DEL-170")       # no PR opened after any close
        for pr in payload["prs"]:
            self.assertFalse(pr["opened_after_close"])
            self.assertEqual(pr["opened_after_close_hours"], 0.0)


class ApprovedUnmergedTests(FindingsStoreCase):
    def test_del_161_payload_names_approvers_closer_and_lost_hours(self):
        payload = self.payload("DEL-161")
        pr = payload["prs"][0]
        self.assertEqual(pr["number"], 34)
        finding = pr["approved_unmerged"]
        self.assertIsNotNone(finding)
        self.assertEqual(finding["approved_by"], ["DaKo", "FrMo"])
        self.assertEqual(finding["closed_by"], "BiBi")
        self.assertTrue(finding["bot_closed"])
        self.assertEqual(finding["hours"], 187.0)
        self.assertEqual(finding["closed_at"], "2026-01-15T06:00:00Z")

    def test_the_narrative_names_the_approved_unmerged_close(self):
        answer = self.payload("DEL-161")["answer"]
        self.assertIn(
            "PR #34 was approved by DaKo + FrMo and closed unmerged by the bot "
            "BiBi — 23 d 3 h lost from the final approval.",
            answer,
        )

    def test_no_closer_in_the_store_means_no_invented_bot(self):
        answer = self.payload("DEL-170")["answer"]
        self.assertIn(
            "PR #35 was approved by DaKo + FrMo and closed unmerged after "
            "approval",
            answer,
        )
        self.assertNotIn("bot", answer)

    def test_a_merged_pr_carries_no_approved_unmerged_finding(self):
        # DEL-183 #65 is merged with every reviewer's approval: delivered, not
        # an orphaned approval.
        payload = self.payload("DEL-183")
        rows = {pr["number"]: pr for pr in payload["prs"]}
        self.assertIsNone(rows[65]["approved_unmerged"])


def _del_combined_path() -> Path | None:
    """The PRD's evidence store, when it is present (never a different tenant)."""
    candidates = []
    env = os.environ.get("MEGASPEED_DB")
    if env:
        candidates.append(Path(env))
    candidates.append(
        Path(__file__).resolve().parent.parent / "data" / "del-combined.duckdb"
    )
    for path in candidates:
        if path.name == "del-combined.duckdb" and path.exists():
            return path
    return None


@unittest.skipUnless(
    _del_combined_path() is not None,
    "the del-combined store is not present",
)
class DelCombinedTicketHonestyTests(unittest.TestCase):
    """The QA run's own findings: #66 after the close, #34 after approval."""

    @classmethod
    def setUpClass(cls):
        path = _del_combined_path()
        con = connect(path, read_only=True)
        try:
            cls.court = ballcourt.load(con, calendar=calendar.load())
            cls.data = jira.load(con, cls.court)
        finally:
            con.close()

    def test_del_183_pr_66_opened_6_d_4_h_after_the_close(self):
        payload = tickets.ticket(self.court, self.data, "DEL-183")
        rows = {pr["number"]: pr for pr in payload["prs"]}
        self.assertTrue(rows[66]["opened_after_close"])
        self.assertEqual(rows[66]["opened_after_close_hours"], 52.72)
        self.assertFalse(rows[65]["opened_after_close"])
        self.assertIn(
            "PR #66 opened 6 d 4 h after the ticket closed — the work cannot "
            "have been part of the delivered story.",
            payload["answer"],
        )

    def test_del_161_pr_34_approved_by_peers_and_bot_closed(self):
        payload = tickets.ticket(self.court, self.data, "DEL-161")
        finding = payload["prs"][0]["approved_unmerged"]
        self.assertIsNotNone(finding)
        self.assertEqual(finding["approved_by"], ["DaKo", "FrMo"])
        self.assertEqual(finding["closed_by"], "BiBi")
        self.assertTrue(finding["bot_closed"])
        self.assertEqual(finding["hours"], 187.8)
        self.assertIn(
            "PR #34 was approved by DaKo + FrMo and closed unmerged by the bot "
            "BiBi — 23 d 3 h lost from the final approval.",
            payload["answer"],
        )

    def test_del_161_strip_numbers_are_age_and_measured_sum(self):
        # The strip labels are the UI's; the payload must supply the two
        # different clocks: the age (cycle_hours) and the measured sum.
        segments = tickets.ticket(self.court, self.data, "DEL-161")["segments"]
        self.assertEqual(segments["cycle_hours"], 260.0)
        self.assertEqual(segments["total_hours"], 56.2)
        self.assertLess(segments["measured"], len(segments["rows"]))
        self.assertLess(segments["total_hours"], segments["cycle_hours"])

    def test_del_155_worklog_delay_is_calendar_and_within_a_calendar_week(self):
        # F-22: the bucket is calendar days, so a `within_week` row is <= 7
        # calendar days (168 h) — the page must render it on that clock.
        rows = tickets.ticket(self.court, self.data, "DEL-155")["worklogs"]
        late = next(row for row in rows if row["bucket"] == "within_week")
        self.assertLessEqual(late["log_delay_hours"], 7 * 24)
        self.assertGreater(late["log_delay_hours"], 2 * 24)


if __name__ == "__main__":
    unittest.main()

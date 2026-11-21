"""prd/64 G5 — Rules correctness & polish (F-05, F-06a, F-18, F-19), backend half.

* **F-06a** — the review-round-cap rule asks for a cap at
  ``REVIEW_ROUND_CAP_TARGET`` (2), so the rows it marks ``affected`` must be the
  same rows its evidence counts as ``prs_over_target``. It used to mark
  ``count > MAX_REVIEW_ROUNDS`` (3) and highlighted nothing while the card said
  "24 over target". A PR with exactly 3 rounds is the boundary case: the rule
  is about it, the conformance threshold (3) is not.
* **F-19** — ``attribute_at_stake()`` gave a zero-hour rule an ``attributed_to``
  pointing at itself, so the Markdown export of rule #7 said "counted in #7".
  Only a rule with priced hours, absorbed by a *different* rule, points
  elsewhere; a genuine shared pool still redirects.

The client halves (affected-first drill ordering, the people-tab stats, the
evidence chips and the Markdown guard) are pinned in
``app/test/prd64g5.test.mjs``.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from analysis import rules
from tests.test_rules import Fixture, StoreCase, by_id


class RoundCapAffectedTests(StoreCase):
    """F-06a: `round-cap` marks the same PRs its evidence counts."""

    def fixture(self) -> Fixture:
        # 0, 1, 2, 3 and 4 review rounds; the rule's target is 2, so PRs 4 and
        # 5 are over it and PR 3 is exactly at the strict conformance line (3).
        fx = Fixture()
        for number, rounds in enumerate((0, 1, 2, 3, 4), start=1):
            fx.pr(number, "AaAa")
            fx.event(number, "comment_added", "RrRr", 1, severity="suggestion")
            for index in range(rounds):
                fx.event(number, "pr_pushed", "AaAa", 2 + index,
                         push_class="content")
        return fx

    def test_affected_rows_are_the_over_target_rows(self):
        metric = self.metric(self.fixture(), "rework_loops")
        by_number = {row["number"]: row for row in metric["rows"]}
        # three rounds is over the cap of 2 — the boundary the old `> 3` missed
        self.assertTrue(by_number[4]["affected"])
        self.assertTrue(by_number[5]["affected"])
        self.assertFalse(by_number[3]["affected"])          # exactly 2 rounds
        self.assertFalse(by_number[2]["affected"])          # 1 round
        self.assertFalse(by_number[1]["affected"])          # 0 rounds
        # a PR with 3 rounds is over the *conformance* line too, but the row's
        # `affected` flag is the rule's target, not that stricter line
        self.assertFalse(by_number[4]["over_max_rounds"])   # 3 rounds: not > 3
        self.assertTrue(by_number[5]["over_max_rounds"])    # 4 rounds: > 3

    def test_the_affected_count_matches_the_evidence_and_leads_the_list(self):
        payload = self.insights(self.fixture())
        suggestion = by_id(payload, "round-cap")
        self.assertIsNotNone(suggestion)
        metric = payload["metrics"]["rework_loops"]
        affected = [row for row in metric["rows"] if row["affected"]]
        # the number on the card and the rows it highlights are one number
        self.assertEqual(suggestion["evidence"]["prs_over_target"], 2)
        self.assertEqual(len(affected), suggestion["evidence"]["prs_over_target"])
        self.assertEqual(sorted(row["number"] for row in affected), [4, 5])
        # and the rows the rule is about lead the drill-down
        self.assertEqual([row["number"] for row in metric["rows"][:2]], [4, 5])

    def test_round_cap_keeps_the_conformance_threshold_separate(self):
        metric = self.metric(self.fixture(), "rework_loops")
        # the conformance metric's line (> 3) is reported, never used as the cap
        self.assertEqual(metric["target_rounds"], 2)
        self.assertEqual(metric["max_rounds"], 3)
        self.assertEqual(metric["prs_over_target"], 2)
        self.assertEqual(metric["prs_over_max"], 1)     # only the 4-round PR


class AttributeAtStakeTests(unittest.TestCase):
    """F-19: no self-attribution, and a real shared pool still points."""

    def suggestion(self, sid, impact, sample=10, rank=1):
        return {
            "id": sid,
            "metric": sid.replace("-", "_"),
            "impact_hours": impact,
            "sample": sample,
            "scope": {"kind": "team"},
            "rank": rank,
        }

    def test_a_zero_hour_rule_is_not_attributed_to_itself(self):
        suggestions = [
            self.suggestion("jira-worklog-mismatch", 0.0, sample=85, rank=7),
            self.suggestion("jira-worklog-noise", 0.0, sample=85, rank=8),
        ]
        rules.attribute_at_stake(suggestions)
        for suggestion in suggestions:
            stake = suggestion["at_stake"]
            # nothing was priced, so there is nothing to redirect and no owner
            self.assertEqual(stake["attributed_hours"], 0.0)
            self.assertIsNone(stake["attributed_to"])
            self.assertNotIn("attributed_to_rank", stake)

    def test_a_priced_rule_absorbed_by_another_still_points_there(self):
        suggestions = [
            self.suggestion("rebase-waste", 100.0, sample=5, rank=2),
            self.suggestion("rework-loops", 60.0, sample=10, rank=1),
        ]
        rules.attribute_at_stake(suggestions)
        absorbed = next(s for s in suggestions if s["id"] == "rework-loops")
        # the pool is counted at its broadest estimate; the narrower rule owns
        # all of it, so the broader one says where its hours were counted
        self.assertEqual(absorbed["at_stake"]["attributed_hours"], 0.0)
        self.assertEqual(absorbed["at_stake"]["attributed_to"], "rebase-waste")
        self.assertEqual(absorbed["at_stake"]["attributed_to_rank"], 2)
        self.assertEqual(absorbed["at_stake"]["pool_rules"], 2)


if __name__ == "__main__":
    unittest.main()

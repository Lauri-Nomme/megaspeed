"""Attribution and honesty regressions in the analysis layer.

Pinned here, each against a hand-built store (no fixture generator):

* **product review #7** — a review wait is attributed to *every* pending
  reviewer, not just the first responder. A three-reviewer PR whose slow member
  never answers must show up on that member at person level, while the
  role/queue level still counts the wait once under ``review``.
* **QA round 2, B1** — ``delivery_list``'s ``dependents`` counts other **open**
  PRs on the same **story** ticket: not closed PRs, not the shared epic key, and
  not summed across keys.
* **product review #5** — ``cycle_time`` reports its censoring (open PRs are
  right-censored) instead of presenting a merged-only median as the population.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis import ballcourt, rules
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run

REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)
AS_OF = datetime(2026, 2, 1, 12, 0)


class Store:
    """A synthetic store with explicit events, object links and objects."""

    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
        self._count = 0

    # -- PRs / events ------------------------------------------------------- #
    def pr(self, number: int, author: str, opened: datetime = T0,
           state: str = "open", closed: datetime | None = None,
           tickets: tuple[str, ...] = ("ENG-1",)) -> "Store":
        self.opened[number] = opened
        self.objects.append({
            "object_type": "PR",
            "object_id": f"PR:{REPO}#{number}",
            "repo": REPO,
            "pr_id": number,
            "ticket_keys": list(tickets),
            "actor": author,
            "state": state,
            "created_at": opened,
            "closed_at": closed,
            "head_commit": "a" * 40,
            "message_class": None,
            "attributes": {},
        })
        self.event(number, "pr_opened", author, 0)
        return self

    def event(self, number: int, activity: str, actor: str, hours: float,
              people: tuple[str, ...] = (), **attributes) -> "Store":
        self._count += 1
        ts = self.opened[number] + timedelta(hours=hours)
        key = f"{REPO}#{number}:{self._count}"
        self.events.append({
            "event_key": key,
            "ts": ts,
            "activity": activity,
            "actor": actor,
            "repo": REPO,
            "pr_id": number,
            "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        for person in people:
            self.relations.append((key, "PERSON", f"PERSON:{person}"))
        return self

    def review_request(self, number: int, actor: str, reviewer: str, hours: float) -> "Store":
        """A reviewer-roster addition (the scrape's ``reviewer_changed added``)."""
        return self.event(number, "reviewer_changed", actor, hours,
                          people=(reviewer,), change="added")

    # -- materialise -------------------------------------------------------- #
    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-attribution",
                "source": "fixture",
                "as_of": self.as_of,
                "source_watermark": self.as_of,
                "classifier_version": "deterministic-v1",
                "prs_seen": len(self.opened),
                "prs_kept": len(self.opened),
                "prs_dropped": 0,
                "events_written": len(self.events),
                "events_skipped": 0,
            })
        finally:
            con.close()
        return path

    def court(self, path: Path) -> ballcourt.BallCourt:
        con = connect(path, read_only=True)
        try:
            return ballcourt.load(con)
        finally:
            con.close()

    def threads(self, path: Path) -> list[dict]:
        con = connect(path, read_only=True)
        try:
            return rules.load_threads(con)
        finally:
            con.close()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, store: Store) -> ballcourt.BallCourt:
        return store.court(store.write(self.path))

    @staticmethod
    def edges(payload: dict) -> dict[tuple[str, str], dict]:
        return {(edge["from"], edge["to"]): edge for edge in payload["edges"]}


# --------------------------------------------------------------------------- #
# product review #7: responder vs blocker
# --------------------------------------------------------------------------- #
class MultiReviewerAttributionTests(Case):
    def store(self) -> Store:
        """PR 1 asked RrRr and SsSs; only RrRr ever answers."""
        store = Store()
        store.pr(1, "AaAa")
        store.review_request(1, "AaAa", "RrRr", 1)
        store.review_request(1, "AaAa", "SsSs", 1)
        store.event(1, "pr_approved", "RrRr", 31)
        return store

    def test_every_pending_reviewer_is_charged_the_wait(self):
        court = self.load(self.store())
        graph = ballcourt.blocking_graph(court, level="person")
        edges = self.edges(graph)
        # [1 h, 31 h) waited on both reviewers: 30 h each, not 30 h to the
        # responder and 0 h to the person who never looked.
        self.assertEqual(edges[("AaAa", "RrRr")]["hours"], 30.0)
        self.assertEqual(edges[("AaAa", "SsSs")]["hours"], 30.0)
        self.assertEqual(graph["hours_kind"], "person-hours")

    def test_the_queue_level_still_counts_one_wait_per_pr(self):
        court = self.load(self.store())
        graph = ballcourt.blocking_graph(court, level="queue")
        edges = self.edges(graph)
        self.assertEqual(edges[("author", "review")]["hours"], 31.0)
        self.assertEqual(graph["hours_kind"], "wait-hours")
        self.assertEqual(sum(e["hours"] for e in graph["edges"]), 31.0)

    def test_queue_series_lists_the_pr_in_both_reviewers_queues(self):
        court = self.load(self.store())
        person = ballcourt.queue_series(court, level="person")
        streams = {s["id"]: s["summary"] for s in person["streams"]}
        self.assertEqual(streams["RrRr"]["hours"], 30.0)
        self.assertEqual(streams["SsSs"]["hours"], 30.0)
        self.assertEqual(streams["RrRr"]["items"], 1)
        self.assertEqual(streams["SsSs"]["items"], 1)

        queue = ballcourt.queue_series(court, level="queue")
        review = next(s for s in queue["streams"] if s["id"] == "review")
        self.assertEqual(review["summary"]["hours"], 31.0)
        self.assertEqual(review["summary"]["items"], 1)

    def test_responsiveness_sees_the_non_responder(self):
        court = self.load(self.store())
        window = rules.window_for(court)
        metric = rules.reviewer_responsiveness(court, window)
        people = {p["person"]: p for p in metric["people"]}
        self.assertEqual(people["RrRr"]["review_median_hours"], 30.0)
        # the reviewer who never answered is now visible, with the same wait
        self.assertEqual(people["SsSs"]["review_median_hours"], 30.0)
        self.assertEqual(people["SsSs"]["review_n"], 1)

    def test_a_responder_off_the_roster_is_not_retro_charged(self):
        """prd/40: with a roster, the status vector is the truth.

        The old responder retrofit charged RrRr for the 20 h *before* they
        answered, even though SsSs was the reviewer the PR actually waited on.
        The retrofit now only fills a store with no roster at all (next test).
        """
        store = Store()
        store.pr(1, "AaAa")
        store.review_request(1, "AaAa", "SsSs", 1)
        store.event(1, "pr_approved", "RrRr", 21)      # ad-hoc reviewer
        court = self.load(store)
        edges = self.edges(ballcourt.blocking_graph(court, level="person"))
        self.assertEqual(edges[("AaAa", "SsSs")]["hours"], 20.0)
        self.assertNotIn(("AaAa", "RrRr"), edges)

    def test_without_a_roster_the_responder_is_the_only_holder(self):
        store = Store()
        store.pr(1, "AaAa")
        store.event(1, "pr_approved", "RrRr", 21)
        court = self.load(store)
        graph = ballcourt.blocking_graph(court, level="person")
        edges = self.edges(graph)
        self.assertEqual(edges[("AaAa", "RrRr")]["hours"], 21.0)
        self.assertNotIn(("AaAa", "SsSs"), edges)

    def test_an_open_pr_lists_every_pending_reviewer(self):
        store = Store()
        store.pr(1, "AaAa")
        store.review_request(1, "AaAa", "RrRr", 1)
        store.review_request(1, "AaAa", "SsSs", 1)
        court = self.load(store)
        delivery = ballcourt.delivery_list(court)
        row = delivery["rows"][0]
        self.assertEqual(row["waiting_on"], ["RrRr", "SsSs"])
        # the single "primary" holder is unchanged (no responder yet -> queue)
        self.assertEqual(row["ball_label"], "review")

    def test_a_removed_reviewer_is_no_longer_pending(self):
        store = Store()
        store.pr(1, "AaAa")
        store.review_request(1, "AaAa", "RrRr", 1)
        store.review_request(1, "AaAa", "SsSs", 1)
        store.event(1, "reviewer_changed", "AaAa", 2, people=("SsSs",), change="removed")
        store.event(1, "pr_approved", "RrRr", 22)
        court = self.load(store)
        edges = self.edges(ballcourt.blocking_graph(court, level="person"))
        # SsSs held it for the hour before the removal; after that only RrRr
        self.assertEqual(edges[("AaAa", "SsSs")]["hours"], 1.0)
        self.assertEqual(edges[("AaAa", "RrRr")]["hours"], 21.0)

    def test_a_content_push_does_not_repend_a_standing_approval(self):
        """prd/40 (S1/S2b): a push stales needs_work/reviewed, not an approval.

        The old machine re-pended the whole roster on every push, so RrRr — who
        approved at t11 and whose approval still stood — was charged the rest of
        the PR's life. Only ``approvals_lost`` re-pends an approval.
        """
        store = Store()
        store.pr(1, "AaAa")
        store.review_request(1, "AaAa", "RrRr", 1)
        store.review_request(1, "AaAa", "SsSs", 1)
        store.event(1, "pr_approved", "RrRr", 11)                 # 10 h: both
        store.event(1, "pr_pushed", "AaAa", 20, push_class="content")  # 9 h: SsSs
        store.event(1, "pr_approved", "SsSs", 40)                 # 20 h: SsSs
        court = self.load(store)
        edges = self.edges(ballcourt.blocking_graph(court, level="person"))
        # RrRr is charged only the wait before its approval: the push did not
        # remove the approval, so the reviewer is not re-required.
        self.assertEqual(edges[("AaAa", "RrRr")]["hours"], 10.0)
        self.assertEqual(edges[("AaAa", "SsSs")]["hours"], 30.0)   # 10 + 20
        # between the two approvals the ball was only with SsSs, and RrRr is the
        # party that handed it over: the wait is RrRr -> SsSs
        self.assertEqual(edges[("RrRr", "SsSs")]["hours"], 9.0)
        inbound = sum(e["hours"] for e in ballcourt.blocking_graph(
            court, level="person")["edges"] if e["to"] == "SsSs")
        self.assertEqual(inbound, 39.0)                            # 10 + 9 + 20


# --------------------------------------------------------------------------- #
# QA round 2, B1: dependents
# --------------------------------------------------------------------------- #
class DependentsTests(Case):
    def store(self) -> Store:
        """EP-1 is the epic on every PR; EP-10/EP-11/EP-13 are story keys."""
        store = Store()
        store.pr(1, "AaAa", tickets=("EP-1", "EP-10", "EP-11"))
        store.pr(2, "BbBb", tickets=("EP-1", "EP-10"))
        store.pr(3, "CcCc", tickets=("EP-1", "EP-11"))
        store.pr(5, "DdDd", tickets=("EP-1",))            # epic link only
        store.pr(6, "EeEe", state="merged", closed=T0 + timedelta(hours=10),
                 tickets=("EP-1", "EP-13"))
        store.pr(7, "FfFf", tickets=("EP-1", "EP-13"))
        return store

    def test_dependents_are_open_prs_on_a_story_key(self):
        court = self.load(self.store())
        delivery = ballcourt.delivery_list(court)
        rows = {row["number"]: row for row in delivery["rows"]}
        # PR 1 shares EP-10 with PR 2 and EP-11 with PR 3: two, counted once each
        self.assertEqual(rows[1]["dependents"], 2)
        self.assertEqual(rows[2]["dependents"], 1)
        self.assertEqual(rows[3]["dependents"], 1)
        # the epic key alone is not a dependency
        self.assertEqual(rows[5]["dependents"], 0)
        self.assertEqual(rows[5]["story_tickets"], [])
        # a merged PR cannot be blocked by a wait
        self.assertEqual(rows[7]["dependents"], 0)
        self.assertNotIn(6, rows)

    def test_the_epic_key_is_named_in_the_payload(self):
        delivery = ballcourt.delivery_list(self.load(self.store()))
        self.assertEqual(delivery["parent_keys"], ["EP-1"])
        self.assertIn("parent", delivery["parent_key_note"])
        self.assertIn("OPEN", delivery["impact_note"])

    def test_impact_is_dependents_times_the_current_wait(self):
        delivery = ballcourt.delivery_list(self.load(self.store()))
        row = next(row for row in delivery["rows"] if row["number"] == 1)
        self.assertAlmostEqual(row["impact_hours"], 2 * row["waiting_hours"], places=1)

    def test_the_old_inflation_is_gone(self):
        """Before the fix this store reported 5 dependents (all PRs, epic summed)."""
        delivery = ballcourt.delivery_list(self.load(self.store()))
        self.assertLessEqual(max(row["dependents"] for row in delivery["rows"]), 2)
        self.assertEqual(delivery["counts"]["open"], 5)

    def test_a_small_store_keeps_a_real_shared_ticket(self):
        # 3 PRs on one ticket: too small to call it an epic, so the dependency
        # is reported instead of being guessed away
        store = Store()
        for number in (1, 2, 3):
            store.pr(number, "AaAa", tickets=("EP-1",))
        delivery = ballcourt.delivery_list(self.load(store))
        self.assertEqual(delivery["parent_keys"], [])
        self.assertEqual([row["dependents"] for row in delivery["rows"]], [2, 2, 2])


# --------------------------------------------------------------------------- #
# product review #5: cycle-time censoring
# --------------------------------------------------------------------------- #
class CycleTimeCensoringTests(Case):
    def store(self) -> Store:
        store = Store()
        for number, hours in enumerate((30, 60, 100, 240), start=1):
            store.pr(number, "AaAa", state="merged", closed=T0 + timedelta(hours=hours))
            store.event(number, "pr_merged", "RrRr", hours)
        store.pr(9, "AaAa")                                    # still open
        store.pr(10, "AaAa", state="declined",
                 closed=T0 + timedelta(hours=48))              # never merges
        return store

    def test_the_censored_population_is_reported_next_to_the_median(self):
        court = self.load(self.store())
        window = rules.window_for(court)
        metric = rules.cycle_time(court, window)
        self.assertEqual(metric["n"], 4)
        self.assertEqual(metric["median_hours"], 80.0)
        self.assertEqual(metric["cohort"], 6)
        self.assertEqual(metric["censored"]["open"], 1)
        self.assertEqual(metric["censored"]["declined"], 1)
        self.assertIn("right-censored", metric["censored"]["note"])
        # an open PR's age is a lower bound, and it is stated as one
        expected_age = (court.snapshot - T0).total_seconds() / 3600.0
        self.assertAlmostEqual(
            metric["censored"]["open_median_age_hours"], expected_age, places=1
        )

    def test_the_suggestion_carries_the_censoring(self):
        court = self.load(self.store())
        window = rules.window_for(court)
        payload = rules.insights(court, [], window)
        suggestion = next(s for s in payload["suggestions"] if s["id"] == "cycle-time")
        self.assertEqual(suggestion["evidence"]["merged_prs"], 4)
        self.assertEqual(suggestion["evidence"]["cohort_prs"], 6)
        self.assertEqual(suggestion["evidence"]["open_censored_prs"], 1)
        self.assertIn("censored", suggestion["evidence"]["censoring"])
        self.assertEqual(payload["sample"]["merged"], 4)
        self.assertEqual(payload["sample"]["open_censored"], 1)


# --------------------------------------------------------------------------- #
# DES-3: the comment tax is a floor, not a flattering estimate
# --------------------------------------------------------------------------- #
class CommentTaxHonestyTests(Case):
    def test_unresolved_threads_are_not_booked_as_acted_on(self):
        store = Store()
        store.pr(1, "AaAa")
        for index, outcome in enumerate(("acted_on", "unresolved", "unresolved"), start=1):
            store.objects.append({
                "object_type": "THREAD",
                "object_id": f"THREAD:{REPO}#1:{index}",
                "repo": REPO,
                "pr_id": 1,
                "ticket_keys": [],
                "actor": None,
                "state": "resolved",
                "created_at": T0,
                "closed_at": None,
                "head_commit": None,
                "message_class": None,
                "attributes": {"severity": "suggestion", "anchor_class": "general",
                               "comment_type": "suggestion", "thread_outcome": outcome},
            })
        court = self.load(store)
        window = rules.window_for(court)
        metric = rules.comment_tax(court, store.threads(self.path), window)
        self.assertEqual(metric["classified"], 3)
        self.assertEqual(metric["acted_on"], 1)
        self.assertEqual(metric["unresolved"], 2)
        self.assertEqual(metric["wasted"], 0)
        self.assertIn("floor", metric["outcome_note"])


if __name__ == "__main__":
    unittest.main()

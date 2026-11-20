"""prd/40 — the review-model middle ground: status vector + debt + ready.

One regression per scenario of ``docs/review-model-analysis.md`` §2, plus the
two aggregation invariants and the sentinel gate. Each test pins the *answer*
the old single-ball machine got wrong, so reverting a transition fails here with
the number that moved:

* **S1** — a standing approval is not re-pended by a push; the silent reviewer
  is charged; ``ready`` is a named state (RA 2 / RB 8 / RC 30 / RD 24 / author 4
  / ready 9, the analysis's C row);
* **S1b** — an approval does not cancel someone else's objection (author 18 h,
  not 2 h);
* **S2a** — a push-induced unapproval hands the ball to the reviewer, not to the
  author who pushed (author 0 h);
* **S2b** — a pure rebase that kept its approvals changes nothing (ready 48 h);
* **S4** — CI is a flag, never an exclusive ball (the reviewer keeps the clock);
* **S5** — a reviewer added mid-debt does not move the ball (author 28 h);
* **S7** — a reviewer pending at merge is ``unrequired`` (listed, not blamed);
* **S8** — business hours are priced per holder on that person's calendar;
* **S12** — responsiveness samples per ``(person, PR)`` episode, not per
  interval;
* invariants ``Σ_x |b_x| ≥ |⋃ b_x|`` and ``Σ solo_x ≤ |⋃ b_x|``;
* ``sentinel_after_peers`` — the sentinel is not charged while a peer is
  pending, and the setting is configurable.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from analysis import ballcourt, rules
from analysis.calendar import Calendar
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run

REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = datetime(2026, 3, 1, 12, 0)


class Store:
    """A synthetic store with explicit events, object links and objects."""

    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.objects: list[dict] = []
        self.opened: dict[int, datetime] = {}
        self.authors: dict[int, str] = {}
        self._count = 0

    # -- PRs / events ------------------------------------------------------- #
    def pr(self, number: int, author: str, opened: datetime = T0,
           state: str = "open", closed: datetime | None = None,
           tickets: tuple[str, ...] = ("ENG-1",)) -> "Store":
        self.opened[number] = opened
        self.authors[number] = author
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

    def roster(self, number: int, *reviewers: str, hours: float = 0) -> "Store":
        """The scrape's ``reviewer_changed added`` events, one per reviewer."""
        for reviewer in reviewers:
            self.event(number, "reviewer_changed", self.authors[number], hours,
                       people=(reviewer,), change="added")
        return self

    def sentinel(self, number: int, person: str, hours: float = 0) -> "Store":
        """Mark ``person`` as a sentinel on this PR (the role is per event)."""
        self.event(number, "reviewer_changed", self.authors[number], hours,
                   people=(person,), change="added", sentinel=True)
        return self

    def thread(self, number: int, outcome: str,
               comment_type: str = "blocker") -> "Store":
        """Give the event just added a THREAD object with its closed outcome.

        prd/41 B6: a mandatory blocker whose thread was ``walked_back`` is a
        withdrawn objection, and the ball-court reads that from the THREAD
        object the comment event links to.
        """
        key = self.events[-1]["event_key"]
        object_id = f"THREAD:{REPO}#{number}:{self._count}"
        self.objects.append({
            "object_type": "THREAD",
            "object_id": object_id,
            "repo": REPO,
            "pr_id": number,
            "ticket_keys": [],
            "actor": None,
            "state": "resolved",
            "created_at": self.events[-1]["ts"],
            "closed_at": None,
            "head_commit": None,
            "message_class": None,
            "attributes": {
                "severity": "mandatory",
                "anchor_class": "file",
                "comment_type": comment_type,
                "thread_outcome": outcome,
            },
        })
        self.relations.append((key, "THREAD", object_id))
        return self

    # -- materialise -------------------------------------------------------- #
    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-review-model",
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

    def court(self, path: Path, calendar: Calendar | None = None,
              sentinel_after_peers: bool = True) -> ballcourt.BallCourt:
        con = connect(path, read_only=True)
        try:
            return ballcourt.load(
                con, calendar=calendar, sentinel_after_peers=sentinel_after_peers,
            )
        finally:
            con.close()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, store: Store, **kwargs) -> ballcourt.BallCourt:
        return store.court(store.write(self.path), **kwargs)

    @staticmethod
    def person_hours(court: ballcourt.BallCourt) -> dict[str, float]:
        """Person-hours per holder, from the shipped intervals."""
        out: dict[str, float] = {}
        for interval in court.intervals:
            if not interval.measured:
                continue
            for person in interval.person_holders:
                out[person] = round(out.get(person, 0.0) + interval.hours_for(person), 2)
        return out

    @staticmethod
    def ready_hours(court: ballcourt.BallCourt) -> float:
        return round(sum(
            i.hours for i in court.intervals if i.measured and i.ready
        ), 2)

    @staticmethod
    def edges(court: ballcourt.BallCourt) -> dict[tuple[str, str], dict]:
        return {
            (edge["from"], edge["to"]): edge
            for edge in ballcourt.blocking_graph(court, level="person")["edges"]
        }


# --------------------------------------------------------------------------- #
# S1 — the analysis's headline scenario, and the C row of its table
# --------------------------------------------------------------------------- #
class S1Tests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA", "RBrB", "RCrC")
        store.event(1, "pr_approved", "RArA", 2)
        store.event(1, "pr_needs_work", "RBrB", 5)
        store.event(1, "reviewer_changed", "AuAu", 7, people=("RDrD",), change="added")
        store.event(1, "pr_pushed", "AuAu", 9, push_class="content")
        store.event(1, "pr_approved", "RBrB", 12)
        store.event(1, "pr_approved", "RCrC", 30)
        store.event(1, "pr_approved", "RDrD", 31)
        store.event(1, "pr_merged", "RDrD", 40)
        return store

    def test_per_actor_hours_match_the_analysis(self):
        hours = self.person_hours(self.load(self.store()))
        self.assertEqual(hours, {
            "RArA": 2.0,      # approved at t2; the push did not re-pend it
            "RBrB": 8.0,
            "RCrC": 30.0,
            "RDrD": 24.0,     # added mid-debt; charged from its own add
            "AuAu": 4.0,      # t5-t9 only: the debt stood until the push
        })

    def test_the_ready_state_is_named_not_booked_as_review(self):
        court = self.load(self.store())
        self.assertEqual(self.ready_hours(court), 9.0)     # t31 → merge t40
        # and nobody is charged for it: the author owns the merge click
        self.assertEqual(self.person_hours(court).get("AuAu"), 4.0)

    def test_the_silent_reviewer_is_the_constraint_not_the_first_approver(self):
        edges = self.edges(self.load(self.store()))
        self.assertEqual(edges[("AuAu", "RArA")]["hours"], 2.0)
        # the silent reviewer's total inbound wait is 30 h, however many
        # handovers split it
        inbound = sum(edge["hours"] for edge in edges.values() if edge["to"] == "RCrC")
        self.assertEqual(inbound, 30.0)


# --------------------------------------------------------------------------- #
# S1b — an approval does not cancel someone else's objection
# --------------------------------------------------------------------------- #
class S1bTests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA", "RBrB", "RCrC")
        store.event(1, "pr_needs_work", "RBrB", 2)
        store.event(1, "pr_approved", "RArA", 4)     # does not clear RB's debt
        store.event(1, "pr_pushed", "AuAu", 20, push_class="content")
        store.event(1, "pr_approved", "RBrB", 22)
        store.event(1, "pr_approved", "RCrC", 23)
        store.event(1, "pr_merged", "RCrC", 24)
        return store

    def test_an_approval_does_not_cancel_another_reviewers_debt(self):
        hours = self.person_hours(self.load(self.store()))
        self.assertEqual(hours["AuAu"], 18.0)       # t2 → t20, not t2 → t4
        self.assertEqual(hours["RArA"], 4.0)
        self.assertEqual(hours["RBrB"], 4.0)
        self.assertEqual(hours["RCrC"], 23.0)

    def test_the_author_is_a_holder_while_the_debt_stands(self):
        court = self.load(self.store())
        interval = next(
            i for i in court.intervals
            if i.start == T0 + timedelta(hours=4) and i.end == T0 + timedelta(hours=20)
        )
        self.assertTrue(interval.author_open)
        self.assertEqual(interval.ball, "author")
        self.assertIn("AuAu", interval.holders)


# --------------------------------------------------------------------------- #
# S2a — a push-induced unapproval is the reviewer's wait, not the author's
# --------------------------------------------------------------------------- #
class S2aTests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA", "RSrS")
        store.event(1, "pr_approved", "RArA", 1)
        store.event(1, "pr_pushed", "AuAu", 10, push_class="pure_rebase",
                    approvals_lost=True)
        store.event(1, "pr_unapproved", "RArA", 10, cause="push")
        store.event(1, "pr_approved", "RArA", 20)
        store.event(1, "pr_approved", "RSrS", 21)
        store.event(1, "pr_merged", "RSrS", 22)
        return store

    def test_the_author_is_not_billed_for_the_reviewers_reapproval(self):
        hours = self.person_hours(self.load(self.store()))
        self.assertNotIn("AuAu", hours)
        self.assertEqual(hours["RArA"], 11.0)       # 1 h + the 10 h re-approval
        self.assertEqual(hours["RSrS"], 21.0)

    def test_author_debt_is_untouched_by_a_push_unapproval(self):
        court = self.load(self.store())
        self.assertFalse(any(i.author_open for i in court.intervals))


# --------------------------------------------------------------------------- #
# S2b — a pure rebase that kept its approvals changes nothing
# --------------------------------------------------------------------------- #
class S2bTests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA", "RSrS")
        store.event(1, "pr_approved", "RArA", 1)
        store.event(1, "pr_approved", "RSrS", 2)
        store.event(1, "pr_pushed", "AuAu", 10, push_class="pure_rebase",
                    approvals_lost=False)
        store.event(1, "pr_merged", "AuAu", 50)
        return store

    def test_a_pure_rebase_does_not_repend_standing_approvals(self):
        court = self.load(self.store())
        hours = self.person_hours(court)
        self.assertEqual(hours["RArA"], 1.0)
        self.assertEqual(hours["RSrS"], 2.0)
        self.assertEqual(self.ready_hours(court), 48.0)    # t2 → t50

    def test_the_status_vector_survives_the_rebase(self):
        court = self.load(self.store())
        after = next(i for i in court.intervals if i.start == T0 + timedelta(hours=10))
        self.assertTrue(after.ready)
        self.assertEqual(after.holders, ())


# --------------------------------------------------------------------------- #
# S4 — CI is a flag, never an exclusive ball
# --------------------------------------------------------------------------- #
class S4Tests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA")
        store.event(1, "build_status", "CiCi", 1)                    # started
        store.event(1, "build_status", "CiCi", 1.5, result="success")  # green
        store.event(1, "pr_approved", "RArA", 20)
        store.event(1, "pr_merged", "RArA", 21)
        return store

    def test_a_build_does_not_hide_a_pending_reviewer(self):
        hours = self.person_hours(self.load(self.store()))
        self.assertEqual(hours["RArA"], 20.0)       # not 1 h then CI for 19 h

    def test_the_flag_is_set_and_cleared_but_never_exclusive(self):
        court = self.load(self.store())
        running = next(i for i in court.intervals if i.start == T0 + timedelta(hours=1))
        self.assertTrue(running.ci)
        self.assertEqual(running.phase, "review")   # the reviewer is still pending
        self.assertEqual(running.ball, "review")
        cleared = next(i for i in court.intervals if i.start == T0 + timedelta(hours=1.5))
        self.assertFalse(cleared.ci)
        self.assertEqual(cleared.phase, "review")


# --------------------------------------------------------------------------- #
# S5 — a reviewer added mid-debt does not move the ball
# --------------------------------------------------------------------------- #
class S5Tests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA")
        store.event(1, "pr_needs_work", "RArA", 2)
        store.event(1, "reviewer_changed", "AuAu", 3, people=("RBrB",), change="added")
        store.event(1, "pr_pushed", "AuAu", 30, push_class="content")
        store.event(1, "pr_approved", "RArA", 32)
        store.event(1, "pr_approved", "RBrB", 33)
        store.event(1, "pr_merged", "RBrB", 34)
        return store

    def test_the_author_keeps_the_ball_and_the_new_reviewer_is_concurrent(self):
        court = self.load(self.store())
        hours = self.person_hours(court)
        self.assertEqual(hours["AuAu"], 28.0)       # t2 → t30, not 1 h
        self.assertEqual(hours["RBrB"], 30.0)       # t3 → t33, concurrently
        self.assertEqual(hours["RArA"], 4.0)
        mid = next(i for i in court.intervals if i.start == T0 + timedelta(hours=3))
        self.assertEqual(mid.ball, "author")
        self.assertTrue(mid.author_open)
        self.assertEqual(set(mid.holders), {"AuAu", "RBrB"})


# --------------------------------------------------------------------------- #
# S7 — a reviewer pending at merge is unrequired, not blamed
# --------------------------------------------------------------------------- #
class S7Tests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu", state="merged", closed=T0 + timedelta(hours=4))
        store.roster(1, "RArA", "ROrO")
        store.event(1, "pr_approved", "RArA", 3)
        store.event(1, "pr_merged", "RArA", 4)
        return store

    def test_a_pending_at_merge_reviewer_is_tagged_unrequired(self):
        court = self.load(self.store())
        self.assertEqual(
            ballcourt.pending_at_close(court)[f"PR:{REPO}#1"], frozenset({"ROrO"})
        )
        metric = rules.reviewer_responsiveness(court, rules.window_for(court))
        self.assertEqual(metric["unrequired_hours"], 4.0)
        row = next(r for r in metric["rows"] if r["person"] == "ROrO")
        self.assertTrue(row["unrequired"])
        self.assertEqual(row["hours"], 4.0)
        self.assertEqual(row["required_hours"], 0.0)
        # not blamed: no sample enters the median
        person = next(p for p in metric["people"] if p["person"] == "ROrO")
        self.assertEqual(person["review_n"], 0)
        self.assertFalse(person["flagged"])


# --------------------------------------------------------------------------- #
# S8 — per-holder calendar pricing
# --------------------------------------------------------------------------- #
class S8Tests(Case):
    def calendar(self) -> Calendar:
        return Calendar(
            timezone="UTC",
            default_window=(9 * 60, 17 * 60),
            weekdays=frozenset({1, 2, 3, 4, 5}),   # isoweekday: Mon=1
            vacations={"RCrC": [(date(2026, 1, 5), date(2026, 1, 9))]},
        )

    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RBrB", "RCrC")
        # RB approves on Friday 17:00 (t=104 wall hours from Monday 09:00)
        store.event(1, "pr_approved", "RBrB", 104)
        store.event(1, "pr_merged", "RBrB", 105)
        return store

    def test_each_holder_is_priced_on_their_own_calendar(self):
        court = self.load(self.store(), calendar=self.calendar())
        interval = next(i for i in court.intervals if i.end == T0 + timedelta(hours=104))
        self.assertEqual(interval.hours, 40.0)                  # default window
        self.assertEqual(interval.hours_for("RBrB"), 40.0)
        self.assertEqual(interval.hours_for("RCrC"), 0.0)       # on vacation

    def test_the_blocking_graph_uses_the_holder_price(self):
        edges = self.edges(self.load(self.store(), calendar=self.calendar()))
        self.assertEqual(edges[("AuAu", "RBrB")]["hours"], 40.0)
        self.assertEqual(edges[("AuAu", "RCrC")]["hours"], 0.0)


# --------------------------------------------------------------------------- #
# S12 — one wait is one episode, however many events split it
# --------------------------------------------------------------------------- #
class S12Tests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RCrC")
        # suggestions by a third person split the interval but not the wait
        for hours in (5, 10, 20):
            store.event(1, "comment_added", "XxXx", hours, severity="suggestion")
        store.event(1, "pr_approved", "RCrC", 30)
        return store

    def test_responsiveness_samples_per_episode_not_per_interval(self):
        court = self.load(self.store())
        metric = rules.reviewer_responsiveness(court, rules.window_for(court))
        person = next(p for p in metric["people"] if p["person"] == "RCrC")
        self.assertEqual(person["review_n"], 1)
        self.assertEqual(person["review_median_hours"], 30.0)
        row = next(r for r in metric["rows"] if r["person"] == "RCrC")
        self.assertEqual(row["waits"], 1)
        self.assertEqual(row["hours"], 30.0)


# --------------------------------------------------------------------------- #
# invariants: person-hours ≥ wall hours, and solo ≤ wall hours
# --------------------------------------------------------------------------- #
class InvariantTests(Case):
    def court(self) -> ballcourt.BallCourt:
        return S1Tests.store(self)

    def test_person_hours_are_at_least_the_wall_clock_union(self):
        court = self.load(self.court())
        wall = sum(i.hours for i in court.intervals if i.measured and i.person_holders)
        person = sum(
            i.hours_for(p) for i in court.intervals if i.measured for p in i.person_holders
        )
        self.assertGreaterEqual(round(person, 2), round(wall, 2))

    def test_solo_hours_are_at_most_the_wall_clock_union(self):
        court = self.load(self.court())
        wall = sum(i.hours for i in court.intervals if i.measured and i.person_holders)
        solo = sum(
            i.hours_for(p)
            for i in court.intervals if i.measured and i.solo
            for p in i.person_holders
        )
        self.assertLessEqual(round(solo, 2), round(wall, 2))
        graph = ballcourt.blocking_graph(court, level="person")
        solo_nodes = sum(node["solo_hours"] for node in graph["nodes"])
        self.assertLessEqual(round(solo_nodes, 1), round(wall, 1))


# --------------------------------------------------------------------------- #
# the sentinel gate (prd/40)
# --------------------------------------------------------------------------- #
class SentinelGateTests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA")
        store.sentinel(1, "RSrS")
        store.event(1, "pr_approved", "RArA", 2)
        store.event(1, "pr_approved", "RSrS", 5)
        store.event(1, "pr_merged", "RSrS", 6)
        return store

    def test_the_sentinel_is_not_charged_while_a_peer_is_pending(self):
        gated = self.load(self.store())
        self.assertEqual(self.person_hours(gated).get("RSrS"), 3.0)   # t2 → t5
        ungated = self.load(self.store(), sentinel_after_peers=False)
        self.assertEqual(self.person_hours(ungated).get("RSrS"), 5.0)  # t0 → t5

    def test_the_gate_is_a_per_pr_pending_derivation(self):
        court = self.load(self.store())
        first = next(i for i in court.intervals if i.start == T0)
        self.assertNotIn("RSrS", first.holders)      # peer still pending
        self.assertEqual(first.holders, ("RArA",))
        self.assertFalse(court.sentinel_after_peers is False)

    def test_the_sentinel_queue_reports_the_policy(self):
        court = self.load(self.store())
        metric = rules.sentinel_queue(court, rules.window_for(court))
        self.assertTrue(metric["sentinel_after_peers"])
        self.assertEqual(metric["n"], 1)
        self.assertEqual(metric["median_hours"], 3.0)


# --------------------------------------------------------------------------- #
# prd/41 B6 — a withdrawn blocker is a pending verdict, not an answer
# --------------------------------------------------------------------------- #
class WithdrawnBlockerTests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA")
        store.sentinel(1, "FrMo")
        store.event(1, "comment_added", "RArA", 1, severity="mandatory")
        store.thread(1, "walked_back")       # the reviewer withdrew it
        store.event(1, "pr_pushed", "AuAu", 2, push_class="content")
        return store

    def test_a_walked_back_blocker_puts_nobody_in_debt(self):
        court = self.load(self.store())
        after_comment = next(
            interval for interval in court.intervals
            if interval.start == T0 + timedelta(hours=1)
        )
        self.assertFalse(after_comment.author_open)
        self.assertIn("RArA", after_comment.pending_all)

    def test_the_delivery_row_says_zero_of_two_approved_and_both_pending(self):
        court = self.load(self.store())
        row = ballcourt.delivery_list(court)["rows"][0]
        self.assertEqual(row["reviewers_total"], 2)
        self.assertEqual(row["reviewers_approved"], 0)
        self.assertEqual(row["reviewers_pending"], 2)
        # both owe a verdict — chasing the peer alone will not close it
        self.assertEqual(row["waiting_on"], ["FrMo", "RArA"])
        self.assertEqual(
            row["waiting_roles"], {"FrMo": "sentinel", "RArA": "review"}
        )


# --------------------------------------------------------------------------- #
# prd/41 B3 — one waiting-week threshold (40 business hours)
# --------------------------------------------------------------------------- #
class WaitingWeekTests(Case):
    def test_one_week_is_forty_business_hours_everywhere(self):
        store = Store()
        span = (AS_OF - T0).total_seconds() / 3600.0
        for number, wait in ((1, 168.0), (2, 152.0), (3, 132.0),
                             (4, 20.0), (5, 16.0)):
            store.pr(number, "AuAu")
            store.roster(number, "RArA", hours=span - wait)
        delivery = ballcourt.delivery_list(self.load(store))
        counts = delivery["counts"]
        self.assertEqual(counts["waiting_week_hours"], 40.0)
        self.assertEqual(counts["waiting_week_days"], 5.0)
        # three items are past a week, and the same three are "oldest waiting":
        # the card, the pill, the header and the footer read one threshold
        self.assertEqual(counts["waiting_over_week"], 3)
        self.assertEqual(counts["oldest_waiting"], 3)
        over = sorted(
            row["number"] for row in delivery["rows"]
            if row["waiting_hours"] >= counts["waiting_week_hours"]
        )
        self.assertEqual(over, [1, 2, 3])


# --------------------------------------------------------------------------- #
# prd/41 P4 — solo hours are the marginal person-level load
# --------------------------------------------------------------------------- #
class SoloLoadTests(Case):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA", "RBrB")
        store.event(1, "pr_approved", "RArA", 2)    # RBrB alone from t2
        store.event(1, "comment_added", "RArA", 10, severity="suggestion")
        return store

    def test_the_blocking_graph_exposes_solo_next_to_the_total(self):
        court = self.load(self.store())
        graph = ballcourt.blocking_graph(court, level="person")
        self.assertIn("solo", graph["hours_note"])
        self.assertIn("shared wait", graph["hours_note"])
        # prd/46 §2: a person-hour total is labelled as a different metric from
        # the canonical sentinel wait.
        self.assertIn("not the sentinel's response time", graph["hours_note"])
        node = next(node for node in graph["nodes"] if node["id"] == "RBrB")
        self.assertEqual(node["hours"], 10.0)        # t0 → snapshot (12 h)
        self.assertEqual(node["solo_hours"], 8.0)    # t2 → snapshot
        self.assertEqual(node["solo_share"], round(8.0 / 10.0, 4))

    def test_the_queue_stream_exposes_solo_next_to_the_total(self):
        court = self.load(self.store())
        queues = ballcourt.queue_series(court, level="person")
        self.assertIn("solo", queues["hours_note"])
        stream = next(item for item in queues["streams"] if item["id"] == "RBrB")
        self.assertLessEqual(
            stream["summary"]["solo_hours"], stream["summary"]["hours"]
        )
        self.assertIsNotNone(stream["summary"]["solo_share"])


# --------------------------------------------------------------------------- #
# API: the delivery row speaks the new model
# --------------------------------------------------------------------------- #
class DeliveryPayloadTests(Case):
    def test_a_doubly_blocked_row_names_the_author_and_the_reviewer(self):
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA", "RBrB")
        store.event(1, "pr_needs_work", "RArA", 2)
        row = ballcourt.delivery_list(self.load(store))["rows"][0]
        self.assertEqual(row["ball"], "author")
        self.assertTrue(row["doubly_blocked"])
        self.assertTrue(row["author_open"])
        self.assertIn("AuAu", row["waiting_on"])
        self.assertIn("RBrB", row["waiting_on"])
        self.assertEqual(row["reviewers_total"], 2)
        self.assertEqual(row["reviewers_pending"], 1)
        self.assertIsNone(row["ready_since"])

    def test_a_ready_row_has_ready_since_and_no_pending_reviewer(self):
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA", "RBrB")
        store.event(1, "pr_approved", "RArA", 2)
        store.event(1, "pr_approved", "RBrB", 4)
        delivery = ballcourt.delivery_list(self.load(store))
        row = delivery["rows"][0]
        self.assertEqual(row["ball"], "ready")
        self.assertEqual(row["waiting_on"], [])
        self.assertEqual(row["reviewers_pending"], 0)
        self.assertIsNotNone(row["ready_since"])
        self.assertFalse(row["doubly_blocked"])
        self.assertFalse(row["blocked"])
        self.assertEqual(delivery["counts"]["ready"], 1)


if __name__ == "__main__":
    unittest.main()

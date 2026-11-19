"""prd/34 (actor & constraint correctness) + prd/36 (lifecycle honesty).

One regression per finding, pinned against small synthetic stores so the
failure names the number that moved:

prd/34
  * a bot (``actor_kind = bot``) is never a constraint, a reviewer, an owner of
    person-hours, or a scope option — and a bot-declined PR is *abandoned*;
  * the sentinel (or any single holder) is **named** in "waiting on";
  * the delivery payload carries the roles a ``person``/``role`` scope needs, so
    ``person:FrMo`` and ``role:sentinel`` resolve to the same set;
  * "overloaded" is queue wait and "busy" is calendar share (different names);
  * the stale-PR signal (open > N business days, or ticket Done + PR open) and
    the unobserved tail are surfaced.

prd/36
  * the first segment is ``created → ready``;
  * a multi-PR idle gap is reported and named in the answer;
  * the headline states the end state and any open PR;
  * a reopened story reports both done boundaries;
  * "per story" is a statistic, not the cohort total;
  * "logged a week late" excludes ``within_week``;
  * the store's ``review_rounds`` uses the same definition as the analysis.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis import ballcourt, jira, rules, tickets
from ingest.pipeline import derive_review_rounds, recompute_derived
from ingest.store import connect
from tests.test_jira_analysis import Fixture, T0, AS_OF
from tests.test_review_model import REPO, Store

BOT = "BiBi"


def _person(object_id: str, kind: str) -> dict:
    """A PERSON object with the store's ``actor_kind`` projection."""
    return {
        "object_type": "PERSON",
        "object_id": f"PERSON:{object_id}",
        "repo": None,
        "pr_id": None,
        "ticket_keys": [],
        "actor": object_id,
        "state": None,
        "created_at": None,
        "closed_at": None,
        "head_commit": None,
        "message_class": None,
        "attributes": {"actor_kind": kind},
    }


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def court(self, store: Store) -> ballcourt.BallCourt:
        store.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            return ballcourt.load(con)
        finally:
            con.close()

    def jira_store(self, fx: Fixture):
        fx.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con)
            data = jira.load(con, court)
            window = rules.window_for(court, None, data)
            return court, data, window
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# prd/34 §1 — bots out of every person aggregate
# --------------------------------------------------------------------------- #
class BotExclusionTests(StoreCase):
    def store(self, state: str = "open", closed: datetime | None = None) -> Store:
        store = Store()
        store.objects.append(_person(BOT, "bot"))
        store.objects.append(_person("RArA", "human"))
        store.pr(1, "AuAu", state=state, closed=closed)
        store.roster(1, "RArA", BOT)
        store.event(1, "pr_approved", BOT, 2)      # the bot answers
        if state == "merged":
            store.event(1, "pr_approved", "RArA", 4)
            store.event(1, "pr_merged", "RArA", 5)
        return store

    def test_a_bot_is_not_a_person_queue_or_constraint(self):
        court = self.court(
            self.store(state="merged", closed=T0 + timedelta(hours=5))
        )
        self.assertEqual(court.bots, {BOT})
        # person-hours: only the human reviewer, and never the bot
        hours: dict[str, float] = {}
        for interval in court.intervals:
            for person in interval.person_holders:
                hours[person] = round(hours.get(person, 0.0) + interval.hours_for(person), 2)
        self.assertNotIn(BOT, hours)
        self.assertEqual(hours["RArA"], 4.0)
        graph = ballcourt.blocking_graph(court, level="person")
        self.assertNotIn(BOT, {node["id"] for node in graph["nodes"]})
        self.assertNotIn(BOT, {edge["to"] for edge in graph["edges"]})
        queues = ballcourt.queue_series(court, level="person")
        self.assertNotIn(BOT, {stream["id"] for stream in queues["streams"]})

    def test_a_bot_is_not_a_reviewer_in_the_diagnosis(self):
        court = self.court(self.store())
        metric = rules.reviewer_responsiveness(court, rules.window_for(court))
        self.assertNotIn(BOT, {person["person"] for person in metric["people"]})
        self.assertNotIn(BOT, metric["flagged"])
        self.assertEqual(metric["bots_excluded"], [BOT])
        payload = rules.insights(court, [], rules.window_for(court), level="person",
                                 bots={BOT})
        ids = {suggestion["id"] for suggestion in payload["suggestions"]}
        self.assertNotIn(f"reviewer-{BOT}", ids)

    def test_a_bot_is_not_a_scope_option(self):
        # the API's health payload is what the scope select filters on
        self.store().write(self.path)
        con = connect(self.path, read_only=True)
        try:
            self.assertEqual(rules.load_bots(con), {BOT})
        finally:
            con.close()

    def test_a_bot_reviewer_is_not_a_holder_in_the_delivery_list(self):
        court = self.court(self.store())          # open PR: RArA still pending
        row = ballcourt.delivery_list(court)["rows"][0]
        self.assertEqual(row["waiting_on"], ["RArA"])
        self.assertNotIn(BOT, row["waiting_roles"])
        self.assertEqual(row["ball_label"], "RArA")

    def test_a_bot_declined_pr_is_abandoned_not_a_human_wait(self):
        store = Store()
        store.objects.append(_person(BOT, "bot"))
        store.pr(2, "AuAu", state="declined", closed=T0 + timedelta(hours=100))
        store.roster(2, "RArA")
        store.event(2, "pr_declined", BOT, 100)
        court = self.court(store)
        tail = max(court.intervals, key=lambda interval: interval.end)
        self.assertTrue(tail.abandoned)
        self.assertEqual(tail.holders, ())
        # no person-hour is charged for the idle tail a bot cleaned up
        self.assertEqual(
            sum(interval.hours_for(person)
                for interval in court.intervals
                for person in interval.person_holders),
            0.0,
        )
        graph = ballcourt.blocking_graph(court, level="person")
        self.assertEqual(graph["edges"], [])
        self.assertEqual(graph["totals"]["abandoned_hours"], 100.0)
        queues = ballcourt.queue_series(court, level="person")
        self.assertEqual(queues["totals"]["abandoned_hours"], 100.0)
        self.assertEqual(queues["streams"], [])
        metric = rules.reviewer_responsiveness(court, rules.window_for(court))
        person = next((p for p in metric["people"] if p["person"] == "RArA"), None)
        self.assertTrue(person is None or person["review_n"] == 0)

    def test_the_stretch_that_ends_at_a_bot_comment_is_abandoned_too(self):
        """prd/41 B1: the automation's closing comment is not the decline event.

        The corpus emits ``comment_added`` by the bot and ``pr_declined`` at the
        same instant, so the long final interval ends at the *comment*. The old
        flag only looked for a ``pr_declined`` boundary and left 400 h charged to
        the author and the sentinel.
        """
        store = Store()
        store.objects.append(_person(BOT, "bot"))
        store.pr(2, "AuAu", state="declined", closed=T0 + timedelta(hours=400))
        store.roster(2, "RArA")
        store.event(2, "comment_added", "RArA", 5, severity="suggestion")
        store.event(2, "comment_added", BOT, 400)      # the automation's note
        store.event(2, "pr_declined", BOT, 400)        # and the close, same instant
        court = self.court(store)
        long_tail = max(court.intervals, key=lambda interval: interval.hours)
        self.assertTrue(long_tail.abandoned)
        self.assertEqual(long_tail.holders, ())
        graph = ballcourt.blocking_graph(court, level="person")
        # the 395 h dead stretch is reported, never charged to a person
        self.assertAlmostEqual(graph["totals"]["abandoned_hours"], 395.0, places=1)
        # only the 5 h before the bot's note was a real wait
        self.assertEqual(
            [(edge["from"], edge["to"], edge["hours"]) for edge in graph["edges"]],
            [("AuAu", "RArA", 5.0)],
        )
        queues = ballcourt.queue_series(court, level="person")
        self.assertAlmostEqual(queues["totals"]["abandoned_hours"], 395.0, places=1)


# --------------------------------------------------------------------------- #
# prd/34 §2 — name the owner
# --------------------------------------------------------------------------- #
class NamedOwnerTests(StoreCase):
    def sentinel_store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA")
        store.sentinel(1, "FrMo")
        store.event(1, "pr_approved", "RArA", 2)     # every peer answered
        return store

    def test_a_pr_waiting_only_on_the_sentinel_names_the_sentinel(self):
        court = self.court(self.sentinel_store())
        self.assertIn("FrMo", court.sentinels)
        row = ballcourt.delivery_list(court)["rows"][0]
        self.assertEqual(row["ball"], "review")
        self.assertEqual(row["waiting_on"], ["FrMo"])
        self.assertEqual(row["waiting_for"], "FrMo")
        self.assertEqual(row["ball_label"], "FrMo")
        self.assertEqual(row["waiting_roles"], {"FrMo": "sentinel"})

    def test_two_pending_reviewers_keep_the_queue_label(self):
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA", "RBrB")
        row = ballcourt.delivery_list(self.court(store))["rows"][0]
        self.assertEqual(row["ball_label"], "review")
        self.assertEqual(row["waiting_on"], ["RArA", "RBrB"])
        self.assertEqual(set(row["waiting_roles"].values()), {"review"})

    def test_the_ticket_pr_rows_name_the_owner_too(self):
        court = self.court(self.sentinel_store())
        con = connect(self.path, read_only=True)
        try:
            data = jira.load(con, court)
        finally:
            con.close()
        # a STORY is not needed: the ticket view answers from the PR join
        payload = tickets.ticket(court, data, "ENG-1")
        self.assertIsNotNone(payload)
        labels = {
            interval["waited_on"] for pr in payload["prs"]
            for interval in pr["intervals"]
        }
        self.assertIn("FrMo", labels)


# --------------------------------------------------------------------------- #
# prd/34 §4 — distinct load metrics
# --------------------------------------------------------------------------- #
class LoadLabelTests(StoreCase):
    def test_queue_wait_is_named_queue_wait(self):
        store = Store()
        store.pr(1, "AuAu")
        store.roster(1, "RArA")
        payload = ballcourt.queue_series(self.court(store), level="person")
        self.assertEqual(payload["diagnosis_kind"], "queue-wait")
        self.assertIn("not calendar utilisation", payload["diagnosis_note"])
        for stream in payload["streams"]:
            self.assertEqual(stream["summary"]["diagnosis_kind"], "queue-wait")


# --------------------------------------------------------------------------- #
# prd/34 §5 — stale PR + unobserved tail
# --------------------------------------------------------------------------- #
class StaleSignalTests(StoreCase):
    def store(self) -> Store:
        store = Store()
        store.pr(1, "AuAu", tickets=("ENG-1",))
        store.roster(1, "RArA")
        store.event(1, "comment_added", "XxXx", 10, severity="suggestion")
        return store

    def test_an_old_pr_is_flagged_stale_in_business_days(self):
        delivery = ballcourt.delivery_list(self.court(self.store()))
        row = delivery["rows"][0]
        self.assertTrue(row["stale"])
        self.assertEqual(row["stale_business_days"], 5.0)
        self.assertTrue(any("business days" in reason for reason in row["stale_reasons"]))
        self.assertEqual(delivery["counts"]["stale"], 1)
        self.assertIn("business days", delivery["stale_note"])

    def test_a_done_ticket_with_an_open_pr_is_stale(self):
        delivery = ballcourt.delivery_list(
            self.court(self.store()), done_tickets={"ENG-1"}
        )
        row = delivery["rows"][0]
        self.assertTrue(row["stale"])
        self.assertIn("Done in Jira", " ".join(row["stale_reasons"]))

    def test_the_unobserved_tail_stays_in_the_delivery_list(self):
        delivery = ballcourt.delivery_list(self.court(self.store()))
        row = delivery["rows"][0]
        self.assertTrue(row["unobserved"])
        self.assertIsNotNone(row["unobserved_since"])
        self.assertEqual(delivery["counts"]["unobserved"], 1)


# --------------------------------------------------------------------------- #
# prd/36 §1 — the first segment is created → ready
# --------------------------------------------------------------------------- #
class SegmentLabelTests(StoreCase):
    def test_the_first_segment_is_created_to_ready(self):
        self.assertEqual(jira.SEGMENT_KEYS[0], "created_ready")
        self.assertEqual(jira.SEGMENT_LABEL["created_ready"], "created → ready")
        self.assertEqual(jira.SEGMENT_BOUNDS["created_ready"], ("created", "ready"))
        fx = Fixture()
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=30))
        fx.pointed("ENG-1", T0 + timedelta(hours=10), 3)
        fx.status("ENG-1", T0 + timedelta(hours=30), "done", from_status="backlog")
        court, data, _window = self.jira_store(fx)
        payload = tickets.ticket(court, data, "ENG-1")
        row = payload["segments"]["rows"][0]
        self.assertEqual(row["key"], "created_ready")
        self.assertEqual(row["label"], "created → ready")
        self.assertEqual(row["bounds"], ["created", "ready"])
        self.assertEqual(row["hours"], 10.0)

    def test_all_nine_segments_report_measured_over_nine(self):
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=30))
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=80))
        fx.pointed("ENG-1", T0 + timedelta(hours=10), 3)
        fx.add_to_sprint("ENG-1", T0 + timedelta(hours=20), 1)
        fx.status("ENG-1", T0 + timedelta(hours=30), "in_progress")
        fx.status("ENG-1", T0 + timedelta(hours=70), "in_verification")
        fx.status("ENG-1", T0 + timedelta(hours=80), "done", from_status="in_verification")
        fx.pr(1, "AaAa", T0 + timedelta(hours=40), closed=T0 + timedelta(hours=60))
        fx.pr_event(1, "comment_added", "RrRr", 2, severity="suggestion")
        fx.pr_event(1, "pr_approved", "RrRr", 10)
        fx.pr_event(1, "pr_merged", "RrRr", 20)
        court, data, _window = self.jira_store(fx)
        payload = tickets.ticket(court, data, "ENG-1")
        self.assertEqual(len(payload["segments"]["rows"]), 9)
        self.assertEqual(payload["segments"]["measured"], 9)
        self.assertNotIn("merge_done", payload["segments"]["order"])


# --------------------------------------------------------------------------- #
# prd/36 §2/§3/§4 — gaps, headline honesty, reopens
# --------------------------------------------------------------------------- #
class LifecycleHonestyTests(StoreCase):
    def gap_fixture(self) -> Fixture:
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=30))
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=170))
        fx.pointed("ENG-1", T0 + timedelta(hours=10), 3)
        fx.add_to_sprint("ENG-1", T0 + timedelta(hours=20), 1)
        fx.status("ENG-1", T0 + timedelta(hours=30), "in_progress")
        fx.status("ENG-1", T0 + timedelta(hours=170), "done", from_status="in_progress")
        # PR 1: open +40, first review +42, approval +45, merge +50
        fx.pr(1, "AaAa", T0 + timedelta(hours=40), closed=T0 + timedelta(hours=50))
        fx.pr_event(1, "comment_added", "RrRr", 2, severity="suggestion")
        fx.pr_event(1, "pr_approved", "RrRr", 5)
        fx.pr_event(1, "pr_merged", "RrRr", 10)
        # a 100 h gap with no PR open, then PR 2 (open +150)
        fx.pr(2, "AaAa", T0 + timedelta(hours=150), closed=T0 + timedelta(hours=160))
        fx.pr_event(2, "comment_added", "RrRr", 2, severity="suggestion")
        fx.pr_event(2, "pr_approved", "RrRr", 5)
        fx.pr_event(2, "pr_merged", "RrRr", 10)
        return fx

    def test_a_multi_pr_idle_gap_is_reported_and_named(self):
        court, data, _window = self.jira_store(self.gap_fixture())
        payload = tickets.ticket(court, data, "ENG-1")
        rounds = next(row for row in payload["segments"]["rows"]
                      if row["key"] == "review_rounds")
        self.assertEqual(rounds["no_pr_open_hours"], 100.0)
        self.assertEqual(rounds["no_pr_open_share"], round(100 / 113, 4))
        # prd/41 B5: a material gap is named in the headline itself, and the
        # day count is rounded (never "12.5000001").
        self.assertIn(
            "review rounds (113 h, 66% of the measured cycle), "
            "100 h of which had no PR open.",
            payload["answer"],
        )
        self.assertIn("PR #2 started 12.5 business days after PR #1 merged",
                      payload["answer"])
        # the sentences are joined, never run together ("active work 100 h …")
        self.assertIn("active work. PR #2 started", payload["answer"])
        self.assertNotIn("12.5000", payload["answer"])
        self.assertEqual(payload["segments"]["pr_gaps"][0]["from_pr"], 1)
        self.assertEqual(payload["segments"]["pr_gaps"][0]["to_pr"], 2)

    def test_the_review_rounds_rule_surfaces_the_no_pr_open_share(self):
        """prd/41 B4(c): "cap review rounds" is partly advice about no PR open."""
        court, data, window = self.jira_store(self.gap_fixture())
        payload = rules.insights(court, [], window, jira=data)
        suggestion = next(
            item for item in payload["suggestions"]
            if item["id"] == "jira-waiting-time"
        )
        self.assertEqual(suggestion["evidence"]["segment"], "review rounds")
        self.assertGreater(suggestion["evidence"]["no_pr_open_share"], 0.5)
        self.assertIn("had no PR open at all", suggestion["suggestion"])

    def test_the_headline_states_done_in_jira_with_an_open_pr(self):
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=30))
        fx.story("DEL-167", T0, state="done", closed=T0 + timedelta(hours=90))
        fx.pointed("DEL-167", T0 + timedelta(hours=10), 8)
        fx.add_to_sprint("DEL-167", T0 + timedelta(hours=20), 1)
        fx.status("DEL-167", T0 + timedelta(hours=30), "in_progress")
        fx.status("DEL-167", T0 + timedelta(hours=90), "done", from_status="in_progress")
        fx.pr(52, "AaAa", T0 + timedelta(hours=40), tickets=("DEL-167",),
              closed=T0 + timedelta(hours=80))
        fx.pr_event(52, "comment_added", "RrRr", 2, severity="suggestion")
        fx.pr_event(52, "pr_merged", "RrRr", 40)
        fx.pr(53, "AlNg", T0 + timedelta(hours=45), tickets=("DEL-167",),
              state="open", closed=None)
        fx.pr_event(53, "comment_added", "RrRr", 2, severity="suggestion")
        fx.pr_event(53, "pr_approved", "RrRr", 10)
        court, data, _window = self.jira_store(fx)
        payload = tickets.ticket(court, data, "DEL-167")
        self.assertEqual(payload["end_state"], "done_with_open_pr")
        self.assertEqual([pr["number"] for pr in payload["open_prs"]], [53])
        self.assertIn(
            "Done in Jira, but PR #53 is still open (right-censored)",
            payload["answer"],
        )
        self.assertIn("DEL-167 took", payload["answer"])

    def test_a_reopened_story_reports_both_done_boundaries(self):
        fx = Fixture()
        fx.sprint(1, T0, end=T0 + timedelta(days=30))
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=100))
        fx.pointed("ENG-1", T0 + timedelta(hours=10), 3)
        fx.add_to_sprint("ENG-1", T0 + timedelta(hours=20), 1)
        fx.status("ENG-1", T0 + timedelta(hours=30), "in_progress")
        fx.status("ENG-1", T0 + timedelta(hours=80), "done", from_status="in_progress")
        fx.status("ENG-1", T0 + timedelta(hours=85), "ready", from_status="done")
        fx.status("ENG-1", T0 + timedelta(hours=90), "in_progress", from_status="ready")
        fx.status("ENG-1", T0 + timedelta(hours=100), "done", from_status="in_progress")
        fx.pr(1, "AaAa", T0 + timedelta(hours=40), closed=T0 + timedelta(hours=60))
        fx.pr_event(1, "comment_added", "RrRr", 2, severity="suggestion")
        fx.pr_event(1, "pr_approved", "RrRr", 15)
        fx.pr_event(1, "pr_merged", "RrRr", 20)
        court, data, _window = self.jira_store(fx)
        payload = tickets.ticket(court, data, "ENG-1")
        self.assertTrue(payload["reopened"])
        self.assertEqual(payload["reopen_count"], 1)
        self.assertEqual(payload["segments"]["cycle_first_done_hours"], 80.0)
        self.assertEqual(payload["segments"]["cycle_hours"], 100.0)
        self.assertIn("reopened", payload["answer"])
        self.assertIn("80 h from created to the first done", payload["answer"])
        self.assertIn("100 h to the final done", payload["answer"])
        # the reopen's own move back to ready is not the refinement boundary
        self.assertEqual(payload["segments"]["rows"][0]["hours"], 10.0)

    def test_a_reopened_and_still_open_story_stays_right_censored(self):
        fx = Fixture()
        fx.story("ENG-2", T0, state="open", closed=None)
        fx.pointed("ENG-2", T0 + timedelta(hours=10), 3)
        fx.status("ENG-2", T0 + timedelta(hours=20), "in_progress")
        fx.status("ENG-2", T0 + timedelta(hours=80), "done", from_status="in_progress")
        fx.status("ENG-2", T0 + timedelta(hours=85), "ready", from_status="done")
        court, data, _window = self.jira_store(fx)
        payload = tickets.ticket(court, data, "ENG-2")
        self.assertTrue(payload["reopened"])
        self.assertTrue(payload["story"]["censored"])
        self.assertEqual(payload["end_state"], "open")
        self.assertIn("80 h from created to the first done", payload["answer"])
        self.assertIn("still open", payload["answer"])


# --------------------------------------------------------------------------- #
# prd/36 §5 — counts, per-story statistics, worklog lateness, rounds
# --------------------------------------------------------------------------- #
class CountsAndDefinitionsTests(StoreCase):
    def test_per_story_is_a_statistic_not_the_cohort_total(self):
        fx = Fixture()
        for index, hours in ((1, 100), (2, 20)):
            key = f"ENG-{index}"
            fx.story(key, T0, state="done", closed=T0 + timedelta(hours=hours))
            fx.pointed(key, T0 + timedelta(hours=10), 3)
            fx.status(key, T0 + timedelta(hours=20), "in_progress")
            fx.status(key, T0 + timedelta(hours=hours), "done", from_status="in_progress")
        court, data, window = self.jira_store(fx)
        metric = jira.waiting_time_decomposition(court, data, window)
        # the total is the aggregate; the per-story figure is the median
        self.assertEqual(metric["largest_hours"], 20.0)
        self.assertEqual(metric["largest_per_story_hours"], 10.0)
        self.assertEqual(metric["largest_per_story_basis"],
                         "median across the stories that measured it")
        payload = rules.insights(court, [], window, jira=data)
        suggestion = next(
            item for item in payload["suggestions"]
            if item["id"] == "jira-waiting-time"
        )
        self.assertEqual(suggestion["evidence"]["per_story_hours"], 10.0)
        self.assertEqual(suggestion["evidence"]["segment_hours"], 20.0)
        self.assertIn("median story waits", suggestion["suggestion"])
        self.assertNotIn(" per story in ", suggestion["suggestion"])

    def test_logged_a_week_late_excludes_within_week(self):
        fx = Fixture()
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=90))
        fx.worklog("ENG-1", T0 + timedelta(hours=40), 1.0,
                   delay_hours=120.0, bucket="within_week")
        fx.worklog("ENG-1", T0 + timedelta(hours=41), 1.0,
                   delay_hours=61 * 24.0, bucket="late")
        court, data, window = self.jira_store(fx)
        metric = jira.worklog_cross_check(court, data, window)
        self.assertEqual(metric["buckets"]["within_week"], 1)
        self.assertEqual(metric["buckets"]["late"], 1)
        self.assertEqual(metric["late_share"], 0.5)   # 1 of 2, not 2 of 2
        payload = tickets.ticket(court, data, "ENG-1")
        self.assertEqual(payload["worklog"]["late_worklogs"], 1)
        self.assertEqual(payload["worklog"]["within_week_worklogs"], 1)

    def test_the_store_round_count_matches_the_analysis_definition(self):
        events = [
            {"event_key": "a:act:1", "ts": T0, "activity": "pr_opened",
             "actor": "AuAu", "attributes": {}},
            {"event_key": "a:act:2", "ts": T0 + timedelta(hours=1),
             "activity": "pr_pushed", "actor": "AuAu",
             "attributes": {"push_class": "content"}},           # setup
            {"event_key": "a:act:3", "ts": T0 + timedelta(hours=2),
             "activity": "comment_added", "actor": "RrRr", "attributes": {}},
            {"event_key": "a:act:4", "ts": T0 + timedelta(hours=3),
             "activity": "pr_pushed", "actor": "AuAu",
             "attributes": {"push_class": "content"}},           # round 1
            {"event_key": "a:act:5", "ts": T0 + timedelta(hours=4),
             "activity": "pr_pushed", "actor": "AuAu",
             "attributes": {"push_class": "pure_rebase",
                            "approvals_lost": False}},           # head move
            {"event_key": "a:act:6", "ts": T0 + timedelta(hours=5),
             "activity": "pr_unapproved", "actor": "RrRr",
             "attributes": {"cause": "push"}},                   # same round
            {"event_key": "a:act:7", "ts": T0 + timedelta(hours=6),
             "activity": "pr_unapproved", "actor": "RrRr",
             "attributes": {"cause": "manual"}},                 # round 2
        ]
        self.assertEqual(derive_review_rounds(events), 2)

    def test_the_store_attribute_is_re_derived_to_the_same_definition(self):
        fx = Fixture()
        fx.story("ENG-1", T0, state="done", closed=T0 + timedelta(hours=100))
        fx.pr(1, "AaAa", T0, closed=T0 + timedelta(hours=100))
        fx.pr_event(1, "pr_pushed", "AaAa", 1, push_class="content")   # setup
        fx.pr_event(1, "comment_added", "RrRr", 5, severity="suggestion")
        fx.pr_event(1, "pr_pushed", "AaAa", 10, push_class="content")
        fx.pr_event(1, "pr_unapproved", "RrRr", 30, cause="push")
        fx.pr_event(1, "pr_unapproved", "RrRr", 40, cause="manual")
        # a pure rebase that loses nothing is a head move, not a round
        fx.pr_event(1, "pr_pushed", "AaAa", 50, push_class="pure_rebase")
        # the reducer's old count (2 pushes + 2 unapprovals = 4) is what a
        # pre-fix store carries; the derived attribute must correct it.
        fx.pr_event(1, "pr_merged", "RrRr", 100, review_rounds=4)
        fx.write(self.path)
        con = connect(self.path)
        try:
            rewritten = recompute_derived(con, {(REPO, 1)})
            row = con.execute(
                "SELECT CAST(attributes AS VARCHAR) FROM events"
                " WHERE activity = 'pr_merged'"
            ).fetchone()
        finally:
            con.close()
        self.assertGreaterEqual(rewritten, 1)
        self.assertEqual(json.loads(row[0])["review_rounds"], 2)


if __name__ == "__main__":
    unittest.main()

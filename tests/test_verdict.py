"""prd/58 — the constraint verdict and the exploit → elevate ladder.

The verdict must name **one** constraint on **one** canonical clock: the station
with the largest *removable* wait pool (live in-station age + the completed
stories' station hours above the station's p85), not the station with the
largest history. The ladder must walk exploit → subordinate → elevate with the
policy lab's verdict on each rung, and **block** elevate while exploit items are
still owed.

Every number here is pinned against a synthetic store with hand-written events
(no fixture generator, no randomness, no calendar — one elapsed hour is one
business hour), plus an optional del-combined pin that runs when that store is
present (the same pattern as ``tests/test_yield.py``).

The endpoint half lives in ``tests/test_server.py`` (``VerdictEndpointTests``).

    .venv/bin/python -m unittest tests.test_verdict -v
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analysis import ballcourt, jira, rules, verdict
from ingest.store import connect, ensure_schema, write_events, write_objects, write_run

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO = "ENG/platform"
T0 = datetime(2026, 1, 5, 9, 0)          # a Monday
AS_OF = datetime(2026, 4, 1, 12, 0)
HOUR = timedelta(hours=1)
DAY = timedelta(hours=8)                 # one *business* day (concepts.md §1)


class Fixture:
    """A tiny store: stories, their PRs, and the events that derive boundaries."""

    def __init__(self, as_of: datetime = AS_OF):
        self.as_of = as_of
        self.events: list[dict] = []
        self.objects: list[dict] = []
        self.relations: list[tuple[str, str, str]] = []
        self.person("AaAa")
        self.person("ReVw")
        self.person("FrMo")
        self._count = 0

    # -- objects ------------------------------------------------------------ #
    def person(self, pseudonym: str, kind: str = "human") -> None:
        self.objects.append({
            "object_type": "PERSON",
            "object_id": f"PERSON:{pseudonym}",
            "repo": None, "pr_id": None, "ticket_keys": [],
            "actor": pseudonym,
            "state": None, "created_at": None, "closed_at": None,
            "head_commit": None, "message_class": None,
            "attributes": {"actor_kind": kind},
        })

    def _story_object(self, key, created, done, actor, points):
        self.objects.append({
            "object_type": "STORY",
            "object_id": f"STORY:{key}",
            "repo": None, "pr_id": None, "ticket_keys": [key],
            "actor": actor,
            "state": "done" if done else "open",
            "created_at": created,
            "closed_at": done,
            "head_commit": None, "message_class": None,
            "attributes": {"points": points},
        })

    def _pr_object(self, number, opened, *, state, closed, actor, tickets):
        self.objects.append({
            "object_type": "PR",
            "object_id": f"PR:{REPO}#{number}",
            "repo": REPO, "pr_id": number,
            "ticket_keys": list(tickets),
            "actor": actor,
            "state": state,
            "created_at": opened,
            "closed_at": closed,
            "head_commit": "a" * 40, "message_class": None,
            "attributes": {},
        })

    def event(self, that, activity, stamp, actor, *, people=(), **attributes):
        """Append one event and its person links (``that`` is a STORY key/PR no)."""
        self._count += 1
        event_key = f"{that}:{self._count}"
        self.events.append({
            "event_key": event_key,
            "ts": stamp,
            "activity": activity,
            "actor": actor,
            "repo": REPO if isinstance(that, int) else None,
            "pr_id": that if isinstance(that, int) else None,
            "attributes": attributes,
            "classifier_version": "deterministic-v1",
            "content_hash": hashlib.blake2b(
                event_key.encode(), digest_size=8).hexdigest(),
            "as_of": self.as_of,
        })
        if isinstance(that, int):
            self.relations.append((event_key, "PR", f"PR:{REPO}#{that}"))
        else:
            self.relations.append((event_key, "STORY", f"STORY:{that}"))
        for person in people:
            self.relations.append((event_key, "PERSON", f"PERSON:{person}"))

    # -- stories ------------------------------------------------------------ #
    def story(self, key, created, *, done=True, actor="AaAa", points=3,
              refine=20, plan=8, start=4, develop=6, wait_review=2,
              first_review=2, rounds=10, merge=2, verify_wait=2, verify=4,
              reviewer="ReVw", sentinel=None, number=None,
              approved=True) -> int:
        """A complete story (or an open one) with a known segment decomposition."""
        number = number or 100 + self._count
        t_ready = created + refine * HOUR
        t_sprint = t_ready + plan * HOUR
        t_start = t_sprint + start * HOUR
        t_pr = t_start + develop * HOUR
        t_first = t_pr + (wait_review + first_review) * HOUR
        t_approval = t_first + rounds * HOUR
        t_merged = t_approval + merge * HOUR
        t_verification = t_merged + verify_wait * HOUR
        t_done = t_verification + verify * HOUR
        done_at = t_done if done else None
        self._story_object(key, created, done_at, actor, points)
        self._pr_object(
            number, t_pr,
            state="merged" if done else "open",
            closed=t_merged if done else None,
            actor=actor, tickets=(key,),
        )
        self.event(key, "created", created, actor)
        self.event(key, "status_changed", t_ready, actor, to_status="ready")
        self.event(key, "sprint_added", t_sprint, actor)
        self.event(key, "status_changed", t_start, actor, to_status="in_progress")
        self.event(number, "pr_opened", t_pr, actor)
        # review opens at the PR open (the Jira status moves then, as it does in
        # the demo store), and the reviewer is asked at the same instant.
        self.event(key, "status_changed", t_pr, actor, to_status="in_review")
        self.event(number, "reviewer_changed", t_pr, actor,
                   people=(reviewer,), change="added")
        self.event(number, "comment_added", t_first, reviewer,
                   people=(reviewer,))
        if approved:
            self.event(number, "pr_approved", t_approval, reviewer,
                       people=(reviewer,))
        if sentinel:
            # the role is declared on the sentinel's own action; the sentinel is
            # asked after the peer answered and approves 18 h later — an
            # *observed* review interval held by one person (concepts.md §2)
            self.event(number, "reviewer_changed", t_first, sentinel,
                       people=(sentinel,), change="added", sentinel=True)
            self.event(number, "pr_approved", t_first + 18 * HOUR, sentinel,
                       people=(sentinel,), sentinel=True)
        if done:
            self.event(number, "pr_merged", t_merged, reviewer)
            self.event(key, "status_changed", t_verification, actor,
                       to_status="in_verification")
            self.event(key, "status_changed", t_done, actor, to_status="done")
        return number

    def dead_story(self, key, created, *, last_seen, number=None) -> None:
        """Abandoned work: a declined PR and no human event since ``last_seen``."""
        number = number or 100 + self._count
        self._story_object(key, created, None, "AaAa", 3)
        self._pr_object(number, created + 4 * HOUR, state="declined",
                        closed=created + 5 * HOUR, actor="AaAa", tickets=(key,))
        self.event(key, "created", created, "AaAa")
        self.event(key, "status_changed", created + 2 * HOUR, "AaAa",
                   to_status="ready")
        self.event(number, "pr_opened", created + 4 * HOUR, "AaAa")
        self.event(number, "pr_declined", created + 5 * HOUR, "AaAa")
        self.event(key, "status_changed", last_seen, "AaAa",
                   to_status="in_review")

    def unmerged_story(self, key, created, *, done_at, number=None) -> None:
        """Jira says Done, the PR is still open (prd/42 M4)."""
        number = number or 100 + self._count
        self._story_object(key, created, done_at, "AaAa", 3)
        self._pr_object(number, created + 4 * HOUR, state="open", closed=None,
                        actor="AaAa", tickets=(key,))
        self.event(key, "created", created, "AaAa")
        self.event(number, "pr_opened", created + 4 * HOUR, "AaAa")
        self.event(number, "reviewer_changed", created + 4 * HOUR,
                   "AaAa", people=("ReVw",), change="added")
        self.event(number, "comment_added", created + 8 * HOUR, "ReVw",
                   people=("ReVw",))
        self.event(key, "status_changed", done_at, "AaAa", to_status="done")

    # -- materialise -------------------------------------------------------- #
    def write(self, path: Path) -> Path:
        con = connect(path)
        try:
            ensure_schema(con)
            write_events(con, self.events, self.relations)
            write_objects(con, self.objects)
            write_run(con, {
                "run_id": "fixture-verdict",
                "source": "fixture",
                "as_of": self.as_of,
                "source_watermark": self.as_of,
                "classifier_version": "deterministic-v1",
                "prs_seen": len([o for o in self.objects if o["object_type"] == "PR"]),
                "prs_kept": len([o for o in self.objects if o["object_type"] == "PR"]),
                "prs_dropped": 0,
                "events_written": len(self.events),
                "events_skipped": 0,
            })
        finally:
            con.close()
        return path


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, fixture: Fixture):
        fixture.write(self.path)
        con = connect(self.path, read_only=True)
        try:
            court = ballcourt.load(con, calendar=None)
            data = jira.load(con, court)
        finally:
            con.close()
        return fixture, court, data


def ranked_store() -> Fixture:
    """Refinement has the *history*; review has the *removable* pool.

    That is the del-combined shape the PRD corrects: 87 stories spend 2,420 h in
    ``created → ready`` (a stable 40 h each, tail ≈ 0) while review carries the
    tail and the live waits. A verdict that ranks history picks refinement; the
    verdict must pick review.
    """
    fx = Fixture()
    created = T0
    for index in range(22):
        fx.story(
            f"S{index}", created + index * 6 * HOUR,
            refine=200,                 # the largest history, perfectly stable
            plan=4, start=2, develop=6, wait_review=1, first_review=1,
            rounds=20 + index * 12, merge=1, verify_wait=1,
            verify=3 + index % 8,        # verification has a small tail
            number=200 + index,
        )
    # two live stories in review, 60 h and 40 h in the station
    fx.story("S-LIVE1", AS_OF - 62 * HOUR, done=False, refine=1, plan=1,
             start=1, develop=2, wait_review=1, first_review=1, number=1,
             approved=False)
    fx.story("S-LIVE2", AS_OF - 42 * HOUR, done=False, refine=1, plan=1,
             start=1, develop=2, wait_review=1, first_review=1, number=2,
             approved=False)
    return fx


# --------------------------------------------------------------------------- #
# 1. the stations partition the segments
# --------------------------------------------------------------------------- #
class PartitionTests(unittest.TestCase):
    def test_every_segment_belongs_to_exactly_one_station(self):
        every = set(jira.SEGMENT_KEYS) | {
            key for key, _label in jira.VERIFICATION_SEGMENTS
        }
        self.assertEqual(set(verdict.SEGMENT_TO_STATION), every)
        seen = []
        for station in verdict.STATIONS:
            seen.extend(station["segments"])
            self.assertTrue(station["label"])
            self.assertEqual(station["clock"], "story")
        self.assertEqual(len(seen), len(set(seen)), "a segment is in two stations")

    def test_the_six_stations_keep_the_value_stream_order(self):
        self.assertEqual(verdict.STATION_IDS,
                         ("refine", "plan", "start", "develop", "review", "verify"))


# --------------------------------------------------------------------------- #
# 2. the pool: history + live, and the tail above the station's own p85
# --------------------------------------------------------------------------- #
class PoolTests(StoreCase):
    def test_the_verdict_picks_review_not_the_largest_history(self):
        fixture, court, data = self.load(ranked_store())
        window = (T0, AS_OF)
        payload = verdict.verdict(court, data, window, suggestions=(), cross_check=None)
        pools = {pool["station"]: pool for pool in payload["pools"]}
        # refinement is the biggest history and has no abnormal tail
        self.assertGreater(pools["refine"]["history_hours"],
                           pools["review"]["history_hours"])
        self.assertEqual(pools["refine"]["tail_hours"], 0.0)
        self.assertEqual(payload["constraint"]["station"], "review")
        self.assertEqual(payload["constraint"]["label"], "Approval gate")
        self.assertEqual(payload["constraint"]["clock"], "story")
        self.assertEqual(payload["constraint"]["hours_kind"], "story")
        self.assertFalse(payload["constraint"]["tied_with"])
        self.assertTrue(payload["available"])
        # removable is live + tail, never the history and never a total
        constraint = payload["constraint"]
        self.assertAlmostEqual(
            constraint["removable_hours"],
            round(constraint["live_hours"] + constraint["tail_hours"], 2),
            places=2,
        )
        self.assertLess(constraint["removable_hours"], constraint["history_hours"])

    def test_every_station_reconciles_with_an_independent_recount(self):
        _fixture, court, data = self.load(ranked_store())
        window = (T0, AS_OF)
        pools = verdict.pools(court, data, window)
        # independent recount from the segment cohort: per completed story, sum
        # its station's segments; compare with the pool's history + n.
        completed = {}
        verification = jira.uses_verification(data)
        for story in data.stories:
            cycle = jira._cycle(story)
            boundaries = cycle.boundaries
            if boundaries.get("done") is None or boundaries.get("created") is None:
                continue
            if not (window[0] <= boundaries["done"] <= window[1]):
                continue
            sums = {}
            for key in jira.segment_keys(boundaries, verification=verification):
                hours = jira.segment_hours(court, cycle, key)
                if hours is None:
                    continue
                station = verdict.SEGMENT_TO_STATION[key]
                sums[station] = round(sums.get(station, 0.0) + hours, 2)
            completed[story.key] = sums
        for station, block in pools.items():
            recount = [sums[station] for sums in completed.values() if station in sums]
            self.assertEqual(block["n_completed"], len(recount), station)
            self.assertAlmostEqual(block["history_hours"], round(sum(recount), 1),
                                   places=1, msg=station)

    def test_tail_maths_on_a_small_fixture(self):
        # A three-item cohort's nearest-rank p85 IS its maximum, so the tail is
        # honestly zero — the formula must not manufacture one.
        fx = Fixture()
        for index, rounds in enumerate((1, 2, 3)):
            fx.story(f"T{index}", T0 + index * 6 * HOUR, rounds=rounds,
                     number=300 + index)
        _fixture, court, data = self.load(fx)
        pools = verdict.pools(court, data, (T0, AS_OF))
        self.assertEqual(pools["review"]["tail_hours"], 0.0)

        # Ten items: nine at 10 h and one at 100 h → p85 = 10, tail = 90.
        fx = Fixture()
        for index in range(9):
            fx.story(f"U{index}", T0 + index * 6 * HOUR,
                     rounds=5, first_review=1, merge=1, number=400 + index)
        fx.story("OUTLIER", T0 + 60 * HOUR,
                 rounds=95, first_review=1, merge=1, number=499)
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "store.duckdb"
        _fixture, court, data = self.load(fx)
        pools = verdict.pools(court, data, (T0, AS_OF))
        review = pools["review"]
        self.assertEqual(review["n_completed"], 10)
        self.assertEqual(review["p85_hours"], 9.0)  # 3 h to first review + 5 rounds + 1
        # the outlier's 99 h station total less p85 9 h
        self.assertEqual(review["tail_hours"], 90.0)

    def test_a_min_n_window_refuses_with_a_reason(self):
        fx = Fixture()
        for index in range(12):
            fx.story(f"N{index}", T0 + index * 6 * HOUR, rounds=10 + index,
                     number=500 + index)
        _fixture, court, data = self.load(fx)
        payload = verdict.verdict(court, data, (T0, AS_OF))
        self.assertFalse(payload["available"])
        self.assertIn("needs 20", payload["note"])
        self.assertIn("completed", payload["note"])
        # the full ranking still travels — a refusal is not a blank page
        self.assertTrue(payload["pools"])
        self.assertTrue(any(pool["removable_hours"] > 0 for pool in payload["pools"]))


# --------------------------------------------------------------------------- #
# 3. liveness, adjacent findings and the sentinel
# --------------------------------------------------------------------------- #
class LivenessTests(StoreCase):
    def test_dead_live_unmerged_are_named_from_the_store(self):
        fx = ranked_store()
        fx.dead_story("S-DEAD", AS_OF - 40 * DAY, last_seen=AS_OF - 20 * DAY,
                      number=9)
        fx.unmerged_story("S-UNMERGED", AS_OF - 10 * DAY,
                          done_at=AS_OF - 5 * DAY, number=10)
        _fixture, court, data = self.load(fx)
        rows = {row["key"]: row for row in verdict.liveness(court, data)}
        self.assertEqual(rows["S-DEAD"]["state"], "dead")
        self.assertEqual(rows["S-DEAD"]["station"], "review")
        self.assertEqual(rows["S-UNMERGED"]["state"], "unmerged")
        self.assertEqual(rows["S-LIVE1"]["state"], "live")
        payload = verdict.verdict(court, data, (T0, AS_OF))
        adjacent = {row["kind"]: row for row in payload["adjacent"]}
        self.assertEqual(adjacent["dead"]["n"], 1)
        self.assertEqual(adjacent["dead"]["items"][0]["key"], "S-DEAD")
        self.assertEqual(adjacent["dead"]["hours_kind"], "story")
        self.assertEqual(adjacent["unmerged"]["n"], 1)
        self.assertEqual(adjacent["unmerged"]["items"][0]["pr"], "#10")
        self.assertEqual(adjacent["unmerged"]["clock"], "pr-holder")
        self.assertIn("not capacity", adjacent["dead"]["sentence"])

    def test_a_bot_sweep_does_not_reset_the_dead_clock(self):
        fx = Fixture()
        fx.person("BiBi", kind="bot")
        fx.dead_story("B-DEAD", AS_OF - 40 * DAY, last_seen=AS_OF - 20 * DAY,
                      number=11)
        # the automation touches the story yesterday; it must not count as a
        # human event, so the item stays dead (concepts.md §2)
        fx.event("B-DEAD", "status_changed", AS_OF - 1 * DAY, "BiBi",
                 to_status="in_review")
        _fixture, court, data = self.load(fx)
        row = next(row for row in verdict.liveness(court, data)
                   if row["key"] == "B-DEAD")
        self.assertEqual(row["state"], "dead")
        self.assertIn("no open PR", row["reason"])

    def test_the_holders_name_the_sentinel_and_never_a_bot(self):
        fx = Fixture()
        fx.person("BiBi", kind="bot")
        # a completed review whose sentinel did the approval
        fx.story("H1", T0, rounds=20, number=12, sentinel="FrMo")
        # a live review waiting on the sentinel alone
        fx.story("H2", AS_OF - 30 * HOUR, done=False, rounds=0, merge=0,
                 wait_review=1, first_review=1, number=13, sentinel="FrMo")
        _fixture, court, data = self.load(fx)
        payload = verdict.verdict(court, data, (T0, AS_OF))
        holders = payload["holders"]
        self.assertEqual(holders["clock"], "pr-holder")
        people = {person["person"]: person for person in holders["people"]}
        self.assertIn("FrMo", people)
        self.assertEqual(people["FrMo"]["role"], "sentinel")
        self.assertNotIn("BiBi", people)
        self.assertIn("response time", holders["note"])
        # the review detail is the PR ball clock, stated next to the story pool
        self.assertEqual(holders["clock"], "pr-holder")
        self.assertIn("never added", payload["holder_note"])


# --------------------------------------------------------------------------- #
# 4. ties, the ladder and the rung map
# --------------------------------------------------------------------------- #
def pool(station: str, hours: float, *, live: float | None = None,
         tail: float | None = None, label: str | None = None) -> dict:
    """One pool-table row: ``hours`` is the reference pool (live + tail)."""
    live = hours if live is None else live
    tail = max(0.0, hours - live) if tail is None else tail
    return {
        "station": station,
        "label": label or station.title(),
        "removable_hours": hours,
        "live_hours": live,
        "tail_hours": tail,
        "removable_now_hours": live,
    }


class TieTests(unittest.TestCase):
    def test_nine_percent_is_a_tie_and_eleven_is_not(self):
        table = {
            "refine": pool("refine", 100.0),
            "review": pool("review", 91.0),    # 9 % behind: tied
        }
        block = verdict.constraint(table)
        self.assertEqual(block["station"], "refine")
        self.assertEqual([row["station"] for row in block["tied_with"]], ["review"])

        table["review"]["removable_hours"] = 89.0   # 11 % behind: not tied
        table["review"]["removable_now_hours"] = 89.0
        table["review"]["live_hours"] = 89.0
        block = verdict.constraint(table)
        self.assertEqual(block["tied_with"], [])

    def test_no_pool_is_no_constraint(self):
        self.assertIsNone(verdict.constraint({}))
        self.assertIsNone(verdict.constraint({"refine": pool("refine", 0.0)}))


class LiveFirstTests(unittest.TestCase):
    """prd/67 W-04: the verdict ranks what is owed now, never the historical tail."""

    def test_zero_live_everywhere_is_no_constraint_and_names_the_history(self):
        table = {
            "refine": pool("refine", 2345.0, live=0, tail=2312.0,
                           label="Refinement"),
            "start": pool("start", 1830.0, live=0, tail=1817.0,
                          label="Starting work"),
            "verify": pool("verify", 91.0, live=0, tail=91.0,
                           label="Verification"),
            "plan": pool("plan", 50.0, live=0, tail=50.0, label="Sprint planning"),
        }
        # A 0-owed historical tail is not a current constraint.
        self.assertIsNone(verdict.constraint(table))
        history = verdict.historical(table)
        self.assertEqual([row["station"] for row in history],
                         ["refine", "start", "verify"])   # top 3, by tail
        tails = [row["tail_hours"] for row in history]
        self.assertEqual(tails, sorted(tails, reverse=True))
        note = verdict.no_queue_note(table)
        self.assertIn("No current queue is visible in this window: 0 h owed now", note)
        self.assertIn("Refinement (2312 h)", note)
        self.assertIn("Starting work (1817 h)", note)
        self.assertIn("history, not a live constraint", note)
        self.assertNotIn("The constraint is", note)

    def test_zero_live_with_no_history_says_so_without_a_station(self):
        table = {"review": pool("review", 0.0, live=0, tail=0)}
        self.assertIsNone(verdict.constraint(table))
        self.assertEqual(verdict.historical(table), [])
        note = verdict.no_queue_note(table)
        self.assertIn("0 h owed now", note)
        self.assertIn("No completed story left an abnormal tail", note)

    def test_a_live_station_beats_a_larger_historical_tail(self):
        table = {
            "refine": pool("refine", 5000.0, live=0, tail=5000.0,
                           label="Refinement"),
            "review": pool("review", 120.0, live=120.0, tail=0.0,
                           label="Approval gate"),
        }
        block = verdict.constraint(table)
        self.assertEqual(block["station"], "review")
        self.assertEqual(block["removable_now_hours"], 120.0)
        self.assertEqual(block["removable_hours"], 120.0)
        self.assertEqual(block["tied_with"], [])

    def test_ties_are_within_ten_percent_of_the_top_live(self):
        table = {
            "refine": pool("refine", 100.0, live=100.0, tail=0),
            "review": pool("review", 91.0, live=91.0, tail=0),   # 9 %: tied
            "verify": pool("verify", 5000.0, live=0, tail=5000.0),  # history
        }
        block = verdict.constraint(table)
        self.assertEqual(block["station"], "refine")
        self.assertEqual([row["station"] for row in block["tied_with"]], ["review"])
        self.assertEqual(block["tied_with"][0]["removable_now_hours"], 91.0)

        table["review"] = pool("review", 89.0, live=89.0, tail=0)  # 11 %: not tied
        block = verdict.constraint(table)
        self.assertEqual(block["tied_with"], [])


class JiraOnlyVerdictTests(StoreCase):
    """The live symptom: a Jira-only window has live = 0 at every station."""

    def test_a_jira_only_window_says_no_current_queue(self):
        fx = ranked_store()
        # Strip the PR half exactly as a Jira-only scrape would: no PR objects
        # and no PR events, so no story can have a live PR.
        fx.objects = [o for o in fx.objects if o["object_type"] != "PR"]
        fx.events = [e for e in fx.events if e["repo"] is None]
        _fixture, court, data = self.load(fx)
        payload = verdict.verdict(court, data, (T0, AS_OF))

        self.assertIsNone(payload["constraint"])
        self.assertTrue(payload["available"])          # the data is fine
        self.assertIn("No current queue is visible in this window: 0 h owed now",
                      payload["note"])
        self.assertIn("history, not a live constraint", payload["note"])
        self.assertEqual(payload["next"], [])
        # history travels as history: the top three tails, sorted
        history = payload["historical"]
        self.assertTrue(history)
        self.assertLessEqual(len(history), 3)
        tails = [row["tail_hours"] for row in history]
        self.assertEqual(tails, sorted(tails, reverse=True))
        self.assertGreater(tails[0], 0)
        self.assertIn(history[0]["label"], payload["note"])
        for row in history:
            self.assertEqual(set(row), {"station", "label", "tail_hours"})
        # the old rule would have named a station: the biggest removable pool
        # here owes nothing now, which is exactly what must not be the verdict.
        pools = {pool["station"]: pool for pool in payload["pools"]}
        biggest = max(pools.values(), key=lambda pool: pool["removable_hours"])
        self.assertEqual(biggest["live_hours"], 0.0)
        self.assertEqual(biggest["removable_now_hours"], 0.0)
        # the ladder still travels (its exploit rules are real), but the card's
        # sentence never names a station.
        self.assertNotIn("The constraint is", payload["note"])


def suggestion(rule_id: str, *, impact: float = 10.0) -> dict:
    return {
        "id": rule_id, "title": f"{rule_id} rule", "metric": "m",
        "severity": "warning", "impact_hours": impact, "rank": 1,
        "policy_lab": {
            "policy": "sla4", "label": "review SLA 4 h",
            "contradiction": False, "supports": True,
            "note": "the policy lab models this at −9 % cycle",
        },
    }


class LadderTests(unittest.TestCase):
    def test_elevate_is_blocked_iff_exploit_is_open(self):
        cross = {"available": True, "policies": []}
        blocked = verdict.ladder([suggestion("stale-prs")], cross, exploit_n=3)
        states = {rung["step"]: rung for rung in blocked}
        self.assertEqual(states["exploit"]["state"], "open")
        self.assertEqual(states["elevate"]["state"], "blocked")
        self.assertIn("exploit first: 3 items", states["elevate"]["reason"])

        # exploit done (no rule, no owed item): elevate is a normal rung, never
        # "blocked" for its own sake
        open_ladder = verdict.ladder(
            [suggestion("sentinel-queue")], cross, exploit_n=0,
        )
        states = {rung["step"]: rung for rung in open_ladder}
        self.assertEqual(states["exploit"]["state"], "done")
        self.assertEqual(states["elevate"]["state"], "open")

        # and with nothing owed and no elevate rule it is `done`, not blocked
        done_ladder = verdict.ladder([], cross, exploit_n=0)
        states = {rung["step"]: rung for rung in done_ladder}
        self.assertEqual(states["elevate"]["state"], "done")

    def test_the_rung_order_and_the_lab_note(self):
        cross = {"available": True, "policies": []}
        rungs = verdict.ladder([suggestion("round-cap")], cross, exploit_n=1)
        self.assertEqual([rung["step"] for rung in rungs],
                         ["exploit", "subordinate", "elevate"])
        exploit = rungs[0]
        self.assertEqual(exploit["rules"][0]["id"], "round-cap")
        self.assertEqual(exploit["rules"][0]["lab"]["note"],
                         "the policy lab models this at −9 % cycle")
        # no advice → the rung says so instead of inventing a verdict
        self.assertIn("not the lab", exploit["note"])

    def test_measured_rules_never_carry_a_rung(self):
        self.assertEqual(set(verdict.RUNGS),
                         {"exploit", "subordinate", "elevate", "measured"})
        levers = set(verdict.RUNGS["exploit"]) | set(verdict.RUNGS["subordinate"]) \
            | set(verdict.RUNGS["elevate"])
        self.assertFalse(levers & set(verdict.RUNGS["measured"]))
        for rule_id in verdict.RUNGS["measured"]:
            self.assertEqual(verdict.rung_for(rule_id), "measured")
        # the generated person rules keep their static sibling's rung
        self.assertEqual(verdict.rung_for("reviewer-AaAa"), "elevate")
        self.assertEqual(verdict.rung_for("vacation-AaAa-2026-01-01"), "subordinate")

    def test_every_templates_rule_id_appears_exactly_once(self):
        source = (REPO_ROOT / "analysis" / "rules.py").read_text()
        ids = re.findall(r'_suggestion\(\s*\n?\s*(f?"[^"]+")', source)
        static = [value.strip('"') for value in set(ids) if not value.startswith('f"')]
        families = [value for value in set(ids) if value.startswith('f"')]
        rung_of = {}
        for rung, members in verdict.RUNGS.items():
            for rule_id in members:
                rung_of.setdefault(rule_id, []).append(rung)
        for rule_id in static:
            with self.subTest(rule=rule_id):
                self.assertIn(rule_id, rung_of)
                self.assertEqual(len(rung_of[rule_id]), 1)
        # the two f-string families are mapped by prefix, not left unplaced
        self.assertTrue(families)
        for value in families:
            match = re.search(r'f"([a-z-]+)\{', value)
            self.assertIsNotNone(match, value)
            mapped = verdict.rung_for(f"{match.group(1)}X")
            self.assertIsNotNone(mapped, value)


# --------------------------------------------------------------------------- #
# 5. the payload's honesty language and the disclosure map
# --------------------------------------------------------------------------- #
class PayloadTests(StoreCase):
    def test_one_constraint_one_clock_and_the_next_ranking(self):
        _fixture, court, data = self.load(ranked_store())
        payload = verdict.verdict(court, data, (T0, AS_OF))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["constraint"]["station"], "review")
        self.assertIn("Pool = business hours", payload["note"])
        self.assertIn("story clock", payload["basis"])
        self.assertIn("PR ball clock", payload["basis"])
        self.assertEqual(payload["current_rung"], "exploit")
        self.assertTrue(payload["next"])
        self.assertNotIn("review", [row["station"] for row in payload["next"]])
        # the client map covers every static rule and names the families
        mapping = payload["rule_rungs"]
        self.assertEqual(mapping["by_rule"]["round-cap"], "exploit")
        self.assertEqual(mapping["by_rule"]["sprint-on-time"], "measured")
        self.assertEqual(mapping["order"],
                         ["exploit", "subordinate", "elevate"])
        # adjacent findings are present even when empty (a stated zero)
        kinds = {row["kind"] for row in payload["adjacent"]}
        self.assertEqual(kinds, {"dead", "unmerged"})
        # the exploit block carries the ranked items, and the ladder names them
        self.assertTrue(payload["exploit"]["items"])
        elevate = next(rung for rung in payload["ladder"]
                       if rung["step"] == "elevate")
        self.assertEqual(elevate["state"], "blocked")

    def test_no_ticket_layer_keeps_the_pr_stations_and_says_so(self):
        # a store with no JIRA stories has develop/review pools only
        fx = Fixture()
        fx._pr_object(21, T0, state="merged",
                      closed=T0 + 10 * HOUR, actor="AaAa", tickets=("ENG-1",))
        fx.event(21, "pr_opened", T0, "AaAa")
        fx.event(21, "reviewer_changed", T0, "AaAa",
                 people=("ReVw",), change="added")
        fx.event(21, "comment_added", T0 + 2 * HOUR, "ReVw",
                 people=("ReVw",))
        fx.event(21, "pr_merged", T0 + 10 * HOUR, "ReVw")
        fixture, court, data = self.load(fx)
        payload = verdict.verdict(court, data, (T0, AS_OF))
        self.assertFalse(payload["available"])
        self.assertIn("completed stories", payload["note"])
        # no invented story hours: every station's history is 0
        self.assertTrue(all(pool["history_hours"] == 0 for pool in payload["pools"]))


# --------------------------------------------------------------------------- #
# 6. the del-combined evidence (only when the store is present)
# --------------------------------------------------------------------------- #
def _del_combined_path() -> Path | None:
    candidates = []
    env = os.environ.get("MEGASPEED_DB")
    if env:
        candidates.append(Path(env))
    candidates.append(REPO_ROOT / "data" / "del-combined.duckdb")
    for path in candidates:
        if path.name == "del-combined.duckdb" and path.exists():
            return path
    return None


@unittest.skipUnless(
    _del_combined_path() is not None,
    "the del-combined store is not present",
)
class DelCombinedVerdictTests(unittest.TestCase):
    """The PRD's evidence: review ≈ 620 h removable, not refinement's history."""

    @classmethod
    def setUpClass(cls):
        from analysis import calendar as workcal

        path = _del_combined_path()
        cls.path = path
        con = connect(path, read_only=True)
        try:
            calendar = workcal.load(
                tenant=path.stem, tenants_dir=path.parent,
                config_path=REPO_ROOT / "config" / "calendar.default.yaml",
            )
            cls.court = ballcourt.load(con, calendar=calendar)
            cls.data = jira.load(con, cls.court)
        finally:
            con.close()
        cls.window = rules.window_for(cls.court, None, cls.data)
        cls.payload = verdict.verdict(cls.court, cls.data, cls.window)
        # The ladder's lab notes must be the real advice payload /api/insights
        # uses. Running the lab's presets is cheap on this store (cached by the
        # server in production); the test runs them once for the class.
        from server import app as app_module

        con = connect(path, read_only=True)
        try:
            cls.cross = app_module._advice_payload(
                con, path.stem, path, cls.court, cls.data, cls.window,
            )
            cls.insights = rules.insights(
                cls.court, [], cls.window, jira=cls.data,
                policy_lab=cls.cross,
            )
        finally:
            con.close()
        cls.ladder_payload = verdict.verdict(
            cls.court, cls.data, cls.window,
            suggestions=cls.insights["suggestions"], cross_check=cls.cross,
        )

    def test_the_constraint_is_the_approval_gate(self):
        constraint = self.payload["constraint"]
        self.assertTrue(self.payload["available"])
        self.assertEqual(constraint["station"], "review")
        self.assertEqual(constraint["label"], "Approval gate")
        self.assertEqual(constraint["n_completed"], 87)
        self.assertEqual(constraint["clock"], "story")
        # removable ≈ 620 h = 238 live + 382 tail (the research's numbers)
        self.assertAlmostEqual(constraint["removable_hours"],
                               round(constraint["live_hours"]
                                     + constraint["tail_hours"], 2), places=2)
        self.assertGreater(constraint["removable_hours"], 600)
        self.assertGreater(constraint["live_hours"], 220)
        self.assertGreater(constraint["tail_hours"], 370)
        self.assertEqual(constraint["tied_with"], [])
        # the ranking is removable, not history: refinement carries 2,419.8 h of
        # history and only 33 h of removable tail — it must not win
        pools = {pool["station"]: pool for pool in self.payload["pools"]}
        self.assertGreater(pools["refine"]["history_hours"],
                           pools["refine"]["removable_hours"] * 10)
        self.assertLess(pools["refine"]["removable_hours"],
                        constraint["removable_hours"] / 10)

    def test_the_next_ranking_matches_the_research(self):
        next_rows = {row["station"]: row["removable_hours"]
                     for row in self.payload["next"]}
        self.assertAlmostEqual(next_rows["verify"], 80.8, delta=1.5)
        self.assertAlmostEqual(next_rows["start"], 41.2, delta=1.5)
        self.assertAlmostEqual(next_rows["refine"], 33.3, delta=1.5)
        self.assertEqual(list(next_rows)[0], "verify")

    def test_the_holder_split_names_the_sentinel(self):
        holders = self.payload["holders"]
        people = {person["person"]: person for person in holders["people"]}
        self.assertEqual(people["FrMo"]["role"], "sentinel")
        self.assertAlmostEqual(people["FrMo"]["hours"], 961.3, delta=1.0)
        self.assertAlmostEqual(people["FrMo"]["all_phase_hours"], 1882.4,
                               delta=1.0)
        peers = holders["peer_range"]
        self.assertAlmostEqual(peers[0], 142.2, delta=1.0)
        self.assertAlmostEqual(peers[1], 230.8, delta=1.0)

    def test_the_adjacent_findings_match_the_research(self):
        adjacent = {row["kind"]: row for row in self.payload["adjacent"]}
        self.assertEqual(adjacent["dead"]["n"], 9)
        self.assertAlmostEqual(adjacent["dead"]["hours"], 2903.5, delta=2.0)
        self.assertEqual(adjacent["dead"]["items"][0]["key"], "DEL-114")
        self.assertEqual(adjacent["unmerged"]["n"], 3)
        self.assertAlmostEqual(adjacent["unmerged"]["hours"], 344.9, delta=2.0)
        self.assertEqual(adjacent["unmerged"]["items"][0]["pr"], "#53")

    def test_the_exploit_rung_names_the_items_and_blocks_elevate(self):
        exploit = self.ladder_payload["exploit"]
        self.assertEqual([item["pr"] for item in exploit["items"]],
                         ["#53", "#55", "#60"])
        self.assertEqual([item["pr"] for item in exploit["orphans"]],
                         ["#55", "#60", "#52"])
        # the ladder's exploit rung names the same items
        exploit_rung = next(rung for rung in self.ladder_payload["ladder"]
                            if rung["step"] == "exploit")
        self.assertEqual([item["pr"] for item in exploit_rung["items"]],
                         ["#53", "#55", "#60"])
        states = {rung["step"]: rung for rung in self.ladder_payload["ladder"]}
        self.assertEqual(states["exploit"]["state"], "open")
        self.assertEqual(states["elevate"]["state"], "blocked")
        self.assertIn("exploit first", states["elevate"]["reason"])
        # the lab's verdict rides the rung, byte-for-byte the Rules note
        notes = {lab["id"]: lab["note"] for lab in states["elevate"]["labs"]}
        self.assertIn("no clear effect", notes["sentinel2"])
        exploit_notes = {lab["id"]: lab["note"]
                         for lab in states["exploit"]["labs"]}
        self.assertIn("−13 %", exploit_notes["roundcap2"])
        self.assertIn("−9 %", exploit_notes["sla4"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

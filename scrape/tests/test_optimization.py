"""Huge-project scrape: the pre-filter, the watermark, settled PRs, early stop.

prd/14 makes a big Bitbucket project affordable by never paying for a PR that
cannot matter:

1. the PR **list** is read cheaply (``limit=100``, ``order=NEWEST``); each row
   carries ``title``, ``fromRef``, ``description``, ``state``, ``updatedDate``;
2. a PR that carries no target ticket key on any of those costs *zero* further
   requests (no detail, no commits, no activities);
3. a MERGED/DECLINED PR scraped once at its current ``updatedDate`` is
   **settled** -- never fetched again unless that date moves; any other
   unchanged PR is skipped by the watermark too;
4. DC 9.4.18 sorts ``order=NEWEST`` by ``updatedDate`` descending, so pagination
   stops at the first full page that is entirely at/below the watermark.

These tests count requests at the transport, so "no requests" means no requests.

    python3 -m unittest discover -s scrape/tests -v
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from ingest.bundle import read_bundle
from ingest.config import Config
from ingest.scrape import scrape
from ingest.transport import FixtureTransport

T0 = 1_790_000_000_000  # epoch ms
MINUTE = 60_000
HASH = "a" * 40
HASH_HEAD = "b" * 40
ADA = {"name": "ada", "displayName": "Ada Lovelace", "type": "NORMAL"}

_PR_PATH = re.compile(r"/pull-requests(?:/(\d+)(?:/(activities|commits))?)?$")

MATCHING = list(range(1, 11))  # PR ids that carry a ticket key
CLOSED = {"MERGED": [1, 2, 3, 4, 5], "DECLINED": [6, 7, 8]}
OPEN = [9, 10]
TICKETED = {**{pr_id: "MERGED" for pr_id in CLOSED["MERGED"]},
            **{pr_id: "DECLINED" for pr_id in CLOSED["DECLINED"]},
            **{pr_id: "OPEN" for pr_id in OPEN}}
HUGE = 500
# Wide enough to keep every re-scraped event (the overlap window of prd/11 §3).
NO_CUTOFF = 24 * 365


def _kind(path: str) -> str:
    """Classify a request path independently of the code under test."""
    match = _PR_PATH.search(path)
    assert match, f"unexpected path {path}"
    if match.group(1) is None:
        return "list"
    return match.group(2) or "detail"


def _pr_id(path: str) -> int | None:
    match = _PR_PATH.search(path)
    return int(match.group(1)) if match and match.group(1) else None


def _activities(pr_id: int, state: str) -> list[dict]:
    activities = [{"id": pr_id * 100 + 1, "createdDate": T0 + pr_id * MINUTE,
                   "action": "OPENED", "user": ADA}]
    if state == "MERGED":
        activities.append({"id": pr_id * 100 + 2, "createdDate": T0 + pr_id * MINUTE + 5 * MINUTE,
                           "action": "MERGED", "user": ADA,
                           "commit": {"id": "m" * 40, "author": ADA, "committer": ADA,
                                      "authorTimestamp": T0, "committerTimestamp": T0,
                                      "message": "merge the thing"}})
    elif state == "DECLINED":
        activities.append({"id": pr_id * 100 + 2, "createdDate": T0 + pr_id * MINUTE + 5 * MINUTE,
                           "action": "DECLINED", "user": ADA})
    return activities


def _record(pr_id: int, *, state: str, key: str | None, updated_minute: int) -> dict:
    """One fixture PR record, shaped like the DC REST payloads."""
    closed = state != "OPEN"
    slug = "feature/plain-thing" if key is None else f"feature/{key}-{pr_id}"
    return {
        "pull_request": {
            "id": pr_id,
            "version": 1,
            "title": ("plain thing" if key is None else f"{key}: thing {pr_id}"),
            "description": "zzcanary description",
            "state": state,
            "open": state == "OPEN",
            "closed": closed,
            "draft": False,
            "createdDate": T0 + pr_id * MINUTE,
            "updatedDate": T0 + updated_minute * MINUTE,
            "closedDate": T0 + updated_minute * MINUTE if closed else None,
            "fromRef": {"id": f"refs/heads/{slug}", "displayId": slug, "type": "BRANCH",
                        "latestCommit": HASH_HEAD},
            "toRef": {"id": "refs/heads/main", "displayId": "main", "type": "BRANCH",
                      "latestCommit": HASH},
            "author": {"user": ADA, "role": "AUTHOR", "approved": False},
            "reviewers": [],
            "participants": [],
            "properties": {},
        },
        "activities": _activities(pr_id, state),
        "commits": [{
            "id": f"{pr_id:040d}", "displayId": f"{pr_id:040d}"[:7], "author": ADA,
            "authorTimestamp": T0 + pr_id * MINUTE, "committer": ADA,
            "committerTimestamp": T0 + pr_id * MINUTE, "message": "add the thing",
        }],
    }


def huge_bundle(total: int = HUGE, *, matching: list[int] | None = None,
                updated_minute=None) -> dict:
    """A project of ``total`` PRs of which the ``matching`` ids carry a key.

    Records are ordered ``updatedDate`` **descending**, which is what DC returns
    for ``order=NEWEST`` (prd/14 §4) -- the early-termination tests need that.
    """
    matching = MATCHING if matching is None else matching
    updated_minute = updated_minute or (lambda pr_id: pr_id)
    records = []
    for pr_id in range(1, total + 1):
        state = TICKETED.get(pr_id, "MERGED")
        key = f"DEL-{pr_id}" if pr_id in matching else None
        records.append(_record(pr_id, state=state, key=key,
                               updated_minute=updated_minute(pr_id)))
    records.sort(key=lambda record: record["pull_request"]["updatedDate"], reverse=True)
    return {
        "schema": "megaspeed.fixture/1",
        "projects": [{"key": "DEL", "repos": [{"slug": "huge", "pull_requests": records}]}],
    }


class RecordingTransport:
    """Forwards to the fixture transport and remembers every request."""

    def __init__(self, bundle: dict, *, lean_list: bool = False):
        self.inner = FixtureTransport(bundle)
        self.lean_list = lean_list
        self.paths: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        self.paths.append((path, params))
        page = self.inner.get(path, params)
        if self.lean_list and _kind(path) == "list":
            # A source whose list projection is trimmed to the cheap fields: the
            # scrape must then pay one detail request per *kept* PR.
            page = dict(page)
            page["values"] = [
                {k: v for k, v in row.items()
                 if k in ("id", "title", "state", "fromRef", "updatedDate", "author")}
                for row in page["values"]
            ]
        return page

    # -- inspection helpers ------------------------------------------------
    def kinds(self) -> Counter:
        return Counter(_kind(path) for path, _ in self.paths)

    def touched_prs(self) -> set[int]:
        """PR ids that cost a detail/commits/activities request."""
        return {pid for path, _ in self.paths if (pid := _pr_id(path)) is not None}

    def list_params(self) -> list[dict]:
        return [params for path, params in self.paths if _kind(path) == "list"]


class HugeProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=self.root / "unused.duckdb",
            identity_path=self.root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("DEL", "huge")],
            ticket_projects=("DEL",),
        )
        self.out = self.root / "export" / "huge.tgz"
        self.state_dir = self.root / "export" / "huge.state"

    def tearDown(self):
        self.tmp.cleanup()

    def run_scrape(self, bundle: dict | None = None, *, overlap_hours: int = NO_CUTOFF,
                   out: Path | None = None, state_dir: Path | None = None,
                   full: bool = False, **transport_kwargs) -> tuple[dict, RecordingTransport]:
        transport = RecordingTransport(bundle or huge_bundle(), **transport_kwargs)
        report = scrape(
            self.cfg, transport, "fixture",
            out=out or self.out,
            state_dir=state_dir or self.state_dir,
            overlap_hours=overlap_hours,
            full=full,
        )
        return report, transport

    # -- 1. the ticket pre-filter -----------------------------------------
    def test_only_matching_prs_cost_a_per_pr_request(self):
        report, transport = self.run_scrape()

        self.assertEqual(report["prs_seen"], HUGE)
        self.assertEqual(report["prs_kept"], len(MATCHING))
        self.assertEqual(report["prs_dropped"], HUGE - len(MATCHING))
        self.assertEqual(
            report["prs_kept"] + report["prs_dropped"] + report["prs_skipped_settled"]
            + report["prs_skipped_watermark"],
            report["prs_seen"],
        )
        # 500 PRs / 100 per page = 5 cheap list requests -- and nothing else but
        # the commits + activities of the 10 ticketed PRs.
        self.assertEqual(transport.kinds(), Counter({"list": 5, "commits": 10, "activities": 10}))
        self.assertEqual(transport.touched_prs(), set(MATCHING))

    def test_requests_are_newest_first_and_paged_at_100(self):
        _report, transport = self.run_scrape()
        for params in transport.list_params():
            self.assertEqual(params["order"], "NEWEST")
            self.assertEqual(params["limit"], 100)

    def test_a_project_with_no_matching_pr_costs_only_the_listing(self):
        report, transport = self.run_scrape(huge_bundle(matching=[]))
        self.assertEqual(report["prs_kept"], 0)
        self.assertEqual(transport.kinds(), Counter({"list": 5}))
        self.assertEqual(transport.touched_prs(), set())

    def test_a_key_only_in_the_description_matches_on_the_list_row(self):
        """DC's list row carries ``description``: no detail request is needed."""
        bundle = huge_bundle(total=1, matching=[])
        record = bundle["projects"][0]["repos"][0]["pull_requests"][0]
        record["pull_request"]["description"] = "Implements: DEL-77 (description only)"

        report, transport = self.run_scrape(bundle)
        self.assertEqual(report["prs_kept"], 1)
        self.assertEqual(report["keys_by_placement"].get("description"), 1)
        self.assertEqual(transport.kinds(), Counter({"list": 1, "commits": 1, "activities": 1}))
        tickets = {
            o["object_id"] for o in read_bundle(self.out).objects if o["object_type"] == "TICKET"
        }
        self.assertEqual(tickets, {"TICKET:DEL-77"})

    def test_trimmed_list_rows_are_topped_up_with_one_detail_request(self):
        report, transport = self.run_scrape(lean_list=True)
        self.assertEqual(report["prs_kept"], len(MATCHING))
        self.assertEqual(
            transport.kinds(),
            Counter({"list": 5, "detail": 10, "commits": 10, "activities": 10}),
        )
        self.assertEqual(transport.touched_prs(), set(MATCHING))

    def test_old_path_costs_a_commit_request_per_pr(self):
        """``--no-prefilter`` is the previous behaviour, kept as a measurement."""
        self.cfg.prefilter = False
        report, transport = self.run_scrape()
        self.assertEqual(report["prs_kept"], len(MATCHING))
        self.assertEqual(report["prs_dropped"], HUGE - len(MATCHING))
        self.assertEqual(transport.kinds()["commits"], HUGE)  # commits for all 500
        self.assertEqual(transport.kinds()["activities"], len(MATCHING))
        self.assertEqual(len(transport.paths), 5 + HUGE + len(MATCHING))

        # ... and the emitted rows are the same as the pre-filtered run's
        self.cfg.prefilter = True
        other = self.root / "export" / "prefiltered.tgz"
        self.run_scrape(out=other, state_dir=self.root / "export" / "prefiltered.state")
        with_filter = read_bundle(other)
        without = read_bundle(self.out)
        self.assertEqual(
            sorted(e["event_key"] for e in without.events),
            sorted(e["event_key"] for e in with_filter.events),
        )
        self.assertEqual(
            sorted(o["object_id"] for o in without.objects),
            sorted(o["object_id"] for o in with_filter.objects),
        )

    # -- 2. the watermark + 3. settled finality ---------------------------
    def test_second_run_skips_settled_prs_and_rechecks_only_the_boundary(self):
        # Put the ticketed PRs on the newest page, so an incremental run walks
        # over them (and skips them) instead of stopping before the old tail.
        bundle = huge_bundle(
            updated_minute=lambda pr_id: 1000 + pr_id if pr_id in MATCHING else pr_id
        )
        self.run_scrape(bundle)

        report, transport = self.run_scrape(bundle)
        # 8 closed PRs sit strictly below the watermark and are settled; the
        # other open one is too. PR 10 *is* the watermark (the newest date, a
        # tie) and is read again -- ties are ambiguous, and re-reading is
        # idempotent, so the boundary is never silently dropped.
        self.assertEqual(report["prs_skipped_settled"], 8)
        self.assertEqual(report["prs_skipped_watermark"], 1)
        self.assertEqual(report["prs_kept"], 1)
        self.assertEqual(transport.touched_prs(), {10})
        self.assertEqual(transport.kinds(), Counter({"list": 2, "commits": 1, "activities": 1}))
        self.assertEqual(report["early_stopped"], ["DEL/huge"])
        self.assertEqual({event["pr_id"] for event in read_bundle(self.out).events}, {10})

    def test_prs_tied_with_the_watermark_are_not_settled(self):
        """``updatedDate`` is not unique: a tie is 'needs processing', not 'done'."""
        bundle = huge_bundle(
            total=3, matching=[1, 2, 3],
            updated_minute=lambda pr_id: 100 if pr_id in (2, 3) else pr_id,
        )
        self.run_scrape(bundle)

        report, transport = self.run_scrape(bundle)
        # PR 2 and PR 3 share the newest updatedDate, which *is* the watermark,
        # so neither may be skipped; PR 1 is strictly below it and is settled.
        self.assertEqual(transport.touched_prs(), {2, 3})
        self.assertEqual(report["prs_kept"], 2)
        self.assertEqual(report["prs_skipped_settled"], 1)

    def test_a_settled_pr_comes_back_only_when_its_updated_date_moves(self):
        self.run_scrape()
        # A late comment on an *old* closed PR bumps its updatedDate past the
        # watermark, so it is re-read (and settles again) -- and it is now the
        # newest row, so the listing stops right after it.
        moved = huge_bundle(
            updated_minute=lambda pr_id: 5000 + pr_id if pr_id == 3 else pr_id
        )

        report, transport = self.run_scrape(moved)
        self.assertEqual(report["prs_kept"], 1)
        self.assertEqual(transport.touched_prs(), {3})
        self.assertEqual(transport.kinds()["list"], 2)  # page with #3, then stop
        self.assertEqual(report["early_stopped"], ["DEL/huge"])

        # Recorded at the new date: it is now the watermark itself, so it is
        # re-read as a boundary tie -- and nothing else is.
        _report, transport = self.run_scrape(moved)
        self.assertEqual(transport.touched_prs(), {3})
        self.assertEqual(transport.kinds()["list"], 2)

    def test_a_pr_that_moved_below_the_watermark_is_not_skipped(self):
        """The cursor is the PR's own date: below the watermark but newer than
        what we recorded means it moved, so it is read (no lost events)."""
        self.run_scrape(huge_bundle(
            total=3, matching=[1, 2, 3],
            updated_minute=lambda pr_id: 10 if pr_id == 3 else pr_id,
        ))
        # PR 1 moves, but stays below PR 3's updatedDate (the watermark).
        moved = huge_bundle(
            total=3, matching=[1, 2, 3],
            updated_minute=lambda pr_id: 4 if pr_id == 1 else (10 if pr_id == 3 else 2),
        )
        report, transport = self.run_scrape(moved)
        # PR 1 moved (below the watermark, newer than its cursor); PR 3 is the
        # boundary tie; PR 2 is strictly below and untouched, so it is skipped.
        self.assertEqual(transport.touched_prs(), {1, 3})
        self.assertEqual(report["prs_kept"], 2)

    def test_a_moved_open_pr_is_rechecked(self):
        self.run_scrape()
        moved = huge_bundle(updated_minute=lambda pr_id: 6000 + pr_id if pr_id == 9 else pr_id)
        report, transport = self.run_scrape(moved)
        self.assertEqual(report["prs_kept"], 1)
        self.assertEqual(transport.touched_prs(), {9})
        self.assertEqual(read_bundle(self.out).events[0]["pr_id"], 9)

    def test_full_rescrape_ignores_the_cursors(self):
        self.run_scrape()
        report, transport = self.run_scrape(full=True)
        self.assertEqual(report["prs_skipped_settled"], 0)
        self.assertEqual(report["prs_skipped_watermark"], 0)
        self.assertEqual(report["prs_kept"], len(MATCHING))
        self.assertEqual(transport.touched_prs(), set(MATCHING))
        self.assertTrue(report["full"])
        self.assertIsNone(report["previous_watermark"])

    def test_a_truncated_listing_does_not_advance_the_repo_watermark(self):
        full_report, _transport = self.run_scrape()
        self.assertIsNotNone(full_report["repo_watermarks"]["DEL/huge"])

        capped = scrape(
            self.cfg, RecordingTransport(huge_bundle()), "fixture",
            out=self.root / "export" / "capped.tgz",
            state_dir=self.root / "export" / "capped.state",
            limit=120,
        )
        self.assertIsNone(capped["repo_watermarks"]["DEL/huge"])

    # -- 4. NEWEST-first early termination ---------------------------------
    def test_pagination_stops_one_page_after_the_watermark(self):
        bundle = huge_bundle(total=300)  # 3 pages of 100
        first, transport = self.run_scrape(bundle)
        self.assertEqual(transport.kinds()["list"], 3)
        self.assertEqual(first["early_stopped"], [])  # no watermark yet

        # Page 1 holds the newest PR, which *is* the watermark (a tie, so it is
        # read through); page 2 is strictly below it -> stop. Page 3 is never
        # requested.
        second, transport = self.run_scrape(bundle)
        self.assertEqual(transport.kinds(), Counter({"list": 2}))
        self.assertEqual(second["early_stopped"], ["DEL/huge"])
        self.assertEqual(second["prs_seen"], 200)

    def test_a_full_page_landing_exactly_on_the_watermark_does_not_stop(self):
        # Two PRs share the newest date, and one of them closes page 1 exactly
        # on the watermark: the tie must not be treated as "already done".
        bundle = huge_bundle(
            total=300, matching=[290, 300],
            updated_minute=lambda pr_id: 300 if pr_id in (290, 300) else pr_id,
        )
        self.run_scrape(bundle)

        report, transport = self.run_scrape(bundle)
        self.assertEqual(transport.touched_prs(), {290, 300})  # both ties re-read
        self.assertEqual(report["prs_kept"], 2)
        self.assertEqual(transport.kinds()["list"], 2)  # page 2 is strictly below
        self.assertEqual(report["early_stopped"], ["DEL/huge"])

    def test_one_new_update_on_the_newest_page_costs_one_more_page(self):
        self.run_scrape(huge_bundle(total=300))
        # A brand-new ticketed PR at the top of the list: page 1 is no longer
        # below the watermark, so exactly one more page is read.
        fresh = huge_bundle(
            total=301, matching=[*MATCHING, 301],
            updated_minute=lambda pr_id: 10_000 if pr_id == 301 else pr_id,
        )
        report, transport = self.run_scrape(fresh)
        self.assertEqual(transport.kinds(), Counter({"list": 2, "commits": 1, "activities": 1}))
        self.assertEqual(transport.touched_prs(), {301})
        self.assertEqual(report["prs_kept"], 1)
        self.assertEqual(report["early_stopped"], ["DEL/huge"])

    def test_a_short_final_page_needs_no_early_stop(self):
        # 150 PRs: page 1 ties the watermark, page 2 is short and is the end of
        # the repo -- nothing to stop, and nothing left to read.
        bundle = huge_bundle(total=150)
        first, transport = self.run_scrape(bundle)
        self.assertEqual(transport.kinds()["list"], 2)
        self.assertEqual(first["early_stopped"], [])
        second, transport = self.run_scrape(bundle)
        self.assertEqual(transport.kinds(), Counter({"list": 2}))
        self.assertEqual(second["early_stopped"], [])

    def test_millisecond_updated_dates_do_not_read_as_moved(self):
        """DC's updatedDate is epoch ms; a seconds-truncated cursor would drift."""
        bundle = huge_bundle(total=2, matching=[1])
        for record in bundle["projects"][0]["repos"][0]["pull_requests"]:
            record["pull_request"]["updatedDate"] += 742
        self.run_scrape(bundle)
        report, transport = self.run_scrape(bundle)
        self.assertEqual(report["prs_kept"], 0)
        self.assertEqual(transport.touched_prs(), set())

    # -- 5. state format ---------------------------------------------------
    def test_state_files_carry_the_watermark_and_the_settled_set(self):
        self.run_scrape()

        watermark = json.loads((self.state_dir / "watermark.json").read_text())
        self.assertEqual(watermark["schema_version"], 2)
        source = watermark["sources"]["fixture"]
        self.assertIsNotNone(source["watermark_ts"])                     # event cursor
        self.assertIsNotNone(source["repos"]["DEL/huge"]["updated_ts"])  # PR cursor
        self.assertEqual(source["repos"]["DEL/huge"]["prs_kept"], len(MATCHING))

        prs = json.loads((self.state_dir / "prs.json").read_text())
        self.assertEqual(prs["schema_version"], 1)
        entries = prs["sources"]["fixture"]
        # only ticket-bearing PRs are tracked: the other 490 rows cost no state
        self.assertEqual(set(entries), {f"DEL/huge#{pr_id}" for pr_id in MATCHING})
        for pr_id in MATCHING:
            entry = entries[f"DEL/huge#{pr_id}"]
            self.assertEqual(entry["state"], TICKETED[pr_id])
            self.assertEqual(entry["done"], TICKETED[pr_id] != "OPEN")
            self.assertEqual(entry["ticket_keys"], [f"DEL-{pr_id}"])
            self.assertTrue(entry["updated_date"].endswith("Z"))


if __name__ == "__main__":
    unittest.main()

"""The constraint verdict and the exploit → subordinate → elevate ladder (prd/58).

Every view used to name a different bottleneck at a different magnitude, and the
Rules page recommended four levers at once — accurate, but not decisive. This
module answers with **one** sentence: the station with the largest queue owed
**now**, on **one** canonical clock, with the at-stake hours and the items
behind it. It then walks Goldratt's ladder so *elevate* (more capacity) is never
proposed while *exploit* options are still unrun.

The definitions are the ones ``docs/concepts.md`` §6 now carries:

* **station** — one of the six fixed stages of the story value stream
  (``refine`` · ``plan`` · ``start`` · ``develop`` · ``review`` · ``verify``).
  The stations *partition* the nine segments, so a segment belongs to exactly
  one station and the pools cannot double-count a wait.
* **removable pool** — a station's open wait an action can remove:
  ``live_hours`` (in-station age of open stories with a live PR) + ``tail_hours``
  (the completed stories' station hours above the station's own p85). It is not
  the station's total, and it is never added to ``history_hours``. The pool table
  keeps it for reference; the **verdict ranks ``live_hours`` alone** — what is
  owed *now* — because a historical tail is not a queue anyone can work.
* **constraint verdict** — the station with the largest *current* queue
  (``removable_now_hours = live_hours``), reported with its holder split, its
  ties (within 10 %), and its adjacent findings. A station with ``live == 0``
  cannot be the constraint: when every station is at zero the payload says so
  (``constraint: null``, a plain ``note`` and the top ``historical`` tails), so a
  window with no PRs never dresses history up as a live bottleneck.

**One clock per concept.** The station pools are **story-clock** business hours
measured from the same segment boundaries the ticket page draws (the assignee's
calendar). The review detail's holders are the **PR ball clock**
(``Interval.holder_hours``, each holder priced on their own calendar) and are
*never summed* with the story hours — the payload labels both. Response time
(the sentinel gate) is a third metric and is not used for the verdict, which is
why the card can name one constraint without the person-vs-role-vs-response
ambiguity.

Pure projection: no simulation runs here. The ladder's lab notes come from the
prd/43 advice report the server already builds for ``/api/insights``, so the rung
and the Policy page cannot quote two numbers.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from analysis import advice as advice_crosscheck
from analysis import aging, ballcourt, jira
from ingest.store import iso

#: prd/58 §Analysis: a window with fewer finished stories cannot have a verdict.
MIN_COMPLETED = 20
#: One waiting week (concepts.md §1: 5 d = 40 business hours). A pool below it
#: is not worth naming — the card says so instead of manufacturing a constraint.
FLOOR_HOURS = 5.0 * ballcourt.BUSINESS_HOURS_PER_DAY
#: Stations whose removable pools are within this share of the leader are tied.
TIE_SHARE = 0.10

#: The six stations, in value-stream order. ``segments`` partition the nine
#: segments; ``entries`` are the boundaries a story crosses when it enters the
#: station (the last one present is "the last transition into the station").
STATIONS = (
    {
        "id": "refine", "label": "Refinement", "short": "refinement",
        "segments": ("created_ready",),
        "entries": ("created",),
        "stage": "backlog", "clock": "story",
        "basis": "created → ready, on the assignee's calendar",
    },
    {
        "id": "plan", "label": "Sprint planning", "short": "planning",
        "segments": ("ready_sprint",),
        "entries": ("ready",),
        "stage": "ready", "clock": "story",
        "basis": "ready → sprint, on the assignee's calendar",
    },
    {
        "id": "start", "label": "Starting work", "short": "starting",
        "segments": ("sprint_in_progress",),
        "entries": ("sprint",),
        "stage": None, "clock": "story",
        "basis": "sprint → in progress, on the assignee's calendar",
    },
    {
        "id": "develop", "label": "Development", "short": "development",
        "segments": ("in_progress_pr_open",),
        "entries": ("started",),
        "stage": "in_progress", "clock": "story",
        "basis": "in progress → PR open, on the assignee's calendar",
    },
    {
        "id": "review", "label": "Approval gate", "short": "review",
        # PR open → first review is two store segments (the wait for a reviewer
        # and the first review itself); both are the one concepts.md §5 segment.
        "segments": (
            "pr_open_reviewer_added", "pr_open_first_review",
            "review_rounds", "final_approval_merge",
        ),
        "entries": ("in_review", "pr_open"),
        "stage": "in_review", "clock": "story",
        "basis": (
            "PR open → merge (waiting for a reviewer + review rounds + final "
            "approval → merge), on the assignee's calendar"
        ),
    },
    {
        "id": "verify", "label": "Verification", "short": "verification",
        "segments": ("merge_done", "merge_verification", "verification_done"),
        "entries": ("in_verification", "merged"),
        "stage": "in_verification", "clock": "story",
        "basis": "merge → verification → done, on the assignee's calendar",
    },
)

STATION_IDS = tuple(station["id"] for station in STATIONS)
STATION_BY_ID = {station["id"]: station for station in STATIONS}
SEGMENT_TO_STATION = {
    segment: station["id"]
    for station in STATIONS for segment in station["segments"]
}

#: The guard the PRD asks for: the stations partition *every* segment the store
#: can produce (including the two-way QA split), with no segment in two stations.
_ALL_SEGMENTS = set(jira.SEGMENT_KEYS) | {
    key for key, _label in jira.VERIFICATION_SEGMENTS
}
assert set(SEGMENT_TO_STATION) == _ALL_SEGMENTS, (
    "the six stations must partition the nine segments: "
    f"missing {sorted(_ALL_SEGMENTS - set(SEGMENT_TO_STATION))}, "
    f"unknown {sorted(set(SEGMENT_TO_STATION) - _ALL_SEGMENTS)}"
)
assert sum(len(station["segments"]) for station in STATIONS) == len(SEGMENT_TO_STATION), (
    "a segment appears in more than one station"
)

#: prd/58 §Data model: every ``templates()`` rule id gets exactly one rung.
#: ``measured`` rules are diagnostics (survival, sprint on-time, the worklog
#: cross-check) — they never carry a rung and the ladder never hides them.
RUNGS = {
    "exploit": (
        "sentinel-tail", "stale-prs", "review-sla", "review-tail", "round-cap",
        "jira-waiting-time", "comment-tax", "conformance-rounds",
        "conformance-unapproved",
    ),
    "subordinate": (
        "cycle-time", "pr-size", "flow-efficiency", "stacked-cascade",
        "rebase-waste", "vacation-blocking", "jira-carry-over",
        "jira-point-churn", "jira-conformance",
    ),
    "elevate": ("sentinel-queue", "reviewer-spread"),
    "measured": (
        "time-to-merge", "cycle-time-censoring", "sprint-on-time",
        "jira-worklog-mismatch", "jira-worklog-noise",
    ),
}
RUNG_ORDER = ("exploit", "subordinate", "elevate")
RUNG_LABEL = {"exploit": "Exploit", "subordinate": "Subordinate", "elevate": "Elevate"}

#: The rule families that are generated per person (never static ids). They keep
#: their static sibling's rung: a per-person responsiveness card is the same
#: lever as ``reviewer-spread`` (capacity), a vacation card the same policy as
#: ``vacation-blocking``.
RUNG_FAMILIES = (
    ("reviewer-", "elevate"),
    ("vacation-", "subordinate"),
)

#: Which lab preset bears on which rung (advice.POLICIES ids).
POLICY_RUNG = {
    "refine": "exploit",        # clean the inputs before adding capacity
    "sla4": "exploit",          # the review SLA is an exploit action
    "roundcap2": "exploit",     # a round cap is an exploit action
    "size3": "subordinate",
    "sentinel2": "elevate",
}

#: The exact honesty copy (prd/58 §Product design).
POOL_NOTE = (
    "Pool = business hours on the team calendar; story segments for refinement "
    "and verification, the PR ball clock for the review detail."
)
EXPLOIT_NOTE = (
    "The model does not see the ticket-level wait, so the exploit rung is the "
    "rules' claim, not the lab's."
)
NO_CONSTRAINT = "No constraint worth naming this week"
NO_QUEUE_HEAD = "No current queue is visible in this window: {owed:g} h owed now."
NO_QUEUE_TAIL = "this is history, not a live constraint."
NO_QUEUE_EMPTY = "No completed story left an abnormal tail in this window either."
#: How many historical tails the payload names (the note names the first two).
HISTORICAL_LIMIT = 3
HOLDER_NOTE = (
    "Holder hours are person-hours with the ball in review, priced on each "
    "holder's own calendar (the PR ball clock) — not response time, not "
    "calendar utilisation, and never added to the story-clock pool."
)


# --------------------------------------------------------------------------- #
# rule → rung
# --------------------------------------------------------------------------- #
def rung_for(rule_id: str) -> str | None:
    """The rung a rule id belongs to (``None`` for an unknown id)."""
    for rung, ids in RUNGS.items():
        if rule_id in ids:
            return rung
    for prefix, rung in RUNG_FAMILIES:
        if str(rule_id).startswith(prefix):
            return rung
    return None


def rung_map() -> dict:
    """The explicit client map: every static id → rung, plus the families."""
    by_rule = {rule_id: rung for rung, ids in RUNGS.items() for rule_id in ids}
    return {
        "by_rule": by_rule,
        "families": [{"prefix": prefix, "rung": rung} for prefix, rung in RUNG_FAMILIES],
        "order": list(RUNG_ORDER),
        "labels": dict(RUNG_LABEL),
        "measured": list(RUNGS["measured"]),
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _in_window(stamp: datetime | None, window) -> bool:
    if stamp is None:
        return False
    if window is None:
        return True
    return window[0] <= stamp <= window[1]


def _hours(value: float | None) -> float:
    return round(max(0.0, float(value or 0.0)), 1)


def station_at(boundaries: dict) -> tuple[str | None, datetime | None]:
    """The station a story is in, and when it last entered it.

    A story is in the **last** station whose entry boundary it crossed; the
    entry stamp is the latest of that station's entry boundaries (a story whose
    Jira status moved to ``in_review`` after its PR opened entered review at the
    status change — the number the PRD's ``144 h`` names).
    """
    chosen: tuple[str, datetime] | None = None
    for station in STATIONS:
        stamps = [
            boundaries.get(key) for key in station["entries"]
            if boundaries.get(key) is not None
        ]
        if stamps:
            chosen = (station["id"], max(stamps))
    return chosen or (None, None)


def _completed_sums(court, data, window) -> list[tuple[str, dict]]:
    """Per completed story, its hours per station (the union of the segments)."""
    if data is None:
        return []
    verification = jira.uses_verification(data)
    rows: list[tuple[str, dict]] = []
    for story in data.stories:
        cycle = jira._cycle(story)
        boundaries = cycle.boundaries
        created = boundaries.get("created")
        done = boundaries.get("done")
        if created is None or done is None or done <= created:
            continue
        if not _in_window(done, window):
            continue
        sums: dict[str, float] = defaultdict(float)
        for key in jira.segment_keys(boundaries, verification=verification):
            hours = jira.segment_hours(court, cycle, key)
            if hours is None:
                continue
            sums[SEGMENT_TO_STATION[key]] += hours
        rows.append((story.key, {key: round(value, 2) for key, value in sums.items()}))
    return rows


def liveness(court, data) -> list[dict]:
    """Every open story's liveness, station and in-station age (prd/59's classifier).

    ``dead`` and ``unobserved`` mean "no live PR" — neither is waiting on a
    nudge, so they are adjacent findings, never part of the removable pool.
    """
    if data is None:
        return []
    done_keys = jira.done_keys(data)
    rows: list[dict] = []
    for story in data.stories:
        cycle = jira._cycle(story)
        open_prs = [
            state.pr for state in story.prs
            if state.pr.get("closed_ts") is None
        ]
        # A finished story is not in flight — unless its code is not: "Done in
        # Jira, PR open" is the `unmerged` state (prd/42 M4), and it belongs in
        # the adjacent findings, not in the removable pool.
        if cycle.boundaries.get("done") is not None and not open_prs:
            continue
        status = aging.liveness(court, story, done=story.key in done_keys)
        station, entered = station_at(cycle.boundaries)
        hours = (
            round(court.business_hours(story.actor, entered, court.snapshot), 2)
            if entered is not None and entered <= court.snapshot else None
        )
        rows.append({
            "key": story.key,
            "object_id": story.object_id,
            "state": status["state"],
            "reason": status["reason"],
            "last_human": status["last_human"],
            "station": station,
            "entered": entered,
            "hours": hours,
            "open_prs": [pr["object_id"] for pr in open_prs],
            "href": f"#ticket/{story.key}",
        })
    return rows


# --------------------------------------------------------------------------- #
# the pools and the verdict
# --------------------------------------------------------------------------- #
def pools(court, data, window=None) -> dict:
    """Every station's history, tail and live wait (the ranked pool table)."""
    completed = _completed_sums(court, data, window)
    open_rows = liveness(court, data)
    live_by_station: dict[str, list[dict]] = defaultdict(list)
    for row in open_rows:
        if row["state"] == "live" and row["station"] and row["hours"] is not None:
            live_by_station[row["station"]].append(row)
    out: dict[str, dict] = {}
    for station in STATIONS:
        sid = station["id"]
        values = [sums[sid] for _key, sums in completed if sid in sums]
        p50 = aging.percentile(values, 0.50)
        p85 = aging.percentile(values, 0.85)
        p95 = aging.percentile(values, 0.95)
        tail = (
            round(sum(max(0.0, value - p85) for value in values), 2)
            if p85 is not None else 0.0
        )
        live_rows = live_by_station.get(sid, [])
        live = round(sum(float(row["hours"] or 0.0) for row in live_rows), 2)
        out[sid] = {
            "station": sid,
            "label": station["label"],
            "wait_label": station["short"],
            "clock": station["clock"],
            "basis": station["basis"],
            "segments": list(station["segments"]),
            "history_hours": round(sum(values), 1),
            "n_completed": len(values),
            "p50_hours": p50, "p85_hours": p85, "p95_hours": p95,
            "tail_hours": tail,
            "live_hours": live,
            "open_n": len(live_rows),
            "removable_hours": round(live + tail, 2),
            # the verdict's number: what is owed *now*. The tail above is a
            # historical fact and never a queue (prd/67 W-04).
            "removable_now_hours": live,
            "items": [
                {"key": row["key"], "hours": row["hours"], "href": row["href"]}
                for row in sorted(live_rows, key=lambda item: -float(item["hours"] or 0))
            ],
        }
    return out


def _now_hours(pool: dict) -> float:
    """A station's queue owed now — never its historical tail."""
    return round(max(0.0, float(
        pool.get("removable_now_hours", pool.get("live_hours", 0.0)) or 0.0
    )), 2)


def rank_pools(pool_table: dict) -> list[dict]:
    """Stations by queue owed **now**, then by the reference removable pool.

    The verdict is live-first (prd/67 W-04): a station with no live PR cannot be
    the constraint. ``removable_hours`` (live + tail) is the second key only so
    the reference table is ordered sensibly among stations that owe nothing now.
    """
    order = {sid: index for index, sid in enumerate(STATION_IDS)}
    return sorted(
        pool_table.values(),
        key=lambda pool: (
            -_now_hours(pool),
            -round(float(pool.get("removable_hours") or 0.0), 2),
            order[pool["station"]],
        ),
    )


def historical(pool_table: dict, limit: int = HISTORICAL_LIMIT) -> list[dict]:
    """The largest completed-story tails, largest first (history, not a queue)."""
    order = {sid: index for index, sid in enumerate(STATION_IDS)}
    rows = [
        pool for pool in pool_table.values()
        if float(pool.get("tail_hours") or 0.0) > 0
    ]
    rows.sort(key=lambda pool: (
        -round(float(pool.get("tail_hours") or 0.0), 2),
        order[pool["station"]],
    ))
    return [
        {
            "station": pool["station"],
            "label": pool["label"],
            "tail_hours": round(float(pool.get("tail_hours") or 0.0), 2),
        }
        for pool in rows[:max(0, int(limit))]
    ]


def no_queue_note(pool_table: dict) -> str:
    """The plain sentence for a window in which no station owes work now.

    A zero-owed historical tail is a fact, not a constraint: name the largest
    tails as history and say so, instead of manufacturing a station (prd/67 W-04).
    """
    owed = round(sum(_now_hours(pool) for pool in pool_table.values()), 2)
    head = NO_QUEUE_HEAD.format(owed=owed)
    tails = historical(pool_table, limit=2)
    if not tails:
        return f"{head} {NO_QUEUE_EMPTY}"
    named = " and ".join(
        f"{row['label']} ({row['tail_hours']:g} h)" for row in tails
    )
    noun = "tails are" if len(tails) > 1 else "tail is"
    return f"{head} The largest historical {noun} {named}; {NO_QUEUE_TAIL}"


def constraint(pool_table: dict) -> dict | None:
    """The largest current queue, with its ties (within 10 %) named.

    Ranks by what is owed **now** (``removable_now_hours`` = ``live_hours``).
    A station with ``live_hours == 0`` can never be the constraint, however
    large its historical tail (prd/67 W-04); when every station is at zero this
    returns ``None`` and the caller states the no-current-queue note instead.
    """
    ranked = rank_pools(pool_table)
    if not ranked or _now_hours(ranked[0]) <= 0:
        return None
    top = ranked[0]
    top_now = _now_hours(top)
    tied = [
        {
            "station": pool["station"],
            "label": pool["label"],
            "removable_hours": pool.get("removable_hours", 0.0),
            "removable_now_hours": _now_hours(pool),
        }
        for pool in ranked[1:]
        if _now_hours(pool) > 0
        and _now_hours(pool) >= top_now * (1.0 - TIE_SHARE)
    ]
    return {**top, "tied_with": tied, "hours_kind": "story"}


# --------------------------------------------------------------------------- #
# holders (the review detail, on the PR ball clock)
# --------------------------------------------------------------------------- #
def _ticket_of(pr: dict) -> str | None:
    keys = [key for key in (pr.get("ticket_keys") or []) if key]
    return keys[-1] if keys else None


def holders(court, delivery_rows=()) -> dict:
    """The review-phase holder split, on the PR ball clock.

    Only **observed** review intervals count (``phase == "review"``): the
    unobserved tail's holder is inferred, so it is reported in ``current`` (the
    delivery list's own number) and never mixed into the person totals. Bots are
    excluded outright (concepts.md §2).
    """
    bots = set(court.bots or ())
    acc: dict[str, dict] = {}
    for state in court.prs:
        pr = state.pr
        author = pr.get("actor")
        for interval in state.intervals:
            observed_review = interval.ball == "review" and interval.phase == "review"
            for person, hours in (interval.holder_hours or {}).items():
                # a role label is not a person (concepts.md §2): never report
                # "review" as a holder beside the people it summarises.
                if not person or person in bots or person in ballcourt.SYNTHETIC:
                    continue
                entry = acc.setdefault(person, {
                    "person": person, "hours": 0.0, "all_phase_hours": 0.0,
                    "_sentinel": False, "_author": False,
                })
                entry["all_phase_hours"] = round(entry["all_phase_hours"] + hours, 2)
                if observed_review:
                    entry["hours"] = round(entry["hours"] + hours, 2)
                    if person in (court.sentinels or ()):
                        entry["_sentinel"] = True
                    elif person == author:
                        entry["_author"] = True
    people = []
    for entry in sorted(
        (item for item in acc.values() if item["all_phase_hours"] > 0),
        key=lambda item: (-item["hours"], item["person"]),
    ):
        role = "sentinel" if entry["_sentinel"] else (
            "author" if entry["_author"] else "reviewer"
        )
        people.append({
            "person": entry["person"],
            "role": role,
            "hours": entry["hours"],
            "all_phase_hours": entry["all_phase_hours"],
            "items": [],
        })
    by_person = {entry["person"]: entry for entry in people}
    for row in delivery_rows:
        holders = [person for person in (row.get("waiting_on") or ())]
        # "items" is the work this person holds *alone* — the actionable tail,
        # not every PR they are one of several reviewers on.
        if len(holders) != 1:
            continue
        entry = by_person.get(holders[0])
        if entry is not None:
            entry["items"].append(f"#{row['number']}")
    current = []
    for row in sorted(delivery_rows, key=lambda item: -float(item["waiting_hours"] or 0)):
        current.append({
            "pr": f"#{row['number']}",
            "ticket": _ticket_of({
                "ticket_keys": row.get("story_tickets") or row.get("tickets") or [],
            }),
            "hours": _hours(row.get("waiting_hours")),
            "holders": list(row.get("waiting_on") or ()),
            "ball": row.get("ball"),
            "stale": bool(row.get("stale")),
        })
    sentinel_hours = [
        entry["hours"] for entry in people
        if entry["role"] == "sentinel" and entry["hours"] > 0
    ]
    peer_hours = [
        entry["hours"] for entry in people
        if entry["role"] != "sentinel" and entry["hours"] > 0
    ]
    return {
        "clock": "pr-holder",
        "people": people,
        "current": current,
        "sentinel_hours": sentinel_hours,
        "peer_range": (
            [round(min(peer_hours), 1), round(max(peer_hours), 1)]
            if peer_hours else None
        ),
        "note": HOLDER_NOTE,
        "available": bool(people) and any(entry["hours"] > 0 for entry in people),
        "basis": (
            "observed review intervals (ball = review, phase = review), each "
            "holder priced on their own calendar; the unobserved tail is listed "
            "under adjacent findings, never in the person totals"
        ),
    }


# --------------------------------------------------------------------------- #
# exploit items and adjacent findings
# --------------------------------------------------------------------------- #
def orphaned_approvals(delivery_rows, court) -> list[dict]:
    """Open PRs with an approval whose only remaining holder is the sentinel.

    The same predicate the Overview card and the Rules sentinel-tail rule use
    (concepts.md §9 "Chase action"): a peer still pending means the sentinel's
    gate has not opened, and a PR whose author owes a fix is not an orphan.
    """
    bots = set(court.bots or ())
    out = []
    for row in delivery_rows:
        if row.get("ball") == "ready":
            continue
        if int(row.get("reviewers_approved") or 0) < 1:
            continue
        roles = row.get("waiting_roles") or {}
        waiting = [
            person for person in (row.get("waiting_on") or ())
            if person != row.get("author") and person not in bots
        ]
        if not waiting:
            continue
        if all(str(roles.get(person) or "").lower() == "sentinel" for person in waiting):
            out.append({
                "pr": f"#{row['number']}",
                "ticket": _ticket_of({"ticket_keys": row.get("story_tickets") or []}),
                "hours": _hours(row.get("waiting_hours")),
                "holders": waiting,
                "stale": bool(row.get("stale")),
            })
    return sorted(out, key=lambda item: -item["hours"])


def exploit_items(delivery_rows, orphans, limit: int = 3) -> list[dict]:
    """The handful of owned items the exploit rung sends the lead to chase.

    The largest current review waits (the delivery list's own holder fact), with
    the orphaned approvals flagged. Ranked by the wait, so the first three are
    the three items the verdict's sentence names.
    """
    orphan_prs = {item["pr"] for item in orphans}
    review_rows = [
        row for row in delivery_rows
        if row.get("ball") in ("review", "sentinel")
    ]
    review_rows.sort(key=lambda row: -float(row.get("waiting_hours") or 0))
    out = []
    for row in review_rows[:limit]:
        pr = f"#{row['number']}"
        out.append({
            "kind": "pr",
            "pr": pr,
            "ticket": _ticket_of({"ticket_keys": row.get("story_tickets") or []}),
            "hours": _hours(row.get("waiting_hours")),
            "holders": list(row.get("waiting_on") or ()),
            "orphaned": pr in orphan_prs,
            "stale": bool(row.get("stale")),
            "href": (
                f"#ticket/{_ticket_of({'ticket_keys': row.get('story_tickets') or []})}"
                if row.get("story_tickets") else None
            ),
        })
    return out


def _station_short(rows: list[dict]) -> str:
    """The station word for a sentence ("sat in review …")."""
    station = rows[0].get("station") if rows else None
    return STATION_BY_ID[station]["short"] if station else "the value stream"


def adjacent(court, data, open_rows, delivery_rows, done_keys) -> list[dict]:
    """The findings beside the verdict: no-live-PR work and unmerged changes."""
    no_live = [
        row for row in open_rows
        if row["state"] in ("dead", "unobserved") and not row["open_prs"]
    ]
    no_live.sort(key=lambda row: (-float(row["hours"] or 0), row["key"]))
    no_live_hours = round(sum(float(row["hours"] or 0.0) for row in no_live), 1)
    out = [{
        "kind": "dead",
        "n": len(no_live),
        "hours": no_live_hours,
        "hours_kind": "story",
        "clock": "story",
        "sentence": (
            f"{len(no_live)} stories ({no_live_hours:g} h) sat in "
            f"{_station_short(no_live)} with no live PR — close or re-plan them; "
            "that is queue cleaning, not capacity."
        ),
        "items": [
            {
                "key": row["key"], "hours": row["hours"],
                "state": row["state"], "href": row["href"],
            }
            for row in no_live
        ],
    }]
    unmerged = []
    for state in court.prs:
        pr = state.pr
        if pr.get("closed_ts") is not None:
            continue
        tickets = [key for key in (pr.get("ticket_keys") or []) if key in done_keys]
        if not tickets:
            continue
        unmerged.append({
            "pr": f"#{pr['pr_id']}",
            "key": tickets[-1],
            "hours": aging.review_hours(state),
            "href": f"#ticket/{tickets[-1]}",
        })
    unmerged.sort(key=lambda item: -item["hours"])
    total = round(sum(float(item["hours"] or 0.0) for item in unmerged), 1)
    out.append({
        "kind": "unmerged",
        "n": len(unmerged),
        "hours": total,
        "hours_kind": "pr-holder",
        "clock": "pr-holder",
        "sentence": (
            f"{len(unmerged)} PR{'s are' if len(unmerged) != 1 else ' is'} still "
            "open although Jira says the ticket is Done — the change is unmerged; "
            "merging it is the next action, not new capacity."
        ),
        "items": unmerged,
    })
    return out


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #
def _rule_entry(suggestion: dict) -> dict:
    lab = suggestion.get("policy_lab") or {}
    return {
        "id": suggestion["id"],
        "title": suggestion["title"],
        "metric": suggestion.get("metric"),
        "severity": suggestion.get("severity"),
        "impact_hours": suggestion.get("impact_hours"),
        "rank": suggestion.get("rank"),
        "lab": {
            "available": bool(lab.get("note")),
            "policy": lab.get("policy"),
            "label": lab.get("label"),
            "verdict": (
                "no clear effect" if lab.get("within_noise")
                else ("not a measured win" if lab.get("contradiction")
                      else ("supported" if lab.get("supports") else None))
            ),
            "within_noise": bool(lab.get("within_noise")),
            "supports": bool(lab.get("supports")),
            "contradiction": bool(lab.get("contradiction")),
            "note": lab.get("note") or (
                "the lab has no verdict for this policy in this window"
            ),
        },
    }


def _rung_labs(cross_check: dict | None) -> dict[str, list[dict]]:
    """The lab's preset verdicts by rung (even for a policy whose rule is quiet)."""
    out: dict[str, list[dict]] = defaultdict(list)
    if not cross_check or not cross_check.get("available"):
        return out
    for policy in advice_crosscheck.POLICIES:
        rung = POLICY_RUNG.get(policy["id"])
        if rung is None:
            continue
        entry = next(
            (item for item in cross_check.get("policies") or ()
             if item.get("id") == policy["id"]),
            None,
        )
        if entry is None:
            continue
        out[rung].append({
            "id": entry["id"],
            "label": entry["label"],
            "available": bool(entry.get("available")),
            "cycle_percent": entry.get("cycle_percent"),
            "within_noise": bool(entry.get("within_noise")),
            "contradiction": bool(entry.get("contradiction")),
            "supports": bool(entry.get("supports")),
            "verdict": (
                "no clear effect" if entry.get("within_noise")
                else ("not a measured win" if entry.get("cycle_better") is False
                      else ("supported" if entry.get("supports") else None))
            ),
            "note": entry.get("note"),
        })
    return out


def ladder(suggestions, cross_check, exploit_n: int) -> list[dict]:
    """Exploit → subordinate → elevate, with the lab's verdict on every rung.

    ``elevate`` is ``blocked`` while ``exploit`` is open: the card may not
    suggest "add a sentinel" while a chaseable item is still owed. The blocked
    rung stays readable (its rules and reason stay in the payload) — a blocked
    instruction is a decision, not a hidden row.
    """
    by_rung: dict[str, list[dict]] = defaultdict(list)
    for suggestion in suggestions or ():
        rung = rung_for(suggestion.get("id"))
        if rung in RUNG_ORDER:
            by_rung[rung].append(_rule_entry(suggestion))
    labs = _rung_labs(cross_check)
    exploit_open = bool(by_rung["exploit"]) or exploit_n > 0
    out = []
    for rung in RUNG_ORDER:
        rules = sorted(
            by_rung[rung],
            key=lambda rule: (-(rule.get("impact_hours") or 0), rule["id"]),
        )
        state = "open" if (
            rules or (rung == "exploit" and exploit_n > 0)
        ) else "done"
        reason = None
        if rung == "elevate" and exploit_open:
            state = "blocked"
            reason = (
                f"exploit first: {exploit_n} "
                f"item{'s are' if exploit_n != 1 else ' is'} still owed"
                if exploit_n else
                f"exploit first: {len(by_rung['exploit'])} rule"
                f"{'' if len(by_rung['exploit']) == 1 else 's'} on the exploit rung"
            )
        run_labs = labs.get(rung) or []
        note = "; ".join(
            f"{lab['label']}: {lab['note']}" for lab in run_labs if lab.get("note")
        ) or (
            "the lab has no verdict for this policy in this window"
            if rung != "exploit" else EXPLOIT_NOTE
        )
        out.append({
            "step": rung,
            "label": RUNG_LABEL[rung],
            "state": state,
            "reason": reason,
            "rules": rules,
            "labs": run_labs,
            "note": note,
            "claims": "rules" if rung == "exploit" else "lab",
        })
    return out


# --------------------------------------------------------------------------- #
# the payload
# --------------------------------------------------------------------------- #
def verdict(court, data, window, suggestions=None, cross_check=None) -> dict:
    """``GET /api/verdict``: one constraint, the ladder, and the adjacent facts.

    ``available: false`` with a stated reason (never a zero-filled card) when the
    window has fewer than :data:`MIN_COMPLETED` finished stories or the largest
    removable pool (live + tail) is under one waiting week. A window with
    completed work but no live queue (e.g. a Jira-only store with no PRs) is
    **available** with ``constraint: null``, a plain ``note`` and the top
    ``historical`` tails — the data is fine, there is simply nothing owed now
    (prd/67 W-04). The full ranking still travels in ``pools`` so the disclosure
    can show it.
    """
    pool_table = pools(court, data, window)
    constraint_block = constraint(pool_table)
    historical_rows = historical(pool_table)
    completed_n = max((pool["n_completed"] for pool in pool_table.values()), default=0)
    done_keys = jira.done_keys(data)
    delivery = ballcourt.delivery_list(court, done_tickets=done_keys)
    delivery_rows = list(delivery.get("rows") or ())
    open_rows = liveness(court, data)
    distinct_done_open = {
        key for row in delivery_rows
        for key in (row.get("story_tickets") or ())
        if key in done_keys
    }
    orphans = orphaned_approvals(delivery_rows, court)
    items = exploit_items(delivery_rows, orphans)
    # Attach the live PR to each pooled story, so the card can name the item
    # ("#60 (DEL-176) has been in review 144 h, waiting on FrMo") without a
    # second lookup in the browser.
    pr_by_ticket: dict[str, dict] = {}
    for row in delivery_rows:
        for key in row.get("story_tickets") or ():
            pr_by_ticket.setdefault(key, row)
    if constraint_block is not None:
        for item in constraint_block.get("items") or ():
            row = pr_by_ticket.get(item["key"])
            if row is None:
                continue
            item["pr"] = f"#{row['number']}"
            item["holders"] = list(row.get("waiting_on") or ())
            item["orphaned"] = any(
                orphan["pr"] == item["pr"] for orphan in orphans
            )
    holder_block = holders(court, delivery_rows)
    findings = adjacent(court, data, open_rows, delivery_rows, done_keys)
    rungs = ladder(suggestions or (), cross_check, len(items))
    # the exploit rung names the items it sends the lead to chase (prd/58 DoD:
    # "#53/#55/#60"), so the card's ladder and the sentence cannot disagree
    for rung in rungs:
        if rung["step"] == "exploit":
            rung["items"] = list(items)
            rung["orphans"] = list(orphans)

    available = True
    note = POOL_NOTE
    if completed_n < MIN_COMPLETED:
        available = False
        note = (
            f"no verdict: the window has {completed_n} completed "
            f"storie{'s' if completed_n != 1 else 'y'} (needs {MIN_COMPLETED})"
        )
    elif constraint_block is None:
        # The data is fine; nothing is owed now. History is named as history
        # (prd/67 W-04), never dressed up as a constraint.
        available = True
        note = no_queue_note(pool_table)
    elif constraint_block["removable_hours"] < FLOOR_HOURS:
        available = False
        biggest = constraint_block["removable_hours"]
        note = (
            f"{NO_CONSTRAINT}: the largest removable pool is {biggest:g} h, "
            f"under one waiting week ({FLOOR_HOURS:g} h). The full ranking is "
            "below."
        )
    start, end = window if window else (court.snapshot, court.snapshot)
    days = round(max(0.0, (end - start).total_seconds()) / 86400.0, 1)
    return {
        "ok": True,
        "as_of": iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "calendar_id": court.calendar_id,
        "window": {"start": iso(start), "end": iso(end), "days": days},
        "constraint": constraint_block,
        "historical": historical_rows,
        "pools": rank_pools(pool_table),
        "holders": holder_block,
        "adjacent": findings,
        "exploit": {
            "items": items,
            "orphans": orphans,
            "n": len(items),
            "hours": round(sum(item["hours"] for item in items), 1),
            "note": EXPLOIT_NOTE,
        },
        "ladder": rungs,
        "next": [] if constraint_block is None else [
            {
                "station": pool["station"],
                "label": pool["label"],
                "removable_hours": pool["removable_hours"],
                "removable_now_hours": pool["removable_now_hours"],
            }
            for pool in rank_pools(pool_table)
            if pool["removable_hours"] > 0
            and pool["station"] != constraint_block["station"]
        ],
        "liveness": {
            "open_prs": len(delivery_rows),
            "live": len([row for row in open_rows if row["state"] == "live"]),
            "dead": len([row for row in open_rows if row["state"] == "dead"]),
            "unobserved": len([row for row in open_rows if row["state"] == "unobserved"]),
            "unmerged": len(distinct_done_open),
            "open": len(open_rows),
        },
        "rule_rungs": rung_map(),
        "current_rung": next(
            (rung["step"] for rung in rungs
             if rung["state"] in ("open", "blocked")),
            None,
        ),
        "available": available,
        "note": note,
        "pool_note": POOL_NOTE,
        "holder_note": HOLDER_NOTE,
        "basis": (
            "one canonical clock: station pools are business hours on the story "
            "clock (the segment boundaries the ticket page draws, assignee's "
            "calendar); the review detail's holders use the PR ball clock. "
            "The verdict ranks the queue owed now (live hours); removable = live "
            "hours + the completed stories' hours above the station's own p85 is "
            "the reference pool, and the tail is history, not a live constraint."
        ),
    }

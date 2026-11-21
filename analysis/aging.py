"""Aging WIP (prd/59): in-flight age against the team's own finished work.

The product's staleness signals were fixed constants (a stale PR is older than
5 business days, the sentinel tail is 24 h) and nothing measured how old the
*open* items are relative to how long this team actually takes. This module is
that forward-looking half of flow:

* **one cohort per clock** — the same durations the window metrics already use,
  read as percentiles (``p50`` / ``p85`` / ``p95``; ``p75`` for time in review),
  with the cohort's ``n`` travelling next to every threshold;
* **one clock per concept** (``docs/concepts.md`` §1, §6) — a story ages on the
  story clock (``created → snapshot`` or ``entered_stage → snapshot``), a PR on
  the review clock (the review phase while the ball was with reviewers, plus the
  unobserved tail) or its cycle (``created → snapshot``). The two are never
  summed;
* **a self-calibrated threshold** — the p85 of the *same cohort*, never an
  invented service level: ``p85 = 22.7 h (n = 106 merged PRs)`` describes this
  team, it does not commit it to anything;
* **bands** — ``normal`` (< p50) · ``watch`` (p50–p85) · ``aging`` (p85–p95) ·
  ``very old`` (≥ p95) · ``insufficient history`` (< :data:`MIN_N` samples);
* **hours at risk** — ``age − p85`` (≥ 0), the removable wait the chase action
  carries, and the ranked ``chase_order`` (live rows whose band is aging/very
  old) that prd/54's card and prd/58's exploit rung read instead of typing the
  three items by hand.

Percentiles are **nearest-rank on the sorted cohort**, index
``round(q × (n − 1))`` (the formula ``ballcourt._p90`` uses), so a tick equals a
manual recompute exactly. ``rules._percentile`` uses the neighbouring
``ceil(q·n) − 1``; the two agree except at ``p85`` on a 106-item cohort, where
the rounding formula gives the 22.7 h the PRD names. Stated once, here.

Nothing here writes: it is a pure projection of ``BallCourt`` + ``JiraData``.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timedelta

from ingest.store import iso
from analysis import ballcourt, jira

#: prd/59 §Data model: below this many finished items a percentile is noise.
MIN_N = 20
#: The one band vocabulary (prd/59 §Band vocabulary) — "stale" is the prd/34
#: rule with its own threshold and is deliberately not one of these.
BANDS = ("normal", "watch", "aging", "very_old", "insufficient")
BAND_LABEL = {
    "normal": "normal",
    "watch": "watch",
    "aging": "aging",
    "very_old": "very old",
    "insufficient": "insufficient history",
}
KINDS = ("story", "pr")
REVIEW = "review"
CYCLE = "cycle"
STORY_CYCLE = "story_cycle"
REVIEW_TIME = "review_time"
PR_CYCLE = "pr_cycle"
#: The workflow stages the stage selector can offer (``docs/concepts.md`` §4).
#: ``done`` is not an in-flight stage; ``backlog``/``ready`` are included when
#: the store measured them, and a stage the store never completed is absent.
STAGE_VIEWS = ("backlog", "ready", "in_progress", "in_review", "in_verification")
#: prd/59 §Edge cases: no open PR **and** no human event for this many business
#: days is abandoned work ("dead"), which the chase list lists separately
#: ("no live PR — close or re-plan") and never chases.
DEAD_BUSINESS_DAYS = 10.0
DEAD_HOURS = DEAD_BUSINESS_DAYS * ballcourt.BUSINESS_HOURS_PER_DAY
#: prd/59 §How it replaces the constants: the fixed fallbacks the rules keep
#: when a cohort is too small (or ``MEGASPEED_AGING_THRESHOLDS=fixed``).
FIXED_STALE_HOURS = ballcourt.STALE_BUSINESS_DAYS * ballcourt.BUSINESS_HOURS_PER_DAY
FIXED_SENTINEL_TAIL_HOURS = 24.0
FIXED_MODE_ENV = "MEGASPEED_AGING_THRESHOLDS"
LIVE_STATES = ("live", "unmerged")

BASIS_STORY_CYCLE = "completed stories' created → done, assignee's calendar"
BASIS_REVIEW = (
    "hours with the ball in review over merged PRs (phase review, role-level "
    "once, team calendar)"
)
BASIS_PR_CYCLE = "merged PRs' created → merged, author's calendar"
BASIS_STAGE = (
    "completed stories' hours in the same workflow stage (entered → left), "
    "assignee's calendar"
)
NOTE = (
    "An age is a censored duration — the item has not finished yet; compare it "
    "with finished work, never add it to a cycle-time median. Ages are business "
    "hours on the holder's calendar; a person on vacation accrues none. "
    "Calendar days in brackets."
)


# --------------------------------------------------------------------------- #
# quantiles and cohort blocks
# --------------------------------------------------------------------------- #
def percentile(values, q: float, digits: int = 2) -> float | None:
    """Nearest-rank percentile on the sorted cohort, index ``round(q·(n−1))``.

    The same formula as ``ballcourt._p90`` (and the PRD's own ticks): a
    hand-sorted recompute with this index equals the reported number exactly.
    """
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return round(ordered[index], digits)


def quantiles(values, qs=(0.50, 0.85, 0.95)) -> dict:
    """``{"p50": …, "p85": …, "p95": …}`` for one cohort."""
    return {f"p{int(round(q * 100))}": percentile(values, q) for q in qs}


def _entry(id: str, label: str, values, basis: str, *, p75: bool = False,
           source: str = "self-calibrated") -> dict:
    """One threshold chip: percentiles, the cohort's ``n``, and its source."""
    cohort = [float(value) for value in values if value is not None]
    qs = (0.50, 0.75, 0.85, 0.95) if p75 else (0.50, 0.85, 0.95)
    block = quantiles(cohort, qs)
    available = len(cohort) >= MIN_N
    block.update({
        "id": id,
        "label": label,
        "unit": "hours",
        # A suppressed percentile is *not* a number the view may draw: the
        # band is `insufficient` and the chip says why (prd/59 §Honesty).
        "p50": block.get("p50") if available else None,
        "p85": block.get("p85") if available else None,
        "p95": block.get("p95") if available else None,
        "n": len(cohort),
        "min_n": MIN_N,
        "available": available,
        "source": source,
        "basis": basis,
        "note": (
            None if available else
            f"insufficient history: {len(cohort)} finished item"
            f"{'' if len(cohort) == 1 else 's'} (needs {MIN_N}) — no percentile shown"
        ),
    })
    if p75:
        block["p75"] = block.get("p75") if available else None
    else:
        block.pop("p75", None)
    return block


def _in_window(stamp: datetime | None, window: tuple[datetime, datetime] | None) -> bool:
    if stamp is None:
        return False
    if window is None:
        return True
    return window[0] <= stamp <= window[1]


# --------------------------------------------------------------------------- #
# the cohorts (the same durations the window metrics report)
# --------------------------------------------------------------------------- #
def review_hours(state) -> float:
    """One PR's review wait: the review phase while the ball is with reviewers.

    ``phase == "review" and ball == "review"`` — the PRD's own clock. The phase
    says no commit landed inside the span, the ball says a reviewer still owed a
    verdict, so this is review time, not author work. A ``ready`` span is
    excluded: all reviewers answered and the merge click is the author's, so it
    is nobody's review wait (concepts.md §2). The ``unobserved`` tail counts
    while the ball is still with review (prd/55 §7: a 3–5-week wait with no
    recent event is still a wait). One sample per PR, never summed per reviewer,
    so an in-flight PR's age and the merged-PR cohort are the same quantity.
    """
    total = 0.0
    for interval in state.intervals:
        if interval.ball != REVIEW:
            continue
        if interval.phase == REVIEW or not interval.measured:
            total += interval.hours
    return round(total, 2)


def first_review_start(state) -> datetime | None:
    """When the review clock opened (the first interval with the ball in review)."""
    for interval in state.intervals:
        if interval.ball == REVIEW:
            return interval.start
    return None


def review_cohort(court: ballcourt.BallCourt,
                  window: tuple[datetime, datetime] | None = None) -> list[tuple[dict, float]]:
    """``[(pr, review_hours)]`` for merged PRs merged inside the window."""
    out: list[tuple[dict, float]] = []
    for state in court.prs:
        pr = state.pr
        if pr.get("state") != "merged" or pr.get("closed_ts") is None:
            continue
        if not _in_window(pr["closed_ts"], window):
            continue
        out.append((pr, review_hours(state)))
    return out


def pr_cycle_cohort(court: ballcourt.BallCourt,
                    window: tuple[datetime, datetime] | None = None) -> list[float]:
    """Merged PRs' ``created → merged`` business hours (author's calendar)."""
    values: list[float] = []
    for state in court.prs:
        pr = state.pr
        if pr.get("state") != "merged" or pr.get("closed_ts") is None:
            continue
        if not _in_window(pr["closed_ts"], window):
            continue
        opened, closed = pr.get("created_ts"), pr.get("closed_ts")
        if opened is None or closed is None or closed <= opened:
            continue
        values.append(round(court.business_hours(pr["actor"], opened, closed), 2))
    return values


def story_cycle_cohort(court: ballcourt.BallCourt, data: jira.JiraData | None,
                       window: tuple[datetime, datetime] | None = None) -> list[dict]:
    """Completed stories' cycle hours, one row per story (prd/57's cohort)."""
    if data is None:
        return []
    rows: list[dict] = []
    for story in data.stories:
        cycle = jira._cycle(story)
        begin = cycle.boundaries.get("created")
        done = cycle.boundaries.get("done")
        if begin is None or done is None or done <= begin:
            continue
        if not _in_window(done, window):
            continue
        rows.append({
            "key": story.key,
            "hours": round(court.business_hours(story.actor, begin, done), 2),
            "done": done,
        })
    return rows


def stage_events(story) -> list[tuple[datetime, str]]:
    """``[(ts, stage)]`` for a story's ``status_changed`` history, ordered."""
    rows = []
    for event in story.of("status_changed"):
        stage = jira.normalize_stage((event.get("attributes") or {}).get("to_status"))
        if stage:
            rows.append((event["_ts"], stage))
    return sorted(rows, key=lambda row: row[0])


def stage_clock(story) -> tuple[str | None, datetime | None]:
    """The story's current workflow stage and when it entered it (last time)."""
    events = stage_events(story)
    if not events:
        return None, None
    return events[-1][1], events[-1][0]


def stage_cohort(court: ballcourt.BallCourt, data: jira.JiraData | None,
                 window: tuple[datetime, datetime] | None = None) -> dict[str, list[float]]:
    """``stage -> [hours]``: one sample per completed story per stage it visited.

    A stage's duration is the business hours from entering it to leaving it
    (the next status change, or the story's done boundary), priced on the
    assignee's calendar — the same stage vocabulary (``docs/concepts.md`` §4)
    and the same clock the story clock uses. A story that never completed a
    stage contributes no sample, so a workflow without QA has no
    ``in_verification`` cohort and the stage is absent from the selector
    (prd/59 §Edge cases).
    """
    if data is None:
        return {}
    out: dict[str, list[float]] = defaultdict(list)
    for story in data.stories:
        cycle = jira._cycle(story)
        done = cycle.boundaries.get("done")
        if done is None:
            continue
        if not _in_window(done, window):
            continue
        events = stage_events(story)
        if not events:
            continue
        for index, (stamp, stage) in enumerate(events):
            end = events[index + 1][0] if index + 1 < len(events) else done
            if end is None or end <= stamp:
                continue
            out[stage].append(round(court.business_hours(story.actor, stamp, end), 2))
    return dict(out)


# --------------------------------------------------------------------------- #
# thresholds (one helper per clock, the same bases everywhere)
# --------------------------------------------------------------------------- #
def _review_source(values) -> str:
    """``p85 of 106 merged PRs (22.7 h)`` — the source a rule quotes."""
    hours = percentile(values, 0.85)
    return (
        f"p85 of {len(values)} merged PR{'s' if len(values) != 1 else ''} "
        f"({hours:g} h)"
        if hours is not None else "no merged PRs, no p85"
    )


def review_thresholds(court: ballcourt.BallCourt,
                      window: tuple[datetime, datetime] | None = None) -> dict:
    """The time-in-review cohort: ``p50``/``p75``/``p85``/``p95`` over merged PRs."""
    values = [hours for _pr, hours in review_cohort(court, window)]
    block = _entry(REVIEW_TIME, "Time in review", values, BASIS_REVIEW, p75=True)
    block["source_label"] = _review_source(values)
    return block


def pr_cycle_thresholds(court: ballcourt.BallCourt,
                        window: tuple[datetime, datetime] | None = None) -> dict:
    values = pr_cycle_cohort(court, window)
    block = _entry(PR_CYCLE, "PR cycle", values, BASIS_PR_CYCLE)
    block["source_label"] = (
        f"p85 of {len(values)} merged PRs ({block['p85']:g} h)"
        if block["available"] else "no p85 (cohort too small)"
    )
    return block


def story_thresholds(court: ballcourt.BallCourt, data: jira.JiraData | None,
                     window: tuple[datetime, datetime] | None = None) -> dict[str, dict]:
    """Per story clock: the cycle plus every workflow stage the store measured.

    ``{"story_cycle": {…}, "stage:in_progress": {…}, …}`` — exactly the
    selector's options, so a stage the store never completed simply has no
    entry (and no empty 0).
    """
    cycle_values = [row["hours"] for row in story_cycle_cohort(court, data, window)]
    blocks = {
        STORY_CYCLE: _entry(
            STORY_CYCLE, "Story cycle", cycle_values, BASIS_STORY_CYCLE,
        ),
    }
    blocks[STORY_CYCLE]["source_label"] = (
        f"p85 of {len(cycle_values)} completed stories "
        f"({blocks[STORY_CYCLE]['p85']:g} h)"
        if blocks[STORY_CYCLE]["available"] else "no p85 (cohort too small)"
    )
    for stage, values in stage_cohort(court, data, window).items():
        key = f"stage:{stage}"
        block = _entry(key, f"Time in {stage.replace('_', ' ')}", values, BASIS_STAGE)
        block["stage"] = stage
        block["source_label"] = (
            f"p85 of {len(values)} completed stories ({block['p85']:g} h)"
            if block["available"] else "no p85 (cohort too small)"
        )
        blocks[key] = block
    return blocks


def threshold_for(kind: str, stage: str | None, blocks: dict[str, dict]) -> dict | None:
    """The threshold block a row of ``kind``/``stage`` is read against."""
    if kind == "pr":
        return blocks.get(REVIEW_TIME if stage in (None, REVIEW) else PR_CYCLE)
    if stage in (None, CYCLE, STORY_CYCLE):
        return blocks.get(STORY_CYCLE)
    return blocks.get(f"stage:{stage}")


def band_for(age_hours: float | None, block: dict | None) -> str:
    """The band of one age (half-open intervals; exactly p85 is ``aging``)."""
    if age_hours is None or not block or not block.get("available"):
        return "insufficient"
    p50, p85, p95 = block.get("p50"), block.get("p85"), block.get("p95")
    if p50 is None or p85 is None or p95 is None:
        return "insufficient"
    if age_hours < p50:
        return "normal"
    if age_hours < p85:
        return "watch"
    if age_hours < p95:
        return "aging"
    return "very_old"


def rank_pct(age_hours: float | None, values) -> float | None:
    """Share of the cohort **≤** the value (ties counted as ≤), 4 decimals."""
    cohort = [float(value) for value in values if value is not None]
    if age_hours is None or not cohort:
        return None
    return round(sum(1 for value in cohort if value <= age_hours) / len(cohort), 4)


# --------------------------------------------------------------------------- #
# liveness (prd/58's classifier, owned here so both views cannot disagree)
# --------------------------------------------------------------------------- #
def _last_human_event(court: ballcourt.BallCourt, story) -> datetime | None:
    """The last event a human left on the story or any of its PRs.

    ``worklogged`` is bookkeeping, not a handover: logging hours on abandoned
    work must not reset the clock (DEL-114's only activity since 27 Aug is a
    worklog). Bot actors are excluded outright (concepts.md §2: a stale sweep is
    not activity).
    """
    bots = set(court.bots)
    bookkeeping = {"worklogged"}

    def counts(event) -> bool:
        return bool(
            event.get("actor")
            and event["actor"] not in bots
            and event.get("activity") not in bookkeeping
        )

    stamps = [
        event["_ts"] for event in story.events if counts(event)
    ]
    for state in story.prs:
        pr = state.pr
        if pr.get("created_ts") is not None and pr.get("actor") not in bots:
            stamps.append(pr["created_ts"])
        stamps.extend(event["_ts"] for event in state.events if counts(event))
    return max(stamps) if stamps else None


def liveness(court: ballcourt.BallCourt, story, *, done: bool = False) -> dict:
    """One story's liveness: ``live`` · ``unmerged`` · ``dead`` · ``unobserved``.

    ``live`` = at least one open PR (or a recent human event with none);
    ``unmerged`` = the ticket is Done in Jira while a PR is still open;
    ``dead`` = no open PR **and** no human event for ≥ 10 business days (a bot
    sweep is not activity, §2); ``unobserved`` = no open PR but a human acted
    recently, so the tail is unmeasured rather than abandoned.
    """
    open_prs = [state.pr for state in story.prs if state.pr.get("closed_ts") is None]
    last = _last_human_event(court, story)
    age = (
        round(court.business_hours(None, last, court.snapshot), 2)
        if last is not None else None
    )
    if open_prs:
        if done:
            return {
                "state": "unmerged", "last_human": last,
                "reason": "the ticket is Done in Jira while a PR is still open",
            }
        return {"state": "live", "last_human": last, "reason": None}
    if age is None or age >= DEAD_HOURS:
        return {
            "state": "dead", "last_human": last,
            "reason": (
                "no open PR and no human event for "
                f"{DEAD_BUSINESS_DAYS:g} business days — abandoned work, not a "
                "slow review"
            ),
        }
    return {
        "state": "unobserved", "last_human": last,
        "reason": "no open PR, but a human acted recently — the tail is unmeasured",
    }


def _story_prs(story) -> list:
    return [state for state in story.prs if state.pr.get("closed_ts") is None]


def _holder_block(court: ballcourt.BallCourt, states, hours: float | None,
                  story=None) -> list[dict]:
    """Who holds the item now: the delivery row's holder set, role-labelled.

    Holder hours are the item's age while it waited on that person; several
    holders **share** the same hours (concepts.md §2 person-hours) and are never
    summed into a total.
    """
    people: dict[str, str] = {}
    for state in states:
        latest = state.intervals[-1] if state.intervals else None
        if latest is None:
            continue
        for person in latest.person_holders:
            if person == state.pr.get("actor"):
                people[person] = "author"
            elif person in court.sentinels:
                people[person] = "sentinel"
            else:
                people[person] = "review"
    if not people and story is not None and story.actor:
        people[story.actor] = "owner"
    return [
        {"person": person, "role": role, "hours": hours}
        for person, role in sorted(people.items())
    ]


# --------------------------------------------------------------------------- #
# in-flight rows
# --------------------------------------------------------------------------- #
def _story_rows(court: ballcourt.BallCourt, data: jira.JiraData,
                stage: str | None) -> list[dict]:
    done_keys = jira.done_keys(data)
    rows: list[dict] = []
    for story in data.stories:
        cycle = jira._cycle(story)
        if cycle.boundaries.get("done") is not None:
            continue
        created = cycle.boundaries.get("created") or story.start
        current_stage, entered_stage = stage_clock(story)
        if stage in (None, CYCLE, STORY_CYCLE):
            entered = created
        else:
            if current_stage != stage or entered_stage is None:
                continue
            entered = entered_stage
        if entered is None or entered > court.snapshot:
            continue
        age = round(court.business_hours(story.actor, entered, court.snapshot), 2)
        open_states = _story_prs(story)
        status = liveness(
            court, story, done=story.key in done_keys,
        )
        rows.append({
            "kind": "story",
            "object_id": story.object_id,
            "ticket": story.key,
            "key": story.key,
            "pr": None,
            "stage": current_stage or CYCLE,
            "entered": iso(entered),
            "age_hours": age,
            "calendar_days": max(0, (court.snapshot - entered).days),
            "live": status["state"],
            "reason": status["reason"],
            "last_human": iso(status["last_human"]) if status["last_human"] else None,
            "open_prs": [state.pr["object_id"] for state in open_states],
            "holder": _holder_block(
                court, open_states, age,
                story if status["state"] in LIVE_STATES else None,
            ),
            # a story's own PRs are not downstream dependents; the chase tie
            # break reads the PR list's own count (prd/59 §Layout)
            "dependents": 0,
            "unobserved": status["state"] == "unobserved",
            "href": f"#ticket/{story.key}",
            "basis": (
                BASIS_STAGE if stage not in (None, CYCLE, STORY_CYCLE)
                else BASIS_STORY_CYCLE
            ),
        })
    return rows


def _pr_rows(court: ballcourt.BallCourt, data: jira.JiraData | None,
             stage: str | None) -> list[dict]:
    done = jira.done_keys(data) if data is not None else set()
    stories = {story.key: story for story in data.stories} if data is not None else {}
    delivery = ballcourt.delivery_list(
        court, done_tickets=done,
    )
    by_id = {row["object_id"]: row for row in delivery.get("rows") or ()}
    rows: list[dict] = []
    for state in court.prs:
        pr = state.pr
        if pr.get("closed_ts") is not None:
            continue
        if stage in (None, REVIEW):
            entered = first_review_start(state)
            age = review_hours(state)
            basis = BASIS_REVIEW
        else:
            entered = pr.get("created_ts")
            age = (
                round(court.business_hours(pr["actor"], entered, court.snapshot), 2)
                if entered is not None else None
            )
            basis = BASIS_PR_CYCLE
        if entered is None or entered > court.snapshot:
            continue
        meta = by_id.get(pr["object_id"]) or {}
        ticket = (meta.get("story_tickets") or [None])[0]
        story = stories.get(ticket) if ticket else None
        status = (
            liveness(court, story, done=bool(story and story.key in done))
            if story is not None else
            {"state": "live", "reason": None, "last_human": None}
        )
        # A PR row is never "dead": it is open. Its *story* can be abandoned,
        # but the open PR is the thing to merge or close, so it stays chasable.
        if status["state"] == "unobserved":
            status = {"state": "live", "reason": status["reason"],
                      "last_human": status["last_human"]}
        rows.append({
            "kind": "pr",
            "object_id": pr["object_id"],
            "ticket": ticket,
            "key": ticket,
            "pr": f"#{pr['pr_id']}",
            "repo": pr["repo"],
            "repo_short": pr["repo"].split("/")[-1],
            "stage": REVIEW if stage in (None, REVIEW) else CYCLE,
            "entered": iso(entered),
            "age_hours": age,
            "calendar_days": max(0, (court.snapshot - entered).days),
            "live": status["state"],
            "reason": status["reason"],
            "last_human": iso(status["last_human"]) if status["last_human"] else None,
            "open_prs": [pr["object_id"]],
            "holder": _holder_block(court, [state], age),
            "dependents": int(meta.get("dependents") or 0),
            "unobserved": bool(meta.get("unobserved")),
            "waiting_since": meta.get("waiting_since"),
            "waiting_hours": meta.get("waiting_hours"),
            "href": f"#ticket/{ticket}" if ticket else None,
            "basis": basis,
        })
    return rows


def in_flight(court: ballcourt.BallCourt, data: jira.JiraData | None,
              kind: str = "story", stage: str | None = None,
              window: tuple[datetime, datetime] | None = None,
              mode: str | None = None) -> list[dict]:
    """One row per open item, with its age, band, rank and holder.

    ``kind="story"`` uses the story clock (cycle or the given workflow stage);
    ``kind="pr"`` the review clock (``stage=review``, the default) or the PR
    cycle (``stage=cycle``). The clocks are never mixed in one row's age.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}: kind must be one of {', '.join(KINDS)}")
    blocks = {}
    values: dict[str, list[float]] = {}
    if kind == "story":
        if data is None:
            return []
        blocks = story_thresholds(court, data, window)
        values[STORY_CYCLE] = [
            row["hours"] for row in story_cycle_cohort(court, data, window)
        ]
        for stage_key, cohort in stage_cohort(court, data, window).items():
            values[f"stage:{stage_key}"] = cohort
        rows = _story_rows(court, data, stage)
        block_key = (
            STORY_CYCLE if stage in (None, CYCLE, STORY_CYCLE) else f"stage:{stage}"
        )
    else:
        blocks = {
            REVIEW_TIME: review_thresholds(court, window),
            PR_CYCLE: pr_cycle_thresholds(court, window),
        }
        values[REVIEW_TIME] = [hours for _pr, hours in review_cohort(court, window)]
        values[PR_CYCLE] = pr_cycle_cohort(court, window)
        rows = _pr_rows(court, data, stage)
        block_key = REVIEW_TIME if stage in (None, REVIEW) else PR_CYCLE
    block = blocks.get(block_key)
    cohort = values.get(block_key) or []
    block = apply_mode(block, block_key, mode or thresholds_mode())
    if block is None or not block.get("available"):
        # an unknown/never-completed stage: the row set is empty, the threshold
        # is a stated "insufficient" rather than a fabricated zero
        block = block or _entry(block_key, block_key, [], BASIS_STAGE)
    for row in rows:
        row["band"] = band_for(row["age_hours"], block)
        row["cohort_n"] = len(cohort)
        row["rank_pct"] = rank_pct(row["age_hours"], cohort)
        p85 = block.get("p85")
        row["multiple"] = (
            round(row["age_hours"] / p85, 2)
            if p85 and row.get("age_hours") is not None else None
        )
        row["hours_at_risk"] = (
            round(max(0.0, float(row["age_hours"]) - float(p85)), 2)
            if row.get("age_hours") is not None and p85 else 0.0
        )
        row["threshold_id"] = block.get("id")
    return rows


def chase_order(rows: list[dict]) -> list[dict]:
    """The ranked chase list: live rows in the aging/very-old bands.

    Sorted by ``hours_at_risk = age − p85`` descending (the removable wait),
    tie-broken by downstream dependents and then age. ``dead`` rows are
    excluded: no nudge can unblock abandoned work, it must be closed or
    re-planned (prd/59 §Algorithm 6).
    """
    ranked = [
        row for row in rows
        if row.get("live") in LIVE_STATES
        and row.get("band") in ("aging", "very_old")
    ]
    ranked.sort(key=lambda row: (
        -float(row.get("hours_at_risk") or 0.0),
        -int(row.get("dependents") or 0),
        -float(row.get("age_hours") or 0.0),
        str(row.get("object_id") or ""),
    ))
    return ranked


def replan(rows: list[dict]) -> list[dict]:
    """The rows with no live PR: close or re-plan them (never chase).

    ``dead`` (no human event for ≥ 10 business days) and ``unobserved`` (no PR,
    but a human acted recently) both belong here: neither is waiting on a
    nudge — the work behind it is gone or unmeasured, so it must be closed or
    re-planned (prd/59 §Honesty / §Algorithm 6).
    """
    abandoned = [
        row for row in rows
        if row.get("live") in ("dead", "unobserved") and not row.get("open_prs")
    ]
    abandoned.sort(key=lambda row: (-float(row.get("age_hours") or 0.0),
                                    str(row.get("object_id") or "")))
    return abandoned


# --------------------------------------------------------------------------- #
# rule thresholds (the constants this re-calibrates)
# --------------------------------------------------------------------------- #
def thresholds_mode() -> str:
    """``self`` (default) or ``fixed`` from ``MEGASPEED_AGING_THRESHOLDS``."""
    raw = os.environ.get(FIXED_MODE_ENV, "").strip().lower()
    return "fixed" if raw in {"fixed", "constant", "constants", "old"} else "self"


def normalize_mode(mode: str | None) -> str:
    return "fixed" if str(mode or "").strip().lower() == "fixed" else "self"


#: The two thresholds prd/59 re-calibrates, when the reader asks for the old
#: constants: a stale PR's 5 business days and the sentinel tail's 24 h.
FIXED_BY_KEY = {
    REVIEW_TIME: FIXED_STALE_HOURS,
    "stage:in_review": FIXED_SENTINEL_TAIL_HOURS,
}


def apply_mode(block: dict | None, key: str, mode: str) -> dict | None:
    """Replace a block's p85 with the fixed constant when ``mode="fixed"``.

    The team's p50/p95 still describe the distribution (they are a measurement,
    not a rule); only the line the rule uses moves, and the chip says ``fixed``.
    A cohort with no fixed constant (the story cycle, the PR cycle) keeps its
    self-calibrated p85 and says so rather than inventing a constant.
    """
    if block is None or normalize_mode(mode) != "fixed":
        return block
    out = dict(block)
    out["self_calibrated_p85"] = block.get("p85")
    out["source"] = "fixed"
    fixed = FIXED_BY_KEY.get(key)
    if fixed is not None:
        out["p85"] = fixed
        out["source_label"] = f"fixed ({fixed:g} h)"
    else:
        out["source_label"] = "self-calibrated (no fixed constant for this cohort)"
    return out


def rule_thresholds(court: ballcourt.BallCourt, data: jira.JiraData | None,
                    window: tuple[datetime, datetime] | None = None,
                    mode: str | None = None) -> dict:
    """The two rules' thresholds and where each number came from.

    ``stale-prs`` keeps its id and evidence but its limit is the p85 of the
    *review-time* cohort; ``sentinel-tail`` uses the p85 of completed stories'
    ``review_rounds`` segment (the story-side review wait). A cohort below
    :data:`MIN_N` falls back to the old constant **and says so**; with
    ``MEGASPEED_AGING_THRESHOLDS=fixed`` (or ``mode="fixed"``) the old constants
    apply by choice.
    """
    mode = mode or thresholds_mode()
    review = review_thresholds(court, window)
    segments = jira.segment_cohort(court, data) if data is not None else {}
    review_segment = segments.get("review_rounds", [])
    segment_entry = _entry(
        "story_review_segment", "Story review segment", review_segment, BASIS_STAGE,
    )

    def pick(entry, fixed_hours, fixed_label, cohort_label):
        if mode == "fixed":
            return {
                "threshold_hours": fixed_hours,
                "available": True,
                "n": entry["n"],
                "min_n": MIN_N,
                "source": f"fixed ({fixed_label}) by setting",
                "self_calibrated": entry["p85"],
            }
        if entry.get("available") and entry.get("p85") is not None:
            return {
                "threshold_hours": entry["p85"],
                "available": True,
                "n": entry["n"],
                "min_n": MIN_N,
                "source": (
                    f"p85 of {entry['n']} {cohort_label} ({entry['p85']:g} h)"
                ),
                "self_calibrated": entry["p85"],
            }
        return {
            "threshold_hours": fixed_hours,
            "available": False,
            "n": entry["n"],
            "min_n": MIN_N,
            "source": (
                f"fixed ({fixed_label}) — insufficient history: "
                f"{entry['n']} finished item{'' if entry['n'] == 1 else 's'} "
                f"(needs {MIN_N})"
            ),
            "self_calibrated": None,
        }

    stale = pick(review, FIXED_STALE_HOURS, "5 business days", "merged PRs")
    stale["threshold_days"] = round(
        stale["threshold_hours"] / ballcourt.BUSINESS_HOURS_PER_DAY, 2,
    )
    tail = pick(
        segment_entry, FIXED_SENTINEL_TAIL_HOURS, "24 h",
        "completed stories' review segment",
    )
    return {
        "stale_prs": stale,
        "sentinel_tail": tail,
        "mode": mode,
        "min_n": MIN_N,
    }


# --------------------------------------------------------------------------- #
# the payload
# --------------------------------------------------------------------------- #
def window_for(court: ballcourt.BallCourt, data: jira.JiraData | None,
               days: float | None = None) -> tuple[datetime, datetime]:
    """The window, extended to the first story (a Jira-only store has no PRs)."""
    first_pr = min((state.pr["created_ts"] for state in court.prs), default=None)
    first_interval = min((interval.start for interval in court.intervals), default=None)
    first_story = min(
        (story.start for story in (data.stories if data else ()) if story.start),
        default=None,
    )
    first = min(
        (stamp for stamp in (first_pr, first_interval, first_story) if stamp is not None),
        default=court.snapshot,
    )
    if not days:
        return first, court.snapshot
    return max(first, court.snapshot - timedelta(days=float(days))), court.snapshot


def stages_for(kind: str, court: ballcourt.BallCourt,
               data: jira.JiraData | None,
               window: tuple[datetime, datetime] | None = None) -> list[str]:
    """The stage options this store can actually answer (never an empty 0)."""
    if kind == "pr":
        return [REVIEW, CYCLE]
    measured = stage_cohort(court, data, window)
    return [CYCLE] + [stage for stage in STAGE_VIEWS if measured.get(stage)]


def payload(court: ballcourt.BallCourt, data: jira.JiraData | None, *,
            kind: str = "story", stage: str | None = None,
            only: str = "all", limit: int | None = None,
            window: tuple[datetime, datetime] | None = None,
            min_n: int = MIN_N, mode: str | None = None) -> dict:
    """``GET /api/aging``: thresholds, the in-flight rows, the chase feed."""
    mode = mode or thresholds_mode()
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}: kind must be one of {', '.join(KINDS)}")
    stages = stages_for(kind, court, data, window)
    if stage is not None and stage not in stages:
        raise ValueError(
            f"unknown stage {stage!r}: stage must be one of {', '.join(stages)}"
        )
    rows = in_flight(court, data, kind, stage, window, mode=mode)
    if only == "live":
        rows = [row for row in rows if row["live"] in LIVE_STATES]
    all_rows = list(rows)
    if limit:
        rows = rows[: int(limit)]
    chase = chase_order(all_rows)
    abandoned = replan(all_rows)
    if kind == "story":
        blocks = story_thresholds(court, data, window)
        block_key = STORY_CYCLE if stage in (None, CYCLE, STORY_CYCLE) else f"stage:{stage}"
    else:
        blocks = {
            REVIEW_TIME: review_thresholds(court, window),
            PR_CYCLE: pr_cycle_thresholds(court, window),
        }
        block_key = REVIEW_TIME if stage in (None, REVIEW) else PR_CYCLE
    block = apply_mode(blocks.get(block_key), block_key, mode)
    review = blocks.get(REVIEW_TIME) or review_thresholds(court, window)
    hours_at_risk = round(
        sum(float(row.get("hours_at_risk") or 0.0) for row in chase), 2,
    )
    p50 = (block or {}).get("p50")
    counts = {
        "open": len(all_rows),
        "live": len([row for row in all_rows if row["live"] in LIVE_STATES]),
        "dead": len([row for row in all_rows if row["live"] == "dead"]),
        "over_p50": len([
            row for row in all_rows
            if p50 is not None and row.get("age_hours") is not None
            and row["age_hours"] >= p50
        ]),
        "over_p85": len([row for row in all_rows if row.get("band") in ("aging", "very_old")]),
        "over_p95": len([row for row in all_rows if row.get("band") == "very_old"]),
        "insufficient": len([row for row in all_rows if row.get("band") == "insufficient"]),
        "chase": len(chase),
        "min_n": min_n,
    }
    if data is not None and jira.done_keys(data):
        done = jira.done_keys(data)
        counts["unmerged"] = len([
            row for row in all_rows
            if row.get("kind") == "pr" and row.get("ticket") in done
        ])
    else:
        counts["unmerged"] = 0
    start, end = window or window_for(court, data)
    if kind == "story":
        order = [STORY_CYCLE] + [
            f"stage:{name}" for name in stages if name != CYCLE
        ]
    else:
        order = [REVIEW_TIME, PR_CYCLE]
    return {
        "ok": True,
        "as_of": iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "calendar_id": court.calendar_id,
        "kind": kind,
        "stage": stage or (REVIEW if kind == "pr" else STORY_CYCLE),
        "only": only,
        "window": {"start": iso(start), "end": iso(end)},
        "thresholds": [
            apply_mode(blocks[key], key, mode) for key in order if key in blocks
        ],
        "active_threshold": block_key,
        "rows": rows,
        "counts": counts,
        "review_p75": {
            "value": review.get("p75"),
            "p50": review.get("p50"),
            "p85": review.get("p85"),
            "p95": review.get("p95"),
            "n": review.get("n"),
            "min_n": review.get("min_n"),
            "available": review.get("available"),
            "basis": "holder-priced time in review (the ball clock, role-level once)",
            "note": review.get("note"),
        },
        "feed": {
            "chase": [row["object_id"] for row in chase],
            "items": [
                {
                    "object_id": row["object_id"],
                    "kind": row["kind"],
                    "ticket": row.get("ticket"),
                    "pr": row.get("pr"),
                    "age_hours": row.get("age_hours"),
                    "band": row.get("band"),
                    "holder": row.get("holder"),
                    "hours_at_risk": row.get("hours_at_risk"),
                    "href": row.get("href"),
                    "dependents": row.get("dependents"),
                }
                for row in chase
            ],
            "hours_at_risk": hours_at_risk,
            "threshold_source": (
                f"{block['source_label']}" if block and block.get("source_label")
                else (block.get("note") if block else None)
            ),
            "threshold_hours": block.get("p85") if block else None,
            "replan": [
                {
                    "object_id": row["object_id"],
                    "ticket": row.get("ticket"),
                    "pr": row.get("pr"),
                    "age_hours": row.get("age_hours"),
                    "reason": row.get("reason"),
                    "href": row.get("href"),
                }
                for row in abandoned
            ],
            "basis": (
                "chase = live items older than the p85 of finished work; "
                "hours at risk = age − p85; dead rows are listed under replan"
            ),
        },
        "stages": stages,
        # the full selector is offered whatever cohort is on screen: a reader
        # can move from a PR clock to a story stage in one control
        "story_stages": stages_for("story", court, data, window),
        "min_n": min_n,
        "bands": list(BANDS),
        "band_labels": dict(BAND_LABEL),
        "note": NOTE,
        "basis": (
            "percentiles are nearest-rank on the sorted cohort "
            "(index = round(q × (n − 1))); ages are business hours on the "
            "holder's calendar; one clock per row"
        ),
    }

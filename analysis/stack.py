"""Stacked-PR cascade (prd/22): the stack DAG and the cost of a lower-PR update.

A PR is *stacked* when its source branch is another PR's target branch: landing
``A`` moves the ground under ``B``, and under ``C`` on top of ``B``. The store
cannot hold branch names (prd/02 -- a DC branch is the ticket summary in slug
form), so ingest persists a one-way digest of each ref and the edge is digest
equality:

    A -> B   when   A.target_ref == B.source_ref          (same repo)

Two numbers come out of that graph:

* **stack depth** -- how tall the stacks are (median / max over the stacked
  PRs), the structural fact.
* **cascade cost** -- what a lower-PR update actually cost up-stack. For every
  ``pr_merged`` or rebase ``pr_pushed`` on a PR that has PRs above it, and for
  every PR above it that was open at that moment, we look for the rebase it
  forced (the next ``pr_pushed``/``rebased`` on that PR) and price the review
  round that rebase reopened -- business hours when the tenant has a calendar
  (prd/19), wall-clock otherwise, through the same ``BallCourt`` intervals
  every other metric uses.

The re-review price is the interval that *starts at the rebase push* and ends at
the next reviewer action, so it is one review round, not "all the time until
merge": a rebase that reopens review three times is three rounds. A round that
never ended before the snapshot is reported as ``unfinished_rounds`` instead of
being counted -- absence of a completion is not zero cost, and it is not a
number either.

The **critical path** is the longest root-to-leaf chain of that attributed cost
inside one stack: the hours one bottom-of-stack merge walks up the chain. A
corpus with no ref digests reports ``available: false`` (re-ingest needed) and a
corpus with refs but no stacks reports ``stacks: 0`` -- both are results, never
a zero that looks like a measurement.

Two guards keep the DAG from inventing edges (the store has digests, not branch
names, so both are derived from the data rather than a name list):

* **the trunk is not a PR.** A branch that two or more PRs target in a repo is
  that repo's shared base (``main``/``master``/``develop``/a release branch). A
  PR *from* a shared base is based on it, not stacked under anything, so it may
  not be a parent: a ``main -> prod`` release PR must not adopt every PR that
  targets ``main``. Dropped candidates are counted in
  ``ignored_shared_branch_edges``;
* **a stack is ordered in time.** A parent must exist before its child and be
  open when the child is created (a PR that closed before the child was opened
  cannot be the branch the child sits on). Dropped candidates are counted in
  ``ignored_temporal_edges``.

The cascade attribution is bounded the same way: a rebase is charged to a
lower-PR update only when it happens within the child's next business day
(:data:`DEFAULT_BUSINESS_DAY_HOURS` without a calendar). A push six weeks later
is the child's own work, not the cascade; a trigger whose rebase fell outside
the window is reported as ``unattributed_triggers`` rather than silently priced.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from analysis import ballcourt

# A lower-PR update that moves the branches above it: the PR landed, or the
# author force-pushed it onto a new base. A *content* push does not move the
# ground under a child, so it is not a trigger.
REBASE_CLASSES = frozenset({"pure_rebase", "rebase_conflict"})
TRIGGER_PUSHES = frozenset({"pr_pushed", "rebased"})
# How long after a lower-PR update a child's rebase still counts as the cascade:
# one business day -- the calendar's daily window, or 24 wall-clock hours when
# the tenant has none (prd/19), the same day the simulation uses. Beyond it the
# push is the child's own work, and attributing it would charge six weeks of
# drift to a merge that happened six weeks earlier.
DEFAULT_BUSINESS_DAY_HOURS = 24.0
# A stack deeper than this is a chain of waits, not a review: the advice caps
# depth at two (a PR and the one it sits on).
MAX_STACK_DEPTH = 2
DEEP_STACK = MAX_STACK_DEPTH + 1


def _median(values: list[float], digits: int = 2) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], digits)
    return round((ordered[middle - 1] + ordered[middle]) / 2.0, digits)


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1))
    return round(ordered[index], 2)


@dataclass(slots=True)
class StackGraph:
    """The DAG over PR object ids, plus the structural facts about it."""

    prs: dict[str, dict]
    children: dict[str, list[str]]
    parents: dict[str, list[str]]
    depth: dict[str, int]
    refs: int                 # PRs that carry at least one ref digest
    edges: list[tuple[str, str]] = field(default_factory=list)
    # candidate edges dropped because the "parent" is based on the trunk: a
    # shared base branch is not a stack edge (FR-5), and the count keeps the
    # exclusion visible instead of silently shrinking the graph
    ignored_shared_branch_edges: int = 0
    # candidate edges dropped because the parent did not exist yet, or had
    # already closed, when the child was opened
    ignored_temporal_edges: int = 0

    def above(self, object_id: str) -> list[str]:
        """Every PR above ``object_id`` (transitive children), cycle-safe."""
        seen: set[str] = set()
        queue = list(self.children.get(object_id, ()))
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(self.children.get(current, ()))
        return sorted(seen)

    def components(self) -> list[list[str]]:
        """Connected stacks (undirected), each sorted by depth then id."""
        seen: set[str] = set()
        stacks: list[list[str]] = []
        for object_id in sorted(self.children):
            if object_id in seen:
                continue
            group: list[str] = []
            queue = [object_id]
            while queue:
                current = queue.pop()
                if current in seen:
                    continue
                seen.add(current)
                group.append(current)
                queue.extend(self.children.get(current, ()))
                queue.extend(self.parents.get(current, ()))
            if len(group) > 1:
                stacks.append(sorted(group, key=lambda oid: (self.depth.get(oid, 0), oid)))
        return stacks


def _depth(prs: dict[str, dict], parents: dict[str, list[str]]) -> dict[str, int]:
    """Longest chain below each PR (0 = a base). Cycle-safe: a malformed DAG
    (two PRs sharing a branch) must not hang the analysis."""
    memo: dict[str, int] = {}

    def walk(object_id: str, path: frozenset[str]) -> int:
        if object_id in memo:
            return memo[object_id]
        best = 0
        for parent in parents.get(object_id, ()):
            if parent in path:
                continue
            best = max(best, walk(parent, path | {object_id}) + 1)
        memo[object_id] = best
        return best

    for object_id in prs:
        walk(object_id, frozenset())
    return memo


def _trunk_refs(prs: dict[str, dict]) -> dict[str, set[str]]:
    """``repo -> the ref digests that are shared base branches``.

    The store holds digests, never branch names (prd/02), so a shared base is
    derived rather than listed: a branch that **two or more** PRs target is an
    integration branch (``main``/``master``/``develop``/a release branch), while
    a real stack base is targeted by exactly one child. A PR whose source is a
    shared base is based on it, not stacked under anything, so it may not be a
    parent -- otherwise a ``main -> prod`` release PR adopts every PR that
    targets ``main`` (FR-5).

    The threshold is deliberately conservative: two children forked off one
    feature branch are indistinguishable from two PRs sharing a trunk using
    digests alone, and the dropped edges are counted in
    ``ignored_shared_branch_edges`` rather than hidden.
    """
    counts: dict[str, Counter] = defaultdict(Counter)
    for pr in prs.values():
        target = pr.get("target_ref")
        if target:
            counts[pr.get("repo") or ""][target] += 1
    return {
        repo: {ref for ref, count in counter.items() if count >= 2}
        for repo, counter in counts.items()
    }


def _can_be_parent(parent: dict, child: dict) -> bool:
    """A stack edge is ordered in time: the parent exists and is open first."""
    created = parent.get("created_ts")
    child_created = child.get("created_ts")
    if created is not None and child_created is not None and created > child_created:
        return False
    closed = parent.get("closed_ts")
    if closed is not None and child_created is not None and closed <= child_created:
        return False
    return True


def build(court: ballcourt.BallCourt) -> StackGraph:
    """Derive the stack DAG from the PR ref digests on the store."""
    prs = {state.pr["object_id"]: state.pr for state in court.prs}
    trunk = _trunk_refs(prs)
    by_source: dict[str, list[str]] = defaultdict(list)
    refs = 0
    for object_id, pr in prs.items():
        # one ref is enough to place a PR in the DAG: a parent needs its source,
        # a child its target. Ingest writes both, but a partial store (or a
        # hand-built one) must not read as "no refs at all".
        if pr.get("source_ref") or pr.get("target_ref"):
            refs += 1
        if pr.get("source_ref"):
            by_source[pr["source_ref"]].append(object_id)

    children: dict[str, list[str]] = defaultdict(list)
    parents: dict[str, list[str]] = defaultdict(list)
    edges: list[tuple[str, str]] = []
    ignored_shared = 0
    ignored_temporal = 0
    for object_id, pr in prs.items():
        target = pr.get("target_ref")
        if not target:
            continue
        # this PR sits on whichever PR *has* its target branch as its source
        for parent in sorted(by_source.get(target, ())):
            if parent == object_id:
                continue
            parent_pr = prs[parent]
            # a PR from the trunk is based on the trunk, not stacked under
            # anything -- otherwise a main -> prod release PR parents every PR
            # that targets main (FR-5)
            repo = parent_pr.get("repo") or pr.get("repo")
            if parent_pr.get("source_ref") in trunk.get(repo, ()):
                ignored_shared += 1
                continue
            if not _can_be_parent(parent_pr, pr):
                ignored_temporal += 1
                continue
            children[parent].append(object_id)
            parents[object_id].append(parent)
            edges.append((parent, object_id))
    return StackGraph(
        prs=prs,
        children=dict(children),
        parents=dict(parents),
        depth=_depth(prs, parents),
        refs=refs,
        edges=sorted(edges),
        ignored_shared_branch_edges=ignored_shared,
        ignored_temporal_edges=ignored_temporal,
    )


def _trigger(event: dict) -> str | None:
    """``"merge"`` / ``"rebase"`` for a lower-PR update, else ``None``."""
    activity = event["activity"]
    if activity == "pr_merged":
        return "merge"
    if activity in TRIGGER_PUSHES:
        if activity == "rebased":
            return "rebase"
        if (event.get("attributes") or {}).get("push_class") in REBASE_CLASSES:
            return "rebase"
    return None


def _response_push(events: list[dict], after: datetime) -> dict | None:
    """The first push on the upper PR at/after the trigger: the rebase it forced."""
    for event in events:
        if event["_ts"] < after:
            continue
        if event["activity"] in TRIGGER_PUSHES:
            return event
    return None


def _business_day_hours(court: ballcourt.BallCourt) -> float:
    """One day on the tenant's clock: the calendar window, else 24 wall hours."""
    calendar = court.calendar
    if calendar is None or calendar.default_window is None:
        return DEFAULT_BUSINESS_DAY_HOURS
    start, end = calendar.default_window
    return max(0.5, (end - start) / 60.0)


def _within_response_window(court: ballcourt.BallCourt, pr: dict,
                            at: datetime, push_ts: datetime) -> bool:
    """Is ``push_ts`` close enough to ``at`` to be the rebase it forced?

    A cascade rebase is the child's next move after its base changed; a push
    that comes a day or a month later is the child's own work. The window is one
    business day on the child author's calendar (prd/19), so a weekend does not
    swallow a Monday rebase.
    """
    if push_ts < at:
        return False
    return (
        court.business_hours(pr["actor"], at, push_ts)
        <= _business_day_hours(court)
    )


def _round_hours(intervals: dict[str, list], object_id: str, push_ts: datetime) -> tuple[float, bool]:
    """The review round a rebase push reopened: ``(hours, finished)``.

    The interval that starts at the push is the round; its phase is ``review``
    when the next thing that happened was a reviewer action (or the merge). A
    round that ran into another push, the snapshot or the close is *not* priced
    -- it is returned as unfinished so the payload can say so.
    """
    for interval in intervals.get(object_id, ()):
        if interval.start == push_ts:
            if interval.phase == "review":
                return interval.hours, True
            return 0.0, False
    return 0.0, False


def _descendants(graph: StackGraph) -> dict[str, list[str]]:
    """Transitive children per PR, computed once (not per event).

    A huge project (prd/14) has one DAG walk, not one per PR: the naive
    ``graph.above()`` per PR is O(V·E) on a 10k-PR store.
    """
    memo: dict[str, set[str]] = {}

    def walk(object_id: str, path: frozenset[str]) -> set[str]:
        if object_id in memo:
            return memo[object_id]
        out: set[str] = set()
        for child in graph.children.get(object_id, ()):
            if child in path:
                continue
            out.add(child)
            out |= walk(child, path | {object_id})
        memo[object_id] = out
        return out

    return {object_id: sorted(walk(object_id, frozenset())) for object_id in graph.children}


def cascade(court: ballcourt.BallCourt, window: tuple[datetime, datetime],
            graph: StackGraph | None = None) -> dict:
    """Every up-stack rebase a lower-PR update triggered, and what it cost.

    ``triggers`` counts the lower-PR updates that actually forced a *new*
    rebase above them: a merge followed by the parent's own rebase moves the
    same branch twice, but the child pushes once, and it is attributed to the
    earliest of the two.
    """
    start, end = window
    graph = graph or build(court)
    descendants = _descendants(graph)
    intervals: dict[str, list] = defaultdict(list)
    for interval in court.intervals:
        intervals[interval.object_id].append(interval)

    # Every lower-PR update that has a PR above it open at that moment, in time
    # order: an upper PR's *first* push after two close triggers is one rebase,
    # so it is attributed to the earliest trigger and never counted twice.
    found: list[tuple[datetime, str, str, list[str]]] = []
    for state in court.prs:
        object_id = state.pr["object_id"]
        above = descendants.get(object_id, [])
        if not above:
            continue
        for event in state.events:
            kind = _trigger(event)
            if kind is None:
                continue
            if not (start <= event["_ts"] <= end):
                continue
            open_above = [
                child for child in above
                if graph.prs[child]["created_ts"] <= event["_ts"]
                and (
                    graph.prs[child]["closed_ts"] is None
                    or graph.prs[child]["closed_ts"] > event["_ts"]
                )
            ]
            if open_above:
                found.append((event["_ts"], object_id, kind, open_above))
    found.sort(key=lambda entry: (entry[0], entry[1]))

    triggers = 0
    pairs: list[dict] = []
    unfinished = 0
    attributed: set[tuple[str, datetime]] = set()
    late: set[tuple[str, datetime]] = set()
    states = {state.pr["object_id"]: state for state in court.prs}
    for at, object_id, kind, open_above in found:
        caused = 0
        for child in open_above:
            push = _response_push(states[child].events, at)
            if push is None:
                continue
            if (child, push["_ts"]) in attributed:
                continue
            is_rebase = push["activity"] == "rebased" or (
                (push.get("attributes") or {}).get("push_class") in REBASE_CLASSES
            )
            if not is_rebase:
                continue
            if not _within_response_window(
                court, states[child].pr, at, push["_ts"]
            ):
                # the child did rebase, but far too late to be this trigger's
                # cascade: report the dropped attribution instead of pricing it
                late.add((object_id, at))
                continue
            attributed.add((child, push["_ts"]))
            hours, finished = _round_hours(intervals, child, push["_ts"])
            if not finished:
                unfinished += 1
            caused += 1
            pairs.append({
                "trigger_pr": object_id,
                "pr": child,
                "repo": graph.prs[child]["repo"],
                "trigger": kind,
                "at": at,
                "rebase_at": push["_ts"],
                "hours": hours,
                "depth": graph.depth.get(child, 0),
            })
        if caused:
            triggers += 1

    hours_by_pr: dict[str, float] = defaultdict(float)
    rebases_by_pr: dict[str, int] = defaultdict(int)
    for pair in pairs:
        hours_by_pr[pair["pr"]] += pair["hours"]
        rebases_by_pr[pair["pr"]] += 1

    return {
        "graph": graph,
        "triggers": triggers,
        "pairs": pairs,
        "unfinished_rounds": unfinished,
        "unattributed_triggers": len(late),
        "hours_by_pr": dict(hours_by_pr),
        "rebases_by_pr": dict(rebases_by_pr),
    }


def _critical_path(graph: StackGraph, hours_by_pr: dict[str, float],
                   rebases_by_pr: dict[str, int]) -> dict:
    """Longest root-to-leaf chain of attributed re-review cost in one stack.

    A chain ``A -> B -> C`` prices as ``hours(B) + hours(C)``: the cost a single
    bottom-of-stack update walks up. Node costs are non-negative, so the longest
    chain from a root is the root's cost plus the best child chain. Ties break
    on the chain ids so the answer is deterministic.
    """
    memo: dict[str, tuple[float, int, list[str]]] = {}

    def down(object_id: str) -> tuple[float, int, list[str]]:
        """Best chain *starting* at ``object_id``: (hours, rebases, chain)."""
        if object_id in memo:
            return memo[object_id]
        best = (0.0, 0, [])
        for child in sorted(graph.children.get(object_id, ())):
            candidate = down(child)
            if candidate[0] > best[0] or (
                candidate[0] == best[0] and candidate[2] and candidate[2] < best[2]
            ):
                best = candidate
        result = (
            round(hours_by_pr.get(object_id, 0.0) + best[0], 2),
            rebases_by_pr.get(object_id, 0) + best[1],
            [object_id] + best[2],
        )
        memo[object_id] = result
        return result

    best_hours = 0.0
    best_rebases = 0
    best_ids: list[str] = []
    for stack in graph.components():
        for root in [oid for oid in stack if not graph.parents.get(oid)]:
            hours, rebases, chain = down(root)
            # compare object ids (never the display numbers) so a tie is a
            # deterministic pick and never an int/str comparison
            if hours > best_hours or (
                hours == best_hours and chain and (not best_ids or chain < best_ids)
            ):
                best_hours, best_rebases, best_ids = hours, rebases, chain
    return {
        "repo": graph.prs[best_ids[-1]]["repo"] if best_ids else None,
        "chain": [graph.prs[oid]["pr_id"] for oid in best_ids],
        "hours": best_hours,
        "rebases": best_rebases,
    }


def metrics(court: ballcourt.BallCourt, window: tuple[datetime, datetime]) -> dict:
    """The ``stacked_cascade`` metric (prd/22): DAG, depth, cascade cost."""
    start, end = window
    graph = build(court)
    result = cascade(court, window, graph)

    cohort = [
        state for state in court.prs
        if start <= state.pr["created_ts"] <= end
    ]
    cohort_ids = {state.pr["object_id"] for state in cohort}
    stacked = [oid for oid in cohort_ids if graph.depth.get(oid, 0) > 0]
    # a stack counts when a *stacked* PR of the cohort is in it: a window that
    # only catches a stack's base has no stacked PR to report, and must not
    # claim a stack it cannot measure
    components = graph.components()
    component_of = {
        oid: index for index, component in enumerate(components) for oid in component
    }
    stacks = [
        components[index]
        for index in sorted({component_of[oid] for oid in stacked if oid in component_of})
    ]
    depths = [float(graph.depth[oid]) for oid in stacked]
    pairs = result["pairs"]
    rebases = len(pairs)
    hours = round(sum(pair["hours"] for pair in pairs), 2)
    critical = _critical_path(graph, result["hours_by_pr"], result["rebases_by_pr"])

    available = graph.refs > 0
    if not available:
        note = (
            "no PR carries source/target ref digests: this store predates the stack "
            "measurement, so the stack DAG cannot be derived (re-ingest to measure it)"
        )
    elif not stacks:
        note = "every PR targets its own branch (or main): the corpus has no stacked PRs"
    elif not rebases:
        note = (
            "stacks exist, but no lower-PR update was observed to force a rebase "
            "up-stack inside this window"
        )
    else:
        note = (
            "one re-review round is priced from the rebase push to the next reviewer "
            "action; a round that had not ended at the snapshot is reported as "
            "unfinished, never as zero"
        )
    if result["unattributed_triggers"]:
        note += (
            f"; {result['unattributed_triggers']} trigger(s) had a rebase outside "
            f"the {_business_day_hours(court):g} business-hour attribution "
            "window and were not priced"
        )

    return {
        "available": available,
        "prs_with_refs": graph.refs,
        "prs": len(cohort),
        "stacks": len(stacks),
        "stacked_prs": len(stacked),
        # edges into the cohort (the child is the stacked PR), so a reader can
        # never see "0 stacks" next to "3 edges" from a stack outside the window
        "edges": sum(1 for _parent, child in graph.edges if child in cohort_ids),
        # the exclusions that keep the DAG honest (FR-5): a PR based on the
        # trunk is not a parent, and a parent must exist before its child
        "ignored_shared_branch_edges": graph.ignored_shared_branch_edges,
        "ignored_temporal_edges": graph.ignored_temporal_edges,
        # depth distribution over the stacked PRs in the cohort (prd/22)
        "depth": {
            "n": len(depths),
            "median": _median(depths),
            "max": int(max(depths)) if depths else 0,
            "p90": _p90(depths),
            "deep_prs": sum(1 for value in depths if value >= DEEP_STACK),
            "histogram": {
                str(level): sum(1 for value in depths if value == level)
                for level in sorted({int(value) for value in depths})
            },
        },
        "triggers": result["triggers"],
        "cascaded_rebases": rebases,
        "re_review_hours": hours,
        "unfinished_rounds": result["unfinished_rounds"],
        "unattributed_triggers": result["unattributed_triggers"],
        "critical_path": critical,
        # prd/26 P5: one row per stacked PR, so "a lower-PR update triggered N
        # rebases" opens into the PRs and the hours each one paid.
        "rows": [
            {
                "kind": "pr",
                "object_id": object_id,
                "repo": graph.prs[object_id]["repo"],
                "number": graph.prs[object_id]["pr_id"],
                "id": (
                    f"{(graph.prs[object_id].get('repo') or '').split('/')[-1]}"
                    f"#{graph.prs[object_id]['pr_id']}"
                ),
                "tickets": list(graph.prs[object_id].get("ticket_keys") or ()),
                "state": (
                    graph.prs[object_id]["state"]
                    if graph.prs[object_id].get("closed_ts") else "open"
                ),
                "depth": graph.depth.get(object_id, 0),
                "re_review_hours": round(result["hours_by_pr"].get(object_id, 0.0), 2),
                "rebases": result["rebases_by_pr"].get(object_id, 0),
                "hours": round(result["hours_by_pr"].get(object_id, 0.0), 2),
                "affected": object_id in result["hours_by_pr"],
            }
            for object_id in sorted(
                stacked,
                key=lambda oid: (
                    -result["hours_by_pr"].get(oid, 0.0),
                    -graph.depth.get(oid, 0),
                    oid,
                ),
            )
        ],
        "stacks_detail": [
            {
                "repo": graph.prs[component[-1]]["repo"] if component else None,
                "prs": [graph.prs[oid]["pr_id"] for oid in component],
                "depth": max((graph.depth.get(oid, 0) for oid in component), default=0),
            }
            for component in stacks
        ],
        "note": note,
    }

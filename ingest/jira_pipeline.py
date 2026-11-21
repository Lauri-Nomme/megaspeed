"""Jira DC payloads -> pseudonymised OCEL-ish event log.

The Jira half of :mod:`ingest.pipeline`. Raw fields are read for exactly three
purposes and then dropped:

1. **the ticket-key filter** (summary, description, labels) -- only the matched
   *keys* survive, and they are already the object ids;
2. **deterministic classification** of a summary / sprint goal into one label
   from ``taxonomy.SUMMARY_CLASS`` (keyword rules, no model, no paraphrase);
3. **deterministic projection** of the Jira schema: a status *stage* (the
   operator's workflow map, else Jira's status category), a resolution class, a
   sprint id, a point value, a worklog gap.

Everything else that leaves this module is a key, a pseudonym, a timestamp or an
enum label. There is no path that copies a summary, a description, a comment, a
status name or a sprint goal into an event or an object.

Objects (prd/03):

* ``EPIC:PROJ-1``  -- summary-class, state, ticket key
* ``STORY:PROJ-101`` -- epic link, points, summary-class, resolution state
* ``SPRINT:PROJ#3`` -- start / planned end / completion, goal-class, state

Events (prd/02 closed vocabulary): ``created``, ``status_changed``,
``assigned``, ``pointed``, ``sprint_added``, ``sprint_removed``, ``worklogged``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from . import taxonomy
from .config import Config
from .jira import sprint_id, user_of
from .pipeline import SYSTEM_ACTOR, _digest, classify_actor
from .pseudonym import IdentityTable
from .timeutil import to_naive_utc

#: Changelog field (lower-cased) -> the token that may appear in an event's
#: natural key. Only these fields become events; every other field ("summary",
#: "description", "labels", "Epic Child", worklog bookkeeping, "Rank", ...) is
#: read and dropped. The token set is closed, so no raw field name can leak into
#: a key (and a key may never contain whitespace).
FIELD_TOKENS = {
    "status": "status",
    "assignee": "assignee",
    "sprint": "sprint",
    "story points": "points",
}

_SUMMARY_RULES = [
    (re.compile(r"\b(fix|bug|broken|race|crash|regress|fail|reject|prevent|guard)\b", re.I), "fix"),
    (re.compile(r"\b(spike|investigate|research|explore|prototype)\b", re.I), "spike"),
    (re.compile(r"\b(refactor|cleanup|clean up|rename|extract|simplify)\b", re.I), "refactor"),
    (re.compile(r"\b(tests?|cover|coverage)\b", re.I), "test"),
    (re.compile(r"\b(docs?|document|readme|runbook)\b", re.I), "docs"),
    (re.compile(r"\b(chore|bump|upgrade|rotate|tidy|pin)\b", re.I), "chore"),
    (re.compile(r"\b(add|implement|support|introduce|render|show|expose|paginate)\b", re.I), "feature"),
]

_RESOLUTION_RULES = [
    (re.compile(r"\b(duplicate|dup)\b", re.I), "duplicate"),
    (re.compile(r"\b(won'?t|will not|declined|rejected|cannot reproduce|incomplete)\b", re.I), "wont_do"),
    (re.compile(r"\b(done|fixed|complete|completed|resolved|delivered)\b", re.I), "done"),
]

_STATUS_BY_NAME = [
    # prd/29: the install's real workflow names. Longest/most specific first, so
    # "ready for refinement" is backlog while "refined" is ready, and
    # "in verification" is not read as "in review" by a prefix rule.
    (
        re.compile(
            r"^(ready for refinement|refinement|refining|backlog|to do|todo|open|new"
            r"|reopened)\b",
            re.I,
        ),
        "backlog",
    ),
    (
        re.compile(r"^(refined|ready|triaged|groomed|sized|estimated|selected)\b", re.I),
        "ready",
    ),
    (
        re.compile(
            r"^(in verification|in qa|qa|verifying|verification|testing|in test)\b",
            re.I,
        ),
        "in_verification",
    ),
    (
        re.compile(r"^(in review|in code review|code review|review|under review|pr review)\b", re.I),
        "in_review",
    ),
    (
        re.compile(r"^(in progress|in development|in dev|development|doing|implementing)\b", re.I),
        "in_progress",
    ),
    (re.compile(r"^(done|closed|resolved|released|verified|completed)\b", re.I), "done"),
]

#: Jira's own status *category* -> stage. This is the deterministic projection
#: (prd/02) and the reason an unconfigured store keeps today's numbers: review
#: and verification statuses are category ``indeterminate``, so without a config
#: map they stay ``in_progress`` (prd/29 §3).
_STAGE_BY_CATEGORY = {"new": "backlog", "indeterminate": "in_progress", "done": "done"}

#: Story/bug/task issue types become a STORY object; an epic becomes an EPIC.
STORY_TYPES = frozenset({"story", "bug", "task", "improvement", "new feature", "technical task"})


def classify_summary(text: Any) -> str:
    """Raw summary (or sprint goal) -> one label. The text itself is never kept."""
    value = str(text or "")
    for pattern, label in _SUMMARY_RULES:
        if pattern.search(value):
            return label
    return "other"


def resolution_class(name: Any) -> str | None:
    """Resolution name -> closed label, or ``None`` for "unresolved"."""
    value = str(name or "").strip()
    if not value:
        return None
    for pattern, label in _RESOLUTION_RULES:
        if pattern.search(value):
            return label
    return "other"


def status_stage(
    status_id: Any,
    name: Any,
    categories: dict[str, str],
    stage_map: dict[str, str] | None = None,
) -> str:
    """Status id (or name) -> one stage from ``taxonomy.STATUS_STAGE`` (prd/29).

    Precedence, most specific first:

    1. ``stage_map`` -- the operator's ``[jira.stages]`` / ``MEGASPEED_JIRA_STAGES``
       mapping, keyed by the normalised status *name*. This is what lets a real
       install distinguish review and QA;
    2. Jira's own status *category* (``new`` / ``indeterminate`` / ``done``),
       the deterministic projection prd/02 asks for -- and the reason an
       unconfigured store is unchanged: review/QA statuses are category
       ``indeterminate`` and stay ``in_progress``;
    3. name keyword rules, for a status id the categories map does not know.

    An unknown name is ``other`` -- never the name itself.
    """
    value = str(name or "").strip()
    if stage_map and value:
        mapped = stage_map.get(value.lower())
        if mapped is not None:
            return taxonomy.normalize_stage(mapped)
    key = str(status_id or "").strip()
    category = categories.get(key)
    if category:
        return _STAGE_BY_CATEGORY.get(category, "other")
    for pattern, stage in _STATUS_BY_NAME:
        if pattern.match(value):
            return stage
    return "other"


def points_value(raw: Any) -> tuple[int | None, bool]:
    """Story points -> ``(whole points, rounded?)``.

    Jira's Story Points field is a float. The store carries whole points (a
    COUNT), so a fractional estimate is rounded half-up and the scrape report
    counts it -- the value is never silently truncated to zero.
    """
    if raw in (None, "", []):
        return None, False
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return None, False
    if not value.is_finite():
        return None, False
    rounded = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(rounded), rounded != value


def log_delay_bucket(delay_seconds: int, started: datetime, created: datetime) -> str:
    """The worklog-noise bucket for a ``created - started`` gap (prd/04)."""
    if created.date() == started.date():
        return "same_day"
    if delay_seconds < 2 * 86_400:
        return "next_day"
    if delay_seconds <= 7 * 86_400:
        return "within_week"
    return "late"


def _ts(value: Any, default: datetime | None = None) -> datetime:
    if value in (None, "", 0):
        if default is None:
            raise ValueError("timestamp required")
        return default
    return to_naive_utc(value)


def _object(object_type: str, object_id: str, **kwargs) -> dict:
    record = {
        "object_type": object_type,
        "object_id": object_id,
        "repo": None,
        "pr_id": None,
        "ticket_keys": [],
        "actor": None,
        "state": None,
        "created_at": None,
        "closed_at": None,
        "head_commit": None,
        "message_class": None,
        "attributes": {},
    }
    record.update(kwargs)
    return record


@dataclass
class JiraStats:
    issues_seen: int = 0
    issues_kept: int = 0
    issues_skipped: int = 0
    #: Issues whose fetch failed (404 / moved / permission / 5xx past the
    #: transport retries). Skipped and counted, never fatal; not recorded in the
    #: per-issue cursor, so the next run retries them (FR-2).
    issues_failed: int = 0
    #: Issues skipped because an interrupted run already checkpointed them.
    issues_skipped_resumed: int = 0
    #: Issue types outside the closed set (sub-tasks, custom types): dropped.
    issues_skipped_type: int = 0
    epics: int = 0
    stories: int = 0
    sprints: int = 0
    worklogs: int = 0
    transitions: int = 0
    assigned_changes: int = 0
    points_changes: int = 0
    sprint_changes: int = 0
    sprint_at_start: int = 0
    changelog_items: int = 0
    changelog_ignored: int = 0
    #: Fractional story points that were rounded to whole points.
    points_rounded: int = 0
    #: Worklog gaps bucketed in this run (the worklog-noise signal).
    log_delays: dict[str, int] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    people: set[str] = field(default_factory=set)
    activities: dict[str, int] = field(default_factory=dict)
    watermark: datetime | None = None

    def counts(self) -> dict:
        return {
            "events": len(self.events),
            "objects": len(self.objects),
            "epics": self.epics,
            "stories": self.stories,
            "sprints": self.sprints,
            "worklogs": self.worklogs,
            "transitions": self.transitions,
            "people": len(self.people),
            "issues_seen": self.issues_seen,
            "issues_kept": self.issues_kept,
            "issues_skipped": self.issues_skipped,
            "issues_failed": self.issues_failed,
        }


class JiraReducer:
    """One issue in, pseudonymised events out."""

    def __init__(
        self,
        cfg: Config,
        ident: IdentityTable,
        as_of: datetime,
        *,
        epic_link_field: str = "",
        sprint_field: str = "",
        story_points_field: str = "",
        status_categories: dict[str, str] | None = None,
        stage_map: dict[str, str] | None = None,
    ):
        self.cfg = cfg
        self.ident = ident
        self.as_of = as_of
        self.epic_link_field = epic_link_field
        self.sprint_field = sprint_field
        self.story_points_field = story_points_field
        self.status_categories = dict(status_categories or {})
        #: prd/29: normalised workflow status name -> stage, from
        #: ``[jira.stages]`` / ``MEGASPEED_JIRA_STAGES``. Empty = the coarse
        #: category projection (today's numbers).
        self.stage_map = {
            str(name).strip().lower(): taxonomy.normalize_stage(stage)
            for name, stage in (stage_map or {}).items()
            if str(name).strip()
        }
        self.actor_kinds: dict[str, str] = {SYSTEM_ACTOR: "bot"}
        #: Object ids that exist in this bundle, so an event never references a
        #: dangling object (an epic outside the scrape scope, an unknown sprint).
        self.known: set[str] = set()
        #: (issue key, sprint id) pairs the changelog already reported, so the
        #: membership fallback only fills the gaps.
        self._sprint_events: set[tuple[str, int]] = set()
        #: sprint id -> start date, for the at-start membership fallback.
        self.sprint_starts: dict[int, datetime] = {}
        #: sprint name -> id, so a changelog item that only names the sprint can
        #: still be resolved to a Sprint object.
        self.sprint_ids_by_name: dict[str, int] = {}

    # -- helpers ---------------------------------------------------------
    def _actor_kind(self, user: dict | None) -> str:
        """Deterministic ``bot`` | ``human`` (prd/02: known account set)."""
        if not user:
            return "bot"
        record = {
            "name": user.get("name") or "",
            "type": "SERVICE" if str(user.get("accountType") or "").lower() == "service" else "NORMAL",
        }
        return classify_actor(record, self.cfg.bot_slugs)

    def _actor(self, user: dict | None, stats: JiraStats) -> str:
        if not user:
            pseudonym = SYSTEM_ACTOR
        else:
            pseudonym = self.ident.pseudonym_jira(user)
        self.actor_kinds.setdefault(pseudonym, self._actor_kind(user))
        stats.people.add(pseudonym)
        # The collector emits one PERSON object per actor seen in this run.
        self.known.add(f"PERSON:{pseudonym}")
        return pseudonym

    def actor_kind(self, pseudonym: str) -> str:
        return self.actor_kinds.get(pseudonym, "human")

    def _event(
        self,
        stats: JiraStats,
        issue_key: str,
        natural: str,
        ts: datetime,
        activity: str,
        actor: str,
        objects: list[str],
        attributes: dict[str, Any],
        raw: Any,
    ) -> None:
        attributes = dict(attributes)
        attributes.setdefault("actor_kind", self.actor_kind(actor))
        attributes = taxonomy.check_attributes(attributes)
        key = f"{issue_key}:{natural}"
        event = {
            "event_key": key,
            "ts": ts,
            "activity": taxonomy.check_activity(activity),
            "actor": actor,
            "repo": None,
            "pr_id": None,
            "attributes": attributes,
            "classifier_version": taxonomy.CLASSIFIER_VERSION,
            "content_hash": _digest(raw),
            "as_of": self.as_of,
        }
        stats.events.append(event)
        stats.activities[activity] = stats.activities.get(activity, 0) + 1
        for object_id in objects:
            # Only objects this bundle actually emits: a dangling reference is a
            # contract violation (store.verify), not a tolerable dangling edge.
            if object_id in self.known:
                stats.relations.append((key, object_id.split(":", 1)[0], object_id))

    # -- registration (pass 1) -------------------------------------------
    def prime(self, issues: list[dict], sprints: list[tuple[str, dict]]) -> None:
        """Register every object id this bundle will emit, before reducing."""
        for project, sprint in sprints:
            self.known.add(f"SPRINT:{project}#{int(sprint['id'])}")
            start = sprint.get("startDate")
            if start:
                self.sprint_starts[int(sprint["id"])] = _ts(start)
            name = str(sprint.get("name") or "").strip()
            if name:
                self.sprint_ids_by_name[name] = int(sprint["id"])
        for issue in issues:
            itype = _issue_type(issue)
            key = str(issue.get("key") or "")
            if not key:
                continue
            if itype == "epic":
                self.known.add(f"EPIC:{key}")
            elif itype in STORY_TYPES:
                self.known.add(f"STORY:{key}")

    # -- sprints (objects only) ------------------------------------------
    def reduce_sprint(self, sprint: dict, project: str, stats: JiraStats) -> None:
        """A Sprint is an object, not an event: its lifecycle is in its fields.

        start -> ``created_at``, planned end -> ``end_date``, actual completion
        -> ``closed_at`` (prd/03: "id, start, end, complete, goal-class").
        """
        sid = int(sprint["id"])
        start = _ts(sprint.get("startDate")) if sprint.get("startDate") else None
        end = _ts(sprint.get("endDate")) if sprint.get("endDate") else None
        complete = _ts(sprint.get("completeDate")) if sprint.get("completeDate") else None
        state = str(sprint.get("state") or "").strip().lower()
        attributes: dict[str, Any] = {"goal_class": classify_summary(sprint.get("goal"))}
        if end is not None:
            attributes["end_date"] = end.strftime("%Y-%m-%d")
        stats.objects.append(
            _object(
                "SPRINT",
                f"SPRINT:{project}#{sid}",
                state=state if state in taxonomy.SPRINT_STATE else "future",
                created_at=start,
                closed_at=complete,
                attributes=attributes,
            )
        )
        stats.sprints += 1

    # -- issues (pass 2) -------------------------------------------------
    def reduce_issue(
        self,
        issue: dict,
        *,
        project: str,
        memberships: dict[str, set[int]] | None = None,
        stats: JiraStats,
    ) -> None:
        itype = _issue_type(issue)
        if itype == "epic":
            self._reduce_epic(issue, stats)
        elif itype in STORY_TYPES:
            self._reduce_story(
                issue, project=project, memberships=memberships, stats=stats
            )
        else:
            stats.issues_skipped_type += 1

    def _reduce_epic(self, issue: dict, stats: JiraStats) -> None:
        key = str(issue["key"])
        fields = issue.get("fields") or {}
        epic_id = f"EPIC:{key}"
        reporter = _user(fields, "reporter", "creator", "assignee")
        assignee = _user(fields, "assignee")
        created = _ts(fields.get("created"), self.as_of)
        resolution = (fields.get("resolution") or {}).get("name")
        state = resolution_class(resolution) or "open"
        owner = self._actor(assignee or reporter, stats)
        stats.objects.append(
            _object(
                "EPIC",
                epic_id,
                ticket_keys=[key],
                actor=owner,
                state=state,
                created_at=created,
                closed_at=_ts(fields["resolutiondate"]) if fields.get("resolutiondate") else None,
                attributes={
                    "summary_class": classify_summary(fields.get("summary")),
                    "actor_kind": self.actor_kind(owner),
                },
            )
        )
        stats.epics += 1
        self._event(
            stats, key, "created", created, "created", self._actor(reporter, stats),
            [epic_id], {}, {"kind": "epic"},
        )
        self._walk_changelog(issue, epic_id, stats, allow_sprint=False)

    def _reduce_story(
        self,
        issue: dict,
        *,
        project: str,
        memberships: dict[str, set[int]] | None,
        stats: JiraStats,
    ) -> None:
        key = str(issue["key"])
        fields = issue.get("fields") or {}
        story_id = f"STORY:{key}"
        reporter = _user(fields, "reporter", "creator", "assignee")
        assignee = _user(fields, "assignee")
        created = _ts(fields.get("created"), self.as_of)
        resolution = (fields.get("resolution") or {}).get("name")
        state = resolution_class(resolution) or "open"
        epic_key = _epic_link(fields.get(self.epic_link_field)) if self.epic_link_field else None
        points, rounded = points_value(fields.get(self.story_points_field)) if self.story_points_field else (None, False)
        if rounded:
            stats.points_rounded += 1

        attributes: dict[str, Any] = {"summary_class": classify_summary(fields.get("summary"))}
        if epic_key:
            attributes["epic_key"] = epic_key
        if points is not None:
            attributes["points"] = points
        stats.objects.append(
            _object(
                "STORY",
                story_id,
                ticket_keys=[key],
                actor=self._actor(assignee or reporter, stats),
                state=state,
                created_at=created,
                closed_at=_ts(fields["resolutiondate"]) if fields.get("resolutiondate") else None,
                attributes=attributes,
            )
        )
        stats.stories += 1

        objects = [story_id]
        if epic_key and f"EPIC:{epic_key}" in self.known:
            objects.append(f"EPIC:{epic_key}")
        self._event(
            stats, key, "created", created, "created", self._actor(reporter, stats),
            objects, {}, {"kind": "story"},
        )

        self._walk_changelog(issue, story_id, stats, allow_sprint=True, project=project)
        # prd/04/66: the ticket's own Sprint field is the membership Jira already
        # returned; the per-sprint board walk is only a fallback (``api`` mode).
        current_sprints = (
            memberships.get(key) if memberships is not None
            else _current_sprints(fields, self.sprint_field)
        )
        self._at_start_membership(
            key, story_id, sprints=current_sprints, project=project, stats=stats
        )
        self._worklogs(issue, story_id, stats)

    # -- changelog -------------------------------------------------------
    def _walk_changelog(
        self,
        issue: dict,
        object_id: str,
        stats: JiraStats,
        *,
        allow_sprint: bool,
        project: str = "",
    ) -> None:
        key = str(issue["key"])
        histories = (issue.get("changelog") or {}).get("histories") or []
        counter = 0
        for history in sorted(
            histories,
            key=lambda h: (str(h.get("created") or ""), str(h.get("id") or "")),
        ):
            counter += 1
            try:
                ts = _ts(history.get("created"))
            except ValueError:
                continue
            author = self._actor(user_of(history.get("author")), stats)
            hid = str(history.get("id") or "").strip()
            if not hid.isdigit():
                hid = f"h{counter}"
            for index, item in enumerate(history.get("items") or []):
                stats.changelog_items += 1
                token = self._field_token(item)
                if token is None or (token == "sprint" and not allow_sprint):
                    stats.changelog_ignored += 1
                    continue
                natural = f"changelog:{hid}:{token}:{index}"
                if token == "status":
                    self._status_change(stats, key, object_id, natural, ts, author, item)
                elif token == "assignee":
                    self._assignee_change(stats, key, object_id, natural, ts, author, item)
                elif token == "points":
                    self._points_change(stats, key, object_id, natural, ts, author, item)
                elif token == "sprint":
                    self._sprint_change(stats, key, object_id, natural, ts, author, item, project)

    def _field_token(self, item: dict) -> str | None:
        """Which closed token this changelog item is about (``None`` = dropped)."""
        field = str(item.get("field") or "").strip().lower()
        if field in FIELD_TOKENS:
            return FIELD_TOKENS[field]
        if self.story_points_field and str(item.get("fieldId") or "") == self.story_points_field:
            return "points"
        return None

    def _status_change(self, stats, key, object_id, natural, ts, actor, item) -> None:
        before = status_stage(
            item.get("from"), item.get("fromString"), self.status_categories, self.stage_map
        )
        after = status_stage(
            item.get("to"), item.get("toString"), self.status_categories, self.stage_map
        )
        if before == after:
            # A no-op status write (Jira emits these on bulk edits), or a move
            # between two statuses of the same stage ("OPEN" -> "READY FOR
            # REFINEMENT" are both backlog): not a stage transition, and
            # definitely not an event.
            stats.changelog_ignored += 1
            return
        stats.transitions += 1
        self._event(
            stats, key, natural, ts, "status_changed", actor, [object_id],
            {"from_status": before, "to_status": after}, item,
        )

    def _assignee_change(self, stats, key, object_id, natural, ts, actor, item) -> None:
        new_user = user_of({"name": item.get("to"), "displayName": item.get("toString")})
        old_user = user_of({"name": item.get("from"), "displayName": item.get("fromString")})
        stats.assigned_changes += 1
        if new_user.get("name") or new_user.get("displayName"):
            person = self._actor(new_user, stats)
            self._event(
                stats, key, natural, ts, "assigned", actor,
                [object_id, f"PERSON:{person}"], {"change": "added"}, item,
            )
            return
        person = self._actor(old_user, stats) if (old_user.get("name") or old_user.get("displayName")) else None
        objects = [object_id] + ([f"PERSON:{person}"] if person else [])
        self._event(stats, key, natural, ts, "assigned", actor, objects, {"change": "removed"}, item)

    def _points_change(self, stats, key, object_id, natural, ts, actor, item) -> None:
        value, rounded = points_value(item.get("toString"))
        if rounded:
            stats.points_rounded += 1
        stats.points_changes += 1
        # A cleared estimate is 0 points, not "unknown": the event says the
        # ticket was re-pointed, and 0 is a legitimate value.
        self._event(
            stats, key, natural, ts, "pointed", actor, [object_id],
            {"points": value if value is not None else 0}, item,
        )

    def _sprint_change(self, stats, key, object_id, natural, ts, actor, item, project) -> None:
        before = self._sprint_ref(item.get("from"), item.get("fromString"))
        after = self._sprint_ref(item.get("to"), item.get("toString"))
        stats.sprint_changes += 1
        if before is not None and before != after:
            self._sprint_event(stats, key, object_id, f"{natural}:removed", ts, actor,
                               "sprint_removed", before, item, project, at_start=False)
        if after is not None:
            self._sprint_event(stats, key, object_id, f"{natural}:added", ts, actor,
                               "sprint_added", after, item, project, at_start=False)

    def _sprint_ref(self, raw: Any, name: Any) -> int | None:
        """A sprint reference in a changelog item: id first, then the name."""
        found = sprint_id(raw)
        if found is not None:
            return found
        text = str(name or "").strip()
        return self.sprint_ids_by_name.get(text)

    def _sprint_event(self, stats, key, object_id, natural, ts, actor, activity,
                      sid, raw, project, *, at_start: bool) -> None:
        self._sprint_events.add((key, sid))
        attributes: dict[str, Any] = {"sprint_id": sid}
        if at_start:
            attributes["at_start"] = True
        objects = [object_id]
        sprint_object = f"SPRINT:{project}#{sid}"
        if sprint_object in self.known:
            objects.append(sprint_object)
        self._event(stats, key, natural, ts, activity, actor, objects, attributes, raw)
        if at_start:
            stats.sprint_at_start += 1

    def _at_start_membership(self, key, object_id, *, sprints, project, stats) -> None:
        """Membership Jira never recorded as a change = in the sprint from its start.

        prd/04 asks for "membership-at-start vs added-later": an issue that is in
        a sprint and has no ``Sprint`` changelog item was in it when the sprint
        started, which is exactly the carry-over signal. It is emitted with the
        synthetic ``system`` actor at the sprint's start date. ``sprints`` is the
        ticket's own Sprint field in the default mode (no board walk).
        """
        for sid in sorted(sprints or ()):
            if (key, sid) in self._sprint_events:
                continue
            start = self.sprint_starts.get(sid)
            if start is None:
                continue
            self._sprint_event(
                stats, key, object_id, f"sprint:{sid}:start", start, SYSTEM_ACTOR,
                "sprint_added", sid, {"kind": "membership-at-start"}, project, at_start=True,
            )

    # -- worklogs --------------------------------------------------------
    def _worklogs(self, issue: dict, object_id: str, stats: JiraStats) -> None:
        key = str(issue["key"])
        block = (issue.get("fields") or {}).get("worklog") or {}
        for index, worklog in enumerate(block.get("worklogs") or [], start=1):
            author = user_of(worklog.get("author") or worklog.get("updateAuthor"))
            try:
                started = _ts(worklog.get("started"))
                created = _ts(worklog.get("created"), started)
            except ValueError:
                continue
            seconds = worklog.get("timeSpentSeconds")
            if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
                continue
            delay = max(0, int((created - started).total_seconds()))
            bucket = log_delay_bucket(delay, started, created)
            stats.worklogs += 1
            stats.log_delays[bucket] = stats.log_delays.get(bucket, 0) + 1
            actor = self._actor(author, stats)
            wid = str(worklog.get("id") or "").strip() or f"n{index}"
            self._event(
                stats, key, f"worklog:{wid}", created, "worklogged", actor,
                [object_id, f"PERSON:{actor}"],
                {
                    "time_spent_seconds": seconds,
                    "log_delay_seconds": delay,
                    "log_delay_bucket": bucket,
                },
                {"id": wid, "started": started, "created": created},
            )


def _issue_type(issue: dict) -> str:
    return str(((issue.get("fields") or {}).get("issuetype") or {}).get("name") or "").strip().lower()


def _current_sprints(fields: dict, sprint_field: str) -> set[int]:
    """The sprint ids in a ticket's own Sprint field (all shapes Jira returns).

    This is the membership the ticket already carries; the per-sprint
    ``/sprint/{id}/issue`` walk is not needed to know it (prd/04/66).
    """
    raw = (fields or {}).get(sprint_field) if sprint_field else None
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: set[int] = set()
    for item in items:
        sid = sprint_id(item)
        if sid is not None:
            out.add(int(sid))
    return out


def _user(fields: dict, *names: str) -> dict:
    """The first present user record among ``names`` (normalised for identity)."""
    for name in names:
        record = fields.get(name)
        if record:
            return user_of(record)
    return {}


def _epic_link(value: Any) -> str | None:
    """The epic key out of the Epic Link field (a key string, or a linked issue)."""
    if value in (None, "", [], {}):
        return None
    if isinstance(value, dict):
        candidate = value.get("key") or (value.get("fields") or {}).get("summary")
        return str(candidate) if candidate and taxonomy.KEY_TOKEN.fullmatch(str(candidate)) else None
    text = str(value).strip()
    return text if taxonomy.KEY_TOKEN.fullmatch(text) else None


__all__ = [
    "FIELD_TOKENS",
    "JiraReducer",
    "JiraStats",
    "STORY_TYPES",
    "classify_summary",
    "log_delay_bucket",
    "points_value",
    "resolution_class",
    "status_stage",
]

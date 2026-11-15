"""Bitbucket DC payloads -> pseudonymised OCEL-ish event log.

The reduction happens entirely in memory. Raw fields are read for exactly two
purposes and then dropped:

1. **the ticket-key filter** (branch, PR title, PR description, commit
   messages) — only the matched *keys* survive;
2. **deterministic classification** of a commit message into one label from
   ``taxonomy.MESSAGE_CLASS`` (keyword rules, no model, no summary).

Everything else that leaves this module is a key, a pseudonym, a timestamp or
an enum label.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import taxonomy
from .bitbucket import PullRequest
from .config import Config
from .pseudonym import IdentityTable
from .timeutil import to_naive_utc

SYSTEM_ACTOR = "system"

_MESSAGE_RULES = [
    (re.compile(r"^revert\b", re.I), "revert"),
    (re.compile(r"^merge\b", re.I), "merge"),
    (re.compile(r"\bwip\b", re.I), "wip"),
    (re.compile(r"\bfix(es|ed)?\b|\bbug\b", re.I), "fix"),
    (re.compile(r"\btest(s|ed|ing)?\b|\bcover\b", re.I), "test"),
    (re.compile(r"\brefactor\b|\bcleanup\b|\brename\b", re.I), "refactor"),
    (re.compile(r"\bdocs?\b|\breadme\b", re.I), "docs"),
    (re.compile(r"\bchore\b|\btidy\b|\bbump\b", re.I), "chore"),
    (re.compile(r"\badd(s|ed)?\b|\bfeature\b|\bimplement", re.I), "feature"),
]


def classify_message(message: str) -> str:
    """Raw commit message -> one label. The message itself is never kept."""
    first_line = (message or "").strip().splitlines()[0] if message else ""
    for pattern, label in _MESSAGE_RULES:
        if pattern.search(first_line):
            return label
    return "other"


def classify_actor(user: dict | None, bot_slugs: tuple[str, ...] | set[str]) -> str:
    """Deterministic ``bot`` | ``human`` for a DC user record (prd/02).

    DC marks service accounts with ``"type": "SERVICE"``; CI/build accounts that
    look like ordinary users are caught by the configured slug list and by
    service-shaped slugs. A missing user is the synthetic ``system`` actor, which
    is never a person.
    """
    if not user:
        return "bot"
    if str(user.get("type") or "").strip().upper() == "SERVICE":
        return "bot"
    slug = str(user.get("name") or user.get("slug") or "").strip().lower()
    if not slug:
        return "human"
    known = {str(s).strip().lower() for s in bot_slugs if str(s).strip()}
    if slug in known or "system-user" in slug:
        return "bot"
    if slug.endswith(("-bot", ".bot", "_bot", "[bot]")) or slug.startswith(
        ("bot-", "bot.", "ci-", "ci.", "build-", "svc-", "service-")
    ):
        return "bot"
    return "human"


def _digest(payload: Any) -> str:
    return hashlib.blake2b(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode(),
        digest_size=8,
    ).hexdigest()


def _ts(value: Any, default: datetime | None = None) -> datetime:
    if value in (None, "", 0):
        if default is None:
            raise ValueError("timestamp required")
        return default
    return to_naive_utc(value)


@dataclass
class KeyFilter:
    pattern: re.Pattern[str]
    shape: re.Pattern[str]
    projects: tuple[str, ...]

    @classmethod
    def build(cls, pattern: str, shape: str, projects: tuple[str, ...] | list[str]) -> "KeyFilter":
        projects = tuple(str(p).strip().upper() for p in projects if str(p).strip())
        if not projects:
            raise SystemExit(
                "no ticket project prefixes configured: set MEGASPEED_TICKET_PROJECTS=DEL,FIX"
            )
        try:
            return cls(re.compile(pattern), re.compile(shape), projects)
        except re.error as exc:
            raise SystemExit(f"bad ticket_regex/key_shape: {exc}") from exc

    def accepts(self, candidate: str) -> bool:
        """A candidate counts only if it is key-shaped *and* a configured project.

        The project list is what stops ``UTF-8`` / ``SHA-256`` / ``ISO-8601``
        from becoming tickets: they are perfectly key-shaped, and only the
        prefix tells them apart from a real key.
        """
        return bool(self.shape.fullmatch(candidate)) and candidate.partition("-")[0] in self.projects

    def keys(self, *texts: str | None) -> list[str]:
        found: set[str] = set()
        for text in texts:
            if not text:
                continue
            for match in self.pattern.finditer(text):
                candidate = match.group(0).strip()
                # Second gate: a permissive regex must not be able to smuggle
                # prose into the keys column; third gate: the prefix must be a
                # configured project.
                if self.accepts(candidate):
                    found.add(candidate)
        return sorted(found)


@dataclass
class Stats:
    prs_seen: int = 0
    prs_kept: int = 0
    prs_dropped: int = 0
    # Watermark / settled bookkeeping (prd/14 §2,§3): closed PRs marked done,
    # closed PRs skipped as settled, and any other PR skipped as unchanged.
    prs_settled: int = 0
    prs_skipped_settled: int = 0
    prs_skipped_watermark: int = 0
    events: list[dict] = field(default_factory=list)
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    people: set[str] = field(default_factory=set)
    keys_by_placement: dict[str, int] = field(default_factory=dict)
    #: ``push_class`` -> count for the rescopes classified in this run (prd/16).
    push_classes: dict[str, int] = field(default_factory=dict)
    watermark: datetime | None = None
    activities_by_action: dict[str, int] = field(default_factory=dict)


class Reducer:
    """One PR in, pseudonymised events out."""

    def __init__(self, cfg: Config, ident: IdentityTable, key_filter: KeyFilter, as_of: datetime):
        self.cfg = cfg
        self.ident = ident
        self.key_filter = key_filter
        self.as_of = as_of
        self.sentinels = set(cfg.sentinel_slugs)
        self.bot_slugs = {str(s).strip().lower() for s in cfg.bot_slugs if str(s).strip()}
        self.actor_kinds: dict[str, str] = {}

    # -- helpers ---------------------------------------------------------
    def _actor_kind(self, user: dict | None) -> str:
        """Deterministic ``bot`` | ``human`` (prd/02: known account set)."""
        return classify_actor(user, self.cfg.bot_slugs)

    def _actor(self, user: dict | None, stats: Stats) -> str:
        if not user:
            pseudonym = SYSTEM_ACTOR
        else:
            pseudonym = self.ident.pseudonym(user)
        self.actor_kinds.setdefault(pseudonym, self._actor_kind(user))
        stats.people.add(pseudonym)
        return pseudonym

    def actor_kind(self, pseudonym: str) -> str:
        return self.actor_kinds.get(pseudonym, "human")

    def _is_sentinel(self, user: dict | None) -> bool:
        return bool(user) and str(user.get("name") or "") in self.sentinels

    def _event(
        self,
        stats: Stats,
        pr: PullRequest,
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
        key = f"{pr.project}/{pr.repo}#{pr.id}:{natural}"
        event = {
            "event_key": key,
            "ts": ts,
            "activity": taxonomy.check_activity(activity),
            "actor": actor,
            "repo": f"{pr.project}/{pr.repo}",
            "pr_id": pr.id,
            "attributes": attributes,
            "classifier_version": taxonomy.CLASSIFIER_VERSION,
            "content_hash": _digest(raw),
            "as_of": self.as_of,
        }
        stats.events.append(event)
        for oid in objects:
            stats.relations.append((key, oid.split(":", 1)[0], oid))

    # -- the reduction ---------------------------------------------------
    def reduce(self, pr: PullRequest, keys: list[str], activities: list[dict], stats: Stats) -> None:
        repo_label = f"{pr.project}/{pr.repo}"
        pr_object_id = f"PR:{repo_label}#{pr.id}"
        ticket_objects = [f"TICKET:{k}" for k in keys]

        commits = self._collect_commits(pr)
        head = (pr.pr.get("fromRef") or {}).get("latestCommit") or (commits[-1]["id"] if commits else None)
        author = self._actor((pr.pr.get("author") or {}).get("user") or {}, stats)
        created = _ts(pr.pr.get("createdDate"), self.as_of)
        closed_ts = _ts(pr.pr["closedDate"]) if pr.pr.get("closedDate") else None
        state = {"MERGED": "merged", "DECLINED": "declined"}.get(pr.pr.get("state"), "open")

        pr_record = _object(
            "PR",
            pr_object_id,
            repo=repo_label,
            pr_id=pr.id,
            ticket_keys=keys,
            actor=author,
            state=state,
            created_at=created,
            closed_at=closed_ts,
            head_commit=head,
        )
        merge_properties = (pr.pr.get("properties") or {}).get("mergeCommit") or {}
        if merge_properties.get("id"):
            pr_record["attributes"]["merge_commit"] = str(merge_properties["id"])
        stats.objects.append(pr_record)
        for key in keys:
            stats.objects.append(_object("TICKET", f"TICKET:{key}", ticket_keys=[key]))

        for commit in commits:
            cid = str(commit.get("id") or "")
            if not cid:
                continue
            stats.objects.append(
                _object(
                    "COMMIT",
                    f"COMMIT:{cid}",
                    repo=repo_label,
                    pr_id=pr.id,
                    ticket_keys=keys,
                    actor=self._actor(commit.get("author"), stats),
                    created_at=_ts(commit.get("authorTimestamp"), created),
                    head_commit=cid,
                    message_class=classify_message(commit.get("message") or ""),
                )
            )

        # ---- activities -> events --------------------------------------
        approvals: dict[str, dict] = {}
        pushed_since = False
        rounds = 0
        threads: dict[str, dict] = {}
        ordered = sorted(activities, key=lambda a: (a.get("createdDate") or 0, a.get("id") or 0))
        for index, activity in enumerate(ordered):
            action = str(activity.get("action") or "")
            stats.activities_by_action[action] = stats.activities_by_action.get(action, 0) + 1
            ts = _ts(activity.get("createdDate"), created)
            user = activity.get("user") or {}
            actor = self._actor(user, stats)
            natural = f"act:{activity.get('id') or index}"

            if action == "OPENED":
                self._event(stats, pr, natural, ts, "pr_opened", actor,
                            [pr_object_id, *ticket_objects], {}, activity)

            elif action == "RESCOPED":
                self._push(stats, pr, natural, ts, activity, actor,
                           pr_object_id, ticket_objects, rounds + 1)
                approvals.clear()
                pushed_since = True
                rounds += 1

            elif action == "COMMENTED":
                comment = activity.get("comment") or {}
                comment_action = activity.get("commentAction")
                if comment_action not in (None, "ADDED", "UPDATED"):
                    continue  # deleted comments are not dialogue
                severity = "mandatory" if str(comment.get("severity") or "").upper() == "BLOCKER" else "suggestion"
                # Real DC 9.x puts the anchor on the *activity* (``commentAnchor``),
                # not inside the comment; accept both shapes.
                anchor = activity.get("commentAnchor") or comment.get("anchor")
                anchor_class = "file" if anchor else "general"
                resolved = bool(comment.get("threadResolved")) or str(comment.get("state") or "OPEN").upper() != "OPEN"
                thread_objects: list[str] = []
                if comment.get("id") is not None:
                    thread_id = f"THREAD:{repo_label}#{pr.id}:{comment['id']}"
                    thread_objects.append(thread_id)
                    # A thread's state is cumulative: ADDED opens it, a later
                    # COMMENTED/UPDATED activity may resolve it. Keep the
                    # earliest timestamp and the latest resolution.
                    entry = threads.setdefault(thread_id, {"created": ts})
                    entry["severity"] = severity
                    entry["anchor_class"] = anchor_class
                    entry["resolved"] = resolved
                    entry["resolved_at"] = (
                        _ts(comment["resolvedDate"]) if resolved and comment.get("resolvedDate")
                        else (ts if resolved else None)
                    )
                    entry["resolver"] = (comment.get("resolver") or {}) if resolved else {}
                if comment_action == "UPDATED":
                    # Edits and thread-resolution updates change the ReviewThread
                    # object, not the dialogue log.
                    continue
                self._event(
                    stats, pr, natural, ts, "comment_added", actor,
                    [pr_object_id, *ticket_objects, *thread_objects],
                    {
                        "severity": severity,
                        "anchor_class": anchor_class,
                        "thread_state": "resolved" if resolved else "open",
                        "replies": len(comment.get("comments") or []),
                    },
                    activity,
                )

            elif action == "APPROVED":
                approvals[actor] = {"ts": ts, "user": user}
                self._event(stats, pr, natural, ts, "pr_approved", actor,
                            [pr_object_id, *ticket_objects, f"PERSON:{actor}"],
                            {"sentinel": self._is_sentinel(user)}, activity)
                pushed_since = False

            elif action == "UNAPPROVED":
                cause = "push" if pushed_since else "manual"
                approvals.pop(actor, None)
                self._event(stats, pr, natural, ts, "pr_unapproved", actor,
                            [pr_object_id, *ticket_objects], {"cause": cause}, activity)
                pushed_since = False

            elif action == "NEEDS_WORK":
                approvals.pop(actor, None)
                self._event(stats, pr, natural, ts, "pr_needs_work", actor,
                            [pr_object_id, *ticket_objects], {"severity": "mandatory"}, activity)

            elif action == "UPDATED":
                # DC 9.x: reviewer roster changes. Some versions/forges also
                # report a raw rescope through UPDATED instead of RESCOPED.
                changed = False
                for change, field_name in (("added", "addedReviewers"), ("removed", "removedReviewers")):
                    for offset, reviewer in enumerate(activity.get(field_name) or []):
                        changed = True
                        pseudonym = self._actor(reviewer, stats)
                        self._event(
                            stats, pr, f"{natural}:{change}{offset}", ts,
                            "reviewer_changed", actor,
                            [pr_object_id, *ticket_objects, f"PERSON:{pseudonym}"],
                            {"change": change}, activity,
                        )
                if not changed and self._has_rescope(activity):
                    self._push(stats, pr, natural, ts, activity, actor,
                               pr_object_id, ticket_objects, rounds + 1)
                    approvals.clear()
                    pushed_since = True
                    rounds += 1

            elif action == "REVIEWED":
                # A reviewer completed their review (DC: PUT .../review). The
                # closed vocabulary has no ``pr_reviewed``, so this is carried as
                # a reviewer-state change; the ReviewThread object (a later
                # slice) breaks it down per thread.
                self._event(stats, pr, natural, ts, "reviewer_changed", actor,
                            [pr_object_id, *ticket_objects, f"PERSON:{actor}"],
                            {"change": "reviewed"}, activity)

            elif action == "MERGED":
                commit = activity.get("commit") or {}
                merge_objects = [f"COMMIT:{commit['id']}"] if commit.get("id") else []
                if commit.get("id"):
                    pr_record["attributes"]["merge_commit"] = str(commit["id"])
                    stats.objects.append(
                        _object(
                            "COMMIT",
                            f"COMMIT:{commit['id']}",
                            repo=repo_label,
                            pr_id=pr.id,
                            ticket_keys=keys,
                            actor=self._actor(commit.get("committer") or commit.get("author"), stats),
                            created_at=_ts(commit.get("committerTimestamp") or commit.get("authorTimestamp"), ts),
                            head_commit=str(commit["id"]),
                            message_class=classify_message(commit.get("message") or ""),
                        )
                    )
                self._event(stats, pr, natural, ts, "pr_merged", actor,
                            [pr_object_id, *ticket_objects, *merge_objects],
                            {"review_rounds": rounds}, activity)

            elif action == "DECLINED":
                self._event(stats, pr, natural, ts, "pr_declined", actor,
                            [pr_object_id, *ticket_objects], {}, activity)

            # Unknown actions are ignored on purpose: the vocabulary is closed,
            # so an unknown label is a taxonomy task, never a free-text fallback.

        for thread_id, entry in sorted(threads.items()):
            resolver = entry.get("resolver") or {}
            stats.objects.append(
                _object(
                    "THREAD",
                    thread_id,
                    repo=repo_label,
                    pr_id=pr.id,
                    ticket_keys=keys,
                    actor=self._actor(resolver, stats) if resolver else None,
                    state="resolved" if entry.get("resolved") else "open",
                    created_at=entry.get("created"),
                    closed_at=entry.get("resolved_at"),
                    attributes={
                        "severity": entry["severity"],
                        "anchor_class": entry["anchor_class"],
                    },
                )
            )

    def _push(self, stats: Stats, pr: PullRequest, natural: str, ts: datetime, activity: dict,
              actor: str, pr_object_id: str, ticket_objects: list[str], push_index: int) -> None:
        """RESCOPED (or a rescope-shaped UPDATED) -> pr_pushed + PRRevision.

        ``approvals_lost`` is deliberately *not* computed here: whether the push
        cost an approval depends on events that may only arrive in a later
        ingest run, so it is derived from the stored event log after writing
        (see :func:`derive_approvals_lost`).
        """
        added = (activity.get("added") or {}).get("commits") or []
        removed = (activity.get("removed") or {}).get("commits") or []
        patch = activity.get("megaspeedPatchId") or {}
        push_class = patch.get("push_class") or "other"
        repo_label = f"{pr.project}/{pr.repo}"
        revision_id = f"PRREV:{repo_label}#{pr.id}:{push_index}"
        stats.objects.append(
            _object(
                "PRREV",
                revision_id,
                repo=repo_label,
                pr_id=pr.id,
                ticket_keys=[oid.partition(":")[2] for oid in ticket_objects],
                actor=actor,
                created_at=ts,
                head_commit=str(activity.get("fromHash") or ""),
                attributes={
                    "push_index": push_index,
                    "push_class": push_class,
                    "commits_added": len(added),
                    "commits_removed": len(removed),
                },
            )
        )
        self._event(
            stats, pr, natural, ts, "pr_pushed", actor,
            [
                pr_object_id,
                revision_id,
                *ticket_objects,
                *[f"COMMIT:{c['id']}" for c in added if c.get("id")],
            ],
            {
                "push_class": push_class,
                "push_index": push_index,
                "commits_added": len(added),
                "commits_removed": len(removed),
            },
            activity,
        )

    @staticmethod
    def _has_rescope(activity: dict) -> bool:
        """True when the activity reports a moved source/target head.

        DC's RESCOPED shape is ``previousFromHash -> fromHash`` (source) and
        ``previousToHash -> toHash`` (target). A reviewer-only UPDATED carries
        none of these.
        """
        previous_from = activity.get("previousFromHash")
        new_from = activity.get("fromHash")
        if previous_from and new_from:
            return previous_from != new_from
        previous_to = activity.get("previousToHash")
        new_to = activity.get("toHash")
        if previous_to and new_to:
            return previous_to != new_to
        return bool(new_from and new_to)

    # -- internals -------------------------------------------------------
    def _collect_commits(self, pr: PullRequest) -> list[dict]:
        seen: dict[str, dict] = {}
        for commit in pr.commits:
            if commit.get("id"):
                seen[str(commit["id"])] = commit
        for activity in pr.activities:
            if activity.get("action") != "RESCOPED":
                continue
            for bucket in ("added", "removed"):
                for commit in (activity.get(bucket) or {}).get("commits") or []:
                    if commit.get("id"):
                        seen.setdefault(str(commit["id"]), commit)
        return sorted(seen.values(), key=lambda c: (c.get("authorTimestamp") or 0, c.get("id") or ""))


# --------------------------------------------------------------------------- #
# derived attributes — recomputed over the *stored* log on every run
# --------------------------------------------------------------------------- #
# Events that end the window in which an approval can be attributed to a push.
# A later approval, another push, merge/decline, or new dialogue means the push
# did not itself cost an approval.
APPROVAL_LOSS_BOUNDARIES = frozenset(
    {
        "pr_approved",
        "pr_pushed",
        "pr_merged",
        "pr_declined",
        "pr_needs_work",
        "comment_added",
        "pr_opened",
    }
)

_ACTIVITY_ORDER = re.compile(r":act:(\d+)(?::(\w+?)(\d+))?$")


def _event_order(event_key: str) -> tuple[int, str, int]:
    """Tiebreak for events with the same timestamp.

    Natural keys embed the DC activity id (``…:act:387``; reviewer changes add
    ``:added0``), so ordering by it matches the order the activities happened in.
    """
    match = _ACTIVITY_ORDER.search(event_key or "")
    if not match:
        return (0, "", 0)
    return (int(match.group(1)), match.group(2) or "", int(match.group(3) or 0))


def derive_approvals_lost(events: list[dict]) -> dict[str, bool]:
    """``event_key -> did this push cost an approval?`` over one PR's events.

    Deterministic look-ahead in the closed vocabulary: an ``pr_unapproved``
    before any other boundary event means the push itself lost the approval.
    This is the signal behind the "pure rebase lost approvals" finding
    (prd/04). It is computed from whatever the store *now* knows, so a re-run
    after the UNAPPROVED arrives converges on the right answer.
    """
    ordered = sorted(events, key=lambda e: (e["ts"], _event_order(e["event_key"])))
    out: dict[str, bool] = {}
    for index, event in enumerate(ordered):
        if event["activity"] != "pr_pushed":
            continue
        lost = False
        for later in ordered[index + 1 :]:
            if later["activity"] == "pr_unapproved":
                lost = True
                break
            if later["activity"] in APPROVAL_LOSS_BOUNDARIES:
                break
        out[event["event_key"]] = lost
    return out


def fill_derived(con, events: list[dict]) -> None:
    """Set derived attributes on *this run's* events before they are written.

    The union of the stored log and the new events is the complete history for
    the touched PRs, so the derivation can happen once, in memory. Doing it
    before the write keeps re-runs true no-ops: the row a run writes is already
    the converged row, so an unchanged PR is not rewritten on every ingest.
    """
    scope = {(e["repo"], e["pr_id"]) for e in events}
    run_events: dict[tuple[str, int], dict[str, dict]] = {}
    for event in events:
        run_events.setdefault((event["repo"], event["pr_id"]), {})[event["event_key"]] = event
    for repo, pr_id in sorted(scope):
        rows = con.execute(
            "SELECT event_key, ts, activity, CAST(attributes AS VARCHAR) FROM events"
            " WHERE repo = ? AND pr_id = ? ORDER BY ts ASC, event_key ASC",
            [repo, pr_id],
        ).fetchall()
        union: dict[str, dict] = {
            key: {"event_key": key, "ts": ts, "activity": activity,
                  "attributes": json.loads(attrs or "{}")}
            for key, ts, activity, attrs in rows
        }
        union.update(run_events.get((repo, pr_id), {}))
        for key, lost in derive_approvals_lost(list(union.values())).items():
            event = run_events[(repo, pr_id)].get(key)
            if event is not None:
                event["attributes"]["approvals_lost"] = lost


def recompute_derived(con, scope: set[tuple[str, int]]) -> int:
    """Recompute derived attributes for the PRs touched by this run.

    Scope is the (repo, pr_id) pairs the run emitted events for -- their event
    history is complete in the store, so anything derived from look-ahead can be
    re-derived exactly. Returns the number of rows rewritten. Normally 0: the
    values were already converged by :func:`fill_derived` before the write; this
    is the safety net for rows the reducer did not re-emit.
    """
    rewritten = 0
    for repo, pr_id in sorted(scope):
        rows = con.execute(
            "SELECT event_key, ts, activity, CAST(attributes AS VARCHAR) FROM events"
            " WHERE repo = ? AND pr_id = ? ORDER BY ts ASC, event_key ASC",
            [repo, pr_id],
        ).fetchall()
        events = [
            {
                "event_key": key,
                "ts": ts,
                "activity": activity,
                "attributes": json.loads(attrs or "{}"),
            }
            for key, ts, activity, attrs in rows
        ]
        by_key = {event["event_key"]: event for event in events}
        for key, lost in derive_approvals_lost(events).items():
            event = by_key[key]
            if event["attributes"].get("approvals_lost") is lost:
                continue
            event["attributes"]["approvals_lost"] = lost
            con.execute(
                "UPDATE events SET attributes = CAST(? AS JSON) WHERE event_key = ?",
                [json.dumps(event["attributes"], separators=(",", ":"), sort_keys=True), key],
            )
            rewritten += 1
    return rewritten


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


def run(cfg: Config, transport, source: str, limit: int | None = None, **kwargs) -> dict:
    """Dev shortcut, kept as ``ingest.pipeline.run`` for existing callers.

    The two sides live in :mod:`ingest.scrape` (source -> tgz) and
    :mod:`ingest.importer` (tgz -> store); :mod:`ingest.live` runs both, in
    process, through a real bundle file. Production uses the split CLIs.

    The import is deliberately dynamic: a static ``from .live import ...``
    would make the ``megaspeed-scrape`` artifact (prd/13) pull in the store,
    the importer and DuckDB through this dev-only wrapper.
    """
    from importlib import import_module

    live = import_module(f"{__package__}.live")
    return live.run(cfg, transport, source, limit, **kwargs)

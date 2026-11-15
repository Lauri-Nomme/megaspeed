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
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import taxonomy
from .bitbucket import BitbucketClient, PullRequest
from .config import Config
from .pseudonym import IdentityTable
from .store import connect, ensure_schema, to_naive_utc, utcnow, verify, write_events, write_objects, write_run

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

    @classmethod
    def build(cls, pattern: str, shape: str) -> "KeyFilter":
        try:
            return cls(re.compile(pattern), re.compile(shape))
        except re.error as exc:
            raise SystemExit(f"bad ticket_regex/key_shape: {exc}") from exc

    def keys(self, *texts: str | None) -> list[str]:
        found: set[str] = set()
        for text in texts:
            if not text:
                continue
            for match in self.pattern.finditer(text):
                candidate = match.group(0).strip()
                # Second gate: a permissive regex must not be able to smuggle
                # prose into the keys column.
                if self.shape.fullmatch(candidate):
                    found.add(candidate)
        return sorted(found)


@dataclass
class Stats:
    prs_seen: int = 0
    prs_kept: int = 0
    prs_dropped: int = 0
    events: list[dict] = field(default_factory=list)
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    people: set[str] = field(default_factory=set)
    keys_by_placement: dict[str, int] = field(default_factory=dict)
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

    # -- helpers ---------------------------------------------------------
    def _actor(self, user: dict | None, stats: Stats) -> str:
        if not user:
            return SYSTEM_ACTOR
        pseudonym = self.ident.pseudonym(user)
        stats.people.add(pseudonym)
        return pseudonym

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

        stats.objects.append(
            {
                "object_type": "PR",
                "object_id": pr_object_id,
                "repo": repo_label,
                "pr_id": pr.id,
                "ticket_keys": keys,
                "actor": author,
                "state": state,
                "created_at": created,
                "closed_at": closed_ts,
                "head_commit": head,
                "message_class": None,
            }
        )
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
                self._push(stats, pr, natural, ts, activity, actor, ordered, index,
                           pr_object_id, ticket_objects)
                approvals.clear()
                pushed_since = True
                rounds += 1

            elif action == "COMMENTED":
                comment = activity.get("comment") or {}
                if activity.get("commentAction") not in (None, "ADDED"):
                    continue  # edits/deletes are not new dialogue
                severity = "mandatory" if str(comment.get("severity") or "").upper() == "BLOCKER" else "suggestion"
                # Real DC 9.x puts the anchor on the *activity* (``commentAnchor``),
                # not inside the comment; accept both shapes.
                anchor = activity.get("commentAnchor") or comment.get("anchor")
                resolved = bool(comment.get("threadResolved")) or str(comment.get("state") or "OPEN").upper() != "OPEN"
                self._event(
                    stats, pr, natural, ts, "comment_added", actor,
                    [pr_object_id, *ticket_objects],
                    {
                        "severity": severity,
                        "anchor_class": "file" if anchor else "general",
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
                    self._push(stats, pr, natural, ts, activity, actor, ordered, index,
                               pr_object_id, ticket_objects)
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

    def _push(self, stats: Stats, pr: PullRequest, natural: str, ts: datetime, activity: dict,
              actor: str, ordered: list[dict], index: int,
              pr_object_id: str, ticket_objects: list[str]) -> None:
        """RESCOPED (or a rescope-shaped UPDATED) -> pr_pushed."""
        added = (activity.get("added") or {}).get("commits") or []
        removed = (activity.get("removed") or {}).get("commits") or []
        patch = activity.get("megaspeedPatchId") or {}
        self._event(
            stats, pr, natural, ts, "pr_pushed", actor,
            [pr_object_id, *ticket_objects, *[f"COMMIT:{c['id']}" for c in added if c.get("id")]],
            {
                "push_class": patch.get("push_class") or "other",
                "commits_added": len(added),
                "commits_removed": len(removed),
                "approvals_lost": self._approvals_lost_after(ordered, index),
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

    @staticmethod
    def _approvals_lost_after(activities: list[dict], index: int) -> bool:
        """Did this push immediately cost approvals?

        Deterministic look-ahead: an UNAPPROVED before any new approval on the
        same push. This is the signal behind the "pure rebase lost approvals"
        finding in prd/04.
        """
        for activity in activities[index + 1 :]:
            action = activity.get("action")
            if action == "UNAPPROVED":
                return True
            if action in ("APPROVED", "RESCOPED", "MERGED", "DECLINED", "COMMENTED", "NEEDS_WORK", "OPENED"):
                return False
        return False


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
    }
    record.update(kwargs)
    return record


def run(cfg: Config, transport, source: str, limit: int | None = None) -> dict:
    """Ingest every configured repo into the pseudonymised store."""
    as_of = utcnow()
    run_id = f"{source}-{as_of.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    key_filter = KeyFilter.build(cfg.ticket_regex, cfg.key_shape)
    ident = IdentityTable(cfg.identity_path)
    client = BitbucketClient(transport, page_limit=cfg.page_limit, min_interval=0.0 if source == "fixture" else 0.2)
    reducer = Reducer(cfg, ident, key_filter, as_of)
    stats = Stats()

    stop = False
    for project, repo in cfg.repos:
        for pr in client.pull_requests(project, repo):
            stats.prs_seen += 1
            if limit is not None and stats.prs_seen > limit:
                stop = True
                break
            keys = reducer.key_filter.keys(
                (pr.pr.get("fromRef") or {}).get("displayId"),
                pr.pr.get("title"),
                pr.pr.get("description"),
                *[c.get("message") for c in pr.commits],
            )
            if not keys:
                stats.prs_dropped += 1
                continue
            stats.prs_kept += 1
            _note_placements(reducer.key_filter, pr, stats)
            # activities are fetched only for PRs that survived the filter
            pr.activities = list(client.iter_activities(project, repo, pr.id))
            reducer.reduce(pr, keys, pr.activities, stats)
            if pr.updated_ms:
                stamp = to_naive_utc(pr.updated_ms)
                stats.watermark = stamp if stats.watermark is None else max(stats.watermark, stamp)
        if stop:
            break

    for pseudonym in sorted(stats.people):
        stats.objects.append(_object("PERSON", f"PERSON:{pseudonym}", actor=pseudonym))

    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(cfg.db_path)
    try:
        ensure_schema(con)
        before = con.execute("SELECT count(*) FROM events").fetchone()[0]
        write_events(con, stats.events, stats.relations)
        write_objects(con, stats.objects)
        write_run(
            con,
            {
                "run_id": run_id,
                "source": source,
                "as_of": as_of,
                "source_watermark": stats.watermark,
                "classifier_version": taxonomy.CLASSIFIER_VERSION,
                "prs_seen": stats.prs_seen,
                "prs_kept": stats.prs_kept,
                "prs_dropped": stats.prs_dropped,
                "events_written": len(stats.events),
                "events_skipped": 0,
            },
        )
        after = con.execute("SELECT count(*) FROM events").fetchone()[0]
        report = verify(con)
    finally:
        con.close()
    ident.save()

    return {
        "run_id": run_id,
        "source": source,
        "as_of": as_of,
        "db": str(cfg.db_path),
        "identity_map": str(cfg.identity_path),
        "requests": client.requests,
        "prs_seen": stats.prs_seen,
        "prs_kept": stats.prs_kept,
        "prs_dropped": stats.prs_dropped,
        "events_emitted": len(stats.events),
        "events_new": int(after - before),
        "objects": len(stats.objects),
        "activities_by_action": dict(sorted(stats.activities_by_action.items())),
        "keys_by_placement": dict(sorted(stats.keys_by_placement.items())),
        "verified": report["ok"],
        "problems": report["problems"],
        "summary": report["summary"],
        "ticket_regex": cfg.ticket_regex,
    }


def _note_placements(key_filter: KeyFilter, pr: PullRequest, stats: Stats) -> None:
    placements = {
        "branch": (pr.pr.get("fromRef") or {}).get("displayId") or "",
        "title": pr.pr.get("title") or "",
        "description": pr.pr.get("description") or "",
        "commit": "\n".join(c.get("message") or "" for c in pr.commits),
    }
    for name, text in placements.items():
        if key_filter.keys(text):
            stats.keys_by_placement[name] = stats.keys_by_placement.get(name, 0) + 1

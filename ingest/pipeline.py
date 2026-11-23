"""Bitbucket DC payloads -> pseudonymised OCEL-ish event log.

The reduction happens entirely in memory. Raw fields are read for exactly three
purposes and then dropped:

1. **the ticket-key filter** (branch, PR title, PR description, commit
   messages) — only the matched *keys* survive;
2. **deterministic classification** of a commit message into one label from
   ``taxonomy.MESSAGE_CLASS`` (keyword rules, no model, no summary);
3. **text classification** of a comment body / PR description into
   ``comment_type``, ``thread_outcome`` and question events (prd/16): the raw
   text goes to the local classifier (:mod:`ingest.classify`) and only the
   labels come back. With no classifier configured, no text is read at all.

Everything else that leaves this module is a key, a pseudonym, a timestamp or
an enum label.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import citext, classify, taxonomy
from .bitbucket import PullRequest
from .config import Config
from .pseudonym import IdentityTable
from .timeutil import iso, to_naive_utc

SYSTEM_ACTOR = "system"
#: The synthetic CI actor (store ``ACTOR_RE`` allows ``CI``). ``build_status``
#: events are the commit's build, not a person: the ball-court treats it as a
#: flag, never a holder (concepts.md §2, prd/40 S4).
CI_ACTOR = "CI"

_CI_SUCCESS = frozenset({"successful", "success", "passed", "green", "ok"})
_CI_FAILED = frozenset({"failed", "failure", "red", "error"})
_CI_RUNNING = frozenset({"inprogress", "in_progress", "running", "pending", "queued"})


def build_result(state: str | None) -> str:
    """A forge's build state -> the closed ``taxonomy.BUILD_RESULT`` set."""
    value = str(state or "").strip().lower().replace("-", "_")
    if value in _CI_SUCCESS:
        return "success"
    if value in _CI_FAILED:
        return "failed"
    if value in _CI_RUNNING:
        return "in_progress"
    return "other"

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


def ref_digest(repo: str, ref: Any, pepper: bytes | str) -> str | None:
    """Keyed digest of a branch ref (prd/22), or ``None`` when absent.

    The stack DAG only ever asks "is this the same branch?", so equality is all
    we need -- and a digest is the only form prd/02 allows: a DC branch name is
    the ticket summary in slug form (``DEL-101-add-login-form-validation``), so
    storing it would smuggle ticket content into the store (``schema.sql`` has no
    branch column, and the object-attribute spec accepts only this digest shape).

    **The digest must be keyed.** A plain hash of ``repo|branch`` is reversible
    by dictionary: the repo is in the clear on the PR object, the ticket key is
    ``PR.ticket_keys``, and branch names are ``KEY-<slug of the summary>``. So
    this is ``HMAC-SHA256(pepper, "repo|branch")`` truncated to 128 bits, where
    the pepper is the per-tenant raw-zone secret of :mod:`ingest.pepper`. Without
    the pepper the digest cannot be reproduced or dictionary-recovered, and the
    same branch in two tenants gives unrelated digests (no cross-tenant
    correlation).

    The repo stays part of the message because a branch ref is repo-scoped: two
    repos' ``main`` must not look like the same branch, or a cross-repo
    "stack" would be invented. 32 hex chars (128 bits); there is deliberately no
    unkeyed mode -- callers that have no pepper emit no ref attributes rather
    than a reversible digest.
    """
    branch = str((ref or {}).get("displayId") or "").strip()
    if not branch:
        return None
    key = pepper.encode() if isinstance(pepper, str) else bytes(pepper or b"")
    if not key:
        raise ValueError("ref_digest needs a non-empty pepper (an unkeyed digest is reversible)")
    return hmac.new(key, f"{repo}|{branch}".encode(), hashlib.sha256).hexdigest()[:32]


def _line_count(value: Any) -> bool:
    """True for a non-negative int. ``bool`` is an ``int`` in python: exclude it."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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
    #: Explicit key set (prd/18). Non-empty => only these exact keys match and
    #: ``projects`` is ignored. (Not ``keys``: that is the extraction method.)
    key_set: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        pattern: str,
        shape: str,
        projects: tuple[str, ...] | list[str],
        keys: tuple[str, ...] | list[str] | frozenset[str] = (),
    ) -> "KeyFilter":
        projects = tuple(str(p).strip().upper() for p in projects if str(p).strip())
        explicit = frozenset(str(k).strip().upper() for k in keys if str(k).strip())
        if not projects and not explicit:
            raise SystemExit(
                "no ticket filter configured: set MEGASPEED_TICKET_KEYS=DEL-1234 "
                "or MEGASPEED_TICKET_PROJECTS=DEL,FIX"
            )
        try:
            return cls(re.compile(pattern), re.compile(shape), projects, explicit)
        except re.error as exc:
            raise SystemExit(f"bad ticket_regex/key_shape: {exc}") from exc

    @property
    def exact(self) -> bool:
        """True when scoped to an explicit key set rather than project prefixes."""
        return bool(self.key_set)

    def accepts(self, candidate: str) -> bool:
        """A candidate counts only if it is key-shaped *and* allowed by the scope.

        With an explicit key set (prd/18) only those exact keys count and the
        project-prefix list is ignored -- a shared project's prefix would match
        everyone's tickets. Otherwise the prefix allowlist is what stops
        ``UTF-8`` / ``SHA-256`` / ``ISO-8601`` from becoming tickets: they are
        perfectly key-shaped, and only the prefix tells them apart from a key.
        """
        if not self.shape.fullmatch(candidate):
            return False
        if self.key_set:
            return candidate.upper() in self.key_set
        return candidate.partition("-")[0] in self.projects

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
    #: PRs already checkpointed by the interrupted run this run resumes.
    prs_skipped_resumed: int = 0
    events: list[dict] = field(default_factory=list)
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    people: set[str] = field(default_factory=set)
    keys_by_placement: dict[str, int] = field(default_factory=dict)
    #: ``push_class`` -> count for the rescopes classified in this run (prd/16).
    push_classes: dict[str, int] = field(default_factory=dict)
    #: Text labels emitted in this run (prd/16): comment_type / thread_outcome.
    comment_types: dict[str, int] = field(default_factory=dict)
    thread_outcomes: dict[str, int] = field(default_factory=dict)
    questions_raised: int = 0
    questions_answered: int = 0
    #: Bot notices that were never sent to the classifier (prd/16).
    bot_comments_skipped: int = 0
    #: Commit build-status records turned into events (prd/44 DES-2).
    build_statuses: int = 0
    watermark: datetime | None = None
    activities_by_action: dict[str, int] = field(default_factory=dict)


class Reducer:
    """One PR in, pseudonymised events out."""

    def __init__(
        self,
        cfg: Config,
        ident: IdentityTable,
        key_filter: KeyFilter,
        as_of: datetime,
        classifier: classify.Classifier | None = None,
        pepper: bytes | None = None,
    ):
        self.cfg = cfg
        self.ident = ident
        self.key_filter = key_filter
        self.as_of = as_of
        #: prd/16: the local text classifier. ``None`` = no text is read (and no
        #: text labels are emitted); the scrape always passes one in practice.
        self.classifier = classifier
        #: prd/22: the per-tenant HMAC key for ref digests. ``None`` = no ref
        #: attributes at all -- never an unkeyed (reversible) digest. Only the
        #: scrape has the raw zone; the read-only probe has nothing to persist.
        self.pepper = pepper
        self.sentinels = set(cfg.sentinel_slugs)
        self.bot_slugs = {str(s).strip().lower() for s in cfg.bot_slugs if str(s).strip()}
        self.actor_kinds: dict[str, str] = {}
        # CI is not a human: the synthetic build actor is a bot for every
        # aggregate that reads ``actor_kind`` (concepts.md §2).
        self.actor_kinds[CI_ACTOR] = "bot"
        #: prd/16: the labels for *this PR's* texts, classified in one bounded
        #: batch by :meth:`_classify_batch` and keyed by content digest. Cleared
        #: at the end of every ``reduce`` so no raw prose is retained.
        self._labels: dict[tuple[str, bytes], tuple[dict, str]] = {}

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
        commit_ids = {str(commit["id"]) for commit in commits if commit.get("id")}
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
        # prd/22: the source/target refs as keyed one-way digests. Equal digests
        # mean "same branch", which is the whole of the stack edge; the branch
        # name never enters the store (see :func:`ref_digest`). No pepper = no
        # ref attributes: an unkeyed digest would be reversible.
        if self.pepper is not None:
            for attribute, ref in (("source_ref", pr.pr.get("fromRef")),
                                   ("target_ref", pr.pr.get("toRef"))):
                digest = ref_digest(repo_label, ref, self.pepper)
                if digest:
                    pr_record["attributes"][attribute] = digest
        # prd/16 "PR diff size": the scrape's git cache measured the final head's
        # numstat totals. Both counts travel together; an absent block means the
        # size is unknown (analysis reads that as "pending", never as zero).
        size = pr.pr.get("megaspeedDiffSize") or {}
        added, removed = size.get("lines_added"), size.get("lines_removed")
        if _line_count(added) and _line_count(removed):
            pr_record["attributes"]["lines_added"] = added
            pr_record["attributes"]["lines_removed"] = removed
        stats.objects.append(pr_record)
        for key in keys:
            stats.objects.append(_object("TICKET", f"TICKET:{key}", ticket_keys=[key]))

        # prd/16 + the review: classify this PR's texts in **one bounded batch**
        # before the loop that consumes them. A serial client was the measured
        # bottleneck (one in-flight request, 4.4 h per 8k comments at 2 s); the
        # batch returns the same labels in the same order, so nothing below
        # changes -- it just no longer waits for one comment at a time.
        ordered = sorted(activities, key=lambda a: (a.get("createdDate") or 0, a.get("id") or 0))
        self._labels = self._classify_batch(pr, ordered, commit_ids, keys, author)

        # prd/16: the PR description is read *here*, classified, and dropped.
        # A description that raises a question is the cheapest place to catch
        # one (it costs a refinement round, not a review round).
        self._description_questions(pr, pr_object_id, ticket_objects, author, created, stats)

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
                labels: dict | None = None
                classifier_version: str | None = None
                if comment.get("id") is not None:
                    thread_id = f"THREAD:{repo_label}#{pr.id}:{comment['id']}"
                    thread_objects.append(thread_id)
                    # A thread's state is cumulative: ADDED opens it, a later
                    # COMMENTED/UPDATED activity may resolve it. Keep the
                    # earliest timestamp and the latest resolution.
                    entry = threads.setdefault(
                        thread_id, {"created": ts, "cid": comment["id"]}
                    )
                    entry.setdefault("commenter", actor)
                    entry["severity"] = severity
                    entry["anchor_class"] = anchor_class
                    entry["resolved"] = resolved
                    entry["resolved_at"] = (
                        _ts(comment["resolvedDate"]) if resolved and comment.get("resolvedDate")
                        else (ts if resolved else None)
                    )
                    entry["resolver"] = (comment.get("resolver") or {}) if resolved else {}
                    # prd/75: what this thread *is*, decided deterministically
                    # from the author and the text before any model call.
                    if comment_action != "UPDATED":
                        self._comment_kind(entry, actor, str(comment.get("text") or ""))
                    # prd/16: one label set per thread, taken from the comment
                    # that opened it. The raw body never leaves this branch.
                    if comment_action != "UPDATED" and self.classifier is not None:
                        labels, classifier_version = self._comment_labels(
                            actor, comment.get("text"), severity, stats,
                            context=self._text_context(
                                pr, role="opener", keys=keys, commit_ids=commit_ids,
                                thread_id=thread_id, comment_id=comment["id"],
                                activity_id=activity.get("id"), ts=ts,
                            ),
                        )
                    if labels is not None:
                        entry.setdefault("classifier_version", classifier_version)
                        entry.setdefault("comment_type", labels["comment_type"])
                        entry.setdefault("label_source", "opener")
                        entry.setdefault("thread_intent", labels["thread_intent"])
                        entry.setdefault("question", labels["question"])
                        entry["phase"] = labels["phase"]
                        if activity.get("megaspeedThreadChange"):
                            # The scrape's git cache diffed the anchor path
                            # between this comment and the next push (prd/16).
                            entry.setdefault(
                                "anchor_changed",
                                (activity["megaspeedThreadChange"] or {}).get("changed"),
                            )
                        if labels["answered"]:
                            entry.setdefault("answer", (ts, actor))
                        if labels["question"]:
                            # A reply inside a question thread is the answer --
                            # classify only those replies (one call per thread,
                            # not one per reply everywhere).
                            answer = self._reply_answer(
                                comment, ts, stats, pr=pr, thread_id=thread_id,
                                keys=keys, commit_ids=commit_ids,
                            )
                            if answer is not None:
                                entry.setdefault("answer", answer)
                    # prd/75: a bot/CI thread has no human opener to label. If a
                    # human replied, that reply is the review remark -- one call
                    # per such thread, not per reply.
                    if (
                        comment_action != "UPDATED"
                        and self.classifier is not None
                        and not entry.get("label_source")
                        and entry.get("comment_kind") in ("bot", "ci_notice", "ci_finding")
                    ):
                        reply = self._first_human_reply(
                            comment, ts, stats, pr=pr, thread_id=thread_id,
                            keys=keys, commit_ids=commit_ids,
                        )
                        if reply is not None:
                            label, version, source, reply_actor = reply
                            entry["comment_type"] = label
                            entry["classifier_version"] = version
                            entry["label_source"] = source
                            entry["commenter"] = reply_actor
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
                if labels and labels["question"]:
                    stats.questions_raised += 1
                    self._event(
                        stats, pr, f"{natural}:q", ts, "question_raised", actor,
                        [pr_object_id, *ticket_objects, *thread_objects],
                        {"phase": labels["phase"]}, {"kind": classify.KIND_COMMENT},
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

            elif action == "BUILD_STATUS":
                # A forge that reports the build inline (the DC build-status
                # resource is the usual source, fetched as ``pr.build_statuses``
                # below). Same closed result label either way.
                commit = activity.get("commit") or {}
                record = activity.get("buildStatus") or activity.get("build") or activity
                self._build_status(
                    stats, pr, natural, ts, record, commit_ids,
                    [pr_object_id, *ticket_objects],
                    commit_id=str(commit.get("id") or record.get("commitId") or ""),
                )

            # Unknown actions are ignored on purpose: the vocabulary is closed,
            # so an unknown label is a taxonomy task, never a free-text fallback.

        # -- commit build statuses -> build_status events -------------------
        # prd/44 DES-2: DC reports CI on its own build-status resource, not in
        # the PR activity stream. The scrape attaches those records per commit;
        # each one is a build_status event, so the prd/40 ``ci`` flag is live
        # and the CI queue reaches the blocking graph. A build is never
        # exclusive with a review (concepts.md §2 S4).
        for index, record in enumerate(pr.build_statuses or (), start=1):
            if not isinstance(record, dict):
                continue
            ts_raw = record.get("dateAdded") or record.get("createdDate")
            if not ts_raw:
                continue
            ts = _ts(ts_raw)
            self._build_status(
                stats, pr, f"build:{index}", ts, record, commit_ids,
                [pr_object_id, *ticket_objects],
                commit_id=str(record.get("commitId") or record.get("commit") or ""),
            )

        for thread_id, entry in sorted(threads.items()):
            resolver = entry.get("resolver") or {}
            resolver_actor = self._actor(resolver, stats) if resolver else None
            attributes = {
                "severity": entry["severity"],
                "anchor_class": entry["anchor_class"],
                "comment_kind": entry.get("comment_kind") or "review",
            }
            if entry.get("label_source"):
                attributes["label_source"] = entry["label_source"]
            if entry.get("ci_template"):
                attributes["ci_template"] = entry["ci_template"]
            if entry.get("ci_issues") is not None:
                attributes["ci_issues"] = int(entry["ci_issues"])
            if entry.get("coverage_min") is not None:
                attributes["coverage_min"] = int(entry["coverage_min"])
                attributes["coverage_zero"] = bool(entry["coverage_zero"])
            resolver_is_commenter = bool(
                resolver_actor and resolver_actor == entry.get("commenter")
            )
            # The two deterministic inputs ``thread_outcome`` needed, kept on the
            # object so a label patch (ingest.relabel) can re-derive the outcome
            # from a new ``thread_intent`` without re-reading the source. Both
            # are closed booleans; ``anchor_changed`` is absent when the diff was
            # unknown (no git cache), which the outcome reads as "intent decides".
            attributes["resolver_is_commenter"] = resolver_is_commenter
            if entry.get("anchor_changed") is not None:
                attributes["anchor_changed"] = bool(entry["anchor_changed"])
            if entry.get("comment_type"):
                # prd/16: comment_type + thread_outcome are the labels the
                # "comment tax" analysis (prd/15 Q4) joins on.
                outcome = classify.thread_outcome(
                    resolved=bool(entry.get("resolved")),
                    anchor_changed=entry.get("anchor_changed"),
                    resolver_is_commenter=resolver_is_commenter,
                    thread_intent=entry.get("thread_intent", "unclear"),
                )
                attributes["comment_type"] = entry["comment_type"]
                attributes["thread_outcome"] = outcome
                attributes["classifier_version"] = (
                    entry.get("classifier_version") or self.classifier.version
                )
                stats.comment_types[entry["comment_type"]] = (
                    stats.comment_types.get(entry["comment_type"], 0) + 1
                )
                stats.thread_outcomes[outcome] = stats.thread_outcomes.get(outcome, 0) + 1
            stats.objects.append(
                _object(
                    "THREAD",
                    thread_id,
                    repo=repo_label,
                    pr_id=pr.id,
                    ticket_keys=keys,
                    actor=resolver_actor,
                    state="resolved" if entry.get("resolved") else "open",
                    created_at=entry.get("created"),
                    closed_at=entry.get("resolved_at"),
                    attributes=attributes,
                )
            )
            self._question_answer(
                pr, thread_id, entry, resolver_actor, resolver_is_commenter,
                pr_object_id, ticket_objects, stats,
            )
        # The batch held raw prose as its keys only long enough to label this
        # PR: drop it now (prd/02 -- nothing raw is retained past its use).
        self._labels = {}

    # -- prd/16 text classification --------------------------------------
    def _text_context(
        self,
        pr: PullRequest,
        *,
        role: str,
        keys: list[str],
        commit_ids: set[str] | None = None,
        thread_id: str | None = None,
        comment_id: Any = None,
        activity_id: Any = None,
        ts: datetime | None = None,
        actor: str | None = None,
    ) -> dict:
        """Where one classified text came from -- the trace's join key.

        It rides in the raw-zone trace only (``<state-dir>/classify-trace.jsonl``)
        so a relabel run can map a fresh answer back to the exact THREAD object
        (and PR / commit) it belongs to, and an operator can see which PR a slow
        call was for. None of it is raw content.
        """
        context = {
            "repo": f"{pr.project}/{pr.repo}",
            "pr_id": pr.id,
            "role": role,
            "thread_id": thread_id,
            "comment_id": comment_id,
            "activity_id": activity_id,
            "ticket_keys": list(keys),
            "ts": iso(ts) if ts is not None else None,
            "actor": actor,
        }
        if commit_ids is not None:
            context["commit_ids"] = sorted(str(c) for c in commit_ids)
        return context

    def _classify_batch(
        self,
        pr: PullRequest,
        ordered: list[dict],
        commit_ids: set[str] | None = None,
        keys: list[str] | None = None,
        author: str | None = None,
    ) -> dict:
        """Classify this PR's description + comment bodies in one bounded batch.

        The texts are exactly the ones :meth:`_comment_labels` /
        :meth:`_description_questions` are about to ask for (same gating: a bot
        notice and an edited body are never sent, and an empty body is decided
        deterministically), so the batch changes *when* the model is asked, never
        *what* it is asked or which label ends up on an event. The answers are
        keyed by content digest (:func:`ingest.classify.digest`), so no raw prose
        outlives the call.

        A backend without a pool -- the deterministic mock -- runs
        :meth:`ingest.classify.Classifier.classify_many` inline and
        synchronously, so fixture scrapes are byte-for-byte unchanged.
        """
        classifier = self.classifier
        if classifier is None:
            return {}
        keys = list(keys or ())
        repo_label = f"{pr.project}/{pr.repo}"
        items: list[tuple] = []
        description = str(pr.pr.get("description") or "").strip()
        if description:
            items.append(
                (
                    description,
                    classify.KIND_DESCRIPTION,
                    self._text_context(
                        pr, role="description", keys=keys, commit_ids=commit_ids,
                        ts=_ts(pr.pr.get("createdDate"), self.as_of), actor=author,
                    ),
                )
            )
        for activity in ordered:
            if str(activity.get("action") or "") != "COMMENTED":
                continue
            if activity.get("commentAction") not in (None, "ADDED"):
                continue  # a deleted comment, or an edit we never label
            comment = activity.get("comment") or {}
            if comment.get("id") is None:
                continue
            # Bot-vs-human is deterministic (prd/02) and decided *before* the
            # model is asked. ``_actor_kind`` reads the user record only, so the
            # batch does not assign a pseudonym ahead of the walk.
            if self._actor_kind(activity.get("user") or {}) == "bot":
                continue
            body = str(comment.get("text") or "").strip()
            if citext.is_bot_request(body, self.cfg.bot_aliases) or citext.is_admin(body):
                continue  # a command to a tool, or a status note (prd/75)
            if body:
                items.append(
                    (
                        body,
                        classify.KIND_COMMENT,
                        self._text_context(
                            pr,
                            role="opener",
                            keys=keys,
                            commit_ids=commit_ids,
                            thread_id=f"THREAD:{repo_label}#{pr.id}:{comment['id']}",
                            comment_id=comment["id"],
                            activity_id=activity.get("id"),
                            ts=_ts(activity.get("createdDate"), self.as_of),
                            actor=self.ident.pseudonym(activity.get("user") or {}),
                        ),
                    )
                )
        if not items:
            return {}
        # No dedupe: an identical text in two threads still needs its own trace
        # record, and the record is grouped by the thread it came from (the
        # label map below stays keyed by content digest, so the reducer is
        # unaffected).
        answers = classifier.classify_many(items)
        return {
            (kind, classify.digest(text)): answer
            for (text, kind, _context), answer in zip(items, answers)
        }

    def _batched(self, text: str, kind: str) -> tuple[dict, str] | None:
        """The batch's answer for ``text``, or ``None`` when it was not batched."""
        return self._labels.get((kind, classify.digest(text)))

    def _comment_kind(self, entry: dict, actor: str, text: str) -> None:
        """What this thread is: review, CI, bot output, a bot request, or admin.

        Deterministic and first (prd/75): the author kind is known before the
        model, the CI/admin templates are matched on text, and the result decides
        whether a model call happens at all. ``comment_type`` is left unset for
        everything that is not a human review remark.
        """
        if entry.get("comment_kind"):
            return
        if self.actor_kind(actor) == "bot":
            template, issues, low = citext.ci_kind(text)
            if template == "unknown":
                entry["comment_kind"] = "bot"
            elif template == "qodana_issues":
                entry["comment_kind"] = "ci_finding"
            else:
                entry["comment_kind"] = "ci_notice"
            entry["ci_template"] = template
            if issues is not None:
                entry["ci_issues"] = issues
            if low is not None:
                entry["coverage_min"] = max(0, min(100, int(round(low))))
                entry["coverage_zero"] = low <= 0
            return
        if citext.is_bot_request(text, self.cfg.bot_aliases):
            entry["comment_kind"] = "bot_request"
        elif citext.is_admin(text):
            entry["comment_kind"] = "admin"
        else:
            entry["comment_kind"] = "review"

    def _first_human_reply(
        self,
        comment: dict,
        ts: datetime,
        stats: Stats,
        *,
        pr: PullRequest | None = None,
        thread_id: str | None = None,
        keys: list[str] | None = None,
        commit_ids: set[str] | None = None,
    ) -> tuple[str, str, str, str] | None:
        """``(comment_type, version, "reply", actor)`` for a bot thread's first
        human reply, or ``None``.

        A bot/CI thread's opener is not a review remark, but people do reply to
        it; that reply is the only human substance the thread has (prd/75).
        """
        assert self.classifier is not None
        for reply in comment.get("comments") or []:
            reply = reply or {}
            body = str(reply.get("text") or "").strip()
            if not body or citext.is_admin(body):
                continue
            reply_actor = self._actor(reply.get("author") or {}, stats)
            if self.actor_kind(reply_actor) == "bot":
                continue
            labels = self.classifier.classify(
                body,
                classify.KIND_COMMENT,
                self._text_context(
                    pr, role="reply", keys=list(keys or ()), commit_ids=commit_ids,
                    thread_id=thread_id, comment_id=reply.get("id"),
                    ts=_ts(reply.get("createdDate"), ts),
                ),
            )
            version = getattr(self.classifier, "last_version", "") or self.classifier.version
            return labels["comment_type"], version, "reply", reply_actor
        return None

    def _comment_labels(
        self,
        actor: str,
        text: Any,
        severity: str,
        stats: Stats,
        *,
        context: dict | None = None,
    ) -> tuple[dict | None, str | None]:
        """Raw comment body -> (closed labels, backend version).

        ``None`` means "this comment is not dialogue worth labelling": a bot
        notice (a stale-PR auto-decline, a build account) is not a review
        remark, and classifying it would put noise in the comment-tax metric.
        Bot-vs-human is deterministic (prd/02), so it is decided before the
        model is ever asked.

        The version is the backend that actually produced *these* labels: a
        per-call fallback to the deterministic mock is stamped ``mock-v1``, not
        the wrapper's ``ollama-v1`` (FR-3).
        """
        assert self.classifier is not None
        if self.actor_kind(actor) == "bot":
            stats.bot_comments_skipped += 1
            return None, None
        body = str(text or "").strip()
        if citext.is_bot_request(body, self.cfg.bot_aliases) or citext.is_admin(body):
            # A command addressed to a tool, or a status note: not a review
            # remark, so there is nothing to classify (prd/75).
            return None, None
        if not body:
            # An attachment-only or empty comment: the residual label, decided
            # deterministically instead of asking the model about nothing.
            return {
                "comment_type": (
                    "blocker" if severity == "mandatory" else classify.DEFAULT_COMMENT_TYPE
                ),
                "thread_intent": classify.DEFAULT_INTENT,
                "question": False,
                "answered": False,
                "phase": classify.PHASES_BY_KIND[classify.KIND_COMMENT][0],
            }, self.classifier.version
        answer = self._batched(body, classify.KIND_COMMENT)
        if answer is None:
            labels = self.classifier.classify(body, classify.KIND_COMMENT, context)
            version = getattr(self.classifier, "last_version", "") or self.classifier.version
        else:
            labels, version = answer
        if severity == "mandatory" and labels["comment_type"] != "blocker":
            # DC's own BLOCKER marker is deterministic evidence (prd/02:
            # "comment severity -- or Bitbucket task-type where available") and
            # outranks a model that read the prose as a mere suggestion.
            labels = {**labels, "comment_type": "blocker"}
        return labels, version

    def _reply_answer(
        self,
        comment: dict,
        ts: datetime,
        stats: Stats,
        *,
        pr: PullRequest | None = None,
        thread_id: str | None = None,
        keys: list[str] | None = None,
        commit_ids: set[str] | None = None,
    ) -> tuple[datetime, str] | None:
        """The first reply the classifier reads as an answer, or ``None``."""
        assert self.classifier is not None
        for reply in comment.get("comments") or []:
            reply = reply or {}
            body = str(reply.get("text") or "").strip()
            if not body:
                continue
            context = None
            if pr is not None:
                context = self._text_context(
                    pr, role="reply", keys=list(keys or ()), commit_ids=commit_ids,
                    thread_id=thread_id, comment_id=reply.get("id"),
                    ts=_ts(reply.get("createdDate"), ts),
                )
            labels = self.classifier.classify(body, classify.KIND_COMMENT, context)
            if not labels["answered"]:
                continue
            return (_ts(reply.get("createdDate"), ts), self._actor(reply.get("author"), stats))
        return None

    def _description_questions(
        self,
        pr: PullRequest,
        pr_object_id: str,
        ticket_objects: list[str],
        author: str,
        created: datetime,
        stats: Stats,
    ) -> None:
        """Classify the PR description; emit a ``question_raised`` if it asks one."""
        if self.classifier is None:
            return
        body = str(pr.pr.get("description") or "").strip()
        if not body:
            return
        answer = self._batched(body, classify.KIND_DESCRIPTION)
        if answer is None:
            labels = self.classifier.classify(
                body,
                classify.KIND_DESCRIPTION,
                self._text_context(pr, role="description", keys=[], ts=created),
            )
        else:
            labels = answer[0]
        if not labels["question"]:
            return
        stats.questions_raised += 1
        self._event(
            stats, pr, "desc:q", created, "question_raised", author,
            [pr_object_id, *ticket_objects],
            {"phase": labels["phase"]}, {"kind": classify.KIND_DESCRIPTION},
        )

    def _question_answer(
        self,
        pr: PullRequest,
        thread_id: str,
        entry: dict,
        resolver_actor: str | None,
        resolver_is_commenter: bool,
        pr_object_id: str,
        ticket_objects: list[str],
        stats: Stats,
    ) -> None:
        """``question_answered`` for a thread that raised a question (prd/16).

        Two closed sources, in order: a reply the classifier read as an answer,
        or the thread's own resolution by someone other than the asker. A thread
        the asker resolved themselves is a withdrawal, not an answer.
        """
        if not entry.get("question") or not entry.get("phase"):
            return
        answer = entry.get("answer")
        if answer is not None:
            ts, actor = answer
            source = "reply"
        elif entry.get("resolved") and entry.get("resolved_at") and resolver_actor \
                and not resolver_is_commenter:
            ts, actor, source = entry["resolved_at"], resolver_actor, "resolution"
        else:
            return
        stats.questions_answered += 1
        self._event(
            stats, pr, f"t{entry.get('cid')}:a", ts, "question_answered", actor,
            [pr_object_id, *ticket_objects, thread_id],
            {"phase": entry["phase"], "answer_source": source},
            {"thread": str(entry.get("cid"))},
        )

    def _build_status(self, stats: Stats, pr: PullRequest, natural: str,
                      ts: datetime, record: dict, commit_ids: set[str],
                      objects: list[str], *, commit_id: str = "") -> None:
        """One commit build-status record -> one ``build_status`` event (prd/44).

        The state is normalised to the closed ``BUILD_RESULT`` set; the actor is
        the synthetic ``CI`` (a bot, never a person), and the COMMIT object is
        linked only when the store actually has it (no dangling relation).
        """
        result = build_result(record.get("state") or record.get("result")
                              or record.get("status"))
        target = list(objects)
        if commit_id and commit_id in commit_ids:
            target.append(f"COMMIT:{commit_id}")
        self._event(stats, pr, natural, ts, "build_status", CI_ACTOR,
                    target, {"result": result}, record)
        stats.build_statuses += 1

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


def derive_review_rounds(events: list[dict]) -> int:
    """Review rounds for one PR, by the ``concepts.md`` §6 definition.

    ``rounds = #pr_pushed (after first review) + #pr_unapproved with cause ≠
    "push" (after first review)``. Three exclusions the old reducer count did not
    make: pre-first-review pushes are setup; a ``pure_rebase`` that lost no
    approval is a head move; and a push-induced unapproval is the same round as
    its push. This is the store-side twin of
    ``ballcourt.review_round_events`` — one definition, so the store's
    ``review_rounds`` and the analysis agree (prd/36 §5).
    """
    ordered = sorted(events, key=lambda e: (e["ts"], _event_order(e["event_key"])))
    author = next(
        (e.get("actor") for e in ordered if e["activity"] == "pr_opened"), None
    )
    first_index = next(
        (
            index for index, event in enumerate(ordered)
            if event["activity"] in ("comment_added", "pr_approved")
            and event.get("actor") != author
        ),
        None,
    )
    if first_index is None:
        return 0
    rounds = 0
    for event in ordered[first_index + 1:]:
        attributes = event.get("attributes") or {}
        if event["activity"] == "pr_pushed":
            if attributes.get("push_class") == "pure_rebase" and not attributes.get("approvals_lost"):
                continue
            rounds += 1
        elif event["activity"] == "pr_unapproved" and attributes.get("cause") != "push":
            rounds += 1
    return rounds


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


def _stored_events_for_scope(con, scope) -> dict[tuple[str, int], list[dict]]:
    """One scan for every PR in ``scope`` (instead of a query per PR, NFR-1/7)."""
    pairs = sorted(
        {(repo, int(pr_id)) for repo, pr_id in scope if repo is not None and pr_id is not None}
    )
    grouped: dict[tuple[str, int], list[dict]] = {pair: [] for pair in pairs}
    if not pairs:
        return grouped
    for start in range(0, len(pairs), 5_000):
        scope_rel = con.values(*pairs[start : start + 5_000])
        rows = con.execute(
            "SELECT e.repo, e.pr_id, e.event_key, e.ts, e.activity, e.actor,"
            " CAST(e.attributes AS VARCHAR)"
            " FROM events e JOIN scope_rel s"
            " ON e.repo = s.col0 AND e.pr_id = CAST(s.col1 AS BIGINT)"
            " ORDER BY e.ts ASC, e.event_key ASC"
        ).fetchall()
        for repo, pr_id, key, ts, activity, actor, attrs in rows:
            grouped[(repo, int(pr_id))].append(
                {
                    "event_key": key,
                    "ts": ts,
                    "activity": activity,
                    "actor": actor,
                    "attributes": json.loads(attrs or "{}"),
                }
            )
    return grouped


def fill_derived(con, events: list[dict]) -> None:
    """Set derived attributes on *this run's* events before they are written.

    The union of the stored log and the new events is the complete history for
    the touched PRs, so the derivation can happen once, in memory. Doing it
    before the write keeps re-runs true no-ops: the row a run writes is already
    the converged row, so an unchanged PR is not rewritten on every ingest.
    """
    scope = {
        (e["repo"], e["pr_id"])
        for e in events
        if e.get("repo") is not None and e.get("pr_id") is not None
    }
    run_events: dict[tuple[str, int], dict[str, dict]] = {}
    for event in events:
        run_events.setdefault((event["repo"], event["pr_id"]), {})[event["event_key"]] = event
    stored = _stored_events_for_scope(con, scope)
    for repo, pr_id in sorted(scope):
        union: dict[str, dict] = {
            row["event_key"]: row for row in stored.get((repo, int(pr_id)), [])
        }
        union.update(run_events.get((repo, pr_id), {}))
        for key, lost in derive_approvals_lost(list(union.values())).items():
            event = run_events[(repo, pr_id)].get(key)
            if event is not None:
                event["attributes"]["approvals_lost"] = lost
        # prd/36 §5: the merge event carries the store's review_rounds, computed
        # with the same definition the analysis uses (after approvals_lost is
        # known, so a pure rebase that lost an approval still counts).
        merged = next(
            (event for event in union.values() if event["activity"] == "pr_merged"),
            None,
        )
        if merged is not None:
            rounds = derive_review_rounds(list(union.values()))
            event = run_events[(repo, pr_id)].get(merged["event_key"])
            if event is not None:
                event["attributes"]["review_rounds"] = rounds


def recompute_derived(con, scope: set[tuple[str, int]]) -> int:
    """Recompute derived attributes for the PRs touched by this run.

    Scope is the (repo, pr_id) pairs the run emitted events for -- their event
    history is complete in the store, so anything derived from look-ahead can be
    re-derived exactly. Returns the number of rows rewritten. Normally 0: the
    values were already converged by :func:`fill_derived` before the write; this
    is the safety net for rows the reducer did not re-emit.
    """
    rewritten = 0
    updates: list[tuple[str, str]] = []
    stored = _stored_events_for_scope(con, scope)
    for repo, pr_id in sorted(scope):
        events = stored.get((repo, int(pr_id)), [])
        by_key = {event["event_key"]: event for event in events}
        for key, lost in derive_approvals_lost(events).items():
            event = by_key[key]
            if event["attributes"].get("approvals_lost") is lost:
                continue
            event["attributes"]["approvals_lost"] = lost
            updates.append(
                (
                    key,
                    json.dumps(event["attributes"], separators=(",", ":"), sort_keys=True),
                )
            )
        # prd/36 §5: the merge event's review_rounds, re-derived with the same
        # definition as the analysis (and after approvals_lost converged).
        merged = next(
            (event for event in events if event["activity"] == "pr_merged"), None
        )
        if merged is not None:
            rounds = derive_review_rounds(events)
            if merged["attributes"].get("review_rounds") != rounds:
                merged["attributes"]["review_rounds"] = rounds
                updates.append(
                    (
                        merged["event_key"],
                        json.dumps(
                            merged["attributes"], separators=(",", ":"), sort_keys=True
                        ),
                    )
                )
    for start in range(0, len(updates), 5_000):
        update_rel = con.values(*updates[start : start + 5_000])
        con.execute(
            "UPDATE events SET attributes = CAST(s.col1 AS JSON)"
            " FROM update_rel s WHERE events.event_key = s.col0"
        )
        rewritten += len(updates[start : start + 5_000])
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

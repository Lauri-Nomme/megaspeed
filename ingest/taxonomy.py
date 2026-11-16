"""Closed vocabularies — the only values that may cross into the pseudonymised zone.

Everything here is a *finite* set. The ingest pipeline validates every event
through :func:`check_attributes` before it reaches the store, so a classifier
bug can only ever produce a rejected row, never a free-text column.

The vocabulary is copied from ``prd/02-pseudonymization.md``; ``other`` is the
explicit escape hatch for "the deterministic classifier could not decide"
(which is also how the PRD tells us to extend the taxonomy — never by falling
back to raw text).
"""

from __future__ import annotations

import re
from typing import Any

# Version of the raw -> label projection. Bumped when a mapping changes; stored
# next to every event so a re-classification is reproducible.
CLASSIFIER_VERSION = "deterministic-v1"
# Every version this codebase has ever emitted. Add a member when bumping: a
# store may legitimately still hold rows from an older version until the next
# re-ingest replays them (BUG-02), and --verify must accept those.
CLASSIFIER_VERSIONS = frozenset({"deterministic-v1"})

# --- closed sets ---------------------------------------------------------------

ACTIVITIES = frozenset(
    {
        # jira-side (used from the next slice on)
        "created",
        "status_changed",
        "assigned",
        "pointed",
        "sprint_added",
        "sprint_removed",
        "comment_added",
        "worklogged",
        # bitbucket-side
        "pr_opened",
        "pr_pushed",
        "pr_approved",
        "pr_unapproved",
        "pr_needs_work",
        "pr_merged",
        "pr_declined",
        "reviewer_changed",
        "build_status",
        # cross-source
        "question_raised",
        "question_answered",
        "rebased",
    }
)

QUESTION_PHASE = frozenset({"breakdown", "refinement", "sprint", "implementation", "review"})
SEVERITY = frozenset({"mandatory", "suggestion"})
# Jira status *category* (``new`` / ``indeterminate`` / ``done``) projected onto
# a closed label. The raw status name is workflow-specific ("In Review", "QA
# Failed", ...) and is never stored; Jira's own category is the deterministic
# projection (prd/02: "Status / assignee / sprint / points -- deterministic:
# Jira changelog schema").
STATUS_CLASS = frozenset({"todo", "in_progress", "done", "other"})
# A story's resolution, projected onto a finite set. ``None`` (unresolved) is
# carried as the object state ``open``, never as a label.
RESOLUTION_CLASS = frozenset({"done", "wont_do", "duplicate", "other"})
# prd/04 "worklog noise": the ``created - started`` gap, bucketed. The raw gap
# in seconds travels too (``log_delay_seconds``); the bucket is the closed
# dimension an analysis can group on without arithmetic.
LOG_DELAY_BUCKET = frozenset({"same_day", "next_day", "within_week", "late"})
# What an epic/story summary or a sprint goal is *about*, projected onto a
# finite set. A summary is never stored and never paraphrased -- only this label
# (prd/02: "a summary is NOT a classification ... classification is a
# projection onto a pre-agreed closed vocabulary").
SUMMARY_CLASS = frozenset(
    {"feature", "fix", "refactor", "test", "docs", "chore", "spike", "other"}
)
# Jira Agile sprint states (future / active / closed), lower-cased.
SPRINT_STATE = frozenset({"future", "active", "closed"})
# Epic / Story object state: ``open`` until resolved, then the resolution class.
ISSUE_STATE = frozenset({"open"}) | RESOLUTION_CLASS
# prd/16 "Local LLM classification": what a comment *is*, projected onto a
# closed set (never a summary, never a paraphrase -- prd/02).
COMMENT_TYPE = frozenset(
    {"blocker", "suggestion", "question", "nit", "out_of_scope", "optimization"}
)
# What became of a review thread: resolution state + whether the diff moved at
# the anchor + the classifier's read of a "won't fix" (prd/15 Q4).
THREAD_OUTCOME = frozenset({"acted_on", "rejected", "walked_back", "unresolved"})
# The classifier's internal read of a thread's *intent*. It never crosses into
# the store; it is one of the inputs to :func:`ingest.classify.thread_outcome`.
THREAD_INTENT = frozenset({"acted", "rejected", "withdrawn", "unclear"})
# How a question got answered: a reply comment, or the thread being resolved.
ANSWER_SOURCE = frozenset({"reply", "resolution"})
PUSH_CLASS = frozenset({"content", "pure_rebase", "rebase_conflict", "other"})
ANCHOR_CLASS = frozenset({"file", "general"})
THREAD_STATE = frozenset({"open", "resolved"})
CHANGE = frozenset({"added", "removed", "reviewed"})
UN_APPROVE_CAUSE = frozenset({"push", "manual"})
MESSAGE_CLASS = frozenset({"feature", "fix", "refactor", "test", "docs", "chore", "merge", "revert", "wip", "other"})
OBJECT_STATE = frozenset({"open", "merged", "declined"})
# prd/02 "Bot vs human": deterministic, from a known account set (DC user type
# SERVICE plus configured CI/build slugs).
ACTOR_KIND = frozenset({"bot", "human"})
OBJECT_TYPES = frozenset(
    {"PR", "COMMIT", "TICKET", "PERSON", "PRREV", "THREAD", "EPIC", "STORY", "SPRINT"}
)

# A commit hash: DC uses full 40-hex, abbreviated forms are still hashes.
HASH = re.compile(r"^[0-9a-f]{7,40}$")
# A ticket key used as an *attribute* value (the epic link on a Story object).
# Ticket keys are explicitly allowed to cross (prd/02); this shape is the same
# default ``key_shape`` the store validates, and it can never hold prose.
KEY_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$")
# A calendar date (``2026-09-11``), used for a Sprint's planned end. A date is a
# timestamp, not text: it is shape-validated like a hash, so it can never carry
# a summary or a comment.
DATE_TOKEN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A classifier version / model tag: one token, no whitespace (``mock-v1``,
# ``ollama-v1``, ``qwen3-30b-a3b:2507``). Shape-validated like a hash, so a
# well-meaning "notes" field can never be smuggled in next to a label.
CLASSIFIER_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}$")


class _NonNegativeInt:
    """Spec marker: an integer that cannot be negative (a line count, an index).

    ``bool`` is an ``int`` in python and a negative line count is nonsense, so
    both are rejected -- otherwise ``--verify`` would happily accept a store
    whose ``lines_added`` is ``True``.
    """

    __name__ = "non-negative int"

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<non-negative int>"


COUNT = _NonNegativeInt()

# attribute name -> allowed string values, or a python type for scalars.
ATTRIBUTE_SPEC: dict[str, Any] = {
    "phase": QUESTION_PHASE,
    "severity": SEVERITY,
    "push_class": PUSH_CLASS,
    "anchor_class": ANCHOR_CLASS,
    "thread_state": THREAD_STATE,
    "answer_source": ANSWER_SOURCE,
    "change": CHANGE,
    "cause": UN_APPROVE_CAUSE,
    "message_class": MESSAGE_CLASS,
    "state": OBJECT_STATE,
    "actor_kind": ACTOR_KIND,
    "sentinel": bool,
    "stacked": bool,
    "approvals_lost": bool,
    "commits_added": int,
    "commits_removed": int,
    "review_rounds": int,
    "replies": int,
    "push_index": int,
    # -- jira-side (Story/Epic/Sprint, prd/03/04) ------------------------------
    # A status transition, both ends projected onto STATUS_CLASS.
    "from_status": STATUS_CLASS,
    "to_status": STATUS_CLASS,
    # Which sprint a ``sprint_added`` / ``sprint_removed`` event is about. The
    # SPRINT object id is on the event's object set; the id here saves a join.
    "sprint_id": COUNT,
    # ``sprint_added`` that Jira never recorded as a change: the issue was in
    # the sprint from its start (membership-at-start vs added-later, prd/04).
    "at_start": bool,
    # ``pointed``: the new estimate, in whole points.
    "points": COUNT,
    # ``worklogged``: the logged amount, and the ``created - started`` gap that
    # is the worklog-noise signal (prd/04).
    "time_spent_seconds": COUNT,
    "log_delay_seconds": COUNT,
    "log_delay_bucket": LOG_DELAY_BUCKET,
}

# Object attributes live in ``objects.attributes`` (JSON). Same rule: closed
# values only, no free text.
OBJECT_ATTRIBUTE_SPEC: dict[str, Any] = {
    "actor_kind": ACTOR_KIND,
    "push_index": int,
    "push_class": PUSH_CLASS,
    "commits_added": int,
    "commits_removed": int,
    "severity": SEVERITY,
    "anchor_class": ANCHOR_CLASS,
    # prd/16: the text classifier's projection of a review thread. Both are
    # closed labels; ``classifier_version`` records *which* classifier produced
    # them so a re-classification is replayable (prd/02, data lifecycle).
    "comment_type": COMMENT_TYPE,
    "thread_outcome": THREAD_OUTCOME,
    "classifier_version": CLASSIFIER_TOKEN,
    "merge_commit": HASH,
    # prd/16 "PR diff size": ``git diff --numstat <target>...<head>`` totals on
    # the PR's final head. Integers only; both absent when the diff could not be
    # computed (the analysis layer reads that as "pending", never as zero).
    "lines_added": COUNT,
    "lines_removed": COUNT,
    # -- jira-side object attributes -------------------------------------------
    # The deterministic projection of an epic/story summary (never the summary).
    "summary_class": SUMMARY_CLASS,
    # The same projection of a sprint goal (prd/03: Sprint.goal-class).
    "goal_class": SUMMARY_CLASS,
    # A Story's epic link, as the epic's ticket key (keys may cross, prd/02).
    "epic_key": KEY_TOKEN,
    # A Story's current estimate, in whole points (absent = unestimated).
    "points": COUNT,
    # A Sprint's planned end; ``closed_at`` carries the actual completion.
    "end_date": DATE_TOKEN,
}

# Bitbucket DC activity action -> activity vocabulary (PRD 02).
#
# The keys are the *real* DC 9.x action names, verified against a DC 9.4.18
# instance (``/rest/api/1.0/.../pull-requests/{id}/activities``): there is no
# ``REVIEWER_UPDATED`` action -- DC emits ``UPDATED`` with ``addedReviewers`` /
# ``removedReviewers``. ``REVIEWED`` is a reviewer completing their review.
PR_ACTIVITY_MAP = {
    "OPENED": "pr_opened",
    "RESCOPED": "pr_pushed",
    "COMMENTED": "comment_added",
    "APPROVED": "pr_approved",
    "UNAPPROVED": "pr_unapproved",
    "NEEDS_WORK": "pr_needs_work",
    "MERGED": "pr_merged",
    "DECLINED": "pr_declined",
    "UPDATED": "reviewer_changed",
    "REVIEWED": "reviewer_changed",
}


class TaxonomyError(ValueError):
    """Raised when a classifier tries to emit a value outside the closed sets."""


def check_activity(activity: str) -> str:
    if activity not in ACTIVITIES:
        raise TaxonomyError(f"activity outside closed vocabulary: {activity!r}")
    return activity


def check_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    """Validate an event's attribute bag; raises :class:`TaxonomyError` on any
    unknown key or non-enumerated value. Never widens, never stringifies."""
    return _check_spec(attrs, ATTRIBUTE_SPEC, "attribute")


def check_object_attributes(attrs: dict[str, Any]) -> dict[str, Any]:
    """Validate an object's attribute bag (``objects.attributes``)."""
    return _check_spec(attrs, OBJECT_ATTRIBUTE_SPEC, "object attribute")


def _check_spec(attrs: dict[str, Any], spec_map: dict[str, Any], label: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        spec = spec_map.get(key)
        if spec is None:
            raise TaxonomyError(f"{label} outside closed vocabulary: {key!r}")
        if isinstance(spec, frozenset):
            if value not in spec:
                raise TaxonomyError(f"{label} {key!r} value outside closed vocabulary: {value!r}")
        elif isinstance(spec, re.Pattern):
            if not isinstance(value, str) or not spec.fullmatch(value):
                raise TaxonomyError(f"{label} {key!r} must match {spec.pattern!r}, got {value!r}")
        elif spec is COUNT:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TaxonomyError(
                    f"{label} {key!r} must be a non-negative int, got {value!r}"
                )
        elif isinstance(spec, type):
            # bool is a subclass of int in python; keep them strictly apart.
            if spec is int and isinstance(value, bool):
                raise TaxonomyError(f"{label} {key!r} must be int, not bool")
            if not isinstance(value, spec):
                raise TaxonomyError(f"{label} {key!r} must be {spec.__name__}, got {type(value).__name__}")
        out[key] = value
    return out


def check_message_class(value: str) -> str:
    if value not in MESSAGE_CLASS:
        return "other"
    return value

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

from typing import Any

# Version of the raw -> label projection. Bumped when a mapping changes; stored
# next to every event so a re-classification is reproducible.
CLASSIFIER_VERSION = "deterministic-v1"

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
PUSH_CLASS = frozenset({"content", "pure_rebase", "rebase_conflict", "other"})
ANCHOR_CLASS = frozenset({"file", "general"})
THREAD_STATE = frozenset({"open", "resolved"})
CHANGE = frozenset({"added", "removed"})
UN_APPROVE_CAUSE = frozenset({"push", "manual"})
MESSAGE_CLASS = frozenset({"feature", "fix", "refactor", "test", "docs", "chore", "merge", "revert", "wip", "other"})
OBJECT_STATE = frozenset({"open", "merged", "declined"})
OBJECT_TYPES = frozenset({"PR", "COMMIT", "TICKET", "PERSON"})

# attribute name -> allowed string values, or a python type for scalars.
ATTRIBUTE_SPEC: dict[str, Any] = {
    "phase": QUESTION_PHASE,
    "severity": SEVERITY,
    "push_class": PUSH_CLASS,
    "anchor_class": ANCHOR_CLASS,
    "thread_state": THREAD_STATE,
    "change": CHANGE,
    "cause": UN_APPROVE_CAUSE,
    "message_class": MESSAGE_CLASS,
    "state": OBJECT_STATE,
    "sentinel": bool,
    "stacked": bool,
    "approvals_lost": bool,
    "commits_added": int,
    "commits_removed": int,
    "review_rounds": int,
    "replies": int,
}

# Bitbucket DC activity action -> activity vocabulary (PRD 02).
PR_ACTIVITY_MAP = {
    "OPENED": "pr_opened",
    "RESCOPED": "pr_pushed",
    "APPROVED": "pr_approved",
    "UNAPPROVED": "pr_unapproved",
    "NEEDS_WORK": "pr_needs_work",
    "MERGED": "pr_merged",
    "DECLINED": "pr_declined",
    "REVIEWER_UPDATED": "reviewer_changed",
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
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        spec = ATTRIBUTE_SPEC.get(key)
        if spec is None:
            raise TaxonomyError(f"attribute outside closed vocabulary: {key!r}")
        if isinstance(spec, frozenset):
            if value not in spec:
                raise TaxonomyError(f"attribute {key!r} value outside closed vocabulary: {value!r}")
        elif isinstance(spec, type):
            # bool is a subclass of int in python; keep them strictly apart.
            if spec is int and isinstance(value, bool):
                raise TaxonomyError(f"attribute {key!r} must be int, not bool")
            if not isinstance(value, spec):
                raise TaxonomyError(f"attribute {key!r} must be {spec.__name__}, got {type(value).__name__}")
        out[key] = value
    return out


def check_message_class(value: str) -> str:
    if value not in MESSAGE_CLASS:
        return "other"
    return value

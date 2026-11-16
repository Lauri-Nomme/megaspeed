"""Flow-efficiency accounting constants, shared by the PR and ticket layers.

A *touch* (a review comment, an approval, a status change, a re-point) is
point-in-time: it ends a wait, it does not fill it. Crediting the whole wait a
touch ended would make every item 100% active, so a touch is worth a fixed
attention cost instead — stated in every payload, so the number can be
recomputed.

The constants live here, not in ``rules`` or ``jira``, because both layers
report a flow efficiency (``rules.flow_efficiency`` per PR, ``jira`` per
segment) and two copies of "0.5 h per touch" would drift into two numbers that
cannot be compared.
"""

from __future__ import annotations

# An activity touch is worth this many hours of attention (prd/17).
TOUCH_HOURS = 0.5
# "Active" phases of the interval model: the author is demonstrably working
# (commits in the interval, a push, a rebase, an author reply). Everything else
# — review, CI, an idle author, the unobserved tail — is waiting.
ACTIVE_PHASES = ("active", "rebasing")
# Events that mean somebody worked on the PR at that instant. Handover and
# roster events (``pr_opened``, ``reviewer_changed``) and the terminal events
# are not work; a build status is CI, not a person.
TOUCH_ACTIVITIES = (
    "comment_added", "pr_approved", "pr_needs_work", "pr_unapproved",
    "pr_pushed", "rebased", "question_raised", "question_answered",
)
# The ticket-side equivalent (prd/21): a status change, a re-point, a
# reassignment or a sprint move is the moment the story changed, and the
# worklog is the work itself (it is priced by its duration, not as a touch).
TICKET_TOUCHES = (
    "status_changed", "pointed", "assigned", "sprint_added", "sprint_removed",
)

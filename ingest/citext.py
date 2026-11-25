"""Deterministic classification of non-review comments (prd/75).

The corpus has three kinds of text that the six-class review vocabulary cannot
describe, and that a model should never be asked to guess:

* **CI output** -- a coverage report or a Qodana result. It has no "want" from
  the author, so it is not a ``comment_type``; it is a notice, or a finding when
  the bot itself says so.
* **administrative notes** -- ``rebased <url>``, ``same as <url>``,
  ``depends on <url>``, a bare link, an image attachment. Status notes, not
  review remarks.
* **bot-directed text** -- ``@bot review``; the opener is a request to a tool.

:func:`is_notice` widens the administrative set to the *no-fit* families of
prd/78 (merge/conflict notes, review requests, author status notes, fyi/thanks).
It is anchored on the whole text on purpose: ``"rebased"`` is a notice, a long
remark that merely starts with it is a review comment.

Everything here is a closed-set answer derived from the text alone, with no
model and no raw-text retention. The pipeline decides *who* the author is
(deterministic bot/human, prd/02); this module only decides *what the text is*
once that is known.
"""

from __future__ import annotations

import re

#: The template families the corpus actually contains (prd/75). ``unknown`` is
#: a real answer: a bot posted something outside the known set, and the caller
#: counts it so a missing rule is visible instead of being guessed.
CI_TEMPLATE = frozenset({"coverage", "qodana_clean", "qodana_issues", "unknown"})

#: ``comment_kind`` -- orthogonal to ``comment_type`` (prd/75).
COMMENT_KIND = frozenset(
    {"review", "ci_notice", "ci_finding", "bot", "bot_request", "admin"}
)

COVERAGE_HEADER = re.compile(r"^\s*partial\s+code\s+coverage\b", re.I)
COVERAGE_PERCENT = re.compile(
    r"(instruction|line|branch) coverage is (\d+)\s*%", re.I
)
COVERAGE_RATIO = re.compile(
    r"(instructions?|lines?|branches?) covered ratio is ([0-9]*\.?[0-9]+)", re.I
)
#: Bitbucket renders a 💯 as the shortcode ``:100:``; the bot's all-covered
#: message carries no per-metric numbers.
COVERAGE_EMOJI = re.compile(r":100:\s*%\s*coverage", re.I)

QODANA = re.compile(
    r"^\s*#{0,6}\s*(?:⚠\ufe0f?\s*|✅\s*|:white_check_mark:\s*)?qodana\b", re.I
)
QODANA_CLEAN = re.compile(r"\bno\s+new\s+issues\b", re.I)
QODANA_COUNT = re.compile(r"detected\s+(\d+)\s+new\s+issue", re.I)

_METRIC = {
    "instruction": "instruction",
    "instructions": "instruction",
    "line": "line",
    "lines": "line",
    "branch": "branch",
    "branches": "branch",
}

_URL = r"(?:\(?https?://\S+?\)?)?"
ADMIN_RULES = (
    # rebased / rebase / [rebased], optionally "& resolved conflicts", + link
    re.compile(
        rf"\[?rebas(?:ed|e)\]?(?:\s*(?:&|and)\s*(?:resolved|solved)\s+conflicts)?"
        rf"\s*{_URL}\s*",
        re.I,
    ),
    # same / same as / same here, + link
    re.compile(rf"same(?:\s+(?:as|here))?\s*{_URL}\s*", re.I),
    # depends on / related to, + link
    re.compile(rf"(?:depends\s+on|related\s+to)\s*{_URL}\s*", re.I),
    # an image attachment on its own
    re.compile(r"!\[[^\]]*\]\(attachment:\d+/\d+\)\s*", re.I),
    # a bare link
    re.compile(r"https?://\S+\s*", re.I),
)


def coverage_metrics(text: str) -> dict[str, float]:
    """``metric -> percent`` from any coverage variant (ratios scaled to 100)."""
    out: dict[str, float] = {}
    for name, value in COVERAGE_PERCENT.findall(text or ""):
        out[_METRIC[name.lower()]] = float(value)
    for name, value in COVERAGE_RATIO.findall(text or ""):
        try:
            out[_METRIC[name.lower()]] = float(value) * 100.0
        except ValueError:  # a malformed ratio is "no metric", not a crash
            continue
    return out


def ci_kind(text: str) -> tuple[str, int | None, float | None]:
    """``(template, issues, lowest_metric)`` for one bot comment.

    ``template`` is always one of :data:`CI_TEMPLATE`; ``issues`` is set only
    for ``qodana_issues``; ``lowest_metric`` is the smallest parsed coverage
    percentage (``None`` when the report carries no numbers, including the
    ``:100:`` emoji form, which is recorded as 100).
    """
    body = text or ""
    if QODANA.search(body):
        count = QODANA_COUNT.search(body)
        if count:
            return "qodana_issues", int(count.group(1)), None
        if QODANA_CLEAN.search(body):
            return "qodana_clean", 0, None
        return "unknown", None, None
    if COVERAGE_HEADER.search(body) or COVERAGE_PERCENT.search(body) or COVERAGE_RATIO.search(body):
        values = coverage_metrics(body)
        if COVERAGE_EMOJI.search(body) and not values:
            return "coverage", None, 100.0
        return "coverage", None, (min(values.values()) if values else None)
    return "unknown", None, None


def is_admin(text: str) -> bool:
    """A status note, not a review remark (prd/75)."""
    body = (text or "").strip()
    if not body:
        return False
    return any(rule.fullmatch(body) for rule in ADMIN_RULES)


#: ``@handle`` outside code fences/inline code and not an email local part.
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MENTION = re.compile(r"(?<![\w.+-])@([A-Za-z][\w.-]*)")


def mentions(text: str) -> list[str]:
    """``@handles`` in prose, not in code fences, inline code, or an email."""
    prose = CODE.sub(" ", text or "")
    prose = EMAIL.sub(" ", prose)
    return MENTION.findall(prose)


#: A bare URL, and a markdown link target -- a ``?`` in either is a query string,
#: not a question about the code.
URL = re.compile(r"https?://\S+", re.I)
MD_TARGET = re.compile(r"\]\([^)\s]*\)")


def has_real_question(text: str) -> bool:
    """True when the text asks a question: a ``?`` in prose.

    prd/75 (finding 7): a PR description is a question only when the ``?`` sits
    in the prose, so a query string (``…?commentId=1``), a code fence or inline
    code do not count. The model can read a statement as a question; this is the
    deterministic guard that keeps a description from adding a question row on
    a URL or a code sample.
    """
    prose = CODE.sub(" ", text or "")
    prose = MD_TARGET.sub(" ", prose)
    prose = URL.sub(" ", prose)
    return "?" in prose


#: A mention that is a tool even when its alias was not configured: service
#: accounts are named ``svc-…``, ``ci-…``, ``…-bot``, or a bare ``bot``.
SERVICE_HANDLE = re.compile(
    r"^(?:bot|agent)$|^(?:svc|ci|bot|agent)[-.]|[-.](?:bot|agent)$", re.I
)


def is_bot_request(text: str, aliases) -> bool:
    """True when the text addresses a bot, by configured alias or account shape.

    The alias is the handle people type (``@APE``), which is not necessarily the
    bot's account name, so it is configured separately. A service-shaped handle
    (``@svc-review``, ``@ci-bot``, ``@deploy-bot``) is a tool without any
    configuration; the audit found 29 such commands escaping the skip.
    """
    wanted = {str(a).strip().lower() for a in (aliases or ()) if str(a).strip()}
    for handle in mentions(text):
        low = handle.lower()
        if low in wanted or SERVICE_HANDLE.search(low):
            return True
    return False


# ---------------------------------------------------------------------------
# prd/78: the deterministic no-fit filter.
#
# A *notice* is text that fits none of the six review classes: a rebase/merge
# note, a review request, an author status note, a bare link/attachment, fyi.
# The filter is deliberately conservative -- a false positive removes a real
# review remark from the comment tax, while a false negative costs one model
# call -- so every rule matches the **whole** text (optionally followed by one
# link or "thanks"), never a prefix.

#: A longer notice is a merged-note or a link; a review remark is never this
#: long as a bare notice.
_MAX_NOTICE = 400
#: Status/request families are short phrases. A "will fix ..." that runs on for
#: 300 characters is prose, not a status note.
_MAX_SHORT_NOTICE = 200

#: One trailing link/attachment and an optional "thanks" that any notice may
#: carry. Everything is optional, so ``sub`` only strips when the tail is the
#: end of the text.
_TRAILING = re.compile(
    r"(?:\s*(?:\(?https?://\S+?\)?|!\[[^\]]*\]\(attachment:\d+/\d+\)))?"
    r"(?:\s*[-–—,]?\s*(?:thanks|thank\s+you|thx|cheers))?"
    r"\s*[.!?]*\s*$",
    re.I,
)

#: A concrete defect phrase wins over any notice shape: the PRD says "err on the
#: side of sending it to the model" when the text states broken behaviour.
DEFECT = re.compile(
    r"\b(?:break(?:s|ing)?|broken|crash\w*|data\s+loss|security|vulnerab\w*"
    r"|regression|fail(?:s|ed|ing|ure)?|wrong|incorrect|always\s+(?:false|true)"
    r"|does\s*n[o']?t\s+work|doesn['’]t\s+work|won['’]t\s+work"
    r"|null\s+pointer|exception|leak(?:s|ing)?|deadlock|race\s+condition)\b",
    re.I,
)

#: rebase / [rebased], optionally "& resolved conflicts" and a branch.
_REBASE = re.compile(
    r"\[?\s*rebas(?:ed|e|ing)\s*\]?"
    r"(?:\s*(?:&|and|,)?\s*(?:resolved|solved|fixed)?\s*conflicts?)?"
    r"(?:\s+(?:on|onto|from|with|to|against)\s+[\w./-]+)?",
    re.I,
)

#: "merged master", "merge origin/main", "merged with develop into x".
_MERGE = re.compile(
    r"(?:merged|merge|merging)"
    r"(?:\s+(?:with|from|into|to|against)\s+[\w./-]+)?"
    r"(?:\s+(?:master|main|develop|development|dev|trunk"
    r"|origin/[\w./-]+|[\w.-]+/[\w./-]+))?"
    r"(?:\s+(?:into|to)\s+[\w./-]+)?"
    r"(?:\s*(?:&|and|,)\s*(?:resolved|solved|fixed)\s+conflicts?)?",
    re.I,
)

_CONFLICTS = re.compile(
    r"(?:(?:resolved|solved|fixed)\s+conflicts?"
    r"|conflicts?\s+(?:resolved|solved|fixed))"
    r"(?:\s+(?:in|on)\s+[\w./-]+)?",
    re.I,
)

#: A one-word or short review request, optionally with "please".
_REVIEW_REQUEST = re.compile(
    r"(?:please\s+)?(?:re-?review|review(?:\s+(?:this|it|again|pls|please))?"
    r"|ptal|take\s+a\s+look|have\s+a\s+look"
    r"|looks?\s+good(?:\s+to\s+me)?|lgtm|ship\s+it|\+1|:\+1:|👍)",
    re.I,
)

#: "can/could someone|you review ..." -- *not* "can we ...", which is a
#: suggestion about the code (see :data:`DEFECT`-style negatives in the tests).
_CAN_REVIEW = re.compile(
    r"(?:can|could|would|will)\s+(?:someone|somebody|anyone|you|u)"
    r"\s+(?:please\s+)?"
    r"(?:review|take\s+a\s+look|have\s+a\s+look|look\s+(?:at\s+)?(?:this|it|that)"
    r"|check\s+(?:this|it))"
    r"(?:\s+(?:this|it|that|please|pls))*",
    re.I,
)

#: "@handle review/look" -- a human request; a bot-shaped handle is
#: :func:`is_bot_request` and wins first in the pipeline. Opinion questions
#: ("@alice wdyt", "@alice thoughts?") are real questions about the code and
#: stay review remarks.
_MENTION_REQUEST = re.compile(
    r"@[A-Za-z][\w.-]*\s+(?:please\s+|pls\s+)?"
    r"(?:review|look|take\s+a\s+look|check|ptal)"
    r"(?:\s+(?:this|it|pls|please))?",
    re.I,
)

#: closing this / done / fixed / nvm, handled.
_STATUS = re.compile(
    r"(?:closing|closed|close)"
    r"(?:\s+(?:this|it|the\s+(?:pr|pull\s+request|thread|issue|ticket|one))"
    r"(?:\s+(?:as|now|for\s+now|since)\b.*)?)?"
    r"|(?:nvm|never\s?mind|my\s+bad|oops)"
    r"(?:[,;–—-]\s*(?:handled|done|fixed|resolved|thanks))?"
    r"|(?:done|finished|fixed|handled|resolved|all\s+good|good\s+catch)",
    re.I,
)

#: "will add the fix", "I'll try another approach", "going to push". The verb
#: list is closed: "will fail when ..." and "will change the behaviour" are
#: predictions about the code, not author commitments.
_WILL = re.compile(
    r"(?:will|i['’]ll|i\s+will|we['’]ll|we\s+will|i['’]m\s+going\s+to"
    r"|going\s+to|gonna)\s+"
    r"(?:add|fix|try|push|do|rebase|close|revert|check|look|update|apply"
    r"|implement|address|remove|rename|wait|take\s+a\s+look)\b.*",
    re.I,
)

#: "fixed in <sha>" / "fixed in the next push"; not "fixed in the loop above".
_FIXED_IN = re.compile(
    r"(?:fixed|addressed|resolved|done)\s+(?:in|by)\s+"
    r"(?:[0-9a-f]{6,40}"
    r"|the\s+(?:next|latest|last|new|same)\s+(?:push|commit|patchset|update|pr)"
    r"|[A-Za-z0-9._-]+/\S*)",
    re.I,
)

#: "added the test", "updated the PR" -- a closed noun list, so "updated the
#: count" (a suggestion about code) does not match.
_ADDED_UPDATED = re.compile(
    r"(?:added|updated|removed|deleted|applied|pushed)\s+"
    r"(?:the\s+|a\s+|an\s+|some\s+|new\s+)?"
    r"(?:tests?|unit\s+tests?|test\s+case|it\s+test|docs?|documentation|readme"
    r"|pr|description|comment|branch|reviewers?|review|fix|change|changes"
    r"|commit|code|checks?|build|release\s+notes|labels?|version|bump|tag"
    r"|dependenc(?:y|ies)|null\s+check|guard)"
    r"(?:\s+(?:to|for|in)\s+[\w./#-]+)?",
    re.I,
)

#: fyi / heads up / note:, whole text only (no arbitrary tail).
_ADMIN_TAG = re.compile(
    r"(?:fyi|for\s+your\s+information|heads?\s+up|psa|note|nb)"
    r"(?:\s*:\s*(?:see\s+above|above|link|link\s+below))?",
    re.I,
)

_SEE_ABOVE = re.compile(
    r"(?:see|as)\s+(?:above|below)(?:\s+(?:link|comment|reply))?", re.I
)

#: The families that may run past :data:`_MAX_SHORT_NOTICE` (a merge note with
#: resolved files, a rebase with a very long link).
NOTICE_RULES_LONG = (_REBASE, _MERGE, _CONFLICTS)

#: Everything else: whole-text, short, no code.
NOTICE_RULES = (
    _REBASE,
    _MERGE,
    _CONFLICTS,
    _REVIEW_REQUEST,
    _CAN_REVIEW,
    _MENTION_REQUEST,
    _STATUS,
    _WILL,
    _FIXED_IN,
    _ADDED_UPDATED,
    _ADMIN_TAG,
    _SEE_ABOVE,
)


def _notice_core(body: str) -> str:
    """``body`` without one trailing link/attachment and one trailing thanks."""
    return _TRAILING.sub("", body, count=1).strip()


def is_notice(text: str) -> bool:
    """True when the **whole** text is a no-fit notice, not a review remark.

    The match is anchored (``fullmatch`` on the stripped text) and bounded, and
    a code fence, inline code or a concrete defect phrase always wins: those are
    review text even when they open with "rebased" or "will fix". A bare link,
    an image attachment and ``same as`` keep their :func:`is_admin` answers, so
    this predicate is a strict superset of it.
    """
    body = (text or "").strip()
    if not body or len(body) > _MAX_NOTICE:
        return False
    if CODE.search(body):
        return False
    if is_admin(body):
        return True
    core = _notice_core(body)
    if not core:
        # "thanks", "thanks!", or a bare link already caught above.
        return True
    if is_admin(core):
        return True
    rules = NOTICE_RULES_LONG if len(core) > _MAX_SHORT_NOTICE else NOTICE_RULES
    if not any(rule.fullmatch(core) for rule in rules):
        return False
    return not DEFECT.search(core)

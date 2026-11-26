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
#: Bitbucket DC writes 355 corpus handles in the quoted form (``@"name"``,
#: ``@'name'``); they are mentions exactly like the bare form (prd/78 round 2).
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
FENCE = re.compile(r"```.*?```", re.S)
#: One *short* inline token may decorate an otherwise notice-shaped text (a
#: path, a method name). A long token or several tokens are content, not
#: decoration, and cancel the notice.
INLINE = re.compile(r"`([^`\n]{1,40})`")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MENTION = re.compile(
    r"(?<![\w.+-])@(?:\"([^\"\n]{1,64})\"|'([^'\n]{1,64})'|([A-Za-z][\w.-]*))"
)


def mentions(text: str) -> list[str]:
    """``@handles`` in prose, not in code fences, inline code, or an email.

    Both ``@handle`` and the quoted DC form ``@"name"`` / ``@'name'`` are
    returned as the unquoted handle, so aliases and the service-shape check work
    on either spelling.
    """
    prose = CODE.sub(" ", text or "")
    prose = EMAIL.sub(" ", prose)
    return [
        match.group(1) or match.group(2) or match.group(3)
        for match in MENTION.finditer(prose)
    ]


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


#: prd/79: the opening word of a question. The list is closed and anchored on
#: the first word, so "Isolate the flag" is not a question and "Is the cache
#: shared?" is. A ``?`` anywhere in the prose is already
#: :func:`has_real_question`; this catches the question that omits it.
QUESTION_OPENER = re.compile(
    r"(?:why|what|how|should|could|can|is|are|does|do|did|where|when|who|which"
    r"|would|will)\b",
    re.I,
)

#: prd/79: an opening request to the reviewers ("please review/check").
REQUEST_OPENER = re.compile(
    r"(?:please|pls)\s+(?:re-?review|review|check|take\s+a\s+look"
    r"|have\s+a\s+look|look|ptal)\b",
    re.I,
)

#: Blockquote markers, bullets, emoji and leading punctuation before the first
#: word. Only the opening word is read, never a later one.
_OPENER_TRIM = re.compile(r"^[\s>*#\-–—•✅⚠️:]+")


def looks_like_question(text: str) -> bool:
    """True when the text asks or requests something (prd/79).

    The guard for the author-note rule: an author's own *question* to the
    reviewers is a review remark, not an administrative note. True when a real
    ``?`` sits in the prose (:func:`has_real_question` -- not a URL query string
    and not code) or when the text opens with a question word or a "please
    review/check". Statement prose is what the rule is for; it returns False.
    """
    body = (text or "").strip()
    if not body:
        return False
    if has_real_question(body):
        return True
    lead = _OPENER_TRIM.sub("", body)
    return bool(QUESTION_OPENER.match(lead) or REQUEST_OPENER.match(lead))


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
    r"\s*[.!?:]*\s*$",
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

#: A merge/rebase note may now carry a reason clause (prd/78 B), so a contrast
#: or remark word means the text goes on to discuss the code: send it to the
#: model. This is deliberately narrower than :data:`DEFECT` (which still wins).
_REMARK = re.compile(
    r"\b(?:but|however|though|although|instead|missing|skips?|skipping"
    r"|should|must|why|consider|nit|rename|still|off"
    r"|conflicts?\s+with|outdated"
    r"|duplicat\w*|unclear|confusing|fragile|inefficient|hard\s+to|unnecessary)\b",
    re.I,
)

#: "merged the two lists here" is a remark about code, not a branch note; a
#: genuine rebase note has no reason to say "here" (see :func:`_is_merge_note`).
_HERE = re.compile(r"\bhere\b", re.I)

#: What keeps a reshuffle/rebase note a note and not a remark: a link, a
#: commit sha, a PR/ticket number, or a branch/path reference (prd/78 B).
_REF = re.compile(
    r"(?:https?://\S+"
    r"|#\d+"
    r"|\b[0-9a-f]{7,40}\b"
    r"|(?:pr|pull\s+request|commit|branch|ticket|issue)\s*#?\d+"
    r"|[\w.-]+/[\w.-]+)",
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
    r"(?:please\s+)?"
    r"(?:re-?review|review(?:\s+(?:this|it|again|pls|please))?"
    r"|ptal|(?:take|have)\s+(?:a\s+)?(?:quick\s+)?look"
    r"|(?:a\s+)?quick\s+(?:look|review)"
    r"|welcome\s+(?:any\s+|more\s+|further\s+)?comments?"
    r"(?:\s+(?:later|here|below|after))?"
    r"|looks?\s+good(?:\s+to\s+me)?|lgtm|ship\s+it|\+1|:\+1:|👍"
    r"|approv(?:e|al)|sign[- ]?off)",
    re.I,
)

#: "could I get the frontend team's review" / "needs the team review" (prd/78 C).
_TEAM_REVIEW = re.compile(
    r"(?:(?:can|could)\s+(?:i|we)\s+get\s+(?:a\s+|the\s+)?"
    r"|(?:need|needs|request(?:ing|ed)?|please\s+request)\s+(?:a\s+|the\s+)?)"
    r"(?:[\w'-]+\s+){0,2}(?:team'?s?|[\w'-]+['’]s)\s+(?:review|approval)",
    re.I,
)

#: "can/could someone|you review ..." -- *not* "can we ...", which is a
#: suggestion about the code (see :data:`DEFECT`-style negatives in the tests).
_CAN_REVIEW = re.compile(
    r"(?:can|could|would|will)\s+(?:someone|somebody|anyone|you|u)"
    r"\s+(?:please\s+)?"
    r"(?:review|(?:take|have)\s+(?:a\s+)?(?:quick\s+)?look"
    r"|look\s+(?:at\s+)?(?:this|it|that)"
    r"|check\s+(?:this|it))"
    r"(?:\s+(?:at|into|through)\s+[^?\n]{1,80})?"
    r"(?:\s+(?:this|it|that|please|pls))*",
    re.I,
)

#: A reviewer-added / review-requested note without a handle (prd/78 C).
_REVIEWER_NOTE = re.compile(
    r"(?:reviewers?|approvers?)\s+(?:added|requested|removed|updated)"
    r"|(?:forgot|need|needs|remember)\s+(?:to\s+)?add\s+"
    r"(?:a\s+|another\s+|the\s+)?(?:reviewers?|approvers?)"
    r"|(?:needs?|wants?)\s+(?:a\s+|another\s+|the\s+)?"
    r"(?:reviewers?|approvers?|review|approval)"
    r"(?:\s+(?:to|for|on)\s+(?:the\s+)?[\w./#-]+)?",
    re.I,
)

#: "you should rebase" / "please rebase this" -- a process request, not a
#: general "you should ..." suggestion about code.
_REBASE_REQUEST = re.compile(
    r"you\s+should\s+rebase(?:\s+(?:this|it|the\s+branch|onto|on)\b.*)?"
    r"|(?:please\s+)?rebase\s+(?:this|it|the\s+branch)"
    r"(?:\s+(?:on|onto)\s+(?:the\s+)?[\w./-]+)?",
    re.I,
)

#: A named group's review, with or without the word "team" (prd/78 C).
_GROUP_REVIEW = re.compile(
    r"(?:review|approval)\s+from\s+(?:the\s+)?[\w'-]+(?:\s+team)?"
    r"|(?:the\s+)?[\w'-]+(?:\s+team)?\s+should\s+review\s+(?:this|it|the\s+branch)"
    r"|(?:can|could)\s+(?:i|we)\s+get\s+(?:a\s+|the\s+)?"
    r"(?:[\w'-]+\s+){0,2}(?:team\s+)?(?:review|approval)"
    r"(?:\s+from\s+(?:the\s+)?[\w'-]+(?:\s+team)?)?",
    re.I,
)

#: A statement about the merge plan, not a request (prd/78 C).
_MERGE_PLAN = re.compile(
    r"(?:the\s+)?plan\s+for\s+(?:the\s+)?remaining\s+comments?"
    r"|merge\s+(?:it\s+|this\s+|the\s+pr\s+)?as[- ]is"
    r"|(?:the\s+)?remaining\s+comments?\s+(?:can|will)\s+be\s+addressed\s+later"
    r"|(?:can|could|may)\s+(?:be\s+)?merged\s+as[- ]is",
    re.I,
)

#: "@handle review/look" -- a human request; a bot-shaped handle is
#: :func:`is_bot_request` and wins first in the pipeline. Opinion questions
#: ("@alice wdyt", "@alice thoughts?") are real questions about the code and
#: stay review remarks. A review request may name what to look at, so it may run
#: longer than the other short families; :func:`_is_mention_request` bounds it.
#: The quoted DC spelling (``@"alice"``) is the same mention.
_HANDLE = r"@(?:\"[^\"\n]{1,64}\"|'[^'\n]{1,64}'|[A-Za-z][\w.-]*)"
_MENTION_REQUEST = re.compile(
    _HANDLE + r"\s+(?:please\s+|pls\s+)?"
    r"(?:(?:can|could|would)\s+you\s+(?:please\s+)?|feel\s+free\s+to\s+)?"
    r"(?:review|re-review|look|take\s+a\s+look|have\s+a\s+look|check|ptal"
    r"|approve|approval|sign[- ]?off"
    r"|comment(?:\s+(?:later|here|below))?)"
    r"(?:[\s\S]{0,400})?",
    re.I,
)

#: "tests added" (noun before verb), an author status note (prd/78 B).
_NOUN_VERB_NOTE = re.compile(
    r"(?:tests?|unit\s+tests?|checks?|docs?|changes?"
    r"|reviewers?|approvers?)\s+"
    r"(?:added|updated|fixed|applied|removed|pushed|included|requested)"
    r"(?:\s+(?:to|for|in|on)\s+(?:the\s+)?[\w./#-]+(?:\s+[\w./#-]+)?)?",
    re.I,
)

#: prd/78 B: a short rebase/merge note that names a branch, with no reference
#: ("Rebased on main", "Merged master", "Rebased on the main branch").
_SHORT_B = re.compile(
    r"(?:rebas(?:ed|e|ing)|merg(?:ed|e|ing))\s+"
    r"(?:(?:on|onto|into|from|with|to|in)\s+)?"
    r"(?:the\s+)?(?:main|master|develop(?:ment)?|dev|trunk"
    r"|release(?:/[\w.-]+)?|[\w.-]+/[\w./-]+)"
    r"(?:\s+branch)?"
    r"(?:\s+(?:into|to)\s+[\w./-]+)?",
    re.I,
)

#: prd/78 B: a rebase/merge note and a commit reshuffle are notes only when the
#: text carries a reference; :func:`_is_merge_note` enforces that.
_MERGE_NOTE_WORD = re.compile(r"(?:rebas(?:ed|e|ing)|merg(?:ed|e|ing))", re.I)
_COMMITS_NOTE = re.compile(
    r"(?:(?:squash|reorder|reshuffl|rewor(?:k|ked|king)|rewr(?:ite|ote|itten|iting)"
    r"|move|split|fold|drop|join|combine|collapse)\w*\s+"
    r"(?:[\w'-]+\s+){0,2}commits?\b"
    r"|commits?\s+(?:squashed|reordered|reshuffled|rewritten|moved|split"
    r"|folded|dropped|joined|combined|collapsed))"
    r"(?:\s+(?:into|to)\s+[\w'-]+(?:\s+[\w'-]+)?)?",
    re.I,
)

#: closing this / done / fixed / nvm, handled. ``closing``/``closed`` need an
#: object (the thread/PR) so "close it" or a bare "closed" -- which can be a
#: request to close a buffer/sink/stream -- is not a notice. The imperative
#: ``close`` is not a status at all.
_STATUS = re.compile(
    r"(?:closing|closed)\s+"
    r"(?:this|it|the\s+(?:pr|pull\s+request|thread|issue|ticket|one))"
    r"(?:\s+(?:as|now|for\s+now|since)\b.*)?"
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

#: fyi / heads up / note, whole text only; a trailing link is stripped before
#: this runs, so "fyi: <link>" leaves "fyi" (prd/78 G).
_ADMIN_TAG = re.compile(
    r"(?:fyi|for\s+your\s+information|heads?\s+up|psa|note|nb)"
    r"(?:\s*:\s*(?:see\s+above|above|link(?:\s+below)?|details|report)?)?",
    re.I,
)

#: "see above/below" plus a footnote-style "see the link / #123 / the ticket"
#: (prd/78 G).
_SEE_ABOVE = re.compile(
    r"(?:please\s+)?see\s+(?:above|below|#\d+|https?://\S+|\[\^?\d{1,3}\]"
    r"|attached(?:\s+(?:file|report|link))?"
    r"|the\s+(?:[\w.-]+\s+){0,2}"
    r"(?:link|comment|thread|pr|pull\s+request|ticket|issue|report|logs?|details?)"
    r"|this\s+(?:link|comment|thread|pr|pull\s+request))"
    r"(?:\s+(?:above|below|for\s+(?:details|context)))?",
    re.I,
)

#: A short labelled pointer ("more context: <link>", "related: #123").
_POINTER = re.compile(
    r"(?:more\s+)?(?:context|details|info|information|background|reference"
    r"|link|pointer|related)s?\s*(?:[:\-–—]\s*)?"
    r"(?:see\s+)?(?:above|below|link|#\d+|https?://\S+|\[\^?\d{1,3}\]"
    r"|[\w.-]+/[\w.-]+)?",
    re.I,
)

#: An unlabelled pointer sentence ("the report is here", "this is the report",
#: "details are attached").
_POINTER_SENTENCE = re.compile(
    r"(?:please\s+)?(?:the\s+)?(?:report|link|details?|context|reference|ticket"
    r"|issue|pr|pull\s+request|logs?)\s+"
    r"(?:is|are|can\s+be\s+found)\s+"
    r"(?:in|at|here|below|above|attached|in\s+the\s+[\w.-]+(?:\s+file)?)"
    r"|this\s+(?:is|was)\s+(?:the\s+)?"
    r"(?:report|link|details?|context|reference|ticket|issue|pr|pull\s+request|logs?)"
    r"|(?:details?|context|reference)\s+(?:are|is)\s+attached",
    re.I,
)

#: "filed as #123" / "tracked in #456": a process pointer (prd/78 G).
_TRACKED = re.compile(
    r"(?:filed|tracked|logged|recorded)\s+(?:as|in|under)\s+#?\d+", re.I
)

#: prd/78 E: safely-distinguishable process/scheduling notes. Anything that
#: would need to reason about the code is deliberately left to the model.
_PROCESS = re.compile(
    r"let'?s\s+wait(?:er)?(?:\s+for\s+[^?]{1,60})?"
    r"|(?:this\s+is\s+)?(?:not|no\s+longer)\s+needed"
    r"|belongs?\s+(?:to|in)\s+(?:a\s+|the\s+)?"
    r"(?:past|previous|last)\s+(?:sprint|release|iteration)"
    r"|(?:please\s+)?keep\s+(?:it|this|the\s+pr)\s+open"
    r"|merge\s+(?:it\s+|this\s+)?last",
    re.I,
)

#: A trailing process phrase on a rebase/merge note or a review request
#: ("..., should be merged last"). Stripped before the remark guard, never
#: allowed to turn a bare "should be ..." into a notice on its own.
_PROCESS_SHOULD = re.compile(
    r"\s*[,;:–—-]?\s*(?:this\s+|it\s+|the\s+pr\s+)?should\s+be\s+"
    r"(?:merged|closed|approved|squashed|rebased)"
    r"(?:\s+(?:last|first|now|as[- ]is))?"
    r"(?:\s+(?:after|before)\s+#?\d+)?\s*[.!]*\s*$",
    re.I,
)

#: Everything else: whole-text, short, no code.
NOTICE_RULES = (
    _REBASE,
    _MERGE,
    _SHORT_B,
    _CONFLICTS,
    _REVIEW_REQUEST,
    _TEAM_REVIEW,
    _GROUP_REVIEW,
    _REVIEWER_NOTE,
    _REBASE_REQUEST,
    _CAN_REVIEW,
    _COMMITS_NOTE,
    _NOUN_VERB_NOTE,
    _MERGE_PLAN,
    _STATUS,
    _WILL,
    _FIXED_IN,
    _ADDED_UPDATED,
    _ADMIN_TAG,
    _SEE_ABOVE,
    _POINTER,
    _POINTER_SENTENCE,
    _TRACKED,
    _PROCESS,
)


def _notice_core(body: str) -> str:
    """``body`` without one trailing link/attachment and one trailing thanks."""
    core = _TRAILING.sub("", body, count=1).strip()
    # A label left dangling by the link/thanks removal ("tests added: <link>").
    return core.rstrip(" \t:,-–—").strip()


def _strip_inline_tokens(body: str) -> str | None:
    """Remove up to two short inline-code tokens, or ``None`` when unsafe.

    prd/78 round 2/3: one or two short tokens (paths, method names) must not
    cancel an otherwise notice-shaped text. Three or more, or a long token
    (``INLINE`` only matches ≤40 chars, and the caller re-checks ``CODE``), are
    content.
    """
    tokens = list(INLINE.finditer(body))
    if not tokens or len(tokens) > 2:
        return None
    out = body
    for token in reversed(tokens):
        out = out[: token.start()] + " " + out[token.end() :]
    return out.strip()


def _without_process_tail(core: str) -> str:
    """``core`` without a trailing "..., should be merged/closed last"."""
    return _PROCESS_SHOULD.sub("", core, count=1).strip().rstrip(" \t:,-–—")


def _is_merge_note(core: str, reference_source: str) -> bool:
    """prd/78 B: a rebase/merge/reshuffle note that carries a reference.

    The reference (link, sha, #123, branch/path) is what keeps it a note,
    "merged the two lists here" stays a potential review remark. A reason
    clause or prefix is allowed, but a contrast/remark word sends it to the
    model; :func:`is_notice` still applies :data:`DEFECT`.
    """
    note = _without_process_tail(core)
    if not note or not _REF.search(reference_source):
        return False
    if not (_MERGE_NOTE_WORD.search(note) or _COMMITS_NOTE.search(note)):
        return False
    if _REMARK.search(note) or _HERE.search(note):
        return False
    # A short note is a phrase; a long one may carry a reason clause, but it is
    # still bounded (B may run to _MAX_NOTICE only with a reference).
    limit = 30 if len(note) <= _MAX_SHORT_NOTICE else 60
    return len(note.split()) <= limit


def _is_mention_request(core: str) -> bool:
    """prd/78 C: a human mention that asks for a review, not an opinion.

    The real-remark guard lives here (not in the general rule list): a quoted
    mention followed by a verdict ("check whether this copy is needed, and why")
    is a review remark. A service-shaped handle is a bot request, not a notice.
    """
    note = _without_process_tail(core)
    if not note or is_bot_request(note, ()):
        return False
    return bool(_MENTION_REQUEST.fullmatch(note)) and not _REMARK.search(note)


def _is_pointer(core: str) -> bool:
    """prd/78 G: a short labelled pointer with framing text."""
    note = _without_process_tail(core)
    return bool(note) and bool(_POINTER.fullmatch(note)) and not _REMARK.search(note)


def is_notice(text: str) -> bool:
    """True when the **whole** text is a no-fit notice, not a review remark.

    A code fence always wins; up to two *short* inline tokens are removed before
    matching (prd/78 round 2/3), but a third or a long token is code again. A
    concrete defect phrase always wins too: those are review text even when they
    open with "rebased" or "will fix". A bare link, an image attachment and
    ``same as`` keep their :func:`is_admin` answers, so this predicate is a
    strict superset of it.

    prd/78 B/C/G widen the anchored rules: a short rebase/merge note may name a
    branch, a longer one must carry a reference, a mention may name what to look
    at (and is always run through the real-remark guard), and a pointer may be
    framed. A trailing "..., should be merged last" is stripped before matching,
    never on its own. B/C may run to :data:`_MAX_NOTICE` (C with a handle, B with
    a reference); the status families stay at :data:`_MAX_SHORT_NOTICE`.
    """
    body = (text or "").strip()
    if not body or len(body) > _MAX_NOTICE:
        return False
    if FENCE.search(body):
        return False
    if INLINE.search(body):
        # Short inline tokens are decoration only for B/C. The tokens may be
        # the reference ("rebased `feature/x`"), so the pre-strip text is the
        # reference source. Every other family stays code-blocked, which keeps
        # "see `rebase` above" a review remark.
        stripped = _strip_inline_tokens(body)
        if stripped is None or not stripped or CODE.search(stripped):
            return False
        core = _notice_core(stripped)
        if not core:
            return False
        if _is_merge_note(core, body) or _is_mention_request(core):
            return not DEFECT.search(core)
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
    # A trailing "..., should be merged last" is allowed after a note/request;
    # stripping it must never make a bare "should be ..." a notice.
    note = _without_process_tail(core)
    if not note:
        return False
    if len(note) > _MAX_SHORT_NOTICE:
        # B needs a reference here; C needs a handle (the mention rule).
        if _is_merge_note(note, body) or _is_mention_request(note):
            return not DEFECT.search(note)
        return False
    if (
        _is_merge_note(note, body)
        or _is_mention_request(note)
        or _is_pointer(note)
    ):
        return not DEFECT.search(note)
    if not any(rule.fullmatch(note) for rule in NOTICE_RULES):
        return False
    return not DEFECT.search(note)

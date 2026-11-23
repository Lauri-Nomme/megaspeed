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


def is_bot_request(text: str, aliases) -> bool:
    """True when the text addresses a configured bot alias.

    The alias is the handle people type (``@APE``), which is not necessarily
    the bot's account name, so it is configured separately.
    """
    wanted = {str(a).strip().lower() for a in (aliases or ()) if str(a).strip()}
    if not wanted:
        return False
    return any(handle.lower() in wanted for handle in mentions(text))

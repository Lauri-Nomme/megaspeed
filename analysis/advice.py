"""Rules ↔ policy-lab cross-check (prd/43 §1).

The Rules page and the Policy lab answer the same question — *what should we
change first?* — and they used to answer it differently: a rule could rank
"refine first" or "add a second sentinel" at the top while the calibrated model
said those policies **lose** cycle time (or have no clear effect). Two advice
surfaces that disagree make both untrustworthy.

This module is the bridge, deliberately small:

* :data:`POLICIES` names the lab's **preset scenarios** (the same seven the
  Policy page offers) and the rule ids whose advice each one tests. This is not
  "run a scenario per rule" (prd/43 out of scope): several rules share one
  policy, and only the policies that bear on a rule's advice are mapped.
* :func:`report` turns the lab's `compare` blocks into one verdict per policy:
  the signed cycle change, the delivered-work change, whether it contradicts the
  rule, and the one-line note the rule carries.
* :func:`attach` stamps that note onto the matching suggestions.
* The report also names **the lab's best scenario**, so the Rules page can say
  which lever the model actually supports.

Nothing here runs a simulation; the server owns that (and caches it). This
module is pure arithmetic over the lab's already-annotated comparison, so it is
unit-testable without SimPy and cannot disagree with what the Policy page shows.
"""

from __future__ import annotations

#: The lab presets that bear on a rule's advice. ``rules`` are the suggestion
#: ids whose *primary* recommendation is this policy; a rule that only mentions
#: a policy in passing is not mapped (the note must not over-claim).
POLICIES = (
    {
        "id": "refine",
        "label": "refine before start",
        "scenario": {"refine_before_start": True},
        "rules": ("jira-waiting-time", "jira-conformance"),
        # prd/55 §2: the same rule id measures whichever segment is largest. The
        # refinement result may only ride the card whose evidence *is* the
        # created → ready segment; on `All` that rule can be a review-rounds
        # card, and the +81 % refinement note must not ride it.
        "metric_scope": {"jira-waiting-time": {"segment_key": "created_ready"}},
        # the lab prices refinement as work on the PR clock; it cannot see the
        # ticket-level wait the rule is about (prd/43 §1's suggested copy).
        "caveat": (
            "the lab models refinement as added work on the PR clock and cannot "
            "see the ticket-level wait"
        ),
    },
    {
        "id": "sentinel2",
        "label": "add a sentinel",
        "scenario": {"sentinels": 2},
        "rules": ("sentinel-queue", "sentinel-tail"),
        "caveat": "the lab models the median gate and cannot see the forgotten-item tail",
    },
    {
        "id": "size3",
        "label": "cap PRs at 3 commits",
        "scenario": {"size_cap_commits": 3},
        "rules": ("pr-size", "flow-efficiency"),
        "caveat": (
            "the lab splits each PR into more review requests, and every one of "
            "them needs the mandatory approval"
        ),
    },
    {
        "id": "sla4",
        "label": "review SLA 4 h",
        "scenario": {"review_sla_hours": 4},
        "rules": ("review-sla", "review-tail"),
        "caveat": "the lab escalates an overdue review to any free reviewer",
    },
    {
        "id": "roundcap2",
        "label": "cap review at 2 rounds",
        "scenario": {"review_round_cap": 2},
        "rules": ("round-cap",),
        "caveat": "the lab's best cap is 2 rounds — the same cap the rule recommends",
    },
)

#: The percent labels the A/B table already uses: a space before ``%``.
def _percent(value: float | None, *, signed: bool = True) -> str:
    if value is None:
        return "–"
    number = float(value)
    sign = ""
    if signed and number > 0:
        sign = "+"
    elif number < 0:
        sign = "−"          # U+2212, the same minus the Policy page renders
    return f"{sign}{abs(number):.0f} %"


def _rate_text(value: float | None) -> str:
    """A share as a rate: 5.7 %, 4.6 %, 24 %, 10 % (prd/62's tile copy).

    Two significant digits: below 10 % keep one decimal (5.7), above it the
    whole percent is the honest precision (24.1 → 24). The same rule as the
    panel's own formatter, so the guardrail and the tile cannot show 5.7 % and
    6 % for one number.
    """
    if value is None:
        return "–"
    pct = float(value) * 100.0
    return f"{round(pct)} %" if abs(pct) >= 10 else f"{pct:.1f} %"


def _number_text(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{round(float(value), 2):g}"


#: Which modelled guardrail a policy's one sentence leads with. A policy with no
#: modelled fact of its own still shows the held-constant sentence, never
#: silence (prd/62).
GUARDRAIL_PRIMARY = {
    "refine": "rounds_mean",
    "roundcap2": "rounds_mean",
    "sentinel2": "rounds_mean",
    "size3": "prs_per_100_commits",
    "sla4": "escalated_share",
}


def _guardrail(policy: dict, compare: dict | None) -> dict | None:
    """prd/62: the guardrail sentence beside this lever, from one source.

    The modelled number is the *same run* the Policy page shows; the held
    baselines are the measured stability panel the server passed into the
    comparison. When the model cannot represent a guardrail the sentence starts
    with ``held constant:`` and prints the measured baseline — never silence and
    never an invented cost.
    """
    guardrails = (compare or {}).get("guardrails") or {}
    modelled = {entry["id"]: entry for entry in guardrails.get("modelled") or ()}
    held = {entry["id"]: entry for entry in guardrails.get("held_constant") or ()}
    primary = modelled.get(GUARDRAIL_PRIMARY.get(policy["id"], "rounds_mean"))
    if primary is None and modelled:
        primary = next(iter(modelled.values()))
    held_bits = []
    for key, label in (
        ("reopen", "reopen"),
        ("failed_verification", "failed verification"),
    ):
        entry = held.get(key)
        if entry and entry.get("baseline") is not None:
            held_bits.append(f"{label} {_rate_text(entry['baseline'])}")
    if (
        primary
        and primary.get("baseline") is not None
        and primary.get("scenario") is not None
    ):
        modelled_text = (
            f"{primary['label']} {_number_text(primary['baseline'])} → "
            f"{_number_text(primary['scenario'])} (model)"
        )
        parts = [{
            "text": modelled_text,
            "tone": "warning" if primary.get("worse") else "neutral",
        }]
        text = modelled_text
        if held_bits:
            sentence = " and ".join(held_bits) + " are held constant"
            text += "; " + sentence
            parts.append({"text": sentence, "tone": "neutral"})
        text += " — the model does not rework the code, only the review loop"
        return {
            "modelled": True,
            "label": primary["label"],
            "text": text,
            "worsens": bool(primary.get("worse")),
            "parts": parts,
        }
    if held_bits:
        sentence = "held constant: " + " and ".join(held_bits)
        return {
            "modelled": False,
            "label": "reopen",
            "text": sentence + " — the model does not rework the code",
            "worsens": False,
            "parts": [{"text": sentence, "tone": "neutral"}],
        }
    return None


def _verdict(compare: dict | None) -> dict:
    """One policy's lab result, flattened to the facts a rule note needs."""
    if not compare:
        return {
            "available": False,
            "cycle_percent": None,
            "cycle_better": None,
            "delivered_percent": None,
            "delivered_better": None,
            "within_noise": False,
            "verdict": None,
            "verdict_tone": None,
        }
    cycle = compare.get("cycle_median_hours") or {}
    delivered = compare.get("throughput_work_per_day") or {}
    return {
        "available": cycle.get("percent") is not None,
        "cycle_percent": cycle.get("percent"),
        "cycle_direction": cycle.get("direction"),
        "cycle_better": cycle.get("better"),
        "delivered_percent": delivered.get("percent"),
        "delivered_direction": delivered.get("direction"),
        "delivered_better": delivered.get("better"),
        "within_noise": bool(compare.get("within_noise")),
        "verdict": compare.get("verdict"),
        "verdict_tone": compare.get("verdict_tone"),
    }


def _note(entry: dict, caveat: str | None = None) -> str:
    """The one sentence a mapped rule carries (prd/43 §1).

    The wording is fixed for the case the PRD names: a policy the lab models as
    *slower* must say **"not a measured win"**, never read as a saving. A
    no-clear-effect result says so, and a policy that also cuts delivered work
    (the 4 h SLA) says that part too. The policy's caveat explains *why* the lab
    and the rule can both be right — the lab sees a different slice of the flow.
    """
    tail = f" ({caveat})" if caveat else ""
    if not entry.get("available"):
        return "the policy lab has no result for this policy in this window"
    percent = entry.get("cycle_percent")
    if entry.get("within_noise"):
        return (
            "the policy lab finds no clear effect "
            f"({_percent(percent)} cycle, within its run-to-run spread){tail}"
        )
    if entry.get("cycle_better") is False:
        return (
            f"the policy lab models this at {_percent(percent)} cycle — "
            f"not a measured win{tail}"
        )
    if entry.get("cycle_better") is True:
        delivered = entry.get("delivered_percent")
        if entry.get("delivered_better") is False and delivered is not None:
            return (
                f"the policy lab models this at {_percent(percent)} cycle but "
                f"{_percent(delivered)} delivered work — a mixed result{tail}"
            )
        return f"the policy lab models this at {_percent(percent)} cycle{tail}"
    return "the policy lab sees no change on cycle time"


def _entry(policy: dict, compare: dict | None) -> dict:
    values = _verdict(compare)
    entry = {
        "id": policy["id"],
        "label": policy["label"],
        "scenario": dict(policy["scenario"]),
        **values,
    }
    entry["contradiction"] = bool(
        entry["available"]
        and (entry["cycle_better"] is False or entry["within_noise"])
    )
    entry["supports"] = bool(
        entry["available"]
        and entry["cycle_better"] is True
        and not entry["within_noise"]
    )
    entry["note"] = _note(entry, policy.get("caveat"))
    # prd/62: the guardrail is the other half of a lab verdict — copied from the
    # comparison (one source) and rendered next to the speed result, modelled or
    # explicitly held constant.
    entry["guardrails"] = (compare or {}).get("guardrails")
    entry["guardrail"] = _guardrail(policy, compare)
    return entry


def report(comparisons: dict[str, dict | None] | None) -> dict:
    """The cross-check: one entry per policy, the best scenario, and the notes.

    ``comparisons`` maps a policy id to the lab's ``compare`` block (``None``
    when the baseline did not validate, so the scenario was never run). A store
    with no lab result therefore reports ``available: false`` with the reason
    instead of inventing advice.
    """
    comparisons = comparisons or {}
    entries = [_entry(policy, comparisons.get(policy["id"])) for policy in POLICIES]
    available = [entry for entry in entries if entry["available"]]
    candidates = [
        entry for entry in available
        if entry["supports"] and (entry["cycle_percent"] or 0) < 0
        # a cycle win that delivers less work is "mixed", not the best result
        and entry["delivered_better"] is not False
    ]
    best = min(
        candidates,
        key=lambda entry: (entry["cycle_percent"], entry["id"]),
        default=None,
    )
    if best is not None:
        note = (
            f"the lab's best scenario is {best['label']} — "
            f"{_percent(best['cycle_percent'])} cycle"
        )
    elif available:
        note = (
            "no lab scenario beats today's process on cycle time — "
            "the measured rules are the ones to act on"
        )
    else:
        note = (
            "the policy lab has no validated result on this store, so there is "
            "no cross-check to show"
        )
    return {
        "available": bool(available),
        "policies": entries,
        "best": (
            {
                "id": best["id"],
                "label": best["label"],
                "cycle_percent": best["cycle_percent"],
                "delivered_percent": best["delivered_percent"],
            }
            if best is not None else None
        ),
        "note": note,
        "basis": (
            "the policy lab's preset scenarios, run on the same calibrated model "
            "the Policy page shows; a positive cycle change means the rule's "
            "advice is modelled as slower, not as a saving"
        ),
    }


def unavailable(reason: str) -> dict:
    """A stated no-result report (no SimPy, an uncalibratable store, an error)."""
    return {
        "available": False,
        "policies": [],
        "best": None,
        "note": reason,
        "basis": (
            "the policy lab could not run, so the Rules page makes no claim "
            "about what the simulation would say"
        ),
    }


def attach(suggestions: list[dict], cross_check: dict | None) -> None:
    """Stamp the matching note onto each suggestion (in place).

    A rule's ``policy_lab`` block carries the policy it was checked against and
    the one-line note; ``contradiction``/``supports`` are set so the UI can tint
    the line without re-parsing prose. A rule with no mapped policy is left
    untouched — silence is honest, an invented note is not.
    """
    if not cross_check or not cross_check.get("available"):
        return
    best_id = (cross_check.get("best") or {}).get("id")
    by_rule: dict[str, dict] = {}
    for policy in POLICIES:
        entry = next(
            (item for item in cross_check.get("policies", ()) if item["id"] == policy["id"]),
            None,
        )
        if entry is None:
            continue
        for rule_id in policy["rules"]:
            by_rule[rule_id] = {**entry, "metric_scope": (
                policy.get("metric_scope") or {}
            ).get(rule_id)}
    for suggestion in suggestions:
        entry = by_rule.get(suggestion.get("id"))
        if entry is None:
            continue
        # prd/46 §3 / prd/55 §2: attach the note only to the metric it models.
        # The refinement policy bears only on the advice that says "refine
        # first"; attaching it to a review-rounds card (the same rule id on
        # `All`) was the QA's exact find.
        metric_scope = entry.get("metric_scope") or {}
        evidence = suggestion.get("evidence") or {}
        if any(evidence.get(key) != value for key, value in metric_scope.items()):
            continue
        suggestion["policy_lab"] = {
            "policy": entry["id"],
            "label": entry["label"],
            "cycle_percent": entry["cycle_percent"],
            "delivered_percent": entry["delivered_percent"],
            "contradiction": entry["contradiction"],
            "supports": entry["supports"],
            # name the metric the note models, so a reader can see why it is here
            "applies_to": (
                "the created → ready segment"
                if metric_scope.get("segment_key") == "created_ready"
                else suggestion.get("metric")
            ),
            # prd/46 §3: name the banner's own best scenario on the rule it
            # comes from, so the #1 and the banner cannot disagree.
            "best": entry["id"] == best_id,
            "note": entry["note"],
            # prd/62: the guardrail line, from the lab's own comparison.
            "guardrail": entry.get("guardrail"),
        }

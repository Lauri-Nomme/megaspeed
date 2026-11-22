"""Discrete-event policy lab: fit a model from the store, then ask "what if" (prd/23).

The product question is *"what should we change first?"* — and the honest way to
answer it is not a rule of thumb but a **model that first reproduces what
happened**. So this module has exactly two halves:

    calibrate(court)                 the store -> a model (all numbers auditable)
    run(calibration, scenario)       the model -> a predicted distribution

and one gate between them:

    validate(calibration)            does the baseline reproduce the history?

Nothing in the UI shows a scenario number until that gate passes (prd/23
§"Validation gate"). A model that cannot reproduce the last quarter cannot
predict the next one, and a confident-looking wrong number is worse than no
number.

The model
---------
SimPy, on a **business-time axis** (one business hour is one time unit; the
tenant's calendar supplies the window, prd/19), because every interval the
calibration measures is already in business hours. Each PR is one process:

    arrive -> [author WIP gate] -> [residue] -> review action* -> author push?
            -> ... -> peer approval -> sentinel approval -> [stack wait] -> merge

* a reviewer is a **resource with capacity 1** shared with that person's own
  author work (the same people review and author), so a busy person makes other
  people wait — that is the bottleneck the model is meant to expose;
* the sentinel is its own resource (the mandatory-approval gate), one per slot,
  so `sentinels: 2` is a rotation;
* a rework push is an author service time followed by another review action;
* the number of review actions and author responses per PR is drawn from the
  store's per-PR histogram, so the *shape* of a PR's life is calibrated, not
  invented;
* the **residue** is the measured part of the cycle that none of those handovers
  explains (the ball sat with someone without producing a push, an approval or a
  merge). It is carried as one delay so the parts add up to the whole.

Attention + queue + service, stated once
----------------------------------------
The store observes a **response time** (the ball enters the review queue ->
reviewer action), never what is inside it. Two different mechanisms hide there
and they need different fixes (prd/05: *slow server* vs *overloaded server*):

* **attention latency** — the reviewer has not looked yet (an SLA fixes this);
* **queueing** — the reviewer is busy (capacity fixes this);
* **service** — the work.

Response times alone cannot separate them, so the split rests on two disclosed
constants, :data:`SERVICE_SHARE` (that share of the observed response is work)
and :data:`QUEUE_RECOVERY` (the share of the M/G/1 queue estimate a server fed
by this pipeline's smoothed arrivals actually realises). The M/G/1 relation then
prices the queue and the rest is attention

    q = (rho / (1 - rho)) * s * (1 + CV^2) / 2,   rho = lambda * s

so at baseline the three terms add back to the observed response by
construction *and* by simulation, and a scenario changes ``rho`` and therefore
``q``. This is an **assumption, not a measurement** — and it is what turns "the
sentinel answers in ~6 h" into "the gate is ~54 % loaded with ~2 h of queue" —
which is the difference between "add a sentinel" being the answer or not.
``QUEUE_RECOVERY`` is the half of the identity the gate is sensitive to: with it
at 1.0 the simulated server waits less than the P-K term, attention absorbs the
loss, and the baseline under-predicts the cycle.

What the model does **not** claim (prd/23 §"Out of scope")
---------------------------------------------------------
Multi-repo/multi-team co-simulation, the Jira ticket flow, the full 8-segment
lifecycle and network/load effects. It models the PR review loop and says so.

Read-only, like the rest of ``analysis/``: it takes the ball-in-court
reconstruction and returns numbers. It never touches raw text.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

try:  # SimPy is the engine; a missing install is a clear error, not a broken import
    import simpy
except ImportError:  # pragma: no cover - requirements.txt installs it
    simpy = None

from analysis import ballcourt
from analysis.effort import TOUCH_HOURS

# A reviewer action (what ends a review wait). Same vocabulary as ``rules``.
REVIEW_ACTIONS = ("comment_added", "pr_approved", "pr_needs_work", "pr_unapproved")

# Fitting constants, stated because they change the numbers.
FLOOR_HOURS = 0.05        # a same-instant handover is not a 0 h service time
MAX_SERVICE_HOURS = 80.0  # clip a pathological lognormal draw (a bot-like tail)
MIN_FIT_SAMPLES = 3       # fewer than this and a person borrows the pooled fit
DEFAULT_TOLERANCE = 0.20  # prd/23: ±20 % on the cycle-time median
DEFAULT_SEED = 20260817   # the window's first day; deterministic by default
DEFAULT_REPLICATIONS = 6  # pooled runs, so Monte-Carlo noise cannot decide a policy
# Without a calendar the time axis is wall-clock, so a day is 24 h. With one,
# a day is the calendar's working window (e.g. 8 h) and business hours are what
# every interval is measured in.
DEFAULT_BH_PER_DAY = 24.0
# The one assumption the store cannot measure: how much of a reviewer's observed
# response time is *work* rather than attention latency + queueing. Just under
# half keeps both mechanisms visible (the mandatory gate comes out ~50-60 %
# loaded), and it is not what the gate turns on: at baseline the three terms add
# back to the observed response by construction, so changing this share moves
# the split, not the level (see :func:`decompose_response`).
SERVICE_SHARE = 0.45
# The other half of that identity, and the constant the validation gate actually
# turns on. The M/G/1 (P-K) queue term assumes *Poisson* arrivals; a server in
# this pipeline is fed by arrivals the upstream stages have smoothed (a review
# request can only arrive after the previous handover), so it realises only part
# of the P-K estimate. Measured on del3 / del-combined by comparing each
# simulated server's mean wait with its calibrated P-K term: peer reviews 0.52-
# 0.55, author work 0.52-0.57, the sentinel gate 0.70-0.85 — a mean of ~0.6.
# Attention takes the rest, which is what makes "attention + service + queue =
# the observed response" true in the *model*, not only in the decomposition.
# At 1.0 the model silently loses the difference (attention is floored at 0
# whenever the P-K term exceeds ``observed - service``, which is exactly the
# mandatory gate on a wall-clock axis) and the baseline median lands ~31 % below
# history while the p90 survives on the heavy merge/residue draws.
QUEUE_RECOVERY = 0.6
# Attention is a cadence, not a work distribution: the reviewer checks the queue
# at a roughly regular interval. The observed response's heavy tail belongs to
# the queue and the work (both priced separately), so the attention delay keeps
# the response's mean but not its spread — otherwise a cadence imports a
# work-shaped tail into every handover and the cycle's p90 inflates with the
# level correction.
ATTENTION_MAX_SIGMA = 0.5
MAX_REPLICATIONS = 8
# Sizes: the store's own elasticity is used when positive; a negative one is a
# selection effect (big PRs get attention), not a mechanism, so it is clipped.
MAX_SIZE_ELASTICITY = 0.5
# A stacked child waits for its base's *outcome* (merged or abandoned), not for
# a merge: an abandoned base can never satisfy a merge predicate, and polling it
# used to stall the run 28x past its horizon (FR-3). The bound is a belt-and-
# braces guard -- a base that has not resolved in four horizons is not a
# modelled wait -- and a timeout is counted, never silent.
STACK_WAIT_HORIZONS = 4.0
# The drain runs past the horizon until every in-flight PR finishes. The guard
# is the last resort against a pathological run; a PR still unfinished when it
# trips is reported as censored, not as a zero.
MAX_DRAIN_STEPS = 20_000


class SimulationError(ValueError):
    """A scenario or calibration the model cannot run; the message says why."""


# --------------------------------------------------------------------------- #
# small numeric helpers
# --------------------------------------------------------------------------- #
def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    value = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return round(value, 2)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 2) if values else None


def fit_lognormal(values: list[float]) -> dict:
    """Method-of-moments lognormal fit, with the numbers a reader can check.

    Returns ``{mu, sigma, n, mean, median, p90, min, max}``. ``mu``/``sigma``
    are the parameters of the log; ``mean``/``median``/``p90`` are the fitted
    distribution's hours. The fit is **mean-preserving** (``mean`` equals the
    sample mean), so a scenario cannot be tuned by editing the sample, and a
    service time scaled by ``f`` has mean exactly ``f × observed``.
    Zero-length samples are floored at :data:`FLOOR_HOURS` (a same-instant
    handover still takes a moment).
    """
    if not values:
        return {"mu": None, "sigma": None, "n": 0, "mean": None,
                "median": None, "p90": None, "min": None, "max": None}
    clipped = [max(FLOOR_HOURS, min(MAX_SERVICE_HOURS, float(v))) for v in values]
    mean = statistics.fmean(clipped)
    variance = statistics.pvariance(clipped) if len(clipped) > 1 else 0.0
    sigma_squared = math.log1p(variance / (mean * mean)) if mean > 0 else 0.0
    sigma = math.sqrt(max(0.0, sigma_squared))
    mu = math.log(mean) - sigma_squared / 2 if mean > 0 else math.log(FLOOR_HOURS)
    return {
        "mu": round(mu, 4),
        "sigma": round(sigma, 4),
        "n": len(clipped),
        "mean": round(mean, 2),
        "median": round(math.exp(mu), 2),
        "p90": round(math.exp(mu + 1.2816 * sigma), 2),
        "min": round(min(clipped), 2),
        "max": round(max(clipped), 2),
    }


def _cv(fit: dict) -> float:
    """Coefficient of variation of a fitted lognormal (``sqrt(e^sigma^2 - 1)``)."""
    sigma = (fit or {}).get("sigma") or 0.0
    return math.sqrt(max(0.0, math.exp(sigma * sigma) - 1.0))


def sample_lognormal(rng: random.Random, fit: dict) -> float:
    """One draw from a fitted distribution, clipped to the fitting range."""
    if not fit or fit.get("mu") is None:
        raise SimulationError("cannot sample a distribution that was never fitted")
    value = math.exp(rng.gauss(fit["mu"], fit["sigma"] or 0.0))
    return max(FLOOR_HOURS, min(MAX_SERVICE_HOURS, value))


def scale_fit(fit: dict, factor: float) -> dict:
    """The same shape with its mean multiplied by ``factor`` (log-space shift)."""
    if not fit or fit.get("mu") is None:
        return fit
    shift = math.log(max(1e-9, factor))
    scaled = dict(fit)
    scaled["mu"] = round(fit["mu"] + shift, 4)
    scaled["mean"] = round(fit["mean"] * factor, 2)
    scaled["median"] = round(fit["median"] * factor, 2)
    scaled["p90"] = round(fit["p90"] * factor, 2)
    return scaled


def decompose_response(observed_mean: float, arrival_rate: float,
                       cv: float = 1.0) -> dict:
    """Split an observed response time into *attention* + *queue* + *service*.

    The store observes ``R`` (push -> reviewer action) and ``lambda`` (requests
    per business hour). Two different mechanisms hide inside ``R``, and they
    need different fixes (prd/05: *slow server* vs *overloaded server*):

    * **attention latency** — the reviewer has not looked at the queue yet. It is
      a cadence, not a capacity: the fix is an SLA or a review-first habit, not
      headcount;
    * **queueing** — the reviewer is busy with something else, so the request
      waits. It grows sharply as utilisation approaches 1 (Kingman/P-K), and the
      fix *is* headcount;
    * **service** — the work itself.

    The split needs one stated assumption: :data:`SERVICE_SHARE` of the response
    is work, the rest is attention + queue. Then the M/G/1 queueing term is

        q = (rho / (1 - rho)) * s * (1 + CV²) / 2,   rho = lambda * s

    and ``attention = R - s - q`` (floored, never negative). At baseline the
    three terms add back to ``R`` by construction; a scenario changes ``rho``
    and therefore ``q``, which is exactly the "add a sentinel" question.

    The P-K formula assumes Poisson arrivals, which a server in this pipeline
    does not see (its requests can only arrive after the upstream handover, so
    the stream is smoothed). The queue term is therefore scaled by
    :data:`QUEUE_RECOVERY` — the measured share of the P-K estimate a simulated
    server actually waits — and attention takes the rest. Without that, the
    decomposition still sums to ``R`` but the *simulation* does not (it realises
    the smaller queue and loses the difference), which is what made the baseline
    median ~31 % low on a wall-clock axis.

    Both constants are disclosed in the payload (``queueing.service_share``,
    ``queueing.queue_recovery``) because they are the things the store cannot
    measure: response times alone cannot tell a slow reviewer from an overloaded
    one.
    """
    if observed_mean is None or observed_mean <= 0:
        return {"service_hours": 0.0, "queue_hours": 0.0, "attention_hours": 0.0,
                "service_share": SERVICE_SHARE, "queue_recovery": QUEUE_RECOVERY,
                "utilisation": 0.0, "service_scale": 0.0, "attention_scale": 0.0}
    service = SERVICE_SHARE * observed_mean
    rate = max(0.0, float(arrival_rate))
    rho = min(0.98, rate * service)
    factor = (1.0 + max(0.0, cv) ** 2) / 2.0
    queue = (rho / (1.0 - rho)) * service * factor if rho < 1.0 else observed_mean
    queue *= QUEUE_RECOVERY
    attention = observed_mean - service - queue
    if attention < 0.0:
        # the queue term cannot exceed the observation; take the excess out of
        # the queue, not out of the service (the response is the measured fact)
        queue = max(0.0, observed_mean - service)
        attention = 0.0
    return {
        "service_hours": round(service, 2),
        "queue_hours": round(queue, 2),
        "attention_hours": round(attention, 2),
        "service_share": SERVICE_SHARE,
        "queue_recovery": QUEUE_RECOVERY,
        "utilisation": round(rate * service, 4),
        "service_scale": round(service / observed_mean, 4),
        "attention_scale": round(attention / observed_mean, 4),
    }


def service_fit(fit: dict, observed_mean: float | None, scale: float,
                max_sigma: float | None = None) -> dict:
    """The service distribution: the fitted shape with mean ``scale × observed``.

    Scaling the fit's own mean would drift whenever the fit is not perfectly
    mean-preserving, and the model's response would then no longer match the
    response it was calibrated from. This pins the target explicitly.

    ``max_sigma`` caps the log-space spread while keeping the mean: attention
    latency is a *cadence* (the reviewer has not looked yet), so it must not
    import the heavy tail of the observed response — that tail is the queue and
    the work, which the model already prices separately.
    """
    if not fit or fit.get("mu") is None or not observed_mean:
        return fit
    target = observed_mean * scale
    factor = target / fit["mean"] if fit.get("mean") else 1.0
    scaled = scale_fit(fit, factor)
    if max_sigma is not None and (scaled.get("sigma") or 0.0) > max_sigma and scaled.get("mean"):
        sigma = max_sigma
        scaled["sigma"] = sigma
        scaled["mu"] = round(math.log(scaled["mean"]) - sigma * sigma / 2.0, 4)
        scaled["median"] = round(math.exp(scaled["mu"]), 2)
        scaled["p90"] = round(math.exp(scaled["mu"] + 1.2816 * sigma), 2)
    return scaled


def _histogram(values: list[int]) -> dict:
    return {str(key): count for key, count in sorted(Counter(values).items())}


def _weighted_choice(rng: random.Random, histogram: dict, default: int) -> int:
    if not histogram:
        return default
    keys = [int(key) for key in histogram]
    weights = [histogram[str(key)] for key in keys]
    return rng.choices(keys, weights=weights, k=1)[0]


# --------------------------------------------------------------------------- #
# scenario: the policy knobs (prd/23 §"Policy knobs")
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Scenario:
    """One policy configuration. Every field defaults to the historical process.

    ``wip_limit``          concurrent PRs per author (a pull system; excess work
                           waits in the author's backlog and is reported as
                           *lead time*, not hidden);
    ``sentinels``          mandatory-approval resources (1 = today, 2 = rotation);
    ``review_sla_hours``   a review that has queued longer than this is escalated
                           to any free reviewer instead of its assignee;
    ``size_cap_commits``   a PR above the cap is split into that many smaller PRs;
    ``stack_depth_cap``    stacks deeper than this are flattened into independent
                           PRs (no cascade rebase); unavailable when the store has
                           no ref digests;
    ``review_first``       reviews preempt the reviewer's own author work
                           ("stop starting, start finishing");
    ``refine_before_start`` prd/42 M3: spend one refinement (the measured
                           created → ready segment) before the first review, and
                           catch one round of rework before it happens;
    ``review_round_cap``   prd/42 M3: stop the review loop at this many rounds
                           (the reviewer and author agree the remainder in one
                           pass instead of another comment round).
    """

    name: str = "baseline"
    wip_limit: int | None = None
    sentinels: int = 1
    review_sla_hours: float | None = None
    size_cap_commits: int | None = None
    stack_depth_cap: int | None = None
    review_first: bool = False
    refine_before_start: bool = False
    review_round_cap: int | None = None

    @property
    def is_baseline(self) -> bool:
        return self == Scenario()

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "wip_limit": self.wip_limit,
            "sentinels": self.sentinels,
            "review_sla_hours": self.review_sla_hours,
            "size_cap_commits": self.size_cap_commits,
            "stack_depth_cap": self.stack_depth_cap,
            "review_first": self.review_first,
            "refine_before_start": self.refine_before_start,
            "review_round_cap": self.review_round_cap,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Scenario":
        """Coerce a query-string/JSON body into a scenario, rejecting the rest.

        An unknown knob is a 400 that lists the valid ones — a policy the model
        silently ignored would make the A/B a lie.
        """
        if not raw:
            return cls()
        if not isinstance(raw, dict):
            raise SimulationError("scenario must be an object of knobs")
        allowed = {
            "name", "wip_limit", "sentinels", "review_sla_hours",
            "size_cap_commits", "stack_depth_cap", "review_first",
            "refine_before_start", "review_round_cap",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise SimulationError(
                f"unknown scenario knob(s) {', '.join(unknown)}; valid: "
                + ", ".join(sorted(allowed))
            )
        name = str(raw.get("name") or "scenario")
        wip = _positive_int(raw.get("wip_limit"), "wip_limit")
        sentinels = _positive_int(raw.get("sentinels", 1), "sentinels") or 1
        sla = _positive_float(raw.get("review_sla_hours"), "review_sla_hours")
        size_cap = _positive_int(raw.get("size_cap_commits"), "size_cap_commits")
        depth_cap = _positive_int(raw.get("stack_depth_cap"), "stack_depth_cap")
        round_cap = _positive_int(raw.get("review_round_cap"), "review_round_cap")
        review_first = raw.get("review_first", False)
        if not isinstance(review_first, bool):
            raise SimulationError("review_first must be true or false")
        refine = raw.get("refine_before_start", False)
        if not isinstance(refine, bool):
            raise SimulationError("refine_before_start must be true or false")
        return cls(
            name=name, wip_limit=wip, sentinels=sentinels, review_sla_hours=sla,
            size_cap_commits=size_cap, stack_depth_cap=depth_cap,
            review_first=review_first, refine_before_start=refine,
            review_round_cap=round_cap,
        )


def _positive_int(value, field: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SimulationError(f"{field} must be a whole number >= 1") from None
    if number < 1:
        raise SimulationError(f"{field} must be >= 1")
    return number


def _positive_float(value, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SimulationError(f"{field} must be a number > 0") from None
    if number <= 0:
        raise SimulationError(f"{field} must be > 0")
    return number


#: Each scenario knob that needs a *measured* calibration input, and the reason
#: when the store has none. A scenario whose input is ``None`` is unavailable,
#: never modelled at a default (prd/64 F-03): a made-up refinement cost is what
#: made a PR-only store headline "refine before start — −25 % cycle".
SCENARIO_INPUTS = {
    "refine_before_start": (
        "refinement",
        "hours",
        "no measured refinement segments",
        "refine before start spends the measured created → ready refinement",
    ),
}


def scenario_inputs(calibration: dict, scenario: Scenario) -> dict:
    """Do this scenario's calibration inputs exist in the store?

    Returns ``{"available": bool, "reason": str | None, "missing": [...]}``.
    A missing input makes the scenario unavailable with a stated reason — it is
    never priced at a default, because that default would be presented as a
    modelled result.
    """
    calibration = calibration or {}
    missing = []
    for knob, (block, field, reason, note) in SCENARIO_INPUTS.items():
        if not getattr(scenario, knob, False):
            continue
        value = (calibration.get(block) or {}).get(field)
        if value is None:
            missing.append({
                "knob": knob,
                "input": f"{block}.{field}",
                "reason": reason,
                "note": note,
            })
    if not missing:
        return {"available": True, "reason": None, "missing": []}
    return {
        "available": False,
        "reason": missing[0]["reason"],
        "missing": missing,
    }


def scenario_unavailable(scenario: Scenario, inputs: dict) -> dict:
    """The stated no-result for a scenario whose calibration input is missing."""
    missing = inputs.get("missing") or []
    reasons = "; ".join(
        f"{entry['note']}: {entry['reason']}" for entry in missing
    ) or (inputs.get("reason") or "a calibration input is missing")
    return {
        "ok": False,
        "available": False,
        "reason": inputs.get("reason") or "a calibration input is missing",
        "note": (
            f"{reasons}. The scenario is not modelled — a default cost would "
            "make an unmeasurable policy look like a measured win"
        ),
        "scenario": scenario.as_dict(),
        "missing_inputs": missing,
    }


# --------------------------------------------------------------------------- #
# calibration: the store -> the model
# --------------------------------------------------------------------------- #
def _bh_per_day(calendar) -> float:
    """One day on the model's time axis: the calendar window, else 24 wall hours."""
    if calendar is None or calendar.default_window is None:
        return DEFAULT_BH_PER_DAY
    start, end = calendar.default_window
    return max(0.5, (end - start) / 60.0)


def _samples_by_person(court, window) -> dict:
    """The two service-time samples the interval model can actually observe.

    * **review response** — from a push (or the PR's opening) to the next
      reviewer action by someone other than the author. The responder is the
      person the wait ended on, so the sample is charged to them;
    * **author response** — from a reviewer action back to the author's next
      push, i.e. the author's rework time;
    * **merge delay** — from the final sentinel approval to the merge.

    All in business hours when a calendar is attached, so they share one clock
    with every other metric.
    """
    start, end = window
    review: dict[str, list[float]] = defaultdict(list)
    author: dict[str, list[float]] = defaultdict(list)
    author_per_pr: list[int] = []
    per_pr: list[dict] = []
    merge: list[float] = []
    peer_actions: list[int] = []
    sentinel_actions: list[int] = []
    pushes: list[int] = []
    push_classes: Counter = Counter()
    commits: list[int] = []
    lines: list[int] = []
    sentinels = set(court.sentinels)

    for state in court.prs:
        pr = state.pr
        if not (start <= pr["created_ts"] <= end):
            continue
        who = pr["actor"]
        commits.append(len(state.commits))
        # A review response is measured from the moment the ball entered the
        # review queue (open / push / an approval that still leaves someone
        # owing a response) to the action that ends the wait — the same handover
        # rule the interval model uses. An author response is measured from the
        # last *reviewer* action, and a push clears it, so two pushes in a row
        # are not two author responses.
        review_start = pr["created_ts"]
        last_review: datetime | None = None
        peers = 0
        sents = 0
        pushed = 0
        responses = 0
        review_hours = 0.0
        author_hours = 0.0
        merge_hours = 0.0
        sent_approval = None
        for event in state.events:
            activity = event["activity"]
            actor = event["actor"]
            if activity == "pr_pushed":
                if actor == who:
                    pushed += 1
                    push_classes[(event.get("attributes") or {}).get("push_class") or "other"] += 1
                    if last_review is not None:
                        hours = court.business_hours(who, last_review, event["_ts"])
                        author[who].append(hours)
                        author_hours += hours
                        last_review = None
                        responses += 1
                review_start = event["_ts"]
            elif activity in REVIEW_ACTIONS and actor != who:
                hours = court.business_hours(actor, review_start, event["_ts"])
                review[actor].append(hours)
                review_hours += hours
                last_review = event["_ts"]
                review_start = event["_ts"]
                if actor in sentinels:
                    sents += 1
                    if activity == "pr_approved":
                        sent_approval = event["_ts"]
                else:
                    peers += 1
        if sent_approval is not None and pr["closed_ts"] is not None:
            merge_hours = court.business_hours(None, sent_approval, pr["closed_ts"])
            merge.append(merge_hours)
        peer_actions.append(peers)
        sentinel_actions.append(sents)
        pushes.append(pushed)
        author_per_pr.append(responses)
        per_pr.append({
            "merged": pr["state"] == "merged" and pr["closed_ts"] is not None,
            "review": review_hours,
            "author": author_hours,
            "merge": merge_hours,
            "peer": peers,
            "author_responses": responses,
            "cycle": (
                sum(i.hours for i in state.intervals if i.measured)
                if pr["state"] == "merged" and pr["closed_ts"] is not None else 0.0
            ),
        })

    return {
        "review": dict(review),
        "author": dict(author),
        "merge": merge,
        "peer_actions": peer_actions,
        "sentinel_actions": sentinel_actions,
        "pushes": pushes,
        "author_per_pr": author_per_pr,
        "per_pr": per_pr,
        "push_classes": push_classes,
        "commits": commits,
    }


def _pr_sizes(conn) -> dict[str, int]:
    """``object_id -> changed lines`` when the scrape captured a diff size."""
    if conn is None:
        return {}
    try:
        from analysis import rules as rules_module
        sizes = rules_module.load_pr_sizes(conn)
    except Exception:  # noqa: BLE001 - a size column is optional, never fatal
        return {}
    return {
        object_id: info["diff_size"]
        for object_id, info in sizes.items()
        if info.get("diff_size") is not None
    }


def _size_elasticity(pairs: list[tuple[int, float]]) -> dict:
    """Elasticity of latency to PR size: ``d log(hours) / d log(commits)``.

    Reported even when it is negative, because a negative elasticity is a fact
    about the store (large PRs get attention, so they are *not* slower here) and
    the honest consequence is that a size cap has no per-PR latency benefit.
    """
    points = [
        (math.log(size), math.log(max(FLOOR_HOURS, hours)))
        for size, hours in pairs if size and size > 0
    ]
    if len(points) < 8:
        return {"value": None, "n": len(points), "used": 0.0,
                "note": "too few sized PRs to fit a size elasticity"}
    count = len(points)
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    var = sum((x - mean_x) ** 2 for x, _ in points) / count
    if var <= 0:
        return {"value": None, "n": count, "used": 0.0, "note": "PR sizes do not vary"}
    cov = sum((x - mean_x) * (y - mean_y) for x, y in points) / count
    value = cov / var
    used = min(MAX_SIZE_ELASTICITY, max(0.0, value))
    if value <= 0:
        note = (
            "the store shows no positive size-to-latency elasticity, so a size "
            "cap is modelled as a split with no per-PR service benefit"
        )
    else:
        note = "latency scales as commits^elasticity"
    return {"value": round(value, 3), "n": count, "used": round(used, 3), "note": note}


def _stack_depth(court) -> dict:
    """The stack-depth histogram, from the PR ref digests (prd/22)."""
    from analysis import stack as stack_module

    graph = stack_module.build(court)
    depths = [int(graph.depth.get(state.pr["object_id"], 0)) for state in court.prs]
    available = graph.refs > 0
    return {
        "available": available,
        "prs_with_refs": graph.refs,
        "histogram": _histogram(depths) if available else {},
        "median": _median([float(d) for d in depths if d > 0]) if available else None,
        "max": max(depths) if available and depths else 0,
        "stacked_prs": sum(1 for d in depths if d > 0) if available else 0,
        "note": (
            "derived from the PRs' source/target ref digests"
            if available else
            "no PR carries source/target ref digests: this store predates the "
            "stack-depth measurement, so the knob cannot be calibrated (it is inert)"
        ),
    }


def _store_bots(conn) -> set[str]:
    """The store's bot pseudonyms (``actor_kind`` on the PERSON object).

    A bot is not a person to staff: if the Bitbucket service account is in the
    reviewer pool it becomes a capacity-1 server with the highest utilisation in
    the model, so the bottleneck and every scenario number are nonsense (FR-7).
    """
    if conn is None:
        return set()
    from analysis import rules as rules_module

    return rules_module.load_bots(conn)


def _refinement_block(refinement: dict | None) -> dict:
    """The refinement knob's calibrated input, present on every calibration.

    prd/68 F-04: the Jira layer is independent of the PR samples, so a store
    with no PRs (a Jira-only dataset) still has a measured ``created → ready``
    segment. The block therefore travels with the calibration — including the
    early return when there is no review sample to fit — so the UI can price
    the knob instead of reading a missing key as "no Jira layer".
    """
    if refinement:
        return {
            "hours": refinement.get("hours"),
            "n": refinement.get("n", 0),
            "basis": refinement.get("basis"),
        }
    return {
        "hours": None, "n": 0,
        "basis": "no Jira layer, so the created → ready cost is unavailable",
    }


def calibrate(court, window=None, conn=None, bots=None,
              include_bots: bool = False, refinement: dict | None = None) -> dict:
    """Derive the model from the store. Every number here is recomputable.

    ``window`` defaults to the whole store (``ballcourt.window_from_events``).
    ``conn`` is used for the optional diff-size column and for the bot list:
    unless ``include_bots`` is set, the reviewer pool excludes every pseudonym
    the store marks ``actor_kind: bot`` *and* every name passed in ``bots``, so
    the obvious call ``calibrate(court, window, conn=con)`` cannot staff a
    service account as a reviewer. ``include_bots=True`` is for debugging only.

    ``refinement`` is the measured created → ready cost (prd/42 M3,
    ``jira.refinement_cost``); the policy lab's refinement knob needs it, and it
    is passed in because the simulation's own input is the ball-court, not the
    Jira layer.
    """
    bots = set() if include_bots else (set(bots or ()) | _store_bots(conn))
    start, end = window or ballcourt.window_from_events(court, None)
    business_hours = court.business_hours(None, start, end)
    bh_per_day = _bh_per_day(court.calendar)
    business_days = max(1.0, business_hours / bh_per_day)

    samples = _samples_by_person(court, (start, end))
    cohort = [
        state for state in court.prs
        if start <= state.pr["created_ts"] <= end
    ]
    if not samples["review"]:
        return {
            "ok": False,
            "available": False,
            "hours_mode": court.hours_mode,
            "assumption": court.assumption,
            "note": (
                "no reviewer response was observed in this window, so there is "
                "nothing to calibrate a review service time from"
            ),
            "prs": {"n": len(cohort)},
            # prd/68 F-04: no PR sample to fit, but the refinement segment is
            # measured from the Jira layer and must still reach the UI.
            "refinement": _refinement_block(refinement),
        }

    pool = sorted(person for person in samples["review"] if person not in bots)
    sentinels = sorted(person for person in pool if person in court.sentinels)
    peers = [person for person in pool if person not in set(sentinels)]
    pooled_review = fit_lognormal(
        [hours for person in pool for hours in samples["review"][person]]
    )
    pooled_author = fit_lognormal(
        [hours for person in pool for hours in samples["author"].get(person, [])]
    )

    # -- per-person service times (the queue/service decomposition) ---------- #
    reviewers = []
    for person in pool:
        responses = samples["review"].get(person, [])
        reworks = samples["author"].get(person, [])
        review_fit = (
            fit_lognormal(responses) if len(responses) >= MIN_FIT_SAMPLES
            else dict(pooled_review, borrowed=True)
        )
        author_fit = (
            fit_lognormal(reworks) if len(reworks) >= MIN_FIT_SAMPLES
            else dict(pooled_author, borrowed=True)
        )
        # lambda per business hour, from this person's own observed load
        rate_review = len(responses) / business_hours if business_hours else 0.0
        rate_author = len(reworks) / business_hours if business_hours else 0.0
        observed_review = _mean(responses)
        observed_author = _mean(reworks)
        # the person is one server for both classes: reviews and their own
        # author work share the queue, so the combined arrival rate is the load
        split = decompose_response(
            observed_review or 0.0,
            rate_review + rate_author,
            _cv(review_fit),
        )
        author_split = decompose_response(
            observed_author or 0.0,
            rate_review + rate_author,
            _cv(author_fit),
        )
        reviewers.append({
            "person": person,
            "sentinel": person in set(sentinels),
            "review": {
                "observed": {
                    "n": len(responses),
                    "mean_hours": observed_review,
                    "median_hours": _median(responses),
                    "p90_hours": _quantile(responses, 0.9),
                },
                "service": service_fit(review_fit, observed_review, split["service_scale"]),
                "attention": service_fit(review_fit, observed_review, split["attention_scale"],
                                        max_sigma=ATTENTION_MAX_SIGMA),
                "decomposition": split,
                "rate_per_hour": round(rate_review, 4),
            },
            "author": {
                "observed": {
                    "n": len(reworks),
                    "mean_hours": observed_author,
                    "median_hours": _median(reworks),
                },
                "service": service_fit(author_fit, observed_author,
                                       author_split["service_scale"]),
                "attention": service_fit(author_fit, observed_author,
                                         author_split["attention_scale"],
                                         max_sigma=ATTENTION_MAX_SIGMA),
                "decomposition": author_split,
                "rate_per_hour": round(rate_author, 4),
            },
            "utilisation": split["utilisation"],
        })

    sentinel_fit = (
        fit_lognormal([
            hours for person in sentinels for hours in samples["review"].get(person, [])
        ]) if sentinels else pooled_review
    )
    sentinel_observed = _mean([
        hours for person in sentinels for hours in samples["review"].get(person, [])
    ]) if sentinels else pooled_review["mean"]
    sentinel_rate = (
        sum(len(samples["review"].get(person, [])) for person in sentinels)
        / business_hours
        if business_hours else 0.0
    )
    sentinel_split = decompose_response(
        sentinel_observed or 0.0, sentinel_rate, _cv(sentinel_fit)
    )

    merge_fit = fit_lognormal(samples["merge"])
    pushes = [max(0, count - 1) for count in samples["pushes"]]
    push_total = sum(samples["push_classes"].values()) or 1
    push_class = {
        name: round(samples["push_classes"].get(name, 0) / push_total, 3)
        for name in ("content", "pure_rebase", "rebase_conflict")
    }
    rounds = [count for count in samples["peer_actions"]]
    # The action counts are *outcome-conditional*: a PR that is abandoned before
    # the mandatory gate stops early, so the merged PRs the gate scores have
    # more review rounds and author responses than the cohort average (del3:
    # 2.51 peer actions / 1.44 author responses merged vs 2.40 / 1.32 pooled).
    # Drawing the pooled histogram for every PR and then comparing merged PRs
    # only under-counted the rounds a merged PR actually had.
    merged_rounds = [e["peer"] for e in samples["per_pr"] if e["merged"]]
    declined_rounds = [e["peer"] for e in samples["per_pr"] if not e["merged"]]
    merged_responses = [e["author_responses"] for e in samples["per_pr"] if e["merged"]]
    declined_responses = [
        e["author_responses"] for e in samples["per_pr"] if not e["merged"]
    ]

    # size: commits per PR, plus the optional changed-line size
    sizes = _pr_sizes(conn)
    lines = [
        sizes[state.pr["object_id"]]
        for state in cohort if state.pr["object_id"] in sizes
    ]
    review_by_pr = []
    for state in court.prs:
        if not (start <= state.pr["created_ts"] <= end):
            continue
        who = state.pr["actor"]
        last_push = state.pr["created_ts"]
        for event in state.events:
            if event["activity"] == "pr_pushed":
                last_push = event["_ts"]
            elif event["activity"] in REVIEW_ACTIONS and event["actor"] != who:
                review_by_pr.append((
                    len(state.commits),
                    court.business_hours(event["actor"], last_push, event["_ts"]),
                ))
    elasticity = _size_elasticity(review_by_pr)

    # -- what the components do not explain ---------------------------------- #
    # The decomposition above prices every review wait, author push and merge
    # handover. Whatever is left of the observed cycle (time the ball spent
    # somewhere the three components do not name) is carried as one measured
    # term rather than silently dropped, so the parts add up to the whole.
    residuals = [
        max(0.0, entry["cycle"] - entry["review"] - entry["author"] - entry["merge"])
        for entry in samples["per_pr"]
        if entry["merged"] and entry["cycle"] > 0
    ]
    cycle_means = [
        entry["cycle"] for entry in samples["per_pr"]
        if entry["merged"] and entry["cycle"] > 0
    ]
    unmeasured_fit = fit_lognormal(residuals)
    unmeasured_mean = _mean(residuals)
    cycle_mean = _mean(cycle_means)

    # -- history (the validation gate's other side) ------------------------- #
    from analysis import rules as rules_module

    historical_window = (start, end)
    cycle = rules_module.cycle_time(court, historical_window)
    efficiency = rules_module.flow_efficiency(court, historical_window)

    merged = [state for state in cohort if state.pr["state"] == "merged"]
    calibration = {
        "ok": True,
        "available": True,
        "hours_mode": court.hours_mode,
        "assumption": court.assumption,
        "window": {
            "start": start.isoformat(sep=" ", timespec="minutes"),
            "end": end.isoformat(sep=" ", timespec="minutes"),
            "days": round((end - start).total_seconds() / 86400.0, 1),
            "business_days": round(business_days, 1),
            "business_hours": round(business_hours, 1),
            "hours_per_day": round(bh_per_day, 1),
        },
        "prs": {
            "n": len(cohort),
            "merged": len(merged),
            "declined": sum(1 for s in cohort if s.pr["state"] == "declined"),
            "open": sum(1 for s in cohort if s.pr["closed_ts"] is None),
        },
        "decline": {
            "probability": round(
                sum(1 for s in cohort if s.pr["state"] == "declined") / len(cohort), 3
            ) if cohort else 0.0,
            "note": (
                "share of PRs abandoned before the mandatory gate; a declined PR "
                "consumed review time but never merged, so it is censored out of "
                "the cycle-time median exactly like the historical metric"
            ),
        },
        "arrival": {
            "per_business_day": round(len(cohort) / business_days, 3),
            "per_business_hour": round(len(cohort) / business_hours, 4) if business_hours else 0.0,
            "mean_interarrival_hours": round(business_hours / len(cohort), 2) if cohort else None,
            "model": "exponential inter-arrival (Poisson arrivals)",
        },
        "review_service": {
            "pooled": pooled_review,
            "by_reviewer": {
                entry["person"]: entry["review"]["service"]
                for entry in reviewers if not entry["sentinel"]
            },
        },
        "author_service": {
            "pooled": pooled_author,
            "by_author": {
                entry["person"]: entry["author"]["service"]
                for entry in reviewers if not entry["sentinel"]
            },
        },
        "sentinel": {
            "actors": sentinels,
            "observed": {
                "mean_hours": sentinel_observed,
                "n": sum(len(samples["review"].get(p, [])) for p in sentinels),
            },
            "service": service_fit(sentinel_fit, sentinel_observed,
                                   sentinel_split["service_scale"]),
            "attention": service_fit(sentinel_fit, sentinel_observed,
                                     sentinel_split["attention_scale"],
                                     max_sigma=ATTENTION_MAX_SIGMA),
            "decomposition": sentinel_split,
            "rate_per_hour": round(sentinel_rate, 4),
            "utilisation": sentinel_split["utilisation"],
            "note": (
                "the mandatory-approval gate: its service time, attention latency "
                "and utilisation are the decomposition of the observed response "
                "(see 'queueing')"
            ),
        },
        "merge_delay": merge_fit,
        "unmeasured": {
            "fit": unmeasured_fit,
            "mean_hours": unmeasured_mean,
            "share_of_cycle": (
                round(unmeasured_mean / cycle_mean, 3)
                if unmeasured_mean is not None and cycle_mean else None
            ),
            "n": len(residuals),
            "carried_as": "fixed mean delay",
            "note": (
                "the part of the measured cycle that the review waits, the author "
                "pushes and the merge handover do not account for (the ball was "
                "with someone without producing one of those events). Carried as "
                "one delay so the components add up to the cycle they were "
                "calibrated from. It is a *balancing item*, not a mechanism: only "
                "its mean is identified, and it is negatively correlated with the "
                "parts by construction (a PR with long reviews has almost no "
                "residue), so it is carried as its measured mean rather than as an "
                "independent heavy-tailed draw that would re-add the cycle's tail"
            ),
        },
        "rework": {
            "probability": round(
                sum(1 for count in pushes if count > 0) / len(pushes), 3
            ) if pushes else 0.0,
            "definition": "share of PRs with more than one push (a rework round)",
            "pushes": {"histogram": _histogram(pushes), "mean": _mean([float(p) for p in pushes])},
            "author_responses": {
                "histogram": _histogram(samples["author_per_pr"]),
                "mean": _mean([float(r) for r in samples["author_per_pr"]]),
                "merged_histogram": _histogram(merged_responses),
                "declined_histogram": _histogram(declined_responses),
                "mean_merged": _mean([float(r) for r in merged_responses]),
                "basis": "outcome-conditional",
                "note": (
                    "how many times the author answered a reviewer action with a "
                    "push — the model's rework count (a response to a comment that "
                    "precedes the first push is one too, so it is not pushes-1). "
                    "The model draws the merged histogram for a PR that will merge "
                    "and the declined one for a PR that will not: a PR abandoned "
                    "before the gate stops early, so the pooled histogram is short "
                    "of the rounds a merged PR actually had"
                ),
            },
            "peer_actions": {
                "histogram": _histogram(rounds),
                "mean": _mean([float(r) for r in rounds]),
                "merged_histogram": _histogram(merged_rounds),
                "declined_histogram": _histogram(declined_rounds),
                "mean_merged": _mean([float(r) for r in merged_rounds]),
                "basis": "outcome-conditional",
            },
            "push_class": push_class,
            "push_class_note": (
                "content = real rework; pure_rebase / rebase_conflict = a lost "
                "approval that bought no review value (rebase waste)"
            ),
        },
        "reviewers": reviewers,
        "pool": {
            "people": pool,
            "peers": peers,
            "sentinels": sentinels,
            "capacity_per_person": 1,
            "note": (
                "a reviewer is a capacity-1 server shared with that person's own "
                "author work: the same people review and author"
            ),
        },
        "pr_size": {
            "commits": {
                "median": _median([float(c) for c in samples["commits"]]),
                "p90": _quantile([float(c) for c in samples["commits"]], 0.9),
                "histogram": _histogram(samples["commits"]),
            },
            "lines": {
                "available": bool(lines),
                "n": len(lines),
                "median": _median([float(v) for v in lines]),
                "p90": _quantile([float(v) for v in lines], 0.9),
            },
            "elasticity": elasticity,
        },
        "stack_depth": _stack_depth(court),
        # prd/42 M3: the refinement knob's measured cost (created → ready).
        # prd/68 F-04: one block on every path, so a Jira-only store keeps it.
        "refinement": _refinement_block(refinement),
        "queueing": {
            "model": (
                "M/G/1 with a realised-queue factor: "
                "R = attention + service + QUEUE_RECOVERY * (rho/(1-rho)) * "
                "service * (1+CV^2)/2"
            ),
            "service_share": SERVICE_SHARE,
            "queue_recovery": QUEUE_RECOVERY,
            "attention_max_sigma": ATTENTION_MAX_SIGMA,
            "note": (
                "an observed review response is split into three parts: waiting "
                "for the reviewer to look (attention), the review work itself "
                "(service), and capacity contention (queue). Response times alone "
                "cannot tell a slow reviewer from an overloaded one, so the split "
                "uses two disclosed calibration shares: a fixed share of each "
                "response is treated as review work, and a fixed share of the "
                "textbook queueing estimate is assumed to materialise, because "
                "this pipeline smooths arrivals before the reviewer sees them "
                "(the measured share is 52–70 % on these datasets). Without the "
                "second adjustment the model under-predicted the cycle-time median "
                "by 31 % on a wall-clock axis. A third constant keeps the "
                "response-time spread below the work's, so the heavy tail is "
                "priced as queue and work, not as attention. The per-person "
                "breakdown below shows the three parts separately."
            ),
            "min_fit_samples": MIN_FIT_SAMPLES,
        },
        "historical": {
            "cycle_median_hours": cycle["median_hours"],
            "cycle_p90_hours": cycle["p90_hours"],
            "cycle_mean_hours": cycle_mean,
            "flow_efficiency": efficiency["aggregate_efficiency"],
            "n_merged": cycle["n"],
            "cohort": cycle["cohort"],
            "censored": cycle["censored"],
        },
        "provenance": (
            f"calibrated from {len(cohort)} PRs, "
            f"{start.date().isoformat()}→{end.date().isoformat()}"
        ),
    }
    return calibration


# --------------------------------------------------------------------------- #
# the model (SimPy)
# --------------------------------------------------------------------------- #
class _Server:
    """One capacity-limited person (or sentinel slot) and its queue statistics."""

    def __init__(self, env, name: str, capacity: int = 1):
        self.name = name
        self.env = env
        self.resource = simpy.PriorityResource(env, capacity=capacity)
        self.busy = 0.0
        self.requests = 0
        self.wait_hours = 0.0
        self.peak_queue = 0
        self.escalated = 0

    def queue_length(self) -> int:
        return len(self.resource.queue)

    def serve(self, service: float, review: bool, review_first: bool,
              attention: float = 0.0, sla: float | None = None, overflow=None):
        """Yield until served; returns the elapsed response (attention + wait).

        ``attention`` is the latency before the request joins the queue (the
        person has not looked yet) — a cadence, not a capacity, so it is a plain
        delay and it is **not** charged to the server's busy time.

        ``review_first`` gives review requests priority 0 over author work
        (priority 1); otherwise both share priority 0 and the queue is FIFO.
        With an ``sla`` and an ``overflow`` server the wait is capped: attention
        is truncated at the SLA, and a request that has queued past the rest of
        the budget is cancelled here and served by the overflow lane.
        """
        env = self.env
        if sla is not None:
            attention = min(attention, sla)
            budget = max(0.05, sla - attention)
        else:
            budget = None
        if attention > 0:
            yield env.timeout(attention)
        self.requests += 1
        priority = 0 if (review and review_first) else 1 if (review_first and not review) else 0
        started = env.now
        self.peak_queue = max(self.peak_queue, self.queue_length() + 1)
        request = self.resource.request(priority=priority)
        if budget is not None and overflow is not None:
            outcome = yield request | env.timeout(budget)
            if request not in outcome:
                request.cancel()
                self.escalated += 1
                self.wait_hours += budget
                yield from overflow.serve(service, review, False)
                return attention + budget
        else:
            yield request
        wait = env.now - started
        self.wait_hours += wait
        yield env.timeout(service)
        self.busy += service
        self.resource.release(request)
        return attention + wait


class _PR:
    """One simulated PR: what it was, when it started, when it finished."""

    __slots__ = ("author", "commits", "peer_actions", "author_pushes", "depth",
                 "parent", "arrived_at", "started_at", "finished_at", "merged_at",
                 "abandoned", "touches", "author_hours", "rebase_hours", "done",
                 "merge_outcome")

    def __init__(self, env, author, commits, peer_actions, author_pushes, depth,
                 parent, merge_outcome: bool = True):
        self.author = author
        self.commits = commits
        self.peer_actions = peer_actions
        self.author_pushes = author_pushes
        self.depth = depth
        self.parent = parent
        # the outcome is drawn *before* the plan, so the plan can use the
        # outcome-conditional action counts the store measured (a PR abandoned
        # before the gate stops early, so it has fewer rounds)
        self.merge_outcome = merge_outcome
        self.arrived_at = 0.0
        self.started_at = 0.0
        self.finished_at = None
        self.merged_at = None
        self.abandoned = False
        self.touches = 0
        self.author_hours = 0.0
        self.rebase_hours = 0.0
        # the PR's *outcome* (merged or abandoned), which is what a stacked
        # child waits on: a merge predicate can never fire for an abandoned
        # base (FR-3)
        self.done = simpy.Event(env)


class _Lab:
    """The calibrated model, ready to run one scenario (one replication)."""

    def __init__(self, calibration: dict, scenario: Scenario, seed: int,
                 horizon_hours: float, bh_per_day: float):
        if simpy is None:  # pragma: no cover - requirements.txt installs it
            raise SimulationError(
                "SimPy is not installed, so the policy lab cannot run "
                "(pip install simpy)"
            )
        self.calibration = calibration
        self.scenario = scenario
        self.rng = random.Random(seed)
        self.bh_per_day = bh_per_day
        self.horizon = horizon_hours
        self.env = simpy.Environment()

        pool = calibration["pool"]
        self.peers = list(pool["peers"]) or list(pool["people"])
        self.sentinels = list(pool["sentinels"])
        self.authors = list(self.peers) or list(pool["people"])
        self.servers: dict[str, _Server] = {}
        self.person_service: dict[str, dict] = {}
        for entry in calibration["reviewers"]:
            person = entry["person"]
            # concepts.md §2/§6: a sentinel's queue *is* the sentinel gate, which
            # is modelled by the ``sentinel:N`` slots below. Giving the person a
            # second, never-used reviewer server made "who is busy" list the
            # gate and an idle 0%-busy person as two rows for the same load.
            if person in set(self.sentinels):
                self.person_service[person] = entry
                continue
            self.servers[person] = _Server(self.env, person, capacity=1)
            self.person_service[person] = entry
        self.wip = {
            person: simpy.Resource(self.env, capacity=scenario.wip_limit or 10**6)
            for person in self.authors
        }
        # the sentinel gate: `sentinels` parallel approval slots (a rotation)
        self.sentinel_servers = [
            _Server(self.env, f"sentinel:{index + 1}", capacity=1)
            for index in range(max(1, scenario.sentinels or 1))
        ]
        # review-SLA overflow lane: any free reviewer may pick up an overdue review
        self.overflow = _Server(
            self.env, "escalation",
            capacity=max(1, len(self.peers) + len(self.sentinel_servers)),
        )
        self.median_commits = calibration["pr_size"]["commits"]["median"] or 1.0
        self.elasticity = calibration["pr_size"]["elasticity"]["used"]
        self.decline_probability = (
            calibration.get("decline", {}).get("probability") or 0.0
        )
        # the residue is a *balancing item* (cycle - review - author - merge):
        # only its mean is identified, so it is carried as a fixed delay rather
        # than drawn from the fitted lognormal (an independent heavy draw would
        # re-add the cycle's tail, which the residue already balances)
        self.unmeasured_hours = (calibration.get("unmeasured") or {}).get("mean_hours") or 0.0
        # prd/42 M3: refinement is the measured created → ready segment. It is
        # a delay before the first review, not a capacity-consuming service.
        # prd/64 F-03: None means the store has no measured segment; `run`
        # refuses the scenario before the lab is built, and the lifecycle below
        # treats None as an error rather than an implicit zero.
        self.refinement_hours = (calibration.get("refinement") or {}).get("hours")
        self.backlog = 0
        self.max_backlog = 0
        self.prs: list[_PR] = []
        # a stack is one author's chain: keying by depth alone parented a PR to
        # an unrelated author's PR at the same level
        self._last_by_depth: dict[tuple[str, int], _PR] = {}
        self.stack_wait_timeouts = 0

    # -- service draws ------------------------------------------------------ #
    def _size_factor(self, commits: int) -> float:
        if not self.elasticity:
            return 1.0
        ratio = max(0.1, commits / max(1.0, self.median_commits))
        return ratio ** self.elasticity

    def review_service(self, person: str) -> tuple[float, float]:
        """``(attention, service)`` for one review request on ``person``."""
        entry = self.person_service.get(person, {}).get("review", {})
        service_fit_ = entry.get("service") or self.calibration["review_service"]["pooled"]
        attention_fit = entry.get("attention") or {"mu": None}
        attention = (
            sample_lognormal(self.rng, attention_fit)
            if attention_fit.get("mu") is not None else 0.0
        )
        return attention, sample_lognormal(self.rng, service_fit_)

    def author_service(self, person: str) -> tuple[float, float]:
        """``(attention, service)`` for one rework push by ``person``."""
        entry = self.person_service.get(person, {}).get("author", {})
        service_fit_ = entry.get("service") or {}
        if (service_fit_ or {}).get("mu") is None:
            service_fit_ = self.calibration["author_service"]["pooled"] or {}
        if (service_fit_ or {}).get("mu") is None:
            # the window observed no author response at all (no rework), so a
            # rework push borrows the pooled review service -- one handover of
            # work -- instead of refusing to run or pricing the rebase as free
            service_fit_ = self.calibration["review_service"]["pooled"]
        attention_fit = entry.get("attention") or {"mu": None}
        attention = (
            sample_lognormal(self.rng, attention_fit)
            if attention_fit.get("mu") is not None else 0.0
        )
        return attention, sample_lognormal(self.rng, service_fit_)

    def sentinel_service(self) -> tuple[float, float]:
        entry = self.calibration["sentinel"]
        attention_fit = entry.get("attention") or {"mu": None}
        attention = (
            sample_lognormal(self.rng, attention_fit)
            if attention_fit.get("mu") is not None else 0.0
        )
        return attention, sample_lognormal(self.rng, entry["service"])

    def merge_service(self) -> float:
        fit = self.calibration["merge_delay"]
        if not fit or fit.get("mu") is None:
            # no merge handover was observed in the window: a merge click is a
            # floor, not a fitted distribution
            return FLOOR_HOURS
        return sample_lognormal(self.rng, fit)

    def pick_peer(self, author: str) -> str:
        choices = [person for person in self.peers if person != author] or self.peers
        return self.rng.choice(choices)

    def pick_sentinel(self) -> _Server:
        # least-loaded slot, so two sentinels actually share the gate
        return min(self.sentinel_servers, key=lambda server: server.queue_length())

    # -- the plan of one PR -------------------------------------------------- #
    def _plan(self, commits: int, merged: bool = True) -> tuple[int, int]:
        """The PR's review rounds and author responses, conditional on outcome.

        The store's action counts are outcome-conditional (a PR abandoned before
        the gate stops early), and the gate scores merged PRs only, so a merged
        PR draws the merged histogram and a declining one the declined
        histogram. The pooled histogram is the fallback for a store whose
        window has no declined PRs at all.
        """
        rework = self.calibration["rework"]
        actions_hist = rework["peer_actions"]
        responses_hist = rework.get("author_responses") or rework["pushes"]
        actions = max(1, _weighted_choice(
            self.rng,
            actions_hist.get("merged_histogram" if merged else "declined_histogram")
            or actions_hist["histogram"],
            2,
        ))
        pushes = _weighted_choice(
            self.rng,
            responses_hist.get("merged_histogram" if merged else "declined_histogram")
            or responses_hist["histogram"],
            0,
        )
        # prd/42 M3: a review-round cap stops the loop at the cap. Refinement
        # before the first review is modelled as catching one round before it
        # happens — the same assumption the rule's advice makes ("refine before
        # the sprint"), stated here rather than hidden.
        cap = self.scenario.review_round_cap
        if cap is not None:
            actions = min(actions, max(1, cap))
        if self.scenario.refine_before_start:
            actions = max(1, actions - 1)
        # a rework push needs a comment before it: the approval is the last action
        return actions, max(0, min(pushes, actions - 1))

    def _depth(self) -> int:
        depth = self.calibration["stack_depth"]
        if not depth.get("available") or not depth.get("histogram"):
            return 0
        drawn = _weighted_choice(self.rng, depth["histogram"], 0)
        cap = self.scenario.stack_depth_cap
        if cap is not None and drawn > cap:
            return 0
        return drawn

    def _generate(self):
        arrival = self.calibration["arrival"]
        rate = arrival["per_business_hour"]
        if not rate:
            return
        commits_histogram = self.calibration["pr_size"]["commits"]["histogram"]
        while True:
            yield self.env.timeout(self.rng.expovariate(rate))
            if self.env.now > self.horizon:
                return
            commits = _weighted_choice(self.rng, commits_histogram, 4) or 1
            cap = self.scenario.size_cap_commits
            splits = 1
            if cap and commits > cap:
                splits = min(12, math.ceil(commits / cap))
                commits = max(1, round(commits / splits))
            author = self.rng.choice(self.authors)
            # the outcome is drawn first: the plan is conditional on it
            merge_outcome = self.rng.random() >= self.decline_probability
            actions, pushes = self._plan(commits, merge_outcome)
            depth = self._depth()
            parent = self._last_by_depth.get((author, depth - 1)) if depth > 0 else None
            for _ in range(splits):
                pr = _PR(self.env, author, commits, actions, pushes, depth, parent,
                         merge_outcome)
                pr.arrived_at = self.env.now
                if depth > 0:
                    self._last_by_depth[(author, depth)] = pr
                self.prs.append(pr)
                self.env.process(self._lifecycle(pr))

    def _author_work(self, pr: _PR, attention: float, service: float):
        server = self.servers.get(pr.author)
        if server is None:
            yield self.env.timeout(attention + service)
            return
        yield from server.serve(
            service, False, self.scenario.review_first, attention=attention
        )

    def _lifecycle(self, pr: _PR):
        env = self.env
        scenario = self.scenario
        sla = scenario.review_sla_hours
        first = scenario.review_first
        self.backlog += 1
        self.max_backlog = max(self.max_backlog, self.backlog)
        admission = self.wip[pr.author].request()
        yield admission
        self.backlog -= 1
        pr.started_at = env.now
        if scenario.refine_before_start:
            if self.refinement_hours is None:
                raise SimulationError(
                    "refine before start needs a measured created → ready segment"
                )
            # the measured created → ready refinement, before the first review
            yield env.timeout(self.refinement_hours)
        if self.unmeasured_hours > 0:
            # the measured residue: handover time no component names. It is a
            # balancing item, so it is carried at its mean (see _Lab.__init__)
            yield env.timeout(self.unmeasured_hours)
        peer = self.pick_peer(pr.author)
        peer_server = self.servers.get(peer)
        factor = self._size_factor(pr.commits)
        pushes = 0
        for index in range(pr.peer_actions):
            attention, service = self.review_service(peer)
            if peer_server is not None:
                yield from peer_server.serve(
                    service * factor, True, first, attention=attention,
                    sla=sla, overflow=self.overflow,
                )
            else:
                yield env.timeout(attention + service * factor)
            pr.touches += 1
            if index < pr.peer_actions - 1 and pushes < pr.author_pushes:
                attention, service = self.author_service(pr.author)
                started = env.now
                yield from self._author_work(pr, attention, service)
                # the author holds the ball for their whole response: the store's
                # author-ball interval is the active time (rules.flow_efficiency)
                pr.author_hours += env.now - started
                pushes += 1
        # A PR can be abandoned before the mandatory gate (declined, or still open
        # at the snapshot). It consumed review time but never merged, so it is
        # censored out of the cycle-time median exactly like the historical metric.
        # The outcome was drawn before the plan (see _plan): a PR that will be
        # abandoned also planned fewer review rounds, as the store shows.
        if not pr.merge_outcome:
            pr.abandoned = True
            pr.finished_at = env.now
            self.wip[pr.author].release(admission)
            pr.done.succeed()
            return
        # mandatory approval
        slot = self.pick_sentinel()
        attention, service = self.sentinel_service()
        yield from slot.serve(
            service * factor, True, first, attention=attention,
            sla=sla, overflow=self.overflow,
        )
        pr.touches += 1
        # Stacked PRs cannot land before their base. The base's outcome is what
        # matters, not a merge: an abandoned base can never merge, so waiting for
        # `merged_at` stalled the child (and the whole run) forever (FR-3).
        # Either outcome moves the ground under the child -- it landed, or it was
        # abandoned and the child re-targets the trunk -- so either way it costs
        # one rebase and one re-review round.
        if pr.parent is not None and pr.parent.merged_at is None:
            if not pr.parent.done.triggered:
                bound = max(1.0, self.horizon * STACK_WAIT_HORIZONS)
                yield pr.parent.done | env.timeout(bound)
                if not pr.parent.done.triggered:
                    # the base never resolved: count it and re-target rather
                    # than spin. The guard should be unreachable now, but a
                    # silent hang is worse than a counted one.
                    self.stack_wait_timeouts += 1
            attention, rebase = self.author_service(pr.author)
            started = env.now
            yield from self._author_work(pr, attention, rebase)
            pr.rebase_hours += env.now - started
            attention, service = self.review_service(peer)
            if peer_server is not None:
                yield from peer_server.serve(
                    service * factor, True, first, attention=attention,
                )
            else:
                yield env.timeout(attention + service * factor)
            pr.touches += 1
        # the merge handover is a *delay*, not a capacity-consuming service: the
        # author noticing an approval and clicking merge is attention latency, so
        # charging it to their capacity would saturate a team that is not busy
        yield env.timeout(self.merge_service())
        pr.merged_at = env.now
        pr.finished_at = env.now
        self.wip[pr.author].release(admission)
        pr.done.succeed()

    # -- run one replication ------------------------------------------------ #
    def run(self) -> dict:
        self.env.process(self._generate())
        self.env.run(until=self.horizon)
        # drain: PRs already in flight finish, so the sample is not truncated
        guard = 0
        while any(pr.finished_at is None for pr in self.prs) and guard < MAX_DRAIN_STEPS:
            self.env.run(until=self.env.now + 1.0)
            guard += 1
        merged = [pr for pr in self.prs if pr.merged_at is not None]
        resources: dict[str, dict] = {}
        for person, server in self.servers.items():
            resources[person] = {
                "person": person,
                "role": "sentinel" if person in self.sentinels else "reviewer",
                "busy": server.busy,
                "requests": server.requests,
                "wait": server.wait_hours,
                "peak_queue": server.peak_queue,
            }
        for server in self.sentinel_servers:
            resources[server.name] = {
                "person": server.name,
                "role": "sentinel",
                "busy": server.busy,
                "requests": server.requests,
                "wait": server.wait_hours,
                "peak_queue": server.peak_queue,
            }
        return {
            "horizon": self.horizon,
            # how far past the horizon the drain ran: the divergence signal that
            # the old utilisation denominator used to swallow (NFR-1)
            "drain": max(0.0, self.env.now - self.horizon),
            "cycles": [pr.merged_at - pr.started_at for pr in merged],
            "leads": [pr.merged_at - pr.arrived_at for pr in merged],
            "active": [pr.author_hours + pr.rebase_hours for pr in merged],
            "touches": [pr.touches for pr in merged],
            # prd/62: a merged PR's review rounds. The last reviewer action is
            # the approval, so rounds = max(0, reviewer actions - 1); the same
            # fact the round-cap guardrail reports.
            "rounds": [max(0, pr.peer_actions - 1) for pr in merged],
            "resources": resources,
            "prs": len(self.prs),
            "merged": len(merged),
            # prd/37 §5: throughput must count delivered *work*, not PR count.
            # A PR's work unit is its commits: the size-cap knob splits a PR
            # into more PRs carrying the same commits, so PR/day would rise
            # while nothing more is delivered.
            "work": sum(pr.commits for pr in merged),
            "work_unit": "commits",
            "declined": sum(1 for pr in self.prs if pr.abandoned),
            "backlog_max": self.max_backlog,
            "backlog_end": self.backlog,
            "escalated": self.overflow.requests,
            "stack_wait_timeouts": self.stack_wait_timeouts,
        }


def _summary(calibration: dict, scenario: Scenario, pooled: dict,
             replications: int, horizon_hours: float, bh_per_day: float) -> dict:
    """Pool the replications into the payload the UI and the gate read."""
    cycles = pooled["cycles"]
    leads = pooled["leads"]
    active = pooled["active"]
    touches = pooled["touches"]
    rounds = pooled.get("rounds") or []
    elapsed = sum(cycles)
    working = sum(active) + sum(touches) * TOUCH_HOURS
    # Utilisation divides by the *modelled horizon*, not by the drained span
    # (NFR-1). The drain runs past the horizon by however long the queues take,
    # and it is longest exactly when a server is saturated -- dividing by it
    # would hide the saturation it exists to expose. A server that was busy in
    # the drain can therefore read above 1.0, which is the honest signal.
    busy_hours = max(1e-9, horizon_hours * replications)
    drain_hours = pooled.get("drain", 0.0) / max(1, replications)
    resources = []
    for name, entry in pooled["resources"].items():
        resources.append({
            "person": entry["person"],
            "role": entry["role"],
            "utilisation": round(entry["busy"] / busy_hours, 4),
            "busy_hours": round(entry["busy"] / replications, 2),
            # prd/63 V-B: requests are pooled over every replication, so a
            # 6-replication run showed "≈640" (a 6x sum). Quote the number per
            # replication — the same basis as busy hours and the queue.
            "requests": round(entry["requests"] / replications, 1),
            "requests_total": entry["requests"],
            "mean_wait_hours": _round(
                entry["wait"] / entry["requests"] if entry["requests"] else None
            ),
            # Little's law against the same modelled horizon, so the queue and
            # the utilisation share one denominator
            "mean_queue": round(entry["wait"] / busy_hours, 2),
            "peak_queue": entry["peak_queue"],
        })
    resources.sort(key=lambda row: (-row["utilisation"], row["person"]))
    bottleneck = resources[0] if resources else None
    horizon_days = max(1e-9, horizon_hours / bh_per_day)
    return {
        "ok": True,
        "available": True,
        "predicted": {
            "cycle_hours": {
                "n": len(cycles),
                "median": _median(cycles),
                "p90": _quantile(cycles, 0.9),
                "p25": _quantile(cycles, 0.25),
                "mean": _mean(cycles),
            },
            "lead_hours": {
                "n": len(leads),
                "median": _median(leads),
                "p90": _quantile(leads, 0.9),
            },
            "flow_efficiency": {
                "aggregate": round(working / elapsed, 4) if elapsed else None,
                "median": _median([
                    (author + count * TOUCH_HOURS) / max(1e-9, cycle)
                    for author, count, cycle in zip(active, touches, cycles)
                ]),
                "working_hours": round(working, 1),
                "elapsed_hours": round(elapsed, 1),
                "touch_hours": TOUCH_HOURS,
            },
            # PRs delivered per business day. Kept for context, but never the
            # headline when a size cap is on: many small PRs are not more work.
            "throughput_per_day": round(
                len(cycles) / (horizon_days * replications), 3
            ),
            # prd/37 §5: delivered work per business day (commits), the honest
            # A/B throughput — the size-cap knob preserves the work and changes
            # only how it is packaged.
            "throughput_work_per_day": round(
                pooled.get("work", 0) / (horizon_days * replications), 3
            ),
            "work_units": pooled.get("work", 0),
            "work_unit": pooled.get("work_unit", "commits"),
            "prs": pooled["prs"],
            "merged": pooled["merged"],
            "declined": pooled["declined"],
            "censored": pooled["prs"] - pooled["merged"] - pooled["declined"],
            # prd/62: the guardrail facts every speed lever must state. Rounds is
            # a run value over merged PRs (reviewer actions - 1); its exact bound
            # under a round cap is what the copy may claim.
            "rounds": {
                "n": len(rounds),
                "mean": _round(_mean([float(value) for value in rounds])),
                "p50": _quantile(rounds, 0.5),
                "p85": _quantile(rounds, 0.85),
            },
            # packaging: PRs per 100 delivered commits. A size cap splits a PR
            # into more PRs carrying the same work, so this rises while
            # delivered work does not (the +289 % artefact, made visible).
            "prs_per_work_unit": _round(
                pooled["merged"] / max(1, pooled.get("work", 0)), 4
            ),
            "prs_per_100_commits": _round(
                100.0 * pooled["merged"] / max(1, pooled.get("work", 0)), 1
            ),
            "escalated_share": _round(
                pooled["escalated"] / pooled["prs"] if pooled["prs"] else None, 4
            ),
            "declined_per_100": _round(
                100.0 * pooled["declined"] / pooled["prs"] if pooled["prs"] else None, 1
            ),
            "backlog_max": pooled["backlog_max"],
            "backlog_end": pooled["backlog_end"],
            "replications": replications,
        },
        "bottleneck": (
            {
                "person": bottleneck["person"],
                "role": bottleneck["role"],
                "utilisation": bottleneck["utilisation"],
                "mean_queue": bottleneck["mean_queue"],
                "mean_wait_hours": bottleneck["mean_wait_hours"],
                "note": (
                    "highest-utilisation capacity-1 server: at ~1.0 a little slack "
                    "(or one more server) buys more than any other change"
                    if bottleneck["utilisation"] >= 0.85 else
                    "no server is saturated; the remaining wait is attention "
                    "latency, not capacity (slow vs overloaded server)"
                ),
            } if bottleneck else None
        ),
        "resources": resources,
        "queues": {
            "escalated": pooled["escalated"],
            "mean_queue_hours": round(sum(row["mean_queue"] for row in resources), 2),
        },
        "warnings": [
            f"{row['person']} is saturated (utilisation {row['utilisation']:.0%}): its "
            "queue grows without bound, so the predicted distribution depends on the "
            "horizon rather than settling"
            for row in resources if row["utilisation"] >= 0.9
        ] + ([
            f"the drain ran {drain_hours:.0f} h past the {horizon_hours:.0f} h "
            "horizon: the run did not settle, so the predicted distribution "
            "depends on the horizon"
        ] if drain_hours > horizon_hours / 2.0 else []),
        "scenario": scenario.as_dict(),
        "horizon_business_days": round(horizon_days, 1),
        "drain_hours": round(drain_hours, 2),
        "stack_wait_timeouts": pooled.get("stack_wait_timeouts", 0),
    }


def run(calibration: dict, scenario: Scenario | None = None, seed: int = DEFAULT_SEED,
        horizon_business_days: float | None = None,
        replications: int = DEFAULT_REPLICATIONS) -> dict:
    """Run one scenario against a calibration. Deterministic for a fixed seed.

    ``replications`` independent runs (seeds ``seed … seed+n-1``) are pooled, so
    a 60-business-day window does not let Monte-Carlo noise decide whether a
    policy "works". A/B comparisons reuse the same seeds (common random
    numbers), so the *difference* is the policy, not the draw.
    """
    if not calibration or not calibration.get("available"):
        return {
            "ok": False,
            "available": False,
            "note": (calibration or {}).get(
                "note", "the store has nothing to calibrate a model from"
            ),
        }
    scenario = scenario or Scenario()
    inputs = scenario_inputs(calibration, scenario)
    if not inputs["available"]:
        # prd/64 F-03: a knob whose calibration input is None is not modelled
        # at a default. The result says why, and callers must not show a delta.
        return scenario_unavailable(scenario, inputs)
    replications = max(1, min(MAX_REPLICATIONS, int(replications)))
    bh_per_day = calibration["window"].get("hours_per_day") or DEFAULT_BH_PER_DAY
    days = horizon_business_days or calibration["window"]["business_days"]
    horizon_hours = max(1.0, float(days) * bh_per_day)
    pooled = {
        "drain": 0.0, "cycles": [], "leads": [], "active": [], "touches": [],
        "rounds": [],
        "resources": {}, "prs": 0, "merged": 0, "declined": 0, "work": 0,
        "backlog_max": 0, "backlog_end": 0, "escalated": 0,
        "stack_wait_timeouts": 0,
    }
    per_replication: list[float] = []
    for index in range(replications):
        lab = _Lab(
            calibration, scenario, seed + index,
            horizon_hours=horizon_hours, bh_per_day=bh_per_day,
        )
        part = lab.run()
        pooled["drain"] += part["drain"]
        if part["cycles"]:
            per_replication.append(statistics.median(part["cycles"]))
        for key in ("cycles", "leads", "active", "touches", "rounds"):
            pooled[key].extend(part[key])
        for name, entry in part["resources"].items():
            target = pooled["resources"].setdefault(name, dict(entry, busy=0.0, wait=0.0,
                                                               requests=0, peak_queue=0))
            target["busy"] += entry["busy"]
            target["wait"] += entry["wait"]
            target["requests"] += entry["requests"]
            target["peak_queue"] = max(target["peak_queue"], entry["peak_queue"])
        pooled["prs"] += part["prs"]
        pooled["merged"] += part["merged"]
        pooled["declined"] += part["declined"]
        pooled["work"] += part["work"]
        pooled["backlog_max"] = max(pooled["backlog_max"], part["backlog_max"])
        pooled["backlog_end"] += part["backlog_end"]
        pooled["escalated"] += part["escalated"]
        pooled["stack_wait_timeouts"] += part["stack_wait_timeouts"]
    result = _summary(calibration, scenario, pooled, replications,
                      horizon_hours, bh_per_day)
    result["seed"] = seed
    # the spread of the per-replication median: a delta smaller than this is
    # Monte-Carlo noise, not a policy effect, and the A/B says so
    low = min(per_replication) if per_replication else None
    high = max(per_replication) if per_replication else None
    result["predicted"]["spread"] = {
        "replications": replications,
        "median_min": _round(low),
        "median_max": _round(high),
        # the pooled median is estimated from all replications, so its
        # uncertainty is the standard error of the per-replication medians —
        # not their full range, which would call every small effect "noise"
        "standard_error": (
            _round(abs(high - low) / 2.0 / math.sqrt(replications))
            if low is not None else None
        ),
    }
    return result


def validate(calibration: dict, baseline: dict | None = None,
             tolerance: float = DEFAULT_TOLERANCE, seed: int = DEFAULT_SEED) -> dict:
    """The honesty device (prd/23): does the baseline reproduce the history?

    Compares the baseline's predicted cycle-time median with the historical
    median on the same store and window. ``ok`` is the gate: no scenario result
    is shown until it passes. The mismatch is always reported, pass or fail, so
    a reader can see how close "close enough" was.
    """
    historical = (calibration or {}).get("historical") or {}
    expected = historical.get("cycle_median_hours")
    baseline = baseline or run(calibration, Scenario(), seed=seed)
    predicted = (
        baseline.get("predicted", {}).get("cycle_hours", {}).get("median")
        if baseline.get("ok") else None
    )
    if expected is None or predicted is None:
        return {
            "ok": False,
            "status": "unavailable",
            "tolerance": tolerance,
            "historical_median_hours": expected,
            "predicted_median_hours": predicted,
            "percent_off": None,
            "note": "no historical median or no predicted median to compare",
        }
    off = (predicted - expected) / expected if expected else None
    passed = off is not None and abs(off) <= tolerance
    # the p90 is a secondary check: the gate is the median (prd/23), but a model
    # that matches the median and misses the tail is worth saying out loud
    expected_p90 = historical.get("cycle_p90_hours")
    predicted_p90 = (
        baseline.get("predicted", {}).get("cycle_hours", {}).get("p90")
        if baseline.get("ok") else None
    )
    off_p90 = (
        (predicted_p90 - expected_p90) / expected_p90
        if expected_p90 and predicted_p90 is not None else None
    )
    return {
        "ok": passed,
        "status": "validated" if passed else "failed",
        "tolerance": tolerance,
        "historical_median_hours": round(expected, 2),
        "predicted_median_hours": round(predicted, 2),
        "percent_off": round(off * 100.0, 1) if off is not None else None,
        "p90_check": {
            "historical_hours": expected_p90,
            "predicted_hours": round(predicted_p90, 2) if predicted_p90 is not None else None,
            "percent_off": round(off_p90 * 100.0, 1) if off_p90 is not None else None,
            "note": "informational: the gate is on the median, not the tail",
        },
        "sample": {"historical_merged": historical.get("n_merged")},
        "note": (
            "the baseline reproduces the historical cycle-time median within the "
            f"stated ±{int(tolerance * 100)} % tolerance"
            if passed else
            "the baseline does NOT reproduce the historical cycle-time median: "
            "scenario numbers are withheld rather than shown with false confidence"
        ),
    }


def _delta(baseline: float | None, scenario: float | None,
           higher_is_better: bool = False) -> dict:
    """Absolute/relative change, plus whether it is an improvement.

    ``direction`` is the sign of the change ("down"/"up"); ``better`` says
    whether that sign is good *for this metric* — shorter cycle and higher
    throughput are good, and a bare "down" would read as good for both.
    """
    if baseline in (None, 0) or scenario is None:
        return {"absolute": None, "percent": None, "direction": None, "better": None}
    absolute = scenario - baseline
    percent = absolute / baseline * 100.0
    direction = "down" if absolute < 0 else ("up" if absolute > 0 else "same")
    better = None if direction == "same" else (absolute > 0) == higher_is_better
    return {
        "absolute": round(absolute, 2),
        "percent": round(percent, 1),
        "direction": direction,
        "better": better,
    }


def _within_noise(baseline: dict, scenario: dict) -> bool:
    """Is the cycle-time delta smaller than the run-to-run spread of the model?

    ``True`` means "the policy changed the prediction by less than Monte-Carlo
    noise", which the UI must say instead of dressing a 1 % difference up as a
    result. The spread is the range of the per-replication medians on both runs.
    """
    def spread(result):
        entry = (result.get("predicted") or {}).get("spread") or {}
        return entry.get("standard_error") or 0.0

    left = (baseline.get("predicted") or {}).get("cycle_hours", {}).get("median")
    right = (scenario.get("predicted") or {}).get("cycle_hours", {}).get("median")
    if left is None or right is None:
        return False
    # ~95 % band on the pooled median: two standard errors of the replications
    return abs(right - left) <= 2.0 * max(spread(baseline), spread(scenario), 1e-9)


#: prd/37 §5: a lead-time rise past this share is a blow-up, not a trade-off.
LEAD_BLOWUP_PERCENT = 25.0


def _verdict(entry: dict, better: str, worse: str,
             *, blowup: float | None = None) -> dict:
    """One plain-language verdict for a delta (prd/37 §5).

    Every scenario row gets one, including the bad ones: a cycle-time win with
    a lead-time blow-up must say so in words, not leave the reader to compare
    two percentages.
    """
    percent = entry.get("percent")
    if percent is None:
        return {"text": "not comparable", "tone": "muted"}
    if entry.get("direction") == "same":
        return {"text": "unchanged", "tone": "muted"}
    value = abs(percent)
    if entry.get("better"):
        return {"text": better.format(p=value), "tone": "good"}
    text = worse.format(p=value)
    if blowup is not None and percent >= blowup:
        return {
            "text": f"{text} — a blow-up: work waits longer to start, so "
                    "delivery is not faster",
            "tone": "critical",
        }
    return {"text": text, "tone": "bad"}


def _flow_verdict(entry: dict) -> dict:
    """Flow efficiency is a share: say the change in points, not a ratio."""
    percent = entry.get("percent")
    if percent is None:
        return {"text": "not comparable", "tone": "muted"}
    points = abs(float(entry.get("absolute") or 0.0)) * 100.0
    if points < 0.05:
        return {"text": "unchanged", "tone": "muted"}
    better = entry.get("better") is True
    return {
        "text": f"flow efficiency {points:.1f} points {'better' if better else 'worse'}",
        "tone": "good" if better else "bad",
    }


def _pr_count_verdict(entry: dict) -> dict:
    """The PR-count row: packaging, with the *signed* change (prd/41 B7).

    The old text took ``abs(percent)`` and always said "higher", so a −1.1 %
    delta read "PR count 1.1 % higher" next to "▾ −1.1 %". The direction now
    comes from the same delta the cell shows.
    """
    percent = entry.get("percent")
    if percent is None or entry.get("direction") == "same":
        return {"text": "unchanged", "tone": "muted"}
    word = "lower" if entry.get("direction") == "down" else "higher"
    note = (
        "fewer PRs, not necessarily less work"
        if word == "lower" else "more PRs, not necessarily more work"
    )
    return {
        "text": f"PR count {abs(percent):.1f} % {word} — {note}",
        "tone": "muted",
    }


def _annotate(compare: dict) -> None:
    """Attach a verdict line to every A/B row and one overall verdict."""
    verdicts = {
        "cycle_median_hours": _verdict(
            compare["cycle_median_hours"],
            "cycle {p:.1f} % shorter", "cycle {p:.1f} % longer",
        ),
        "cycle_p90_hours": _verdict(
            compare["cycle_p90_hours"],
            "p90 cycle {p:.1f} % shorter", "p90 cycle {p:.1f} % longer",
        ),
        "lead_hours": _verdict(
            compare["lead_hours"],
            "lead time {p:.1f} % shorter", "lead time {p:.1f} % longer",
            blowup=LEAD_BLOWUP_PERCENT,
        ),
        "throughput_work_per_day": _verdict(
            compare["throughput_work_per_day"],
            "delivered work {p:.1f} % more", "delivered work {p:.1f} % less",
        ),
        "flow_efficiency": _flow_verdict(compare["flow_efficiency"]),
        # PR count is packaging: more PRs is not more work (concepts.md §1)
        "throughput_per_day": _pr_count_verdict(compare["throughput_per_day"]),
    }
    for key, verdict in verdicts.items():
        if key in compare:
            compare[key]["verdict"] = verdict["text"]
            compare[key]["verdict_tone"] = verdict["tone"]
    compare["verdicts"] = verdicts
    if compare.get("within_noise"):
        compare["verdict"] = (
            "no clear effect on cycle time — the change is smaller than the "
            "model's run-to-run spread"
        )
        compare["verdict_tone"] = "muted"
        return
    lead = verdicts["lead_hours"]
    cycle = verdicts["cycle_median_hours"]
    work = verdicts["throughput_work_per_day"]
    # the overall sentence already quotes cycle time and delivered work, so the
    # other rows are the only ones appended — never the same delta twice (B7)
    headline = ("cycle_median_hours", "throughput_work_per_day", "throughput_per_day")
    others_bad = [
        verdict for key, verdict in verdicts.items()
        if key not in headline and verdict["tone"] in ("bad", "critical")
    ]
    bad = [verdict for verdict in verdicts.values() if verdict["tone"] in ("bad", "critical")]
    good = [verdict for verdict in verdicts.values() if verdict["tone"] == "good"]
    if lead["tone"] == "critical":
        if cycle["tone"] == "good":
            compare["verdict"] = (
                f"{cycle['text']}, but {lead['text']} — the policy moves waiting "
                "from the PR to the queue; not a net win for delivery"
            )
        else:
            compare["verdict"] = (
                f"{cycle['text']} and {lead['text']} — the policy makes delivery "
                "slower on both clocks; no net win"
            )
        compare["verdict_tone"] = "critical"
        return
    if not bad and good:
        compare["verdict"] = (
            f"{cycle['text']}, {work['text']} — the policy helps on both "
            "cycle time and delivered work"
        )
        compare["verdict_tone"] = "good"
    elif bad and good:
        parts = [cycle["text"]]
        if work["tone"] in ("bad", "critical", "good"):
            parts.append(work["text"])
        compare["verdict"] = "mixed: " + " and ".join(parts)
        if others_bad:
            compare["verdict"] += ", but " + "; ".join(
                verdict["text"] for verdict in others_bad
            )
        compare["verdict_tone"] = "warning"
    elif bad:
        compare["verdict"] = (
            "no win: " + "; ".join(verdict["text"] for verdict in bad)
        )
        compare["verdict_tone"] = "bad"
    else:
        compare["verdict"] = "no clear effect on cycle time or delivered work"
        compare["verdict_tone"] = "muted"


def _guardrail_entry(entry_id: str, label: str, baseline, scenario,
                     better: str = "lower", note: str | None = None) -> dict:
    """One modelled guardrail: the same run's baseline vs scenario."""
    delta = (
        round(float(scenario) - float(baseline), 4)
        if baseline is not None and scenario is not None else None
    )
    out = {
        "id": entry_id,
        "label": label,
        "baseline": baseline,
        "scenario": scenario,
        "delta": delta,
        "better": better,
        # a Monte-Carlo wobble is not a worsening: require a change larger than
        # 5 % of the baseline (and at least 0.1 of its own unit)
        "worse": bool(
            delta is not None and better == "lower" and delta > 0
            and delta >= max(0.1, abs(float(baseline or 0.0)) * 0.05)
        ),
    }
    if note:
        out["note"] = note
    return out


def _guardrails(baseline: dict, scenario: dict,
                held_constant: dict | None = None) -> dict:
    """prd/62: a guardrail beside every speed lever, marked modelled or held.

    The modelled half is built from **this run's** own facts (rounds, the PRs
    per 100 commits the size cap inflates, escalation and decline shares), so a
    guardrail can never disagree with the A/B it sits under. The held half is
    the measured stability panel passed in by the server: the model re-loops the
    review, it does not rework the code, so reopen and failed verification are
    printed at their measured baseline and say they are held — never silence.
    """
    left = baseline.get("predicted") or {}
    right = scenario.get("predicted") or {}
    left_rounds = left.get("rounds") or {}
    right_rounds = right.get("rounds") or {}
    held = held_constant or {}
    modelled = [
        _guardrail_entry(
            "rounds_mean", "rounds",
            left_rounds.get("mean"), right_rounds.get("mean"),
            note=(
                "bounded: a 2-action cap makes rounds ≤ 1 by construction"
                if (scenario.get("scenario") or {}).get("review_round_cap") == 2
                else "run value: reviewer actions − 1 per merged PR"
            ),
        ),
        _guardrail_entry(
            "rounds_p85", "rounds p85",
            left_rounds.get("p85"), right_rounds.get("p85"),
        ),
        _guardrail_entry(
            "prs_per_100_commits", "PRs per 100 commits",
            left.get("prs_per_100_commits"), right.get("prs_per_100_commits"),
            note=(
                "every extra PR needs the sentinel's approval again; delivered "
                "work is unchanged, so this is packaging, not throughput"
            ),
        ),
        _guardrail_entry(
            "escalated_share", "reviews escalated",
            left.get("escalated_share"), right.get("escalated_share"),
        ),
        _guardrail_entry(
            "declined_per_100", "declined per 100 PRs",
            left.get("declined_per_100"), right.get("declined_per_100"),
        ),
    ]
    not_modelled: list[dict] = [
        {
            "id": "bug_share",
            "reason": (
                (held.get("bug_share") or {}).get("reason")
                or "the store's issue objects carry no issue type"
            ),
        },
    ]
    held_out: list[dict] = []
    if held.get("available"):
        for key, label, note in (
            ("reopen", "reopened", "the model re-loops review, it does not rework code"),
            ("failed_verification", "failed verification",
             "the model re-loops review, it does not rework code"),
            ("zero_approval_merges", "merged with no approval",
             "the gate is mandatory in the model"),
        ):
            entry = held.get(key) or {}
            value = entry.get("rate") if key != "zero_approval_merges" else entry.get("count")
            held_out.append({
                "id": key,
                "label": label,
                "baseline": value,
                "count": entry.get("count"),
                "n": entry.get("n", held.get("n")),
                "note": f"held constant: {note}",
            })
    else:
        not_modelled.append({
            "id": "reopen",
            "reason": (
                held.get("note")
                or "the store has no ticket layer, so no reopen baseline is measured"
            ),
        })
    return {
        "modelled": modelled,
        "held_constant": held_out,
        "not_modelled": not_modelled,
        "note": (
            "modelled = this run's own fact; held constant = the measured "
            "baseline the model does not simulate. The model does not rework the "
            "code; it re-loops the review."
        ),
    }


def compare(baseline: dict, scenario: dict,
            held_constant: dict | None = None) -> dict:
    """A/B: the two predicted distributions and their deltas, side by side."""
    left = baseline["predicted"]
    right = scenario["predicted"]
    result = {
        "cycle_median_hours": {
            "baseline": left["cycle_hours"]["median"],
            "scenario": right["cycle_hours"]["median"],
            **_delta(left["cycle_hours"]["median"], right["cycle_hours"]["median"]),
        },
        "cycle_p90_hours": {
            "baseline": left["cycle_hours"]["p90"],
            "scenario": right["cycle_hours"]["p90"],
            **_delta(left["cycle_hours"]["p90"], right["cycle_hours"]["p90"]),
        },
        "flow_efficiency": {
            "baseline": left["flow_efficiency"]["aggregate"],
            "scenario": right["flow_efficiency"]["aggregate"],
            **_delta(left["flow_efficiency"]["aggregate"],
                     right["flow_efficiency"]["aggregate"], higher_is_better=True),
        },
        # prd/37 §5: the throughput that matters is delivered work, not PRs.
        "throughput_work_per_day": {
            "baseline": left["throughput_work_per_day"],
            "scenario": right["throughput_work_per_day"],
            **_delta(left["throughput_work_per_day"], right["throughput_work_per_day"],
                     higher_is_better=True),
        },
        "throughput_per_day": {
            "baseline": left["throughput_per_day"],
            "scenario": right["throughput_per_day"],
            **_delta(left["throughput_per_day"], right["throughput_per_day"],
                     higher_is_better=True),
        },
        "bottleneck": {
            "baseline": (baseline.get("bottleneck") or {}).get("person"),
            "scenario": (scenario.get("bottleneck") or {}).get("person"),
            "baseline_utilisation": (baseline.get("bottleneck") or {}).get("utilisation"),
            "scenario_utilisation": (scenario.get("bottleneck") or {}).get("utilisation"),
        },
        "lead_hours": {
            "baseline": left["lead_hours"]["median"],
            "scenario": right["lead_hours"]["median"],
            **_delta(left["lead_hours"]["median"], right["lead_hours"]["median"]),
        },
        "backlog_max": {
            "baseline": left["backlog_max"],
            "scenario": right["backlog_max"],
        },
        # prd/62: every speed lever carries its guardrail — modelled from this
        # same run, or the measured baseline it is held at.
        "guardrails": _guardrails(baseline, scenario, held_constant),
        "within_noise": _within_noise(baseline, scenario),
        "note": (
            "a delta is only a prediction if the baseline validated; the two runs "
            "share one seed (common random numbers) so the difference is the policy"
        ),
    }
    _annotate(result)
    return result


def simulate(calibration: dict, scenario: Scenario | None = None,
             seed: int = DEFAULT_SEED, tolerance: float = DEFAULT_TOLERANCE,
             horizon_business_days: float | None = None,
             replications: int = DEFAULT_REPLICATIONS,
             held_constant: dict | None = None) -> dict:
    """Baseline + validation gate + (if the gate passes) the scenario A/B.

    The gate is enforced here, once, so no caller can accidentally show a
    scenario number from an unvalidated model: when the baseline fails, the
    payload carries the mismatch and ``scenario`` is ``None``. ``replications``
    is honoured for both runs (it used to be accepted by the API and ignored
    here, so a caller asking for one replication paid for six -- FR-6).
    """
    baseline = run(calibration, Scenario(), seed=seed,
                   horizon_business_days=horizon_business_days,
                   replications=replications)
    gate = validate(calibration, baseline, tolerance=tolerance, seed=seed)
    requested = scenario is not None and not scenario.is_baseline
    inputs = (
        scenario_inputs(calibration, scenario)
        if requested else {"available": True, "reason": None, "missing": []}
    )
    payload = {
        "ok": bool(baseline.get("ok")),
        "available": bool(baseline.get("available")),
        "hours_mode": calibration.get("hours_mode"),
        "assumption": calibration.get("assumption"),
        "provenance": calibration.get("provenance"),
        "calibration": calibration,
        "baseline": baseline,
        "validation": gate,
        "scenario": None,
        "compare": None,
        "seed": seed,
    }
    if requested:
        # prd/64 F-03: expose the scenario's own availability, so Rules and the
        # lab can say "not measurable" instead of ranking a fabricated result.
        payload["scenario_available"] = bool(inputs["available"])
        if not inputs["available"]:
            unavailable = scenario_unavailable(scenario, inputs)
            payload["scenario_unavailable"] = unavailable
            payload["scenario_unavailable_reason"] = unavailable["reason"]
            payload["missing_inputs"] = unavailable["missing_inputs"]
    if not payload["ok"]:
        payload["note"] = baseline.get("note")
        return payload
    if scenario is None or scenario.is_baseline:
        payload["note"] = (
            "baseline only; pass scenario={...} to compare a policy against it"
        )
        return payload
    if not gate["ok"]:
        payload["blocked"] = (
            "baseline did not validate, so the scenario was not run: "
            f"predicted {gate['predicted_median_hours']} h vs historical "
            f"{gate['historical_median_hours']} h "
            f"({gate['percent_off']} % off, tolerance ±{int(tolerance * 100)} %)"
        )
        return payload
    if not inputs["available"]:
        # The gate passed, but this scenario's own input is not in the store.
        # Nothing is modelled: no default hours, no delta, no best-scenario bid.
        payload["note"] = (
            "the scenario was not run: "
            f"{inputs['reason']}"
        )
        return payload
    result = run(calibration, scenario, seed=seed,
                 horizon_business_days=horizon_business_days,
                 replications=replications)
    payload["scenario"] = result
    payload["compare"] = compare(baseline, result, held_constant=held_constant)
    payload["note"] = "baseline validated; the scenario is a prediction, not a measurement"
    return payload

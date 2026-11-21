# 62 — Rolled first-pass yield and a guardrail beside every speed lever

The Rules page recommends speed levers (review SLA, cap rounds, smaller PRs) and never shows what they might cost;
the store has 5 reopened stories, 4 failed-verification bounces and 21 review bounces but no aggregate says "only
X % of stories go through without a loop-back". This PRD adds (a) per-step **%complete-and-accurate** and their
product — VSM's **rolled %C&A** — (b) a **stability panel** mirroring DORA's instability factor from tracker data
(each number either a direct loop-back rate or an explicitly labelled proxy), and (c) a **guardrail next to every lever's speed result** in the policy lab and the
rules, either modelled or explicitly held constant. Pairs with prd/51; conforms to `docs/concepts.md` (§5 stages,
§6 metrics, §7 simulation) and prd/10.

## User journey

Priit reads "cap review at 2 rounds — the lab models this at −13 % cycle". Before acting he scrolls to the
First-pass yield panel:

> **Rolled %C&A 8.6 %** — only 8 of 87 stories went through all four steps without a loop-back.
> Refined **100 %** · Developed **76 %** · Reviewed **13 %** · Verified **90 %**.

Only 13 % of stories get through review without a rework round, so "cap at 2 rounds" is not "ship unreviewed" — it
is "stop asking for the fourth round". The lever's guardrail line says exactly that: `rounds fall by construction
(1.6 → 0.8); reopen 5.7 % and failed verification 4.6 % are held constant — the model does not rework the code, only
the review loop.` The stability panel below warns the other way: `Review bounced 24 %` — most of the loop cost is
real review, not gate latency.

## Product design

**Where.** Rules page: a `First-pass yield` panel immediately above the rule list (the page's question is "What
should we change first?", and yield is the *cost side* of that answer, so it must be read before the levers), with
the stability panel directly under it. Policy lab: a second line under each scenario's cycle delta. Ticket page: a
four-step pass line in the story drawer. Overview: no new surface (the verdict card already carries the ladder).

**Layout — waterfall.** One line: four step columns and the rolled figure at the right.

`REFINED 100 % · DEVELOPED 76 % · REVIEWED 13 % · VERIFIED 90 %  →  ROLLED 8.6 %`

Each step: label (11 px uppercase), rate (15 px/700), `n / N` (12 px muted), and a 4 px bar whose fill is the rate;
step colours are a **category** (pipeline stage) from `--cat-1…4` (prd/10 §3.3 allows category colour), the rolled
figure stays neutral (a number, not a state). A step below the lab-implied yield gets a `--warning` dot, never a
recolour. One sentence under the line: "Only 8 of 87 stories went through without a loop-back."

**Layout — stability panel.** Four compact tiles, each a rate of *deliveries*: `Reopened 5.7 %` · `Failed
verification 4.6 %` · `Review bounced 24 %` · `Bug share —` with the inline reason "issue type is not in this
store". Under them, one line: `Rework proxy 10 % (reopened or failed verification) — not DORA's rework rate: this
store has no deploy or incident data.` Each tile's tooltip names its cohort and n.

**Guardrail rendering.** `ruleRow` gains a `guardrail` block under the lab note: an 11 px `GUARDRAIL` overline, one
sentence, ≤2 numbers. Neutral text unless the guardrail *worsens* under the lever, in which case only the number is
`--warning`-tinted. In the Policy lab each scenario row's delta cell gains a 12 px second line: `guardrail: rounds
1.6 → 0.8 · reopen held at 5.7 %`. When the model cannot represent the guardrail, the sentence starts with
`held constant:` and the measured baseline is printed — never silence.

**Interactions.** Clicking a step opens its drill-down (the loop-back list: DEL-137, DEL-153, DEL-169 reopened;
DEL-106, DEL-132, DEL-158, DEL-171 failed verification — each a `#ticket/KEY` link). `Copy as Markdown` includes the
step table, the stability rates and every guardrail line. No new toggles: the panel is part of the answer, not an
option.

**Honesty language (exact).** `%C&A = the share of items that left this step complete and accurate — no loop-back
into it.` · `Rolled %C&A is the product of the four steps; the joint count (8 of 87) is the measured "clean first
pass" and can differ slightly because a story can loop at more than one step.` · `Bug share needs the issue type,
which this store does not carry; the rework proxy is the honest substitute, labelled.` · `The model does not rework
the code; it re-loops the review. A guardrail it cannot model is held constant and says so.`

**Phone (≤640 px).** The waterfall becomes four stacked rows (`DEVELOPED · 76 % · 66/87`); the stability tiles go
2×2; the guardrail line wraps under the lever with its overline and keeps its numbers on one line; no horizontal
scroll, no chart.

## Technical design

### Definitions (add to `docs/concepts.md` §6)

- **Step yield (%C&A)** — the share of the cohort that left a pipeline step without a loop-back *into* it. Four
  steps: `refine`, `develop`, `review`, `verify`. The cohort is **completed stories** in the window (a delivery).
- **Rolled %C&A** — the product of the four step yields, with the **joint clean count** reported next to it (the
  measured number of stories clean at every step).
- **Stability rate** — a loop-back count ÷ deliveries in the window (`reopen`, `failed_verification`,
  `review_bounce`), or a labelled proxy (`bug_share`, `rework_proxy`). Not a DORA metric: no deploy/incident source.
- **Guardrail** — a quality or rework measure shown next to a lever's predicted speed effect, marked `modelled` or
  `held_constant` (with its measured baseline).

### Step signals (exact transitions, one place)

| step | clean = | signals | del-combined |
|---|---|---|---|
| `refine` (`created→ready`) | no `ready → backlog` return and no `pointed` event after `started` | stage events + point churn | 87 / 87 (no loop-back event in this store — stated) |
| `develop` (`ready…→first review`) | no `in_review → in_progress` bounce on the story | stage events | 66 / 87 = 76 % |
| `review` (`first review→approval`) | **zero** review rounds across **all** the story's merged PRs (`ballcourt.review_round_events`) | prd/40 rounds | 11 / 87 = 13 % |
| `verify` (`merge→verification→done`) | no `in_verification → in_progress` and no `done → ready` reopen | stage events | 78 / 87 = 90 % |
| **rolled** | product | | **8.6 %** (joint clean 8/87 = 9.2 %) |

A story with two PRs must pass review twice; it is clean only if both PRs are. A step whose falsifying transitions do
not exist in the store reports `available: "unfalsifiable"` and `rate: null` rather than a perfect score. Below
`MIN_DELIVERIES = 10` no percentage is shown (counts only), mirroring `benchmark.BENCHMARK_MIN_N`.

### API

`GET /api/insights` — `metrics.yield` and `metrics.stability` (the Rules page payload, no extra request):

```json
"yield": {"cohort": "completed stories", "n": 87, "min_deliveries": 10,
  "steps": [{"id": "develop", "label": "Developed", "clean_n": 66, "n": 87, "rate": 0.759,
             "available": true, "signals": ["in_review → in_progress"]}],
  "rolled": 0.086, "clean_joint": 8, "note": "…"},
"stability": {"n": 87,
  "reopen": {"count": 5, "rate": 0.057, "items": ["DEL-105", "DEL-137", "…"]},
  "failed_verification": {"count": 4, "rate": 0.046, "items": ["DEL-106", "…"]},
  "review_bounce": {"count": 21, "rate": 0.241},
  "bug_share": {"available": false, "reason": "the store's issue objects carry no issue type"},
  "rework_proxy": {"rate": 0.103, "basis": "reopened or failed verification / deliveries", "proxy": true}}
```

`GET /api/simulate` — `compare.guardrails` per preset (built from the model's own run):

```json
"guardrails": {
  "modelled": [{"id": "rounds_mean", "baseline": 1.59, "scenario": 0.8, "delta": -0.79, "better": "lower",
                "note": "bounded: a 2-action cap makes rounds ≤ 1 by construction"},
               {"id": "rounds_p85", "baseline": 3, "scenario": 1, "better": "lower"},
               {"id": "prs_per_100_commits", "baseline": 16.2, "scenario": 26.4, "better": "lower",
                "note": "every extra PR needs the sentinel again"},
               {"id": "escalated_share", "baseline": 0.0, "scenario": 0.18}],
  "held_constant": [{"id": "reopen", "baseline": 0.057, "note": "the model re-loops review, it does not rework code"},
                    {"id": "failed_verification", "baseline": 0.046},
                    {"id": "zero_approval_merges", "baseline": 0, "note": "the gate is mandatory in the model"}],
  "not_modelled": [{"id": "bug_share", "reason": "no issue type"}] }
```

`advice.report` copies `guardrails` onto each preset entry, so the rule's note and the lab's row show the same
sentence from one source. `GET /api/ticket/{key}` gains `yield: {steps: [{id, clean, signals}], rolled_clean: bool}`.

### Simulation changes (precise, small)

`_summarize` already has every `_PR`; add to `pooled` and `predicted`:

- `rounds`: `mean`, `p50`, `p85` over **merged** PRs, where a PR's rounds = `max(0, peer_actions − 1)` (the last
  reviewer action is the approval; prd/40 defines a round as a re-review pass);
- `prs_per_work_unit` = `prs ÷ max(1, work)` where `work` is commits (`work_unit` already exists), rendered as
  `prs_per_100_commits = round(100 × prs ÷ work, 1)` (del-combined: 106 ÷ 656 → 16.2);
- `escalated_share` = `queues.escalated ÷ prs`;
- `declined_per_100` = `declined ÷ prs × 100`.
- `compare()` computes the deltas against the baseline with the same seed (common random numbers, already the
  lab's method), and adds the `held_constant` block from the **measured** stability panel (passed in by the server,
  since the model does not simulate reopens/verification). `advice._entry` gains
  `guardrail: {"modelled": bool, "label": "…", "text": "…"}` with fixed wording:
  modelled → `rounds 1.6 → 0.8 (model)`; held → `reopen held at 5.7 % (model does not rework the code)`. The
  modelled number is a run value; its *bound* (rounds ≤ cap − 1) is exact, and that bound is what the copy may claim.

### Cache and invalidation

`yield` and `stability` ride the existing `/api/insights` cache key (store stamp + calendar + window + repo). The
simulated guardrails ride `_sim_cache` (key already includes the scenario and seed). `advice.report` is built from
the same cached `/api/simulate` comparisons, so a guardrail can never disagree with the Policy page. No new cache.

### Edge cases

- **<10 deliveries**: counts only, no percentage; the panel says "5 deliveries — too few for a rate".
- **Zero loop-backs**: 100 % + "no loop-back event in this store — check the workflow emits `ready`, `in_review`,
  `in_verification` transitions" (the §5.4 "verify with the team" prompt, not a boast).
- **Story with no PR** (Jira-only): `review` step `unfalsifiable`, the story is excluded from the review denominator
  and the panel says `N stories have no PR`.
- **Story with several PRs**: clean only if every merged PR had zero rounds (stated in the step's `signals`).
- **Bug share** unavailable → the tile is a dash with the reason, and the rework proxy carries the meaning; never
  substitute `summary_class` (it classifies summary text, not issue type — using it would be a category error).
- **A lever with no lab result**: the guardrail line is omitted and the rule says "no lab verdict for this policy",
  rather than inventing a cost.
- **Repo scope**: yields recompute on the repo's cohort (prd/49); a PR-count-based guardrail states its cohort.

### Tests

`tests/test_yield.py` — each step on hand-built transitions (clean, bounced, multi-PR, no-PR); del-combined
66/87 · 11/87 · 78/87 · rolled 0.086 · joint 8; a step with no falsifying signal → `unfalsifiable`/`rate:null`;
`<10` deliveries → no rate; reopen/fail-verification/review-bounce counts and rates; bug share unavailable without
`issuetype`; `summary_class` never used for bug share (source guard). `tests/test_simulation.py` — `rounds` pooled
from `_PR.peer_actions`; `prs_per_work_unit` rises under `size_cap_commits` while `work` is unchanged;
`guardrails.modelled` keys present for every preset; `held_constant` carries the measured baselines.
`tests/test_advice.py` — every entry carries the guardrail wording; modelled vs held copy; a missing lab result omits
it. `app/test/prd62.test.mjs` — waterfall order and rates; rolled figure neutral; warning dot only below the
implied yield; stability tiles incl. the dash + reason; guardrail line on every rule row and in the lab rows; phone
stacking. `tests/test_conformance.py` — rates always carry `n`; no new duration formatter.

## Definition of done

- On del-combined the Rules page shows `Rolled %C&A 8.6 %` with steps `100 % · 76 % · 13 % · 90 %` and
  "8 of 87 stories clean"; the joint count and the product are both printed and explained.
- Stability shows `reopen 5.7 % (5)`, `failed verification 4.6 % (4)`, `review bounce 24 % (21)`, `bug share —`
  with the missing-issue-type reason, and the rework proxy `10 %` labelled a proxy.
- Every rule row renders a guardrail line; the round-cap lever says `rounds 1.6 → 0.8 (model)` and
  `reopen held at 5.7 %`; the PR-size lever's guardrail shows `PRs per 100 commits` rising while delivered work is
  unchanged — which is the +289 % artefact, now visible where the lever is read.
- `/api/simulate.compare.guardrails` is present for all five presets and agrees with the Rules page (one source).
- The ticket page shows this story's four-step pass (e.g. DEL-101: refined clean, review bounced, verified clean).
- A fixture store with 5 deliveries shows counts and no rates; a store without Jira refuses with a reason.

## Builds on / non-goals

Builds on **51** (story rework — this aggregates it and adds the roll-up), **42** (second-order comparisons),
**43** (lab notes; the guardrail is the other half of a lab verdict), **40** (review rounds, the one definition),
**39** (size caps, whose side effect the guardrail exposes), **61** (the same split machinery for guardrail
*trends*). Amends `docs/concepts.md` §6 with *%C&A*, *rolled %C&A*, *stability rate* and *guardrail*.

Does **not**: claim DORA's change-failure rate or rework rate (no deploy/incident source; proxies are labelled);
score people on quality; add AI-authorship attribution; ingest "eyeball time" or comment content (impossible
content-free — the labelled proxies are the honest substitute); add review-SLA enforcement inside the lab beyond
what the model already does; print a guardrail the model did not compute without saying `held constant`.

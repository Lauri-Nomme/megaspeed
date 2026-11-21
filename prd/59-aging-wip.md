# 59 — Aging WIP against the team's own percentiles ("age vs your own p85")

The product's staleness thresholds are fixed numbers (stale PR = 5 business days, sentinel tail = 24 h) and there is
no view of *in-flight* items' age relative to history — the forward-looking half of flow. This PRD adds the aging
view, replaces the hard-coded constants with **self-calibrated percentile thresholds** (p50/p85/p95, and p75
time-in-review — Meta's north star, on the holder clock), and makes the resulting list the chase data source for
prd/54 and the exploit step of prd/58. Conforms to `docs/concepts.md` (§1 business hours, §5 censoring, §6
benchmark contrast) and prd/10.

## User journey

18 Nov. Priit opens Delivery list → the new **Aging** tab (13 open stories, 8 open PRs). Top of the list:

> DEL-114 · 516 h (58 working days) — older than **every** finished story in this store (max 200 h) ·
> **no live PR** — PR #8 was declined on 16 Nov, nothing human since 27 Aug.
> #60 (DEL-176) · 144 h in review — **8× the p75 of finished review time (18.3 h)** · waiting on FrMo.
> #53 · 192 h · waiting on CaOs + FrMo · ticket already Done in Jira.

The old UI said "#66 stale (>5 bd)" and stopped. Now he sees that 6 of 8 open PRs are past the cohort's p85 for
time in review (22.7 h), that DEL-114 is not a slow review but abandoned work (so it must be closed, not chased),
and that the three real chase items are #53/#55/#60 — the same three prd/54 names, now derived rather than typed.

## Product design

**Where.** A fourth subset tab on the Delivery list (the prd/10 §5.3 triage table):
`Blocked 1 | Oldest waiting 3 | Aging 13 | All 8` — counts from the same rows, so the tab cannot disagree with the
table. The tab answers the page's question (*What needs attention?*) with age **against the team's own finished work**
rather than against a fixed constant. The Overview tile "waiting over a week" links here with `?tab=aging`. No new page.

**Layout (desktop).** Above the rows, a **strip chart** (dot plot, 88 px): x = current age in business hours, three
vertical ticks labelled `p50 13 h · p85 22.7 h · p95 31.2 h` (PR review view) with `n = 106 merged PRs` under the
axis; one dot per in-flight item, no connecting line, no area. Below it, dense single-line rows:

`band bar (4 px) · kind · key (mono) · stage · age (right, tabular) · ×p85 · holder (mono) · live tag`

Band bar: a 3-stop micro-scale (p50 / p85 / p95 as tick positions) with the item's position filled — the same
encoding on every row, so the column scans vertically. `×p85` reads `1.0×` inside the band and `8.5×` outside.
Sorting: `hours_at_risk = age − p85` descending (the removable wait), tie-broken by downstream dependents.

**Band vocabulary (one set, used by 58, 59, 54).** `normal` (<p50) · `watch` (p50–p85) · `aging` (p85–p95) ·
`very old` (>p95) · `insufficient history` (cohort <20). "Stale" stays the name of the prd/34 rule with its own
threshold; the aging bands are the distribution-aware layer and never reuse the word.

**Controls.** Stage selector: `Story cycle | PR review | PR cycle | Stage ▾` (defaults: PR review when the tab is
opened from a PR row, story cycle otherwise). `Only live` toggle (default on when arriving from prd/54, off from
the Overview tile, so abandoned work is visible at least once). `Threshold source: self-calibrated (default) |
fixed (5 bd / 24 h)` — a per-tenant setting that prints the source on every threshold chip.

**How it replaces the constants.** The `stale-prs` and `sentinel-tail` rules keep their ids and evidence, but their
threshold is now supplied by `aging.rule_thresholds()`: the p85 of the *same cohort* (merged PRs for review time,
story review segment for the tail), printed in the evidence as `threshold_source: "p85 of 106 merged PRs (22.7 h)"`.
With `fixed`, the old 5 bd / 24 h apply and the chip says `fixed`. Counts may move by an item or two; that is the
point, and the source travels with the number.

**Interactions.** Row click → PR drawer / `#ticket/KEY`; right-click → the prd/10 pivot menu; `Chase list →` opens
the prd/54 card filtered to the aging order; `Copy as Markdown` exports the ranked list with each item's band,
holder and `×p85`; the strip chart's ticks are hoverable (`p85 = 75th…` wording is not used — the label is exact).

**Honesty.** Meta line: `Ages are business hours on the holder's calendar; a person on vacation accrues none.
Calendar days in brackets.` Under the chart: `An age is a censored duration — the item has not finished yet; compare
it with finished work, never add it to a cycle-time median.` `Insufficient history: 12 finished items (needs 20) —
no percentile shown` replaces the chart instead of drawing a fake one.

**Phone (≤640 px).** The strip becomes 64 px with the three tick labels above it (10 px, no rotation); rows stack
to two lines (`DEL-114 · 516 h` / `no live PR · 58 working days`); the stage selector and threshold setting move to
a bottom-sheet picker; tab labels shorten to `Blocked · Oldest · Aging · All`. Tap targets ≥44 px, no horizontal
scroll.

## Technical design

### Data model

`analysis/aging.py` (pure over `court`/`jira`):

| function | output |
|---|---|
| `story_thresholds(court, jira, window)` | per stage: `{p50,p85,p95,n,available}` from `jira.segment_cohort` + `story_cycle_cohort` |
| `review_thresholds(court, window)` | per merged PR, Σ `interval.hours` where `phase == "review" and ball == "review"` (role-level, once) → `{p50,p75,p85,p95,n}` |
| `pr_cycle_thresholds(court)` | merged PR `created→merged` business hours → `{p50,p85,p95,n}` |
| `in_flight(court, jira, kind, stage)` | one row per open item (story or PR) — see payload |
| `chase_order(rows)` | live rows with band ∈ {aging, very_old}, sorted by `age − p85` desc |
| `rule_thresholds(court, jira, window)` | the two rule thresholds + their source strings; `None` when <20 samples |

`MIN_N = 20`; `BANDS = ("normal","watch","aging","very_old","insufficient")`. Quantiles use the store's existing
sorted-quantile helper (nearest-rank, as `rules._percentile` and `jira._percentile` do) so a tick equals a manual
recompute exactly.

### API — `GET /api/aging?tenant=&days=&repo=&kind=story|pr&stage=&only=live|all&limit=`

```json
{ "ok": true, "as_of": "…", "hours_mode": "business", "window": {"start": "…", "end": "…"},
  "thresholds": [
    {"id": "review_time", "label": "Time in review", "unit": "hours",
     "p50": 13.0, "p75": 18.3, "p85": 22.7, "p95": 31.2, "n": 106, "min_n": 20,
     "available": true, "source": "self-calibrated", "basis": "merged PRs, role-level, holder clock"},
    {"id": "story_cycle", "p50": 90.1, "p85": 127.5, "p95": 172.3, "n": 87, …}],
  "rows": [
    {"kind": "pr", "object_id": "PR:DEL/delivery-api#60", "pr": "#60", "ticket": "DEL-176",
     "stage": "review", "entered": "2026-10-26T11:23:54Z",
     "age_hours": 144.2, "calendar_days": 23, "band": "very_old", "rank_pct": 1.0,
     "cohort_n": 106, "multiple": 6.4,
     "holder": [{"person": "FrMo", "role": "sentinel", "hours": 144.2}],
     "live": true, "reason": null, "dependents": 1, "href": "#ticket/DEL-176"}],
  "counts": {"open": 8, "live": 8, "over_p50": 8, "over_p85": 6, "over_p95": 3, "insufficient": 0},
  "review_p75": {"value": 18.3, "n": 106, "basis": "holder-priced time in review"},
  "feed": {"chase": ["PR:…/#53", "PR:…/#55", "PR:…/#60"], "hours_at_risk": 423.0,
           "threshold_source": "p85 of 106 merged PRs (22.7 h)"},
  "note": "…" }
```

Unknown `stage` → 400 listing the stages. `kind=story` rows carry the story clock, `kind=pr` the review clock; each
row states which via its `basis` (one clock per concept — never mixed in a sum).

### Algorithm

1. Cohort per `kind`; refuse the percentile (`available:false`, band `insufficient`) when `n < 20`.
2. Age: opened PR → Σ review-phase hours up to the snapshot, including the `unobserved` tail (prd/55 §7: a
   3–5-week wait with no recent event is still a wait); story → `created → snapshot` (cycle view) or
   `entered_stage → snapshot` (stage view).
3. `rank_pct` = share of the cohort `≤` the value (ties counted as `≤`), reported with `cohort_n`; `multiple =
   age ÷ p85`.
4. `band` from the thresholds, half-open intervals: `age < p50 → normal`, `p50 ≤ age < p85 → watch`,
   `p85 ≤ age < p95 → aging`, `age ≥ p95 → very old`. Exactly p85 is `aging` (test-pinned).
5. `live`/`reason` via `verdict.liveness()` (shared with prd/58 — one classifier, so the two views cannot disagree):
   `live`, `unmerged` (ticket done, PR open), `dead` (no open PR, no human event ≥10 business days), `unobserved`.
6. `hours_at_risk = age − p85` (≥0), the removable wait the chase action carries. `chase_order` keeps `live` and
   `unmerged` rows (merging the done work *is* the action) and excludes `dead` rows, which are listed separately under
   "no live PR — close or re-plan".

### Cache and invalidation

`_aging_cache` keyed `(tenant, stamp, calendar key, days, repo, kind, stage, only)`, 32 entries, cleared like
`_trend_cache`. Thresholds depend only on `(store, calendar, window)`, so they are cached once per combination and
shared by `/api/aging`, `/api/insights` (rule thresholds) and `/api/verdict` (the exploit pool).

### Edge cases

- **Cohort too small** (repo scope on a 131-PR store): `available:false`, no chart, no `×p85`, band `insufficient`;
  the rules fall back to the fixed constants **and say so**.
- **Stage never completed** (`merge_verification` on a workflow without QA): no threshold, and the stage is absent
  from the selector rather than showing an empty 0.
- **Story with several PRs:** story rows use the story clock; PR rows are per PR. The chase list never sums across
  the two clocks.
- **Vacation-priced age:** an item held by someone on vacation accrues no business hours, so it can look *young*;
  `calendar_days` travels next to every age, and the meta line says why.
- **A declined PR with a merged sibling:** the story is `live` (the sibling is open) — liveness is per story, not
  per PR.
- **Bots:** excluded from `holder` and from the "human event" clock (a stale-PR sweep is not activity, §2).
- **Unmeasured tail:** rows carry `unobserved: true` and the prd/10 §7 disclosure line, so the oldest waits are not
  silently dropped.

### Tests

`tests/test_aging.py` — quantiles equal a hand-sorted nearest-rank recompute; band boundaries at exactly
p50/p85/p95; `rank_pct` with ties; min-n → `insufficient` and no threshold; review P75 equals a manual sum of review
intervals for a named PR; del-combined rows (DEL-114 516 h `dead`, DEL-176 200 h `very_old` live/FrMo, #53 192 h,
#60 144.2 h); `chase_order` = #53/#55/#60 and excludes `dead`; `rule_thresholds` moves `sentinel-tail` off 24 h and
prints the source; `MEGASPEED_AGING_THRESHOLDS=fixed` restores it. `tests/test_server.py` — payload shape, 400 on an
unknown stage, cache reuse, `available:false` at small n. `app/test/prd59.test.mjs` — three ticks rendered with
labels; band colour only for `aging`/`very_old`; `only live` toggle; `insufficient history` state; phone markup
(no chart at ≤640 px, no overflow); Markdown export order. `tests/test_conformance.py` — no "24 h"/"5 bd" constant
rendered without its source.

## Definition of done

- Aging tab on del-combined: 13 open stories; DEL-114 516 h (`very old`, `dead`, 58 working days), DEL-176 200 h
  (`very old`, live, FrMo), DEL-194 102 h (`watch`); 8 open PRs, 6 past p85 (22.7 h), #53 at 192 h (8.5×).
- `review_p75 = 18.3 h` over 106 merged PRs; every threshold chip prints its `n` and source.
- `feed.chase` = #53 (192 h) · #55 (155 h) · #60 (144 h), `hours_at_risk ≈ 423 h` — the same three items prd/54
  names, now derived; prd/58's exploit rung reads the same list.
- `stale-prs` / `sentinel-tail` quote the self-calibrated thresholds with `threshold_source`; `fixed` restores the
  old constants; a <20 cohort falls back with the reason visible.
- All suites green, including prd/45 conformance (no duration string built outside the shared formatters).

## Builds on / non-goals

Builds on **34** (the stale/sentinel-tail rules this re-thresholds), **54** (the chase card, which becomes a view of
this list), **42/57** (cohort contrast — the same `n` + `median`/`multiple` shape), **55** (censoring honesty),
**58** (the verdict's exploit pool). Amends `docs/concepts.md` §6 with *aging band*, *self-calibrated threshold*
(the p85 of the same cohort, with `n`), and *hours at risk* (`age − p85`).

Does **not**: score people (holder is a role-labelled attribution); alert in real time (no pager, prd/08);
predict an item's completion date (that is prd/60); invent a service-level *target* (the p85 is a description of
this team's work, not a commitment); show an age without its cohort or n.

# 61 — Intervention-aware trend ("did the change work — or can't we tell?")

prd/50 draws one metric per bucket, but a single-window aggregate cannot answer "did the review SLA help?", and Code
Climate's users complained that vacations and process changes cannot be annotated. This PRD adds **annotation
markers** (team-entered policy changes; vacation/holiday spans from the existing calendar), a **before/after panel**
that tests the *item distribution* with a bootstrap CI and a rank test, an explicit third outcome **"can't tell at
this n"**, and Little's-Law stability flags. Extends prd/50; conforms to `docs/concepts.md` (§1 Little's Law, §6
trend, §7 prediction language) and prd/10.

## User journey

Priit's team adopted the 4 h review SLA on **8 Oct**. The Trend page (question "Is it getting better?") shows the
cycle-median dip in October and a marker at 8 Oct. He clicks *Did it work?*:

> **Story cycle: 92.9 h → 83.0 h.** 95 % CI on the median difference **−22.3 … +8.3 h**.
> **Can't tell at this n** — 62 stories before, 25 after. The interval spans zero, so this data cannot separate the
> change from noise.
>
> **PR cycle: 24.8 h → 24.7 h.** CI **−3.5 … +3.7 h**. **No clear change** — the interval is tight around zero.

Below it the stability strip: `WIP 6 → 17 (+1.0/wk)` · `throughput 8.2/wk (+0.3/wk)` · `arrivals 10.1/wk vs
departures 8.2/wk (+25 items)` → *"The queue is filling: WIP grows 3.4× faster than throughput. Little's Law says
cycle time will rise; treat any cycle-time gain in this window with suspicion."* He now knows the SLA did not buy a
story-level improvement, and why the next quarter is at risk.

## Product design

**Where.** The Trend page (prd/50), in three additions, in reading order:

1. **Annotation layer on the existing chart.** A 1 px `--info-accent` hairline at each annotation date with a 4 px
   dot on the axis; vacation/holiday spans as `--warning` 8 %-alpha bands behind the plot, labelled at 10 px when
   ≥2 buckets wide. Hover/tap opens a popover with the closed-vocabulary label, the date, and the team's optional
   note (`review SLA 4 h · 8 Oct` · "adopted after the retro"). Under the chart: `2 policy changes · 1 holiday in
   this window`. Annotations outside the window are counted, not drawn.
2. **"Did the change work?" panel** below the chart. One annotation selected at a time (default: the latest policy
   change; a select of the closed labels). Layout: a two-column before/after table (`n`, `p50`, `p85`, the metric's
   own unit), the CI as a 12 px line with a 2 px range bar centred on zero, and the **verdict as a pill + one
   sentence**. Four outcomes only: `better` (healthy tint), `worse` (critical tint), `no clear change` (neutral
   outline), `can't tell at this n` (neutral outline). Never a naked p-value — the MWU p is in the tooltip.
3. **Stability strip.** Three chips + one sentence, plus a `Little's Law` chip when the implied and measured cycles
   diverge: `WIP ↗ +1.0/wk`, `throughput → 8.2/wk`, `arrivals > departures (+25)`, `implied 1.4 wk vs measured
   0.6 wk`. A fourth metric tab `Cumulative flow` draws the four cumulative bands (created / started / in review /
   done) from the same buckets.

**Density and colour.** Hairline-separated blocks inside the page (no cards, no shadows, prd/10 §3.5); tabular
figures; markers are *interactivity/info*, spans are *context* (never state colour); only the verdict pill uses
state tints, and `can't tell` is not a failure colour.

**Interactions.** Clicking a marker selects it in the panel and moves the split; brushing the histogram offers
`Set split here` as an in-place chip (no modal); `Copy as Markdown` includes the verdict, both `n`s, the CI and the
stability sentence; the panel's "compare this metric" links to the prd/62 guardrail trend for the same split.

**Adding an annotation.** A small "Record a change" affordance on the Trend page opens a menu of the **closed
vocabulary** for the change *type* (`review SLA`, `WIP limit`, `PR size cap`, `sentinel added`, `sentinel rotation`,
`refinement change`, `other`) plus one numeric value where it applies (hours / commits / limit), a date, and an
optional **free-text note** in the team's own words (e.g. "adopted after the retro; rollback is a 2-click revert").
The note is **first-party metadata** — the team typed it into megaspeed, it is not scraped Bitbucket/Jira content, so
it lives in the store (an `annotations` row, never the event log) and never touches the classifier — so it does not
violate the pseudonymisation contract (prd/02 §"Scope"). The `kind` stays enumerated so the chart/label is renderable
and testable; the optional `note` is where the nuance lives. Delete is a two-step inline confirm.

**Honesty language (exact).** `A before/after is not an experiment: it compares two samples of items either side of
the change, with everything else that also changed. The interval is the answer.` ·
`Can't tell at this n: 62 items before, 25 after — the interval spans zero, so this data cannot separate the change
from noise.` · `Two changes in the same week cannot be separated; move one annotation or widen the window.` ·
`Holiday: 20 Aug (taasiseseisvumispäev) — the calendar already excludes it from every business hour.`

**Phone (≤640 px).** Chart 140 px; markers get 44 px invisible hit areas; the before/after table becomes a
two-column grid (`before | after`) with the verdict above it; the stability chips stack; the CFD keeps the same
width. No horizontal scroll, no hover-only affordance (tap-and-hold for the popover).

## Technical design

### Annotations (a store table — the honest default)

Annotations are **first-party team metadata**, not scraped content, so they live in the store like every other
mutable datum — one DuckDB, one backup, one transactional write, one cache invalidation, queryable alongside the
events. New table in `schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS annotations (
    annotation_id VARCHAR PRIMARY KEY,   -- stable id (uuid)
    created_at    TIMESTAMP NOT NULL,
    date          DATE NOT NULL,         -- the change date shown on the axis
    kind          VARCHAR NOT NULL,      -- closed vocabulary (below)
    value         DOUBLE,                -- hours / commits / limit, or null
    note          VARCHAR,               -- FIRST-PARTY free text (the team's own words) — see the contract carve-out
    scope         VARCHAR,               -- optional repo
);
```

`kind ∈ {review_sla, wip_limit, size_cap, sentinel_added, sentinel_rotation, refinement_change, other}`;
`value` numeric or null; `note` optional free text; `scope` optional (a repo). The `kind`/`value` renders a closed
label (`review_sla` + 4 → "review SLA 4 h"); the `note` is the team's own words, stored verbatim.

**Contract carve-out (why a free-text column is allowed here).** `prd/02`'s "no free-text columns" rule is about
*imported content* (descriptions/comments/diffs/commit messages). `annotations.note` is first-party, never scraped,
never touched by the classifier/LLM, so it is only false-positive-prone for the store's heuristic — not a
violation. Concretely, in `ingest/store.py`:
- add `annotations` to `ALLOWED` (the table + its declared columns);
- exempt `annotations.note` from the whitespace `_scan` (the "a stored string must hold a pseudonym/key/enum,
  not prose" heuristic), with a comment naming it the **one** first-party free-text column;
- **every** other rule still applies to the table's other columns, and the canary test still proves no
  *scraped* raw text reaches the store. A migration adds the table to existing stores (empty).

`analysis/annotations.py`: `load(con)`, `save(con, entry)`, `delete(con, id)`, `catalogue()`, `render(entry)`,
`validate(entry)` — all against the store; **calendar spans** are still derived, never entered twice
(`calendar.summary()["holidays"]["dated"]`, `vacation_list()`). A write bumps the store stamp, so the existing
`_trend_cache` invalidation already covers a new annotation (no separate mtime key needed).

### Metrics (no new definitions)

`analysis/trend.py` gains: `cycle_p85` (add `p85_hours` to `rules.cycle_time`, so trend and headline share one
function), `throughput_stories` (the prd/60 delivered rule), `arrival_prs`, `arrival_stories`, and
`cumulative_flow` (per bucket, cumulative counts of PRs created / started / in review / merged, and stories
created / started / done — four series in one point, binned on the bucket's end). Every existing metric keeps its
`definition` string in the payload catalogue.

### Before/after (`analysis/before_after.py`)

```python
MIN_ITEMS = 8            # per side
MIN_BUCKETS = 3          # per side
NOISE_BAND = 0.20        # the smallest change this product acts on (the lab's ±20 % gate, concepts §7)
TRIALS = 10_000
```

- `compare(items_before, items_after, *, higher_is_better, seed)` where `items` are **item-level durations** (PR
  cycles created either side of the split; story cycles created either side), never bucket aggregates — a bucket
  mean would hide the distribution and shrink n.
- Primary statistic: the difference of medians; 95 % CI by percentile bootstrap (10k resamples, deterministic seed
  from `(tenant, stamp, calendar, metric, split, bucket)`).
- Secondary: two-sided Mann–Whitney U p (rank test, distribution-free), reported in the payload/tooltip.
- Outcome rule, in order:
  1. `n_before < 8` or `n_after < 8` → `cant_tell`, reason `small_n`;
  2. fewer than 3 buckets on either side, or another annotation within ±1 bucket → `cant_tell`, reason
     `confounded` or `short_window`;
  3. CI excludes 0 → `better` / `worse` by `higher_is_better`;
  4. `|median after − median before| < NOISE_BAND × median_before` **and** the CI lies inside ±`NOISE_BAND` of the
     before median → `no_clear_change` ("tight around zero");
  5. otherwise → `cant_tell`, reason `wide_interval` (the data cannot separate a change from noise).
- Censoring: open items are excluded from both sides and counted (`censored_before/after`), because a running age is
  not a completed duration; the panel says so when the count is non-zero.
- Cohort basis travels in the payload (`basis: "stories created before/after the split, created → done"`), so a
  reader cannot mistake it for the bucket series.

### Stability (`analysis/stability.py`)

- Theil–Sen slope over the per-bucket points (in-progress bucket excluded) for `wip` (open PRs at bucket end) and
  `throughput` (stories delivered or PRs merged per bucket — one population per series).
- `flag` (the combined reading, on `stability.flag`): `insufficient` when < 4 buckets; `overload` when WIP slope
  > +0.25/wk **and** throughput slope ≤ +0.05/wk (the queue grows while nothing leaves faster — the case that must
  veto a forecast, prd/60); `filling` when WIP slope > +0.25 and throughput grows but WIP grows > 2× faster (the
  queue grows relative to the exit rate); `stable` when |WIP slope| ≤ 0.25; `draining` when WIP slope < −0.25 and
  throughput ≥ 0. `filling` and `overload` both carry the sentence "Little's Law says cycle time will rise"; only
  `overload` refuses a forecast. The throughput series carries its own `trend: rising | flat | falling`.
- `little`: implied cycle = `mean(WIP) ÷ mean(throughput)` (weeks) vs the **Kaplan–Meier** median cycle
  (`rules.survival`, censoring-correct). A divergence > 2× is labelled `dead_wip` with the prd/59 count ("9 items
  have no live PR; they inflate WIP without ever departing").
- `arrivals_vs_departures`: cumulative arrivals − cumulative departures per bucket; `gap_items`, `gap_sentence`.

### API

`GET /api/trend` (extended; same params) adds:

```json
{ "…": "…",
  "annotations": [{"id": "a1", "date": "2026-10-08", "kind": "review_sla", "value": 4,
                   "label": "review SLA 4 h", "note": "adopted after the retro", "in_window": true}],
  "spans": [{"kind": "holiday", "start": "2026-08-20", "end": "2026-08-20", "label": "taasiseseisvumispäev"}],
  "stability": {"flag": "filling",
                "wip": {"slope_per_week": 1.0, "from": 6, "to": 17},
                "throughput": {"slope_per_week": 0.29, "mean": 8.15, "trend": "rising"},
                "little": {"implied_weeks": 1.4, "measured_weeks": 0.6, "flag": "dead_wip"},
                "arrivals_departures": {"gap_items": 25, "sentence": "…"},
                "note": "…"} }
```

`GET /api/trend/compare?tenant=&metric=&split=2026-10-08&bucket=week&days=` →

```json
{ "ok": true, "metric": "cycle_median", "unit": "hours", "higher_is_better": false,
  "split": "2026-10-08T00:00:00Z", "basis": "stories created before/after the split, created → done",
  "before": {"n": 62, "p50": 92.9, "p85": 142.0}, "after": {"n": 25, "p50": 83.0, "p85": 104.0},
  "diff": -9.9, "ci": [-22.3, 8.3], "p_value": 0.31, "test": "mann_whitney_u",
  "outcome": "cant_tell", "reason": "wide_interval",
  "annotation": {"id": "a1", "label": "review SLA 4 h"},
  "censored_before": 9, "censored_after": 4, "note": "…" }
```

`GET|POST /api/annotations`, `DELETE /api/annotations/{id}` — validated, written transactionally to the store's
`annotations` table; 400 only on an **unknown `kind`** (the type is enumerated so the chart/label is renderable);
the optional `note` is free text (first-party metadata) and is stored verbatim in the `annotations` table — the
one allowlisted first-party free-text column (see the contract carve-out above). 409/`unavailable` only when the
store is read-only (a mirror install still *reads* annotations).

### Cache and invalidation

Extend `_trend_cache` keys with `(metric, split, bucket)` for `/trend/compare`; a compare result is cached 32
entries. A new store stamp (which an annotation write bumps) or a calendar edit invalidates both; no separate
annotations-mtime key is needed because the rows live in the store. The bootstrap seed is derived from the same
key, so a cached and a cold call agree.

### Edge cases

- **No annotations:** an empty state that is a sentence, not a blank: "No policy changes recorded; a change you did
  not write down is indistinguishable from noise." plus *Record a change*.
- **Annotation outside the window / before the first bucket:** counted, not drawn.
- **Two annotations in one bucket:** `cant_tell (confounded)` — the panel says which two.
- **Split leaves <3 buckets or <8 items:** `cant_tell` with the reason; no CI bar.
- **Metric gaps:** compare uses items, so a missing week does not bias the split; the chart still shows gaps.
- **Active sprint bucket:** `in_progress` and excluded from both sides.
- **A `1w` window:** `cant_tell (short_window)`; the panel still renders the annotation and the counts.
- **Clock:** every CI is in the metric's own unit (hours, ratio, count); business hours for durations, never calendar
  days; `d = 8 h` in the copy.

### Tests

`tests/test_before_after.py` — CI coverage on a synthetic 20 % shift (95 % of 200 simulated datasets contain the
true diff); MWU p hand-checked on a small sample; all four outcomes reachable (fixtures); `NOISE_BAND` boundary
at exactly 20 %; determinism (same seed → identical CI); censored items excluded and counted; confounded split.
`tests/test_stability.py` — Theil–Sen slope on a known series (+1.0/wk on the del-combined WIP); `filling` on
del-combined (WIP +1.0 vs throughput +0.3) and `overload` on a flat-throughput fixture; `stable` on a flat series;
`dead_wip` when WIP ≫ KM median; `insufficient` with 3 buckets; only `overload` refuses a forecast.
`tests/test_annotations.py` — round-trip POST/GET/DELETE; every kind renders; an unknown kind → 400; a free-text `note` is accepted and stored verbatim in the `annotations` table; the store's `verify` passes with the allowlisted table + the `note` carve-out, and still rejects a free-text column on any *content* table; a transactional write leaves no partial row. `tests/test_trend.py` — `cycle_p85`
equals `rules.cycle_time`'s p85; spans include the 20 Aug holiday; in-progress bucket excluded from the compare.
`tests/test_server.py` — endpoint validation/cache. `app/test/prd61.test.mjs` — marker positions, span bands,
verdict pill wording, four outcomes, phone markup (no hover-only), Markdown export.

## Definition of done

- With an annotation `{date: 2026-10-08, kind: review_sla, value: 4}` on del-combined:
  story cycle `92.9 → 83.0 h`, CI `[−22.3, +8.3]`, outcome `cant_tell` (`wide_interval`, 62 · 25);
  PR cycle `24.8 → 24.7 h`, CI `[−3.5, +3.7]`, outcome `no_clear_change`. Both match a manual recompute.
- The stability strip flags `filling` on del-combined (WIP 6 → 17, +1.0/wk vs throughput 8.2/wk, +0.3/wk; arrivals
  10.1/wk vs departures 8.2/wk, +25 items) and prints the Little's-Law sentence (`implied 1.4 wk vs measured
  0.6 wk`, `dead_wip`); the 20 Aug holiday span renders from the calendar with no manual entry.
- Annotation CRUD round-trips, an unknown kind is a 400, a free-text note is accepted into the `annotations` table (the store `verify` passes), and a second annotation in the same bucket forces `cant_tell`.
- Every before/after on screen shows both `n`s, the CI and one of the four outcomes — never a bare p-value; the
  in-progress bucket never appears on either side.
- Tests green (Python + node), including prd/45 conformance.

## Builds on / non-goals

Builds on **50** (buckets and the "same metric function" rule), **19** (calendar spans, holidays and vacations),
**55** (censoring honesty), **59** (self-calibrated thresholds for the "is it a change" reading), **60** (the
throughput series and the WIP flag), **62** (guardrails get the same before/after treatment). Amends
`docs/concepts.md` §6 with *annotation* (a closed-vocabulary dated policy change), *before/after comparison* (item
distributions either side of a split, with CI and outcome) and *stability flag* (Theil–Sen WIP/throughput slopes).

Does **not**: claim causality or replace an experiment; let a note (or any free text) reach the
**event log / classifier / LLM view** (the note is first-party metadata in its own allowlisted table, never imported);
add AI attribution; benchmark against other teams; send alerts; forecast (prd/60); annotate automatically (if the
product guessed a policy change, "did it work" would become "did the product's guess work").

# 60 — Probabilistic throughput forecast ("how many, by when")

prd/52 classifies open stories at risk but cannot answer "how many of 30 will we finish by 27 Nov" — it prints a
deterministic classification. This PRD adds a Monte Carlo over **weekly stories delivered** (count, not points:
story points and logged hours are uncorrelated in this store, r ≈ −0.05), reads *when* at P85 and *how many* at
P15, makes the **interval the headline** ("22–30 of 30"), overlays prd/52's risk classes as adjustments, and
refuses with a reason when its assumptions break. Extends prd/52; conforms to `docs/concepts.md` (§1 throughput,
§6 sprint "done", §7 prediction language) and prd/10.

## User journey

18 Nov. Sprint 3 closes 27 Nov with 28 of 30 delivered. Priit plans sprint 4 (30 Nov – 27 Dec, 4 weeks) and asks
"how much can I take?". The Sprints page card answers:

> **22–30 of 30** · by 27 Dec · **P50 27**
> 29 % chance all 30 land by 27 Dec; 85 % by 10 Jan.
> *What the model can't see:* 1 at risk — #60 (DEL-176) has waited 18 days on FrMo.

He commits 26, not 30 — and plans the last four for the first week of January. Then he asks about sprint 3's tail
("the 2 left?"): the card refuses to forecast 2 items and hands off — "a 2-item set is a risk question, not a
throughput question; #60 is stuck on a person (prd/52/59)". The same model applied to sprint 2's start would have
refused ("3 weeks of history, 8 needed") — which is the honest reason the tool never claimed 25/30.

## Product design

**Where.** A `forecast-card` at the top of the Sprints page (above the sprint strip), directly under the page's
question *Will we land what we committed?*, and a `Will it land?` button on prd/52's at-risk block that opens the
same card scoped to the active sprint. One component, one endpoint, two entry points.

**Layout (desktop).** 8 px overline `FORECAST`; the headline in 17 px/700 is the **interval and the date**:
`22–30 of 30 · by 27 Dec`; below it in 13 px secondary `P50 27 · 29 % chance all 30 · 10,000 resamples · 13 weeks`.
Then a 100 %-width **interval bar** (32 px): the P15–P85 span filled `--info-accent` at 22 % alpha, a 2 px P50 tick,
a 1 px hairline at the target N, labels under the ends (`22` / `27` / `30`). Third line: `What the model can't see`
→ up to 3 prd/52 risk rows (`#60 stuck on FrMo · 18 d`) and a separate count `delivered if the blocked items hold:
29`. Fourth line: `Refuse`-aware provenance (below).

**Parameters.** One inline control row: `Target [30] · By [sprint end ▾ | date] · Confidence [P15–P85]`. Changing
the target re-renders from the payload's pre-computed quantiles — the client never interpolates and never
re-simulates.

**Colour.** The bar is *encoding*, so it is neutral accent, not state. It turns `--warning` only when `p85 < target`
(even the optimistic edge misses) and never red: a forecast is information, not an incident. Zero and refusal
states are white/neutral with a full sentence.

**Honesty language (exact).** `Method: 10,000 resamples of 13 weeks of stories delivered (mean 6.7, sd 2.5, CV
0.38). Not points — points and logged hours do not track time here (r ≈ −0.05).` · `Both numbers are the 85 % edge:
"by" uses P85 because a date is a promise; "how many" uses P15 for the same reason. A count is capped at the target,
so a P85 of "30" can hide a 29 % chance of reaching 30 — the p(reach) line says which it is.` ·
Refusal copy: `Not enough history to forecast: 3 weeks observed, 8 needed.` / `Throughput is erratic (CV 0.74): the
range would be so wide it would look like a forecast and behave like a guess.` / `WIP rose 6 → 17 while throughput
did not follow — Little's Law says cycle time will grow, so a throughput forecast would be optimistic. Fix the queue
first (prd/61).`

**Phone (≤640 px).** Three lines: interval headline (17 px), `by 27 Dec · P50 27` (13 px), and a `2 at risk →`
disclosure. The bar is 32 px tall with only the three numbers under it; no axis, no chart, no horizontal scroll.

## Technical design

### Data model

`analysis/forecast.py` (pure; reuses `trend.week_bounds`).

- `MIN_WEEKS = 8`, `MAX_CV = 0.6`, `TRIALS = 10_000`, `HOW_MANY_Q = 0.15`, `WHEN_Q = 0.85`, `WHEN_P95 = 0.95`.
- `throughput(court, jira, window)`:
  - buckets = Monday 00:00 UTC weeks (the event histogram alignment, prd/50);
  - count = stories whose `done` boundary falls in the bucket **and no open PR names them** (the prd/37 sprint
    "done" rule — a Jira-done story with an open PR is *unmerged*, not delivered, so the throughput series cannot
    disagree with the sprint lens);
  - the bucket containing the snapshot is `in_progress` and **excluded from the sample** (it is a partial week);
  - `{values, buckets:[{start,end,count,in_progress}], n, mean, sd, cv, basis:"stories delivered / week"}`.
- `assess(series, wip)` → `{available, reasons:[{code, sentence, minimum}], warnings:[{code, sentence}]}`; refusal
  codes `sparse` (n < 8), `erratic` (cv > 0.6 or sd == 0), `overload` (prd/61's flag is `overload`: WIP rising while
  throughput is flat or falling), `no_jira` (falls back to merged-PR throughput with `basis` changed, never silently
  mixed). The `filling` flag (WIP rising *faster* than throughput) is a **warning**, not a refusal: the interval is
  shown with the caveat, because the exits are still growing.
- `forecast(series, n, periods, seed)`:
  - resample the observed weekly counts **with replacement** (empirical distribution, no normal assumption);
  - *how many*: for each trial, sum `periods` draws, cap at `n` → quantiles P15/P50/P85, plus
    `p_reach_target` = share of trials whose *uncapped* sum ≥ `n` (a capped count hides the real confidence: P85 can
    read "30" when only 29 % of runs reach 30);
  - *when*: cumulate draws until the running total ≥ `n`, cap at `horizon_weeks` (default 26); record the period
    index; `when = {p50, p85, p95, unfinished_share}` — `unfinished_share` is the honest "may not land inside the
    horizon" number, never hidden;
  - `periods_to_date(period_index, start)` → period end = `start + 7 × index − 1` days (the last day of the period),
    clipped to the sprint band when the target is a sprint; the payload carries the ISO date **and** the period
    index, plus `working_days = periods × 5` (dates are calendar; hours stay business hours).
- `adjust(forecast, risk)` — prd/52 classes are **adjustments, not inputs**:
  - `delivered_now` = stories already done and merged; `remaining` = target − delivered_now;
  - `at_risk` = prd/52's rows with their class (`done-unmerged`, `stuck-on`, `no-live-pr`, `approved-unmerged`);
  - two counts, never blended: `how_many_if_the_at_risk_merge` (the model's number) and
    `how_many_if_they_do_not` (each `done-unmerged`/`no-live-pr` item removed from the ceiling). The card prints the
    first as the headline and the second on the "can't see" line.
- Determinism: `seed = crc32(f"{tenant}|{as_of}|{n}|{periods}|{horizon}")`, so two fetches return the identical
  interval. A different window legitimately gives a different number, and the payload states `seed` and `trials`.

### API — `GET /api/forecast?tenant=&days=&repo=&n=&by=sprint|date&horizon_weeks=&target=sprint:<id>|epic:<key>|open`

```json
{ "ok": true, "as_of": "…", "hours_mode": "business",
  "throughput": {"basis": "stories delivered / week", "n": 13, "mean": 6.69, "sd": 2.52, "cv": 0.38,
                 "buckets": [{"start": "2026-08-17T00:00:00Z", "count": 2, "in_progress": false}, …]},
  "assess": {"available": true, "reasons": [], "warnings": [{"code": "wip_filling", "sentence": "WIP rose 6 → 17 (+1.0/wk) while throughput rose +0.3/wk — the interval is optimistic; plan at P85."}],
             "minimum_weeks": 8},
  "request": {"n": 30, "periods": 4, "by": "2026-12-27", "target": "sprint:4"},
  "forecast": {"how_many": {"p15": 22, "p50": 27, "p85": 30}, "p_reach_target": 0.29,
               "when": {"p50_weeks": 5, "p85_weeks": 6, "p95_weeks": 6,
                        "p50_date": "2027-01-03", "p85_date": "2027-01-10",
                        "unfinished_share": 0.0},
               "trials": 10000, "seed": 1234567, "method": "bootstrap resample of weekly counts"},
  "adjust": {"delivered_now": 0, "remaining": 30,
             "at_risk": [{"kind": "stuck-on", "key": "DEL-176", "pr": "#60", "holder": "FrMo", "hours": 131.6}],
             "how_many_if_the_at_risk_merge": {"p15": 22, "p50": 27, "p85": 30},
             "how_many_if_they_do_not": {"p15": 21, "p50": 26, "p85": 29}},
  "delegate": null, "available": true, "note": "…" }
```

`available:false` (with `assess.reasons`) instead of a number. `delegate: {"to":"prd/52","reason":"a 2-item set is a
risk question"}` when `n < 5` or `remaining < 5` — the sprint card then renders the prd/52 list, not a fake
interval.

**Wiring.** `/api/sprints` links each sprint band to `/api/forecast?target=sprint:<id>`; the card fetches once per
(target, n, band). `/api/forecast` is the only forecast endpoint (no second implementation on the sprint payload).

### Cache and invalidation

`_forecast_series_cache` keyed `(tenant, stamp, calendar key, days, repo)`; `_forecast_cache` keyed
`(series key, n, periods, horizon, target)`. Both cleared like `_trend_cache`; 32 and 128 entries. 10k trials ×
≤26 periods is ~2 ms, so the cache exists for **consistency** (one interval per request), not speed.

### Edge cases

- **Sparse / erratic** → refuse with the sentence and the minimum (the fixture: 5 buckets; CV 0.9).
- **WIP rising** → `overload` (WIP up, throughput flat or down) refuses; `filling` (WIP up faster than throughput)
  only warns, because the exits are still growing; both link prd/61's stability strip.
- **Target larger than history can reach** (periods > n_weeks): the interval widens legitimately; a note says
  "forecasting 7 weeks from 13 observed — the range is the answer".
- **Capped count** (`p85 == n`): the card always prints `p_reach_target`, so "30" is never read as "likely 30".
- **Zero-throughput weeks** stay in the sample (they are data, not missing buckets); a week before the team existed
  is not in the window at all.
- **Sprint with scope added after start** (23 of 30 in S1, 28 adopted in S2): the payload carries
  `scope_added_after_start` and the card says "this forecasts the scope as of today; scope added later is not in
  the interval" — the commitment caveat, not a silent adjustment.
- **No Jira** → merged-PR throughput, `basis` changed, and the card's unit says "PRs", never "stories".
- **Censored items** already in flight are not in the throughput sample (only finished weeks count).
- **`n = 0`** → "nothing to forecast"; **`n` between 1 and 4** → delegate.

### Tests

`tests/test_forecast.py` — weekly counts equal a manual recount on del-combined (2,3,7,8,6,7,5,7,10,6,8,6,12, the
last bucket in progress and excluded); `mean 6.69 / sd 2.52 / cv 0.38` from that series; in-progress exclusion;
`unmerged` stories excluded; MC determinism (same seed → identical p15/p50/p85); `p_reach_target` = 0.29 for
(30, 4) and monotone in periods; quantile monotonicity; cap at `n`; `when` conversion (5 weeks → 3 Jan 2027 from
30 Nov); `unfinished_share` decreases as the horizon grows; refusal for 3 buckets, CV 0.9, `overload` fixture (each
reason code); `filling` warns but does not refuse (del-combined); `n<5` delegation; PR-basis fallback changes the
unit; Theil–Sen WIP slope on the del-combined series.
`tests/test_server.py` — payload shape, 400 for `n<0`/bad `by`, cache stability across two fetches, `available:false`
body. `app/test/prd60.test.mjs` — the headline is the interval, not the point; P50 tick position proportional
(22/27/30 → 0 %/62.5 %/100 %); warning colour only when `p85 < target`; refusal copy; delegation renders the
prd/52 rows; phone markup has no chart and no overflow.

## Definition of done

- del-combined, `n = 30, 4 weeks` (30 Nov – 27 Dec) → `how_many = 22 / 27 / 30`, `p_reach_target = 0.29`,
  headline `22–30 of 30`, `when.p50 = 5 weeks (3 Jan)`, `when.p85 = 6 weeks (10 Jan)`; the same request twice
  returns the same seed and interval.
- `n = 30, 2 weeks` → `9 / 13 / 17`; the active sprint's remaining 2 delegates to prd/52 and prints no interval.
- A `1w` window refuses: "1 week observed, 8 needed" (or the actual count for the window), with no number shown.
- The throughput series matches the sprint lens on the same store (a story counted delivered by one is delivered by
  the other) and reproduces the 13 weekly counts by hand.
- Every forecast on screen carries `n`, the interval, `p_reach_target`, the method and the seed; `≈` is used for the
  predicted numbers, and the headline is never a single number.

## Builds on / non-goals

Builds on **52** (the at-risk classes this overlays and defers to at small n), **37** (sprint done-by-end and
scope-added), **50** (the Monday week buckets), **59** (the p85 convention: plan at the confident edge), **61**
(the WIP flag that can veto a forecast). Amends `docs/concepts.md` §6 with *throughput forecast* (a bootstrap
distribution over delivered-item counts, with its `n`, CV and method) and *the 85 % edge convention*.

Does **not**: use story points or velocity; estimate a single story's completion (that is prd/59's aging plus
judgement); forecast per person; add a commitment/roadmap workflow; claim a DORA-style prediction; hide the interval
behind a point estimate; run without stating its refusal conditions.

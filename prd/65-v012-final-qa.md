# 65 — v0.12.0 final-QA fixes (N-01 … N-10)

Source: `docs/QA-v012-final.md` (final verification on `19287c5`). The 22 `prd/64` findings are all
**FIXED** with **0 regressions**; these are findings on the new v0.12.0 surfaces (verdict, aging,
forecast, intervention trend, yield/guardrails). Every finding gets a fix and a regression test.
Conform to `docs/concepts.md`; follow `prd/10`. IDs trace the report.

## Dispatch map (file-disjoint, parallel-safe)

| unit | findings | files |
|---|---|---|
| **A — aging + main.js + stylesheet** | N-01, N-03, N-04, N-05, N-06, N-07, N-08 | `app/main.js`, `app/aging.js`, `app/style.css` |
| **B — forecast** | N-02 | `app/forecast.js` |
| **C — before/after** | N-09 | `analysis/before_after.py`, `app/intervention.js` |
| **D — policy guardrail** | N-10 | `app/policy.js` |

---

## A. Aging tab, phone trend bar, annotation echo (N-01, N-03, N-04, N-05, N-06, N-07, N-08)

### N-01 BUG (must-fix) — repo scope empties the story-aging table while chip and chart show 7
- **Cause:** `/api/aging?kind=story&repo=…` returns 7 stories server-side, but `agingMatchesScope()` →
  `matchesScope(row, scope)` checks `row.repo` / `row.repo_short`, which **story** rows do not carry. Every
  story row is filtered out client-side: tab badge `Aging 0`, table "Nothing in flight matches this view",
  while the chip says `7 open · 5 past p85` and the strip draws 7 dots.
- **Fix:** make repo scoping honest for story rows — either carry the joined `repos` on `/api/aging` story
  rows and match against them, or skip the client-side repo predicate for `row.kind === 'story'` when the
  request was already repo-filtered. One rule, stated in a comment; the tab badge, table, chip and chart
  must agree.
- **Test:** `?scope=repo:DEL/delivery-api&tab=aging` → badge 7, table 7, chip 7, 7 dots; a story with no
  PR in that repo is still excluded when the server does the join.

### N-03 PAPERCUT — phone Trend metric bar overflows (717 px vs 412)
- **Cause:** `#trend-metrics` grew to 10 tabs; `.seg` does not wrap on phone, so `documentElement.scrollWidth`
  is 717.
- **Fix:** in `@media (max-width: 640px)` let `#trend-metrics` wrap (`flex-wrap: wrap`) or scroll inside its
  own `overflow-x: auto` container — the page itself must stay at 412. No desktop change.
- **Test:** source guard (phone block has the rule) + harness `scrollWidth === 412` on Trend at 412×915.

### N-04 PAPERCUT — recording/deleting an annotation flips the status flash after 1 s
- **Cause:** `POST/DELETE /api/annotations` bumps the store stamp; the `/ws` watcher broadcasts `changed`
  ~1 s later and `load()` resets the status bar from "recorded review SLA 4 h" to "live · …".
- **Fix:** suppress or absorb the watcher echo caused by our own annotation write (e.g. ignore the next
  `changed` for the tenant while the confirmation is fresh, or keep the flash for its normal duration).
  Do not suppress *other* tenants' changes.
- **Test:** record an annotation → status still says "recorded …" after the watcher tick; an external
  store change still refreshes.

### N-05 PAPERCUT — person/role scope filters the table but not chip, chart, honesty or re-plan list
- **Cause:** `agingChart()`, `[data-aging-counts]`, `#honesty` and `replanList()` receive the unfiltered
  rows while the table receives the scoped rows.
- **Fix:** pass the filtered rows (one `rowFilter`) into all four, so counts/dots/honesty/re-plan match the
  table and tab badge under any scope. `#aging-replan` must not list team-wide dead stories under
  `person:CaOs`.
- **Test:** `person:CaOs` on PR review → chip 1, 1 dot, honesty "1 in flight", no re-plan rows; on Story
  cycle → all four agree at 0.

### N-06 POLISH — percentile tick labels collide above the desktop strip chart
- **Cause:** absolute `left: %` labels, ~76 px wide, cluster in the left 6–31 % of an axis scaled to the
  oldest item (p50/p85 overlap 7 px on stories; p50/p75/p85/p95 overlap 30–45 px on PRs).
- **Fix:** use the phone's readable inline tick row on desktop too (p50/p75/p85/p95 with the duration in
  the title), or clamp/collapse colliding labels. No overlap at 1600×1000 on either stage.
- **Test:** bounding boxes of adjacent `.aging-tick-labels span` do not intersect (node test against the
  rendered markup's computed positions, or a harness bounding-box assertion).

### N-07 POLISH — `.aging-meta` prints the censoring/vacation sentences twice
- **Cause:** `agingMarkup()` hardcodes the two sentences and also appends `payload.note`, which is exactly
  those two sentences.
- **Fix:** render `payload.note` once — either drop the hardcoded pair and print the note, or omit the note.
- **Test:** the two sentences appear exactly once in the rendered Aging tab.

### N-08 POLISH — desktop aging table truncates `story` and `91 calendar days`
- **Cause:** `.aging .c-kind { width: 58px }`, `.aging .c-age { width: 170px }`, and the re-plan `WHY`
  cell is `nowrap` + ellipsis.
- **Fix:** widen `.c-kind` to fit `story` (≈68 px), `.c-age` to fit `64 d 4 h · 91 calendar days` (≈195 px)
  or allow wrap; set the re-plan reason cell to wrap.
- **Test:** no `scrollWidth > clientWidth` on `.c-kind`, `.c-age`, or the re-plan `WHY` at 1600×1000
  (source/markup guard or harness measurement).

---

## B. Forecast card (N-02)

### N-02 BUG (must-fix) — degenerate `30–30` interval overlaps its end labels into "3030"; no Copy as Markdown
- **Cause:** `forecastBar()` positions all three end spans at `left: 100%` when `p15 === p50 === p85`
  (`high === scaleLow`), so `translateX` variants collide. The card is also the only analyst surface with
  no Markdown export.
- **Fix:** deduplicate the end labels when quantile positions coincide (print the value once at the right
  edge, e.g. `30`), and give `#forecast-card` a `forecastMarkdown()` + copy chip that keeps `?tenant=` and
  states the interval, the by-date, p(reach), n/weeks/CV, the at-risk ceiling and any refusal/warning.
- **Test:** a degenerate 30/30/30 payload renders one `30` end label and no overlapping boxes; a normal
  20/26/30 renders three; the Markdown contains the interval + tenant and is produced without a network
  call.

---

## C. Before/after censoring note (N-09)

### N-09 POLISH — the censoring sentence prints twice
- **Cause:** `before_after.py` appends the "N open item(s) are excluded…" sentence to `payload["note"]`
  (rendered at the top) and `beforeAfterPanel()` also renders it below the table.
- **Fix:** keep the detailed below-table line and drop the duplicate suffix from `note` (or vice versa);
  one sentence, one place.
- **Test:** with censored items on both sides, the sentence appears exactly once.

---

## D. Policy-lab guardrail (N-10)

### N-10 POLISH — `cap PRs at 3 commits` guardrail shows `rounds` in the lab, `PRs per 100 commits` on Rules
- **Cause:** `labGuardrail()` hardcodes `rounds_mean` (or `modelled[0]`), ignoring the worsening
  packaging metric that `analysis/advice.py` selects (`GUARDRAIL_PRIMARY["size3"]`).
- **Fix:** prefer the entry flagged `worse === true` (matching the Rules choice), falling back to
  `rounds_mean`; both surfaces quote the same primary guardrail for the same preset.
- **Test:** `size3` preset renders `PRs per 100 commits 20.2 → 42.2` with the warning tint; unchanged for
  presets whose primary is `rounds`.

---

## Definition of done

- All 10 findings fixed, each with a regression test that fails without the fix.
- Suites green (`tests/`, `scrape/tests`, `node --test app/test/*.mjs`), `--verify` on del-combined still
  `contract OK`, phone `scrollWidth === 412` on every view.
- Then cut the **new** `v0.12.0` tag on the verified commit (never re-point `v0.11.0`/`v0.11.1`).

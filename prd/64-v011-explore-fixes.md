# 64 — v0.11.1 free-exploring QA fixes — COMPLETE checklist (all 22 findings)

Source: `docs/QA-explore-v011.md` (free-exploring pass on `v0.11.1`, `del-combined` + `del3`, dark/light,
1600×1000 / 412×915). This is the full inventory — **every** finding, grouped by the dispatch unit that
owns it, each with its fix and its regression test. Conform to `docs/concepts.md`; follow `prd/10`
(one question per view, density, colour = state only). IDs trace the report (`F-01` … `F-22`).

**Verdict from the pass:** conditional ship — 4 must-fix bugs (F-01, F-03, F-07, F-11), 2 further bugs
(F-08, F-12), 7 misleading numbers, 6 papercuts, 3 polish. Nothing here is architectural; every fix is
local, and each must land with a test that fails without it (repo rule 1).

## Dispatch map (file-disjoint units, parallel-safe)

| unit | findings | files it owns | runs |
|---|---|---|---|
| **G1 phone layout & touch** | F-01, F-02 | `app/style.css` | after Wave 2 (style.css) |
| **G2 no-Jira honesty** | F-03, F-06b (`jira.py` sprint `never_done`) | `analysis/jira.py`, `analysis/simulation.py`, `analysis/advice.py`, `server/app.py`, `app/policy.js` | after prd/58 + prd/61 (app.py, advice.py) |
| **G3 scope correctness** | F-07, F-08 | `analysis/ballcourt.py`, `app/tables.js` | after prd/58 (ballcourt.py, tables.js) |
| **G4 state/overlay bugs** | F-04, F-09, F-10, F-11, F-12, F-13 | `app/main.js` | after prd/58 (main.js) |
| **G5 rules correctness & polish** | F-05, F-06a, F-18, F-19 | `app/rules.js`, `analysis/rules.py` | after prd/58 (rules.py, rules.js) |
| **G6 ticket honesty & polish** | F-14, F-15, F-21, F-22 | `analysis/tickets.py`, `app/ticket.js` | **now** (no overlap) |
| **G7 trend & queue numbers** | F-16, F-17 | `app/queues.js`, `analysis/trend.py`, `app/trend.js` | F-16 **now**; F-17 after prd/61 (trend.py/trend.js) |
| **G8 build hygiene** | F-20 | `VERSION`, `server/version.py` | **now** (orchestrator) |

Two findings straddle a unit boundary and are split on purpose: **F-06** backend (`affected` flags in
`analysis/rules.py` + `analysis/jira.py`) and client (`affected`-first sort in `app/rules.js`) — the
backend half rides G5's `rules.py`, the `jira.py` half rides G2 to keep G5 single-file; **F-17** backend
(`analysis/trend.py`) and UI (`app/trend.js`) stay together in G7 because one agent must own both ends of
the min-n contract.

---

## G1 — Phone layout & touch targets (F-01, F-02)

### F-01 BUG (must-fix) — phone Delivery list & Queue response cards clip every value off-screen
- **Cause:** `.delivery { min-width: 1120px }` (`app/style.css:1158`) and
  `.response-panel .response-table { min-width: 900px }` (`:1169`) are not reset inside
  `@media (max-width: 640px)`. `.table` turns rows into stacked cards (`:1967`), so each card is 900–1120 px
  wide inside a 412 px scroller: labels left, values at `x ≈ 880–1100`.
- **Fix:** in the 640 px media block set `.delivery, .response-panel .response-table { min-width: 0 }`
  (same treatment `.drill` already gets at `:2050`). Audit every `min-width` > 412 px on a table/view
  container and reset it there; the phone layout must not rely on one-off fixes.
- **Test:** a source-level guard in `app/test/` that fails if a `min-width` > 412 px on `.delivery`,
  `.response-table`, or any stacked `.table` container is not reset in the ≤640 px block; plus a harness
  assertion that `scrollWidth === 412` on Delivery (All 8) and Queue response-time at 412×915 (both themes).

### F-02 PAPERCUT — phone tap targets below 44 px
- **Cause:** Blocking text-view `<summary>` 16 px tall, policy-lab checkboxes 13 px wide with no 44 px row,
  `.yah-step` 34 px, `#ticket-jump-input` 36 px, `.skip-link` / timeline carry-over `<summary>` 35 px.
- **Fix:** in the phone media block give each interactive control `min-height: 44px` (labels pad the full
  row; the checkbox itself may stay visually small but its label is the target). Desktop density unchanged.
- **Test:** node source guard asserting the phone block carries the 44 px rule for
  `.blocking summary`, `.policy-knob`, `.yah-step`, `#ticket-jump-input`, `.skip-link`, and timeline
  `<summary>`; no desktop rule changed.

---

## G2 — No-Jira honesty (F-03, F-06b)

### F-03 BUG (must-fix) — `del3` fabricates a 1 h refinement and headlines "refine before start"
- **Cause:** `jira_analysis.refinement_cost()` (`analysis/jira.py:3823`) returns
  `DEFAULT_REFINEMENT_HOURS = 1.0` when there are no `created → ready` spans. SimPy then trades a full
  rework round (`≈5.8 h`) for a 1 h timeout, so Rules and the Policy lab promote `refine before start`
  (`−24.8 %`) on a store with **no Jira layer** — contradicting `del-combined` (`+82 % — not a measured
  win`) and `app/policy.js:353`, which expects `hours == null` to print "not measurable (no Jira layer)".
- **Fix:**
  1. `refinement_cost()` returns `{"hours": None, "n": 0, …}` when nothing is measured — delete the
     fabrication. If a model needs a horizon, it must come from the store or the scenario is unavailable.
  2. `analysis/simulation.py`: a scenario whose calibration input is `None` is `available: false` with the
     reason ("no measured refinement segments"), never silently modelled with a default.
  3. `analysis/advice.py`: `best scenario` ranking excludes unavailable scenarios; the Rules banner must
     not name refinement on a PR-only store.
  4. `server/app.py`: `_advice_payload` / `/api/simulate` expose the unavailable state (no fabricated
     `hours`), and the same calibration reaches both the Rules note and the lab.
  5. `app/policy.js`: render "not measurable (no Jira layer)" and disable the `refine before start` knob
     when `refinement.hours == null`.
- **Test:** synthetic PR-only store (no Jira): `calibration.refinement.hours is None`, `refine_before_start`
  `available: false`, Rules `best scenario` does not name refine, banner does not claim a refinement win;
  `del-combined` unchanged (still `+82 %`). Existing simulation validation tests must pass untouched.

### F-06b MISLEADING — sprint-on-time marks "still open" stories as unaffected
- **Cause:** `analysis/jira.py:1979` sets `affected: False` for `never_done` stories; only `late` count, so
  S2 reads "12 affected" of 15 even though all 15 missed the close.
- **Fix:** `never_done` (still open at the sprint close) is `affected: True`; keep the outcome label
  distinct so the drill-down still says *why* it missed (`late` vs `still open`).
- **Test:** a fixture sprint with late + never_done + by_end items → affected count includes both misses;
  the by_end rows stay unaffected.

---

## G3 — Scope correctness (F-07, F-08)

### F-07 BUG (must-fix) — `person:CaOs` includes PR #66, which waits on `BoFe + FrMo`
- **Cause:** `analysis/ballcourt.py:2258` does
  `owner = (tail.owner if tail else None) or (current.owner if current else None)`. On #66 the unobserved
  tail has two holders → `tail.owner is None`, so the **ended** interval's owner (`CaOs`) leaks into
  `waiting_for`; `matchesScope()` (`app/tables.js:240`) then matches `CaOs` and both Overview and Delivery
  list show #66 under `person:CaOs` (and "1 BLOCKED").
- **Fix:**
  1. `owner = latest.owner if latest is not None else None` — never fall back to an ended interval. When
     several hold the ball, `waiting_for` is the role label, and `waiting_on` / `waiting_roles` carry the
     real holders.
  2. `matchesScope()` builds the holder set from `waiting_on` when non-empty, else `[waiting_for]`; never
     treats `ball_label` as a person. Role scopes match `waiting_roles`, not pseudonyms.
- **Test:** `person:CaOs` excludes #66; `waiting_for` for #66 is `sentinel`/`review` (not a person);
  a role scope still includes the row; server-side and client-side scope filters agree.

### F-08 BUG — "Done in Jira, PR open" ignores `role:*` scopes
- **Cause:** `doneOpenMarkup()` (`app/tables.js:487`) matches `scope.value` (`"sentinel"`) against
  pseudonyms in `waiting_on`, so `role:sentinel` / `role:author` say "none — 3 team-wide".
- **Fix:** filter with `matchesScope(row, scope)` (which now handles `role`), so the panel agrees with the
  rest of Overview/Delivery list.
- **Test:** `role:sentinel` lists the 3 Done-open PRs; `role:author`/`role:review` list the rows whose
  `waiting_roles` say so; `all` unchanged.

---

## G4 — State & overlay bugs in `app/main.js` (F-04, F-09, F-10, F-11, F-12, F-13)

### F-11 BUG (must-fix) — desktop `#delivery-detail` action buttons do nothing
- **Cause:** the `[data-detail]` handler always reads `state.drawerEntity`; on desktop the drawer is closed
  (`null`), the selected entity lives in `state.selection`, so Copy-as-Markdown copies `""` (while flashing
  "copied") and Timeline opens unscoped.
- **Fix:** resolve `const entity = pivot.closest('#drawer') ? state.drawerEntity : (state.selection ||
  state.drawerEntity)`. All three buttons (`timeline`, `blocking`, `copy`) act on the selected row.
- **Test:** desktop Delivery row → Copy as Markdown yields the row's link; Timeline scopes to the row;
  Blocking focuses the row; the drawer path still works.

### F-12 BUG — PR drawer repeats four facts with contradictory values
- **Cause:** the live-status rows and the measured phase rows both print `waiting on author`, `CI`,
  `rebasing`, `no events since`; e.g. `waiting on author: yes · 2 d 1 h` above `waiting on author: —`
  (the wait is in the unobserved tail).
- **Fix:** group the measured phase rows under one `PHASES` subheading with a distinct label
  (`waiting on author · measured`), keep the live-status rows as the only bare keys, and make the
  tail explicit: `waiting on author: yes` + `measured: —` + `unobserved (no events since): 2 d 1 h`.
  One key per `<dl>`.
- **Test:** open #66's drawer → every `<dt>` unique; the `yes` and the measured `—` coexist with the
  unobserved tail explaining the gap.

### F-04 MISLEADING — Overview tiles mutate when Queue/Blocking level is toggled
- **Cause:** Overview reads the global `state.queueLevel` / `state.blockingLevel`, so switching Queue to
  `queues` changes Overview from `0 OVERLOADED PEOPLE (≈304 d 6 h)` to `2 OVERLOADED QUEUES (≈340 d 0 h)`
  while the subtitle still says "role-level handover graph, each handover once".
- **Fix:** Overview's headline tiles always query their **own canonical level** (role-level handover graph
  for MEASURED WAIT and queue diagnosis) and say so; the Queue/Blocking toggles only affect those views.
  If a level is ever shown on Overview it must carry that level's basis text. No subtitle may claim a
  basis the number does not use.
- **Test:** mutate `state.queueLevel`/`state.blockingLevel`, re-render Overview → tiles and subtitles
  identical; the Queue view still honours its toggle.

### F-09 PAPERCUT — route notice overwritten / stale / sticky
- **Cause:** `loadTenants()` calls `setNotice(..., { append: false })` and wipes the invalid-view/`tf`
  notice; an invalid `?view=` inherits the stale `localStorage` page; scope/`tf`/tenant edits do not clear
  the notice.
- **Fix:** `{ append: true }` in `loadTenants()`; an invalid `?view=` resets `state.page` to the default
  entry view; changing scope/timeframe/tenant clears `#route-notice`.
- **Test:** `?tenant=nonexistent&view=bogus&tf=99y` → both notices present, page is the default view;
  editing the timeframe clears the notice.

### F-10 PAPERCUT — leaving Timeline with a Sprint lens leaves all timeframe buttons unpressed
- **Cause:** `custom` is timeline-only, so on other views `active = !custom && …` is false for every
  button while the range shows `1q`.
- **Fix:** on non-timeline views highlight `state.timeframe` (the preset those views actually query).
- **Test:** set a sprint lens on Timeline, go to Overview → `1q` is pressed and no `custom` chip.

### F-13 PAPERCUT — clearing the Sprint lens leaves Timeline trapped in the sprint window
- **Cause:** `clearSprintFocus()` leaves `state.timeframeCustom = true`, so the window stays
  `17 Aug 09:00 → 10 Sep 16:00` under a `custom` chip.
- **Fix:** clearing restores the active preset (`timeframeCustom = false`, `applyTimeframeToTimeline()`),
  and clicking the active sprint row/child toggles the lens off consistently.
- **Test:** click S1, then clear → window equals the `1q` preset and the chip is gone; clicking the S1
  badge while active clears it.

---

## G5 — Rules correctness & polish (F-05, F-06a, F-18, F-19)

### F-05 MISLEADING — `people (pseudonyms)` tab keeps team stats and repeats the empty message
- **Cause:** `rulesStats()` (`app/rules.js:500`) counts all of `data.suggestions` without the level filter;
  `rulesMarkup()` prints the empty sentence twice.
- **Fix:** filter stats by `data.level === 'person'` (render `0 rules · 0 critical · ≈0 min at stake` or
  `0 person rules (N team)`); one empty paragraph only.
- **Test:** person tab → stats reflect person rules; empty message appears exactly once.

### F-06a MISLEADING — drill-downs lose `affected`-first order; `round-cap` marks 0 affected
- **Cause:** `ruleItems()` (`app/rules.js:311`) re-sorts by `impactOf()` and drops the server's
  `_drill_order`; `analysis/rules.py:1304` sets `affected = count > MAX_REVIEW_ROUNDS` (`> 3`) while the
  rule's own text says "cap them at 2".
- **Fix:** sort `affected` rows first (`Number(Boolean(b.row.affected)) - Number(Boolean(a.row.affected))`)
  before `impactOf`; `affected = count > REVIEW_ROUND_CAP_TARGET` (`> 2`) for `round-cap`.
- **Test:** `cycle-time-censoring` puts its 3 affected open PRs first; `jira-worklog-mismatch` puts its 4
  affected stories first; `flow-efficiency` puts its 64 affected first; `round-cap` marks the >2-round PRs
  affected (24 over target) and the drill summary's affected count matches the evidence.

### F-18 PAPERCUT — raw floats in evidence chips
- **Cause:** `evidenceValue()` handles `*_hours`, `hours_per_round`, `hours_per_refinement`, but not
  `hours` or `hours_per_extra_round`.
- **Fix:** format both through `fmtImpact` (`≈6 h 1 min`, `≈35 d 4 h`); add a guard so no evidence chip
  prints a bare float.
- **Test:** rule 04 shows `per extra round: ≈6 h 1 min`; rule 10 shows `hours: ≈35 d 4 h`.

### F-19 PAPERCUT — Markdown export self-attributes rule #7
- **Cause:** `attribute_at_stake()` (`analysis/rules.py:3584`) sets `attributed_to = owner["id"]` when the
  rule's own stake is 0, so #7 exports "counted in #7"; the UI hides it (`pool_rules < 2`) but Markdown
  does not.
- **Fix:** only set `attributed_to` when `owner["id"] != suggestion["id"]` and `hours > 0`;
  `rulesMarkdown()` guards `stake.pool_rules >= 2`.
- **Test:** the #7 export has no "counted in #7"; the shared-pool note still appears on the rule that
  legitimately points elsewhere.

---

## G6 — Ticket detail honesty & polish (F-14, F-15, F-21, F-22)

### F-14 MISLEADING — DEL-183 ticket page omits "PR opened after the ticket closed"
- **Cause:** Overview/Delivery flag `#66` opened 6 d 4 h after DEL-183 closed; the ticket page never says
  it. `doneOpen` already computes `opened_after_close_hours` in `analysis/tickets.py` for the done-open
  panel, but the ticket payload's PRs do not carry it.
- **Fix:** compute `opened_after_close` / `opened_after_close_hours` per PR in the ticket payload; the
  ticket narrative and the `#66` PR row state `opened 6 d 4 h after the ticket closed — the work cannot
  have been part of the delivered story`.
- **Test:** `DEL-183` ticket payload and rendered page contain the badge/sentence; a PR opened before the
  close does not.

### F-15 MISLEADING — DEL-161 headlines `32 d 4 h cycle` beside `2 d 0 h (30%)`
- **Cause:** the stats strip prints the full `cycle_hours` (age to snapshot for an open story) next to a
  segment share computed over the **measured** sum (7 d 0 h), so `2 d 0 h (30%)` reads as 30 % of 32 d.
  The approved-never-merged finding (`#34` approved by peers + sentinel, bot-declined, 23 d 3 h lost) is
  also missing from the ticket page.
- **Fix:** when `measured < total`, print the strip as `32 d 4 h age (open) · 7 d 0 h measured across
  7/10 segments`, and label the largest share `30% of measured`; add the `approved_unmerged` finding to
  the narrative and the `#34` PR row (`approved by DaKo + FrMo, closed unmerged by the bot`).
- **Test:** `DEL-161` strip says age + measured; the percentage is labelled "of measured"; the narrative
  and PR row name the approved-unmerged close.

### F-21 POLISH — boundary chips render `PR_OPEN`, `REVIEWER_ADDED`, …
- **Fix:** `boundaryChips()` replaces `_` with a space in the chip and its tooltip (`PR OPEN`,
  `IN VERIFICATION`).
- **Test:** no `_` in the rendered boundary chips or their titles.

### F-22 POLISH — "log delay: within week" beside "logged late by: 9 d 4 h"
- **Cause:** `log delay` is bucketed in working weeks (5 d = 40 h) while `logged_late_by` is 76 working
  hours formatted on an 8 h day, so `9 d 4 h` looks > one week.
- **Fix:** price `logged_late_by` on the same clock as the bucket (calendar days/hours) or relabel it
  (`logged late by 19 calendar days`), so the pair cannot contradict.
- **Test:** the DEL-155 worklog row's bucket and duration agree under one clock; a fixture crossing the
  one-week boundary is labelled consistently.

---

## G7 — Trend & queue numbers (F-16, F-17)

### F-16 MISLEADING — Queue `NEVER` cell `8 (3 only) +5 (21 d 4 h)` reads as "+5 aged 21 d 4 h"
- **Cause:** the oldest age is appended in the same muted style directly after `+5`, but it is
  `never_oldest_hours` across all 8 requests.
- **Fix:** render `8 · 3 only · +5 (oldest of 8: 21 d 4 h)` (or move the oldest age into its own labelled
  column/span), so the modifier clearly applies to the total.
- **Test:** the cell's text and title name the oldest as the oldest of the total, not of `+5`.

### F-17 MISLEADING — `Sprint on-time delivery` can never meet the n≥2 floor
- **Cause:** `bucket=sprint` puts exactly one sprint per bucket (`n=1`), so every bucket is flagged
  `1 low n`; the headline is permanently `– no bucket meets the n≥2 floor`. Timeline/Rules meanwhile say
  `0/2 sprints on time` (the ≥80 % target), a different question.
- **Fix:** for `metric=on_time` + `bucket=sprint`, the n-guard is the **sprint's committed story count**
  (`≥ 5`), not buckets; the headline reads `50.0% latest (S2 · 15/30 stories) · −10.0 pts since S1`. Keep
  the `n≥2` bucket floor for week buckets. Label the tab/metric as "sprint story delivery share" and keep
  the "sprints on time (≥80 %)" answer on Timeline/Rules rather than implying the same number.
- **Test:** sprint buckets report a percentage and a delta; a sprint with <5 committed stories is `low n`;
  week buckets keep the n≥2 rule; the metric label matches the plotted quantity.

---

## G8 — Build hygiene (F-20)

### F-20 POLISH — `VERSION` / `__version__` still say `v0.9.0`
- **Fix:** bump `VERSION` and `server/version.py:__version__` with the release tag; add a release test that
  fails when the two disagree with the newest reachable `v*` tag.
- **Test:** a test reads both files and asserts they carry the current release line (and the package test
  builds a `git archive` copy reporting the same version).

---

## Definition of done

- All 22 findings implemented, each with a regression test that fails without the fix.
- `docs/concepts.md` untouched unless a definition genuinely changes (it should not: these are honesty and
  layout fixes). `prd/10` respected: no new colour vocabulary, one question per view.
- Suites green: `.venv/bin/python -m unittest discover -s tests`, `... -s scrape/tests`,
  `node --test app/test/*.mjs`.
- `--verify` still passes on both demo stores (no new column, no imported text).
- No tag re-pointed: the fixes land under a **new** tag (`v0.12.0`), never on `v0.11.0` / `v0.11.1`.

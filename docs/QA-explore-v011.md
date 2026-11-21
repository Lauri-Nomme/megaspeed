# Adversarial QA Exploration — megaspeed `v0.11.1`

- **Target:** `v0.11.1` (`4cbca9a8`, verified via clean `git archive v0.11.1` tree)
- **Tenants explored:** `del-combined` (2,529 events · 131 PRs · 100 stories · 3 sprints) and `del3` (1,484 events · 131 PRs · 0 stories · 0 sprints)
- **Viewports & themes:** Desktop (`1600×1000`) and Phone (`412×915`), Dark and Light themes
- **Scratch & screenshots:** `/tmp/opencode/qa-explore-v011/*.png` (`01-overview-dark.png` .. `06-phone-delivery-dark.png`)

---

## Executive Verdict — Shippable vs. Must-Fix

**Verdict: Conditional ship — fix 4 high-impact bugs first.**
Core reconciliation across Overview, Delivery list, Ticket detail, Timeline, Trend, Blocking map, Queue, Rules, Policy lab, and Sources is solid, and deep links / Copy-as-Markdown / benchmark toggles / scope banners work reliably. However, the adversarial pass uncovered **22 findings** (6 bugs, 7 misleading numbers, 6 papercuts, 3 polish items), including **4 must-fix issues**:

1. **Phone layout bug (`412×915`):** Delivery list and Queue response-time cards are clipped off-screen (`min-width: 1120px` and `900px` not reset in `@media (max-width: 640px)`), hiding every value column on phone (`05-phone-queue-dark.png`, `06-phone-delivery-dark.png`).
2. **Person-scope bug (`waiting_for` leak):** Scoping to `person:CaOs` includes PR `#66` (`DEL-183`), whose ball is with `BoFe (author) + FrMo (sentinel)` — because `matchesScope()` checks legacy `row.waiting_for` (`"CaOs"`, the commenter who opened the blocker) alongside `waiting_on`.
3. **Policy lab / Rules contradiction on no-Jira tenants (`del3`):** When a store has no Jira layer, `refinement_cost` falls back to `DEFAULT_REFINEMENT_HOURS = 1.0`, causing Policy lab and Rules on `del3` to headline *"the lab's best scenario is refine before start — −25 % cycle"* while `del-combined` says `+82 % cycle — not a measured win`.
4. **Delivery list inspector action buttons dead:** Clicking `Timeline`, `Blocking map`, or `Copy as Markdown` inside the desktop `#delivery-detail` inspector panel silently does nothing because the click handler reads `state.drawerEntity` (which is `null` when the drawer is closed) instead of `state.selection.row`.

---

## Ranked Findings by Area

### 1. Themes & Viewports (`1600×1000` & `412×915`, Dark & Light)

#### [F-01] BUG (Must-Fix) · Phone (`412×915`) Delivery list & Queue response-time cards clip all values off-screen
- **Views:** Delivery list (`?view=delivery`) and Queue (`?view=queue`) at `412×915` (both Dark and Light themes).
- **Screenshots:** `/tmp/opencode/qa-explore-v011/05-phone-queue-dark.png`, `/tmp/opencode/qa-explore-v011/06-phone-delivery-dark.png`.
- **Repro:**
  1. Open the app at viewport `412×915`.
  2. Navigate to **Delivery list** (`All 8` tab).
  3. Navigate to **Queue** and scroll to the **Response time** table.
- **Observed:** On phone, `.table` converts rows into stacked key-value cards (`display: block; width: 100%` in `app/style.css:1967`). However, `.delivery { min-width: 1120px; }` (`app/style.css:1158`) and `.response-panel .response-table { min-width: 900px; }` (`app/style.css:1169`) are **not** reset inside `@media (max-width: 640px)`. As a result, each card is 1120px / 900px wide inside a 412px scroll container: the left `data-label` (`STATUS`, `WAITING ON`, `PHASE`, `MEDIAN RESPONSE`, `MEAN`, `P90`, `NEVER`) is visible on the left, while every actual value is pushed to `x ≈ 880–1100px` off-screen right.
- **Expected:** In `@media (max-width: 640px)`, `.delivery` and `.response-panel .response-table` should reset `min-width: 0` (just like `.drill { min-width: 0; }` on line 2050) so stacked cards fit the 412px viewport without horizontal scrolling.

#### [F-02] PAPERCUT · Phone (`412×915`) tap targets below 44px minimum on Blocking text-view summary, Policy checkboxes, and YAH bar
- **Views:** Blocking map, Policy lab, Timeline, and top chrome at `412×915`.
- **Repro:**
  1. At `412×915`, inspect interactive element bounding boxes across views.
- **Observed:**
  - Blocking map `<summary>TEXT VIEW — 7 NODES · 28 EDGES</summary>` is `412×16px` (16px tall).
  - Policy lab checkboxes `#policy-knob-review_first` and `#policy-knob-refine_before_start` have width `13px` (`13×44px`), and their parent `<label>` text does not have `min-height: 44px` padding across the full row.
  - Top chrome `.yah-step` button is `34px` tall, `#ticket-jump-input` is `36px` tall, and `.skip-link` / Timeline carry-over `<summary>` are `35px` tall.
- **Expected:** Interactive touch controls on phone should meet the `44×44px` touch target contract (prd/28).

---

### 2. Views & Cross-View Reconciliation

#### [F-03] BUG (Must-Fix) · `del3` (no-Jira store) fabricates a `1 h` refinement cost and headlines "refine before start — −25 % cycle"
- **Views:** Rules (`?tenant=del3&view=rules`) and Policy lab (`?tenant=del3&view=policy`).
- **Repro:**
  1. Switch tenant to `del3` (which has 0 Jira stories and no `created → ready` events).
  2. Open **Rules** and **Policy lab**, or click the `refine before start` preset.
- **Observed:**
  - On `del3`, **Rules** displays the banner: `"the lab's best scenario is refine before start — −25 % cycle"`.
  - In **Policy lab**, `CALIBRATED FROM` shows `refinement: ≈1 h created → ready`, and running `refine before start` predicts `−24.8 % cycle` (`≈3 d 0 h → ≈2 d 2 h`).
  - Root cause: `server/app.py:1230` and `1673` call `jira_analysis.refinement_cost(court, jira, window)`, which returns `DEFAULT_REFINEMENT_HOURS = 1.0` whenever `spans` is empty (`analysis/jira.py:3845`), instead of `hours: None`. Because `1.0 h` is tiny relative to one rework round (`≈5.83 h`), SimPy removes a full rework round in exchange for a 1-hour timeout and declares refinement the #1 winning policy on a store that has no Jira data at all — directly contradicting `del-combined` (`+82 % cycle — not a measured win`) and `policy.js:353` (which expects `refinement.hours == null` to render `"not measurable (no Jira layer)"`).
- **Expected:** When a store has no measured `created → ready` segments (`n == 0`), `calibration.refinement.hours` should be `None` (not `1.0 h`), the `refine before start` knob/preset should be disabled or marked unavailable without a Jira layer, and `_advice_payload` must not rank `refine` as the best scenario on a PR-only store.

#### [F-04] MISLEADING NUMBER · Overview diagnosis tile switches metric, count, and threshold after visiting Queue view
- **Views:** Overview (`?view=overview`) and Queue (`?view=queue`).
- **Screenshots:** Compare `/tmp/opencode/qa-explore-v011/01-overview-dark.png` (after visiting Queue in `queues` mode) vs. initial boot dump.
- **Repro:**
  1. Fresh-load `?tenant=del-combined&view=overview` (`state.queueLevel` defaults to `'person'`). Observe the 4th stat tile and the bottom-right panel on Overview.
  2. Navigate to **Queue**, switch `level` dropdown from `people` to `queues`.
  3. Navigate back to **Overview**.
- **Observed:**
  - On initial load (`queueLevel = 'person'`), Overview shows: **`0 OVERLOADED PEOPLE`** (`0 slow · longest mean wait ≈1 d 3 h … a flag needs > 2 d 1 h vs the 1 d 1 h baseline`) and the **People diagnosis** panel (`FrMo ≈ healthy ≈1 d 3 h`, etc.).
  - After switching Queue to `queues`, Overview mutates into: **`2 OVERLOADED QUEUES`** (`2 slow · longest mean wait ≈1 d 7 h … a flag needs > 3 h 8 min vs the 2 h 30 min baseline`) and **Queue diagnosis** (`review ≈ overloaded ≈1 d 7 h`, `sentinel ≈ overloaded ≈5 h 42 min`, `author ≈ slow`, `ready ≈ slow`).
  - Similarly, if Blocking map was switched to `person`, Overview's **Where waits accumulate** panel switches from role edges (`author waited on review 167 d 5 h`) to person edges (`CaOs waited on FrMo 38 d 2 h`), and the **MEASURED WAIT** tile changes from `≈304 d 6 h` to `≈340 d 0 h` while its subtitle still claims `"queue-wait hours (role-level handover graph, each handover once)"`!
- **Expected:** Overview's headline tiles (`MEASURED WAIT` whose subtitle explicitly states *"role-level handover graph, each handover once"*) must either always query the canonical level or update their subtitle/basis when `state.blockingLevel` / `state.queueLevel` is changed on another page. Claiming `"role-level handover graph, each handover once"` under `≈340 d 0 h` (which is the person-level concurrent-holder sum) is factually false.

#### [F-05] MISLEADING NUMBER · Rules `people (pseudonyms)` tab keeps team-level headline stats (`10 rules · 4 critical · ≈2.6k h at stake`) over an empty state
- **View:** Rules (`?view=rules`).
- **Repro:**
  1. Open **Rules** on `del-combined` (or `del3`) across any timeframe (`1w`, `4w`, `1q`, `All`).
  2. Click the **`people (pseudonyms)`** segment button in `#rules-level`.
- **Observed:**
  - The body filters to person-scoped rules and shows an empty state (`"No person-level rule crosses its threshold in this window"`).
  - However, `#rules-stats` in the toolbar still displays **`10 rules · 4 critical · ≈2.6k h at stake (counted once)`** because `rulesStats(data)` (`app/rules.js:500`) computes counts over `data.suggestions` without filtering by `data.level === 'person'`.
  - Furthermore, the empty state renders **two duplicate paragraphs** back-to-back:
    1. `"No person-level rule crosses its threshold in this window; the team tab names the team-level rules. show the 10 team rules →"`
    2. `"No person-level rule crosses its threshold in this window."` (`app/rules.js:576-580`).
- **Expected:** When `people (pseudonyms)` is selected, `#rules-stats` should reflect the filtered person-level rule count (`0 rules · 0 critical · ≈0 min at stake`, or state `0 person rules (10 team)`), and the empty state should not repeat `"No person-level rule crosses its threshold in this window"` twice.

#### [F-06] MISLEADING NUMBER · Rules drill-down sorting and `affected` counts break on 4 rules (`round-cap`, `cycle-time-censoring`, `jira-worklog-mismatch`, `flow-efficiency`)
- **View:** Rules (`?view=rules`), drill-down `<details>` tables.
- **Repro:**
  1. Open **Rules** (`1q`, `del-combined`) and expand the drill-down `<details>` on each rule card.
- **Observed:**
  - **Rule 04 (`round-cap`, "cap them at 2"):** The evidence facts say `PRs over target: 24` and `round cap: 2`, but the drill-down summary says **`124 items · worst wait first`** with **0 affected rows** (`tr.affected` count is 0, even for PRs with 3 rounds!). Root cause: `analysis/rules.py:1304` sets `affected=count > MAX_REVIEW_ROUNDS` (`count > 3`) instead of `count > REVIEW_ROUND_CAP_TARGET` (`count > 2`).
  - **Rule 05 (`cycle-time-censoring`, "3 open PRs already waited past it"):** The drill-down summary says `124 items · 3 affected · worst wait first`, but the first 6 rows in the table are **unaffected declined PRs** (`delivery-web#8 · declined — censored · 56 d 0 h`, `#18 · 43 d 2 h`, …), while the 3 affected open PRs (`#53`, `#55`, `#60`) are buried at rows 7–9. Root cause: `app/rules.js:311` re-sorts rows client-side by `impactOf(row, metric)` without sorting `row.affected` first, overriding Python's `_drill_order`.
  - **Rule 07 (`jira-worklog-mismatch`):** Summary says `85 items · 4 affected · worst wait first`, but client-side `impactOf` sorts by `logged_hours` descending without putting `affected` rows first, burying the 4 affected stories (`DEL-167`, `DEL-147`, `DEL-187`, …) at rows 18, 34, 47, etc.
  - **Rule 09 (`flow-efficiency`):** Summary says `124 items · 64 affected · worst wait first`, but 40 affected PRs appear *after* unaffected PRs because `impactOf` sorts by `waiting_hours` while `affected` is `efficiency < 0.25`.
  - **Rule 01 (`sprint-on-time`):** The 3 `never_done` ("still open") stories in S2 (`DEL-138`, `DEL-142`, `DEL-143`) have `affected: False` (`analysis/jira.py:1979`), so only the 12 `late` stories are highlighted as `12 affected` out of `15 items`, even though all 15 missed S2's close.
- **Expected:** `ruleItems()` in `app/rules.js:311` should sort `Number(Boolean(b.row.affected)) - Number(Boolean(a.row.affected))` before `impactOf` (or preserve `_drill_order`), `round-cap` should mark `count > REVIEW_ROUND_CAP_TARGET` as `affected`, and `sprint-on-time` should mark `never_done` stories as `affected: True`.

---

### 3. Navigation, Deep Links & Scope Controls

#### [F-07] BUG (Must-Fix) · Scoping to `person:CaOs` includes PR `#66` (`DEL-183`), which is waiting on `BoFe + FrMo`, not `CaOs`
- **Views:** Overview (`?view=overview&scope=person:CaOs`) and Delivery list (`?view=delivery&scope=person:CaOs`).
- **Repro:**
  1. In the top bar `scope` dropdown, select **`person CaOs`**.
  2. Inspect **Overview** (`Needs attention` and `BLOCKED` tile) or **Delivery list** (`Blocked 1` / `All 2`).
- **Observed:**
  - The scope banner states: `"Scope person CaOs (items waiting on CaOs)"`.
  - Yet both Overview and Delivery list include **`DEL-183 #66 delivery-api`**, whose `WAITING ON` column is **`BoFe (author) + FrMo (sentinel)`** (CaOs is not a holder!). That also causes `person:CaOs` to show `1 BLOCKED` on Overview.
  - Root cause: On `#66`, CaOs left a blocker comment at `2026-11-13 13:48`, which handed the ball back to the author (`BoFe`) while `FrMo` (sentinel) is also unapproved. In `analysis/ballcourt.py:2258`, the measured interval before `13:48` had `current.owner = "CaOs"`, while the unobserved tail has `tail.owner = None` (2 holders: `BoFe` and `FrMo`). Line 2258 evaluates `owner = (tail.owner if tail else None) or (current.owner if current else None)`, leaking the *previous* interval's owner (`"CaOs"`) into `row["waiting_for"]`. Then `matchesScope()` in `app/tables.js:240` builds the holder set from `[...(row.waiting_on || []), row.waiting_for, row.ball_label]`, matching `CaOs`!
- **Expected:** `ballcourt.py:2258` must read `owner = latest.owner if latest is not None else None` (never falling back to an ended interval's owner when the current interval has multiple holders), and `matchesScope()` should check `row.waiting_on` (falling back to `waiting_for` only when `waiting_on` is empty).

#### [F-08] BUG · `Done in Jira, PR open` panel ignores `role` scopes (`role:sentinel`, `role:author`, `role:review`), falsely claiming "none" in scope
- **Views:** Overview and Delivery list under `scope=role:sentinel` or `scope=role:author`.
- **Repro:**
  1. Select **`queue sentinel`** (`role:sentinel`) or **`queue author`** (`role:author`) in the `scope` dropdown.
  2. Scroll to the **Done in Jira, PR open** panel on Overview or Delivery list.
- **Observed:**
  - Under `role:sentinel`, the top **What to do now** card lists `DEL-168 #55` waiting on `FrMo (sentinel)` (which is Done in Jira, PR open), and all 3 Done-open PRs (`#66`, `#53`, `#55`) have `FrMo (sentinel)` in `waiting_on`.
  - Yet the **Done in Jira, PR open** panel says: **`none — No ticket in this scope is Done in Jira with its PR open — 3 team-wide.`**
  - Root cause: `doneOpenMarkup()` in `app/tables.js:487-489` handles `repo` and `ticket` scopes, and in the `else` branch checks `(row.waiting_on || []).some((person) => String(person).toLowerCase() === value)`. When `scope.kind === 'role'`, `value` is `"sentinel"` or `"author"`, which never matches a pseudonym in `row.waiting_on` (`["BoFe", "FrMo"]`).
- **Expected:** `doneOpenMarkup()` should use `matchesScope(row, scope)` (or match against `row.waiting_roles`) so `role:sentinel`, `role:author`, and `role:review` filter Done-open rows consistently with the rest of Overview and Delivery list.

#### [F-09] PAPERCUT · Route fallback notice (`#route-notice`) is overwritten by unknown-scope notice on boot and persists across scope/timeframe changes
- **View:** Top banner `#route-notice` across all views.
- **Repro:**
  1. Navigate to `http://127.0.0.1:8798/?tenant=nonexistent&view=bogus&tf=99y&scope=person:Nobody`.
  2. Observe `#route-notice`.
  3. Change `scope` or `timeframe` in the top bar while staying on the same view.
- **Observed:**
  - At step 1, boot (`app/main.js:4036`) sets the notice for `view “bogus” and timeframe “99y”`. Then `loadTenants()` (`line 947`) calls `setNotice('No dataset “nonexistent”…', { append: false })` (default `append` is `false`!), **wiping out** the `view “bogus” and timeframe “99y”` notice.
  - Furthermore, because `view=bogus` was ignored, `restoreUI()` kept `state.page = 'delivery'` from `localStorage` rather than resetting to `'overview'`, contradicting the notice text `"showing the default view, timeframe and scope"`.
  - At step 3, changing `scope` or `timeframe` does not clear `#route-notice` (only `showPage()` to a *different* view clears it).
- **Expected:** `loadTenants()` should pass `{ append: true }` to `setNotice()`, an invalid `?view=` in the URL should reset `state.page` to the default entry view rather than inheriting a stale `localStorage` view, and user edits to `scope` / `timeframe` / `tenant` should clear stale route notices.

#### [F-10] PAPERCUT · Leaving Timeline with an active Sprint lens leaves all timeframe buttons (`1w`, `4w`, `1q`, `All`) unpressed on other views
- **Views:** Timeline → any other view (Overview, Delivery list, Rules, etc.).
- **Repro:**
  1. Go to **Timeline** and click **S1** in the sprint strip (`state.timeframeCustom` becomes `true`, and the `custom` chip appears on Timeline).
  2. Click **Overview** (or **Delivery list** / **Rules**) in the sidebar.
  3. Inspect the top bar `timeframe` control.
- **Observed:** On Overview, the `custom` chip is hidden (`chip.hidden = !custom || state.page !== 'timeline'`, `app/main.js:345`), `#range` reverts to `20 Aug → 18 Nov` (the `1q` preset window that Overview actually queries), **but all four preset buttons (`1w`, `4w`, `1q`, `All`) have `aria-pressed="false"` and no `.on` highlight** (`const active = !custom && button.dataset.tf === state.timeframe`, `line 337`).
- **Expected:** When `state.page !== 'timeline'`, the timeframe segmented control should highlight `state.timeframe` (`1q`) since non-timeline views query the preset timeframe, not the timeline's custom zoom window.

---

### 4. Interactions, Drawers & Keyboard

#### [F-11] BUG (Must-Fix) · Desktop Delivery list inspector buttons (`Timeline`, `Blocking map`, `Copy as Markdown`) silently do nothing
- **View:** Delivery list (`?view=delivery`) at desktop viewport (`1600×1000`).
- **Repro:**
  1. Open **Delivery list** on desktop (`1600×1000`).
  2. Click any row in `#delivery-table` (e.g., `DEL-183 #66`). The inline right-hand inspector `#delivery-detail` populates with facts and three action buttons at the bottom: `Timeline`, `Blocking map`, `Copy as Markdown`. (On desktop, clicking a delivery row intentionally does not open `#drawer`; `app/main.js:3796`).
  3. Click **`Copy as Markdown`** or **`Timeline`** in `#delivery-detail`.
- **Observed:**
  - Clicking **`Copy as Markdown`** copies an **empty string `""`** (while flashing `"copied as Markdown"`!).
  - Clicking **`Timeline`** switches to Timeline **without scoping to the ticket or focusing the PR** (`scope` stays `all:all`).
  - Root cause: Both `#delivery-detail` and `#drawer` render `detailMarkup(entity)` with `data-detail="timeline"`, `data-detail="blocking"`, and `data-detail="copy"`. In `app/main.js:3817-3837`, the `[data-detail]` click handler always reads `state.drawerEntity` (`copyMarkdown(state.drawerEntity)` and `const entity = state.drawerEntity`). When clicking from `#delivery-detail`, `#drawer` is closed so `state.drawerEntity` is `null`; the selected entity lives in `state.selection`.
- **Expected:** The `[data-detail]` handler should resolve `const entity = pivot.closest('#drawer') ? state.drawerEntity : (state.selection || state.drawerEntity);` so all three buttons in `#delivery-detail` act on the selected row.

#### [F-12] BUG · PR drawer repeats `waiting on author`, `CI`, `rebasing`, and `no events since` twice in the same `<dl class="facts">` list
- **Views:** PR drawer (`#drawer`) opened from Overview, Timeline, or Ticket detail.
- **Repro:**
  1. Open **Overview** and click any row in `Needs attention` (e.g., `DEL-183 #66`), or open **Ticket detail** (`#ticket/DEL-183`) and click PR `#65` or `#66`.
  2. Inspect the `<dl class="facts">` list inside `#drawer`.
- **Observed:** Four `<dt>` keys appear **twice** in the same facts list:
  - First as boolean/status facts (`app/main.js:2189-2194` and `2286-2291`):
    - `waiting on author`: `yes · 2 d 1 h` (or `no`)
    - `CI`: `not running`
    - `rebasing`: `no`
    - `no events since`: `2026-11-13 13:48 UTC` (or `events observed through the last event`)
  - Immediately followed by the 6 phase-duration rows (`app/main.js:2196-2205` and `2292-2299`):
    - `waiting on author`: `—`
    - `in progress`: `—`
    - `in review`: `2 h 5 min`
    - `CI`: `—`
    - `rebasing`: `—`
    - `unobserved (no events since the snapshot)`: `2 d 1 h`
  - Worse, on `DEL-183 #66`, the first `waiting on author` says **`yes · 2 d 1 h`**, while two lines below the second `waiting on author` says **`—`** (because the 2 d 1 h is in the `unobserved` tail phase, not a measured `waiting` phase!).
- **Expected:** Separate or rename the live-status facts vs. the measured phase-duration breakdown (e.g., group phase durations under a `PHASES` subheading or prefix/label them distinctly, and don't print `waiting on author: yes · 2 d 1 h` right above `waiting on author: —`).

#### [F-13] PAPERCUT · Clicking the selected Sprint row in Timeline's sprint strip does not toggle it off if clicked on a child `<span>`
- **View:** Timeline (`?view=timeline`), `#sprint-strip`.
- **Repro:**
  1. On **Timeline**, click the `S1` sprint row (`[data-sprint-id="1"]`) to activate the S1 lens.
  2. Click the `S1` badge (`[data-sprint="1"]`) or row again to toggle it off.
- **Observed:** When S1 is active, `renderSprintStrip()` re-renders the strip. If a test or user clicks `[data-sprint="1"]` or a child element when `state.sprint` is already `'1'`, `scopeToSprint` / `sprintRow` toggle behavior can be inconsistent depending on whether the click hits `[data-sprint-start]` vs. `[data-sprint-clear]`. (Clicking the `clear` chip `[data-sprint-clear]` works, but leaving the sprint lens via `clear` leaves `state.timeframeCustom = true` and the window stuck on `17 Aug 09:00 → 10 Sep 16:00` instead of restoring the active `1q` preset window!).
- **Expected:** Clicking `clear` on the sprint lens (`clearSprintFocus()`) should restore the active timeframe preset (`applyTimeframeToTimeline()`) so the timeline actually returns to the `1q` window instead of staying trapped in S1's dates under a `custom` chip.

---

### 5. Edge Cases & Content Honesty

#### [F-14] MISLEADING NUMBER · Ticket detail (`DEL-183`) shows `"10/10 segments"` and `"12 d 4 h cycle"` without flagging that PR `#66` opened 6 d 4 h *after* the ticket closed
- **View:** Ticket detail (`?tenant=del-combined&view=ticket#ticket/DEL-183`).
- **Repro:**
  1. Open **Overview** and look at `Done in Jira, PR open`: `DEL-183 #66` is highlighted with the warning pill **`PR opened after the ticket closed (6 d 4 h later)`**.
  2. Click **`DEL-183`** to open its Ticket detail page.
- **Observed:**
  - On `#ticket/DEL-183`, the narrative says: `"Done in Jira, but PR #66 is still open (right-censored); the cycle is measured to the ticket's done, not to that PR. 50 % of the story's change (96 of 192 changed lines) is in an unmerged PR, so this is not a shipped change."`
  - However, **nowhere on the Ticket detail page** does it mention that PR `#66` was opened **after** the ticket closed (`2026-11-13 11:42` vs. `DONE 2026-11-04 15:46` — 6 d 4 h later), even though Overview and Delivery list call that out as *"a stronger finding: the work cannot have been part of the delivered story"*.
- **Expected:** Ticket detail's narrative / PR table row for `#66` should include the `opened after the ticket closed (6 d 4 h later)` badge/sentence so the ticket page is at least as informative as the summary row that links to it.

#### [F-15] MISLEADING NUMBER · Ticket detail (`DEL-161`) header says `"32 d 4 h cycle"` while prose says `"78 % of the cycle is unmeasured… the 7 of 10 segments it can measure cover 7 d 0 h"`
- **View:** Ticket detail (`?tenant=del-combined&view=ticket#ticket/DEL-161`).
- **Repro:**
  1. Navigate to `#ticket/DEL-161` (featured in `Sprint 3 at risk` as `"approved, never merged · 23 d 3 h · closed by the bot BiBi"` and in Rules `Story rework` under `Approved but never merged`).
- **Observed:**
  - The top stats strip says **`32 d 4 h cycle · 7/10 segments · 1 PR · 2 points · most in created → ready (30%)`**.
  - In the segment legend, `created → ready` is **`2 d 0 h · 30%`** (which is 30% of the *7 d 0 h measured sum*, not 30% of the *32 d 4 h cycle*!).
  - Meanwhile, the Pull Requests table shows `#34 delivery-web · declined · Cycle: 3 d 7 h`, with **no mention** in the ticket narrative or PR table that `#34` was **approved by DaKo + FrMo and closed unmerged by the bot BiBi (23 d 3 h lost)** — the very reason `DEL-161` is flagged on Timeline and Rules!
- **Expected:**
  1. When a ticket's PR was approved by all reviewers and declined by a bot (`approved_unmerged`), the Ticket detail narrative and PR row should state that finding.
  2. Clarify `32 d 4 h age (open · 7 d 0 h measured across 7/10 segments)` in the stats strip so `2 d 0 h (30%)` does not appear to claim that `2 d 0 h` is 30% of `32 d 4 h`.

#### [F-16] MISLEADING NUMBER · Queue response-time table `NEVER` column appends oldest age `(21 d 4 h)` right after `+5`, reading as if the 5 other-holder PRs are 21 d 4 h old
- **View:** Queue (`?view=queue`), **Response time** table, `FrMo sentinel` row.
- **Repro:**
  1. Open **Queue** on `del-combined` (`1q`).
  2. Look at the `NEVER` column for `FrMo sentinel`.
- **Observed:** The cell renders: **`8 (3 only) +5 (21 d 4 h)`**.
  - Because `(21 d 4 h)` is placed immediately after `+5` (in the same muted style), it visually reads as *"+5 waiting 21 d 4 h"*, whereas `21 d 4 h` is actually `never_oldest_hours` across all 8 open requests (specifically `#53`, which is one of the `+5` where CaOs also owes, while the oldest of the `3 only` is `#55` at `19 d 0 h`).
- **Expected:** Render `8 · 3 only · +5 with peer/author (oldest 21 d 4 h)` or separate the oldest-age label so it clearly modifies the total `8`, not `+5`.

#### [F-17] MISLEADING NUMBER · Trend `Sprint on-time delivery` chart draws a rising line (`50% → 60%`) while the headline says `"no bucket meets the n≥2 floor"` and Rules says `"0/2 sprints on time"`
- **View:** Trend (`?view=trend&metric=on_time&bucket=sprint` or `bucket=week`).
- **Repro:**
  1. Open **Trend**, click **`Sprint on-time delivery`**, and toggle between **`week`** and **`sprint`**.
- **Observed:**
  - The stats strip says: **`– no bucket meets the n≥2 floor (the latest measured bucket, S2, has n=1) · 2/3 buckets measured · S3 in progress`**.
  - Because each sprint bucket (`S1`, `S2`) contains by definition **at most 1 sprint (`n=1`)**, `metric=on_time` with `bucket=sprint` can **never** meet an `n ≥ 2` per-bucket floor! Every single sprint bucket is permanently flagged `1 low n`, making the headline permanently `– no bucket meets the n≥2 floor` on `bucket=sprint`.
  - Meanwhile, the terminology clashes with Timeline/Rules: Timeline says `0/2 sprints on time` (meaning 0 of 2 sprints achieved the ≥80% target share), whereas Trend's tab titled `Sprint on-time delivery` plots each sprint's *story delivery share* (`60.0%` for S1, `50.0%` for S2).
- **Expected:** On `metric=on_time` + `bucket=sprint`, a bucket *is* one sprint (of 30 stories), so `min_n` should either check the sprint's committed story count (`stories >= 5`) or use `min_n = 1` sprint per sprint-bucket so the headline can actually report `50.0% latest (S2 · 15/30 stories) · −10.0 pts since S1`.

#### [F-18] PAPERCUT · Rules card 04 (`round-cap`) and card 10 (`sentinel-tail`) render raw unformatted floats (`per extra round: 6.01`, `hours: 283.6`) in evidence chips
- **View:** Rules (`?view=rules`).
- **Repro:**
  1. Open **Rules** on `del-combined` (`1q`).
  2. Inspect the `<dl class="rule-evidence">` facts on Rule 04 (`REVIEW ROUNDS`) and Rule 10 (`SENTINEL TAIL`).
- **Observed:**
  - Rule 04 displays **`per extra round: 6.01`** (raw float hours, whereas the prose right above it says `≈6 h 1 min` and Rule 03 says `hours per round: ≈6 h 1 min`).
  - Rule 10 displays **`hours: 283.6`** (raw float hours, right next to `excess: ≈29 d 3 h`).
  - Root cause: `evidenceValue(key, value)` in `app/rules.js:144` formats numbers as hours only when `key.endsWith('_hours') || key === 'hours_per_round' || key === 'hours_per_refinement'`. It misses `key === 'hours_per_extra_round'` (Rule 04) and `key === 'hours'` (Rule 10).
- **Expected:** Add `key === 'hours' || key === 'hours_per_extra_round'` to the `fmtImpact(value)` branch in `evidenceValue()` (`app/rules.js:144`) so they render `≈6 h 1 min` and `≈35 d 4 h` in the single duration language.

#### [F-19] PAPERCUT · Rules Markdown export outputs `"impact: ≈0 min (confidence 1) — counted in #7"` on Rule #7 (`jira-worklog-mismatch`, self-reference)
- **View:** Rules (`?view=rules`) → `Copy as Markdown`.
- **Repro:**
  1. On **Rules** (`del-combined`, `1q`), click **`Copy as Markdown`**.
  2. Inspect section `## 7. Logged hours don't track story points (r = -0.072)`.
- **Observed:**
  - The exported markdown says: `- impact: ≈0 min (confidence 1) — counted in #7`.
  - Rule #7 saying `"counted in #7"` is a self-referential bug: because `jira-worklog-mismatch` has `impact_hours: 0`, `attribute_at_stake()` (`analysis/rules.py:3584`) sees `attributed_hours <= 0` and sets `attributed_to = owner["id"]` (which is `jira-worklog-mismatch` itself, rank 7!). On screen, `stakeNote()` (`app/rules.js:415`) hides the note when `stake.pool_rules < 2`, but `rulesMarkdown()` (`app/rules.js:668`) checks only `suggestion.at_stake && suggestion.at_stake.attributed_to` without checking `pool_rules >= 2` or `attributed_to !== suggestion.id`.
- **Expected:** In `analysis/rules.py:3584`, only set `attributed_to` when `owner["id"] != suggestion["id"]` (and `hours > 0`), and guard `rulesMarkdown()` with `stake.pool_rules >= 2`.

---

### 6. Polish & Build Hygiene

#### [F-20] POLISH · Baked `VERSION` file at tag `v0.11.1` still says `v0.9.0`
- **View:** Sources (`?view=sources`), sidebar footer `#app-version`, and `GET /api/health` when run from an exported archive (`git archive`).
- **Repro:**
  1. Inspect `VERSION` and `server/version.py:36` at tag `v0.11.1`.
- **Observed:** Both `VERSION` and `__version__` in `server/version.py` still contain `v0.9.0` / `"0.9.0"`. In a live git checkout `.git` wins and reports `v0.11.1 · 4cbca9a8`, but in an airgapped tarball / `git archive` deployment (which is the exact purpose of the `VERSION` file per `server/version.py:15`), the app and `/api/health` report **`v0.9.0`**.
- **Expected:** Update `VERSION` and `__version__` when cutting release tags so tarball deployments report the true release version.

#### [F-21] POLISH · Ticket detail boundary chips render raw snake_case keys (`PR_OPEN`, `REVIEWER_ADDED`, `IN_REVIEW`, `FIRST_REVIEW`, `IN_VERIFICATION`)
- **View:** Ticket detail (`#ticket/DEL-155`), `.ticket-boundaries`.
- **Screenshot:** `/tmp/opencode/qa-explore-v011/04-ticket-light.png`.
- **Repro:**
  1. Open any ticket detail page (e.g., `#ticket/DEL-155`).
  2. Look at the boundary timestamp chips below the 10-segment legend.
- **Observed:** While the segment legend and rule evidence were cleaned of `snake_case` (prd/63 V-B), `boundaryChips()` in `app/ticket.js:198` prints `<span class="k">${esc(name)}</span>` with `text-transform: uppercase`, resulting in **`PR_OPEN`**, **`REVIEWER_ADDED`**, **`IN_REVIEW`**, **`FIRST_REVIEW`**, **`IN_VERIFICATION`** with literal underscores, and tooltips reading `"the pr_open boundary"`.
- **Expected:** Replace underscores with spaces (`name.replace(/_/g, ' ')`) in `boundaryChips()` so the chips and tooltips read `PR OPEN`, `REVIEWER ADDED`, `IN REVIEW`, `FIRST REVIEW`, `IN VERIFICATION`.

#### [F-22] POLISH · Ticket detail `worklogged` event attributes say `log delay: within week · logged late by: 9 d 4 h` (business-day vs. calendar-week mismatch)
- **View:** Ticket detail (`#ticket/DEL-155`), `TICKET EVENTS · 10`.
- **Screenshot:** `/tmp/opencode/qa-explore-v011/04-ticket-light.png`.
- **Repro:**
  1. Open `#ticket/DEL-155` and scroll to `TICKET EVENTS · 10`.
  2. Look at the `2026-10-12 15:45 worklogged` row.
- **Observed:** The attributes read: **`by a human · log delay within week · logged late by 9 d 4 h · time spent 3 h`**. Because `1 d = 8 h` in business hours, `9 d 4 h` (`76 h`, or ~3.2 calendar days formatted on an 8h/day scale) is greater than `1 working week (5 d = 40 h)`, making `"log delay: within week"` right next to `"logged late by: 9 d 4 h"` look contradictory (`9 d > 7 d`).
- **Expected:** Either price `logged_late_by` on the same clock as `log_delay` (calendar days/hours, or business hours) or clarify the attribute label so `within week` and `9 d 4 h` do not contradict each other.

---

## Summary Table

| ID | Severity | Area | View(s) | One-line summary |
| --- | --- | --- | --- | --- |
| **F-01** | **BUG (Must-Fix)** | Themes & Viewports | Delivery, Queue (`412×915`) | `.delivery` (`min-width: 1120px`) & `.response-table` (`min-width: 900px`) not reset on phone; card values clipped off-screen. |
| **F-02** | **PAPERCUT** | Themes & Viewports | Blocking, Policy, Topbar (`412×915`) | Phone touch targets under 44px on Blocking text-view `<summary>` (16px), Policy checkboxes (13px wide), YAH (34px). |
| **F-03** | **BUG (Must-Fix)** | Views / Honesty | Policy lab, Rules (`del3`) | No-Jira store (`del3`) falls back to `DEFAULT_REFINEMENT_HOURS = 1.0`, making Policy lab & Rules headline `refine before start — −25 % cycle`. |
| **F-04** | **MISLEADING NUMBER** | Views / Honesty | Overview ↔ Queue / Blocking | Switching level in Queue/Blocking mutates Overview's headline tiles (`0 OVERLOADED PEOPLE` → `2 OVERLOADED QUEUES`; `304 d 6 h` → `340 d 0 h` with wrong subtitle). |
| **F-05** | **MISLEADING NUMBER** | Views | Rules (`people` tab) | Rules `people (pseudonyms)` tab keeps team stats (`10 rules · 4 critical · ≈2.6k h`) over empty state and duplicates the empty message twice. |
| **F-06** | **MISLEADING NUMBER** | Views / Honesty | Rules (drill-downs) | Client-side `ruleItems()` sort drops `affected`-first ordering (`cycle-time-censoring`, `worklog`, `flow-efficiency`); `round-cap` uses `> 3` instead of `> 2` (0 affected). |
| **F-07** | **BUG (Must-Fix)** | Navigation / Scope | Overview, Delivery (`person:CaOs`) | `person:CaOs` scope includes PR `#66` (waiting on `BoFe + FrMo`) because `ballcourt.py` leaks previous interval's `owner` into `waiting_for`. |
| **F-08** | **BUG** | Navigation / Scope | Overview, Delivery (`role:*`) | `Done in Jira, PR open` panel ignores `role:sentinel` / `role:author` scopes, falsely showing `none` when 3 rows match. |
| **F-09** | **PAPERCUT** | Navigation | Route notice (`#route-notice`) | `loadTenants()` overwrites invalid-view/tf route notice (`append: false`), inherits stale `localStorage` page, and notice lingers on scope/tf change. |
| **F-10** | **PAPERCUT** | Navigation | Timeline → other views | Leaving Timeline with Sprint lens active leaves all 4 timeframe buttons (`1w`, `4w`, `1q`, `All`) unpressed on other views. |
| **F-11** | **BUG (Must-Fix)** | Interactions | Delivery list inspector | Desktop `#delivery-detail` buttons (`Timeline`, `Blocking map`, `Copy as Markdown`) read `state.drawerEntity` (`null`) instead of `state.selection`. |
| **F-12** | **BUG** | Interactions | PR Drawer (`#drawer`) | PR drawer duplicates `waiting on author`, `CI`, `rebasing`, and `no events since` twice in `<dl class="facts">`, with contradictory values (`yes · 2 d 1 h` vs `—`). |
| **F-13** | **PAPERCUT** | Interactions | Timeline Sprint lens | Clearing Sprint lens via `clear` leaves Timeline stuck on the sprint's date window under `custom` instead of restoring the active timeframe preset. |
| **F-14** | **MISLEADING NUMBER** | Content Honesty | Ticket detail (`DEL-183`) | `#ticket/DEL-183` omits that PR `#66` was opened 6 d 4 h *after* the ticket closed (featured on Overview/Delivery). |
| **F-15** | **MISLEADING NUMBER** | Content Honesty | Ticket detail (`DEL-161`) | `#ticket/DEL-161` headlines `32 d 4 h cycle` next to `2 d 0 h (30%)` (30% of 7 d measured) and omits that `#34` was approved by peers+sentinel and bot-declined. |
| **F-16** | **MISLEADING NUMBER** | Content Honesty | Queue (Response time) | `FrMo` `NEVER` cell renders `8 (3 only) +5 (21 d 4 h)`, making `(21 d 4 h)` read as the age of `+5` rather than the oldest of all `8`. |
| **F-17** | **MISLEADING NUMBER** | Content Honesty | Trend (`on_time`) | Trend `Sprint on-time delivery` on `bucket=sprint` has `n=1` per sprint and permanently fails the `n≥2` headline floor (`– no bucket meets the n≥2 floor`). |
| **F-18** | **PAPERCUT** | Content Honesty | Rules (Cards 04 & 10) | `evidenceValue()` misses `hours_per_extra_round` and `hours`, printing raw floats `per extra round: 6.01` and `hours: 283.6`. |
| **F-19** | **PAPERCUT** | Interactions / Export | Rules (`Copy as Markdown`) | Rule #7 (`jira-worklog-mismatch`, 0 h) exports `- impact: ≈0 min (confidence 1) — counted in #7` (self-attribution). |
| **F-20** | **POLISH** | Build Hygiene | Sources / `/api/health` | `VERSION` file and `server/version.py:__version__` still say `v0.9.0` at tag `v0.11.1`. |
| **F-21** | **POLISH** | Views | Ticket detail | Boundary chips render raw uppercase snake_case (`PR_OPEN`, `REVIEWER_ADDED`, `IN_VERIFICATION`). |
| **F-22** | **POLISH** | Content Honesty | Ticket detail (`DEL-155`) | Worklog event attributes print `log delay: within week` next to `logged late by: 9 d 4 h` (76 wall-clock hours formatted as 8h business days). |

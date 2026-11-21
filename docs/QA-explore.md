# megaspeed v0.10.0 — free-exploring bug / papercut hunt

Adversarial, no-script exploration of the whole product at tag `v0.10.0` (build `ea244541`).
Own server on `:8790` (copies of `del-combined` + `del3`), harness browser at 1600×1000, plus
Playwright at 412×915 (touch/mobile) for the phone pass, dark and light themes.
The live server on 8766 was not touched; source was not modified.

Screenshots: `/tmp/opencode/qa-explore/shots/*.png` (referenced as `[shot: name]`).
Raw scratch (page text dumps, lint JSON): `/tmp/opencode/qa-explore/`.

Severity: **BUG** (broken / dead end) > **NUMBER** (misleading or contradictory number/label) >
**PAPERCUT** > **POLISH**.

## Verdict

**Not shippable as-is.** Two dead-end bugs (F1, F2) leave the user on a spinner with no way
to recover except navigating away. F1 is also persisted by the scope preference, so it
survives reload. Fix those, plus the rest of the "must-fix" group (F3–F9), and the rest can
ship as known papercuts.

| bucket | findings |
| --- | --- |
| **Must fix before ship** | F1, F2, F3, F4, F5, F6, F7, F8, F9 |
| Should fix soon | F10–F22 |
| Papercut / polish backlog | F23–F30 |

What is solid: no JS errors or `undefined/NaN/null` leaks on any view in any scope for either
tenant (9 views × 2 tenants × 8 scopes scanned); numbers reconcile across Overview ↔ Blocking ↔
Queue at role level (304 d 6 h, 167 d 5 h, 52 d 1 h, 46 d 7 h; FrMo ≈1 d 3 h; sprint 25+3+2=30);
deep links, Back/Forward, YAH breadcrumb and the tenant switcher work; the phone layout has no
horizontal overflow except Timeline and Ticket (F25); the phone sheet/drawer, nav and Esc behave.

---

## 1. Views

### F1 · BUG (high) · Trend + repo scope → "Loading the trend…" forever
- **Repro:** open `/?tenant=del-combined&view=trend&repo=DEL/delivery-web`, or from Overview
  choose scope `repo delivery-web`, then click **Trend**. The scope is persisted, so a plain
  reload stays stuck until the scope is cleared on another view.
- **Observed:** page body ends at "Loading the trend…"; never resolves. `GET /api/trend?...&repo=DEL%2Fdelivery-web`
  → **400** `unknown query parameter(s): repo` (valid: bucket, days, metric, tenant). The UI swallows the error,
  and `__megaspeedReady` never fires (page-level `PAGEERROR: /api/trend … → 400`; the harness loses its websocket
  and its health-reload loop spins). The scope banner on the same page says Trend is "not filtered — it still shows
  the whole team", yet the client sends `repo=`.
- **Expected:** either honour `repo` on `/api/trend` or don't send it (the banner already promises whole-team),
  and always render an error state on a failed fetch.
- `[shot: trend-repo-stuck]`

### F2 · BUG (high) · Unknown / malformed ticket → "Loading the ticket…" forever
- **Repro:** `…#ticket/NOPE-1` (or `DEL-9999`, `DEL-0167`, `foo%20bar`); or type `DEL-9999` / `del-0167` into **go to ticket…**
  (the box accepts anything shaped like a key and navigates).
- **Observed:** header shows the bogus key, body "Loading the ticket…" indefinitely, no error, app never ready.
  Server answers correctly: `404 {"error":"unknown ticket NOPE-1","hint":"a ticket is a STORY object or a PR.ticket_keys entry…"}`
  / `400 ticket key 'foo bar' is not shaped like PROJ-123`. The UI ignores both.
- **Expected:** a "no such ticket in tenant X" empty state showing the server hint, with a way back.

### F3 · BUG · Ticket page: "Worklog vs active" bars collapse to 2 px
- **Repro:** open any ticket with worklogs (`#ticket/DEL-183`), look at the right column.
- **Observed:** the `logged` / `active (PR)` tracks render as two thin ticks (`.worklog-track` 2×8 px) with a ~78 px empty band under them.
  Root cause: **class collision** — `.worklog-bars` is defined twice in `app/style.css` (line ~1618 `flex-direction:column` for the
  ticket panel; line ~2166 `flex-direction:row; height:78px; align-items:flex-end` for the Rules worklog chart). The later rule wins and breaks the
  grid rows (`.worklog-row`). Same in light theme.
- `[shot: ticket-183]`, `[shot: desk-light-ticket]`

### F8 · NUMBER · Trend headline treats the empty in-progress bucket as "latest"
- **Repro:** Trend → **Merged PRs** (week): `0 latest · −2 since the first bucket`; **Wait hours**: `0 min latest · −6 d 0 h since the first bucket`.
  Flow efficiency / Cycle time use the last *complete* bucket (32.1 %, 2 d 1 h) — the headline rule is inconsistent per metric.
- **Observed:** the in-progress 16 Nov bucket (N=0, one business day) is plotted as a dive to 0 and used as "latest". The Merged-PRs y-axis even
  shows **−2** as the lowest tick for a count. The chart label "in progress" is clipped to "in progre" at the right edge and the plot only fills ~65 % of the width.
- **Also:** "Sprint on-time delivery" headline (`50.0 % latest · −10.0 pts since the first bucket`) is 1 sprint vs 1 sprint (N=1 each),
  and the flow-efficiency "−15.8 pts since first bucket" compares a 3-PR bucket with a 9-PR bucket. N is in the table but never qualifies the headline.
- **Expected:** headline = last complete bucket with a minimum N; clamp count axes at 0; show N on the headline.
- `[shot: trend-week-Merg]`, `[shot: trend-week-Spri]`

### F9 · BUG · Delivery list: `stale` / `unobserved` pills are clipped, and "Done in Jira, PR open →" lands on a page that doesn't show it
- **Repro:** Delivery list → any row. The STATUS cell (137 px, no wrap) shows `blocked …` / `in review …`; the `stale` and `unobserved`
  pills (and their tooltips) exist in the DOM but are clipped. (Overview "5 orphaned approvals … stale" and rule 06 depend on that signal.)
  Then Overview → **3 Done in Jira, PR open →**: it opens the Delivery list, which has no such section (the list is Overview-only, `doneOpenMarkup`),
  so the "Done in Jira, PR open" set can't be found from the Delivery list except through the clipped pill.
- **Expected:** wrap the pills (or icon+tooltip) and make the link land on a filtered/tabbed view.
- `[shot: delivery-detail]`

### F11 · NUMBER · Delivery "waiting on" disagrees with Overview "who owes" for the same item
- DEL-183 #66: Delivery list "WAITING ON: **author**" (while the sibling rows show names: `CaOs + FrMo`); detail says "waiting on author", holder roles
  `BoFe (author), FrMo (sentinel)`; Overview "WHO OWES: `BoFe (author) + FrMo (sentinel)`". The Overview header "5 orphaned approvals — waiting on FrMo — nudge the holder
  today" includes DEL-183 whose ball is actually in the author's court (needs-rework by BoFe) and DEL-167/183 which are also owed by CaOs/BoFe. Overview doc text claims "the same holder set the delivery list shows".
- Also the API has `waiting_for: CaOs` / `waiting_on: [BoFe, FrMo]` / `ball_label: author` for that row — three answers for one question.

### F12 · NUMBER · "median ×" toggle on Delivery silently does nothing
- Delivery list shows a pressed `median ×` chip, tooltip "showing the cohort median × next to each duration", but nothing is rendered: `wait_benchmark.available=false`
  because the cohort is N=8 < 10 (by design). No indication on the page why; the chip looks broken. Works on Ticket (`(median 26.7 h · 1.3×)`) and Queue.
- **Expected:** disabled chip or inline "needs ≥10 items" note.

### F13 · NUMBER · Declined PR shows a 20× "slow" benchmark
- Ticket DEL-100, PR #2 (`declined`, closed by the bot on 16 Nov, three months after the ticket's own 20 Aug close): CYCLE `504 h (median 24.8 h · 20.4×)`.
  504 h is exactly the 63-business-day window (cap), the PR is not delivered work (the Rules page itself says so), and the median is for merged PRs.
  Timeline draws the same PR as a bar spanning the whole window (`DEL-100 #2`).
- **Expected:** no benchmark multiple on declined / capped / right-censored spans.

### F23 · PAPERCUT · Overview / Delivery column truncation at 1600 px
- Overview "What to do now": WHO OWES clipped to 92 px (`CaOs (re…`, `FrMo (se…`) with ~700 px of empty row to the left; the actionable part (the holder)
  is the part that's cut. "Done in Jira" table: WHO OWES `BoFe + Fr…`; NOTE pill `PR opened aft…` overflows past the viewport edge (`span.pill` right = 1836 > 1600).
  "waiting 1w+" pill in Needs-attention is clipped and leaves a stray `.` (the ellipsis). Blocking text view: two distinct edges both read `PRs waited on the …`.
  Queue: `(median 3 h 2 min…`, Policy lab `≈2 h 45 …`, Timeline "Sprint at risk" REASON pills `Done in Jira,` / `approved, nev` / `stuck on FrMo` clipped.
- `[shot: overview-dark]`, `[shot: timeline-dark]`, `[shot: blocking-dark]`

### F24 · POLISH · Timeline page is mostly prose; the canvas is below the fold
- At 1600×1000 the first ~780 px are legend, sprint strip, "Sprint 3 at risk", carry-over list; the timeline canvas starts at y≈912 and is cut off at the bottom.
  S3 strip says `8 Oct → 18 Nov` (snapshot) while the ticket page and "plan ends" say 27 Nov. Clicking an already-selected sprint row doesn't deselect (`aria-pressed` stays true).
- `[shot: timeline-dark]`

### F26 · POLISH · Queue
- Header "WIP sampled every 24 h" vs footer "every 23 h 44 min". With person scope LaAd: "0 streams" but the response-time table below still lists everyone and the banner says it is filtered.
  Same explanatory paragraph repeated twice ("queue wait, not calendar utilisation…", "slow = long wait with little WIP…") at the bottom.
- `[shot: queue-dark]`

### F27 · POLISH · Policy lab / Sources
- Policy lab prose leaks implementation jargon to users: `service_share`, `queue_recovery`, `attention_max_sigma`, "P-K formula", "measured: 0.52-0.70 … on del3/del-combined".
  Presets *replace* knob state (each preset resets the others) while the helper text says "a scenario can combine knobs: sentinel + review SLA is one run" — only manual selection combines; scenario isn't in the URL (not shareable).
  Sources: hard-coded `data/<tenant>.duckdb`; "window 27 Jul → 16 Nov" vs top bar "20 Aug → 18 Nov" vs Rules "103.6-day dataset" vs Queue/Blocking "All" = 92.6 d (first-PR based); no per-source breakdown for the combined tenant
  (says only `jira-fixture`).
- `[shot: policy-dark]`, `[shot: sources-dark]`

### F28 · POLISH · Rules
- **Rules → people** level shows only "No person-level rule crosses its threshold…" and then repeats all the team rules (toggle is effectively a no-op). Upper-snake rule ids (`SPRINT_DELIVERY`) as titles.
  Markdown export grammar: "7 PRs opened before the window and is not in the cohort".
- **PR-size sliders:** both rails rescale after the first input (commits max 24→26, lines max 800→1380, so the thumb jumps), and at *every* cap (1/1 → 131 of 131 large; 24/800 → 0 large)
  the list stays at the same 10 rules — the footer says "the same caps drive the pr_size rule and its drill-down", but no pr_size rule ever appears. The counts line (`7 of 131 … 3 over commits · 4 over changed lines`) updates correctly; "saved per tenant" persists across reload (✔).
- `[shot: rules-dark]`, `[shot: rules-people]`

---

## 2. Themes and viewports

### F5 · BUG (a11y) · "chase →" link is unstyled: `#0000EE` on `#292B2E` = **1.51 : 1** in dark theme
- Overview "What to do now" (all 5 rows) and the phone cards. `a.chip.chase-link` has `background` from `.chip` but the browser's default link colour. In the light theme it reads fine (blue on light),
  so only dark fails. It's the only real WCAG failure found by an automated pass over Overview/Delivery/Queue/Trend in both themes (everything else ≥ ~4.0 : 1; the `·` separators are 1.5 : 1 but decorative).
- `[shot: c-pov1]` (phone), `[shot: desk-light-overview]`

### F25 · PAPERCUT · Phone (412×915)
- **Horizontal overflow:** Timeline `docSW=419`, Ticket `docSW=415` (everything else 412). Timeline: the activity-legend chips run to x=779 (`pr_approved`, `pr_merged`… off-screen, no wrap/scroll affordance);
  sprint rows clip (`28 adopted` cut off) `[shot: c-ptl1]`; Queue response table is wider than the viewport (right ≈720 px).
- **Build tag `v0.10.0 · ea244541`** is `position:fixed` at the bottom-left and sits on top of content on both viewports (e.g. over the third "Done in Jira" row and the "TICKET EVENTS" heading
  at 1600×1000 light) — `pointer-events:none`, so visual only. `[shot: desk-light-overview]`, `[shot: desk-light-ticket]`
- **Tap targets:** inline ticket keys in the Timeline carry-over/at-risk prose are 46×13 px (every DEL-xxx there); `chase →` is ~71×44 but low-contrast (F5). Topbar controls, sheets, nav drawer and tenant menu are ≥ 36 px.
- Works: nav drawer (`aria-expanded`, scrim, auto-close on navigate), bottom sheet for delivery row, Esc closes, tenant menu fits.
- `[shot: phone-nav-open]`, `[shot: phone-drawer]`, `[shot: phone-delivery-sel]`, `[shot: phone-tenant]`

### F29 · POLISH · Both themes
- Header meta line has empty-slot separators ("· · business hours"); "Or adopt a review SLA…" paragraph is flush against the card border (no padding), phone and desktop.
- Light theme otherwise consistent; light-mode toggle persists; `?theme=light` sticks as a stored preference (visiting one shared link flips your own default — dark is only recoverable via the toggle).
- `[shot: desk-light-*]`

---

## 3. Navigation

### F6 · BUG · Copy-as-Markdown links drop the tenant
- **Repro:** `?tenant=del3&view=delivery` → **Copy as Markdown** → `| blocked | [DEL-183](http://127.0.0.1:8790/#ticket/DEL-183) …`.
- **Observed:** no `?tenant=del3`. The recipient (or you, tomorrow) lands on the *default* tenant `del-combined`, a different dataset with different numbers and a different cycle for the same ticket
  (`9/9 segments` there vs `0/8` in del3). Same for the drawer/row-menu copies.
- **Also:** the Delivery export covers only the active tab but is titled "Delivery list — 1 open item" (the Blocked tab), not "Blocked: 1 of 8".

### F4 · BUG · Row context menu: "Open in timeline" ignores the item; "Clear scope" navigates away
- **Repro:** Delivery → right-click a row (menu: Scope to FrMo / BoFe · Open in timeline · Copy as Markdown · Clear scope · whole team).
  **Open in timeline** → Timeline page, nothing selected/focused/scrolled (`app/main.js` ~3237: `action === 'timeline'` only calls `showPage('timeline')`;
  `timeline.focusPr` exists but isn't used here). **Clear scope · whole team** (shown even when the scope is already `all`) → `setScope('all')` **and jumps to Timeline** from whatever view you were on.
- `[shot: ctxmenu]`, `[shot: open-in-timeline]`

### F14 · PAPERCUT · Silent fallbacks
- `?tenant=nope` → silently loads `del-combined` (URL rewritten, no notice); `view=bogus`, `tf=zzz`, `scope=person:Zed` → silently Overview/1q/all.
  Tenant switch resets a person/role scope to `all` without saying so (identity spaces are independent — plausible, but unannounced).
- `/api/meta?tenant=…` is rejected as an unknown parameter although every other endpoint takes `tenant` (so `/api/meta` can't be asked about `del3`).

### F30 · PAPERCUT · Trend state isn't in the URL
- Metric (Flow efficiency … Open PRs) and week/sprint toggle aren't encoded; reload or a shared link always returns to week/flow-efficiency. Sprint lens `sprint=` deep link on Delivery silently keeps the pill pressed but doesn't filter or persist in the URL.

### Navigation that works (checked)
Back/Forward across Overview→Delivery→Timeline→Trend and ticket deep link; in-app Back button label ("back — 2 steps back"); YAH breadcrumb jump; `← Trend` / `← Overview` ticket back chip reflects origin; tenant pill menu; `#ticket/DEL-1` renders an epic page with a clear "not a story" message; ticket search trims/uppercases (`  del-1 `) and rejects `167`, `DEL 168`, `PR #53` with "not a ticket key — try DEL-155".

---

## 4. Edge cases

### F7 · NUMBER · Person/repo scope on Overview contradicts itself
- **Repro:** `/?view=overview&scope=person:LaAd` (LaAd is a story owner with no PRs; also `repo:DEL/delivery-web`).
- **Observed (same page):**
  - header "**8 open items · 1 blocked** · scoped to person LaAd" vs tiles "**0 BLOCKED**, … of **0** open items";
  - tile "**≈304 d 6 h** MEASURED WAIT" unchanged (whole team) next to "Where waits accumulate: *No measured handover in this window.*";
  - "Done in Jira, PR open: **none** — *Every ticket marked Done in Jira has its code merged.*" — false (3 tickets are Done with open PRs team-wide) — it only means "none in this scope";
  - Delivery list scoped to BoFe/LaAd: "2 / 0 open items in view · … **4 stale · 8 unobserved**" (whole-team counts).
  - Scoping to a person with no PR role gives no hint *why* it's empty; banner reads `Scope person LaAd (person LaAd)` (label duplicated).
- **Expected:** tiles and header from the same filtered set; "none in this scope" wording.

### F15 · PAPERCUT · Scope options that do nothing
- Scope `queue ready` → banner "filtered: none · not filtered: overview, delivery list, timeline, … " — selectable control with zero effect.
  Rules/Policy/Sources/Trend/Blocking always say "not filtered — still shows the whole team" (explained, but the scope selector stays active and the filter-state banner is the only clue).

### F16 · PAPERCUT · Done in Jira, PR open / stale PRs (the new list)
- Overview list is correct and consistent (3 rows; "1 PR was opened after the ticket closed" = DEL-183, 6 d 4 h later; `unmerged` pills) and the Rules 06 "STALE_PRS" numbers (4 stale, 3 with ticket already Done) agree with Delivery (4 stale). But see F9 (not reachable from Delivery) and F11 (holder disagreement).
- All 8 open items carry `unobserved` — the pill is noise when it's true for every row; "waiting" ages are priced to a snapshot 4 days old (labelled).

### F17 · PAPERCUT · Ticket detail
- **Boundaries are not chronological** (DEL-183): `REFINED 23 Oct 13:20` is listed before `READY 23 Oct 13:00` (20 min out of order), `IN_VERIFICATION 4 Nov` before `PR_OPEN 29 Oct`; no flag for the out-of-order pair.
- Raw attribute dump in TICKET EVENTS (`actor_kind=human log_delay_seconds=72480 time_spent_seconds=10800`), clipped at `tim…`; header ends `· other` (unlabelled ticket type); `closed 2026-11-04 15:46` is UTC while the calendar is Tallinn (stated in the small print).
- **Bitbucket-only tenant (del3):** ticket DEL-183 says `PR_OPEN / FIRST_REVIEW / APPROVAL / MERGED missing` and "0/8 segments measured" although the same page lists PR #65 as merged with a 27.7 h cycle and review rounds — the PR-derived boundaries are discarded because there's no STORY object. Honest, but looks self-contradictory. `[shot: ticket-del3]`

### F18 · PAPERCUT · Annotation UI (Trend "add / delete a change")
- **Not present in v0.10.0.** `prd/61-intervention-trend.md` specifies it and the last commits (`ea24454`) change only PRD/docs; there is no annotation endpoint, table or Trend control.
  Nothing to add/delete; the Trend page has no intervention marker affordance. Out of scope to test.

---

## 5. Interactions

### F19 · PAPERCUT · Keyboard / focus
- Tab order: tenant → scope → 4 timeframe → search → theme → density → YAH → **9 sidebar items** → content; no "skip to content". Focus rings are visible everywhere (✔).
- Overview/Delivery rows (`tr[role=button][tabindex=0]`) contain real links (nested interactive) and have no `aria-selected/pressed`. Enter opens the drawer/inspector but **focus stays on the row** (not moved into the drawer) and Esc closes without refocusing anything new; mouse click on the page behind does *not* close the drawer.

### F20 · PAPERCUT · Canvas views
- Timeline: hover tip + click on a sprint band applies sprint lens (✔), `+ − Home` keys work. Blocking: click node → scopes to the role and jumps to Queue (hover tip says "click: open its queue" ✔). Queue: click a chart row → person scope (✔; hint "click a row to scope" is wrong for the response *table* rows below, which do nothing).
- Blocking people vs role levels give different totals for the same window (role 304 d 6 h / 6 edges; people 340 d 0 h / 28 edges; "mean 3 h 12 min" identical in both) — explained by concurrent-holder charging in the small print, but the Overview headline silently uses the role number.

### F21 · PAPERCUT · Policy lab
- Presets work and run (SLA 4 h, cap review at 2 rounds −13 % median, sentinel ≈55 % → ≈54 % busy; verdict text agrees with Rules' "best scenario … −13 % cycle"). Baseline validated +0.9 % ✔. A/B table is empty ("Baseline only") until Run; no loading indicator while running; scenario not in URL.

### F22 · NUMBER · "Snapshot from N calendar days ago"
- Snapshot 2026-11-18 00:10 UTC viewed on 2026-11-21 → "4 calendar days ago" (3.6 days, 3 calendar days); del3 snapshot 16 Nov → "6 calendar days ago" (5.6 days, 5 calendar days). Rounds up but says calendar days.

### F10 · NUMBER · Overview rule-of-thumb counts
- "5 orphaned approvals — waiting on FrMo" vs Rules 10 "SENTINEL_TAIL: **2 items** waiting only on FrMo up to 19 d" (threshold 3 d; DEL-197 #52 = 2 d is below it). Defensible by threshold, but the same page headline never says "5 ≠ 2".
  Rule 02 "24 stories carried over" vs sprint strip "27 carried over" — footnoted ("window vs whole store") but the numbers sit 400 px apart on one page's neighbours.

---

## 6. Content honesty — quick table

| # | where | claim | reality |
| --- | --- | --- | --- |
| F7 | Overview scoped | "8 open · 1 blocked" / "Done in Jira: none — every ticket has its code merged" | tiles say 0; 3 team-wide Done-with-open-PR |
| F8 | Trend | "0 latest · −2 since first bucket" | in-progress N=0 bucket treated as latest |
| F11 | Delivery vs Overview | "same holder set" | DEL-183: `author` vs `BoFe + FrMo` |
| F13 | Ticket DEL-100 | "504 h · 20.4×" | window cap on a bot-declined PR |
| F22 | all headers | "4 calendar days ago" | 3.6 d |
| F28 | Rules | "caps drive the pr_size rule" | no pr_size rule appears at any cap |
| F12 | Delivery | "showing the cohort median ×" | nothing shown (N<10) |

---

## 7. Reproduction cheat-sheet (copy/paste URLs, tenant `del-combined`)

```
/?tenant=del-combined&view=trend&repo=DEL/delivery-web           # F1 hang
/?tenant=del-combined&view=ticket#ticket/NOPE-1                  # F2 hang
/?tenant=del-combined&view=ticket#ticket/DEL-183                 # F3 worklog bars, F17 ordering
/?tenant=del-combined&view=overview&scope=person:LaAd            # F7 contradictions
/?tenant=del-combined&view=trend  → Merged PRs → week            # F8
/?tenant=del3&view=delivery → Copy as Markdown                    # F6 (no tenant in links)
/?tenant=del-combined&view=ticket#ticket/DEL-100                 # F13 504 h · 20.4×
/?tenant=del-combined&view=overview&theme=light (or dark)         # F5 chase link, F23 clipping
```

## 8. Not covered / caveats
- Real-mouse brush/zoom drag on the timeline canvas was only exercised by wheel/keys and hover/click via Playwright; drag-to-pan/"Set timeframe" selection was not.
- Annotation UI (F18) and cross-tenant identity joins don't exist in this build. `del3` (Bitbucket only) exercised on every view for errors; deep review of its numbers was shallower than `del-combined`.
- Contrast scan covered Overview/Delivery/Queue/Trend in both themes; Rules/Policy/Timeline/Blocking got the layout lint only.

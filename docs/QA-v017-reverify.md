# Re-Verification Pass — megaspeed `v0.17.3` (`prd/70` Fixes `F-01` … `F-15`)

- **Version / Commit SHA re-verified:** `v0.17.3-16-g16926c15` (commit **`16926c1`**, `merge: prd/70 u6-polish`)
- **Date:** `2026-11-23` (`as_of` snapshot `2026-11-22T05:00:44Z`)
- **Sandbox URL:** `http://127.0.0.1:8799/?tenant=demo-tlla`
- **Sandbox Store:** `/tmp/opencode/qa-v017-r1/demo-tlla.duckdb` (repaired store copy; live store `/data/personal/work/demo-tlla.duckdb` and port `8766` untouched)
- **Repaired Corpus (`demo-tlla`, combined Jira Cloud + hans Bitbucket `DEMO/widget`):**
  - **76,213 events** (`33,425` Jira events + `42,788` Bitbucket events; span `2021-09-28T15:17:33Z` → `2026-11-21T22:38:31Z`)
  - **2,574 PRs** kept of **10,733** seen (`8,159` PRs without a ticket key dropped) · **16,605 COMMIT** · **5,545 PRREV** · **12,938 THREAD** (`11,885` `mock-v1`, `1,053` unclassified)
  - **2,320 STORY** · **1,185 TICKET** · **71 EPIC** · **106 SPRINT** objects (`134` sprint IDs)
  - **199 PERSON** objects (`0` suffixed `X2`/`X4`/`X6` pseudonyms; `3` bots excluded: `AgJu`, `BiBi`, `system`; `LeMi2` merged to `LeMi`)
- **Viewports & themes exercised:** Desktop (`1600×1000`) and Phone (`412×915`), Dark and Light themes
- **Screenshots:** `/tmp/opencode/qa-v017-r1/shots/01-overview-1q-dark.png` … `09-phone-aging-pr-review-dark.png`

---

## 1. Summary Verdict

**Table Summary (`F-01` … `F-15` at `16926c1`): 12 PASS · 1 PARTIAL (`F-03`) · 2 FAIL (`F-05`, `F-06`) · 0 Regressions** *(with `F-16` out of scope while U7 is in progress)*

- **12 PASS:** `F-01`, `F-02`, `F-04`, `F-07`, `F-08`, `F-09`, `F-10`, `F-11`, `F-12`, `F-13`, `F-14`, `F-15`
- **1 PARTIAL:** `F-03` (Server-side `Aging`, `Done in Jira, PR open`, and `Epic` detail PR table all pass, and client-side `#11096` resolves to `DEMO-60130`; however, client-side `Delivery All 8` and `Timeline` still label `#11030` as `DEMO-56715` because `loadObjects()` hits `OBJECT_MAX = 60000` before `STORY`/`EPIC` rows in `/api/objects`, leaving `KIND_BY_KEY` unpopulated on the client, and `analysis/verdict.py` still labels `#11096` as `DEMO-9714` in `holders.current` and `adjacent.unmerged` because Wave 2 `U4` has not been implemented/merged yet.)
- **2 FAIL:** `F-05` and `F-06` (Both belong to Wave 2 unit **`U4 — verdict card`** (`analysis/verdict.py`, `app/verdict.js`); branch `prd70/u4-verdict` has `0` commits ahead of `main` at `16926c1`.)

---

## 2. `prd/70` Re-Verification Table (`F-01` … `F-15`)

| ID | Finding (`docs/QA-v017-combined.md` / `prd/70`) | Status | Concrete Evidence (`16926c1` on `http://127.0.0.1:8799/?tenant=demo-tlla`) |
| --- | --- | --- | --- |
| **F-01** | Cross-source identity split: merge suffix variants (`MaPa2`→`MaPa`, `MaRa2`→`MaRa`, `JaKu4`→`JaKu`, `MaPo6`→`MaPo`, `AlKr2`→`AlKr`, `BeWo2`→`BeWo`, `DaHa4`→`DaHa`, `LeMi2`→`LeMi`, etc.) → **199 people**, no suffixed pseudonyms, no duplicate scope/legend entries | **PASS** | Verified in `/tmp/opencode/qa-v017-r1/demo-tlla.duckdb` and UI (`01-overview-1q-dark.png`, `05-rules-1q-dark.png`):<br>• `SELECT count(*) FROM objects WHERE object_type='PERSON'` → **`199`**.<br>• `regexp_matches(..., '\d+$')` across `objects (PERSON)`, `events.actor`, and `event_objects (PERSON)` → **`0` rows (`[]`)**.<br>• Unified cross-source counts:<br>  – `MaPa`: **407 stories · 530 PRs** (`2,335` Jira + `6,559` BB events)<br>  – `MaRa`: **195 stories · 271 PRs** (`1,111` Jira + `4,519` BB events)<br>  – `JaKu`: **55 stories · 59 PRs** (`273` Jira + `1,163` BB events)<br>  – `MaPo`: **20 stories · 25 PRs** (`259` Jira + `1,280` BB events)<br>  – `AlKr`: **10 stories · 9 PRs** (`162` Jira + `158` BB events)<br>  – `BeWo`: **2 PRs** (`61` Jira + `580` BB events); `DaHa`: **1 PR** (`16` Jira + `74` BB events); `LeMi`: `4` reviewer links.<br>• `#scope` has `0` suffixed person options (`suffixedScopes: []`), and `WHO IS WHO` (`1q`) lists single unified chips (`MaPa AUTHOR`, `MaRa AUTHOR`, `JaKu AUTHOR`, `MaPo REVIEWER`, `AlKr AUTHOR`). |
| **F-02** | Person roster & `validateScope()` (`app/main.js`) must include all `PERSON` objects, `STORY.actor`, queue streams, aging holders, and events so actors outside the 40k event cap (`DmMa`, `IvZi`, `JoEr`) and reviewer-only targets (`LeMi`) appear in `#scope` and can be scoped | **PASS** | Verified on `?tenant=demo-tlla&view=overview&tf=1q` (`1600×1000`):<br>• `#scope` now populates **200 options** (all 196 non-bot humans + `everything` + roles + `repo widget`), including `person:DmMa`, `person:IvZi`, `person:JoEr`, and `person:LeMi` without loading past 40k events.<br>• `window.__megaspeed.setScope('person', 'DmMa')` → `#status`: **`"scoped to DmMa"`**, `#scope.value`: **`"person:DmMa"`**.<br>• `setScope('person', 'IvZi')` → **`"scoped to IvZi"`**.<br>• `setScope('person', 'LeMi')` → **`"scoped to LeMi"`** (`#scope.value`: `"person:LeMi"`). |
| **F-03** | One canonical primary ticket per multi-key PR (`primary_ticket`: `STORY`/`TICKET` over `EPIC`, `open` over `done`, newest key on a tie) across Aging, Delivery, Ticket detail, Timeline, and Verdict; Epic detail page lists PRs that name the Epic directly | **PARTIAL** | **What PASSES:**<br>1. **Aging (`PR review` / `PR cycle`, `04-aging-pr-review-dark.png`):** `#11030` now resolves to **`DEMO-57507 #11030 widget`** with state **`unmerged`** (`very old`), and `#11096` resolves to **`DEMO-60130 #11096 widget`** with state **`live`** (`normal`).<br>2. **Overview & Delivery `Done in Jira, PR open` (`01-overview-1q-dark.png`):** `#11096` (whose canonical ticket is the open story `DEMO-60130`) is no longer falsely listed under the 2023-Done story `DEMO-9714`; `done_open.count` is **`1`** (`DEMO-57507 #11030 widget`).<br>3. **Epic detail page (`#ticket/DEMO-56715`, `07-ticket-epic-56715-dark.png`):** Now renders **`PULL REQUESTS · 3`** (`#10803 declined`, `#10865 merged`, `#11030 open · censored`) with note `"the pull requests that name this epic directly — a PR that also names a child story appears on both pages"` (and `#ticket/DEMO-1968` lists all **24** PRs).<br>4. **Client `#11096`:** Both `Delivery All 8` and `Timeline` (`lanes=object`) now label `#11096` as **`DEMO-60130 #11096 widget`**.<br>**Why PARTIAL (2 remaining gaps — see Section 4):**<br>1. **Client-side `KIND_BY_KEY` misses `STORY`/`EPIC` objects due to `/api/objects` ordering + `OBJECT_MAX = 60000`:** In `Delivery All 8` and `Timeline` (`lanes=object`), **`#11030` still renders as `DEMO-56715 #11030 widget`** (the Epic key!) instead of `DEMO-57507 #11030 widget`. Why? `/api/objects` orders alphabetically by `object_type`; on `demo-tlla`, `COMMIT` (`16,605`) + `EPIC` (`71`) + `PERSON` (`199`) + `PR` (`2,574`) + `PRREV` (`5,545`) + `SPRINT` (`106`) + `STORY` (`2,320`) + `THREAD` (`12,938`) + `TICKET` (`1,185`) = `41,543` rows, **and** `TICKET:DEMO-56715` exists alongside `EPIC:DEMO-56715`! In `indexTickets(objects)` (`app/tickets.js:110`), `if (known === 'STORY' && type !== 'STORY') continue;` only protects `'STORY'` from being overwritten by `'TICKET'`, so `TICKET:DEMO-56715` (which comes after `EPIC:DEMO-56715` alphabetically!) **overwrites `kinds.get('DEMO-56715')` from `'EPIC'` to `'TICKET'`**! Then `primaryTicket` ranks both `DEMO-56715` (`TICKET`, state `undefined` → rank `[0, 1, -56715]`) and `DEMO-57507` (`STORY`, state `'done'` → rank `[0, 2, -57507]`), so `DEMO-56715` (rank `[0, 1]`) beats `DEMO-57507` (rank `[0, 2]`)!<br>2. **Wave 2 `U4` (`analysis/verdict.py`) not merged yet:** In `/api/verdict?tenant=demo-tlla&days=90`, `holders.current` and `adjacent.unmerged` still use `_ticket_of` (`keys[-1]`) and attribute `#11096` to `"DEMO-9714"` while `constraint.items` attributes `#11096` to `"DEMO-60130"`. |
| **F-04** | Timeline Forecast card (`app/main.js:3615`) must pass the active timeframe's `days` to `/api/forecast` so `1q` (`90d`, `CV 0.45`) forecasts instead of refusing on all-time 260-week `CV 0.87`, while `1w` still refuses honestly | **PASS** | Verified on `?tenant=demo-tlla&view=timeline` (`02-timeline-1q-dark.png`):<br>• **`1q` (`90d`):** `#forecast-card` renders **`6–16 of 41 · by 27 Nov`** (`P50 11 · 0 % chance all 41 · 85 % by 2 Jan · 10,000 resamples · 12 weeks`) + `WHAT THE MODEL CAN'T SEE · 39 at risk · 59 pts`.<br>• **`1w` (`7d`):** Refuses honestly with **`"Not enough history to forecast: 0 weeks observed, 8 needed."`**<br>• **`4w` (`28d`):** Refuses honestly with **`"Not enough history to forecast: 3 weeks observed, 8 needed."`**<br>• **`All` (`1882d`):** Refuses honestly with **`"Throughput is erratic (CV 0.87): the range would be so wide it would look like a forecast and behave like a guess."`** |
| **F-05** | Verdict card `Next: verification 634 h · refinement ≈2.3k h` summary & caption (`app/verdict.js:330`) should explain live-first (`owed now`) ordering | **FAIL** *(U4 not merged)* | On `?tenant=demo-tlla&view=overview&tf=1q` (`01-overview-1q-dark.png`), `<details data-next-constraint>` still renders `Next: verification 634 h · refinement ≈2.3k h · starting work ≈1.8k h` with caption `"Every station's removable pool, ranked by the same clock and method as the constraint."` Branch `prd70/u4-verdict` has no commits yet (Wave 2 unit `U4`). |
| **F-06** | Verdict card `HOLDERS PR clock` (`analysis/verdict.py:475`, `app/verdict.js:136`) sums all-time 5-year PR review hours (`MaRa ≈4.6k h · peers 0–4624 h`) across all timeframes and includes the top peer in `peers` | **FAIL** *(U4 not merged)* | On `/api/verdict?tenant=demo-tlla&days=28`, `days=90`, and `days=1882` (`01-overview-1q-dark.png`), `holders` still returns all-time `MaRa: 4623.74 h` with `peer_range: [0.0, 4623.7]` (`"MaRa ≈4.6k h · peers 0–4624 h"`). Branch `prd70/u4-verdict` has no commits yet (Wave 2 unit `U4`). |
| **F-07** | Timeline `lanes = queues` (`app/timeline.js`) must price queue lanes on the business-hours clock (`1 d = 8 h`) instead of raw 24/7 wall-clock hours (`60.9k h`) | **PASS** | Verified on `?tenant=demo-tlla&view=timeline&tf=all` with `lanes = queues` (`03-timeline-queue-all-dark.png`):<br>• `window.__megaspeed.timeline.laneRows()` now prices queue segments through the business-hours calendar (`09:00-17:00 Europe/Tallinn`, Mon–Fri, holidays excluded):<br>  – `review`: **`review · 13.6k h`** (`13,587.3 h`, down from `60.9k` 24/7 wall-clock hours)<br>  – `author`: **`author · 272 h`** (`272.0 h`, down from `990` 24/7 wall-clock hours) |
| **F-08** | Top bar `#status` (`app/main.js`) must distinguish loaded vs total events (`40,000 of 76,213 events loaded`) and share one snapshot age with `#provenance` | **PASS** | Verified across all desktop views (`01-overview-1q-dark.png`, `04-aging-pr-review-dark.png`, `05-rules-1q-dark.png`, `08-sources-dark.png`):<br>• `#provenance`: **`"demo-tlla · Snapshot from 21 h ago: 76213 events, 2574 PRs in the dataset, 16605 commits..."`**<br>• `#status`: **`"live · 40,000 of 76,213 events loaded · demo-tlla"`** (reconciling both numbers everywhere). |
| **F-09** | Unified holder attribution (`holder_attribution` in `analysis/ballcourt.py`) across Aging, Delivery, and Ticket detail: `#11030`/`#11088`/`#11095` → `review queue` (never `—` or `"nobody"`); `#11093` → `ready to merge` | **PASS** | Verified across all three surfaces (`04-aging-pr-review-dark.png`, `06-ticket-57507-dark.png`):<br>• **Delivery (`All 8`):** `#11030`, `#11088`, `#11095` → **`review queue`**; `#11093` → **`ready to merge`**.<br>• **Aging (`PR review` & `PR cycle`):** `#11030`, `#11088`, `#11095` → `HOLDER =` **`review queue`**; `#11093` → `HOLDER =` **`ready to merge`** (`0` rows have `HOLDER = —`).<br>• **Ticket detail (`#ticket/DEMO-57507`):** Header pill reads **`"PR #11030 open · waiting 5 d 0 h on review queue"`** and `#ticket-answer` reads **`"PR #11030 (waiting 5 d 0 h on review queue)."`** (`"waiting ... on nobody"` is completely gone). |
| **F-10** | Disclose `mock-v1` thread classifier on Rules (`#rules-view`, `comment_tax`) and in `/api/insights` | **PASS** | Verified on `?tenant=demo-tlla&view=rules&tf=1q` (`05-rules-1q-dark.png`) and `/api/insights`:<br>• Above the Rules metrics, `[data-thread-labels]` renders: **`"labels: deterministic mock (mock-v1) — all 1,837 classified threads carry placeholder labels; a model relabel is pending"`** (`11,885` on `All`).<br>• `/api/insights` `comment_tax.outcome_note` now states: **`"acted_on means a deterministic signal (the diff moved at the anchor) or a label from the deterministic mock classifier (a placeholder, not a model read)... Labels: deterministic mock (mock-v1) — all 1,837 classified threads carry placeholder labels; a model relabel is pending"`**. |
| **F-11** | Cap embedded drill-down arrays in `/api/insights` (`DRILL_CAP = 200`) so `GET /api/insights?days=1882` drops from `12.2 MB` to `~1.1 MB` while keeping honest totals | **PASS** | Verified via API across all 5 windows:<br>• `days=1882` (`All`): **`1,105,384` bytes (`1,079.5 KB` / `1.05 MB`)**, down **91.1%** from `12,468,756` bytes (`12.2 MB`)!<br>• `days=120`: **`844,623` bytes (`824.8 KB`)**, down from `2.20 MB`.<br>• `days=90` (`1q`): **`812,877` bytes (`793.8 KB`)**, down from `1.91 MB`.<br>• `days=28` (`4w`): **`519,115` bytes (`506.9 KB`)**; `days=7` (`1w`): **`284,477` bytes (`277.8 KB`)**.<br>• Visible rule drill-down counts and totals (`186 items · 108 affected`, `180 items · 123 affected`, `80 items · 80 affected`, `8 items · 4 affected`) reconcile identically. |
| **F-12** | Multi-year date ranges include the year (`28 Sep 2021 → 21 Nov 2026`), `All` on Timeline clamps to data end (`21 Nov 2026`, not future `14 Dec`), and `All` sprint band labels do not overlap (`S55…` / `S5800`) | **PASS** | Verified on Sources (`08-sources-dark.png`) and Timeline `All` (`03-timeline-queue-all-dark.png`):<br>• **Sources `store span`:** **`28 Sep 2021 → 21 Nov 2026 (every event the store holds)`**.<br>• **Timeline `All` `#range`:** `window.__megaspeed.timeline` + `dataWindow()` formats `#range` as **`28 Sep 2022 → 21 Nov 2026`** (the loaded 40k-event + 2,574-PR data window, clamped to `21 Nov 2026` instead of future padded `14 Dec`).<br>• **Timeline `All` sprint band labels (`03-timeline-queue-all-dark.png`):** `placeSprintLabels()` skips narrow colliding bands and draws **`S5800`** cleanly with zero overlap. |
| **F-13** | Widen `.aging .c-id` (`200px`) so 5-digit PR numbers and repo badges (`DEMO-58511 #11021 widget`) do not truncate to `#1…` at `1600×1000` | **PASS** | Verified on `?tenant=demo-tlla&view=delivery&tab=aging&tf=1q` (`Stage = PR review`, `04-aging-pr-review-dark.png`):<br>• Every row displays its full ticket key, 5-digit PR number, and repo badge (`DEMO-58511 #11021 widget`, `DEMO-57507 #11030 widget`, `DEMO-58511 #11091 widget`, …, `DEMO-60130 #11096 widget`).<br>• DOM truncation check across all `td`/`th` cells in `#delivery-table`: **`trunc = []` (`0` truncated cells)**, and `.table.aging` has **`clientWidth === scrollWidth === 1048`** (no horizontal scrollbar). |
| **F-14** | Epic child stories render a single `open` state pill instead of `open · open` (`app/ticket.js:617`) | **PASS** | Verified on `#ticket/DEMO-56715` (`07-ticket-epic-56715-dark.png`):<br>• All 17 child story `.pill` elements render either **`done`** (6 stories) or **`open`** (11 stories) — zero instances of `open · open`. |
| **F-15** | Sources per-source ingest table (`app/main.js`) shows `ISSUES | PRS | DROPPED | EVENTS` so `jira-cloud` shows `2,391 kept of 2,391 seen` and `33,425` events instead of `0 / 0 / 0` PRs | **PASS** | Verified on `?tenant=demo-tlla&view=sources` (`08-sources-dark.png`):<br>• `jira-cloud`: `ISSUES =` **`2,391 kept of 2,391 seen`**, `PRS =` **`—`**, `DROPPED =` **`—`**, `EVENTS =` **`33,425`**.<br>• `hans`: `ISSUES =` **`—`**, `PRS =` **`2,574 kept of 10,733 seen`**, `DROPPED =` **`8,159`**, `EVENTS =` **`42,788`**. |

---

## 3. Regression Check (`prd/68` & Phone/Desktop Invariants)

We re-ran all checks covering the `prd/68` fixes on the touched files (`analysis/aging.py`, `analysis/ballcourt.py`, `analysis/tickets.py`, `analysis/rules.py`, `app/main.js`, `app/format.js`, `app/timeline.js`, `app/tables.js`, `app/tickets.js`, `app/ticket.js`, `app/rules.js`, `app/style.css`):

1. **Desktop Aging Table Width & No-Scroll (`prd/68 F-14` + `prd/70 F-13`):**
   - At `1600×1000`, `.table.aging` has `clientWidth === 1048` and `scrollWidth === 1048` (`0px` horizontal scroll) on both `Story cycle` (`only=live` and `only=all`) and `PR review` / `PR cycle`, with `0` truncated cells.
2. **Aging Holders, Re-plan Feed & Scope Filtering (`prd/68 F-03, F-06, F-08`):**
   - Under default `only=live` (`1q`, `Story cycle`), the 6 live stories render with their merged holders (`MaPa (owner)`, `FuSu (owner)`, `CaGi (review) + FuSu (review) + JaKu (review) + MiMe (review)`), and `#aging-replan` renders all **99** window-scoped re-plan items directly below.
   - Selecting `person:MaPa` in `#scope` now filters **both** story rows (owned by `MaPa`) and PR/review rows (authored/held by `MaPa`, formerly `MaPa2`).
3. **Verdict & Rules Consistency (`prd/67 W-04, W-05` & `prd/68 F-05, F-07, F-09`):**
   - The Overview Verdict card and Rules Verdict banner both state: `"The constraint is the approval gate. ≈1.5k h of removable wait — 62 h owed now on 5 stories."`
   - Rules `First-pass yield` (`1q`: `92% · 99% · 19% · 98% → ROLLED 17%`, `9 / 84`) and `jira-carry-over` (`1880.6 calendar days` on `All`, `limit days: 1.4 business days` on `stale-prs`) render with zero regressions.
4. **Phone (`412×915`) Zero Horizontal Overflow (`prd/68 F-15` + `prd/70 F-13`):**
   - Exercised all 9 views (`overview`, `timeline`, `queue`, `delivery` + `aging pr:review`, `trend`, `rules`, `policy`, `ticket #ticket/DEMO-56715`, `sources`) at `412×915` in **both Dark and Light themes**:
   - `document.documentElement.scrollWidth === 412` (`0px` overflow) on every view (`09-phone-aging-pr-review-dark.png`).
   - `0` console errors and `0` page errors across desktop and phone.

---

## 4. Detailed Repros for `PARTIAL` / `FAIL` Items

### 1. `[F-03]` PARTIAL — Why client-side `Delivery (All 8)` and `Timeline (object lanes)` still label `#11030` as `DEMO-56715` (and why `/api/verdict` still has `#11096` as `DEMO-9714` in two lists)

- **Sub-issue A (Client-side `indexTickets` in `app/tickets.js:108-112`):**
  - **Repro:** Open `http://127.0.0.1:8799/?tenant=demo-tlla&view=delivery&tf=1q`, click the **`All 8`** tab (or open **Timeline** and switch `lanes` to **`pull requests`**), and look at row `#11030`.
  - **Observed:** `#11030` is labelled **`DEMO-56715 #11030 widget`** (the Epic key) on `Delivery → All 8` and `Timeline → pull requests`, whereas `Delivery → Aging (PR review)` and `Done in Jira, PR open` label it **`DEMO-57507 #11030 widget`** (the Story key).
  - **Root cause (`app/tickets.js:108-112`):**
    In `demo-tlla`, `/api/objects` returns both `EPIC:DEMO-56715` (`object_type: "EPIC", state: "open"`) and `TICKET:DEMO-56715` (`object_type: "TICKET", state: null`, emitted by the PR ingest whenever a PR mentions a key). Because `/api/objects` is ordered alphabetically by `object_type`, `EPIC:DEMO-56715` is processed first (`kinds.set("DEMO-56715", "EPIC")`), and then `TICKET:DEMO-56715` is processed later. Look at lines 108–112 of `app/tickets.js`:
    ```js
    const known = kinds.get(key);
    // a STORY row outranks a bare TICKET key dimension for the same key
    if (known === 'STORY' && type !== 'STORY') continue;
    kinds.set(key, type);
    if (obj.state) states.set(key, String(obj.state));
    ```
    Because the guard only checks `known === 'STORY'`, when `known === 'EPIC'` and `type === 'TICKET'`, **`TICKET:DEMO-56715` overwrites `kinds.get('DEMO-56715')` from `'EPIC'` to `'TICKET'`**!
    And because `EPIC:DEMO-56715` had `state: "open"`, `states.get('DEMO-56715')` remains `'open'`!
    Consequently, `primaryTicket(['DEMO-56715', 'DEMO-57507'])` sees `DEMO-56715` as `{kind: 'TICKET', state: 'open'}` (rank `[0, 0, -56715]`) and `DEMO-57507` as `{kind: 'STORY', state: 'done'}` (rank `[0, 2, -57507]`), picking `DEMO-56715`!
  - **1-line fix in `app/tickets.js:110`:**
    Protect **both** `'STORY'` and `'EPIC'` from being overwritten by a bare `'TICKET'` dimension row:
    ```js
    if ((known === 'STORY' || known === 'EPIC') && type === 'TICKET') continue;
    ```

- **Sub-issue B (Server-side `analysis/verdict.py` — Wave 2 `U4` not merged yet):**
  - **Repro:** `curl -s "http://127.0.0.1:8799/api/verdict?tenant=demo-tlla&days=90" | jq '{constraint_items: .constraint.items, holders_current: .holders.current, unmerged: .adjacent[1]}'`
  - **Observed:** `constraint.items` lists `#11096` under `"key": "DEMO-60130"`, while `holders.current` and `adjacent.unmerged` in the same payload still list `#11096` under `"ticket": "DEMO-9714"` (and count `#11096` as a 2nd unmerged Done story, contradicting `Done in Jira, PR open` which now has `count: 1`).
  - **Root cause:** `analysis/verdict.py` is owned by Wave 2 unit **`U4 — verdict card`**, which has not been committed/merged yet.

---

### 2. `[F-05]` & `[F-06]` FAIL — Wave 2 Unit `U4 — verdict card` (`analysis/verdict.py`, `app/verdict.js`) not committed/merged at `16926c1`

- **Repro:**
  1. Check `git log main..prd70/u4-verdict` → empty (`0` commits).
  2. Open `http://127.0.0.1:8799/?tenant=demo-tlla&view=overview&tf=1q` (`01-overview-1q-dark.png`).
- **Observed:**
  - **`F-05`:** The `#verdict` footer summary still reads `Next: verification 634 h · refinement ≈2.3k h · starting work ≈1.8k h` without noting that `Verification` ranks first because it has `8 h` owed now (`live_hours`), while the expanded `.panel-note` still says `"ranked by the same clock and method as the constraint"` without mentioning live-first ranking.
  - **`F-06`:** `HOLDERS PR clock` in `#verdict` still sums all-time 5-year review hours (`MaRa ≈4.6k h · peers 0–4624 h`) identically across `4w`, `1q`, and `All`, and includes `MaRa` (`4623.7 h`) as the upper bound of `peers 0–4624 h`.


---

## Final addendum — U4 + U7 (v0.18.0)

- **F-03 client END:** fixed in `9163b12` — `/api/objects` orders `EPIC` before `TICKET`, and the
  `TICKET` row downgraded the epic into the story tier where its open state beat its done story.
  `#11030` now resolves to the story in Delivery/Timeline as well.
- **F-03 verdict:** U4 uses the canonical ticket; `/api/verdict?days=90` gives `#11030 → DEMO-57507`
  and `#11096 → DEMO-60130`.
- **F-05:** `Next:` is now explicitly ordered by hours owed now, then the reference pool, and prints
  both (`verification 8 h owed now (634 h removable)`), consistent with the caption.
- **F-06:** holder PR hours are clipped to the card's window (`50.97 h` @1w, `271.98 h` @4w,
  `617.0 h` @1q; `All` unchanged) and the redundant peer range is suppressed for a peer.
- **F-16:** scrape-side `sprint_no` (numeric, no name text), API rows, and one `S<sprint_no>` /
  `S<id>` formatter across Sprints, Timeline, Rules, ticket chips and Trend. The live store predates
  the parser (`sprint_no: null`), so it renders the `S<id>` fallback until a Jira re-scrape with the
  original identity map.

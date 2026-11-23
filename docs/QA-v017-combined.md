# Free-Exploring Adversarial QA Pass — megaspeed `v0.17.3` (Combined Bitbucket + Jira Corpus)

- **Version / Commit SHA tested:** `v0.17.3-1-g4135dc30` (commit `4135dc3`, `docs(state): record the live store rekey (57 Bitbucket actors, 329->223 people)`)
- **Date:** `2026-11-23` (`as_of` snapshot `2026-11-22T05:00:44Z`)
- **Sandbox URL:** `http://127.0.0.1:8799/?tenant=demo-tlla`
- **Sandbox Store:** `/tmp/opencode/qa-v017/demo-tlla.duckdb` (read-only copy of the combined live store; live store `/data/personal/work/demo-tlla.duckdb` and port `8766` untouched)
- **Corpus (`demo-tlla`, combined Jira Cloud + hans Bitbucket `DEMO/widget`):**
  - **76,213 events** (`33,425` Jira events with `repo IS NULL` + `42,788` Bitbucket events on `DEMO/widget`; span `2021-09-28T15:17:33Z` → `2026-11-21T22:38:31Z`)
  - **2,574 PRs** kept of **10,733** seen (`8,159` PRs without a ticket key dropped; `2,393` merged, `173` declined, `8` open)
  - **16,605 COMMIT** objects · **5,545 PRREV** objects · **12,938 THREAD** objects (`11,885` classified with `mock-v1`, `1,053` unclassified `None`)
  - **2,320 STORY** objects (`1,185` TICKET objects) · **71 EPIC** objects · **106 SPRINT** objects (`134` sprint IDs incl. 28 event-only sprints)
  - **223 PERSON** objects (`3` bots excluded: `AgJu`, `BiBi`, `system`)
- **Viewports & themes exercised:** Desktop (`1600×1000`) and Phone (`412×915`), Dark (`?theme=dark`) and Light (`?theme=light`) themes
- **Screenshots:** `/tmp/opencode/qa-v017/shots/01-overview-1q-dark.png` … `14-phone-rules-dark.png`

> **Mock-Label Caveat (`mock-v1`):** All 11,885 classified `THREAD` objects in this store currently carry `classifier_version: "mock-v1"` (the deterministic hash-based mock classifier; the real LLM relabel patch was running separately and was **not** imported into this store). Label *quality* (e.g., 7,203 `suggestion`, 2,806 `question`, 10,294 `unresolved`) is therefore not evaluated as a defect here. Surfaces were audited specifically for any claim that `mock-v1` labels are model-derived, or any crash/inconsistency tied to thread labels.

---

## 1. Executive Verdict

**Verdict (`v0.17.3` / `4135dc30` on combined `demo-tlla`): SHIP-WITH-NOTES (or MUST-FIX if cross-source identity completeness and multi-key PR attribution are release blockers)**

The combined Jira + Bitbucket `demo-tlla` corpus (`76,213` events, `2,574` PRs, `2,320` stories) renders across all 9 navigation views, 5 timeframes (`1w`, `4w`, `1q`, `120d`, `all`), dark and light themes, and desktop (`1600×1000`) + phone (`412×915`) viewports with **0 console errors, 0 uncaught page errors, 0 failed network requests, 0 horizontal overflows on phone (`scrollWidth === 412`), and 0 pseudonymisation contract leaks**. Furthermore, **all 16 `prd/68` fixes (`F-01`..`F-16` from `docs/QA-v014.md`) held up with zero regressions** once the 2,574 Bitbucket PRs were joined to the 2,320 Jira stories.

However, adversarial exploration of the new cross-source joins, the 76.2k-event scale (which crosses the client's `EVENT_MAX = 40,000` cap for the first time), and the post-rekey identity space exposed **15 findings**:
- **3 BUGs** (`F-01` .. `F-03`)
- **8 PAPERCUTs** (`F-04` .. `F-11`)
- **4 POLISH** (`F-12` .. `F-15`)

### Top 3 Findings
1. **`[F-01]` BUG (Cross-source identity split after live rekey):** At least 5 core engineers remain split into separate Jira-only (`X`) and Bitbucket-only (`X2`/`X4`/`X6`) pseudonyms because Bitbucket assigned numeric collision suffixes during separate-map scraping (`MaPa` 407 Jira stories vs `MaPa2` 530 BB PRs with **222 shared stories**; `MaRa` 195 stories vs `MaRa2` 271 PRs with **102 shared stories**; `JaKu` 55 stories vs `JaKu4` 59 PRs with **40 shared stories**; `MaPo` 20 stories vs `MaPo6` 25 PRs with **15 shared stories**; `AlKr` vs `AlKr2`; `BeWo` vs `BeWo2`; `DaHa` vs `DaHa4`). Every person-scoped view, the Overview Verdict holder line (`MaRa2 ≈4.6k h`), and the `WHO IS WHO` legend treat each pair as two different people (one pure `"story owner"`, one pure `"author"`/`"reviewer"`).
2. **`[F-02]` BUG (Client-side `validateScope()` & `#scope` reject 102 valid people when `events > 40,000` or when a person is only a linked reviewer/story owner):** Because `app/main.js` caps initial event loading at the newest `40,000` of `76,213` events (`ts >= 2025-05-20`) and `validateScope()` (`app/main.js:3074`) requires `state.events.some(e => e.actor === value)`, **75 historical event actors** (including `DmMa` with 2,647 events and `IvZi` with 777 events, both shown in the `1q` `WHO IS WHO` legend because they own still-open stories!) and **27 reviewers** who only appear in `event_objects` (`PERSON:LeMi2`, who is an active `1q` Queue stream!) are absent from `#scope` and rejected with `"that scope has no events in this dataset — showing everything"` when clicked or deep-linked.
3. **`[F-03]` BUG (Multi-key PRs pick the first ticket key or an EPIC key instead of the story key across Aging, Done-but-open, Verdict, and Timeline):** `PR #11030` carries `ticket_keys: ['DEMO-56715', 'DEMO-57507']` where `DEMO-56715` is an **EPIC** (`EPIC:DEMO-56715`) and `DEMO-57507` is the **STORY** (`done`), while `PR #11096` carries `['DEMO-60130', 'DEMO-9714']` where `DEMO-60130` is an open story created today and `DEMO-9714` is a story closed in **May 2023**. Because `DEMO-56715` appears on only 3 of 2,574 PRs (below the 25% ` _parent_keys` heuristic), `analysis/ballcourt.py` keeps `DEMO-56715` in `story_tickets` at index 0. As a result, `Aging (PR review / PR cycle)` labels `#11030` with the Epic `DEMO-56715` (and clicking it opens the Epic page, which lists **0 PRs**!), while `Verdict` (`_ticket_of` takes `keys[-1]`) labels `#11030` as `DEMO-57507` in the Exploit chase list and `#11096` as `DEMO-60130` in `constraint.items` but `DEMO-9714` in `holders.current`.

---

## 2. Coverage Matrix

| Surface / Area | Timeframes Exercised | Scopes Exercised | Viewports & Themes | Status |
| --- | --- | --- | --- | --- |
| **Overview (`view=overview`)** | `1w`, `4w`, `1q`, `120d` (API), `all` | `all`, `repo:DEMO/widget`, `person:LaNo`, `person:MaPa`, `person:MaPa2`, `person:DmMa`, `role:review` | `1600×1000` (Dark/Light), `412×915` (Dark/Light) | Exercised (`F-01`, `F-02`, `F-03`, `F-05`, `F-06`) |
| **Timeline (`view=timeline`)** — `actor`, `story`, `object`, `queue` lanes, zoom/pan, brush, `Load the full range` | `1w`, `4w`, `1q`, `all` | `all`, `repo:DEMO/widget`, `person:LaNo`, `person:TiTr`, `person:MaPa`, `person:MaPa2`, `ticket:DEMO-57507` | `1600×1000` (Dark/Light), `412×915` (Dark/Light) | Exercised (`F-01`, `F-02`, `F-07`, `F-08`, `F-12`) |
| **Sprints & Forecast (`#sprint-strip`, `#forecast-card`, `/api/sprints`, `/api/forecast`)** | Store-wide + `1w`, `4w`, `1q`, `120d`, `all` API windows; sprint lens `S18568`, `S6596`, `S4958` | `all`, `repo:DEMO/widget` | `1600×1000` (Dark/Light), `412×915` (Dark/Light) | Exercised (`F-04`, `F-12`) |
| **Blocking map (`view=blocking`)** | `1w`, `4w`, `1q`, `all` (`level=queue`, `level=person`) | `all`, `repo:DEMO/widget`, `person:LaNo` | `1600×1000` (Dark), `412×915` (Dark) | Exercised |
| **Queue (`view=queue`)** — WIP streams, response table, who-is-who legend | `1w`, `4w`, `1q`, `120d` (API), `all` (`level=person`, `level=queue`) | `all`, `repo:DEMO/widget`, `person:LaNo`, `person:LeMi2`, `person:BuMa`, `person:MaBu` | `1600×1000` (Dark/Light), `412×915` (Dark/Light) | Exercised (`F-01`, `F-02`) |
| **Delivery list (`view=delivery`)** — `Blocked 0`, `Oldest waiting 1`, `All 8`, `Done in Jira, PR open 2` | `1w`, `4w`, `1q`, `all` | `all`, `repo:DEMO/widget`, `person:LaNo`, `person:FuSu`, `role:review` | `1600×1000` (Dark/Light), `412×915` (Dark/Light) | Exercised (`F-03`, `F-09`) |
| **Aging (`view=delivery&tab=aging`)** — `story:cycle`, `pr:review`, `pr:cycle`, `story:backlog..in_verification`, `only=live/all`, `self/fixed` | `1w`, `4w`, `1q`, `120d` (API), `all` | `all`, `repo:DEMO/widget`, `person:LaNo`, `person:MaPa`, `person:MaPa2`, `person:CaGi41` | `1600×1000` (Dark/Light), `412×915` (Dark/Light) | Exercised (`F-03`, `F-09`, `F-13`) |
| **Trend (`view=trend`)** — all 11 metrics × `week` & `sprint` buckets, annotations, before/after compare | `1w`, `4w`, `1q`, `all` | `all`, `repo:DEMO/widget` | `1600×1000` (Dark/Light), `412×915` (Dark/Light) | Exercised |
| **Rules & Yield (`view=rules`)** — Verdict banner, First-pass yield, Stability, 10 rules, PR size sliders, Worklog cross-check, Comment tax (`mock-v1`), Story rework | `1w`, `4w`, `1q`, `120d` (API), `all` (`level=team`, `level=person`) | `all`, `repo:DEMO/widget`, `person:LaNo` | `1600×1000` (Dark/Light), `412×915` (Dark/Light) | Exercised (`F-10`, `F-11`) |
| **Policy lab (`view=policy`)** — calibration, validation gate (`+146.9%` failure), 8 knobs, 7 presets | `1w`, `4w`, `1q`, `120d` (API), `all` | `all`, `repo:DEMO/widget` | `1600×1000` (Dark/Light), `412×915` (Dark/Light) | Exercised |
| **Ticket detail (`#ticket/KEY`)** — stories (`DEMO-57062`, `DEMO-58511`, `DEMO-57507`, `DEMO-9714`, `DEMO-8050`) & epics (`DEMO-56715`, `DEMO-12350`, `DEMO-1968`) | Store-wide per ticket | Deep links, PR drawer buttons, Copy-as-Markdown | `1600×1000` (Dark/Light), `412×915` (Dark/Light) | Exercised (`F-03`, `F-09`, `F-14`) |
| **Sources (`view=sources`)** — provenance, 2 ingest runs (`jira-cloud` + `hans`) | `1w`, `4w`, `1q`, `all` | `all` | `1600×1000` (Dark), `412×915` (Dark/Light) | Exercised (`F-12`, `F-15`) |
| **Pseudonymisation Audit (`/api/*`)** | All endpoints across `7..1882` days | `all`, `repo=DEMO/widget` | 58 JSON payloads + DuckDB tables inspected | **PASS (0 leaks)** |

*Untested gaps:* We did not mutate annotations (`POST /api/annotations` / `DELETE /api/annotations/{id}`) because this exploration is strictly read-only against the sandbox store. Note also that `120d` is supported on the API (`?days=120`) and was verified via API, while the top-bar UI buttons expose `1w`, `4w`, `1q`, and `All`.

---

## 3. Findings (`F-01` … `F-15`, Ranked by Severity)

### Area A — Bugs (Wrong Data, Broken Scoping, Cross-Source Join Defects)

#### [F-01] BUG · Cross-Source Identity Space (`demo-tlla` store / `ingest/identity.py`) · 5+ core engineers remain split into separate Jira (`MaPa`, `MaRa`, `JaKu`, `MaPo`, `AlKr`) and Bitbucket (`MaPa2`, `MaRa2`, `JaKu4`, `MaPo6`, `AlKr2`) pseudonyms across all views
- **Severity:** BUG (Split person histories / wrong person-level metrics & scopes after identity merge)
- **View:** All views (`Overview`, `Timeline`, `Queue`, `Delivery`, `Aging`, `Rules`, `Ticket detail`), e.g. `http://127.0.0.1:8799/?tenant=demo-tlla&view=overview&tf=1q` (`1600×1000` and `412×915`, Dark & Light).
- **Screenshots:** `/tmp/opencode/qa-v017/shots/01-overview-1q-dark.png`, `/tmp/opencode/qa-v017/shots/02-timeline-1q-dark.png`.
- **Repro:**
  1. Open `http://127.0.0.1:8799/?tenant=demo-tlla&view=overview&tf=1q` and inspect the **`WHO IS WHO`** legend and the **`#scope`** dropdown.
  2. Look for `MaPa` vs `MaPa2`, `MaRa` vs `MaRa2`, `JaKu` vs `JaKu4`, `MaPo` vs `MaPo6`, and `AlKr` vs `AlKr2`.
  3. Open `#ticket/DEMO-58340` (or query the store for stories assigned to `MaPa`, `MaRa`, `JaKu`, `MaPo`, `AlKr` and compare with the PR author on those exact stories).
- **Observed:**
  - The 2026-11-23 live store rekey (`STATE.md` lines 39–50) swapped 2-syllable surname-first Bitbucket pseudonyms (`NoLa` → `LaNo`, `SuFu` → `FuSu`, `MeMi` → `MiMe`), which successfully merged `LaNo`, `FuSu`, `MiMe`, `DmMa`, and `IvZi`.
  - However, whenever a Bitbucket actor had a **numeric collision suffix** (`2`, `4`, `6`, etc.) from scraping against a separate/larger Bitbucket user table, swapping or rekeying left the numeric suffix intact (`MaPa2`, `MaRa2`, `JaKu4`, `MaPo6`, `BeWo2`, `AlKr2`, `DaHa4`, `JoBr3`, `AlAl3`, `AlMu3`, `RoHa2`), while Jira had the unsuffixed pseudonym (`MaPa`, `MaRa`, `JaKu`, `MaPo`, `BeWo`, `AlKr`, `DaHa`, `JoBr`, `AlAl`, `AlMu`, `RoHa`)!
  - Cross-joining `STORY.actor` (Jira assignee) with `PR.actor` (Bitbucket PR author) on the **same ticket key** proves these are the exact same humans:
    ```text
    MaPa  (407 Jira stories,    0 BB events) <-> MaPa2 (0 Jira stories, 530 BB PRs / 6,559 BB events): 222 shared stories (479 PRs)!
    MaRa  (195 Jira stories,    0 BB events) <-> MaRa2 (0 Jira stories, 271 BB PRs / 4,519 BB events): 102 shared stories (261 PRs)!
    JaKu  ( 55 Jira stories,    0 BB events) <-> JaKu4 (0 Jira stories,  59 BB PRs / 1,163 BB events):  40 shared stories ( 54 PRs)!
    MaPo  ( 20 Jira stories,    0 BB events) <-> MaPo6 (0 Jira stories,  25 BB PRs / 1,280 BB events):  15 shared stories ( 20 PRs)!
    AlKr  ( 10 Jira stories,    0 BB events) <-> AlKr2 (0 Jira stories,   9 BB PRs /   158 BB events):   6 shared stories (  7 PRs)!
    BeWo  ( 61 Jira events,     0 BB events) <-> BeWo2 (0 Jira events,    2 BB PRs /   580 BB events)
    DaHa  ( 16 Jira events,     0 BB events) <-> DaHa4 (0 Jira events,    1 BB PR  /    74 BB events)
    ```
  - **User-visible impact across the app:**
    1. **`WHO IS WHO` legend (`01-overview-1q-dark.png`):** Lists `MaPa STORY OWNER`, `MaRa STORY OWNER`, `JaKu STORY OWNER` right above `MaPa2 AUTHOR`, `MaRa2 AUTHOR`, `JaKu4 AUTHOR`, `MaPo6 REVIEWER`, `AlKr2 AUTHOR`, `BeWo2 REVIEWER`, `DaHa4 REVIEWER` — presenting the team's top 4 developers after `LaNo`/`FuSu`/`MiMe` as two separate people each!
    2. **Scope filter (`#scope`):** Selecting `person MaPa` shows 1,086 Jira events and **0 PRs / 0 queue lanes** (`"No stream matches scope MaPa"` on Queue; `0` rows on Delivery), whereas selecting `person MaPa2` shows 3,253 Bitbucket events and **0 owned Jira stories** on Aging!
    3. **Calendar & cycle pricing:** `court.business_hours(story.actor, ...)` prices story segments on `MaPa`'s calendar while PR intervals are priced on `MaPa2`'s calendar, and on `#ticket/DEMO-58340` the story assignee is `MaPa` while its open PR `#11093` is authored by `MaPa2`.
  - *(Note on `BuMa`/`MaBu` and `ClLa`/`LaCl`: As stated in the prompt, `BuMa` (7 BB events) / `MaBu` (1 Jira + 10 BB events, plus `MaBu2` with 2 Jira events) and `ClLa` (4 Jira events) / `LaCl` (6 BB events) are known design splits and are not counted as part of this defect.)*
- **Expected:** Remap the suffixed Bitbucket pseudonyms (`MaPa2` → `MaPa`, `MaRa2` → `MaRa`, `JaKu4` → `JaKu`, `MaPo6` → `MaPo`, `AlKr2` → `AlKr`, and verify `BeWo2` → `BeWo` / `DaHa4` → `DaHa`) in the store and in the shared `identity.json` map so each engineer has one unified pseudonym across Jira and Bitbucket.

---

#### [F-02] BUG · Scope Filter & Person Validation (`app/main.js:2936, 3074`) · 102 valid people (75 actors outside the 40k event cap + 27 reviewers in `event_objects`) are missing from `#scope` and rejected by `setScope()` — including people visibly shown on the active `1q` Overview, Queue, and Aging views
- **Severity:** BUG (Broken interaction / valid person scopes rejected as non-existent)
- **View:** Top bar `#scope`, Queue (`?tenant=demo-tlla&view=queue&tf=1q`), Overview (`?tenant=demo-tlla&view=overview&tf=1q`), Aging (`?tenant=demo-tlla&view=delivery&tab=aging&tf=all`).
- **Screenshots:** `/tmp/opencode/qa-v017/shots/01-overview-1q-dark.png`, `/tmp/opencode/qa-v017/shots/04-queue-1q-dark.png`.
- **Repro:**
  1. Open `http://127.0.0.1:8799/?tenant=demo-tlla&view=overview&tf=1q`.
  2. Look at the **`WHO IS WHO`** legend at the bottom of Overview: it displays chips for **`DmMa STORY OWNER`**, **`IvZi STORY OWNER`**, **`JoEr STORY OWNER`**, **`MaOr STORY OWNER`**, and **`MaPf STORY OWNER`** (because they own open stories from 2022–2024 that still intersect the `1q` window).
  3. Open the `#scope` dropdown in the top bar and look for `person DmMa` (who has **2,647 events** in the store!) or `person IvZi` (**777 events**), or run `window.__megaspeed.setScope('person', 'DmMa')`.
  4. Now switch to **Queue** (`?tenant=demo-tlla&view=queue&tf=1q`). The `1q` Queue chart draws a stream for **`LeMi2`** (`reviewer`, added as reviewer on PRs `#10405` and `#10417` in Aug 2026). Click the **`LeMi2`** stream on the Queue chart to scope to `LeMi2`.
- **Observed:**
  - Clicking `LeMi2` on the `1q` Queue chart (or scoping to `DmMa`, `IvZi`, `JoEr`, `MaOr`, `MaPf`) **fails and resets the scope to `everything`**, flashing the amber stale status:
    > **`that scope has no events in this dataset — showing everything`**
  - And navigating via deep link `?tenant=demo-tlla&scope=person:DmMa` prints the error banner:
    > **`Scope “person DmMa” does not exist in dataset demo-tlla — showing everything.`**
    even though `DmMa` has **2,647 events**, 67 stories, and 85 PRs in `demo-tlla`!
  - **Root cause (two bugs in `app/main.js:2935-2945` and `3071-3075`):**
    1. **`state.events` is capped at the newest 40,000 events (`ts >= 2025-05-20`):** On the combined 76,213-event store, initial load only fetches `40,000` events (`EVENT_MAX = 40000`). Both `buildScopeOptions()` (`for (const event of state.events)`) and `validateScope()` (`!state.events.some((event) => event.actor === state.scope.value)`) check **only `state.events`**. Consequently, **75 actors** whose events are all prior to `2025-05-20` (including `DmMa` with 2,647 events last active `2025-05-06`, and `IvZi` with 777 events) are treated as if they do not exist in the dataset until the user goes to Timeline and clicks `Load the full range · 40k of 76.2k events`!
    2. **People who are only linked targets (`event_objects` / `STORY.actor`) never have `event.actor === person`:** **27 `PERSON` objects** (such as `LeMi2`, `MaHe4`, `TeAl2`, `ErOt`, `PuAl`) were added as reviewers (`reviewer_changed` links to `PERSON:LeMi2` in `event_objects`) and accrue review wait on the Queue chart and Verdict holders list, but never triggered an event as `events.actor`. Even after loading all 76,213 events, `validateScope()` rejects all 27 of them!
- **Expected:** Build and validate person scopes against the store's full human roster — i.e., `state.objects` where `object_type === 'PERSON'` (or `STORY.actor`), plus `state.queues.streams`, `state.aging.rows` holders, and `state.events` — rather than only `state.events.some(e => e.actor === value)` over the 40k-capped client event slice.

---

#### [F-03] BUG · Multi-Key PR Attribution (`analysis/ballcourt.py:2378`, `analysis/aging.py:611`, `analysis/verdict.py:472`) · Open PRs with 2 ticket keys (`#11030` with Epic `DEMO-56715` + Story `DEMO-57507`; `#11096` with open Story `DEMO-60130` + 2023-Done Story `DEMO-9714`) get contradictory ticket labels across views and link to an Epic page with `0` PRs
- **Severity:** BUG (Cross-source join inconsistency & dead-end drill-down on multi-key PRs)
- **View:** Delivery list → Aging (`PR review` / `PR cycle`), Delivery list → `All 8`, Overview (`#verdict` & `Done in Jira, PR open`), and Ticket detail (`#ticket/DEMO-56715`, `#ticket/DEMO-57507`, `#ticket/DEMO-9714`, `#ticket/DEMO-60130`).
- **Screenshots:** `/tmp/opencode/qa-v017/shots/01-overview-1q-dark.png`, `/tmp/opencode/qa-v017/shots/06-delivery-aging-pr-review-dark.png`, `/tmp/opencode/qa-v017/shots/10-ticket-epic-56715-light.png`.
- **Repro:**
  1. Open **Overview** (`?tenant=demo-tlla&view=overview&tf=1q`).
     - In `#verdict`, look at `THE LONGEST CURRENT WAITS`: item 1 is **`#11030 DEMO-57507 5 d 0 h`**.
     - Expand or inspect `constraint.items` in `/api/verdict?tenant=demo-tlla&days=90`: it lists **`DEMO-60130` (`pr: "#11096"`)**, while `holders.current` and `adjacent.unmerged` in the **exact same `/api/verdict` response** list **`pr: "#11096", ticket: "DEMO-9714"`**!
  2. Open **Delivery list** → **`Aging`** tab and switch `Stage` to **`PR review`** (`06-delivery-aging-pr-review-dark.png`).
     - Look at row 2 (`#11030`): its ticket link is **`DEMO-56715 #11030`** (and row 8 `#11096` is **`DEMO-60130 #11096`** with state **`live`** instead of **`unmerged`**!).
  3. Click **`DEMO-56715`** on that `#11030` Aging row.
- **Observed:**
  - Clicking `DEMO-56715` from the `#11030` PR row opens **`EPIC DEMO-56715`** (`10-ticket-epic-56715-light.png`), which lists 17 child stories and **0 pull requests** — `#11030` is nowhere on the page it linked to!
  - Meanwhile, on **Delivery → `All 8`** and **Overview → `Needs attention`**, `#11030` is labelled **`DEMO-57507 #11030`**, and `#11096` is labelled **`DEMO-9714 #11096`**!
  - **Root cause (three conflicting multi-key selection rules):**
    1. **Epic keys are not filtered out of `story_tickets` using `EPIC` objects:** In `analysis/ballcourt.py:2209-2231, 2378`, `_parent_keys()` only uses the `>= 25% of PRs` heuristic (built for `DEL-1`). On `demo-tlla`, 31 PRs directly name one of 4 real `EPIC` objects (`DEMO-1968` on 24 PRs, `DEMO-56715` on 3 PRs, `DEMO-25967` on 3 PRs, `DEMO-36960` on 1 PR). Because none reaches 25% of 2,574 PRs (`644` PRs), `_parent_keys()` returns `set()`, and `story_keys` on `#11030` stays `['DEMO-56715', 'DEMO-57507']` with the Epic key at `[0]`!
    2. **First-key (`[0]`) vs Last-key (`[-1]`) disagreement between Backend and Frontend:**
       - `analysis/aging.py:611` takes `(meta.get("story_tickets") or [None])[0]` → picks **`DEMO-56715`** (the Epic, which is not in `data.stories`, so `story = None` and `#11030` is marked `live` instead of `unmerged`!) for `#11030`, and picks **`DEMO-60130`** (the open story, so `#11096` is also marked `live` instead of `unmerged`!) for `#11096`.
       - `analysis/verdict.py:472` (`_ticket_of`) and `app/tickets.js:46` (`ownTicket`) take `candidates[candidates.length - 1]` (`keys[-1]`) → picks **`DEMO-57507`** for `#11030` and **`DEMO-9714`** for `#11096`!
    3. **Old closed ticket key on a new PR (`#11096`):** `PR #11096` was opened on `2026-11-20` for the new open story `DEMO-60130` (created `2026-11-20`), but also mentioned `DEMO-9714` (closed `2023-05-24`, 885 business days earlier). Because `DEMO-9714` happens to sort after `DEMO-60130` alphabetically (`'DEMO-60130' < 'DEMO-9714'`), `ticket_keys` is `['DEMO-60130', 'DEMO-9714']`, causing `ownTicket` (`keys[-1]`) to label `#11096` under the 2023 ticket `DEMO-9714` instead of its active story `DEMO-60130`!
- **Expected:**
  1. Exclude known `EPIC` object keys (`SELECT ticket_keys[1] FROM objects WHERE object_type = 'EPIC'`) from `story_tickets` on the server and include `EPIC` objects in `epicKeySet` on the client, so an Epic key like `DEMO-56715` is never chosen as a PR's `story_ticket` when a `STORY` key (`DEMO-57507`) is present. And when a PR *only* names an Epic key (e.g. `#10865`, `#10803` on `DEMO-56715`), list those PRs on the Epic detail page (`_epic()` in `analysis/tickets.py`) so they are not orphaned.
  2. Use one consistent rule for choosing a PR's primary story key across `aging.py`, `verdict.py`, and `tickets.js` (preferring an open/recent `STORY` key over a years-old closed story when multiple `STORY` keys are present).

---

### Area B — Misleading Copy, Windowing & Metric Contradictions (PAPERCUTs)

#### [F-04] PAPERCUT · Timeline Forecast Card (`app/main.js:3450` & `server/app.py:1926`) · The UI always calls `/api/forecast` without `days`, evaluating CV over all 260 weeks since 2021 (`CV 0.87` → refused as `"erratic"`) even when the user is viewing `1q` (`90d`, where `CV = 0.45` and the forecast succeeds!)
- **Severity:** PAPERCUT (Timeframe-insensitive refusal caused by 2021–2023 empty ramp-up weeks)
- **View:** Timeline → `#forecast-card` (`?tenant=demo-tlla&view=timeline&tf=1q` and `tf=120d`).
- **Screenshot:** `/tmp/opencode/qa-v017/shots/02-timeline-1q-dark.png`.
- **Repro:**
  1. Open **Timeline** on `demo-tlla` with `TIMEFRAME = 1q` (`24 Aug → 22 Nov`).
  2. Read the **FORECAST** card above `#sprint-strip`.
  3. Compare with `curl "http://127.0.0.1:8799/api/forecast?tenant=demo-tlla&target=sprint:18568&days=90"`.
- **Observed:**
  - On screen at `1q`, `#forecast-card` refuses to forecast:
    > **`Throughput is erratic (CV 0.87): the range would be so wide it would look like a forecast and behave like a guess.`**
  - Why is `CV = 0.87`? Because `loadForecast()` (`app/main.js:3450`) calls `/api/forecast?target=sprint:18568` **without `days`**, and in `server/app.py:1926`, `insights_rules.window_for(court, _days(None), jira)` defaults to **`90` days only when `jira` is empty**, and uses the **entire 5-year store (`1880.6` days / `260` weeks, `2021-09-28 → 2026-11-22`)** whenever `jira` is present!
  - In `demo-tlla`, the first 75 weeks (`2021-10` to `2023-04`) have almost all `0` story deliveries (`[2, 0, 0, ..., 0]`), which inflates the all-time 260-week CV to `0.87`.
  - Meanwhile, when `/api/forecast` is queried for `days=90` (`1q`, 12 weeks, `CV = 0.45 <= 0.60`) or `days=120` (16 weeks, `CV = 0.51 <= 0.60`), it **succeeds (`available: true`)** and produces **`"6–16 of 41 · by 27 Nov (P50 11 · 0 % chance all 41)"`**!
- **Expected:** Either pass the active timeframe (`scopedQuery()`, or a trailing rolling calibration window such as the last 12–26 active weeks) to `/api/forecast`, or if the all-time window is intentional, state `"over 260 weeks since Sep 2021 (CV 0.87)"` in the refusal sentence so the reader understands why a stable `1q` team (`Trend` reports `WIP is stable`) is refused as erratic.

---

#### [F-05] PAPERCUT · Overview Verdict Card (`app/verdict.js:330` & `analysis/verdict.py:394`) · `Next: verification 634 h · refinement ≈2.3k h · starting work ≈1.8k h` claims stations are ranked by removable pool, but lists `634 h` ahead of `2,312 h` and `1,817 h`
- **Severity:** PAPERCUT (Self-contradictory ranking claim in `#verdict` footer)
- **View:** Overview → `#verdict` → `[data-next-constraint]` (`?tenant=demo-tlla&view=overview&tf=1q`).
- **Screenshot:** `/tmp/opencode/qa-v017/shots/01-overview-1q-dark.png`.
- **Repro:**
  1. Open **Overview** (`1q`) and look at the `<details data-next-constraint>` line at the bottom of `#verdict`.
  2. Click to expand `Next: verification 634 h · refinement ≈2.3k h · starting work ≈1.8k h` and read the table and its caption.
- **Observed:**
  - The summary line reads:
    > **`Next: verification 634 h · refinement ≈2.3k h · starting work ≈1.8k h`**
  - Expanding it shows:
    - `Verification`: `removable = 634 h`, `owed now = 8 h`, `tail = 626 h`
    - `Refinement`: `removable = ≈2.3k h`, `owed now = 0 h`, `tail = ≈2.3k h`
    - `Starting work`: `removable = ≈1.8k h`, `owed now = 0 h`, `tail = ≈1.8k h`
    - `Sprint planning`: `removable = ≈1.5k h`, `owed now = 0 h`, `tail = ≈1.5k h`
  - Immediately below the table, the caption claims:
    > **`Every station's removable pool, ranked by the same clock and method as the constraint.`**
  - Because `prd/67` W-04 changed `rank_pools()` (`analysis/verdict.py:394`) to rank by **owed now (`live_hours`) first** and `removable_hours` second (`Verification` has `8 h` owed now vs `0 h` for `Refinement`), the summary line prints only `removable_hours` (`634 h` before `≈2.3k h`), which looks mis-sorted until the user expands the table and notices `owed now = 8 h`.
- **Expected:** In `nextLine()` (`app/verdict.js:339`), include the owed-now amount when non-zero (e.g. `verification 8 h owed now (634 h removable) · refinement 0 h owed (≈2.3k h history)`) and update the `.panel-note` caption to state that stations are ranked by **hours owed now first**, then by removable pool.

---

#### [F-06] PAPERCUT · Overview Verdict Card (`analysis/verdict.py:485` & `app/verdict.js:136`) · `HOLDERS PR clock` inside `#verdict` sums all-time 5-year PR review hours (`MaRa2 ≈4.6k h · peers 0–4624 h`) instead of the active timeframe window (`1w` / `4w` / `1q`), and includes `" · peers 0–4624 h"` when the named person is a peer rather than a sentinel
- **Severity:** PAPERCUT (Unwindowed all-time total inside a windowed verdict card + redundant peer range)
- **View:** Overview → `#verdict` (`?tenant=demo-tlla&view=overview&tf=1w`, `4w`, `1q`, `all`).
- **Screenshot:** `/tmp/opencode/qa-v017/shots/01-overview-1q-dark.png`.
- **Repro:**
  1. Open **Overview** on `demo-tlla` and switch the timeframe between **`4w`**, **`1q`**, and **`All`**.
  2. Look at the 4th stat in `#verdict`: **`HOLDERS PR clock`**.
- **Observed:**
  1. **Unwindowed 5-year sum across every timeframe:** At `4w` (`29` completed stories, `1,353 h` review history), `1q` (`88` completed stories, `3,729 h` review history), and `All` (`1,143` completed stories), `HOLDERS PR clock` displays the **exact same 5-year all-time number**:
     > **`MaRa2 ≈4.6k h · peers 0–4624 h`**
     Because `holders(court, delivery_rows)` (`analysis/verdict.py:475-505`) loops over all `court.prs` and all `state.intervals` without filtering by `window`, `MaRa2`'s 4,623.7 hours from 2022–2026 are displayed next to `4w` and `1q` story pools (`62 h` owed now, `≈1.4k h` tail), making the holder number larger than the entire `4w` and `1q` review pools!
  2. **Self-referential peer range when there is no sentinel:** On `demo-tlla`, `sentinels` is empty (`[]`). In `app/verdict.js:136-144`, `sentinel` falls back to `(holders.people || [])[0]` (`MaRa2`, who is a peer with `4623.7 h`), and then prints `MaRa2 ≈4.6k h · peers 0–4624 h` — comparing `MaRa2` against a peer range whose upper bound (`4624 h`) is `MaRa2` himself!
- **Expected:** Scope `holders(court, delivery_rows, window)` in `analysis/verdict.py` to the same `window` as the verdict card (or label it `"all-time"`), and when the store has no sentinel, label the top holder as `top reviewer MaRa2 ...` with `other reviewers 0–... h` (excluding the top reviewer from the comparison range).

---

#### [F-07] PAPERCUT · Timeline Queue Lanes (`app/timeline.js:1079`) · Switching Timeline `lanes` to `queues` labels the two lanes `review · 60.9k h` and `author · 990 h` in raw 24/7 wall-clock hours (`(seg.to - seg.from) / HOUR`), contradicting the business-hours clock (`1 d = 8 h`) and the `LIFECYCLE` legend right above it (`in review 2036 calendar days`)
- **Severity:** PAPERCUT (Unit/clock contract violation: wall-clock hours printed as bare `h` under `business hours · 1 d = 8 h`)
- **View:** Timeline (`?tenant=demo-tlla&view=timeline&tf=1q` or `tf=all`, `lanes = queues`).
- **Screenshot:** `/tmp/opencode/qa-v017/shots/03-timeline-queue-all-dark.png`.
- **Repro:**
  1. Open **Timeline** on `demo-tlla` and change the **`lanes`** dropdown from `actors` to **`queues`**.
  2. Compare the lane labels (`review · 60.9k h`, `author · 990 h`) with the `LIFECYCLE` legend directly above the canvas (`waiting on author 19 calendar days`, `in progress 350 calendar days`, `in review 2036 calendar days`) and with the **Queue** page (`All` timeframe: `review` queue total wait is `14,757.8` business hours).
- **Observed:**
  - `queueHours(queue)` (`app/timeline.js:1079-1088`) sums raw 24-hour wall-clock milliseconds across all 2,574 PRs (`(seg.to - seg.from) / HOUR`, including nights, weekends, and holidays) and formats them in `laneRows()` (`line 852`) as **`review · 60.9k h`** and **`author · 990 h`**.
  - Per `concepts.md §1` (`prd/35`, enforced in `app/format.js:7-11`), bare `h` / `d` is reserved for **business hours**, while wall-clock spans on the timeline must use `fmtCalendarDuration` / `"calendar days"` (which the `LIFECYCLE` legend 30px above it does: `2036 calendar days`, i.e., `60,881 / 24`!). Printing `60.9k h` on the `review` queue lane makes it look 4× larger than the store's actual 5-year business-hour review wait (`14.8k h`).
- **Expected:** Format the Timeline queue lane span using `fmtCalendarHours(hours)` (or `fmtCalendarDuration`, e.g., `review · 2537 calendar days`) so wall-clock canvas sums never masquerade as business-hour `h` totals.

---

#### [F-08] PAPERCUT · Top Bar Status & Provenance (`app/main.js:522, 3245`) · Top bar simultaneously says `Snapshot from 20 h ago: 76213 events` on the left and `live · 40000 events · demo-tlla` on the right on every view
- **Severity:** PAPERCUT (Contradictory event counts in the persistent top chrome)
- **View:** Top bar across all 9 views (`?tenant=demo-tlla`).
- **Screenshots:** `/tmp/opencode/qa-v017/shots/01-overview-1q-dark.png`, `/tmp/opencode/qa-v017/shots/07-rules-1q-light.png`.
- **Repro:**
  1. Open any view on `demo-tlla` at desktop width (`1600×1000`).
  2. Compare `#provenance`, `#nav-count`, `#tenant-button`, and `#status` in the top-right corner.
- **Observed:**
  - `#tenant-button` says: **`demo-tlla 2574 PRs · 76213 events`**
  - `#nav-count` says: **`76213 events · 2574 PRs`**
  - `#provenance` says: **`demo-tlla · Snapshot from 20 h ago: 76213 events, 2574 PRs in the dataset, 16605 commits`**
  - `#status` (top right) says: **`live · 40000 events · demo-tlla`**
  - On every page except Timeline (where the `Load the full range · 40k of 76.2k events` button is visible), there is no indication of why `#status` says `40000 events` while three other labels on the same screen say `76213 events` (and all server-computed pages — Overview, Queue, Delivery, Aging, Trend, Rules, Policy, Ticket — actually analyze all **76,213** events!).
- **Expected:** In `liveStatusText()` (`app/main.js:522`), either report `meta.events` (`76213 events`) on server-backed views and `40k of 76.2k events` when `state.eventCapped` is true on Timeline, or write `live · 40k/76.2k events loaded · demo-tlla`.

---

#### [F-09] PAPERCUT · Aging PR Table vs. Delivery List & Ticket Detail (`analysis/aging.py:639`, `app/tables.js:64`, `analysis/tickets.py:1248`) · Open PRs with no assigned reviewers (`#11030`, `#11088`, `#11095`) or already reviewed (`#11093`) show `HOLDER = —` on Aging, `WAITING ON = review queue` / `ready to merge` on Delivery, and `"waiting 5 d 0 h on nobody"` on Ticket detail
- **Severity:** PAPERCUT (Contradictory holder attribution across three views for the same 4 open PRs)
- **View:** Delivery list → `Aging` (`Stage = PR review` / `PR cycle`), Delivery list → `All 8`, and Ticket detail (`#ticket/DEMO-57507`, `#ticket/DEMO-58509`, `#ticket/DEMO-58340`, `#ticket/DEMO-60089`).
- **Screenshots:** `/tmp/opencode/qa-v017/shots/05-delivery-all-1q-dark.png`, `/tmp/opencode/qa-v017/shots/06-delivery-aging-pr-review-dark.png`, `/tmp/opencode/qa-v017/shots/09-ticket-story-57507-light.png`.
- **Repro:**
  1. Open **Delivery list** → **`All 8`** (`05-delivery-all-1q-dark.png`) and inspect `#11030`, `#11088`, `#11093`, `#11095`.
     - `#11030`, `#11088`, `#11095` show `WAITING ON = review queue`.
     - `#11093` shows `STATUS = ready to merge`, `WAITING ON = ready to merge`.
  2. Switch to the **`Aging 8`** tab and select `Stage = PR review` (`06-delivery-aging-pr-review-dark.png`).
     - All 4 of those PRs (`#11030`, `#11088`, `#11093`, `#11095`) show **`HOLDER = —`** (`holder: []`)!
     - Moreover, `#11093` (where `LaNo` already completed review via `reviewer_changed: reviewed` and `ball = "ready"`) still shows `STAGE = review` on Aging!
  3. Click `#11030` / `DEMO-57507` to open `#ticket/DEMO-57507` (`09-ticket-story-57507-light.png`).
     - Both the header pill and `#ticket-answer` print:
       > **`PR #11030 open · waiting 5 d 0 h on nobody`**
       > **`PR #11030 (waiting 5 d 0 h on nobody).`**
- **Observed:**
  - When an open PR has no human in `latest.person_holders` (because no reviewer was added to the roster yet on `#11030`/`#11088`/`#11095`, or because the reviewer finished and the PR is ready to merge on `#11093`):
    - **Delivery (`All 8`)** prints `review queue` / `ready to merge`.
    - **Aging (`PR review` / `PR cycle`)** calls `_holder_block(court, [state], age)` without a fallback (`analysis/aging.py:639`), leaving `holder: []` (`—`) on 4 of the 8 open PRs (and making `role:review` or `person:*` scopes unable to match `#11093`'s author `MaPa2` who needs to merge it).
    - **Ticket detail** (`analysis/tickets.py:1248` and `app/ticket.js:648`) falls back to `who = ... || 'nobody'`, printing the oxymoron **`"waiting 5 d 0 h on nobody"`**!
- **Expected:**
  1. On Ticket detail (`analysis/tickets.py:1248` and `app/ticket.js:648`), replace `'nobody'` with the ball/queue label (`'a reviewer to be assigned'` / `'the review queue'`, or `'merge (author ' + pr.author + ')'` when `ball === 'ready'`).
  2. On Aging (`_pr_rows` in `analysis/aging.py:639`), when `latest.ball == "ready"`, attribute the holder to the PR author (`role: "merge"` or `"author"`), and when no reviewer is assigned, display `review queue (unassigned)` instead of `—`.

---

#### [F-10] PAPERCUT · Rules (`analysis/insights.py` & `app/rules.js`) · Neither the Rules header nor the `Comment tax` / `Questions` metrics disclose that all 11,885 classified review threads carry `classifier_version: "mock-v1"` (placeholder hash labels, not LLM classifications)
- **Severity:** PAPERCUT (Undisclosed `mock-v1` placeholder classifier on thread-classification surfaces)
- **View:** Rules (`?tenant=demo-tlla&view=rules&tf=1q` and `tf=all`) and Sources (`?tenant=demo-tlla&view=sources`).
- **Screenshot:** `/tmp/opencode/qa-v017/shots/07-rules-1q-light.png`.
- **Repro:**
  1. Open **Rules** on `demo-tlla` (`1q` or `All`).
  2. Read `#honesty`:
     > **`286 PRs opened in the window are the cohort · 1837 classified threads · impact is an estimate (≈) · business hours (1 d = 8 h)`**
  3. Inspect `/api/insights?tenant=demo-tlla&days=90` (`metrics.comment_tax`) and `/api/health?tenant=demo-tlla`.
- **Observed:**
  - None of the UI views (`Rules`, `Sources`, `Ticket detail`) mentions that the store's `11,885` classified `THREAD` objects are stamped with `classifier_version: "mock-v1"` (while `ingest_runs.classifier_version` only records `"deterministic-v2"` for the event layer).
  - Even worse, in `/api/insights`, `metrics.comment_tax.outcome_note` states:
    > **`"acted_on means a deterministic signal (the diff moved at the anchor) or a definite classifier read; unresolved includes threads whose outcome could not be determined, so wasted_share is a floor, not a flattering estimate"`**
    Calling `mock-v1` `"a definite classifier read"` in the API payload (and printing `"1837 classified threads"` in `#honesty` without a `mock-v1` badge) gives no warning that the thread labels are synthetic mocks awaiting the LLM relabel patch.
- **Expected:** Expose the thread `classifier_version` (`mock-v1` vs a real model tag) in `/api/health` (for **Sources**) and in `/api/insights` (`metrics.comment_tax`), and when `classifier_version` starts with `mock`, append `(mock-v1 placeholder labels)` to `#honesty` (`"1837 classified threads (mock-v1)"`) and to `comment_tax.outcome_note`.

---

#### [F-11] PAPERCUT · Rules API Payload Size (`server/app.py:1523` / `analysis/insights.py`) · `GET /api/insights?tenant=demo-tlla&days=1882` (`All` timeframe) returns a **12.2 MB** uncompressed JSON payload containing all 11,885 thread objects, 2,320 worklog story rows, and 2,081 yield story rows even when the UI renders at most 10–25 drill-down rows
- **Severity:** PAPERCUT (12.2 MB JSON payload on `Rules` → `All` timeframe causes multi-second UI lag on timeframe switch)
- **View:** Rules (`?tenant=demo-tlla&view=rules&tf=all`).
- **Repro:**
  1. Run `curl -s -w "%{size_download} bytes in %{time_total}s\n" -o /dev/null "http://127.0.0.1:8799/api/insights?tenant=demo-tlla&days=1882"`.
  2. In the browser on **Rules**, click **`All`** in the timeframe bar and observe the delay before `#first-pass-yield` and `#rules-list` update.
- **Observed:**
  - `GET /api/insights?tenant=demo-tlla&days=1882` transfers **12,468,756 bytes (12.2 MB)** of JSON (`1.91 MB` at `1q`, `2.20 MB` at `120d`).
  - Why is `/api/insights` 12.2 MB? Because `metrics.comment_tax.items` serializes all **11,885** `THREAD` objects (`~2.4 MB`), `metrics.rework_loops` / `conformance` / `pr_size` / `cycle_time` / `flow_efficiency` each serialize all **2,574** PRs repeatedly across every metric block, and `metrics.worklog_cross_check.stories` / `waiting_time_decomposition.stories` / `carry_over.stories` serialize thousands of story dicts — even when a metric (`comment_tax`, `pr_size`, `worklog_cross_check`) did **not** fire as a suggestion, and even though `app/rules.js` (`DRILL_LIMIT`) only ever renders the top rows in a `<details>` drill-down!
- **Expected:** Cap per-metric drill-down arrays (`items` / `stories` / `prs`) in `/api/insights` to a reasonable limit (e.g. top 100 affected items + total count) or omit full item arrays for metrics that did not fire a suggestion, reducing the `All` payload from `12.2 MB` to `< 300 KB`.

---

### Area C — Visual, Formatting & Copy Polish (POLISH)

#### [F-12] POLISH · Timeline Top-Bar Date Range & Sources Store Span (`app/format.js:26, 60`, `app/main.js:2515, 3179`, `app/timeline.js:1417`) · Multi-year ranges omit the year (`"28 Sep → 21 Nov"` for a 2021→2026 span!), `All` on Timeline shows a future end date `"28 Sep → 14 Dec"`, and sprint band label `"S55…"` overlaps `"S5800"` at `All` zoom
- **Severity:** POLISH (Ambiguous yearless multi-year dates + padded future end date + 1 canvas label overlap at `All`)
- **View:** Sources (`?tenant=demo-tlla&view=sources`) and Timeline (`?tenant=demo-tlla&view=timeline&tf=all`).
- **Screenshots:** `/tmp/opencode/qa-v017/shots/03-timeline-queue-all-dark.png`, `/tmp/opencode/qa-v017/shots/11-sources-dark.png`.
- **Repro:**
  1. Open **Sources** (`11-sources-dark.png`) and read **`store span`**.
  2. Open **Timeline**, click **`All`** in the timeframe bar (`03-timeline-queue-all-dark.png`), and read `#range` next to `All` and the sprint header row on the canvas.
- **Observed:**
  1. **5-year span printed without years:** On Sources (`app/main.js:2515`) and in the top bar `#range` on `All` (`app/format.js:60`), `fmtDay()` formats `2021-09-28` → `2026-11-21` as:
     > **`28 Sep → 21 Nov (every event the store holds)`**
     Because `fmtDay()` omits the year even when `start` and `end` are in different years (or > 365 days apart), a 5-year store (`Sep 2021 → Nov 2026`) reads like a 54-day autumn window!
  2. **`All` on Timeline shows `"28 Sep → 14 Dec"` (22 days in the future):** When `All` is clicked on Timeline, `applyTimeframeToTimeline()` (`app/main.js:3179`) calls `timeline.fit()`, which sets `timeline.view` to `timeline.domain`. Because `timeline.domain` adds `1.5%` padding on each side of the 5-year span (`0.015 × 1,880 days ≈ 28 days`, `app/timeline.js:582`), `view.end` becomes **`2026-12-14`** (22 days after the `2026-11-22` snapshot!), so the top bar `#range` displays **`28 Sep → 14 Dec`** on Timeline `All`, but **`28 Sep → 22 Nov`** on every other page!
  3. **Minor sprint label collision at 5-year `All` zoom:** In `03-timeline-queue-all-dark.png`, on the sprint header row at `All` zoom, truncated **`S55…`** slightly touches/overlaps **`S5800`**. (At `1q`, `4w`, and `1w`, all sprint band labels are completely clean and non-overlapping.)
- **Expected:** In `fmtRange(start, end)` and `renderSourcesPage()`, use `fmtDayYear()` (`"28 Sep 2021 → 21 Nov 2026"`) whenever `span > 180 * DAY` or the UTC years differ; display the unpadded data range in `#range` on `All`; and tighten the minimum pixel width before drawing a truncated `"S55…"` sprint label at multi-year zoom.

---

#### [F-13] POLISH · Desktop Delivery List (`Aging` tab, `Stage = PR review` / `PR cycle`, `app/style.css:2748`) · `.aging .c-id { width: 144px }` truncates every PR number and repo badge to **`DEMO-58511 #1…`**
- **Severity:** POLISH (Desktop table cell ellipsis hides PR numbers on PR Aging views)
- **View:** Delivery list → `Aging` tab → `Stage = PR review` or `PR cycle` (`?tenant=demo-tlla&view=delivery&tab=aging&tf=1q`) at `1600×1000`.
- **Screenshot:** `/tmp/opencode/qa-v017/shots/06-delivery-aging-pr-review-dark.png`.
- **Repro:**
  1. On desktop (`1600×1000`), open **Delivery list** → **`Aging`** tab.
  2. Change `Stage` from `Story cycle` to **`PR review`** or **`PR cycle`**.
  3. Look at the **`TICKET / PR`** column (`.c-id`).
- **Observed:**
  - `.aging .c-id` has `width: 144px` (`app/style.css:2748`), which fits a bare Jira story key (`DEMO-58511`) on `kind=story`, but when `kind=pr` appends the 5-digit PR number and repo badge (`DEMO-58511 #11021 widget`), **every single row** truncates the PR number to **`#1…`**:
    - `DEMO-58511 #1…` (hiding whether it is `#11021` or `#11091`, which are both on `DEMO-58511`!)
    - `DEMO-56715 #1…`
    - `DEMO-59919 #1…`
- **Expected:** Widen `.aging .c-id` from `144px` to `220px` (or allow `.aging td.c-id` to wrap / take remaining table width) so 5-digit PR numbers (`#11021`, `#11091`) and the repo name are never truncated to `#1…`.

---

#### [F-14] POLISH · Ticket Detail (`Epic` page, `app/ticket.js:617`) · Open child stories on an Epic page render duplicate **`open · open`** state pills
- **Severity:** POLISH (Redundant `"open · open"` text in Epic child-story state pills)
- **View:** Ticket detail for any Epic with open child stories, e.g. `http://127.0.0.1:8799/?tenant=demo-tlla#ticket/DEMO-56715`.
- **Screenshot:** `/tmp/opencode/qa-v017/shots/10-ticket-epic-56715-light.png`.
- **Repro:**
  1. Navigate to `#ticket/DEMO-56715` (`10-ticket-epic-56715-light.png`).
  2. Look at the `STATE` column for the 11 open child stories (`DEMO-58516` … `DEMO-58515`).
- **Observed:**
  - All 11 open child stories render their state pill as:
    > **`• open · open`**
  - **Root cause:** In `app/ticket.js:617`:
    ```js
    <span class="pill ${story.censored ? 'waiting' : 'healthy'}">${esc(story.state || 'unknown')}${story.censored ? ' · open' : ''}</span>
    ```
    When `story.state` is already `'open'` and `story.censored` is `true`, it appends `' · open'` without checking `story.state !== 'open'`! (Notice that `epicMarkdown()` at `app/ticket.js:762` already has the guard `story.censored && story.state !== 'open' ? ' · open' : ''`, whereas `epicMarkup()` at line 617 missed `&& story.state !== 'open'`!)
- **Expected:** Add `&& story.state !== 'open'` to `app/ticket.js:617` (matching line 762 and line 692 `' · still open'`).

---

#### [F-15] POLISH · Sources (`app/main.js:2526`) · The ingest-runs table on Sources shows PR columns (`PRS SEEN | PRS KEPT | DROPPED`) with `0 | 0 | 0` for the `jira-cloud` run while omitting `issues_seen` / `issues_kept` (`2,391`) and `events_written` (`33,425` Jira + `42,788` Bitbucket)
- **Severity:** POLISH (Incomplete per-run provenance columns on combined store)
- **View:** Sources (`?tenant=demo-tlla&view=sources`).
- **Screenshot:** `/tmp/opencode/qa-v017/shots/11-sources-dark.png`.
- **Repro:**
  1. Open **Sources** on `demo-tlla` (`11-sources-dark.png`).
  2. Look at the per-source ingest runs table.
- **Observed:**
  - The table renders:
    ```text
    SOURCE       SNAPSHOT                PRS SEEN   PRS KEPT   DROPPED
    jira-cloud   2026-11-22 00:14 UTC           0          0         0
    hans         2026-11-22 05:00 UTC       10733       2574      8159
    ```
  - Meanwhile, the `ingest_runs` table in DuckDB records `events_written` (`33,425` for `jira-cloud`, `42,788` for `hans`) and `issues_seen: 2391, issues_kept: 2391` for `jira-cloud`. Printing only the PR columns makes the `jira-cloud` run row read `0 / 0 / 0`.
- **Expected:** Include `events_written` (and `issues_kept` or `items kept`) in `/api/health`'s `runs` array and the Sources `.source-runs` table so both the Jira run (`2,391 issues · 33,425 events`) and the Bitbucket run (`2,574 of 10,733 PRs · 42,788 events`) show what they contributed.

---

## 4. Pseudonymisation Contract Audit (`prd/02`)

We audited the DuckDB tables (`objects`, `events`, `event_objects`, `ingest_runs`, `annotations`) and 58 API responses (`/api/health`, `/api/events`, `/api/objects`, `/api/verdict`, `/api/insights`, `/api/queues`, `/api/blocking`, `/api/delivery`, `/api/aging`, `/api/sprints`, `/api/forecast`, `/api/trend`, `/api/simulate`, `/api/ticket/*`) for any violation of `prd/02`:

1. **No raw prose / comment / commit / diff / ticket summary text:** Verified. `objects.attributes` and `events.attributes` contain only closed-vocabulary enums (`comment_type`, `thread_outcome`, `push_class`, `anchor_class`, `severity`, `summary_class`, `from_status`, `to_status`, `delay_bucket`) and numeric counts (`lines_added`, `lines_removed`, `commits_added`, `points`, `hours`).
2. **No e-mail addresses or camelCase/spaced human names:** Verified (`0` matches for email regex `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` across all API responses). All actors are 2-syllable pseudonyms (`LaNo`, `FuSu`, `MiMe`, `MaPa`, `MaPa2`, …) or allowlisted bots (`AgJu`, `BiBi`, `system`).
3. **No `zzcanary` or raw-zone leak (`identity.json`, `.trace.jsonl`, `classify-trace.jsonl`):** Verified (`0` matches across all endpoints and store tables).

---

## 5. Verified Good Surfaces & Regression Check vs. `docs/QA-v014.md`

### Regression Check (`v0.14.0` / `v0.14.1` `prd/67` & `prd/68` items on the combined corpus)
- **Zero regressions** across all 16 `prd/68` findings (`F-01`..`F-16` in `docs/QA-v014.md`) and all 5 `prd/67` window-scoping items (`W-01`..`W-05`):
  - **Queue view (`v014 F-01`):** Renders 24 streams (`1q`) and 19 reviewer response rows cleanly across all timeframes (`1w`, `4w`, `1q`, `all`) and scopes.
  - **Dateless & future sprints (`v014 F-02`):** All 28 event-only sprints render `"unknown · dates unknown"`, clicking `S6596` filters the story lanes to its 1 story, the 4 future sprints render `"future · dates unknown"` without inverted bands, and `S18568` is the active sprint (`41 stories · 64 pts`).
  - **Aging holders & counts (`v014 F-03, F-06, F-08`):** `only=live` shows the 6 live stories with their PR review holders (`CaGi41 (review) + FuSu (review) + JaKu4 (review) + MiMe (review)` on `DEMO-58511`) and renders all **99** window-scoped re-plan rows directly below (`#aging-replan`).
  - **Policy lab (`v014 F-04`):** Calibrates over `286` PRs (`1q`) with `refinement: 6.84 h (n = 133)` enabled, and its validation gate honestly withholds scenario numbers because the baseline median (`14.22 h`) is `+146.9%` above the historical merged-PR median (`5.76 h`, tolerance `±20%`).
  - **Rules carry-over & float days (`v014 F-05`, `W-05`):** Prints `"36 of 115 stories committed in the 6 sprints closed in this 90.0-day window"` beside `"the whole store: 1607 carried over across 99 sprints"`, and formats `1880.6 calendar days` on `All` and `limit days: 1.4 business days` on `stale-prs`.
  - **First-pass yield (`v014 F-07`):** With the PR layer now present, `Reviewed` is measured (`19%` / `11 of 57` at `1q`; `31%` / `351 of 1,131` at `All`), `Rolled %C&A` computes `17%` (`9 / 84` joint clean at `1q`) and `28%` (`309 / 2,081` at `All`), and explicitly notes the stories without a merged PR excluded from the review denominator (`27` at `1q`, `950` at `All`).
  - **Worklog cross-check (`Rules`):** Cross-checks Jira logged hours against Bitbucket active PR hours (`147 d 7 h` logged vs `150 d 4 h` active PR time = `0.98×` at `1q`, `r = 0.51` over `53` stories).
  - **Phone (`412×915`) layout (`v014 F-15`):** `document.documentElement.scrollWidth === 412` (`0px` horizontal overflow) across all 9 views in both Dark and Light themes.

---

## 6. Screenshot Index (`/tmp/opencode/qa-v017/shots/`)

| File | Viewport | Theme | Surface & Evidence Captured |
| --- | --- | --- | --- |
| `01-overview-1q-dark.png` | `1600×1000` | Dark | Overview (`1q`): Constraint Verdict (`Approval gate`, `≈1.5k h` removable, `62 h` owed now on 5 stories), `MaRa2 ≈4.6k h · peers 0–4624 h` all-time holder (`F-06`), `Next: verification 634 h · refinement ≈2.3k h` (`F-05`), `WHO IS WHO` legend with split identities (`MaPa`/`MaPa2`, `MaRa`/`MaRa2`, `JaKu`/`JaKu4` in `F-01`) and unscopable 40k-capped story owners (`DmMa`, `IvZi` in `F-02`), `Done in Jira, PR open` (`2` rows), and `live · 40000 events` vs `76213 events` (`F-08`). |
| `02-timeline-1q-dark.png` | `1600×1000` | Dark | Timeline (`1q`, `actor` lanes): `46` active actor lanes (`72` hidden), `Load the full range · 40k of 76.2k events` button, `FORECAST` card refusing with all-time 260-week `CV 0.87` (`F-04`), and `Sprint 18568 at risk` (`41 stories · 64 pts`). |
| `03-timeline-queue-all-dark.png` | `1600×1000` | Dark | Timeline (`All`, `queue` lanes): Top bar range `28 Sep → 14 Dec` (`F-12`), `review · 60.9k h` and `author · 990 h` wall-clock queue lane labels vs `in review 2036 calendar days` (`F-07`), and `S55…`/`S5800` label touch (`F-12`). |
| `04-queue-1q-dark.png` | `1600×1000` | Dark | Queue (`1q`): 24 WIP streams (including `LeMi2`, whose click-to-scope is rejected by `validateScope()` in `F-02`), 19-person response-time table, and `WHO IS WHO` legend. |
| `05-delivery-all-1q-dark.png` | `1600×1000` | Dark | Delivery list (`All 8` tab, `1q`): All 8 open PRs (`#11021`, `#11030`, `#11088`, `#11093`, `#11069`, `#11091`, `#11095`, `#11096`) and `Done in Jira, PR open` (`DEMO-9714 #11096` and `DEMO-57507 #11030`). |
| `06-delivery-aging-pr-review-dark.png` | `1600×1000` | Dark | Delivery list (`Aging 8` tab, `Stage = PR review`, `1q`): `#11030` attributed to Epic `DEMO-56715` instead of `DEMO-57507` and `#11096` attributed to `DEMO-60130` (`F-03`), `HOLDER = —` on 4 of 8 open PRs (`F-09`), and `.c-id` truncating every PR number to `#1…` (`F-13`). |
| `07-rules-1q-light.png` | `1600×1000` | Light | Rules (`1q`, Light): `First-pass yield` (`92% · 99% · 19% · 98% → ROLLED 17%`, `9 / 84`), `Stability` (`1.2%` reopen/failed-verify/bounce), 4 Exploit rules + 4 next-constraint rules, `PR SIZE` (`748 of 2574 PRs flagged large`), `Logged hours vs story points` (`147 d 7 h` vs `150 d 4 h` active PR time, `0.98×`), and `Story rework` (`22 approved but never merged`). |
| `08-policy-1q-light.png` | `1600×1000` | Light | Policy lab (`1q`, Light): `not validated baseline · ≈1 d 6 h cycle median · 286 PRs calibrated`, red `baseline failed` gate (`+146.9%` vs measured `5 h 46 min`), and calibrated parameters table (`refinement ≈6.84 h`). |
| `09-ticket-story-57507-light.png` | `1600×1000` | Light | Ticket detail (`#ticket/DEMO-57507`, Light): Story joined to 9 PRs (`8` merged, `1` open `#11030`), `32% of the change unmerged` pill, `"PR #11030 open · waiting 5 d 0 h on nobody"` (`F-09`), data-quirk clamp note, and `worklog vs active` (`2 d 7 h` logged vs `10 d 1 h` active PR time). |
| `10-ticket-epic-56715-light.png` | `1600×1000` | Light | Ticket detail (`#ticket/DEMO-56715`, Light): Epic page reached by clicking `DEMO-56715` on `#11030` in Aging (`F-03`), showing 17 child stories, duplicate `open · open` state pills (`F-14`), and 0 pull requests. |
| `11-sources-dark.png` | `1600×1000` | Dark | Sources (`1q`, Dark): Provenance facts (`76213` events, `2574` of `10733` PRs, `16605` commits, `2320` stories, `71` epics, `106` sprints), yearless `store span: 28 Sep → 21 Nov` (`F-12`), and 2 ingest runs (`jira-cloud` with `0/0/0` PR columns vs `hans` in `F-15`). |
| `12-phone-overview-dark.png` | `412×915` | Dark | Phone Overview (`1q`, Dark): `scrollWidth === 412`, responsive Verdict card, tiles, cards, `WHO IS WHO` legend, and `Done in Jira, PR open`. |
| `13-phone-aging-light.png` | `412×915` | Light | Phone Delivery → Aging (`1q`, Light): `scrollWidth === 412`, 6 live stories + 99 re-plan items stacked cleanly. |
| `14-phone-rules-dark.png` | `412×915` | Dark | Phone Rules (`1q`, Dark): `scrollWidth === 412`, responsive First-pass yield waterfall and rule cards. |

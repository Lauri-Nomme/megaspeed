# 70 — v0.17.3 combined-corpus QA fixes (F-01 … F-15)

Source: `docs/QA-v017-combined.md` (free exploration on the **combined** `demo-tlla`
corpus: 76,213 events, 2,574 PRs, 2,320 stories). 15 findings: **3 BUG, 8
PAPERCUT, 4 POLISH**. Conform to `docs/concepts.md` and prd/10; every finding gets
a regression test and a scoped commit.

**F-01 is a data repair, not a code unit** (done in the live store: all
Bitbucket/Jira identity suffix variants merged to one pseudonym, 199 people).
Everything below is code.

## Dispatch map (file-disjoint)

Each unit runs in its **own worktree + branch**, so the units in a wave never
touch the same file. A unit must **not** edit files outside its list; if a fix
needs one, stop and report instead of crossing the boundary.

| unit | findings | files it owns |
|---|---|---|
| **U1 — attribution & holders** | F-03 (aging/tickets side), F-09 | `analysis/ballcourt.py`, `analysis/aging.py`, `analysis/tickets.py`, `app/tables.js`, `app/tickets.js` |
| **U2 — scope & chrome** | F-02, F-04 (client), F-08, F-12 (dates/Sources), F-15 | `app/main.js`, `app/format.js` |
| **U3 — timeline clock & labels** | F-07, F-12 (timeline label overlap) | `app/timeline.js` |
| **U5 — insights honesty & payload** | F-10, F-11 | `server/app.py`, `analysis/insights.py`, `app/rules.js` |
| **U6 — polish** | F-13, F-14 | `app/style.css`, `app/ticket.js` |
| **U4 — verdict card** (wave 2, after U1) | F-03 (verdict side), F-05, F-06 | `analysis/verdict.py`, `app/verdict.js` |
| **U7 — sprint labels** (wave 2, after U2/U3/U5) | F-16 | `ingest/taxonomy.py`, `ingest/jira_pipeline.py`, `analysis/jira.py`, `app/sprints.js`, `app/timeline.js`, `app/format.js`, `app/main.js` |

Wave 1 runs U1/U2/U3/U5/U6 in parallel. U4 waits for U1 because it imports U1's
canonical ticket helper (see F-03).

## U1 — attribution & holders

### F-03 — one canonical primary ticket per PR
`analysis/ballcourt.py` currently strips a key from **story_tickets** only when it
appears on ≥25 % of PRs (`_parent_keys`), so an EPIC key can stay at index 0;
`analysis/aging.py` takes `story_tickets[0]`, while `analysis/verdict.py` takes
`keys[-1]` and `app/tickets.js` another order. `#11030` = `['DEMO-56715' (EPIC),
'DEMO-57507' (STORY)]` links to the **Epic page with 0 PRs**; `#11096` =
`['DEMO-60130' (open STORY), 'DEMO-9714' (2023-done STORY)]` flips between the
two depending on the view.

**Fix:** add **one exported helper** in `analysis/ballcourt.py`:

```python
def primary_ticket(keys, *, kind_by_key, state_by_key) -> str | None
```

Rules, first match wins: prefer a `STORY`/`TICKET` key over `EPIC`; among stories
prefer `open`/live over `done`/closed; among ties the **most recent** story;
`None` when no key is usable. Use it in `analysis/aging.py`,
`analysis/tickets.py`, `app/tables.js`/`app/tickets.js` so one PR always shows the
same ticket. `analysis/verdict.py` is U4 and will import the same helper — do not
edit it here.

**Acceptance:** a fixture with an epic+story PR and an open+closed story PR
resolves to the story / the open story in every surface; the ticket link opens a
page whose PR list contains the PR; regression tests in `tests/`.

### F-09 — the same open PR has one holder story
`#11030`, `#11088`, `#11095` (no assigned reviewer) and `#11093` (already
reviewed) show Aging `HOLDER = —`, Delivery `WAITING ON = review queue` /
`ready to merge`, Ticket detail `waiting 5 d 0 h on nobody`. **Fix:** one
attribution function used by `analysis/aging.py` + `analysis/tickets.py` +
`app/tables.js`/`app/tickets.js`: no reviewer ⇒ `review queue` (not “nobody”);
reviewed/ready ⇒ `ready to merge`; never `—` when Delivery names a live queue.
**Acceptance:** all three views agree for those four PRs; tests.

## U2 — scope & chrome (`app/main.js`, `app/format.js`)

### F-02 — the person roster is not the 40k event slice
`buildScopeOptions()` and `validateScope()` (`app/main.js:2936,3074`) only look at
`state.events`, which is capped at the newest 40,000 of 76,213. 75 historical
actors (`DmMa` 2,647 events, `IvZi` 777) and 27 reviewer-only people
(`PERSON:LeMi2`, active on the 1q Queue) are rejected with “does not exist”.
**Fix:** build/validate against the full roster: `state.objects` PERSON rows,
`STORY.actor`, queue streams, aging holders, **and** `state.events`; a
`person:` deep link must resolve without loading all events. A person who is only
a review target must scope.
**Acceptance:** `#scope` contains `DmMa`, `IvZi`, `LeMi2`; setting them (and their
deep links) works on a >40k-event store; tests.

### F-04 — forecast must use the current timeframe
`#forecast-card` calls `/api/forecast` without `days`, so `1q` evaluates 260
weeks (`CV 0.87` → refused) instead of 90 d (`CV 0.45` → forecast). **Fix:** pass
the active window’s `days` (as other cards do). **Acceptance:** Timeline `1q`
forecast is not refused for “erratic”; `1w` still refuses honestly; test.

### F-08 — top bar contradicts itself
Left: `Snapshot from 20 h ago: 76213 events`; right: `live · 40000 events`.
**Fix:** distinguish loaded vs total (`40,000 of 76,213 loaded`) and keep one
source for the snapshot age. **Acceptance:** both numbers reconcile on every
view; test.

### F-12 (dates/Sources) — yearless multi-year ranges + padded “All” end
`28 Sep → 21 Nov` for a 2021→2026 span; `All` ends at a future date
(`28 Sep → 14 Dec`). **Fix in `app/format.js`:** include the year whenever the
range crosses a year boundary (or the span is > ~11 months); clamp the `All` end
to the store’s last event/`as_of`, never a future padding date. Sources page uses
the same formatter. **Acceptance:** formatted ranges disambiguate years; All ends
at the data, not in the future; tests (pure functions).

### F-15 — Sources table shows PR columns for the Jira run
`jira-cloud` row shows `PRS SEEN 0 / KEPT 0 / DROPPED 0` and hides
`issues_seen/kept` and events. **Fix:** per-source columns (issues for
Jira runs, PRs for Bitbucket/hans) plus `events_written` from the existing
`/api/ingest-runs` payload (extend it if needed; do not edit `server/app.py` — it
is U5; if the payload lacks `events_written`, use `counts` already present).
**Acceptance:** the Jira row shows its 2,391 issues and event count; no zero PR
columns; test.

## U3 — timeline clock & labels (`app/timeline.js`)

### F-07 — queue lanes use wall-clock hours under a business-hours legend
`review · 60.9k h` / `author · 990 h` are 24/7 wall clock; the legend says
`business hours · 1 d = 8 h`. **Fix:** price/segment the queue lanes with the
same business-hours calendar (or label them explicitly `wall-clock h` and
reconcile with the legend). Prefer the business clock. **Acceptance:** 1 d of
review shows 8 h, matching the legend; test.

### F-12 (timeline) — sprint label overlap at `All`
`S55…` overlaps `S5800`. **Fix:** collision-aware label placement/rotation or
skip every nth label at low zoom. **Acceptance:** no overlapping sprint labels at
`All` in the fixture; pure-placement test.

## U5 — insights honesty & payload

### F-10 — disclose the mock classifier
Rules header and `Comment tax`/`Questions` never say the 11,885 threads are
`mock-v1` (placeholder hash labels). **Fix:** surface the classifier
version/share in the Rules payload and render a disclosure next to the
thread-derived metrics (“labels: deterministic mock — a model relabel is
pending/imported”); when a model version is present, name it. **Acceptance:** the
disclosure appears with `mock-v1` and disappears/changes with an `ollama-v1`
store; tests.

### F-11 — 12.2 MB `/api/insights` at `days=1882`
The payload embeds all 11,885 threads, 2,320 worklog rows and 2,081 yield rows
while the UI renders ≤25 drill-down rows. **Fix:** cap the embedded drill-down
arrays (e.g., top N per panel, honest `total` counts) or add an explicit
`?full=1`; the default response must be small (target < ~2 MB) and the UI
unchanged for the visible rows. **Acceptance:** `days=1882` payload size drops
and every visible row/count still reconciles; test on a synthetic large store or
the fixture.

## U6 — polish

### F-13 — PR ids truncated in Aging
`.aging .c-id { width: 144px }` cuts `DEMO-58511 #1…`. **Fix:** widen/flex the
cell or right-align the PR number so the full id is visible at 1600 px.
**Acceptance:** regression test/assertion on the CSS contract or a DOM snapshot;
no horizontal overflow on phone.

### F-14 — Epic child stories show `open · open`
**Fix:** render one state token (or `open · story` only when they differ).
**Acceptance:** unit test on the formatter; the Epic page shows one state.

## U7 — sprint labels (F-16)

**F-16 (user-reported):** the Sprints page, Timeline strip, forecast card and
lens render `S${sprint_id}` (`S4392`, `S18568`). The team's Jira names are
`Sprint 352`-style, but the name is (correctly) never stored, so the number is
gone after the scrape.

**Fix, scrape side:** in `ingest/jira_pipeline.py`, parse the sprint name with a
closed pattern — `^\s*(?:[A-Za-z0-9 _.-]*?\s*)?(?:sprint|s)\s*#?(\d+)\s*$`,
case-insensitive — and store `sprint_no` (a non-negative int) in the SPRINT
attributes only when it matches. Add `"sprint_no": COUNT` to
`taxonomy.OBJECT_ATTRIBUTE_SPEC`. **Never store the name.**

**Fix, read side:** `analysis/jira.py` includes `sprint_no` in each sprint row
(fall back to `None`); `app/sprints.js`, `app/timeline.js`, `app/format.js` and
`app/main.js` render `S<sprint_no>` when present, else `S<sprint_id>`. One shared
`S` formatter if practical; all surfaces must agree.

**Acceptance:** a fixture sprint named `Sprint 352` yields `sprint_no: 352`, the
API row carries it, and every surface shows `S352`; a store without it still
shows `S<id>`; `--verify` passes; no name text anywhere in bundle/store/API.
**Operational:** populating the live store needs a Jira re-scrape **with the same
identity map** (otherwise actor pseudonyms churn), then import. Document that in
the unit report; do not re-scrape without the map.

## Rules for every unit

1. Work only in your assigned worktree/branch; **do not** edit another unit’s
   files, `prd/70…`, `STATE.md`, or `docs/QA-v017-combined.md`.
2. Add a regression test that fails without the fix. Run the relevant Python
   suite (`python -m unittest tests.<module>` / the full `tests/` if cheap) and,
   for `app/`, `node --test app/test/*.mjs`; report exact commands and counts.
3. One scoped commit (`git add <paths>`, never `-A`) with `(prd/70 F-xx)` in the
   message. **Do not push, do not tag.**
4. If a finding cannot be fixed without touching another unit’s file, **stop and
   report** the blocker rather than crossing the boundary.
5. Return: branch, commit SHA, files changed, tests run, acceptance evidence, and
   any deviation from this PRD.

The orchestrator merges the wave, runs the full suite, re-verifies the fixed
findings (fresh QA sandbox), and cuts the next tag.

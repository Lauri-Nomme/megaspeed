# 68 — v0.14.0 QA fixes (F-01 … F-16)

Source: `docs/QA-v014.md` (free exploration on `demo-tlla`, a 2,320-story **Jira-only** corpus, plus
`del-combined`). 16 findings: 4 bugs, 8 papercuts, 4 polish. W-03 is **partial** because F-03/F-06/F-08
break the aging surface. Every finding gets a fix and a regression test. Conform to `docs/concepts.md`;
follow `prd/10`. The absent PR layer is **by design** on this corpus: the fixes must say "unavailable"
honestly, never invent or crash.

## Dispatch map (file-disjoint)

| unit | findings | files |
|---|---|---|
| **G1 — aging + main.js** | F-03, F-06, F-08, F-13, F-14 (label), F-16 | `analysis/aging.py`, `app/aging.js`, `app/main.js` |
| **G2 — sprints/trend/timeline/layout** | F-02, F-07 (backend), F-11, F-14 (CSS), F-15 | `app/sprints.js`, `app/timeline.js`, `app/style.css`, `app/trend.js`, `analysis/jira.py`, `analysis/trend.py` |
| **G3 — no-PR honesty** | F-04, F-07 (UI), F-10, F-12 | `analysis/simulation.py`, `app/policy.js`, `app/yield.js`, `app/ticket.js`, `analysis/tickets.py`, `app/tables.js` |
| **G4 — quick UI** | F-01, F-05, F-09 | `app/queues.js`, `app/rules.js`, `app/verdict.js` |

---

## G1 — Aging correctness + indirect main.js

### F-03 BUG — person scope filters all Jira-only aging rows to 0
`_holder_block(..., story if status["state"] in LIVE_STATES else None)`; on a store with no PRs a story
is never `live`/`unmerged` (it is `dead`/`unobserved`), so every row's holder is `[]`, `HOLDER` is `—`,
and `agingMatchesScope()` matches nobody under `person:*`. **Fix:** pass the story unconditionally as the
fallback (`_holder_block(court, open_states, age, story)`), so the Jira assignee is the holder
(`{person: story.actor, role: "owner", hours: age}`). Person scoping then works; `person:LaNo` shows
their open stories. **Test:** a no-PR story row has its assignee as holder; `person:<actor>` includes it.

### F-06 PAPERCUT — “see re-plan” with no re-plan
`all_rows` is taken **after** the `only=live` filter, so `replan(all_rows)` is empty exactly when the
default tab says “see re-plan”. **Fix:** keep the window-scoped rows before the `only` filter and compute
`abandoned = replan(in_window_rows)` from those, so `#aging-replan` always renders below the live table
(and is window-scoped). **Test:** `only=live` still exposes the re-plan items and its count matches the
hidden note; `only=all` unchanged.

### F-08 PAPERCUT — “50 no live PR” vs 103 non-live
`counts.dead` (dead only) is printed as “no live PR”; `unobserved` (53) also has no live PR and appears
under “No live PR — close or re-plan”. **Fix:** add `counts.no_live_pr = dead + unobserved` (or
`len(abandoned)`) to the payload and use it everywhere the chip/honesty/tab says “no live PR”; keep
`dead` for the abandoned-only meaning. Update `app/aging.js` and the tab chip in `app/main.js`.
**Test:** with 50 dead + 53 unobserved, the chip/honesty say 103, matching the re-plan table; a
dead-only label (if kept anywhere) says “abandoned”.

### F-13 POLISH — Sources is PR-centric on a Jira-only store
`app/main.js` Sources facts omit stories/epics/sprints, print “PRs 0 kept of 0 seen”, “dropped: 0 PRs”
and “the combined tenant joins the PR half…”. **Fix:** include `stories`, `epics`, `sprints` when
present; when the dataset has no PRs, say “PR layer: not in this dataset” instead of “0 kept/0 seen” and
tailor the ingest note to the actual source(s) (e.g. “this dataset is the Jira half only”). **Test:** a
Jira-only meta renders the story/epic/sprint facts and no false PR/combined claim.

### F-14 (label half) — stage prints `in_progress` / `in_verification`
`app/aging.js` renders the raw stage key. **Fix:** `String(row.stage||'').replace(/_/g,' ')` (CSS widths
are G2's). **Test:** rendered cells read “in progress”, “in verification”.

### F-16 POLISH — malformed `#scope` value creates “the undefined queue”
A `change` on a value outside the options yields `["", undefined]`; `setScope("", undefined)` passes
validation. **Fix:** in `setScope`/`validateScope` (app/main.js), coerce any kind not in
`('all','person','role','repo','ticket')`, or a non-`all` kind with no value, to `all`. **Test:** the
guard produces `{kind:'all'}` and the banner is unscoped.

---

## G2 — sprints, trend, timeline, layout

### F-02 BUG — 28 dateless sprints render `NaN undefined`; future sprints invert
- `windowLabel()` must check `Number.isFinite(Date.parse(band_start))` and say “dates unknown” (and label
  `state:null/closed:false` as “unknown”, not “closed”).
- `Timeline.setSprints()` must keep a **membership map independent of finite bands**, so focusing a
  dateless sprint still filters the story lanes; `sprintFocusKeys()` must find its stories.
- `state === 'future'` (or `start > snapshot`) sprints must not be treated as in-progress: no active
  snapshot band, no `S7564 in progress` in `/api/trend?bucket=sprint` (prefer the actual active sprint,
  else “no sprint in progress”); a 0-story future placeholder is not a band.
- Backend (`analysis/jira.py`) must expose whether dates/state are known so the UI can say “unknown”
  without guessing; keep the 28 event-only sprints in the list (their membership is real).
**Test:** a dateless sprint renders “dates unknown”, filters to its story on focus; a future sprint with
start > snapshot is not in-progress and draws no inverted band; the trend headline names the active
sprint (or none), never a future one.

### F-11 PAPERCUT — PR-only trend tabs report 0 as measured on a Jira-only store
When `court.prs` is empty, `merged`/`open`/`arrival_prs` must be `available:false` with “no PR objects in
this store” (not `13/13 buckets measured · 0 latest`). `cumulative_flow` already computes `story_bands`
but hardcodes PR `bands`/`population:'pr'`; fall back to story bands (`population:'story'`, legend
“cumulative stories by stage”) when there are no PRs and stories exist.
**Test:** empty PR court → PR count metrics unavailable with the reason; cumulative flow plots story
bands and says so.

### F-07 (backend half) — rolled %C&A 0/84 when Reviewed is unfalsifiable
`_story_step_clean(...)["review"]` is `False` for every story without a merged PR, forcing
`clean_joint = 0`. When `review.available === false`, the joint clean count/rate must be `null`/`–`, not
0; `rolled` stays null with the reason. **Test:** a Jira-only cohort has `clean_joint is None` (not 0),
`rolled is None`, and the four other steps’ counts unchanged.
(The UI half is G3.)

### F-14 (CSS half) — 3-digit ages/stages/pills clip, horizontal scrollbar
In `app/style.css`, widen `.aging .c-stage`, `.c-age`, `.c-live` (or allow wrapping) for “420 d 6 h 610
calendar days”, “in verification”, “unobserved” / “very old”, at 1600×1000, without a table scrollbar.
**Test:** source guard / harness widths; no `scrollWidth > clientWidth` on those cells.

### F-15 POLISH — canvas sprint labels collide; phone `.sprint-scope-count` overflows 4px
`drawSprintBands()` must track the last label right and skip/clip colliding labels (like `drawAxis`);
`.sprint-scope-count` must wrap inside `.sprint-row` at 412 px. **Test:** label boxes do not intersect at
1600 and 412; Timeline `documentElement.scrollWidth === 412`.

---

## G3 — no-PR honesty

### F-04 BUG — Jira-only store claims “no Jira layer” and disables refine
`simulation.calibrate()` early-returns when `samples["review"]` is empty **without** `refinement`, so
`refinementUnavailable()` sees `undefined` and prints “no Jira layer — no created → ready segment” on a
store that has 1,865 refinement segments. **Fix:** always include `refinement` in the calibration (even
on the early PR-missing return); `app/policy.js` must only call it unavailable when `refinement.hours` is
actually null, and the message must reflect the real reason. **Test:** a Jira-only store has
`calibration.refinement.hours` measured and the refine knob **enabled**; a truly empty refinement cohort
still disables it with the correct reason.

### F-07 (UI half) — “84 deliveries — too few for a rate” and “0 / 84”
- `app/yield.js`: show “too few for a rate” only when `n < min_deliveries`; when `rolled == null` because
  Reviewed is unfalsifiable, say “Rolled %C&A is unavailable because Reviewed is not measured in this
  store” and render `– / 84`, not `0 / 84`.
- `app/ticket.js` Markdown: a step with `available === false` exports **not measured**, never “bounced”.
**Test:** the Jira-only panel shows the unavailable reason and no false “too few”; the ticket export says
“reviewed not measured”.

### F-10 PAPERCUT — “every ticket … has its code merged” with 0 PRs
`analysis/tickets.done_open()` must be `available:false` when `court.prs` is empty (Jira-only), with a
neutral “unavailable: this dataset holds no pull requests” state; never a green “none — every ticket …”.
Update `app/tables.js` rendering. **Test:** a 0-PR store renders the unavailable state; a store with PRs
keeps the current result.

### F-12 PAPERCUT — Epic Copy-as-Markdown exports a broken story template
`ticketMarkdown()` has no `kind === 'epic'` branch. **Fix:** export the epic header (`story_count`,
`points`, state) and a child table (`story | state | points | cycle` with links), never “logged — in
undefined worklogs”. **Test:** an epic export contains the child rows and no “undefined”.

---

## G4 — quick UI

### F-01 BUG — Queue view crashes when `scope=all` and there are no reviewer responses
`responseTable()` dereferences `scope.label` when `scope` is `null`. **Fix:** only use the scoped empty
message when `scope` is non-null; with `scope === null`, return `''` (the existing intent) or an
unscoped “No review requests in this window.” No `TypeError`, and the legend/assumption/honesty still
render. **Test:** a 0-response response object + `scope:null` renders without throwing and the rest of the
queue page mounts.

### F-05 PAPERCUT — `window_days: 1880.4` rendered as `≈1.9k h`
`evidenceValue()` treats any non-integer `*day*` key as hours. **Fix:** format `*_days` keys as days
(`1880.4 calendar days`, or one decimal) and never pass a day count to `fmtImpact`. **Test:** a float
`window_days`/`limit_days` renders in days; real hour keys still use `fmtImpact`.

### F-09 PAPERCUT — no-queue verdict duplicates the exploit disclaimer; “exploit first: 0 items are still owed”
- `app/verdict.js`: when the exploit rung has no lab verdict, print the lab line **or** the honesty line,
  never the same sentence twice.
- `analysis/verdict.py`: when `exploit_n == 0` but exploit rules exist, the elevate reason must name the
  rule (“exploit first: 1 rule on the exploit rung”), not “0 items are still owed”.
**Test:** the no-current-queue card has one disclaimer line; elevate’s reason names the rule; the
normal state unchanged.

---

## Definition of done

- All 16 fixed, each with a regression test that fails without the fix; W-03 re-verified FIXED.
- Suites green (`tests/`, `scrape/tests`, `node --test app/test/*.mjs`).
- Live Jira-only store: person scope works on Aging; no `NaN` dates; no false “no Jira layer”; no PR-only
  metric reported as measured 0; no “every ticket merged”; epic export intact; queue doesn’t crash.
- Then a **new tag** (`v0.14.1`) on the verified commit — never re-point `v0.14.0`.

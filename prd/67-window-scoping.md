# 67 — window scoping for people, lanes, aging and the constraint verdict

Source: the first look at a **Jira-only** corpus (2,320 stories · 106 sprints · 252 people, no PRs).
Five findings, verified against the live store. Two are real scoping bugs (all-time people/rows in a
windowed view), one is a semantics bug (a zero-owed historical tail named as "the constraint"), one is
a copy problem (a window-scoped count read as a store total), one is the same all-time noise in the
"who is who" legend.

## Findings (verified)

### W-01 BUG — timeline actor lanes include people with no activity in the view
`Timeline.laneRows()` (actor dim) builds rows from `this.events` (every loaded event) and
`activityCounts()`; the visible window is only the axis. With 252 people, the lane list is hundreds of
empty lanes. Fix: keep an actor only if it has ≥1 event inside `this.view` (`start ≤ e.t ≤ end`), rebuild
when the view changes, and report the hidden count ("N people with no activity in this window").

### W-02 BUG — "who is who" legend lists every person ever seen
`analysis/ballcourt.person_legend()` unions all-time authors/approvers/reviewers/story owners (plus
bots), with `stories_owned` counted over the whole store. The Queue/Rules legend therefore lists 252
chips, most with `0/0/0/0`. Fix: make the legend window-aware — count PR activity inside the window and
stories whose work intersects it (created ≤ window end and done ≥ window start, or still open) — and
drop rows with no activity. Add `hidden` to the legend and a note.

### W-03 BUG — aging rows ignore the window
`analysis/aging._story_rows()` returns every open story; the window only shapes the percentile cohort.
On the Jira corpus a 90-day view returned **239 rows**, all `dead`/`unobserved`, ages up to 1,243
calendar days; `only=live` returns 0. Fix: an in-flight row is in this window only when it was
**active** in it — `last_human ≥ window.start` (or created inside the window). Dead/old rows leave the
list (they remain in the re-plan feed, also window-scoped), and the counts must match. The Aging tab
then shows the work that moved in the chosen timeframe, with a note saying why rows are hidden.
Default the tab to `only=live` (an honest empty state beats 239 abandoned rows), keeping the toggle.

### W-04 BUG — a station with 0 h owed is named as "the constraint"
`analysis/verdict.constraint()` ranks by `removable_hours = live + tail`, where `tail` is the **historical**
excess above p85 over completed stories. A Jira-only window has `live = 0` for every station, so it
names Refinement (or Starting work) with "0 h currently owed and 2312 h of abnormal tail" — a historical
fact, not a queue to exploit. Fix: the verdict ranks **live** (owed now) first; `removable_hours` stays
for the pool table, but a station with `live == 0` cannot be the constraint. When no station has live
work, return `constraint: null` with a `note` ("no current queue: 0 h owed now; the largest historical
tails are …") and a `historical` list; the card/banner say exactly that instead of naming a constraint.
With the PR half this automatically comes back to life.

### W-05 COPY — "7 stories carried over" is window-scoped and reads as a total
The number is **correct**: 7 of 36 stories committed at start in the **one sprint that closed inside the
28-day window** were not done by its close (`jira-carry-over`). The whole store has many more across 106
sprints. The label already mentions the window but is easy to misread. Fix: name the sprint count in the
title ("7 of 36 committed in the 1 sprint closed in this window…") and add a whole-store context line
(`N carried over across all 106 sprints`) so the two scopes are side by side. No number changes.

## Dispatch map (file-disjoint)

| unit | findings | files |
|---|---|---|
| **A — people in the window** | W-01, W-02 | `app/timeline.js`, `analysis/ballcourt.py`, `server/app.py` (legend calls), tests |
| **B — aging window** | W-03 | `analysis/aging.py`, `app/aging.js`, `app/main.js` (tab default only=live), tests |
| **C — verdict live-first** | W-04 | `analysis/verdict.py`, `app/verdict.js`, tests |
| **D — carry-over copy** | W-05 | `analysis/jira.py`, `analysis/rules.py`, `app/rules.js`, tests |

## Definition of done

- All five fixed/implemented with regression tests (windowed fixture with old and recent people/rows).
- Suites green; `--verify` untouched (no store change).
- The Jira-only live store shows: no empty people lanes, no all-time legend, the aging tab scoped to the
  window (and honest when empty), "no current queue" instead of a 0-owed constraint, and the carry-over
  count with both scopes.

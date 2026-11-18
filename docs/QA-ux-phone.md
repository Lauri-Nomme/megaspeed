# QA — phone UX review (412 × 915, OnePlus IN2025)

Follow-up to `docs/QA-mobile.md` (which found the build unusable on a phone) and
`prd/28-mobile.md`. The shell/table work of prd/28 landed; a real phone then showed
three defects:

1. Jira ticket numbers missing on the timeline (PR under story under epic).
2. No one-finger vertical scroll over the charts.
3. Labels cut off on the right / word-wrapped into vertical fragments.

Method: headless Chromium at **412 × 915** (`harness/browser.py`, tenant
`ux-del-combined`: 2161 events · 131 PRs · 100 stories · 1 epic · 3 sprints).
Layout through `/api/eval` (`getBoundingClientRect`, computed style, text-node line
counts); real finger gestures through CDP `Input.dispatchTouchEvent` on a
`has_touch` context (the harness page cannot synthesise those); a few screenshots
for confirmation. "Before" = `HEAD` served from a separate worktree on `:8784`,
"after" = this branch on `:8783`, same DuckDB copy.

## Root causes and fixes

### 1. Ticket keys missing on the timeline

Three separate causes, all in the story/PR lane gutter (`app/timeline.js`):

- **The epic row disappeared when PRs nested.** `storyRows()` showed the epic header
  only above 60 days and *hid it* below 45 days, so the moment a user pinched in,
  every story lost its parent (`DEL-1`).
- **A PR row printed its story key, not itself.** On a phone the gutter label was
  `row.ticket`, so three PRs of `DEL-100` were three identical `DEL-100` rows
  (screenshot below). The PR number was dropped.
- **Stories with no PR had no epic.** The epic came only from a PR's
  `ticket_keys`; a `STORY` object's own `attributes.epic_key` was ignored.

Fix: on a phone (canvas ≤ 640 px) the epic header stays at every zoom; the gutter is
112 px (was 88) and left-aligned with the hierarchy drawn in it — `◆ DEL-1` →
`▸ DEL-105` → `DEL-105 #4` — and the PR rows print `KEY #number`. The story's epic
falls back to `STORY.attributes.epic_key` (`Timeline.storyEpic`). The section header
is the short `STORIES 100` (the 31-character desktop header was wider than the
gutter). A first-ever phone visit opens the timeline on **story** lanes (a saved
choice still wins). The desktop keeps its old rows, labels and 60-day epic rule.

| | zoom 90 d (wide) | zoom 30 d (PRs nested) |
| --- | --- | --- |
| before | `before-timeline-story-top.png`: stories, no hierarchy glyphs | `before-timeline-nested.png`: `DEL-105`, `DEL-105`, `DEL-100`, `DEL-100`, `DEL-100` … no epic, no PR number |
| after | `after-timeline-story-top.png`: `◆ DEL-1` / `▸ DEL-105` … | `after-timeline-nested.png`: `◆ DEL-1`, `▸ DEL-100`, `DEL-100 #1`, `DEL-100 #2` |

Object/actor lanes: `after-timeline-actor-prs.png` (`DEL-100 #2`, `DEL-108 #10` …).

### 2. One-finger vertical scroll blocked on the charts

Root cause: `#timeline, #blocking, #queues { touch-action: none }`, plus
`preventDefault()` on every `pointerdown`/`pointermove` of the timeline and blocking
map. The browser was told never to scroll when a finger started on a canvas, and
those canvases are 566–2336 px tall, i.e. most of the page.

Fix: `touch-action: pan-y` on the three canvases; `touchAxis()` (`app/mobile.js`)
decides once the finger passes the 8 px slop — horizontal ⇒ the app pans (and
captures the pointer, `preventDefault`s), vertical ⇒ the page scrolls and the app
does nothing; two fingers ⇒ pinch (captured, `preventDefault`ed); a tap that never
passes the slop still selects. The blocking map no longer pans vertically (that axis
is the page's). Mouse input takes the untouched `pointerType === 'mouse'` path.

Real-touch results (CDP, 412 × 915, canvas partly scrolled into view; swipe 200 px):

| gesture | before | after |
| --- | --- | --- |
| vertical swipe on timeline | page `scrollY` Δ **0** | Δ **235** |
| vertical swipe on blocking map | Δ **0** | Δ **234** |
| vertical swipe on WIP chart | Δ **0** | Δ **91** (reached the end of the page) |
| horizontal drag on timeline | view shifted 19.7 d | view shifted 19.7 d, page scroll Δ 0 |
| two-finger spread on timeline | span × 0.167 | span × 0.167 |
| tap a PR bar | drawer opens | drawer opens (`DEL-100 #1`) |
| horizontal drag on blocking map | pan.x −73 | pan.x −73, pan.y 0 |
| tap blocking node `review` | → Queue scoped to `review` | same |
| `getComputedStyle(canvas).touchAction` | `none` | `pan-y` |

A side effect found while testing: panning the timeline switches the timeframe to
`custom`, and that chip was in the flow of the top bar's context row on a phone.
It wrapped the row, grew the top bar **107 → 157 px** mid-drag, and scroll
anchoring then shoved the page 50 px under the finger (even with `touch-action:
none`). The chip is now an absolutely positioned badge on the timeframe group;
top bar height after a pan: **107 px**, page scroll Δ **0**.

### 3. Right-edge clipping and vertical word-wrap

Audit: for every view, `document.documentElement.scrollWidth`, every visible element
whose rect passes 412 outside an intentional scroller, every `text-overflow:
ellipsis` element that is actually truncating, and every text node laid out in
≥ 3 lines at < 6.5 characters per line (the signature of vertical wrapping).

| symptom (before) | cause | fix |
| --- | --- | --- |
| Overview "Needs attention" / "People diagnosis" / "Where waits accumulate" cards: `9 h 11 min` laid out as `9 h / 11 / min` (10 text nodes, 3–4 lines each); Policy lab `≈2 h 29 min` (2) | `.panel .table .c-dur { width: 96px }` and `.table.compact .c-dur { width: 90px }` outrank the phone card rule `.table .c-dur { width: auto }`, so each card value was pinned to 90–112 px; `.panel .table td` padding survived too | phone block now overrides the panel/compact widths, paddings and heights |
| Sprint strip: `S1 … S3` rows `26 done`, `11 Sep → 7 Oct`, `plan 2026-11-27` all right of 412 (8 nodes, up to x = 1394), reachable only by a hidden horizontal scroller | `.sprint-rows` was `nowrap; overflow-x: auto; scrollbar-width: none` | rows stack (`flex: 1 1 100%`, each fact `nowrap`, row wraps between facts) |
| Tenant pill `ux-del-c…` (the name of the dataset every number belongs to) | 96 px pill with `text-overflow: ellipsis` | name wraps onto two lines inside the pill (`ux-del-` / `combined`) |
| WIP chart: diagnosis pill painted over the first buckets of the sparkline; `≈ overloaded` ran 3 px past its own pill; a long role text would run into the pill | pill at fixed x = 194 in a 232 px gutter | pill right-aligned in the gutter and sized to its text; the role label sits after the name and is ellipsised to the room left (`sentinel` stays whole) |
| Lone `·` at the start of wrapped lines in the meta strip | inline separators between wrapping flex items | separators hidden on a phone, items spaced by gap |

Everything else measured clean at 412 × 915.

## Before / after numbers (`/api/eval`, 412 × 915)

| view | `scrollWidth` | right-clipped text nodes | vertical-wrapped text nodes | interactive elements < 44 px |
| --- | --- | --- | --- | --- |
| overview | 412 → 412 | 0 → 0 | **10 → 0** | **4 → 0** |
| delivery | 412 → 412 | 0 → 0 | 0 → 0 | **1 → 0** |
| timeline | 412 → 412 | **8 → 0** | 0 → 0 | 10 → 10 (inline key links, see below) |
| blocking | 412 → 412 | 0 → 0 | 0 → 0 | 0 → 0 |
| queue | 412 → 412 | 0 → 0 | 0 → 0 | 0 → 0 |
| rules | 412 → 412 | 0 → 0 | 0 → 0 | **851 → 5** |
| policy | 412 → 412 | 0 → 0 | **2 → 0** | 1 → 1 (13 px checkbox inside a 44 px label row) |
| sources | 412 → 412 | 0 → 0 | 0 → 0 | 0 → 0 |
| ticket `DEL-167` | 412 → 412 | 0 → 0 | 0 → 0 | — |

Tap targets: card ticket keys and rules drill-down links are now 44 px high
(`inline-flex; min-height: 44px`: 851 → 5 on Rules). The remaining ones are ticket
keys inside running text (`sprint-out-keys`, rule examples); they keep their line
height and get a 44 px hit area through a pseudo-element, which
`getBoundingClientRect` cannot see.

## Per-view verdict at 412 × 915

| view | verdict |
| --- | --- |
| Overview | fixed — values one line each (`after-overview.png`, `before-overview.png`) |
| Delivery | fine; card key now a 44 px target (`after-delivery.png`, `before-delivery.png`) |
| Timeline | fixed — hierarchy + keys, scroll, pan, pinch, tap; sprint strip stacked |
| Blocking map | fine; vertical scroll now works, pan is horizontal-only (`after-blocking.png`) |
| Queue | fixed — pill/role (`before-queue-top.png` → `after-queue-top2.png`) |
| Rules | fine; drill links 44 px (`after-rules.png`) |
| Policy lab | fixed — durations no longer wrap |
| Sources | fine |
| Ticket detail | fine, no overflow (`after-ticket.png`) |

Screenshots (not committed) are in `/tmp/opencode/ux-review/`: `before-*.png` from
`HEAD`, `after-*.png` from this branch. `after-delivery.png`, `after-blocking.png`,
`after-ticket.png` and `after-rules.png` are 2× captures of `.app`.

## Desktop and medium are unchanged

Every phone rule is under `@media (max-width: 640px)` or gated on a canvas ≤ 640 px
(`Timeline.isPhone()`, the queue `phone` flag); the mouse handlers are untouched.
Checked at **1600 × 1000** and **900 × 1000**, before (`HEAD`) vs after, all 8 views:
a fingerprint of every visible `.app` element's tag/class/x/y/width/height is
**byte-identical**, as are FNV hashes of the timeline canvas (actor, story and object
lanes), the blocking map and the WIP chart pixels; a mouse drag moves the timeline
23.27 d and a wheel zoom reaches 16.8 d in both builds.
(At 900 px the document is 913 px wide in both builds — a pre-existing medium-width
issue outside prd/28's ≤ 640 px scope, not touched here.)

## Verdict

At 412 × 915 the three reported defects are gone: ticket keys and the epic → story →
PR hierarchy are on the timeline at every zoom, one finger scrolls the page over every
chart while horizontal drag pans / two fingers pinch / a tap selects, and no view has
right-edge clipping or vertical word-wrap. Remaining, deliberately left: the 13 px
Policy checkbox (its label row is the 44 px target), inline ticket keys in prose
(hit area only), the 913 px-wide document at 900 px, and any non-Chromium touch engine (only
Chromium was available here).

Reproduce: scripts used for the numbers are in `/tmp/opencode/ux-review/`
(`touch_check.py`, `touch_tap.py`, `desktop_check.py`, `mouse_check.py`) and
`/tmp/opencode/audit*.js`; run against two servers
(`MEGASPEED_DB=… .venv/bin/python -m server --port N`) with
`harness/browser.py --width 412 --height 915`. Cache note: the harness browser keeps
ES modules in its HTTP cache; after editing `app/*.js` fetch them with
`cache: 'reload'` (or restart the browser) before measuring.

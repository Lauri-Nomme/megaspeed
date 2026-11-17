# Review — the Bitbucket and Jira layers, presented **together**

**Question:** the scrape now has a Bitbucket source (PRs, commits, `push_class`, comments) and a
Jira source (Story/Epic/Sprint objects, status/sprint/worklog events) joined on ticket keys. Does
the UI + analysis actually surface a **ticket-centric** view?

**Read at:** `083e0b9` (working tree clean apart from this file).
**Method:** read `prd/03`, `prd/09`, `prd/17`, `prd/21`, `app/` (main.js, timeline.js, tickets.js,
tables.js, rules.js, index.html, scope.js), `analysis/jira.py`, `analysis/rules.py`,
`analysis/ballcourt.py`, `server/app.py`, `server/query.py`; drove the live UI on
`127.0.0.1:8766` (tenant `del3`) through `/api/eval` + `/api/screenshot`; and drove a
**combined Bitbucket+Jira probe store** on `127.0.0.1:8841` (built by the repo's own fixture
pipeline, `tests/test_jira_analysis.py::CombinedFixtureTests`: Bitbucket fixture → `ingest.pipeline`
then Jira fixture → `ingest.scrape_jira` → `ingest.import_bundle`) to see what the *same UI* does
when STORY/EPIC/SPRINT objects really exist. The probe store is synthetic — it is not the blocked
lab scrape — and is labelled as such everywhere below. Nothing in the repo was modified except
this file.

Screenshots (scratch dir, not committed):
`/tmp/megaspeed-shots/int-del3-1q.png` (live `del3`: 131 PR lanes + `DEL-167` drawer) ·
`/tmp/opencode/megaspeed-shots/int-timeline-prs.png` (probe store: PR lanes) ·
`/tmp/opencode/megaspeed-shots/int-actors-jira-events.png` (probe store: story events as actor-lane dots) ·
`/tmp/opencode/megaspeed-shots/int-rules-jira.png` (probe store: the only Jira surface).

---

## Short answers to the four questions

1. **PRs under a ticket in the timeline?** No. The object lane dimension *is* the list of PRs
   (`timeline.js:437`), and the ticket key is a prefix on each lane label. There is no grouping,
   no connector and no nesting; a ticket with two PRs is two unrelated rows. No view renders
   STORY, EPIC or SPRINT objects at all — the word `sprint` does not occur anywhere in `app/`,
   and `story` appears only in comments and one **count** in the Rules stats strip (`rules.js:103`).
2. **A ticket-centric view?** No. PRs live in the timeline/drawer/delivery list; the Jira layer
   lives only in the Rules page as cohort-level prose. The join key is used for PR *labels*, epic
   *demotion* (`tickets.js:23`), impact *arithmetic* (`ballcourt.py:906`), and the analysis join —
   never as a destination you can open.
3. **A view of one ticket's 8 segments + its PRs?** No. The eight segments exist as per-story
   numbers in `/api/insights` (`metrics.waiting_time_decomposition.rows[]`) and as one aggregate
   rule ("36% of ticket cycle time in the review rounds segment"). Nothing renders a segment bar,
   a boundary timestamp, or the PRs behind a segment.
4. **What is needed for real answers?** A ticket detail page (8-segment bar + the ticket's PR
   rows + sprint membership + worklog + one prose answer), reachable from every ticket key; a
   single `/api/ticket/{key}` that joins both halves; a `stories` lane dimension with epic nesting
   (semantic zoom); and a sprint dimension. Ordered P0–P7 in §3.

---

## 1. What's presented today

### 1.1 Both halves are in the store; only one half is in the UI

| Store | PRs | Events | STORY | EPIC | SPRINT | TICKET |
| --- | --- | --- | --- | --- | --- | --- |
| `del3` (live, PRs only) | 131 | 1 484 | — | — | — | 101 (empty placeholders) |
| probe store (combined fixture) | 34 | 402 | **12** | **1** | **3** | 13 |

On `del3` there is literally no ticket object with a life: `GET /api/objects?type=TICKET&ticket=DEL-167`
returns `{state:null, created_at:null, closed_at:null, attributes:{}}`. The store knows the key and
nothing else about it.

On the probe store `/api/health` reports `"epics":1,"stories":12,"sprints":3`, and the page can
load them — I counted what the UI's own fetches return from inside the page:

```
PR 34 · COMMIT 155 · PRREV 42 · THREAD 82 · PERSON 9 · TICKET 13 · EPIC 1 · STORY 12 · SPRINT 3
events: status_changed 18 · sprint_added 11 · sprint_removed 1 · pointed 10 · worklogged 14 · created 13
```

The UI then renders **PR objects only**:

| Object | Where it is used in `app/` | Rendered as an object |
| --- | --- | --- |
| `PR` | timeline lanes + gantt bars, drawer, delivery list | **yes** |
| `COMMIT` | tick marks inside PR bars | decoration |
| `TICKET` | nothing — the key is a *string* on the PR | label only |
| `STORY` | nothing — `state.objects` is scanned only for `PR.ticket_keys` (`tickets.js:83`) | **no** |
| `EPIC` | nothing — only the shared-key census that *demotes* `DEL-1` (`tickets.js:23`) | label only |
| `SPRINT` | nothing — `SPRINT` never appears in `app/` | **no** |

### 1.2 The timeline has three lane dimensions, and none of them is a story

`index.html:95` offers `lanes: actors / PRs / queues`. The object dimension is literally
`this.prs.map(...)` (`timeline.js:437`), one flat row per PR, section header `PULL REQUESTS · 131`
(`timeline.js:768`) — see `int-del3-1q.png`.

- **A ticket's PRs are not one thing.** In `del3`, 101 distinct keys appear on 131 PRs; **31 story
  keys have more than one PR** (`DEL-100 #1 #2`, `DEL-104 #4 #5`, `DEL-108 #9 #10`, `DEL-167 #52
  #53`, …). They are separate lanes, in different parts of a list sorted by `repo` then PR number,
  with the key repeated in each label and no grouping, connector or epic nesting — although
  `prd/09 §View 1` specifies "one row per Story / PR, **nested under its epic**" and "semantic zoom:
  epic → story → PR → event".
- **Story events are loaded but unreadable.** On the probe store the legend includes
  `status_changed 18 · sprint_added 11 · sprint_removed 1 · pointed 10 · worklogged 14`, and those
  events are drawn — as anonymous dots on the **actor** lanes, next to PR events of the same size
  (`int-actors-jira-events.png`). The only way to learn a dot is a story transition is to hover it
  and read `objects: STORY:PROJ-108` (`timeline.js:1010`). In `PRs` lane mode the story events are
  not drawn at all (`drawObjectLanes`, `timeline.js:850`, iterates `pr.events` only). No PR bar
  moves when its ticket changes status, and no view draws "in progress started" or "added to sprint
  2" as a *span*.
- **A worklog is a duration drawn as a point.** `worklogged` events carry `time_spent_seconds` and
  `log_delay_seconds` (the analysis reconstructs the work span from them, `jira.py:462`), but the
  timeline renders the event as a single dot. Logged work is never visible as time.
- **No ticket/epic/sprint filtering.** The top bar is `scope` (person / role queue, `scope.js:20`)
  + `timeframe` only. There is no epic, sprint, points, status or ticket filter, though `prd/09`
  lists epic and sprint filters. You cannot even isolate `DEL-167`'s two PRs.
- **The gantt vocabulary stops at the PR.** `PHASE` = `waiting on author · in progress · in review ·
  CI · rebasing · closed · no events since`; `QUEUE_ORDER` = `review · author · CI · calendar`
  (`timeline.js:43,74`). **Five of the eight Jira segments have no phase, colour or queue**:
  `ready→refined`, `refined→sprint`, `sprint→in-progress`, `in-progress→PR-open` (the PR does not
  exist yet, so "in progress" is not that interval) and `merge→done` (the bar ends at merge). They
  are unrepresentable by construction, not merely missing.

### 1.3 The only detail panel is a PR drawer

`openPrDrawer` (`main.js:1029`) shows `overline: PULL REQUEST` plus six measured PR phases, and its
"Related" block is three **strings** — the ticket key, the epic key, `repo#number`
(`tables.js:200`) — not links. Nothing else about the ticket appears.

`DEL-167` is the natural test case: **two** PRs, `#52` merged (`2026-10-14 14:22 → 10-16 11:38`,
cycled `1 d 21 h`) and `#53` open since `2026-10-14 16:10` (a month at snapshot time). Open `#52`
and the drawer says (`int-del3-1q.png`):

```
PULL REQUEST · DEL-167 #52 delivery-api · author BoFe · merged
Closed: its wait is history, not a live bottleneck.
ticket DEL-167 · epic DEL-1 · author BoFe · state merged · cycled 1 d 21 h (measured) · commits 4
waiting on author — · in progress 20 h 10 min · in review 1 d 1 h · CI — · rebasing — · no events since —
RELATED  DEL-167  DEL-1  delivery-api#52
```

`#53` — the PR that has been open for a month and actually explains the ticket's duration — is not
mentioned. The impact line even asserts the *PR* is closed history while the *ticket* is still open.

On the probe store, the drawer of a PR whose STORY object exists (`PROJ-201 #100`) is still titled
`PULL REQUEST` and lists no story state, points, sprint, segment or worklog: `ticket PROJ-201 ·
state merged · cycled 11 d 17 h · …`.

### 1.4 The delivery list is PR triage with ticket keys as labels

`/api/delivery` returns **open PRs** (6 rows on the probe store, 8 on `del3`); the column is
`Ticket / PR` and the value is the PR's own key. Stories that have not opened a PR yet are
invisible in the only list view — on the probe store **2 of 12 stories have no PR at all**
(`PROJ-103`, `PROJ-107`), and 3 PR keys have no STORY (`ARCH-31`, `OPS-7`, `PROJ-201`). The
overview page is blocked/oldest/queue/blocking, all PR-derived.

The single ticket-aware sentence in the whole UI is `impactSentence` ("… N other open PRs share
ticket DEL-167", `tables.js:233`) — the join used as *impact arithmetic*, not as a view.

### 1.5 The Rules page is the only place the Jira layer surfaces — as prose

`app/rules.js` renders `data.suggestions` and (only) `sample.stories`; it renders **no metric
object**. Grep for `metrics.` in `app/` returns one use, and it is PR-side (`pr_size.diff_size.pending`).
On the probe store the page is 10 rules, 5 critical, `≈3.2k h at stake`, `34 PRs · 12 stories ·
98.4 d` (`int-rules-jira.png`):

> **01 · warning · WAITING_TIME_DECOMPOSITION · ≈29 d 17 h at stake**
> *The team spends 36% of ticket cycle time in the review rounds segment*
> `segment review rounds · segment ≈1.4k h · share 36% · cycle ≈4k h · stories 12 · flow efficiency 18%`
> **07 · warning · CARRY_OVER** — *1 story carried over between sprints*
> `carried over 1 · stories in a sprint 10 · share 10% · sprints 2 · wait ≈6 d 22 h`
> **09/10 · critical · WORKLOG_CROSS_CHECK** — *Logged hours differ from active PR time by 0.13×*,
> *Logging is 23.4 days late on average*

That is the entire visual vocabulary of the ticket layer: **aggregate assertions with a count**.
There is no segment bar, no list of the 12 stories, no way to go from "36% in review rounds" to
*which* tickets paid it, and **zero links or buttons on any rule row** (I counted `a,button`
inside each `.rule`: 0). The carry-over rule does not even name `PROJ-105`, although the API has it
in `carry_over.rows[]`. "Where does the time go?" is answered for the cohort, never for a ticket.

### 1.6 The API already computes per-ticket data that nothing consumes

`GET /api/insights` carries `metrics.waiting_time_decomposition.rows[]` — one row per story with
the eight segment hours:

```json
{"key":"PROJ-101","cycle_hours":176.0,
 "segments":{"ready_refined":16.0,"refined_sprint":26.0,"sprint_in_progress":14.0,
             "in_progress_pr_open":128.0,"pr_open_first_review":3.25,
             "review_rounds":23.95,"final_approval_merge":null,"merge_done":null}}
```

plus `carry_over.rows[]` (`key, sprints, wait_hours, points`) and `point_churn.rows[]`. The UI reads
none of it. What the payload does **not** carry is what a ticket page needs most:

- the nine **boundary timestamps** (`jira.py::Cycle.boundaries`: created, refined, sprint, started,
  pr_open, first_review, approval, merged, done) — computed, then dropped;
- the story's **PRs** — `story.prs` is joined in `jira.py:321` and never serialised;
- per-story **worklog rows** — only aggregates survive;
- the story's **events** — the row has no timeline.

Two API-level facts matter for any ticket page:

- **No route returns a ticket's whole history.** `GET /api/events?ticket=PROJ-201` returns **17
  rows, all PR events**, because `event_objects` links `TICKET:*` only from PR-side events; a
  story's `status_changed` / `sprint_added` / `worklogged` rows link `STORY:*` and `SPRINT:*`
  (`select object_type,count(*) from event_objects` on the probe store → `TICKET 334`, `STORY 66`,
  `SPRINT 12`). The `ticket=` filter matches `TICKET:` objects only (`query.py:109`), and
  `EVENT_PARAMS` (`query.py:30`) has no `object`/`story` filter.
- `GET /api/objects?type=STORY` gives `state, points, epic_key, created_at, closed_at` but **no
  events**, and `SPRINT` objects carry `end_date`/`goal_class` but not their member stories.

There is no `/api/ticket/{key}` (route list: tenants, health, calendar, events, events/counts,
objects, blocking, queues, delivery, insights, simulate, eval, screenshot).

The building blocks that *do* exist: `/api/objects?type=PR&ticket=DEL-167` already returns both
PRs, and the per-story segment rows are one query away. So a minimum ticket panel could be
assembled client-side from 3–4 calls today — but not the boundaries, worklogs, or the ticket's own
events.

### 1.7 Provenance omits the Jira half, and the live API predates it

- The Sources page (`main.js:978`) lists `tenant, store, source, snapshot, events, PRs, commits,
  dropped, window, duration basis`. It has **no stories/epics/sprints rows**, even though
  `/api/health` on the probe store reports `epics 1, stories 12, sprints 3`. The one screen whose
  job is "what is this made of" does not know the Jira layer exists.
- The app files served by `:8766` are current (md5 matches `app/`), but the API process is not: it
  started **2026-11-16 01:17**, while `analysis/jira.py` landed **05:36** and `prd/21` at **04:48**.
  The live `/api/insights` therefore has **no `jira` key and no `sample.stories`**:

  ```
  :8766 /api/insights?tenant=del3  keys: ok, generated_at, as_of, hours_mode, assumption, level,
                                        window, sample, targets, metrics, suggestions, note, tenant
                                   sample: {prs:131, days:90.6, merged:106, reviewed:131, threads:375}
                                   5 rules, none of them ticket-layer
  :8917 /api/insights  (worktree server on newer code)
                                   ... jira: {available:false, stories:0, ...}
  ```

  So on the live URL even the *aggregate* ticket rules are absent, and `rules.js:103` guards on
  `data.jira.available`, which the running API never sends.

---

## 2. What's missing

Symptoms of one root cause: **the ticket is a join key in the data and a label in the UI** — never
an object with a lifetime, and never a destination.

| # | Gap | Evidence | Severity |
| --- | --- | --- | --- |
| G1 | No story/epic/sprint object in any view (no lane, no nesting, no roll-up) | `timeline.js:437`; `PULL REQUESTS · 131`; 31 multi-PR tickets in `del3` | high |
| G2 | No ticket detail view — no single ticket's cycle with its PRs, rounds, sprint, worklog | drawer is `PULL REQUEST`-only (`main.js:1029`); `DEL-167` shows only `#52` | high |
| G3 | A ticket's history is not retrievable in one request (story events unfilterable; `ticket=` is PR-only) | `query.py:30,109`; `TICKET 334 / STORY 66 / SPRINT 12`; no `/api/ticket` | high (blocks G2) |
| G4 | The 8 segments exist only as cohort hours + a "largest" label; boundaries and per-story PRs are dropped before the API | `jira.py:431` vs `rows[]` shape | high |
| G5 | No sprint dimension: no sprint lane, no sprint band, no sprint scope; carry-over counted but not shown | `QUEUE_ORDER`; top bar = scope + timeframe | medium |
| G6 | No worklog view at all (`worklog` absent from `app/`): hours, log-delay buckets, logged-vs-active per story; even as an event it is a dot, not a span | rules text only; `int-actors-jira-events.png` | medium |
| G7 | Rules are unclickable: no drill-down from a rule to the stories/PRs behind it | `ruleRow()` emits static markup (`rules.js:68`); 0 links | medium |
| G8 | Story events render as unlabelled actor-lane dots; semantic zoom (epic→story→PR→event) never built | `prd/09:31`; `int-actors-jira-events.png` | medium |
| G9 | Censoring is dishonest at ticket level: an open PR's ticket reads as "merged, history" and the open half disappears | `DEL-167` #52/#53 | medium |
| G10 | Provenance/sources panel omits stories/epics/sprints; live API older than the layer it should show | `main.js:978`; `:8766` insights | low effort, high visibility |

Non-gaps (they make the fix cheap): the pseudonymisation contract needs nothing new (keys,
pseudonyms, closed attributes only); the **join is already proven and tested**
(`tests/test_jira_analysis.py::CombinedFixtureTests`); business hours, the calendar and the
honesty/assumption lines already apply to the Jira metrics; per-story segment rows are already in
the payload; and `/api/objects?type=PR&ticket=` already resolves a ticket's PRs.

---

## 3. To get real answers — prioritised

Ordered by *answers unlocked per unit of work*. P0–P3 turn aggregate assertions into a
ticket-centric product; P4–P6 make the timeline match `prd/09`; P7 is polish.

### P0 — Make the live instance current, and get Jira objects into a live store (hours, no code)

Two independent blockers, both outside the UI:

1. **Restart the API** (`:8766` runs Nov-16 01:17 code). Done when `/api/insights` returns a `jira`
   key and the Rules strip shows `· N stories` on a store that has STORY objects.
2. **Get real Jira data into a store.** `del3` has 101 ticket keys and no ticket objects, so even a
   perfect ticket page would say "the store cannot say" for `DEL-167`. The combined probe proves the
   pipeline works; the lab scrape is the missing input. This is *the* blocker to real answers.

Also in P0 (minutes): add `stories / epics / sprints` to the Sources panel, so the provenance line
matches `/api/health`.

### P1 — `GET /api/ticket/{key}`: one request = one ticket's whole life (backend, ~1 day)

Return, for a ticket key, everything the store knows, joined server-side where the identity
resolution already lives:

- **story object** — state, points, `summary_class`, epic, created/closed, assignee;
- **boundaries** — the nine `Cycle.boundaries` timestamps, with `missing: [...]` stated (never
  estimated — the module's existing honesty rule);
- **segments — the 8** with hours/share/efficiency, per story (not cohort), each with its
  `bounds` and an `available` flag;
- **PRs** — every PR naming the key (reuse `/api/objects?type=PR&ticket=`), each with its lifecycle
  intervals, review-round count, the `push_class` of the rounds that reset approvals, `state` and
  right-censoring;
- **sprints** — memberships in order (ids, start/end/state from the SPRINT objects), plus this
  story's `carry_over` context;
- **worklogs** — per-story rows (`time_spent_seconds`, `log_delay_bucket`, logged-at, reconstructed
  span) and logged-vs-active hours;
- **events** — the ticket's own events joined across `STORY`, `SPRINT` **and** the `TICKET` objects
  of its PRs. Fix G3 by extending the `ticket=` filter to match `STORY:<key>` too (or adding
  `object=STORY:<key>`), using `event_objects` as `jira.py` already does.

Tests (repo rule: every feature ships with them): a fixture story with all eight boundaries → the
route returns 8 segments and the 9 timestamps; a story with two PRs (one open, one merged) → both
returned; a store with no Jira → `available:false` with the existing note; the canary test still
passes (keys/pseudonyms only).

### P2 — Ticket page: the thing that answers "why did DEL-167 take 3 weeks" (~2–3 days)

A full page (not a drawer) in the existing visual language, top to bottom:

1. **Header** — `<key> · <summary-class> · <points> pts · epic <EPIC> · state <state> · in <n>
   sprints`, the cycle in business hours, and the censoring state ("1 PR still open").
2. **The 8-segment bar** — the same segmented-bar idea the PR gantt already uses, one bar for the
   ticket: `ready→refined | refined→sprint | sprint→in-progress | in-progress→PR-open |
   PR-open→first-review | review rounds | final-approval→merge | merge→done`, each with hours,
   share and flow efficiency; missing segments hatched "not measurable", never zero.
3. **PR rows under the bar** — each PR as its own lifecycle bar (reuse `timeline.lifecycle`), with
   review-round count, the `push_class` of each rework push, and the approvals it cost. This is the
   only view where "the ticket had 2 PRs, the second sat open for a month" is visible.
4. **Sprint strip** — which sprints it was in, carried-over marker, points at plan vs at done.
5. **Work the humans logged** — worklog hours, log-delay bucket, and the ratio against PR active
   hours (so "3 weeks" is not confused with "6 days of work").
6. **The answer line, in prose** — one sentence generated from the data, in the Rules voice
   (e.g. *"66% of the 21-day cycle was waiting: 9 d in `refined→sprint`, 6 d with the PR open
   across 3 rounds (2 reset by pure rebases), 2 d from merge to Done."*). The Rules engine
   already produces that shape per cohort; this is the same template with one story in the sample.

For `DEL-167` **today** the honest answer is "the store cannot say": the ticket's created, refined,
sprint and done timestamps do not exist and `TICKET:DEL-167` is empty (§1.1). That is the point of
the page — once the combined store exists it becomes answerable, and until then it says *which
boundary is missing* instead of showing a PR's `1 d 21 h` as the ticket's cycle.

### P3 — Make every ticket key a link (hours; biggest UX win per line)

`ticket` in the PR drawer and delivery detail, the timeline lane label, the `RELATED` chips, and
the rule evidence (`segment review rounds` → the stories in that segment) all become
`href → /?ticket=DEL-167` (client-side route over P1). Without this, even a perfect ticket page is
unreachable from where the questions are asked. Ship it in the same PR as P2 if possible.

### P4 — A `stories` lane dimension + epic nesting (timeline, ~2–3 days)

Add a fourth lane dimension `stories`: one row per STORY, grouped under its `EPIC` header, with the
8-segment bar per row and the ticket's PRs as indented sub-rows. Collapse/expand at
epic → story → PR is the "semantic zoom" `prd/09` and `prd/10` promise and `QA-round2 G3` records
as failed. Note the timeline currently draws the actor lanes *and* the full `PR LIFECYCLE` section
in one view (`timeline.js:484`), so this is a new section, not a replacement.

### P5 — Rules → evidence drill-down (~1 day)

Each rule row gets a "see the N" affordance listing `metrics.*.rows` (already in the payload, and
already carrying the keys — e.g. `PROJ-105` in `carry_over.rows`) with links to P2/P3, plus "scope
the timeline to these". This converts the only current ticket-layer surface (§1.5) from an
assertion into a starting point.

### P6 — Sprint dimension (~1–2 days)

Sprint bands as timeline background (start/end from the SPRINT objects), a `sprint` scope, and a
carry-over mark on story rows. Answers "which sprint ate the work" without leaving the timeline.

### P7 — Polish: censoring and assumptions at ticket level

Open stories and open PRs shown with the same right-censor arrow the PR bars already have; the
assumption footer extended with "Jira cohort: N stories created in window, M decomposed"; the PR
drawer's "closed: its wait is history" re-worded when the ticket still has an open PR.

---

## 4. Bottom line

The Jira half is **present in the data, in the analysis, and in one label** — and absent as a
*view*. Every screen is PR-centric: the timeline's object lanes are PRs, the drawer and detail
panel are PRs, the delivery list is PRs, and the ticket layer reaches the user only as ranked prose
with a story count. The join itself is finished and tested; what is missing is a representation of
the ticket as an object with a lifetime (P1+P2), a way to reach it (P3), and a way to see a set of
them (P4/P6). Until then "why did DEL-167 take 3 weeks" has no UI answer — and on today's live
instance it does not even have the aggregate one.

# 58 — The constraint verdict and the exploit→elevate ladder ("one answer")

Every view names a different bottleneck at a different magnitude (QA-final: 1.2× / 2.2× / 3.1× / 5.6× for one
person), and the Rules page recommends refine + cap rounds + add a sentinel + shrink PRs all at once — "accurate but
not decisive". This PRD makes **one card** name the single current constraint, in one sentence, and then walks
Goldratt's ladder — exploit → subordinate → elevate — with the policy lab's verdict on each rung, so *elevate* is
never proposed while *exploit* options are unrun. Conforms to `docs/concepts.md` (§2 holders, §5 segments, §6
"constraint ranking", "at stake") and to prd/10's design language.

## User journey

It is 18 Nov; sprint 3 closes on 27 Nov. A delivery lead (Priit) opens Overview: 8 open PRs, 3 waiting over a
week, 1 overloaded person. Rules says *refine first* (≈1.2k h at stake, lab "+82 %"), *cap rounds at 3*, *add a
second sentinel* ("no clear effect, −3 %"), *smaller PRs* ("+289 %"), and a banner saying the lab's best is *cap
review at 2 rounds (−13 %)*. Five answers, one of which contradicts its own banner.

The verdict card answers instead:

> **The constraint is the approval gate.** 620 business hours of removable review wait: 238 h currently owed and
> 382 h of abnormal tail. #60 (DEL-176) has been in review 144 h, waiting on FrMo.
> Before you elevate: 9 stories (2,903 h) sat in review with **no live PR** — their PRs were declined. Close or
> re-plan them; that is queue cleaning, not capacity.

The ladder below it reads: **Exploit** — chase the three orphans, review SLA (lab −9 %); **Subordinate** — cap
review at 2 rounds (lab −13 %, the best); **Elevate** — second sentinel, *blocked* (lab −3 %, "no clear effect").
He acts on the exploit rung and does not ask for headcount.

## Product design

**Where.** One card at the top of Overview (above the prd/54 chase card) and the same verdict as a one-line banner
at the top of Rules. No new page: the page question "What needs attention?" is answered by the sentence, and
"What should we change first?" by the ladder. The card is *not* a stat grid — it is a claim with its numbers and
the items behind it.

**Layout (desktop).** Full-bleed panel, hairline top rule, no shadow. Three bands:

1. `THE CONSTRAINT` (11 px uppercase overline) · one bold sentence (17 px) naming the station, the removable hours
   and the oldest live item · a 12 px muted meta line: `87 completed stories · 8 open PRs · story clock · as of 18 Nov 00:10`.
2. A four-row definition list, right-aligned tabular figures: `removable 620 h` · `owed now 238 h on 4 stories` ·
   `tail 382 h (above p85)` · `holders FrMo 961 h · peers 142–231 h` (the holder row is labelled `PR clock`).
3. The ladder: three rows, each `[step] [state pill] [≤3 rule titles → anchors] [lab note in italic]`.
   `Exploit` open, `Subordinate` open, `Elevate` **blocked** — grey outline pill, the reason in plain words, and
   the rung is collapsed to one line (not hidden; a blocked instruction must be readable).

Footer: `Next: verification 81 h · start 41 h · refinement 33 h` behind a `<details>` ("next constraint"), and
`Copy as Markdown`.

**Colour.** Structure neutral graphite. The constraint pill is a `--warning` tint (state: something is owed); the
rung pills are tints of the same state set — exploit open = warning, subordinate = neutral outline, elevate
blocked = neutral outline. **Never red**: a blocked elevation is a decision, not a fire. Zero/absent pools are
white. Numbers 15–17 px/700, units as `620 h`, keys mono (`#60`, `DEL-176`), durations `5 d 4 h`.

**Interactions.** Clicking a rung expands it in place (no modal); clicking an item opens the PR drawer (prd/25);
`Only this queue` scopes the console to that station; the Rules banner's "see the ladder" jumps to the card. On
Rules, levers **not** on the current rung collapse into `<details>` "Rules for the next constraint (N)" with a
persisted *Show all rules* toggle, while the `measured` rules (diagnostics such as survival or sprint on-time) keep
their ranked position — hiding a measurement is not a decision aid. No number changes: ranking stays impact ×
confidence, `at_stake` stays additive — only the default disclosure changes.

**Honesty language (exact copy).** `Pool = business hours on the team calendar; story segments for refinement and
verification, the PR ball clock for the review detail.` · `The model does not see the ticket-level wait, so the
exploit rung is the rules' claim, not the lab's.` · `No constraint worth naming this week` when the largest
removable pool is under one waiting week (40 h) — with the full ranking still in the disclosure.

**Phone (≤640 px).** The sentence keeps its words, the definition list drops to two rows (`620 h removable`,
`oldest #60 · 144 h`), and the ladder becomes three stacked rows with 44 px targets. The holder row moves into the
exploit rung. No chart, no horizontal scroll, no truncated keys.

## Technical design

### Definitions (add to `docs/concepts.md` §6)

- **Station** — one of six fixed stages of the story value stream: `refine` (`created→ready`), `plan`
  (`ready→sprint`), `start` (`sprint→in progress`), `develop` (`in progress→PR open`), `review`
  (`PR open→first review` + `review rounds` + `final approval→merge`), `verify` (`merge→verification` +
  `verification→done`). The stations **partition** the nine segments; a segment belongs to exactly one station.
- **Removable pool** — a station's open wait that an action can remove now: `live_hours` (in-station age of open
  stories with a live PR) + `tail_hours` (Σ over completed stories of `max(0, station_hours − p85_station)`, where
  `station_hours` is that story's sum over the station's segments). It is *not* the station's total, and it is never
  added to `history_hours`.
- **Constraint verdict** — the station with the largest removable pool, reported with its holder split, its ties
  (within 10 %), and its adjacent findings. One per store, window and repo scope.

### Data model

No new store fields. `analysis/verdict.py` (pure; takes `court`, `jira`, `window`, and the prd/43 advice payload):

| block | contents |
|---|---|
| `STATIONS` | ordered tuple of `{id, label, segments, clock}`; a module-level guard asserts the segments partition `jira.segment_keys(...)` |
| `pools()` | per station: `history_hours, n_completed, p50, p85, p95, tail_hours, live_hours, open_n, removable_hours` |
| `liveness()` | per open story: `live` (≥1 open PR) · `unmerged` (story done, ≥1 open PR) · `dead` (no open PR **and no human event for ≥10 business days**; bot sweeps don't count, §2) · `unobserved` (no open PR, recent human event) |
| `holders()` | `pr-holder` clock: `person_hours` per holder over review intervals (`Interval.holder_hours`), role split `sentinel/reviewer/author`, and the open PRs' current waits |
| `RUNGS` | `exploit → ("sentinel-tail","stale-prs","review-sla","review-tail","round-cap","jira-waiting-time","comment-tax","conformance-rounds","conformance-unapproved")`, `subordinate → ("cycle-time","pr-size","flow-efficiency","stacked-cascade","rebase-waste","vacation-blocking","jira-carry-over","jira-point-churn","jira-conformance")`, `elevate → ("sentinel-queue","reviewer-spread")`, `measured → ("time-to-merge","cycle-time-censoring","sprint-on-time","jira-worklog-mismatch","jira-worklog-noise")` — every `templates()` id appears exactly once (test-enforced). `measured` rules are diagnostics, not levers: they are never hidden by the ladder and never carry a rung |
| `ladder()` | per rung: `state` (`open`/`blocked`/`done`), its rules enriched with `impact_hours`, and the `advice` entry (`verdict`, `note`) for the mapped preset |
| `adjacent` | `dead` stories (story clock, count + hours), `unmerged` PRs (PR clock, count + hours), each with one sentence and a drill list |

### API — `GET /api/verdict?tenant=&days=&repo=`

```json
{ "ok": true, "as_of": "…", "hours_mode": "business", "calendar_id": "…",
  "window": {"start": "…", "end": "…", "days": 90},
  "constraint": {"station": "review", "label": "Approval gate",
    "removable_hours": 620.0, "live_hours": 238.0, "tail_hours": 382.0,
    "history_hours": 3328.0, "n_completed": 87, "p85_hours": 70.8,
    "hours_kind": "story", "tied_with": [], "basis": "…"},
  "holders": {"clock": "pr-holder",
    "people": [{"person": "FrMo", "role": "sentinel", "hours": 961.0, "open_review_hours": 144.2, "items": ["#60"]}],
    "current": [{"pr": "#53", "ticket": "DEL-167", "hours": 192.0, "holders": ["CaOs","FrMo"]}]},
  "adjacent": [{"kind": "dead", "n": 9, "hours": 2903.0, "hours_kind": "story", "sentence": "…", "items": ["DEL-114"]},
               {"kind": "unmerged", "n": 3, "hours": 349.0, "hours_kind": "pr-holder", "sentence": "…", "items": ["#53"]}],
  "ladder": [{"step": "exploit", "state": "open",
              "rules": [{"id": "round-cap", "title": "…", "impact_hours": 812.0,
                         "lab": {"available": true, "verdict": "…", "note": "the policy lab models this at −13 % cycle"}}],
              "note": "…"}],
  "next": [{"station": "verify", "removable_hours": 65.0}, …],
  "available": true, "note": "…" }
```

`available: false` with a stated reason (never a zero-filled card) when the window has fewer than 20 completed
stories, or when the largest removable pool is below 40 h (one waiting week).

**No simulation runs here.** The server passes the same cached `_advice_payload(...)` it builds for
`/api/insights`, so the rung's lab note is the number the Policy page shows. No advice → the rung says "the lab
has no verdict for this policy in this window".

### Analysis (precise)

1. `seg = jira.segment_cohort(court, jira)` → per segment, the completed cohort's hours.
2. Per station: `p50/p85/p95` from the station's segment sample (union of its segments' per-story sums, not the sum
   of segment percentiles); `tail = Σ_s max(0, s − p85_station)`; `history = Σ_s s`.
3. `live`: for each open story, in-station age = business hours from the last transition into the station to the
   snapshot, on the **story's** calendar; only items with `liveness == live`.
4. `removable = live + tail`; rank descending; `tied_with` = stations within 10 %.
5. Holders (review station only): review intervals' `holder_hours` grouped by person and role; `current` waits from
   the open PRs' last interval (`holders` = `ball_people`, the truth per §2).
6. Ladder state: `exploit` is `open` while any of its rules has `state=open` and a live item exists; `elevate` is
   `blocked` while `exploit` is open, with the reason "exploit first: N items are still owed". The `done` state is
   used when the rung's rules are all below threshold in the window.

### Cache and invalidation

Module-level `_verdict_cache` keyed `(tenant, store stamp, calendar key, days, repo, advice as_of)`, cleared like
`_trend_cache` (32 entries). The advice payload is already cached per (store, calendar, window), so the endpoint
adds one cohort pass, not five simulations. A new ingest or a calendar edit makes the old key unreachable.

### Edge cases

- **Tie** (≤10 % apart): both stations named, `tied_with` populated, and the card says "tied" (never picks silently).
- **Small n:** <20 completed stories → refuse; a single station over the floor still ranks, and the meta line
  states the n.
- **No Jira:** only `develop`/`review` pools exist (PR clock); the card says "this store has no ticket lifecycle".
- **One sentinel:** role-first copy ("the approval gate (FrMo)"), the pseudonym behind a label; bots are excluded
  from `holders` (§2) and a bot-only pool is reported as "no human holder".
- **Censoring:** live hours are ages at the snapshot, labelled as such; completed-only segments stay completed-only.
- **Dead WIP larger than the constraint:** the adjacent finding leads the card's second line, because ignoring it
  would make the verdict look like a capacity problem.

### Tests

`tests/test_verdict.py` — segments partition exactly (union == segment keys, no overlap); `history + live` per
station reconciles with an independent recount; `tail` maths on a 3-item fixture; liveness on del-combined
(DEL-176 `live`, DEL-114 `dead`, DEL-167 `unmerged`); bot sweep does not make a dead item `unobserved`; tie
detection at 9 % and 11 %; min-n refusal; `RUNGS` covers every `templates()` id exactly once, and `measured` rules never appear in a rung; `elevate` blocked
iff `exploit` open; sentinel named. `tests/test_server.py` — endpoint 200 shape, unknown repo → 400 listing repos,
`_advice_payload` called once across two requests, `available:false` body for an empty window.
`app/test/prd58.test.mjs` — card renders the sentence (no point-estimate headline); rung order; blocked rung has
its reason and is still readable; `<details>` next-constraint; Rules banner + collapsed non-rung rules; phone
markup has no chart and no overflow class; Markdown export includes the verdict and the guardrail-free lab note.
`tests/test_conformance.py` — no duration formatting outside the shared formatters.

## Definition of done

- On del-combined, the verdict is **review / approval gate**: `removable ≈ 620 h` (live 238 + tail 382),
  `tied_with: []`, next = verification 81 h · start 41 h · refinement 33 h.
- Adjacent findings: 9 `dead` stories / 2,903 h (DEL-114 first) and 3 `unmerged` PRs / 349 h (#53 192 h, #55
  155 h, #66 2 h); the holder split names FrMo (961 h review-phase, 1,882 h all phases) and peers 142–231 h.
- The ladder names #53/#55/#60 as the exploit items, cites round-cap −13 % and SLA −9 %, and **blocks** the
  second sentinel with "no clear effect" — byte-equal to `/api/insights` → `policy_lab` and `/api/simulate` for the
  same window.
- Rules shows the current rung's levers expanded and every other lever behind the disclosure; the `measured` rules
  keep their ranked position; the toggle restores the full list with no change to rank or `at_stake`.
- A fixture store with 12 completed stories refuses with a reason; both corpora and all existing suites green.

## Builds on / non-goals

Builds on **43** (the lab's verdict per rule), **55** (one number per concept), **40** (holders, status vector),
**54** (the chase card this ladder sends the lead to), **59** (the aging list is the exploit rung's data source),
**57** (the tail is a cohort contrast). Amends `docs/concepts.md` §6 with *station*, *removable pool*, *constraint
verdict*.

Does **not**: score or rank people (a person appears only as a holder, behind a role label); auto-recommend hiring;
re-rank rules by impact (ranking stays impact × confidence); add investment/allocation; run new simulations; add a
pager or real-time alerting; define a second "review wait" (the verdict reuses the prd/40 ball clock).

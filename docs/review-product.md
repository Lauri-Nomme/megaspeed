# Product review — megaspeed vs. its goal

**Scope:** product-level completeness, not code quality. Does the shipped thing answer the
question the project exists to answer?
**Read at:** `6a8a426` (working tree clean). **Method:** read the PRDs (`prd/01`, `prd/05`,
`prd/07`, `prd/09`, `prd/11`, `prd/15`, `prd/16`, `prd/17`, `prd/18`, `prd/19`), read the
implementation, ran the test suites, and executed the analysis layer against both stores that
exist on this machine (`data/del.duckdb` = the real DEL corpus, `data/megaspeed.duckdb` = the
fixture corpus). Every number quoted below was measured, not estimated. Nothing in the repo was
modified except this file.

---

## 1. Verdict

**The measurement engine is real; the answer engine is half-built; the "where does time go"
question is only answered for the PR slice.**

Concretely:

- **Ingest is the strongest layer — genuinely done.** A scrape produces a pseudonymised tgz in
  one pass, with `push_class` from a shallow git cache, closed-vocabulary LLM labels for
  comments/threads/questions, PR diff size, ticket-set scoping, a watermark, a cheap pre-filter,
  settled-PR finality and early termination. The airgap contract (no identity map in the bundle,
  checksums, monotonic watermark, `--verify` over every string column plus a canary) is enforced
  in code and tested, not aspirational.
- **Analysis is uneven.** Ball-in-court, the blocking graph, the queue view (Little's law) and
  the delivery list are complete and coherent (all four are projections of one interval set, so
  they agree by construction). Nine prd/17 metrics are implemented with ranked, evidence-backed
  suggestions. But on the **real** store, four of those nine are silent, and four of prd/01's
  eight goals have no implementation at all (flow efficiency, cascade cost, survival analysis,
  simulation).
- **The UI ships 6 real views + 2 stubs**, in plain canvas JS rather than the SvelteKit/sigma/uPlot
  stack PLAN.md chose. The two views the PRD ranks #1 and #2 as *outputs* are, respectively,
  done (timeline) and not built (simulation).
- **The single biggest gap is not code, it is data plumbing:** the live store predates
  `push_class` + the LLM classifier, so today's live product cannot answer prd/15 Q2 (rebase
  waste), Q4 (comment tax), the PR-size rule, or the sentinel queue. The code for all four
  exists and passes tests; none of it fires on real data yet.
- **The second biggest gap is the missing Jira layer.** Without Story/Epic/Sprint/Tempo, the
  product measures the *review* half of the delivery pipeline and silently omits the half the
  PRD says dominates (ready→refined batching, refinement questions, carry-over, point churn,
  worklog cross-check). The headline "where does time go" is therefore answered for PRs only.

Scorecard (share of the capability as specified that is actually usable today):

| Layer | Status | Score | Why |
| --- | --- | --- | --- |
| Airgap / pseudonymisation | **done** | 95% | contract enforced + canary + tests; JSONL-only transfer |
| Bitbucket DC scrape | **done** | 90% | activities, scoping, watermark, push_class, diff size, LLM labels |
| Import → store | **done** | 95% | idempotent, watermark-guarded, verified on load |
| Jira Cloud + Tempo | **missing** | 0% | no code, no schema objects, no events |
| Slack | **missing** | 0% | de-prioritised in the PRD; nothing built |
| Ball-in-court / blocking / queue / delivery | **done** | 85% | one interval set, four agreeing views |
| prd/17 rules engine | **partial** | 60% | 9 metrics implemented; 4/9 silent on the live store |
| Business-hours calendar | **missing** | 5% | PRD only (`prd/19`) at the reviewed commit; every number is wall-clock (an implementation is in flight, uncommitted — see §7.3) |
| Timeline UI | **done** | 85% | canvas LTR, 3 lane dims, phase bars, LOD, zoom/pan |
| Other UI views | **partial** | 75% | overview/delivery/blocking/queue/rules/sources real; policy-lab stub |
| Simulation / policy lab | **missing** | 5% | explicit stub page |
| prd/15 Q1 reviewer lag | **partial** | 65% | implemented and fires; attribution model limits it |
| prd/15 Q2 rebase waste | **partial** | 45% | fully implemented, zero signal on live data |
| prd/15 Q3 stacked cascade | **missing** | 0% | no stack edges, no cascade analysis |
| prd/15 Q4 comment tax | **partial** | 35% | implemented, unclassified on live data |

---

## 2. The goal, restated as five testable claims

From `prd/01` + `prd/15`:

1. **Mine** the delivery pipeline (Jira/Bitbucket/Slack) into a pseudonymised event log.
2. **Keep raw content out of everything downstream** — airgap + pseudonymisation by construction,
   no LLM ever sees source prose after classification.
3. **Answer**: where does time go (waiting-time decomposition + flow efficiency), who blocks whom
   (weighted handover graph), what is the queueing reality (Little's law), what is the
   rebase/review/comment tax.
4. **Turn it into suggestions/rules** — a ranked "change this first" list with evidence.
5. **Test interventions before inflicting them** (calibrated simulation) and give a daily
   "what should I do next" list.

Claim-by-claim: **1 is one-third done** (Bitbucket only), **2 is done**, **3 is done for PRs and
missing for stories**, **4 is done as a mechanism and half-silent as a result**, **5 is a stub**
(the daily list exists in a simplified form as the Delivery view).

---

## 3. Ingest — assessment

Everything below is in `ingest/` (≈5.3k lines) plus the stdlib-only `scrape/` build; `scrape/build.sh`
+ `.github/workflows/build-scraper.yml` produce a onefile artifact for the airgapped box.

| Capability | Status | Evidence |
| --- | --- | --- |
| Bitbucket DC activities stream | **done** | `ingest/bitbucket.py`, `pipeline.py`; real action names verified against DC 9.4.18 (`taxonomy.PR_ACTIVITY_MAP`) |
| Ticket-set scoping (`MEGASPEED_TICKET_KEYS`/`_FILE`) | **done** | `pipeline.KeyFilter` (exact set replaces the prefix list), enforced again in `store.verify`; `tests/test_ticket_keys.py` (424 lines) |
| Cheap pre-filter before the expensive calls | **done** | `scrape.collect` layer 1: branch/title/description key check, then detail/commits; `prs_dropped` counted |
| Watermark + settled-PR finality + early termination | **done** | per-repo watermark, strict `<` boundary, newest-first pagination stops at the first page entirely below the watermark; `tests/test_incremental.py`, `tests/test_scrape_incremental.py` |
| `push_class` (content / pure_rebase / rebase_conflict) | **done** | `gitclass.PushClassifier`: shallow `--filter=blob:none` cache, `git patch-id` + `range-diff`; real-git tests in `tests/test_push_class.py` (703 lines) |
| PR diff size | **done** | `gitclass.annotate_diff_size` → `PR.attributes.lines_added/removed`; absent means "pending", never zero |
| LLM comment/thread/question classification | **done** | `ingest/classify.py`: Ollama (`qwen3-30b-a3b`) + deterministic mock, one JSON schema; `thread_outcome` is hybrid (git diff at the anchor first, text intent only when the diff is unknown); bot comments never sent to the model |
| Pseudonymised tgz + import | **done** | `bundle.py` (4 members, deterministic tar, sha256sums, no identity map) → `importer.py` (checksum → schema/classifier version → watermark → contract verify) |
| Airgap correctness | **done** | no identity map in the bundle; no source credentials on the main side; `--verify` rejects prose-shaped strings, unknown enums, unknown tables/columns and the `zzcanary` token; classifier runs scrape-side only |

**Caveat that matters for the product (not the code):** the classifier is implemented and
benchmarked (340 comments + 131 descriptions, median 1.86 s/call, 0 schema violations,
`scrape/bench/llm-bench.md`), but it has **never completed a run into a store on this machine**.
`data/del.duckdb` was scraped before `push_class` and the LLM existed: all 202 `pr_pushed` events
carry `push_class=other`, all 340 `THREAD` objects have `comment_type=NULL` /
`thread_outcome=NULL`, no `PR` object carries a diff size, and there are **zero**
`question_raised`/`question_answered` events. The re-scrape is listed as in progress in
`STATE.md`; until it lands, every scrape-side capability is "built, not yet exercised in
production".

---

## 4. Analysis — assessment

`analysis/ballcourt.py` (697 lines) + `analysis/rules.py` (1102 lines). Both read the schema
directly, so they cannot be broken by ingest refactors.

### 4.1 The projections — done

| Capability | Status | Notes |
| --- | --- | --- |
| Ball-in-court state machine | **done** | phases waiting/active/review/ci/rebasing/closed/unobserved; measured vs inferred is explicit; censored tail is `unobserved`, never a phase |
| Blocking graph `X → Y = N h` | **done** | `blocking_graph`, role/queue and person level, WIP-now per node, per-day rates |
| Queue view (Little's law) | **done** | `queue_series`: WIP/arrivals/completions per bucket, slow-vs-overloaded diagnosis against the window's median wait |
| Delivery list (daily triage) | **partial** | `delivery_list` ranks open PRs by `dependents × waiting_hours`; that is a defensible proxy for WSJF but there are no story-level dependents and no cost-of-delay weighting |
| Flow efficiency (active ÷ elapsed) | **missing** | no code anywhere (`grep -i efficien` → nothing) |
| Waiting-time decomposition (8 named segments) | **partial** | only the PR-internal phases exist; ready→refined, refined→sprint, sprint→in-progress, in-progress→PR-open, merge→done all require Jira |

### 4.2 The prd/17 metrics — implemented, but 4/9 silent on real data

`GET /api/insights` computes all nine metrics and ranks suggestions by
`impact_hours × confidence` with stated thresholds. Measured on both stores:

| Metric | Code | `data/del.duckdb` (real) | `data/megaspeed.duckdb` (fixture) |
| --- | --- | --- | --- |
| `time_to_first_review` | done | median **4.39 h**, p90 68.3 h, 21/130 over 24 h | median 3.37 h |
| `cycle_time` | done | median **100 h (4.2 d)**, p90 142 h, 78/107 over 3 d | median 123 h |
| `reviewer_responsiveness` | done | per-person review medians **2.88 h → 21.15 h** (bots 1278 h); `flagged=[]` | `flagged=[]` |
| `rework_loops` | done | mean 1.63 rounds, p90 3; `by_push_class = {other: 202}` | p90 5 rounds; classes present |
| `rebase_waste` | done | **0 approvals attributed** (all pushes `other`) | **10 lost on pure rebases** → critical rule |
| `comment_tax` | done | **`available: false`** — 340 threads, 0 classified | `available: false` (no THREAD objects at all) |
| `conformance` | done | 0 merged-without-approval, 4 PRs over 3 rounds | 1 merged without approval, 10 over 3 rounds |
| `sentinel_queue` | done | **`available: false`** — no sentinel labels in DEL | available, median 2.62 h |
| `pr_size` | done | commits only; **`diff_size.pending: true`** | commits only, pending |

Resulting suggestion lists: **DEL → 4 rules** (cycle-time, rework-loops, review-tail,
conformance-rounds). Fixture → 5 rules including `rebase-waste` and `conformance-unapproved`.
The engine's own empty-state copy is honest ("that is a result, not a missing screen"), which is
good product behaviour — but a user on the real store is told nothing about rebase waste, comment
tax, PR size or the sentinel queue even though all four are implemented.

### 4.3 Analyses the goal requires that do not exist

| Missing | PRD | Why it matters |
| --- | --- | --- |
| Stacked-PR cascade cost / critical path | prd/01 §5, prd/05 §cascade, prd/15 Q3 | the `stacked` attribute exists in the taxonomy but **is never emitted**; no stack edges, no DAG, no cascade. The only stacked-PR data in the repo is in the synthetic `corpus/generate.py` |
| Survival analysis (KM/Cox) on censored cycle time | prd/01 §7, prd/05 §drivers | all reported numbers are medians over merged PRs only — open PRs are dropped from `cycle_time`, i.e. the exact censoring bias prd/05 warns about |
| Per-round rework cause attribution | prd/05 §rework loops | rounds are counted and pushes are split by `push_class`, but a round is never labelled "mandatory comment / accepted suggestion / rebase / conflict"; `comment_tax` is a separate aggregate, not per round |
| Question-phase distribution | prd/05 §question lens, prd/15 "what else" | `question_raised.phase` is produced by the classifier but **no metric, API field or view reads it**; and without Jira only `implementation`/`review` phases are reachable |
| Business-hours waits | prd/03, prd/19 | `hours_mode` is hard-coded `"wall-clock"`; nights and weekends count as wait. Every headline number above is inflated by an unstated factor |
| Simulation / policy lab | prd/01 §8, prd/09 view 4, prd/07 S9–S10 | stub page, no model, no `/api/simulate` |

---

## 5. UI — assessment

`app/` is dependency-free canvas JS (7 modules, ≈4.3k lines), not the SvelteKit/sigma.js/uPlot
stack in `PLAN.md`. That is a reasonable product decision (no build step, no Node), but it means
`prd/09`'s technology section no longer describes the product.

| View | Status | What it does |
| --- | --- | --- |
| Timeline (`#page-timeline`) | **done** | canvas LTR axis, lanes by actors / PRs / queues, activity markers, gantt phase bars, histogram, brush-to-timeframe, semantic-ish LOD, zoom/pan/keyboard, tooltips with pseudonyms + closed attributes only |
| Delivery list | **done** | open PRs ranked by impact, blocked/oldest tabs, detail pane, pivots, "copy as Markdown" — the closest thing to the daily "what next" report |
| Blocking map | **done** | weighted directed graph, role/queue and person level, lenses, click-through to that node's queue |
| Queue | **done** | WIP/arrivals/completions series per person or queue, slow-vs-overloaded diagnosis, click-to-scope |
| Overview | **done** | 4 headline stats + needs-attention / where-waits-accumulate / queue-diagnosis panels |
| Rules | **done** | ranked suggestions, severity tint, numbers-first, evidence as inline facts, team/person toggle, Markdown export |
| Tenants | **done** | top-bar dataset switcher, one DuckDB store per tenant, per-tenant identity space (`prd/12`) |
| Sources | **done** (thin) | provenance: tenant, store, snapshot age, events, PRs kept/seen, window, wall-clock disclosure |
| Policy lab | **missing** | explicit "Not built in this slice" stub |
| Epic/sprint filters, story lanes | **missing** | no epic/sprint dimension exists to filter on |

Honesty affordances are unusually good and should be counted as product value: `wall-clock hours`
is labelled in the header and on every analysis page, `unobserved` tails are hatched rather than
painted, and every analysis payload carries an `assumption` string.

---

## 6. prd/15's four questions, mapped to reality

| Question | Spec | What exists | Live signal | Verdict |
| --- | --- | --- | --- | --- |
| **Q1** — is a reviewer slow / do they prefer their own PR work? | per-person `PR-ready → first review`, self-vs-other contrast | `rules.time_to_first_review.by_reviewer`, `rules.reviewer_responsiveness` (review-ball vs author-ball medians), `flagged` contrast, `review-sla` / `review-tail` / `reviewer-spread` / per-person `reviewer-<pseudonym>` rules | **yes**: DEL review waits 2.88 h–21.15 h across people; team median 4.79 h; p90 first review 68 h | **mostly done** |
| **Q2** — conflict / rebase / lost-review lag | `push_class`, approvals lost on pure rebase, avoidable-vs-real rework | `gitclass` patch-id/range-diff; `rework_loops.by_push_class`; `rebase_waste` + `rebase-waste` rule ("tie auto-unapprove to patch-id change, not any push") | **no**: DEL pushes are all `other`, `lost_on_pure_rebase=0`; only the fixture shows 10 lost approvals | **built, unexercised** |
| **Q3** — are stacked PRs worse? | stack DAG, cascade cost, critical path, stack-head latency | nothing. `stacked` is a declared-but-unused attribute; no target-branch edge model, no cascade analysis | **no** | **missing** |
| **Q4** — who posts BS comments / what is the comment tax? | `comment_type` × `thread_outcome`, cost per comment class, nit/question rules | `classify.py` (6 comment types, 4 outcomes, hybrid diff-anchor outcome), `THREAD` objects, `comment_tax` matrix + `comment-tax` rule ("nits/out-of-scope → ticket; questions → refinement") | **no**: 340 threads, 0 classified on DEL | **built, unexercised** |

Additional prd/15 metrics: **time-to-first-review** done; **time-to-merge** done (as
`cycle_time`); **PR size vs review latency** implemented and waiting on diff size; **question-phase
distribution** absent; **conformance** two of four checks; **reviewer load balance** done
(slow vs overloaded); **sentinel queue** implemented, unavailable on DEL because no sentinel slugs
are configured for the DEL pseudonyms.

---

## 7. The biggest gaps between the stated goal and what is built

Ranked by how much of the product's promise they withhold.

1. **The live product cannot answer three of its four headline questions.** `push_class`,
   `comment_type`/`thread_outcome` and diff size are all implemented and tested, but the only
   real store predates them. Until the DEL `--full` re-scrape lands, Q2/Q4/pr-size/sentinel are
   "coming soon" in a shipped product. *This is the highest-leverage fix and it is not code.*
2. **No Jira layer at all.** Story/Epic/Sprint, Tempo worklogs, the changelog, the story↔PR glue
   and the "blocks" links do not exist. Consequences: no waiting-time decomposition, no
   carry-over, no point churn, no "work started before refinement", no worklog cross-check, no
   refinement batching delay, no epic/sprint filters, and question phases limited to
   implementation/review. The PRD's own framing ("the story layer is where the waiting is") is
   the part that is missing.
3. **Wall-clock hours everywhere.** prd/03 says without a calendar "the analysis is meaningless";
   `prd/19` exists as a design only. A 4.2 d median cycle time, a 68 h p90 first review and a
   58.95 h "cost per review round" all include nights and weekends, so every ranking and every
   impact-hour estimate is inflated by an unknown, person-dependent factor. *(Status at the
   reviewed commit `6a8a426`; a calendar is being built on the working tree
   — `analysis/calendar.py`, `config/calendar.default.yaml`, a modified
   `analysis/ballcourt.py` — so this gap may close shortly. Until it is wired into
   `rules.py` and the API's `hours_mode`, every number above stays wall-clock.)*
4. **Stacked-PR cascade (Q3) is absent end to end** — no attribute emission, no DAG, no cascade
   cost, no critical path. The PRD calls this one of the two things no competing product does;
   today megaspeed does not do it either.
5. **No flow efficiency and no survival analysis.** The two most quantitative goals in prd/01
   (active ÷ elapsed; Kaplan–Meier/Cox on censored data) are missing, and `cycle_time` silently
   conditions on merged PRs — the exact bias prd/05 §drivers warns about.
6. **No simulation / policy lab.** prd/01's #2 output and prd/07's S9–S10. The product can say
   *what is wrong* but not *what would happen if we changed it*, which is the difference between
   a dashboard and a recommendation engine.
7. **Person-level blocking attribution measures responders, not blockers.** `_person_for` assigns
   a review wait to "the reviewer whose action ended the wait" and collapses multi-reviewer PRs
   into one `review` queue. A reviewer who never responds never appears; a PR waiting on three
   reviewers is charged entirely to whoever spoke first. So "who blocks whom" at person level is
   systematically optimistic about slow non-responders — the exact people Q1 wants to find.
   (Role/queue level is unaffected.)
8. **The suggestion engine's impact model is a single scalar.** `impact_hours = count ×
   hours_per_round` (or `× median excess`), where `hours_per_round` is the median PR cycle ÷ its
   rounds (58.95 h on DEL). That is defensible but coarse, it is never validated against a
   counterfactual, and it inherits the wall-clock bias — so the "≈3.0k h at stake" headline is
   the least trustworthy number on the most prominent screen.
9. **Slack is absent** (deliberately de-prioritised, but it is one of the three named sources and
   the question/answer signal is the thinnest part of the current model).
10. **Minor but real:** bot review queues are excluded from person rules but included in
    team-level review aggregates (DEL `review_n=366` includes a bot with a 1278 h median wait);
    `delivery_list`'s "dependents" is same-ticket open PRs, not a real dependency graph; the
    timeline has no phase filter; `PLAN.md`'s stack and `docs/DEV.md`'s "no Node" decision have
    diverged from `prd/09`'s technology section.

---

## 8. What the product can honestly claim today

- "We mine your Bitbucket history into a pseudonymised event log, and no raw prose ever reaches
  the analysis side." — **true, and enforced.**
- "We can show where time goes inside the PR lifecycle, who waits on whom, who is overloaded vs
  merely slow, and what is sitting in a queue right now." — **true for the PR slice.**
- "We rank the changes that would buy back the most hours, with the evidence for each." — **true
  as a mechanism; on the live store, 4 of 9 metrics are silent and the hours are wall-clock.**
- "We can tell you the rebase tax, the comment tax and whether stacked PRs are hurting you." —
  **not yet: implemented, no data.**
- "We can simulate a policy change before you make it." — **not built.**

A fair one-line verdict: **megaspeed today is a strong, honest PR-flow miner with a working
suggestion engine, not yet the delivery-pipeline process miner the PRDs describe.** The distance
is concentrated in three places — Jira, business hours, and getting the already-written
classifiers onto real data — plus two genuinely unbuilt analyses (cascade, simulation).

---

## 9. Evidence / how to reproduce

```
.venv/bin/python -m unittest discover -s tests          # 228 tests, OK
.venv/bin/python -m unittest discover -s scrape/tests   #  46 tests, OK
```

Live-store measurements quoted above were produced by loading `data/del.duckdb` through
`analysis.ballcourt.load` + `analysis.rules.insights` (read-only connection), and cross-checked
with direct SQL on `events`, `objects` and `ingest_runs`:

```
select activity, count(*) from events group by 1;                                  -- 1316 events, 8 activities
select attributes->>'push_class', count(*) from events where activity='pr_pushed' group by 1;  -- all 'other'
select attributes->>'comment_type', count(*) from objects where object_type='THREAD' group by 1; -- all NULL
select count(*) from objects where object_type='PR' and attributes->>'lines_added' is not null; -- 0
select source, as_of, prs_seen, prs_kept, events_written from ingest_runs;         -- one run, 2026-11-15
```

Capability claims are anchored to: `ingest/gitclass.py` (push_class, diff size, thread anchors),
`ingest/classify.py` (closed-vocabulary LLM), `ingest/bundle.py` + `ingest/importer.py` (airgap),
`ingest/scrape.py` (watermark/pre-filter/finality), `analysis/ballcourt.py` (intervals, graph,
queues, delivery), `analysis/rules.py` (9 metrics + templates + ranking), `server/app.py`
(`/api/{blocking,queues,delivery,insights}`), `app/*.js` (8 nav views, 2 of them stubs),
`ingest/taxonomy.py` (closed vocabularies; note `stacked` declared but never emitted).

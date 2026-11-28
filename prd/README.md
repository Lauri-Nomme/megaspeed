# megaspeed

> Process-mining our own software delivery pipeline to cut cycle time —
> without ever letting raw ticket text, commit messages, or diffs out of the building.

## What this is

A product plan (PRD) for a tool that ingests **Bitbucket Data Center**, **Jira Cloud** (+ **Jira Cloud Tempo**), and **Slack**
metadata, reduces it to a **pseudonymised, object-centric event log**, and then
answers one question repeatedly: *who is blocking whom, and what is the cheapest
thing we can change to maximise delivery?*

This directory is **design only**. No implementation lives here yet.

## Why the name "megaspeed"

The project exists to make delivery *mega fast* — to minimise cycle time, kill
review queues, and stop rebase churn from eating the week. The name is the
obvious, almost naive, wish for what we actually want: **mega speed**.

Which is exactly why it's funny right now.

As of November 2026, **Megaspeed International** is the Singaporean data-centre
firm at the centre of the Nvidia / Jensen Huang scandal — under police
investigation for "suspected breaches of our domestic laws", accused of being
the conduit through which Nvidia GPUs allegedly reached China, and entangled in
a honey-trap espionage story that has the entire chip industry watching its
every move.

So the name lands on three levels at once:

1. **Literal** — we want our delivery to go at *mega speed*. Fine. Earnest.
2. **Ironic** — we named the "go faster" project after a company whose name
   promises speed but whose speed is now famous for being the wrong kind of
   speed: chips *allegedly* out the door before anyone noticed, a reputation in
   free fall, a board meeting to decide whether the founder keeps his job.
   Our own "megaspeed" is a project about *slowing down to observe* the process
   — the exact opposite of blindly shipping.
3. **Sarcastic** — the whole tool is a surveillance apparatus pointed at
   ourselves (who holds the ball, who blocks whom, whose reviews are the
   bottleneck). Megaspeed the company is, at this moment, the most surveilled
   company on the planet. We are building a *small, internal, voluntary* version
   of being watched — because we asked for it.

It is a name that will read differently in a year, which is fine. The
pseudonymisation requirement (see `02-pseudonymization.md`) means the tool we
build here is, functionally, the *anti-Megaspeed*: we make sure the interesting
stuff never actually leaves, under a fixed pseudonym, while they allegedly
couldn't account for tens of thousands of GPUs.

## Document index

| File | Contents |
| --- | --- |
| `01-context.md` | Problem, goals, non-goals, scope |
| `02-pseudonymization.md` | **The special requirement** — closed-vocabulary pseudonymisation, local classification, threat model |
| `03-data-model.md` | Object-centric event log (OCEL 2.0), object types, events |
| `04-exports.md` | Bitbucket DC / Jira Cloud (+ Tempo) / Slack export specs, push classification |
| `05-analysis.md` | Ball-in-court, blocking graphs, queueing, rework, drivers, simulation |
| `06-architecture.md` | Components, tech choices, raw↔pseudonymised boundary |
| `07-milestones.md` | Delivery slices, ingest & output priorities |
| `08-risks.md` | Risks: pseudonymisation/opsec, Goodhart, data quality, open questions |
| `09-web-ui.md` | Web interface: timeline viz (#1), blocking/queue views, simulation UI (#2) |

## Status

Draft. Iterating in committed slices. **No implementation yet.**
| `10-ux-look-feel.md` | Design language & UX spec |
| `11-airgap-ingest.md` | Airgapped ingest (scrape → transfer → import) |
| `12-tenant.md` | Tenants (parallel datasets) |
| `13-scraper-build-ci.md` | Scraper build & CI |
| `14-scrape-optimization.md` | Scrape optimization (huge projects) |
| `15-analysis-questions.md` | Analysis questions → metrics → suggestions |
| `16-scrape-classification.md` | Scrape-side classification: `push_class` + local LLM |
| `17-analysis-rules.md` | Analysis → suggested rules |
| `18-ticket-set-scope.md` | Ticket-set scoping |
| `19-business-hours.md` | Business-hours calendar |
| `21-jira-analysis.md` | Jira-layer analysis |
| `22-stacked-cascade.md` | Stacked-PR cascade |
| `23-simulation.md` | Simulation / policy lab |
| `24-cloud-jira.md` | Cloud Jira scrape (via a DC→Cloud emulation proxy) |
| `25-ticket-view.md` | Ticket-centric view |
| `26-review-backlog.md` | Mined-but-unimplemented insights (review backlog) |
| `27-epic-scope.md` | Epic scoping |
| `28-mobile.md` | Responsive design (desktop + phone) |
| `29-status-stages.md` | Configurable workflow stages (real support for the install's statuses) |
| `30-tempo.md` | Tempo worklogs (DC + Cloud) |
| `31-hans-import.md` | Bitbucket import from a hans scrape |
| `32-versioning.md` | App versioning (tag + short SHA in the UI) |
| `33-performance.md` | Performance hardening (from the adversarial review) |
| `34-actor-constraint.md` | Actor & constraint correctness (bots, sentinel, scope) |
| `35-time-unit.md` | One time unit: one "d", one clock |
| `36-lifecycle-honesty.md` | Ticket lifecycle honesty & labels |
| `37-sprint-rules-policy.md` | Sprint, rules & policy-lab consistency |
| `38-navigation-papercuts.md` | Navigation & papercuts |
| `39-pr-size-calibration.md` | PR-size calibration (distribution + sliders) |
| `40-review-model.md` | Review model: participant-status vector + author debt + `ready` (the middle ground) |
| `41-second-order-correctness.md` | Second-order correctness (the numbers that are still wrong) |
| `42-second-order-features.md` | Second-order features & presentation (the gaps the fresh pass still hit) |
| `43-reconcile-advice.md` | Reconcile the Rules and Policy-lab advice (and close the last gaps) |
| `44-cross-qa-reconciliation.md` | Cross-QA reconciliation: the still-unfixed, still-un-PRDd findings |
| `45-concepts-conformance.md` | Concepts conformance: reconcile the implementation against the glossary |
| `46-final-qa-reconciliation.md` | Final-QA reconciliation (segments, diagnosis consistency, rework/worklog) |
| `47-complete-final-qa.md` | Complete the final-QA backlog (index) |
| `48-navigation-back.md` | Navigation history & back |
| `49-repo-scope.md` | Repo scope dimension |
| `50-trend.md` | Trend / time-series view |
| `51-story-rework.md` | Story-level rework view |
| `52-sprint-at-risk.md` | Sprint-at-risk forecast |
| `53-worklog-legend.md` | Worklog-vs-points honesty + pseudonym legend |
| `54-review-sla-chase.md` | Review SLA / chase action surface |
| `55-diagnosis-consistency.md` | Diagnosis consistency & honesty fixes |
| `56-ui-papercuts.md` | UI papercuts (layout + interaction) |
| `57-benchmark-contrast.md` | Benchmark contrast: every specific number next to its cohort median |
| `58-constraint-verdict.md` | The constraint verdict and the exploit→elevate ladder ("one answer") |
| `59-aging-wip.md` | Aging WIP against the team's own percentiles ("age vs your own p85") |
| `60-throughput-forecast.md` | Probabilistic throughput forecast ("how many, by when") |
| `61-intervention-trend.md` | Intervention-aware trend ("did the change work — or can't we tell?") |
| `62-first-pass-yield.md` | Rolled first-pass yield and a guardrail beside every speed lever |
| `63-v010-qa-fixes.md` | v0.10.0 QA fixes — COMPLETE checklist (every finding from both passes) |
| `64-v011-explore-fixes.md` | v0.11.1 free-exploring QA fixes — COMPLETE checklist (all 22 findings) |
| `65-v012-final-qa.md` | v0.12.0 final-QA fixes (N-01 … N-10) |
| `66-jira-team-scope.md` | Team-scoped Jira import (the team field) |
| `67-window-scoping.md` | window scoping for people, lanes, aging and the constraint verdict |
| `68-v014-qa-fixes.md` | v0.14.0 QA fixes (F-01 … F-16) |
| `69-llm-trace-and-relabel.md` | Raw-zone LLM trace and label-only re-classification |
| `70-v017-qa-fixes.md` | v0.17.3 combined-corpus QA fixes (F-01 … F-15) |
| `71-identity-rekey.md` | Identity maintenance: one `rekey` command |
| `72-sprint-refresh.md` | Sprint metadata refresh |
| `73-performance-v2.md` | Performance long-tail v2 (draft; measure before dispatch) |
| `74-scrape-classification-cache.md` | Scrape classification cache (draft; after the relabel workflow settles) |
| `75-label-prompt-v2.md` | Label prompt v2, CI/admin taxonomy and the anchor-diff verdict |
| `76-reply-events.md` | Replies as first-class events (v0.21 design; implemented) |
| `77-policy-lab-cache.md` | The policy-lab result survives a restart (v0.21.1) |
| `78-no-fit-filter.md` | Deterministic no-fit/notice filter (v0.22.0; measured, scope corrected) |
| `79-author-notes.md` | Author-authored openers are not review remarks (implemented v0.24.0) |
| `80-browser-jira-titles.md` | Ticket titles resolved in the browser (implemented v0.25.1) |
| `81-first-review-is-human.md` | A first review is a human review remark, not a bot comment |

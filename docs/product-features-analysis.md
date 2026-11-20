# Product features analysis — theory, practitioner discourse, competitors, and gaps

> Research writeup for megaspeed. Web research only (no code changes). Date of research: 2026-11-20.
> Read this with `docs/concepts.md` (what the numbers mean) and `docs/QA-final.md` (current capabilities + known gaps).
> **Evidence grading** used below: **[P]** primary (paper, framework authors, the vendor's own docs),
> **[S]** secondary (a blog/analyst describing something else), **[V]** vendor marketing (treat as a claim, not a fact).
> The field is noisy and much of the competitive material is vendor-authored or SEO comparison content; see §6 for what I could not verify.

---

## 0. TL;DR

- **Theory converges** on a small core: *work flows through queues; most elapsed time is waiting; WIP, throughput and cycle time are
  tied together (Little's Law); the system has one constraint at a time (Theory of Constraints); measure teams/systems not
  individuals; use several metrics in tension*. DORA measures the **pipeline** (commit→prod, now five metrics); flow metrics measure
  the **whole value stream** (request→value). Megaspeed's ticket-centric decomposition is a flow-metrics tool that happens to be built from
  a process-mining event log. It is closer to value-stream mapping than to a DORA dashboard.
- **Practitioners converge** on: Goodhart is real (the same metric stops working once it is a target); DORA is an *outcome scoreboard*, flow is the
  *explanation*; report **distributions (p50/p85), not averages**; **review latency is the most actionable lever** and **nudges** are the best-evidenced
  intervention; dashboards that show a number without pointing to an action are the category's standing complaint.
- **The competitive category converges** on: DORA metrics + PR cycle-time breakdown + benchmarks + investment allocation + developer surveys + AI-impact
  measurement + (increasingly) workflow automation/nudges + capitalization reporting. The standalone "metrics dashboard" business is consolidating hard
  (Pluralsight Flow end-of-life 2027, Code Climate Velocity self-serve closed, Athenian wound down).
- **Megaspeed is differentiated** on (a) ball-in-court / holder attribution of *waiting*, (b) ticket-level lifecycle decomposition including the pre-PR and
  post-merge segments, (c) honest censoring (Kaplan–Meier, "assumed" at-stake), (d) a *mechanistic*, validated what-if (the policy lab), and (e) the
  airgapped, content-free, pseudonymised ingest. **It is missing** distribution-first/aging views, probabilistic forecasting, a "did the change work"
  loop, a single constraint verdict, investment mix, deploy-side DORA data, and any perception data.
- **Top ideas** (§5): (1) a single *constraint verdict* with an exploit→elevate ladder; (2) *aging WIP with self-calibrated percentile thresholds* as the
  chase-list engine; (3) *probabilistic throughput forecast* for sprints/epics; (4) *intervention-aware trend* ("did the change work, or can't we tell?");
  (5) *rolled first-pass yield + guardrail metrics* beside every speed lever.

---

## 1. Theory — the canonical frameworks and why

### 1.1 DORA / *Accelerate* (the pipeline lens)
- **Four keys** (introduced 2013, validated in *Accelerate*, Forsgren/Humble/Kim 2018): change lead time, deployment frequency, change failure rate, failed-deployment recovery
  time (formerly MTTR). They are *throughput* (first, second, fourth) and *stability* (third). They move together for most teams; best performers do well on all four,
  so "speed vs. stability" is a myth at the capability level. [P: dora.dev/guides/dora-metrics, 2024 report]
- **2021–2024 additions.** 2021 widened "availability" to "reliability" (an operational, not delivery, measure; DORA itself says calling it a "fifth metric" was inaccurate).
  **2024 added *rework rate*** (share of deployments that are unplanned fixes for user-facing bugs) because change-failure rate behaves as a proxy for rework; the two together
  form a *stability/instability* factor, separate from a *throughput* factor. DORA now describes **five metrics in two factors**. [P: dora.dev history + 2024 report]
- **2024 finding on AI:** more AI adoption → small *negative* effect on throughput and larger negative effect on stability (−7.2 % per +25 % adoption); DORA's hypothesis is
  that **batch size grew** ("forgot DORA's most basic principle: small batches"). **2025 report:** throughput now improves with AI, instability still increases; "AI is an amplifier";
  introduces **seven team archetypes** (cluster analysis mixing throughput, instability, friction, burnout, valuable work…) instead of the elite/high/medium/low ladder. [P: 2024/2025 reports;
  that the tiers were *retired* is stated by a third party only [S: scums.com] — treat as likely but unconfirmed.]
- **Code review specifically:** 2023 report: teams with faster code review have ~50 % higher software-delivery performance, with the caveat "if your reviews are already fast, don't make them faster" [S: via
  DX newsletter/Nathen Harvey]. Google's own study: median review latency < 4 h across 9 M changes, first response < 1 h for small changes, one-business-day max response. [P: ICSE-SEIP 2018 "Modern Code Review: A Case Study at Google"]
- **Why canonical:** statistically associated with organisational outcomes, measurable from existing systems, team-level, resistant to gaming *as a set*. **Why limited:** survey-derived correlation
  not causal proof; commit→prod only (blind to refinement/approval queues); outcome metrics tell you *that* something moved, not *where*. DORA itself warns against targets, competing between
  teams, and "measuring at the expense of improving". [P: dora.dev common pitfalls]

### 1.2 Flow framework (Kersten, *Project to Product*) — the value-stream lens
- **Flow items:** features, defects, risks, debts. **Five metrics:** *flow velocity* (items done per period), *flow time* (accepted→done, including weekends/waits), *flow efficiency*
  (active time ÷ flow time), *flow load* (WIP, active + waiting), *flow distribution* (mix of the four item types). Correlated with business results (value, cost, quality, happiness). [P: Kersten
  book companion PDF; Planview]
- **Flow time ≠ DORA lead time:** flow time starts when the organisation commits to the work, so it includes the intake/refinement/approval queues DORA never sees. The recurring teaching case
  (practitioner-written, [S]): a team with a 2-hour pipeline lead time, 9 % flow efficiency and 47-day median flow time whose constraint was *pre-commit* queues — "optimising the fastest part of the system".
- **Flow efficiency folklore:** "5–15 % is typical, >40 % is good" is repeated everywhere but traces to Lean-Kanban-University teaching and consultant experience, not a published survey [S: LKU/3cs/Kanban
  University — note LKU itself footnotes it as "taught"]. Treat the *shape* (waiting dominates) as robust and the *number* as folklore. It is also hard to measure honestly: "active" must be defined
  (a PR labelled *In review* while nobody is reviewing is waiting — a point the tenagile/SAFe pieces and megaspeed's ball-in-court model both make).

### 1.3 Little's Law, Kanban flow metrics, forecasting
- **Little's Law:** average WIP = average throughput × average cycle time. Value is less the arithmetic than its **assumptions** (arrival ≈ departure rate, everything that starts finishes,
  WIP/age roughly stable over the window, consistent units); violating them is what destroys predictability. [P: Vacanti, *Actionable Agile Metrics for Predictability*; S: Bartel review, Agile Ramblings]
- **Four basic flow measures** (Vacanti/Kanban Guide): WIP, cycle time, throughput, and **work-item age** — the age of in-flight items, called "by far the most important" because it is the
  only forward-looking one. Report cycle time as a distribution; commit to a **service-level expectation** ("85 % of items finish in ≤ N days").
- **Probabilistic forecasting:** Monte Carlo over historical throughput ("85 % chance by date X") beats story-point velocity in the cases quoted (UCL 500 k-item study showing no story-point↔time
  correlation; Duarte's NoEstimates experiment: 20 % vs 4 % forecast error) — but these are *secondary citations* [S] and the method breaks on erratic/sparse throughput or incomparable item sizes
  (acknowledged even by the pro-MC side, InfoQ/Coleman).

### 1.4 Queueing economics and the Theory of Constraints
- **Reinertsen, *Principles of Product Development Flow*:** queues are the dominant, invisible cost; utilisation raises queues non-linearly (M/M/1: queue ∝ ρ/(1−ρ); delay doubles from 80→90 %);
  *control queue size, not utilisation*; quantify **cost of delay** and sequence by WSJF (cost of delay ÷ duration). Only ~2 % of developers measured queues and ~15 % knew their cost of delay
  in his surveys [P: book ch.1; self-reported anecdotal stats, decades old].
- **Goldratt, *The Goal* / TOC:** one constraint at a time; **identify → exploit → subordinate → elevate → repeat.** The typical software error is to *elevate* (hire, add capacity) before
  *exploiting* (feed it clean work, never idle, no wasted effort). [P/S: Goldratt; software restatements]
- **Value-stream mapping** (Lean; AWS DVSM guidance): per step *process time, wait time, and percent complete & accurate (%C&A)*; flow efficiency = ΣPT ÷ lead time; **rolled %C&A = product of step yields** (8 steps
  at 90 % ⇒ 43 % of items traverse without a loop-back). Timestamps from the work tool beat workshop estimates (people systematically under-estimate wait). [S: AWS prescriptive guidance, tenagile]

### 1.5 SPACE and DevEx (the human lens)
- **SPACE** (Forsgren, Storey, Maddila, Zimmermann, Houck, Butler, ACM Queue 2021): Satisfaction & well-being, Performance, Activity, Communication & collaboration, Efficiency & flow. Core
  claims: productivity is multidimensional; pick **≥3 dimensions** and **at least one perceptual metric**; metrics *should be in tension*; activity counts alone mislead. [P]
- **DevEx** (Noda, Storey, Forsgren, Greiler 2023): three dimensions — **feedback loops, cognitive load, flow state**; combine perceptual (survey) with workflow (telemetry) measures; the
  example given is that code review can be "fast" yet disruptive. [P]
- **DX Core 4** (vendor synthesis of DORA+SPACE+DevEx): Speed, Effectiveness (DXI), Quality, Impact (share of time on new capability). Note the vendor itself admits system data for Quality/Impact is
  still rough (defect ratio / innovation ratio from AI classification of PRs). [V/P: docs.getdx.com]

### 1.6 Review latency evidence (the most concrete intervention research)
- **Meta** (FSE 2022, Using nudges to accelerate code reviews at scale): developer dissatisfaction tracks **p75 time-in-review** (the slowest quarter of diffs), not the median; "time in review" counts only
  when the diff waits on a reviewer (i.e. *ball-in-court*); guardrail metric "eyeball time" to detect rubber-stamping; a **NudgeBot at 24 h stale** produced −6.8 % time-in-review (p = 0.049, borderline),
  −11.9 % of diffs > 3 days, −9.9 % time-to-first-action, in a cluster-randomised 30 k-developer experiment; they could **not** predict review duration well (a similar Microsoft model had a ~32 h mean
  absolute error), so they chose a simple stale threshold. [P: ACM DL paper + engineering.fb.com]
- **Post-acceptance → merge delay** is its own waste (29–63 % of review lifetime on some Phabricator projects) — a segment megaspeed already carves out (`ready`/approval→merge). [P: arXiv 2203.05048]

**What the theory says to *measure* (synthesis):** (1) the full-value-stream time with queues made explicit; (2) WIP/age/throughput as a system (Little's Law holds or not); (3) the single constraint and how loaded it
is; (4) rework/first-pass yield; (5) batch size; (6) a perceptual signal; (7) always teams/systems, always several metrics, always distributions.

---

## 2. Practitioner discourse — recurring themes, warnings, what works

1. **Goodhart / "you get what you measure."** Kent Beck's Facebook survey-score anecdote (score → performance review → target → rolled-up → gamed); Orosz's sprint-points anecdote; Bryan Finster's *How to Misuse and
   Abuse DORA Metrics* (DOEJ 2022): DORA metrics became goals/OKRs and "vanity radiators"; DORA's own page lists *setting metrics as a goal* as pitfall #1. Consensus mitigation: team-level, trend-over-level,
   metrics used diagnostically not evaluatively, paired guardrails. [P/S]
2. **McKinsey (Aug 2023) "Yes, you can measure software developer productivity."** Beck & Orosz's response: McKinsey's custom metrics mostly measure *effort/output*; measure *outcomes/impact*, debug with effort/output
   rather than manage by them; "every measure changes behaviour". Subtext: CEOs/CFOs *do* want an answer, and the vacuum gets filled by whoever offers one. (I did not re-read McKinsey's original article; this is
   the critique's account.) [S: Pragmatic Engineer / Kent Beck newsletters]
3. **DORA vs flow is a false dichotomy.** Recurring framing: DORA = outcome scoreboard / alarm; flow = explanation / investigation; DORA is easy to start (Git+CI), flow needs the issue tracker and an agreed
   active/waiting taxonomy. Divergence pattern to watch: *great DORA, poor flow time ⇒ the constraint is pre-commit*. [S: DevStats, tenagile, Uplevel]
4. **Vanity vs actionable.** Counts (commits, PRs, LoC, velocity) are easy and gameable; actionable metrics point at a queue someone can change (queue age, WIP, review wait, blocked dependency, first-pass yield).
   Dashboards that stop at the number are the standing criticism of the category, including by vendors about each other. [S]
5. **Distributions, not averages.** Right-skewed cycle times: report p50 and p85, show scatterplots/aging charts; the mean is "where relatively few items land". The Meta result is the same lesson from the review side
   (median 2.5 h looked fine; p75 predicted unhappiness). [S/P]
6. **PR/review analytics are where the leverage is.** Small PRs, fast first response, avoid post-approval merge lag, route to the right reviewer, nudge stale ones. Warning (Meta, DORA): *speed alone invites rubber-stamping*
   — pair with a quality guardrail. Warning (Orosz): measuring PR count makes people split PRs for the count. [P/S]
7. **Team-health / burnout signals.** Tools surface weekend/after-hours activity, reviewer imbalance, multitasking, meeting load (LinearB Engineering Health, Hatica, Uplevel, Multitudes). DORA 2024/25 tie
   *instability* and *unstable priorities* to burnout. The ethics debate: only team-level or opt-in individual views, k-anonymity (≥5), no leaderboards; GDPR/works-council constraints in DE/NL. [S/V]
8. **Investment distribution** (features vs bugs vs KTLO, planned vs unplanned) is the CFO-facing lever and the stated #1 reason many leaders buy (Swarmia investment balance, Jellyfish allocation, DX
   "Impact", Kersten's flow distribution). Practical warning: auto-categorisation quality and "capitalisation" use raise audit stakes. [V/S]
9. **Forecasting.** "Count items, not points; forecast with throughput; give a percentile, not a date; re-forecast often." Caveat from the same community: probabilistic forecasts on tiny/erratic samples are
   theatre. [S]
10. **Perception + telemetry.** SPACE/DevEx insist on at least one survey measure; Meta joined survey data to telemetry to find the p75 relationship. Warning: survey scores become targets too (Beck). [P]
11. **The category itself is under strain:** "dashboard alone can't survive as a company" (Pluralsight Flow EOL, Velocity self-serve retired, Athenian gone); buyers want diagnosis → action. Analyst view (Gartner, May 2026):
    platforms are "evolving from software delivery measurement to workflow automation" and assessing AI impact. [S/V]

**"What actually works" (practitioner synthesis):** make *waiting* visible per item with an owner; look at the slowest quarter, not the median; limit WIP / stop starting; shrink batches; set a published review response
expectation (Google: one business day) and **nudge stale items**; keep metrics team-level with guardrails; track *change over time* against your own baseline, not an industry tier; verify with the team.

---

## 3. Competitive landscape

Legend: ● headline/strong · ◐ partial · ○ not found in public material · ? unverified. *All entries are from vendor pages or third-party comparisons dated 2025–2026; see caveats.*

| Product | DORA | PR / review analytics | Flow-eff. / wait / bottleneck | Blocked/risk alerts & nudges | Investment / allocation | Forecasting | Surveys / team health | Benchmarks | AI-impact | Self-host / on-prem | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **LinearB** | ● | ● (coding/pickup/review/deploy; PR size) | ◐ (phase breakdown, "what's slowing") | ● (Slack goals, **gitStream** automations, AI review) | ● (Enterprise) | ● (Monte Carlo, Enterprise) | ● (dev satisfaction; Engineering Health: reviewer imbalance, weekend activity, multitasking) | ● (8.1 M PRs / 4.8 k teams) | ● | ◐ (on-prem agents, Enterprise) | Acts on metrics (automation); Essentials GitHub-only [V] |
| **Swarmia** | ● | ● (review time, WIP) | ◐ | ● (**working agreements** + Slack/Teams nudges) | ● (investment balance) | ◐ (initiatives) | ● (37-q DevEx survey) | ● | ● | ○ (GitHub/GitLab only [S]) | Team-level by design; claims correlation of agreements with faster reviews (own data, [V]) |
| **Jellyfish** | ● | ◐ | ◐ | ◐ | ● (patented allocation model, DevFinOps) | ● (delivery mgmt) | ● | ● (20 M+ PRs) | ● | ○ | Sales-led, no public price; needs clean ticket data [S] |
| **Uplevel** | ● | ◐ | ◐ | ○ | ◐ (limited, per competitor) | ○ | ● (focus time, meetings, burnout; DevEx Discovery interviews) | ○ | ● | ? | Services-heavy ("45-day GearUp"); critics cite thin API/integrations (competitor-sourced) |
| **Code Climate Velocity** | ● | ● (PR Resolution stage breakdown, 60+ metrics) | ◐ | ○ | ◐ | ○ | ○ | ● | ◐ | ? | **Self-serve tier closed Mar 2024**; now enterprise/MCP; user complaints: outliers skew results, no annotation for vacation/process changes [S] |
| **Haystack** | ● | ● (cycle time breakdown) | ◐ | ● (stalled/blocked PR+issue **risk alerts**, high WIP, missed SLA) | ○ | ○ | ◐ | ○ | ○ | ○ | Metadata-only, small teams, fast setup [S/V] |
| **Faros AI** | ● | ◐ | ◐ | ◐ (automation workflows) | ◐ (initiatives, cap.) | ○ | ○ | ● | ● | ● (self-hosted/hybrid; open data model; Community Edition Apache-2) | Strength: connect everything (70+ sources); weakness: you build metrics [S/V] |
| **DX (getdx)** | ● (Core 4) | ◐ | ◐ | ◐ (SLAs, insights) | ● (AI-classified allocation) | ◐ (sprint predictability) | ● (**DXI, snapshots**, triage, playbooks) | ● (direct peer benchmarking) | ● | ○ | Survey-first; **reportedly now part of Atlassian [S: port.io]** |
| **Pluralsight/Appfire Flow** | ● | ● (git-derived) | ◐ | ○ | ◐ | ○ | ○ | ● | ○ | ? | **End-of-life 2027-12-31** (Appfire) [S] |
| **Athenian** | ● | ● (WIP/review/merge/release) | ◐ | ○ | ◐ (Outcome view) | ○ | ○ | ○ | ○ | ○ | Team-only philosophy; **wound down, site redirects** [S] |
| Hatica | ● | ● | ◐ | ◐ | ◐ | ○ | ● (well-being, burnout) | ○ | ◐ | ◐ (private cloud) | Free tier [S] |
| Waydev | ● | ● | ◐ | ◐ (agent) | ● | ○ | ● | ○ | ● | ● (on-prem, Gerrit) | [V] |
| Plandek | ● | ● | ● (500+ metrics incl. Flow) | ○ | ◐ | ◐ | ● | ● | ● | ○ | 180-day history cap [S] |
| Allstacks | ● | ◐ | ◐ | ● (delivery-risk alerts) | ● (capitalisation) | ● (statistical) | ○ | ○ | ◐ | ○ | Sprint-based assumptions [V/S] |
| Multitudes | ● | ● | ◐ | ◐ | ○ | ○ | ● (out-of-hours, review wait, feedback given) | ○ | ◐ | ○ | Reads comment *contents* [P: its own docs]; team-level ethics stance |

**What the category converges on:** (1) DORA as the lingua franca; (2) a PR-lifecycle breakdown (coding → pickup → review → merge → deploy) as the "diagnosis" layer; (3) benchmarks against large peer datasets;
(4) investment/allocation (CFO story) and capitalisation reporting; (5) developer surveys paired with telemetry; (6) AI-tool adoption/impact; (7) a shift to *acting*: working agreements, Slack nudges, workflow
automation, MCP/natural-language assistants; (8) team-level framing to defuse surveillance fears. **Bitbucket Data Center + Jira + air-gap is not their centre of gravity**: most list GitHub/GitLab first; on-prem
is mostly an Enterprise-tier, Faros/Waydev/Oobeya feature.

**What they do not do well (as far as public material shows — absence of evidence, not proof):**
- Mostly measure *the PR/pipeline window*; **ticket-level wait before the first PR and after merge** (refinement, ready, verification) is rarely a first-class decomposition (Athenian/Jellyfish touch it via Jira statuses).
- **Waiting is rarely attributed to a *holder*** with the review state machine (who owes what: peer vs mandatory approver vs author), nor priced on that holder's calendar. "Pickup/review time" is an interval, not a ball.
- **Censoring** (open/declined work in cycle-time medians) is not advertised anywhere I saw; medians over *merged* work flatter you (megaspeed's KM survival fixes this).
- Forecasting is throughput Monte Carlo (LinearB/Allstacks) — *statistical*, not a **mechanistic what-if of a policy** (review SLA, WIP cap, extra approver). Policy modelling was not found in any vendor material.
- Outputs are dashboards; user reviews repeatedly say *metrics without a fix* and *outliers skew* (Code Climate), *no annotation of vacations/process changes*.
- **Privacy:** they ask for broad SaaS access to code hosts and trackers; Multitudes even ingests comment text. Pseudonymisation-before-analysis with a closed vocabulary and *no content ever stored* is not a thing in this category (Network Perspective/Worklytics do HMAC pseudonymisation for *collaboration* data, not delivery).

---

## 4. Gap analysis — megaspeed vs theory + competition

Current surface (from `prd/README.md`, `docs/concepts.md`, `STATE.md`, `docs/QA-final.md`): ball-in-court/blocking/queue/delivery attribution, business-hours calendar with vacations/holidays, flow efficiency,
Kaplan–Meier survival, 8/9-segment lifecycle, review-round/rework/comment-tax/PR-size/vacation/stale-PR/sentinel-tail/sprint-on-time rules, SimPy policy lab with a ±20 % validation gate, ticket page with cohort
benchmark, sprint lens, size sliders, responsive UI. Drafted-not-listed-as-shipped PRDs at the time of research: 50 trend, 51 story rework, 52 sprint-at-risk, 53 worklog legend, 54 review SLA/chase, 55 diagnosis
consistency, 56 UI papercuts, 57 benchmark contrast (STATE.md does not list them as done).

### 4.1 Capability matrix

| Concept (source) | Megaspeed today | Verdict |
|---|---|---|
| Flow time / lead time, queues made explicit (Kersten, Vacanti, VSM) | ● created→done decomposed into 8–9 segments incl. backlog→ready, in-review, verification; business-hours | **Strong / differentiated** |
| Flow efficiency (Kersten, LKU) | ● active ÷ cycle with ball-in-court; calendar-aware | **Strong** (better definition than "In Review = active") |
| Wait attributed to an owner (ToC, VSM handoffs) | ● holders, debt, ready, sentinel gate, per-holder calendar pricing | **Differentiated** |
| Honest treatment of open work (Vacanti: item age; stats) | ● KM survival, "(assumed)" at-stake, "unmeasured" labels | **Differentiated** |
| Constraint identification, one at a time (Goldratt) | ◐ constraint ranking exists but gives **five magnitudes for one person** and contradictory "what first" answers (QA-final) | **Present but not decisive** — prd/55 in flight |
| Work-item **age** vs a service-level expectation (Vacanti; Meta p75/24 h) | ◐ fixed thresholds (stale 5 bd, sentinel tail 24 h); no p85 anywhere (p90 only); no aging view | **Missing the distribution-aware version** |
| Cycle-time **distribution** views (scatterplot, percentiles) | ◐ medians + p90 in text | **Missing** |
| WIP / throughput / Little's Law consistency, CFD (Vacanti, Reinertsen) | ◐ queue WIP, WIP knob in lab; no arrival-vs-departure, no CFD | **Partial** |
| Utilisation→queue non-linearity (Reinertsen) | ◐ ρ shown in lab but chip wording misread ("≈27 % idle") | **Present, mis-presented** |
| Rework / first-pass yield, rolled %C&A (VSM; DORA rework rate) | ◐ PR rounds, reopens, failed verification, bounces exist as events; **no aggregate / no rolled yield** (prd/51 drafted) | **Gap (cheap)** |
| Throughput vs stability pair, speed with guardrail (DORA, Meta eyeball time, SPACE "tension") | ○ rules recommend speed levers without a paired quality guardrail; lab "SLA wins −9 %" is not shown with its rework/zero-approval side effects | **Gap** |
| Probabilistic forecasting (Vacanti, LinearB, Allstacks) | ◐ sprint on-time on closed sprints; calibrated sim of PR cycle; **no "how many/when" forecast** (prd/52 is deterministic classification) | **Gap** |
| What-if of a policy (none of the competitors) | ● SimPy lab, validated ±20 %, verdict per rule (prd/43) | **Differentiated** (unique in what I found) |
| Trend / "did it work" (DORA improvement-over-level; Beck/Orosz) | ○ single-window aggregates (prd/50 drafted) | **Gap** |
| Review latency nudges (Meta/MS) | ◐ stale PR / sentinel-tail rules + prd/54 chase card; real-time alerts are an explicit **non-goal** | **Partial by design** |
| Investment / flow distribution (Kersten, DX Impact, Swarmia, Jellyfish) | ○ issue types (Story/Bug/Epic) are ingested but no mix/per-class flow time | **Gap** (category's #1 buyer feature, not megaspeed's core question) |
| DORA four/five keys (deploy freq., CFR, recovery, rework rate) | ○ no deploy/CI/incident source; "verification→done" covers part of lead time | **Missing** (data source gap, not analytics gap) |
| Team health: after-hours, reviewer imbalance, multitasking, vacation (LinearB, Hatica, Multitudes) | ◐ calendar, vacation blocking, per-person WIP; no after-hours/load view | **Partial** |
| Perception / surveys (SPACE, DevEx, DX, Swarmia) | ○ none (airgapped, no survey tool) | **Missing, partly by design** |
| External benchmarks (LinearB, Jellyfish, DX) | ◐ **internal** cohort benchmark only (prd/57 extends) | **Different by design** |
| AI-impact measurement (everyone) | ○ (and content-free ingest cannot attribute AI authorship) | **Out of scope by design** |
| Privacy posture | ● content-free closed-vocab store, local LLM classification, canary tests, `--verify` | **Differentiated** — with a caveat below |

### 4.2 Where megaspeed is differentiated (and should lean in)
1. **Process-mining honesty.** Ball-in-court + business-hours + censoring answers "where did the time go, and who owed what" in a way the PR-interval dashboards can't. QA-final reproduced >50 numbers by hand.
2. **Ticket-centric decomposition.** It sees refinement (29 % of story cycle in the demo store) and verification, i.e. the segments DORA is blind to and the teaching cases say usually dominate.
3. **Calibrated, validated policy lab** + rule↔lab reconciliation (prd/43). Competitors forecast; nobody (found) lets you test "review SLA / WIP cap / second approver" against a validated model and states "no clear effect".
4. **Airgapped, content-free, pseudonymised** with Bitbucket DC + Jira — an under-served corner.

### 4.3 Honest caveats about the differentiators
- **Small-n.** The demo store is 131 PRs / 100 stories / 3 sprints. Effects of the order of −3 %…−13 % in the lab, correlations r = 0.16–0.62, and per-sprint "50 % delivered" are within plausible sampling
  noise. Vacanti-style forecasting and before/after claims need ≥ ~8–12 periods and explicit intervals. The product shows "n" in places but not uncertainty on lab deltas.
- **Pseudonymisation ≠ anonymity at team size 6.** The store protects content, not identity: one person is the sentinel on 131/131 PRs and is trivially identifiable. Competitors that take privacy seriously
  use ≥5-person aggregation floors (Network Perspective) or avoid person-level views (Swarmia/Athenian/Multitudes stance). Megaspeed's non-goal "not a people-ranking tool" is sound, but person-level
  views (`FrMo ≈ overloaded`) invite exactly the Goodhart/surveillance dynamic §2 warns about. Prefer role/queue-level framing by default; person-level behind an explicit, labelled toggle.
- **"Accurate but not decisive"** (QA-final's verdict) is the exact category complaint (§2 #4/#11). Breadth is not the gap; *one defensible answer* is.

---

## 5. Worthwhile ideas — ranked (value × effort)

Scale: **Value** 1–5 (how much it moves the "speed up delivery" decisions a delivery lead actually makes); **Effort** S ≈ days, M ≈ 1–2 weeks, L ≈ weeks+, given the existing store/analysis. Each is framed as a candidate PRD
and mapped to sources. "Builds on" lists existing PRDs so we don't duplicate them.

### 5.1 Worth doing now

**1. Constraint verdict with an exploit→elevate ladder** — *Value 5 · Effort S–M* · PRD working title: "one answer".
- *Problem.* QA-final: the same constraint is reported as 1.2×/2.2×/3.1×/5.6×; Rules says refine, cap rounds, add sentinel, shrink PRs all at once; "accurate but not decisive".
- *Design.* A single card (Overview top + banner on Rules) that names **the current constraint** (largest *removable* wait pool, by one canonical clock: holder-priced business hours at the segment level) and
  then walks Goldratt's ladder: **exploit** (chase orphaned approvals, review SLA, clean inputs) → **subordinate** (WIP/size policy) → **elevate** (second sentinel/headcount) — each step annotated with the
  lab verdict and "no clear effect" where applicable, so *elevate* is never suggested while *exploit* options remain unrun. Hides lower-ranked rules behind "next constraint".
- *Sources.* Goldratt five focusing steps (§1.4); tenagile/Reinertsen "queues invisible"; Uplevel/others on "metrics without a fix" (§3); QA-final §"accurate but not decisive". Builds on prd/43, prd/55.
- *Risk.* Over-claiming. Must carry "n", ties ("tied" already supported), and the lab's censoring caveats.

**2. Aging WIP with self-calibrated percentile thresholds (the chase-list engine)** — *Value 5 · Effort S–M* · "age vs your own p85".
- *Problem.* Stale thresholds are fixed numbers (5 bd, 24 h); there's no view of *in-flight* items' age relative to history — the forward-looking half of flow.
- *Design.* For each in-flight story/PR/stage: age vs the cohort's own p50/p85/p95 for that stage (and **p75 time-in-review**, the Meta north star computed on holder-waiting time — megaspeed already has this ball
  clock). Output: aging chart + a ranked "older than 85 % of finished work" list with holder, replacing hard-coded stale constants. This list *is* the prd/54 chase card's data source; also feeds #1's "exploit" step.
- *Sources.* Vacanti (work-item age "most important"; SLE); Meta/FSE'22 (p75, 24 h stale; nudges −6.8 %/−11.9 %); Haystack/Swarmia/LinearB (stale/blocked alerts); Google one-business-day response.
- *Delivery constraint.* Airgapped: the *digest* needs a local, raw-zone de-pseudonymise step to be usable (see Later #6); the in-UI list needs nothing new.
- *Risk.* Small cohorts → unstable p85; show n and fall back to "insufficient history" below ~20 samples.

**3. Probabilistic throughput forecast for sprint/epic ("how many / by when")** — *Value 4 · Effort M* · extends prd/52.
- *Problem.* Sprint 2 delivered 15/30 (50 %); 25/30 stories were added after start; the product classifies at-risk items but can't say "85 % chance we finish ≥N of these by the 27th".
- *Design.* Monte Carlo over *weekly stories-done throughput* (count of stories, not points — logged hours/points are uncorrelated here: r ≈ −0.05) from the event log; "when" read at P85, "how many" at P15 (the
  Vacanti/sascha-ms pitfall: confidence flips direction); re-forecast on every snapshot; refuse (and say why) when throughput is sparse/erratic or WIP is rising (Little's assumptions violated). Overlay open-item
  risk classes from prd/52 (done-in-Jira/PR-open, stuck-on-X) as *adjustments*, not point estimates.
- *Sources.* Vacanti/ActionableAgile Monte Carlo; story-points-vs-throughput evidence [S]; LinearB/Allstacks forecasting (category convergence); concepts.md sprint definitions. 
- *Risk.* ~14 weekly buckets is thin: show the interval width as the headline ("16–25 of 30"), not a point.

**4. Intervention-aware trend: "did the change work — or can't we tell?"** — *Value 4 · Effort M* · extends prd/50.
- *Problem.* Single-window aggregates can't answer "did the review SLA help?"; Code Climate users complained that vacations/process changes can't be annotated; DORA says improvement > level; Beck/Orosz: measure impact.
- *Design.* Weekly/sprint series (p50/p85 cycle, flow efficiency, on-time %, WIP, arrival vs departure rate, CFD) **with annotation markers** (team-entered policy changes; vacation/holiday spans from the existing calendar)
  and a before/after panel using a rank test or bootstrap CI on the *distribution*, with an explicit third outcome **"can't tell at this n"**. Add Little's-Law stability flags (WIP trending up with flat throughput = overload).
  This is also the natural home for *batch-size drift* (PR size trend), the DORA-2024 AI-era failure mode, without needing AI attribution.
- *Sources.* DORA "improving matters more than level"; Vacanti stability assumptions; Meta cluster-randomised caution (even they had p = 0.049); Code Climate user feedback; Uplevel/DX trend features.
- *Risk.* Over-interpretation of noise is the main hazard — the "can't tell" state is the feature.

**5. Rolled first-pass yield + guardrails beside every speed lever** — *Value 4 · Effort S* · pairs with prd/51.
- *Problem.* Rules recommend speed levers (review SLA, cap rounds, smaller PRs) but never show what they might cost; 5 reopened + 4 failed-verification + 21 review bounces exist but no aggregate says "only X % of stories
  go through without a loop-back".
- *Design.* (a) Per-segment **%complete-and-accurate** (items not bounced back) and their **product** — the VSM "rolled %C&A" — on the Overview/ticket cohort; (b) a **stability panel** that mirrors DORA's instability
  factor using tracker data: reopen rate, failed-verification rate, bug-type share of throughput (proxy for rework rate — label as a proxy); (c) the lab and rules display a **guardrail** next to each lever's speed result
  (rounds, zero-approval merges, reopen rate), the Meta "eyeball time" idea translated to content-free signals.
- *Sources.* VSM %C&A and rolled yield (AWS DVSM, tenagile); DORA 2024 throughput/instability split and rework rate; Meta eyeball-time guardrail; SPACE "metrics in tension"; Orosz PR-splitting warning.
- *Risk.* The lab must actually *model* the guardrail effect or say it doesn't ("rework held constant in this model").

### 5.2 Nice later (ranked)

**6. Outbound chase digest with a local reveal step** — *Value 4 · Effort M* — the only intervention with experimental evidence (Meta, Microsoft). Blocked by two real tensions: real-time alerting is a stated non-goal (fine: a
   *daily/ weekly static digest*, not a pager) and pseudonyms are useless in a message (needs a raw-zone-only mapping to render names, kept outside the store). Do after #2 and prd/53's legend; decide the trust boundary first.
   Sources: Meta/FSE'22; Swarmia working agreements; Haystack/LinearB alerts.

**7. Flow distribution / investment mix by issue type** — *Value 3–4 · Effort S (types) / M (labels)* — features vs bugs vs other from `issuetype` (already ingested; closed vocabulary, no new leakage), with per-class
   flow time ("bugs wait 2× longer than stories") and planned-vs-unplanned. This is the category's most-bought feature (Swarmia, Jellyfish, DX Impact, Kersten's fifth metric) but it answers a CFO question, not the delivery
   lead's; rank after the decision-quality items. Skip LLM classification of prose to keep the contract intact; use type + label whitelist.

**8. Release/deploy proxy for change lead time and deployment frequency** — *Value 3 · Effort M–L* — Bitbucket tags/builds or Jira fixVersion/release as a proxy so megaspeed can emit DORA-comparable *lead time* and
   *frequency*. Valuable for category credibility and for the "divergent DORA vs flow" diagnosis, but data quality will vary per install and CFR/recovery need an incident source (likely absent). Do last of the data-source work; label as proxy.

**9. Load & sustainability panel** — *Value 3 · Effort S–M* — after-hours/weekend activity share (timestamps + existing calendar), review-load concentration (e.g. share of review-hours by top reviewer), multitasking (WIP/person),
   all **role/team-level by default**. Map to LinearB Engineering Health, Multitudes, DORA burnout findings. Privacy caveat from §4.3 applies; use ≥5-person floors where a team permits.

**10. Optional cost-of-delay weights for "at stake" ordering** — *Value 3 · Effort S + manual input* — let a lead assign a per-epic cost of delay (even coarse: 1–3–10) so chase lists sort by WSJF instead of raw hours. Reinertsen's single most
    pitched idea; but it adds a human-entered number that people will argue about and megaspeed can't validate. Keep optional.

**11. Cited reference ranges (not benchmarks)** — *Value 2 · Effort S* — static, sourced constants in tooltips (Google median review < 4 h, first response < 1 h for small changes; Meta p75 24 h; DORA 2023 "faster review ↔ +50 %") with the
    "your own trend matters more than the level" caveat. Gives context without a peer-data feed.

**12. Perception pulse import (SPACE/DevEx)** — *Value 3 · Effort M–L* — a 3-question pulse tied to pseudonymous team so p75 time-in-review can be correlated with felt speed (the Meta analysis). Hard parts: anonymity at n≈6, survey fatigue, and
    that survey scores become targets (Beck). Lowest priority; a lightweight alternative is to ship "ask the team" prompts alongside each rule.

### 5.3 Deliberately *not* doing (and why)
- **AI-authorship / AI-ROI attribution.** Everyone has it; it needs content or tool telemetry and breaks the contract. Watch *batch-size drift* and *rework* instead (#4, #5), which is where DORA 2024 says AI harm shows up.
- **Per-person scores, leaderboards, "productivity" indices.** Contradicts the stated non-goal, the whole Goodhart/McKinsey literature, and makes the pseudonymisation theatre at n≈6.
- **External peer benchmark feed / DORA performance tiers.** Needs a multi-tenant data business; the tiers themselves are reportedly retired in favour of archetypes; DORA warns against "industry as a shield" and competing.
- **Real-time pager alerts / full SaaS-style integrations breadth.** Different product; the category is consolidating exactly because breadth-without-action doesn't pay.

### 5.4 Cross-cutting principles to apply to all of the above
1. **Show n and an interval** on every statistic that drives advice; "no clear effect" is a valid, valuable output.
2. **One clock per concept** (concepts.md already enforces this; #1 depends on it).
3. **Distribution first** (p50/p85, aging), never the mean; **team/role first**, person behind a toggle.
4. **Every speed lever carries its guardrail.**
5. **Verify with the team** — pair numbers with a question to ask (the DevEx/SPACE perceptual requirement, done cheaply).

---

## 6. Uncertainty and what I could not verify

- **Competitor feature matrix:** built from vendor sites (LinearB, Swarmia, Jellyfish, DX, Faros, Uplevel), vendor-authored comparison posts (Waydev, Faros, Jellyfish, Uplevel, Axify, Typo, Pensero each promote themselves while
  describing rivals), and roundup sites. Cells marked ○ mean "I did not find it", not "does not exist". Pricing/plan gating changes frequently. I did not trial any product.
- **Single-source facts:** DX "now part of Atlassian" (port.io only); Pluralsight/Appfire Flow EOL date (Axify/hivel citing Appfire); Athenian wind-down (scums.com, apis.io); Sleuth "pivoted away" (devyardstick
  changelog); DORA "retired the tiers" (scums.com). The DORA 2025 report text itself does confirm the seven archetypes and five metrics.
- **Numbers that are folklore or secondary:** flow efficiency "5–15 % typical"; Reinertsen's "2 % measure queues / 15 % know cost of delay"; UCL 500 k-item story-point finding and Duarte's 20 %/4 % (quoted via a blog); Microsoft
  Nudge "−60 %, 8 500 PRs" (quoted via a Springer paper's related-work). The Meta nudge result is primary but borderline (p = 0.049) and in a monorepo at hyperscale — generalisation to a 6-person team is untested.
- **DORA** is observational survey data; its own authors say metrics are outcomes, not causes. **McKinsey's original article** was not re-read; the critique is Beck & Orosz's.
- **Megaspeed's own state:** drafted PRDs 50–57 may have progressed since STATE.md; "missing" in §4 means "not listed as shipped in STATE.md/QA-final.md at research time". I did not run the product.

---

## 7. Sources

**Theory**
- DORA metrics guide & history: https://dora.dev/guides/dora-metrics/ · https://dora.dev/insights/dora-metrics-history/
- 2024 Accelerate State of DevOps (rework rate, AI & batch size): https://dora.dev/research/2024/dora-report/2024-dora-accelerate-state-of-devops-report.pdf
- 2025 State of AI-assisted Software Development (seven archetypes): https://dora.dev/research/2025/dora-report/ · PDF mirror via Thoughtworks
- Forsgren et al., SPACE (ACM Queue 2021): https://queue.acm.org/doi/10.1145/3454122.3454124
- Noda, Storey, Forsgren, Greiler, DevEx (ACM Queue/CACM 2023): https://queue.acm.org/detail.cfm?id=3595878
- Kersten, *Project to Product* / Flow Framework (companion PDF https://itrevolution.com/wp-content/uploads/2022/06/P2P_audio-companion.pdf; Planview guide)
- Vacanti, *Actionable Agile Metrics for Predictability*; ProKanban *Flow Metrics for Scrum Teams*: https://prokanban.org/pdfs/flow-metrics-for-scrum-teams-en.pdf · Little's Law assumptions: https://agileramblings.com/2012/12/11/littles-law-its-not-about-the-numbers/
- Kanban University, flow efficiency: https://kanban.university/flow-efficiency-a-great-metric-you-probably-arent-using/ · 3cs: https://www.3cs.ch/measuring-flow-efficiency/
- Reinertsen, *Principles of Product Development Flow* ch.1: http://celeritaspublishing.com/wp-content/uploads/2013/07/ReinertsenFLOWChap1.pdf
- Theory of Constraints / VSM: https://deliveryplaybook.com/playbook/documentation/guides/theory-of-constraints · AWS DVSM https://docs.aws.amazon.com/pdfs/prescriptive-guidance/latest/strategy-devops-value-stream-mapping/strategy-devops-value-stream-mapping.pdf · https://tenagile.com/books/value-stream-operating-models/ch-10/
- Google, *Modern Code Review: A Case Study at Google* (ICSE-SEIP 2018): https://research.google/pubs/modern-code-review-a-case-study-at-google/
- Meta, *Using nudges to accelerate code reviews at scale* (FSE 2022): https://dl.acm.org/doi/10.1145/3540250.3549104 · https://engineering.fb.com/2022/11/16/culture/meta-code-review-time-improving/
- Waiting between acceptance and merge: https://arxiv.org/abs/2203.05048
- Process mining of PR/issue/review logs: https://dl.acm.org/doi/10.1145/2597073.2597081 · https://arxiv.org/abs/2510.25935

**Practitioner**
- Beck & Orosz, response to McKinsey: https://newsletter.kentbeck.com/p/measuring-developer-productivity · https://newsletter.pragmaticengineer.com/p/measuring-developer-productivity-part-2
- Finster, *How to Misuse & Abuse DORA Metrics*: https://bryanfinster.com/whitepapers/dora-metrics · InfoQ anti-patterns https://www.infoq.com/articles/dora-metrics-anti-patterns/
- DORA vs flow: https://www.devstats.com/blog/flow-metrics-vs-dora-metrics · https://tenagile.com/books/ai-agile-copilot-risk-predictor/ch-08/
- Monte Carlo forecasting: https://prokanban.org/blog/story-points-with-mcs · https://www.infoq.com/articles/sizing-forecasting-scrum/ · https://sascha-ms.de/en/monte-carlo-forecasting-delivery-dates
- Privacy-safe analytics: https://www.networkperspective.io/ethics-in-people-analytics · https://docs.multitudes.com/account-management/security-and-privacy

**Competitors (vendor / comparison)**
- LinearB: https://linearb.io/platform/engineering-metrics · https://linearb.io/pricing · release notes https://linearb.helpdocs.io/article/b7okinmoom-release-notes-2026
- Swarmia: https://www.swarmia.com/answers/ · https://www.swarmia.com/product/working-agreements/ · https://www.swarmia.com/product/investment-balance/
- Jellyfish: https://jellyfish.co/platform/engineering-management-platform/ · review https://johndeveloper.substack.com/p/jellyfish-review-2026-whats-good
- Uplevel & Code Climate Velocity: https://uplevelteam.com/blog/top-engineering-intelligence-platforms-2026 · https://pensero.ai/blog/code-climate-velocity-reviews · https://coderbuds.com/blog/code-climate-velocity-alternatives-2026
- Faros AI: https://community.faros.ai/docs/welcome · https://www.faros.ai/blog/faros-ai-a-modern-data-stack-for-engineering-operations
- DX: https://docs.getdx.com/dx-core-4/ · https://docs.getdx.com/concepts/ · https://www.port.io/blog/what-does-dx-do
- Pluralsight/Appfire Flow: https://appfire.com/newsroom/appfire-acquires-flow · https://axify.io/blog/pluralsight-flow-alternatives
- Athenian: https://techcrunch.com/2022/03/02/athenian-gives-you-metrics-about-your-engineering-team-without-focusing-on-individuals/ · https://www.scrums.com/blog/athenian-alternative-dora-metrics
- Landscape roundups: https://engineeringintelligenceplatforms.com/ · https://waydev.co/navigating-the-software-engineering-intelligence/ · https://devyardstick.com/ · Gartner critical capabilities (summary) https://www.gartner.com/en/documents/7810517

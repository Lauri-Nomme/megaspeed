# 17 — Analysis → suggested rules

The layer that turns the pseudonymised event log into a **ranked list of concrete "change this"
suggestions**, each with evidence and severity. It answers "what should we change first" — the
product's headline output (prd/15).

## Metrics (computed from the current store: events, objects, `push_class`, `comment_type`,
`thread_outcome`, `question`)

1. **`time_to_first_review`** — per PR, `min(ts of first reviewer action: comment_added | pr_approved) − pr_opened`. Median + p90.
2. **`cycle_time` / `time_to_merge`** — per PR, `pr_merged.ts − pr_opened.ts`. Median + p90.
3. **`reviewer_responsiveness` (self-vs-other)** — per person: median *review-ball* hours (time they
   hold the ball as reviewer) vs *author-ball* hours (time they hold it as author). Flag people whose
   review latency is high while their own cycle is low.
4. **`rework_loops`** — review rounds per PR (from `pr_pushed` / `pr_unapproved`), split by `push_class`
   (`content` / `pure_rebase` / `rebase_conflict`).
5. **`rebase_waste`** — approvals lost (`pr_unapproved`) where the causing push was `push_class=pure_rebase`.
6. **`comment_tax`** — threads grouped by `comment_type` × `thread_outcome`; "wasted rounds" = count of
   `rejected` / `walked_back` threads (each ≈ a re-review round for zero value).
7. **`conformance`** — PRs merged with 0 approvals; PRs with > N review rounds.
8. **`sentinel_queue`** — (when role/sentinel data exists) median wait on the mandatory approval.
9. **`pr_size`** — commits per PR (count of COMMIT objects, already in the store) + diff size
   (lines added/removed, scrape-side capture per prd/16), vs review latency.

## Suggestion engine

Each metric → one or more **rule templates** (threshold + severity). A suggestion is
`{id, metric, title, evidence:{concrete numbers}, suggestion (plain-language rule), severity, impact_hours}`.
Rank by estimated hours saved × confidence (sample size). Person-level rules reference pseudonyms only.

Example templates:
- "Median time-to-first-review is 41 h (target < 24 h) → adopt a review SLA / first-response target."
- "N approvals were lost on pure rebases → don't rebase unless necessary; tie auto-unapprove to content change."
- "K% of review threads are nits/out-of-scope that got rejected → move nits/out-of-scope to the ticket, not the PR."
- "P PRs merged with no approval → enforce the +1 rule."
- "The mandatory-approval (sentinel) queue is the longest wait → rotate / add sentinels."
- "PRs over N commits / M lines get the slowest reviews → adopt a PR size cap."

## API

`GET /api/insights?tenant=&days=&level=team|person` →
`{ generated_at, as_of, hours_mode:"wall-clock", sample:{prs, days},
   suggestions:[{id, metric, title, evidence:{...}, suggestion, severity, impact_hours}] }`

## UI

A **"Rules"** view (the Policy-lab "What should we change first?" question): a ranked, dense list of
suggestions — severity-tinted, numbers-first, each with the plain-language rule + evidence + impact.
Per prd/10 design language (dense table, semantic color, sentence-case).

## Out of scope (v1)

Business-hours calendar (wall-clock for now) · story/epic level · question-phase full spectrum ·
simulation (policy lab).

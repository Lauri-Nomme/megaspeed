# 29 — Configurable workflow stages (real support for the install's statuses)

The 3-bucket projection (`new/indeterminate/done` → `todo/in_progress/done`) collapses the phases
that matter. The install's real workflow is:

- **Story**: OPEN → READY FOR REFINEMENT → REFINED → IN PROGRESS → IN REVIEW → IN VERIFICATION → CLOSED
- **Bug**:   NEW → TRIAGED → IN PROGRESS → IN REVIEW → IN VERIFICATION → RESOLVED

The product must recognise these *stages* and derive the ticket lifecycle from them, configured per
workflow, so "time in QA", "time in review" and "time in refinement" are measured rather than folded
into one "in progress".

## 1. Stage vocabulary (closed, fixed set)

`STATUS_STAGE = {"backlog", "ready", "in_progress", "in_review", "in_verification", "done", "other"}`

| stage | meaning | this workflow |
|---|---|---|
| `backlog` | not ready to be worked | OPEN, NEW, READY FOR REFINEMENT |
| `ready` | refined/triaged, ready to start | REFINED, TRIAGED |
| `in_progress` | being developed | IN PROGRESS |
| `in_review` | under review | IN REVIEW |
| `in_verification` | under QA/verification | IN VERIFICATION |
| `done` | closed | CLOSED, RESOLVED |
| `other` | fallback | — |

## 2. Config

```toml
[jira.stages]            # workflow status name (lower-cased, exact) -> stage
open                 = "backlog"
"ready for refinement" = "backlog"
refined              = "ready"
"in progress"        = "in_progress"
"in review"          = "in_review"
"in verification"    = "in_verification"
closed               = "done"
new                  = "backlog"
triaged              = "ready"
resolved             = "done"
```

Env: `MEGASPEED_JIRA_STAGES="open=backlog,in progress=in_progress,in verification=in_verification,…"`
(comma-separated `name=stage`; TOML is authoritative, env merges over it). A stage outside the closed
set is an error, never a silent `other`. The mapping is echoed in the scrape manifest/report so a
store's projection is reproducible.

## 3. Projection (replaces `todo/in_progress/done`)

`status_stage(status_id, name, categories, stage_map)`:

1. `stage_map` (config, by normalised name) wins;
2. else Jira's own `statusCategory` — `new→backlog`, `indeterminate→in_progress`, `done→done`;
3. else name keyword rules (extended: `ready for refinement`/`refined`/`triaged`/`ready` →
   `backlog`/`ready`, `in review`/`in verification` → their stages).

`to_status`/`from_status` now carry the stage. `taxonomy.STATUS_CLASS` becomes `STATUS_STAGE`
(`CLASSIFIER_VERSION` bump). Legacy `todo` in an old store normalises to `backlog` on read.

## 4. Lifecycle (analysis/jira.py)

New status-driven boundaries, with the PR detail nested inside them:

- `ready` — first status→`ready` (fallback: first `pointed`, for workflows without a refined status);
- `in_review` — first status→`in_review`;
- `in_verification` — first status→`in_verification`.

The 8 segments stay; `merge→done` becomes `merge→verification` + `verification→done` when the
`in_verification` boundary exists (falls back to today's `merge_done` otherwise). `in_review` is a
Jira-level cross-check of the PR review (a ticket `in_review` with no PR is reported honestly).

## 5. Ticket detail + rules + UI

- `GET /api/ticket/{key}` + `#ticket/KEY`: render the verification segment and boundary; missing
  `in_verification` is named honestly (like the other missing boundaries).
- `waiting_time_decomposition` carries the new segment; `jira_conformance` uses the stage boundaries.
- Timeline/phase colours: two new hues for `in_review`/`in_verification`.

## 6. Definition of done

- A store scraped from the install's workflow (config above) yields distinct `ready`, `in_review`,
  `in_verification` boundaries and measures QA time (`verification→done`) separately.
- No config ⇒ byte-equivalent to today (3 coarse stages, `backlog` in place of `todo`).
- Synthetic stores with the exact Story + Bug statuses produce the right segments (incl. the QA
  segment and the `refined`/`ready` boundary); legacy `todo` stores still read.
- `check_attributes` accepts only the closed stage set. Tests green; no regressions in the 8-segment
  decomposition for the unconfigured path.

## Out of scope

Re-seeding the lab with this workflow (follow-up: `jira-lab/seed.py` → the Story/Bug workflows above,
then re-scrape `del-combined` to show real review/QA numbers).

# 21 — Jira-layer analysis

With Story/Epic/Sprint objects + worklogs + status/sprint events now in the store (the Jira ingest),
the analysis can compute the ticket-layer metrics prd/05 / prd/01 promised.

## Metrics

1. **Waiting-time decomposition** — the 8 segments: ready→refined · refined→sprint · sprint→in-progress ·
   in-progress→PR-open · PR-open→first-review · review rounds · final-approval→merge · merge→done. Built
   from Story status transitions (`status_changed`) + Sprint membership (`sprint_added`/`sprint_removed`) +
   the PR events. Report per-segment flow efficiency (business hours), so "where does the time go" is
   answered per phase.
2. **Carry-over** — stories added to >1 sprint (repeated `sprint_added` across sprints). Count + list them;
   it's the "sprint plan isn't honest" signal.
3. **Point churn** — points changed (`pointed`) *after* the story left the backlog (status → in-progress).
4. **Worklog cross-check** — worklog hours vs the activity signal, and the `log_delay_bucket` distribution
   (same_day / next_day / within_week / late) as the "worklog noise" measure — how much to trust logged time.
5. **Conformance (Jira side)** — work started before refinement, points changed after start, carry-over.

## Shape

Expose as metrics in `analysis/rules.py` + `GET /api/insights` + suggestion templates (ranked, evidence +
plain-language rule, per prd/17). Business hours via the calendar (prd/19); fall back to wall-clock.

## Out of scope

Tempo-specific fields (native worklogs for now) · epic-level rollups · Jira board/column fidelity.

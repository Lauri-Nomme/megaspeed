# 26 — Mined-but-unimplemented insights (review backlog)

Distilled from the review docs (`QA-REPORT`, `QA-UI-REPORT`, `QA-round2`, `review-product`,
`review-adversarial`, `review-round2`, `review-integration`) — the items that were flagged as gaps
but never built. Prioritised by value to "real answers".

## P5 — Rules → evidence drill-down (HIGH)

The Rules page shows ranked suggestions but has **no drill-down**: a suggestion's evidence is a flat
number ("16 PRs past 3 rounds", "15% threads wasted") with no way to see *which* items. Make each
suggestion expand to its underlying PRs/stories (the `metrics.*.rows` already carry them). This closes
the loop from "what to change" to "which items, go look".

## P6 — Sprint dimension (HIGH)

The team plans in two-week sprints, but there is no sprint lens. Add: sprint **bands** on the timeline,
**scope** (stories in vs out of a sprint), and a **carry-over mark** (a story re-added across sprints).
The data is already in the store (SPRINT objects + `sprint_added`/`sprint_removed`).

## P7 — Ticket-level censoring (MED)

The ticket detail (prd/25) must show censoring — open stories / open PRs — so a ticket's cycle isn't
presented as complete when it isn't (the same honesty `cycle_time` got for PRs).

## G9 — API regression tests (MED)

Round-1 QA produced a curl matrix (filters, pagination, 400/404, the harness token gate) that was
never promoted to `tests/test_server.py`. Promote it so the API contract can't regress.

## FR-8 — `--jira --fixture --limit` ignores `--limit` (SMALL)

The Jira fixture path doesn't honor `--limit` (`scrape_cli._jira_transport`). One-line plumbing fix.

## Deferred (polish, low value — leave)

Light theme · theme toggle · refresh cadence · Rebuild/read-time in the provenance line.

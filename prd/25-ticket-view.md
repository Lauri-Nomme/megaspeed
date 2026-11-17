# 25 — Ticket-centric view

The UI renders PR objects only; the ticket (story) is the first-class unit for "why did DEL-167 take
3 weeks", but it's absent from every view. This adds a ticket-centric view: PRs grouped under their
ticket, and a ticket detail page joining the 8 segments + PRs + sprint + worklog.

## API

`GET /api/ticket/{key}?tenant=` → the ticket's full cycle:
- `story`: key, points, state, epic_key, created/closed.
- `segments`: the 8-segment decomposition (hours, share, flow efficiency, missing) with the 9 boundary
  timestamps.
- `prs`: the ticket's PRs (open/merge, review rounds, `push_class`, censored, intervals).
- `sprints`: membership (add/remove + dates).
- `worklogs`: `time_spent` + `log_delay` + author.
- `events`: the ticket's status/sprint/point events.

Also: `?ticket=` on `/api/events` must match `STORY:<key>` (not only `TICKET:`), so story events are
filterable.

## UI

- **Ticket detail page** (reachable from any ticket key): the 8-segment bar (phase-coloured, per the
  PR-gantt vocabulary extended to the 5 story segments) + the PR rows beneath it + a sprint strip +
  worklog-vs-active hours + a one-line prose answer ("the store cannot say X because the Y boundary is
  missing" when a segment is absent — honesty, per prd/10).
- **Ticket keys are links** everywhere: timeline lane labels, delivery table, drawer, rules evidence,
  Markdown export.
- **`stories` lane dimension** in the timeline: stories as lanes with their PRs nested beneath
  (epic → story → PR → event), semantic zoom.

## Out of scope

Epic-level rollup UI · sprint-band UI (P6) · the Cloud Jira proxy (prd/24).

## Small adjacent fix

`analysis/jira.py::_note()` currently returns the "no … event in this window" note even when the metric
is available (e.g. worklog note says "no worklogged event" with 199 counted). Make the note `None` when
the metric is available.

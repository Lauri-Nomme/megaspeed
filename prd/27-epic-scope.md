# 27 — Epic scoping

Scope the Jira scrape to a set of **epics** (the team's epics in a shared project), resolving to
their child stories — the middle ground between "whole project" and "explicit ticket set".

## Config

- `MEGASPEED_JIRA_EPICS` — comma-separated epic keys (e.g. `DEL-1,DEL-2`). At scrape start, resolve
  each epic's child issues via JQL `"Epic Link" in (DEL-1, DEL-2)` (one extra search), then scrape the
  union of the epics + their children using the existing `key in (...)` scope. Works for both
  `--jira` and `--jira-cloud`.

## Semantics

- Precedence (more specific wins): explicit ticket set (`MEGASPEED_TICKET_KEYS`) > epic set
  (`MEGASPEED_JIRA_EPICS`) > project (`MEGASPEED_JIRA_PROJECTS`).
- Same incremental/watermark/checkpoint/capped-run semantics; the resolved key set rides in the
  manifest/state so import + `--verify` scope the same way.

## Tests

Resolve epics → children (union); scope to that union; precedence (ticket set over epic set over
project); a childless epic; equivalence with the explicit ticket-set path.

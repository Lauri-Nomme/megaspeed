# 30 — Tempo worklogs (DC + Cloud)

Read timesheet worklogs through **Tempo** (the org's time-tracking add-on), not just Jira's native
worklog. Production uses Tempo; the scrape must read it.

## Current state

- **Cloud** (`ingest/jira_cloud.py`): `tempo=True` already reads `/rest/tempo-timesheets/4/worklogs/{key}`
  and normalises `worklogId`→`id` into the native worklog shape.
- **DC** (`ingest/jira.py`): only native `/rest/api/2/issue/{key}/worklog`. No Tempo path.
- The cloud proxy (`proxy/cloud_jira_proxy.py`) stubs Tempo over native worklogs (lab has no Tempo).

## Work

1. **DC Tempo support**: `ingest/jira.py` gains a `tempo` flag (default off) and `tempo_worklogs(key)`
   reading the DC Tempo endpoint (Tempo Server/DC API), normalised into the same native worklog shape
   the reducer already consumes. The `timeSpentSeconds`/`started`/`author` mapping matches the Cloud
   adapter, so the store shape is identical regardless of source.
2. **Config/CLI**: `jira_tempo: bool` config + `MEGASPEED_JIRA_TEMPO` env + `--tempo` (implies `--jira`
   or `--jira-cloud`). Works for both sources.
3. **Deterministic test** (the lab has no Tempo, and the installer JAR is empty — a real install needs
   the org's Tempo license): a Tempo transport/fixture serving the Tempo worklog payload, asserted to
   reduce to the same worklogged events + `log_delay_bucket` as the native path. A live Tempo install is
   a follow-up gated on the license.

## Definition of done

- DC + Cloud both read Tempo worklogs when configured; the reduced events are identical in shape to the
  native-worklog path.
- Tempo payloads (DC shape + Cloud shape) reduce correctly in tests; no raw Tempo `comment` crosses into
  the store (prd/02).
- Existing 715 + 73 green.

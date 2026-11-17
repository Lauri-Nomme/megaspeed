# 24 — Cloud Jira scrape (via a DC→Cloud emulation proxy)

Atlassian Cloud is **SaaS-only — there is no local install.** For integration fidelity, emulate the
Cloud API surface in front of the DC lab with a thin proxy, so the cloud-Jira scrape is developed and
tested locally against real-shaped responses.

## Why a proxy (not just a second client)

Cloud differs from DC in the API surface: auth (Bearer PAT vs Basic), base path (`/rest/api/3/` vs
`/rest/api/2/`), field IDs (`customfield_XXXXX`), some renamed fields, pagination (cursor + `startAt`),
and the separate Tempo Cloud API (`/rest/tempo-timesheets/4/`). The proxy maps Cloud-request →
DC-request and translates the response, so the scrape targets the Cloud shape; production just swaps
the base URL + PAT for the real Cloud.

## Proxy scope (only what the ingest uses)

- **Auth**: Bearer PAT → Basic (DC admin) upstream.
- `/rest/api/3/search` (JQL, pagination) → `/rest/api/2/search`.
- `/rest/api/3/issue/{key}?expand=changelog` → `/rest/api/2/issue/{key}?expand=changelog`.
- `/rest/api/3/issue/{key}/worklog` → `/rest/api/2/issue/{key}/worklog`.
- `/rest/agile/1.0/board/{id}/sprint` → the DC agile endpoint (same path).
- **Field IDs**: map Cloud's documented `customfield_*` names → the DC lab's discovered IDs.
- **Tempo Cloud** (`/rest/tempo-timesheets/4/worklogs/…`) → DC native worklogs (or skip + note, since
  Tempo isn't in the DC lab).

## Ingest

A `--jira-cloud` source in the scrape using the Cloud shape (Bearer auth, v3, cursor pagination, field
mapping). Reuses the existing Jira object/event model (Story/Epic/Sprint, status/sprint/assign/point/
worklog). Tested against the proxy; production = real Cloud URL + PAT.

## Out of scope

Tempo Cloud full fidelity (until Tempo DC is installed) · Cloud-specific fields the ingest doesn't use.
